"""
HDC (Hyperdimensional Computing) vector operations.

Core mathematical operations for bipolar vectors used throughout the
Glyphh engine: bind, bundle, cosine similarity, hamming similarity,
and deterministic symbol generation.
"""

import hashlib

import numpy as np
from typing import List


def bind(r: np.ndarray, v: np.ndarray) -> np.ndarray:
    """
    Bind operation: element-wise multiplication of two bipolar vectors.

    Args:
        r: Role vector (bipolar: {-1, +1})
        v: Value vector (bipolar: {-1, +1})

    Returns:
        Bound vector (element-wise product)

    Raises:
        ValueError: If dimensions don't match
    """
    if r.shape != v.shape:
        raise ValueError(f"Dimension mismatch: r has shape {r.shape}, v has shape {v.shape}")
    return (r * v).astype(np.int8)


def bundle(vectors: List[np.ndarray]) -> np.ndarray:
    """
    Bundle operation: majority-vote aggregation of multiple bipolar vectors.

    Args:
        vectors: List of bipolar vectors to bundle

    Returns:
        Bundled vector (majority vote per dimension)

    Raises:
        ValueError: If vector list is empty or dimensions don't match
    """
    if not vectors:
        raise ValueError("Cannot bundle empty vector list")

    dim = vectors[0].shape[0]
    for i, vec in enumerate(vectors):
        if vec.shape[0] != dim:
            raise ValueError(
                f"Dimension mismatch: vector 0 has {dim} dimensions, "
                f"vector {i} has {vec.shape[0]} dimensions"
            )

    sums = np.sum(vectors, axis=0)
    return np.where(sums >= 0, 1, -1).astype(np.int8)


def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """
    Cosine similarity for bipolar vectors.

    Range: [-1, 1]
    - 1.0: Identical vectors
    - 0.0: Orthogonal (50% agreement)
    - -1.0: Opposite vectors

    Args:
        v1: First bipolar vector
        v2: Second bipolar vector

    Returns:
        Cosine similarity score

    Raises:
        ValueError: If dimensions don't match
    """
    if v1.shape != v2.shape:
        raise ValueError(f"Dimension mismatch: v1 has shape {v1.shape}, v2 has shape {v2.shape}")

    v1_int32 = v1.astype(np.int32)
    v2_int32 = v2.astype(np.int32)
    dot = np.dot(v1_int32, v2_int32)
    norm = len(v1)
    return float(dot / norm)


def hamming_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """
    Hamming similarity for bipolar vectors.

    Range: [0, 1]
    - 1.0: Identical vectors
    - 0.5: Orthogonal (50% agreement)
    - 0.0: Opposite vectors

    Relationship: hamming = (cosine + 1) / 2

    Args:
        v1: First bipolar vector
        v2: Second bipolar vector

    Returns:
        Hamming similarity score

    Raises:
        ValueError: If dimensions don't match
    """
    if v1.shape != v2.shape:
        raise ValueError(f"Dimension mismatch: v1 has shape {v1.shape}, v2 has shape {v2.shape}")

    dimensions_agree = np.sum(v1 == v2)
    return float(dimensions_agree / len(v1))


def generate_symbol(seed: int, key: str, dimension: int) -> np.ndarray:
    """
    Generate a deterministic bipolar symbol vector.

    The same (seed, key, dimension) combination will always produce the same vector.
    This is critical for maintaining deterministic encoding guarantees.

    Args:
        seed: Base seed for the random number generator
        key: String key to generate vector for (e.g., "color", "red")
        dimension: Dimension of the vector to generate

    Returns:
        Deterministic bipolar vector
    """
    combined = f"{seed}:{key}:{dimension}"
    hash_digest = hashlib.sha256(combined.encode()).hexdigest()
    derived_seed = int(hash_digest[:8], 16)

    rng = np.random.RandomState(derived_seed)
    return rng.choice([-1, 1], size=dimension).astype(np.int8)
