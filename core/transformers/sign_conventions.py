#!/usr/bin/env python3
"""
Sign Convention Handler for Financial Statement Data

This module handles the proper application of sign conventions to match SEC presentation.
It applies calculation weights and concept-based sign rules to ensure values display
correctly (positive/negative) as shown in SEC filings.

Key Principles (based on actual SEC filings like Microsoft 10-Q):
1. Income Statement: Expenses are shown POSITIVE, calculation weights apply subtraction logic
2. Cash Flow: Outflows are NEGATIVE (in parentheses), Inflows are POSITIVE
3. Balance Sheet: Contra-accounts are NEGATIVE (in parentheses)
"""

from typing import Optional
import re


class SignConventionHandler:
    """Handles sign conventions for financial statement data to match SEC presentation"""
    
    # Cash Flow Statement - Outflow patterns (should be negative)
    CASH_OUTFLOW_PATTERNS = [
        # Payments and purchases
        'Payments', 'Payment', 'Purchase', 'Acquisition', 
        'Repayment', 'Repaid', 'Redemption',
        
        # Decreases and reductions
        'Decrease', 'Reduction', 'Used',
        
        # Dividends and distributions
        'Dividends', 'Distribution',
        
        # Investments and capital
        'PurchaseOf', 'AdditionsTo', 'AcquisitionOf',
        
        # Stock buybacks
        'Repurchase', 'TreasuryStock', 'StockRepurchased',
    ]
    
    # Cash Flow Statement - Inflow patterns (should be positive)
    CASH_INFLOW_PATTERNS = [
        # Proceeds and receipts
        'Proceeds', 'Received', 'Receipt', 'Collection',
        
        # Increases and additions
        'Increase', 'Addition', 'Issuance',
        
        # Sales and disposals
        'Sale', 'Disposal', 'Divestiture',
        
        # Borrowings and financing
        'Borrowing', 'DebtIssuance', 'StockIssuance',
    ]
    
    # Balance Sheet - Contra-account patterns (should be negative)
    CONTRA_ACCOUNT_PATTERNS = [
        # Contra-assets
        'AccumulatedDepreciation', 'AccumulatedAmortization',
        'Allowance', 'AllowanceFor', 'Reserve',
        'Impairment', 'Valuation',
        
        # Contra-equity
        'TreasuryStock', 'Discount', 'DeferredCompensation',
        
        # Contra-liability (rare, but included)
        'UnamortizedDebt', 'DebtDiscount',
    ]
    
    # Income Statement - Expense patterns that typically have weight=-1 in calculations
    # Note: In SEC filings, expenses are shown POSITIVE, calculation weight handles the math
    EXPENSE_PATTERNS = [
        'Cost', 'Expense', 'Loss', 'Write', 'Impairment',
        'Depreciation', 'Amortization', 'Interest', 'Tax',
    ]
    
    @classmethod
    def apply_sign_conventions(
        cls,
        value: Optional[float],
        concept: str,
        statement_type: str,
        calc_weight: Optional[float] = None,
        label: Optional[str] = None
    ) -> Optional[float]:
        """
        Apply sign conventions to a financial value based on SEC presentation standards.
        
        Args:
            value: The numeric value to adjust
            concept: The XBRL concept name (e.g., 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment')
            statement_type: Type of statement ('income_statement', 'cash_flow', 'balance_sheet', etc.)
            calc_weight: Calculation weight from XBRL (typically 1.0 or -1.0)
            label: Human-readable label for the line item
            
        Returns:
            Adjusted value with proper sign convention applied
        """
        if value is None or value == 0:
            return value
        
        # Determine statement type for context
        statement_type_lower = statement_type.lower() if statement_type else ''
        
        # PRIORITY 1: Apply calculation weight ONLY for cash flow statements
        # For income statements, weights are for calculation validation only, not display
        # SEC shows all income statement items as positive (Revenue, Expenses, Net Income all positive)
        if calc_weight is not None and calc_weight == -1.0:
            if 'cash' in statement_type_lower or 'flow' in statement_type_lower:
                # In cash flow, weight -1.0 means cash outflow (show negative)
                return -abs(value)
            # For other statements (income, balance), ignore weight for display
            # Calculation weights are for validation math, not presentation
        
        # PRIORITY 2: Statement-specific rules based on concept patterns
        
        if 'cash' in statement_type_lower or 'flow' in statement_type_lower:
            return cls._apply_cash_flow_conventions(value, concept, label)
        
        elif 'balance' in statement_type_lower or 'position' in statement_type_lower:
            return cls._apply_balance_sheet_conventions(value, concept, label)
        
        elif 'income' in statement_type_lower or 'operations' in statement_type_lower:
            return cls._apply_income_statement_conventions(value, concept, label, calc_weight)
        
        elif 'equity' in statement_type_lower or 'stockholder' in statement_type_lower:
            return cls._apply_equity_conventions(value, concept, label)
        
        # Default: return value as-is
        return value
    
    @classmethod
    def _apply_cash_flow_conventions(
        cls,
        value: float,
        concept: str,
        label: Optional[str]
    ) -> float:
        """
        Apply cash flow statement conventions.
        
        Rule: Cash outflows are NEGATIVE (shown in parentheses in SEC filings)
              Cash inflows are POSITIVE
        """
        concept_lower = concept.lower()
        search_text = f"{concept} {label or ''}".lower()
        
        # Special handling for "IncreaseDecrease" concepts - these are NET CHANGE items
        # The sign depends on the actual change direction:
        # - Positive value = Increase in account (for assets: use of cash, for liabilities: source of cash)
        # - Negative value = Decrease in account (for assets: source of cash, for liabilities: use of cash)
        # These should rely on the original XBRL value or calculation weight, NOT pattern matching
        if 'increasedecrease' in concept_lower.replace('_', '').replace('-', ''):
            # Preserve the original sign - it already represents the economic direction
            return value
        
        # Special handling for NET CHANGE concepts that combine inflows and outflows
        # These concepts report the NET result, so preserve the original sign
        net_change_patterns = [
            'proceedsfrompayments',  # e.g., ProceedsFromPaymentsForOtherFinancingActivities
            'paymentsforproceeds',   # e.g., PaymentsForProceedsFromOtherInvestingActivities
            'proceedsfromsale',      # Some "proceeds from sale" are actually net of payments
        ]
        
        concept_normalized = concept_lower.replace('-', '').replace('_', '').replace(':', '')
        if any(pattern in concept_normalized for pattern in net_change_patterns):
            # For net change concepts, preserve the sign as-is from XBRL
            # Positive = net inflow, Negative = net outflow
            return value
        
        # Special handling for "Proceeds from Maturities" - this is a genuine inflow
        # Despite having "Payments" in some contexts, "Maturities" means receiving money back
        if 'maturit' in concept_lower and 'proceed' in concept_lower:
            return abs(value)  # Always positive - it's money received
        
        # CRITICAL: Check for netcash/netchange/total BEFORE pattern matching
        # These concepts represent NET amounts that can be positive or negative
        # Pattern matching would incorrectly flip them (e.g., "Used" in "NetCashProvidedByUsedIn...")
        if any(term in concept_lower for term in ['netcash', 'netchange', 'total']):
            return value
        
        # Check if it's already negative - preserve negative values
        if value < 0:
            return value
        
        # Check for outflow patterns (should be negative)
        for pattern in cls.CASH_OUTFLOW_PATTERNS:
            if pattern.lower() in search_text:
                return -abs(value)
        
        # Check for inflow patterns (should be positive)
        for pattern in cls.CASH_INFLOW_PATTERNS:
            if pattern.lower() in search_text:
                return abs(value)
        
        # Default for unmatched items: keep as-is
        return value
    
    @classmethod
    def _apply_balance_sheet_conventions(
        cls,
        value: float,
        concept: str,
        label: Optional[str]
    ) -> float:
        """
        Apply balance sheet conventions.
        
        Rule: Contra-accounts are NEGATIVE (shown in parentheses in SEC filings)
              Regular assets, liabilities, and equity are POSITIVE
        """
        # Check if it's already negative
        if value < 0:
            return value
        
        search_text = f"{concept} {label or ''}".lower()
        
        # Check for contra-account patterns
        for pattern in cls.CONTRA_ACCOUNT_PATTERNS:
            if pattern.lower() in search_text:
                return -abs(value)
        
        # Default: keep positive for normal accounts
        return abs(value)
    
    @classmethod
    def _apply_income_statement_conventions(
        cls,
        value: float,
        concept: str,
        label: Optional[str],
        calc_weight: Optional[float]
    ) -> float:
        """
        Apply income statement conventions.
        
        Rule: In SEC filings, expenses are shown POSITIVE
              The calculation weight (if -1.0) is applied for computation, not display
              Exception: Losses and write-offs may be shown negative if already negative
        """
        # If calc_weight is -1.0, we already handled it in main method
        # For income statements, typically expenses stay positive for display
        
        # Preserve naturally negative values (losses, write-offs that came in negative)
        if value < 0:
            return value
        
        # For positive values in income statement, keep them positive
        # The weight will be used in calculations, not display
        return abs(value)
    
    @classmethod
    def _apply_equity_conventions(
        cls,
        value: float,
        concept: str,
        label: Optional[str]
    ) -> float:
        """
        Apply equity statement conventions.
        
        Rule: In SEC equity statements, all changes are shown as POSITIVE values
              The context (whether they increase or decrease equity) is shown by the row label
              Example: Microsoft shows "Common stock cash dividends" as positive 6,764
              even though it reduces equity
        """
        # Equity statements show all values as positive, regardless of whether
        # they increase or decrease equity. The column header and row label 
        # provide the context (e.g., "Dividends" in a deduction column)
        return abs(value)
    
    @classmethod
    def should_apply_weight(
        cls,
        concept: str,
        statement_type: str,
        calc_weight: Optional[float]
    ) -> bool:
        """
        Determine if calculation weight should be applied for display purposes.
        
        Args:
            concept: XBRL concept name
            statement_type: Type of statement
            calc_weight: Calculation weight from XBRL
            
        Returns:
            True if weight should be applied, False otherwise
        """
        if calc_weight is None or calc_weight == 1.0:
            return False
        
        # For income statements, weight=-1 should be applied
        # For other statements, rely on concept patterns
        statement_type_lower = statement_type.lower() if statement_type else ''
        
        if 'income' in statement_type_lower or 'operations' in statement_type_lower:
            return calc_weight == -1.0
        
        return False
