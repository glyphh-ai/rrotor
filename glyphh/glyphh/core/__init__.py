"""
Core module for Glyphh SDK.

This module provides the fundamental data structures and utilities for
hyperdimensional computing operations.
"""

from .types import (
    Vector,
    Concept,
    Edge,
    Glyph,
    Layer,
    Segment,
    compute_space_id
)

from .config import (
    EncoderConfig,
    NLEncoderConfig,
    GQLPatternsConfig,
    TemporalConfig,
    Role,
    NumericConfig,
    ContinuousConfig,
    EncodingStrategy,
    migrate_legacy_config,
    # Temporal layer constants
    TEMPORAL_LAYER_NAME,
    TEMPORAL_SEGMENT_NAME,
    TEMPORAL_ROLE_NAME,
)
# Import config classes with Config suffix to distinguish from runtime types
from .config import (
    Layer as LayerConfig,
    Segment as SegmentConfig,
)

__all__ = [
    # Data structures (runtime types)
    "Vector",
    "Concept",
    "Edge",
    "Glyph",
    "Layer",
    "Segment",
    "compute_space_id",
    # Configuration
    "EncoderConfig",
    "NLEncoderConfig",
    "GQLPatternsConfig",
    "TemporalConfig",
    "Role",
    "NumericConfig",
    "ContinuousConfig",
    "EncodingStrategy",
    "LayerConfig",
    "SegmentConfig",
    "migrate_legacy_config",
    # Temporal layer constants
    "TEMPORAL_LAYER_NAME",
    "TEMPORAL_SEGMENT_NAME",
    "TEMPORAL_ROLE_NAME",
]
