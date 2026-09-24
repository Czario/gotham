#!/usr/bin/env python3
"""
Dimensional context filtering utilities to exclude unwanted dimensional contexts
like forecasts, scenarios, estimates, and adjustments that don't represent actual reported figures.
"""

import re
import logging
from typing import Dict, List, Optional, Any, Set
from datetime import datetime

logger = logging.getLogger(__name__)


class DimensionalContextFilter:
    """Filter out unwanted dimensional contexts from XBRL data"""
    
    # Dimensional members that should be excluded (forecast, scenario, estimate data)
    EXCLUDED_MEMBERS = {
        # Forecast/Scenario members
        'srt:ScenarioForecastMember',
        'us-gaap:ScenarioForecastMember', 
        'srt:ScenarioUnspecifiedMember',
        'us-gaap:ScenarioUnspecifiedMember',
        
        # Estimate/Adjustment members
        'us-gaap:ChangeInAccountingEstimateByTypeMember',
        'us-gaap:ChangeInAccountingPrincipleMember',
        'us-gaap:ErrorCorrectionMember',
        'us-gaap:RestatementMember',
        'us-gaap:AdjustmentMember',
        
        # Pro forma members
        'us-gaap:ProFormaMember',
        'us-gaap:ProFormaAdjustmentMember',
        
        # Budget/Planned members
        'us-gaap:BudgetMember',
        'us-gaap:PlannedMember',
        'us-gaap:ProjectedMember',

        # Aggregate/total-segment members — value equals consolidated total, not a real breakdown.
        # Company-specific variants (e.g. nflx:ReportableSegmentMember) are caught by the
        # 'reportable' EXCLUDED_KEYWORD; these cover the well-known srt qnames.
        'srt:ReportableSegmentsMember',
        'srt:OperatingSegmentsMember',
        'srt:AllSegmentsMember',

        # Reconciling/elimination members — internal accounting adjustments, not segment data.
        'srt:MaterialReconcilingItemsMember',
        'srt:EliminationsMember',
        'us-gaap:IntersegmentEliminationMember',
        'us-gaap:IntersubsegmentEliminationsMember',

        # Disclosure-only members that are not statement line-item breakdowns:
        # derivative hedge instruments and AOCI-reclassification schedules.
        # These are matched by EXACT member name (both the qualified qname and
        # the local name), never by keyword, so the earlier substring/token
        # false-positives (e.g. 'change' inside 'ForeignExchangeContractMember')
        # are not reintroduced — only these specific members are dropped.
        'us-gaap:ForeignExchangeContractMember',
        'ForeignExchangeContractMember',
        'us-gaap:InterestRateContractMember',
        'InterestRateContractMember',
        'us-gaap:ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember',
        'ReclassificationOutOfAccumulatedOtherComprehensiveIncomeMember',
    }
    
    # Dimensional axes that often contain unwanted data
    EXCLUDED_AXES = {
        'srt:StatementScenarioAxis',  # Usually contains forecast/scenario data
        'us-gaap:ChangeInAccountingEstimateByTypeAxis',  # Accounting estimates
        'us-gaap:ChangeInAccountingPrincipleAxis',  # Accounting principle changes
        'us-gaap:ErrorCorrectionAxis',  # Error corrections
        'us-gaap:RestatementAxis',  # Restatements
    }
    
    # Keywords in member names that indicate unwanted contexts.
    # These are matched against whole camelCase/qname tokens (see _matches_excluded_keyword),
    # NOT as raw substrings. Substring matching previously caused false positives such as
    # 'change' matching 'ForeignExchangeContractMember' and 'life' matching
    # 'LifeInsuranceSegmentMember', silently dropping legitimate dimensional facts.
    #
    # 'reportable' is included to catch *ReportableSegmentMember variants, which always
    # represent the aggregate of all reportable segments (= same value as consolidated),
    # not a meaningful breakdown. These appear as duplicate children in the UI.
    EXCLUDED_KEYWORDS = {
        'forecast', 'scenario', 'estimate', 'adjustment', 'restatement',
        'error', 'correction', 'proforma', 'forma', 'budget', 'planned',
        'projected', 'unspecified', 'change', 'useful',
        'reportable',  # *ReportableSegmentMember = aggregate total of all segments
    }

    @staticmethod
    def _tokenize_qname(name: str) -> Set[str]:
        """
        Split an XBRL qname / member / axis name into lowercase word tokens.

        Handles camelCase boundaries and the separators ':', '_', '-', '.', and digits,
        so 'us-gaap:ForeignExchangeContractMember' -> {'us','gaap','foreign','exchange',
        'contract','member'}. This lets EXCLUDED_KEYWORDS match whole words instead of
        accidental substrings.
        """
        if not name:
            return set()
        # Insert spaces at camelCase boundaries: 'ForeignExchange' -> 'Foreign Exchange'
        spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)
        # Split on any non-alphabetic character (handles ':', '_', '-', '.', digits)
        tokens = re.split(r'[^a-zA-Z]+', spaced)
        return {t.lower() for t in tokens if t}

    @classmethod
    def _matches_excluded_keyword(cls, name: str) -> bool:
        """Return True if any excluded keyword appears as a whole token in name."""
        if not name:
            return False
        return bool(cls._tokenize_qname(name) & cls.EXCLUDED_KEYWORDS)

    
    @classmethod
    def should_exclude_dimensional_fact(cls, dimensional_fact: Dict[str, Any]) -> bool:
        """
        Determine if a dimensional fact should be excluded based on its dimensional context
        
        Args:
            dimensional_fact: Dictionary containing dimensional fact information
            
        Returns:
            True if the fact should be excluded, False otherwise
        """
        # Get dimensions and dimension_details
        dimensions = dimensional_fact.get('dimensions', {})
        dimension_details = dimensional_fact.get('dimension_details', {})
        
        if not dimensions and not dimension_details:
            return False  # No dimensional context to filter
        
        # Check explicit member exclusions
        for member_value in dimensions.values():
            if isinstance(member_value, str) and member_value in cls.EXCLUDED_MEMBERS:
                return True
        
        # Check for excluded axes
        for axis_name in dimensions.keys():
            if axis_name in cls.EXCLUDED_AXES:
                return True
        
        # Check dimension_details for excluded axes and members
        if isinstance(dimension_details, dict):
            for dim_name, dim_info in dimension_details.items():
                if isinstance(dim_info, dict):
                    # Check axis_qname
                    axis_qname = dim_info.get('axis_qname', '')
                    if axis_qname in cls.EXCLUDED_AXES:
                        return True
                    
                    # Check member_qname
                    member_qname = dim_info.get('member_qname', '')
                    if member_qname in cls.EXCLUDED_MEMBERS:
                        return True
                    
                    # Check for excluded keywords in member labels (whole-token match)
                    member_label = dim_info.get('member_label', '')
                    if cls._matches_excluded_keyword(member_label):
                        return True
        
        return False
    
    @classmethod
    def should_exclude_main_fact(cls, fact_data: Dict[str, Any]) -> bool:
        """
        Determine if a main fact should be excluded based on its dimensional context
        
        Args:
            fact_data: Dictionary containing fact information including dimensions
            
        Returns:
            True if the fact should be excluded, False otherwise
        """
        # Check the main fact's dimensions
        dimensions = fact_data.get('dimensions', [])
        if isinstance(dimensions, list):
            for dim in dimensions:
                if isinstance(dim, dict):
                    member = dim.get('member', '')
                    axis = dim.get('axis', '')
                    
                    if member in cls.EXCLUDED_MEMBERS or axis in cls.EXCLUDED_AXES:
                        return True
                    
                    # Check for excluded keywords in member or axis (whole-token match)
                    if cls._matches_excluded_keyword(member):
                        return True
                    if cls._matches_excluded_keyword(axis):
                        return True
        
        return False
    
    @classmethod
    def filter_dimensional_facts(cls, dimensional_facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter out unwanted dimensional facts
        
        Args:
            dimensional_facts: List of dimensional fact dictionaries
            
        Returns:
            Filtered list with unwanted dimensional contexts removed
        """
        if not dimensional_facts:
            return dimensional_facts
        
        filtered_facts = []
        excluded_count = 0
        
        for fact in dimensional_facts:
            if not cls.should_exclude_dimensional_fact(fact):
                filtered_facts.append(fact)
            else:
                excluded_count += 1
        
        if excluded_count > 0:
            logger.debug(f"DIMENSIONAL CONTEXT FILTER: Excluded {excluded_count} forecast/estimate/scenario facts")
        
        return filtered_facts
    
    @classmethod
    def filter_financial_statement_data(cls, statement_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter financial statement data to remove unwanted dimensional contexts
        
        Args:
            statement_data: List of financial statement line items
            
        Returns:
            Filtered statement data with unwanted contexts removed
        """
        if not statement_data:
            return statement_data
        
        filtered_data = []
        main_facts_excluded = 0
        dimensional_facts_excluded = 0
        
        for item in statement_data:
            # Check if main fact should be excluded
            if cls.should_exclude_main_fact(item):
                main_facts_excluded += 1
                continue  # Skip this entire item
            
            # Filter dimensional_facts within the item
            dimensional_facts = item.get('dimensional_facts', [])
            if dimensional_facts:
                original_count = len(dimensional_facts)
                filtered_dimensional_facts = cls.filter_dimensional_facts(dimensional_facts)
                excluded_in_item = original_count - len(filtered_dimensional_facts)
                dimensional_facts_excluded += excluded_in_item
                
                # Update the item with filtered dimensional facts
                item = item.copy()
                item['dimensional_facts'] = filtered_dimensional_facts
            
            filtered_data.append(item)
        
        if main_facts_excluded > 0:
            logger.debug(f"MAIN FACTS FILTER: Excluded {main_facts_excluded} main facts with unwanted contexts")
        if dimensional_facts_excluded > 0:
            logger.debug(f"DIMENSIONAL FACTS FILTER: Excluded {dimensional_facts_excluded} dimensional facts with unwanted contexts")
        
        return filtered_data


class PeriodValidationFilter:
    """Filter facts with incorrect periods relative to the filing date"""
    
    @classmethod
    def should_exclude_by_period(cls, fact_data: Dict[str, Any], filing_fiscal_year: int) -> bool:
        """
        Determine if a fact should be excluded due to incorrect period
        
        Args:
            fact_data: Dictionary containing fact information
            filing_fiscal_year: The fiscal year of the filing
            
        Returns:
            True if the fact should be excluded due to period mismatch
        """
        period_str = fact_data.get('period', '')
        if not period_str:
            return False
        
        # Extract year from period string (look for years > filing_fiscal_year)
        # Pattern: "2024-01-01 00:00:00 to 2025-01-01 00:00:00"
        years = re.findall(r'\b(20\d{2})\b', period_str)
        if years:
            max_year = max(int(year) for year in years)
            # Exclude facts from future years (likely forecasts)
            # Allow year+1 to accommodate period end dates (e.g., FY2024 ends 2025-01-01)
            if max_year > filing_fiscal_year + 1:
                return True
        
        return False
    
    @classmethod
    def filter_by_period_validation(cls, statement_data: List[Dict[str, Any]], filing_fiscal_year: int) -> List[Dict[str, Any]]:
        """
        Filter statement data to remove facts with incorrect periods
        
        Args:
            statement_data: List of financial statement line items
            filing_fiscal_year: The fiscal year of the filing
            
        Returns:
            Filtered statement data with period-invalid facts removed
        """
        if not statement_data:
            return statement_data
        
        filtered_data = []
        period_excluded = 0
        
        for item in statement_data:
            # Check main fact period
            if cls.should_exclude_by_period(item, filing_fiscal_year):
                period_excluded += 1
                continue
            
            # Filter dimensional facts by period
            dimensional_facts = item.get('dimensional_facts', [])
            if dimensional_facts:
                filtered_dimensional_facts = []
                for fact in dimensional_facts:
                    if not cls.should_exclude_by_period(fact, filing_fiscal_year):
                        filtered_dimensional_facts.append(fact)
                
                # Update the item with filtered dimensional facts
                item = item.copy()
                item['dimensional_facts'] = filtered_dimensional_facts
            
            filtered_data.append(item)
        
        if period_excluded > 0:
            logger.debug(f"PERIOD VALIDATION FILTER: Excluded {period_excluded} facts with incorrect periods")
        
        return filtered_data


def apply_dimensional_context_filters(statement_data: List[Dict[str, Any]], filing_fiscal_year: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Apply all dimensional context filters to clean the financial statement data
    
    Args:
        statement_data: List of financial statement line items
        filing_fiscal_year: The fiscal year of the filing (for period validation)
        
    Returns:
        Cleaned statement data with unwanted contexts removed
    """
    if not statement_data:
        return statement_data
    
    logger.debug("Applying dimensional context filters...")
    
    # Apply dimensional context filtering
    filtered_data = DimensionalContextFilter.filter_financial_statement_data(statement_data)
    
    # Apply period validation if fiscal year is provided
    if filing_fiscal_year:
        filtered_data = PeriodValidationFilter.filter_by_period_validation(filtered_data, filing_fiscal_year)
    
    logger.debug("Dimensional context filtering completed")
    return filtered_data
