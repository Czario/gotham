"""The final write decision (P8).

Every write to MongoDB happens in the ``persist`` node, but that node no longer
decides *whether* to write: the ``decide`` node produces an explicit
:class:`WriteDecision` and ``persist`` executes it literally.  Nothing reaches
the database unless a decision said so.

Two layers
----------
**Ceiling** (validation, non-negotiable).  Validation defines the maximum that
may ever be written:

* a statement with an unresolved *statement-level* blocking finding is blocked,
* a concept named by a *concept-scoped* blocking finding is excluded,
* a filing-level blocking finding blocks everything.

Nothing outside the ceiling is written, because validation proved it wrong.
This is a hard rail, not a policy choice.

**Decision** (policy or agent).  Within the ceiling the decider chooses:

* ``write`` — write every permitted statement,
* ``write_partial`` — write a subset of the permitted statements and/or drop
  further concepts,
* ``skip`` — write nothing for this filing.

The deterministic policy is the default decider (and the fallback when the
agent is unavailable).  When ``AGENT_DECISION_ENABLED`` is set, an LLM judge is
consulted for the ambiguous middle — there are blocking findings *and*
something is still permitted — and may narrow further or opt into the partial
write.  It can never widen the ceiling.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .validation.findings import blocking_findings

ACTION_WRITE = "write"
ACTION_WRITE_PARTIAL = "write_partial"
ACTION_SKIP = "skip"


def statement_type_of(bundle: Any) -> Optional[str]:
    """Statement type of a bundle (tolerates non-bundle placeholders)."""
    return getattr(bundle, "statement_type", None)


def compute_ceiling(
    bundles: Iterable[Any],
    findings: Iterable[dict],
) -> tuple[list[Optional[str]], list[Optional[str]], dict[str, list[str]]]:
    """Return ``(permitted, blocked, concept_drops)`` — the hard write rail."""
    bundle_list = [b for b in (bundles or []) if b is not None]
    all_types = [statement_type_of(b) for b in bundle_list]

    blocked_statements: set[Optional[str]] = set()
    concept_drops: dict[str, set[str]] = defaultdict(set)
    filing_level = False

    for finding in blocking_findings(findings or []):
        statement_type = finding.get("statement_type")
        concept = finding.get("concept")
        if statement_type and concept:
            concept_drops[statement_type].add(concept)
        elif statement_type:
            blocked_statements.add(statement_type)
        else:
            filing_level = True

    if filing_level:
        return [], list(all_types), {k: sorted(v) for k, v in concept_drops.items()}

    permitted = [t for t in all_types if t not in blocked_statements]
    blocked = sorted({t for t in blocked_statements})
    return permitted, blocked, {k: sorted(v) for k, v in concept_drops.items()}


@dataclass
class WriteDecision:
    """What the agent decided to write for one filing."""

    action: str
    statements: list[Optional[str]] = field(default_factory=list)
    drop_concepts: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    decided_by: str = "policy"
    # Audit context: what validation would have permitted / blocked.
    permitted: list[Optional[str]] = field(default_factory=list)
    blocked: list[Optional[str]] = field(default_factory=list)
    confidence: Optional[float] = None
    agent_notes: Optional[str] = None

    @property
    def writes_anything(self) -> bool:
        return bool(self.statements)

    def allows(self, statement_type: Optional[str]) -> bool:
        return statement_type in set(self.statements)

    def drops_for(self, statement_type: Optional[str]) -> set[str]:
        return set(self.drop_concepts.get(statement_type) or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "statements": list(self.statements),
            "drop_concepts": {k: list(v) for k, v in self.drop_concepts.items()},
            "reason": self.reason,
            "decided_by": self.decided_by,
            "permitted": list(self.permitted),
            "blocked": list(self.blocked),
            "confidence": self.confidence,
            "agent_notes": self.agent_notes,
        }


def policy_decision(
    bundles: Iterable[Any],
    findings: Iterable[dict],
    *,
    strict_accuracy: bool = True,
) -> WriteDecision:
    """The deterministic default decision (also the agent's fallback)."""
    bundle_list = [b for b in (bundles or []) if b is not None]
    permitted, blocked, drops = compute_ceiling(bundle_list, findings)
    all_types = [statement_type_of(b) for b in bundle_list]

    if not bundle_list:
        return WriteDecision(
            ACTION_SKIP, [], drops,
            "no statement bundles to write", "policy", permitted, blocked,
        )

    blocking = blocking_findings(findings or [])
    if not blocking:
        return WriteDecision(
            ACTION_WRITE, all_types, drops,
            "validation passed", "policy", permitted, blocked,
        )

    if not strict_accuracy:
        # Advisory mode: findings are recorded, not enforced.  Data validation
        # proved wrong is still never written (the ceiling), but the rest is.
        if permitted:
            return WriteDecision(
                ACTION_WRITE, permitted, drops,
                "advisory mode: findings recorded but not enforced",
                "policy", permitted, blocked,
            )
        return WriteDecision(
            ACTION_SKIP, [], drops,
            "advisory mode: every statement is blocked by validation",
            "policy", permitted, blocked,
        )

    # Strict: conservative default — write nothing until the decision is made.
    return WriteDecision(
        ACTION_SKIP, [], drops,
        f"{len(blocking)} unresolved high-severity finding(s); "
        f"{len(permitted)} of {len(all_types)} statement(s) would be writable",
        "policy", permitted, blocked,
    )


def needs_agent_decision(decision: WriteDecision, *, enabled: bool) -> bool:
    """True only for the ambiguous middle: blocked somewhere, writable elsewhere."""
    return bool(enabled) and decision.action == ACTION_SKIP and bool(decision.permitted)


def apply_agent_plan(policy: WriteDecision, plan: Any) -> WriteDecision:
    """Fold an agent proposal into the policy decision (narrowing only)."""
    if not isinstance(plan, dict):
        return policy

    permitted = list(policy.permitted)
    permitted_set = set(permitted)
    action = str(plan.get("action") or "").strip().lower()

    requested = plan.get("statements")
    if isinstance(requested, list) and requested:
        selected = [s for s in requested if s in permitted_set]
    elif action == ACTION_WRITE:
        selected = list(permitted)
    else:
        selected = []

    # The agent may ADD concept exclusions, never remove the ceiling's.
    drops = {k: list(v) for k, v in (policy.drop_concepts or {}).items()}
    extra = plan.get("drop_concepts")
    if isinstance(extra, dict):
        for statement_type, concepts in extra.items():
            if isinstance(concepts, list) and concepts:
                drops[statement_type] = sorted(
                    set(drops.get(statement_type, [])) | {str(c) for c in concepts}
                )

    confidence = plan.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    notes = plan.get("notes") or plan.get("agent_notes")
    reason = str(plan.get("reason") or "").strip() or "agent decision"

    if action == ACTION_SKIP or not selected:
        return WriteDecision(
            ACTION_SKIP, [], drops, reason, "agent",
            permitted, list(policy.blocked), confidence, notes,
        )

    # "write" only when the whole filing is written: every permitted statement,
    # nothing blocked, and no extra concept exclusions beyond the ceiling's.
    wrote_everything = (
        set(selected) == permitted_set
        and not policy.blocked
        and not extra
    )
    return WriteDecision(
        ACTION_WRITE if wrote_everything else ACTION_WRITE_PARTIAL,
        selected, drops, reason, "agent",
        permitted, list(policy.blocked), confidence, notes,
    )
