"""Read-only runtime checks for Diagram Grill.

This module reports missing or incompatible dependencies. It never installs,
updates, or otherwise mutates the user's environment.
"""

from __future__ import annotations

import importlib
import hashlib
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


REQUIRED_DIAGRAMS_VERSION = "0.25.1"
REQUIRED_JSONSCHEMA_VERSION = "4.25.1"
REQUIRED_GRAPHVIZ_PYTHON_VERSION = "0.20.3"
MINIMUM_PYTHON_VERSION = (3, 9)
DEFAULT_DOT_TIMEOUT_SECONDS = 2.0
POST_KILL_GRACE_SECONDS = 0.25


@dataclass(frozen=True)
class CheckResult:
    """The immutable outcome of one preflight check."""

    name: str
    ok: bool
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None


@dataclass(frozen=True)
class ExecutableIdentity:
    """Filesystem identity used to detect executable replacement or mutation."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class PreflightResult:
    """The immutable aggregate result returned by :func:`run_preflight`."""

    checks: Tuple[CheckResult, ...]
    dot_path: Optional[str] = None
    dot_identity: Optional[ExecutableIdentity] = None
    dot_sha256: Optional[str] = None
    ok: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ok", all(check.ok for check in self.checks))


VersionGetter = Callable[[str], str]
ModuleImporter = Callable[[str], Any]
CommandRunner = Callable[..., subprocess.CompletedProcess]
ExecutableFinder = Callable[..., Optional[str]]
PosixGroupLookup = Callable[[int], Any]
PosixUserLookup = Callable[[str], Any]
PosixAllUsersLookup = Callable[[], Sequence[Any]]
PosixAclChecker = Callable[[Path, str], Optional[str]]


def run_preflight(
    *,
    python_version: Optional[Sequence[int]] = None,
    version_getter: Optional[VersionGetter] = None,
    module_importer: Optional[ModuleImporter] = None,
    command_runner: Optional[CommandRunner] = None,
    executable_finder: Optional[ExecutableFinder] = None,
    environment: Optional[Mapping[str, str]] = None,
    forbidden_roots: Optional[Sequence[Path]] = None,
    dot_timeout_seconds: float = DEFAULT_DOT_TIMEOUT_SECONDS,
) -> PreflightResult:
    """Run all dependency checks without changing the environment.

    Injectable callables keep the checks deterministic in tests and allow a
    host application to provide equivalent read-only implementations.
    """

    resolved_python_version = python_version or sys.version_info
    resolved_version_getter = version_getter or metadata.version
    resolved_module_importer = module_importer or importlib.import_module
    resolved_command_runner = command_runner or _run_bounded_command
    resolved_executable_finder = executable_finder or shutil.which
    resolved_environment = environment if environment is not None else os.environ
    resolved_forbidden_roots = (
        runtime_forbidden_roots(Path.cwd())
        if forbidden_roots is None
        else _normalized_roots(forbidden_roots)
    )

    dot_check, dot_path, dot_identity, dot_sha256 = _check_dot(
        resolved_command_runner,
        resolved_executable_finder,
        resolved_environment,
        resolved_forbidden_roots,
        dot_timeout_seconds,
    )
    checks = (
        _check_python(resolved_python_version),
        _check_distribution_version(
            "diagrams",
            REQUIRED_DIAGRAMS_VERSION,
            resolved_version_getter,
        ),
        _check_distribution_version(
            "jsonschema",
            REQUIRED_JSONSCHEMA_VERSION,
            resolved_version_getter,
        ),
        _check_graphviz_python(
            resolved_version_getter,
            resolved_module_importer,
        ),
        dot_check,
    )
    return PreflightResult(
        checks=checks,
        dot_path=dot_path,
        dot_identity=dot_identity,
        dot_sha256=dot_sha256,
    )


def runtime_forbidden_roots(workspace: Path) -> Tuple[Path, ...]:
    """Return workspace and enclosing repository roots that cannot supply tools."""

    workspace_root = Path(workspace).resolve()
    roots = [workspace_root]
    for candidate in (workspace_root,) + tuple(workspace_root.parents):
        if candidate == Path(candidate.anchor):
            continue
        if (candidate / ".git").exists():
            if candidate not in roots:
                roots.append(candidate)
            break
    return tuple(roots)


def _normalized_roots(roots: Sequence[Path]) -> Tuple[Path, ...]:
    normalized = []
    for root in roots:
        candidate = Path(root).resolve()
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def _check_python(python_version: Sequence[int]) -> CheckResult:
    actual_parts = tuple(int(part) for part in python_version[:3])
    actual = ".".join(str(part) for part in actual_parts)
    expected = ">=3.9"

    if actual_parts[:2] >= MINIMUM_PYTHON_VERSION:
        return CheckResult(
            name="python",
            ok=True,
            expected=expected,
            actual=actual,
            message=f"Python {actual} satisfies the {expected} requirement.",
        )

    return CheckResult(
        name="python",
        ok=False,
        expected=expected,
        actual=actual,
        message=(
            f"Python {actual} is too old. Use Python 3.9 or newer before "
            "running Diagram Grill."
        ),
    )


def _check_distribution_version(
    distribution: str,
    required_version: str,
    version_getter: VersionGetter,
) -> CheckResult:
    try:
        actual = str(version_getter(distribution))
    except metadata.PackageNotFoundError:
        return CheckResult(
            name=distribution,
            ok=False,
            expected=required_version,
            message=(
                f"Python package '{distribution}' is not installed. Install "
                f"the project dependencies that provide {distribution}=={required_version}."
            ),
        )
    except Exception as exc:
        return CheckResult(
            name=distribution,
            ok=False,
            expected=required_version,
            message=(
                f"Could not read the installed '{distribution}' version "
                f"({type(exc).__name__}: {exc}). Verify the active Python environment."
            ),
        )

    if actual == required_version:
        return CheckResult(
            name=distribution,
            ok=True,
            expected=required_version,
            actual=actual,
            message=f"{distribution}=={actual} is installed.",
        )

    return CheckResult(
        name=distribution,
        ok=False,
        expected=required_version,
        actual=actual,
        message=(
            f"Installed {distribution} version is {actual}; Diagram Grill "
            f"requires exactly {distribution}=={required_version}. Use the "
            "pinned project dependency version."
        ),
    )


def _check_graphviz_python(
    version_getter: VersionGetter,
    module_importer: ModuleImporter,
) -> CheckResult:
    try:
        actual = str(version_getter("graphviz"))
    except metadata.PackageNotFoundError:
        return CheckResult(
            name="graphviz_python",
            ok=False,
            expected=REQUIRED_GRAPHVIZ_PYTHON_VERSION,
            message=(
                "Python package 'graphviz' is not installed. Install the pinned "
                "project dependencies that provide graphviz=={}."
            ).format(REQUIRED_GRAPHVIZ_PYTHON_VERSION),
        )
    except Exception as exc:
        return CheckResult(
            name="graphviz_python",
            ok=False,
            expected=REQUIRED_GRAPHVIZ_PYTHON_VERSION,
            message=(
                "Could not read the installed 'graphviz' version ({}: {}). Verify "
                "the active Python environment."
            ).format(type(exc).__name__, exc),
        )

    if actual != REQUIRED_GRAPHVIZ_PYTHON_VERSION:
        return CheckResult(
            name="graphviz_python",
            ok=False,
            expected=REQUIRED_GRAPHVIZ_PYTHON_VERSION,
            actual=actual,
            message=(
                "Installed graphviz version is {}; Diagram Grill requires exactly "
                "graphviz=={}. Use the pinned project dependency version."
            ).format(actual, REQUIRED_GRAPHVIZ_PYTHON_VERSION),
        )

    try:
        module_importer("graphviz")
    except Exception as exc:
        return CheckResult(
            name="graphviz_python",
            ok=False,
            expected=REQUIRED_GRAPHVIZ_PYTHON_VERSION,
            actual=actual,
            message=(
                "graphviz=={} is installed but cannot be imported "
                f"({type(exc).__name__}: {exc}). Install it in the active "
                "Python environment before rendering."
            ).format(REQUIRED_GRAPHVIZ_PYTHON_VERSION),
        )

    return CheckResult(
        name="graphviz_python",
        ok=True,
        expected=REQUIRED_GRAPHVIZ_PYTHON_VERSION,
        actual=actual,
        message=(
            "graphviz=={} is installed and importable."
        ).format(REQUIRED_GRAPHVIZ_PYTHON_VERSION),
    )


def _check_dot(
    command_runner: CommandRunner,
    executable_finder: ExecutableFinder,
    environment: Mapping[str, str],
    forbidden_roots: Sequence[Path],
    timeout_seconds: float,
) -> Tuple[
    CheckResult,
    Optional[str],
    Optional[ExecutableIdentity],
    Optional[str],
]:
    try:
        candidate = executable_finder("dot", path=environment.get("PATH"))
    except (OSError, TypeError, ValueError) as exc:
        return _dot_failure(
            "Could not resolve Graphviz 'dot' on PATH "
            "({}: {}). Check the Graphviz installation and PATH.".format(
                type(exc).__name__, exc
            ),
            expected="trusted absolute executable on PATH",
        )

    if not candidate:
        return _dot_failure(
            "Graphviz 'dot' was not found on PATH. Install Graphviz and ensure "
            "`dot -V` succeeds before rendering.",
            expected="trusted absolute executable on PATH",
        )

    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        return _dot_failure(
            "Refusing the relative Graphviz 'dot' candidate {!r}. PATH must resolve "
            "dot to an absolute executable outside the workspace and repository.".format(
                str(candidate)
            ),
            expected="trusted absolute executable on PATH",
            actual=str(candidate),
        )

    absolute_candidate = Path(os.path.abspath(str(candidate_path)))
    if absolute_candidate.stem.casefold() != "dot":
        return _dot_failure(
            "PATH resolved 'dot' to an unexpected executable name: {}. Check the "
            "Graphviz installation and PATH.".format(absolute_candidate),
            expected="trusted absolute Graphviz dot executable",
            actual=str(absolute_candidate),
        )

    try:
        canonical_candidate = absolute_candidate.resolve(strict=True)
    except OSError as exc:
        return _dot_failure(
            "Could not resolve the Graphviz 'dot' candidate {} ({}: {}). Check the "
            "Graphviz installation and PATH.".format(
                absolute_candidate, type(exc).__name__, exc
            ),
            expected="existing executable on PATH",
            actual=str(absolute_candidate),
        )

    if canonical_candidate.stem.casefold() != "dot":
        return _dot_failure(
            "The canonical Graphviz executable has an unexpected name: {}. Use an "
            "installation whose resolved executable is named dot.".format(
                canonical_candidate
            ),
            expected="canonical Graphviz dot executable",
            actual=str(canonical_candidate),
        )

    if _inside_any_root(absolute_candidate, forbidden_roots) or _inside_any_root(
        canonical_candidate, forbidden_roots
    ):
        return _dot_failure(
            "Refusing Graphviz 'dot' from the workspace or repository: {}. Use a "
            "system-managed Graphviz installation outside untrusted project "
            "content.".format(absolute_candidate),
            expected="executable outside workspace and repository",
            actual=str(absolute_candidate),
        )

    trust_failure = posix_executable_trust_failure(canonical_candidate)
    if trust_failure is not None:
        return _dot_failure(
            trust_failure,
            expected="locally trusted canonical executable path",
            actual=str(canonical_candidate),
        )

    if not canonical_candidate.is_file() or not os.access(
        str(canonical_candidate), os.X_OK
    ):
        return _dot_failure(
            "Graphviz 'dot' is not an executable file: {}. Check the Graphviz "
            "installation and executable permissions.".format(canonical_candidate),
            expected="executable file on PATH",
            actual=str(canonical_candidate),
        )

    try:
        dot_identity, dot_sha256 = capture_executable(canonical_candidate)
    except (OSError, ValueError) as exc:
        return _dot_failure(
            "Could not establish the Graphviz executable identity ({}: {}).".format(
                type(exc).__name__, exc
            ),
            expected="stable regular executable with SHA-256",
            actual=str(canonical_candidate),
        )

    trust_failure = posix_executable_trust_failure(canonical_candidate)
    if trust_failure is not None:
        return _dot_failure(
            trust_failure,
            expected="locally trusted canonical executable path",
            actual=str(canonical_candidate),
        )

    command = [str(canonical_candidate), "-V"]
    command_environment = _minimal_dot_environment(
        environment,
        canonical_candidate.parent,
    )
    try:
        completed = command_runner(
            command,
            cwd=str(canonical_candidate.parent),
            env=command_environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return _dot_failure(
            "The resolved Graphviz 'dot' executable disappeared before it could "
            "run: {}. Check the Graphviz installation.".format(canonical_candidate),
            expected="resolved executable remains available",
            actual=str(canonical_candidate),
        )
    except subprocess.TimeoutExpired:
        return _dot_failure(
            "`dot -V` did not finish within {:g} seconds. Check the Graphviz "
            "installation and PATH, then retry.".format(timeout_seconds),
            expected="responsive resolved executable",
            actual=str(canonical_candidate),
        )
    except OSError as exc:
        return _dot_failure(
            "Could not execute `dot -V` ({}: {}). Check the Graphviz installation "
            "and executable permissions.".format(type(exc).__name__, exc),
            expected="executable resolved path",
            actual=str(canonical_candidate),
        )

    try:
        post_identity, post_sha256 = capture_executable(canonical_candidate)
    except (OSError, ValueError) as exc:
        return _dot_failure(
            "Graphviz 'dot' changed or disappeared during its version check "
            "({}: {}).".format(type(exc).__name__, exc),
            expected="stable executable identity during preflight",
            actual=str(canonical_candidate),
        )
    if post_identity != dot_identity or post_sha256 != dot_sha256:
        return _dot_failure(
            "Graphviz 'dot' changed during its version check; refusing an unstable "
            "runtime executable.",
            expected="stable executable identity during preflight",
            actual=str(canonical_candidate),
        )

    output = _command_output(completed)
    if completed.returncode != 0:
        detail = f" Output: {output}" if output else ""
        return _dot_failure(
            "`dot -V` failed with exit code {}. Repair the Graphviz installation "
            "or PATH.{}".format(completed.returncode, detail),
            expected="successful `dot -V`",
            actual="exit code {}".format(completed.returncode),
        )

    match = re.search(r"\bgraphviz\s+version\s+([^\s(]+)", output, re.IGNORECASE)
    if match is None:
        detail = f" Output: {output}" if output else " No output was returned."
        return _dot_failure(
            "`dot -V` ran, but its Graphviz version could not be determined. Verify "
            "that PATH points to a Graphviz `dot` executable.{}".format(detail),
            expected="parseable Graphviz version",
            actual=str(canonical_candidate),
        )

    version = match.group(1)
    return (
        CheckResult(
            name="dot",
            ok=True,
            expected="trusted canonical executable outside workspace and repository",
            actual=version,
            message=(
                "Graphviz dot {} is available at the verified path {}."
            ).format(version, canonical_candidate),
        ),
        str(canonical_candidate),
        dot_identity,
        dot_sha256,
    )


def _dot_failure(
    message: str,
    *,
    expected: str,
    actual: Optional[str] = None,
) -> Tuple[CheckResult, None, None, None]:
    return (
        CheckResult(
            name="dot",
            ok=False,
            expected=expected,
            actual=actual,
            message=message,
        ),
        None,
        None,
        None,
    )


def posix_executable_trust_failure(
    path: Path,
    *,
    effective_uid: Optional[int] = None,
    group_lookup: Optional[PosixGroupLookup] = None,
    user_lookup: Optional[PosixUserLookup] = None,
    all_users_lookup: Optional[PosixAllUsersLookup] = None,
    platform_name: Optional[str] = None,
    acl_checker: Optional[PosixAclChecker] = None,
) -> Optional[str]:
    """Return why a canonical executable path is not locally trusted on POSIX."""

    if os.name != "posix":
        return None

    resolved_effective_uid = os.geteuid() if effective_uid is None else effective_uid
    resolved_platform = sys.platform if platform_name is None else platform_name
    resolved_acl_checker = acl_checker or _posix_acl_trust_failure
    trusted_uids = frozenset((0, resolved_effective_uid))
    for component in (path,) + tuple(path.parents):
        try:
            component_stat = component.stat()
        except OSError as exc:
            return "Could not inspect runtime path permissions for {} ({}: {}).".format(
                component, type(exc).__name__, exc
            )
        if component_stat.st_mode & stat.S_IWOTH:
            return (
                "Refusing Graphviz 'dot' because its canonical path crosses a "
                "world-writable component: {}."
            ).format(component)
        if component_stat.st_uid not in trusted_uids:
            return (
                "Refusing Graphviz 'dot' because its canonical path crosses a "
                "component owned by untrusted uid {}: {}."
            ).format(component_stat.st_uid, component)
        acl_failure = resolved_acl_checker(component, resolved_platform)
        if acl_failure is not None:
            return acl_failure
        if component_stat.st_mode & stat.S_IWGRP:
            group_failure = _posix_group_write_trust_failure(
                component,
                component_stat,
                resolved_effective_uid,
                resolved_platform,
                group_lookup=group_lookup,
                user_lookup=user_lookup,
                all_users_lookup=all_users_lookup,
            )
            if group_failure is not None:
                return group_failure
    return None


def _posix_group_write_trust_failure(
    component: Path,
    component_stat: os.stat_result,
    effective_uid: int,
    platform_name: str,
    *,
    group_lookup: Optional[PosixGroupLookup],
    user_lookup: Optional[PosixUserLookup] = None,
    all_users_lookup: Optional[PosixAllUsersLookup] = None,
) -> Optional[str]:
    group_id = int(component_stat.st_gid)
    homebrew_root = Path("/opt/homebrew")
    if (
        platform_name != "darwin"
        or (
            component != homebrew_root
            and homebrew_root not in component.parents
        )
        or int(component_stat.st_uid) not in (0, effective_uid)
    ):
        return (
            "Refusing Graphviz 'dot' because its canonical path crosses a "
            "group-writable component outside the narrow macOS Homebrew "
            "administrator exception: {}."
        ).format(component)

    try:
        if group_lookup is None:
            import grp

            group_lookup = grp.getgrgid
        if user_lookup is None or all_users_lookup is None:
            import pwd

            user_lookup = user_lookup or pwd.getpwnam
            all_users_lookup = all_users_lookup or pwd.getpwall
        group = group_lookup(group_id)
        resolved_gid = _posix_numeric_id(group, "gr_gid")
        if resolved_gid != group_id:
            raise ValueError("group lookup returned a different gid")
        group_name = str(group.gr_name)
        member_names = tuple(group.gr_mem)
    except Exception as exc:
        return _posix_group_lookup_failure(component, group_id, exc)

    if group_id != 80 or group_name != "admin":
        return (
            "Refusing Graphviz 'dot' because macOS Homebrew group-writable "
            "component {} is owned by unexpected group {!r} (gid {})."
        ).format(component, group_name, group_id)

    trusted_uids = frozenset((0, effective_uid))
    try:
        if any(not isinstance(name, str) or not name for name in member_names):
            raise ValueError("group membership contains an invalid user name")
        supplementary_users = tuple(user_lookup(name) for name in member_names)
        primary_users = tuple(
            user for user in all_users_lookup()
            if _posix_numeric_id(user, "pw_gid") == group_id
        )
        untrusted = sorted(
            {
                str(user.pw_name)
                for user in supplementary_users + primary_users
                if _posix_numeric_id(user, "pw_uid") not in trusted_uids
            }
        )
    except Exception as exc:
        return _posix_group_lookup_failure(component, group_id, exc)
    if untrusted:
        return (
            "Refusing Graphviz 'dot' because macOS Homebrew group-writable "
            "component {} is writable by untrusted admin-group principal(s): {}."
        ).format(component, ", ".join(untrusted))
    return None


def _posix_acl_trust_failure(component: Path, platform_name: str) -> Optional[str]:
    if platform_name == "darwin":
        return _darwin_acl_trust_failure(component)
    if platform_name.startswith("linux"):
        try:
            names = os.listxattr(str(component), follow_symlinks=False)
        except (AttributeError, OSError) as exc:
            return (
                "Refusing Graphviz 'dot' because ACL metadata could not be "
                "inspected for {} ({}: {})."
            ).format(component, type(exc).__name__, exc)
        if any("acl" in str(name).casefold() for name in names):
            return (
                "Refusing Graphviz 'dot' because its canonical path crosses a "
                "component with extended ACL metadata: {}."
            ).format(component)
        return None
    return (
        "Refusing Graphviz 'dot' because extended ACL trust checks are not "
        "implemented for POSIX platform {!r}: {}."
    ).format(platform_name, component)


def _darwin_acl_trust_failure(component: Path) -> Optional[str]:
    import ctypes
    import errno

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        acl_get_file = libc.acl_get_file
        acl_get_file.argtypes = (ctypes.c_char_p, ctypes.c_int)
        acl_get_file.restype = ctypes.c_void_p
        acl_get_entry = libc.acl_get_entry
        acl_get_entry.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        acl_get_entry.restype = ctypes.c_int
        acl_free = libc.acl_free
        acl_free.argtypes = (ctypes.c_void_p,)
        acl_free.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        return (
            "Refusing Graphviz 'dot' because macOS ACL APIs are unavailable "
            "for {} ({}: {})."
        ).format(component, type(exc).__name__, exc)

    ctypes.set_errno(0)
    acl = acl_get_file(os.fsencode(str(component)), 0x00000100)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOENT:
            return None
        return (
            "Refusing Graphviz 'dot' because macOS ACL metadata could not be "
            "read for {} (errno {})."
        ).format(component, error_number)

    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        status = acl_get_entry(acl, 0, ctypes.byref(entry))
        if status == 0:
            return (
                "Refusing Graphviz 'dot' because its canonical path crosses a "
                "component with an extended macOS ACL: {}."
            ).format(component)
        error_number = ctypes.get_errno()
        if error_number != errno.EINVAL:
            return (
                "Refusing Graphviz 'dot' because macOS ACL entries could not be "
                "enumerated for {} (errno {})."
            ).format(component, error_number)
        return None
    finally:
        acl_free(acl)


def _posix_numeric_id(record: Any, field: str) -> int:
    value = getattr(record, field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("account lookup returned an invalid {}".format(field))
    return value


def _posix_group_lookup_failure(
    component: Path,
    group_id: int,
    error: Exception,
) -> str:
    return (
        "Refusing Graphviz 'dot' because POSIX membership for group-writable "
        "component {} (gid {}) could not be resolved completely ({}: {})."
    ).format(component, group_id, type(error).__name__, error)


def capture_executable(path: Path) -> Tuple[ExecutableIdentity, str]:
    """Read a canonical executable without following a final symlink."""

    executable = Path(path)
    if not executable.is_absolute():
        raise ValueError("executable path must be absolute")
    if executable.resolve(strict=True) != executable:
        raise ValueError("executable path is no longer canonical")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(executable), flags)
    try:
        before = _executable_identity(os.fstat(descriptor))
        if not stat.S_ISREG(before.mode):
            raise ValueError("executable is not a regular file")

        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

        after = _executable_identity(os.fstat(descriptor))
        if after != before:
            raise ValueError("executable changed while it was being hashed")
        return before, digest.hexdigest()
    finally:
        os.close(descriptor)


def _executable_identity(result: os.stat_result) -> ExecutableIdentity:
    return ExecutableIdentity(
        device=int(result.st_dev),
        inode=int(result.st_ino),
        mode=int(result.st_mode),
        size=int(result.st_size),
        mtime_ns=int(getattr(result, "st_mtime_ns", result.st_mtime * 1_000_000_000)),
        ctime_ns=int(getattr(result, "st_ctime_ns", result.st_ctime * 1_000_000_000)),
    )


def _run_bounded_command(
    command: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    capture_output: bool,
    text: bool,
    timeout: float,
    check: bool,
    shell: bool,
) -> subprocess.CompletedProcess:
    if not capture_output or shell:
        raise ValueError("bounded command runner requires captured output and shell=False")

    popen_arguments = {
        "cwd": cwd,
        "env": None if env is None else dict(env),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": text,
        "shell": False,
    }
    if os.name == "posix":
        popen_arguments["start_new_session"] = True
    elif os.name == "nt":
        popen_arguments["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    process = subprocess.Popen(list(command), **popen_arguments)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_bounded_process(process)
        stdout, stderr = _bounded_post_kill_drain(process, exc)
        raise subprocess.TimeoutExpired(
            list(command), timeout, output=stdout, stderr=stderr
        ) from exc

    completed = subprocess.CompletedProcess(
        list(command), process.returncode, stdout=stdout, stderr=stderr
    )
    if check:
        completed.check_returncode()
    return completed


def _minimal_dot_environment(
    source: Mapping[str, str],
    executable_directory: Path,
) -> Mapping[str, str]:
    """Build an environment that cannot inject loaders or Graphviz settings."""

    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(executable_directory),
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            value = source.get(key)
            if value:
                environment[key] = str(value)
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    return environment


def _terminate_bounded_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=POST_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.kill()
        return

    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=POST_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def _bounded_post_kill_drain(
    process: subprocess.Popen,
    timeout_error: subprocess.TimeoutExpired,
) -> Tuple[Any, Any]:
    stdout, stderr = timeout_error.output, timeout_error.stderr
    try:
        drained_stdout, drained_stderr = process.communicate(
            timeout=POST_KILL_GRACE_SECONDS
        )
        return drained_stdout, drained_stderr
    except subprocess.TimeoutExpired as drain_error:
        if drain_error.output is not None:
            stdout = drain_error.output
        if drain_error.stderr is not None:
            stderr = drain_error.stderr
        _close_process_pipes(process)
        try:
            process.wait(timeout=POST_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return stdout, stderr


def _close_process_pipes(process: subprocess.Popen) -> None:
    for pipe in (process.stdout, process.stderr, process.stdin):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _inside_any_root(candidate: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _command_output(completed: subprocess.CompletedProcess) -> str:
    parts = []
    for value in (completed.stdout, completed.stderr):
        if value:
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            parts.append(str(value).strip())
    return " ".join(parts)[:300]
