"""Validate node — deterministic checks (ask 1).

Runs every validator over the in-memory bundles and records a
``ValidationReport`` on the state.  The node itself NEVER blocks: it always
moves the run to ``validated``.  Blocking is the persist node's decision (the
STRICT_ACCURACY gate), which keeps "validate" and "enforce" separable — a
report-only rollout is just ``STRICT_ACCURACY=0``.

When a report store is supplied, the report is persisted (upserted) so a
rejected filing is still traceable.  Report storage is best-effort: a failure
there must never fail the filing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..validation import validate_bundles

logger = logging.getLogger(__name__)


def make_validate_node(report_store: Optional[Any] = None) -> Callable[[dict], dict]:
    """Build the ``validate`` node, optionally persisting its report."""

    def validate_node(state: dict) -> dict:
        bundles = state.get("bundles") or []

        report = validate_bundles(
            bundles,
            cik=str(state.get("cik") or ""),
            ticker=str(state.get("ticker") or ""),
            form_type=str(state.get("form_type") or ""),
            accession_number=state.get("accession_number"),
        )

        # ``needs_decision`` is state-only metadata for the next graph route;
        # reports remain the stable Finding schema.  Every blocking finding
        # needs either deterministic repair or agent/human judgment.
        findings = []
        for finding in report.findings:
            item = finding.to_dict()
            item["needs_decision"] = finding.is_blocking
            findings.append(item)

        if report_store is not None:
            try:
                report_store.save(report)
            except Exception as exc:  # noqa: BLE001 — audit trail is best-effort
                logger.warning(
                    "validation report save failed for %s: %s", state.get("cik"), exc
                )

        logger.info(
            "validate: %s — %d finding(s), %d blocking (CIK %s %s)",
            report.status,
            len(report.findings),
            len(report.blocking),
            state.get("cik"),
            state.get("accession_number") or "",
        )

        return {
            **state,
            "findings": findings,
            "validation_report": report.to_dict(),
            "status": "validated",
        }

    return validate_node
