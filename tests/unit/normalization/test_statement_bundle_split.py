"""P1 — statement normalization split contract.

Verifies that normalization was split into a pure compute phase
(``normalize_statement_to_bundle`` — no DB access) and an explicit write phase
(``persist_statement_bundle`` — the only writer), with the legacy
``normalize_statement_in_memory`` retained as a compute+persist wrapper.
"""
from unittest.mock import Mock, MagicMock
from datetime import datetime

import pytest
from bson import ObjectId

from data_normalization_service.services.normalization_service import (
    FinancialNormalizationService,
)
from data_normalization_service.core.config import AppConfig
from data_normalization_service.core.models import StatementBundle


@pytest.fixture
def service(mocker):
    """A service with its DB connection patched out (mirrors test_sync_behavior)."""
    mocker.patch(
        "data_normalization_service.services.normalization_service.DatabaseConnection"
    )
    mocker.patch(
        "data_normalization_service.services.normalization_service.DatabaseTracker"
    )
    config = Mock(spec=AppConfig)
    config.database = Mock()
    return FinancialNormalizationService(config)


def _statement_doc(statement_type: str = "income") -> dict:
    return {
        "cik": "0000320193",
        "statement_type": statement_type,
        "reporting_period": {"end_date": "2024-03-31", "fiscal_year": 2024, "quarter": 1},
        "data": [
            {
                "concept": "us-gaap:IncomeStatementAbstract",
                "label": "Income Statement",
                "value": None,
                "period": "2024-01-01 00:00:00 to 2024-03-31 00:00:00",
                "level": 0,
                "order": 0,
                "abstract": True,
            },
            {
                "concept": "us-gaap:Revenues",
                "label": "Revenue",
                "value": 100.0,
                "period": "2024-01-01 00:00:00 to 2024-03-31 00:00:00",
                "level": 1,
                "order": 1,
                "abstract": False,
                "fact_id": "fact-1",
                "decimals": "-6",
                "unit": "USD",
            },
        ],
    }


FILING_DOC = {"_id": ObjectId(), "form_type": "10-Q", "accession_number": "0000320193-24-000001"}
COMPANY_DOC = {"cik": "0000320193", "name": "Apple Inc."}


# ── pure compute ────────────────────────────────────────────────────────────


def test_bundle_build_touches_no_database(service):
    """The compute phase must not read or write the DB."""
    # Any DB interaction in the pure phase is a hard failure.
    service._get_concept_repo_by_form_type = Mock(
        side_effect=AssertionError("pure phase touched the concept repo")
    )
    service._ensure_company_in_target_from_dict = Mock(
        side_effect=AssertionError("pure phase touched the company repo")
    )

    bundle = service.normalize_statement_to_bundle(
        _statement_doc(), FILING_DOC, COMPANY_DOC
    )

    assert bundle is not None
    service._get_concept_repo_by_form_type.assert_not_called()
    service._ensure_company_in_target_from_dict.assert_not_called()


def test_bundle_contents(service):
    bundle = service.normalize_statement_to_bundle(
        _statement_doc(), FILING_DOC, COMPANY_DOC
    )

    assert isinstance(bundle, StatementBundle)
    assert bundle.company_cik == "0000320193"
    assert bundle.statement_type == "income"
    assert bundle.form_type == "10-Q"
    assert bundle.accession_number == "0000320193-24-000001"
    assert bundle.company_doc == COMPANY_DOC

    # Only the concrete concept survives the structural filter.
    assert [c["concept"] for c in bundle.concepts] == ["us-gaap:Revenues"]
    # The full hierarchy (incl. the abstract node) is retained for audit.
    assert len(bundle.hierarchy) == 2
    assert bundle.source_items == _statement_doc()["data"]

    # Informational flattened value view.
    assert len(bundle.values) == 1
    assert bundle.values[0]["concept"] == "us-gaap:Revenues"
    assert bundle.values[0]["value"] == 100.0
    assert bundle.values[0]["period_key"] == "2024-03-31"
    assert bundle.values[0]["fact_id"] == "fact-1"


# ── explicit persist ────────────────────────────────────────────────────────


def _recording_service(service):
    """Wire the service so write helpers record their call order."""
    calls = []

    fake_repo = Mock()
    fake_repo.find_concept_reference_for_hierarchy.return_value = None

    service._ensure_company_in_target_from_dict = Mock(
        side_effect=lambda *a, **k: calls.append("company")
    )
    service._get_concept_repo_by_form_type = Mock(return_value=fake_repo)
    service._get_or_create_concept = Mock(
        side_effect=lambda *a, **k: (calls.append("concept"), ObjectId())[1]
    )
    service._create_value_record = Mock(
        side_effect=lambda *a, **k: (calls.append("value"), ObjectId())[1]
    )
    service._process_dimensional_data_enhanced = Mock(
        side_effect=lambda *a, **k: calls.append("dimensional")
    )
    return service, calls


def test_persist_writes_in_historical_order(service):
    bundle = service.normalize_statement_to_bundle(
        _statement_doc(), FILING_DOC, COMPANY_DOC
    )
    service, calls = _recording_service(service)

    written = service.persist_statement_bundle(bundle)

    assert written is True
    # company ensure → concept create/reuse → value write
    assert calls == ["company", "concept", "value"]
    service._ensure_company_in_target_from_dict.assert_called_once_with(COMPANY_DOC)
    assert service._get_or_create_concept.call_count == 1
    assert service._create_value_record.call_count == 1


def test_persist_none_is_noop(service):
    service._ensure_company_in_target_from_dict = Mock()
    assert service.persist_statement_bundle(None) is False
    service._ensure_company_in_target_from_dict.assert_not_called()


def test_persist_skips_disallowed_type_but_still_ensures_company(service):
    """Historical behaviour: company is written before the statement-type filter."""
    bundle = service.normalize_statement_to_bundle(
        _statement_doc("equity"), FILING_DOC, COMPANY_DOC
    )
    service, calls = _recording_service(service)

    written = service.persist_statement_bundle(bundle)

    assert written is False
    assert calls == ["company"]  # company ensured, nothing else written


def test_persist_allowed_type_enforced_off_writes_everything(service):
    """The legacy source-DB path opts out of the statement-type filter."""
    bundle = service.normalize_statement_to_bundle(
        _statement_doc("equity"), FILING_DOC, COMPANY_DOC
    )
    service, calls = _recording_service(service)

    written = service.persist_statement_bundle(bundle, enforce_allowed_types=False)

    assert written is True
    assert calls == ["company", "concept", "value"]


# ── backward-compatible wrapper ─────────────────────────────────────────────


def test_wrapper_is_compute_then_persist(service):
    service.normalize_statement_to_bundle = Mock(return_value=MagicMock())
    service.persist_statement_bundle = Mock()

    service.normalize_statement_in_memory(
        _statement_doc(), FILING_DOC, COMPANY_DOC
    )

    service.normalize_statement_to_bundle.assert_called_once()
    service.persist_statement_bundle.assert_called_once_with(
        service.normalize_statement_to_bundle.return_value
    )


# ── legacy source-DB path ───────────────────────────────────────────────────


def test_legacy_path_builds_bundle_from_models_and_persists(service):
    """The source-DB path builds a bundle from model objects and writes it,
    without applying the allowed-statement-type filter."""
    from data_normalization_service.core.models import FinancialStatement, Filing

    filing_id = ObjectId()
    statement = FinancialStatement(
        id=ObjectId(),
        company_cik="0000320193",
        filing_id=filing_id,
        statement_type="income",
        reporting_period={"end_date": "2024-03-31", "fiscal_year": 2024},
        created_at=datetime.utcnow(),
        financial_data=_statement_doc()["data"],
    )
    filing = Filing(id=filing_id, form_type="10-K", accession_number="acc-1")
    service, calls = _recording_service(service)

    service._process_financial_statement_with_filing(statement, filing)

    assert calls == ["company", "concept", "value"]
