"""Read-only runtime checks for Diagram Grill.

This module reports missing or incompatible dependencies. It never installs,
updates, or otherwise mutates the user's environment.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Callable, Optional, Sequence, Tuple


REQUIRED_DIAGRAMS_VERSION = "0.25.1"
REQUIRED_JSONSCHEMA_VERSION = "4.25.1"
MINIMUM_PYTHON_VERSION = (3, 9)
DEFAULT_DOT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class CheckResult:
    """The immutable outcome of one preflight check."""

    name: str
    ok: bool
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None


@dataclass(frozen=True)
class PreflightResult:
    """The immutable aggregate result returned by :func:`run_preflight`."""

    checks: Tuple[CheckResult, ...]
    ok: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ok", all(check.ok for check in self.checks))


VersionGetter = Callable[[str], str]
ModuleImporter = Callable[[str], Any]
CommandRunner = Callable[..., subprocess.CompletedProcess]


def run_preflight(
    *,
    python_version: Optional[Sequence[int]] = None,
    version_getter: Optional[VersionGetter] = None,
    module_importer: Optional[ModuleImporter] = None,
    command_runner: Optional[CommandRunner] = None,
    dot_timeout_seconds: float = DEFAULT_DOT_TIMEOUT_SECONDS,
) -> PreflightResult:
    """Run all dependency checks without changing the environment.

    Injectable callables keep the checks deterministic in tests and allow a
    host application to provide equivalent read-only implementations.
    """

    resolved_python_version = python_version or sys.version_info
    resolved_version_getter = version_getter or metadata.version
    resolved_module_importer = module_importer or importlib.import_module
    resolved_command_runner = command_runner or subprocess.run

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
        _check_graphviz_import(resolved_module_importer),
        _check_dot(resolved_command_runner, dot_timeout_seconds),
    )
    return PreflightResult(checks=checks)


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


def _check_graphviz_import(module_importer: ModuleImporter) -> CheckResult:
    try:
        module_importer("graphviz")
    except Exception as exc:
        return CheckResult(
            name="graphviz_python",
            ok=False,
            expected="importable",
            message=(
                "The Python 'graphviz' package cannot be imported "
                f"({type(exc).__name__}: {exc}). Install it in the active "
                "Python environment before rendering."
            ),
        )

    return CheckResult(
        name="graphviz_python",
        ok=True,
        expected="importable",
        actual="importable",
        message="The Python 'graphviz' package is importable.",
    )


def _check_dot(
    command_runner: CommandRunner,
    timeout_seconds: float,
) -> CheckResult:
    command = ["dot", "-V"]
    try:
        completed = command_runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return CheckResult(
            name="dot",
            ok=False,
            expected="available on PATH",
            message=(
                "Graphviz 'dot' was not found on PATH. Install Graphviz and "
                "ensure `dot -V` succeeds before rendering."
            ),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="dot",
            ok=False,
            expected="responsive on PATH",
            message=(
                f"`dot -V` did not finish within {timeout_seconds:g} seconds. "
                "Check the Graphviz installation and PATH, then retry."
            ),
        )
    except OSError as exc:
        return CheckResult(
            name="dot",
            ok=False,
            expected="executable on PATH",
            message=(
                f"Could not execute `dot -V` ({type(exc).__name__}: {exc}). "
                "Check the Graphviz installation and executable permissions."
            ),
        )

    output = _command_output(completed)
    if completed.returncode != 0:
        detail = f" Output: {output}" if output else ""
        return CheckResult(
            name="dot",
            ok=False,
            expected="successful `dot -V`",
            actual=f"exit code {completed.returncode}",
            message=(
                f"`dot -V` failed with exit code {completed.returncode}. "
                f"Repair the Graphviz installation or PATH.{detail}"
            ),
        )

    match = re.search(r"\bgraphviz\s+version\s+([^\s(]+)", output, re.IGNORECASE)
    if match is None:
        detail = f" Output: {output}" if output else " No output was returned."
        return CheckResult(
            name="dot",
            ok=False,
            expected="parseable Graphviz version",
            message=(
                "`dot -V` ran, but its Graphviz version could not be determined."
                f" Verify that PATH points to a Graphviz `dot` executable.{detail}"
            ),
        )

    version = match.group(1)
    return CheckResult(
        name="dot",
        ok=True,
        expected="available on PATH",
        actual=version,
        message=f"Graphviz dot {version} is available on PATH.",
    )


def _command_output(completed: subprocess.CompletedProcess) -> str:
    parts = []
    for value in (completed.stdout, completed.stderr):
        if value:
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            parts.append(str(value).strip())
    return " ".join(parts)[:300]
