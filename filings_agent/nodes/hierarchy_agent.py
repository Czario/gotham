"""Unified, agent-owned hierarchy node.

This node replaces resolve_hierarchy + hierarchy_review + concept_resolve with a
single tool-calling loop. The model pulls the stored tree and filing tree,
decides equivalences, proposes the full tree (line items AND dimensional members),
lints its plan, and finalizes. A thin executor materializes paths; the mapping
tool deterministically enforces that the newer observed financial period wins
when equivalent concept tags are merged.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Callable, Optional

import time

from ..agent.hierarchy_prompts import AGENTIC_HIERARCHY_SYSTEM_PROMPT
from ..tools.hierarchy_tools import build_unified_hierarchy_tools
from ..agent.loop import run_agent_loop
from ..hooks import report_call, report_detail

logger = logging.getLogger(__name__)

_ORDER_CHARS = "abcdefghijklmnopqrstuvwxyz"


def _order_key(position: int) -> str:
    position = max(0, int(position))
    chars = []
    n = position + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(_ORDER_CHARS[rem])
    return "".join(reversed(chars))


def _order_key_index(key: Optional[str]) -> int:
    """Return 0-based position corresponding to _order_key (a->0, z->25, aa->26, etc.)."""
    if not key or not isinstance(key, str):
        return -1
    res = 0
    for ch in key.strip().lower():
        if "a" <= ch <= "z":
            res = res * 26 + (ord(ch) - ord("a") + 1)
        else:
            return -1
    return res - 1


def _fractional_order_key(prev_key: Optional[str], next_key: Optional[str]) -> str:
    """Return a lexicographically sorted string key K such that prev_key < K < next_key."""
    if not prev_key and not next_key:
        return "a"
    if not prev_key and next_key:
        if len(next_key) == 1 and next_key > "a":
            return chr((ord("`") + ord(next_key[0])) // 2)
        return "0n"
    if prev_key and not next_key:
        idx = _order_key_index(prev_key)
        if idx >= 0:
            return _order_key(idx + 1)
        return prev_key + "n"

    p, n = prev_key, next_key
    i = 0
    while i < len(p) and i < len(n) and p[i] == n[i]:
        i += 1
    prefix = p[:i]
    c_low = ord(p[i]) if i < len(p) else ord("`")
    c_high = ord(n[i]) if i < len(n) else ord("{")

    if c_high - c_low > 1:
        mid_char = chr((c_low + c_high) // 2)
        cand = prefix + mid_char
        if p < cand < n:
            return cand

    cand = p + "n"
    if p < cand < n:
        return cand
    cand = p + "m"
    if p < cand < n:
        return cand
    return p + "n"


def _lookup_identity(id_map: Optional[dict], concept: Any, parent: Any, is_dim: bool = False, header: Any = None) -> Any:
    """Robust lookup supporting (concept, parent, is_dim, header), 3-tuple, or (concept, parent)."""
    if not id_map:
        return None
    for k in (
        (concept, parent, is_dim, header),
        (concept, parent, is_dim),
        (concept, parent),
        concept,
    ):
        if k in id_map:
            return id_map[k]
    return None


def _period_info(value: Any) -> tuple[Optional[tuple[int, int, int]], Optional[str]]:
    """Return an orderable (year, month, day) and display label for a period."""
    if isinstance(value, dict):
        for key in ("period_date", "end_date"):
            if value.get(key) is not None:
                rank, label = _period_info(value[key])
                if rank is not None:
                    return rank, label
        year = value.get("fiscal_year") or value.get("year")
        quarter = value.get("quarter")
        try:
            year = int(year)
            if quarter is not None:
                month = int(quarter) * 3
                import calendar
                day = calendar.monthrange(year, month)[1]
                return (year, month, day), f"{year}-Q{int(quarter)}"
            return (year, 12, 31), str(year)
        except (TypeError, ValueError):
            return None, None

    if isinstance(value, datetime):
        return (value.year, value.month, value.day), value.date().isoformat()
    if isinstance(value, date):
        return (value.year, value.month, value.day), value.isoformat()
    if not isinstance(value, str):
        return None, None

    text = value.strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None, None
        return (year, month, day), parsed.isoformat()
    match = re.match(r"^(\d{4})[- ]?Q([1-4])$", text, re.IGNORECASE)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        month = quarter * 3
        import calendar
        return (year, month, calendar.monthrange(year, month)[1]), f"{year}-Q{quarter}"
    match = re.match(r"^(\d{4})$", text)
    if match:
        year = int(match.group(1))
        return (year, 12, 31), str(year)
    return None, None


def _latest_period(values: Any) -> tuple[Optional[tuple[int, int, int]], Optional[str]]:
    latest_rank: Optional[tuple[int, int, int]] = None
    latest_label: Optional[str] = None
    for value in values or []:
        rank, label = _period_info(value)
        if rank is not None and (latest_rank is None or rank > latest_rank):
            latest_rank, latest_label = rank, label
    return latest_rank, latest_label


def _materialize(rows: list[dict], dims: list[dict], stored_paths: dict[str, str],
                 occupied_paths: Optional[set] = None,
                 identity_paths: Optional[dict] = None,
                 identity_order_keys: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Encode the model's STRUCTURE decisions (parent/position/hide) into path
    strings.  The model's own ``path``/``order_key`` fields are ignored except
    that ``hide`` (or an explicit path "555") means the supplementary bucket —
    so a model can never emit a malformed materialised path.

    ``occupied_paths`` is every path already stored for this company+statement
    (main rows, dimensional members and grouping headers).  It is reserved up
    front so a newly-added row can never steal the slot of an existing row the
    agent did not re-propose; existing concepts additionally reuse their own
    stored path so paths stay stable across filings.

    ``path`` is an opaque parent/child identity (prefix = nesting).  Sibling
    ORDER is carried by ``order_key`` — the numeric part of a path is not a
    position and may not match it after a mid-tree insertion; consumers sort
    by ``order_key``, never by path number."""
    norm_rows: list[dict] = []
    norm_dims: list[dict] = []

    import re as _re

    # 1. Collect incoming rows first
    for r in rows:
        if not isinstance(r, dict):
            continue
        item = r.copy()
        c = item.get("concept") or item.get("member")
        if not c:
            continue
        item["concept"] = c
        norm_rows.append(item)

    # 2. Any custom: grouping headers or abstract items proposed in dims belong in rows
    # so they receive row paths and are persisted to the bundle's abstract_concepts.
    for d in dims:
        if not isinstance(d, dict):
            continue
        item = d.copy()
        c = item.get("concept") or item.get("member")
        if not c:
            continue
        item["concept"] = c
        p = item.get("parent") or item.get("parent_concept")
        if str(c).startswith("custom:") or item.get("abstract"):
            item["parent"] = p
            item["abstract"] = True
            norm_rows.append(item)
        else:
            norm_dims.append(item)

    # 3. Synthesize any parent_header referenced in dims (or custom parent) if not already declared in rows
    existing_row_keys: set[tuple[Any, Any]] = {(r.get("parent"), r.get("concept")) for r in norm_rows}
    header_counts: dict[Any, int] = defaultdict(int)
    for d in norm_dims:
        ph = d.get("parent_header")
        pc = d.get("parent_concept") or d.get("parent")
        if not ph and d.get("parent") and str(d.get("parent")).startswith("custom:"):
            ph = d.get("parent")
        if ph and str(ph).startswith("custom:"):
            # A member whose parent IS the header needs no synthesis; only
            # synthesize when we know the real (non-header) line item it hangs
            # under.  Otherwise the header would parent itself (infinite walk).
            if not pc or str(pc).startswith("custom:"):
                continue
            key = (pc, ph)
            if key not in existing_row_keys:
                existing_row_keys.add(key)
                header_counts[pc] += 1
                label = _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(ph).split(":")[-1])
                norm_rows.append({
                    "concept": ph,
                    "parent": pc,
                    "abstract": True,
                    "label": label,
                    "order": header_counts[pc],
                })

    # De-duplicate rows by (parent, concept) while preserving order
    seen_row_keys: set[tuple[Any, Any]] = set()
    deduped_rows: list[dict] = []
    for r in norm_rows:
        key = (r.get("parent"), r["concept"])
        if key not in seen_row_keys:
            seen_row_keys.add(key)
            deduped_rows.append(r)
    norm_rows = deduped_rows

    # Map headers to their parents so members know their line item parent
    header_parents: dict[str, str] = {}
    for r in norm_rows:
        if str(r.get("concept", "")).startswith("custom:") and r.get("parent"):
            header_parents[str(r["concept"])] = str(r["parent"])

    for d in norm_dims:
        p = d.get("parent")
        pc = d.get("parent_concept")
        ph = d.get("parent_header")
        if p and str(p).startswith("custom:"):
            d["parent_header"] = p
            d["parent_concept"] = pc or header_parents.get(str(p))
        elif p and not pc:
            d["parent_concept"] = p
        elif ph and not pc and str(ph) in header_parents:
            d["parent_concept"] = header_parents[str(ph)]

    by_parent: dict[Optional[str], list[dict]] = defaultdict(list)
    for r in norm_rows:
        by_parent[r.get("parent")].append(r)

    row_seq = {id(r): idx for idx, r in enumerate(norm_rows)}

    def _pos(r: dict, default_idx: int) -> int:
        for k in ("position", "order", "index"):
            val = r.get(k)
            if val is not None:
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, str):
                    parts = val.split(".")
                    try:
                        return int(parts[-1])
                    except (ValueError, TypeError):
                        pass
        return default_idx

    def _order_key_to_pos(key: Optional[str]) -> Optional[int]:
        if not key or not isinstance(key, str):
            return None
        key = key.strip().lower()
        if len(key) == 1 and "a" <= key <= "z":
            return ord(key) - ord("a") + 1
        if len(key) == 2 and "a" <= key[0] <= "z" and "a" <= key[1] <= "z":
            return 26 + (ord(key[0]) - ord("a") + 1) * 26 + (ord(key[1]) - ord("a")) + 1
        return None

    def _explicit_pos(r: dict) -> Optional[int]:
        if r.get("_is_stored"):
            return None
        for k in ("position", "order", "index"):
            val = r.get(k)
            if val is not None:
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, str):
                    parts = val.split(".")
                    try:
                        return int(parts[-1])
                    except (ValueError, TypeError):
                        pass
        return None

    def _is_555(r: dict) -> bool:
        return bool(r.get("hide")) or str(r.get("path") or "") == "555"

    path_by_concept: dict[str, str] = {}
    path_by_parent_and_concept: dict[tuple[Optional[str], str], str] = {}
    # Reserve every stored path, then let a re-proposed concept reclaim its own
    # (tracked in ``claimed``) so it is not renumbered just because a sibling
    # was inserted before it.
    used: set[str] = {str(p) for p in (occupied_paths or set()) if p}
    claimed: set[str] = set()

    def _base(parent: Optional[str]) -> str:
        if not parent:
            return ""
        if parent in path_by_concept:
            return path_by_concept[parent]
        if parent in stored_paths:
            return stored_paths[parent]
        return ""

    def _sibling_rank(r: dict) -> tuple:
        c = r.get("concept")
        p = r.get("parent")
        # 1. Stored rows: preserve order strictly by stored order_key
        stored_ok = r.get("order_key")
        if not stored_ok and identity_order_keys:
            stored_ok = _lookup_identity(identity_order_keys, c, p, False)
        if stored_ok and isinstance(stored_ok, str):
            idx = _order_key_index(stored_ok)
            if idx >= 0:
                return (float(idx), row_seq.get(id(r), 0))
        # 2. For root concepts (parent is None): determine relative rank among roots
        if not p:
            pos = _explicit_pos(r)
            if pos is not None:
                return (float(pos) - 0.5, row_seq.get(id(r), 0))
            # If no explicit pos, check relative position in norm_rows among roots
            prev_root_idx = -1
            next_root_idx = None
            r_seq = row_seq.get(id(r), 0)
            for other in norm_rows:
                if other.get("parent") or other.get("hide") or str(other.get("path") or "") == "555":
                    continue
                o_ok = other.get("order_key") or (_lookup_identity(identity_order_keys, other.get("concept"), None, False) if identity_order_keys else None)
                if o_ok and isinstance(o_ok, str):
                    o_idx = _order_key_index(o_ok)
                    if o_idx >= 0:
                        o_seq = row_seq.get(id(other), 0)
                        if o_seq < r_seq:
                            prev_root_idx = max(prev_root_idx, o_idx)
                        elif o_seq > r_seq and next_root_idx is None:
                            next_root_idx = o_idx
            if prev_root_idx >= 0 and next_root_idx is not None:
                return ((prev_root_idx + next_root_idx) / 2.0, r_seq)
            if prev_root_idx >= 0:
                return (float(prev_root_idx) + 0.5, r_seq)
            if next_root_idx is not None:
                return (float(next_root_idx) - 0.5, r_seq)
            return (1000.0, r_seq)

        # 3. For child rows: explicit position or append after stored siblings
        pos = _explicit_pos(r)
        if pos is not None:
            return (1000.0 + pos, row_seq.get(id(r), 0))
        return (2000.0 + row_seq.get(id(r), 0), row_seq.get(id(r), 0))

    def _walk(parent: Optional[str], ancestors: frozenset = frozenset()) -> None:
        base = _base(parent)
        children = sorted(by_parent.get(parent, []), key=_sibling_rank)
        # Collect order_keys already in use by stored siblings under this parent
        local_order: set[str] = set()
        for r in children:
            if _is_555(r):
                continue
            ok = r.get("order_key")
            if not ok and identity_order_keys:
                ok = _lookup_identity(identity_order_keys, r["concept"], r.get("parent"), False)
            if ok and str(ok):
                local_order.add(str(ok))

        for idx, r in enumerate(children):
            if _is_555(r):
                r["path"] = "555"
                r["order_key"] = ""  # filled after all non-555 rows
                r["level"] = 0
            else:
                stored = stored_paths.get(r["concept"]) if stored_paths else None
                if stored is None and identity_paths:
                    stored = _lookup_identity(identity_paths, r["concept"], r.get("parent"), False)
                is_stored = bool(
                    stored
                    and stored != "555"
                    and stored not in claimed
                    and (not base or str(stored).startswith(f"{base}."))
                )
                if is_stored:
                    # Existing stored row: PRESERVE EXACT STORED PATH AND ORDER KEY
                    cand = str(stored)
                    r["path"] = cand
                    r["level"] = len(cand.split(".")) - 1
                    stored_ok = _lookup_identity(identity_order_keys, r["concept"], r.get("parent"), False)
                    if not stored_ok and r.get("order_key"):
                        stored_ok = r.get("order_key")
                    r["order_key"] = str(stored_ok) if stored_ok else "a"
                    local_order.add(str(r["order_key"]))
                    used.add(cand)
                    claimed.add(cand)
                else:
                    # NEW row: allocate incremental path and incremental order key
                    # Find max numeric suffix among direct children under base in used
                    max_suffix = 0
                    for p in used:
                        if base and p.startswith(f"{base}."):
                            suffix_str = p[len(base) + 1:]
                            if "." not in suffix_str and suffix_str.isdigit():
                                max_suffix = max(max_suffix, int(suffix_str))
                        elif not base and "." not in p and p.isdigit():
                            max_suffix = max(max_suffix, int(p))
                    n = max_suffix + 1
                    cand = f"{base}.{n:03d}" if base else f"{n:03d}"
                    while cand in used or n in (404, 555):
                        n += 1
                        cand = f"{base}.{n:03d}" if base else f"{n:03d}"
                    r["path"] = cand
                    r["level"] = len(cand.split(".")) - 1
                    used.add(cand)
                    claimed.add(cand)

                    # Order key assignment
                    if not base:
                        # Root concept (parent is None): use Fractional / Lexicographic Ordering!
                        prev_ok: Optional[str] = None
                        for s_r in children[:idx]:
                            if s_r.get("order_key") and not _is_555(s_r):
                                prev_ok = str(s_r["order_key"])
                        next_ok: Optional[str] = None
                        for s_r in children[idx + 1:]:
                            if s_r.get("order_key") and not _is_555(s_r):
                                next_ok = str(s_r["order_key"])
                                break
                            elif identity_order_keys:
                                cand_ok = _lookup_identity(identity_order_keys, s_r.get("concept"), None, False)
                                if cand_ok:
                                    next_ok = str(cand_ok)
                                    break
                        ok = _fractional_order_key(prev_ok, next_ok)
                        while ok in local_order:
                            ok = _fractional_order_key(ok, next_ok)
                        r["order_key"] = ok
                        local_order.add(ok)
                    else:
                        # Child concept (parent is not None): use same next incremental as existing
                        if local_order:
                            max_idx = max((_order_key_index(ok) for ok in local_order if _order_key_index(ok) >= 0), default=-1)
                            next_idx = max_idx + 1
                        else:
                            next_idx = 0
                        ok = _order_key(next_idx)
                        while ok in local_order:
                            next_idx += 1
                            ok = _order_key(next_idx)
                        r["order_key"] = ok
                        local_order.add(ok)

            path_by_concept.setdefault(r["concept"], str(r["path"]))
            path_by_parent_and_concept[(r.get("parent"), r["concept"])] = str(r["path"])
            if r["concept"] not in ancestors:
                _walk(r["concept"], ancestors | {r["concept"]})

    _walk(None)
    # rows whose parent was never proposed/stored become roots
    used_root_order_indices = {
        _order_key_index(str(r["order_key"]))
        for r in norm_rows
        if r.get("order_key") and _order_key_index(str(r["order_key"])) >= 0
    }
    if identity_order_keys:
        used_root_order_indices.update(
            _order_key_index(str(ok)) for ok in identity_order_keys.values() if _order_key_index(str(ok)) >= 0
        )
    for r in norm_rows:
        if not r.get("path"):
            max_root_suffix = 0
            for p in used:
                if "." not in p and p.isdigit():
                    max_root_suffix = max(max_root_suffix, int(p))
            n = max_root_suffix + 1
            cand = f"{n:03d}"
            while cand in used:
                n += 1
                cand = f"{n:03d}"
            r["path"] = cand
            # incremental order key for root
            max_root_idx = max(used_root_order_indices, default=-1)
            next_idx = max_root_idx + 1
            ok = _order_key(next_idx)
            used_root_order_indices.add(next_idx)
            r["order_key"] = ok
            r["level"] = 0
            used.add(cand)
            claimed.add(cand)
            path_by_concept[r["concept"]] = r["path"]
            path_by_parent_and_concept[(r.get("parent"), r["concept"])] = r["path"]

    # supplementary bucket order keys (distinct, deterministic order)
    supp = [r for r in norm_rows if str(r.get("path") or "") == "555"]
    for i, r in enumerate(supp):
        r["order_key"] = _order_key(i)

    def _dim_base(d: dict) -> str:
        # The extracted ``parent_concept`` is authoritative for WHICH line item a
        # member hangs under. Try specific (parent_concept, header) path first.
        pc = d.get("parent_concept")
        hdr = d.get("parent_header")
        if pc and hdr and (pc, hdr) in path_by_parent_and_concept:
            return path_by_parent_and_concept[(pc, hdr)]
        pc_path = path_by_concept.get(pc) if pc else None
        hdr_path = path_by_concept.get(hdr) if hdr else None
        if hdr_path and pc_path:
            # accept the header only if it is inside the parent's subtree
            if hdr_path == pc_path or hdr_path.startswith(str(pc_path) + "."):
                return hdr_path
            return pc_path
        if hdr_path:
            return hdr_path
        if pc_path:
            return pc_path
        if pc and pc in stored_paths:
            return stored_paths[pc]
        return ""

    dim_seq = {id(d): idx for idx, d in enumerate(norm_dims)}
    sorted_dims = sorted(
        norm_dims,
        key=lambda d: (_dim_base(d), _pos(d, dim_seq.get(id(d), 0)), dim_seq.get(id(d), 0)),
    )
    for d in sorted_dims:
        base = _dim_base(d)
        stored_dim = _lookup_identity(
            identity_paths, d.get("concept"), d.get("parent_concept"), True, d.get("parent_header")
        )
        if (
            stored_dim
            and stored_dim != "555"
            and stored_dim not in claimed
            and (not base or str(stored_dim).startswith(f"{base}."))
        ):
            cand = str(stored_dim)
            d["path"] = cand
            stored_dim_ok = _lookup_identity(
                identity_order_keys, d.get("concept"), d.get("parent_concept"), True, d.get("parent_header")
            )
            if stored_dim_ok and str(stored_dim_ok):
                d["order_key"] = str(stored_dim_ok)
            else:
                d["order_key"] = d.get("order_key") or "a"
            d["level"] = len(cand.split(".")) - 1
            used.add(cand)
            claimed.add(cand)
        else:
            # New dimensional concept: next incremental path & order key under base
            max_suffix = 0
            for p in used:
                if base and p.startswith(f"{base}."):
                    suffix_str = p[len(base) + 1:]
                    if "." not in suffix_str and suffix_str.isdigit():
                        max_suffix = max(max_suffix, int(suffix_str))
                elif not base and "." not in p and p.isdigit():
                    max_suffix = max(max_suffix, int(p))
            n = max_suffix + 1
            cand = f"{base}.{n:03d}" if base else f"{n:03d}"
            while cand in used or n in (404, 555):
                n += 1
                cand = f"{base}.{n:03d}" if base else f"{n:03d}"
            d["path"] = cand
            d["level"] = len(cand.split(".")) - 1
            used.add(cand)
            claimed.add(cand)

            # Sibling order keys for dims under base
            local_dim_order = set()
            for sd in sorted_dims:
                if _dim_base(sd) == base:
                    s_ok = _lookup_identity(identity_order_keys, sd.get("concept"), sd.get("parent_concept"), True, sd.get("parent_header"))
                    if s_ok:
                        local_dim_order.add(str(s_ok))
                    elif sd.get("order_key"):
                        local_dim_order.add(str(sd.get("order_key")))
            if local_dim_order:
                max_ok_idx = max((_order_key_index(ok) for ok in local_dim_order if _order_key_index(ok) >= 0), default=-1)
                next_ok_idx = max_ok_idx + 1
            else:
                next_ok_idx = 0
            new_ok = _order_key(next_ok_idx)
            while new_ok in local_dim_order:
                next_ok_idx += 1
                new_ok = _order_key(next_ok_idx)
            d["order_key"] = new_ok
            local_dim_order.add(new_ok)

    return norm_rows, sorted_dims


def _stored_rows_for(norm_service: Any, bundle: Any) -> tuple[list[dict], dict[str, str], set, dict]:
    """Fetch all stored rows (main + dims) for this cik+statement.

    Returns ``(stored, path_by_concept, occupied_paths, identity_paths,
    identity_order_keys)``.  Rows are returned in stored tree order (siblings
    by ``order_key``) so the agent and the encoder never fall back to Mongo's
    arbitrary natural order.
    """
    resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(resolver):
        return [], {}, set(), {}, {}
    repo: Any = resolver(getattr(bundle, "form_type", None))
    from normalization.src.data_normalization_service.database.database import is_polluted_path, _POLLUTED_PATH_RX
    try:
        main = [
            r for r in repo.collection.find({
                "cik": getattr(bundle, "company_cik", None),
                "statement_type": getattr(bundle, "statement_type", None),
                "dimension_concept": False,
                "path": {"$not": _POLLUTED_PATH_RX},
            }) if not is_polluted_path(r.get("path"))
        ]
    except Exception:
        main = []
    try:
        dims = [
            r for r in repo.collection.find({
                "cik": getattr(bundle, "company_cik", None),
                "statement_type": getattr(bundle, "statement_type", None),
                "dimension_concept": True,
                "path": {"$not": _POLLUTED_PATH_RX},
            }) if not is_polluted_path(r.get("path"))
        ]
    except Exception:
        dims = []
    def _tree_key(s: dict) -> tuple:
        p = str(s.get("path") or "")
        parent = p.rsplit(".", 1)[0] if "." in p else ""
        return (parent, str(s.get("order_key") or ""), p)

    main.sort(key=_tree_key)
    dims.sort(key=_tree_key)
    main_name_by_id = {row.get('_id'): row.get('concept') for row in main if row.get('_id')}
    for dim in dims:
        if not dim.get('parent_concept'):
            # Legacy dimensional rows sometimes lack parent_concept and retain
            # a parent ObjectId that was later promoted/deleted. The fact's
            # concept_name is the semantic parent tag; restore it for hierarchy
            # matching so an unchanged member (e.g. country:US) is not treated
            # as a new child merely because its old parent ID drifted.
            dim['parent_concept'] = (
                dim.get('concept_name') or main_name_by_id.get(dim.get('concept_id'))
            )
    stored = main + dims

    # Concept rows do not carry their most recent observed period. Fetch it from
    # the matching annual/quarterly value collection so alias promotions are
    # decided by financial period, not insertion/processing time.
    value_repo_resolver = getattr(norm_service, "_get_value_repo_by_form_type", None)
    if callable(value_repo_resolver):
        concept_ids = [s.get("_id") for s in main if s.get("_id") is not None]
        if concept_ids:
            try:
                value_repo = value_repo_resolver(getattr(bundle, "form_type", None))
                values = value_repo.collection.find(
                    {
                        "cik": getattr(bundle, "company_cik", None),
                        "concept_id": {"$in": concept_ids},
                        "dimension_value": {"$ne": True},
                    },
                    {"concept_id": 1, "reporting_period": 1},
                )
                latest_by_id: dict[Any, tuple[tuple[int, int, int], str]] = {}
                for value_doc in values:
                    rank, label = _period_info(value_doc.get("reporting_period") or {})
                    concept_id = value_doc.get("concept_id")
                    if rank is not None and concept_id is not None and (
                        concept_id not in latest_by_id or rank > latest_by_id[concept_id][0]
                    ):
                        latest_by_id[concept_id] = (rank, label or "")
                for row in main:
                    latest = latest_by_id.get(row.get("_id"))
                    if latest:
                        row["_latest_period_rank"], row["latest_period"] = latest
            except Exception as exc:  # noqa: BLE001 — period metadata is advisory
                logger.debug("Could not load latest concept periods: %s", exc)

    path_by_concept: dict[str, str] = {}
    for s in main:
        if s.get("concept") and s.get("path"):
            path_by_concept.setdefault(s["concept"], str(s["path"]))
    occupied = {
        str(s["path"]) for s in stored
        if s.get("path") and str(s["path"]) != "555"
    }
    identity_paths: dict[tuple, str] = {}
    identity_order_keys: dict[tuple, str] = {}
    for s in stored:
        if not s.get("concept") or not s.get("path"):
            continue
        # Include parent_header in key so dim members routed through a custom grouping
        # header (e.g. custom:ProductSegmentation) are stored separately from the same
        # member placed directly under the parent concept.
        key = (
            s["concept"],
            s.get("parent_concept"),
            bool(s.get("dimension_concept")),
            s.get("parent_header"),  # None for non-header dims
        )
        identity_paths.setdefault(key, str(s["path"]))
        if s.get("order_key") is not None:
            identity_order_keys.setdefault(key, str(s["order_key"]))

        # Also register aliases so incoming concepts matching an alias find stored placement
        if s.get("aliases") and isinstance(s.get("aliases"), list):
            for alias in s["aliases"]:
                if alias and alias != s["concept"]:
                    alias_key = (
                        alias,
                        s.get("parent_concept"),
                        bool(s.get("dimension_concept")),
                        s.get("parent_header"),
                    )
                    identity_paths.setdefault(alias_key, str(s["path"]))
                    if s.get("order_key") is not None:
                        identity_order_keys.setdefault(alias_key, str(s["order_key"]))
    return stored, path_by_concept, occupied, identity_paths, identity_order_keys


def _filing_rows_for(bundle: Any) -> list[dict]:
    concrete = list(getattr(bundle, "concepts", None) or [])
    abstracts = list(getattr(bundle, "abstract_concepts", None) or [])

    # path -> concept, so each row's filing parent is visible to the model
    path_to_concept: dict[str, str] = {}
    for item in concrete + abstracts:
        if isinstance(item, dict) and item.get("path"):
            path_to_concept[str(item["path"])] = item.get("concept", "")

    def _parent_of(item: dict) -> Optional[str]:
        path = str(item.get("path") or "")
        if "." not in path:
            return None
        return path_to_concept.get(path.rsplit(".", 1)[0])

    incoming_periods: dict[str, list[Any]] = defaultdict(list)
    for value in getattr(bundle, "values", None) or []:
        if isinstance(value, dict) and value.get("concept") and value.get("period_key"):
            incoming_periods[str(value["concept"])].append(value["period_key"])
    # Use the bundle's reporting period only when no concept-specific fact
    # period is available for that concept.
    incoming_period_info = {
        concept: _latest_period(periods)
        for concept, periods in incoming_periods.items()
    }
    fallback_period = getattr(bundle, "reporting_period", None)

    rows = []
    for item in concrete + abstracts:
        if isinstance(item, dict) and item.get("concept"):
            row = {
                "concept": item["concept"],
                "label": item.get("label"),
                "abstract": bool(item.get("abstract")),
                "value": item.get("value"),
                "path": item.get("path"),            # filing reference only
                "order_key": item.get("order_key"),
                "parent_concept": _parent_of(item),
                "dimension_concept": False,
            }
            period = incoming_period_info.get(str(item["concept"]))
            if period is None or period[0] is None:
                period = _latest_period([fallback_period])
            if period[0] is not None:
                row["_latest_period_rank"], row["latest_period"] = period
            rows.append(row)
    for dim in getattr(bundle, "dimensional_concepts", None) or []:
        if isinstance(dim, dict) and dim.get("concept"):
            rows.append({
                "concept": dim["concept"],
                "label": dim.get("label"),
                "parent_concept": dim.get("parent_concept"),
                "segment_type": dim.get("segment_type"),
                "dimension_concept": True,
            })
    return rows


_CONCEPT_STOP_TOKENS = frozenset({
    "net", "total", "other", "and", "of", "in", "to", "for", "from", "with",
    "by", "on", "at", "the", "a", "an", "current", "noncurrent", "operating",
    "nonoperating", "member", "axis", "domain", "line", "items", "table",
    "abstract",
})


def _concept_tokens(concept: Optional[str], label: Optional[str] = None) -> set[str]:
    """Extract normalized semantic tokens from a concept local name and label."""
    raw = str(concept or "").split(":")[-1]
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", raw)
    if label:
        parts.extend(re.findall(r"[A-Za-z]+|\d+", str(label)))
    return {
        p.lower() for p in parts
        if len(p) > 2 and p.lower() not in _CONCEPT_STOP_TOKENS
    }


def _infer_dim_parent_header(
    parent_concept: str,
    segment_type: Optional[str],
    stored: list[dict],
) -> Optional[str]:
    """Reuse an existing grouping header under ``parent_concept`` or infer the canonical custom header."""
    # 1. Check existing stored dims under the same parent_concept
    parent_dims = [
        s for s in stored
        if s.get("dimension_concept") and s.get("parent_concept") == parent_concept
    ]
    if segment_type:
        for s in parent_dims:
            if s.get("segment_type") == segment_type and s.get("parent_header"):
                return str(s["parent_header"])

    # 2. Check existing custom headers stored under parent_concept
    parent_headers = [
        str(s["concept"]) for s in stored
        if not s.get("dimension_concept")
        and str(s.get("concept") or "").startswith("custom:")
        and s.get("parent_concept") == parent_concept
    ]
    seg_low = (segment_type or "").strip().lower()
    if seg_low:
        if any(k in seg_low for k in ("prod", "serv")):
            for h in parent_headers:
                if "prod" in h.lower() or "serv" in h.lower():
                    return h
            return "custom:ProductSegmentation"
        if any(k in seg_low for k in ("geo", "country", "region")):
            for h in parent_headers:
                if "geo" in h.lower() or "region" in h.lower() or "country" in h.lower():
                    return h
            return "custom:GeographicSegmentation"
        if any(k in seg_low for k in ("seg", "bus", "oper")):
            for h in parent_headers:
                if "seg" in h.lower() or "bus" in h.lower():
                    return h
            return "custom:BusinessSegments"

    if len(parent_headers) == 1:
        return parent_headers[0]
    for s in parent_dims:
        if s.get("parent_header"):
            return str(s["parent_header"])
    return None


def _try_programmatic_incremental_hierarchy(
    stored: list[dict],
    filing_rows: list[dict],
    new_concepts: set[str],
    tools_by_name: dict[str, Any],
    proposal: dict,
) -> bool:
    """Attempt deterministic incremental hierarchy placement without calling the LLM.

    Returns ``True`` only when all new concepts (dimensional members and/or
    unambiguous child line items with known stored parents and no alias/rename
    candidates) are placed and pass ``check_hierarchy`` with zero warnings.
    Otherwise resets ``proposal`` and returns ``False`` so the LLM agent takes over.
    """
    if not stored or not new_concepts:
        return False
    if "propose_hierarchy" not in tools_by_name or "check_hierarchy" not in tools_by_name:
        return False

    import json as _json

    stored_main = {
        str(s["concept"]): s
        for s in stored
        if not s.get("dimension_concept")
        and s.get("concept")
        and not str(s["concept"]).startswith("custom:")
    }
    filing_main_names = {
        str(r["concept"])
        for r in filing_rows
        if not r.get("dimension_concept") and r.get("concept")
    }
    new_main = [
        r for r in filing_rows
        if not r.get("dimension_concept") and r.get("concept") in new_concepts
    ]
    new_dims = [
        r for r in filing_rows
        if r.get("dimension_concept") and r.get("concept") in new_concepts
    ]

    # 1. Validate new main concepts (if any): must have known stored parent and no alias candidate
    stored_absent = [
        s for c, s in stored_main.items()
        if c not in filing_main_names and not s.get("abstract")
    ]
    auto_rows: list[dict] = []
    sibling_counters: dict[str, int] = defaultdict(int)
    for s in stored:
        if not s.get("dimension_concept") and s.get("parent_concept"):
            sibling_counters[str(s["parent_concept"])] += 1

    for r in new_main:
        concept = str(r["concept"])
        parent = r.get("parent_concept")
        # Require a known stored parent (never guess root-level placement or missing parents)
        if not parent or str(parent) not in stored_main:
            return False
        parent_str = str(parent)
        # Check for potential alias/rename against absent stored concepts
        r_tokens = _concept_tokens(concept, r.get("label"))
        for abs_row in stored_absent:
            abs_tokens = _concept_tokens(abs_row.get("concept"), abs_row.get("label"))
            overlap = r_tokens & abs_tokens
            if abs_row.get("parent_concept") == parent_str and overlap:
                return False
            if len(overlap) >= 2:
                return False

        sibling_counters[parent_str] += 1
        auto_rows.append({
            "concept": concept,
            "parent": parent_str,
            "abstract": bool(r.get("abstract")),
            "label": r.get("label"),
            "order": sibling_counters[parent_str],
        })

    # 2. Validate and build new dimensional members
    known_parents = set(stored_main.keys()) | {r["concept"] for r in auto_rows}
    dim_counters: dict[tuple[str, Optional[str]], int] = defaultdict(int)
    for s in stored:
        if s.get("dimension_concept") and s.get("parent_concept"):
            dim_counters[(str(s["parent_concept"]), s.get("parent_header"))] += 1

    auto_dims: list[dict] = []
    for d in new_dims:
        concept = str(d["concept"])
        pc = d.get("parent_concept")
        if not pc or str(pc) not in known_parents:
            return False
        pc_str = str(pc)
        ph = _infer_dim_parent_header(pc_str, d.get("segment_type"), stored)
        dim_counters[(pc_str, ph)] += 1
        dim_entry: dict[str, Any] = {
            "concept": concept,
            "parent_concept": pc_str,
            "order": dim_counters[(pc_str, ph)],
        }
        if ph:
            dim_entry["parent_header"] = ph
        if d.get("segment_type"):
            dim_entry["segment_type"] = d["segment_type"]
        if d.get("label"):
            dim_entry["label"] = d["label"]
        auto_dims.append(dim_entry)

    try:
        prop_res = tools_by_name["propose_hierarchy"].invoke({
            "rows_json": _json.dumps(auto_rows),
            "dims_json": _json.dumps(auto_dims),
        })
        if not isinstance(prop_res, str) or not prop_res.startswith("OK"):
            proposal.clear()
            proposal.update({"merges": {}, "rows": [], "dims": []})
            return False
        preview = proposal.get("_preview") or {}
        p_rows = preview.get("rows") or []
        p_dims = preview.get("dims") or []
        if not p_rows:
            proposal.clear()
            proposal.update({"merges": {}, "rows": [], "dims": []})
            return False

        # Verify every newly added row/dim materialized cleanly with no path collisions
        path_counts: dict[str, int] = defaultdict(int)
        for item in list(p_rows) + list(p_dims):
            p = str(item.get("path") or "")
            if p and p != "555":
                path_counts[p] += 1
        row_by_c = {r.get("concept"): r for r in p_rows if r.get("concept")}
        dim_by_c = {
            (d.get("parent_concept"), d.get("concept")): d
            for d in p_dims if d.get("concept")
        }
        all_placed_cleanly = True
        for r in auto_rows:
            placed = row_by_c.get(r["concept"])
            p = str((placed or {}).get("path") or "")
            if not p or p == "555" or path_counts.get(p, 0) > 1:
                all_placed_cleanly = False
                break
        if all_placed_cleanly:
            for d in auto_dims:
                placed = dim_by_c.get((d["parent_concept"], d["concept"]))
                p = str((placed or {}).get("path") or "")
                if not p or p == "555" or path_counts.get(p, 0) > 1:
                    all_placed_cleanly = False
                    break
        if all_placed_cleanly:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("programmatic incremental hierarchy check failed: %s", exc)

    proposal.clear()
    proposal.update({"merges": {}, "rows": [], "dims": []})
    return False



def make_hierarchy_agent_node(
    norm_service: Any,
    *,
    chat_llm: Any = None,
    on_event: Any = None,
) -> Callable[[dict], dict]:
    """Build the unified hierarchy agent node (single loop, full ownership)."""

    def hierarchy_agent_node(state: dict) -> dict:
        bundles = state.get("bundles") or []
        reasoned_about = 0
        total_updates = 0
        seeded: list[str] = []
        all_promotions: list[dict] = []

        for bundle in bundles:
            stmt_t0 = time.perf_counter()
            stmt_type = getattr(bundle, "statement_type", "?")
            stored, stored_paths, occupied_paths, identity_paths, identity_order_keys = _stored_rows_for(norm_service, bundle)
            filing_rows = _filing_rows_for(bundle)
            if not filing_rows and not stored:
                continue
            if not stored:
                seeded.append(stmt_type)

            # ── Fast-path: skip LLM when all filing concepts are already stored ─────
            # Compute the diff deterministically. If there are no new concepts,
            # stored paths are authoritative and we bypass the LLM entirely.
            proposal: dict = {"merges": {}, "rows": [], "dims": []}
            stored_names = set()
            for r in stored:
                c = r.get("concept")
                if c:
                    stored_names.add(c)
                    if "_" in c:
                        stored_names.add(c.split("_")[-1])
                for al in r.get("aliases") or []:
                    if al:
                        stored_names.add(al)
            filing_names = {r.get("concept") for r in filing_rows if r.get("concept")}
            new_concepts = filing_names - stored_names
            if stored and not new_concepts:
                # All concepts are known — reuse the stored hierarchy verbatim.
                # No LLM and no encoding: the stored rows already carry their
                # agent-approved path/order_key/parent, so reuse them as-is.
                stmt_dur = time.perf_counter() - stmt_t0
                report_call(
                    f"  [hierarchy]  • {stmt_type}: ⚡ fast-path (0 new concepts — reusing stored hierarchy)  {stmt_dur:.2f}s"
                )
                report_detail(f"{stmt_type}: fast-path")
                proposal["_preview"] = {
                    "rows": [
                        {
                            "concept": s["concept"],
                            "parent": s.get("parent_concept"),
                            "label": s.get("label"),
                            "abstract": s.get("abstract", False),
                            "path": s.get("path"),
                            "order_key": s.get("order_key"),
                            "level": s.get("level", s.get("hierarchy_level", 0)),
                        }
                        for s in stored
                        if not s.get("dimension_concept") and s.get("concept") and s.get("path")
                    ],
                    "dims": [
                        {
                            "concept": s["concept"],
                            "parent_concept": s.get("parent_concept"),
                            "label": s.get("label"),
                            "segment_type": s.get("segment_type"),
                            "parent_header": s.get("parent_header"),
                            "path": s.get("path"),
                            "order_key": s.get("order_key"),
                        }
                        for s in stored
                        if s.get("dimension_concept") and s.get("concept") and s.get("path")
                    ],
                }
            else:
                tools = build_unified_hierarchy_tools(
                    stored,
                    filing_rows,
                    proposal,
                    statement_type=getattr(bundle, "statement_type", ""),
                    form_type=getattr(bundle, "form_type", ""),
                    company_cik=getattr(bundle, "company_cik", ""),
                    stored_paths=stored_paths,
                    occupied_paths=occupied_paths,
                    identity_paths=identity_paths,
                    identity_order_keys=identity_order_keys,
                    materialize=_materialize,
                )
                tools_by_name = {t.name: t for t in tools}

                # Programmatic incremental fast-path: resolve unambiguous new dims /
                # child line items without calling the LLM.
                resolved_programmatically = (
                    bool(stored)
                    and _try_programmatic_incremental_hierarchy(
                        stored, filing_rows, new_concepts, tools_by_name, proposal
                    )
                )
                if resolved_programmatically:
                    stmt_dur = time.perf_counter() - stmt_t0
                    report_call(
                        f"  [hierarchy]  • {stmt_type}: ⚡ programmatic incremental fast-path "
                        f"({len(new_concepts)} new concept(s) placed cleanly)  {stmt_dur:.2f}s"
                    )
                    report_detail(f"{stmt_type}: programmatic incremental")
                else:
                    # Agent takeover: fresh seed or ambiguous incremental concepts.
                    if not stored:
                        report_call(
                            f"  [hierarchy]  • {stmt_type}: fresh seed (0 stored) → running hierarchy agent"
                        )
                    else:
                        report_call(
                            f"  [hierarchy]  • {stmt_type}: {len(new_concepts)} new concept(s) detected ({len(stored)} stored) → running hierarchy agent"
                        )
                    report_detail(f"{stmt_type}: running agent...")

                    if not stored:
                        context_dump = (
                            tools_by_name["query_filing_hierarchy"].invoke({})
                            if "query_filing_hierarchy" in tools_by_name
                            else ""
                        )
                        task = (
                            f"FRESH SEED for {getattr(bundle, 'statement_type', '?')} "
                            f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                            f"No existing hierarchy is stored in DB.\n\n"
                            f"{context_dump}\n\n"
                            f"Predict ONCE: call propose_hierarchy(rows_json, dims_json) using the universal statement blueprint, "
                            f"and immediately call finalize_hierarchy({{\"status\": \"done\"}}). Do NOT loop or call preview_hierarchy/check_hierarchy."
                        )
                    else:
                        context_dump = (
                            tools_by_name["query_hierarchy_diff"].invoke({})
                            if "query_hierarchy_diff" in tools_by_name
                            else ""
                        )
                        task = (
                            f"INCREMENTAL UPDATE for {getattr(bundle, 'statement_type', '?')} "
                            f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                            f"Stored rows: {len(stored)}, New incoming concepts: {len(new_concepts)}. "
                            f"The existing stored hierarchy in DB is verified ground truth — DO NOT redefine it completely.\n\n"
                            f"{context_dump}\n\n"
                            f"Predict ONCE:\n"
                            f"1. Check the 'High-Probability Alias Candidates' above: if any new concept is an alias/synonym of a stored concept (representing the same economic line item), call decide_mapping({{\"concept\": \"<incoming>\", \"same_as\": \"<stored>\"}});\n"
                            f"2. For genuinely new concepts to insert, call propose_hierarchy(rows_json, dims_json) passing ONLY the new concept(s) "
                            f"with their parent and sibling position (all stored concepts and their order keys are preserved automatically);\n"
                            f"3. Immediately call finalize_hierarchy({{\"status\": \"done\"}}). Do NOT loop or call preview_hierarchy/check_hierarchy."
                        )
                    try:
                        _ = run_agent_loop(
                            AGENTIC_HIERARCHY_SYSTEM_PROMPT,
                            task,
                            tools,
                            ticker=str(state.get("ticker") or state.get("cik") or "?"),
                            finalize_name="finalize_hierarchy",
                            finalize_description="Call when the full hierarchy is decided. Pass {\"status\": \"done\"}.",
                            chat_llm=chat_llm,
                            max_steps=3,
                            on_event=on_event,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("hierarchy agent failed for %s/%s: %s",
                                       bundle.company_cik, bundle.statement_type, exc)

            # The agent's tools (propose_hierarchy / preview_hierarchy) already
            # materialized the final tree and the agent checked it.  Use that
            # frozen payload verbatim — no deterministic encoding after the loop.
            preview = proposal.get("_preview")
            if not preview or not preview.get("rows"):
                report_call(
                    f"  [hierarchy]  ! {bundle.statement_type}: no agent-checked tree — skipping (nothing written)"
                )
                logger.error(
                    "hierarchy agent produced no checked tree for %s/%s; skipping",
                    bundle.company_cik, bundle.statement_type,
                )
                continue
            rows, dims = preview["rows"], preview["dims"]

            # (parent_concept, concept) -> (path, order, parent_header) for dims
            # Key by (parent_concept, concept) since the incoming filing bundle dims
            # only have this identity. The agent's proposal provides the parent_header.
            dim_plan = {
                (d.get("parent_concept"), d.get("concept")): (
                    d.get("path"), d.get("order_key"), d.get("parent_header")
                )
                for d in dims if d.get("concept") and d.get("path")
            }

            concrete = list(getattr(bundle, "concepts", None) or [])
            abstracts = getattr(bundle, "abstract_concepts", None)
            if abstracts is None:
                abstracts = []
                bundle.abstract_concepts = abstracts
            plan_by_concept = {(r.get("parent"), r.get("concept")): r for r in rows if r.get("concept")}
            stored_merge_targets = {
                concept: merge.get("same_as")
                for concept, merge in (proposal.get("merges") or {}).items()
                if isinstance(merge, dict) and merge.get("same_as")
                and merge.get("keep_tag") != "incoming"
            }
            for item in list(concrete) + list(abstracts) + list(getattr(bundle, "dimensional_concepts", None) or []):
                if isinstance(item, dict) and item.get("concept") in stored_merge_targets:
                    item["_concept_target"] = stored_merge_targets[item["concept"]]
            # Grouping headers repeat by name across parents, so they must be
            # resolved by (concept, parent) instead of name alone.
            plan_header_by_parent = {
                (r.get("concept"), r.get("parent")): r
                for r in rows if r.get("concept") and str(r["concept"]).startswith("custom:")
            }

            # Index stored concepts by concept name and header parent
            stored_by_concept: dict[str, dict] = {}
            stored_header_by_parent: dict[tuple, dict] = {}
            for s in stored:
                if not s.get("concept") or s.get("dimension_concept"):
                    continue
                if str(s["concept"]).startswith("custom:"):
                    stored_header_by_parent[(s["concept"], s.get("parent_concept"))] = s
                else:
                    stored_by_concept[s["concept"]] = s

            for item in list(concrete) + list(abstracts):
                if not isinstance(item, dict) or not item.get("concept"):
                    continue
                # If this item was already stored in DB, strictly retain its stored path and order_key
                stored_entry = stored_by_concept.get(str(item["concept"]))
                if stored_entry and stored_entry.get("path"):
                    item["path"] = stored_entry["path"]
                    item["order_key"] = stored_entry.get("order_key")
                    item["hierarchy_level"] = stored_entry.get("hierarchy_level", stored_entry.get("level", 0))
                    item["level"] = item["hierarchy_level"]
                    item["parent_concept"] = stored_entry.get("parent_concept")
                    item["_hierarchy_resolved"] = True
                    item["hierarchy_source"] = "stored"
                    continue

                if str(item["concept"]).startswith("custom:"):
                    planned = plan_header_by_parent.get((item["concept"], item.get("parent_concept")))
                else:
                    target = item.get("_concept_target") or item["concept"]
                    planned = plan_by_concept.get((item.get("parent_concept"), target))
                    if not planned:
                        # Fallback: search plan_by_concept for ANY entry matching the target concept
                        for (p, c), e in plan_by_concept.items():
                            if c == target:
                                planned = e
                                break
                
                if planned and planned.get("path"):
                    item["path"] = planned["path"]
                    item["order_key"] = planned["order_key"]
                    item["hierarchy_level"] = planned.get("level", 0)
                    item["level"] = planned.get("level", 0)
                    item["parent_concept"] = planned.get("parent")
                    item["_hierarchy_resolved"] = True
                    item["hierarchy_source"] = "agent"
                else:
                    # Clear original linkbase paths if the concept was dropped by the agent
                    item["path"] = None
                    item["order_key"] = None

            # Persist any NEW custom grouping headers the agent proposed (the
            # executor adds rows the model chose; it invents nothing).
            import re as _re
            concrete_concepts_set = {i.get("concept") for i in list(concrete) if isinstance(i, dict) and i.get("concept")}
            existing_abstract_keys = {
                (i.get("parent_concept"), i.get("concept"), str(i.get("path") or ""))
                for i in list(abstracts) if isinstance(i, dict)
            }
            for r in rows:
                c = r.get("concept")
                if not c or c in concrete_concepts_set:
                    continue
                path = str(r.get("path") or "")
                parent = r.get("parent")
                key = (parent, c, path)
                if key in existing_abstract_keys:
                    continue
                existing_abstract_keys.add(key)
                label = r.get("label") or _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(c).split(":")[-1])
                abstracts.append({
                    "concept": c,
                    "label": label,
                    "value": None,
                    "period": None,
                    "abstract": True,
                    "parent_concept": parent,
                    "path": r.get("path"),
                    "order_key": r.get("order_key"),
                    "hierarchy_level": r.get("level", 0),
                    "level": r.get("level", 0),
                    "_hierarchy_resolved": True,
                    "hierarchy_source": "agent",
                })

            for dim in getattr(bundle, "dimensional_concepts", None) or []:
                if not isinstance(dim, dict):
                    continue

                # If this dimensional concept was already stored in DB, strictly retain its stored values
                stored_dim_path = _lookup_identity(
                    identity_paths, dim.get("concept"), dim.get("parent_concept"), True, dim.get("parent_header")
                )
                if stored_dim_path:
                    dim["path"] = stored_dim_path
                    dim["order_key"] = _lookup_identity(
                        identity_order_keys, dim.get("concept"), dim.get("parent_concept"), True, dim.get("parent_header")
                    )
                    dim["hierarchy_level"] = len(str(stored_dim_path).split(".")) - 1
                    dim["level"] = dim["hierarchy_level"]
                    dim["_hierarchy_resolved"] = True
                    dim["hierarchy_source"] = "stored"
                    continue
                
                target = dim.get("_concept_target") or dim.get("concept")
                entry = dim_plan.get((dim.get("parent_concept"), target))
                if not entry:
                    # Fallback: search dim_plan for ANY entry matching the target concept
                    for (p, c), e in dim_plan.items():
                        if c == target:
                            entry = e
                            break
                if entry:
                    dim["path"], dim["order_key"], dim["parent_header"] = entry
                    dim["hierarchy_level"] = len(str(dim["path"]).split(".")) - 1
                    dim["level"] = dim["hierarchy_level"]
                    dim["_hierarchy_resolved"] = True
                else:
                    dim["path"] = None
                    dim["order_key"] = None

            # merges -> attach (stored name wins) or promotion (incoming name wins).
            promotions: list[dict] = []
            for concept, m in (proposal.get("merges") or {}).items():
                if not isinstance(m, dict):
                    continue
                same_as = m.get("same_as")
                if not same_as:
                    continue
                if m.get("keep_tag") == "incoming":
                    # The loser's dimensional children must follow the surviving
                    # row.  Use the paths the AGENT placed (via move_row +
                    # preview); persist never computes them.
                    preview_dims = (proposal.get("_preview") or {}).get("dims") or []
                    child_plan: list[dict] = []
                    for ch in (m.get("children") or []):
                        child_name = ch.get("concept")
                        placed = next(
                            (
                                d for d in preview_dims
                                if d.get("concept") == child_name
                                and (d.get("parent_concept") or d.get("parent")) == concept
                            ),
                            None,
                        )
                        child_plan.append({
                            "concept": child_name,
                            "path": (placed or {}).get("path"),
                            "order_key": (placed or {}).get("order_key"),
                            "parent_header": (placed or {}).get("parent_header"),
                        })
                    promotions.append({
                        "cik": getattr(bundle, "company_cik", None),
                        "statement_type": getattr(bundle, "statement_type", None),
                        "form_type": getattr(bundle, "form_type", None),
                        "from_concept": same_as,
                        "to_concept": concept,
                        "children": child_plan,
                    })
                else:
                    all_items = list(concrete) + list(abstracts) + list(getattr(bundle, "dimensional_concepts", None) or [])
                    for item in all_items:
                        if isinstance(item, dict) and item.get("concept") == concept:
                            item["_concept_target"] = same_as

            # Invariant: If a stock already exists in DB (stored is not empty),
            # NEVER make any changes to the existing hierarchy in the DB!
            # Only newly inserted concepts receive paths/order keys; existing concepts are immutable.
            existing_updates = []
            if not stored:
                # Fresh seed — no existing stored rows to update
                pass
            else:
                # Existing stock: strictly zero updates to existing stored hierarchy
                existing_updates = []
            total_updates += len(existing_updates)
            all_promotions.extend(promotions)
            reasoned_about += 1
            bundle._agent_updates = existing_updates
            if not (stored and not new_concepts):
                stmt_dur = time.perf_counter() - stmt_t0
                report_call(
                    f"  [hierarchy]  ✓ {stmt_type} hierarchy resolved in {stmt_dur:.1f}s ({len(rows)} rows, {len(existing_updates)} re-pathed)"
                )
                report_detail(f"{stmt_type}: done")

        plan = {
            "decided_by": "agent",
            "seeded_statement_types": seeded,
            "existing_updates": [
                u for b in bundles for u in getattr(b, "_agent_updates", [])
            ],
            "resolved_concepts": sum(
                len([i for i in list(getattr(b, "concepts", None) or []) + list(getattr(b, "abstract_concepts", None) or []) if isinstance(i, dict) and i.get("path")])
                for b in bundles
            ),
        }
        report_call(
            f"  [hierarchy]  ✓ {reasoned_about} statement(s) processed ({total_updates} stored row update(s))"
        )
        return {**state, "hierarchy_plan": plan, "concept_promotions": all_promotions, "status": "hierarchy_agent_done"}

    def _resilient_node(state: dict) -> dict:
        """A hierarchy defect must never block the write.

        Any unexpected failure (stored-tree read, tool build, materialisation,
        agent loop) degrades to whatever placement the bundles already carry
        and lets the write proceed.  The issue is reported loudly, never
        hidden; ``status`` stays out of the short-circuit set.
        """
        try:
            return hierarchy_agent_node(state)
        except Exception as exc:  # noqa: BLE001 — hierarchy must not stop the write
            logger.error(
                "hierarchy agent node failed (%s); continuing with stored placement",
                exc, exc_info=True,
            )
            report_call(
                f"  [hierarchy]  ! hierarchy failed — writing with stored placement —"
                f" {str(exc)[:120]}"
            )
            return {
                **state,
                "hierarchy_plan": state.get("hierarchy_plan")
                or {"decided_by": "degraded", "existing_updates": [], "resolved_concepts": 0},
                "concept_promotions": state.get("concept_promotions") or [],
                "status": "hierarchy_agent_done",
            }

    return _resilient_node
