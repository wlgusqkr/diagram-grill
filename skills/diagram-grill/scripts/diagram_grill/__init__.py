"""Validated architecture IR tooling for Diagram Grill.

Runtime modules are intentionally not imported here. This keeps the read-only
preflight command usable before optional Python dependencies are installed.
"""

from .errors import DiagramValidationError, ValidationIssue

__all__ = ["DiagramValidationError", "ValidationIssue"]
