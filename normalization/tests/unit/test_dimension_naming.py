"""Dimensional concept naming: deterministic, multi-axis preservation without collisions.

Regression tests ensuring multi-dimensional facts (e.g. Segment x Product, Segment x Geography)
deterministically preserve sub-breakdown specificity (such as
``meta:FamilyOfAppsMember_us-gaap:AdvertisingMember``) to prevent value collisions
and row omission across all companies, while remaining 100% stable regardless of
dictionary iteration order.
"""
from __future__ import annotations

from data_normalization_service.services.normalization_service import (
    FinancialNormalizationService,
)


def _service():
    """A service instance without DB wiring — the method under test is pure."""
    return object.__new__(FinancialNormalizationService)


def _dim(axes: dict[str, str]) -> dict:
    """Build ``dimension_data`` with both the flat map and the details map."""
    details = {}
    for axis, member in axes.items():
        details[axis] = {
            "type": "explicit",
            "axis_qname": f"us-gaap:{axis}",
            "member_qname": member,
            "member_local_name": member.split(":")[-1],
            "axis_local_name": axis,
        }
    return {"dimensions": dict(axes), "dimension_details": details}


SEGMENT_AXIS = "StatementBusinessSegmentsAxis"
PRODUCT_AXIS = "ProductOrServiceAxis"
WRAPPER_AXIS = "ConsolidationItemsAxis"


def test_single_axis_keeps_clean_member_name():
    data = _dim({SEGMENT_AXIS: "meta:FamilyOfAppsMember"})
    segment_type, concept = _service()._determine_segment_info(data)
    assert segment_type == "business_segment"
    assert concept == "meta:FamilyOfAppsMember"


def test_multi_axis_preserves_sub_breakdown_composite():
    """Multi-axis facts combine deterministically to prevent collisions."""
    data = _dim(
        {
            SEGMENT_AXIS: "meta:FamilyOfAppsMember",
            PRODUCT_AXIS: "us-gaap:AdvertisingMember",
        }
    )
    segment_type, concept = _service()._determine_segment_info(data)

    assert segment_type == "business_segment"
    assert concept == "meta:FamilyOfAppsMember_us-gaap:AdvertisingMember"


def test_naming_is_independent_of_dict_order():
    """Order-reversed dicts produce identical canonical concept names."""
    axes_a = {
        SEGMENT_AXIS: "meta:FamilyOfAppsMember",
        PRODUCT_AXIS: "us-gaap:AdvertisingMember",
    }
    axes_b = {
        PRODUCT_AXIS: "us-gaap:AdvertisingMember",
        SEGMENT_AXIS: "meta:FamilyOfAppsMember",
    }
    first = _service()._determine_segment_info(_dim(axes_a))
    second = _service()._determine_segment_info(_dim(axes_b))
    assert first == second
    assert first[1] == "meta:FamilyOfAppsMember_us-gaap:AdvertisingMember"

    # Repeated calls are stable too (no set/dict-order dependence).
    assert len({_service()._determine_segment_info(_dim(axes_a)) for _ in range(5)}) == 1


def test_wrapper_axes_do_not_produce_composite_names():
    """Wrapper axes (like ConsolidationItemsAxis) are ignored and do not append."""
    data = _dim(
        {
            SEGMENT_AXIS: "meta:FamilyOfAppsMember",
            WRAPPER_AXIS: "srt:OperatingSegmentsMember",
        }
    )
    segment_type, concept = _service()._determine_segment_info(data)
    assert segment_type == "business_segment"
    assert concept == "meta:FamilyOfAppsMember"
    assert "OperatingSegmentsMember" not in concept


def test_geographic_and_product_deterministic_naming():
    """Product x geography combines deterministically to separate rows."""
    data = _dim(
        {
            "StatementGeographicalAxis": "srt:NorthAmericaMember",
            PRODUCT_AXIS: "gtls:SpecialtyProductsMember",
        }
    )
    segment_type, concept = _service()._determine_segment_info(data)
    assert segment_type == "geographic_segment"
    assert concept == "srt:NorthAmericaMember_gtls:SpecialtyProductsMember"
