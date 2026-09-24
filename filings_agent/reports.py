"""Persistence for validation reports (the ``validation_reports`` collection).

Reports are upserted per ``(cik, accession_number, form_type)`` so re-running a
filing replaces its report instead of accumulating duplicates.  Reports are
written even when statement persistence is refused, so a rejected filing is
always traceable.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .validation.report import ValidationReport

logger = logging.getLogger(__name__)

VALIDATION_REPORTS_COLLECTION = "validation_reports"


class ValidationReportStore:
    """Thin MongoDB writer for :class:`ValidationReport` documents."""

    def __init__(self, db: Any, collection_name: str = VALIDATION_REPORTS_COLLECTION):
        self._collection = db[collection_name]

    def save(self, report: ValidationReport) -> None:
        """Upsert the report for its filing identity."""
        doc = report.to_dict()
        self._collection.update_one(
            {
                "cik": report.cik,
                "accession_number": report.accession_number,
                "form_type": report.form_type,
            },
            {"$set": doc},
            upsert=True,
        )
        logger.debug(
            "validation report stored for %s %s (%s)",
            report.cik,
            report.accession_number or "?",
            report.status,
        )

    def finalize_company(self, cik: str, summary: Optional[dict] = None) -> int:
        """Close every report for *cik* once the company stage has run.

        Marks the documents final so a report is distinguishable from one still
        awaiting quarterly deaccumulation / companyfacts reconciliation.
        Returns the number of reports updated (0 when the store is empty).
        """
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc)
        result = self._collection.update_many(
            {"cik": str(cik)},
            {
                "$set": {
                    "finalized_at": now,
                    "phase": "company_final",
                    "company_stage": summary or {},
                }
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)

    def update_decision(
        self,
        cik: Optional[str],
        accession_number: Optional[str],
        form_type: Optional[str],
        decision: dict,
    ) -> None:
        """Attach the agent's final write decision to the filing's report.

        The report is the durable record of *why* a filing was written, written
        partially, or skipped, so the decision is stored alongside the findings
        it was made from.
        """
        if not cik:
            return
        self._collection.update_one(
            {
                "cik": str(cik),
                "accession_number": accession_number,
                "form_type": form_type,
            },
            {"$set": {"decision": decision}},
            upsert=False,
        )


def build_report_store() -> Optional[ValidationReportStore]:
    """Build a report store from the environment (``.env``); ``None`` on failure."""
    try:
        from pymongo import MongoClient

        from .config import DATABASE_NAME, MONGODB_URI

        return ValidationReportStore(MongoClient(MONGODB_URI)[DATABASE_NAME])
    except Exception as exc:  # noqa: BLE001 — report storage is best-effort
        logger.warning("could not build validation report store: %s", exc)
        return None
