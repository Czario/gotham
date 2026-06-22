#!/usr/bin/env python3

import sys
import os
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Ensure the project root is on sys.path when running as an installed entry point
_PROJECT_ROOT = str(Path(__file__).resolve().parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load environment variables from .env file
load_dotenv()

# Setup logging first
from utilities.helpers.logger_config import LoggerConfig, get_module_logger

# Initialize loggers - only main logger, no separate error/performance logs
# Change module-level logger to WARNING/no-console — main() reconfigures per --verbose/--debug
logger = LoggerConfig.setup_logging(
    level='WARNING',
    log_dir='logs',
    detailed=False,
    console_output=False
)

from database.config.mongodb_config import DatabaseConfig
from api.sec_client import SECAPIClient
from core.processors.statement_processor import EnhancedFinancialStatementProcessor
# Raw DB repository layer removed — normalization service writes directly to normalize_data
from core.transformers.data_transformers import CompanyDataTransformer, FilingDataTransformer, FinancialDataTransformer
from utilities.helpers.error_handling import (
    safe_processing_operation, 
    validate_required_fields, 
    log_operation_result,
    format_filing_log_message
)
import re
from tqdm import tqdm

def _fmt_time(seconds: float) -> str:
    """Convert seconds to a human-readable string: 5s, 2m 15s, 1h 23m."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# tqdm.format_meter() calls tqdm.format_interval() directly (not via self),
# so subclass overrides are ignored. Patch the class method globally instead.
tqdm.format_interval = staticmethod(_fmt_time)  # type: ignore[method-assign]

# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------
class ProgressManager:
    """Clean progress bars for multi-company processing.

    In verbose mode all tqdm bars are disabled and full log output flows to
    the console. In default (clean) mode logger console output is suppressed
    and progress is communicated exclusively via tqdm bars.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._company_bar: Optional["tqdm"] = None
        self._filing_bar: Optional["tqdm"] = None
        self._current_index: int = 0
        # Maps CIK -> the ticker symbol the user actually typed, so the bar
        # shows e.g. "JPM" instead of whatever the reverse CIK->ticker map
        # happens to return (a CIK can map to multiple tickers).
        self.cik_labels: dict = {}

    def label(self, cik: str, fallback: str) -> str:
        """Return the user-supplied ticker for a CIK, else the fallback."""
        if not cik:
            return fallback
        return (self.cik_labels.get(cik)
                or self.cik_labels.get(str(cik).zfill(10))
                or self.cik_labels.get(str(cik).lstrip('0'))
                or fallback)

    # -- Company-level bar --------------------------------------------------
    def start_companies(self, total: int) -> None:
        if self.verbose:
            return
        self._company_bar = tqdm(
            total=total,
            desc="waiting       ",
            unit="co",
            colour="cyan",
            dynamic_ncols=True,
            position=0,
            leave=True,
            bar_format="Companies [{desc}] {bar} {n_fmt}/{total_fmt}  ⏱ {elapsed}  eta {remaining}",
        )

    def set_current_company(self, ticker: str, index: Optional[int] = None) -> None:
        """Show the company currently being processed in the outer bar.

        The bar position reflects *completed* companies, so while company
        ``index`` is in progress the bar sits at ``index - 1``. If ``index``
        is None, only the description is updated.
        """
        if self.verbose or self._company_bar is None:
            return
        if index is not None:
            self._current_index = index
            target = index - 1  # companies finished before this one
            delta = target - self._company_bar.n
            if delta > 0:
                self._company_bar.update(delta)
        self._company_bar.set_description_str(f"{ticker:<6} processing")
        self._company_bar.refresh()

    def advance_company(self, ticker: str, processed: int, skipped: int,
                        failed: int, reconciled: int = 0) -> None:
        if self.verbose or self._company_bar is None:
            return
        parts = []
        if processed:
            parts.append(f"{processed} new")
        if skipped:
            parts.append(f"{skipped} skip")
        if failed:
            parts.append(f"{failed} ❌")
        if reconciled:
            parts.append(f"+{reconciled} filled")
        status = ", ".join(parts) or "done"
        # Mark the current company as finished.
        delta = self._current_index - self._company_bar.n
        if delta > 0:
            self._company_bar.update(delta)
        self._company_bar.set_description_str(f"{ticker:<6} {status}")
        self._company_bar.refresh()

    def finish_companies(self) -> None:
        self.close_filing_bar()
        if self._company_bar is not None:
            self._company_bar.close()
            self._company_bar = None

    # -- Filing-level bar (per company) ------------------------------------
    def filing_bar(self, ticker: str, total: int) -> "tqdm":
        """Return a tqdm bar for the filing loop; disabled in verbose mode.

        The bar is pinned to ``position=1`` so it renders on its own line
        directly beneath the company bar instead of overwriting it. Any
        previously open filing bar is closed first.
        """
        self.close_filing_bar()
        self._filing_bar = tqdm(
            total=total,
            desc=f"{ticker:<6}              ",
            unit="f",
            leave=False,
            disable=self.verbose,
            dynamic_ncols=True,
            colour="green",
            position=1,
            bar_format="  [{desc}] {bar} {n_fmt}/{total_fmt}  ⏱ {elapsed}  eta {remaining}",
        )
        return self._filing_bar

    def close_filing_bar(self) -> None:
        """Close the active filing bar if one is open."""
        if self._filing_bar is not None:
            self._filing_bar.close()
            self._filing_bar = None

    def close_all(self) -> None:
        """Tear down every active bar cleanly (e.g. on interrupt)."""
        self.close_filing_bar()
        if self._company_bar is not None:
            self._company_bar.close()
            self._company_bar = None

    # -- One-line status (verbose suppressed) ------------------------------
    def status(self, msg: str) -> None:
        """Print a status line that appears above progress bars in clean mode."""
        if not self.verbose:
            tqdm.write(msg)

    def error(self, msg: str) -> None:
        tqdm.write(msg)

# ---------------------------------------------------------------------------
# Merged normalization pipeline
# ---------------------------------------------------------------------------
from data_normalization_service.core.config import AppConfig as _NormAppConfig, DatabaseConfig as _NormDBConfig
from data_normalization_service.services.normalization_service import FinancialNormalizationService as _NormService
from data_normalization_service.services.quarterly_service import PeriodBasedFinancialCalculationService as _QuarterlyService

class ProcessingSummary:
    """Track processing statistics for companies and filings with real-time streaming"""
    def __init__(self, report_file: Optional[str] = None):
        self.companies_stats = {}
        self.start_time = datetime.now()
        self.report_file = report_file
        self.company_count = 0
    
    def write_header(self, total_companies: int):
        """Write header to report file at start"""
        if not self.report_file:
            return
        
        try:
            with open(self.report_file, 'w', buffering=1) as f:
                f.write("=" * 80 + "\n")
                f.write("SEC DATA SCRAPER - PROCESSING SUMMARY\n")
                f.write("=" * 80 + "\n\n")
                f.write(f"Start Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Companies to Process: {total_companies}\n\n")
                f.write("PROCESSING PROGRESS\n")
                f.write("-" * 80 + "\n\n")
                f.flush()
        except Exception as e:
            logger.error(f"Failed to write header to report file: {e}")
    
    def update_company(self, cik: str, ticker: str, total_filings: int, 
                      filings_processed: int, filings_skipped: int, filings_failed: int):
        """Record and stream company stats to file"""
        self.company_count += 1
        self.companies_stats[cik] = {
            'ticker': ticker,
            'total_filings': total_filings,
            'processed': filings_processed,
            'skipped': filings_skipped,
            'failed': filings_failed,
            'status': 'success' if filings_failed == 0 else 'partial'
        }
        
        # Stream update to file with immediate flush
        if self.report_file:
            try:
                with open(self.report_file, 'a', buffering=1) as f:
                    f.write(f"[{self.company_count}] Company: {ticker} (CIK: {cik})\n")
                    f.write(f"    Total Filings: {total_filings}\n")
                    f.write(f"    Processed: {filings_processed} | Skipped: {filings_skipped} | Failed: {filings_failed}\n")
                    f.write(f"    Status: {self.companies_stats[cik]['status'].upper()}\n\n")
                    f.flush()
                    os.fsync(f.fileno())  # Force disk write
            except Exception as e:
                logger.error(f"Failed to update report file: {e}")
    
    def log_filing_progress(self, ticker: str, current_filing: int, total_filings: int, accession: str, status: str):
        """Log individual filing progress (e.g., 'processing', 'processed', 'skipped', 'failed')"""
        if not self.report_file:
            return
        
        try:
            with open(self.report_file, 'a', buffering=1) as f:
                f.write(f"      [{current_filing}/{total_filings}] {accession} - {status}\n")
                f.flush()
                os.fsync(f.fileno())  # Force disk write
        except Exception as e:
            logger.error(f"Failed to log filing progress: {e}")
    
    def write_final_summary(self):
        """Write final summary statistics at end"""
        if not self.report_file:
            return
        
        try:
            total_companies = len(self.companies_stats)
            successful_companies = sum(1 for s in self.companies_stats.values() if s['status'] == 'success')
            total_filings = sum(s['total_filings'] for s in self.companies_stats.values())
            total_processed = sum(s['processed'] for s in self.companies_stats.values())
            total_skipped = sum(s['skipped'] for s in self.companies_stats.values())
            total_failed = sum(s['failed'] for s in self.companies_stats.values())
            duration = (datetime.now() - self.start_time).total_seconds()
            
            with open(self.report_file, 'a', buffering=1) as f:
                f.write("=" * 80 + "\n")
                f.write("FINAL SUMMARY\n")
                f.write("-" * 80 + "\n")
                f.write(f"End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Duration: {duration:.1f}s\n\n")
                f.write(f"Companies Processed: {total_companies}\n")
                f.write(f"  Successful: {successful_companies}\n")
                f.write(f"  With Errors: {total_companies - successful_companies}\n\n")
                f.write(f"Total Filings: {total_filings}\n")
                f.write(f"  Processed: {total_processed}\n")
                f.write(f"  Skipped: {total_skipped}\n")
                f.write(f"  Failed: {total_failed}\n\n")
                f.write("=" * 80 + "\n")
                f.flush()
                os.fsync(f.fileno())  # Force disk write
        except Exception as e:
            logger.error(f"Failed to write final summary: {e}")

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

def load_companies_from_tickers(tickers_file: str = "tickers.json", limit: Optional[int] = None, verbose: bool = False) -> List[str]:
    """Load company CIKs from tickers.json file"""
    import json
    from pathlib import Path
    
    tickers_path = Path(tickers_file)
    if not tickers_path.exists():
        print(f"❌ Tickers file not found: {tickers_file}")
        return []
    
    try:
        with open(tickers_path, 'r') as f:
            data = json.load(f)
        
        # Extract CIKs from ticker_to_cik mapping
        ticker_to_cik = data.get('ticker_to_cik', {})
        companies = list(ticker_to_cik.values())
        
        # Remove duplicates while preserving order
        seen = set()
        unique_companies = []
        for cik in companies:
            if cik not in seen:
                seen.add(cik)
                unique_companies.append(cik)
        
        # Apply limit if specified
        if limit is not None and limit > 0:
            unique_companies = unique_companies[:limit]
        
        if verbose:
            print(f"📈 Loaded {len(unique_companies)} companies from {tickers_file}")
            if limit:
                print(f"🎯 Limited to first {limit} companies")
            
        return unique_companies
        
    except Exception as e:
        print(f"❌ Error reading tickers file: {e}")
        return []

class SECDataScraperApp:
    
    def __init__(self, database_config: Optional[DatabaseConfig] = None, start_year: int = 2010, end_year: Optional[int] = None, enable_dimensions: bool = False, target_fiscal_year: Optional[int] = None, target_fiscal_quarter: Optional[str] = None, reload: bool = False, incremental: bool = False, html_download_path: Optional[str] = None, xbrl_zip_cache_path: Optional[str] = None, enable_reconciliation: bool = True, progress: Optional["ProgressManager"] = None):
        # Database setup (source DB kept for scraper internal use; normalization writes to target)
        self.db_config = database_config or DatabaseConfig()
        if not self.db_config.connect():
            raise RuntimeError("Failed to connect to database")
        self.db = self.db_config.get_database()
        self.start_year = start_year
        self.end_year = end_year
        self.enable_dimensions = enable_dimensions
        self.enable_reconciliation = enable_reconciliation
        self.progress = progress or ProgressManager(verbose=True)  # safe default: verbose (no bars)
        self.target_fiscal_year = target_fiscal_year
        self.target_fiscal_quarter = target_fiscal_quarter
        self.reload = reload
        self.incremental = incremental
        self.html_download_path = html_download_path or os.getenv('SEC_HTML_DOWNLOAD_PATH')
        if not self.html_download_path:
            raise RuntimeError(
                "SEC_HTML_DOWNLOAD_PATH is not set. Please set SEC_HTML_DOWNLOAD_PATH in your .env "
                "file (e.g. SEC_HTML_DOWNLOAD_PATH=/Users/aijaz/sec_html_filings) before running."
            )
        self.xbrl_zip_cache_path = xbrl_zip_cache_path or os.getenv('XBRL_ZIP_CACHE_PATH')
        if not self.xbrl_zip_cache_path:
            raise RuntimeError(
                "XBRL_ZIP_CACHE_PATH is not set. Please set XBRL_ZIP_CACHE_PATH in your .env "
                "file (e.g. XBRL_ZIP_CACHE_PATH=/Users/aijaz/xbrl_zip_cache) before running."
            )

        # Lightweight dedup tracker — only stores accession_number + metadata, no raw data
        from pymongo import MongoClient as _MongoClient
        _mongo_uri = os.getenv('MONGODB_URI')
        if not _mongo_uri:
            raise RuntimeError("MONGODB_URI environment variable is required. Set it in your .env file.")
        _database_name = os.getenv('DATABASE_NAME')
        if not _database_name:
            raise RuntimeError("DATABASE_NAME environment variable is required. Set it in your .env file.")
        self._processed_col = _MongoClient(_mongo_uri)[_database_name]['processed_accessions']
        self._processed_col.create_index('cik')

        # ---------------------------------------------------------------------------
        # Normalizer (merged pipeline) — writes directly to normalize_data
        # ---------------------------------------------------------------------------
        _norm_config = _NormAppConfig(
            database=_NormDBConfig(
                mongodb_uri=_mongo_uri,
                database_name=_database_name,
            )
        )
        self._norm_service = _NormService(_norm_config)
        self._quarterly_service = _QuarterlyService(_norm_config)
        # companyfacts reconciliation: fills genuine extraction gaps (incl. the
        # latest filing's edge) from the SEC companyfacts API after each company.
        # Built lazily in _run_companyfacts_reconciliation (needs sec_client).
        self._reconciliation_service = None
        self._reconciliation_db_uri = _mongo_uri
        self._reconciliation_db_name = _database_name
        # Per-company accumulator: company_cik -> list of raw statement dicts
        # (only cash_flow + income statements; used for deaccumulation)
        self._quarterly_accumulator: Dict[str, List[Dict]] = {}
        
        # Initialize services
        self.sec_client = SECAPIClient(database=self.db)
        
        # Initialize filing failure logger (disabled to avoid cluttering logs)
        # self.failure_logger = get_filing_failure_logger()
        
        # Initialize financial processor (always use enhanced processor with configurable dimensions)
        logger.info(f"🎯 Initializing Financial Processor with dimensions {'ENABLED' if enable_dimensions else 'DISABLED'}")
        self.financial_processor = EnhancedFinancialStatementProcessor(
            current_period_only=True,
            max_periods=1,
            include_dimensions=enable_dimensions,
            company_repo=None
        )
            
        self.company_transformer = CompanyDataTransformer()
        self.filing_transformer = FilingDataTransformer()
        self.financial_transformer = FinancialDataTransformer()
        
        logger.info("SEC Data Scraper initialized successfully")

    # ------------------------------------------------------------------
    # XBRL zip replay-cache helpers
    # ------------------------------------------------------------------

    def _xbrl_zip_cache_path_for(self, ticker: str, form_type: str, accession_number: str) -> Path:
        """Return local path for this filing's cached XBRL zip."""
        form_dir = '10-K' if '10-K' in form_type else '10-Q'
        return Path(self.xbrl_zip_cache_path) / ticker / form_dir / f"{accession_number}.zip"

    def _save_xbrl_zip_to_cache(self, ticker: str, form_type: str, accession_number: str,
                                 cik: str) -> None:
        """Download and save the XBRL zip to the local replay cache (idempotent)."""
        dest = self._xbrl_zip_cache_path_for(ticker, form_type, accession_number)
        if dest.exists():
            logger.debug(f"XBRL zip already cached: {dest}")
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Build SEC EDGAR zip URL
            clean_accession = accession_number.replace('-', '')
            zip_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{clean_accession}/{accession_number}-xbrl.zip"
            )
            import requests as _req
            resp = _req.get(zip_url, headers={'User-Agent': self.sec_client.user_agent}, timeout=30)
            if resp.status_code == 200 and resp.content:
                dest.write_bytes(resp.content)
                logger.info(f"Cached XBRL zip: {dest} ({len(resp.content):,} bytes)")
            else:
                logger.debug(f"XBRL zip not available at {zip_url} (status {resp.status_code}), skipping cache")
        except Exception as e:
            logger.debug(f"Could not cache XBRL zip for {accession_number}: {e}")

    # ------------------------------------------------------------------
    # Quarterly accumulator helpers
    # ------------------------------------------------------------------

    def _accumulate_for_quarterly(self, cik: str, statement_doc: dict, filing_doc: dict) -> None:
        """Append a slim copy of the statement (for quarterly deaccumulation) to the per-company accumulator."""
        stmt_type = statement_doc.get('statement_type', '')
        if stmt_type.lower() not in ('cash_flow', 'cashflow', 'cash_flows',
                                      'income_statement', 'income_statements'):
            return  # Balance sheets are skipped — point-in-time, no deaccumulation needed
        entry = {
            '_id': statement_doc.get('_id'),
            'company_cik': cik,
            'statement_type': stmt_type,
            'reporting_period': statement_doc.get('reporting_period', {}),
            'filing_id': statement_doc.get('filing_id'),
            'data': statement_doc.get('data', []),
            '_filing_doc': filing_doc,  # carry filing for accession_number lookup
        }
        self._quarterly_accumulator.setdefault(cik, []).append(entry)

    def _flush_quarterly_accumulator(self, cik: str) -> None:
        """Run deaccumulation for a company then free its accumulator entry."""
        statements = self._quarterly_accumulator.pop(cik, [])
        if not statements:
            return
        logger.info(f"Running quarterly deaccumulation for {cik} ({len(statements)} statements)")
        try:
            self._quarterly_service.process_company_from_statements(cik, statements)
        except Exception as e:
            logger.error(f"Quarterly deaccumulation failed for {cik}: {e}", exc_info=True)

    def _run_companyfacts_reconciliation(self, cik: str) -> None:
        """Fill genuine extraction gaps for a company from the SEC companyfacts
        API after its filings are processed. Edge mode is on, so the latest
        filing's gaps are recovered immediately. INSERT-ONLY and provenance
        tagged; never overwrites primary-extracted values."""
        if not self.enable_reconciliation:
            return
        try:
            if self._reconciliation_service is None:
                from pymongo import MongoClient as _MongoClient
                from data_normalization_service.services.companyfacts_reconciliation import (
                    CompanyFactsReconciliationService as _ReconService,
                )
                recon_db = _MongoClient(self._reconciliation_db_uri)[self._reconciliation_db_name]
                self._reconciliation_service = _ReconService(recon_db, self.sec_client)
            cik_padded = str(cik).zfill(10)
            total = 0
            for freq in ("annual", "quarterly"):
                stats = self._reconciliation_service.reconcile_company(
                    cik_padded, frequency=freq, include_edge=True)
                total += stats.get("filled", 0)
            if total:
                logger.info(f"✅ companyfacts reconciliation filled {total} gap value(s) for CIK {cik}")
        except Exception as e:
            logger.warning(f"companyfacts reconciliation failed for CIK {cik}: {e}", exc_info=True)

    def _build_sec_url(self, company_cik: str, accession_number: str) -> str:
        accession_clean = accession_number.replace('-', '')
        return f"https://www.sec.gov/Archives/edgar/data/{company_cik}/{accession_clean}/{accession_number}-index.htm"
    
    def setup_database(self):
        # Schema/indexes are managed by the normalization service (DatabaseTracker._ensure_indexes)
        # No raw collections (filings, financial_statements) should be created here
        return True
    
    def filter_incremental_filings(self, filings_list: List[Dict], cik: str, latest_date: str) -> List[Dict]:
        """Filter filings to only include those newer than the latest date in database"""
        try:
            latest_dt = datetime.strptime(latest_date, '%Y-%m-%d')
            
            filtered_filings = []
            for filing in filings_list:
                filing_date_str = filing.get('filingDate') or filing.get('reportDate')
                if not filing_date_str:
                    continue
                    
                filing_dt = datetime.strptime(filing_date_str, '%Y-%m-%d')
                # Only include filings that are newer than our latest date
                if filing_dt > latest_dt:
                    filtered_filings.append(filing)
            
            logger.info(f"📊 Incremental filtering: {len(filtered_filings)} new filings found after {latest_date}")
            return filtered_filings
            
        except Exception as e:
            logger.error(f"Error filtering incremental filings: {e}")
            return filings_list

    @staticmethod
    def _make_filing_id(accession_number: str):
        """Generate a stable ObjectId from an accession number (no DB write needed)."""
        import hashlib
        from bson import ObjectId
        return ObjectId(hashlib.md5(accession_number.encode()).hexdigest()[:24])

    def get_latest_filing_date(self, cik: str) -> Optional[str]:
        """Get the latest processed filing date for a company from the tracker collection."""
        try:
            latest = self._processed_col.find_one(
                {'cik': cik},
                sort=[('processed_at', -1)]
            )
            if latest and 'processed_at' in latest:
                return latest['processed_at'].strftime('%Y-%m-%d')
            return None
        except Exception as e:
            logger.error(f"Error getting latest filing date for CIK {cik}: {e}")
            return None
    
    def process_company(self, cik: str, summary: Optional['ProcessingSummary'] = None) -> Dict:
        company_start_time = time.time()
        
        try:
            # Determine the start date for processing
            if self.incremental:
                latest_date = self.get_latest_filing_date(cik)
                if latest_date:
                    logger.info(f"📅 INCREMENTAL MODE: Processing company CIK: {cik} from {latest_date} onwards")
                    # Use the latest date as start year
                    latest_year = datetime.strptime(latest_date, '%Y-%m-%d').year
                    effective_start_year = latest_year
                else:
                    logger.info(f"📅 INCREMENTAL MODE: No existing data found for CIK: {cik}, processing from {self.start_year}")
                    effective_start_year = self.start_year
            else:
                range_desc = f"from {self.start_year}" + (f" to {self.end_year}" if self.end_year else " onwards")
                logger.info(f"Processing company CIK: {cik} (filings {range_desc})")
                effective_start_year = self.start_year
            
            # Step 1: Fetch company information from SEC API
            filings_list = []  # Initialize to avoid unbound variable
            if self.reload:
                logger.info(f"🔄 RELOAD mode: refreshing data for CIK: {cik}")
                if self.target_fiscal_year or self.target_fiscal_quarter:
                    logger.info(f"🎯 Will reload specific period: FY{self.target_fiscal_year} {self.target_fiscal_quarter or 'complete'}")
            company_data, filings_list = self.sec_client.get_company_submissions(cik, start_year=effective_start_year, end_year=self.end_year)
            if not company_data:
                error_msg = f"Failed to fetch company data for CIK: {cik}"
                logger.error(error_msg)
                return False
            self.current_company_data = company_data

            _range_desc = f"from {self.start_year}" + (f" to {self.end_year}" if self.end_year else " onwards")
            logger.info(f"Fetched {len(filings_list)} total filings for company {company_data.get('name', 'Unknown')} {_range_desc}")
            
            # Apply fiscal year/quarter filtering if specified
            if self.target_fiscal_year or self.target_fiscal_quarter:
                filings_list = self._filter_filings_by_fiscal_period(filings_list, company_data)
                logger.info(f"After fiscal year/quarter filtering: {len(filings_list)} filings")
            
            # Apply incremental filtering if enabled
            if self.incremental:
                latest_date = self.get_latest_filing_date(cik)
                if latest_date:
                    filings_list = self.filter_incremental_filings(filings_list, cik, latest_date)
                    if not filings_list:
                        logger.info(f"✅ No new filings found for company {cik} after {latest_date}")
                        return True
            
            # Log date range of filings
            if filings_list:
                filing_dates = [f.get('filingDate', '') for f in filings_list if f.get('filingDate')]
                if filing_dates:
                    earliest_date = min(filing_dates)
                    latest_date = max(filing_dates)
                    logger.info(f"Filing date range: {earliest_date} to {latest_date}")
                    
                    # Count by form type
                    form_counts = {}
                    for filing in filings_list:
                        form_type = filing.get('form', 'Unknown')
                        form_counts[form_type] = form_counts.get(form_type, 0) + 1
                    
                    form_summary = ", ".join([f"{form}: {count}" for form, count in form_counts.items()])
                    logger.info(f"Form types found: {form_summary}")
            else:
                company_label = company_data.get('name', 'Unknown')
                _no_range = f"from {self.start_year}" + (f" to {self.end_year}" if self.end_year else " onwards")
                logger.info(f"No filings found for company {company_label} {_no_range}")
                self.progress.status(f"  ⓘ {company_label}: no filings {_no_range}")
            
            # Update company name for logging
            company_name = company_data.get('name', 'Unknown')
            
            # Step 2: Prepare company doc for in-memory passing to normalization service
            company_doc = self.company_transformer.transform_company_data(company_data)
            self.current_company_doc = company_doc if isinstance(company_doc, dict) else {}
            logger.info(f"Company data ready for CIK: {cik}")
            
            # Step 3: Process filings (10-K and 10-Q) with existence checks and reload logic
            filings_processed = 0
            filings_skipped = 0
            filings_failed = 0
            target_forms = ['10-K', '10-Q']
            
            # Filter filings to only target forms for progress bar
            target_filings = [f for f in filings_list if f.get('form') in target_forms]
            
            # Get ticker for logging
            ticker = self.sec_client.get_ticker_from_cik(cik)
            if not ticker:
                ticker = f"CIK_{cik}"
            # Prefer the ticker symbol the user typed (a CIK may map to several)
            if self.progress is not None:
                ticker = self.progress.label(cik, ticker)
            
            # Track if we've logged the section header for this session
            session_header_logged = False
            
            # Create per-company filing progress bar
            filing_iterator = target_filings
            # Update outer bar immediately with the real ticker
            self.progress.set_current_company(ticker)
            filing_bar = self.progress.filing_bar(ticker, len(target_filings))
            for i, filing in enumerate(filing_iterator, 1):
                accession_number = filing.get('accessionNumber', '')
                form_type = filing.get('form', '')
                filing_bar.set_description_str(f"{ticker:<6} {form_type} {accession_number[-9:] if accession_number else ''}")
                filing_bar.update(1)
                
                # Log to summary file
                if summary and accession_number:
                    summary.log_filing_progress(ticker, i, len(target_filings), accession_number, "processing")
                filing_date = filing.get('filingDate')
                acceptance_datetime = filing.get('acceptanceDateTime')
                
                # Skip if accession number is missing
                if not accession_number:
                    logger.warning(f"Skipping filing with missing accession number: {filing}")
                    continue
                
                # Check if filing already processed (via lightweight tracker)
                existing_filing = self._processed_col.find_one({'_id': accession_number})

                if existing_filing and not self.reload:
                    should_process = True
                    if self.target_fiscal_year or self.target_fiscal_quarter:
                        should_process = self._should_process_filing_for_period(filing, company_data)

                    if should_process:
                        logger.debug(f"📋 Filing {accession_number} already processed, skipping")
                        if self.html_download_path:
                            self.sec_client.download_html_filing(cik, accession_number, filing_date or '2010-01-01', self.html_download_path, ticker)
                        filings_skipped += 1
                        if summary:
                            summary.log_filing_progress(ticker, i, len(target_filings), accession_number, "skipped")
                        continue
                    else:
                        logger.debug(f"📋 Filing {accession_number} exists but outside target period, skipping")
                        filings_skipped += 1
                        if summary:
                            summary.log_filing_progress(ticker, i, len(target_filings), accession_number, "skipped")
                        continue
                elif existing_filing and self.reload:
                    should_reload = True
                    if self.target_fiscal_year or self.target_fiscal_quarter:
                        should_reload = self._should_process_filing_for_period(filing, company_data)

                    if should_reload:
                        logger.info(f"🔄 RELOAD: Removing tracker entry for {accession_number}")
                        self._processed_col.delete_one({'_id': accession_number})
                    else:
                        logger.debug(f"📋 Filing {accession_number} outside reload period, skipping")
                        filings_skipped += 1
                        continue
                
                # Process individual filing with full SEC API data
                processing_result = self.process_filing_with_full_data(cik, filing, is_local=False)
                
                if processing_result:
                    filings_processed += 1
                    # Log to summary
                    if summary:
                        summary.log_filing_progress(ticker, i, len(target_filings), accession_number, "processed")
                else:
                    # Log section header on first failure (disabled to avoid clutter)
                    # if not session_header_logged:
                    #     self.failure_logger.log_section_header(ticker, cik, f"Processing Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    #     session_header_logged = True
                    
                    filings_failed += 1
                    # Log to summary
                    if summary:
                        summary.log_filing_progress(ticker, i, len(target_filings), accession_number, "failed")
                
                # Rate limiting to respect SEC API guidelines
                time.sleep(0.1)
            
            self.progress.close_filing_bar()

            # Step 4: Run quarterly deaccumulation for this company (uses in-memory accumulator)
            self._flush_quarterly_accumulator(cik)

            # Step 4b: Fill genuine extraction gaps from SEC companyfacts (edge mode)
            self._run_companyfacts_reconciliation(cik)

            # Step 5: Log processing results
            logger.info(f"Processed {filings_processed} filings, skipped {filings_skipped} existing filings for company CIK: {cik}")
            return {
                'cik': cik,
                'ticker': ticker,
                'total_filings': len(target_filings),
                'processed': filings_processed,
                'skipped': filings_skipped,
                'failed': filings_failed,
                'success': True
            }
            
        except Exception as e:
            error_msg = f"Error processing company CIK {cik}: {e}"
            logger.error(error_msg)
            return {
                'cik': cik,
                'ticker': 'UNKNOWN',
                'total_filings': 0,
                'processed': 0,
                'skipped': 0,
                'failed': 0,
                'success': False,
                'error': str(e)
            }
    
    def process_single_filing_from_url(self, cik: str, accession_number: str) -> bool:
        """
        Process a single filing given its CIK and accession number
        
        Args:
            cik: Company CIK identifier
            accession_number: Filing accession number
            
        Returns:
            bool: True if processing successful, False otherwise
        """
        try:
            logger.info(f"Processing single filing: CIK {cik}, Accession {accession_number}")
            
            # Fetch company data from SEC API
            company_data, filings_list = self.sec_client.get_company_submissions(cik)
            if not company_data:
                logger.error(f"Failed to fetch company data for CIK: {cik}")
                return False
            
            # Store company data for use in financial processing
            self.current_company_data = company_data
            
            # Transform company doc for in-memory passing to normalization service
            company_doc = self.company_transformer.transform_company_data(company_data)
            self.current_company_doc = company_doc if isinstance(company_doc, dict) else {}
            logger.info(f"Company data ready for CIK: {cik}")
            
            # Find the specific filing in the filings list
            target_filing = None
            for filing in filings_list:
                if filing.get('accessionNumber') == accession_number:
                    target_filing = filing
                    break
            
            if not target_filing:
                # If not found in filings list, create a minimal filing info dict
                logger.warning(f"Filing {accession_number} not found in company's filing list, creating minimal filing info")
                target_filing = {
                    'accessionNumber': accession_number,
                    'form': 'Unknown',  # Will be determined from XBRL if possible
                    'filingDate': datetime.now().strftime('%Y-%m-%d'),
                    'reportDate': None,
                    'company_cik': cik
                }
            
            # Get ticker for logging
            ticker = self.sec_client.get_ticker_from_cik(cik) or f"CIK_{cik}"
            company_name = company_data.get('name', 'Unknown')
            
            logger.info(f"Processing filing {accession_number} for {company_name} ({ticker})")
            
            # Process the filing
            success = self.process_filing_with_full_data(cik, target_filing, is_local=False)

            if success:
                logger.info(f"✅ Successfully processed filing {accession_number}")
            else:
                logger.error(f"❌ Failed to process filing {accession_number}")

            # Deaccumulate quarterly deltas and fill extraction gaps, same as
            # the multi-filing path.
            self._flush_quarterly_accumulator(cik)
            self._run_companyfacts_reconciliation(cik)

            return success
            
        except Exception as e:
            logger.error(f"Error processing filing {accession_number} for CIK {cik}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _filter_filings_by_fiscal_period(self, filings_list: List[Dict], company_data: Dict) -> List[Dict]:
        """Filter filings by target fiscal year and quarter"""
        if not self.target_fiscal_year:
            return filings_list
        
        from utilities.helpers.period_utils import FiscalYearCalculator
        
        # Check if these are local filings (they won't have reportDate)
        has_report_dates = any(filing.get('reportDate') for filing in filings_list)
        
        if not has_report_dates:
            # These are local filings - period information is not readily available
            logger.warning("⚠️  Fiscal year/quarter filtering is not available for local XBRL files")
            logger.warning("💡 Period information must be extracted from XBRL content, which is not implemented")
            logger.warning("💡 Consider using online mode (without --local) for fiscal period filtering")
            logger.warning(f"🔄 Processing all {len(filings_list)} local filings instead")
            return filings_list
        
        fiscal_year_end_code = company_data.get('fiscalYearEnd')
        if not fiscal_year_end_code:
            logger.warning("No fiscal year end found for company, cannot filter by fiscal period")
            return filings_list
        
        filtered_filings = []
        quarter_map = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4}
        target_quarter_num = quarter_map.get(self.target_fiscal_quarter) if self.target_fiscal_quarter else None
        
        for filing in filings_list:
            report_date = filing.get('reportDate')
            form_type = filing.get('form')
            
            if not report_date:
                continue
                
            try:
                report_end_date = datetime.strptime(report_date, '%Y-%m-%d')
                fiscal_year, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                    report_end_date, fiscal_year_end_code
                )
                
                # Check if this filing matches our target criteria
                if fiscal_year == self.target_fiscal_year:
                    # If specific quarter is requested, check quarter match
                    if self.target_fiscal_quarter:
                        # For 10-K (annual), include if target quarter is Q4
                        if form_type == '10-K' and self.target_fiscal_quarter == 'Q4':
                            filtered_filings.append(filing)
                        # For 10-Q (quarterly), check exact quarter match
                        elif form_type == '10-Q' and quarter == target_quarter_num:
                            filtered_filings.append(filing)
                    else:
                        # No specific quarter requested, include all filings for the fiscal year
                        filtered_filings.append(filing)
                        
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not calculate fiscal year for filing {filing.get('accessionNumber', 'unknown')}: {e}")
                continue
        
        if self.target_fiscal_quarter:
            logger.info(f"Filtered to FY {self.target_fiscal_year} {self.target_fiscal_quarter}: {len(filtered_filings)} filings")
        else:
            logger.info(f"Filtered to FY {self.target_fiscal_year}: {len(filtered_filings)} filings")
            
        return filtered_filings

    def _should_process_filing_for_period(self, filing: Dict, company_data: Dict) -> bool:
        """Check if a filing should be processed based on target fiscal year/quarter"""
        if not self.target_fiscal_year:
            return True  # No period filter, process all
        
        from utilities.helpers.period_utils import FiscalYearCalculator
        
        report_date = filing.get('reportDate')
        form_type = filing.get('form')
        
        if not report_date:
            # No report date available, include it to be safe
            return True
        
        fiscal_year_end_code = company_data.get('fiscalYearEnd')
        if not fiscal_year_end_code:
            # No fiscal year end info, include it to be safe
            return True
        
        try:
            report_end_date = datetime.strptime(report_date, '%Y-%m-%d')
            fiscal_year, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                report_end_date, fiscal_year_end_code
            )
            
            # Check if this filing matches our target criteria
            if fiscal_year == self.target_fiscal_year:
                # If specific quarter is requested, check quarter match
                if self.target_fiscal_quarter:
                    quarter_map = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4}
                    target_quarter_num = quarter_map.get(self.target_fiscal_quarter)
                    
                    # For 10-K (annual), include if target quarter is Q4
                    if form_type == '10-K' and self.target_fiscal_quarter == 'Q4':
                        return True
                    # For 10-Q (quarterly), check exact quarter match
                    elif form_type == '10-Q' and quarter == target_quarter_num:
                        return True
                    else:
                        return False
                else:
                    # No specific quarter requested, include all filings for the fiscal year
                    return True
            else:
                return False
                
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not calculate fiscal year for filing {filing.get('accessionNumber', 'unknown')}: {e}")
            return True  # Include it to be safe

    def process_company_local(self, ticker: str, filing_limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Process a company's local XBRL files using the same pipeline as online processing
        
        Args:
            ticker: Company ticker symbol (e.g., 'AAPL')
            filing_limit: Maximum number of filings to process (None for all)
            
        Returns:
            Processing results summary
        """
        company_start_time = time.time()
        
        # Get CIK from ticker
        cik = self.financial_processor.ticker_to_cik.get(ticker)
        if not cik:
            return {'error': f'No CIK found for ticker {ticker}'}
        
        try:
            logger.info(f"Processing company {ticker} (CIK: {cik}) from local XBRL files")
            
            # Get company data from SEC API for fiscal year end and other metadata
            try:
                company_data, _ = self.sec_client.get_company_submissions(cik)
                if not company_data:
                    logger.warning(f"Could not fetch company data from SEC API for {ticker} (CIK: {cik}), using fallback")
                    company_data = {
                        'cik': cik,
                        'ticker': ticker,
                        'name': f'{ticker} Company',
                        'sic': None,
                        'sicDescription': None,
                        'entityType': None,
                        'category': None,
                        'fiscalYearEnd': '1231'  # Default fallback
                    }
                else:
                    logger.info(f"✅ Fetched company metadata from SEC API: fiscal year end = {company_data.get('fiscalYearEnd', 'N/A')}")
            except Exception as e:
                logger.warning(f"Failed to fetch company data from SEC API for {ticker}: {e}, using fallback")
                company_data = {
                    'cik': cik,
                    'ticker': ticker,
                    'name': f'{ticker} Company',
                    'sic': None,
                    'sicDescription': None,
                    'entityType': None,
                    'category': None,
                    'fiscalYearEnd': '1231'  # Default fallback
                }
            
            self.current_company_data = company_data
            if self.reload:
                logger.info(f"🔄 RELOAD mode: refreshing data for {ticker} (CIK: {cik})")
            company_doc = self.company_transformer.transform_company_data(company_data)
            self.current_company_doc = company_doc if isinstance(company_doc, dict) else {}
            logger.info(f"Company data ready for {ticker} (CIK: {cik})")
            
            # Get local filing list
            local_filings = self._get_local_filing_list(ticker, filing_limit)
            if not local_filings:
                error_msg = f"No local XBRL files found for {ticker}"
                return {'error': error_msg}
            
            logger.info(f"Found {len(local_filings)} local filings for {ticker}")
            
            # Apply fiscal year/quarter filtering if specified
            if self.target_fiscal_year or self.target_fiscal_quarter:
                local_filings = self._filter_filings_by_fiscal_period(local_filings, company_data)
                logger.info(f"After fiscal year/quarter filtering: {len(local_filings)} local filings")
            
            # Process filings with existence checks and reload logic
            filings_processed = 0
            filings_skipped = 0
            filings_failed = 0
            
            # Track if we've logged the section header for this session
            session_header_logged = False
            
            for filing_info in local_filings:
                accession_number = filing_info.get('accessionNumber')
                
                # Skip if accession number is missing
                if not accession_number:
                    logger.warning(f"Skipping local filing with missing accession number: {filing_info}")
                    continue
                
                # Check if filing already processed (via lightweight tracker)
                existing_filing = self._processed_col.find_one({'_id': accession_number})

                if existing_filing and not self.reload:
                    should_process = True
                    if self.target_fiscal_year or self.target_fiscal_quarter:
                        should_process = self._should_process_filing_for_period(filing_info, company_data)
                    if should_process:
                        logger.debug(f"📋 Local filing {accession_number} already processed, skipping")
                        filings_skipped += 1
                        continue
                    else:
                        logger.debug(f"📋 Local filing {accession_number} outside target period, skipping")
                        filings_skipped += 1
                        continue
                elif existing_filing and self.reload:
                    should_reload = True
                    if self.target_fiscal_year or self.target_fiscal_quarter:
                        should_reload = self._should_process_filing_for_period(filing_info, company_data)
                    if should_reload:
                        logger.info(f"🔄 RELOAD: Removing tracker entry for local filing {accession_number}")
                        self._processed_col.delete_one({'_id': accession_number})
                    else:
                        logger.debug(f"📋 Local filing {accession_number} outside reload period, skipping")
                        filings_skipped += 1
                        continue
                
                # Process each filing
                processing_result = self.process_filing_with_full_data(cik, filing_info, is_local=True)
                
                if processing_result:
                    filings_processed += 1
                else:
                    # Log section header on first failure (disabled to avoid clutter)
                    # if not session_header_logged:
                    #     self.failure_logger.log_section_header(ticker, cik, f"Local Processing Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    #     session_header_logged = True
                    pass
                    
                    filings_failed += 1
            
            # Log processing summary for this stock (disabled to avoid clutter)
            # if local_filings and filings_failed > 0:
            #     self.failure_logger.log_processing_summary(
            #         ticker, cik,
            #         total_filings=len(local_filings),
            #         successful=filings_processed,
            #         failed=filings_failed,
            #         skipped=filings_skipped
            #     )
            
            # Calculate processing stats
            # Run quarterly deaccumulation for this company (uses in-memory accumulator)
            self._flush_quarterly_accumulator(cik)

            # Fill genuine extraction gaps from SEC companyfacts (edge mode)
            self._run_companyfacts_reconciliation(cik)

            processing_time = time.time() - company_start_time
            
            logger.info(f"Processed {filings_processed} filings, skipped {filings_skipped} existing filings for company {ticker} (CIK: {cik})")
            return {
                'ticker': ticker,
                'cik': cik,
                'total_filings': len(local_filings),
                'processed_successfully': filings_processed,
                'skipped': filings_skipped,
                'failed': filings_failed,
                'results': [],
                'processing_time': processing_time
            }
            
        except Exception as e:
            error_msg = f"Error processing company {ticker} (CIK: {cik}): {e}"
            logger.error(error_msg)
            return {'error': error_msg}
    
    def _get_local_filing_list(self, ticker: str, filing_limit: Optional[int] = None) -> List[Dict]:
        """Get list of local XBRL filings in SEC API format for uniform processing"""
        from pathlib import Path
        
        local_xbrl_directory = self.xbrl_zip_cache_path
        company_dir = Path(local_xbrl_directory) / ticker
        
        if not company_dir.exists():
            return []
        
        # Find all zip files and convert to SEC API format
        local_filings = []
        for form_dir in company_dir.iterdir():
            if form_dir.is_dir() and form_dir.name in ['10-K', '10-Q']:
                for zip_file in form_dir.glob('*.zip'):
                    # Convert to SEC API format for uniform processing
                    filing_info = {
                        'accessionNumber': zip_file.stem,
                        'form': form_dir.name,
                        'filingDate': '2023-01-01',  # Default for local files
                        'acceptanceDateTime': '2023-01-01T00:00:00.000Z',
                        'zip_file_path': str(zip_file),  # Local-specific field
                        'ticker': ticker  # Local-specific field
                    }
                    local_filings.append(filing_info)
        
        # Sort by accession number (newest first) and apply limit
        local_filings.sort(key=lambda x: x['accessionNumber'], reverse=True)
        if filing_limit:
            local_filings = local_filings[:filing_limit]
        
        return local_filings
    
    @safe_processing_operation("filing_processing", default_return=False)
    def process_filing_with_full_data(self, cik: str, filing_info: Dict, is_local: bool = False) -> bool:
        """
        Process a filing with full data (unified method for both online and local)
        
        Args:
            cik: Company CIK identifier
            filing_info: Complete filing information
            is_local: Whether this is a local filing or online SEC filing
            
        Returns:
            bool: True if processing successful, False otherwise
        """
        # Validate required fields
        required_fields = ['accessionNumber', 'form']
        if not validate_required_fields(filing_info, required_fields, f"process_filing_with_full_data ({'local' if is_local else 'online'})"):
            return False
        
        accession_number: str = filing_info['accessionNumber']  # Type assertion after validation
        form_type: str = filing_info['form']  # Type assertion after validation
        filing_type = "local" if is_local else "online"
        
        # Get ticker for failure logging (always ensure it's a string)
        ticker = self.sec_client.get_ticker_from_cik(cik) or f"CIK_{cik}"
        
        # Extract period information for logging
        filing_date = filing_info.get('filingDate')
        report_period = filing_info.get('reportDate')
        
        # Calculate fiscal year and quarter if possible
        fiscal_year = None
        fiscal_quarter = None
        if hasattr(self, 'current_company_data') and self.current_company_data:
            try:
                from utilities.helpers.period_utils import FiscalYearCalculator
                fiscal_year_end = self.current_company_data.get('fiscalYearEnd', '1231')
                if report_period:
                    report_end_date = datetime.strptime(report_period, '%Y-%m-%d')
                    fiscal_year, quarter_num = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                        report_end_date, fiscal_year_end
                    )
                    fiscal_quarter = f"Q{quarter_num}" if quarter_num else None
            except Exception as e:
                logger.debug(f"Could not calculate fiscal period: {e}")
        
        logger.info(f"Processing {filing_type} filing {accession_number} for CIK: {cik}")
        
        # Build SEC URL for online filings
        sec_url = None if is_local else self._build_sec_url(cik, accession_number)
        
        # Download HTML filing if requested and not processing local files
        if self.html_download_path and not is_local:
            filing_date_for_download = filing_info.get('filingDate', filing_info.get('reportDate', '2010-01-01'))
            self.sec_client.download_html_filing(cik, accession_number, filing_date_for_download, self.html_download_path, ticker)

        # Save XBRL zip to local replay cache (idempotent; skipped for already-local files)
        if not is_local:
            self._save_xbrl_zip_to_cache(ticker, form_type, accession_number, cik)
        
        # Generate a stable filing_id from the accession number (no DB write)
        filing_id = self._make_filing_id(accession_number)
        
        # Process financial statements (unified method)
        statements_processed = self.process_financial_statements_enhanced(cik, accession_number, filing_id, filing_info, is_local)
        
        url_info = f" - URL: {sec_url}" if sec_url else ""
        
        # Determine if processing was successful
        if statements_processed > 0:
            log_operation_result(
                f"{filing_type.capitalize()} filing {accession_number}{url_info}", 
                True, 
                f"Statements: {statements_processed}"
            )
            return True
        else:
            failure_reason = "No financial statements extracted from XBRL data (parsing failed or no recognized statement types found)"
            
            log_operation_result(
                f"{filing_type.capitalize()} filing {accession_number}{url_info}", 
                False, 
                failure_reason
            )
            # Log failure (disabled to avoid clutter)
            try:
                self.failure_logger.log_filing_failure(
                    ticker, cik, accession_number, form_type,
                    failure_reason=failure_reason,
                    filing_date=filing_date,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    report_period=report_period,
                    additional_info={"url": sec_url} if sec_url else None
                )
            except AttributeError:
                pass  # failure_logger disabled
            return False
    
    def process_financial_statements_enhanced(self, cik: str, accession_number: str, filing_id, filing_info: Dict, is_local: bool = False) -> int:
        """
        Process financial statements for a filing (unified method for online and local)
        
        Args:
            cik: Company CIK identifier
            accession_number: SEC accession number
            filing_id: MongoDB ObjectId of the filing document
            filing_info: Complete filing information
            is_local: Whether this is a local filing or online SEC filing
            
        Returns:
            int: Number of statements processed (0 if failed)
        """
        filing_type = "local" if is_local else "online"
        
        try:
            # Enrich company info with fiscal year information for this specific filing
            company_info_enriched = getattr(self, 'current_company_data', None) or {}
            
            # Calculate fiscal year and quarter for this filing to pass to XBRL parser
            if company_info_enriched and filing_info:
                fiscal_year_end_code = company_info_enriched.get('fiscalYearEnd')
                if fiscal_year_end_code and 'reportDate' in filing_info:
                    try:
                        from utilities.helpers.period_utils import FiscalYearCalculator
                        report_end_date = filing_info['reportDate']
                        if isinstance(report_end_date, str):
                            report_end_date = datetime.strptime(report_end_date, '%Y-%m-%d')
                        
                        fiscal_year, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                            report_end_date, fiscal_year_end_code
                        )
                        
                        # Add to company info dict for XBRL parser to use in period validation
                        company_info_enriched = company_info_enriched.copy()
                        company_info_enriched['fiscal_year'] = fiscal_year
                        company_info_enriched['fiscal_year_end_code'] = fiscal_year_end_code
                        company_info_enriched['quarter'] = quarter
                    except Exception as e:
                        logger.debug(f"Could not enrich company info with fiscal year for {accession_number}: {e}")
            
            # Process using unified financial processor
            statements_data = self.financial_processor.process_filing(
                filing_info, cik, company_info_enriched, is_local=is_local
            )
            
            if not statements_data:
                logger.warning(f"Financial processor returned None for {filing_type} filing {accession_number}")
                return 0
            
            if 'statements' not in statements_data:
                logger.warning(f"No 'statements' key in processed data for {filing_type} filing {accession_number}")
                return 0
            
            # Process statements using shared logic
            statements_processed = 0
            statements_with_data = 0
            statements = statements_data['statements']
            reporting_period = statements_data.get('reporting_period', {})
            
            for statement_type, statement_data in statements.items():
                if statement_data:
                    # Extract line items from the statement hierarchy structure
                    # statement_data is a dict with {'hierarchy': [root_item], ...}
                    # We need to flatten the tree to a list for validation
                    logger.info(f"📊 Processing {statement_type}...")
                    logger.debug(f"   statement_data type: {type(statement_data)}")
                    if isinstance(statement_data, dict):
                        logger.debug(f"   statement_data keys: {list(statement_data.keys())}")
                    elif isinstance(statement_data, list):
                        logger.debug(f"   statement_data length: {len(statement_data)}")
                        if statement_data:
                            logger.debug(f"   First item type: {type(statement_data[0])}")
                            if isinstance(statement_data[0], dict):
                                logger.debug(f"   First item keys: {list(statement_data[0].keys())}")
                    
                    line_items = self._extract_line_items_from_hierarchy(statement_data)
                    logger.info(f"   Extracted {len(line_items)} line items")
                    
                    # Validate that the statement has actual data (not just abstract items)
                    has_actual_data = self._validate_statement_has_data(line_items)
                    logger.info(f"   Validation result: {has_actual_data}")
                    
                    if not has_actual_data:
                        logger.warning(f"⚠️  {statement_type} has no actual data values (only abstract/structural items), skipping")
                        continue
                    
                    # Content quality check for income_statement: warn if no revenue/profit concepts found.
                    # This indicates a classification error (e.g., CI statement used as income_statement).
                    if statement_type == 'income_statement':
                        _revenue_markers = {
                            'Revenues', 'SalesRevenueNet', 'SalesRevenueGoodsNet',
                            'RevenueFromContractWithCustomer', 'GrossProfit',
                            'OperatingIncomeLoss', 'CostOfRevenue', 'CostOfGoodsAndServicesSold',
                        }
                        concepts_in_stmt = {item.get('concept', '') for item in line_items if item.get('value') is not None}
                        has_revenue_concept = any(
                            any(marker in c for marker in _revenue_markers)
                            for c in concepts_in_stmt
                        )
                        if not has_revenue_concept and len(line_items) < 20:
                            logger.warning(
                                f"⚠️  income_statement for {cik} ({accession_number}) has {len(line_items)} items "
                                f"but no revenue/gross-profit/operating-income concepts — likely a misclassified "
                                f"comprehensive-income statement. Consider re-running with --reload after updating "
                                f"the XBRL classifier."
                            )
                    
                    # Extract primary period from statement data for enhanced transformations
                    primary_period_string = self._extract_primary_period_string(statements_data, line_items)
                    logger.debug(f"   Primary period: {primary_period_string}")
                    
                    # Transform financial statement data - pass the line items list instead of the raw statement_data dict
                    logger.debug(f"   Transforming {statement_type}...")
                    statement_doc = self.financial_transformer.transform_statement_data(
                        line_items, filing_id, cik, statement_type, reporting_period, primary_period_string
                    )
                    logger.debug(f"   Transformation complete")
                    
                    # ----------------------------------------------------------
                    # Merged pipeline: normalize directly into normalize_data
                    # ----------------------------------------------------------
                    logger.debug(f"   Normalizing {statement_type} into normalize_data...")
                    company_doc = getattr(self, 'current_company_doc', {}) or {}
                    filing_doc_for_norm = {
                        '_id': filing_id,
                        'form_type': filing_info.get('form', filing_info.get('form_type', 'UNKNOWN')),
                        'accession_number': accession_number,
                    }
                    try:
                        self._norm_service.normalize_statement_in_memory(
                            statement_doc, filing_doc_for_norm, company_doc
                        )
                        statements_processed += 1
                        statements_with_data += 1
                        logger.info(f"✅ Normalized {statement_type} statement into normalize_data")
                        # Record this accession as processed (upsert — idempotent)
                        self._processed_col.update_one(
                            {'_id': accession_number},
                            {'$set': {'cik': cik, 'form_type': filing_doc_for_norm['form_type'],
                                      'processed_at': datetime.now()}},
                            upsert=True
                        )
                        # Accumulate slim copy for quarterly deaccumulation pass
                        self._accumulate_for_quarterly(cik, statement_doc, filing_doc_for_norm)
                        # Log period information
                        self._log_period_information(statement_type, reporting_period, is_local)
                    except Exception as norm_err:
                        logger.warning(f"⚠️  Failed to normalize {statement_type}: {norm_err}", exc_info=True)
            
            return statements_with_data
            
        except Exception as e:
            filing_type = "local" if is_local else "online"
            # Get full traceback for debugging
            import traceback
            traceback_str = traceback.format_exc()
            logger.error(f"Error processing financial statements for {filing_type} filing {accession_number}: {e}\n{traceback_str}")
            return 0

    def _extract_primary_period_string(self, statements_data: Dict, statement_data: List) -> Optional[str]:
        """Extract primary period string from statement data"""
        primary_period_string = None
        arelle_data = statements_data.get('arelle_data', {})
        
        if arelle_data and 'filing_info' in arelle_data:
            primary_period_info = arelle_data['filing_info'].get('primary_period_info', {})
            primary_period_string = (
                primary_period_info.get('duration_period') or 
                primary_period_info.get('instant_period')
            )
        
        # If no period found in arelle_data, try to extract from first statement item
        if not primary_period_string and statement_data:
            for item in statement_data:
                if item.get('period'):
                    primary_period_string = item['period']
                    break
        
        return primary_period_string

    def _log_period_information(self, statement_type: str, reporting_period: Dict, is_local: bool = False):
        """Log period information for processed statements"""
        log_message = format_filing_log_message(statement_type, reporting_period, is_local)
        logger.info(log_message)
    
    def _extract_line_items_from_hierarchy(self, statement_data) -> List[Dict]:
        """
        Extract line items from the statement hierarchy structure.
        
        The extractor can return statements in two formats:
        1. Dict with hierarchy: {'role_uri': '...', 'hierarchy': [root_FinancialLineItem], ...}
        2. List of items directly: [item1, item2, ...]
        
        We need to flatten the tree to a list of dict items.
        
        Args:
            statement_data: Statement dict with hierarchy structure OR list of items
            
        Returns:
            List of line item dicts
        """
        from core.extractors.xbrl_parser import FinancialLineItem
        
        line_items = []
        
        # Handle case where statement_data is already a list
        if isinstance(statement_data, list):
            # Statement data is already in list format
            for item in statement_data:
                if isinstance(item, dict):
                    line_items.append(item)
                elif isinstance(item, FinancialLineItem):
                    item_dict = {
                        'label': item.label,
                        'value': item.value,
                        'abstract': item.abstract,
                        'concept_name': item.concept_name,
                        'period': item.period,
                        'dimensional_facts': item.all_dimensional_facts
                    }
                    line_items.append(item_dict)
            return line_items
        
        # Handle case where statement_data is a dict with hierarchy
        if not isinstance(statement_data, dict):
            return line_items
            
        # Get the hierarchy list
        hierarchy = statement_data.get('hierarchy', [])
        if not hierarchy:
            return line_items
        
        # The hierarchy contains FinancialLineItem objects
        # Recursively flatten the tree
        def flatten_item(item):
            # Convert FinancialLineItem to dict if needed
            if isinstance(item, FinancialLineItem):
                item_dict = {
                    'label': item.label,
                    'value': item.value,
                    'abstract': item.abstract,
                    'concept_name': item.concept_name,
                    'period': item.period,
                    'dimensional_facts': item.all_dimensional_facts
                }
                line_items.append(item_dict)
                
                # Recursively process children
                if hasattr(item, 'children') and item.children:
                    for child in item.children:
                        flatten_item(child)
            elif isinstance(item, dict):
                line_items.append(item)
        
        # Flatten all items in hierarchy
        for item in hierarchy:
            flatten_item(item)
        
        return line_items
    
    def _validate_statement_has_data(self, statement_data: List[Dict]) -> bool:
        """
        Validate that a statement has actual data values, not just abstract/structural items
        
        Args:
            statement_data: List of statement line items
            
        Returns:
            bool: True if statement has actual data, False if only abstract items
        """
        if not statement_data:
            logger.debug("Validation: Empty statement_data")
            return False
        
        logger.debug(f"Validation: Checking {len(statement_data)} items")
        logger.debug(f"Validation: First item keys: {list(statement_data[0].keys()) if statement_data else 'N/A'}")
        
        # Check if there are any items with actual values (not None and not abstract)
        has_values = False
        items_checked = 0
        for item in statement_data:
            items_checked += 1
            # Skip abstract items
            if item.get('abstract', False):
                continue
            
            # Check if item has a non-null value
            value = item.get('value')
            if value is not None:
                has_values = True
                logger.debug(f"Validation: Found value in item {items_checked}: {item.get('label', 'N/A')}")
                break
        
        # Also check if we have any dimensional facts with values
        if not has_values:
            logger.debug("Validation: No direct values found, checking dimensional facts...")
            for item in statement_data:
                dimensional_facts = item.get('dimensional_facts', [])
                if dimensional_facts:
                    for fact in dimensional_facts:
                        if fact.get('value') is not None:
                            has_values = True
                            logger.debug(f"Validation: Found value in dimensional fact")
                            break
                if has_values:
                    break
        
        logger.debug(f"Validation result: {has_values}")
        return has_values
    
    def _determine_filing_failure_reason(self, cik: str, accession_number: str, filing_id) -> str:
        """
        Determine the specific reason why a filing failed to extract data
        
        Args:
            cik: Company CIK
            accession_number: Filing accession number
            filing_id: MongoDB filing ID
            
        Returns:
            str: Detailed failure reason
        """
        # In the merged pipeline, financial_statements are not stored — failure reason is always XBRL parse failure
        return "No financial statements extracted from XBRL data (parsing failed or no recognized statement types found)"
        items_with_values = 0  # unreachable; kept to preserve method signature
        
        if total_items == 0:
            return "Statements extracted but contain no line items (empty statements)"
        
        if items_with_values == 0:
            if statements_with_only_abstracts > 0:
                return f"Statements contain only abstract/structural items with no actual data values ({statements_with_only_abstracts} abstract items, 0 data values)"
            else:
                return "Statements extracted but all line items have null values (no data)"
        
        # This shouldn't happen but just in case
        return f"Unknown extraction issue (extracted {len(saved_statements)} statements with {items_with_values}/{total_items} items with values)"
    
    def update_company_statistics(self, cik: str):
        """Update company statistics based on stored data"""
        try:
            logger.info(f"Statistics update requested for CIK: {cik}")
        except Exception as e:
            logger.error(f"Error updating company statistics for CIK {cik}: {e}")
    
    def process_multiple_companies(self, ciks: List[str], resume: bool = True) -> Dict[str, bool]:
        """
        Process multiple companies
        
        Args:
            ciks: List of company CIK identifiers
            resume: Whether to resume from previous progress (deprecated - no longer used)
            
        Returns:
            dict: Results for each CIK
        """
        results = {}
        
        logger.info(f"Processing {len(ciks)} companies: {ciks}")
        self.progress.status(f"\nProcessing {len(ciks)} companies...\n")
        self.progress.start_companies(len(ciks))

        for i, cik in enumerate(ciks, 1):
            try:
                if self.progress.verbose:
                    print(f"[{i}/{len(ciks)}] Processing company CIK: {cik}")

                logger.info(f"Starting processing for CIK: {cik}")
                # Show which company is active in the outer bar immediately.
                # Prefer the user-typed ticker, else the reverse CIK->ticker
                # lookup, else the raw CIK.
                display = self.progress.label(
                    cik, self.sec_client.get_ticker_from_cik(cik) or cik)
                self.progress.set_current_company(display, i)
                stats = self.process_company(cik, summary=None)

                ticker = (stats.get('ticker') if isinstance(stats, dict) else None) or display
                if isinstance(stats, dict):
                    results[cik] = stats.get('success', False)
                    self.progress.advance_company(
                        ticker,
                        stats.get('processed', 0),
                        stats.get('skipped', 0),
                        stats.get('failed', 0),
                    )
                    if not results[cik]:
                        self.progress.error(f"  ⚠️  {ticker} ({cik}) completed with errors")
                else:
                    results[cik] = stats
                    self.progress.advance_company(ticker, 0, 0, 0)
                    if not stats:
                        self.progress.error(f"  ⚠️  CIK {cik} completed with errors")
                
                # Rate limiting between companies
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Failed to process CIK {cik}: {e}")
                self.progress.error(f"  ❌ CIK {cik} failed: {e}")
                self.progress.advance_company(cik, 0, 0, 1)
                results[cik] = False

        self.progress.finish_companies()
        self.progress.status("\n✅ Processing complete")
        
        return results
    
    def get_company_summary(self, cik: str) -> Dict[str, Any]:
        """Get comprehensive summary for a company"""
        try:
            processed = list(self._processed_col.find({'cik': cik}).sort('processed_at', -1).limit(10))
            return {
                'cik': cik,
                'processed_filings': len(processed),
                'recent_accessions': [p['_id'] for p in processed],
                'last_updated': processed[0]['processed_at'] if processed else None,
            }
        except Exception as e:
            logger.error(f"Error getting company summary for CIK {cik}: {e}")
            return {}
    
    def cleanup(self):
        """Clean up resources"""
        try:
            self.db_config.disconnect()
            logger.info("Application cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


def download_files_only_mode(companies: List[str], args) -> Dict[str, bool]:
    """
    HTML-download-only mode: Downloads HTML files for companies that already 
    have XBRL data and accession numbers in the database.
    
    Args:
        companies: List of company CIKs to process
        args: Command line arguments
    
    Returns:
        Dict mapping CIK to success status
    """
    print(f"📁 Processing {len(companies)} companies in HTML-download-only mode...")
    
    # Initialize database connection
    database_config = DatabaseConfig()
    db = database_config.get_database()
    if db is None:
        print("❌ Failed to connect to database")
        return {cik: False for cik in companies}
    
    sec_client = SECAPIClient(database=database_config.get_database())
    
    # Setup HTML download path (required for this mode)
    html_download_path = os.getenv('SEC_HTML_DOWNLOAD_PATH')
    if not html_download_path:
        print("❌ SEC_HTML_DOWNLOAD_PATH environment variable not set")
        return {cik: False for cik in companies}
    
    print(f"📂 HTML files will be saved to: {html_download_path}")
    
    results = {}
    
    for i, cik in enumerate(companies, 1):
        print(f"\n📂 Processing company {i}/{len(companies)}: CIK {cik}")
        
        try:
            # Fetch company data from SEC API
            company_data_dl, existing_filings = sec_client.get_company_submissions(cik)
            if not company_data_dl:
                print(f"❌ Could not fetch data for CIK: {cik} from SEC API")
                results[cik] = False
                continue
            if not existing_filings:
                print(f"❌ No filings found for CIK: {cik}")
                results[cik] = False
                continue
            print(f"✅ Found company {company_data_dl.get('name', 'Unknown')}")
            
            # Apply fiscal year/quarter filtering if specified
            if args.fiscal_year or args.fiscal_quarter:
                filtered_filings = []
                for filing in existing_filings:
                    # Create a filing dict compatible with the filter function
                    # Convert datetime objects to strings if needed
                    report_date = filing.get('report_date', '')
                    filing_date = filing.get('filing_date', '')
                    
                    if hasattr(report_date, 'strftime'):
                        report_date = report_date.strftime('%Y-%m-%d')
                    if hasattr(filing_date, 'strftime'):
                        filing_date = filing_date.strftime('%Y-%m-%d')
                    
                    filing_dict = {
                        'reportDate': report_date,
                        'filingDate': filing_date,
                        'form': filing.get('form_type', '')
                    }
                    company_dict = {
                        'fiscalYearEnd': existing_company.get('fiscal_year_end_code', '1231')
                    }
                    
                    if should_process_filing_for_download_period(filing_dict, company_dict, args.fiscal_year, args.fiscal_quarter):
                        filtered_filings.append(filing)
                existing_filings = filtered_filings
                print(f"After fiscal year/quarter filtering: {len(existing_filings)} filings")
            
            print(f"📄 Found {len(existing_filings)} filings in database to download HTML for")
            
            html_files_downloaded = 0
            
            for filing in existing_filings:
                accession_number = filing.get('accession_number', '')
                filing_date = filing.get('filing_date', '')
                form_type = filing.get('form_type', '')
                
                if not accession_number:
                    continue
                
                print(f"  📋 Processing filing {form_type} - {accession_number} ({filing_date})")
                
                # Get ticker for folder organization
                ticker = sec_client.get_ticker_from_cik(cik)
                
                # Download HTML file (use filing_date if available, fallback to default)
                filing_date_str = filing_date if filing_date else '2010-01-01'
                html_downloaded = sec_client.download_html_filing(cik, accession_number, filing_date_str, html_download_path, ticker)
                if html_downloaded:
                    print(f"    ✅ HTML file downloaded: {accession_number}")
                    html_files_downloaded += 1
                else:
                    print(f"    ❌ HTML file download failed: {accession_number}")
            
            print(f"📊 Results for CIK {cik}:")
            print(f"    HTML files downloaded: {html_files_downloaded}/{len(existing_filings)}")
            
            # Consider successful if we downloaded at least some files
            success = html_files_downloaded > 0 or len(existing_filings) == 0
            results[cik] = success
            
            if success:
                print(f"✅ Successfully downloaded HTML files for CIK {cik}")
            else:
                print(f"❌ No HTML files downloaded for CIK {cik}")
        
        except Exception as e:
            print(f"❌ Error processing CIK {cik}: {e}")
            logger.error(f"HTML download-only mode error for CIK {cik}: {e}")
            results[cik] = False
    
    # Cleanup
    try:
        database_config.disconnect()
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    
    return results


def should_process_filing_for_download_period(filing: Dict, company_data: Dict, target_fiscal_year: Optional[int] = None, target_fiscal_quarter: Optional[str] = None) -> bool:
    """
    Determine if a filing should be processed based on fiscal year/quarter criteria for download-only mode
    """
    if not target_fiscal_year and not target_fiscal_quarter:
        return True
    
    # Extract fiscal period information from filing
    from utilities.helpers.period_utils import FiscalYearCalculator
    
    # Get filing date and company fiscal year end
    filing_date = filing.get('reportDate') or filing.get('filingDate', '')
    fiscal_year_end = company_data.get('fiscalYearEnd', '1231')
    
    if not filing_date:
        return False
    
    try:
        # Parse the filing date
        parsed_date = datetime.strptime(filing_date, '%Y-%m-%d')
        
        # Calculate fiscal year and quarter using static methods
        filing_fiscal_year, filing_quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
            parsed_date, fiscal_year_end
        )
        
        # Filter by fiscal year
        if target_fiscal_year and filing_fiscal_year != target_fiscal_year:
            return False
        
        # Filter by fiscal quarter (if specified)
        if target_fiscal_quarter:
            quarter_map = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4}
            target_quarter_num = quarter_map.get(target_fiscal_quarter)
            if target_quarter_num and filing_quarter != target_quarter_num:
                return False
        
        return True
        
    except Exception as e:
        print(f"Error parsing filing date {filing_date}: {e}")
        return False
    
    return True


def download_xbrl_file_if_needed(sec_client, cik: str, accession_number: str, args) -> bool:
    """
    Download XBRL file if it doesn't already exist locally.
    This is a placeholder for XBRL download logic.
    """
    # For now, we'll just log that we would download the XBRL file
    # In a real implementation, you would:
    # 1. Check if XBRL file already exists locally
    # 2. If not, download it using SEC client's XBRL discovery logic
    # 3. Save it to a designated directory
    
    print(f"    📥 XBRL file download check: {accession_number}")
    
    # TODO: Implement actual XBRL file download logic
    # This would involve:
    # - Using the SEC client's _discover_xbrl_url method
    # - Downloading the XBRL file to a local directory
    # - Handling file naming and organization
    
    # For now, return True to indicate successful processing
    return True




def main():
    import argparse
    from pathlib import Path
    
    # Set up argument parser
    parser = argparse.ArgumentParser(
        prog='sec-scraper',
        description=(
            'SEC EDGAR Data Scraper — downloads and normalises 10-K/10-Q XBRL '
            'filings from the SEC API into MongoDB.\n\n'
            'Quickstart examples:\n'
            '  sec-scraper --file tickers.txt\n'
            '  sec-scraper --companies 0000320193 0001065280 --year 2015 --end-year 2026\n'
            '  sec-scraper --companies AAPL NFLX --reload --fiscal-year 2023\n'
            '  sec-scraper --url https://www.sec.gov/Archives/edgar/data/1065280/.../nflx-20230930.htm'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Company / filing selection ----------------------------------------
    src = parser.add_argument_group('Company / filing selection')
    src.add_argument(
        '--companies', nargs='+', metavar='CIK_OR_TICKER',
        help='One or more company CIKs or ticker symbols to process (e.g. AAPL 0001065280).')
    src.add_argument(
        '--file', metavar='FILE',
        help='Path to a plain-text file containing one CIK or ticker per line.')
    src.add_argument(
        '--tickers', action='store_true',
        help='Use the bundled tickers.json as the company source. Combined with --limit.')
    src.add_argument(
        '--limit', type=int, default=10, metavar='N',
        help='Max companies to process when using --tickers (default: 10).')
    src.add_argument(
        '--url', metavar='URL',
        help='Process a single SEC filing directly from its URL. '
             'Accepts EDGAR archives URLs or the viewer URL with cik= and accession_number= params.')

    # --- Year / period filtering -------------------------------------------
    period = parser.add_argument_group('Year / period filtering')
    period.add_argument(
        '--year', type=int, default=2010, metavar='YEAR',
        help='Earliest filing year to include (default: 2010). '
             'Combined with --end-year this defines a closed range.')
    period.add_argument(
        '--end-year', type=int, metavar='YEAR',
        help='Latest filing year to include (inclusive). '
             'Omit to process all filings from --year onwards. '
             'Example: --year 2015 --end-year 2026.')
    period.add_argument(
        '--fiscal-year', type=int, metavar='YEAR',
        help='Restrict processing to a specific fiscal year (e.g. 2024). '
             'Overrides --year / --end-year for period matching.')
    period.add_argument(
        '--fiscal-quarter', choices=['Q1', 'Q2', 'Q3', 'Q4'],
        help='Restrict to a specific fiscal quarter within --fiscal-year (e.g. Q3). '
             'Requires --fiscal-year.')

    # --- Processing behaviour ----------------------------------------------
    proc = parser.add_argument_group('Processing behaviour')
    proc.add_argument(
        '--reload', action='store_true',
        help='Force reprocessing of filings that are already in the database. '
             'Scope with --year / --end-year or --fiscal-year / --fiscal-quarter '
             'to reload only a specific range.')
    proc.add_argument(
        '--incremental', action='store_true',
        help='Process only filings filed after the most recent filing already '
             'stored for each company. Useful for routine updates.')
    proc.add_argument(
        '--no-dimensions', action='store_true',
        help='Skip dimensional (segment / product / geography) data extraction. '
             'Dimensions are enabled by default.')
    proc.add_argument(
        '--local', action='store_true',
        help='Read XBRL zip files from XBRL_ZIP_CACHE_PATH instead of '
             'downloading from the SEC API.')
    proc.add_argument(
        '--no-reconciliation', action='store_true',
        help='Disable the post-processing gap-fill step that back-fills missing '
             'periods from SEC companyfacts (enabled by default).')

    # --- HTML download options --------------------------------------------
    html = parser.add_argument_group('HTML filing downloads')
    html.add_argument(
        '--no-download-html-filings', action='store_true',
        help='Skip HTML filing downloads entirely. '
             'By default, filings are saved to SEC_HTML_DOWNLOAD_PATH when that '
             'env variable is set.')
    html.add_argument(
        '--only-download-files', action='store_true',
        help='Download HTML filings only — no XBRL extraction or database writes. '
             'Requires SEC_HTML_DOWNLOAD_PATH to be set.')

    # --- Output / logging -------------------------------------------------
    log = parser.add_argument_group('Output / logging')
    log.add_argument(
        '--verbose', action='store_true',
        help='Print detailed per-filing log output. Disables the progress bars.')
    log.add_argument(
        '--debug', action='store_true',
        help='Print DEBUG-level logs (implies --verbose). Use for troubleshooting.')
    
    args = parser.parse_args()
    
    # --verbose implies debug-level console; --debug is the highest detail
    is_verbose = args.verbose or args.debug

    # Configure logging
    if args.debug:
        from utilities.helpers.logger_config import LoggerConfig
        LoggerConfig.setup_logging(level='DEBUG', detailed=True, console_output=True)
        logger.info("Debug logging enabled")
    elif args.verbose:
        from utilities.helpers.logger_config import LoggerConfig
        LoggerConfig.setup_logging(level='INFO', detailed=True, console_output=True)
        logger.info("Verbose logging enabled")
    else:
        # Clean mode: suppress logger console output; progress bars carry all UI
        from utilities.helpers.logger_config import LoggerConfig
        LoggerConfig.setup_logging(level='WARNING', console_output=False)

    # Build progress manager — verbose=False activates clean tqdm bars
    progress = ProgressManager(verbose=is_verbose)

    # Install a SIGINT handler so the first Ctrl+C immediately tears down the
    # progress bars (stopping any half-drawn refresh) before the normal
    # KeyboardInterrupt unwinding runs the graceful shutdown path below.
    import signal as _signal

    def _handle_sigint(signum, frame):
        try:
            progress.close_all()
        except Exception:
            pass
        raise KeyboardInterrupt

    _signal.signal(_signal.SIGINT, _handle_sigint)

    # Log startup information
    logger.info("="*80)
    logger.info("SEC Data Scraper v9 - Enhanced with DRY Principles")
    logger.info("="*80)
    logger.info(f"Arguments: {vars(args)}")
    
    # Validate arguments
    if args.fiscal_quarter and not args.fiscal_year:
        print("❌ --fiscal-quarter requires --fiscal-year to be specified")
        sys.exit(1)
    
    if args.fiscal_year and args.fiscal_year < 1990:
        print("❌ --fiscal-year must be 1990 or later")
        sys.exit(1)
    
    if args.end_year is not None and args.end_year < args.year:
        print(f"❌ --end-year ({args.end_year}) cannot be earlier than --year ({args.year})")
        sys.exit(1)
    
    if args.limit and not args.tickers and not args.companies and not args.file and not args.url:
        # Default behavior: use tickers with limit
        args.tickers = True
    
    # Validate conflicting options for --url
    if args.url:
        if args.companies or args.file or args.tickers:
            print("❌ --url cannot be used with --companies, --file, or --tickers")
            sys.exit(1)
        if args.local:
            print("❌ --url cannot be used with --local (local processing mode)")
            sys.exit(1)
        if args.only_download_files:
            print("❌ --url cannot be used with --only-download-files")
            sys.exit(1)
    
    # HTML download: enabled by default; disabled only with --no-download-html-filings.
    # Silently skip (no hard exit) if SEC_HTML_DOWNLOAD_PATH is not configured.
    html_download_path = None
    if not args.no_download_html_filings:
        html_download_path = os.getenv('SEC_HTML_DOWNLOAD_PATH')
        if html_download_path:
            html_path = Path(html_download_path)
            try:
                html_path.mkdir(parents=True, exist_ok=True)
                if not html_path.is_dir():
                    print(f"⚠️  SEC_HTML_DOWNLOAD_PATH is not a directory: {html_download_path} — HTML downloads disabled")
                    html_download_path = None
                else:
                    if is_verbose:
                        tqdm.write(f"📁 HTML filings will be saved to: {html_path.absolute()}")
            except Exception as e:
                print(f"⚠️  Invalid SEC_HTML_DOWNLOAD_PATH: {e} — HTML downloads disabled")
                html_download_path = None
    
    try:
        # Handle --url option for single filing processing
        if args.url:
            print(f"\n🔗 Processing single filing from URL...")
            print(f"URL: {args.url}")
            print(f"{'='*50}")
            
            # Parse the URL to extract CIK and accession number
            filing_info = parse_sec_filing_url(args.url)
            if not filing_info:
                print(f"❌ Failed to parse SEC filing URL: {args.url}")
                print("💡 Supported URL formats:")
                print("   - https://www.sec.gov/cgi-bin/viewer?action=view&cik=320193&accession_number=0000320193-23-000077")
                print("   - https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230701.htm")
                print("   - https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/")
                sys.exit(1)
            
            cik = filing_info['cik']
            accession_number = filing_info['accession_number']
            
            print(f"📊 Extracted filing information:")
            print(f"   CIK: {cik}")
            print(f"   Accession Number: {accession_number}")
            print(f"{'='*50}")
            
            # Initialize scraper app
            app = SECDataScraperApp(
                start_year=args.year,
                end_year=args.end_year,
                enable_dimensions=not args.no_dimensions,
                target_fiscal_year=args.fiscal_year,
                target_fiscal_quarter=args.fiscal_quarter,
                reload=args.reload,
                incremental=False,  # Not applicable for single filing
                html_download_path=html_download_path,
                xbrl_zip_cache_path=os.getenv('XBRL_ZIP_CACHE_PATH'),
                enable_reconciliation=not args.no_reconciliation,
                progress=progress,
            )
            
            if not app.setup_database():
                print("❌ Failed to setup database")
                sys.exit(1)
            
            # Process the single filing
            success = app.process_single_filing_from_url(cik, accession_number)
            
            # Cleanup
            app.cleanup()
            
            # Show results
            print(f"\n{'='*50}")
            if success:
                print(f"✅ Filing processed successfully!")
                print(f"   CIK: {cik}")
                print(f"   Accession Number: {accession_number}")
            else:
                print(f"❌ Failed to process filing")
                print(f"   CIK: {cik}")
                print(f"   Accession Number: {accession_number}")
                print(f"   Check logs for details")
            print(f"{'='*50}")
            
            sys.exit(0 if success else 1)
        
        # Load companies
        companies = []
        if args.companies:
            companies = args.companies
            print(f"Using specified companies: {companies}")
        elif args.file:
            companies_file = Path(args.file)
            if companies_file.exists():
                if is_verbose:
                    print(f"Loading companies/tickers from {args.file}...")
                import json
                import re

                # Load ticker -> CIK mapping once (case-insensitive keys)
                try:
                    with open('tickers.json', 'r') as tf:
                        ticker_data = json.load(tf)
                        ticker_map = {k.upper(): v for k, v in ticker_data.get('ticker_to_cik', {}).items()}
                except Exception:
                    ticker_map = {}

                # Initialize SEC client for validation of new entries
                sec_client = SECAPIClient()

                with open(companies_file, 'r') as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line or line.startswith('#'):
                            continue

                        # Split tokens by commas or whitespace so one line can contain multiple entries
                        tokens = re.split(r'[,\s]+', line)
                        for token in tokens:
                            token = token.split('#')[0].strip()
                            if not token:
                                continue

                            cik_resolved = None

                            # If token looks like a numeric CIK, validate and append
                            if re.fullmatch(r'\d{1,10}', token):
                                cik_val = token.zfill(10)
                                # Validate CIK exists in SEC
                                if sec_client.validate_cik(cik_val):
                                    cik_resolved = cik_val
                                    if is_verbose:
                                        print(f"✅ Validated CIK: {token}")
                                else:
                                    tqdm.write(f"❌ CIK not found in SEC: {token} — skipping")
                            else:
                                # Treat token as ticker symbol
                                mapped = ticker_map.get(token.upper())
                                if mapped:
                                    cik_resolved = mapped
                                    if is_verbose:
                                        print(f"✅ Found ticker in tickers.json: {token} → {mapped}")
                                else:
                                    # Try to treat it as a CIK anyway (in case it's an unpadded CIK)
                                    if sec_client.validate_cik(token):
                                        cik_resolved = token.zfill(10)
                                        if is_verbose:
                                            print(f"✅ Validated as CIK in SEC: {token}")
                                    else:
                                        tqdm.write(f"⚠️  Unknown ticker or CIK: {token} — skipping")

                            if cik_resolved:
                                companies.append(cik_resolved)
                                # Remember the symbol the user typed so the
                                # progress bar can show it (CIK->ticker reverse
                                # lookup may return a different sibling ticker).
                                if not re.fullmatch(r'\d{1,10}', token):
                                    progress.cik_labels[cik_resolved] = token.upper()

                if is_verbose:
                    print(f"Loaded {len(companies)} companies from {args.file}")
            else:
                tqdm.write(f"❌ Companies file not found: {args.file}")
                sys.exit(1)
        else:
            # Default behavior: use tickers.json
            companies = load_companies_from_tickers(limit=args.limit, verbose=args.verbose)
            if not companies:
                print("❌ No companies loaded from tickers.json")
                print("💡 Try: python sec_scraper_cli.py --companies 0000320193 0000789019")
                sys.exit(1)
        
        if not companies:
            print("❌ No companies to process")
            print("📝 Use --companies with CIK list or --tickers with --limit")
            sys.exit(1)
        
        # Initialize and run scraper
        if progress.verbose:
            print(f"\nStarting SEC data scraper...")
            print(f"Companies to process: {len(companies)}")
            if args.fiscal_year:
                if args.fiscal_quarter:
                    print(f"Target: Fiscal Year {args.fiscal_year} {args.fiscal_quarter}")
                else:
                    print(f"Target: Complete Fiscal Year {args.fiscal_year}")
            else:
                if args.end_year:
                    print(f"Year range: {args.year} to {args.end_year}")
                else:
                    print(f"Start year: {args.year}")
            print(f"{'='*50}")
            if args.local:
                print("LOCAL PROCESSING MODE - Using offline XBRL zip files")
            else:
                print("ONLINE PROCESSING MODE - Downloading from SEC API")
            if args.no_dimensions:
                print("Dimensions processing DISABLED")
            else:
                print("Dimensions processing ENABLED - will capture segment, product, and geographic data")
            if html_download_path:
                print(f"HTML downloading ENABLED - saving to {html_download_path}")
            elif args.no_download_html_filings:
                print("HTML downloading DISABLED (--no-download-html-filings)")
            if args.only_download_files:
                print("📁 HTML-DOWNLOAD-ONLY MODE - Will only download HTML files for existing data")
                if not html_download_path:
                    print("⚠️  --only-download-files requires SEC_HTML_DOWNLOAD_PATH to be set (and not --no-download-html-filings)")
                    print("💡 Set SEC_HTML_DOWNLOAD_PATH in your .env file")
                    sys.exit(1)
            if args.reload:
                reload_msg = "🔄 RELOAD MODE ENABLED"
                if args.fiscal_year:
                    if args.fiscal_quarter:
                        reload_msg += f" - Will refresh FY{args.fiscal_year} {args.fiscal_quarter} data"
                    else:
                        reload_msg += f" - Will refresh complete FY{args.fiscal_year} data"
                else:
                    reload_msg += f" - Will refresh all filings from {args.year} onwards"
                print(reload_msg)
            print(f"{'='*50}")
        
        # Handle download-only mode
        if args.only_download_files:
            print("\n📁 Starting download-only mode...")
            results = download_files_only_mode(companies, args)
            
            # Show results
            successful = sum(1 for success in results.values() if success)
            total = len(results)
            
            print(f"\n{'='*50}")
            print(f"HTML DOWNLOAD-ONLY MODE COMPLETE")
            print(f"{'='*50}")
            print(f"Companies processed: {successful}/{total}")
            
            if successful == total:
                print(f"\nAll companies' HTML files downloaded successfully!")
            else:
                print(f"\n{total - successful} companies had issues downloading HTML files. Check logs for details.")
            
            sys.exit(0)
        
        if args.local:
            # Use full scraper app for local XBRL files with database storage
            database_config = DatabaseConfig()
            scraper_app = SECDataScraperApp(
                database_config=database_config,
                start_year=args.year or 2010,
                end_year=args.end_year,
                enable_dimensions=not args.no_dimensions,
                target_fiscal_year=args.fiscal_year,
                target_fiscal_quarter=args.fiscal_quarter,
                reload=args.reload,
                html_download_path=html_download_path,
                xbrl_zip_cache_path=os.getenv('XBRL_ZIP_CACHE_PATH'),
                enable_reconciliation=not args.no_reconciliation,
                progress=progress,
            )
            
            # Convert CIKs to tickers for local processing
            if args.verbose:
                print("Converting CIKs to tickers for local processing...")
            tickers_to_process = []
            
            # Load ticker mappings
            import json
            with open('tickers.json', 'r') as f:
                ticker_data = json.load(f)
            cik_to_ticker = {v: k for k, v in ticker_data.get('ticker_to_cik', {}).items()}
            
            for cik in companies:
                # Normalize CIK (remove leading zeros for lookup)
                normalized_cik = str(int(cik)) if cik.isdigit() else cik
                
                # Try both original CIK and normalized CIK
                ticker = cik_to_ticker.get(cik) or cik_to_ticker.get(normalized_cik)
                
                if ticker:
                    tickers_to_process.append(ticker)
                    if args.verbose:
                        print(f"  CIK {cik} → {ticker}")
                else:
                    if args.verbose:
                        print(f"  CIK {cik} not found in ticker mappings, skipping")
            
            if not tickers_to_process:
                print("No valid tickers found for local processing")
                sys.exit(1)
            
            print(f"\nProcessing {len(tickers_to_process)} companies locally: {', '.join(tickers_to_process)}")
            
            # Process companies using unified financial processor
            results = {}
            for ticker in tickers_to_process:
                print(f"\n🏢 Processing {ticker}...")
                try:
                    result = scraper_app.process_company_local(ticker, filing_limit=None)
                    
                    # Check for errors in the result
                    if 'error' in result:
                        print(f"❌ {ticker}: {result['error']}")
                        results[ticker] = False
                        continue
                    
                    success = result.get('processed_successfully', 0) > 0
                    results[ticker] = success
                    
                    if success:
                        total_statements = sum(len(r.get('statements', [])) for r in result.get('results', []) if r.get('status') == 'success')
                        print(f"✅ {ticker}: {result.get('processed_successfully', 0)} filings processed, {total_statements} statements extracted")
                    else:
                        failed_count = result.get('failed', 0)
                        print(f"❌ {ticker}: {failed_count} failures out of {result.get('total_filings', 0)} filings")
                        
                        # Show failed filing details only in verbose mode
                        if args.verbose:
                            failed_results = [r for r in result.get('results', []) if r.get('status') in ['failed', 'error']]
                            for failed_result in failed_results[:3]:  # Show first 3 failures
                                accession = failed_result.get('accession_number', 'unknown')
                                error_msg = failed_result.get('error', 'unknown error')
                                print(f"    💥 {accession}: {error_msg}")
                    
                except Exception as e:
                    print(f"❌ {ticker}: Exception - {e}")
                    results[ticker] = False
                    
            # No app to cleanup in local mode
            app = None
            
        else:
            # Use standard online processing
            app = SECDataScraperApp(
                start_year=args.year, 
                end_year=args.end_year,
                enable_dimensions=not args.no_dimensions, 
                target_fiscal_year=args.fiscal_year,
                target_fiscal_quarter=args.fiscal_quarter,
                reload=args.reload,
                incremental=args.incremental,
                html_download_path=html_download_path,
                xbrl_zip_cache_path=os.getenv('XBRL_ZIP_CACHE_PATH'),
                enable_reconciliation=not args.no_reconciliation,
                progress=progress,
            )
            
            if not app.setup_database():
                print("Failed to setup database")
                sys.exit(1)
            
            # Process companies
            results = app.process_multiple_companies(companies, resume=True)
        
        # Show results
        successful = sum(1 for success in results.values() if success)
        total = len(results)
        
        print(f"\n{'='*50}")
        print(f"PROCESSING COMPLETE")
        print(f"{'='*50}")
        print(f"Companies processed: {successful}/{total}")
        
        # Cleanup (only cleanup app if in online mode and app exists)
        if not args.local and app is not None:
            app.cleanup()
        
        if successful == total:
            print(f"\nAll companies processed successfully!")
        else:
            print(f"\n{total - successful} companies had issues. Check logs for details.")
        
        print(f"\nFor more features, try: python sec_scraper_cli.py --help")
        
    except KeyboardInterrupt:
        try:
            progress.close_all()
        except Exception:
            pass
        print("\nProcess interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
