#!/usr/bin/env python3
"""Credential-free Docker isolation boundary for the Codex evaluation.

Public state contract for :func:`run_turn`::

    state = {
        "condition": "baseline" | "diagram-grill",
        "model": "gpt-5.4",
        "session_id": None | "UUID",
        "run_id": "bounded-run-id",
        "execution": {"isolation": {
            "runtime_validation": validate_runtime(...),
            "workspace_snapshot_path": "/absolute/.../snapshots/workspace",
            "codex_home_snapshot_path": "/absolute/.../codex-home",
        }},
    }

The agent container never receives an API credential.  A dedicated proxy
container receives one bounded credential envelope over stdin, while every
Docker CLI invocation revalidates the executable, socket, and seccomp profile.
"""

from __future__ import annotations

import grp
import hashlib
import io
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unicodedata
import fcntl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


EVAL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = EVAL_ROOT.parent
RUNTIME_ROOT = EVAL_ROOT / "runtime"
SECCOMP_PROFILE = EVAL_ROOT / "seccomp-bwrap.json"

CODEX_CLI_VERSION = "0.149.1"
SECCOMP_PROFILE_SHA256 = (
    "9d518673d0165b530cce86e08e606d431ebc4ea6cc20b803e5e91fb0da10c8ed"
)
SECCOMP_PROFILE_CANONICAL_SHA256 = (
    "81c9af403f5aff1fb682adb245a6bed21c267112e5ac29fe99bf1a7601e9c58c"
)
AGENT_RUNTIME_CONTRACT = "credential-free-agent-v1"
PROXY_RUNTIME_CONTRACT = "stdin-credential-proxy-v1"
AGENT_ENTRYPOINT = ["python3", "-I", "-S", "/opt/diagram-grill-eval/supervisor.py"]
PROXY_ENTRYPOINT = ["python3", "-I", "-S", "/opt/diagram-grill-eval/responses_proxy.py"]

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CODEX_HOME = "/codex-home"
CONTAINER_SKILL = "/codex-home/skills/diagram-grill"
CONTAINER_RUNTIME_ROOT = "/opt/diagram-grill-eval"
CONTAINER_RUNTIME_PATH = "/usr/local/bin:/usr/bin:/bin"
CONTAINER_PROXY_HOST = "eval-proxy"
CONTAINER_PROXY_PORT = 8080
CONFIG_PROBE_PORT = 18080

CONTAINER_PIDS_LIMIT = 256
CONTAINER_MEMORY = "2g"
CONTAINER_CPUS = "2.0"
PROXY_PIDS_LIMIT = 64
PROXY_MEMORY = "256m"
PROXY_CPUS = "1.0"
WORKSPACE_LIMIT_BYTES = 64 * 1024 * 1024
STATE_LIMIT_BYTES = 64 * 1024 * 1024
WORKSPACE_TMPFS_BYTES = 80 * 1024 * 1024
STATE_TMPFS_BYTES = 80 * 1024 * 1024
TMP_LIMIT_BYTES = 96 * 1024 * 1024
SNAPSHOT_MAX_ENTRIES = 4096
SNAPSHOT_INODE_LIMIT = 4096
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 2 * 1024 * 1024
MAX_LAST_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_OVERHEAD_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_TURNS_PER_RUN = 12
MAX_DOCKER_DIAGNOSTIC_BYTES = 64 * 1024
API_KEY_MIN_BYTES = 20
API_KEY_MAX_BYTES = 512
MAX_TURN_TIMEOUT_SECONDS = 900
PROXY_START_TIMEOUT_SECONDS = 15
MINIMUM_DOCKER_ENGINE_MAJOR = 28
RESOURCE_LABEL = "io.diagram-grill.eval.managed=true"
RESOURCE_LABEL_KEY = "io.diagram-grill.eval.managed"
RESOURCE_LABEL_VALUE = "true"
RESOURCE_SCOPE_LABEL_KEY = "io.diagram-grill.eval.state-scope"
CAPABILITY_HEADER = "X-Diagram-Grill-Eval-Capability"
AGENT_MAX_LIFETIME_SECONDS = 1_200

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_CONTAINER_NAME_PART = re.compile(r"[^a-z0-9_.-]+")
_TRUSTED_OWNER_IDS = (
    frozenset((0, os.geteuid())) if hasattr(os, "geteuid") else frozenset((0,))
)
_SENSITIVE_ENV = re.compile(
    r"(?:API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH(?:ORIZATION|_JSON)?|CREDENTIAL|SECRET)",
    re.IGNORECASE,
)
_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "network_proxy",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
)


class IsolationError(ValueError):
    """Raised when the secure evaluation boundary cannot be preserved."""


class _CredentialExposure(IsolationError):
    """Internal signal that secret material crossed the agent boundary."""


class _TerminationRequested(IsolationError):
    """Internal signal used to unwind through deterministic Docker cleanup."""


@dataclass
class _TerminationState:
    previous: Dict[int, Any]
    requested_signum: Optional[int] = None
    deferred_depth: int = 0


_TERMINATION_STATE: Optional[_TerminationState] = None


@dataclass(frozen=True)
class Capture:
    returncode: Optional[int]
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    limited_stream: Optional[str] = None
    input_error: Optional[str] = None


@dataclass
class ManagedProcess:
    process: subprocess.Popen[bytes]
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    limited_stream: Optional[str] = None
    readers: List[threading.Thread] = field(default_factory=list)
    writer: Optional[threading.Thread] = None
    input_error: Optional[str] = None
    input_finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class SnapshotEntry:
    path: Path
    relative: str
    normalized: str
    mode: int
    size: int
    is_directory: bool
    device: int
    inode: int
    mtime_ns: int


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _group_is_trusted(group_id: int) -> bool:
    try:
        group = grp.getgrgid(group_id)
        if group.gr_gid != group_id:
            return False
        current_uid = os.geteuid()
        users = list(pwd.getpwall())
        allowed_uids = {0, current_uid}
        explicit_names = set(group.gr_mem)
        if any(not isinstance(name, str) or not name for name in explicit_names):
            return False
        explicit_member_ids = {
            pwd.getpwnam(member_name).pw_uid for member_name in explicit_names
        }
    except (KeyError, OSError, TypeError, ValueError):
        return False
    # Directory Services may return duplicate names from getpwall(). Names are
    # not a unique security identity, so validate every primary-group record
    # and every record matching an explicit member independently by UID.
    return explicit_member_ids.issubset(allowed_uids) and all(
        user.pw_uid in allowed_uids
        for user in users
        if user.pw_gid == group_id or user.pw_name in explicit_names
    )


def _darwin_acl_output_is_trusted(output: str, expected_paths: int) -> bool:
    path_lines = 0
    for line in output.splitlines():
        if not line or not line[0].isspace():
            if not line:
                return False
            path_lines += 1
            continue
        numbered_entry = line.strip().partition(":")
        if (
            not numbered_entry[1]
            or not numbered_entry[0].isdigit()
            or numbered_entry[2].strip() != "group:everyone deny delete"
        ):
            return False
    return path_lines == expected_paths


def _reject_extended_acl(path: Path) -> None:
    system = platform.system()
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    if system == "Darwin":
        command = ["/bin/ls", "-lde", str(path)]
        try:
            completed = subprocess.run(
                command,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IsolationError("cannot inspect ACLs on trusted path") from error
        if completed.returncode != 0 or not _darwin_acl_output_is_trusted(
            completed.stdout, 1
        ):
            raise IsolationError("trusted path has an unreadable or extended ACL")
        return
    if system == "Linux":
        executable = next(
            (candidate for candidate in ("/usr/bin/getfacl", "/bin/getfacl") if Path(candidate).is_file()),
            None,
        )
        if executable is None:
            raise IsolationError("getfacl is required to validate Docker trust on Linux")
        completed = subprocess.run(
            [executable, "--absolute-names", "--omit-header", str(path)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode != 0:
            raise IsolationError("cannot inspect ACLs on trusted path")
        entries = [
            line
            for line in completed.stdout.splitlines()
            if line and not line.startswith("#")
        ]
        if any(
            line.startswith("default:")
            or (line.startswith("user:") and not line.startswith("user::"))
            or (line.startswith("group:") and not line.startswith("group::"))
            for line in entries
        ):
            raise IsolationError("trusted path has an extended ACL: {}".format(path))
        return
    raise IsolationError("Docker trust validation supports only macOS and Linux")


def _exact_existing_path(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise IsolationError("{} must be absolute".format(label))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise IsolationError("{} is unavailable".format(label)) from error
    if resolved != candidate:
        raise IsolationError("{} cannot contain symlinks or aliases".format(label))

    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        try:
            with os.scandir(current) as entries:
                exact = next(entry for entry in entries if entry.name == part)
        except (OSError, StopIteration) as error:
            raise IsolationError("{} contains a case alias".format(label)) from error
        current = Path(exact.path)
    try:
        same = current.samefile(candidate)
    except OSError as error:
        raise IsolationError("{} identity is unavailable".format(label)) from error
    if not same:
        raise IsolationError("{} identity is inconsistent".format(label))
    return resolved


def _exact_absolute_path(path: Path) -> Path:
    return _exact_existing_path(path, "trusted Docker path")


def _validate_trusted_components(path: Path) -> Path:
    selected = _exact_absolute_path(path)
    current = Path(selected.anchor)
    components = [current]
    for part in selected.parts[1:]:
        current = current / part
        components.append(current)
    for component in components:
        result = component.lstat()
        if stat.S_ISLNK(result.st_mode):
            raise IsolationError("trusted Docker path contains a symlink")
        if result.st_uid not in _TRUSTED_OWNER_IDS:
            raise IsolationError("trusted Docker path has an untrusted owner")
        permissions = stat.S_IMODE(result.st_mode)
        if permissions & stat.S_IWOTH:
            raise IsolationError("trusted Docker path is writable by others")
        if permissions & stat.S_IWGRP and not _group_is_trusted(result.st_gid):
            raise IsolationError("trusted Docker path is writable by an untrusted group")
        _reject_extended_acl(component)
    return selected


def _file_identity(path: Path, kind: str) -> Dict[str, Any]:
    selected = _validate_trusted_components(path)
    result = selected.lstat()
    if kind == "executable":
        if not stat.S_ISREG(result.st_mode) or not os.access(str(selected), os.X_OK):
            raise IsolationError("Docker executable must be a regular executable file")
    elif kind == "socket":
        if not stat.S_ISSOCK(result.st_mode):
            raise IsolationError("Docker socket path is not a Unix socket")
    elif kind == "regular":
        if not stat.S_ISREG(result.st_mode):
            raise IsolationError("trusted profile must be a regular file")
    else:
        raise AssertionError("unknown trusted path kind")
    identity: Dict[str, Any] = {
        "path": str(selected),
        "kind": kind,
        "device": result.st_dev,
        "inode": result.st_ino,
        "mode": stat.S_IMODE(result.st_mode),
        "uid": result.st_uid,
        "gid": result.st_gid,
        "size": result.st_size,
        "mtime_ns": result.st_mtime_ns,
        "ctime_ns": result.st_ctime_ns,
    }
    if kind in ("executable", "regular"):
        identity["sha256"] = _sha256_file(selected)
    return identity


def _revalidate_identity(identity: Mapping[str, Any]) -> None:
    try:
        current = _file_identity(Path(str(identity["path"])), str(identity["kind"]))
    except (KeyError, TypeError, ValueError) as error:
        raise IsolationError("trusted Docker runtime identity is malformed") from error
    if current != dict(identity):
        raise IsolationError("trusted Docker runtime identity changed after validation")


def _docker_environment(socket_path: str) -> Dict[str, str]:
    return {
        "DOCKER_CONFIG": "/var/empty/diagram-grill-eval-no-config",
        "DOCKER_HOST": "unix://{}".format(socket_path),
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _read_stream(
    managed: ManagedProcess,
    stream: BinaryIO,
    target: bytearray,
    limit: int,
    label: str,
) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with managed.lock:
                remaining = max(0, limit - len(target))
                target.extend(chunk[:remaining])
                if len(chunk) > remaining and managed.limited_stream is None:
                    managed.limited_stream = label
                    try:
                        managed.process.kill()
                    except OSError:
                        pass
    finally:
        stream.close()


def _write_input(
    managed: ManagedProcess, stream: BinaryIO, input_data: bytearray | bytes
) -> None:
    try:
        view = memoryview(input_data)
        while view:
            written = stream.write(view[: 64 * 1024])
            if written is None:
                written = 0
            if written <= 0:
                raise OSError("short subprocess stdin write")
            view = view[written:]
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as error:
        with managed.lock:
            managed.input_error = type(error).__name__
    finally:
        try:
            stream.close()
        except OSError:
            pass
        managed.input_finished.set()


def _start_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    input_data: Optional[bytearray | bytes] = None,
    stdout_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
    stderr_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
) -> ManagedProcess:
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            close_fds=True,
        )
    except OSError as error:
        raise IsolationError("subprocess could not start") from error
    managed = ManagedProcess(process=process)
    assert process.stdout is not None and process.stderr is not None
    for stream, target, limit, label in (
        (process.stdout, managed.stdout, stdout_limit, "stdout"),
        (process.stderr, managed.stderr, stderr_limit, "stderr"),
    ):
        reader = threading.Thread(
            target=_read_stream,
            args=(managed, stream, target, limit, label),
            daemon=True,
        )
        reader.start()
        managed.readers.append(reader)
    if input_data is not None:
        assert process.stdin is not None
        managed.writer = threading.Thread(
            target=_write_input,
            args=(managed, process.stdin, input_data),
            daemon=True,
        )
        managed.writer.start()
    else:
        managed.input_finished.set()
    return managed


def _wait_for_input(managed: ManagedProcess, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while not managed.input_finished.wait(
        min(0.1, max(0.0, deadline - time.monotonic()))
    ):
        try:
            _raise_if_termination_requested()
        except _TerminationRequested:
            _finish_process(managed, 5, terminate=True)
            raise
        if time.monotonic() >= deadline:
            break
    if not managed.input_finished.is_set():
        try:
            managed.process.kill()
        except OSError:
            pass
        raise IsolationError("bounded subprocess input did not drain")
    if managed.writer is not None:
        managed.writer.join(timeout=1)
    if managed.input_error is not None:
        raise IsolationError("bounded subprocess input was rejected")


def _finish_process(
    managed: ManagedProcess, timeout: int, *, terminate: bool = False
) -> Capture:
    if terminate and managed.process.poll() is None:
        try:
            managed.process.terminate()
        except OSError:
            pass
    timed_out = False
    interrupted_signum: Optional[int] = None
    deadline = time.monotonic() + timeout
    while managed.process.poll() is None:
        state = _TERMINATION_STATE
        if (
            not terminate
            and interrupted_signum is None
            and state is not None
            and state.deferred_depth == 0
            and state.requested_signum is not None
        ):
            interrupted_signum = state.requested_signum
            deadline = min(deadline, time.monotonic() + 5)
            try:
                managed.process.terminate()
            except OSError:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = interrupted_signum is None
            try:
                managed.process.kill()
            except OSError:
                pass
            try:
                managed.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            break
        try:
            managed.process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
    returncode = managed.process.poll()
    if managed.writer is not None:
        managed.writer.join(timeout=5)
        if managed.writer.is_alive() and managed.input_error is None:
            managed.input_error = "writer_did_not_stop"
    for reader in managed.readers:
        reader.join(timeout=5)
    capture = Capture(
        returncode=returncode,
        stdout=bytes(managed.stdout),
        stderr=bytes(managed.stderr),
        timed_out=timed_out,
        limited_stream=managed.limited_stream,
        input_error=managed.input_error,
    )
    if interrupted_signum is not None:
        raise _TerminationRequested(
            "evaluation interrupted by signal {}".format(interrupted_signum)
        )
    return capture


def _capture_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: int,
    input_data: Optional[bytearray | bytes] = None,
    stdout_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
    stderr_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
) -> Capture:
    managed = _start_process(
        command,
        env=env,
        input_data=input_data,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )
    try:
        return _finish_process(managed, timeout)
    except BaseException:
        deferred = _defer_termination()
        try:
            _finish_process(managed, 5, terminate=True)
        finally:
            _resume_termination(deferred)
        raise


def _runtime_trust(runtime: Mapping[str, Any]) -> Mapping[str, Any]:
    trust = runtime.get("trust")
    if not isinstance(trust, Mapping):
        raise IsolationError("runtime validation has no trust metadata")
    for key in ("docker_executable", "docker_socket", "seccomp_profile"):
        identity = trust.get(key)
        if not isinstance(identity, Mapping):
            raise IsolationError("runtime trust metadata is malformed")
    return trust


def _revalidate_runtime(runtime: Mapping[str, Any]) -> None:
    trust = _runtime_trust(runtime)
    _revalidate_identity(trust["docker_executable"])
    _revalidate_identity(trust["docker_socket"])
    _revalidate_identity(trust["seccomp_profile"])


def _docker_capture(
    runtime: Mapping[str, Any],
    arguments: Sequence[str],
    *,
    timeout: int = 60,
    input_data: Optional[bytearray | bytes] = None,
    stdout_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
    stderr_limit: int = MAX_DOCKER_DIAGNOSTIC_BYTES,
    check: bool = True,
) -> Capture:
    _revalidate_runtime(runtime)
    trust = _runtime_trust(runtime)
    capture = _capture_process(
        [str(trust["docker_executable"]["path"]), *arguments],
        env=_docker_environment(str(trust["docker_socket"]["path"])),
        timeout=timeout,
        input_data=input_data,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )
    if check and (
        capture.timed_out
        or capture.limited_stream is not None
        or capture.input_error is not None
        or capture.returncode != 0
    ):
        diagnostic = capture.stderr.decode("utf-8", "replace")[:2000].strip()
        raise IsolationError(
            "Docker command failed: {}".format(diagnostic or "no bounded diagnostic")
        )
    return capture


def _docker_start(
    runtime: Mapping[str, Any],
    arguments: Sequence[str],
    *,
    input_data: Optional[bytearray | bytes] = None,
    stdout_limit: int,
    stderr_limit: int,
) -> ManagedProcess:
    _revalidate_runtime(runtime)
    trust = _runtime_trust(runtime)
    return _start_process(
        [str(trust["docker_executable"]["path"]), *arguments],
        env=_docker_environment(str(trust["docker_socket"]["path"])),
        input_data=input_data,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )


def _real_directory(path: Path, label: str, *, writable: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise IsolationError("{} must be an absolute real directory".format(label))
    selected = _exact_existing_path(candidate, label)
    if not selected.is_dir() or selected.is_symlink():
        raise IsolationError("{} must be a real directory".format(label))
    current = Path(selected.anchor)
    for part in selected.parts[1:]:
        current = current / part
        result = current.lstat()
        if stat.S_ISLNK(result.st_mode):
            raise IsolationError("{} contains a symlinked parent".format(label))
    access = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(str(selected), access):
        raise IsolationError("{} has insufficient permissions".format(label))
    if any(character in str(selected) for character in (",", "\n", "\r")):
        raise IsolationError("{} has an unsafe Docker path".format(label))
    return selected


def _private_directory(path: Path, label: str, *, writable: bool = False) -> Path:
    selected = _real_directory(path, label, writable=writable)
    result = selected.lstat()
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
    if result.st_uid != expected_uid or stat.S_IMODE(result.st_mode) & 0o077:
        raise IsolationError("{} must be owned by the current user with mode 0700".format(label))
    _reject_extended_acl(selected)
    return selected


def _canonical_name(name: str) -> Tuple[str, str]:
    if (
        not name
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise IsolationError("snapshot member has an invalid name")
    pure = PurePosixPath(name.rstrip("/"))
    if pure.is_absolute() or not pure.parts or any(
        part in ("", ".", "..") for part in pure.parts
    ):
        raise IsolationError("snapshot member escapes its root")
    normalized = "/".join(unicodedata.normalize("NFC", part) for part in pure.parts)
    return normalized, normalized.casefold()


def _scan_snapshot(
    root: Path, *, max_entries: int, max_bytes: int
) -> List[SnapshotEntry]:
    selected = _real_directory(root, "snapshot root")
    pending: List[Tuple[Path, str]] = [(selected, "")]
    entries: List[SnapshotEntry] = []
    seen = set()
    total_bytes = 0
    while pending:
        directory, prefix = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as error:
            raise IsolationError("snapshot directory is unreadable") from error
        for child in children:
            relative = "{}/{}".format(prefix, child.name).lstrip("/")
            normalized, folded = _canonical_name(relative)
            if folded in seen:
                raise IsolationError("snapshot contains a Unicode or casefold duplicate")
            seen.add(folded)
            result = child.stat(follow_symlinks=False)
            mode = result.st_mode
            if stat.S_ISLNK(mode):
                raise IsolationError("snapshot contains a symlink")
            if stat.S_ISDIR(mode):
                entry = SnapshotEntry(
                    Path(child.path),
                    relative,
                    normalized,
                    mode,
                    0,
                    True,
                    result.st_dev,
                    result.st_ino,
                    result.st_mtime_ns,
                )
                pending.append((Path(child.path), relative))
            elif stat.S_ISREG(mode):
                if result.st_nlink != 1:
                    raise IsolationError("snapshot contains a hard-linked file")
                total_bytes += result.st_size
                if total_bytes > max_bytes:
                    raise IsolationError("snapshot exceeds its byte limit")
                entry = SnapshotEntry(
                    Path(child.path),
                    relative,
                    normalized,
                    mode,
                    result.st_size,
                    False,
                    result.st_dev,
                    result.st_ino,
                    result.st_mtime_ns,
                )
            else:
                raise IsolationError("snapshot contains a special file")
            entries.append(entry)
            if len(entries) > max_entries:
                raise IsolationError("snapshot exceeds its entry limit")
    return sorted(entries, key=lambda entry: entry.normalized)


def _open_snapshot_entry(entry: SnapshotEntry) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(entry.path, flags)
    result = os.fstat(descriptor)
    identity = (result.st_dev, result.st_ino, result.st_size, result.st_mtime_ns)
    expected = (entry.device, entry.inode, entry.size, entry.mtime_ns)
    if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1 or identity != expected:
        os.close(descriptor)
        raise IsolationError("snapshot file changed while archiving")
    return os.fdopen(descriptor, "rb")


def _snapshot_archive(root: Path, *, max_bytes: int) -> bytes:
    entries = _scan_snapshot(
        root, max_entries=SNAPSHOT_MAX_ENTRIES, max_bytes=max_bytes
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for entry in entries:
            info = tarfile.TarInfo(
                entry.normalized + ("/" if entry.is_directory else "")
            )
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if entry.is_directory:
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.mode = 0o600
            info.size = entry.size
            with _open_snapshot_entry(entry) as source:
                archive.addfile(info, source)
    value = output.getvalue()
    if len(value) > max_bytes + MAX_ARCHIVE_OVERHEAD_BYTES:
        raise IsolationError("snapshot archive exceeds its transport limit")
    return value


def _contains_secret(data: bytes | bytearray, secrets_: Sequence[bytearray]) -> bool:
    return any(secret and data.find(secret) >= 0 for secret in secrets_)


def _validated_archive_entries(
    archive_bytes: bytes,
    *,
    max_bytes: int,
    secrets_: Sequence[bytearray],
) -> List[Tuple[str, bool, bytes]]:
    if len(archive_bytes) > max_bytes + MAX_ARCHIVE_OVERHEAD_BYTES:
        raise IsolationError("snapshot archive exceeds its transport limit")
    accepted: List[Tuple[str, bool, bytes]] = []
    seen = set()
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            for member in archive:
                if len(accepted) >= SNAPSHOT_MAX_ENTRIES:
                    raise IsolationError("snapshot exceeds its entry limit")
                normalized, folded = _canonical_name(member.name)
                if folded in seen:
                    raise IsolationError(
                        "snapshot contains a Unicode or casefold duplicate"
                    )
                seen.add(folded)
                if _contains_secret(normalized.encode("utf-8"), secrets_):
                    raise _CredentialExposure("credential material appeared in a snapshot")
                if member.isdir():
                    content = b""
                    is_directory = True
                elif member.isreg():
                    if member.size < 0 or getattr(member, "sparse", None):
                        raise IsolationError("snapshot contains a sparse file")
                    total_bytes += member.size
                    if total_bytes > max_bytes:
                        raise IsolationError("snapshot exceeds its byte limit")
                    source = archive.extractfile(member)
                    if source is None:
                        raise IsolationError("snapshot file content is unavailable")
                    content = source.read(member.size + 1)
                    if len(content) != member.size:
                        raise IsolationError("snapshot file size is inconsistent")
                    if _contains_secret(content, secrets_):
                        raise _CredentialExposure("credential material appeared in a snapshot")
                    is_directory = False
                else:
                    raise IsolationError("snapshot contains a link or special file")
                accepted.append((normalized, is_directory, content))
    except (tarfile.TarError, OSError) as error:
        raise IsolationError("snapshot archive is malformed") from error
    return accepted


def _reject_agent_control_entries(
    entries: Sequence[Tuple[str, bool, bytes]], *, root: str
) -> None:
    """Keep resume state from introducing a new skill instruction layer."""

    for relative, _is_directory, _content in entries:
        parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
        if root == CONTAINER_WORKSPACE and len(parts) >= 2 and parts[:2] in (
            (".agents", "skills"),
            (".codex", "skills"),
        ):
            raise IsolationError("workspace snapshot contains an agent skill directory")
        if root == CONTAINER_CODEX_HOME and parts and parts[0] == "skills":
            raise IsolationError("Codex state snapshot contains an agent skill directory")
        if root == CONTAINER_CODEX_HOME and parts and parts[-1] in {
            "auth.json",
            "credentials.json",
            ".credentials.json",
        }:
            raise IsolationError("Codex state snapshot contains an authentication file")


def _validated_snapshot_archive(
    archive_bytes: bytes,
    *,
    root: str,
    max_bytes: int,
    secrets_: Sequence[bytearray],
) -> List[Tuple[str, bool, bytes]]:
    entries = _validated_archive_entries(
        archive_bytes, max_bytes=max_bytes, secrets_=secrets_
    )
    _reject_agent_control_entries(entries, root=root)
    return entries


def _mkdir_beneath(root: Path, relative: str) -> None:
    current = root
    parent = PurePosixPath(relative).parent
    if str(parent) == ".":
        return
    for part in parent.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            result = current.lstat()
            if not stat.S_ISDIR(result.st_mode) or current.is_symlink():
                raise IsolationError("snapshot directory collides with a file")


def _replace_snapshot(
    target: Path,
    archive_bytes: bytes,
    *,
    max_bytes: int,
    secrets_: Sequence[bytearray],
) -> Dict[str, Any]:
    entries = _validated_archive_entries(
        archive_bytes, max_bytes=max_bytes, secrets_=secrets_
    )
    destination = _real_directory(target, "snapshot destination", writable=True)
    destination_stat = destination.lstat()
    parent = _real_directory(
        destination.parent, "snapshot destination parent", writable=True
    )
    parent_stat = parent.lstat()
    incoming = Path(tempfile.mkdtemp(prefix=".snapshot-incoming-", dir=str(parent)))
    incoming.chmod(0o700)
    backup = parent / (".snapshot-backup-" + secrets.token_hex(12))
    moved_old = False
    try:
        for relative, is_directory, content in entries:
            path = incoming / relative
            if is_directory:
                path.mkdir(mode=0o700, parents=True, exist_ok=False)
                continue
            _mkdir_beneath(incoming, relative)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise IsolationError("short snapshot write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        current_parent = _real_directory(
            parent, "snapshot destination parent", writable=True
        ).lstat()
        current_destination = _real_directory(
            destination, "snapshot destination", writable=True
        ).lstat()
        if (current_parent.st_dev, current_parent.st_ino) != (
            parent_stat.st_dev,
            parent_stat.st_ino,
        ) or (
            current_destination.st_dev,
            current_destination.st_ino,
            current_destination.st_mtime_ns,
        ) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
            destination_stat.st_mtime_ns,
        ):
            raise IsolationError("snapshot destination changed before atomic replacement")
        os.replace(destination, backup)
        moved_old = True
        try:
            os.replace(incoming, destination)
        except BaseException:
            os.replace(backup, destination)
            moved_old = False
            raise
        moved_old = False
        shutil.rmtree(backup)
    finally:
        if incoming.exists() and not incoming.is_symlink():
            shutil.rmtree(incoming)
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    return {
        "archive_bytes": len(archive_bytes),
        "entry_count": len(entries),
        "sha256": digest,
    }


def _tmpfs(path: str, size: int, user: str) -> str:
    uid, gid = user.split(":", 1)
    return "{}:rw,nosuid,nodev,noexec,size={},nr_inodes={},mode=0700,uid={},gid={}".format(
        path, size, SNAPSHOT_INODE_LIMIT, uid, gid
    )


def _tmpfs_options(size: int, user: str) -> Dict[str, Optional[str]]:
    uid, gid = user.split(":", 1)
    return {
        "rw": None,
        "nosuid": None,
        "nodev": None,
        "noexec": None,
        "size": str(size),
        "nr_inodes": str(SNAPSHOT_INODE_LIMIT),
        "mode": "0700",
        "uid": uid,
        "gid": gid,
    }


def _parse_tmpfs_options(value: Any) -> Dict[str, Optional[str]]:
    if not isinstance(value, str) or not value:
        raise IsolationError("container tmpfs options are unavailable")
    parsed: Dict[str, Optional[str]] = {}
    for token in value.split(","):
        if not token:
            raise IsolationError("container tmpfs options are malformed")
        key, separator, option_value = token.partition("=")
        if not key or key in parsed or (separator and not option_value):
            raise IsolationError("container tmpfs options are malformed")
        parsed[key] = option_value if separator else None
    return parsed


def _verify_tmpfs_contract(
    value: Any,
    expected_sizes: Mapping[str, int],
    user: str,
    label: str,
) -> None:
    if not isinstance(value, Mapping) or set(value) != set(expected_sizes):
        raise IsolationError("{} tmpfs contract drifted".format(label))
    expected_by_path = {
        path: _tmpfs_options(size, user) for path, size in expected_sizes.items()
    }
    try:
        observed_by_path = {
            path: _parse_tmpfs_options(value[path]) for path in expected_sizes
        }
    except IsolationError as error:
        raise IsolationError("{} tmpfs contract drifted".format(label)) from error
    if observed_by_path != expected_by_path:
        raise IsolationError("{} tmpfs contract drifted".format(label))


def _resource_scope_label(runtime: Mapping[str, Any]) -> str:
    roots = runtime.get("roots")
    state = roots.get("state") if isinstance(roots, Mapping) else None
    path = state.get("path") if isinstance(state, Mapping) else None
    if not isinstance(path, str) or not path.startswith("/"):
        raise IsolationError("runtime state scope is unavailable")
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return "{}={}".format(RESOURCE_SCOPE_LABEL_KEY, digest)


def _bind_mount(source: Path, destination: str) -> str:
    selected = _real_directory(source, "read-only mount source")
    return (
        "type=bind,src={},dst={},readonly,bind-recursive=readonly,"
        "bind-propagation=rprivate"
    ).format(selected, destination)


def _numeric_user(value: Any, label: str) -> str:
    text = str(value)
    parts = text.split(":", 1)
    if (
        len(parts) != 2
        or not parts[0].isdigit()
        or not parts[1].isdigit()
        or int(parts[0]) == 0
        or int(parts[1]) == 0
    ):
        raise IsolationError("{} must declare a numeric non-root UID:GID".format(label))
    return text


def _agent_container_arguments(
    runtime: Mapping[str, Any],
    *,
    name: str,
    network: str,
    repo: Path,
    skill: Optional[Path],
    detached: bool = True,
) -> List[str]:
    images = runtime.get("images")
    if not isinstance(images, Mapping) or not isinstance(images.get("agent"), Mapping):
        raise IsolationError("runtime validation has no agent image")
    agent = images["agent"]
    user = _numeric_user(agent.get("user"), "agent image")
    seccomp = _runtime_trust(runtime)["seccomp_profile"]
    arguments = [
        "run",
        "--name",
        name,
        "--label",
        RESOURCE_LABEL,
        "--label",
        _resource_scope_label(runtime),
    ]
    if detached:
        arguments.append("--detach")
    arguments.extend(
        [
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--security-opt",
            "seccomp={}".format(seccomp["path"]),
            "--security-opt",
            "systempaths=unconfined",
            "--pids-limit",
            str(CONTAINER_PIDS_LIMIT),
            "--memory",
            CONTAINER_MEMORY,
            "--memory-swap",
            CONTAINER_MEMORY,
            "--cpus",
            CONTAINER_CPUS,
            "--user",
            user,
            "--workdir",
            CONTAINER_WORKSPACE,
            "--env",
            "HOME={}".format(CONTAINER_CODEX_HOME),
            "--env",
            "CODEX_HOME={}".format(CONTAINER_CODEX_HOME),
            "--env",
            "PATH={}".format(CONTAINER_RUNTIME_PATH),
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "DIAGRAM_GRILL_MAX_LIFETIME_SECONDS={}".format(
                AGENT_MAX_LIFETIME_SECONDS
            ),
            "--tmpfs",
            _tmpfs(CONTAINER_WORKSPACE, WORKSPACE_TMPFS_BYTES, user),
            "--tmpfs",
            _tmpfs(CONTAINER_CODEX_HOME, STATE_TMPFS_BYTES, user),
            "--tmpfs",
            _tmpfs("/tmp", TMP_LIMIT_BYTES, user),
            "--mount",
            _bind_mount(repo, CONTAINER_WORKSPACE + "/repo"),
        ]
    )
    if skill is not None:
        arguments.extend(("--mount", _bind_mount(skill, CONTAINER_SKILL)))
    arguments.append(str(agent["image_id"]))
    return arguments


def _proxy_create_arguments(
    runtime: Mapping[str, Any], *, name: str, network: str, model: str
) -> List[str]:
    images = runtime.get("images")
    if not isinstance(images, Mapping) or not isinstance(images.get("proxy"), Mapping):
        raise IsolationError("runtime validation has no proxy image")
    proxy = images["proxy"]
    user = _numeric_user(proxy.get("user"), "proxy image")
    return [
        "create",
        "--interactive",
        "--name",
        name,
        "--label",
        RESOURCE_LABEL,
        "--label",
        _resource_scope_label(runtime),
        "--network",
        network,
        "--network-alias",
        CONTAINER_PROXY_HOST,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(PROXY_PIDS_LIMIT),
        "--memory",
        PROXY_MEMORY,
        "--memory-swap",
        PROXY_MEMORY,
        "--cpus",
        PROXY_CPUS,
        "--user",
        user,
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--tmpfs",
        _tmpfs("/tmp", TMP_LIMIT_BYTES, user),
        str(proxy["image_id"]),
        "--host",
        CONTAINER_PROXY_HOST,
        "--port",
        str(CONTAINER_PROXY_PORT),
        "--model",
        model,
    ]


_ACTIVE_PROBE_PROGRAM = r"""
import importlib.metadata as metadata
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

def version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"

def write_must_fail(path):
    try:
        path.write_text("unsafe", encoding="utf-8")
    except OSError:
        return True
    return False

def tree_file_readable(path):
    try:
        candidate = next(item for item in path.rglob("*") if item.is_file())
        with candidate.open("rb") as source:
            source.read(1)
        return True
    except (OSError, StopIteration):
        return False

sentinel_path = Path("/tmp/outer-parent-sentinel")
try:
    sentinel = sentinel_path.read_bytes()
except OSError:
    sentinel = b""
sentinel_visible = False
visible_pids = []
for item in Path("/proc").iterdir():
    if not item.name.isdigit():
        continue
    visible_pids.append(int(item.name))
    try:
        if int(item.name) != os.getpid() and sentinel and sentinel in (item / "environ").read_bytes():
            sentinel_visible = True
    except OSError:
        pass

routes = []
try:
    for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[0] != "lo":
            routes.append({"interface": fields[0], "destination": fields[1]})
except OSError:
    routes.append("unreadable")

workspace = Path("/workspace")
output = workspace / ".isolation-probe-output"
output.write_text("ok", encoding="ascii")
repo_read = tree_file_readable(workspace / "repo")
repo_write_blocked = write_must_fail(workspace / "repo" / ".write-probe")
skill_expected = sys.argv[1] == "1"
skill = Path("/codex-home/skills/diagram-grill")
skill_read = tree_file_readable(skill) if skill_expected else not skill.exists()
skill_write_blocked = write_must_fail(skill / ".write-probe") if skill_expected else True
dot = subprocess.run(
    ["dot", "-V"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
)
sensitive_names = [
    name for name in os.environ
    if any(marker in name.upper() for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_JSON", "CREDENTIAL"))
]
checks = {
    "auth_absent": not Path("/codex-home/auth.json").exists(),
    "credential_env_absent": not sensitive_names,
    "fresh_proc": not sentinel_visible and len(visible_pids) <= 3,
    "graphviz": dot.returncode == 0,
    "network_isolated": not routes,
    "outer_tmp_hidden": not sentinel_path.exists(),
    "repo_readable": repo_read,
    "repo_read_only": repo_write_blocked,
    "skill_contract": skill_read and skill_write_blocked,
    "workspace_writable": output.read_text(encoding="ascii") == "ok",
}
output.unlink()
print(json.dumps({
    "ok": all(checks.values()),
    "checks": checks,
    "container_uid": os.getuid(),
    "versions": {
        "codex": "0.149.1",
        "diagrams": version("diagrams"),
        "graphviz_python": version("graphviz"),
        "jsonschema": version("jsonschema"),
        "python": platform.python_version(),
        "dot": dot.stdout.strip(),
    },
}, sort_keys=True))
"""

_CONFIG_PROBE_SERVER_PROGRAM = r"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

port = int(sys.argv[1])
if not 1024 <= port <= 65535:
    raise SystemExit(2)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdecimal():
            self.send_error(400)
            return
        length = int(raw_length)
        if length > 2 * 1024 * 1024:
            self.send_error(413)
            return
        self.rfile.read(length)
        body = b'{"error":{"message":"nonbillable config probe","type":"invalid_request_error"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        pass

HTTPServer(("127.0.0.1", port), Handler).serve_forever(poll_interval=0.1)
"""

_CONFIG_PROBE_READY_PROGRAM = r"""
import socket
import sys

connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])), 0.2)
connection.close()
"""

_SKILL_DISCOVERY_PROBE_PROGRAM = r"""
import json
import selectors
import subprocess
import sys
import time

expected_name, expected_path, cwd = sys.argv[1:]
environment = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/codex-home",
    "CODEX_HOME": "/codex-home",
    "LANG": "C",
    "LC_ALL": "C",
}
process = subprocess.Popen(
    ["codex", "app-server", "--listen", "stdio://"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    encoding="utf-8",
    errors="strict",
    bufsize=1,
    env=environment,
)
selector = selectors.DefaultSelector()
selector.register(process.stdout, selectors.EVENT_READ)

def send(value):
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()

def receive(response_id):
    deadline = time.monotonic() + 10
    total = 0
    while time.monotonic() < deadline:
        events = selector.select(deadline - time.monotonic())
        if not events:
            break
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server closed before discovery completed")
        total += len(line.encode("utf-8"))
        if total > 2 * 1024 * 1024:
            raise RuntimeError("Codex app-server discovery output exceeded its bound")
        value = json.loads(line)
        if value.get("id") == response_id:
            return value
    raise RuntimeError("Codex app-server discovery timed out")

try:
    send({
        "method": "initialize",
        "id": 1,
        "params": {
            "clientInfo": {
                "name": "diagram_grill_eval",
                "title": "Diagram Grill Eval",
                "version": "0.1.0",
            },
            "capabilities": {},
        },
    })
    receive(1)
    send({"method": "initialized", "params": {}})
    send({
        "method": "skills/list",
        "id": 2,
        "params": {"cwds": [cwd], "forceReload": True},
    })
    response = receive(2)
finally:
    try:
        process.stdin.close()
    except OSError:
        pass
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)

result = response.get("result", {})
data = result.get("data", [])
matches = []
for entry in data if isinstance(data, list) else []:
    if not isinstance(entry, dict) or entry.get("cwd") != cwd:
        continue
    errors = entry.get("errors", [])
    if errors:
        raise RuntimeError("Codex reported a skill discovery error")
    for skill in entry.get("skills", []):
        if (
            isinstance(skill, dict)
            and skill.get("name") == expected_name
            and skill.get("path") == expected_path
            and skill.get("enabled") is True
        ):
            matches.append(skill)
if len(matches) != 1:
    raise RuntimeError("Codex did not discover exactly one mounted treatment skill")
print(json.dumps({
    "ok": True,
    "name": expected_name,
    "path": expected_path,
    "description": matches[0].get("description"),
}, sort_keys=True))
"""


_IMPORT_BRIDGE_PROGRAM = r"""
import importlib.util
import sys
from pathlib import Path

source_path, target, max_entries, max_bytes, archive_limit = sys.argv[1:]
spec = importlib.util.spec_from_file_location("snapshot_import_runtime", source_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
# snapshot_import.py owns the one bounded seekable buffer.  Adding another
# transport copy here would let a near-limit archive consume two /tmp quotas.
module.import_snapshot(
    Path(target), sys.stdin.buffer,
    max_entries=int(max_entries), max_bytes=int(max_bytes)
)
"""


_CREATE_STAGING_PROGRAM = r"""
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
staging = root / ".snapshot-import"
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(root, flags)
try:
    os.mkdir(staging, 0o700, dir_fd=descriptor)
finally:
    os.close(descriptor)
"""


_PROMOTE_STAGING_PROGRAM = r"""
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
forbidden = set(sys.argv[2:])
staging = root / ".snapshot-import"
if staging.is_symlink() or not staging.is_dir():
    raise RuntimeError("snapshot staging directory is unavailable")
for child in sorted(os.scandir(staging), key=lambda item: item.name):
    if child.name in forbidden:
        raise RuntimeError("snapshot collides with a protected mount")
    result = child.stat(follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise RuntimeError("snapshot staging contains a symlink")
    destination = root / child.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("snapshot collides with existing container state")
    os.replace(child.path, destination)
staging.rmdir()
"""


_READ_LAST_MESSAGE_PROGRAM = r"""
import os
import stat
import sys

path = sys.argv[1]
limit = int(sys.argv[2])
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    result = os.fstat(descriptor)
    if not stat.S_ISREG(result.st_mode) or result.st_size > limit:
        raise RuntimeError("last message is not a bounded regular file")
    data = os.read(descriptor, limit + 1)
    if len(data) != result.st_size:
        raise RuntimeError("last message changed while reading")
finally:
    os.close(descriptor)
sys.stdout.buffer.write(data)
"""


_SNAPSHOT_PREFLIGHT_PROGRAM = r"""
import json
import os
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1])
max_entries = int(sys.argv[2])
max_bytes = int(sys.argv[3])
excludes = set(sys.argv[4:])
root_stat = root.lstat()
if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
    raise RuntimeError("snapshot root is not a real directory")
visible = {1, os.getpid()}
unexpected = [
    int(item.name) for item in Path("/proc").iterdir()
    if item.name.isdigit() and int(item.name) not in visible
]
if unexpected:
    raise RuntimeError("snapshot container is not quiescent")
pending = [(root, "")]
seen = set()
entries = 0
total = 0
while pending:
    directory, prefix = pending.pop()
    for child in sorted(os.scandir(directory), key=lambda item: item.name, reverse=True):
        relative = (prefix + "/" + child.name).lstrip("/")
        if relative.split("/", 1)[0] in excludes:
            continue
        pure = PurePosixPath(relative)
        if (
            "\\" in relative
            or any(ord(character) < 32 or ord(character) == 127 for character in relative)
            or pure.is_absolute()
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise RuntimeError("snapshot contains an unsafe name")
        normalized = "/".join(unicodedata.normalize("NFC", part) for part in pure.parts)
        folded = normalized.casefold()
        if folded in seen:
            raise RuntimeError("snapshot contains a Unicode or casefold duplicate")
        seen.add(folded)
        result = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(result.st_mode):
            pending.append((Path(child.path), relative))
        elif stat.S_ISREG(result.st_mode):
            if result.st_nlink != 1:
                raise RuntimeError("snapshot contains a hard link")
            total += result.st_size
            if total > max_bytes:
                raise RuntimeError("snapshot exceeds its byte limit")
        else:
            raise RuntimeError("snapshot contains a link or special file")
        entries += 1
        if entries > max_entries:
            raise RuntimeError("snapshot exceeds its entry limit")
print(json.dumps({"entries": entries, "payload_bytes": total}, sort_keys=True))
"""


_PROXY_HEALTH_PROGRAM = r"""
import http.client
import sys
connection = http.client.HTTPConnection(sys.argv[1], 8080, timeout=1)
connection.request("GET", "/healthz", headers={"Connection": "close"})
response = connection.getresponse()
body = response.read(1024)
connection.close()
if response.status != 200 or body != b'{"ok":true}':
    raise SystemExit(1)
"""


_PROXY_ROUTE_PROGRAM = r"""
import json
import socket
import struct
from pathlib import Path

routes = []
for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
    fields = line.split()
    if len(fields) < 4 or fields[0] == "lo":
        continue
    gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    routes.append({
        "destination": fields[1],
        "gateway": gateway,
        "interface": fields[0],
        "up": bool(int(fields[3], 16) & 1),
    })
print(json.dumps(routes, sort_keys=True))
"""


_AGENT_NETWORK_PROGRAM = r"""
import json
import socket
from pathlib import Path

defaults = []
for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
    fields = line.split()
    if len(fields) >= 4 and fields[0] != "lo" and fields[1] == "00000000":
        defaults.append(fields[0])

def unresolved(name):
    try:
        socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True
    return False

checks = {
    "no_default_route": not defaults,
    "external_dns_blocked": unresolved("api.openai.com"),
    "host_alias_blocked": unresolved("host.docker.internal"),
}
print(json.dumps({"ok": all(checks.values()), "checks": checks}, sort_keys=True))
"""


_PROXY_LISTEN_PROGRAM = r"""
import json
import socket
import struct
import sys
from pathlib import Path

port = int(sys.argv[1])
listeners = []
for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
    for line in path.read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        address_hex, port_hex = fields[1].split(":", 1)
        if int(port_hex, 16) != port:
            continue
        if len(address_hex) == 8:
            address = socket.inet_ntoa(struct.pack("<L", int(address_hex, 16)))
        else:
            chunks = [address_hex[index:index + 8] for index in range(0, 32, 8)]
            packed = b"".join(struct.pack("<L", int(chunk, 16)) for chunk in chunks)
            address = socket.inet_ntop(socket.AF_INET6, packed)
        listeners.append(address)
print(json.dumps(sorted(listeners)))
"""


_MOUNT_MANIFEST_PROGRAM = r"""
import hashlib
import json
import os
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve(strict=True)
max_entries = int(sys.argv[2])
max_bytes = int(sys.argv[3])
if root.is_symlink() or not root.is_dir():
    raise RuntimeError("mount root is not a real directory")
pending = [(root, "")]
seen = set()
items = []
total = 0
while pending:
    directory, prefix = pending.pop()
    for child in sorted(os.scandir(directory), key=lambda item: item.name, reverse=True):
        relative = (prefix + "/" + child.name).lstrip("/")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            raise RuntimeError("mount member escapes its root")
        normalized = "/".join(unicodedata.normalize("NFC", part) for part in pure.parts)
        folded = normalized.casefold()
        if folded in seen:
            raise RuntimeError("mount contains a casefold duplicate")
        seen.add(folded)
        before = child.stat(follow_symlinks=False)
        path = Path(child.path)
        if stat.S_ISDIR(before.st_mode):
            items.append([normalized, "directory", 0, ""])
            pending.append((path, relative))
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            total += before.st_size
            if total > max_bytes:
                raise RuntimeError("mount exceeds its byte limit")
            digest = hashlib.sha256()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                observed = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or observed != expected:
                    raise RuntimeError("mount file changed while hashing")
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            finally:
                os.close(descriptor)
            after = path.stat(follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != expected:
                raise RuntimeError("mount file changed after hashing")
            items.append([normalized, "file", before.st_size, digest.hexdigest()])
        else:
            raise RuntimeError("mount contains a link, hard link, or special file")
        if len(items) > max_entries:
            raise RuntimeError("mount exceeds its entry limit")
items.sort(key=lambda item: item[0])
encoded = json.dumps(items, separators=(",", ":"), ensure_ascii=True).encode("ascii")
print(json.dumps({"manifest_sha256": hashlib.sha256(encoded).hexdigest(), "entries": len(items), "bytes": total}, sort_keys=True))
"""


def _image_metadata(
    runtime: Mapping[str, Any], image_id: str, kind: str
) -> Dict[str, Any]:
    capture = _docker_capture(
        runtime, ["image", "inspect", image_id], stdout_limit=2 * 1024 * 1024
    )
    try:
        values = json.loads(capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("Docker image inspection returned malformed JSON") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise IsolationError("Docker image inspection returned an unexpected result")
    value = values[0]
    if value.get("Id") != image_id:
        raise IsolationError("Docker resolved a different immutable image")
    config = value.get("Config")
    if not isinstance(config, dict):
        raise IsolationError("Docker image has no immutable configuration")
    user = _numeric_user(config.get("User"), "{} image".format(kind))
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise IsolationError("{} image has no runtime labels".format(kind))
    entrypoint = config.get("Entrypoint")
    expected_entrypoint = AGENT_ENTRYPOINT if kind == "agent" else PROXY_ENTRYPOINT
    expected_contract = (
        AGENT_RUNTIME_CONTRACT if kind == "agent" else PROXY_RUNTIME_CONTRACT
    )
    if entrypoint != expected_entrypoint:
        raise IsolationError("{} image entrypoint is not pinned".format(kind))
    if labels.get("io.diagram-grill.runtime-contract") != expected_contract:
        raise IsolationError("{} image runtime contract label is invalid".format(kind))
    if kind == "agent" and labels.get("io.diagram-grill.codex-version") != CODEX_CLI_VERSION:
        raise IsolationError("agent image Codex version label is invalid")
    environment = config.get("Env", [])
    if not isinstance(environment, list) or any(
        _SENSITIVE_ENV.search(str(item).split("=", 1)[0]) for item in environment
    ):
        raise IsolationError("{} image contains a sensitive environment variable".format(kind))
    repo_digests = value.get("RepoDigests", [])
    return {
        "image_id": image_id,
        "repo_digests": sorted(str(item) for item in repo_digests)
        if isinstance(repo_digests, list)
        else [],
        "user": user,
        "entrypoint": list(entrypoint),
        "runtime_contract": expected_contract,
        "labels": {str(key): str(item) for key, item in sorted(labels.items())},
    }


def _resource_name(run_id: str, turn: int, role: str, nonce: str) -> str:
    safe = _CONTAINER_NAME_PART.sub("-", run_id.lower()).strip("-._") or "run"
    suffix = "-t{:03d}-{}-{}".format(turn, nonce, role)
    prefix = "diagram-grill-eval-"
    return prefix + safe[: 63 - len(prefix) - len(suffix)] + suffix


def _codex_configuration(model: str, base_url: str, capability: str) -> List[str]:
    if re.fullmatch(r"[0-9a-f]{64}", capability) is None:
        raise IsolationError("per-turn proxy capability is invalid")
    values = (
        ("model_provider", json.dumps("eval_proxy")),
        ("model_providers.eval_proxy.name", json.dumps("Evaluation proxy")),
        ("model_providers.eval_proxy.base_url", json.dumps(base_url)),
        ("model_providers.eval_proxy.wire_api", json.dumps("responses")),
        ("model_providers.eval_proxy.requires_openai_auth", "false"),
        ("model_providers.eval_proxy.supports_websockets", "false"),
        ("model_providers.eval_proxy.supports_standalone_web_search", "false"),
        ("model_providers.eval_proxy.request_max_retries", "0"),
        ("model_providers.eval_proxy.stream_max_retries", "0"),
        (
            "model_providers.eval_proxy.http_headers",
            '{{{}={}}}'.format(json.dumps(CAPABILITY_HEADER), json.dumps(capability)),
        ),
        ("model_reasoning_effort", json.dumps("medium")),
        ("service_tier", json.dumps("default")),
        ("approval_policy", json.dumps("never")),
        ("sandbox_mode", json.dumps("workspace-write")),
        ("sandbox_workspace_write.network_access", "false"),
        ("shell_environment_policy.inherit", json.dumps("none")),
        (
            "shell_environment_policy.set",
            '{PATH="/usr/local/bin:/usr/bin:/bin",HOME="/codex-home",CODEX_HOME="/codex-home",LANG="C",LC_ALL="C"}',
        ),
        ("project_doc_fallback_filenames", "[]"),
        ("check_for_update_on_startup", "false"),
        ("web_search", json.dumps("disabled")),
    )
    arguments: List[str] = []
    for key, value in values:
        arguments.extend(("--config", "{}={}".format(key, value)))
    for feature in _DISABLED_FEATURES:
        arguments.extend(("--disable", feature))
    arguments.extend(("--model", model))
    return arguments


def _codex_exec_arguments(
    *,
    model: str,
    base_url: str,
    capability: str,
    last_message: str,
    initial: bool,
    session_id: Optional[str],
) -> List[str]:
    common = [
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--json",
        "--output-last-message",
        last_message,
        *_codex_configuration(model, base_url, capability),
    ]
    if initial:
        return ["exec", *common, "-"]
    if session_id is None or _THREAD_ID.fullmatch(session_id) is None:
        raise IsolationError("resume requires a pinned Codex session ID")
    return ["exec", "resume", *common, session_id, "-"]


def _extract_session_id(output: bytes) -> Optional[str]:
    observed = set()
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        candidate = value.get("thread_id")
        if isinstance(candidate, str) and _THREAD_ID.fullmatch(candidate):
            observed.add(candidate.lower())
    if len(observed) > 1:
        raise IsolationError("Codex output changed session IDs within one turn")
    return next(iter(observed), None)


def _parse_probe_output(value: bytes) -> Dict[str, Any]:
    for line in reversed(value.splitlines()):
        try:
            parsed = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            if parsed.get("ok") is not True:
                checks = parsed.get("checks")
                failed = sorted(
                    str(name)
                    for name, passed in checks.items()
                    if passed is not True
                ) if isinstance(checks, Mapping) else []
                suffix = ": {}".format(", ".join(failed)) if failed else ""
                raise IsolationError(
                    "nested bwrap active probe reported a failed check{}".format(suffix)
                )
            return parsed
    raise IsolationError("nested bwrap active probe returned no result")


def _remove_container(
    runtime: Mapping[str, Any], name: str
) -> Dict[str, Any]:
    try:
        capture = _docker_capture(
            runtime, ["rm", "-f", name], timeout=30, check=False
        )
    except _TerminationRequested:
        raise
    except IsolationError as error:
        return {"attempted": True, "succeeded": False, "error": str(error)}
    diagnostic = capture.stderr.decode("utf-8", "replace")
    absent = capture.returncode != 0 and "No such container" in diagnostic
    return {
        "attempted": True,
        "succeeded": capture.returncode == 0 or absent,
        "exit_code": capture.returncode,
        "container_absent": absent,
    }


def _remove_network(runtime: Mapping[str, Any], name: str) -> Dict[str, Any]:
    try:
        capture = _docker_capture(runtime, ["network", "rm", name], timeout=30, check=False)
    except _TerminationRequested:
        raise
    except IsolationError as error:
        return {"attempted": True, "succeeded": False, "error": str(error)}
    diagnostic = capture.stderr.decode("utf-8", "replace")
    absent = capture.returncode != 0 and "No such network" in diagnostic
    return {
        "attempted": True,
        "succeeded": capture.returncode == 0 or absent,
        "exit_code": capture.returncode,
        "network_absent": absent,
    }


def _acquire_evaluation_lock(state_root: Path) -> int:
    root = _private_directory(state_root, "state root", writable=True)
    path = root / ".diagram-grill-eval.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        result = os.fstat(descriptor)
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_uid != expected_uid
            or stat.S_IMODE(result.st_mode) & 0o077
        ):
            raise IsolationError("evaluation lock is not a private regular file")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _raise_if_termination_requested()
                time.sleep(0.1)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_evaluation_lock(descriptor: Optional[int]) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _labeled_resource_ids(
    runtime: Mapping[str, Any], kind: str
) -> List[str]:
    scope = _resource_scope_label(runtime)
    filters = ["--filter", "label=" + RESOURCE_LABEL, "--filter", "label=" + scope]
    if kind == "container":
        arguments = ["ps", "--all", "--quiet", *filters]
    elif kind == "network":
        arguments = [
            "network",
            "ls",
            "--quiet",
            *filters,
        ]
    else:
        raise AssertionError("unknown Docker resource kind")
    capture = _docker_capture(runtime, arguments, timeout=30)
    values = [line.strip() for line in capture.stdout.decode("ascii", "strict").splitlines() if line.strip()]
    if any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in values):
        raise IsolationError("Docker returned an invalid labeled resource ID")
    return values


def _scavenge_resources(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    removed = {"containers": [], "networks": []}
    deferred = _defer_termination()
    cleanup_termination: Optional[_TerminationRequested] = None
    cleanup_error: Optional[IsolationError] = None
    try:
        containers = _labeled_resource_ids(runtime, "container")
        networks = _labeled_resource_ids(runtime, "network")
        for identifier in containers:
            result, pending = _attempt_cleanup(
                lambda identifier=identifier: _remove_container(runtime, identifier),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
            if result.get("succeeded") is not True:
                cleanup_error = IsolationError(
                    "stale evaluator container could not be removed"
                )
            else:
                removed["containers"].append(identifier)
        for identifier in networks:
            result, pending = _attempt_cleanup(
                lambda identifier=identifier: _remove_network(runtime, identifier),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
            if result.get("succeeded") is not True:
                cleanup_error = cleanup_error or IsolationError(
                    "stale evaluator network could not be removed"
                )
            else:
                removed["networks"].append(identifier)
    finally:
        try:
            _resume_termination(deferred)
        except _TerminationRequested as error:
            cleanup_termination = cleanup_termination or error
    if cleanup_termination is not None:
        raise cleanup_termination
    if cleanup_error is not None:
        raise cleanup_error
    return removed


def _install_termination_handlers() -> _TerminationState:
    global _TERMINATION_STATE
    if threading.current_thread() is not threading.main_thread():
        raise IsolationError("evaluation turns must run on the main thread")
    if _TERMINATION_STATE is not None:
        raise IsolationError("evaluation termination handlers are already installed")
    previous: Dict[int, Any] = {}
    state = _TerminationState(previous=previous)

    def terminate(signum: int, _frame: object) -> None:
        if state.requested_signum is None:
            state.requested_signum = signum

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    _TERMINATION_STATE = state
    return state


def _restore_termination_handlers(state: _TerminationState) -> None:
    global _TERMINATION_STATE
    if _TERMINATION_STATE is not state:
        raise IsolationError("evaluation termination handler state drifted")
    for signum, handler in state.previous.items():
        signal.signal(signum, handler)
    _TERMINATION_STATE = None


def _defer_termination() -> Optional[_TerminationState]:
    state = _TERMINATION_STATE
    if state is not None:
        state.deferred_depth += 1
    return state


def _raise_if_termination_requested() -> None:
    state = _TERMINATION_STATE
    if (
        state is not None
        and state.deferred_depth == 0
        and state.requested_signum is not None
    ):
        raise _TerminationRequested(
            "evaluation interrupted by signal {}".format(state.requested_signum)
        )


def _resume_termination(state: Optional[_TerminationState]) -> None:
    signum = _end_termination_deferral(state)
    if signum is not None:
        raise _TerminationRequested(
            "evaluation interrupted by signal {}".format(signum)
        )


def _end_termination_deferral(state: Optional[_TerminationState]) -> Optional[int]:
    if state is None:
        return None
    if _TERMINATION_STATE is not state or state.deferred_depth <= 0:
        raise IsolationError("evaluation termination deferral state drifted")
    state.deferred_depth -= 1
    if state.deferred_depth == 0 and state.requested_signum is not None:
        return state.requested_signum
    return None


def _attempt_cleanup(action: Any, failure: Any) -> Tuple[Any, Optional[_TerminationRequested]]:
    pending: Optional[_TerminationRequested] = None
    for _attempt in range(2):
        try:
            return action(), pending
        except _TerminationRequested as error:
            pending = pending or error
        except BaseException:
            return failure, pending
    return failure, pending


def _credential_envelope(
    credential: bytearray, sentinel: bytearray, capability: bytearray
) -> bytearray:
    def append_string(target: bytearray, value: bytearray) -> None:
        target.append(ord('"'))
        for byte in value:
            if byte in (ord('"'), ord("\\")):
                target.append(ord("\\"))
                target.append(byte)
            elif byte == 8:
                target.extend(b"\\b")
            elif byte == 9:
                target.extend(b"\\t")
            elif byte == 10:
                target.extend(b"\\n")
            elif byte == 12:
                target.extend(b"\\f")
            elif byte == 13:
                target.extend(b"\\r")
            elif 32 <= byte <= 126:
                target.append(byte)
            else:
                raise IsolationError("credential envelope contains non-ASCII data")
        target.append(ord('"'))

    output = bytearray(b'{"api_key":')
    append_string(output, credential)
    output.extend(b',"sentinel":')
    append_string(output, sentinel)
    output.extend(b',"capability":')
    append_string(output, capability)
    output.extend(b"}\n")
    return output


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _create_network(
    runtime: Mapping[str, Any], name: str, *, internal: bool
) -> Dict[str, Any]:
    arguments = [
        "network",
        "create",
        "--driver",
        "bridge",
        "--label",
        RESOURCE_LABEL,
        "--label",
        _resource_scope_label(runtime),
    ]
    if internal:
        arguments.extend(
            (
                "--internal",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv6=isolated",
            )
        )
    arguments.append(name)
    capture = _docker_capture(runtime, arguments, timeout=30)
    identifier = capture.stdout.decode("ascii", "strict").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise IsolationError("Docker returned an invalid network ID")
    return {"name": name, "id": identifier, "internal": internal}


def _inspect_one(
    runtime: Mapping[str, Any], kind: str, name: str
) -> Dict[str, Any]:
    capture = _docker_capture(
        runtime,
        [kind, "inspect", name],
        timeout=30,
        stdout_limit=2 * 1024 * 1024,
    )
    try:
        values = json.loads(capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("Docker inspection returned malformed JSON") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise IsolationError("Docker inspection returned an unexpected result")
    return values[0]


def _start_proxy(
    runtime: Mapping[str, Any],
    *,
    name: str,
    egress_network: str,
    internal_network: str,
    model: str,
    credential: bytearray,
    sentinel: bytearray,
    capability: bytearray,
) -> ManagedProcess:
    _docker_capture(
        runtime,
        _proxy_create_arguments(
            runtime, name=name, network=internal_network, model=model
        ),
        timeout=30,
    )
    _docker_capture(
        runtime,
        [
            "network",
            "connect",
            egress_network,
            name,
        ],
        timeout=30,
    )
    envelope = _credential_envelope(credential, sentinel, capability)
    managed: Optional[ManagedProcess] = None
    try:
        managed = _docker_start(
            runtime,
            ["start", "--attach", "--interactive", name],
            input_data=envelope,
            stdout_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
            stderr_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
        )
        _wait_for_input(managed, PROXY_START_TIMEOUT_SECONDS)
        deadline = time.monotonic() + PROXY_START_TIMEOUT_SECONDS
        while True:
            health = _docker_capture(
                runtime,
                [
                    "exec",
                    name,
                    "python3",
                    "-I",
                    "-S",
                    "-c",
                    _PROXY_HEALTH_PROGRAM,
                    CONTAINER_PROXY_HOST,
                ],
                timeout=3,
                check=False,
            )
            if health.returncode == 0 and not health.timed_out and health.limited_stream is None:
                return managed
            if managed.process.poll() is not None:
                raise IsolationError("credential proxy exited before becoming healthy")
            if time.monotonic() >= deadline:
                raise IsolationError("credential proxy health check timed out")
            time.sleep(0.1)
    except BaseException:
        if managed is not None:
            _finish_process(managed, 5, terminate=True)
        raise
    finally:
        _zero(envelope)


def _network_gateway(value: Mapping[str, Any]) -> Optional[str]:
    ipam = value.get("IPAM")
    if not isinstance(ipam, Mapping) or not isinstance(ipam.get("Config"), list):
        return None
    gateways = [
        item.get("Gateway")
        for item in ipam["Config"]
        if isinstance(item, Mapping) and isinstance(item.get("Gateway"), str)
    ]
    return gateways[0] if len(gateways) == 1 else None


def _canonical_security_option(value: str) -> str:
    if value in {
        "no-new-privileges",
        "no-new-privileges=true",
        "no-new-privileges:true",
    }:
        return "no-new-privileges=true"
    return value


def _seccomp_option_is_pinned(value: str, expected_path: str) -> bool:
    if value == "seccomp={}".format(expected_path):
        return True
    prefix = "seccomp="
    if not value.startswith(prefix):
        return False
    try:
        profile = json.loads(value[len(prefix) :])
    except json.JSONDecodeError:
        return False
    canonical = json.dumps(
        profile, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest() == SECCOMP_PROFILE_CANONICAL_SHA256


def _verify_container_hardening(
    runtime: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    role: str,
    network: str,
) -> None:
    host = value.get("HostConfig")
    config = value.get("Config")
    if not isinstance(host, Mapping) or not isinstance(config, Mapping):
        raise IsolationError("{} container inspection is incomplete".format(role))
    expected_image = runtime["images"][role]
    expected_user = _numeric_user(expected_image.get("user"), "{} image".format(role))
    if config.get("User") != expected_user:
        raise IsolationError("{} container user drifted".format(role))
    labels = config.get("Labels")
    scope_key, scope_value = _resource_scope_label(runtime).split("=", 1)
    if (
        not isinstance(labels, Mapping)
        or labels.get(RESOURCE_LABEL_KEY) != RESOURCE_LABEL_VALUE
        or labels.get(scope_key) != scope_value
    ):
        raise IsolationError("{} container management label drifted".format(role))
    environment = config.get("Env", [])
    if not isinstance(environment, list) or any(
        _SENSITIVE_ENV.search(str(item).split("=", 1)[0]) for item in environment
    ):
        raise IsolationError("{} container environment is sensitive".format(role))
    security_options = host.get("SecurityOpt", [])
    seccomp_path = str(_runtime_trust(runtime)["seccomp_profile"]["path"])
    if not isinstance(security_options, list) or any(
        not isinstance(item, str) for item in security_options
    ):
        raise IsolationError("{} container security options drifted".format(role))
    normalized_security_options = {
        "seccomp=pinned"
        if _seccomp_option_is_pinned(item, seccomp_path)
        else _canonical_security_option(item)
        for item in security_options
        if isinstance(item, str)
    }
    expected_security_options = {
        "agent": {"no-new-privileges=true", "seccomp=pinned"},
        "proxy": {"no-new-privileges=true"},
    }[role]
    if (
        len(security_options) != len(expected_security_options)
        or normalized_security_options != expected_security_options
    ):
        raise IsolationError("{} container security options drifted".format(role))
    if role == "agent" and (
        host.get("MaskedPaths") != [] or host.get("ReadonlyPaths") != []
    ):
        raise IsolationError("agent container system path policy drifted")
    expected = {
        "agent": {
            "memory": 2 * 1024 * 1024 * 1024,
            "nano_cpus": 2_000_000_000,
            "pids": CONTAINER_PIDS_LIMIT,
        },
        "proxy": {
            "memory": 256 * 1024 * 1024,
            "nano_cpus": 1_000_000_000,
            "pids": PROXY_PIDS_LIMIT,
        },
    }[role]
    if (
        host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != network
        or host.get("Memory") != expected["memory"]
        or host.get("MemorySwap") != expected["memory"]
        or host.get("NanoCpus") != expected["nano_cpus"]
        or host.get("PidsLimit") != expected["pids"]
        or host.get("CapDrop") != ["ALL"]
        or host.get("CapAdd") not in (None, [])
        or host.get("PidMode") not in (None, "")
        or host.get("IpcMode") not in (None, "private")
    ):
        raise IsolationError("{} container hardening settings drifted".format(role))
    if role == "agent":
        _verify_tmpfs_contract(
            host.get("Tmpfs"),
            {
                CONTAINER_WORKSPACE: WORKSPACE_TMPFS_BYTES,
                CONTAINER_CODEX_HOME: STATE_TMPFS_BYTES,
                "/tmp": TMP_LIMIT_BYTES,
            },
            expected_user,
            "agent container",
        )
    else:
        _verify_tmpfs_contract(
            host.get("Tmpfs"),
            {"/tmp": TMP_LIMIT_BYTES},
            expected_user,
            "proxy container",
        )


def _boundary_probe(
    runtime: Mapping[str, Any], repo: Path, skill: Path, probe_id: str
) -> Dict[str, Any]:
    nonce = secrets.token_hex(6)
    internal_name = _resource_name(probe_id, 0, "probe-int", nonce)
    egress_name = _resource_name(probe_id, 0, "probe-eg", nonce)
    proxy_name = _resource_name(probe_id, 0, "probe-proxy", nonce)
    agent_name = _resource_name(probe_id, 0, "probe-agent", nonce)
    fake_credential = bytearray(b"diagram-grill-nonbillable-probe-key")
    sentinel = bytearray(secrets.token_hex(32).encode("ascii"))
    capability = bytearray(secrets.token_hex(32).encode("ascii"))
    proxy_process: Optional[ManagedProcess] = None
    teardown: Dict[str, Any] = {}
    result: Dict[str, Any] = {}
    try:
        _create_network(runtime, egress_name, internal=False)
        _create_network(runtime, internal_name, internal=True)
        proxy_process = _start_proxy(
            runtime,
            name=proxy_name,
            egress_network=egress_name,
            internal_network=internal_name,
            model="gpt-5.4",
            credential=fake_credential,
            sentinel=sentinel,
            capability=capability,
        )
        _docker_capture(
            runtime,
            _agent_container_arguments(
                runtime,
                name=agent_name,
                network=internal_name,
                repo=repo,
                skill=skill,
                detached=True,
            ),
            timeout=60,
        )
        agent_health = _docker_capture(
            runtime,
            [
                "exec",
                "--user",
                str(runtime["images"]["agent"]["user"]),
                agent_name,
                "python3",
                "-I",
                "-S",
                "-c",
                _PROXY_HEALTH_PROGRAM,
                CONTAINER_PROXY_HOST,
            ],
            timeout=10,
        )
        if agent_health.stdout or agent_health.stderr:
            raise IsolationError("agent-to-proxy health probe emitted unexpected output")

        egress = _inspect_one(runtime, "network", egress_name)
        internal = _inspect_one(runtime, "network", internal_name)
        if egress.get("Internal") is not False or internal.get("Internal") is not True:
            raise IsolationError("Docker network isolation metadata is inconsistent")
        egress_gateway = _network_gateway(egress)
        isolated_options = {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
            "com.docker.network.enable_ipv4": "true",
            "com.docker.network.enable_ipv6": "false",
        }
        scope_key, scope_value = _resource_scope_label(runtime).split("=", 1)
        if (
            not egress_gateway
            or internal.get("Options") != isolated_options
            or egress.get("Labels", {}).get(RESOURCE_LABEL_KEY) != RESOURCE_LABEL_VALUE
            or internal.get("Labels", {}).get(RESOURCE_LABEL_KEY) != RESOURCE_LABEL_VALUE
            or egress.get("Labels", {}).get(scope_key) != scope_value
            or internal.get("Labels", {}).get(scope_key) != scope_value
        ):
            raise IsolationError("Docker isolated gateway contract is not exact")
        route_capture = _docker_capture(
            runtime,
            ["exec", proxy_name, "python3", "-I", "-S", "-c", _PROXY_ROUTE_PROGRAM],
            timeout=10,
        )
        try:
            routes = json.loads(route_capture.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IsolationError("proxy route probe returned malformed JSON") from error
        default_routes = [
            route
            for route in routes
            if isinstance(route, Mapping) and route.get("destination") == "00000000"
        ] if isinstance(routes, list) else []
        if (
            len(default_routes) != 1
            or default_routes[0].get("gateway") != egress_gateway
            or default_routes[0].get("up") is not True
        ):
            raise IsolationError("proxy default route is not pinned to egress")

        proxy_inspect = _inspect_one(runtime, "container", proxy_name)
        agent_inspect = _inspect_one(runtime, "container", agent_name)
        _verify_container_hardening(
            runtime, proxy_inspect, role="proxy", network=internal_name
        )
        _verify_container_hardening(
            runtime, agent_inspect, role="agent", network=internal_name
        )
        proxy_networks = proxy_inspect.get("NetworkSettings", {}).get("Networks", {})
        agent_networks = agent_inspect.get("NetworkSettings", {}).get("Networks", {})
        if set(proxy_networks) != {egress_name, internal_name}:
            raise IsolationError("proxy network attachment set is invalid")
        if set(agent_networks) != {internal_name}:
            raise IsolationError("agent is attached outside its private network")
        internal_proxy_ip = proxy_networks.get(internal_name, {}).get("IPAddress")
        if not isinstance(internal_proxy_ip, str) or not internal_proxy_ip:
            raise IsolationError("proxy has no pinned internal address")
        listen_capture = _docker_capture(
            runtime,
            [
                "exec",
                proxy_name,
                "python3",
                "-I",
                "-S",
                "-c",
                _PROXY_LISTEN_PROGRAM,
                str(CONTAINER_PROXY_PORT),
            ],
            timeout=10,
        )
        try:
            listeners = json.loads(listen_capture.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IsolationError("proxy listener probe returned malformed JSON") from error
        if listeners != [internal_proxy_ip]:
            raise IsolationError("proxy is not bound only to its internal interface")
        agent_network_capture = _docker_capture(
            runtime,
            ["exec", agent_name, "python3", "-I", "-S", "-c", _AGENT_NETWORK_PROGRAM],
            timeout=10,
        )
        try:
            agent_network = json.loads(agent_network_capture.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IsolationError("agent network probe returned malformed JSON") from error
        if not isinstance(agent_network, Mapping) or agent_network.get("ok") is not True:
            raise IsolationError("agent retained host, default-route, or DNS egress")
        if proxy_inspect.get("HostConfig", {}).get("PortBindings") not in (None, {}):
            raise IsolationError("proxy unexpectedly publishes a host port")
        proxy_mounts = proxy_inspect.get("Mounts", [])
        if not isinstance(proxy_mounts, list) or any(
            not isinstance(item, Mapping)
            or item.get("Type") != "tmpfs"
            or item.get("Destination") != "/tmp"
            for item in proxy_mounts
        ):
            raise IsolationError("proxy unexpectedly has a host-backed mount")
        mounts = agent_inspect.get("Mounts")
        if not isinstance(mounts, list):
            raise IsolationError("agent mount inspection is unavailable")
        bind_mounts = [
            item
            for item in mounts
            if isinstance(item, Mapping) and item.get("Type") == "bind"
        ]
        destinations = {item.get("Destination") for item in bind_mounts}
        expected = {CONTAINER_WORKSPACE + "/repo", CONTAINER_SKILL}
        if destinations != expected or any(
            item.get("RW") is not False for item in bind_mounts
        ):
            raise IsolationError("agent nested read-only mounts are not exact")
        if any(
            item.get("Type") not in ("bind", "tmpfs")
            for item in mounts
            if isinstance(item, Mapping)
        ):
            raise IsolationError("agent unexpectedly has a persistent mount")
        _verify_tmpfs_contract(
            agent_inspect.get("HostConfig", {}).get("Tmpfs"),
            {
                CONTAINER_WORKSPACE: WORKSPACE_TMPFS_BYTES,
                CONTAINER_CODEX_HOME: STATE_TMPFS_BYTES,
                "/tmp": TMP_LIMIT_BYTES,
            },
            _numeric_user(runtime["images"]["agent"].get("user"), "agent image"),
            "agent boundary",
        )
        encoded_inspection = json.dumps(
            {"proxy": proxy_inspect, "agent": agent_inspect}, sort_keys=True
        )
        if str(_runtime_trust(runtime)["docker_socket"]["path"]) in encoded_inspection:
            raise IsolationError("Docker socket leaked into a container")
        result = {
            "ok": True,
            "agent_to_proxy_health": True,
            "agent_internal_only": True,
            "proxy_dual_homed": True,
            "proxy_default_route": "egress",
            "proxy_internal_bind_only": True,
            "isolated_gateway": True,
            "agent_dns_egress_blocked": True,
            "no_published_ports": True,
            "no_docker_socket_mount": True,
        }
    finally:
        deferred = _defer_termination()
        cleanup_termination: Optional[_TerminationRequested] = None
        try:
            teardown["agent"], pending = _attempt_cleanup(
                lambda: _remove_container(runtime, agent_name),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
            teardown["proxy"], pending = _attempt_cleanup(
                lambda: _remove_container(runtime, proxy_name),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
            if proxy_process is not None:
                proxy_capture, pending = _attempt_cleanup(
                    lambda: _finish_process(proxy_process, 5, terminate=True),
                    Capture(None, b"", b"", timed_out=True),
                )
                cleanup_termination = cleanup_termination or pending
                teardown["proxy_attach"] = {
                    "succeeded": not proxy_capture.timed_out
                    and proxy_capture.limited_stream is None,
                    "exit_code": proxy_capture.returncode,
                    "timed_out": proxy_capture.timed_out,
                }
            teardown["internal_network"], pending = _attempt_cleanup(
                lambda: _remove_network(runtime, internal_name),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
            teardown["egress_network"], pending = _attempt_cleanup(
                lambda: _remove_network(runtime, egress_name),
                {"attempted": True, "succeeded": False},
            )
            cleanup_termination = cleanup_termination or pending
        finally:
            _zero(fake_credential)
            _zero(sentinel)
            _zero(capability)
            try:
                _resume_termination(deferred)
            except _TerminationRequested as error:
                cleanup_termination = cleanup_termination or error
        if cleanup_termination is not None:
            raise cleanup_termination
        if any(not item.get("succeeded", True) for item in teardown.values() if isinstance(item, Mapping)):
            raise IsolationError("runtime boundary probe teardown failed")
    result["teardown"] = teardown
    return result


def _active_probe(
    runtime: Mapping[str, Any], repo: Path, skill: Optional[Path], probe_id: str
) -> Dict[str, Any]:
    name = _resource_name(probe_id, 0, "probe", secrets.token_hex(6))
    teardown: Dict[str, Any] = {"attempted": False, "succeeded": False}
    result: Dict[str, Any] = {}
    try:
        run_arguments = _agent_container_arguments(
            runtime,
            name=name,
            network="none",
            repo=repo,
            skill=skill,
            detached=True,
        )
        _docker_capture(runtime, run_arguments, timeout=60)
        version = _docker_capture(
            runtime,
            ["exec", name, "codex", "--version"],
            timeout=30,
        )
        if version.stdout.strip() != b"codex-cli " + CODEX_CLI_VERSION.encode("ascii"):
            raise IsolationError("containerized Codex CLI version is not pinned")
        command = [
            "exec",
            "--user",
            str(runtime["images"]["agent"]["user"]),
            "--workdir",
            CONTAINER_WORKSPACE,
            name,
            "codex",
            "sandbox",
            "-P",
            ":workspace",
            "-C",
            CONTAINER_WORKSPACE,
            "-c",
            "sandbox_workspace_write.network_access=false",
            "--",
            "python3",
            "-I",
            "-c",
            _ACTIVE_PROBE_PROGRAM,
            "1" if skill is not None else "0",
        ]
        probe_capture = _docker_capture(
            runtime, command, timeout=60, stdout_limit=2 * 1024 * 1024
        )
        result = _parse_probe_output(probe_capture.stdout)
        freeze = _docker_capture(
            runtime,
            ["exec", name, "python3", "-I", "-m", "pip", "freeze", "--all"],
            timeout=30,
            stdout_limit=2 * 1024 * 1024,
        )
        try:
            pip_freeze = freeze.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IsolationError("dependency inventory is not UTF-8") from error
        if "diagrams==" not in pip_freeze or "graphviz==" not in pip_freeze:
            raise IsolationError("agent image dependency inventory is incomplete")

        server_command = _agent_exec_arguments(
            runtime,
            name,
            (
                "python3",
                "-I",
                "-S",
                "-c",
                _CONFIG_PROBE_SERVER_PROGRAM,
                str(CONFIG_PROBE_PORT),
            ),
        )
        server_command.insert(1, "--detach")
        _docker_capture(runtime, server_command, timeout=10)
        for _attempt in range(20):
            ready = _docker_capture(
                runtime,
                _agent_exec_arguments(
                    runtime,
                    name,
                    (
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _CONFIG_PROBE_READY_PROGRAM,
                        str(CONFIG_PROBE_PORT),
                    ),
                ),
                timeout=5,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.05)
        else:
            raise IsolationError("local nonbillable configuration probe did not start")

        last_message = "/tmp/nonbillable-config-probe-last.txt"
        config_command = _agent_exec_arguments(
            runtime,
            name,
            (
                "codex",
                *_codex_exec_arguments(
                    model="gpt-5.4",
                    base_url="http://127.0.0.1:{}/v1".format(CONFIG_PROBE_PORT),
                    capability=secrets.token_hex(32),
                    last_message=last_message,
                    initial=True,
                    session_id=None,
                ),
            ),
            interactive=True,
        )
        config_capture = _docker_capture(
            runtime,
            config_command,
            timeout=30,
            input_data=b"This is a nonbillable configuration parse probe.\n",
            stdout_limit=2 * 1024 * 1024,
            stderr_limit=2 * 1024 * 1024,
            check=False,
        )
        config_output = config_capture.stdout + b"\n" + config_capture.stderr
        if (
            config_capture.timed_out
            or config_capture.limited_stream is not None
            or config_capture.input_error is not None
            or config_capture.returncode == 0
            or _extract_session_id(config_capture.stdout) is None
            or b"unknown configuration" in config_output.lower()
            or b"config error" in config_output.lower()
        ):
            raise IsolationError("pinned Codex exec configuration probe failed")
        if skill is not None:
            discovery_capture = _docker_capture(
                runtime,
                _agent_exec_arguments(
                    runtime,
                    name,
                    (
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _SKILL_DISCOVERY_PROBE_PROGRAM,
                        "diagram-grill",
                        CONTAINER_SKILL + "/SKILL.md",
                        CONTAINER_WORKSPACE,
                    ),
                ),
                timeout=30,
                stdout_limit=2 * 1024 * 1024,
            )
            try:
                discovery = json.loads(discovery_capture.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IsolationError("Codex returned malformed skill discovery output") from error
            if not isinstance(discovery, dict) or discovery.get("ok") is not True:
                raise IsolationError("Codex did not discover the mounted treatment skill")
            result["skill_discovery"] = {
                "ok": True,
                "path": CONTAINER_SKILL,
                "output_sha256": hashlib.sha256(discovery_capture.stdout).hexdigest(),
            }
        result["stdout_sha256"] = hashlib.sha256(probe_capture.stdout).hexdigest()
        result["container_name"] = name
        result["codex_exec_config_parsed"] = True
        result["pip_freeze"] = pip_freeze
    finally:
        deferred = _defer_termination()
        cleanup_termination: Optional[_TerminationRequested] = None
        try:
            teardown, cleanup_termination = _attempt_cleanup(
                lambda: _remove_container(runtime, name),
                {"attempted": True, "succeeded": False},
            )
        finally:
            try:
                _resume_termination(deferred)
            except _TerminationRequested as error:
                cleanup_termination = cleanup_termination or error
        if cleanup_termination is not None:
            raise cleanup_termination
        if not teardown.get("succeeded"):
            raise IsolationError("active probe container teardown failed")
    result["teardown"] = teardown
    return result


def _validate_skill_package(path: Path) -> Dict[str, Any]:
    package = _real_directory(Path(path).absolute(), "skill package")
    entries = _scan_snapshot(package, max_entries=500, max_bytes=10 * 1024 * 1024)
    relative = {entry.relative for entry in entries if not entry.is_directory}
    required = {
        "SKILL.md",
        "requirements.lock",
        "scripts/diagram_grill_cli.py",
    }
    if not required.issubset(relative):
        raise IsolationError("skill package is missing its pinned runtime files")
    encoded = json.dumps(
        [(entry.normalized, entry.size) for entry in entries],
        separators=(",", ":"),
    ).encode("utf-8")
    result = package.lstat()
    return {
        "path": str(package),
        "entry_count": len(entries),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "device": result.st_dev,
        "inode": result.st_ino,
        "mtime_ns": result.st_mtime_ns,
    }


def _revalidate_skill_package_metadata(value: Mapping[str, Any]) -> Path:
    try:
        package = _real_directory(Path(str(value["path"])), "skill package")
        result = package.lstat()
        current = (result.st_dev, result.st_ino, result.st_mtime_ns)
        expected = (int(value["device"]), int(value["inode"]), int(value["mtime_ns"]))
    except (KeyError, TypeError, ValueError) as error:
        raise IsolationError("skill package identity is malformed") from error
    if current != expected:
        raise IsolationError("skill package changed during runtime validation")
    return package


def _directory_identity(path: Path) -> Dict[str, Any]:
    selected = _real_directory(path, "runtime root", writable=True)
    result = selected.lstat()
    return {
        "path": str(selected),
        "device": result.st_dev,
        "inode": result.st_ino,
        "mode": stat.S_IMODE(result.st_mode),
        "uid": result.st_uid,
        "gid": result.st_gid,
    }


def _revalidate_directory_identity(identity: Mapping[str, Any]) -> None:
    try:
        current = _directory_identity(Path(str(identity["path"])))
    except (KeyError, TypeError, ValueError) as error:
        raise IsolationError("runtime root identity is malformed") from error
    if current != dict(identity):
        raise IsolationError("runtime root identity changed after validation")


def _paths_overlap(first: Path, second: Path) -> bool:
    left = Path(first)
    right = Path(second)
    if left == right or left in right.parents or right in left.parents:
        return True
    for component in (right,) + tuple(right.parents):
        try:
            if left.samefile(component):
                return True
        except FileNotFoundError:
            continue
    for component in (left,) + tuple(left.parents):
        try:
            if right.samefile(component):
                return True
        except FileNotFoundError:
            continue
    return False


def _docker_engine_version(value: Any) -> Tuple[int, int, int]:
    text = str(value)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", text)
    if match is None:
        raise IsolationError("Docker Engine returned an unsupported version string")
    return tuple(int(part) for part in match.groups())


def _required_docker_engine_version(value: Any) -> Tuple[int, int, int]:
    version = _docker_engine_version(value)
    if version[0] < MINIMUM_DOCKER_ENGINE_MAJOR:
        raise IsolationError(
            "Docker Engine {} or newer is required for isolated bridge gateways".format(
                MINIMUM_DOCKER_ENGINE_MAJOR
            )
        )
    return version


def validate_runtime(
    agent_image_id: str,
    proxy_image_id: str,
    skill_package: Path,
    results_root: Path,
    state_root: Path,
    docker_executable: Path,
    docker_socket: Path,
) -> Dict[str, Any]:
    """Validate and freeze the nonbillable Docker isolation boundary."""

    if _IMAGE_ID.fullmatch(str(agent_image_id)) is None or _IMAGE_ID.fullmatch(
        str(proxy_image_id)
    ) is None:
        raise IsolationError("Docker images must be immutable sha256 image IDs")
    results = _private_directory(
        Path(results_root).absolute(), "results root", writable=True
    )
    state = _private_directory(Path(state_root).absolute(), "state root", writable=True)
    repository = _real_directory(REPOSITORY_ROOT, "repository root")
    roots = {"repository": repository, "results": results, "state": state}
    root_items = list(roots.items())
    for index, (left_name, left) in enumerate(root_items):
        for right_name, right in root_items[index + 1 :]:
            if _paths_overlap(left, right):
                raise IsolationError(
                    "{} and {} roots must be disjoint".format(left_name, right_name)
                )
    package_metadata = _validate_skill_package(skill_package)
    trust = {
        "docker_executable": _file_identity(Path(docker_executable), "executable"),
        "docker_socket": _file_identity(Path(docker_socket), "socket"),
        "seccomp_profile": _file_identity(SECCOMP_PROFILE, "regular"),
    }
    if trust["seccomp_profile"].get("sha256") != SECCOMP_PROFILE_SHA256:
        raise IsolationError("pinned bwrap seccomp profile hash is invalid")
    runtime: Dict[str, Any] = {
        "schema_version": "1",
        "contract": "docker-agent-proxy-v1",
        "trust": trust,
        "roots": {
            name: _directory_identity(path) for name, path in roots.items()
        },
        "limits": {
            "workspace_bytes": WORKSPACE_LIMIT_BYTES,
            "codex_home_bytes": STATE_LIMIT_BYTES,
            "tmp_bytes": TMP_LIMIT_BYTES,
            "snapshot_entries": SNAPSHOT_MAX_ENTRIES,
            "stdout_bytes": MAX_STDOUT_BYTES,
            "stderr_bytes": MAX_STDERR_BYTES,
            "last_message_bytes": MAX_LAST_MESSAGE_BYTES,
            "max_turns": MAX_TURNS_PER_RUN,
            "archive_overhead_bytes": MAX_ARCHIVE_OVERHEAD_BYTES,
            "turn_timeout_seconds": MAX_TURN_TIMEOUT_SECONDS,
        },
        "skill_package": package_metadata,
    }
    version_capture = _docker_capture(
        runtime,
        ["version", "--format", "{{json .}}"],
        stdout_limit=1024 * 1024,
    )
    try:
        version = json.loads(version_capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("Docker runtime returned malformed version metadata") from error
    if not isinstance(version, dict) or not isinstance(version.get("Server"), dict):
        raise IsolationError("Docker daemon version metadata is unavailable")
    server_version = _required_docker_engine_version(version["Server"].get("Version"))
    runtime["docker"] = {
        "client_version": str(version.get("Client", {}).get("Version", "unavailable")),
        "server_version": str(version["Server"].get("Version", "unavailable")),
        "server_version_tuple": list(server_version),
    }
    runtime["images"] = {
        "agent": _image_metadata(runtime, agent_image_id, "agent"),
        "proxy": _image_metadata(runtime, proxy_image_id, "proxy"),
    }
    validation_lock = _acquire_evaluation_lock(state)
    probe_root: Optional[Path] = None
    try:
        runtime["startup_scavenge"] = _scavenge_resources(runtime)
        probe_root = Path(tempfile.mkdtemp(prefix=".runtime-probe-", dir=str(state)))
        probe_root.chmod(0o700)
        probe_repo = probe_root / "repo"
        probe_skill = probe_root / "skill-package"
        probe_repo.mkdir(mode=0o755)
        probe_readme = probe_repo / "README.md"
        probe_readme.write_text("nonbillable isolation probe\n", encoding="utf-8")
        probe_readme.chmod(0o644)
        probe_skill.mkdir(mode=0o755)
        package_source = _revalidate_skill_package_metadata(package_metadata)
        package_archive = _snapshot_archive(package_source, max_bytes=10 * 1024 * 1024)
        _revalidate_skill_package_metadata(package_metadata)
        runtime["skill_package"]["probe_archive_sha256"] = hashlib.sha256(
            package_archive
        ).hexdigest()
        _replace_snapshot(
            probe_skill,
            package_archive,
            max_bytes=10 * 1024 * 1024,
            secrets_=(),
        )
        _set_mount_source_modes(probe_repo)
        _set_mount_source_modes(probe_skill)
        _mount_source_identity(probe_repo)
        _mount_source_identity(probe_skill)
        probe = _active_probe(runtime, probe_repo, probe_skill, "runtime")
        boundary = _boundary_probe(runtime, probe_repo, probe_skill, "runtime")
    finally:
        if probe_root is not None and probe_root.exists() and not probe_root.is_symlink():
            shutil.rmtree(probe_root)
        _release_evaluation_lock(validation_lock)
    pip_freeze = probe.pop("pip_freeze")
    runtime["active_probe"] = probe
    runtime["boundary_probe"] = boundary
    runtime["preflight"] = {"sandbox": probe, "boundary": boundary}
    runtime["pip_freeze"] = pip_freeze
    runtime["codex_version"] = "codex-cli {}".format(CODEX_CLI_VERSION)
    runtime["agent_image"] = {
        "id": agent_image_id,
        "configured_user": runtime["images"]["agent"]["user"],
        "contract": AGENT_RUNTIME_CONTRACT,
        "entrypoint": runtime["images"]["agent"]["entrypoint"],
    }
    runtime["proxy_image"] = {
        "id": proxy_image_id,
        "configured_user": runtime["images"]["proxy"]["user"],
        "contract": PROXY_RUNTIME_CONTRACT,
        "entrypoint": runtime["images"]["proxy"]["entrypoint"],
    }
    runtime["validated_at"] = _now()
    return runtime


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_snapshot_tree(path: Path) -> None:
    root = _real_directory(path, "snapshot fsync root")
    directories = [root]
    for current, names, _files in os.walk(root, topdown=True, followlinks=False):
        parent = Path(current)
        for name in names:
            child = parent / name
            if child.is_symlink() or not child.is_dir():
                raise IsolationError("snapshot fsync tree contains a non-directory")
            directories.append(child)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _atomic_journal_write(path: Path, value: Mapping[str, Any]) -> None:
    parent = _private_directory(path.parent, "snapshot journal parent", writable=True)
    temporary = parent / (".snapshot-journal-" + secrets.token_hex(12))
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise IsolationError("short snapshot journal write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _load_snapshot_journal(path: Path) -> Dict[str, Any]:
    selected = _exact_existing_path(path, "snapshot journal")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(selected, flags)
    try:
        result = os.fstat(descriptor)
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else result.st_uid
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_uid != expected_uid
            or stat.S_IMODE(result.st_mode) & 0o077
            or result.st_size > 64 * 1024
        ):
            raise IsolationError("snapshot journal is not a private bounded file")
        payload = os.read(descriptor, result.st_size + 1)
        if len(payload) != result.st_size:
            raise IsolationError("snapshot journal changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("snapshot journal is malformed") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "generation",
        "turn",
        "session_id",
        "workspace",
        "state",
    }:
        raise IsolationError("snapshot journal has an unexpected schema")
    if value.get("schema_version") != "1":
        raise IsolationError("snapshot journal schema is unsupported")
    generation = value.get("generation")
    turn = value.get("turn")
    session_id = value.get("session_id")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(turn, int)
        or isinstance(turn, bool)
        or generation != turn
        or turn < 0
        or turn > MAX_TURNS_PER_RUN
        or (
            session_id is not None
            and (not isinstance(session_id, str) or _THREAD_ID.fullmatch(session_id) is None)
        )
    ):
        raise IsolationError("snapshot journal turn or session is invalid")
    for name in ("workspace", "state"):
        entry = value.get(name)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry.get("path"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256"))) is None
        ):
            raise IsolationError("snapshot journal generation entry is invalid")
    return value


def _empty_snapshot_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w"):
        pass
    return output.getvalue()


def prepare_snapshots(run_dir: Path, state_dir: Path) -> Dict[str, str]:
    """Create generation zero and one durable pointer for the snapshot pair."""

    run = _private_directory(Path(run_dir).absolute(), "run directory", writable=True)
    state = _private_directory(Path(state_dir).absolute(), "state directory", writable=True)
    if _paths_overlap(run, state) or _paths_overlap(REPOSITORY_ROOT.resolve(), state):
        raise IsolationError("session state directory must be external and disjoint")
    if stat.S_IMODE(state.lstat().st_mode) & 0o077:
        raise IsolationError("session state directory must be owner-only")
    try:
        if any(state.iterdir()):
            raise IsolationError("session state directory must be empty")
    except OSError as error:
        raise IsolationError("session state directory is unreadable") from error

    snapshots = run / "snapshots"
    workspace_generations = snapshots / "workspace-generations"
    state_generations = state / "codex-home-generations"
    initial_archive = _empty_snapshot_archive()
    digest = hashlib.sha256(initial_archive).hexdigest()
    workspace = workspace_generations / digest
    codex_home = state_generations / digest
    journal = snapshots / "current.json"
    created: List[Path] = []
    try:
        snapshots.mkdir(mode=0o700)
        created.append(snapshots)
        workspace_generations.mkdir(mode=0o700)
        created.append(workspace_generations)
        state_generations.mkdir(mode=0o700)
        created.append(state_generations)
        workspace.mkdir(mode=0o700)
        created.append(workspace)
        codex_home.mkdir(mode=0o700)
        created.append(codex_home)
        for path, label in (
            (workspace, "workspace snapshot"),
            (codex_home, "Codex state snapshot"),
        ):
            selected = _private_directory(path, label, writable=True)
            if any(selected.iterdir()):
                raise IsolationError("{} was not created empty".format(label))
        pointer = {
            "schema_version": "1",
            "generation": 0,
            "turn": 0,
            "session_id": None,
            "workspace": {"path": str(workspace), "sha256": digest},
            "state": {"path": str(codex_home), "sha256": digest},
        }
        for durable_directory in (
            workspace,
            codex_home,
            workspace_generations,
            state_generations,
            snapshots,
            run,
            state,
        ):
            _fsync_directory(durable_directory)
        _atomic_journal_write(journal, pointer)
        if _load_snapshot_journal(journal) != pointer:
            raise IsolationError("initial snapshot journal did not persist exactly")
    except BaseException:
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass
        raise
    return {
        "workspace": str(workspace),
        "state": str(codex_home),
        "journal": str(journal),
    }


def _snapshot_with_identity(root: Path, *, max_bytes: int) -> Tuple[bytes, Dict[str, Any]]:
    selected = _real_directory(root, "snapshot source")
    before = selected.lstat()
    identity = {
        "path": str(selected),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mtime_ns": before.st_mtime_ns,
    }
    archive = _snapshot_archive(selected, max_bytes=max_bytes)
    after = selected.lstat()
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise IsolationError("snapshot source changed while being archived")
    return archive, identity


def _revalidate_snapshot_identity(identity: Mapping[str, Any]) -> None:
    try:
        selected = _real_directory(Path(str(identity["path"])), "snapshot source")
        result = selected.lstat()
        current = (result.st_dev, result.st_ino, result.st_mtime_ns)
        expected = (
            int(identity["device"]),
            int(identity["inode"]),
            int(identity["mtime_ns"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IsolationError("snapshot identity is malformed") from error
    if current != expected:
        raise IsolationError("snapshot source identity changed")


def _agent_exec_arguments(
    runtime: Mapping[str, Any], name: str, command: Sequence[str], *, interactive: bool = False
) -> List[str]:
    user = _numeric_user(runtime["images"]["agent"].get("user"), "agent image")
    arguments = ["exec"]
    if interactive:
        arguments.append("--interactive")
    arguments.extend(("--user", user, "--workdir", CONTAINER_WORKSPACE, name))
    arguments.extend(str(item) for item in command)
    return arguments


def _import_snapshot(
    runtime: Mapping[str, Any],
    name: str,
    root: str,
    archive: bytes,
    *,
    max_bytes: int,
    protected: Sequence[str],
) -> Dict[str, Any]:
    archive_limit = max_bytes + MAX_ARCHIVE_OVERHEAD_BYTES
    _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime, name, ("python3", "-I", "-S", "-c", _CREATE_STAGING_PROGRAM, root)
        ),
        timeout=30,
    )
    _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime,
            name,
            (
                "python3",
                "-I",
                "-S",
                "-c",
                _IMPORT_BRIDGE_PROGRAM,
                CONTAINER_RUNTIME_ROOT + "/snapshot_import.py",
                root + "/.snapshot-import",
                str(SNAPSHOT_MAX_ENTRIES),
                str(max_bytes),
                str(archive_limit),
            ),
            interactive=True,
        ),
        timeout=90,
        input_data=archive,
        stdout_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
        stderr_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
    )
    _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime,
            name,
            (
                "python3",
                "-I",
                "-S",
                "-c",
                _PROMOTE_STAGING_PROGRAM,
                root,
                *protected,
            ),
        ),
        timeout=30,
    )
    return {
        "archive_bytes": len(archive),
        "sha256": hashlib.sha256(archive).hexdigest(),
    }


def _export_snapshot(
    runtime: Mapping[str, Any],
    name: str,
    root: str,
    *,
    max_bytes: int,
    excludes: Sequence[str],
    secrets_: Sequence[bytearray],
) -> Tuple[bytes, Dict[str, Any]]:
    preflight = _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime,
            name,
            (
                "python3",
                "-I",
                "-S",
                "-c",
                _SNAPSHOT_PREFLIGHT_PROGRAM,
                root,
                str(SNAPSHOT_MAX_ENTRIES),
                str(max_bytes),
                *excludes,
            ),
        ),
        timeout=30,
        stdout_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
    )
    try:
        preflight_value = json.loads(preflight.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("snapshot preflight returned malformed JSON") from error
    command: List[str] = [
        "python3",
        "-I",
        "-S",
        CONTAINER_RUNTIME_ROOT + "/snapshot_export.py",
        root,
    ]
    for excluded in excludes:
        command.extend(("--exclude", excluded))
    command.extend(
        ("--max-entries", str(SNAPSHOT_MAX_ENTRIES), "--max-bytes", str(max_bytes))
    )
    capture = _docker_capture(
        runtime,
        _agent_exec_arguments(runtime, name, command),
        timeout=90,
        stdout_limit=max_bytes + MAX_ARCHIVE_OVERHEAD_BYTES,
        stderr_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
    )
    entries = _validated_snapshot_archive(
        capture.stdout, root=root, max_bytes=max_bytes, secrets_=secrets_
    )
    protected = set(excludes)
    if any(PurePosixPath(relative).parts[0] in protected for relative, _, _ in entries):
        raise IsolationError("snapshot export included a protected mount")
    return capture.stdout, {
        "preflight": preflight_value,
        "archive_bytes": len(capture.stdout),
        "entry_count": len(entries),
        "sha256": hashlib.sha256(capture.stdout).hexdigest(),
    }


def _verify_snapshot_generation(
    path: Path,
    digest: str,
    *,
    root: str,
    max_bytes: int,
    secrets_: Sequence[bytearray],
) -> Dict[str, Any]:
    if path.name != digest:
        raise IsolationError("snapshot generation path is not content-addressed")
    selected = _private_directory(path, "snapshot generation")
    archive = _snapshot_archive(selected, max_bytes=max_bytes)
    _validated_snapshot_archive(
        archive, root=root, max_bytes=max_bytes, secrets_=secrets_
    )
    observed = hashlib.sha256(archive).hexdigest()
    if observed != digest:
        raise IsolationError("snapshot generation content hash mismatched its path")
    return {
        "path": str(selected),
        "sha256": observed,
        "archive_bytes": len(archive),
    }


def _materialize_snapshot_generation(
    parent: Path,
    archive: bytes,
    *,
    root: str,
    max_bytes: int,
    secrets_: Sequence[bytearray],
) -> Dict[str, Any]:
    generations = _private_directory(parent, "snapshot generations", writable=True)
    _validated_snapshot_archive(
        archive, root=root, max_bytes=max_bytes, secrets_=secrets_
    )
    digest = hashlib.sha256(archive).hexdigest()
    target = generations / digest
    if target.exists() or target.is_symlink():
        verified = _verify_snapshot_generation(
            target, digest, root=root, max_bytes=max_bytes, secrets_=secrets_
        )
        # A prior process may have renamed this generation into place and died
        # before persisting the directory entry. Re-durabilize the complete
        # existing generation before allowing a journal to point at it.
        _fsync_snapshot_tree(target)
        _fsync_directory(generations)
        return verified
    incoming = Path(tempfile.mkdtemp(prefix=".snapshot-generation-", dir=str(generations)))
    incoming.chmod(0o700)
    try:
        _replace_snapshot(
            incoming, archive, max_bytes=max_bytes, secrets_=secrets_
        )
        _fsync_snapshot_tree(incoming)
        try:
            os.rename(incoming, target)
        except OSError:
            if not target.exists() or target.is_symlink():
                raise
            shutil.rmtree(incoming)
        _fsync_directory(generations)
    finally:
        if incoming.exists() and not incoming.is_symlink():
            shutil.rmtree(incoming)
    return _verify_snapshot_generation(
        target, digest, root=root, max_bytes=max_bytes, secrets_=secrets_
    )


def _commit_snapshot_pair(
    journal_path: Path,
    workspace_target: Path,
    state_target: Path,
    workspace_after: bytes,
    state_after: bytes,
    *,
    turn: int,
    session_id: Optional[str],
    secrets_: Sequence[bytearray],
) -> Dict[str, Any]:
    """Durably publish both immutable generations with one atomic pointer write."""

    current = _load_snapshot_journal(journal_path)
    if (
        current.get("turn") != turn - 1
        or current.get("generation") != turn - 1
        or current.get("workspace", {}).get("path") != str(workspace_target)
        or current.get("state", {}).get("path") != str(state_target)
    ):
        raise IsolationError("snapshot journal changed before pair commit")
    if session_id is not None and (
        not isinstance(session_id, str) or _THREAD_ID.fullmatch(session_id) is None
    ):
        raise IsolationError("snapshot commit session is invalid")
    workspace = _materialize_snapshot_generation(
        workspace_target.parent,
        workspace_after,
        root=CONTAINER_WORKSPACE,
        max_bytes=WORKSPACE_LIMIT_BYTES,
        secrets_=secrets_,
    )
    state = _materialize_snapshot_generation(
        state_target.parent,
        state_after,
        root=CONTAINER_CODEX_HOME,
        max_bytes=STATE_LIMIT_BYTES,
        secrets_=secrets_,
    )
    pointer = {
        "schema_version": "1",
        "generation": turn,
        "turn": turn,
        "session_id": session_id,
        "workspace": {"path": workspace["path"], "sha256": workspace["sha256"]},
        "state": {"path": state["path"], "sha256": state["sha256"]},
    }
    _atomic_journal_write(journal_path, pointer)
    if _load_snapshot_journal(journal_path) != pointer:
        raise IsolationError("snapshot pair journal did not persist exactly")
    return {
        "workspace": workspace,
        "state": state,
        "journal": str(journal_path),
        "generation": turn,
        "atomic_pointer": True,
    }


def _atomic_raw_write(run_dir: Path, relative: str, data: bytes) -> str:
    if not relative.startswith("raw/") or PurePosixPath(relative).is_absolute():
        raise IsolationError("raw output path is invalid")
    raw = _real_directory(run_dir / "raw", "raw output directory", writable=True)
    destination = run_dir / relative
    if destination.parent != raw or destination.exists() or destination.is_symlink():
        raise IsolationError("raw output destination is not new and direct")
    temporary = raw / (".raw-write-" + secrets.token_hex(12))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise IsolationError("short raw output write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return relative


def _recovery_runtime(
    run_dir: Path, state: Mapping[str, Any]
) -> Tuple[Path, Mapping[str, Any], Path, Path]:
    """Validate only the durable runtime/root boundary needed for scavenging."""

    if not isinstance(state, Mapping):
        raise IsolationError("run state is malformed")
    run = _real_directory(Path(run_dir).absolute(), "run directory", writable=True)
    execution = state.get("execution")
    isolation_state = execution.get("isolation") if isinstance(execution, Mapping) else None
    if not isinstance(isolation_state, Mapping):
        raise IsolationError("run state has no isolation metadata")
    runtime = isolation_state.get("runtime_validation")
    if not isinstance(runtime, Mapping) or runtime.get("contract") != "docker-agent-proxy-v1":
        raise IsolationError("runtime validation contract is unavailable")
    if runtime.get("codex_version") != "codex-cli {}".format(CODEX_CLI_VERSION):
        raise IsolationError("runtime validation Codex version drifted")
    preflight = runtime.get("preflight")
    if (
        not isinstance(preflight, Mapping)
        or not isinstance(preflight.get("sandbox"), Mapping)
        or preflight["sandbox"].get("ok") is not True
        or preflight["sandbox"].get("codex_exec_config_parsed") is not True
        or not isinstance(preflight.get("boundary"), Mapping)
        or preflight["boundary"].get("ok") is not True
    ):
        raise IsolationError("runtime active probes are incomplete")
    _revalidate_runtime(runtime)
    roots = runtime.get("roots")
    if not isinstance(roots, Mapping):
        raise IsolationError("runtime roots are unavailable")
    for key in ("repository", "results", "state"):
        identity = roots.get(key)
        if not isinstance(identity, Mapping):
            raise IsolationError("runtime root identity is incomplete")
        _revalidate_directory_identity(identity)
    results_root = Path(str(roots["results"]["path"]))
    state_root = Path(str(roots["state"]["path"]))
    try:
        run.relative_to(results_root)
    except ValueError as error:
        raise IsolationError("run directory escaped the validated results root") from error
    return run, runtime, results_root, state_root


def _turn_context(
    run_dir: Path,
    state: Mapping[str, Any],
    prompt: str,
    turn: int,
    initial: bool,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if (
        not isinstance(turn, int)
        or isinstance(turn, bool)
        or turn < 1
        or turn > MAX_TURNS_PER_RUN
    ):
        raise IsolationError("turn is outside the frozen evaluation limit")
    if not isinstance(state, Mapping):
        raise IsolationError("run state is malformed")
    condition = state.get("condition")
    if condition not in ("baseline", "diagram-grill"):
        raise IsolationError("run condition is invalid")
    model = state.get("model")
    if (
        not isinstance(model, str)
        or not model
        or len(model) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in model)
    ):
        raise IsolationError("run model is invalid")
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or _SAFE_ID.fullmatch(run_id) is None:
        raise IsolationError("run ID is invalid")
    previous_turn = state.get("turn_count", turn - 1)
    if not isinstance(previous_turn, int) or turn != previous_turn + 1:
        raise IsolationError("turn does not advance the prepared state by one")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 1
        or timeout_seconds > MAX_TURN_TIMEOUT_SECONDS
    ):
        raise IsolationError("turn timeout is outside the proxy lifetime")
    if not isinstance(prompt, str):
        raise IsolationError("turn prompt must be text")
    encoded_prompt = prompt.encode("utf-8")
    if not encoded_prompt or len(encoded_prompt) > MAX_PROMPT_BYTES or "\x00" in prompt:
        raise IsolationError("turn prompt is empty or exceeds its bound")
    session_id = state.get("session_id")
    if initial:
        if turn != 1 or session_id is not None:
            raise IsolationError("initial turn state is inconsistent")
    elif not isinstance(session_id, str) or _THREAD_ID.fullmatch(session_id) is None:
        raise IsolationError("reply turn has no pinned session ID")

    run, runtime, results_root, state_root = _recovery_runtime(run_dir, state)
    execution = state["execution"]
    isolation_state = execution["isolation"]
    run_relative = run.relative_to(results_root)

    workspace_value = isolation_state.get("workspace_snapshot_path")
    state_value = isolation_state.get("state_snapshot_path")
    if state_value is None:
        state_value = isolation_state.get("codex_home_snapshot_path")
    journal_value = isolation_state.get("snapshot_journal_path")
    expected_journal = run / "snapshots" / "current.json"
    journal_path = _exact_existing_path(
        Path(str(journal_value)), "snapshot journal"
    )
    if journal_path != expected_journal:
        raise IsolationError("snapshot journal path is not frozen under the run")
    journal = _load_snapshot_journal(journal_path)
    expected_session = session_id.lower() if isinstance(session_id, str) else None
    journal_session = journal.get("session_id")
    if isinstance(journal_session, str):
        journal_session = journal_session.lower()
    if journal.get("turn") != previous_turn or journal_session != expected_session:
        raise IsolationError("snapshot journal does not match the prepared turn/session")
    workspace = _private_directory(
        Path(str(journal["workspace"]["path"])), "workspace snapshot", writable=True
    )
    codex_home = _private_directory(
        Path(str(journal["state"]["path"])), "Codex state snapshot", writable=True
    )
    frozen_workspace = Path(str(workspace_value))
    frozen_state = Path(str(state_value))
    expected_workspace_parent = run / "snapshots" / "workspace-generations"
    expected_state_parent = state_root / run_relative / "codex-home-generations"
    if (
        workspace.parent != expected_workspace_parent
        or frozen_workspace != workspace
        or frozen_workspace.parent != expected_workspace_parent
    ):
        raise IsolationError("workspace generation path is not frozen under the run")
    try:
        codex_home.relative_to(state_root)
    except ValueError as error:
        raise IsolationError("Codex state snapshot escaped the validated state root") from error
    if (
        frozen_state != codex_home
        or codex_home.parent != expected_state_parent
        or _paths_overlap(workspace, codex_home)
        or _paths_overlap(run, codex_home)
    ):
        raise IsolationError("run and state snapshots are not disjoint")
    _verify_snapshot_generation(
        workspace,
        str(journal["workspace"]["sha256"]),
        root=CONTAINER_WORKSPACE,
        max_bytes=WORKSPACE_LIMIT_BYTES,
        secrets_=(),
    )
    _verify_snapshot_generation(
        codex_home,
        str(journal["state"]["sha256"]),
        root=CONTAINER_CODEX_HOME,
        max_bytes=STATE_LIMIT_BYTES,
        secrets_=(),
    )

    repo = _real_directory(run / "input" / "repo", "evidence repository")
    skill_path = run / "input" / "skill-package"
    if condition == "diagram-grill":
        skill: Optional[Path] = _real_directory(skill_path, "installed skill package")
    else:
        if skill_path.exists() or skill_path.is_symlink():
            raise IsolationError("baseline run unexpectedly exposes a skill package")
        skill = None
    raw = _real_directory(run / "raw", "raw output directory", writable=True)
    if any(path.is_symlink() for path in (run, workspace, codex_home, repo, raw)):
        raise IsolationError("run boundary contains a symlink")
    return {
        "run": run,
        "runtime": runtime,
        "condition": condition,
        "model": model,
        "run_id": run_id,
        "session_id": session_id,
        "prompt": encoded_prompt,
        "workspace": workspace,
        "codex_home": codex_home,
        "snapshot_journal": journal_path,
        "repo": repo,
        "skill": skill,
    }


def _reject_mount_acls(paths: Sequence[Path]) -> None:
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    system = platform.system()
    for offset in range(0, len(paths), 100):
        batch = [str(path) for path in paths[offset : offset + 100]]
        if system == "Darwin":
            command = ["/bin/ls", "-lde", *batch]
        elif system == "Linux":
            executable = next(
                (
                    candidate
                    for candidate in ("/usr/bin/getfacl", "/bin/getfacl")
                    if Path(candidate).is_file()
                ),
                None,
            )
            if executable is None:
                raise IsolationError("getfacl is required to inspect container mount ACLs")
            command = [executable, "--absolute-names", "--omit-header", *batch]
        else:
            raise IsolationError("container mount ACL checks support only macOS and Linux")
        try:
            completed = subprocess.run(
                command,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IsolationError("container mount ACLs cannot be inspected") from error
        if completed.returncode != 0:
            raise IsolationError("container mount ACLs cannot be inspected")
        lines = [line for line in completed.stdout.splitlines() if line]
        if system == "Darwin":
            if not _darwin_acl_output_is_trusted(completed.stdout, len(batch)):
                raise IsolationError("container mount source has an extended ACL")
        elif any(
            line.startswith("default:")
            or (line.startswith("user:") and not line.startswith("user::"))
            or (line.startswith("group:") and not line.startswith("group::"))
            for line in lines
        ):
            raise IsolationError("container mount source has an extended ACL")


def _set_mount_source_modes(path: Path) -> None:
    selected = _real_directory(path, "container mount source")
    entries = _scan_snapshot(
        selected, max_entries=SNAPSHOT_MAX_ENTRIES, max_bytes=WORKSPACE_LIMIT_BYTES
    )
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else selected.lstat().st_uid
    for candidate, is_directory in (
        (selected, True),
        *((entry.path, entry.is_directory) for entry in entries),
    ):
        result = candidate.lstat()
        if result.st_uid != expected_uid:
            raise IsolationError("container mount source is not owned by the current user")
        candidate.chmod(0o755 if is_directory else 0o644)


def _mount_source_identity(path: Path) -> Dict[str, Any]:
    selected = _real_directory(path, "container mount source")
    _private_directory(
        selected.parent, "container mount source private parent"
    )
    root_result = selected.lstat()
    entries = _scan_snapshot(
        selected, max_entries=SNAPSHOT_MAX_ENTRIES, max_bytes=WORKSPACE_LIMIT_BYTES
    )
    items: List[List[Any]] = []
    total_bytes = 0
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else root_result.st_uid
    if root_result.st_uid != expected_uid or stat.S_IMODE(root_result.st_mode) != 0o755:
        raise IsolationError(
            "container mount root must be current-user-owned mode 0755 under a private parent"
        )
    for entry in entries:
        result = entry.path.lstat()
        permissions = stat.S_IMODE(result.st_mode)
        expected_mode = 0o755 if entry.is_directory else 0o644
        if result.st_uid != expected_uid or permissions != expected_mode:
            raise IsolationError(
                "container mount entries must be current-user-owned mode 0755/0644"
            )
        if entry.is_directory:
            items.append([entry.normalized, "directory", 0, ""])
            continue
        with _open_snapshot_entry(entry) as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                digest.update(chunk)
        current = entry.path.lstat()
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) != (entry.device, entry.inode, entry.size, entry.mtime_ns):
            raise IsolationError("container mount changed while hashing")
        total_bytes += entry.size
        items.append([entry.normalized, "file", entry.size, digest.hexdigest()])
    _reject_mount_acls([selected, *(entry.path for entry in entries)])
    encoded = json.dumps(
        items, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return {
        "path": str(selected),
        "device": root_result.st_dev,
        "inode": root_result.st_ino,
        "mtime_ns": root_result.st_mtime_ns,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "entries": len(items),
        "bytes": total_bytes,
    }


def _revalidate_mount_source(identity: Mapping[str, Any]) -> None:
    try:
        current = _mount_source_identity(Path(str(identity["path"])))
    except (KeyError, TypeError, ValueError) as error:
        raise IsolationError("container mount identity is malformed") from error
    if current != dict(identity):
        raise IsolationError("container mount source identity changed")


def _verify_container_mount_manifest(
    runtime: Mapping[str, Any], name: str, container_path: str, identity: Mapping[str, Any]
) -> None:
    capture = _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime,
            name,
            (
                "python3",
                "-I",
                "-S",
                "-c",
                _MOUNT_MANIFEST_PROGRAM,
                container_path,
                str(SNAPSHOT_MAX_ENTRIES),
                str(WORKSPACE_LIMIT_BYTES),
            ),
        ),
        timeout=90,
        stdout_limit=MAX_DOCKER_DIAGNOSTIC_BYTES,
    )
    try:
        value = json.loads(capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("container mount manifest is malformed") from error
    expected = {
        "manifest_sha256": identity.get("manifest_sha256"),
        "entries": identity.get("entries"),
        "bytes": identity.get("bytes"),
    }
    if value != expected:
        raise IsolationError("container mount content differs from the host manifest")


def _verify_turn_boundary(
    runtime: Mapping[str, Any],
    *,
    agent_name: str,
    proxy_name: str,
    internal_name: str,
    egress_name: str,
    repo: Path,
    skill: Optional[Path],
) -> Dict[str, Any]:
    _docker_capture(
        runtime,
        _agent_exec_arguments(
            runtime,
            agent_name,
            ("python3", "-I", "-S", "-c", _PROXY_HEALTH_PROGRAM, CONTAINER_PROXY_HOST),
        ),
        timeout=10,
    )
    egress = _inspect_one(runtime, "network", egress_name)
    internal = _inspect_one(runtime, "network", internal_name)
    if egress.get("Internal") is not False or internal.get("Internal") is not True:
        raise IsolationError("per-turn networks are not private and separated")
    egress_gateway = _network_gateway(egress)
    isolated_options = {
        "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
        "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
    }
    scope_key, scope_value = _resource_scope_label(runtime).split("=", 1)
    if (
        not egress_gateway
        or internal.get("Options") != isolated_options
        or egress.get("Labels", {}).get(RESOURCE_LABEL_KEY) != RESOURCE_LABEL_VALUE
        or internal.get("Labels", {}).get(RESOURCE_LABEL_KEY) != RESOURCE_LABEL_VALUE
        or egress.get("Labels", {}).get(scope_key) != scope_value
        or internal.get("Labels", {}).get(scope_key) != scope_value
    ):
        raise IsolationError("per-turn isolated gateway contract drifted")
    route_capture = _docker_capture(
        runtime,
        ["exec", proxy_name, "python3", "-I", "-S", "-c", _PROXY_ROUTE_PROGRAM],
        timeout=10,
    )
    try:
        routes = json.loads(route_capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("per-turn proxy route probe is malformed") from error
    defaults = [
        item
        for item in routes
        if isinstance(item, Mapping) and item.get("destination") == "00000000"
    ] if isinstance(routes, list) else []
    if len(defaults) != 1 or defaults[0].get("gateway") != egress_gateway:
        raise IsolationError("per-turn proxy default route is not egress")

    agent = _inspect_one(runtime, "container", agent_name)
    proxy = _inspect_one(runtime, "container", proxy_name)
    _verify_container_hardening(
        runtime, agent, role="agent", network=internal_name
    )
    _verify_container_hardening(
        runtime, proxy, role="proxy", network=internal_name
    )
    if set(agent.get("NetworkSettings", {}).get("Networks", {})) != {internal_name}:
        raise IsolationError("per-turn agent has an unexpected network")
    if set(proxy.get("NetworkSettings", {}).get("Networks", {})) != {
        internal_name,
        egress_name,
    }:
        raise IsolationError("per-turn proxy network set is invalid")
    proxy_networks = proxy.get("NetworkSettings", {}).get("Networks", {})
    internal_proxy_ip = proxy_networks.get(internal_name, {}).get("IPAddress")
    if not isinstance(internal_proxy_ip, str) or not internal_proxy_ip:
        raise IsolationError("per-turn proxy has no internal address")
    listen_capture = _docker_capture(
        runtime,
        [
            "exec",
            proxy_name,
            "python3",
            "-I",
            "-S",
            "-c",
            _PROXY_LISTEN_PROGRAM,
            str(CONTAINER_PROXY_PORT),
        ],
        timeout=10,
    )
    try:
        listeners = json.loads(listen_capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("per-turn proxy listener probe is malformed") from error
    if listeners != [internal_proxy_ip]:
        raise IsolationError("per-turn proxy bind escaped its internal interface")
    agent_network_capture = _docker_capture(
        runtime,
        ["exec", agent_name, "python3", "-I", "-S", "-c", _AGENT_NETWORK_PROGRAM],
        timeout=10,
    )
    try:
        agent_network = json.loads(agent_network_capture.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolationError("per-turn agent network probe is malformed") from error
    if not isinstance(agent_network, Mapping) or agent_network.get("ok") is not True:
        raise IsolationError("per-turn agent retained host, default-route, or DNS egress")
    if proxy.get("HostConfig", {}).get("PortBindings") not in (None, {}):
        raise IsolationError("per-turn proxy publishes a host port")
    proxy_mounts = proxy.get("Mounts", [])
    if not isinstance(proxy_mounts, list) or any(
        not isinstance(item, Mapping)
        or item.get("Type") != "tmpfs"
        or item.get("Destination") != "/tmp"
        for item in proxy_mounts
    ):
        raise IsolationError("per-turn proxy has a host-backed mount")
    bind_mounts = [
        item
        for item in agent.get("Mounts", [])
        if isinstance(item, Mapping) and item.get("Type") == "bind"
    ]
    expected_sources = {
        (str(repo), CONTAINER_WORKSPACE + "/repo"),
    }
    if skill is not None:
        expected_sources.add((str(skill), CONTAINER_SKILL))
    observed_sources = {
        (str(item.get("Source")), str(item.get("Destination"))) for item in bind_mounts
    }
    if observed_sources != expected_sources or any(item.get("RW") is not False for item in bind_mounts):
        raise IsolationError("per-turn nested read-only mounts are not exact")
    inspected = json.dumps({"agent": agent, "proxy": proxy}, sort_keys=True)
    forbidden_fragments = (
        str(_runtime_trust(runtime)["docker_socket"]["path"]),
        "/oracles/",
        "operator-answers.json",
        "/raw/",
    )
    if any(fragment in inspected for fragment in forbidden_fragments):
        raise IsolationError("a forbidden host path reached a container")
    return {
        "agent_to_proxy_health": True,
        "agent_internal_only": True,
        "proxy_default_route": "egress",
        "proxy_internal_bind_only": True,
        "isolated_gateway": True,
        "agent_dns_egress_blocked": True,
        "mounts_exact": True,
    }


def run_turn(
    run_dir: Path,
    state: Mapping[str, Any],
    prompt: str,
    turn: int,
    initial: bool,
    timeout_seconds: int,
    credential: bytearray,
) -> Dict[str, Any]:
    """Run one Codex turn through the credential-free, disposable boundary."""

    started_at = _now()
    sentinel = bytearray()
    capability = bytearray()
    lock_descriptor: Optional[int] = None
    termination_state: Optional[_TerminationState] = None
    try:
        if not isinstance(credential, bytearray):
            raise IsolationError("dedicated credential must be a mutable bytearray")
        if not API_KEY_MIN_BYTES <= len(credential) <= API_KEY_MAX_BYTES or any(
            byte < 33 or byte > 126 for byte in credential
        ):
            raise IsolationError("dedicated credential is not bounded printable ASCII")
        if (
            not isinstance(turn, int)
            or isinstance(turn, bool)
            or turn < 1
            or turn > MAX_TURNS_PER_RUN
        ):
            raise IsolationError("turn is outside the frozen evaluation limit")
        _run, runtime, _results_root, state_root = _recovery_runtime(run_dir, state)
        termination_state = _install_termination_handlers()
        lock_descriptor = _acquire_evaluation_lock(state_root)
        # Revalidate trust after waiting, install the signal latch, and clean
        # this state scope before inspecting the journal. This also recovers a
        # crash that published a snapshot pointer before state.json advanced.
        _run, runtime, _results_root, state_root = _recovery_runtime(run_dir, state)
        startup_scavenge = _scavenge_resources(runtime)
        context = _turn_context(
            run_dir, state, prompt, turn, initial, timeout_seconds
        )
        runtime = context["runtime"]
        run = context["run"]
        repo = context["repo"]
        skill = context["skill"]
        workspace_target = context["workspace"]
        state_target = context["codex_home"]
        nonce = secrets.token_hex(8)
        internal_name = _resource_name(context["run_id"], turn, "internal", nonce)
        egress_name = _resource_name(context["run_id"], turn, "egress", nonce)
        proxy_name = _resource_name(context["run_id"], turn, "proxy", nonce)
        agent_name = _resource_name(context["run_id"], turn, "agent", nonce)
        sentinel.extend(secrets.token_hex(32).encode("ascii"))
        capability.extend(secrets.token_hex(32).encode("ascii"))
        secret_values = (credential, sentinel, capability)

        isolation_record: Dict[str, Any] = {
            "contract": "docker-agent-proxy-v1",
            "succeeded": False,
            "credential_exposure_detected": False,
            "active_probe": None,
            "imports": None,
            "boundary": None,
            "exports": None,
            "snapshot_commit": None,
            "agent": {"name": agent_name, "teardown": None},
            "proxy": {"name": proxy_name, "teardown": None},
            "networks": {
                "internal": {"name": internal_name, "teardown": None},
                "egress": {"name": egress_name, "teardown": None},
            },
            "teardown": {"succeeded": False},
            "startup_scavenge": startup_scavenge,
        }
        exit_code: Optional[int] = None
        timed_out = False
        launch_error: Optional[str] = None
        transport_violation: Optional[str] = None
        stdout_data: Optional[bytes] = None
        stderr_data: Optional[bytes] = None
        last_data: Optional[bytes] = None
        observed_session: Optional[str] = None
        proxy_process: Optional[ManagedProcess] = None
        workspace_after: Optional[bytes] = None
        state_after: Optional[bytes] = None
        workspace_before: Optional[bytes] = None
        state_before: Optional[bytes] = None
        phase = "preflight"
        teardown: Dict[str, Any] = {}
        try:
            workspace_before, workspace_identity = _snapshot_with_identity(
                workspace_target, max_bytes=WORKSPACE_LIMIT_BYTES
            )
            state_before, state_identity = _snapshot_with_identity(
                state_target, max_bytes=STATE_LIMIT_BYTES
            )
            _validated_snapshot_archive(
                workspace_before,
                root=CONTAINER_WORKSPACE,
                max_bytes=WORKSPACE_LIMIT_BYTES,
                secrets_=(),
            )
            _validated_snapshot_archive(
                state_before,
                root=CONTAINER_CODEX_HOME,
                max_bytes=STATE_LIMIT_BYTES,
                secrets_=(),
            )
            repo_identity = _mount_source_identity(repo)
            skill_identity = _mount_source_identity(skill) if skill is not None else None
            phase = "nested sandbox preflight"
            isolation_record["active_probe"] = _active_probe(
                runtime, repo, skill, context["run_id"]
            )
            _revalidate_mount_source(repo_identity)
            if skill_identity is not None:
                _revalidate_mount_source(skill_identity)

            phase = "network creation"
            _create_network(runtime, egress_name, internal=False)
            _create_network(runtime, internal_name, internal=True)
            phase = "credential proxy launch"
            proxy_process = _start_proxy(
                runtime,
                name=proxy_name,
                egress_network=egress_name,
                internal_network=internal_name,
                model=context["model"],
                credential=credential,
                sentinel=sentinel,
                capability=capability,
            )
            phase = "agent launch"
            agent_arguments = _agent_container_arguments(
                runtime,
                name=agent_name,
                network=internal_name,
                repo=repo,
                skill=skill,
                detached=True,
            )
            _revalidate_mount_source(repo_identity)
            if skill_identity is not None:
                _revalidate_mount_source(skill_identity)
            _docker_capture(runtime, agent_arguments, timeout=60)
            _revalidate_mount_source(repo_identity)
            _verify_container_mount_manifest(
                runtime,
                agent_name,
                CONTAINER_WORKSPACE + "/repo",
                repo_identity,
            )
            if skill_identity is not None:
                _revalidate_mount_source(skill_identity)
                _verify_container_mount_manifest(
                    runtime, agent_name, CONTAINER_SKILL, skill_identity
                )

            phase = "container boundary verification"
            isolation_record["boundary"] = _verify_turn_boundary(
                runtime,
                agent_name=agent_name,
                proxy_name=proxy_name,
                internal_name=internal_name,
                egress_name=egress_name,
                repo=repo,
                skill=skill,
            )
            phase = "snapshot import"
            _revalidate_snapshot_identity(workspace_identity)
            workspace_import = _import_snapshot(
                runtime,
                agent_name,
                CONTAINER_WORKSPACE,
                workspace_before,
                max_bytes=WORKSPACE_LIMIT_BYTES,
                protected=("repo",),
            )
            _revalidate_snapshot_identity(workspace_identity)
            _revalidate_snapshot_identity(state_identity)
            state_import = _import_snapshot(
                runtime,
                agent_name,
                CONTAINER_CODEX_HOME,
                state_before,
                max_bytes=STATE_LIMIT_BYTES,
                protected=("skills",),
            )
            _revalidate_snapshot_identity(state_identity)
            isolation_record["imports"] = {
                "workspace": workspace_import,
                "state": state_import,
            }

            phase = "Codex execution"
            last_path = "/tmp/turn-{:03d}-last-message.txt".format(turn)
            codex_arguments = _codex_exec_arguments(
                model=context["model"],
                base_url="http://{}:{}/v1".format(
                    CONTAINER_PROXY_HOST, CONTAINER_PROXY_PORT
                ),
                capability=capability.decode("ascii"),
                last_message=last_path,
                initial=initial,
                session_id=context["session_id"],
            )
            managed = _docker_start(
                runtime,
                _agent_exec_arguments(
                    runtime, agent_name, ("codex", *codex_arguments), interactive=True
                ),
                input_data=context["prompt"],
                stdout_limit=MAX_STDOUT_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
            )
            codex_capture = _finish_process(managed, timeout_seconds)
            _revalidate_mount_source(repo_identity)
            if skill_identity is not None:
                _revalidate_mount_source(skill_identity)
            exit_code = codex_capture.returncode
            timed_out = codex_capture.timed_out
            stdout_data = codex_capture.stdout
            stderr_data = codex_capture.stderr
            try:
                observed_session = _extract_session_id(stdout_data)
            except IsolationError:
                launch_error = "Codex execution returned inconsistent session IDs"
            if codex_capture.limited_stream is not None:
                transport_violation = "{} exceeded its frozen byte limit".format(
                    codex_capture.limited_stream
                )
            elif codex_capture.input_error is not None:
                transport_violation = "prompt transport failed"
            elif timed_out:
                launch_error = "Codex execution timed out"
            elif exit_code != 0:
                launch_error = "Codex execution failed"
            else:
                if observed_session is None:
                    launch_error = "Codex execution returned no pinned session ID"
                elif not initial and observed_session != str(context["session_id"]).lower():
                    launch_error = "Codex execution returned a different session ID"

            if _contains_secret(stdout_data or b"", secret_values) or _contains_secret(
                stderr_data or b"", secret_values
            ):
                isolation_record["credential_exposure_detected"] = True
                transport_violation = "credential material reached agent output"

            if not timed_out and transport_violation is None:
                phase = "last message read"
                last_capture = _docker_capture(
                    runtime,
                    _agent_exec_arguments(
                        runtime,
                        agent_name,
                        (
                            "python3",
                            "-I",
                            "-S",
                            "-c",
                            _READ_LAST_MESSAGE_PROGRAM,
                            last_path,
                            str(MAX_LAST_MESSAGE_BYTES),
                        ),
                    ),
                    timeout=30,
                    stdout_limit=MAX_LAST_MESSAGE_BYTES,
                    check=False,
                )
                if (
                    last_capture.returncode == 0
                    and not last_capture.timed_out
                    and last_capture.limited_stream is None
                ):
                    last_data = last_capture.stdout
                    if _contains_secret(last_data, secret_values):
                        isolation_record["credential_exposure_detected"] = True
                        transport_violation = "credential material reached the last message"
                elif exit_code == 0:
                    launch_error = "last message read failed"

            if not timed_out and transport_violation is None:
                phase = "snapshot export"
                workspace_after, workspace_export = _export_snapshot(
                    runtime,
                    agent_name,
                    CONTAINER_WORKSPACE,
                    max_bytes=WORKSPACE_LIMIT_BYTES,
                    excludes=("repo", ".eval-transport"),
                    secrets_=secret_values,
                )
                state_after, state_export = _export_snapshot(
                    runtime,
                    agent_name,
                    CONTAINER_CODEX_HOME,
                    max_bytes=STATE_LIMIT_BYTES,
                    excludes=("skills",) if skill is not None else (),
                    secrets_=secret_values,
                )
                isolation_record["exports"] = {
                    "workspace": workspace_export,
                    "state": state_export,
                }
        except _CredentialExposure:
            isolation_record["credential_exposure_detected"] = True
            transport_violation = "credential material reached a snapshot"
        except _TerminationRequested:
            raise
        except Exception:
            if launch_error is None and transport_violation is None:
                launch_error = "{} failed".format(phase)
        finally:
            deferred = _defer_termination()
            cleanup_termination: Optional[_TerminationRequested] = None
            try:
                teardown["agent"], pending = _attempt_cleanup(
                    lambda: _remove_container(runtime, agent_name),
                    {"attempted": True, "succeeded": False},
                )
                cleanup_termination = cleanup_termination or pending
                teardown["proxy"], pending = _attempt_cleanup(
                    lambda: _remove_container(runtime, proxy_name),
                    {"attempted": True, "succeeded": False},
                )
                cleanup_termination = cleanup_termination or pending
                if proxy_process is not None:
                    proxy_capture, pending = _attempt_cleanup(
                        lambda: _finish_process(proxy_process, 5, terminate=True),
                        Capture(None, b"", b"", timed_out=True),
                    )
                    cleanup_termination = cleanup_termination or pending
                    teardown["proxy_attach"] = {
                        "succeeded": not proxy_capture.timed_out
                        and proxy_capture.limited_stream is None,
                        "exit_code": proxy_capture.returncode,
                        "timed_out": proxy_capture.timed_out,
                    }
                    if _contains_secret(
                        proxy_capture.stdout, secret_values
                    ) or _contains_secret(proxy_capture.stderr, secret_values):
                        isolation_record["credential_exposure_detected"] = True
                        transport_violation = (
                            "credential proxy emitted credential material"
                        )
                teardown["internal_network"], pending = _attempt_cleanup(
                    lambda: _remove_network(runtime, internal_name),
                    {"attempted": True, "succeeded": False},
                )
                cleanup_termination = cleanup_termination or pending
                teardown["egress_network"], pending = _attempt_cleanup(
                    lambda: _remove_network(runtime, egress_name),
                    {"attempted": True, "succeeded": False},
                )
                cleanup_termination = cleanup_termination or pending
            finally:
                try:
                    _resume_termination(deferred)
                except _TerminationRequested as error:
                    cleanup_termination = cleanup_termination or error
            if cleanup_termination is not None:
                raise cleanup_termination

        teardown_succeeded = all(
            isinstance(item, Mapping) and item.get("succeeded") is True
            for item in teardown.values()
        )
        isolation_record["agent"]["teardown"] = teardown.get("agent")
        isolation_record["proxy"]["teardown"] = teardown.get("proxy")
        isolation_record["networks"]["internal"]["teardown"] = teardown.get(
            "internal_network"
        )
        isolation_record["networks"]["egress"]["teardown"] = teardown.get(
            "egress_network"
        )
        isolation_record["teardown"] = {
            "succeeded": teardown_succeeded,
            "proxy_attach": teardown.get("proxy_attach"),
        }
        if not teardown_succeeded and launch_error is None:
            launch_error = "isolation teardown failed"

        if (
            transport_violation is None
            and exit_code is not None
            and not timed_out
            and teardown_succeeded
            and workspace_after is not None
            and state_after is not None
            and workspace_before is not None
            and state_before is not None
        ):
            try:
                _revalidate_snapshot_identity(workspace_identity)
                _revalidate_snapshot_identity(state_identity)
                committed_session = (
                    observed_session
                    if isinstance(observed_session, str)
                    and _THREAD_ID.fullmatch(observed_session) is not None
                    else context["session_id"]
                )
                isolation_record["snapshot_commit"] = _commit_snapshot_pair(
                    context["snapshot_journal"],
                    workspace_target,
                    state_target,
                    workspace_after,
                    state_after,
                    turn=turn,
                    session_id=committed_session,
                    secrets_=secret_values,
                )
            except _TerminationRequested:
                raise
            except Exception:
                launch_error = "atomic snapshot commit failed"

        stdout_path: Optional[str] = None
        stderr_path: Optional[str] = None
        last_path_result: Optional[str] = None
        output_has_secret = any(
            _contains_secret(value, secret_values)
            for value in (stdout_data or b"", stderr_data or b"", last_data or b"")
        )
        if output_has_secret:
            isolation_record["credential_exposure_detected"] = True
            transport_violation = "credential material reached persisted output"
        elif stdout_data is not None and stderr_data is not None:
            try:
                stdout_path = _atomic_raw_write(
                    run,
                    "raw/turn-{:03d}.jsonl".format(turn),
                    stdout_data,
                )
                stderr_path = _atomic_raw_write(
                    run,
                    "raw/turn-{:03d}.stderr.txt".format(turn),
                    stderr_data,
                )
                if last_data is not None:
                    last_path_result = _atomic_raw_write(
                        run,
                        "raw/turn-{:03d}.last-message.txt".format(turn),
                        last_data,
                    )
            except _TerminationRequested:
                raise
            except IsolationError:
                launch_error = launch_error or "raw output persistence failed"

        isolation_record["succeeded"] = (
            launch_error is None
            and transport_violation is None
            and exit_code == 0
            and not timed_out
            and teardown_succeeded
            and isolation_record.get("snapshot_commit") is not None
            and isolation_record.get("credential_exposure_detected") is False
        )
        return {
            "turn": turn,
            "initial": initial,
            "started_at": started_at,
            "finished_at": _now(),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout_path,
            "stderr": stderr_path,
            "last_message": last_path_result,
            "session_id": observed_session,
            "launch_error": launch_error,
            "transport_violation": transport_violation,
            "isolation": isolation_record,
        }
    finally:
        deferred = _defer_termination()
        cleanup_error: Optional[BaseException] = None
        try:
            try:
                _release_evaluation_lock(lock_descriptor)
            except BaseException as error:
                cleanup_error = error
            finally:
                if isinstance(credential, bytearray):
                    _zero(credential)
                _zero(sentinel)
                _zero(capability)
        finally:
            pending_signum = _end_termination_deferral(deferred)
            if termination_state is not None:
                try:
                    _restore_termination_handlers(termination_state)
                except BaseException as error:
                    cleanup_error = cleanup_error or error
        if pending_signum is not None:
            raise _TerminationRequested(
                "evaluation interrupted by signal {}".format(pending_signum)
            )
        if cleanup_error is not None:
            raise cleanup_error
