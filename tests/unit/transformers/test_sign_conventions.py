"""Unit tests for SignConventionHandler (Option A: SEC Presentation Table)."""
import pytest
from core.transformers.sign_conventions import SignConventionHandler


OPERATING_ASSET_CONCEPTS = [
    "us-gaap:IncreaseDecreaseInAccountsReceivable",
    "us-gaap:IncreaseDecreaseInOtherReceivables",
    "us-gaap:IncreaseDecreaseInInventories",
    "us-gaap:IncreaseDecreaseInOtherOperatingAssets",
    "us-gaap:IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
    "aapl:IncreaseDecreaseInAccountsReceivable",
]


@pytest.mark.parametrize("concept", OPERATING_ASSET_CONCEPTS)
def test_is_cash_flow_operating_asset_change(concept):
    assert SignConventionHandler.is_cash_flow_operating_asset_change(concept) is True


@pytest.mark.parametrize("concept", OPERATING_ASSET_CONCEPTS)
def test_operating_asset_changes_flip_to_cash_flow_impact(concept):
    """An asset decrease (raw XBRL negative) is a cash inflow (shown positive on filing face).
    An asset increase (raw XBRL positive) is a cash outflow (shown negative on filing face)."""
    # Raw XBRL negative (e.g. Apple AR -9,295M) -> positive on face (+9,295M)
    res_pos = SignConventionHandler.apply_sign_conventions(
        value=-9295000000.0,
        concept=concept,
        statement_type="cashflow",
        label="Accounts receivable, net"
    )
    assert res_pos == 9295000000.0

    # Raw XBRL positive (e.g. Apple Inventory +1,084M) -> negative on face (-1,084M)
    res_neg = SignConventionHandler.apply_sign_conventions(
        value=1084000000.0,
        concept=concept,
        statement_type="cashflow",
        label="Inventories"
    )
    assert res_neg == -1084000000.0


def test_balance_sheet_receivables_with_allowance_in_label_stay_positive():
    """Accounts receivable net is an asset (positive), not a contra-account, even
    though its standard label contains 'after Allowance for Credit Loss'."""
    res = SignConventionHandler.apply_sign_conventions(
        value=30339000000.0,
        concept="us-gaap:AccountsReceivableNetCurrent",
        statement_type="balancesheet",
        label="Accounts Receivable, after Allowance for Credit Loss, Current"
    )
    assert res == 30339000000.0


def test_share_based_compensation_is_always_positive_add_back():
    res = SignConventionHandler.apply_sign_conventions(
        value=7122000000.0,
        concept="us-gaap:ShareBasedCompensation",
        statement_type="cashflow",
        label="Share-based compensation expense"
    )
    assert res == 7122000000.0


def test_payments_for_other_investing_is_negative_outflow():
    res = SignConventionHandler.apply_sign_conventions(
        value=1584000000.0,
        concept="us-gaap:PaymentsForProceedsFromOtherInvestingActivities",
        statement_type="cashflow",
        label="Other"
    )
    assert res == -1584000000.0


def test_other_noncash_income_expense_is_negative_deduction():
    """Other non-cash adjustments that subtract from net income are shown in parentheses (negative)."""
    # Apple Q2 6M raw XBRL fact is positive 1,717M with weight -1.0 -> presentation is -1,717M
    res_sub = SignConventionHandler.apply_sign_conventions(
        value=1717000000.0,
        concept="us-gaap:OtherNoncashIncomeExpense",
        statement_type="cashflow",
        calc_weight=-1.0,
        label="Other"
    )
    assert res_sub == -1717000000.0

    # Without explicit weight, default to presentation deduction (-abs(value))
    res_default = SignConventionHandler.apply_sign_conventions(
        value=1717000000.0,
        concept="us-gaap:OtherNoncashIncomeExpense",
        statement_type="cashflow",
        label="Other"
    )
    assert res_default == -1717000000.0

    # If weight is +1.0 (an add-back), it stays positive
    res_add = SignConventionHandler.apply_sign_conventions(
        value=1717000000.0,
        concept="us-gaap:OtherNoncashIncomeExpense",
        statement_type="cashflow",
        calc_weight=1.0,
        label="Other"
    )
    assert res_add == 1717000000.0
