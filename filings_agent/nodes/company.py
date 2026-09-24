"""Company-level stage nodes (P7).

Runs once per company, after all of its filings have been processed by the
filing graph:

* ``quarterly_deaccumulation`` — derives quarterly deltas from the accumulated
  income/cashflow statements (``PeriodBasedFinancialCalculationService``).
* ``companyfacts_reconciliation`` — the SEC companyfacts gap-fill backstop
  (``CompanyFactsReconciliationService``), insert-only with
  ``source='sec_companyfacts'`` provenance.
* ``finalize_validation_reports`` — closes the company's ``validation_reports``
  rows, recording the company-stage summary on them.

Every node is best-effort: it records its own status and never raises.  A node
never downgrades an existing ``failed`` status, so an earlier failure stays
visible in the company summary.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


def _next_status(state: dict, default: str) -> str:
    """Preserve ``failed``; otherwise advance to *default*."""
    return "failed" if state.get("status") == "failed" else default


def make_quarterly_node(quarterly_service: Any) -> Callable[[dict], dict]:
    """Build the ``quarterly_deaccumulation`` node."""

    def quarterly_deaccumulation_node(state: dict) -> dict:
        statements = state.get("quarterly_statements") or []
        cik = state.get("cik")
        if not statements or not cik:
            return {
                **state,
                "deaccumulation": {"status": "no_statements", "statements": 0},
                "status": _next_status(state, "deaccumulated"),
            }

        logger.info(
            "quarterly: deaccumulating %d statement(s) for CIK %s",
            len(statements), cik,
        )
        try:
            quarterly_service.process_company_from_statements(str(cik), statements)
        except Exception as exc:  # noqa: BLE001 — derived data, never fatal
            logger.error("quarterly deaccumulation failed for %s: %s", cik, exc, exc_info=True)
            return {
                **state,
                "deaccumulation": {
                    "status": "failed",
                    "statements": len(statements),
                    "error": str(exc),
                },
                "status": "failed",
            }
        return {
            **state,
            "deaccumulation": {"status": "ok", "statements": len(statements)},
            "status": _next_status(state, "deaccumulated"),
        }

    return quarterly_deaccumulation_node


def make_companyfacts_node(
    reconciliation_provider: Any = None,
    *,
    enable_reconciliation: bool = True,
    frequencies: Iterable[str] = ("annual", "quarterly"),
    include_edge: bool = True,
) -> Callable[[dict], dict]:
    """Build the ``companyfacts_reconciliation`` node.

    *reconciliation_provider* is a zero-arg callable returning the service (or
    ``None``); the scraper passes its lazy builder so the service is only
    constructed when reconciliation actually runs.
    """

    def companyfacts_reconciliation_node(state: dict) -> dict:
        cik = state.get("cik")
        enabled = state.get("enable_reconciliation")
        if enabled is None:
            enabled = enable_reconciliation
        if not enabled:
            return {
                **state,
                "reconciliation": {"status": "disabled", "filled": 0},
                "status": _next_status(state, "reconciled"),
            }

        service = None
        if callable(reconciliation_provider):
            try:
                service = reconciliation_provider()
            except Exception as exc:  # noqa: BLE001
                logger.warning("companyfacts service unavailable for %s: %s", cik, exc)
        elif reconciliation_provider is not None:
            service = reconciliation_provider

        if service is None:
            return {
                **state,
                "reconciliation": {"status": "unavailable", "filled": 0},
                "status": _next_status(state, "reconciled"),
            }

        padded = str(cik or "").zfill(10)
        by_frequency: dict[str, Any] = {}
        filled = 0
        try:
            for freq in frequencies:
                stats = service.reconcile_company(
                    padded, frequency=freq, include_edge=include_edge
                )
                by_frequency[freq] = stats
                filled += int((stats or {}).get("filled", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — backstop only
            logger.warning("companyfacts reconciliation failed for %s: %s", cik, exc, exc_info=True)
            return {
                **state,
                "reconciliation": {"status": "failed", "filled": filled, "error": str(exc)},
                "status": "failed",
            }

        if filled:
            logger.info("companyfacts: filled %d gap value(s) for CIK %s", filled, cik)
        return {
            **state,
            "reconciliation": {
                "status": "ok",
                "filled": filled,
                "by_frequency": by_frequency,
            },
            "status": _next_status(state, "reconciled"),
        }

    return companyfacts_reconciliation_node


def make_finalize_reports_node(report_store: Any = None) -> Callable[[dict], dict]:
    """Build the ``finalize_validation_reports`` node."""

    def finalize_validation_reports_node(state: dict) -> dict:
        cik = state.get("cik")
        if report_store is None:
            return {
                **state,
                "report_finalization": {"status": "unavailable", "finalized": 0},
                "status": _next_status(state, "finalized"),
            }

        summary = {
            "deaccumulation": (state.get("deaccumulation") or {}).get("status"),
            "reconciliation": (state.get("reconciliation") or {}).get("status"),
            "reconciliation_filled": (state.get("reconciliation") or {}).get("filled"),
        }
        try:
            finalized = report_store.finalize_company(str(cik), summary)
        except Exception as exc:  # noqa: BLE001 — audit stamping is best-effort
            logger.warning("validation-report finalization failed for %s: %s", cik, exc)
            return {
                **state,
                "report_finalization": {"status": "failed", "finalized": 0, "error": str(exc)},
                "status": "failed",
            }
        return {
            **state,
            "report_finalization": {"status": "ok", "finalized": int(finalized or 0)},
            "status": _next_status(state, "finalized"),
        }

    return finalize_validation_reports_node
