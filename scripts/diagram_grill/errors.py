"""Errors raised while validating Diagram Grill specifications."""

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class ValidationIssue:
    """One actionable schema or semantic validation failure."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        location = self.path or "$"
        return "{}: {} ({})".format(location, self.message, self.code)


class DiagramValidationError(ValueError):
    """Raised when an architecture specification is not safe to build."""

    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues: Tuple[ValidationIssue, ...] = tuple(issues)
        if not self.issues:
            raise ValueError("DiagramValidationError requires at least one issue")
        super().__init__("\n".join(str(issue) for issue in self.issues))
