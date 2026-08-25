#!/usr/bin/env python3
"""Run the fixed prelaunch evaluation through Codex CLI, one reviewed turn at a time.

This runner deliberately does not classify agent questions or adjudicate outputs. A
human operator selects the matching decision ID, and the runner sends only the exact
pre-registered answer. Every client event and operator message is retained verbatim.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import harness


EVAL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = EVAL_ROOT.parent
APPROVAL_PHRASE = "I approve this Architecture Brief for rendering."
UNKNOWN_PHRASE = "That is not specified. Keep it explicit as unknown and do not assume it."
SKILL_INVOCATION_PREFIX = "$diagram-grill\n\n"
CONDITIONS = ("baseline", "diagram-grill")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_FORBIDDEN_PACKAGE_PARTS = frozenset((".git", ".github", "evals", "tests"))
_FORBIDDEN_PACKAGE_FILES = frozenset(
    ("operator-answers.json", "cases.json", "oss-cases.json", "run.schema.json")
)
_MAX_PACKAGE_FILES = 500
_MAX_PACKAGE_BYTES = 10 * 1024 * 1024


class RunnerError(ValueError):
    """Raised when an evaluation run would violate the frozen protocol."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def _write_json(path: Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(target))


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_manifest(root: Path, ignore_python_cache: bool = False) -> Dict[str, Any]:
    """Return a stable content manifest and reject links and special files."""

    directory = Path(root)
    if not directory.is_dir() or directory.is_symlink():
        raise RunnerError("manifest root must be a real directory: {}".format(directory))
    files: List[Dict[str, Any]] = []
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise RunnerError("symlinks are not allowed: {}".format(relative))
        if ignore_python_cache and (
            "__pycache__" in path.relative_to(directory).parts
            or path.suffix in (".pyc", ".pyo")
            or path.name == ".DS_Store"
        ):
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise RunnerError("special files are not allowed: {}".format(relative))
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    encoded = json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def validate_skill_package(path: Path) -> Dict[str, Any]:
    """Validate a runtime-only skill package before exposing it to the agent."""

    package = Path(path).resolve()
    manifest = directory_manifest(package, ignore_python_cache=True)
    required = (
        package / "SKILL.md",
        package / "requirements.lock",
        package / "scripts" / "diagram_grill_cli.py",
    )
    if any(not item.is_file() or item.is_symlink() for item in required):
        raise RunnerError("skill package is missing SKILL.md, requirements.lock, or its CLI")
    skill_header = (package / "SKILL.md").read_text(encoding="utf-8")[:4000]
    if re.search(r"(?m)^name:\s*diagram-grill\s*$", skill_header) is None:
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
        raise RunnerError("skill package contains more than {} files".format(_MAX_PACKAGE_FILES))
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


def _protocol_hashes(case_id: str, eval_root: Path = EVAL_ROOT) -> Dict[str, str]:
    root = Path(eval_root)
    case = _case(case_id, root)
    return {
        "operator_answers_sha256": _sha256(root / "operator-answers.json"),
        "case_oracle_sha256": _sha256(root / str(case["oracle_path"])),
        "cases_index_sha256": _sha256(root / "cases.json"),
        "run_schema_sha256": _sha256(root / "run.schema.json"),
    }


def _verify_protocol_hashes(state: Mapping[str, Any]) -> None:
    expected = state.get("input", {}).get("protocol_hashes")
    if not isinstance(expected, Mapping):
        raise RunnerError("run state has no frozen protocol input hashes")
    current = _protocol_hashes(str(state["case_id"]))
    changed = sorted(
        name for name, digest in current.items() if expected.get(name) != digest
    )
    if changed or set(expected) != set(current):
        raise RunnerError(
            "frozen protocol input changed after preparation: {}".format(
                ", ".join(changed or ("hash set",))
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


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(str(source), str(destination), symlinks=False)


def _copy_skill_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        str(source),
        str(destination),
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )


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


def collect_execution_metadata(
    model: str, model_version: str, codex_executable: str = "codex"
) -> Dict[str, Any]:
    """Collect frozen client and renderer metadata without making a model call."""

    resolved_codex = shutil.which(codex_executable)
    if resolved_codex is None:
        raise RunnerError("Codex CLI is not on PATH: {}".format(codex_executable))
    try:
        diagrams_version = importlib.metadata.version("diagrams")
    except importlib.metadata.PackageNotFoundError:
        diagrams_version = "not-installed"
    git_commit = _command_output(("git", "rev-parse", "HEAD"), REPOSITORY_ROOT)
    git_status = _command_output(
        ("git", "status", "--porcelain", "--untracked-files=all"), REPOSITORY_ROOT
    )
    return {
        "client": "Codex CLI",
        "client_version": _command_output((resolved_codex, "--version")),
        "client_executable": resolved_codex,
        "model_requested": model,
        "model_version_declared": model_version,
        "operating_system": platform.platform(),
        "runner_environment": {
            "python_version": platform.python_version(),
            "graphviz_version": _command_output(("dot", "-V")),
            "diagrams_version": diagrams_version,
        },
        "skill_commit": git_commit,
        "repository_clean": git_status == "unavailable" or not git_status,
        "tested_at": _now(),
    }


def _assert_external_runtime_path(runtime_bin: Path, results_root: Path) -> Path:
    selected = Path(runtime_bin).resolve()
    forbidden_roots = (
        REPOSITORY_ROOT.resolve(),
        EVAL_ROOT.resolve(),
        Path(results_root).resolve(),
    )
    for forbidden in forbidden_roots:
        try:
            selected.relative_to(forbidden)
        except ValueError:
            continue
        raise RunnerError("runtime bin must be outside the repository and results root")
    return selected


def validate_runtime(
    runtime_bin: Path, skill_package: Path, results_root: Path
) -> Dict[str, Any]:
    """Verify the external Python runtime with the skill's own preflight command."""

    selected = _assert_external_runtime_path(runtime_bin, results_root)
    python = selected / "python3"
    if not selected.is_dir() or not python.is_file() or not os.access(str(python), os.X_OK):
        raise RunnerError("runtime bin must contain an executable python3: {}".format(selected))
    cli = Path(skill_package).resolve() / "scripts" / "diagram_grill_cli.py"
    if not cli.is_file() or cli.is_symlink():
        raise RunnerError("skill package is missing scripts/diagram_grill_cli.py")

    environment = _safe_codex_environment(selected)
    preflight = subprocess.run(
        (str(python), str(cli), "preflight", "--json"),
        cwd=str(Path(skill_package).resolve()),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    if preflight.returncode != 0:
        raise RunnerError(
            "external runtime failed skill preflight: {}".format(
                preflight.stderr.strip() or preflight.stdout.strip() or "no diagnostic"
            )
        )
    try:
        preflight_data = json.loads(preflight.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError("skill preflight did not return JSON") from exc
    if not isinstance(preflight_data, Mapping) or preflight_data.get("ok") is not True:
        raise RunnerError("external runtime did not pass every skill preflight check")

    frozen = subprocess.run(
        (str(python), "-m", "pip", "freeze", "--all"),
        cwd=str(Path(skill_package).resolve()),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    if frozen.returncode != 0:
        raise RunnerError(
            "could not freeze external runtime dependencies: {}".format(
                frozen.stderr.strip() or frozen.stdout.strip() or "no diagnostic"
            )
        )
    freeze_text = frozen.stdout
    if freeze_text and not freeze_text.endswith("\n"):
        freeze_text += "\n"
    return {
        "runtime_bin": str(selected),
        "python3": str(python.resolve()),
        "preflight": dict(preflight_data),
        "preflight_stdout_sha256": hashlib.sha256(
            preflight.stdout.encode("utf-8")
        ).hexdigest(),
        "pip_freeze": freeze_text,
        "pip_freeze_sha256": hashlib.sha256(freeze_text.encode("utf-8")).hexdigest(),
    }


def _assert_safe_id(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise RunnerError("{} must match {}".format(label, _SAFE_ID.pattern))


def _run_identifier(case_id: str, condition: str) -> str:
    return "{}--{}--r1".format(case_id, condition)


def prepare_run(
    results_root: Path,
    experiment_id: str,
    case_id: str,
    condition: str,
    model: str,
    model_version: str,
    runtime_bin: Path,
    skill_package: Path,
    eval_root: Path = EVAL_ROOT,
    execution_metadata: Optional[Mapping[str, Any]] = None,
    runtime_validation: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Create an oracle-free workspace and immutable input snapshots."""

    root = Path(eval_root).resolve()
    fixture_issues = harness.validate_fixtures(root)
    if fixture_issues:
        raise RunnerError("fixed fixtures are invalid: {}".format(fixture_issues[0]))
    _assert_safe_id(experiment_id, "experiment ID")
    if condition not in CONDITIONS:
        raise RunnerError("condition must be baseline or diagram-grill")
    if not model.strip() or not model_version.strip():
        raise RunnerError("model and model version are required")
    if skill_package is None:
        raise RunnerError("every condition requires a package for runtime validation")

    package_manifest = validate_skill_package(Path(skill_package))
    runtime_data = dict(
        runtime_validation
        or validate_runtime(Path(runtime_bin), Path(skill_package), Path(results_root))
    )
    selected_runtime_bin = _assert_external_runtime_path(
        Path(str(runtime_data.get("runtime_bin", ""))), Path(results_root)
    )
    if not selected_runtime_bin.is_dir():
        raise RunnerError("validated runtime bin is missing: {}".format(selected_runtime_bin))

    case = _case(case_id, root)
    run_dir = Path(results_root).resolve() / experiment_id / _run_identifier(case_id, condition)
    if run_dir.exists() or run_dir.is_symlink():
        raise RunnerError("run directory already exists; runs are never overwritten: {}".format(run_dir))
    input_dir = run_dir / "input"
    workspace = run_dir / "workspace"
    raw_dir = run_dir / "raw"
    input_dir.mkdir(parents=True)
    workspace.mkdir()
    raw_dir.mkdir()

    shell_profile = (
        "export PATH={}:\"$PATH\"\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
    ).format(shlex.quote(str(selected_runtime_bin)))
    shell_profile_dir = workspace / ".eval-shell"
    shell_profile_dir.mkdir()
    (shell_profile_dir / ".zprofile").write_text(shell_profile, encoding="utf-8")
    (input_dir / "shell-profile.zprofile").write_text(shell_profile, encoding="utf-8")

    prompt_source = root / str(case["prompt_path"])
    base_prompt = prompt_source.read_text(encoding="utf-8")
    delivered_prompt = (
        SKILL_INVOCATION_PREFIX + base_prompt
        if condition == "diagram-grill"
        else base_prompt
    )
    (input_dir / "prompt.md").write_text(base_prompt, encoding="utf-8")
    (input_dir / "delivered-prompt.md").write_text(delivered_prompt, encoding="utf-8")
    source_repo = root / str(case["repo_path"])
    _copy_tree(source_repo, input_dir / "repo")
    _copy_tree(source_repo, workspace / "repo")
    evidence_manifest = directory_manifest(input_dir / "repo")
    _write_json(input_dir / "repo-manifest.json", evidence_manifest)

    if condition == "diagram-grill":
        package_snapshot = input_dir / "skill-package"
        _copy_skill_tree(Path(skill_package).resolve(), package_snapshot)
        if directory_manifest(package_snapshot, ignore_python_cache=True) != package_manifest:
            raise RunnerError("skill package changed while the run was being prepared")
        installed = workspace / ".agents" / "skills" / "diagram-grill"
        installed.parent.mkdir(parents=True)
        _copy_skill_tree(package_snapshot, installed)
        _write_json(input_dir / "skill-package-manifest.json", package_manifest)

    preflight_data = runtime_data.get("preflight")
    freeze_text = runtime_data.get("pip_freeze")
    if not isinstance(preflight_data, Mapping) or not isinstance(freeze_text, str):
        raise RunnerError("runtime validation data is malformed")
    _write_json(input_dir / "runtime-preflight.json", dict(preflight_data))
    (input_dir / "pip-freeze.txt").write_text(freeze_text, encoding="utf-8")
    runtime_manifest = {
        "runtime_bin": str(selected_runtime_bin),
        "python3": str(runtime_data.get("python3", "")),
        "preflight_sha256": _sha256(input_dir / "runtime-preflight.json"),
        "pip_freeze_sha256": _sha256(input_dir / "pip-freeze.txt"),
        "requirements_lock_sha256": _sha256(
            Path(skill_package).resolve() / "requirements.lock"
        ),
    }
    _write_json(input_dir / "runtime-manifest.json", runtime_manifest)

    metadata = dict(execution_metadata or collect_execution_metadata(model, model_version))
    metadata["model_requested"] = model
    metadata["model_version_declared"] = model_version
    checks = {
        str(check.get("name")): str(check.get("actual"))
        for check in preflight_data.get("checks", [])
        if isinstance(check, Mapping) and check.get("name") and check.get("actual")
    }
    required_runtime_versions = ("python", "diagrams", "jsonschema", "dot")
    missing_runtime_versions = [
        name for name in required_runtime_versions if name not in checks
    ]
    if missing_runtime_versions:
        raise RunnerError(
            "runtime preflight omitted authoritative version: {}".format(
                missing_runtime_versions[0]
            )
        )
    metadata["python_version"] = checks["python"]
    metadata["diagrams_version"] = checks["diagrams"]
    metadata["jsonschema_version"] = checks["jsonschema"]
    metadata["graphviz_version"] = checks["dot"]
    metadata["runtime"] = {
        **runtime_manifest,
        "authoritative_versions": {
            "python": checks["python"],
            "diagrams": checks["diagrams"],
            "jsonschema": checks["jsonschema"],
            "graphviz_dot": checks["dot"],
        },
    }
    metadata["treatment_settings"] = {
        "model_reasoning_effort": "medium",
        "service_tier": "default",
        "approval_policy": "never",
        "sandbox": "workspace-write",
        "ignore_user_config": True,
        "ignore_rules": True,
        "runtime_bin": str(selected_runtime_bin),
        "shell_environment_policy": "inherit-all-with-fixed-path-and-zdotdir",
    }
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
        "run_id": _run_identifier(case_id, condition),
        "planned_ordinal": plan_entry["ordinal"],
        "status": "prepared",
        "model": model,
        "model_version": model_version,
        "session_id": None,
        "turn_count": 0,
        "revealed_decision_ids": [],
        "approval_sent": False,
        "source_integrity_violations": [],
        "prepared_at": _now(),
        "updated_at": _now(),
        "execution": metadata,
        "input": {
            "base_prompt_path": "input/prompt.md",
            "base_prompt_sha256": _sha256(input_dir / "prompt.md"),
            "delivered_prompt_path": "input/delivered-prompt.md",
            "delivered_prompt_sha256": _sha256(input_dir / "delivered-prompt.md"),
            "skill_invocation_prefix": (
                SKILL_INVOCATION_PREFIX if condition == "diagram-grill" else ""
            ),
            "repo_manifest_sha256": evidence_manifest["manifest_sha256"],
            "skill_manifest_sha256": (
                package_manifest["manifest_sha256"]
            ),
            "runtime_manifest_path": "input/runtime-manifest.json",
            "runtime_manifest_sha256": _sha256(input_dir / "runtime-manifest.json"),
            "shell_profile_sha256": _sha256(input_dir / "shell-profile.zprofile"),
            "protocol_hashes": _protocol_hashes(case_id, root),
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


def _global_skill_conflicts() -> List[str]:
    candidates = [Path.home() / ".agents" / "skills" / "diagram-grill" / "SKILL.md"]
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    candidates.append(codex_home / "skills" / "diagram-grill" / "SKILL.md")
    return [str(path) for path in candidates if path.is_file()]


def _safe_codex_environment(
    runtime_bin: Path, zdotdir: Optional[Path] = None
) -> Dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "CODEX_HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    existing_path = environment.get("PATH", os.defpath)
    environment["PATH"] = str(runtime_bin) + os.pathsep + existing_path
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if zdotdir is not None:
        environment["ZDOTDIR"] = str(zdotdir)
    return environment


def _extract_session_id(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "thread.started":
            continue
        value = event.get("thread_id")
        if isinstance(value, str) and _THREAD_ID.fullmatch(value):
            return value
    return None


def _invoke_codex(
    run_dir: Path,
    state: Mapping[str, Any],
    prompt: str,
    turn: int,
    initial: bool,
    timeout_seconds: int,
) -> Dict[str, Any]:
    raw_dir = Path(run_dir) / "raw"
    stdout_path = raw_dir / "turn-{:03d}.jsonl".format(turn)
    stderr_path = raw_dir / "turn-{:03d}.stderr.txt".format(turn)
    last_message_path = raw_dir / "turn-{:03d}.last-message.txt".format(turn)
    executable = str(state["execution"]["client_executable"])
    runtime_bin = Path(state["execution"]["treatment_settings"]["runtime_bin"])
    shell_profile_dir = Path(run_dir) / "workspace" / ".eval-shell"
    inherited_path = str(runtime_bin) + os.pathsep + os.environ.get("PATH", os.defpath)
    common = [
        executable,
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'service_tier="default"',
        "-c",
        "shell_environment_policy.inherit=all",
        "-c",
        "shell_environment_policy.set.PATH={}".format(json.dumps(inherited_path)),
        "-c",
        "shell_environment_policy.set.ZDOTDIR={}".format(
            json.dumps(str(shell_profile_dir))
        ),
        "-c",
        'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"',
        "--model",
        str(state["model"]),
        "--output-last-message",
        str(last_message_path),
    ]
    if initial:
        command = common + [
            "--sandbox",
            "workspace-write",
            "--cd",
            str(Path(run_dir) / "workspace"),
            "-",
        ]
    else:
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or _THREAD_ID.fullmatch(session_id) is None:
            raise RunnerError("active run has no valid Codex session ID")
        command = common + ["resume", session_id, "-"]

    started_at = _now()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(run_dir) / "workspace"),
            env=_safe_codex_environment(runtime_bin, shell_profile_dir),
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code: Optional[int] = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        exit_code = None
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    with (raw_dir / "transcript.jsonl").open("a", encoding="utf-8") as transcript:
        transcript.write(stdout)
        if stdout and not stdout.endswith("\n"):
            transcript.write("\n")
    return {
        "turn": turn,
        "initial": initial,
        "started_at": started_at,
        "finished_at": _now(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout_path.relative_to(run_dir).as_posix(),
        "stderr": stderr_path.relative_to(run_dir).as_posix(),
        "last_message": (
            last_message_path.relative_to(run_dir).as_posix()
            if last_message_path.is_file()
            else None
        ),
        "session_id": _extract_session_id(stdout),
    }


def _record_operator_message(run_dir: Path, turn: int, kind: str, text: str, **extra: Any) -> None:
    event: Dict[str, Any] = {
        "turn": turn,
        "kind": kind,
        "text": text,
        "sent_at": _now(),
    }
    event.update(extra)
    _append_jsonl(Path(run_dir) / "raw" / "operator-turns.jsonl", event)


def _integrity_violations(run_dir: Path, state: Mapping[str, Any]) -> List[str]:
    violations: List[str] = []
    input_dir = Path(run_dir) / "input"
    workspace = Path(run_dir) / "workspace"
    expected_repo = _load_json(input_dir / "repo-manifest.json")
    try:
        current_repo = directory_manifest(workspace / "repo")
    except (OSError, RunnerError):
        violations.append("evidence repository is missing, symlinked, or unreadable")
    else:
        if current_repo["manifest_sha256"] != expected_repo["manifest_sha256"]:
            violations.append("evidence repository changed after preparation")
    profile = workspace / ".eval-shell" / ".zprofile"
    if not profile.is_file() or _sha256(profile) != state["input"]["shell_profile_sha256"]:
        violations.append("evaluation shell profile changed during the run")
    if state["condition"] == "diagram-grill":
        expected_skill = _load_json(input_dir / "skill-package-manifest.json")
        try:
            current_skill = directory_manifest(
                workspace / ".agents" / "skills" / "diagram-grill",
                ignore_python_cache=True,
            )
        except (OSError, RunnerError):
            violations.append("installed skill is missing, symlinked, or unreadable")
        else:
            if current_skill["manifest_sha256"] != expected_skill["manifest_sha256"]:
                violations.append("installed skill package changed during the run")
    return violations


Invoke = Callable[[Path, Mapping[str, Any], str, int, bool, int], Dict[str, Any]]


def _apply_turn_result(
    run_dir: Path, state: Dict[str, Any], result: Mapping[str, Any], initial: bool
) -> Dict[str, Any]:
    _append_jsonl(Path(run_dir) / "raw" / "turn-index.jsonl", dict(result))
    state["turn_count"] = int(result["turn"])
    if initial:
        session_id = result.get("session_id")
        if not isinstance(session_id, str):
            state["status"] = "client-failed"
        else:
            state["session_id"] = session_id
            state["status"] = "active"
    if result.get("timed_out") or result.get("exit_code") != 0:
        state["status"] = "client-failed"
    state["source_integrity_violations"] = sorted(
        set(state.get("source_integrity_violations", []))
        | set(_integrity_violations(run_dir, state))
    )
    _write_state(run_dir, state)
    return state


def launch_run(
    run_dir: Path,
    timeout_seconds: int = 900,
    allow_dirty: bool = False,
    invoke: Invoke = _invoke_codex,
) -> Dict[str, Any]:
    """Make the first model call for a prepared run."""

    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state["status"] != "prepared":
        raise RunnerError("only a prepared run can be launched")
    conflicts = _global_skill_conflicts()
    if conflicts:
        raise RunnerError("remove the global diagram-grill installation before evaluation")
    if not bool(state["execution"].get("repository_clean")) and not allow_dirty:
        raise RunnerError("repository is dirty; commit inputs or pass --allow-dirty for a pilot")
    prompt = (directory / "input" / "delivered-prompt.md").read_text(encoding="utf-8")
    _record_operator_message(directory, 1, "initial-prompt", prompt)
    result = invoke(directory, state, prompt, 1, True, timeout_seconds)
    return _apply_turn_result(directory, state, result, True)


def reply_run(
    run_dir: Path,
    decision_ids: Sequence[str] = (),
    unknown: bool = False,
    approve: bool = False,
    brief_reviewed: bool = False,
    timeout_seconds: int = 900,
    invoke: Invoke = _invoke_codex,
) -> Dict[str, Any]:
    """Resume with exact scripted answers chosen by the human operator."""

    directory = Path(run_dir).resolve()
    state = _state(directory)
    if state["status"] != "active":
        raise RunnerError("only an active run can receive an operator reply")
    _verify_protocol_hashes(state)
    if approve and (decision_ids or unknown):
        raise RunnerError("approval cannot be combined with another operator reply")
    if not approve and not decision_ids and not unknown:
        raise RunnerError("choose decision IDs, unknown, or approve")
    answers, approval_phrase = _operator_case(str(state["case_id"]), EVAL_ROOT)
    kind: str
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
            raise RunnerError(
                "approval is withheld because indexed decisions remain unasked; close the run"
            )
        prompt = approval_phrase
        kind = "architecture-brief-approval"
        state["approval_sent"] = True
        extra["brief_reviewed"] = True

    turn = int(state["turn_count"]) + 1
    _record_operator_message(directory, turn, kind, prompt, **extra)
    result = invoke(directory, state, prompt, turn, False, timeout_seconds)
    return _apply_turn_result(directory, state, result, False)


def close_run(run_dir: Path, reason: str) -> Dict[str, Any]:
    """Close a run without another model call; failed and partial outputs remain intact."""

    allowed = (
        "agent-finished",
        "approval-withheld",
        "client-failed",
        "operator-terminated",
    )
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


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("plan", help="print the frozen alternating 24-run plan")
    prepare = commands.add_parser("prepare", help="create one oracle-free run workspace")
    prepare.add_argument("--results-root", type=Path, required=True)
    prepare.add_argument("--experiment", required=True)
    prepare.add_argument("--case", required=True)
    prepare.add_argument("--condition", choices=CONDITIONS, required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--model-version", required=True)
    prepare.add_argument("--runtime-bin", type=Path, required=True)
    prepare.add_argument("--skill-package", type=Path, required=True)

    launch = commands.add_parser("launch", help="execute the initial prompt")
    launch.add_argument("run_dir", type=Path)
    launch.add_argument("--execute", action="store_true", help="confirm a billable model call")
    launch.add_argument("--allow-dirty", action="store_true")
    launch.add_argument("--timeout", type=int, default=900)

    reply = commands.add_parser("reply", help="resume with one scripted operator reply")
    reply.add_argument("run_dir", type=Path)
    reply.add_argument("--execute", action="store_true", help="confirm a billable model call")
    reply.add_argument("--decision-id", action="append", default=[])
    reply.add_argument("--unknown", action="store_true")
    reply.add_argument("--approve", action="store_true")
    reply.add_argument("--brief-reviewed", action="store_true")
    reply.add_argument("--timeout", type=int, default=900)

    close = commands.add_parser("close", help="close without another model call")
    close.add_argument("run_dir", type=Path)
    close.add_argument(
        "--reason",
        choices=(
            "agent-finished",
            "approval-withheld",
            "client-failed",
            "operator-terminated",
        ),
        required=True,
    )
    status = commands.add_parser("status", help="print retained run state")
    status.add_argument("run_dir", type=Path)

    args = parser.parse_args(list(argv) if argv else None)
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
                args.runtime_bin,
                args.skill_package,
            )
            _print_json({"ok": True, "run_dir": str(run_dir), "state": _state(run_dir)})
            return 0
        if args.command == "launch":
            if not args.execute:
                raise RunnerError("launch requires --execute to confirm a model call")
            _print_json(launch_run(args.run_dir, args.timeout, args.allow_dirty))
            return 0
        if args.command == "reply":
            if not args.execute:
                raise RunnerError("reply requires --execute to confirm a model call")
            _print_json(
                reply_run(
                    args.run_dir,
                    args.decision_id,
                    args.unknown,
                    args.approve,
                    args.brief_reviewed,
                    args.timeout,
                )
            )
            return 0
        if args.command == "close":
            _print_json(close_run(args.run_dir, args.reason))
            return 0
        _print_json(_state(args.run_dir))
        return 0
    except (RunnerError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print("diagram-grill-codex-eval: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
