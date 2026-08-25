#!/usr/bin/env python3
"""Run the fixed prelaunch evaluation one human-reviewed turn at a time.

This module owns the condition-neutral protocol and retained evidence. The
Docker, credential, network, and snapshot boundary lives in ``isolation.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import harness
import isolation


EVAL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = EVAL_ROOT.parent
APPROVAL_PHRASE = "I approve this Architecture Brief for rendering."
UNKNOWN_PHRASE = "That is not specified. Keep it explicit as unknown and do not assume it."
SKILL_INVOCATION_PREFIX = "$diagram-grill\n\n"
CONDITIONS = ("baseline", "diagram-grill")
CODEX_CLI_VERSION = "0.149.1"
MAX_TURNS_PER_RUN = 12
MAX_EVIDENCE_FILES = 1000
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 512
MIN_CREDENTIAL_BYTES = 20
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT_FILE = re.compile(
    r"^attempt-turn-(?P<turn>[0-9]{3})\.(?P<state>in-progress|completed)\.json$"
)
_FORBIDDEN_PACKAGE_PARTS = frozenset((".git", ".github", "evals", "tests"))
_FORBIDDEN_PACKAGE_FILES = frozenset(
    ("operator-answers.json", "cases.json", "oss-cases.json", "run.schema.json")
)
_MAX_PACKAGE_FILES = 500
_MAX_PACKAGE_BYTES = 10 * 1024 * 1024
REVIEWED_RUN_FILENAME = "reviewed-run.json"
_REVIEW_ARTIFACT_NAMES = frozenset(
    (
        "transcript",
        "tool_log",
        "architecture_brief",
        "primary_diagram",
        "diagram_json",
        "diagram_python",
        "render_manifest",
        "diagram_svg",
        "diagram_png",
        "stdout",
        "stderr",
    )
)


class RunnerError(ValueError):
    """Raised when an evaluation run would violate the frozen protocol."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RunnerError("durable evidence write was short")
        view = view[written:]


def _write_json(path: Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-write-".format(target.name), dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        result = os.fstat(descriptor)
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_nlink != 1
            or result.st_uid != expected_uid
            or stat.S_IMODE(result.st_mode) & 0o077
        ):
            raise RunnerError("durable evidence log is not a private regular file")
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(target.parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_manifest(
    root: Path,
    ignore_python_cache: bool = False,
    *,
    max_files: int = MAX_EVIDENCE_FILES,
    max_bytes: int = MAX_EVIDENCE_BYTES,
) -> Dict[str, Any]:
    """Return a bounded content manifest and reject links and special files."""

    directory = Path(root)
    if not directory.is_dir() or directory.is_symlink():
        raise RunnerError("manifest root must be a real directory: {}".format(directory))
    pending = [directory]
    candidates: List[Tuple[Path, str, os.stat_result]] = []
    filesystem_entries = 0
    total_bytes = 0
    while pending:
        parent = pending.pop()
        try:
            children = sorted(os.scandir(parent), key=lambda item: item.name)
        except OSError as exc:
            raise RunnerError("manifest directory is unreadable: {}".format(parent)) from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(directory).as_posix()
            filesystem_entries += 1
            if filesystem_entries > max_files * 2:
                raise RunnerError("manifest contains too many filesystem entries")
            result = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(result.st_mode):
                raise RunnerError("symlinks are not allowed: {}".format(relative))
            if ignore_python_cache and (
                "__pycache__" in path.relative_to(directory).parts
                or path.suffix in (".pyc", ".pyo")
                or path.name == ".DS_Store"
            ):
                continue
            if stat.S_ISDIR(result.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(result.st_mode):
                raise RunnerError("special files are not allowed: {}".format(relative))
            total_bytes += result.st_size
            if len(candidates) >= max_files:
                raise RunnerError("manifest contains more than {} files".format(max_files))
            if total_bytes > max_bytes:
                raise RunnerError("manifest exceeds its {}-byte limit".format(max_bytes))
            candidates.append((path, relative, result))

    files: List[Dict[str, Any]] = []
    for path, relative, before in sorted(candidates, key=lambda item: item[1]):
        digest = _sha256(path)
        after = path.stat(follow_symlinks=False)
        identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if not stat.S_ISREG(after.st_mode) or identity != expected:
            raise RunnerError("manifest file changed while hashing: {}".format(relative))
        files.append({"path": relative, "bytes": before.st_size, "sha256": digest})
    encoded = json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def validate_skill_package(path: Path) -> Dict[str, Any]:
    """Validate the installable package without exposing evaluation material."""

    package = Path(path).resolve(strict=True)
    manifest = directory_manifest(package, ignore_python_cache=True)
    required = (
        package / "SKILL.md",
        package / "requirements.lock",
        package / "scripts" / "diagram_grill_cli.py",
    )
    if any(not item.is_file() or item.is_symlink() for item in required):
        raise RunnerError("skill package is missing SKILL.md, requirements.lock, or its CLI")
    header = (package / "SKILL.md").read_text(encoding="utf-8")[:4000]
    if re.search(r"(?m)^name:\s*diagram-grill\s*$", header) is None:
        raise RunnerError("SKILL.md must declare name: diagram-grill")
    for item in manifest["files"]:
        relative = str(item["path"])
        parts = tuple(part.lower() for part in Path(relative).parts)
        filename = parts[-1]
        if any(part in _FORBIDDEN_PACKAGE_PARTS for part in parts):
            raise RunnerError("skill package contains forbidden path: {}".format(relative))
        if filename in _FORBIDDEN_PACKAGE_FILES or "oracle" in filename:
            raise RunnerError("skill package contains evaluation-only file: {}".format(relative))
    if int(manifest["file_count"]) > _MAX_PACKAGE_FILES:
        raise RunnerError("skill package contains too many files")
    if int(manifest["total_bytes"]) > _MAX_PACKAGE_BYTES:
        raise RunnerError("skill package exceeds the 10 MiB evaluation limit")
    return manifest


def build_prelaunch_plan(eval_root: Path = EVAL_ROOT) -> List[Dict[str, Any]]:
    """Build the frozen 24-run order, alternating which condition runs first."""

    plan: List[Dict[str, Any]] = []
    ordinal = 1
    for index, case in enumerate(harness.load_case_index(Path(eval_root))):
        order = CONDITIONS if index % 2 == 0 else tuple(reversed(CONDITIONS))
        for condition in order:
            plan.append(
                {
                    "ordinal": ordinal,
                    "phase": "prelaunch-24",
                    "case_id": str(case["id"]),
                    "condition": condition,
                    "repeat": 1,
                }
            )
            ordinal += 1
    return plan


def _case(case_id: str, eval_root: Path) -> Mapping[str, Any]:
    by_id = {str(case["id"]): case for case in harness.load_case_index(eval_root)}
    if case_id not in by_id:
        raise RunnerError("unknown case ID: {}".format(case_id))
    return by_id[case_id]


def _protocol_hashes(
    case_id: str, condition: str, eval_root: Path = EVAL_ROOT
) -> Dict[str, str]:
    return harness.expected_protocol_hashes(case_id, condition, Path(eval_root))


def _verify_protocol_hashes(run_dir: Path, state: Mapping[str, Any]) -> None:
    violations = _integrity_violations(Path(run_dir), state)
    if violations:
        raise RunnerError(
            "frozen protocol input changed after preparation: {}".format(
                violations[0]
            )
        )


def _operator_case(case_id: str, eval_root: Path) -> Tuple[Dict[str, str], str]:
    data = _load_json(Path(eval_root) / "operator-answers.json")
    approval = str(data.get("architecture_brief_approval_phrase", ""))
    if approval != APPROVAL_PHRASE:
        raise RunnerError("operator approval phrase differs from the frozen protocol")
    for case in data.get("cases", []):
        if isinstance(case, Mapping) and case.get("id") == case_id:
            answers = {
                str(item["decision_id"]): str(item["answer"])
                for item in case.get("answers", [])
                if isinstance(item, Mapping)
            }
            return answers, approval
    raise RunnerError("operator answers are missing case {}".format(case_id))


def _copy_verified_tree(
    source: Path,
    destination: Path,
    expected: Mapping[str, Any],
    *,
    ignore_python_cache: bool = False,
) -> None:
    ignore = None
    if ignore_python_cache:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
    shutil.copytree(source, destination, symlinks=False, ignore=ignore)
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else destination.lstat().st_uid
    for path in sorted(
        (destination, *destination.rglob("*")),
        key=lambda item: len(item.parts),
    ):
        result = path.lstat()
        if stat.S_ISLNK(result.st_mode) or result.st_uid != expected_uid:
            raise RunnerError("copied input is not exclusively owned by the evaluator")
        if stat.S_ISDIR(result.st_mode):
            # The private 0700 input parent prevents host-user traversal. The
            # bind subtree itself must remain traversable by the fixed nonroot
            # container UID, including under user-namespace remapping.
            path.chmod(0o755)
        elif stat.S_ISREG(result.st_mode):
            path.chmod(0o644)
        else:
            raise RunnerError("copied input contains a link or special file")
    copied = directory_manifest(destination, ignore_python_cache=ignore_python_cache)
    if copied != dict(expected):
        raise RunnerError("input changed while its immutable snapshot was being prepared")


def _command_output(command: Sequence[str], cwd: Optional[Path] = None) -> str:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return "unavailable (exit {})".format(completed.returncode)
    return completed.stdout.strip() or "unavailable"


def collect_execution_metadata(model: str, model_version: str) -> Dict[str, Any]:
    git_commit = _command_output(("git", "rev-parse", "HEAD"), REPOSITORY_ROOT)
    git_status = _command_output(
        ("git", "status", "--porcelain", "--untracked-files=all"), REPOSITORY_ROOT
    )
    return {
        "client": "Codex CLI",
        "model_requested": model,
        "model_version_declared": model_version,
        "operating_system": platform.platform(),
        "runner_environment": {"python_version": platform.python_version()},
        "skill_commit": git_commit,
        "repository_clean": git_status == "unavailable" or not git_status,
        "tested_at": _now(),
    }


def _assert_safe_id(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise RunnerError("{} must match {}".format(label, _SAFE_ID.pattern))


def _run_identifier(case_id: str, condition: str) -> str:
    return "{}--{}--r1".format(case_id, condition)


def _validate_runtime_record(
    value: Mapping[str, Any], agent_image: str, proxy_image: str
) -> Dict[str, Any]:
    runtime = dict(value)
    agent = runtime.get("agent_image")
    proxy = runtime.get("proxy_image")
    if not isinstance(agent, Mapping) or agent.get("id") != agent_image:
        raise RunnerError("runtime validation did not preserve the agent image ID")
    if not isinstance(proxy, Mapping) or proxy.get("id") != proxy_image:
        raise RunnerError("runtime validation did not preserve the proxy image ID")
    if runtime.get("codex_version") != "codex-cli {}".format(CODEX_CLI_VERSION):
        raise RunnerError("runtime validation did not preserve the pinned Codex version")
    if not isinstance(runtime.get("preflight"), Mapping):
        raise RunnerError("runtime validation omitted its active probes")
    if not isinstance(runtime.get("pip_freeze"), str):
        raise RunnerError("runtime validation omitted the dependency inventory")
    return runtime


def prepare_run(
    results_root: Path,
    experiment_id: str,
    case_id: str,
    condition: str,
    model: str,
    model_version: str,
    skill_package: Path,
    agent_image: str,
    proxy_image: str,
    session_state_root: Path,
    docker_executable: Path,
    docker_socket: Path,
    *,
    dedicated_project_key_confirmed: bool,
    eval_root: Path = EVAL_ROOT,
    execution_metadata: Optional[Mapping[str, Any]] = None,
    runtime_validation: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Prepare immutable inputs and empty credential-free snapshots without an API call."""

    root = Path(eval_root).resolve(strict=True)
    fixture_issues = harness.validate_fixtures(root)
    if fixture_issues:
        raise RunnerError("fixed fixtures are invalid: {}".format(fixture_issues[0]))
    _assert_safe_id(experiment_id, "experiment ID")
    if condition not in CONDITIONS:
        raise RunnerError("condition must be baseline or diagram-grill")
    if not model.strip() or not model_version.strip():
        raise RunnerError("model and model version are required")
    if not dedicated_project_key_confirmed:
        raise RunnerError(
            "preparation requires confirmation of a dedicated API project key with a budget and rate limit"
        )
    if _IMAGE_ID.fullmatch(agent_image) is None or _IMAGE_ID.fullmatch(proxy_image) is None:
        raise RunnerError("agent and proxy images must be immutable sha256 image IDs")

    package = Path(skill_package).resolve(strict=True)
    package_manifest = validate_skill_package(package)
    results = Path(results_root).resolve()
    state_root = Path(session_state_root).resolve(strict=True)
    runtime_data = _validate_runtime_record(
        runtime_validation
        or isolation.validate_runtime(
            agent_image_id=agent_image,
            proxy_image_id=proxy_image,
            skill_package=package,
            results_root=results,
            state_root=state_root,
            docker_executable=Path(docker_executable),
            docker_socket=Path(docker_socket),
        ),
        agent_image,
        proxy_image,
    )

    case = _case(case_id, root)
    prompt_source = root / str(case["prompt_path"])
    base_prompt_bytes = prompt_source.read_bytes()
    try:
        base_prompt = base_prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError("the fixed base prompt must be UTF-8") from exc
    delivered_prompt = (
        SKILL_INVOCATION_PREFIX + base_prompt
        if condition == "diagram-grill"
        else base_prompt
    )
    delivered_prompt_bytes = delivered_prompt.encode("utf-8")
    source_repo = root / str(case["repo_path"])
    evidence_manifest = directory_manifest(source_repo)
    protocol_hashes = _protocol_hashes(case_id, condition, root)
    observed_protocol_hashes = {
        "base_prompt_sha256": hashlib.sha256(base_prompt_bytes).hexdigest(),
        "delivered_prompt_sha256": hashlib.sha256(delivered_prompt_bytes).hexdigest(),
        "repo_manifest_sha256": evidence_manifest["manifest_sha256"],
        "skill_manifest_sha256": package_manifest["manifest_sha256"],
    }
    changed_protocol_hashes = sorted(
        name
        for name, digest in observed_protocol_hashes.items()
        if protocol_hashes.get(name) != digest
    )
    if changed_protocol_hashes:
        raise RunnerError(
            "prepared input differs from the checked-in protocol: {}".format(
                ", ".join(changed_protocol_hashes)
            )
        )

    run_id = _run_identifier(case_id, condition)
    run_dir = results / experiment_id / run_id
    state_dir = state_root / experiment_id / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise RunnerError("run directory already exists; runs are never overwritten: {}".format(run_dir))
    if state_dir.exists() or state_dir.is_symlink():
        raise RunnerError("session state directory already exists; runs are never reused: {}".format(state_dir))

    input_dir = run_dir / "input"
    raw_dir = run_dir / "raw"
    results_experiment = run_dir.parent
    state_experiment = state_dir.parent
    results_experiment.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_experiment.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.mkdir(mode=0o700)
    input_dir.mkdir(mode=0o700)
    raw_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    for directory in (
        results_experiment,
        state_experiment,
        run_dir,
        input_dir,
        raw_dir,
        state_dir,
    ):
        result = directory.lstat()
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
        if result.st_uid != expected_uid or stat.S_IMODE(result.st_mode) & 0o077:
            raise RunnerError("evaluation directories must be current-user-owned mode 0700")

    (input_dir / "prompt.md").write_text(base_prompt, encoding="utf-8")
    (input_dir / "delivered-prompt.md").write_text(delivered_prompt, encoding="utf-8")

    _copy_verified_tree(source_repo, input_dir / "repo", evidence_manifest)
    _write_json(input_dir / "repo-manifest.json", evidence_manifest)
    if condition == "diagram-grill":
        _copy_verified_tree(
            package,
            input_dir / "skill-package",
            package_manifest,
            ignore_python_cache=True,
        )
        _write_json(input_dir / "skill-package-manifest.json", package_manifest)

    snapshot_paths = isolation.prepare_snapshots(run_dir, state_dir)
    if not isinstance(snapshot_paths, Mapping):
        raise RunnerError("isolation backend did not prepare bounded snapshots")

    preflight = runtime_data["preflight"]
    freeze_text = runtime_data["pip_freeze"]
    _write_json(input_dir / "runtime-preflight.json", preflight)
    (input_dir / "pip-freeze.txt").write_text(freeze_text, encoding="utf-8")
    runtime_manifest = {
        "backend": "docker-agent-proxy-v1",
        "agent_image_id": agent_image,
        "proxy_image_id": proxy_image,
        "codex_version": runtime_data["codex_version"],
        "preflight_sha256": _sha256(input_dir / "runtime-preflight.json"),
        "pip_freeze_sha256": _sha256(input_dir / "pip-freeze.txt"),
        "requirements_lock_sha256": _sha256(package / "requirements.lock"),
    }
    _write_json(input_dir / "runtime-manifest.json", runtime_manifest)

    metadata = dict(execution_metadata or collect_execution_metadata(model, model_version))
    metadata.update(
        {
            "client": "Codex CLI (credential-free Docker agent)",
            "client_version": runtime_data["codex_version"],
            "model_requested": model,
            "model_version_declared": model_version,
            "runtime": runtime_manifest,
            "isolation": {
                "backend": "docker-agent-proxy-v1",
                "runtime_validation": runtime_data,
                "workspace_snapshot_path": str(snapshot_paths["workspace"]),
                "state_snapshot_path": str(snapshot_paths["state"]),
                "snapshot_journal_path": str(snapshot_paths["journal"]),
                "dedicated_project_key_attested": True,
                "credential_storage": "proxy-process-memory-from-stdin-only",
                "agent_has_credentials": False,
                "host_read_write_bind_mounts": False,
                "docker_socket_mounted": False,
            },
            "treatment_settings": {
                "model_reasoning_effort": "medium",
                "service_tier": "default",
                "approval_policy": "never",
                "sandbox_mode": "workspace-write",
                "network": "tool-network-disabled; parent-agent-proxy-only",
                "ignore_user_config": True,
                "ignore_rules": True,
                "web_search": "disabled",
            },
        }
    )
    plan_entry = next(
        item
        for item in build_prelaunch_plan(root)
        if item["case_id"] == case_id and item["condition"] == condition
    )
    state = {
        "schema_version": "1.0",
        "phase": "prelaunch-24",
        "case_id": case_id,
        "condition": condition,
        "repeat": 1,
        "run_id": run_id,
        "planned_ordinal": plan_entry["ordinal"],
        "status": "prepared",
        "model": model,
        "model_version": model_version,
        "session_id": None,
        "turn_count": 0,
        "revealed_decision_ids": [],
        "approval_sent": False,
        "source_integrity_violations": [],
        "session_integrity_violations": [],
        "prepared_at": _now(),
        "updated_at": _now(),
        "execution": metadata,
        "input": {
            "base_prompt_path": "input/prompt.md",
            "base_prompt_sha256": _sha256(input_dir / "prompt.md"),
            "delivered_prompt_path": "input/delivered-prompt.md",
            "delivered_prompt_sha256": _sha256(input_dir / "delivered-prompt.md"),
            "skill_invocation_prefix": SKILL_INVOCATION_PREFIX if condition == "diagram-grill" else "",
            "repo_manifest_sha256": evidence_manifest["manifest_sha256"],
            "skill_manifest_sha256": package_manifest["manifest_sha256"],
            "skill_exposed": condition == "diagram-grill",
            "runtime_manifest_path": "input/runtime-manifest.json",
            "runtime_manifest_sha256": _sha256(input_dir / "runtime-manifest.json"),
            "protocol_hashes": protocol_hashes,
        },
    }
    _write_json(run_dir / "state.json", state)
    return run_dir


def _state(run_dir: Path) -> Dict[str, Any]:
    state_path = Path(run_dir).resolve() / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise RunnerError("run directory has no valid state.json")
    value = _load_json(state_path)
    if not isinstance(value, Mapping) or value.get("schema_version") != "1.0":
        raise RunnerError("run state is malformed")
    return dict(value)


def _write_state(run_dir: Path, state: Mapping[str, Any]) -> None:
    updated = dict(state)
    updated["updated_at"] = _now()
    _write_json(Path(run_dir) / "state.json", updated)


def _integrity_violations(run_dir: Path, state: Mapping[str, Any]) -> List[str]:
    violations: List[str] = []
    input_dir = Path(run_dir) / "input"
    input_state = state.get("input")
    if not isinstance(input_state, Mapping):
        return ["run state has no frozen input metadata"]
    pinned = input_state.get("protocol_hashes")
    if not isinstance(pinned, Mapping):
        return ["run state has no frozen protocol input hashes"]

    try:
        current_protocol = _protocol_hashes(
            str(state["case_id"]), str(state["condition"]), EVAL_ROOT
        )
    except (OSError, harness.EvaluationError, KeyError, TypeError, ValueError):
        current_protocol = {}
        violations.append("checked-in protocol inputs are unavailable")
    changed_protocol = sorted(
        name
        for name in set(pinned) | set(current_protocol)
        if pinned.get(name) != current_protocol.get(name)
    )
    if changed_protocol:
        violations.append(
            "checked-in protocol hashes changed: {}".format(
                ", ".join(changed_protocol)
            )
        )

    for state_name, protocol_name in (
        ("base_prompt_sha256", "base_prompt_sha256"),
        ("delivered_prompt_sha256", "delivered_prompt_sha256"),
        ("repo_manifest_sha256", "repo_manifest_sha256"),
        ("skill_manifest_sha256", "skill_manifest_sha256"),
    ):
        if input_state.get(state_name) != pinned.get(protocol_name):
            violations.append("frozen {} differs from protocol".format(state_name))

    fixed_files = (
        ("prompt.md", "base_prompt_sha256"),
        ("delivered-prompt.md", "delivered_prompt_sha256"),
    )
    for filename, hash_name in fixed_files:
        path = input_dir / filename
        if (
            not path.is_file()
            or path.is_symlink()
            or _sha256(path) != pinned.get(hash_name)
        ):
            violations.append("{} changed after preparation".format(filename))

    try:
        current_repo = directory_manifest(input_dir / "repo")
    except (OSError, RunnerError):
        violations.append("evidence repository snapshot is missing, linked, or unreadable")
    else:
        if current_repo["manifest_sha256"] != pinned.get("repo_manifest_sha256"):
            violations.append("evidence repository snapshot changed after preparation")
    repo_manifest_path = input_dir / "repo-manifest.json"
    try:
        repo_manifest = _load_json(repo_manifest_path)
    except (OSError, json.JSONDecodeError):
        repo_manifest = None
    if (
        not isinstance(repo_manifest, Mapping)
        or repo_manifest_path.is_symlink()
        or repo_manifest.get("manifest_sha256") != pinned.get("repo_manifest_sha256")
    ):
        violations.append("evidence repository manifest changed after preparation")

    if state["condition"] == "diagram-grill":
        try:
            current_skill = directory_manifest(
                input_dir / "skill-package", ignore_python_cache=True
            )
        except (OSError, RunnerError):
            violations.append("skill package snapshot is missing, linked, or unreadable")
        else:
            if current_skill["manifest_sha256"] != pinned.get("skill_manifest_sha256"):
                violations.append("skill package snapshot changed after preparation")
        skill_manifest_path = input_dir / "skill-package-manifest.json"
        try:
            skill_manifest = _load_json(skill_manifest_path)
        except (OSError, json.JSONDecodeError):
            skill_manifest = None
        if (
            not isinstance(skill_manifest, Mapping)
            or skill_manifest_path.is_symlink()
            or skill_manifest.get("manifest_sha256")
            != pinned.get("skill_manifest_sha256")
        ):
            violations.append("skill package manifest changed after preparation")
    elif (input_dir / "skill-package").exists() or (
        input_dir / "skill-package-manifest.json"
    ).exists():
        violations.append("baseline input unexpectedly contains the treatment skill")

    runtime_manifest = input_dir / "runtime-manifest.json"
    if (
        input_state.get("runtime_manifest_path") != "input/runtime-manifest.json"
        or not runtime_manifest.is_file()
        or runtime_manifest.is_symlink()
        or _sha256(runtime_manifest) != input_state.get("runtime_manifest_sha256")
    ):
        violations.append("runtime manifest changed after preparation")
    return violations


Invoke = Callable[
    [Path, Mapping[str, Any], str, int, bool, int, bytearray], Dict[str, Any]
]


def _invoke_codex(
    run_dir: Path,
    state: Mapping[str, Any],
    prompt: str,
    turn: int,
    initial: bool,
    timeout_seconds: int,
    credential: bytearray,
) -> Dict[str, Any]:
    try:
        return isolation.run_turn(
            run_dir=Path(run_dir),
            state=state,
            prompt=prompt,
            turn=turn,
            initial=initial,
            timeout_seconds=timeout_seconds,
            credential=credential,
        )
    except isolation.IsolationError as exc:
        raise RunnerError(str(exc)) from exc


def _record_operator_message(
    run_dir: Path, turn: int, kind: str, text: str, **extra: Any
) -> None:
    event: Dict[str, Any] = {"turn": turn, "kind": kind, "text": text, "sent_at": _now()}
    event.update(extra)
    _append_jsonl(Path(run_dir) / "raw" / "operator-turns.jsonl", event)


def _attempt_files(run_dir: Path) -> List[Path]:
    raw = Path(run_dir) / "raw"
    if not raw.is_dir() or raw.is_symlink():
        raise RunnerError("run has no private raw evidence directory")
    attempts = []
    for path in raw.iterdir():
        if _ATTEMPT_FILE.fullmatch(path.name) is None:
            continue
        if not path.is_file() or path.is_symlink():
            raise RunnerError("billable attempt marker is not a regular file")
        result = path.lstat()
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
        if (
            result.st_nlink != 1
            or result.st_uid != expected_uid
            or stat.S_IMODE(result.st_mode) != 0o600
        ):
            raise RunnerError("billable attempt marker identity drifted")
        attempts.append(path)
    return sorted(attempts, key=lambda item: item.name)


def _refuse_incomplete_attempt(run_dir: Path) -> None:
    incomplete = [
        path for path in _attempt_files(run_dir) if ".in-progress.json" in path.name
    ]
    if incomplete:
        raise RunnerError(
            "this run has an incomplete billable attempt and can never be resumed; prepare a new run"
        )


def _begin_billable_attempt(
    run_dir: Path,
    state: Mapping[str, Any],
    prompt: str,
    turn: int,
    initial: bool,
) -> Path:
    _refuse_incomplete_attempt(run_dir)
    raw = Path(run_dir) / "raw"
    name = "attempt-turn-{:03d}.in-progress.json".format(turn)
    completed = raw / name.replace(".in-progress.json", ".completed.json")
    if completed.exists() or completed.is_symlink():
        raise RunnerError("this turn already has a durable billable attempt marker")
    marker = raw / name
    value = {
        "schema_version": "1",
        "turn": turn,
        "initial": initial,
        "run_id": state.get("run_id"),
        "session_id": state.get("session_id"),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "created_at": _now(),
    }
    encoded = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as error:
        raise RunnerError(
            "this turn already has a durable billable attempt marker"
        ) from error
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(raw)
    return marker


def _complete_billable_attempt(marker: Path) -> Path:
    source = Path(marker)
    match = _ATTEMPT_FILE.fullmatch(source.name)
    if match is None or match.group("state") != "in-progress":
        raise RunnerError("billable attempt marker path is invalid")
    result = source.lstat()
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
    if (
        not stat.S_ISREG(result.st_mode)
        or result.st_nlink != 1
        or result.st_uid != expected_uid
        or stat.S_IMODE(result.st_mode) != 0o600
    ):
        raise RunnerError("billable attempt marker identity drifted")
    completed = source.with_name(
        source.name.replace(".in-progress.json", ".completed.json")
    )
    try:
        os.link(source, completed, follow_symlinks=False)
    except FileExistsError as error:
        raise RunnerError("completed billable attempt marker already exists") from error
    _fsync_directory(source.parent)
    source.unlink()
    _fsync_directory(source.parent)
    return completed


def _apply_turn_result(
    run_dir: Path,
    state: Dict[str, Any],
    result: Mapping[str, Any],
    initial: bool,
    attempt_marker: Path,
) -> Dict[str, Any]:
    _append_jsonl(Path(run_dir) / "raw" / "turn-index.jsonl", dict(result))
    state["turn_count"] = int(result["turn"])
    observed_session = result.get("session_id")
    if initial:
        if not isinstance(observed_session, str) or _THREAD_ID.fullmatch(observed_session) is None:
            state["status"] = "client-failed"
        else:
            state["session_id"] = observed_session
            state["status"] = "active"
    elif observed_session != state.get("session_id"):
        violations = list(state.get("session_integrity_violations", []))
        violations.append(
            "turn {} returned session {!r}; expected {!r}".format(
                result.get("turn"), observed_session, state.get("session_id")
            )
        )
        state["session_integrity_violations"] = violations
        state["status"] = "client-failed"

    boundary = result.get("isolation")
    boundary_ok = isinstance(boundary, Mapping) and boundary.get("succeeded") is True
    if isinstance(boundary, Mapping):
        committed = boundary.get("snapshot_commit")
        isolation_state = state.get("execution", {}).get("isolation")
        if isinstance(committed, Mapping) and isinstance(isolation_state, dict):
            workspace = committed.get("workspace")
            codex_home = committed.get("state")
            if isinstance(workspace, Mapping) and isinstance(workspace.get("path"), str):
                isolation_state["workspace_snapshot_path"] = workspace["path"]
            if isinstance(codex_home, Mapping) and isinstance(codex_home.get("path"), str):
                isolation_state["state_snapshot_path"] = codex_home["path"]
    if (
        result.get("timed_out")
        or result.get("exit_code") != 0
        or result.get("launch_error")
        or result.get("transport_violation")
        or not boundary_ok
    ):
        state["status"] = "client-failed"
    state["source_integrity_violations"] = sorted(
        set(state.get("source_integrity_violations", []))
        | set(_integrity_violations(run_dir, state))
    )
    _write_state(run_dir, state)
    _complete_billable_attempt(attempt_marker)
    return state


def _require_credential(credential: Optional[bytearray]) -> bytearray:
    if not isinstance(credential, bytearray):
        raise RunnerError("a dedicated project API key must be supplied through stdin")
    if not MIN_CREDENTIAL_BYTES <= len(credential) <= MAX_CREDENTIAL_BYTES:
        raise RunnerError("dedicated project API key has an invalid length")
    if any(byte < 33 or byte > 126 for byte in credential):
        raise RunnerError("dedicated project API key must contain printable ASCII without spaces")
    return credential


def launch_run(
    run_dir: Path,
    timeout_seconds: int = 900,
    allow_dirty: bool = False,
    *,
    dedicated_project_key_confirmed: bool,
    credential: Optional[bytearray],
    invoke: Invoke = _invoke_codex,
) -> Dict[str, Any]:
    """Make the first billable model call for a prepared run."""

    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state["status"] != "prepared":
        raise RunnerError("only a prepared run can be launched")
    if not dedicated_project_key_confirmed:
        raise RunnerError("launch requires confirmation of the dedicated API project key")
    if not bool(state["execution"].get("repository_clean")) and not allow_dirty:
        raise RunnerError("repository is dirty; commit inputs or pass --allow-dirty for a pilot")
    selected_credential = _require_credential(credential)
    _verify_protocol_hashes(directory, state)
    prompt = (directory / "input" / "delivered-prompt.md").read_text(encoding="utf-8")
    attempt_marker = _begin_billable_attempt(directory, state, prompt, 1, True)
    _record_operator_message(directory, 1, "initial-prompt", prompt)
    result = invoke(directory, state, prompt, 1, True, timeout_seconds, selected_credential)
    return _apply_turn_result(directory, state, result, True, attempt_marker)


def reply_run(
    run_dir: Path,
    decision_ids: Sequence[str] = (),
    unknown: bool = False,
    approve: bool = False,
    brief_reviewed: bool = False,
    timeout_seconds: int = 900,
    *,
    dedicated_project_key_confirmed: bool,
    credential: Optional[bytearray],
    invoke: Invoke = _invoke_codex,
) -> Dict[str, Any]:
    """Resume with exact scripted answers selected by the human operator."""

    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state["status"] != "active":
        raise RunnerError("only an active run can receive an operator reply")
    if int(state.get("turn_count", 0)) >= MAX_TURNS_PER_RUN:
        raise RunnerError("the run reached its fixed turn limit")
    if not dedicated_project_key_confirmed:
        raise RunnerError("reply requires confirmation of the dedicated API project key")
    selected_credential = _require_credential(credential)
    _verify_protocol_hashes(directory, state)
    if approve and (decision_ids or unknown):
        raise RunnerError("approval cannot be combined with another operator reply")
    if not approve and not decision_ids and not unknown:
        raise RunnerError("choose decision IDs, unknown, or approve")
    answers, approval_phrase = _operator_case(str(state["case_id"]), EVAL_ROOT)
    extra: Dict[str, Any] = {}
    if decision_ids:
        if len(set(decision_ids)) != len(decision_ids):
            raise RunnerError("do not repeat a decision ID within one reply")
        missing = [decision_id for decision_id in decision_ids if decision_id not in answers]
        if missing:
            raise RunnerError("decision is not registered for this case: {}".format(missing[0]))
        response_parts = [answers[decision_id] for decision_id in decision_ids]
        if unknown:
            response_parts.append(UNKNOWN_PHRASE)
        prompt = "\n".join(response_parts)
        revealed = list(state.get("revealed_decision_ids", []))
        for decision_id in decision_ids:
            if decision_id not in revealed:
                revealed.append(decision_id)
        state["revealed_decision_ids"] = revealed
        kind = "fixed-mixed-answer" if unknown else "fixed-decision-answer"
        extra["decision_ids"] = list(decision_ids)
        if unknown:
            extra["includes_unknown_answer"] = True
    elif unknown:
        prompt = UNKNOWN_PHRASE
        kind = "fixed-unknown-answer"
    else:
        if not brief_reviewed:
            raise RunnerError("approval requires --brief-reviewed after checking the current brief")
        unrevealed = sorted(set(answers) - set(state.get("revealed_decision_ids", [])))
        if unrevealed:
            raise RunnerError("approval is withheld because indexed decisions remain unasked; close the run")
        prompt = approval_phrase
        kind = "architecture-brief-approval"
        state["approval_sent"] = True
        extra["brief_reviewed"] = True

    turn = int(state["turn_count"]) + 1
    attempt_marker = _begin_billable_attempt(directory, state, prompt, turn, False)
    _record_operator_message(directory, turn, kind, prompt, **extra)
    result = invoke(
        directory, state, prompt, turn, False, timeout_seconds, selected_credential
    )
    return _apply_turn_result(directory, state, result, False, attempt_marker)


def close_run(run_dir: Path, reason: str) -> Dict[str, Any]:
    """Close without another model call; partial evidence remains intact."""

    allowed = ("agent-finished", "approval-withheld", "client-failed", "operator-terminated")
    if reason not in allowed:
        raise RunnerError("close reason must be one of {}".format(", ".join(allowed)))
    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state["status"] == "closed":
        raise RunnerError("run is already closed")
    state["status"] = "closed"
    state["closed_at"] = _now()
    state["close_reason"] = reason
    state["source_integrity_violations"] = sorted(
        set(state.get("source_integrity_violations", []))
        | set(_integrity_violations(directory, state))
    )
    _write_state(directory, state)
    return state


def _review_execution(state: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        metadata = state["execution"]
        runtime = metadata["runtime"]
        settings = metadata["treatment_settings"]
        runtime_validation = metadata["isolation"]["runtime_validation"]
        versions = runtime_validation["preflight"]["sandbox"]["versions"]
    except (KeyError, TypeError) as exc:
        raise RunnerError("run state lacks reviewed execution metadata") from exc
    if not all(
        isinstance(value, Mapping)
        for value in (metadata, runtime, settings, runtime_validation, versions)
    ):
        raise RunnerError("run state has malformed reviewed execution metadata")
    if settings.get("network") != "tool-network-disabled; parent-agent-proxy-only":
        raise RunnerError("run state does not prove the disabled tool-network contract")
    if settings.get("web_search") != "disabled":
        raise RunnerError("run state does not prove disabled web search")
    version_source = metadata.get("model_version_source", "requested-identifier")
    if version_source not in ("requested-identifier", "provider-reported"):
        raise RunnerError("run state has an invalid model version source")
    return {
        "client": metadata.get("client"),
        "client_version": metadata.get("client_version"),
        "model": state.get("model"),
        "model_version": state.get("model_version"),
        "model_version_source": version_source,
        "operating_system": metadata.get("operating_system"),
        "python_version": versions.get("python"),
        "graphviz_version": versions.get("dot"),
        "diagrams_version": versions.get("diagrams"),
        "skill_commit": metadata.get("skill_commit"),
        "agent_image_id": runtime.get("agent_image_id"),
        "proxy_image_id": runtime.get("proxy_image_id"),
        "reasoning_effort": settings.get("model_reasoning_effort"),
        "service_tier": settings.get("service_tier"),
        "approval_policy": settings.get("approval_policy"),
        "sandbox_mode": settings.get("sandbox_mode"),
        "tool_network": "disabled",
        "web_search": "disabled",
        "tested_at": metadata.get("tested_at"),
    }


def _turn_records(run_dir: Path, state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    try:
        turn_count = int(state["turn_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunnerError("run state has an invalid turn count") from exc
    if turn_count < 0 or turn_count > MAX_TURNS_PER_RUN:
        raise RunnerError("run state has an invalid turn count")
    path = Path(run_dir) / "raw" / "turn-index.jsonl"
    attempt_files = _attempt_files(run_dir)
    if any(".in-progress.json" in item.name for item in attempt_files):
        raise RunnerError("run contains an incomplete billable attempt")
    completed_attempts = [
        item for item in attempt_files if ".completed.json" in item.name
    ]
    if turn_count == 0:
        if path.exists() or path.is_symlink() or completed_attempts:
            raise RunnerError("zero-turn run has unexpected turn evidence")
        return []
    if not path.is_file() or path.is_symlink():
        raise RunnerError("closed run has no regular turn index")
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as input_file:
            for line_number, line in enumerate(input_file, start=1):
                if not line.strip():
                    raise RunnerError(
                        "turn index has a blank record at line {}".format(line_number)
                    )
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise RunnerError(
                        "turn index record {} is not an object".format(line_number)
                    )
                records.append(dict(value))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("turn index is unreadable or malformed") from exc
    observed = [record.get("turn") for record in records]
    expected = list(range(1, turn_count + 1))
    if observed != expected:
        raise RunnerError("turn index is missing, duplicated, or out of order")
    expected_attempt_names = [
        "attempt-turn-{:03d}.completed.json".format(turn) for turn in expected
    ]
    if [item.name for item in completed_attempts] != expected_attempt_names:
        raise RunnerError("billable attempt markers do not match the turn index")
    for index, record in enumerate(records):
        if record.get("initial") is not (index == 0):
            raise RunnerError("turn index has an invalid initial-turn marker")
        marker = _load_json(completed_attempts[index])
        if (
            not isinstance(marker, Mapping)
            or set(marker)
            != {
                "schema_version",
                "turn",
                "initial",
                "run_id",
                "session_id",
                "prompt_sha256",
                "created_at",
            }
            or marker.get("schema_version") != "1"
            or marker.get("turn") != index + 1
            or marker.get("initial") is not (index == 0)
            or marker.get("run_id") != state.get("run_id")
            or re.fullmatch(r"[0-9a-f]{64}", str(marker.get("prompt_sha256"))) is None
        ):
            raise RunnerError("billable attempt marker is malformed or mismatched")
    return records


def _review_integrity(
    run_dir: Path, state: Mapping[str, Any], turns: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    source = set(str(item) for item in state.get("source_integrity_violations", []))
    source.update(_integrity_violations(run_dir, state))
    execution = state.get("execution")
    if not isinstance(execution, Mapping) or not execution.get("repository_clean"):
        source.add("repository was dirty at preparation")
    session = set(str(item) for item in state.get("session_integrity_violations", []))
    isolation_succeeded = bool(turns) and all(
        isinstance(turn.get("isolation"), Mapping)
        and turn["isolation"].get("succeeded") is True
        for turn in turns
    )
    teardown_succeeded = bool(turns) and all(
        isinstance(turn.get("isolation"), Mapping)
        and isinstance(turn["isolation"].get("teardown"), Mapping)
        and turn["isolation"]["teardown"].get("succeeded") is True
        for turn in turns
    )
    return {
        "source_violations": sorted(source),
        "session_violations": sorted(session),
        "isolation_succeeded": isolation_succeeded,
        "teardown_succeeded": teardown_succeeded,
    }


def _artifact_reference(run_dir: Path, relative_path: Any) -> Dict[str, Any]:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise RunnerError("review artifact paths must be nonempty relative strings")
    posix = PurePosixPath(relative_path)
    windows = PureWindowsPath(relative_path)
    parts = relative_path.replace("\\", "/").split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in parts
    ):
        raise RunnerError("review artifact paths must be relative without traversal")
    root = Path(run_dir).resolve()
    candidate = root / relative_path
    current = root
    for part in posix.parts:
        current = current / part
        if current.is_symlink():
            raise RunnerError("review artifacts may not be symlinked")
    try:
        resolved = candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise RunnerError("review artifact path escapes the run directory") from exc
    path = root / resolved
    if not path.is_file():
        raise RunnerError("review artifact is missing: {}".format(relative_path))
    return {
        "path": relative_path,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def finalize_review(
    run_dir: Path, review_path: Path, eval_root: Path = EVAL_ROOT
) -> Path:
    """Create one immutable schema-1.1 record from a closed, human-reviewed run."""

    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state.get("status") != "closed":
        raise RunnerError("only a closed run can be finalized for review")
    review_file = Path(review_path)
    if not review_file.is_file() or review_file.is_symlink():
        raise RunnerError("review input must be a regular JSON file")
    review = _load_json(review_file)
    if not isinstance(review, Mapping) or set(review) != {
        "schema_version",
        "adjudication",
        "artifacts",
    }:
        raise RunnerError(
            "review input must contain only schema_version, adjudication, and artifacts"
        )
    if review.get("schema_version") != "1.0":
        raise RunnerError("review input must use schema_version 1.0")
    adjudication = review.get("adjudication")
    artifacts = review.get("artifacts")
    if not isinstance(adjudication, Mapping):
        raise RunnerError("review adjudication must be an object supplied by the reviewer")
    if not isinstance(artifacts, Mapping) or "transcript" not in artifacts:
        raise RunnerError("review artifacts must map an explicit transcript")
    unexpected_artifacts = sorted(set(artifacts) - _REVIEW_ARTIFACT_NAMES)
    if unexpected_artifacts:
        raise RunnerError(
            "review artifacts contain unknown fields: {}".format(
                ", ".join(unexpected_artifacts)
            )
        )

    turns = _turn_records(directory, state)
    execution = _review_execution(state)
    reviewed_run = {
        "schema_version": "1.1",
        "phase": state.get("phase"),
        "case_id": state.get("case_id"),
        "condition": state.get("condition"),
        "repeat": state.get("repeat"),
        "execution": execution,
        "execution_fingerprint": harness.compute_execution_fingerprint(execution),
        "protocol_hashes": dict(state.get("input", {}).get("protocol_hashes", {})),
        "integrity": _review_integrity(directory, state, turns),
        "artifacts": {
            str(name): _artifact_reference(directory, path)
            for name, path in artifacts.items()
        },
        "adjudication": dict(adjudication),
    }

    output = directory / REVIEWED_RUN_FILENAME
    if output.exists() or output.is_symlink():
        raise RunnerError("reviewed run already exists and will not be overwritten")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".reviewed-run-", suffix=".json.tmp", dir=str(directory)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
            json.dump(
                reviewed_run,
                output_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        try:
            harness.load_reviewed_run(temporary, Path(eval_root))
        except (harness.EvaluationError, OSError, json.JSONDecodeError) as exc:
            raise RunnerError("reviewed run failed validation: {}".format(exc)) from exc
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise RunnerError(
                "reviewed run already exists and will not be overwritten"
            ) from exc
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return output


def _read_credential_stdin(stream: Any) -> bytearray:
    source = getattr(stream, "buffer", stream)
    data = source.read(MAX_CREDENTIAL_BYTES + 2)
    if isinstance(data, str):
        try:
            data = data.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RunnerError("credential stdin must be printable ASCII") from exc
    if not isinstance(data, bytes) or len(data) > MAX_CREDENTIAL_BYTES + 1:
        raise RunnerError("credential stdin exceeds its limit")
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    credential = bytearray(data)
    _require_credential(credential)
    return credential


def _wipe(value: Optional[bytearray]) -> None:
    if value is not None:
        for index in range(len(value)):
            value[index] = 0


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="print the frozen alternating 24-run plan")

    prepare = commands.add_parser("prepare", help="create one oracle-free run")
    prepare.add_argument("--results-root", type=Path, required=True)
    prepare.add_argument("--experiment", required=True)
    prepare.add_argument("--case", required=True)
    prepare.add_argument("--condition", choices=CONDITIONS, required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--model-version", required=True)
    prepare.add_argument("--skill-package", type=Path, required=True)
    prepare.add_argument("--agent-image", required=True)
    prepare.add_argument("--proxy-image", required=True)
    prepare.add_argument("--session-state-root", type=Path, required=True)
    prepare.add_argument("--docker-executable", type=Path, required=True)
    prepare.add_argument("--docker-socket", type=Path, required=True)
    prepare.add_argument("--confirm-dedicated-project-key", action="store_true")

    launch = commands.add_parser("launch", help="execute the initial prompt")
    launch.add_argument("run_dir", type=Path)
    launch.add_argument("--execute", action="store_true")
    launch.add_argument("--credential-stdin", action="store_true")
    launch.add_argument("--confirm-dedicated-project-key", action="store_true")
    launch.add_argument("--allow-dirty", action="store_true")
    launch.add_argument("--timeout", type=int, default=900)

    reply = commands.add_parser("reply", help="resume with one scripted reply")
    reply.add_argument("run_dir", type=Path)
    reply.add_argument("--execute", action="store_true")
    reply.add_argument("--credential-stdin", action="store_true")
    reply.add_argument("--confirm-dedicated-project-key", action="store_true")
    reply.add_argument("--decision-id", action="append", default=[])
    reply.add_argument("--unknown", action="store_true")
    reply.add_argument("--approve", action="store_true")
    reply.add_argument("--brief-reviewed", action="store_true")
    reply.add_argument("--timeout", type=int, default=900)

    close = commands.add_parser("close", help="close without another model call")
    close.add_argument("run_dir", type=Path)
    close.add_argument(
        "--reason",
        choices=("agent-finished", "approval-withheld", "client-failed", "operator-terminated"),
        required=True,
    )
    finalize = commands.add_parser(
        "finalize-review", help="validate and freeze one human-reviewed run"
    )
    finalize.add_argument("run_dir", type=Path)
    finalize.add_argument("--review", type=Path, required=True)
    status = commands.add_parser("status", help="print retained run state")
    status.add_argument("run_dir", type=Path)

    args = parser.parse_args(list(argv) if argv else None)
    credential: Optional[bytearray] = None
    try:
        if args.command == "plan":
            _print_json({"phase": "prelaunch-24", "runs": build_prelaunch_plan()})
            return 0
        if args.command == "prepare":
            run_dir = prepare_run(
                args.results_root,
                args.experiment,
                args.case,
                args.condition,
                args.model,
                args.model_version,
                args.skill_package,
                args.agent_image,
                args.proxy_image,
                args.session_state_root,
                args.docker_executable,
                args.docker_socket,
                dedicated_project_key_confirmed=args.confirm_dedicated_project_key,
            )
            _print_json({"ok": True, "run_dir": str(run_dir), "state": _state(run_dir)})
            return 0
        if args.command in ("launch", "reply"):
            if not args.execute:
                raise RunnerError("{} requires --execute to confirm a billable model call".format(args.command))
            if not args.credential_stdin:
                raise RunnerError("{} requires --credential-stdin".format(args.command))
            credential = _read_credential_stdin(sys.stdin)
        if args.command == "launch":
            _print_json(
                launch_run(
                    args.run_dir,
                    args.timeout,
                    args.allow_dirty,
                    dedicated_project_key_confirmed=args.confirm_dedicated_project_key,
                    credential=credential,
                )
            )
            return 0
        if args.command == "reply":
            _print_json(
                reply_run(
                    args.run_dir,
                    args.decision_id,
                    args.unknown,
                    args.approve,
                    args.brief_reviewed,
                    args.timeout,
                    dedicated_project_key_confirmed=args.confirm_dedicated_project_key,
                    credential=credential,
                )
            )
            return 0
        if args.command == "close":
            _print_json(close_run(args.run_dir, args.reason))
            return 0
        if args.command == "finalize-review":
            output = finalize_review(args.run_dir, args.review)
            _print_json({"ok": True, "reviewed_run": str(output)})
            return 0
        _print_json(_state(args.run_dir))
        return 0
    except (
        RunnerError,
        isolation.IsolationError,
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print("diagram-grill-codex-eval: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        _wipe(credential)


if __name__ == "__main__":
    raise SystemExit(main())
