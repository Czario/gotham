"""
Business logic services for financial data normalization.
"""
from typing import Dict, Set, Optional, NamedTuple, Any, List
from datetime import datetime
from bson import ObjectId
import logging

from ..core.models import ConceptKey, ConceptDocument, ValueDocument, Company, FinancialStatement, StatementBundle
from ..database import (
    DatabaseConnection,
    FinancialStatementRepository,
    FilingRepository,
    ConceptRepository,
    ValueRepository,
    CompanyRepository,
    DatabaseTracker
)
from ..core.config import AppConfig
from ..core.concept_canonicalization import canonical_concept
from ..core.row_identity import row_key_str
from ..core.logging_config import get_status_logger
from ..utils.hierarchy import HierarchyManager
from ..utils.progress import progress_wrapper, create_progress_bar
from .quarterly_service import PeriodBasedFinancialCalculationService
from ..utils.taxonomy import get_taxonomy_manager, lookup_concept_label
from ..utils.duplicate_prevention import DuplicatePreventionManager
from ..utils.label_cleaning import clean_label, clean_labels_in_item

logger = logging.getLogger(__name__)

# Statement types materialized into normalized_concepts_* / concept_values_*.
# Core statements plus equity_changes (share buybacks, dividends, retained
# earnings).  comprehensive_income is intentionally excluded — its key metrics
# (NetIncomeLoss, ComprehensiveIncomeNetOfTax) are already captured from the
# income_statement.
_ALLOWED_STATEMENT_TYPES = {'income', 'balancesheet', 'cashflow'}

# Structural / namespace guards used to reduce a statement hierarchy down to
# concrete financial line items (see ``_is_structural_concept``).
_NON_FIN_NS = ('dei:', 'srt:', 'country:', 'invest:')
_ALWAYS_SKIP_CONTAINS = (
    'IncomeStatementAbstract', 'StatementOfFinancialPositionAbstract',
    'StatementOfCashFlowsAbstract', 'StatementOfIncomeAndComprehensiveIncomeAbstract',
    'StatementOfStockholdersEquityAbstract', 'StatementTable', 'StatementLineItems',
    'ComprehensiveIncomeNetOfTaxAbstract', 'WeightedAverageNumberOfSharesOutstandingAbstract',
)


class DimensionalConceptInfo(NamedTuple):
    """Information about a dimensional concept to be created."""
    parent_concept: str
    segment_type: str
    concept: str


class FinancialNormalizationService:
    """Service for normalizing financial statement data."""
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.db_connection = DatabaseConnection(config.database)
        
        # Initialize status logger
        self.status_logger = get_status_logger(__name__)
        
        # Initialize repositories
        self.financial_repo = FinancialStatementRepository(self.db_connection)
        self.filing_repo = FilingRepository(self.db_connection)
        
        # Create separate concept repositories for annual and quarterly data
        self.annual_concept_repo = ConceptRepository(self.db_connection, 'normalized_concepts_annual')
        self.quarterly_concept_repo = ConceptRepository(self.db_connection, 'normalized_concepts_quarterly')
        
        # For backward compatibility, keep the general concept repo pointing to annual
        self.concept_repo = self.annual_concept_repo
        
        # Use separate repositories for annual and quarterly values
        self.annual_value_repo = ValueRepository(self.db_connection, 'concept_values_annual')
        self.quarterly_value_repo = ValueRepository(self.db_connection, 'concept_values_quarterly')
        self.value_repo = self.annual_value_repo
        
        self.company_repo = CompanyRepository(self.db_connection)
        
        # Initialize database tracker for intelligent processing
        self.db_tracker = DatabaseTracker(config.database, config.database)
        
        # Initialize hierarchy manager
        self.hierarchy_manager = HierarchyManager()
        
        # Initialize quarterly financial calculation service with tracker
        self.quarterly_service = PeriodBasedFinancialCalculationService(config, self.db_tracker)
        
        # Initialize duplicate prevention manager
        self.duplicate_manager = DuplicatePreventionManager(config)
        
        # Cache for concept lookups (includes both regular and dimensional concepts)
        # Key is (ConceptKey, form_type) to separate annual and quarterly concepts
        self.concept_cache: Dict[tuple, ObjectId] = {}
        
        # Taxonomy label fixing is not supported; taxonomy_manager is always None.
        self.taxonomy_manager = None

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

    def normalize_data(self) -> None:
        """Main method to normalize financial data using database tracker with sync behavior (only insert new data)."""
        logger.info("Starting normalization service with SYNC BEHAVIOR - only new data will be inserted, existing data preserved...")
        
        try:
            # First, copy companies from source to target database
            self.status_logger.info("🏢 Step 1: Copying companies from source to target database...")
            self._copy_companies()
            
            # Initialize and refresh the database tracker
            self.status_logger.info("🔍 Step 2: Analyzing database status (sync mode - only missing data)...")
            self.db_tracker.refresh_cache()
            
            # Show database summary in console
            summary = self.db_tracker.get_processing_summary()
            self.status_logger.info(f"📋 Found {summary.total_companies} companies in source database")
            self.status_logger.info(f"📋 {summary.processed_companies} companies already processed")
            self.status_logger.info(f"📋 Current progress: {summary.overall_percentage:.1f}%")
            self.status_logger.info("🔄 SYNC MODE: Will only insert missing periods, concepts, and values")
            
            self.db_tracker.log_summary()
            
            # Get companies that need processing
            companies_to_process = self.db_tracker.get_companies_to_process()
            
            if len(companies_to_process) == 0:
                self.status_logger.info("✅ All statements are already processed!")
                # Continue to quarterly calculations and accession number backfill
            else:
                self.status_logger.info(f"📊 Step 3: Syncing {len(companies_to_process)} companies (insert missing data only)...")
                
                # Use progress bar for company processing
                with create_progress_bar(
                    total=len(companies_to_process),
                    desc="Syncing Companies",
                    disable=False
                ) as pbar:
                    for company_cik in companies_to_process:
                        unprocessed_statements = self.db_tracker.get_unprocessed_statements_for_company(company_cik)
                        if unprocessed_statements:
                            logger.debug(f"Processing company {company_cik} with {len(unprocessed_statements)} unprocessed statements")
                            # Update progress bar description to show current company and statement count
                            pbar.set_description(f"Processing Companies - {company_cik} ({len(unprocessed_statements)} statements)")
                            self._process_company_statements_with_tracker(company_cik, unprocessed_statements)
                        else:
                            logger.debug(f"No unprocessed statements for company {company_cik}")
                            # Update progress bar description to show current company (no statements)
                            pbar.set_description(f"Processing Companies - {company_cik} (0 statements)")
                        pbar.update(1)
                
            # PROCESS 2: Sync missing accession numbers to existing data
            self.status_logger.info("🔄 Step 4: Updating existing records with missing accession numbers...")
            total_accession_updates = 0
            processed_companies = [cik for cik in companies_to_process if not self.db_tracker.get_unprocessed_statements_for_company(cik)]
            
            # Also check all existing companies in target DB for missing accession numbers
            all_target_companies = list(self.company_repo.target_collection.find({}, {'cik': 1}))
            all_company_ciks = list(set([company['cik'] for company in all_target_companies] + companies_to_process))
            
            for company_cik in all_company_ciks:
                try:
                    accession_stats = self._sync_missing_accession_numbers(company_cik)
                    total_accession_updates += accession_stats.get('updated_count', 0)
                except Exception as e:
                    logger.warning(f"Error updating accession numbers for company {company_cik}: {e}")
                    continue
            
            if total_accession_updates > 0:
                self.status_logger.info(f"✅ Updated {total_accession_updates} existing records with missing accession numbers")
            else:
                self.status_logger.info("✅ All existing records already have accession numbers")
            
            # Final summary
            self.status_logger.info("📋 Step 5: Generating final summary...")
            self.db_tracker.refresh_cache()
            summary = self.db_tracker.get_processing_summary()
            
            # Display final results
            self.status_logger.info("="*50)
            self.status_logger.info("📊 PROCESSING SUMMARY")
            self.status_logger.info("="*50)
            self.status_logger.info(f"Companies Total: {summary.total_companies}")
            self.status_logger.info(f"Companies Processed: {summary.processed_companies}")
            self.status_logger.info(f"Overall Progress: {summary.overall_percentage:.1f}%")
            self.status_logger.info("="*50)
            
            logger.info("Normalization completed successfully.")
            
        except Exception as e:
            logger.error(f"Error during normalization: {e}", exc_info=True)
            raise
        finally:
            self.db_tracker.close()
            self.db_connection.close()

    def normalize_data_without_quarterly(self) -> None:
        """Main method to normalize financial data without quarterly calculations using database tracker."""
        logger.info("Starting normalization service with database tracker (skipping quarterly calculations)...")
        
        try:
            # First, copy companies from source to target database
            self.status_logger.info("🏢 Step 1: Copying companies from source to target database...")
            self._copy_companies()
            
            # Initialize and refresh the database tracker
            self.status_logger.info("🔍 Step 2: Analyzing database status...")
            self.db_tracker.refresh_cache()
            
            # Show database summary in console
            summary = self.db_tracker.get_processing_summary()
            self.status_logger.info(f"📋 Found {summary.total_companies} companies in source database")
            self.status_logger.info(f"📋 {summary.processed_companies} companies already processed")
            self.status_logger.info(f"📋 Current progress: {summary.overall_percentage:.1f}%")
            
            self.db_tracker.log_summary()
            
            # Get companies that need processing
            companies_to_process = self.db_tracker.get_companies_to_process()
            
            if len(companies_to_process) == 0:
                self.status_logger.info("✅ All companies are already processed!")
                return
            
            self.status_logger.info(f"📊 Step 3: Processing {len(companies_to_process)} companies...")
            
            # Use progress bar for company processing
            with create_progress_bar(
                total=len(companies_to_process),
                desc="Processing Companies",
                disable=False
            ) as pbar:
                for company_cik in companies_to_process:
                    unprocessed_statements = self.db_tracker.get_unprocessed_statements_for_company(company_cik)
                    if unprocessed_statements:
                        logger.debug(f"Processing company {company_cik} with {len(unprocessed_statements)} unprocessed statements")
                        # Update progress bar description to show current company and statement count
                        pbar.set_description(f"Processing Companies - {company_cik} ({len(unprocessed_statements)} statements)")
                        self._process_company_statements_with_tracker(company_cik, unprocessed_statements)
                    else:
                        logger.debug(f"No unprocessed statements for company {company_cik}")
                        # Update progress bar description to show current company (no statements)
                        pbar.set_description(f"Processing Companies - {company_cik} (0 statements)")
                    pbar.update(1)
                
            # Final summary
            self.status_logger.info("📋 Step 4: Generating final summary...")
            self.db_tracker.refresh_cache()
            summary = self.db_tracker.get_processing_summary()
            
            # Display final results
            self.status_logger.info("="*50)
            self.status_logger.info("📊 PROCESSING SUMMARY")
            self.status_logger.info("="*50)
            self.status_logger.info(f"Companies Total: {summary.total_companies}")
            self.status_logger.info(f"Companies Processed: {summary.processed_companies}")
            self.status_logger.info(f"Overall Progress: {summary.overall_percentage:.1f}%")
            self.status_logger.info("="*50)
            
            logger.info("Normalization completed successfully (without quarterly calculations).")
            
        except Exception as e:
            logger.error(f"Error during normalization: {e}", exc_info=True)
            raise
        finally:
            self.db_tracker.close()
            self.db_connection.close()

    def _ensure_company_in_target(self, company_cik: str) -> None:
        """
        Ensure a company document exists in the target database.
        If missing, copies it from source. Safe to call any time - will not
        overwrite or duplicate an existing record. Never raises - errors are
        only logged so the caller's normalisation flow is never interrupted.
        """
        try:
            existing = self.company_repo.find_by_cik_target(company_cik)
            if existing:
                return  # Already there - nothing to do

            # Look up the company in source and copy it
            source_company = None
            for company in self.company_repo.find_all_source():
                if company.cik == company_cik:
                    source_company = company
                    break

            if source_company:
                self.company_repo.insert_target(source_company)
                logger.info(f"Inserted missing company document for CIK {company_cik} into target database")
            else:
                logger.warning(f"Company {company_cik} not found in source database - skipping company doc insert")
        except Exception as e:
            # Log the problem but never bubble up so normalisation keeps going
            logger.error(f"Failed to ensure company {company_cik} in target database: {e}", exc_info=True)

    def _copy_companies(self) -> None:
        """Copy companies from source to target database (sync mode - only copy missing companies)."""
        logger.info("Syncing companies from source to target database...")
        
        # Get total count first for status reporting
        total_companies = self.company_repo.source_collection.count_documents({})
        existing_count = self.company_repo.count_target()
        
        self.status_logger.info(f"📋 Found {existing_count} companies already in target database")
        self.status_logger.info(f"📋 Checking {total_companies} companies from source database...")
        
        # Copy companies from source to target (sync mode - only missing ones)
        copied_count = 0
        skipped_count = 0
        
        for company in self.company_repo.find_all_source():
            try:
                # Check if company already exists in target
                existing = self.company_repo.find_by_cik_target(company.cik)
                if not existing:
                    self.company_repo.insert_target(company)
                    copied_count += 1
                    logger.debug(f"Copied missing company {company.cik}: {company.name}")
                else:
                    skipped_count += 1
                    logger.debug(f"Company {company.cik} already exists in target database")
                    
            except Exception as e:
                logger.error(f"Error copying company {company.cik}: {e}")
                continue
        
        if copied_count > 0:
            self.status_logger.info(f"📋 Copied {copied_count} missing companies to target database")
        else:
            self.status_logger.info(f"📋 All {skipped_count} companies already exist in target database")
            
        logger.info(f"Company sync complete: copied {copied_count}, skipped {skipped_count}")

    def _process_financial_statement(self, statement) -> None:
        """Process a statement fetched from the source DB (legacy path).

        Resolves the filing from source DB then delegates to
        ``_process_financial_statement_with_filing``.
        """
        filing = self._get_filing(statement.filing_id)
        if not filing:
            logger.warning(f"Filing not found for id: {statement.filing_id}")
            return
        self._process_financial_statement_with_filing(statement, filing)

    # ------------------------------------------------------------------
    # In-memory API — used by the merged scraper pipeline (no source DB)
    # ------------------------------------------------------------------

    def _is_structural_concept(self, concept: str, is_abstract: bool) -> bool:
        """True when *concept* is structural/namespace noise, not a line item.

        Some filings report a zero-value fact for abstract/structural concepts
        (e.g. ``StatementOfFinancialPositionAbstract``), which makes them arrive
        with ``abstract=False`` — the name-pattern checks below catch those
        regardless of the abstract flag.
        """
        if concept.startswith(_NON_FIN_NS):
            return True  # DEI / SRT / country / invest are not financial line items
        if concept.endswith('Axis'):
            return True  # Axis concepts never carry values
        if any(p in concept for p in _ALWAYS_SKIP_CONTAINS):
            return True
        if is_abstract:
            local = concept.split(':')[-1]
            if local.endswith(('Abstract', 'Domain')):
                return True
        return False

    # ------------------------------------------------------------------
    # In-memory API — pure compute (no DB) + explicit persist
    # ------------------------------------------------------------------

    def _build_bundle(self, statement, filing, company_doc: Optional[dict] = None) -> StatementBundle:
        """PURE: build a ``StatementBundle`` from resolved model objects.

        No database reads or writes happen here.  Cross-company hierarchy
        reference resolution (path/order_key promotion) is deliberately a
        persistence concern — it happens in ``persist_statement_bundle``.
        """
        logger.debug(f"Building bundle for {statement.statement_type} / CIK {statement.company_cik}")

        # Clean every label before it can reach the database.  Taxonomy labels
        # carry XBRL presentation tags such as "[Member]" ("Rest Of World
        # [Member]") and redundant "Segment" words; those must never be
        # persisted.  Done in place so hierarchy/source views stay consistent.
        for _item in statement.financial_data:
            clean_labels_in_item(_item)
            for _fact in _item.get('dimensional_facts') or []:
                clean_labels_in_item(_fact)

        # PHASE 1 (pure): dimensional concepts + their source facts
        all_dimensional_concepts, dimension_data_map = self._extract_all_dimensional_concepts(
            statement.financial_data
        )

        # PHASE 2 (pure): the filing's own hierarchy + concrete-concept filter
        hierarchy_data = self.hierarchy_manager.build_hierarchy_data(statement.financial_data)

        # Grouping headers: abstract rows that PARENT at least one non-abstract
        # row ("Operating expenses:", "Earnings per share:").  They carry no
        # value but must be persisted as rows, otherwise their children attach
        # to the nearest preceding line item instead (e.g. R&D under Gross
        # Profit).  They are handled SEPARATELY from the concrete concepts so
        # each row is resolved exactly once.
        def _parents_a_concrete_row(candidate: dict) -> bool:
            path = candidate.get('path')
            if not path:
                return False
            return any(
                (not other.get('abstract'))
                and (other.get('path') or '').startswith(f"{path}.")
                for other in hierarchy_data
            )

        def _is_statement_root_header(concept: str) -> bool:
            """Statement-level/table wrappers carry no grouping information.

            ``IncomeStatementAbstract`` / ``StatementTable`` / ``...LineItems``
            only restate the statement type, so they stay out; mid-statement
            groupings (``OperatingExpenses``, ``EarningsPerShareBasicAbstract``)
            are what give nested rows their real parent.
            """
            if concept.startswith(_NON_FIN_NS) or concept.endswith('Axis'):
                return True
            if any(p in concept for p in _ALWAYS_SKIP_CONTAINS):
                return True
            return concept.split(':')[-1].endswith(('Table', 'LineItems', 'Domain', 'Abstract'))

        abstract_concepts = [
            item for item in hierarchy_data
            if item.get('abstract')
            and _parents_a_concrete_row(item)
            and not _is_statement_root_header(item.get('concept', ''))
        ]
        header_names = {item.get('concept') for item in abstract_concepts}

        concrete_concepts = [
            item for item in hierarchy_data
            if item.get('concept') not in header_names
            and not self._is_structural_concept(
                item.get('concept', ''), item.get('abstract', False)
            )
        ]

        # Re-root: if all concrete concepts were nested under a dropped statement root header
        # (e.g. "001.001", "001.002", "001.004.001"), re-root them by stripping the
        # "001." prefix so Revenue starts at "001", Cost at "002", etc., matching
        # production reference companies (TSLA, GOOGL, MSFT, META) with zero orphans.
        concrete_paths = [item.get('path') for item in concrete_concepts if item.get('path')]
        if concrete_paths and not any(p == '001' for p in concrete_paths) and all(p.startswith('001.') for p in concrete_paths):
            for item in concrete_concepts:
                if item.get('path', '').startswith('001.'):
                    item['path'] = item['path'][4:]
                    item['level'] = max(0, len(item['path'].split('.')) - 1)
                    item['hierarchy_level'] = item['level']

        logger.info(
            f"Bundle: kept {len(concrete_concepts)} concrete concept(s) and "
            f"{len(abstract_concepts)} grouping header(s), "
            f"skipped {len(hierarchy_data) - len(concrete_concepts) - len(abstract_concepts)} "
            f"abstract/structural"
        )

        dimensional_concepts = []
        for dim_info in all_dimensional_concepts:
            dim_data = dimension_data_map.get(dim_info, {})
            label = None
            dim_details = dim_data.get('dimension_details', {})
            for _ax, _det in dim_details.items():
                if isinstance(_det, dict) and _det.get('member_qname') == dim_info.concept:
                    label = _det.get('member_label') or _det.get('member_local_name')
                    break
            if not label:
                label = dim_data.get('fact_label') or dim_data.get('label') or dim_info.concept.split(':')[-1]
            dimensional_concepts.append({
                'parent_concept': dim_info.parent_concept,
                'segment_type': dim_info.segment_type,
                'concept': dim_info.concept,
                'label': clean_label(label),
                'dimension_data': dim_data,
            })

        # Informational flattened value view.  Persistence re-reads the items
        # (which carry fact_id/decimals/dimensional_facts) for the actual write.
        values = []
        for item in concrete_concepts:
            for period_key, value in self._extract_time_period_values(item).items():
                if value is None:
                    continue
                values.append({
                    'concept': item.get('concept'),
                    'period_key': period_key,
                    'value': value,
                    'fact_id': item.get('fact_id'),
                    'decimals': item.get('decimals'),
                    'unit': item.get('unit'),
                })

        return StatementBundle(
            company_cik=statement.company_cik,
            statement_type=statement.statement_type,
            form_type=filing.form_type,
            accession_number=filing.accession_number,
            reporting_period=statement.reporting_period,
            created_at=statement.created_at,
            statement_id=statement.id,
            filing_id=filing.id,
            concepts=concrete_concepts,
            abstract_concepts=abstract_concepts,
            dimensional_concepts=dimensional_concepts,
            values=values,
            dimensional_values=[],
            hierarchy=hierarchy_data,
            source_items=list(statement.financial_data),
            company_doc=dict(company_doc or {}),
        )

    def normalize_statement_to_bundle(
        self,
        statement_doc: dict,
        filing_doc: dict,
        company_doc: dict,
    ) -> Optional[StatementBundle]:
        """PURE: normalize a single statement into an in-memory bundle.

        Primary entry point for the merged scraper pipeline.  Performs NO
        database reads or writes — the bundle is plain data.  The caller
        decides whether/when to persist it via ``persist_statement_bundle``
        (in the agent pipeline, validation and repair run in between).

        Args:
            statement_doc: Raw statement dict as produced by the scraper.  Must
                contain ``cik``, ``statement_type`` and ``data`` (list of
                line-item dicts); ``reporting_period`` (dict) is optional.
            filing_doc: Filing metadata dict.  Must contain ``form_type`` and
                optionally ``accession_number``.
            company_doc: Company master dict (same shape as the ``companies``
                collection).  Written to the target DB on persist if missing.
        """
        from datetime import datetime as _datetime
        from ..core.models import FinancialStatement as _FS, Filing as _Filing

        filing = _Filing(
            id=filing_doc.get('_id', ObjectId()),
            form_type=filing_doc.get('form_type', filing_doc.get('form', 'UNKNOWN')),
            accession_number=filing_doc.get('accession_number',
                                            filing_doc.get('accessionNumber')),
        )

        statement = _FS(
            id=statement_doc.get('_id', ObjectId()),
            company_cik=statement_doc['cik'],
            filing_id=filing.id,
            statement_type=statement_doc['statement_type'],
            reporting_period=statement_doc.get('reporting_period', {}),
            created_at=statement_doc.get('created_at', _datetime.utcnow()),
            financial_data=statement_doc.get('data', statement_doc.get('financial_data', [])),
        )

        return self._build_bundle(statement, filing, company_doc)

    def persist_statement_bundle(
        self,
        bundle: Optional[StatementBundle],
        *,
        enforce_allowed_types: bool = True,
        replace_existing: bool = False,
    ) -> bool:
        """WRITES: persist an approved bundle to the target database.

        The only in-memory entry point that mutates the database.  Mirrors the
        historical write order exactly: ensure company -> resolve concept paths
        -> create/reuse concepts -> create/reuse dimensional concepts -> write
        values.

        Args:
            bundle: The bundle to persist (``None`` is a no-op).
            enforce_allowed_types: When True (merged pipeline) statements
                outside ``_ALLOWED_STATEMENT_TYPES`` are skipped.  The legacy
                source-DB path passes False to preserve its historical
                behaviour of processing every statement type.
            replace_existing: True for a ``--reload``: an existing row for the
                same period is overwritten instead of skipped, so the reload
                actually replaces the period's values.

        Returns:
            True when the bundle was written, False when it was skipped.
        """
        if bundle is None:
            return False

        # Companies are ensured before the statement-type filter (historical
        # behaviour).  A bundle with an empty company_doc is a safe no-op.
        self._ensure_company_in_target_from_dict(bundle.company_doc)

        if enforce_allowed_types and bundle.statement_type not in _ALLOWED_STATEMENT_TYPES:
            logger.debug(f"Skipping statement_type='{bundle.statement_type}' (not in allowed set)")
            return False

        from ..core.models import FinancialStatement as _FS, Filing as _Filing

        filing = _Filing(
            id=bundle.filing_id,
            form_type=bundle.form_type,
            accession_number=bundle.accession_number,
        )
        statement = _FS(
            id=bundle.statement_id,
            company_cik=bundle.company_cik,
            filing_id=bundle.filing_id,
            statement_type=bundle.statement_type,
            reporting_period=bundle.reporting_period,
            created_at=bundle.created_at,
            financial_data=bundle.source_items,
        )

        logger.debug(f"Persisting bundle: {statement.statement_type} for CIK {statement.company_cik}")

        concept_repo = self._get_concept_repo_by_form_type(filing.form_type)
        concept_mapping: dict = {}

        # P5 hierarchy resolution normally runs before this method. Resolved
        # bundles keep their approved company-specific path/order_key. The
        # fallback below preserves the P1/legacy behavior for direct callers
        # that bypass the graph.
        def _promote(item: dict, index: int) -> dict:
            """Resolve an item's hierarchy placement (P5-resolved items keep theirs)."""
            promoted_item = item.copy()
            if not item.get('_hierarchy_resolved'):
                reference = concept_repo.find_concept_reference_for_hierarchy(
                    statement.statement_type, item['concept'], dimension_concept=False
                )
                if reference:
                    promoted_item['path'] = reference['path']
                    promoted_item['order_key'] = reference['order_key']
                else:
                    promoted_item['path'] = f"{index + 1:03d}"
                    promoted_item['order_key'] = (
                        chr(ord('a') + (index % 26))
                        if index < 26
                        else f"{chr(ord('a') + (index // 26 - 1))}{chr(ord('a') + (index % 26))}"
                    )
                promoted_item['level'] = 1
            else:
                promoted_item['path'] = item.get('path')
                promoted_item['order_key'] = item.get('order_key')
                promoted_item['level'] = item.get(
                    'hierarchy_level', item.get('level', 0)
                )
            return promoted_item

        # Do NOT insert abstract wrapper concepts into the database (matching production reference
        # companies TSLA, GOOGL, META, MSFT which have 0 abstract concepts in the DB).
        # Exception: ``custom:`` prefixed grouping headers (e.g. custom:ProductSegmentation)
        # are deliberately abstract rows inserted by the hierarchy agent to group multi-
        # dimensional segment breakdowns.  They must be stored with abstract=True so the
        # agent and UI can distinguish them from financial line items.
        all_concepts_to_persist = [
            item for item in (list(bundle.concepts) + list(bundle.abstract_concepts or []))
            if not item.get("concept", "").split(":")[-1].endswith("Abstract")
        ]
        promoted_concepts = [
            _promote(item, i) for i, item in enumerate(all_concepts_to_persist)
        ]
        for item in promoted_concepts:
            # Preserve abstract=True for custom: grouping headers; force False for everything else.
            if not item.get("concept", "").startswith("custom:"):
                item["abstract"] = False
            concept_id = self._get_or_create_concept(
                statement.company_cik, statement.statement_type, item, filing.form_type
            )
            if concept_id:
                concept_mapping[item['concept']] = concept_id

        logger.info(f"Promotion: promoted {len(promoted_concepts)} concept(s) (abstract wrappers skipped)")


        # PHASE 3: Dimensional concepts
        dimensional_concept_mapping: dict = {}
        parent_concepts_with_dimensions = {
            item.get('concept'): True
            for item in statement.financial_data
            if item.get('dimensional_facts')
        }

        for dim in bundle.dimensional_concepts:
            if dim['parent_concept'] not in parent_concepts_with_dimensions:
                continue
            parent_concept_id = concept_mapping.get(dim['parent_concept'])
            if parent_concept_id:
                try:
                    dim_concept_id = self._get_or_create_dimensional_concept(
                        concept_id=parent_concept_id,
                        company_cik=statement.company_cik,
                        statement_type=statement.statement_type,
                        dimension_data=dim['dimension_data'],
                        form_type=filing.form_type,
                        assigned_path=dim.get('path'),
                        assigned_order_key=dim.get('order_key'),
                    )
                    dimensional_concept_mapping[(dim['parent_concept'], dim['concept'])] = dim_concept_id
                except Exception as e:
                    logger.warning(f"Error creating dimensional concept {dim['concept']}: {e}")

        # PHASE 4: Values
        for item in promoted_concepts:
            if item.get('abstract', False):
                continue
            concept_id = concept_mapping.get(item['concept'])
            if not concept_id:
                logger.warning(f"Concept ID not found for: {item['concept']}")
                continue
            time_period_values = self._extract_time_period_values(item)
            for _, value in time_period_values.items():
                if value is not None:
                    self._create_value_record(
                        concept_id=concept_id,
                        statement=statement,
                        filing=filing,
                        item=item,
                        value=value,
                        is_calculated=False,
                        replace_existing=replace_existing,
                    )
            if 'dimensional_facts' in item:
                self._process_dimensional_data_enhanced(
                    concept_id=concept_id,
                    statement=statement,
                    filing=filing,
                    item=item,
                    dimensional_concept_mapping=dimensional_concept_mapping,
                )

        return True

    def normalize_statement_in_memory(
        self,
        statement_doc: dict,
        filing_doc: dict,
        company_doc: dict,
    ) -> None:
        """Backward-compatible wrapper: compute a bundle, then persist it.

        Equivalent to ``normalize_statement_to_bundle`` followed by
        ``persist_statement_bundle``.  Kept so existing callers/tests keep
        working; new code should call the two methods explicitly so a
        validation/repair agent can sit between compute and persist.
        """
        bundle = self.normalize_statement_to_bundle(statement_doc, filing_doc, company_doc)
        self.persist_statement_bundle(bundle)

    def _process_financial_statement_with_filing(self, statement, filing) -> None:
        """Legacy source-DB path: build a bundle from model objects, then persist.

        Preserves the historical behaviour of processing every statement type
        (no allowed-type filter) and not touching the company doc here (the
        caller ensures the company before processing).
        """
        bundle = self._build_bundle(statement, filing, None)
        self.persist_statement_bundle(bundle, enforce_allowed_types=False)

    def apply_hierarchy_updates(self, updates: list[dict]) -> None:
        """Apply agent-approved repairs to existing concept hierarchy metadata."""
        for update in updates or []:
            concept_id = update.get('_id')
            if not concept_id:
                continue
            repo = self._get_concept_repo_by_form_type(
                '10-Q' if update.get('form_type') == '10-Q' else '10-K'
            )
            fields = {
                key: update[key]
                for key in ('path', 'order_key', 'hide', 'abstract', 'hierarchy_level', 'level')
                if update.get(key) is not None
            }
            if fields:
                # User rule: don't hide any concept, instead put in path 555
                if fields.get('path') == '555':
                    fields['hide'] = False
                fields['hierarchy_repaired_at'] = datetime.utcnow()
                fields['hierarchy_repair_reason'] = update.get('reason', 'agent_repath')
                res = repo.collection.update_one({'_id': concept_id}, {'$set': fields})
                if res.matched_count == 0:
                    other_repo = (
                        self.annual_concept_repo
                        if repo == self.quarterly_concept_repo
                        else self.quarterly_concept_repo
                    )
                    other_repo.collection.update_one({'_id': concept_id}, {'$set': fields})

    def update_hierarchy_seed_metadata(self, cik: str, hierarchy_plan: dict) -> None:
        """Persist P5 hierarchy seed metadata after financial data writes."""
        if not cik or not hierarchy_plan:
            return
        seeded_types = hierarchy_plan.get('seeded_statement_types') or []
        now = datetime.utcnow()
        update = {
            'hierarchy_seed_version': 1,
            'hierarchy_last_updated_at': now,
        }
        if seeded_types:
            update['hierarchy_seeded_at'] = now
            self.company_repo.target_collection.update_one(
                {'cik': str(cik)},
                {
                    '$set': update,
                    '$addToSet': {
                        'hierarchy_seeded_statement_types': {
                            '$each': seeded_types
                        }
                    },
                },
                upsert=False,
            )
        else:
            self.company_repo.target_collection.update_one(
                {'cik': str(cik)},
                {'$set': update},
                upsert=False,
            )

    def _ensure_company_in_target_from_dict(self, company_doc: dict) -> None:
        """Write company to target DB from an in-memory dict (no source DB read)."""
        cik = company_doc.get('cik')
        if not cik:
            return
        try:
            existing = self.company_repo.find_by_cik_target(cik)
            if existing:
                return
            from ..core.models import Company as _Company
            company = _Company.from_dict(company_doc)
            self.company_repo.insert_target(company)
            logger.info(f"Inserted company {cik} into target DB")
        except Exception as e:
            logger.error(f"Failed to insert company {cik}: {e}", exc_info=True)

    def _get_filing(self, filing_id: ObjectId):
        """Get filing by ID from source DB (legacy path — not used in merged pipeline)."""
        filing = self.filing_repo.find_by_id(filing_id)
        if not filing:
            logger.warning(f"Filing not found for id: {filing_id}")
        return filing

    def _get_or_create_concept(self, cik: str, statement_type: str, item: dict, form_type: str = '10-K') -> ObjectId:
        """
        Get existing concept or create new one using the appropriate repository based on form type.
        Always prioritizes existing concepts to avoid duplicates and maintain consistency.
        
        SYNC BEHAVIOR FOR REPROCESSING:
        - If concept exists in database: REUSE IT (preserve existing concept with all its properties)
        - If concept doesn't exist: CREATE IT
        
        This ensures that when all values are deleted but concepts remain for any statement,
        we keep those concepts as-is and only add new values to them during reprocessing.
        """
        if item.get("concept", "").split(":")[-1].endswith("Abstract"):
            return None
        # Preserve abstract=True for custom: grouping headers; force False for all others.
        if not item.get("concept", "").startswith("custom:"):
            item['abstract'] = False

        concept_key = ConceptKey(cik, statement_type, item['concept'])
        
        # Get the appropriate concept repository based on form type
        concept_repo = self._get_concept_repo_by_form_type(form_type)

        # Agent-decided concept equivalence: when the concept-resolution agent
        # (or a stored alias decision) judged this incoming concept name to be
        # the SAME economic line item as an already-stored concept under a
        # different tag, we must NOT create a duplicate row — the values attach
        # to the existing concept instead.
        alias_target = self._resolve_concept_alias(
            cik, statement_type, item, form_type, concept_repo
        )
        if alias_target:
            logger.info(
                f"Concept '{item['concept']}' resolved by alias to '{alias_target}' "
                f"for {cik} {statement_type} — reusing existing concept (no duplicate)"
            )
            existing = concept_repo.find_existing(
                cik, statement_type, alias_target,
                dimension_concept=item.get('dimension', False),
            )
            if existing:
                concept_id = existing['_id']
                logger.debug(
                    f"Alias reuse: {item['concept']} -> {alias_target} "
                    f"(ID: {concept_id}), skipping separate creation"
                )
                self.concept_cache[(concept_key, form_type)] = concept_id
                return concept_id
            # Target concept is gone (deleted/migrated): fall through and treat
            # the incoming concept normally below.
            logger.warning(
                f"Alias target '{alias_target}' not found for {cik} {statement_type} "
                f"— creating '{item['concept']}' as its own concept"
            )
        
        # Check cache first (make cache form-type specific)
        cache_key = (concept_key, form_type)
        if cache_key in self.concept_cache:
            logger.debug(f"Found concept in cache: {item['concept']}")
            return self.concept_cache[cache_key]
        
        # Always check database first for existing concept
        # Pass dimension_concept parameter to ensure correct matching
        existing = concept_repo.find_existing(
            cik, 
            statement_type, 
            item['concept'],
            dimension_concept=item.get('dimension', False)
        )
        if existing:
            concept_id = existing['_id']
            # Self-heal labels persisted before cleaning existed.
            cleaned_existing_label = clean_label(existing.get('label'))
            if cleaned_existing_label and cleaned_existing_label != existing.get('label'):
                concept_repo.collection.update_one(
                    {'_id': concept_id}, {'$set': {'label': cleaned_existing_label}}
                )
                logger.debug(
                    f"Cleaned stored label for existing concept {item['concept']}: "
                    f"{existing.get('label')!r} -> {cleaned_existing_label!r}"
                )
            logger.debug(f"Reusing existing concept: {item['concept']} (ID: {concept_id}) - keeping concept as-is")
        else:
            # Only create new concept if it doesn't exist
            logger.debug(f"Creating new concept: {item['concept']}")
            concept_id = self._create_concept(cik, statement_type, item, form_type)
        
        # Cache the result with form type
        self.concept_cache[cache_key] = concept_id
        return concept_id

    def _resolve_concept_alias(
        self, cik: str, statement_type: str, item: dict, form_type: str,
        concept_repo: 'ConceptRepository' = None,
    ) -> Optional[str]:
        """Return the alias target concept name for an incoming concept, if one
        has been decided (by the concept-resolution agent) for this
        company+statement+form-type.

        Preference order:
          1. The item's own ``_concept_target`` annotation — the in-memory
             decision made this run by the ``concept_resolve`` agent node;
          2. A previously recorded decision in the ``concept_aliases`` store
             (so callers that bypass the agent still get the merge).

        Returns ``None`` when no alias exists (the incoming concept is a real
        new concept and may be created).
        """
        target = item.get('_concept_target')
        if target:
            return target if isinstance(target, str) and target else None
        try:
            store = getattr(self, '_concept_alias_store', None)
            if store is None:
                from ..database import ConceptAliasStore
                store = ConceptAliasStore(self.db_connection)
                self._concept_alias_store = store
            return store.get(cik, statement_type, form_type, item.get('concept', ''))
        except Exception as exc:  # noqa: BLE001 — alias lookup must never break ingestion
            logger.debug(f"Concept alias lookup failed for {item.get('concept')}: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # Newest-period concept promotion
    # ------------------------------------------------------------------ #
    def promote_concept(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        from_concept: str,
        to_concept: str,
        *,
        winner_item: Optional[dict] = None,
    ) -> dict:
        """Newest-period concept wins: move ALL values of ``from_concept`` onto
        ``to_concept`` and hard-delete the ``from_concept`` concept row.

        Historical values are never dropped: each value row is repointed to the
        winner's concept ``_id``.  The only exception is a genuine same-period
        duplicate — the value collections have a unique key
        (cik, concept_id, fiscal_year, quarter), so if the winner already holds
        the exact period the loser's redundant row cannot coexist and is
        removed (the winner's row is kept).  That is reported as
        ``duplicate_values_dropped`` in the receipt.

        The delete of the losing concept row is the very last step, so a crash
        at any point can be retried safely.

        Scope is a single form type: a 10-Q promotion only ever touches the
        quarterly collections and a 10-K promotion only the annual ones.  The
        two never influence each other.

        Returns a small receipt (counts) for logging/audit.
        """
        receipt = {
            "cik": cik,
            "statement_type": statement_type,
            "form_type": form_type,
            "from_concept": from_concept,
            "to_concept": to_concept,
            "values_moved": 0,
            "duplicate_values_dropped": 0,
            "dimensional_children_moved": 0,
            "alias_rows_repointed": 0,
            "deleted": False,
            "skipped": None,
        }
        if not from_concept or not to_concept or from_concept == to_concept:
            receipt["skipped"] = "invalid_names"
            return receipt

        concept_repo = self._get_concept_repo_by_form_type(form_type)
        value_repo = self._get_value_repo_by_form_type(form_type)

        loser = concept_repo.find_existing(
            cik, statement_type, from_concept, dimension_concept=False
        )
        if not loser:
            # Already promoted (or never existed): make sure the old tag still
            # resolves to the winner, then stop.  Idempotent retry path.
            receipt["skipped"] = "from_concept_absent"
            receipt["alias_rows_repointed"] = self._repoint_concept_aliases(
                cik, statement_type, form_type, from_concept, to_concept
            )
            return receipt

        winner = concept_repo.find_existing(
            cik, statement_type, to_concept, dimension_concept=False
        )
        if not winner:
            item = dict(winner_item or {})
            item.update(
                {
                    "concept": to_concept,
                    "label": item.get("label") or loser.get("label"),
                    "path": item.get("path") or loser.get("path"),
                    "order_key": item.get("order_key") or loser.get("order_key"),
                    "abstract": False,
                    "dimension": loser.get("dimension", False),
                }
            )
            winner_id = self._create_concept(cik, statement_type, item, form_type)
            winner = concept_repo.find_existing(
                cik, statement_type, to_concept, dimension_concept=False
            ) or {"_id": winner_id, "concept": to_concept, "path": item.get("path")}
        winner_id = winner["_id"]

        # 1) MOVE values — repoint, never delete.  The value collection has a
        #    unique key (cik, concept_id, fiscal_year, quarter), so if the
        #    winner already holds the same period the bulk repoint trips it;
        #    fall back to a per-row move that keeps the winner's row and drops
        #    only the genuine same-period duplicate.
        try:
            moved = value_repo.collection.update_many(
                {"concept_id": loser["_id"]},
                {"$set": {"concept_id": winner_id}},
            )
            receipt["values_moved"] = getattr(moved, "modified_count", 0)
        except Exception as exc:  # noqa: BLE001 — likely a unique-key collision
            logger.info(
                "Concept promotion %s -> %s: bulk value move hit %s; "
                "retrying collision-safe",
                from_concept, to_concept, type(exc).__name__,
            )
            moved_count, dropped = self._move_values_collision_safe(
                value_repo, loser["_id"], winner_id, cik
            )
            receipt["values_moved"] = moved_count
            receipt["duplicate_values_dropped"] = dropped

        # 2) Re-parent the loser's dimensional children under the winner.
        #    (Dimensional VALUES hang off the dimensional concept id, which is
        #    unchanged, so they are not touched.)
        child = concept_repo.collection.update_many(
            {
                "cik": cik,
                "statement_type": statement_type,
                "concept_id": loser["_id"],
                "dimension_concept": True,
            },
            {
                "$set": {
                    "concept_id": winner_id,
                    "parent_concept": to_concept,
                    "parent_path": winner.get("path"),
                }
            },
        )
        receipt["dimensional_children_moved"] = getattr(child, "modified_count", 0)

        # 3) Decision store: members of the loser now resolve to the winner.
        receipt["alias_rows_repointed"] = self._repoint_concept_aliases(
            cik, statement_type, form_type, from_concept, to_concept
        )

        # 4) Hard-delete the losing concept row (the only destructive step).
        deleted = concept_repo.collection.delete_one({"_id": loser["_id"]})
        receipt["deleted"] = bool(getattr(deleted, "deleted_count", 0))

        # 5) Drop stale in-process concept-id cache entries for the loser.
        self._invalidate_concept_cache(cik, statement_type, from_concept, form_type)

        logger.info(
            "Concept promotion %s -> %s for %s %s/%s: moved %s value(s), "
            "re-parented %s dimensional child(ren), deleted loser=%s",
            from_concept, to_concept, cik, statement_type, form_type,
            receipt["values_moved"], receipt["dimensional_children_moved"],
            receipt["deleted"],
        )
        return receipt

    def _move_values_collision_safe(
        self,
        value_repo: 'ValueRepository',
        loser_id: ObjectId,
        winner_id: ObjectId,
        cik: str,
    ) -> tuple[int, int]:
        """Repoint each remaining loser value onto the winner.

        When the winner already has a value for the same period (the unique
        key that would collide), the loser's row is a genuine same-period
        duplicate: the winner's row is kept and only the duplicate is removed.
        Returns ``(moved, duplicates_removed)``.
        """
        moved = 0
        dropped = 0
        for doc in list(value_repo.collection.find({"concept_id": loser_id})):
            reporting_period = doc.get("reporting_period") or {}
            query = {
                "concept_id": winner_id,
                "cik": cik,
                "dimension_value": {"$ne": True},
                "reporting_period.fiscal_year": reporting_period.get("fiscal_year"),
            }
            if reporting_period.get("quarter") is not None:
                query["reporting_period.quarter"] = reporting_period.get("quarter")
            if value_repo.collection.find_one(query):
                value_repo.collection.delete_one({"_id": doc["_id"]})
                dropped += 1
            else:
                value_repo.collection.update_one(
                    {"_id": doc["_id"]}, {"$set": {"concept_id": winner_id}}
                )
                moved += 1
        return moved, dropped

    def _repoint_concept_aliases(
        self,
        cik: str,
        statement_type: str,
        form_type: str,
        from_concept: str,
        to_concept: str,
    ) -> int:
        """Re-key the durable alias/series store after a promotion."""
        try:
            store = getattr(self, '_concept_alias_store', None)
            if store is None:
                from ..database import ConceptAliasStore
                store = ConceptAliasStore(self.db_connection)
                self._concept_alias_store = store
            return store.promote(
                cik, statement_type, form_type, from_concept, to_concept,
                reason="newest-period promotion",
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not break the merge
            logger.warning(
                "Could not repoint concept aliases %s -> %s: %s",
                from_concept, to_concept, exc,
            )
            return 0

    def _invalidate_concept_cache(
        self, cik: str, statement_type: str, concept: str, form_type: str
    ) -> None:
        """Forget any cached concept id for a name that was just promoted away."""
        try:
            self.concept_cache.pop(
                (ConceptKey(cik, statement_type, concept), form_type), None
            )
        except Exception:  # noqa: BLE001 — cache hygiene only
            pass

    def _create_concept(self, cik: str, statement_type: str, item: dict, form_type: str = '10-K') -> ObjectId:
        """
        Create a new concept document with taxonomy-based label and proper hierarchy placement.
        Ensures concepts are fitted into hierarchy correctly and preserves abstract concepts for hierarchy.
        """
        if item.get("concept", "").split(":")[-1].endswith("Abstract"):
            return None
        # custom: grouping headers (e.g. custom:ProductSegmentation) are allowed through
        # with abstract=True — they are NOT structural XBRL noise.
        is_custom_grouping = item.get("concept", "").startswith("custom:")
        if not is_custom_grouping and item.get("abstract"):
            return None
        if not is_custom_grouping:
            item['abstract'] = False

        # Use taxonomy label only if taxonomy manager is enabled
        if self.taxonomy_manager:
            taxonomy_label = lookup_concept_label(item['concept'])
            # If taxonomy label is not found, use the label from the source database if available
            if taxonomy_label == item['concept'] and 'label' in item and item['label']:
                taxonomy_label = item['label']
        else:
            # Use the label from the source database if available, otherwise use concept name
            taxonomy_label = item.get('label', item['concept'])

        # Never persist XBRL presentation tags ("[Member]", "[Text Block]" ...).
        taxonomy_label = clean_label(taxonomy_label) or item['concept']

        # custom: grouping headers are stored with abstract=True; everything else is False.
        is_abstract = bool(is_custom_grouping and item.get('abstract'))

        # Compute the cross-era canonical concept name so equivalent concepts
        # (e.g. ASC 606 revenue, continuing-operations cash-flow variants) form a
        # single continuous time series for downstream consumers.
        canonical = canonical_concept(item['concept'])

        # Ensure proper hierarchy placement by preserving path and order_key
        concept_doc = ConceptDocument(
            company_cik=cik,
            statement_type=statement_type,
            concept=item['concept'],
            form_type=form_type,  # Add form_type to the document
            label=taxonomy_label,  # Use taxonomy-based label or fallback
            canonical_concept=canonical,  # Cross-era canonical name
            path=item.get('path'),  # Preserve hierarchy path for correct placement
            order_key=item.get('order_key'),  # Preserve order for correct hierarchy position
            abstract=is_abstract,  # True for custom: grouping headers, False for all others
            dimension=item.get('dimension', False)
        )

        
        # Get the appropriate concept repository based on form type
        concept_repo = self._get_concept_repo_by_form_type(form_type)
        
        # Use DuplicatePreventionManager with the specific repository for this form type
        duplicate_manager = DuplicatePreventionManager(self.config, concept_repo)
        concept_id = duplicate_manager.safe_insert_concept(concept_doc)
        
        if concept_id is None:
            # Fallback to finding existing concept
            existing = concept_repo.find_existing(
                cik, 
                statement_type, 
                item['concept'],
                dimension_concept=item.get('dimension', False)
            )
            if existing:
                logger.warning(f"Using existing concept for {item['concept']} after failed creation")
                return existing['_id']
            else:
                raise ValueError(f"Failed to create or find concept {item['concept']}")
        
        logger.debug(f"Successfully created new concept: {item['concept']} with hierarchy path: {item.get('path', 'N/A')}")
        return concept_id

    def _create_value_record(self, concept_id: ObjectId, statement, filing, item: dict, value: float, is_calculated: bool = False, replace_existing: bool = False) -> Optional[ObjectId]:
        """
        Create a value record for a specific time period - with sync behavior (only insert if not exists).
        
        SYNC BEHAVIOR FOR REPROCESSING:
        - If value already exists: SKIP IT (don't duplicate)
        - If value doesn't exist: INSERT IT
        
        This ensures that when reprocessing a statement after deleting values,
        only the new/missing values are added without duplicating existing ones.

        Returns the ``_id`` of the existing or newly inserted value document so
        callers (the persistence/validation agent) can reference the row.
        """
        # Extract period information from the item
        period_info = self._extract_period_info_from_item(item)
        
        # Clean reporting_period — keep only canonical fields
        _ALLOWED_RP_KEYS = {'end_date', 'period_date', 'fiscal_year', 'quarter'}
        clean_reporting_period = {
            k: v for k, v in (statement.reporting_period or {}).items()
            if k in _ALLOWED_RP_KEYS
        }
        # Remove quarter for annual filings (10-K)
        if filing.form_type == '10-K':
            clean_reporting_period.pop('quarter', None)

        # Get the appropriate value repository based on form type
        value_repo = self._get_value_repo_by_form_type(filing.form_type)
        
        # Dedup on EXACTLY the DB unique index key:
        #   quarterly (cik, concept_id, reporting_period.fiscal_year, .quarter)
        #   annual    (cik, concept_id, reporting_period.fiscal_year)
        #   (quarter is already popped for 10-K above, matching the annual index)
        # Any EXTRA filter (statement_type / form_type / calculated /
        # reporting_period.period_date) can miss a row the index considers
        # identical, so the insert then fails with E11000 DuplicateKeyError.
        # The repository finder encodes this key — always use it.
        existing_value = value_repo.find_existing_value(
            concept_id,
            clean_reporting_period,
            dimension_value=False,
            company_cik=statement.company_cik,
        )
        
        if existing_value:
            # A repaired bundle is explicitly allowed to update the existing
            # period value. Ordinary reprocessing remains insert/skip-only.
            if item.get('repaired'):
                repair_fields = {
                    'value': value,
                    'source': item.get('source', 'deterministic_identity'),
                    'corrected_from': item.get('corrected_from'),
                    'correction_reason': item.get('correction_reason'),
                    'correction_source': item.get('correction_source', item.get('source')),
                    'corrected_at': datetime.utcnow(),
                }
                if filing.accession_number and not existing_value.get('accession_number'):
                    repair_fields['accession_number'] = filing.accession_number
                value_repo.collection.update_one(
                    {'_id': existing_value['_id']},
                    {'$set': repair_fields}
                )
                logger.info(
                    "Updated repaired value for concept %s: %s -> %s",
                    concept_id, item.get('corrected_from'), value,
                )
            # --reload: the period is being replaced, so overwrite in place.
            # Checked BEFORE the accession backfill, because a replace also
            # refreshes the accession number.
            # (The DB allows only ONE row per period, so an update — not a
            # second insert — is the only correct way to replace it.)
            elif replace_existing:
                replace_fields = {
                    'value': value,
                    'reporting_period': clean_reporting_period,
                    'source': item.get('source'),
                    'calculated': is_calculated,
                    'fact_id': item.get('fact_id'),
                    'decimals': item.get('decimals'),
                }
                if filing.accession_number:
                    replace_fields['accession_number'] = filing.accession_number
                value_repo.collection.update_one(
                    {'_id': existing_value['_id']},
                    {'$set': {k: v for k, v in replace_fields.items() if v is not None}},
                )
                logger.info(
                    "Reload: replaced value for concept %s (period %s)",
                    concept_id, clean_reporting_period.get('period_date', 'unknown'),
                )
            # If value exists but is missing accession_number, update it
            elif filing.accession_number and not existing_value.get('accession_number'):
                logger.debug(f"Updating existing value with missing accession_number for concept {concept_id}, period {clean_reporting_period.get('period_date', 'unknown')}")
                value_repo.collection.update_one(
                    {'_id': existing_value['_id']},
                    {'$set': {'accession_number': filing.accession_number}}
                )
            else:
                logger.debug(f"Value already exists for concept {concept_id}, period {clean_reporting_period.get('period_date', 'unknown')}, skipping insertion")
            return existing_value['_id']
        
        # Extract metadata from metadata-only dimensional facts
        metadata_from_dimensional_facts = self._extract_metadata_from_dimensional_facts(item)
        
        value_doc = ValueDocument(
            concept_id=concept_id,
            company_cik=statement.company_cik,
            statement_type=statement.statement_type,
            form_type=filing.form_type,
            reporting_period=clean_reporting_period,
            value=value,
            created_at=statement.created_at,
            fact_id=item.get('fact_id'),  # Preserve fact_id for auditing
            decimals=item.get('decimals') or metadata_from_dimensional_facts.get('decimals'),  # Preserve decimals from item or dimensional facts
            source=item.get('source'),
            accession_number=filing.accession_number,
        )
        
        # Add calculated field to distinguish direct vs derived data
        value_doc_dict = value_doc.to_dict()
        value_doc_dict['calculated'] = is_calculated
        if filing.accession_number:
            value_doc_dict['accession_number'] = filing.accession_number
        if item.get('repaired'):
            value_doc_dict.update({
                'corrected_from': item.get('corrected_from'),
                'correction_reason': item.get('correction_reason'),
                'correction_source': item.get('correction_source', item.get('source')),
                'corrected_at': datetime.utcnow(),
            })
        
        # Insert the value (only new data)
        insert_result = value_repo.collection.insert_one(value_doc_dict)
        logger.debug(f"Inserted new value for concept {concept_id}, period {clean_reporting_period.get('period_date', 'unknown')}")
        
        # Validate critical data preservation
        if item.get('fact_id') and not value_doc.fact_id:
            logger.warning(f"CRITICAL: Lost fact_id during value processing for concept_id {concept_id}")
        if item.get('decimals') and not value_doc.decimals:
            logger.warning(f"CRITICAL: Lost decimals during value processing for concept_id {concept_id}")
        
        # Log when we preserve metadata from dimensional facts
        if metadata_from_dimensional_facts:
            logger.debug(f"Preserved metadata from dimensional facts for {item.get('concept', 'unknown')}: {metadata_from_dimensional_facts}")

        return insert_result.inserted_id
    
    def _extract_period_info_from_item(self, item: dict) -> dict:
        """Extract period information from a financial data item."""
        period_info = {}
        
        if 'period' in item:
            period_info['item_period'] = item['period']
        
        if 'context_id' in item:
            period_info['context_id'] = item['context_id']
        
        if 'unit' in item:
            period_info['unit'] = item['unit']
        
        return period_info

    def _extract_metadata_from_dimensional_facts(self, item: dict) -> dict:
        """Extract metadata from metadata-only dimensional facts."""
        metadata = {}
        
        if 'dimensional_facts' not in item:
            return metadata
            
        dimensional_facts = item.get('dimensional_facts', [])
        if not isinstance(dimensional_facts, list):
            return metadata
        
        for dimension_data in dimensional_facts:
            if not isinstance(dimension_data, dict):
                continue
                
            # Check if this is a metadata-only fact (no dimensions or value)
            has_dimensions = bool(dimension_data.get('dimensions') or dimension_data.get('dimension_details'))
            has_value = 'value' in dimension_data
            
            if not has_dimensions and not has_value:
                # Extract all metadata from this fact
                for key, value in dimension_data.items():
                    if key not in ['dimensions', 'dimension_details', 'value'] and value is not None:
                        # Use the first occurrence of each metadata field
                        if key not in metadata:
                            metadata[key] = value
        
        return metadata

    def _validate_no_fact_loss(self, item: dict, processed_dimensional_count: int, processed_metadata_count: int) -> None:
        """Validate that no dimensional facts are being lost during processing."""
        if 'dimensional_facts' not in item:
            return
            
        dimensional_facts = item.get('dimensional_facts', [])
        if not isinstance(dimensional_facts, list):
            return
        
        total_facts = len(dimensional_facts)
        total_processed = processed_dimensional_count + processed_metadata_count
        
        if total_processed < total_facts:
            lost_count = total_facts - total_processed
            logger.warning(f"POTENTIAL DATA LOSS: {lost_count} dimensional facts may have been lost for concept {item.get('concept', 'unknown')}. "
                          f"Total: {total_facts}, Processed: {total_processed} (dimensional: {processed_dimensional_count}, metadata: {processed_metadata_count})")
        elif total_processed == total_facts:
            logger.debug(f"All {total_facts} dimensional facts processed for concept {item.get('concept', 'unknown')} "
                        f"(dimensional: {processed_dimensional_count}, metadata: {processed_metadata_count})")

    def _group_statements_by_company(self) -> Dict[str, list]:
        """Group financial statements by company CIK."""
        statements_by_company = {}
        
        for statement in self.financial_repo.find_all():
            company_cik = statement.company_cik
            if company_cik not in statements_by_company:
                statements_by_company[company_cik] = []
            statements_by_company[company_cik].append(statement)
        
        logger.info(f"Found statements for {len(statements_by_company)} companies")
        return statements_by_company
    
    def _process_company_statements(self, company_cik: str, statements: list) -> None:
        """Process all statements for a single company."""
        logger.info(f"Processing {len(statements)} statements for company {company_cik}")
        
        for statement in statements:
            logger.debug(f"Processing statement {statement.id} for company {company_cik}")
            
            try:
                self._process_financial_statement(statement)
                
            except Exception as e:
                logger.error(f"Error processing statement {statement.id}: {e}")
                continue
    
    def _process_company_statements_with_tracker(self, company_cik: str, unprocessed_statements: List[FinancialStatement]) -> None:
        """Process unprocessed statements for a single company using database tracker."""
        logger.debug(f"Processing {len(unprocessed_statements)} unprocessed statements for company {company_cik}")
        
        # Guarantee the company document exists in target before writing any statement data.
        # This is a no-op when the doc is already there, and never interrupts processing on failure.
        self._ensure_company_in_target(company_cik)
        
        # Use progress bar for statement processing if there are many statements
        statements_iterable = unprocessed_statements
        if len(unprocessed_statements) > 5:  # Only show progress bar if more than 5 statements
            statements_iterable = progress_wrapper(
                unprocessed_statements,
                desc=f"Company {company_cik} Statements",
                disable=False
            )
        
        for statement in statements_iterable:
            logger.debug(f"Processing statement {statement.id} for company {company_cik}")
            
            try:
                # Process the statement using existing normalization logic
                self._process_financial_statement(statement)
                
                # Mark as processed in the tracker
                self.db_tracker.mark_statement_processed(statement)
                
                # Log progress periodically
                status = self.db_tracker.get_company_status(company_cik)
                if status:
                    logger.debug(f"Company {company_cik} progress: {status.processing_percentage:.1f}%")
                
            except Exception as e:
                logger.error(f"Error processing statement {statement.id}: {e}", exc_info=True)
                continue
        
        # Show completion status for this company
        status = self.db_tracker.get_company_status(company_cik)
        if status:
            # Use tqdm.write to properly display completion message without interfering with progress bars
            try:
                from tqdm import tqdm
                tqdm.write(f"📊 ✅ Company {company_cik} completed ({status.processing_percentage:.1f}% processed)")
            except ImportError:
                self.status_logger.info(f"✅ Company {company_cik} completed ({status.processing_percentage:.1f}% processed)")
    
    
    def show_processing_summary(self) -> None:
        """Display current processing summary using database tracker."""
        if not hasattr(self, 'db_tracker'):
            logger.warning("Database tracker not initialized. Call normalize_data() first.")
            return
            
        self.db_tracker.refresh_cache()
        self.db_tracker.log_summary()
    
    def get_companies_needing_processing(self) -> List[str]:
        """Get list of companies that still need processing."""
        if not hasattr(self, 'db_tracker'):
            logger.warning("Database tracker not initialized. Call normalize_data() first.")
            return []
            
        self.db_tracker.refresh_cache()
        return self.db_tracker.get_companies_to_process()
    
    def process_specific_company(self, company_cik: str) -> None:
        """Process a specific company using database tracker."""
        if not hasattr(self, 'db_tracker'):
            logger.warning("Database tracker not initialized. Call normalize_data() first.")
            return
            
        logger.info(f"Processing specific company: {company_cik}")
        
        try:
            self.db_tracker.refresh_cache()
            unprocessed_statements = self.db_tracker.get_unprocessed_statements_for_company(company_cik)
            
            if not unprocessed_statements:
                logger.info(f"Company {company_cik} is already fully processed")
                return
                
            self._process_company_statements_with_tracker(company_cik, unprocessed_statements)
            
            # Show final status for this company
            status = self.db_tracker.get_company_status(company_cik)
            if status:
                logger.info(f"Company {company_cik} processing completed. Final progress: {status.processing_percentage:.1f}%")
                
        except Exception as e:
            logger.error(f"Error processing company {company_cik}: {e}")
            raise

    def _sync_missing_accession_numbers(self, company_cik: str) -> Dict[str, int]:
        """
        Find and update value records that are missing accession_numbers.
        This handles cases where data was normalized before accession_number feature was added.
        """
        stats = {'checked_count': 0, 'updated_count': 0}
        
        try:
            # Check both annual and quarterly collections
            for collection_name, value_repo in [
                ('concept_values_annual', self.annual_value_repo),
                ('concept_values_quarterly', self.quarterly_value_repo)
            ]:
                # Find values missing the canonical top-level accession_number.
                # Older records may have it nested under reporting_period; those
                # are also normalized to the top-level field below.
                missing_accession_query = {
                    'cik': company_cik,
                    'accession_number': {'$exists': False}
                }
                
                values_missing_accession = list(value_repo.collection.find(missing_accession_query))
                stats['checked_count'] += len(values_missing_accession)
                
                if values_missing_accession:
                    logger.info(f"Found {len(values_missing_accession)} values missing accession_number in {collection_name}")
                
                # Create a mapping of period+statement_type to filing for efficiency
                period_to_filing_map = {}
                
                # Get all financial statements for this company to create the mapping
                source_statements = self.financial_repo.collection.find({
                    'cik': company_cik
                })
                
                for stmt in source_statements:
                    reporting_period = stmt.get('reporting_period', {})
                    if isinstance(reporting_period, dict):
                        period_key = (
                            stmt.get('statement_type'),
                            reporting_period.get('fiscal_year'),
                            reporting_period.get('period_date')
                        )
                        
                        # Get filing for this statement
                        filing_id = stmt.get('filing_id')
                        if filing_id and period_key not in period_to_filing_map:
                            filing = self.filing_repo.find_by_id(filing_id)
                            if filing and filing.accession_number:
                                period_to_filing_map[period_key] = filing.accession_number
                
                logger.debug(f"Created period-to-filing mapping with {len(period_to_filing_map)} entries")
                
                # Update values with missing accession numbers
                for value_record in values_missing_accession:
                    try:
                        reporting_period = value_record.get('reporting_period', {})
                        period_key = (
                            value_record.get('statement_type'),
                            reporting_period.get('fiscal_year'),
                            reporting_period.get('period_date')
                        )
                        
                        accession_number = period_to_filing_map.get(period_key)
                        
                        if accession_number:
                            # Update the value record with accession_number
                            update_result = value_repo.collection.update_one(
                                {'_id': value_record['_id']},
                                {'$set': {'accession_number': accession_number}}
                            )
                            
                            if update_result.modified_count > 0:
                                stats['updated_count'] += 1
                                logger.debug(f"Added accession_number {accession_number} to value record {value_record['_id']}")
                        else:
                            logger.debug(f"No accession_number found for period {period_key}")
                    
                    except Exception as e:
                        logger.warning(f"Could not add accession_number to value record {value_record.get('_id', 'unknown')}: {e}")
                        continue
        
        except Exception as e:
            logger.error(f"Error syncing accession numbers for company {company_cik}: {e}")
        
        return stats

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
    
    def _extract_time_period_values(self, item: dict) -> Dict[str, float]:
        """Extract time-period values from financial data item - NEW STRUCTURE: single value with period."""
        time_period_values = {}
        
        # In the new structure, each item has a single 'value' field and a 'period' field
        if 'value' in item and item['value'] is not None and 'period' in item:
            period = item['period']
            value = item['value']
            
            # Handle different value types
            if isinstance(value, (int, float)):
                # Extract end date from period string (e.g., "2024-12-29 00:00:00 to 2025-03-30 00:00:00")
                period_key = self._extract_period_key_from_period_string(period)
                if period_key:
                    time_period_values[period_key] = float(value)
            elif isinstance(value, str):
                # Try to convert string numbers to float
                try:
                    period_key = self._extract_period_key_from_period_string(period)
                    if period_key:
                        time_period_values[period_key] = float(value)
                except ValueError:
                    logger.debug(f"Skipping non-numeric value for period '{period}': {value}")
        
        return time_period_values
    
    def _extract_period_key_from_period_string(self, period: str) -> str:
        """Extract period key from period string like '2024-12-29 00:00:00 to 2025-03-30 00:00:00'."""
        import re
        
        # For period ranges, extract the end date
        if ' to ' in period:
            end_part = period.split(' to ')[1].strip()
            # Extract date part (YYYY-MM-DD) from datetime string
            match = re.match(r'^(\d{4}-\d{2}-\d{2})', end_part)
            if match:
                return match.group(1)
        
        # For single dates, extract the date part
        match = re.match(r'^(\d{4}-\d{2}-\d{2})', period.strip())
        if match:
            return match.group(1)
        
        # Fallback: return the original period if no date pattern found
        return period

    def process_quarterly_calculations(self) -> None:
        """Process quarterly financial calculations for all companies."""
        logger.info("Starting quarterly financial calculations...")
        
        try:
            self.quarterly_service.process_all_companies()
            logger.info("Quarterly financial calculations completed successfully")
            
        except Exception as e:
            logger.error(f"Error during quarterly calculations: {e}")
            raise

    def generate_period_report(self, company_cik: str, fiscal_year: int) -> Dict:
        """Generate period calculation report for a specific company and year."""
        return self.quarterly_service.generate_period_report(company_cik, fiscal_year)

    def _process_cashflow_statement_with_normalization(self, statement, filing) -> None:
        """Process cashflow statement with cumulative-to-individual normalization."""
        # For cashflow statements, we need to normalize cumulative quarterly data to individual quarters
        # This integrates the quarterly service logic directly into the main normalization
        
        # Build hierarchy data for cashflow
        hierarchy_data = self.hierarchy_manager.build_hierarchy_data(statement.financial_data)
        
        # Group concepts by their base name (without time periods)
        concepts_by_name = {}
        for item in hierarchy_data:
            concept_name = item.get('concept')
            if concept_name:
                if concept_name not in concepts_by_name:
                    concepts_by_name[concept_name] = []
                concepts_by_name[concept_name].append(item)
        
        # Process each concept across all time periods for normalization
        for concept_name, concept_items in concepts_by_name.items():
            self._process_cashflow_concept_normalization(
                statement, filing, concept_name, concept_items
            )

    def _process_cashflow_concept_normalization(self, statement, filing, concept_name: str, concept_items: list) -> None:
        """Normalize a single cashflow concept across time periods."""
        # Extract all time periods and values for this concept
        all_periods = {}
        concept_item = concept_items[0]  # Use first item for concept metadata
        
        for item in concept_items:
            time_period_values = self._extract_time_period_values(item)
            all_periods.update(time_period_values)
        
        if not all_periods:
            return
        
        # Get or create the concept
        concept_id = self._get_or_create_concept(
            statement.company_cik,
            statement.statement_type,
            concept_item,
            filing.form_type  # Pass form_type for repository selection
        )
        
        # Group periods by fiscal year and quarter vs annual
        periods_by_year = {}
        annual_periods = {}
        
        for period_key, value in all_periods.items():
            # Parse period to determine if it's quarterly or annual
            fiscal_year, quarter = self._parse_period_key(period_key)
            
            if quarter:  # Quarterly data
                if fiscal_year not in periods_by_year:
                    periods_by_year[fiscal_year] = {}
                periods_by_year[fiscal_year][quarter] = value
            else:  # Annual data
                annual_periods[fiscal_year] = value
        
        # Process each fiscal year
        for fiscal_year, quarterly_data in periods_by_year.items():
            annual_value = annual_periods.get(fiscal_year)
            
            # Normalize cumulative quarterly data to individual quarters
            individual_quarters = self._normalize_cashflow_quarters(
                quarterly_data, annual_value or 0.0, fiscal_year
            )
            
            # Save normalized individual quarterly values
            for quarter, individual_value in individual_quarters.items():
                # Create a normalized period key
                normalized_period_key = f"{fiscal_year}-Q{quarter}"
                
                # Create value record with normalized individual quarter value
                self._create_normalized_cashflow_value_record(
                    concept_id=concept_id,
                    statement=statement,
                    filing=filing,
                    fiscal_year=fiscal_year,
                    quarter=quarter,
                    value=individual_value,
                    is_calculated=True  # Mark as calculated since it's normalized
                )

    def _normalize_cashflow_quarters(self, quarterly_data: dict, annual_value: float, fiscal_year: int) -> dict:
        """Apply cashflow normalization logic: convert cumulative to individual quarters."""
        # quarterly_data format: {1: cumulative_q1, 2: cumulative_q2, 3: cumulative_q3, ...}
        individual_quarters = {}
        
        # Sort quarters to process in order
        available_quarters = sorted(quarterly_data.keys())
        
        # Q1: Use as-is (already individual quarter)
        if 1 in quarterly_data:
            individual_quarters[1] = quarterly_data[1]
        
        # Q2: Q2_individual = Q2_cumulative - Q1_individual
        if 2 in quarterly_data:
            q1_value = individual_quarters.get(1, 0)
            individual_quarters[2] = quarterly_data[2] - q1_value
        
        # Q3: Q3_individual = Q3_cumulative - (Q1_individual + Q2_individual)
        if 3 in quarterly_data:
            q1_value = individual_quarters.get(1, 0)
            q2_value = individual_quarters.get(2, 0)
            individual_quarters[3] = quarterly_data[3] - (q1_value + q2_value)
        
        return individual_quarters

    def _parse_period_key(self, period_key: str) -> tuple:
        """Parse period key to extract fiscal year and quarter."""
        # Period keys are typically like "2024-03-31", "2024-06-30", etc.
        # We need to map these to fiscal year and quarter
        
        try:
            from datetime import datetime
            
            # Extract date from period key
            date_str = period_key.split()[0] if ' ' in period_key else period_key
            period_date = datetime.strptime(date_str, '%Y-%m-%d')
            
            fiscal_year = period_date.year
            month = period_date.month
            
            # Map month to quarter (approximate)
            if month in [1, 2, 3]:
                quarter = 1
            elif month in [4, 5, 6]:
                quarter = 2
            elif month in [7, 8, 9]:
                quarter = 3
            elif month in [10, 11, 12]:
                quarter = 4
            else:
                quarter = None
            
            return fiscal_year, quarter
            
        except (ValueError, AttributeError):
            # If parsing fails, assume it's annual data
            try:
                # Try to extract just the year
                fiscal_year = int(period_key[:4])
                return fiscal_year, None
            except:
                logger.warning(f"Could not parse period key: {period_key}")
                return None, None

    def _create_normalized_cashflow_value_record(self, concept_id: ObjectId, statement, filing, 
                                               fiscal_year: int, quarter: int, value: float, 
                                               is_calculated: bool = True) -> None:
        """Create a value record for normalized cashflow data - with sync behavior (only insert if not exists)."""
        # Create reporting period for the normalized quarter (canonical fields only)
        end_date = self._get_quarter_end_date(fiscal_year, quarter)
        reporting_period = {
            'fiscal_year': fiscal_year,
            'quarter': quarter,
            'period_date': end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date),
            'end_date': end_date,
        }

        # Get the appropriate value repository based on form_type
        value_repo = self._get_value_repo_by_form_type(filing.form_type)
        
        # Check if value already exists to implement sync behavior (only insert new data)
        existing_query = {
            'concept_id': concept_id,
            'cik': statement.company_cik,
            'statement_type': statement.statement_type,
            'form_type': filing.form_type,
            'reporting_period.fiscal_year': fiscal_year,
            'reporting_period.quarter': quarter,
            'calculated': is_calculated
        }
        
        existing_value = value_repo.collection.find_one(existing_query)
        
        if existing_value:
            # If value exists but is missing accession_number, update it
            if filing.accession_number and not existing_value.get('accession_number'):
                logger.debug(f"Updating existing normalized cashflow value with missing accession_number for concept {concept_id}, Q{quarter} {fiscal_year}")
                value_repo.collection.update_one(
                    {'_id': existing_value['_id']},
                    {'$set': {'accession_number': filing.accession_number}}
                )
            else:
                logger.debug(f"Normalized cashflow value already exists for concept {concept_id}, Q{quarter} {fiscal_year}, skipping insertion")
            return
        
        from ..core.models import ValueDocument
        from datetime import datetime
        
        value_doc = ValueDocument(
            concept_id=concept_id,
            company_cik=statement.company_cik,
            statement_type=statement.statement_type,
            form_type=filing.form_type,
            reporting_period=reporting_period,
            value=value,
            created_at=datetime.now(),
            accession_number=filing.accession_number,
        )
        
        # Add calculated field to distinguish normalized data
        value_doc_dict = value_doc.to_dict()
        value_doc_dict['calculated'] = is_calculated
        if filing.accession_number:
            value_doc_dict['accession_number'] = filing.accession_number
        
        # Insert the normalized value using the appropriate repository based on form_type
        value_repo.collection.insert_one(value_doc_dict)
        logger.debug(f"Inserted new normalized cashflow Q{quarter} {fiscal_year}: {concept_id}")

    def _get_quarter_start_date(self, fiscal_year: int, quarter: int):
        """Get start date for a quarter."""
        from datetime import datetime
        
        # Standard calendar quarters
        quarter_starts = {
            1: (1, 1),   # Q1: Jan 1
            2: (4, 1),   # Q2: Apr 1  
            3: (7, 1),   # Q3: Jul 1
            4: (10, 1)   # Q4: Oct 1
        }
        
        month, day = quarter_starts.get(quarter, (1, 1))
        return datetime(fiscal_year, month, day)

    def _get_quarter_end_date(self, fiscal_year: int, quarter: int):
        """Get end date for a quarter."""
        from datetime import datetime
        import calendar
        
        # Standard calendar quarters
        quarter_ends = {
            1: (3, 31),   # Q1: Mar 31
            2: (6, 30),   # Q2: Jun 30  
            3: (9, 30),   # Q3: Sep 30
            4: (12, 31)   # Q4: Dec 31
        }
        
        month, day = quarter_ends.get(quarter, (12, 31))
        return datetime(fiscal_year, month, day)

    def _process_dimensional_data_enhanced(self, concept_id: ObjectId, statement, filing, item: dict, dimensional_concept_mapping: Dict[tuple, ObjectId]) -> None:
        """Process dimensional values using pre-created dimensional concepts."""
        if 'dimensional_facts' not in item:
            return
        
        dimensional_facts = item['dimensional_facts']
        if not isinstance(dimensional_facts, list):
            logger.warning(f"Dimensional facts is not a list for concept {item['concept']}")
            return
        
        logger.debug(f"Processing dimensional values for {len(dimensional_facts)} dimensional concepts for concept {item['concept']}")
        
        processed_dimensional_count = 0
        processed_metadata_count = 0
        
        for dimension_data in dimensional_facts:
            if not isinstance(dimension_data, dict):
                logger.debug(f"Skipping non-dict dimensional data: {dimension_data}")
                continue
            
            # Check if this is a metadata-only dimensional fact (like {"decimals": "-6"})
            has_dimensions = bool(dimension_data.get('dimensions') or dimension_data.get('dimension_details'))
            has_value = 'value' in dimension_data
            
            if not has_dimensions and not has_value:
                # This is a metadata-only fact (like {"decimals": "-6"})
                # This metadata will be preserved with the parent concept via _extract_metadata_from_dimensional_facts
                metadata_fields = {k: v for k, v in dimension_data.items() if k not in ['dimensions', 'dimension_details', 'value']}
                if metadata_fields:
                    logger.debug(f"Found metadata-only dimensional fact for {item['concept']} (will be preserved with parent): {metadata_fields}")
                    processed_metadata_count += 1
                continue
            
            # Process dimensional facts that have dimensions (with or without values)
            try:
                # Determine segment info to find the pre-created dimensional concept
                segment_type, concept = self._determine_segment_info(dimension_data)
                
                # Skip if we couldn't determine meaningful segment info
                if segment_type == 'unknown' and concept == 'unknown':
                    logger.debug(f"Skipping dimensional fact with unknown segment info: {dimension_data}")
                    continue
                
                dimensional_concept_id = dimensional_concept_mapping.get((item['concept'], concept))
                
                if dimensional_concept_id:
                    # Only create value record if there's an actual value
                    if has_value:
                        # Create dimensional value record linking to pre-created concept
                        self._create_dimensional_value_record(
                            dimensional_concept_id=dimensional_concept_id,
                            statement=statement,
                            filing=filing,
                            dimension_data=dimension_data
                        )
                        processed_dimensional_count += 1
                    else:
                        # Log that we found dimensional metadata without value
                        metadata_fields = [k for k in dimension_data.keys() if k not in ['dimensions', 'dimension_details', 'value']]
                        if metadata_fields:
                            logger.debug(f"Found dimensional metadata without value for {item['concept']} -> {concept}: {metadata_fields}")
                        processed_dimensional_count += 1
                else:
                    logger.warning(f"Pre-created dimensional concept not found for {item['concept']} -> {concept}")
            except Exception as e:
                logger.warning(f"Error processing dimensional data for {item['concept']}: {e}")
        
        # Validate that no facts were lost
        self._validate_no_fact_loss(item, processed_dimensional_count, processed_metadata_count)

    def _process_dimensional_data(self, concept_id: ObjectId, statement, filing, item: dict) -> None:
        """Process dimensional breakdown data for a concept."""
        if 'dimensional_facts' not in item:
            return
        
        dimensional_facts = item['dimensional_facts']
        if not isinstance(dimensional_facts, list):
            logger.warning(f"Dimensional facts is not a list for concept {item['concept']}")
            return
        
        logger.debug(f"Processing {len(dimensional_facts)} dimensional facts for concept {item['concept']}")
        
        for dimension_data in dimensional_facts:
            if not isinstance(dimension_data, dict):
                logger.warning(f"Invalid dimensional data structure (not dict): {dimension_data}")
                continue
            
            # Only process if there's a value (skip metadata-only dimensional facts in legacy mode)
            if 'value' not in dimension_data:
                logger.debug(f"Skipping dimensional data without value in legacy mode: {dimension_data}")
                continue
            
            # Get or create dimensional concept
            dimensional_concept_id = self._get_or_create_dimensional_concept(
                concept_id=concept_id,
                company_cik=statement.company_cik,
                statement_type=statement.statement_type,
                dimension_data=dimension_data
            )
            
            # Create dimensional value record
            self._create_dimensional_value_record(
                dimensional_concept_id=dimensional_concept_id,
                statement=statement,
                filing=filing,
                dimension_data=dimension_data
            )

    def _get_or_create_dimensional_concept(
        self,
        concept_id: ObjectId,
        company_cik: str,
        statement_type: str,
        dimension_data: dict,
        form_type: str = '10-K',
        assigned_path: Optional[str] = None,
        assigned_order_key: Optional[str] = None,
    ) -> ObjectId:
        """
        Get existing dimensional concept or create new one using the appropriate repository based on form type.
        Always prioritizes existing concepts to avoid duplicates.
        
        SYNC BEHAVIOR FOR REPROCESSING:
        - If dimensional concept exists: REUSE IT (preserve existing, update path/order_key if assigned)
        - If dimensional concept doesn't exist: CREATE IT
        
        This ensures dimensional concepts follow the same sync pattern as regular concepts.
        """
        # Determine segment type and identifier
        segment_type, concept = self._determine_segment_info(dimension_data)
        
        # Create cache key for dimensional concepts with form type and parent concept ID
        dimensional_key = ConceptKey(company_cik, statement_type, f"dim_{concept}_{concept_id}")
        cache_key = (dimensional_key, form_type)
        
        # Get the appropriate concept repository based on form type
        concept_repo = self._get_concept_repo_by_form_type(form_type)
        
        # Check cache first
        if cache_key in self.concept_cache:
            logger.debug(f"Found dimensional concept in cache: {concept}")
            return self.concept_cache[cache_key]
        
        # Always check database first for existing dimensional concept
        context_id = dimension_data.get('context_id')
        existing = concept_repo.find_dimensional_existing(company_cik, statement_type, segment_type, concept, concept_id, context_id)
        if existing:
            dimensional_concept_id = existing['_id']
            logger.debug(f"Reusing existing dimensional concept: {concept} (ID: {dimensional_concept_id})")
            # If found, update the segment_type if it was determined differently this time
            if existing.get('segment_type') != segment_type:
                logger.debug(f"Dimensional concept {concept} found with different segment_type: "
                           f"existing={existing.get('segment_type')}, determined={segment_type}. Using existing.")
            updates = {}
            # Self-heal labels persisted before cleaning existed.
            cleaned_existing_label = clean_label(existing.get('label'))
            if cleaned_existing_label and cleaned_existing_label != existing.get('label'):
                updates['label'] = cleaned_existing_label
            if assigned_path and existing.get('path') != assigned_path:
                updates['path'] = assigned_path
                updates['hierarchy_level'] = len(assigned_path.split('.')) - 1
                updates['level'] = updates['hierarchy_level']
            if assigned_order_key and existing.get('order_key') != assigned_order_key:
                updates['order_key'] = assigned_order_key
            if updates:
                concept_repo.collection.update_one({'_id': dimensional_concept_id}, {'$set': updates})
        else:
            # Only create new dimensional concept if it doesn't exist
            logger.debug(f"Creating new dimensional concept: {concept}")
            dimensional_concept_id = self._create_dimensional_concept(
                concept_id, company_cik, statement_type, dimension_data, segment_type, concept, form_type,
                assigned_path=assigned_path, assigned_order_key=assigned_order_key,
            )
        
        # Cache the result
        self.concept_cache[cache_key] = dimensional_concept_id
        return dimensional_concept_id

    def _extract_all_dimensional_concepts(self, financial_data: list) -> tuple[Set[DimensionalConceptInfo], Dict[DimensionalConceptInfo, Dict[str, Any]]]:
        """Extract all dimensional concepts from financial data, regardless of values."""
        dimensional_concepts = set()
        dimension_data_map = {}
        
        for item in financial_data:
            if 'dimensional_facts' not in item:
                continue
                
            dimensional_facts = item['dimensional_facts']
            if not isinstance(dimensional_facts, list):
                continue
            
            for dimension_data in dimensional_facts:
                if not isinstance(dimension_data, dict):
                    continue
                
                # Skip metadata-only dimensional facts (like {"decimals": "-6"})
                has_dimensions = bool(dimension_data.get('dimensions') or dimension_data.get('dimension_details'))
                if not has_dimensions:
                    logger.debug(f"Skipping metadata-only dimensional fact during concept extraction: {dimension_data}")
                    continue
                
                try:
                    # Extract dimensional concept info
                    segment_type, concept = self._determine_segment_info(dimension_data)
                    
                    # Skip if we couldn't determine meaningful segment info
                    if segment_type == 'unknown' and concept == 'unknown':
                        logger.debug(f"Skipping dimensional fact with unknown segment info during concept extraction: {dimension_data}")
                        continue
                    
                    # CRITICAL FIX: Use concept_name from dimensional fact itself as the true parent
                    # This ensures dimensional concepts are assigned to their semantically correct parent,
                    # not just whatever array they happen to be stored in
                    true_parent_concept = dimension_data.get('concept_name', item['concept'])
                    
                    # Log when we find a mismatch (indicates potential data quality issue)
                    if true_parent_concept != item['concept']:
                        logger.debug(f"Dimensional fact parent mismatch: array container='{item['concept']}', "
                                   f"concept_name='{true_parent_concept}' - using concept_name as true parent")
                        
                    dim_info = DimensionalConceptInfo(
                        parent_concept=true_parent_concept,  # Use the explicit parent from concept_name
                        segment_type=segment_type,
                        concept=concept
                    )
                    dimensional_concepts.add(dim_info)
                    dimension_data_map[dim_info] = dimension_data
                except Exception as e:
                    logger.warning(f"Error extracting dimensional concept from {dimension_data}: {e}")
                    continue
        
        logger.info(f"Extracted {len(dimensional_concepts)} unique dimensional concepts from financial data")
        return dimensional_concepts, dimension_data_map

    def _determine_segment_info(self, dimension_data: dict) -> tuple[str, str]:
        """Determine segment type and identifier from dimensional data."""
        # Get dimensions and dimension_details from the new structure
        dimensions = dimension_data.get('dimensions', {})
        dimension_details = dimension_data.get('dimension_details', {})
        
        # If no dimensions, fall back to legacy structure
        if not dimensions:
            return self._determine_segment_info_legacy(dimension_data)
        
        # Helper function to get qualified member name from dimension_details
        def get_qualified_member_name(axis_name: str, member_name: str) -> str:
            """Get the qualified member name from dimension_details, fallback to member_name."""
            if axis_name in dimension_details:
                details = dimension_details[axis_name]
                member_qname = details.get('member_qname')
                if member_qname:
                    return member_qname
            return member_name
        
        # Filter out metadata axes to get meaningful dimensions
        meaningful_dimensions = {k: v for k, v in dimensions.items() if k != 'explicitMember'}

        # Skip aggregate/total-segment members that duplicate the consolidated value.
        # e.g. nflx:ReportableSegmentMember, srt:OperatingSegmentsMember (alone),
        # AllSegmentsMember.  These are "we only have one segment" or "sum of all segments"
        # declarations that equal the parent line-item value exactly.
        # Reconciling/elimination members (MaterialReconcilingItemsMember, EliminationsMember)
        # are internal accounting adjustments, not real segment breakdowns.
        _AGGREGATE_MEMBER_SUFFIXES = (
            'reportablesegmentmember',      # *ReportableSegmentMember - total of all reportable segs
            'reportablesegmentsmember',     # plural variant
            'allsegmentsmember',            # *AllSegmentsMember - all segments combined
            'operatingsegmentsmember',      # srt:OperatingSegmentsMember when used as sole member
            'materialsreconcilingitemsmember',  # srt:MaterialReconcilingItemsMember
            'materialreconcilingitemsmember',
            'eliminationsmember',           # srt:EliminationsMember
            'intersegmenteliminationmember',  # us-gaap:IntersegmentEliminationMember
            'intersubsegmenteliminationsmember',
        )
        all_member_values = [v for v in meaningful_dimensions.values() if isinstance(v, str)]
        if all_member_values and all(
            any(v.lower().endswith(sfx) for sfx in _AGGREGATE_MEMBER_SUFFIXES)
            for v in all_member_values
        ):
            return 'unknown', 'unknown'

        # Prioritized mapping of axis types based on actual data patterns
        segment_type = None
        concept = None
        primary_axis = None
        primary_member = None
        
        # Priority 1: Business/Geographic segments (most specific)
        for axis_name, member_name in meaningful_dimensions.items():
            axis_lower = axis_name.lower()
            if 'statementbusinesssegments' in axis_lower:
                segment_type = 'business_segment'
                concept = get_qualified_member_name(axis_name, member_name)
                primary_axis = axis_name
                primary_member = concept
                break
            elif 'statementgeographical' in axis_lower or 'geographic' in axis_lower:
                segment_type = 'geographic_segment'
                concept = get_qualified_member_name(axis_name, member_name)
                primary_axis = axis_name
                primary_member = concept
                break
        
        # Priority 2: Investment/Financial Instrument breakdowns
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if 'investmenttype' in axis_lower:
                    segment_type = 'investment_type'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
                elif 'fairvaluebyfairvaluehierarchylevel' in axis_lower:
                    segment_type = 'fair_value_level'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 3: Derivative/Risk instruments
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if 'derivativeinstrumentrisk' in axis_lower:
                    segment_type = 'derivative_risk'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 4: Equity/Comprehensive income components
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if 'statementequitycomponents' in axis_lower:
                    segment_type = 'equity_component'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
                elif 'reclassificationoutofaccumulated' in axis_lower:
                    segment_type = 'comprehensive_income'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 5: Consolidation segments
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if 'consolidationitems' in axis_lower:
                    segment_type = 'consolidation'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 6: Cash/Securities schedule breakdowns
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if ('scheduleof' in axis_lower and 
                    ('cash' in axis_lower or 'securities' in axis_lower)):
                    segment_type = 'cash_securities_schedule'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 7: Product/Service segments
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if ('product' in axis_lower or 'service' in axis_lower):
                    segment_type = 'product_service'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break
        
        # Priority 8: Nature of expense
        if not segment_type:
            for axis_name, member_name in meaningful_dimensions.items():
                axis_lower = axis_name.lower()
                if 'natureofexpense' in axis_lower:
                    segment_type = 'expense_nature'
                    concept = get_qualified_member_name(axis_name, member_name)
                    primary_axis = axis_name
                    primary_member = concept
                    break

        # Priority 9: Typed date dimensions (e.g. performance obligation timing).
        # These axes carry a raw date string as the "member" rather than a named
        # explicit member.  Build a stable, human-identifiable concept key so the
        # UI never displays a bare date as a concept name.
        if not segment_type:
            import re as _re
            _DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
            for axis_name, member_name in meaningful_dimensions.items():
                if _DATE_RE.match(str(member_name)):
                    axis_lower = axis_name.lower()
                    if 'revenueremainingperformanceobligation' in axis_lower or 'performanceobligation' in axis_lower:
                        segment_type = 'performance_obligation'
                        concept = f'us-gaap:RevenueRemainingPerformanceObligation_{member_name}'
                    else:
                        # Generic typed-date axis — derive a readable segment type
                        axis_clean = axis_name.replace('Axis', '').lower()
                        segment_type = f'typed_date_{axis_clean}' if axis_clean else 'typed_date'
                        concept = f'{axis_clean}_{member_name}' if axis_clean else member_name
                    primary_axis = axis_name
                    primary_member = concept
                    break

        # Fallback: use a deterministic primary dimension and derive a descriptive
        # segment type from its axis.  Dict iteration order is not stable across
        # runs, so an arbitrary pick here previously produced order-reversed
        # duplicate concepts for the same fact (435 such twins in production).
        def _primary_dimension_key(axis_name: str) -> tuple:
            lowered = str(axis_name).lower()
            return (
                0 if "segment" in lowered else 1,
                0 if "product" in lowered else 1,
                0 if "geograph" in lowered else 1,
                lowered,
            )

        if not segment_type and meaningful_dimensions:
            first_axis, first_member = sorted(
                meaningful_dimensions.items(), key=lambda kv: _primary_dimension_key(kv[0])
            )[0]
            
            # Create segment type from axis name
            axis_clean = first_axis.replace('Axis', '').lower()
            # Remove common prefixes to get cleaner names
            for prefix in ['statementof', 'scheduleof', 'us_gaap_', 'usgaap']:
                axis_clean = axis_clean.replace(prefix, '')
            
            segment_type = axis_clean if axis_clean else 'dimensional'
            concept = get_qualified_member_name(first_axis, first_member)
            primary_axis = first_axis
            primary_member = concept
        
        # Multi-axis facts (e.g. segment x product, segment x geography) are NOT encoded
        # into the concept name any more.  Concatenating member names produced values like
        # "meta:FamilyOfAppsMember_us-gaap:ServiceOtherMember" which
        #   * dropped the axis each member belonged to,
        #   * varied with dict iteration order (order-reversed twins in production), and
        #   * could not separate rows that share a member but differ in line item, because
        #     the row identity does not contain the parent line item.
        # The full slice is now recorded as a canonical dimension signature on the row
        # (``data_normalization_service.core.row_identity.dimension_signature``) and the row
        # is keyed by its parent, so the concept stays a clean, reusable member name.
        #
        # Don't clean up concept name - preserve the qualified names with namespaces
        # The qualified names are needed to match existing dimensional concepts in the database
        
        # Ensure we have valid values
        if not segment_type:
            segment_type = 'unknown'
        if not concept:
            concept = 'unknown'
        
        # Legacy fallback for old structure compatibility
        if segment_type == 'unknown':
            return self._determine_segment_info_legacy(dimension_data)
        
        return str(segment_type), str(concept)
    
    def _determine_segment_info_legacy(self, dimension_data: dict) -> tuple[str, str]:
        """Handle legacy dimension data structure."""
        segment_type = 'unknown'
        concept = 'unknown'
        
        if 'consolidation_id' in dimension_data or 'segment_id' in dimension_data:
            segment_type = 'business'
            concept = dimension_data.get('consolidation_id') or dimension_data.get('segment_id') or 'unknown'
        elif 'product_id' in dimension_data:
            segment_type = 'product_service'
            concept = dimension_data['product_id'] or 'unknown'
        elif dimension_data.get('segment_type') and 'concept' in dimension_data:
            segment_type = dimension_data['segment_type']
            concept = dimension_data['concept']
        elif dimension_data.get('segment_type'):
            segment_type = dimension_data['segment_type']
            concept = dimension_data.get('concept', 'unknown')
        
        return str(segment_type), str(concept)

    def _create_dimensional_concept(
        self,
        concept_id: ObjectId,
        company_cik: str,
        statement_type: str,
        dimension_data: dict,
        segment_type: str,
        concept: str,
        form_type: str = '10-K',
        assigned_path: Optional[str] = None,
        assigned_order_key: Optional[str] = None,
    ) -> ObjectId:
        """Create a new dimensional concept document using the appropriate repository based on form type."""
        # Get the appropriate concept repository based on form type
        concept_repo = self._get_concept_repo_by_form_type(form_type)
        
        # Get parent concept to fetch its path
        parent_concept = concept_repo.collection.find_one({"_id": concept_id})
        parent_concept_path = parent_concept.get('path', '') if parent_concept else ''
        
        # Generate path and order_key for this dimensional concept
        if assigned_path and assigned_order_key:
            path = assigned_path
            order_key = assigned_order_key
        else:
            path = concept_repo.generate_dimensional_path(concept_id, segment_type, parent_concept_path)
            order_key = concept_repo.get_next_dimensional_order_key(concept_id, segment_type)
        
        # Create base dimensional concept document using ConceptDocument with dimension_concept=True
        dimensional_concept_doc = ConceptDocument(
            company_cik=company_cik,
            statement_type=statement_type,
            concept=concept,
            form_type=form_type,  # Add form_type to dimensional concepts
            path=path,
            order_key=order_key,
            dimension_concept=True,
            concept_id=concept_id,  # Store parent concept ID (use correct field name in ConceptDocument)
            segment_type=segment_type
        )
        
        # Extract member_label as label and member_qname as concept from source database
        dimension_details = dimension_data.get('dimension_details', {})

        # For multi-axis facts (e.g. ConsolidationItemsAxis + StatementBusinessSegmentsAxis),
        # the `concept` parameter was already correctly identified by _determine_segment_info
        # as the semantically meaningful member (e.g. aapl:AmericasSegmentMember).
        # We must find the axis whose member_qname MATCHES `concept` so we use the right label,
        # rather than blindly taking the first axis (which may be a consolidation/wrapper axis).
        def _find_primary_axis_details() -> dict:
            """Return the dimension_details entry whose member_qname matches `concept`."""
            for _ax, _details in dimension_details.items():
                if isinstance(_details, dict) and _details.get('member_qname') == concept:
                    return _details
            # Fallback: return first axis details that has a member_qname
            for _ax, _details in dimension_details.items():
                if isinstance(_details, dict) and 'member_qname' in _details:
                    return _details
            return {}

        primary_axis_details = _find_primary_axis_details()

        # Use label from the primary (semantically correct) axis
        if primary_axis_details.get('member_label'):
            dimensional_concept_doc.label = primary_axis_details['member_label']
        elif primary_axis_details.get('member_local_name'):
            dimensional_concept_doc.label = primary_axis_details['member_local_name']
        elif 'fact_label' in dimension_data:
            dimensional_concept_doc.label = dimension_data['fact_label']
        elif 'label' in dimension_data:
            dimensional_concept_doc.label = dimension_data['label']

        # For typed-date concepts the member has no label at all; build one from
        # the fact label (e.g. "Revenue, Remaining Performance Obligation, Percentage")
        # and the date qualifier so the UI shows something meaningful.
        if not dimensional_concept_doc.label:
            import re as _re2
            _date_match = _re2.search(r'(\d{4}-\d{2}-\d{2})$', dimensional_concept_doc.concept or '')
            if _date_match:
                date_str = _date_match.group(1)
                base_label = (dimension_data.get('fact_label') or
                              dimension_data.get('label') or
                              'Revenue Remaining Performance Obligation')
                dimensional_concept_doc.label = f"{base_label} ({date_str})"

        # The `concept` parameter is already correct — do NOT overwrite it from dimension_details,
        # which would pick the wrong axis for dual-axis facts (regression fix).

        # Labels come straight from the taxonomy linkbase and carry XBRL
        # presentation tags ("Rest Of World [Member]", "Rest of Asia Pacific
        # Segment [Member]").  Clean them before the row is written.
        if dimensional_concept_doc.label:
            dimensional_concept_doc.label = clean_label(dimensional_concept_doc.label)
        
        # Store new dimensional data fields
        dimensional_fields = {
            'context_id': dimension_data.get('context_id'),
            'unit_id': dimension_data.get('unit_id'),
            'period': dimension_data.get('period'),
            'concept_name': dimension_data.get('concept_name'),
            'fact_label': dimension_data.get('fact_label'),
            'dimensions': dimension_data.get('dimensions'),
            'dimension_details': dimension_data.get('dimension_details')
        }
        
        # Set only non-null new fields
        for field_name, field_value in dimensional_fields.items():
            if field_value is not None:
                setattr(dimensional_concept_doc, field_name, field_value)

        # Parent linkage + canonical identity.  The member name alone cannot
        # identify this row (the same member is a child of Revenues and of
        # CostOfRevenue), so stamp the parent and the full identity here — the
        # hierarchy layer reads both back and must never have to guess a parent
        # from the concept name.
        dimensional_concept_doc.parent_concept = (
            parent_concept.get('concept') if parent_concept else None
        ) or dimension_data.get('concept_name')
        dimensional_concept_doc.parent_path = parent_concept_path or None
        dimensional_concept_doc.row_key = row_key_str(
            {
                "cik": company_cik,
                "form_type": form_type,
                "statement_type": statement_type,
                "period": dimension_data.get('period'),
                "concept": concept,
                "concept_id": concept_id,
                "concept_name": dimension_data.get('concept_name'),
                "dimensions": dimension_data.get('dimensions'),
                "dimension_details": dimension_data.get('dimension_details'),
            }
        )
        
        # Use the appropriate repository for insertion with duplicate prevention
        duplicate_manager = DuplicatePreventionManager(self.config, concept_repo)
        dimensional_concept_id = duplicate_manager.safe_insert_concept(dimensional_concept_doc)
        
        if dimensional_concept_id is None:
            # Fallback to finding existing with enhanced parameters
            context_id = dimension_data.get('context_id')
            existing = concept_repo.find_dimensional_existing(company_cik, statement_type, segment_type, concept, concept_id, context_id)
            if existing:
                logger.warning(f"Using existing dimensional concept for {concept} (parent: {concept_id}, context: {context_id})")
                dimensional_concept_id = existing['_id']
            else:
                raise ValueError(f"Failed to create dimensional concept: {segment_type} - {concept}")
        
        # Log creation with original concept if available
        original_concept = dimension_data.get('concept_name', concept)
        logger.debug(f"Created dimensional concept: {segment_type} - {original_concept} (path: {path}, order: {order_key})")
        return dimensional_concept_id

    def _create_dimensional_value_record(self, dimensional_concept_id: ObjectId, statement, filing, dimension_data: dict) -> None:
        """
        Create a dimensional value record with duplicate prevention.
        
        SYNC BEHAVIOR FOR REPROCESSING:
        - If dimensional value already exists: SKIP IT (don't duplicate)
        - If dimensional value doesn't exist: INSERT IT
        
        This ensures dimensional values follow the same sync pattern as regular values.
        
        Args:
            dimensional_concept_id: The ID of the dimensional concept (which is now the concept_id for the value)
            statement: Financial statement data
            filing: Filing data
            dimension_data: The dimensional data containing value, fact_id, decimals, etc.
        """
        # Clean reporting_period — keep only canonical fields (same as regular values)
        _ALLOWED_RP_KEYS = {'end_date', 'period_date', 'fiscal_year', 'quarter'}
        clean_reporting_period = {
            k: v for k, v in (statement.reporting_period or {}).items()
            if k in _ALLOWED_RP_KEYS
        }
        if filing.form_type == '10-K':
            clean_reporting_period.pop('quarter', None)

        # Check for existing dimensional value to prevent duplicates
        # Get the appropriate value repository based on form type
        value_repo = self._get_value_repo_by_form_type(filing.form_type)
        
        existing_value = value_repo.find_dimensional_existing_value(
            dimensional_concept_id, clean_reporting_period
        )
        
        if existing_value:
            # If value exists but is missing accession_number, update it
            if filing.accession_number and not existing_value.get('accession_number'):
                logger.debug(f"Updating existing dimensional value with missing accession_number for dimensional_concept_id {dimensional_concept_id}, "
                            f"period {clean_reporting_period}")
                value_repo.collection.update_one(
                    {'_id': existing_value['_id']},
                    {'$set': {'accession_number': filing.accession_number}}
                )
            else:
                logger.debug(f"Dimensional value already exists for dimensional_concept_id {dimensional_concept_id}, "
                            f"period {clean_reporting_period}")
            return
        
        dimensional_value_doc = ValueDocument(
            concept_id=dimensional_concept_id,
            company_cik=statement.company_cik,
            statement_type=statement.statement_type,
            form_type=filing.form_type,
            reporting_period=clean_reporting_period,
            value=dimension_data.get('value'),
            created_at=statement.created_at,
            dimension_value=True,
            dimensional_concept_id=dimensional_concept_id,
            accession_number=filing.accession_number,
        )
        
        value_doc_dict = dimensional_value_doc.to_dict()
        value_doc_dict['calculated'] = False
        if filing.accession_number:
            value_doc_dict['accession_number'] = filing.accession_number
        value_repo.collection.insert_one(value_doc_dict)
        logger.debug(f"Created dimensional value: {dimension_data.get('value')} for dimensional_concept_id {dimensional_concept_id}")
