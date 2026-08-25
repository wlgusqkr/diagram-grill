#!/usr/bin/python3
"""Minimal credential-free PID 1 for an evaluation container."""

from __future__ import annotations

import os
import secrets
import signal
import sys
from pathlib import Path


SENTINEL_PATH = "/tmp/outer-parent-sentinel"
SENTINEL_ENV = "DIAGRAM_GRILL_OUTER_PARENT_SENTINEL"
BOOTSTRAPPED_ENV = "DIAGRAM_GRILL_SUPERVISOR_BOOTSTRAPPED"
MAX_LIFETIME_ENV = "DIAGRAM_GRILL_MAX_LIFETIME_SECONDS"
MAX_LIFETIME_SECONDS = 1_800
_running = True


def _reap_children(_signum: int, _frame: object) -> None:
    while True:
        try:
            child, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if child == 0:
            return


def _stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False


def _write_parent_sentinel(sentinel: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(SENTINEL_PATH, flags, 0o400)
    try:
        written = os.write(descriptor, sentinel)
        if written != len(sentinel):
            raise RuntimeError("short sentinel write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    if os.environ.get(BOOTSTRAPPED_ENV) != "1":
        sentinel = secrets.token_hex(32)
        _write_parent_sentinel(sentinel.encode("ascii"))
        environment = dict(os.environ)
        environment[SENTINEL_ENV] = sentinel
        environment[BOOTSTRAPPED_ENV] = "1"
        os.execve(
            sys.executable,
            [sys.executable, "-I", "-S", str(Path(__file__).resolve(strict=True))],
            environment,
        )
    signal.signal(signal.SIGCHLD, _reap_children)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGHUP, _stop)
    signal.signal(signal.SIGALRM, _stop)
    raw_lifetime = os.environ.get(MAX_LIFETIME_ENV, "")
    if not raw_lifetime.isascii() or not raw_lifetime.isdecimal():
        raise RuntimeError("container lifetime is not a positive decimal")
    lifetime = int(raw_lifetime)
    if not 1 <= lifetime <= MAX_LIFETIME_SECONDS:
        raise RuntimeError("container lifetime is outside its frozen bound")
    signal.alarm(lifetime)
    while _running:
        signal.pause()
    signal.alarm(0)
    _reap_children(signal.SIGCHLD, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
