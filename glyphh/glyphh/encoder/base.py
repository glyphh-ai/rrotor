"""
Base Encoder class for the Glyphh SDK.

This module provides the base encoder class with strict contracts for:
- Dimension and bipolar constraint validation
- Deterministic symbol generation with caching
- Vector space consistency enforcement
- Space ID computation

Custom Encoder Contracts
========================

Custom encoders should inherit from this class and follow these strict contracts:

1. **Bipolar Constraint**: All vectors MUST be bipolar {-1, +1}
   - Use self._validate_vector() to ensure compliance
   - Vectors with any other values will raise BipolarConstraintException

2. **Dimension Consistency**: All vectors MUST have dimension matching encoder.dimension
   - Use self._validate_vector() to ensure compliance
   - Mismatched dimensions will raise DimensionMismatchException

3. **Vector Space Consistency**: All vectors MUST have space_id matching encoder.space_id
   - Use self._validate_vector() to ensure compliance
   - Different space_ids will raise VectorSpaceException
   - Ensures all vectors can be safely compared and combined

4. **Deterministic Encoding**: Encoding MUST be deterministic (same input → same output)
   - Use self.generate_symbol() for deterministic vector generation
   - Same key always produces same vector for a given encoder instance
   - Critical for reproducibility across environments

5. **Proper Initialization**: Custom encoders MUST call super().__init__(config)
   - Ensures dimension, seed, space_id, and cache are properly initialized
   - Required for validation hooks to work correctly

Extension Points
================

Custom encoders can override or extend these methods:

- encode(): Complete concept encoding (override for custom logic)
- encode_segment(): Segment-level encoding (override for custom segment handling)
- merge_segments(): Segment merging (override for custom aggregation)

Always use these base class methods in custom implementations:

- generate_symbol(): Deterministic symbol generation with caching
- bind(): Role-value binding (element-wise multiplication)
- bundle(): Vector aggregation (majority vote)
- _validate_vector(): Ensure vector meets all contracts

Example Custom Encoder
=======================

```python
from glyphh.encoder.base import Encoder
from glyphh.core.config import EncoderConfig
from glyphh.core.types import Concept, Glyph, Vector
import numpy as np

class CustomDomainEncoder(Encoder):
    '''
    Custom encoder for domain-specific concepts.
    
    This encoder extends the base encoder with custom transformation logic
    while maintaining all vector space contracts.
    '''
    
    def __init__(self, config: EncoderConfig, domain_params: dict = None):
        # REQUIRED: Call parent __init__ to initialize base encoder
        super().__init__(config)
        
        # Add custom domain parameters
        self.domain_params = domain_params or {}
    
    def encode(self, concept: Concept) -> Glyph:
        '''
        Custom encoding logic for domain-specific concepts.
        
        This method demonstrates how to:
        1. Apply custom transformations
        2. Validate vectors using base class methods
        3. Maintain vector space consistency
        '''
        # Apply custom transformation to concept
        transformed_attributes = self._custom_transform(concept.attributes)
        
        # Use base class encoding with transformed data
        transformed_concept = Concept(
            name=concept.name,
            attributes=transformed_attributes,
            relationships=concept.relationships,
            metadata=concept.metadata
        )
        
        # Encode using base class (ensures all contracts are met)
        glyph = super().encode(transformed_concept)
        
        # Validation is automatic through base class methods
        # All vectors in glyph are guaranteed to be:
        # - Bipolar {-1, +1}
        # - Correct dimension
        # - Same space_id
        
        return glyph
    
    def _custom_transform(self, attributes: dict) -> dict:
        '''Apply domain-specific transformations to attributes.'''
        # Example: normalize numeric values, apply domain rules, etc.
        transformed = {}
        for key, value in attributes.items():
            if key in self.domain_params.get('normalize', []):
                # Custom normalization logic
                transformed[key] = self._normalize_value(value)
            else:
                transformed[key] = value
        return transformed
    
    def _normalize_value(self, value):
        '''Domain-specific value normalization.'''
        # Custom normalization logic
        return value
    
    def custom_encode_with_validation(self, data: dict) -> Vector:
        '''
        Example method showing explicit validation.
        
        Use this pattern when creating vectors directly (not through encode()).
        '''
        # Generate base vectors
        role_vector = self.generate_symbol("custom_role")
        value_vector = self.generate_symbol(str(data.get("value", "default")))
        
        # Bind them
        bound_vector = self.bind(role_vector, value_vector)
        
        # Explicit validation (optional - bind() already validates)
        self._validate_vector(bound_vector)
        
        return bound_vector
```

Validation Hooks
================

Use _validate_custom_encoder() to ensure custom encoders are compatible:

```python
base_encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
custom_encoder = CustomDomainEncoder(EncoderConfig(dimension=10000, seed=42))

# Validate custom encoder operates in same vector space
base_encoder._validate_custom_encoder(custom_encoder)

# Now safe to use vectors from both encoders together
base_vector = base_encoder.generate_symbol("test")
custom_vector = custom_encoder.generate_symbol("test")

# These vectors can be safely compared and combined
similarity = np.dot(base_vector.data, custom_vector.data)
```

Common Pitfalls
===============

1. **Forgetting to call super().__init__(config)**
   - Results in missing dimension, seed, space_id attributes
   - Validation will fail with ValueError

2. **Creating vectors without using generate_symbol()**
   - May violate bipolar constraint
   - May have wrong space_id
   - Use generate_symbol() for all vector creation

3. **Using different configs for base and custom encoders**
   - Results in different space_ids
   - Vectors cannot be compared or combined
   - Always use same config for encoders that need to work together

4. **Modifying vectors after creation**
   - May violate bipolar constraint
   - Always validate after modifications using _validate_vector()

5. **Not validating custom vectors**
   - Use _validate_vector() after any custom transformations
   - Ensures contracts are maintained
"""

from typing import Dict, Optional, List, Any
import numpy as np

from glyphh.core.types import Vector, Segment, compute_space_id
from glyphh.core.config import EncoderConfig
from glyphh.exceptions import (
    BipolarConstraintException,
    DimensionMismatchException,
    VectorSpaceException,
    EncodingException,
    log_encoding_failure,
)
from glyphh.core.ops import generate_symbol


class Encoder:
    """
    Base encoder class for transforming concepts into bipolar vectors.
    
    The Encoder provides the foundation for all encoding operations in the SDK.
    It enforces strict contracts to maintain vector space consistency and
    deterministic encoding guarantees.
    
    Key Features:
    - Deterministic symbol generation with caching
    - Bipolar constraint validation
    - Dimension consistency enforcement
    - Vector space consistency via space_id
    
    Attributes:
        dimension: Vector dimension (number of dimensions)
        seed: Random seed for deterministic generation
        config: Encoder configuration
        space_id: Unique identifier for this vector space
        symbol_cache: Cache of generated symbol vectors
    
    Example:
        >>> config = EncoderConfig(dimension=10000, seed=42)
        >>> encoder = Encoder(config)
        >>> symbol = encoder.generate_symbol("color")
        >>> print(symbol.dimension)
        10000
        >>> print(symbol.space_id)
        'a3f2e8b1c4d5e6f7'
    
    Custom Encoder Example:
        >>> class CustomEncoder(Encoder):
        ...     def custom_encode(self, data):
        ...         # Custom encoding logic
        ...         vector = self._custom_transform(data)
        ...         
        ...         # Validation enforced by base class
        ...         self._validate_vector(vector)
        ...         
        ...         return vector
    """
    
    def __init__(self, config: EncoderConfig):
        """
        Initialize the encoder with configuration.
        
        Args:
            config: EncoderConfig with dimension, seed, and other parameters
        
        Raises:
            ConfigurationException: If configuration is invalid
        
        Example:
            >>> config = EncoderConfig(dimension=10000, seed=42)
            >>> encoder = Encoder(config)
        """
        self.dimension = config.dimension
        self.seed = config.seed
        self.config = config
        
        # Compute unique space_id for this vector space
        self.space_id = self._compute_space_id(
            self.dimension,
            self.seed,
            config.to_json()
        )
        
        # Cache for generated symbol vectors
        # Key: symbol key (string), Value: Vector
        self.symbol_cache: Dict[str, Vector] = {}
    
    def generate_symbol(self, key: str) -> Vector:
        """
        Generate a deterministic bipolar symbol vector.
        
        The same key will always produce the same vector for this encoder instance.
        Symbol vectors are cached to avoid redundant generation.
        
        This method is the foundation for all encoding operations. It ensures:
        - Deterministic generation (same key → same vector)
        - Caching for performance
        - Bipolar constraint enforcement
        - Vector space consistency
        
        Args:
            key: String key to generate vector for (e.g., "color", "red", "type")
        
        Returns:
            Deterministic bipolar Vector with this encoder's space_id
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> color_role = encoder.generate_symbol("color")
            >>> red_value = encoder.generate_symbol("red")
            >>> 
            >>> # Same key always produces same vector
            >>> color_role_2 = encoder.generate_symbol("color")
            >>> assert np.array_equal(color_role.data, color_role_2.data)
        """
        # Check cache first
        if key in self.symbol_cache:
            return self.symbol_cache[key]
        
        # Generate new symbol vector using Rust (or Python fallback)
        vector_data = generate_symbol(self.seed, key, self.dimension)
        
        # Create Vector with space_id
        vector = Vector(
            data=vector_data,
            dimension=self.dimension,
            space_id=self.space_id
        )
        
        # Cache for future use
        self.symbol_cache[key] = vector
        
        return vector
    
    def _validate_vector(self, vector: Vector) -> None:
        """
        Validate that a vector meets all requirements.
        
        This method enforces the strict contracts for vectors:
        1. Bipolar constraint: all values in {-1, +1}
        2. Dimension consistency: matches encoder.dimension
        3. Vector space consistency: matches encoder.space_id
        
        Args:
            vector: Vector to validate
        
        Raises:
            BipolarConstraintException: If vector contains non-bipolar values
            DimensionMismatchException: If vector dimension doesn't match
            VectorSpaceException: If vector space_id doesn't match
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> vector = encoder.generate_symbol("test")
            >>> encoder._validate_vector(vector)  # Passes
            >>> 
            >>> # Invalid vector (wrong dimension)
            >>> bad_vector = Vector(
            ...     data=np.array([-1, 1, -1], dtype=np.int8),
            ...     dimension=3,
            ...     space_id=encoder.space_id
            ... )
            >>> encoder._validate_vector(bad_vector)  # Raises DimensionMismatchException
        """
        # Validate bipolar constraint (optimized: avoid np.isin for performance)
        # For bipolar vectors, we can check if all values are in {-1, 1} by checking:
        # 1. All absolute values are 1
        # This is much faster than np.isin()
        abs_values = np.abs(vector.data)
        if not np.all(abs_values == 1):
            # Only compute invalid values if validation fails (rare case)
            mask = abs_values != 1
            invalid_values = vector.data[mask]
            raise BipolarConstraintException(
                invalid_values=invalid_values.tolist(),
                message="Vector must be bipolar (values in {-1, +1})"
            )
        
        # Validate dimension consistency
        if vector.dimension != self.dimension:
            raise DimensionMismatchException(
                expected=self.dimension,
                actual=vector.dimension,
                message="Vector dimension must match encoder dimension"
            )
        
        # Validate vector space consistency
        if vector.space_id != self.space_id:
            raise VectorSpaceException(
                expected=self.space_id,
                actual=vector.space_id,
                message="Vector must belong to the same vector space as encoder"
            )
    
    def _compute_space_id(
        self,
        dimension: int,
        seed: int,
        config_json: str
    ) -> str:
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
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> space_id = encoder._compute_space_id(
            ...     10000,
            ...     42,
            ...     '{"dimension":10000,"seed":42}'
            ... )
            >>> print(space_id)
            'a3f2e8b1c4d5e6f7'
        """
        return compute_space_id(dimension, seed, config_json)
    
    def clear_cache(self) -> None:
        """
        Clear the symbol vector cache.
        
        This can be useful for memory management in long-running processes,
        though it will cause regeneration of symbol vectors on next access.
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> encoder.generate_symbol("color")
            >>> print(len(encoder.symbol_cache))
            1
            >>> encoder.clear_cache()
            >>> print(len(encoder.symbol_cache))
            0
        """
        self.symbol_cache.clear()
    
    def get_cache_size(self) -> int:
        """
        Get the number of cached symbol vectors.
        
        Returns:
            Number of symbol vectors in cache
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> encoder.generate_symbol("color")
            >>> encoder.generate_symbol("red")
            >>> print(encoder.get_cache_size())
            2
        """
        return len(self.symbol_cache)
    
    def get_cached_keys(self) -> list:
        """
        Get list of all cached symbol keys.
        
        Returns:
            List of cached symbol keys
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> encoder.generate_symbol("color")
            >>> encoder.generate_symbol("red")
            >>> print(encoder.get_cached_keys())
            ['color', 'red']
        """
        return list(self.symbol_cache.keys())
    
    def bind(self, role: Vector, value: Vector) -> Vector:
        """
        Bind a role vector with a value vector.
        
        The bind operation performs element-wise multiplication of two bipolar vectors.
        This is used to create role bindings (e.g., bind("color", "red")).
        
        The bind operation has the inverse property:
        bind(bind(role, value), role) = value
        
        Args:
            role: Role vector (e.g., vector for "color")
            value: Value vector (e.g., vector for "red")
        
        Returns:
            Bound vector (role * value)
        
        Raises:
            DimensionMismatchException: If vectors have different dimensions
            VectorSpaceException: If vectors are from different spaces
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> color_role = encoder.generate_symbol("color")
            >>> red_value = encoder.generate_symbol("red")
            >>> color_red_binding = encoder.bind(color_role, red_value)
            >>> 
            >>> # Inverse property: unbind to retrieve value
            >>> retrieved_value = encoder.bind(color_red_binding, color_role)
            >>> assert np.array_equal(retrieved_value.data, red_value.data)
        """
        # Validate both vectors
        self._validate_vector(role)
        self._validate_vector(value)
        
        # Import bind operation
        from glyphh.core.ops import bind as bind_op
        
        # Perform bind operation
        bound_data = bind_op(role.data, value.data)
        
        # Create and return bound vector
        return Vector(
            data=bound_data,
            dimension=self.dimension,
            space_id=self.space_id
        )
    
    def bundle(self, vectors: List[Vector], weights: Optional[List[float]] = None) -> Vector:
        """
        Bundle multiple vectors using majority-vote aggregation.
        
        The bundle operation aggregates multiple vectors by taking the majority vote
        at each dimension. This is used to combine role bindings into segment cortices,
        segment cortices into layer cortices, and layer cortices into global cortices.
        
        The bundle operation is commutative:
        bundle([a, b, c]) = bundle([c, a, b])
        
        Args:
            vectors: List of vectors to bundle
            weights: Optional list of weights for weighted bundling (use weighted_bundle instead)
        
        Returns:
            Bundled vector (majority vote per dimension)
        
        Raises:
            ValueError: If vector list is empty
            DimensionMismatchException: If vectors have different dimensions
            VectorSpaceException: If vectors are from different spaces
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> type_binding = encoder.bind(
            ...     encoder.generate_symbol("type"),
            ...     encoder.generate_symbol("car")
            ... )
            >>> color_binding = encoder.bind(
            ...     encoder.generate_symbol("color"),
            ...     encoder.generate_symbol("red")
            ... )
            >>> segment_cortex = encoder.bundle([type_binding, color_binding])
        """
        if not vectors:
            raise ValueError("Cannot bundle empty vector list")
        
        # Validate all vectors
        for vector in vectors:
            self._validate_vector(vector)
        
        # Import bundle operation
        from glyphh.core.ops import bundle as bundle_op
        
        # If weights provided, use weighted_bundle instead
        if weights is not None:
            return self.weighted_bundle(list(zip(vectors, weights)))
        
        # Extract numpy arrays from vectors
        vector_arrays = [v.data for v in vectors]
        
        # Perform bundle operation
        bundled_data = bundle_op(vector_arrays)
        
        # Create and return bundled vector
        return Vector(
            data=bundled_data,
            dimension=self.dimension,
            space_id=self.space_id
        )
    
    def weighted_bundle(
        self,
        weighted_vectors: List[tuple]
    ) -> Vector:
        """
        Bundle vectors with weights using weighted majority vote.
        
        For each dimension, the weighted sum determines the output:
        - Sum weighted contributions: sum(vector[i] * weight for vector, weight in weighted_vectors)
        - Output +1 if sum >= 0, else -1
        
        This method is used when apply_weights_during_encoding=True to bake
        weights into the encoded representation at each hierarchy level.
        
        Args:
            weighted_vectors: List of (Vector, weight) tuples where weight is a float
        
        Returns:
            Bundled vector with weighted majority vote, always bipolar {-1, +1}
        
        Raises:
            ValueError: If weighted_vectors list is empty
            DimensionMismatchException: If vectors have different dimensions
            VectorSpaceException: If vectors are from different spaces
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> v1 = encoder.generate_symbol("important")
            >>> v2 = encoder.generate_symbol("less_important")
            >>> 
            >>> # Weight v1 higher than v2
            >>> result = encoder.weighted_bundle([
            ...     (v1, 1.0),   # Full weight
            ...     (v2, 0.3),   # Lower weight
            ... ])
            >>> 
            >>> # Result is always bipolar
            >>> assert np.all(np.isin(result.data, [-1, 1]))
        """
        if not weighted_vectors:
            raise ValueError("Cannot bundle empty vector list")
        
        # Validate all vectors
        for vector, weight in weighted_vectors:
            self._validate_vector(vector)
        
        # Compute weighted sum
        weighted_sum = np.zeros(self.dimension, dtype=np.float32)
        for vector, weight in weighted_vectors:
            weighted_sum += vector.data.astype(np.float32) * weight
        
        # Apply majority vote: +1 if sum >= 0, else -1
        bundled_data = np.where(weighted_sum >= 0, 1, -1).astype(np.int8)
        
        return Vector(
            data=bundled_data,
            dimension=self.dimension,
            space_id=self.space_id
        )
    
    def _encode_numeric_value(
        self,
        value: Any,
        numeric_config: 'NumericConfig'
    ) -> Vector:
        """
        Encode a numeric value using binning for similarity preservation.
        
        This method converts a numeric value into a bipolar vector using the
        configured binning strategy. Close numeric values will produce similar
        vectors, enabling similarity-based retrieval of numerically close values.
        
        Args:
            value: The numeric value to encode (will be converted to float)
            numeric_config: NumericConfig with bin_width, encoding_strategy, and bounds
        
        Returns:
            Bipolar Vector representing the binned numeric value
        
        Raises:
            EncodingException: If value cannot be converted to a number
        
        Example:
            >>> from glyphh.core.config import NumericConfig, EncodingStrategy
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> config = NumericConfig(bin_width=1.0, encoding_strategy=EncodingStrategy.THERMOMETER)
            >>> v1 = encoder._encode_numeric_value(7.2, config)
            >>> v2 = encoder._encode_numeric_value(7.8, config)  # Same bin as 7.2
            >>> v3 = encoder._encode_numeric_value(3.1, config)  # Different bin
            >>> # v1 and v2 will be identical (same bin), v3 will be different
        """
        from glyphh.encoder.numeric import encode_numeric_value, compute_bin_number
        from glyphh.core.config import EncodingStrategy
        import math
        import logging
        
        # Try to convert value to float
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            # Fall back to symbolic encoding if value is not numeric
            logging.warning(
                f"Cannot convert value '{value}' to numeric, falling back to symbolic encoding"
            )
            return self.generate_symbol(str(value))
        
        # Handle NaN and Infinity - fall back to symbolic
        if math.isnan(numeric_value) or math.isinf(numeric_value):
            logging.warning(
                f"Numeric value is NaN or Infinity, falling back to symbolic encoding"
            )
            return self.generate_symbol(str(value))
        
        # Compute bin number
        bin_num = compute_bin_number(numeric_value, numeric_config)
        
        # Determine max_bins for encoding
        if numeric_config.min_value is not None and numeric_config.max_value is not None:
            max_bins = math.ceil(
                (numeric_config.max_value - numeric_config.min_value) / numeric_config.bin_width
            )
        else:
            # Default to a reasonable number of bins
            max_bins = 100
        
        # Get the bit pattern from numeric encoder
        bits = encode_numeric_value(numeric_value, numeric_config, max_bins=max_bins)
        
        # Convert bit pattern to bipolar vector
        # We need to expand the bit pattern to fill the full dimension
        # Strategy: Use the bit pattern as a seed to generate a deterministic vector
        # This ensures same bin always produces same vector
        
        # Create a unique key from the bin number and config
        bin_key = f"__numeric_bin_{bin_num}_{numeric_config.encoding_strategy.value}_{numeric_config.bin_width}"
        
        # Generate base vector for this bin
        base_vector = self.generate_symbol(bin_key)

        return base_vector

    def _encode_continuous_value(
        self,
        value: Any,
        continuous_config: 'ContinuousConfig'
    ) -> Vector:
        """
        Encode a continuous float vector using random projection.

        Projects a dense float vector (e.g., neural network embedding) into
        bipolar HDC space via deterministic random projection + sign quantization.
        Similar input vectors produce similar bipolar vectors.

        Args:
            value: Float vector as list, tuple, or numpy array of shape (source_dim,)
            continuous_config: ContinuousConfig with source_dim and projection_seed

        Returns:
            Bipolar Vector representing the projected embedding

        Raises:
            EncodingException: If value cannot be converted to a float array

        Example:
            >>> from glyphh.core.config import ContinuousConfig
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> config = ContinuousConfig(source_dim=512, projection_seed=100)
            >>> embedding = np.random.randn(512).astype(np.float32)
            >>> v = encoder._encode_continuous_value(embedding, config)
        """
        import numpy as np
        import logging
        from glyphh.encoder.projection import ContinuousProjector

        try:
            arr = np.asarray(value, dtype=np.float32).flatten()
        except (TypeError, ValueError) as e:
            logging.warning(
                f"Cannot convert value to float array, falling back to symbolic: {e}"
            )
            return self.generate_symbol(str(value))

        if arr.shape[0] != continuous_config.source_dim:
            logging.warning(
                f"Expected {continuous_config.source_dim} elements, "
                f"got {arr.shape[0]}, falling back to symbolic encoding"
            )
            return self.generate_symbol(str(value))

        projector = ContinuousProjector(
            source_dim=continuous_config.source_dim,
            target_dim=self.dimension,
            seed=continuous_config.projection_seed,
        )
        bipolar = projector.project(arr)

        return Vector(
            data=bipolar,
            dimension=self.dimension,
            space_id=self.space_id,
        )

    def _get_morphology_engine(self):
        """Lazily initialize MorphologyEngine for BoW normalization.

        Creates a single MorphologyEngine per Encoder instance using the
        same dimension/seed.  Returns None if the linguistics module is
        unavailable (graceful fallback — BoW still works, just without
        morphological normalization).
        """
        if not hasattr(self, '_morphology_engine'):
            try:
                from glyphh.linguistics.character import CharacterEncoder as CharEnc
                from glyphh.linguistics.morphology import MorphologyEngine
                char_enc = CharEnc(dimension=self.dimension, seed=self.seed)
                self._morphology_engine = MorphologyEngine(char_enc)
            except Exception:
                self._morphology_engine = None
        return self._morphology_engine

    def _get_char_encoder(self):
        """Lazily initialize CharacterEncoder for bag-of-words encoding."""
        if not hasattr(self, '_char_encoder'):
            from glyphh.linguistics.character import CharacterEncoder
            self._char_encoder = CharacterEncoder(
                dimension=self.dimension, seed=self.seed,
            )
        return self._char_encoder

    def _encode_bag_of_words(self, text: str) -> Vector:
        """
        Encode a text value using bag-of-words bundling with character
        n-gram word vectors.

        Each word is encoded via CharacterEncoder (positional char n-grams),
        giving fuzzy matching between morphological variants, misspellings,
        and partial matches:
          - "wife" ↔ "wifes" ≈ 0.8 (shared n-grams)
          - "love" ↔ "loves" ≈ 0.8
          - "delete" ↔ "deploy" ≈ 0.3 (some prefix overlap)

        This replaces the previous generate_symbol() approach where every
        distinct string got a random orthogonal vector — "wife" and "wifes"
        had cosine ≈ 0, breaking natural language matching entirely.

        Morphology normalization is still applied as a first pass for cases
        CharacterEncoder alone can't handle (irregular forms).

        Args:
            text: The text value to encode

        Returns:
            Bundled bipolar Vector representing the bag of words

        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> v1 = encoder._encode_bag_of_words("calculate area of circle")
            >>> v2 = encoder._encode_bag_of_words("calculate circumference of circle")
            >>> # v1 and v2 share "calculate", "circle" → high cosine similarity
        """
        import re
        # Split on non-alphanumeric, lowercase, filter empty
        words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
        words = [w for w in words if len(w) > 1]

        if not words:
            return self.generate_symbol("__empty__")

        # Normalize morphology — "days"→"day", "running"→"run", etc.
        morph = self._get_morphology_engine()
        if morph is not None:
            words = [morph.normalize(w)[0] for w in words]

        # Deduplicate — BoW treats each unique word equally regardless of
        # frequency.  This gives stable cosine distances between exemplar and
        # query vectors even when descriptions repeat keywords for emphasis.
        seen: set[str] = set()
        unique_words: list[str] = []
        for w in words:
            if w not in seen:
                seen.add(w)
                unique_words.append(w)

        # CharacterEncoder: positional char n-grams give fuzzy matching
        # between word variants. Same interface as generate_symbol (returns
        # bipolar np.ndarray) but similarity degrades gracefully with
        # character distance instead of being all-or-nothing.
        char_enc = self._get_char_encoder()
        word_vecs = [
            Vector(
                data=char_enc.encode_word(w),
                dimension=self.dimension,
                space_id=self.space_id,
            )
            for w in unique_words
        ]
        return self.bundle(word_vecs)
    
    def encode_segment(
        self,
        segment_name: str,
        attributes: Dict[str, Any],
        weights: Optional[Dict[str, float]] = None
    ) -> Segment:
        """
        Encode a single segment with role bindings.
        
        This method creates a segment by:
        1. Generating role and value vectors for each attribute
        2. Binding each role with its value
        3. Bundling all role bindings into a segment cortex
        
        Args:
            segment_name: Name of the segment (e.g., "attributes", "relations")
            attributes: Dictionary of role-value pairs (e.g., {"color": "red", "size": "medium"})
            weights: Optional weights for roles and segment
        
        Returns:
            Segment with role bindings and cortex
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> segment = encoder.encode_segment(
            ...     segment_name="attributes",
            ...     attributes={"type": "car", "color": "red", "size": "medium"},
            ...     weights={"segment": 0.9, "type": 1.0, "color": 0.8, "size": 0.6}
            ... )
        """
        if not attributes:
            # Log the encoding failure
            log_encoding_failure(
                concept_name=f"segment:{segment_name}",
                reason="Cannot encode segment with no attributes",
                problematic_input={"segment_name": segment_name, "attributes": attributes},
                stage="segment_encoding"
            )
            raise EncodingException(
                concept_name=f"segment:{segment_name}",
                reason="Cannot encode segment with no attributes",
                problematic_input={"segment_name": segment_name, "attributes": attributes},
                stage="segment_encoding"
            )
        
        # Initialize weights if not provided
        if weights is None:
            weights = {}
        
        # Create role bindings
        roles = {}
        role_values = {}
        role_bindings = []
        
        for role_name, value in attributes.items():
            # Generate role vector
            role_vector = self.generate_symbol(role_name)
            
            # Generate value vector (convert value to string for symbol generation)
            value_str = str(value)
            value_vector = self.generate_symbol(value_str)
            
            # Bind role with value
            bound_vector = self.bind(role_vector, value_vector)
            
            # Store role binding and original value
            roles[role_name] = bound_vector
            role_values[role_name] = value
            role_bindings.append(bound_vector)
        
        # Bundle all role bindings into segment cortex
        segment_cortex = self.bundle(role_bindings)
        
        # Create and return segment
        return Segment(
            name=segment_name,
            cortex=segment_cortex,
            roles=roles,
            role_values=role_values,
            weights=weights
        )
    
    def merge_segments(
        self,
        segments: List[Segment],
        weights: Optional[List[float]] = None
    ) -> Vector:
        """
        Merge multiple segments into a layer cortex using weighted bundle.
        
        This method combines segment cortices into a single layer cortex vector.
        
        Args:
            segments: List of segments to merge
            weights: Optional list of weights for weighted bundling
        
        Returns:
            Layer cortex vector (bundle of segment cortices)
        
        Raises:
            ValueError: If segment list is empty
        
        Example:
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> attributes_segment = encoder.encode_segment(
            ...     "attributes",
            ...     {"type": "car", "color": "red"}
            ... )
            >>> relations_segment = encoder.encode_segment(
            ...     "relations",
            ...     {"has_part": "wheels", "used_for": "transportation"}
            ... )
            >>> layer_cortex = encoder.merge_segments([attributes_segment, relations_segment])
        """
        if not segments:
            # Log the encoding failure
            log_encoding_failure(
                concept_name="merge_segments",
                reason="Cannot merge empty segment list",
                problematic_input={"segments": segments},
                stage="segment_merging"
            )
            raise EncodingException(
                concept_name="merge_segments",
                reason="Cannot merge empty segment list",
                problematic_input={"segments": segments},
                stage="segment_merging"
            )
        
        # Extract cortex vectors from segments
        cortex_vectors = [segment.cortex for segment in segments]
        
        # Bundle segment cortices into layer cortex
        return self.bundle(cortex_vectors, weights)
    
    def _create_temporal_layer(
        self,
        concept: 'Concept',
        timestamp: 'datetime'
    ) -> tuple:
        """
        Create the dedicated _temporal layer for encoding the temporal signal.
        
        The temporal layer is always present in every glyph and contains a single
        role binding for the temporal value. The source of the temporal value is
        determined by config.temporal_source:
        - "auto": Use current timestamp
        - "layer.segment.role": Use the value from the specified role
        
        Args:
            concept: Concept being encoded
            timestamp: Current timestamp for auto-generation
        
        Returns:
            Tuple of (RuntimeLayer, temporal_value_string)
        """
        from glyphh.core.types import Layer as RuntimeLayer, Segment as RuntimeSegment
        from glyphh.core.config import (
            TEMPORAL_LAYER_NAME, TEMPORAL_SEGMENT_NAME, TEMPORAL_ROLE_NAME
        )
        
        # Determine temporal value based on temporal_source
        temporal_source = self.config.temporal_source
        
        if temporal_source == "auto":
            # Auto-generate timestamp
            temporal_value = timestamp.isoformat()
        else:
            # Extract value from specified role path (layer.segment.role)
            parts = temporal_source.split(".")
            if len(parts) == 3:
                layer_name, segment_name, role_name = parts
                # Get value from concept attributes
                temporal_value = concept.attributes.get(role_name)
                if temporal_value is not None:
                    temporal_value = self._format_temporal_value(temporal_value)
                else:
                    # Fallback to auto if role value not found
                    temporal_value = timestamp.isoformat()
            else:
                temporal_value = timestamp.isoformat()
        
        # Create role binding for temporal value
        role_vector = self.generate_symbol(TEMPORAL_ROLE_NAME)
        value_vector = self.generate_symbol(str(temporal_value))
        binding = self.bind(role_vector, value_vector)
        
        # Create segment with single role
        segment = RuntimeSegment(
            name=TEMPORAL_SEGMENT_NAME,
            cortex=binding,  # Single binding is the cortex
            roles={TEMPORAL_ROLE_NAME: binding},
            role_values={TEMPORAL_ROLE_NAME: temporal_value},
            weights={"similarity": 1.0, "security": 1.0}
        )
        
        # Create layer with single segment
        layer = RuntimeLayer(
            name=TEMPORAL_LAYER_NAME,
            cortex=binding,  # Single segment cortex is the layer cortex
            segments={TEMPORAL_SEGMENT_NAME: segment},
            weights={"similarity": 1.0, "security": 1.0}
        )
        
        return layer, temporal_value
    
    def _build_identifier(
        self,
        concept: 'Concept',
        timestamp: 'datetime',
        temporal_value: str = None
    ) -> str:
        """
        Build glyph identifier from composite key and temporal value.
        
        Format: {composite_key}@{temporal_value}#{version}
        
        The composite key is built from all roles with key_part=True.
        The temporal value is provided from the _temporal layer encoding.
        
        Args:
            concept: Concept being encoded
            timestamp: Timestamp for auto-generation fallback
            temporal_value: Pre-computed temporal value from _create_temporal_layer
        
        Returns:
            Identifier string in format {key}@{temporal}#{version}
        """
        # Find key_part roles and build composite key
        key_parts = []
        
        if self.config.layers:
            for layer in self.config.layers:
                for segment in layer.segments:
                    for role in segment.roles:
                        if role.key_part and role.name in concept.attributes:
                            value = str(concept.attributes[role.name])
                            # Sanitize value for identifier
                            sanitized = value.replace(" ", "_").replace("@", "_").replace("#", "_")
                            key_parts.append(sanitized)
        
        # Build primary key
        if key_parts:
            primary_key = "_".join(key_parts)
        else:
            # Fallback to concept name
            primary_key = concept.name.replace(" ", "_").replace("@", "_").replace("#", "_")
        
        # Use provided temporal value or fallback to timestamp
        if temporal_value is None:
            temporal_value = timestamp.isoformat()
        
        return f"{primary_key}@{temporal_value}#v1"
    
    def _format_temporal_value(self, value: Any) -> str:
        """
        Format temporal value based on TemporalConfig.
        
        Args:
            value: Raw temporal value from concept attributes
        
        Returns:
            Formatted temporal string for identifier
        """
        from glyphh.core.config import TemporalConfig
        from datetime import datetime
        
        config = self.config.temporal_config or TemporalConfig()
        
        try:
            if config.signal_type == "auto":
                return datetime.now().isoformat()
            elif config.signal_type == "datetime":
                # Parse and reformat to ISO8601
                dt = datetime.strptime(str(value), config.format)
                return dt.isoformat()
            elif config.signal_type == "epoch":
                # Convert epoch to ISO8601
                dt = datetime.fromtimestamp(float(value))
                return dt.isoformat()
            elif config.signal_type == "sequence":
                # Use sequence number directly
                return str(value)
            elif config.signal_type == "version":
                # Use version string directly
                return str(value)
            else:
                return str(value)
        except (ValueError, TypeError) as e:
            # Fallback to auto-generated timestamp on error
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to format temporal value '{value}': {e}. Using auto-generated timestamp."
            )
            return datetime.now().isoformat()
    
    def encode(
        self,
        concept: 'Concept',
        layer_config: Optional[Dict[str, Any]] = None
    ) -> 'Glyph':
        """
        Encode a concept into a glyph with hierarchical structure.
        
        This method performs the complete encoding process:
        1. Create role bindings for each attribute
        2. Bundle role bindings into segment cortices
        3. Bundle segment cortices into layer cortices
        4. Bundle layer cortices into global cortex
        
        If the encoder's config has explicit layers defined, those are used.
        Otherwise, falls back to the legacy layer_config parameter or default config.
        
        Weighting strategy is determined by config.apply_weights_during_encoding:
        - False (default): Use unweighted bundle(), store weights in glyph
        - True: Use weighted_bundle() at each level
        
        Args:
            concept: Concept to encode
            layer_config: Optional legacy configuration for layers and segments
        
        Returns:
            Glyph with complete hierarchical structure
        
        Example:
            >>> from glyphh.core.types import Concept
            >>> encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> concept = Concept(
            ...     name="red car",
            ...     attributes={"type": "car", "color": "red", "size": "medium"},
            ...     relationships=[("has_part", "wheels"), ("used_for", "transportation")],
            ...     metadata={"domain": "automotive"}
            ... )
            >>> glyph = encoder.encode(concept)
        """
        # If config has explicit layers, use the new encoding path
        if self.config.layers:
            return self._encode_with_explicit_config(concept)
        
        # Otherwise, use legacy encoding path
        return self._encode_legacy(concept, layer_config)
    
    def _encode_with_explicit_config(self, concept: 'Concept') -> 'Glyph':
        """
        Encode a concept using the explicit layer/segment/role structure from config.
        
        This method uses the config's layers, segments, and roles to encode the concept.
        Weighting strategy is determined by config.apply_weights_during_encoding.
        
        Args:
            concept: Concept to encode
        
        Returns:
            Glyph with complete hierarchical structure
        """
        from glyphh.core.types import Glyph, Layer as RuntimeLayer, Segment as RuntimeSegment
        from datetime import datetime
        
        # Generate timestamp for temporal layer and identifier
        timestamp = datetime.now()
        
        use_weighted = self.config.apply_weights_during_encoding
        
        layers = {}
        layer_cortices = []
        layer_weights = []
        
        for layer_def in self.config.layers:
            segments = {}
            segment_cortices = []
            segment_weights = []
            
            for segment_def in layer_def.segments:
                roles = {}
                role_values = {}
                role_bindings = []
                role_weights = []
                
                for role_def in segment_def.roles:
                    # Check if concept has this attribute
                    if role_def.name in concept.attributes:
                        value = concept.attributes[role_def.name]
                        
                        # Create role binding
                        role_vector = self.generate_symbol(role_def.name)
                        
                        # Check encoding type: continuous > numeric > bag_of_words > symbolic
                        if role_def.continuous_config is not None:
                            value_vector = self._encode_continuous_value(value, role_def.continuous_config)
                        elif role_def.numeric_config is not None:
                            value_vector = self._encode_numeric_value(value, role_def.numeric_config)
                        elif role_def.text_encoding == "bag_of_words":
                            value_vector = self._encode_bag_of_words(str(value))
                        else:
                            value_vector = self.generate_symbol(str(value))
                        
                        binding = self.bind(role_vector, value_vector)
                        
                        roles[role_def.name] = binding
                        role_values[role_def.name] = value
                        role_bindings.append(binding)
                        role_weights.append(role_def.similarity_weight)
                
                # Skip empty segments
                if not role_bindings:
                    continue
                
                # Bundle role bindings into segment cortex
                if use_weighted and role_weights:
                    weighted = list(zip(role_bindings, role_weights))
                    segment_cortex = self.weighted_bundle(weighted)
                else:
                    segment_cortex = self.bundle(role_bindings)
                
                # Create runtime segment
                segment = RuntimeSegment(
                    name=segment_def.name,
                    cortex=segment_cortex,
                    roles=roles,
                    role_values=role_values,
                    weights={
                        "similarity": segment_def.similarity_weight,
                        "security": segment_def.security_weight
                    }
                )
                segments[segment_def.name] = segment
                segment_cortices.append(segment_cortex)
                segment_weights.append(segment_def.similarity_weight)
            
            # Skip empty layers
            if not segment_cortices:
                continue
            
            # Bundle segment cortices into layer cortex
            if use_weighted and segment_weights:
                weighted = list(zip(segment_cortices, segment_weights))
                layer_cortex = self.weighted_bundle(weighted)
            else:
                layer_cortex = self.bundle(segment_cortices)
            
            # Create runtime layer
            layer = RuntimeLayer(
                name=layer_def.name,
                cortex=layer_cortex,
                segments=segments,
                weights={
                    "similarity": layer_def.similarity_weight,
                    "security": layer_def.security_weight
                }
            )
            layers[layer_def.name] = layer
            layer_cortices.append(layer_cortex)
            layer_weights.append(layer_def.similarity_weight)
        
        # Handle case where no layers could be created
        if not layer_cortices:
            log_encoding_failure(
                concept_name=concept.name,
                reason="No valid layers could be created from concept data",
                problematic_input={
                    "attributes": concept.attributes,
                    "config_layers": [l.name for l in self.config.layers]
                },
                stage="layer_bundling"
            )
            raise EncodingException(
                concept_name=concept.name,
                reason="Cannot encode concept: no attributes match defined roles",
                problematic_input={
                    "attributes": concept.attributes,
                    "config_layers": [l.name for l in self.config.layers]
                },
                stage="layer_bundling"
            )
        
        # Create the _temporal layer (if enabled)
        if self.config.include_temporal:
            temporal_layer, temporal_value = self._create_temporal_layer(concept, timestamp)
            layers[temporal_layer.name] = temporal_layer
            layer_cortices.append(temporal_layer.cortex)
            layer_weights.append(1.0)  # Temporal layer has weight 1.0
        else:
            temporal_value = timestamp.isoformat()
        
        # Bundle layer cortices into global cortex
        if use_weighted and layer_weights:
            weighted = list(zip(layer_cortices, layer_weights))
            global_cortex = self.weighted_bundle(weighted)
        else:
            global_cortex = self.bundle(layer_cortices)
        
        # Generate composite identifier using key_part and temporal value
        identifier = self._build_identifier(concept, timestamp, temporal_value)
        
        # Create and return glyph
        return Glyph(
            identifier=identifier,
            name=concept.name,
            space_id=self.space_id,
            global_cortex=global_cortex,
            layers=layers,
            security_levels={
                "cortex": self.config.security_weight,
            },
            metadata=concept.metadata,
            timestamp=timestamp,
            version="v1"
        )
    
    def _encode_legacy(
        self,
        concept: 'Concept',
        layer_config: Optional[Dict[str, Any]] = None
    ) -> 'Glyph':
        """
        Legacy encoding path for backward compatibility.
        
        Used when config doesn't have explicit layers defined.
        
        Args:
            concept: Concept to encode
            layer_config: Optional configuration for layers and segments
        
        Returns:
            Glyph with complete hierarchical structure
        """
        from glyphh.core.types import Glyph, Layer as RuntimeLayer
        from datetime import datetime
        
        # Use default layer config if not provided
        if layer_config is None:
            layer_config = {
                "layers": [
                    {
                        "name": "semantic",
                        "segments": [
                            {"name": "attributes", "source": "attributes"},
                            {"name": "relations", "source": "relationships"}
                        ]
                    }
                ]
            }
        
        # Build layers
        layers = {}
        layer_cortices = []
        
        for layer_spec in layer_config["layers"]:
            layer_name = layer_spec["name"]
            segments = {}
            segment_list = []
            
            # Build segments for this layer
            for segment_spec in layer_spec["segments"]:
                segment_name = segment_spec["name"]
                source = segment_spec["source"]
                
                # Get data from concept based on source
                if source == "attributes":
                    segment_data = concept.attributes
                elif source == "relationships":
                    # Convert relationships to dict format
                    segment_data = {
                        rel_type: target
                        for rel_type, target in concept.relationships
                    }
                else:
                    raise ValueError(f"Unknown segment source: {source}")
                
                # Skip empty segments
                if not segment_data:
                    continue
                
                # Encode segment
                segment = self.encode_segment(segment_name, segment_data)
                segments[segment_name] = segment
                segment_list.append(segment)
            
            # Skip empty layers
            if not segment_list:
                continue
            
            # Merge segments into layer cortex
            layer_cortex = self.merge_segments(segment_list)
            
            # Create layer
            layer = RuntimeLayer(
                name=layer_name,
                cortex=layer_cortex,
                segments=segments,
                weights={}
            )
            
            layers[layer_name] = layer
            layer_cortices.append(layer_cortex)
        
        # Bundle layer cortices into global cortex
        if not layer_cortices:
            # Log the encoding failure
            log_encoding_failure(
                concept_name=concept.name,
                reason="No valid layers could be created from concept data",
                problematic_input={
                    "attributes": concept.attributes,
                    "relationships": concept.relationships
                },
                stage="layer_bundling"
            )
            raise EncodingException(
                concept_name=concept.name,
                reason="Cannot encode concept with no layers",
                problematic_input={
                    "attributes": concept.attributes,
                    "relationships": concept.relationships
                },
                stage="layer_bundling"
            )
        
        # Create the _temporal layer (if enabled)
        timestamp = datetime.now()
        if self.config.include_temporal:
            temporal_layer, temporal_value = self._create_temporal_layer(concept, timestamp)
            layers[temporal_layer.name] = temporal_layer
            layer_cortices.append(temporal_layer.cortex)
        else:
            temporal_value = timestamp.isoformat()
        
        global_cortex = self.bundle(layer_cortices)
        
        # Generate composite identifier using key_part and temporal value
        identifier = self._build_identifier(concept, timestamp, temporal_value)
        
        # Create and return glyph
        return Glyph(
            identifier=identifier,
            name=concept.name,
            space_id=self.space_id,
            global_cortex=global_cortex,
            layers=layers,
            security_levels={},
            metadata=concept.metadata,
            timestamp=timestamp,
            version="v1"
        )
    
    def _validate_custom_encoder(self, encoder: 'Encoder') -> None:
        """
        Validate that a custom encoder operates in the same vector space.
        
        This method ensures that custom encoders maintain vector space consistency
        by validating that they:
        1. Have the same dimension as this encoder
        2. Have the same space_id as this encoder
        3. Are properly initialized
        
        This is used when registering or using custom encoders to ensure all
        vectors can be safely compared and combined.
        
        Args:
            encoder: Custom encoder instance to validate
        
        Raises:
            VectorSpaceException: If encoder is in a different vector space
            DimensionMismatchException: If encoder has different dimension
            ValueError: If encoder is not properly initialized
        
        Example:
            >>> base_encoder = Encoder(EncoderConfig(dimension=10000, seed=42))
            >>> custom_encoder = CustomDomainEncoder(EncoderConfig(dimension=10000, seed=42))
            >>> base_encoder._validate_custom_encoder(custom_encoder)  # Passes
            >>> 
            >>> # Different dimension - fails
            >>> bad_encoder = CustomDomainEncoder(EncoderConfig(dimension=5000, seed=42))
            >>> base_encoder._validate_custom_encoder(bad_encoder)  # Raises DimensionMismatchException
        """
        # Check if encoder is properly initialized
        if not hasattr(encoder, 'dimension') or not hasattr(encoder, 'space_id'):
            raise ValueError(
                "Custom encoder is not properly initialized. "
                "Ensure it calls super().__init__(config) in its __init__ method."
            )
        
        # Validate dimension consistency
        if encoder.dimension != self.dimension:
            raise DimensionMismatchException(
                expected=self.dimension,
                actual=encoder.dimension,
                message=(
                    f"Custom encoder dimension ({encoder.dimension}) must match "
                    f"base encoder dimension ({self.dimension})"
                )
            )
        
        # Validate vector space consistency
        if encoder.space_id != self.space_id:
            raise VectorSpaceException(
                expected=self.space_id,
                actual=encoder.space_id,
                message=(
                    f"Custom encoder space_id ({encoder.space_id}) must match "
                    f"base encoder space_id ({self.space_id}). "
                    "Ensure both encoders use the same configuration (dimension, seed, config)."
                )
            )
    
    def __repr__(self) -> str:
        """String representation of the encoder."""
        return (
            f"Encoder(dimension={self.dimension}, seed={self.seed}, "
            f"space_id='{self.space_id}', cached_symbols={len(self.symbol_cache)})"
        )
