"""Transient SEC failures must be retried (F1/F3).

A single ``503 Service Unavailable`` used to be enough to make a filing look
like it had no XBRL; company-mode never retries a filing, so the loss was
permanent.  The detector session now mounts a retrying HTTP adapter.
"""
from utilities.sec_url_detector import SECURLDetector


def test_detector_session_retries_transient_failures():
    detector = SECURLDetector()
    adapter = detector.session.get_adapter("https://www.sec.gov/")
    retries = adapter.max_retries
    assert retries.total >= 3
    assert 503 in retries.status_forcelist
    assert 429 in retries.status_forcelist
