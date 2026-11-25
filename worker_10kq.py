"""Backward-compatible entry point for sec-scraper-worker.

Canonical implementation has moved to `workers.queue_worker`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from workers.queue_worker import main

if __name__ == "__main__":
    main()
