"""Schema and semantic validation for the Diagram Grill architecture IR."""

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from jsonschema import Draft202012Validator

from .errors import DiagramValidationError, ValidationIssue


SCHEMA_VERSION = "1.0"
VALIDATION_MODES = frozenset(("draft", "render"))
MAX_GROUP_DEPTH = 3

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "references" / "diagram.schema.json"
_ICON_PATH_RE = re.compile(
    r"^diagrams\.(?!custom(?:\.|$))[a-z][a-z0-9_]*\."
    r"[a-z][a-z0-9_]*\.[A-Z][A-Za-z0-9]*$"
)
_GENERIC_ICON_PATH = "diagrams.generic.blank.Blank"


def _load_schema() -> Dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    Draft202012Validator.check_schema(schema)
    return schema


SCHEMA = _load_schema()
_VALIDATOR = Draft202012Validator(SCHEMA)


def _json_path(parts: Iterable[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += "[{}]".format(part)
        else:
            path += ".{}".format(part)
    return path


def _schema_issues(spec: Any) -> List[ValidationIssue]:
    errors = sorted(
        _VALIDATOR.iter_errors(spec),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        ValidationIssue(
            code="schema",
            path=_json_path(error.absolute_path),
            message=error.message,
        )
        for error in errors
    ]


def _object_list(spec: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = spec.get(key)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return ()
    return tuple(value)


def _duplicate_id_issues(spec: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for collection in ("groups", "nodes", "edges", "decisions", "assumptions"):
        items = _object_list(spec, collection)
        ids = [item.get("id") for item in items if isinstance(item.get("id"), str)]
        duplicate_ids = {item_id for item_id, count in Counter(ids).items() if count > 1}
        for index, item in enumerate(items):
            if item.get("id") in duplicate_ids:
                issues.append(
                    ValidationIssue(
                        code="duplicate-id",
                        path="$.{}[{}].id".format(collection, index),
                        message="ID {!r} is duplicated in {}".format(item.get("id"), collection),
                    )
                )
    return issues


def _group_issues(spec: Mapping[str, Any]) -> List[ValidationIssue]:
    groups = _object_list(spec, "groups")
    group_indexes = {
        group["id"]: index
        for index, group in enumerate(groups)
        if isinstance(group.get("id"), str)
    }
    parents = {
        group["id"]: group.get("parent")
        for group in groups
        if isinstance(group.get("id"), str)
    }
    issues: List[ValidationIssue] = []

    for group_id, parent_id in parents.items():
        if parent_id is not None and parent_id not in parents:
            issues.append(
                ValidationIssue(
                    code="dangling-group",
                    path="$.groups[{}].parent".format(group_indexes[group_id]),
                    message="Parent group {!r} does not exist".format(parent_id),
                )
            )

    reported_cycles: Set[str] = set()
    for group_id in parents:
        chain: List[str] = []
        cursor: Optional[str] = group_id
        while cursor in parents:
            if cursor in chain:
                cycle = chain[chain.index(cursor) :]
                for cycle_id in cycle:
                    if cycle_id not in reported_cycles:
                        issues.append(
                            ValidationIssue(
                                code="group-cycle",
                                path="$.groups[{}].parent".format(group_indexes[cycle_id]),
                                message="Group {!r} participates in a parent cycle".format(cycle_id),
                            )
                        )
                        reported_cycles.add(cycle_id)
                break
            chain.append(cursor)
            cursor = parents[cursor]

        if cursor is None and len(chain) > MAX_GROUP_DEPTH:
            issues.append(
                ValidationIssue(
                    code="group-depth",
                    path="$.groups[{}]".format(group_indexes[group_id]),
                    message="Group {!r} is nested {} levels; maximum is {}".format(
                        group_id, len(chain), MAX_GROUP_DEPTH
                    ),
                )
            )

    return issues


def _reference_issues(spec: Mapping[str, Any]) -> List[ValidationIssue]:
    groups = _object_list(spec, "groups")
    nodes = _object_list(spec, "nodes")
    decisions = _object_list(spec, "decisions")
    group_ids = {group.get("id") for group in groups if isinstance(group.get("id"), str)}
    node_ids = {node.get("id") for node in nodes if isinstance(node.get("id"), str)}
    decision_ids = {
        decision.get("id") for decision in decisions if isinstance(decision.get("id"), str)
    }
    issues: List[ValidationIssue] = []

    for index, node in enumerate(nodes):
        group_id = node.get("group")
        if isinstance(group_id, str) and group_id not in group_ids:
            issues.append(
                ValidationIssue(
                    code="dangling-group",
                    path="$.nodes[{}].group".format(index),
                    message="Group {!r} does not exist".format(group_id),
                )
            )

    for index, edge in enumerate(_object_list(spec, "edges")):
        for endpoint in ("from", "to"):
            node_id = edge.get(endpoint)
            if isinstance(node_id, str) and node_id not in node_ids:
                issues.append(
                    ValidationIssue(
                        code="dangling-node",
                        path="$.edges[{}].{}".format(index, endpoint),
                        message="Node {!r} does not exist".format(node_id),
                    )
                )

    for collection in ("nodes", "edges", "decisions", "assumptions"):
        for item_index, item in enumerate(_object_list(spec, collection)):
            evidence_items = item.get("evidence")
            if not isinstance(evidence_items, list):
                continue
            for evidence_index, evidence in enumerate(evidence_items):
                if not isinstance(evidence, Mapping) or evidence.get("kind") != "decision":
                    continue
                decision_id = evidence.get("decision_id")
                if isinstance(decision_id, str) and decision_id not in decision_ids:
                    issues.append(
                        ValidationIssue(
                            code="dangling-decision",
                            path="$.{}[{}].evidence[{}].decision_id".format(
                                collection, item_index, evidence_index
                            ),
                            message="Decision {!r} does not exist".format(decision_id),
                        )
                    )

    return issues


def _unsafe_evidence_path(path: str) -> bool:
    if "\x00" in path:
        return True
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
    ):
        return True
    normalized_parts = path.replace("\\", "/").split("/")
    return ".." in normalized_parts


def _safety_issues(spec: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    for index, node in enumerate(_object_list(spec, "nodes")):
        icon = node.get("icon")
        if not isinstance(icon, str):
            continue
        if icon == "diagrams.custom" or icon.startswith("diagrams.custom."):
            issues.append(
                ValidationIssue(
                    code="custom-icon",
                    path="$.nodes[{}].icon".format(index),
                    message="diagrams.custom icons are not allowed",
                )
            )
        elif not _ICON_PATH_RE.fullmatch(icon):
            issues.append(
                ValidationIssue(
                    code="icon-path",
                    path="$.nodes[{}].icon".format(index),
                    message=(
                        "Icon must be an exact diagrams.<provider>.<category>.<Class> import path"
                    ),
                )
            )
        fallback_for = node.get("icon_fallback_for")
        if isinstance(fallback_for, str) and icon != _GENERIC_ICON_PATH:
            issues.append(
                ValidationIssue(
                    code="fallback-icon",
                    path="$.nodes[{}].icon".format(index),
                    message=(
                        "A node with icon_fallback_for must use diagrams.generic.blank.Blank"
                    ),
                )
            )

    for collection in ("nodes", "edges", "decisions", "assumptions"):
        for item_index, item in enumerate(_object_list(spec, collection)):
            evidence_items = item.get("evidence")
            if not isinstance(evidence_items, list):
                continue
            for evidence_index, evidence in enumerate(evidence_items):
                if not isinstance(evidence, Mapping) or evidence.get("kind") != "file":
                    continue
                path = evidence.get("path")
                if isinstance(path, str) and _unsafe_evidence_path(path):
                    issues.append(
                        ValidationIssue(
                            code="unsafe-evidence-path",
                            path="$.{}[{}].evidence[{}].path".format(
                                collection, item_index, evidence_index
                            ),
                            message="Evidence path must be a repository-relative path without traversal",
                        )
                    )
                line_start = evidence.get("line_start")
                line_end = evidence.get("line_end")
                if line_end is not None and line_start is None:
                    issues.append(
                        ValidationIssue(
                            code="invalid-line-range",
                            path="$.{}[{}].evidence[{}].line_end".format(
                                collection, item_index, evidence_index
                            ),
                            message="line_end requires line_start",
                        )
                    )
                elif (
                    isinstance(line_start, int)
                    and not isinstance(line_start, bool)
                    and isinstance(line_end, int)
                    and not isinstance(line_end, bool)
                    and line_start > line_end
                ):
                    issues.append(
                        ValidationIssue(
                            code="invalid-line-range",
                            path="$.{}[{}].evidence[{}].line_end".format(
                                collection, item_index, evidence_index
                            ),
                            message="line_end must be greater than or equal to line_start",
                        )
                    )

    return issues


def _render_issues(spec: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    approval = spec.get("approval")
    if not isinstance(approval, Mapping) or approval.get("status") != "approved":
        issues.append(
            ValidationIssue(
                code="approval-required",
                path="$.approval.status",
                message="The architecture brief must be approved before rendering",
            )
        )

    for index, assumption in enumerate(_object_list(spec, "assumptions")):
        if assumption.get("status") == "unresolved":
            issues.append(
                ValidationIssue(
                    code="unresolved-assumption",
                    path="$.assumptions[{}].status".format(index),
                    message="Unresolved assumptions must be approved or rejected before rendering",
                )
            )

    return issues


def collect_validation_issues(spec: Any, *, mode: str = "draft") -> List[ValidationIssue]:
    """Return every schema and semantic issue found in ``spec``.

    ``draft`` mode permits an unapproved brief and unresolved assumptions.
    ``render`` mode requires explicit approval and resolved assumptions.
    """

    if mode not in VALIDATION_MODES:
        raise ValueError(
            "Unknown validation mode {!r}; expected one of {}".format(
                mode, ", ".join(sorted(VALIDATION_MODES))
            )
        )

    issues = _schema_issues(spec)
    if not isinstance(spec, Mapping):
        return issues

    issues.extend(_duplicate_id_issues(spec))
    issues.extend(_group_issues(spec))
    issues.extend(_reference_issues(spec))
    issues.extend(_safety_issues(spec))
    if mode == "render":
        issues.extend(_render_issues(spec))
    return issues


def validate_spec(spec: Any, *, mode: str = "draft") -> None:
    """Raise ``DiagramValidationError`` unless ``spec`` is valid for ``mode``."""

    issues = collect_validation_issues(spec, mode=mode)
    if issues:
        raise DiagramValidationError(issues)


def load_and_validate(path: Path, *, mode: str = "draft") -> Dict[str, Any]:
    """Load a UTF-8 JSON file and validate it for the requested lifecycle mode."""

    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as input_file:
        spec = json.load(input_file)
    validate_spec(spec, mode=mode)
    return spec
