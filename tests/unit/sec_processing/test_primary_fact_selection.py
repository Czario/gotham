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
