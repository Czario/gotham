#!/usr/bin/env python3
"""Enhanced Financial statement processor with dimensions support using Arelle"""

import pandas as pd
import numpy as np
import time
import re
import requests
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from typing import Dict, List, Optional, Any, Union, Sequence
from bs4 import BeautifulSoup
from core.extractors.sec_data_extractors import extract_period_info_from_sec_api
from utilities.helpers.period_utils import (
    PeriodConfig, PeriodParser, PeriodClassifier, PeriodMatcher, FiscalYearCalculator
)
from core.extractors.dimensional_filters import filter_dimensional_data_by_period_length, get_target_duration_for_form
from core.extractors.dimensional_context_filters import apply_dimensional_context_filters
from core.extractors.xbrl_parser import FlexibleXBRLExtractor
from utilities.sec_url_detector import SECURLDetector

logger = logging.getLogger(__name__)

class EnhancedFinancialStatementProcessor:
    """Process financial statements from XBRL data with dimensions support using Arelle"""
    
    def __init__(self, current_period_only: bool = True, max_periods: int = 3, include_dimensions: bool = True, company_repo = None):
        self.current_period_only = current_period_only
        self.max_periods = max_periods
        self.include_dimensions = include_dimensions
        self.company_repo = company_repo
        
        # Initialize unified URL detector
        self.url_detector = SECURLDetector()
        
    def process_filing(self, filing_info: Dict, company_cik: str, company_info: Optional[Dict] = None, filing_url: Optional[str] = None) -> Optional[Dict]:
        """
        Process a single online SEC filing to extract financial statements with dimensions.

        Args:
            filing_info: Filing information from SEC API
            company_cik: Company CIK identifier
            company_info: Optional company information from SEC API (contains fiscal year-end)
            filing_url: Optional direct XBRL filing URL (discovered automatically when absent)

        Returns:
            Processed financial data with dimensions or None
        """
        try:
            # Reset period filtering state for new filing to avoid output spam
            if hasattr(self, '_period_filtering_state'):
                delattr(self, '_period_filtering_state')
            return self._process_online_filing(filing_info, company_cik, company_info, filing_url)
        except Exception as e:
            logger.error(f"Error processing filing {filing_info.get('accessionNumber', 'unknown')}: {e}")
            return None
    
    def _process_online_filing(self, filing_info: Dict, company_cik: str, company_info: Optional[Dict] = None, filing_url: Optional[str] = None) -> Optional[Dict]:
        """Process online SEC filing"""
        accession_number = filing_info.get('accessionNumber', 'unknown')
        
        # Use provided URL or try to construct it
        if filing_url:
            url_to_use = filing_url
        else:
            # Discover the correct XBRL file URL from the filing directory
            try:
                url_to_use = self._discover_xbrl_url(filing_info, company_cik)
                if not url_to_use:
                    logger.error(f"❌ XBRL URL Discovery Failed for filing {accession_number}: Could not find XBRL file in SEC directory")
                    return None
            except Exception as e:
                logger.error(f"❌ XBRL URL Discovery Error for filing {accession_number}: {str(e)}")
                return None
        
        logger.debug(f"Processing filing with Arelle: {url_to_use}")
        
        # Extract filing form type for period filtering
        filing_form_type = filing_info.get('form') or 'unknown'
        
        # CRITICAL FIX: Calculate fiscal year and quarter from SEC API BEFORE parsing
        # This ensures the XBRL parser can filter facts by fiscal year during primary fact selection
        enhanced_company_info = (company_info or {}).copy()
        if not enhanced_company_info.get('fiscal_year'):
            report_date = filing_info.get('reportDate')
            fiscal_year_end_code = enhanced_company_info.get('fiscalYearEnd')
            
            # If fiscal year end not in company_info, retrieve from database
            if not fiscal_year_end_code and hasattr(self, 'company_repo') and self.company_repo:
                db_company = self.company_repo.get_company(company_cik)
                if db_company:
                    fiscal_year_end_code = db_company.get('corporate_info', {}).get('fiscal_year_end')
                    if fiscal_year_end_code:
                        logger.debug(f"Retrieved fiscal year end {fiscal_year_end_code} from database for CIK {company_cik}")
            
            if report_date and fiscal_year_end_code:
                try:
                    end_date = datetime.strptime(report_date, '%Y-%m-%d')
                    fiscal_year, quarter = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                        end_date, fiscal_year_end_code
                    )
                    enhanced_company_info['fiscal_year'] = fiscal_year
                    enhanced_company_info['fiscal_quarter'] = quarter
                    enhanced_company_info['fiscal_year_end_code'] = fiscal_year_end_code
                    logger.debug(f"Pre-calculated fiscal year {fiscal_year} Q{quarter} for fact selection")
                except Exception as e:
                    logger.warning(f"Could not pre-calculate fiscal year: {e}")
        
        # Use FlexibleXBRLExtractor to extract financial statements
        try:
            with FlexibleXBRLExtractor(enable_enhanced_dimensions=self.include_dimensions, company_info=enhanced_company_info, filing_form_type=filing_form_type) as extractor:
                financial_data = extractor.extract_financial_statements(url_to_use)
                
                if not financial_data:
                    logger.error(f"❌ XBRL Extraction Failed for filing {accession_number}: FlexibleXBRLExtractor returned None")
                    return None
                
                if not financial_data.get('statements'):
                    if financial_data.get('no_xbrl_available'):
                        logger.warning(
                            f"⏭️  No XBRL for filing {accession_number}: this filing predates "
                            f"the XBRL era or has no XBRL exhibits (and no companion amendment "
                            f"supplies them). Skipping."
                        )
                        return None
                    logger.error(f"❌ No Statements Found in XBRL for filing {accession_number}: XBRL parsed but no financial statements identified")
                    return None
                
                reporting_period = extract_period_info_from_sec_api(filing_info, company_info or {})
                
                return self._convert_arelle_data_to_result(financial_data, filing_info, company_cik, reporting_period)
        except Exception as e:
            logger.error(f"❌ XBRL Extraction Exception for filing {accession_number}: {str(e)}")
            return None
    
    def _convert_arelle_data_to_result(self, financial_data: Dict, filing_info: Dict, company_cik: str, reporting_period: Dict) -> Dict:
        """Convert Arelle data format to expected result format"""
        # Convert Arelle data format to expected format
        statements = {}
        arelle_statements = financial_data.get('statements', {})
        
        # Log what we received
        logger.info(f"Converting Arelle data - received {len(arelle_statements)} statements: {list(arelle_statements.keys())}")
        
        # Extract filing form type for dimensional filtering
        filing_form_type = filing_info.get('form') or reporting_period.get('form_type')
        logger.debug(f"Processing {filing_form_type or 'unknown'} filing with dimensional filtering")
        
        # Map Arelle statement types to the three core stored types.
        # equity_changes is intentionally excluded — its roll-forward activity items
        # (dividends, buybacks, APIC changes) are NOT balance sheet line items; the
        # ending equity balances already appear in the balance_sheet role itself.
        # comprehensive_income is intentionally excluded — its key metrics
        # (NetIncomeLoss, ComprehensiveIncomeNetOfTax) are already captured from
        # the income_statement, so storing it would only duplicate concepts.
        statement_mapping = {
            'income_statement': 'income_statement',
            'balance_sheet': 'balance_sheet',
            'cash_flow': 'cash_flows',
        }
        
        for arelle_type, expected_type in statement_mapping.items():
            logger.debug(f"Checking for {arelle_type} in arelle_statements...")
            if arelle_type in arelle_statements:
                logger.info(f"Found {arelle_type}, converting to {expected_type}...")
                converted_data = self._convert_arelle_hierarchy_to_flat_list(
                    arelle_statements[arelle_type]['hierarchy'],
                    expected_type,
                    filing_form_type,
                    financial_data.get('filing_info', {})
                )
                if converted_data:
                    if expected_type in statements:
                        # Multiple source types map to the same target — merge, deduplicating by concept name
                        existing_concepts = {it.get('concept') for it in statements[expected_type]}
                        new_items = [it for it in converted_data if it.get('concept') not in existing_concepts]
                        statements[expected_type].extend(new_items)
                        logger.info(f"✅ Merged {arelle_type} -> {expected_type}: +{len(new_items)} items (deduped)")
                    else:
                        statements[expected_type] = converted_data
                        logger.info(f"✅ Converted {arelle_type} -> {expected_type}: {len(converted_data)} items")
                else:
                    logger.warning(f"⚠️  {arelle_type} -> {expected_type}: conversion resulted in empty data")
            else:
                logger.debug(f"  {arelle_type} not found in arelle_statements")
        
        logger.info(f"Final converted statements: {list(statements.keys())}")

        result = {
            'filing_info': filing_info,
            'company_cik': str(company_cik),
            'statements': statements,
            'reporting_period': reporting_period,
            'processed_at': datetime.now(),
            'arelle_data': financial_data  # Include original Arelle data for reference
        }
        
        return result
    
    def _convert_arelle_hierarchy_to_flat_list(self, hierarchy: List, statement_type: str, filing_form_type: Optional[str] = None, filing_info: Optional[Dict] = None) -> List[Dict]:
        """
        Convert Arelle hierarchical data to flat list format expected by the rest of the system
        
        Args:
            hierarchy: Arelle hierarchy data (list of FinancialLineItem objects)
            statement_type: Type of statement for context
            filing_form_type: Filing form type (e.g., '10-K', '10-Q') for period filtering
            filing_info: Filing information containing fiscal year and quarter data
            
        Returns:
            List of dictionaries in expected format
        """
        flat_data = []
        
        # Extract fiscal year and quarter from filing info
        fiscal_year = None
        quarter = None
        if filing_info and 'primary_period_info' in filing_info:
            primary_period_info = filing_info['primary_period_info']
            fiscal_year = primary_period_info.get('fiscal_year')
            quarter = primary_period_info.get('quarter')
        
        def process_item(item, level=0):
            # Convert FinancialLineItem to dictionary format
            fact_dict = {
                'concept': item.concept_name,
                'label': item.label,
                'value': item.value,
                'context_id': item.context_id,
                'unit': item.unit_id,
                'period': item.period,
                'level': item.level if hasattr(item, 'level') else level,
                'order': item.order if hasattr(item, 'order') else 0,
                'abstract': item.abstract if hasattr(item, 'abstract') else False,
                'statement_type': statement_type
            }
            
            # Add calculation relationships if available (CRITICAL for accurate sign conventions)
            if hasattr(item, 'calculations') and item.calculations:
                fact_dict['calculations'] = item.calculations
            
            # Add dimensional information if available
            if hasattr(item, 'dimensions') and item.dimensions:
                fact_dict['dimensions'] = []
                for dim in item.dimensions:
                    fact_dict['dimensions'].append({
                        'axis': dim.axis,
                        'member': dim.member,
                        'label': dim.label
                    })
            
            # Add all dimensional facts if available and dimensions are enabled
            if (hasattr(item, 'all_dimensional_facts') and 
                item.all_dimensional_facts and 
                self.include_dimensions):
                # Get fiscal year end code from filing info
                fiscal_year_end_code = None
                if filing_info and 'primary_period_info' in filing_info:
                    fiscal_year_end_code = filing_info['primary_period_info'].get('fiscal_year_end_code')
                
                # Filter dimensional facts to current period only if requested
                if self.current_period_only:
                    filtered_facts = self._filter_current_period_facts(
                        item.all_dimensional_facts, 
                        filing_form_type,
                        fiscal_year=fiscal_year,
                        quarter=quarter,
                        fiscal_year_end_code=fiscal_year_end_code
                    )
                    # Only warn if many facts were filtered (could indicate a configuration issue)
                    if len(item.all_dimensional_facts) >= 5 and len(filtered_facts) == 0:
                        logger.warning(f"⚠️  ALL dimensional facts filtered out for {item.label}: {len(item.all_dimensional_facts)} → 0 (filing: {filing_form_type})")
                else:
                    filtered_facts = item.all_dimensional_facts
                
                # Add fiscal year and quarter to each dimensional fact
                enhanced_dimensional_facts = []
                logger.debug(f"Processing {len(filtered_facts)} dimensional facts for {item.label}")
                for i, dim_fact in enumerate(filtered_facts):
                    if i == 0:  # Log first fact to see structure
                        logger.debug(f"First dimensional fact structure: {list(dim_fact.keys()) if isinstance(dim_fact, dict) else type(dim_fact)}")
                        if isinstance(dim_fact, dict) and 'dimension_details' in dim_fact:
                            logger.debug(f"dimension_details present: {dim_fact['dimension_details']}")
                        else:
                            logger.debug(f"dimension_details missing or not dict")
                    enhanced_fact = dict(dim_fact)  # Create a copy
                    enhanced_dimensional_facts.append(enhanced_fact)
                
                fact_dict['dimensional_facts'] = enhanced_dimensional_facts
            elif not self.include_dimensions:
                # Explicitly set to empty list when dimensions are disabled
                fact_dict['dimensional_facts'] = []
            
            # Add the main fact with all dimensional facts attached (single record approach)
            # Only include actual line items: concepts with a real numeric value OR
            # concepts whose only data exists as dimensional breakdowns (no consolidated total).
            # Abstract header/grouping concepts with no value and no dimensional data are
            # structural noise and must not be stored.
            has_dimensional_data = bool(fact_dict.get('dimensional_facts'))
            should_include = (
                fact_dict['value'] is not None
                or has_dimensional_data
            )
            
            # Always exclude non-financial taxonomy concepts even if they have a value
            if should_include and self._should_skip_irrelevant_concept(
                fact_dict['concept'], fact_dict['abstract'], has_dimensional_data
            ):
                should_include = False
                
            if should_include:
                flat_data.append(fact_dict)
            
            # Process children recursively
            if hasattr(item, 'children') and item.children:
                for child in item.children:
                    process_item(child, level + 1)
        
        # Process all items in hierarchy
        for item in hierarchy:
            process_item(item)
        
        # Apply dimensional context filters to remove unwanted contexts (forecast, scenario, estimates)
        if flat_data and self.include_dimensions:
            # Count dimensional facts before filtering
            total_before = sum(len(item.get('dimensional_facts', [])) for item in flat_data)
            flat_data = apply_dimensional_context_filters(flat_data, fiscal_year)
            total_after = sum(len(item.get('dimensional_facts', [])) for item in flat_data)
            if total_before != total_after:
                logger.debug(f"Context filters: {total_before} dimensional facts → {total_after} kept ({total_before - total_after} removed)")
        
        return flat_data

    def _filter_current_period_facts(self, dimensional_facts: List[Dict], filing_form_type: Optional[str] = None, 
                                    fiscal_year: Optional[int] = None, quarter: Optional[int] = None, 
                                    fiscal_year_end_code: Optional[str] = None) -> List[Dict]:
        """
        Enhanced filtering for dimensional facts that prioritizes appropriate periods based on filing type
        and excludes facts with empty dimensional data
        
        Args:
            dimensional_facts: List of dimensional fact dictionaries
            filing_form_type: Filing form type (e.g., '10-Q', '10-K') to determine target period
            
        Returns:
            List of dimensional facts with proper period filtering and meaningful dimensions only
        """
        if not dimensional_facts:
            return dimensional_facts

        # First filter: Remove facts with empty or meaningless dimensional data
        meaningful_dimensional_facts = []
        for fact in dimensional_facts:
            # Get dimensions and dimension_details
            dimensions = fact.get('dimensions', {})
            dimension_details = fact.get('dimension_details', {})
            
            # Check if this fact has meaningful dimensional data
            has_meaningful_dimensions = False
            
            # Check if dimensions contains meaningful data (not empty)
            if isinstance(dimensions, dict) and dimensions:
                has_meaningful_dimensions = True
            
            # Check if dimension_details contains meaningful data (not empty)
            if isinstance(dimension_details, dict) and dimension_details:
                has_meaningful_dimensions = True
            
            # Only include facts with meaningful dimensional information
            if has_meaningful_dimensions:
                meaningful_dimensional_facts.append(fact)
        
        # If no facts have meaningful dimensions, return empty list
        if not meaningful_dimensional_facts:
            return []

        # For 10-K filings, use RELAXED filtering - accept all annual periods without strict matching
        # This avoids the issue where strict end-date matching rejects valid 12-month periods
        if filing_form_type == '10-K':
            # For annual filings, simple duration-based filtering is sufficient
            # Accept periods between 11-13 months
            filtered_facts = []
            
            for fact in meaningful_dimensional_facts:
                period_str = fact.get('period', '')
                if not period_str or ' to ' not in period_str:
                    # Include instant periods or facts without period info
                    filtered_facts.append(fact)
                    continue
                
                try:
                    start_date = PeriodParser.extract_start_date_from_period_string(period_str)
                    end_date = PeriodParser.extract_end_date_from_period_string(period_str)
                    
                    if start_date and end_date:
                        duration_months = PeriodParser.calculate_duration_months(start_date, end_date)
                        # Accept annual periods (11-13 months) OR quarterly periods (2.5-4 months)
                        # 10-K filings often include quarterly dimensional breakdowns
                        if (11.0 <= duration_months <= 13.0) or (2.5 <= duration_months <= 4.0):
                            filtered_facts.append(fact)
                    else:
                        # Conservative inclusion if we can't parse
                        filtered_facts.append(fact)
                except Exception:
                    # Conservative inclusion on error
                    filtered_facts.append(fact)
            
            return filtered_facts

        # Initialize period filtering state tracking to avoid output spam
        if not hasattr(self, '_period_filtering_state'):
            self._period_filtering_state = {}
            
        # Check if this is the first time filtering for this filing type
        state_key = filing_form_type or 'unknown'
        is_first_time = state_key not in self._period_filtering_state
        
        if is_first_time:
            self._period_filtering_state[state_key] = True
        
        # Get target duration and tolerance using centralized utilities
        target_duration_months = PeriodMatcher.get_target_duration_for_form(filing_form_type)
        duration_tolerance = PeriodMatcher.get_duration_tolerance_for_form(filing_form_type)
        
        # Only show initial message on first run for this filing type
        if is_first_time:
            logger.debug(f"Filtering dimensional facts for {filing_form_type or 'unknown'} filing (target: {target_duration_months} months)")
            logger.debug(f"Filtered {len(dimensional_facts)} → {len(meaningful_dimensional_facts)} facts (removed empty dimensions)")
        
        # For unknown filing types, be conservative and include more data
        if not filing_form_type:
            return meaningful_dimensional_facts
            
        # Continue with period filtering using meaningful facts
        dimensional_facts = meaningful_dimensional_facts

        # Group facts by period type
        instant_facts = []
        duration_facts = []
        
        for fact in dimensional_facts:
            period_str = fact.get('period', '')
            if not period_str:
                continue
            
            if ' to ' in period_str:
                duration_facts.append(fact)
            else:
                instant_facts.append(fact)
        
        # Filter duration facts by target duration
        filtered_duration_facts = self._filter_duration_facts_by_target(
            duration_facts, target_duration_months, duration_tolerance, filing_form_type, 
            fiscal_year=fiscal_year, quarter=quarter, fiscal_year_end_code=fiscal_year_end_code,
            verbose=is_first_time
        )
        
        # Find latest instant period
        latest_instant_facts = self._filter_to_latest_instant_period(instant_facts)
        
        # Combine and deduplicate
        final_facts = self._combine_and_deduplicate_facts(filtered_duration_facts + latest_instant_facts)
        
        # Only show progress message on first run for this filing type
        if is_first_time:
            logger.debug(f"Period-aware filtering: {len(dimensional_facts)} → {len(final_facts)} facts "
                  f"(prioritized {target_duration_months}-month periods)")
        
        # Apply dimensional context filters to remove unwanted contexts (forecast, scenario, estimates)
        from core.extractors.dimensional_context_filters import DimensionalContextFilter
        final_facts = DimensionalContextFilter.filter_dimensional_facts(final_facts)
        
        return final_facts

    def _filter_duration_facts_by_target(self, duration_facts: List[Dict], target_duration_months: int, 
                                       duration_tolerance: float, filing_form_type: str, 
                                       fiscal_year: Optional[int] = None, quarter: Optional[int] = None,
                                       fiscal_year_end_code: Optional[str] = None, verbose: bool = True) -> List[Dict]:
        """
        Filter duration facts to match target duration using FISCAL YEAR logic, not calendar dates.
        
        For quarterly filings (10-Q), this identifies which periods belong to the specific fiscal quarter
        by calculating the expected quarter start/end dates based on the company's fiscal year end.
        """
        if not duration_facts:
            return []
        
        filtered_facts = []
        period_analysis = {}
        
        # CRITICAL: For 10-Q filings with fiscal info, use fiscal-aware matching
        use_fiscal_matching = (filing_form_type == '10-Q' and 
                              fiscal_year is not None and 
                              quarter is not None and 
                              fiscal_year_end_code is not None)
        
        # DRY: Use centralized fiscal quarter boundary calculation
        q_start = None
        q_end = None
        if use_fiscal_matching:
            # Type guards: we know these are not None here due to the check above
            assert fiscal_year is not None
            assert quarter is not None
            assert fiscal_year_end_code is not None
            
            boundaries = FiscalYearCalculator.calculate_fiscal_quarter_boundaries(
                fiscal_year, quarter, fiscal_year_end_code
            )
            if boundaries:
                q_start, q_end = boundaries
                if verbose:
                    logger.debug(f"🎯 FISCAL-AWARE MODE: Filtering for FY{fiscal_year} Q{quarter} (FYE: {fiscal_year_end_code})")
                    logger.debug(f"📅 Expected Q{quarter} period: {q_start.strftime('%Y-%m-%d')} to {q_end.strftime('%Y-%m-%d')}")
            else:
                if verbose:
                    logger.warning(f"⚠️  Failed to calculate fiscal quarter boundaries")
                use_fiscal_matching = False
        
        # Group facts by end date and analyze durations
        for fact in duration_facts:
            period_str = fact.get('period', '')
            try:
                start_date = PeriodParser.extract_start_date_from_period_string(period_str)
                end_date = PeriodParser.extract_end_date_from_period_string(period_str)
                
                if not start_date or not end_date:
                    # CRITICAL FIX: For quarterly filings, don't include facts with unparseable periods
                    # Only be conservative for annual filings where close matches are acceptable
                    if filing_form_type != '10-Q':
                        filtered_facts.append(fact)  # Conservative inclusion only for non-quarterly
                    continue
                
                duration_months = PeriodParser.calculate_duration_months(start_date, end_date)
                
                # FISCAL-AWARE FILTERING: For 10-Q with fiscal info, check if this period matches the target quarter
                if use_fiscal_matching:
                    # Type guards: these are guaranteed to be non-None due to use_fiscal_matching condition
                    assert fiscal_year is not None
                    assert quarter is not None
                    assert fiscal_year_end_code is not None
                    assert q_start is not None
                    assert q_end is not None
                    
                    # CRITICAL: First check if this period belongs to the correct fiscal year
                    # This prevents matching comparative/prior year periods (e.g., 2012 Q2 when we want 2013 Q2)
                    period_fiscal_year = FiscalYearCalculator.determine_fiscal_year_from_date(end_date, fiscal_year_end_code)
                    
                    if period_fiscal_year != fiscal_year:
                        if verbose:
                            logger.debug(f"❌ Rejected: {period_str} is FY{period_fiscal_year}, not target FY{fiscal_year}")
                        continue
                    
                    # Check if this period's end date aligns with the expected quarter end
                    expected_q_end = q_end
                    days_diff = abs((end_date - expected_q_end).days)
                    
                    # Must end within tolerance of the expected quarter end
                    if days_diff > PeriodConfig.DATE_TOLERANCE_DAYS:
                        if verbose:
                            logger.debug(f"❌ Rejected: {period_str} (ends {days_diff} days from Q{quarter} end)")
                        continue
                    
                    # Must be approximately quarterly duration (not cumulative)
                    if PeriodMatcher.reject_non_quarterly_period(duration_months, filing_form_type):
                        if verbose:
                            logger.debug(f"❌ Rejected: {duration_months:.1f} months (not quarterly)")
                        continue
                    
                    # Check if start date aligns with expected quarter start
                    expected_q_start = q_start
                    start_diff = abs((start_date - expected_q_start).days)
                    
                    if start_diff <= 7:  # Within 1 week of expected quarter start
                        if verbose:
                            logger.debug(f"✅ FISCAL MATCH: {period_str} matches FY{fiscal_year} Q{quarter} (duration: {duration_months:.2f}m)")
                        filtered_facts.append(fact)
                    else:
                        if verbose:
                            logger.debug(f"❌ Rejected: Start date {start_diff} days from Q{quarter} start")
                    continue
                
                # FALLBACK: Duration-based filtering for non-fiscal matching (10-K, unknown types, etc.)
                # CRITICAL FIX: For quarterly filings, reject cumulative periods immediately
                # This prevents 6-month or 9-month periods from even entering the analysis
                if filing_form_type == '10-Q' and PeriodMatcher.reject_non_quarterly_period(duration_months, filing_form_type):
                    if verbose:
                        logger.debug(f"PRE-FILTER REJECTION: {duration_months:.1f} months period rejected before analysis")
                    continue  # Skip this fact entirely - it's not a valid quarterly period
                
                end_date_key = end_date.strftime('%Y-%m-%d')
                
                if end_date_key not in period_analysis:
                    period_analysis[end_date_key] = []
                
                period_analysis[end_date_key].append({
                    'fact': fact,
                    'start_date': start_date,
                    'end_date': end_date,
                    'duration_months': duration_months,
                    'period_str': period_str
                })
                
            except Exception:
                # CRITICAL FIX: For quarterly filings, don't include facts with parsing errors
                # Only be conservative for annual filings where close matches are acceptable
                if filing_form_type != '10-Q':
                    filtered_facts.append(fact)  # Conservative inclusion only for non-quarterly
                continue
        
        # For each end date, prefer periods matching the target duration - STRICT MODE
        for end_date_key, period_facts in period_analysis.items():
            period_facts.sort(key=lambda x: abs(x['duration_months'] - target_duration_months))
            
            # STRICT QUARTERLY ENFORCEMENT: Only apply to 10-Q filings
            # For 10-K and other filing types, use all periods without quarterly filtering
            if filing_form_type == '10-Q':
                strictly_quarterly_facts = []
                for pf in period_facts:
                    # Use strict validation for quarterly periods
                    if PeriodMatcher.reject_non_quarterly_period(pf['duration_months'], filing_form_type):
                        if verbose:
                            logger.debug(f"STRICT REJECTION: {pf['duration_months']:.1f} months period for {filing_form_type} filing (end: {end_date_key})")
                        continue
                    strictly_quarterly_facts.append(pf)
                
                # If no periods pass strict validation, reject all for this end date
                if not strictly_quarterly_facts:
                    if verbose:
                        all_periods = [f"{pf['duration_months']:.1f}m" for pf in period_facts]
                        logger.debug(f"STRICT MODE: NO VALID QUARTERLY PERIODS for {end_date_key}")
                        logger.debug(f"   All periods rejected: {all_periods}")
                        logger.debug(f"   Required: Exactly 3.0 months (±{PeriodConfig.QUARTERLY_STRICT_TOLERANCE:.2f})")
                    continue  # Skip this entire end date - no valid periods
                
                valid_periods = strictly_quarterly_facts
            else:
                # For 10-K and other types, use all periods without quarterly filtering
                valid_periods = period_facts
            
            # Find matching periods within strict tolerance
            matching_facts = []
            for pf in valid_periods:
                if PeriodMatcher.is_period_match(
                    pf['start_date'], pf['end_date'], pf['end_date'],
                    pf['duration_months'], target_duration_months, filing_form_type
                ):
                    matching_facts.append(pf)
            
            if matching_facts:
                if verbose:
                    period_type = "quarterly" if target_duration_months == 3 else "annual"
                    logger.debug(f"STRICT VALIDATION: Found {period_type} period ({matching_facts[0]['duration_months']:.3f} months) for {end_date_key}")
                filtered_facts.extend([pf['fact'] for pf in matching_facts])
            else:
                # CRITICAL FIX: For 10-Q quarterly filings, NEVER use fallback that could pick cumulative periods
                # Only use fallback for annual filings (10-K) where close matches are acceptable
                if filing_form_type == '10-K' and valid_periods:
                    closest_period = valid_periods[0]
                    if verbose:
                        period_type = "annual"
                        logger.debug(f"No strict {period_type} period found for {end_date_key}, using {closest_period['duration_months']:.1f} months")
                    filtered_facts.append(closest_period['fact'])
                else:
                    # For 10-Q or unknown types: reject all if no exact match found
                    # This prevents picking 6-month or 9-month cumulative periods for quarterly reports
                    if verbose:
                        logger.debug(f"STRICT MODE: Completely rejected all periods for {end_date_key} (filing type: {filing_form_type or 'unknown'})")
        
        return filtered_facts

    def _filter_to_latest_instant_period(self, instant_facts: List[Dict]) -> List[Dict]:
        """Filter instant facts to the latest period only"""
        if not instant_facts:
            return []
        
        latest_instant_date = None
        latest_instant_period = None
        
        # Find the latest instant period
        for fact in instant_facts:
            period_str = fact.get('period', '')
            try:
                instant_date = PeriodParser.extract_end_date_from_period_string(period_str)
                if instant_date and (latest_instant_date is None or instant_date > latest_instant_date):
                    latest_instant_date = instant_date
                    latest_instant_period = period_str
            except:
                continue
        
        # Return facts from the latest instant period
        if latest_instant_period:
            return [f for f in instant_facts if f.get('period') == latest_instant_period]
        
        return instant_facts  # Return all if we can't determine latest

    def _combine_and_deduplicate_facts(self, facts: List[Dict]) -> List[Dict]:
        """Combine facts and remove duplicates while preserving unique dimensional combinations"""
        if not facts:
            return []
        
        seen_combinations = set()
        final_facts = []
        
        for fact in facts:
            # Create a key for the dimensional combination
            dimensions = fact.get('dimensions', {})
            dim_key = tuple(sorted(dimensions.items())) if dimensions else ()
            combination_key = (fact.get('concept_name', ''), dim_key, fact.get('period', ''))
            
            if combination_key not in seen_combinations:
                final_facts.append(fact)
                seen_combinations.add(combination_key)
        
        # Sort by period and dimension count
        final_facts.sort(key=lambda x: (
            x.get('period', ''),
            x.get('dimension_count', 0) if 'dimension_count' in x else len(x.get('dimensions', {}))
        ), reverse=True)
        
        return final_facts

    def _normalize_dimension_member(self, member: str) -> str:
        """
        Normalize dimension member names for consistency
        
        Args:
            member: Raw dimension member name
            
        Returns:
            Normalized member name
        """
        if not member:
            return member
        
        # Convert to title case and clean up
        normalized = member.replace('_', ' ').replace('-', ' ')
        normalized = ' '.join(word.capitalize() for word in normalized.split())
        
        # Handle common abbreviations
        replacements = {
            'Usa': 'USA',
            'Usd': 'USD',
            'Us': 'US',
            'Nyse': 'NYSE',
            'Llc': 'LLC',
            'Inc': 'Inc.',
            'Corp': 'Corp.',
            'Ltd': 'Ltd.'
        }
        
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)
        
        return normalized

    def _discover_xbrl_url(self, filing_info: Dict, company_cik: str) -> Optional[str]:
        """
        Discover the correct XBRL file URL using unified URL detector
        
        Args:
            filing_info: Filing information from SEC API
            company_cik: Company CIK identifier
            
        Returns:
            The optimal XBRL file URL
        """
        try:
            accession_number = filing_info['accessionNumber']
            filing_date = filing_info.get('filingDate', filing_info.get('reportDate', '2010-01-01'))
            
            # Use unified URL detector
            logger.debug(f"Detecting XBRL URL for {accession_number} (filing date: {filing_date})")
            urls = self.url_detector.detect_filing_urls(company_cik, accession_number, filing_date)
            
            xbrl_url = urls.get('xbrl_url')
            if xbrl_url and isinstance(xbrl_url, str):
                if urls.get('is_legacy'):
                    logger.info(f"Using legacy filing URL: {xbrl_url}")
                else:
                    logger.debug(f"Using modern filing URL: {xbrl_url}")
                return xbrl_url
            else:
                logger.warning(f"Could not detect XBRL URL for {accession_number}")
                return None
            
        except Exception as e:
            logger.error(f"Error discovering XBRL URL: {e}")
            return None

    def _resolve_optimal_xbrl_url(self, filing_url: str, cik: str, accession_number: str) -> str:
        """
        Intelligently resolve the optimal XBRL URL by discovering files in the SEC directory.
        This method explores the SEC filing directory structure to find the best instance files.
        
        SEC filings directory structure:
        https://www.sec.gov/Archives/edgar/data/{CIK}/{accession_no_dashes}/
        
        Priority order:
        1. _htm.xml (pure XBRL instance) - best for parsing
        2. .xml files (XBRL instance documents)
        3. .txt file (inline XBRL) - universal availability
        4. Directory URL for Arelle to handle
        """
        import requests
        import re
        
        logger.debug(f"Discovering optimal XBRL files in SEC directory for: {filing_url}")
        
        # SEC requires proper user-agent headers
        headers = {
            'User-Agent': 'Financial Research Tool 1.0 (contact@example.com)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        # If URL already points to a specific file, try to get the directory
        if filing_url.endswith(('.xml', '.txt', '.htm')):
            base_url = filing_url.rsplit('/', 1)[0] + '/'
        else:
            base_url = filing_url if filing_url.endswith('/') else filing_url + '/'
        
        # Construct directory URL using CIK and accession number
        try:
            # Remove leading zeros from CIK for URL construction
            unpadded_cik = cik.lstrip('0')
            clean_accession = accession_number.replace('-', '')
            
            # Construct the SEC directory URL
            directory_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{clean_accession}/"
            logger.debug(f"Exploring SEC directory: {directory_url}")
            
            # Try to fetch the directory listing
            try:
                response = requests.get(directory_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    return self._discover_files_from_directory(directory_url, response.text, accession_number, headers)
                else:
                    logger.debug(f"Could not access directory (status: {response.status_code}). Falling back to file-based approach.")
            except Exception as e:
                logger.debug(f"Directory access failed: {e}. Falling back to file-based approach.")
            
            # Fallback: Try direct file patterns if directory listing fails
            return self._try_direct_file_patterns(directory_url, accession_number, cik, headers)
            
        except Exception as e:
            logger.debug(f"Error in URL resolution: {e}")
            # Final fallback: return original URL or construct .txt URL
            if filing_url.endswith(('.xml', '.txt')):
                return filing_url
            else:
                # Construct .txt URL as final fallback
                unpadded_cik = cik.lstrip('0')
                clean_accession = accession_number.replace('-', '')
                txt_url = f"https://www.sec.gov/Archives/edgar/data/{unpadded_cik}/{clean_accession}/{accession_number}.txt"
                logger.debug(f"Using constructed .txt URL as fallback: {txt_url}")
                return txt_url

    def _discover_files_from_directory(self, directory_url: str, html_content: str, accession_number: str, headers: Dict) -> str:
        """
        Discover and prioritize XBRL files from SEC directory HTML listing.
        
        Args:
            directory_url: Base directory URL
            html_content: HTML content of directory listing
            accession_number: Filing accession number for context
            headers: Request headers
            
        Returns:
            Best available XBRL file URL
        """
        try:
            import re
            
            # Find all file links in the directory using regex parsing
            file_links = []
            # Look for href attributes in anchor tags
            href_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
            matches = re.findall(href_pattern, html_content, re.IGNORECASE)
            
            for href in matches:
                if href and not href.startswith('../') and not href == '/':
                    # Clean up the href to get just the filename
                    if href.startswith('./'):
                        href = href[2:]
                    elif href.startswith('/'):
                        # Extract just the filename from full path
                        href = href.split('/')[-1]
                    if href:  # Only add non-empty hrefs
                        file_links.append(href)
            
            if not file_links:
                logger.debug("No files found in directory listing")
                return self._construct_fallback_url(directory_url, accession_number)
            
            logger.debug(f"Found {len(file_links)} files in SEC directory")
            
            # Categorize and score files
            instance_files = []
            xml_files = []
            txt_files = []
            
            for filename in file_links:
                filename_lower = filename.lower()
                
                # Skip non-XBRL related files
                if any(skip in filename_lower for skip in ['.css', '.js', '.jpg', '.png', '.gif', '.pdf']):
                    continue
                
                # Score files based on likelihood of being instance documents
                score = self._score_sec_file(filename)
                
                if score > 0:
                    # Ensure we use proper URL construction
                    if filename.startswith('http'):
                        full_url = filename
                    else:
                        full_url = directory_url + filename
                    
                    file_info = {
                        'filename': filename,
                        'url': full_url,
                        'score': score
                    }
                    
                    if filename_lower.endswith('_htm.xml'):
                        # These are typically the best instance files
                        instance_files.append(file_info)
                    elif filename_lower.endswith('.xml'):
                        xml_files.append(file_info)
                    elif filename_lower.endswith('.txt'):
                        txt_files.append(file_info)
            
            # Sort each category by score (highest first)
            instance_files.sort(key=lambda x: x['score'], reverse=True)
            xml_files.sort(key=lambda x: x['score'], reverse=True)
            txt_files.sort(key=lambda x: x['score'], reverse=True)
            
            # Report findings
            logger.debug(f"File analysis:")
            logger.debug(f"  _htm.xml files: {len(instance_files)}")
            logger.debug(f"  .xml files: {len(xml_files)}")
            logger.debug(f"  .txt files: {len(txt_files)}")
            
            # Choose the best file based on priority
            if instance_files:
                best_file = instance_files[0]
                logger.debug(f"Selected _htm.xml instance file: {best_file['filename']} (score: {best_file['score']})")
                return best_file['url']
            elif xml_files:
                best_file = xml_files[0]
                logger.debug(f"Selected .xml file: {best_file['filename']} (score: {best_file['score']})")
                return best_file['url']
            elif txt_files:
                best_file = txt_files[0]
                logger.debug(f"Selected .txt file: {best_file['filename']} (score: {best_file['score']})")
                return best_file['url']
            else:
                logger.debug("No suitable XBRL files found")
                return self._construct_fallback_url(directory_url, accession_number)
                
        except Exception as e:
            logger.debug(f"Error parsing directory content: {e}")
            return self._construct_fallback_url(directory_url, accession_number)

    def _try_direct_file_patterns(self, directory_url: str, accession_number: str, cik: str, headers: Dict) -> str:
        """
        Try common XBRL file patterns when directory listing is not available.
        
        Args:
            directory_url: Base directory URL
            accession_number: Filing accession number  
            cik: Company CIK
            headers: Request headers
            
        Returns:
            Best available file URL
        """
        logger.debug("Trying direct file patterns...")
        
        # Common file patterns to try
        patterns = []
        
        # Pattern 1: accession_number_htm.xml
        base_accession = accession_number.replace('-', '')
        patterns.append(f"{accession_number}_htm.xml")
        patterns.append(f"{base_accession}_htm.xml")
        
        # Pattern 2: Company ticker patterns (if we can determine ticker)
        ticker = self._get_ticker_for_cik(cik)
        if ticker:
            # Extract date from accession number for ticker-based patterns
            date_match = re.search(r'\d{2}-(\d{6})', accession_number)
            if date_match:
                date_part = date_match.group(1)
                year = "20" + date_part[:2]
                month = date_part[2:4]
                day = date_part[4:6]
                date_str = f"{year}{month}{day}"
                
                patterns.extend([
                    f"{ticker}-{date_str}_htm.xml",
                    f"{ticker.lower()}-{date_str}_htm.xml",
                    f"{ticker.upper()}-{date_str}_htm.xml"
                ])
        
        # Pattern 3: Generic XML patterns
        patterns.extend([
            f"{accession_number}.xml",
            f"{base_accession}.xml"
        ])
        
        # Pattern 4: TXT files as fallback
        patterns.extend([
            f"{accession_number}.txt",
            f"{base_accession}.txt"
        ])
        
        # Test each pattern
        for pattern in patterns:
            test_url = directory_url + pattern
            try:
                response = requests.head(test_url, headers=headers, timeout=5)
                if response.status_code == 200:
                    logger.debug(f"Found file using pattern: {pattern}")
                    return test_url
            except Exception:
                continue
        
        # If no patterns work, return directory URL for Arelle to handle
        logger.debug(f"No direct files found, returning directory URL for Arelle: {directory_url}")
        return directory_url

    def _score_sec_file(self, filename: str) -> int:
        """
        Score SEC files based on likelihood of being useful XBRL instance documents.
        
        Args:
            filename: Name of the file
            
        Returns:
            Score (higher = better, 0 = skip)
        """
        filename_lower = filename.lower()
        score = 0
        
        # Skip linkbase and schema files immediately
        skip_patterns = [
            '_lab.xml', '_cal.xml', '_pre.xml', '_def.xml', '_ref.xml',
            'schema', 'taxonomy', 'linkbase', 'label', 'calculation', 
            'presentation', 'definition', 'reference'
        ]
        
        for pattern in skip_patterns:
            if pattern in filename_lower:
                return 0
        
        # High priority files
        if filename_lower.endswith('_htm.xml'):
            score += 100  # Highest priority for inline XBRL converted to XML
        elif filename_lower.endswith('.xml') and 'instance' in filename_lower:
            score += 90
        elif filename_lower.endswith('.xml'):
            score += 50
        elif filename_lower.endswith('.txt'):
            score += 30
        
        # Bonus for containing accession number pattern
        if re.search(r'\d{10}-\d{2}-\d{6}', filename):
            score += 20
        
        # Bonus for company ticker patterns
        if re.search(r'[a-z]{2,5}-\d{8}', filename_lower):
            score += 15
        
        # Bonus for instance indicators
        instance_indicators = ['instance', 'document', 'filing']
        for indicator in instance_indicators:
            if indicator in filename_lower:
                score += 10
        
        return score

    def _get_ticker_for_cik(self, cik: str) -> Optional[str]:
        """
        Get ticker symbol for a given CIK.
        
        Args:
            cik: Company CIK
            
        Returns:
            Ticker symbol or None
        """
        # Expanded mapping of common CIKs to tickers
        cik_to_ticker = {
            '320193': 'aapl',     # Apple
            '1318605': 'tsla',    # Tesla  
            '789019': 'msft',     # Microsoft
            '1652044': 'googl',   # Alphabet
            '1045810': 'nvda',    # NVIDIA
            '1018724': 'amzn',    # Amazon
            '1326801': 'meta',    # Meta
            '886982': 'nflx',     # Netflix
            '1067983': 'baba',    # Alibaba
            '1559720': 'crm',     # Salesforce
        }
        
        return cik_to_ticker.get(cik.lstrip('0'))

    def _construct_fallback_url(self, directory_url: str, accession_number: str) -> str:
        """
        Construct a fallback URL when file discovery fails.
        
        Args:
            directory_url: Base directory URL
            accession_number: Filing accession number
            
        Returns:
            Fallback URL
        """
        # Try .txt file as most common fallback
        txt_url = directory_url + f"{accession_number}.txt"
        logger.debug(f"Using fallback .txt URL: {txt_url}")
        return txt_url
        
        # Fallback: return original URL
        logger.debug(f"Using original URL: {filing_url}")
        return filing_url
    
    def _should_skip_irrelevant_concept(self, concept_name: str, is_abstract: bool, has_dimensional_data: bool = False) -> bool:
        """
        Determine if a concept should be skipped based on it being an irrelevant abstract/structural element.
        
        Args:
            concept_name: The XBRL concept name (e.g., "us-gaap:IncomeStatementAbstract")
            is_abstract: Whether the concept is marked as abstract
            has_dimensional_data: Whether this concept has associated dimensional facts with values
            
        Returns:
            True if the concept should be skipped, False if it should be included
        """
        
        # Always skip concepts from non-financial taxonomies (DEI cover-page metadata,
        # SRT structural axes, country codes, investment taxonomy).  These are not
        # financial statement line items regardless of whether they carry a value.
        non_financial_prefixes = ('dei:', 'srt:', 'country:', 'invest:')
        if concept_name.startswith(non_financial_prefixes):
            return True

        # Always skip well-known structural/header patterns regardless of the abstract flag.
        # Some XBRL filings report a zero-value fact for abstract concepts, which causes
        # is_effectively_concrete=True and abstract=False — the name check must run first.
        always_skip_patterns = [
            'IncomeStatementAbstract',
            'StatementOfFinancialPositionAbstract',
            'StatementOfCashFlowsAbstract',
            'StatementOfIncomeAndComprehensiveIncomeAbstract',
            'StatementOfStockholdersEquityAbstract',
            'StatementTable',
            'StatementLineItems',
            'ComprehensiveIncomeNetOfTaxAbstract',
            'WeightedAverageNumberOfSharesOutstandingAbstract',
        ]
        for pattern in always_skip_patterns:
            if pattern in concept_name:
                return True

        # Axis concepts are ALWAYS structural (pure metadata in XBRL, never carry values)
        if concept_name.endswith('Axis'):
            return True

        # If it's not abstract, never skip it (has actual financial data)
        if not is_abstract:
            return False

        # Any concept whose local name ends in 'Abstract' is a structural grouping header.
        # Cover all remaining cases like AssetsAbstract, LiabilitiesAbstract, etc.
        local_name = concept_name.split(':')[-1]
        if local_name.endswith('Abstract'):
            return True

        # Domain concepts are structural (define axis range) — skip only when abstract
        if local_name.endswith('Domain'):
            return True

        # Skip Member concepts that are abstract and have no dimensional data
        if local_name.endswith('Member') and not has_dimensional_data:
            return True

        # Keep all other abstract concepts (they provide important hierarchy)
        return False
