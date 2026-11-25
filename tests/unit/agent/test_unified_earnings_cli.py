"""Unit tests for unified ``main --form`` and ``--earnings`` CLI dispatch."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import sec_scraper_cli


@pytest.fixture(autouse=True)
def _isolate_logs_and_report_db(monkeypatch, tmp_path):
    """Keep CLI unit tests from writing to repo logs/ or live MongoDB run_reports."""
    from shared import run_logger
    monkeypatch.setattr(sec_scraper_cli, "_safe_get_report_db", lambda: None)
    monkeypatch.setattr(run_logger, "_DEFAULT_LOGS_DIR", tmp_path)
    _orig_init = run_logger.RunLogSession.__init__

    def _tmp_init(self, ticker, *, log_dir=None, debug=False, now_utc=None):
        _orig_init(self, ticker, log_dir=tmp_path, debug=debug, now_utc=now_utc)

    monkeypatch.setattr(run_logger.RunLogSession, "__init__", _tmp_init)


def test_parse_form_selection_variants():
    assert sec_scraper_cli.parse_form_selection(None) == {
        "mode": "default",
        "xbrl_forms": ["10-K", "10-Q", "20-F", "20-F/A"],
        "earnings_forms": [],
    }
    assert sec_scraper_cli.parse_form_selection("10k-10q") == {
        "mode": "explicit",
        "xbrl_forms": ["10-K", "10-Q"],
        "earnings_forms": [],
    }
    assert sec_scraper_cli.parse_form_selection("10-K,10-Q,8-K") == {
        "mode": "explicit",
        "xbrl_forms": ["10-K", "10-Q"],
        "earnings_forms": ["8-K"],
    }
    assert sec_scraper_cli.parse_form_selection("20f-6k") == {
        "mode": "explicit",
        "xbrl_forms": ["20-F", "20-F/A"],
        "earnings_forms": ["6-K"],
    }
    assert sec_scraper_cli.parse_form_selection("6-K") == {
        "mode": "explicit",
        "xbrl_forms": [],
        "earnings_forms": ["6-K"],
    }
    with pytest.raises(ValueError):
        sec_scraper_cli.parse_form_selection("all")
    with pytest.raises(ValueError):
        sec_scraper_cli.parse_form_selection("invalid-form")


def test_main_form_all_is_rejected():
    with pytest.raises(SystemExit) as exc:
        sec_scraper_cli.main(["--companies", "MSFT", "--form", "all"])
    assert exc.value.code == 1


def test_main_form_6k_without_latest_runs_full_backfill_from_2010():
    with patch("earnings_agents.cli.backfill.main") as mock_backfill:
        sec_scraper_cli.main(["--companies", "ASML", "--form", "6-K"])
    mock_backfill.assert_called_once_with([
        "--ticker", "ASML",
        "--form", "6-K",
        "--from-year", "2010",
    ])


def test_main_form_6k_with_latest_runs_single_latest():
    with patch("earnings_agents.cli.earnings.main") as mock_earnings:
        sec_scraper_cli.main(["--companies", "ASML", "--form", "6-K", "--latest"])
    mock_earnings.assert_called_once_with([
        "--ticker", "ASML",
        "--form", "6-K",
    ])


def test_main_form_8k_with_fiscal_quarter_and_reload():
    with patch("earnings_agents.cli.backfill.main") as mock_backfill:
        sec_scraper_cli.main([
            "--companies", "MSFT",
            "--form", "8-K",
            "--fiscal-year", "2024",
            "--quarter", "Q2",
            "--upsert",
        ])
    mock_backfill.assert_called_once_with([
        "--ticker", "MSFT",
        "--form", "8-K",
        "--fiscal-year", "2024",
        "--fiscal-quarter", "2",
        "--upsert",
    ])


def test_main_form_10k_10q_passes_target_forms_to_scraper():
    with patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls:
        app = app_cls.return_value
        app.setup_database.return_value = True
        app.process_multiple_companies.return_value = {
            "0000789019": {"ticker": "MSFT", "processed": 1, "skipped": 0, "failed": 0, "success": True}
        }
        sec_scraper_cli.main([
            "--companies", "0000789019",
            "--form", "10k-10q",
            "--no-download-html-filings",
        ])
        _, kwargs = app_cls.call_args
        assert kwargs["target_forms"] == ["10-K", "10-Q"]


def test_main_combined_form_runs_8k_first_then_10q_10k():
    """Combined --form 8k-10k-10q (or --from 8k-10k-10q) must process 8-K FIRST, then 10-K/10-Q SECOND."""
    call_order: list[str] = []
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "MSFT"

    def _record_backfill(argv):
        call_order.append("earnings")

    def _make_app(*args, **kwargs):
        call_order.append("xbrl")
        app = MagicMock()
        app.setup_database.return_value = True
        return app

    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp", side_effect=_make_app), \
         patch("earnings_agents.cli.backfill.main", side_effect=_record_backfill):
        sec_scraper_cli.main([
            "--companies", "0000789019",
            "--from", "8k-10k-10q",
            "--year", "2025",
            "--no-download-html-filings",
        ])

    assert call_order == ["earnings", "xbrl"]


def test_main_foreign_20f_6k_runs_20f_first_then_6k():
    """Foreign --form 20f-6k must process annual 20-F FIRST, then 6-K SECOND."""
    call_order: list[str] = []
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "ASML"

    def _record_backfill(argv):
        call_order.append("6-K")

    def _make_app(*args, **kwargs):
        call_order.append("20-F")
        assert kwargs["target_forms"] == ["20-F", "20-F/A"]
        app = MagicMock()
        app.setup_database.return_value = True
        return app

    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp", side_effect=_make_app), \
         patch("earnings_agents.cli.backfill.main", side_effect=_record_backfill):
        sec_scraper_cli.main([
            "--companies", "0000937966",
            "--form", "20f-6k",
            "--year", "2025",
            "--no-download-html-filings",
        ])

    assert call_order == ["20-F", "6-K"]


def test_8k_only_guidance_is_retained_after_10q_runs(monkeypatch):
    """When 8-K saves guidance for metrics A & B and 10-Q only reports A, B stays current."""
    from datetime import date
    from shared import guidance_store
    from filings_agent.period import DetectedPeriod
    from tests.unit.agent.test_guidance_only_mode import _InMemoryCollection

    col = _InMemoryCollection()
    fake_db = {
        "guidance_values": col,
        "concepts_standard_mapping": _InMemoryCollection(),
        "normalized_concepts_quarterly": _InMemoryCollection(),
        "normalized_concepts_annual": _InMemoryCollection(),
    }
    fp = DetectedPeriod("quarterly", date(2026, 3, 31), 1, "FY2026 Q1", 2026)

    rec_a = {
        "metric": "revenue", "standard_label": "Total Revenues",
        "statement_type": "income", "basis": "gaap", "form": "point",
        "value": 100e9, "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
        "period_type": "quarterly",
    }
    rec_b = {
        "metric": "capex", "standard_label": "Capital Expenditure",
        "statement_type": "cashflow", "basis": "gaap", "form": "point",
        "value": 20e9, "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
        "period_type": "quarterly",
    }

    # Pass 1: 8-K writes revenue + capex
    guidance_store.upsert_guidance_records(
        "0000789019", [rec_a, rec_b], accession_number="acc-8k", form_type="8-K", filing_period=fp, db=fake_db
    )
    # Pass 2: 10-Q writes only revenue (updated value)
    guidance_store.upsert_guidance_records(
        "0000789019", [{**rec_a, "value": 102e9}], accession_number="acc-10q", form_type="10-Q", filing_period=fp, db=fake_db
    )

    current = {d["metric"]: d for d in col.docs if d.get("is_current")}
    assert current["revenue"]["value"] == 102e9
    assert current["revenue"]["form_type"] == "10-Q"
    assert current["capex"]["value"] == 20e9
    assert current["capex"]["form_type"] == "8-K"


def test_main_combined_form_with_latest_picks_single_newest_filing():
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "MSFT"
    fake_client.get_company_submissions.return_value = (
        {"name": "Microsoft"},
        [
            {"form": "8-K", "filingDate": "2026-05-02"},
            {"form": "10-Q", "filingDate": "2026-04-30"},
        ],
    )
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.earnings.main") as mock_earnings:
        sec_scraper_cli.main([
            "--companies", "0000789019",
            "--form", "10k-10q-8k",
            "--latest",
            "--no-download-html-filings",
        ])
    app_cls.assert_not_called()
    mock_earnings.assert_called_once_with([
        "--cik", "0000789019",
        "--form", "8-K",
    ])


def test_main_without_form_processes_nothing():
    """Calling main without --form must exit with code 1 and process nothing."""
    with patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.earnings.main") as mock_earnings, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        with pytest.raises(SystemExit) as exc:
            sec_scraper_cli.main(["--companies", "MSFT"])
    assert exc.value.code == 1
    app_cls.assert_not_called()
    mock_earnings.assert_not_called()
    mock_backfill.assert_not_called()


def test_main_earnings_failures():
    with patch("earnings_agents.cli.failures.main") as mock_failures:
        sec_scraper_cli.main(["--failures", "--companies", "MSFT"])
    mock_failures.assert_called_once_with(["--ticker", "MSFT"])


def test_main_guidance_only_with_combined_forms():
    """--guidence-only with combined forms runs guidance-only on both XBRL and earnings."""
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "ASML"
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        app_cls.return_value.setup_database.return_value = True
        sec_scraper_cli.main([
            "--companies", "0000937966",
            "--guidence-only",
            "--form", "20f-6k",
            "--year", "2024",
            "--no-download-html-filings",
        ])
    _, kwargs = app_cls.call_args
    assert kwargs["guidance_only"] is True
    assert kwargs["target_forms"] == ["20-F", "20-F/A"]
    mock_backfill.assert_called_once_with([
        "--cik", "0000937966",
        "--form", "6-K",
        "--from-year", "2024",
        "--guidance-only",
    ])


def test_earnings_guidance_only_graph_skips_statements_and_q4():
    """earnings_agents.graph.build_guidance_only_graph runs fetch -> detect -> extract_guidance_only -> save_guidance only."""
    from earnings_agents import graph as e_graph

    fake_record = {
        "metric": "revenue",
        "standard_label": "Total Revenues",
        "statement_type": "income",
        "basis": "gaap",
        "form": "point",
        "value": 100e9,
        "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
        "period_type": "quarterly",
    }

    def fetch_mock(s):
        return {**s, "raw_text": "Outlook: Revenue $100B", "status": "fetched"}

    def detect_mock(s):
        return {
            **s,
            "detected_period": {
                "period_type": "quarterly",
                "period_end": "2026-03-31",
                "quarter": 1,
                "period_label": "Q1 FY2026",
                "fiscal_year": 2026,
            },
        }

    extract_mock = MagicMock(return_value={
        "guidance_records": [fake_record],
        "guidance_extract": {"status": "extracted"},
    })
    upsert_mock = MagicMock(return_value={"n_records": 1, "upserted": 1, "skipped_manual": 0, "demoted": 0})
    score_mock = MagicMock(return_value={"checked": 0, "scored": 0})
    save_statements_mock = MagicMock()
    q4_mock = MagicMock()

    with patch.object(e_graph, "fetch_filing_node", fetch_mock), \
         patch.object(e_graph, "detect_period_node", detect_mock), \
         patch.object(e_graph, "mongodb_save_node", save_statements_mock), \
         patch.object(e_graph, "calculate_q4_node", q4_mock), \
         patch("filings_agent.nodes.guidance.make_guidance_extract_node", return_value=extract_mock), \
         patch("earnings_agents.integrations.guidance.upsert_guidance_records", upsert_mock), \
         patch("earnings_agents.integrations.guidance.score_guidance_for_cik", score_mock):
        g = e_graph.build_guidance_only_graph()
        final = g.invoke({
            "ticker": "MSFT",
            "company_name": "Microsoft",
            "cik": "0000789019",
            "accession_number": "0000789019-26-000010",
            "source_form_type": "8-K",
            "guidance_only": True,
            "status": "pending",
        })

    assert final["status"] == "saved"
    assert final["guidance_save"]["status"] == "saved"
    upsert_mock.assert_called_once()
    save_statements_mock.assert_not_called()
    q4_mock.assert_not_called()


def test_auto_q4_runs_only_for_annual_forms_never_10q():
    """_accumulate_for_quarterly must run _auto_calculate_q4_if_ready ONLY for annual forms, never 10-Q."""
    app = sec_scraper_cli.SECDataScraperApp.__new__(sec_scraper_cli.SECDataScraperApp)
    app._quarterly_accumulator = {}
    app._deaccumulate_quarterly_cashflow_immediately = MagicMock()
    app._auto_calculate_q4_if_ready = MagicMock()

    for stmt in ("income", "balancesheet", "cashflow"):
        app._accumulate_for_quarterly(
            "0000789019",
            {"statement_type": stmt, "reporting_period": {"fiscal_year": 2026, "quarter": 1}},
            {"form_type": "10-Q", "accession_number": "acc-10q"},
        )
    app._auto_calculate_q4_if_ready.assert_not_called()

    app._accumulate_for_quarterly(
        "0000789019",
        {"statement_type": "income", "reporting_period": {"fiscal_year": 2025}},
        {"form_type": "10-K", "accession_number": "acc-10k"},
    )
    app._auto_calculate_q4_if_ready.assert_called_once()


def test_msft_fiscal_calendar_8k_manifest_period_resolution():
    """MSFT (FYE 0630): 2025-10-29 8-K is FY2026 Q1, 2026-01-28 8-K is FY2026 Q2."""
    from earnings_agents.integrations.edgar import _resolve_manifest_fiscal_period

    q1_filing = {"form": "8-K", "filing_date": "2025-10-29", "report_date": "2025-10-29"}
    q2_filing = {"form": "8-K", "filing_date": "2026-01-28", "report_date": "2026-01-28"}
    assert _resolve_manifest_fiscal_period(q1_filing, "0630") == (2026, 1)
    assert _resolve_manifest_fiscal_period(q2_filing, "0630") == (2026, 2)


def test_check_period_node_protects_official_xbrl_and_checks_target_quarter():
    """8-K must never overwrite official 10-Q/10-K data or process the wrong quarter."""
    from earnings_agents.nodes.check import check_period_node

    base_state = {
        "ticker": "MSFT",
        "company_name": "Microsoft",
        "cik": "0000789019",
        "detected_period": {
            "period_type": "quarterly",
            "period_end": "2025-12-31",
            "quarter": 2,
            "period_label": "Three Months Ended December 31, 2025",
            "fiscal_year": 2026,
        },
        "status": "detected",
    }

    res_mismatch = check_period_node({**base_state, "target_fiscal_year": 2026, "target_fiscal_quarter": 1})
    assert res_mismatch["status"] == "skipped"
    assert "mismatch" in res_mismatch["error"]

    with patch("earnings_agents.cli.check.fiscal_period_exists", return_value=True) if False else patch("earnings_agents.nodes.check.fiscal_period_exists", return_value=True), \
         patch("earnings_agents.nodes.check.official_filing_period_exists", return_value=True):
        res_protected = check_period_node(base_state)
    assert res_protected["status"] == "skipped"
    assert "official" in res_protected["error"]


def test_replace_and_upsert_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        sec_scraper_cli.main(["--companies", "MSFT", "--form", "10-Q", "--upsert", "--replace"])
    assert exc.value.code == 1


def test_replace_deletes_existing_period_before_fresh_insert_in_xbrl_and_8k():
    """--replace deletes existing rows for the target period before inserting fresh; --upsert does not."""
    # 1. XBRL _delete_reload_data: upsert (replace=False) does NOT delete; replace=True DOES delete
    app = sec_scraper_cli.SECDataScraperApp.__new__(sec_scraper_cli.SECDataScraperApp)
    app.reload = True
    app.replace = False
    app.latest = False
    app.target_fiscal_year = 2026
    app.target_fiscal_quarter = "Q1"
    app.target_statement = None
    app.progress = MagicMock()
    app._cv_annual_col = MagicMock()
    app._cv_quarterly_col = MagicMock()
    app._cv_annual_col.delete_many.return_value = MagicMock(deleted_count=0)
    app._cv_quarterly_col.delete_many.return_value = MagicMock(deleted_count=5)
    app._build_reload_filter = MagicMock(return_value={"cik": "0000789019", "reporting_period.fiscal_year": 2026, "reporting_period.quarter": 1})

    app._delete_reload_data("0000789019", {"form": "10-Q"})
    app._cv_quarterly_col.delete_many.assert_not_called()

    app.replace = True
    app._delete_reload_data("0000789019", {"form": "10-Q"})
    app._cv_quarterly_col.delete_many.assert_called_once()

    # 2. 8-K upsert_concept_values with hard_replace=True deletes FIRST, then bulk_write
    from datetime import date
    from earnings_agents.agent.period import DetectedPeriod
    from earnings_agents.integrations import normalize as e_norm

    events: list[str] = []
    fake_col = MagicMock()
    fake_col.delete_many.side_effect = lambda filt: (events.append("delete_many"), MagicMock(deleted_count=3))[1]
    fake_col.bulk_write.side_effect = lambda ops, ordered=False: events.append("bulk_write")
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_col
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    period = DetectedPeriod("quarterly", date(2026, 3, 31), 1, "Q1 FY2026", 2026)
    with patch.object(e_norm, "_get_client", return_value=fake_client), \
         patch.object(e_norm, "_values_collection", return_value="concept_values_quarterly"):
        e_norm.upsert_concept_values(
            cik="0000789019",
            company_name="Microsoft",
            concept_metrics={"5072e8e0c9d3f4a1b2c3d4e5": 42.0},
            period=period,
            value_metadata_by_id={"5072e8e0c9d3f4a1b2c3d4e5": {"currency": "USD"}},
            hard_replace=True,
        )
    assert events == ["delete_many", "bulk_write"]


def test_main_pdf_and_url_flags_dispatch_to_earnings_cli(tmp_path):
    pdf_file = tmp_path / "q3_release.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\n")

    with patch("earnings_agents.cli.earnings.main") as mock_earnings:
        sec_scraper_cli.main([
            "--companies", "MSFT",
            "--form", "8-K",
            "--pdf", str(pdf_file),
            "--upsert",
        ])
    mock_earnings.assert_called_once_with([
        "--ticker", "MSFT",
        "--form", "8-K",
        "--pdf", str(pdf_file.resolve()),
        "--upsert",
    ])

    with patch("earnings_agents.cli.earnings.main") as mock_earnings:
        sec_scraper_cli.main([
            "--companies", "ASML",
            "--form", "6-K",
            "--filing-url", "https://example.com/release.pdf",
            "--replace",
        ])
    mock_earnings.assert_called_once_with([
        "--ticker", "ASML",
        "--form", "6-K",
        "--filing-url", "https://example.com/release.pdf",
        "--replace",
    ])

    # --pdf and --url together must be rejected
    with pytest.raises(SystemExit) as exc:
        sec_scraper_cli.main([
            "--companies", "MSFT",
            "--form", "8-K",
            "--pdf", str(pdf_file),
            "--url", "https://example.com/release.pdf",
        ])
    assert exc.value.code == 1
