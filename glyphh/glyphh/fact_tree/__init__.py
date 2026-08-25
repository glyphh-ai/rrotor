"""
Fact Tree Builder for the Glyphh SDK.

This module provides comprehensive fact trees with citations and data context
for complete transparency in similarity computations.

Key Features:
- Hierarchical tree structure with parent-child relationships
- Citations to source data for verification
- Data samples and mathematical explanations
- Human-readable and machine-readable formats
- Integration with SimilarityCalculator

Example:
    >>> from glyphh.fact_tree import FactTree, Citation
    >>> 
    >>> fact_tree = FactTree()
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
    ...     )]
    ... )
    >>> 
    >>> # Serialize to JSON for MCP transport
    >>> json_output = fact_tree.to_json()
    >>> 
    >>> # Render as human-readable text
    >>> text_output = fact_tree.to_text()
"""

from glyphh.fact_tree.builder import (
    FactTree,
    FactNode,
    Citation
)

__all__ = [
    "FactTree",
    "FactNode",
    "Citation"
]
