"""Automatic Q4 calculation from Annual (10-K / 20-F) and Q1–Q3 data.

Whenever an annual filing is persisted (or when Q1–Q3 finish persisting for a
fiscal year whose annual data is already stored), this module computes Q4 values
into ``concept_values_quarterly``:
  - Balance Sheet: point-in-time at fiscal year end (Q4 = Annual).
  - Income & Cash Flow: Q4 = Annual - (Q1 + Q2 + Q3) for every concept where
    Annual, Q1, Q2, and Q3 are all present (with share counts kept as Annual).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _is_share_count_concept(concept: str) -> bool:
    low = (concept or "").split(":")[-1].lower()
    return "shares" in low and "pershare" not in low and "earningspershare" not in low


def _build_quarterly_concept_lookup(q_concepts: list[dict[str, Any]]) -> dict[str, Any]:
    """Map concept / canonical_concept / aliases -> quarterly concept _id."""
    lookup: dict[str, Any] = {}
    for doc in q_concepts:
        if doc.get("dimension_concept"):
            continue
        cid = doc.get("_id")
        if not cid:
            continue
        for key in (doc.get("concept"), doc.get("canonical_concept")):
            if key and key not in lookup:
                lookup[str(key)] = cid
        for alias in doc.get("aliases") or []:
            if alias and alias not in lookup:
                lookup[str(alias)] = cid
    return lookup


def auto_calculate_q4_for_fiscal_year(
    db: Any,
    cik: str,
    fiscal_year: int,
) -> Dict[str, int]:
    """Compute and upsert Q4 values for ``(cik, fiscal_year)`` when required periods exist.

    Returns a dict ``{statement_type: count_written}`` for statements where Q4
    values were computed.
    """
    if db is None or not cik or not isinstance(fiscal_year, int):
        return {}

    ac_col = db["normalized_concepts_annual"]
    av_col = db["concept_values_annual"]
    qc_col = db["normalized_concepts_quarterly"]
    qv_col = db["concept_values_quarterly"]

    now = datetime.now(timezone.utc)
    written: Dict[str, int] = {}

    for stmt_type in ("income", "balancesheet", "cashflow"):
        annual_vals = list(
            av_col.find({
                "cik": cik,
                "statement_type": stmt_type,
                "reporting_period.fiscal_year": fiscal_year,
                "dimension_value": {"$ne": True},
            })
        )
        if not annual_vals:
            continue

        # Map annual concept_id -> annual concept doc
        a_concept_ids = {v.get("concept_id") for v in annual_vals if v.get("concept_id")}
        a_concept_map = {
            d["_id"]: d
            for d in ac_col.find({"_id": {"$in": list(a_concept_ids)}})
        }

        q_concepts = list(
            qc_col.find({
                "cik": cik,
                "statement_type": stmt_type,
                "dimension_concept": {"$ne": True},
            })
        )
        q_lookup = _build_quarterly_concept_lookup(q_concepts)
        if not q_lookup:
            continue

        # For income & cashflow, preload Q1, Q2, Q3 values keyed by (concept_id, quarter)
        q_vals_by_cid: dict[Any, dict[int, float]] = {}
        if stmt_type != "balancesheet":
            for qv in qv_col.find({
                "cik": cik,
                "statement_type": stmt_type,
                "reporting_period.fiscal_year": fiscal_year,
                "reporting_period.quarter": {"$in": [1, 2, 3]},
                "dimension_value": {"$ne": True},
            }):
                cid = qv.get("concept_id")
                q_num = (qv.get("reporting_period") or {}).get("quarter")
                val = qv.get("value")
                if cid and q_num in (1, 2, 3) and isinstance(val, (int, float)):
                    q_vals_by_cid.setdefault(cid, {})[q_num] = float(val)

        count = 0
        for av in annual_vals:
            a_val = av.get("value")
            if not isinstance(a_val, (int, float)):
                continue
            a_doc = a_concept_map.get(av.get("concept_id"))
            if not a_doc or a_doc.get("abstract"):
                continue

            c_name = str(a_doc.get("concept") or "")
            canon_name = str(a_doc.get("canonical_concept") or "")
            q_cid = q_lookup.get(c_name) or q_lookup.get(canon_name)
            if not q_cid:
                for alias in a_doc.get("aliases") or []:
                    q_cid = q_lookup.get(str(alias))
                    if q_cid:
                        break
            if not q_cid:
                continue

            if stmt_type == "balancesheet":
                q4_val = float(a_val)
            else:
                q_map = q_vals_by_cid.get(q_cid) or {}
                if not all(q in q_map for q in (1, 2, 3)):
                    continue
                if _is_share_count_concept(c_name):
                    q4_val = float(a_val)
                else:
                    q4_val = round(float(a_val) - (q_map[1] + q_map[2] + q_map[3]), 6)

            a_rp = av.get("reporting_period") or {}
            q4_rp = {
                "end_date": a_rp.get("end_date"),
                "period_date": a_rp.get("period_date"),
                "fiscal_year": fiscal_year,
                "quarter": 4,
            }

            existing = qv_col.find_one({
                "cik": cik,
                "concept_id": q_cid,
                "reporting_period.fiscal_year": fiscal_year,
                "reporting_period.quarter": 4,
                "dimension_value": {"$ne": True},
            })
            if existing:
                qv_col.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "value": q4_val,
                            "reporting_period": q4_rp,
                            "calculated": True,
                            "source": "auto_q4_from_annual",
                            "form_type": av.get("form_type") or "10-K",
                            "accession_number": av.get("accession_number"),
                        }
                    },
                )
            else:
                qv_col.insert_one({
                    "concept_id": q_cid,
                    "cik": cik,
                    "statement_type": stmt_type,
                    "form_type": av.get("form_type") or "10-K",
                    "reporting_period": q4_rp,
                    "value": q4_val,
                    "created_at": now,
                    "dimension_value": False,
                    "calculated": True,
                    "source": "auto_q4_from_annual",
                    "accession_number": av.get("accession_number"),
                })
            count += 1

        if count > 0:
            written[stmt_type] = count

    return written
