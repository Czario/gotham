"""P3 — the persist gate (STRICT_ACCURACY).

Verifies the graph refuses to write a filing that has unresolved high-severity
validation findings, still records the report, and that the gate is bypassable
(``STRICT_ACCURACY=0``) and scoped (medium findings and absence-only finding
types never block).
"""
from datetime import datetime, timezone

from bson import ObjectId

from data_normalization_service.core.models import StatementBundle
from filings_agent.graph import build_filing_graph
from filings_agent.state import new_state
from filings_agent.validation.findings import blocking_findings


# ── factories ───────────────────────────────────────────────────────────────


def _item(concept, value, **overrides):
    base = {
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
    base.update(overrides)
    return base


def _bundle(items):
    return StatementBundle(
        company_cik="0000320193",
        statement_type="income",
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=list(items),
    )


def _income_items(gp=600_000):
    return [
        _item("us-gaap:Revenues", 1_000_000, path="001", order_key="a"),
        _item("us-gaap:CostOfRevenue", 400_000, path="002", order_key="b"),
        _item("us-gaap:GrossProfit", gp, path="003", order_key="c"),
    ]


class _FakeNormService:
    """Returns one pre-built bundle per statement doc and records writes."""

    def __init__(self, bundles):
        self._bundles = list(bundles)
        self._index = 0
        self.persist_calls = []

    def normalize_statement_to_bundle(self, statement_doc, filing_doc, company_doc):
        bundle = self._bundles[self._index]
        self._index += 1
        return bundle

    def persist_statement_bundle(self, bundle, *, enforce_allowed_types=True, **kwargs):
        self.persist_calls.append(bundle)
        return True


class _FakeReportStore:
    def __init__(self):
        self.saved = []
        self.decisions = []

    def save(self, report):
        self.saved.append(report)

    def update_decision(self, cik, accession_number, form_type, decision):
        self.decisions.append(decision)


def _state():
    return new_state(
        cik="0000320193",
        ticker="AAPL",
        form_type="10-K",
        accession_number="acc-1",
        statement_docs=[{"statement_type": "income"}],
        filing_doc={"form_type": "10-K"},
        company_doc={"cik": "0000320193"},
    )


# ── gate behaviour ──────────────────────────────────────────────────────────


def test_gate_refuses_filing_with_high_finding(monkeypatch):
    import filings_agent.config as cfg

    monkeypatch.setattr(cfg, "STRICT_ACCURACY", True)  # strict gate under test
    svc = _FakeNormService([_bundle(_income_items(gp=999_999))])
    store = _FakeReportStore()
    graph = build_filing_graph(svc, report_store=store)

    final = graph.invoke(_state())

    # The agent's explicit decision is "write nothing" — a skip, not a failure.
    assert final["status"] == "skipped"
    assert final["decision"]["action"] == "skip"
    assert final["decision"]["decided_by"] == "policy"
    assert svc.persist_calls == []  # nothing written
    assert final["validation_report"]["status"] == "fail"
    # The report (and the decision) are recorded even though nothing was written.
    assert len(store.saved) >= 1
    assert store.saved[-1].status == "fail"
    assert store.decisions and store.decisions[-1]["action"] == "skip"


def test_gate_allows_clean_filing():
    svc = _FakeNormService([_bundle(_income_items())])
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "saved"
    assert len(svc.persist_calls) == 1
    assert final["persist_receipt"]["blocking_findings"] == 0


def test_gate_allows_medium_findings_only():
    items = _income_items()
    items[0]["path"] = None
    items[0]["order_key"] = None  # medium: missing_hierarchy_path
    svc = _FakeNormService([_bundle(items)])
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "saved"
    # The hierarchy node repairs the missing path/order before the final
    # validation, so the persisted report is clean.
    assert final["validation_report"]["status"] == "pass"
    assert len(svc.persist_calls) == 1


def test_gate_can_be_disabled(monkeypatch):
    import filings_agent.config as cfg

    monkeypatch.setattr(cfg, "STRICT_ACCURACY", False)

    svc = _FakeNormService([_bundle(_income_items(gp=999_999))])
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "saved"
    assert len(svc.persist_calls) == 1
    # report still records the failure, it just does not block
    assert final["validation_report"]["status"] == "fail"


def test_absence_only_high_findings_never_block():
    assert blocking_findings(
        [
            {"type": "missing_concept", "severity": "high", "message": "absent"},
            {"type": "empty_statement", "severity": "high", "message": "empty"},
        ]
    ) == []


def test_graph_runs_normalize_then_validate_then_persist():
    """Ordering: validate must complete before persist writes."""
    events = []

    class _Svc(_FakeNormService):
        def normalize_statement_to_bundle(self, *a, **k):
            events.append("normalize")
            return super().normalize_statement_to_bundle(*a, **k)

        def persist_statement_bundle(self, bundle, **k):
            events.append("persist")
            return super().persist_statement_bundle(bundle, **k)

    class _Store:
        def save(self, report):
            events.append("validate-report")

    svc = _Svc([_bundle(_income_items())])
    graph = build_filing_graph(svc, report_store=_Store())
    final = graph.invoke(_state())

    assert final["status"] == "saved"
    assert events == ["normalize", "validate-report", "validate-report", "persist"]
