"""P5 — hierarchy seed/reuse/conflict-safe placement."""
from datetime import datetime, timezone
from unittest.mock import Mock

from bson import ObjectId

from data_normalization_service.core.models import StatementBundle
from filings_agent.hierarchy.resolver import resolve_hierarchy_bundles
from filings_agent.nodes.hierarchy import make_hierarchy_node


def item(concept, path, order_key, value=1.0):
    return {
        "concept": concept,
        "label": concept,
        "value": value,
        "period": "2024-01-01 00:00:00 to 2024-12-31 00:00:00",
        "level": 1,
        "hierarchy_level": 0,
        "order": 1,
        "abstract": False,
        "path": path,
        "order_key": order_key,
    }


def bundle(items):
    return StatementBundle(
        company_cik="0000320193",
        statement_type="income",
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=items,
    )


class Repo:
    def __init__(self, docs=None, reference=None):
        self.docs = docs or []
        self.reference = reference
        self.collection = Mock()
        self.collection.find.return_value = list(self.docs)

    def find_concept_reference_for_hierarchy(self, *args, **kwargs):
        return self.reference


class Service:
    def __init__(self, repo):
        self.repo = repo

    def _get_concept_repo_by_form_type(self, form_type):
        return self.repo


def test_first_statement_seeds_complete_filing_hierarchy():
    b = bundle([
        item("us-gaap:Revenues", "001", "a"),
        item("us-gaap:CostOfRevenue", "002", "b"),
    ])
    plan = resolve_hierarchy_bundles([b], Service(Repo()), cik="0000320193")

    assert plan["seeded_statement_types"] == ["income"]
    assert plan["resolved_concepts"] == 2
    assert plan["sources"]["fresh_seed"] == 2
    assert all(i["_hierarchy_resolved"] for i in b.concepts)
    assert [(i["path"], i["order_key"]) for i in b.concepts] == [
        ("001", "a"), ("002", "b")
    ]


def test_existing_same_company_concept_reuses_path_and_order():
    existing = [{
        "concept": "us-gaap:Revenues",
        "path": "005",
        "order_key": "z",
        "dimension_concept": False,
    }]
    b = bundle([
        item("us-gaap:Revenues", "001", "a"),
        item("us-gaap:CostOfRevenue", "002", "b"),
    ])
    plan = resolve_hierarchy_bundles([b], Service(Repo(existing)), cik="0000320193")

    assert b.concepts[0]["path"] == "005"
    assert b.concepts[0]["order_key"] == "z"
    assert b.concepts[0]["hierarchy_source"] == "same_company_existing"
    assert plan["seeded_statement_types"] == []


def test_new_concept_conflict_gets_safe_sibling_placement():
    existing = [{
        "concept": "us-gaap:Revenues",
        "path": "001",
        "order_key": "a",
        "dimension_concept": False,
    }]
    b = bundle([item("us-gaap:NewConcept", "001", "a")])
    plan = resolve_hierarchy_bundles([b], Service(Repo(existing)), cik="0000320193")

    assert (b.concepts[0]["path"], b.concepts[0]["order_key"]) == ("002", "b")
    assert b.concepts[0]["hierarchy_source"] == "conflict_safe_sibling"
    assert plan["conflicts"] == []


def test_cross_company_reference_is_used_when_filing_position_conflicts():
    existing = [{
        "concept": "us-gaap:Revenues",
        "path": "001",
        "order_key": "a",
        "dimension_concept": False,
    }]
    reference = {"path": "007", "order_key": "g", "cik": "0000789019"}
    b = bundle([item("us-gaap:NewConcept", "001", "a")])
    plan = resolve_hierarchy_bundles([b], Service(Repo(existing, reference)), cik="0000320193")

    assert (b.concepts[0]["path"], b.concepts[0]["order_key"]) == ("007", "g")
    assert b.concepts[0]["hierarchy_source"] == "cross_company_reference"
    assert plan["sources"]["cross_company_reference"] == 1


def test_no_duplicate_path_order_pairs_after_resolution():
    existing = [{
        "concept": "us-gaap:Revenues",
        "path": "001",
        "order_key": "a",
        "dimension_concept": False,
    }]
    b = bundle([
        item("us-gaap:One", "001", "a"),
        item("us-gaap:Two", "001", "a"),
    ])
    resolve_hierarchy_bundles([b], Service(Repo(existing)), cik="0000320193")
    pairs = [(i["path"], i["order_key"]) for i in b.concepts]
    assert len(pairs) == len(set(pairs))
    assert all(i.get("_hierarchy_resolved") for i in b.concepts)


def test_existing_duplicate_path_order_is_repathed():
    existing = [
        {"_id": ObjectId(), "concept": "us-gaap:One", "path": "001", "order_key": "a", "dimension_concept": False},
        {"_id": ObjectId(), "concept": "us-gaap:Two", "path": "001", "order_key": "a", "dimension_concept": False},
    ]
    b = bundle([item("us-gaap:Two", "001", "a")])
    plan = resolve_hierarchy_bundles([b], Service(Repo(existing)), cik="0000320193")
    assert len(plan["existing_updates"]) == 1
    assert plan["existing_updates"][0]["concept"] == "us-gaap:Two"
    assert plan["existing_updates"][0]["path"] == "002"
    assert plan["existing_updates"][0]["order_key"] == "b"


def test_hierarchy_node_writes_plan_to_state_without_db_side_effects():
    b = bundle([item("us-gaap:Revenues", "001", "a")])
    state = {"cik": "0000320193", "bundles": [b], "status": "validated"}
    result = make_hierarchy_node(Service(Repo()))(state)
    assert result["status"] == "hierarchy_resolved"
    assert result["hierarchy_plan"]["resolved_concepts"] == 1


def test_resolve_hierarchy_creates_custom_segmentation_headers():
    b = bundle([
        item("us-gaap:Revenues", "001", "a"),
        item("us-gaap:CostOfRevenue", "002", "b"),
    ])
    b.abstract_concepts = []
    b.dimensional_concepts = [
        {
            "parent_concept": "us-gaap:Revenues",
            "segment_type": "product_service",
            "concept": "aapl:IPhoneMember",
            "label": "iPhone [Member]",
            "dimension_data": {"dimensions": {"ProductOrServiceAxis": "IPhoneMember"}},
        },
        {
            "parent_concept": "us-gaap:Revenues",
            "segment_type": "product_service",
            "concept": "aapl:MacMember",
            "label": "Mac [Member]",
            "dimension_data": {"dimensions": {"ProductOrServiceAxis": "MacMember"}},
        },
        {
            "parent_concept": "us-gaap:Revenues",
            "segment_type": "business_segment",
            "concept": "aapl:AmericasSegmentMember",
            "label": "Americas Segment [Member]",
            "dimension_data": {"dimensions": {"StatementBusinessSegmentsAxis": "AmericasSegmentMember"}},
        },
        {
            "parent_concept": "us-gaap:Revenues",
            "segment_type": "business_segment",
            "concept": "aapl:EuropeSegmentMember",
            "label": "Europe Segment [Member]",
            "dimension_data": {"dimensions": {"StatementBusinessSegmentsAxis": "EuropeSegmentMember"}},
        },
    ]

    plan = resolve_hierarchy_bundles([b], Service(Repo()), cik="0000320193")

    # Custom grouping headers created in abstract_concepts
    header_concepts = [a["concept"] for a in b.abstract_concepts]
    assert "custom:ProductSegmentation" in header_concepts
    assert "custom:GeographicSegmentation" in header_concepts

    prod_hdr = next(a for a in b.abstract_concepts if a["concept"] == "custom:ProductSegmentation")
    geo_hdr = next(a for a in b.abstract_concepts if a["concept"] == "custom:GeographicSegmentation")

    assert prod_hdr["abstract"] is True
    assert geo_hdr["abstract"] is True
    assert prod_hdr["path"].startswith("001.")
    assert geo_hdr["path"].startswith("001.")
    assert prod_hdr["path"] != geo_hdr["path"]

    # Check dimensional concept placements under the headers
    by_concept = {d["concept"]: d for d in b.dimensional_concepts}
    assert by_concept["aapl:IPhoneMember"]["path"].startswith(f"{prod_hdr['path']}.")
    assert by_concept["aapl:MacMember"]["path"].startswith(f"{prod_hdr['path']}.")
    assert by_concept["aapl:AmericasSegmentMember"]["path"].startswith(f"{geo_hdr['path']}.")
    assert by_concept["aapl:EuropeSegmentMember"]["path"].startswith(f"{geo_hdr['path']}.")

    # Verify zero duplicate paths across all resolved items
    all_paths = [i["path"] for i in b.concepts] + [a["path"] for a in b.abstract_concepts] + [d["path"] for d in b.dimensional_concepts]
    assert len(all_paths) == len(set(all_paths))

