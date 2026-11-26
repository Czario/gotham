"""Unified Redis consumer for annual and quarterly XBRL filing jobs.

Canonical worker implementation for sec-scraper queue processing.
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

from db.redis.queue import decode_message, normalize_queue_name
from sec_scraper_cli import ProgressManager, SECDataScraperApp, parse_sec_filing_url
from worker_progress import WorkerHeartbeat, WorkerProgressPublisher

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE = "sec:filings:10kq"
_HANDLED_TYPES = frozenset({
    "10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A",
    "10-K/10-Q", "10K-10Q",
})


def _target_forms_for_payload(form_type: str) -> list[str]:
    ft = (form_type or "").strip().upper()
    if ft in {"10-K/10-Q", "10K-10Q"}:
        return ["10-K", "10-Q"]
    if ft in {"20-F", "20-F/A"}:
        return ["20-F", "20-F/A"]
    if ft in {"10-K", "10-K/A"}:
        return ["10-K", "10-K/A"]
    if ft in {"10-Q", "10-Q/A"}:
        return ["10-Q", "10-Q/A"]
    return ["10-K", "10-Q", "20-F", "20-F/A"]


def _resolve_cik_for_ticker(ticker: str) -> str | None:
    if not ticker:
        return None
    try:
        from utilities.helpers.ticker_resolver import get_ticker_to_cik
        mapped = {k.upper(): v for k, v in get_ticker_to_cik().items()}.get(ticker.upper())
        if mapped:
            return str(mapped).zfill(10)
    except Exception:
        pass
    try:
        uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        db_name = os.getenv("DATABASE_NAME", "normalize_data")
        with MongoClient(uri, serverSelectionTimeoutMS=5000) as mongo:
            doc = mongo[db_name]["companies"].find_one(
                {"ticker_symbol": ticker.upper()}, {"cik": 1}
            )
            if doc and doc.get("cik"):
                return str(doc["cik"]).zfill(10)
    except Exception:
        pass
    return None


class WorkerProgressManager(ProgressManager):
    """ProgressManager subclass that forwards every set_status / status / error
    call to Redis instead of tqdm.
    """

    def __init__(self, pub: WorkerProgressPublisher, ticker: str) -> None:
        super().__init__(verbose=False, parallel=False)
        self._pub = pub
        self._ticker = ticker

    def set_status(self, msg: str) -> None:  # type: ignore[override]
        clean = msg.strip()
        if clean:
            self._pub.publish("step", f"► {clean}", kind="step_start")

    def status(self, msg: str) -> None:  # type: ignore[override]
        clean = msg.strip()
        if clean:
            self._pub.publish("info", clean, kind="step_end")

    def error(self, msg: str) -> None:  # type: ignore[override]
        clean = msg.strip()
        if clean:
            self._pub.publish("error", clean, kind="call")


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


def _build_app() -> SECDataScraperApp:
    """Build and initialise the SECDataScraperApp."""
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
    )
    if not app.setup_database():
        raise RuntimeError("Failed to set up database")
    return app


def _process_payload(app: SECDataScraperApp, payload: dict[str, Any]) -> bool:
    """Process one XBRL filing message (10-K, 10-Q, 20-F, 10-K/10-Q, amendments)."""
    from datetime import datetime, timezone
    from pathlib import Path
    from shared.run_logger import RunLogSession
    from shared.run_report import (
        build_run_report,
        diff_stage,
        format_run_report_text,
        get_normalize_db,
        save_run_report,
        snapshot_company_state,
    )

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    load_request_id = payload.get("load_request_id")
    ticker = (payload.get("ticker") or "").upper()
    form_type = (payload.get("filing_type") or payload.get("form_type") or "10-K/10-Q").upper()

    # Resolve (cik, accession_number) from filing_url, filing_href, or explicit payload fields
    cik: str | None = None
    accession_number: str | None = None
    raw_url = payload.get("filing_url") or payload.get("filing_href")
    if raw_url:
        filing_info = parse_sec_filing_url(str(raw_url))
        if filing_info:
            cik = filing_info["cik"]
            accession_number = filing_info["accession_number"]

    if not cik and payload.get("cik"):
        digits = "".join(ch for ch in str(payload["cik"]) if ch.isdigit())
        if digits:
            cik = digits.zfill(10)
    if not cik and ticker:
        cik = _resolve_cik_for_ticker(ticker)
    if not accession_number and payload.get("accession_number"):
        accession_number = str(payload["accession_number"]).strip() or None

    if not cik:
        logger.error("Queue message could not resolve CIK (ticker=%r, url=%r): %s", ticker, raw_url, payload)
        return False

    label = payload.get("company_name") or ticker or cik
    pub = WorkerProgressPublisher(redis_url, ticker or cik, load_request_id)

    if not load_request_id and ticker:
        _cv_q = getattr(app, "_cv_quarterly_col", None)
        _cv_a = getattr(app, "_cv_annual_col", None)
        has_data = False
        for col in [c for c in [_cv_q, _cv_a] if c is not None]:
            if col.count_documents({"cik": cik}, limit=1):
                has_data = True
                break
        if not has_data:
            pub.publish("skip", f"skipped — no existing normalize_data for {ticker}", kind="skip")
            pub.close()
            logger.info("10-K/Q skipped for %s (CIK %s) — no prior normalize_data", ticker, cik)
            return True

    original_pm = app.progress
    original_reload = getattr(app, "reload", False)
    original_replace = getattr(app, "replace", False)
    original_latest = getattr(app, "latest", False)
    original_target_forms = getattr(app, "target_forms", None)
    original_fy = getattr(app, "target_fiscal_year", None)
    original_fq = getattr(app, "target_fiscal_quarter", None)
    original_no_new_concept = getattr(app, "no_new_concept", False)
    original_target_statement = getattr(app, "target_statement", None)

    app.progress = WorkerProgressManager(pub, ticker or cik)
    # Respect explicit write mode:
    # - default: resume mode (skip filings already stored in MongoDB)
    # - upsert:  reprocess & update differing values / fill gaps non-destructively
    # - replace: delete all existing values for target period first, then insert fresh
    mode = str(payload.get("mode") or "default").strip().lower()
    app.replace = bool(payload.get("replace")) or mode == "replace"
    app.reload = bool(payload.get("upsert")) or app.replace or mode == "upsert"
    app.no_new_concept = bool(payload.get("no_new_concept"))
    raw_stmt = payload.get("statement") or payload.get("target_statement")
    if raw_stmt and str(raw_stmt).strip():
        from shared.cli_flags import canonicalize_statement_type
        app.target_statement = canonicalize_statement_type(str(raw_stmt).strip())
    else:
        app.target_statement = None
    app.target_forms = _target_forms_for_payload(form_type)
    raw_fy = payload.get("fiscal_year")
    app.target_fiscal_year = (
        int(raw_fy) if raw_fy is not None and str(raw_fy).strip().isdigit() else None
    )
    raw_q = payload.get("quarter")
    if raw_q is not None and str(raw_q).strip():
        q_clean = str(raw_q).strip().upper()
        app.target_fiscal_quarter = q_clean if q_clean.startswith("Q") else f"Q{q_clean}"
    else:
        app.target_fiscal_quarter = None

    started_utc = datetime.now(timezone.utc)
    logs_dir = Path(os.getenv("RUN_LOGS_DIR") or (Path(__file__).resolve().parent.parent / "logs"))
    run_log = RunLogSession(ticker or cik, log_dir=logs_dir)
    run_log.attach()

    try:
        report_db = get_normalize_db()
    except Exception:
        report_db = None
    snap_before = snapshot_company_state(
        report_db, cik,
        fiscal_year=app.target_fiscal_year,
        quarter=app.target_fiscal_quarter,
    )

    acc_desc = accession_number or "latest"
    pub.publish("start", f"► processing  {form_type}  {label}  ({acc_desc})", kind="step_start")
    logger.info(
        "Processing %s filing for %s (CIK %s) accession=%s mode=%s no_new_concept=%s statement=%s",
        form_type, label, cik, acc_desc, mode, app.no_new_concept, app.target_statement,
    )

    t0 = perf_counter()
    try:
        with WorkerHeartbeat(pub, ticker or cik, interval_s=60):
            if accession_number:
                success = bool(app.process_single_filing_from_url(cik, accession_number))
            else:
                # Manual trigger with no accession/URL: fetch latest matching XBRL filing
                app.latest = not bool(app.target_fiscal_year)
                res_map = app.process_multiple_companies([cik], resume=True)
                entry = res_map.get(cik) if isinstance(res_map, dict) else None
                success = (
                    bool(entry.get("success", False)) and not entry.get("failed", 0)
                    if isinstance(entry, dict)
                    else bool(entry)
                )
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
        app.reload = original_reload
        app.replace = original_replace
        app.latest = original_latest
        app.target_forms = original_target_forms
        app.target_fiscal_year = original_fy
        app.target_fiscal_quarter = original_fq
        app.no_new_concept = original_no_new_concept
        app.target_statement = original_target_statement
        pub.close()
        run_log.close()
        raise

    elapsed_s = perf_counter() - t0
    elapsed_str = (
        f"{elapsed_s:.1f}s" if elapsed_s < 60
        else f"{int(elapsed_s // 60)}m {elapsed_s % 60:.0f}s"
    )

    # Persist structured run summary into normalize_data.gotham_run_reports
    try:
        snap_after = snapshot_company_state(
            report_db, cik,
            fiscal_year=app.target_fiscal_year,
            quarter=app.target_fiscal_quarter,
        )
        stage = diff_stage(
            snap_before, snap_after,
            stage_name=form_type,
            stage_forms=app.target_forms or [form_type],
            duration_seconds=elapsed_s,
        )
        rep = build_run_report(
            serial_number=run_log.serial_number,
            ticker=ticker or cik,
            cik=cik,
            forms_requested=form_type,
            mode=mode,
            log_file=str(run_log.path),
            started_at_utc=started_utc,
            finished_at_utc=datetime.now(timezone.utc),
            stages=[stage],
            target_fiscal_year=app.target_fiscal_year,
            target_quarter=app.target_fiscal_quarter,
            status="success" if success else "failed",
        )
        save_run_report(report_db, rep)
        run_log.write_raw(format_run_report_text(rep))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record gotham_run_reports for %s: %s", label, exc)

    if success:
        # Best-effort continuous profile update after official XBRL filing saves
        try:
            from shared.company_profile import refresh_company_profile
            refresh_company_profile(cik, mode="default")
        except Exception as _prof_exc:  # noqa: BLE001
            logger.debug("Post-save profile refresh skipped for %s: %s", label, _prof_exc)

        period_label = (
            getattr(app, "_last_period_label", None)
            or f"FY {(getattr(app, '_last_report_date', None) or payload.get('period_of_report') or '')[:4] or '?'}"
        )
        summary = f"\u2713 {period_label} saved  ({form_type})  {elapsed_str}"
        pub.publish("summary", summary, kind="summary")
        logger.info("✅ Saved filing %s for %s  elapsed=%s", acc_desc, label, elapsed_str)
    else:
        pub.publish("summary", f"✗ failed  {elapsed_str}", kind="summary")
        logger.warning("❌ Failed filing %s for %s", acc_desc, label)

    app.progress = original_pm
    app.reload = original_reload
    app.replace = original_replace
    app.latest = original_latest
    app.target_forms = original_target_forms
    app.target_fiscal_year = original_fy
    app.target_fiscal_quarter = original_fq
    app.no_new_concept = original_no_new_concept
    app.target_statement = original_target_statement
    pub.close()
    run_log.close()
    return success


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sec-scraper-worker",
        description="Consume annual/quarterly filing jobs from Redis and run the XBRL pipeline.",
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
                        default=min(3, max(1, int(os.getenv("REDIS_MAX_ATTEMPTS", "3")))),
                        help="Retry limit before moving a job to the dead-letter queue (max 3).")
    parser.add_argument("--retry-delay", type=int,
                        default=int(os.getenv("REDIS_RETRY_DELAY_SECONDS", "5")),
                        help="Seconds to wait between retries.")
    parser.add_argument("--once", action="store_true",
                        help="Process one message then exit (useful for testing).")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable info-level logging.")
    return parser.parse_args(argv)


def _make_client(redis_url: str, poll_timeout: int) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=None,
    )


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    root = logging.getLogger()
    root.handlers.clear()
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(_handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("worker_10kq").setLevel(logging.INFO)
    logging.getLogger("workers.queue_worker").setLevel(logging.INFO)
    logger.setLevel(logging.INFO)

    if args.verbose:
        for _noisy in ("sec_scraper_cli", "data_normalization_service"):
            logging.getLogger(_noisy).setLevel(logging.INFO)

    queue_name = normalize_queue_name(args.queue_name)
    dead_letter_queue = normalize_queue_name(args.dead_letter_queue)
    client: Redis = _make_client(args.redis_url, args.poll_timeout)

    app = _build_app()
    logger.info(
        "Gotham XBRL worker (10-K, 10-Q, 20-F, 10-K/10-Q, amendments) listening on Redis queue '%s'",
        queue_name,
    )

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
                payload = decode_message(raw_message.decode("utf-8") if isinstance(raw_message, bytes) else str(raw_message))
            except (json.JSONDecodeError, ValueError):
                logger.error("Could not decode queue message: %r", raw_message)
                if args.once:
                    break
                continue

            form_type = (payload.get("filing_type") or payload.get("form_type") or "").upper()

            if form_type not in _HANDLED_TYPES:
                logger.warning("Unsupported form_type %r on annual/quarterly queue — skipping", form_type)
                if args.once:
                    break
                continue

            attempts = int(payload.get("attempts") or 0)
            success = False
            _update_load_request_status(payload, "processing")

            try:
                success = _process_payload(app, payload)
            except (KeyboardInterrupt, SystemExit):
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
                    period_of_report=getattr(app, "_last_period_label", None)
                        or getattr(app, "_last_report_date", None),
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
