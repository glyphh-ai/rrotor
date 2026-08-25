"""
Intent Pattern Encoder for NL Query Matching.

This module provides deterministic rules-based NL query matching using HDC similarity.
The core value proposition: "When your LLM can't afford to be wrong, sidecar it with Glyphh."

Key Design Principles:
1. No JSON config files - patterns defined in Python code (notebook.py)
2. Patterns travel with model - serialized in .glyphh file
3. Simplicity - just `add_pattern()` and `match_intent()`
4. Determinism - same query always matches the same intent
5. Extensibility - model authors add domain-specific patterns in code

Usage in notebook.py:
    >>> from glyphh import IntentEncoder, IntentPattern, EncoderConfig
    >>> 
    >>> config = EncoderConfig(dimension=10000, seed=42)
    >>> intent_encoder = IntentEncoder(config)
    >>> 
    >>> # Define patterns in Python code (no JSON!)
    >>> intent_encoder.add_pattern(IntentPattern(
    ...     intent_type="find_customer",
    ...     example_phrases=["find customer", "lookup customer", "show customer"],
    ...     query_template={"operation": "similarity_search", "entity_type": "customer"}
    ... ))
    >>> 
    >>> # Test locally
    >>> match = intent_encoder.match_intent("find customer John")
    >>> print(f"Matched: {match.intent_type} (confidence: {match.confidence:.2f})")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import numpy as np

from glyphh.core.types import Vector
from glyphh.core.config import EncoderConfig
from glyphh.encoder.base import Encoder


@dataclass
class IntentPattern:
    """
    A pattern for matching natural language queries to structured operations.
    
    IntentPatterns are defined in Python code (notebook.py) and bundled with
    the model in the .glyphh file. No separate JSON configuration files needed.
    
    Attributes:
        intent_type: Unique identifier for this intent (e.g., "similarity_search", "find_customer")
        example_phrases: List of example phrases that should match this intent
        query_template: Structured query template to execute when matched
    
    Example:
        >>> pattern = IntentPattern(
        ...     intent_type="find_customer",
        ...     example_phrases=["find customer", "lookup customer", "show customer", "get customer"],
        ...     query_template={"operation": "similarity_search", "entity_type": "customer"}
        ... )
    """
    intent_type: str
    example_phrases: List[str]
    query_template: Dict[str, Any]
    
    def __post_init__(self):
        """Validate pattern on creation."""
        if not self.intent_type:
            raise ValueError("intent_type cannot be empty")
        if not self.example_phrases:
            raise ValueError("example_phrases cannot be empty")
        if not isinstance(self.query_template, dict):
            raise ValueError("query_template must be a dictionary")


@dataclass
class IntentMatch:
    """
    Result of matching a query against intent patterns.
    
    Returned by IntentEncoder.match_intent() with the best matching intent
    and confidence score based on HDC similarity.
    
    Attributes:
        intent_type: The matched intent type
        confidence: Similarity score (0.0 to 1.0, higher is better)
        structured_query: The filled-in query template ready for execution
        matched_phrase: The example phrase that matched best
    
    Example:
        >>> match = intent_encoder.match_intent("find customer John")
        >>> if match.is_high_confidence():
        ...     result = execute_query(match.structured_query)
        ... else:
        ...     result = llm_fallback(query)
    """
    intent_type: str
    confidence: float
    structured_query: Dict[str, Any]
    matched_phrase: str
    
    def is_high_confidence(self, threshold: float = 0.85) -> bool:
        """
        Check if the match confidence exceeds the threshold.
        
        Args:
            threshold: Confidence threshold (default 0.85)
        
        Returns:
            True if confidence >= threshold
        
        Example:
            >>> if match.is_high_confidence():
            ...     # Execute directly - deterministic, no LLM needed
            ...     result = query_service.execute(match.structured_query)
            ... else:
            ...     # Fall back to LLM for translation
            ...     result = llm_fallback.translate_and_execute(query)
        """
        return self.confidence >= threshold
    
    def __repr__(self) -> str:
        return (
            f"IntentMatch(intent_type='{self.intent_type}', "
            f"confidence={self.confidence:.4f}, "
            f"matched_phrase='{self.matched_phrase}')"
        )



class IntentEncoder(Encoder):
    """
    Encoder for intent pattern matching using HDC similarity.
    
    The IntentEncoder extends the base Encoder to provide deterministic
    rules-based NL query matching. Patterns are defined in Python code
    and bundled with the model - no JSON configuration files needed.
    
    Key Features:
    - Simple API: just add_pattern() and match_intent()
    - Deterministic: same query always matches the same intent
    - Efficient: intent vectors are cached after encoding
    - Portable: patterns serialize with the .glyphh model file
    
    Example:
        >>> from glyphh import IntentEncoder, IntentPattern, EncoderConfig
        >>> 
        >>> config = EncoderConfig(dimension=10000, seed=42)
        >>> intent_encoder = IntentEncoder(config)
        >>> 
        >>> # Add default patterns for common Glyphh operations
        >>> intent_encoder.add_defaults()
        >>> 
        >>> # Add domain-specific patterns
        >>> intent_encoder.add_pattern(IntentPattern(
        ...     intent_type="find_customer",
        ...     example_phrases=["find customer", "lookup customer", "show customer"],
        ...     query_template={"operation": "similarity_search", "entity_type": "customer"}
        ... ))
        >>> 
        >>> # Match a query
        >>> match = intent_encoder.match_intent("find customer John")
        >>> print(f"Matched: {match.intent_type} (confidence: {match.confidence:.2f})")
    """
    
    def __init__(self, config: EncoderConfig):
        """
        Initialize the IntentEncoder.
        
        Args:
            config: EncoderConfig with dimension, seed, and other parameters
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42)
            >>> intent_encoder = IntentEncoder(config)
        """
        super().__init__(config)
        
        # Registered intent patterns
        self._patterns: List[IntentPattern] = []
        
        # Cached intent vectors (intent_type → bundled vector)
        self._intent_vectors: Dict[str, Vector] = {}
        
        # Mapping from intent_type to pattern for quick lookup
        self._pattern_map: Dict[str, IntentPattern] = {}
    
    def add_pattern(self, pattern: IntentPattern) -> None:
        """
        Add an intent pattern.
        
        Call this in notebook.py to define patterns in Python code.
        Each pattern's example phrases are encoded and bundled into
        a single intent vector for efficient matching.
        
        Args:
            pattern: IntentPattern to register
        
        Raises:
            ValueError: If pattern with same intent_type already exists
        
        Example:
            >>> intent_encoder.add_pattern(IntentPattern(
            ...     intent_type="find_customer",
            ...     example_phrases=["find customer", "lookup customer", "show customer"],
            ...     query_template={"operation": "similarity_search", "entity_type": "customer"}
            ... ))
        """
        # Check for duplicate intent_type
        if pattern.intent_type in self._pattern_map:
            raise ValueError(
                f"Pattern with intent_type '{pattern.intent_type}' already exists. "
                f"Use a unique intent_type for each pattern."
            )
        
        # Store pattern
        self._patterns.append(pattern)
        self._pattern_map[pattern.intent_type] = pattern
        
        # Encode each example phrase, bundle into single intent vector
        phrase_vectors = []
        for phrase in pattern.example_phrases:
            phrase_vector = self._encode_phrase(phrase)
            phrase_vectors.append(phrase_vector)
        
        # Bundle all phrase vectors into a single intent vector
        intent_vector = self.bundle(phrase_vectors)
        self._intent_vectors[pattern.intent_type] = intent_vector
    
    def _encode_phrase(self, phrase: str) -> Vector:
        """
        Encode a phrase into a vector by encoding words and bundling.
        
        Args:
            phrase: Natural language phrase to encode
        
        Returns:
            Bundled vector representing the phrase
        """
        # Tokenize: lowercase and split on whitespace
        words = phrase.lower().split()
        
        if not words:
            # Empty phrase - return a random vector (will have low similarity)
            return self.generate_symbol("__empty__")
        
        # Generate symbol vector for each word
        word_vectors = [self.generate_symbol(word) for word in words]
        
        # Bundle word vectors into phrase vector
        return self.bundle(word_vectors)
    
    def match_intent(
        self,
        query: str,
        threshold: float = 0.85
    ) -> IntentMatch:
        """
        Match a query against loaded intent patterns.
        
        Encodes the query and computes HDC similarity against all
        cached intent vectors. Returns the best match with confidence.
        
        Args:
            query: Natural language query to match
            threshold: Confidence threshold (used in IntentMatch.is_high_confidence())
        
        Returns:
            IntentMatch with best matching intent and confidence score
        
        Raises:
            ValueError: If no patterns have been registered
        
        Example:
            >>> match = intent_encoder.match_intent("find customer John")
            >>> if match.is_high_confidence():
            ...     result = query_service.execute(match.structured_query)
            ... else:
            ...     result = llm_fallback.translate_and_execute(query)
        """
        if not self._patterns:
            raise ValueError(
                "No intent patterns registered. "
                "Call add_pattern() or add_defaults() first."
            )
        
        # Encode the query
        query_vector = self._encode_phrase(query)
        
        # Find best matching intent
        best_type: Optional[str] = None
        best_score: float = -1.0
        best_phrase: str = ""
        
        for intent_type, intent_vector in self._intent_vectors.items():
            # Compute cosine similarity
            score = self._cosine_similarity(query_vector.data, intent_vector.data)
            
            # Normalize to [0, 1] range (cosine is [-1, 1])
            normalized_score = (score + 1.0) / 2.0
            
            if normalized_score > best_score:
                best_score = normalized_score
                best_type = intent_type
                # Get the first example phrase as the "matched" phrase
                best_phrase = self._pattern_map[intent_type].example_phrases[0]
        
        # Get pattern and build result
        pattern = self._pattern_map[best_type]
        
        return IntentMatch(
            intent_type=best_type,
            confidence=best_score,
            structured_query=pattern.query_template.copy(),
            matched_phrase=best_phrase
        )
    
    def _cosine_similarity(self, v1: np.ndarray, v2: np.ndarray) -> float:
        """
        Compute cosine similarity between two vectors.
        
        Args:
            v1: First vector
            v2: Second vector
        
        Returns:
            Cosine similarity in range [-1, 1]
        """
        dot_product = np.dot(v1.astype(np.float64), v2.astype(np.float64))
        norm1 = np.linalg.norm(v1.astype(np.float64))
        norm2 = np.linalg.norm(v2.astype(np.float64))
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)
    
    def get_patterns(self) -> List[IntentPattern]:
        """
        Get all registered patterns.
        
        Returns:
            Copy of the list of registered patterns
        
        Example:
            >>> patterns = intent_encoder.get_patterns()
            >>> for p in patterns:
            ...     print(f"{p.intent_type}: {len(p.example_phrases)} phrases")
        """
        return self._patterns.copy()
    
    def get_pattern(self, intent_type: str) -> Optional[IntentPattern]:
        """
        Get a specific pattern by intent_type.
        
        Args:
            intent_type: The intent type to look up
        
        Returns:
            IntentPattern if found, None otherwise
        
        Example:
            >>> pattern = intent_encoder.get_pattern("find_customer")
            >>> if pattern:
            ...     print(f"Found: {pattern.example_phrases}")
        """
        return self._pattern_map.get(intent_type)
    
    def remove_pattern(self, intent_type: str) -> bool:
        """
        Remove a pattern by intent_type.
        
        Args:
            intent_type: The intent type to remove
        
        Returns:
            True if pattern was removed, False if not found
        
        Example:
            >>> intent_encoder.remove_pattern("find_customer")
        """
        if intent_type not in self._pattern_map:
            return False
        
        # Remove from all data structures
        del self._pattern_map[intent_type]
        del self._intent_vectors[intent_type]
        self._patterns = [p for p in self._patterns if p.intent_type != intent_type]
        
        return True
    
    def add_defaults(self) -> None:
        """
        Add default intent patterns for common Glyphh operations.
        
        Adds patterns for:
        - similarity_search: "find similar to", "what's like", "search for"
        - fact_tree: "verify", "explain", "prove"
        - temporal_predict: "predict", "what comes after", "forecast"
        
        Example:
            >>> intent_encoder = IntentEncoder(config)
            >>> intent_encoder.add_defaults()  # Add built-in patterns
            >>> intent_encoder.add_pattern(...)  # Add domain-specific patterns
        """
        from glyphh.encoder.default_intents import DEFAULT_INTENT_PATTERNS
        
        for pattern in DEFAULT_INTENT_PATTERNS:
            # Skip if already registered (allows user to override defaults)
            if pattern.intent_type not in self._pattern_map:
                self.add_pattern(pattern)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize intent patterns for .glyphh file bundling.
        
        Patterns are serialized as part of the model - no separate JSON files.
        
        Returns:
            Dictionary representation of all patterns
        
        Example:
            >>> data = intent_encoder.to_dict()
            >>> # Include in model export
            >>> model_data["intent_patterns"] = data
        """
        return {
            "patterns": [
                {
                    "intent_type": p.intent_type,
                    "example_phrases": p.example_phrases,
                    "query_template": p.query_template
                }
                for p in self._patterns
            ]
        }
    
    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        config: EncoderConfig
    ) -> 'IntentEncoder':
        """
        Deserialize intent patterns from .glyphh file.
        
        Args:
            data: Dictionary from to_dict()
            config: EncoderConfig for the encoder
        
        Returns:
            IntentEncoder with all patterns restored
        
        Example:
            >>> # Load from model
            >>> intent_encoder = IntentEncoder.from_dict(
            ...     model_data["intent_patterns"],
            ...     model.encoder_config
            ... )
        """
        encoder = cls(config)
        
        for p_data in data.get("patterns", []):
            pattern = IntentPattern(
                intent_type=p_data["intent_type"],
                example_phrases=p_data["example_phrases"],
                query_template=p_data["query_template"]
            )
            encoder.add_pattern(pattern)
        
        return encoder
    
    def __repr__(self) -> str:
        """String representation of the encoder."""
        return (
            f"IntentEncoder(dimension={self.dimension}, seed={self.seed}, "
            f"patterns={len(self._patterns)}, space_id='{self.space_id}')"
        )
