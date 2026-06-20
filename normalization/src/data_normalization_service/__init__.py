"""
Financial Data Normalization Service

A comprehensive service for normalizing financial statement data from SEC filings
into a structured MongoDB database format with proper hierarchy and period management.
"""

__version__ = "0.2.0"
__author__ = "TrueGrids"

from .core.config import AppConfig
from .services.normalization_service import FinancialNormalizationService

__all__ = ["AppConfig", "FinancialNormalizationService"]
