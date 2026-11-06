"""Primary fact selection — period tolerance + consolidated-value fallback.

Regression for AAPL: a 14-week quarter (98 days ≈ 3.22 months) was rejected by
the old 0.05-month quarterly tolerance, so ``_select_best_primary_fact`` fell
back to ``facts[0]`` — the dimensional "Products" revenue (96.4B) rather than
total net sales (117.2B).  That both stored the wrong value and made the
gross-profit identity fail (blocking the filing).
"""
from __future__ import annotations

from datetime import datetime

from core.extractors.xbrl_parser import FlexibleXBRLExtractor
from utilities.helpers.period_utils import PeriodMatcher


class _Ctx:
    def __init__(self, start, end, dims=None, instant=False):
        self.isInstantPeriod = instant
        self.isStartEndPeriod = not instant
        self.startDatetime = start
        self.endDatetime = end
        self.instantDatetime = end
        self.qnameDims = dims or {}
        self.period = True  # presence marker checked by the extractor


class _Fact:
    def __init__(self, value, ctx, qname="us-gaap:Revenues", cid="c1"):
        self.effectiveValue = value
        self.xValue = value
        self.context = ctx
        self.contextID = cid
        self.qname = qname


def _extractor():
    ex = object.__new__(FlexibleXBRLExtractor)
    ex.company_info = {"fiscal_year": 2023, "fiscal_year_end_code": "0930"}
    ex.target_form_type = "10-Q"
    ex.current_statement_type = "income_statement"
    return ex


def test_fourteen_week_quarter_is_accepted():
    # 98 days / 30.4 = 3.224 months
    assert PeriodMatcher.is_strictly_quarterly_period(3.224) is True
    assert PeriodMatcher.reject_non_quarterly_period(3.224, "10-Q") is False


def test_cumulative_periods_are_still_rejected():
    assert PeriodMatcher.reject_non_quarterly_period(6.0, "10-Q") is True
    assert PeriodMatcher.reject_non_quarterly_period(9.0, "10-Q") is True


def test_consolidated_total_beats_dimensional_member():
    total_ctx = _Ctx(datetime(2022, 9, 25), datetime(2022, 12, 31))
    products_ctx = _Ctx(
        datetime(2022, 9, 25), datetime(2022, 12, 31),
        dims={"srt:ProductOrServiceAxis": object()},
    )
    products = _Fact(96_388_000_000, products_ctx)
    total = _Fact(117_154_000_000, total_ctx)

    best = _extractor()._select_best_primary_fact([products, total])
    assert best is total


def test_20f_prefers_annual_fact_over_same_end_date_quarter():
    annual_ctx = _Ctx(datetime(2025, 1, 1), datetime(2025, 12, 31))
    q4_ctx = _Ctx(datetime(2025, 10, 1), datetime(2025, 12, 31))
    annual = _Fact(1_000, annual_ctx)
    q4 = _Fact(250, q4_ctx)

    extractor = _extractor()
    extractor.target_form_type = "20-F"
    extractor.filing_form_type = "20-F"

    assert extractor._select_best_primary_fact([q4, annual]) is annual


def test_20f_primary_period_filter_keeps_annual_duration():
    annual_ctx = _Ctx(datetime(2025, 1, 1), datetime(2025, 12, 31))
    q4_ctx = _Ctx(datetime(2025, 10, 1), datetime(2025, 12, 31))
    annual = _Fact(1_000, annual_ctx)
    q4 = _Fact(250, q4_ctx)

    extractor = object.__new__(FlexibleXBRLExtractor)
    extractor.filing_form_type = "20-F"
    extractor.is_older_filing = False

    assert extractor._filter_facts_to_primary_period([annual, q4], None) == [annual]
    assert extractor._is_primary_period("2025-01-01 to 2025-12-31")
    assert not extractor._is_primary_period("2025-10-01 to 2025-12-31")


def test_fallback_prefers_undimensioned_fact_when_periods_rejected():
    # Both are 6-month periods → all rejected by the quarterly filter, forcing
    # the fallback path.
    total_ctx = _Ctx(datetime(2022, 7, 1), datetime(2022, 12, 31))
    products_ctx = _Ctx(
        datetime(2022, 7, 1), datetime(2022, 12, 31),
        dims={"srt:ProductOrServiceAxis": object()},
    )
    products = _Fact(96_388_000_000, products_ctx)
    total = _Fact(117_154_000_000, total_ctx)

    best = _extractor()._select_best_primary_fact([products, total])
    assert best is total


def test_cash_flow_q2_prefers_6m_fact_over_3m_income_fact():
    # In Q2 10-Q, NetIncomeLoss appears as both a 3-month fact (income statement)
    # and a 6-month fact (cash flow statement). Cash flow must select the 6-month fact.
    q2_3m_ctx = _Ctx(datetime(2025, 12, 28), datetime(2026, 3, 29))  # ~3 months
    q2_6m_ctx = _Ctx(datetime(2025, 9, 28), datetime(2026, 3, 29))   # ~6 months
    fact_3m = _Fact(29_578_000_000, q2_3m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_inc")
    fact_6m = _Fact(71_675_000_000, q2_6m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_cf")

    ex = _extractor()
    ex.company_info = {"fiscal_year": 2026, "fiscal_quarter": 2, "fiscal_year_end_code": "0930"}
    ex.current_statement_type = "cash_flow"

    best = ex._select_best_primary_fact([fact_3m, fact_6m])
    assert best is fact_6m


def test_cash_flow_q3_prefers_9m_fact_over_3m_and_6m_facts():
    # In Q3 10-Q, cash flow must select the 9-month cumulative fact.
    q3_3m_ctx = _Ctx(datetime(2026, 3, 29), datetime(2026, 6, 28))   # ~3 months
    q3_9m_ctx = _Ctx(datetime(2025, 9, 28), datetime(2026, 6, 28))   # ~9 months
    fact_3m = _Fact(29_789_000_000, q3_3m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_inc")
    fact_9m = _Fact(101_464_000_000, q3_9m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_cf")

    ex = _extractor()
    ex.company_info = {"fiscal_year": 2026, "fiscal_quarter": 3, "fiscal_year_end_code": "0930"}
    ex.current_statement_type = "cash_flow"

    best = ex._select_best_primary_fact([fact_3m, fact_9m])
    assert best is fact_9m


def test_income_statement_q2_prefers_3m_fact_over_6m_fact():
    # In Q2 10-Q, income statement must select the strictly quarterly (3m) fact.
    q2_3m_ctx = _Ctx(datetime(2025, 12, 28), datetime(2026, 3, 29))  # ~3 months
    q2_6m_ctx = _Ctx(datetime(2025, 9, 28), datetime(2026, 3, 29))   # ~6 months
    fact_3m = _Fact(29_578_000_000, q2_3m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_inc")
    fact_6m = _Fact(71_675_000_000, q2_6m_ctx, qname="us-gaap:NetIncomeLoss", cid="c_cf")

    ex = _extractor()
    ex.company_info = {"fiscal_year": 2026, "fiscal_quarter": 2, "fiscal_year_end_code": "0930"}
    ex.current_statement_type = "income_statement"

    best = ex._select_best_primary_fact([fact_6m, fact_3m])
    assert best is fact_3m
