"""Structured per-run summary reports persisted to MongoDB and rendered on console.

Takes lightweight before/after snapshots of ``normalize_data`` (concept values
and guidance values) around each processing stage (e.g. ``8-K`` pass, then
``10-K/10-Q`` pass) so every run produces a 100% accurate, deterministic
breakdown of:

  • Concepts newly inserted by each form
  • Concepts upserted / updated (e.g. 8-K preliminary → 10-Q/10-K official)
  • Concepts retained as-is from 8-K / 6-K because 10-Q / 10-K did not provide them
  • Concepts deleted (e.g. via ``--replace``)
  • Guidance inserted, updated/superseded, retained from 8-K / 6-K, or deleted

The exact report document is saved to ``normalize_data.gotham_run_reports`` and
rendered identically to the console (and appended to the run's log file).
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

_OFFICIAL_XBRL_FORMS = {"10-K", "10-Q", "20-F", "10-K/A", "10-Q/A", "20-F/A"}
_EARNINGS_FORMS = {"8-K", "6-K", "8-K/A", "6-K/A"}


def _cik_variants(cik: str) -> list[str]:
    raw = str(cik or "").strip()
    if not raw:
        return []
    padded = raw.zfill(10) if raw.isdigit() else raw
    unpadded = raw.lstrip("0") or "0"
    return list(dict.fromkeys([raw, padded, unpadded]))


def _quarter_to_int(quarter: Any) -> Optional[int]:
    if quarter is None:
        return None
    if isinstance(quarter, int):
        return quarter
    s = str(quarter).strip().upper()
    if s.startswith("Q") and s[1:].isdigit():
        return int(s[1:])
    if s.isdigit():
        return int(s)
    return None


def _fmt_period(fy: Any, q: Any, cadence: str = "") -> str:
    if fy is None:
        return "?"
    q_int = _quarter_to_int(q)
    if q_int is not None:
        return f"FY{fy} Q{q_int}"
    return f"FY{fy}"


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa = float(a)
        fb = float(b)
        if math.isnan(fa) and math.isnan(fb):
            return True
        return math.isclose(fa, fb, rel_tol=1e-9, abs_tol=1e-6)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _fmt_val(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            return f"{int(v):,}"
        if abs(v) >= 1000:
            return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
        return f"{v:g}"
    return str(v)


def _clean_label(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    return first_line[:90]


def get_normalize_db() -> Any:
    """Return the ``normalize_data`` PyMongo database handle."""
    from pymongo import MongoClient
    from filings_agent.config import DATABASE_NAME, MONGODB_URI

    return MongoClient(MONGODB_URI)[DATABASE_NAME]


@dataclass
class CompanyStateSnapshot:
    """Point-in-time snapshot of one company's stored concept & guidance values."""

    cik: str
    concepts: dict[tuple[str, str, Any, Any], dict[str, Any]] = field(default_factory=dict)
    guidance: dict[tuple[str, str, Any, Any, str], dict[str, Any]] = field(default_factory=dict)


def snapshot_company_state(
    db: Any,
    cik: str,
    *,
    fiscal_year: Optional[int] = None,
    quarter: Optional[str | int] = None,
    statement_type: Optional[str] = None,
) -> CompanyStateSnapshot:
    """Capture current concept values and guidance values for *cik* in *db*."""
    snap = CompanyStateSnapshot(cik=str(cik).zfill(10) if str(cik).isdigit() else str(cik))
    if db is None:
        return snap

    cik_vars = _cik_variants(cik)
    if not cik_vars:
        return snap

    q_int = _quarter_to_int(quarter)

    # 1. Load concept metadata maps for readable concept/label names
    concept_meta: dict[str, dict[str, str]] = {}
    for col_name in ("normalized_concepts_quarterly", "normalized_concepts_annual"):
        try:
            for doc in db[col_name].find(
                {"cik": {"$in": cik_vars}},
                {"concept": 1, "label": 1, "statement_type": 1},
            ):
                cid = str(doc.get("_id"))
                concept_meta[cid] = {
                    "concept": str(doc.get("concept") or ""),
                    "label": _clean_label(doc.get("label")),
                    "statement_type": str(doc.get("statement_type") or ""),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_report: concept meta lookup failed on %s: %s", col_name, exc)

    # 2. Snapshot concept_values_quarterly and concept_values_annual
    val_collections = [
        ("quarterly", "concept_values_quarterly"),
        ("annual", "concept_values_annual"),
    ]
    for cadence, col_name in val_collections:
        # If a specific quarter is requested, skip annual unless Q4 (which comes from 10-K/20-F)
        if q_int is not None and cadence == "annual" and q_int != 4:
            continue
        filt: dict[str, Any] = {"cik": {"$in": cik_vars}}
        if fiscal_year is not None:
            filt["reporting_period.fiscal_year"] = int(fiscal_year)
        if cadence == "quarterly" and q_int is not None:
            filt["reporting_period.quarter"] = q_int
        if statement_type:
            filt["statement_type"] = statement_type

        try:
            for doc in db[col_name].find(
                filt,
                {
                    "concept_id": 1,
                    "statement_type": 1,
                    "form_type": 1,
                    "reporting_period": 1,
                    "value": 1,
                    "earning_data": 1,
                    "accession_number": 1,
                    "currency": 1,
                    "calculated": 1,
                },
            ):
                cid = str(doc.get("concept_id") or "")
                rp = doc.get("reporting_period") or {}
                fy = rp.get("fiscal_year")
                q = rp.get("quarter") if cadence == "quarterly" else None
                meta = concept_meta.get(cid, {})
                stmt = str(doc.get("statement_type") or meta.get("statement_type") or "")
                form_type = str(doc.get("form_type") or "")
                is_earning = bool(doc.get("earning_data")) or (
                    form_type.upper() in _EARNINGS_FORMS
                )
                key = (cadence, cid, fy, q)
                snap.concepts[key] = {
                    "concept_id": cid,
                    "concept": meta.get("concept") or cid,
                    "label": meta.get("label") or meta.get("concept") or cid,
                    "statement_type": stmt,
                    "cadence": cadence,
                    "fiscal_year": fy,
                    "quarter": q,
                    "period": _fmt_period(fy, q, cadence),
                    "value": doc.get("value"),
                    "form_type": form_type or ("8-K" if is_earning else "XBRL"),
                    "earning_data": is_earning,
                    "accession_number": str(doc.get("accession_number") or ""),
                    "currency": str(doc.get("currency") or ""),
                    "calculated": bool(doc.get("calculated")),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_report: value snapshot failed on %s: %s", col_name, exc)

    # 3. Snapshot guidance_values (current records per metric/basis/period)
    try:
        g_filt: dict[str, Any] = {"cik": {"$in": cik_vars}}
        for doc in db["guidance_values"].find(g_filt):
            is_cur = doc.get("is_current", True)
            if not is_cur:
                continue
            period = doc.get("period") or {}
            fy = period.get("fiscal_year")
            q = period.get("quarter")
            ptype = str(doc.get("period_type") or period.get("period_type") or "")
            metric = str(doc.get("metric") or "").strip().lower()
            basis = str(doc.get("basis") or "gaap").strip().lower()
            if not metric:
                continue
            key = (metric, basis, fy, q, ptype)
            form_type = str(doc.get("form_type") or doc.get("source_form") or "")
            # Format compact value representation for guidance
            val_repr = _format_guidance_value(doc)
            snap.guidance[key] = {
                "metric": metric,
                "label": str(doc.get("standard_label") or doc.get("label") or metric),
                "basis": basis,
                "fiscal_year": fy,
                "quarter": q,
                "period_type": ptype,
                "period": _fmt_period(fy, q, ptype),
                "value": val_repr,
                "midpoint": doc.get("midpoint"),
                "low": doc.get("low"),
                "high": doc.get("high"),
                "form_type": form_type,
                "accession_number": str(doc.get("accession_number") or ""),
                "source": str(doc.get("source") or "llm"),
                "filing_period": doc.get("filing_period"),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("run_report: guidance snapshot failed: %s", exc)

    return snap


def _format_guidance_value(doc: dict[str, Any]) -> str:
    low = doc.get("low")
    high = doc.get("high")
    mid = doc.get("midpoint")
    val = doc.get("value")
    unit = str(doc.get("unit") or doc.get("currency") or "").strip()
    suffix = f" {unit}" if unit else ""
    if low is not None and high is not None and not _values_equal(low, high):
        return f"{_fmt_val(low)}–{_fmt_val(high)}{suffix}"
    if mid is not None:
        return f"{_fmt_val(mid)}{suffix}"
    if val is not None:
        return f"{_fmt_val(val)}{suffix}"
    if low is not None:
        return f"{_fmt_val(low)}{suffix}"
    if high is not None:
        return f"{_fmt_val(high)}{suffix}"
    return str(doc.get("raw_text") or doc.get("qualitative") or "n/a")[:60]


def diff_stage(
    before: CompanyStateSnapshot,
    after: CompanyStateSnapshot,
    *,
    stage_name: str,
    stage_forms: Sequence[str],
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    """Compare *before* and *after* snapshots for a single pipeline stage."""
    forms_upper = [str(f).upper() for f in stage_forms]
    is_official_xbrl = any(f in _OFFICIAL_XBRL_FORMS for f in forms_upper)

    # ── Concept value diff ────────────────────────────────────────────────
    c_inserted: list[dict[str, Any]] = []
    c_updated: list[dict[str, Any]] = []
    c_deleted: list[dict[str, Any]] = []
    c_retained: list[dict[str, Any]] = []
    c_unchanged = 0

    touched_periods: set[tuple[str, Any, Any]] = set()

    for key, cur in after.concepts.items():
        prev = before.concepts.get(key)
        if prev is None:
            c_inserted.append(
                {
                    "concept": cur["concept"],
                    "label": cur["label"],
                    "statement_type": cur["statement_type"],
                    "period": cur["period"],
                    "value": cur["value"],
                    "form_type": cur["form_type"],
                    "earning_data": cur["earning_data"],
                }
            )
            touched_periods.add((cur["cadence"], cur["fiscal_year"], cur["quarter"]))
        else:
            val_changed = not _values_equal(prev["value"], cur["value"])
            form_changed = prev["form_type"] != cur["form_type"]
            earn_changed = bool(prev["earning_data"]) != bool(cur["earning_data"])
            if val_changed or form_changed or earn_changed:
                c_updated.append(
                    {
                        "concept": cur["concept"],
                        "label": cur["label"],
                        "statement_type": cur["statement_type"],
                        "period": cur["period"],
                        "old_value": prev["value"],
                        "new_value": cur["value"],
                        "old_form": prev["form_type"],
                        "new_form": cur["form_type"],
                    }
                )
                touched_periods.add((cur["cadence"], cur["fiscal_year"], cur["quarter"]))
            else:
                c_unchanged += 1

    for key, prev in before.concepts.items():
        if key not in after.concepts:
            c_deleted.append(
                {
                    "concept": prev["concept"],
                    "label": prev["label"],
                    "statement_type": prev["statement_type"],
                    "period": prev["period"],
                    "old_value": prev["value"],
                    "old_form": prev["form_type"],
                }
            )
            touched_periods.add((prev["cadence"], prev["fiscal_year"], prev["quarter"]))

    # Retained as-is from 8-K / 6-K after an official 10-K / 10-Q / 20-F pass
    if is_official_xbrl:
        for key, cur in after.concepts.items():
            if not cur.get("earning_data"):
                continue
            period_sig = (cur["cadence"], cur["fiscal_year"], cur["quarter"])
            if touched_periods and period_sig not in touched_periods:
                continue
            c_retained.append(
                {
                    "concept": cur["concept"],
                    "label": cur["label"],
                    "statement_type": cur["statement_type"],
                    "period": cur["period"],
                    "value": cur["value"],
                    "form_type": cur["form_type"],
                }
            )

    # Sort lists deterministically by (period, statement_type, label)
    _sort_c = lambda r: (str(r.get("period", "")), str(r.get("statement_type", "")), str(r.get("label", "")))
    c_inserted.sort(key=_sort_c)
    c_updated.sort(key=_sort_c)
    c_deleted.sort(key=_sort_c)
    c_retained.sort(key=_sort_c)

    # ── Guidance diff ─────────────────────────────────────────────────────
    g_inserted: list[dict[str, Any]] = []
    g_updated: list[dict[str, Any]] = []
    g_deleted: list[dict[str, Any]] = []
    g_retained: list[dict[str, Any]] = []
    g_unchanged = 0

    for key, cur in after.guidance.items():
        prev = before.guidance.get(key)
        if prev is None:
            g_inserted.append(
                {
                    "metric": cur["metric"],
                    "label": cur["label"],
                    "basis": cur["basis"],
                    "period": cur["period"],
                    "value": cur["value"],
                    "form_type": cur["form_type"],
                }
            )
        else:
            changed = (
                prev["value"] != cur["value"]
                or prev["form_type"] != cur["form_type"]
                or prev["accession_number"] != cur["accession_number"]
            )
            if changed:
                g_updated.append(
                    {
                        "metric": cur["metric"],
                        "label": cur["label"],
                        "basis": cur["basis"],
                        "period": cur["period"],
                        "old_value": prev["value"],
                        "new_value": cur["value"],
                        "old_form": prev["form_type"],
                        "new_form": cur["form_type"],
                    }
                )
            else:
                g_unchanged += 1

    for key, prev in before.guidance.items():
        if key not in after.guidance:
            g_deleted.append(
                {
                    "metric": prev["metric"],
                    "label": prev["label"],
                    "basis": prev["basis"],
                    "period": prev["period"],
                    "old_value": prev["value"],
                    "old_form": prev["form_type"],
                }
            )

    if is_official_xbrl:
        for key, cur in after.guidance.items():
            if cur.get("form_type", "").upper() in _EARNINGS_FORMS:
                g_retained.append(
                    {
                        "metric": cur["metric"],
                        "label": cur["label"],
                        "basis": cur["basis"],
                        "period": cur["period"],
                        "value": cur["value"],
                        "form_type": cur["form_type"],
                    }
                )

    _sort_g = lambda r: (str(r.get("period", "")), str(r.get("metric", "")), str(r.get("basis", "")))
    g_inserted.sort(key=_sort_g)
    g_updated.sort(key=_sort_g)
    g_deleted.sort(key=_sort_g)
    g_retained.sort(key=_sort_g)

    return {
        "stage": stage_name,
        "forms": list(stage_forms),
        "duration_seconds": round(float(duration_seconds), 2),
        "concepts": {
            "inserted_count": len(c_inserted),
            "updated_count": len(c_updated),
            "deleted_count": len(c_deleted),
            "retained_from_earnings_count": len(c_retained),
            "unchanged_count": c_unchanged,
            "inserted": c_inserted,
            "updated": c_updated,
            "deleted": c_deleted,
            "retained_from_earnings": c_retained,
        },
        "guidance": {
            "inserted_count": len(g_inserted),
            "updated_count": len(g_updated),
            "deleted_count": len(g_deleted),
            "retained_from_earnings_count": len(g_retained),
            "unchanged_count": g_unchanged,
            "inserted": g_inserted,
            "updated": g_updated,
            "deleted": g_deleted,
            "retained_from_earnings": g_retained,
        },
    }


def build_run_report(
    *,
    serial_number: str,
    ticker: str,
    cik: str,
    forms_requested: str,
    mode: str,
    log_file: str,
    started_at_utc: datetime,
    finished_at_utc: datetime,
    stages: list[dict[str, Any]],
    target_fiscal_year: Optional[int] = None,
    target_quarter: Optional[str] = None,
    target_statement: Optional[str] = None,
    status: str = "success",
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the complete structured run report document."""
    duration = max(0.0, (finished_at_utc - started_at_utc).total_seconds())
    totals = {
        "concepts_inserted": sum(s["concepts"]["inserted_count"] for s in stages),
        "concepts_updated": sum(s["concepts"]["updated_count"] for s in stages),
        "concepts_deleted": sum(s["concepts"]["deleted_count"] for s in stages),
        "concepts_retained_from_earnings": (
            stages[-1]["concepts"]["retained_from_earnings_count"] if stages else 0
        ),
        "guidance_inserted": sum(s["guidance"]["inserted_count"] for s in stages),
        "guidance_updated": sum(s["guidance"]["updated_count"] for s in stages),
        "guidance_deleted": sum(s["guidance"]["deleted_count"] for s in stages),
        "guidance_retained_from_earnings": (
            stages[-1]["guidance"]["retained_from_earnings_count"] if stages else 0
        ),
    }
    return {
        "run_id": run_id or uuid.uuid4().hex[:12],
        "serial_number": serial_number,
        "ticker": ticker,
        "cik": cik,
        "forms_requested": forms_requested,
        "mode": mode,
        "target_fiscal_year": target_fiscal_year,
        "target_quarter": target_quarter,
        "target_statement": target_statement,
        "started_at_utc": started_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at_utc": finished_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round(duration, 2),
        "log_file": str(log_file),
        "status": status,
        "stages": stages,
        "totals": totals,
    }


def save_run_report(db: Any, report: dict[str, Any]) -> Optional[str]:
    """Insert *report* into ``normalize_data.gotham_run_reports`` and return its ``_id`` string."""
    if db is None:
        return None
    try:
        doc = dict(report)
        doc.pop("_id", None)
        res = db["gotham_run_reports"].insert_one(doc)
        oid = str(res.inserted_id)
        report["_id"] = oid
        return oid
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_report: failed to save report to MongoDB: %s", exc)
        return None


def format_run_report_text(report: dict[str, Any]) -> str:
    """Format the exact DB report document into a clean, readable plain-text summary."""
    lines: list[str] = []
    sep = "=" * 78
    thin = "-" * 78

    period_str = (
        f"FY{report['target_fiscal_year']}"
        + (f" {report['target_quarter']}" if report.get("target_quarter") else "")
        if report.get("target_fiscal_year")
        else "all/range"
    )
    if report.get("target_statement"):
        period_str += f" ({report['target_statement']})"

    lines.append("")
    lines.append(sep)
    lines.append(
        f"RUN SUMMARY  #{report.get('serial_number', '?')}  |  "
        f"{report.get('ticker', '?')} ({report.get('cik', '?')})  |  "
        f"form={report.get('forms_requested', '?')}  |  "
        f"period={period_str}  |  mode={report.get('mode', 'default')}"
    )
    lines.append(
        f"Status: {report.get('status', '?')}  |  "
        f"Duration: {report.get('duration_seconds', 0):.1f}s  |  "
        f"UTC: {report.get('started_at_utc', '')} → {report.get('finished_at_utc', '')}"
    )
    lines.append(f"Log file : {report.get('log_file', '-')}")
    if report.get("_id"):
        lines.append(f"DB report: normalize_data.gotham_run_reports ({report['_id']})")
    lines.append(sep)

    # Compact stage overview table
    lines.append(
        f"{'Stage':<14} {'Inserted':>8} {'Upserted':>8} {'Retained':>8} "
        f"{'Deleted':>8} {'Unchanged':>9}   {'Guidance (Ins/Upd/Ret)':<22}"
    )
    lines.append(thin)
    for st in report.get("stages") or []:
        c = st["concepts"]
        g = st["guidance"]
        g_str = f"{g['inserted_count']}/{g['updated_count']}/{g['retained_from_earnings_count']}"
        lines.append(
            f"{st['stage']:<14} {c['inserted_count']:>8} {c['updated_count']:>8} "
            f"{c['retained_from_earnings_count']:>8} {c['deleted_count']:>8} "
            f"{c['unchanged_count']:>9}   {g_str:<22}"
        )
    lines.append(thin)

    # Detailed per-stage breakdown
    for st in report.get("stages") or []:
        c = st["concepts"]
        g = st["guidance"]
        lines.append(f"\n[{st['stage']}] ({st.get('duration_seconds', 0):.1f}s)")

        if c["inserted"]:
            lines.append(f"  + Newly inserted concepts ({len(c['inserted'])}):")
            for item in c["inserted"]:
                lines.append(
                    f"      [{item['statement_type'] or '?'}] {item['label']} ({item['concept']}) "
                    f"[{item['period']}] = {_fmt_val(item['value'])}  ({item['form_type']})"
                )

        if c["updated"]:
            lines.append(f"  ~ Upserted / updated concepts ({len(c['updated'])}):")
            for item in c["updated"]:
                lines.append(
                    f"      [{item['statement_type'] or '?'}] {item['label']} ({item['concept']}) "
                    f"[{item['period']}]: {_fmt_val(item['old_value'])} ({item['old_form']}) "
                    f"→ {_fmt_val(item['new_value'])} ({item['new_form']})"
                )

        if c["retained_from_earnings"]:
            lines.append(
                f"  = Retained as-is from 8-K/6-K (missing in {st['stage']}) "
                f"({len(c['retained_from_earnings'])}):"
            )
            for item in c["retained_from_earnings"]:
                lines.append(
                    f"      [{item['statement_type'] or '?'}] {item['label']} ({item['concept']}) "
                    f"[{item['period']}] = {_fmt_val(item['value'])}  (retained from {item['form_type']})"
                )

        if c["deleted"]:
            lines.append(f"  - Deleted concepts ({len(c['deleted'])}):")
            for item in c["deleted"]:
                lines.append(
                    f"      [{item['statement_type'] or '?'}] {item['label']} ({item['concept']}) "
                    f"[{item['period']}] was {_fmt_val(item['old_value'])} ({item['old_form']})"
                )

        if g["inserted"]:
            lines.append(f"  + Guidance inserted ({len(g['inserted'])}):")
            for item in g["inserted"]:
                lines.append(
                    f"      {item['label']} [{item['basis']}, {item['period']}] = {item['value']}  ({item['form_type']})"
                )

        if g["updated"]:
            lines.append(f"  ~ Guidance upserted / updated ({len(g['updated'])}):")
            for item in g["updated"]:
                lines.append(
                    f"      {item['label']} [{item['basis']}, {item['period']}]: "
                    f"{item['old_value']} ({item['old_form']}) → {item['new_value']} ({item['new_form']})"
                )

        if g["retained_from_earnings"]:
            lines.append(
                f"  = Guidance retained as-is from 8-K/6-K (missing in {st['stage']}) "
                f"({len(g['retained_from_earnings'])}):"
            )
            for item in g["retained_from_earnings"]:
                lines.append(
                    f"      {item['label']} [{item['basis']}, {item['period']}] = {item['value']}  (retained from {item['form_type']})"
                )

        if g["deleted"]:
            lines.append(f"  - Guidance deleted ({len(g['deleted'])}):")
            for item in g["deleted"]:
                lines.append(
                    f"      {item['label']} [{item['basis']}, {item['period']}] was {item['old_value']} ({item['old_form']})"
                )

        if (
            not c["inserted"]
            and not c["updated"]
            and not c["retained_from_earnings"]
            and not c["deleted"]
            and not g["inserted"]
            and not g["updated"]
            and not g["retained_from_earnings"]
            and not g["deleted"]
        ):
            lines.append(f"  (no changes — {c['unchanged_count']} concept(s), {g['unchanged_count']} guidance unchanged)")

    lines.append(sep)
    return "\n".join(lines)


def print_run_report(report: dict[str, Any], *, console: Any = None) -> str:
    """Print the formatted run report to console and return the rendered text."""
    text = format_run_report_text(report)
    if console is not None:
        try:
            console.print(text, markup=False, highlight=False)
            return text
        except Exception:
            pass
    print(text)
    return text
