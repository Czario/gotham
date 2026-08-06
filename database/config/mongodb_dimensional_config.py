#!/usr/bin/env python3
"""Enhanced database configuration with dimensions support"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database
from datetime import datetime
import os
from typing import Optional

# Enhanced database collections configuration with dimensions support
ENHANCED_COLLECTIONS_CONFIG = {
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
                "required": ["cik", "accession_number", "form_type", "filing_date", "created_at"],
                "properties": {
                    "cik": {"bsonType": "string"},
                    "accession_number": {"bsonType": "string"},
                    "form_type": {"enum": ["10-K", "10-Q", "8-K", "DEF 14A", "S-1", "S-3"]},
                    "filing_date": {"bsonType": "date"},
                    "acceptance_datetime": {"bsonType": ["date", "null"]},
                    "created_at": {"bsonType": "date"}
                }
            }
        },
        'indexes': [
            ([("cik", ASCENDING), ("accession_number", ASCENDING)], {"unique": True}),
            ([("cik", ASCENDING), ("filing_date", DESCENDING)], {}),
            ([("form_type", ASCENDING)], {}),
            ([("filing_date", DESCENDING)], {})
        ]
    },
    
    'financial_statements': {
        'validator': {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["cik", "filing_id", "statement_type", "reporting_period", "created_at"],
                "properties": {
                    "cik": {"bsonType": "string"},
                    "filing_id": {"bsonType": "objectId"},
                    "statement_type": {"enum": ["income", "balancesheet", "cashflow", "equity"]},
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
                            "cik": {"bsonType": ["string", "null"]},
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
                                "order": {"bsonType": ["int", "double", "number"]},
                                "concept": {"bsonType": "string"},
                                "label": {"bsonType": "string"},
                                "level": {"bsonType": ["int", "null"]},
                                "abstract": {"bsonType": "bool"},
                                "dimension": {"bsonType": "bool"}
                            },
                            "additionalProperties": True  # Allows for period data and dimensional breakdown
                        }
                    },
                    # New field for dimension metadata
                    "dimension_metadata": {
                        "bsonType": ["object", "null"],
                        "properties": {
                            "total_dimension_columns": {"bsonType": "int"},
                            "dimensions": {
                                "bsonType": "object",
                                "additionalProperties": {
                                    "bsonType": "object",
                                    "properties": {
                                        "column_name": {"bsonType": "string"},
                                        "unique_values_count": {"bsonType": "int"},
                                        "unique_values": {"bsonType": "array"},
                                        "sample_concepts_using_dimension": {"bsonType": "array"}
                                    }
                                }
                            }
                        }
                    },
                    # Data quality tracking for enhanced transformations
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
                    "validation_warnings": {
                        "bsonType": ["array", "null"],
                        "items": {"bsonType": "string"}
                    },
                    "created_at": {"bsonType": "date"}
                }
            }
        },
        'indexes': [
            ([("cik", ASCENDING), ("statement_type", ASCENDING), ("reporting_period.end_date", DESCENDING)], {}),
            ([("filing_id", ASCENDING)], {}),
            ([("statement_type", ASCENDING)], {}),
            ([("reporting_period.fiscal_year", ASCENDING)], {}),
            ([("created_at", DESCENDING)], {}),
            ([("financial_data.order", ASCENDING)], {}),
            ([("financial_data.label", "text"), ("financial_data.concept", "text")], {}),
            # New indexes for dimensional queries
            ([("dimension_metadata.dimensions", ASCENDING)], {}),
            ([("financial_data.dimension", ASCENDING)], {})
        ]
    }
}

def setup_enhanced_database_schema(db):
    """
    Set up MongoDB collections with enhanced schema supporting dimensions
    
    Args:
        db: MongoDB database instance
    """
    try:
        # Set up each collection with validation schema and indexes
        for collection_name, config in ENHANCED_COLLECTIONS_CONFIG.items():
            # Create collection with validation schema if it doesn't exist
            if collection_name not in db.list_collection_names():
                db.create_collection(
                    collection_name,
                    validator=config['validator']
                )
                print(f"Created enhanced collection: {collection_name}")
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
            # Indexes are managed by create-indexes.js in admin_backend
            # No auto-creation here.
        
        print("Enhanced database schema setup completed successfully")
        print("New features:")
        print("  - Dimensional data support in financial_data")
        print("  - Dimension metadata tracking")
        print("  - Enhanced indexing for dimensional queries")
        
    except Exception as e:
        print(f"Error setting up enhanced database schema: {e}")
        raise

# Convenience function to upgrade existing schema
def upgrade_to_enhanced_schema(db):
    """
    Upgrade existing database to support dimensions
    """
    print("🔄 Upgrading database schema to support dimensions...")
    setup_enhanced_database_schema(db)
    print("✅ Schema upgrade completed!")
