"""Structural checks — is the bundle internally well-formed?

These run on the in-memory bundle before anything is written, so they catch
hierarchy/data problems the persistence layer would otherwise silently absorb
(e.g. two different concepts claiming the same ``(path, order_key)``, or the
same concept name appearing twice in one statement where the first-wins mapping
would drop the second row's value).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .common import statement_label
from .findings import HIGH, LOW, MEDIUM, Finding


def check_structure(bundle: Any) -> list[Finding]:
    """Run all structural checks for one statement bundle."""
    findings: list[Finding] = []
    st = statement_label(bundle)
    concepts = [c for c in (getattr(bundle, "concepts", None) or []) if isinstance(c, dict)]

    if not concepts:
        findings.append(
            Finding(
                "empty_statement",
                MEDIUM,
                f"{st}: bundle contains no concrete concepts",
                statement_type=st,
            )
        )
        return findings

    # ── concept identity ───────────────────────────────────────────────────
    concept_names = []
    for item in concepts:
        concept = item.get("concept")
        if not concept:
            findings.append(
                Finding(
                    "missing_concept_name",
                    HIGH,
                    f"{st}: a line item has no concept name",
                    statement_type=st,
                    evidence={"label": item.get("label")},
                )
            )
            continue
        concept_names.append(concept)

    # Same concept twice in one statement → the persistence mapping is keyed by
    # concept name, so the second row's values would be dropped.
    for concept, count in Counter(concept_names).items():
        if count > 1:
            findings.append(
                Finding(
                    "duplicate_concept",
                    MEDIUM,
                    f"{st}: concept '{concept}' appears {count} times in one statement "
                    f"(later rows may not persist)",
                    statement_type=st,
                    concept=concept,
                    evidence={"count": count},
                )
            )

    # ── hierarchy placement ────────────────────────────────────────────────
    by_path: dict[tuple, list[str]] = defaultdict(list)
    for item in concepts:
        concept = item.get("concept")
        if not concept:
            continue
        path, order_key = item.get("path"), item.get("order_key")
        if not path or not order_key:
            findings.append(
                Finding(
                    "missing_hierarchy_path",
                    MEDIUM,
                    f"{st}: concept '{concept}' has no (path, order_key)",
                    statement_type=st,
                    concept=concept,
                )
            )
            continue
        by_path[(path, order_key)].append(concept)

    for (path, order_key), names in by_path.items():
        unique = sorted(set(names))
        if len(unique) > 1:
            findings.append(
                Finding(
                    "duplicate_path_order",
                    HIGH,
                    f"{st}: path '{path}' order '{order_key}' is claimed by "
                    f"{len(unique)} different concepts: {', '.join(unique[:4])}",
                    statement_type=st,
                    evidence={"path": path, "order_key": order_key, "concepts": unique},
                )
            )

    # ── levels ─────────────────────────────────────────────────────────────
    negative_levels = [
        item.get("concept") for item in concepts
        if isinstance(item.get("level"), (int, float)) and item["level"] < 0
    ]
    if negative_levels:
        findings.append(
            Finding(
                "negative_hierarchy_level",
                MEDIUM,
                f"{st}: {len(negative_levels)} concept(s) have a negative level",
                statement_type=st,
                evidence={"concepts": negative_levels[:5]},
            )
        )

    # ── abstract leakage (should have been filtered in compute) ────────────
    abstract_leaks = [c.get("concept") for c in concepts if c.get("abstract")]
    if abstract_leaks:
        findings.append(
            Finding(
                "abstract_in_concrete_set",
                LOW,
                f"{st}: {len(abstract_leaks)} abstract concept(s) present in the "
                f"concrete concept list",
                statement_type=st,
                evidence={"concepts": abstract_leaks[:5]},
            )
        )

    return findings
