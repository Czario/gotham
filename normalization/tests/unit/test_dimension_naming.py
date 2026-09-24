"""Dimensional concept naming: clean member names, deterministic, no composites.

Regression tests for the defect that produced concept names such as
``meta:FamilyOfAppsMember_us-gaap:ServiceOtherMember`` in production — member
names concatenated with ``_``, losing the axis each member came from, varying
with dict iteration order, and unable to separate rows that share a member but
hang under different line items.
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


def test_single_axis_keeps_clean_member_name():
    data = _dim({SEGMENT_AXIS: "meta:FamilyOfAppsMember"})
    segment_type, concept = _service()._determine_segment_info(data)
    assert segment_type == "business_segment"
    assert concept == "meta:FamilyOfAppsMember"


def test_multi_axis_does_not_concatenate_members():
    """The core regression: no ``memberA_memberB`` composite is ever produced."""
    data = _dim(
        {
            SEGMENT_AXIS: "meta:FamilyOfAppsMember",
            PRODUCT_AXIS: "us-gaap:AdvertisingMember",
        }
    )
    segment_type, concept = _service()._determine_segment_info(data)

    assert segment_type == "business_segment"
    # Exactly one of the real members, not a mashup of both.
    assert concept in {"meta:FamilyOfAppsMember", "us-gaap:AdvertisingMember"}
    assert concept == "meta:FamilyOfAppsMember"  # the segment axis wins
    assert "_us-gaap:" not in concept
    assert not concept.startswith("custom:_")


def test_naming_is_independent_of_dict_order():
    """Order-reversed twins came from unstable axis iteration."""
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

    # Repeated calls are stable too (no set/dict-order dependence).
    assert len({_service()._determine_segment_info(_dim(axes_a)) for _ in range(5)}) == 1


def test_geographic_and_product_do_not_merge_into_one_name():
    """Product x geography must stay one member, not ``Product_Geography``."""
    data = _dim(
        {
            "StatementGeographicalAxis": "srt:NorthAmericaMember",
            PRODUCT_AXIS: "gtls:SpecialtyProductsMember",
        }
    )
    _, concept = _service()._determine_segment_info(data)
    assert concept in {"srt:NorthAmericaMember", "gtls:SpecialtyProductsMember"}
    assert "_gtls:" not in concept and "_srt:" not in concept
