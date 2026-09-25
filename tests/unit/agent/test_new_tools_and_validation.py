"""Tests for newly introduced agentic tools and validators.

Verifies:
- coverage validator catches missing mandatory concepts (e.g. revenue)
- cross-statement consistency validator catches Net Income discrepancies
- classification validator validates statement concepts
- filing & reconciliation tools execute cleanly
"""
from datetime import datetime, timezone
from bson import ObjectId

from data_normalization_service.core.models import StatementBundle
from filings_agent.validation.coverage import check_coverage
from filings_agent.validation.cross_statement import check_cross_statement_consistency
from filings_agent.tools.xbrl_tools import (
    validate_statement_classification_logic,
    xbrl_extract,
)
from filings_agent.tools.filing_tools import check_filing_exists
from filings_agent.tools.reconcile_tools import approve_reconciliation_fill, reject_reconciliation_fill


def _make_bundle(statement_type, concepts):
    return StatementBundle(
        company_cik="0000320193",
        statement_type=statement_type,
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=concepts,
    )


def test_coverage_validator_missing_revenue():
    # Bundle with no revenue item
    bundle = _make_bundle("income", [{"concept": "us-gaap:GeneralAndAdministrativeExpense", "value": 5000}])
    findings = check_coverage(bundle)
    assert len(findings) == 1
    assert findings[0].type == "missing_mandatory_concept"
    assert findings[0].severity == "high"


def test_coverage_validator_with_revenue():
    bundle = _make_bundle("income", [{"concept": "us-gaap:Revenues", "value": 100000}])
    findings = check_coverage(bundle)
    assert findings == []


def test_cross_statement_consistency_matching():
    inc_bundle = _make_bundle("income", [
        {"concept": "us-gaap:NetIncomeLoss", "value": 50000, "period": "2024-01-01 to 2024-12-31"}
    ])
    cf_bundle = _make_bundle("cash_flow", [
        {"concept": "us-gaap:NetIncomeLoss", "value": 50000, "period": "2024-01-01 to 2024-12-31"}
    ])
    findings = check_cross_statement_consistency([inc_bundle, cf_bundle])
    assert findings == []


def test_cross_statement_consistency_mismatch():
    inc_bundle = _make_bundle("income", [
        {"concept": "us-gaap:NetIncomeLoss", "value": 50000, "period": "2024-01-01 to 2024-12-31"}
    ])
    cf_bundle = _make_bundle("cash_flow", [
        {"concept": "us-gaap:NetIncomeLoss", "value": 40000, "period": "2024-01-01 to 2024-12-31"}
    ])
    findings = check_cross_statement_consistency([inc_bundle, cf_bundle])
    assert len(findings) == 1
    assert findings[0].type == "cross_statement_math_mismatch"
    assert findings[0].severity == "high"


def test_statement_classification_logic():
    res = validate_statement_classification_logic("income", [
        {"concept": "us-gaap:Revenues", "value": 1000}
    ])
    assert res["valid"] is True

    bad_res = validate_statement_classification_logic("income", [
        {"concept": "us-gaap:SomeOtherItem", "value": 1000}
    ])
    assert bad_res["valid"] is False
    assert "No revenue" in bad_res["reason"]


def test_reconciliation_decision_tools():
    app = approve_reconciliation_fill.invoke({"proposal_id": "P-101"})
    assert "Approved gap-fill proposal P-101" in app

    rej = reject_reconciliation_fill.invoke({"proposal_id": "P-102"})
    assert "Rejected gap-fill proposal P-102" in rej


def test_structure_validator_flags_orphan_hierarchy_path():
    """A row whose parent path has no owning row must block (this is exactly the
    symptom of a collapsed/moved grouping header)."""
    from filings_agent.validation.structure import check_structure

    bundle = _make_bundle("income", [
        {"concept": "us-gaap:Revenues", "value": 100.0, "path": "001", "order_key": "a"},
    ])
    # Member nested under a header path (001.001) that no row owns.
    bundle.dimensional_concepts = [
        {"concept": "aapl:IPhoneMember", "parent_concept": "us-gaap:Revenues",
         "path": "001.001.001", "order_key": "a"},
    ]
    findings = check_structure(bundle)
    assert any(f.type == "orphan_hierarchy_path" for f in findings)
    assert any(f.is_blocking for f in findings)


def test_structure_validator_accepts_a_complete_tree():
    from filings_agent.validation.structure import check_structure

    bundle = _make_bundle("income", [
        {"concept": "us-gaap:Revenues", "value": 100.0, "path": "001", "order_key": "a"},
    ])
    bundle.abstract_concepts = [
        {"concept": "custom:ProductSegmentation", "abstract": True,
         "parent_concept": "us-gaap:Revenues", "path": "001.001", "order_key": "a"},
    ]
    bundle.dimensional_concepts = [
        {"concept": "aapl:IPhoneMember", "parent_concept": "us-gaap:Revenues",
         "path": "001.001.001", "order_key": "a"},
    ]
    findings = check_structure(bundle)
    assert not any(f.type == "orphan_hierarchy_path" for f in findings)
