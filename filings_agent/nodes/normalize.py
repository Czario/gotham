"""Normalize node — pure compute, NO database access.

Turns the already-extracted/transformed statement docs on the state into
in-memory ``StatementBundle`` objects (the P1 split's compute half).  Nothing is
written to MongoDB here; persistence is the ``persist`` node's job.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def make_normalize_node(norm_service: Any) -> Callable[[dict], dict]:
    """Build the ``normalize_bundle`` node bound to a normalization service.

    Args:
        norm_service: A ``FinancialNormalizationService`` (or any object
            exposing ``normalize_statement_to_bundle``).
    """

    def normalize_bundle_node(state: dict) -> dict:
        statement_docs = state.get("statement_docs") or []
        filing_doc = state.get("filing_doc") or {}
        company_doc = state.get("company_doc") or {}

        bundles = []
        for statement_doc in statement_docs:
            bundle = norm_service.normalize_statement_to_bundle(
                statement_doc, filing_doc, company_doc
            )
            if bundle is not None:
                bundles.append(bundle)

        if not bundles:
            return {
                **state,
                "status": "failed",
                "error": "normalize_bundle produced no bundles",
            }

        logger.info(
            "normalize_bundle: built %d bundle(s) for CIK %s %s",
            len(bundles),
            state.get("cik"),
            state.get("accession_number") or "",
        )
        return {**state, "bundles": bundles, "status": "normalized"}

    return normalize_bundle_node
