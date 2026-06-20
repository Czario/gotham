"""
XBRL Taxonomy Label Management System
Fetches and caches proper concept labels from US-GAAP taxonomy.
"""
import os
import re
import requests
import zipfile
import xml.etree.ElementTree as ET
import json
import logging
import glob
from typing import Dict, Tuple, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL = "https://xbrl.fasb.org/us-gaap/{year}/us-gaap-{year}.zip"
BASE_DIR = "us-gaap-taxonomy"
CACHE_FILE = "concept_labels_cache.json"
CACHE_DURATION_DAYS = 30  # Cache labels for 30 days

NAMESPACE = {
    'link': 'http://www.xbrl.org/2003/linkbase',
    'xlink': 'http://www.w3.org/1999/xlink'
}


class TaxonomyLabelManager:
    """Manages XBRL taxonomy label fetching and caching."""
    
    def __init__(self, taxonomy_base_dir: str = "us-gaap-taxonomy"):
        """
        Initialize taxonomy label manager using only local taxonomy years that exist in the given directory.
        
        Args:
            taxonomy_base_dir: Directory containing local taxonomy folders (e.g., 2022, 2023, 2024)
        """
        self.taxonomy_base_dir = taxonomy_base_dir
        self.concept_labels = {}
        self.cache_path = os.path.join(self.taxonomy_base_dir, CACHE_FILE)
        
        # Only use top-level numeric folders as years
        self.available_years = sorted([
            int(name)
            for name in os.listdir(self.taxonomy_base_dir)
            if os.path.isdir(os.path.join(self.taxonomy_base_dir, name)) and name.isdigit()
        ])
        
        # Load cached labels or build new ones
        self._load_or_build_labels()
    
    def _load_or_build_labels(self):
        """Load labels from cache or build from taxonomy files."""
        if self._is_cache_valid():
            logger.info("Loading concept labels from cache...")
            self._load_from_cache()
        else:
            logger.info("Building concept labels from taxonomy...")
            self._build_concept_labels()
            self._save_to_cache()
    
    def _is_cache_valid(self) -> bool:
        """Check if cache file exists and is recent enough."""
        if not os.path.exists(self.cache_path):
            return False
        
        # Check cache age
        cache_mtime = os.path.getmtime(self.cache_path)
        cache_date = datetime.fromtimestamp(cache_mtime)
        cutoff_date = datetime.now() - timedelta(days=CACHE_DURATION_DAYS)
        
        return cache_date > cutoff_date
    
    def _load_from_cache(self):
        """Load concept labels from cache file."""
        try:
            with open(self.cache_path, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                self.concept_labels = cache_data.get('concept_labels', {})
                logger.info(f"Loaded {len(self.concept_labels)} concept labels from cache")
        except Exception as e:
            logger.error(f"Error loading cache: {e}")
            self._build_concept_labels()
    
    def _save_to_cache(self):
        """Save concept labels to cache file."""
        try:
            cache_data = {
                'concept_labels': self.concept_labels,
                'created_at': datetime.now().isoformat(),
                'year_range': f"{min(self.available_years)}-{max(self.available_years)}"
            }
            with open(self.cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
                logger.info(f"Saved {len(self.concept_labels)} concept labels to cache")
        except Exception as e:
            logger.error(f"Error saving cache: {e}")
    
    def _build_concept_labels(self):
        """Build concept label map from all available local taxonomy years, prioritizing most recent."""
        concept_label_map = {}
        # Search years in descending order (most recent first)
        for year in sorted(self.available_years, reverse=True):
            logger.info(f"Processing local taxonomy for year {year}...")
            year_path = os.path.join(self.taxonomy_base_dir, str(year))
            labels = self._extract_labels_from_year(year_path, year)
            for concept, (label, y) in labels.items():
                # Only set if not already found in a more recent year
                if concept not in concept_label_map:
                    concept_label_map[concept] = (label, y)
        self.concept_labels = {
            concept: label for concept, (label, year) in concept_label_map.items()
        }
        logger.info(f"Built label map with {len(self.concept_labels)} concepts from years: {self.available_years}")
    
    def _extract_labels_from_year(self, year_path: str, year: int) -> Dict[str, Tuple[str, int]]:
        """Extract concept labels from all label files in year directory (recursively)."""
        concept_labels = {}
        for root, _, files in os.walk(year_path):
            for file in files:
                # Accept both lab_us-gaap_*.xml and *-lab-*.xml (e.g., us-gaap-lab-2024.xml)
                if (file.startswith("lab_us-gaap_") and file.endswith(".xml")) or ("-lab-" in file and file.endswith(".xml")):
                    file_path = os.path.join(root, file)
                    print(f"[TAXONOMY DEBUG] Parsing label file: {file_path}")
                    labels = self._extract_concept_labels(file_path, year)
                    print(f"[TAXONOMY DEBUG] Extracted {len(labels)} labels from {file}")
                    for concept, (label, y) in labels.items():
                        if concept not in concept_labels or concept_labels[concept][1] < y:
                            concept_labels[concept] = (label, y)
        return concept_labels
    
    def _extract_concept_labels(self, xml_path: str, year: int) -> Dict[str, Tuple[str, int]]:
        """Parse XML label file and extract concept labels."""
        concept_labels = {}
        
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            # Find all label elements
            for label in root.findall('.//link:label', NAMESPACE):
                # Only use standard labels in English
                role = label.attrib.get('{http://www.w3.org/1999/xlink}role', '')
                lang = label.attrib.get('{http://www.w3.org/XML/1998/namespace}lang', '')
                if role != 'http://www.xbrl.org/2003/role/label' or lang != 'en-US':
                    continue
                xlink_label = label.attrib.get('{http://www.w3.org/1999/xlink}label', '')
                # xlink_label is like 'lab_NetIncomeLoss' or 'lab_NetIncomeLoss_label_en-US'
                if xlink_label.startswith('lab_'):
                    # Remove 'lab_' prefix and any suffix after concept name
                    concept_name = xlink_label[4:]
                    # Remove any suffix after the concept name (e.g., '_label_en-US')
                    concept_name = concept_name.split('_label')[0]
                    if concept_name not in concept_labels or concept_labels[concept_name][1] < year:
                        label_text = label.text.strip() if label.text else ''
                        concept_labels[concept_name] = (label_text, year)
        
        except Exception as e:
            logger.error(f"Error parsing {xml_path}: {e}")
        
        return concept_labels
    
    def lookup_label(self, concept_name: str) -> str:
        """
        Look up proper label for a concept.
        
        Args:
            concept_name: XBRL concept name (e.g., "us-gaap_NetIncomeLoss")
            
        Returns:
            Human-readable label or the concept name if not found
        """
        clean_concept = self._normalize_concept_name(concept_name)
        
        # Try exact match first
        if clean_concept in self.concept_labels:
            return self.concept_labels[clean_concept]
        
        # Try with us-gaap prefix if not present
        if not clean_concept.startswith('us-gaap_'):
            prefixed_concept = f"us-gaap_{clean_concept}"
            if prefixed_concept in self.concept_labels:
                return self.concept_labels[prefixed_concept]
        
        # Try without prefix if present
        if clean_concept.startswith('us-gaap_'):
            unprefixed_concept = clean_concept.replace('us-gaap_', '')
            if unprefixed_concept in self.concept_labels:
                return self.concept_labels[unprefixed_concept]
        
        # Fallback: return the original concept name
        return concept_name
    
    def _normalize_concept_name(self, concept_name: str) -> str:
        """Normalize concept name for consistent lookup."""
        if not concept_name:
            return ""
        
        # Remove common prefixes/suffixes that might interfere
        clean_name = concept_name.strip()
        
        # Handle namespace variations
        if ':' in clean_name:
            clean_name = clean_name.split(':')[-1]
        
        return clean_name
    
    def get_label_stats(self) -> dict:
        """Get statistics about loaded labels."""
        if not self.available_years:
            return {
                'total_concepts': len(self.concept_labels),
                'year_range': 'none',
                'cache_exists': os.path.exists(self.cache_path),
                'error': 'No local taxonomy years found',
                'detected_dirs': os.listdir(self.taxonomy_base_dir)
            }
        return {
            'total_concepts': len(self.concept_labels),
            'year_range': f"{min(self.available_years)}-{max(self.available_years)}",
            'cache_exists': os.path.exists(self.cache_path),
            'available_years': self.available_years
        }


# Global instance for reuse
_taxonomy_manager = None


def get_taxonomy_manager(taxonomy_base_dir: str = "us-gaap-taxonomy") -> TaxonomyLabelManager:
    """Get or create global taxonomy manager instance using only local taxonomy years."""
    global _taxonomy_manager
    if _taxonomy_manager is None or _taxonomy_manager.taxonomy_base_dir != taxonomy_base_dir:
        _taxonomy_manager = TaxonomyLabelManager(taxonomy_base_dir=taxonomy_base_dir)
    return _taxonomy_manager


def lookup_concept_label(concept_name: str) -> str:
    """
    Convenience function to lookup concept label.
    
    Args:
        concept_name: XBRL concept name
        
    Returns:
        Human-readable label
    """
    return get_taxonomy_manager().lookup_label(concept_name)
