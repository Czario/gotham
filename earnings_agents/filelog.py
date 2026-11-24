"""Per-run log file mirroring the admin-panel event stream.

Every line the worker publishes to the admin panel (Redis ``sec:worker:events``
→ SSE) is ALSO appended to a dated log file under the project's ``Logs/``
directory — one file per run, named by the run's start date+time::

    Logs/2026-08-26_11-45-30.log

The file log is best-effort only: any failure to create/write the file is
logged at WARNING and never interrupts the pipeline.  Heartbeat events are
skipped, matching the admin panel which filters them from the visible log.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from earnings_agents import config

logger = logging.getLogger(__name__)

_HEARTBEAT_KIND = "heartbeat"


class RunLogFile:
    """Append-only log file capturing one pipeline run's event lines."""

    def __init__(
        self,
        ticker: str,
        load_request_id: str | None,
        log_dir: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._handle = None
        self._path: Path | None = None
        try:
            from datetime import timezone
            from shared.run_logger import build_run_log_path

            now_utc = datetime.now(timezone.utc)
            _serial, path = build_run_log_path(
                ticker,
                log_dir=log_dir or config.RUN_LOGS_DIR,
                now_utc=now_utc,
            )
            self._handle = open(path, "a", encoding="utf-8")
            self._path = path
            self.write(
                f"── run started {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}  "
                f"serial={_serial}  "
                f"ticker={ticker or '?'}  "
                f"load_request_id={load_request_id or '-'}"
            )
            logger.info("Run log file: %s", path)
        except Exception as exc:  # noqa: BLE001 — logging must never break the pipeline
            logger.warning("RunLogFile: could not open log file: %s", exc)

    def write(self, message: str, kind: str | None = None) -> None:
        """Append one event line (same text the admin panel displays)."""
        if kind == _HEARTBEAT_KIND:
            return
        if self._handle is None:
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            with self._lock:
                self._handle.write(f"{ts}  {message}\n")
                self._handle.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RunLogFile: write failed: %s", exc)

    def close(self, summary: str | None = None) -> None:
        """Write the final run line, embedding *summary* when given.

        *summary* (e.g. ``"✓ HIMS_2026_latest saved  (24 concepts)  (7 LLM
        calls)  33.3s"``) is appended to the ``── run ended`` line so the
        log file's LAST line carries the run's outcome, LLM-call count, and
        elapsed time — mirroring the CLI/UI summary line.
        """
        if self._handle is None:
            return
        try:
            with self._lock:
                line = f"── run ended {datetime.now().isoformat(timespec='seconds')}"
                if summary:
                    line += f"  {summary}"
                self._handle.write(f"{line}\n")
                self._handle.flush()
                self._handle.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._handle = None

    @property
    def path(self) -> str | None:
        return str(self._path) if self._path is not None else None
