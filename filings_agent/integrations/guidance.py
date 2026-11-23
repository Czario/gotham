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

from filings_agent.agent.guidance import (
    _period_type_binary,
    period_arrived,
    score,
)

logger = logging.getLogger(__name__)


def _get_db() -> Any:
    """Return the target database handle (patchable in tests)."""
    from pymongo import MongoClient

    from filings_agent.config import DATABASE_NAME, MONGODB_URI

    return MongoClient(MONGODB_URI)[DATABASE_NAME]


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

    # Prefer the concept collection that matches the COVERED period.  A guidance
    # doc covering a full year must resolve to the ANNUAL concept row: the
    # actual lookup in _find_actual queries concept_values_annual with the
    # resolved concept id, so resolving to the quarterly row instead would miss
    # every annual actual (the 8-K stack never hit this because its guidance is
    # always quarterly).
    try:
        covered_type = _period_type_binary(guidance_doc.get("period") or {})
    except Exception:  # noqa: BLE001 — malformed period falls back to quarterly-first
        covered_type = None
    if covered_type == "annual":
        col_order = ("normalized_concepts_annual", "normalized_concepts_quarterly")
    else:
        col_order = ("normalized_concepts_quarterly", "normalized_concepts_annual")

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
            for col_name in col_order:
                for row in db[col_name].find({
                    "cik": cik, "statement_type": statement_type,
                }):
                    row_label = (row.get("label") or "").strip()
                    if row_label and _norm_label(row_label) == norm_label \
                            and row.get("concept"):
                        return {"name": row["concept"], "_id": row["_id"]}
            return None

    for col_name in col_order:
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
    }


def _canonical_cik(cik: str) -> str:
    """Canonical 10-digit zero-padded CIK when numeric."""
    s = str(cik or "").strip()
    return s.zfill(10) if s.isdigit() else s


def _cik_variants(cik: str) -> list[str]:
    """Return padded + unpadded CIK variants so legacy unpadded rows never duplicate."""
    s = str(cik or "").strip()
    if not s:
        return []
    if s.isdigit():
        padded = s.zfill(10)
        unpadded = s.lstrip("0") or "0"
        return list(dict.fromkeys([padded, unpadded, s]))
    return [s]


def _canonical_accession(accession_number: str | None) -> str | None:
    """Strip whitespace and trailing '-xbrl' so accession variants never duplicate."""
    if accession_number is None:
        return None
    s = str(accession_number).strip()
    if not s:
        return None
    return s[:-5] if s.endswith("-xbrl") else s


def _accession_variants(accession_number: str | None) -> list[str]:
    """Return clean + '-xbrl' variants for matching/sweeping existing docs."""
    clean = _canonical_accession(accession_number)
    if not clean:
        return []
    raw = str(accession_number).strip()
    return list(dict.fromkeys([clean, f"{clean}-xbrl", raw]))


def upsert_guidance_records(
    cik: str,
    records: list[dict[str, Any]],
    *,
    accession_number: str | None = None,
    form_type: str = "8-K",
    filing_period: Any = None,
    db: Any = None,
) -> dict[str, Any]:
    """Persist normalized guidance records into ``guidance_values``.

    Returns a summary dict (never raises on record-level problems).  Each
    record is stored EXACTLY in the backend schema shape (all ``_*`` internal
    keys stripped).  Idempotent across reruns (default pipeline, guidance-only,
    with or without --reload): never creates duplicate or triplicate rows for
    the same filing/metric/concept/period.
    """
    db = db if db is not None else _get_db()
    col = _guidance_col(db)
    now = datetime.now(tz=timezone.utc)

    cik_norm = _canonical_cik(cik)
    cik_vars = _cik_variants(cik) or [cik_norm]
    acc_norm = _canonical_accession(accession_number)
    acc_vars = _accession_variants(accession_number)

    filing_period_doc = None
    if filing_period is not None:
        filing_period_doc = {
            "fiscal_year": filing_period.fiscal_year,
            "quarter": filing_period.quarter,
            "period_end_date": filing_period.period_end.isoformat(),
        }

    summary: dict[str, Any] = {
        "n_records": len(records),
        "upserted": 0,
        "skipped_manual": 0,
        "skipped_duplicate": 0,
        "demoted": 0,
        "swept": 0,
        "superseded": [],
    }
    written_ids: list[Any] = []
    seen_metric_keys: set[tuple[Any, ...]] = set()
    seen_concept_keys: set[tuple[Any, ...]] = set()

    for rec in records:
        period = rec.get("period") or {}
        fy = period.get("fiscal_year")
        q = period.get("quarter")
        ptype = period.get("period_type")
        # Top-level binary class (quarterly|annual) — derived from period so
        # hand-built/legacy records can never persist without it, and the two
        # fields can never drift apart.  Normalized records already carry it.
        period_type_bin = (
            rec["period_type"]
            if rec.get("period_type") in ("quarterly", "annual")
            else _period_type_binary(period)
        )
        metric = str(rec.get("metric") or "").strip().lower()
        basis = str(rec.get("basis") or "gaap").strip().lower()
        if not metric or not isinstance(fy, int):
            continue

        # Within-batch metric deduplication
        metric_batch_key = (metric, basis, fy, q, period_type_bin)
        if metric_batch_key in seen_metric_keys:
            summary["skipped_duplicate"] += 1
            continue
        seen_metric_keys.add(metric_batch_key)

        key = {
            "cik": {"$in": cik_vars},
            "metric": metric,
            "basis": basis,
            "period.fiscal_year": fy,
            "period.quarter": q,
        }

        period_match_or: list[dict[str, Any]] = [{"period_type": period_type_bin}]
        if ptype:
            period_match_or.append({"period.period_type": ptype})

        # Manual protection — a human's hand-entered value is authoritative.
        manual = col.find_one({**key, "$or": period_match_or, "source": "manual"})
        if manual is not None:
            logger.info(
                "guidance: skipping LLM write for CIK %s metric=%s FY%s Q%s — "
                "source=manual exists",
                cik_norm, metric, fy, q,
            )
            summary["skipped_manual"] += 1
            continue

        # Supersede: any OTHER current doc for the same key is replaced.
        prev = col.find_one({**key, "is_current": True})
        supersedes = None
        if prev is not None:
            prev_acc = _canonical_accession(prev.get("accession_number"))
            if prev_acc and prev_acc != acc_norm:
                supersedes = prev_acc
        if prev is not None and supersedes is None and (acc_norm is None):
            supersedes = prev.get("_id")  # accessionless manual-URL runs still trace

        demote_acc_filter = {"$nin": acc_vars} if acc_vars else {"$ne": None}
        res = col.update_many(
            {**key, "is_current": True, "accession_number": demote_acc_filter},
            {"$set": {"is_current": False, "updated_at": now}},
        )
        summary["demoted"] += getattr(res, "modified_count", 0)

        doc = {k: v for k, v in rec.items() if not k.startswith("_")}
        doc["metric"] = metric
        doc["basis"] = basis
        doc["period_type"] = period_type_bin
        event_type = rec.get("event_type") or "initial"
        if supersedes and event_type == "initial":
            event_type = "updated"  # a replacement, even when the filing said initial
        doc.update({
            "cik": cik_norm,
            "accession_number": acc_norm,
            "form_type": form_type,
            "filing_period": filing_period_doc,
            "supersedes": supersedes,
            "is_current": True,
            "event_type": event_type,
            "source": "llm",
            "edited_by": None,
            "edited_at": None,
            "updated_at": now,
        })

        # concept must never stay null when the company HAS a matching concept
        # row — resolve the standard_label via the existing mapping vocabulary
        # (the same rows the backend enrichment joins against) and store the
        # concept NAME (direct-row match takes priority there).  The agent's
        # own concept (when provided) is authoritative and kept as-is.  The
        # key is ALWAYS present on the stored doc (null only when the company
        # genuinely has no matching row — never guessed).
        resolved_name: Any = None
        if not doc.get("concept"):
            try:
                resolved = _resolve_concept(db, doc)
                resolved_name = resolved["name"] if resolved else None
            except Exception as exc:  # noqa: BLE001 — one record's resolution
                # failure must never kill the whole guidance batch
                logger.warning("guidance concept resolution failed: %s", exc)
        doc["concept"] = doc.get("concept") or resolved_name or None

        # Within-batch concept deduplication (e.g. "revenue" and "total_revenue"
        # resolving to the same concept for the same period & basis).
        if doc["concept"]:
            concept_batch_key = (doc["concept"], basis, fy, q, period_type_bin)
            if concept_batch_key in seen_concept_keys:
                summary["skipped_duplicate"] += 1
                continue
            seen_concept_keys.add(concept_batch_key)

        # Match ANY existing source="llm" doc for this filing + metric/concept + period
        # across CIK/accession/period_type variants so reruns always update in place
        # and collapse any pre-existing duplicates.
        acc_query: Any = {"$in": acc_vars} if acc_vars else None
        identity_or: list[dict[str, Any]] = [{"metric": metric}]
        if doc["concept"]:
            identity_or.append({"concept": doc["concept"]})
        existing_query: dict[str, Any] = {
            "cik": {"$in": cik_vars},
            "accession_number": acc_query,
            "basis": basis,
            "period.fiscal_year": fy,
            "period.quarter": q,
            "source": "llm",
            "$and": [
                {"$or": identity_or},
                {"$or": period_match_or},
            ],
        }

        existing_docs: list[dict[str, Any]] = []
        if hasattr(col, "find"):
            try:
                existing_docs = [
                    d for d in col.find(existing_query, {"_id": 1})
                    if isinstance(d, dict) and d.get("_id") is not None
                ]
            except Exception:  # noqa: BLE001
                existing_docs = []

        up_id = None
        if existing_docs:
            target_id = existing_docs[0]["_id"]
            col.update_one(
                {"_id": target_id},
                {"$set": doc},
                upsert=False,
            )
            up_id = target_id
            # Immediately remove any extra pre-existing duplicate LLM docs for this key
            extra_ids = [d["_id"] for d in existing_docs[1:] if d["_id"] != target_id]
            if extra_ids and hasattr(col, "delete_many"):
                try:
                    del_dup = col.delete_many({"_id": {"$in": extra_ids}, "source": "llm"})
                    summary["swept"] += getattr(del_dup, "deleted_count", 0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("guidance duplicate collapse skipped: %s", exc)
        else:
            filt = {
                "cik": cik_norm,
                "accession_number": acc_norm,
                "metric": metric,
                "basis": basis,
                "period.fiscal_year": fy,
                "period.quarter": q,
                "period_type": period_type_bin,
                "source": "llm",
            }
            up_res = col.update_one(
                filt,
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            up_id = getattr(up_res, "upserted_id", None)
            if up_id is None:
                existing = col.find_one(filt, {"_id": 1})
                if isinstance(existing, dict):
                    up_id = existing.get("_id")

        if up_id is not None:
            written_ids.append(up_id)
        summary["upserted"] += 1
        if supersedes:
            summary["superseded"].append(supersedes)

    # Sweep stale or duplicate source="llm" docs for this same filing so
    # reruns (even if the LLM renames a metric or drops an item) never leave
    # duplicate guidance rows in guidance_values.  Manual entries are untouched.
    if acc_vars and written_ids and hasattr(col, "delete_many"):
        try:
            del_res = col.delete_many({
                "cik": {"$in": cik_vars},
                "accession_number": {"$in": acc_vars},
                "source": "llm",
                "_id": {"$nin": written_ids},
            })
            swept = getattr(del_res, "deleted_count", 0)
            summary["swept"] += swept
            if swept:
                logger.info(
                    "guidance: swept %d stale/duplicate LLM doc(s) for CIK %s %s",
                    swept, cik_norm, acc_norm,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("guidance stale sweep skipped: %s", exc)

    return summary


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
    summary: dict[str, Any] = {"checked": 0, "scored": 0, "unresolved": 0, "errors": 0}

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
            result = score(d, float(actual["value"]), actual.get("accession_number"))
            col.update_one(
                {"_id": d["_id"]},
                {"$set": {"result": result, "updated_at": now}},
            )
            summary["scored"] += 1
        except Exception as exc:  # noqa: BLE001 — scoring never fails a run
            logger.warning("guidance scoring error for CIK %s: %s", cik, exc)
            summary["errors"] += 1

    return summary