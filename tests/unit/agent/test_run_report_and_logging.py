"""Unit tests for per-run UTC serial log files and DB/console run summary reports."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.hooks import report_call
from shared.run_logger import RunLogSession, build_run_log_path
from shared.run_report import (
    build_run_report,
    diff_stage,
    format_run_report_text,
    save_run_report,
    snapshot_company_state,
)


class _MemCollection:
    """Minimal in-memory MongoDB collection for run_report tests."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: list[dict[str, Any]] = list(docs or [])
        self._next_id = 1

    def find(self, filt: dict[str, Any] | None = None, projection: Any = None):
        filt = filt or {}
        out = []
        for doc in self.docs:
            if self._matches(doc, filt):
                out.append(dict(doc))
        return out

    def insert_one(self, doc: dict[str, Any]):
        d = dict(doc)
        oid = f"rep_{self._next_id}"
        self._next_id += 1
        d["_id"] = oid
        self.docs.append(d)

        class _Res:
            inserted_id = oid

        return _Res()

    @staticmethod
    def _get_nested(doc: dict[str, Any], dotted: str) -> Any:
        cur: Any = doc
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def _matches(self, doc: dict[str, Any], filt: dict[str, Any]) -> bool:
        for k, v in filt.items():
            actual = self._get_nested(doc, k)
            if isinstance(v, dict) and "$in" in v:
                if actual not in v["$in"]:
                    return False
            elif actual != v:
                return False
        return True


def test_build_run_log_path_serial_ticker_utc(tmp_path: Path):
    dt = datetime(2026, 11, 24, 6, 15, 30, tzinfo=timezone.utc)
    s1, p1 = build_run_log_path("aapl", log_dir=tmp_path, now_utc=dt)
    assert s1 == "0001"
    assert p1.name == "0001-AAPL-20261124T061530Z.log"
    p1.write_text("first\n", encoding="utf-8")

    s2, p2 = build_run_log_path(["MSFT", "AAPL"], log_dir=tmp_path, now_utc=dt)
    assert s2 == "0002"
    assert p2.name == "0002-MSFT_AAPL-20261124T061530Z.log"


def test_run_log_session_captures_pipeline_trace(tmp_path: Path):
    dt = datetime(2026, 11, 24, 7, 0, 0, tzinfo=timezone.utc)
    session = RunLogSession("AAPL", log_dir=tmp_path, now_utc=dt)
    log_path = session.attach()
    try:
        report_call("  [llm]  agent step 1 -> calling llm (deepseek)")
        logging.getLogger("test.module").info("hello file log")
    finally:
        session.close()

    text = log_path.read_text(encoding="utf-8")
    assert "[llm]  agent step 1 -> calling llm (deepseek)" in text
    assert "hello file log" in text
    assert log_path.name == "0001-AAPL-20261124T070000Z.log"


def test_two_stage_8k_then_10q_snapshot_diff_and_report():
    db = {
        "normalized_concepts_quarterly": _MemCollection([
            {"_id": "c_rev", "cik": "0000320193", "concept": "us-gaap:Revenues", "label": "Total Revenue", "statement_type": "income"},
            {"_id": "c_ni", "cik": "0000320193", "concept": "us-gaap:NetIncomeLoss", "label": "Net Income", "statement_type": "income"},
            {"_id": "c_book", "cik": "0000320193", "concept": "custom:Bookings", "label": "Bookings", "statement_type": "income"},
            {"_id": "c_oi", "cik": "0000320193", "concept": "us-gaap:OperatingIncomeLoss", "label": "Operating Income", "statement_type": "income"},
        ]),
        "normalized_concepts_annual": _MemCollection([]),
        "concept_values_quarterly": _MemCollection([]),
        "concept_values_annual": _MemCollection([]),
        "guidance_values": _MemCollection([]),
        "gotham_run_reports": _MemCollection([]),
    }

    # Snapshot 0: empty
    s0 = snapshot_company_state(db, "0000320193", fiscal_year=2026, quarter="Q3")

    # Stage 1: 8-K inserts Revenue, Net Income, Bookings + 2 guidance items
    db["concept_values_quarterly"].docs.extend([
        {
            "cik": "0000320193", "concept_id": "c_rev", "statement_type": "income",
            "form_type": "8-K", "earning_data": True, "value": 100.0,
            "reporting_period": {"fiscal_year": 2026, "quarter": 3},
        },
        {
            "cik": "0000320193", "concept_id": "c_ni", "statement_type": "income",
            "form_type": "8-K", "earning_data": True, "value": 50.0,
            "reporting_period": {"fiscal_year": 2026, "quarter": 3},
        },
        {
            "cik": "0000320193", "concept_id": "c_book", "statement_type": "income",
            "form_type": "8-K", "earning_data": True, "value": 30.0,
            "reporting_period": {"fiscal_year": 2026, "quarter": 3},
        },
    ])
    db["guidance_values"].docs.extend([
        {
            "cik": "0000320193", "metric": "revenue", "standard_label": "Total Revenue",
            "basis": "gaap", "period": {"fiscal_year": 2026, "quarter": 4},
            "period_type": "quarterly", "midpoint": 110.0, "unit": "USD",
            "form_type": "8-K", "accession_number": "acc-8k", "is_current": True,
        },
        {
            "cik": "0000320193", "metric": "capex", "standard_label": "Capital Expenditure",
            "basis": "gaap", "period": {"fiscal_year": 2026, "quarter": 4},
            "period_type": "quarterly", "midpoint": 20.0, "unit": "USD",
            "form_type": "8-K", "accession_number": "acc-8k", "is_current": True,
        },
    ])
    s1 = snapshot_company_state(db, "0000320193", fiscal_year=2026, quarter="Q3")
    stage_8k = diff_stage(s0, s1, stage_name="8-K", stage_forms=["8-K"], duration_seconds=12.3)

    assert stage_8k["concepts"]["inserted_count"] == 3
    assert stage_8k["concepts"]["updated_count"] == 0
    assert stage_8k["guidance"]["inserted_count"] == 2

    # Stage 2: 10-Q updates Revenue (100->101) and Net Income (form 8-K->10-Q),
    # newly inserts Operating Income (60), leaves Bookings (8-K) intact,
    # updates revenue guidance to 10-Q (112) and leaves capex guidance (8-K) intact.
    for doc in db["concept_values_quarterly"].docs:
        if doc["concept_id"] == "c_rev":
            doc["value"] = 101.0
            doc["form_type"] = "10-Q"
            doc["earning_data"] = False
        elif doc["concept_id"] == "c_ni":
            doc["form_type"] = "10-Q"
            doc["earning_data"] = False
    db["concept_values_quarterly"].docs.append({
        "cik": "0000320193", "concept_id": "c_oi", "statement_type": "income",
        "form_type": "10-Q", "earning_data": False, "value": 60.0,
        "reporting_period": {"fiscal_year": 2026, "quarter": 3},
    })
    for gdoc in db["guidance_values"].docs:
        if gdoc["metric"] == "revenue":
            gdoc["midpoint"] = 112.0
            gdoc["form_type"] = "10-Q"
            gdoc["accession_number"] = "acc-10q"

    s2 = snapshot_company_state(db, "0000320193", fiscal_year=2026, quarter="Q3")
    stage_10q = diff_stage(s1, s2, stage_name="10-K/10-Q", stage_forms=["10-K", "10-Q"], duration_seconds=9.5)

    assert stage_10q["concepts"]["inserted_count"] == 1
    assert stage_10q["concepts"]["inserted"][0]["label"] == "Operating Income"
    assert stage_10q["concepts"]["updated_count"] == 2
    assert stage_10q["concepts"]["retained_from_earnings_count"] == 1
    assert stage_10q["concepts"]["retained_from_earnings"][0]["label"] == "Bookings"
    assert stage_10q["guidance"]["updated_count"] == 1
    assert stage_10q["guidance"]["retained_from_earnings_count"] == 1
    assert stage_10q["guidance"]["retained_from_earnings"][0]["metric"] == "capex"

    started = datetime(2026, 11, 24, 6, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 11, 24, 6, 0, 22, tzinfo=timezone.utc)
    report = build_run_report(
        serial_number="0001",
        ticker="AAPL",
        cik="0000320193",
        forms_requested="8k-10k-10q",
        mode="upsert",
        log_file="logs/0001-AAPL-20261124T060000Z.log",
        started_at_utc=started,
        finished_at_utc=finished,
        stages=[stage_8k, stage_10q],
        target_fiscal_year=2026,
        target_quarter="Q3",
    )
    oid = save_run_report(db, report)
    assert oid == "rep_1"
    assert len(db["gotham_run_reports"].docs) == 1
    assert report["totals"]["concepts_retained_from_earnings"] == 1
    assert report["totals"]["guidance_retained_from_earnings"] == 1

    rendered = format_run_report_text(report)
    assert "RUN SUMMARY  #0001" in rendered
    assert "normalize_data.gotham_run_reports (rep_1)" in rendered
    assert "logs/0001-AAPL-20261124T060000Z.log" in rendered
    assert "Retained as-is from 8-K/6-K" in rendered
    assert "Bookings" in rendered
    assert "Capital Expenditure" in rendered
