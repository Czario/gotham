"""Per-accession filing ledger and non-destructive 20-F amendment handling."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from sec_scraper_cli import SECDataScraperApp


def _app():
    app = SECDataScraperApp.__new__(SECDataScraperApp)
    app.db = MagicMock()
    app._foreign_filing_collection_ready = True
    app.filing_transformer = MagicMock()
    app._make_filing_id = MagicMock(return_value="stable-object-id")
    app.filing_transformer.transform_filing_data.return_value = {
        "cik": "0000937966",
        "accession_number": "0001628280-26-011378",
        "form_type": "20-F",
        "filing_date": datetime(2026, 2, 25),
        "acceptance_datetime": None,
        "created_at": datetime(2026, 2, 25),
    }
    return app


def test_foreign_filing_ledger_upserts_each_accession_with_original_form():
    app = _app()
    filing = {
        "form": "20-F/A",
        "accessionNumber": "0001628280-26-011378",
        "filingDate": "2026-02-25",
        "reportDate": "2025-12-31",
        "primaryDocument": "asml-20251231.htm",
        "primaryDocDescription": "20-F/A",
        "isXBRL": True,
    }

    app.filing_transformer.transform_filing_data.return_value["form_type"] = "20-F/A"
    assert app._record_foreign_annual_filing("937966", filing)

    app.filing_transformer.transform_filing_data.assert_called_once_with(filing, "0000937966")
    app.db["filings"].update_one.assert_called_once()
    query, update = app.db["filings"].update_one.call_args.args
    assert query == {
        "cik": "0000937966",
        "accession_number": "0001628280-26-011378",
    }
    assert update["$set"]["form_type"] == "20-F/A"
    assert update["$set"]["report_date"] == datetime(2025, 12, 31)
    assert update["$set"]["primary_document"] == "asml-20251231.htm"
    assert update["$set"]["is_xbrl"] is True
    assert update["$setOnInsert"]["_id"] == "stable-object-id"


@pytest.mark.parametrize(
    "raw_value, expected",
    [(1, True), (0, False), ("1", True), ("0", False), (True, True), ("no", False)],
)
def test_sec_is_xbrl_metadata_is_normalized_to_bson_boolean(raw_value, expected):
    app = _app()
    filing = {
        "form": "20-F",
        "accessionNumber": "annual",
        "filingDate": "2026-02-25",
        "isXBRL": raw_value,
    }

    assert app._record_foreign_annual_filing("937966", filing)
    saved = app.db["filings"].update_one.call_args.args[1]["$set"]
    assert saved["is_xbrl"] is expected


def test_non_foreign_form_does_not_create_a_foreign_ledger_record():
    app = _app()

    assert app._record_foreign_annual_filing(
        "0000320193", {"form": "10-K", "accessionNumber": "domestic"}
    )
    app.db["filings"].update_one.assert_not_called()


def test_complete_annual_amendment_requires_all_three_core_statements():
    complete = [("income", {}), ("balancesheet", {}), ("cashflow", {})]
    partial = [("income", {}), ("balancesheet", {})]

    assert SECDataScraperApp._has_complete_annual_statement_set(complete)
    assert not SECDataScraperApp._has_complete_annual_statement_set(partial)


def test_failed_ledger_write_fails_closed_for_foreign_annual_filing(mocker):
    app = _app()
    app._foreign_filing_collection_ready = False
    mocker.patch(
        "database.config.mongodb_config.ensure_filing_metadata_collection",
        side_effect=RuntimeError("collMod denied"),
    )

    assert not app._record_foreign_annual_filing(
        "937966",
        {
            "form": "20-F",
            "accessionNumber": "acc",
            "filingDate": "2026-02-25",
        },
    )
    app.db["filings"].update_one.assert_not_called()
