"""P6 — forward-looking MD&A guidance extraction and persistence."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage

from data_normalization_service.core.models import StatementBundle
from filings_agent.agent.guidance import normalize_guidance_records
from filings_agent.agent.prompts import GUIDANCE_SYSTEM_PROMPT
from filings_agent.mda import extract_mda_section, fetch_mda_text, html_to_text
from filings_agent.nodes.guidance import (
    filing_currency,
    make_guidance_extract_node,
    make_guidance_save_node,
)
from filings_agent.period import build_detected_period, format_period_label


# ── period contract ─────────────────────────────────────────────────────────


def test_build_detected_period_annual_from_10k():
    period = build_detected_period(
        {"end_date": "2024-12-31", "fiscal_year": 2024}, form_type="10-K"
    )
    assert period.period_type == "annual"
    assert period.period_end.isoformat() == "2024-12-31"
    assert period.fiscal_year == 2024
    assert period.quarter is None
    assert format_period_label(period) == "FY2024 (annual)"


def test_build_detected_period_quarterly_from_10q():
    period = build_detected_period(
        {"end_date": "2024-06-30", "fiscal_year": 2024, "quarter": 2}, form_type="10-Q"
    )
    assert period.period_type == "quarterly"
    assert period.quarter == 2
    assert format_period_label(period) == "FY2024 Q2"


def test_build_detected_period_without_end_date_is_none():
    assert build_detected_period({}) is None
    assert build_detected_period(None) is None


# ── MD&A extraction ─────────────────────────────────────────────────────────


def test_html_to_text_strips_markup():
    text = html_to_text("<html><body><p>Hello&nbsp;world</p><script>x</script></body></html>")
    assert "Hello world" in text
    assert "script" not in text


def _filing_html() -> str:
    filler = " ".join(["The company continues to invest in its operations."] * 60)
    return (
        "<html><body>"
        "<p>Table of Contents: Item 7. Management's Discussion and Analysis of "
        "Financial Condition and Results of Operations ........ 21</p>"
        "<p>Item 8. Financial Statements and Supplementary Data .... 60</p>"
        "<p>Item 7. Management's Discussion and Analysis of Financial Condition "
        "and Results of Operations</p>"
        f"<p>{filler}</p>"
        "<p>We expect fiscal 2025 revenue of approximately $108.0 billion.</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>"
        "<p>Interest rate risk discussion.</p>"
        "</body></html>"
    )


def test_extract_mda_section_prefers_body_over_table_of_contents():
    text = html_to_text(_filing_html())
    section = extract_mda_section(text, "10-K")
    assert section is not None
    assert "fiscal 2025 revenue" in section
    assert "Interest rate risk" not in section  # ends at Item 7A


def test_fetch_mda_text_from_html_text():
    section = fetch_mda_text(
        accession_number="0000320193-24-000123",
        form_type="10-K",
        html_text=_filing_html(),
    )
    assert section and "fiscal 2025 revenue" in section


def test_fetch_mda_text_returns_none_without_html(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_HTML_DOWNLOAD_PATH", str(tmp_path))
    assert fetch_mda_text(
        accession_number="0000000000-00-000000", form_type="10-K"
    ) is None


def test_extract_mda_section_handles_unicode_quotes_10q():
    filler = " ".join(["The company continues to expand operations."] * 60)
    html = (
        "<html><body>"
        "<p>Table of Contents: Item 2. Management’s Discussion and Analysis of "
        "Financial Condition and Results of Operations ........ 15</p>"
        "<p>Item 1. Financial Statements .... 2</p>"
        "<p>Item 2. Management’s Discussion and Analysis of Financial Condition "
        "and Results of Operations</p>"
        f"<p>{filler}</p>"
        "<p>We expect Q4 revenue of approximately $25.0 billion.</p>"
        "<p>Item 3. Quantitative and Qualitative Disclosures About Market Risk</p>"
        "<p>Market risk details.</p>"
        "</body></html>"
    )
    text = html_to_text(html)
    section = extract_mda_section(text, "10-Q")
    assert section is not None
    assert "Q4 revenue" in section
    assert "Market risk" not in section


def test_extract_mda_section_prefers_primary_item_over_quote_in_paragraph():
    filler = " ".join(["The company reported solid earnings across segments."] * 60)
    html = (
        "<html><body>"
        "<p>Item 7. Management’s Discussion and Analysis of Financial Condition and Results of Operations</p>"
        "<p>This discussion should be read in conjunction with the financial statements. More details "
        "can be found in “Management’s Discussion and Analysis of Financial Condition and Results of Operations” "
        "in our previous annual report.</p>"
        f"<p>{filler}</p>"
        "<p>We expect full year operating margin of 30%.</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>"
        "</body></html>"
    )
    text = html_to_text(html)
    section = extract_mda_section(text, "10-K")
    assert section is not None
    assert section.startswith("Item 7. Management’s Discussion")
    assert "operating margin of 30%" in section


# ── guidance normalization (ported stack) ───────────────────────────────────


def test_normalize_guidance_record_scales_monetary_value():
    filing_period = build_detected_period(
        {"end_date": "2024-12-31", "fiscal_year": 2024}, form_type="10-K"
    )
    records, issues = normalize_guidance_records(
        [
            {
                "metric": "revenue",
                "standard_label": "Total Revenues",
                "statement_type": "income",
                "basis": "gaap",
                "form": "point",
                "value": 108.0,
                "period": {"fiscal_year": 2025, "period_type": "annual"},
                "unit": "USD",
                "scale": "billions",
                "currency": "USD",
                "as_printed": "We expect fiscal 2025 revenue of approximately $108.0 billion",
                "lines": [10, 12],
            }
        ],
        filing_period,
        "USD",
    )
    assert issues == []
    assert len(records) == 1
    doc = records[0]
    assert doc["value"] == pytest.approx(108_000_000_000)
    assert doc["period_type"] == "annual"
    assert doc["metric"] == "revenue"
    assert doc["is_current"] is True


def test_normalize_guidance_drops_past_periods():
    filing_period = build_detected_period(
        {"end_date": "2024-12-31", "fiscal_year": 2024}, form_type="10-K"
    )
    records, issues = normalize_guidance_records(
        [
            {
                "metric": "revenue",
                "value": 100.0,
                "scale": "millions",
                "period": {"fiscal_year": 2023, "period_type": "annual"},
                "unit": "USD",
            }
        ],
        filing_period,
        "USD",
    )
    assert records == []
    assert issues  # dropped, with a reason


def test_normalize_guidance_keeps_per_share_as_is():
    filing_period = build_detected_period(
        {"end_date": "2024-12-31", "fiscal_year": 2024}, form_type="10-K"
    )
    records, _ = normalize_guidance_records(
        [
            {
                "metric": "eps_adjusted",
                "standard_label": "Earnings Per Share, Diluted",
                "basis": "non_gaap",
                "form": "range",
                "value_low": 4.70,
                "value_high": 4.90,
                "period": {"fiscal_year": 2025, "period_type": "annual"},
                "unit": "USD",
                "scale": "as-is",
            }
        ],
        filing_period,
        "USD",
    )
    assert records[0]["value"] == pytest.approx(4.80)  # midpoint, not scaled


# ── prompt contract ─────────────────────────────────────────────────────────


def test_guidance_prompt_mentions_terminal_tool_and_schema():
    assert "finalize_guidance" in GUIDANCE_SYSTEM_PROMPT
    assert '"guidance"' in GUIDANCE_SYSTEM_PROMPT
    assert "plus_minus_pct" in GUIDANCE_SYSTEM_PROMPT


# ── nodes ───────────────────────────────────────────────────────────────────


def _bundle():
    return StatementBundle(
        company_cik="0000320193",
        statement_type="income",
        form_type="10-K",
        reporting_period={"end_date": "2024-12-31", "fiscal_year": 2024},
        created_at=datetime.now(tz=timezone.utc),
        statement_id=ObjectId(),
        filing_id=ObjectId(),
        concepts=[{"concept": "us-gaap:Revenues", "value": 1.0, "unit": "USD"}],
    )


def _state(**overrides):
    base = {
        "cik": "0000320193",
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "form_type": "10-K",
        "accession_number": "acc-1",
        "status": "saved",
        "reporting_period": {"end_date": "2024-12-31", "fiscal_year": 2024},
        "bundles": [_bundle()],
    }
    base.update(overrides)
    return base


class _StubChat:
    def __init__(self, payload):
        self._payload = payload
        self.tools = None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "finalize_guidance",
                    "args": {"result_json": self._payload},
                    "id": "g1",
                }
            ],
        )


GUIDANCE_JSON = (
    '{"guidance":[{"metric":"revenue","value":108.0,"scale":"billions",'
    '"form":"point","period":{"fiscal_year":2025,"period_type":"annual"},'
    '"unit":"USD","currency":"USD","as_printed":"expect $108.0 billion"}]}'
)


def test_extract_guidance_node_normalizes_agent_output():
    node = make_guidance_extract_node(
        chat_llm=_StubChat(GUIDANCE_JSON),
        mda_provider=lambda state: "We expect fiscal 2025 revenue of $108.0 billion.",
    )
    out = node(_state())

    assert out["status"] == "saved"  # status preserved
    assert len(out["guidance_records"]) == 1
    assert out["guidance_records"][0]["value"] == pytest.approx(108_000_000_000)
    assert out["guidance_extract"]["status"] == "extracted"
    assert out["mda_text"].startswith("We expect")


def test_extract_guidance_node_skips_without_mda_text():
    node = make_guidance_extract_node(
        chat_llm=_StubChat(GUIDANCE_JSON), mda_provider=lambda state: None
    )
    out = node(_state())

    assert out["status"] == "saved"
    assert out["guidance_records"] == []
    assert out["guidance_extract"]["status"] == "no_mda_text"


def test_extract_guidance_node_handles_empty_report():
    node = make_guidance_extract_node(
        chat_llm=_StubChat('{"guidance": []}'),
        mda_provider=lambda state: "No forward-looking statements here.",
    )
    out = node(_state())

    assert out["guidance_records"] == []
    assert out["guidance_extract"]["status"] == "empty"


def test_save_guidance_node_upserts_and_scores(monkeypatch):
    calls = {}

    def fake_upsert(cik, records, **kwargs):
        calls["upsert"] = (cik, len(records), kwargs.get("form_type"))
        return {"upserted": len(records), "skipped_manual": 0, "demoted": 2}

    def fake_score(cik, period):
        calls["score"] = cik
        return {"checked": 1, "scored": 1}

    monkeypatch.setattr(
        "filings_agent.integrations.guidance.upsert_guidance_records", fake_upsert
    )
    monkeypatch.setattr(
        "filings_agent.integrations.guidance.score_guidance_for_cik", fake_score
    )

    out = make_guidance_save_node()(
        _state(guidance_records=[{"metric": "revenue", "value": 1.0}])
    )

    assert out["status"] == "saved"
    assert out["guidance_save"]["status"] == "saved"
    assert out["guidance_save"]["upserted"] == 1
    assert out["guidance_save"]["demoted"] == 2
    assert out["guidance_save"]["score"] == {"checked": 1, "scored": 1}
    assert calls["upsert"] == ("0000320193", 1, "10-K")
    assert calls["score"] == "0000320193"


def test_save_guidance_node_never_fails_the_run(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(
        "filings_agent.integrations.guidance.upsert_guidance_records", boom
    )
    out = make_guidance_save_node()(
        _state(guidance_records=[{"metric": "revenue", "value": 1.0}])
    )
    assert out["status"] == "saved"
    assert out["guidance_save"]["status"] == "failed"


def test_save_guidance_node_noop_without_records():
    out = make_guidance_save_node()(_state(guidance_records=[]))
    assert out["guidance_save"]["status"] == "no_records"


def test_filing_currency_detection():
    assert filing_currency([_bundle()]) == "USD"
    assert filing_currency([]) == "USD"
    foreign = _bundle()
    foreign.concepts[0]["unit"] = "iso4217:EUR"
    assert filing_currency([foreign]) == "EUR"


# ── graph wiring ────────────────────────────────────────────────────────────


class _FakeNormService:
    def __init__(self, bundle, persist_ok=True):
        self._bundle = bundle
        self._persist_ok = persist_ok
        self.persist_calls = 0

    def normalize_statement_to_bundle(self, *args, **kwargs):
        return self._bundle

    def persist_statement_bundle(self, bundle, **kwargs):
        self.persist_calls += 1
        return self._persist_ok


def _graph_state():
    from filings_agent.state import new_state

    return new_state(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form_type="10-K",
        accession_number="acc-1",
        statement_docs=[{"statement_type": "income"}],
        filing_doc={"form_type": "10-K"},
        company_doc={"cik": "0000320193"},
    )


def test_graph_runs_guidance_after_successful_save(monkeypatch):
    from filings_agent.graph import build_filing_graph

    events = []

    def fake_upsert(cik, records, **kwargs):
        events.append("save-guidance")
        return {"upserted": len(records), "skipped_manual": 0, "demoted": 0}

    monkeypatch.setattr(
        "filings_agent.integrations.guidance.upsert_guidance_records", fake_upsert
    )
    monkeypatch.setattr(
        "filings_agent.integrations.guidance.score_guidance_for_cik",
        lambda cik, period: {"checked": 0, "scored": 0},
    )

    svc = _FakeNormService(_bundle())
    graph = build_filing_graph(
        svc,
        guidance_chat_llm=_StubChat(GUIDANCE_JSON),
        mda_provider=lambda state: "We expect fiscal 2025 revenue of $108.0 billion.",
    )

    final = graph.invoke(_graph_state())

    assert final["status"] == "saved"
    assert events == ["save-guidance"]
    assert len(final["guidance_records"]) == 1
    assert final["guidance_save"]["upserted"] == 1


def test_graph_skips_guidance_when_persist_fails(monkeypatch):
    from filings_agent.graph import build_filing_graph

    called = []
    monkeypatch.setattr(
        "filings_agent.integrations.guidance.upsert_guidance_records",
        lambda *a, **k: called.append("upsert") or {},
    )

    svc = _FakeNormService(_bundle(), persist_ok=False)
    graph = build_filing_graph(
        svc,
        guidance_chat_llm=_StubChat(GUIDANCE_JSON),
        mda_provider=lambda state: "We expect fiscal 2025 revenue of $108.0 billion.",
    )

    final = graph.invoke(_graph_state())

    assert final["status"] == "failed"
    assert called == []
    assert not final.get("guidance_records")


# ── concept resolution across quarterly/annual collections ──────────────────


def _get_path(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(doc, query):
    for key, want in (query or {}).items():
        got = _get_path(doc, key)
        if isinstance(want, dict):
            if "$in" in want and got not in want["$in"]:
                return False
            if "$ne" in want and got == want["$ne"]:
                return False
        elif got != want:
            return False
    return True


class _FakeCollection:
    def __init__(self, docs=()):
        self._docs = list(docs)

    def find(self, query=None, projection=None):
        return [d for d in self._docs if _matches(d, query)]

    def find_one(self, query=None, projection=None):
        rows = self.find(query)
        return rows[0] if rows else None


class _FakeDB:
    def __init__(self, collections):
        self._collections = collections

    def __getitem__(self, name):
        return self._collections.get(name, _FakeCollection())


def _resolution_db():
    from filings_agent.integrations.guidance import _resolve_concept  # noqa: F401

    quarterly_id, annual_id = ObjectId(), ObjectId()
    row = {"cik": "1", "concept": "us-gaap:Revenues", "statement_type": "income"}
    db = _FakeDB(
        {
            "concepts_standard_mapping": _FakeCollection(
                [
                    {
                        "standard_label": "Total Revenues",
                        "statement_type": "income",
                        "concepts": ["us-gaap:Revenues"],
                        "isActive": True,
                    }
                ]
            ),
            "normalized_concepts_quarterly": _FakeCollection(
                [dict(row, _id=quarterly_id)]
            ),
            "normalized_concepts_annual": _FakeCollection([dict(row, _id=annual_id)]),
        }
    )
    return db, quarterly_id, annual_id


def test_resolve_concept_prefers_collection_matching_covered_period():
    from filings_agent.integrations.guidance import _resolve_concept

    db, quarterly_id, annual_id = _resolution_db()

    annual_doc = {
        "cik": "1",
        "statement_type": "income",
        "concept": "us-gaap:Revenues",
        "period": {"fiscal_year": 2025, "period_type": "annual"},
    }
    quarterly_doc = {
        "cik": "1",
        "statement_type": "income",
        "concept": "us-gaap:Revenues",
        "period": {"fiscal_year": 2025, "quarter": 2, "period_type": "quarterly"},
    }

    assert _resolve_concept(db, annual_doc)["_id"] == annual_id
    assert _resolve_concept(db, quarterly_doc)["_id"] == quarterly_id


def test_resolve_concept_annual_via_standard_label_fallback():
    from filings_agent.integrations.guidance import _resolve_concept

    db, _quarterly_id, annual_id = _resolution_db()
    doc = {
        "cik": "1",
        "statement_type": "income",
        "standard_label": "Total Revenues",
        "period": {"fiscal_year": 2025, "period_type": "annual"},
    }
    assert _resolve_concept(db, doc)["_id"] == annual_id


def test_resolve_concept_unknown_company_returns_none():
    from filings_agent.integrations.guidance import _resolve_concept

    db, _q, _a = _resolution_db()
    doc = {"cik": "999", "statement_type": "income", "concept": "us-gaap:Revenues",
           "period": {"fiscal_year": 2025, "period_type": "annual"}}
    assert _resolve_concept(db, doc) is None
