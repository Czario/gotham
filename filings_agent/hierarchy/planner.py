"""Deterministic planner + validator for agent-proposed hierarchies.

The agent decides the *shape* of the hierarchy (which row is a grouping header,
what parents what, what order siblings sit in, and which legacy rows to hide).
This module turns that decision into a concrete, structurally valid tree:

* materialised paths (``001.002.003``) and lexicographic order keys are computed
  here — never by the model, so they can never be malformed;
* the proposal is validated (parents exist, no cycles, no duplicate siblings,
  no duplicate concepts) and any error is returned so the agent can fix it and
  re-propose.

An invalid or unavailable proposal never reaches the database: the caller keeps
the deterministic resolver's plan instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

_ORDER_CHARS = "abcdefghijklmnopqrstuvwxyz"


def order_key(position: int) -> str:
    """0-based sibling position → ``a``, ``b`` … ``z``, ``aa``, ``ab`` …

    Identical encoding to the resolver, so agent-placed siblings sort the same
    way as deterministically-placed ones.
    """
    position = max(0, int(position))
    if position < len(_ORDER_CHARS):
        return _ORDER_CHARS[position]
    position -= len(_ORDER_CHARS)
    first, second = divmod(position, len(_ORDER_CHARS))
    if first < len(_ORDER_CHARS):
        return _ORDER_CHARS[first] + _ORDER_CHARS[second]
    position -= len(_ORDER_CHARS) * len(_ORDER_CHARS)
    first, rest = divmod(position, len(_ORDER_CHARS) * len(_ORDER_CHARS))
    second, third = divmod(rest, len(_ORDER_CHARS))
    return _ORDER_CHARS[first] + _ORDER_CHARS[second] + _ORDER_CHARS[third]


@dataclass
class PlannedRow:
    """One row of the proposed tree, with its computed placement."""

    concept: str
    parent: Optional[str] = None
    position: int = 0
    abstract: bool = False
    hide: bool = False
    path: str = ""
    order_key: str = ""
    after: Optional[str] = None


@dataclass
class HierarchyPlan:
    rows: list[PlannedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    max_depth: int = 0

    @property
    def valid(self) -> bool:
        return not self.errors

    def by_concept(self) -> dict[str, PlannedRow]:
        return {row.concept: row for row in self.rows}

    def path_of(self, concept: str) -> Optional[str]:
        row = self.by_concept().get(concept)
        return row.path if row else None


def _as_rows(proposal: Any) -> list[dict]:
    if isinstance(proposal, dict):
        proposal = proposal.get("rows") or proposal.get("hierarchy") or proposal.get("placements") or []
    if not isinstance(proposal, list):
        return []
    return [r for r in proposal if isinstance(r, dict)]


def _parent_path(path: str | None) -> str:
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[0]


def _path_depth(path: str | None) -> int:
    return len(path.split(".")) - 1 if path else 0


def plan_hierarchy(
    proposal: Any,
    *,
    known_concepts: Iterable[str] = (),
    stored_rows: Iterable[dict] = (),
) -> HierarchyPlan:
    """Turn an agent proposal into a validated tree with paths and order keys.

    ``known_concepts`` are concepts that already exist in the database — they
    may be named as a parent without being re-proposed (an existing grouping
    header the filing does not repeat).

    ``stored_rows`` are existing stored documents from MongoDB with their
    assigned paths and order keys. When provided, incremental proposals (e.g.
    placing new concepts into an existing hierarchy) are placed relative to the
    stored parents and siblings without needing to re-propose the entire tree.
    """
    raw_rows = _as_rows(proposal)
    plan = HierarchyPlan()
    if not raw_rows:
        plan.errors.append("proposal is empty")
        return plan

    stored_list = [dict(r) for r in stored_rows if isinstance(r, dict)]
    stored_by_concept = {
        r.get("concept"): r for r in stored_list if r.get("concept")
    }
    known = {str(c) for c in known_concepts} | set(stored_by_concept.keys())
    occupied_paths = {
        r["path"]: r.get("concept", "")
        for r in stored_list
        if r.get("path")
    }
    occupied_pairs = {
        (r["path"], r["order_key"]): r.get("concept", "")
        for r in stored_list
        if r.get("path") and r.get("order_key")
    }

    for index, raw in enumerate(raw_rows):
        concept = str(raw.get("concept") or "").strip()
        if not concept:
            plan.errors.append(f"row {index}: missing 'concept'")
            continue
        parent = raw.get("parent")
        parent = str(parent).strip() or None if parent is not None else None
        after = raw.get("after")
        after = str(after).strip() or None if after is not None else None
        explicit_path = str(raw.get("path") or "").strip()
        explicit_order = str(raw.get("order_key") or "").strip()
        try:
            position = int(raw.get("position", index))
        except (TypeError, ValueError):
            plan.errors.append(f"{concept}: 'position' must be an integer")
            continue
        is_hide = bool(raw.get("hide", False))
        is_custom_abstract = bool(raw.get("abstract", False)) or (
            concept.startswith("custom:") and bool(stored_by_concept.get(concept, {}).get("abstract"))
        )
        if is_custom_abstract and (explicit_path == "555" or is_hide):
            # Custom grouping headers must NEVER be sent to 555 or hidden.
            # If already stored at a valid path, preserve its stored path and order key;
            # otherwise clear explicit_path so it is placed under its parent.
            is_hide = False
            stored_match = stored_by_concept.get(concept)
            if stored_match and stored_match.get("path") and not stored_match.get("path", "").startswith("555"):
                explicit_path = stored_match["path"]
                explicit_order = stored_match.get("order_key") or ""
            else:
                explicit_path = ""
                explicit_order = ""
        elif explicit_path == "555" or explicit_path.startswith("555."):
            explicit_path = "555"
        elif is_hide:
            explicit_path = "555"

        plan.rows.append(
            PlannedRow(
                concept=concept,
                parent=parent,
                position=position,
                abstract=bool(raw.get("abstract", False)),
                hide=is_hide,
                path=explicit_path,
                order_key=explicit_order,
                after=after,
            )
        )

    seen: dict[str, PlannedRow] = {}
    for row in plan.rows:
        if row.concept in seen:
            plan.errors.append(f"duplicate concept in proposal: {row.concept}")
        seen[row.concept] = row

    # Preserve existing stored custom grouping headers that the proposal omitted
    for s in stored_list:
        c = s.get("concept", "")
        if c.startswith("custom:") and s.get("abstract") and s.get("path") and not s.get("path", "").startswith("555"):
            if c not in seen:
                plan.rows.append(
                    PlannedRow(
                        concept=c,
                        parent=None,
                        position=0,
                        abstract=True,
                        hide=False,
                        path=s["path"],
                        order_key=s.get("order_key") or "a",
                    )
                )
                seen[c] = plan.rows[-1]

    # Parents must exist (proposed, or already stored). Path 555 has no parent.
    for row in plan.rows:
        if row.parent and row.parent not in seen and row.parent not in known:
            plan.errors.append(
                f"{row.concept}: parent '{row.parent}' is neither proposed nor already stored"
            )

    # Sibling positions must be distinct, otherwise the order is ambiguous.
    siblings: dict[Optional[str], set[int]] = {}
    for index, row in enumerate(plan.rows):
        if row.path == "555" or (row.path and row.order_key):
            continue
        bucket = siblings.setdefault(row.parent, set())
        # Only check duplicate position if multiple proposed under same parent
        if row.position in bucket:
            plan.errors.append(
                f"{row.concept}: position {row.position} is already used under "
                f"'{row.parent or 'root'}'"
            )
        bucket.add(row.position)

    # Cycles.
    for row in plan.rows:
        walked: set[str] = {row.concept}
        parent = row.parent
        while parent:
            if parent in walked:
                plan.errors.append(f"hierarchy cycle detected at '{row.concept}'")
                break
            walked.add(parent)
            parent_row = seen.get(parent)
            parent = parent_row.parent if parent_row else None

    if plan.errors:
        return plan

    # Auto-assign order_key for path 555 if missing
    used_555_orders = {
        r.get("order_key") for r in stored_list if r.get("path") == "555" and r.get("order_key")
    }
    for row in plan.rows:
        if row.path == "555":
            if not row.order_key:
                idx = 0
                while order_key(idx) in used_555_orders:
                    idx += 1
                row.order_key = order_key(idx)
            used_555_orders.add(row.order_key)

    # Handle explicit paths/order keys first: validate they don't collide.
    proposed_paths: set[str] = {r.path for r in plan.rows if r.path}
    for row in plan.rows:
        if row.path and row.order_key:
            pair = (row.path, row.order_key)
            if pair in occupied_pairs and occupied_pairs[pair] != row.concept:
                plan.errors.append(
                    f"{row.concept}: path '{row.path}' and order_key '{row.order_key}' already occupied by '{occupied_pairs[pair]}'"
                )
            elif row.path != "555" and row.path in occupied_paths and occupied_paths[row.path] != row.concept:
                plan.errors.append(
                    f"{row.concept}: path '{row.path}' already occupied by '{occupied_paths[row.path]}'"
                )
            parent_p = _parent_path(row.path)
            if parent_p and not row.path.startswith("555") and parent_p not in occupied_paths and parent_p not in proposed_paths:
                plan.errors.append(
                    f"{row.concept}: parent path '{parent_p}' does not exist for path '{row.path}'"
                )
            plan.max_depth = max(plan.max_depth, _path_depth(row.path))

    # Group unassigned rows by parent
    children: dict[Optional[str], list[PlannedRow]] = {}
    for row in plan.rows:
        if not (row.path and row.order_key):
            children.setdefault(row.parent, []).append(row)
    for bucket in children.values():
        bucket.sort(key=lambda r: (r.position, r.concept))

    # Recursive walk for proposed subtrees
    def _walk_subtree(parent_concept: str, parent_path: str) -> None:
        for index, row in enumerate(children.get(parent_concept, [])):
            component = f"{row.position + 1:03d}" if row.position >= 0 else f"{index + 1:03d}"
            row.path = f"{parent_path}.{component}" if parent_path else component
            row.order_key = order_key(index)
            plan.max_depth = max(plan.max_depth, _path_depth(row.path))
            _walk_subtree(row.concept, row.path)

    proposed_concepts = {r.concept for r in plan.rows}

    # 1. Subtrees hanging off existing stored parents
    for parent_concept, parent_doc in stored_by_concept.items():
        if parent_concept in children:
            parent_path = parent_doc.get("path") or ""
            # find existing stored children under this parent that are NOT being re-proposed
            unproposed_children = [
                r for r in stored_list
                if _parent_path(r.get("path")) == parent_path
                and r.get("concept") not in proposed_concepts
            ]
            prefix = f"{parent_path}." if parent_path else ""
            existing_nums = [
                int(r["path"][len(prefix):])
                for r in unproposed_children
                if r.get("path", "").startswith(prefix) and r["path"][len(prefix):].isdigit()
            ]
            max_num = max(existing_nums, default=0)
            existing_order_count = len(unproposed_children)

            for index, row in enumerate(children[parent_concept]):
                if unproposed_children:
                    component = f"{max_num + index + 1:03d}"
                    row.path = f"{parent_path}.{component}" if parent_path else component
                    row.order_key = order_key(existing_order_count + index)
                else:
                    component = f"{row.position + 1:03d}" if row.position >= 0 else f"{index + 1:03d}"
                    row.path = f"{parent_path}.{component}" if parent_path else component
                    row.order_key = order_key(index)
                plan.max_depth = max(plan.max_depth, _path_depth(row.path))
                _walk_subtree(row.concept, row.path)

    # 2. Roots (parent is None)
    if None in children:
        unproposed_roots = [
            r for r in stored_list
            if not _parent_path(r.get("path"))
            and r.get("concept") not in proposed_concepts
        ]
        if unproposed_roots:
            existing_root_nums = [
                int(r["path"]) for r in unproposed_roots
                if r.get("path") and r["path"].isdigit()
            ]
            max_root_num = max(existing_root_nums, default=0)
            existing_root_order_count = len(unproposed_roots)

            for index, row in enumerate(children[None]):
                component = f"{max_root_num + index + 1:03d}"
                row.path = component
                row.order_key = order_key(existing_root_order_count + index)
                plan.max_depth = max(plan.max_depth, _path_depth(row.path))
                _walk_subtree(row.concept, row.path)
        else:
            # Full tree proposal or seed filing: layout based on proposal order
            for index, row in enumerate(children[None]):
                component = f"{row.position + 1:03d}" if row.position >= 0 else f"{index + 1:03d}"
                row.path = component
                row.order_key = order_key(index)
                plan.max_depth = max(plan.max_depth, _path_depth(row.path))
                _walk_subtree(row.concept, row.path)

    # Paths must be unique across the proposal (except 555 which holds non-relevant concepts)
    paths = [row.path for row in plan.rows if row.path and row.path != "555"]
    if len(paths) != len(set(paths)):
        plan.errors.append("proposal produces duplicate paths")

    pairs = [(row.path, row.order_key) for row in plan.rows if row.path and row.order_key]
    if len(pairs) != len(set(pairs)):
        plan.errors.append("proposal produces duplicate (path, order_key) pairs")

    return plan
