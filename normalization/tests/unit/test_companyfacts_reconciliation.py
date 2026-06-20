"""Unit tests for the SEC companyfacts reconciliation matching logic.

Focuses on the pure period/duration matching that carries the risk of inserting
a wrong-duration value (e.g. a YTD figure into an annual slot). Network and DB
access are not exercised here.
"""
from datetime import datetime

import pytest
from bson import ObjectId

from data_normalization_service.services.companyfacts_reconciliation import (
    CompanyFactsReconciliationService,
)


@pytest.fixture
def svc():
    return CompanyFactsReconciliationService(target_db=None, sec_client=None)


class _FakeColl:
    """Minimal in-memory stand-in for a pymongo collection."""

    def __init__(self, docs):
        self._docs = docs

    def find(self, query=None, projection=None):
        query = query or {}
        out = []
        for d in self._docs:
            if "company_cik" in query and d.get("company_cik") != query["company_cik"]:
                continue
            cid_q = query.get("concept_id")
            if isinstance(cid_q, dict) and "$in" in cid_q and d.get("concept_id") not in cid_q["$in"]:
                continue
            if "value" in query and d.get("value") is None:
                continue
            out.append(d)
        return out


class _FakeDB:
    def __init__(self, mapping):
        self._mapping = mapping

    def __getitem__(self, name):
        return self._mapping[name]


def _facts(tag, units):
    return {"facts": {"us-gaap": {tag: {"units": units}}}}


class TestItemPeriodDays:
    def test_parses_annual(self, svc):
        assert svc._item_period_days(
            "2014-09-28 00:00:00 to 2015-09-27 00:00:00") == 364

    def test_parses_quarter(self, svc):
        assert svc._item_period_days(
            "2015-09-27 00:00:00 to 2015-12-27 00:00:00") == 91

    def test_bad_input(self, svc):
        assert svc._item_period_days("garbage") is None
        assert svc._item_period_days("") is None


class TestDurationOk:
    def test_annual_window(self, svc):
        assert svc._duration_ok("2014-09-28", "2015-09-27", "annual") is True
        assert svc._duration_ok("2015-06-27", "2015-09-27", "annual") is False

    def test_quarterly_window(self, svc):
        assert svc._duration_ok("2015-06-27", "2015-09-27", "quarterly") is True
        assert svc._duration_ok("2014-09-28", "2015-09-27", "quarterly") is False

    def test_expected_overrides_frequency(self, svc):
        # YTD 9-month value: outside the quarterly window but matches expected.
        assert svc._duration_ok("2015-01-01", "2015-09-30", "quarterly",
                                expected=272) is True
        assert svc._duration_ok("2015-07-01", "2015-09-30", "quarterly",
                                expected=272) is False


class TestMatchFact:
    TAGS = ("us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")

    def _gap(self, **kw):
        g = {"statement_type": "cash_flows", "canonical": self.TAGS[0],
             "period_end": None, "period_str": "2015-09-27",
             "is_instant": False, "expected_duration": None}
        g.update(kw)
        return g

    def test_matches_annual_duration(self, svc):
        facts = _facts("NetCashProvidedByUsedInOperatingActivities", {
            "USD": [{"start": "2014-09-28", "end": "2015-09-27", "val": 81266000000,
                     "form": "10-K", "accn": "x", "fy": 2015, "frame": "CY2015"}]
        })
        m = svc._match_fact(facts, self.TAGS, self._gap(), "annual")
        assert m is not None and m["value"] == 81266000000

    def test_rejects_wrong_duration(self, svc):
        # Only a 3-month fact exists; annual gap must not match it.
        facts = _facts("NetCashProvidedByUsedInOperatingActivities", {
            "USD": [{"start": "2015-06-27", "end": "2015-09-27", "val": 20000000000,
                     "form": "10-Q", "accn": "x"}]
        })
        assert svc._match_fact(facts, self.TAGS, self._gap(), "annual") is None

    def test_finds_value_under_sibling_tag(self, svc):
        facts = _facts("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", {
            "USD": [{"start": "2014-09-28", "end": "2015-09-27", "val": 81266000000,
                     "form": "10-K", "accn": "x", "frame": None}]
        })
        m = svc._match_fact(facts, self.TAGS, self._gap(), "annual")
        assert m is not None
        assert m["short_tag"] == "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"

    def test_instant_gap_rejects_duration_fact(self, svc):
        facts = _facts("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", {
            "USD": [{"start": "2014-09-28", "end": "2015-09-27", "val": 1, "form": "10-K"}]
        })
        gap = self._gap(canonical="us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                        is_instant=True)
        tags = ("us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",)
        assert svc._match_fact(facts, tags, gap, "annual") is None

    def test_instant_gap_matches_instant_fact(self, svc):
        facts = _facts("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", {
            "USD": [{"end": "2015-09-27", "val": 21120000000, "form": "10-K"}]
        })
        gap = self._gap(canonical="us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                        is_instant=True)
        tags = ("us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",)
        m = svc._match_fact(facts, tags, gap, "annual")
        assert m is not None and m["value"] == 21120000000

    def test_prefers_form_match(self, svc):
        facts = _facts("NetCashProvidedByUsedInOperatingActivities", {
            "USD": [
                {"start": "2014-09-28", "end": "2015-09-27", "val": 1,
                 "form": "10-Q", "frame": None},
                {"start": "2014-09-28", "end": "2015-09-27", "val": 2,
                 "form": "10-K", "frame": "CY2015"},
            ]
        })
        m = svc._match_fact(facts, self.TAGS, self._gap(), "annual")
        assert m["value"] == 2  # the 10-K fact wins

    def test_no_match_returns_none(self, svc):
        facts = _facts("SomethingElse", {"USD": [
            {"start": "2014-09-28", "end": "2015-09-27", "val": 1, "form": "10-K"}]})
        assert svc._match_fact(facts, self.TAGS, self._gap(), "annual") is None


class TestFindGaps:
    """Edge-mode vs interpolation-only gap detection."""

    CIK = "0000000001"

    def _build(self):
        rev_id, cost_id = ObjectId(), ObjectId()
        concepts = [
            {"_id": rev_id, "company_cik": self.CIK, "statement_type": "income_statement",
             "concept": "Revenues", "canonical_concept": "Revenues",
             "abstract": False, "dimension": False},
            {"_id": cost_id, "company_cik": self.CIK, "statement_type": "income_statement",
             "concept": "CostOfRevenue", "canonical_concept": "CostOfRevenue",
             "abstract": False, "dimension": False},
        ]

        def val(cid, year):
            end = datetime(year, 12, 31)
            return {"concept_id": cid, "value": 1.0, "reporting_period": {
                "end_date": end,
                "item_period": f"{year}-01-01 00:00:00 to {year}-12-31 00:00:00"}}

        # Revenue filled 2012,2013,2014; CostOfRevenue filled only 2013.
        values = [val(rev_id, y) for y in (2012, 2013, 2014)] + [val(cost_id, 2013)]
        db = _FakeDB({"normalized_concepts_annual": _FakeColl(concepts),
                      "concept_values_annual": _FakeColl(values)})
        return CompanyFactsReconciliationService(db, sec_client=None)

    def test_interpolation_only_skips_edges(self):
        svc = self._build()
        gaps, _ = svc._find_interpolation_gaps(
            self.CIK, "normalized_concepts_annual", "concept_values_annual",
            include_edge=False)
        # CostOfRevenue's only value is 2013, so 2012/2014 are edges -> skipped.
        assert gaps == []

    def test_edge_mode_includes_trailing_and_leading(self):
        svc = self._build()
        gaps, _ = svc._find_interpolation_gaps(
            self.CIK, "normalized_concepts_annual", "concept_values_annual",
            include_edge=True)
        filled = {(g["canonical"], g["period_str"]) for g in gaps}
        assert filled == {("CostOfRevenue", "2012-12-31"),
                          ("CostOfRevenue", "2014-12-31")}
