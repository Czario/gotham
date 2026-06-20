#!/usr/bin/env python3
"""
Script to create MongoDB indexes for optimal performance.
Run this once after setting up the database.
"""
import sys
from pathlib import Path

# Add the src directory to the Python path
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

from pymongo import ASCENDING, IndexModel
from data_normalization_service.core.config import AppConfig
from data_normalization_service.database import DatabaseConnection


def create_source_indexes(db_connection: DatabaseConnection):
    """Create indexes on source database collections."""
    print("Creating indexes on source database...")
    
    source_db = db_connection.source_db
    financial_statements = source_db['financial_statements']
    
    # Create indexes for financial_statements collection
    indexes = [
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
        IndexModel(
            [("reporting_period.end_date", ASCENDING)],
            name="idx_reporting_period_end_date"
        ),
    ]
    
    # Create indexes (will skip if they already exist)
    result = financial_statements.create_indexes(indexes)
    print(f"  ✓ Created {len(result)} indexes on financial_statements collection")
    
    # List all indexes
    print("  Current indexes on financial_statements:")
    for index in financial_statements.list_indexes():
        print(f"    - {index['name']}: {index['key']}")


def create_target_indexes(db_connection: DatabaseConnection):
    """Create indexes on target database collections."""
    print("\nCreating indexes on target database...")
    
    target_db = db_connection.target_db
    
    # Indexes for annual collections
    concept_values_annual = target_db['concept_values_annual']
    indexes_annual = [
        IndexModel(
            [
                ("company_cik", ASCENDING),
                ("reporting_period.end_date", ASCENDING),
                ("statement_type", ASCENDING)
            ],
            name="idx_company_period_type"
        ),
        IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
        IndexModel([("concept", ASCENDING)], name="idx_concept"),
    ]
    
    result = concept_values_annual.create_indexes(indexes_annual)
    print(f"  ✓ Created {len(result)} indexes on concept_values_annual collection")
    
    # List all indexes
    print("  Current indexes on concept_values_annual:")
    for index in concept_values_annual.list_indexes():
        print(f"    - {index['name']}: {index['key']}")
    
    # Indexes for quarterly collections
    concept_values_quarterly = target_db['concept_values_quarterly']
    indexes_quarterly = [
        IndexModel(
            [
                ("company_cik", ASCENDING),
                ("reporting_period.end_date", ASCENDING),
                ("statement_type", ASCENDING)
            ],
            name="idx_company_period_type"
        ),
        IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
        IndexModel([("concept", ASCENDING)], name="idx_concept"),
    ]
    
    result = concept_values_quarterly.create_indexes(indexes_quarterly)
    print(f"  ✓ Created {len(result)} indexes on concept_values_quarterly collection")
    
    # List all indexes
    print("  Current indexes on concept_values_quarterly:")
    for index in concept_values_quarterly.list_indexes():
        print(f"    - {index['name']}: {index['key']}")
    
    # Indexes for normalized concepts (annual and quarterly)
    for collection_name in ['normalized_concepts_annual', 'normalized_concepts_quarterly']:
        collection = target_db[collection_name]
        indexes_concepts = [
            IndexModel(
                [
                    ("company_cik", ASCENDING),
                    ("statement_type", ASCENDING),
                    ("concept", ASCENDING),
                    ("dimension_concept", ASCENDING)
                ],
                name="idx_company_statement_concept"
            ),
            IndexModel([("company_cik", ASCENDING)], name="idx_company_cik"),
            IndexModel([("concept", ASCENDING)], name="idx_concept"),
        ]
        
        result = collection.create_indexes(indexes_concepts)
        print(f"  ✓ Created {len(result)} indexes on {collection_name} collection")


def main():
    """Main function to create all indexes."""
    try:
        print("="*60)
        print("MONGODB INDEX CREATION SCRIPT")
        print("="*60)
        print()
        
        # Load configuration
        config = AppConfig.from_env()
        
        # Create database connection
        db_connection = DatabaseConnection(config.database)
        
        # Create indexes
        create_source_indexes(db_connection)
        create_target_indexes(db_connection)
        
        print()
        print("="*60)
        print("✅ All indexes created successfully!")
        print("="*60)
        print()
        print("Note: Indexes are created idempotently - running this script")
        print("multiple times is safe and will not create duplicate indexes.")
        print()
        
        # Close connection
        db_connection.close()
        
    except Exception as e:
        print(f"\n❌ Error creating indexes: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
