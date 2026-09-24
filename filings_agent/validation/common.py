"""Shared helpers for the validation checks.

The checks operate on duck-typed ``StatementBundle`` objects (they only read
``statement_type``, ``form_type``, ``reporting_period``, ``concepts``,
``values``) so this package has no import dependency on the normalization
service.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional


def numeric_items(bundle: Any) -> list[dict]:
    """Concrete items of a bundle that carry a real numeric value."""
    out = []
    for item in (getattr(bundle, "concepts", None) or []):
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.append(item)
    return out


def concept_value_map(bundle: Any) -> dict[str, float]:
    """Map ``{concept: value}`` for the bundle's numeric items.

    When the same concept appears more than once the FIRST occurrence wins —
    the persistence helpers have the same first-wins semantics, so validation
    sees what would actually be written.
    """
    vmap: dict[str, float] = {}
    for item in numeric_items(bundle):
        concept = item.get("concept")
        if concept and concept not in vmap:
            vmap[concept] = float(item["value"])
    return vmap


def local_name(concept: str) -> str:
    """Return the local part of a QName (``us-gaap:Assets`` → ``Assets``)."""
    return concept.split(":", 1)[-1] if concept else ""


def parse_date(value: Any) -> Optional[date]:
    """Parse a date-ish value into a ``date``; ``None`` when unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Fast path: leading YYYY-MM-DD (covers "2024-12-31" and
        # "2024-01-01 00:00:00").
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def duration_days(period: Any) -> Optional[int]:
    """Days covered by an XBRL period string (``A to B``); ``None`` if instant/unknown."""
    if not isinstance(period, str) or " to " not in period:
        return None
    start_str, end_str = period.split(" to ", 1)
    start, end = parse_date(start_str), parse_date(end_str)
    if start is None or end is None:
        return None
    return (end - start).days


def statement_label(bundle: Any) -> str:
    """Human-readable statement identifier used in finding messages."""
    return getattr(bundle, "statement_type", None) or "unknown"


def iter_bundles(bundles: Iterable[Any]) -> list[Any]:
    return [b for b in (bundles or []) if b is not None]
