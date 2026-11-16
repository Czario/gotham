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
        sec_pres = state.get("sec_presentation")
        if not sec_pres and statement_docs:
            try:
                from core.extractors.sec_presentation_extractor import build_sec_presentation_snapshot
                sec_pres = build_sec_presentation_snapshot(statement_docs=statement_docs)
            except Exception:
                sec_pres = None

        # Align bundle concept signs to the human-visible SEC .htm table sign (single truth)
        if isinstance(sec_pres, dict):
            from core.extractors.sec_presentation_extractor import canonical_statement_type
            stmts_pres = sec_pres.get("statements") or {}
            for bundle in bundles:
                canon = canonical_statement_type(getattr(bundle, "statement_type", ""))
                pres_nodes = (stmts_pres.get(canon) or {}).get("nodes") or []
                sign_by_concept = {
                    str(n["concept"]): (n.get("html_display_sign") or n.get("expected_display_sign"))
                    for n in pres_nodes
                    if isinstance(n, dict) and n.get("concept") and (n.get("html_display_sign") in (-1, 1) or n.get("expected_display_sign") in (-1, 1))
                }
                dim_sign_by_key = {}
                for n in pres_nodes:
                    if not isinstance(n, dict) or not n.get("concept"):
                        continue
                    for dm in n.get("dimensional_members") or []:
                        if isinstance(dm, dict) and dm.get("member") and dm.get("display_sign") in (-1, 1):
                            dim_sign_by_key[(str(n["concept"]), str(dm["member"]))] = dm["display_sign"]

                for item in getattr(bundle, "concepts", None) or []:
                    if not isinstance(item, dict) or item.get("calculated"):
                        continue
                    c_name = str(item.get("concept") or "")
                    val = item.get("value")
                    if c_name in sign_by_concept and isinstance(val, (int, float)) and val != 0:
                        exp_sign = sign_by_concept[c_name]
                        item["value"] = float(exp_sign) * abs(float(val))
                        item["html_display_sign"] = exp_sign
                    for df in item.get("dimensional_facts") or []:
                        if not isinstance(df, dict):
                            continue
                        d_val = df.get("value")
                        if not isinstance(d_val, (int, float)) or d_val == 0:
                            continue
                        members = [
                            det.get("member_qname")
                            for det in (df.get("dimension_details") or {}).values()
                            if isinstance(det, dict) and det.get("member_qname")
                        ]
                        for m_qname in members:
                            d_sign = dim_sign_by_key.get((c_name, str(m_qname)))
                            if d_sign in (-1, 1):
                                df["value"] = float(d_sign) * abs(float(d_val))
                                break

        return {**state, "bundles": bundles, "sec_presentation": sec_pres, "status": "normalized"}

    return normalize_bundle_node
