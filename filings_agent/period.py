"""Reporting-period contract for the filings agent.

The 8-K agent derives its period by reading the filing (a period agent).  The
filings-extractor already knows the period deterministically from XBRL metadata,
so this module only provides:

* :class:`DetectedPeriod` — the same frozen contract the ported guidance stack
  consumes, and
* :func:`build_detected_period` — build it from a bundle's ``reporting_period``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


@dataclass(frozen=True)
class DetectedPeriod:
    """Canonical reporting-period contract (shared with the guidance stack)."""

    period_type: str
    period_end: date
    quarter: int | None
    period_label: str | None
    fiscal_year: int


def parse_period_end(raw: Any) -> Optional[date]:
    """Parse a date-ish period end (``date``/``datetime``/ISO string)."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def format_period_label(period: DetectedPeriod) -> str:
    """Render the human-readable label for a detected period."""
    if period.quarter is not None:
        return f"FY{period.fiscal_year} Q{period.quarter}"
    return f"FY{period.fiscal_year} (annual)"


def build_detected_period(
    reporting_period: dict[str, Any] | None,
    *,
    form_type: str = "",
) -> Optional[DetectedPeriod]:
    """Build a :class:`DetectedPeriod` from a bundle's ``reporting_period``.

    Returns ``None`` when there is no usable period end — callers treat that as
    "guidance extraction unavailable for this filing", never a run failure.
    """
    reporting_period = reporting_period or {}
    period_end = parse_period_end(
        reporting_period.get("end_date") or reporting_period.get("period_date")
    )
    if period_end is None:
        return None

    quarter = reporting_period.get("quarter")
    try:
        quarter = int(quarter) if quarter not in (None, "") else None
    except (TypeError, ValueError):
        quarter = None

    fiscal_year = reporting_period.get("fiscal_year")
    try:
        fiscal_year = int(fiscal_year) if fiscal_year not in (None, "") else None
    except (TypeError, ValueError):
        fiscal_year = None
    if fiscal_year is None:
        fiscal_year = period_end.year

    declared = str(reporting_period.get("period_type") or "").lower()
    if declared in ("annual", "quarterly", "interim"):
        period_type = "annual" if declared == "annual" else "quarterly"
    elif (form_type or "").upper() == "10-K":
        period_type = "annual"
    elif (form_type or "").upper() == "10-Q":
        period_type = "quarterly"
    else:
        period_type = "quarterly" if quarter else "annual"

    if period_type == "annual":
        quarter = None

    label = reporting_period.get("period_label")
    if not label:
        label = (
            f"Fiscal Year Ended {period_end.isoformat()}"
            if period_type == "annual"
            else f"Three Months Ended {period_end.isoformat()}"
        )

    return DetectedPeriod(
        period_type=period_type,
        period_end=period_end,
        quarter=quarter,
        period_label=label,
        fiscal_year=fiscal_year,
    )
