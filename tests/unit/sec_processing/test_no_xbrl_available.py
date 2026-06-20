"""
Unit tests for the "no XBRL available" short-circuit in FlexibleXBRLExtractor.

Covers genuine pre-XBRL-era filings (e.g. Tesla 2010-2011) that have no XBRL
instance in their own accession and no companion amendment supplying one. These
must be skipped cleanly with a distinct signal rather than attempting a doomed
Arelle parse that yields the misleading "no financial statements" error.
"""
import pytest

from core.extractors.xbrl_parser import FlexibleXBRLExtractor


TXT_URL = "https://www.sec.gov/Archives/edgar/data/1318605/000119312510188792/0001193125-10-188792.txt"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/1318605/000119312510188792/0001193125-10-188792-index.htm"


@pytest.fixture
def extractor():
    ex = FlexibleXBRLExtractor(enable_enhanced_dimensions=False)
    return ex


class TestNoXbrlAvailable:
    def test_resolver_sets_flag_when_no_xbrl_and_no_amendment(self, extractor, mocker):
        mocker.patch.object(
            extractor.url_detector, "detect_filing_urls",
            return_value={"xbrl_url": TXT_URL, "txt_url": TXT_URL},
        )
        mocker.patch.object(extractor.url_detector, "is_txt_fallback", return_value=True)
        mocker.patch.object(extractor.url_detector, "find_amendment_xbrl_url", return_value=None)

        extractor._resolve_optimal_xbrl_url(INDEX_URL)
        assert extractor._no_xbrl_available is True

    def test_extract_short_circuits_without_loading(self, extractor, mocker):
        mocker.patch.object(
            extractor.url_detector, "detect_filing_urls",
            return_value={"xbrl_url": TXT_URL, "txt_url": TXT_URL},
        )
        mocker.patch.object(extractor.url_detector, "is_txt_fallback", return_value=True)
        mocker.patch.object(extractor.url_detector, "find_amendment_xbrl_url", return_value=None)
        load = mocker.patch.object(extractor.model_manager, "load")

        result = extractor.extract_financial_statements(INDEX_URL)

        assert result["no_xbrl_available"] is True
        assert result["statements"] == {}
        load.assert_not_called()

    def test_flag_reset_when_real_xbrl_found(self, extractor, mocker):
        real_xbrl = "https://www.sec.gov/Archives/edgar/data/1318605/x/tsla-20131231.xml"
        mocker.patch.object(
            extractor.url_detector, "detect_filing_urls",
            return_value={"xbrl_url": real_xbrl, "txt_url": TXT_URL},
        )
        mocker.patch.object(extractor.url_detector, "is_txt_fallback", return_value=False)

        resolved = extractor._resolve_optimal_xbrl_url(INDEX_URL)
        assert resolved == real_xbrl
        assert extractor._no_xbrl_available is False

    def test_flag_reset_when_amendment_recovers_xbrl(self, extractor, mocker):
        amend_xbrl = "https://www.sec.gov/Archives/edgar/data/1318605/y/tsla-20111231.xml"
        mocker.patch.object(
            extractor.url_detector, "detect_filing_urls",
            return_value={"xbrl_url": TXT_URL, "txt_url": TXT_URL},
        )
        mocker.patch.object(extractor.url_detector, "is_txt_fallback", return_value=True)
        mocker.patch.object(extractor.url_detector, "find_amendment_xbrl_url", return_value=amend_xbrl)

        resolved = extractor._resolve_optimal_xbrl_url(INDEX_URL)
        assert resolved == amend_xbrl
        assert extractor._no_xbrl_available is False
