"""
Unit tests for SECURLDetector amendment-fallback XBRL recovery.

Covers the 2009-2012 SEC grace-period pattern where the readable 10-Q/10-K was
filed first (no XBRL) and the XBRL exhibits arrived in a separate 10-Q/A or 10-K/A
amendment for the same reporting period.
"""
import pytest

from utilities.sec_url_detector import SECURLDetector


@pytest.fixture
def detector():
    return SECURLDetector(user_agent="test test@example.com")


class TestIsTxtFallback:
    def test_real_xbrl_instance_not_fallback(self):
        d = {"xbrl_url": "https://x/msft-20111231.xml", "txt_url": "https://x/acc.txt"}
        assert SECURLDetector.is_txt_fallback(d) is False

    def test_txt_extension_is_fallback(self):
        d = {"xbrl_url": "https://x/acc.txt", "txt_url": "https://x/acc.txt"}
        assert SECURLDetector.is_txt_fallback(d) is True

    def test_equal_to_txt_url_is_fallback(self):
        d = {"xbrl_url": "https://x/acc.TXT", "txt_url": "https://x/acc.TXT"}
        assert SECURLDetector.is_txt_fallback(d) is True

    def test_missing_xbrl_is_fallback(self):
        assert SECURLDetector.is_txt_fallback({"xbrl_url": None, "txt_url": "t"}) is True


class TestFindAmendmentXbrlUrl:
    ORIG = "0001193125-12-017029"
    AMEND = "0001193125-12-026864"

    def _index(self):
        return [
            {"form": "10-Q", "accession": self.ORIG,
             "reportDate": "2011-12-31", "filingDate": "2012-01-19"},
            {"form": "10-Q/A", "accession": self.AMEND,
             "reportDate": "2011-12-31", "filingDate": "2012-01-27"},
        ]

    def test_finds_companion_amendment(self, detector, mocker):
        mocker.patch.object(detector, "_get_submission_index", return_value=self._index())
        mocker.patch.object(
            detector, "_discover_xbrl_from_directory",
            return_value="https://www.sec.gov/Archives/edgar/data/789019/000119312512026864/msft-20111231.xml",
        )
        url = detector.find_amendment_xbrl_url("0000789019", self.ORIG)
        assert url.endswith("msft-20111231.xml")

    def test_returns_none_when_no_amendment(self, detector, mocker):
        index = [self._index()[0]]  # only the original, no /A
        mocker.patch.object(detector, "_get_submission_index", return_value=index)
        disc = mocker.patch.object(detector, "_discover_xbrl_from_directory")
        assert detector.find_amendment_xbrl_url("0000789019", self.ORIG) is None
        disc.assert_not_called()

    def test_skips_amendment_with_only_txt(self, detector, mocker):
        mocker.patch.object(detector, "_get_submission_index", return_value=self._index())
        mocker.patch.object(
            detector, "_discover_xbrl_from_directory",
            return_value="https://www.sec.gov/Archives/edgar/data/789019/000119312512026864/acc.txt",
        )
        assert detector.find_amendment_xbrl_url("0000789019", self.ORIG) is None

    def test_period_must_match(self, detector, mocker):
        index = [
            {"form": "10-Q", "accession": self.ORIG,
             "reportDate": "2011-12-31", "filingDate": "2012-01-19"},
            # amendment for a DIFFERENT period - must be ignored
            {"form": "10-Q/A", "accession": "9999999999-99-999999",
             "reportDate": "2011-09-30", "filingDate": "2012-01-27"},
        ]
        mocker.patch.object(detector, "_get_submission_index", return_value=index)
        disc = mocker.patch.object(detector, "_discover_xbrl_from_directory")
        assert detector.find_amendment_xbrl_url("0000789019", self.ORIG) is None
        disc.assert_not_called()

    def test_prefers_earliest_amendment(self, detector, mocker):
        index = [
            {"form": "10-Q", "accession": self.ORIG,
             "reportDate": "2011-12-31", "filingDate": "2012-01-19"},
            {"form": "10-Q/A", "accession": "AAA",
             "reportDate": "2011-12-31", "filingDate": "2012-03-01"},
            {"form": "10-Q/A", "accession": "BBB",
             "reportDate": "2011-12-31", "filingDate": "2012-01-27"},
        ]
        mocker.patch.object(detector, "_get_submission_index", return_value=index)
        seen = []

        def fake_disc(directory_url, accession):
            seen.append(accession)
            return f"https://x/{accession}/inst-20111231.xml"

        mocker.patch.object(detector, "_discover_xbrl_from_directory", side_effect=fake_disc)
        url = detector.find_amendment_xbrl_url("0000789019", self.ORIG)
        # BBB (2012-01-27) is earlier than AAA (2012-03-01) and should be tried first
        assert seen[0] == "BBB"
        assert "BBB" in url
