"""Redis consumer: process 10-K / 10-Q filings published by admin_backend.

Listens to the **dedicated** ``sec:filings:10kq`` queue (set via
``REDIS_QUEUE_NAME`` env var, default ``sec:filings:10kq``).
admin_backend publishes 10-K/10-Q messages here and 8-K messages to a
separate queue consumed by the earning_scraping_agent worker.  Each worker
only ever sees its own messages — no re-queue loops.

Pipeline mirrors ``uv run main --url <filing_url>`` exactly:
  CLI    → main --url <url>  → parse CIK + accession → pipeline
  Worker → Redis message            → parse CIK + accession → same pipeline
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from time import perf_counter
from typing import Any

from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from redis_queue import decode_message, get_redis_client, normalize_queue_name
from sec_scraper_cli import ProgressManager, SECDataScraperApp, parse_sec_filing_url
from utilities.helpers.logger_config import LoggerConfig
from worker_progress import WorkerHeartbeat, WorkerProgressPublisher

# NOTE: logging is configured in main() AFTER all imports have run.
# sec_scraper_cli resets the root logger to WARNING+NullHandler at import time;
# any basicConfig() call here would be silently overridden.
logger = logging.getLogger(__name__)

_DEFAULT_QUEUE = "sec:filings:10kq"
_HANDLED_TYPES = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})


# ── Worker progress manager ──────────────────────────────────────────────────────────

class WorkerProgressManager(ProgressManager):
    """ProgressManager subclass that forwards every set_status / status / error
    call to Redis instead of tqdm.

    Installed on ``app.progress`` for the duration of each job so all
    ``self.progress.*`` calls inside the pipeline — including the new ones
    added to ``process_single_filing_from_url`` and
    ``process_financial_statements_enhanced`` — automatically publish live
    log lines to the UI.  The base ProgressManager methods only update tqdm
    bars so the CLI is completely unaffected.
    """

    def __init__(self, pub: WorkerProgressPublisher, ticker: str) -> None:
        super().__init__(verbose=False, parallel=False)
        self._pub = pub
        self._ticker = ticker

    def set_status(self, msg: str) -> None:  # type: ignore[override]
        """Publish a ► step_start event for each stage transition."""
        clean = msg.strip()
        if clean:
            self._pub.publish("step", f"► {clean}", kind="step_start")

    def status(self, msg: str) -> None:  # type: ignore[override]
        """Publish an info / step_end line."""
        clean = msg.strip()
        if clean:
            self._pub.publish("info", clean, kind="step_end")

    def error(self, msg: str) -> None:  # type: ignore[override]
        """Publish an error line."""
        clean = msg.strip()
        if clean:
            self._pub.publish("error", clean, kind="call")


# ── MongoDB helper ─────────────────────────────────────────────────────────────

def _update_load_request_status(
    payload: dict[str, Any],
    status: str,
    period_of_report: str | None = None,
) -> None:
    """Update StockLoadRequest.status (and optionally sec_period_of_report) in MongoDB."""
    load_request_id = payload.get("load_request_id")
    if not load_request_id:
        return
    try:
        uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        db_name = os.getenv("DATABASE_NAME", "normalize_data")
        fields: dict[str, Any] = {"status": status}
        if period_of_report:
            fields["sec_period_of_report"] = period_of_report
        with MongoClient(uri, serverSelectionTimeoutMS=5000) as mongo:
            mongo[db_name]["stock_load_requests"].update_one(
                {"_id": ObjectId(load_request_id)},
                {"$set": fields},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to update load_request status → %s: %s", status, exc)


# ── App factory ────────────────────────────────────────────────────────────────

def _build_app() -> SECDataScraperApp:
    """Build and initialise the SECDataScraperApp — mirrors CLI setup."""
    progress = ProgressManager(verbose=False, parallel=False)
    app = SECDataScraperApp(
        start_year=2010,
        end_year=None,
        enable_dimensions=True,
        target_fiscal_year=None,
        target_fiscal_quarter=None,
        reload=False,
        latest=False,
        html_download_path=os.getenv("SEC_HTML_DOWNLOAD_PATH"),
        enable_reconciliation=True,
        progress=progress,
        workers=1,
    )
    if not app.setup_database():
        raise RuntimeError("Failed to set up database")
    return app


# ── Core processing — mirrors CLI's --url path ─────────────────────────────────

def _process_payload(app: SECDataScraperApp, payload: dict[str, Any]) -> bool:
    """Process one 10-K/10-Q filing message using the same pipeline as the CLI.

    Mirrors exactly what ``uv run main --url <filing_url>`` does:
      1. Parse the filing URL to extract CIK and accession number.
      2. Call app.process_single_filing_from_url(cik, accession_number).

    Returns True on success, False on failure.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    load_request_id = payload.get("load_request_id")
    ticker = (payload.get("ticker") or "").upper()
    form_type = (payload.get("filing_type") or payload.get("form_type") or "10-K/10-Q").upper()

    filing_url = payload.get("filing_url")
    if not filing_url:
        logger.error("Queue message missing filing_url: %s", payload)
        return False

    filing_info = parse_sec_filing_url(str(filing_url))
    if not filing_info:
        logger.error("Could not parse SEC filing URL: %s", filing_url)
        return False

    cik = filing_info["cik"]
    accession_number = filing_info["accession_number"]
    label = payload.get("company_name") or ticker or cik

    pub = WorkerProgressPublisher(redis_url, ticker or cik, load_request_id)

    # Guard (mirrors 8-K worker): skip filings for companies that have no prior
    # data in normalize_data.  In "All Companies" polling mode the RSS feed
    # publishes every 10-K/10-Q in the DB — this prevents the worker from
    # blindly processing hundreds of filings for companies we haven't set up.
    # Only applies when there is no explicit load_request_id (unmatched entries);
    # explicit load requests always proceed regardless.
    if not load_request_id and ticker:
        from sec_scraper_cli import SECDataScraperApp as _App
        _cv_q = app._cv_quarterly_col if hasattr(app, '_cv_quarterly_col') else None
        _cv_a = app._cv_annual_col if hasattr(app, '_cv_annual_col') else None
        has_data = False
        for col in [c for c in [_cv_q, _cv_a] if c is not None]:
            if col.count_documents({"company_cik": cik}, limit=1):
                has_data = True
                break
        if not has_data:
            pub.publish("skip", f"skipped — no existing normalize_data for {ticker}", kind="skip")
            pub.close()
            logger.info("10-K/Q skipped for %s (CIK %s) — no prior normalize_data", ticker, cik)
            return True

    # Install a job-specific progress manager that forwards set_status/status
    # calls from inside the pipeline directly to Redis.  Restore the original
    # (silent tqdm) manager when the job finishes so the next job starts clean.
    original_pm = app.progress
    app.progress = WorkerProgressManager(pub, ticker or cik)

    pub.publish("start", f"► processing  {form_type}  {label}  ({accession_number})", kind="step_start")
    logger.info(
        "Processing %s filing for %s (CIK %s) accession %s",
        form_type, label, cik, accession_number,
    )

    t0 = perf_counter()
    try:
        with WorkerHeartbeat(pub, ticker or cik, interval_s=60):
            success = bool(app.process_single_filing_from_url(cik, accession_number))
    except BaseException as exc:
        elapsed_s = perf_counter() - t0
        elapsed_str = (
            f"{elapsed_s:.1f}s" if elapsed_s < 60
            else f"{int(elapsed_s // 60)}m {elapsed_s % 60:.0f}s"
        )
        reason = (
            "worker stopped (SIGTERM)"
            if isinstance(exc, SystemExit)
            else "interrupted"
            if isinstance(exc, KeyboardInterrupt)
            else str(exc)[:120]
        )
        pub.publish("summary", f"✗ {reason}  {elapsed_str}", kind="summary")
        app.progress = original_pm
        pub.close()
        raise
    finally:
        pass  # pub.close() called in success/failure paths below or in except above

    elapsed_s = perf_counter() - t0
    elapsed_str = (
        f"{elapsed_s:.1f}s" if elapsed_s < 60
        else f"{int(elapsed_s // 60)}m {elapsed_s % 60:.0f}s"
    )

    if success:
        period_label = (
            getattr(app, '_last_period_label', None)
            or f"FY {(getattr(app, '_last_report_date', None) or payload.get('period_of_report') or '')[:4] or '?'}"
        )
        summary = f"\u2713 {period_label} saved  ({form_type})  {elapsed_str}"
        pub.publish("summary", summary, kind="summary")
        logger.info("✅ Saved filing %s for %s  elapsed=%s", accession_number, label, elapsed_str)
    else:
        pub.publish("summary", f"✗ failed  {elapsed_str}", kind="summary")
        logger.warning("❌ Failed filing %s for %s", accession_number, label)

    app.progress = original_pm
    pub.close()
    return success


# ── CLI argument parsing ───────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sec-scraper-worker",
        description="Consume 10-K/10-Q filing jobs from Redis and run the extraction pipeline.",
    )
    parser.add_argument("--redis-url",
                        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--queue-name",
                        default=os.getenv("REDIS_QUEUE_NAME", _DEFAULT_QUEUE))
    parser.add_argument("--dead-letter-queue",
                        default=os.getenv("REDIS_DEAD_LETTER_QUEUE", "sec:filings:dlq:10kq"))
    parser.add_argument("--poll-timeout", type=int, default=5,
                        help="Seconds to block-wait for a Redis message.")
    parser.add_argument("--max-attempts", type=int,
                        default=int(os.getenv("REDIS_MAX_ATTEMPTS", "3")),
                        help="Retry limit before moving a job to the dead-letter queue.")
    parser.add_argument("--retry-delay", type=int,
                        default=int(os.getenv("REDIS_RETRY_DELAY_SECONDS", "5")),
                        help="Seconds to wait between retries.")
    parser.add_argument("--once", action="store_true",
                        help="Process one message then exit (useful for testing).")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable info-level logging.")
    return parser.parse_args(argv)


# ── Main loop ──────────────────────────────────────────────────────────────────

def _make_client(redis_url: str, poll_timeout: int) -> Redis:
    """Create a Redis client suitable for blocking blpop.

    socket_timeout must be None (no socket-level deadline) so the blocking
    BLPOP command can wait the full poll_timeout seconds without the socket
    layer raising TimeoutError.  socket_connect_timeout is kept short so a
    bad URL fails fast at startup.
    """
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=None,  # no socket-level timeout — blpop controls waiting
    )


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Re-apply logging AFTER all imports.  sec_scraper_cli resets the root
    # logger to WARNING+NullHandler at import time, so we must override it here.
    # Use INFO for our worker logger so startup/job/error lines are always
    # visible in Docker logs — matches the 8-K worker behaviour.
    # Suppress the noisy underlying CLI and normalisation loggers.
    root = logging.getLogger()
    root.handlers.clear()
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(_handler)
    root.setLevel(logging.WARNING)          # keep noisy libs quiet
    logging.getLogger("worker_10kq").setLevel(logging.INFO)   # our messages

    if args.verbose:
        # Verbose mode re-enables the underlying CLI loggers for deep debugging.
        for _noisy in ("sec_scraper_cli", "data_normalization_service"):
            logging.getLogger(_noisy).setLevel(logging.INFO)

    queue_name = normalize_queue_name(args.queue_name)
    dead_letter_queue = normalize_queue_name(args.dead_letter_queue)
    client: Redis = _make_client(args.redis_url, args.poll_timeout)

    app = _build_app()
    logger.info("sec-scraper-worker listening on Redis queue '%s'", queue_name)

    # Convert SIGTERM (docker stop / docker-compose down) into SystemExit so it
    # propagates through finally blocks and the BaseException handler in
    # _process_payload, letting us publish a failure event and mark MongoDB
    # as failed before the process exits.
    def _sigterm(_signum, _frame):  # noqa: ANN001
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        while True:
            try:
                item = client.blpop(queue_name, timeout=args.poll_timeout)
            except Exception as exc:
                logger.warning("Redis blpop error (%s) — reconnecting in 5s", exc)
                time.sleep(5)
                try:
                    client = _make_client(args.redis_url, args.poll_timeout)
                except Exception:
                    pass
                continue

            if not item:
                if args.once:
                    break
                continue

            _, raw_message = item
            try:
                payload = decode_message(raw_message)
            except (json.JSONDecodeError, ValueError):
                logger.error("Could not decode queue message: %r", raw_message)
                if args.once:
                    break
                continue

            form_type = (payload.get("filing_type") or payload.get("form_type") or "").upper()

            if form_type not in _HANDLED_TYPES:
                # Dedicated queue: only 10-K/10-Q messages should arrive here.
                # Log and skip anything unexpected without re-queuing.
                logger.warning("Unexpected form_type %r on 10-K/Q queue — skipping", form_type)
                if args.once:
                    break
                continue

            # ── Process the filing ─────────────────────────────────────────────
            attempts = int(payload.get("attempts") or 0)
            success = False
            _update_load_request_status(payload, "processing")

            try:
                success = _process_payload(app, payload)
            except (KeyboardInterrupt, SystemExit):
                # Worker is shutting down mid-job — _process_payload already
                # published the ✗ summary event; mark MongoDB failed and exit.
                _update_load_request_status(payload, "failed")
                logger.info(
                    "Worker shutdown mid-job — marked %s as failed",
                    payload.get("ticker"),
                )
                raise
            except Exception as exc:  # noqa: BLE001
                payload["last_error"] = str(exc)
                logger.exception("Unhandled error processing filing for %s", payload.get("ticker"))

            if success:
                _update_load_request_status(
                    payload,
                    "completed",
                    period_of_report=getattr(app, '_last_period_label', None)
                        or getattr(app, '_last_report_date', None),
                )
            else:
                payload["attempts"] = attempts + 1
                payload["failed_at"] = time.time()
                if payload["attempts"] < args.max_attempts:
                    logger.warning(
                        "Filing job failed; retrying attempt %d/%d for %s",
                        payload["attempts"], args.max_attempts, payload.get("ticker"),
                    )
                    time.sleep(max(0, args.retry_delay))
                    client.rpush(queue_name, json.dumps(payload, sort_keys=True, separators=(",", ":")))
                else:
                    logger.error(
                        "Filing job exhausted %d retries; moving to dead-letter queue '%s' for %s",
                        args.max_attempts, dead_letter_queue, payload.get("ticker"),
                    )
                    _update_load_request_status(payload, "failed")
                    client.rpush(dead_letter_queue, json.dumps(payload, sort_keys=True, separators=(",", ":")))

            if args.once:
                break

    finally:
        try:
            app.cleanup()
        except Exception:
            logger.exception("App cleanup failed")


if __name__ == "__main__":
    main()
