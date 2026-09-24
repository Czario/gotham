"""MD&A text extraction for the guidance pass (P6).

Locates the filing's HTML — the local ``SEC_HTML_DOWNLOAD_PATH`` archive first
(``<dir>/<TICKER>/<accession>.html.gz``), optionally fetching it via the SEC
client — converts it to plain text, and isolates the Management's Discussion
and Analysis section (Item 7 for 10-K, Item 2 for 10-Q).

Everything here is best-effort: any failure returns ``None`` and guidance is
simply skipped for that filing (guidance must never fail a run).
"""
from __future__ import annotations

import gzip
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MAX_READ_BYTES = 60_000_000

# MD&A start markers. The real body appears AFTER the table of contents, so the
# last plausible match is used.
_QUOTE = r"[\x27\x22’‘´`\u2018\u2019\u201c\u201d]"

_MDA_START_10K_PRIMARY = (
    rf"(?:^|\n|\b)\s*item\s*7\s*[\.\-–—:]?\s*management{_QUOTE}?s?\s+discussion",
)
_MDA_START_10Q_PRIMARY = (
    rf"(?:^|\n|\b)\s*item\s*2\s*[\.\-–—:]?\s*management{_QUOTE}?s?\s+discussion",
)
_MDA_START_GENERIC = (
    rf"(?:^|\n|\b)\s*management{_QUOTE}?s?\s+discussion\s+and\s+analysis\s+of\s+financial\s+condition",
    rf"management{_QUOTE}?s?\s+discussion\s+and\s+analysis\s+of\s+financial\s+condition",
)

# Section end markers.
_MDA_END_10K = (
    r"(?:^|\n|\b)\s*item\s*7a\s*[\.\-–—:]?\s*quantitative",
    r"(?:^|\n|\b)\s*item\s*8\s*[\.\-–—:]?\s*financial\s+statements",
    r"(?:^|\n|\b)\s*item\s*9\s*[\.\-–—:]?\s*changes\s+in\s+and\s+disagreements",
)
_MDA_END_10Q = (
    r"(?:^|\n|\b)\s*item\s*3\s*[\.\-–—:]?\s*quantitative",
    r"(?:^|\n|\b)\s*item\s*4\s*[\.\-–—:]?\s*controls\s+and\s+procedures",
    r"(?:^|\n|\b)\s*item\s*1\s*[\.\-–—:]?\s*legal\s+proceedings",
    r"(?:^|\n|\b)\s*part\s*ii\s*[\.\-–—:]?\s*other\s+information",
)

_MIN_SECTION_CHARS = 800


def _compile(patterns: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def html_to_text(html: str) -> str:
    """Convert filing HTML to line-oriented plain text."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    raw = soup.get_text("\n")
    lines = []
    for line in raw.split("\n"):
        cleaned = re.sub(r"[\u00a0\s]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def extract_mda_section(text: str, form_type: str = "") -> Optional[str]:
    """Isolate the MD&A section from filing text; ``None`` when not found."""
    if not text:
        return None
    is_10k = (form_type or "").upper() == "10-K"
    primary_start_rx = _compile(_MDA_START_10K_PRIMARY if is_10k else _MDA_START_10Q_PRIMARY)
    generic_start_rx = _compile(_MDA_START_GENERIC)
    end_rx = _compile(_MDA_END_10K if is_10k else _MDA_END_10Q)

    starts = [
        m for m in primary_start_rx.finditer(text)
        if len(text) - m.end() >= _MIN_SECTION_CHARS
    ]
    if not starts:
        starts = [
            m for m in generic_start_rx.finditer(text)
            if len(text) - m.end() >= _MIN_SECTION_CHARS
        ]
    if not starts:
        # Fall back to the other form's markers: some 10-K filers label MD&A
        # as "Item 2" and vice versa (e.g. transition reports).
        alt_primary = _compile(_MDA_START_10Q_PRIMARY if is_10k else _MDA_START_10K_PRIMARY)
        alt_end = _compile(_MDA_END_10Q if is_10k else _MDA_END_10K)
        starts = [
            m for m in alt_primary.finditer(text)
            if len(text) - m.end() >= _MIN_SECTION_CHARS
        ]
        if starts:
            end_rx = alt_end

    if not starts:
        return None

    # Body, not the table of contents: last plausible start.
    start = starts[-1]
    body_start = start.start()

    end_match = end_rx.search(text, start.end())
    body_end = end_match.start() if end_match else len(text)
    body = text[body_start:body_end].strip()
    if len(body) < _MIN_SECTION_CHARS:
        return None
    return body


def _clean_accession(accession_number: str) -> str:
    acc = (accession_number or "").strip()
    return acc[:-5] if acc.endswith("-xbrl") else acc


def find_local_html(
    accession_number: str,
    *,
    ticker: Optional[str] = None,
    html_dir: Optional[str] = None,
) -> Optional[Path]:
    """Locate a cached HTML filing (``<dir>/<TICKER>/<accession>.html.gz``)."""
    acc = _clean_accession(accession_number)
    if not acc:
        return None
    base = Path(html_dir or os.getenv("SEC_HTML_DOWNLOAD_PATH") or "")
    if not str(base) or not base.exists():
        return None

    candidates: list[Path] = []
    if ticker:
        folder = base / ticker.upper()
        candidates += [folder / f"{acc}.html.gz", folder / f"{acc}.html", folder / f"{acc}.htm"]
    candidates += [
        base / f"{acc}.html.gz",
        base / f"{acc}.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    # Fall back to a recursive search (one level of ticker folders is typical).
    for pattern in (f"{acc}.html.gz", f"{acc}.html", f"{acc}.htm"):
        for found in base.glob(f"*/{pattern}"):
            if found.is_file():
                return found
    return None


def read_html_file(path: Path) -> Optional[str]:
    """Read (and gunzip) a cached filing file."""
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rb") as fh:
                data = fh.read(_MAX_READ_BYTES)
        else:
            data = path.read_bytes()[:_MAX_READ_BYTES]
    except OSError as exc:
        logger.warning("could not read filing html %s: %s", path, exc)
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def fetch_mda_text(
    *,
    accession_number: str,
    cik: Optional[str] = None,
    ticker: Optional[str] = None,
    form_type: str = "",
    html_dir: Optional[str] = None,
    html_text: Optional[str] = None,
    sec_client: Any = None,
    filing_date: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> Optional[str]:
    """Return the MD&A plain text for a filing, or ``None``.

    Preference order: explicit ``html_text`` → local archive → SEC download
    (when a ``sec_client`` and ``filing_date`` are supplied).  Never raises.
    """
    try:
        if html_text is None:
            path = find_local_html(
                accession_number, ticker=ticker, html_dir=html_dir
            )
            if path is None and sec_client is not None and filing_date:
                try:
                    sec_client.download_html_filing(
                        cik=(cik or ticker or ""),
                        accession_number=accession_number,
                        filing_date=filing_date,
                        download_path=html_dir or os.getenv("SEC_HTML_DOWNLOAD_PATH") or "",
                        ticker=ticker,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("html download failed for %s: %s", accession_number, exc)
                path = find_local_html(
                    accession_number, ticker=ticker, html_dir=html_dir
                )
            if path is None:
                logger.info("no cached HTML for %s — skipping guidance", accession_number)
                return None
            html_text = read_html_file(path)
            if html_text is None:
                return None

        text = html_to_text(html_text)
        section = extract_mda_section(text, form_type) or ""
        if not section:
            logger.info(
                "MD&A section not isolated for %s (%s) — skipping guidance",
                accession_number, form_type or "?",
            )
            return None

        if max_chars and len(section) > max_chars:
            section = section[:max_chars]
        return section
    except Exception as exc:  # noqa: BLE001
        logger.warning("MD&A extraction failed for %s: %s", accession_number, exc)
        return None


def default_mda_provider(state: dict) -> Optional[str]:
    """Graph-state MD&A provider using the local archive + env config."""
    from .config import GUIDANCE_MAX_CHARS

    return fetch_mda_text(
        accession_number=str(state.get("accession_number") or ""),
        cik=str(state.get("cik") or ""),
        ticker=state.get("ticker"),
        form_type=str(state.get("form_type") or ""),
        html_dir=os.getenv("SEC_HTML_DOWNLOAD_PATH"),
        html_text=state.get("html_text"),
        sec_client=state.get("sec_client"),
        filing_date=state.get("filing_date"),
        max_chars=GUIDANCE_MAX_CHARS,
    )
