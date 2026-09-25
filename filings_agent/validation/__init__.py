"""Deterministic validation of statement bundles (ask 1).

Public surface:

    validate_bundles(bundles, cik=...) -> ValidationReport
    Finding / ValidationReport
    blocking_findings(findings)
    check_coverage(bundle)
    check_cross_statement_consistency(bundles)
"""
from .coverage import check_coverage
from .cross_statement import check_cross_statement_consistency
from .findings import (
    ABSENCE_ONLY_TYPES,
    HIGH,
    LOW,
    MEDIUM,
    Finding,
    blocking_findings,
    summarize_severities,
)
from .report import ValidationReport
from .validator import CHECKS, validate_bundles

__all__ = [
    "ABSENCE_ONLY_TYPES",
    "CHECKS",
    "Finding",
    "HIGH",
    "LOW",
    "MEDIUM",
    "ValidationReport",
    "blocking_findings",
    "check_coverage",
    "check_cross_statement_consistency",
    "summarize_severities",
    "validate_bundles",
]
