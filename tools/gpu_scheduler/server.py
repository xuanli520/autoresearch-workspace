"""Local Unix-socket service; all queue mutations occur in one event loop."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import struct
import time
from pathlib import Path

from .common import (MAX_MESSAGE, JobWaitInterrupted, JobWaitTimeout, atomic_json,
                     check_storage, fields, validate_config, validate_wait_timeout)
from .resources import probe, simulated_probe
from .scheduler import Scheduler


MAX_HANDLERS = 64
MAX_WAITERS = 32


async def serve(raw, *, local_test=False):
    config = validate_config(raw, local_test=local_test)
    os.umask(0o077)
    root = Path(config["root"])
    socket_path = root / "scheduler.sock"
    if len(os.fsencode(socket_path)) >= 108:
        raise ValueError("root is too long for a Unix socket path")
    # Abstract socket: kernel-owned singleton, independent of root/socket names.
    # Executors inherit this FD so scheduler crashes cannot permit double booking.
    lock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lock_name = f"\0autoresearch-gpu-scheduler{'-test' if local_test else ''}-{os.getuid()}"
    try:
        lock.bind(lock_name)
    except OSError:
        lock.close()
        raise ValueError("a scheduler or orphan executor is still active for this host user") from None
    scheduler = None
    server = None
    socket_created = False
    interrupted = asyncio.Event()
    handlers = set()
    waiters = set()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise ValueError("root must belong to the service user and have mode 0700")
        if socket_path.exists():
            if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                raise ValueError("refusing to replace a non-socket scheduler.sock")
            socket_path.unlink()
        scheduler = Scheduler(config, lock.fileno())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, interrupted.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: None)

        async def dispatch(request):
            fields(request, {"op"}, {"session_id", "spec", "id", "request_id", "reason",
                                      "wait", "timeout"})
            op = request["op"]
            if op == "hello":
                return scheduler.status()
            if request.get("session_id") != scheduler.id:
                raise ValueError("session changed; inspect old receipts before submitting to a new session")
            if op == "submit":
                wait = request.get("wait", True)
                if type(wait) is not bool:
                    raise ValueError("wait must be a boolean")
                timeout = validate_wait_timeout(request.get("timeout"))
                job = scheduler.submit(request.get("spec"))
                return await scheduler.wait(job["id"], timeout=timeout) if wait else job
            if op == "wait":
                return await scheduler.wait(request.get("id"), timeout=request.get("timeout"))
            if op == "status":
                return scheduler.status()
            if op == "list":
                return [scheduler.view(j) for j in scheduler.jobs.values()]
            if op == "get":
                return scheduler.view(scheduler.get(request.get("id"), request.get("request_id")))
            if op == "cancel":
                reason = request.get("reason", "user_cancel")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                    raise ValueError("reason must be a nonempty string up to 1000 characters")
                return scheduler.cancel(request.get("id"), reason)
            if op == "stop":
                scheduler.begin_shutdown()
                return scheduler.status()
            raise ValueError("unknown operation")

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            pending = []
            try:
                if len(handlers) > MAX_HANDLERS:
                    raise ValueError("too many concurrent requests")
                peer = writer.get_extra_info("socket")
                _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
                line = await asyncio.wait_for(reader.readline(), 2)
                if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
                    raise ValueError("request must be one bounded JSON line")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                waiting = request.get("op") == "wait" or (
                    request.get("op") == "submit" and request.get("wait", True) is True)
                if waiting:
                    if len(waiters) >= MAX_WAITERS:
                        raise ValueError("too many concurrent blocking waits")
                    waiters.add(task)
                operation = asyncio.create_task(dispatch(request))
                disconnected = asyncio.create_task(reader.read(1))
                pending = [operation, disconnected]
                completed, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if operation not in completed:
                    return
                result = operation.result()
                reply = {"ok": True, "result": result}
            except JobWaitTimeout as exc:
                reply = {"ok": False, "code": "WAIT_TIMEOUT", "error": str(exc), "job": exc.job}
            except JobWaitInterrupted as exc:
                reply = {"ok": False, "code": "WAIT_INTERRUPTED", "error": str(exc),
                         "reason": exc.reason, "job": exc.job}
            except (ValueError, TypeError, KeyError, OSError, asyncio.TimeoutError) as exc:
                reply = {"ok": False, "error": str(exc)}
            finally:
                for pending_task in pending:
                    pending_task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                try:
                    if "reply" in locals():
                        payload = json.dumps(reply, ensure_ascii=False, allow_nan=False).encode() + b"\n"
                        if len(payload) > MAX_MESSAGE:
                            payload = b'{"ok":false,"error":"response too large; query individual jobs"}\n'
                        writer.write(payload)
                        await asyncio.wait_for(writer.drain(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(task)
                waiters.discard(task)

        server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=MAX_MESSAGE)
        socket_created = True
        os.chmod(socket_path, 0o600)
        atomic_json(root / "service.json", scheduler.status())
        print(json.dumps({"ready": True, **scheduler.status()}), flush=True)
        while not interrupted.is_set() and not scheduler.closing:
            snapshot, error = None, None
            try:
                snapshot = simulated_probe(config) if local_test else await asyncio.to_thread(probe)
            except Exception as exc:
                # Probe failure denies allocation; existing executors keep their
                # fixed budget. No stale snapshot is substituted as fresh data.
                error = f"{type(exc).__name__}: telemetry unavailable"
            scheduler.tick(snapshot, error)
            try:
                await asyncio.wait_for(interrupted.wait(), config["poll_seconds"])
            except asyncio.TimeoutError:
                pass
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        if scheduler is not None:
            try:
                scheduler.begin_shutdown()
            except (OSError, ValueError):
                # Storage failure must not prevent signalling our own executors.
                for job in scheduler.active():
                    if job["process"] is not None and job["process"].poll() is None:
                        job["process"].terminate()
            cleanup_deadline = time.monotonic() + 8
            while any(j["process"] is not None and j["process"].poll() is None for j in scheduler.active()):
                if time.monotonic() >= cleanup_deadline:
                    break
                try:
                    scheduler.reconcile()
                except (OSError, ValueError):
                    pass
                await asyncio.sleep(.05)
            try:
                scheduler.reconcile()
                check_storage(config, root)
                atomic_json(scheduler.directory / "service-exit.json", {
                    "at": time.time(), "active_count": len(scheduler.active()),
                    "cleanup_confirmed": not scheduler.active(),
                })
            except (OSError, ValueError):
                pass
        if handlers:
            await asyncio.gather(*list(handlers), return_exceptions=True)
        if socket_created:
            socket_path.unlink(missing_ok=True)
        lock.close()
