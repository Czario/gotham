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


def build_parent_specific_segmentation_header(
    parent_concept: Optional[str],
    parent_label: Optional[str] = None,
    segment_type_or_header: Optional[str] = None,
) -> tuple[str, str]:
    """Build a parent-specific ``(custom_concept, human_label)`` for a segmentation header.

    Every parent line item (e.g. Revenue / Net Sales, Cost of Products Sold / Cost of Revenue,
    SG&A, R&D, Goodwill, Stock-based compensation) receives a distinct ``custom:`` concept name
    and a distinct human-readable label that starts with the parent's label so it is immediately
    clear in the UI which line item is being segmented.
    """
    p_local = str(parent_concept or "").split(":")[-1]
    p_low = p_local.lower()

    if any(k in p_low for k in ("revenue", "salesrevenue", "revenues")):
        p_prefix = "Revenue"
    elif any(k in p_low for k in ("costofgoodsandservicessold", "costofrevenue", "costofgoods", "costofsales")):
        p_prefix = "CostOfRevenue"
    elif "sellinggeneralandadministrative" in p_low:
        p_prefix = "SGA"
    elif "researchanddevelopment" in p_low:
        p_prefix = "RD"
    elif "operatingexpenses" in p_low:
        p_prefix = "OperatingExpenses"
    elif "operatingincomeloss" in p_low:
        p_prefix = "OperatingIncome"
    elif "sharebasedcompensation" in p_low:
        p_prefix = "ShareBasedCompensation"
    elif "incomelossfromequitymethodinvestments" in p_low or "equitymethod" in p_low:
        p_prefix = "EquityMethod"
    elif "noncontrollinginterest" in p_low:
        p_prefix = "NoncontrollingInterest"
    elif "depreciation" in p_low:
        p_prefix = "Depreciation"
    elif "goodwill" in p_low:
        p_prefix = "Goodwill"
    elif "intangible" in p_low:
        p_prefix = "IntangibleAssets"
    elif "propertyplantandequipment" in p_low:
        p_prefix = "PPE"
    elif "inventory" in p_low:
        p_prefix = "Inventory"
    elif "accountsreceivable" in p_low:
        p_prefix = "AccountsReceivable"
    elif "cashandcashequivalents" in p_low:
        p_prefix = "Cash"
    elif "debt" in p_low:
        p_prefix = "Debt"
    elif p_local:
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", p_local)
        p_prefix = "".join(w.capitalize() for w in words[:3]) or "LineItem"
    else:
        p_prefix = "LineItem"

    raw_seg = str(segment_type_or_header or "").split(":")[-1].strip()
    seg_low = raw_seg.lower()
    if any(k in seg_low for k in ("prod", "serv")):
        seg_tag = "ProductSegmentation"
        seg_lbl = "Product Segmentation"
    elif any(k in seg_low for k in ("geo", "country", "region")):
        seg_tag = "GeographicSegmentation"
        seg_lbl = "Geographic Segmentation"
    elif "awardtype" in seg_low or "award_type" in seg_low:
        seg_tag = "AwardTypeSegmentation"
        seg_lbl = "Award Type Segmentation"
    elif "incomestatementlocation" in seg_low:
        seg_tag = "IncomeStatementLocationSegmentation"
        seg_lbl = "Income Statement Location Segmentation"
    elif "investment" in seg_low or "cash_securities" in seg_low:
        seg_tag = "InvestmentSegmentation"
        seg_lbl = "Investment Segmentation"
    elif "fair_value" in seg_low or "fairvalue" in seg_low:
        seg_tag = "FairValueSegmentation"
        seg_lbl = "Fair Value Segmentation"
    elif "derivative" in seg_low:
        seg_tag = "DerivativeSegmentation"
        seg_lbl = "Derivative Segmentation"
    elif "relatedparty" in seg_low or "related_party" in seg_low:
        seg_tag = "RelatedPartySegmentation"
        seg_lbl = "Related Party Segmentation"
    elif any(k in seg_low for k in ("seg", "bus", "oper", "consolidat")):
        seg_tag = "SegmentSegmentation"
        seg_lbl = "Segment Segmentation"
    elif raw_seg:
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", raw_seg)
        filtered = [w for w in words if w.lower() not in ("custom", "segmentation", "segments", "breakdown")]
        if filtered:
            seg_tag = "".join(w.capitalize() for w in filtered) + "Segmentation"
            seg_lbl = " ".join(w.capitalize() for w in filtered) + " Segmentation"
        else:
            seg_tag = "SegmentSegmentation"
            seg_lbl = "Segment Segmentation"
    else:
        seg_tag = "SegmentSegmentation"
        seg_lbl = "Segment Segmentation"

    if seg_tag.lower().startswith(p_prefix.lower()):
        custom_concept = f"custom:{seg_tag}"
    else:
        custom_concept = f"custom:{p_prefix}{seg_tag}"

    clean_parent_lbl = (parent_label or "").strip().rstrip(":")
    if not clean_parent_lbl:
        clean_parent_lbl = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", p_prefix).strip()

    if clean_parent_lbl.lower() in seg_lbl.lower():
        human_label = seg_lbl
    else:
        human_label = f"{clean_parent_lbl} {seg_lbl}"

    return custom_concept, human_label


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

    # 3. Synthesize any parent_header referenced in dims (or custom parent) if not already declared in rows.
    # Guard: a custom: segmentation header belongs to ONE parent_concept. If a dim under parent B
    # accidentally references a custom: header owned by parent A (e.g. custom:RevenueSegmentSegmentation
    # on OperatingIncomeLoss), re-scope it to parent B via build_parent_specific_segmentation_header.
    parent_label_by_concept: dict[str, str] = {
        str(r["concept"]): str(r["label"]).strip()
        for r in norm_rows
        if r.get("concept") and r.get("label") and not str(r.get("concept")).startswith("custom:")
    }
    header_owner: dict[str, str] = {}
    for r in norm_rows:
        rc = str(r.get("concept") or "")
        rp = r.get("parent") or r.get("parent_concept")
        if rc.startswith("custom:") and rp and not str(rp).startswith("custom:"):
            header_owner.setdefault(rc, str(rp))

    existing_row_keys: set[tuple[Any, Any]] = {(r.get("parent") or r.get("parent_concept"), r.get("concept")) for r in norm_rows}
    header_counts: dict[Any, int] = defaultdict(int)
    for d in norm_dims:
        ph = d.get("parent_header")
        pc = d.get("parent_concept") or d.get("parent")
        if not ph and d.get("parent") and str(d.get("parent")).startswith("custom:"):
            ph = d.get("parent")
        if ph and str(ph).startswith("custom:"):
            if not pc or str(pc).startswith("custom:"):
                continue
            owner = header_owner.get(str(ph))
            if owner and owner != str(pc) and (pc, ph) not in existing_row_keys:
                new_ph, new_lbl = build_parent_specific_segmentation_header(
                    str(pc), parent_label_by_concept.get(str(pc)), str(ph)
                )
                ph = new_ph
                d["parent_header"] = new_ph
                if d.get("parent") and str(d.get("parent")).startswith("custom:"):
                    d["parent"] = new_ph
            else:
                header_owner.setdefault(str(ph), str(pc))
                new_lbl = None
            key = (pc, ph)
            if key not in existing_row_keys:
                existing_row_keys.add(key)
                header_counts[pc] += 1
                label = new_lbl or _re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(ph).split(":")[-1])
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

    # Map headers to their parents so members know their line item parent,
    # and ensure every custom: segmentation header has a parent-specific label.
    parent_label_by_concept: dict[str, str] = {
        str(r["concept"]): str(r["label"]).strip()
        for r in norm_rows
        if r.get("concept") and r.get("label") and not str(r.get("concept")).startswith("custom:")
    }
    header_parents: dict[str, str] = {}
    for r in norm_rows:
        if str(r.get("concept", "")).startswith("custom:") and r.get("parent"):
            p_c = str(r["parent"])
            header_parents[str(r["concept"])] = p_c
            p_lbl = parent_label_by_concept.get(p_c)
            if p_lbl:
                curr_lbl = str(r.get("label") or "").strip()
                # If label is missing or generic (doesn't mention the parent label/topic),
                # build a parent-qualified label so Revenue vs Cost vs SG&A headers are distinct.
                p_tokens = _concept_tokens(p_c, p_lbl)
                lbl_tokens = _concept_tokens("", curr_lbl) if curr_lbl else set()
                if not curr_lbl or not (p_tokens & lbl_tokens):
                    _, qualified_lbl = build_parent_specific_segmentation_header(
                        p_c, p_lbl, str(r["concept"])
                    )
                    r["label"] = qualified_lbl

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
            dict(r)
            for r in repo.collection.find({
                "cik": getattr(bundle, "company_cik", None),
                "statement_type": getattr(bundle, "statement_type", None),
                "dimension_concept": False,
                "path": {"$not": _POLLUTED_PATH_RX},
            })
            if not r.get("dimension_concept") and not is_polluted_path(r.get("path"))
        ]
    except Exception:
        main = []
    try:
        dims = [
            dict(r)
            for r in repo.collection.find({
                "cik": getattr(bundle, "company_cik", None),
                "statement_type": getattr(bundle, "statement_type", None),
                "dimension_concept": True,
                "path": {"$not": _POLLUTED_PATH_RX},
            })
            if r.get("dimension_concept") and not is_polluted_path(r.get("path"))
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


def _filing_rows_for(bundle: Any, sec_presentation: Optional[dict] = None) -> list[dict]:
    concrete = list(getattr(bundle, "concepts", None) or [])
    abstracts = list(getattr(bundle, "abstract_concepts", None) or [])

    # Lookup SEC presentation linkbase metadata if available
    sec_nodes_by_concept: dict[str, dict] = {}
    if isinstance(sec_presentation, dict):
        from core.extractors.sec_presentation_extractor import canonical_statement_type
        canon_st = canonical_statement_type(getattr(bundle, "statement_type", ""))
        stmt_pres = (sec_presentation.get("statements") or {}).get(canon_st) or {}
        for pnode in stmt_pres.get("nodes") or []:
            if isinstance(pnode, dict) and pnode.get("concept"):
                sec_nodes_by_concept[str(pnode["concept"])] = pnode

    # path -> concept, so each row's filing parent is visible to the model
    path_to_concept: dict[str, str] = {}
    for item in concrete + abstracts:
        if isinstance(item, dict) and item.get("path"):
            path_to_concept[str(item["path"])] = item.get("concept", "")

    def _parent_of(item: dict) -> Optional[str]:
        if "parent_concept" in item or "parent" in item:
            explicit_p = item.get("parent_concept") or item.get("parent")
            return str(explicit_p) if explicit_p else None
        sec_node = sec_nodes_by_concept.get(str(item.get("concept") or ""))
        if sec_node and ("parent_concept" in sec_node or "calc_parent_concept" in sec_node):
            sp = sec_node.get("parent_concept") or sec_node.get("calc_parent_concept")
            return str(sp) if sp else None
        path = str(item.get("path") or "")
        if "." in path:
            p = path_to_concept.get(path.rsplit(".", 1)[0])
            if p:
                return p
        return None

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
    for idx, item in enumerate(concrete + abstracts):
        if isinstance(item, dict) and item.get("concept"):
            sec_node = sec_nodes_by_concept.get(str(item["concept"])) or {}
            html_parent = sec_node.get("html_parent_concept")
            r_order = (
                item.get("reading_order")
                or sec_node.get("html_reading_order")
                or sec_node.get("reading_order")
                or (idx + 1)
            )
            row = {
                "concept": item["concept"],
                "label": item.get("label") or sec_node.get("label"),
                "abstract": bool(item.get("abstract")),
                "calculated": bool(item.get("calculated")),
                "value": item.get("value"),
                "path": item.get("path"),            # filing reference only
                "order_key": item.get("order_key"),
                "parent_concept": html_parent or _parent_of(item),
                "sec_parent_concept": item.get("sec_parent_concept") or sec_node.get("sec_parent_concept"),
                "calc_parent_concept": sec_node.get("calc_parent_concept"),
                "reading_order": r_order,
                "dimension_concept": False,
            }
            period = incoming_period_info.get(str(item["concept"]))
            if period is None or period[0] is None:
                period = _latest_period([fallback_period])
            if period[0] is not None:
                row["_latest_period_rank"], row["latest_period"] = period
            rows.append(row)
    # Sort main rows by human-visible HTML / SEC presentation top-down reading_order
    rows.sort(key=lambda r: int(r.get("reading_order") or 999999))
    for dim in getattr(bundle, "dimensional_concepts", None) or []:
        if isinstance(dim, dict) and dim.get("concept"):
            dim_data = dim.get("dimension_data")
            dim_val = dim_data.get("value") if isinstance(dim_data, dict) else dim.get("value")
            dim_row = {
                "concept": dim["concept"],
                "label": dim.get("label"),
                "value": dim_val,
                "parent_concept": dim.get("parent_concept"),
                "segment_type": dim.get("segment_type"),
                "dimension_concept": True,
            }
            dim_period = _latest_period([fallback_period])
            if dim_period[0] is not None:
                dim_row["_latest_period_rank"], dim_row["latest_period"] = dim_period
            rows.append(dim_row)
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


def _canonical_header_for_segment_type(segment_type: Optional[str]) -> Optional[str]:
    """Deterministically map any XBRL axis / segment_type to a canonical custom grouping header."""
    seg_low = (segment_type or "").strip().lower()
    if not seg_low or seg_low in ("unknown", "dimensional"):
        return None
    if any(k in seg_low for k in ("prod", "serv")):
        return "custom:ProductSegmentation"
    if any(k in seg_low for k in ("geo", "country", "region")):
        return "custom:GeographicSegmentation"
    if any(k in seg_low for k in ("seg", "bus", "oper", "consolidat")):
        return "custom:BusinessSegments"
    if "awardtype" in seg_low or "award_type" in seg_low:
        return "custom:AwardTypeSegmentation"
    if "incomestatementlocation" in seg_low:
        return "custom:IncomeStatementLocationSegmentation"
    if "investment" in seg_low or "cash_securities" in seg_low:
        return "custom:InvestmentSegmentation"
    if "fair_value" in seg_low or "fairvalue" in seg_low:
        return "custom:FairValueSegmentation"
    if "derivative" in seg_low:
        return "custom:DerivativeSegmentation"
    words = re.findall(r"[A-Za-z0-9]+", seg_low)
    if words:
        pascal = "".join(w.capitalize() for w in words)
        if not pascal.endswith("Segmentation") and not pascal.endswith("Segments"):
            pascal += "Segmentation"
        return f"custom:{pascal}"
    return None


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
        # Match against any stored custom header whose tokens overlap segment_type
        seg_tokens = _concept_tokens(seg_low, seg_low)
        for h in parent_headers:
            h_low = h.lower()
            if seg_low in h_low or (seg_tokens and (seg_tokens & _concept_tokens(h))):
                return h
        canon_h = _canonical_header_for_segment_type(segment_type)
        if canon_h:
            return canon_h

    if len(parent_headers) == 1:
        return parent_headers[0]
    if len(parent_headers) == 0:
        for s in parent_dims:
            if s.get("parent_header"):
                return str(s["parent_header"])
    return None


_POLLUTED_EQUITY_ROLLFORWARD_CONCEPTS = {
    "us-gaap:RetainedEarningsMember",
    "RetainedEarningsMember",
    "us-gaap:CommonStockMember",
    "CommonStockMember",
    "us-gaap:AdditionalPaidInCapitalMember",
    "AdditionalPaidInCapitalMember",
    "us-gaap:TreasuryStockCommonMember",
    "TreasuryStockCommonMember",
    "us-gaap:TreasuryStockPreferredMember",
    "TreasuryStockPreferredMember",
    "us-gaap:AccumulatedOtherComprehensiveIncomeMember",
    "AccumulatedOtherComprehensiveIncomeMember",
    "custom:EquityComponentSegmentation",
}


def _repair_corrupted_stored_hierarchy_if_needed(
    norm_service: Any,
    bundle: Any,
    stored: list[dict],
) -> bool:
    """Detect and repair in-place any stored hierarchy corrupted by sibling-as-parent
    paths (e.g. COGS/SG&A under Sales, PPE Net under PPE Gross) or leaked equity members.
    Returns True if the stored DB rows were modified and should be reloaded.
    """
    if not stored:
        return False
    repo_resolver = getattr(norm_service, "_get_concept_repo_by_form_type", None)
    if not callable(repo_resolver):
        return False
    form_type = getattr(bundle, "form_type", None) or "10-Q"
    cik = str(getattr(bundle, "company_cik", "") or "")
    stmt_type = str(getattr(bundle, "statement_type", "") or "")
    try:
        repo = repo_resolver(form_type)
        coll = repo.collection
    except Exception:
        return False

    modified_db = False

    # 1. Remove leaked Statement of Stockholders' Equity roll-forward members
    polluted_ids = [
        r["_id"]
        for r in stored
        if r.get("_id") is not None and str(r.get("concept") or "") in _POLLUTED_EQUITY_ROLLFORWARD_CONCEPTS
    ]
    if polluted_ids and hasattr(coll, "delete_many"):
        try:
            coll.delete_many({"_id": {"$in": polluted_ids}})
            modified_db = True
        except Exception:
            pass

    return modified_db


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

        def _process_one_bundle(bundle):
            stmt_t0 = time.perf_counter()
            stmt_type = getattr(bundle, "statement_type", "?")
            stored, stored_paths, occupied_paths, identity_paths, identity_order_keys = _stored_rows_for(norm_service, bundle)
            if stored and _repair_corrupted_stored_hierarchy_if_needed(norm_service, bundle, stored):
                stored, stored_paths, occupied_paths, identity_paths, identity_order_keys = _stored_rows_for(norm_service, bundle)
            filing_rows = _filing_rows_for(bundle, sec_presentation=state.get("sec_presentation"))
            if not filing_rows and not stored:
                return None
            was_seeded = not stored

            # ── Fast-path: skip LLM when all filing concepts & (parent, dim) pairs are placed ──
            # Require a valid non-polluted path on stored rows AND check dimensional
            # identity by (parent_concept, concept) so unplaced (path=None) rows or
            # existing members appearing under a new parent always run the agent.
            from normalization.src.data_normalization_service.database.database import is_polluted_path as _is_polluted
            proposal: dict = {"merges": {}, "rows": [], "dims": []}
            stored_main_names: set[str] = set()
            stored_dim_identities: set[tuple[Any, str]] = set()
            for r in stored:
                c = r.get("concept")
                r_path = r.get("path")
                if not c or not r_path or _is_polluted(r_path):
                    continue
                if r.get("dimension_concept"):
                    r_p = r.get("parent_concept") or r.get("parent")
                    stored_dim_identities.add((r_p, c))
                    if "_" in c:
                        stored_dim_identities.add((r_p, c.split("_")[-1]))
                    for al in r.get("aliases") or []:
                        if al:
                            stored_dim_identities.add((r_p, al))
                else:
                    stored_main_names.add(c)
                    if "_" in c:
                        stored_main_names.add(c.split("_")[-1])
                    for al in r.get("aliases") or []:
                        if al:
                            stored_main_names.add(al)

            new_main = {
                str(r["concept"])
                for r in filing_rows
                if not r.get("dimension_concept") and r.get("concept") and r["concept"] not in stored_main_names
            }
            new_dim_identities = {
                (r.get("parent_concept") or r.get("parent"), str(r["concept"]))
                for r in filing_rows
                if r.get("dimension_concept")
                and r.get("concept")
                and (r.get("parent_concept") or r.get("parent"), r["concept"]) not in stored_dim_identities
            }
            new_concepts = new_main | {c for _, c in new_dim_identities}
            no_new_concept = bool(state.get("no_new_concept"))
            if no_new_concept and not stored:
                report_call(
                    f"  [hierarchy]  ! {stmt_type}: no existing stored hierarchy under --no-new-concept — skipping"
                )
                logger.warning(
                    "hierarchy %s/%s: no existing stored hierarchy under --no-new-concept; blocking persist",
                    bundle.company_cik, stmt_type,
                )
                return (stmt_type, False, [], [], True)

            def _stored_preview() -> dict:
                return {
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

            if stored and not new_main and not new_dim_identities:
                # All concepts are known — reuse the stored hierarchy verbatim.
                # No LLM and no encoding: the stored rows already carry their
                # agent-approved path/order_key/parent, so reuse them as-is.
                stmt_dur = time.perf_counter() - stmt_t0
                logger.info(
                    "[hierarchy] %s: fast-path (0 new concepts) %.2fs",
                    stmt_type, stmt_dur,
                )
                report_detail(f"{stmt_type}: fast-path")
                proposal["_preview"] = _stored_preview()
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
                    sec_presentation=state.get("sec_presentation"),
                    no_new_concept=no_new_concept,
                )
                from ..tools.html_table_tools import build_html_table_tools
                html_tools = build_html_table_tools(
                    html_text=state.get("html_text"),
                    accession_number=state.get("accession_number") or getattr(bundle, "accession_number", None),
                    ticker=state.get("ticker"),
                    include_text_search=False,
                )
                tools = list(tools) + list(html_tools)

                if no_new_concept:
                    logger.info("[hierarchy] %s: --no-new-concept mapping-only (+%d unstored vs %d stored) -> agent", stmt_type, len(new_concepts), len(stored))
                elif not stored:
                    logger.info("[hierarchy] %s: fresh seed -> agent", stmt_type)
                else:
                    logger.info("[hierarchy] %s: +%d new (%d stored) -> agent", stmt_type, len(new_concepts), len(stored))
                report_detail(f"{stmt_type}: running agent...")

                sec_pres_dump = str(proposal.pop("_sec_pres_dump", "") or "")
                context_dump = str(proposal.pop("_context_dump", "") or "")
                sec_section = (
                    f"\n\nSEC PRESENTATION LINKBASE:\n{sec_pres_dump[:18000]}"
                    if sec_pres_dump
                    else ""
                )
                if no_new_concept:
                    task = (
                        f"STRICT EXISTING-HIERARCHY MAPPING (--no-new-concept) for {getattr(bundle, 'statement_type', '?')} "
                        f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}).\n"
                        f"DO NOT create or propose any new concepts or modify the stored hierarchy.\n\n"
                        f"{context_dump}{sec_section}\n\n"
                        f"WORKFLOW:\n"
                        f"1. Inspect the NEW INCOMING CONCEPTS above (and High-Probability Alias Candidates; use find_html_table_by_value if needed).\n"
                        f"2. If an incoming concept/member clearly maps to an existing STORED concept in the hierarchy, call decide_mapping({{\"concept\": \"<incoming>\", \"same_as\": \"<stored>\", \"keep_tag\": \"stored\"}}).\n"
                        f"3. Any genuinely new concept that cannot map to the existing hierarchy will be skipped automatically — just call finalize_hierarchy({{\"status\": \"done\"}})."
                    )
                elif not stored:
                    task = (
                        f"FRESH SEED for {getattr(bundle, 'statement_type', '?')} "
                        f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                        f"No existing hierarchy is stored in DB.\n\n"
                        f"{context_dump}{sec_section}\n\n"
                        f"WORKFLOW:\n"
                        f"- Limit HTML lookups to at most 2 calls (pass label_hint to find_html_table_by_value(value=..., label_hint=...) when helpful) — then immediately call propose_hierarchy.\n"
                        f"- Call propose_hierarchy(rows_json, dims_json) once (it auto-checks structural rules inline).\n"
                        f"- Fix any reported FINDINGS via move_row/propose_hierarchy, then immediately call finalize_hierarchy({{\"status\": \"done\"}})."
                    )
                else:
                    task = (
                        f"INCREMENTAL UPDATE for {getattr(bundle, 'statement_type', '?')} "
                        f"for CIK {getattr(bundle, 'company_cik', '?')} ({getattr(bundle, 'form_type', '?')}). "
                        f"Stored rows: {len(stored)}, New incoming concepts: {len(new_concepts)}. "
                        f"The existing stored hierarchy in DB is verified ground truth — DO NOT redefine it completely.\n\n"
                        f"{context_dump}{sec_section}\n\n"
                        f"WORKFLOW:\n"
                        f"1. Check 'High-Probability Alias Candidates': if any new concept is an alias/synonym of a stored concept, call decide_mapping({{\"concept\": \"<incoming>\", \"same_as\": \"<stored>\"}});\n"
                        f"2. Limit HTML lookups to at most 2 calls (call find_html_table_by_value(value=..., label_hint=...) only for items whose placement isn't clear from SEC PRESENTATION above);\n"
                        f"3. Call propose_hierarchy(rows_json, dims_json) passing ONLY the new concept(s) with their parent and sibling position (it auto-checks structural rules inline);\n"
                        f"4. Fix any FINDINGS if reported, then immediately call finalize_hierarchy({{\"status\": \"done\"}})."
                    )
                if True:
                    def _validate_all_placed(_parsed: dict) -> Optional[str]:
                        if no_new_concept:
                            return None
                        prev = proposal.get("_preview")
                        if not prev or not prev.get("rows"):
                            return "Call propose_hierarchy(rows_json, dims_json) before finalizing."
                        if stored and new_concepts:
                            merged_keys = set((proposal.get("merges") or {}).keys())
                            placed_main = {str(r.get("concept")) for r in (prev.get("rows") or []) if r.get("concept")}
                            placed_dims = {
                                (d.get("parent_concept"), d.get("concept"))
                                for d in (prev.get("dims") or [])
                                if d.get("concept")
                            }
                            missing: list[str] = []
                            for nc in filing_rows:
                                if not isinstance(nc, dict):
                                    continue
                                nc_c = str(nc.get("concept") or "")
                                if not nc_c or nc_c in merged_keys:
                                    continue
                                if nc.get("dimension_concept"):
                                    nc_p = nc.get("parent_concept") or nc.get("parent")
                                    if (nc_p, nc_c) in new_dim_identities and (nc_p, nc_c) not in placed_dims:
                                        missing.append(f"{nc_p} -> {nc_c}")
                                elif nc_c in new_main and nc_c not in placed_main:
                                    missing.append(nc_c)
                            if missing:
                                return (
                                    f"Incomplete: you omitted {len(missing)} new item(s): {', '.join(missing[:8])}. "
                                    f"Include them in propose_hierarchy(rows_json, dims_json) or merge via decide_mapping."
                                )
                        return None

                    try:
                        _ = run_agent_loop(
                            AGENTIC_HIERARCHY_SYSTEM_PROMPT,
                            task,
                            tools,
                            ticker=str(state.get("ticker") or state.get("cik") or "?"),
                            finalize_name="finalize_hierarchy",
                            finalize_description="Call when the full hierarchy is decided. Pass {\"status\": \"done\"}.",
                            chat_llm=chat_llm,
                            max_steps=6 if no_new_concept else 10,
                            role="hierarchy",
                            on_event=on_event,
                            finalize_validator=_validate_all_placed,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("hierarchy agent failed for %s/%s: %s",
                                       bundle.company_cik, bundle.statement_type, exc)

                if no_new_concept:
                    # Strictly keep the stored hierarchy untouched in --no-new-concept mode:
                    # drop any concept/dim that neither matches stored nor mapped via decide_mapping.
                    proposal["_preview"] = _stored_preview()
                    merges = proposal.get("merges") or {}
                    mapped_targets = {
                        c: m.get("same_as")
                        for c, m in merges.items()
                        if isinstance(m, dict) and m.get("same_as") in (stored_main_names | {mc for _, mc in stored_dim_identities})
                    }
                    kept_concrete = [
                        it for it in (getattr(bundle, "concepts", None) or [])
                        if isinstance(it, dict) and (it.get("concept") in stored_main_names or it.get("concept") in mapped_targets)
                    ]
                    kept_abstract = [
                        it for it in (getattr(bundle, "abstract_concepts", None) or [])
                        if isinstance(it, dict) and (it.get("concept") in stored_main_names or it.get("concept") in mapped_targets)
                    ]
                    kept_dims = [
                        d for d in (getattr(bundle, "dimensional_concepts", None) or [])
                        if isinstance(d, dict) and (
                            (d.get("parent_concept") or d.get("container_concept"), d.get("concept")) in stored_dim_identities
                            or d.get("concept") in mapped_targets
                        )
                    ]
                    dropped_n = (
                        len(getattr(bundle, "concepts", None) or []) - len(kept_concrete)
                        + len(getattr(bundle, "dimensional_concepts", None) or []) - len(kept_dims)
                    )
                    bundle.concepts = kept_concrete
                    bundle.abstract_concepts = kept_abstract
                    bundle.dimensional_concepts = kept_dims
                    if dropped_n:
                        logger.info(
                            "[hierarchy] %s: --no-new-concept skipped %d unmapped new item(s) (%d mapped)",
                            stmt_type, dropped_n, len(mapped_targets),
                        )

            # The agent's propose_hierarchy tool already materialized and checked
            # the tree.  If no checked tree exists, return a sentinel so decide
            # blocks persistence rather than writing path=None rows to MongoDB.
            preview = proposal.get("_preview")
            if not preview or not preview.get("rows"):
                report_call(
                    f"  [hierarchy]  ! {bundle.statement_type}: no agent-checked tree — blocking persist to avoid unplaced concepts"
                )
                logger.error(
                    "hierarchy agent produced no checked tree for %s/%s; blocking statement persist",
                    bundle.company_cik, bundle.statement_type,
                )
                return (stmt_type, False, [], [], True)
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
                # Never fall back across different parent_concepts: a dimensional member
                # like hrl:RetailSegmentMember repeats under Revenue, Cost, and OperatingIncome.
                entry = dim_plan.get((dim.get("parent_concept"), target))
                if not entry and dim.get("container_concept"):
                    entry = dim_plan.get((dim.get("container_concept"), target))
                if entry:
                    dim["path"], dim["order_key"], dim["parent_header"] = entry
                    dim["hierarchy_level"] = len(str(dim["path"]).split(".")) - 1
                    dim["level"] = dim["hierarchy_level"]
                    dim["_hierarchy_resolved"] = True
                else:
                    # Safety net: never leave a dim with path=None / order_key=None
                    p_c = dim.get("parent_concept") or dim.get("container_concept")
                    p_row = next((i for i in list(concrete) if isinstance(i, dict) and i.get("concept") == p_c and i.get("path")), None)
                    p_path = p_row.get("path") if p_row else stored_paths.get(str(p_c or ""))
                    if p_c and p_path:
                        p_lbl = (p_row or {}).get("label") or str(p_c).split(":")[-1]
                        hdr_c, hdr_lbl = build_parent_specific_segmentation_header(
                            str(p_c), p_lbl, dim.get("segment_type") or "segment"
                        )
                        existing_hdr = next(
                            (a for a in abstracts if isinstance(a, dict) and a.get("concept") == hdr_c and a.get("parent_concept") == p_c and a.get("path")),
                            None,
                        )
                        if not existing_hdr:
                            hdr_path = f"{p_path}.001"
                            abstracts.append({
                                "concept": hdr_c, "label": hdr_lbl, "value": None, "period": None,
                                "abstract": True, "parent_concept": p_c, "path": hdr_path,
                                "order_key": "a", "hierarchy_level": len(hdr_path.split(".")) - 1,
                                "level": len(hdr_path.split(".")) - 1, "_hierarchy_resolved": True,
                            })
                        else:
                            hdr_path = existing_hdr["path"]
                        sibling_count = sum(
                            1 for d2 in (getattr(bundle, "dimensional_concepts", None) or [])
                            if isinstance(d2, dict) and str(d2.get("path") or "").startswith(f"{hdr_path}.")
                        )
                        n_idx = sibling_count + 1
                        dim["path"] = f"{hdr_path}.{n_idx:03d}"
                        dim["order_key"] = chr(ord("a") + min(n_idx - 1, 25))
                        dim["parent_header"] = hdr_c
                        dim["hierarchy_level"] = len(dim["path"].split(".")) - 1
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
            bundle._agent_updates = existing_updates
            if not (stored and not new_concepts):
                stmt_dur = time.perf_counter() - stmt_t0
                logger.info(
                    "[hierarchy] %s resolved in %.1fs (%d rows, %d re-pathed)",
                    stmt_type, stmt_dur, len(rows), len(existing_updates),
                )
                report_detail(f"{stmt_type}: done")
            return (stmt_type, was_seeded, existing_updates, promotions, False)

        if len(bundles) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(3, len(bundles))) as pool:
                results = list(pool.map(_process_one_bundle, bundles))
        else:
            results = [_process_one_bundle(b) for b in bundles]

        extra_findings: list[dict] = []
        unresolved_statements: list[str] = []
        for res in results:
            if res is None:
                continue
            st_name, was_seeded, ex_updates, promos, unresolved = res
            if unresolved:
                unresolved_statements.append(st_name)
                extra_findings.append({
                    "type": "unresolved_hierarchy",
                    "severity": "high",
                    "statement_type": st_name,
                    "message": f"{st_name}: hierarchy agent produced no checked tree — refusing to write unplaced concepts",
                })
                continue
            if was_seeded:
                seeded.append(st_name)
            total_updates += len(ex_updates)
            all_promotions.extend(promos)
            reasoned_about += 1

        merged_findings = list(state.get("findings") or []) + extra_findings
        plan = {
            "decided_by": "agent",
            "seeded_statement_types": seeded,
            "unresolved_statements": unresolved_statements,
            "existing_updates": [
                u for b in bundles for u in getattr(b, "_agent_updates", [])
            ],
            "resolved_concepts": sum(
                len([i for i in list(getattr(b, "concepts", None) or []) + list(getattr(b, "abstract_concepts", None) or []) if isinstance(i, dict) and i.get("path")])
                for b in bundles
            ),
        }
        logger.info(
            "[hierarchy] %d statement(s) processed (%d stored row update(s))",
            reasoned_about, total_updates,
        )
        return {**state, "findings": merged_findings, "hierarchy_plan": plan, "concept_promotions": all_promotions, "status": "hierarchy_agent_done"}

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
