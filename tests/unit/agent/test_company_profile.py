"""Unit tests for shared.company_profile extraction, manual override locks, and history."""
from __future__ import annotations

from typing import Any

from shared.company_profile import (
    build_profile_document_tools,
    extract_deterministic_profile,
    extract_sections_from_annual_html,
    upsert_company_profile,
)


class _MemCol:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def find_one(self, filt: dict[str, Any]):
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                return dict(d)
        return None

    def update_one(self, filt: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        payload = dict(update.get("$set") or {})
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in filt.items()):
                merged = dict(d)
                merged.update(payload)
                self.docs[i] = merged
                return
        if upsert:
            doc = dict(filt)
            doc.update(payload)
            self.docs.append(doc)

    def insert_one(self, doc: dict[str, Any]):
        self.docs.append(dict(doc))


def test_extract_deterministic_profile_from_submissions_and_dei():
    subs = {
        "name": "MICROSOFT CORP",
        "tickers": ["MSFT"],
        "exchanges": ["Nasdaq"],
        "sic": "7372",
        "sicDescription": "SERVICES-PREPACKAGED SOFTWARE",
        "ein": "911144442",
        "stateOfIncorporation": "WA",
        "fiscalYearEnd": "0630",
        "phone": "425-882-8080",
        "website": "www.microsoft.com",
        "addresses": {
            "business": {
                "street1": "ONE MICROSOFT WAY",
                "city": "REDMOND",
                "stateOrCountry": "WA",
                "zipCode": "98052-6399",
            }
        },
    }
    dei = {
        "shares_outstanding": 7426194552,
        "shares_as_of": "2026-10-22",
        "shares_citation": "10-Q cover",
        "public_float_usd": 3600000000000.0,
        "public_float_as_of": "2025-12-31",
    }
    prof = extract_deterministic_profile("789019", subs, dei_facts=dei)
    assert prof["cik"] == "0000789019"
    assert prof["ticker"] == "MSFT"
    assert prof["exchange"] == "Nasdaq"
    assert prof["key_facts"]["incorporated_in"] == "Washington"
    assert prof["key_facts"]["headquarters"]["country"] == "US"
    assert prof["key_facts"]["fiscal_year_end"] == "June 30"
    assert prof["key_facts"]["ein"] == "91-1144442"
    assert prof["shares"]["shares_outstanding"] == 7426194552
    assert prof["shares"]["shares_as_of"] == "2026-10-22"


def test_upsert_company_profile_updates_without_locking_and_logs_history():
    db = {
        "company_profiles": _MemCol(),
        "company_profile_history": _MemCol(),
    }

    # Initial auto-create
    r1 = upsert_company_profile(
        db,
        "0000789019",
        {
            "company_name": "Microsoft Corporation",
            "ticker": "MSFT",
            "workforce": {"total_employees": 220000, "as_of": "2025-06-30"},
            "key_facts": {"auditor": "Deloitte & Touche LLP"},
        },
        actor="gotham:auto",
        trigger_form="10-K",
        accession="0001193125-25-000001",
    )
    assert r1["profile_version"] == 1
    assert r1["changes_count"] == 5
    assert len(db["company_profile_history"].docs) == 1

    # Subsequent auto-update always overwrites changed fields (no field locking)
    r2 = upsert_company_profile(
        db,
        "0000789019",
        {
            "workforce": {"total_employees": 223000, "as_of": "2026-06-30"},
            "shares": {"shares_outstanding": 7426194552, "shares_as_of": "2026-10-22"},
        },
        actor="gotham:auto",
        trigger_form="10-K",
        accession="0001193125-26-323660",
    )
    assert r2["profile_version"] == 2
    assert r2["changes_count"] == 4
    saved = db["company_profiles"].find_one({"cik": "0000789019"})
    assert saved["workforce"]["total_employees"] == 223000
    assert saved["shares"]["shares_outstanding"] == 7426194552
    assert len(db["company_profile_history"].docs) == 2


def test_extract_sections_from_annual_html():
    html = """
    <html><body>
      <p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION Form 10-K Microsoft Corporation</p>
      <p>Table of Contents: Item 1. Business ... Item 2. Properties</p>
      """ + (" filler " * 400) + """
      <h2>ITEM 1. BUSINESS</h2>
      <p>Microsoft builds software and cloud services.</p>
      <h3>HUMAN CAPITAL</h3>
      <p>As of June 30, 2026 we employed 223,000 people.</p>
      <h2>ITEM 2. PROPERTIES</h2>
      <p>Total 112M sq ft across owned and leased facilities.</p>
      <h2>REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM</h2>
      <p>Deloitte &amp; Touche LLP</p>
    </body></html>
    """
    sections = extract_sections_from_annual_html(html)
    assert "cover" in sections
    assert "item1_business" in sections
    assert "item2_properties" in sections
    assert "auditor" in sections
    assert "Deloitte" in sections["auditor"]


def test_build_profile_document_tools_grep_and_read():
    doc = "\n".join([
        "FORM 10-K MICROSOFT CORPORATION",
        "ITEM 1. BUSINESS",
        "Our mission is to empower every person.",
        "HUMAN CAPITAL RESOURCES",
        "As of June 30, 2026 we employed approximately 228,000 people on a full-time basis.",
        "INFORMATION ABOUT OUR EXECUTIVE OFFICERS",
        "Satya Nadella, 58, Chairman and Chief Executive Officer",
        "Amy E. Hood, 54, Executive Vice President and Chief Financial Officer",
        "REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM",
        "Deloitte & Touche LLP",
    ])
    tools = {t.name: t for t in build_profile_document_tools(doc, form_type="10-K")}
    assert {"get_document_info", "grep", "read_lines"} == set(tools.keys())

    info = tools["get_document_info"].invoke({})
    assert "10 lines" in info

    hit = tools["grep"].invoke({"pattern": r"Deloitte|Chief Executive Officer", "context_lines": 1})
    assert "Satya Nadella" in hit
    assert "Deloitte & Touche LLP" in hit

    chunk = tools["read_lines"].invoke({"start": 4, "end": 8})
    assert "228,000" in chunk
    assert "Amy E. Hood" in chunk
