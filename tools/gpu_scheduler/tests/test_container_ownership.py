"""Admission regressions for trusted container attribution and unknown GPU PIDs."""
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.gpu_scheduler import container_ownership as ownership
from tools.gpu_scheduler.resources import choose_gpu


CID = "a" * 64
TOKEN = "b" * 32
GPU = "GPU-test"


class ContainerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.receipts = self.directory / "containers"
        self.receipts.mkdir(mode=0o700)
        self.job = {"id": "job", "token": TOKEN, "directory": str(self.directory),
                    "gpu_uuid": GPU, "spec": {"cpu_cores": 1, "ram_mib": 1024,
                    "compute_units": 25, "memory_mib": 2048}}
        self.row = {"version": 1, "job_id": "job", "token": TOKEN, "container_id": CID,
                    "init_pid": 100, "init_start_ticks": 10, "boot_id": "boot"}
        self.write_receipt()
        self.config = {"max_running": 8, "cpu_cores": 8, "ram_mib": 14000,
                       "poll_seconds": 1, "gpus": [{"uuid": GPU, "memory_mib": 30000,
                       "compute_units": 100}], "external_process_policy": "exclusive_admission"}
        self.snapshot = {"at": time.time(), "gpus": {GPU: {"total_mib": 32607,
                          "used_mib": 1628, "utilization": 10,
                          "processes": [{"pid": 200, "memory_mib": 1628}]}}}
        self.groups = {100: [("", f"/system.slice/docker-{CID}.scope")],
                       200: [("", f"/system.slice/docker-{CID}.scope/child")],
                       300: [("", "/system.slice/docker-" + "c" * 64 + ".scope")]}
        self.ticks = {100: 10, 200: 20, 300: 30}
        for target, replacement in (
                ("boot_id", lambda: "boot"),
                ("process_start_ticks", lambda pid: self.ticks.get(pid)),
                ("pid_matches", lambda pid, ticks, boot=None:
                 boot in (None, "boot") and ticks is not None and self.ticks.get(pid) == ticks),
                ("scope_members", lambda token: [])):
            mock = patch.object(ownership.processes, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(ownership, "_cgroups", lambda pid: self.groups.get(pid, []))
        mock.start()
        self.addCleanup(mock.stop)

    def write_receipt(self):
        path = self.receipts / f"{CID}.json"
        path.write_text(json.dumps(self.row))
        path.chmod(0o600)

    def admission(self):
        return choose_gpu(self.job["spec"], [self.job], self.config, self.snapshot, set())

    def assert_external(self):
        gpu, reason = self.admission()
        self.assertIsNone(gpu)
        self.assertIn("external_or_unidentified_process", reason)

    def test_container_gpu_admitted_without_host_token(self):
        self.assertEqual(self.admission(), (GPU, "ready"))

    def test_unknown_container_still_blocks(self):
        self.snapshot["gpus"][GPU]["processes"].append({"pid": 300, "memory_mib": 10})
        self.assert_external()

    def test_container_init_pid_reuse_rejects_old_receipt(self):
        self.ticks[100] = 99
        self.assert_external()

    def test_old_boot_and_wrong_job_token_rejected(self):
        for key, value in (("boot_id", "old-boot"), ("token", "x" * 32), ("job_id", "other")):
            original = self.row[key]
            self.row[key] = value
            self.write_receipt()
            self.assert_external()
            self.row[key] = original

    def test_common_host_cgroup_cannot_claim_container(self):
        self.groups[100] = self.groups[200] = [("", "/system.slice")]
        self.assert_external()

    def test_writable_and_symlink_receipts_rejected(self):
        path = self.receipts / f"{CID}.json"
        path.chmod(0o666)
        self.assert_external()
        path.unlink()
        source = self.directory / "outside.json"
        source.write_text(json.dumps(self.row))
        source.chmod(0o600)
        path.symlink_to(source)
        self.assert_external()

    def test_ram_limits_and_unconsumed_gpu_reservation_unchanged(self):
        self.config["ram_mib"] = 1500
        self.assertEqual(self.admission(), (None, "insufficient_ram_mib"))
        self.config["ram_mib"] = 14000
        self.config["gpus"][0]["memory_mib"] = 4095
        self.assertEqual(self.admission(), (None, f"{GPU}:insufficient_memory_reservation"))

    def test_ambiguous_container_cannot_belong_to_two_jobs(self):
        other = self.directory / "other"
        (other / "containers").mkdir(parents=True, mode=0o700)
        other.chmod(0o700)
        row = {**self.row, "job_id": "other", "token": "d" * 32}
        target = other / "containers" / f"{CID}.json"
        target.write_text(json.dumps(row))
        target.chmod(0o600)
        jobs = [self.job, {**self.job, "id": "other", "token": row["token"], "directory": str(other)}]
        self.assertEqual(ownership.container_process_map(jobs, {200}), {"job": set(), "other": set()})

    def test_gpu_pid_reuse_during_cgroup_read_is_rejected(self):
        def changed(pid):
            if pid == 200:
                self.ticks[200] = 99
            return self.groups.get(pid, [])
        with patch.object(ownership, "_cgroups", changed):
            self.assert_external()

    def test_registration_uses_exact_docker_identity_and_private_receipt(self):
        endpoint = self.directory / "docker.sock"
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(endpoint))
        launch = {"id": "job", "token": TOKEN, "config": {"local_test": True}}
        (self.directory / "launch.json").write_text(json.dumps(launch))
        (self.directory / "launch.json").chmod(0o600)
        env = {"GPU_SCHEDULER_JOB_DIR": str(self.directory), "GPU_SCHEDULER_JOB_ID": "job",
               "AUTORESEARCH_PROCESS_TOKEN": TOKEN}
        inspected = [{"Id": CID, "State": {"Pid": 100, "Running": True},
                      "Config": {"Labels": {"com.docker.compose.project": "owned"}}}]
        with patch.dict(os.environ, env), patch.object(ownership.subprocess, "run") as run:
            run.return_value.stdout = json.dumps(inspected)
            result = ownership.register_container(CID[:12], "unix://" + str(endpoint), "owned")
            self.assertTrue(result["registered"])
            self.assertNotIn("token", result)
            record = self.receipts / f"{CID}.json"
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.admission(), (GPU, "ready"))
            with self.assertRaisesRegex(ValueError, "provider project"):
                ownership.register_container(CID, "unix://" + str(endpoint), "other")


if __name__ == "__main__":
    unittest.main()
