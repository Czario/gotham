"""
Enhanced implementation of cashflow deaccumulation logic.
This service handles period-based financial data processing with clean deaccumulation formulas.

Clean Deaccumulation Logic for Cashflow:
- Q1: Individual deaccumulative value (keep as-is from source)
- Q2: Cumulative in source = Q1 + Q2 individual → Q2 = Q2_cumulative - Q1_individual  
- Q3: Cumulative in source = Q1 + Q2 + Q3 individual → Q3 = Q3_cumulative - (Q1_individual + Q2_individual)

Key Features:
- Only saves individual deaccumulative values to target database
- Handles standard SEC cashflow pattern: Q1 individual, Q2+ cumulative  
- No original cumulative values are saved to target database
- Clean mathematical formulas ensure data integrity
- Preserves original reporting period metadata as specified in user requirements
"""

import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from dataclasses import dataclass
from collections import defaultdict
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from ..core.config import AppConfig
from ..core.logging_config import get_status_logger
from ..database import DatabaseConnection, ConceptRepository, ValueRepository, FilingRepository
from ..core.models import ConceptDocument, ValueDocument
from ..utils.progress import progress_wrapper, create_progress_bar

logger = logging.getLogger(__name__)


@dataclass
class PeriodData:
    """Represents financial data for a specific period."""
    fiscal_year: int
    period_date: str
    form_type: str
    filing_id: ObjectId
    statement_id: ObjectId
    reporting_period: Dict[str, Any]
    values: Dict[str, float]  # concept_key -> value
    is_calculated: bool = False


@dataclass
class AnnualData:
    """Represents annual financial data from 10-K."""
    fiscal_year: int
    form_type: str
    filing_id: ObjectId
    statement_id: ObjectId
    reporting_period: Dict[str, Any]
    values: Dict[str, float]


class PeriodBasedFinancialCalculationService:
    """Clean service for cash flow deaccumulation."""
    
    def __init__(self, config: AppConfig, db_tracker=None):
        self.config = config
        self.db_connection = DatabaseConnection(config.database)
        self.db_tracker = db_tracker
        
        # Initialize status logger
        self.status_logger = get_status_logger(__name__)
        
        # Initialize filing repository for accession_number access
        self.filing_repo = FilingRepository(self.db_connection)
        
        # Use separate repositories for annual and quarterly concepts
        self.annual_concept_repo = ConceptRepository(self.db_connection, 'normalized_concepts_annual')
        self.quarterly_concept_repo = ConceptRepository(self.db_connection, 'normalized_concepts_quarterly')
        self.concept_repo = self.annual_concept_repo
        
        # Use separate repositories for annual and quarterly values
        self.annual_value_repo = ValueRepository(self.db_connection, 'concept_values_annual')
        self.quarterly_value_repo = ValueRepository(self.db_connection, 'concept_values_quarterly')
        self.value_repo = self.annual_value_repo
        
        # Cache for canonical period_dates to ensure consistency across filings
        # Key: (company_cik, statement_type, fiscal_year, quarter)
        # Value: canonical period_date string
        self._canonical_period_date_cache: Dict[tuple, str] = {}
    
    def _get_concept_repo_by_form_type(self, form_type: str) -> ConceptRepository:
        """Get the appropriate concept repository based on form type."""
        if form_type == '10-K':
            return self.annual_concept_repo
        elif form_type == '10-Q':
            return self.quarterly_concept_repo
        else:
            # Default to annual for unknown form types
            logger.warning(f"Unknown form type: {form_type}, defaulting to annual repository")
            return self.annual_concept_repo

    def _get_value_repo_by_form_type(self, form_type: str) -> ValueRepository:
        """Get the appropriate value repository based on form type."""
        if form_type == '10-K':
            return self.annual_value_repo
        elif form_type == '10-Q':
            return self.quarterly_value_repo
        else:
            # Default to annual for unknown form types
            logger.warning(f"Unknown form type: {form_type}, defaulting to annual value repository")
            return self.annual_value_repo

    def _find_concept_in_any_collection(self, company_cik: str, statement_type: str, concept: str) -> Optional[Dict[str, Any]]:
        """Find concept in either annual or quarterly collections."""
        # First try quarterly collection (most likely for quarterly calculations)
        concept_doc = self.quarterly_concept_repo.collection.find_one({
            'cik': company_cik,
            'statement_type': statement_type,
            'concept': concept
        })
        
        if concept_doc:
            logger.debug(f"Found concept {concept} in quarterly collection")
            return concept_doc
            
        # If not found in quarterly, try annual collection
        concept_doc = self.annual_concept_repo.collection.find_one({
            'cik': company_cik,
            'statement_type': statement_type,
            'concept': concept
        })
        
        if concept_doc:
            logger.debug(f"Found concept {concept} in annual collection")
            return concept_doc
            
        return None
    
    def process_all_companies(self) -> None:
        """Process companies for cash flow deaccumulation using database tracker if available."""
        logger.info("Starting cash flow processing for all companies")
        
        if self.db_tracker:
            # Use tracker to only process companies that need processing
            companies_to_process = self.db_tracker.get_companies_to_process()
            
            if len(companies_to_process) == 0:
                self.status_logger.info("✅ No companies need quarterly processing")
                return
                
            self.status_logger.info(f"📊 Processing quarterly calculations for {len(companies_to_process)} companies...")
            logger.info(f"Found {len(companies_to_process)} companies to process (using tracker)")
            
            # Use progress bar for company processing
            with create_progress_bar(
                total=len(companies_to_process),
                desc="Quarterly Processing",
                disable=False
            ) as pbar:
                processed_count = 0
                for company_cik in companies_to_process:
                    try:
                        # Get unprocessed statements for this company
                        unprocessed_statements = self.db_tracker.get_unprocessed_statements_for_company(company_cik)
                        if unprocessed_statements:
                            self.status_logger.info(f"🔢 Quarterly processing for company {company_cik} ({len(unprocessed_statements)} statements)")
                            logger.debug(f"Processing quarterly calculations for company: {company_cik}")
                            self._process_company_with_tracker(company_cik, unprocessed_statements)
                            processed_count += 1
                        else:
                            logger.debug(f"No unprocessed statements for company {company_cik}")
                    except Exception as e:
                        logger.error(f"Error processing company {company_cik}: {e}", exc_info=True)
                        continue
                    finally:
                        pbar.update(1)
                
                if processed_count > 0:
                    self.status_logger.info(f"✅ Processed quarterly data for {processed_count} companies")
        else:
            # Fallback to processing all companies (original behavior)
            source_db = self.db_connection.source_db
            companies = source_db['financial_statements'].distinct('cik')
            
            logger.info(f"Found {len(companies)} companies to process (no tracker)")
            
            # Use progress bar for company processing
            with create_progress_bar(
                total=len(companies),
                desc="Quarterly Processing",
                disable=False
            ) as pbar:
                processed_count = 0
                for company_cik in companies:
                    try:
                        logger.debug(f"Processing quarterly calculations for company: {company_cik}")
                        self._process_company(company_cik)
                        processed_count += 1
                    except Exception as e:
                        logger.error(f"Error processing company {company_cik}: {e}", exc_info=True)
                        continue
                    finally:
                        pbar.update(1)
                
                self.status_logger.info(f"✅ Processed quarterly data for {processed_count} companies")
        
        logger.info("Completed cash flow processing for all companies")
    
    def _process_company_with_tracker(self, company_cik: str, unprocessed_statements: set) -> None:
        """Process only specific unprocessed statements for a company."""
        source_db = self.db_connection.source_db
        
        # Get all statements for this company
        statements = list(source_db['financial_statements'].find({'cik': company_cik}))
        
        # Filter to only process unprocessed statements
        statements_to_process = [stmt for stmt in statements if stmt['_id'] in unprocessed_statements]
        
        if not statements_to_process:
            logger.info(f"No statements to process for company {company_cik}")
            return
            
        logger.info(f"Processing {len(statements_to_process)} unprocessed statements for company {company_cik}")
        
        # Group statements by type for processing
        statements_by_type = {}
        for stmt in statements_to_process:
            stmt_type = stmt['statement_type']
            if stmt_type not in statements_by_type:
                statements_by_type[stmt_type] = []
            statements_by_type[stmt_type].append(stmt)
        
        for statement_type, statements in statements_by_type.items():
            try:
                if statement_type.lower() in ['cash_flow', 'cashflow', 'cashflow']:
                    self._process_cash_flow_statement(company_cik, statement_type)
                elif statement_type.lower() not in ['balancesheet', 'balance_sheets']:
                    # Process income statements and other statement types
                    self._process_income_statement(company_cik, statement_type)
                # Balance sheets are skipped (point-in-time snapshots)
            except KeyError as e:
                logger.error(f"Missing field {e} in {statement_type} for {company_cik}")
            except Exception as e:
                logger.error(f"Error processing {statement_type} for {company_cik}: {e}")
                continue
    
    def _process_company(self, company_cik: str) -> None:
        """Process a single company for cash flow calculations."""
        # Get statement types for this company
        source_db = self.db_connection.source_db
        statement_types = source_db['financial_statements'].distinct(
            'statement_type', {'cik': company_cik}
        )
        
        for statement_type in statement_types:
            try:
                if statement_type.lower() in ['cash_flow', 'cashflow', 'cashflow']:
                    self._process_cash_flow_statement(company_cik, statement_type)
                elif statement_type.lower() not in ['balancesheet', 'balance_sheets']:
                    # Process income statements and other statement types
                    self._process_income_statement(company_cik, statement_type)
                # Balance sheets are skipped (point-in-time snapshots)
            except KeyError as e:
                logger.error(f"Missing field {e} in {statement_type} for {company_cik}")
            except Exception as e:
                logger.error(f"Error processing {statement_type} for {company_cik}: {e}")
                continue

    # ------------------------------------------------------------------
    # In-memory entry point — used by the merged scraper pipeline
    # ------------------------------------------------------------------

    def process_company_from_statements(self, company_cik: str, statements: list) -> None:
        """Run quarterly deaccumulation for a company using an in-memory statement list.

        This replaces all source-DB reads in the merged pipeline. ``statements``
        is the slim accumulator list built by the scraper — each entry is a dict
        with at least ``statement_type``, ``reporting_period``, ``data`` (line items),
        and ``_filing_doc`` (filing metadata dict with ``form_type`` and
        ``accession_number``).

        Balance sheets are silently skipped; only cash_flow and income_statement
        types are deaccumulated.
        """
        if not statements:
            return

        # Group by statement_type
        by_type: dict = {}
        for stmt in statements:
            st = stmt.get('statement_type', '')
            by_type.setdefault(st, []).append(stmt)

        for statement_type, stmts in by_type.items():
            try:
                if statement_type.lower() in ('cash_flow', 'cashflow', 'cashflow'):
                    self._process_cash_flow_from_memory(company_cik, statement_type, stmts)
                elif statement_type.lower() not in ('balancesheet', 'balance_sheets'):
                    self._process_income_from_memory(company_cik, statement_type, stmts)
            except Exception as e:
                logger.error(f"Error in quarterly deaccumulation for {company_cik} {statement_type}: {e}", exc_info=True)

    def _build_period_data_from_memory(self, company_cik: str, statement_type: str, stmts: list) -> tuple:
        """Convert in-memory slim statement dicts to (period_data, annual_data) lists."""
        period_data: list = []
        annual_data: list = []

        for stmt in stmts:
            filing_doc = stmt.get('_filing_doc', {})
            form_type = filing_doc.get('form_type', filing_doc.get('form', 'UNKNOWN'))
            reporting_period = stmt.get('reporting_period', {})
            fiscal_year = reporting_period.get('fiscal_year')
            period_date = reporting_period.get('period_date')

            if not fiscal_year or not period_date:
                continue

            from bson import ObjectId as _OID
            values = self._extract_financial_values(stmt.get('data', []))

            # Embed cik + statement_type + accession_number into reporting_period
            # so _save_period_data can use them without touching source_db
            enriched_period = dict(reporting_period)
            enriched_period['_cik'] = company_cik
            enriched_period['_statement_type'] = statement_type
            enriched_period['accession_number'] = filing_doc.get('accession_number',
                                                                  filing_doc.get('accessionNumber', ''))

            if form_type == '10-Q':
                period_data.append(PeriodData(
                    fiscal_year=fiscal_year,
                    period_date=period_date,
                    form_type=form_type,
                    filing_id=stmt.get('filing_id') or _OID(),
                    statement_id=stmt.get('_id') or _OID(),
                    reporting_period=enriched_period,
                    values=values,
                    is_calculated=False,
                ))
            elif form_type == '10-K':
                annual_data.append(AnnualData(
                    fiscal_year=fiscal_year,
                    form_type=form_type,
                    filing_id=stmt.get('filing_id') or _OID(),
                    statement_id=stmt.get('_id') or _OID(),
                    reporting_period=enriched_period,
                    values=values,
                ))

        return period_data, annual_data

    def _get_filing_doc_for_statement(self, stmt: dict) -> Optional[object]:
        """Return a minimal Filing-like object from the in-memory filing dict."""
        filing_raw = stmt.get('_filing_doc', {})
        if not filing_raw:
            return None
        from ..core.models import Filing as _Filing
        from bson import ObjectId as _OID
        return _Filing(
            id=filing_raw.get('_id', _OID()),
            form_type=filing_raw.get('form_type', filing_raw.get('form', 'UNKNOWN')),
            accession_number=filing_raw.get('accession_number', filing_raw.get('accessionNumber')),
        )

    def _process_cash_flow_from_memory(self, company_cik: str, statement_type: str, stmts: list) -> None:
        """Deaccumulate cash-flow statements from in-memory slim dicts."""
        period_data, annual_data = self._build_period_data_from_memory(company_cik, statement_type, stmts)
        data_by_year = self._group_by_fiscal_year(period_data, annual_data)

        # Build a stmt-id → filing-doc map for _save_period_data
        self._memory_filing_map = {
            str(stmt.get('_id', '')): self._get_filing_doc_for_statement(stmt)
            for stmt in stmts
        }

        for fiscal_year, year_data in data_by_year.items():
            periods = year_data['periods']
            if len(periods) < 2:
                continue
            is_cumulative = self._detect_cumulative_pattern(periods)
            individual_periods = self._deaccumulate_periods(periods)
            for period in individual_periods:
                self._save_period_data(period, is_calculated=True)

        self._memory_filing_map = {}

    def _process_income_from_memory(self, company_cik: str, statement_type: str, stmts: list) -> None:
        """Save income-statement periods from in-memory slim dicts."""
        period_data, _ = self._build_period_data_from_memory(company_cik, statement_type, stmts)

        self._memory_filing_map = {
            str(stmt.get('_id', '')): self._get_filing_doc_for_statement(stmt)
            for stmt in stmts
        }

        data_by_year = self._group_by_fiscal_year(period_data, [])
        for fiscal_year, year_data in data_by_year.items():
            for period in year_data['periods']:
                self._save_period_data(period, is_calculated=True)

        self._memory_filing_map = {}
    
    def _process_cash_flow_statement(self, company_cik: str, statement_type: str) -> None:
        """Process cash flow statements with clean deaccumulation logic.
        
        Standard cashflow pattern in source DB:
        - Q1: Individual deaccumulative values (already correct)
        - Q2: Cumulative (Q1 individual + Q2 individual)  
        - Q3: Cumulative (Q1 individual + Q2 individual + Q3 individual)
        
        Target DB receives only individual deaccumulative values:
        - Q1: Preserved as-is
        - Q2: Q2_cumulative - Q1_individual
        - Q3: Q3_cumulative - (Q1_individual + Q2_individual)
        """
        logger.info(f"Processing cashflow for {company_cik} using clean deaccumulation logic")
        
        # Get period and annual data
        period_data, annual_data = self._get_filing_data(company_cik, statement_type)
        
        # Group by fiscal year
        data_by_year = self._group_by_fiscal_year(period_data, annual_data)
        
        for fiscal_year, year_data in data_by_year.items():
            periods = year_data['periods']
            annual = year_data['annual']
            
            if len(periods) < 2:
                logger.info(f"Skipping {fiscal_year} - need at least 2 periods for processing")
                continue
            
            logger.info(f"Processing cashflow {fiscal_year} with {len(periods)} periods")
            
            # Step 1: Always assume standard cumulative pattern for cashflow
            is_cumulative = self._detect_cumulative_pattern(periods)
            
            # Step 2: Apply clean deaccumulation logic
            individual_periods = self._deaccumulate_periods(periods)
            
            # Step 3: Save ONLY individual deaccumulative values to target database
            for period in individual_periods:
                self._save_period_data(period, is_calculated=True)
    
    def _process_income_statement(self, company_cik: str, statement_type: str) -> None:
        """Process income statements.
        
        Income statements are typically individual quarters already, so we save them
        as calculated to maintain consistency with cash flow processing.
        """
        logger.info(f"Processing income statement for {company_cik}")
        
        # Get period and annual data
        period_data, annual_data = self._get_filing_data(company_cik, statement_type)
        
        # Group by fiscal year
        data_by_year = self._group_by_fiscal_year(period_data, annual_data)
        
        for fiscal_year, year_data in data_by_year.items():
            periods = year_data['periods']
            annual = year_data['annual']
            
            # Save income statement periods as calculated (they're already individual)
            for period in periods:
                self._save_period_data(period, is_calculated=True)
    
    def _get_filing_data(self, company_cik: str, statement_type: str) -> Tuple[List[PeriodData], List[AnnualData]]:
        """Get period and annual filing data for a company and statement type."""
        source_db = self.db_connection.source_db
        
        # Get statements with filing information
        pipeline = [
            {'$match': {'cik': company_cik, 'statement_type': statement_type}},
            {'$lookup': {'from': 'filings', 'localField': 'filing_id', 'foreignField': '_id', 'as': 'filing_info'}},
            {'$unwind': '$filing_info'},
            {'$match': {'filing_info.form_type': {'$in': ['10-Q', '10-K']}}},
            {'$sort': {'reporting_period.fiscal_year': 1, 'reporting_period.period_date': 1}}
        ]
        
        statements = list(source_db['financial_statements'].aggregate(pipeline))
        
        # Debug: Log what form types we found
        form_types = {}
        for stmt in statements:
            form_type = stmt['filing_info']['form_type']
            form_types[form_type] = form_types.get(form_type, 0) + 1
        
        logger.info(f"Found filings for {company_cik} {statement_type}: {form_types}")
        
        period_data = []
        annual_data = []
        
        for stmt in statements:
            form_type = stmt['filing_info']['form_type']
            reporting_period = stmt.get('reporting_period', {})
            
            if not reporting_period:
                continue
            
            # Use existing fiscal year from source database (no calculation needed)
            fiscal_year = reporting_period.get('fiscal_year')
            period_date = reporting_period.get('period_date')
            
            if not fiscal_year:
                logger.warning(f"No fiscal_year found in reporting_period for statement {stmt['_id']}")
                continue
                
            if not period_date:
                logger.warning(f"No period_date found in reporting_period for statement {stmt['_id']}")
                continue
            
            # Extract financial values - use 'data' field from database
            financial_data = stmt.get('data', stmt.get('financial_data', []))
            values = self._extract_financial_values(financial_data)
            
            # Debug: Log how many values were extracted
            logger.debug(f"Extracted {len(values)} values from {form_type} {period_date}")
            
            if form_type == '10-Q':
                period_data.append(PeriodData(
                    fiscal_year=fiscal_year,
                    period_date=period_date,
                    form_type=form_type,
                    filing_id=stmt['filing_id'],
                    statement_id=stmt['_id'],
                    reporting_period=reporting_period,
                    values=values,
                    is_calculated=False
                ))
            elif form_type == '10-K':
                annual_data.append(AnnualData(
                    fiscal_year=fiscal_year,
                    form_type=form_type,
                    filing_id=stmt['filing_id'],
                    statement_id=stmt['_id'],
                    reporting_period=reporting_period,
                    values=values
                ))
        
        logger.info(f"Processed {len(period_data)} quarterly periods and {len(annual_data)} annual statements")
        return period_data, annual_data
    
    def _normalize_period_key(self, period_key: str) -> str:
        """Normalize period key to extract just the date portion.
        
        Handles both formats:
        - "2025-03-31" -> "2025-03-31"
        - "2025-03-31 (Q1)" -> "2025-03-31"
        """
        import re
        # Extract just the date part (YYYY-MM-DD) from the key
        match = re.match(r'^(\d{4}-\d{2}-\d{2})', period_key.strip())
        return match.group(1) if match else period_key

    def _extract_financial_values(self, financial_data: List[Dict]) -> Dict[str, float]:
        """Extract financial values from financial data - Updated for new data structure.
        
        New data structure: Each item represents one fact for one period:
        {
            "concept": "us-gaap:SomeConcept",
            "value": 123456,
            "period": "2024-12-29 00:00:00"
        }
        """
        import re
        from datetime import datetime
        
        values = {}
        
        for item in financial_data:
            concept = item.get('concept')
            value = item.get('value')
            period = item.get('period')
            
            # Skip items without required fields
            if not concept or value is None or not period:
                continue
            
            # Skip abstract items (they don't have values)
            if item.get('abstract', False):
                continue
            
            try:
                # Convert period to date string format (YYYY-MM-DD)
                if isinstance(period, str):
                    # Handle period like "2024-12-29 00:00:00"
                    period_date = datetime.strptime(period.split(' ')[0], '%Y-%m-%d')
                else:
                    # Handle datetime objects
                    period_date = period
                
                period_str = period_date.strftime('%Y-%m-%d')
                
                # Convert value to float
                numeric_value = float(value)
                
                # Create value key in expected format: concept_date
                value_key = f"{concept}_{period_str}"
                values[value_key] = numeric_value
                
            except (ValueError, TypeError, AttributeError) as e:
                logger.debug(f"Error processing financial value: {e}")
                continue
        
        # Debug logging
        total_items = len(financial_data)
        abstract_items = sum(1 for item in financial_data if item.get('abstract', False))
        
        logger.debug(f"Financial data extraction: {total_items} total items, {abstract_items} abstract items, {len(values)} values extracted")
        
        if len(values) == 0 and total_items > 0:
            # Sample the first few non-abstract items to see what we have
            non_abstract = [item for item in financial_data[:5] if not item.get('abstract', False)]
            for i, item in enumerate(non_abstract):
                logger.warning(f"Sample item {i}: concept={item.get('concept')}, value={item.get('value')}, period={item.get('period')}")
        
        return values
    
    def _group_by_fiscal_year(self, period_data: List[PeriodData], annual_data: List[AnnualData]) -> Dict[int, Dict[str, Any]]:
        """Group period and annual data by fiscal year."""
        data_by_year: Dict[int, Dict[str, Any]] = {}
        
        # Initialize each fiscal year data structure
        for p_data in period_data:
            if p_data.fiscal_year:
                if p_data.fiscal_year not in data_by_year:
                    data_by_year[p_data.fiscal_year] = {'periods': [], 'annual': None}
                data_by_year[p_data.fiscal_year]['periods'].append(p_data)
        
        for a_data in annual_data:
            if a_data.fiscal_year:
                if a_data.fiscal_year not in data_by_year:
                    data_by_year[a_data.fiscal_year] = {'periods': [], 'annual': None}
                data_by_year[a_data.fiscal_year]['annual'] = a_data
        
        # Sort periods by date within each fiscal year
        for fiscal_year, year_data in data_by_year.items():
            year_data['periods'].sort(key=lambda x: x.period_date)
        
        return data_by_year
    
    def _detect_cumulative_pattern(self, periods: List[PeriodData]) -> bool:
        """For cashflow statements, always assume the standard SEC pattern.
        
        Standard cashflow pattern:
        - Q1: Individual quarter value (deaccumulative)
        - Q2: Cumulative (Q1 individual + Q2 individual)  
        - Q3: Cumulative (Q1 individual + Q2 individual + Q3 individual)
        
        This is the standard pattern in SEC filings and should be the default assumption.
        """
        if len(periods) < 2:
            return False
        
        logger.info("Using standard cashflow pattern: Q1 individual, Q2+ cumulative")
        return True
    
    def _deaccumulate_periods(self, periods: List[PeriodData]) -> List[PeriodData]:
        """Convert cumulative periods to individual deaccumulative periods.
        
        Clean deaccumulation logic:
        - Q1: Individual (keep as-is) - already deaccumulative
        - Q2: Q2_individual = Q2_cumulative - Q1_individual
        - Q3: Q3_individual = Q3_cumulative - (Q1_individual + Q2_individual)
        
        Only individual deaccumulative values are saved to target database.
        """
        sorted_periods = sorted(periods, key=lambda x: x.period_date)
        individual_periods = []
        
        logger.info(f"Deaccumulating {len(sorted_periods)} cashflow periods using clean logic")
        
        # Track individual deaccumulative values by base concept
        individual_sum_by_concept = defaultdict(float)
        
        for i, period in enumerate(sorted_periods):
            quarter_num = i + 1
            
            if i == 0:
                # Q1 is already individual deaccumulative - keep as-is
                individual_periods.append(period)
                logger.info(f"Q1 ({period.period_date}): kept as individual deaccumulative")
                
                # Track Q1 individual values for future calculations
                for value_key, value in period.values.items():
                    base_concept = self._get_base_concept(value_key)
                    individual_sum_by_concept[base_concept] = value
                    
            else:
                # Q2+ are cumulative - deaccumulate to individual values
                individual_values = {}
                
                for value_key, cumulative_value in period.values.items():
                    base_concept = self._get_base_concept(value_key)
                    
                    # Get sum of all previous individual quarters
                    previous_individual_sum = individual_sum_by_concept.get(base_concept, 0)
                    
                    # Calculate individual deaccumulative value
                    # Q2_individual = Q2_cumulative - Q1_individual
                    # Q3_individual = Q3_cumulative - (Q1_individual + Q2_individual)
                    individual_value = cumulative_value - previous_individual_sum
                    individual_values[value_key] = individual_value
                    
                    # Update running sum for next quarter
                    individual_sum_by_concept[base_concept] += individual_value
                    
                    logger.debug(f"Q{quarter_num} {base_concept}: {cumulative_value:,.0f} (cumulative) → {individual_value:,.0f} (individual)")
                
                # Create individual deaccumulative period
                individual_period = PeriodData(
                    fiscal_year=period.fiscal_year,
                    period_date=period.period_date,
                    form_type=period.form_type,
                    filing_id=period.filing_id,
                    statement_id=period.statement_id,
                    reporting_period=period.reporting_period,
                    values=individual_values,
                    is_calculated=True
                )
                
                individual_periods.append(individual_period)
                logger.info(f"Q{quarter_num} ({period.period_date}): deaccumulated to individual values")
        
        logger.info(f"Successfully converted {len(sorted_periods)} periods to individual deaccumulative values")
        return individual_periods

    def _get_or_set_canonical_period_date(
        self,
        company_cik: str,
        statement_type: str,
        fiscal_year: int,
        quarter: int,
        default_period_date: str
    ) -> str:
        """
        Get or set a canonical period_date for a given (company, statement_type, fiscal_year, quarter).
        
        This ensures all concept values for the same fiscal period have the same period_date,
        even if they come from different filings (e.g., 10-Q + 10-Q/A amendment).
        
        The canonical period_date is determined by:
        1. If cache has a value for this fiscal period, use it
        2. Otherwise, query existing values in DB for this fiscal period
        3. Use the most common period_date from existing data
        4. If no existing data, use the default (current filing's report_date)
        5. Cache the result for future use
        
        Args:
            company_cik: Company CIK number
            statement_type: Financial statement type (income_statement, balance_sheet, etc.)
            fiscal_year: Fiscal year
            quarter: Quarter number (1-4)
            default_period_date: Default period_date from current filing
            
        Returns:
            Canonical period_date string (YYYY-MM-DD format)
        """
        cache_key = (company_cik, statement_type, fiscal_year, quarter)
        
        # Check cache first
        if cache_key in self._canonical_period_date_cache:
            return self._canonical_period_date_cache[cache_key]
        
        # Query existing values for this fiscal period
        existing_values = list(self.quarterly_value_repo.collection.find(
            {
                'cik': company_cik,
                'statement_type': statement_type,
                'reporting_period.fiscal_year': fiscal_year,
                'reporting_period.quarter': quarter
            },
            {'reporting_period.period_date': 1}
        ).limit(100))  # Limit to avoid loading too much data
        
        if not existing_values:
            # No existing data, use the default (current filing's report_date)
            canonical_date = default_period_date
        else:
            # Find the most common period_date
            period_date_counts: Dict[str, int] = {}
            for val in existing_values:
                pd = val.get('reporting_period', {}).get('period_date')
                if pd:
                    period_date_counts[pd] = period_date_counts.get(pd, 0) + 1
            
            if not period_date_counts:
                canonical_date = default_period_date
            else:
                # Return the most common period_date
                canonical_date = max(period_date_counts.items(), key=lambda x: x[1])[0]
        
        # Cache the result
        self._canonical_period_date_cache[cache_key] = canonical_date
        
        return canonical_date

    def _save_period_data(self, period_data: PeriodData, is_calculated: bool = False) -> None:
        """Save period data to the normalized database with retries."""
        try:
            # Prefer values embedded by _build_period_data_from_memory (merged pipeline),
            # otherwise fall back to source-DB reads (legacy standalone runs).
            rp = period_data.reporting_period or {}
            company_cik = rp.get('_cik') or self._extract_cik_from_period_data(period_data)
            statement_type = rp.get('_statement_type') or self._extract_statement_type_from_period_data(period_data)

            if not company_cik or not statement_type:
                logger.error(f"Cannot extract company CIK ({company_cik}) or statement type ({statement_type})")
                return

            logger.debug(f"Attempting to save {len(period_data.values)} values for {company_cik} {statement_type}")

            saved_count = 0
            skipped_no_concept = 0
            skipped_exists = 0
            
            for value_key, value in period_data.values.items():
                # Extract concept from value key - concept is everything before the last underscore
                # which represents the date part
                concept = self._extract_concept_from_value_key(value_key)
                if not concept:
                    logger.warning(f"Could not extract concept from value key: {value_key}")
                    continue

                # Retry logic for database operations
                for attempt in range(3):
                    try:
                        # Convert underscore format to colon format for lookup
                        concept_lookup = concept.replace('_', ':') if '_' in concept else concept
                        
                        # Find concept in either annual or quarterly collections
                        concept_doc = self._find_concept_in_any_collection(
                            company_cik, statement_type, concept_lookup
                        )

                        if not concept_doc:
                            logger.debug(f"Concept {concept} not found for {company_cik} {statement_type}, skipping")
                            skipped_no_concept += 1
                            break

                        # Get filing information for accession_number.
                        # Prefer accession_number already embedded in reporting_period (merged pipeline).
                        # Fall back to _memory_filing_map, then skip (never read source_db).
                        # Normalize period_date to ensure consistency for the same fiscal period
                        # This prevents duplicate period columns when the same fiscal period has slightly different dates
                        clean_reporting_period = period_data.reporting_period.copy() if hasattr(period_data.reporting_period, 'copy') else dict(period_data.reporting_period)
                        
                        # Normalize period_date to canonical value for this fiscal period
                        quarter = clean_reporting_period.get('quarter')
                        if quarter is not None:
                            canonical_period_date = self._get_or_set_canonical_period_date(
                                company_cik=company_cik,
                                statement_type=statement_type,
                                fiscal_year=period_data.fiscal_year,
                                quarter=quarter,
                                default_period_date=clean_reporting_period.get('period_date', period_data.period_date)
                            )
                            clean_reporting_period['period_date'] = canonical_period_date
                        
                        filing_doc = None
                        if 'accession_number' not in clean_reporting_period or not clean_reporting_period.get('accession_number'):
                            filing_doc = getattr(self, '_memory_filing_map', {}).get(str(period_data.statement_id))
                            if filing_doc and filing_doc.accession_number:
                                clean_reporting_period['accession_number'] = filing_doc.accession_number

                        accession_number = clean_reporting_period.get('accession_number')

                        # Keep only canonical fields in reporting_period
                        _ALLOWED_RP_KEYS = {'end_date', 'period_date', 'fiscal_year', 'quarter'}
                        clean_reporting_period = {
                            k: v for k, v in clean_reporting_period.items()
                            if k in _ALLOWED_RP_KEYS
                        }

                        value_doc = ValueDocument(
                            concept_id=concept_doc['_id'],
                            company_cik=company_cik,
                            statement_type=statement_type,
                            form_type="10-Q",
                            reporting_period=clean_reporting_period,
                            value=value,
                            created_at=datetime.now(),
                            accession_number=accession_number,
                        )

                        value_doc_dict = value_doc.to_dict()
                        value_doc_dict['calculated'] = is_calculated
                        value_doc_dict.pop('_id', None)

                        # FIX: unique-key query no longer includes period_date (caused
                        # 1-day off-by-one duplicates in 52/53-week fiscal calendars) and
                        # now includes quarter (was previously missing, causing wrong-quarter
                        # matches / skipped inserts).
                        query = {
                            'concept_id': concept_doc['_id'],
                            'cik': company_cik,
                            'reporting_period.fiscal_year': period_data.fiscal_year,
                            'calculated': is_calculated
                        }
                        _query_quarter = period_data.reporting_period.get('quarter')
                        if _query_quarter is not None:
                            query['reporting_period.quarter'] = _query_quarter

                        period_date = period_data.reporting_period.get('period_date')

                        # REDUNDANCY FIX: a calculated row is only worth storing when it
                        # DIFFERS from the as-reported value.  Income statements (already
                        # individual quarters) produce a calculated value identical to the
                        # reported one — storing it just duplicates the row.
                        #
                        # CASH FLOW IS DELIBERATELY EXCLUDED: the deaccumulated cash-flow
                        # series (Q1..Q4) must stay COMPLETE so downstream consumers can read
                        # the full individual-quarter series via calculated=True.  Q1 cash flow
                        # equals its reported value but must still be persisted as calculated
                        # so the series has no gap.  Only Q2/Q3 differ, but we keep all of them.
                        _is_cash_flow = str(statement_type).lower() in (
                            'cash_flow', 'cashflow', 'cashflow'
                        )
                        if is_calculated and not _is_cash_flow:
                            reported_query = {
                                'concept_id': concept_doc['_id'],
                                'cik': company_cik,
                                'reporting_period.fiscal_year': period_data.fiscal_year,
                                'calculated': False,
                            }
                            # Match by quarter rather than period_date to avoid 1-day
                            # off-by-one mismatches in 52/53-week fiscal calendars
                            # (e.g. LEVI Q1 FY2026: reported row has 2026-03-02,
                            # quarterly-service PeriodData has 2026-03-01).
                            _q = period_data.reporting_period.get('quarter')
                            if _q is not None:
                                reported_query['reporting_period.quarter'] = _q
                            elif period_date:
                                reported_query['reporting_period.period_date'] = period_date
                            reported = self.quarterly_value_repo.collection.find_one(reported_query)
                            if reported is not None and reported.get('value') == value:
                                logger.debug(
                                    f"Skipping redundant calculated value for {concept} on "
                                    f"{period_date} (equals as-reported value)"
                                )
                                skipped_exists += 1
                                break

                        # FIX: atomic insert + DuplicateKeyError catch instead of
                        # non-atomic find_one() then insert_one() (race condition source).
                        try:
                            self.quarterly_value_repo.collection.insert_one(value_doc_dict)
                            saved_count += 1
                            logger.debug(f"Saved {'calculated' if is_calculated else 'original'} value for {concept}")
                        except DuplicateKeyError:
                            existing = self.quarterly_value_repo.collection.find_one(query)
                            if (accession_number and existing
                                    and not existing.get('accession_number')):
                                logger.debug(f"Updating existing quarterly value with missing accession_number for {concept} on {period_date}")
                                self.quarterly_value_repo.collection.update_one(
                                    {'_id': existing['_id']},
                                    {'$set': {'accession_number': accession_number}}
                                )
                            else:
                                logger.debug(f"Value already exists for {concept} on {period_date}, skipping (DuplicateKeyError)")
                            skipped_exists += 1

                        break  # Exit retry loop on success
                    except Exception as e:
                        logger.error(f"Error saving value for {concept} (Attempt {attempt + 1}/3): {e}")
                        if attempt == 2:
                            raise

            logger.info(f"Saved {saved_count} values for period {period_data.period_date} (skipped {skipped_no_concept} no concept, {skipped_exists} existing)")

            # Only warn when values were genuinely lost (concept lookup failed).
            # saved_count == 0 with everything in skipped_exists is normal on reruns/reload
            # (dedup correctly skips already-present values) and is NOT data loss.
            if saved_count == 0 and skipped_no_concept > 0:
                sample_concepts = list(period_data.values.keys())[:5]
                extracted_concepts = [self._extract_concept_from_value_key(k) for k in sample_concepts]
                logger.warning(
                    f"No values saved for period {period_data.period_date}: "
                    f"{skipped_no_concept} concept(s) not found in DB (potential data loss). "
                    f"Sample: {extracted_concepts}"
                )
            elif saved_count == 0 and len(period_data.values) > 0:
                logger.debug(
                    f"No new values for period {period_data.period_date} "
                    f"({skipped_exists} already existed) - expected on rerun/reload."
                )
                
        except Exception as e:
            logger.error(f"Error saving period data: {e}", exc_info=True)

    def _extract_cik_from_period_data(self, period_data: PeriodData) -> Optional[str]:
        """Extract company CIK from period data."""
        source_db = self.db_connection.source_db
        statement = source_db['financial_statements'].find_one({'_id': period_data.statement_id})
        return statement.get('cik') if statement else None
    
    def _extract_statement_type_from_period_data(self, period_data: PeriodData) -> Optional[str]:
        """Extract statement type from period data."""
        source_db = self.db_connection.source_db
        statement = source_db['financial_statements'].find_one({'_id': period_data.statement_id})
        return statement.get('statement_type') if statement else None
    
    def _get_base_concept(self, value_key: str) -> str:
        """Extract base concept from value key by removing date suffix."""
        import re
        return re.sub(r'_\d{4}-\d{2}-\d{2}$', '', value_key)
    
    def _extract_concept_from_value_key(self, value_key: str) -> Optional[str]:
        """Extract concept from value key formatted as 'concept_date'."""
        if not value_key or '_' not in value_key:
            return value_key
        
        # Split by underscore and take everything except the last part (which should be the date)
        parts = value_key.split('_')
        if len(parts) < 2:
            return value_key
        
        # The last part should be a date, so we take everything before it
        concept = '_'.join(parts[:-1])
        
        # Handle us-gaap: prefix - convert colon to underscore for database lookup
        if ':' in concept:
            concept = concept.replace(':', '_')
        
        return concept

    def generate_period_report(self, company_cik: str, fiscal_year: int) -> Dict[str, Any]:
        """Generate a report showing period data calculations for a company."""
        return {
            'cik': company_cik,
            'fiscal_year': fiscal_year,
            'status': 'Report generation not implemented in lean version'
        }
    
    def cleanup_original_cumulative_values(self, company_cik: Optional[str] = None) -> None:
        """Remove original cumulative values from target database, keeping only deaccumulated values.

        WARNING: This performs a bulk delete_many() and is NOT safe to run concurrently
        with active ingestion for the same company — a concept saved by another process
        after count_documents() but before delete_many() completes will still match the
        query and be deleted, and if that same process re-saves afterwards you can end up
        with the row missing until the next full run. Only run this as an offline/maintenance
        step when no ingestion is in progress for the target company_cik.
        """
        query: Dict[str, Any] = {'calculated': False}
        if company_cik:
            query['cik'] = company_cik

        # Clean up from quarterly repository (where quarterly calculations are stored)
        original_count = self.quarterly_value_repo.collection.count_documents(query)
        logger.info(f"Found {original_count} original cumulative values to remove from quarterly repository")

        if original_count > 0:
            result = self.quarterly_value_repo.collection.delete_many(query)
            logger.info(f"Removed {result.deleted_count} original cumulative values from quarterly repository")
        else:
            logger.info("No original cumulative values found to remove")
