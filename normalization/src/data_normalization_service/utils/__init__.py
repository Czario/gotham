"""Utilities module initialization."""

from .hierarchy import HierarchyManager
from .taxonomy import get_taxonomy_manager, lookup_concept_label
from .duplicate_prevention import DuplicatePreventionManager

__all__ = [
    "HierarchyManager",
    "get_taxonomy_manager",
    "lookup_concept_label", 
    "DuplicatePreventionManager"
]
