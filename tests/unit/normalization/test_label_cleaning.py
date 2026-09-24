"""Label cleaning: XBRL presentation tags must never reach the database.

Regression tests for dirty labels such as ``"Rest Of World [Member]"`` and
``"Rest of Asia Pacific Segment [Member]"`` that were persisted verbatim from
the taxonomy label linkbase.
"""
from __future__ import annotations

from types import SimpleNamespace

from bson import ObjectId

from data_normalization_service.services.normalization_service import (
    FinancialNormalizationService,
)
from data_normalization_service.utils.hierarchy import HierarchyManager
from data_normalization_service.utils.label_cleaning import (
    clean_label,
    clean_labels_in_item,
)


# ── clean_label ─────────────────────────────────────────────────────────────

def test_removes_member_tag():
    assert clean_label("Rest Of World [Member]") == "Rest Of World"


def test_removes_segment_word_before_member_tag():
    assert clean_label("Rest of Asia Pacific Segment [Member]") == "Rest of Asia Pacific"


def test_removes_plural_segment_word_before_member_tag():
    assert clean_label("All Other Segments [Member]") == "All Other"


def test_keeps_generic_segment_label_intact():
    # "Operating Segments [Member]" is a meaningful label; it must not collapse
    # to the single generic word "Operating".
    assert clean_label("Operating Segments [Member]") == "Operating Segments"
    assert clean_label("Reportable Segments [Member]") == "Reportable Segments"


def test_removes_other_xbrl_tags():
    assert clean_label("Revenue [Text Block]") == "Revenue"
    assert clean_label("Statement Business Segments [Axis]") == "Statement Business Segments"
    assert clean_label("Rest Of World [Domain]") == "Rest Of World"


def test_handles_none_and_empty():
    assert clean_label(None) is None
    assert clean_label("") == ""
    assert clean_label("   ") == ""


def test_is_idempotent():
    for raw in (
        "Rest Of World [Member]",
        "Rest of Asia Pacific Segment [Member]",
        "Operating Segments [Member]",
        "Revenue [Text Block]",
    ):
        once = clean_label(raw)
        assert clean_label(once) == once


def test_unknown_bracket_content_is_preserved():
    # Bracketed content that is not a known XBRL tag is genuine data.
    assert clean_label("Revenue (2024) [unaudited]") == "Revenue (2024) [unaudited]"


def test_collapses_whitespace():
    assert clean_label("Rest   of\nAsia Pacific  Segment [Member]") == "Rest of Asia Pacific"


# ── clean_labels_in_item ────────────────────────────────────────────────────

def test_cleans_nested_member_label():
    item = {
        "label": "Revenues [Member]",
        "fact_label": "Revenues [Text Block]",
        "dimension_details": {
            "StatementGeographicalAxis": {
                "member_label": "Rest of Asia Pacific Segment [Member]",
                "member_local_name": "RestOfAsiaPacificSegmentMember",
            }
        },
    }
    clean_labels_in_item(item)
    assert item["label"] == "Revenues"
    assert item["fact_label"] == "Revenues"
    assert (
        item["dimension_details"]["StatementGeographicalAxis"]["member_label"]
        == "Rest of Asia Pacific"
    )


# ── bundle construction ─────────────────────────────────────────────────────

def _service() -> FinancialNormalizationService:
    service = object.__new__(FinancialNormalizationService)
    service.hierarchy_manager = HierarchyManager()
    return service


def _statement(financial_data):
    return SimpleNamespace(
        company_cik="0000320193",
        statement_type="income",
        reporting_period={"fiscal_year": 2025, "quarter": 3},
        created_at=None,
        id=ObjectId(),
        financial_data=financial_data,
    )


def _filing():
    return SimpleNamespace(
        id=ObjectId(), form_type="10-Q", accession_number="0000320193-25-000001"
    )


def test_build_bundle_cleans_line_item_and_dimensional_labels():
    financial_data = [
        {
            "concept": "us-gaap:Revenues",
            "label": "Revenues [Member]",
            "value": 100.0,
            "period": "2025-01-01 00:00:00 to 2025-03-31 00:00:00",
            "level": 1,
            "order": 0,
            "abstract": False,
            "dimensional_facts": [
                {
                    "concept_name": "us-gaap:Revenues",
                    "value": 40.0,
                    "period": "2025-01-01 00:00:00 to 2025-03-31 00:00:00",
                    "fact_label": "Revenues",
                    "dimensions": {"StatementGeographicalAxis": "country:RestOfWorldMember"},
                    "dimension_details": {
                        "StatementGeographicalAxis": {
                            "type": "explicit",
                            "axis_qname": "us-gaap:StatementGeographicalAxis",
                            "member_qname": "country:RestOfWorldMember",
                            "member_local_name": "RestOfWorldMember",
                            "member_label": "Rest Of World [Member]",
                            "axis_local_name": "StatementGeographicalAxis",
                        }
                    },
                }
            ],
        }
    ]

    bundle = _service()._build_bundle(_statement(financial_data), _filing())

    assert bundle.concepts[0]["label"] == "Revenues"
    assert bundle.dimensional_concepts[0]["label"] == "Rest Of World"
    # The raw source view is cleaned too so validation agents never see tags.
    assert bundle.source_items[0]["label"] == "Revenues"
    _details = bundle.source_items[0]["dimensional_facts"][0]["dimension_details"]
    assert _details["StatementGeographicalAxis"]["member_label"] == "Rest Of World"
