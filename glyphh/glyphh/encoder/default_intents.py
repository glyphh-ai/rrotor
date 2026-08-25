"""
Default Intent Patterns for common Glyphh operations.

These patterns provide out-of-the-box support for the core Glyphh operations:
- similarity_search: Find similar concepts
- fact_tree: Verify and explain relationships
- temporal_predict: Predict future states

Model authors can use these defaults and add domain-specific patterns:

    >>> intent_encoder = IntentEncoder(config)
    >>> intent_encoder.add_defaults()  # Add these patterns
    >>> intent_encoder.add_pattern(...)  # Add domain-specific patterns
"""

from glyphh.encoder.intent import IntentPattern


# Default patterns for common Glyphh operations
DEFAULT_INTENT_PATTERNS = [
    # Similarity Search - find similar concepts
    IntentPattern(
        intent_type="similarity_search",
        example_phrases=[
            "find similar to",
            "what is like",
            "search for",
            "find",
            "similar to",
            "what's similar to",
            "show me similar",
            "find matching",
            "lookup",
            "search",
        ],
        query_template={
            "operation": "similarity_search",
            "top_k": 10,
            "edge_type": "neural_cortex"
        }
    ),
    
    # Fact Tree - verify and explain relationships
    IntentPattern(
        intent_type="fact_tree",
        example_phrases=[
            "verify",
            "explain",
            "prove",
            "is it true that",
            "evidence for",
            "show proof",
            "why is",
            "how is",
            "explain why",
            "show evidence",
        ],
        query_template={
            "operation": "fact_tree",
            "max_depth": 3,
            "include_citations": True
        }
    ),
    
    # Temporal Prediction - predict future states
    IntentPattern(
        intent_type="temporal_predict",
        example_phrases=[
            "predict",
            "what comes after",
            "forecast",
            "next state",
            "what will happen",
            "future of",
            "predict next",
            "what's next for",
            "forecast for",
            "predict future",
        ],
        query_template={
            "operation": "temporal_predict",
            "steps_ahead": 1,
            "beam_width": 3
        }
    ),
    
    # List/Show All - enumerate concepts
    IntentPattern(
        intent_type="list_all",
        example_phrases=[
            "list all",
            "show all",
            "get all",
            "enumerate",
            "what are all",
            "show me all",
            "list everything",
            "all items",
        ],
        query_template={
            "operation": "list",
            "limit": 100
        }
    ),
    
    # Count - count concepts
    IntentPattern(
        intent_type="count",
        example_phrases=[
            "how many",
            "count",
            "total number of",
            "number of",
            "count all",
            "how much",
        ],
        query_template={
            "operation": "count"
        }
    ),
    
    # Compare - compare two concepts
    IntentPattern(
        intent_type="compare",
        example_phrases=[
            "compare",
            "difference between",
            "how different",
            "similarity between",
            "compare to",
            "versus",
            "vs",
            "contrast",
        ],
        query_template={
            "operation": "compare",
            "edge_type": "neural_cortex"
        }
    ),
]


def get_default_patterns() -> list:
    """
    Get a copy of the default intent patterns.
    
    Returns:
        List of IntentPattern objects
    
    Example:
        >>> patterns = get_default_patterns()
        >>> for p in patterns:
        ...     print(f"{p.intent_type}: {len(p.example_phrases)} phrases")
    """
    return DEFAULT_INTENT_PATTERNS.copy()


def get_pattern_by_type(intent_type: str) -> IntentPattern:
    """
    Get a specific default pattern by intent type.
    
    Args:
        intent_type: The intent type to look up
    
    Returns:
        IntentPattern if found
    
    Raises:
        KeyError: If intent_type not found in defaults
    
    Example:
        >>> pattern = get_pattern_by_type("similarity_search")
        >>> print(pattern.example_phrases)
    """
    for pattern in DEFAULT_INTENT_PATTERNS:
        if pattern.intent_type == intent_type:
            return pattern
    raise KeyError(f"No default pattern with intent_type '{intent_type}'")
