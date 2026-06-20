#!/usr/bin/env python3
"""SEC API client for fetching company filings and data"""

import logging
import requests
import json
import time
import gzip
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from utilities.helpers.period_utils import FiscalYearCalculator
from utilities.sec_url_detector import SECURLDetector

logger = logging.getLogger(__name__)

class SECAPIClient:
    """Client for interacting with SEC EDGA            # Construct SEC directory URL using the same pattern as XBRL
            directory_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{clean_accession}/"
            
            # Debug: Log the URL being accessed (only in verbose mode)
            import os
            if os.getenv('DEBUG_HTML_DOWNLOAD'):
                print(f"    🔍 Accessing: {directory_url}")
            
            # Try to get the directory listing first (same as XBRL approach)
            html_url = self._discover_html_file_url(directory_url, clean_accession_number)"""
    
    BASE_URL = "https://data.sec.gov"
    
    def __init__(self, user_agent="statements@colab.net", rate_limit_delay=0.1, database=None):
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self.database = database
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept-Encoding': 'gzip, deflate',
            'Host': 'data.sec.gov'
        })
        # Initialize unified URL detector
        self.url_detector = SECURLDetector(user_agent=user_agent, rate_limit_delay=rate_limit_delay)
        # Load ticker mappings for folder organization
        self._load_ticker_mappings()
    
    def _load_ticker_mappings(self):
        """Load ticker mappings from tickers.json for folder organization"""
        try:
            import json
            from pathlib import Path
            
            tickers_file = Path("tickers.json")
            if tickers_file.exists():
                with open(tickers_file, 'r') as f:
                    ticker_data = json.load(f)
                # Create CIK to ticker mapping
                ticker_to_cik = ticker_data.get('ticker_to_cik', {})
                self.cik_to_ticker = {v: k for k, v in ticker_to_cik.items()}
            else:
                self.cik_to_ticker = {}
        except Exception as e:
            logger.warning(f"Failed to load ticker mappings: {e}")
            self.cik_to_ticker = {}
    
    def get_ticker_from_cik(self, cik: str) -> Optional[str]:
        """Get ticker symbol from CIK"""
        # Normalize CIK by removing leading zeros for lookup
        normalized_cik = str(int(cik)) if cik.isdigit() else cik
        return self.cik_to_ticker.get(cik) or self.cik_to_ticker.get(normalized_cik)
    
    def _get_fiscal_year_end_from_db(self, cik: str) -> Optional[str]:
        """Get company fiscal year end from database"""
        if self.database is None:
            return None
        
        try:
            companies_collection = self.database.companies
            company_doc = companies_collection.find_one({'cik': cik})
            
            if company_doc:
                # Try different possible field names for fiscal year end
                fiscal_year_end = (company_doc.get('corporate_info', {}).get('fiscal_year_end') or
                                 company_doc.get('fiscal_year_end_code') or 
                                 company_doc.get('fiscalYearEnd'))
                return fiscal_year_end
            
        except Exception as e:
            logger.warning(f"Failed to get fiscal year end from database: {e}")
        
        return None
    
    def _make_request(self, url: str, max_retries: int = 3, backoff_base: float = 2.0) -> Optional[Dict]:
        """Make rate-limited request to SEC API with retry logic for transient errors"""
        for attempt in range(max_retries):
            try:
                time.sleep(self.rate_limit_delay)  # Rate limiting
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait = backoff_base ** attempt
                if attempt < max_retries - 1:
                    logger.warning(f"API request failed for {url}: {e} — retrying in {wait:.0f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                else:
                    logger.warning(f"API request failed for {url}: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                logger.warning(f"API request failed for {url}: {e}")
                return None
            except json.JSONDecodeError as e:
                logger.warning(f"JSON parsing failed for {url}: {e}")
                return None
    
    def get_company_submissions(self, cik: str, start_year: int = 2010) -> Tuple[Optional[Dict], List[Dict]]:
        """
        Fetch company submissions from SEC API including historical data
        
        Args:
            cik: Company CIK identifier
            start_year: Earliest year to include filings from (default: 2010)
            
        Returns:
            Tuple of (company_info, filings_list)
        """
        # Format CIK with leading zeros (10 digits)
        cik_formatted = str(cik).zfill(10)
        url = f"{self.BASE_URL}/submissions/CIK{cik_formatted}.json"
        
        data = self._make_request(url)
        if not data:
            return None, []
        
        # Extract company information
        company_info = self._extract_company_info(data)
        
        # Extract recent filings with date filtering
        filings = self._extract_filings(data, start_year=start_year, company_cik=cik_formatted)
        
        # Fetch historical data from additional files if needed
        historical_filings = self._fetch_historical_filings(cik_formatted, data, start_year)
        filings.extend(historical_filings)
        
        # Sort filings by date (most recent first)
        filings.sort(key=lambda x: x.get('filingDate', ''), reverse=True)
        
        return company_info, filings
    
    def _extract_company_info(self, data: Dict) -> Dict:
        """Extract and structure company information from SEC response"""
        return {
            'cik': data.get('cik'),
            'name': data.get('name'),
            'sic': data.get('sic'),
            'sicDescription': data.get('sicDescription'),
            'tickers': data.get('tickers', []),
            'exchanges': data.get('exchanges', []),
            'entityType': data.get('entityType'),
            'category': data.get('category'),
            'fiscalYearEnd': data.get('fiscalYearEnd'),
            'stateOfIncorporation': data.get('stateOfIncorporation'),
            'stateOfIncorporationDescription': data.get('stateOfIncorporationDescription'),
            'addresses': data.get('addresses', {}),
            'phone': data.get('phone'),
            'flags': data.get('flags'),
            'formerNames': data.get('formerNames', []),
            'filings': data.get('filings', {})
        }
    
    def _extract_filings(self, data: Dict, forms_filter: List[str] = ['10-K', '10-Q'], start_year: int = 2010, company_cik: Optional[str] = None) -> List[Dict]:
        """Extract filings from SEC response, filtered by form types and fiscal year range"""
        # Handle both main response format and historical file format
        if 'filings' in data and 'recent' in data['filings']:
            # Main submissions response format
            recent_filings = data.get('filings', {}).get('recent', {})
        else:
            # Historical file format - data is directly in the root
            recent_filings = data
        
        filings_data = []
        forms = recent_filings.get('form', [])
        accession_numbers = recent_filings.get('accessionNumber', [])
        filing_dates = recent_filings.get('filingDate', [])
        acceptance_dates = recent_filings.get('acceptanceDateTime', [])
        report_dates = recent_filings.get('reportDate', [])
        fiscal_year_ends = recent_filings.get('fiscalYearEnd', [])
        
        # Get company fiscal year end from main data (fallback if per-filing data is missing)
        company_fiscal_year_end = data.get('fiscalYearEnd')
        
        # If not available in SEC data, try to get from database
        if not company_fiscal_year_end and company_cik:
            company_fiscal_year_end = self._get_fiscal_year_end_from_db(company_cik)
        
        for i, form in enumerate(forms):
            if form in forms_filter:
                filing_date = filing_dates[i]
                
                # Get filing-specific data
                report_date = report_dates[i] if i < len(report_dates) else None
                fiscal_year_end = fiscal_year_ends[i] if i < len(fiscal_year_ends) else company_fiscal_year_end
                
                # Filter by fiscal year using proper calculation
                should_include = False
                try:
                    # First, try simple filing year check
                    filing_year = int(filing_date.split('-')[0])
                    if filing_year >= start_year:
                        should_include = True
                    
                    # If filing year is before start_year, check if the fiscal year is >= start_year
                    if not should_include and report_date:
                        # Use filing-specific fiscal year end, or fallback to company fiscal year end
                        effective_fiscal_year_end = fiscal_year_end or company_fiscal_year_end
                        
                        if effective_fiscal_year_end:
                            try:
                                report_end_date = datetime.strptime(report_date, '%Y-%m-%d')
                                fiscal_year, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                                    report_end_date, effective_fiscal_year_end
                                )
                                
                                # Include if the calculated fiscal year is >= start_year
                                if fiscal_year and fiscal_year >= start_year:
                                    should_include = True
                                    
                            except (ValueError, AttributeError) as e:
                                # If fiscal year calculation fails, fall back to filing year
                                pass
                    
                    if not should_include:
                        continue
                        
                except (ValueError, IndexError):
                    # Skip if date parsing fails
                    continue
                
                filing_info = {
                    'form': form,
                    'accessionNumber': accession_numbers[i],
                    'filingDate': filing_date,
                    'acceptanceDateTime': acceptance_dates[i] if i < len(acceptance_dates) else None,
                    # Extract period information directly from SEC API
                    'reportDate': report_date,
                    'primaryDocument': recent_filings.get('primaryDocument', [])[i] if i < len(recent_filings.get('primaryDocument', [])) else None,
                    'primaryDocDescription': recent_filings.get('primaryDocDescription', [])[i] if i < len(recent_filings.get('primaryDocDescription', [])) else None,
                    'fiscalYearEnd': fiscal_year_end,
                    'isXBRL': recent_filings.get('isXBRL', [])[i] if i < len(recent_filings.get('isXBRL', [])) else None
                }
                filings_data.append(filing_info)
        
        return filings_data
    
    def _fetch_historical_filings(self, cik_formatted: str, main_data: Dict, start_year: int) -> List[Dict]:
        """Fetch historical filings from additional SEC JSON files"""
        historical_filings = []
        
        # Get the files array from the main response
        files_info = main_data.get('filings', {}).get('files', [])
        
        if not files_info:
            logger.debug("No historical filing files found")
            return historical_filings
        
        logger.debug(f"Found {len(files_info)} historical filing files to process")
        
        for file_info in files_info:
            file_name = file_info.get('name', '')
            
            # Construct URL for historical file
            file_url = f"{self.BASE_URL}/submissions/{file_name}"
            
            logger.debug(f"Fetching historical data from: {file_name}")
            
            # Fetch the historical filing data
            historical_data = self._make_request(file_url)
            if not historical_data:
                logger.warning(f"Failed to fetch historical data from {file_name} — some filings may be missing for CIK {cik_formatted}")
                continue
            
            # Extract filings from historical data
            historical_batch = self._extract_filings(historical_data, start_year=start_year, company_cik=cik_formatted)
            
            if historical_batch:
                # Get date range of this batch
                dates = [f.get('filingDate', '') for f in historical_batch if f.get('filingDate')]
                if dates:
                    earliest = min(dates)
                    latest = max(dates)
                    logger.debug(f"Added {len(historical_batch)} filings from {file_name} (date range: {earliest} to {latest})")
                else:
                    logger.debug(f"Added {len(historical_batch)} filings from {file_name}")
            else:
                logger.debug(f"No relevant filings found in {file_name} for year {start_year}+")
            
            historical_filings.extend(historical_batch)
        
        if historical_filings:
            # Show overall date range of historical filings
            all_dates = [f.get('filingDate', '') for f in historical_filings if f.get('filingDate')]
            if all_dates:
                earliest = min(all_dates)
                latest = max(all_dates)
                logger.debug(f"Total historical filings found: {len(historical_filings)} (date range: {earliest} to {latest})")
            else:
                logger.debug(f"Total historical filings found: {len(historical_filings)}")
        else:
            logger.debug("No historical filings found matching the criteria")
        
        return historical_filings
    
    def get_company_facts(self, cik: str) -> Optional[Dict]:
        """
        Get company facts (financial data) from SEC API
        
        Args:
            cik: Company CIK identifier
            
        Returns:
            Company facts data or None
        """
        cik_formatted = str(cik).zfill(10)
        url = f"{self.BASE_URL}/api/xbrl/companyfacts/CIK{cik_formatted}.json"
        
        return self._make_request(url)
    
    def search_companies(self, query: str) -> List[Dict]:
        """
        Search for companies (Note: This is a placeholder - SEC doesn't provide direct search)
        In a real implementation, you might use company tickers API or maintain your own index
        """
        # This would require a different approach, possibly using company tickers endpoint
        # or maintaining a local search index
        logger.debug(f"Company search not directly available via SEC API for query: {query}")
        return []
    
    def validate_cik(self, cik: str) -> bool:
        """Validate if a CIK exists by attempting to fetch submissions"""
        company_info, _ = self.get_company_submissions(cik)
        return company_info is not None
    
    def get_filing_documents(self, cik: str, accession_number: str) -> Optional[Dict]:
        """
        Get filing documents for a specific filing
        
        Args:
            cik: Company CIK
            accession_number: Filing accession number
            
        Returns:
            Filing documents data
        """
        # Remove dashes from accession number for URL
        accession_clean = accession_number.replace('-', '')
        cik_formatted = str(cik).zfill(10)
        
        url = f"{self.BASE_URL}/submissions/CIK{cik_formatted}.json"
        
        # For now, this returns the submissions data
        # In a full implementation, you might want to fetch specific filing documents
        return self._make_request(url)

    def download_html_filing(self, cik: str, accession_number: str, filing_date: str, download_path: str, ticker: Optional[str] = None) -> bool:
        """
        Download HTML filing and save it compressed with accession number as filename
        Uses unified URL detector that handles both modern and legacy filings
        
        Args:
            cik: Company CIK
            accession_number: Filing accession number
            filing_date: Filing date in YYYY-MM-DD format
            download_path: Directory path to save the HTML file
            ticker: Optional ticker symbol for organizing files in folders
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Use the download_path directly as base
            base_path = Path(download_path)
            
            # Require ticker for folder organization
            if not ticker:
                logger.info(f"No ticker found for CIK {cik}, skipping HTML download")
                return False
            
            # Create ticker-based folder
            ticker_folder = base_path / ticker.upper()
            ticker_folder.mkdir(parents=True, exist_ok=True)
            final_download_path = ticker_folder
            
            # Clean accession number - remove -xbrl suffix if present
            clean_accession_number = accession_number.replace('-xbrl', '') if accession_number.endswith('-xbrl') else accession_number
            
            # Validate accession number format
            if not re.match(r'^\d{10}-\d{2}-\d{6}$', clean_accession_number):
                logger.warning(f"Invalid accession number format: {accession_number} (cleaned: {clean_accession_number})")
                print(f"    Expected format: ##########-##-######")
                return False
            
            # Skip if file already exists on disk
            filename = f"{clean_accession_number}.html.gz"
            filepath = ticker_folder / filename
            if filepath.exists():
                logger.debug(f"HTML filing already exists, skipping: /{ticker.upper()}/{filename}")
                return True
            
            # Use unified URL detector to find HTML filing
            logger.info(f"Detecting URLs for filing: {clean_accession_number} (date: {filing_date})")
            urls = self.url_detector.detect_filing_urls(cik, clean_accession_number, filing_date)
            
            html_url = urls.get('html_url')
            if not html_url or not isinstance(html_url, str):
                logger.warning(f"Failed to discover HTML filing URL for {clean_accession_number}")
                if urls.get('is_legacy'):
                    logger.debug("Note: This is a legacy filing (pre-2019) - HTML may not be available")
                logger.debug(f"Directory checked: {urls.get('directory_url')}")
                return False
            
            # Download the HTML file
            try:
                logger.info(f"Downloading from: {html_url}")
                success, html_content, error = self.url_detector.download_file(html_url)
                
                if not success or not html_content:
                    logger.warning(f"Failed to download HTML: {error or 'No content received'}")
                    return False
                
                # Create compressed filename (already computed above)
                with gzip.open(filepath, 'wt', encoding='utf-8') as f:
                    f.write(html_content)
                
                logger.info(f"Downloaded HTML filing: /{ticker.upper()}/{filename} ({len(html_content):,} chars)")
                return True
                
            except Exception as e:
                logger.error(f"Error saving HTML file: {e}")
                return False
            
        except Exception as e:
            logger.error(f"Error processing HTML filing {accession_number}: {e}")
            return False