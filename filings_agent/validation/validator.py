"""Validate stage — orchestrates the deterministic checks into a report.

Pure: given the in-memory bundles it returns a :class:`ValidationReport`.  No
database access happens here (the validate *node* decides whether to persist
the report).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .coverage import check_coverage
from .cross_statement import check_cross_statement_consistency
from .findings import Finding
from .math import check_math
from .periods import check_periods
from .plausibility import check_plausibility
from .report import ValidationReport
from .sec_presentation_diff import check_sec_presentation_diff
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
    sec_presentation: Optional[dict[str, Any]] = None,
    include_cross_statement: bool = False,
) -> ValidationReport:
    """Run every check across every statement bundle and build a report."""
    bundle_list = [b for b in (bundles or []) if b is not None]

    findings: list[Finding] = []
    checks_run = {name: 0 for name, _ in CHECKS}

    for bundle in bundle_list:
        for name, check in CHECKS:
            findings.extend(check(bundle))
            checks_run[name] += 1
        if sec_presentation is not None:
            findings.extend(check_sec_presentation_diff(bundle, sec_presentation))
            findings.extend(check_coverage(bundle))

    if include_cross_statement and len(bundle_list) > 1:
        findings.extend(check_cross_statement_consistency(bundle_list))

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
