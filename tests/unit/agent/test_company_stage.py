"""P7 — company-stage graph (quarterly → companyfacts → finalize) and batch runner."""
from unittest.mock import Mock

import pytest

from filings_agent.batch import run_batch, run_company_stage, summarize
from filings_agent.company_graph import build_company_graph
from filings_agent.state import company_state_summary, new_company_state


class FakeQuarterly:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def process_company_from_statements(self, cik, statements):
        self.calls.append((cik, len(statements)))
        if self.fail:
            raise RuntimeError("deaccumulation boom")


class FakeReconciliation:
    def __init__(self, *, filled=2, fail=False):
        self.calls = []
        self.filled = filled
        self.fail = fail

    def reconcile_company(self, cik, frequency="annual", include_edge=True):
        self.calls.append((cik, frequency))
        if self.fail:
            raise RuntimeError("companyfacts boom")
        return {"filled": self.filled}


class FakeReportStore:
    def __init__(self, *, finalized=3, fail=False):
        self.calls = []
        self.finalized = finalized
        self.fail = fail

    def finalize_company(self, cik, summary=None):
        self.calls.append((cik, summary))
        if self.fail:
            raise RuntimeError("finalize boom")
        return self.finalized


def _graph(quarterly=None, recon=None, store=None, **kwargs):
    return build_company_graph(
        quarterly_service=quarterly or FakeQuarterly(),
        reconciliation_provider=(lambda: recon) if recon is not None else None,
        report_store=store,
        **kwargs,
    )


# ── company graph ───────────────────────────────────────────────────────────


def test_company_stage_runs_all_three_nodes():
    quarterly = FakeQuarterly()
    recon = FakeReconciliation(filled=2)
    store = FakeReportStore(finalized=3)

    final = _graph(quarterly, recon, store).invoke(
        new_company_state(
            cik="0000320193", quarterly_statements=[{"a": 1}, {"b": 2}]
        )
    )
    assert final["status"] == "finalized"

    assert quarterly.calls == [("0000320193", 2)]
    assert recon.calls == [("0000320193", "annual"), ("0000320193", "quarterly")]
    assert store.calls and store.calls[0][0] == "0000320193"
    assert final["status"] == "finalized"
    assert final["deaccumulation"]["status"] == "ok"
    assert final["reconciliation"]["status"] == "ok"
    assert final["reconciliation"]["filled"] == 4
    assert final["report_finalization"] == {"status": "ok", "finalized": 3}

    summary = company_state_summary(final)
    assert summary["deaccumulation"] == "ok"
    assert summary["reconciliation"] == "ok"
    assert summary["report_finalization"] == "ok"


def test_company_stage_skips_deaccumulation_without_statements():
    final = _graph(FakeQuarterly(), FakeReconciliation(), FakeReportStore()).invoke(
        new_company_state(cik="1")
    )
    assert final["deaccumulation"] == {"status": "no_statements", "statements": 0}
    assert final["status"] == "finalized"


def test_company_stage_reconciliation_disabled():
    recon = FakeReconciliation()
    final = _graph(
        FakeQuarterly(), recon, FakeReportStore(), enable_reconciliation=False
    ).invoke(new_company_state(cik="1", quarterly_statements=[{"a": 1}]))
    assert final["reconciliation"]["status"] == "disabled"
    assert recon.calls == []


def test_company_stage_reconciliation_unavailable():
    graph = build_company_graph(
        quarterly_service=FakeQuarterly(),
        reconciliation_provider=lambda: None,
        report_store=FakeReportStore(),
    )
    final = graph.invoke(new_company_state(cik="1", quarterly_statements=[{"a": 1}]))
    assert final["reconciliation"]["status"] == "unavailable"


def test_company_stage_failures_are_recorded_not_raised():
    final = _graph(
        FakeQuarterly(fail=True),
        FakeReconciliation(fail=True),
        FakeReportStore(fail=True),
    ).invoke(new_company_state(cik="1", quarterly_statements=[{"a": 1}]))

    assert final["deaccumulation"]["status"] == "failed"
    assert final["reconciliation"]["status"] == "failed"
    assert final["report_finalization"]["status"] == "failed"
    # Every node failed; the run is reported as failed rather than raising.
    assert final["status"] == "failed"


def test_company_stage_missing_report_store():
    graph = build_company_graph(
        quarterly_service=FakeQuarterly(), reconciliation_provider=lambda: None,
        report_store=None,
    )
    final = graph.invoke(new_company_state(cik="1"))
    assert final["report_finalization"]["status"] == "unavailable"


# ── batch runner ────────────────────────────────────────────────────────────


class FakeGraph:
    def __init__(self, results=None, fail_on=()):
        self.results = list(results or [])
        self.fail_on = set(fail_on)
        self.invocations = []

    def invoke(self, state):
        self.invocations.append(state.get("accession_number"))
        if state.get("accession_number") in self.fail_on:
            raise RuntimeError("graph boom")
        if self.results:
            return {**state, "status": "saved", **self.results.pop(0)}
        return {**state, "status": "saved"}


def _filing(accession, cik="1"):
    return {
        "cik": cik,
        "accession_number": accession,
        "form_type": "10-K",
        "status": "pending",
    }


def test_run_batch_records_and_audits():
    from filings_agent.audit import AuditLog
    from filings_agent.session import RunSession
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        session = RunSession(f"{tmp}/s.jsonl")
        audit = AuditLog(f"{tmp}/a.jsonl")
        graph = FakeGraph()

        out = run_batch(graph, [_filing("a-1"), _filing("a-2")], session=session, audit=audit)

        assert out["summary"] == {"filings": 2, "counts": {"saved": 2}}
        assert session.saved_accessions() == {"a-1", "a-2"}
        events = [r["event"] for r in audit.records()]
        assert events.count("filing_start") == 2
        assert events.count("filing_end") == 2
        assert events[-1] == "batch_end"


def test_run_batch_resumes_by_skipping_saved():
    from filings_agent.session import RunSession
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        session = RunSession(f"{tmp}/s.jsonl")
        first = FakeGraph()
        run_batch(first, [_filing("a-1")], session=session)

        second = FakeGraph()
        out = run_batch(second, [_filing("a-1"), _filing("a-2")], session=session)

        assert second.invocations == ["a-2"]  # a-1 skipped
        assert out["summary"]["counts"] == {"saved": 1, "skipped": 1}


def test_run_batch_survives_one_filing_failing():
    graph = FakeGraph(fail_on={"a-2"})
    out = run_batch(graph, [_filing("a-1"), _filing("a-2"), _filing("a-3")])
    assert out["summary"]["counts"] == {"saved": 2, "failed": 1}
    assert "graph boom" in (out["results"][1]["error"] or "")


def test_summarize_counts_statuses():
    assert summarize([{"status": "saved"}, {"status": "saved"}, {"status": "failed"}]) == {
        "filings": 3,
        "counts": {"saved": 2, "failed": 1},
    }


def test_run_company_stage_records_session(monkeypatch):
    from tempfile import TemporaryDirectory

    from filings_agent.session import RunSession

    with TemporaryDirectory() as tmp:
        session = RunSession(f"{tmp}/s.jsonl")
        graph = _graph(FakeQuarterly(), FakeReconciliation(), FakeReportStore())
        final = run_company_stage(
            graph, cik="1", ticker="TEST", statements=[{"a": 1}], session=session
        )
        assert final["status"] == "finalized"
        company_records = [r for r in session.records() if r["event"] == "company"]
        assert company_records and company_records[0]["status"] == "finalized"
