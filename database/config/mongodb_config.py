#!/usr/bin/env python3
"""Database configuration and connection management"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database
from datetime import datetime
import os
from typing import Optional

class DatabaseConfig:
    """Database configuration and connection management"""
    
    def __init__(self, connection_string: Optional[str] = None, database_name: Optional[str] = None):
        _uri = connection_string or os.getenv('MONGODB_URI')
        if not _uri:
            raise RuntimeError("MONGODB_URI environment variable is required. Set it in your .env file.")
        self.connection_string = _uri
        _db = database_name or os.getenv('DATABASE_NAME')
        if not _db:
            raise RuntimeError("DATABASE_NAME environment variable is required. Set it in your .env file.")
        self.database_name = _db
        self.client: Optional[MongoClient] = None
        self.db: Optional[Database] = None
    
    def connect(self) -> bool:
        """Establish database connection"""
        try:
            self.client = MongoClient(self.connection_string)
            self.db = self.client[self.database_name]
            # Test connection
            self.client.admin.command('ping')
            return True
        except Exception as e:
            print(f"Database connection failed: {e}")
            return False
    
    def disconnect(self) -> None:
        """Close database connection"""
        if self.client:
            self.client.close()
    
    def get_database(self) -> Optional[Database]:
        """Get database instance"""
        if self.db is None:
            self.connect()
        return self.db

# Database collections configuration
COLLECTIONS_CONFIG = {
    'companies': {
        'validator': {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["cik", "name", "created_at", "updated_at"],
                "properties": {
                    "cik": {"bsonType": "string"},
                    "name": {"bsonType": "string"},
                    "ticker_symbol": {"bsonType": ["string", "null"]},
                    "industry": {
                        "bsonType": "object",
                        "properties": {
                            "sic_code": {"bsonType": ["int", "string", "null"]},
                            "sic_description": {"bsonType": ["string", "null"]}
                        }
                    },
                    "market_info": {
                        "bsonType": "object",
                        "properties": {
                            "tickers": {"bsonType": "array"},
                            "exchanges": {"bsonType": "array"}
                        }
                    },
                    "corporate_info": {
                        "bsonType": "object",
                        "properties": {
                            "state_of_incorporation": {"bsonType": ["string", "null"]},
                            "fiscal_year_end": {"bsonType": ["string", "null"]},
                            "entity_type": {"bsonType": ["string", "null"]},
                            "business_address": {
                                "bsonType": "object",
                                "properties": {
                                    "street": {"bsonType": ["string", "null"]},
                                    "city": {"bsonType": ["string", "null"]},
                                    "state": {"bsonType": ["string", "null"]},
                                    "zip_code": {"bsonType": ["string", "null"]}
                                }
                            },
                            "phone": {"bsonType": ["string", "null"]}
                        }
                    },
                    "created_at": {"bsonType": "date"},
                    "updated_at": {"bsonType": "date"}
                }
            }
        },
        'indexes': [
            ([("cik", ASCENDING)], {"unique": True}),
            ([("name", ASCENDING)], {}),
            ([("market_info.tickers", ASCENDING)], {}),
            ([("industry.sic_code", ASCENDING)], {}),
            ([("updated_at", DESCENDING)], {}),
            ([("name", "text"), ("industry.sic_description", "text")], {})
        ]
    },
    
    'filings': {
        'validator': {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["company_cik", "accession_number", "form_type", "filing_date", "created_at"],
                "properties": {
                    "company_cik": {"bsonType": "string"},
                    "accession_number": {"bsonType": "string"},
                    "form_type": {"enum": ["10-K", "10-Q", "8-K", "DEF 14A", "S-1", "S-3"]},
                    "filing_date": {"bsonType": "date"},
                    "acceptance_datetime": {"bsonType": ["date", "null"]},
                    "created_at": {"bsonType": "date"}
                }
            }
        },
        'indexes': [
            ([("company_cik", ASCENDING), ("accession_number", ASCENDING)], {"unique": True}),
            ([("company_cik", ASCENDING), ("filing_date", DESCENDING)], {}),
            ([("form_type", ASCENDING)], {}),
            ([("filing_date", DESCENDING)], {})
        ]
    },
    
    'financial_statements': {
        'validator': {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["company_cik", "filing_id", "statement_type", "reporting_period", "created_at"],
                "properties": {
                    "company_cik": {"bsonType": "string"},
                    "filing_id": {"bsonType": ["objectId", "string"]},  # Accept both ObjectId and string
                    "statement_type": {"enum": ["income_statement", "balance_sheet", "cash_flows", "equity_changes", "comprehensive_income"]},
                    "reporting_period": {
                        "bsonType": "object",
                        "required": ["end_date"],
                        "properties": {
                            "start_date": {"bsonType": ["date", "null"]},
                            "end_date": {"bsonType": "date"},
                            "period_type": {"enum": ["annual", "quarterly", "interim"]},
                            "fiscal_year": {"bsonType": ["int", "null"]},
                            "quarter": {"bsonType": ["int", "null"]},
                            "period_date": {"bsonType": ["string", "null"]},
                            "form_type": {"bsonType": ["string", "null"]},
                            "fiscal_year_end_code": {"bsonType": ["string", "null"]},
                            "data_source": {"bsonType": ["string", "null"]},
                            "company_cik": {"bsonType": ["string", "null"]},
                            "company_name": {"bsonType": ["string", "null"]},
                            "note": {"bsonType": ["string", "null"]},
                            "validation_warnings": {"bsonType": ["array", "null"]}
                        }
                    },
                    "financial_data": {
                        "bsonType": "array",
                        "items": {
                            "bsonType": "object",
                            "properties": {
                                "order": {"bsonType": "int"},
                                "concept": {"bsonType": "string"},
                                "label": {"bsonType": "string"},
                                "level": {"bsonType": ["int", "null"]},
                                "abstract": {"bsonType": "bool"},
                                "dimension": {"bsonType": "bool"}
                            },
                            "additionalProperties": True
                        }
                    },
                    "data_quality": {
                        "bsonType": ["object", "null"],
                        "properties": {
                            "enhanced_transformation": {"bsonType": ["bool", "null"]},
                            "period_standardized": {"bsonType": ["bool", "null"]},
                            "units_standardized": {"bsonType": ["bool", "null"]},
                            "prior_periods_filtered": {"bsonType": ["bool", "null"]},
                            "dimensional_facts_cleaned": {"bsonType": ["bool", "null"]},
                            "fiscal_calculations_added": {"bsonType": ["bool", "null"]}
                        }
                    },
                    "created_at": {"bsonType": "date"}
                }
            }
        },
        'indexes': [
            ([("company_cik", ASCENDING), ("statement_type", ASCENDING), ("reporting_period.end_date", DESCENDING)], {}),
            ([("filing_id", ASCENDING)], {}),
            ([("statement_type", ASCENDING)], {}),
            ([("reporting_period.fiscal_year", ASCENDING)], {}),
            ([("created_at", DESCENDING)], {}),
            ([("financial_data.order", ASCENDING)], {}),
            ([("financial_data.label", "text"), ("financial_data.concept", "text")], {})
        ]
    }
}

def setup_database_schema(db):
    """
    Set up MongoDB collections with proper indexing and schema validation
    
    Args:
        db: MongoDB database instance
        
    Returns:
        bool: True if setup was successful, False otherwise
    """
    try:
        # Set up each collection with validation schema and indexes
        for collection_name, config in COLLECTIONS_CONFIG.items():
            # Create collection with validation schema if it doesn't exist
            if collection_name not in db.list_collection_names():
                db.create_collection(
                    collection_name,
                    validator=config['validator']
                )
                print(f"Created collection: {collection_name}")
            else:
                # Update validation schema for existing collection
                try:
                    db.command({
                        "collMod": collection_name,
                        "validator": config['validator']
                    })
                    print(f"Updated validation schema for: {collection_name}")
                except Exception as e:
                    print(f"Warning: Could not update validation for {collection_name}: {e}")
            
            # Create indexes
            collection = db[collection_name]
            for index_spec, index_options in config['indexes']:
                try:
                    collection.create_index(index_spec, **index_options)
                except Exception as e:
                    # Index might already exist
                    pass
        
        print("Database schema setup completed successfully")
        return True
        
    except Exception as e:
        print(f"Error setting up database schema: {e}")
        return False
