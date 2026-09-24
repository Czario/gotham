"""P5 hierarchy node — resolve paths/order keys before final validation."""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..hierarchy.resolver import resolve_hierarchy_bundles

logger = logging.getLogger(__name__)


def make_hierarchy_node(norm_service: Any) -> Callable[[dict], dict]:
    """Build a node that mutates only bundle hierarchy metadata."""

    def resolve_hierarchy_node(state: dict) -> dict:
        plan = resolve_hierarchy_bundles(
            state.get("bundles") or [],
            norm_service,
            cik=str(state.get("cik") or ""),
        )
        logger.info(
            "hierarchy: resolved %d concept(s), seeded=%s, conflicts=%d",
            plan.get("resolved_concepts", 0),
            plan.get("seeded_statement_types", []),
            len(plan.get("conflicts", [])),
        )
        return {
            **state,
            "status": "hierarchy_resolved",
            "hierarchy_plan": plan,
        }

    return resolve_hierarchy_node
