import sys
import os
import time
import json
import traceback
import logging
import requests
from typing import Dict, List, Optional, Any, Set, Tuple
from datetime import datetime
from decimal import Decimal
import pandas as pd
from dataclasses import dataclass, field, asdict
import re
from bs4 import BeautifulSoup


# Custom filter to suppress Arelle transformation namespace warnings
class ArelleTransformationWarningFilter(logging.Filter):
    """Filter out noisy Arelle warnings that don't affect extraction"""
    
    def filter(self, record):
        message = record.getMessage()
        
        # Suppress invalidTransformation warnings - they're harmless and clutter output
        if 'invalidTransformation' in message:
            return False
        # Suppress messages about unrecognized transformation namespace
        if 'unrecognized transformation namespace' in message:
            return False
        # Suppress resourceIdDuplication warnings - duplicate IDs in inline XBRL (common, harmless)
        if 'resourceIdDuplication' in message:
            return False
        # Suppress xmlSchema:syntax warnings - malformed HTML in old filings (doesn't prevent extraction)
        if 'xmlSchema:syntax' in message:
            return False
        # Suppress "Opening and ending tag mismatch" - old filing HTML issues
        if 'Opening and ending tag mismatch' in message:
            return False
        
        return True


logger = logging.getLogger(__name__)

# Optional imports for Excel export
try:
    from openpyxl import Workbook
    from openpyxl.utils.dataframe import dataframe_to_rows
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.formatting.rule import ColorScaleRule
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False
    logger.debug("Excel export dependencies not available. Only JSON export will work.")

# The arelle-release package is the canonical Arelle installation; no manual path needed.

from arelle import Cntlr, ModelManager

# Import SEC URL Detector for intelligent XBRL file discovery
from utilities.sec_url_detector import SECURLDetector

# Import enhanced dimensional extractor
try:
    from core.extractors.dimensional_data_extractor import enhance_dimensional_extraction, EnhancedDimensionalExtractor
    ENHANCED_DIMENSIONAL_AVAILABLE = True
    logger.debug("Enhanced dimensional extractor available")
except ImportError as e:
    ENHANCED_DIMENSIONAL_AVAILABLE = False
    logger.debug(f"Enhanced dimensional extractor not available: {e}")
from arelle.ModelValue import qname
from arelle import XbrlConst

@dataclass
class DimensionalContext:
    """Represents dimensional context information"""
    axis: str                   # Dimension axis name
    member: str                 # Dimension member value
    label: str                  # Human-readable label

@dataclass
class FinancialLineItem:
    """Represents a single line item in financial statement"""
    concept_name: str           # XBRL concept QName
    label: str                  # Human-readable label
    value: Optional[float]      # Extracted numeric value
    context_id: str            # XBRL context reference
    unit_id: Optional[str]     # Unit of measurement
    period: str                # Reporting period
    level: int                 # Hierarchy depth (0 = root)
    order: float               # Presentation order
    abstract: bool             # Whether concept is abstract
    parent_concept: Optional[str] = None
    dimensions: List[DimensionalContext] = field(default_factory=list)  # Dimensional breakdowns
    all_dimensional_facts: List[Dict[str, Any]] = field(default_factory=list)  # All facts with dimensions
    calculations: Dict[str, Any] = field(default_factory=dict)  # Calculation relationships
    children: List['FinancialLineItem'] = field(default_factory=list)

def _filing_date_from_accession(accession_number: str) -> Optional[str]:
    """Approximate a filing date from an SEC accession number.

    Accessions are ``{filer}-{YY}-{serial}``; the serial is NOT a date.  Returns
    Jan 1 of the accession's year (the URL detector only needs pre/post-2019),
    or ``None`` when the accession does not match.
    """
    match = re.search(r"-(\d{2})-\d{6}$", str(accession_number or ""))
    if not match:
        return None
    return f"20{match.group(1)}-01-01"


class FlexibleXBRLExtractor:
    """Enhanced XBRL extractor supporting multiple taxonomies and international standards"""
    
    def __init__(self, taxonomy_preference: Optional[List[str]] = None, enable_enhanced_dimensions: bool = True, company_info: Optional[Dict] = None, filing_form_type: Optional[str] = None, filing_date: Optional[str] = None):
        """Initialize with taxonomy preference order and enhanced dimensional support"""
        
        # Initialize Arelle controller normally
        self.ctrl = Cntlr.Cntlr()
        self.model_manager = ModelManager.ModelManager(self.ctrl)
        
        # Suppress noisy Arelle warnings - apply comprehensive filtering
        for logger_name in ['arelle', 'arelle.ModelXbrl', 'arelle.ModelDocument', 'arelle.ValidateInlineXBRL']:
            arelle_logger = logging.getLogger(logger_name)
            arelle_logger.addFilter(ArelleTransformationWarningFilter())
            arelle_logger.setLevel(logging.CRITICAL)  # Only show critical errors, suppress all warnings
            # Remove all handlers to prevent output
            arelle_logger.handlers = []
            arelle_logger.propagate = False  # Prevent propagation to parent loggers
        
        # Initialize SEC URL Detector for intelligent XBRL file discovery
        self.url_detector = SECURLDetector()

        # Default taxonomy preference: US-GAAP, IFRS, then any others
        if taxonomy_preference is None:
            self.taxonomy_preference = ['us-gaap', 'ifrs-full', 'dei', 'country', 'srt', 'invest']
        else:
            self.taxonomy_preference = taxonomy_preference

        # Enhanced dimensional extraction support
        self.enable_enhanced_dimensions = enable_enhanced_dimensions and ENHANCED_DIMENSIONAL_AVAILABLE
        self.enhanced_dimensional_extractor = None

        # Company information for fiscal year calculations
        self.company_info = company_info or {}

        # Filing form type for period filtering (10-Q, 10-K, etc.)
        self.target_form_type = filing_form_type

        # Authoritative filing date (YYYY-MM-DD) from the SEC submissions API.
        # When absent the date is approximated from the accession number.
        self.filing_date = filing_date

        # Cache for discovered taxonomies and concepts
        self.discovered_taxonomies = set()
        self.statement_concepts_cache = {}
        # Ensure statements container always exists (used later for fallback presentation concept collection)
        self.statements = {}
        
        # Flag for older filings (pre-2015) to enable relaxed extraction rules
        self.is_older_filing = False

        if self.enable_enhanced_dimensions:
            logger.debug("Enhanced dimensional extraction enabled")
        else:
            logger.debug("Standard dimensional extraction enabled")

        if self.target_form_type:
            logger.debug(f"Filing form type: {self.target_form_type} - applying strict period filtering")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Cleanup if needed
        pass
    
    def _resolve_optimal_xbrl_url(self, filing_url: str) -> str:
        """
        Intelligently resolve the optimal XBRL URL using SECURLDetector.
        Extracts CIK, accession number, and filing date from the URL, then uses
        the enhanced URL detector to find the best XBRL instance file.
        """
        import re
        from urllib.parse import urlparse
        
        logger.debug(f"Resolving optimal XBRL URL for: {filing_url}")
        
        # Reset the "no XBRL anywhere" signal for this filing. It is set to True only
        # when the filing has no real XBRL instance and no companion amendment supplies
        # one (i.e. a genuine pre-XBRL-era filing), so the caller can skip it cleanly.
        self._no_xbrl_available = False
        
        try:
            # Extract CIK and accession number from URL
            url_parts = filing_url.split('/')
            cik = None
            accession_number = None
            
            # Find CIK and accession number from URL path
            # Pattern: .../edgar/data/{CIK}/{accession_no_dashes}/...
            for i, part in enumerate(url_parts):
                if 'data' in part and i + 1 < len(url_parts):
                    cik = url_parts[i + 1]
                    # The next part after CIK should be the accession number (18 digits without dashes)
                    if i + 2 < len(url_parts):
                        accession_part = url_parts[i + 2]
                        if re.match(r'^\d{18}$', accession_part):  # Exactly 18 digits, no extension
                            accession_number = f"{accession_part[:10]}-{accession_part[10:12]}-{accession_part[12:18]}"
                            break
            
            if not cik or not accession_number:
                logger.debug(f"Could not extract CIK or accession number from URL: {filing_url}")
                return filing_url
            
            # Filing date: prefer the authoritative value from the SEC API.
            # The accession is ``{filer}-{YY}-{serial}``; the serial is NOT a
            # date.  A previous regex matched the CIK/accession boundary and
            # produced a bogus year (e.g. 2000 for a 2020 filing), which sent
            # modern filings down the legacy path and falsely reported
            # "no XBRL".  Use the real date, or at worst Jan 1 of the
            # accession's year (the detector only needs pre/post-2019).
            filing_date = self.filing_date
            if not filing_date:
                filing_date = _filing_date_from_accession(accession_number)
            
            # Use SECURLDetector to find the optimal XBRL file
            logger.debug(f"Using SECURLDetector for CIK={cik}, Accession={accession_number}")
            detected_urls = self.url_detector.detect_filing_urls(cik, accession_number, filing_date or "")
            
            if detected_urls and isinstance(detected_urls, dict):
                xbrl_url = detected_urls.get('xbrl_url')

                # Store the full ordered candidate list for fallback use in extract_financial_statements
                self._xbrl_candidates = [
                    u for u in detected_urls.get('xbrl_candidates', [])
                    if isinstance(u, str)
                ]

                # If the detector resolved a real XBRL instance, use it directly.
                if xbrl_url and isinstance(xbrl_url, str) and not self.url_detector.is_txt_fallback(detected_urls):
                    logger.info(f"✅ SECURLDetector found optimal XBRL file: {xbrl_url}")
                    return xbrl_url

                # No real XBRL instance in this accession (only the .txt fallback).
                # This is the 2009-2012 grace-period pattern: the readable 10-Q/10-K was
                # filed first and the XBRL exhibits arrived in a separate /A amendment.
                # Try to recover the XBRL from the companion amendment.
                logger.info(
                    f"No XBRL instance in accession {accession_number}; "
                    f"checking for a companion amendment (10-Q/A, 10-K/A) with XBRL..."
                )
                amendment_xbrl_url = self.url_detector.find_amendment_xbrl_url(cik, accession_number)
                if amendment_xbrl_url:
                    return amendment_xbrl_url

                # No real XBRL instance and no companion amendment with XBRL: this filing
                # genuinely has no XBRL (e.g. pre-2012 filings that predate the XBRL mandate).
                # Signal the caller so it can skip cleanly instead of attempting a doomed parse.
                self._no_xbrl_available = True

                if xbrl_url and isinstance(xbrl_url, str):
                    logger.warning(
                        f"No XBRL found for {accession_number} (and no companion amendment); "
                        f"this filing appears to predate XBRL"
                    )
                    return xbrl_url

                logger.warning(f"SECURLDetector returned no xbrl_url, using original URL: {filing_url}")
                return filing_url
            else:
                logger.warning(f"SECURLDetector returned unexpected result, using original URL: {filing_url}")
                return filing_url
                
        except Exception as e:
            logger.error(f"Error in URL resolution: {e}")
            logger.debug(f"Falling back to original URL: {filing_url}")
            return filing_url
    
    def extract_financial_statements(self, filing_url: str, filing_form_type: Optional[str] = None) -> Dict[str, Any]:
        """Extract all financial statements with hierarchical structure and enhanced dimensional support"""
        
        start_time = time.time()
        
        try:
            # Intelligently resolve the optimal XBRL URL
            optimal_url = self._resolve_optimal_xbrl_url(filing_url)
            
            # If resolution determined this filing genuinely has no XBRL (pre-XBRL era,
            # no instance in the accession and no companion amendment), skip the doomed
            # Arelle load and return a clear signal for the caller.
            if getattr(self, '_no_xbrl_available', False):
                logger.warning(
                    f"⏭️  No XBRL data available for filing at {filing_url} "
                    f"(predates XBRL or no XBRL exhibits filed); skipping extraction"
                )
                return {
                    'filing_info': {'original_url': filing_url},
                    'statements': {},
                    'no_xbrl_available': True,
                    'discovered_taxonomies': [],
                    'processing_time': time.time() - start_time
                }
            
            # Load XBRL document, trying each validated candidate in priority order
            # until one produces actual facts.  Most filings need only the first try;
            # the fallback handles cases like WFC 10-K where the primary .htm is a
            # narrative document and the data lives in the _htm.xml extracted instance.
            candidates_to_try = list(getattr(self, '_xbrl_candidates', []))
            # Ensure the resolved optimal_url is always the first candidate tried
            if optimal_url not in candidates_to_try:
                candidates_to_try.insert(0, optimal_url)

            modelXbrl = None
            used_url = optimal_url
            for attempt, candidate_url in enumerate(candidates_to_try):
                logger.debug(f"Loading XBRL document (attempt {attempt + 1}/{len(candidates_to_try)}): {candidate_url}")
                # Transient SEC failures (503/network) are common on long
                # backfills; retry before abandoning a candidate.
                model = None
                for _retry in range(3):
                    try:
                        model = self.model_manager.load(candidate_url)
                    except Exception as load_exc:  # noqa: BLE001
                        logger.warning(f"   Arelle load error ({load_exc}) for {candidate_url}")
                        model = None
                    if model:
                        break
                    if _retry < 2:
                        time.sleep(1.5 * (_retry + 1))
                if not model:
                    logger.warning(f"   Arelle failed to load: {candidate_url}")
                    continue
                fact_count = len(model.facts) if hasattr(model, 'facts') else -1
                if fact_count == 0 and attempt < len(candidates_to_try) - 1:
                    logger.warning(
                        f"   ⚠️  {candidate_url.split('/')[-1]} loaded but has 0 facts — "
                        f"trying next candidate..."
                    )
                    continue
                modelXbrl = model
                used_url = candidate_url
                if attempt > 0:
                    logger.info(f"   ✅ Fallback candidate succeeded: {candidate_url.split('/')[-1]} "
                                f"({fact_count} facts)")
                break

            if not modelXbrl:
                raise Exception("Failed to load XBRL filing from any candidate")

            if used_url != optimal_url:
                logger.info(f"🔄 Using fallback XBRL source: {used_url}")

            # Store modelXbrl reference for use in dimensional extraction
            self.current_model = modelXbrl

            # Reset dimensional enhancement cache for new filing
            if hasattr(self, '_dimensional_enhancement_completed'):
                delattr(self, '_dimensional_enhancement_completed')
            if hasattr(self, '_enhanced_dimensional_results'):
                delattr(self, '_enhanced_dimensional_results')
            
            # Discover available taxonomies
            self._discover_taxonomies(modelXbrl)
            
            # Extract filing information
            filing_info = self._extract_filing_info(modelXbrl)
            filing_info['resolved_url'] = used_url     # Track which URL was actually used
            filing_info['original_url'] = filing_url   # Track original input URL
            
            # Extract dimensional analysis summary
            dimensional_summary = self._extract_dimensional_analysis_summary(modelXbrl)
            filing_info['dimensional_analysis'] = dimensional_summary
            
            # Extract each financial statement
            statements = {}
            
            # Find financial statement presentation roles using flexible approach
            statement_roles = self._identify_statement_roles_flexible(modelXbrl)
            
            logger.info(f"Identified {len(statement_roles)} statement roles:")
            for role_uri, role_info in statement_roles.items():
                logger.info(f"  - {role_info['statement_type']}: {role_uri[:80]}...")
            
            for role_uri, role_info in statement_roles.items():
                # Get role-specific presentation relationships
                # Use role_info['role_uri'] instead of role_uri because for income_statement,
                # the dict key is synthetic (role_uri + '_income') but we need the actual XBRL role URI
                actual_role_uri = role_info['role_uri']
                logger.info(f"🔍 Processing role for {role_info['statement_type']}: dict_key={role_uri[:80]}, actual_uri={actual_role_uri[:80]}")
                role_pres_rel_set = modelXbrl.relationshipSet(XbrlConst.parentChild, actual_role_uri)
                
                # Extract hierarchical structure
                statement_hierarchy = self._extract_statement_hierarchy(
                    modelXbrl, role_pres_rel_set, role_info
                )
                logger.info(f"📊 Extracted hierarchy for {role_info['statement_type']}: {len(statement_hierarchy)} items")
                
                statements[role_info['statement_type']] = {
                    'role_uri': actual_role_uri,
                    'description': role_info['description'],
                    'hierarchy': statement_hierarchy,
                    'taxonomy_info': role_info.get('taxonomy_info', {})
                }
            
            processing_time = time.time() - start_time
            
            # Add missing dimensional concepts to statements if enhanced extraction was performed
            missing_dimensional_concepts = []
            if (hasattr(self, '_enhanced_dimensional_results') and 
                self._enhanced_dimensional_results and 
                'missing_concept_line_items' in self._enhanced_dimensional_results):
                missing_dimensional_concepts = self._enhanced_dimensional_results['missing_concept_line_items']
                
                # Create a special section for missing dimensional concepts
                if missing_dimensional_concepts:
                    statements['missing_dimensional_concepts'] = {
                        'role_uri': 'enhanced_discovery',
                        'description': 'Concepts with dimensional data not found in standard presentation relationships',
                        'hierarchy': missing_dimensional_concepts,
                        'taxonomy_info': {
                            'source': 'enhanced_dimensional_extraction',
                            'discovery_note': 'These concepts contain dimensional data but were not included in standard financial statement presentations'
                        }
                    }

            # Step 2a metrics: surface how much data lives outside the presentation
            # tree so coverage gains from the segment/disclosure capture are visible.
            if missing_dimensional_concepts:
                _facts_total = sum(li.get('fact_count', 0) for li in missing_dimensional_concepts)
                _with_values = sum(1 for li in missing_dimensional_concepts if li.get('value') is not None)
                _dim_facts = sum(li.get('dimensional_fact_count', 0) for li in missing_dimensional_concepts)
                logger.info(
                    f"📈 Outside-presentation discovery: {len(missing_dimensional_concepts)} concepts "
                    f"({_with_values} with primary values), {_facts_total} total facts "
                    f"({_dim_facts} dimensional) captured for segment/disclosure storage"
                )
            
            result = {
                'filing_info': filing_info,
                'statements': statements,
                'discovered_taxonomies': list(self.discovered_taxonomies),
                'processing_time': processing_time
            }
            
            # Add enhanced dimensional results summary if available
            if hasattr(self, '_enhanced_dimensional_results') and self._enhanced_dimensional_results:
                result['enhanced_extraction_summary'] = {
                    'total_enhanced_facts': len(self._enhanced_dimensional_results.get('enhanced_facts', [])),
                    'missing_concepts_discovered': len(missing_dimensional_concepts),
                    'dimensional_structure_discovered': bool(self._enhanced_dimensional_results.get('dimensional_structure')),
                    'missing_analysis_performed': bool(self._enhanced_dimensional_results.get('missing_analysis'))
                }
            
            return result
            
        except Exception as e:
            raise Exception(f"Extraction failed: {str(e)}")
        finally:
            # Clean up model reference
            self.current_model = None
    
    def _discover_taxonomies(self, modelXbrl):
        """Discover available taxonomies in the filing with support for older versions (2009-2013)"""
        
        # Track taxonomy years for older filing detection
        taxonomy_years = set()
        
        # Check concepts to identify taxonomies
        for concept in modelXbrl.qnameConcepts.values():
            if hasattr(concept, 'qname') and concept.qname:
                namespace = concept.qname.namespaceURI
                if namespace:
                    namespace_lower = namespace.lower()  # Case-insensitive matching
                    
                    # Extract taxonomy identifier from namespace with case-insensitive matching
                    if 'us-gaap' in namespace_lower:
                        self.discovered_taxonomies.add('us-gaap')
                    elif 'ifrs' in namespace_lower:
                        self.discovered_taxonomies.add('ifrs-full')
                    elif 'dei' in namespace_lower:
                        self.discovered_taxonomies.add('dei')
                    elif 'srt' in namespace_lower:
                        self.discovered_taxonomies.add('srt')
                    elif 'country' in namespace_lower:
                        self.discovered_taxonomies.add('country')
                    elif 'invest' in namespace_lower:
                        self.discovered_taxonomies.add('invest')
                    # Add custom/company-specific taxonomies
                    elif any(indicator in namespace_lower for indicator in ['extension', 'company', 'custom']):
                        self.discovered_taxonomies.add('company-extension')
                    
                    # Extract year from namespace for version tracking (e.g., /2009-01-31, /2010/, /2013/)
                    year_match = re.search(r'/(\d{4})[/-]', namespace)
                    if year_match:
                        taxonomy_years.add(int(year_match.group(1)))
        
        # Log taxonomy information for debugging older filings
        if taxonomy_years:
            min_year = min(taxonomy_years)
            max_year = max(taxonomy_years)
            logger.debug(f"Discovered taxonomies: {', '.join(self.discovered_taxonomies)}")
            logger.debug(f"Taxonomy years detected: {min_year}-{max_year}")
            
            # Flag if this is an older filing (pre-2015) for special handling
            if min_year < 2015:
                logger.debug(f"⚠ Older taxonomy detected ({min_year}) - applying relaxed extraction rules")
                self.is_older_filing = True
        else:
            logger.debug(f"Discovered taxonomies: {', '.join(self.discovered_taxonomies)}")
    
    def _extract_dimensional_analysis_summary(self, modelXbrl) -> Dict[str, Any]:
        """Extract comprehensive dimensional analysis using Arelle's advanced capabilities"""
        
        try:
            dimensional_info = {
                'total_facts': len(modelXbrl.facts),
                'dimensional_facts': 0,
                'non_dimensional_facts': 0,
                'unique_dimensions': set(),
                'unique_members': set(),
                'dimension_combinations': {},
                'hypercubes': {},
                'dimension_defaults': {}
            }
            
            # Analyze all facts for dimensional content
            for fact in modelXbrl.facts:
                if (fact.context is not None and 
                    hasattr(fact.context, 'qnameDims') and 
                    fact.context.qnameDims is not None and 
                    len(fact.context.qnameDims) > 0):
                    
                    dimensional_info['dimensional_facts'] += 1
                    
                    # Collect dimension information
                    for dim_qname, dim_value in fact.context.qnameDims.items():
                        dimensional_info['unique_dimensions'].add(str(dim_qname))
                        
                        if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit:
                            if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                                dimensional_info['unique_members'].add(str(dim_value.memberQname))
                else:
                    dimensional_info['non_dimensional_facts'] += 1
            
            # Analyze dimension-domain relationships using Arelle's definition linkbase
            try:
                if hasattr(modelXbrl, 'relationshipSet'):
                    # Get dimension-domain relationships
                    dim_domain_rel_set = modelXbrl.relationshipSet(XbrlConst.dimensionDomain)
                    if dim_domain_rel_set:
                        for rel in dim_domain_rel_set.modelRelationships:
                            if hasattr(rel, 'fromModelObject') and hasattr(rel, 'toModelObject'):
                                dim_name = str(rel.fromModelObject.qname) if rel.fromModelObject is not None else "unknown"
                                domain_name = str(rel.toModelObject.qname) if rel.toModelObject is not None else "unknown"
                                
                                if dim_name not in dimensional_info['dimension_combinations']:
                                    dimensional_info['dimension_combinations'][dim_name] = []
                                dimensional_info['dimension_combinations'][dim_name].append(domain_name)
                    
                    # Get hypercube relationships
                    all_rel_set = modelXbrl.relationshipSet(XbrlConst.all)
                    if all_rel_set:
                        for rel in all_rel_set.modelRelationships:
                            if hasattr(rel, 'fromModelObject') and hasattr(rel, 'toModelObject'):
                                primary_name = str(rel.fromModelObject.qname) if rel.fromModelObject is not None else "unknown"
                                hypercube_name = str(rel.toModelObject.qname) if rel.toModelObject is not None else "unknown"
                                
                                if primary_name not in dimensional_info['hypercubes']:
                                    dimensional_info['hypercubes'][primary_name] = []
                                dimensional_info['hypercubes'][primary_name].append(hypercube_name)
                    
                    # Get dimension defaults
                    dim_default_rel_set = modelXbrl.relationshipSet(XbrlConst.dimensionDefault)
                    if dim_default_rel_set:
                        for rel in dim_default_rel_set.modelRelationships:
                            if hasattr(rel, 'fromModelObject') and hasattr(rel, 'toModelObject'):
                                dim_name = str(rel.fromModelObject.qname) if rel.fromModelObject is not None else "unknown"
                                default_name = str(rel.toModelObject.qname) if rel.toModelObject is not None else "unknown"
                                dimensional_info['dimension_defaults'][dim_name] = default_name
                                
            except Exception as e:
                logger.debug(f"Advanced dimensional analysis failed: {e}")
            
            # Convert sets to lists for JSON serialization
            dimensional_info['unique_dimensions'] = list(dimensional_info['unique_dimensions'])
            dimensional_info['unique_members'] = list(dimensional_info['unique_members'])
            
            # Calculate percentages
            if dimensional_info['total_facts'] > 0:
                dimensional_info['dimensional_fact_percentage'] = round(
                    (dimensional_info['dimensional_facts'] / dimensional_info['total_facts']) * 100, 2
                )
            else:
                dimensional_info['dimensional_fact_percentage'] = 0
            
            logger.debug(f"Dimensional Analysis: {dimensional_info['dimensional_facts']}/{dimensional_info['total_facts']} "
                  f"facts have dimensions ({dimensional_info['dimensional_fact_percentage']}%)")
            logger.debug(f"Found {len(dimensional_info['unique_dimensions'])} unique dimensions, "
                  f"{len(dimensional_info['unique_members'])} unique members")
                  
            return dimensional_info
            
        except Exception as e:
            logger.debug(f"Error in dimensional analysis: {e}")
            return {
                'total_facts': len(modelXbrl.facts) if hasattr(modelXbrl, 'facts') else 0,
                'dimensional_facts': 0,
                'error': str(e)
            }
    
    def _extract_filing_info(self, modelXbrl) -> Dict[str, Any]:
        """Extract comprehensive filing information with flexible taxonomy support and primary period detection"""
        
        entity_info = {}
        primary_period_info = {}
        
        # Flexible entity mapping supporting multiple taxonomies
        entity_mapping = {
            'entity_name': ['EntityRegistrantName', 'EntityPublicName', 'NameOfReportingEntityOrOtherMeansOfIdentification'],
            'entity_cik': ['EntityCentralIndexKey', 'EntityIdentifier'],
            'document_type': ['DocumentType', 'TypeOfReportingEntityDocumentType'],
            'document_period_end': ['DocumentPeriodEndDate', 'PeriodEnd'],
            'fiscal_year': ['DocumentFiscalYearFocus', 'FiscalYear'],
            'fiscal_period': ['DocumentFiscalPeriodFocus', 'FiscalPeriod'],
            'currency': ['ReportingCurrencyISO', 'FunctionalCurrency', 'ReportingCurrency']
        }
        
        # Use centralized entity extraction (DRY principle)
        from utilities.helpers.fiscal_year_extractor import EntityInformationExtractor
        entity_info = EntityInformationExtractor.extract_entity_info(
            modelXbrl, entity_mapping, self.company_info
        )
        
        # Extract primary period information directly from XBRL contexts
        primary_period_info = self._extract_primary_period_from_contexts(modelXbrl, entity_info)
        
        # Safely extract document URI (modelDocument can be None in some cases)
        document_uri = None
        if hasattr(modelXbrl, 'modelDocument') and modelXbrl.modelDocument is not None:
            document_uri = modelXbrl.modelDocument.uri
        
        return {
            'document_uri': document_uri,
            'entity_info': entity_info,
            'primary_period_info': primary_period_info,
            'discovered_taxonomies': list(self.discovered_taxonomies)
        }
    
    def _extract_primary_period_from_contexts(self, modelXbrl, entity_info: Dict[str, Any]) -> Dict[str, Any]:
        """Extract primary period information directly from XBRL contexts"""
        
        from datetime import datetime
        
        # Validate document_period_end looks like YYYY-MM-DD before trusting it.
        # Old XBRL filings (pre-2012) may have numeric facts whose qname contains
        # 'PeriodEnd' as a substring (e.g. share counts), causing garbage values.
        _raw_doc_period = entity_info.get('document_period_end')
        if _raw_doc_period and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(_raw_doc_period).strip()):
            logger.warning(f"Ignoring non-date document_period_end from XBRL entity info: {_raw_doc_period!r}")
            _raw_doc_period = None

        primary_period_info = {
            'instant_period': None,           # For balance sheet (point in time)
            'duration_period': None,         # For income statement, cash flow (period)
            'fiscal_year_end': None,         # Fiscal year end date
            'fiscal_year': None,             # Fiscal year number
            'document_period_end': _raw_doc_period,
            'fiscal_period': entity_info.get('fiscal_period')
        }
        
        # PRIORITY 1: Use fiscal year/period directly from XBRL if available
        xbrl_fiscal_year = entity_info.get('fiscal_year')
        xbrl_fiscal_period = entity_info.get('fiscal_period')
        
        if xbrl_fiscal_year:
            try:
                # Validate and use XBRL fiscal year
                fiscal_year_int = int(xbrl_fiscal_year)
                if 1900 <= fiscal_year_int <= 2100:  # Reasonable range check
                    primary_period_info['fiscal_year'] = fiscal_year_int
                    logger.debug(f"Using XBRL fiscal year: {fiscal_year_int}")
                else:
                    logger.debug(f"Invalid XBRL fiscal year {xbrl_fiscal_year}, will calculate")
            except (ValueError, TypeError):
                logger.debug(f"Invalid XBRL fiscal year format {xbrl_fiscal_year}, will calculate")
        
        if xbrl_fiscal_period:
            logger.debug(f"Using XBRL fiscal period: {xbrl_fiscal_period}")
            # fiscal_period is already set above
        
        # Parse document period end for reference
        doc_period_end = None
        if primary_period_info['document_period_end']:
            try:
                doc_period_end = datetime.strptime(primary_period_info['document_period_end'], "%Y-%m-%d")
                primary_period_info['fiscal_year_end'] = primary_period_info['document_period_end']
            except ValueError:
                pass
        
        # Analyze all contexts to find the primary periods
        instant_contexts = []
        duration_contexts = []
        
        for context in modelXbrl.contexts.values():
            if not hasattr(context, 'period'):
                continue
                
            # Skip contexts with dimensions (we want consolidated data)
            if (hasattr(context, 'qnameDims') and 
                context.qnameDims is not None and 
                len(context.qnameDims) > 0):
                continue
            
            try:
                if hasattr(context, 'isInstantPeriod') and context.isInstantPeriod:
                    # Instant period (balance sheet date)
                    instant_date = context.instantDatetime
                    if instant_date:
                        instant_contexts.append((str(instant_date), instant_date, context.id))
                
                elif hasattr(context, 'isStartEndPeriod') and context.isStartEndPeriod:
                    # Duration period (income statement, cash flow period)
                    start_date = context.startDatetime
                    end_date = context.endDatetime
                    if start_date and end_date:
                        duration_days = (end_date - start_date).days
                        period_str = f"{start_date} to {end_date}"
                        duration_contexts.append((period_str, start_date, end_date, duration_days, context.id))
            except:
                continue
        
        # Find the primary instant period (most recent, matching fiscal year end)
        if instant_contexts and doc_period_end:
            # Sort by date, most recent first
            instant_contexts.sort(key=lambda x: x[1], reverse=True)
            
            # Find the instant period that matches or is closest to document period end
            best_instant = None
            min_days_diff = float('inf')
            
            for period_str, instant_date, context_id in instant_contexts:
                days_diff = abs((instant_date.date() - doc_period_end.date()).days)
                if days_diff <= 2:  # Allow 1-2 days difference
                    best_instant = period_str
                    logger.debug(f"Primary instant period found: {best_instant} (context: {context_id})")
                    break
                elif days_diff < min_days_diff:
                    min_days_diff = days_diff
                    best_instant = period_str
            
            primary_period_info['instant_period'] = best_instant
        
        # Find the primary duration period (fiscal year ending around document period end)
        if duration_contexts and doc_period_end:
            # Determine target duration based on filing form type
            filing_form_type = getattr(self, 'filing_form_type', None)
            target_days_min = 350 if filing_form_type == '10-K' else 80  # Annual vs Quarterly
            target_days_max = 370 if filing_form_type == '10-K' else 100
            
            # Look for periods matching the filing type and ending around document period end
            matching_periods = []
            
            for period_str, start_date, end_date, duration_days, context_id in duration_contexts:
                # Check if period duration matches filing type expectations
                is_duration_match = target_days_min <= duration_days <= target_days_max
                
                # For 10-K, also allow slightly longer periods (up to 400 days for fiscal years)
                if filing_form_type == '10-K' and 350 <= duration_days <= 400:
                    is_duration_match = True
                    
                # For 10-Q, be more strict about quarterly periods (80-100 days)
                elif filing_form_type == '10-Q' and 80 <= duration_days <= 100:
                    is_duration_match = True
                
                if is_duration_match:
                    days_diff = abs((end_date.date() - doc_period_end.date()).days)
                    # Only consider periods ending within 7 days of document period end
                    if days_diff <= 7:
                        matching_periods.append((period_str, start_date, end_date, days_diff, context_id, duration_days))
            
            if matching_periods:
                # Sort by how close the end date is to document period end, then by duration appropriateness
                matching_periods.sort(key=lambda x: (x[3], abs(x[5] - (365 if filing_form_type == '10-K' else 90))))
                
                best_duration = matching_periods[0][0]  # period_str
                best_context_id = matching_periods[0][4]
                best_duration_days = matching_periods[0][5]
                logger.debug(f"Primary duration period found: {best_duration} ({best_duration_days} days, context: {best_context_id})")
                primary_period_info['duration_period'] = best_duration
            else:
                # Fallback: Look for annual periods (350-370 days) ending around document period end
                annual_periods = []
                
                for period_str, start_date, end_date, duration_days, context_id in duration_contexts:
                    # Consider as annual if between 350-370 days
                    if 350 <= duration_days <= 370:
                        days_diff = abs((end_date.date() - doc_period_end.date()).days)
                        if days_diff <= 30:  # Allow up to 30 days difference for annual periods
                            annual_periods.append((period_str, start_date, end_date, days_diff, context_id))
                
                if annual_periods:
                    # Sort by how close the end date is to document period end
                    annual_periods.sort(key=lambda x: x[3])  # Sort by days_diff
                    
                    best_duration = annual_periods[0][0]  # period_str
                    best_context_id = annual_periods[0][4]
                    logger.debug(f"Primary duration period found (annual fallback): {best_duration} (context: {best_context_id})")
                    primary_period_info['duration_period'] = best_duration
                else:
                    # Final fallback: use the period closest to document period end
                    closest_periods = []
                    for period_str, start_date, end_date, duration_days, context_id in duration_contexts:
                        days_diff = abs((end_date.date() - doc_period_end.date()).days)
                        closest_periods.append((period_str, start_date, end_date, days_diff, context_id))
                    
                    if closest_periods:
                        closest_periods.sort(key=lambda x: x[3])  # Sort by days_diff
                        best_duration = closest_periods[0][0]
                        best_context_id = closest_periods[0][4]
                        logger.debug(f"Primary duration period (closest match): {best_duration} (context: {best_context_id})")
                        primary_period_info['duration_period'] = best_duration
        
        # PRIORITY 3: Calculate fiscal year using centralized extractor (DRY principle)
        if primary_period_info['fiscal_year'] is None:
            # Use centralized fiscal year extraction with fallback chain
            from utilities.helpers.fiscal_year_extractor import FiscalYearExtractor
            
            # Determine end date for calculation
            end_date_for_calc = None
            if primary_period_info['duration_period']:
                # Extract from duration period
                try:
                    parts = primary_period_info['duration_period'].split(" to ")
                    if len(parts) == 2:
                        end_date_str = parts[1].split(" ")[0]
                        end_date_for_calc = datetime.strptime(end_date_str, "%Y-%m-%d")
                except:
                    pass
            
            if not end_date_for_calc and doc_period_end:
                end_date_for_calc = doc_period_end
            
            # Extract fiscal year with metadata for tracking source
            # Safely extract document URI (modelDocument can be None in some cases)
            document_uri = None
            if hasattr(modelXbrl, 'modelDocument') and modelXbrl.modelDocument is not None:
                document_uri = modelXbrl.modelDocument.uri
                
            fiscal_year, source = FiscalYearExtractor.extract_fiscal_year_with_metadata(
                entity_info=entity_info,
                end_date=end_date_for_calc,
                document_uri=document_uri,
                company_info=self.company_info,
                fiscal_year_convention=(self.company_info or {}).get(
                    'fiscal_year_convention', 'end'
                ),
            )
            
            if fiscal_year:
                primary_period_info['fiscal_year'] = fiscal_year
                primary_period_info['fiscal_year_source'] = source
                logger.debug(f"✓ Extracted fiscal year {fiscal_year} (source: {source})")
                
                # Calculate quarter if we have the necessary data
                fiscal_year_end_code = entity_info.get('fiscal_year_end')
                if end_date_for_calc and fiscal_year_end_code:
                    from utilities.helpers.period_utils import FiscalYearCalculator
                    _, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                        end_date_for_calc, fiscal_year_end_code, None,
                        (self.company_info or {}).get('fiscal_year_convention', 'end'),
                    )
                    if quarter:
                        primary_period_info['quarter'] = quarter
            else:
                logger.warning("⚠ Could not extract fiscal year from any source")
        
        return primary_period_info
    
    def _identify_statement_roles_flexible(self, modelXbrl) -> Dict[str, Dict[str, str]]:
        """Flexible statement role identification supporting multiple taxonomies"""
        
        identified_roles = {}
        
        # Dynamic concept discovery based on available taxonomies
        key_concepts = self._build_flexible_concept_patterns(modelXbrl)
        
        # Get all presentation roles and analyze them
        for role_uri, role_type_list in modelXbrl.roleTypes.items():
            if not role_type_list:
                continue
                
            role_type = role_type_list[0]

            # Some companies (e.g. Boeing pre-2018) declare roleType elements in their XSD
            # without a <link:definition> child element.  In that case fall back to deriving a
            # human-readable description from the URI path segment using CamelCase splitting.
            if hasattr(role_type, 'definition') and role_type.definition:
                role_desc = role_type.definition.lower()
            else:
                # Extract last path segment: ".../role/CondensedConsolidatedStatementsOfOperations"
                # → "condensed consolidated statements of operations"
                uri_segment = role_uri.rstrip('/').rsplit('/', 1)[-1]
                # Split CamelCase: "CondensedConsolidatedStatementsOfOperations" → individual words
                import re as _re
                words = _re.sub(r'([A-Z])', r' \1', uri_segment).strip().lower()
                role_desc = words
                logger.debug(f"No definition for role {role_uri!r} — derived desc: {role_desc!r}")
            
            # More flexible filtering - don't be too restrictive
            exclude_patterns = [
                'detail', 'parenthetical', 'disclosure', 'policy', 'table',
                'schedule', 'note', 'footnote', 'supplemental', 'additional'
            ]
            
            if any(exclude in role_desc for exclude in exclude_patterns):
                continue
                
            # Get presentation relationship set for this role
            pres_rel_set = modelXbrl.relationshipSet(XbrlConst.parentChild, role_uri)
            if not pres_rel_set:
                continue
                
            # Collect all concepts in this presentation tree
            all_concepts_in_role = set()
            
            def collect_concepts(concept):
                if concept is not None and hasattr(concept, 'qname'):
                    all_concepts_in_role.add(str(concept.qname))
                    child_rels = pres_rel_set.fromModelObject(concept)
                    for child_rel in child_rels:
                        if hasattr(child_rel, 'toModelObject'):
                            collect_concepts(child_rel.toModelObject)
            
            # Start from root concepts
            root_concepts = pres_rel_set.rootConcepts
            for root_concept in root_concepts:
                collect_concepts(root_concept)
            
            # Determine statement type using flexible pattern matching
            statement_match = self._classify_statement_type(role_desc, all_concepts_in_role, key_concepts)
            
            if statement_match:
                statement_type = statement_match['type']
                
                # Special handling: If this is comprehensive_income, also create an income_statement
                # Many companies report a single "Statement of Comprehensive Income" that includes
                # both traditional income statement items AND other comprehensive income
                if statement_type == 'comprehensive_income':
                    # Check if this also has core income statement concepts.
                    # Exclude NetIncomeLoss/ProfitLoss because these appear in BOTH the
                    # separate "Statements of Comprehensive Income" AND the "Statements of
                    # Operations" — they are not exclusive to the income statement.
                    # Only Revenues, GrossProfit, OperatingIncomeLoss etc. are exclusive.
                    # Using NetIncomeLoss here causes companies like Boeing (which present a
                    # separate Operations statement + a separate Comprehensive Income statement)
                    # to have their Comprehensive Income role incorrectly treated as the
                    # income_statement, blocking the real Operations statement from being used.
                    income_concepts = key_concepts.get('income_statement', [])
                    _exclusive_income_concepts = [
                        p for p in income_concepts
                        if not any(shared in p['concept'] for shared in ('NetIncomeLoss', 'ProfitLoss'))
                    ]
                    has_income_concepts = any(
                        pattern['concept'] in all_concepts_in_role
                        for pattern in _exclusive_income_concepts
                    )
                    
                    logger.debug(f"Comprehensive income statement found. Has income concepts: {has_income_concepts}")
                    logger.debug(f"Checking {len(_exclusive_income_concepts)} exclusive income concept patterns against {len(all_concepts_in_role)} role concepts")
                    
                    if has_income_concepts:
                        # Create an income_statement entry if it doesn't exist
                        income_exists = any(role['statement_type'] == 'income_statement' for role in identified_roles.values())
                        logger.info(f"✅ Creating income_statement from comprehensive_income role. Income exists: {income_exists}")
                        if not income_exists:
                            # Add income_statement with same role but different type.
                            # Mark as synthesized so a real Operations/Earnings role can
                            # always override it if encountered later.
                            identified_roles[role_uri + '_income'] = {
                                'statement_type': 'income_statement',
                                'description': role_type.definition,
                                'role_uri': role_uri,  # Use same role URI
                                'priority': statement_match['priority'] - 5,  # Slightly higher priority
                                'taxonomy_info': statement_match['taxonomy_info'],
                                'synthesized': True  # Flag: derived from comprehensive_income role
                            }
                            logger.info(f"✅ Added income_statement role: {role_uri}_income")
                
                # Avoid duplicates by preferring primary statements
                if statement_type in [role['statement_type'] for role in identified_roles.values()]:
                    # Check if this one is more primary
                    existing_role = next(r for r in identified_roles.values() if r['statement_type'] == statement_type)
                    # A real (non-synthesized) role always beats a synthesized one,
                    # regardless of priority numbers
                    existing_is_synthesized = existing_role.get('synthesized', False)
                    if not existing_is_synthesized and statement_match['priority'] >= existing_role.get('priority', 100):
                        continue
                    # If existing is synthesized and new is real, fall through to replace it
                    if existing_is_synthesized and statement_match.get('synthesized', False):
                        # Both synthesized — keep higher priority (lower number)
                        if statement_match['priority'] >= existing_role.get('priority', 100):
                            continue
                    # Remove the old synthesized entry so the real one can be added below
                    if existing_is_synthesized:
                        old_key = next(k for k, v in identified_roles.items() if v['statement_type'] == statement_type)
                        del identified_roles[old_key]
                        logger.info(f"♻️  Replacing synthesized income_statement with real role: {role_uri[:80]}")
                
                identified_roles[role_uri] = {
                    'statement_type': statement_type,
                    'description': role_type.definition,
                    'role_uri': role_uri,
                    'priority': statement_match['priority'],
                    'taxonomy_info': statement_match['taxonomy_info']
                }
        
        return identified_roles
    
    def _build_flexible_concept_patterns(self, modelXbrl) -> Dict[str, List[Dict[str, Any]]]:
        """Build flexible concept patterns based on discovered taxonomies"""
        
        patterns = {
            'balance_sheet': [],
            'income_statement': [],
            'comprehensive_income': [],
            'cash_flow': [],
            'equity_changes': []
        }
        
        # Get all available concepts
        available_concepts = set()
        for concept in modelXbrl.qnameConcepts.values():
            if hasattr(concept, 'qname'):
                available_concepts.add(str(concept.qname))
        
        # Define patterns for different taxonomies
        taxonomy_patterns = {
            'us-gaap': {
                'balance_sheet': ['Assets', 'Liabilities', 'StockholdersEquity', 'AssetsCurrent'],
                # Include all revenue concept variants used across ASC 606 transition (2018) so
                # that a combined "Operations and Comprehensive Income" statement is correctly
                # detected as containing income concepts regardless of the era.
                # SalesRevenueNet was retired in 2018; RevenueFromContractWithCustomer replaced it.
                'income_statement': [
                    'Revenues',
                    'SalesRevenueNet',
                    'SalesRevenueGoodsNet',
                    'RevenueFromContractWithCustomerExcludingAssessedTax',
                    'RevenueFromContractWithCustomerIncludingAssessedTax',
                    'OperatingIncomeLoss',
                    'GrossProfit',
                    'CostOfGoodsAndServicesSold',
                    'CostOfRevenue',
                ],
                'comprehensive_income': [
                    'ComprehensiveIncomeNetOfTax', 
                    'OtherComprehensiveIncomeLossNetOfTax',
                    'OtherComprehensiveIncomeLoss',
                    'ComprehensiveIncome',
                    'AccumulatedOtherComprehensiveIncomeLoss'
                ],
                'cash_flow': ['CashAndCashEquivalentsAtCarryingValue', 'NetCashProvidedByUsedInOperatingActivities'],
                'equity_changes': [
                    'StockholdersEquity',
                    'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
                    'RetainedEarningsAccumulatedDeficit',
                ]
            },
            'ifrs-full': {
                'balance_sheet': ['Assets', 'Liabilities', 'Equity', 'CurrentAssets'],
                'income_statement': ['Revenue', 'ProfitLoss', 'OperatingExpense', 'GrossProfit'],
                'comprehensive_income': [
                    'ComprehensiveIncome', 
                    'OtherComprehensiveIncome',
                    'ComponentsOfOtherComprehensiveIncome'
                ],
                'cash_flow': ['CashAndCashEquivalents', 'CashFlowsFromUsedInOperatingActivities'],
                'equity_changes': ['Equity', 'RetainedEarnings']
            },
            'dei': {
                'balance_sheet': ['Assets', 'Liabilities'],
                'income_statement': ['Revenue'],
                'cash_flow': ['CashAndCashEquivalents'],
                'equity_changes': ['Equity']
            }
        }
        
        # Build patterns based on available taxonomies
        for taxonomy in self.discovered_taxonomies:
            if taxonomy in taxonomy_patterns:
                for statement_type, concepts in taxonomy_patterns[taxonomy].items():
                    for concept in concepts:
                        # Find matching concepts with different namespace prefixes
                        matching_concepts = [c for c in available_concepts if concept in c]
                        for match in matching_concepts:
                            patterns[statement_type].append({
                                'concept': match,
                                'taxonomy': taxonomy,
                                'weight': 1.0,
                                'pattern': concept
                            })
        
        return patterns
    
    def _classify_statement_type(self, role_desc: str, concepts_in_role: Set[str], 
                                key_concepts: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        """Classify statement type using flexible pattern matching with role description priority"""
        
        best_match = None
        best_score = 0
        
        # Exclusive income concepts: present in Operations but NOT in a pure CI statement.
        # We check these BEFORE the role description match so that a combined role description
        # like "Consolidated Statements of Operations and Other Comprehensive Income" (which
        # contains "comprehensive income") is correctly kept as income_statement when it
        # also contains revenue/gross-profit/operating-income concepts.
        _exclusive_income_patterns = [
            p for p in key_concepts.get('income_statement', [])
            if not any(shared in p['concept']
                       for shared in ('NetIncomeLoss', 'ProfitLoss', 'ComprehensiveIncome'))
        ]
        role_has_exclusive_income = any(
            p['concept'] in concepts_in_role for p in _exclusive_income_patterns
        )

        # First, check for strong role description matches - these should take priority
        # Order matters! Check more specific patterns first to avoid false matches.
        # EXCEPTION: if the role contains exclusive income concepts AND "comprehensive income"
        # is in the description, treat it as income_statement (combined presentation).
        desc_patterns = [
            ('comprehensive_income', ['comprehensive income']),
            ('balance_sheet', ['balance sheet', 'statement of financial position', 'position']),
            ('cash_flow', ['cash flow', 'cash flows']),
            ('equity_changes', ['equity', 'stockholders', 'shareholders', 'changes in equity']),
            ('income_statement', ['income', 'operations', 'earnings', 'profit', 'loss'])  # Keep income last due to broad match
        ]
        
        # Check for exact role description matches first (highest priority)
        for statement_type, patterns in desc_patterns:
            for pattern in patterns:
                if pattern in role_desc:
                    # If this would be classified as comprehensive_income but the role also
                    # has exclusive income concepts (Revenue, GrossProfit, OperatingIncomeLoss),
                    # it is a combined "Operations and Comprehensive Income" statement.
                    # Classify it as income_statement so that the revenue/profit data is not lost.
                    effective_type = statement_type
                    if statement_type == 'comprehensive_income' and role_has_exclusive_income:
                        effective_type = 'income_statement'
                        logger.debug(
                            f"Combined income+CI role detected ('{role_desc[:60]}'): "
                            f"classifying as income_statement instead of comprehensive_income"
                        )
                    
                    priority = 50  # Default priority
                    if 'consolidated' in role_desc:
                        priority -= 20  # Higher priority (lower number)
                    if 'combined' in role_desc:
                        priority -= 10
                    if 'parenthetical' in role_desc:
                        priority += 30  # Lower priority
                    
                    return {
                        'type': effective_type,
                        'score': 100.0,  # Very high score for role description match
                        'priority': priority,
                        'taxonomy_info': {
                            'primary_taxonomy': 'role-based',
                            'all_taxonomies': ['role-based']
                        }
                    }
        
        # If no strong role description match, fall back to concept-based scoring
        # Special handling: Check for comprehensive income concepts first as they should override income statement
        comprehensive_concepts = key_concepts.get('comprehensive_income', [])
        has_comprehensive_concepts = any(pattern['concept'] in concepts_in_role for pattern in comprehensive_concepts)
        
        if has_comprehensive_concepts:
            # If we have comprehensive income concepts, prioritize comprehensive_income classification
            score = 0
            taxonomy_matches = []
            for pattern in comprehensive_concepts:
                if pattern['concept'] in concepts_in_role:
                    score += pattern['weight'] * 2.0  # Give extra weight to comprehensive income concepts
                    taxonomy_matches.append(pattern['taxonomy'])
            
            if score > 0:
                priority = 50  # Default priority
                if 'consolidated' in role_desc:
                    priority -= 20  # Higher priority (lower number)
                if 'combined' in role_desc:
                    priority -= 10
                if 'parenthetical' in role_desc:
                    priority += 30  # Lower priority
                
                return {
                    'type': 'comprehensive_income',
                    'score': score,
                    'priority': priority,
                    'taxonomy_info': {
                        'primary_taxonomy': taxonomy_matches[0] if taxonomy_matches else 'unknown',
                        'all_taxonomies': list(set(taxonomy_matches))
                    }
                }
        
        # Regular concept-based scoring for other statement types
        for statement_type, concept_patterns in key_concepts.items():
            score = 0
            taxonomy_matches = []
            
            # Score based on concept presence (but with lower weights)
            for pattern in concept_patterns:
                if pattern['concept'] in concepts_in_role:
                    score += pattern['weight'] * 0.5  # Reduce concept weight
                    taxonomy_matches.append(pattern['taxonomy'])
            
            # Priority scoring (prefer primary statements)
            priority = 50  # Default priority
            if 'consolidated' in role_desc:
                priority -= 20  # Higher priority (lower number)
            if 'combined' in role_desc:
                priority -= 10
            if 'parenthetical' in role_desc:
                priority += 30  # Lower priority
            
            if score > best_score and score > 0:
                best_score = score
                best_match = {
                    'type': statement_type,
                    'score': score,
                    'priority': priority,
                    'taxonomy_info': {
                        'primary_taxonomy': taxonomy_matches[0] if taxonomy_matches else 'unknown',
                        'all_taxonomies': list(set(taxonomy_matches))
                    }
                }
        
        return best_match
    
    def _extract_statement_hierarchy(self, modelXbrl, pres_rel_set, role_info) -> List[FinancialLineItem]:
        """Extract hierarchical structure for a financial statement."""
        
        # Store statement type for use in fact selection
        self.current_statement_type = role_info.get('statement_type')
        
        # Note: If this is an income_statement derived from a comprehensive_income statement,
        # we extract the full hierarchy. The income_statement will contain ALL line items including
        # revenue, expenses, net income, and any OCI items. This allows the consumer to decide
        # which items to use. The comprehensive_income statement will have the exact same data.
        
        # Find root concepts for this role
        root_concepts = pres_rel_set.rootConcepts
        
        hierarchy = []
        
        for root_concept in sorted(root_concepts, key=lambda c: str(c.qname) if hasattr(c, 'qname') else ''):
            root_item = self._build_hierarchy_tree(
                modelXbrl, root_concept, pres_rel_set, level=0
            )
            if root_item:
                hierarchy.append(root_item)
        
        return hierarchy
    
    def _build_hierarchy_tree(self, modelXbrl, concept, pres_rel_set, level=0, visited=None):
        """Recursively build hierarchy tree for a concept with enhanced dimensional support."""
        
        if visited is None:
            visited = set()
        
        if concept in visited:
            return None  # Avoid circular references
        
        visited.add(concept)
        
        # Get concept information with proper null checking
        concept_name = str(concept.qname) if hasattr(concept, 'qname') else str(concept)
        
        label = concept.label() if hasattr(concept, 'label') and concept.label() else (
            concept.name if hasattr(concept, 'name') else concept_name.split(':')[-1]
        )
        
        # Find facts for this concept using exact qname matching
        concept_facts = []
        if hasattr(concept, 'qname'):
            concept_facts = [f for f in modelXbrl.facts if f.concept.qname == concept.qname]
        
        # Enhanced fact processing for dimensional data with comprehensive extraction
        all_dimensional_facts = self._extract_dimensional_facts_comprehensive(modelXbrl, concept, concept_facts)
        
        # Select best primary fact
        best_fact = self._select_best_primary_fact(concept_facts)
        
        # Extract fact values with proper null checking
        value = None
        context_id = None
        unit_id = None
        period = None
        dimensions = []
        
        if best_fact is not None:
            try:
                # Handle comma-separated numbers from XBRL
                raw_value = best_fact.effectiveValue
                if raw_value:
                    clean_value = str(raw_value).replace(',', '')
                    value = float(clean_value)
                else:
                    value = None
            except (ValueError, TypeError):
                value = None
            
            context_id = best_fact.contextID
            # Fix FutureWarning: proper element testing
            unit_id = best_fact.unitID if (best_fact.unit is not None) else None
            
            # Extract period with proper context checking
            if best_fact.context is not None and hasattr(best_fact.context, 'period'):
                if hasattr(best_fact.context, 'isInstantPeriod') and best_fact.context.isInstantPeriod:
                    period = str(best_fact.context.instantDatetime)
                elif hasattr(best_fact.context, 'isStartEndPeriod') and best_fact.context.isStartEndPeriod:
                    period = f"{best_fact.context.startDatetime} to {best_fact.context.endDatetime}"
        
        # Extract dimensional context with proper null checking
        if (best_fact is not None and 
            best_fact.context is not None and 
            hasattr(best_fact.context, 'qnameDims') and
            best_fact.context.qnameDims is not None and
            len(best_fact.context.qnameDims) > 0):
            try:
                for dim_qname, dim_value in best_fact.context.qnameDims.items():
                    axis_name = dim_qname.localName if hasattr(dim_qname, 'localName') else str(dim_qname).split(':')[-1]
                    
                    if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit and hasattr(dim_value, 'memberQname'):
                        member_qname = dim_value.memberQname
                        member_name = member_qname.localName if hasattr(member_qname, 'localName') else str(member_qname).split(':')[-1]
                        
                        dimensions.append(DimensionalContext(
                            axis=str(dim_qname),
                            member=str(member_qname),
                            label=f"{axis_name}: {member_name}"
                        ))
                    elif hasattr(dim_value, 'isTyped') and dim_value.isTyped and hasattr(dim_value, 'typedMember'):
                        typed_value = getattr(dim_value.typedMember, 'textValue', str(dim_value.typedMember))
                        dimensions.append(DimensionalContext(
                            axis=str(dim_qname),
                            member=typed_value,
                            label=f"{axis_name}: {typed_value}"
                        ))
            except Exception:
                # Skip dimensional context if there's an access issue
                pass
        
        # Get presentation order
        order = 0.0
        parent_rels = pres_rel_set.toModelObject(concept)
        if parent_rels:
            order = min(rel.order for rel in parent_rels)
        
        # Determine if concept should be treated as concrete
        is_effectively_concrete = False
        if concept_facts and any(f.xValue is not None for f in concept_facts):
            is_effectively_concrete = True
        elif all_dimensional_facts and any(f.get('value') is not None for f in all_dimensional_facts):
            is_effectively_concrete = True
        
        # Extract calculation relationships
        calculations = self._extract_calculations(modelXbrl, concept)
        
        # Create line item with proper abstract checking
        abstract_value = getattr(concept, 'abstract', False) and not is_effectively_concrete
        
        line_item = FinancialLineItem(
            concept_name=concept_name,
            label=label,
            value=value,
            context_id=context_id or "",
            unit_id=unit_id,
            period=period or "",
            level=level,
            order=order,
            abstract=abstract_value,
            dimensions=dimensions,
            all_dimensional_facts=all_dimensional_facts,
            calculations=calculations
        )
        
        # Process children
        child_rels = pres_rel_set.fromModelObject(concept)
        child_rels = sorted(child_rels, key=lambda r: getattr(r, 'order', 0))
        
        for child_rel in child_rels:
            if hasattr(child_rel, 'toModelObject'):
                child_concept = child_rel.toModelObject
                child_item = self._build_hierarchy_tree(
                    modelXbrl, child_concept, pres_rel_set, level + 1, visited.copy()
                )
                if child_item:
                    child_item.parent_concept = concept_name
                    line_item.children.append(child_item)
        
        return line_item
    
    def _select_best_primary_fact(self, facts):
        """Select the best primary fact with improved scoring and period duration filtering"""
        
        if not facts:
            return None
        
        if len(facts) == 1:
            return facts[0]
        
        # Import period utilities for strict filtering
        from utilities.helpers.period_utils import PeriodParser, PeriodMatcher, PeriodClassifier
        
        scored_facts = []
        target_form_type = getattr(self, 'target_form_type', None)  # Get from filing context if available
        
        for fact in facts:
            score = 0
            
            # Context validation with proper null checking
            if fact.context is None or not hasattr(fact.context, 'period'):
                continue
            
            # Period type and duration filtering
            period_end = None
            period_start = None
            duration_months = None
            
            if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                score += 1000
                period_end = fact.context.instantDatetime
                # Instant periods are always fine
            elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                period_end = fact.context.endDatetime
                period_start = fact.context.startDatetime
                
                # Calculate duration for filtering
                if period_start and period_end:
                    duration_months = PeriodParser.calculate_duration_months(period_start, period_end)
                    
                    # CRITICAL: FISCAL YEAR VALIDATION
                    # Extract fiscal year and fiscal year end from company_info if available
                    fiscal_year = None
                    fiscal_year_end_code = None
                    if self.company_info:
                        fiscal_year = self.company_info.get('fiscal_year')
                        fiscal_year_end_code = self.company_info.get('fiscal_year_end_code')
                        logger.debug(f"Fact selection fiscal info: FY={fiscal_year}, FYE={fiscal_year_end_code}")
                    
                    # If we have fiscal information, validate the period belongs to the correct fiscal year
                    # This prevents selecting comparative/prior year periods (e.g., 2012 Q2 when we want 2013 Q2)
                    if fiscal_year and fiscal_year_end_code and target_form_type == '10-Q':
                        from utilities.helpers.period_utils import FiscalYearCalculator
                        period_fiscal_year = FiscalYearCalculator.determine_fiscal_year_from_date(
                            period_end, fiscal_year_end_code
                        )
                        
                        logger.debug(f"Fiscal year validation for {fact.qname}: period_end={period_end}, period_fiscal_year={period_fiscal_year}, target_fiscal_year={fiscal_year}")
                        
                        if period_fiscal_year != fiscal_year:
                            # Wrong fiscal year - skip this fact completely
                            # This is comparative/prior year data, not current period data
                            logger.debug(f"SKIPPING fact {fact.qname} - wrong fiscal year: {period_fiscal_year} != {fiscal_year}")
                            continue
                    
                    # STRICT PERIOD FILTERING with statement-type awareness
                    # Cash flow statements use YTD (cumulative) periods even in quarterly filings
                    statement_type = getattr(self, 'current_statement_type', None)
                    is_cash_flow = statement_type in ('cash_flow_statement', 'cash_flow', 'cash_flows')
                    
                    logger.debug(f"Period filtering for {fact.qname}: statement_type={statement_type}, is_cash_flow={is_cash_flow}, duration={duration_months:.2f} months")
                    
                    if target_form_type == '10-Q':
                        # For quarterly filings, handle based on statement type
                        if is_cash_flow:
                            # Cash flow: Accept YTD cumulative periods
                            # For Q2: 6 months (Jan-Jun), Q3: 9 months (Jan-Sep), Q4: 12 months (Jan-Dec)
                            # These are all valid for cash flow in quarterly filings
                            logger.debug(f"Cash flow fact: duration={duration_months:.2f}, threshold=2.9")
                            if duration_months >= 2.9:  # Any period >= ~3 months is acceptable
                                score += 1200  # YTD period for cash flow
                                logger.debug(f"✅ Accepted YTD cash flow period: {duration_months:.2f} months")
                            else:
                                # Reject monthly or other very short periods
                                logger.debug(f"❌ Rejected too-short cash flow period: {duration_months:.2f} months")
                                continue
                        else:
                            # Income/balance sheet: Strictly quarterly only
                            if PeriodMatcher.reject_non_quarterly_period(duration_months, target_form_type):
                                # Skip this fact - it's a cumulative period (6, 9, 12 months)
                                logger.debug(f"❌ Rejected non-quarterly period for income/balance sheet: {duration_months:.2f} months")
                                continue
                            
                            # Bonus for being exactly quarterly
                            if PeriodMatcher.is_strictly_quarterly_period(duration_months):
                                score += 1200  # Higher than instant periods
                            else:
                                score += 800
                    elif target_form_type == '10-K':
                        # For annual filings, prefer annual periods
                        if abs(duration_months - 12.0) <= 1.0:
                            score += 1200  # Annual period
                        else:
                            score += 400  # Non-annual period (lower priority)
                    else:
                        # Unknown filing type - be conservative
                        score += 800
                else:
                    continue
            else:
                continue
            
            # Recency bonus
            if period_end and hasattr(period_end, 'toordinal'):
                score += period_end.toordinal()
            
            # Dimensional context scoring with unwanted context filtering
            context = fact.context
            if (hasattr(context, 'qnameDims') and 
                context.qnameDims is not None and 
                len(context.qnameDims) > 0):
                
                # Check if this fact has unwanted dimensional contexts (forecast, scenario, estimates)
                has_unwanted_context = False
                for dim_qname, dim_value in context.qnameDims.items():
                    dim_axis = str(dim_qname)
                    
                    # Check for excluded axes
                    excluded_axes = {
                        'srt:StatementScenarioAxis',
                        'us-gaap:ChangeInAccountingEstimateByTypeAxis',
                        'us-gaap:ChangeInAccountingPrincipleAxis',
                        'us-gaap:ErrorCorrectionAxis',
                        'us-gaap:RestatementAxis'
                    }
                    if dim_axis in excluded_axes:
                        has_unwanted_context = True
                        break
                    
                    # Check for excluded members
                    if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                        member_qname = str(dim_value.memberQname)
                        excluded_members = {
                            'srt:ScenarioForecastMember',
                            'us-gaap:ScenarioForecastMember',
                            'srt:ScenarioUnspecifiedMember',
                            'us-gaap:ScenarioUnspecifiedMember'
                        }
                        if member_qname in excluded_members:
                            has_unwanted_context = True
                            break
                
                if has_unwanted_context:
                    # Heavily penalize facts with forecast/scenario/estimate contexts
                    score -= 10000  # This should exclude them from selection
                else:
                    score += 200  # Has dimensions but they're acceptable
            else:
                score += 500  # No dimensions - consolidated total
            
            # Value presence
            if fact.xValue is not None:
                score += 100
            
            scored_facts.append((score, fact, duration_months))
        
        if not scored_facts:
            logger.debug(f"No facts passed validation for {facts[0].qname if facts else 'unknown'} - falling back to best undimensioned/latest fact")
            if facts and "DepreciationDepletionAndAmortization" in str(facts[0].qname):
                logger.warning(f"FALLBACK for Depreciation: using fallback fact with context {facts[0].contextID}")
            # Never fall back to an arbitrary fact: prefer the consolidated
            # (undimensioned) fact, then the most recent period.  Picking
            # ``facts[0]`` previously returned a dimensional member value
            # (e.g. "Products" revenue) as the line item's total.
            def _fallback_rank(f):
                ctx = f.context
                dims = getattr(ctx, "qnameDims", None)
                has_dims = 1 if (dims is not None and len(dims) > 0) else 0
                end = getattr(ctx, "endDatetime", None) or getattr(ctx, "instantDatetime", None)
                try:
                    ordinal = end.toordinal()
                except Exception:
                    ordinal = 0
                # Prefer undimensioned (consolidated) facts, then the latest period.
                return (0 if has_dims else 1, ordinal, str(getattr(f, "contextID", "")))

            return max(facts, key=_fallback_rank) if facts else None
        
        # Log period filtering results for debugging
        best_fact_info = max(scored_facts, key=lambda x: x[0])
        best_fact, best_score, best_duration = best_fact_info[1], best_fact_info[0], best_fact_info[2]
        
        if best_duration and target_form_type:
            logger.debug(f"Selected primary fact with {best_duration:.1f} months duration for {target_form_type} filing")
        
        # Log the selected fact details for depreciation
        if "DepreciationDepletionAndAmortization" in str(best_fact.qname):
            logger.debug(f"SELECTED BEST FACT for {best_fact.qname}:")
            logger.debug(f"  Context: {best_fact.contextID}")
            logger.debug(f"  Value: {best_fact.effectiveValue}")
            if hasattr(best_fact.context, 'isStartEndPeriod') and best_fact.context.isStartEndPeriod:
                logger.debug(f"  Period: {best_fact.context.startDatetime} to {best_fact.context.endDatetime}")
        
        return best_fact
    
    def _extract_typed_dimensions_enhanced(self, fact):
        """Enhanced typed dimension extraction using multiple methods"""
        typed_dimensions = {}
        typed_details = {}
        
        if (fact.context is not None and 
            hasattr(fact.context, 'qnameDims') and 
            fact.context.qnameDims is not None):
            
            for dim_qname, dim_value in fact.context.qnameDims.items():
                if hasattr(dim_value, 'isTyped') and dim_value.isTyped:
                    axis_local_name = dim_qname.localName if hasattr(dim_qname, 'localName') else str(dim_qname).split(':')[-1]
                    
                    typed_value = None
                    if hasattr(dim_value, 'typedMember') and dim_value.typedMember is not None:
                        typed_member = dim_value.typedMember
                        
                        # Try multiple extraction methods (more than current single method)
                        for attr_name in ['textValue', 'stringValue', 'xValue', 'text', 'value']:
                            try:
                                attr_value = getattr(typed_member, attr_name, None)
                                if attr_value is not None:
                                    typed_value = str(attr_value)
                                    break
                            except:
                                continue
                        
                        # Fallback to string conversion
                        if not typed_value:
                            typed_value = str(typed_member)
                    
                    if typed_value and typed_value not in ['None', '']:
                        typed_dimensions[axis_local_name] = typed_value
                        typed_details[axis_local_name] = {
                            'type': 'typed_enhanced',
                            'axis_qname': str(dim_qname),
                            'typed_value': typed_value,
                            'axis_local_name': axis_local_name,
                            'axis_namespace': dim_qname.namespaceURI if hasattr(dim_qname, 'namespaceURI') else None,
                            'source': 'enhanced_extraction'
                        }
        
        return typed_dimensions, typed_details
    
    def _extract_context_dimensional_elements(self, fact):
        """Extract dimensional info from segment and scenario elements"""
        context_dimensions = {}
        context_details = {}
        
        if fact.context is None:
            return context_dimensions, context_details
        
        # Extract from segment
        if hasattr(fact.context, 'segment') and fact.context.segment is not None:
            for element in fact.context.segment:
                if hasattr(element, 'tag') and hasattr(element, 'text'):
                    tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag
                    element_value = element.text if element.text else None
                    
                    if (element_value and 
                        any(keyword in tag_name.lower() for keyword in 
                            ['axis', 'member', 'dimension', 'segment'])):
                        
                        context_dimensions[tag_name] = element_value
                        context_details[tag_name] = {
                            'type': 'segment_element',
                            'axis_qname': element.tag,
                            'member': element_value,
                            'axis_local_name': tag_name,
                            'source': 'context_segment'
                        }
        
        # Extract from scenario
        if hasattr(fact.context, 'scenario') and fact.context.scenario is not None:
            for element in fact.context.scenario:
                if hasattr(element, 'tag') and hasattr(element, 'text'):
                    tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag
                    element_value = element.text if element.text else None
                    
                    if (element_value and 
                        any(keyword in tag_name.lower() for keyword in 
                            ['axis', 'member', 'dimension', 'scenario'])):
                        
                        if tag_name not in context_dimensions:  # Don't override segment
                            context_dimensions[tag_name] = element_value
                            context_details[tag_name] = {
                                'type': 'scenario_element',
                                'axis_qname': element.tag,
                                'member': element_value,
                                'axis_local_name': tag_name,
                                'source': 'context_scenario'
                            }
        
        return context_dimensions, context_details

    def _extract_dimensional_facts(self, facts):
        """Extract all dimensional fact information with enhanced Arelle API utilization and improved error handling"""
        
        dimensional_facts = []
        
        for fact in facts:
            # Process all facts, including those with and without dimensions
            # This ensures we capture the complete picture of all available data
            
            try:
                # Extract fact value with enhanced value parsing
                value = None
                raw_value = None
                
                # Try multiple value extraction methods
                if hasattr(fact, 'xValue') and fact.xValue is not None:
                    raw_value = fact.xValue
                elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                    raw_value = fact.effectiveValue
                elif hasattr(fact, 'value') and fact.value is not None:
                    raw_value = fact.value
                
                # Parse the raw value
                if raw_value is not None:
                    if isinstance(raw_value, (int, float, Decimal)):
                        value = float(raw_value)
                    else:
                        try:
                            # Handle comma-separated numbers and various formats
                            clean_value = str(raw_value).replace(',', '').replace('$', '').strip()
                            if clean_value and clean_value not in ['', '-', 'N/A', 'n/a']:
                                value = float(clean_value)
                        except (ValueError, TypeError):
                            value = None
                
                # Enhanced dimensional context extraction
                dimensions = {}
                dimension_details = {}
                has_dimensions = False
                
                # Check for dimensional context
                if (fact.context is not None and 
                    hasattr(fact.context, 'qnameDims') and 
                    fact.context.qnameDims is not None and 
                    len(fact.context.qnameDims) > 0):
                    
                    has_dimensions = True
                    
                    for dim_qname, dim_value in fact.context.qnameDims.items():
                        axis_name = str(dim_qname)
                        axis_local_name = dim_qname.localName if hasattr(dim_qname, 'localName') else str(dim_qname).split(':')[-1]
                        
                        # Extract explicit dimension members
                        if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit:
                            if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                                member_qname = dim_value.memberQname
                                member_name = member_qname.localName if hasattr(member_qname, 'localName') else str(member_qname).split(':')[-1]
                                
                                # Store in simplified format for easy querying
                                dimensions[axis_local_name] = member_name
                                
                                # Store detailed information for advanced analysis
                                dimension_details[axis_local_name] = {
                                    'type': 'explicit',
                                    'axis_qname': str(dim_qname),
                                    'member_qname': str(member_qname),
                                    'member_local_name': member_name,
                                    'axis_local_name': axis_local_name,
                                    'member_label': self._get_concept_label(dim_value.memberQname) if hasattr(dim_value, 'memberQname') else member_name
                                }
                        
                        # Extract typed dimension values with enhanced extraction if enabled
                        elif hasattr(dim_value, 'isTyped') and dim_value.isTyped:
                            if hasattr(dim_value, 'typedMember') and dim_value.typedMember is not None:
                                typed_value = None
                                
                                if self.enable_enhanced_dimensions:
                                    # Enhanced typed dimension extraction
                                    for attr_name in ['textValue', 'stringValue', 'xValue', 'text', 'value']:
                                        try:
                                            attr_value = getattr(dim_value.typedMember, attr_name, None)
                                            if attr_value is not None:
                                                typed_value = str(attr_value)
                                                break
                                        except:
                                            continue
                                    
                                    # Fallback to string conversion
                                    if not typed_value:
                                        typed_value = str(dim_value.typedMember)
                                else:
                                    # Original extraction method
                                    typed_value = getattr(dim_value.typedMember, 'textValue', str(dim_value.typedMember))
                                
                                if typed_value and typed_value not in ['None', '']:
                                    dimensions[axis_local_name] = typed_value
                                    
                                    dimension_details[axis_local_name] = {
                                        'type': 'typed_enhanced' if self.enable_enhanced_dimensions else 'typed',
                                        'axis_qname': str(dim_qname),
                                        'typed_value': typed_value,
                                        'axis_local_name': axis_local_name,
                                        'axis_namespace': dim_qname.namespaceURI if hasattr(dim_qname, 'namespaceURI') else None,
                                        'source': 'enhanced_extraction' if self.enable_enhanced_dimensions else 'standard_extraction'
                                    }
                
                
                # Enhanced context dimensional extraction if enabled
                if self.enable_enhanced_dimensions:
                    context_dimensions, context_details = self._extract_context_dimensional_elements(fact)
                    # Merge context dimensions with existing dimensions (don't override)
                    for axis_name, member_value in context_dimensions.items():
                        if axis_name not in dimensions:
                            dimensions[axis_name] = member_value
                            dimension_details[axis_name] = context_details[axis_name]
                            has_dimensions = True
                
                # Enhanced period information extraction
                period_info = ""
                period_type = None
                if fact.context is not None and hasattr(fact.context, 'period'):
                    if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                        period_type = "instant"
                        period_info = str(fact.context.instantDatetime) if fact.context.instantDatetime else ""
                    elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                        period_type = "duration"
                        start_date = str(fact.context.startDatetime) if fact.context.startDatetime else ""
                        end_date = str(fact.context.endDatetime) if fact.context.endDatetime else ""
                        period_info = f"{start_date} to {end_date}"
                    elif hasattr(fact.context, 'isForeverPeriod') and fact.context.isForeverPeriod:
                        period_type = "forever"
                        period_info = "forever"
                
                # Extract entity information with scheme URL like online API
                entity_info = {}
                if fact.context is not None and hasattr(fact.context, 'entityIdentifier'):
                    entity_info = {
                        'scheme': fact.context.entityIdentifier[0] if len(fact.context.entityIdentifier) > 0 else 'http://www.sec.gov/CIK',
                        'identifier': fact.context.entityIdentifier[1] if len(fact.context.entityIdentifier) > 1 else None
                    }
                
                # Enhanced unit information to match online API format
                unit_info = {}
                if fact.unit is not None:
                    unit_info = {
                        'id': fact.unitID,
                        'measures': []
                    }
                    
                    # Extract measure information using Arelle's unit model
                    if hasattr(fact.unit, 'measures'):
                        multiply_measures, divide_measures = fact.unit.measures
                        
                        # Process multiply measures
                        if multiply_measures:
                            for measure in multiply_measures:
                                unit_info['measures'].append(str(measure))
                        
                        # Process divide measures (if any) - remove "/" prefix to match online format
                        if divide_measures:
                            for measure in divide_measures:
                                unit_info['measures'].append(str(measure))
                    
                    # If no measures found, try alternative approach
                    if not unit_info['measures'] and hasattr(fact.unit, 'value'):
                        unit_info['measures'].append(str(fact.unit.value))
                
                # Only include facts that have dimensions OR a non-None value
                # This ensures we capture both dimensional breakdowns and total values
                if has_dimensions or value is not None:
                    # Extract concept and label information for identification
                    concept_name = str(fact.concept.qname) if (fact.concept is not None) else None
                    fact_label = self._get_concept_label(fact.concept.qname) if (fact.concept is not None) else None
                    
                    fact_data = {
                        'value': value,
                        'dimensions': dimensions,
                        'dimension_details': dimension_details,
                        'context_id': fact.contextID,
                        'unit_id': fact.unitID if (fact.unit is not None) else None,
                        'unit_info': unit_info,
                        'period': period_info,
                        'period_type': period_type,
                        'entity_info': entity_info,
                        'concept_name': concept_name,
                        'fact_label': fact_label,
                        'has_dimensions': has_dimensions,
                        'dimension_count': len(dimensions),
                        'fact_id': getattr(fact, 'id', None),
                        'decimals': getattr(fact, 'decimals', None),
                        'precision': getattr(fact, 'precision', None)
                    }
                    
                    # Remove null precision/decimals if desired (optional optimization)
                    if fact_data['precision'] is None:
                        del fact_data['precision']
                    if fact_data['decimals'] is None:
                        del fact_data['decimals']
                    
                    dimensional_facts.append(fact_data)
                
            except Exception as e:
                # Log the error but continue processing other facts
                logger.debug(f"Error processing fact {getattr(fact, 'contextID', 'unknown')}: {e}")
                continue
        
        # Sort dimensional facts by period and dimension count for consistent ordering
        dimensional_facts.sort(key=lambda x: (x.get('period', ''), x.get('dimension_count', 0)))
        
        return dimensional_facts
    
    def _get_concept_label(self, concept_qname):
        """Get human-readable label for a concept using Arelle's label functionality"""
        try:
            if hasattr(concept_qname, 'localName'):
                # Try to find the concept in the model
                for concept in self.model_manager.modelXbrl.qnameConcepts.values() if self.model_manager.modelXbrl else []:
                    if hasattr(concept, 'qname') and concept.qname == concept_qname:
                        if hasattr(concept, 'label') and concept.label():
                            return concept.label()
                        else:
                            return concept_qname.localName
                return concept_qname.localName
            else:
                return str(concept_qname).split(':')[-1]
        except Exception:
            return str(concept_qname).split(':')[-1] if concept_qname else "Unknown"
    
    def _filter_facts_to_primary_period(self, facts, modelXbrl):
        """
        Filter facts to only include those from the primary/current period
        This prevents inclusion of multiple historical periods in dimensional facts
        """
        if not facts:
            return facts
        
        from datetime import datetime
        
        # Get the target form type if available
        target_form_type = getattr(self, 'filing_form_type', None)
        
        # Check if this is an older filing (pre-2015) for relaxed filtering
        is_older_filing = getattr(self, 'is_older_filing', False)
        
        # Reset debug counters
        self._filtered_duration_count = 0
        
        primary_period_facts = []
        
        # Group facts by period to find the most current periods
        period_groups = {}
        
        for fact in facts:
            if fact.context is None or not hasattr(fact.context, 'period'):
                continue
                
            # Get period information
            period_key = None
            period_end = None
            
            if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                period_end = fact.context.instantDatetime
                period_key = f"instant_{period_end}"
            elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                period_end = fact.context.endDatetime
                period_start = fact.context.startDatetime
                if period_start and period_end:
                    # Calculate duration in months
                    from utilities.helpers.period_utils import PeriodParser
                    duration_months = PeriodParser.calculate_duration_months(period_start, period_end)
                    
                    # Apply period filtering with relaxed rules for older filings (pre-2015)
                    should_include = True
                    if target_form_type == '10-Q':
                        # Relaxed quarterly range for older filings
                        min_months = 2.3 if is_older_filing else 2.5
                        max_months = 4.2 if is_older_filing else 4.0
                        if not (min_months <= duration_months <= max_months):
                            should_include = False
                    elif target_form_type == '10-K':
                        # Relaxed annual range for older filings
                        min_months = 10.5 if is_older_filing else 11.0
                        max_months = 13.5 if is_older_filing else 13.0
                        if not (min_months <= duration_months <= max_months):
                            should_include = False
                    else:
                        # Default: prefer annual periods (11-13 months) or quarterly (2.5-4 months)
                        # More lenient for older filings
                        if is_older_filing:
                            if not ((2.3 <= duration_months <= 4.2) or (10.5 <= duration_months <= 13.5)):
                                should_include = False
                        else:
                            if not ((2.5 <= duration_months <= 4.0) or (11.0 <= duration_months <= 13.0)):
                                should_include = False
                    
                    if not should_include:
                        # Debug: track filtered facts
                        if not hasattr(self, '_filtered_duration_count'):
                            self._filtered_duration_count = 0
                        self._filtered_duration_count += 1
                        continue
                    
                    period_key = f"duration_{period_end}_{duration_months:.1f}m"
            
            if period_key and period_end:
                if period_key not in period_groups:
                    period_groups[period_key] = []
                period_groups[period_key].append((fact, period_end))
        
        # If we have period groups, select the most recent periods
        if period_groups:
            # Debug: show filtering results
            if hasattr(self, '_filtered_duration_count') and self._filtered_duration_count > 0:
                logger.debug(f"Duration filtering: excluded {self._filtered_duration_count} facts with inappropriate durations for {target_form_type or 'unknown'} filing")
            
            # Sort periods by end date (most recent first)
            all_periods = []
            for period_key, facts_list in period_groups.items():
                # Get the most recent fact from this period group
                most_recent_fact = max(facts_list, key=lambda x: x[1] if x[1] else datetime.min)
                all_periods.append((period_key, most_recent_fact[1], facts_list))
            
            # Sort by period end date descending (most recent first)
            all_periods.sort(key=lambda x: x[1] if x[1] else datetime.min, reverse=True)
            
            # Take facts from the most recent period(s)
            # For financial statements, we typically want the current period only
            if all_periods:
                most_recent_period = all_periods[0]
                primary_period_facts = [fact for fact, _ in most_recent_period[2]]
                
                logger.debug(f"Filtered to primary period: {most_recent_period[0]} with {len(primary_period_facts)} facts")
        else:
            # Debug: show why no period groups were found
            if hasattr(self, '_filtered_duration_count') and self._filtered_duration_count > 0:
                logger.debug(f"All {self._filtered_duration_count} duration facts were filtered out for {target_form_type or 'unknown'} filing")
            
            # Fallback: if no clear period structure, try a more lenient filter
            logger.debug("No clear period structure found, applying lenient period filtering")
            
            # Group all facts by period without strict duration filtering
            lenient_period_groups = {}
            
            for fact in facts:
                if fact.context is None or not hasattr(fact.context, 'period'):
                    continue
                    
                period_key = None
                period_end = None
                
                if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                    period_end = fact.context.instantDatetime
                    period_key = f"instant_{period_end}"
                elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                    period_end = fact.context.endDatetime
                    period_key = f"duration_{period_end}"
                
                if period_key and period_end:
                    if period_key not in lenient_period_groups:
                        lenient_period_groups[period_key] = []
                    lenient_period_groups[period_key].append((fact, period_end))
            
            # Select the most recent period from lenient groups
            if lenient_period_groups:
                all_lenient_periods = []
                for period_key, facts_list in lenient_period_groups.items():
                    most_recent_fact = max(facts_list, key=lambda x: x[1] if x[1] else datetime.min)
                    all_lenient_periods.append((period_key, most_recent_fact[1], facts_list))
                
                all_lenient_periods.sort(key=lambda x: x[1] if x[1] else datetime.min, reverse=True)
                
                if all_lenient_periods:
                    most_recent_period = all_lenient_periods[0]
                    primary_period_facts = [fact for fact, _ in most_recent_period[2]]
                    logger.debug(f"Lenient filter applied: {most_recent_period[0]} with {len(primary_period_facts)} facts")
                else:
                    logger.debug("Still no period structure, using all facts")
                    primary_period_facts = facts
            else:
                logger.debug("No period information available, using all facts")
                primary_period_facts = facts
        
        return primary_period_facts
    
    def _is_primary_period(self, period_str):
        """
        Check if a period string represents a primary/current period
        Used for filtering dimensional facts to avoid historical periods
        """
        if not period_str:
            return False
            
        try:
            from datetime import datetime, timedelta
            
            # Get the target form type if available
            target_form_type = getattr(self, 'filing_form_type', None)
            
            # Extract dates from period string
            if ' to ' in period_str:
                # Duration period: "2024-07-01 00:00:00 to 2025-07-01 00:00:00"
                parts = period_str.split(' to ')
                if len(parts) == 2:
                    try:
                        start_str = parts[0].strip()
                        end_str = parts[1].strip()
                        
                        # Parse dates (handle both with and without time)
                        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                            try:
                                start_date = datetime.strptime(start_str, fmt)
                                end_date = datetime.strptime(end_str, fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            return False  # Could not parse dates
                        
                        # Calculate duration in months
                        duration_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
                        
                        # Apply form-type specific duration filters
                        if target_form_type == '10-Q':
                            # Quarterly: 2.5-4 months
                            return 2.5 <= duration_months <= 4.0
                        elif target_form_type == '10-K':
                            # Annual: 11-13 months
                            return 11.0 <= duration_months <= 13.0
                        else:
                            # Default: accept common periods (quarterly or annual)
                            return ((2.5 <= duration_months <= 4.0) or (11.0 <= duration_months <= 13.0))
                    except Exception:
                        return False
            else:
                # Instant period: assume it's current if it looks like a recent date
                try:
                    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                        try:
                            period_date = datetime.strptime(period_str.strip(), fmt)
                            # Accept instant periods from the last 3 years (should cover current reporting)
                            cutoff_date = datetime.now() - timedelta(days=3*365)
                            return period_date >= cutoff_date
                        except ValueError:
                            continue
                    return False
                except Exception:
                    return False
                    
        except Exception:
            # If anything fails, default to including the fact
            return True
    
    def _extract_dimensional_facts_comprehensive(self, modelXbrl, concept, concept_facts):
        """
        Comprehensive dimensional facts extraction using Arelle's full dimensional model
        This method leverages Arelle's advanced dimensional relationship capabilities
        Enhanced with additional dimensional discovery when available
        """
        
        # FIRST: Filter concept facts to primary period only to avoid multiple historical periods
        primary_period_concept_facts = self._filter_facts_to_primary_period(concept_facts, modelXbrl)
        
        # Start with the basic dimensional facts from PRIMARY PERIOD concept facts
        dimensional_facts = self._extract_dimensional_facts(primary_period_concept_facts)
        
        # Enhanced dimensional extraction if enabled - RUN ONLY ONCE PER FILING
        if (self.enable_enhanced_dimensions and hasattr(self, 'current_model') and self.current_model 
            and not hasattr(self, '_dimensional_enhancement_completed')):
            try:
                # Build a comprehensive list of presentation facts (once) so we can
                # detect concepts with dimensional data that are NOT in any line items.
                # Previously we only passed the current concept's facts which caused
                # presentation_concept_qnames to be nearly empty and suppressed discovery.
                if not hasattr(self, '_all_presentation_facts'):
                    presentation_concept_qnames: Set[str] = set()
                    try:
                        rel_sets = []
                        # relationshipSets in Arelle is typically a dict keyed by (arcrole, linkrole, linkqname, arcqname)
                        rel_sets_container = getattr(self.current_model, 'relationshipSets', None)
                        if isinstance(rel_sets_container, dict):
                            for key, rs in rel_sets_container.items():
                                try:
                                    if isinstance(key, tuple) and len(key) > 0 and key[0] == XbrlConst.parentChild:
                                        rel_sets.append(rs)
                                except Exception:
                                    continue
                        # Fallback single set
                        if not rel_sets:
                            single_rel_set = self.current_model.relationshipSet(XbrlConst.parentChild)
                            if single_rel_set:
                                rel_sets = [single_rel_set]

                        for rel_set in rel_sets:
                            for rel in getattr(rel_set, 'modelRelationships', []) or []:
                                if rel.fromModelObject is not None and hasattr(rel.fromModelObject, 'qname'):
                                    presentation_concept_qnames.add(str(rel.fromModelObject.qname))
                                if rel.toModelObject is not None and hasattr(rel.toModelObject, 'qname'):
                                    presentation_concept_qnames.add(str(rel.toModelObject.qname))
                    except Exception as e:
                        logger.debug(f"Failed building presentation concept list: {e}")

                    # Safeguard: pull concepts from previously built statements hierarchy if any
                    if not presentation_concept_qnames and self.statements:
                        for stmt in self.statements.values():
                            for li in stmt.get('line_items', []):
                                concept_name = li.get('concept')
                                if concept_name:
                                    presentation_concept_qnames.add(str(concept_name))

                    if not presentation_concept_qnames:
                        logger.debug("Presentation concept list empty after relationship scan; missing concept detection may under-report.")
                    else:
                        logger.debug(f"Collected {len(presentation_concept_qnames)} presentation concepts across link roles for enhancement baseline")

                    all_pres_facts = [f for f in self.current_model.facts if (
                        f.concept is not None and hasattr(f.concept, 'qname') and str(f.concept.qname) in presentation_concept_qnames
                    )]
                    self._all_presentation_facts = all_pres_facts if all_pres_facts else []
                
                presentation_facts_for_enhancement = self._all_presentation_facts if hasattr(self, '_all_presentation_facts') else concept_facts
                
                # Initialize the enhanced dimensional extractor if not already done
                if not self.enhanced_dimensional_extractor and ENHANCED_DIMENSIONAL_AVAILABLE:
                    from core.extractors.dimensional_data_extractor import EnhancedDimensionalExtractor
                    self.enhanced_dimensional_extractor = EnhancedDimensionalExtractor(self)
                
                # Use enhanced dimensional extraction with FULL presentation facts list
                if self.enhanced_dimensional_extractor and ENHANCED_DIMENSIONAL_AVAILABLE:
                    from core.extractors.dimensional_data_extractor import enhance_dimensional_extraction
                    enhanced_results = enhance_dimensional_extraction(
                        self, self.current_model, presentation_facts_for_enhancement
                    )
                    
                    # Store enhanced results for reuse across all concepts
                    self._enhanced_dimensional_results = enhanced_results
                    self._dimensional_enhancement_completed = True
                    
                    # Merge enhanced facts with existing dimensional facts
                    if enhanced_results and 'enhanced_facts' in enhanced_results:
                        enhanced_dimensional_facts = enhanced_results['enhanced_facts']
                        for enhanced_fact in enhanced_dimensional_facts:
                            is_duplicate = any(
                                existing.get('context_id') == enhanced_fact.get('context_id')
                                for existing in dimensional_facts
                            )
                            if not is_duplicate and enhanced_fact:
                                enhanced_fact['enhanced_extraction'] = True
                                dimensional_facts.append(enhanced_fact)
                                
            except Exception as e:
                logger.debug(f"Enhanced dimensional extraction failed for {concept.qname}: {e}")
                # Continue with standard extraction
                                
        # Reuse cached enhanced results for subsequent concepts
        elif (hasattr(self, '_enhanced_dimensional_results') and 
              self._enhanced_dimensional_results and 
              'enhanced_facts' in self._enhanced_dimensional_results):
            enhanced_dimensional_facts = self._enhanced_dimensional_results['enhanced_facts']
            
            # Filter enhanced facts that match this specific concept
            concept_qname = str(concept.qname) if hasattr(concept, 'qname') else None
            if concept_qname:
                for enhanced_fact in enhanced_dimensional_facts:
                    if (enhanced_fact.get('concept') == concept_qname or 
                        enhanced_fact.get('name') == concept_qname):
                        is_duplicate = any(
                            existing.get('context_id') == enhanced_fact.get('context_id')
                            for existing in dimensional_facts
                        )
                        
                        if not is_duplicate and enhanced_fact:
                            # Mark as enhanced extraction
                            enhanced_fact['enhanced_extraction'] = True
                            dimensional_facts.append(enhanced_fact)
        
        # If this concept participates in dimensional relationships, extract additional context
        if hasattr(concept, 'qname'):
            try:
                # Get all facts for this concept across the entire model
                all_concept_facts = [f for f in modelXbrl.facts if f.concept.qname == concept.qname]
                
                # FILTER TO PRIMARY PERIOD ONLY to avoid multiple historical periods
                primary_period_facts = self._filter_facts_to_primary_period(all_concept_facts, modelXbrl)
                
                # Group facts by their dimensional contexts
                dimensional_groups = {}
                
                for fact in primary_period_facts:
                    if (fact.context is not None and 
                        hasattr(fact.context, 'qnameDims') and 
                        fact.context.qnameDims is not None):
                        
                        # Create a key representing the dimensional combination
                        dim_key = tuple(sorted([
                            f"{dim.localName}:{member.memberQname.localName if hasattr(member, 'memberQname') else str(member)}"
                            for dim, member in fact.context.qnameDims.items()
                        ]))
                        
                        if dim_key not in dimensional_groups:
                            dimensional_groups[dim_key] = []
                        dimensional_groups[dim_key].append(fact)
                
                # Extract comprehensive dimensional data for each group
                for dim_key, facts_group in dimensional_groups.items():
                    # Use the most representative fact from each group
                    representative_fact = max(facts_group, key=lambda f: (
                        f.xValue is not None,
                        len(f.context.qnameDims) if hasattr(f.context, 'qnameDims') else 0
                    ))
                    
                    # Extract detailed dimensional information
                    comprehensive_fact = self._extract_single_dimensional_fact_detailed(representative_fact)
                    
                    # Check if this fact is already in our dimensional_facts
                    is_duplicate = any(
                        existing.get('context_id') == comprehensive_fact.get('context_id')
                        for existing in dimensional_facts
                    )
                    
                    if not is_duplicate and comprehensive_fact:
                        dimensional_facts.append(comprehensive_fact)
                
                # Use Arelle's dimension-defaults model if available
                if hasattr(modelXbrl, 'dimensionDefaultConcepts'):
                    self._extract_dimension_defaults(modelXbrl, concept, dimensional_facts)
                
            except Exception as e:
                logger.debug(f"Error in comprehensive dimensional extraction for {concept.qname}: {e}")
        
        # FINAL STEP: Apply period filtering to any enhanced facts that may have been added
        # Convert dimensional_facts back to fact-like objects for filtering, then back to dimensional format
        filtered_dimensional_facts = []
        for dim_fact in dimensional_facts:
            period_str = dim_fact.get('period', '')
            
            # Check if this dimensional fact's period matches the primary period pattern
            if self._is_primary_period(period_str):
                filtered_dimensional_facts.append(dim_fact)
            else:
                # Debug: track filtered dimensional facts
                if not hasattr(self, '_filtered_dimensional_count'):
                    self._filtered_dimensional_count = 0
                self._filtered_dimensional_count += 1
        
        if hasattr(self, '_filtered_dimensional_count') and self._filtered_dimensional_count > 0:
            logger.debug(f"Filtered {self._filtered_dimensional_count} dimensional facts from non-primary periods")
        
        return filtered_dimensional_facts
    
    def _extract_single_dimensional_fact_detailed(self, fact):
        """Extract detailed information from a single dimensional fact using Arelle's full dimensional model"""
        try:
            # Extract fact value with enhanced parsing
            value = None
            raw_value = None
            
            if hasattr(fact, 'xValue') and fact.xValue is not None:
                raw_value = fact.xValue
            elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                raw_value = fact.effectiveValue
            
            if raw_value is not None:
                if isinstance(raw_value, (int, float, Decimal)):
                    value = float(raw_value)
                else:
                    try:
                        clean_value = str(raw_value).replace(',', '').replace('$', '').strip()
                        if clean_value and clean_value not in ['', '-', 'N/A', 'n/a']:
                            value = float(clean_value)
                    except (ValueError, TypeError):
                        value = None
            
            # Enhanced dimensional context extraction using Arelle's dimensional API
            dimensions = {}
            dimension_details = {}
            
            if (fact.context is not None and 
                hasattr(fact.context, 'qnameDims') and 
                fact.context.qnameDims is not None):
                
                for dim_qname, dim_value in fact.context.qnameDims.items():
                    axis_local_name = dim_qname.localName if hasattr(dim_qname, 'localName') else str(dim_qname).split(':')[-1]
                    
                    # Use Arelle's dimensional methods for comprehensive extraction
                    dimension_info = {
                        'axis_qname': str(dim_qname),
                        'axis_local_name': axis_local_name,
                        'axis_namespace': dim_qname.namespaceURI if hasattr(dim_qname, 'namespaceURI') else None
                    }
                    
                    if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit:
                        # Use Arelle's dimMemberQname method for explicit dimensions
                        member_qname = fact.context.dimMemberQname(dim_qname)
                        if member_qname:
                            member_name = member_qname.localName if hasattr(member_qname, 'localName') else str(member_qname).split(':')[-1]
                            
                            dimensions[axis_local_name] = member_name
                            dimension_info.update({
                                'type': 'explicit',
                                'member_qname': str(member_qname),
                                'member_local_name': member_name,
                                'member_namespace': member_qname.namespaceURI if hasattr(member_qname, 'namespaceURI') else None,
                                'member_label': self._get_concept_label(member_qname)
                            })
                            
                            # Get member concept for additional information
                            if hasattr(fact.context.modelXbrl, 'qnameConcepts'):
                                member_concept = fact.context.modelXbrl.qnameConcepts.get(member_qname)
                                if member_concept is not None:
                                    dimension_info['member_documentation'] = getattr(member_concept, 'documentation', None)
                                    dimension_info['member_type'] = str(member_concept.typeQname) if hasattr(member_concept, 'typeQname') else None
                    
                    elif hasattr(dim_value, 'typedMember') and dim_value.typedMember is not None:
                        # Enhanced typed dimension handling
                        typed_member = dim_value.typedMember
                        typed_value = None
                        
                        # Try multiple ways to extract typed value
                        if hasattr(typed_member, 'stringValue'):
                            typed_value = typed_member.stringValue
                        elif hasattr(typed_member, 'textValue'):
                            typed_value = typed_member.textValue
                        elif hasattr(typed_member, 'xValue'):
                            typed_value = str(typed_member.xValue)
                        else:
                            typed_value = str(typed_member)
                        
                        if typed_value:
                            dimensions[axis_local_name] = typed_value
                            dimension_info.update({
                                'type': 'typed',
                                'typed_value': typed_value,
                                'typed_element_name': typed_member.localName if hasattr(typed_member, 'localName') else None,
                                'typed_element_namespace': typed_member.namespaceURI if hasattr(typed_member, 'namespaceURI') else None
                            })
                    
                    # Get dimension concept information using Arelle's model
                    if hasattr(fact.context.modelXbrl, 'qnameConcepts'):
                        dim_concept = fact.context.modelXbrl.qnameConcepts.get(dim_qname)
                        if dim_concept is not None:
                            dimension_info['axis_label'] = getattr(dim_concept, 'label', None)
                            dimension_info['axis_documentation'] = getattr(dim_concept, 'documentation', None)
                            dimension_info['axis_type'] = str(dim_concept.typeQname) if hasattr(dim_concept, 'typeQname') else None
                    
                    dimension_details[axis_local_name] = dimension_info
            
            # Enhanced period information using Arelle's context methods
            period_info = ""
            period_details = {}
            if fact.context is not None and hasattr(fact.context, 'period'):
                if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                    instant_date = fact.context.instantDatetime if fact.context.instantDatetime else None
                    period_info = str(instant_date) if instant_date else ""
                    period_details = {
                        'type': 'instant',
                        'instant_date': str(instant_date) if instant_date else None
                    }
                elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                    start_date = fact.context.startDatetime if fact.context.startDatetime else None
                    end_date = fact.context.endDatetime if fact.context.endDatetime else None
                    period_info = f"{start_date} to {end_date}" if start_date and end_date else ""
                    period_details = {
                        'type': 'duration',
                        'start_date': str(start_date) if start_date else None,
                        'end_date': str(end_date) if end_date else None
                    }
            
            # Enhanced unit information
            unit_details = {}
            if fact.unit is not None:
                unit_details = {
                    'id': fact.unitID,
                    'value': str(fact.unit.value) if hasattr(fact.unit, 'value') else None,
                    'measures': None
                }
                
                # Extract unit measures using Arelle's unit model
                if hasattr(fact.unit, 'measures'):
                    multiply_measures, divide_measures = fact.unit.measures
                    unit_details['measures'] = {
                        'multiply': [str(m) for m in multiply_measures] if multiply_measures else [],
                        'divide': [str(m) for m in divide_measures] if divide_measures else [],
                        'is_divide': fact.unit.isDivide if hasattr(fact.unit, 'isDivide') else False,
                        'is_single_measure': fact.unit.isSingleMeasure if hasattr(fact.unit, 'isSingleMeasure') else False
                    }
            
            # Extract concept and label information for identification
            concept_name = str(fact.concept.qname) if (fact.concept is not None) else None
            fact_label = self._get_concept_label(fact.concept.qname) if (fact.concept is not None) else None
            
            return {
                'value': value,
                'dimensions': dimensions,
                'dimension_details': dimension_details,
                'context_id': fact.contextID,
                'unit_details': unit_details,
                'period_info': period_info,
                'period_details': period_details,
                'concept_name': concept_name,
                'concept_local_name': fact.concept.qname.localName if (fact.concept is not None and hasattr(fact.concept.qname, 'localName')) else None,
                'concept_namespace': fact.concept.qname.namespaceURI if (fact.concept is not None and hasattr(fact.concept.qname, 'namespaceURI')) else None,
                'fact_label': fact_label,
                'has_dimensions': len(dimensions) > 0,
                'dimension_count': len(dimensions),
                'precision': getattr(fact, 'precision', None),
                'decimals': getattr(fact, 'decimals', None),
                'fact_id': getattr(fact, 'id', None)
            }
            
            # Remove null precision/decimals if desired (optional optimization)
            if result['precision'] is None:
                del result['precision']
            if result['decimals'] is None:
                del result['decimals']
            
        except Exception as e:
            logger.debug(f"Error extracting detailed dimensional fact: {e}")
            return None
    
    def _extract_dimension_defaults(self, modelXbrl, concept, dimensional_facts):
        """Extract dimension default information using Arelle's dimension model"""
        try:
            # This method leverages Arelle's built-in dimensional default handling
            # It ensures we capture default dimensional members that might not appear explicitly
            
            if hasattr(modelXbrl, 'dimensionDefaultConcepts'):
                for default_concept in modelXbrl.dimensionDefaultConcepts.values():
                    if hasattr(default_concept, 'qname'):
                        # Add logic to extract default dimensional relationships
                        # This is advanced Arelle functionality for complete dimensional coverage
                        pass
                        
        except Exception as e:
            logger.debug(f"Error extracting dimension defaults: {e}")
    
    def _extract_unit_measures(self, unit):
        """Extract comprehensive unit measure information using Arelle's unit model"""
        if not unit:
            return None
        
        try:
            unit_info = {
                'id': getattr(unit, 'id', None),
                'value': str(unit.value) if hasattr(unit, 'value') else None,
                'is_divide': unit.isDivide if hasattr(unit, 'isDivide') else False,
                'is_single_measure': unit.isSingleMeasure if hasattr(unit, 'isSingleMeasure') else False
            }
            
            # Extract measures using Arelle's measures property
            if hasattr(unit, 'measures'):
                multiply_measures, divide_measures = unit.measures
                unit_info['measures'] = {
                    'multiply': [str(m) for m in multiply_measures] if multiply_measures else [],
                    'divide': [str(m) for m in divide_measures] if divide_measures else []
                }
                
                # Add detailed measure information
                unit_info['multiply_count'] = len(multiply_measures) if multiply_measures else 0
                unit_info['divide_count'] = len(divide_measures) if divide_measures else 0
                
                # Extract namespace and local names for measures
                if multiply_measures:
                    unit_info['multiply_details'] = []
                    for measure in multiply_measures:
                        measure_detail = {
                            'qname': str(measure),
                            'local_name': measure.localName if hasattr(measure, 'localName') else None,
                            'namespace': measure.namespaceURI if hasattr(measure, 'namespaceURI') else None
                        }
                        unit_info['multiply_details'].append(measure_detail)
                
                if divide_measures:
                    unit_info['divide_details'] = []
                    for measure in divide_measures:
                        measure_detail = {
                            'qname': str(measure),
                            'local_name': measure.localName if hasattr(measure, 'localName') else None,
                            'namespace': measure.namespaceURI if hasattr(measure, 'namespaceURI') else None
                        }
                        unit_info['divide_details'].append(measure_detail)
            
            # Add hash information if available
            if hasattr(unit, 'hash'):
                unit_info['hash'] = unit.hash
            if hasattr(unit, 'md5hash'):
                unit_info['md5hash'] = unit.md5hash
            
            return unit_info
            
        except Exception as e:
            logger.debug(f"Error extracting unit measures: {e}")
            return None
    
    def _extract_period_info(self, context):
        """Extract comprehensive period information using Arelle's context model"""
        if not context:
            return None
        
        try:
            period_info = {
                'type': None,
                'period_string': None,
                'start_date': None,
                'end_date': None,
                'instant_date': None,
                'is_forever': False
            }
            
            if hasattr(context, 'period'):
                # Check for instant period using Arelle's methods
                if hasattr(context, 'isInstantPeriod') and context.isInstantPeriod:
                    period_info['type'] = 'instant'
                    if context.instantDatetime:
                        instant_date = context.instantDatetime
                        period_info['instant_date'] = str(instant_date)
                        period_info['period_string'] = str(instant_date)
                
                # Check for duration period using Arelle's methods
                elif hasattr(context, 'isStartEndPeriod') and context.isStartEndPeriod:
                    period_info['type'] = 'duration'
                    start_date = context.startDatetime if context.startDatetime else None
                    end_date = context.endDatetime if context.endDatetime else None
                    
                    if start_date:
                        period_info['start_date'] = str(start_date)
                    if end_date:
                        period_info['end_date'] = str(end_date)
                    
                    if start_date and end_date:
                        period_info['period_string'] = f"{start_date} to {end_date}"
                    elif end_date:
                        period_info['period_string'] = f"ending {end_date}"
                    elif start_date:
                        period_info['period_string'] = f"starting {start_date}"
                
                # Check for forever period using Arelle's methods
                elif hasattr(context, 'isForeverPeriod') and context.isForeverPeriod:
                    period_info['type'] = 'forever'
                    period_info['is_forever'] = True
                    period_info['period_string'] = 'forever'
                
                # Add additional period metadata if available
                if hasattr(context.period, 'localName'):
                    period_info['period_element'] = context.period.localName
                
                # Calculate period duration if it's a duration period
                if (period_info['type'] == 'duration' and 
                    period_info['start_date'] and period_info['end_date']):
                    try:
                        from datetime import datetime
                        start = datetime.fromisoformat(period_info['start_date'].replace('Z', '+00:00'))
                        end = datetime.fromisoformat(period_info['end_date'].replace('Z', '+00:00'))
                        duration = end - start
                        period_info['duration_days'] = duration.days
                    except Exception:
                        pass
            
            return period_info
            
        except Exception as e:
            logger.debug(f"Error extracting period info: {e}")
            return None
    
    def _extract_fact_value(self, fact):
        """Extract comprehensive fact value using Arelle's fact model"""
        if not fact:
            return None
        
        try:
            # Try multiple value extraction methods in order of preference
            value = None
            raw_value = None
            value_type = None
            
            # 1. Try xValue (Arelle's processed value)
            if hasattr(fact, 'xValue') and fact.xValue is not None:
                raw_value = fact.xValue
                value_type = 'xValue'
            
            # 2. Try effectiveValue (Arelle's effective value after transformations)
            elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                raw_value = fact.effectiveValue
                value_type = 'effectiveValue'
            
            # 3. Try value property (basic value)
            elif hasattr(fact, 'value') and fact.value is not None:
                raw_value = fact.value
                value_type = 'value'
            
            # 4. Try textValue (string representation)
            elif hasattr(fact, 'textValue') and fact.textValue is not None:
                raw_value = fact.textValue
                value_type = 'textValue'
            
            # 5. Try stringValue (another string representation)
            elif hasattr(fact, 'stringValue') and fact.stringValue is not None:
                raw_value = fact.stringValue
                value_type = 'stringValue'
            
            if raw_value is not None:
                # Handle different value types
                if isinstance(raw_value, (int, float, Decimal)):
                    value = float(raw_value)
                elif isinstance(raw_value, bool):
                    value = raw_value
                elif isinstance(raw_value, str):
                    # Try to parse numeric values from strings
                    try:
                        # Remove common formatting characters
                        clean_value = str(raw_value).replace(',', '').replace('$', '').replace('(', '-').replace(')', '').strip()
                        
                        # Skip empty or placeholder values
                        if clean_value and clean_value not in ['', '-', 'N/A', 'n/a', 'nil', 'NULL']:
                            # Try to convert to float
                            if '.' in clean_value or 'e' in clean_value.lower():
                                value = float(clean_value)
                            else:
                                # Try integer first, then float
                                try:
                                    value = int(clean_value)
                                except ValueError:
                                    value = float(clean_value)
                        else:
                            # Keep string values that couldn't be converted
                            value = str(raw_value) if raw_value != '' else None
                    except (ValueError, TypeError):
                        # Keep as string if numeric conversion fails
                        value = str(raw_value) if raw_value != '' else None
                else:
                    # For other types, convert to string
                    value = str(raw_value) if raw_value != '' else None
            
            # Additional value metadata
            value_info = {
                'value': value,
                'raw_value': str(raw_value) if raw_value is not None else None,
                'value_type': value_type,
                'is_numeric': isinstance(value, (int, float)),
                'is_nil': getattr(fact, 'isNil', False) if hasattr(fact, 'isNil') else False,
                'precision': getattr(fact, 'precision', None),
                'decimals': getattr(fact, 'decimals', None)
            }
            
            # Check for nil or empty values using Arelle's methods
            if hasattr(fact, 'isNil') and fact.isNil:
                value_info['value'] = None
                value_info['is_nil'] = True
            
            # Return just the value for backwards compatibility, but info is available
            return value
            
        except Exception as e:
            logger.debug(f"Error extracting fact value: {e}")
            return None
    
    def _extract_calculations(self, modelXbrl, concept):
        """Extract calculation relationships for a concept with proper null checking"""
        calculations = {
            'is_summation_parent': False,
            'summation_children': [],
            'is_summation_child': False,
            'summation_parents': []
        }
        
        # Get calculation relationships
        calc_relationships = modelXbrl.relationshipSet(XbrlConst.summationItem)
        if not calc_relationships:
            return calculations
        
        # Check if this concept is a summation parent
        from_rels = calc_relationships.fromModelObject(concept)
        if from_rels:
            calculations['is_summation_parent'] = True
            for rel in from_rels:
                if hasattr(rel, 'toModelObject') and rel.toModelObject is not None:
                    child_data = {
                        'concept': str(rel.toModelObject.qname),
                        'weight': getattr(rel, 'weight', 1.0),
                        'order': getattr(rel, 'order', None),
                        'label': rel.toModelObject.label() if (hasattr(rel.toModelObject, 'label') and rel.toModelObject.label()) else None
                    }
                    calculations['summation_children'].append(child_data)
        
        # Check if this concept is a summation child
        to_rels = calc_relationships.toModelObject(concept)
        if to_rels:
            calculations['is_summation_child'] = True
            for rel in to_rels:
                if hasattr(rel, 'fromModelObject') and rel.fromModelObject is not None:
                    parent_data = {
                        'concept': str(rel.fromModelObject.qname),
                        'weight': getattr(rel, 'weight', 1.0),
                        'order': getattr(rel, 'order', None),
                        'label': rel.fromModelObject.label() if (hasattr(rel.fromModelObject, 'label') and rel.fromModelObject.label()) else None
                    }
                    calculations['summation_parents'].append(parent_data)
        
        return calculations
    
    def export_to_json(self, financial_data: Dict[str, Any], output_dir: str = "financial_statements_output", primary_period_only: bool = True) -> Dict[str, str]:
        """Export only core financial statements (Income, Cash Flow, Balance Sheet) to JSON files
        
        Args:
            financial_data: Financial data dictionary from extraction
            output_dir: Output directory for JSON files
            primary_period_only: If True, export only primary period; if False, export all periods
        """
        
        os.makedirs(output_dir, exist_ok=True)
        
        filing_info = financial_data.get('filing_info', {})
        entity_info = filing_info.get('entity_info', {})
        
        # Create safe filename components
        entity_name = entity_info.get('entity_name', 'unknown_entity')
        entity_cik = entity_info.get('entity_cik', 'unknown_cik')
        
        safe_entity_name = "".join(c for c in str(entity_name) if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_entity_name = safe_entity_name.replace(' ', '_')[:50]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        exported_files = {}
        
        # Only export core financial statements
        core_statements = ['income_statement', 'cash_flow', 'balance_sheet']
        statements = financial_data.get('statements', {})
        
        for statement_type, statement_data in statements.items():
            # Skip non-core statements
            if statement_type not in core_statements:
                continue
                
            # Create statement-specific filename
            statement_file = os.path.join(
                output_dir, 
                f"{safe_entity_name}_{entity_cik}_{statement_type}_{timestamp}.json"
            )
            
            # Identify primary period for this statement
            primary_period = None
            if primary_period_only:
                primary_period = self._identify_primary_period(filing_info, statement_data['hierarchy'], statement_type)
                if primary_period:
                    logger.debug(f"Filtering {statement_type} to primary period: {self._format_period_display(primary_period)}")
                    filtered_hierarchy = self._filter_hierarchy_to_primary_period(statement_data['hierarchy'], primary_period)
                else:
                    logger.debug(f"No primary period found for {statement_type}, exporting all data")
                    filtered_hierarchy = statement_data['hierarchy']
            else:
                filtered_hierarchy = statement_data['hierarchy']
            
            # Convert hierarchy to JSON-serializable format
            json_hierarchy = self._convert_hierarchy_to_json(filtered_hierarchy, filing_info)
            
            # Calculate enhanced statistics
            stats = self._calculate_statement_stats(filtered_hierarchy)
            
            statement_export = {
                "statement_metadata": {
                    "statement_type": statement_type,
                    "description": statement_data.get('description', ''),
                    "role_uri": statement_data.get('role_uri', ''),
                    "taxonomy_info": statement_data.get('taxonomy_info', {}),
                    "export_timestamp": datetime.now().isoformat(),
                    "export_mode": "Primary Period Only" if primary_period_only else "All Periods",
                    "primary_period": primary_period,
                    "primary_period_display": self._format_period_display(primary_period) if primary_period else None,
                    "statistics": stats
                },
                "filing_info": filing_info,
                "hierarchy": json_hierarchy
            }
            
            # Write statement file
            with open(statement_file, 'w', encoding='utf-8') as f:
                json.dump(statement_export, f, indent=2, ensure_ascii=False, default=str)
            
            exported_files[statement_type] = statement_file
        
        return exported_files
    
    def export_to_excel(self, financial_data: Dict[str, Any], output_dir: str = "financial_statements_output", primary_period_only: bool = True) -> Dict[str, str]:
        """Export only core financial statements (Income, Cash Flow, Balance Sheet) to Excel files
        
        Args:
            financial_data: Financial data dictionary from extraction
            output_dir: Output directory for Excel files
            primary_period_only: If True, export only primary period; if False, export all periods
        """
        
        if not EXCEL_AVAILABLE:
            logger.warning("Excel export not available. Please install: pip install openpyxl xlsxwriter")
            return {}
        
        # Store the filtering preference for use in sheet creation
        self._primary_period_only = primary_period_only
        
        os.makedirs(output_dir, exist_ok=True)
        
        filing_info = financial_data.get('filing_info', {})
        entity_info = filing_info.get('entity_info', {})
        
        # Create safe filename components
        entity_name = entity_info.get('entity_name', 'unknown_entity')
        entity_cik = entity_info.get('entity_cik', 'unknown_cik')
        
        safe_entity_name = "".join(c for c in str(entity_name) if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_entity_name = safe_entity_name.replace(' ', '_')[:50]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        exported_files = {}
        statements = financial_data.get('statements', {})
        
        # Only export core financial statements
        core_statements = ['income_statement', 'cash_flow', 'balance_sheet']
        
        for statement_type, statement_data in statements.items():
            # Skip non-core statements
            if statement_type not in core_statements:
                continue
                
            # Create individual Excel files for each core statement
            individual_file = os.path.join(
                output_dir, 
                f"{safe_entity_name}_{entity_cik}_{statement_type}_{timestamp}.xlsx"
            )
            
            with pd.ExcelWriter(individual_file, engine='openpyxl') as individual_writer:
                self._create_statement_sheet(individual_writer, statement_type, statement_data, filing_info)
                self._format_excel_sheet(individual_writer.sheets[statement_type.replace('_', ' ').title()[:31]])
            
            exported_files[f"{statement_type}_excel"] = individual_file
        
        return exported_files
    
    def _create_summary_sheet(self, writer: pd.ExcelWriter, financial_data: Dict[str, Any]):
        """Create summary sheet with overview of all statements"""
        
        filing_info = financial_data.get('filing_info', {})
        entity_info = filing_info.get('entity_info', {})
        statements = financial_data.get('statements', {})
        
        # Create summary data
        summary_rows = []
        
        # Filing information
        summary_rows.extend([
            ["FILING INFORMATION", "", "", "", ""],
            ["Entity Name", entity_info.get('entity_name', ''), "", "", ""],
            ["Entity CIK", entity_info.get('entity_cik', ''), "", "", ""],
            ["Document Type", entity_info.get('document_type', ''), "", "", ""],
            ["Period End Date", entity_info.get('document_period_end', ''), "", "", ""],
            ["Fiscal Year", entity_info.get('fiscal_year', ''), "", "", ""],
            ["Fiscal Period", entity_info.get('fiscal_period', ''), "", "", ""],
            ["", "", "", "", ""],
            ["STATEMENTS OVERVIEW", "", "", "", ""],
            ["Statement Type", "Total Items", "Items with Values", "Dimensional Items", "Primary Taxonomy"]
        ])
        
        # Statement statistics
        for statement_type, statement_data in statements.items():
            stats = self._calculate_statement_stats(statement_data['hierarchy'])
            taxonomy_info = statement_data.get('taxonomy_info', {})
            primary_taxonomy = taxonomy_info.get('primary_taxonomy', 'unknown')
            
            summary_rows.append([
                statement_type.replace('_', ' ').title(),
                stats['total_items'],
                stats['items_with_values'],
                stats['items_with_dimensions'],
                primary_taxonomy.upper()
            ])
        
        # Add taxonomies information
        summary_rows.extend([
            ["", "", "", "", ""],
            ["DISCOVERED TAXONOMIES", "", "", "", ""],
            ["Taxonomy", "Status", "", "", ""]
        ])
        
        for taxonomy in financial_data.get('discovered_taxonomies', []):
            summary_rows.append([taxonomy.upper(), "Discovered", "", "", ""])
        
        # Create DataFrame and export
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_excel(writer, sheet_name='Summary', index=False, header=False)
    
    def _create_statement_sheet(self, writer: pd.ExcelWriter, statement_type: str, statement_data: Dict[str, Any], filing_info: Dict[str, Any]):
        """Create detailed sheet for a single financial statement with configurable period filtering"""
        
        hierarchy = statement_data['hierarchy']
        
        # Use the filtering preference from export_to_excel
        primary_only = getattr(self, '_primary_period_only', True)
        
        # Collect periods based on filtering preference
        all_periods = self._collect_unique_periods(hierarchy, filing_info, primary_only=primary_only, statement_type=statement_type)
        sorted_periods = sorted(all_periods, reverse=True)  # Most recent first
        
        # Header information
        entity_info = filing_info.get('entity_info', {})
        
        # Create header rows
        header_rows = []
        header_rows.append([entity_info.get('entity_name', '')] + [''] * (len(sorted_periods) + 4))
        header_rows.append([f"{statement_type.replace('_', ' ').title()}"] + [''] * (len(sorted_periods) + 4))
        
        period_description = "Primary Period Only" if primary_only else "All Periods Available"
        header_rows.append([period_description] + [''] * (len(sorted_periods) + 4))
        header_rows.append([''] * (len(sorted_periods) + 5))
        
        # Column headers
        column_headers = ["Concept", "Label", "Unit", "Level", "Abstract"]
        for period in sorted_periods:
            period_display = self._format_period_display(period)
            column_headers.append(period_display)
        
        header_rows.append(column_headers)
        
        # Extract data from hierarchy with period-based columns
        data_rows = []
        
        def extract_data_with_periods(items: List[FinancialLineItem]):
            for item in items:
                # Basic item information
                concept_display = "  " * item.level + item.concept_name.split(":")[-1]
                label_display = "  " * item.level + item.label
                
                # Create base row
                row = [
                    concept_display,
                    label_display,
                    item.unit_id or "",
                    item.level,
                    "Yes" if item.abstract else "No"
                ]
                
                # Add values for each period
                period_values = self._get_period_values_for_item(item, sorted_periods)
                row.extend(period_values)
                
                data_rows.append(row)
                
                # Add dimensional breakdowns as separate rows
                dimensional_rows = self._create_dimensional_rows(item, sorted_periods)
                data_rows.extend(dimensional_rows)
                
                # Recursively process children
                extract_data_with_periods(item.children)
        
        extract_data_with_periods(hierarchy)
        
        # Combine all rows
        all_rows = header_rows + data_rows
        
        # Create DataFrame and export
        statement_df = pd.DataFrame(all_rows)
        sheet_name = statement_type.replace('_', ' ').title()[:31]  # Excel sheet name limit
        statement_df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)
    
    def _identify_primary_period(self, filing_info: Dict[str, Any], hierarchy: List[FinancialLineItem], statement_type: Optional[str] = None) -> str:
        """Identify the primary reporting period using directly extracted XBRL context information"""
        
        # Get the primary period info that was extracted directly from XBRL contexts
        primary_period_info = filing_info.get('primary_period_info', {})
        
        # For balance sheet, use instant period
        if statement_type == 'balance_sheet':
            instant_period = primary_period_info.get('instant_period')
            if instant_period:
                logger.debug(f"Primary period for {statement_type} from XBRL contexts: {instant_period}")
                return instant_period
        
        # For income statement and cash flow, use duration period
        elif statement_type in ['income_statement', 'cash_flow']:
            duration_period = primary_period_info.get('duration_period')
            if duration_period:
                logger.debug(f"Primary period for {statement_type} from XBRL contexts: {duration_period}")
                return duration_period
        
        # If no statement type specified or no direct period found, use the old logic as fallback
        instant_period = primary_period_info.get('instant_period')
        duration_period = primary_period_info.get('duration_period')
        
        # Prefer duration period for most statements, instant for balance sheet-like data
        if duration_period:
            logger.debug(f"Primary period from XBRL contexts (duration): {duration_period}")
            return duration_period
        elif instant_period:
            logger.debug(f"Primary period from XBRL contexts (instant): {instant_period}")
            return instant_period
        
        # Fallback to old method if direct extraction failed
        logger.debug("Using fallback primary period identification")
        return self._identify_primary_period_fallback(filing_info, hierarchy)
    
    def _identify_primary_period_fallback(self, filing_info: Dict[str, Any], hierarchy: List[FinancialLineItem]) -> str:
        """Fallback primary period identification method (original logic)"""
        
        from datetime import datetime
        
        # Get document period end from filing info
        entity_info = filing_info.get('entity_info', {})
        doc_period_end = entity_info.get('document_period_end', '')
        fiscal_period = entity_info.get('fiscal_period', '')
        
        # Collect all periods first
        all_periods = set()
        
        def collect_all_periods(items: List[FinancialLineItem]):
            for item in items:
                if item.period:
                    all_periods.add(item.period)
                
                # Collect periods from dimensional facts
                for dim_fact in item.all_dimensional_facts:
                    period = dim_fact.get('period', '')
                    if period:
                        all_periods.add(period)
                
                collect_all_periods(item.children)
        
        collect_all_periods(hierarchy)
        
        if not all_periods:
            logger.debug("No periods found in hierarchy")
            return ""
        
        # Parse document period end date for comparison
        doc_end_date = None
        if doc_period_end:
            try:
                doc_end_date = datetime.strptime(doc_period_end, "%Y-%m-%d")
            except ValueError:
                pass
        
        # Strategy 1: Look for period that exactly matches or is very close to document period end
        if doc_end_date:
            for period in all_periods:
                # Check instant periods (balance sheet dates)
                if " to " not in period:
                    try:
                        period_date = datetime.strptime(period.split(" ")[0], "%Y-%m-%d")
                        # Allow 1-2 days difference for fiscal year end dates
                        if abs((period_date - doc_end_date).days) <= 2:
                            logger.debug(f"Primary period identified by document end date match: {period}")
                            return period
                    except ValueError:
                        continue
                
                # Check duration periods (income statement, cash flow periods)
                else:
                    parts = period.split(" to ")
                    if len(parts) == 2:
                        try:
                            end_dt = datetime.strptime(parts[1].split(" ")[0], "%Y-%m-%d")
                            # Allow 1-2 days difference for fiscal year end dates
                            if abs((end_dt - doc_end_date).days) <= 2:
                                logger.debug(f"Primary period identified by document end date match: {period}")
                                return period
                        except ValueError:
                            continue
        
        # Strategy 2: For annual filings, find the most recent fiscal year period
        # Look for periods that are approximately 12 months (350-370 days)
        fiscal_year_periods = []
        instant_periods = []
        
        for period in all_periods:
            if " to " in period:
                # Duration period
                parts = period.split(" to ")
                if len(parts) == 2:
                    try:
                        start_dt = datetime.strptime(parts[0].split(" ")[0], "%Y-%m-%d")
                        end_dt = datetime.strptime(parts[1].split(" ")[0], "%Y-%m-%d")
                        
                        duration_days = (end_dt - start_dt).days
                        
                        # Consider as fiscal year if between 350-370 days (approximately 12 months)
                        if 350 <= duration_days <= 370:
                            fiscal_year_periods.append((period, end_dt, duration_days))
                    except ValueError:
                        continue
            else:
                # Instant period (balance sheet date)
                try:
                    period_date = datetime.strptime(period.split(" ")[0], "%Y-%m-%d")
                    instant_periods.append((period, period_date))
                except ValueError:
                    continue
        
        # If we found fiscal year periods, return the most recent one
        if fiscal_year_periods:
            # Sort by end date, most recent first
            fiscal_year_periods.sort(key=lambda x: x[1], reverse=True)
            most_recent_fiscal_year = fiscal_year_periods[0][0]
            logger.debug(f"Primary period identified as most recent fiscal year: {most_recent_fiscal_year}")
            return most_recent_fiscal_year
        
        # Strategy 3: If no fiscal year periods found, use the most recent instant period
        if instant_periods:
            instant_periods.sort(key=lambda x: x[1], reverse=True)
            most_recent_instant = instant_periods[0][0]
            logger.debug(f"Primary period identified as most recent instant period: {most_recent_instant}")
            return most_recent_instant
        
        # Strategy 4: Fallback to most recent period by string comparison
        if all_periods:
            primary_period = sorted(all_periods, reverse=True)[0]
            logger.debug(f"Primary period fallback to most recent: {primary_period}")
            return primary_period
        
        logger.debug("No primary period could be identified")
        return ""
    
    def _collect_unique_periods(self, hierarchy: List[FinancialLineItem], filing_info: Optional[Dict[str, Any]] = None, primary_only: bool = False, statement_type: Optional[str] = None) -> Set[str]:
        """Collect unique periods from the hierarchy, optionally filtering to primary period only"""
        
        if primary_only and filing_info:
            # Return only the primary period for this specific statement type
            primary_period = self._identify_primary_period(filing_info, hierarchy, statement_type)
            return {primary_period} if primary_period else set()
        
        # Original behavior: collect all periods
        periods = set()
        
        def collect_periods(items: List[FinancialLineItem]):
            for item in items:
                if item.period:
                    periods.add(item.period)
                
                # Collect periods from dimensional facts
                for dim_fact in item.all_dimensional_facts:
                    period = dim_fact.get('period', '')
                    if period:
                        periods.add(period)
                
                collect_periods(item.children)
        
        collect_periods(hierarchy)
        return periods
    
    def _filter_hierarchy_to_primary_period(self, hierarchy: List[FinancialLineItem], primary_period: str) -> List[FinancialLineItem]:
        """Filter hierarchy to only include data from the primary period"""
        
        def filter_item(item: FinancialLineItem) -> Optional[FinancialLineItem]:
            # Create a copy of the item
            filtered_item = FinancialLineItem(
                concept_name=item.concept_name,
                label=item.label,
                value=None,  # Will be set if matches primary period
                context_id="",
                unit_id=item.unit_id,
                period="",
                level=item.level,
                order=item.order,
                abstract=item.abstract,
                parent_concept=item.parent_concept,
                dimensions=[],
                all_dimensional_facts=[],
                calculations=item.calculations
            )
            
            # Check if primary fact matches primary period
            if item.period == primary_period and item.value is not None:
                filtered_item.value = item.value
                filtered_item.context_id = item.context_id
                filtered_item.period = item.period
                filtered_item.dimensions = item.dimensions
            
            # Filter dimensional facts to primary period only
            primary_dimensional_facts = []
            for dim_fact in item.all_dimensional_facts:
                if dim_fact.get('period', '') == primary_period:
                    primary_dimensional_facts.append(dim_fact)
            
            filtered_item.all_dimensional_facts = primary_dimensional_facts
            
            # Recursively filter children
            filtered_children = []
            for child in item.children:
                filtered_child = filter_item(child)
                if filtered_child:
                    filtered_children.append(filtered_child)
            
            filtered_item.children = filtered_children
            
            # Return item if it has data (value, dimensional facts, or children with data)
            has_data = (
                filtered_item.value is not None or
                len(filtered_item.all_dimensional_facts) > 0 or
                len(filtered_item.children) > 0 or
                filtered_item.abstract  # Keep abstract items for structure
            )
            
            return filtered_item if has_data else None
        
        # Filter the entire hierarchy
        filtered_hierarchy = []
        for item in hierarchy:
            filtered_item = filter_item(item)
            if filtered_item:
                filtered_hierarchy.append(filtered_item)
        
        return filtered_hierarchy
    
    def _format_period_display(self, period: str) -> str:
        """Format period for display in column headers with consistent labeling"""
        if not period:
            return "Unknown Period"
        
        # Handle different period formats
        if " to " in period:
            # Duration period (e.g., "2024-01-01 to 2025-01-01")
            parts = period.split(" to ")
            if len(parts) == 2:
                start_date = parts[0].split(" ")[0]  # Remove time component
                end_date = parts[1].split(" ")[0]    # Remove time component
                
                # Parse dates
                try:
                    from datetime import datetime
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                    
                    # Check if it's a full year (12 months)
                    if (start_dt.month == 1 and start_dt.day == 1 and 
                        end_dt.month == 1 and end_dt.day == 1 and
                        end_dt.year == start_dt.year + 1):
                        # Full fiscal year
                        return f"FY {start_dt.year}"
                    
                    # Check if it's a quarter
                    month_diff = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month)
                    if month_diff == 3:
                        # Quarter - determine which quarter
                        if start_dt.month == 1:
                            return f"Q1 {start_dt.year}"
                        elif start_dt.month == 4:
                            return f"Q2 {start_dt.year}"
                        elif start_dt.month == 7:
                            return f"Q3 {start_dt.year}"
                        elif start_dt.month == 10:
                            return f"Q4 {start_dt.year}"
                    
                    # For other durations, show the range
                    return f"{start_date} to {end_date}"
                    
                except ValueError:
                    # Fallback for invalid dates
                    return f"{start_date} to {end_date}"
        else:
            # Instant period (e.g., "2024-12-31")
            date_part = period.split(" ")[0]  # Remove time component
            return f"As of {date_part}"
        
        return period
    
    def _get_period_values_for_item(self, item: FinancialLineItem, sorted_periods: List[str]) -> List[str]:
        """Get values for each period for a specific item"""
        period_values = []
        
        for period in sorted_periods:
            value_str = ""
            
            # Check primary fact
            if item.period == period and item.value is not None:
                value_str = self._format_value_for_excel(item.value)
            else:
                # Check dimensional facts
                for dim_fact in item.all_dimensional_facts:
                    if dim_fact.get('period', '') == period:
                        value = dim_fact.get('value')
                        if value is not None:
                            value_str = self._format_value_for_excel(value)
                            break
            
            period_values.append(value_str)
        
        return period_values
    
    def _create_dimensional_rows(self, item: FinancialLineItem, sorted_periods: List[str]) -> List[List[str]]:
        """Create separate rows for dimensional breakdowns"""
        dimensional_rows = []
        
        # Group dimensional facts by dimension combination
        dimension_groups = {}
        for dim_fact in item.all_dimensional_facts:
            dimensions = dim_fact.get('dimensions', {})
            if not dimensions:
                continue
            
            # Create dimension key
            dim_key = tuple(sorted(f"{k}={v}" for k, v in dimensions.items()))
            
            if dim_key not in dimension_groups:
                dimension_groups[dim_key] = {}
            
            period = dim_fact.get('period', '')
            if period:
                dimension_groups[dim_key][period] = dim_fact.get('value')
        
        # Create rows for each dimension group
        for dim_key, period_data in dimension_groups.items():
            concept_display = "    └─ " + item.concept_name.split(":")[-1]
            label_display = "    └─ " + " | ".join(dim_key)
            
            row = [
                concept_display,
                label_display,
                item.unit_id or "",
                item.level + 1,
                "No"
            ]
            
            # Add values for each period
            for period in sorted_periods:
                value = period_data.get(period)
                value_str = self._format_value_for_excel(value) if value is not None else ""
                row.append(value_str)
            
            dimensional_rows.append(row)
        
        return dimensional_rows
    
    def _format_value_for_excel(self, value) -> str:
        """Format value for Excel display"""
        if value is None:
            return ""
        
        if isinstance(value, (int, float)):
            if abs(value) >= 1000000:
                return f"{value:,.0f}"
            else:
                return f"{value:,.2f}"
        else:
            return str(value)
    
    def _format_excel_sheet(self, worksheet):
        """Apply enhanced formatting to Excel worksheet with period columns"""
        
        if not EXCEL_AVAILABLE:
            return
        
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        
        # Define styles
        header_font = Font(bold=True, size=14, color="FFFFFF")
        subheader_font = Font(bold=True, size=11, color="FFFFFF")
        column_header_font = Font(bold=True, size=10, color="000000")
        value_font = Font(size=10)
        dimensional_font = Font(size=9, italic=True, color="666666")
        
        header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
        subheader_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        column_header_fill = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")
        value_fill = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")
        
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        thick_border = Border(
            left=Side(style='thick'),
            right=Side(style='thick'),
            top=Side(style='thick'),
            bottom=Side(style='thick')
        )
        
        # Auto-adjust column widths with improved logic
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            
            for cell in column:
                try:
                    if cell.value:
                        cell_length = len(str(cell.value))
                        if cell_length > max_length:
                            max_length = cell_length
                except:
                    pass
            
            # Set minimum and maximum widths
            if column_letter in ['A', 'B']:  # Concept and Label columns
                adjusted_width = min(max(max_length + 2, 25), 60)
            elif column_letter in ['C', 'D', 'E']:  # Unit, Level, Abstract
                adjusted_width = min(max(max_length + 2, 10), 15)
            else:  # Period value columns
                adjusted_width = min(max(max_length + 2, 12), 18)
            
            worksheet.column_dimensions[column_letter].width = adjusted_width
        
        # Apply formatting to different row types
        max_row = worksheet.max_row
        max_col = worksheet.max_column
        
        for row_num in range(1, max_row + 1):
            for col_num in range(1, max_col + 1):
                cell = worksheet.cell(row=row_num, column=col_num)
                
                # Entity name header (row 1)
                if row_num == 1:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    cell.border = thick_border
                
                # Statement title (row 2)
                elif row_num == 2:
                    cell.font = subheader_font
                    cell.fill = subheader_fill
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    cell.border = thick_border
                
                # Period description (row 3)
                elif row_num == 3:
                    cell.font = subheader_font
                    cell.fill = subheader_fill
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    cell.border = thick_border
                
                # Column headers (row 5)
                elif row_num == 5:
                    cell.font = column_header_font
                    cell.fill = column_header_fill
                    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                    cell.border = thin_border
                
                # Data rows
                elif row_num > 5:
                    # Check if this is a dimensional row (indented with └─)
                    if col_num == 1 and cell.value and "└─" in str(cell.value):
                        cell.font = dimensional_font
                    else:
                        cell.font = value_font
                    
                    # Apply alternating row colors for better readability
                    if row_num % 2 == 0:
                        cell.fill = value_fill
                    
                    # Align values appropriately
                    if col_num <= 2:  # Concept and Label columns
                        cell.alignment = Alignment(horizontal='left', vertical='center')
                    elif col_num > 5:  # Value columns
                        cell.alignment = Alignment(horizontal='right', vertical='center')
                        # Format numbers with thousands separator
                        if cell.value and isinstance(cell.value, str) and cell.value.replace(',', '').replace('.', '').replace('-', '').isdigit():
                            try:
                                num_value = float(cell.value.replace(',', ''))
                                cell.number_format = '#,##0'
                            except:
                                pass
                    else:  # Other columns
                        cell.alignment = Alignment(horizontal='center', vertical='center')
                    
                    cell.border = thin_border
        
        # Merge header cells for better appearance
        if max_col > 1:
            worksheet.merge_cells(f'A1:{chr(64 + max_col)}1')  # Entity name
            worksheet.merge_cells(f'A2:{chr(64 + max_col)}2')  # Statement title
            worksheet.merge_cells(f'A3:{chr(64 + max_col)}3')  # Period description
        
        # Freeze panes to keep headers visible
        worksheet.freeze_panes = 'A6'  # Freeze above row 6 (after headers)
        
        # Add auto-filter to column headers
        if max_row > 5:
            worksheet.auto_filter.ref = f'A5:{chr(64 + max_col)}{max_row}'
    
    def _format_consolidated_workbook(self, file_path: str):
        """Apply formatting to the entire consolidated workbook"""
        
        if not EXCEL_AVAILABLE:
            return
            
        try:
            from openpyxl import load_workbook
            
            wb = load_workbook(file_path)
            
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                self._format_excel_sheet(ws)
            
            wb.save(file_path)
        except Exception as e:
            logger.warning(f"Could not format consolidated workbook: {e}")
    
    def _convert_hierarchy_to_json(self, hierarchy: List[FinancialLineItem], filing_info: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Convert FinancialLineItem hierarchy to JSON-serializable format"""
        
        # Extract fiscal year and quarter from filing info
        fiscal_year = None
        quarter = None
        if filing_info:
            primary_period_info = filing_info.get('primary_period_info', {})
            fiscal_year = primary_period_info.get('fiscal_year')
            quarter = primary_period_info.get('quarter')
        
        def convert_item(item: FinancialLineItem) -> Dict[str, Any]:
            # Process dimensional facts to add fiscal year and quarter
            enhanced_dimensional_facts = []
            if item.all_dimensional_facts:
                for fact in item.all_dimensional_facts:
                    enhanced_fact = fact.copy() if isinstance(fact, dict) else fact
                    if isinstance(enhanced_fact, dict):
                        if fiscal_year is not None:
                            enhanced_fact["fiscal_year"] = fiscal_year
                        if quarter is not None:
                            enhanced_fact["quarter"] = quarter
                    enhanced_dimensional_facts.append(enhanced_fact)
            
            result = {
                "concept_name": item.concept_name,
                "label": item.label,
                "value": item.value,
                "context_id": item.context_id,
                "unit_id": item.unit_id,
                "period": item.period,
                "level": item.level,
                "order": item.order,
                "abstract": item.abstract,
                "parent_concept": item.parent_concept,
                "dimensions": [
                    {
                        "axis": dim.axis,
                        "member": dim.member,
                        "label": dim.label
                    } for dim in item.dimensions
                ],
                "dimensional_facts": enhanced_dimensional_facts,  # Use enhanced facts with fiscal year/quarter
                "calculations": item.calculations,
                "children": [convert_item(child) for child in item.children]
            }
            
            # Add fiscal year and quarter to each line item
            if fiscal_year is not None:
                result["fiscal_year"] = fiscal_year
            if quarter is not None:
                result["quarter"] = quarter
            
            return result
        
        return [convert_item(item) for item in hierarchy]
    
    def _calculate_statement_stats(self, hierarchy: List[FinancialLineItem]) -> Dict[str, Any]:
        """Calculate enhanced statistics for a financial statement"""
        
        def count_items(items: List[FinancialLineItem], stats: Dict[str, int]):
            for item in items:
                stats["total_items"] += 1
                
                if item.value is not None:
                    stats["items_with_values"] += 1
                    
                if item.abstract:
                    stats["abstract_items"] += 1
                else:
                    stats["concrete_items"] += 1
                
                if item.dimensions or item.all_dimensional_facts:
                    stats["items_with_dimensions"] += 1
                
                if item.all_dimensional_facts:
                    stats["dimensional_facts_count"] += len(item.all_dimensional_facts)
                    dimensional_values = [f for f in item.all_dimensional_facts if f.get('value') is not None]
                    stats["dimensional_values_count"] += len(dimensional_values)
                
                if item.calculations.get('is_summation_parent', False):
                    stats["summation_parents"] += 1
                    stats["summation_children_total"] += len(item.calculations.get('summation_children', []))
                
                if item.calculations.get('is_summation_child', False):
                    stats["summation_children"] += 1
                    stats["summation_parents_total"] += len(item.calculations.get('summation_parents', []))
                
                stats["max_level"] = max(stats["max_level"], item.level)
                
                count_items(item.children, stats)
        
        stats = {
            "total_items": 0,
            "items_with_values": 0,
            "abstract_items": 0,
            "concrete_items": 0,
            "items_with_dimensions": 0,
            "dimensional_facts_count": 0,
            "dimensional_values_count": 0,
            "summation_parents": 0,
            "summation_children": 0,
            "summation_children_total": 0,
            "summation_parents_total": 0,
            "max_level": 0
        }
        
        count_items(hierarchy, stats)
        
        result_stats: Dict[str, Any] = dict(stats)
        if stats["total_items"] > 0:
            result_stats["value_extraction_rate"] = round(
                (stats["items_with_values"] / stats["total_items"]) * 100, 1
            )
            result_stats["dimensional_coverage"] = round(
                (stats["items_with_dimensions"] / stats["total_items"]) * 100, 1
            )
        else:
            result_stats["value_extraction_rate"] = 0.0
            result_stats["dimensional_coverage"] = 0.0
        
        if stats["dimensional_facts_count"] > 0:
            result_stats["dimensional_value_rate"] = round(
                (stats["dimensional_values_count"] / stats["dimensional_facts_count"]) * 100, 1
            )
        else:
            result_stats["dimensional_value_rate"] = 0.0
        
        if stats["total_items"] > 0:
            result_stats["calculation_parent_rate"] = round(
                (stats["summation_parents"] / stats["total_items"]) * 100, 1
            )
            result_stats["calculation_child_rate"] = round(
                (stats["summation_children"] / stats["total_items"]) * 100, 1
            )
        else:
            result_stats["calculation_parent_rate"] = 0.0
            result_stats["calculation_child_rate"] = 0.0
        
        return result_stats

# Enhanced usage functions
def extract_and_export_enhanced(filing_url: Optional[str] = None, 
                                output_dir: str = "financial_statements_output",
                                taxonomy_preference: Optional[List[str]] = None):
    """Enhanced extraction function supporting multiple taxonomies"""
    
    # Default URLs for testing different taxonomies
    test_urls = {
        'tesla_us_gaap': "https://www.sec.gov/Archives/edgar/data/1318605/000162828025003063/tsla-20241231_htm.xml",
        'apple_us_gaap': "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928_htm.xml",
        'microsoft_us_gaap': "https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630_htm.xml",
        'sofi_us_gaap': "https://www.sec.gov/Archives/edgar/data/1818874/000181887425000016/sofi-20241231_htm.xml"
    }
    
    if filing_url is None:
        filing_url = test_urls['sofi_us_gaap']
    
    
    with FlexibleXBRLExtractor(taxonomy_preference=taxonomy_preference) as extractor:
        print("🚀 Enhanced Flexible XBRL Financial Statement Extractor")
        print("======================================================")
        print(f"Loading and processing: {filing_url}")
        
        try:
            # Extract financial statements
            financial_data = extractor.extract_financial_statements(filing_url)
            
            logger.debug(f"Processing completed in {financial_data['processing_time']:.2f} seconds")
            print(f"📊 Extracted {len(financial_data['statements'])} financial statements")
            print(f"🏷️  Taxonomies found: {', '.join(financial_data.get('discovered_taxonomies', []))}")
            
            # Export to JSON files
            print(f"\n📁 Exporting to JSON format in directory: {output_dir} (Primary Period Only)")
            json_files = extractor.export_to_json(financial_data, output_dir, primary_period_only=True)
            
            # Export to Excel files (if available)
            excel_files = {}
            if EXCEL_AVAILABLE:
                print(f"📊 Exporting to Excel format in directory: {output_dir} (Primary Period Only)")
                excel_files = extractor.export_to_excel(financial_data, output_dir, primary_period_only=True)
            else:
                print(f"⚠️  Excel export skipped - dependencies not installed")
            
            # Combine all exported files
            exported_files = {**json_files, **excel_files}
            
            print(f"\n✅ Successfully exported {len(exported_files)} files:")
            
            # Separate JSON and Excel files for display
            json_file_count = len(json_files)
            excel_file_count = len(excel_files)
            
            print(f"\n📄 JSON Files ({json_file_count}):")
            for file_type, file_path in json_files.items():
                file_size = os.path.getsize(file_path) / 1024
                print(f"  📄 {file_type}: {file_path} ({file_size:.1f} KB)")
            
            print(f"\n📊 Excel Files ({excel_file_count}):")
            for file_type, file_path in excel_files.items():
                file_size = os.path.getsize(file_path) / 1024
                print(f"  � {file_type}: {file_path} ({file_size:.1f} KB)")
            
            # Display enhanced summary
            print(f"\n📊 Enhanced Export Summary:")
            filing_info = financial_data.get('filing_info', {})
            entity_info = filing_info.get('entity_info', {})
            
            print(f"  🏢 Entity: {entity_info.get('entity_name', 'Unknown')}")
            print(f"  🆔 CIK: {entity_info.get('entity_cik', 'Unknown')}")
            logger.debug(f"  Period: {entity_info.get('document_period_end', 'Unknown')}")
            print(f"  📋 Document Type: {entity_info.get('document_type', 'Unknown')}")
            print(f"  💱 Currency: {entity_info.get('currency', 'Unknown')}")
            
            for statement_type, statement in financial_data['statements'].items():
                hierarchy = statement['hierarchy']
                stats = extractor._calculate_statement_stats(hierarchy)
                taxonomy_info = statement.get('taxonomy_info', {})
                primary_taxonomy = taxonomy_info.get('primary_taxonomy', 'unknown')
                
                print(f"  📈 {statement_type} ({primary_taxonomy}): {stats['total_items']} items, "
                      f"{stats['value_extraction_rate']}% with values, "
                      f"{stats['concrete_items']} concrete, "
                      f"{stats['dimensional_coverage']}% dimensional")
                
                if stats['dimensional_facts_count'] > 0:
                    print(f"     ↳ {stats['dimensional_facts_count']} dimensional facts, "
                          f"{stats['dimensional_value_rate']}% with values")
            
            return exported_files
            
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            traceback.print_exc()
            return None

if __name__ == "__main__":
    import sys
    
    # Test the enhanced extractor
    print("Testing Enhanced Flexible XBRL Extractor...")
    
    # Get URL from command line arguments or use default
    filing_url = None
    if len(sys.argv) > 1:
        filing_url = sys.argv[1]
        print(f"Using provided URL: {filing_url}")
    else:
        print("Using default Tesla filing")
    
    result = extract_and_export_enhanced(filing_url=filing_url)
    
    if result:
        print(f"\n🎉 Enhanced extraction completed successfully!")
        print(f"\n🔍 Key improvements:")
        print(f"  ✓ Support for multiple taxonomies (US-GAAP, IFRS, DEI, etc.)")
        print(f"  ✓ Flexible statement role identification")
        print(f"  ✓ Improved error handling and null checking")
        print(f"  ✓ Enhanced dimensional data extraction")
        print(f"  ✓ Automatic taxonomy discovery")
        print(f"  ✓ Better international standard support")
        print(f"  ✓ JSON and Excel export formats with rich formatting")
        print(f"  ✓ Consolidated Excel workbooks with multiple sheets")
        print(f"  ✓ Individual Excel files for each statement")
    else:
        print(f"❌ Enhanced extraction failed")
