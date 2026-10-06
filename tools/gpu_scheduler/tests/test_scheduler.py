from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.gpu_scheduler import (Client, JobWaitInterrupted, JobWaitTimeout, RemoteClient,
                                SchedulerError)
from tools.gpu_scheduler.common import check_storage, processes, read_json, validate_config, validate_job
from tools.gpu_scheduler.resources import choose_gpu, probe, simulated_probe
from tools.gpu_scheduler.scheduler import Scheduler

CLI = ROOT / "tools/gpu_scheduler/cli.py"
SCRATCH = ROOT / "notes/gpu-scheduler-v1/scratch"


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError("condition did not become true")


def config(root):
    return dict(version=1, root=str(root), data_mount=None,
                gpus=[dict(uuid="GPU-test", memory_mib=100, compute_units=100)],
                cpu_cores=8, ram_mib=8192, poll_seconds=.05, service_seconds=120,
                min_free_disk_mib=0, max_bypass=2)


def spec(cwd, request="request-1", seconds=10, **kwargs):
    return dict(request_id=request, owner="agent-a", cwd=str(cwd),
                command=[sys.executable, "-B", "-c", f"import time; time.sleep({seconds})"],
                memory_mib=30, compute_units=40, max_runtime_seconds=30, **kwargs)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = validate_config(config(ROOT), local_test=True)
        self.job = validate_job(spec(ROOT), self.cfg)

    def test_reservations_include_unused_future_peak_and_measured_overhead(self):
        active = [{"id": "a", "token": "x" * 32, "gpu_uuid": "GPU-test", "spec": self.job}]
        snap = simulated_probe(self.cfg)
        snap["gpus"]["GPU-test"].update(used_mib=60, processes=[{"pid": 123, "memory_mib": 10}])
        with patch("tools.gpu_scheduler.resources.processes.scope_members", return_value=[(123, 1)]):
            gpu, why = choose_gpu(self.job, active, self.cfg, snap, set())
        self.assertIsNone(gpu)  # 60 measured + 20 promised + 30 new > 100
        self.assertIn("insufficient_memory", why)

    def test_unknown_process_and_stale_telemetry_block(self):
        snap = simulated_probe(self.cfg)
        snap["gpus"]["GPU-test"]["processes"] = [{"pid": 123, "memory_mib": 1}]
        self.assertIn("unidentified", choose_gpu(self.job, [], self.cfg, snap, set())[1])
        snap["at"] -= 30
        self.assertIn("stale", choose_gpu(self.job, [], self.cfg, snap, set())[1])

    def test_compute_cpu_and_quarantine(self):
        active = [{"id": "a", "token": "x" * 32, "gpu_uuid": "GPU-test",
                   "spec": {**self.job, "compute_units": 80}}]
        snap = simulated_probe(self.cfg)
        self.assertIn("compute", choose_gpu(self.job, active, self.cfg, snap, set())[1])
        active[0]["spec"]["cpu_cores"] = 8
        self.assertIn("cpu_cores", choose_gpu(self.job, active, self.cfg, snap, set())[1])
        self.assertIn("quarantined", choose_gpu(self.job, [], self.cfg, snap, {"GPU-test"})[1])

    def test_bad_specs_and_system_disk_rejected(self):
        for changes in ({"max_runtime_seconds": float("nan")}, {"memory_mib": 101},
                        {"compute_units": True}, {"gpu_uuid": "GPU-absent"}, {"unknown": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_job({**spec(ROOT), **changes}, self.cfg)
        with self.assertRaises(ValueError):
            validate_config({**config(ROOT), "data_mount": "/"})
        for changes in ({"max_running": True}, {"max_running": 0}, {"max_running": 33},
                        {"scheduling_policy": "random"}, {"starvation_seconds": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config({**config(ROOT), **changes}, local_test=True)

    def test_timezone_deadline_normalizes_for_idempotency(self):
        offset = validate_job({**spec(ROOT), "deadline_at": "2026-10-07T00:00:00+08:00"}, self.cfg)
        utc = validate_job({**spec(ROOT), "deadline_at": "2026-10-06T16:00:00Z"}, self.cfg)
        numeric = validate_job({**spec(ROOT), "deadline_epoch": 1791302400}, self.cfg)
        self.assertEqual(offset, utc)
        self.assertEqual(offset, numeric)
        self.assertNotIn("deadline_at", offset)
        for value in ("2026-10-07T00:00:00", "invalid", True, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "timezone"):
                validate_job({**spec(ROOT), "deadline_at": value}, self.cfg)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_job({**spec(ROOT), "deadline_at": "2026-10-07T00:00:00+08:00",
                          "deadline_epoch": numeric["deadline_epoch"]}, self.cfg)

    def test_gpu_parser_fails_closed_on_unobservable_memory(self):
        results = [subprocess.CompletedProcess([], 0, "GPU-test, 100, 10, 50\n"),
                   subprocess.CompletedProcess([], 0, "GPU-test, 123, [N/A]\n")]
        with patch("tools.gpu_scheduler.resources.subprocess.run", side_effect=results):
            with self.assertRaises(ValueError):
                probe()

    def test_only_explicit_nested_data_mounts_accept_another_device(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            primary = base / 'data'
            nested = primary / 'task'
            nested.mkdir(parents=True)
            def fake_stat(path, *args, **kwargs):
                device = 2 if path.is_relative_to(nested) else 1 if path.is_relative_to(primary) else 0
                return SimpleNamespace(st_dev=device, st_mode=0o40755)
            with patch.object(Path, 'is_mount', lambda p: p in (primary, nested)), \
                 patch.object(Path, 'stat', fake_stat):
                cfg = {'local_test': False, 'data_mount': str(primary), 'data_device': 1}
                with self.assertRaisesRegex(ValueError, 'unexpected device'):
                    check_storage(cfg, nested)
                cfg.update(data_mounts=[str(nested)], data_devices={str(nested): 2})
                check_storage(cfg, nested, primary)
                cfg['data_devices'][str(nested)] = 3
                with self.assertRaisesRegex(ValueError, 'device changed'):
                    check_storage(cfg, nested)
                cfg['data_devices'][str(nested)] = 0
                with self.assertRaisesRegex(ValueError, 'device changed'):
                    check_storage(cfg, nested)
                with patch.object(Path, 'is_mount', lambda p: p == primary):
                    with self.assertRaisesRegex(ValueError, 'mounted'):
                        check_storage(cfg, nested)


class QueueAdmissionTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="q-", dir=SCRATCH)
        self.base = Path(self.temp.name)
        self.cfg = validate_config(config(self.base / "r"), local_test=True)
        self.scheduler = Scheduler(self.cfg, -1)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_jobs_enter_queue_when_each_resource_is_temporarily_unavailable(self):
        scenarios = ("memory", "compute", "cpu_cores", "ram_mib", "global_concurrency", "telemetry")
        for index, scenario in enumerate(scenarios):
            with self.subTest(resource=scenario):
                self.scheduler = Scheduler(self.cfg, -1)
                submitted = self.scheduler.submit(spec(self.base, f"resource-{index}"))
                self.assertEqual(submitted["state"], "QUEUED")
                snapshot = simulated_probe(self.cfg)
                occupied = {"id": "occupied", "token": "x" * 32, "gpu_uuid": "GPU-test",
                            "spec": validate_job(spec(self.base, "occupied"), self.cfg)}
                active = [occupied]
                if scenario == "memory":
                    snapshot["gpus"]["GPU-test"]["used_mib"] = 90
                elif scenario == "compute":
                    occupied["spec"]["compute_units"] = 80
                elif scenario == "cpu_cores":
                    occupied["spec"]["cpu_cores"] = 8
                elif scenario == "ram_mib":
                    occupied["spec"]["ram_mib"] = 8192
                elif scenario == "global_concurrency":
                    active = [occupied, {**occupied, "id": "occupied-other"}]
                else:
                    active, snapshot = [], None
                with patch.object(self.scheduler, "active", return_value=active), \
                     patch.object(self.scheduler, "reconcile"), \
                     patch("tools.gpu_scheduler.resources.processes.scope_members", return_value=[]):
                    self.scheduler.tick(snapshot)
                queued = self.scheduler.view(self.scheduler.get(submitted["id"]))
                self.assertEqual(queued["state"], "QUEUED")
                self.assertEqual(queued["id"], submitted["id"])
                self.assertIsNone(queued["exit"])
                expected = "telemetry" if scenario == "telemetry" else scenario
                self.assertIn(expected, queued["reason"])
                self.assertEqual(read_json(Path(queued["directory"]) / "status.json")["id"], queued["id"])
                self.assertEqual(len(self.scheduler.jobs), index + 1)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="t-", dir=SCRATCH)
        self.base = Path(self.temp.name)
        self.root = self.base / "r"
        self.cfg = config(self.root)
        self.cfg_path = self.base / "config.json"
        self.daemon = None
        self.logs = []
        self.start()

    def start(self):
        self.cfg_path.write_text(json.dumps(self.cfg))
        log = (self.base / f"server-{len(self.logs)}.log").open("ab")
        self.logs.append(log)
        self.daemon = subprocess.Popen([sys.executable, "-B", str(CLI), "serve", "--local-test",
                                        "--config", str(self.cfg_path)], stdout=log, stderr=log,
                                       start_new_session=True)

        def ready():
            if self.daemon.poll() is not None:
                raise AssertionError((self.base / f"server-{len(self.logs)-1}.log").read_text())
            try:
                self.client = Client(self.root, timeout=3)
                return True
            except SchedulerError:
                return False
        eventually(ready)

    def tearDown(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.terminate()
            self.daemon.wait(timeout=12)
        # Tests clean only their own recorded tokens, including crash-test orphans.
        for launch in self.root.glob("sessions/*/jobs/*/launch.json"):
            self.assertTrue(processes.terminate_scope(read_json(launch)["token"], .1))
        for log in self.logs:
            log.close()
        self.temp.cleanup()

    def submit(self, name, **changes):
        return self.client.submit_async({**spec(self.base, name), **changes})

    def running(self, job):
        return eventually(lambda: self.client.get(job["id"])["state"] == "RUNNING")

    def test_two_shared_jobs_third_queued_and_targeted_cancel(self):
        # All three would fit declared resources: only the global cap blocks #3.
        first, second, third = [self.submit(f"job-{i}", memory_mib=20, compute_units=20) for i in range(3)]
        self.running(first)
        self.running(second)
        self.assertEqual(self.client.get(third["id"])["state"], "QUEUED")
        self.assertEqual(self.client.status()["active_count"], 2)
        self.assertEqual(self.client.get(first["id"])["gpu_uuid"], self.client.get(second["id"])["gpu_uuid"])
        self.client.cancel(first["id"], "test single-job cancel")
        self.assertEqual(self.client.wait(first["id"], interval=.05)["state"], "CANCELLED")
        self.running(third)
        self.assertEqual(self.client.get(second["id"])["state"], "RUNNING")

    def test_module_cli_cancel_alias_is_scoped_and_survives_session_change(self):
        first, peer = [self.submit(f"cli-cancel-{i}") for i in range(2)]
        self.running(first)
        self.running(peer)
        command = [sys.executable, "-B", "-m", "tools.gpu_scheduler.cli", "cancel",
                   "--root", str(self.root), "--session", "wrong-session",
                   "--job-id", first["id"], "--reason", "review regression"]
        denied = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(denied.returncode, 0)
        command[command.index("wrong-session")] = self.client.session_id
        cancelled = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        receipt = json.loads(cancelled.stdout)
        self.assertEqual(receipt["id"], first["id"])
        self.assertEqual(self.client.wait(first["id"], timeout=5)["state"], "CANCELLED")
        self.assertEqual(self.client.get(peer["id"])["state"], "RUNNING")

    def test_four_concurrent_jobs_with_configured_limit_and_fifth_waiting(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=10)
        self.cfg.update(max_running=4, scheduling_policy="fair_share")
        self.start()
        jobs = [self.submit(f"four-{i}", memory_mib=15, compute_units=15, owner=f"agent-{i}") for i in range(5)]
        for job in jobs[:4]:
            self.running(job)
        self.assertEqual(self.client.status()["active_count"], 4)
        self.assertEqual(self.client.status()["max_running"], 4)
        self.assertEqual(self.client.get(jobs[4]["id"])["state"], "QUEUED")
        self.client.cancel(jobs[0]["id"], "release one slot")
        self.running(jobs[4])
        for job in jobs[1:4]:
            self.assertEqual(self.client.get(job["id"])["state"], "RUNNING")

    def test_submit_blocks_until_execution_finishes(self):
        started = time.monotonic()
        with patch.object(self.client, "get", side_effect=AssertionError("wait must not poll get")):
            result = self.client.submit(spec(self.base, "blocking", seconds=.35))
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertGreaterEqual(time.monotonic() - started, .25)

    def test_official_wait_forwards_queue_and_terminal_updates_without_adapter_polling(self):
        first = self.submit("callback-blocker", memory_mib=100, compute_units=100)
        self.running(first)
        queued = self.submit("callback-original", command=[sys.executable, "-B", "-c", "pass"])
        updates = []
        def update(job):
            updates.append(job)
            if job['state'] == 'QUEUED':
                self.client.cancel(first['id'], 'release for callback test')
        result = self.client.wait(queued['id'], timeout=6, on_update=update)
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertEqual(updates[0]['state'], 'QUEUED')
        self.assertEqual(updates[-1]['state'], 'SUCCEEDED')
        self.assertTrue(all(job['id'] == queued['id'] for job in updates))

    def test_persistent_service_keeps_queue_and_execution_timeouts(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=10)
        self.cfg.pop("service_seconds")
        self.cfg["persistent"] = True
        self.start()
        self.assertIsNone(self.client.status()["deadline_epoch"])
        running = self.submit("persistent-timeout", max_runtime_seconds=1,
                              memory_mib=100, compute_units=100)
        self.running(running)
        expired = self.client.submit({**spec(self.base, "persistent-queue"),
                                      "deadline_epoch": time.time() + 30.2}, timeout=5)
        self.assertEqual(expired["state"], "INFEASIBLE")
        result = self.client.wait(running["id"], timeout=5)
        self.assertEqual(result["state"], "TIMED_OUT")
        self.assertTrue(result["exit"]["cleanup_ok"])
        self.assertFalse(self.client.status()["stopping"])
        self.assertEqual(self.client.submit({**spec(self.base, "parent-expired"),
            "deadline_epoch": time.time() - 1})["state"], "INFEASIBLE")
        good = self.client.submit(spec(self.base, "persistent-next", seconds=.1))
        self.assertEqual(good["state"], "SUCCEEDED")

    def test_submit_blocks_through_resource_queue_and_wakes_after_release(self):
        from concurrent.futures import ThreadPoolExecutor

        first = self.submit("queue-first", command=[sys.executable, "-B", "-c",
                                                    "import time; time.sleep(.8)"],
                            memory_mib=100, compute_units=100)
        self.running(first)
        queued_spec = {**spec(self.base, "queue-blocking", seconds=.1),
                       "memory_mib": 100, "compute_units": 100}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.submit, queued_spec, timeout=6)
            def queued():
                try:
                    return self.client.get(request_id="queue-blocking")["state"] == "QUEUED"
                except SchedulerError:
                    return False
            eventually(queued)
            self.assertFalse(future.done())
            self.client.cancel(first["id"], "release resource for blocking queue test")
            self.assertEqual(self.client.wait(first["id"], timeout=6)["state"], "CANCELLED")
            result = future.result(timeout=6)
        self.assertEqual(result["state"], "SUCCEEDED")

    def test_invalid_submission_fails_before_queue_creation(self):
        for changes in ({"memory_mib": 101}, {"compute_units": 101}, {"cpu_cores": 9},
                        {"ram_mib": 8193}, {"gpu_uuid": "GPU-absent"},
                        {"max_runtime_seconds": 43201}, {"command": []}):
            with self.subTest(changes=changes), self.assertRaises(SchedulerError):
                self.client.submit({**spec(self.base, "invalid"), **changes})
        self.assertEqual(self.client.jobs(), [])
        self.assertEqual(list(self.root.glob("sessions/*/jobs/*")), [])

    def test_invalid_wait_options_are_rejected_before_admission(self):
        for timeout in (-1, True, float("inf"), float("nan"), 43201):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.client.submit(spec(self.base, "invalid-timeout"), timeout=timeout)
        for timeout in (-1, True, "invalid", 43201):
            with self.subTest(raw_timeout=timeout), self.assertRaises(SchedulerError):
                self.client._request("submit", spec=spec(self.base, "invalid-raw-timeout"),
                                     timeout=timeout)
        self.assertEqual(self.client.jobs(), [])

    def test_zero_timeout_returns_identity_without_cancelling_queued_job(self):
        first = self.submit("zero-timeout-first", memory_mib=100, compute_units=100)
        self.running(first)
        with self.assertRaises(JobWaitTimeout) as waiting:
            self.client.submit(spec(self.base, "zero-timeout-queued", seconds=.1), timeout=0)
        queued = waiting.exception.job
        self.assertEqual(queued["state"], "QUEUED")
        self.assertEqual(waiting.exception.job_id, queued["id"])
        self.assertEqual(self.client.get(request_id="zero-timeout-queued")["id"], queued["id"])
        self.client.cancel(first["id"])
        self.assertEqual(self.client.wait(queued["id"], timeout=6)["state"], "SUCCEEDED")

    def test_blocking_submission_returns_queue_expiry(self):
        first = self.submit("expiry-first", memory_mib=100, compute_units=100)
        self.running(first)
        result = self.client.submit({**spec(self.base, "blocking-expiry"),
                                     "deadline_epoch": time.time() + 31}, timeout=5)
        self.assertEqual(result["state"], "INFEASIBLE")
        self.assertEqual(result["reason"], "projected_start_after_latest_start")
        self.assertEqual(self.client.get(first["id"])["state"], "RUNNING")

    def test_concurrent_blocking_submissions_share_one_job_and_wake_all_waiters(self):
        from concurrent.futures import ThreadPoolExecutor

        value = spec(self.base, "blocking-idempotent", seconds=.3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.client.submit(value, timeout=6), range(8)))
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertTrue(all(result["state"] == "SUCCEEDED" for result in results))
        self.assertEqual(len(self.client.jobs()), 1)
        directory = Path(results[0]["directory"])
        self.assertEqual(read_json(directory / "status.json")["revision"], results[0]["revision"])

    def test_disconnected_waiters_release_connections_without_cancelling_job(self):
        job = self.submit("disconnected-waiters")
        self.running(job)
        request = json.dumps({"op": "wait", "session_id": self.client.session_id,
                              "id": job["id"]}).encode() + b"\n"
        for _ in range(40):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(self.client.socket_path)
                connection.sendall(request)
            time.sleep(.01)
        with self.assertRaises(JobWaitTimeout):
            self.client.wait(job["id"], timeout=.05)
        self.assertEqual(self.client.get(job["id"])["state"], "RUNNING")
        self.assertEqual(len(self.client.jobs()), 1)

    def test_full_wait_capacity_does_not_starve_queries_or_cancellation(self):
        job = self.submit("full-wait-capacity")
        self.running(job)
        request = json.dumps({"op": "wait", "session_id": self.client.session_id,
                              "id": job["id"]}).encode() + b"\n"
        connections = []
        try:
            for _ in range(32):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connections.append(connection)
                connection.settimeout(3)
                connection.connect(self.client.socket_path)
                connection.sendall(request)
            time.sleep(.15)
            with self.assertRaisesRegex(SchedulerError, "too many concurrent blocking waits"):
                self.client.wait(job["id"], timeout=0)
            self.assertEqual(self.client.status()["active_count"], 1)
            self.client.cancel(job["id"])
            for connection in connections:
                with connection.makefile("rb") as stream:
                    response = json.loads(stream.readline())
                self.assertTrue(response["ok"])
                self.assertEqual(response["result"]["state"], "CANCELLED")
        finally:
            for connection in connections:
                connection.close()

    def test_service_stop_releases_blocking_waiters_with_resumable_interruption(self):
        from concurrent.futures import ThreadPoolExecutor

        job = self.submit("shutdown-waiter")
        self.running(job)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.wait, job["id"], timeout=6)
            time.sleep(.1)
            self.client.stop()
            try:
                result = future.result(timeout=6)
            except JobWaitInterrupted as interrupted:
                self.assertEqual(interrupted.reason, "service_stop")
                self.assertEqual(interrupted.job_id, job["id"])
                self.assertEqual(interrupted.session_id, self.client.session_id)
                self.assertEqual(interrupted.job["state"], "CANCELLING")
            else:
                self.assertEqual(result["state"], "CANCELLED")
        self.daemon.wait(timeout=10)
        receipt = read_json(Path(job["directory"]) / "exit.json")
        self.assertTrue(receipt["cleanup_ok"])

    def test_client_interrupt_keeps_original_identity_without_replaying(self):
        with patch.object(self.client, "_request", side_effect=KeyboardInterrupt) as request:
            with self.assertRaises(JobWaitInterrupted) as interrupted:
                self.client.submit(spec(self.base, "client-interrupted"))
            self.assertEqual(interrupted.exception.request_id, "client-interrupted")
            self.assertEqual(interrupted.exception.session_id, self.client.session_id)
            self.assertIsNone(interrupted.exception.job)
            self.assertEqual(request.call_count, 1)
            with self.assertRaises(JobWaitInterrupted) as waiting:
                self.client.wait("known-job")
            self.assertEqual(waiting.exception.job_id, "known-job")

    def test_cli_submit_wait_timeout_and_explicit_enqueue(self):
        spec_path = self.base / "cli-job.json"
        spec_path.write_text(json.dumps(spec(self.base, "cli-blocking", seconds=.1)))
        command = [sys.executable, "-B", str(CLI), "submit", "--root", str(self.root),
                   "--spec", str(spec_path)]
        finished = subprocess.run(command, capture_output=True, text=True, timeout=6)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(json.loads(finished.stdout)["state"], "SUCCEEDED")
        spec_path.write_text(json.dumps(spec(self.base, "cli-timeout", seconds=2)))
        timed_out = subprocess.run(command + ["--timeout", "0"], capture_output=True,
                                   text=True, timeout=6)
        self.assertEqual(timed_out.returncode, 3, timed_out.stderr)
        response = json.loads(timed_out.stderr)
        self.assertTrue(response["resumable"])
        self.assertEqual(response["job"]["request_id"], "cli-timeout")
        self.client.cancel(response["job"]["id"])
        spec_path.write_text(json.dumps(spec(self.base, "cli-async", seconds=.1)))
        command[3] = "enqueue"
        enqueued = subprocess.run(command, capture_output=True, text=True, timeout=6)
        self.assertEqual(enqueued.returncode, 0, enqueued.stderr)
        queued = json.loads(enqueued.stdout)
        self.assertEqual(queued["state"], "QUEUED")
        self.assertEqual(self.client.wait(queued["id"], timeout=6)["state"], "SUCCEEDED")

    def test_cli_interrupt_returns_identity_without_cancelling_training(self):
        spec_path = self.base / "interrupt-job.json"
        spec_path.write_text(json.dumps(spec(self.base, "cli-interrupted", seconds=10)))
        command = [sys.executable, "-B", str(CLI), "submit", "--root", str(self.root),
                   "--spec", str(spec_path)]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as caller:
            def started():
                try:
                    return self.client.get(request_id="cli-interrupted")["state"] == "RUNNING"
                except SchedulerError:
                    return False
            eventually(started)
            caller.send_signal(signal.SIGTERM)
            stdout, stderr = caller.communicate(timeout=6)
            self.assertEqual(caller.returncode, 3, stderr)
            self.assertEqual(stdout, "")
            response = json.loads(stderr)
            self.assertTrue(response["resumable"])
            self.assertEqual(response["request_id"], "cli-interrupted")
            self.assertEqual(response["reason"], "client_interrupted")
        job = self.client.get(request_id="cli-interrupted")
        self.assertEqual(job["state"], "RUNNING")
        self.client.cancel(job["id"])

    def test_idempotency_mismatch_and_concurrent_submissions(self):
        from concurrent.futures import ThreadPoolExecutor
        value = spec(self.base)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.client.submit_async(value), range(8)))
        self.assertEqual(len({j["id"] for j in jobs}), 1)
        with self.assertRaises(SchedulerError):
            self.client.submit_async({**value, "memory_mib": 40})

    def test_two_clients_eight_jobs_share_one_global_queue(self):
        from concurrent.futures import ThreadPoolExecutor
        clients = [self.client, Client(self.root)]
        self.assertEqual(clients[0].session_id, clients[1].session_id)

        def submit_one(index):
            return clients[index % 2].submit_async({**spec(self.base, f"agent-{index}", seconds=.5),
                                                    "owner": f"agent-{index}", "memory_mib": 10,
                                                    "compute_units": 10, "ram_mib": 256,
                                                    "max_runtime_seconds": 1})

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(submit_one, range(8)))
        self.assertEqual(len({j["id"] for j in jobs}), 8)
        counts = []

        def complete():
            counts.append(clients[len(counts) % 2].status()["active_count"])
            states = clients[1].jobs()
            return len(states) == 8 and all(j["state"] == "SUCCEEDED" for j in states)

        eventually(complete, timeout=12)
        self.assertEqual(max(counts), 2)
        # Confirm actual executor intervals too, rather than only trusting status.
        boundaries = []
        for job in jobs:
            directory = Path(job["directory"])
            boundaries.append((read_json(directory / "started.json")["at"], 1))
            boundaries.append((read_json(directory / "exit.json")["at"], -1))
        active = peak = 0
        for _, delta in sorted(boundaries):
            active += delta
            peak = max(peak, active)
        self.assertEqual(peak, 2)

    def test_remote_bridge_uses_stdin_and_same_session_without_replay(self):
        import shlex
        from tools.gpu_monitor import monitor

        cfg = self.base / "remote.json"
        cfg.write_text(json.dumps({"version": 1, "auth_file": "auth-unused.txt", "python": sys.executable,
                                   "cli": str(CLI), "root": str(self.root)}))
        auth = {"host": "test.invalid", "port": 22, "user": "test", "password": "fixture"}
        commands = []
        timeouts = []
        requests = []

        def transport(host, command, code, timeout):
            commands.append(command)
            timeouts.append(timeout)
            requests.append(json.loads(code))
            return subprocess.run(shlex.split(command[-1]), input=code, text=True, capture_output=True, timeout=timeout)

        with patch.object(monitor, "load_auth", return_value=auth), patch.object(monitor, "run_ssh", side_effect=transport):
            remote = RemoteClient(cfg)
            self.assertEqual(remote.session_id, self.client.session_id)
            job = remote.submit({**spec(self.base), "command": [sys.executable, "-c", "print('literal $HOME; not shell')"]})
            self.assertEqual(remote.wait(job["id"], timeout=5, interval=.05)["state"], "SUCCEEDED")
            self.assertEqual(remote.get(request_id="request-1")["id"], job["id"])
            self.assertEqual(len(remote.jobs()), 1)
            self.assertNotIn("literal $HOME", repr(commands))
            self.assertEqual(requests[1]["op"], "submit")
            self.assertFalse(requests[1]["wait"])
            self.assertEqual(timeouts[1], remote.timeout)
            self.assertEqual(requests[2]["op"], "watch")
            pending = remote.submit_async(spec(self.base, "remote-pending", seconds=2))
            self.assertEqual(requests[-1]["op"], "submit")
            self.assertFalse(requests[-1]["wait"])
            self.assertEqual(timeouts[-1], remote.timeout)
            with self.assertRaises(JobWaitTimeout) as timed_out:
                remote.wait(pending["id"], timeout=0)
            self.assertEqual(timed_out.exception.job_id, pending["id"])
            remote.cancel(pending["id"])
            self.assertEqual(requests[-1]["op"], "cancel")
            self.assertEqual(requests[-1]["session_id"], remote.session_id)
            self.assertEqual(remote.wait(pending["id"], timeout=6)["state"], "CANCELLED")
            interrupted = {"ok": False, "code": "WAIT_INTERRUPTED", "reason": "service_deadline",
                           "job": pending}
            response = subprocess.CompletedProcess([], 0, json.dumps(interrupted), "")
            with patch.object(monitor, "run_ssh", return_value=response):
                with self.assertRaises(JobWaitInterrupted) as stopped:
                    remote.wait(pending["id"])
            self.assertEqual(stopped.exception.reason, "service_deadline")
            self.assertEqual(stopped.exception.job_id, pending["id"])
            with patch.object(monitor, "run_ssh", side_effect=subprocess.TimeoutExpired("ssh", 1)) as failing:
                with self.assertRaisesRegex(SchedulerError, "UNKNOWN"):
                    remote.submit(spec(self.base, "uncertain"))
                self.assertEqual(failing.call_count, 1)

    def test_timeout_cleans_detached_descendant(self):
        code = ("import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); "
                "time.sleep(60)")
        job = self.submit("timeout", command=[sys.executable, "-B", "-c", code], max_runtime_seconds=.4)
        result = self.client.wait(job["id"], timeout=6, interval=.05)
        self.assertEqual(result["state"], "TIMED_OUT")
        self.assertTrue(result["exit"]["cleanup_ok"])
        launch = read_json(Path(result["directory"]) / "launch.json")
        self.assertEqual(processes.scope_members(launch["token"]), [])

    def test_bounded_backfill_protects_large_job(self):
        large_running = self.submit("active", memory_mib=60, compute_units=60)
        self.running(large_running)
        waiting = self.submit("large-waiting", memory_mib=80, compute_units=80)
        small = [self.submit(f"small-{i}", command=[sys.executable, "-B", "-c", "pass"],
                             memory_mib=20, compute_units=20, max_runtime_seconds=1) for i in range(3)]
        for job in small[:2]:
            self.assertEqual(self.client.wait(job["id"], timeout=6, interval=.05)["state"], "SUCCEEDED")
        self.assertEqual(self.client.wait(small[2]["id"], timeout=6)["state"], "SUCCEEDED")
        self.assertEqual(self.client.get(waiting["id"])["state"], "QUEUED")
        self.client.cancel(large_running["id"])
        self.running(waiting)

    def test_queue_timeout_cancel_and_no_wait_side_effect(self):
        first = self.submit("full", memory_mib=100, compute_units=100)
        self.running(first)
        expired = self.submit("expire", deadline_epoch=time.time() + 31)
        cancelled = self.submit("cancel")
        self.assertEqual(self.client.cancel(cancelled["id"])["state"], "CANCELLED")
        with self.assertRaises(TimeoutError) as timeout:
            self.client.wait(first["id"], timeout=.1, interval=.03)
        self.assertEqual(timeout.exception.job_id, first["id"])
        self.assertEqual(timeout.exception.job["state"], "RUNNING")
        self.assertEqual(self.client.wait(expired["id"], timeout=4, interval=.05)["state"], "INFEASIBLE")
        self.assertEqual(self.client.get(first["id"])["state"], "RUNNING")

    def test_success_failure_and_graceful_service_stop(self):
        success = self.client.submit({**spec(self.base, "ok"),
                                      "command": [sys.executable, "-c", "print('result')"]})
        failure = self.client.submit({**spec(self.base, "bad"),
                                      "command": ["/nonexistent/gpu-scheduler-test-command"]})
        self.assertEqual(success["state"], "SUCCEEDED")
        self.assertEqual(failure["state"], "FAILED")
        self.assertIn("result", (Path(success["directory"]) / "stdout.log").read_text())
        running = self.submit("running", memory_mib=100)
        self.running(running)
        queued = self.submit("queued-on-stop")
        self.client.stop()
        self.daemon.wait(timeout=10)
        receipt = read_json(Path(running["directory"]) / "exit.json")
        self.assertEqual(receipt["reason"], "cancelled")
        self.assertTrue(receipt["cleanup_ok"])
        self.assertEqual(read_json(Path(queued["directory"]) / "status.json")["state"], "CANCELLED")

    def test_executor_loss_reclaims_scope_before_next_job(self):
        running = self.submit("executor-loss", memory_mib=100)
        waiting = self.submit("after-executor-loss")
        self.running(running)
        started = read_json(Path(running["directory"]) / "started.json")
        pid = started["worker_pid"]
        self.assertTrue(processes.signal_identity(pid, processes.process_start_ticks(pid), signal.SIGKILL))
        result = self.client.wait(running["id"], timeout=6, interval=.05)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["exit"]["reason"], "executor_lost")
        self.assertTrue(result["exit"]["cleanup_ok"])
        self.assertTrue((Path(running["directory"]) / "recovery-exit.json").exists())
        self.running(waiting)

    def test_crash_restarts_immediately_preserves_queue_and_never_reexecutes(self):
        marker = self.base / "execution-count.txt"
        running = self.submit("orphan", max_runtime_seconds=4,
            command=[sys.executable, "-B", "-c", "from pathlib import Path; import time; "
                     f"p=Path({str(marker)!r}); p.open('a').write('started\\n'); time.sleep(10)"])
        queued = self.submit("retained-queue", memory_mib=100)
        self.running(running)
        self.assertEqual(self.client.get(queued["id"])["state"], "QUEUED")
        queued_before = self.client.get(queued["id"])
        old_client = self.client
        old_session = self.client.session_id
        self.daemon.kill()
        self.daemon.wait(timeout=3)
        launch = read_json(Path(running["directory"]) / "launch.json")
        self.assertTrue(processes.scope_members(launch["token"]))
        self.start()
        self.assertNotEqual(self.client.session_id, old_session)
        recovered = self.client.get(running["id"])
        self.assertEqual(recovered["state"], "UNKNOWN")
        self.assertTrue(recovered["reconciling"])
        self.assertEqual(recovered["id"], running["id"])
        self.assertTrue(processes.scope_members(launch["token"]))
        queued_after = self.client.get(queued["id"])
        self.assertEqual(queued_after["state"], "QUEUED")
        for key in ("id", "submitted_at", "deadline_epoch", "bypasses", "directory"):
            self.assertEqual(queued_after[key], queued_before[key])
        exit_path = Path(running["directory"]) / "exit.json"
        eventually(exit_path.exists, timeout=6)
        self.assertEqual(read_json(exit_path)["reason"], "deadline")
        self.assertEqual(processes.scope_members(launch["token"]), [])
        self.assertEqual(len(self.client.jobs()), 2)
        self.assertEqual(old_client.status()["session_id"], self.client.session_id)
        self.assertEqual(self.client.get(queued["id"])["id"], queued["id"])
        self.assertEqual(self.client.wait(running["id"], timeout=3)["state"], "TIMED_OUT")
        self.running(queued)
        self.assertEqual(marker.read_text().splitlines(), ["started"])

    def test_restart_reconciles_lost_executor_identity_before_dispatch(self):
        running = self.submit("orphan-worker-loss", memory_mib=100)
        queued = self.submit("after-orphan-worker-loss")
        self.running(running)
        identity = read_json(Path(running["directory"]) / "executor.json")
        self.daemon.kill()
        self.daemon.wait(timeout=3)
        self.start()
        self.assertEqual(self.client.get(running["id"])["state"], "UNKNOWN")
        self.assertTrue(processes.signal_identity(identity["pid"], identity["start_ticks"],
                                                  signal.SIGKILL, identity["boot_id"]))
        receipt = self.client.wait(running["id"], timeout=6)
        self.assertEqual(receipt["state"], "FAILED")
        self.assertEqual(receipt["exit"]["reason"], "executor_lost")
        self.assertTrue(receipt["exit"]["cleanup_ok"])
        self.running(queued)

    def test_restarted_service_can_cancel_the_original_executor(self):
        running = self.submit("cancel-restored", memory_mib=100)
        self.running(running)
        identity = read_json(Path(running["directory"]) / "executor.json")
        self.daemon.kill()
        self.daemon.wait(timeout=3)
        self.start()
        self.assertEqual(self.client.get(running["id"])["state"], "UNKNOWN")
        self.client.cancel(running["id"], "cancel restored executor")
        result = self.client.wait(running["id"], timeout=5)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertTrue(result["exit"]["cleanup_ok"])
        self.assertFalse(processes.pid_matches(identity["pid"], identity["start_ticks"], identity["boot_id"]))


if __name__ == "__main__":
    unittest.main()
