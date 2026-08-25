"""Validated architecture IR tooling for Diagram Grill."""

from .errors import DiagramValidationError, ValidationIssue
from .validation import (
    MAX_GROUP_DEPTH,
    SCHEMA_VERSION,
    collect_validation_issues,
    load_and_validate,
    validate_spec,
)

__all__ = [
    "DiagramValidationError",
    "MAX_GROUP_DEPTH",
    "SCHEMA_VERSION",
    "ValidationIssue",
    "collect_validation_issues",
    "load_and_validate",
    "validate_spec",
]
