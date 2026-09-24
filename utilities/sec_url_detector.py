#!/usr/bin/env python3
"""
Unified SEC URL Detector
Handles URL discovery for both modern and legacy SEC filings
Works for both XBRL data extraction and HTML filing downloads
"""

import requests
import re
import time
from typing import Optional, Dict, Tuple, List, Union
from datetime import datetime
import logging
from utilities.sec_rate_limiter import _global_rate_limiter


# Custom filter to suppress Arelle transformation namespace warnings
class ArelleTransformationWarningFilter(logging.Filter):
    """Filter out noisy Arelle warnings that don't affect extraction"""
    
    def filter(self, record):
        message = record.getMessage()
        
        # Suppress invalidTransformation warnings - they're harmless and clutter output
        if 'invalidTransformation' in message:
            return False
        # Suppress messages about unrecognized transformation namespace
        if 'unrecognized transformation namespace' in message:
            return False
        # Suppress resourceIdDuplication warnings - duplicate IDs in inline XBRL (common, harmless)
        if 'resourceIdDuplication' in message:
            return False
        # Suppress xmlSchema:syntax warnings - malformed HTML in old filings (doesn't prevent extraction)
        if 'xmlSchema:syntax' in message:
            return False
        # Suppress "Opening and ending tag mismatch" - old filing HTML issues
        if 'Opening and ending tag mismatch' in message:
            return False
        
        return True


logger = logging.getLogger(__name__)


class SECURLDetector:
    """
    Unified URL detector for SEC filings that handles both modern and legacy filing formats
    Supports both XBRL data extraction and HTML filing downloads
    """
    
    # Cutoff date for modern vs legacy filing structure
    MODERN_FILING_CUTOFF = datetime(2019, 1, 1)
    
    def __init__(self, user_agent: str = "statements@colab.net", rate_limit_delay: float = 0.1):
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        })
        # Retry transient SEC failures (503 Service Unavailable, 429 rate limit,
        # 5xx).  Previously a single 503 could make a filing look like it had no
        # XBRL, and the company-mode pipeline never retries a filing.
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            _retry = Retry(
                total=4,
                connect=4,
                read=4,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                respect_retry_after_header=True,
            )
            _adapter = HTTPAdapter(max_retries=_retry)
            self.session.mount("https://", _adapter)
            self.session.mount("http://", _adapter)
        except Exception:  # noqa: BLE001 — retries are best-effort
            pass
    
    def detect_filing_urls(self, cik: str, accession_number: str, filing_date: str) -> Dict[str, Union[str, bool, None]]:
        """
        Detect all relevant URLs for a filing (XBRL, HTML, directory)
        
        Args:
            cik: Company CIK identifier
            accession_number: Filing accession number
            filing_date: Filing date in YYYY-MM-DD format
            
        Returns:
            Dict with keys: 'xbrl_url', 'html_url', 'directory_url', 'txt_url', 'is_legacy' (bool)
        """
        # Parse filing date
        try:
            filing_dt = datetime.strptime(filing_date, '%Y-%m-%d')
        except (ValueError, TypeError):
            # Malformed or missing filing date - extract year if possible
            year_match = re.search(r'(20\d{2})', filing_date) if filing_date else None
            if year_match:
                year = int(year_match.group(1))
                # Use Jan 1 of that year as a proxy
                filing_dt = datetime(year, 1, 1)
                logger.debug(f"Extracted year {year} from malformed date '{filing_date}', using {year}-01-01")
            else:
                # No valid year found - assume modern filing
                logger.debug(f"Invalid or missing filing date '{filing_date}', assuming modern filing (post-2019)")
                filing_dt = datetime.now()
        
        # Determine if this is a modern or legacy filing
        is_modern = filing_dt >= self.MODERN_FILING_CUTOFF
        
        # Clean inputs
        unpadded_cik = cik.lstrip('0')
        clean_accession = accession_number.replace('-', '')
        
        if is_modern:
            return self._detect_modern_filing_urls(unpadded_cik, accession_number, clean_accession)
        else:
            return self._detect_legacy_filing_urls(unpadded_cik, accession_number, clean_accession)

    @staticmethod
    def is_txt_fallback(detected_urls: Dict[str, Union[str, bool, None]]) -> bool:
        """
        Determine whether detect_filing_urls resolved a real XBRL instance or merely
        fell back to the raw .txt submission file (i.e. the accession has no XBRL).
        """
        xbrl_url = detected_urls.get('xbrl_url')
        txt_url = detected_urls.get('txt_url')
        if not xbrl_url or not isinstance(xbrl_url, str):
            return True
        if xbrl_url.lower().endswith('.txt'):
            return True
        if txt_url and xbrl_url == txt_url:
            return True
        return False

    def _get_json(self, url: str) -> Optional[Dict]:
        """Fetch and parse a JSON document from SEC, returning None on failure."""
        try:
            _global_rate_limiter.acquire()
            response = self.session.get(url, timeout=15)
            if response.status_code == 200:
                return response.json()
            logger.debug(f"JSON fetch {url} -> HTTP {response.status_code}")
        except Exception as e:
            logger.debug(f"JSON fetch error for {url}: {e}")
        return None

    def _get_submission_index(self, cik: str) -> List[Dict[str, Optional[str]]]:
        """
        Fetch the company's full submission index (recent + historical files) from
        data.sec.gov and return a flat list of dicts with keys:
        accession, form, reportDate, filingDate.
        """
        cik_padded = str(cik).lstrip('0').zfill(10)
        entries: List[Dict[str, Optional[str]]] = []

        def _add_block(block: Dict) -> None:
            forms = block.get('form', []) or []
            accs = block.get('accessionNumber', []) or []
            report_dates = block.get('reportDate', []) or []
            filing_dates = block.get('filingDate', []) or []
            for i, form in enumerate(forms):
                entries.append({
                    'form': form,
                    'accession': accs[i] if i < len(accs) else None,
                    'reportDate': report_dates[i] if i < len(report_dates) else None,
                    'filingDate': filing_dates[i] if i < len(filing_dates) else None,
                })

        main = self._get_json(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
        if not main:
            return entries

        _add_block(main.get('filings', {}).get('recent', {}))

        for file_obj in main.get('filings', {}).get('files', []):
            name = file_obj.get('name')
            if name:
                historical = self._get_json(f"https://data.sec.gov/submissions/{name}")
                if historical:
                    _add_block(historical)

        return entries

    def find_amendment_xbrl_url(self, cik: str, original_accession: str) -> Optional[str]:
        """
        Recover XBRL for filings that lack it in their own accession (the 2009-2012
        SEC grace-period pattern, where the readable 10-Q/10-K was filed first and the
        XBRL exhibits arrived later in a separate 10-Q/A or 10-K/A amendment).

        Locates a companion amendment for the SAME reporting period whose directory
        contains a real XBRL instance, and returns that instance URL. Returns None if
        no such amendment exists.
        """
        try:
            index = self._get_submission_index(cik)
            if not index:
                return None

            original = next((f for f in index if f.get('accession') == original_accession), None)
            if not original:
                logger.debug(f"Original accession {original_accession} not found in submission index")
                return None

            base_form = original.get('form') or ''
            report_date = original.get('reportDate')
            if not base_form or not report_date:
                return None

            amend_form = base_form if base_form.endswith('/A') else f"{base_form}/A"

            candidates = [
                f for f in index
                if f.get('form') == amend_form
                and f.get('reportDate') == report_date
                and f.get('accession') != original_accession
            ]
            # Prefer the amendment filed soonest after the original (most likely the XBRL exhibit filing)
            candidates.sort(key=lambda f: f.get('filingDate') or '')

            unpadded_cik = str(cik).lstrip('0')
            for cand in candidates:
                accession = cand.get('accession')
                if not accession:
                    continue
                clean_accession = accession.replace('-', '')
                directory_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{clean_accession}/"
                xbrl_url = self._discover_xbrl_from_directory(directory_url, accession)
                if xbrl_url and not xbrl_url.lower().endswith('.txt'):
                    logger.info(
                        f"✅ Recovered XBRL from companion amendment {accession} "
                        f"(period {report_date}) for original {original_accession}"
                    )
                    return xbrl_url

            logger.debug(f"No companion amendment with XBRL found for {original_accession}")
            return None
        except Exception as e:
            logger.debug(f"Amendment XBRL lookup failed for {original_accession}: {e}")
            return None

    def _detect_modern_filing_urls(self, cik: str, accession_number: str, clean_accession: str) -> Dict[str, Union[str, bool, None]]:
        """
        Detect URLs for modern filings (2019+)
        Structure: https://www.sec.gov/Archives/edgar/data/{CIK}/{accession-no-dashes}/
        """
        directory_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/"
        txt_url = f"{directory_url}{accession_number}.txt"

        # Discover XBRL files; _discover_xbrl_from_directory populates self._last_xbrl_candidates
        xbrl_url = self._discover_xbrl_from_directory(directory_url, accession_number)
        xbrl_candidates = list(getattr(self, '_last_xbrl_candidates', []))
        html_url = self._discover_html_from_directory(directory_url, accession_number)

        return {
            'directory_url': directory_url,
            'txt_url': txt_url,
            'xbrl_url': xbrl_url or txt_url,  # Fallback to txt if XBRL not found
            'xbrl_candidates': xbrl_candidates,
            'html_url': html_url,
            'is_legacy': False
        }
    
    def _detect_legacy_filing_urls(self, cik: str, accession_number: str, clean_accession: str) -> Dict[str, Union[str, bool, None]]:
        """
        Detect URLs for legacy filings (pre-2019)
        These use a different structure and may not have directory listings
        """
        # Legacy filings still use the Archives structure but may have different file organization
        # Try the standard directory structure first
        directory_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/"
        
        # For legacy filings, the .txt file is often the primary document
        txt_url = f"{directory_url}{accession_number}.txt"
        
        # Try to access directory - if it exists, use modern discovery
        if self._check_url_exists(directory_url):
            logger.debug(f"Legacy filing has accessible directory: {directory_url}")
            xbrl_url = self._discover_xbrl_from_directory(directory_url, accession_number)
            xbrl_candidates = list(getattr(self, '_last_xbrl_candidates', []))
            html_url = self._discover_html_from_directory(directory_url, accession_number)
        else:
            # Directory not accessible - use alternative patterns
            logger.debug(f"Legacy filing directory not accessible, trying alternative patterns")
            xbrl_url = self._try_legacy_xbrl_patterns(cik, accession_number, clean_accession)
            xbrl_candidates = [xbrl_url] if xbrl_url else []
            html_url = self._try_legacy_html_patterns(cik, accession_number, clean_accession)

        # For very old filings, the .txt file might be in a parent directory
        if not self._check_url_exists(txt_url):
            # Try alternative .txt location
            alt_txt_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_number}.txt"
            if self._check_url_exists(alt_txt_url):
                txt_url = alt_txt_url

        return {
            'directory_url': directory_url,
            'txt_url': txt_url,
            'xbrl_url': xbrl_url or txt_url,  # Fallback to txt
            'xbrl_candidates': xbrl_candidates,
            'html_url': html_url,
            'is_legacy': True
        }
    
    def _try_legacy_xbrl_patterns(self, cik: str, accession_number: str, clean_accession: str) -> Optional[str]:
        """Try common XBRL file patterns for legacy filings"""
        # Common patterns for older XBRL files
        patterns = [
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/{accession_number}.xml",
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/{accession_number}-xbrl.xml",
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/{clean_accession}.xml",
        ]
        
        for pattern in patterns:
            if self._check_url_exists(pattern):
                logger.debug(f"Found legacy XBRL file: {pattern}")
                return pattern
        
        return None
    
    def _try_legacy_html_patterns(self, cik: str, accession_number: str, clean_accession: str) -> Optional[str]:
        """Try common HTML file patterns for legacy filings"""
        # For legacy filings, we need to discover the HTML file from directory if possible
        directory_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/"
        
        # Try to get directory listing
        try:
            _global_rate_limiter.acquire()
            response = self.session.get(directory_url, timeout=10)
            
            if response.status_code == 200:
                # Parse HTML to find .htm files
                return self._discover_html_from_content(directory_url, response.text, accession_number)
        except Exception as e:
            logger.debug(f"Could not access legacy directory: {e}")
        
        return None
    
    def _discover_xbrl_from_directory(self, directory_url: str, accession_number: str) -> Optional[str]:
        """
        Discover XBRL file from directory listing with fallback validation.
        Returns the highest-scoring validated candidate URL, or None.

        Also populates ``self._last_xbrl_candidates`` with the full ordered list of
        validated (URL-accessible) candidate URLs so callers can iterate through
        fallbacks without a second network round-trip.
        """
        self._last_xbrl_candidates: List[str] = []

        try:
            _global_rate_limiter.acquire()
            response = self.session.get(directory_url, timeout=10)

            if response.status_code != 200:
                logger.debug(f"Directory not accessible: {directory_url} (HTTP {response.status_code})")
                return None

            # Parse directory listing and score every relevant file
            file_links = self._parse_file_links(response.text)
            scored = []
            for filename in file_links:
                if filename.lower().endswith(('.htm', '.html', '.xml', '.txt')):
                    score = self._score_xbrl_file(filename, accession_number)
                    if score > 0:
                        scored.append({'filename': filename, 'url': directory_url + filename, 'score': score})

            if not scored:
                logger.warning(f"No XBRL files found in directory: {directory_url}")
                return None

            scored.sort(key=lambda x: x['score'], reverse=True)
            logger.debug(f"Found {len(scored)} XBRL candidates, validating in priority order...")

            # HIGH-CONFIDENCE FAST PATH: files scoring >= 90 are trusted with URL-only validation.
            # Only high-confidence candidates (score >= 90) are ever included as fallbacks.
            # Low-score files like R*.xml XBRL-viewer fragments (score ~60) are excluded on purpose:
            # they are not XBRL instance documents and would only cause rate-limit hammering.
            HIGH_CONFIDENCE_SCORE = 90

            # Collect all confidently-validated candidates in score order
            for i, candidate in enumerate(scored, 1):
                url = candidate['url']
                logger.debug(f"  [{i}/{len(scored)}] {candidate['filename']} (score: {candidate['score']})")

                # Skip low-confidence candidates entirely — no HTTP request made
                if candidate['score'] < HIGH_CONFIDENCE_SCORE:
                    logger.debug(f"      ⏭ Score {candidate['score']} < {HIGH_CONFIDENCE_SCORE}, skipping")
                    continue

                # High-confidence candidates are trusted by filename pattern alone.
                # The SEC directory listing already confirmed the file is present — no
                # additional HTTP check is needed or desirable. Transient HEAD failures
                # under load would otherwise silently drop valid XBRL files. Arelle's
                # own load step in extract_financial_statements() handles any real failures.
                logger.debug(f"      ✅ High-confidence instance accepted (trusted by directory listing)")
                self._last_xbrl_candidates.append(url)

            if self._last_xbrl_candidates:
                best = self._last_xbrl_candidates[0]
                logger.info(f"✅ Best XBRL candidate: {best.split('/')[-1]} "
                            f"({len(self._last_xbrl_candidates)} total candidates available)")
                return best

            # Fallback: try Arelle-validation for lower-confidence candidates (one at a time,
            # stopping at the first one that passes — avoids bulk HTTP hammering)
            for candidate in scored:
                if candidate['score'] >= HIGH_CONFIDENCE_SCORE:
                    continue  # already tried above
                url = candidate['url']
                if self._validate_xbrl_url(url) and self._validate_xbrl_instance(url):
                    logger.info(f"✅ Lower-confidence XBRL instance validated: {candidate['filename']}")
                    self._last_xbrl_candidates.append(url)
                    return url

            # Absolute last resort — use top-scored accessible URL without validation
            for candidate in scored:
                if self._validate_xbrl_url(candidate['url']):
                    logger.warning(f"⚠️  All candidates failed validation; using top-scored accessible: "
                                   f"{candidate['filename']}")
                    self._last_xbrl_candidates = [candidate['url']]
                    return candidate['url']

            fallback = scored[0]['url']
            logger.warning(f"⚠️  All candidates failed URL validation; using top-scored as last resort: "
                           f"{scored[0]['filename']}")
            self._last_xbrl_candidates = [fallback]
            return fallback

        except Exception as e:
            logger.debug(f"Error discovering XBRL from directory: {e}")

        return None
    
    def _discover_html_from_directory(self, directory_url: str, accession_number: str) -> Optional[str]:
        """
        Discover HTML filing from directory listing
        """
        try:
            _global_rate_limiter.acquire()
            response = self.session.get(directory_url, timeout=10)
            
            if response.status_code != 200:
                logger.debug(f"Directory not accessible: {directory_url} (HTTP {response.status_code})")
                return None
            
            return self._discover_html_from_content(directory_url, response.text, accession_number)
        
        except Exception as e:
            logger.debug(f"Error discovering HTML from directory: {e}")
        
        return None
    
    def _discover_html_from_content(self, directory_url: str, html_content: str, accession_number: str) -> Optional[str]:
        """Parse directory HTML content to find HTML filing"""
        # Parse directory listing
        file_links = self._parse_file_links(html_content)
        
        # Score and prioritize HTML files
        html_candidates = []
        
        for filename in file_links:
            filename_lower = filename.lower()
            
            # Look for HTML files, skip exhibits and supplemental files
            if filename_lower.endswith(('.htm', '.html')):
                score = self._score_html_file(filename, accession_number)
                if score > 0:
                    html_candidates.append({
                        'filename': filename,
                        'url': directory_url + filename,
                        'score': score
                    })
        
        if html_candidates:
            # Sort by score and return best candidate
            html_candidates.sort(key=lambda x: x['score'], reverse=True)
            best = html_candidates[0]
            logger.debug(f"Found HTML file: {best['filename']} (score: {best['score']})")
            return best['url']
        
        return None
    
    def _parse_file_links(self, html_content: str) -> List[str]:
        """Parse file links from SEC directory HTML listing"""
        file_links = []
        href_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
        matches = re.findall(href_pattern, html_content, re.IGNORECASE)
        
        for href in matches:
            if href and not href.startswith('../') and not href == '/':
                # Extract just the filename from full paths
                if href.startswith('/Archives/edgar/data/'):
                    filename = href.split('/')[-1]
                    if filename:
                        file_links.append(filename)
                elif href.startswith('./'):
                    file_links.append(href[2:])
                elif not href.startswith('/'):
                    file_links.append(href)
        
        return file_links
    
    def _score_xbrl_file(self, filename: str, accession_number: str) -> int:
        """
        Score XBRL files to identify the best instance document
        
        Priority (based on actual SEC filing structures):
        1. .xml with ticker-date pattern (e.g., nflx-20170930.xml) - 120 (traditional XBRL instance)
        2. .htm iXBRL files with ticker-date pattern (e.g., nflx-20241231.htm) - 110 (modern iXBRL)
        3. _htm.xml files (modern iXBRL instances) - 100
        4. Ticker-date.xml pattern (e.g., msft-20141231.xml) - 90
        5. Files matching accession number - 80
        6. Other .xml files - 50
        7. .txt files - 30
        """
        filename_lower = filename.lower()
        score = 0
        
        # Skip non-XBRL files and schema/linkbase files
        skip_patterns = [
            'filingsummary', 'def.xml', 'lab.xml', 'pre.xml', 'cal.xml', 
            'schema', '_def.xml', '_lab.xml', '_pre.xml', '_cal.xml',
            'index', '-index', 'ex', 'dex', 'graphic', 'logo', 'image', 
            '.jpg', '.png', '.gif', '.css', '.js', 'img', '.xsd',
            '10qxdoc', '10kxdoc', 'doc.htm', 'document.htm'  # Skip document HTML files
        ]
        for pattern in skip_patterns:
            if pattern in filename_lower:
                return 0
        
        # Tier 1a: Traditional XBRL .xml instance with ticker-date pattern (e.g., nflx-20170930.xml)
        # This is the STANDARD format for traditional XBRL (pre-2019)
        if filename_lower.endswith('.xml') and re.search(r'[a-z]{2,5}-\d{8}\.xml$', filename_lower):
            score = 120
            # Small bonus for short ticker (more likely to be correct)
            if len(filename) < 25:
                score += 5
            return score
        
        # Tier 1b: Modern iXBRL _htm.xml files (e.g., msft-20240930_htm.xml)
        # This is the SEC-generated "EXTRACTED XBRL INSTANCE DOCUMENT" — the authoritative
        # instance for modern iXBRL filings. Preferred over the raw .htm because some
        # companies (e.g. Wells Fargo 10-K) embed financial facts only in an EX-13 exhibit
        # while the main .htm is a narrative document sharing the same presentation linkbase.
        # The _htm.xml always contains the complete, consolidated set of facts.
        if filename_lower.endswith('_htm.xml'):
            score = 125
            # Bonus if matches accession number
            if accession_number.replace('-', '') in filename:
                score += 20
            return score

        # Tier 1c: Modern .htm iXBRL files with ticker-date pattern (e.g., nflx-20241231.htm)
        # Used as fallback when _htm.xml is not present (e.g. very early iXBRL filings).
        if filename_lower.endswith('.htm') and re.search(r'[a-z]{2,5}-\d{8}\.htm$', filename_lower):
            score = 110
            # Small bonus for short ticker (more likely to be correct)
            if len(filename) < 25:
                score += 5
            return score
        
        # Tier 2: Ticker-date XBRL pattern (e.g., msft-20141231.xml)
        # This is the primary instance document for legacy XBRL filings
        if re.search(r'[a-z]{2,5}-\d{8}\.xml$', filename_lower):
            score = 90
            # Small bonus if it's a short ticker (more likely to be correct)
            if len(filename) < 25:
                score += 5
            return score
        
        # Tier 3: Files matching accession number
        if accession_number.replace('-', '') in filename:
            if filename_lower.endswith('.xml'):
                score = 80
            elif filename_lower.endswith('.txt'):
                score = 70
            return score
        
        # Tier 4: Other .xml files (but not schema/linkbase files)
        if filename_lower.endswith('.xml'):
            # Prefer shorter filenames (likely instance documents)
            if len(filename) < 30:
                score = 60
            else:
                score = 50
            return score
        
        # Tier 5: .txt files (fallback for inline XBRL)
        if filename_lower.endswith('.txt'):
            score = 30
        
        return score
    
    def _score_html_file(self, filename: str, accession_number: str) -> int:
        """
        Score HTML files to identify the primary document
        Based on actual SEC filing patterns observed across different eras
        
        Priority (from actual SEC filings):
        1. Modern ticker-date (msft-20240930.htm) - 100
        2. Ticker-date for 10-K/Q (msft-10q_20180630.htm) - 90  
        3. Accession number match - 80
        4. Simple form pattern (d827041d10q.htm, d266753d10q.htm) - 70
        5. Other patterns - 20-50
        
        Excludes: exhibits (ex, dex prefix), R-numbered sections, graphics, etc.
        """
        filename_lower = filename.lower()
        score = 0
        
        # Skip patterns - exclude these immediately
        skip_patterns = [
            'index', '-index', 'ex', 'dex',  # Exhibits have 'ex' or 'dex' in name
            'graphic', 'logo', 'image', '.jpg', '.png', '.gif', '.css', '.js', 'img'
        ]
        
        for pattern in skip_patterns:
            if pattern in filename_lower:
                return 0
        
        # Only process .htm files (not .html)
        if not filename_lower.endswith('.htm'):
            return 0
        
        # Major penalty for R-numbered section files (R1.htm, R2.htm, etc.)
        # These are individual sections/pages, not primary documents
        if re.search(r'^r\d+\.htm$', filename_lower):
            return 5  # Very low score
        
        # Tier 1: Modern ticker-date patterns (msft-20240930.htm) - 100
        # This is the standard modern format with iXBRL
        if re.search(r'^[a-z]{2,5}-\d{8}\.htm$', filename_lower):
            score = 100
            # Small bonus if matches accession number pattern
            if accession_number.replace('-', '') in filename:
                score += 10
            return score
        
        # Tier 2: Ticker with form type (msft-10k_20180630.htm) - 90
        # Used in some older modern filings
        if re.search(r'^[a-z]{2,5}-\d+[a-z]+_\d{8}\.htm$', filename_lower):
            return 90
        
        # Tier 3: Files matching accession number - 80
        # High confidence this is the primary document
        if accession_number.replace('-', '') in filename:
            return 80
        
        # Tier 4: Simple form pattern (d827041d10q.htm, d266753d10q.htm) - 70
        # This is the PRIMARY DOCUMENT pattern for legacy filings (2012-2018)
        # Pattern: d[numbers]d10q.htm or d[numbers]d10k.htm
        if re.search(r'^d\d+d10[qk]\.htm$', filename_lower):
            score = 70
            return score
        
        # Tier 4b: Other simple form files without 'ex' - 65
        # Generic simple patterns that aren't exhibits
        if re.search(r'^[a-z]*\d+[a-z]*\.htm$', filename_lower) and len(filename) < 20:
            score = 65
            # Penalty for very long number sequences (less likely primary)
            if re.search(r'\d{6,}', filename_lower):
                score = 40
            return score
        
        # Tier 5: Other company ticker patterns - 50
        if re.search(r'^[a-z]+-.*\.htm$', filename_lower):
            return 50
        
        # Tier 6: Generic files - 20-40
        score = 20
        
        # Form indicators bonus
        form_indicators = ['10-k', '10-q', '8-k', '20-f', '10k', '10q', '8k', '20f']
        for indicator in form_indicators:
            if indicator in filename_lower:
                score += 15
                break
        
        # Primary document indicators
        primary_indicators = ['instance', 'document', 'filing']
        for indicator in primary_indicators:
            if indicator in filename_lower:
                score += 20
                break
        
        return max(0, score)
    
    def _validate_xbrl_url(self, url: str) -> bool:
        """
        Validate that a URL points to a potential XBRL instance document
        Does a lightweight check without full parsing
        
        Returns:
            bool: True if URL appears to be a valid XBRL/iXBRL file
        """
        try:
            _global_rate_limiter.acquire()
            # Use HEAD request first to check if file exists
            response = self.session.head(url, timeout=5, allow_redirects=True)
            
            # If HEAD doesn't work, try GET with limited content
            if response.status_code != 200:
                response = self.session.get(url, timeout=10, stream=True)
                
            if response.status_code != 200:
                return False
            
            # For .htm/.html files (iXBRL), check content type and initial content
            if url.lower().endswith(('.htm', '.html')):
                # Get first few KB to validate it's HTML/XML-like content
                response = self.session.get(url, timeout=10, stream=True)
                chunk = next(response.iter_content(chunk_size=2048), b'')
                content_preview = chunk.decode('utf-8', errors='ignore').lower()
                
                # Valid iXBRL should contain HTML/XML and XBRL namespace indicators
                has_html_xml = any(marker in content_preview for marker in ['<html', '<?xml', 'xbrl'])
                has_xbrl_indicators = any(ns in content_preview for ns in [
                    'xbrli', 'dei:', 'us-gaap:', 'ifrs:', 'xmlns:',
                    'contextref', 'unitref', 'instant', 'duration'
                ])
                
                return has_html_xml or has_xbrl_indicators
            
            # For .xml files, check if it's likely an XBRL instance (not schema/linkbase)
            elif url.lower().endswith('.xml'):
                response = self.session.get(url, timeout=10, stream=True)
                chunk = next(response.iter_content(chunk_size=2048), b'')
                content_preview = chunk.decode('utf-8', errors='ignore').lower()
                
                # Should have XML declaration and XBRL namespaces
                has_xml = '<?xml' in content_preview
                has_xbrl = 'xbrli' in content_preview or 'xbrl' in content_preview
                
                # Exclude schema/linkbase files — use specific patterns to avoid false positives.
                # NOTE: bare 'schema' and 'linkbase' appear in xmlns namespace declarations of
                # valid XBRL instance documents (e.g. xmlns:xsd="...XMLSchema",
                # xmlns:link="...linkbase"), so check for element-level markers only.
                is_schema_linkbase = any(marker in content_preview for marker in [
                    'xsd:schema', 'xs:schema',
                    '<link:linkbase', '<linkbase',
                    'xs:element ', 'xsd:element ',
                ])
                
                return has_xml and has_xbrl and not is_schema_linkbase
            
            # For .txt files, they're inline XBRL wrapped in text - accept if accessible
            elif url.lower().endswith('.txt'):
                return True  # If it exists and is accessible, it's likely valid
                
            return False
            
        except Exception as e:
            logger.debug(f"Validation failed for {url}: {e}")
            return False
    
    def _validate_xbrl_instance(self, url: str) -> bool:
        """
        Validate that a URL is a proper XBRL instance document using Arelle
        This is the definitive check - if Arelle can load it, it's valid XBRL
        
        Returns:
            bool: True if Arelle can load this as an XBRL instance document
        """
        try:
            from arelle import Cntlr, ModelManager
            import logging as arelle_logging
            
            # Suppress Arelle warnings - apply to all relevant loggers
            for logger_name in ['arelle', 'arelle.ModelXbrl', 'arelle.ModelDocument', 'arelle.ValidateInlineXBRL']:
                arelle_logger = arelle_logging.getLogger(logger_name)
                arelle_logger.addFilter(ArelleTransformationWarningFilter())
                arelle_logger.setLevel(arelle_logging.CRITICAL)  # Only show critical errors, suppress all warnings
                # Remove all handlers to prevent output
                arelle_logger.handlers = []
                arelle_logger.propagate = False  # Prevent propagation to parent loggers
            
            # Initialize Arelle controller normally
            controller = Cntlr.Cntlr()
            controller.webCache.workOffline = False
            model_manager = ModelManager.initialize(controller)
            
            # Try to load the XBRL instance
            logger.debug(f"      Validating with Arelle: {url}")
            model_xbrl = model_manager.load(url)
            
            if model_xbrl:
                # Check if it has facts (actual XBRL instance content)
                has_facts = len(model_xbrl.facts) > 0 if hasattr(model_xbrl, 'facts') else False
                
                # Check if it has contexts (required for XBRL instances)
                has_contexts = len(model_xbrl.contexts) > 0 if hasattr(model_xbrl, 'contexts') else False
                
                # Don't check model_xbrl.errors - transformation namespace warnings show as errors
                # but don't prevent extraction. Only check for facts and contexts existence.
                if has_facts and has_contexts:
                    logger.debug(f"      ✅ Valid XBRL instance ({len(model_xbrl.facts)} facts, {len(model_xbrl.contexts)} contexts)")
                    model_xbrl.close()
                    return True
                else:
                    logger.debug(f"      ❌ Not an instance document (facts: {has_facts}, contexts: {has_contexts})")
                    model_xbrl.close()
                    return False
            else:
                logger.debug(f"      ❌ Arelle failed to load document")
                return False
                
        except Exception as e:
            logger.debug(f"      ❌ Arelle validation exception: {str(e)[:100]}")
            return False
    
    def _check_url_exists(self, url: str) -> bool:
        """Check if a URL exists (returns 200)"""
        try:
            _global_rate_limiter.acquire()
            response = self.session.head(url, timeout=5, allow_redirects=True)
            return response.status_code == 200
        except Exception:
            return False
    
    def download_file(self, url: str, timeout: int = 30) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Download file from URL
        
        Returns:
            Tuple of (success: bool, content: str, error_message: str)
        """
        try:
            _global_rate_limiter.acquire()
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
            return True, response.text, None
        except requests.exceptions.HTTPError as e:
            return False, None, f"HTTP {e.response.status_code}: {e}"
        except requests.exceptions.RequestException as e:
            return False, None, str(e)
