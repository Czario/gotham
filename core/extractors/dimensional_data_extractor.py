#!/usr/bin/env python3
"""
Enhanced dimensional extraction improvements for XBRL processing.
This module provides specific enhancements to capture more dimensional data.
"""

from arelle import XbrlConst
from collections import defaultdict
from typing import Dict, List, Set, Any, Optional
from utilities.helpers.logger_config import get_module_logger

logger = get_module_logger(__name__)

class EnhancedDimensionalExtractor:
    """Enhanced dimensional extraction capabilities"""
    
    def __init__(self, xbrl_extractor):
        self.extractor = xbrl_extractor
        self.dimensional_structure = None
        self.missing_dimensional_contexts = []
        
    def discover_dimensional_relationships(self, modelXbrl) -> Dict[str, Any]:
        """
        Discover dimensional relationships from DTS for comprehensive coverage.
        This uses Arelle's relationship sets to find all available dimensional structure.
        """
        logger.debug("Discovering dimensional relationships from DTS...")
        
        dimensional_structure = {
            'dimensions': set(),
            'domains': defaultdict(set),
            'members': defaultdict(set),
            'defaults': {},
            'hypercubes': defaultdict(set),
            'primary_items': defaultdict(set),
            'typed_dimensions': set(),
            'explicit_dimensions': set()
        }
        
        try:
            logger.debug("Checking relationship sets...")
            
            # 1. Dimension-Domain relationships
            dim_domain_rels = modelXbrl.relationshipSet(XbrlConst.dimensionDomain)
            logger.debug(f"   dimensionDomain relationships: {len(dim_domain_rels.modelRelationships) if dim_domain_rels else 0}")
            if dim_domain_rels and dim_domain_rels.modelRelationships:
                for rel in dim_domain_rels.modelRelationships:
                    if rel.fromModelObject is not None and rel.toModelObject is not None:
                        dim_qname = str(rel.fromModelObject.qname)
                        domain_qname = str(rel.toModelObject.qname)
                        dimensional_structure['dimensions'].add(dim_qname)
                        dimensional_structure['domains'][dim_qname].add(domain_qname)
                        
                        # Check if it's typed or explicit
                        if hasattr(rel.fromModelObject, 'typedDomainElement') and rel.fromModelObject.typedDomainElement:
                            dimensional_structure['typed_dimensions'].add(dim_qname)
                        else:
                            dimensional_structure['explicit_dimensions'].add(dim_qname)
            
            # 2. Domain-Member relationships
            domain_member_rels = modelXbrl.relationshipSet(XbrlConst.domainMember)
            logger.debug(f"   domainMember relationships: {len(domain_member_rels.modelRelationships) if domain_member_rels else 0}")
            if domain_member_rels and domain_member_rels.modelRelationships:
                for rel in domain_member_rels.modelRelationships:
                    if rel.fromModelObject is not None and rel.toModelObject is not None:
                        domain_qname = str(rel.fromModelObject.qname)
                        member_qname = str(rel.toModelObject.qname)
                        dimensional_structure['members'][domain_qname].add(member_qname)
            
            # 3. Hypercube-Dimension relationships
            hc_dim_rels = modelXbrl.relationshipSet(XbrlConst.hypercubeDimension)
            logger.debug(f"   hypercubeDimension relationships: {len(hc_dim_rels.modelRelationships) if hc_dim_rels else 0}")
            if hc_dim_rels and hc_dim_rels.modelRelationships:
                for rel in hc_dim_rels.modelRelationships:
                    if rel.fromModelObject is not None and rel.toModelObject is not None:
                        hc_qname = str(rel.fromModelObject.qname)
                        dim_qname = str(rel.toModelObject.qname)
                        dimensional_structure['hypercubes'][hc_qname].add(dim_qname)
            
            # 4. All relationships (primary items to hypercubes)
            all_rels = modelXbrl.relationshipSet(XbrlConst.all)
            logger.debug(f"   all relationships: {len(all_rels.modelRelationships) if all_rels else 0}")
            if all_rels and all_rels.modelRelationships:
                for rel in all_rels.modelRelationships:
                    if rel.fromModelObject is not None and rel.toModelObject is not None:
                        primary_qname = str(rel.fromModelObject.qname)
                        hc_qname = str(rel.toModelObject.qname)
                        dimensional_structure['primary_items'][primary_qname].add(hc_qname)
            
            # 5. Dimension defaults
            dim_default_rels = modelXbrl.relationshipSet(XbrlConst.dimensionDefault)
            logger.debug(f"   dimensionDefault relationships: {len(dim_default_rels.modelRelationships) if dim_default_rels else 0}")
            if dim_default_rels and dim_default_rels.modelRelationships:
                for rel in dim_default_rels.modelRelationships:
                    if rel.fromModelObject is not None and rel.toModelObject is not None:
                        dim_qname = str(rel.fromModelObject.qname)
                        default_member = str(rel.toModelObject.qname)
                        dimensional_structure['defaults'][dim_qname] = default_member
            
            # 6. ADDITIONAL: Extract dimensional info from actual fact contexts
            logger.debug("Analyzing actual fact contexts for dimensional information...")
            context_dimensions = set()
            context_members = set()
            
            for fact in modelXbrl.facts:
                if (fact.context is not None and 
                    hasattr(fact.context, 'qnameDims') and 
                    fact.context.qnameDims is not None and 
                    len(fact.context.qnameDims) > 0):
                    
                    for dim_qname, dim_value in fact.context.qnameDims.items():
                        dim_str = str(dim_qname)
                        context_dimensions.add(dim_str)
                        dimensional_structure['dimensions'].add(dim_str)
                        
                        # Extract explicit dimension members
                        if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit:
                            if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                                member_str = str(dim_value.memberQname)
                                context_members.add(member_str)
                                dimensional_structure['members'][dim_str].add(member_str)
                                dimensional_structure['explicit_dimensions'].add(dim_str)
                        
                        # Extract typed dimension values
                        elif hasattr(dim_value, 'isTyped') and dim_value.isTyped:
                            dimensional_structure['typed_dimensions'].add(dim_str)
            
            logger.debug(f"   Found {len(context_dimensions)} dimensions from fact contexts")
            logger.debug(f"   Found {len(context_members)} members from fact contexts")
            
            # Convert sets to lists for JSON serialization
            for key, value in dimensional_structure.items():
                if isinstance(value, set):
                    dimensional_structure[key] = list(value)
                elif isinstance(value, defaultdict):
                    dimensional_structure[key] = {k: list(v) for k, v in value.items()}
            
            self.dimensional_structure = dimensional_structure
            
            logger.debug(f"Discovered dimensional relationships:")
            logger.debug(f"   {len(dimensional_structure['dimensions'])} dimensions")
            logger.debug(f"   {len(dimensional_structure['domains'])} domain relationships") 
            logger.debug(f"   {sum(len(members) for members in dimensional_structure['members'].values())} total members")
            logger.debug(f"   {len(dimensional_structure['hypercubes'])} hypercubes")
            logger.debug(f"   {len(dimensional_structure['defaults'])} dimension defaults")
            
            return dimensional_structure
            
        except Exception as e:
            logger.debug(f"Error discovering dimensional relationships: {e}")
            import traceback
            traceback.print_exc()
            return dimensional_structure
    
    def enhanced_dimensional_fact_extraction(self, facts, modelXbrl) -> List[Dict]:
        """
        Enhanced dimensional fact extraction that uses relationship discovery
        to find additional dimensional data that might be missed.
        
        The system captures ALL dimensional data, including concepts
        that don't appear in standard presentation relationships.
        """
        logger.debug("Enhanced dimensional fact extraction...")
        
        # First discover the dimensional structure
        if not self.dimensional_structure:
            self.discover_dimensional_relationships(modelXbrl)
        
        enhanced_facts = []
        potential_dimensional_facts = 0
        
        # STEP 1: Process provided facts (from presentation relationships)
        for fact in facts:
            try:
                # Check if this fact already has dimensions
                has_existing_dimensions = (fact.context is not None and hasattr(fact.context, 'qnameDims') and 
                                         fact.context.qnameDims is not None and len(fact.context.qnameDims) > 0)
                
                if has_existing_dimensions:
                    # Use enhanced dimensional extraction for existing dimensional facts
                    enhanced_fact = self._extract_enhanced_dimensional_fact(fact)
                    if enhanced_fact:
                        enhanced_facts.append(enhanced_fact)
                else:
                    # Check if it should have dimensional context based on relationships or segment/scenario
                    enhanced_fact = self._check_for_missing_dimensional_context(fact, modelXbrl)
                    if enhanced_fact:
                        enhanced_facts.append(enhanced_fact)
                        potential_dimensional_facts += 1
                        
            except Exception as e:
                logger.debug(f"Error in enhanced extraction for fact {getattr(fact, 'contextID', 'unknown')}: {e}")
                continue
        
        # STEP 2: Discover ALL concepts with dimensional data that don't exist in standard presentation
        logger.debug("Discovering concepts not in standard presentation relationships...")
        missing_dimensional_concepts = self._discover_missing_dimensional_concepts(modelXbrl, facts)
        
        # STEP 3: Extract facts for missing dimensional concepts
        for concept_qname, concept_facts in missing_dimensional_concepts.items():
            try:
                for fact in concept_facts:
                    enhanced_fact = self._extract_enhanced_dimensional_fact(fact)
                    if enhanced_fact:
                        enhanced_fact['missing_from_presentation'] = True
                        enhanced_fact['discovered_concept'] = True
                        enhanced_facts.append(enhanced_fact)
                        potential_dimensional_facts += 1
            except Exception as e:
                logger.debug(f"Error extracting missing dimensional concept {concept_qname}: {e}")
                continue
        
        if potential_dimensional_facts > 0:
            logger.debug(f"Enhanced extraction found {potential_dimensional_facts} additional potential dimensional facts")
            logger.debug(f"Discovered {len(missing_dimensional_concepts)} concepts not in standard presentation")
        
        return enhanced_facts
    
    def _extract_enhanced_dimensional_fact(self, fact) -> Optional[Dict]:
        """
        Extract dimensional fact with enhanced methods
        """
        if fact.context is None or not hasattr(fact.context, 'qnameDims') or fact.context.qnameDims is None:
            return None
            
        try:
            # Enhanced value extraction
            value = None
            raw_value = None
            
            # Try multiple value extraction methods
            if hasattr(fact, 'xValue') and fact.xValue is not None:
                raw_value = fact.xValue
            elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                raw_value = fact.effectiveValue
            elif hasattr(fact, 'value') and fact.value is not None:
                raw_value = fact.value
            
            # Parse the raw value
            if raw_value is not None:
                if isinstance(raw_value, (int, float)):
                    value = float(raw_value)
                else:
                    try:
                        # Handle comma-separated numbers and various formats
                        clean_value = str(raw_value).replace(',', '').replace('$', '').strip()
                        if clean_value and clean_value not in ['', '-', 'N/A', 'n/a']:
                            value = float(clean_value)
                    except (ValueError, TypeError):
                        value = None
            
            # Enhanced dimensional context extraction
            dimensions = {}
            dimension_details = {}
            
            for dim_qname, dim_value in fact.context.qnameDims.items():
                axis_local_name = dim_qname.localName if hasattr(dim_qname, 'localName') else str(dim_qname).split(':')[-1]
                
                # Extract explicit dimension members with enhanced label resolution
                if hasattr(dim_value, 'isExplicit') and dim_value.isExplicit:
                    if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                        member_qname = dim_value.memberQname
                        member_name = member_qname.localName if hasattr(member_qname, 'localName') else str(member_qname).split(':')[-1]
                        
                        dimensions[axis_local_name] = member_name
                        dimension_details[axis_local_name] = {
                            'type': 'explicit_enhanced',
                            'axis_qname': str(dim_qname),
                            'member_qname': str(member_qname),
                            'member_local_name': member_name,
                            'axis_local_name': axis_local_name,
                            'axis_namespace': dim_qname.namespaceURI if hasattr(dim_qname, 'namespaceURI') else None,
                            'member_namespace': member_qname.namespaceURI if hasattr(member_qname, 'namespaceURI') else None,
                            'source': 'enhanced_extraction'
                        }
                
                # Enhanced typed dimension extraction
                elif hasattr(dim_value, 'isTyped') and dim_value.isTyped:
                    if hasattr(dim_value, 'typedMember') and dim_value.typedMember is not None:
                        typed_value = None
                        
                        # Try multiple extraction methods
                        for attr_name in ['textValue', 'stringValue', 'xValue', 'text', 'value']:
                            try:
                                attr_value = getattr(dim_value.typedMember, attr_name, None)
                                if attr_value is not None:
                                    typed_value = str(attr_value)
                                    break
                            except:
                                continue
                        
                        # Fallback to string conversion
                        if not typed_value:
                            typed_value = str(dim_value.typedMember)
                        
                        if typed_value and typed_value not in ['None', '']:
                            dimensions[axis_local_name] = typed_value
                            dimension_details[axis_local_name] = {
                                'type': 'typed_enhanced',
                                'axis_qname': str(dim_qname),
                                'typed_value': typed_value,
                                'axis_local_name': axis_local_name,
                                'axis_namespace': dim_qname.namespaceURI if hasattr(dim_qname, 'namespaceURI') else None,
                                'source': 'enhanced_extraction'
                            }
            
            # Enhanced period information
            period_info = ""
            period_type = None
            if fact.context is not None and hasattr(fact.context, 'period'):
                if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                    period_type = "instant"
                    period_info = str(fact.context.instantDatetime) if fact.context.instantDatetime else ""
                elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                    period_type = "duration"
                    start_date = str(fact.context.startDatetime) if fact.context.startDatetime else ""
                    end_date = str(fact.context.endDatetime) if fact.context.endDatetime else ""
                    period_info = f"{start_date} to {end_date}"
                elif hasattr(fact.context, 'isForeverPeriod') and fact.context.isForeverPeriod:
                    period_type = "forever"
                    period_info = "forever"
            
            # Enhanced entity information
            entity_info = {}
            if fact.context is not None and hasattr(fact.context, 'entityIdentifier'):
                entity_info = {
                    'scheme': fact.context.entityIdentifier[0] if len(fact.context.entityIdentifier) > 0 else 'http://www.sec.gov/CIK',
                    'identifier': fact.context.entityIdentifier[1] if len(fact.context.entityIdentifier) > 1 else None
                }
            
            # Enhanced unit information
            unit_info = {}
            if fact.unit is not None:
                unit_info = {
                    'id': fact.unitID,
                    'measures': []
                }
                
                if hasattr(fact.unit, 'measures'):
                    multiply_measures, divide_measures = fact.unit.measures
                    
                    if multiply_measures:
                        for measure in multiply_measures:
                            unit_info['measures'].append(str(measure))
                    
                    if divide_measures:
                        for measure in divide_measures:
                            unit_info['measures'].append(str(measure))
                
                if not unit_info['measures'] and hasattr(fact.unit, 'value'):
                    unit_info['measures'].append(str(fact.unit.value))
            
            return {
                'value': value,
                'dimensions': dimensions,
                'dimension_details': dimension_details,
                'context_id': fact.contextID,
                'unit_id': fact.unitID if fact.unit is not None else None,
                'unit_info': unit_info,
                'period': period_info,
                'period_type': period_type,
                'entity_info': entity_info,
                'concept_name': str(fact.concept.qname) if fact.concept is not None else None,
                'concept_local_name': fact.concept.qname.localName if (fact.concept is not None and hasattr(fact.concept.qname, 'localName')) else None,
                'has_dimensions': True,
                'dimension_count': len(dimensions),
                'fact_id': getattr(fact, 'id', None),
                'decimals': getattr(fact, 'decimals', None),
                'precision': getattr(fact, 'precision', None),
                'source': 'enhanced_dimensional_extraction'
            }
            
        except Exception as e:
            logger.debug(f"Error extracting enhanced dimensional fact: {e}")
            return None

    def _extract_simple_fact(self, fact) -> Optional[Dict]:
        """
        Extract a numeric fact that has NO dimensional context.

        Mirrors the dict shape returned by ``_extract_enhanced_dimensional_fact``
        (empty ``dimensions``/``dimension_details``) so that non-dimensional
        facts discovered outside the presentation tree can flow through the same
        line-item creation path.
        """
        if fact is None or fact.context is None:
            return None

        try:
            # Numeric value extraction (matches enhanced extractor logic)
            value = None
            raw_value = None
            if hasattr(fact, 'xValue') and fact.xValue is not None:
                raw_value = fact.xValue
            elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                raw_value = fact.effectiveValue
            elif hasattr(fact, 'value') and fact.value is not None:
                raw_value = fact.value

            if raw_value is not None:
                if isinstance(raw_value, (int, float)):
                    value = float(raw_value)
                else:
                    try:
                        clean_value = str(raw_value).replace(',', '').replace('$', '').strip()
                        if clean_value and clean_value not in ['', '-', 'N/A', 'n/a']:
                            value = float(clean_value)
                    except (ValueError, TypeError):
                        value = None

            # Non-dimensional facts with no numeric value carry no information
            if value is None:
                return None

            # Period information
            period_info = ""
            period_type = None
            if hasattr(fact.context, 'period'):
                if hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                    period_type = "instant"
                    period_info = str(fact.context.instantDatetime) if fact.context.instantDatetime else ""
                elif hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                    period_type = "duration"
                    start_date = str(fact.context.startDatetime) if fact.context.startDatetime else ""
                    end_date = str(fact.context.endDatetime) if fact.context.endDatetime else ""
                    period_info = f"{start_date} to {end_date}"
                elif hasattr(fact.context, 'isForeverPeriod') and fact.context.isForeverPeriod:
                    period_type = "forever"
                    period_info = "forever"

            entity_info = {}
            if hasattr(fact.context, 'entityIdentifier'):
                entity_info = {
                    'scheme': fact.context.entityIdentifier[0] if len(fact.context.entityIdentifier) > 0 else 'http://www.sec.gov/CIK',
                    'identifier': fact.context.entityIdentifier[1] if len(fact.context.entityIdentifier) > 1 else None
                }

            unit_info = {}
            if fact.unit is not None:
                unit_info = {'id': fact.unitID, 'measures': []}
                if hasattr(fact.unit, 'measures'):
                    multiply_measures, divide_measures = fact.unit.measures
                    for measure in (multiply_measures or []):
                        unit_info['measures'].append(str(measure))
                    for measure in (divide_measures or []):
                        unit_info['measures'].append(str(measure))

            return {
                'value': value,
                'dimensions': {},
                'dimension_details': {},
                'context_id': fact.contextID,
                'unit_id': fact.unitID if fact.unit is not None else None,
                'unit_info': unit_info,
                'period': period_info,
                'period_type': period_type,
                'entity_info': entity_info,
                'concept_name': str(fact.concept.qname) if fact.concept is not None else None,
                'concept_local_name': fact.concept.qname.localName if (fact.concept is not None and hasattr(fact.concept.qname, 'localName')) else None,
                'has_dimensions': False,
                'dimension_count': 0,
                'fact_id': getattr(fact, 'id', None),
                'decimals': getattr(fact, 'decimals', None),
                'precision': getattr(fact, 'precision', None),
                'source': 'non_dimensional_discovery'
            }

        except Exception as e:
            logger.debug(f"Error extracting non-dimensional fact: {e}")
            return None

    def _check_for_missing_dimensional_context(self, fact, modelXbrl) -> Optional[Dict]:
        """
        Check if a fact should have dimensional context based on the discovered
        dimensional relationships, segment/scenario elements, even if it doesn't have qnameDims.
        """
        if fact.concept is None or fact.context is None:
            return None
            
        concept_qname = str(fact.concept.qname)
        
        # 1. Check segment and scenario elements for dimensional information
        segment_scenario_dimensions = {}
        segment_scenario_details = {}
        
        # Extract from segment
        if hasattr(fact.context, 'segment') and fact.context.segment is not None:
            for element in fact.context.segment:
                if hasattr(element, 'tag') and hasattr(element, 'text'):
                    tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag
                    element_value = element.text if element.text else None
                    
                    if (element_value and 
                        any(keyword in tag_name.lower() for keyword in 
                            ['axis', 'member', 'dimension', 'segment', 'product', 'geographic', 'business'])):
                        
                        segment_scenario_dimensions[tag_name] = element_value
                        segment_scenario_details[tag_name] = {
                            'type': 'segment_element',
                            'axis_qname': element.tag,
                            'member': element_value,
                            'axis_local_name': tag_name,
                            'source': 'context_segment'
                        }
        
        # Extract from scenario
        if hasattr(fact.context, 'scenario') and fact.context.scenario is not None:
            for element in fact.context.scenario:
                if hasattr(element, 'tag') and hasattr(element, 'text'):
                    tag_name = element.tag.split('}')[-1] if '}' in element.tag else element.tag
                    element_value = element.text if element.text else None
                    
                    if (element_value and 
                        any(keyword in tag_name.lower() for keyword in 
                            ['axis', 'member', 'dimension', 'scenario', 'product', 'geographic', 'business'])):
                        
                        if tag_name not in segment_scenario_dimensions:  # Don't override segment
                            segment_scenario_dimensions[tag_name] = element_value
                            segment_scenario_details[tag_name] = {
                                'type': 'scenario_element',
                                'axis_qname': element.tag,
                                'member': element_value,
                                'axis_local_name': tag_name,
                                'source': 'context_scenario'
                            }
        
        # 2. Check if this concept is a primary item that should have dimensional context
        dimensional_structure_dimensions = {}
        dimensional_structure_details = {}
        
        if self.dimensional_structure and concept_qname in self.dimensional_structure.get('primary_items', {}):
            hypercubes = self.dimensional_structure['primary_items'][concept_qname]
            
            # This concept is related to hypercubes, so it might have dimensional context
            # Check if there are default dimensional members that should be applied
            for hc_qname in hypercubes:
                # Get dimensions for this hypercube
                if self.dimensional_structure and 'hypercubes' in self.dimensional_structure:
                    hc_dimensions = self.dimensional_structure.get('hypercubes', {}).get(hc_qname, [])
                else:
                    continue
                
                for dim_qname in hc_dimensions:
                    # Check if there's a default member for this dimension
                    if (self.dimensional_structure and 
                        'defaults' in self.dimensional_structure and 
                        dim_qname in self.dimensional_structure['defaults']):
                        default_member = self.dimensional_structure['defaults'][dim_qname]
                        
                        # Parse dimension and member names
                        dim_local_name = dim_qname.split(':')[-1] if ':' in dim_qname else dim_qname
                        member_local_name = default_member.split(':')[-1] if ':' in default_member else default_member
                        
                        dimensional_structure_dimensions[dim_local_name] = member_local_name
                        dimensional_structure_details[dim_local_name] = {
                            'type': 'explicit_default',
                            'axis_qname': dim_qname,
                            'member_qname': default_member,
                            'member_local_name': member_local_name,
                            'axis_local_name': dim_local_name,
                            'member_label': f"Default {member_local_name}",
                            'is_default': True,
                            'source': 'dimensional_structure'
                        }
        
        # 3. Combine all found dimensions
        all_dimensions = {}
        all_dimension_details = {}
        
        # Prioritize: segment/scenario elements > dimensional structure
        all_dimensions.update(dimensional_structure_dimensions)
        all_dimension_details.update(dimensional_structure_details)
        all_dimensions.update(segment_scenario_dimensions)  # Override with segment/scenario
        all_dimension_details.update(segment_scenario_details)
        
        # 4. If we found any dimensional information, create a dimensional fact
        if all_dimensions:
            # Create a potential dimensional fact with discovered dimensional information
            return {
                'value': self._extract_fact_value(fact),
                'dimensions': all_dimensions,
                'dimension_details': all_dimension_details,
                'context_id': fact.contextID,
                'unit_id': fact.unitID if fact.unit else None,
                'period': self._extract_period_info(fact.context),
                'concept_name': concept_qname,
                'concept_local_name': fact.concept.qname.localName if (fact.concept and hasattr(fact.concept.qname, 'localName')) else None,
                'fact_label': self.extractor._get_concept_label(fact.concept.qname) if fact.concept else None,
                'has_dimensions': True,
                'dimension_count': len(all_dimensions),
                'extraction_note': 'Enhanced extraction with segment/scenario/default dimensional members',
                'source': 'enhanced_missing_context_discovery'
            }
        
        return None
    
    def _extract_fact_value(self, fact):
        """Extract fact value using multiple approaches"""
        try:
            if hasattr(fact, 'xValue') and fact.xValue is not None:
                return float(fact.xValue) if isinstance(fact.xValue, (int, float)) else fact.xValue
            elif hasattr(fact, 'effectiveValue') and fact.effectiveValue is not None:
                return float(fact.effectiveValue) if isinstance(fact.effectiveValue, (int, float)) else fact.effectiveValue
            elif hasattr(fact, 'value') and fact.value is not None:
                return float(fact.value) if isinstance(fact.value, (int, float)) else fact.value
            return None
        except (ValueError, TypeError):
            return str(fact.value) if hasattr(fact, 'value') else None
    
    @staticmethod
    def _is_capturable_numeric_concept(fact) -> bool:
        """
        Decide whether a non-dimensional fact is worth capturing outside the
        presentation tree. We only want genuine numeric financial facts and must
        exclude entity/cover-page metadata (``dei`` namespace) and non-numeric
        text blocks, which would otherwise add noise.
        """
        concept = getattr(fact, 'concept', None)
        if concept is None or not hasattr(concept, 'qname'):
            return False

        # Must be numeric
        is_numeric = getattr(concept, 'isNumeric', None)
        if is_numeric is None:
            # Fall back to checking the fact value can be parsed as a number later
            is_numeric = not getattr(concept, 'isTextBlock', False)
        if not is_numeric:
            return False

        # Skip abstract concepts and DEI / cover-page metadata
        if getattr(concept, 'isAbstract', False):
            return False
        namespace = getattr(concept.qname, 'namespaceURI', '') or ''
        if 'dei' in namespace.lower():
            return False

        # Skip nil facts
        if getattr(fact, 'isNil', False):
            return False

        return True

    def _discover_missing_dimensional_concepts(self, modelXbrl, presentation_facts,
                                               include_non_dimensional: bool = True) -> Dict[str, List]:
        """
        Discover concepts that don't appear in standard presentation relationships.

        Captures two classes of otherwise-dropped facts:
          1. Concepts carrying dimensional data (segment/product/geography breakdowns).
          2. (when ``include_non_dimensional`` is True) plain numeric facts that
             exist in the instance document but were never wired into any
             presentation linkbase — these are real reported values the standard
             pass silently misses.
        """
        logger.debug("Discovering missing dimensional concepts...")
        logger.debug(f"Input presentation_facts count: {len(presentation_facts)}")
        
        # Get all concepts that appear in the provided presentation facts
        presentation_concept_qnames = set()
        facts_without_concept = 0
        facts_without_qname = 0
        
        for fact in presentation_facts:
            if not hasattr(fact, 'concept') or fact.concept is None:
                facts_without_concept += 1
                continue
            if not hasattr(fact.concept, 'qname'):
                facts_without_qname += 1
                continue
            presentation_concept_qnames.add(str(fact.concept.qname))
        
        logger.debug(f"Debug: Facts without concept: {facts_without_concept}")
        logger.debug(f"Debug: Facts without qname: {facts_without_qname}")
        logger.debug(f"Debug: Found {len(presentation_concept_qnames)} unique concepts from presentation facts")
        
        # Find ALL facts in the modelXbrl that have dimensional context
        all_dimensional_facts = []
        missing_dimensional_concepts = {}
        
        logger.debug(f"Scanning {len(modelXbrl.facts)} total facts for dimensional data...")
        
        dimensional_fact_count = 0
        missing_concept_count = 0
        non_dimensional_fact_count = 0

        for fact in modelXbrl.facts:
            try:
                # Check if this fact has dimensional context
                if (fact.context is not None and hasattr(fact.context, 'qnameDims') and 
                    fact.context.qnameDims is not None and len(fact.context.qnameDims) > 0):
                    
                    dimensional_fact_count += 1
                    
                    # Get the concept name
                    if fact.concept is not None and hasattr(fact.concept, 'qname'):
                        concept_qname = str(fact.concept.qname)
                        
                        # Check if this concept is NOT in the standard presentation relationships
                        if concept_qname not in presentation_concept_qnames:
                            if concept_qname not in missing_dimensional_concepts:
                                missing_dimensional_concepts[concept_qname] = []
                                missing_concept_count += 1
                            
                            missing_dimensional_concepts[concept_qname].append(fact)

                # Non-dimensional numeric facts outside the presentation tree
                elif include_non_dimensional:
                    if (fact.concept is not None and hasattr(fact.concept, 'qname') and
                            self._is_capturable_numeric_concept(fact)):
                        concept_qname = str(fact.concept.qname)
                        if concept_qname not in presentation_concept_qnames:
                            if concept_qname not in missing_dimensional_concepts:
                                missing_dimensional_concepts[concept_qname] = []
                                missing_concept_count += 1
                            missing_dimensional_concepts[concept_qname].append(fact)
                            non_dimensional_fact_count += 1

            except Exception as e:
                logger.debug(f"Error analyzing fact for missing concepts: {e}")
                continue
        
        logger.debug(f"Analysis complete:")
        logger.debug(f"   {dimensional_fact_count} total facts with dimensional data")
        logger.debug(f"   {non_dimensional_fact_count} non-dimensional numeric facts captured outside presentation")
        logger.debug(f"   {len(presentation_concept_qnames)} concepts in standard presentation")
        logger.debug(f"   {len(missing_dimensional_concepts)} concepts NOT in presentation")
        logger.debug(f"   {sum(len(facts) for facts in missing_dimensional_concepts.values())} dimensional facts from missing concepts")
        
        # Log some examples of missing concepts for debugging
        if missing_dimensional_concepts:
            logger.debug("Examples of missing dimensional concepts:")
            for i, (concept_qname, facts_list) in enumerate(list(missing_dimensional_concepts.items())[:5]):
                concept_local_name = concept_qname.split(':')[-1] if ':' in concept_qname else concept_qname
                logger.debug(f"   • {concept_local_name} ({len(facts_list)} dimensional facts)")
                
                # Show sample dimensional context (removed verbose output)
                # Dimensional context details only in debug logs
        
        return missing_dimensional_concepts

    def _extract_period_info(self, context):
        """Extract period information from context"""
        if not context or not hasattr(context, 'period'):
            return ""
        
        try:
            if hasattr(context, 'isInstantPeriod') and context.isInstantPeriod:
                return str(context.instantDatetime) if context.instantDatetime else ""
            elif hasattr(context, 'isStartEndPeriod') and context.isStartEndPeriod:
                start_date = str(context.startDatetime) if context.startDatetime else ""
                end_date = str(context.endDatetime) if context.endDatetime else ""
                return f"{start_date} to {end_date}"
            return ""
        except Exception:
            return ""
    
    def analyze_missing_dimensional_opportunities(self, facts, modelXbrl) -> Dict[str, Any]:
        """
        Analyze what dimensional opportunities are being missed
        """
        logger.debug("Analyzing missing dimensional opportunities...")
        
        if not self.dimensional_structure:
            self.discover_dimensional_relationships(modelXbrl)
        
        analysis = {
            'total_facts': len(facts),
            'facts_with_qname_dims': 0,
            'facts_with_potential_dims': 0,
            'concepts_with_dimensional_potential': set(),
            'missing_dimension_applications': [],
            'unused_dimensional_structure': {
                'unused_dimensions': set(),
                'unused_members': set(),
                'unused_defaults': set()
            }
        }
        
        # Track which dimensional elements are actually used
        used_dimensions = set()
        used_members = set()
        used_defaults = set()
        
        for fact in facts:
            if fact.concept is None:
                continue
                
            concept_qname = str(fact.concept.qname)
            
            # Check if fact has qnameDims
            if (fact.context is not None and hasattr(fact.context, 'qnameDims') and 
                fact.context.qnameDims is not None):
                analysis['facts_with_qname_dims'] += 1
                
                # Track used dimensional elements
                for dim_qname, dim_value in fact.context.qnameDims.items():
                    used_dimensions.add(str(dim_qname))
                    if hasattr(dim_value, 'memberQname') and dim_value.memberQname:
                        used_members.add(str(dim_value.memberQname))
            
            # Check if this concept should have dimensional context based on relationships
            elif (self.dimensional_structure and 
                  'primary_items' in self.dimensional_structure and
                  concept_qname in self.dimensional_structure['primary_items']):
                analysis['facts_with_potential_dims'] += 1
                analysis['concepts_with_dimensional_potential'].add(concept_qname)
                
                # This is a missing dimensional application
                analysis['missing_dimension_applications'].append({
                    'concept': concept_qname,
                    'context_id': fact.contextID,
                    'potential_hypercubes': self.dimensional_structure['primary_items'][concept_qname]
                })
        
        # Identify unused dimensional structure
        if self.dimensional_structure:
            all_dimensions = set(self.dimensional_structure.get('dimensions', []))
            all_members = set()
            for members_list in self.dimensional_structure.get('members', {}).values():
                all_members.update(members_list)
            all_defaults = set(self.dimensional_structure.get('defaults', {}).keys())
        else:
            all_dimensions = set()
            all_members = set()
            all_defaults = set()
        
        analysis['unused_dimensional_structure'] = {
            'unused_dimensions': list(all_dimensions - used_dimensions),
            'unused_members': list(all_members - used_members),
            'unused_defaults': list(all_defaults - used_defaults)
        }
        
        # Convert sets to lists for JSON serialization
        analysis['concepts_with_dimensional_potential'] = list(analysis['concepts_with_dimensional_potential'])
        
        logger.debug(f"Missing dimensional opportunities analysis:")
        logger.debug(f"   {analysis['facts_with_qname_dims']} facts have explicit dimensional context")
        logger.debug(f"   {analysis['facts_with_potential_dims']} facts have potential dimensional context")
        logger.debug(f"   {len(analysis['missing_dimension_applications'])} missing dimensional applications")
        logger.debug(f"   {len(analysis['unused_dimensional_structure']['unused_dimensions'])} unused dimensions")
        logger.debug(f"   {len(analysis['unused_dimensional_structure']['unused_members'])} unused members")
        
        return analysis

    def _create_line_items_for_missing_concepts(self, modelXbrl, presentation_facts) -> List[Dict]:
        """
        Create line items for concepts that have dimensional data but don't appear 
        in standard presentation relationships. This ensures ALL dimensional data is captured.
        """
        logger.debug("Creating line items for missing dimensional concepts...")
        
        # Discover missing dimensional concepts
        missing_dimensional_concepts = self._discover_missing_dimensional_concepts(modelXbrl, presentation_facts)
        
        line_items = []
        
        for concept_qname, concept_facts in missing_dimensional_concepts.items():
            try:
                # Get concept information
                concept_local_name = concept_qname.split(':')[-1] if ':' in concept_qname else concept_qname
                
                # Try to get the concept object for better label extraction
                concept_obj = None
                if hasattr(modelXbrl, 'qnameConcepts'):
                    # Convert string back to qname for lookup
                    for qname, concept in modelXbrl.qnameConcepts.items():
                        if str(qname) == concept_qname:
                            concept_obj = concept
                            break
                
                # Get a readable label
                concept_label = concept_local_name
                if concept_obj is not None and hasattr(concept_obj, 'label'):
                    try:
                        label = concept_obj.label()
                        if label:
                            concept_label = label
                    except:
                        pass
                
                # Extract all facts for this concept (dimensional and non-dimensional)
                all_dimensional_facts = []
                for fact in concept_facts:
                    enhanced_fact = self._extract_enhanced_dimensional_fact(fact)
                    if not enhanced_fact:
                        # Non-dimensional numeric fact discovered outside presentation
                        enhanced_fact = self._extract_simple_fact(fact)
                    if enhanced_fact:
                        enhanced_fact['concept_name'] = concept_qname
                        enhanced_fact['concept_local_name'] = concept_local_name
                        enhanced_fact['fact_label'] = concept_label
                        enhanced_fact['source'] = 'missing_concept_discovery'
                        all_dimensional_facts.append(enhanced_fact)
                
                # Create a line item for this missing concept
                if all_dimensional_facts:
                    # Select a primary fact (typically the one without dimensions or with fewer dimensions)
                    primary_fact = None
                    for fact_data in all_dimensional_facts:
                        if fact_data.get('dimension_count', 0) == 0:
                            primary_fact = fact_data
                            break
                    
                    # If no non-dimensional fact, use the first one
                    if not primary_fact and all_dimensional_facts:
                        primary_fact = all_dimensional_facts[0]
                    
                    line_item = {
                        'name': concept_qname,
                        'label': concept_label,
                        'value': primary_fact.get('value') if primary_fact else None,
                        'context_id': primary_fact.get('context_id') if primary_fact else None,
                        'unit_id': primary_fact.get('unit_id') if primary_fact else None,
                        'unit_info': primary_fact.get('unit_info') if primary_fact else {},
                        'period': primary_fact.get('period') if primary_fact else None,
                        'period_type': primary_fact.get('period_type') if primary_fact else None,
                        'entity_info': primary_fact.get('entity_info') if primary_fact else {},
                        'concept_local_name': concept_local_name,
                        'level': 0,  # Top level since not in hierarchy
                        'order': 999999,  # Place at end
                        'children': [],
                        'dimensions': primary_fact.get('dimensions', {}) if primary_fact else {},
                        'dimensional_facts': all_dimensional_facts,
                        'fact_count': len(all_dimensional_facts),
                        'dimensional_fact_count': len([f for f in all_dimensional_facts if f.get('has_dimensions', False)]),
                        'source': 'enhanced_missing_concept_discovery',
                        'missing_from_presentation': True,
                        'discovery_note': f'Concept with {len(all_dimensional_facts)} dimensional facts not found in standard presentation'
                    }
                    
                    line_items.append(line_item)
                    
            except Exception as e:
                logger.debug(f"Error creating line item for missing concept {concept_qname}: {e}")
                continue
        
        if line_items:
            logger.debug(f"Created {len(line_items)} line items for missing dimensional concepts")
            logger.debug("Examples of created line items:")
            for i, item in enumerate(line_items[:3]):
                logger.debug(f"   • {item['label']} ({item['fact_count']} facts, {item['dimensional_fact_count']} dimensional)")
        
        return line_items

# Integration function to enhance existing xbrl_extractor
def enhance_dimensional_extraction(xbrl_extractor_instance, modelXbrl, facts):
    """
    Enhance the dimensional extraction of an existing XBRL extractor instance.
    The system captures ALL dimensional data, including concepts
    that don't appear in standard presentation relationships.
    """
    logger.debug("ENHANCING DIMENSIONAL EXTRACTION")
    logger.debug("=" * 50)
    
    enhancer = EnhancedDimensionalExtractor(xbrl_extractor_instance)
    
    # Discover dimensional relationships
    dimensional_structure = enhancer.discover_dimensional_relationships(modelXbrl)
    
    # Analyze missing opportunities
    missing_analysis = enhancer.analyze_missing_dimensional_opportunities(facts, modelXbrl)
    
    # Perform enhanced extraction
    enhanced_facts = enhancer.enhanced_dimensional_fact_extraction(facts, modelXbrl)
    
    # Create line items for missing dimensional concepts
    missing_concept_line_items = enhancer._create_line_items_for_missing_concepts(modelXbrl, facts)
    
    logger.debug(f"\nEnhancement complete:")
    logger.debug(f"   Original dimensional structure discovered")
    logger.debug(f"   {missing_analysis['facts_with_potential_dims']} facts identified with potential dimensions")
    logger.debug(f"   Enhanced extraction captured {len(enhanced_facts)} dimensional facts")
    logger.debug(f"   Created {len(missing_concept_line_items)} line items for missing concepts")
    
    return {
        'dimensional_structure': dimensional_structure,
        'missing_analysis': missing_analysis,
        'enhanced_facts': enhanced_facts,
        'missing_concept_line_items': missing_concept_line_items,
        'enhancer': enhancer
    }
