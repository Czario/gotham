"""Guidance persistence — `guidance_values` collection (normalize_data).

Writes the normalized records from ``agent/guidance.py`` into the SAME
``guidance_values`` collection the admin backend reads/writes, with doc
semantics matching ``GuidanceService.buildDoc`` (form → band, midpoint,
plus_minus derivation) and ``source="llm"`` as the only difference from
manual entries.

Upsert semantics (plan §6, aligned with the backend's ``create`` demotion):
  • key = (cik, accession_number, metric, basis, period.fiscal_year,
    period.quarter, period.period_type) — re-runs of the same filing replace
    the same doc in place (``created_at`` preserved).
  • is_current = latest for (cik, metric, basis, period): any OTHER accession
    still holding ``is_current:true`` for that key is demoted, and the new
    doc carries ``supersedes`` = the replaced doc's accession_number.
  • source=manual protection: a doc with ``source:"manual"`` for the key
    BLOCKS the LLM write — a human's hand-entered value is authoritative
    until the human removes it (same discipline as the backend comment).

Scoring (plan §4): ``score_guidance_for_cik`` resolves each current doc's
``standard_label`` (+ optional ``concept``) to the company's concept row via
the EXISTING ``concepts_standard_mapping`` vocabulary, reads the stored
actual from ``concept_values_{quarterly|annual}`` for the covered period,
and writes ``result {actual_value, actual_accession_number, delta_abs,
delta_pct, outcome}`` back onto the doc.  Non-GAAP metrics with no mapping
stay ``result=None`` (enrichment shows outcome=``pending``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from earnings_agents.agent.currency import is_currency_code
from earnings_agents.agent.guidance import (
    _period_type_binary,
    period_arrived,
    score,
)

logger = logging.getLogger(__name__)


def _get_db() -> Any:
    """Return the normalize_data database handle (patchable in tests)."""
    from earnings_agents.integrations.normalize import _get_client, _NORMALIZE_DB
    return _get_client()[_NORMALIZE_DB]


def _guidance_col(db: Any) -> Any:
    return db["guidance_values"]


def _label_variants(label: str) -> list[str]:
    """Total/singular/plural variants — matches the backend's labelVariants."""
    trimmed = (label or "").strip()
    if not trimmed:
        return []
    without_total = trimmed
    base = trimmed
    if trimmed.lower().startswith("total "):
        without_total = trimmed[6:].strip()
        base = without_total
    variants = [trimmed, without_total, base]
    if base.endswith("s"):
        variants.append(base[:-1])
    else:
        variants.append(base + "s")
    return list(dict.fromkeys(v for v in variants if v))


def _norm_label(s: str) -> str:
    """Whitespace/case-normalized label for deterministic matching."""
    return " ".join((s or "").strip().lower().split())


def _resolve_concept(db: Any, guidance_doc: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a guidance doc to the company's concept row — {name, _id}.

    Prefers the stored ``concept`` name (direct row join — takes priority in
    the backend enrichment); otherwise resolves ``standard_label`` → concept
    names via ``concepts_standard_mapping`` (the existing mapping
    vocabulary).  Checks both quarterly and annual concept collections so the
    resolution works regardless of how the covered period is stored.
    Returns None when the company has no such row (e.g. non-GAAP metrics
    with no GAAP concept) — the caller then keeps ``concept`` null.
    """
    cik = guidance_doc.get("cik")
    if not cik:
        return None
    statement_type = guidance_doc.get("statement_type") or "income"
    names: list[str] = []

    concept = (guidance_doc.get("concept") or "").strip()
    if concept:
        names = [concept]
    else:
        label = (guidance_doc.get("standard_label") or "").strip()
        if not label:
            return None
        variants = _label_variants(label)
        mapping = db["concepts_standard_mapping"]
        docs = list(mapping.find({
            "standard_label": {"$in": variants},
            "statement_type": statement_type,
            "isActive": {"$ne": False},
        }))
        for d in docs:
            for n in d.get("concepts") or []:
                if n and n not in names:
                    names.append(n)
        if not names:
            # Final fallback: the standard_label may BE the row's label (e.g.
            # "Provision for income taxes", "Total costs and expenses") rather
            # than a concepts_standard_mapping entry.  Match normalized_concepts
            # rows by normalized label so the direct-row join still works for
            # GAAP labels the mapping vocabulary lacks.
            norm_label = _norm_label(label)
            for col_name in ("normalized_concepts_quarterly", "normalized_concepts_annual"):
                for row in db[col_name].find({
                    "cik": cik, "statement_type": statement_type,
                }):
                    row_label = (row.get("label") or "").strip()
                    if row_label and _norm_label(row_label) == norm_label \
                            and row.get("concept"):
                        return {"name": row["concept"], "_id": row["_id"]}
            return None

    for col_name in ("normalized_concepts_quarterly", "normalized_concepts_annual"):
        row = db[col_name].find_one({
            "cik": cik,
            "concept": {"$in": names},
            "statement_type": statement_type,
        })
        if row is not None and row.get("concept"):
            return {"name": row["concept"], "_id": row["_id"]}
    return None


def _resolve_concept_id(db: Any, guidance_doc: dict[str, Any]) -> Any | None:
    """Resolve a guidance doc to a normalized_concepts_* row _id (scoring)."""
    resolved = _resolve_concept(db, guidance_doc)
    return resolved["_id"] if resolved else None


def _find_actual(
    db: Any, cik: str, concept_id: Any, covered_period: dict[str, Any],
) -> dict[str, Any] | None:
    """Read the stored actual for a covered period from concept_values_*."""
    fy = covered_period.get("fiscal_year")
    if not isinstance(fy, int) or fy < 1:
        return None
    q = covered_period.get("quarter")
    try:
        oid = concept_id  # ObjectId when real Mongo, str in tests — both work as filters
    except Exception:  # noqa: BLE001
        oid = concept_id
    if q is not None:
        doc = db["concept_values_quarterly"].find_one({
            "cik": cik,
            "concept_id": oid,
            "reporting_period.fiscal_year": fy,
            "reporting_period.quarter": q,
        })
    else:
        doc = db["concept_values_annual"].find_one({
            "cik": cik,
            "concept_id": oid,
            "reporting_period.fiscal_year": fy,
        })
    if doc is None:
        return None
    return {
        "value": doc.get("value"),
        "accession_number": doc.get("accession_number"),
        "currency": doc.get("currency"),
        # The company's REAL period end for the covered period.  Guidance period
        # identity is (fiscal_year, quarter) — this date is presentation, and
        # stamping it once the period has arrived keeps the stored doc
        # self-consistent with the reported period.
        "period_date": (doc.get("reporting_period") or {}).get("period_date"),
    }


def upsert_guidance_records(
    cik: str,
    records: list[dict[str, Any]],
    *,
    accession_number: str | None = None,
    form_type: str = "8-K",
    filing_period: Any = None,
) -> dict[str, Any]:
    """Persist normalized guidance records into ``guidance_values``.

    Delegates to ``filings_agent.integrations.guidance.upsert_guidance_records``
    so 8-K/6-K earnings releases and 10-K/10-Q MD&A filings share a single
    hardened, deduplicated writer into ``guidance_values``.
    """
    from filings_agent.integrations.guidance import (
        upsert_guidance_records as _shared_upsert,
    )

    return _shared_upsert(
        cik,
        records,
        accession_number=accession_number,
        form_type=form_type,
        filing_period=filing_period,
        db=_get_db(),
    )


def score_guidance_for_cik(
    cik: str,
    filing_period: Any,
) -> dict[str, Any]:
    """Score every current guidance doc whose covered period has arrived.

    Runs after each successful save (node ``save_guidance``): for each
    current guidance doc whose covered period is at-or-before the just-saved
    period, resolve it to the company's concept row (existing
    ``concepts_standard_mapping`` vocabulary), read the actual from
    ``concept_values_*``, compute ``result`` (score() in agent/guidance.py),
    and write it back onto the doc.  Metrics with no mapping (non-GAAP)
    simply stay unscored — enrichment renders outcome=``pending``.

    Never raised — always returns a summary dict (observability).
    """
    db = _get_db()
    col = _guidance_col(db)
    now = datetime.now(tz=timezone.utc)
    summary: dict[str, Any] = {
        "checked": 0, "scored": 0, "unresolved": 0, "errors": 0,
        "currency_mismatch": 0,
    }

    docs = list(col.find({"cik": cik, "is_current": True}))
    for d in docs:
        period = d.get("period") or {}
        if not period_arrived(period, filing_period):
            continue
        summary["checked"] += 1
        try:
            concept_id = _resolve_concept_id(db, d)
            if concept_id is None:
                summary["unresolved"] += 1
                continue
            actual = _find_actual(db, cik, concept_id, period)
            if actual is None or actual.get("value") is None:
                summary["unresolved"] += 1
                continue
            # Never compare DIFFERENT currencies: a beat/miss delta is only
            # meaningful when guidance and actual are in the same currency.
            # A confirmed mismatch leaves the doc unscored (result stays None);
            # a legacy/missing currency on either side keeps the previous
            # behavior rather than silently skipping every old record.
            guidance_currency = str(d.get("currency") or "").strip().upper()
            actual_currency = str(actual.get("currency") or "").strip().upper()
            if (
                is_currency_code(guidance_currency)
                and is_currency_code(actual_currency)
                and guidance_currency != actual_currency
            ):
                summary["currency_mismatch"] += 1
                continue
            result = score(d, float(actual["value"]), actual.get("accession_number"))
            update: dict[str, Any] = {"result": result, "updated_at": now}
            # The covered period has arrived: stamp the company's actual period
            # end onto the guidance doc.  Period identity is (fiscal_year,
            # quarter); the date is presentation, so this makes the doc show the
            # filer's real quarter-end (e.g. 2026-06-28) instead of a
            # calendar-derived guess (2026-06-30).
            actual_period_date = actual.get("period_date")
            if actual_period_date and str(period.get("period_end_date") or "") != str(
                actual_period_date
            ):
                update["period.period_end_date"] = actual_period_date
            col.update_one({"_id": d["_id"]}, {"$set": update})
            summary["scored"] += 1
        except Exception as exc:  # noqa: BLE001 — scoring never fails a run
            logger.warning("guidance scoring error for CIK %s: %s", cik, exc)
            summary["errors"] += 1

    return summary