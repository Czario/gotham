"""Derived accounting-identity concepts and child hierarchy enrichment.

Automatically computes missing provable financial concepts (e.g., Gross Profit,
Total Operating Expenses, Total Liabilities, Total Non-Current Assets, Total
Non-Current Liabilities) from reported line items using standard accounting
identities, marks them with ``calculated=True``, and groups their child items
underneath them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


_REVENUE_LOCALS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenueFromSaleOfGoods",
    "Revenue",
)

_COST_OF_REVENUE_LOCALS = (
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "CostOfGoodsSold",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "CostOfSales",
)

_OPEX_CHILD_LOCALS = frozenset({
    "SellingGeneralAndAdministrativeExpense",
    "SellingAndMarketingExpense",
    "GeneralAndAdministrativeExpense",
    "ResearchAndDevelopmentExpense",
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    "MarketingExpense",
    "SellingExpense",
    "DepreciationAndAmortization",
    "DepreciationDepletionAndAmortization",
    "RestructuringCharges",
    "RestructuringSettlementAndImpairmentProvisions",
    "GoodwillImpairmentLoss",
    "AssetImpairmentCharges",
    "PreOpeningCosts",
    "OtherCostAndExpenseOperating",
    "OtherOperatingExpenses",
})

_ASSETS_ROOT_LOCALS = ("Assets",)
_ASSETS_CURRENT_LOCALS = ("AssetsCurrent", "CurrentAssets")
_ASSETS_NONCURRENT_LOCALS = ("AssetsNoncurrent", "NoncurrentAssets")

_LSE_ROOT_LOCALS = ("LiabilitiesAndStockholdersEquity", "EquityAndLiabilities")
_LIABILITIES_ROOT_LOCALS = ("Liabilities", "TotalLiabilities")
_LIABILITIES_CURRENT_LOCALS = ("LiabilitiesCurrent", "CurrentLiabilities")
_LIABILITIES_NONCURRENT_LOCALS = ("LiabilitiesNoncurrent", "NoncurrentLiabilities")

_EQUITY_LOCALS = (
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "StockholdersEquity",
    "Equity",
)
_MEZZANINE_EQUITY_LOCALS = (
    "TemporaryEquityCarryingAmountAttributableToParent",
    "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
    "RedeemableNoncontrollingInterestEquityCarryingAmount",
)


def _local_name(concept: Optional[str]) -> str:
    return str(concept or "").split(":")[-1]


def _to_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _find_first_by_locals(items: List[Dict], locals_seq: tuple[str, ...]) -> tuple[int, Optional[Dict]]:
    for target in locals_seq:
        for idx, item in enumerate(items):
            if item.get("abstract"):
                continue
            if _local_name(item.get("concept")) == target and _to_float(item.get("value")) is not None:
                return idx, item
    return -1, None


def _has_any_local(items: List[Dict], locals_seq: tuple[str, ...]) -> bool:
    target_set = set(locals_seq)
    return any(_local_name(it.get("concept")) in target_set for it in items)


def _comparative_map(item: Dict) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for cf in item.get("comparative_facts") or []:
        if not isinstance(cf, dict):
            continue
        p = cf.get("period") or cf.get("end_date")
        v = _to_float(cf.get("value"))
        if p and v is not None:
            out[str(p)] = v
    return out


def _derive_comparatives(
    pos_items: List[Dict],
    neg_items: List[Dict],
    *,
    abs_neg: bool = False,
) -> List[Dict[str, Any]]:
    """Compute derived values across all comparative periods shared by operands."""
    if not pos_items and not neg_items:
        return []
    pos_maps = [_comparative_map(it) for it in pos_items]
    neg_maps = [_comparative_map(it) for it in neg_items]
    all_maps = pos_maps + neg_maps
    if not all_maps:
        return []
    common_periods = set(all_maps[0].keys())
    for m in all_maps[1:]:
        common_periods &= set(m.keys())
    derived: List[Dict[str, Any]] = []
    for p in sorted(common_periods):
        total = sum(m[p] for m in pos_maps)
        for m in neg_maps:
            total -= abs(m[p]) if abs_neg else m[p]
        derived.append({"period": p, "value": round(total, 6)})
    return derived


def _make_derived_item(
    *,
    concept: str,
    label: str,
    value: float,
    template: Dict[str, Any],
    parent_concept: Optional[str] = None,
    level: int = 0,
    comparative_facts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "concept": concept,
        "label": label,
        "value": round(value, 6),
        "abstract": False,
        "calculated": True,
        "source": "derived_accounting_identity",
        "parent_concept": parent_concept,
        "level": level,
        "unit": template.get("unit", "USD"),
        "decimals": template.get("decimals"),
        "period": template.get("period"),
        "dimension_concept": False,
        "comparative_facts": comparative_facts or [],
    }
    return item


def _enrich_income(flat_data: List[Dict]) -> List[Dict]:
    items = list(flat_data)

    # 1. Gross Profit = Revenue - |Cost of Revenue|
    if not _has_any_local(items, ("GrossProfit", "GrossMargin")):
        rev_idx, rev_item = _find_first_by_locals(items, _REVENUE_LOCALS)
        cost_idx, cost_item = _find_first_by_locals(items, _COST_OF_REVENUE_LOCALS)
        if rev_item is not None and cost_item is not None:
            rev_val = _to_float(rev_item.get("value"))
            cost_val = _to_float(cost_item.get("value"))
            if rev_val is not None and cost_val is not None:
                gp_val = rev_val - abs(cost_val)
                gp_comp = _derive_comparatives([rev_item], [cost_item], abs_neg=True)
                gp_item = _make_derived_item(
                    concept="us-gaap:GrossProfit",
                    label="Gross Profit",
                    value=gp_val,
                    template=rev_item,
                    parent_concept=None,
                    level=0,
                    comparative_facts=gp_comp,
                )
                insert_at = max(rev_idx, cost_idx) + 1
                items.insert(insert_at, gp_item)

    # 2. Total Operating Expenses = sum(OpEx children), verified against GrossProfit - OperatingIncomeLoss
    # Note: CostsAndExpenses includes COGS + OpEx, so its presence does NOT replace OperatingExpenses.
    if not _has_any_local(
        items,
        ("OperatingExpenses", "TotalOperatingExpenses", "NoninterestExpense"),
    ):
        opinc_idx, opinc_item = _find_first_by_locals(
            items, ("OperatingIncomeLoss", "IncomeLossFromOperations")
        )
        _, gp_item = _find_first_by_locals(items, ("GrossProfit", "GrossMargin"))
        opex_cutoff = opinc_idx if opinc_idx >= 0 else len(items)
        opex_children_idx: List[tuple[int, Dict]] = [
            (idx, it)
            for idx, it in enumerate(items[:opex_cutoff])
            if not it.get("abstract")
            and _local_name(it.get("concept")) in _OPEX_CHILD_LOCALS
            and _to_float(it.get("value")) is not None
        ]
        # Only create Total Operating Expenses when there are 2+ operating expense
        # items to group; a single child (e.g. SG&A alone) needs no parent header.
        if len(opex_children_idx) >= 2:
            opex_children = [it for _, it in opex_children_idx]
            sum_opex = sum(abs(_to_float(it.get("value")) or 0.0) for it in opex_children)
            verified = True
            if gp_item is not None and opinc_item is not None:
                gp_val = _to_float(gp_item.get("value"))
                opinc_val = _to_float(opinc_item.get("value"))
                if gp_val is not None and opinc_val is not None:
                    implied_opex = gp_val - opinc_val
                    if abs(sum_opex - implied_opex) > max(abs(implied_opex) * 0.02, 1.0):
                        verified = False
            if verified and sum_opex > 0:
                opex_comp = _derive_comparatives(opex_children, [])
                opex_item = _make_derived_item(
                    concept="us-gaap:OperatingExpenses",
                    label="Total Operating Expenses",
                    value=sum_opex,
                    template=opex_children[0],
                    parent_concept=None,
                    level=0,
                    comparative_facts=opex_comp,
                )
                first_opex_idx = opex_children_idx[0][0]
                for _, child in opex_children_idx:
                    child["parent_concept"] = "us-gaap:OperatingExpenses"
                    child["level"] = 1
                items.insert(first_opex_idx, opex_item)

    # 3. Operating Income = Gross Profit - Total Operating Expenses (or single OpEx item like SG&A)
    if not _has_any_local(items, ("OperatingIncomeLoss", "IncomeLossFromOperations")):
        gp_idx, gp_item = _find_first_by_locals(items, ("GrossProfit", "GrossMargin"))
        opex_idx, opex_item = _find_first_by_locals(
            items,
            ("OperatingExpenses", "TotalOperatingExpenses", "SellingGeneralAndAdministrativeExpense"),
        )
        if gp_item is not None and opex_item is not None:
            gp_val = _to_float(gp_item.get("value"))
            opex_val = _to_float(opex_item.get("value"))
            if gp_val is not None and opex_val is not None:
                opinc_val = gp_val - abs(opex_val)
                opinc_comp = _derive_comparatives([gp_item], [opex_item], abs_neg=True)
                opinc_item = _make_derived_item(
                    concept="us-gaap:OperatingIncomeLoss",
                    label="Operating Income (Loss)",
                    value=opinc_val,
                    template=gp_item,
                    parent_concept=None,
                    level=0,
                    comparative_facts=opinc_comp,
                )
                # Insert after the last OpEx child
                last_opex_idx = opex_idx
                for idx_c in range(opex_idx + 1, len(items)):
                    if items[idx_c].get("parent_concept") == opex_item.get("concept"):
                        last_opex_idx = idx_c
                    else:
                        break
                items.insert(last_opex_idx + 1, opinc_item)

    return items


def _enrich_balancesheet(flat_data: List[Dict]) -> List[Dict]:
    items = list(flat_data)

    # 1. Total Liabilities = LSE - StockholdersEquity (- Mezzanine Equity if present)
    if not _has_any_local(items, _LIABILITIES_ROOT_LOCALS):
        lse_idx, lse_item = _find_first_by_locals(items, _LSE_ROOT_LOCALS)
        if lse_item is None:
            lse_idx, lse_item = _find_first_by_locals(items, _ASSETS_ROOT_LOCALS)
        eq_idx, eq_item = _find_first_by_locals(items, _EQUITY_LOCALS)
        if lse_item is not None and eq_item is not None:
            lse_val = _to_float(lse_item.get("value"))
            eq_val = _to_float(eq_item.get("value"))
            mezz_items = [
                it for it in items
                if not it.get("abstract")
                and _local_name(it.get("concept")) in _MEZZANINE_EQUITY_LOCALS
                and _to_float(it.get("value")) is not None
            ]
            mezz_val = sum(_to_float(it.get("value")) or 0.0 for it in mezz_items)
            if lse_val is not None and eq_val is not None:
                liab_val = lse_val - eq_val - mezz_val
                _, liab_curr_item = _find_first_by_locals(items, _LIABILITIES_CURRENT_LOCALS)
                curr_liab_val = _to_float(liab_curr_item.get("value")) if liab_curr_item else None
                if curr_liab_val is None or liab_val >= curr_liab_val - 1.0:
                    liab_comp = _derive_comparatives([lse_item], [eq_item] + mezz_items)
                    lse_concept = lse_item.get("concept")
                    liab_parent = (
                        lse_concept
                        if _local_name(lse_concept) in _LSE_ROOT_LOCALS
                        else None
                    )
                    liab_item = _make_derived_item(
                        concept="us-gaap:Liabilities",
                        label="Total Liabilities",
                        value=liab_val,
                        template=lse_item,
                        parent_concept=liab_parent,
                        level=1 if liab_parent else 0,
                        comparative_facts=liab_comp,
                    )
                    curr_liab_idx, _ = _find_first_by_locals(items, _LIABILITIES_CURRENT_LOCALS)
                    insert_at = curr_liab_idx if curr_liab_idx >= 0 else (eq_idx if eq_idx >= 0 else len(items))
                    items.insert(insert_at, liab_item)
                    if liab_curr_item is not None:
                        liab_curr_item["parent_concept"] = "us-gaap:Liabilities"

    # 2. Total Non-Current Assets = Assets - AssetsCurrent
    if not _has_any_local(items, _ASSETS_NONCURRENT_LOCALS):
        assets_idx, assets_item = _find_first_by_locals(items, _ASSETS_ROOT_LOCALS)
        curr_assets_idx, curr_assets_item = _find_first_by_locals(items, _ASSETS_CURRENT_LOCALS)
        if assets_item is not None and curr_assets_item is not None and 0 <= curr_assets_idx < assets_idx:
            assets_val = _to_float(assets_item.get("value"))
            curr_val = _to_float(curr_assets_item.get("value"))
            noncurr_candidates = [
                it for it in items[curr_assets_idx + 1 : assets_idx]
                if not it.get("abstract")
                and _to_float(it.get("value")) is not None
                and _local_name(it.get("concept")) not in _ASSETS_ROOT_LOCALS
            ]
            if assets_val is not None and curr_val is not None and len(noncurr_candidates) >= 2:
                noncurr_val = assets_val - curr_val
                if noncurr_val >= -1.0:
                    nca_comp = _derive_comparatives([assets_item], [curr_assets_item])
                    nca_item = _make_derived_item(
                        concept="us-gaap:AssetsNoncurrent",
                        label="Total Non-Current Assets",
                        value=noncurr_val,
                        template=assets_item,
                        parent_concept=assets_item.get("concept"),
                        level=1,
                        comparative_facts=nca_comp,
                    )
                    assets_c = assets_item.get("concept")
                    for cand in noncurr_candidates:
                        cand_p = cand.get("parent_concept")
                        if not cand_p or cand_p == assets_c:
                            cand["parent_concept"] = "us-gaap:AssetsNoncurrent"
                    items.insert(curr_assets_idx + 1, nca_item)

    # 3. Total Non-Current Liabilities = Liabilities - LiabilitiesCurrent
    if not _has_any_local(items, _LIABILITIES_NONCURRENT_LOCALS):
        liab_idx, liab_item = _find_first_by_locals(items, _LIABILITIES_ROOT_LOCALS)
        curr_liab_idx, curr_liab_item = _find_first_by_locals(items, _LIABILITIES_CURRENT_LOCALS)
        eq_idx, _ = _find_first_by_locals(items, _EQUITY_LOCALS)
        if liab_item is not None and curr_liab_item is not None:
            liab_val = _to_float(liab_item.get("value"))
            curr_liab_val = _to_float(curr_liab_item.get("value"))
            # Non-current liabilities sit after LiabilitiesCurrent and before Equity (or Liabilities if at bottom)
            end_idx = eq_idx if eq_idx > curr_liab_idx else (liab_idx if liab_idx > curr_liab_idx else len(items))
            noncurr_liab_candidates = [
                it for it in items[curr_liab_idx + 1 : end_idx]
                if not it.get("abstract")
                and _to_float(it.get("value")) is not None
                and _local_name(it.get("concept")) not in _LIABILITIES_ROOT_LOCALS
                and _local_name(it.get("concept")) not in _MEZZANINE_EQUITY_LOCALS
                and _local_name(it.get("concept")) not in _EQUITY_LOCALS
            ]
            if liab_val is not None and curr_liab_val is not None and len(noncurr_liab_candidates) >= 2:
                ncl_val = liab_val - curr_liab_val
                if ncl_val >= -1.0:
                    ncl_comp = _derive_comparatives([liab_item], [curr_liab_item])
                    liab_c = liab_item.get("concept")
                    ncl_item = _make_derived_item(
                        concept="us-gaap:LiabilitiesNoncurrent",
                        label="Total Non-Current Liabilities",
                        value=ncl_val,
                        template=liab_item,
                        parent_concept=liab_c,
                        level=2,
                        comparative_facts=ncl_comp,
                    )
                    lse_concepts = {f"us-gaap:{x}" for x in _LSE_ROOT_LOCALS} | {f"ifrs-full:{x}" for x in _LSE_ROOT_LOCALS}
                    for cand in noncurr_liab_candidates:
                        cand_p = cand.get("parent_concept")
                        if not cand_p or cand_p == liab_c or cand_p in lse_concepts:
                            cand["parent_concept"] = "us-gaap:LiabilitiesNoncurrent"
                    items.insert(curr_liab_idx + 1, ncl_item)

    return items


def enrich_statement_with_calculated_concepts(
    flat_data: List[Dict[str, Any]],
    statement_type: str,
) -> List[Dict[str, Any]]:
    """Enrich a statement's flat item list with missing derived concepts.

    Idempotent: if a derived concept (or equivalent local name) is already present,
    it is not duplicated.
    """
    if not flat_data:
        return flat_data

    st = str(statement_type or "").lower()
    if "income" in st:
        enriched = _enrich_income(flat_data)
    elif "balance" in st:
        enriched = _enrich_balancesheet(flat_data)
    else:
        return flat_data

    # Only refresh reading_order if a derived item was actually inserted
    if len(enriched) != len(flat_data):
        for idx, item in enumerate(enriched):
            if isinstance(item, dict):
                item["reading_order"] = idx + 1

    return enriched
