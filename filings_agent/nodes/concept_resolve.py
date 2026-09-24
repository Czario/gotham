"""Agent-owned concept resolution (LLM decides whether two tags are the same).

Same spirit as ``hierarchy_review``: with thousands of companies each reporting
thousands of XBRL concepts, no hand-written mapping can anticipate every way a
filer re-tags the same economic line item across accounting-standard eras
(ASC 606 revenue, ASU 2016-18 restricted cash, continuing-operations variants,
…).  So when a filing introduces a concept NAME the company has never used, an
LLM decides — given the company's *stored* concepts as the only valid targets —
whether the new name is:

  * the SAME line item under a different tag  → alias: values attach to the
    existing concept, no duplicate row is created; or
  * a genuinely NEW line item                 → created as its own concept.

Guardrails (mirroring the established agent architecture):
  * The model can only choose ``same_as`` from the stored concept names, or
    ``null`` — it can never invent a target (enforced in the tool AND re-checked
    in the node).  The model decides EQUIVALENCE only.
  * Which tag NAME survives is deterministic, never an LLM judgement: the tag
    belonging to the NEWEST reporting period wins.  When the incoming filing's
    period is newer than the newest period the stored concept has a value for,
    the node emits a promotion (``concept_promotions``); the persister then
    moves the stored concept's values onto the incoming tag and hard-deletes
    the old concept row.  Values are only repointed — never deleted.  Otherwise
    the incoming values attach to the stored concept (historical behaviour).
  * Every decision is persisted to the ``concept_aliases`` store, so a company
    filing the same legacy tag again costs ZERO LLM calls: the persister resolves
    repeat tags from the store deterministically.  A promotion re-keys the
    store (old tag -> new tag) instead of recording a new alias onto the loser.
  * Scope is a single form type: a 10-K run only ever reads/writes the annual
    collection and a 10-Q run only the quarterly one.  The two never influence
    each other.
  * The LLM is asked at most once per (company, statement, form type) per new
    concept, and the incoming concepts are decided in ONE call.
  * ``CONCEPT_AGENT_FAILURE_POLICY``: ``fallback`` (default — lost decisions
    simply mean the incoming concept is created as new, the historical
    behaviour) or ``block`` (nothing is persisted until the agent decides).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Optional

from ..hooks import report_call

logger = logging.getLogger(__name__)

_MAX_STORED = 400          # stored candidates shown to the model
_MAX_SHORTLIST = 120       # similarity shortlist cap (prompt budget)


def concept_agent_enabled() -> bool:
    return os.getenv("CONCEPT_AGENT_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def concept_agent_failure_policy() -> str:
    policy = os.getenv("CONCEPT_AGENT_FAILURE_POLICY", "fallback").strip().lower()
    return policy if policy in {"block", "fallback"} else "fallback"


def concept_promotion_enabled() -> bool:
    """Whether the newest-period promotion (rename + value move + delete) is on.

    Independent of ``CONCEPT_AGENT_ENABLED``: with promotion off, the agent
    still resolves equivalence and incoming values still attach to the stored
    concept — but the stored tag is never renamed and never deleted.
    """
    return os.getenv("CONCEPT_PROMOTION_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _tokenize(name: str) -> set[str]:
    """Normalize a concept name into lowercase tokens (prefix stripped)."""
    local = name.split(":", 1)[-1]
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", local)
    return {p.lower() for p in parts if p and p.lower() not in {"us", "gaap"}}


def _similarity(incoming: str, candidate: str) -> float:
    """Token overlap + prefix-overlap scoring (0..1).

    Plain Jaccard misses the flagship case: ``us-gaap:Revenues`` and
    ``us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`` share no
    exact token ("revenues" vs "revenue"), so the partial/prefix term keeps
    genuine aliases ranked above unrelated rows in the LLM shortlist.
    """
    a, b = _tokenize(incoming), _tokenize(candidate)
    if not a or not b:
        return 0.0
    exact = len(a & b)
    partial = sum(
        1
        for x in a for y in b
        if len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x))
    )
    return (exact + 0.5 * partial) / len(a | b)


def _shortlist(stored: list[dict], incoming_names: list[str]) -> list[dict]:
    """Return the stored concepts most likely to be aliases, bounded."""
    if len(stored) <= _MAX_SHORTLIST:
        return stored
    scored = []
    for row in stored:
        name = row.get("concept") or ""
        best = max((_similarity(n, name) for n in incoming_names), default=0.0)
        label = row.get("label") or ""
        if label:
            best = max(best, max(
                (_similarity(n.split(":", 1)[-1], label) for n in incoming_names),
                default=0.0,
            ))
        scored.append((best, name, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [row for _, _, row in scored[:_MAX_SHORTLIST]]


def _stored_concepts(norm_service: Any, bundle: Any) -> list[dict]:
    """Stored main-line concept rows for this (cik, statement_type, form type)."""
    try:
        repo = norm_service._get_concept_repo_by_form_type(bundle.form_type)
        cursor = repo.collection.find(
            {
                "cik": bundle.company_cik,
                "statement_type": bundle.statement_type,
                "dimension_concept": False,
                "concept": {"$not": {"$regex": "Abstract$"}},
            },
            {"concept": 1, "label": 1, "path": 1, "order_key": 1, "_id": 1},
        )
        try:
            cursor = cursor.limit(_MAX_STORED)
        except AttributeError:  # plain list (tests)
            pass
        return list(cursor)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("concept resolve: could not read stored rows: %s", exc)
        return []


def _quarter_number(value: Any) -> int:
    """Normalise a quarter value (1-4, "1".."4", "Q1".."Q4") to an int."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 1 <= value <= 4 else 0
    text = str(value).strip().upper()
    if text.startswith("Q"):
        text = text[1:]
    try:
        number = int(text)
    except (TypeError, ValueError):
        return 0
    return number if 1 <= number <= 4 else 0


def _period_key(reporting_period: Any) -> Optional[int]:
    """Sortable period key: ``fiscal_year * 10 + quarter`` (0 for annual).

    Used only to compare an incoming filing period against the newest period a
    STORED concept has values for.  A 10-K period (quarter 0) always sorts
    below the same year's 10-Q periods — and since comparisons never cross form
    types, that is exactly the intended within-collection ordering.
    """
    if not isinstance(reporting_period, dict):
        return None
    fiscal_year = reporting_period.get("fiscal_year")
    if fiscal_year is None:
        return None
    try:
        fiscal_year = int(fiscal_year)
    except (TypeError, ValueError):
        return None
    return fiscal_year * 10 + _quarter_number(reporting_period.get("quarter"))


def _stored_latest_period_key(
    norm_service: Any, bundle: Any, concept_id: Any
) -> Optional[int]:
    """Newest period the given stored concept has a value for, or ``None``.

    Read on demand (only for concepts the agent judged equivalent) so we never
    maintain a redundant ``latest_period`` field.  Any failure returns ``None``
    — the node then keeps the safe historical behaviour (attach to stored).
    """
    if concept_id is None:
        return None
    try:
        value_repo = norm_service._get_value_repo_by_form_type(bundle.form_type)
        cursor = value_repo.collection.find(
            {"concept_id": concept_id, "dimension_value": {"$ne": True}},
            {"reporting_period": 1},
        )
        best: Optional[int] = None
        for doc in cursor:
            key = _period_key((doc or {}).get("reporting_period"))
            if key is not None and (best is None or key > best):
                best = key
        return best
    except Exception as exc:  # noqa: BLE001 — direction is best-effort
        logger.debug("concept resolve: no stored period for %s: %s", concept_id, exc)
        return None


def _incoming_concepts(bundle: Any) -> list[dict]:
    """Concepts this filing reports that are candidate aliases/new rows.

    Only concrete line items (no ``custom:`` grouping headers, no standard
    Abstract wrappers) — those are never aliases of financial line items.
    """
    rows = []
    for item in getattr(bundle, "concepts", None) or []:
        if not isinstance(item, dict) or not item.get("concept"):
            continue
        concept = item["concept"]
        if concept.split(":")[-1].endswith("Abstract"):
            continue
        if concept.startswith("custom:"):
            continue
        rows.append(item)
    return rows


def _get_alias_store(norm_service: Any):
    """Return a ``ConceptAliasStore`` for the service, or ``None`` when the
    service has no target DB (tests / minimal callers).  Decisions then apply
    in-memory only and are not cached — never a hard failure."""
    db_connection = getattr(norm_service, "db_connection", None)
    if db_connection is None:
        return None
    try:
        from data_normalization_service.database import ConceptAliasStore
        store = ConceptAliasStore(db_connection)
        store.ensure_indexes()
        return store
    except Exception as exc:  # noqa: BLE001 — caching must never break ingestion
        logger.warning("concept alias store unavailable: %s", exc)
        return None


def _apply_decisions(
    bundle: Any,
    decisions: dict[str, str],
) -> tuple[int, list[str]]:
    """Annotate bundle items with ``_concept_target`` for the aliases decided.

    Returns ``(annotated_count, aliased_names)``.
    """
    annotated = 0
    aliased_names: list[str] = []
    for item in list(getattr(bundle, "concepts", None) or []) + list(
        getattr(bundle, "abstract_concepts", None) or []
    ):
        if not isinstance(item, dict):
            continue
        target = decisions.get(item.get("concept"))
        if target:
            item["_concept_target"] = target
            annotated += 1
            aliased_names.append(item["concept"])
    return annotated, aliased_names


def make_concept_resolve_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
    force: bool = False,
) -> Callable[[dict], dict]:
    """Build the ``concept_resolve`` node (agent-decided concept equivalence)."""

    def concept_resolve_node(state: dict) -> dict:
        from ..agent.loop import run_agent_loop
        from ..agent.prompts import (CONCEPT_FINALIZE_DESCRIPTION,
                                     CONCEPT_SYSTEM_PROMPT)
        from ..agent.tools import build_concept_tools

        bundles = state.get("bundles") or []
        if not bundles:
            return {**state, "concept_resolution": {"skipped": True},
                    "status": state.get("status") or "concept_resolved"}

        enabled = concept_agent_enabled()
        policy = concept_agent_failure_policy()

        if not enabled:
            report_call("  [concept resolve]  – agent disabled (CONCEPT_AGENT_ENABLED=0)")
            return {**state, "concept_resolution": {"skipped": True},
                    "status": state.get("status") or "concept_resolved"}

        alias_store = _get_alias_store(norm_service)

        summary = {"candidates": 0, "aliases": 0, "new_concepts": 0,
                   "from_cache": 0, "statements": 0, "promotions": 0}
        unresolved_any: list[tuple[str, str]] = []
        promotions: list[dict] = []

        for bundle in bundles:
            stored = _stored_concepts(norm_service, bundle)
            stored_names = {r.get("concept") for r in stored if r.get("concept")}

            incoming = _incoming_concepts(bundle)
            incoming_names = [
                i["concept"] for i in incoming
                if i.get("concept") and i["concept"] not in stored_names
            ]
            # A name that exactly exists is, by definition, not a new concept.
            incoming_names = sorted(set(incoming_names))
            if not incoming_names:
                continue  # nothing new for this statement — zero LLM calls

            summary["statements"] += 1
            summary["candidates"] += len(incoming_names)

            # 1) Durable decisions already recorded (repeat filings cost nothing).
            cached: dict[str, str] = {}
            if alias_store is not None:
                try:
                    cached = alias_store.get_many(
                        bundle.company_cik, bundle.statement_type,
                        bundle.form_type, incoming_names,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("alias cache lookup failed: %s", exc)
            aliases = dict(cached)
            summary["from_cache"] += len(cached)

            unresolved = [n for n in incoming_names if n not in cached]
            decisions: dict[str, str] = {}
            reasons: dict[str, str] = {}

            if unresolved:
                row = sorted(
                    (i for i in incoming if i.get("concept") in unresolved),
                    key=lambda i: i["concept"],
                )
                candidates = _shortlist(stored, unresolved)
                report_call(
                    f"  [concept resolve]  {bundle.company_cik} {bundle.statement_type}: "
                    f"{len(unresolved)} new concept(s), {len(candidates)} stored candidate(s)"
                )

                tool_decisions: dict[str, Any] = {}
                tools = build_concept_tools(candidates, row, tool_decisions)
                task_message = (
                    f"Decide, for each NEW concept reported by this {bundle.form_type} "
                    f"{bundle.statement_type} filing for CIK {bundle.company_cik}, whether it is "
                    f"an alias of a STORED concept (same economic line item) or a new line item.\n\n"
                    f"NEW CONCEPTS THIS FILING REPORTS ({len(unresolved)}):\n"
                    + "\n".join(
                        f"  * {n}" + (
                            f" (label: '{next((i.get('label') for i in row if i.get('concept') == n), '')}')"
                            if next((i.get('label') for i in row if i.get('concept') == n), '')
                            else ""
                        )
                        for n in unresolved[:120]
                    )
                    + ("\n  … truncated" if len(unresolved) > 120 else "")
                    + "\n\nCall query_stored_concepts() to see the stored concepts, then "
                    "decide_mapping() for EVERY new concept, then finalize_concepts()."
                )

                try:
                    result = run_agent_loop(
                        CONCEPT_SYSTEM_PROMPT,
                        task_message,
                        tools,
                        ticker=str(state.get("ticker") or state.get("cik") or "?"),
                        finalize_name="finalize_concepts",
                        finalize_description=CONCEPT_FINALIZE_DESCRIPTION,
                        chat_llm=chat_llm,
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001 — policy decides
                    report_call(
                        f"  [concept resolve]  ✗ exception for {bundle.statement_type}: {exc}"
                    )
                    logger.warning(
                        "concept resolve failed for %s/%s: %s",
                        bundle.company_cik, bundle.statement_type, exc,
                    )
                    unresolved_any.append((bundle.statement_type, f"agent error: {exc}"))
                    continue

                # 2) Guardrail: only accept decisions whose targets really exist.
                for concept, dec in (tool_decisions or {}).items():
                    if concept not in unresolved:
                        continue  # not this statement's problem; ignore
                    same_as = dec.get("same_as") if isinstance(dec, dict) else None
                    if same_as is not None and same_as not in stored_names:
                        same_as = None  # invented target → treat as new
                    decisions[concept] = same_as
                    reasons[concept] = (
                        dec.get("reason") if isinstance(dec, dict) else ""
                    )
                summary["new_concepts"] += sum(
                    1 for v in decisions.values() if v is None
                )

                # 3) Decisions are persisted AFTER the promotion split below,
                #    so a promoted name is never recorded as an alias onto the
                #    tag it is about to replace.

            else:
                report_call(
                    f"  [concept resolve]  {bundle.company_cik} {bundle.statement_type}: "
                    f"all {len(incoming_names)} new concept(s) already decided (cache hit)"
                )

            # 4) Direction: newest period wins.  The agent decided only
            #    EQUIVALENCE; which name survives is a deterministic period
            #    comparison — never an LLM judgement.
            all_aliases = {**decisions, **aliases}
            all_aliases = {k: v for k, v in all_aliases.items() if v}
            stored_by_name = {r.get("concept"): r for r in stored if r.get("concept")}
            incoming_key = _period_key(getattr(bundle, "reporting_period", None))
            attach: dict[str, str] = {}
            promoted_here: list[str] = []
            for concept, target in all_aliases.items():
                target_row = stored_by_name.get(target) or {}
                stored_key = _stored_latest_period_key(
                    norm_service, bundle, target_row.get("_id")
                )
                if (
                    concept_promotion_enabled()
                    and incoming_key is not None
                    and stored_key is not None
                    and incoming_key > stored_key
                ):
                    promotions.append({
                        "cik": bundle.company_cik,
                        "statement_type": bundle.statement_type,
                        "form_type": bundle.form_type,
                        "from_concept": target,
                        "to_concept": concept,
                        "incoming_period": getattr(bundle, "reporting_period", None),
                        "stored_latest_period_key": stored_key,
                        "reason": reasons.get(concept) or "newest period wins",
                    })
                    promoted_here.append(concept)
                else:
                    # Stored/older tag wins (or direction unknown) — the
                    # incoming values attach to the existing concept, exactly
                    # as before.
                    attach[concept] = target

            if promoted_here:
                summary["promotions"] += len(promoted_here)
                # A promoted name must be written under its OWN tag, so clear
                # any stale attach annotation from an earlier pass.
                promoted_set = set(promoted_here)
                for item in list(getattr(bundle, "concepts", None) or []) + list(
                    getattr(bundle, "abstract_concepts", None) or []
                ):
                    if isinstance(item, dict) and item.get("concept") in promoted_set:
                        item.pop("_concept_target", None)
                report_call(
                    f"  [concept resolve]  ⇄ {bundle.company_cik} {bundle.statement_type}: "
                    f"{len(promoted_here)} concept(s) promoted to the newest filing's tag "
                    f"({', '.join(promoted_here[:3])}{'…' if len(promoted_here) > 3 else ''})"
                )

            # 5) Persist only the attach decisions; a promoted name must not be
            #    recorded as an alias onto the tag it replaces.  The promotion
            #    itself re-keys the store (old tag -> new tag) at persist time.
            if alias_store is not None and decisions:
                promoted_set = set(promoted_here)
                fresh_attach = {
                    c: t for c, t in decisions.items()
                    if t and c not in promoted_set
                }
                if fresh_attach:
                    try:
                        alias_store.record_many(
                            bundle.company_cik, bundle.statement_type,
                            bundle.form_type, fresh_attach,
                            decided_by="concept_agent", reasons=reasons,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("could not persist concept aliases: %s", exc)

            # 6) Annotate the bundle so the persister reuses existing concepts.
            annotated, aliased_names = _apply_decisions(bundle, attach)
            summary["aliases"] += len(aliased_names)
            if aliased_names:
                report_call(
                    f"  [concept resolve]  ✓ {bundle.company_cik} {bundle.statement_type}: "
                    f"{len(aliased_names)} concept(s) merged into existing rows "
                    f"({', '.join(aliased_names[:3])}{'…' if len(aliased_names) > 3 else ''})"
                )

        if unresolved_any and policy == "block":
            detail = "; ".join(f"{s}: {r}" for s, r in unresolved_any)
            report_call(
                f"  [concept resolve]  ✗ BLOCKED — the agent could not decide "
                f"{len(unresolved_any)} statement(s); nothing will be written"
            )
            logger.error(
                "concept resolve blocked the filing for CIK %s %s — %s",
                state.get("cik"), state.get("accession_number") or "", detail,
            )
            return {
                **state,
                "concept_resolution": {**summary, "blocked": True},
                "status": "failed",
                "error": f"concept resolution not decided by the agent: {detail}",
            }

        if unresolved_any:
            report_call(
                f"  [concept resolve]  ⚠ {len(unresolved_any)} statement(s) left undecided "
                f"(CONCEPT_AGENT_FAILURE_POLICY=fallback) — new concepts will be created"
            )

        return {**state, "concept_resolution": summary,
                "concept_promotions": promotions,
                "status": state.get("status") or "concept_resolved"}

    return concept_resolve_node


__all__ = [
    "make_concept_resolve_node",
    "concept_agent_enabled",
    "concept_agent_failure_policy",
    "concept_promotion_enabled",
    "_shortlist",
    "_similarity",
    "_tokenize",
    "_period_key",
    "_quarter_number",
    "_stored_latest_period_key",
]