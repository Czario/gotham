"""P7 — JSONL audit trail."""
import json

from filings_agent.audit import AuditLog, build_audit_log, install_audit
from filings_agent.hooks import (
    get_call_callback,
    get_node_callback,
    report_call,
    set_call_callback,
    set_node_callback,
)


def _cleanup():
    set_call_callback(None)
    set_node_callback(None)


def test_emit_writes_one_json_object_per_line(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.emit("filing_start", cik="1", accession_number="a-1")
    log.emit("filing_end", cik="1", status="saved")

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "filing_start"
    assert first["cik"] == "1"
    assert "ts" in first
    assert len(log.records()) == 2


def test_agent_event_accepts_dict_and_kwargs(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.agent_event({"event": "llm_step", "step": 1, "provider": "deepseek"})
    log.agent_event("tool_call", node="review")

    events = [r["event"] for r in log.records()]
    assert events == ["llm_step", "tool_call"]
    assert log.records()[0]["step"] == 1


def test_agent_event_never_raises(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    # Unserializable value must be handled by the default=str encoder.
    assert log.agent_event("weird", payload=object()) is not None


def test_attach_composes_with_existing_callbacks(tmp_path):
    previous = []
    set_call_callback(lambda msg: previous.append(msg))
    try:
        log = AuditLog(tmp_path / "audit.jsonl").attach()
        report_call("hello")
        assert previous == ["hello"]  # previous callback still fires
        assert any(r["event"] == "call" for r in log.records())
    finally:
        _cleanup()


def test_node_callback_records_status_and_error(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.node_callback("validate", "end", "AAPL", {"status": "validated"}, 12.3)
    log.node_callback("persist", "error", "AAPL", {"status": "failed", "error": "boom"}, 1.0)

    recs = log.records()
    assert recs[0]["node"] == "validate"
    assert recs[0]["status"] == "validated"
    assert recs[0]["elapsed_ms"] == 12.3
    assert recs[1]["error"] == "boom"


def test_install_audit_respects_disable_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("AGENT_AUDIT_ENABLED", "0")
    try:
        assert install_audit() is None
    finally:
        _cleanup()

    monkeypatch.setenv("AGENT_AUDIT_ENABLED", "1")
    try:
        log = install_audit()
        assert log is not None and log.path == tmp_path / "a.jsonl"
        assert get_call_callback() is not None
        assert get_node_callback() is not None
    finally:
        _cleanup()


def test_build_audit_log_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_AUDIT_LOG", raising=False)
    assert build_audit_log() is None
    assert build_audit_log(str(tmp_path / "x.jsonl")) is not None
