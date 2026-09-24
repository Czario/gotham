"""Validation report — the audit record produced for every filing.

A report is built by :func:`filings_agent.validation.validator.validate_bundles`
and persisted to the ``validation_reports`` collection by the validate node (see
``filings_agent/reports.py``).  Reports are written even when persistence of the
statement data is refused, so a rejected filing is always traceable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .findings import Finding, blocking_findings, summarize_severities


@dataclass
class ValidationReport:
    """Result of validating all statement bundles of one filing."""

    cik: str
    ticker: str
    form_type: str
    accession_number: Optional[str]
    statement_types: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Number of checks executed per category (observability).
    checks_run: dict[str, int] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    # ── derived ────────────────────────────────────────────────────────────
    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.is_blocking]

    @property
    def status(self) -> str:
        """``pass`` | ``pass_with_findings`` | ``fail``."""
        if self.blocking:
            return "fail"
        return "pass_with_findings" if self.findings else "pass"

    @property
    def severity_counts(self) -> dict[str, int]:
        return summarize_severities(f.to_dict() for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cik": self.cik,
            "ticker": self.ticker,
            "form_type": self.form_type,
            "accession_number": self.accession_number,
            "statement_types": list(self.statement_types),
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "severity_counts": self.severity_counts,
            "blocking_count": len(self.blocking),
            "checks_run": dict(self.checks_run),
            "created_at": self.created_at,
        }


def blocking_dicts(findings: list[dict]) -> list[dict]:
    """Convenience re-export for callers holding plain finding dicts."""
    return blocking_findings(findings)
