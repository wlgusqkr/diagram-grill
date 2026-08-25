"""Build validated Diagram Grill artifact bundles without partial outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


MANIFEST_SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "diagram-grill"
GENERATOR_VERSION = "0.1.0-alpha"
SUPPORTED_FORMATS = frozenset(("svg", "png"))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PASSTHROUGH_ENV_KEYS = frozenset(
    (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    )
)


class BundleBuildError(RuntimeError):
    """Raised when a bundle cannot be rendered or safely promoted."""


class BundleCollisionError(FileExistsError):
    """Raised when a target exists and update mode was not requested."""


class ArtifactValidationError(BundleBuildError):
    """Raised when a renderer output is absent or malformed."""


Loader = Callable[..., Mapping[str, Any]]
PreflightChecker = Callable[[], Any]
Compiler = Callable[..., Any]
CommandRunner = Callable[..., subprocess.CompletedProcess]


def build_bundle(
    spec_path: Path,
    workspace_root: Path,
    formats: Sequence[str] = ("svg",),
    update: bool = False,
    timeout_seconds: float = 30,
    *,
    load_and_validate_func: Optional[Loader] = None,
    preflight_func: Optional[PreflightChecker] = None,
    compile_func: Optional[Compiler] = None,
    command_runner: Optional[CommandRunner] = None,
    base_environment: Optional[Mapping[str, str]] = None,
) -> Path:
    """Render ``spec_path`` into ``workspace_root/diagrams/<slug>``.

    Every generated file is written and verified in a staging directory under
    ``workspace_root/diagrams``. The target is changed only after the complete
    bundle is valid. Existing targets require ``update=True``.
    """

    workspace = Path(workspace_root).resolve()
    if not workspace.is_dir():
        raise BundleBuildError(
            "Workspace root does not exist or is not a directory: {}".format(workspace)
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than zero")

    input_path = Path(spec_path)
    if not input_path.is_absolute():
        input_path = workspace / input_path

    loader = load_and_validate_func or _default_loader()
    preflight_checker = preflight_func or _default_preflight_checker()
    compiler = compile_func or _default_compiler()
    runner = command_runner or subprocess.run

    spec = loader(input_path, mode="render")
    slug = _validated_slug(spec)
    selected_formats = _normalize_formats(formats)

    diagrams_root = workspace / "diagrams"
    if diagrams_root.is_symlink():
        raise BundleBuildError("Refusing to write through a symlinked diagrams directory")

    target = diagrams_root / slug
    target_exists = target.exists() or target.is_symlink()
    if target_exists and not update:
        raise BundleCollisionError(
            "Bundle target already exists: {}. Pass update=True to replace it.".format(
                target
            )
        )
    if target.is_symlink():
        raise BundleBuildError("Refusing to replace a symlinked bundle target: {}".format(target))
    if target_exists and not target.is_dir():
        raise BundleBuildError("Bundle target is not a directory: {}".format(target))

    preflight = preflight_checker()
    _require_preflight(preflight)

    compiled = compiler(spec, formats=selected_formats)
    source = _normalized_source(getattr(compiled, "source", None))
    warnings = tuple(str(item) for item in getattr(compiled, "warnings", ()))

    diagrams_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".{}-stage-".format(slug), dir=str(diagrams_root)))

    try:
        canonical_spec = _canonical_json(spec)
        source_bytes = source.encode("utf-8")
        (stage / "diagram.json").write_bytes(canonical_spec)
        (stage / "diagram.py").write_bytes(source_bytes)

        completed = _run_generated_source(
            runner,
            stage,
            timeout_seconds,
            base_environment if base_environment is not None else os.environ,
        )
        _require_success(completed)

        artifact_data = _verify_artifacts(stage, selected_formats)
        manifest = _build_manifest(
            spec=spec,
            canonical_spec=canonical_spec,
            source=source_bytes,
            artifact_data=artifact_data,
            preflight=preflight,
            warnings=warnings,
        )
        (stage / "render-manifest.json").write_bytes(_canonical_json(manifest))

        _promote_stage(stage, target, replace_existing=target_exists)
    except subprocess.TimeoutExpired as exc:
        raise BundleBuildError(
            "Diagram rendering exceeded the {:.3g}-second timeout".format(timeout_seconds)
        ) from exc
    except OSError as exc:
        raise BundleBuildError("Could not execute the generated diagram: {}".format(exc)) from exc
    finally:
        if stage.exists():
            shutil.rmtree(str(stage), ignore_errors=True)

    return target


def _default_loader() -> Loader:
    from .validation import load_and_validate

    return load_and_validate


def _default_preflight_checker() -> PreflightChecker:
    from .preflight import run_preflight

    return run_preflight


def _default_compiler() -> Compiler:
    from .compiler import compile_spec

    return compile_spec


def _validated_slug(spec: Mapping[str, Any]) -> str:
    slug = spec.get("slug")
    if not isinstance(slug, str) or _SLUG_RE.fullmatch(slug) is None:
        raise BundleBuildError("Validated specification has an unsafe or missing slug")
    return slug


def _normalize_formats(formats: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(formats, str):
        candidates = (formats,)
    else:
        candidates = tuple(formats)

    normalized = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ValueError("Render formats must be strings")
        value = candidate.lower()
        if value not in SUPPORTED_FORMATS:
            raise ValueError(
                "Unsupported render format {!r}; expected svg or png".format(candidate)
            )
        if value not in normalized:
            normalized.append(value)

    if "svg" not in normalized:
        normalized.insert(0, "svg")
    return tuple(normalized)


def _require_preflight(preflight: Any) -> None:
    if getattr(preflight, "ok", False):
        return

    messages = [
        str(getattr(check, "message", "preflight check failed"))
        for check in getattr(preflight, "checks", ())
        if not getattr(check, "ok", False)
    ]
    detail = "; ".join(messages) or "one or more runtime checks failed"
    raise BundleBuildError("Preflight failed: {}".format(detail))


def _normalized_source(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        raise BundleBuildError("Compiler returned empty or non-text Python source")
    return source.rstrip() + "\n"


def _canonical_json(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    )
    return (text + "\n").encode("utf-8")


def _minimal_environment(source: Mapping[str, str]) -> Dict[str, str]:
    environment = {
        key: str(value)
        for key, value in source.items()
        if key.upper() in _PASSTHROUGH_ENV_KEYS
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _run_generated_source(
    runner: CommandRunner,
    stage: Path,
    timeout_seconds: float,
    base_environment: Mapping[str, str],
) -> subprocess.CompletedProcess:
    return runner(
        [sys.executable, "diagram.py"],
        cwd=stage,
        env=_minimal_environment(base_environment),
        shell=False,
        timeout=timeout_seconds,
        capture_output=True,
        text=True,
        check=False,
    )


def _require_success(completed: subprocess.CompletedProcess) -> None:
    if completed.returncode == 0:
        return

    output_parts = []
    for value in (completed.stdout, completed.stderr):
        if value:
            output_parts.append(str(value).strip())
    output = " ".join(output_parts)[:500]
    detail = ": {}".format(output) if output else ""
    raise BundleBuildError(
        "Generated diagram.py failed with exit code {}{}".format(
            completed.returncode,
            detail,
        )
    )


def _verify_artifacts(stage: Path, formats: Sequence[str]) -> Dict[str, bytes]:
    artifacts: Dict[str, bytes] = {}
    for render_format in formats:
        name = "diagram.{}".format(render_format)
        path = stage / name
        if path.is_symlink() or not path.is_file():
            raise ArtifactValidationError("Expected artifact was not created: {}".format(name))
        data = path.read_bytes()
        if not data:
            raise ArtifactValidationError("Rendered artifact is empty: {}".format(name))

        if render_format == "svg":
            _verify_svg(data)
        elif render_format == "png":
            _verify_png(data)
        artifacts[name] = data
    return artifacts


def _verify_svg(data: bytes) -> None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ArtifactValidationError("Rendered diagram.svg is not parseable XML") from exc

    local_name = root.tag.rsplit("}", 1)[-1] if isinstance(root.tag, str) else ""
    if local_name.lower() != "svg":
        raise ArtifactValidationError("Rendered diagram.svg does not have an SVG root element")


def _verify_png(data: bytes) -> None:
    if len(data) <= len(PNG_SIGNATURE) or not data.startswith(PNG_SIGNATURE):
        raise ArtifactValidationError("Rendered diagram.png has an invalid PNG signature")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_manifest(preflight: Any) -> Dict[str, Dict[str, Optional[str]]]:
    runtime: Dict[str, Dict[str, Optional[str]]] = {}
    for check in getattr(preflight, "checks", ()):
        name = str(getattr(check, "name", "unknown"))
        runtime[name] = {
            "required": getattr(check, "expected", None),
            "actual": getattr(check, "actual", None),
        }
    return runtime


def _build_manifest(
    *,
    spec: Mapping[str, Any],
    canonical_spec: bytes,
    source: bytes,
    artifact_data: Mapping[str, bytes],
    preflight: Any,
    warnings: Tuple[str, ...],
) -> Dict[str, Any]:
    artifacts = {
        name: {
            "sha256": _sha256(data),
            "size_bytes": len(data),
        }
        for name, data in sorted(artifact_data.items())
    }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "diagram_schema_version": str(spec.get("schema_version", "unknown")),
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
        },
        "spec": {
            "path": "diagram.json",
            "sha256": _sha256(canonical_spec),
        },
        "source": {
            "path": "diagram.py",
            "sha256": _sha256(source),
        },
        "artifacts": artifacts,
        "runtime": _runtime_manifest(preflight),
        "warnings": list(warnings),
    }


def _promote_stage(stage: Path, target: Path, *, replace_existing: bool) -> None:
    if not replace_existing:
        os.replace(str(stage), str(target))
        return

    backup = target.parent / ".{}-backup-{}".format(target.name, uuid.uuid4().hex)
    os.replace(str(target), str(backup))
    try:
        os.replace(str(stage), str(target))
    except Exception:
        os.replace(str(backup), str(target))
        raise
    else:
        shutil.rmtree(str(backup), ignore_errors=True)
