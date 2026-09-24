"""P5 hierarchy resolver.

Responsibilities:

* First company/statement filing: preserve the complete filing hierarchy as the
  seed.
* Later filings: reuse the same company's existing concept path/order first.
* New concepts: use the filing's presentation position when free, then a
  cross-company reference, then a conflict-safe sibling placement.
* Never create duplicate ``(path, order_key)`` pairs for different concepts.

This module only mutates in-memory bundles. Company seed metadata is returned in
``HierarchyPlan`` and is written by the persist node, preserving the rule that
agent persistence is the only DB writer.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


def _parent_path(path: str | None) -> str:
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[0]


def _path_depth(path: str | None) -> int:
    return len(path.split(".")) - 1 if path else 0


def _order_key(position: int) -> str:
    """0-based sibling position → a, b, ..., z, aa, ab..."""
    chars = "abcdefghijklmnopqrstuvwxyz"
    if position < 26:
        return chars[position]
    position -= 26
    first, second = divmod(position, 26)
    return chars[first % 26] + chars[second]


def _as_docs(cursor: Any) -> list[dict]:
    if cursor is None:
        return []
    if isinstance(cursor, list):
        return cursor
    try:
        return list(cursor)
    except TypeError:
        return []


def _numeric_child_component(paths: Iterable[str], parent: str) -> int:
    maximum = 0
    prefix = f"{parent}." if parent else ""
    for path in paths:
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        tail = path[len(prefix):]
        if "." in tail or not tail.isdigit():
            continue
        maximum = max(maximum, int(tail))
    return maximum + 1


def _nearest_existing_parent(path: str, existing_paths: set[str]) -> str:
    parent = _parent_path(path)
    while parent and parent not in existing_paths:
        parent = _parent_path(parent)
    return parent


def _free_placement(
    candidate_path: str,
    candidate_order: str,
    concept: str,
    occupied: dict[tuple[str, str], str],
    existing_paths: set[str],
    valid_parents: set[str] | None = None,
) -> tuple[str, str, str]:
    """Return a free path/order/source for a new concept."""
    candidate_path = candidate_path or ""
    candidate_order = candidate_order or ""
    allowed_parents = valid_parents if valid_parents is not None else existing_paths
    if candidate_path and candidate_order and (candidate_path, candidate_order) not in occupied:
        parent_p = _parent_path(candidate_path)
        if not parent_p or parent_p in allowed_parents or candidate_path.startswith("555"):
            return candidate_path, candidate_order, "filing_order"

    if candidate_path == "555" or candidate_path.startswith("555"):
        used_orders = {
            order for (p, order) in occupied
            if p == "555"
        }
        idx = 0
        while _order_key(idx) in used_orders:
            idx += 1
        return "555", _order_key(idx), "auxiliary_supplementary"

    parent = _nearest_existing_parent(candidate_path, existing_paths)
    used_paths = {path for path, _ in occupied}
    next_component = _numeric_child_component(used_paths, parent)
    path = f"{parent}.{next_component:03d}" if parent else f"{next_component:03d}"

    sibling_orders = sorted(
        order for (existing_path, order), _name in occupied.items()
        if _parent_path(existing_path) == parent
    )
    order = _order_key(len(sibling_orders))
    while (path, order) in occupied:
        next_component += 1
        path = f"{parent}.{next_component:03d}" if parent else f"{next_component:03d}"
        order = _order_key(len(sibling_orders))
    return path, order, "conflict_safe_sibling"


def _repo_docs(norm_service: Any, bundle: Any) -> tuple[Any, list[dict]]:
    resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(resolver):
        return None, []
    repo = resolver(getattr(bundle, "form_type", None))
    query = {
        "cik": getattr(bundle, "company_cik", None),
        "statement_type": getattr(bundle, "statement_type", None),
        "dimension_concept": False,
        "concept": {"$not": {"$regex": "Abstract$"}},
        "$or": [
            {"abstract": {"$ne": True}},
            {"concept": {"$regex": "^custom:"}},
        ],
    }
    try:
        docs = _as_docs(repo.collection.find(query))
    except Exception:
        docs = []
    return repo, docs


def _classify_segmentation(dim: dict) -> tuple[str, str]:
    """Classify a dimensional concept into a custom grouping header.

    Returns (header_concept, header_label), e.g.:
    ('custom:ProductSegmentation', 'Product Segmentation') or
    ('custom:GeographicSegmentation', 'Geographic Segmentation')
    """
    concept_str = str(dim.get("concept") or "").lower()
    label_str = str(dim.get("label") or "").lower()
    seg_type = str(dim.get("segment_type") or "").lower()
    dim_data = dim.get("dimension_data") or {}
    dimensions = dim_data.get("dimensions") or {}
    axis_names = [str(k).lower() for k in dimensions.keys() if k != "explicitMember"]

    # Check for geographic cues
    geo_axes = any(
        "geograph" in ax or "region" in ax or "country" in ax or "territory" in ax or "area" in ax
        for ax in axis_names
    )
    geo_terms = (
        "americas", "europe", "china", "japan", "asia", "pacific", "uscanada", "restofworld",
        "international", "domestic", "foreign", "emea", "apac", "northamerica", "latinamerica",
        "unitedstates", "germany", "france", "uk", "unitedkingdom", "taiwan", "india",
    )
    is_geo_member = any(term in concept_str or term in label_str for term in geo_terms)
    is_geo = geo_axes or seg_type in ("geographic", "geographic_segment") or is_geo_member

    if is_geo:
        return "custom:GeographicSegmentation", "Geographic Segmentation"

    # Check for product / service cues
    prod_axes = any(
        "product" in ax or "service" in ax or "offering" in ax
        for ax in axis_names
    )
    prod_terms = (
        "product", "service", "hardware", "software", "subscription", "device",
        "ad", "advertising", "iphone", "ipad", "mac", "wearable", "cloud",
    )
    is_prod_member = any(term in concept_str or term in label_str for term in prod_terms)
    is_prod = prod_axes or seg_type == "product_service" or is_prod_member

    if is_prod:
        return "custom:ProductSegmentation", "Product Segmentation"

    # Fallback based on segment_type
    if seg_type and seg_type != "unknown":
        pascal = "".join(part.capitalize() for part in seg_type.replace("-", "_").split("_"))
        spaced = " ".join(part.capitalize() for part in seg_type.replace("-", "_").split("_"))
        return f"custom:{pascal}Segmentation", f"{spaced} Segmentation"

    return "custom:OtherSegmentation", "Other Segmentation"


def _is_revenue_concept(concept: str) -> bool:
    """True for a top-line revenue/sales line item.

    The old check (``"revenue" in concept``) also matched ``us-gaap:CostOfRevenue``
    and similar cost lines, which then grew the *same* custom segmentation
    headers as the real revenue line — a second header path that could never be
    stored (only one row per concept name exists) and orphaned every child.
    """
    local = str(concept or "").split(":")[-1].lower()
    if any(bad in local for bad in ("cost", "expense", "discount", "allowance")):
        return False
    return "revenue" in local or local.startswith("sales") or "netsales" in local


def _resolve_dimensional_segmentation(
    bundle: Any,
    items_to_resolve: list[dict],
    existing_by_concept: dict[str, dict],
    occupied: dict[tuple[str, str], str],
    existing_paths: set[str],
    is_seed: bool,
    plan: dict[str, Any],
    norm_service: Any = None,
) -> None:
    dim_concepts = getattr(bundle, "dimensional_concepts", None) or []
    if not dim_concepts:
        return

    abstract_list = getattr(bundle, "abstract_concepts", None)
    if abstract_list is None:
        abstract_list = []
        bundle.abstract_concepts = abstract_list

    existing_dims_by_key: dict[tuple[str, str], dict] = {}
    if norm_service is not None:
        resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
        if callable(resolver):
            try:
                repo = resolver(getattr(bundle, "form_type", None))
                stored_dims = list(repo.collection.find({
                    "cik": getattr(bundle, "company_cik", None),
                    "statement_type": getattr(bundle, "statement_type", None),
                    "dimension_concept": True,
                }))
                id_to_concept = {doc["_id"]: doc["concept"] for doc in existing_by_concept.values() if "_id" in doc}
                for sd in stored_dims:
                    parent_c = id_to_concept.get(sd.get("concept_id"))
                    if parent_c:
                        existing_dims_by_key[(parent_c, sd.get("concept"))] = sd
            except Exception:
                pass

    parent_dims: dict[str, list[dict]] = defaultdict(list)
    for dim in dim_concepts:
        if isinstance(dim, dict) and dim.get("parent_concept") and dim.get("concept"):
            parent_dims[dim["parent_concept"]].append(dim)

    for parent_concept, dims in parent_dims.items():
        parent_item = next((it for it in items_to_resolve if it.get("concept") == parent_concept), None)
        if not parent_item:
            continue
        parent_path = parent_item.get("path")
        if not parent_path:
            continue

        is_revenue = _is_revenue_concept(parent_concept)

        if is_revenue:
            groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for dim in dims:
                hdr_concept, hdr_label = _classify_segmentation(dim)
                groups[(hdr_concept, hdr_label)].append(dim)

            sorted_groups = sorted(
                groups.items(),
                key=lambda g: (0 if "Product" in g[0][0] else (1 if "Geograph" in g[0][0] else 2), g[0][0])
            )

            for (hdr_concept, hdr_label), group_dims in sorted_groups:
                existing_hdr = existing_by_concept.get(hdr_concept)
                bundle_hdr = next((a for a in abstract_list if a.get("concept") == hdr_concept), None)
                header_path = None
                header_order = None

                if existing_hdr and existing_hdr.get("path") and _parent_path(existing_hdr.get("path")) == parent_path:
                    header_path = existing_hdr["path"]
                    header_order = existing_hdr.get("order_key") or "a"
                    if not any(a.get("concept") == hdr_concept for a in abstract_list):
                        abstract_list.append(existing_hdr)
                    if not any(it.get("concept") == hdr_concept for it in items_to_resolve):
                        items_to_resolve.append(existing_hdr)
                elif bundle_hdr and bundle_hdr.get("path") and _parent_path(bundle_hdr.get("path")) == parent_path:
                    header_path = bundle_hdr["path"]
                    header_order = bundle_hdr.get("order_key") or "a"
                    if not any(it.get("concept") == hdr_concept for it in items_to_resolve):
                        items_to_resolve.append(bundle_hdr)
                elif existing_hdr or bundle_hdr:
                    # The same grouping header already hangs under a DIFFERENT
                    # parent.  Only one row per concept name can exist in the
                    # database, so creating a second one here would leave this
                    # group's children attached to a path that is never stored
                    # (orphans).  Keep the members parent-scoped by placing
                    # them directly under their real line item instead.
                    header_path = None
                    header_order = None
                else:
                    hdr_num = _numeric_child_component(existing_paths, parent_path)
                    header_path = f"{parent_path}.{hdr_num:03d}"
                    sibling_orders = [o for (p, o) in occupied.items() if _parent_path(p) == parent_path]
                    header_order = _order_key(len(sibling_orders))
                    new_header = {
                        "concept": hdr_concept,
                        "label": hdr_label,
                        "abstract": True,
                        "path": header_path,
                        "order_key": header_order,
                        "hierarchy_level": _path_depth(header_path),
                        "level": _path_depth(header_path),
                        "hierarchy_source": "fresh_seed" if is_seed else "filing_order",
                        "_hierarchy_resolved": True,
                    }
                    abstract_list.append(new_header)
                    items_to_resolve.append(new_header)
                    existing_by_concept[hdr_concept] = new_header
                    plan["resolved_concepts"] += 1

                base_path = header_path or parent_path
                if header_path:
                    occupied[(header_path, header_order)] = hdr_concept
                    existing_paths.add(header_path)

                for idx, dim in enumerate(group_dims):
                    existing_dim = existing_dims_by_key.get((parent_concept, dim.get("concept")))
                    if (
                        existing_dim
                        and existing_dim.get("path")
                        and existing_dim.get("order_key")
                        and _parent_path(existing_dim["path"]) == base_path
                        and (existing_dim["path"], existing_dim["order_key"]) not in occupied
                    ):
                        dim_path = existing_dim["path"]
                        dim_order = existing_dim["order_key"]
                    else:
                        dim_num = _numeric_child_component(existing_paths, base_path)
                        dim_path = f"{base_path}.{dim_num:03d}"
                        dim_sibling_orders = [o for (p, o) in occupied.items() if _parent_path(p) == base_path]
                        dim_order = _order_key(len(dim_sibling_orders))

                    dim["path"] = dim_path
                    dim["order_key"] = dim_order
                    dim["hierarchy_level"] = _path_depth(dim_path)
                    dim["level"] = _path_depth(dim_path)
                    dim["_hierarchy_resolved"] = True
                    dim["hierarchy_source"] = "same_company_existing" if existing_dim else ("fresh_seed" if is_seed else "filing_order")
                    dim["parent_header"] = hdr_concept if header_path else None
                    dim["parent_concept"] = parent_concept
                    occupied[(dim_path, dim_order)] = dim["concept"]
                    existing_paths.add(dim_path)
        else:
            for dim in dims:
                existing_dim = existing_dims_by_key.get((parent_concept, dim.get("concept")))
                if (
                    existing_dim
                    and existing_dim.get("path")
                    and existing_dim.get("order_key")
                    and _parent_path(existing_dim["path"]) == parent_path
                    and (existing_dim["path"], existing_dim["order_key"]) not in occupied
                ):
                    dim_path = existing_dim["path"]
                    dim_order = existing_dim["order_key"]
                else:
                    dim_num = _numeric_child_component(existing_paths, parent_path)
                    dim_path = f"{parent_path}.{dim_num:03d}"
                    dim_sibling_orders = [o for (p, o) in occupied.items() if _parent_path(p) == parent_path]
                    dim_order = _order_key(len(dim_sibling_orders))

                dim["path"] = dim_path
                dim["order_key"] = dim_order
                dim["hierarchy_level"] = _path_depth(dim_path)
                dim["level"] = _path_depth(dim_path)
                dim["_hierarchy_resolved"] = True
                dim["hierarchy_source"] = "same_company_existing" if existing_dim else ("fresh_seed" if is_seed else "filing_order")
                dim["parent_header"] = None
                dim["parent_concept"] = parent_concept
                occupied[(dim_path, dim_order)] = dim["concept"]
                existing_paths.add(dim_path)


def resolve_hierarchy_bundles(
    bundles: Iterable[Any],
    norm_service: Any,
    *,
    cik: str,
) -> dict[str, Any]:
    """Resolve and annotate all bundles; return a serializable hierarchy plan."""
    plan: dict[str, Any] = {
        "cik": cik,
        "resolved_concepts": 0,
        "seeded_statement_types": [],
        "new_concepts": [],
        "conflicts": [],
        "existing_updates": [],
        "integrity": {"duplicate_paths": 0, "orphans": 0},
        "max_depth": 0,
        "sources": defaultdict(int),
        "resolved_at": datetime.now(timezone.utc),
    }

    for bundle in bundles or []:
        statement_type = getattr(bundle, "statement_type", None)
        if statement_type is None or getattr(bundle, "concepts", None) is None:
            # Not a statement bundle (e.g. a placeholder in a test double).
            continue
        repo, existing_docs = _repo_docs(norm_service, bundle)
        existing_by_concept: dict[str, dict] = {}
        occupied: dict[tuple[str, str], str] = {}
        existing_paths: set[str] = set()
        # Normalize duplicate historical path/order pairs first. Concept IDs
        # remain unchanged; only hierarchy metadata moves.
        for original in existing_docs:
            doc = dict(original)
            concept_name = doc.get("concept", "")
            path, order = doc.get("path"), doc.get("order_key")
            if path and order and (path, order) in occupied and occupied[(path, order)] != concept_name:
                new_path, new_order, _source = _free_placement(
                    path, order, concept_name, occupied, existing_paths
                )
                plan["existing_updates"].append({
                    "_id": doc.get("_id"),
                    "statement_type": statement_type,
                    "form_type": getattr(bundle, "form_type", None),
                    "concept": concept_name,
                    "path": new_path,
                    "order_key": new_order,
                    "reason": "duplicate_existing_path_order",
                })
                doc["path"], doc["order_key"] = new_path, new_order
                path, order = new_path, new_order
            if path and order:
                occupied[(path, order)] = concept_name
                existing_paths.add(path)
            if concept_name:
                existing_by_concept[concept_name] = doc

        is_seed = not existing_docs
        if is_seed:
            plan["seeded_statement_types"].append(statement_type)

        # Abstract wrapper concepts are not inserted in the database; resolve concrete concepts
        # and non-wrapper grouping headers.
        items_to_resolve = [
            item for item in (
                list(getattr(bundle, "concepts", None) or [])
                + list(getattr(bundle, "abstract_concepts", None) or [])
            )
            if not item.get("concept", "").split(":")[-1].endswith("Abstract")
        ]
        if not items_to_resolve:
            # Fallback for test doubles that only populated abstract_concepts
            items_to_resolve = list(getattr(bundle, "abstract_concepts", None) or [])

        # Re-root: if all incoming items start with "001." and "001" is not present,
        # strip the dropped root wrapper prefix so line items start at "001", "002"...
        paths_with_dot = [str(it.get("path", "")) for it in items_to_resolve if it.get("path")]
        if paths_with_dot and not any(p == "001" for p in paths_with_dot) and "001" not in existing_paths and all(p.startswith("001.") for p in paths_with_dot):
            for it in items_to_resolve:
                if str(it.get("path", "")).startswith("001."):
                    it["path"] = it["path"][4:]
                    it["hierarchy_level"] = _path_depth(it["path"])
                    it["level"] = it["hierarchy_level"]

        incoming_paths = {str(it.get("path")) for it in items_to_resolve if it.get("path")}
        valid_parents = existing_paths | incoming_paths

        for index, item in enumerate(items_to_resolve):
            if not isinstance(item, dict) or not item.get("concept"):
                continue
            concept = item["concept"]
            existing = existing_by_concept.get(concept)
            source = "fresh_seed" if is_seed else "filing_order"

            if existing and existing.get("path") and existing.get("order_key"):
                path = existing["path"]
                order_key = existing["order_key"]
                source = "same_company_existing"
                item["is_new_concept"] = False
            else:
                item["is_new_concept"] = not is_seed
                if not is_seed:
                    plan.setdefault("new_concepts", []).append({
                        "statement_type": statement_type,
                        "concept": concept,
                        "label": item.get("label") or concept,
                    })
                raw_path = item.get("path") or f"{index + 1:03d}"
                raw_order = item.get("order_key") or _order_key(index)
                path, order_key, source = _free_placement(
                    raw_path,
                    raw_order,
                    concept,
                    occupied,
                    existing_paths,
                    valid_parents=valid_parents,
                )
                if is_seed and source == "filing_order":
                    source = "fresh_seed"

                # If the filing position was occupied, try the historical
                # cross-company majority reference before sibling insertion.
                if source == "conflict_safe_sibling" and not is_seed:
                    try:
                        reference = repo.find_concept_reference_for_hierarchy(
                            statement_type, concept, dimension_concept=False
                        )
                    except Exception:
                        reference = None
                    if reference:
                        ref_path, ref_order = reference.get("path"), reference.get("order_key")
                        if ref_path and ref_order and (ref_path, ref_order) not in occupied:
                            ref_parent = _parent_path(ref_path)
                            if not ref_parent or ref_parent in valid_parents or ref_path.startswith("555"):
                                path, order_key, source = ref_path, ref_order, "cross_company_reference"

            if (path, order_key) in occupied and occupied[(path, order_key)] != concept:
                # Defensive final fallback; this should only be reachable when
                # malformed historical rows already occupy every candidate.
                path, order_key, source = _free_placement(
                    path, order_key, concept, occupied, existing_paths,
                    valid_parents=valid_parents,
                )
                plan["conflicts"].append({
                    "statement_type": statement_type,
                    "concept": concept,
                    "resolved_path": path,
                    "resolved_order_key": order_key,
                })

            item["path"] = path
            item["order_key"] = order_key
            item["hierarchy_level"] = _path_depth(path)
            item["level"] = _path_depth(path)
            item["_hierarchy_resolved"] = True
            item["hierarchy_source"] = source
            occupied[(path, order_key)] = concept
            existing_paths.add(path)
            plan["resolved_concepts"] += 1
            plan["sources"][source] += 1

        _resolve_dimensional_segmentation(
            bundle, items_to_resolve, existing_by_concept, occupied, existing_paths, is_seed, plan, norm_service=norm_service
        )

        # Integrity of the RESOLVED set for this statement: no two DIFFERENT concepts
        # may share a (path, order_key) [exempting auxiliary path 555], and every child
        # path needs its parent path present (in this filing or already stored).
        # Note: statements like Cash Flow may report beginning and ending balances using the
        # same concept; evaluate unique concept-to-placement mappings.
        resolved_by_concept = {}
        for item in items_to_resolve:
            c = item.get("concept")
            p = item.get("path")
            o = item.get("order_key")
            if c and p and o:
                resolved_by_concept[c] = (p, o)

        distinct_pairs = [
            (p, o) for p, o in resolved_by_concept.values()
            if not p.startswith("555")
        ]
        dup_paths = len(distinct_pairs) - len(set(distinct_pairs))
        plan["integrity"]["duplicate_paths"] += dup_paths
        known_paths = {p for p, _ in resolved_by_concept.values()} | existing_paths
        stmt_orphans = sum(
            1 for p, _ in resolved_by_concept.values()
            if "." in p and not p.startswith("555") and p.rsplit(".", 1)[0] not in known_paths
        )
        plan["integrity"]["orphans"] += stmt_orphans
        plan.setdefault("statement_integrity", {})[statement_type] = {
            "duplicate_paths": dup_paths,
            "orphans": stmt_orphans,
        }
        plan["max_depth"] = max(
            [plan["max_depth"]] + [_path_depth(p) for p, _ in resolved_by_concept.values()]
        )

    plan["sources"] = dict(plan["sources"])
    plan["seeded_statement_types"] = sorted(set(plan["seeded_statement_types"]))
    return plan
