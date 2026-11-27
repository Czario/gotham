"""Single-container supervisor that runs both Redis queue workers together.

Starts:
  1. sec-scraper-worker  (queue: REDIS_QUEUE_10KQ, default ``sec:filings:10kq``)
  2. earnings-8k-worker  (queue: REDIS_QUEUE_8K,   default ``sec:filings:8k``)

Passing ``--queue-name`` explicitly avoids collisions if ``REDIS_QUEUE_NAME`` is
set in the environment.  If either worker exits unexpectedly, the remaining
worker is terminated and this process exits non-zero so Docker can restart the
container cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _stop_all(procs: list[tuple[str, subprocess.Popen[bytes]]], timeout: float = 15.0) -> None:
    for _, proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + timeout
    for name, proc in procs:
        if proc.poll() is not None:
            continue
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            print(f"[supervisor] {name} did not exit in time; killing", flush=True)
            proc.kill()
            proc.wait()


def main() -> int:
    queue_10kq = os.getenv("REDIS_QUEUE_10KQ", "sec:filings:10kq")
    queue_8k = os.getenv("REDIS_QUEUE_8K", "sec:filings:8k")

    specs = [
        ("worker-10kq", [sys.executable, "-m", "workers.queue_worker", "--queue-name", queue_10kq]),
        ("worker-8k", [sys.executable, "-m", "earnings_agents.cli.worker", "--queue-name", queue_8k]),
    ]

    procs: list[tuple[str, subprocess.Popen[bytes]]] = []
    stopping = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        print(f"[supervisor] received signal {signum}; stopping workers...", flush=True)
        _stop_all(procs)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    for name, cmd in specs:
        print(f"[supervisor] starting {name}: {' '.join(cmd)}", flush=True)
        procs.append((name, subprocess.Popen(cmd)))

    try:
        while not stopping:
            for name, proc in procs:
                rc = proc.poll()
                if rc is not None:
                    if stopping:
                        return 0
                    print(
                        f"[supervisor] {name} exited unexpectedly with code {rc}; shutting down",
                        file=sys.stderr,
                        flush=True,
                    )
                    stopping = True
                    _stop_all(procs)
                    return rc if rc != 0 else 1
            time.sleep(0.5)
    finally:
        _stop_all(procs)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
