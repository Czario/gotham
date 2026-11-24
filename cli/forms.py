"""SEC URL, form-type, statement-type, and quarter parsing helpers."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

def parse_sec_filing_url(url: str) -> Optional[Dict[str, str]]:
    """
    Parse an SEC filing URL to extract CIK and accession number
    
    Supports various SEC URL formats:
    - https://www.sec.gov/cgi-bin/viewer?action=view&cik=320193&accession_number=0000320193-23-000077
    - https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230701.htm
    - https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/
    
    Args:
        url: SEC filing URL
        
    Returns:
        Dict with 'cik' and 'accession_number' keys, or None if parsing fails
    """
    try:
        # Pattern 1: viewer URL with explicit parameters
        viewer_pattern = r'cik=(\d+).*?accession[_-]number=(\d{10}-\d{2}-\d{6})'
        match = re.search(viewer_pattern, url, re.IGNORECASE)
        if match:
            cik = match.group(1).zfill(10)  # Pad CIK to 10 digits
            accession = match.group(2)
            return {'cik': cik, 'accession_number': accession}
        
        # Pattern 2: Archives URL - extract from path
        # Example: /Archives/edgar/data/320193/000032019323000077/...
        archives_pattern = r'/edgar/data/(\d+)/(\d{18})'
        match = re.search(archives_pattern, url)
        if match:
            cik = match.group(1).zfill(10)
            accession_raw = match.group(2)
            # Convert 18-digit format to standard format: 0000320193-23-000077
            accession = f"{accession_raw[:10]}-{accession_raw[10:12]}-{accession_raw[12:]}"
            return {'cik': cik, 'accession_number': accession}
        
        # Pattern 3: Index URL with standard accession format
        # Example: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K&dateb=&owner=exclude&count=40&search_text=
        index_pattern = r'CIK=(\d+)'
        match = re.search(index_pattern, url, re.IGNORECASE)
        if match:
            logger.warning(f"URL appears to be a company listing page, not a specific filing: {url}")
            return None
            
        logger.error(f"Unable to parse SEC filing URL: {url}")
        return None
        
    except Exception as e:
        logger.error(f"Error parsing SEC filing URL: {e}")
        return None

STATEMENT_ALIASES: Dict[str, str] = {
    'income': 'income',
    'income_statement': 'income',
    'income-statement': 'income',
    'incomestatement': 'income',
    'is': 'income',
    'balancesheet': 'balancesheet',
    'balance_sheet': 'balancesheet',
    'balance-sheet': 'balancesheet',
    'bs': 'balancesheet',
    'cashflow': 'cashflow',
    'cash_flow': 'cashflow',
    'cash-flow': 'cashflow',
    'cash_flows': 'cashflow',
    'cashflows': 'cashflow',
    'cf': 'cashflow',
}

CANONICAL_STATEMENT_TYPES = {'income', 'balancesheet', 'cashflow'}


def canonicalize_statement_type(stmt: Optional[str]) -> Optional[str]:
    """Convert user-supplied statement identifier to the canonical internal name.

    Returns:
        'income', 'balancesheet', 'cashflow', or None if input is empty.
    Raises:
        ValueError: if stmt is not a recognized statement type.
    """
    if not stmt:
        return None
    normalized = str(stmt).strip().lower().replace("-", "_")
    canonical = STATEMENT_ALIASES.get(normalized)
    if not canonical:
        raise ValueError(
            f"Invalid statement type '{stmt}'. Must be one of: "
            "income (or income_statement), balancesheet (or balance_sheet), cashflow (or cash_flow)."
        )
    return canonical


DEFAULT_XBRL_FORMS = ['10-K', '10-Q', '20-F', '20-F/A']
ALL_EARNINGS_FORMS = ['8-K', '6-K']


def parse_form_selection(raw_form: Optional[str]) -> Dict[str, Any]:
    """Parse ``--form`` into XBRL forms and earnings (8-K/6-K) forms.

    Supported examples (case-insensitive):
      - None            → default XBRL forms (10-K, 10-Q, 20-F, 20-F/A)
      - ``10-K`` / ``10k`` → 10-K only
      - ``10-Q`` / ``10q`` → 10-Q only
      - ``10k-10q`` / ``10-K,10-Q`` → 10-K and 10-Q
      - ``20-F`` / ``20f`` → 20-F and 20-F/A
      - ``8-K`` / ``8k``   → 8-K only
      - ``6-K`` / ``6k``   → 6-K only
      - ``20f-6k`` / ``10k-10q-8k`` → combined XBRL + earnings
    """
    if not raw_form or not str(raw_form).strip():
        return {
            'mode': 'default',
            'xbrl_forms': list(DEFAULT_XBRL_FORMS),
            'earnings_forms': [],
        }

    text = str(raw_form).strip().lower()
    tokens = re.findall(r'20-?f/a|20-?f|10-?k|10-?q|8-?k|6-?k', text)
    if not tokens:
        raise ValueError(
            f"Unsupported --form value: {raw_form!r}. "
            "Use '10-K', '10-Q', '10k-10q', '20-F', '8-K', '6-K', '20f-6k', or '10k-10q-8k'."
        )

    xbrl_forms: list[str] = []
    earnings_forms: list[str] = []
    for tok in tokens:
        clean = tok.replace('-', '')
        if clean == '10k':
            xbrl_forms.append('10-K')
        elif clean == '10q':
            xbrl_forms.append('10-Q')
        elif clean == '20f':
            xbrl_forms.extend(['20-F', '20-F/A'])
        elif clean == '20f/a':
            xbrl_forms.append('20-F/A')
        elif clean == '8k':
            earnings_forms.append('8-K')
        elif clean == '6k':
            earnings_forms.append('6-K')

    return {
        'mode': 'explicit',
        'xbrl_forms': list(dict.fromkeys(xbrl_forms)),
        'earnings_forms': list(dict.fromkeys(earnings_forms)),
    }


def normalize_quarter(q: Optional[Any]) -> Optional[str]:
    """Convert user-supplied quarter identifier to canonical form ('Q1', 'Q2', 'Q3', 'Q4').

    Returns:
        'Q1', 'Q2', 'Q3', 'Q4', or None if input is empty.
    Raises:
        ValueError: if q is not a valid quarter.
    """
    if not q:
        return None
    val = str(q).strip().upper()
    if val in ('Q1', '1'):
        return 'Q1'
    if val in ('Q2', '2'):
        return 'Q2'
    if val in ('Q3', '3'):
        return 'Q3'
    if val in ('Q4', '4'):
        return 'Q4'
    raise ValueError(
        f"Invalid fiscal quarter '{q}'. Must be one of: Q1, Q2, Q3, Q4 (or 1, 2, 3, 4)."
    )

