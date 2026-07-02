"""Redis consumer: process 10-K / 10-Q filings published by admin_backend.

Listens to the shared Redis queue, filters for 10-K and 10-Q messages,
and runs the **exact same pipeline** as:

    uv run sec-scraper --url <filing_url>

The only difference from the CLI is the trigger source:
  CLI    → sec-scraper --url <url>  → parse CIK + accession → pipeline
  Worker → Redis message            → parse CIK + accession → same pipeline

8-K messages are re-queued so the earning_scraping_agent worker can
consume them without losing any messages.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any

from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from redis_queue import decode_message, get_redis_client, normalize_queue_name
from sec_scraper_cli import ProgressManager, SECDataScraperApp, parse_sec_filing_url
from utilities.helpers.logger_config import LoggerConfig

logger = logging.getLogger(__name__)

# Form types handled by this worker. 8-K belongs to earning_scraping_agent.
_HANDLED_TYPES = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})


# ── MongoDB helper ─────────────────────────────────────────────────────────────

def _update_load_request_status(payload: dict[str, Any], status: str) -> None:
    """Update StockLoadRequest.status in MongoDB. Best-effort — never raises."""
    load_request_id = payload.get("load_request_id")
    if not load_request_id:
        return
    try:
        uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        db_name = os.getenv("DATABASE_NAME", "normalize_data")
        with MongoClient(uri, serverSelectionTimeoutMS=5000) as mongo:
            mongo[db_name]["stock_load_requests"].update_one(
                {"_id": ObjectId(load_request_id)},
                {"$set": {"status": status}},
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
        incremental=False,
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

    Mirrors exactly what ``uv run sec-scraper --url <filing_url>`` does:
      1. Parse the filing URL to extract CIK and accession number.
      2. Call app.process_single_filing_from_url(cik, accession_number).

    Returns True on success, False on failure.
    """
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
    label = payload.get("company_name") or payload.get("ticker") or cik

    logger.info(
        "Processing %s filing for %s (CIK %s) accession %s",
        (payload.get("filing_type") or payload.get("form_type") or "10-K/10-Q").upper(),
        label, cik, accession_number,
    )

    success = bool(app.process_single_filing_from_url(cik, accession_number))

    if success:
        logger.info("✅ Saved filing %s for %s", accession_number, label)
    else:
        logger.warning("❌ Failed filing %s for %s", accession_number, label)

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
                        default=os.getenv("REDIS_QUEUE_NAME", "sec:filings"))
    parser.add_argument("--dead-letter-queue",
                        default=os.getenv("REDIS_DEAD_LETTER_QUEUE", "sec:filings:dlq"))
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

def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.verbose:
        LoggerConfig.setup_logging(level="INFO", detailed=True, console_output=True)
    else:
        LoggerConfig.setup_logging(level="WARNING", console_output=True)

    queue_name = normalize_queue_name(args.queue_name)
    dead_letter_queue = normalize_queue_name(args.dead_letter_queue)
    client: Redis = get_redis_client(args.redis_url)

    app = _build_app()
    logger.info("sec-scraper-worker listening on Redis queue '%s'", queue_name)

    try:
        while True:
            item = client.blpop(queue_name, timeout=args.poll_timeout)
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

            # Route by form type — only handle 10-K/10-Q; re-queue 8-K for
            # the earning_scraping_agent worker so no messages are lost.
            form_type = (payload.get("filing_type") or payload.get("form_type") or "").upper()

            if form_type and form_type not in _HANDLED_TYPES:
                logger.debug("Re-queuing %s filing (handled by earning_scraping_agent)", form_type)
                client.rpush(queue_name, raw_message)
                if args.once:
                    break
                continue

            # ── Process the filing ─────────────────────────────────────────────
            attempts = int(payload.get("attempts") or 0)
            success = False
            _update_load_request_status(payload, "processing")

            try:
                success = _process_payload(app, payload)
            except Exception as exc:  # noqa: BLE001
                payload["last_error"] = str(exc)
                logger.exception("Unhandled error processing filing for %s", payload.get("ticker"))

            if success:
                _update_load_request_status(payload, "completed")
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
