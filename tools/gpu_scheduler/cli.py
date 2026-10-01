#!/usr/bin/env python3
"""Run with python3 -B tools/gpu_scheduler/cli.py --help."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import socket
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "tools.gpu_scheduler"

from .client import Client, SchedulerError
from .common import (MAX_MESSAGE, JobWaitInterrupted, JobWaitTimeout, read_json,
                     validate_config, validate_wait_timeout)
from .remote import RemoteClient
from .server import serve


def main():
    parser = argparse.ArgumentParser(description="单机 GPU 内存队列；全局并发上限 2")
    commands = parser.add_subparsers(dest="command", required=True)
    rpc = commands.add_parser("rpc", help="SSH 桥接：从 stdin 接收一个请求并转发本机 socket")
    rpc.add_argument("--root", required=True)
    for op in ("serve", "validate"):
        command = commands.add_parser(op)
        command.add_argument("--config", required=True)
        command.add_argument("--local-test", action="store_true", help="仅本地 CPU 测试：模拟 GPU 并跳过数据盘校验")
    for op in ("submit", "enqueue", "status", "list", "get", "wait", "cancel", "stop"):
        command = commands.add_parser(op, help={
            "submit": "校验、入队并阻塞等待终态或关键中断；默认使用此入口",
            "enqueue": "显式异步入队；仅用于持续跟踪进度或同时编排多个任务",
            "wait": "恢复等待已接受的作业；等待超时不会取消作业",
        }.get(op))
        connection = command.add_mutually_exclusive_group(required=True)
        connection.add_argument("--root")
        connection.add_argument("--remote", help="本地 SSH 连接配置；连接云端唯一服务")
        command.add_argument("--session", help="绑定先前响应的 session_id，拒绝跨重启重放")
        if op in ("submit", "enqueue"):
            command.add_argument("--spec", required=True)
        if op == "get":
            identity = command.add_mutually_exclusive_group(required=True)
            identity.add_argument("--id")
            identity.add_argument("--request-id")
        if op in ("wait", "cancel"):
            command.add_argument("--id", required=True)
        if op == "cancel":
            command.add_argument("--reason", required=True)
        if op == "wait":
            command.add_argument("--timeout", type=float)
        if op == "submit":
            command.add_argument("--timeout", type=float,
                                 help="阻塞等待上限；超时不会取消已接受的作业")
    args = parser.parse_args()
    if args.command in ("submit", "wait"):
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "rpc":
            payload = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
            if len(payload) > MAX_MESSAGE or not payload.endswith(b"\n"):
                raise ValueError("RPC requires one bounded JSON request line")
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise ValueError("RPC requires a JSON object")
            wait = request.get("op") == "wait" or (
                request.get("op") == "submit" and request.get("wait", True) is True)
            wait_timeout = validate_wait_timeout(request.get("timeout")) if wait else None
            response_timeout = 43210 if wait and wait_timeout is None else (
                max(10, wait_timeout + 5) if wait else 10)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(str(Path(args.root).resolve() / "scheduler.sock"))
                connection.sendall(payload)
                connection.settimeout(response_timeout)
                with connection.makefile("rb") as stream:
                    reply = stream.readline(MAX_MESSAGE + 1)
            if len(reply) > MAX_MESSAGE or not reply.endswith(b"\n"):
                raise ValueError("invalid scheduler response; outcome UNKNOWN")
            sys.stdout.buffer.write(reply)
            return 0
        elif args.command in ("serve", "validate"):
            raw = read_json(args.config)
            if args.command == "serve":
                asyncio.run(serve(raw, local_test=args.local_test))
                return 0
            result = validate_config(raw, local_test=args.local_test)
        else:
            client = RemoteClient(args.remote, session_id=args.session) if args.remote else Client(args.root, session_id=args.session)
            if args.command == "submit":
                result = client.submit(read_json(args.spec), timeout=args.timeout)
            elif args.command == "enqueue":
                result = client.submit_async(read_json(args.spec))
            elif args.command == "status":
                result = client.status()
            elif args.command == "list":
                result = client.jobs()
            elif args.command == "get":
                result = client.get(args.id, request_id=args.request_id)
            elif args.command == "wait":
                result = client.wait(args.id, timeout=args.timeout)
            elif args.command == "cancel":
                result = client.cancel(args.id, args.reason)
            else:
                result = client.stop()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        if args.command in ("submit", "wait") and result["state"] != "SUCCEEDED":
            return 1
        return 0
    except JobWaitTimeout as exc:
        print(json.dumps({"error": str(exc), "code": "WAIT_TIMEOUT", "job": exc.job,
                          "resumable": True},
                          ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 3
    except JobWaitInterrupted as exc:
        print(json.dumps({"error": str(exc), "code": "WAIT_INTERRUPTED", "reason": exc.reason,
                          "job": exc.job, "session_id": exc.session_id, "id": exc.job_id,
                          "request_id": exc.request_id, "resumable": True},
                          ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 3
    except (ValueError, OSError, SchedulerError, TimeoutError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
