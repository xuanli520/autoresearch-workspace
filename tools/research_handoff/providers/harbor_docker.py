"""Optional Harbor provider: default-bridge builds and isolated Agent execution.

Requires the pinned Harbor Docker provider. The generic controller itself does
not import Harbor. Explicit recovery changes only its configured bridge and
model HTTPS forwarding rules, never shared chain policies or daemon services.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.docker import DockerEnvironment, _sanitize_docker_compose_project_name
from harbor.models.task.config import NetworkMode
from ..core.docker_network import bridge_preflight, egress_rules, validate_network_config


class ManagedDockerEnvironment(DockerEnvironment):
    def __init__(self, *args, network_config=None, model_host_addresses=None,
                 use_default_bridge=True, ownership_root=None, completion_contract=None,
                 public_image_digest=None, gpu_attachment="device-request", gpu_memory_policy=None, **kwargs):
        if type(use_default_bridge) is not bool:
            raise ValueError("use_default_bridge must be boolean")
        if not use_default_bridge:
            raise ValueError("managed Harbor provider requires the Docker default bridge")
        if gpu_attachment not in {"device-request", "cdi"}:
            raise ValueError("gpu_attachment must be device-request or cdi")
        self.gpu_attachment = gpu_attachment
        self.gpu_memory_policy = gpu_memory_policy
        self._gpu_memory_guard = None
        self._gpu_guard_dir = None
        environment_dir = kwargs.get("environment_dir", args[0] if args else None)
        self._trusted_verifier = environment_dir is not None and Path(environment_dir).name == "tests"
        self.network_config = validate_network_config(network_config or {
            "enabled": True, "docker_host": os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
            "model_host_addresses": model_host_addresses or {}})
        self.model_host_addresses = self.network_config["model_host_addresses"]
        self.ownership_root = Path(ownership_root).resolve(strict=True) if ownership_root else None
        self._managed_build_path = None
        self.completion_contract = completion_contract
        self.public_image_digest = public_image_digest
        if completion_contract and not self._trusted_verifier:
            from ..core.completion import trust_boundary
            from ..core.longrun import read_json
            self.completion_contract = read_json(Path(completion_contract))
            trust_boundary(self.completion_contract)
            if not public_image_digest:
                raise ValueError("scientific completion requires an audited public image digest")
        super().__init__(*args, **kwargs)
        if self.ownership_root:
            if self.ownership_root.stat().st_uid != os.getuid():
                raise ValueError("container receipts require a trusted host-owned directory")
            project = _sanitize_docker_compose_project_name(self.session_id)
            self._receipt(project, {"project": project, "role": self.environment_dir.name,
                                    "session_id": self.session_id})

    def _receipt(self, name, value):
        from ..core.longrun import atomic_json
        if self.ownership_root:
            value = {"project": _sanitize_docker_compose_project_name(self.session_id), **value}
            atomic_json(self.ownership_root / f"{name}.json", value)

    def _requires_egress_control(self, startup_network_policy, phase_network_policies):
        if self._trusted_verifier:
            return False
        return super()._requires_egress_control(startup_network_policy=startup_network_policy,
                                              phase_network_policies=phase_network_policies)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return super().capabilities.model_copy(update={
            "gpus": True, "mounted": not self._trusted_verifier,
            "disable_internet": True})

    @property
    def _docker_compose_paths(self):
        paths = list(super()._docker_compose_paths)
        if self._managed_build_path is None:
            # TMPDIR is set by the official worker to the verified data disk.
            import tempfile
            self._managed_build_dir = tempfile.TemporaryDirectory()
            self._managed_build_path = Path(self._managed_build_dir.name) / "managed-build-network.json"
        main = {} if getattr(self, '_use_prebuilt', False) else {"build": {"network": "default"}}
        if self._trusted_verifier:
            main['network_mode'] = 'none'
        if self.gpu_memory_policy:
            if self.ownership_root is None:
                raise ValueError('host GPU enforcement requires ownership_root')
            if self._gpu_guard_dir is None:
                self._gpu_guard_dir = self.ownership_root / ('gpu-guard-' + _sanitize_docker_compose_project_name(self.session_id))
                self._gpu_guard_dir.mkdir(mode=0o700, exist_ok=False)
            main.setdefault('volumes', []).append({
                'type': 'bind', 'source': str(self._gpu_guard_dir),
                'target': '/run/autoresearch-gpu-guard', 'read_only': True})
        services = {"main": main}
        if os.environ.get("GPU_SCHEDULER_JOB_DIR"):
            from tools.gpu_scheduler.resource_limits import docker_overrides
            limits = docker_overrides()
            if limits:
                main["cgroup_parent"] = limits["cgroup_parent"]
                if self._enable_egress_control:
                    services[self._EGRESS_CONTROL_SERVICE_NAME] = dict(limits)
        self._managed_build_path.write_text(json.dumps({"services": services}))
        return [*paths, self._managed_build_path]

    def _write_resources_compose_file(self):
        path = super()._write_resources_compose_file()
        document = json.loads(path.read_text())
        if os.environ.get("GPU_SCHEDULER_JOB_DIR"):
            from tools.gpu_scheduler.resource_limits import docker_overrides
            limits = docker_overrides(document["services"]["main"])
            if limits:
                main = document["services"]["main"]
                main.update(limits)
                resources = main.setdefault("deploy", {}).setdefault("resources", {})
                resources.setdefault("limits", {}).update(memory=str(limits["mem_limit"]), cpus=str(limits["cpus"]))
        if self.task_env_config.gpus:
            gpu = os.environ.get("CUDA_VISIBLE_DEVICES", "")
            if self.task_env_config.gpus != 1 or not re.fullmatch(r"GPU-[a-fA-F0-9-]+", gpu):
                raise ValueError("one scheduler-assigned GPU UUID is required")
            resources = document["services"]["main"].setdefault("deploy", {}).setdefault("resources", {})
            reservations = resources.setdefault("reservations", {})
            if self.gpu_attachment == "cdi":
                reservations.pop("devices", None)
                main = document["services"]["main"]
                main["devices"] = ["nvidia.com/gpu=" + gpu]
                main.setdefault("environment", {})["NVIDIA_VISIBLE_DEVICES"] = "void"
            else:
                reservations["devices"] = [
                    {"driver": "nvidia", "device_ids": [gpu], "capabilities": ["gpu"]}]
        path.write_text(json.dumps(document, indent=2))
        return path

    def _write_egress_control_services_compose_file(self):
        path = super()._write_egress_control_services_compose_file()
        if path:
            document = json.loads(path.read_text())
            document['services']['main'] = {
                'network_mode': 'service:' + self._EGRESS_CONTROL_SERVICE_NAME,
                'depends_on': {self._EGRESS_CONTROL_SERVICE_NAME: {'condition': 'service_healthy'}}}
            sidecar = document["services"].setdefault(self._EGRESS_CONTROL_SERVICE_NAME, {})
            sidecar["network_mode"] = "bridge"
            sidecar["extra_hosts"] = [f"{host}={ip}" for host, ip in sorted(self.model_host_addresses.items())]
            path.write_text(json.dumps(document, indent=2))
        return path

    async def _apply_network_policy(self, network_policy):
        if network_policy.network_mode == NetworkMode.PUBLIC:
            raise ValueError("managed Agent runtime cannot have public egress")
        if network_policy.network_mode == NetworkMode.ALLOWLIST:
            hosts = set(network_policy.allowed_hosts)
            if not hosts or not hosts.issubset(self.model_host_addresses):
                raise ValueError("only frozen model HTTPS endpoints may be allowed")
            receipt = bridge_preflight(self.network_config,
                                       repair_forwarding=self.network_config["repair_forwarding"])
            self._receipt("bridge-" + _sanitize_docker_compose_project_name(self.session_id), receipt)
            script = "printf '%s\\n' " + shlex.quote(egress_rules(self.model_host_addresses[h] for h in hosts)) + " | nft --file -"
            await self._run_docker_compose_command([
                "exec", "--no-TTY", self._EGRESS_CONTROL_SERVICE_NAME, "sh", "-c", script])
            probe = (
                "import json,socket,ssl; "
                "addresses=" + repr({h: self.model_host_addresses[h] for h in sorted(hosts)}) + "; "
                "context=ssl.create_default_context(); results=[]\n"
                "for host,address in addresses.items():\n"
                " with socket.create_connection((address,443),timeout=5) as raw:\n"
                "  with context.wrap_socket(raw,server_hostname=host) as tls:\n"
                "   results.append({'host':host,'address':address,'tls':tls.version()})\n"
                "print(json.dumps({'ok':True,'endpoints':results}))"
            )
            checked = await self.exec("python3 -B -c " + shlex.quote(probe), user="solver", timeout_sec=15)
            if checked.return_code:
                raise RuntimeError("model HTTPS preflight failed before Agent requests: " + (checked.stderr or checked.stdout or "")[-1000:])
            self._receipt("https-" + _sanitize_docker_compose_project_name(self.session_id), json.loads(checked.stdout))
        elif network_policy.network_mode == NetworkMode.NO_NETWORK and not self._trusted_verifier:
            script = "printf '%s\\n' " + shlex.quote(egress_rules([])) + " | nft --file -"
            await self._run_docker_compose_command([
                "exec", "--no-TTY", self._EGRESS_CONTROL_SERVICE_NAME, "sh", "-c", script])
        else:
            await super()._apply_network_policy(network_policy)
        self.logger.info('Managed Agent network policy applied: %s', network_policy.network_mode.value)
        self._receipt("network-" + _sanitize_docker_compose_project_name(self.session_id), {
            "session_id": self.session_id, "build_network": "default", "runtime_bridge": "bridge",
            "network_mode": network_policy.network_mode.value,
            "allowed_model_hosts": self.model_host_addresses if network_policy.network_mode == NetworkMode.ALLOWLIST else {},
            "enforcement": "Harbor sidecar nft output chain in task network namespace"})

    async def start(self, force_build):
        # Harbor's full verifier log mount contains private test evidence. It is
        # never exposed to a solver, including when completion is disabled.
        if not self._trusted_verifier:
            verifier = Path('/logs/verifier')
            self._mounts = [mount for mount in self._mounts
                            if not (Path(mount['target']) == verifier or
                                    verifier.is_relative_to(Path(mount['target'])) or
                                    Path(mount['target']).is_relative_to(verifier))]
            if not self._enable_egress_control:
                raise ValueError("Agent isolation requires Harbor egress control before startup")
        if os.environ.get("GPU_SCHEDULER_JOB_DIR"):
            from tools.gpu_scheduler.container_ownership import register_project
            register_project(_sanitize_docker_compose_project_name(self.session_id),
                             self.network_config['docker_host'])
        result = await super().start(force_build)
        if self.gpu_memory_policy:
            import subprocess
            from .host_gpu_memory import HostGpuMemoryGuard
            inspected = await self._run_docker_compose_command(['ps', '-q', 'main'])
            container = inspected.stdout.strip()
            receipt = subprocess.run(['docker', '--host', self.network_config['docker_host'],
                                      'inspect', container], capture_output=True, text=True, check=True, timeout=15)
            info = json.loads(receipt.stdout)[0]
            self._gpu_memory_guard = HostGpuMemoryGuard(
                info['State']['Pid'], os.environ['CUDA_VISIBLE_DEVICES'],
                self._gpu_guard_dir, self.gpu_memory_policy)
            self._gpu_memory_guard.start()
            self._receipt('gpu-memory-' + _sanitize_docker_compose_project_name(self.session_id),
                          {'container_id': container, 'guard_dir': str(self._gpu_guard_dir),
                           **self._gpu_memory_guard.state})
        if self._trusted_verifier:
            secured = await self.exec("chown root:root /logs/verifier && chmod 700 /logs/verifier", user="root")
            if secured.return_code:
                raise RuntimeError("could not secure trusted verifier output at startup")
        else:
            # An unchanged Harbor phase policy skips set_network_policy().
            await self._apply_network_policy(self.network_policy)
        if os.environ.get("GPU_SCHEDULER_JOB_DIR"):
            from tools.gpu_scheduler.container_ownership import register_container
            inspected = await self._run_docker_compose_command(['ps', '-q', 'main'])
            containers = inspected.stdout.strip().splitlines()
            if len(containers) != 1:
                raise ValueError("managed provider requires one exact main container")
            register_container(containers[0], self.network_config['docker_host'],
                               _sanitize_docker_compose_project_name(self.session_id))
        if self.completion_contract and not self._trusted_verifier:
            from ..core.completion import attest_docker_isolation
            inspected = await self._run_docker_compose_command(['ps', '-q', 'main'])
            containers = inspected.stdout.strip().splitlines()
            attest_docker_isolation(self.completion_contract, containers,
                                   docker_host=self.network_config['docker_host'],
                                   public_image_digest=self.public_image_digest)
        if not self._trusted_verifier and self.model_host_addresses:
            hosts = "\n".join(f"{ip} {host}" for host, ip in sorted(self.model_host_addresses.items())) + "\n"
            saved = await self.exec("printf '%s' " + shlex.quote(hosts) + " >> /etc/hosts", user="root")
            if saved.return_code:
                raise RuntimeError("could not freeze permitted model DNS")
        return result

    async def stop(self, delete):
        try:
            if self._gpu_memory_guard is not None:
                self._gpu_memory_guard.close()
                self._receipt('gpu-memory-final-' + _sanitize_docker_compose_project_name(self.session_id),
                              self._gpu_memory_guard.state)
        finally:
            await super().stop(delete=delete)

    async def empty_dirs(self, dirs, *, chmod=True):
        result = await super().empty_dirs(dirs, chmod=chmod)
        if self._trusted_verifier:
            secured = await self.exec("chown root:root /logs/verifier && chmod 700 /logs/verifier", user="root")
            if secured.return_code:
                raise RuntimeError("could not secure trusted verifier output")
        return result
