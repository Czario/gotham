"""Unit tests for unified ``main --form`` and ``--earnings`` CLI dispatch."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import sec_scraper_cli


def test_parse_form_selection_variants():
    assert sec_scraper_cli.parse_form_selection(None) == {
        "mode": "default",
        "xbrl_forms": ["10-K", "10-Q", "20-F", "20-F/A"],
        "earnings_forms": [],
    }
    assert sec_scraper_cli.parse_form_selection("all")["mode"] == "all"
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
        sec_scraper_cli.parse_form_selection("invalid-form")


def test_detect_company_issuer_forms():
    us_filings = [{"form": "10-K"}, {"form": "10-Q"}, {"form": "8-K"}]
    assert sec_scraper_cli.detect_company_issuer_forms(us_filings) == (
        ["10-K", "10-Q"],
        ["8-K"],
    )
    foreign_filings = [{"form": "20-F"}, {"form": "6-K"}]
    assert sec_scraper_cli.detect_company_issuer_forms(foreign_filings) == (
        ["20-F", "20-F/A"],
        ["6-K"],
    )


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
            "--reload",
        ])
    mock_backfill.assert_called_once_with([
        "--ticker", "MSFT",
        "--form", "8-K",
        "--fiscal-year", "2024",
        "--fiscal-quarter", "2",
        "--reload",
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


def test_main_form_all_us_company_runs_xbrl_then_8k():
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "MSFT"
    fake_client.get_company_submissions.return_value = (
        {"name": "Microsoft"},
        [
            {"form": "10-Q", "filingDate": "2026-04-30"},
            {"form": "8-K", "filingDate": "2026-04-25"},
            {"form": "10-K", "filingDate": "2025-07-30"},
        ],
    )
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        app = app_cls.return_value
        app.setup_database.return_value = True
        sec_scraper_cli.main([
            "--companies", "0000789019",
            "--form", "all",
            "--year", "2024",
            "--no-download-html-filings",
        ])
    _, kwargs = app_cls.call_args
    assert kwargs["target_forms"] == ["10-K", "10-Q"]
    mock_backfill.assert_called_once_with([
        "--cik", "0000789019",
        "--form", "8-K",
        "--from-year", "2024",
    ])


def test_main_form_all_foreign_company_runs_20f_then_6k():
    fake_client = MagicMock()
    fake_client.get_ticker_from_cik.return_value = "ASML"
    fake_client.get_company_submissions.return_value = (
        {"name": "ASML Holding"},
        [
            {"form": "6-K", "filingDate": "2026-04-15"},
            {"form": "20-F", "filingDate": "2026-02-20"},
        ],
    )
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=fake_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        app = app_cls.return_value
        app.setup_database.return_value = True
        sec_scraper_cli.main([
            "--companies", "0000937966",
            "--form", "all",
            "--year", "2024",
            "--no-download-html-filings",
        ])
    _, kwargs = app_cls.call_args
    assert kwargs["target_forms"] == ["20-F", "20-F/A"]
    mock_backfill.assert_called_once_with([
        "--cik", "0000937966",
        "--form", "6-K",
        "--from-year", "2024",
    ])


def test_main_form_all_with_latest_picks_single_newest_filing():
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
            "--form", "all",
            "--latest",
            "--no-download-html-filings",
        ])
    app_cls.assert_not_called()
    mock_earnings.assert_called_once_with([
        "--cik", "0000789019",
        "--form", "8-K",
    ])


def test_main_earnings_failures():
    with patch("earnings_agents.cli.failures.main") as mock_failures:
        sec_scraper_cli.main(["--failures", "--companies", "MSFT"])
    mock_failures.assert_called_once_with(["--ticker", "MSFT"])


def test_main_guidance_only_with_from_all_covers_us_and_foreign_forms():
    """--guidence-only --from all (or --form all) runs guidance-only across ALL relevant company forms."""
    # US company → 10-K/10-Q (guidance_only=True) + 8-K (--guidance-only)
    us_client = MagicMock()
    us_client.get_ticker_from_cik.return_value = "MSFT"
    us_client.get_company_submissions.return_value = (
        {"name": "Microsoft"},
        [{"form": "10-K", "filingDate": "2025-07-30"}, {"form": "8-K", "filingDate": "2026-04-25"}],
    )
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=us_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        app_cls.return_value.setup_database.return_value = True
        sec_scraper_cli.main([
            "--companies", "0000789019",
            "--guidence-only",
            "--from", "all",
            "--year", "2024",
            "--no-download-html-filings",
        ])
    _, kwargs = app_cls.call_args
    assert kwargs["guidance_only"] is True
    assert kwargs["target_forms"] == ["10-K", "10-Q"]
    mock_backfill.assert_called_once_with([
        "--cik", "0000789019",
        "--form", "8-K",
        "--from-year", "2024",
        "--guidance-only",
    ])

    # Foreign company → 20-F/20-F/A (guidance_only=True) + 6-K (--guidance-only)
    foreign_client = MagicMock()
    foreign_client.get_ticker_from_cik.return_value = "ASML"
    foreign_client.get_company_submissions.return_value = (
        {"name": "ASML"},
        [{"form": "20-F", "filingDate": "2026-02-20"}, {"form": "6-K", "filingDate": "2026-04-15"}],
    )
    with patch.object(sec_scraper_cli, "SECAPIClient", return_value=foreign_client), \
         patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls, \
         patch("earnings_agents.cli.backfill.main") as mock_backfill:
        app_cls.return_value.setup_database.return_value = True
        sec_scraper_cli.main([
            "--companies", "0000937966",
            "--guidence-only",
            "--form", "all",
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
