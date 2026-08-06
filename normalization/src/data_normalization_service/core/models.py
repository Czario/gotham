"""
Data models and DTOs for the normalization service.
"""
from datetime import datetime
from typing import Optional, Any, Dict
from dataclasses import dataclass
from bson import ObjectId


@dataclass
class ConceptKey:
    """Key for caching normalized concepts."""
    cik: str
    statement_type: str
    concept: str

    def __hash__(self):
        return hash((self.cik, self.statement_type, self.concept))





@dataclass
class ConceptDocument:
    """Represents a normalized concept document (includes both regular and dimensional concepts)."""
    company_cik: str
    statement_type: str
    concept: str
    form_type: Optional[str] = None  # 10-K, 10-Q, etc.
    label: Optional[str] = None
    canonical_concept: Optional[str] = None  # Cross-era canonical concept name
    path: Optional[str] = None  # Materialized path: "001.002.003"
    order_key: Optional[str] = None  # Lexicographic order: "a", "m", "z"
    abstract: bool = False
    dimension: bool = False
    # New flag to indicate if this is a dimensional concept
    dimension_concept: bool = False
    # Dimensional-specific fields (only used when dimension_concept=True)
    concept_id: Optional[ObjectId] = None  # Original concept_id for dimensional concepts
    segment_type: Optional[str] = None  # business, product_service, geographic, etc.
    
    # New dimensional data fields from updated source structure
    context_id: Optional[str] = None
    unit_id: Optional[str] = None
    period: Optional[str] = None
    concept_name: Optional[str] = None
    fact_label: Optional[str] = None
    dimensions: Optional[Dict[str, Any]] = None
    dimension_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB insertion."""
        result = {
            "cik": self.company_cik,
            "statement_type": self.statement_type,
            "concept": self.concept,
            "form_type": self.form_type,
            "label": self.label,
            "canonical_concept": self.canonical_concept or self.concept,
            "path": self.path,
            "order_key": self.order_key,
            "abstract": self.abstract,
            "dimension": self.dimension,
            "dimension_concept": self.dimension_concept
        }
        
        # Add dimensional-specific fields if this is a dimensional concept
        if self.dimension_concept:
            if self.concept_id is not None:
                result["concept_id"] = self.concept_id
            if self.segment_type is not None:
                result["segment_type"] = self.segment_type
            
            # Add new dimensional data fields if they have values
            new_dimensional_fields = {
                "context_id": self.context_id,
                "unit_id": self.unit_id,
                "period": self.period,
                "concept_name": self.concept_name,
                "fact_label": self.fact_label,
                "dimensions": self.dimensions,
                "dimension_details": self.dimension_details
            }
            
            for field_name, field_value in new_dimensional_fields.items():
                if field_value is not None:
                    result[field_name] = field_value
        
        return result

    def get_segment_identifier(self) -> str:
        """Get the primary segment identifier for dimensional concepts."""
        if self.dimension_concept:
            return self.concept or "unknown"
        return self.concept or "unknown"





@dataclass
class ValueDocument:
    """Represents a concept value document (includes both regular and dimensional values)."""
    concept_id: ObjectId
    company_cik: str
    statement_type: str
    form_type: str
    filing_id: Optional[ObjectId] = None  # Made optional - not needed in target db
    reporting_period: Optional[Dict[str, Any]] = None  # Period information without period_type
    value: Optional[Any] = None
    created_at: Optional[datetime] = None
    # New flag to indicate if this is a dimensional value
    dimension_value: bool = False
    # Dimensional-specific fields (only used when dimension_value=True)
    dimensional_concept_id: Optional[ObjectId] = None
    # Critical metadata fields to prevent data loss
    fact_id: Optional[str] = None           # CRITICAL for auditing and traceability
    decimals: Optional[str] = None          # CRITICAL for precision
    # Provenance: where this value came from. Defaults to primary XBRL statement
    # extraction; set to 'sec_companyfacts' for values recovered via the SEC
    # companyfacts API reconciliation pass.
    source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB insertion."""
        result = {
            "concept_id": self.concept_id,
            "cik": self.company_cik,
            "statement_type": self.statement_type,
            "form_type": self.form_type,
            "reporting_period": self.reporting_period,
            "value": self.value,
            "earning_data": True,
            "created_at": self.created_at,
            "dimension_value": self.dimension_value
        }
        
        # Only include filing_id if it's present (for backwards compatibility)
        if self.filing_id is not None:
            result["filing_id"] = self.filing_id
        
        # Add dimensional-specific fields if this is a dimensional value
        if self.dimension_value and self.dimensional_concept_id is not None:
            result["dimensional_concept_id"] = self.dimensional_concept_id

        return result





@dataclass
class Company:
    """Represents a company document."""
    id: ObjectId
    cik: str
    name: str
    ticker_symbol: Optional[str] = None
    corporate_info: Optional[Dict[str, Any]] = None
    industry: Optional[Dict[str, Any]] = None
    market_info: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Company':
        """Create instance from dictionary."""
        return cls(
            id=data.get('_id') or ObjectId(),
            cik=data['cik'],
            name=data['name'],
            ticker_symbol=data.get('ticker_symbol'),
            corporate_info=data.get('corporate_info'),
            industry=data.get('industry'),
            market_info=data.get('market_info'),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at')
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB insertion."""
        return {
            "_id": self.id,
            "cik": self.cik,
            "name": self.name,
            "ticker_symbol": self.ticker_symbol,
            "corporate_info": self.corporate_info,
            "industry": self.industry,
            "market_info": self.market_info,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }


@dataclass
class FinancialStatement:
    """Represents a financial statement document."""
    id: ObjectId
    company_cik: str
    filing_id: ObjectId
    statement_type: str
    reporting_period: datetime
    created_at: datetime
    financial_data: list

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FinancialStatement':
        """Create instance from dictionary."""
        return cls(
            id=data['_id'],
            company_cik=data['cik'],
            filing_id=data['filing_id'],
            statement_type=data['statement_type'],
            reporting_period=data['reporting_period'],
            created_at=data['created_at'],
            financial_data=data['data']  # Use 'data' field from database
        )


@dataclass
class Filing:
    """Represents a filing document."""
    id: ObjectId
    form_type: str
    accession_number: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Filing':
        """Create instance from dictionary."""
        return cls(
            id=data['_id'],
            form_type=data.get('form_type', 'UNKNOWN'),
            accession_number=data.get('accession_number')
        )
