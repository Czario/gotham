"""Validation findings — the shared vocabulary of the validate stage.

Findings are deliberately plain dicts (via :meth:`Finding.to_dict`) so they can
live on the graph state, be stored in MongoDB, and be read back without any
class coupling.  Severity drives the persist gate: only ``high`` findings block,
and only when the finding records corrupt data (see ``ABSENCE_ONLY_TYPES``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

SEVERITIES = (HIGH, MEDIUM, LOW)

# Finding types that record an ABSENCE (something the filing legitimately does
# not contain) rather than corrupting stored data.  Mirrors the earning_agent's
# absence-only exemption: these never block persistence, whatever their severity.
ABSENCE_ONLY_TYPES = frozenset({
    "missing_concept",
    "empty_statement",
})

# Hierarchy finding types: structural/hierarchy placement issues must NEVER stop
# the process or block writing to the database. The hierarchy agent attempts to fix
# them, but remaining hierarchy issues are advisory and non-blocking.
HIERARCHY_FINDING_TYPES = frozenset({
    "duplicate_path_order",
    "orphan_hierarchy_path",
    "missing_hierarchy_path",
    "negative_hierarchy_level",
    "missing_concept_name",
    "abstract_in_concrete_set",
    "duplicate_concept",
})

NON_BLOCKING_TYPES = ABSENCE_ONLY_TYPES | HIERARCHY_FINDING_TYPES


@dataclass
class Finding:
    """A single validation observation about one statement bundle."""

    type: str
    severity: str
    message: str
    statement_type: str | None = None
    concept: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        """True when this finding must refuse persistence (under STRICT_ACCURACY)."""
        return self.severity == HIGH and self.type not in NON_BLOCKING_TYPES

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
        }
        if self.statement_type:
            out["statement_type"] = self.statement_type
        if self.concept:
            out["concept"] = self.concept
        if self.evidence:
            out["evidence"] = self.evidence
        return out


def blocking_findings(findings: Iterable[dict]) -> list[dict]:
    """Return the findings that must refuse persistence."""
    return [
        f for f in findings
        if f.get("severity") == HIGH and f.get("type") not in NON_BLOCKING_TYPES
    ]


def summarize_severities(findings: Iterable[dict]) -> dict[str, int]:
    """Count findings per severity (all severities present as keys)."""
    counts = {sev: 0 for sev in SEVERITIES}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
    return counts
