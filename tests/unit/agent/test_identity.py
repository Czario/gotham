"""Row identity: parent scoping, dimension signatures, canonical keys.

Every test here encodes a production data defect that the identity module
exists to prevent.  Real CIK/fact pairs are cited inline.
"""
from __future__ import annotations

import pytest

from filings_agent.hierarchy.identity import (
    dimension_signature,
    is_dimensional,
    parent_anchor,
    parent_scoped_concept,
    row_key,
    row_key_str,
    same_parent,
)


# ── dimension_signature ──────────────────────────────────────────────────────


def test_non_dimensional_row_has_empty_signature():
    assert dimension_signature(None, None) == ""
    assert dimension_signature({}, {}) == ""
    # An axis that carries no member contributes nothing.
    assert dimension_signature({"us-gaap:XAxis": None}, None) == ""


def test_signature_is_order_independent():
    """The defect that produced 435 order-reversed twin concepts in production.

    `srt:NorthAmericaMember_gtls:SpecialtyProductsMember` and
    `gtls:SpecialtyProductsMember_srt:NorthAmericaMember` were both stored for
    CIK 0000892553. The signature must collapse those to one value.
    """
    a = {"us-gaap:StatementBusinessSegmentsAxis": "gtls:SpecialtyProductsMember",
         "srt:StatementGeographicalAxis": "srt:NorthAmericaMember"}
    b = {"srt:StatementGeographicalAxis": "srt:NorthAmericaMember",
         "us-gaap:StatementBusinessSegmentsAxis": "gtls:SpecialtyProductsMember"}
    assert dimension_signature(a) == dimension_signature(b)
    assert dimension_signature(a) != ""


def test_signature_recovers_member_from_dimension_details():
    """Flat maps often carry only the local name; details carry the namespace."""
    flat = {"StatementBusinessSegmentsAxis": "FamilyOfAppsMember"}
    details = {
        "StatementBusinessSegmentsAxis": {
            "member_qname": "meta:FamilyOfAppsMember",
            "member_local_name": "FamilyOfAppsMember",
        }
    }
    assert dimension_signature(flat, details) == "statementbusinesssegments=meta:familyofappsmember"


def test_wrapper_axes_and_explicitmember_are_ignored():
    """`explicitMember` is an XBRL-DI internal, not a dimension."""
    with_wrapper = {
        "us-gaap:StatementBusinessSegmentsAxis": "meta:FamilyOfAppsMember",
        "srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember",
        "explicitMember": "meta:FamilyOfAppsMember",
    }
    without = {"us-gaap:StatementBusinessSegmentsAxis": "meta:FamilyOfAppsMember"}
    assert dimension_signature(with_wrapper) == dimension_signature(without)


# ── parent_anchor / same_parent ──────────────────────────────────────────────


def test_parent_anchor_prefers_concept_id_over_name():
    row = {"concept_id": "6ab25da82b4985af5465585c", "concept_name": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"}
    parent_id, parent_concept = parent_anchor(row)
    assert parent_id == "6ab25da82b4985af5465585c"
    assert parent_concept == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


def test_parent_anchor_absent_for_root_row():
    assert parent_anchor({"concept": "us-gaap:Revenues"}) == (None, None)


def test_same_parent_uses_id_when_available():
    left = {"concept_id": "AAA", "concept_name": "us-gaap:Revenues"}
    right = {"concept_id": "AAA", "concept_name": "us-gaap:Revenues"}
    other = {"concept_id": "BBB", "concept_name": "us-gaap:CostOfRevenue"}
    assert same_parent(left, right)
    assert not same_parent(left, other)


def test_same_parent_falls_back_to_name():
    left = {"concept_name": "us-gaap:Revenues"}
    right = {"concept_name": "us-gaap:Revenues"}
    other = {"concept_name": "us-gaap:CostOfRevenue"}
    assert same_parent(left, right)
    assert not same_parent(left, other)


# ── the Revenue / CostOfRevenue collision (CIK 0000882184) ───────────────────


def _dhi(member, parent_name, parent_id):
    return {
        "concept": member,
        "concept_name": parent_name,
        "concept_id": parent_id,
        "dimension_concept": True,
        "dimensions": {"StatementBusinessSegmentsAxis": member},
    }


def test_shared_member_under_different_parents_is_never_the_same_row():
    """`dhi:HomeBuildingOpsMember` is a child of BOTH Revenues and CostOfRevenue.

    Merging or de-duplicating these would fuse two different economic facts.
    """
    revenue = _dhi("dhi:HomeBuildingOpsMember", "us-gaap:Revenues", "R1")
    cost = _dhi("dhi:HomeBuildingOpsMember", "us-gaap:CostOfRevenue", "C1")

    assert revenue["concept"] == cost["concept"]          # same name
    assert not same_parent(revenue, cost)                 # different parent
    assert row_key(revenue) != row_key(cost)              # different row

    # ...and the alias key is parent-scoped, so no merge can bridge them.
    assert parent_scoped_concept(revenue) == ("us-gaap:Revenues", "dhi:HomeBuildingOpsMember")
    assert parent_scoped_concept(cost) == ("us-gaap:CostOfRevenue", "dhi:HomeBuildingOpsMember")
    assert parent_scoped_concept(revenue) != parent_scoped_concept(cost)


def test_shared_concept_without_parent_ids_still_separates_by_name():
    revenue = {"concept": "duot:TechnologySolutionsMember", "concept_name": "us-gaap:Revenues"}
    cost = {"concept": "duot:TechnologySolutionsMember", "concept_name": "us-gaap:CostOfRevenue"}
    assert not same_parent(revenue, cost)
    assert row_key(revenue) != row_key(cost)


# ── row_key ──────────────────────────────────────────────────────────────────


def test_row_key_includes_period_and_parent():
    row = _dhi("meta:FamilyOfAppsMember", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "P1")
    k1 = row_key(row, cik="0001326801", form_type="10-Q", statement_type="income",
                 period="2026-04-01 to 2026-07-01")
    k2 = row_key(row, cik="0001326801", form_type="10-Q", statement_type="income",
                 period="2025-04-01 to 2025-07-01")
    assert k1 != k2, "a new period is a new row (time series must keep growing)"
    assert k1[4] == "P1"


def test_row_key_is_stable_for_the_same_row():
    row = _dhi("meta:FamilyOfAppsMember", "us-gaap:Revenues", "P1")
    row["dimensions"] = {
        "us-gaap:StatementBusinessSegmentsAxis": "meta:FamilyOfAppsMember",
        "srt:StatementGeographicalAxis": "srt:NorthAmericaMember",
    }
    same = dict(row, dimensions={
        "srt:StatementGeographicalAxis": "srt:NorthAmericaMember",
        "us-gaap:StatementBusinessSegmentsAxis": "meta:FamilyOfAppsMember",
    })
    assert row_key_str(row) == row_key_str(same)


def test_degenerate_axis_names_stay_distinct():
    """An axis whose local name is bare ``Axis`` must not collapse with another."""
    assert dimension_signature({"a:Axis": "m1", "b:Axis": "m2"}) == "aaxis=m1|baxis=m2"
    assert dimension_signature({"a:Axis": "m1"}) != dimension_signature({"b:Axis": "m1"})


def test_row_key_distinguishes_sibling_members_under_one_parent():
    """META revenue carries FoA and Reality Labs plus advertising/other splits."""
    parent = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    foa = _dhi("meta:FamilyOfAppsMember", parent, "P1")
    rl = _dhi("meta:RealityLabsMember", parent, "P1")
    adv = _dhi("us-gaap:AdvertisingMember", parent, "P1")
    keys = {row_key_str(r) for r in (foa, rl, adv)}
    assert len(keys) == 3


def test_row_key_separates_line_items_with_no_parent_or_dimensions():
    """Regression: an empty parent + empty signature must not collapse rows.

    Without a node component every line item in a statement+period shared one
    key (``cik|form|stmt|period||``), which would make a unique index impossible
    and merge unrelated line items.
    """
    base = {"cik": "1", "form_type": "10-Q", "statement_type": "income", "period": "P1"}
    revenue = row_key_str(dict(base, concept="us-gaap:Revenues"))
    cost = row_key_str(dict(base, concept="us-gaap:CostOfRevenue"))
    assert revenue != cost
    assert revenue.endswith("us-gaap:Revenues")


def test_dimensional_row_identity_ignores_member_rename():
    """A dimensional node is keyed by its slice, so a rename keeps the identity."""
    base = {
        "cik": "1", "form_type": "10-Q", "statement_type": "income", "period": "P1",
        "concept_id": "p",
        "dimensions": {"us-gaap:StatementBusinessSegmentsAxis": "meta:FamilyOfAppsMember"},
    }
    first = row_key_str(dict(base, concept="meta:FamilyOfAppsMember"))
    renamed = row_key_str(dict(base, concept="meta:FamilyOfAppsMember"))
    other_slice = row_key_str(dict(
        base,
        concept="meta:RealityLabsMember",
        dimensions={"us-gaap:StatementBusinessSegmentsAxis": "meta:RealityLabsMember"},
    ))
    assert first == renamed
    assert first != other_slice


def test_dimensional_row_without_metadata_falls_back_to_member_name():
    """Legacy rows have no dimensions map; the member name must carry identity."""
    base = {"cik": "1", "form_type": "10-Q", "statement_type": "income",
            "period": "P1", "concept_id": "p", "concept_name": "us-gaap:Revenues"}
    a = row_key_str(dict(base, concept="meta:FamilyOfAppsMember"))
    b = row_key_str(dict(base, concept="meta:RealityLabsMember"))
    assert a != b


def test_is_dimensional():
    assert is_dimensional(_dhi("meta:FamilyOfAppsMember", "us-gaap:Revenues", "P1"))
    assert not is_dimensional({"concept": "us-gaap:Revenues"})
    # signature alone is enough to mark a row dimensional
    assert is_dimensional({"concept": "x", "dimensions": {"a:Axis": "m"}})
