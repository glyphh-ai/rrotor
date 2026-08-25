"""
Core data structures for the Glyphh SDK.

This module defines the fundamental data types used throughout the SDK:
- Vector: Bipolar vectors in {-1, +1}
- Concept: Structured concepts for encoding
- Edge: Edges connecting glyphs for similarity computation
- Glyph: Encoded concepts with hierarchical structure
- Layer: Layer-level encoding structure
- Segment: Segment-level encoding structure

All data structures enforce strict validation to maintain vector space consistency
and bipolar constraints.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
import numpy as np
import hashlib


@dataclass
class Vector:
    """
    Bipolar vector in {-1, +1}.
    
    Vectors are the fundamental unit of representation in HDC. All vectors must:
    - Contain only values in {-1, +1} (bipolar constraint)
    - Have a fixed dimension
    - Belong to a specific vector space (identified by space_id)
    
    Attributes:
        data: NumPy array with dtype=int8, values in {-1, 1}
        dimension: Number of dimensions in the vector
        space_id: Identifier for the vector space this vector belongs to
    
    Raises:
        AssertionError: If bipolar constraint or dimension mismatch is violated
    
    Example:
        >>> vec = Vector(
        ...     data=np.array([-1, 1, -1, 1], dtype=np.int8),
        ...     dimension=4,
        ...     space_id="a3f2e8b1"
        ... )
    """
    data: np.ndarray
    dimension: int
    space_id: str
    
    def __post_init__(self):
        """Validate bipolar constraint and dimension consistency."""
        # Ensure data is numpy array
        if not isinstance(self.data, np.ndarray):
            self.data = np.array(self.data, dtype=np.int8)
        
        # Validate bipolar constraint: all values must be in {-1, +1}
        # Optimized: check if all absolute values are 1 (faster than np.isin)
        abs_values = np.abs(self.data)
        if not np.all(abs_values == 1):
            # Only compute invalid values if validation fails (rare case)
            mask = abs_values != 1
            invalid_values = self.data[mask]
            raise ValueError(
                f"Vector must be bipolar (values in {{-1, +1}}). "
                f"Found invalid values: {invalid_values[:10].tolist()}"
            )
        
        # Validate dimension consistency
        if len(self.data) != self.dimension:
            raise ValueError(
                f"Dimension mismatch: expected {self.dimension}, "
                f"got {len(self.data)}"
            )
    
    def __eq__(self, other):
        """Check equality based on data and space_id."""
        if not isinstance(other, Vector):
            return False
        return (
            np.array_equal(self.data, other.data) and
            self.space_id == other.space_id and
            self.dimension == other.dimension
        )
    
    def __hash__(self):
        """Compute hash for use in sets and dicts."""
        return hash((self.data.tobytes(), self.space_id, self.dimension))


@dataclass
class Concept:
    """
    Structured concept for encoding.
    
    Concepts represent the input to the encoding process. They contain:
    - A human-readable name
    - Attributes (key-value pairs)
    - Relationships to other concepts
    - Optional metadata
    
    Attributes:
        name: Human-readable name for the concept
        attributes: Dictionary of attribute key-value pairs
        relationships: List of (relation_type, target) tuples
        metadata: Optional metadata dictionary
    
    Example:
        >>> concept = Concept(
        ...     name="red car",
        ...     attributes={"type": "car", "color": "red", "size": "medium"},
        ...     relationships=[("has_part", "wheels"), ("used_for", "transportation")],
        ...     metadata={"domain": "automotive"}
        ... )
    """
    name: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    relationships: List[Tuple[str, str]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert concept to dictionary representation."""
        return {
            "name": self.name,
            "attributes": self.attributes,
            "relationships": self.relationships,
            "metadata": self.metadata
        }


@dataclass
class Segment:
    """
    Segment-level encoding structure.
    
    A segment represents a subdivision within a layer, containing:
    - Role bindings (role → bound vector)
    - Role values (role → original value)
    - Segment cortex (bundle of all role bindings)
    - Weights for importance and security
    
    Attributes:
        name: Segment name (e.g., "attributes", "relations")
        cortex: Aggregated segment representation (bundle of role bindings)
        roles: Dictionary mapping role names to bound vectors
        role_values: Dictionary mapping role names to original values
        weights: Segment-level weights (similarity and security)
    
    Example:
        >>> segment = Segment(
        ...     name="attributes",
        ...     cortex=attributes_cortex_vector,
        ...     roles={
        ...         "type": type_binding,
        ...         "color": color_binding,
        ...         "size": size_binding
        ...     },
        ...     role_values={
        ...         "type": "car",
        ...         "color": "red",
        ...         "size": "medium"
        ...     },
        ...     weights={"segment": 0.9, "type": 1.0, "color": 0.8, "size": 0.6}
        ... )
    """
    name: str
    cortex: Vector
    roles: Dict[str, Vector] = field(default_factory=dict)
    role_values: Dict[str, Any] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class Layer:
    """
    Layer-level encoding structure.
    
    A layer represents a hierarchical level in the multi-layer encoding architecture,
    containing:
    - Multiple segments
    - Layer cortex (bundle of all segment cortices)
    - Layer-level weights
    
    Attributes:
        name: Layer name (e.g., "semantic", "syntactic")
        cortex: Aggregated layer representation (bundle of segment cortices)
        segments: Dictionary mapping segment names to Segment objects
        weights: Layer-level weights (similarity and security)
    
    Example:
        >>> layer = Layer(
        ...     name="semantic",
        ...     cortex=semantic_layer_cortex,
        ...     segments={
        ...         "attributes": attributes_segment,
        ...         "relations": relations_segment
        ...     },
        ...     weights={"layer": 0.8}
        ... )
    """
    name: str
    cortex: Vector
    segments: Dict[str, Segment] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class Glyph:
    """
    Encoded concept represented as a hyperdimensional vector with semantic metadata.
    
    A glyph is the fundamental unit of encoded knowledge, containing:
    - Composite identifier (primary_key@timestamp#version)
    - Human-readable name
    - Vector space identifier
    - Hierarchical structure (global cortex → layers → segments → roles)
    - Security levels for access control
    - Metadata
    
    The hierarchical structure follows this pattern:
    - Role bindings → (bundle) → Segment cortex
    - Segment cortices → (bundle) → Layer cortex
    - Layer cortices → (bundle) → Global cortex
    
    Attributes:
        identifier: Composite identifier (e.g., "user_123@2024-01-15T10:30:00Z#v1")
        name: Human-readable name
        space_id: Vector space identifier for consistency validation
        global_cortex: Aggregated global representation (bundle of all layers)
        layers: Dictionary mapping layer names to Layer objects
        security_levels: Security clearance requirements by hierarchy level
        metadata: Optional metadata dictionary
        timestamp: Creation timestamp
        version: Version string
    
    Example:
        >>> glyph = Glyph(
        ...     identifier="car_red@2024-01-15T10:30:00Z#v1",
        ...     name="red car",
        ...     space_id="a3f2e8b1",
        ...     global_cortex=global_cortex_vector,
        ...     layers={
        ...         "semantic": semantic_layer
        ...     },
        ...     security_levels={"cortex": 0.9, "layer": 0.8},
        ...     metadata={"domain": "automotive"},
        ...     timestamp=datetime.now(),
        ...     version="v1"
        ... )
    """
    identifier: str
    name: str
    space_id: str
    global_cortex: Vector
    layers: Dict[str, Layer] = field(default_factory=dict)
    security_levels: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    version: str = "v1"
    
    def __post_init__(self):
        """Validate glyph structure."""
        # Validate identifier format (should be primary_key@timestamp#version)
        if "@" not in self.identifier or "#" not in self.identifier:
            raise ValueError(
                f"Invalid identifier format: {self.identifier}. "
                f"Expected format: primary_key@timestamp#version"
            )
        
        # Validate space_id consistency
        if self.global_cortex.space_id != self.space_id:
            raise ValueError(
                f"Space ID mismatch: glyph space_id={self.space_id}, "
                f"global_cortex space_id={self.global_cortex.space_id}"
            )
        
        # Validate all layers have same space_id
        for layer_name, layer in self.layers.items():
            if layer.cortex.space_id != self.space_id:
                raise ValueError(
                    f"Space ID mismatch in layer '{layer_name}': "
                    f"expected {self.space_id}, got {layer.cortex.space_id}"
                )
            
            # Validate all segments in layer have same space_id
            for segment_name, segment in layer.segments.items():
                if segment.cortex.space_id != self.space_id:
                    raise ValueError(
                        f"Space ID mismatch in segment '{segment_name}' "
                        f"of layer '{layer_name}': "
                        f"expected {self.space_id}, got {segment.cortex.space_id}"
                    )
                
                # Validate all roles in segment have same space_id
                for role_name, role_vector in segment.roles.items():
                    if role_vector.space_id != self.space_id:
                        raise ValueError(
                            f"Space ID mismatch in role '{role_name}' "
                            f"of segment '{segment_name}' in layer '{layer_name}': "
                            f"expected {self.space_id}, got {role_vector.space_id}"
                        )


@dataclass
class Edge:
    """
    Edge connecting glyphs for similarity computation.
    
    Edges represent connections between glyphs at different hierarchical levels.
    There are 8 edge types:
    - Spatial edges (4): neural_cortex, neural_layer, neural_segment, neural_role
    - Temporal edges (4): temporal_cortex, temporal_layer, temporal_segment, temporal_role
    
    Attributes:
        type: Edge type (one of 8 types)
        source: Source glyph identifier
        target: Target glyph identifier (optional, None for spatial edges)
        vector: Vector representation for this edge
        weights: Weights for similarity computation
        security_level: Required security clearance for this edge
        layer: Layer index (optional, for layer/segment/role edges)
        segment: Segment index (optional, for segment/role edges)
        role: Role name (optional, for role edges)
    
    Example:
        >>> edge = Edge(
        ...     type="neural_cortex",
        ...     source="car_red@2024-01-15T10:30:00Z#v1",
        ...     target=None,
        ...     vector=global_cortex_vector,
        ...     weights={"cortex": 1.0},
        ...     security_level=0.9,
        ...     layer=None,
        ...     segment=None,
        ...     role=None
        ... )
    """
    type: str
    source: str
    target: Optional[str]
    vector: Vector
    weights: Dict[str, float]
    security_level: float
    layer: Optional[int] = None
    segment: Optional[int] = None
    role: Optional[str] = None
    
    def __post_init__(self):
        """Validate edge type."""
        valid_types = [
            "neural_cortex", "neural_layer", "neural_segment", "neural_role",
            "temporal_cortex", "temporal_layer", "temporal_segment", "temporal_role"
        ]
        if self.type not in valid_types:
            raise ValueError(
                f"Invalid edge type: {self.type}. "
                f"Must be one of: {valid_types}"
            )


def compute_space_id(dimension: int, seed: int, config_json: str) -> str:
    """
    Compute unique space identifier for vector space consistency.
    
    The space_id is a deterministic hash of the dimension, seed, and configuration.
    This ensures that vectors from the same configuration always have the same space_id,
    enabling validation that similarity operations occur in the same vector space.
    
    Args:
        dimension: Vector dimension
        seed: Random seed for deterministic generation
        config_json: JSON string of encoder configuration
    
    Returns:
        16-character hexadecimal space identifier
    
    Example:
        >>> space_id = compute_space_id(10000, 42, '{"dimension":10000,"seed":42}')
        >>> print(space_id)
        'a3f2e8b1c4d5e6f7'
    """
    # Create deterministic hash from dimension, seed, and config
    hash_input = f"{dimension}:{seed}:{config_json}"
    hash_bytes = hashlib.sha256(hash_input.encode()).hexdigest()
    
    # Return first 16 characters as space_id
    return hash_bytes[:16]
