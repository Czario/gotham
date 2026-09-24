"""P4 — review, repair, correction gates and provenance."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import Filing, FinancialStatement, StatementBundle
from filings_agent.graph import build_filing_graph
from filings_agent.review.corrections import CorrectionGate, apply_decisions_to_bundles
from filings_agent.state import new_state
from filings_agent.agent.tools import build_review_tools


def item(concept, value, **overrides):
    data = {
        "concept": concept,
        "label": concept,
        "value": value,
        "period": "2024-01-01 00:00:00 to 2024-12-31 00:00:00",
        "level": 1,
        "order": 1,
        "abstract": False,
        "path": "001",
        "order_key": "a",
    }
    data.update(overrides)
    return data


def bundle(gross_profit=999_999.0):
    return StatementBundle(
        company_cik="0000320193",
        statement_type="income",
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=[
            item("us-gaap:Revenues", 1_000_000.0, path="001", order_key="a"),
            item("us-gaap:CostOfRevenue", 400_000.0, path="002", order_key="b"),
            item("us-gaap:GrossProfit", gross_profit, path="003", order_key="c"),
        ],
    )


class FakeNormService:
    def __init__(self, b):
        self.b = b
        self.persist_calls = []

    def normalize_statement_to_bundle(self, *args):
        return self.b

    def persist_statement_bundle(self, b, **kwargs):
        self.persist_calls.append(b)
        return True


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, report):
        self.saved.append(report)


def state():
    return new_state(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form_type="10-K",
        accession_number="acc-repair-1",
        statement_docs=[{"statement_type": "income"}],
        filing_doc={"form_type": "10-K"},
        company_doc={"cik": "0000320193", "name": "Apple Inc."},
    )


class StubChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tools = None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return self.responses.pop(0)


def test_deterministic_repair_corrects_math_and_persists(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_MODE", "repair")
    monkeypatch.setattr(config, "AGENT_REVIEW_ENABLED", False)
    monkeypatch.setattr(config, "STRICT_ACCURACY", True)

    b = bundle(999_999.0)
    svc = FakeNormService(b)
    final = build_filing_graph(
        svc,
        report_store=FakeStore(),
        repair_mode="repair",
    ).invoke(state())

    assert final["status"] == "saved"
    assert len(svc.persist_calls) == 1
    assert b.concepts[2]["value"] == pytest.approx(600_000.0)
    assert b.concepts[2]["repaired"] is True
    assert b.concepts[2]["source"] == "deterministic_identity"
    assert b.concepts[2]["corrected_from"] == pytest.approx(999_999.0)
    assert final["repair_actions"]["applied"] == 1
    assert final["validation_report"]["status"] == "pass"


def test_report_mode_does_not_apply_correction_and_gate_refuses(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_MODE", "report")
    monkeypatch.setattr(config, "AGENT_REVIEW_ENABLED", False)
    monkeypatch.setattr(config, "STRICT_ACCURACY", True)

    b = bundle(999_999.0)
    svc = FakeNormService(b)
    final = build_filing_graph(svc, repair_mode="report").invoke(state())

    # Report mode cannot repair, so the agent decides to write nothing.
    assert final["status"] == "skipped"
    assert final["decision"]["action"] == "skip"
    assert svc.persist_calls == []
    assert b.concepts[2]["value"] == pytest.approx(999_999.0)


def test_llm_review_proposal_is_gated_and_applied(monkeypatch):
    import filings_agent.config as config

    monkeypatch.setattr(config, "AGENT_MODE", "repair")
    monkeypatch.setattr(config, "AGENT_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "STRICT_ACCURACY", True)

    response = AIMessage(
        content="",
        tool_calls=[{
            "name": "finalize_review",
            "args": {
                "result_json": (
                    '{"repairs":[{"statement_type":"income",'
                    '"concept":"us-gaap:GrossProfit","old_value":999999,'
                    '"new_value":600000,"reason":"Revenue minus cost of revenue",'
                    '"source":"agent_confirmed","approved":true,"confidence":0.95}]}'
                )
            },
            "id": "final-1",
        }],
    )
    chat = StubChat([response])
    b = bundle(999_999.0)
    svc = FakeNormService(b)
    final = build_filing_graph(
        svc, repair_mode="repair", review_chat_llm=chat
    ).invoke(state())

    assert final["status"] == "saved"
    assert b.concepts[2]["value"] == pytest.approx(600_000.0)
    assert b.concepts[2]["source"] == "agent_confirmed"
    assert chat.tools is not None
    assert {tool.name for tool in chat.tools} >= {
        "query_concepts", "query_values", "verify_math", "propose_repair", "finalize_review"
    }


def test_correction_gate_rejects_stale_or_unapproved():
    b = bundle(999_999.0)
    gate = CorrectionGate(mode="repair")
    decision, reason = gate.approve({
        "statement_type": "income", "concept": "us-gaap:GrossProfit",
        "old_value": 1.0, "new_value": 600000.0,
        "reason": "stale", "source": "deterministic_identity",
    }, b)
    assert decision is None
    assert "no longer matches current" in reason

    strict = CorrectionGate(mode="strict")
    decision, reason = strict.approve({
        "statement_type": "income", "concept": "us-gaap:GrossProfit",
        "old_value": 999999.0, "new_value": 600000.0,
        "reason": "identity", "source": "agent_confirmed", "approved": False,
    }, b)
    assert decision is None
    assert "explicit approval" in reason


def test_correction_gate_rejects_untrusted_source():
    b = bundle(999_999.0)
    decision, reason = CorrectionGate(mode="repair").approve({
        "statement_type": "income", "concept": "us-gaap:GrossProfit",
        "old_value": 999999.0, "new_value": 600000.0,
        "reason": "guess", "source": "model_guess", "approved": True,
    }, b)
    assert decision is None
    assert "not allowed" in reason


def test_apply_decision_updates_flattened_and_concept_values():
    b = bundle(999_999.0)
    decision = CorrectionGate(mode="repair").approve({
        "statement_type": "income", "concept": "us-gaap:GrossProfit",
        "old_value": 999999.0, "new_value": 600000.0,
        "reason": "identity", "source": "deterministic_identity", "approved": True,
    }, b)[0]
    b.values = [{"concept": "us-gaap:GrossProfit", "value": 999999.0}]
    actions = apply_decisions_to_bundles([b], [decision])
    assert len(actions) == 1
    assert b.concepts[2]["value"] == pytest.approx(600000.0)
    assert b.values[0]["value"] == pytest.approx(600000.0)
    assert b.concepts[2]["corrected_from"] == pytest.approx(999999.0)


def test_review_tools_are_read_and_propose_only():
    b = bundle(999_999.0)
    proposals = []
    tools = {t.name: t for t in build_review_tools([b], proposals)}
    concepts = tools["query_concepts"].invoke({"query": "GrossProfit"})
    assert "GrossProfit" in concepts
    assert "600000" not in concepts
    assert tools["verify_math"].invoke({"expression": "1000 - 400"}) == "600.0"
    result = tools["propose_repair"].invoke({
        "statement_type": "income", "concept": "us-gaap:GrossProfit",
        "old_value": 999999, "new_value": 600000,
        "reason": "identity", "source": "agent_confirmed", "approved": True,
    })
    assert "no database write" in result
    assert len(proposals) == 1


# ── persistence provenance ──────────────────────────────────────────────────


def test_create_value_record_inserts_repair_provenance(mocker):
    from data_normalization_service.core.config import AppConfig
    from data_normalization_service.services.normalization_service import FinancialNormalizationService

    mocker.patch("data_normalization_service.services.normalization_service.DatabaseConnection")
    mocker.patch("data_normalization_service.services.normalization_service.DatabaseTracker")
    config = Mock(spec=AppConfig)
    config.database = Mock()
    service = FinancialNormalizationService(config)

    collection = Mock()
    inserted_id = ObjectId()
    collection.insert_one.return_value.inserted_id = inserted_id
    repo = Mock(collection=collection)
    repo.find_existing_value.return_value = None  # nothing stored for the period
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    filing_id = ObjectId()
    statement = FinancialStatement(
        id=ObjectId(), company_cik="0000320193", filing_id=filing_id,
        statement_type="income", reporting_period={"fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc), financial_data=[],
    )
    filing = Filing(id=filing_id, form_type="10-K", accession_number="acc-1")
    repaired_item = item(
        "us-gaap:GrossProfit", 600000.0,
        repaired=True, source="deterministic_identity", corrected_from=999999.0,
        correction_reason="identity", correction_source="deterministic_identity",
    )

    result = service._create_value_record(
        ObjectId(), statement, filing, repaired_item, 600000.0
    )
    assert result == inserted_id
    doc = collection.insert_one.call_args.args[0]
    assert doc["source"] == "deterministic_identity"
    assert doc["corrected_from"] == pytest.approx(999999.0)
    assert doc["correction_reason"] == "identity"


def test_create_value_record_updates_existing_repaired_value(mocker):
    from data_normalization_service.core.config import AppConfig
    from data_normalization_service.services.normalization_service import FinancialNormalizationService

    mocker.patch("data_normalization_service.services.normalization_service.DatabaseConnection")
    mocker.patch("data_normalization_service.services.normalization_service.DatabaseTracker")
    config = Mock(spec=AppConfig)
    config.database = Mock()
    service = FinancialNormalizationService(config)

    existing_id = ObjectId()
    collection = Mock()
    repo = Mock(collection=collection)
    repo.find_existing_value.return_value = {"_id": existing_id, "value": 999999.0}
    service._get_value_repo_by_form_type = Mock(return_value=repo)

    filing_id = ObjectId()
    statement = FinancialStatement(
        id=ObjectId(), company_cik="0000320193", filing_id=filing_id,
        statement_type="income", reporting_period={"fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc), financial_data=[],
    )
    filing = Filing(id=filing_id, form_type="10-K", accession_number="acc-1")
    repaired_item = item(
        "us-gaap:GrossProfit", 600000.0,
        repaired=True, source="deterministic_identity", corrected_from=999999.0,
        correction_reason="identity", correction_source="deterministic_identity",
    )

    result = service._create_value_record(
        ObjectId(), statement, filing, repaired_item, 600000.0
    )
    assert result == existing_id
    update = collection.update_one.call_args.args[1]["$set"]
    assert update["value"] == pytest.approx(600000.0)
    assert update["source"] == "deterministic_identity"
    assert update["corrected_from"] == pytest.approx(999999.0)
