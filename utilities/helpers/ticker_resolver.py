"""Live SEC ticker ↔ CIK resolver.

Fetches https://www.sec.gov/files/company_tickers.json once per process and
caches the result in module-level dicts.  Callers that previously opened the
local ``tickers.json`` file can use the helpers below as a drop-in replacement.

Usage::

    from utilities.helpers.ticker_resolver import resolve_ticker, resolve_cik
    from utilities.helpers.ticker_resolver import get_ticker_to_cik, get_cik_to_ticker

    cik = resolve_ticker("AAPL")        # "0000320193"
    ticker = resolve_cik("0000320193")  # "AAPL"
"""

import logging
import requests
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT = "statements@colab.net"

# Module-level in-memory cache (populated on first call)
_ticker_to_cik: Dict[str, str] = {}   # "AAPL" → "0000320193"
_cik_to_ticker: Dict[str, str] = {}   # "0000320193" → "AAPL"
_cik_to_name: Dict[str, str] = {}     # "0000320193" → "Apple Inc."
_loaded = False


def _load() -> None:
    """Fetch and parse company_tickers.json from SEC (runs once per process)."""
    global _ticker_to_cik, _cik_to_ticker, _cik_to_name, _loaded
    if _loaded:
        return
    try:
        from utilities.sec_rate_limiter import _global_rate_limiter
        _global_rate_limiter.acquire()
        response = requests.get(
            _SEC_TICKERS_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        for entry in data.values():
            cik_str = str(entry["cik_str"]).zfill(10)
            ticker = entry["ticker"].upper()
            _ticker_to_cik[ticker] = cik_str
            _cik_to_ticker[cik_str] = ticker
            _cik_to_name[cik_str] = entry.get("title", "")
        _loaded = True
        logger.debug(f"Loaded {len(_ticker_to_cik):,} ticker mappings from SEC API")
    except Exception as exc:
        logger.error(f"Failed to fetch ticker mappings from SEC API: {exc}")


# ── Public helpers ─────────────────────────────────────────────────────────────

def get_ticker_to_cik() -> Dict[str, str]:
    """Return the full ticker → zero-padded-CIK mapping."""
    _load()
    return _ticker_to_cik


def get_cik_to_ticker() -> Dict[str, str]:
    """Return the full zero-padded-CIK → ticker mapping."""
    _load()
    return _cik_to_ticker


def get_cik_to_name() -> Dict[str, str]:
    """Return the full zero-padded-CIK → company name mapping."""
    _load()
    return _cik_to_name


def resolve_ticker(ticker: str) -> Optional[str]:
    """Return zero-padded 10-digit CIK for *ticker*, or ``None`` if unknown."""
    _load()
    return _ticker_to_cik.get(ticker.upper())


def resolve_cik(cik: str) -> Optional[str]:
    """Return the primary ticker for *cik* (padded or unpadded), or ``None``."""
    _load()
    padded = cik.zfill(10)
    return _cik_to_ticker.get(padded) or _cik_to_ticker.get(cik)
