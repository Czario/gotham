"""Cross-statement validation checks.

Verifies consistency across statements in the same filing (e.g. Net Income
on the Income Statement matching Net Income on the Cash Flow Statement).
"""
from __future__ import annotations

from typing import Any

from .findings import HIGH, Finding


def check_cross_statement_consistency(bundles: list[Any]) -> list[Finding]:
    """Check consistency of shared concepts across different statements."""
    findings: list[Finding] = []

    income_net_income: dict[str, float] = {}
    cashflow_net_income: dict[str, float] = {}

    for bundle in bundles or []:
        st = (getattr(bundle, "statement_type", "") or "").lower()
        is_income = "income" in st and "comprehensive" not in st
        is_cf = "cash" in st or "flow" in st

        for item in getattr(bundle, "concepts", None) or []:
            if not isinstance(item, dict):
                continue
            concept = item.get("concept") or ""
            val = item.get("value")
            period = item.get("period") or ""

            if "NetIncomeLoss" in concept and isinstance(val, (int, float)):
                if is_income:
                    income_net_income[period] = float(val)
                elif is_cf:
                    cashflow_net_income[period] = float(val)

    # Check common periods
    common_periods = set(income_net_income.keys()) & set(cashflow_net_income.keys())
    for period in common_periods:
        inc_val = income_net_income[period]
        cf_val = cashflow_net_income[period]
        if abs(inc_val - cf_val) > 1.0:  # Allow $1 rounding difference
            findings.append(
                Finding(
                    type="cross_statement_math_mismatch",
                    severity=HIGH,
                    message=(
                        f"Net Income mismatch between Income Statement ({inc_val:,.0f}) "
                        f"and Cash Flow Statement ({cf_val:,.0f}) for period {period}."
                    ),
                    evidence={
                        "period": period,
                        "income_statement_value": inc_val,
                        "cash_flow_statement_value": cf_val,
                        "discrepancy": abs(inc_val - cf_val),
                    },
                )
            )

    return findings
