"""Unit tests for --guidance-only / --guidence-only mode."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from filings_agent import config
from filings_agent.graph import build_guidance_only_graph
from filings_agent.state import new_state


def test_build_guidance_only_graph_runs_only_guidance_even_when_disabled(monkeypatch):
    """Guidance-only graph must run extract_guidance -> save_guidance even when GUIDANCE_ENABLED=False."""
    monkeypatch.setattr(config, "GUIDANCE_ENABLED", False)
    monkeypatch.setattr(config, "GUIDANCE_LLM_ENABLED", True)

    fake_record = {
        "metric": "revenue",
        "standard_label": "Revenue",
        "statement_type": "income",
        "basis": "gaap",
        "form": "range",
        "low": 100.0,
        "high": 110.0,
        "midpoint": 105.0,
        "unit": "USD",
        "as_printed": "$100M - $110M",
        "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
        "period_type": "quarterly",
    }

    upsert_mock = MagicMock(return_value={"n_records": 1, "upserted": 1, "demoted": 0})
    score_mock = MagicMock(return_value={"scored": 0})
    mda_provider = MagicMock(return_value="Item 2. Management's Discussion and Analysis. Outlook: Revenue $100M-$110M.")

    with patch("filings_agent.agent.loop.run_agent_loop", return_value={"guidance": [fake_record]}), \
         patch("filings_agent.agent.guidance.normalize_guidance_records", return_value=([fake_record], [])), \
         patch("filings_agent.integrations.guidance.upsert_guidance_records", upsert_mock), \
         patch("filings_agent.integrations.guidance.score_guidance_for_cik", score_mock):
        graph = build_guidance_only_graph(mda_provider=mda_provider)
        state = new_state(
            cik="0000320193",
            ticker="AAPL",
            company_name="Apple Inc.",
            form_type="10-Q",
            accession_number="0000320193-26-000001",
            filing_date="2026-05-01",
        )
        state["reporting_period"] = {
            "end_date": "2026-03-31",
            "period_type": "quarterly",
            "fiscal_year": 2026,
            "quarter": 1,
        }

        final = graph.invoke(state)

    assert final["guidance_extract"]["status"] == "extracted"
    assert len(final["guidance_records"]) == 1
    assert final["guidance_save"]["status"] == "saved"
    assert final.get("persist_receipt") is None
    assert final.get("bundles") is None
    upsert_mock.assert_called_once()
    score_mock.assert_called_once()


def test_sec_scraper_cli_accepts_both_guidance_only_spellings():
    """Both --guidence-only and --guidance-only must parse cleanly and pass guidance_only=True."""
    import sec_scraper_cli

    for flag in ("--guidence-only", "--guidance-only"):
        with patch.object(sec_scraper_cli, "SECDataScraperApp") as app_cls:
            app = app_cls.return_value
            app.setup_database.return_value = True
            app.process_multiple_companies.return_value = {
                "0000320193": {"ticker": "AAPL", "processed": 1, "skipped": 0, "failed": 0, "success": True}
            }
            sec_scraper_cli.main(["--companies", "0000320193", flag, "--no-download-html-filings"])
            _, kwargs = app_cls.call_args
            assert kwargs["guidance_only"] is True


def test_sec_scraper_cli_rejects_conflicting_flags_with_guidance_only():
    """--guidence-only must reject --only-download-files and --fix-hierarchy."""
    import sec_scraper_cli

    with pytest.raises(SystemExit) as exc1:
        sec_scraper_cli.main(["--companies", "0000320193", "--guidence-only", "--only-download-files"])
    assert exc1.value.code == 1

    with pytest.raises(SystemExit) as exc2:
        sec_scraper_cli.main(["--companies", "0000320193", "--guidence-only", "--fix-hierarchy"])
    assert exc2.value.code == 1


def test_process_company_guidance_only_skips_statements_and_company_stage():
    """In guidance_only mode, process_company must not run statement extraction, DB company writes, or company stage."""
    import sec_scraper_cli

    app = sec_scraper_cli.SECDataScraperApp.__new__(sec_scraper_cli.SECDataScraperApp)
    app.guidance_only = True
    app.start_year = 2025
    app.end_year = 2026
    app.reload = False
    app.latest = False
    app.target_fiscal_year = None
    app.target_fiscal_quarter = None
    app.target_statement = None
    app.html_download_path = None
    app._presenter = None
    app.db = MagicMock()
    app.progress = MagicMock()
    app.progress.label.return_value = "AAPL"
    app.progress.filing_bar.return_value = MagicMock()
    app.company_transformer = MagicMock()
    app.company_transformer.transform_company_data.return_value = {"cik": "0000320193", "name": "Apple Inc."}
    app.sec_client = MagicMock()
    app.sec_client.get_ticker_from_cik.return_value = "AAPL"
    app.sec_client.get_company_submissions.return_value = (
        {"name": "Apple Inc.", "cik": "0000320193"},
        [
            {"accessionNumber": "0000320193-26-000001", "form": "10-Q", "filingDate": "2026-05-01", "reportDate": "2026-03-31"},
            {"accessionNumber": "0000320193-25-000099", "form": "10-K", "filingDate": "2025-11-01", "reportDate": "2025-09-27"},
        ],
    )

    app._ensure_audit = MagicMock()
    app._get_authoritative_fiscal_year_convention = MagicMock(return_value="end_year")
    app._sort_filings_by_fy_desc_quarter_asc = MagicMock(side_effect=lambda fs, *_: fs)
    app._format_filing_period = MagicMock(return_value="FY2026 Q1")
    app._record_filing_session = MagicMock()
    # Financial statement dedup would return True, but MUST NOT be consulted in guidance_only mode!
    app._find_existing_filing = MagicMock(return_value={"_id": "already-has-statements"})
    # Guidance already exists only for the second filing
    app._find_existing_guidance = MagicMock(side_effect=lambda cik, acc: acc == "0000320193-25-000099")
    app.process_filing_with_full_data = MagicMock(return_value=True)
    app.process_filing_guidance_only = MagicMock(return_value=True)
    app._run_company_agent_stage = MagicMock()

    result = app.process_company("0000320193")

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    app.process_filing_guidance_only.assert_called_once()
    assert app.process_filing_guidance_only.call_args[0][1]["accessionNumber"] == "0000320193-26-000001"
    app.process_filing_with_full_data.assert_not_called()
    app._find_existing_filing.assert_not_called()
    app._run_company_agent_stage.assert_not_called()
    app.db.companies.update_one.assert_not_called()


class _InMemoryCollection:
    """Minimal Mongo-like collection supporting $in, $nin, $ne, $or, $and, dotted paths."""

    def __init__(self):
        self.docs: list[dict] = []
        self._next_id = 1

    @staticmethod
    def _get_field(doc: dict, key: str):
        cur = doc
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    @classmethod
    def _match(cls, doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if k == "$and":
                if not all(cls._match(doc, sub) for sub in v):
                    return False
            elif k == "$or":
                if not any(cls._match(doc, sub) for sub in v):
                    return False
            elif isinstance(v, dict):
                val = cls._get_field(doc, k)
                for op, operand in v.items():
                    if op == "$in" and val not in operand:
                        return False
                    elif op == "$nin" and val in operand:
                        return False
                    elif op == "$ne" and val == operand:
                        return False
            else:
                if cls._get_field(doc, k) != v:
                    return False
        return True

    def find(self, query: dict, projection: dict | None = None):
        return [dict(d) for d in self.docs if self._match(d, query)]

    def find_one(self, query: dict, projection: dict | None = None):
        for d in self.docs:
            if self._match(d, query):
                return dict(d)
        return None

    def update_one(self, query: dict, update: dict, upsert: bool = False):
        res = MagicMock()
        res.upserted_id = None
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                return res
        if upsert:
            new_doc = {"_id": self._next_id}
            self._next_id += 1
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
            res.upserted_id = new_doc["_id"]
        return res

    def update_many(self, query: dict, update: dict):
        count = 0
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                count += 1
        res = MagicMock()
        res.modified_count = count
        return res

    def delete_many(self, query: dict):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, query)]
        res = MagicMock()
        res.deleted_count = before - len(self.docs)
        return res


def test_upsert_guidance_never_duplicates_across_reruns_and_variants(monkeypatch):
    """Rerunning a filing multiple times (with CIK/accession/casing/period_type variants) never doubles/triples rows."""
    from datetime import date
    from filings_agent.integrations import guidance as guidance_int
    from filings_agent.period import DetectedPeriod

    col = _InMemoryCollection()
    fake_db = {
        "guidance_values": col,
        "concepts_standard_mapping": _InMemoryCollection(),
        "normalized_concepts_quarterly": _InMemoryCollection(),
        "normalized_concepts_annual": _InMemoryCollection(),
    }
    monkeypatch.setattr(guidance_int, "_get_db", lambda: fake_db)

    fp = DetectedPeriod(
        period_type="quarterly",
        period_end=date(2026, 3, 31),
        quarter=1,
        period_label="FY2026 Q1",
        fiscal_year=2026,
    )

    rec_v1 = {
        "metric": "Revenue",
        "standard_label": "Total Revenues",
        "concept": "us-gaap:Revenues",
        "statement_type": "income",
        "basis": "gaap",
        "form": "range",
        "value": 105e9,
        "value_low": 100e9,
        "value_high": 110e9,
        "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "current_quarter"},
        "period_type": "quarterly",
    }
    rec_v2 = {
        **rec_v1,
        "metric": "revenue",
        "value": 106e9,
        "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
    }
    # Duplicate in same batch with different metric name but SAME resolved concept
    rec_same_concept = {
        **rec_v2,
        "metric": "total_revenue",
    }

    # Run 1: unpadded CIK + -xbrl accession suffix
    guidance_int.upsert_guidance_records(
        "320193", [rec_v1], accession_number="0000320193-26-000001-xbrl", form_type="10-Q", filing_period=fp
    )
    assert len(col.docs) == 1

    # Run 2: padded CIK + clean accession + drifted period.period_type + duplicate same-concept record in batch
    guidance_int.upsert_guidance_records(
        "0000320193", [rec_v2, rec_same_concept], accession_number="0000320193-26-000001", form_type="10-Q", filing_period=fp
    )
    assert len(col.docs) == 1
    assert col.docs[0]["value"] == 106e9
    assert col.docs[0]["cik"] == "0000320193"
    assert col.docs[0]["accession_number"] == "0000320193-26-000001"

    # Run 3: pre-existing duplicate LLM doc injected + manual doc for another metric
    col.docs.append({**col.docs[0], "_id": 999})
    col.docs.append({
        "_id": 1000,
        "cik": "0000320193",
        "accession_number": "0000320193-26-000001",
        "metric": "capex",
        "basis": "gaap",
        "period": {"fiscal_year": 2026, "quarter": 2, "period_type": "quarterly"},
        "period_type": "quarterly",
        "source": "manual",
        "value": 5e9,
    })
    assert len(col.docs) == 3

    guidance_int.upsert_guidance_records(
        "0000320193", [rec_v2], accession_number="0000320193-26-000001", form_type="10-Q", filing_period=fp
    )
    # Exactly 1 LLM doc + 1 untouched manual doc
    llm_docs = [d for d in col.docs if d["source"] == "llm"]
    manual_docs = [d for d in col.docs if d["source"] == "manual"]
    assert len(llm_docs) == 1
    assert len(manual_docs) == 1

