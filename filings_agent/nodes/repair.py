"""P4 repair node — apply gated correction proposals in memory.

No Mongo writes happen here. Approved decisions annotate bundle items with
repair provenance, then the graph re-runs validation before persistence.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..review.corrections import CorrectionGate, apply_decisions_to_bundles

logger = logging.getLogger(__name__)


def make_repair_node(*, mode: str | None = None) -> Callable[[dict], dict]:
    """Build a repair node using ``AGENT_MODE`` unless explicitly overridden."""

    def repair_node(state: dict) -> dict:
        from ..config import AGENT_MODE

        effective_mode = mode or AGENT_MODE
        gate = CorrectionGate(mode=effective_mode)
        bundles = state.get("bundles") or []
        proposals = state.get("repair_decisions") or []

        approved = []
        rejected = []
        for proposal in proposals:
            matched_bundle = next(
                (
                    b for b in bundles
                    if getattr(b, "statement_type", "") == proposal.get("statement_type")
                    and any(
                        isinstance(item, dict) and item.get("concept") == proposal.get("concept")
                        for item in (getattr(b, "concepts", None) or [])
                    )
                ),
                None,
            )
            if matched_bundle is None:
                rejected.append({"proposal": proposal, "reason": "bundle/concept not found"})
                continue
            decision, reason = gate.approve(proposal, matched_bundle)
            if decision is None:
                rejected.append({"proposal": proposal, "reason": reason})
            else:
                approved.append(decision)

        applied = apply_decisions_to_bundles(bundles, approved)
        logger.info(
            "repair: approved=%d applied=%d rejected=%d mode=%s",
            len(approved), len(applied), len(rejected), effective_mode,
        )
        return {
            **state,
            "status": "repaired",
            "repair_actions": {
                "proposed": len(proposals),
                "approved": len(approved),
                "applied": len(applied),
                "rejected": rejected,
                "applied_details": applied,
            },
        }

    return repair_node
