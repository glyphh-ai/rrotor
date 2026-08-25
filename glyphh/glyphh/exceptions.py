"""
Exception hierarchy for the Glyphh SDK.

This module defines all custom exception types used throughout the SDK for
consistent error handling and debugging. All exceptions inherit from GlyphhException
to enable easy catching of SDK-specific errors.

Exception Hierarchy:
====================

GlyphhException (base)
├── ConfigurationException
├── VectorSpaceException
│   ├── DimensionMismatchException
│   └── SpaceIdMismatchException
├── BipolarConstraintException
├── EncodingException
├── ModelValidationException
├── ValidationException
└── OperationException

Usage Examples:
===============

1. Configuration Errors:
   >>> raise ConfigurationException("dimension", "Dimension must be positive")

2. Vector Space Errors:
   >>> raise VectorSpaceException(
   ...     expected="a3f2e8b1",
   ...     actual="b7e9c2a3",
   ...     message="Cannot compare vectors from different spaces"
   ... )

3. Encoding Errors:
   >>> raise EncodingException(
   ...     concept_name="red car",
   ...     reason="Missing required attribute 'type'",
   ...     problematic_input={"color": "red"}
   ... )

4. Model Validation Errors:
   >>> raise ModelValidationException(
   ...     errors=[
   ...         "Model contains no glyphs",
   ...         "Invalid version format: 1.2"
   ...     ]
   ... )
"""

from typing import Any, List, Optional
import logging

# Configure logger for error logging
logger = logging.getLogger(__name__)


# ============================================================================
# Base Exception
# ============================================================================

class GlyphhException(Exception):
    """
    Base exception for all Glyphh SDK errors.
    
    All SDK exceptions inherit from this class to enable easy catching
    of SDK-specific errors while allowing standard Python exceptions
    to propagate normally.
    
    Attributes:
        message: Human-readable error message
        context: Optional dictionary with additional error context
    
    Example:
        >>> try:
        ...     # SDK operation
        ...     pass
        ... except GlyphhException as e:
        ...     print(f"SDK error: {e}")
        ... except Exception as e:
        ...     print(f"Other error: {e}")
    """
    
    def __init__(self, message: str, context: Optional[dict] = None):
        """
        Initialize base exception.
        
        Args:
            message: Human-readable error message
            context: Optional dictionary with additional error context
        """
        self.message = message
        self.context = context or {}
        super().__init__(message)
        
        # Log the exception for debugging
        logger.error(f"{self.__class__.__name__}: {message}", extra={"context": self.context})


# ============================================================================
# Configuration Exceptions
# ============================================================================

class ConfigurationException(GlyphhException):
    """
    Exception raised when configuration validation fails.
    
    This exception is raised when:
    - Required configuration fields are missing
    - Configuration values are invalid (wrong type, out of range, etc.)
    - Configuration file cannot be read or parsed
    
    Attributes:
        field: The configuration field that failed validation
        message: Detailed error message explaining the failure
        expected: Expected value or type (optional)
        actual: Actual value that caused the error (optional)
    
    Example:
        >>> raise ConfigurationException(
        ...     field="dimension",
        ...     message="Dimension must be positive",
        ...     expected="positive integer",
        ...     actual=-1000
        ... )
    """
    
    def __init__(
        self,
        field: str,
        message: str,
        expected: Optional[Any] = None,
        actual: Optional[Any] = None
    ):
        """
        Initialize configuration exception.
        
        Args:
            field: The configuration field that failed validation
            message: Detailed error message
            expected: Expected value or type (optional)
            actual: Actual value that caused the error (optional)
        """
        self.field = field
        self.expected = expected
        self.actual = actual
        
        # Build detailed message
        full_message = f"Configuration error in '{field}': {message}"
        if expected is not None and actual is not None:
            full_message += f" (expected: {expected}, got: {actual})"
        
        context = {
            "field": field,
            "expected": expected,
            "actual": actual
        }
        
        super().__init__(full_message, context)


# ============================================================================
# Vector Space Exceptions
# ============================================================================

class VectorSpaceException(GlyphhException):
    """
    Exception raised when vector space validation fails.
    
    This exception is raised when:
    - Vectors from different spaces are compared or combined
    - Vector dimensions don't match
    - Space IDs don't match
    
    Attributes:
        expected: Expected value (e.g., expected space_id or dimension)
        actual: Actual value that caused the error
        message: Detailed error message
        operation: Optional operation that failed (e.g., "similarity", "bind")
    
    Example:
        >>> raise VectorSpaceException(
        ...     expected="a3f2e8b1",
        ...     actual="b7e9c2a3",
        ...     message="Cannot compare vectors from different spaces",
        ...     operation="similarity"
        ... )
    """
    
    def __init__(
        self,
        expected: Any,
        actual: Any,
        message: str,
        operation: Optional[str] = None
    ):
        """
        Initialize vector space exception.
        
        Args:
            expected: Expected value
            actual: Actual value that caused the error
            message: Detailed error message
            operation: Optional operation that failed
        """
        self.expected = expected
        self.actual = actual
        self.operation = operation
        
        # Build detailed message
        full_message = f"{message}: expected {expected}, got {actual}"
        if operation:
            full_message = f"Operation '{operation}' failed - {full_message}"
        
        context = {
            "expected": expected,
            "actual": actual,
            "operation": operation
        }
        
        super().__init__(full_message, context)


class DimensionMismatchException(VectorSpaceException):
    """
    Exception raised when vector dimensions don't match.
    
    This is a specialized VectorSpaceException for dimension mismatches.
    
    Example:
        >>> raise DimensionMismatchException(
        ...     expected=10000,
        ...     actual=5000,
        ...     message="Vector dimension mismatch",
        ...     operation="bind"
        ... )
    """
    
    def __init__(
        self,
        expected: int,
        actual: int,
        message: str = "Vector dimension mismatch",
        operation: Optional[str] = None
    ):
        """
        Initialize dimension mismatch exception.
        
        Args:
            expected: Expected dimension
            actual: Actual dimension
            message: Detailed error message
            operation: Optional operation that failed
        """
        super().__init__(expected, actual, message, operation)


class SpaceIdMismatchException(VectorSpaceException):
    """
    Exception raised when vector space IDs don't match.
    
    This is a specialized VectorSpaceException for space ID mismatches.
    
    Example:
        >>> raise SpaceIdMismatchException(
        ...     expected="a3f2e8b1",
        ...     actual="b7e9c2a3",
        ...     message="Cannot compare vectors from different spaces",
        ...     operation="similarity"
        ... )
    """
    
    def __init__(
        self,
        expected: str,
        actual: str,
        message: str = "Vector space ID mismatch",
        operation: Optional[str] = None
    ):
        """
        Initialize space ID mismatch exception.
        
        Args:
            expected: Expected space_id
            actual: Actual space_id
            message: Detailed error message
            operation: Optional operation that failed
        """
        super().__init__(expected, actual, message, operation)


# ============================================================================
# Constraint Exceptions
# ============================================================================

class BipolarConstraintException(GlyphhException):
    """
    Exception raised when bipolar constraint {-1, +1} is violated.
    
    This exception is raised when a vector contains values outside the
    bipolar set {-1, +1}. All vectors in the SDK must be bipolar.
    
    Attributes:
        invalid_values: List of invalid values found in the vector
        message: Detailed error message
        vector_info: Optional information about the vector (dimension, source, etc.)
    
    Example:
        >>> raise BipolarConstraintException(
        ...     invalid_values=[0, 2, -3],
        ...     message="Vector contains non-bipolar values",
        ...     vector_info={"dimension": 10000, "source": "custom_encoder"}
        ... )
    """
    
    def __init__(
        self,
        invalid_values: List[Any],
        message: str = "Vector must be bipolar (values in {-1, +1})",
        vector_info: Optional[dict] = None
    ):
        """
        Initialize bipolar constraint exception.
        
        Args:
            invalid_values: List of invalid values found
            message: Detailed error message
            vector_info: Optional information about the vector
        """
        self.invalid_values = invalid_values
        self.vector_info = vector_info or {}
        
        # Build detailed message (limit invalid values to first 10)
        invalid_sample = invalid_values[:10]
        full_message = f"{message}: found invalid values {invalid_sample}"
        if len(invalid_values) > 10:
            full_message += f" (and {len(invalid_values) - 10} more)"
        
        context = {
            "invalid_values": invalid_values,
            "invalid_count": len(invalid_values),
            "vector_info": self.vector_info
        }
        
        super().__init__(full_message, context)


# ============================================================================
# Encoding Exceptions
# ============================================================================

class EncodingException(GlyphhException):
    """
    Exception raised when encoding fails.
    
    This exception is raised when:
    - Required attributes are missing from a concept
    - Attribute values are invalid
    - Encoding process encounters an error
    
    Attributes:
        concept_name: Name of the concept that failed to encode
        reason: Reason for the encoding failure
        problematic_input: The input that caused the failure
        stage: Optional encoding stage where failure occurred
    
    Example:
        >>> raise EncodingException(
        ...     concept_name="red car",
        ...     reason="Missing required attribute 'type'",
        ...     problematic_input={"color": "red"},
        ...     stage="segment_encoding"
        ... )
    """
    
    def __init__(
        self,
        concept_name: str,
        reason: str,
        problematic_input: Optional[Any] = None,
        stage: Optional[str] = None
    ):
        """
        Initialize encoding exception.
        
        Args:
            concept_name: Name of the concept that failed to encode
            reason: Reason for the encoding failure
            problematic_input: The input that caused the failure
            stage: Optional encoding stage where failure occurred
        """
        self.concept_name = concept_name
        self.reason = reason
        self.problematic_input = problematic_input
        self.stage = stage
        
        # Build detailed message
        full_message = f"Failed to encode concept '{concept_name}': {reason}"
        if stage:
            full_message = f"[{stage}] {full_message}"
        
        context = {
            "concept_name": concept_name,
            "reason": reason,
            "problematic_input": problematic_input,
            "stage": stage
        }
        
        # Log the problematic input for debugging
        if problematic_input is not None:
            logger.debug(
                f"Encoding failed for concept '{concept_name}'",
                extra={"problematic_input": problematic_input}
            )
        
        super().__init__(full_message, context)


# ============================================================================
# Model Validation Exceptions
# ============================================================================

class ModelValidationException(GlyphhException):
    """
    Exception raised when model validation fails.
    
    This exception is raised when:
    - Model is missing required components (glyphs, config, etc.)
    - Model has inconsistent vector spaces
    - Model version format is invalid
    - Model fails completeness checks
    
    Attributes:
        errors: List of validation error messages
        model_name: Optional name of the model that failed validation
    
    Example:
        >>> raise ModelValidationException(
        ...     errors=[
        ...         "Model contains no glyphs",
        ...         "Invalid version format: 1.2",
        ...         "Multiple vector spaces detected: ['a3f2e8b1', 'b7e9c2a3']"
        ...     ],
        ...     model_name="my_model"
        ... )
    """
    
    def __init__(self, errors: List[str], model_name: Optional[str] = None):
        """
        Initialize model validation exception.
        
        Args:
            errors: List of validation error messages
            model_name: Optional name of the model
        """
        self.errors = errors
        self.model_name = model_name
        
        # Build detailed message
        error_list = "\n  - ".join(errors)
        full_message = f"Model validation failed with {len(errors)} error(s):\n  - {error_list}"
        if model_name:
            full_message = f"Model '{model_name}' validation failed with {len(errors)} error(s):\n  - {error_list}"
        
        context = {
            "errors": errors,
            "error_count": len(errors),
            "model_name": model_name
        }
        
        super().__init__(full_message, context)


# ============================================================================
# General Validation Exceptions
# ============================================================================

class ValidationException(GlyphhException):
    """
    Exception raised for general validation failures.
    
    This is a catch-all exception for validation errors that don't fit
    into more specific categories.
    
    Attributes:
        field: The field or component that failed validation
        message: Detailed error message
        expected: Expected value or constraint (optional)
        actual: Actual value that failed validation (optional)
    
    Example:
        >>> raise ValidationException(
        ...     field="glyph.identifier",
        ...     message="Invalid identifier format",
        ...     expected="primary_key@timestamp#version",
        ...     actual="invalid_id"
        ... )
    """
    
    def __init__(
        self,
        field: str,
        message: str,
        expected: Optional[Any] = None,
        actual: Optional[Any] = None
    ):
        """
        Initialize validation exception.
        
        Args:
            field: The field or component that failed validation
            message: Detailed error message
            expected: Expected value or constraint (optional)
            actual: Actual value that failed validation (optional)
        """
        self.field = field
        self.expected = expected
        self.actual = actual
        
        # Build detailed message
        full_message = f"Validation error in '{field}': {message}"
        if expected is not None and actual is not None:
            full_message += f" (expected: {expected}, got: {actual})"
        
        context = {
            "field": field,
            "expected": expected,
            "actual": actual
        }
        
        super().__init__(full_message, context)


# ============================================================================
# Operation Exceptions
# ============================================================================

class OperationException(GlyphhException):
    """
    Exception raised when an operation fails.
    
    This exception is raised when:
    - An operation receives invalid inputs
    - An operation cannot complete due to constraints
    - An operation encounters an unexpected error
    
    Attributes:
        operation: Name of the operation that failed
        reason: Reason for the failure
        inputs: Optional information about the inputs
    
    Example:
        >>> raise OperationException(
        ...     operation="similarity",
        ...     reason="Cannot compare glyphs from different vector spaces",
        ...     inputs={"glyph1_space": "a3f2e8b1", "glyph2_space": "b7e9c2a3"}
        ... )
    """
    
    def __init__(
        self,
        operation: str,
        reason: str,
        inputs: Optional[dict] = None
    ):
        """
        Initialize operation exception.
        
        Args:
            operation: Name of the operation that failed
            reason: Reason for the failure
            inputs: Optional information about the inputs
        """
        self.operation = operation
        self.reason = reason
        self.inputs = inputs or {}
        
        # Build detailed message
        full_message = f"Operation '{operation}' failed: {reason}"
        
        context = {
            "operation": operation,
            "reason": reason,
            "inputs": self.inputs
        }
        
        super().__init__(full_message, context)


# ============================================================================
# Utility Functions
# ============================================================================

def log_encoding_failure(
    concept_name: str,
    reason: str,
    problematic_input: Any,
    stage: Optional[str] = None
) -> None:
    """
    Log an encoding failure for debugging.
    
    This function logs encoding failures with full context to help with
    debugging. It should be called whenever encoding fails, even if an
    exception is not raised.
    
    Args:
        concept_name: Name of the concept that failed to encode
        reason: Reason for the encoding failure
        problematic_input: The input that caused the failure
        stage: Optional encoding stage where failure occurred
    
    Example:
        >>> log_encoding_failure(
        ...     concept_name="red car",
        ...     reason="Missing required attribute 'type'",
        ...     problematic_input={"color": "red"},
        ...     stage="segment_encoding"
        ... )
    """
    logger.error(
        f"Encoding failed for concept '{concept_name}': {reason}",
        extra={
            "concept_name": concept_name,
            "reason": reason,
            "problematic_input": problematic_input,
            "stage": stage
        }
    )


def log_validation_failure(
    field: str,
    message: str,
    expected: Optional[Any] = None,
    actual: Optional[Any] = None
) -> None:
    """
    Log a validation failure for debugging.
    
    This function logs validation failures with full context to help with
    debugging. It should be called whenever validation fails.
    
    Args:
        field: The field or component that failed validation
        message: Detailed error message
        expected: Expected value or constraint (optional)
        actual: Actual value that failed validation (optional)
    
    Example:
        >>> log_validation_failure(
        ...     field="dimension",
        ...     message="Dimension must be positive",
        ...     expected="positive integer",
        ...     actual=-1000
        ... )
    """
    logger.error(
        f"Validation failed for '{field}': {message}",
        extra={
            "field": field,
            "message": message,
            "expected": expected,
            "actual": actual
        }
    )
