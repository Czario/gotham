"""SEC Presentation Ground-Truth Extractor.

Captures the exact SEC filing presentation hierarchy (`_pre.xml`), calculation
rollups (`_cal.xml`), dimensional breakdowns (`_def.xml`), and comparative
prior-period columns from Arelle XBRL data, statement documents, and optional
SEC `FilingSummary.xml` / `R*.htm` tables.

This ground-truth snapshot (`sec_presentation`) is attached to `FilingAgentState`
so downstream ADK agents (Extraction Auditor, Hierarchy Agent, Review Agent)
can compare normalized bundles directly against what was reported on SEC EDGAR.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

_STATEMENT_MAP = {
    "income_statement": "income",
    "income": "income",
    "balance_sheet": "balancesheet",
    "balancesheet": "balancesheet",
    "balance": "balancesheet",
    "cash_flow": "cashflow",
    "cashflow": "cashflow",
}


def canonical_statement_type(raw: Optional[str]) -> str:
    """Normalize a statement type string to 'income', 'balancesheet', or 'cashflow'."""
    if not raw:
        return ""
    cleaned = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _STATEMENT_MAP.get(cleaned, cleaned)


def _serialize_line_item_tree(
    items: list[Any],
    *,
    out_nodes: list[dict[str, Any]],
    comparative_by_period: dict[str, dict[str, list[dict[str, Any]]]],
    effective_parent: Optional[str] = None,
    position_counter: Optional[list[int]] = None,
) -> None:
    """Flatten Arelle ``FinancialLineItem`` hierarchy while preserving exact SEC tree edges."""
    if position_counter is None:
        position_counter = [0]

    for item in items or []:
        if isinstance(item, dict):
            concept = item.get("concept") or item.get("concept_name") or ""
            label = item.get("label") or ""
            value = item.get("value")
            period = item.get("period") or ""
            level = int(item.get("level") or 0)
            order = float(item.get("order") or 0.0)
            abstract = bool(item.get("abstract", False))
            raw_parent = item.get("sec_parent_concept") or item.get("parent_concept") or effective_parent
            resolved_parent = item.get("parent_concept") or effective_parent
            pref_role = item.get("preferred_label_role")
            calcs = item.get("calculations") or {}
            dim_facts = item.get("dimensional_facts") or item.get("all_dimensional_facts") or []
            comp_facts = item.get("comparative_facts") or []
            comp_dims = item.get("comparative_dimensional_facts") or []
            children = item.get("children") or []
        else:
            concept = getattr(item, "concept_name", "") or ""
            label = getattr(item, "label", "") or ""
            value = getattr(item, "value", None)
            period = getattr(item, "period", "") or ""
            level = int(getattr(item, "level", 0) or 0)
            order = float(getattr(item, "order", 0.0) or 0.0)
            abstract = bool(getattr(item, "abstract", False))
            raw_parent = getattr(item, "parent_concept", None) or effective_parent
            resolved_parent = effective_parent or raw_parent
            pref_role = getattr(item, "preferred_label_role", None)
            calcs = getattr(item, "calculations", None) or {}
            dim_facts = getattr(item, "all_dimensional_facts", None) or []
            comp_facts = getattr(item, "comparative_facts", None) or []
            comp_dims = getattr(item, "comparative_dimensional_facts", None) or []
            children = getattr(item, "children", None) or []

        if not concept:
            continue

        position_counter[0] += 1
        reading_order = position_counter[0]

        dim_members = []
        for df in dim_facts:
            if not isinstance(df, dict):
                continue
            details = df.get("dimension_details") or {}
            member = (
                df.get("member")
                or df.get("dimension_member")
                or (next(iter(details.values()), None) if isinstance(details, dict) and details else None)
            )
            axis = (
                df.get("axis")
                or df.get("dimension_axis")
                or (next(iter(details.keys()), None) if isinstance(details, dict) and details else None)
            )
            dim_members.append({
                "member": member,
                "axis": axis,
                "label": df.get("member_label") or df.get("label") or member,
                "value": df.get("value"),
                "period": df.get("period") or period,
            })

        calc_children = []
        calc_parents = []
        if isinstance(calcs, dict):
            for c_rel in (calcs.get("summation_children") or calcs.get("children") or []):
                if isinstance(c_rel, dict) and c_rel.get("concept"):
                    calc_children.append({
                        "concept": c_rel["concept"],
                        "weight": c_rel.get("weight", 1.0),
                    })
            for p_rel in (calcs.get("summation_parents") or calcs.get("parents") or []):
                if isinstance(p_rel, dict) and p_rel.get("concept"):
                    calc_parents.append({
                        "concept": p_rel["concept"],
                        "weight": p_rel.get("weight", 1.0),
                    })

        # If parent is an *Abstract wrapper and calculation parent is a concrete line item,
        # record the calculation parent as a fallback concrete parent hint.
        calc_parent_hint = calc_parents[0]["concept"] if calc_parents else None
        calc_weight = calc_parents[0].get("weight", 1.0) if calc_parents else None

        # Determine expected human-visible display sign from preferredLabel (negatedLabel)
        is_negated_label = bool(pref_role and "negated" in str(pref_role).lower())
        expected_display_sign: Optional[int] = None
        if isinstance(value, (int, float)) and value != 0:
            expected_display_sign = -1 if value < 0 else 1

        node_entry = {
            "concept": concept,
            "label": label,
            "value": value,
            "period": period,
            "level": level,
            "order": order,
            "reading_order": reading_order,
            "abstract": abstract,
            "parent_concept": resolved_parent if resolved_parent != concept else None,
            "sec_parent_concept": raw_parent if raw_parent != concept else None,
            "calc_parent_concept": calc_parent_hint,
            "calc_weight": calc_weight,
            "preferred_label_role": pref_role,
            "is_negated_label": is_negated_label,
            "expected_display_sign": expected_display_sign,
            "calculation_children": calc_children,
            "calculation_parents": calc_parents,
            "dimensional_members": dim_members,
        }
        out_nodes.append(node_entry)

        for cf in comp_facts:
            if isinstance(cf, dict) and cf.get("period") and cf.get("value") is not None:
                p_key = str(cf["period"])
                comparative_by_period[p_key]["concepts"].append({
                    **cf,
                    "parent_concept": node_entry["parent_concept"],
                    "calc_parent_concept": calc_parent_hint,
                    "reading_order": reading_order,
                })

        for cdf in comp_dims:
            if isinstance(cdf, dict) and cdf.get("period") and cdf.get("value") is not None:
                p_key = str(cdf["period"])
                comparative_by_period[p_key]["dimensional_facts"].append({
                    **cdf,
                    "parent_concept": concept,
                    "reading_order": reading_order,
                })

        if children:
            local = concept.split(":")[-1]
            is_wrapper = local.endswith(("Abstract", "Table", "LineItems", "Domain")) or concept.endswith("Axis")
            next_eff_parent = effective_parent if is_wrapper else concept
            _serialize_line_item_tree(
                children,
                out_nodes=out_nodes,
                comparative_by_period=comparative_by_period,
                effective_parent=next_eff_parent,
                position_counter=position_counter,
            )


def parse_rendered_sec_html_tables(html_text: Optional[str]) -> dict[str, list[dict[str, Any]]]:
    """Parse rendered SEC HTML / Inline-XBRL / R*.htm financial statement tables.

    Extracts statement tables (income, balancesheet, cashflow) directly from rendered
    SEC HTML so the agent sees both the XBRL linkbase and the human-visible SEC HTML
    table structure — and can fall back to pure HTML extraction when no XBRL exists.
    """
    if not html_text or not isinstance(html_text, str):
        return {}
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    soup = BeautifulSoup(html_text, "html.parser")
    extracted_tables: dict[str, list[dict[str, Any]]] = {}

    # Map common SEC HTML row labels to standard US-GAAP concepts when ix:nonFraction is absent
    _LABEL_TO_CONCEPT = {
        "total revenue": ("income", "us-gaap:Revenues"),
        "total revenues": ("income", "us-gaap:Revenues"),
        "total net sales": ("income", "us-gaap:Revenues"),
        "revenues": ("income", "us-gaap:Revenues"),
        "net sales": ("income", "us-gaap:Revenues"),
        "cost of sales": ("income", "us-gaap:CostOfGoodsAndServicesSold"),
        "cost of revenues": ("income", "us-gaap:CostOfRevenue"),
        "cost of revenue": ("income", "us-gaap:CostOfRevenue"),
        "gross profit": ("income", "us-gaap:GrossProfit"),
        "gross margin": ("income", "us-gaap:GrossProfit"),
        "research and development": ("income", "us-gaap:ResearchAndDevelopmentExpense"),
        "selling, general and administrative": ("income", "us-gaap:SellingGeneralAndAdministrativeExpense"),
        "total operating expenses": ("income", "us-gaap:OperatingExpenses"),
        "operating expenses": ("income", "us-gaap:OperatingExpenses"),
        "operating income": ("income", "us-gaap:OperatingIncomeLoss"),
        "operating income (loss)": ("income", "us-gaap:OperatingIncomeLoss"),
        "income before income taxes": ("income", "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
        "provision for income taxes": ("income", "us-gaap:IncomeTaxExpenseBenefit"),
        "income tax expense": ("income", "us-gaap:IncomeTaxExpenseBenefit"),
        "net income": ("income", "us-gaap:NetIncomeLoss"),
        "net income (loss)": ("income", "us-gaap:NetIncomeLoss"),
        "cash and cash equivalents": ("balancesheet", "us-gaap:CashAndCashEquivalentsAtCarryingValue"),
        "accounts receivable, net": ("balancesheet", "us-gaap:AccountsReceivableNetCurrent"),
        "inventories": ("balancesheet", "us-gaap:InventoryNet"),
        "total current assets": ("balancesheet", "us-gaap:AssetsCurrent"),
        "property, plant and equipment, net": ("balancesheet", "us-gaap:PropertyPlantAndEquipmentNet"),
        "total assets": ("balancesheet", "us-gaap:Assets"),
        "accounts payable": ("balancesheet", "us-gaap:AccountsPayableCurrent"),
        "total current liabilities": ("balancesheet", "us-gaap:LiabilitiesCurrent"),
        "total liabilities": ("balancesheet", "us-gaap:Liabilities"),
        "total stockholders' equity": ("balancesheet", "us-gaap:StockholdersEquity"),
        "total shareholders' equity": ("balancesheet", "us-gaap:StockholdersEquity"),
        "total liabilities and stockholders' equity": ("balancesheet", "us-gaap:LiabilitiesAndStockholdersEquity"),
        "total liabilities and shareholders' equity": ("balancesheet", "us-gaap:LiabilitiesAndStockholdersEquity"),
        "net cash provided by operating activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInOperatingActivities"),
        "cash generated by operating activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInOperatingActivities"),
        "net cash used in investing activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInInvestingActivities"),
        "net cash provided by (used in) investing activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInInvestingActivities"),
        "net cash used in financing activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInFinancingActivities"),
        "net cash provided by (used in) financing activities": ("cashflow", "us-gaap:NetCashProvidedByUsedInFinancingActivities"),
    }

    def _parse_num_and_sign(cell) -> Optional[tuple[float, int]]:
        """Extract (scaled_human_visible_value, display_sign) from an HTML <td>/<th> cell."""
        cell_text = cell.get_text(" ", strip=True)
        cleaned = cell_text.strip().replace("$", "").replace(",", "").replace("\u00a0", "").strip()
        if not cleaned or cleaned in ("-", "—", "–", "N/A"):
            return None
        neg = False
        if ("(" in cleaned and ")" in cleaned) or cleaned.startswith(("-", "–", "—", "−")):
            neg = True
        num_match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if not num_match:
            return None
        try:
            mag = float(num_match.group(0))
        except ValueError:
            return None

        # Check Inline XBRL scale attribute (e.g. scale="6" -> millions, scale="3" -> thousands)
        ix_el = cell.find(re.compile(r"^ix:nonfraction$", re.I))
        if ix_el is not None and ix_el.get("scale") is not None:
            try:
                scale_pow = int(ix_el.get("scale"))
                mag = mag * (10 ** scale_pow)
            except (ValueError, TypeError):
                pass

        if mag == 0:
            return (0.0, 0)
        signed_val = -mag if neg else mag
        display_sign = -1 if neg else 1
        return (signed_val, display_sign)

    for table in soup.find_all("table"):
        rows_out: list[dict[str, Any]] = []
        stmt_votes: dict[str, int] = defaultdict(int)
        order_idx = 0
        pending_section_children: list[dict[str, Any]] = []
        active_header_concept: Optional[str] = None

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            label_raw = cells[0].get_text(" ", strip=True)
            if not label_raw or len(label_raw) > 140:
                continue

            # Detect visual indentation from leading non-breaking spaces or CSS style
            raw_cell_html = str(cells[0])
            style_attr = str(cells[0].get("style") or "").lower()
            indent_level = 0
            if "padding-left" in style_attr or "margin-left" in style_attr or "text-indent" in style_attr:
                indent_level = 1
            elif "\u00a0\u00a0" in cells[0].get_text("", strip=False) or "&nbsp;" in raw_cell_html:
                indent_level = 1

            # Check for Inline XBRL tag if embedded inside the HTML row
            ix_tag = tr.find(re.compile(r"^(ix:nonfraction|ix:nonnumeric)$", re.I))
            ix_concept = ix_tag.get("name") if ix_tag else None

            nums_with_sign: list[tuple[float, int]] = []
            for c in cells[1:]:
                parsed_ns = _parse_num_and_sign(c)
                if parsed_ns is not None:
                    nums_with_sign.append(parsed_ns)

            norm_lbl = label_raw.strip().lower().rstrip(":")
            mapped = _LABEL_TO_CONCEPT.get(norm_lbl)
            if mapped:
                stmt_votes[mapped[0]] += 1

            concept_name = ix_concept or (mapped[1] if mapped else None)
            is_header_row = (not nums_with_sign) and label_raw.strip().endswith(":")
            if not concept_name and not nums_with_sign and not is_header_row:
                continue
            if not concept_name:
                slug = re.sub(r"[^A-Za-z0-9]+", " ", label_raw).title().replace(" ", "")
                if not slug:
                    continue
                concept_name = f"custom:{slug[:60]}" if is_header_row else f"html:{slug[:60]}"

            order_idx += 1
            primary_val = nums_with_sign[0][0] if nums_with_sign else None
            primary_sign = nums_with_sign[0][1] if nums_with_sign else None
            comp_vals = [x[0] for x in nums_with_sign[1:]] if len(nums_with_sign) > 1 else []
            comp_signs = [x[1] for x in nums_with_sign[1:]] if len(nums_with_sign) > 1 else []

            is_total_row = norm_lbl.startswith(("total ", "net cash provided", "net cash used", "gross profit", "operating income", "net income"))
            row_dict: dict[str, Any] = {
                "concept": concept_name,
                "label": label_raw,
                "value": primary_val,
                "display_sign": primary_sign,
                "expected_display_sign": primary_sign,
                "comparative_values": comp_vals,
                "comparative_signs": comp_signs,
                "order": float(order_idx),
                "reading_order": order_idx,
                "level": 0 if (is_header_row or is_total_row) else max(1, indent_level),
                "abstract": primary_val is None and not comp_vals,
                "parent_concept": None,
                "source": "sec_rendered_html",
            }

            if is_header_row:
                active_header_concept = concept_name
                pending_section_children = []
            elif is_total_row and pending_section_children:
                # In human-visible HTML tables, component rows under a section header sum into the
                # section's Total row (e.g. R&D + SG&A -> Total operating expenses; Cash + AR -> Total current assets).
                for child_r in pending_section_children:
                    if child_r["concept"] != concept_name:
                        child_r["parent_concept"] = concept_name
                        child_r["level"] = max(1, int(child_r.get("level") or 1))
                pending_section_children = []
                active_header_concept = None
            elif active_header_concept is not None and primary_val is not None:
                row_dict["parent_concept"] = active_header_concept
                row_dict["level"] = 1
                pending_section_children.append(row_dict)

            rows_out.append(row_dict)

        if stmt_votes and rows_out:
            best_stmt = max(stmt_votes.items(), key=lambda kv: kv[1])[0]
            if len(rows_out) > len(extracted_tables.get(best_stmt, [])):
                extracted_tables[best_stmt] = rows_out

    return extracted_tables


def build_sec_presentation_snapshot(
    *,
    extracted_xbrl: Optional[dict[str, Any]] = None,
    statement_docs: Optional[list[dict[str, Any]]] = None,
    html_text: Optional[str] = None,
) -> dict[str, Any]:
    """Build the ``sec_presentation`` ground-truth snapshot across all statements.

    Returns:
        {
          "statements": {
            "income": {
              "role_uri": "...",
              "description": "...",
              "nodes": [...],
              "concrete_concepts": [...],
              "dimensional_breakdowns": {...},
              "comparative_periods": {...},
            },
            ...
          },
          "outside_presentation_concepts": [...],
        }
    """
    snapshot: dict[str, Any] = {
        "statements": {},
        "outside_presentation_concepts": [],
        "rendered_html_tables": {},
    }

    if html_text:
        snapshot["rendered_html_tables"] = parse_rendered_sec_html_tables(html_text)

    arelle_data = (extracted_xbrl or {}).get("arelle_data") or {}
    arelle_stmts = arelle_data.get("statements") or {}

    if arelle_stmts:
        for raw_type, stmt_payload in arelle_stmts.items():
            if raw_type == "missing_dimensional_concepts":
                outside_items = stmt_payload.get("hierarchy") or []
                for item in outside_items:
                    if isinstance(item, dict) and item.get("concept"):
                        snapshot["outside_presentation_concepts"].append({
                            "concept": item.get("concept"),
                            "label": item.get("label"),
                            "value": item.get("value"),
                            "dimensional_fact_count": len(item.get("dimensional_facts") or []),
                        })
                continue

            canon_type = canonical_statement_type(raw_type)
            if canon_type not in ("income", "balancesheet", "cashflow"):
                continue

            nodes: list[dict[str, Any]] = []
            comp_periods: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
                lambda: {"concepts": [], "dimensional_facts": []}
            )
            hierarchy = stmt_payload.get("hierarchy") or []
            _serialize_line_item_tree(
                hierarchy,
                out_nodes=nodes,
                comparative_by_period=comp_periods,
            )

            concrete = [
                n for n in nodes
                if not n.get("abstract") and (n.get("value") is not None or n.get("dimensional_members"))
            ]
            dim_breakdowns = {
                n["concept"]: n["dimensional_members"]
                for n in nodes
                if n.get("dimensional_members")
            }

            snapshot["statements"][canon_type] = {
                "role_uri": stmt_payload.get("role_uri") or "",
                "description": stmt_payload.get("description") or canon_type,
                "nodes": nodes,
                "concrete_concepts": [n["concept"] for n in concrete],
                "dimensional_breakdowns": dim_breakdowns,
                "comparative_periods": dict(comp_periods),
            }

    # Fallback / enrichment from statement_docs when arelle_data is not present (e.g. fixtures)
    for doc in statement_docs or []:
        if not isinstance(doc, dict):
            continue
        canon_type = canonical_statement_type(doc.get("statement_type"))
        if not canon_type or canon_type in snapshot["statements"]:
            continue

        raw_items = doc.get("data") or doc.get("line_items") or doc.get("concepts") or []
        if not isinstance(raw_items, list):
            continue

        nodes = []
        comp_periods = defaultdict(lambda: {"concepts": [], "dimensional_facts": []})
        _serialize_line_item_tree(
            raw_items,
            out_nodes=nodes,
            comparative_by_period=comp_periods,
        )
        concrete = [
            n for n in nodes
            if not n.get("abstract") and (n.get("value") is not None or n.get("dimensional_members"))
        ]
        dim_breakdowns = {
            n["concept"]: n["dimensional_members"]
            for n in nodes
            if n.get("dimensional_members")
        }
        snapshot["statements"][canon_type] = {
            "role_uri": doc.get("role_uri") or "statement_doc",
            "description": doc.get("description") or canon_type,
            "nodes": nodes,
            "concrete_concepts": [n["concept"] for n in concrete],
            "dimensional_breakdowns": dim_breakdowns,
            "comparative_periods": dict(comp_periods),
        }

    # Fallback / cross-check from rendered SEC HTML tables when XBRL / statement_docs are missing
    for canon_type, html_rows in (snapshot.get("rendered_html_tables") or {}).items():
        if canon_type not in snapshot["statements"] and html_rows:
            snapshot["statements"][canon_type] = {
                "role_uri": "sec_rendered_html",
                "description": f"SEC Rendered HTML ({canon_type})",
                "nodes": html_rows,
                "concrete_concepts": [r["concept"] for r in html_rows if not r.get("abstract")],
                "dimensional_breakdowns": {},
                "comparative_periods": {},
            }
        elif canon_type in snapshot["statements"] and html_rows:
            stmt_entry = snapshot["statements"][canon_type]
            stmt_entry["rendered_html_row_count"] = len(html_rows)
            stmt_entry["rendered_html_rows"] = html_rows
            # Cross-reference rendered HTML display signs, order, and hierarchy onto XBRL nodes
            html_by_concept = {r["concept"]: r for r in html_rows if r.get("concept")}
            html_by_label = {
                str(r.get("label") or "").strip().lower().rstrip(":"): r
                for r in html_rows
                if r.get("label")
            }
            for node in stmt_entry.get("nodes") or []:
                c_name = str(node.get("concept") or "")
                lbl_key = str(node.get("label") or "").strip().lower().rstrip(":")
                h_match = html_by_concept.get(c_name) or html_by_label.get(lbl_key)
                if h_match is not None:
                    if h_match.get("display_sign") in (-1, 1):
                        node["html_display_sign"] = h_match["display_sign"]
                        node["expected_display_sign"] = h_match["display_sign"]
                        if isinstance(node.get("value"), (int, float)) and node["value"] != 0:
                            node["value"] = float(h_match["display_sign"]) * abs(float(node["value"]))
                    if h_match.get("parent_concept") and not str(h_match["parent_concept"]).startswith("custom:"):
                        node["html_parent_concept"] = h_match["parent_concept"]
                        if not node.get("parent_concept"):
                            node["parent_concept"] = h_match["parent_concept"]
                    if h_match.get("reading_order") is not None:
                        node["html_reading_order"] = h_match["reading_order"]

    return snapshot


def format_sec_presentation_tree(
    sec_presentation: Optional[dict[str, Any]],
    statement_type: str,
) -> str:
    """Format the ground-truth SEC presentation tree for agent inspection tools."""
    if not isinstance(sec_presentation, dict):
        return f"No raw SEC presentation linkbase snapshot available for {statement_type}."

    canon = canonical_statement_type(statement_type)
    stmt = (sec_presentation.get("statements") or {}).get(canon)
    if not stmt or not stmt.get("nodes"):
        return f"No raw SEC presentation linkbase snapshot available for {statement_type}."

    lines = [
        f"=== SEC FILING PRESENTATION LINKBASE ({canon.upper()}) ===",
        f"Role URI: {stmt.get('role_uri') or 'N/A'} | Title: {stmt.get('description') or canon}",
        "Columns: [Order] Indent Concept (Label) = Value | SEC Parent | Calc Parent | Breakdowns",
        "-" * 80,
    ]

    for node in stmt.get("nodes") or []:
        indent = "  " * max(0, int(node.get("level") or 0))
        concept = node.get("concept") or "?"
        label = node.get("label") or ""
        val = node.get("value")
        val_str = f" = {val:,.2f}" if isinstance(val, (int, float)) else (" [ABSTRACT]" if node.get("abstract") else "")
        parent = node.get("parent_concept") or node.get("sec_parent_concept") or "ROOT"
        calc_p = node.get("calc_parent_concept")
        calc_str = f" | calc_parent={calc_p}" if calc_p and calc_p != parent else ""
        pref = node.get("preferred_label_role")
        pref_str = f" [{pref.split('/')[-1]}]" if isinstance(pref, str) and pref else ""
        dims = node.get("dimensional_members") or []
        dim_str = f" | {len(dims)} dim member(s)" if dims else ""
        r_order = node.get("reading_order", 0)
        lines.append(
            f"[{r_order:02d}] {indent}{concept} (\"{label}\"){pref_str}{val_str} "
            f"| parent={parent}{calc_str}{dim_str}"
        )
        for dm in dims[:8]:
            m_name = dm.get("member") or "?"
            m_lbl = dm.get("label") or ""
            m_val = dm.get("value")
            m_val_s = f" = {m_val:,.2f}" if isinstance(m_val, (int, float)) else ""
            lines.append(f"       {indent}  ↳ [DIM] {m_name} (\"{m_lbl}\"){m_val_s}")
        if len(dims) > 8:
            lines.append(f"       {indent}  ↳ ... +{len(dims) - 8} more dim member(s)")

    comp = stmt.get("comparative_periods") or {}
    if comp:
        lines.append("")
        lines.append(f"COMPARATIVE HISTORICAL PERIODS IN FILING ({len(comp)}):")
        for p_key, p_data in sorted(comp.items(), reverse=True):
            c_count = len(p_data.get("concepts") or [])
            d_count = len(p_data.get("dimensional_facts") or [])
            lines.append(f"  • Period {p_key}: {c_count} line item(s), {d_count} dimensional fact(s)")

    return "\n".join(lines)
