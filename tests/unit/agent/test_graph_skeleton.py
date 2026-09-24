"""P2 — filings-agent graph skeleton.

Locks in the runtime contract: the graph runs ``normalize_bundle → persist`` on
the state, ``with_hooks`` converts node exceptions into a ``failed`` status
that short-circuits to END, and the normalize phase never writes to the DB.
"""
from unittest.mock import Mock

import pytest

from filings_agent.graph import build_filing_graph
from filings_agent.nodes.normalize import make_normalize_node
from filings_agent.nodes.persist import make_persist_node
from filings_agent.state import new_state, state_summary


class FakeNormService:
    """Records compute/persist calls and simulates bundle production."""

    def __init__(self, *, produce=True, persist_ok=True, raise_in_normalize=False):
        self._produce = produce
        self._persist_ok = persist_ok
        self._raise_in_normalize = raise_in_normalize
        self.normalize_calls = []
        self.persist_calls = []
        self.persist_calls_during_normalize = 0

    def normalize_statement_to_bundle(self, statement_doc, filing_doc, company_doc):
        if self._raise_in_normalize:
            raise RuntimeError("boom")
        # If the node wrote during compute, this proves the split is broken.
        self.persist_calls_during_normalize += len(self.persist_calls)
        self.normalize_calls.append(statement_doc)
        if not self._produce:
            return None
        return f"bundle:{statement_doc.get('statement_type')}"

    def persist_statement_bundle(self, bundle, *, enforce_allowed_types=True, **kwargs):
        self.persist_calls.append((bundle, enforce_allowed_types))
        return self._persist_ok


def _state(**overrides):
    defaults = dict(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form_type="10-K",
        accession_number="0000320193-24-000123",
        statement_docs=[{"statement_type": "income"}, {"statement_type": "balancesheet"}],
        filing_doc={"form_type": "10-K"},
        company_doc={"cik": "0000320193", "name": "Apple Inc."},
    )
    defaults.update(overrides)
    return new_state(**defaults)


# ── graph happy path ────────────────────────────────────────────────────────


def test_graph_runs_normalize_then_persist():
    svc = FakeNormService()
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "saved"
    assert len(svc.normalize_calls) == 2
    assert len(svc.persist_calls) == 2
    assert final["persist_receipt"] == {
        "statements_written": 2,
        "statements_total": 2,
        "statements_skipped": [],
        "cik": "0000320193",
        "accession_number": "0000320193-24-000123",
        "blocking_findings": 0,
        "dropped_concepts": {},
        "promotions": [],
        "action": "write",
        "decided_by": "policy",
        "decision_reason": "validation passed",
    }


def test_normalize_phase_is_not_a_writer():
    """The compute node must never call the persist API."""
    svc = FakeNormService()
    node = make_normalize_node(svc)

    state = node(_state())

    assert state["status"] == "normalized"
    assert svc.persist_calls == []
    assert svc.persist_calls_during_normalize == 0


def test_persist_node_receives_enforce_allowed_types_flag():
    svc = FakeNormService()
    node = make_persist_node(svc, enforce_allowed_types=False)

    state = _state()
    state["bundles"] = ["b1"]
    node(state)

    assert svc.persist_calls == [("b1", False)]


# ── short-circuit / failure handling ────────────────────────────────────────


def test_no_bundles_short_circuits_before_persist():
    svc = FakeNormService(produce=False)
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "failed"
    assert final["error"] == "normalize_bundle produced no bundles"
    assert svc.persist_calls == []  # persist never ran


def test_persist_writing_nothing_marks_failed():
    svc = FakeNormService(persist_ok=False)
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "failed"
    assert final["error"] == "persist wrote no statements"
    assert len(svc.persist_calls) == 2  # attempted, wrote nothing


def test_node_exception_is_converted_to_failed_status():
    """with_hooks turns a node exception into status=failed + error."""
    svc = FakeNormService(raise_in_normalize=True)
    graph = build_filing_graph(svc)

    final = graph.invoke(_state())

    assert final["status"] == "failed"
    assert "normalize_bundle_node raised" in (final["error"] or "")
    assert svc.persist_calls == []


# ── state helpers ───────────────────────────────────────────────────────────


def test_new_state_defaults():
    state = new_state(cik="123", form_type="10-Q")
    assert state["status"] == "pending"
    assert state["ticker"] == "123"  # falls back to cik
    assert state["statement_docs"] == []
    assert state["filing_doc"] == {}


def test_state_summary_is_log_friendly():
    final = build_filing_graph(FakeNormService()).invoke(_state())
    summary = state_summary(final)

    assert summary["status"] == "saved"
    assert summary["bundles"] == 2
    assert summary["statement_docs"] == 2
    assert "bundles_" not in summary  # no raw payloads
