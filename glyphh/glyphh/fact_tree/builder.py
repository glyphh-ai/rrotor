"""
Fact Tree Builder for comprehensive similarity computation transparency.

This module implements the FactTree and FactNode data structures that provide
complete transparency for every similarity computation, including:
- Citations to source data for verification
- Data samples (vector samples, actual values)
- Mathematical explanations for formulas
- Data context (dimensions, filtering, computation time)
- Hierarchical organization with parent-child relationships

The fact tree is used by SimilarityCalculator to explain every step of the
similarity computation process, making the system fully auditable and debuggable.

Example:
    >>> from glyphh.fact_tree import FactTree, Citation
    >>> from datetime import datetime
    >>> 
    >>> # Create fact tree
    >>> fact_tree = FactTree()
    >>> 
    >>> # Add facts with citations
    >>> fact_tree.add_fact(
    ...     path=["computation", "raw_similarity"],
    ...     description="Raw Similarity",
    ...     value=0.87,
    ...     math_explanation="cos(v1, v2) = (v1 · v2) / (||v1|| * ||v2||)",
    ...     citations=[Citation(
    ...         glyph_id="car_red@2024-01-15T10:30:00Z#v1",
    ...         component="cortex",
    ...         timestamp=datetime.now(),
    ...         version="v1",
    ...         data_hash="a3f2e8b1"
    ...     )],
    ...     data_sample={
    ...         "dot_product": 8700,
    ...         "norm_product": 10000.0,
    ...         "dimensions_compared": 10000
    ...     }
    ... )
    >>> 
    >>> # Serialize to JSON
    >>> json_output = fact_tree.to_json()
    >>> 
    >>> # Render as text
    >>> text_output = fact_tree.to_text()
    >>> print(text_output)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
import json


@dataclass
class Citation:
    """
    Reference to source data used in computation.
    
    Citations provide traceability to the exact source data used in each
    computation step, enabling verification and debugging.
    
    Attributes:
        glyph_id: Identifier of the source glyph (e.g., "car_red@2024-01-15T10:30:00Z#v1")
        component: Component within the glyph (e.g., "cortex", "layer_0", "segment_1", "role_color")
        timestamp: When the data was created/accessed
        version: Version of the glyph
        data_hash: Hash of the actual data for verification
    
    Example:
        >>> citation = Citation(
        ...     glyph_id="car_red@2024-01-15T10:30:00Z#v1",
        ...     component="cortex",
        ...     timestamp=datetime.now(),
        ...     version="v1",
        ...     data_hash="a3f2e8b1c4d5e6f7"
        ... )
    """
    glyph_id: str
    component: str
    timestamp: datetime
    version: str
    data_hash: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert citation to dictionary representation."""
        return {
            "glyph_id": self.glyph_id,
            "component": self.component,
            "timestamp": self.timestamp.isoformat(),
            "version": self.version,
            "data_hash": self.data_hash
        }


@dataclass
class FactNode:
    """
    Node in fact tree with full context.
    
    Each node represents a fact or computation step in the similarity calculation.
    Nodes can have children, forming a hierarchical tree structure.
    
    Attributes:
        description: Human-readable description of this fact
        value: Computed value (can be any type)
        children: List of child FactNodes
        citations: References to source data used
        data_sample: Sample of actual data used (optional)
        math_explanation: Mathematical formula/reasoning (optional)
        data_context: Volume, filtering info, and other metadata
    
    Example:
        >>> node = FactNode(
        ...     description="Raw Similarity",
        ...     value=0.87,
        ...     children=[],
        ...     citations=[citation],
        ...     data_sample={"dot_product": 8700},
        ...     math_explanation="cos(v1, v2) = (v1 · v2) / (||v1|| * ||v2||)",
        ...     data_context={"dimensions": 10000}
        ... )
    """
    description: str
    value: Any
    children: List['FactNode'] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)
    data_sample: Optional[Any] = None
    math_explanation: Optional[str] = None
    data_context: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert fact node to dictionary representation."""
        result = {
            "description": self.description,
            "value": self._serialize_value(self.value),
            "children": [child.to_dict() for child in self.children],
            "citations": [citation.to_dict() for citation in self.citations],
            "data_context": self.data_context
        }
        
        if self.data_sample is not None:
            result["data_sample"] = self._serialize_value(self.data_sample)
        
        if self.math_explanation is not None:
            result["math_explanation"] = self.math_explanation
        
        return result
    
    def _serialize_value(self, value: Any) -> Any:
        """Serialize value to JSON-compatible format."""
        # Handle datetime objects
        if isinstance(value, datetime):
            return value.isoformat()
        
        # Handle numpy arrays (convert to list)
        if hasattr(value, 'tolist'):
            return value.tolist()
        
        # Handle dictionaries recursively
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        
        # Handle lists recursively
        if isinstance(value, list):
            return [self._serialize_value(v) for v in value]
        
        # Return as-is for primitive types
        return value
    
    def to_text(self, indent: int = 0) -> str:
        """
        Render fact node as indented text.
        
        Args:
            indent: Current indentation level
        
        Returns:
            Human-readable text representation
        """
        lines = []
        prefix = "  " * indent
        
        # Add description and value
        if self.value is not None:
            lines.append(f"{prefix}+- {self.description}: {self._format_value(self.value)}")
        else:
            lines.append(f"{prefix}+- {self.description}")
        
        # Add mathematical explanation if present
        if self.math_explanation:
            lines.append(f"{prefix}|  +- Formula: {self.math_explanation}")
        
        # Add data sample if present
        if self.data_sample is not None:
            lines.append(f"{prefix}|  +- Data: {self._format_value(self.data_sample)}")
        
        # Add data context if present
        if self.data_context:
            lines.append(f"{prefix}|  +- Context: {self._format_value(self.data_context)}")
        
        # Add citations if present
        if self.citations:
            lines.append(f"{prefix}|  +- Citations:")
            for citation in self.citations:
                lines.append(
                    f"{prefix}|     +- {citation.glyph_id}/{citation.component} "
                    f"(hash: {citation.data_hash[:8]}...)"
                )
        
        # Add children recursively
        for child in self.children:
            lines.append(child.to_text(indent + 1))
        
        return "\n".join(lines)
    
    def _format_value(self, value: Any) -> str:
        """Format value for text display."""
        if isinstance(value, float):
            return f"{value:.4f}"
        elif isinstance(value, dict):
            # Format dict compactly
            items = [f"{k}={self._format_value(v)}" for k, v in value.items()]
            return "{" + ", ".join(items) + "}"
        elif isinstance(value, list):
            # Show first few items for lists
            if len(value) > 5:
                formatted = [self._format_value(v) for v in value[:5]]
                return "[" + ", ".join(formatted) + ", ...]"
            else:
                formatted = [self._format_value(v) for v in value]
                return "[" + ", ".join(formatted) + "]"
        else:
            return str(value)


class FactTree:
    """
    Hierarchical explanation of similarity computation with citations and data.
    
    The FactTree provides complete transparency for every similarity computation,
    organizing facts in a hierarchical tree structure with parent-child relationships.
    
    Each fact can include:
    - Description: Human-readable explanation
    - Value: Computed result
    - Citations: References to source data
    - Data samples: Actual data used in computation
    - Mathematical explanations: Formulas and reasoning
    - Data context: Volume, filtering, and metadata
    
    The tree can be serialized to JSON for MCP transport or rendered as
    human-readable text for debugging and auditing.
    
    Attributes:
        root: Root node of the fact tree
    
    Example:
        >>> fact_tree = FactTree()
        >>> 
        >>> # Add facts with path-based insertion
        >>> fact_tree.add_fact(
        ...     path=["computation", "raw_similarity"],
        ...     description="Raw Similarity",
        ...     value=0.87,
        ...     math_explanation="cos(v1, v2) = (v1 · v2) / (||v1|| * ||v2||)"
        ... )
        >>> 
        >>> # Serialize to JSON
        >>> json_output = fact_tree.to_json()
        >>> 
        >>> # Render as text
        >>> text_output = fact_tree.to_text()
    """
    
    def __init__(self):
        """Initialize fact tree with root node."""
        self.root = FactNode(
            description="Similarity Computation",
            value=None,
            children=[],
            citations=[],
            data_context={}
        )
    
    def add_fact(
        self,
        path: List[str],
        description: str,
        value: Any,
        citations: Optional[List[Citation]] = None,
        data_sample: Optional[Any] = None,
        math_explanation: Optional[str] = None,
        data_context: Optional[Dict[str, Any]] = None
    ):
        """
        Add a fact at the specified path in the tree.
        
        The path is a list of strings representing the hierarchical location
        of the fact. If intermediate nodes don't exist, they are created
        automatically.
        
        Args:
            path: Path to the fact in the tree (e.g., ["computation", "raw_similarity"])
            description: Human-readable description
            value: Computed value
            citations: References to source data (optional)
            data_sample: Sample of actual data used (optional)
            math_explanation: Mathematical formula/reasoning (optional)
            data_context: Volume, filtering info, and metadata (optional)
        
        Example:
            >>> fact_tree = FactTree()
            >>> fact_tree.add_fact(
            ...     path=["computation", "raw_similarity"],
            ...     description="Raw Similarity",
            ...     value=0.87,
            ...     math_explanation="cos(v1, v2) = (v1 · v2) / (||v1|| * ||v2||)",
            ...     citations=[citation],
            ...     data_sample={"dot_product": 8700}
            ... )
        """
        # Start at root
        current_node = self.root
        
        # Navigate/create path
        for i, path_element in enumerate(path):
            # Look for existing child with this description
            found_child = None
            for child in current_node.children:
                # Match on the path element (first word of description)
                if child.description.split(":")[0].strip() == path_element:
                    found_child = child
                    break
            
            # If this is the last element in path, create the final node
            if i == len(path) - 1:
                # Create the fact node
                fact_node = FactNode(
                    description=description,
                    value=value,
                    children=[],
                    citations=citations or [],
                    data_sample=data_sample,
                    math_explanation=math_explanation,
                    data_context=data_context or {}
                )
                
                # Add to current node's children
                current_node.children.append(fact_node)
            else:
                # Intermediate node - create if doesn't exist
                if found_child is None:
                    # Create intermediate node
                    intermediate_node = FactNode(
                        description=path_element,
                        value=None,
                        children=[],
                        citations=[],
                        data_context={}
                    )
                    current_node.children.append(intermediate_node)
                    current_node = intermediate_node
                else:
                    # Navigate to existing child
                    current_node = found_child
    
    def to_json(self) -> Dict[str, Any]:
        """
        Serialize fact tree to JSON for MCP transport.
        
        Returns:
            Dictionary representation of the fact tree
        
        Example:
            >>> fact_tree = FactTree()
            >>> fact_tree.add_fact(
            ...     path=["computation"],
            ...     description="Computation",
            ...     value=0.87
            ... )
            >>> json_output = fact_tree.to_json()
            >>> print(json.dumps(json_output, indent=2))
        """
        return self.root.to_dict()
    
    def to_text(self, indent: int = 0) -> str:
        """
        Render fact tree as indented text for human readability.
        
        Args:
            indent: Starting indentation level (default: 0)
        
        Returns:
            Human-readable text representation
        
        Example:
            >>> fact_tree = FactTree()
            >>> fact_tree.add_fact(
            ...     path=["computation", "raw_similarity"],
            ...     description="Raw Similarity",
            ...     value=0.87
            ... )
            >>> print(fact_tree.to_text())
            Similarity Computation
            +- computation
            |  +- Raw Similarity: 0.8700
        """
        lines = [self.root.description]
        
        # Add children
        for child in self.root.children:
            lines.append(child.to_text(indent))
        
        return "\n".join(lines)
    
    def __repr__(self) -> str:
        """String representation of the fact tree."""
        num_children = len(self.root.children)
        return f"FactTree(root='{self.root.description}', children={num_children})"
