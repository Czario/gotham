"""Historical / Comparative Period Backfill Node (ADK Agent + DB Gap Matcher).

When an SEC filing (10-K, 10-Q, 20-F) is processed, it frequently reports
comparative columns for prior quarters or prior fiscal years — including:
  1. Main-line concepts that were not reported (or were reclassified/restated)
     in the older historical filing.
  2. Dimensional breakdowns (product, segment, geography) for prior periods that
     were only introduced in the newer filing.
  3. Entire historical periods that do not yet exist in our database.

This node inspects all comparative periods captured in ``sec_presentation`` and
``statement_docs``, checks the database (``concepts_annual`` / ``concepts_quarterly``
and ``concept_values_annual`` / ``concept_values_quarterly``) to find what is
missing for those historical periods, and stages safe, insert-only backfill
entries on ``state["historical_backfill"]`` for the ``persist`` node to write.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Optional

from core.extractors.sec_presentation_extractor import canonical_statement_type
from filings_agent.hooks import report_call

logger = logging.getLogger(__name__)


def _parse_period_dates(period_str: str, form_type: str = "10-K") -> Optional[dict[str, Any]]:
    """Parse an XBRL period string into ``end_date``, ``start_date``, ``cadence``, ``fiscal_year``, ``quarter``."""
    if not period_str or not isinstance(period_str, str):
        return None
    text = period_str.strip()

    # Format: "YYYY-MM-DD HH:MM:SS to YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD to YYYY-MM-DD"
    m_dur = re.search(r"(\d{4}-\d{2}-\d{2}).*?\bto\b.*?(\d{4}-\d{2}-\d{2})", text, re.IGNORECASE)
    if m_dur:
        start_s, end_s = m_dur.group(1), m_dur.group(2)
        try:
            start_dt = datetime.strptime(start_s, "%Y-%m-%d")
            end_dt = datetime.strptime(end_s, "%Y-%m-%d")
        except ValueError:
            return None
        days = (end_dt - start_dt).days
        if 330 <= days <= 385:
            cadence = "annual"
            quarter = None
        elif 70 <= days <= 110:
            cadence = "quarterly"
            quarter = ((end_dt.month - 1) // 3) + 1
        else:
            # YTD (6m / 9m) — skip direct slot backfill unless deaccumulated
            return None
        return {
            "period_str": text,
            "start_date": start_s,
            "end_date": end_s,
            "end_dt": end_dt,
            "cadence": cadence,
            "fiscal_year": end_dt.year,
            "quarter": quarter,
            "period_type": "duration",
        }

    # Format: instant "YYYY-MM-DD..."
    m_inst = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m_inst:
        end_s = m_inst.group(1)
        try:
            end_dt = datetime.strptime(end_s, "%Y-%m-%d")
        except ValueError:
            return None
        is_q = str(form_type or "").upper().startswith("10-Q")
        cadence = "quarterly" if is_q else "annual"
        quarter = (((end_dt.month - 1) // 3) + 1) if is_q else None
        return {
            "period_str": text,
            "start_date": None,
            "end_date": end_s,
            "end_dt": end_dt,
            "cadence": cadence,
            "fiscal_year": end_dt.year,
            "quarter": quarter,
            "period_type": "instant",
        }

    return None


def _collect_comparative_candidates(
    state: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Collect comparative historical period facts across ``sec_presentation`` and ``statement_docs``.

    Returns map keyed by ``(statement_type, end_date)`` -> candidate dict.
    """
    form_type = str(state.get("form_type") or "10-K")
    primary_rp = state.get("reporting_period") or {}
    primary_end = str(primary_rp.get("end_date") or primary_rp.get("period_date") or "")[:10]

    # Also detect primary period end from bundles if state["reporting_period"] is empty
    if not primary_end:
        for b in state.get("bundles") or []:
            brp = getattr(b, "reporting_period", None)
            if isinstance(brp, dict):
                primary_end = str(brp.get("end_date") or brp.get("period_date") or "")[:10]
                if primary_end:
                    break

    candidates: dict[tuple[str, str], dict[str, Any]] = {}

    def _add_fact(stmt_type: str, p_info: dict[str, Any], fact: dict[str, Any], is_dim: bool = False) -> None:
        end_s = p_info["end_date"]
        if primary_end and end_s == primary_end:
            return
        key = (stmt_type, end_s)
        entry = candidates.setdefault(
            key,
            {
                "statement_type": stmt_type,
                "end_date": end_s,
                "start_date": p_info.get("start_date"),
                "cadence": p_info["cadence"],
                "fiscal_year": p_info["fiscal_year"],
                "quarter": p_info["quarter"],
                "concepts": {},
                "dimensional_facts": [],
            },
        )
        if not is_dim:
            c_name = str(fact.get("concept") or "")
            if c_name and fact.get("value") is not None and c_name not in entry["concepts"]:
                entry["concepts"][c_name] = {
                    "concept": c_name,
                    "label": fact.get("label") or c_name.split(":")[-1],
                    "value": float(fact["value"]),
                    "unit": fact.get("unit"),
                    "parent_concept": fact.get("parent_concept"),
                    "calc_parent_concept": fact.get("calc_parent_concept"),
                    "reading_order": fact.get("reading_order", 0),
                }
        else:
            c_name = str(fact.get("concept") or fact.get("parent_concept") or "")
            if c_name and fact.get("value") is not None:
                dim_sig = (
                    c_name,
                    str(fact.get("label") or ""),
                    str(sorted((fact.get("dimension_details") or {}).items())),
                )
                seen_sigs = entry.setdefault("_seen_dim_sigs", set())
                if dim_sig not in seen_sigs:
                    seen_sigs.add(dim_sig)
                    entry["dimensional_facts"].append({
                        "parent_concept": c_name,
                        "label": fact.get("label") or "",
                        "value": float(fact["value"]),
                        "unit": fact.get("unit"),
                        "dimension_details": fact.get("dimension_details") or {},
                    })

    # 1. From sec_presentation snapshot
    sec_pres = state.get("sec_presentation") or {}
    for raw_st, st_data in (sec_pres.get("statements") or {}).items():
        canon_st = canonical_statement_type(raw_st)
        for period_key, p_payload in (st_data.get("comparative_periods") or {}).items():
            p_info = _parse_period_dates(period_key, form_type=form_type)
            if not p_info:
                continue
            for cf in p_payload.get("concepts") or []:
                _add_fact(canon_st, p_info, cf, is_dim=False)
            for cdf in p_payload.get("dimensional_facts") or []:
                _add_fact(canon_st, p_info, cdf, is_dim=True)

    # 2. From statement_docs directly (if comparative_facts were attached on line items)
    for doc in state.get("statement_docs") or []:
        if not isinstance(doc, dict):
            continue
        canon_st = canonical_statement_type(doc.get("statement_type"))
        items = doc.get("data") or doc.get("line_items") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for cf in item.get("comparative_facts") or []:
                p_info = _parse_period_dates(str(cf.get("period") or ""), form_type=form_type)
                if p_info:
                    _add_fact(canon_st, p_info, cf, is_dim=False)
            for cdf in item.get("comparative_dimensional_facts") or []:
                p_info = _parse_period_dates(str(cdf.get("period") or ""), form_type=form_type)
                if p_info:
                    _add_fact(canon_st, p_info, cdf, is_dim=True)

    return candidates


def _filter_missing_in_db(
    norm_service: Any,
    cik: str,
    candidates: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter comparative candidates to ONLY facts/concepts not yet present in MongoDB for that period."""
    db_conn = getattr(norm_service, "db_connection", None)
    target_db = getattr(db_conn, "target_db", None) if db_conn is not None else None

    entries: list[dict[str, Any]] = []
    for (stmt_type, end_date), cand in sorted(candidates.items()):
        cadence = cand["cadence"]
        if target_db is None:
            # In-memory / test mode without a live MongoDB connection: keep all comparative candidates
            missing_concepts = list(cand["concepts"].values())
            missing_dims = list(cand["dimensional_facts"])
        else:
            concepts_coll_name = (
                "normalized_concepts_annual" if cadence == "annual" else "normalized_concepts_quarterly"
            )
            legacy_concepts_coll_name = "concepts_annual" if cadence == "annual" else "concepts_quarterly"
            values_coll_name = "concept_values_annual" if cadence == "annual" else "concept_values_quarterly"
            try:
                concepts_coll = target_db[concepts_coll_name]
                values_coll = target_db[values_coll_name]

                # Map concept_id -> concept doc for this company + statement
                stored_concepts = list(
                    concepts_coll.find(
                        {"cik": cik, "statement_type": stmt_type},
                        {"_id": 1, "concept": 1, "aliases": 1, "dimension_concept": 1, "parent_concept": 1},
                    )
                )
                if not stored_concepts:
                    stored_concepts = list(
                        target_db[legacy_concepts_coll_name].find(
                            {"cik": cik, "statement_type": stmt_type},
                            {"_id": 1, "concept": 1, "aliases": 1, "dimension_concept": 1, "parent_concept": 1},
                        )
                    )
                cid_to_names: dict[Any, set[str]] = {}
                for sc in stored_concepts:
                    names = {str(sc.get("concept") or "")}
                    for al in sc.get("aliases") or []:
                        if al:
                            names.add(str(al))
                    cid_to_names[sc["_id"]] = names

                # Find values already stored for this historical period end_date
                end_dt_prefix = end_date[:10]
                existing_val_docs = list(
                    values_coll.find(
                        {
                            "cik": cik,
                            "statement_type": stmt_type,
                            "$or": [
                                {"reporting_period.end_date": end_dt_prefix},
                                {"reporting_period.period_date": end_dt_prefix},
                                {"period_key": end_dt_prefix},
                            ],
                        },
                        {"concept_id": 1, "concept": 1, "value": 1},
                    )
                )
                filled_concept_names: set[str] = set()
                for vd in existing_val_docs:
                    if vd.get("value") is None:
                        continue
                    if vd.get("concept"):
                        filled_concept_names.add(str(vd["concept"]))
                    cid = vd.get("concept_id")
                    if cid in cid_to_names:
                        filled_concept_names.update(cid_to_names[cid])

                missing_concepts = [
                    c_item
                    for c_name, c_item in cand["concepts"].items()
                    if c_name not in filled_concept_names
                ]
                missing_dims = [
                    d_item
                    for d_item in cand["dimensional_facts"]
                    if str(d_item.get("label") or "") not in filled_concept_names
                ]
            except Exception as exc:
                logger.debug("historical_backfill DB check skipped for %s %s: %s", cik, end_date, exc)
                missing_concepts = list(cand["concepts"].values())
                missing_dims = list(cand["dimensional_facts"])

        if missing_concepts or missing_dims:
            entries.append({
                "cik": cik,
                "statement_type": stmt_type,
                "cadence": cadence,
                "end_date": end_date,
                "start_date": cand.get("start_date"),
                "fiscal_year": cand.get("fiscal_year"),
                "quarter": cand.get("quarter"),
                "missing_concepts": missing_concepts,
                "missing_dimensional_facts": missing_dims,
            })

    return entries


def make_historical_backfill_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build the comparative / historical period gap discovery node."""

    def historical_backfill_node(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") in ("failed", "skipped"):
            return state

        cik = str(state.get("cik") or "")
        accession = str(state.get("accession_number") or "")
        candidates = _collect_comparative_candidates(state)
        if not candidates:
            return {
                **state,
                "historical_backfill": {
                    "periods_checked": 0,
                    "missing_facts_found": 0,
                    "missing_dim_facts_found": 0,
                    "entries": [],
                },
            }

        entries = _filter_missing_in_db(norm_service, cik, candidates)
        for entry in entries:
            entry["source"] = "comparative_filing_backfill"
            entry["source_accession"] = accession

        total_concepts = sum(len(e["missing_concepts"]) for e in entries)
        total_dims = sum(len(e["missing_dimensional_facts"]) for e in entries)

        if total_concepts or total_dims:
            logger.info(
                "[backfill] +%d items, +%d dims (%d periods)",
                total_concepts, total_dims, len(entries),
            )

        return {
            **state,
            "historical_backfill": {
                "periods_checked": len(candidates),
                "missing_facts_found": total_concepts,
                "missing_dim_facts_found": total_dims,
                "entries": entries,
            },
        }

    return historical_backfill_node


def persist_historical_backfill(
    norm_service: Any,
    state: dict[str, Any],
    *,
    permitted_statements: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Insert missing historical period concepts and values into MongoDB (insert-only, never overwrites)."""
    plan = state.get("historical_backfill") or {}
    entries = plan.get("entries") or []
    if not entries:
        return {"periods_backfilled": 0, "values_inserted": 0, "dims_inserted": 0}

    db_conn = getattr(norm_service, "db_connection", None)
    target_db = getattr(db_conn, "target_db", None) if db_conn is not None else None

    # Allow custom hook on norm_service (useful for tests or custom persistence)
    custom_persister = getattr(norm_service, "persist_historical_backfill_entries", None)
    if callable(custom_persister):
        return custom_persister(entries, permitted_statements=permitted_statements)

    if target_db is None:
        return {
            "periods_backfilled": len(entries),
            "values_inserted": sum(len(e.get("missing_concepts") or []) for e in entries),
            "dims_inserted": sum(len(e.get("missing_dimensional_facts") or []) for e in entries),
        }

    values_inserted = 0
    dims_inserted = 0
    periods_touched = 0

    for entry in entries:
        stmt_type = entry.get("statement_type") or ""
        if permitted_statements is not None and stmt_type not in permitted_statements:
            continue

        cik = str(entry.get("cik") or state.get("cik") or "")
        cadence = entry.get("cadence") or "annual"
        end_date = entry.get("end_date")
        fy = entry.get("fiscal_year")
        quarter = entry.get("quarter")
        source_acc = entry.get("source_accession") or state.get("accession_number")

        concepts_coll = target_db[
            "normalized_concepts_annual" if cadence == "annual" else "normalized_concepts_quarterly"
        ]
        legacy_concepts_coll = target_db[
            "concepts_annual" if cadence == "annual" else "concepts_quarterly"
        ]
        values_coll = target_db["concept_values_annual" if cadence == "annual" else "concept_values_quarterly"]

        entry_inserted = 0
        for item in entry.get("missing_concepts") or []:
            c_name = item.get("concept")
            val = item.get("value")
            if not c_name or val is None:
                continue

            # Find or create concept document in stored hierarchy
            c_doc = concepts_coll.find_one({
                "cik": cik,
                "statement_type": stmt_type,
                "$or": [{"concept": c_name}, {"aliases": c_name}],
            })
            if c_doc is None:
                c_doc = legacy_concepts_coll.find_one({
                    "cik": cik,
                    "statement_type": stmt_type,
                    "$or": [{"concept": c_name}, {"aliases": c_name}],
                })
            if c_doc is None:
                continue

            cid = c_doc["_id"]
            # Insert-only check: never overwrite an existing value for this historical period
            existing = values_coll.find_one({
                "cik": cik,
                "concept_id": cid,
                "reporting_period.end_date": end_date,
            })
            if existing is not None and existing.get("value") is not None:
                continue

            values_coll.update_one(
                {
                    "cik": cik,
                    "concept_id": cid,
                    "reporting_period.end_date": end_date,
                },
                {
                    "$setOnInsert": {
                        "cik": cik,
                        "statement_type": stmt_type,
                        "concept_id": cid,
                        "concept": c_doc.get("concept") or c_name,
                        "value": float(val),
                        "unit": item.get("unit") or "USD",
                        "reporting_period": {
                            "end_date": end_date,
                            "fiscal_year": fy,
                            "quarter": quarter,
                            "period_type": cadence,
                        },
                        "source": "comparative_filing_backfill",
                        "source_accession": source_acc,
                    }
                },
                upsert=True,
            )
            values_inserted += 1
            entry_inserted += 1

        if entry_inserted > 0:
            periods_touched += 1

    return {
        "periods_backfilled": periods_touched,
        "values_inserted": values_inserted,
        "dims_inserted": dims_inserted,
    }
