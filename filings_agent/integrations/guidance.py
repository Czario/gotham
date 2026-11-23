"""Guidance persistence wrapper for ``filings_agent``.

Canonical implementation lives in ``shared.guidance_store``.
"""
from __future__ import annotations

from typing import Any

from shared.guidance_store import (  # noqa: F401
    _canonical_accession,
    _canonical_cik,
    _find_actual,
    _guidance_col,
    _label_variants,
    _norm_label,
    _resolve_concept,
    _resolve_concept_id,
    score_guidance_for_cik as _shared_score,
    upsert_guidance_records as _shared_upsert,
)


def _get_db() -> Any:
    """Return the target database handle (patchable in tests)."""
    from pymongo import MongoClient
    from filings_agent.config import DATABASE_NAME, MONGODB_URI

    return MongoClient(MONGODB_URI)[DATABASE_NAME]


def upsert_guidance_records(
    cik: str,
    records: list[dict[str, Any]],
    *,
    accession_number: str | None = None,
    form_type: str = "8-K",
    filing_period: Any = None,
    db: Any = None,
) -> dict[str, Any]:
    return _shared_upsert(
        cik,
        records,
        accession_number=accession_number,
        form_type=form_type,
        filing_period=filing_period,
        db=db if db is not None else _get_db(),
    )


def score_guidance_for_cik(
    cik: str,
    filing_period: Any,
    *,
    db: Any = None,
) -> dict[str, Any]:
    return _shared_score(
        cik,
        filing_period,
        db=db if db is not None else _get_db(),
    )
