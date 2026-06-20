"""
Hierarchy management using Materialized Path + Lexicographic Order Keys.
"""
import logging
import string
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class HierarchyManager:
    """Manages hierarchy using materialized path and lexicographic order keys."""
    
    # Character set for lexicographic ordering
    ORDER_CHARS = string.ascii_lowercase  # a-z provides 26 positions
    
    def __init__(self):
        # Cache for path generation
        self._path_counter = defaultdict(int)  # Tracks next available number per path level
        self._order_usage = defaultdict(set)  # Tracks used order keys per parent path
    
    def build_hierarchy_data(self, financial_data: List[dict]) -> List[dict]:
        """
        Build hierarchy data with path and order_key for all items.
        Maintains source data hierarchy order using 'level' and 'order' fields.
        
        Args:
            financial_data: List of financial data items with 'level' and 'order' fields
            
        Returns:
            List of items with added 'path' and 'order_key' fields
        """
        if not financial_data:
            return []
        
        # Preserve original document order - do NOT sort by level/order
        # Financial statements should maintain their presentation order as they appear in the document
        # The level and order fields are for hierarchy structure, not for reordering the data
        sorted_data = financial_data.copy()  # Keep original order
        
        # Helper function for safe integer conversion
        def safe_get_int(item, key, default=0):
            value = item.get(key, default)
            return int(value) if isinstance(value, (int, float)) else default
        
        # Clear caches for new processing
        self._path_counter.clear()
        self._order_usage.clear()
        
        # Build hierarchy preserving original document order
        # Two-pass approach: first analyze structure, then generate paths
        
        # Pass 1: Analyze the hierarchy structure and group by levels
        level_groups = {}
        for item in sorted_data:
            current_level = safe_get_int(item, 'level')
            if current_level not in level_groups:
                level_groups[current_level] = []
            level_groups[current_level].append(item)
        
        # Pass 2: Generate paths while preserving document order
        result = []
        level_paths = {}  # Track path counters per level
        level_stack = []  # Stack to track parent at each level
        
        for item in sorted_data:
            current_level = safe_get_int(item, 'level')
            source_order = safe_get_int(item, 'order')
            
            # Validate level (handle negative or None levels)
            if current_level < 0:
                logger.warning(f"Invalid level {current_level} for item {item.get('concept', 'unknown')}, treating as level 0")
                current_level = 0
            
            # Adjust stack to current level - handle level jumps gracefully
            if current_level < len(level_stack):
                # Going up in hierarchy - truncate stack
                level_stack = level_stack[:current_level]
            
            # Generate path using document position-based approach
            path = self._generate_path_document_order(level_stack, current_level, item, result)
            
            # Generate order key based on document position to preserve original order
            parent_path = '.'.join(path.split('.')[:-1]) if '.' in path else ""
            order_key = self._generate_order_key_document_order(parent_path, item, result)
            
            # Create enhanced item
            enhanced_item = item.copy()
            enhanced_item['path'] = path
            enhanced_item['order_key'] = order_key
            enhanced_item['has_parent'] = bool(parent_path)
            enhanced_item['source_order'] = source_order
            enhanced_item['hierarchy_level'] = len(path.split('.')) - 1
            enhanced_item['original_input_level'] = current_level
            enhanced_item['document_position'] = len(result)  # Track document position
            
            result.append(enhanced_item)
            
            # Update level stack for potential children
            while len(level_stack) <= current_level:
                level_stack.append(None)
            
            # Set current level info
            level_stack[current_level] = {
                'path': path,
                'source_order': source_order,
                'order_key': order_key,
                'concept': item.get('concept', 'unknown'),
                'document_position': len(result) - 1
            }
        
        return result
    
    def _generate_path_for_position(self, level_stack: List, current_level: int, source_order: int) -> str:
        """
        Generate materialized path for current item using source order for consistency.
        
        Args:
            level_stack: Stack of parent information at each level
            current_level: Current level (0-based)
            source_order: Order from source data
            
        Returns:
            Materialized path like "001", "001.002", "001.002.003"
        """
        # Ensure source_order is integer
        if isinstance(source_order, float):
            source_order = int(source_order)
            
        if current_level == 0:
            # Root level item - use source order + 1 (to start from 001)
            parent_path = ""
            current_part = f"{source_order + 1:03d}"
        else:
            # Try to get parent path from immediate parent level
            parent_level = current_level - 1
            
            if (parent_level < len(level_stack) and 
                level_stack[parent_level] is not None and 
                'path' in level_stack[parent_level]):
                # Normal case: parent exists at expected level
                parent_path = level_stack[parent_level]['path']
            else:
                # Handle orphaned nodes or missing parent levels
                parent_path = self._find_best_parent_path(level_stack, current_level)
            
            # For child items, use source order + 1 within the parent context
            current_part = f"{source_order + 1:03d}"
        
        # Combine with parent path
        if parent_path:
            return f"{parent_path}.{current_part}"
        else:
            return current_part

    def _generate_path(self, level_stack: List, current_level: int) -> str:
        """
        Generate materialized path for current item.
        Handles nodes that may not have parents (orphaned) or children (leaf nodes).
        
        Args:
            level_stack: Stack of parent information at each level
            current_level: Current level (0-based)
            
        Returns:
            Materialized path like "001", "001.002", "001.002.003"
        """
        if current_level == 0:
            # Root level item - no parent needed
            parent_path = ""
        else:
            # Try to get parent path from immediate parent level
            parent_level = current_level - 1
            
            if (parent_level < len(level_stack) and 
                level_stack[parent_level] is not None and 
                'path' in level_stack[parent_level]):
                # Normal case: parent exists at expected level
                parent_path = level_stack[parent_level]['path']
            else:
                # Handle orphaned nodes or missing parent levels
                parent_path = self._find_best_parent_path(level_stack, current_level)
        
        # Generate new path component for current level
        next_number = self._get_next_path_number(parent_path)
        current_part = f"{next_number:03d}"  # Zero-padded 3 digits
        
        # Combine with parent path
        if parent_path:
            return f"{parent_path}.{current_part}"
        else:
            return current_part
    
    def _find_best_parent_path(self, level_stack: List, current_level: int) -> str:
        """
        Find the best parent path when direct parent is missing.
        Handles cases where nodes don't have immediate parents.
        
        Args:
            level_stack: Stack of parent information at each level
            current_level: Current level (0-based)
            
        Returns:
            Best parent path found, or empty string if treating as root
        """
        # Look backwards from current level to find the nearest valid parent
        for level in range(current_level - 1, -1, -1):
            if (level < len(level_stack) and 
                level_stack[level] is not None and 
                'path' in level_stack[level]):
                
                parent_path = level_stack[level]['path']
                
                # If we found a parent several levels up, we need to build
                # intermediate path segments to maintain proper hierarchy depth
                missing_levels = (current_level - 1) - level
                
                if missing_levels > 0:
                    # Build intermediate path parts for missing levels
                    intermediate_path = parent_path
                    for missing_level in range(missing_levels):
                        # Create default path component for missing intermediate levels
                        next_num = self._get_next_path_number(intermediate_path)
                        intermediate_part = f"{next_num:03d}"
                        intermediate_path = f"{intermediate_path}.{intermediate_part}"
                    
                    return intermediate_path
                else:
                    return parent_path
        
        # No valid parent found - treat as orphaned root-level node
        # Generate a unique root path to avoid conflicts
        orphan_marker = "orphan"
        return ""  # Let it be treated as root level
    
    def _get_next_path_number(self, parent_path: str) -> int:
        """Get next available number for path generation."""
        self._path_counter[parent_path] += 1
        return self._path_counter[parent_path]
    
    def _generate_order_key_for_position(self, parent_path: str, position: int) -> str:
        """
        Generate lexicographic order key for a specific position among siblings.
        This ensures consistent ordering based on source data order.
        
        Args:
            parent_path: Path of the parent (empty string for root level)
            position: Position/order from source data
            
        Returns:
            Order key like "a", "b", "c", etc. based on position
        """
        # Convert position to integer in case it's a float
        if isinstance(position, float):
            position = int(position)
        
        if position < 0:
            position = 0
        
        # Convert position to lexicographic key
        if position < 26:
            # Single character: a-z
            return chr(ord('a') + position)
        elif position < 702:  # 26 + 26*26 = 702
            # Two characters: aa-zz
            first_char = chr(ord('a') + (position - 26) // 26)
            second_char = chr(ord('a') + (position - 26) % 26)
            return first_char + second_char
        else:
            # Three characters for larger positions
            pos = position - 702
            first_char = chr(ord('a') + pos // (26 * 26))
            second_char = chr(ord('a') + (pos // 26) % 26)
            third_char = chr(ord('a') + pos % 26)
            return first_char + second_char + third_char

    def _generate_order_key(self, parent_path: str) -> str:
        """
        Generate lexicographic order key for siblings.
        
        Args:
            parent_path: Path of the parent (empty string for root level)
            
        Returns:
            Order key like "a", "b", "c", ..., "z", "aa", "ab", etc.
        """
        used_keys = self._order_usage[parent_path]
        
        # Find next available key
        for length in range(1, 4):  # Support up to 3-character keys (aaa = 17,576 items)
            for key in self._generate_keys_of_length(length):
                if key not in used_keys:
                    used_keys.add(key)
                    return key
        
        # Fallback if we somehow run out (shouldn't happen with reasonable data)
        fallback_key = f"z{len(used_keys):03d}"
        used_keys.add(fallback_key)
        return fallback_key
    
    def _generate_keys_of_length(self, length: int):
        """Generate all possible keys of given length."""
        if length == 1:
            for char in self.ORDER_CHARS:
                yield char
        else:
            for prefix in self._generate_keys_of_length(length - 1):
                for char in self.ORDER_CHARS:
                    yield prefix + char
    
    def get_hierarchy_level(self, path: str) -> int:
        """Get hierarchy level from path."""
        if not path:
            return 0
        return len(path.split('.')) - 1
    
    def get_parent_path(self, path: str) -> Optional[str]:
        """Get parent path from child path."""
        if not path or '.' not in path:
            return None
        parts = path.split('.')
        return '.'.join(parts[:-1])
    
    def get_children_pattern(self, parent_path: str) -> str:
        """Get regex pattern to find direct children."""
        if not parent_path:
            return "^[^.]+$"  # Root level items
        return f"^{parent_path}\\.[^.]+$"  # Direct children only
    
    def get_descendants_pattern(self, ancestor_path: str) -> str:
        """Get regex pattern to find all descendants."""
        return f"^{ancestor_path}\\."
    
    def insert_item_after(self, target_order_key: str, parent_path: str = "") -> str:
        """
        Generate order key for inserting an item after target.
        
        Args:
            target_order_key: Order key of the item to insert after
            parent_path: Parent path for sibling context
            
        Returns:
            New order key that sorts after target_order_key
        """
        used_keys = self._order_usage[parent_path]
        
        # Find the next item after target to determine insertion point
        sorted_keys = sorted([k for k in used_keys if k > target_order_key])
        
        if not sorted_keys:
            # Insert at end - generate next key after target
            new_key = self._increment_order_key(target_order_key)
        else:
            # Insert between target and next item
            next_key = sorted_keys[0]
            new_key = self._generate_between_keys(target_order_key, next_key)
        
        used_keys.add(new_key)
        return new_key
    
    def insert_item_before(self, target_order_key: str, parent_path: str = "") -> str:
        """
        Generate order key for inserting an item before target.
        
        Args:
            target_order_key: Order key of the item to insert before
            parent_path: Parent path for sibling context
            
        Returns:
            New order key that sorts before target_order_key
        """
        used_keys = self._order_usage[parent_path]
        
        # Find the previous item before target
        sorted_keys = sorted([k for k in used_keys if k < target_order_key])
        
        if not sorted_keys:
            # Insert at beginning
            new_key = self._decrement_order_key(target_order_key)
        else:
            # Insert between previous and target
            prev_key = sorted_keys[-1]
            new_key = self._generate_between_keys(prev_key, target_order_key)
        
        used_keys.add(new_key)
        return new_key
    
    def _increment_order_key(self, order_key: str) -> str:
        """Generate next order key after given key."""
        if not order_key:
            return "a"
        
        # Simple increment - just append 'a'
        return order_key + "a"
    
    def _decrement_order_key(self, order_key: str) -> str:
        """Generate order key before given key."""
        if not order_key or order_key == "a":
            return "0"  # Special case for inserting before first item
        
        # For simplicity, use half-way between start and target
        if len(order_key) == 1:
            char_pos = ord(order_key[0]) - ord('a')
            if char_pos > 0:
                return chr(ord('a') + char_pos // 2)
        
        # Fallback: prepend character
        return "0" + order_key
    
    def _generate_between_keys(self, key1: str, key2: str) -> str:
        """Generate order key between two existing keys."""
        # Simple approach: append 'a' to first key
        # More sophisticated fractional approaches could be implemented
        if key1 + "a" < key2:
            return key1 + "a"
        
        # If that doesn't work, try half-way character
        if len(key1) == len(key2) == 1:
            char1_pos = ord(key1[0]) - ord('a')
            char2_pos = ord(key2[0]) - ord('a')
            if char2_pos - char1_pos > 1:
                mid_pos = (char1_pos + char2_pos) // 2
                return chr(ord('a') + mid_pos)
        
        # Fallback: append to key1
        return key1 + "m"  # Use middle character
    
    def validate_hierarchy_structure(self, hierarchy_data: List[dict]) -> Dict[str, Any]:
        """
        Validate hierarchy structure and identify potential issues.
        
        Args:
            hierarchy_data: List of hierarchy items with path and level info
            
        Returns:
            Dictionary with validation results and statistics
        """
        stats = {
            'total_items': len(hierarchy_data),
            'root_items': 0,
            'orphaned_items': [],
            'leaf_items': 0,
            'level_distribution': defaultdict(int),
            'max_depth': 0,
            'path_conflicts': []
        }
        
        path_to_items = defaultdict(list)
        level_items = defaultdict(list)
        
        for item in hierarchy_data:
            path = item.get('path', '')
            level = item.get('hierarchy_level', item.get('level', 0))
            has_parent = item.get('has_parent', False)
            
            # Track statistics
            stats['level_distribution'][level] += 1
            stats['max_depth'] = max(stats['max_depth'], level)
            
            # Track path usage
            path_to_items[path].append(item)
            level_items[level].append(item)
            
            # Identify root items
            if level == 0 or not has_parent:
                stats['root_items'] += 1
            
            # Identify orphaned items (high level but no proper parent path)
            if level > 0 and not has_parent:
                stats['orphaned_items'].append({
                    'concept': item.get('concept', 'unknown'),
                    'level': level,
                    'path': path,
                    'expected_parent_level': level - 1
                })
        
        # Check for path conflicts
        for path, items in path_to_items.items():
            if len(items) > 1:
                stats['path_conflicts'].append({
                    'path': path,
                    'count': len(items),
                    'concepts': [item.get('concept', 'unknown') for item in items]
                })
        
        # Identify leaf items (items with no children)
        all_paths = set(path_to_items.keys())
        for path in all_paths:
            has_children = any(child_path.startswith(path + '.') for child_path in all_paths if child_path != path)
            if not has_children:
                stats['leaf_items'] += 1
        
        return stats
    
    def get_orphaned_nodes(self, hierarchy_data: List[dict]) -> List[dict]:
        """
        Get list of nodes that don't have proper parents.
        
        Args:
            hierarchy_data: List of hierarchy items
            
        Returns:
            List of orphaned nodes
        """
        orphaned = []
        path_exists = set()
        
        # First pass: collect all existing paths
        for item in hierarchy_data:
            path_exists.add(item.get('path', ''))
        
        # Second pass: find orphaned nodes
        for item in hierarchy_data:
            path = item.get('path', '')
            level = item.get('hierarchy_level', item.get('level', 0))
            
            if level > 0:  # Not a root node
                parent_path = self.get_parent_path(path)
                if parent_path and parent_path not in path_exists:
                    orphaned.append({
                        'item': item,
                        'missing_parent_path': parent_path,
                        'reason': 'Parent path does not exist'
                    })
                elif not item.get('has_parent', False):
                    orphaned.append({
                        'item': item,
                        'missing_parent_path': parent_path,
                        'reason': 'No parent found during processing'
                    })
        
        return orphaned
    
    def get_leaf_nodes(self, hierarchy_data: List[dict]) -> List[dict]:
        """
        Get list of nodes that have no children (leaf nodes).
        
        Args:
            hierarchy_data: List of hierarchy items
            
        Returns:
            List of leaf nodes
        """
        all_paths = {item.get('path', '') for item in hierarchy_data}
        leaf_nodes = []
        
        for item in hierarchy_data:
            path = item.get('path', '')
            has_children = any(
                child_path.startswith(path + '.') 
                for child_path in all_paths 
                if child_path != path
            )
            
            if not has_children:
                leaf_nodes.append(item)
        
        return leaf_nodes
    
    def normalize_hierarchy_levels(self, financial_data: List[dict], treat_min_level_as_root: bool = True) -> List[dict]:
        """
        Normalize hierarchy levels for real-world data that may start at arbitrary levels.
        
        Args:
            financial_data: List of financial data items with 'order' and 'level' fields
            treat_min_level_as_root: If True, treat the minimum level as root level (0)
            
        Returns:
            List of items with normalized levels
        """
        if not financial_data:
            return []
        
        if not treat_min_level_as_root:
            return financial_data  # Return as-is
        
        # Find the minimum level in the data
        levels = [item.get('level', 0) for item in financial_data]
        # Handle float levels by converting to int
        int_levels = [int(level) if isinstance(level, (int, float)) else 0 for level in levels]
        min_level = min(int_levels)
        
        # If already starting at 0, no normalization needed
        if min_level == 0:
            return financial_data
        
        # Normalize levels by subtracting the minimum level
        normalized_data = []
        for item in financial_data:
            normalized_item = item.copy()
            original_level = item.get('level', 0)
            # Handle float levels
            if isinstance(original_level, float):
                original_level = int(original_level)
            normalized_level = original_level - min_level
            normalized_item['level'] = normalized_level
            normalized_item['original_level'] = original_level  # Keep track of original
            normalized_data.append(normalized_item)
        
        logger.info(f"Normalized hierarchy levels: shifted all levels down by {min_level} (min level {min_level} → 0)")
        return normalized_data

    def build_hierarchy_data_normalized(self, financial_data: List[dict], normalize_levels: bool = True) -> List[dict]:
        """
        Build hierarchy data with optional level normalization for real-world data.
        
        Args:
            financial_data: List of financial data items with 'order' and 'level' fields
            normalize_levels: If True, normalize levels so minimum level becomes 0
            
        Returns:
            List of items with added 'path' and 'order_key' fields
        """
        # First normalize levels if requested
        if normalize_levels:
            processed_data = self.normalize_hierarchy_levels(financial_data, treat_min_level_as_root=True)
        else:
            processed_data = financial_data
        
        # Then build hierarchy normally
        return self.build_hierarchy_data(processed_data)

    def _generate_path_document_order(self, level_stack: List, current_level: int, item: dict, processed_items: List[dict]) -> str:
        """
        Generate materialized path for current item preserving document order.
        
        Args:
            level_stack: Stack of parent information at each level
            current_level: Current level (0-based)
            item: Current item being processed
            processed_items: Items already processed (to count within parent)
            
        Returns:
            Materialized path like "001", "001.002", "001.002.003"
        """
        if current_level == 0:
            # Root level item - count how many root level items we've seen
            root_count = sum(1 for processed in processed_items 
                           if processed.get('original_input_level', 0) == 0)
            return f"{root_count + 1:03d}"
        else:
            # Find parent path from level stack
            parent_level = current_level - 1
            
            if (parent_level < len(level_stack) and 
                level_stack[parent_level] is not None and 
                'path' in level_stack[parent_level]):
                parent_path = level_stack[parent_level]['path']
            else:
                # Handle orphaned nodes
                parent_path = self._find_best_parent_path(level_stack, current_level)
            
            # Count siblings at this level with the same parent
            sibling_count = 0
            for processed in processed_items:
                if (processed.get('original_input_level') == current_level and
                    processed.get('path', '').startswith(parent_path + '.') if parent_path else
                    '.' not in processed.get('path', '')):
                    sibling_count += 1
            
            # Generate child path component
            child_part = f"{sibling_count + 1:03d}"
            
            if parent_path:
                return f"{parent_path}.{child_part}"
            else:
                return child_part

    def _generate_order_key_document_order(self, parent_path: str, item: dict, processed_items: List[dict]) -> str:
        """
        Generate order key based on document position to preserve original order.
        
        Args:
            parent_path: Path of the parent (empty string for root level)
            item: Current item being processed
            processed_items: Items already processed
            
        Returns:
            Lexicographic order key
        """
        # Count siblings at the same level with the same parent
        sibling_count = 0
        for processed in processed_items:
            processed_parent = '.'.join(processed.get('path', '').split('.')[:-1]) if '.' in processed.get('path', '') else ""
            if processed_parent == parent_path:
                sibling_count += 1
        
        # Generate lexicographic key based on sibling position
        return self._get_lexicographic_key(sibling_count)
        
    def _get_lexicographic_key(self, position: int) -> str:
        """Generate lexicographic key for given position (0-based)."""
        if position < len(self.ORDER_CHARS):
            return self.ORDER_CHARS[position]
        
        # For positions beyond single character, use multi-character keys
        # aa, ab, ac, ..., ba, bb, bc, ...
        base = len(self.ORDER_CHARS)
        if position < base * base:
            first_char = self.ORDER_CHARS[position // base]
            second_char = self.ORDER_CHARS[position % base]
            return first_char + second_char
        
        # For even larger positions, use three characters
        if position < base * base * base:
            pos = position - base * base
            first_char = self.ORDER_CHARS[pos // (base * base)]
            second_char = self.ORDER_CHARS[(pos // base) % base]
            third_char = self.ORDER_CHARS[pos % base]
            return first_char + second_char + third_char
        
        # Fallback for very large positions
        return f"z{position:04d}"
