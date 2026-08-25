"""
Numeric binning encoder for Glyphh SDK.

This module provides functions for encoding numeric values using binning
strategies that preserve similarity between close values.

Encoding Strategies:
    - Thermometer: Adjacent bins share bits (best for similarity)
    - Binary: Standard binary representation (space-efficient)
    - Gray: Gray code (adjacent values differ by 1 bit)

Example:
    >>> from glyphh.core.config import NumericConfig, EncodingStrategy
    >>> config = NumericConfig(bin_width=1.0, encoding_strategy=EncodingStrategy.THERMOMETER)
    >>> bin_num = compute_bin_number(7.2, config)  # Returns 7
    >>> bits = thermometer_encode(bin_num, max_bins=10)  # Returns [1,1,1,1,1,1,1,0,0,0]
"""

from typing import List, Optional
import math

from glyphh.core.config import NumericConfig, EncodingStrategy


def clamp_value(value: float, config: NumericConfig) -> float:
    """
    Clamp a value to the configured min/max bounds.
    
    Args:
        value: The numeric value to clamp
        config: NumericConfig with optional min_value and max_value
    
    Returns:
        The clamped value within [min_value, max_value] bounds
    
    Example:
        >>> config = NumericConfig(bin_width=1.0, min_value=0, max_value=10)
        >>> clamp_value(-5, config)  # Returns 0
        >>> clamp_value(15, config)  # Returns 10
        >>> clamp_value(5, config)   # Returns 5
    """
    result = value
    
    if config.min_value is not None and result < config.min_value:
        result = config.min_value
    
    if config.max_value is not None and result > config.max_value:
        result = config.max_value
    
    return result


def compute_bin_number(value: float, config: NumericConfig) -> int:
    """
    Compute the bin number for a numeric value.
    
    The bin number is calculated as: floor((value - min_value) / bin_width)
    If min_value is not specified, 0 is used as the default.
    
    Args:
        value: The numeric value to bin
        config: NumericConfig with bin_width and optional min_value/max_value
    
    Returns:
        The bin number (non-negative integer)
    
    Example:
        >>> config = NumericConfig(bin_width=1.0)
        >>> compute_bin_number(7.2, config)  # Returns 7
        >>> compute_bin_number(7.9, config)  # Returns 7
        >>> compute_bin_number(8.0, config)  # Returns 8
        
        >>> config = NumericConfig(bin_width=0.5, min_value=5.0)
        >>> compute_bin_number(7.2, config)  # Returns 4 (floor((7.2-5.0)/0.5))
    """
    # Clamp value to bounds first
    clamped = clamp_value(value, config)
    
    # Use min_value as offset, default to 0
    min_val = config.min_value if config.min_value is not None else 0.0
    
    # Compute bin number
    bin_num = math.floor((clamped - min_val) / config.bin_width)
    
    # Ensure non-negative (can happen with floating point edge cases)
    return max(0, bin_num)


def thermometer_encode(bin_number: int, max_bins: int) -> List[int]:
    """
    Encode a bin number using thermometer encoding.
    
    Thermometer encoding sets all bits up to the bin index to 1.
    This preserves similarity: adjacent bins share most bits.
    
    Args:
        bin_number: The bin number to encode (0-indexed)
        max_bins: Maximum number of bins (determines vector length)
    
    Returns:
        List of 0s and 1s representing the thermometer encoding
    
    Example:
        >>> thermometer_encode(0, 5)   # [0, 0, 0, 0, 0]
        >>> thermometer_encode(1, 5)   # [1, 0, 0, 0, 0]
        >>> thermometer_encode(3, 5)   # [1, 1, 1, 0, 0]
        >>> thermometer_encode(5, 5)   # [1, 1, 1, 1, 1]
        
    Note:
        bin_number=0 means no bits set (value in first bin)
        bin_number=N means N bits set
    """
    # Clamp bin_number to valid range
    effective_bins = min(bin_number, max_bins)
    effective_bins = max(0, effective_bins)
    
    # Create thermometer pattern: effective_bins 1s followed by 0s
    return [1] * effective_bins + [0] * (max_bins - effective_bins)


def binary_encode(bin_number: int, num_bits: int) -> List[int]:
    """
    Encode a bin number using standard binary encoding.
    
    Binary encoding represents the bin number in binary form.
    This is space-efficient but doesn't preserve similarity between adjacent bins.
    
    Args:
        bin_number: The bin number to encode
        num_bits: Number of bits in the output (determines max representable value)
    
    Returns:
        List of 0s and 1s representing the binary encoding (MSB first)
    
    Example:
        >>> binary_encode(0, 4)   # [0, 0, 0, 0]
        >>> binary_encode(1, 4)   # [0, 0, 0, 1]
        >>> binary_encode(5, 4)   # [0, 1, 0, 1]
        >>> binary_encode(7, 4)   # [0, 1, 1, 1]
        >>> binary_encode(15, 4)  # [1, 1, 1, 1]
    """
    # Clamp to max representable value
    max_value = (1 << num_bits) - 1
    effective_bin = min(bin_number, max_value)
    effective_bin = max(0, effective_bin)
    
    # Convert to binary, MSB first
    result = []
    for i in range(num_bits - 1, -1, -1):
        result.append((effective_bin >> i) & 1)
    
    return result


def gray_encode(bin_number: int, num_bits: int) -> List[int]:
    """
    Encode a bin number using Gray code encoding.
    
    Gray code ensures adjacent values differ by exactly one bit.
    This provides a balance between similarity preservation and space efficiency.
    
    The Gray code is computed as: bin_number XOR (bin_number >> 1)
    
    Args:
        bin_number: The bin number to encode
        num_bits: Number of bits in the output
    
    Returns:
        List of 0s and 1s representing the Gray code encoding (MSB first)
    
    Example:
        >>> gray_encode(0, 4)   # [0, 0, 0, 0] (0 XOR 0 = 0)
        >>> gray_encode(1, 4)   # [0, 0, 0, 1] (1 XOR 0 = 1)
        >>> gray_encode(2, 4)   # [0, 0, 1, 1] (2 XOR 1 = 3)
        >>> gray_encode(3, 4)   # [0, 0, 1, 0] (3 XOR 1 = 2)
        >>> gray_encode(4, 4)   # [0, 1, 1, 0] (4 XOR 2 = 6)
    """
    # Clamp to max representable value
    max_value = (1 << num_bits) - 1
    effective_bin = min(bin_number, max_value)
    effective_bin = max(0, effective_bin)
    
    # Compute Gray code: n XOR (n >> 1)
    gray = effective_bin ^ (effective_bin >> 1)
    
    # Convert to bit list, MSB first
    result = []
    for i in range(num_bits - 1, -1, -1):
        result.append((gray >> i) & 1)
    
    return result


def encode_numeric_value(
    value: float,
    config: NumericConfig,
    max_bins: Optional[int] = None,
    num_bits: Optional[int] = None
) -> List[int]:
    """
    Encode a numeric value using the configured binning strategy.
    
    This is the main entry point for numeric encoding. It:
    1. Clamps the value to configured bounds
    2. Computes the bin number
    3. Encodes using the configured strategy
    
    Args:
        value: The numeric value to encode
        config: NumericConfig with bin_width, encoding_strategy, and optional bounds
        max_bins: For thermometer encoding, the maximum number of bins.
                  If not provided, computed from bounds or defaults to 100.
        num_bits: For binary/gray encoding, the number of bits.
                  If not provided, computed to fit max_bins or defaults to 8.
    
    Returns:
        List of 0s and 1s representing the encoded value
    
    Example:
        >>> config = NumericConfig(
        ...     bin_width=1.0,
        ...     encoding_strategy=EncodingStrategy.THERMOMETER,
        ...     min_value=0,
        ...     max_value=10
        ... )
        >>> encode_numeric_value(7.2, config)  # [1,1,1,1,1,1,1,0,0,0]
    """
    # Compute bin number
    bin_num = compute_bin_number(value, config)
    
    # Determine max_bins if not provided
    if max_bins is None:
        if config.min_value is not None and config.max_value is not None:
            max_bins = math.ceil((config.max_value - config.min_value) / config.bin_width)
        else:
            max_bins = 100  # Default
    
    # Determine num_bits if not provided
    if num_bits is None:
        # Compute bits needed to represent max_bins
        num_bits = max(1, math.ceil(math.log2(max_bins + 1)))
    
    # Encode based on strategy
    strategy = config.encoding_strategy
    
    if strategy == EncodingStrategy.THERMOMETER:
        return thermometer_encode(bin_num, max_bins)
    elif strategy == EncodingStrategy.BINARY:
        return binary_encode(bin_num, num_bits)
    elif strategy == EncodingStrategy.GRAY:
        return gray_encode(bin_num, num_bits)
    else:
        # Default to thermometer
        return thermometer_encode(bin_num, max_bins)


def compute_similarity(bits1: List[int], bits2: List[int]) -> float:
    """
    Compute the similarity between two bit patterns.
    
    Similarity is computed as the number of matching bits divided by total bits.
    This is equivalent to 1 - (Hamming distance / length).
    
    Args:
        bits1: First bit pattern
        bits2: Second bit pattern (must be same length)
    
    Returns:
        Similarity score between 0.0 and 1.0
    
    Example:
        >>> compute_similarity([1,1,1,0,0], [1,1,0,0,0])  # 0.8 (4/5 match)
        >>> compute_similarity([1,1,1,0,0], [1,0,0,0,0])  # 0.6 (3/5 match)
    """
    if len(bits1) != len(bits2):
        raise ValueError(f"Bit patterns must have same length: {len(bits1)} != {len(bits2)}")
    
    if len(bits1) == 0:
        return 1.0
    
    matches = sum(1 for a, b in zip(bits1, bits2) if a == b)
    return matches / len(bits1)
