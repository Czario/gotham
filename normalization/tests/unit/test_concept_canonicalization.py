"""Unit tests for cross-era concept canonicalization."""
import pytest

from data_normalization_service.core.concept_canonicalization import (
    canonical_concept,
    is_canonicalized,
)


class TestCanonicalConcept:
    def test_identity_for_unknown_concept(self):
        assert canonical_concept("us-gaap:SomeRandomConcept") == "us-gaap:SomeRandomConcept"

    def test_empty_and_none_safe(self):
        assert canonical_concept("") == ""
        assert canonical_concept(None) is None

    @pytest.mark.parametrize("variant,canonical", [
        ("us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
         "us-gaap:NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
         "us-gaap:NetCashProvidedByUsedInInvestingActivities"),
        ("us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
         "us-gaap:NetCashProvidedByUsedInFinancingActivities"),
        ("us-gaap:Revenues",
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap:SalesRevenueNet",
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease",
         "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect"),
        ("us-gaap:CashAndCashEquivalentsAtCarryingValue",
         "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("us-gaap:IncomeTaxesPaid", "us-gaap:IncomeTaxesPaidNet"),
        ("us-gaap:InterestPaid", "us-gaap:InterestPaidNet"),
        ("us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfDividendsCommonStock"),
    ])
    def test_known_equivalences(self, variant, canonical):
        assert canonical_concept(variant) == canonical

    def test_canonical_maps_to_itself(self):
        # The canonical member of a class is stable under canonicalization.
        canon = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        assert canonical_concept(canon) == canon

    def test_idempotent(self):
        once = canonical_concept("us-gaap:Revenues")
        assert canonical_concept(once) == once


class TestIsCanonicalized:
    def test_variant_is_canonicalized(self):
        assert is_canonicalized("us-gaap:Revenues") is True

    def test_canonical_member_is_not_flagged(self):
        assert is_canonicalized(
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax") is False

    def test_unknown_is_not_flagged(self):
        assert is_canonicalized("us-gaap:Foo") is False
