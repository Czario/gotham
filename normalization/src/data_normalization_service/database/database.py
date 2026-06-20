"""
Database connection and repository classes.
"""
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from bson import ObjectId
from typing import Optional, Dict, Any, Iterator
import logging

from ..core.models import ConceptDocument, ValueDocument, FinancialStatement, Filing, Company
from ..core.config import DatabaseConfig

logger = logging.getLogger(__name__)


class DatabaseConnection:
    """Manages MongoDB connections."""
    
    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._source_client: Optional[MongoClient] = None
        self._target_client: Optional[MongoClient] = None
        self._source_db: Optional[Database] = None
        self._target_db: Optional[Database] = None

    @property
    def source_db(self) -> Database:
        """Get source database connection."""
        if self._source_db is None:
            if self._source_client is None:
                self._source_client = MongoClient(self.config.mongodb_uri)
            # Type checker doesn't understand we've verified _source_client is not None
            source_client = self._source_client
            assert source_client is not None
            self._source_db = source_client[self.config.source_db_name]
        return self._source_db

    @property
    def target_db(self) -> Database:
        """Get target database connection."""
        if self._target_db is None:
            if self._target_client is None:
                self._target_client = MongoClient(self.config.mongodb_uri)
            # Type checker doesn't understand we've verified _target_client is not None
            target_client = self._target_client
            assert target_client is not None
            self._target_db = target_client[self.config.database_name]
        return self._target_db

    def close(self):
        """Close database connections."""
        if self._source_client:
            self._source_client.close()
        if self._target_client:
            self._target_client.close()


class FinancialStatementRepository:
    """Repository for financial statements."""
    
    def __init__(self, db_connection: DatabaseConnection):
        self.db_connection = db_connection
        self._collection: Optional[Collection] = None

    @property
    def collection(self) -> Collection:
        """Get financial statements collection."""
        if self._collection is None:
            self._collection = self.db_connection.source_db['financial_statements']
        return self._collection

    def find_all(self) -> Iterator[FinancialStatement]:
        """Find all financial statements."""
        cursor = self.collection.find({})
        for doc in cursor:
            yield FinancialStatement.from_dict(doc)


class FilingRepository:
    """Repository for filings."""
    
    def __init__(self, db_connection: DatabaseConnection):
        self.db_connection = db_connection
        self._collection: Optional[Collection] = None

    @property
    def collection(self) -> Collection:
        """Get filings collection."""
        if self._collection is None:
            self._collection = self.db_connection.source_db['filings']
        return self._collection

    def find_by_id(self, filing_id: ObjectId) -> Optional[Filing]:
        """Find filing by ID."""
        doc = self.collection.find_one({"_id": filing_id})
        if doc:
            return Filing.from_dict(doc)
        return None


class ConceptRepository:
    """Repository for normalized concepts (includes both regular and dimensional concepts)."""
    
    def __init__(self, db_connection: DatabaseConnection, collection_name: str = 'normalized_concepts'):
        self.db_connection = db_connection
        self.collection_name = collection_name
        self._collection: Optional[Collection] = None

    @property
    def collection(self) -> Collection:
        """Get concepts collection."""
        if self._collection is None:
            self._collection = self.db_connection.target_db[self.collection_name]
        return self._collection

    def find_existing(self, company_cik: str, statement_type: str, concept: str, dimension_concept: bool = False) -> Optional[Dict[str, Any]]:
        """Find existing concept (regular or dimensional)."""
        logger.debug(f"Querying concept: company_cik={company_cik}, statement_type={statement_type}, concept={concept}, dimension_concept={dimension_concept}")
        result = self.collection.find_one({
            "company_cik": company_cik,
            "statement_type": statement_type,
            "concept": concept,
            "dimension_concept": dimension_concept
        })
        if result:
            logger.debug(f"Found existing concept: {concept}")
        else:
            logger.debug(f"Concept not found in DB: company_cik={company_cik}, statement_type={statement_type}, concept={concept}")
        return result

    def find_concept_reference_for_hierarchy(self, statement_type: str, concept: str, dimension_concept: bool = False) -> Optional[Dict[str, Any]]:
        """
        Find a reference concept from ANY company to copy path and order_key for hierarchy consistency.
        Uses the most common (majority) path and order_key when multiple references exist.
        
        Args:
            statement_type: Statement type to match
            concept: Concept name to find
            dimension_concept: Whether this is a dimensional concept
            
        Returns:
            Dict with most common path and order_key, or None if no references exist
        """
        logger.debug(f"Looking for reference concept for hierarchy: statement_type={statement_type}, concept={concept}, dimension_concept={dimension_concept}")
        
        # Find all existing concepts (from any company) with this statement_type and concept name
        # that have both path and order_key defined
        cursor = self.collection.find(
            {
                "statement_type": statement_type,
                "concept": concept,
                "dimension_concept": dimension_concept,
                "path": {"$exists": True, "$ne": None},
                "order_key": {"$exists": True, "$ne": None}
            },
            {"path": 1, "order_key": 1, "company_cik": 1, "_id": 0}
        )
        
        # Collect all references
        references = list(cursor)
        
        if not references:
            logger.debug(f"No reference concept found for {concept} in {statement_type}")
            return None
        
        if len(references) == 1:
            # Single reference - use it directly
            result = references[0]
            logger.debug(f"Found single reference from company {result.get('company_cik')}: path={result.get('path')}, order_key={result.get('order_key')}")
            return result
        
        # Multiple references - find the most common path and order_key
        logger.debug(f"Found {len(references)} references for {concept}, selecting most common path and order_key")
        
        # Count occurrences of each path
        from collections import Counter
        path_counts = Counter(ref['path'] for ref in references)
        order_key_counts = Counter(ref['order_key'] for ref in references)
        
        # Get the most common path and order_key
        most_common_path = path_counts.most_common(1)[0][0]
        most_common_order_key = order_key_counts.most_common(1)[0][0]
        
        # Find a reference company that has this combination (for logging purposes)
        reference_company = None
        for ref in references:
            if ref['path'] == most_common_path and ref['order_key'] == most_common_order_key:
                reference_company = ref['company_cik']
                break
        
        # If no exact match, use first company with most common path
        if not reference_company:
            for ref in references:
                if ref['path'] == most_common_path:
                    reference_company = ref['company_cik']
                    break
        
        result = {
            'path': most_common_path,
            'order_key': most_common_order_key,
            'company_cik': reference_company or references[0]['company_cik']
        }
        
        logger.info(f"Selected most common hierarchy for {concept}: path={most_common_path} ({path_counts[most_common_path]}/{len(references)} companies), order_key={most_common_order_key} ({order_key_counts[most_common_order_key]}/{len(references)} companies)")
        
        return result

    def find_dimensional_existing(self, company_cik: str, statement_type: str, segment_type: str, concept: str, parent_concept_id: Optional[ObjectId] = None, context_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Find existing dimensional concept by company, statement, concept, parent concept ID, and optionally context ID."""
        logger.debug(f"Querying dimensional concept: company_cik={company_cik}, statement_type={statement_type}, concept={concept}, parent_concept_id={parent_concept_id}, context_id={context_id}")
        
        # Build query to find dimensional concept
        query = {
            "company_cik": company_cik,
            "statement_type": statement_type,
            "concept": concept,
            "dimension_concept": True
        }
        
        # Add parent concept ID to ensure uniqueness per parent-child relationship
        if parent_concept_id:
            query["concept_id"] = parent_concept_id
        
        # Add context_id if provided for even more specific matching
        # This helps identify the exact dimensional slice across related concepts
        if context_id:
            query["context_id"] = context_id
        
        result = self.collection.find_one(query)
        if result:
            logger.debug(f"Found existing dimensional concept: {concept} (segment_type: {result.get('segment_type')}, parent: {parent_concept_id}, context: {context_id})")
        else:
            logger.debug(f"Dimensional concept not found: company_cik={company_cik}, statement_type={statement_type}, concept={concept}, parent: {parent_concept_id}, context: {context_id}")
        return result

    def find_by_concept_id(self, concept_id: ObjectId) -> Iterator[Dict[str, Any]]:
        """Find all dimensional concepts for a given concept_id."""
        cursor = self.collection.find({"concept_id": concept_id, "dimension_concept": True})
        for doc in cursor:
            yield doc

    def find_by_path(self, company_cik: str, statement_type: str, path: str, dimension_concept: bool = False) -> Optional[Dict[str, Any]]:
        """Find concept by path."""
        return self.collection.find_one({
            "company_cik": company_cik,
            "statement_type": statement_type,
            "path": path,
            "dimension_concept": dimension_concept
        })

    def find_dimensional_by_path(self, concept_id: ObjectId, segment_type: str, path: str) -> Optional[Dict[str, Any]]:
        """Find dimensional concept by path."""
        return self.collection.find_one({
            "concept_id": concept_id,
            "segment_type": segment_type,
            "path": path,
            "dimension_concept": True
        })

    def find_children(self, company_cik: str, statement_type: str, parent_path: str, dimension_concept: bool = False) -> Iterator[Dict[str, Any]]:
        """Find child concepts by parent path."""
        # Find concepts whose path starts with parent_path followed by a dot
        pattern = f"^{parent_path}\\."
        cursor = self.collection.find({
            "company_cik": company_cik,
            "statement_type": statement_type,
            "path": {"$regex": pattern},
            "dimension_concept": dimension_concept
        }).sort("order_key", 1)
        
        for doc in cursor:
            yield doc

    def find_dimensional_children(self, concept_id: ObjectId, segment_type: str, parent_path: str) -> Iterator[Dict[str, Any]]:
        """Find child dimensional concepts by parent path."""
        # Find dimensional concepts whose path starts with parent_path followed by a dot
        pattern = f"^{parent_path}\\."
        cursor = self.collection.find({
            "concept_id": concept_id,
            "segment_type": segment_type,
            "path": {"$regex": pattern},
            "dimension_concept": True
        }).sort("order_key", 1)
        
        for doc in cursor:
            yield doc

    def find_siblings(self, company_cik: str, statement_type: str, path: str, dimension_concept: bool = False) -> Iterator[Dict[str, Any]]:
        """Find sibling concepts at the same level."""
        # Extract parent path (everything before the last dot)
        if '.' in path:
            parent_path = '.'.join(path.split('.')[:-1])
            pattern = f"^{parent_path}\\.[^.]+$"  # Direct children only
        else:
            pattern = "^[^.]+$"  # Root level items only
        
        cursor = self.collection.find({
            "company_cik": company_cik,
            "statement_type": statement_type,
            "path": {"$regex": pattern},
            "dimension_concept": dimension_concept
        }).sort("order_key", 1)
        
        for doc in cursor:
            yield doc

    def find_dimensional_siblings(self, concept_id: ObjectId, segment_type: str, path: str) -> Iterator[Dict[str, Any]]:
        """Find sibling dimensional concepts at the same level."""
        # Extract parent path (everything before the last dot)
        if '.' in path:
            parent_path = '.'.join(path.split('.')[:-1])
            pattern = f"^{parent_path}\\.[^.]+$"  # Direct children only
        else:
            pattern = "^[^.]+$"  # Root level items only
        
        cursor = self.collection.find({
            "concept_id": concept_id,
            "segment_type": segment_type,
            "path": {"$regex": pattern},
            "dimension_concept": True
        }).sort("order_key", 1)
        
        for doc in cursor:
            yield doc

    def generate_dimensional_path(self, concept_id: ObjectId, segment_type: str, parent_concept_path: str) -> str:
        """
        Generate a materialized path for dimensional concept as child of parent ConceptDocument.
        The dimensional concept's path naturally extends the parent's path (e.g., parent "003" -> child "003.001").
        """
        # Count existing dimensional concepts for this SPECIFIC parent (concept_id) and segment_type
        existing_count = self.collection.count_documents({
            "concept_id": concept_id,
            "segment_type": segment_type,
            "dimension_concept": True
        })
        
        # Generate child path by appending to parent path
        # Format: parent_path.child_number
        # Example: "003.001" (parent path "003" + child number "001")
        child_number = f"{existing_count + 1:03d}"
        
        if parent_concept_path:
            return f"{parent_concept_path}.{child_number}"
        else:
            # Fallback if parent path is not available
            return child_number

    def get_next_dimensional_order_key(self, concept_id: ObjectId, segment_type: str) -> str:
        """Get the next available order key for dimensional concepts of this segment type."""
        # Count existing dimensional concepts for this concept_id and segment_type
        # This is simpler and more reliable than trying to parse order keys
        existing_count = self.collection.count_documents({
            "concept_id": concept_id,
            "segment_type": segment_type,
            "dimension_concept": True
        })
        
        # Generate order key like "a", "b", "c" for each segment type independently
        if existing_count >= 26:
            # Handle more than 26 items (unlikely but safe)
            return f"z{existing_count - 25}"
        
        return chr(ord('a') + existing_count)

    def insert(self, concept_doc: ConceptDocument) -> ObjectId:
        """Insert new concept document with duplicate prevention."""
        # FIRST: Check if concept with same name already exists (most important check for sync behavior)
        # For dimensional concepts, use more specific matching
        if concept_doc.dimension_concept:
            # For dimensional concepts, check by segment_type, concept, parent concept_id, and context_id
            existing_concept = self.find_dimensional_existing(
                concept_doc.company_cik,
                concept_doc.statement_type,
                concept_doc.segment_type or '',
                concept_doc.concept,
                concept_doc.concept_id,  # parent concept_id
                concept_doc.context_id
            )
        else:
            # For regular concepts, simple name-based matching
            existing_concept = self.find_existing(
                concept_doc.company_cik,
                concept_doc.statement_type,
                concept_doc.concept,
                concept_doc.dimension_concept
            )
        
        if existing_concept:
            # Concept already exists - return existing ID instead of creating duplicate
            concept_type = "Dimensional concept" if concept_doc.dimension_concept else "Concept"
            logger.info(f"{concept_type} '{concept_doc.concept}' already exists with ID {existing_concept['_id']} - reusing existing (sync behavior)")
            return existing_concept['_id']
        
        # SECOND: Check for path-order duplicates before inserting
        if concept_doc.path and concept_doc.order_key:
            # Build duplicate check query
            duplicate_query = {
                "company_cik": concept_doc.company_cik,
                "statement_type": concept_doc.statement_type,
                "path": concept_doc.path,
                "order_key": concept_doc.order_key,
                "dimension_concept": concept_doc.dimension_concept
            }
            
            # For dimensional concepts, also include segment_type in duplicate check
            if concept_doc.dimension_concept and hasattr(concept_doc, 'segment_type'):
                duplicate_query["segment_type"] = concept_doc.segment_type
            
            existing_path_order = self.collection.find_one(duplicate_query)
            
            if existing_path_order and existing_path_order.get('concept') != concept_doc.concept:
                raise ValueError(
                    f"Duplicate path-order combination detected! "
                    f"Path: {concept_doc.path}, Order: {concept_doc.order_key}, "
                    f"Segment: {getattr(concept_doc, 'segment_type', 'N/A')}. "
                    f"Existing concept: {existing_path_order.get('concept')}, "
                    f"New concept: {concept_doc.concept}. "
                    f"Use DuplicatePreventionManager.safe_insert_concept() to auto-adjust order_key."
                )
        
        result = self.collection.insert_one(concept_doc.to_dict())
        concept_type = "dimensional concept" if concept_doc.dimension_concept else "concept"
        logger.debug(f"Inserted new {concept_type} '{concept_doc.concept}' with ID {result.inserted_id}")
        return result.inserted_id


class ValueRepository:
    """Repository for concept values (includes both regular and dimensional values)."""
    
    def __init__(self, db_connection: DatabaseConnection, collection_name: str = 'concept_values'):
        self.db_connection = db_connection
        self.collection_name = collection_name
        self._collection: Optional[Collection] = None

    @property
    def collection(self) -> Collection:
        """Get values collection."""
        if self._collection is None:
            self._collection = self.db_connection.target_db[self.collection_name]
        return self._collection

    def find_existing_value(self, concept_id: ObjectId, reporting_period: Dict[str, Any], dimension_value: bool = False, dimensional_concept_id: Optional[ObjectId] = None) -> Optional[Dict[str, Any]]:
        """Find existing value to prevent duplicates."""
        query = {
            "concept_id": concept_id,
            "reporting_period": reporting_period,
            "dimension_value": dimension_value
        }
        
        if dimension_value and dimensional_concept_id is not None:
            query["dimensional_concept_id"] = dimensional_concept_id
        
        return self.collection.find_one(query)

    def find_dimensional_existing_value(self, dimensional_concept_id: ObjectId, reporting_period: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find existing dimensional value to prevent duplicates."""
        return self.collection.find_one({
            "dimensional_concept_id": dimensional_concept_id,
            "reporting_period": reporting_period,
            "dimension_value": True
        })

    def find_by_concept_id(self, concept_id: ObjectId, dimension_value: bool = False) -> Iterator[Dict[str, Any]]:
        """Find all values for a concept."""
        cursor = self.collection.find({"concept_id": concept_id, "dimension_value": dimension_value})
        for doc in cursor:
            yield doc

    def find_by_dimensional_concept_id(self, dimensional_concept_id: ObjectId) -> Iterator[Dict[str, Any]]:
        """Find all values for a specific dimensional concept."""
        cursor = self.collection.find({"dimensional_concept_id": dimensional_concept_id, "dimension_value": True})
        for doc in cursor:
            yield doc

    def insert(self, value_doc: ValueDocument) -> ObjectId:
        """Insert new value document."""
        result = self.collection.insert_one(value_doc.to_dict())
        return result.inserted_id


class CompanyRepository:
    """Repository for companies."""
    
    def __init__(self, db_connection: DatabaseConnection):
        self.db_connection = db_connection
        self._source_collection: Optional[Collection] = None
        self._target_collection: Optional[Collection] = None

    @property
    def source_collection(self) -> Collection:
        """Get source companies collection."""
        if self._source_collection is None:
            self._source_collection = self.db_connection.source_db['companies']
        return self._source_collection

    @property
    def target_collection(self) -> Collection:
        """Get target companies collection."""
        if self._target_collection is None:
            self._target_collection = self.db_connection.target_db['companies']
        return self._target_collection

    def find_all_source(self) -> Iterator[Company]:
        """Find all companies from source database."""
        cursor = self.source_collection.find({})
        for doc in cursor:
            yield Company.from_dict(doc)

    def insert_target(self, company: Company) -> ObjectId:
        """Insert company into target database."""
        result = self.target_collection.insert_one(company.to_dict())
        return result.inserted_id

    def count_target(self) -> int:
        """Count companies in target database."""
        return self.target_collection.count_documents({})

    def find_by_cik_target(self, cik: str) -> Optional[Company]:
        """Find company by CIK in target database."""
        doc = self.target_collection.find_one({"cik": cik})
        if doc:
            return Company.from_dict(doc)
        return None

    def find_by_ticker_source(self, ticker_symbol: str) -> Optional[Company]:
        """Find company by ticker symbol in source database."""
        doc = self.source_collection.find_one({"ticker_symbol": ticker_symbol.upper()})
        if doc:
            return Company.from_dict(doc)
        return None

    def find_by_ticker_target(self, ticker_symbol: str) -> Optional[Company]:
        """Find company by ticker symbol in target database."""
        doc = self.target_collection.find_one({"ticker_symbol": ticker_symbol.upper()})
        if doc:
            return Company.from_dict(doc)
        return None



