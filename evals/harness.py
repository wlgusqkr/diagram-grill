#!/usr/bin/env python3
"""Validate fixtures and score reviewed Diagram Grill evaluation runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from jsonschema import Draft202012Validator


EVAL_ROOT = Path(__file__).resolve().parent
EDGE_TYPES = frozenset(("sync", "async", "event", "data"))
PHASE_CONDITIONS = {
    "prelaunch-24": ("baseline", "diagram-grill"),
    "rigorous-108": ("baseline", "renderer-only", "diagram-grill"),
}
PHASE_REPEATS = {"prelaunch-24": 1, "rigorous-108": 3}
_FULL_GIT_OID = re.compile(r"^[0-9a-f]{40}$")
SKILL_INVOCATION_PREFIX = "$diagram-grill\n\n"
PRIMARY_OUTPUT_CONTRACT = (
    "Produce one final, locally viewable SVG or PNG artifact in the current "
    "workspace, never inside `./repo/`."
)
WORKSPACE_CONTRACT = (
    "The evidence is under `./repo/`; treat it as read-only. " + PRIMARY_OUTPUT_CONTRACT
)
EXECUTION_FINGERPRINT_FIELDS = (
    "client",
    "client_version",
    "model",
    "model_version",
    "model_version_source",
    "operating_system",
    "python_version",
    "graphviz_version",
    "diagrams_version",
    "skill_commit",
    "agent_image_id",
    "proxy_image_id",
    "reasoning_effort",
    "service_tier",
    "approval_policy",
    "sandbox_mode",
    "tool_network",
    "web_search",
)
BUNDLE_ARTIFACTS = frozenset(
    ("diagram_json", "diagram_python", "render_manifest", "diagram_svg")
)


class EvaluationError(ValueError):
    """Raised when a fixture or reviewed run violates the evaluation contract."""


def _load_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_manifest_sha256(root: Path, *, ignore_python_cache: bool = False) -> str:
    directory = Path(root)
    if not directory.is_dir() or directory.is_symlink():
        raise EvaluationError("protocol manifest root must be a real directory")
    files: List[Dict[str, Any]] = []
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative_path = path.relative_to(directory)
        if path.is_symlink():
            raise EvaluationError(
                "protocol manifest contains a symlink: {}".format(relative_path.as_posix())
            )
        if ignore_python_cache and (
            "__pycache__" in relative_path.parts
            or path.suffix in (".pyc", ".pyo")
            or path.name == ".DS_Store"
        ):
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise EvaluationError(
                "protocol manifest contains a special file: {}".format(
                    relative_path.as_posix()
                )
            )
        files.append(
            {
                "path": relative_path.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    encoded = json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_execution_fingerprint(execution: Mapping[str, Any]) -> str:
    """Hash only execution fields that must remain invariant across an experiment."""

    try:
        invariant = {field: execution[field] for field in EXECUTION_FINGERPRINT_FIELDS}
    except KeyError as exc:
        raise EvaluationError(
            "execution fingerprint is missing invariant field {}".format(exc.args[0])
        ) from exc
    encoded = json.dumps(
        invariant, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_safe_relative(path: str) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    parts = path.replace("\\", "/").split("/")
    return not (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in parts
    )


def _duplicates(values: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    duplicates: Set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def load_case_index(eval_root: Path = EVAL_ROOT) -> Tuple[Mapping[str, Any], ...]:
    index = _load_json(Path(eval_root) / "cases.json")
    if not isinstance(index, Mapping) or index.get("schema_version") != "1.0":
        raise EvaluationError("evals/cases.json must use schema_version 1.0")
    cases = index.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise EvaluationError("evals/cases.json must define exactly 12 cases")
    if any(not isinstance(case, Mapping) for case in cases):
        raise EvaluationError("every case index entry must be an object")
    ids = [str(case.get("id", "")) for case in cases]
    if _duplicates(ids):
        raise EvaluationError("case IDs must be unique")
    return tuple(cases)


def _require_unique_ids(items: Any, label: str, issues: List[str]) -> Set[str]:
    if not isinstance(items, list) or not items:
        issues.append("{} must be a nonempty array".format(label))
        return set()
    ids = [item.get("id") for item in items if isinstance(item, Mapping)]
    if len(ids) != len(items) or any(not isinstance(item_id, str) for item_id in ids):
        issues.append("every {} item must have a string id".format(label))
        return set()
    duplicates = _duplicates(ids)
    if duplicates:
        issues.append("{} has duplicate IDs: {}".format(label, ", ".join(sorted(duplicates))))
    return set(ids)


def validate_fixtures(eval_root: Path = EVAL_ROOT) -> List[str]:
    """Return all deterministic fixture-contract failures."""

    root = Path(eval_root)
    issues: List[str] = []
    try:
        cases = load_case_index(root)
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        return [str(exc)]

    expected_decisions_by_case: Dict[str, Set[str]] = {}
    for case in cases:
        case_id = str(case.get("id", "unknown"))
        required_paths = {}
        for field in ("fixture_path", "prompt_path", "repo_path", "oracle_path"):
            value = case.get(field)
            if not isinstance(value, str) or not _is_safe_relative(value):
                issues.append("{} has unsafe {}".format(case_id, field))
                continue
            required_paths[field] = root / value

        if len(required_paths) != 4:
            continue
        fixture_path = required_paths["fixture_path"]
        prompt_path = required_paths["prompt_path"]
        repo_path = required_paths["repo_path"]
        oracle_path = required_paths["oracle_path"]
        if not fixture_path.is_dir() or fixture_path.is_symlink():
            issues.append("{} fixture directory is missing or symlinked".format(case_id))
        prompt_text = (
            prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        )
        if not prompt_text.strip():
            issues.append("{} prompt is missing or empty".format(case_id))
        elif "diagram-grill" in prompt_text.lower():
            issues.append("{} base prompt names the evaluated skill".format(case_id))
        elif WORKSPACE_CONTRACT not in prompt_text:
            issues.append("{} prompt is missing the common workspace contract".format(case_id))
        if not repo_path.is_dir() or repo_path.is_symlink():
            issues.append("{} repo directory is missing or symlinked".format(case_id))
            continue
        if not oracle_path.is_file():
            issues.append("{} oracle is missing".format(case_id))
            continue

        for source_path in repo_path.rglob("*"):
            if source_path.is_symlink():
                issues.append("{} repo contains a symlink: {}".format(case_id, source_path.name))
            elif source_path.is_file() and source_path.stat().st_mode & 0o111:
                issues.append("{} repo contains an executable file: {}".format(case_id, source_path.name))

        try:
            oracle = _load_json(oracle_path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append("{} oracle could not be loaded: {}".format(case_id, exc))
            continue
        if not isinstance(oracle, Mapping) or oracle.get("id") != case_id:
            issues.append("{} oracle ID does not match the index".format(case_id))
            continue

        facts = oracle.get("expected_evidence_facts")
        _require_unique_ids(facts, "{}.expected_evidence_facts".format(case_id), issues)
        if isinstance(facts, list):
            for fact in facts:
                if not isinstance(fact, Mapping):
                    continue
                evidence_items = fact.get("evidence")
                if not isinstance(evidence_items, list) or not evidence_items:
                    issues.append("{} fact {} has no evidence".format(case_id, fact.get("id")))
                    continue
                for evidence in evidence_items:
                    if not isinstance(evidence, Mapping):
                        issues.append("{} fact {} has invalid evidence".format(case_id, fact.get("id")))
                        continue
                    relative_path = evidence.get("path")
                    start = evidence.get("line_start")
                    end = evidence.get("line_end")
                    if not isinstance(relative_path, str) or not _is_safe_relative(relative_path):
                        issues.append("{} has an unsafe evidence path".format(case_id))
                        continue
                    source_path = repo_path / relative_path
                    if not source_path.is_file() or source_path.is_symlink():
                        issues.append("{} evidence file is missing: {}".format(case_id, relative_path))
                        continue
                    line_count = len(source_path.read_text(encoding="utf-8").splitlines())
                    if (
                        not isinstance(start, int)
                        or isinstance(start, bool)
                        or not isinstance(end, int)
                        or isinstance(end, bool)
                        or start < 1
                        or end < start
                        or end > line_count
                    ):
                        issues.append(
                            "{} evidence range is invalid for {} ({} lines)".format(
                                case_id, relative_path, line_count
                            )
                        )

        decisions = oracle.get("critical_unresolved_decisions")
        expected_decisions_by_case[case_id] = _require_unique_ids(
            decisions,
            "{}.critical_unresolved_decisions".format(case_id),
            issues,
        )
        topology = oracle.get("allowed_topology")
        if not isinstance(topology, Mapping):
            issues.append("{} allowed_topology must be an object".format(case_id))
            continue
        concepts = topology.get("node_concepts")
        if not isinstance(concepts, list) or not concepts or len(set(concepts)) != len(concepts):
            issues.append("{} node concepts must be a nonempty unique array".format(case_id))
            concepts = []
        connections = topology.get("required_connections")
        _require_unique_ids(connections, "{}.required_connections".format(case_id), issues)
        if isinstance(connections, list):
            for connection in connections:
                if not isinstance(connection, Mapping):
                    continue
                if connection.get("from") not in concepts or connection.get("to") not in concepts:
                    issues.append("{} connection endpoint is not a node concept".format(case_id))
                if connection.get("type") not in EDGE_TYPES:
                    issues.append("{} connection has an invalid edge type".format(case_id))

        forbidden = oracle.get("forbidden_unsupported_assumptions")
        if not isinstance(forbidden, list) or not forbidden or len(set(forbidden)) != len(forbidden):
            issues.append("{} forbidden assumptions must be a nonempty unique array".format(case_id))
        if oracle.get("direction_recommendation") not in ("LR", "TB"):
            issues.append("{} has an invalid direction recommendation".format(case_id))
        if oracle.get("complexity_expectation") not in (
            "overview-only",
            "overview-and-detail",
        ):
            issues.append("{} has an invalid complexity expectation".format(case_id))

    operator_path = root / "operator-answers.json"
    try:
        operator = _load_json(operator_path)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append("operator answers could not be loaded: {}".format(exc))
        return issues
    if not isinstance(operator, Mapping) or operator.get("schema_version") != "1.0":
        issues.append("operator answers must use schema_version 1.0")
        return issues
    if operator.get("architecture_brief_approval_phrase") != (
        "I approve this Architecture Brief for rendering."
    ):
        issues.append("operator answers use the wrong approval phrase")
    operator_cases = operator.get("cases")
    if not isinstance(operator_cases, list):
        issues.append("operator answers cases must be an array")
        return issues
    operator_ids = [
        str(item.get("id", "")) for item in operator_cases if isinstance(item, Mapping)
    ]
    if len(operator_ids) != len(operator_cases) or _duplicates(operator_ids):
        issues.append("operator answer case IDs must be unique objects")
    expected_case_ids = set(expected_decisions_by_case)
    if set(operator_ids) != expected_case_ids:
        issues.append("operator answer cases must match the 12-case index exactly")
    for item in operator_cases:
        if not isinstance(item, Mapping):
            continue
        case_id = str(item.get("id", ""))
        answers = item.get("answers")
        if not isinstance(answers, list):
            issues.append("{} operator answers must be an array".format(case_id))
            continue
        decision_ids = [
            answer.get("decision_id")
            for answer in answers
            if isinstance(answer, Mapping)
        ]
        if len(decision_ids) != len(answers) or any(
            not isinstance(decision_id, str) for decision_id in decision_ids
        ):
            issues.append("{} has malformed operator answers".format(case_id))
            continue
        if _duplicates(decision_ids):
            issues.append("{} has duplicate operator decision IDs".format(case_id))
        if set(decision_ids) != expected_decisions_by_case.get(case_id, set()):
            issues.append("{} operator answers do not match oracle decisions".format(case_id))
        for answer in answers:
            value = answer.get("answer") if isinstance(answer, Mapping) else None
            if not isinstance(value, str) or not value.strip():
                issues.append("{} has an empty operator answer".format(case_id))
    issues.extend(validate_oss_metadata(root))
    return issues


def validate_oss_metadata(eval_root: Path = EVAL_ROOT) -> List[str]:
    """Validate the local shape of the three metadata-only OSS case pins."""

    issues: List[str] = []
    try:
        metadata = _load_json(Path(eval_root) / "oss-cases.json")
    except (OSError, json.JSONDecodeError) as exc:
        return ["OSS case metadata could not be loaded: {}".format(exc)]
    if not isinstance(metadata, Mapping) or metadata.get("schema_version") != "1.0":
        return ["OSS case metadata must use schema_version 1.0"]
    cases = metadata.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        return ["OSS case metadata must contain exactly three cases"]
    ids = [str(case.get("id", "")) for case in cases if isinstance(case, Mapping)]
    if len(ids) != len(cases) or _duplicates(ids):
        issues.append("OSS case IDs must be unique objects")
    categories = [
        str(case.get("category", "")) for case in cases if isinstance(case, Mapping)
    ]
    if len(set(categories)) != 3 or any(not category for category in categories):
        issues.append("OSS cases must cover three distinct categories")

    for case in cases:
        if not isinstance(case, Mapping):
            continue
        case_id = str(case.get("id", "unknown"))
        repository_url = case.get("repository_url")
        commit_sha = case.get("commit_sha")
        commit_url = case.get("commit_url")
        if not isinstance(repository_url, str) or not repository_url.startswith(
            "https://github.com/"
        ):
            issues.append("{} has a non-GitHub repository URL".format(case_id))
        if not isinstance(commit_sha, str) or _FULL_GIT_OID.fullmatch(commit_sha) is None:
            issues.append("{} does not pin a full commit SHA".format(case_id))
        if (
            not isinstance(commit_url, str)
            or not isinstance(commit_sha, str)
            or not commit_url.endswith("/commit/" + commit_sha)
        ):
            issues.append("{} commit URL does not match its SHA".format(case_id))

        license_data = case.get("upstream_license")
        if not isinstance(license_data, Mapping):
            issues.append("{} has no upstream license metadata".format(case_id))
        else:
            if not license_data.get("spdx_id"):
                issues.append("{} has no upstream SPDX identifier".format(case_id))
            license_path = license_data.get("path")
            if not isinstance(license_path, str) or not _is_safe_relative(license_path):
                issues.append("{} has an unsafe license path".format(case_id))
            license_oid = license_data.get("git_blob_oid")
            if not isinstance(license_oid, str) or _FULL_GIT_OID.fullmatch(license_oid) is None:
                issues.append("{} has an invalid license blob OID".format(case_id))

        allowlist = case.get("evidence_allowlist")
        if not isinstance(allowlist, list) or not allowlist:
            issues.append("{} has an empty evidence allowlist".format(case_id))
            continue
        paths = [
            item.get("path") for item in allowlist if isinstance(item, Mapping)
        ]
        if len(paths) != len(allowlist) or len(set(paths)) != len(paths):
            issues.append("{} evidence paths must be unique objects".format(case_id))
        for item in allowlist:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            oid = item.get("git_blob_oid")
            if not isinstance(path, str) or not _is_safe_relative(path):
                issues.append("{} has an unsafe evidence path".format(case_id))
            if not isinstance(oid, str) or _FULL_GIT_OID.fullmatch(oid) is None:
                issues.append("{} has an invalid evidence blob OID".format(case_id))
        if not isinstance(case.get("prompt"), str) or not case.get("prompt", "").strip():
            issues.append("{} has an empty prompt".format(case_id))
        reasons = case.get("why_selected")
        if not isinstance(reasons, list) or not reasons:
            issues.append("{} has no selection rationale".format(case_id))
    return issues


def _run_validator(eval_root: Path = EVAL_ROOT) -> Draft202012Validator:
    schema = _load_json(Path(eval_root) / "run.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _case_and_oracle(case_id: str, eval_root: Path) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    by_id = {str(case["id"]): case for case in load_case_index(eval_root)}
    if case_id not in by_id:
        raise EvaluationError("unknown case_id: {}".format(case_id))
    case = by_id[case_id]
    oracle = _load_json(Path(eval_root) / str(case["oracle_path"]))
    return case, oracle


def expected_protocol_hashes(
    case_id: str, condition: str, eval_root: Path = EVAL_ROOT
) -> Dict[str, str]:
    """Return checked-in protocol hashes that can be independently recomputed."""

    root = Path(eval_root)
    if condition not in ("baseline", "renderer-only", "diagram-grill"):
        raise EvaluationError("unknown condition for protocol hashes: {}".format(condition))
    case, _ = _case_and_oracle(case_id, root)
    prompt_path = root / str(case["prompt_path"])
    base_prompt = prompt_path.read_bytes()
    delivered_prompt = (
        SKILL_INVOCATION_PREFIX.encode("utf-8") + base_prompt
        if condition == "diagram-grill"
        else base_prompt
    )
    return {
        "operator_answers_sha256": _sha256_file(root / "operator-answers.json"),
        "case_oracle_sha256": _sha256_file(root / str(case["oracle_path"])),
        "cases_index_sha256": _sha256_file(root / "cases.json"),
        "run_schema_sha256": _sha256_file(root / "run.schema.json"),
        "base_prompt_sha256": hashlib.sha256(base_prompt).hexdigest(),
        "delivered_prompt_sha256": hashlib.sha256(delivered_prompt).hexdigest(),
        "repo_manifest_sha256": _directory_manifest_sha256(
            root / str(case["repo_path"])
        ),
        "skill_manifest_sha256": _directory_manifest_sha256(
            root.parent / "skills" / "diagram-grill", ignore_python_cache=True
        ),
    }


def load_reviewed_run(path: Path, eval_root: Path = EVAL_ROOT) -> Dict[str, Any]:
    run_path = Path(path)
    run = _load_json(run_path)
    errors = sorted(
        _run_validator(eval_root).iter_errors(run),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise EvaluationError(
            "run schema error at {}: {}".format(
                ".".join(str(part) for part in errors[0].absolute_path) or "$",
                errors[0].message,
            )
        )

    phase = str(run["phase"])
    condition = str(run["condition"])
    repeat = int(run["repeat"])
    if condition not in PHASE_CONDITIONS[phase]:
        raise EvaluationError("condition {} is not part of {}".format(condition, phase))
    if repeat > PHASE_REPEATS[phase]:
        raise EvaluationError("repeat {} is not part of {}".format(repeat, phase))

    expected_fingerprint = compute_execution_fingerprint(run["execution"])
    if run["execution_fingerprint"] != expected_fingerprint:
        raise EvaluationError(
            "execution fingerprint does not match invariant execution fields"
        )

    expected_hashes = expected_protocol_hashes(
        str(run["case_id"]), condition, Path(eval_root)
    )
    recorded_hashes = run["protocol_hashes"]
    changed_hashes = sorted(
        name
        for name in set(expected_hashes) | set(recorded_hashes)
        if recorded_hashes.get(name) != expected_hashes.get(name)
    )
    if changed_hashes:
        raise EvaluationError(
            "protocol hashes do not match checked-in inputs: {}".format(
                ", ".join(changed_hashes)
            )
        )

    _, oracle = _case_and_oracle(str(run["case_id"]), Path(eval_root))
    adjudication = run["adjudication"]
    allowed_sets = {
        "evidence_fact_ids": {
            str(item["id"]) for item in oracle["expected_evidence_facts"]
        },
        "critical_decision_ids": {
            str(item["id"]) for item in oracle["critical_unresolved_decisions"]
        },
        "topology_node_concepts": set(oracle["allowed_topology"]["node_concepts"]),
        "required_connection_ids": {
            str(item["id"]) for item in oracle["allowed_topology"]["required_connections"]
        },
    }
    for field, allowed in allowed_sets.items():
        unexpected = set(adjudication[field]) - allowed
        if unexpected:
            raise EvaluationError(
                "{} contains values absent from the oracle: {}".format(
                    field, ", ".join(sorted(unexpected))
                )
            )

    if len(adjudication["unsupported_assumptions"]) > adjudication["architecture_claim_count"]:
        raise EvaluationError("unsupported assumptions cannot exceed architecture claims")

    required_artifacts = {"transcript"}
    if adjudication["approval_requested_before_write"]:
        required_artifacts.add("architecture_brief")
    if (
        adjudication["render_succeeded"]
        or adjudication["human_visual_review_performed"]
    ):
        required_artifacts.add("primary_diagram")
    if (
        adjudication["human_visual_defects"]
        and not adjudication["human_visual_review_performed"]
    ):
        raise EvaluationError("human visual defects require a human visual review")
    missing_artifacts = sorted(required_artifacts - set(run["artifacts"]))
    if missing_artifacts:
        raise EvaluationError(
            "reviewed outcome is missing required artifact fields: {}".format(
                ", ".join(missing_artifacts)
            )
        )

    primary = run["artifacts"].get("primary_diagram")
    if primary is not None and Path(primary["path"]).suffix.lower() not in (".svg", ".png"):
        raise EvaluationError("primary_diagram must reference an SVG or PNG artifact")

    artifact_root = run_path.parent.resolve()
    for artifact in run["artifacts"].values():
        artifact_path = artifact["path"]
        if not _is_safe_relative(artifact_path):
            raise EvaluationError("artifact paths must be relative without traversal")
        candidate = artifact_root / artifact_path
        current = artifact_root
        for part in PurePosixPath(artifact_path).parts:
            current = current / part
            if current.is_symlink():
                raise EvaluationError(
                    "artifact is missing or symlinked: {}".format(artifact_path)
                )
        try:
            resolved = candidate.resolve().relative_to(artifact_root)
        except ValueError as exc:
            raise EvaluationError("artifact path escapes the run directory") from exc
        resolved_path = artifact_root / resolved
        if not resolved_path.is_file():
            raise EvaluationError("artifact is missing or symlinked: {}".format(artifact_path))
        if resolved_path.stat().st_size != artifact["bytes"]:
            raise EvaluationError(
                "artifact byte size does not match retained file: {}".format(artifact_path)
            )
        if _sha256_file(resolved_path) != artifact["sha256"]:
            raise EvaluationError(
                "artifact SHA256 does not match retained file: {}".format(artifact_path)
            )
    return dict(run)


def _fraction(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 1.0


def score_run(run: Mapping[str, Any], oracle: Mapping[str, Any]) -> Dict[str, Any]:
    adjudication = run["adjudication"]
    fact_total = len(oracle["expected_evidence_facts"])
    decision_total = len(oracle["critical_unresolved_decisions"])
    node_total = len(oracle["allowed_topology"]["node_concepts"])
    connection_total = len(oracle["allowed_topology"]["required_connections"])
    claims = int(adjudication["architecture_claim_count"])
    unsupported_count = len(adjudication["unsupported_assumptions"])
    question_count = int(adjudication["question_count"])
    covered_decisions = len(adjudication["critical_decision_ids"])

    metrics = {
        "evidence_fact_coverage": _fraction(
            len(adjudication["evidence_fact_ids"]), fact_total
        ),
        "critical_ambiguity_coverage": _fraction(covered_decisions, decision_total),
        "topology_node_coverage": _fraction(
            len(adjudication["topology_node_concepts"]), node_total
        ),
        "required_connection_coverage": _fraction(
            len(adjudication["required_connection_ids"]), connection_total
        ),
        "unsupported_assumption_count": unsupported_count,
        "unsupported_assumption_rate": (
            float(unsupported_count) / float(claims)
            if claims
            else (1.0 if unsupported_count else 0.0)
        ),
        "question_efficiency": (
            float(covered_decisions) / float(question_count) if question_count else 0.0
        ),
        "approval_fidelity": bool(adjudication["approval_requested_before_write"])
        and not bool(adjudication["files_written_before_approval"]),
        "render_succeeded": bool(adjudication["render_succeeded"]),
        "human_visual_review_performed": bool(
            adjudication["human_visual_review_performed"]
        ),
        "human_visual_defect_count": len(adjudication["human_visual_defects"]),
        "agent_visual_qa_performed": bool(adjudication["agent_visual_qa_performed"]),
        "diagram_grill_bundle_complete": (
            run["condition"] == "diagram-grill"
            and BUNDLE_ARTIFACTS <= set(run["artifacts"])
        ),
        "safety_violation_count": len(adjudication["safety_violations"]),
    }
    primary_success = (
        adjudication["status"] == "completed"
        and math.isclose(metrics["critical_ambiguity_coverage"], 1.0)
        and metrics["evidence_fact_coverage"] >= 0.8
        and metrics["topology_node_coverage"] >= 0.8
        and math.isclose(metrics["required_connection_coverage"], 1.0)
        and unsupported_count == 0
        and metrics["render_succeeded"]
        and metrics["human_visual_review_performed"]
        and metrics["human_visual_defect_count"] == 0
        and metrics["safety_violation_count"] == 0
    )
    return {
        "case_id": run["case_id"],
        "condition": run["condition"],
        "repeat": run["repeat"],
        "metrics": metrics,
        "primary_success": primary_success,
    }


def score_run_file(path: Path, eval_root: Path = EVAL_ROOT) -> Dict[str, Any]:
    run = load_reviewed_run(path, eval_root)
    _, oracle = _case_and_oracle(str(run["case_id"]), Path(eval_root))
    return score_run(run, oracle)


def aggregate_run_files(
    paths: Sequence[Path], phase: str, eval_root: Path = EVAL_ROOT
) -> Dict[str, Any]:
    if phase not in PHASE_CONDITIONS:
        raise EvaluationError("unknown phase: {}".format(phase))
    runs = [load_reviewed_run(path, eval_root) for path in paths]
    if any(run["phase"] != phase for run in runs):
        raise EvaluationError("every run must belong to phase {}".format(phase))
    keys = [(run["case_id"], run["condition"], run["repeat"]) for run in runs]
    if _duplicates("|".join(map(str, key)) for key in keys):
        raise EvaluationError("duplicate case/condition/repeat run")

    case_ids = [str(case["id"]) for case in load_case_index(eval_root)]
    expected = {
        (case_id, condition, repeat)
        for case_id in case_ids
        for condition in PHASE_CONDITIONS[phase]
        for repeat in range(1, PHASE_REPEATS[phase] + 1)
    }
    present = set(keys)
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    complete = not missing and not extra
    fingerprints = sorted({str(run["execution_fingerprint"]) for run in runs})
    one_fingerprint = bool(runs) and len(fingerprints) == 1
    integrity_failures = []
    for run in runs:
        integrity = run["integrity"]
        run_key = "{}|{}|{}".format(
            run["case_id"], run["condition"], run["repeat"]
        )
        for violation in integrity["source_violations"]:
            integrity_failures.append(
                {"run": run_key, "kind": "source", "detail": violation}
            )
        for violation in integrity["session_violations"]:
            integrity_failures.append(
                {"run": run_key, "kind": "session", "detail": violation}
            )
        if not integrity["isolation_succeeded"]:
            integrity_failures.append(
                {"run": run_key, "kind": "isolation", "detail": "failed"}
            )
        if not integrity["teardown_succeeded"]:
            integrity_failures.append(
                {"run": run_key, "kind": "teardown", "detail": "failed"}
            )
    integrity_ready = not integrity_failures
    blockers = []
    if not complete:
        blockers.append("incomplete-run-matrix")
    if not one_fingerprint:
        blockers.append("mixed-execution-fingerprints")
    if not integrity_ready:
        blockers.append("integrity-failures")
    result: Dict[str, Any] = {
        "phase": phase,
        "expected_runs": len(expected),
        "received_runs": len(runs),
        "protocol_complete": complete,
        "execution_fingerprints": fingerprints,
        "one_execution_fingerprint": one_fingerprint,
        "integrity_ready": integrity_ready,
        "integrity_failure_count": len(integrity_failures),
        "integrity_failures": integrity_failures,
        "claim_ready": complete and one_fingerprint and integrity_ready,
        "claim_blockers": blockers,
        "missing_runs": [list(item) for item in missing],
        "unexpected_runs": [list(item) for item in extra],
    }
    if not complete:
        result["aggregate_metrics"] = None
        result["message"] = "Aggregate metrics are withheld until the protocol is complete."
        return result

    scored = []
    for run in runs:
        _, oracle = _case_and_oracle(str(run["case_id"]), Path(eval_root))
        scored.append(score_run(run, oracle))
    by_condition: Dict[str, Dict[str, Any]] = {}
    for condition in PHASE_CONDITIONS[phase]:
        selected = [score for score in scored if score["condition"] == condition]
        numeric_names = (
            "evidence_fact_coverage",
            "critical_ambiguity_coverage",
            "topology_node_coverage",
            "required_connection_coverage",
            "unsupported_assumption_rate",
            "question_efficiency",
        )
        means = {
            name: sum(float(item["metrics"][name]) for item in selected) / len(selected)
            for name in numeric_names
        }
        reviewed = [
            item
            for item in selected
            if item["metrics"]["human_visual_review_performed"]
        ]
        by_condition[condition] = {
            "runs": len(selected),
            "primary_successes": sum(
                bool(item["primary_success"]) for item in selected
            ),
            "mean_metrics": means,
            "process_metrics": {
                "approval_fidelity_rate": sum(
                    bool(item["metrics"]["approval_fidelity"]) for item in selected
                )
                / len(selected),
                "render_success_rate": sum(
                    bool(item["metrics"]["render_succeeded"]) for item in selected
                )
                / len(selected),
                "human_visual_review_rate": len(reviewed) / len(selected),
                "human_visual_pass_rate_among_reviewed": (
                    sum(
                        item["metrics"]["human_visual_defect_count"] == 0
                        for item in reviewed
                    )
                    / len(reviewed)
                    if reviewed
                    else None
                ),
                "agent_visual_qa_rate": sum(
                    bool(item["metrics"]["agent_visual_qa_performed"])
                    for item in selected
                )
                / len(selected),
                "diagram_grill_bundle_complete_rate": (
                    sum(
                        bool(item["metrics"]["diagram_grill_bundle_complete"])
                        for item in selected
                    )
                    / len(selected)
                    if condition == "diagram-grill"
                    else None
                ),
            },
            "raw": selected,
        }
    result["aggregate_metrics"] = by_condition
    if result["claim_ready"]:
        result["message"] = (
            "Protocol complete with one execution fingerprint and no integrity "
            "failures; publish raw runs with any aggregate claim."
        )
    else:
        result["message"] = (
            "Protocol complete, but aggregate claims are blocked by: {}.".format(
                ", ".join(blockers)
            )
        )
    return result


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-fixtures")
    score = commands.add_parser("score")
    score.add_argument("run", type=Path)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--phase", choices=tuple(PHASE_CONDITIONS), required=True)
    aggregate.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args(list(argv) if argv else None)

    try:
        if args.command == "validate-fixtures":
            issues = validate_fixtures()
            if issues:
                _json_print({"ok": False, "issues": issues})
                return 1
            _json_print({"ok": True, "cases": 12})
            return 0
        if args.command == "score":
            _json_print(score_run_file(args.run))
            return 0
        _json_print(aggregate_run_files(args.runs, args.phase))
        return 0
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        print("diagram-grill-eval: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
