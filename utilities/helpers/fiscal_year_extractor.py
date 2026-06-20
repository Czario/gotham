#!/usr/bin/env python3
"""
Centralized Fiscal Year Extraction Utility
Follows DRY principles - single source of truth for all fiscal year extraction logic
"""

import re
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple, Any
from .period_utils import FiscalYearCalculator

logger = logging.getLogger(__name__)


class FiscalYearExtractor:
    """
    Centralized fiscal year extraction from multiple sources with fallback chain.
    
    Extraction priority order:
    1. XBRL DocumentFiscalYearFocus field (if valid)
    2. Calculation from fiscal year end code + end date
    3. Pattern extraction from document URI/filename
    4. Calendar year from end date (last resort)
    
    This ensures maximum data completeness for all filing years, especially 2010-2013.
    """
    
    @classmethod
    def extract_fiscal_year_from_multiple_sources(
        cls,
        entity_info: Dict[str, Any],
        end_date: Optional[datetime] = None,
        document_uri: Optional[str] = None,
        company_info: Optional[Dict[str, Any]] = None
    ) -> Optional[int]:
        """
        Extract fiscal year trying multiple sources with fallback chain.
        
        Args:
            entity_info: Dictionary with XBRL entity information
            end_date: Document period end date
            document_uri: URI of the XBRL document
            company_info: Company information (may contain fiscalYearEnd)
            
        Returns:
            Fiscal year as integer, or None if absolutely no source available
        """
        
        # Method 1: XBRL DocumentFiscalYearFocus field
        fiscal_year = cls._extract_from_xbrl_field(entity_info)
        if fiscal_year:
            logger.debug(f"✓ Fiscal year from XBRL field: {fiscal_year}")
            return fiscal_year
        
        # Method 2: Calculate from fiscal year end code
        fiscal_year = cls._extract_from_calculation(entity_info, end_date, company_info)
        if fiscal_year:
            logger.debug(f"✓ Fiscal year from calculation: {fiscal_year}")
            return fiscal_year
        
        # Method 3: Extract from document URI pattern
        fiscal_year = cls._extract_from_uri_pattern(document_uri)
        if fiscal_year:
            logger.debug(f"✓ Fiscal year from URI pattern: {fiscal_year}")
            return fiscal_year
        
        # Method 4: Use calendar year from end date (last resort)
        fiscal_year = cls._extract_from_calendar_year(end_date)
        if fiscal_year:
            logger.warning(f"⚠ Using calendar year as fiscal year fallback: {fiscal_year}")
            return fiscal_year
        
        # No source available
        logger.error("✗ Could not extract fiscal year from any source")
        return None
    
    @classmethod
    def _extract_from_xbrl_field(cls, entity_info: Dict[str, Any]) -> Optional[int]:
        """
        Extract fiscal year from XBRL DocumentFiscalYearFocus field.
        
        Args:
            entity_info: Dictionary with XBRL entity information
            
        Returns:
            Fiscal year as integer if valid, None otherwise
        """
        xbrl_fiscal_year = entity_info.get('fiscal_year')
        
        if not xbrl_fiscal_year:
            return None
        
        try:
            fiscal_year_int = int(xbrl_fiscal_year)
            
            # Validate year is in reasonable range (1990-2100)
            if 1990 <= fiscal_year_int <= 2100:
                return fiscal_year_int
            else:
                logger.debug(f"Invalid XBRL fiscal year {xbrl_fiscal_year} (out of range)")
                return None
                
        except (ValueError, TypeError) as e:
            logger.debug(f"Invalid XBRL fiscal year format '{xbrl_fiscal_year}': {e}")
            return None
    
    @classmethod
    def _extract_from_calculation(
        cls,
        entity_info: Dict[str, Any],
        end_date: Optional[datetime],
        company_info: Optional[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Calculate fiscal year from fiscal year end code and document end date.
        
        Args:
            entity_info: Dictionary with XBRL entity information
            end_date: Document period end date
            company_info: Company information (may contain fiscalYearEnd)
            
        Returns:
            Calculated fiscal year as integer, or None if calculation fails
        """
        if not end_date:
            return None
        
        # Try to get fiscal year end code from multiple sources
        fiscal_year_end_code = (
            entity_info.get('fiscal_year_end') or
            (company_info.get('fiscalYearEnd') if company_info else None)
        )
        
        if not fiscal_year_end_code:
            return None
        
        try:
            fiscal_year, _ = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                end_date, fiscal_year_end_code
            )
            
            if fiscal_year:
                return fiscal_year
            else:
                logger.debug("Fiscal year calculation returned None")
                return None
                
        except Exception as e:
            logger.debug(f"Fiscal year calculation failed: {e}")
            return None
    
    @classmethod
    def _extract_from_uri_pattern(cls, document_uri: Optional[str]) -> Optional[int]:
        """
        Extract fiscal year from document URI/filename pattern.
        
        Common patterns:
        - /Archives/edgar/data/320193/0001193125-11-282113/...  (year: 2011)
        - .../aapl-20110924.xml  (year: 2011)
        - .../0001193125-11-282113-index.htm  (year: 2011)
        
        Args:
            document_uri: URI of the XBRL document
            
        Returns:
            Extracted fiscal year as integer, or None if pattern not found
        """
        if not document_uri:
            return None
        
        # Pattern 1: Look for YYYY-MM-DD date pattern
        date_pattern = re.search(r'[-_](\d{4})(\d{2})(\d{2})', document_uri)
        if date_pattern:
            year = int(date_pattern.group(1))
            if 1990 <= year <= 2100:
                return year
        
        # Pattern 2: Look for year in accession number (format: NNNNNNNNNN-YY-NNNNNN)
        accession_pattern = re.search(r'/(\d{10})-(\d{2})-(\d{6})', document_uri)
        if accession_pattern:
            year_short = int(accession_pattern.group(2))
            # Convert 2-digit year to 4-digit (00-50 = 2000-2050, 51-99 = 1951-1999)
            year = 2000 + year_short if year_short <= 50 else 1900 + year_short
            if 1990 <= year <= 2100:
                return year
        
        # Pattern 3: Look for standalone YYYY in path
        year_pattern = re.search(r'/(\d{4})/', document_uri)
        if year_pattern:
            year = int(year_pattern.group(1))
            if 1990 <= year <= 2100:
                return year
        
        return None
    
    @classmethod
    def _extract_from_calendar_year(cls, end_date: Optional[datetime]) -> Optional[int]:
        """
        Extract calendar year from end date as last resort fallback.
        
        Args:
            end_date: Document period end date
            
        Returns:
            Calendar year as integer, or None if end_date not available
        """
        if not end_date:
            return None
        
        return end_date.year
    
    @classmethod
    def extract_fiscal_year_with_metadata(
        cls,
        entity_info: Dict[str, Any],
        end_date: Optional[datetime] = None,
        document_uri: Optional[str] = None,
        company_info: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[int], str]:
        """
        Extract fiscal year and return source method for tracking/debugging.
        
        Args:
            entity_info: Dictionary with XBRL entity information
            end_date: Document period end date
            document_uri: URI of the XBRL document
            company_info: Company information (may contain fiscalYearEnd)
            
        Returns:
            Tuple of (fiscal_year, source_method)
        """
        
        # Try XBRL field
        fiscal_year = cls._extract_from_xbrl_field(entity_info)
        if fiscal_year:
            return fiscal_year, "xbrl_field"
        
        # Try calculation
        fiscal_year = cls._extract_from_calculation(entity_info, end_date, company_info)
        if fiscal_year:
            return fiscal_year, "calculated"
        
        # Try URI pattern
        fiscal_year = cls._extract_from_uri_pattern(document_uri)
        if fiscal_year:
            return fiscal_year, "uri_pattern"
        
        # Try calendar year fallback
        fiscal_year = cls._extract_from_calendar_year(end_date)
        if fiscal_year:
            return fiscal_year, "calendar_year_fallback"
        
        return None, "none_available"


class EntityInformationExtractor:
    """
    Centralized entity information extraction from XBRL facts.
    Collects all candidates and selects the best value (follows DRY principles).
    """
    
    @classmethod
    def extract_entity_info(
        cls,
        modelXbrl,
        entity_mapping: Dict[str, list],
        company_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Extract entity information from XBRL facts with improved candidate selection.
        
        Args:
            modelXbrl: Arelle XBRL model object
            entity_mapping: Mapping of attribute names to concept names
            company_info: Company information for fallback values
            
        Returns:
            Dictionary with extracted entity information
        """
        entity_info = {}
        entity_candidates = {}  # Store all candidates for each attribute
        
        # Collect all candidates from XBRL facts
        for fact in modelXbrl.facts:
            if fact.concept is not None and hasattr(fact.concept, 'name'):
                concept_name = fact.concept.name
                concept_qname = str(fact.concept.qname) if hasattr(fact.concept, 'qname') else ''
                
                for attr_name, concept_names in entity_mapping.items():
                    for target_concept in concept_names:
                        # Match both local name and full qname
                        if (concept_name == target_concept or 
                            target_concept in concept_qname or
                            concept_qname.endswith(':' + target_concept)):
                            
                            if attr_name not in entity_candidates:
                                entity_candidates[attr_name] = []
                            
                            # Store candidate with context for selection
                            entity_candidates[attr_name].append({
                                'value': fact.value,
                                'context': fact.context,
                                'fact': fact
                            })
        
        # Select best value for each attribute
        for attr_name, candidates in entity_candidates.items():
            best_value = cls._select_best_candidate(candidates)
            if best_value:
                entity_info[attr_name] = best_value
        
        # Add company_info fallbacks
        if company_info:
            if 'fiscalYearEnd' in company_info and 'fiscal_year_end' not in entity_info:
                entity_info['fiscal_year_end'] = company_info['fiscalYearEnd']
                logger.debug(f"Using company fiscal year end: {company_info['fiscalYearEnd']}")
        
        return entity_info
    
    @classmethod
    def _select_best_candidate(cls, candidates: list) -> Optional[str]:
        """
        Select the best candidate from multiple values.
        
        Selection criteria:
        1. Prefer non-null, non-empty values
        2. Prefer values without dimensions (consolidated data)
        3. Prefer most recent context
        
        Args:
            candidates: List of candidate dictionaries with value, context, fact
            
        Returns:
            Best candidate value as string, or None
        """
        if not candidates:
            return None
        
        # Filter to valid values only
        valid_candidates = [
            c for c in candidates 
            if c['value'] and str(c['value']).strip() and str(c['value']).strip().lower() not in ['none', 'null', '']
        ]
        
        if not valid_candidates:
            return None
        
        # Score each candidate
        scored_candidates = []
        for candidate in valid_candidates:
            score = 0
            
            # Prefer values without dimensions (consolidated data)
            if candidate['context'] is not None:
                if (hasattr(candidate['context'], 'qnameDims') and 
                    candidate['context'].qnameDims is not None and 
                    len(candidate['context'].qnameDims) == 0):
                    score += 10
                
                # Prefer more recent contexts (if instant period)
                if hasattr(candidate['context'], 'instantDatetime') and candidate['context'].instantDatetime:
                    # Use timestamp as score component
                    score += candidate['context'].instantDatetime.timestamp() / 1e10
            
            scored_candidates.append((score, candidate['value']))
        
        # Sort by score (highest first) and return best
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        return str(scored_candidates[0][1])
