import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.gpu_scheduler.common import validate_config, validate_job
from tools.gpu_scheduler.fairness import order_queue, projected_start, safe_backfill
from tools.gpu_scheduler.resources import choose_gpu, simulated_probe
from tools.gpu_scheduler.scheduler import Scheduler


class FairnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = validate_config({
            "version": 1, "root": self.tmp.name, "data_mount": None,
            "gpus": [{"uuid": "GPU-a", "memory_mib": 100, "compute_units": 100}],
            "cpu_cores": 8, "ram_mib": 8192, "persistent": True, "min_free_disk_mib": 0,
            "max_running": 4, "scheduling_policy": "fair_share", "max_bypass": 2,
            "starvation_seconds": 60}, local_test=True)
        self.snapshot = simulated_probe(self.cfg)

    def job(self, name, owner="a", memory=20, compute=20, runtime=30, **updates):
        spec = validate_job({"request_id": name, "owner": owner, "command": ["/usr/bin/true"],
                             "cwd": self.tmp.name, "memory_mib": memory, "compute_units": compute,
                             "max_runtime_seconds": runtime}, self.cfg)
        return {"id": name, "token": "a" * 32, "spec": spec, "gpu_uuid": "GPU-a",
                "state": "QUEUED", "submitted_mono": 99, "bypasses": 0, **updates}

    def test_configured_four_slots_still_enforce_every_resource(self):
        active = [self.job(str(i)) for i in range(3)]
        job = self.job("fourth")
        self.assertEqual(choose_gpu(job["spec"], active, self.cfg, self.snapshot, set(), pid_sets={})[0], "GPU-a")
        active.append(self.job("fourth"))
        self.assertEqual(choose_gpu(job["spec"], active, self.cfg, self.snapshot, set(), pid_sets={})[1],
                         "global_concurrency_limit")
        for key, capacity in [("cpu_cores", 8), ("ram_mib", 8192), ("memory_mib", 100), ("compute_units", 100)]:
            with self.subTest(resource=key):
                occupied = self.job("occupied")
                occupied["spec"][key] = capacity
                gpu, reason = choose_gpu(job["spec"], [occupied], self.cfg, self.snapshot, set(), pid_sets={})
                self.assertIsNone(gpu)
                self.assertIn("insufficient_", reason)

    def test_dominant_share_and_round_robin_prevent_owner_monopoly(self):
        busy = self.job("running", "a", memory=60)
        a = self.job("a-next", "a", submitted_mono=90)
        b = self.job("b-next", "b", submitted_mono=99)
        self.assertEqual(order_queue([a, b], [busy], self.cfg, {}, 100)[0], b)
        self.assertEqual(order_queue([a, b], [], self.cfg, {"a": 1}, 100)[0], b)

    def test_oldest_protected_job_wins_even_against_idle_owner(self):
        aged = self.job("aged", "a", submitted_mono=30)
        bypassed = self.job("bypassed", "a", submitted_mono=50, bypasses=2)
        fresh = self.job("fresh", "b")
        active = [self.job("busy", "a")]
        self.assertEqual(order_queue([fresh, bypassed, aged], active, self.cfg, {}, 100),
                         [aged, bypassed, fresh])

    def test_short_backfill_fills_idle_capacity_without_delaying_reservation(self):
        active = [self.job("active", memory=60, compute=60, state="RUNNING", execution_deadline_mono=200)]
        head = self.job("large", memory=80, compute=80, bypasses=2)
        short = self.job("short", memory=30, compute=30, runtime=10)
        long = self.job("long", memory=30, compute=30, runtime=300)
        self.assertEqual(projected_start(head["spec"], active, self.cfg, self.snapshot, set(), {}, 100), 210)
        self.assertTrue(safe_backfill(short, "GPU-a", [head], active, self.cfg, self.snapshot, set(), {}, 100))
        self.assertFalse(safe_backfill(long, "GPU-a", [head], active, self.cfg, self.snapshot, set(), {}, 100))
        # A long job may still fit alongside the reserved large job.
        small = self.job("small", memory=20, compute=20, runtime=300)
        self.assertTrue(safe_backfill(small, "GPU-a", [head], active, self.cfg, self.snapshot, set(), {}, 100))

    def test_forecast_only_reclaims_observed_owned_memory(self):
        active = [self.job("active", memory=60, state="RUNNING", execution_deadline_mono=200)]
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["gpus"]["GPU-a"].update(used_mib=70, processes=[{"pid": 42, "memory_mib": 60}])
        head = self.job("large", memory=80)
        self.assertEqual(projected_start(head["spec"], active, self.cfg, snapshot, set(), {"active": {42}}, 100), 210)
        snapshot["gpus"]["GPU-a"]["processes"] = []
        self.assertIsNone(projected_start(head["spec"], active, self.cfg, snapshot, set(), {}, 100))

    def test_external_blocked_gpu_does_not_block_independent_gpu(self):
        cfg = {**self.cfg, "gpus": [*self.cfg["gpus"], {"uuid": "GPU-b", "memory_mib": 100, "compute_units": 100}]}
        snapshot = simulated_probe(cfg)
        snapshot["gpus"]["GPU-a"].update(used_mib=95, processes=[{"pid": 42, "memory_mib": 95}])
        head = self.job("blocked", memory=80, bypasses=2)
        head["spec"]["gpu_uuid"] = "GPU-a"
        later = self.job("other-gpu", memory=20)
        later["spec"]["gpu_uuid"] = "GPU-b"
        self.assertTrue(safe_backfill(later, "GPU-b", [head], [], cfg, snapshot, set(), {}, 100))
        self.assertFalse(safe_backfill(later, "GPU-b", [head], [], {**cfg, "max_running": 1}, snapshot, set(), {}, 100))

    def test_unconfirmed_cleanup_never_promises_a_release(self):
        head = self.job("large", memory=80)
        active = [self.job("unknown", memory=60, state="UNKNOWN", execution_deadline_mono=200)]
        self.assertIsNone(projected_start(head["spec"], active, self.cfg, self.snapshot, set(), {}, 100))
        later = self.job("later", memory=30, runtime=1)
        self.assertFalse(safe_backfill(later, "GPU-a", [head], active, self.cfg, self.snapshot, set(), {}, 100))

    def test_shared_headroom_impossible_request_rejected_before_queue(self):
        cfg = {**self.cfg, "external_process_policy": "shared", "shared_headroom_mib": 2048,
               "gpus": [{"uuid": "GPU-a", "memory_mib": 8000, "compute_units": 100}]}
        raw = self.job("impossible")["spec"]
        with self.assertRaisesRegex(ValueError, "headroom"):
            validate_job({**raw, "memory_mib": 6000}, cfg)

    def test_same_tick_recomputes_fairness_and_reports_wait_budget_risk(self):
        scheduler = Scheduler(self.cfg, -1)
        names = ["a-1", "a-2", "a-3", "a-4", "b-1", "c-1", "d-1"]
        for name in names:
            scheduler.submit({**self.job(name, name[0])["spec"], "queue_timeout_seconds": 1})
        launched = []

        def launch(job, gpu):
            job.update(gpu_uuid=gpu, state="RUNNING", execution_deadline_mono=time.monotonic() + 200)
            launched.append(job["spec"]["owner"])

        with patch.object(scheduler, "launch", side_effect=launch), patch.object(scheduler, "reconcile"):
            scheduler.tick(self.snapshot)
        # Bypass protection is enabled; A's backlog cannot permanently exclude others.
        self.assertGreaterEqual(len(set(launched)), 3)
        self.assertEqual(len(launched), 4)
        queued = next(j for j in scheduler.jobs.values() if j["state"] == "QUEUED")
        info = scheduler.view(queued)["queue"]
        self.assertTrue(info["deadline_risk"])
        self.assertGreater(info["projected_start_epoch"], info["latest_start_epoch"])

    def test_sustained_short_jobs_do_not_postpone_protected_large_job(self):
        scheduler = Scheduler(self.cfg, -1)
        mono = time.monotonic()
        occupied = scheduler.get(scheduler.submit(self.job("occupied", memory=60, compute=60)["spec"])["id"])
        occupied.update(state="RUNNING", gpu_uuid="GPU-a", execution_deadline_mono=mono + 100)
        large = scheduler.get(scheduler.submit(self.job("large", memory=80, compute=80)["spec"])["id"])
        large["bypasses"] = 2
        dispatches = []

        def launch(job, gpu):
            job.update(state="RUNNING", gpu_uuid=gpu,
                       execution_deadline_mono=time.monotonic() + job["spec"]["max_runtime_seconds"])
            dispatches.append(job["spec"]["request_id"])

        with patch.object(scheduler, "launch", side_effect=launch), patch.object(scheduler, "reconcile"):
            for second in range(0, 120, 5):
                with patch("time.monotonic", return_value=mono + second):
                    for job in scheduler.active():
                        if job["execution_deadline_mono"] + 10 <= mono + second:
                            job["state"] = "SUCCEEDED"
                    scheduler.submit(self.job(f"short-{second}", owner="short", memory=30, compute=30, runtime=5)["spec"])
                    scheduler.tick(self.snapshot)
                    if large["state"] != "QUEUED":
                        break
        self.assertEqual(second, 110)
        self.assertEqual(large["state"], "RUNNING")
        self.assertTrue(any(name.startswith("short-") for name in dispatches))

    def test_short_queue_budget_is_never_silently_extended_by_aging(self):
        scheduler = Scheduler(self.cfg, -1)
        now = time.monotonic()
        queued = scheduler.get(scheduler.submit({**self.job("expiring")["spec"],
                                                "queue_timeout_seconds": 1})["id"])
        with patch("time.monotonic", return_value=now + 2):
            scheduler.tick(self.snapshot)
        self.assertEqual(queued["state"], "EXPIRED")
        self.assertEqual(queued["reason"], "queue_timeout")


if __name__ == "__main__":
    unittest.main()
