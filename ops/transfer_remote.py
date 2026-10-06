#!/usr/bin/env python3
"""Stream one file over SSH using the workspace askpass contract."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.gpu_monitor import monitor


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("remote_path")
    p.add_argument("--auth", type=Path, default=ROOT / "auth.txt")
    p.add_argument("--download", action="store_true", help="Read remote_path into a new local source file")
    args = p.parse_args()
    if args.download and args.source.exists():
        raise SystemExit(f"download destination already exists: {args.source}")
    if not args.download and not args.source.is_file():
        raise SystemExit(f"missing source: {args.source}")
    auth = monitor.load_auth(args.auth)
    host = monitor.apply_auth({"transport": "ssh", "connect_timeout_seconds": 20}, auth)
    remote_python = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
        "f=p.open('wb'); "
        "\nwhile True:\n b=sys.stdin.buffer.read(1024*1024); "
        "\n if not b: break\n f.write(b)\n"
        "f.close()"
    )
    if args.download:
        remote_python = (
            "import pathlib,sys,shutil; p=pathlib.Path(sys.argv[1]); "
            "assert p.is_file() and not p.is_symlink(); "
            "shutil.copyfileobj(p.open('rb'),sys.stdout.buffer,1024*1024)"
        )
    remote = shlex.join(["python3", "-c", remote_python, args.remote_path])
    command = monitor.ssh_command(host, remote, password_auth=True)
    password = host["password"]
    temp_dir = Path(tempfile.mkdtemp(prefix="autoresearch-transfer-"))
    fifo = temp_dir / "password.fifo"
    os.mkfifo(fifo, 0o600)
    fifo_fd = os.open(fifo, os.O_RDWR)
    env = os.environ.copy()
    env.update({"AUTORESEARCH_SSH_PASSWORD_FIFO": str(fifo), "SSH_ASKPASS": str(ROOT / "tools/gpu_monitor/askpass.py"), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":autoresearch-transfer"})
    try:
        os.write(fifo_fd, password.encode() + b"\n")
        if args.download:
            args.source.parent.mkdir(parents=True, exist_ok=True)
            fd, partial = tempfile.mkstemp(prefix=".download-", dir=args.source.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                            stderr=subprocess.PIPE, env=env)
                    _, err = proc.communicate()
                    output.flush()
                    os.fsync(output.fileno())
                if proc.returncode:
                    print(err.decode(errors="replace"), file=sys.stderr)
                    return proc.returncode
                # A concurrent download must not overwrite an existing result.
                os.link(partial, args.source)
                print(f"downloaded {args.source} ({args.source.stat().st_size} bytes)")
                return 0
            finally:
                if 'proc' in locals() and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                Path(partial).unlink(missing_ok=True)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        sent = 0
        with args.source.open("rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                sent += len(chunk)
                if sent % (256 * 1024 * 1024) < len(chunk):
                    print(f"transferred {sent / 1024**3:.2f} GiB", flush=True)
        proc.stdin.close()
        proc.stdin = None
        out, err = proc.communicate()
        if proc.returncode:
            print(err.decode(errors="replace"), file=sys.stderr)
            return proc.returncode
        print(f"completed {args.source} -> {args.remote_path} ({sent} bytes)")
        return 0
    finally:
        os.close(fifo_fd)
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
