"""Tests for derived accounting-identity concepts and child hierarchy enrichment."""
from core.processors.derived_concepts import enrich_statement_with_calculated_concepts


def test_income_statement_derives_gross_profit_without_single_child_opex_header():
    flat = [
        {
            "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "label": "Sales",
            "value": 6010.48,
            "abstract": False,
            "parent_concept": None,
            "comparative_facts": [{"period": "2025-05-03", "value": 4984.97}],
        },
        {
            "concept": "us-gaap:CostOfGoodsAndServicesSold",
            "label": "Cost of goods sold",
            "value": 4230.59,
            "abstract": False,
            "parent_concept": None,
            "comparative_facts": [{"period": "2025-05-03", "value": 3581.37}],
        },
        {
            "concept": "us-gaap:SellingGeneralAndAdministrativeExpense",
            "label": "Selling, general and administrative",
            "value": 975.86,
            "abstract": False,
            "parent_concept": None,
            "comparative_facts": [{"period": "2025-05-03", "value": 797.14}],
        },
        {
            "concept": "us-gaap:OperatingIncomeLoss",
            "label": "Operating income",
            "value": 804.03,
            "abstract": False,
            "parent_concept": None,
        },
    ]

    enriched = enrich_statement_with_calculated_concepts(flat, "income")
    by_c = {it["concept"]: it for it in enriched}

    assert "us-gaap:GrossProfit" in by_c
    gp = by_c["us-gaap:GrossProfit"]
    assert gp["calculated"] is True
    assert round(gp["value"], 2) == 1779.89
    assert gp["comparative_facts"] == [{"period": "2025-05-03", "value": 1403.6}]

    # Single OpEx child (SG&A alone) should stay at root without a redundant OperatingExpenses header
    assert "us-gaap:OperatingExpenses" not in by_c
    assert by_c["us-gaap:SellingGeneralAndAdministrativeExpense"]["parent_concept"] is None

    # Idempotent: second call does not duplicate
    enriched2 = enrich_statement_with_calculated_concepts(enriched, "income")
    assert len(enriched2) == len(enriched)


def test_income_statement_derives_operating_expenses_for_multiple_children():
    flat = [
        {"concept": "us-gaap:Revenues", "label": "Sales", "value": 1000.0, "abstract": False, "parent_concept": None},
        {"concept": "us-gaap:CostOfRevenue", "label": "COGS", "value": 600.0, "abstract": False, "parent_concept": None},
        {"concept": "us-gaap:SellingGeneralAndAdministrativeExpense", "label": "SG&A", "value": 150.0, "abstract": False, "parent_concept": None},
        {"concept": "us-gaap:ResearchAndDevelopmentExpense", "label": "R&D", "value": 50.0, "abstract": False, "parent_concept": None},
        {"concept": "us-gaap:OperatingIncomeLoss", "label": "Operating income", "value": 200.0, "abstract": False, "parent_concept": None},
    ]
    enriched = enrich_statement_with_calculated_concepts(flat, "income")
    by_c = {it["concept"]: it for it in enriched}
    assert "us-gaap:OperatingExpenses" in by_c
    assert by_c["us-gaap:OperatingExpenses"]["calculated"] is True
    assert by_c["us-gaap:OperatingExpenses"]["value"] == 200.0
    assert by_c["us-gaap:SellingGeneralAndAdministrativeExpense"]["parent_concept"] == "us-gaap:OperatingExpenses"
    assert by_c["us-gaap:ResearchAndDevelopmentExpense"]["parent_concept"] == "us-gaap:OperatingExpenses"


def test_balance_sheet_derives_liabilities_and_noncurrent_subtotals():
    flat = [
        {"concept": "us-gaap:AssetsCurrent", "label": "Total current assets", "value": 6000.0, "parent_concept": "us-gaap:Assets"},
        {"concept": "us-gaap:PropertyPlantAndEquipmentNet", "label": "PP&E", "value": 3000.0, "parent_concept": "us-gaap:Assets"},
        {"concept": "us-gaap:OperatingLeaseRightOfUseAsset", "label": "ROU Assets", "value": 1000.0, "parent_concept": "us-gaap:Assets"},
        {"concept": "us-gaap:Assets", "label": "Total assets", "value": 10000.0, "parent_concept": None},
        {"concept": "us-gaap:LiabilitiesCurrent", "label": "Total current liabilities", "value": 2500.0, "parent_concept": "us-gaap:LiabilitiesAndStockholdersEquity"},
        {"concept": "us-gaap:LongTermDebtNoncurrent", "label": "Long-term debt", "value": 2000.0, "parent_concept": "us-gaap:LiabilitiesAndStockholdersEquity"},
        {"concept": "us-gaap:OperatingLeaseLiabilityNoncurrent", "label": "Non-current lease", "value": 1500.0, "parent_concept": "us-gaap:LiabilitiesAndStockholdersEquity"},
        {"concept": "us-gaap:StockholdersEquity", "label": "Stockholders' equity", "value": 4000.0, "parent_concept": "us-gaap:LiabilitiesAndStockholdersEquity"},
        {"concept": "us-gaap:LiabilitiesAndStockholdersEquity", "label": "Total liabilities and equity", "value": 10000.0, "parent_concept": None},
    ]

    enriched = enrich_statement_with_calculated_concepts(flat, "balancesheet")
    by_c = {it["concept"]: it for it in enriched}

    assert by_c["us-gaap:Liabilities"]["calculated"] is True
    assert by_c["us-gaap:Liabilities"]["value"] == 6000.0
    assert by_c["us-gaap:Liabilities"]["parent_concept"] == "us-gaap:LiabilitiesAndStockholdersEquity"

    assert by_c["us-gaap:AssetsNoncurrent"]["calculated"] is True
    assert by_c["us-gaap:AssetsNoncurrent"]["value"] == 4000.0
    assert by_c["us-gaap:AssetsNoncurrent"]["parent_concept"] == "us-gaap:Assets"
    assert by_c["us-gaap:PropertyPlantAndEquipmentNet"]["parent_concept"] == "us-gaap:AssetsNoncurrent"
    assert by_c["us-gaap:OperatingLeaseRightOfUseAsset"]["parent_concept"] == "us-gaap:AssetsNoncurrent"

    assert by_c["us-gaap:LiabilitiesNoncurrent"]["calculated"] is True
    assert by_c["us-gaap:LiabilitiesNoncurrent"]["value"] == 3500.0
    assert by_c["us-gaap:LiabilitiesNoncurrent"]["parent_concept"] == "us-gaap:Liabilities"
    assert by_c["us-gaap:LiabilitiesCurrent"]["parent_concept"] == "us-gaap:Liabilities"
    assert by_c["us-gaap:LongTermDebtNoncurrent"]["parent_concept"] == "us-gaap:LiabilitiesNoncurrent"
    assert by_c["us-gaap:OperatingLeaseLiabilityNoncurrent"]["parent_concept"] == "us-gaap:LiabilitiesNoncurrent"
