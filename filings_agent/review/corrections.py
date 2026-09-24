"""CorrectionGate — safety checks for agent-proposed value repairs.

The LLM never writes to MongoDB and never gets to invent an unrestricted
number. It can only propose a correction; this gate checks that the concept
exists, the old value still matches the in-memory bundle, the new value is
numeric, and the source/reason are recorded. The repair node applies approved
proposals in memory; the normal persist node remains the sole DB writer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


_ALLOWED_SOURCES = frozenset({
    "deterministic_identity",
    "xbrl_calculation",
    "sec_companyfacts",
    "agent_confirmed",
})


@dataclass(frozen=True)
class CorrectionDecision:
    statement_type: str
    concept: str
    old_value: float
    new_value: float
    reason: str
    source: str
    approved: bool = False
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_type": self.statement_type,
            "concept": self.concept,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "reason": self.reason,
            "source": self.source,
            "approved": self.approved,
            "confidence": self.confidence,
        }


class CorrectionGate:
    """Validate and approve in-memory repair proposals."""

    def __init__(self, *, mode: str = "report", tolerance: float = 0.005):
        self.mode = (mode or "report").lower()
        self.tolerance = tolerance

    def _close(self, a: float, b: float) -> bool:
        return abs(a - b) <= max(abs(a) * self.tolerance, 1.0)

    def approve(self, proposal: dict[str, Any], bundle: Any) -> tuple[CorrectionDecision | None, str | None]:
        """Return an approved decision or ``(None, reason)``.

        ``report`` mode never applies a correction. ``repair`` permits
        deterministic/authoritative proposals. ``strict`` requires the caller
        to mark the proposal ``approved=True`` in addition to all checks.
        """
        try:
            statement_type = str(proposal.get("statement_type") or bundle.statement_type)
            concept = str(proposal.get("concept") or "")
            old_value = float(proposal.get("old_value"))
            new_value = float(proposal.get("new_value"))
        except (TypeError, ValueError):
            return None, "proposal has invalid numeric or identity fields"

        if not concept:
            return None, "proposal has no concept"
        if not math.isfinite(new_value) or not math.isfinite(old_value):
            return None, "proposal contains a non-finite number"

        source = str(proposal.get("source") or "").strip()
        if source not in _ALLOWED_SOURCES:
            return None, f"source {source!r} is not allowed"
        reason = str(proposal.get("reason") or "").strip()
        if not reason:
            return None, "proposal has no reason"

        current = None
        for item in (getattr(bundle, "concepts", None) or []):
            if isinstance(item, dict) and item.get("concept") == concept:
                value = item.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    current = float(value)
                    break
        if current is None:
            return None, f"concept {concept!r} is not present with a numeric value"
        if not self._close(current, old_value):
            return None, f"old_value {old_value} no longer matches current {current}"
        if self._close(old_value, new_value):
            return None, "new_value does not change the current value"

        explicitly_approved = bool(proposal.get("approved", False))
        if self.mode == "report":
            return None, "AGENT_MODE=report does not apply corrections"
        if self.mode == "strict" and not explicitly_approved:
            return None, "strict mode requires explicit approval"

        confidence = proposal.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        return CorrectionDecision(
            statement_type=statement_type,
            concept=concept,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            source=source,
            approved=True,
            confidence=confidence,
        ), None


def apply_decisions_to_bundles(bundles: Iterable[Any], decisions: Iterable[CorrectionDecision]) -> list[dict[str, Any]]:
    """Apply approved decisions in memory and annotate repaired facts.

    Returns provenance actions. The persistence service sees ``repaired``,
    ``source``, ``corrected_from`` and ``correction_reason`` on the item and
    carries them into the value document.
    """
    applied: list[dict[str, Any]] = []
    by_statement = {}
    for bundle in bundles or []:
        by_statement.setdefault(getattr(bundle, "statement_type", ""), []).append(bundle)

    for decision in decisions:
        matched = False
        for bundle in by_statement.get(decision.statement_type, []):
            for item in (getattr(bundle, "concepts", None) or []):
                if not isinstance(item, dict) or item.get("concept") != decision.concept:
                    continue
                value = item.get("value")
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                item["value"] = decision.new_value
                item["repaired"] = True
                item["source"] = decision.source
                item["corrected_from"] = decision.old_value
                item["correction_reason"] = decision.reason
                item["correction_source"] = decision.source
                # Keep the flattened bundle view consistent for validators and
                # downstream consumers.
                for flat in getattr(bundle, "values", None) or []:
                    if flat.get("concept") == decision.concept:
                        flat["value"] = decision.new_value
                        flat["repaired"] = True
                        flat["source"] = decision.source
                        flat["corrected_from"] = decision.old_value
                matched = True
                break
            if matched:
                break
        if matched:
            applied.append(decision.to_dict())
    return applied
