#!/usr/bin/env python3
"""Database operations and data access layer with unified error handling"""

from datetime import datetime
from typing import Dict, List, Optional, Any, Union
from bson import ObjectId
import pandas as pd
import time
from pymongo.database import Database

from database.config.mongodb_config import DatabaseConfig, COLLECTIONS_CONFIG
from utilities.helpers.error_handling import safe_database_operation
from utilities.helpers.logger_config import get_module_logger

logger = get_module_logger(__name__)

class DatabaseManager:
    """Manages database operations for financial data"""
    
    def __init__(self, db_config: Optional[DatabaseConfig] = None):
        self.db_config = db_config or DatabaseConfig()
        self.db: Optional[Database] = self.db_config.get_database()
    
    def setup_collections(self):
        """Set up database collections with validation and indexes"""
        try:
            db = self.db
            if db is None:
                print("Database connection not available")
                return False
                
            for collection_name, config in COLLECTIONS_CONFIG.items():
                # Create collection with validation if it doesn't exist
                if collection_name not in db.list_collection_names():
                    db.create_collection(collection_name, validator=config['validator'])
                
                # Create indexes
                collection = db[collection_name]
                for index_spec, options in config['indexes']:
                    try:
                    except Exception as e:
                        print(f"Index creation warning for {collection_name}: {e}")
            
            print("Database collections setup completed successfully!")
            return True
            
        except Exception as e:
            print(f"Error setting up collections: {e}")
            return False

class CompanyRepository:
    """Repository for company data operations"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager.db
        if self.db is not None:
            self.collection = self.db.companies
        else:
            raise Exception("Database connection not available")
    
    def save_company(self, company_data: Dict) -> bool:
        """Save or update company information"""
        try:
            self.collection.update_one(
                {'cik': company_data['cik']},
                {'$set': {**company_data, 'updated_at': datetime.now()}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"Error saving company {company_data.get('cik')}: {e}")
            return False
    
    def get_company(self, cik: str) -> Optional[Dict]:
        """Get company by CIK"""
        return self.collection.find_one({'cik': str(cik)})
    
    def update_fiscal_year_end(self, cik: str, fiscal_year_end: str) -> bool:
        """Update the fiscal year-end for a company"""
        try:
            result = self.collection.update_one(
                {'cik': str(cik)},
                {
                    '$set': {
                        'fiscal_year_end': fiscal_year_end,
                        'fiscal_year_end_updated_at': datetime.now()
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            print(f"Error updating fiscal year-end for company {cik}: {e}")
            return False
    
    def get_fiscal_year_end(self, cik: str) -> Optional[str]:
        """Get the fiscal year-end for a company"""
        company = self.get_company(cik)
        return company.get('fiscal_year_end') if company else None

class FilingRepository:
    """Repository for filing data operations"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager.db
        if self.db is not None:
            self.collection = self.db.filings
        else:
            raise Exception("Database connection not available")
    
    @safe_database_operation("save_filing", default_return=None)
    def save_filing(self, filing_data: Dict) -> Optional[ObjectId]:
        """Save filing and return its ID"""
        result = self.collection.update_one(
            {
                'cik': filing_data['cik'],
                'accession_number': filing_data['accession_number']
            },
            {'$set': filing_data},
            upsert=True
        )
        
        if result.upserted_id:
            return result.upserted_id
        else:
            # Find the existing document
            doc = self.collection.find_one({
                'cik': filing_data['cik'],
                'accession_number': filing_data['accession_number']
            })
            return doc['_id'] if doc else None
    
    def get_filing_by_accession(self, accession_number: str) -> Optional[Dict]:
        """Get filing by accession number"""
        return self.collection.find_one({'accession_number': accession_number})
    
    def get_filings(self, cik: str, limit: int = 10) -> List[Dict]:
        """Get recent filings for a company"""
        try:
            cursor = self.collection.find(
                {'cik': str(cik)},
                sort=[('filing_date', -1)],
                limit=limit
            )
            return list(cursor)
        except Exception as e:
            print(f"Error getting filings for CIK {cik}: {e}")
            return []


class FinancialStatementRepository:
    """Repository for financial statement data operations"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager.db
        if self.db is not None:
            self.collection = self.db.financial_statements
        else:
            raise Exception("Database connection not available")
    
    @safe_database_operation("save_statement", default_return=False)
    def save_statement(self, statement_data: Dict) -> bool:
        """Save financial statement with enhanced duplicate prevention"""
        # Create unique identifier
        unique_id = f"{statement_data['cik']}_{statement_data['filing_id']}_{statement_data['statement_type']}"
        
        # Enhanced duplicate checking: use end_date as the primary period identifier
        reporting_period = statement_data.get('reporting_period', {})
        fiscal_year = reporting_period.get('fiscal_year')  # May be None if using SEC API only
        quarter = reporting_period.get('quarter')
        period_type = reporting_period.get('period_type')
        end_date = reporting_period.get('end_date')
        
        # Check if we already have a statement for this exact period
        # Use end_date as the primary identifier since it's unique for each period
        existing_query = {
            'cik': statement_data['cik'],
            'statement_type': statement_data['statement_type'],
            'reporting_period.end_date': end_date
        }
        
        # Fallback to fiscal_year + quarter only if end_date is not available AND fiscal_year exists
        if end_date is None and fiscal_year is not None:
            existing_query = {
                'cik': statement_data['cik'],
                'statement_type': statement_data['statement_type'],
                'reporting_period.fiscal_year': fiscal_year,
                'reporting_period.period_type': period_type
            }
            # For quarterly statements, also match the quarter
            if quarter is not None:
                existing_query['reporting_period.quarter'] = quarter
        elif end_date is None and fiscal_year is None:
            # Final fallback: use filing_id as unique identifier if no period info available
            existing_query = {
                'cik': statement_data['cik'],
                'statement_type': statement_data['statement_type'],
                'filing_id': statement_data.get('filing_id')
            }
        
        existing_statement = self.collection.find_one(existing_query)
        
        if existing_statement:
            # Found existing statement for same period
            existing_filing_id = existing_statement.get('filing_id')
            current_filing_id = statement_data.get('filing_id')
            
            # Create period description for logging
            if end_date:
                period_desc = f"ending {end_date}"
            elif fiscal_year:
                period_desc = f"FY{fiscal_year} Q{quarter if quarter else 'N/A'}"
            else:
                period_desc = f"filing {current_filing_id}"
            
            logger.warning(f"Found existing statement for {statement_data['cik']} {statement_data['statement_type']} "
                         f"period {period_desc}")
            logger.info(f"   Existing filing: {existing_filing_id}")
            logger.info(f"   Current filing:  {current_filing_id}")
            
            # Only update if it's the same filing or if we have a newer filing
            if current_filing_id == existing_filing_id:
                logger.info("   ✅ Updating same filing")
                # Update the existing document
                self.collection.update_one(
                    {'_id': existing_statement['_id']},
                    {'$set': statement_data}
                )
            else:
                logger.warning("   ⚠️  Skipping duplicate - different filing for same period")
                logger.info("   💡 Consider manual review if this is an amended filing")
                return False
        else:
            # No existing statement for this period, safe to insert
            self.collection.update_one(
                {
                    'cik': statement_data['cik'],
                    'filing_id': statement_data['filing_id'],
                    'statement_type': statement_data['statement_type']
                },
                {'$set': statement_data},
                upsert=True
            )
        
        return True
    
    def get_latest_statement(self, cik: str, statement_type: str) -> Optional[Dict]:
        """Get latest statement of a specific type"""
        return self.collection.find_one(
            {'cik': str(cik), 'statement_type': statement_type},
            sort=[('reporting_period.end_date', -1)]
        )
    
    def get_statements(self, cik: str, limit: int = 10) -> List[Dict]:
        """Get recent financial statements for a company"""
        try:
            cursor = self.collection.find(
                {'cik': str(cik)},
                sort=[('reporting_period.end_date', -1)],
                limit=limit
            )
            return list(cursor)
        except Exception as e:
            print(f"Error getting statements for CIK {cik}: {e}")
            return []
    
    def find_duplicate_statements(self, company_cik: Optional[str] = None) -> List[Dict]:
        """Find duplicate financial statements for the same period"""
        try:
            # Aggregation pipeline to find duplicates
            pipeline = []
            
            # Match specific company if provided
            if company_cik:
                pipeline.append({"$match": {"cik": company_cik}})
            
            # Group by company, statement type, and reporting period
            pipeline.extend([
                {
                    "$group": {
                        "_id": {
                            "cik": "$cik",
                            "statement_type": "$statement_type",
                            "fiscal_year": "$reporting_period.fiscal_year",
                            "quarter": "$reporting_period.quarter",
                            "period_type": "$reporting_period.period_type"
                        },
                        "count": {"$sum": 1},
                        "documents": {"$push": {
                            "id": "$_id",
                            "filing_id": "$filing_id",
                            "created_at": "$created_at"
                        }}
                    }
                },
                {
                    "$match": {"count": {"$gt": 1}}  # Only duplicates
                },
                {
                    "$sort": {"_id.company_cik": 1, "_id.statement_type": 1}
                }
            ])
            
            duplicates = list(self.collection.aggregate(pipeline))
            
            print(f"🔍 Found {len(duplicates)} duplicate statement groups")
            
            return duplicates
            
        except Exception as e:
            print(f"Error finding duplicates: {e}")
            return []
    
    def clean_duplicate_statements(self, company_cik: Optional[str] = None, dry_run: bool = True) -> Dict:
        """Clean up duplicate statements, keeping the most recent one"""
        try:
            duplicates = self.find_duplicate_statements(company_cik)
            
            cleanup_stats = {
                "groups_found": len(duplicates),
                "documents_removed": 0,
                "groups_cleaned": 0
            }
            
            for duplicate_group in duplicates:
                group_info = duplicate_group["_id"]
                documents = duplicate_group["documents"]
                
                print(f"\n📋 Duplicate group: {group_info['cik']} {group_info['statement_type']} "
                      f"FY{group_info['fiscal_year']} Q{group_info['quarter'] or 'N/A'}")
                
                # Sort by created_at to keep the most recent
                documents.sort(key=lambda x: x.get('created_at', ''), reverse=True)
                
                # Keep the first (most recent), remove the rest
                to_keep = documents[0]
                to_remove = documents[1:]
                
                print(f"   ✅ Keeping: {to_keep['id']} (filing: {to_keep['filing_id']})")
                
                for doc in to_remove:
                    print(f"   🗑️  {'[DRY RUN] ' if dry_run else ''}Removing: {doc['id']} (filing: {doc['filing_id']})")
                    
                    if not dry_run:
                        self.collection.delete_one({"_id": doc["id"]})
                        cleanup_stats["documents_removed"] += 1
                
                if not dry_run:
                    cleanup_stats["groups_cleaned"] += 1
            
            if dry_run:
                print(f"\n🔍 DRY RUN COMPLETE: Found {cleanup_stats['groups_found']} duplicate groups")
                print(f"   Would remove {sum(len(g['documents']) - 1 for g in duplicates)} duplicate documents")
            else:
                print(f"\n✅ CLEANUP COMPLETE: Cleaned {cleanup_stats['groups_cleaned']} groups")
                print(f"   Removed {cleanup_stats['documents_removed']} duplicate documents")
            
            return cleanup_stats
            
        except Exception as e:
            print(f"Error cleaning duplicates: {e}")
            return {"error": str(e)}
