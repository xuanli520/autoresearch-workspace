"""Bounded retries of complete Responses requests without restarting Codex."""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid


MAX_BYTES = 32 * 1024 * 1024


class ModelAccessError(Exception):
    def __init__(self, kind, status=None):
        self.kind, self.status = kind, status
        super().__init__(kind)


class SSEValidator:
    def __init__(self):
        self.pending = b""
        self.data = []
        self.completed = False

    def feed(self, chunk):
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            try:
                line = line.rstrip(b"\r").decode("utf-8")
            except UnicodeError:
                raise ModelAccessError("invalid_utf8") from None
            if line.startswith("data:"):
                self.data.append(line[5:].lstrip(" "))
            elif not line and self.data:
                raw, self.data = "\n".join(self.data), []
                if raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    raise ModelAccessError("invalid_sse_json") from None
                if not isinstance(event, dict):
                    raise ModelAccessError("invalid_sse_event")
                response = event.get("response")
                if (event.get("type") in ("error", "response.failed", "response.incomplete")
                        or isinstance(response, dict) and (response.get("error")
                                                          or response.get("status") in ("failed", "incomplete"))):
                    raise ModelAccessError("response_error")
                kind = event.get("type")
                if not isinstance(kind, str):
                    raise ModelAccessError("invalid_sse_event")
                if kind in ("response.output_item.added", "response.output_item.done"):
                    item = event.get("item")
                    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                        raise ModelAccessError("invalid_output_item")
                if kind in ("response.output_text.delta", "response.function_call_arguments.delta") and not isinstance(event.get("delta"), str):
                    raise ModelAccessError("invalid_output_delta")
                if kind == "response.completed":
                    if (not isinstance(response, dict) or not isinstance(response.get("id"), str)
                            or not response["id"] or response.get("status") != "completed"
                            or not isinstance(response.get("output"), list)):
                        raise ModelAccessError("invalid_completed_response")
                self.completed |= event.get("type") == "response.completed"


def validate_response(payload, content_type, streaming):
    if not streaming:
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            raise ModelAccessError("invalid_json") from None
        if not isinstance(value, dict) or not value or value.get("error") or value.get("status") in ("failed", "incomplete"):
            raise ModelAccessError("response_error")
        return
    if "text/event-stream" not in content_type:
        raise ModelAccessError("invalid_content_type")
    validator = SSEValidator()
    validator.feed(payload + b"\n\n")
    if not validator.completed:
        raise ModelAccessError("incomplete_stream")


class RequestRetry:
    def __init__(self, base_url, *, retries=8, timeout=90, deadline=None, stop=None,
                 backoff=1, on_event=None, allow_http=False):
        self.url = urlsplit(base_url)
        if (self.url.scheme not in (("https", "http") if allow_http else ("https",))
                or not self.url.hostname or self.url.username or self.url.password or self.url.query or self.url.fragment):
            raise ValueError("retry endpoint must be a credential-free HTTPS URL")
        if type(retries) is not int or not 0 <= retries <= 8:
            raise ValueError("retry count must be an integer in 0..8")
        self.retries, self.timeout, self.deadline = retries, timeout, deadline
        self.stop = stop or threading.Event()
        self.backoff, self.on_event = backoff, on_event
        self.connections = {}
        self.lock = threading.Lock()

    def remaining(self, cancel=None):
        if self.stop.is_set() or cancel is not None and cancel.is_set():
            raise ModelAccessError("cancelled")
        remaining = self.deadline - time.monotonic() if self.deadline is not None else self.timeout
        if remaining <= 0:
            raise ModelAccessError("deadline")
        return remaining

    def close(self, cancel=None):
        if cancel is None:
            self.stop.set()
        else:
            cancel.set()
        with self.lock:
            connections = [connection for connection, owner in self.connections.items()
                           if cancel is None or owner is cancel]
        for connection in connections:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(2)
                except OSError:
                    pass
            connection.close()

    def once(self, path, body, headers, streaming, cancel=None):
        connect = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": min(self.timeout, self.remaining(cancel))}
        if self.url.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        forwarded = {key: value for key, value in headers.items()
                     if key.lower() not in ("host", "content-length", "connection", "transfer-encoding", "accept-encoding")}
        forwarded["Accept-Encoding"] = "identity"
        prefix = self.url.path.rstrip("/")
        if path not in (prefix + "/responses", prefix + "/responses/compact"):
            raise ModelAccessError("invalid_request_path")
        connection = None
        response = None
        try:
            connection = connect(self.url.hostname, self.url.port, **kwargs)
            with self.lock:
                self.connections[connection] = cancel
            self.remaining(cancel)
            connection.request("POST", path, body=body, headers=forwarded)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise ModelAccessError("http_error", response.status)
            content_type = response.getheader("Content-Type", "")
            if streaming and "text/event-stream" not in content_type:
                raise ModelAccessError("invalid_content_type")
            buffer = bytearray()
            validator = SSEValidator() if streaming else None
            while True:
                remaining = self.remaining(cancel)
                if connection.sock is not None:
                    connection.sock.settimeout(min(self.timeout, remaining))
                chunk = response.read1(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_BYTES:
                    raise ModelAccessError("response_too_large")
                if validator:
                    validator.feed(chunk)
                    if validator.completed:
                        break
            payload = bytes(buffer)
            validate_response(payload, content_type, streaming)
            self.remaining(cancel)
            return payload, content_type
        except (OSError, http.client.HTTPException) as exc:
            raise ModelAccessError("transport_error") from exc
        finally:
            if connection is not None:
                with self.lock:
                    self.connections.pop(connection, None)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def request(self, path, body, headers, streaming, cancel=None):
        request_id = uuid.uuid4().hex
        last_error = ModelAccessError("request_failed")
        for index in range(self.retries + 1):
            self.remaining(cancel)
            try:
                result = self.once(path, body, headers, streaming, cancel)
                self.event(request_id, index, "succeeded")
                return result
            except ModelAccessError as exc:
                last_error = exc
                self.event(request_id, index, "failed", exc)
                if exc.kind in ("cancelled", "deadline") or index == self.retries:
                    break
                until = time.monotonic() + min(self.backoff * 2 ** index, 10, self.remaining(cancel))
                while time.monotonic() < until:
                    self.remaining(cancel)
                    self.stop.wait(min(.1, until - time.monotonic()))
        raise last_error

    def event(self, request_id, index, state, error=None):
        if self.on_event:
            self.on_event({"event": "model.request_attempt", "at_epoch": time.time(), "request_id": request_id,
                           "attempt": index + 1, "max_attempts": self.retries + 1, "state": state,
                           "error_kind": error.kind if error else None, "http_status": error.status if error else None})


def proxy_server(retry):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    raise ValueError
                body = self.rfile.read(length)
                streaming = json.loads(body).get("stream") is True
            except (ValueError, AttributeError):
                self.send_error(400)
                return
            future = concurrent.futures.Future()
            cancel = threading.Event()
            def request():
                try:
                    future.set_result(retry.request(self.path, body, dict(self.headers), streaming, cancel))
                except Exception as exc:
                    future.set_exception(exc)
            threading.Thread(target=request, daemon=True).start()
            try:
                if streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while not future.done():
                        try:
                            future.result(timeout=min(5, retry.timeout / 2))
                        except concurrent.futures.TimeoutError:
                            self.wfile.write(b": upstream request pending\n\n")
                            self.wfile.flush()
                payload, content_type = future.result()
                if not streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", content_type or "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
            except ModelAccessError as exc:
                message = "model access failed after bounded retries: " + exc.kind
                try:
                    if streaming:
                        event = {"type": "error", "code": "invalid_request_error", "message": message}
                        self.wfile.write(("event: error\ndata: " + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                    else:
                        payload = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                # A proxy implementation failure must still surface as a
                # model-access error to Codex, without exposing internals.
                try:
                    if streaming:
                        self.wfile.write(b'event: error\ndata: {"type":"error","code":"invalid_request_error"}\n\n')
                        self.wfile.flush()
                    else:
                        self.send_error(502)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                retry.close(cancel)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    child_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    config = json.loads(args.config.read_text())
    if not any(arg in ("exec", "e", "resume") for arg in child_args):
        return subprocess.call([args.binary, *child_args])
    stop = threading.Event()
    def record(event):
        path = Path(config["log_path"])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
    retry = RequestRetry(config["base_url"], retries=max(config["request_max_retries"], config["stream_max_retries"]),
                         timeout=config["stream_idle_timeout_ms"] / 1000,
                         deadline=time.monotonic() + config["max_seconds"], stop=stop, on_event=record)
    server = proxy_server(retry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = {"name": "Bounded model retry", "base_url": f"http://127.0.0.1:{server.server_port}" + retry.url.path.rstrip("/"),
                "env_key": "OPENAI_API_KEY", "wire_api": "responses", "supports_websockets": False,
                "request_max_retries": 0, "stream_max_retries": 0,
                "stream_idle_timeout_ms": config["stream_idle_timeout_ms"]}
    table = "{" + ",".join(key + "=" + json.dumps(value) for key, value in provider.items()) + "}"
    # Last CLI overrides win. One layer owns retries; Codex keeps its native session.
    overrides = ["-c", 'model_provider="research_https"', "-c", "model_providers.research_https=" + table,
                 "--disable", "unbounded_connection_retries"]
    if "--" in child_args:
        index = child_args.index("--")
        child_args[index:index] = overrides
    else:
        child_args.extend(overrides)
    process = None
    def interrupted(signum, _frame):
        retry.close()
        if process is not None and process.poll() is None:
            process.send_signal(signum)
    old = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        process = subprocess.Popen([args.binary, *child_args])
        while process.poll() is None:
            if time.monotonic() >= retry.deadline:
                interrupted(signal.SIGTERM, None)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(.1)
        return process.wait()
    finally:
        retry.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for sig, handler in old.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
