#!/usr/bin/env python3
"""
Enhanced data transformers for fixing financial statement data issues
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union, Tuple
import re
import pandas as pd
from dateutil.relativedelta import relativedelta
from utilities.helpers.period_utils import PeriodParser, FiscalYearCalculator, normalize_period_type


class UnitStandardizer:
    """Utility class for standardizing unit formats"""
    
    @staticmethod
    def standardize_units(value: Union[str, float, int], unit: str) -> Tuple[float, str]:
        """
        Standardize monetary units to a common format
        
        Args:
            value: The numeric value
            unit: The unit (e.g., 'USD', 'usd', etc.)
            
        Returns:
            Tuple of (standardized_value, standardized_unit)
        """
        if not value or pd.isna(value):
            return 0.0, 'USD'
        
        # Convert value to float
        try:
            numeric_value = float(value)
        except (ValueError, TypeError):
            return 0.0, 'USD'
        
        # Standardize unit
        if not unit:
            return numeric_value, 'USD'
        
        unit_upper = str(unit).upper()
        
        # Handle common unit variations
        if unit_upper in ['USD', 'DOLLARS', 'US DOLLARS', '$']:
            return numeric_value, 'USD'
        elif unit_upper in ['SHARES', 'SHARE']:
            return numeric_value, 'shares'
        else:
            return numeric_value, unit_upper


class CurrentPeriodFilter:
    """Utility class for filtering facts to current period only"""
    
    @staticmethod
    def filter_dimensional_facts_to_current_period(dimensional_facts: List[Dict], target_period: str) -> List[Dict]:
        """
        Filter dimensional facts to match the target period exactly
        
        Args:
            dimensional_facts: List of dimensional fact dictionaries
            target_period: Target period string (e.g., "2024-03-31 to 2024-06-30")
            
        Returns:
            List of facts matching the target period
        """
        if not dimensional_facts or not target_period:
            return dimensional_facts
        
        # Extract target period end date
        target_end_date = PeriodParser.extract_end_date_from_period_string(target_period)
        if not target_end_date:
            return dimensional_facts
        
        current_period_facts = []
        
        for fact in dimensional_facts:
            fact_period = fact.get('period', '')
            fact_end_date = PeriodParser.extract_end_date_from_period_string(fact_period)
            
            if fact_end_date:
                # Allow small tolerance for date matching (1-2 days)
                date_diff = abs((fact_end_date - target_end_date).days)
                if date_diff <= 2:
                    current_period_facts.append(fact)
            else:
                # If we can't parse the date, include conservatively
                current_period_facts.append(fact)
        
        return current_period_facts

    @staticmethod
    def remove_prior_period_facts(financial_data: List[Dict], current_year: int = 2025) -> List[Dict]:
        """
        Remove facts from prior periods (e.g., 2024 data when current is 2025)
        
        Args:
            financial_data: List of financial line items
            current_year: Current reporting year
            
        Returns:
            List with prior period facts removed
        """
        if not financial_data:
            return financial_data
        
        filtered_data = []
        
        for item in financial_data:
            # Filter main item period
            main_period = item.get('period', '')
            main_end_date = PeriodParser.extract_end_date_from_period_string(main_period)
            
            # Include item if it's from current year or we can't determine the year
            if not main_end_date or main_end_date.year >= current_year:
                # Also filter dimensional facts
                dimensional_facts = item.get('dimensional_facts', [])
                if dimensional_facts:
                    filtered_dimensional_facts = []
                    for fact in dimensional_facts:
                        fact_period = fact.get('period', '')
                        fact_end_date = PeriodParser.extract_end_date_from_period_string(fact_period)
                        
                        # Include fact if it's from current year or we can't determine the year
                        if not fact_end_date or fact_end_date.year >= current_year:
                            filtered_dimensional_facts.append(fact)
                    
                    # Update the item with filtered dimensional facts
                    item = item.copy()
                    item['dimensional_facts'] = filtered_dimensional_facts
                
                filtered_data.append(item)
        
        return filtered_data


class DimensionalDataCleaner:
    """Utility class for cleaning dimensional fact data"""
    
    @staticmethod
    def clean_dimensional_facts(dimensional_facts: List[Dict]) -> List[Dict]:
        """
        Clean and standardize dimensional facts, filtering out facts with empty dimensions
        
        Args:
            dimensional_facts: List of dimensional fact dictionaries
            
        Returns:
            Cleaned list of dimensional facts with meaningful dimension data only
        """
        if not dimensional_facts:
            return []
        
        cleaned_facts = []
        
        for fact in dimensional_facts:
            # Skip facts with null or zero values
            value = fact.get('value') or fact.get('numeric_value')
            if not value or (isinstance(value, (int, float)) and value == 0):
                continue
            
            # Get dimensions and dimension_details
            dimensions = fact.get('dimensions', {})
            dimension_details = fact.get('dimension_details', {})
            
            # Skip facts that have empty or meaningless dimensional data
            has_meaningful_dimensions = False
            
            # Check if dimensions contains meaningful data (not empty)
            if isinstance(dimensions, dict) and dimensions:
                has_meaningful_dimensions = True
            
            # Check if dimension_details contains meaningful data (not empty)
            if isinstance(dimension_details, dict) and dimension_details:
                has_meaningful_dimensions = True
            
            # Skip facts without any meaningful dimensional information
            if not has_meaningful_dimensions:
                continue
            
            # Standardize the fact structure
            cleaned_fact = {
                'concept_name': fact.get('concept_name', ''),
                'value': value,
                'unit': fact.get('unit', 'USD'),
                'period': fact.get('period', ''),
                'dimensions': dimensions,
                'dimension_details': dimension_details,  # CRITICAL: Preserve dimension metadata
            }
            
            # Add context information if available
            if 'context_id' in fact:
                cleaned_fact['context_id'] = fact['context_id']
            
            cleaned_facts.append(cleaned_fact)
        
        return cleaned_facts


class BusinessDateValidator:
    """Utility class for validating business dates"""
    
    @staticmethod
    def is_valid_business_date(date_obj: datetime) -> bool:
        """
        Check if a date is a valid business date
        
        Args:
            date_obj: Date object to validate
            
        Returns:
            True if valid business date
        """
        if not date_obj:
            return False
        
        # Check if it's a reasonable year (between 1990 and current year + 2)
        current_year = datetime.now().year
        if not (1990 <= date_obj.year <= current_year + 2):
            return False
        
        # Check if it's a reasonable month/day combination
        try:
            # This will raise ValueError if invalid
            datetime(date_obj.year, date_obj.month, date_obj.day)
            return True
        except ValueError:
            return False

    @staticmethod
    def validate_period_dates(start_date: datetime, end_date: datetime) -> bool:
        """
        Validate that period dates make business sense
        
        Args:
            start_date: Period start date
            end_date: Period end date
            
        Returns:
            True if dates are valid
        """
        if not start_date or not end_date:
            return False
        
        # Both dates must be valid
        if not (BusinessDateValidator.is_valid_business_date(start_date) and 
                BusinessDateValidator.is_valid_business_date(end_date)):
            return False
        
        # Start date must be before end date
        if start_date >= end_date:
            return False
        
        # Period shouldn't be longer than 18 months (reasonable business constraint)
        period_length = (end_date - start_date).days
        if period_length > 548:  # 18 months * 30.4 days
            return False
        
        return True


class EnhancedFinancialDataTransformer:
    """Main transformer that orchestrates all enhancements"""
    
    def __init__(self):
        self.unit_standardizer = UnitStandardizer()
        self.period_filter = CurrentPeriodFilter()
        self.data_cleaner = DimensionalDataCleaner()
        self.date_validator = BusinessDateValidator()
    
    def transform_financial_statement_data(self, financial_data: List[Dict], 
                                         reporting_period: Dict,
                                         statement_type: str = None) -> List[Dict]:
        """
        Apply all transformations to financial statement data
        
        Args:
            financial_data: List of financial line items
            reporting_period: Reporting period information
            statement_type: Type of statement for sign conventions
            
        Returns:
            Transformed financial data
        """
        if not financial_data:
            return financial_data
        
        transformed_data = []
        
        for item in financial_data:
            # Transform the main item
            transformed_item = self._transform_line_item(item, reporting_period, statement_type)
            
            # Transform dimensional facts if present
            dimensional_facts = item.get('dimensional_facts', [])
            if dimensional_facts:
                # Clean dimensional facts
                cleaned_facts = self.data_cleaner.clean_dimensional_facts(dimensional_facts)
                
                # Filter to current period if requested
                if reporting_period and reporting_period.get('period'):
                    cleaned_facts = self.period_filter.filter_dimensional_facts_to_current_period(
                        cleaned_facts, reporting_period['period']
                    )
                
                transformed_item['dimensional_facts'] = cleaned_facts
            
            transformed_data.append(transformed_item)
        
        return transformed_data
    
    def transform_statement_data(self, 
                               statement_data: List, 
                               filing_id: Any, 
                               company_cik: str, 
                               statement_type: str,
                               reporting_period: Dict,
                               primary_period_string: Optional[str] = None) -> Dict:
        """
        Transform financial statement data for database storage - main entry point
        
        Args:
            statement_data: List of financial line items
            filing_id: Filing identifier
            company_cik: Company CIK
            statement_type: Type of financial statement (e.g., 'income', 'cash_flow', 'balancesheet')
            reporting_period: Reporting period information
            primary_period_string: Primary period string from XBRL
            
        Returns:
            Transformed statement document ready for database storage
        """
        # Apply financial data transformations with statement type for sign conventions
        transformed_data = self.transform_financial_statement_data(
            statement_data, reporting_period, statement_type
        )
        
        # Create the final document structure
        statement_doc = {
            'filing_id': filing_id,
            'cik': company_cik,
            'statement_type': statement_type,
            'reporting_period': reporting_period,
            'primary_period_string': primary_period_string,
            'data': transformed_data,
            'created_at': datetime.utcnow(),
            'data_source': 'xbrl_enhanced'
        }
        
        return statement_doc
    
    def _transform_line_item(self, item: Dict, reporting_period: Dict, statement_type: str = None) -> Dict:
        """Transform a single line item"""
        transformed_item = item.copy()
        
        # Standardize units
        value = item.get('value')
        unit = item.get('unit', 'USD')
        
        if value is not None:
            standardized_value, standardized_unit = self.unit_standardizer.standardize_units(value, unit)
            transformed_item['value'] = standardized_value
            transformed_item['unit'] = standardized_unit
            
            # Apply sign conventions to match SEC presentation
            if statement_type:
                from core.transformers.sign_conventions import SignConventionHandler
                
                # Extract calculation weight if available
                calc_weight = None
                calculations = item.get('calculations', {})
                if calculations:
                    # Check if this item is a summation child with a weight
                    summation_parents = calculations.get('summation_parents', [])
                    if summation_parents and len(summation_parents) > 0:
                        calc_weight = summation_parents[0].get('weight')
                
                # Apply sign conventions
                corrected_value = SignConventionHandler.apply_sign_conventions(
                    value=standardized_value,
                    concept=item.get('concept', item.get('concept_name', '')),
                    statement_type=statement_type,
                    calc_weight=calc_weight,
                    label=item.get('label', '')
                )
                
                # Store both original and corrected values for transparency
                transformed_item['original_value'] = standardized_value
                transformed_item['value'] = corrected_value
                if calc_weight is not None:
                    transformed_item['calc_weight'] = calc_weight
        
        # Validate period dates if present
        period = item.get('period')
        if period:
            start_date = PeriodParser.extract_start_date_from_period_string(period)
            end_date = PeriodParser.extract_end_date_from_period_string(period)
            
            if start_date and end_date:
                if not self.date_validator.validate_period_dates(start_date, end_date):
                    transformed_item['period_validation_warning'] = True
        
        return transformed_item

    def standardize_reporting_period(self, reporting_period: Dict, 
                                   primary_period_string: Optional[str] = None, 
                                   xbrl_fiscal_period: Optional[str] = None) -> Dict:
        """
        Standardize reporting period using centralized utilities
        
        Args:
            reporting_period: Original reporting period data
            primary_period_string: Primary period string from XBRL
            xbrl_fiscal_period: XBRL fiscal period value
            
        Returns:
            Enhanced reporting period with standardized fields
        """
        enhanced_period = reporting_period.copy()
        
        # Extract dates using centralized parser
        end_date = enhanced_period.get('end_date')
        if isinstance(end_date, str):
            try:
                end_date = datetime.strptime(end_date, '%Y-%m-%d')
                enhanced_period['end_date'] = end_date
            except ValueError:
                pass
        
        # Extract start date
        start_date = enhanced_period.get('start_date')
        if not start_date and primary_period_string:
            start_date = PeriodParser.extract_start_date_from_period_string(primary_period_string)
            if start_date:
                enhanced_period['start_date'] = start_date
        elif not start_date and end_date and isinstance(end_date, datetime):
            # Estimate start_date based on period type
            period_type = enhanced_period.get('period_type', 'quarterly')
            if period_type == 'quarterly':
                start_date = end_date - relativedelta(months=3) + timedelta(days=1)
            elif period_type == 'annual':
                start_date = end_date - relativedelta(months=12) + timedelta(days=1)
            else:
                start_date = end_date - relativedelta(months=3) + timedelta(days=1)  # Default to quarterly
            enhanced_period['start_date'] = start_date
        
        # Use centralized fiscal year calculation
        if end_date and isinstance(end_date, datetime):
            fiscal_year_end_code = enhanced_period.get('fiscal_year_end_code')
            form_type = enhanced_period.get('form_type', '')
            
            if fiscal_year_end_code:
                # Use XBRL fiscal period if available
                xbrl_quarter = None
                if xbrl_fiscal_period:
                    xbrl_quarter = FiscalYearCalculator.extract_quarter_from_xbrl_fiscal_period(xbrl_fiscal_period)
                
                if xbrl_quarter and enhanced_period.get('period_type') == 'quarterly':
                    enhanced_period['quarter'] = xbrl_quarter
                    enhanced_period['fiscal_period_source'] = 'xbrl_document'
                
                # Calculate fiscal year if missing
                if 'fiscal_year' not in enhanced_period:
                    fiscal_year, _ = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                        end_date, fiscal_year_end_code
                    )
                    if fiscal_year:
                        enhanced_period['fiscal_year'] = fiscal_year
                
                # Use form-based quarter determination if not from XBRL
                if 'quarter' not in enhanced_period and enhanced_period.get('period_type') == 'quarterly':
                    quarter = FiscalYearCalculator.determine_quarter_from_form_and_date(
                        form_type, end_date, fiscal_year_end_code
                    )
                    if quarter:
                        enhanced_period['quarter'] = quarter
                        enhanced_period['fiscal_period_source'] = 'form_type_and_date'
        
        # Normalize period type
        period_type = enhanced_period.get('period_type')
        if period_type:
            enhanced_period['period_type'] = normalize_period_type(period_type)
        
        return enhanced_period


# Backward compatibility - maintain old class for existing imports
class PeriodStandardizer:
    """Legacy compatibility class - delegates to centralized utilities"""
    
    @staticmethod
    def extract_start_date_from_period_string(period_string: str) -> Optional[datetime]:
        return PeriodParser.extract_start_date_from_period_string(period_string)

    @staticmethod
    def extract_end_date_from_period_string(period_string: str) -> Optional[datetime]:
        return PeriodParser.extract_end_date_from_period_string(period_string)

    @staticmethod
    def extract_quarter_from_xbrl_fiscal_period(fiscal_period: str) -> Optional[int]:
        return FiscalYearCalculator.extract_quarter_from_xbrl_fiscal_period(fiscal_period)

    @staticmethod
    def determine_quarter_from_form_and_date(form_type: str, end_date: datetime, fiscal_year_end_code: str) -> Optional[int]:
        return FiscalYearCalculator.determine_quarter_from_form_and_date(form_type, end_date, fiscal_year_end_code)

    @staticmethod
    def calculate_fiscal_year_and_quarter(end_date: datetime, fiscal_year_end_code: str) -> Tuple[Optional[int], Optional[int]]:
        return FiscalYearCalculator.calculate_fiscal_year_and_quarter(end_date, fiscal_year_end_code)

    @staticmethod
    def standardize_reporting_period(reporting_period: Dict, primary_period_string: Optional[str] = None, xbrl_fiscal_period: Optional[str] = None) -> Dict:
        transformer = EnhancedFinancialDataTransformer()
        return transformer.standardize_reporting_period(reporting_period, primary_period_string, xbrl_fiscal_period)
