import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.gpu_scheduler import resource_limits
from tools.gpu_scheduler.resource_profiles import profile_key, suggest


class ResourceLimitTests(unittest.TestCase):
    def test_local_test_process_backend_is_explicitly_unenforced(self):
        config = {"local_test": True, "execution_backend": "process"}
        spec = {"cpu_cores": 2, "memory_max_mib": 100, "memory_high_mib": 90,
                "command": ["true"]}
        result = resource_limits.prepare(config, spec, "job")
        self.assertEqual(result["backend"], "process")
        self.assertFalse(result["enforced"])
        self.assertEqual(resource_limits.command(config, spec, result), ["true"])

    def test_verify_cgroup_rejects_unbounded_cpu_or_swap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "memory.max").write_text(str(100 * resource_limits.MIB))
            (root / "memory.high").write_text(str(90 * resource_limits.MIB))
            (root / "memory.swap.max").write_text("1")
            (root / "cpu.max").write_text("max 100000")
            with self.assertRaises(resource_limits.ResourceLimitError):
                resource_limits.verify_cgroup(root, {"memory_max_mib": 100,
                                                     "memory_high_mib": 90, "cpu_cores": 2})

    def test_attest_container_requires_exact_cgroup_parent_and_memory_swap(self):
        launch = {"id": "job", "token": "x", "config": {"local_test": True},
                  "spec": {"cpu_cores": 1, "memory_max_mib": 100,
                            "memory_high_mib": 90}}
        receipt = {"backend": "systemd", "enforced": True,
                   "slice": "gpujob.slice", "scope": "gpujob.scope",
                   "cgroup": "/sys/fs/cgroup/fake"}
        with patch.object(resource_limits, "current_boundary", return_value=(launch, receipt)), \
             patch.object(resource_limits, "verify_cgroup"):
            bad = {"HostConfig": {"CgroupParent": "other.slice", "Memory": 100,
                                   "MemorySwap": 100, "NanoCpus": 1000000000,
                                   "Privileged": False, "CapAdd": []}}
            with self.assertRaises(resource_limits.ResourceLimitError):
                resource_limits.attest_container(bad, [("", "/sys/fs/cgroup/fake/x")])

    def test_anonymous_peak_is_sampled_and_total_peak_comes_from_kernel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in (("memory.stat", "anon 100\nfile 900\nshmem 20\nkernel 30\n"),
                                ("memory.peak", "1500"), ("cpu.stat", "usage_usec 90\n"),
                                ("memory.events", "oom 0\noom_kill 0\n")):
                (root / name).write_text(value)
            sampler = resource_limits.ResourceSampler({"config": {"local_test": True}},
                {"backend": "systemd", "cgroup": str(root)})
            sampler.sample(gpu=False)
            (root / "memory.stat").write_text("anon 200\nfile 600\nshmem 10\nkernel 20\n")
            sampler.sample(gpu=False)
            result = sampler.finish()
            self.assertEqual(result["ram_anon_peak_bytes"], 200)
            self.assertEqual(result["ram_file_peak_bytes"], 900)
            self.assertEqual(result["ram_total_peak_bytes"], 1500)

    def test_only_exact_harbor_egress_sidecar_may_hold_network_capabilities(self):
        launch = {"spec": {"cpu_cores": 1, "memory_max_mib": 100, "memory_high_mib": 90}}
        receipt = {"slice": "gpujob.slice", "cgroup": "/sys/fs/cgroup/fake"}
        container = {"HostConfig": {"CgroupParent": "gpujob.slice", "Memory": 100,
                                   "MemorySwap": 100, "NanoCpus": 1000000000,
                                   "Privileged": False, "CapAdd": ["NET_ADMIN", "NET_RAW"]},
                     "Config": {"Labels": {"com.docker.compose.service":
                                            "harbor-docker-egress-control-sidecar"}}}
        with patch.object(resource_limits, "current_boundary", return_value=(launch, receipt)), \
                patch.object(resource_limits, "verify_cgroup"):
            proof = resource_limits.attest_container(container, [("", "/fake/docker-test.scope")])
            self.assertTrue(proof["enforced"])
            container["HostConfig"]["CapAdd"] = ["CAP_NET_ADMIN", "CAP_NET_RAW"]
            proof = resource_limits.attest_container(container, [("", "/fake/docker-test.scope")])
            self.assertTrue(proof["enforced"])
            container["Config"]["Labels"]["com.docker.compose.service"] = "main"
            with self.assertRaises(resource_limits.ResourceLimitError):
                resource_limits.attest_container(container, [("", "/fake/docker-test.scope")])
            container["Config"]["Labels"]["com.docker.compose.service"] = "harbor-docker-egress-control-sidecar"
            container["HostConfig"]["CapAdd"].append("SYS_ADMIN")
            with self.assertRaises(resource_limits.ResourceLimitError):
                resource_limits.attest_container(container, [("", "/fake/docker-test.scope")])

    def test_docker_limits_preserve_stricter_task_contract(self):
        launch = {"spec": {"memory_max_mib": 1024, "cpu_cores": 2}}
        receipt = {"slice": "owned.slice"}
        with patch.object(resource_limits, "current_boundary", return_value=(launch, receipt)):
            result = resource_limits.docker_overrides({"deploy": {"resources": {"limits": {
                "memory": "512m", "cpus": "0.5"}}}})
        self.assertEqual(result["mem_limit"], 512 * 1024**2)
        self.assertEqual(result["memswap_limit"], result["mem_limit"])
        self.assertEqual(result["cpus"], .5)


class ResourceProfileTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"owner": "alice", "job_class": "train", "command": ["python", "train.py"],
                     "cpu_cores": 2, "ram_mib": 1024, "memory_mib": 500, "compute_units": 20}

    def test_cold_start_keeps_declared_request(self):
        result = suggest(self.spec, [])
        self.assertEqual(result["sample_jobs"], 0)
        self.assertEqual(result["ram_mib"], 1024)
        self.assertEqual(result["memory_mib"], 500)
        self.assertFalse(result["applied"])

    def test_profile_uses_total_peak_as_separate_safety_bound(self):
        usage = {"sample_count": 2, "gpu_sample_count": 1,
                 "ram_anon_peak_bytes": 100 * 1024**2,
                 "ram_shmem_peak_bytes": 0, "ram_kernel_peak_bytes": 0,
                 "ram_total_peak_bytes": 900 * 1024**2,
                 "gpu_memory_peak_mib": 100}
        job = {"state": "SUCCEEDED", "spec": self.spec,
               "exit": {"cleanup_ok": True, "resource_usage": usage}}
        result = suggest(self.spec, [job], min_samples=1)
        self.assertGreater(result["memory_max_mib"], result["ram_mib"])
        self.assertIn("file cache remains charged", result["cache_policy"])
        self.assertFalse(result["applied"])

    def test_different_command_does_not_cross_contaminate_profile(self):
        other = {"state": "SUCCEEDED", "spec": {**self.spec, "job_class": "other-train", "command": ["python", "other.py"]},
                 "exit": {"cleanup_ok": True, "resource_usage": {"sample_count": 9,
                 "ram_anon_peak_bytes": 1, "ram_total_peak_bytes": 1, "gpu_sample_count": 1,
                 "gpu_memory_peak_mib": 1}}}
        result = suggest(self.spec, [other])
        self.assertEqual(result["sample_jobs"], 0)
        self.assertNotEqual(profile_key(self.spec), profile_key(other["spec"]))

    def test_insufficient_history_keeps_declaration(self):
        job = {"state": "SUCCEEDED", "spec": self.spec,
               "exit": {"cleanup_ok": True, "resource_usage": {"sample_count": 1,
               "ram_anon_peak_bytes": 1, "ram_total_peak_bytes": 1}}}
        result = suggest(self.spec, [job])
        self.assertEqual(result["sample_jobs"], 1)
        self.assertEqual(result["ram_mib"], self.spec["ram_mib"])

    def test_generic_profiles_require_exact_commands(self):
        spec = {**self.spec, "job_class": "generic"}
        self.assertNotEqual(profile_key(spec), profile_key({**spec, "command": ["python", "other.py"]}))

    def test_named_class_profile_is_a_declared_workload_contract(self):
        spec = {**self.spec, "command": ["python", "train.py", "--seed", "7"]}
        self.assertEqual(profile_key(self.spec), profile_key(spec))
        self.assertNotEqual(profile_key(self.spec), profile_key({**spec, "job_class": "train-v2"}))

    def test_failed_and_oom_history_cannot_shrink_request(self):
        usage = {"sample_count": 1, "ram_anon_peak_bytes": 1, "ram_total_peak_bytes": 1,
                 "memory_events": {"oom": 1}}
        job = {"state": "SUCCEEDED", "spec": self.spec,
               "exit": {"cleanup_ok": True, "resource_usage": usage}}
        self.assertEqual(suggest(self.spec, [job], min_samples=1)["sample_jobs"], 0)


if __name__ == "__main__":
    unittest.main()
