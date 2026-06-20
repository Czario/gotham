"""
Utility functions to help identify and analyze facts with missing or unclear identification
"""
import json
from typing import Dict, List, Any, Optional
import re


def analyze_unidentified_facts(financial_data: Dict) -> Dict[str, Any]:
    """
    Analyze facts that are difficult to identify due to missing labels, dimensions, or concept names
    Returns a report with problematic facts and suggestions for identification
    """
    analysis = {
        'total_facts': 0,
        'problematic_facts': [],
        'identification_issues': {
            'no_label': 0,
            'no_dimensions': 0,
            'empty_dimension_details': 0,
            'cryptic_concept_name': 0,
            'no_readable_identifier': 0
        },
        'suggestions': []
    }
    
    # Analyze all statements in the financial data
    for statement_type, statement_data in financial_data.items():
        if isinstance(statement_data, dict) and 'facts' in statement_data:
            facts = statement_data['facts']
            
            for fact in facts:
                # Skip None or invalid facts
                if not fact or not isinstance(fact, dict):
                    continue
                    
                analysis['total_facts'] += 1
                issues = []
                
                # Check for identification issues
                fact_label = fact.get('fact_label', '')
                concept_name = fact.get('concept_name', '')
                dimension_details = fact.get('dimension_details', {})
                readable_identifier = fact.get('readable_identifier', '')
                
                # No label or generic label
                if not fact_label or fact_label in ['Unknown', 'N/A', '']:
                    issues.append('no_label')
                    analysis['identification_issues']['no_label'] += 1
                
                # Empty dimension details
                if not dimension_details or dimension_details == {}:
                    issues.append('empty_dimension_details')
                    analysis['identification_issues']['empty_dimension_details'] += 1
                
                # Cryptic concept name (contains lots of underscores, numbers, or is very technical)
                if concept_name and _is_cryptic_concept(concept_name):
                    issues.append('cryptic_concept_name')
                    analysis['identification_issues']['cryptic_concept_name'] += 1
                
                # No readable identifier
                if not readable_identifier or readable_identifier in ['Unknown Fact', 'Unknown']:
                    issues.append('no_readable_identifier')
                    analysis['identification_issues']['no_readable_identifier'] += 1
                
                # If fact has multiple identification issues, it's problematic
                if len(issues) >= 2:
                    analysis['problematic_facts'].append({
                        'fact': fact,
                        'issues': issues,
                        'statement_type': statement_type,
                        'identification_score': _calculate_identification_score(fact)
                    })
    
    # Generate suggestions based on analysis
    analysis['suggestions'] = _generate_identification_suggestions(analysis)
    
    return analysis


def _is_cryptic_concept(concept_name: str) -> bool:
    """Determine if a concept name is cryptic and hard to understand"""
    if not concept_name:
        return True
    
    # Remove namespace prefix for analysis
    local_name = concept_name.split(':')[-1]
    
    # Check for cryptic patterns
    # Strong indicators that are sufficient alone
    strong_indicators = [
        bool(re.search(r'\d{3,}', local_name)),  # Contains 3+ digit numbers
        '_' in concept_name and 'us-gaap' in concept_name,  # Technical prefixes with underscores
    ]
    
    # Weak indicators that require multiple to be cryptic
    weak_indicators = [
        len(local_name) > 50,  # Very long names
        local_name.count('_') > 3,  # Too many underscores
        not bool(re.search(r'[A-Z][a-z]', local_name)),  # No camelCase pattern
    ]
    
    # A concept is cryptic if it has any strong indicator OR multiple weak indicators
    return any(strong_indicators) or sum(weak_indicators) >= 2


def _calculate_identification_score(fact: Dict) -> float:
    """Calculate a score (0-1) for how well a fact can be identified"""
    score = 0.0
    
    # Readable identifier (40% weight)
    readable_identifier = fact.get('readable_identifier', '')
    if readable_identifier and readable_identifier not in ['Unknown Fact', 'Unknown']:
        score += 0.4
    
    # Fact label (30% weight)
    fact_label = fact.get('fact_label', '')
    if fact_label and fact_label not in ['Unknown', 'N/A', '']:
        score += 0.3
    
    # Dimension details (20% weight)
    dimension_details = fact.get('dimension_details', {})
    if dimension_details and dimension_details != {}:
        score += 0.2
    
    # Concept name readability (10% weight)
    concept_name = fact.get('concept_name', '')
    if concept_name and not _is_cryptic_concept(concept_name):
        score += 0.1
    
    return score


def _generate_identification_suggestions(analysis: Dict) -> List[str]:
    """Generate suggestions based on the analysis results"""
    suggestions = []
    
    issues = analysis['identification_issues']
    total_facts = analysis['total_facts']
    
    if issues['no_label'] > total_facts * 0.3:
        suggestions.append(
            "High number of facts with missing labels. Consider enhancing label extraction "
            "or using alternative label sources (e.g., presentation linkbase)."
        )
    
    if issues['empty_dimension_details'] > total_facts * 0.5:
        suggestions.append(
            "Many facts have empty dimension_details. This is normal for aggregate totals, "
            "but ensure dimensional extraction is working for detailed facts."
        )
    
    if issues['cryptic_concept_name'] > total_facts * 0.2:
        suggestions.append(
            "Several facts have cryptic concept names. Consider implementing concept name "
            "cleanup or using a concept mapping dictionary."
        )
    
    if len(analysis['problematic_facts']) > total_facts * 0.1:
        suggestions.append(
            "Consider implementing enhanced fact identification using context information, "
            "period details, and unit information to improve fact recognition."
        )
    
    return suggestions


def find_similar_facts(target_fact: Dict, all_facts: List[Dict], threshold: float = 0.7) -> List[Dict]:
    """
    Find facts similar to the target fact based on concept, value, and context
    Useful for identifying what a poorly-labeled fact might represent
    """
    similar_facts = []
    
    target_concept = target_fact.get('concept_name', '')
    target_value = target_fact.get('value', 0)
    target_period = target_fact.get('period_info', '')
    
    for fact in all_facts:
        if fact is target_fact:  # Use identity comparison, not equality
            continue
        
        similarity_score = 0.0
        
        # Concept similarity
        fact_concept = fact.get('concept_name', '')
        if target_concept and fact_concept:
            if target_concept == fact_concept:
                similarity_score += 0.4
            elif target_concept.split(':')[-1] == fact_concept.split(':')[-1]:
                similarity_score += 0.2
        
        # Value similarity (for numerical facts)
        fact_value = fact.get('value', 0)
        if isinstance(target_value, (int, float)) and isinstance(fact_value, (int, float)):
            if target_value != 0:
                value_ratio = min(fact_value, target_value) / max(fact_value, target_value)
                if value_ratio > 0.9:
                    similarity_score += 0.3
                elif value_ratio > 0.5:
                    similarity_score += 0.1
        
        # Period similarity
        fact_period = fact.get('period_info', '')
        if target_period and fact_period:
            if target_period == fact_period:
                similarity_score += 0.3
        
        if similarity_score >= threshold:
            similar_facts.append({
                'fact': fact,
                'similarity_score': similarity_score
            })
    
    # Sort by similarity score
    similar_facts.sort(key=lambda x: x['similarity_score'], reverse=True)
    
    return similar_facts


def export_identification_report(analysis: Dict, output_file: str = 'fact_identification_report.json'):
    """Export the identification analysis to a JSON file for review"""
    with open(output_file, 'w') as f:
        json.dump(analysis, f, indent=2, default=str)
    
    print(f"📄 Fact identification report exported to: {output_file}")
    print(f"   Total facts analyzed: {analysis['total_facts']}")
    print(f"   Problematic facts: {len(analysis['problematic_facts'])}")
    print(f"   Issues found: {sum(analysis['identification_issues'].values())}")


if __name__ == "__main__":
    # Example usage
    print("Fact Identifier Utility")
    print("Usage: from core.extractors.fact_identifier import analyze_unidentified_facts")
    print("       analysis = analyze_unidentified_facts(your_financial_data)")
    print("       export_identification_report(analysis)")
