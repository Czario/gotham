"""Persist node — the ONLY writer in the filings-agent graph.

It no longer decides *whether* to write.  The ``decide`` node produces an
explicit ``WriteDecision`` (see :mod:`filings_agent.decision`) and this node
executes it literally:

* ``skip``         → nothing is written for the filing,
* ``write``        → every permitted statement is written,
* ``write_partial``→ only the decided statements, with the decided concepts
                     excluded before writing.

If no decision is present on the state (e.g. the node is driven directly), the
deterministic policy is applied so behaviour is always defined and never an
implicit write.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..hooks import report_call
from ..validation.findings import blocking_findings

logger = logging.getLogger(__name__)


def _drop_concepts(bundle: Any, drops: set[str]) -> None:
    """Remove excluded concepts from every in-memory view of the bundle."""
    for attr in ("concepts", "values", "source_items"):
        items = getattr(bundle, attr, None)
        if isinstance(items, list):
            setattr(
                bundle,
                attr,
                [
                    item for item in items
                    if not (isinstance(item, dict) and item.get("concept") in drops)
                ],
            )


def _allows(plan: dict, statement_type: Any) -> bool:
    return statement_type in set(plan.get("statements") or [])


def make_persist_node(
    norm_service: Any,
    *,
    enforce_allowed_types: bool = True,
) -> Callable[[dict], dict]:
    """Build the ``persist`` node bound to a normalization service."""

    def persist_node(state: dict) -> dict:
        from ..config import STRICT_ACCURACY
        from ..decision import ACTION_SKIP, policy_decision

        bundles = state.get("bundles") or []
        plan = state.get("write_plan")
        if not isinstance(plan, dict):
            # No decide node in this graph: derive the policy decision so the
            # write is always explicit and ceiling-respecting.
            plan = policy_decision(
                bundles, state.get("findings") or [],
                strict_accuracy=STRICT_ACCURACY,
            ).to_dict()

        if plan.get("action") == ACTION_SKIP or not plan.get("statements"):
            reason = str(plan.get("reason") or "no write decision permitted")
            report_call(f"  [persist]  skipping — {reason[:90]}")
            logger.info(
                "persist: skipped %s — %s", state.get("accession_number"), reason
            )
            return {
                **state,
                "status": "skipped",
                "error": None,
                "persist_receipt": {
                    "statements_written": 0,
                    "statements_total": len(bundles),
                    "statements_skipped": [
                        getattr(b, "statement_type", None) for b in bundles
                    ],
                    "cik": state.get("cik"),
                    "accession_number": state.get("accession_number"),
                    "blocking_findings": len(blocking_findings(state.get("findings") or [])),
                    "action": plan.get("action"),
                    "decided_by": plan.get("decided_by"),
                    "decision_reason": reason,
                },
            }

        written = 0
        skipped_statements: list[Any] = []

        # A write is now certain: run the deferred pre-write step (the scraper
        # uses this for the --reload delete, so a skip can never delete data).
        pre_write_hook = state.get("pre_write_hook")
        if callable(pre_write_hook):
            try:
                pre_write_hook()
            except Exception as exc:  # noqa: BLE001
                logger.error("pre-write step failed; writing nothing: %s", exc, exc_info=True)
                return {
                    **state,
                    "status": "failed",
                    "error": f"pre-write step failed: {exc}",
                }

        report_call(
            f"  [persist]  writing {plan.get('statements')} "
            f"({plan.get('action')} by {plan.get('decided_by')})"
        )

        # Newest-period concept promotion.  The resolve node decided that an
        # incoming tag is EQUIVALENT to a stored concept AND belongs to a newer
        # period; execute the merge here, before the write, so the bundle is
        # persisted under the promoted (winning) concept.  Values are only
        # repointed — never deleted — and the losing concept row is deleted.
        # Scoped to this filing's form type (10-K and 10-Q never cross).
        promotions = state.get("concept_promotions") or []
        promotion_receipts: list[dict] = []
        if promotions:
            report_call(
                f"  [persist]  applying {len(promotions)} newest-period concept promotion(s)"
            )
        for promo in promotions:
            if not _allows(plan, promo.get("statement_type")):
                continue
            apply_promotion = getattr(norm_service, "promote_concept", None)
            if not callable(apply_promotion):
                continue
            try:
                receipt = apply_promotion(
                    promo.get("cik") or state.get("cik"),
                    promo.get("statement_type"),
                    promo.get("form_type"),
                    promo.get("from_concept"),
                    promo.get("to_concept"),
                )
                promotion_receipts.append(receipt)
                report_call(
                    f"  [persist]  ⇄ promoted {promo.get('from_concept')} → "
                    f"{promo.get('to_concept')} "
                    f"({receipt.get('values_moved', 0)} value(s) moved, "
                    f"old row deleted={receipt.get('deleted')})"
                )
            except Exception as exc:  # noqa: BLE001 — never lose the filing over a merge
                logger.error(
                    "persist: concept promotion %s → %s failed: %s",
                    promo.get("from_concept"), promo.get("to_concept"), exc,
                    exc_info=True,
                )

        for bundle in bundles:
            statement_type = getattr(bundle, "statement_type", None)
            if not _allows(plan, statement_type):
                skipped_statements.append(statement_type)
                continue

            drops = set((plan.get("drop_concepts") or {}).get(statement_type) or [])
            if drops:
                _drop_concepts(bundle, drops)
                logger.info(
                    "persist: excluded %d concept(s) from %s: %s",
                    len(drops), statement_type, sorted(drops),
                )

            concepts = getattr(bundle, "concepts", None)
            if isinstance(concepts, list) and not concepts:
                skipped_statements.append(statement_type)
                continue

            if norm_service.persist_statement_bundle(
                bundle,
                enforce_allowed_types=enforce_allowed_types,
                replace_existing=bool(state.get("replace_existing")),
            ):
                written += 1
                report_call(f"  [persist]  ✓ {statement_type} written")
            else:
                skipped_statements.append(statement_type)

        if written == 0:
            return {
                **state,
                "status": "failed",
                "error": "persist wrote no statements",
            }

        # Apply hierarchy repairs (re-pathing duplicate paths, moving non-relevant to path 555)
        hierarchy_plan = state.get("hierarchy_plan") or {}
        existing_updates = hierarchy_plan.get("existing_updates") or []
        if existing_updates and hasattr(norm_service, "apply_hierarchy_updates"):
            try:
                norm_service.apply_hierarchy_updates(existing_updates)
                logger.info("persist: applied %d hierarchy update(s)", len(existing_updates))
            except Exception as exc:  # noqa: BLE001
                logger.warning("persist: could not apply hierarchy updates: %s", exc)

        if hasattr(norm_service, "update_hierarchy_seed_metadata"):
            try:
                norm_service.update_hierarchy_seed_metadata(state.get("cik"), hierarchy_plan)
            except Exception as exc:  # noqa: BLE001
                logger.warning("persist: could not update hierarchy seed metadata: %s", exc)

        logger.info(
            "persist: wrote %d/%d statement(s) for CIK %s %s (action=%s, by %s)",
            written,
            len(bundles),
            state.get("cik"),
            state.get("accession_number") or "",
            plan.get("action"),
            plan.get("decided_by"),
        )
        return {
            **state,
            "status": "saved",
            "persist_receipt": {
                "statements_written": written,
                "statements_total": len(bundles),
                "statements_skipped": skipped_statements,
                "cik": state.get("cik"),
                "accession_number": state.get("accession_number"),
                "blocking_findings": len(blocking_findings(state.get("findings") or [])),
                "dropped_concepts": plan.get("drop_concepts") or {},
                "promotions": promotion_receipts,
                "action": plan.get("action"),
                "decided_by": plan.get("decided_by"),
                "decision_reason": plan.get("reason"),
            },
        }

    return persist_node
