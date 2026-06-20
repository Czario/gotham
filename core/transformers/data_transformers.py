#!/usr/bin/env python3
"""Data transformation utilities for SEC data to structured format"""

from datetime import datetime
from typing import Dict, List, Optional, Any

class CompanyDataTransformer:
    """Transform raw SEC company data to structured format"""
    
    @staticmethod
    def transform_company_data(raw_data: Dict) -> Dict:
        """Transform raw SEC API company data into structured format"""
        # Extract business address from complex address structure
        business_address = {}
        addresses = raw_data.get('addresses', {})
        if addresses.get('business'):
            addr = addresses['business']
            business_address = {
                "street": f"{addr.get('street1', '')}{' ' + addr.get('street2', '') if addr.get('street2') else ''}".strip() or None,
                "city": addr.get('city'),
                "state": addr.get('stateOrCountry'),
                "zip_code": addr.get('zipCode')
            }
        
        return {
            "cik": raw_data.get('cik'),
            "name": raw_data.get('name'),
            "ticker_symbol": raw_data.get('tickers', [None])[0] if raw_data.get('tickers') else None,
            "industry": {
                "sic_code": raw_data.get('sic'),
                "sic_description": raw_data.get('sicDescription')
            },
            "market_info": {
                "tickers": raw_data.get('tickers', []),
                "exchanges": raw_data.get('exchanges', [])
            },
            "corporate_info": {
                "state_of_incorporation": raw_data.get('stateOfIncorporation'),
                "fiscal_year_end": raw_data.get('fiscalYearEnd'),
                "entity_type": raw_data.get('entityType'),
                "business_address": business_address,
                "phone": raw_data.get('phone')
            },
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }

class FilingDataTransformer:
    """Transform filing data for database storage"""
    
    @staticmethod
    def transform_filing_data(filing_info: Dict, company_cik: str) -> Dict:
        """Transform filing information into structured format"""
        # Parse acceptance datetime if provided
        acceptance_datetime = None
        if filing_info.get('acceptanceDateTime'):
            try:
                acceptance_datetime = datetime.strptime(filing_info['acceptanceDateTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
            except (ValueError, TypeError):
                try:
                    acceptance_datetime = datetime.strptime(filing_info['acceptanceDateTime'], '%Y-%m-%dT%H:%M:%S')
                except (ValueError, TypeError):
                    acceptance_datetime = None
        
        return {
            "company_cik": str(company_cik),
            "accession_number": filing_info['accessionNumber'],
            "form_type": filing_info['form'],
            "filing_date": datetime.strptime(filing_info['filingDate'], '%Y-%m-%d'),
            "acceptance_datetime": acceptance_datetime,
            "created_at": datetime.now()
        }

class FinancialDataTransformer:
    @staticmethod
    def transform_statement_data(
        statement_data: List, 
        filing_id: Any, 
        company_cik: str, 
        statement_type: str,
        reporting_period: Dict,
        primary_period_string: Optional[str] = None
    ) -> Dict:
        """
        Transform financial statement data for database storage using enhanced transformers
        
        This method now uses the enhanced transformer that fixes:
        - Missing start_date
        - Period format standardization
        - Business date validation
        - Fiscal year/quarter calculations
        - Unit standardization
        - Prior period filtering
        """
        # Import enhanced transformers
        from core.transformers.advanced_transformers import EnhancedFinancialDataTransformer
        
        # Create instance and call method
        transformer = EnhancedFinancialDataTransformer()
        return transformer.transform_statement_data(
            statement_data=statement_data,
            filing_id=filing_id,
            company_cik=company_cik,
            statement_type=statement_type,
            reporting_period=reporting_period,
            primary_period_string=primary_period_string
        )

class ValidationUtils:
    """Utilities for data validation"""
    
    @staticmethod
    def validate_cik(cik: str) -> bool:
        """Validate CIK format"""
        if not cik:
            return False
        try:
            int(cik)
            return len(cik) <= 10
        except ValueError:
            return False
    
    @staticmethod
    def validate_accession_number(accession_number: str) -> bool:
        """Validate accession number format"""
        if not accession_number:
            return False
        
        # Format: NNNNNNNNNN-NN-NNNNNN
        parts = accession_number.split('-')
        return (
            len(parts) == 3 and
            len(parts[0]) == 10 and parts[0].isdigit() and
            len(parts[1]) == 2 and parts[1].isdigit() and
            len(parts[2]) == 6 and parts[2].isdigit()
        )
    
    @staticmethod
    def validate_financial_data(data: Dict) -> List[str]:
        """Validate financial statement data"""
        errors = []
        
        if not data.get('financial_data'):
            errors.append("No financial data provided")
        
        if not isinstance(data.get('financial_data'), list):
            errors.append("Financial data must be a list")
        
        for i, item in enumerate(data.get('financial_data', [])):
            if not isinstance(item, dict):
                errors.append(f"Financial data item {i} must be a dictionary")
                continue
                
            # Check for essential fields (using current field names)
            if 'fact_concept' not in item:
                errors.append(f"Financial data item {i} missing fact_concept field")
            
            if 'fact_label' not in item:
                errors.append(f"Financial data item {i} missing fact_label field")
                
            if 'fact_value' not in item:
                errors.append(f"Financial data item {i} missing fact_value field")
        
        return errors
