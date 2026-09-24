"""Terminal presenter — the step-by-step operator narrative."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from filings_agent.presenter import (
    NODE_LABELS,
    StepPresenter,
    format_step_line,
    install_presenter,
)
from filings_agent.hooks import get_call_callback, get_node_callback, set_call_callback, set_node_callback


class Recorder:
    """Captures presenter output instead of printing."""

    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    @property
    def text(self):
        return "\n".join(self.lines)


def _bundle(statement_type="income"):
    from data_normalization_service.core.models import StatementBundle

    return StatementBundle(
        company_cik="0000320193",
        statement_type=statement_type,
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=[{"concept": "us-gaap:Revenues", "value": 1.0}],
    )


# ── per-node summaries ──────────────────────────────────────────────────────


def test_validate_summary_lists_findings_with_verdict():
    line = format_step_line("validate_node", {
        "status": "validated",
        "validation_report": {"status": "fail", "blocking_count": 1,
                              "checks_run": {"math": 2}},
        "findings": [{
            "severity": "high", "type": "math_mismatch", "statement_type": "income",
            "concept": "us-gaap:GrossProfit", "message": "does not reconcile",
        }],
    })
    assert "fail" in line and "1 finding(s) (1 blocking)" in line
    assert "math_mismatch" in line and "us-gaap:GrossProfit" in line


def test_validate_stages_are_labelled_distinctly():
    state = {"status": "validated", "validation_report": {"status": "pass"}, "findings": []}
    assert "[validate]" in format_step_line("validate_node", state)
    assert "[re-validate]" in format_step_line("validate_after_repair_node", state)
    assert "[final validate]" in format_step_line("validate_final_node", state)


def test_normalize_summary_reports_bundle_count_and_types():
    line = format_step_line("normalize_bundle_node", {
        "status": "normalized",
        "statement_docs": [1, 2],
        "bundles": [_bundle("income"), _bundle("balancesheet")],
    })
    assert "2 statement doc(s) → 2 bundle(s)" in line
    assert "income, balancesheet" in line


def test_decide_summary_shows_action_decider_and_ceiling():
    line = format_step_line("decide_node", {
        "status": "decided",
        "decision": {
            "action": "write_partial", "decided_by": "agent",
            "statements": ["income"], "permitted": ["income"], "blocked": ["balancesheet"],
            "drop_concepts": {"income": ["us-gaap:GrossProfit"]},
            "reason": "balance sheet unreliable",
        },
    })
    assert "WRITE_PARTIAL by agent" in line
    assert "blocked=['balancesheet']" in line
    assert "us-gaap:GrossProfit" in line
    assert "balance sheet unreliable" in line


def test_persist_summary_written_and_skipped():
    written = format_step_line("persist_node", {
        "status": "saved",
        "persist_receipt": {"statements_written": 2, "statements_total": 2,
                            "action": "write", "decided_by": "policy",
                            "statements_skipped": [], "dropped_concepts": {}},
    })
    assert "wrote 2/2 statement(s)" in written

    skipped = format_step_line("persist_node", {
        "status": "skipped",
        "persist_receipt": {"decision_reason": "1 unresolved high-severity finding(s)"},
    })
    assert "skipped" in skipped and "unresolved" in skipped


def test_repair_summary_shows_before_after_and_source():
    line = format_step_line("repair_node", {
        "status": "repaired",
        "repair_actions": {
            "proposed": 1, "applied": 1, "rejected": [],
            "applied_details": [{
                "concept": "us-gaap:GrossProfit", "old_value": 999999,
                "new_value": 600000, "source": "deterministic_identity",
            }],
        },
    })
    assert "applied 1/1" in line
    assert "999,999 → 600,000" in line
    assert "deterministic_identity" in line


def test_hierarchy_and_company_summaries():
    assert "seeded hierarchy" in format_step_line("resolve_hierarchy_node", {
        "status": "hierarchy_resolved",
        "hierarchy_plan": {"resolved_concepts": 5, "sources": {"fresh_seed": 5},
                           "seeded_statement_types": ["income"]},
    })
    assert "upserted 3" in format_step_line("save_guidance_node", {
        "guidance_save": {"status": "saved", "upserted": 3, "demoted": 1,
                          "score": {"scored": 2}},
    })
    assert "filled 4" in format_step_line("companyfacts_reconciliation_node", {
        "reconciliation": {"status": "ok", "filled": 4},
    })
    assert "finalized 2" in format_step_line("finalize_validation_reports_node", {
        "report_finalization": {"status": "ok", "finalized": 2},
    })


def test_unknown_node_has_no_summary():
    assert format_step_line("something_else_node", {"status": "x"}) is None


# ── presenter behaviour ─────────────────────────────────────────────────────


# ── detailed mode (AGENT_STEPS=1 / -v) ──────────────────────────────────────


def _detailed(writer):
    return StepPresenter(writer=writer, detailed=True, show_progress_line=True)


def test_detailed_mode_prints_start_summary_and_progress_line():
    rec = Recorder()
    presenter = _detailed(rec)

    presenter.node_callback("normalize_bundle_node", "start", "AAPL", {
        "cik": "0000320193", "accession_number": "acc-1",
        "ticker": "AAPL", "form_type": "10-K",
    })
    presenter.node_callback("normalize_bundle_node", "end", "AAPL", {
        "status": "normalized", "statement_docs": [1], "bundles": [_bundle()],
    }, 12.0)

    text = rec.text
    assert "── AAPL" in text and "10-K" in text and "acc-1" in text
    assert "▶ normalize" in text
    assert "[normalize]" in text
    assert "0.0s" in text


def test_detailed_mode_header_resets_between_filings():
    rec = Recorder()
    presenter = _detailed(rec)
    for accession in ("acc-1", "acc-2"):
        presenter.node_callback("normalize_bundle_node", "start", "AAPL", {
            "cik": "1", "accession_number": accession, "ticker": "AAPL",
        })
    headers = [line for line in rec.lines if line.strip().startswith("── AAPL")]
    assert len(headers) == 2


def test_detailed_mode_forwards_in_node_lines():
    rec = Recorder()
    presenter = _detailed(rec)
    presenter.call_callback("  [llm]  agent step 1  → calling llm  (deepseek)")
    presenter.call_callback("   ")
    assert rec.text.count("[llm]") == 1


# ── step list (default narration) ───────────────────────────────────────────


class StageRecorder:
    """Captures the live per-filing stage text sent to the progress bar."""

    def __init__(self):
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)

    @property
    def last(self):
        return self.texts[-1] if self.texts else ""


def _steps(writer, stage=None):
    return StepPresenter(writer=writer, stage_writer=stage)


def _filing_state(accession="acc-1", form="10-Q"):
    return {"cik": "1", "accession_number": accession, "ticker": "AAPL",
            "form_type": form}


def _run_filing(presenter, *, findings=(), status="saved", guidance=None):
    """Drive one filing through the real node sequence."""
    state = _filing_state()
    presenter.filing_header(state, period_label="FY2026 Q3")
    presenter.node_callback("normalize_bundle_node", "start", "AAPL", state)
    presenter.node_callback("normalize_bundle_node", "end", "AAPL",
                            {"status": "normalized", "bundles": [_bundle()]}, 2.0)
    presenter.node_callback("validate_node", "end", "AAPL", {
        "status": "validated", "findings": list(findings),
        "validation_report": {"status": "pass_with_findings" if findings else "pass",
                              "blocking_count": 0},
    }, 18.0)
    presenter.node_callback("resolve_hierarchy_node", "end", "AAPL", {
        "status": "hierarchy_resolved",
        "hierarchy_plan": {"resolved_concepts": 72,
                           "sources": {"same_company_existing": 72}},
    }, 9.0)
    presenter.node_callback("validate_final_node", "end", "AAPL", {
        "status": "validated", "findings": list(findings),
        "validation_report": {"status": "pass_with_findings" if findings else "pass",
                              "blocking_count": 0},
    }, 2.0)
    presenter.node_callback("decide_node", "end", "AAPL", {
        "status": "decided",
        "decision": (
            {"action": "write", "statements": ["income", "balancesheet", "cashflow"]}
            if status == "saved" else {"action": "skip", "statements": []}
        ),
    }, 1.0)
    presenter.node_callback("persist_node", "end", "AAPL", {
        "status": status, "findings": list(findings),
        "persist_receipt": (
            {"statements_written": 3, "statements_total": 3}
            if status == "saved" else {"decision_reason": "unresolved high finding"}
        ),
    }, 520.0)
    # The graph only routes to guidance after a successful save.
    if status == "saved":
        presenter.node_callback("extract_guidance_node", "end", "AAPL", {
            "status": "saved", "guidance_extract": {"status": "no_mda_text"},
        }, 1.0)
        presenter.node_callback("save_guidance_node", "end", "AAPL", {
            "status": "saved", "guidance_records": guidance or [],
            "guidance_save": (
                {"status": "saved", "upserted": len(guidance), "score": {"scored": 1}}
                if guidance else {"status": "no_records"}
            ),
        }, 1.0)
    return state


def test_step_list_prints_every_main_task_in_order():
    rec = Recorder()
    _run_filing(_steps(rec))
    text = rec.text

    for step in ("normalize", "validate", "hierarchy", "decide", "persist", "guidance"):
        assert f"    {step}" in text, step
    assert "── AAPL" in text and "FY2026 Q3" in text
    assert "3/3 statements written" in text
    assert rec.lines[-1].strip().startswith("✓ written")


def test_step_list_shows_counts_but_not_internal_dicts():
    rec = Recorder()
    _run_filing(_steps(rec), findings=[{
        "severity": "medium", "type": "duplicate_concept", "statement_type": "cashflow",
        "concept": "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "message": "cashflow: concept appears 2 times in one statement",
    }])
    text = rec.text
    assert "pass_with_findings  ·  1 finding" in text
    for noise in ("checks {", "sources {", "permitted=", "duplicate_concept",
                  "us-gaap:CashCashEquivalents"):
        assert noise not in text, noise


def test_repeated_identical_verdict_is_suppressed():
    rec = Recorder()
    _run_filing(_steps(rec))
    # validate and verify have the same verdict -> verify is not repeated.
    assert rec.text.count("    validate") == 1
    assert "verify" not in rec.text


def test_step_detail_per_step():
    rec = Recorder()
    presenter = _steps(rec)
    state = _filing_state()
    presenter.filing_header(state, period_label="FY2026 Q3")

    presenter.node_callback("resolve_hierarchy_node", "end", "AAPL", {
        "status": "hierarchy_resolved",
        "hierarchy_plan": {"resolved_concepts": 70, "sources": {"fresh_seed": 70}},
    }, 5.0)
    assert "70 concepts placed" in rec.text and "seeded" in rec.text

    presenter.node_callback("decide_node", "end", "AAPL", {
        "status": "decided", "decision": {"action": "skip", "statements": []}}, 1.0)
    assert "skip — nothing written" in rec.text

    presenter.node_callback("persist_node", "end", "AAPL", {
        "status": "saved",
        "persist_receipt": {"statements_written": 2, "statements_total": 3,
                            "dropped_concepts": {"income": ["us-gaap:GrossProfit"]}},
    }, 900.0)
    assert "2/3 statements written" in rec.text
    assert "1 concept excluded" in rec.text


def test_blocking_findings_are_shown_once_as_detail():
    rec = Recorder()
    _run_filing(_steps(rec), findings=[{
        "severity": "high", "type": "math_mismatch", "statement_type": "income",
        "concept": "us-gaap:GrossProfit", "message": "does not reconcile",
    }])
    assert rec.text.count("math_mismatch") == 1
    assert "us-gaap:GrossProfit" in rec.text


def test_refusal_prints_a_step_and_a_result_line():
    rec = Recorder()
    _run_filing(_steps(rec), status="skipped")
    assert "skip — nothing written" in rec.text
    assert rec.lines[-1].strip().startswith("– not written")


def test_guidance_step_reports_records_when_present():
    rec = Recorder()
    _run_filing(_steps(rec), guidance=[{"metric": "revenue"}])
    assert "1 record saved" in rec.text
    assert "scored 1" in rec.text


def test_guidance_save_is_silent_without_records():
    rec = Recorder()
    _run_filing(_steps(rec), guidance=None)
    assert "guidance save" not in rec.text


def test_company_steps_are_listed():
    rec = Recorder()
    presenter = _steps(rec)
    presenter.node_callback("quarterly_deaccumulation_node", "end", "AAPL",
                            {"status": "deaccumulated",
                             "deaccumulation": {"status": "ok", "statements": 2}}, 300.0)
    presenter.node_callback("companyfacts_reconciliation_node", "end", "AAPL",
                            {"status": "reconciled",
                             "reconciliation": {"status": "ok", "filled": 1}}, 1200.0)
    presenter.node_callback("finalize_validation_reports_node", "end", "AAPL",
                            {"status": "finalized",
                             "report_finalization": {"status": "ok", "finalized": 2}}, 5.0)
    assert "quarterly" in rec.text and "2 statements deaccumulated" in rec.text
    assert "companyfacts" in rec.text and "1 gap value filled" in rec.text
    assert "reports" in rec.text and "2 reports finalized" in rec.text


def test_company_steps_stay_silent_when_nothing_happened():
    rec = Recorder()
    presenter = _steps(rec)
    presenter.node_callback("quarterly_deaccumulation_node", "end", "AAPL",
                            {"status": "deaccumulated",
                             "deaccumulation": {"status": "no_statements"}}, 1.0)
    presenter.node_callback("companyfacts_reconciliation_node", "end", "AAPL",
                            {"status": "reconciled",
                             "reconciliation": {"status": "unavailable", "filled": 0}}, 1.0)
    assert rec.lines == []


def test_micro_durations_are_not_printed():
    rec = Recorder()
    presenter = _steps(rec)
    presenter.step("validate", "pass", ok=True, duration_ms=12.0)
    presenter.step("persist", "3/3 statements written", ok=True, duration_ms=900.0)
    assert "0.0s" not in rec.text
    assert "0.9s" in rec.text


def test_live_stage_text_shows_the_trail_and_current_step():
    stage = StageRecorder()
    presenter = _steps(Recorder(), stage)
    presenter.node_callback("normalize_bundle_node", "start", "AAPL", {
        "cik": "1", "ticker": "AAPL", "form_type": "10-Q"})
    assert "▶normalize" in stage.last

    presenter.node_callback("normalize_bundle_node", "end", "AAPL", {"status": "normalized"}, 1.0)
    presenter.node_callback("validate_node", "end", "AAPL", {"status": "validated"}, 1.0)
    presenter.node_callback("resolve_hierarchy_node", "start", "AAPL", {"cik": "1"})
    assert "✓norm" in stage.last and "✓valid" in stage.last
    assert "▶hierarchy" in stage.last


def test_quiet_skip_marks_the_live_trail():
    stage = StageRecorder()
    presenter = _steps(Recorder(), stage)
    presenter.node_callback("normalize_bundle_node", "start", "AAPL", {"cik": "1"})
    presenter.node_callback("persist_node", "end", "AAPL", {
        "status": "skipped", "findings": [],
        "persist_receipt": {"decision_reason": "unresolved finding"}}, 1.0)
    assert "✗skip" in stage.last and "✓write" not in stage.last


def test_presenter_never_raises_on_garbage_state():
    rec = Recorder()
    presenter = StepPresenter(writer=rec)
    presenter.node_callback("validate_node", "end", "AAPL", None, None)
    presenter.node_callback("persist_node", "end", "AAPL", {"status": None}, None)
    presenter.node_callback("decide_node", "end", "AAPL", {"decision": None}, None)
    presenter.close("  ✓ done")


def test_every_graph_node_has_a_label():
    for node in (
        "normalize_bundle_node", "validate_node", "agent_review_node", "repair_node",
        "resolve_hierarchy_node", "validate_after_repair_node", "validate_final_node",
        "decide_node", "persist_node", "extract_guidance_node", "save_guidance_node",
        "quarterly_deaccumulation_node", "companyfacts_reconciliation_node",
        "finalize_validation_reports_node",
    ):
        assert node in NODE_LABELS, f"missing label for {node}"


def test_writer_failure_warns_once_and_never_raises():
    attempts = []

    def broken(_message):
        attempts.append(1)
        raise RuntimeError("no tty")

    presenter = StepPresenter(writer=broken)
    presenter.write("a")
    presenter.write("b")
    assert len(attempts) == 2          # both attempted
    assert presenter._write_failures == 2
