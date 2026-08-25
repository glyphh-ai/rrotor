"""
Continuous vector projection for Hyperdimensional Computing.

Projects continuous float vectors (e.g., neural network embeddings) into
bipolar HDC space via deterministic random projection + sign quantization.

This enables encoding of dense float vectors from external models (vision,
audio, sensor, etc.) into the bipolar {-1, +1} vectors used throughout
the Glyphh SDK.

Mathematical basis:
    Johnson-Lindenstrauss lemma guarantees that random projection preserves
    pairwise distances with high probability. Sign quantization (1-bit
    quantization) further maps to bipolar space while approximately
    preserving cosine similarity (Goemans-Williamson rounding).
"""

import numpy as np
from typing import Optional, Tuple


class ContinuousProjector:
    """
    Project continuous float vectors into bipolar HDC space.

    Uses a deterministic random projection matrix (seeded) followed by
    sign quantization. Same input always produces the same output.

    Args:
        source_dim: Dimensionality of input float vectors (e.g., 512 for ArcFace)
        target_dim: Dimensionality of output bipolar vectors (default: 10000)
        seed: Seed for deterministic projection matrix generation

    Example:
        >>> projector = ContinuousProjector(source_dim=512, target_dim=10000, seed=42)
        >>> embedding = np.random.randn(512).astype(np.float32)
        >>> bipolar = projector.project(embedding)
        >>> assert bipolar.shape == (10000,)
        >>> assert set(np.unique(bipolar)).issubset({-1, 1})
    """

    # Class-level cache: (source_dim, target_dim, seed) → projection matrix
    _matrix_cache: dict = {}

    def __init__(self, source_dim: int, target_dim: int = 10000, seed: int = 42):
        self.source_dim = source_dim
        self.target_dim = target_dim
        self.seed = seed
        self._projection_matrix = self._get_or_create_matrix()

    def _get_or_create_matrix(self) -> np.ndarray:
        """Get cached projection matrix or create a new one."""
        cache_key = (self.source_dim, self.target_dim, self.seed)
        if cache_key not in ContinuousProjector._matrix_cache:
            rng = np.random.RandomState(self.seed)
            # Gaussian random projection (scaled for unit variance)
            matrix = rng.randn(self.source_dim, self.target_dim).astype(np.float32)
            matrix /= np.sqrt(self.source_dim)
            ContinuousProjector._matrix_cache[cache_key] = matrix
        return ContinuousProjector._matrix_cache[cache_key]

    def project(self, embedding: np.ndarray) -> np.ndarray:
        """
        Project a continuous float vector to bipolar HDC space.

        Args:
            embedding: Float vector of shape (source_dim,)

        Returns:
            Bipolar int8 vector of shape (target_dim,) with values in {-1, +1}

        Raises:
            ValueError: If embedding shape doesn't match source_dim
        """
        embedding = np.asarray(embedding, dtype=np.float32)
        if embedding.shape != (self.source_dim,):
            raise ValueError(
                f"Expected embedding of shape ({self.source_dim},), "
                f"got {embedding.shape}"
            )
        projected = embedding @ self._projection_matrix
        return np.where(projected >= 0, 1, -1).astype(np.int8)

    def project_spatial(
        self,
        feature_map: np.ndarray,
        grid: Tuple[int, int] = (8, 8),
    ) -> np.ndarray:
        """
        Project a 2D spatial feature map to bipolar HDC space.

        Pools the feature map to a fixed grid, flattens, and projects.
        Useful for depth maps, segmentation masks, attention maps, etc.

        Args:
            feature_map: 2D array of shape (H, W) or 3D array of shape (H, W, C)
            grid: Target grid size for average pooling (default: 8x8)

        Returns:
            Bipolar int8 vector of shape (target_dim,) with values in {-1, +1}
        """
        feature_map = np.asarray(feature_map, dtype=np.float32)

        if feature_map.ndim == 2:
            pooled = self._pool_2d(feature_map, grid)
        elif feature_map.ndim == 3:
            # Pool each channel separately, then concatenate
            channels = []
            for c in range(feature_map.shape[2]):
                channels.append(self._pool_2d(feature_map[:, :, c], grid))
            pooled = np.concatenate(channels)
        else:
            raise ValueError(
                f"Expected 2D or 3D feature map, got {feature_map.ndim}D"
            )

        # Flatten and project (may need a different projector if dims differ)
        flat = pooled.flatten()
        if flat.shape[0] != self.source_dim:
            raise ValueError(
                f"Pooled feature map has {flat.shape[0]} elements, "
                f"but projector expects source_dim={self.source_dim}. "
                f"Adjust grid size or source_dim."
            )
        return self.project(flat)

    @staticmethod
    def _pool_2d(arr: np.ndarray, grid: Tuple[int, int]) -> np.ndarray:
        """Average-pool a 2D array to a fixed grid size."""
        h, w = arr.shape
        gh, gw = grid
        # Compute block sizes
        bh = h / gh
        bw = w / gw
        result = np.zeros((gh, gw), dtype=np.float32)
        for i in range(gh):
            r0 = int(round(i * bh))
            r1 = int(round((i + 1) * bh))
            for j in range(gw):
                c0 = int(round(j * bw))
                c1 = int(round((j + 1) * bw))
                block = arr[r0:r1, c0:c1]
                if block.size > 0:
                    result[i, j] = block.mean()
        return result

    @classmethod
    def clear_cache(cls):
        """Clear the projection matrix cache."""
        cls._matrix_cache.clear()
