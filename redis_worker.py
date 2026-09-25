"""Backward-compatible entry point for redis worker.

Canonical implementation has moved to `workers.queue_worker`.
"""
from __future__ import annotations

from workers.queue_worker import main

if __name__ == "__main__":
    main()
