import asyncio
import json
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.gpu_scheduler.common import atomic_json, validate_config
from tools.gpu_scheduler.journal import Journal
from tools.gpu_scheduler.resources import simulated_probe
from tools.gpu_scheduler.scheduler import Scheduler


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = validate_config({"version": 1, "root": str(self.root), "data_mount": None,
            "gpus": [{"uuid": "GPU-a", "memory_mib": 100, "compute_units": 100}],
            "cpu_cores": 16, "ram_mib": 30000, "persistent": True, "min_free_disk_mib": 0,
            "max_running": 1, "scheduling_policy": "fair_share"}, local_test=True)

    def spec(self, name, **extra):
        return {"request_id": name, "owner": "owner", "command": ["/usr/bin/true"],
                "cwd": str(self.root), "memory_mib": 50, "compute_units": 50,
                "max_runtime_seconds": 30, "deadline_epoch": time.time() + 1000, **extra}

    def test_restart_retains_intent_position_bypass_deadline_and_identity(self):
        scheduler = Scheduler(self.cfg, -1)
        specification = self.spec("queued", queue_timeout_seconds=.1)
        old = scheduler.get(scheduler.submit(specification)["id"])
        old["bypasses"] = 2
        scheduler._persist(old)
        restored = Scheduler(self.cfg, -1)
        job = restored.get(request_id="queued")
        self.assertEqual(job["id"], old["id"])
        self.assertEqual(job["submitted_at"], old["submitted_at"])
        self.assertEqual(job["deadline_epoch"], old["deadline_epoch"])
        self.assertEqual(job["bypasses"], 2)
        self.assertEqual(restored.submit(specification)["id"], old["id"])
        with self.assertRaisesRegex(ValueError, "different spec"):
            restored.submit({**specification, "memory_mib": 40})

    def test_restart_never_relaunches_execution_and_reconciles_real_exit(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.get(scheduler.submit(self.spec("started"))["id"])
        with patch.object(scheduler, "_spawn_worker", return_value=None):
            scheduler.launch(job, "GPU-a")
        restored = Scheduler(self.cfg, -1)
        job = restored.get(request_id="started")
        self.assertEqual(job["state"], "UNKNOWN")
        self.assertTrue(job["reconciling"])
        with patch.object(restored, "launch") as launch:
            restored.tick(simulated_probe(self.cfg))
            launch.assert_not_called()
        atomic_json(Path(job["directory"]) / "exit.json", {
            "reason": "process_exit", "returncode": 0, "cleanup_ok": True})
        with patch("tools.gpu_scheduler.scheduler.processes.scope_members", return_value=[]):
            restored.reconcile()
        self.assertEqual(job["state"], "SUCCEEDED")

    def test_launch_receipt_on_queued_journal_never_replays_execution(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.get(scheduler.submit(self.spec("handoff-gap"))["id"])
        directory = Path(job["directory"])
        atomic_json(directory / "launch.json", {"id": job["id"], "token": job["token"],
            "gpu_uuid": "GPU-a", "deadline_epoch": time.time() + 60})
        restored = Scheduler(self.cfg, -1)
        recovered = restored.get(job["id"])
        self.assertEqual(recovered["state"], "UNKNOWN")
        self.assertEqual(recovered["gpu_uuid"], "GPU-a")
        with patch.object(restored, "launch") as launch:
            restored.tick(simulated_probe(self.cfg))
            launch.assert_not_called()

    def test_starting_is_durable_before_launch_receipt_write(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.get(scheduler.submit(self.spec("durable-starting"))["id"])
        def interrupted_write(path, value):
            if Path(path).name == "launch.json":
                raise OSError("crash before launch receipt")
            return atomic_json(path, value)
        with patch("tools.gpu_scheduler.scheduler.atomic_json", side_effect=interrupted_write):
            with self.assertRaises(OSError):
                scheduler.launch(job, "GPU-a")
        recovered = Scheduler(self.cfg, -1).get(job["id"])
        self.assertEqual(recovered["state"], "UNKNOWN")
        self.assertEqual(recovered["reason"], "missing_execution_evidence")

    def test_restarted_scheduler_reserves_delayed_popen_and_never_replays(self):
        async def verify():
            scheduler = Scheduler(self.cfg, -1)
            marker = self.root / "execution-count"
            raw = self.spec("late-spawn", max_runtime_seconds=5,
                command=[sys.executable, "-B", "-c", "from pathlib import Path; import time; "
                         f"Path({str(marker)!r}).open('a').write('started\\n'); time.sleep(.15)"])
            first = scheduler.submit(raw)
            queued = scheduler.submit(self.spec("retained"))
            released, entered = threading.Event(), threading.Event()
            spawn = scheduler._spawn_worker
            def late_spawn(*args):
                entered.set()
                if not released.wait(3):
                    raise OSError("test handoff not released")
                return spawn(*args)
            with patch.object(scheduler, "_spawn_worker", side_effect=late_spawn):
                await scheduler.tick_async(simulated_probe(self.cfg))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                restored = Scheduler(self.cfg, -1)
                with patch.object(restored, "launch") as launch:
                    await restored.tick_async(simulated_probe(self.cfg))
                    launch.assert_not_called()
                self.assertEqual(restored.get(first["id"])["state"], "UNKNOWN")
                self.assertEqual(restored.get(queued["id"])["state"], "QUEUED")
                released.set()
                await scheduler.settle_background()
            await asyncio.to_thread(scheduler.get(first["id"])["process"].wait, 5)
            await restored.reconcile_async()
            self.assertEqual(restored.get(first["id"])["state"], "SUCCEEDED")
            self.assertEqual(marker.read_text().splitlines(), ["started"])
        asyncio.run(verify())

    def test_duplicate_worker_launch_cannot_execute_or_replace_original_exit(self):
        scheduler = Scheduler(self.cfg, -1)
        marker = self.root / "worker-execution-count"
        raw = self.spec("one-shot-worker", max_runtime_seconds=5,
            command=[sys.executable, "-B", "-c", "from pathlib import Path; import time; "
                     f"Path({str(marker)!r}).open('a').write('started\\n'); time.sleep(.2)"])
        job = scheduler.get(scheduler.submit(raw)["id"])
        scheduler.launch(job, "GPU-a")
        duplicate = scheduler._spawn_worker(Path(job["directory"]), -1, {})
        duplicate.wait(timeout=5)
        job["process"].wait(timeout=5)
        exit_path = Path(job["directory"]) / "exit.json"
        original = exit_path.read_bytes()
        second = scheduler._spawn_worker(Path(job["directory"]), -1, {})
        second.wait(timeout=5)
        self.assertEqual(exit_path.read_bytes(), original)
        self.assertEqual(marker.read_text().splitlines(), ["started"])

    def test_queue_timeout_is_ignored_and_only_deadline_expires(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.get(scheduler.submit(self.spec("waiting", queue_timeout_seconds=.1))["id"])
        job["submitted_mono"] -= 100
        scheduler.tick(None)
        self.assertEqual(job["state"], "QUEUED")
        job["deadline_epoch"] = time.time() + 1
        job["deadline_mono"] = time.monotonic() + 1
        scheduler.tick(None)
        self.assertEqual(job["state"], "EXPIRED")
        self.assertEqual(job["reason"], "insufficient_remaining_budget")

    def test_admission_includes_queued_work_and_rejects_impossible_start(self):
        scheduler = Scheduler(self.cfg, -1)
        scheduler.snapshot = simulated_probe(self.cfg)
        head = scheduler.submit(self.spec("head", max_runtime_seconds=80))
        tail = scheduler.submit(self.spec("tail", deadline_epoch=time.time() + 60))
        self.assertEqual(head["state"], "QUEUED")
        self.assertEqual(tail["state"], "INFEASIBLE")
        self.assertGreater(tail["queue"]["projected_start_epoch"], tail["queue"]["latest_start_epoch"])
        self.assertEqual(len(scheduler.requests), 2)

    def test_missing_telemetry_keeps_intent_and_does_not_invent_infeasibility(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.submit(self.spec("waiting", deadline_epoch=time.time() + 40))
        scheduler.tick(None, "probe failed")
        self.assertEqual(scheduler.get(job["id"])["state"], "QUEUED")

    def test_journal_preserves_torn_tail_but_fails_closed_on_complete_corruption(self):
        scheduler = Scheduler(self.cfg, -1)
        scheduler.submit(self.spec("waiting"))
        journal = self.root / "requests.jsonl"
        with journal.open("ab") as stream:
            stream.write(b'{"unfinished":')
        self.assertEqual(len(Journal(self.root).latest), 1)
        self.assertEqual(next(self.root.glob("journal-torn-tail-*.bin")).read_bytes(), b'{"unfinished":')
        with journal.open("ab") as stream:
            stream.write(b'{}\n')
        with self.assertRaisesRegex(ValueError, "corrupt"):
            Journal(self.root)

    def test_wait_reconciling_unknown_remains_pending(self):
        scheduler = Scheduler(self.cfg, -1)
        job = scheduler.get(scheduler.submit(self.spec("waiting"))["id"])
        job["reconciling"] = True
        scheduler.transition(job, "UNKNOWN", "restart_reconciliation")
        async def verify():
            waiting = asyncio.create_task(scheduler.wait(job["id"]))
            await asyncio.sleep(.01)
            self.assertFalse(waiting.done())
            job["reconciling"] = False
            scheduler.transition(job, "FAILED", "reconciled_exit")
            self.assertEqual((await waiting)["state"], "FAILED")
        asyncio.run(verify())

    def test_calibration_changes_reservation_before_admission_but_keeps_spec_idempotent(self):
        scheduler = Scheduler(self.cfg, -1)
        for index in range(3):
            job = scheduler.get(scheduler.submit(self.spec("history" + str(index), job_class="train-v1", ram_mib=8192))["id"])
            usage = {"sample_count": 10, "gpu_sample_count": 10,
                     "ram_anon_peak_bytes": 1024**3, "ram_total_peak_bytes": 7 * 1024**3,
                     "gpu_memory_peak_mib": 20}
            job["exit"] = {"cleanup_ok": True, "resource_usage": usage}
            job["resource_usage"] = usage
            scheduler.transition(job, "SUCCEEDED", "process_exit")
        raw = self.spec("calibrated", job_class="train-v1", ram_mib=8192)
        result = scheduler.submit(raw)
        self.assertLess(result["resources"]["ram_mib"], 8192)
        self.assertTrue(result["suggested_resources"]["applied"])
        accepted = scheduler.get(result["id"])
        self.assertEqual(accepted["spec"]["memory_max_mib"], 8192)
        restarted = Scheduler(self.cfg, -1)
        self.assertEqual(restarted.submit(raw)["id"], result["id"])


if __name__ == "__main__":
    unittest.main()
