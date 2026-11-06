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

    # Operating asset changes on the Cash Flow statement whose cash flow impact
    # is the inverse of the raw XBRL asset balance change (-1 * change).
    @classmethod
    def is_cash_flow_operating_asset_change(cls, concept: str) -> bool:
        """Return True if concept represents an operating asset change on Cash Flow.
        
        In SEC filing presentation tables:
          - A decrease in an operating asset is a cash inflow (shown positive without parens).
          - An increase in an operating asset is a cash outflow (shown negative in parens).
        Because raw XBRL records the asset delta (Ending - Beginning), its sign is the
        exact opposite of the presentation table. Inverting it reproduces the presentation row.
        """
        if not concept:
            return False
        local_lower = concept.rsplit(':', 1)[-1].lower()
        if not local_lower.startswith('increasedecrease'):
            return False
        asset_indicators = [
            'receivable',
            'inventor',
            'operatingasset',
            'prepaid',
            'contractwithcustomerasset',
            'contractasset',
            'otherasset',
        ]
        return any(ind in local_lower for ind in asset_indicators)

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
        Apply sign conventions to a financial value based on SEC presentation standards (Option A: Presentation Table).
        
        Args:
            value: The numeric value to adjust
            concept: The XBRL concept name (e.g., 'us-gaap:IncreaseDecreaseInAccountsReceivable')
            statement_type: Type of statement ('income_statement', 'cash_flow', 'balance_sheet', etc.)
            calc_weight: Calculation weight from XBRL (typically 1.0 or -1.0)
            label: Human-readable label for the line item
            
        Returns:
            Adjusted value matching the SEC filing presentation table row.
        """
        if value is None or value == 0:
            return value
        
        # Determine statement type for context
        statement_type_lower = statement_type.lower() if statement_type else ''
        is_cash_flow = 'cash' in statement_type_lower or 'flow' in statement_type_lower

        if is_cash_flow:
            local_lower = concept.rsplit(':', 1)[-1].lower() if concept else ''
            
            # 1. Share-based compensation is always a positive non-cash add-back
            if 'sharebasedcompensation' in local_lower:
                return abs(value)
            
            # 2. Operating asset changes: Presentation shows cash flow contribution (-1 * raw asset change)
            # E.g. Accounts receivable decrease of 9,295 is shown positive (+9,295),
            #      Inventory increase of 1,084 is shown negative ((1,084)).
            if cls.is_cash_flow_operating_asset_change(concept):
                return -float(value)
            
            # 3. Payments for other investing activities (shown in parens as outflow in filings)
            if 'paymentsforproceedsfromotherinvestingactivities' in local_lower:
                if calc_weight is not None and calc_weight == 1.0:
                    return abs(float(value))
                return -abs(float(value))

            # 4. Other noncash income/expense (shown in parens when subtracted in operating cash flows)
            if 'othernoncashincomeexpense' in local_lower:
                if calc_weight is not None and calc_weight == 1.0:
                    return abs(float(value))
                return -abs(float(value))

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
        Apply balance sheet conventions to match SEC filing presentation table.
        
        Rule: Normal assets, liabilities, and equity are POSITIVE.
              Only true contra-accounts (accumulated depreciation, allowance for credit
              loss / doubtful accounts, treasury stock) are NEGATIVE.
        """
        if value is None or value == 0:
            return value

        local = concept.rsplit(':', 1)[-1].lower() if concept else ''
        label_str = (label or '').lower()

        # Regular assets (receivables, cash, inventory, PPE, etc.) are always POSITIVE.
        # Do not treat as a contra-account even if the label contains "after allowance" or "net".
        if (
            'receivable' in local
            or 'receivable' in label_str
            or 'after allowance' in label_str
            or 'net of allowance' in label_str
            or 'less allowance' in label_str
        ):
            is_allowance_itself = (
                'allowance' in local
                and not any(k in local for k in ['netcurrent', 'grosscurrent', 'nontrade', 'accountsnotes'])
            )
            if not is_allowance_itself:
                return abs(value)

        # Check for genuine contra-accounts
        for pattern in cls.CONTRA_ACCOUNT_PATTERNS:
            pattern_lower = pattern.lower()
            if pattern_lower.startswith('allowance'):
                if 'allowance' in local and not any(k in local for k in ['netcurrent', 'grosscurrent']):
                    return -abs(value)
            elif pattern_lower in local:
                return -abs(value)

        # Default: keep positive for normal balance sheet accounts
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
