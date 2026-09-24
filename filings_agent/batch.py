"""Headless batch runner (P7).

Drives a sequence of prepared filings through the filing graph with optional
**resume** (skip accessions whose latest session record is ``saved``) and
optional **audit** (JSONL).  Also exposes the company-stage entry point.

"LLM only on ``needs_decision``" is a property of the graph itself, not of this
runner: ``agent_review`` is skipped unless validation flagged a decision, and
the guidance pass only runs when MD&A text is available and enabled.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def summarize(results: Iterable[dict]) -> dict[str, Any]:
    """Count filing outcomes (status → count)."""
    counts: dict[str, int] = {}
    for result in results or []:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"filings": sum(counts.values()), "counts": counts}


def run_batch(
    graph: Any,
    states: Iterable[dict],
    *,
    session: Any = None,
    audit: Any = None,
    skip_saved: bool = True,
    on_result: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """Run prepared filing states through *graph*.

    Args:
        graph: A compiled filing graph (``build_filing_graph``).
        states: ``FilingAgentState`` dicts, processed in the order given
            (newest → oldest is the pipeline's natural order).
        session: Optional ``RunSession`` for resume + durable ledger.
        audit: Optional ``AuditLog``.
        skip_saved: When True and a session is supplied, skip filings whose
            latest recorded status is ``saved``.
        on_result: Optional callback invoked with each final state.

    Returns:
        ``{"results": [...], "summary": {...}}``.
    """
    results: list[dict] = []

    for state in states or []:
        cik = state.get("cik")
        accession = state.get("accession_number")
        form_type = state.get("form_type")

        if skip_saved and session is not None and session.is_saved(accession):
            if audit is not None:
                audit.emit(
                    "filing_skipped",
                    cik=cik,
                    accession_number=accession,
                    form_type=form_type,
                    reason="already_saved",
                )
            logger.info("batch: skipping %s (already saved)", accession)
            skipped = {**state, "status": "skipped", "error": None}
            results.append(skipped)
            if on_result is not None:
                on_result(skipped)
            continue

        if audit is not None:
            audit.emit(
                "filing_start",
                cik=cik,
                accession_number=accession,
                form_type=form_type,
            )

        started = time.perf_counter()
        try:
            final = graph.invoke(state)
        except Exception as exc:  # noqa: BLE001 — one filing must not kill a batch
            logger.error("batch: filing %s failed: %s", accession, exc, exc_info=True)
            final = {**state, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        elapsed = round(time.perf_counter() - started, 2)

        findings = len(final.get("findings") or [])
        blocking = (final.get("validation_report") or {}).get("blocking_count")
        guidance = len(final.get("guidance_records") or [])

        if session is not None:
            session.record_filing(
                cik=cik,
                accession_number=accession,
                status=str(final.get("status") or "unknown"),
                form_type=form_type,
                elapsed_s=elapsed,
                findings=findings,
                blocking_findings=blocking,
                guidance_records=guidance,
                validation_status=(final.get("validation_report") or {}).get("status"),
                error=final.get("error"),
            )
        if audit is not None:
            audit.emit(
                "filing_end",
                cik=cik,
                accession_number=accession,
                form_type=form_type,
                status=final.get("status"),
                elapsed_s=elapsed,
                findings=findings,
                blocking_findings=blocking,
                guidance_records=guidance,
                error=final.get("error"),
            )

        results.append(final)
        if on_result is not None:
            on_result(final)

    summary = summarize(results)
    if audit is not None:
        audit.emit("batch_end", **summary)
    return {"results": results, "summary": summary}


def run_company_stage(
    company_graph: Any,
    *,
    cik: str,
    ticker: str = "",
    statements: Optional[list] = None,
    session: Any = None,
    audit: Any = None,
) -> dict[str, Any]:
    """Run the company-level stage graph for one company."""
    from .state import new_company_state

    state = new_company_state(
        cik=cik, ticker=ticker, quarterly_statements=statements
    )
    if audit is not None:
        audit.emit(
            "company_start",
            cik=str(cik),
            ticker=ticker,
            statements=len(statements or []),
        )

    try:
        final = company_graph.invoke(state)
    except Exception as exc:  # noqa: BLE001 — company stage is best-effort
        logger.error("company stage failed for %s: %s", cik, exc, exc_info=True)
        final = {**state, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    quarterly = final.get("deaccumulation") or {}
    reconciliation = final.get("reconciliation") or {}
    finalization = final.get("report_finalization") or {}

    if session is not None:
        session.record_company(
            cik=cik,
            ticker=ticker,
            status=final.get("status"),
            statements=len(statements or []),
            deaccumulation=quarterly.get("status"),
            reconciliation=reconciliation.get("status"),
            reconciliation_filled=reconciliation.get("filled"),
            report_finalization=finalization.get("status"),
            error=final.get("error"),
        )
    if audit is not None:
        audit.emit(
            "company_end",
            cik=str(cik),
            ticker=ticker,
            status=final.get("status"),
            deaccumulation=quarterly.get("status"),
            reconciliation=reconciliation.get("status"),
            reconciliation_filled=reconciliation.get("filled"),
            report_finalization=finalization.get("status"),
            error=final.get("error"),
        )
    return final
