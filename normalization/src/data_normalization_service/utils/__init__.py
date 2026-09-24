"""Utilities module initialization."""

from .hierarchy import HierarchyManager
from .taxonomy import get_taxonomy_manager, lookup_concept_label
from .duplicate_prevention import DuplicatePreventionManager
from .label_cleaning import clean_label, clean_labels_in_item

__all__ = [
    "HierarchyManager",
    "get_taxonomy_manager",
    "lookup_concept_label", 
    "DuplicatePreventionManager",
    "clean_label",
    "clean_labels_in_item",
]
