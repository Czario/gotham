"""Accession → filing-date approximation used by URL resolution.

Regression: the old regex matched the CIK/accession boundary
(``...193-20-000062``) and produced year 2000 for a 2020 filing, sending modern
filings down the legacy path and falsely reporting "no XBRL".
"""
from core.extractors.xbrl_parser import _filing_date_from_accession


def test_modern_accession_year():
    assert _filing_date_from_accession("0000320193-20-000062") == "2020-01-01"


def test_legacy_accession_year():
    assert _filing_date_from_accession("0001193125-12-444068") == "2012-01-01"
    assert _filing_date_from_accession("0001628280-17-004790") == "2017-01-01"


def test_invalid_accession_returns_none():
    assert _filing_date_from_accession("not-an-accession") is None
    assert _filing_date_from_accession("") is None
    assert _filing_date_from_accession(None) is None
