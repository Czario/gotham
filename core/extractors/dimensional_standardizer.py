#!/usr/bin/env python3
"""
Utility to standardize dimensional breakdown data structure
"""

from typing import List, Dict, Optional, Union, Any, Sequence
import pandas as pd


def standardize_dimensional_breakdown(dimensional_breakdown: Optional[Sequence[Optional[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """
    Standardize dimensional breakdown data to use consistent field names.
    
    Instead of using:
    - product_id, product_label, product_type
    - segment_id, segment_label, segment_type  
    - consolidation_id, consolidation_label, consolidation_type
    - equity_id, equity_label, equity_type
    
    Use generalized fields:
    - concept, label, segment_type
    
    Args:
        dimensional_breakdown: List of dimensional breakdown items, or None
        
    Returns:
        List of standardized dimensional breakdown items
    """
    if not dimensional_breakdown:
        return []
    
    standardized_breakdown = []
    
    for item in dimensional_breakdown:
        # Skip None or invalid items
        if not item or not isinstance(item, dict):
            continue
            
        standardized_item = {
            'value': item.get('value', 0)
        }
        
        # Find the primary dimension concept and label
        concept = None
        label = None
        segment_type = None
        
        # First, try the new standardized format with 'concept' field
        if 'concept' in item:
            concept = item['concept']
            label = item.get('label')
            segment_type = item.get('segment_type')
        else:
            # Fallback to old format: Look for ID fields (ending with _id)
            id_fields = [k for k in item.keys() if k.endswith('_id')]
            
            if id_fields:
                # Use the first ID field as the primary concept
                id_field = id_fields[0]
                concept = item[id_field]
                
                # Get the corresponding label
                label_field = id_field.replace('_id', '_label')
                label = item.get(label_field)
                
                # If no specific label field found, try generic 'label' field
                if not label:
                    label = item.get('label')
                
                # Get the corresponding type
                type_field = id_field.replace('_id', '_type')
                segment_type = item.get(type_field)
                
                # If no specific type, try to determine from the field name
                if not segment_type:
                    field_prefix = id_field.replace('_id', '')
                    segment_type = field_prefix
        
        # If we still don't have a segment type, look for segment_type
        if not segment_type:
            segment_type = item.get('segment_type', 'other')
        
        # Get the label from the item if not already found
        if not label:
            label = item.get('label')
        
        # Set the standardized fields
        standardized_item['concept'] = concept or 'unknown'
        standardized_item['label'] = label or concept or 'Unknown'
        
        # Improve segment_type if it's set to 'concept' or other non-meaningful values
        if segment_type in ['concept', 'other', 'unknown'] and concept:
            improved_segment_type = _determine_segment_type_from_concept(concept, str(label) if label else "")
            standardized_item['segment_type'] = improved_segment_type
        else:
            standardized_item['segment_type'] = segment_type or 'other'
        
        # Preserve any additional metadata
        for key, value in item.items():
            if not key.endswith('_id') and not key.endswith('_label') and not key.endswith('_type') and key not in ['value', 'segment_type', 'concept', 'label']:
                standardized_item[key] = value
        
        standardized_breakdown.append(standardized_item)
    
    return standardized_breakdown


def migrate_existing_dimensional_data(financial_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Migrate existing financial data to use standardized dimensional breakdown structure.
    
    Args:
        financial_data: List of financial statement items
        
    Returns:
        List of financial statement items with standardized dimensional breakdowns
    """
    migrated_data = []
    
    for item in financial_data:
        migrated_item = item.copy()
        
        if item.get('dimensional_breakdown'):
            migrated_item['dimensional_breakdown'] = standardize_dimensional_breakdown(
                item['dimensional_breakdown']
            )
        
        migrated_data.append(migrated_item)
    
    return migrated_data


def create_dimensional_breakdown_with_standardized_fields(
    value: float,
    dimension_id: str,
    dimension_label: str,
    dimension_type: str,
    additional_metadata: Optional[Dict] = None
) -> Dict:
    """
    Create a standardized dimensional breakdown item.
    
    Args:
        value: The financial value
        dimension_id: The dimension member ID (e.g., "us-gaap:ProductMember")
        dimension_label: The human-readable label (e.g., "Products")
        dimension_type: The dimension type (e.g., "product_service", "business_segment")
        additional_metadata: Optional additional metadata
        
    Returns:
        Standardized dimensional breakdown item
    """
    breakdown_item = {
        'value': value,
        'concept': dimension_id,
        'label': dimension_label,
        'segment_type': dimension_type
    }
    
    if additional_metadata:
        breakdown_item.update(additional_metadata)
    
    return breakdown_item


def validate_standardized_dimensional_breakdown(dimensional_breakdown: List[Dict[str, Any]]) -> List[str]:
    """
    Validate that dimensional breakdown follows the standardized structure.
    
    Args:
        dimensional_breakdown: List of dimensional breakdown items
        
    Returns:
        List of validation errors (empty if valid)
    """
    errors = []
    
    if not dimensional_breakdown:
        return errors
    
    required_fields = ['value', 'concept', 'label', 'segment_type']
    
    for i, item in enumerate(dimensional_breakdown):
        for field in required_fields:
            if field not in item:
                errors.append(f"Item {i}: Missing required field '{field}'")
            elif item[field] is None:
                errors.append(f"Item {i}: Field '{field}' is None")
    
    return errors


def _determine_segment_type_from_concept(concept: Optional[str], label: Optional[str] = "") -> str:
    """
    Determine the segment type from a concept ID and label.
    
    Args:
        concept: The concept ID (e.g., "aapl:IPhoneMember") or None
        label: Optional human-readable label
        
    Returns:
        String representing the segment type
    """
    if not concept:
        return 'other'
        
    # concept is guaranteed to be a string here
    concept_lower = concept.lower()
    label_lower = str(label).lower() if label else ""
    
    # 1. CONSOLIDATION indicators (highest priority - these are structural and specific)
    if any(cons in concept_lower for cons in ['consolidation', 'elimination', 'reconciling', 'materialreconcilingitems', 'corporate', 'unallocated']):
        return 'consolidation'
    elif any(cons in label_lower for cons in ['consolidation', 'elimination', 'reconciling', 'corporate', 'unallocated']):
        return 'consolidation'
        
    # 2. GEOGRAPHIC indicators (high priority for clear geographic patterns)
    elif concept_lower.startswith('country:'):
        return 'geographic'
    elif any(geo in concept_lower for geo in ['geographic', 'region', 'americas', 'europe', 'asia', 'china', 'japan', 'pacific', 'othercountries']):
        return 'geographic'
    elif any(geo in label_lower for geo in ['us', 'united states', 'china', 'europe', 'asia', 'america', 'japan', 'country', 'region', 'countries']):
        return 'geographic'
        
    # 3. EQUITY indicators (high priority - these are structural)
    elif any(eq in concept_lower for eq in ['equity', 'stock', 'shares', 'retained', 'earnings', 'retainedearningsmember', 'commonstockmember']):
        return 'equity'
    elif any(eq in label_lower for eq in ['equity', 'stock', 'shares', 'retained', 'earnings']):
        return 'equity'
        
    # 4. BUSINESS SEGMENT indicators (explicit segment keywords)
    elif any(seg in concept_lower for seg in ['segmentmember', 'divisonmember', 'operatingsegmentsmember']) or concept_lower.endswith('segmentmember'):
        return 'business_segment'
    elif any(seg in label_lower for seg in ['segment', 'division', 'operating segments']):
        return 'business_segment'
        
    # 5. PRODUCT/SERVICE indicators (specific product/service patterns)
    elif any(prod in concept_lower for prod in ['productmember', 'servicemember', 'salesmember', 'leasingmember']):
        return 'product_service'
    elif any(prod in label_lower for prod in ['product', 'service', 'sales', 'leasing']):
        return 'product_service'
    # Industry-specific product/service patterns
    elif any(industry_prod in concept_lower for industry_prod in ['automotive', 'energy']):
        return 'product_service'
    elif any(industry_prod in label_lower for industry_prod in ['automotive', 'energy']):
        return 'product_service'
        
    # 6. GENERIC MEMBER classification (fallback for members that don't fit clear categories)
    # This is more conservative - only if it clearly looks like a product/service and not a segment
    elif ('member' in concept_lower and 
          not any(bus_term in concept_lower for bus_term in ['business', 'operating']) and
          not any(bus_term in label_lower for bus_term in ['business', 'operating'])):
        return 'product_service'
        
    # 7. DEFAULT for anything else
    else:
        return 'other'
