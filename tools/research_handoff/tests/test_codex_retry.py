"""Offline upstream, proxy and pinned-Codex regression checks."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tools.research_handoff.providers import codex_retry
from tools.research_handoff.providers.codex_retry import ModelAccessError, RequestRetry, proxy_server
from tools.research_handoff.providers.codex_transport import transport_flags


def sse(*events):
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


SUCCESS = sse({"type": "response.completed", "response": {"id": "resp_test", "status": "completed", "output": []}})


@contextmanager
def upstream(responses, tls=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.path, body, self.headers.get("Authorization")))
            status, content_type, payload = responses[min(len(requests) - 1, len(responses) - 1)]
            if status is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("https" if tls else "http") + f"://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class RequestRetryTests(unittest.TestCase):
    def request(self, retry):
        return retry.request("/v1/responses", b'{"stream":true,"input":"private"}',
                             {"Authorization": "Bearer private-key"}, True)

    def test_every_http_error_retries_identical_request(self):
        for status in (300, 400, 401, 403, 404, 405, 408, 409, 422, 429, 500, 502, 503, 504):
            with self.subTest(status=status), upstream([(status, "application/json", b'{}'),
                                                      (200, "text/event-stream", SUCCESS)]) as (url, requests):
                events = []
                retry = RequestRetry(url, backoff=0, allow_http=True, on_event=events.append)
                self.assertEqual(self.request(retry)[0], SUCCESS)
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0], requests[1])
                self.assertEqual(events[0]["http_status"], status)
                self.assertNotIn("private", json.dumps(events))

    def test_exhaustion_is_initial_attempt_plus_eight(self):
        with upstream([(400, "application/json", b'{}')]) as (url, requests):
            events = []
            retry = RequestRetry(url, backoff=0, allow_http=True, on_event=events.append)
            with self.assertRaises(ModelAccessError):
                self.request(retry)
            self.assertEqual(len(requests), 9)
            self.assertEqual([event["attempt"] for event in events], list(range(1, 10)))
            self.assertEqual(len({event["request_id"] for event in events}), 1)

    def test_network_and_stream_errors_retry(self):
        failures = [(None, "", b""), (200, "application/json", b'{}'),
                    (200, "text/event-stream", b"data: bad-json\n\n"),
                    (200, "text/event-stream", b"data: \xff\n\n"),
                    (200, "text/event-stream", sse({"type": "response.created"})),
                    (200, "text/event-stream", sse({"type": "error"})),
                    (200, "text/event-stream", sse({"type": "response.failed"})),
                    (200, "text/event-stream", sse({"type": "response.incomplete"})),
                    (200, "text/event-stream", sse({"type": "response.completed"})),
                    (200, "text/event-stream", sse({"type": "response.output_item.done", "item": "invalid"})),
                    (200, "text/event-stream", sse({"type": "response.completed", "response": {"status": "failed"}}))]
        for failure in failures:
            with self.subTest(failure=failure), upstream([failure, (200, "text/event-stream", SUCCESS)]) as (url, requests):
                self.assertEqual(self.request(RequestRetry(url, backoff=0, allow_http=True))[0], SUCCESS)
                self.assertEqual(len(requests), 2)

    def test_nonstream_errors_retry(self):
        for payload in (b"not-json", b"[]", b'{}', b'{"error":{"message":"bad"}}', b'{"status":"incomplete"}'):
            with self.subTest(payload=payload), upstream([(200, "application/json", payload),
                                                       (200, "application/json", b'{"id":"ok"}')]) as (url, requests):
                retry = RequestRetry(url, backoff=0, allow_http=True)
                self.assertEqual(retry.request("/v1/responses/compact", b'{}', {}, False)[0], b'{"id":"ok"}')
                self.assertEqual(len(requests), 2)

    def test_partial_tool_events_are_not_replayed(self):
        partial = sse({"type": "response.output_item.done", "item": {"name": "BAD_TOOL"}}, {"type": "error"})
        with upstream([(200, "text/event-stream", partial), (200, "text/event-stream", SUCCESS)]) as (url, requests):
            retry = RequestRetry(url, backoff=0, allow_http=True)
            server = proxy_server(retry)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("POST", "/v1/responses", b'{"stream":true}')
                payload = client.getresponse().read()
                self.assertNotIn(b"BAD_TOOL", payload)
                self.assertEqual(payload.count(b"response.completed"), 1)
                self.assertEqual(len(requests), 2)
                client.close()
            finally:
                retry.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_completed_stream_does_not_wait_for_upstream_eof(self):
        response = Mock()
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [SUCCESS, AssertionError("must stop at response.completed")]
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(http.client, "HTTPConnection", return_value=connection):
            self.assertEqual(self.request(RequestRetry("http://localhost/v1", allow_http=True))[0], SUCCESS)

    def test_deadline_and_cancellation_prevent_additional_requests(self):
        for kind in ("cancelled", "deadline"):
            retry = RequestRetry("https://example.com/v1", backoff=0, deadline=time.monotonic() + 10)
            def fail(*_args):
                if kind == "cancelled":
                    retry.stop.set()
                else:
                    retry.deadline = time.monotonic() - 1
                raise ModelAccessError("http_error", 400)
            with self.subTest(kind=kind), patch.object(retry, "once", side_effect=fail) as once:
                with self.assertRaisesRegex(ModelAccessError, kind):
                    self.request(retry)
                self.assertEqual(once.call_count, 1)

    def test_production_endpoint_requires_https_and_bounded_retries(self):
        for url in ("http://example.com", "https://key@example.com", "https://example.com?key=secret"):
            with self.assertRaises(ValueError):
                RequestRetry(url)
        with self.assertRaises(ValueError):
            RequestRetry("https://example.com", retries=9)


@unittest.skipUnless(os.environ.get("CODEX_TRANSPORT_TEST_BINARY"), "set pinned Codex binary for offline integration")
class CodexRetryTests(unittest.TestCase):
    def test_original_turn_survives_400_and_exhaustion_is_bounded(self):
        with tempfile.TemporaryDirectory(prefix="codex-retry-test-") as tmp:
            cert, key = Path(tmp) / "ca.pem", Path(tmp) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            item = {"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
            response = {"id": "resp_offline", "object": "response", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            success = sse({"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
                          {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                           "content_index": 0, "delta": "OK"},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": response})
            for exhausted, separator in ((False, False), (False, True), (True, False)):
                replies = [(400, "application/json", b'{"error":{"type":"invalid_request_error","code":"model_not_found"}}')]
                if not exhausted:
                    replies.append((200, "text/event-stream", success))
                with self.subTest(exhausted=exhausted, separator=separator), upstream(replies, tls) as (url, requests):
                    home = Path(tmp) / f"home-{exhausted}-{separator}"
                    home.mkdir()
                    policy = home / "policy.json"
                    log = home / "attempts.jsonl"
                    policy.write_text(json.dumps({"base_url": url, "request_max_retries": 8, "stream_max_retries": 8,
                                                  "stream_idle_timeout_ms": 90000, "max_seconds": 100,
                                                  "log_path": str(log)}))
                    env = dict(os.environ, CODEX_HOME=str(home), OPENAI_API_KEY="offline-test-key", SSL_CERT_FILE=str(cert))
                    flags = shlex.split(transport_flags({"base_url": "https://wrong.invalid/v1"}))
                    command = ["python3", str(Path(codex_retry.__file__)), "--config", str(policy), "--binary",
                               os.environ["CODEX_TRANSPORT_TEST_BINARY"], "--", "exec", "--skip-git-repo-check",
                               "--json", "--model", "offline-test-model", *flags]
                    if separator:
                        command.append("--")
                    result = subprocess.run([*command, "Reply with OK."], cwd=tmp, env=env,
                                            capture_output=True, text=True, timeout=100)
                    self.assertEqual(result.returncode, 1 if exhausted else 0, result.stdout + result.stderr)
                    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                    self.assertEqual(sum(e["type"] == "thread.started" for e in events), 1, result.stdout)
                    self.assertEqual(sum(e["type"] == "turn.started" for e in events), 1, result.stdout)
                    self.assertEqual(sum(e["type"] == "turn.completed" for e in events), 0 if exhausted else 1, result.stdout)
                    self.assertEqual(len(requests), 9 if exhausted else 2, result.stdout + result.stderr)
                    self.assertTrue(all(request == requests[0] for request in requests))
                    self.assertEqual(len(log.read_text().splitlines()), len(requests))
                    self.assertNotIn("offline-test-key", log.read_text())


if __name__ == "__main__":
    unittest.main()
