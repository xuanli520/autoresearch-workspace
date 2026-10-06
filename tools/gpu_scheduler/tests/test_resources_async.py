"""Resource-accounting and nonblocking dispatch regressions; no GPU work."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.gpu_scheduler.common import atomic_json, processes, read_json, validate_config, validate_job
from tools.gpu_scheduler.resources import choose_gpu, probe, simulated_probe
from tools.gpu_scheduler.scheduler import Scheduler


SCRATCH = Path(__file__).resolve().parents[3] / "notes/gpu-scheduler-v1/scratch"


def configuration(root: Path) -> dict:
    return validate_config({"version": 1, "root": str(root), "data_mount": None,
                            "gpus": [{"uuid": "GPU-test", "memory_mib": 100, "compute_units": 100}],
                            "cpu_cores": 8, "ram_mib": 8192, "min_free_disk_mib": 0,
                            "max_running": 2, "poll_seconds": .05}, local_test=True)


def specification(directory: Path, request: str = "job") -> dict:
    return {"request_id": request, "owner": "test", "cwd": str(directory),
            "command": [sys.executable, "-B", "-c", "pass"], "memory_mib": 30,
            "compute_units": 40, "max_runtime_seconds": 5}


class ResourceAccountingTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration(Path.cwd())
        self.spec = validate_job(specification(Path.cwd()), self.config)

    def test_newer_process_fb_memory_cannot_be_subtracted_from_older_gpu_total(self):
        snapshot = simulated_probe(self.config)
        snapshot["gpus"]["GPU-test"].update(used_mib=10,
            processes=[{"pid": 123, "memory_mib": 90}])
        active = [{"id": "active", "gpu_uuid": "GPU-test", "spec": {**self.spec, "memory_mib": 90}}]
        gpu, reason = choose_gpu(self.spec, active, self.config, snapshot, set(), pid_sets={"active": {123}})
        self.assertIsNone(gpu)
        self.assertIn("insufficient_memory", reason)

    def test_ambiguous_pid_never_discounts_two_reservations(self):
        snapshot = simulated_probe(self.config)
        snapshot["gpus"]["GPU-test"].update(used_mib=30,
            processes=[{"pid": 123, "memory_mib": 30}])
        active = [{"id": name, "gpu_uuid": "GPU-test", "spec": self.spec} for name in ("a", "b")]
        config = {**self.config, "max_running": 3}
        gpu, reason = choose_gpu(self.spec, active, config, snapshot, set(), pid_sets={"a": {123}, "b": {123}})
        self.assertIsNone(gpu)
        self.assertIn("unidentified", reason)

    def test_pid_running_on_another_gpu_is_unidentified(self):
        config = {**self.config, "gpus": [*self.config["gpus"],
                  {"uuid": "GPU-other", "memory_mib": 100, "compute_units": 100}]}
        snapshot = simulated_probe(config)
        snapshot["gpus"]["GPU-other"].update(used_mib=30,
            processes=[{"pid": 123, "memory_mib": 30}])
        active = [{"id": "a", "gpu_uuid": "GPU-test", "spec": self.spec}]
        gpu, reason = choose_gpu({**self.spec, "gpu_uuid": "GPU-other"}, active, config,
                                 snapshot, set(), pid_sets={"a": {123}})
        self.assertIsNone(gpu)
        self.assertIn("unidentified", reason)

    def test_pid_claimed_by_jobs_on_different_gpus_is_ambiguous(self):
        config = {**self.config, "max_running": 3, "gpus": [*self.config["gpus"],
                  {"uuid": "GPU-other", "memory_mib": 100, "compute_units": 100}]}
        snapshot = simulated_probe(config)
        snapshot["gpus"]["GPU-test"].update(used_mib=30,
            processes=[{"pid": 123, "memory_mib": 30}])
        active = [{"id": name, "gpu_uuid": gpu, "spec": self.spec}
                  for name, gpu in (("a", "GPU-test"), ("b", "GPU-other"))]
        gpu, reason = choose_gpu({**self.spec, "gpu_uuid": "GPU-test"}, active, config,
                                 snapshot, set(), pid_sets={"a": {123}, "b": {123}})
        self.assertIsNone(gpu)
        self.assertIn("unidentified", reason)

    def test_unknown_shared_memory_is_included_even_if_gpu_total_lags(self):
        config = {**self.config, "external_process_policy": "shared", "shared_headroom_mib": 1024,
                  "gpus": [{"uuid": "GPU-test", "memory_mib": 1200, "compute_units": 100}]}
        snapshot = simulated_probe(config)
        snapshot["gpus"]["GPU-test"].update(used_mib=10,
            processes=[{"pid": 123, "memory_mib": 90}])
        gpu, reason = choose_gpu({**self.spec, "memory_mib": 100}, [], config, snapshot, set(), pid_sets={})
        self.assertIsNone(gpu)
        self.assertIn("insufficient_memory", reason)

    def test_parser_uses_fb_memory_and_rejects_duplicate_or_negative_readings(self):
        for gpu_text, process_text in (("GPU-test, 100, -1, 50\n", ""),
                ("GPU-test, 100, 10, 50\n", "GPU-test, 123, 5\nGPU-test, 123, 5\n")):
            results = [subprocess.CompletedProcess([], 0, gpu_text),
                       subprocess.CompletedProcess([], 0, process_text)]
            with self.subTest(process_text=process_text), patch(
                    "tools.gpu_scheduler.resources.subprocess.run", side_effect=results):
                with self.assertRaises(ValueError):
                    probe()
        results = [subprocess.CompletedProcess([], 0, "GPU-test, 100, 90, 50\n"),
                   subprocess.CompletedProcess([], 0, "GPU-test, 123, 90\n")]
        with patch("tools.gpu_scheduler.resources.subprocess.run", side_effect=results) as query:
            self.assertEqual(probe()["gpus"]["GPU-test"]["processes"][0]["memory_mib"], 90)
            self.assertIn("--query-compute-apps=gpu_uuid,pid,used_gpu_memory", query.call_args.args[0])


class AsyncDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="review-", dir=SCRATCH)
        self.directory = Path(self.temporary.name)
        self.fd = os.open(os.devnull, os.O_RDONLY)
        self.scheduler = Scheduler(configuration(self.directory / "queue"), self.fd)

    async def asyncTearDown(self):
        self.scheduler.begin_shutdown()
        await self.scheduler.settle_background()
        for job in self.scheduler.active():
            if job["process"] is not None:
                await asyncio.to_thread(job["process"].wait, 5)
        await self.scheduler.reconcile_async()
        await self.scheduler.settle_background()
        os.close(self.fd)
        self.temporary.cleanup()

    async def test_slow_spawn_reserves_resources_and_cancel_prevents_candidate_start(self):
        marker = self.directory / "candidate-started"
        raw = specification(self.directory)
        raw["command"] = [sys.executable, "-B", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        first = self.scheduler.submit(raw)
        second = self.scheduler.submit(specification(self.directory, "second"))
        third = self.scheduler.submit(specification(self.directory, "third"))
        release, entered = threading.Event(), threading.Event()
        spawn = self.scheduler._spawn_worker

        def slow_spawn(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test spawn was not released")
            return spawn(*args)

        with patch.object(self.scheduler, "_spawn_worker", side_effect=slow_spawn):
            await self.scheduler.tick_async(simulated_probe(self.scheduler.config))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual(self.scheduler.status()["active_count"], 2)
            self.assertEqual(self.scheduler.get(second["id"])["state"], "STARTING")
            self.assertEqual(self.scheduler.get(third["id"])["state"], "QUEUED")
            self.assertEqual(self.scheduler.cancel(first["id"])["state"], "CANCELLING")
            release.set()
            await self.scheduler.settle_background()
        proc = self.scheduler.get(first["id"])["process"]
        await asyncio.to_thread(proc.wait, 5)
        await self.scheduler.reconcile_async()
        await self.scheduler.settle_background()
        self.assertEqual(self.scheduler.get(first["id"])["state"], "CANCELLED")
        self.assertFalse(marker.exists())

    async def test_shutdown_rechecks_executor_after_stop_receipt_failure(self):
        """A STOP write failure must not leave a late async executor running."""
        marker = self.directory / "candidate-started"
        raw = specification(self.directory)
        raw["command"] = [sys.executable, "-B", "-c",
                           f"from pathlib import Path; Path({str(marker)!r}).touch(); import time; time.sleep(30)"]
        job = self.scheduler.submit(raw)
        release, entered = threading.Event(), threading.Event()
        spawn = self.scheduler._spawn_worker

        def slow_spawn(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test spawn was not released")
            return spawn(*args)

        with patch.object(self.scheduler, "_spawn_worker", side_effect=slow_spawn), \
                patch("tools.gpu_scheduler.scheduler.atomic_json") as write:
            def fail_stop(path, value):
                if Path(path).name == "STOP.json":
                    raise OSError("simulated stop receipt failure")
                return atomic_json(path, value)
            write.side_effect = fail_stop
            await self.scheduler.tick_async(simulated_probe(self.scheduler.config))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.scheduler.begin_shutdown("test_stop")
            self.assertTrue(self.scheduler.jobs[job["id"]].get("stop_without_receipt"))
            release.set()
            await self.scheduler.settle_background()

        process = self.scheduler.jobs[job["id"]]["process"]
        await asyncio.to_thread(process.wait, 5)
        await self.scheduler.reconcile_async()
        await self.scheduler.settle_background()
        self.assertIsNotNone(process.poll())
        self.assertFalse(processes.scope_members(self.scheduler.jobs[job["id"]]["token"]))
        self.assertFalse(any(item["process"] is not None and item["process"].poll() is None
                             for item in self.scheduler.active()))

    async def test_service_stop_cleans_late_executor_and_continues_after_write_failure(self):
        await self.check_service_stop_receipt_failure(use_signal=False)

    async def test_service_sigterm_cleans_late_executor_after_write_failure(self):
        await self.check_service_stop_receipt_failure(use_signal=True)

    async def check_service_stop_receipt_failure(self, *, use_signal):
        from tools.gpu_scheduler.server import serve

        self.scheduler.root.chmod(0o700)
        marker = self.directory / "late-candidate"
        raw = specification(self.directory)
        raw.update(max_runtime_seconds=30, command=[sys.executable, "-B", "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch(); import time; time.sleep(30)"])
        first = self.scheduler.get(self.scheduler.submit(raw)["id"])
        second = self.scheduler.get(self.scheduler.submit({**raw, "request_id": "second"})["id"])
        third = self.scheduler.get(self.scheduler.submit({**raw, "request_id": "queued"})["id"])
        release, entered = threading.Event(), threading.Event()
        spawn = self.scheduler._spawn_worker

        def slow_spawn(directory, *args):
            process = spawn(directory, *args)
            if directory.name == first["id"]:
                # Leave a real candidate running while its Popen handoff is pending.
                deadline = time.monotonic() + 3
                while (not (directory / "started.json").exists() or not marker.exists()) and time.monotonic() < deadline:
                    time.sleep(.01)
                entered.set()
                if not release.wait(3):
                    process.terminate()
                    process.wait(5)
                    raise RuntimeError("test spawn was not released")
            return process

        def fail_stop(path, value):
            if Path(path) == Path(first["directory"]) / "STOP.json":
                raise OSError("simulated stop receipt failure")
            return atomic_json(path, value)

        service = None
        try:
            with patch("tools.gpu_scheduler.server.validate_config", return_value=self.scheduler.config), \
                    patch("tools.gpu_scheduler.server.Scheduler", return_value=self.scheduler), \
                    patch.object(self.scheduler, "_spawn_worker", side_effect=slow_spawn), \
                    patch("tools.gpu_scheduler.scheduler.atomic_json", side_effect=fail_stop), \
                    redirect_stdout(StringIO()):
                service = asyncio.create_task(serve({}, local_test=True))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertTrue(marker.exists())
                self.assertIsNone(first["process"])
                if use_signal:
                    os.kill(os.getpid(), signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while not self.scheduler.closing and time.monotonic() < deadline:
                        await asyncio.sleep(.01)
                    self.assertTrue(self.scheduler.closing)
                else:
                    reader, writer = await asyncio.open_unix_connection(str(self.scheduler.root / "scheduler.sock"))
                    try:
                        writer.write(json.dumps({"op": "stop", "session_id": self.scheduler.id}).encode() + b"\n")
                        await writer.drain()
                        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
                        self.assertTrue(reply["ok"], reply)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                release.set()
                await asyncio.wait_for(asyncio.shield(service), 8)
            self.assertEqual(third["state"], "CANCELLED")
            for job in (first, second):
                self.assertIsNotNone(job["process"].poll())
                self.assertEqual(job["state"], "CANCELLED")
                self.assertTrue(job["exit"]["cleanup_ok"])
                self.assertFalse(processes.scope_members(job["token"]))
            receipt = read_json(self.scheduler.directory / "service-exit.json")
            self.assertTrue(receipt["cleanup_confirmed"])
            self.assertEqual(receipt["active_count"], 0)
            self.assertFalse((self.scheduler.root / "scheduler.sock").exists())
        finally:
            release.set()
            if service is not None and not service.done():
                await asyncio.wait_for(service, 8)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                asyncio.get_running_loop().remove_signal_handler(sig)

    async def test_slow_recovery_keeps_reservation_without_blocking_another_dispatch(self):
        first = self.scheduler.get(self.scheduler.submit(specification(self.directory))["id"])
        first.update(state="RUNNING", gpu_uuid="GPU-test", process=Mock(poll=Mock(return_value=1)),
                     execution_deadline_mono=time.monotonic() + 5)
        second = self.scheduler.submit(specification(self.directory, "second"))
        release, entered = threading.Event(), threading.Event()

        def slow_recovery(job):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test cleanup was not released")
            return {"reason": "executor_lost", "returncode": None, "cleanup_ok": True}, False

        with patch.object(self.scheduler, "_recover_and_inspect", side_effect=slow_recovery):
            await self.scheduler.tick_async(simulated_probe(self.scheduler.config))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual(first["state"], "RUNNING")
            self.assertEqual(self.scheduler.get(second["id"])["state"], "STARTING")
            self.assertEqual(self.scheduler.cancel(second["id"])["state"], "CANCELLING")
            release.set()
            await self.scheduler.settle_background()
        self.assertEqual(first["state"], "FAILED")

    async def test_probe_failure_retains_diagnostic_for_queued_job(self):
        submitted = self.scheduler.submit(specification(self.directory))
        await self.scheduler.tick_async(None, "CalledProcessError: nvidia-smi exited 9")
        job = self.scheduler.view(self.scheduler.get(submitted["id"]))
        self.assertEqual(job["state"], "QUEUED")
        self.assertIn("nvidia-smi exited 9", job["telemetry_error"])
        self.assertEqual(read_json(Path(job["directory"]) / "status.json")["telemetry_error"], job["telemetry_error"])

    async def test_slow_spawn_cannot_reset_the_original_execution_deadline(self):
        marker = self.directory / "late-candidate-started"
        raw = specification(self.directory)
        raw.update(max_runtime_seconds=.1, command=[sys.executable, "-B", "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()"])
        submitted = self.scheduler.submit(raw)
        job = self.scheduler.get(submitted["id"])
        release, entered = threading.Event(), threading.Event()
        spawn = self.scheduler._spawn_worker

        def slow_spawn(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test spawn was not released")
            return spawn(*args)

        with patch.object(self.scheduler, "_spawn_worker", side_effect=slow_spawn):
            await self.scheduler.tick_async(simulated_probe(self.scheduler.config))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            original = read_json(Path(job["directory"]) / "launch.json")
            await asyncio.sleep(.15)
            release.set()
            await self.scheduler.settle_background()
        await asyncio.to_thread(job["process"].wait, 5)
        await self.scheduler.reconcile_async()
        await self.scheduler.settle_background()
        self.assertEqual(job["state"], "TIMED_OUT")
        self.assertEqual(read_json(Path(job["directory"]) / "launch.json"), original)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
