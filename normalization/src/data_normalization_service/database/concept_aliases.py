"""Durable store of agent-decided concept aliases.

The concept-resolution agent (``filings_agent/nodes/concept_resolve.py``) decides,
per company + statement + form type, whether a concept name a NEW filing reports
is actually the SAME economic line item as a concept already stored under a
different tag (e.g. ``us-gaap:Revenues`` → ``us-gaap:
RevenueFromContractWithCustomerExcludingAssessedTax``).  When it decides "same",
the value must attach to the EXISTING concept instead of creating a duplicate.

This collection is the durable record of those decisions:

    {cik, statement_type, form_type, concept, target_concept,
     decided_by, reason, confidence, created_at, updated_at}

Why it exists (not just an in-memory map):
  * **Cost** — with thousands of companies × thousands of concepts the LLM must
    never be asked the same question twice.  Subsequent filings for the same
    company resolve from here with no LLM call.
  * **Audit** — every merge of two concept names is traceable to a decision.
  * **Robustness** — the persister consults it even when a caller bypasses the
    agent node, so an alias can never be lost mid-pipeline.

Lookups are keyed on the exact tuple ``(cik, statement_type, form_type,
concept)`` because a company's 10-K and 10-Q concept rows live in separate
collections and may legitimately resolve differently.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .database import DatabaseConnection

logger = logging.getLogger(__name__)

# Ensured once per process — repeated create_index calls are wasted round-trips.
_INDEX_ENSURED = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConceptAliasStore:
    """Read/write access to the ``concept_aliases`` decision collection."""

    COLLECTION_NAME = "concept_aliases"

    def __init__(
        self,
        db_connection: DatabaseConnection,
        collection_name: Optional[str] = None,
    ) -> None:
        self.db_connection = db_connection
        self.collection_name = collection_name or self.COLLECTION_NAME
        self._collection = None

    @property
    def collection(self):
        if self._collection is None:
            self._collection = self.db_connection.target_db[self.collection_name]
        return self._collection

    def ensure_indexes(self) -> None:
        """Create the uniqueness index on the decision key (idempotent)."""
        global _INDEX_ENSURED
        if _INDEX_ENSURED:
            return
        try:
            self.collection.create_index(
                [("cik", 1), ("statement_type", 1), ("form_type", 1), ("concept", 1)],
                unique=True,
                name="idx_concept_alias_key",
            )
            _INDEX_ENSURED = True
        except Exception as exc:  # noqa: BLE001 — never block ingestion on index setup
            logger.warning("Could not ensure concept_aliases index: %s", exc)

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def get(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        concept: str,
    ) -> Optional[str]:
        """Return the stored alias target for one concept, or ``None``."""
        doc = self.collection.find_one(
            {
                "cik": cik,
                "statement_type": statement_type,
                "form_type": form_type,
                "concept": concept,
            },
            {"target_concept": 1},
        )
        if not doc:
            return None
        target = doc.get("target_concept")
        return target if isinstance(target, str) and target else None

    def get_many(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        concepts: Iterable[str],
    ) -> Dict[str, str]:
        """Return ``{concept: target_concept}`` for the concepts that have one."""
        names = [c for c in set(concepts) if c]
        if not names:
            return {}
        cursor = self.collection.find(
            {
                "cik": cik,
                "statement_type": statement_type,
                "form_type": form_type,
                "concept": {"$in": names},
            },
            {"concept": 1, "target_concept": 1},
        )
        return {
            d["concept"]: d["target_concept"]
            for d in cursor
            if d.get("concept") and d.get("target_concept")
        }

    def list_for_company(self, cik: str) -> List[dict]:
        """All decisions recorded for a company (audit / debugging)."""
        return list(self.collection.find({"cik": cik}))

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def record(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        concept: str,
        target_concept: str,
        *,
        decided_by: str = "concept_agent",
        reason: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        """Upsert one decision — ``concept`` is the incoming name, ``target``
        the existing stored concept its values must attach to."""
        if not concept or not target_concept:
            return
        now = _now()
        self.collection.update_one(
            {
                "cik": cik,
                "statement_type": statement_type,
                "form_type": form_type,
                "concept": concept,
            },
            {
                "$set": {
                    "target_concept": target_concept,
                    "decided_by": decided_by,
                    "reason": reason,
                    "confidence": confidence,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    def record_many(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        decisions: Dict[str, str],
        *,
        decided_by: str = "concept_agent",
        reasons: Optional[Dict[str, str]] = None,
    ) -> int:
        """Upsert a batch of ``{concept: target_concept}`` decisions."""
        written = 0
        for concept, target in (decisions or {}).items():
            if not concept or not target:
                continue
            self.record(
                cik, statement_type, form_type, concept, target,
                decided_by=decided_by,
                reason=(reasons or {}).get(concept),
            )
            written += 1
        return written

    def promote(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        from_concept: str,
        to_concept: str,
        *,
        reason: Optional[str] = None,
    ) -> int:
        """Re-key the decision store when ``to_concept`` becomes canonical.

        Called by the newest-period promotion: the winning concept name takes
        over as the canonical row and the losing concept row is hard-deleted.
        Every member that used to resolve to the loser must now resolve to the
        winner, and the winner itself must no longer alias to anything (it IS
        canonical).  Finally the old tag is recorded as pointing at the winner
        so a future filing that reports the legacy tag again costs zero LLM
        calls.

        Returns the number of member rows repointed.
        """
        if not from_concept or not to_concept or from_concept == to_concept:
            return 0
        now = _now()
        repointed = self.collection.update_many(
            {
                "cik": cik,
                "statement_type": statement_type,
                "form_type": form_type,
                "target_concept": from_concept,
            },
            {"$set": {"target_concept": to_concept, "updated_at": now}},
        )
        # The winner is canonical now — drop any stale alias onto the loser.
        self.collection.delete_many(
            {
                "cik": cik,
                "statement_type": statement_type,
                "form_type": form_type,
                "concept": to_concept,
            }
        )
        # Legacy tag keeps resolving to the winner for future filings.
        self.record(
            cik, statement_type, form_type, from_concept, to_concept,
            decided_by="concept_promotion", reason=reason,
        )
        return getattr(repointed, "modified_count", 0)


__all__ = ["ConceptAliasStore"]
