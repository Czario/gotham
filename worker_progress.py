"""Shared real-time progress publishing for the 10-K/10-Q worker process.

Publishes events to the Redis ``sec:worker:events`` pub/sub channel so
admin_backend can stream them to the frontend via SSE — the same channel used
by the earning_scraping_agent 8-K worker, giving a unified live log in the UI.

Event ``kind`` values (match the 8-K worker convention):

  ``step_start``  — step begins:   "► fetching company data"
  ``step_end``    — step result:   "[fetch]  General Mills Inc  (10-K)"
  ``summary``     — final line:    "✓ 0000040704_2026 saved  (3m 12s)"
  ``skip``        — non-fatal skip
  ``heartbeat``   — alive ping (filtered from visible log in UI)

Usage::

    from worker_progress import WorkerProgressPublisher, WorkerHeartbeat

    pub = WorkerProgressPublisher(redis_url, ticker, load_request_id)
    pub.publish("step", "► fetching company data", kind="step_start")
    with WorkerHeartbeat(pub, ticker):
        success = app.process_single_filing_from_url(cik, accession)
    pub.publish("summary", "✓ saved  (45.2s)", kind="summary")
    pub.close()
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from redis import Redis

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "sec:worker:events"


class WorkerProgressPublisher:
    """Publishes pipeline progress to Redis pub/sub.

    Uses a dedicated Redis connection so publish calls never interfere with
    the queue consumer connection.  All errors are swallowed so a Redis hiccup
    never kills the pipeline.
    """

    def __init__(
        self,
        redis_url: str,
        ticker: str,
        load_request_id: str | None,
    ) -> None:
        self._ticker = ticker
        self._load_request_id = load_request_id
        self._client: Redis | None = None
        try:
            self._client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("WorkerProgressPublisher: Redis connect failed: %s", exc)

    def publish(
        self,
        node_name: str,
        message: str,
        elapsed_ms: float | None = None,
        kind: str = "step_end",
    ) -> None:
        """Publish one log line to ``sec:worker:events``."""
        if not self._client:
            return
        event: dict[str, Any] = {
            "event":           "worker_progress",
            "ticker":          self._ticker,
            "load_request_id": self._load_request_id,
            "node":            node_name,
            "message":         message,
            "kind":            kind,
            "elapsed_ms":      round(elapsed_ms, 1) if elapsed_ms is not None else None,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._client.publish(EVENTS_CHANNEL, json.dumps(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning("WorkerProgressPublisher: publish failed: %s", exc)

    def close(self) -> None:
        try:
            if self._client:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass


class WorkerHeartbeat:
    """Context manager that publishes a ``kind='heartbeat'`` event every
    *interval_s* seconds while the pipeline is running.

    The frontend uses these to distinguish a slow-but-alive XBRL parse from a
    crashed worker: as long as heartbeats arrive, ``lastEvt.timestamp`` stays
    fresh and the "Stalled" badge never fires.  Heartbeat events are filtered
    from the visible log in the UI.

    Usage::

        with WorkerHeartbeat(pub, ticker, interval_s=60):
            success = app.process_single_filing_from_url(cik, accession)
    """

    def __init__(
        self,
        pub: WorkerProgressPublisher,
        ticker: str,
        interval_s: int = 60,
    ) -> None:
        self._pub = pub
        self._ticker = ticker
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        elapsed = 0
        while not self._stop.wait(timeout=self._interval_s):
            elapsed += self._interval_s
            self._pub.publish(
                "heartbeat",
                f"\u2665 alive  ({elapsed}s)",
                kind="heartbeat",
            )

    def __enter__(self) -> "WorkerHeartbeat":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
