"""
Configuration for the Glyphh SDK.

This module defines:
- Role: Role definition with weights
- Segment: Segment definition containing roles
- Layer: Layer definition containing segments
- NLEncoderConfig: Configuration for NL encoder intent patterns
- EncoderConfig: Configuration dataclass for encoder initialization
- Configuration validation logic

All configuration is validated on initialization to ensure correctness.

Temporal Layer:
    Every glyph has a dedicated `_temporal` layer that encodes the temporal signal.
    This layer is automatically created during encoding and contains a single role
    binding for the temporal value. The temporal source can be:
    - "auto" (default): Auto-generate ISO8601 timestamps
    - "layer.segment.role": Use a specific role's value as the temporal signal
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional, TYPE_CHECKING
import json

# Import exceptions from dedicated module
from glyphh.exceptions import ConfigurationException

# Type checking import to avoid circular dependency
if TYPE_CHECKING:
    from glyphh.encoder.intent import IntentPattern
    from glyphh.gql.patterns import GQLPattern


# ============================================================================
# Constants
# ============================================================================

# Reserved name for the temporal layer - always contains the temporal signal
TEMPORAL_LAYER_NAME = "_temporal"
TEMPORAL_SEGMENT_NAME = "signal"
TEMPORAL_ROLE_NAME = "value"  # The role name in the temporal layer


# ============================================================================
# EncodingStrategy Enum
# ============================================================================

from enum import Enum

class EncodingStrategy(str, Enum):
    """
    Encoding strategy for numeric bins.
    
    Determines how bin numbers are converted to bit patterns in the vector.
    
    Values:
        THERMOMETER: Adjacent bins share bits (best for similarity preservation)
            - Bin 3 with max 5: [1,1,1,0,0]
            - Adjacent bins have high similarity
        BINARY: Standard binary representation (space-efficient)
            - Bin 3: [0,1,1] (binary 011)
            - No inherent similarity between adjacent bins
        GRAY: Gray code encoding (adjacent values differ by 1 bit)
            - Bin 3: [0,1,0] (Gray code)
            - Balanced similarity/efficiency
    """
    THERMOMETER = "thermometer"
    BINARY = "binary"
    GRAY = "gray"


# ============================================================================
# NumericConfig Dataclass
# ============================================================================

@dataclass
class NumericConfig:
    """
    Configuration for numeric binning on a role.
    
    When enabled, numeric values are discretized into bins and encoded
    using the specified strategy, preserving similarity between close values.
    
    Attributes:
        bin_width: Width of each bin (must be positive)
        encoding_strategy: How bin numbers are encoded to vectors
        min_value: Optional minimum value (values below are clamped)
        max_value: Optional maximum value (values above are clamped)
    
    Example:
        >>> # Tire tread depth: 0-10mm with 0.5mm bins
        >>> config = NumericConfig(
        ...     bin_width=0.5,
        ...     encoding_strategy=EncodingStrategy.THERMOMETER,
        ...     min_value=0.0,
        ...     max_value=10.0
        ... )
        >>> # 7.2mm -> bin 14, 6.8mm -> bin 13 (adjacent, high similarity)
    """
    bin_width: float
    encoding_strategy: EncodingStrategy = EncodingStrategy.THERMOMETER
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    
    def __post_init__(self):
        """Validate numeric config on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all numeric config fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.bin_width, (int, float)):
            raise ConfigurationException(
                "bin_width",
                f"Must be a number, got {type(self.bin_width).__name__}"
            )
        if self.bin_width <= 0:
            raise ConfigurationException(
                "bin_width",
                f"Must be positive, got {self.bin_width}"
            )
        
        if self.min_value is not None and not isinstance(self.min_value, (int, float)):
            raise ConfigurationException(
                "min_value",
                f"Must be a number or None, got {type(self.min_value).__name__}"
            )
        
        if self.max_value is not None and not isinstance(self.max_value, (int, float)):
            raise ConfigurationException(
                "max_value",
                f"Must be a number or None, got {type(self.max_value).__name__}"
            )
        
        if self.min_value is not None and self.max_value is not None:
            if self.min_value >= self.max_value:
                raise ConfigurationException(
                    "min_value",
                    f"min_value ({self.min_value}) must be less than max_value ({self.max_value})"
                )
        
        # Validate encoding_strategy
        if isinstance(self.encoding_strategy, str):
            try:
                self.encoding_strategy = EncodingStrategy(self.encoding_strategy)
            except ValueError:
                valid = [e.value for e in EncodingStrategy]
                raise ConfigurationException(
                    "encoding_strategy",
                    f"Must be one of {valid}, got '{self.encoding_strategy}'"
                )
        elif not isinstance(self.encoding_strategy, EncodingStrategy):
            raise ConfigurationException(
                "encoding_strategy",
                f"Must be an EncodingStrategy, got {type(self.encoding_strategy).__name__}"
            )
    
    def to_dict(self) -> dict:
        """Serialize numeric config to dictionary."""
        result = {
            "bin_width": self.bin_width,
            "encoding_strategy": self.encoding_strategy.value if isinstance(self.encoding_strategy, EncodingStrategy) else self.encoding_strategy,
        }
        if self.min_value is not None:
            result["min_value"] = self.min_value
        if self.max_value is not None:
            result["max_value"] = self.max_value
        return result
    
    @classmethod
    def from_dict(cls, data: dict) -> 'NumericConfig':
        """Create NumericConfig from dictionary."""
        encoding_strategy = data.get("encoding_strategy", "thermometer")
        if isinstance(encoding_strategy, str):
            encoding_strategy = EncodingStrategy(encoding_strategy)
        
        return cls(
            bin_width=data["bin_width"],
            encoding_strategy=encoding_strategy,
            min_value=data.get("min_value"),
            max_value=data.get("max_value"),
        )
    
    def __repr__(self) -> str:
        """String representation of the numeric config."""
        parts = [f"bin_width={self.bin_width}"]
        parts.append(f"encoding_strategy={self.encoding_strategy.value}")
        if self.min_value is not None:
            parts.append(f"min_value={self.min_value}")
        if self.max_value is not None:
            parts.append(f"max_value={self.max_value}")
        return f"NumericConfig({', '.join(parts)})"


# ============================================================================
# ContinuousConfig Dataclass
# ============================================================================

@dataclass
class ContinuousConfig:
    """
    Configuration for continuous vector projection on a role.

    When enabled, continuous float vectors (e.g., embeddings from neural networks)
    are projected into bipolar HDC space via random projection + sign quantization.
    The projection is deterministic: same input always produces the same output.

    Attributes:
        source_dim: Dimensionality of the input float vector (e.g., 512 for ArcFace)
        projection_seed: Seed for the deterministic projection matrix

    Example:
        >>> # Face embedding from ArcFace (512-dim float vector)
        >>> config = ContinuousConfig(source_dim=512, projection_seed=100)
        >>> # Depth map pooled to 8x8 grid (64-dim float vector)
        >>> config = ContinuousConfig(source_dim=64, projection_seed=300)
    """
    source_dim: int
    projection_seed: int = 42

    def __post_init__(self):
        """Validate continuous config on initialization."""
        self._validate()

    def _validate(self):
        """Validate all continuous config fields."""
        if not isinstance(self.source_dim, int):
            raise ConfigurationException(
                "source_dim",
                f"Must be an integer, got {type(self.source_dim).__name__}"
            )
        if self.source_dim <= 0:
            raise ConfigurationException(
                "source_dim",
                f"Must be positive, got {self.source_dim}"
            )
        if not isinstance(self.projection_seed, int):
            raise ConfigurationException(
                "projection_seed",
                f"Must be an integer, got {type(self.projection_seed).__name__}"
            )

    def to_dict(self) -> dict:
        """Serialize continuous config to dictionary."""
        return {
            "source_dim": self.source_dim,
            "projection_seed": self.projection_seed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'ContinuousConfig':
        """Create ContinuousConfig from dictionary."""
        return cls(
            source_dim=data["source_dim"],
            projection_seed=data.get("projection_seed", 42),
        )

    def __repr__(self) -> str:
        return f"ContinuousConfig(source_dim={self.source_dim}, projection_seed={self.projection_seed})"


# ============================================================================
# Role Dataclass
# ============================================================================

@dataclass
class Role:
    """
    A role definition within a segment.
    
    Roles represent named attributes that can be bound to values during encoding.
    Each role has its own similarity and security weights.
    
    Attributes:
        name: Role identifier (e.g., "topic", "question", "answer")
        similarity_weight: Weight for similarity computation (0.0-1.0)
        security_weight: Weight for security/access control (0.0-1.0)
        key_part: If True, this role contributes to the composite primary key
        numeric_config: Optional configuration for numeric binning
        lexicons: Optional list of alternative names/synonyms for NL query matching
        text_encoding: Optional text encoding strategy for string values.
            - None (default): Treat entire string as one symbol via generate_symbol()
            - "bag_of_words": Split value into words, encode each word as a symbol,
              and bundle them. Shared words between two values produce shared signal
              in cosine similarity. Ideal for matching NL queries against descriptions.
    
    Note:
        Temporal signals are now always encoded on a dedicated `_temporal` layer.
        Use EncoderConfig.temporal_source to specify which role provides the
        temporal value, or use "auto" for auto-generated timestamps.
    
    Example:
        >>> role = Role(
        ...     name="question",
        ...     similarity_weight=1.0,
        ...     security_weight=0.9,
        ...     key_part=True
        ... )
        >>> # Role with numeric binning for tire tread depth
        >>> role = Role(
        ...     name="tread_depth_mm",
        ...     numeric_config=NumericConfig(bin_width=0.5, encoding_strategy=EncodingStrategy.THERMOMETER)
        ... )
        >>> # Role with lexicons for NL query matching
        >>> role = Role(
        ...     name="tire_tread_depth_mm",
        ...     lexicons=["tire wear", "tread depth", "tire condition"]
        ... )
        >>> # Role with bag-of-words text encoding
        >>> role = Role(
        ...     name="description",
        ...     text_encoding="bag_of_words",
        ...     similarity_weight=0.8,
        ... )
        >>> # Role with continuous vector projection (e.g., face embedding)
        >>> role = Role(
        ...     name="face_embedding",
        ...     continuous_config=ContinuousConfig(source_dim=512, projection_seed=100),
        ... )
    """
    name: str
    similarity_weight: float = 1.0
    security_weight: float = 1.0
    key_part: bool = False
    numeric_config: Optional['NumericConfig'] = None
    continuous_config: Optional['ContinuousConfig'] = None
    lexicons: Optional[List[str]] = None
    text_encoding: Optional[str] = None
    
    def __post_init__(self):
        """Validate role on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all role fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.name, str) or not self.name:
            raise ConfigurationException(
                "name",
                "Role name must be a non-empty string"
            )
        if not isinstance(self.similarity_weight, (int, float)):
            raise ConfigurationException(
                "similarity_weight",
                f"Must be a number, got {type(self.similarity_weight).__name__}"
            )
        if not 0.0 <= self.similarity_weight <= 1.0:
            raise ConfigurationException(
                "similarity_weight",
                f"Must be between 0.0 and 1.0, got {self.similarity_weight}"
            )
        if not isinstance(self.security_weight, (int, float)):
            raise ConfigurationException(
                "security_weight",
                f"Must be a number, got {type(self.security_weight).__name__}"
            )
        if not 0.0 <= self.security_weight <= 1.0:
            raise ConfigurationException(
                "security_weight",
                f"Must be between 0.0 and 1.0, got {self.security_weight}"
            )
        # Validate text_encoding
        valid_text_encodings = {None, "bag_of_words"}
        if self.text_encoding not in valid_text_encodings:
            raise ConfigurationException(
                "text_encoding",
                f"Must be one of {valid_text_encodings}, got '{self.text_encoding}'"
            )
    
    def to_dict(self) -> dict:
        """Serialize role to dictionary."""
        result = {
            "name": self.name,
            "similarity_weight": self.similarity_weight,
            "security_weight": self.security_weight,
            "key_part": self.key_part,
        }
        if self.numeric_config is not None:
            result["numeric_config"] = self.numeric_config.to_dict()
        if self.continuous_config is not None:
            result["continuous_config"] = self.continuous_config.to_dict()
        if self.lexicons is not None and len(self.lexicons) > 0:
            result["lexicons"] = self.lexicons
        if self.text_encoding is not None:
            result["text_encoding"] = self.text_encoding
        return result
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Role':
        """Create Role from dictionary with backward compatibility."""
        # Handle legacy primary_id field - migrate to key_part
        key_part = data.get("key_part", False)
        if not key_part and data.get("primary_id", False):
            key_part = True
        
        # Parse numeric_config if present
        numeric_config = None
        if "numeric_config" in data and data["numeric_config"] is not None:
            numeric_config = NumericConfig.from_dict(data["numeric_config"])

        # Parse continuous_config if present
        continuous_config = None
        if "continuous_config" in data and data["continuous_config"] is not None:
            continuous_config = ContinuousConfig.from_dict(data["continuous_config"])

        # Parse lexicons if present
        lexicons = data.get("lexicons")
        if lexicons is not None and not isinstance(lexicons, list):
            lexicons = None

        return cls(
            name=data["name"],
            similarity_weight=data.get("similarity_weight", 1.0),
            security_weight=data.get("security_weight", 1.0),
            key_part=key_part,
            numeric_config=numeric_config,
            continuous_config=continuous_config,
            lexicons=lexicons,
            text_encoding=data.get("text_encoding"),
        )


# ============================================================================
# Segment Dataclass
# ============================================================================

@dataclass
class Segment:
    """
    A segment definition within a layer.
    
    Segments group related roles together. Each segment has its own weights
    and contains a list of role definitions.
    
    Attributes:
        name: Segment identifier (e.g., "config", "schema", "spec")
        similarity_weight: Weight for similarity computation (0.0-1.0)
        security_weight: Weight for security/access control (0.0-1.0)
        roles: List of Role definitions in this segment
    
    Example:
        >>> segment = Segment(
        ...     name="config",
        ...     similarity_weight=0.8,
        ...     security_weight=0.9,
        ...     roles=[
        ...         Role(name="topic", similarity_weight=0.5),
        ...         Role(name="question", similarity_weight=1.0, key_part=True),
        ...     ]
        ... )
    """
    name: str
    similarity_weight: float = 1.0
    security_weight: float = 1.0
    roles: List[Role] = field(default_factory=list)
    
    def __post_init__(self):
        """Validate segment on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all segment fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.name, str) or not self.name:
            raise ConfigurationException(
                "name",
                "Segment name must be a non-empty string"
            )
        if not isinstance(self.similarity_weight, (int, float)):
            raise ConfigurationException(
                "similarity_weight",
                f"Must be a number, got {type(self.similarity_weight).__name__}"
            )
        if not 0.0 <= self.similarity_weight <= 1.0:
            raise ConfigurationException(
                "similarity_weight",
                f"Must be between 0.0 and 1.0, got {self.similarity_weight}"
            )
        if not isinstance(self.security_weight, (int, float)):
            raise ConfigurationException(
                "security_weight",
                f"Must be a number, got {type(self.security_weight).__name__}"
            )
        if not 0.0 <= self.security_weight <= 1.0:
            raise ConfigurationException(
                "security_weight",
                f"Must be between 0.0 and 1.0, got {self.security_weight}"
            )
        
        # Validate role names are unique within segment
        role_names = [r.name for r in self.roles]
        if len(role_names) != len(set(role_names)):
            raise ConfigurationException(
                "roles",
                "Role names must be unique within a segment"
            )
    
    def to_dict(self) -> dict:
        """Serialize segment to dictionary."""
        return {
            "name": self.name,
            "similarity_weight": self.similarity_weight,
            "security_weight": self.security_weight,
            "roles": [r.to_dict() for r in self.roles]
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Segment':
        """Create Segment from dictionary."""
        roles = [Role.from_dict(r) for r in data.get("roles", [])]
        return cls(
            name=data["name"],
            similarity_weight=data.get("similarity_weight", 1.0),
            security_weight=data.get("security_weight", 1.0),
            roles=roles
        )


# ============================================================================
# Layer Dataclass
# ============================================================================

@dataclass
class Layer:
    """
    A layer definition within the encoder config.
    
    Layers represent hierarchical levels in the encoding structure.
    Each layer has its own weights and contains segments.
    
    Attributes:
        name: Layer identifier (e.g., "blueprint", "constraints", "output")
        similarity_weight: Weight for similarity computation (0.0-1.0)
        security_weight: Weight for security/access control (0.0-1.0)
        segments: List of Segment definitions in this layer
    
    Example:
        >>> layer = Layer(
        ...     name="blueprint",
        ...     similarity_weight=0.8,
        ...     security_weight=0.9,
        ...     segments=[
        ...         Segment(name="config", roles=[...]),
        ...         Segment(name="metadata", roles=[...]),
        ...     ]
        ... )
    """
    name: str
    similarity_weight: float = 1.0
    security_weight: float = 1.0
    segments: List[Segment] = field(default_factory=list)
    
    def __post_init__(self):
        """Validate layer on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all layer fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.name, str) or not self.name:
            raise ConfigurationException(
                "name",
                "Layer name must be a non-empty string"
            )
        if not isinstance(self.similarity_weight, (int, float)):
            raise ConfigurationException(
                "similarity_weight",
                f"Must be a number, got {type(self.similarity_weight).__name__}"
            )
        if not 0.0 <= self.similarity_weight <= 1.0:
            raise ConfigurationException(
                "similarity_weight",
                f"Must be between 0.0 and 1.0, got {self.similarity_weight}"
            )
        if not isinstance(self.security_weight, (int, float)):
            raise ConfigurationException(
                "security_weight",
                f"Must be a number, got {type(self.security_weight).__name__}"
            )
        if not 0.0 <= self.security_weight <= 1.0:
            raise ConfigurationException(
                "security_weight",
                f"Must be between 0.0 and 1.0, got {self.security_weight}"
            )
        
        # Validate segment names are unique within layer
        segment_names = [s.name for s in self.segments]
        if len(segment_names) != len(set(segment_names)):
            raise ConfigurationException(
                "segments",
                "Segment names must be unique within a layer"
            )
    
    def to_dict(self) -> dict:
        """Serialize layer to dictionary."""
        return {
            "name": self.name,
            "similarity_weight": self.similarity_weight,
            "security_weight": self.security_weight,
            "segments": [s.to_dict() for s in self.segments]
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Layer':
        """Create Layer from dictionary."""
        segments = [Segment.from_dict(s) for s in data.get("segments", [])]
        return cls(
            name=data["name"],
            similarity_weight=data.get("similarity_weight", 1.0),
            security_weight=data.get("security_weight", 1.0),
            segments=segments
        )


# ============================================================================
# NLEncoderConfig Dataclass
# ============================================================================

@dataclass
class NLEncoderConfig:
    """
    Configuration for NL encoder intent patterns.
    
    NLEncoderConfig holds a list of intent patterns that define how natural
    language queries are matched to structured operations. This configuration
    travels with the model in the .glyphh file.
    
    Attributes:
        patterns: List of intent pattern dictionaries, each containing:
            - intent_type: Unique identifier for the intent
            - example_phrases: List of example phrases that match this intent
            - query_template: Structured query template to execute when matched
    
    Example:
        >>> nl_config = NLEncoderConfig(patterns=[
        ...     {
        ...         "intent_type": "find_customer",
        ...         "example_phrases": ["find customer", "lookup customer"],
        ...         "query_template": {"operation": "similarity_search", "entity_type": "customer"}
        ...     }
        ... ])
    """
    patterns: List[Dict[str, Any]] = field(default_factory=list)
    
    def __post_init__(self):
        """Validate NL encoder config on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all NL encoder config fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.patterns, list):
            raise ConfigurationException(
                "patterns",
                f"Must be a list, got {type(self.patterns).__name__}"
            )
        
        # Validate unique intent types
        intent_types = []
        for i, pattern in enumerate(self.patterns):
            if not isinstance(pattern, dict):
                raise ConfigurationException(
                    "patterns",
                    f"Pattern at index {i} must be a dictionary"
                )
            
            intent_type = pattern.get("intent_type")
            if not intent_type:
                raise ConfigurationException(
                    "patterns",
                    f"Pattern at index {i} must have a non-empty 'intent_type'"
                )
            
            if intent_type in intent_types:
                raise ConfigurationException(
                    "patterns",
                    f"Duplicate intent_type '{intent_type}' found. Intent types must be unique."
                )
            intent_types.append(intent_type)
            
            # Validate example_phrases
            example_phrases = pattern.get("example_phrases", [])
            if not example_phrases:
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{intent_type}' must have at least one example phrase"
                )
            if not isinstance(example_phrases, list):
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{intent_type}' example_phrases must be a list"
                )
            
            # Validate query_template
            query_template = pattern.get("query_template")
            if query_template is not None and not isinstance(query_template, dict):
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{intent_type}' query_template must be a dictionary"
                )
    
    def to_dict(self) -> dict:
        """
        Serialize NL encoder config to dictionary.
        
        Returns:
            Dictionary representation of the NL encoder config
        """
        return {
            "patterns": [
                {
                    "intent_type": p.get("intent_type", ""),
                    "example_phrases": p.get("example_phrases", []),
                    "query_template": p.get("query_template", {})
                }
                for p in self.patterns
            ]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NLEncoderConfig':
        """
        Create NLEncoderConfig from dictionary.
        
        Args:
            data: Dictionary containing NL encoder config fields
        
        Returns:
            NLEncoderConfig instance
        
        Example:
            >>> data = {"patterns": [{"intent_type": "search", "example_phrases": ["find"], "query_template": {}}]}
            >>> config = NLEncoderConfig.from_dict(data)
        """
        return cls(patterns=data.get("patterns", []))
    
    def add_pattern(
        self,
        intent_type: str,
        example_phrases: List[str],
        query_template: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a new intent pattern.
        
        Args:
            intent_type: Unique identifier for the intent
            example_phrases: List of example phrases that match this intent
            query_template: Structured query template (default: empty dict)
        
        Raises:
            ConfigurationException: If intent_type already exists or validation fails
        
        Example:
            >>> nl_config = NLEncoderConfig()
            >>> nl_config.add_pattern(
            ...     intent_type="find_customer",
            ...     example_phrases=["find customer", "lookup customer"],
            ...     query_template={"operation": "similarity_search"}
            ... )
        """
        # Check for duplicate
        existing_types = [p.get("intent_type") for p in self.patterns]
        if intent_type in existing_types:
            raise ConfigurationException(
                "patterns",
                f"Pattern with intent_type '{intent_type}' already exists"
            )
        
        if not intent_type:
            raise ConfigurationException(
                "patterns",
                "intent_type cannot be empty"
            )
        
        if not example_phrases:
            raise ConfigurationException(
                "patterns",
                f"Pattern '{intent_type}' must have at least one example phrase"
            )
        
        self.patterns.append({
            "intent_type": intent_type,
            "example_phrases": example_phrases,
            "query_template": query_template or {}
        })
    
    def remove_pattern(self, intent_type: str) -> bool:
        """
        Remove a pattern by intent_type.
        
        Args:
            intent_type: The intent type to remove
        
        Returns:
            True if pattern was removed, False if not found
        
        Example:
            >>> nl_config.remove_pattern("find_customer")
        """
        original_len = len(self.patterns)
        self.patterns = [p for p in self.patterns if p.get("intent_type") != intent_type]
        return len(self.patterns) < original_len
    
    def get_pattern(self, intent_type: str) -> Optional[Dict[str, Any]]:
        """
        Get a pattern by intent_type.
        
        Args:
            intent_type: The intent type to look up
        
        Returns:
            Pattern dictionary if found, None otherwise
        
        Example:
            >>> pattern = nl_config.get_pattern("find_customer")
        """
        for pattern in self.patterns:
            if pattern.get("intent_type") == intent_type:
                return pattern
        return None
    
    def __len__(self) -> int:
        """Return the number of patterns."""
        return len(self.patterns)
    
    def __repr__(self) -> str:
        """String representation of the NL encoder config."""
        return f"NLEncoderConfig(patterns={len(self.patterns)})"


# ============================================================================
# GQLPatternsConfig Dataclass
# ============================================================================

@dataclass
class GQLPatternsConfig:
    """
    Configuration for GQL patterns that translate NL queries to GQL.
    
    GQLPatternsConfig holds a list of GQL patterns that define how natural
    language queries are matched and translated to GQL queries. This configuration
    travels with the model in the .glyphh file.
    
    Attributes:
        patterns: List of GQL pattern dictionaries, each containing:
            - name: Unique pattern name
            - phrases: List of example phrases with {slot} placeholders
            - slots: List of slot definitions
            - gql_template: GQL template with {slot} placeholders
            - description: Optional description
            - priority: Pattern priority (higher = matched first)
    
    Example:
        >>> gql_config = GQLPatternsConfig(patterns=[
        ...     {
        ...         "name": "find_similar",
        ...         "phrases": ["find similar to {query}", "search for {query}"],
        ...         "slots": [{"name": "query", "type": "string", "required": True}],
        ...         "gql_template": 'FIND SIMILAR TO "{query}" LIMIT 10'
        ...     }
        ... ])
    """
    patterns: List[Dict[str, Any]] = field(default_factory=list)
    
    def __post_init__(self):
        """Validate GQL patterns config on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all GQL patterns config fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if not isinstance(self.patterns, list):
            raise ConfigurationException(
                "patterns",
                f"Must be a list, got {type(self.patterns).__name__}"
            )
        
        # Validate unique pattern names
        pattern_names = []
        for i, pattern in enumerate(self.patterns):
            if not isinstance(pattern, dict):
                raise ConfigurationException(
                    "patterns",
                    f"Pattern at index {i} must be a dictionary"
                )
            
            name = pattern.get("name")
            if not name:
                raise ConfigurationException(
                    "patterns",
                    f"Pattern at index {i} must have a non-empty 'name'"
                )
            
            if name in pattern_names:
                raise ConfigurationException(
                    "patterns",
                    f"Duplicate pattern name '{name}' found. Pattern names must be unique."
                )
            pattern_names.append(name)
            
            # Validate phrases
            phrases = pattern.get("phrases", [])
            if not phrases:
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{name}' must have at least one phrase"
                )
            if not isinstance(phrases, list):
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{name}' phrases must be a list"
                )
            
            # Validate gql_template
            gql_template = pattern.get("gql_template")
            if not gql_template:
                raise ConfigurationException(
                    "patterns",
                    f"Pattern '{name}' must have a gql_template"
                )
    
    def to_dict(self) -> dict:
        """
        Serialize GQL patterns config to dictionary.
        
        Returns:
            Dictionary representation of the GQL patterns config
        """
        return {
            "patterns": [
                {
                    "name": p.get("name", ""),
                    "phrases": p.get("phrases", []),
                    "slots": p.get("slots", []),
                    "gql_template": p.get("gql_template", ""),
                    "description": p.get("description"),
                    "priority": p.get("priority", 0)
                }
                for p in self.patterns
            ]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GQLPatternsConfig':
        """
        Create GQLPatternsConfig from dictionary.
        
        Args:
            data: Dictionary containing GQL patterns config fields
        
        Returns:
            GQLPatternsConfig instance
        """
        return cls(patterns=data.get("patterns", []))
    
    def add_pattern(
        self,
        name: str,
        phrases: List[str],
        gql_template: str,
        slots: Optional[List[Dict[str, Any]]] = None,
        description: Optional[str] = None,
        priority: int = 0
    ) -> None:
        """
        Add a new GQL pattern.
        
        Args:
            name: Unique pattern name
            phrases: List of example phrases with {slot} placeholders
            gql_template: GQL template with {slot} placeholders
            slots: List of slot definitions
            description: Optional description
            priority: Pattern priority
        
        Raises:
            ConfigurationException: If name already exists or validation fails
        """
        existing_names = [p.get("name") for p in self.patterns]
        if name in existing_names:
            raise ConfigurationException(
                "patterns",
                f"Pattern with name '{name}' already exists"
            )
        
        if not name:
            raise ConfigurationException(
                "patterns",
                "Pattern name cannot be empty"
            )
        
        if not phrases:
            raise ConfigurationException(
                "patterns",
                f"Pattern '{name}' must have at least one phrase"
            )
        
        if not gql_template:
            raise ConfigurationException(
                "patterns",
                f"Pattern '{name}' must have a gql_template"
            )
        
        self.patterns.append({
            "name": name,
            "phrases": phrases,
            "slots": slots or [],
            "gql_template": gql_template,
            "description": description,
            "priority": priority
        })
    
    def remove_pattern(self, name: str) -> bool:
        """
        Remove a pattern by name.
        
        Args:
            name: The pattern name to remove
        
        Returns:
            True if pattern was removed, False if not found
        """
        original_len = len(self.patterns)
        self.patterns = [p for p in self.patterns if p.get("name") != name]
        return len(self.patterns) < original_len
    
    def get_pattern(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get a pattern by name.
        
        Args:
            name: The pattern name to look up
        
        Returns:
            Pattern dictionary if found, None otherwise
        """
        for pattern in self.patterns:
            if pattern.get("name") == name:
                return pattern
        return None
    
    def to_gql_patterns(self) -> List['GQLPattern']:
        """
        Convert to list of GQLPattern objects.
        
        Returns:
            List of GQLPattern instances
        """
        from glyphh.gql.patterns import GQLPattern, SlotDefinition, SlotType
        
        result = []
        for p in self.patterns:
            slots = []
            for s in p.get("slots", []):
                slots.append(SlotDefinition(
                    name=s.get("name", ""),
                    type=SlotType(s.get("type", "string")),
                    required=s.get("required", True),
                    default=s.get("default"),
                    enum_values=s.get("enum_values"),
                    description=s.get("description")
                ))
            
            result.append(GQLPattern(
                name=p.get("name", ""),
                phrases=p.get("phrases", []),
                slots=slots,
                gql_template=p.get("gql_template", ""),
                description=p.get("description"),
                priority=p.get("priority", 0)
            ))
        
        return result
    
    def __len__(self) -> int:
        """Return the number of patterns."""
        return len(self.patterns)
    
    def __repr__(self) -> str:
        """String representation of the GQL patterns config."""
        return f"GQLPatternsConfig(patterns={len(self.patterns)})"


# ============================================================================
# TemporalConfig Dataclass
# ============================================================================

@dataclass
class TemporalConfig:
    """
    Configuration for temporal signal interpretation.
    
    TemporalConfig specifies how temporal data from a temporal role is
    interpreted and formatted for glyph identifiers.
    
    Attributes:
        signal_type: How to interpret temporal data
            - "auto": Auto-generate ISO8601 timestamps (default)
            - "datetime": Parse using format string
            - "epoch": Unix epoch seconds
            - "sequence": Monotonic sequence number
            - "version": Version string (e.g., "v1", "1.0.0")
        format: Format string for datetime parsing (required when signal_type="datetime")
        auto_increment: Auto-increment version on duplicate keys (default: True)
    
    Example:
        >>> config = TemporalConfig(signal_type="auto")
        >>> config = TemporalConfig(signal_type="datetime", format="%Y-%m-%d %H:%M:%S")
        >>> config = TemporalConfig(signal_type="epoch")
    """
    signal_type: str = "auto"
    format: Optional[str] = None
    auto_increment: bool = True
    
    VALID_SIGNAL_TYPES = {"auto", "datetime", "epoch", "sequence", "version"}
    
    def __post_init__(self):
        """Validate temporal config on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all temporal config fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        if self.signal_type not in self.VALID_SIGNAL_TYPES:
            raise ConfigurationException(
                "signal_type",
                f"Must be one of {self.VALID_SIGNAL_TYPES}, got '{self.signal_type}'"
            )
        
        if self.signal_type == "datetime" and not self.format:
            raise ConfigurationException(
                "format",
                "Format string is required when signal_type is 'datetime'"
            )
    
    def to_dict(self) -> dict:
        """Serialize temporal config to dictionary."""
        return {
            "signal_type": self.signal_type,
            "format": self.format,
            "auto_increment": self.auto_increment
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'TemporalConfig':
        """Create TemporalConfig from dictionary."""
        return cls(
            signal_type=data.get("signal_type", "auto"),
            format=data.get("format"),
            auto_increment=data.get("auto_increment", True)
        )
    
    def __repr__(self) -> str:
        """String representation of the temporal config."""
        return f"TemporalConfig(signal_type='{self.signal_type}', format={self.format!r}, auto_increment={self.auto_increment})"


# ============================================================================
# EncoderConfig
# ============================================================================

@dataclass
class EncoderConfig:
    """
    Configuration for encoder initialization with explicit nested structure.
    
    The EncoderConfig defines the complete hierarchical structure for encoding:
    Config (cortex) → Layers → Segments → Roles
    
    Each level has its own similarity and security weights.
    
    Required Fields:
        dimension: Vector dimension (must be positive integer)
        seed: Random seed for deterministic generation (must be non-negative)
    
    Optional Fields:
        similarity_weight: Cortex-level similarity weight (0.0-1.0, default: 1.0)
        security_weight: Cortex-level security weight (0.0-1.0, default: 1.0)
        apply_weights_during_encoding: Whether to apply weights during encoding (default: False)
        include_temporal: Whether to include the automatic _temporal layer (default: True).
            Set to False for models where temporal signal is irrelevant (e.g., function routing).
        layers: List of Layer definitions (default: empty list)
        nl_encoder_config: NL encoder configuration for intent patterns (default: None)
        gql_patterns: GQL patterns configuration for NL-to-GQL translation (default: None)
        temporal_config: Temporal signal configuration (default: None, uses auto timestamps)
        temporal_source: Source for temporal signal (default: "auto")
            - "auto": Auto-generate ISO8601 timestamps
            - "layer.segment.role": Use a specific role's value as the temporal signal
    
    Temporal Layer:
        Every glyph has a dedicated `_temporal` layer that encodes the temporal signal.
        This layer is automatically created during encoding. The temporal_source field
        specifies where the temporal value comes from:
        - "auto" (default): Uses current datetime in ISO8601 format
        - "semantic.attributes.year": Uses the value from the "year" role in the
          "attributes" segment of the "semantic" layer
    
    Weighting Strategy:
        The `apply_weights_during_encoding` flag controls when weights are applied:
        
        - False (default): Weights stored in glyph, applied during similarity computation
        - True: Weights applied during encoding via weighted bundling
    
    Example:
        >>> config = EncoderConfig(
        ...     dimension=10000,
        ...     seed=42,
        ...     temporal_source="semantic.attributes.year",  # Use year role as temporal signal
        ...     layers=[
        ...         Layer(
        ...             name="semantic",
        ...             similarity_weight=0.8,
        ...             segments=[
        ...                 Segment(
        ...                     name="attributes",
        ...                     roles=[
        ...                         Role(name="question", key_part=True),
        ...                         Role(name="answer"),
        ...                         Role(name="year"),  # This will be used as temporal signal
        ...                     ]
        ...                 )
        ...             ]
        ...         )
        ...     ],
        ...     temporal_config=TemporalConfig(signal_type="sequence"),  # Interpret year as sequence
        ... )
    """
    dimension: int
    seed: int
    similarity_weight: float = 1.0
    security_weight: float = 1.0
    apply_weights_during_encoding: bool = False
    include_temporal: bool = True
    layers: List[Layer] = field(default_factory=list)
    nl_encoder_config: Optional[NLEncoderConfig] = field(default=None)
    gql_patterns: Optional[GQLPatternsConfig] = field(default=None)
    temporal_config: Optional[TemporalConfig] = field(default=None)
    temporal_source: str = "auto"  # "auto" or "layer.segment.role" path
    
    def __post_init__(self):
        """Validate configuration on initialization."""
        self._validate()
    
    def _validate(self):
        """
        Validate all configuration fields.
        
        Raises:
            ConfigurationException: If any validation fails
        """
        # Validate dimension
        if not isinstance(self.dimension, int):
            raise ConfigurationException(
                "dimension",
                f"Dimension must be an integer, got {type(self.dimension).__name__}"
            )
        if self.dimension <= 0:
            raise ConfigurationException(
                "dimension",
                f"Dimension must be positive, got {self.dimension}"
            )
        
        # Validate seed
        if not isinstance(self.seed, int):
            raise ConfigurationException(
                "seed",
                f"Seed must be an integer, got {type(self.seed).__name__}"
            )
        if self.seed < 0:
            raise ConfigurationException(
                "seed",
                f"Seed must be non-negative, got {self.seed}"
            )
        
        # Validate cortex-level similarity_weight
        if not isinstance(self.similarity_weight, (int, float)):
            raise ConfigurationException(
                "similarity_weight",
                f"Must be a number, got {type(self.similarity_weight).__name__}"
            )
        if not 0.0 <= self.similarity_weight <= 1.0:
            raise ConfigurationException(
                "similarity_weight",
                f"Must be between 0.0 and 1.0, got {self.similarity_weight}"
            )
        
        # Validate cortex-level security_weight
        if not isinstance(self.security_weight, (int, float)):
            raise ConfigurationException(
                "security_weight",
                f"Must be a number, got {type(self.security_weight).__name__}"
            )
        if not 0.0 <= self.security_weight <= 1.0:
            raise ConfigurationException(
                "security_weight",
                f"Must be between 0.0 and 1.0, got {self.security_weight}"
            )
        
        # Validate apply_weights_during_encoding
        if not isinstance(self.apply_weights_during_encoding, bool):
            raise ConfigurationException(
                "apply_weights_during_encoding",
                f"Must be a boolean, got {type(self.apply_weights_during_encoding).__name__}"
            )
        
        # Validate include_temporal
        if not isinstance(self.include_temporal, bool):
            raise ConfigurationException(
                "include_temporal",
                f"Must be a boolean, got {type(self.include_temporal).__name__}"
            )
        
        # Validate layers is a list
        if not isinstance(self.layers, list):
            raise ConfigurationException(
                "layers",
                f"Must be a list, got {type(self.layers).__name__}"
            )
        
        # Validate layer names are unique
        layer_names = [layer.name for layer in self.layers]
        if len(layer_names) != len(set(layer_names)):
            raise ConfigurationException(
                "layers",
                "Layer names must be unique"
            )
        
        # Validate temporal_source format
        if not isinstance(self.temporal_source, str):
            raise ConfigurationException(
                "temporal_source",
                f"Must be a string, got {type(self.temporal_source).__name__}"
            )
        if self.temporal_source != "auto":
            # Must be in format "layer.segment.role"
            parts = self.temporal_source.split(".")
            if len(parts) != 3:
                raise ConfigurationException(
                    "temporal_source",
                    f"Must be 'auto' or 'layer.segment.role' format, got '{self.temporal_source}'"
                )
            # Validate the path exists in layers (if layers are defined)
            if self.layers:
                layer_name, segment_name, role_name = parts
                layer = next((l for l in self.layers if l.name == layer_name), None)
                if not layer:
                    raise ConfigurationException(
                        "temporal_source",
                        f"Layer '{layer_name}' not found in config"
                    )
                segment = next((s for s in layer.segments if s.name == segment_name), None)
                if not segment:
                    raise ConfigurationException(
                        "temporal_source",
                        f"Segment '{segment_name}' not found in layer '{layer_name}'"
                    )
                role = next((r for r in segment.roles if r.name == role_name), None)
                if not role:
                    raise ConfigurationException(
                        "temporal_source",
                        f"Role '{role_name}' not found in segment '{segment_name}'"
                    )
    
    def to_dict(self) -> dict:
        """
        Serialize configuration to dictionary.
        
        Returns:
            Dictionary representation of the configuration
        """
        result = {
            "dimension": self.dimension,
            "seed": self.seed,
            "similarity_weight": self.similarity_weight,
            "security_weight": self.security_weight,
            "apply_weights_during_encoding": self.apply_weights_during_encoding,
            "include_temporal": self.include_temporal,
            "layers": [layer.to_dict() for layer in self.layers],
            "temporal_source": self.temporal_source,
        }
        
        # Include nl_encoder_config if present
        if self.nl_encoder_config is not None:
            if isinstance(self.nl_encoder_config, dict):
                result["nl_encoder_config"] = self.nl_encoder_config
            else:
                result["nl_encoder_config"] = self.nl_encoder_config.to_dict()
        
        # Include gql_patterns if present
        if self.gql_patterns is not None:
            if isinstance(self.gql_patterns, dict):
                result["gql_patterns"] = self.gql_patterns
            else:
                result["gql_patterns"] = self.gql_patterns.to_dict()
        
        # Include temporal_config if present
        if self.temporal_config is not None:
            if isinstance(self.temporal_config, dict):
                result["temporal_config"] = self.temporal_config
            else:
                result["temporal_config"] = self.temporal_config.to_dict()
        
        return result
    
    def to_json(self) -> str:
        """
        Serialize configuration to JSON string for space_id computation.
        
        The JSON string is deterministically generated with sorted keys to ensure
        that identical configurations always produce the same space_id.
        
        Returns:
            JSON string representation of the configuration
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42)
            >>> json_str = config.to_json()
        """
        return json.dumps(self.to_dict(), sort_keys=True)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EncoderConfig':
        """
        Create EncoderConfig from dictionary.
        
        Args:
            data: Dictionary containing configuration fields
        
        Returns:
            EncoderConfig instance
        
        Raises:
            ConfigurationException: If required fields are missing or invalid
        
        Example:
            >>> data = {"dimension": 10000, "seed": 42}
            >>> config = EncoderConfig.from_dict(data)
        """
        # Check for required fields
        required_fields = ["dimension", "seed"]
        missing_fields = [f for f in required_fields if f not in data]
        
        if missing_fields:
            raise ConfigurationException(
                "required_fields",
                f"Missing required fields: {missing_fields}"
            )
        
        # Parse layers if present
        layers = [Layer.from_dict(l) for l in data.get("layers", [])]
        
        # Parse nl_encoder_config if present
        nl_encoder_config = None
        if "nl_encoder_config" in data and data["nl_encoder_config"] is not None:
            nl_encoder_config = NLEncoderConfig.from_dict(data["nl_encoder_config"])
        
        # Parse gql_patterns if present
        gql_patterns = None
        if "gql_patterns" in data and data["gql_patterns"] is not None:
            gql_patterns = GQLPatternsConfig.from_dict(data["gql_patterns"])
        
        # Parse temporal_config if present
        temporal_config = None
        if "temporal_config" in data and data["temporal_config"] is not None:
            temporal_config = TemporalConfig.from_dict(data["temporal_config"])
        
        return cls(
            dimension=data["dimension"],
            seed=data["seed"],
            similarity_weight=data.get("similarity_weight", 1.0),
            security_weight=data.get("security_weight", 1.0),
            apply_weights_during_encoding=data.get("apply_weights_during_encoding", False),
            include_temporal=data.get("include_temporal", True),
            layers=layers,
            nl_encoder_config=nl_encoder_config,
            gql_patterns=gql_patterns,
            temporal_config=temporal_config,
            temporal_source=data.get("temporal_source", "auto"),
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> 'EncoderConfig':
        """
        Create EncoderConfig from JSON string.
        
        Args:
            json_str: JSON string containing configuration
        
        Returns:
            EncoderConfig instance
        
        Raises:
            ConfigurationException: If JSON is invalid or fields are missing
        
        Example:
            >>> json_str = '{"dimension": 10000, "seed": 42}'
            >>> config = EncoderConfig.from_json(json_str)
        """
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ConfigurationException(
                "json_format",
                f"Invalid JSON format: {e}"
            )
        
        return cls.from_dict(data)
    
    @classmethod
    def from_file(cls, path: str) -> 'EncoderConfig':
        """
        Load EncoderConfig from JSON file.
        
        Args:
            path: Path to JSON configuration file
        
        Returns:
            EncoderConfig instance
        
        Raises:
            ConfigurationException: If file cannot be read or is invalid
        
        Example:
            >>> config = EncoderConfig.from_file("config.json")
        """
        try:
            with open(path, 'r') as f:
                json_str = f.read()
        except FileNotFoundError:
            raise ConfigurationException(
                "file_path",
                f"Configuration file not found: {path}"
            )
        except IOError as e:
            raise ConfigurationException(
                "file_read",
                f"Failed to read configuration file: {e}"
            )
        
        return cls.from_json(json_str)
    
    def to_file(self, path: str):
        """
        Save EncoderConfig to JSON file.
        
        Args:
            path: Path to save JSON configuration file
        
        Raises:
            ConfigurationException: If file cannot be written
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42)
            >>> config.to_file("config.json")
        """
        try:
            with open(path, 'w') as f:
                f.write(self.to_json())
        except IOError as e:
            raise ConfigurationException(
                "file_write",
                f"Failed to write configuration file: {e}"
            )
    
    def get_all_roles(self) -> List[Role]:
        """
        Get all roles across all layers and segments.
        
        Returns:
            List of all Role instances in the config
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42, layers=[...])
            >>> roles = config.get_all_roles()
        """
        return [
            role
            for layer in self.layers
            for segment in layer.segments
            for role in segment.roles
        ]
    
    def get_key_part_roles(self) -> List[Role]:
        """
        Get all roles marked as key_part (composite primary key).
        
        Returns:
            List of Role objects with key_part=True, in segment order
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42, layers=[...])
            >>> key_roles = config.get_key_part_roles()
            >>> for role in key_roles:
            ...     print(f"Key part: {role.name}")
        """
        return [role for role in self.get_all_roles() if role.key_part]
    
    def get_temporal_source_role(self) -> Optional[Role]:
        """
        Get the role used as temporal source, if temporal_source points to a role.
        
        Returns:
            The Role specified by temporal_source, or None if temporal_source is "auto"
        
        Example:
            >>> config = EncoderConfig(
            ...     dimension=10000, seed=42,
            ...     temporal_source="semantic.attributes.year",
            ...     layers=[...]
            ... )
            >>> temporal_role = config.get_temporal_source_role()
            >>> if temporal_role:
            ...     print(f"Temporal source: {temporal_role.name}")
        """
        if self.temporal_source == "auto":
            return None
        
        parts = self.temporal_source.split(".")
        if len(parts) != 3:
            return None
        
        layer_name, segment_name, role_name = parts
        for layer in self.layers:
            if layer.name == layer_name:
                for segment in layer.segments:
                    if segment.name == segment_name:
                        for role in segment.roles:
                            if role.name == role_name:
                                return role
        return None
    
    def get_primary_role(self) -> Optional[Role]:
        """
        Get the first key_part role (for backward compatibility).
        
        Deprecated: Use get_key_part_roles() for composite keys.
        
        Returns:
            The first Role with key_part=True, or None if no key_part role exists
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42, layers=[...])
            >>> primary = config.get_primary_role()
            >>> if primary:
            ...     print(f"Primary role: {primary.name}")
        """
        key_roles = self.get_key_part_roles()
        return key_roles[0] if key_roles else None


# ============================================================================
# Migration Utility
# ============================================================================

def migrate_legacy_config(
    dimension: int,
    seed: int,
    num_layers: int = 1,
    segments_per_layer: int = 2,
    default_roles: List[str] = None,
    similarity_weights: Dict[str, float] = None,
    security_weights: Dict[str, float] = None
) -> EncoderConfig:
    """
    Migrate from legacy count-based config to new explicit structure.
    
    Creates a simple structure with:
    - N layers named "layer_0", "layer_1", etc.
    - M segments per layer named "segment_0", "segment_1", etc.
    - Roles from default_roles list
    
    Args:
        dimension: Vector dimension
        seed: Random seed
        num_layers: Number of layers to create
        segments_per_layer: Number of segments per layer
        default_roles: Role names (default: ["type", "value"])
        similarity_weights: Legacy hierarchy-level weights
        security_weights: Legacy hierarchy-level security weights
    
    Returns:
        New EncoderConfig with explicit structure
    
    Example:
        >>> # Migrate old-style config
        >>> config = migrate_legacy_config(
        ...     dimension=10000,
        ...     seed=42,
        ...     num_layers=2,
        ...     segments_per_layer=3,
        ...     default_roles=["type", "value", "category"],
        ...     similarity_weights={"cortex": 1.0, "layer": 0.8}
        ... )
    """
    if default_roles is None:
        default_roles = ["type", "value"]
    if similarity_weights is None:
        similarity_weights = {}
    if security_weights is None:
        security_weights = {}
    
    # Create layers
    layers = []
    for layer_idx in range(num_layers):
        # Create segments for this layer
        segments = []
        for seg_idx in range(segments_per_layer):
            # Create roles from default_roles
            roles = [
                Role(
                    name=role_name,
                    similarity_weight=similarity_weights.get("role", 1.0),
                    security_weight=security_weights.get("role", 1.0)
                )
                for role_name in default_roles
            ]
            
            segments.append(Segment(
                name=f"segment_{seg_idx}",
                similarity_weight=similarity_weights.get("segment", 1.0),
                security_weight=security_weights.get("segment", 1.0),
                roles=roles
            ))
        
        layers.append(Layer(
            name=f"layer_{layer_idx}",
            similarity_weight=similarity_weights.get("layer", 1.0),
            security_weight=security_weights.get("layer", 1.0),
            segments=segments
        ))
    
    return EncoderConfig(
        dimension=dimension,
        seed=seed,
        similarity_weight=similarity_weights.get("cortex", 1.0),
        security_weight=security_weights.get("cortex", 1.0),
        layers=layers
    )
