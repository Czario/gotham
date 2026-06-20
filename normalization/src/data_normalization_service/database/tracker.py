"""
Database-based tracker for normalization service.
Compares source and target databases to track processing status.
"""
import logging
from typing import Dict, Set, List, Optional, Tuple, Any
from dataclasses import dataclass
from datetime import datetime
from bson import ObjectId

from ..core.models import Company, FinancialStatement
from ..utils.progress import progress_wrapper, create_progress_bar
from .database import (
    DatabaseConnection,
    FinancialStatementRepository,
    CompanyRepository,
    ConceptRepository,
    ValueRepository
)

logger = logging.getLogger(__name__)


@dataclass
class CompanyProcessingStatus:
    """Status of processing for a single company."""
    company_cik: str
    source_statements_count: int
    target_statements_count: int
    unprocessed_statements: Set[str]
    fiscal_years_processed: Set[str]
    quarters_processed: Set[Tuple[str, str]]  # (year, quarter) tuples
    
    @property
    def is_fully_processed(self) -> bool:
        """Check if all statements for this company have been processed."""
        return len(self.unprocessed_statements) == 0
    
    @property
    def processing_percentage(self) -> float:
        """Calculate processing percentage for this company."""
        if self.source_statements_count == 0:
            return 100.0
        processed_count = self.source_statements_count - len(self.unprocessed_statements)
        return (processed_count / self.source_statements_count) * 100.0


@dataclass
class ProcessingSummary:
    """Overall processing summary."""
    total_companies: int
    processed_companies: int
    companies_with_partial_processing: int
    total_source_statements: int
    total_target_statements: int
    unprocessed_statements_count: int
    
    @property
    def overall_percentage(self) -> float:
        """Calculate overall processing percentage."""
        if self.total_source_statements == 0:
            return 100.0
        return (self.total_target_statements / self.total_source_statements) * 100.0


class DatabaseTracker:
    """Database-based tracker for normalization progress."""
    
    def __init__(self, source_config, target_config, ensure_indexes: bool = True):
        """
        Initialize tracker with source and target database connections.
        
        Args:
            source_config: Source database configuration
            target_config: Target database configuration
            ensure_indexes: If True, ensure required indexes exist (default: True)
        """
        self.source_connection = DatabaseConnection(source_config)
        self.target_connection = DatabaseConnection(target_config)
        
        # Source repositories
        self.source_statement_repo = FinancialStatementRepository(self.source_connection)
        self.source_company_repo = CompanyRepository(self.source_connection)
        
        # Target repositories
        self.target_statement_repo = FinancialStatementRepository(self.target_connection)
        self.target_company_repo = CompanyRepository(self.target_connection)
        self.target_concept_repo = ConceptRepository(self.target_connection, 'normalized_concepts_annual')
        self.target_value_repo = ValueRepository(self.target_connection, 'concept_values_annual')
        
        # Get value repositories for checking processed statements
        self.annual_value_repo = ValueRepository(self.target_connection, 'concept_values_annual')
        self.quarterly_value_repo = ValueRepository(self.target_connection, 'concept_values_quarterly')
        
        # Lightweight cache for summary stats only (not full data)
        self._summary_cache: Optional[ProcessingSummary] = None
        self._cache_valid = False
        
        # Ensure indexes exist for optimal performance
        if ensure_indexes:
            self._ensure_indexes()
    
    def _ensure_indexes(self) -> None:
        """
        Ensure required indexes exist for optimal performance.
        This is safe to call multiple times - MongoDB will skip existing indexes.
        """
        from pymongo import ASCENDING, IndexModel
        
        try:
            logger.info("Ensuring MongoDB indexes exist for optimal performance...")
            
            # Source database indexes (skipped in merged pipeline where source_db_name is empty)
            if self.source_connection.config.source_db_name:
                source_db = self.source_connection.source_db
                financial_statements = source_db['financial_statements']
                
                source_indexes = [
                    IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
                    IndexModel(
                        [("company_cik", ASCENDING), ("data", ASCENDING)],
                        name="idx_company_cik_data"
                    ),
                    IndexModel(
                        [
                            ("company_cik", ASCENDING),
                            ("reporting_period.end_date", ASCENDING),
                            ("statement_type", ASCENDING)
                        ],
                        name="idx_company_period_type"
                    ),
                ]
                
                financial_statements.create_indexes(source_indexes)
                logger.debug("Source database indexes verified/created")
            
            # Target database indexes
            target_db = self.target_connection.target_db
            
            # Indexes for concept_values_annual
            annual_indexes = [
                IndexModel(
                    [
                        ("company_cik", ASCENDING),
                        ("reporting_period.end_date", ASCENDING),
                        ("statement_type", ASCENDING)
                    ],
                    name="idx_company_period_type"
                ),
                IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
            ]
            
            target_db['concept_values_annual'].create_indexes(annual_indexes)
            
            # Indexes for concept_values_quarterly
            quarterly_indexes = [
                IndexModel(
                    [
                        ("company_cik", ASCENDING),
                        ("reporting_period.end_date", ASCENDING),
                        ("statement_type", ASCENDING)
                    ],
                    name="idx_company_period_type"
                ),
                IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
            ]
            
            target_db['concept_values_quarterly'].create_indexes(quarterly_indexes)
            logger.debug("Target database indexes verified/created")
            
            logger.info("✓ MongoDB indexes ready")
            
        except Exception as e:
            # Don't fail startup if index creation fails - just warn
            logger.warning(f"Could not ensure indexes: {e}. Performance may be degraded.")
            logger.info("Run scripts/create_indexes.py to manually create indexes.")
        
    def refresh_cache(self) -> None:
        """
        Refresh the lightweight summary cache.
        Now only calculates aggregate statistics, not full company-by-company data.
        This is much faster as it uses MongoDB aggregation instead of loading all data.
        """
        logger.info("Calculating database statistics...")
        
        try:
            # Count companies - fast
            total_companies = self.source_company_repo.source_collection.count_documents({})
            
            # Count total statements with data using aggregation - fast
            total_source_statements = self.source_statement_repo.collection.count_documents({
                "data": {"$exists": True, "$nin": [[], None]}
            })
            
            # Count unique (company, statement_type, end_date) combinations in target - fast
            # This tells us how many unique statements have been processed
            pipeline = [
                {
                    "$group": {
                        "_id": {
                            "company_cik": "$company_cik",
                            "statement_type": "$statement_type",
                            "end_date": "$reporting_period.end_date"
                        }
                    }
                },
                {
                    "$count": "total"
                }
            ]
            
            # Count in both annual and quarterly collections
            annual_result = list(self.annual_value_repo.collection.aggregate(pipeline))
            quarterly_result = list(self.quarterly_value_repo.collection.aggregate(pipeline))
            
            annual_count = annual_result[0]['total'] if annual_result else 0
            quarterly_count = quarterly_result[0]['total'] if quarterly_result else 0
            total_target_statements = annual_count + quarterly_count
            
            # Get count of companies that have any data in target
            pipeline_companies = [
                {
                    "$group": {
                        "_id": "$company_cik"
                    }
                },
                {
                    "$count": "total"
                }
            ]
            
            annual_companies_result = list(self.annual_value_repo.collection.aggregate(pipeline_companies))
            quarterly_companies_result = list(self.quarterly_value_repo.collection.aggregate(pipeline_companies))
            
            annual_companies = set()
            quarterly_companies = set()
            
            if annual_companies_result:
                annual_companies = set([doc['_id'] for doc in self.annual_value_repo.collection.aggregate([{"$group": {"_id": "$company_cik"}}])])
            if quarterly_companies_result:
                quarterly_companies = set([doc['_id'] for doc in self.quarterly_value_repo.collection.aggregate([{"$group": {"_id": "$company_cik"}}])])
            
            processed_companies = len(annual_companies | quarterly_companies)
            
            # Estimate companies with partial processing
            # (Companies that have some data but might have missing statements)
            companies_with_partial_processing = 0  # We'll calculate this on-demand if needed
            
            unprocessed_statements_count = max(0, total_source_statements - total_target_statements)
            
            self._summary_cache = ProcessingSummary(
                total_companies=total_companies,
                processed_companies=processed_companies,
                companies_with_partial_processing=companies_with_partial_processing,
                total_source_statements=total_source_statements,
                total_target_statements=total_target_statements,
                unprocessed_statements_count=unprocessed_statements_count
            )
            
            self._cache_valid = True
            logger.info(f"Statistics calculated: {total_companies} companies, {total_source_statements} source statements, {total_target_statements} processed")
            
        except Exception as e:
            logger.error(f"Error calculating statistics: {e}")
            # Create a default summary on error
            self._summary_cache = ProcessingSummary(
                total_companies=0,
                processed_companies=0,
                companies_with_partial_processing=0,
                total_source_statements=0,
                total_target_statements=0,
                unprocessed_statements_count=0
            )
            self._cache_valid = True
    
    def _create_statement_identifier(self, statement: FinancialStatement) -> str:
        """Create a unique identifier for a statement for comparison purposes."""
        # Handle different reporting_period formats
        try:
            if hasattr(statement.reporting_period, 'isoformat'):
                # It's a datetime object
                period_str = statement.reporting_period.isoformat()
            elif isinstance(statement.reporting_period, dict):
                # It might be a MongoDB date dict like {'$date': '2023-01-01T00:00:00.000Z'}
                if '$date' in statement.reporting_period:
                    period_str = statement.reporting_period['$date']
                else:
                    period_str = str(statement.reporting_period)
            else:
                # Convert to string as fallback
                period_str = str(statement.reporting_period)
        except Exception as e:
            logger.warning(f"Error processing reporting_period: {e}, using fallback")
            period_str = str(statement.reporting_period)
        
        # Use a combination of available fields to identify a statement
        return f"{statement.company_cik}_{statement.statement_type}_{period_str}_{str(statement.id)}"
    
    def get_companies_to_process(self) -> List[str]:
        """
        Get list of company CIKs that need processing.
        Uses efficient aggregation to find companies with unprocessed statements.
        """
        logger.info("Finding companies with unprocessed statements...")
        
        try:
            # Get all company CIKs from source that have statements with financial data
            source_companies_with_data = list(self.source_statement_repo.collection.aggregate([
                {
                    "$match": {
                        "data": {"$exists": True, "$nin": [[], None]}
                    }
                },
                {
                    "$group": {
                        "_id": "$company_cik"
                    }
                }
            ]))
            
            source_ciks = {doc['_id'] for doc in source_companies_with_data}
            
            # Get all company CIKs that have been processed (have values in target)
            processed_ciks_annual = set([doc['_id'] for doc in self.annual_value_repo.collection.aggregate([
                {"$group": {"_id": "$company_cik"}}
            ])])
            
            processed_ciks_quarterly = set([doc['_id'] for doc in self.quarterly_value_repo.collection.aggregate([
                {"$group": {"_id": "$company_cik"}}
            ])])
            
            all_processed_ciks = processed_ciks_annual | processed_ciks_quarterly
            
            # Companies to process = companies in source with data but not fully in target
            # For simplicity, we include any company that has source data
            # The get_unprocessed_statements_for_company will filter which statements need processing
            companies_to_process = list(source_ciks)
            
            logger.info(f"Found {len(companies_to_process)} companies with financial data in source")
            logger.info(f"{len(all_processed_ciks)} companies have some processed data in target")
            
            return companies_to_process
            
        except Exception as e:
            logger.error(f"Error finding companies to process: {e}")
            # Fallback: get all companies from source
            return [c.cik for c in self.source_company_repo.find_all_source()]
    
    def get_unprocessed_statements_for_company(self, company_cik: str) -> List[FinancialStatement]:
        """
        Get unprocessed statements for a specific company.
        
        Returns all statements from source that:
        1. Have financial data (non-empty)
        2. Don't exist in target database (based on end_date + statement_type match)
        
        This ensures we process all source statements as they exist.
        Optimized for scalability with thousands of companies.
        """
        # Query source statements for THIS company only (not all companies!)
        source_statements = list(self.source_statement_repo.collection.find(
            {
                "company_cik": company_cik,
                "data": {"$exists": True, "$nin": [[], None]}
            }
        ))
        
        if not source_statements:
            logger.debug(f"No source statements found for company {company_cik}")
            return []
        
        # Convert to FinancialStatement objects
        source_statements_objects = [FinancialStatement.from_dict(doc) for doc in source_statements]
        
        # Get repositories for checking target
        annual_value_repo = ValueRepository(self.target_connection, 'concept_values_annual')
        quarterly_value_repo = ValueRepository(self.target_connection, 'concept_values_quarterly')
        
        # Filter statements
        unprocessed_statements = []
        empty_statements_count = 0
        already_processed_count = 0
        
        for stmt in source_statements_objects:
            # Skip statements with no financial data
            if not stmt.financial_data or len(stmt.financial_data) == 0:
                empty_statements_count += 1
                logger.debug(
                    f"Skipping empty statement for company {company_cik}: "
                    f"{stmt.statement_type} - {stmt.reporting_period} (no financial data)"
                )
                continue
            
            # Check if statement already processed
            try:
                statement_date = None
                if hasattr(stmt.reporting_period, 'isoformat'):
                    statement_date = stmt.reporting_period
                elif isinstance(stmt.reporting_period, dict) and 'end_date' in stmt.reporting_period:
                    statement_date = stmt.reporting_period['end_date']
                
                if statement_date:
                    query = {
                        "company_cik": company_cik,
                        "reporting_period.end_date": statement_date,
                        "statement_type": stmt.statement_type
                    }
                    
                    # Check both collections
                    annual_count = annual_value_repo.collection.count_documents(query, limit=1)
                    quarterly_count = quarterly_value_repo.collection.count_documents(query, limit=1)
                    
                    if annual_count > 0 or quarterly_count > 0:
                        already_processed_count += 1
                        logger.debug(
                            f"Statement already processed for company {company_cik}: "
                            f"{stmt.statement_type} - {statement_date}"
                        )
                        continue
                
                # Statement has data and not yet processed - add it
                unprocessed_statements.append(stmt)
                
            except Exception as e:
                logger.warning(f"Error checking statement status: {e}, including it in unprocessed list")
                # If we can't check, include it to be safe
                unprocessed_statements.append(stmt)
        
        # Log summary
        if empty_statements_count > 0:
            logger.info(
                f"Filtered out {empty_statements_count} empty statement(s) for company {company_cik} "
                f"(placeholder records with no financial data)"
            )
        
        if already_processed_count > 0:
            logger.info(
                f"Skipped {already_processed_count} already processed statement(s) for company {company_cik} "
                f"(SYNC behavior - avoiding duplicates)"
            )
        
        if unprocessed_statements:
            logger.info(
                f"Found {len(unprocessed_statements)} unprocessed statement(s) with data for company {company_cik}"
            )
        else:
            logger.info(f"No unprocessed statements found for company {company_cik}")
        
        return unprocessed_statements
    
    def get_company_status(self, company_cik: str) -> Optional[CompanyProcessingStatus]:
        """
        Deprecated. Returns None - use get_unprocessed_statements_for_company instead.
        Kept for backward compatibility only.
        """
        return None
    
    def get_processing_summary(self) -> ProcessingSummary:
        """Get overall processing summary from cache."""
        if not self._cache_valid:
            self.refresh_cache()
        
        return self._summary_cache if self._summary_cache else ProcessingSummary(
            total_companies=0,
            processed_companies=0,
            companies_with_partial_processing=0,
            total_source_statements=0,
            total_target_statements=0,
            unprocessed_statements_count=0
        )
    
    def is_company_processed(self, company_cik: str) -> bool:
        """Check if a company has been fully processed."""
        status = self.get_company_status(company_cik)
        return status.is_fully_processed if status else False
    
    def is_statement_processed(self, statement: FinancialStatement) -> bool:
        """
        Check if a specific statement has been processed.
        Queries the database directly instead of using cache.
        """
        try:
            statement_date = None
            if hasattr(statement.reporting_period, 'isoformat'):
                statement_date = statement.reporting_period
            elif isinstance(statement.reporting_period, dict) and 'end_date' in statement.reporting_period:
                statement_date = statement.reporting_period['end_date']
            
            if statement_date:
                query = {
                    "company_cik": statement.company_cik,
                    "reporting_period.end_date": statement_date,
                    "statement_type": statement.statement_type
                }
                
                annual_count = self.annual_value_repo.collection.count_documents(query, limit=1)
                quarterly_count = self.quarterly_value_repo.collection.count_documents(query, limit=1)
                
                return (annual_count > 0 or quarterly_count > 0)
        except Exception as e:
            logger.debug(f"Error checking if statement is processed: {e}")
        
        return False
    
    def mark_statement_processed(self, statement: FinancialStatement) -> None:
        """
        Mark a statement as processed.
        In the new optimized tracker, this is a no-op since we query the database directly.
        The statement is considered processed once values exist in the target database.
        """
        # No-op: The database itself is the source of truth
        pass
    
    def get_companies_by_fiscal_year(self, fiscal_year: str) -> List[str]:
        """
        Get companies that have statements for a specific fiscal year.
        Uses database query instead of cache.
        """
        from datetime import datetime
        
        # Query source statements for the given fiscal year
        companies_set = set()
        
        try:
            # Use aggregation to find companies with statements in this fiscal year
            pipeline = [
                {
                    "$match": {
                        "data": {"$exists": True, "$nin": [[], None]}
                    }
                },
                {
                    "$project": {
                        "company_cik": 1,
                        "year": {
                            "$year": {
                                "$cond": [
                                    {"$eq": [{"$type": "$reporting_period"}, "date"]},
                                    "$reporting_period",
                                    {"$dateFromString": {"dateString": "$reporting_period.end_date"}}
                                ]
                            }
                        }
                    }
                },
                {
                    "$match": {
                        "year": int(fiscal_year)
                    }
                },
                {
                    "$group": {
                        "_id": "$company_cik"
                    }
                }
            ]
            
            results = list(self.source_statement_repo.collection.aggregate(pipeline))
            companies_set = {doc['_id'] for doc in results}
            
        except Exception as e:
            logger.warning(f"Error querying fiscal year {fiscal_year}: {e}, using cursor-based fallback")
            # Fallback: use cursor instead of loading all into memory
            cursor = self.source_statement_repo.collection.find(
                {"data": {"$exists": True, "$nin": [[], None]}},
                {"company_cik": 1, "reporting_period": 1}  # Only fetch needed fields
            )
            
            for doc in cursor:
                try:
                    reporting_period = doc.get('reporting_period')
                    if not reporting_period:
                        continue
                    
                    year = None
                    if isinstance(reporting_period, datetime):
                        year = str(reporting_period.year)
                    elif isinstance(reporting_period, dict):
                        end_date = reporting_period.get('end_date')
                        if isinstance(end_date, datetime):
                            year = str(end_date.year)
                    
                    if year == fiscal_year:
                        companies_set.add(doc['company_cik'])
                except Exception:
                    continue
        
        return list(companies_set)
    
    def get_companies_by_quarter(self, fiscal_year: str, fiscal_period: str) -> List[str]:
        """
        Get companies that have statements for a specific quarter.
        Uses database query instead of cache.
        Optimized for scalability with thousands of companies.
        """
        from datetime import datetime
        
        # Convert quarter to month range (Q1=1-3, Q2=4-6, Q3=7-9, Q4=10-12)
        quarter_num = int(fiscal_period.replace('Q', ''))
        start_month = (quarter_num - 1) * 3 + 1
        end_month = quarter_num * 3
        
        companies_set = set()
        
        try:
            # Use aggregation to find companies with statements in this quarter
            # This is much faster than loading all statements
            pipeline = [
                {
                    "$match": {
                        "data": {"$exists": True, "$nin": [[], None]}
                    }
                },
                {
                    "$addFields": {
                        "period_date": {
                            "$cond": [
                                {"$eq": [{"$type": "$reporting_period.end_date"}, "date"]},
                                "$reporting_period.end_date",
                                {
                                    "$cond": [
                                        {"$eq": [{"$type": "$reporting_period"}, "date"]},
                                        "$reporting_period",
                                        None
                                    ]
                                }
                            ]
                        }
                    }
                },
                {
                    "$match": {
                        "period_date": {"$ne": None}
                    }
                },
                {
                    "$addFields": {
                        "year": {"$year": "$period_date"},
                        "month": {"$month": "$period_date"}
                    }
                },
                {
                    "$match": {
                        "year": int(fiscal_year),
                        "month": {"$gte": start_month, "$lte": end_month}
                    }
                },
                {
                    "$group": {
                        "_id": "$company_cik"
                    }
                }
            ]
            
            results = list(self.source_statement_repo.collection.aggregate(pipeline))
            companies_set = {doc['_id'] for doc in results}
            
        except Exception as e:
            logger.warning(f"Error querying quarter {fiscal_year} {fiscal_period}: {e}, using cursor-based fallback")
            # Fallback: use cursor (don't load all into memory)
            cursor = self.source_statement_repo.collection.find(
                {"data": {"$exists": True, "$nin": [[], None]}},
                {"company_cik": 1, "reporting_period": 1}  # Only fetch needed fields
            )
            
            for doc in cursor:
                try:
                    year = None
                    month = None
                    
                    reporting_period = doc.get('reporting_period')
                    if not reporting_period:
                        continue
                    
                    # Handle different formats
                    if isinstance(reporting_period, datetime):
                        year = str(reporting_period.year)
                        month = reporting_period.month
                    elif isinstance(reporting_period, dict):
                        end_date = reporting_period.get('end_date')
                        if isinstance(end_date, datetime):
                            year = str(end_date.year)
                            month = end_date.month
                    
                    if year and month:
                        stmt_quarter = f"Q{((month - 1) // 3) + 1}"
                        if year == fiscal_year and stmt_quarter == fiscal_period:
                            companies_set.add(doc['company_cik'])
                            
                except Exception as e:
                    logger.debug(f"Error processing statement for quarter search: {e}")
                    continue
        
        return list(companies_set)
    
    def invalidate_cache(self) -> None:
        """Invalidate the cache to force refresh on next access."""
        self._cache_valid = False
        if self._summary_cache:
            self._summary_cache = None
    
    def check_concept_exists(self, company_cik: str, statement_type: str, concept: str, dimension_concept: bool = False) -> bool:
        """Check if a concept already exists in the target database."""
        existing_concept = self.target_concept_repo.find_existing(
            company_cik, statement_type, concept, dimension_concept
        )
        return existing_concept is not None
    
    def get_concept_reference(self, company_cik: str, statement_type: str, concept: str, dimension_concept: bool = False) -> Optional[ObjectId]:
        """Get reference to existing concept if it exists."""
        existing_concept = self.target_concept_repo.find_existing(
            company_cik, statement_type, concept, dimension_concept
        )
        if existing_concept:
            return existing_concept.get('_id')
        return None
    
    def should_skip_abstract_concept(self, concept_data: Dict[str, Any]) -> bool:
        """Check if a concept should be skipped because it's abstract."""
        return concept_data.get('abstract', False)
    
    def get_hierarchy_position(self, company_cik: str, statement_type: str, concept: str) -> Optional[str]:
        """Get the correct hierarchy position for a new concept."""
        # This is a placeholder - in a real implementation, you would
        # analyze the concept structure and find the best position
        # For now, we'll use a simple approach
        try:
            existing_concepts = list(self.target_concept_repo.collection.find({
                "company_cik": company_cik,
                "statement_type": statement_type,
                "dimension_concept": False
            }).sort("path", 1))
            
            if not existing_concepts:
                return "001"  # First concept
            
            # Find next available path
            last_concept = existing_concepts[-1]
            if last_concept.get('path'):
                # Extract the last number and increment
                path_parts = last_concept['path'].split('.')
                last_num = int(path_parts[-1])
                next_num = last_num + 1
                path_parts[-1] = f"{next_num:03d}"
                return '.'.join(path_parts)
            else:
                return f"{len(existing_concepts) + 1:03d}"
                
        except Exception as e:
            logger.warning(f"Error determining hierarchy position: {e}")
            return None

    def close(self) -> None:
        """Close database connections."""
        self.source_connection.close()
        self.target_connection.close()
    
    def log_summary(self) -> None:
        """Log a detailed processing summary."""
        summary = self.get_processing_summary()
        
        logger.info("="*80)
        logger.info("DATABASE TRACKER SUMMARY")
        logger.info("="*80)
        logger.info(f"Total Companies: {summary.total_companies}")
        logger.info(f"Fully Processed Companies: {summary.processed_companies}")
        logger.info(f"Partially Processed Companies: {summary.companies_with_partial_processing}")
        logger.info(f"Unprocessed Companies: {summary.total_companies - summary.processed_companies - summary.companies_with_partial_processing}")
        logger.info(f"Total Source Statements: {summary.total_source_statements}")
        logger.info(f"Total Target Statements: {summary.total_target_statements}")
        logger.info(f"Unprocessed Statements: {summary.unprocessed_statements_count}")
        logger.info(f"Overall Progress: {summary.overall_percentage:.2f}%")
        logger.info("="*80)
