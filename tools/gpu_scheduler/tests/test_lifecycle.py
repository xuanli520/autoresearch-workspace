import json
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.gpu_scheduler.common import validate_config, validate_job
from tools.gpu_scheduler.scheduler import Scheduler
from tools.gpu_scheduler import lifecycle
from tools.gpu_scheduler import managed_service


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = {"version": 1, "root": self.tmp.name, "data_mount": "/unused",
                    "gpus": [{"uuid": "GPU-test", "memory_mib": 8000, "compute_units": 100}],
                    "cpu_cores": 2, "ram_mib": 8000}

    def test_long_service_needs_explicit_authorization(self):
        with self.assertRaises(ValueError):
            validate_config(dict(self.raw, service_seconds=172800), local_test=True)
        lease = {"authorization": "user explicitly renewed shared infrastructure for 48 hours",
                 "deadline_epoch": time.time() + 172800}
        cfg = validate_config(dict(self.raw, service_seconds=172800, infrastructure_lease=lease), local_test=True)
        with socket.socket(socket.AF_UNIX) as lock:
            scheduler = Scheduler(cfg, lock.fileno())
        self.assertAlmostEqual(scheduler.deadline_epoch, lease["deadline_epoch"], delta=.01)

    def test_infrastructure_does_not_extend_job_budget(self):
        cfg = validate_config(self.raw, local_test=True)
        spec = {"request_id": "test", "owner": "test", "command": ["/usr/bin/true"],
                "cwd": self.tmp.name, "memory_mib": 100, "compute_units": 1,
                "max_runtime_seconds": 43201}
        with self.assertRaises(ValueError):
            validate_job(spec, cfg)

    def test_persistent_service_has_no_service_deadline(self):
        raw = dict(self.raw, persistent=True)
        cfg = validate_config(raw, local_test=True)
        self.assertIsNone(cfg["service_seconds"])
        with socket.socket(socket.AF_UNIX) as lock:
            scheduler = Scheduler(cfg, lock.fileno())
        self.assertIsNone(scheduler.deadline_epoch)
        self.assertIsNone(scheduler.deadline_mono)
        self.assertTrue(scheduler.status()["persistent"])

    def test_persistent_service_rejects_lease(self):
        with self.assertRaises(ValueError):
            validate_config(dict(self.raw, persistent=True,
                                 infrastructure_lease={"authorization": "x", "deadline_epoch": time.time()+100}),
                            local_test=True)
        with self.assertRaisesRegex(ValueError, "omit service_seconds"):
            validate_config(dict(self.raw, persistent=True, service_seconds=1), local_test=True)

    def test_persistent_units_only_manage_gpu_service(self):
        plan = {"root": self.tmp.name, "unit_name": "test-gpu-persistent", "python": "/usr/bin/python3"}
        config = {"root": self.tmp.name, "data_mount": "/mnt/data"}
        units = managed_service.unit_files(plan, config, Path(self.tmp.name) / "plan.json")
        self.assertEqual(set(units), {"test-gpu-persistent.service"})
        unit = next(iter(units.values()))
        self.assertIn("Restart=always", unit)
        self.assertIn("KillMode=process", unit)
        self.assertIn("RequiresMountsFor=/mnt/data", unit)
        self.assertNotIn("docker", unit)
        self.assertNotIn("OnCalendar", unit)

    def test_persistent_service_keeps_job_runtime_limit(self):
        cfg = validate_config(dict(self.raw, persistent=True), local_test=True)
        spec = {"request_id": "test", "owner": "test", "command": ["/usr/bin/true"],
                "cwd": self.tmp.name, "memory_mib": 100, "compute_units": 1,
                "max_runtime_seconds": 43201}
        with self.assertRaises(ValueError):
            validate_job(spec, cfg)

    def test_crashed_service_recovery_preserves_missing_exit_and_never_replays(self):
        root = Path(self.tmp.name)
        sid = "a" * 32
        directory = root / "sessions" / sid
        directory.mkdir(parents=True)
        (root / "service.json").write_text(json.dumps({"session_id": sid}))
        (directory / "service.json").write_text(json.dumps({"identity": {
            "pid": 99999999, "start_ticks": 1, "boot_id": managed_service.processes.boot_id()}}))
        result = managed_service.previous_session({"root": str(root)})
        self.assertTrue(result["executor_cleanup_verified"])
        self.assertFalse(result["jobs_replayed"])
        self.assertFalse((directory / "service-exit.json").exists())
        job = directory / "jobs" / "b"
        job.mkdir(parents=True)
        (job / "launch.json").write_text(json.dumps({"token": "b" * 32}))
        with self.assertRaisesRegex(RuntimeError, "executor has not finished"):
            managed_service.previous_session({"root": str(root)})
        (job / "exit.json").write_text(json.dumps({"cleanup_ok": False}))
        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            managed_service.previous_session({"root": str(root)})

    def test_reboot_allows_empty_new_session_without_fabricating_cleanup(self):
        root = Path(self.tmp.name)
        sid = "c" * 32
        directory = root / "sessions" / sid
        directory.mkdir(parents=True)
        (root / "service.json").write_text(json.dumps({"session_id": sid}))
        (directory / "service.json").write_text(json.dumps({"identity": {"boot_id": "previous-boot"}}))
        result = managed_service.previous_session({"root": str(root)})
        self.assertTrue(result["previous_boot_ended"])
        self.assertFalse(result["cleanup_confirmed"])

    def test_timer_failure_never_detaches_runtime(self):
        config = {"root": self.tmp.name, "original_deadline_epoch": time.time() + 100,
                  "renewed_deadline_epoch": time.time() + 1000, "gpu_root": self.tmp.name,
                  "gpu_launch": "/unused"}
        with patch.object(lifecycle.os, "geteuid", return_value=0), \
             patch.object(lifecycle, "load", return_value=(config, {})), \
             patch.object(lifecycle, "read", return_value={
                 "child_survived_original_deadline": True, "control_deadline_verified": True,
                 "boot_id": lifecycle.processes.boot_id()}), \
             patch.object(lifecycle, "runtime_identities", return_value=[]), \
             patch.object(lifecycle, "alive", return_value=True), \
             patch.object(lifecycle, "digest", return_value="hash"), \
             patch.object(lifecycle, "schedule", side_effect=RuntimeError("timer failed")), \
             patch.object(lifecycle.processes, "signal_identity") as signal:
            with self.assertRaises(RuntimeError):
                lifecycle.install(Path(self.tmp.name) / "config.json")
            signal.assert_not_called()


class TimeoutProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.detached = {"pid": 101, "start_ticks": 1, "boot_id": "boot"}
        self.control_child = {"pid": 201, "start_ticks": 2, "boot_id": "boot"}
        self.wrapper = Mock(pid=100, returncode=-signal.SIGKILL)
        self.control = Mock(pid=200, returncode=124)
        self.wrapper.poll.return_value = self.wrapper.returncode
        self.control.poll.return_value = self.control.returncode

    def probe(self, *, elapsed=2.1, control_alive=False, detached_alive=True):
        with patch.object(lifecycle.os, "geteuid", return_value=0), \
             patch.object(lifecycle.subprocess, "Popen", side_effect=[self.wrapper, self.control]), \
             patch.object(lifecycle, "_probe_child", side_effect=[self.detached, self.control_child]), \
             patch.object(lifecycle.processes, "boot_id", return_value="boot"), \
             patch.object(lifecycle.processes, "process_start_ticks", return_value=1), \
             patch.object(lifecycle.processes, "signal_identity", return_value=True) as sent, \
             patch.object(lifecycle, "alive", side_effect=lambda item:
                          control_alive if item == self.control_child else detached_alive), \
             patch.object(lifecycle.time, "monotonic", side_effect=[100, 100 + elapsed]):
            result = lifecycle.probe(self.tmp.name)
        return result, sent

    def test_paired_control_proves_real_deadline_before_success(self):
        result, sent = self.probe()
        self.assertTrue(result["control_deadline_verified"])
        self.assertTrue(result["child_survived_original_deadline"])
        self.control.wait.assert_called_once_with(timeout=lifecycle.PROBE_WAIT_SECONDS)
        self.assertEqual(sent.call_args_list[-2].args, (101, 1, signal.SIGKILL, "boot"))
        self.assertEqual(sent.call_args_list[-1].args, (201, 2, signal.SIGKILL, "boot"))
        self.assertEqual(json.loads((Path(self.tmp.name) / "timeout-probe.json").read_text()), result)

    def test_early_control_exit_is_not_detachment_proof(self):
        with self.assertRaisesRegex(RuntimeError, "control did not enforce"):
            self.probe(elapsed=1)
        receipt = json.loads((Path(self.tmp.name) / "timeout-probe.json").read_text())
        self.assertFalse(receipt["control_deadline_verified"])
        self.assertIn("control did not enforce", receipt["error"])

    def test_live_control_child_is_not_detachment_proof(self):
        with self.assertRaisesRegex(RuntimeError, "control did not enforce"):
            self.probe(control_alive=True)

    def test_control_must_exit_from_deadline(self):
        self.control.returncode = 0
        with self.assertRaisesRegex(RuntimeError, "control did not enforce"):
            self.probe()

    def test_supervisor_must_exit_from_detachment_signal(self):
        self.wrapper.returncode = 124
        with self.assertRaisesRegex(RuntimeError, "detachment signal"):
            self.probe()
        self.control.wait.assert_not_called()

    def test_control_wait_timeout_keeps_failure_receipt(self):
        self.control.wait.side_effect = subprocess.TimeoutExpired(['timeout'], lifecycle.PROBE_WAIT_SECONDS)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.probe()
        receipt = json.loads((Path(self.tmp.name) / "timeout-probe.json").read_text())
        self.assertFalse(receipt["control_deadline_verified"])
        self.assertIn('TimeoutExpired', receipt['error'])
        self.assertEqual(receipt['daemon'], self.detached)
        self.assertEqual(receipt['control_child'], self.control_child)

    def test_detached_child_death_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be detached"):
            self.probe(detached_alive=False)
        receipt = json.loads((Path(self.tmp.name) / "timeout-probe.json").read_text())
        self.assertTrue(receipt["control_deadline_verified"])
        self.assertFalse(receipt["child_survived_original_deadline"])

    def test_missing_child_fails_when_wrapper_exits(self):
        with patch.object(lifecycle, "child", side_effect=FileNotFoundError), \
             patch.object(lifecycle.time, "monotonic", return_value=100), \
             patch.object(lifecycle.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "child did not start"):
                lifecycle._probe_child(self.wrapper)
        sleep.assert_not_called()

    def test_install_rejects_old_or_other_boot_probe(self):
        config = {"root": self.tmp.name, "original_deadline_epoch": time.time() + 100}
        receipts = ({"child_survived_original_deadline": True},
                    {"child_survived_original_deadline": True, "control_deadline_verified": True,
                     "boot_id": "previous-boot"})
        for receipt in receipts:
            with self.subTest(receipt=receipt), \
                 patch.object(lifecycle.os, "geteuid", return_value=0), \
                 patch.object(lifecycle, "load", return_value=(config, {})), \
                 patch.object(lifecycle, "read", return_value=receipt), \
                 patch.object(lifecycle.processes, "boot_id", return_value="boot"), \
                 patch.object(lifecycle, "schedule") as schedule:
                with self.assertRaisesRegex(ValueError, "required timeout detachment probe"):
                    lifecycle.install(Path(self.tmp.name) / "config.json")
                schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
