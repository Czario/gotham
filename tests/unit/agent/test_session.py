"""P7 — durable run session (resume + ledger)."""
from filings_agent.session import RunSession, build_run_session, build_session


def test_session_records_and_summarizes(tmp_path):
    session = RunSession(tmp_path / "session.jsonl")
    session.record_filing(cik="1", accession_number="a-1", status="saved", form_type="10-K")
    session.record_filing(cik="1", accession_number="a-2", status="failed", error="boom")
    session.record_company(cik="1", ticker="TEST", status="finalized")

    summary = session.summary()
    assert summary["filings"] == 2
    assert summary["counts"] == {"saved": 1, "failed": 1}
    assert summary["run_id"] == session.run_id
    assert len(summary["companies"]) == 1


def test_is_saved_and_saved_accessions(tmp_path):
    session = RunSession(tmp_path / "session.jsonl")
    assert session.is_saved("a-1") is False
    session.record_filing(cik="1", accession_number="a-1", status="saved")
    session.record_filing(cik="1", accession_number="a-2", status="failed")
    assert session.is_saved("a-1") is True
    assert session.is_saved("a-2") is False
    assert session.saved_accessions() == {"a-1"}
    assert session.latest_status("a-2") == "failed"


def test_latest_status_wins(tmp_path):
    session = RunSession(tmp_path / "session.jsonl")
    session.record_filing(cik="1", accession_number="a-1", status="failed")
    session.record_filing(cik="1", accession_number="a-1", status="saved")
    assert session.latest_status("a-1") == "saved"
    assert session.is_saved("a-1") is True


def test_resume_across_processes(tmp_path):
    path = tmp_path / "session.jsonl"
    first = RunSession(path)
    first.record_filing(cik="1", accession_number="a-1", status="saved")

    # A new run (fresh process) reads the ledger back for resume.
    second = RunSession(path)
    assert second.run_id != first.run_id
    assert second.is_saved("a-1") is True
    assert second.saved_accessions() == {"a-1"}


def test_run_id_tagged_on_every_record(tmp_path):
    session = RunSession(tmp_path / "session.jsonl")
    session.record_filing(cik="1", accession_number="a-1", status="saved")
    session.record_company(cik="1", ticker="T", status="finalized")
    assert {r["run_id"] for r in session.records()} == {session.run_id}


def test_build_session_defaults_and_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_FILE", str(tmp_path / "s.jsonl"))
    assert build_session().path == tmp_path / "s.jsonl"
    assert build_run_session().path == tmp_path / "s.jsonl"

    monkeypatch.delenv("AGENT_SESSION_FILE", raising=False)
    assert build_run_session() is None  # opt-in variant
