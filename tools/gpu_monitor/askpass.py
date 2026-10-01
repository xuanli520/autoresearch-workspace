#!/usr/bin/env python3
"""OpenSSH askpass helper.

OpenSSH execs this helper after ``closefrom(STDERR_FILENO + 1)`` (see its
readpass.c), so an inherited descriptor cannot carry the secret. The monitor
instead creates a mode-0600 FIFO and passes its path in the environment; this
helper re-opens that path by name. No regular file is written.
"""
import os
import sys


def main():
    path = os.environ.get('AUTORESEARCH_SSH_PASSWORD_FIFO')
    if not path:
        return 1
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return 1
    try:
        secret = os.read(fd, 4096).rstrip(b'\r\n')
    except OSError:
        return 1
    finally:
        os.close(fd)
    if not secret:
        return 1
    sys.stdout.buffer.write(secret + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
