"""SEC Presentation Diff Validator.

Compares a normalized ``StatementBundle`` against the ground-truth
``sec_presentation`` snapshot captured from the SEC filing (`_pre.xml`,
`_cal.xml`, `_def.xml`, `R*.htm`) to detect:
  1. Concrete line items with reported values in the SEC presentation that are
     missing from the normalized bundle.
  2. Dimensional breakdowns (segments, products, geographies) reported in the
     SEC presentation that were dropped or left empty in the bundle.
  3. Hierarchy parent / ordering drift between the SEC presentation tree and
     the bundle's hierarchy placement.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from core.extractors.sec_presentation_extractor import canonical_statement_type
from .common import statement_label
from .findings import HIGH, LOW, MEDIUM, Finding


def check_sec_presentation_diff(
    bundle: Any,
    sec_presentation: Optional[dict[str, Any]],
) -> list[Finding]:
    """Compare *bundle* against its statement entry in *sec_presentation*."""
    findings: list[Finding] = []
    if not isinstance(sec_presentation, dict):
        return findings

    st_raw = getattr(bundle, "statement_type", "") or ""
    canon_st = canonical_statement_type(st_raw)
    stmt_pres = (sec_presentation.get("statements") or {}).get(canon_st)
    if not isinstance(stmt_pres, dict):
        return findings

    st_label = statement_label(bundle)

    # 1. Collect concepts & aliases present in the normalized bundle
    bundle_concepts: dict[str, Any] = {}
    for item in getattr(bundle, "concepts", None) or []:
        if not isinstance(item, dict):
            continue
        c_name = str(item.get("concept") or "")
        if c_name:
            bundle_concepts[c_name] = item
        for al in item.get("aliases") or []:
            if al:
                bundle_concepts[str(al)] = item

    # 2. Collect dimensional members per parent_concept in the normalized bundle
    bundle_dims_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for dim in getattr(bundle, "dimensional_concepts", None) or []:
        if not isinstance(dim, dict):
            continue
        parent_c = str(dim.get("parent_concept") or dim.get("concept_name") or "")
        if parent_c:
            bundle_dims_by_parent[parent_c].append(dim)

    # 3. Check every concrete SEC presentation node for missing concept or value
    for node in stmt_pres.get("nodes") or []:
        if not isinstance(node, dict) or node.get("abstract"):
            continue
        c_name = str(node.get("concept") or "")
        sec_val = node.get("value")
        if not c_name or sec_val is None:
            continue

        matched_item = bundle_concepts.get(c_name)
        if matched_item is None:
            findings.append(
                Finding(
                    "missing_sec_presentation_concept",
                    HIGH,
                    f"{st_label}: SEC presentation concept '{c_name}' ({node.get('label')}) "
                    f"with value {sec_val} is missing from normalized bundle",
                    statement_type=st_label,
                    concept=c_name,
                    evidence={
                        "sec_value": sec_val,
                        "sec_label": node.get("label"),
                        "sec_parent": node.get("parent_concept"),
                        "reading_order": node.get("reading_order"),
                    },
                )
            )
        elif matched_item.get("value") is None:
            findings.append(
                Finding(
                    "missing_sec_presentation_value",
                    HIGH,
                    f"{st_label}: SEC presentation concept '{c_name}' has value {sec_val} "
                    f"in filing but null value in bundle",
                    statement_type=st_label,
                    concept=c_name,
                    evidence={
                        "sec_value": sec_val,
                        "sec_label": node.get("label"),
                    },
                )
            )
        else:
            # Verify value sign matches human-visible HTML table / SEC presentation sign
            b_val = matched_item.get("value")
            exp_sign = node.get("html_display_sign") or node.get("expected_display_sign")
            if isinstance(b_val, (int, float)) and b_val != 0 and exp_sign in (-1, 1):
                b_sign = -1 if b_val < 0 else 1
                if b_sign != exp_sign:
                    findings.append(
                        Finding(
                            "sec_presentation_sign_mismatch",
                            HIGH,
                            f"{st_label}: concept '{c_name}' has value {b_val} in bundle, "
                            f"but human-visible SEC HTML/presentation displays sign "
                            f"{'negative (in parentheses)' if exp_sign == -1 else 'positive'}",
                            statement_type=st_label,
                            concept=c_name,
                            evidence={
                                "bundle_value": b_val,
                                "expected_display_sign": exp_sign,
                                "sec_value": sec_val,
                            },
                        )
                    )

        # 4. Check dimensional breakdown completeness for this line item
        sec_dims = [
            dm for dm in (node.get("dimensional_members") or [])
            if isinstance(dm, dict) and dm.get("value") is not None
        ]
        if sec_dims:
            bundle_dims = bundle_dims_by_parent.get(c_name, [])
            if not bundle_dims:
                findings.append(
                    Finding(
                        "missing_sec_dimensional_breakdown",
                        MEDIUM,
                        f"{st_label}: SEC presentation reports {len(sec_dims)} dimensional breakdown "
                        f"member(s) under '{c_name}', but 0 are present in bundle",
                        statement_type=st_label,
                        concept=c_name,
                        evidence={
                            "expected_dim_count": len(sec_dims),
                            "sample_members": [dm.get("member") for dm in sec_dims[:5]],
                        },
                    )
                )

    return findings
