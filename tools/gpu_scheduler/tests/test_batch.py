import unittest
from unittest.mock import Mock

from tools.gpu_scheduler.batch import execute_one, run_batch
from tools.gpu_scheduler.client import SchedulerError
from tools.gpu_scheduler.common import JobWaitTimeout


class BatchTests(unittest.TestCase):
    def test_existing_request_is_waited_without_resubmission(self):
        client = Mock()
        row = {"id": "job", "state": "QUEUED", "request_id": "request"}
        client.wait.return_value = dict(row, state="SUCCEEDED")
        execute_one(client, {"request_id": "request"}, {"request": row})
        client.submit.assert_not_called()
        client.wait.assert_called_once_with("job", timeout=45)

    def test_terminal_request_is_queried_without_replay(self):
        client = Mock()
        row = {"id": "job", "state": "SUCCEEDED", "request_id": "request"}
        execute_one(client, {"request_id": "request"}, {"request": row})
        client.submit.assert_not_called()
        client.wait.assert_not_called()
        client.get.assert_called_once_with("job")

    def test_transport_error_is_not_retried(self):
        client = Mock()
        client.submit.side_effect = RuntimeError("outcome UNKNOWN")
        with self.assertRaises(RuntimeError):
            execute_one(client, {"request_id": "request"}, {})
        client.submit.assert_called_once()

    def test_disconnected_submit_queries_same_request_before_waiting(self):
        client = Mock()
        client.submit.side_effect = SchedulerError("SSH outcome UNKNOWN")
        row = {"id": "job", "state": "RUNNING", "request_id": "request"}
        client.get.return_value = row
        client.wait.return_value = dict(row, state="SUCCEEDED")
        result = execute_one(client, {"request_id": "request"}, {})
        self.assertEqual(result["state"], "SUCCEEDED")
        client.submit.assert_called_once()
        client.get.assert_called_once_with(request_id="request")
        client.wait.assert_called_once_with("job", timeout=45)

    def test_timeout_continues_same_job_without_submitting(self):
        client = Mock()
        row = {"id": "job", "state": "RUNNING", "request_id": "request"}
        client.wait.side_effect = [JobWaitTimeout("waiting", row), dict(row, state="SUCCEEDED")]
        result = execute_one(client, {"request_id": "request"}, {"request": row})
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(client.wait.call_count, 2)
        client.submit.assert_not_called()

    def test_disconnected_wait_recovers_without_replaying(self):
        client = Mock()
        row = {"id": "job", "state": "RUNNING", "request_id": "request"}
        client.wait.side_effect = SchedulerError("SSH outcome UNKNOWN")
        client.get.return_value = dict(row, state="SUCCEEDED")
        result = execute_one(client, {"request_id": "request"}, {"request": row})
        self.assertEqual(result["state"], "SUCCEEDED")
        client.submit.assert_not_called()
        client.get.assert_called_once_with(request_id="request")

    def test_cleanup_hook_finishes_before_next_admission(self):
        events = []
        run_batch([1, 2], 1, lambda spec: events.append(("run", spec)),
                  lambda spec, job: events.append(("cleanup", spec)), Mock())
        self.assertEqual(events, [("run", 1), ("cleanup", 1), ("run", 2), ("cleanup", 2)])

    def test_failed_cleanup_stops_new_admission(self):
        run = Mock(return_value={})
        failed = Mock()
        run_batch([1, 2, 3], 1, run, Mock(side_effect=RuntimeError("cleanup unknown")), failed)
        run.assert_called_once_with(1)
        failed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
