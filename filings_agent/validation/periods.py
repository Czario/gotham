"""Reporting-period checks.

The period is produced upstream (SEC metadata + the fiscal-year calculator), so
these checks are mostly a sanity net: is there a usable period end, does the
declared period type match the form, and is any value's duration absurd?
"""
from __future__ import annotations

from typing import Any

from .common import duration_days, parse_date, statement_label
from .findings import LOW, MEDIUM, Finding

# Durations (days) considered plausible.  A 10-K covers ~365 days, a 10-Q
# income statement ~90 and its year-to-date cash flow ~180–270.  Anything
# outside this window is almost certainly a period-parse error.
_MIN_DURATION = 30
_MAX_DURATION = 400


def check_periods(bundle: Any) -> list[Finding]:
    findings: list[Finding] = []
    st = statement_label(bundle)
    reporting_period = getattr(bundle, "reporting_period", None) or {}
    form_type = (getattr(bundle, "form_type", "") or "").upper()

    end_raw = reporting_period.get("end_date") or reporting_period.get("period_date")
    if not end_raw:
        findings.append(
            Finding(
                "missing_period_end",
                MEDIUM,
                f"{st}: reporting period has no end_date/period_date",
                statement_type=st,
                evidence={"reporting_period": dict(reporting_period)},
            )
        )
    elif parse_date(end_raw) is None:
        findings.append(
            Finding(
                "unparseable_period_end",
                MEDIUM,
                f"{st}: reporting period end_date {end_raw!r} is not a valid date",
                statement_type=st,
            )
        )

    if reporting_period.get("fiscal_year") in (None, ""):
        findings.append(
            Finding(
                "missing_fiscal_year",
                LOW,
                f"{st}: reporting period has no fiscal_year",
                statement_type=st,
            )
        )

    period_type = str(reporting_period.get("period_type") or "").lower()
    if period_type:
        if form_type == "10-K" and period_type != "annual":
            findings.append(
                Finding(
                    "period_type_form_mismatch",
                    MEDIUM,
                    f"{st}: 10-K carries period_type={period_type!r} (expected 'annual')",
                    statement_type=st,
                )
            )
        elif form_type == "10-Q" and period_type not in ("quarterly", "interim"):
            findings.append(
                Finding(
                    "period_type_form_mismatch",
                    MEDIUM,
                    f"{st}: 10-Q carries period_type={period_type!r} "
                    f"(expected 'quarterly'/'interim')",
                    statement_type=st,
                )
            )

    # Duration sanity — report at most one finding per bundle.
    odd: list[dict] = []
    for item in (getattr(bundle, "concepts", None) or []):
        if not isinstance(item, dict):
            continue
        days = duration_days(item.get("period"))
        if days is None:
            continue
        if days < _MIN_DURATION or days > _MAX_DURATION:
            odd.append({"concept": item.get("concept"), "days": days})
    if odd:
        findings.append(
            Finding(
                "unusual_period_duration",
                LOW,
                f"{st}: {len(odd)} value(s) cover an unusual duration "
                f"(first: {odd[0]['days']} days)",
                statement_type=st,
                evidence={"samples": odd[:5]},
            )
        )

    return findings
