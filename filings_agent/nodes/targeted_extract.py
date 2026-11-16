"""Targeted text extraction node — reads filing text for missing XBRL concepts.

When automated XBRL extraction finishes, this node compares the extracted concepts
against the company's stored target concepts (excluding 404/555 polluted paths).
For any placed target concept missing a value for the target reporting period,
an extraction agent navigates the filing plain text using document tools (search,
read_lines, get_document_info) to extract the reported figure.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from ..agent.loop import run_agent_loop
from ..hooks import report_call
from ..mda import find_local_html, read_html_file, html_to_text
from ..period import build_detected_period, format_period_label
from normalization.src.data_normalization_service.database.database import (
    is_polluted_path,
    _POLLUTED_PATH_RX,
)

logger = logging.getLogger(__name__)

_EXTRACT_FINALIZE_DESCRIPTION = """\
Call this when you have finished searching and extracting the missing targeted concepts.
Pass result_json as a JSON string:
[{"concept": "us-gaap:ConceptName", "value": 123456000.0, "statement_type": "income", "label": "Concept Label"}]
Only include metrics you actually found in the document with high confidence.
If a metric cannot be found, omit it from the list.
"""


def _is_quarterly_form(form_type: str) -> bool:
    return str(form_type or "").upper().startswith("10-Q")


def _extract_bundle_period_str(bundle: Any) -> Optional[str]:
    """Get the string period from existing items in bundle, or fallback to reporting_period date."""
    if hasattr(bundle, "concepts") and isinstance(bundle.concepts, list):
        for c in bundle.concepts:
            if isinstance(c, dict) and isinstance(c.get("period"), str) and c.get("period"):
                return c.get("period")
    if hasattr(bundle, "source_items") and isinstance(bundle.source_items, list):
        for c in bundle.source_items:
            if isinstance(c, dict) and isinstance(c.get("period"), str) and c.get("period"):
                return c.get("period")
    rp = getattr(bundle, "reporting_period", None)
    if isinstance(rp, dict):
        pdate = rp.get("period_date") or rp.get("period_end") or ""
        return str(pdate) if pdate else None
    return None


def make_targeted_extract_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
) -> Callable[[dict], dict]:
    """Build the targeted text extraction node."""

    def targeted_extract_node(state: dict) -> dict:
        if state.get("status") in ("failed", "skipped"):
            return state

        bundles = state.get("bundles") or []
        if not bundles:
            return state

        cik = str(state.get("cik") or "")
        ticker = state.get("ticker") or ""
        form_type = str(state.get("form_type") or "10-Q")
        accession_number = str(state.get("accession_number") or "")

        repo_resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
        concept_repo = repo_resolver(form_type) if callable(repo_resolver) else None
        sec_presentation = state.get("sec_presentation") or {}

        # If html_text is available and rendered_html_tables haven't been parsed yet, enrich sec_presentation
        html_text_pre = state.get("html_text")
        if html_text_pre and isinstance(sec_presentation, dict) and not sec_presentation.get("rendered_html_tables"):
            try:
                from core.extractors.sec_presentation_extractor import build_sec_presentation_snapshot
                sec_presentation = build_sec_presentation_snapshot(
                    statement_docs=state.get("statement_docs"),
                    html_text=html_text_pre,
                )
            except Exception:
                pass

        sign_fixes = list(state.get("sign_fixes") or [])

        # 1. Collect missing targeted concepts across statements (from stored DB targets AND SEC presentation)
        missing_by_stmt: dict[str, list[dict]] = {}
        for bundle in bundles:
            stmt_type = getattr(bundle, "statement_type", None)
            if not stmt_type:
                continue

            # Check extracted concepts in bundle
            extracted_concepts: set[str] = set()
            for item in getattr(bundle, "concepts", None) or []:
                if isinstance(item, dict) and item.get("concept"):
                    val = item.get("value")
                    if val is not None and val != "":
                        extracted_concepts.add(str(item["concept"]))
            for item in getattr(bundle, "dimensional_concepts", None) or []:
                if isinstance(item, dict) and item.get("concept"):
                    val = item.get("value") or (item.get("dimension_data") or {}).get("value")
                    if val is not None and val != "":
                        extracted_concepts.add(str(item["concept"]))

            # 1a. Recover any concrete SEC presentation nodes that were dropped during transform/normalize
            #     AND synchronize value signs & hierarchy parents with the human-visible HTML table.
            from core.extractors.sec_presentation_extractor import canonical_statement_type
            canon_st = canonical_statement_type(stmt_type)
            stmt_pres = (sec_presentation.get("statements") or {}).get(canon_st) or {}
            pres_by_concept = {
                str(n.get("concept")): n
                for n in (stmt_pres.get("nodes") or [])
                if isinstance(n, dict) and n.get("concept")
            }
            for pres_node in stmt_pres.get("nodes") or []:
                if not isinstance(pres_node, dict) or pres_node.get("abstract"):
                    continue
                p_concept = str(pres_node.get("concept") or "")
                p_val = pres_node.get("value")
                if p_concept and p_concept not in extracted_concepts and p_val is not None:
                    if hasattr(bundle, "concepts") and isinstance(bundle.concepts, list):
                        bundle.concepts.append({
                            "concept": p_concept,
                            "label": pres_node.get("label") or p_concept.split(":")[-1],
                            "value": float(p_val),
                            "period": pres_node.get("period") or _extract_bundle_period_str(bundle),
                            "parent_concept": pres_node.get("html_parent_concept") or pres_node.get("parent_concept"),
                            "reading_order": pres_node.get("html_reading_order") or pres_node.get("reading_order"),
                            "source": "sec_presentation_recovery",
                        })
                        extracted_concepts.add(p_concept)

            # Enforce exact human-visible HTML table sign and hierarchy on existing bundle concepts
            for item in getattr(bundle, "concepts", None) or []:
                if not isinstance(item, dict) or not item.get("concept"):
                    continue
                c_name = str(item["concept"])
                pnode = pres_by_concept.get(c_name)
                if not pnode:
                    continue
                # Sync HTML parent_concept and reading_order if missing on bundle item
                html_parent = pnode.get("html_parent_concept") or pnode.get("parent_concept")
                if html_parent and not item.get("parent_concept"):
                    item["parent_concept"] = html_parent
                r_order = pnode.get("html_reading_order") or pnode.get("reading_order")
                if r_order is not None and item.get("reading_order") is None:
                    item["reading_order"] = r_order

                # Sync exact human-visible HTML display sign
                exp_sign = pnode.get("html_display_sign") or pnode.get("expected_display_sign")
                cur_val = item.get("value")
                if isinstance(cur_val, (int, float)) and cur_val != 0 and exp_sign in (-1, 1):
                    cur_sign = -1 if cur_val < 0 else 1
                    if cur_sign != exp_sign:
                        new_val = float(exp_sign) * abs(float(cur_val))
                        item["value"] = new_val
                        for v_entry in getattr(bundle, "values", None) or []:
                            if isinstance(v_entry, dict) and v_entry.get("concept") == c_name:
                                v_entry["value"] = new_val
                        sign_fixes.append({
                            "statement_type": stmt_type,
                            "concept": c_name,
                            "old_value": float(cur_val),
                            "new_value": new_val,
                            "required_sign": "negative" if exp_sign == -1 else "positive",
                            "source": "sec_rendered_html_table",
                        })

            # 1b. Query stored target concepts (excluding 404/555)
            stored_targets = []
            if concept_repo is not None:
                try:
                    stored_cursor = concept_repo.collection.find({
                        "cik": cik,
                        "statement_type": stmt_type,
                        "path": {"$not": _POLLUTED_PATH_RX},
                        "abstract": {"$ne": True},
                        "hide": {"$ne": True},
                        "dimension_concept": {"$ne": True},
                    })
                    stored_targets = [
                        c for c in stored_cursor
                        if not is_polluted_path(c.get("path"))
                        and not c.get("dimension_concept")
                        and c.get("path") not in (None, "", "404", "555")
                    ]
                    stored_targets.sort(
                        key=lambda c: (
                            bool(c.get("dimension_concept")),
                            len(str(c.get("path", "")).split(".")),
                            str(c.get("path", "")),
                            str(c.get("order_key", "")),
                        )
                    )
                except Exception as exc:
                    logger.warning("targeted_extract: could not query stored targets: %s", exc)
                    stored_targets = []

            if not stored_targets:
                continue

            # Find which stored targets have no value extracted
            missing = []
            for target in stored_targets:
                t_concept = str(target.get("concept") or "")
                aliases = set(target.get("aliases") or [])
                if t_concept not in extracted_concepts and not (aliases & extracted_concepts):
                    missing.append(target)

            if missing:
                missing_by_stmt[stmt_type] = missing

        total_missing = sum(len(m) for m in missing_by_stmt.values())
        if total_missing == 0:
            logger.debug("targeted_extract: all targeted concepts present in XBRL extraction")
            if sign_fixes != (state.get("sign_fixes") or []):
                return {**state, "sec_presentation": sec_presentation, "sign_fixes": sign_fixes}
            return state

        report_call(
            f"  [text extract]  {total_missing} targeted concept(s) missing after XBRL → reading filing text"
        )

        # 2. Load filing HTML plain text
        html_text = state.get("html_text")
        if not html_text:
            local_path = find_local_html(
                accession_number, ticker=ticker, html_dir=os.getenv("SEC_HTML_DOWNLOAD_PATH")
            )
            if local_path:
                html_text = read_html_file(local_path)

        if not html_text:
            logger.info("targeted_extract: no filing HTML available for %s — skipping text extraction", accession_number)
            return state

        plain_text = html_to_text(html_text)
        if not plain_text or len(plain_text) < 1000:
            logger.info("targeted_extract: filing plain text empty or too short — skipping")
            return state

        # 3. Build document navigation tools + ADK built-in load_artifacts tool
        from ..adk.builtin_tools import (
            InMemoryArtifactService,
            build_adk_load_artifacts_tool,
            wrap_custom_function_tool,
        )
        from ..tools.guidance_tools import build_mda_tools

        artifact_svc = InMemoryArtifactService()
        artifact_svc.save_artifact("filing_plain_text_summary", plain_text[:8000])
        if sec_presentation:
            artifact_svc.save_artifact("sec_presentation", sec_presentation)
        tools = [wrap_custom_function_tool(t) for t in build_mda_tools(plain_text)]
        tools.append(build_adk_load_artifacts_tool(artifact_svc))

        period = build_detected_period(state.get("reporting_period"), form_type=form_type)
        period_label = format_period_label(period) if period else form_type

        # Build prompt
        lines = [
            f"You are a financial extraction agent. For {ticker or cik} ({form_type} {period_label}), "
            "the following targeted concepts were not extracted from the primary XBRL tables.",
            "Use search() and read_lines() to inspect the financial statement tables, segment disclosures, "
            "or notes in the filing text to extract the exact numbers reported for this period.\n",
            "Targeted concepts needing values:",
        ]
        def _normalize_label(lbl: str) -> str:
            s = str(lbl or "").strip().lower()
            for prefix in ("total ", "net ", "consolidated "):
                if s.startswith(prefix):
                    s = s[len(prefix):].strip()
            return re.sub(r"[^a-z0-9]", "", s)

        for stmt_type, targets in missing_by_stmt.items():
            lines.append(f"\nStatement: {stmt_type.upper()}")
            for t in targets[:25]:  # limit to top 25 per statement to preserve context
                t_concept = str(t.get('concept') or '')
                t_label = str(t.get('label') or '')
                synonyms = ""
                if "operatingexpenses" in t_concept.lower() or "operating expense" in t_label.lower():
                    synonyms = " (also look for 'Operating expenses' or standard tag 'us-gaap:OperatingExpenses')"
                lines.append(
                    f"  • Concept: {t_concept} | Label: {t_label}{synonyms} | Path: {t.get('path')} | Parent: {t.get('parent_concept')}"
                )

        lines.append(
            "\nRules:\n"
            "1. Locate the exact table or note in the document text containing each concept.\n"
            "2. Note the table units (e.g. in millions, thousands, or exact dollars) and scale accordingly.\n"
            "3. Call finalize_extraction(result_json='[...]') with the extracted figures.\n"
            "4. Only return metrics you find with high confidence in the text for this period.\n"
            "5. If a stored concept has a 'system:' or 'custom:' prefix (such as system:TotalOperatingExpenses), "
            "and the filing reports it under standard US-GAAP (such as us-gaap:OperatingExpenses) or table label, "
            "return the standard concept tag (e.g. us-gaap:OperatingExpenses). The system will update the stored concept in-place."
        )
        system_prompt = "\n".join(lines)
        initial_message = (
            f"Please locate and extract values for the missing targeted concepts for {period_label}. "
            "Call get_document_info() or search() to begin."
        )

        def _parse_result(raw: str) -> Optional[list[dict]]:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    if "repairs" in data:
                        return data["repairs"]
                    if "values" in data:
                        return data["values"]
                    return [data]
            except Exception:
                pass
            return None

        # 4. Run agent loop
        try:
            result = run_agent_loop(
                system_prompt=system_prompt,
                initial_message=initial_message,
                tools=tools,
                chat_llm=chat_llm,
                ticker=ticker or cik,
                max_steps=12,
                finalize_name="finalize_extraction",
                finalize_description=_EXTRACT_FINALIZE_DESCRIPTION,
                parse_final_result=_parse_result,
            )
        except Exception as exc:
            logger.warning("targeted_extract: agent loop failed (%s) — continuing with XBRL values", exc)
            result = None

        # 5. Incorporate extracted values into the appropriate bundle
        extracted_count = 0
        bundle_by_stmt = {getattr(b, "statement_type", ""): b for b in bundles}

        for item in (result or []):
            if not isinstance(item, dict):
                continue
            concept_tag = item.get("concept")
            val = item.get("value")
            stmt_type = (item.get("statement_type") or "income").lower()
            if not concept_tag or val is None:
                continue

            bundle = bundle_by_stmt.get(stmt_type)
            if not bundle:
                continue

            try:
                num_val = float(val)
            except (ValueError, TypeError):
                continue

            # Find matching stored target concept
            stored_list = missing_by_stmt.get(stmt_type, [])
            matched_target = None
            for t in stored_list:
                if t.get("concept") == concept_tag:
                    matched_target = t
                    break
                if concept_tag in (t.get("aliases") or []):
                    matched_target = t
                    break

            if not matched_target:
                item_clean = _normalize_label(item.get("label") or concept_tag)
                for t in stored_list:
                    if _normalize_label(t.get("label") or "") == item_clean:
                        matched_target = t
                        break

            if not matched_target and any(k in concept_tag.lower() for k in ["operatingexpense", "totaloperatingexpense"]):
                for t in stored_list:
                    if any(k in str(t.get("concept", "")).lower() for k in ["operatingexpense", "totaloperatingexpense"]) or \
                       any(k in str(t.get("label", "")).lower() for k in ["operating expense", "total operating expense"]):
                        matched_target = t
                        break

            new_label = item.get("label") or (matched_target.get("label") if matched_target else concept_tag)
            stored_path = matched_target.get("path") if matched_target else None
            stored_order = matched_target.get("order_key") if matched_target else None
            stored_id = matched_target.get("_id") if matched_target else None

            # Update concept in place by _id if a standard concept tag is found for a stored concept
            if matched_target and stored_id and concept_tag != matched_target.get("concept"):
                stored_concept = matched_target.get("concept")
                new_aliases = list(set((matched_target.get("aliases") or []) + [stored_concept, concept_tag]))
                concept_repo.collection.update_one(
                    {"_id": stored_id},
                    {"$set": {
                        "concept": concept_tag,
                        "aliases": new_aliases,
                        "label": new_label,
                        "canonical_concept": concept_tag,
                    }}
                )
                logger.info(
                    "targeted_extract: updated stored concept %s in place from %s -> %s (label: %r)",
                    stored_id, stored_concept, concept_tag, new_label,
                )
                report_call(
                    f"  [text extract]  ⇄ updated concept {stored_concept} → {concept_tag} in-place"
                )
                matched_target["concept"] = concept_tag
                matched_target["aliases"] = new_aliases

            bundle_period_str = _extract_bundle_period_str(bundle)
            new_item = {
                "concept": concept_tag,
                "label": new_label,
                "value": num_val,
                "decimals": -6,
                "unit": "USD",
                "period": bundle_period_str,
                "source": "targeted_text_extraction",
            }
            if stored_path:
                new_item["path"] = stored_path
                new_item["order_key"] = stored_order
                new_item["_hierarchy_resolved"] = True
                new_item["hierarchy_level"] = len(str(stored_path).split(".")) - 1
                new_item["level"] = new_item["hierarchy_level"]
            if stored_id:
                new_item["concept_id"] = stored_id

            if hasattr(bundle, "concepts") and isinstance(bundle.concepts, list):
                bundle.concepts.append(new_item)
            if hasattr(bundle, "source_items") and isinstance(bundle.source_items, list):
                bundle.source_items.append(new_item)
            extracted_count += 1

        # 6. Fallback calculation for Total Operating Expenses if still missing in income statement
        income_bundle = bundle_by_stmt.get("income")
        if income_bundle and hasattr(income_bundle, "concepts") and isinstance(income_bundle.concepts, list):
            has_opex = any(
                c.get("concept") in ("us-gaap:OperatingExpenses", "system:TotalOperatingExpenses") or
                "operating expenses" in str(c.get("label", "")).lower()
                for c in income_bundle.concepts
                if c.get("value") is not None and c.get("value") != ""
            )
            if not has_opex:
                components: dict[str, float] = {}
                for c in income_bundle.concepts:
                    tag = str(c.get("concept") or "")
                    val = c.get("value")
                    if val is not None and val != "":
                        try:
                            fval = float(val)
                            if tag in ("us-gaap:ResearchAndDevelopmentExpense", "us-gaap:ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost"):
                                components["rd"] = fval
                            elif tag in ("us-gaap:SellingAndMarketingExpense", "us-gaap:SellingExpense", "us-gaap:MarketingExpense"):
                                components["sm"] = fval
                            elif tag in ("us-gaap:GeneralAndAdministrativeExpense",):
                                components["ga"] = fval
                            elif tag in ("us-gaap:SellingGeneralAndAdministrativeExpense",):
                                components["sga"] = fval
                        except (ValueError, TypeError):
                            pass

                stored_targets_inc = missing_by_stmt.get("income", [])
                matched_opex = next(
                    (
                        t for t in stored_targets_inc
                        if "operatingexpenses" in str(t.get("concept", "")).lower()
                        or "operating expense" in str(t.get("label", "")).lower()
                    ),
                    None,
                )

                sum_opex = None
                if "rd" in components and "sm" in components and "ga" in components:
                    sum_opex = components["rd"] + components["sm"] + components["ga"]
                elif "rd" in components and "sga" in components:
                    sum_opex = components["rd"] + components["sga"]
                elif "sm" in components and "ga" in components:
                    sum_opex = components["sm"] + components["ga"]
                elif "sga" in components and matched_opex is not None:
                    sum_opex = components["sga"]

                if sum_opex is not None and matched_opex is not None:
                    target_tag = "us-gaap:OperatingExpenses"
                    target_label = "Total Operating Expenses"
                    target_path = "004"
                    target_order = "a"
                    target_id = None
                    if matched_opex:
                        target_id = matched_opex.get("_id")
                        target_path = matched_opex.get("path") or target_path
                        target_order = matched_opex.get("order_key") or target_order
                        target_label = matched_opex.get("label") or target_label
                        stored_concept = matched_opex.get("concept")
                        if stored_concept != target_tag and target_id:
                            new_aliases = list(set((matched_opex.get("aliases") or []) + [stored_concept, target_tag]))
                            concept_repo.collection.update_one(
                                {"_id": target_id},
                                {"$set": {
                                    "concept": target_tag,
                                    "aliases": new_aliases,
                                    "canonical_concept": target_tag,
                                }}
                            )
                            report_call(
                                f"  [calculate]  ⇄ updated concept {stored_concept} → {target_tag} in-place"
                            )

                    income_period_str = _extract_bundle_period_str(income_bundle)
                    calc_item = {
                        "concept": target_tag,
                        "label": target_label,
                        "value": sum_opex,
                        "decimals": -6,
                        "unit": "USD",
                        "period": income_period_str,
                        "source": "calculated_operating_expenses",
                        "path": target_path,
                        "order_key": target_order,
                        "_hierarchy_resolved": True,
                        "hierarchy_level": len(str(target_path).split(".")) - 1,
                        "level": len(str(target_path).split(".")) - 1,
                    }
                    if target_id:
                        calc_item["concept_id"] = target_id
                    income_bundle.concepts.append(calc_item)
                    if hasattr(income_bundle, "source_items") and isinstance(income_bundle.source_items, list):
                        income_bundle.source_items.append(calc_item)
                    extracted_count += 1
                    report_call(
                        f"  [calculate]  ✓ Total Operating Expenses = {sum_opex:,.0f} calculated from components"
                    )

        if extracted_count > 0:
            report_call(
                f"  [text extract]  ✓ extracted/calculated {extracted_count} missing targeted concept(s)"
            )

        return {**state, "bundles": bundles, "status": "targeted_extract_done"}

    return targeted_extract_node
