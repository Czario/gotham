#!/usr/bin/env python3
"""
Duplicate Prevention System for Hierarchy Management

This module provides comprehensive duplicate prevention mechanisms to ensure
no multiple concepts exist with the same path and order combination.
"""
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import logging
from dataclasses import dataclass

# Add the current directory to the Python path
# sys.path.insert(0, str(Path(__file__).parent))

from ..database import DatabaseConnection, ConceptRepository
from ..core.models import ConceptDocument
from ..core.config import AppConfig
from bson import ObjectId

logger = logging.getLogger(__name__)


@dataclass
class DuplicateCheck:
    """Result of duplicate checking operation."""
    has_duplicates: bool
    conflicts: List[Dict[str, Any]]
    total_checked: int
    

@dataclass
class PathOrderKey:
    """Represents a path-order combination for uniqueness checking."""
    company_cik: str
    statement_type: str
    path: str
    order_key: str
    
    def __hash__(self):
        return hash((self.company_cik, self.statement_type, self.path, self.order_key))


class DuplicatePreventionManager:
    """Manages duplicate prevention for concept insertion and hierarchy operations."""
    
    def __init__(self, config: AppConfig, concept_repo: Optional[ConceptRepository] = None):
        self.config = config
        self.db_connection = DatabaseConnection(config.database)
        self.concept_repo = concept_repo or ConceptRepository(self.db_connection)
        
    def check_for_duplicates(self, company_cik: str, statement_type: str) -> DuplicateCheck:
        """
        Check for existing duplicates in the database.
        
        Args:
            company_cik: Company identifier
            statement_type: Statement type to check
            
        Returns:
            DuplicateCheck result with any found conflicts
        """
        logger.info(f"Checking for duplicates: {company_cik} - {statement_type}")
        
        # Get all concepts for this company/statement combination
        concepts = list(self.concept_repo.collection.find({
            "company_cik": company_cik,
            "statement_type": statement_type
        }))
        
        # Track path-order combinations
        path_order_map = {}
        conflicts = []
        
        for concept in concepts:
            path = concept.get('path', '')
            order_key = concept.get('order_key', '')
            
            # Skip concepts without proper path/order (they have other issues)
            if not path or not order_key:
                continue
                
            path_order_combo = PathOrderKey(
                company_cik=company_cik,
                statement_type=statement_type,
                path=path,
                order_key=order_key
            )
            
            if path_order_combo in path_order_map:
                # Found duplicate!
                existing_concept = path_order_map[path_order_combo]
                conflicts.append({
                    'path': path,
                    'order_key': order_key,
                    'concepts': [
                        {
                            'id': str(existing_concept['_id']),
                            'concept': existing_concept['concept'],
                            'label': existing_concept.get('label', '')
                        },
                        {
                            'id': str(concept['_id']),
                            'concept': concept['concept'],
                            'label': concept.get('label', '')
                        }
                    ]
                })
            else:
                path_order_map[path_order_combo] = concept
        
        return DuplicateCheck(
            has_duplicates=len(conflicts) > 0,
            conflicts=conflicts,
            total_checked=len(concepts)
        )
    
    def validate_and_adjust_concept(self, concept_doc: ConceptDocument) -> ConceptDocument:
        """
        Validate and adjust concept to prevent duplicates by finding next available order_key.
        
        Args:
            concept_doc: Concept document to validate and potentially adjust
            
        Returns:
            ConceptDocument with adjusted order_key if needed
        """
        if not concept_doc.path or not concept_doc.order_key:
            logger.warning(f"Concept {concept_doc.concept} missing path or order_key")
            return concept_doc  # Let other validation handle this
        
        # Check if this path-order combination already exists
        existing = self.concept_repo.collection.find_one({
            "company_cik": concept_doc.company_cik,
            "statement_type": concept_doc.statement_type,
            "path": concept_doc.path,
            "order_key": concept_doc.order_key
        })
        
        if existing and existing.get('concept') != concept_doc.concept:
            logger.debug(
                f"Path-order combination already exists! "
                f"Path: {concept_doc.path}, Order: {concept_doc.order_key} "
                f"Existing: {existing.get('concept')}, New: {concept_doc.concept}"
            )
            
            # Find next available order_key for this path
            next_order_key = self._find_next_available_order_key(
                concept_doc.company_cik,
                concept_doc.statement_type, 
                concept_doc.path,
                concept_doc.order_key
            )
            
            logger.debug(f"Adjusted order_key from '{concept_doc.order_key}' to '{next_order_key}' for concept {concept_doc.concept}")
            
            # Create new concept doc with adjusted order_key, preserving all fields
            adjusted_concept = ConceptDocument(
                company_cik=concept_doc.company_cik,
                statement_type=concept_doc.statement_type,
                concept=concept_doc.concept,
                label=concept_doc.label,
                path=concept_doc.path,
                order_key=next_order_key,
                abstract=concept_doc.abstract,
                dimension=concept_doc.dimension,
                dimension_concept=concept_doc.dimension_concept,
                concept_id=concept_doc.concept_id,
                segment_type=concept_doc.segment_type,
                context_id=concept_doc.context_id,
                unit_id=concept_doc.unit_id,
                period=concept_doc.period,
                concept_name=concept_doc.concept_name,
                fact_label=concept_doc.fact_label,
                dimensions=concept_doc.dimensions,
                dimension_details=concept_doc.dimension_details
            )
            return adjusted_concept
            
        return concept_doc
    
    def safe_insert_concept(self, concept_doc: ConceptDocument) -> Optional[ObjectId]:
        """
        Safely insert a concept with automatic order_key adjustment to prevent duplicates.
        
        SYNC BEHAVIOR: Always check if concept exists by name first before inserting.
        This ensures we never create duplicate concepts during reprocessing.
        
        Args:
            concept_doc: Concept document to insert
            
        Returns:
            ObjectId if successful, None if insertion fails
        """
        # SYNC BEHAVIOR: First check if this concept already exists.
        # For dimensional concepts, uniqueness must include the parent concept
        # relationship (concept_id). Reusing by member name alone (e.g.
        # ProductMember) incorrectly collapses different parents like Revenue
        # and CostOfGoodsAndServicesSold into the same dimensional concept.
        if concept_doc.dimension_concept:
            existing = self.concept_repo.find_dimensional_existing(
                company_cik=concept_doc.company_cik,
                statement_type=concept_doc.statement_type,
                segment_type=concept_doc.segment_type or 'unknown',
                concept=concept_doc.concept,
                parent_concept_id=concept_doc.concept_id,
                context_id=concept_doc.context_id,
            )
            if existing:
                logger.debug(
                    f"Dimensional concept {concept_doc.concept} already exists "
                    f"for parent {concept_doc.concept_id} (ID: {existing['_id']}), "
                    f"reusing it instead of creating new"
                )
                return existing['_id']
        else:
            existing_by_name = self.concept_repo.find_existing(
                concept_doc.company_cik,
                concept_doc.statement_type,
                concept_doc.concept,
                dimension_concept=concept_doc.dimension_concept
            )

            if existing_by_name:
                logger.debug(f"Concept {concept_doc.concept} already exists (ID: {existing_by_name['_id']}), reusing it instead of creating new")
                return existing_by_name['_id']
        
        # If concept doesn't exist by name, validate and adjust to prevent path-order conflicts
        adjusted_concept = self.validate_and_adjust_concept(concept_doc)
        
        try:
            return self.concept_repo.insert(adjusted_concept)
        except Exception as e:
            logger.error(f"Failed to insert concept {adjusted_concept.concept}: {e}")
            return None
    
    def resolve_duplicates(self, company_cik: str, statement_type: str, 
                          resolution_strategy: str = "keep_first") -> int:
        """
        Resolve existing duplicates using specified strategy.
        
        Args:
            company_cik: Company identifier
            statement_type: Statement type
            resolution_strategy: "keep_first", "keep_last", or "manual"
            
        Returns:
            Number of duplicates resolved
        """
        duplicate_check = self.check_for_duplicates(company_cik, statement_type)
        
        if not duplicate_check.has_duplicates:
            logger.info("No duplicates found to resolve")
            return 0
        
        resolved_count = 0
        
        for conflict in duplicate_check.conflicts:
            path = conflict['path']
            order_key = conflict['order_key']
            concepts = conflict['concepts']
            
            logger.info(f"Resolving duplicate at path {path}, order {order_key}")
            
            if resolution_strategy == "keep_first":
                # Keep first concept, remove others
                to_remove = concepts[1:]
            elif resolution_strategy == "keep_last":
                # Keep last concept, remove others
                to_remove = concepts[:-1]
            else:
                logger.warning(f"Unknown resolution strategy: {resolution_strategy}")
                continue
            
            # Remove duplicate concepts
            for concept_to_remove in to_remove:
                concept_id = ObjectId(concept_to_remove['id'])
                logger.info(f"Removing duplicate concept: {concept_to_remove['concept']}")
                
                # Remove from database
                result = self.concept_repo.collection.delete_one({'_id': concept_id})
                if result.deleted_count > 0:
                    resolved_count += 1
                    logger.info(f"Removed concept {concept_to_remove['concept']}")
                else:
                    logger.warning(f"Failed to remove concept {concept_to_remove['concept']}")
        
        logger.info(f"Resolved {resolved_count} duplicate concepts")
        return resolved_count
    
    def _find_next_available_order_key(self, company_cik: str, statement_type: str, 
                                      path: str, current_order_key: str) -> str:
        """
        Find the next available order_key for a given path to ensure sequential insertion.
        
        Args:
            company_cik: Company identifier
            statement_type: Statement type
            path: The path where we want to insert
            current_order_key: The originally requested order_key
            
        Returns:
            Next available order_key that maintains sequence
        """
        # Get all existing order_keys for this path
        existing_concepts = list(self.concept_repo.collection.find({
            "company_cik": company_cik,
            "statement_type": statement_type,
            "path": path
        }, {"order_key": 1}).sort("order_key", 1))
        
        used_keys = {concept.get('order_key', '') for concept in existing_concepts if concept.get('order_key')}
        
        # Start from the current order key and find the next available one
        base_key = current_order_key
        
        # If the base key is available, use it
        if base_key not in used_keys:
            return base_key
        
        # Find the next available key sequentially
        return self._generate_next_order_key(base_key, used_keys)

    def _generate_next_order_key(self, base_key: str, used_keys: set) -> str:
        """
        Generate the next available order key lexicographically.
        
        Args:
            base_key: The base key to start from
            used_keys: Set of already used keys
            
        Returns:
            Next available order key
        """
        # Method 1: Try incrementing the base key (append 'a', 'b', etc.)
        for suffix in 'abcdefghijklmnopqrstuvwxyz':
            candidate = base_key + suffix
            if candidate not in used_keys:
                return candidate
        
        # Method 2: Try double letters
        for suffix1 in 'abcdefghijklmnopqrstuvwxyz':
            for suffix2 in 'abcdefghijklmnopqrstuvwxyz':
                candidate = base_key + suffix1 + suffix2
                if candidate not in used_keys:
                    return candidate
        
        # Method 3: Fallback with timestamp
        import time
        timestamp_suffix = str(int(time.time() * 1000))[-6:]  # Last 6 digits
        return f"{base_key}_{timestamp_suffix}"
    
    def create_database_indexes(self) -> None:
        """
        Create database indexes to prevent duplicates at the database level.
        """
        try:
            # Create compound unique index on company_cik, statement_type, path, order_key
            self.concept_repo.collection.create_index([
                ("company_cik", 1),
                ("statement_type", 1),
                ("path", 1),
                ("order_key", 1)
            ], unique=True, background=True, name="unique_path_order")
            
            # Create index on company_cik, statement_type, concept (for existing lookups)
            self.concept_repo.collection.create_index([
                ("company_cik", 1),
                ("statement_type", 1),
                ("concept", 1)
            ], background=True, name="concept_lookup")
            
            # Create index for path-based queries
            self.concept_repo.collection.create_index([
                ("company_cik", 1),
                ("statement_type", 1),
                ("path", 1)
            ], background=True, name="path_lookup")
            
            logger.info("Database indexes created successfully")
            
        except Exception as e:
            logger.error(f"Failed to create database indexes: {e}")
    
    def close(self):
        """Close database connections."""
        self.db_connection.close()
    
    def generate_next_order_key_for_testing(self, base_key: str, used_keys: set) -> str:
        """Public method for testing order key generation."""
        return self._generate_next_order_key(base_key, used_keys)


def main():
    """Main function for duplicate prevention utilities."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Duplicate prevention utilities")
    parser.add_argument('command', choices=['check', 'resolve', 'create-indexes'],
                       help='Command to execute')
    parser.add_argument('--cik', help='Company CIK')
    parser.add_argument('--statement-type', help='Statement type')
    parser.add_argument('--strategy', default='keep_first', 
                       choices=['keep_first', 'keep_last'],
                       help='Resolution strategy for duplicates')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    
    # Load configuration
    config = AppConfig.from_env()
    manager = DuplicatePreventionManager(config)
    
    try:
        if args.command == 'check':
            if not args.cik or not args.statement_type:
                print("--cik and --statement-type are required for check command")
                sys.exit(1)
            
            result = manager.check_for_duplicates(args.cik, args.statement_type)
            
            print(f"\nDuplicate Check Results:")
            print(f"Total concepts checked: {result.total_checked}")
            print(f"Duplicates found: {len(result.conflicts)}")
            
            if result.has_duplicates:
                print("\nConflicts:")
                for i, conflict in enumerate(result.conflicts, 1):
                    print(f"\n{i}. Path: {conflict['path']}, Order: {conflict['order_key']}")
                    for concept in conflict['concepts']:
                        print(f"   - {concept['concept']} ({concept['label']})")
            else:
                print("✓ No duplicates found!")
        
        elif args.command == 'resolve':
            if not args.cik or not args.statement_type:
                print("--cik and --statement-type are required for resolve command")
                sys.exit(1)
            
            resolved = manager.resolve_duplicates(args.cik, args.statement_type, args.strategy)
            print(f"Resolved {resolved} duplicate concepts using strategy: {args.strategy}")
        
        elif args.command == 'create-indexes':
            manager.create_database_indexes()
            print("Database indexes created successfully")
    
    finally:
        manager.close()


if __name__ == '__main__':
    main()
