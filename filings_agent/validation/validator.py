"""Validate stage — orchestrates the deterministic checks into a report.

Pure: given the in-memory bundles it returns a :class:`ValidationReport`.  No
database access happens here (the validate *node* decides whether to persist
the report).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .findings import Finding
from .math import check_math
from .periods import check_periods
from .plausibility import check_plausibility
from .report import ValidationReport
from .structure import check_structure

# Ordered so report/console output is stable.
CHECKS = (
    ("structure", check_structure),
    ("math", check_math),
    ("periods", check_periods),
    ("plausibility", check_plausibility),
)


def validate_bundles(
    bundles: Iterable[Any],
    *,
    cik: str,
    ticker: str = "",
    form_type: str = "",
    accession_number: Optional[str] = None,
) -> ValidationReport:
    """Run every check across every statement bundle and build a report."""
    bundle_list = [b for b in (bundles or []) if b is not None]

    findings: list[Finding] = []
    checks_run = {name: 0 for name, _ in CHECKS}

    for bundle in bundle_list:
        for name, check in CHECKS:
            findings.extend(check(bundle))
            checks_run[name] += 1

    return ValidationReport(
        cik=cik,
        ticker=ticker or cik,
        form_type=form_type,
        accession_number=accession_number,
        statement_types=[
            getattr(b, "statement_type", "unknown") for b in bundle_list
        ],
        findings=findings,
        checks_run=checks_run,
    )
