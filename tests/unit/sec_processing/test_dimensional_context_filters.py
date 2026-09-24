"""
Unit tests for DimensionalContextFilter keyword matching.

Regression coverage for a substring-matching bug where EXCLUDED_KEYWORDS such as
'change' and 'life' matched inside legitimate dimensional member/axis names
(e.g. 'ForeignExchangeContractMember', 'LifeInsuranceSegmentMember'), silently
dropping valid dimensional facts. Matching is now whole-token / camelCase aware.
"""
import pytest

from core.extractors.dimensional_context_filters import DimensionalContextFilter as F


class TestTokenizer:
    def test_camelcase_split(self):
        assert F._tokenize_qname("us-gaap:ForeignExchangeContractMember") == {
            "us", "gaap", "foreign", "exchange", "contract", "member"
        }

    def test_separators_and_digits(self):
        assert F._tokenize_qname("ba:AirplaneProgram777xMember") >= {
            "airplane", "program", "member"
        }

    def test_empty(self):
        assert F._tokenize_qname("") == set()
        assert F._tokenize_qname(None) == set()


class TestKeywordMatching:
    # Names that must NOT be matched by the KEYWORD matcher (previously
    # false-positives from substrings like 'change'/'life').  Note: some of these
    # (e.g. ForeignExchangeContractMember) ARE excluded, but by the explicit
    # EXCLUDED_MEMBERS set — never by a broad keyword.
    @pytest.mark.parametrize("name", [
        "us-gaap:ForeignExchangeContractMember",
        "us-gaap:ExchangeTradedMember",
        "srt:ForeignExchangeMember",
        "us-gaap:LifeInsuranceSegmentMember",
        "us-gaap:ProductiveLifeMember",
        "ba:InterestRateExchangeAgreementMember",
        "us-gaap:OperatingSegmentsMember",
        "country:US",
    ])
    def test_legitimate_members_not_matched(self, name):
        assert F._matches_excluded_keyword(name) is False

    # Names that SHOULD still be excluded
    @pytest.mark.parametrize("name", [
        "us-gaap:ChangeInAccountingEstimateByTypeMember",
        "srt:ScenarioForecastMember",
        "us-gaap:RestatementMember",
        "us-gaap:ProFormaMember",
        "us-gaap:BudgetMember",
        "us-gaap:ScenarioUnspecifiedMember",
    ])
    def test_unwanted_members_still_matched(self, name):
        assert F._matches_excluded_keyword(name) is True


class TestShouldExcludeMainFact:
    def test_foreign_exchange_main_fact_excluded(self):
        fact = {
            "concept": "us-gaap:DerivativeFairValue",
            "dimensions": [
                {"axis": "us-gaap:DerivativeInstrumentAxis",
                 "member": "us-gaap:ForeignExchangeContractMember",
                 "label": "Foreign Exchange Contract"}
            ],
        }
        assert F.should_exclude_main_fact(fact) is True

    def test_forecast_main_fact_excluded(self):
        fact = {
            "concept": "us-gaap:Revenues",
            "dimensions": [
                {"axis": "srt:StatementScenarioAxis",
                 "member": "srt:ScenarioForecastMember",
                 "label": "Forecast"}
            ],
        }
        assert F.should_exclude_main_fact(fact) is True


class TestShouldExcludeDimensionalFact:
    def test_exchange_dimensional_fact_excluded(self):
        df = {
            "dimensions": {"DerivativeInstrumentAxis": "ForeignExchangeContractMember"},
            "dimension_details": {
                "DerivativeInstrumentAxis": {
                    "member_label": "Foreign Exchange Contract Member"
                }
            },
        }
        assert F.should_exclude_dimensional_fact(df) is True

    def test_interest_rate_contract_member_excluded(self):
        df = {
            "dimensions": {"DerivativeInstrumentAxis": "us-gaap:InterestRateContractMember"},
            "dimension_details": {
                "DerivativeInstrumentAxis": {
                    "member_qname": "us-gaap:InterestRateContractMember",
                    "member_label": "Interest Rate Contract Member",
                }
            },
        }
        assert F.should_exclude_dimensional_fact(df) is True

    def test_aoci_reclassification_member_excluded(self):
        df = {
            "dimensions": {
                "StatementEquityComponentsAxis":
                    "us-gaap:ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember"
            },
        }
        assert F.should_exclude_dimensional_fact(df) is True

    def test_forecast_dimensional_fact_excluded_by_label(self):
        df = {
            "dimensions": {"StatementScenarioAxis": "ScenarioForecastMember"},
            "dimension_details": {
                "StatementScenarioAxis": {"member_label": "Scenario Forecast Member"}
            },
        }
        assert F.should_exclude_dimensional_fact(df) is True
