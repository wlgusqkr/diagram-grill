"""Build validated Diagram Grill artifact bundles without partial outputs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import unquote, urlsplit


MANIFEST_SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "diagram-grill"
GENERATOR_VERSION = "0.1.0-alpha"
SUPPORTED_FORMATS = frozenset(("svg", "png"))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
MAX_INPUT_SVG_BYTES = 16 * 1024 * 1024
MAX_EMBEDDED_ICON_BYTES = 1024 * 1024
MAX_EMBEDDED_ICON_COUNT = 200
MAX_EMBEDDED_ICON_TOTAL_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_SVG_BYTES = 96 * 1024 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATA_PNG_RE = re.compile(
    r"\Adata:image/png;base64,([A-Za-z0-9+/]+={0,2})\Z",
    re.IGNORECASE,
)
_DANGEROUS_SCHEME_RE = re.compile(br"(?i)(javascript|vbscript):")
_GRAPHVIZ_SVG_DOCTYPE_RE = re.compile(
    r'<!DOCTYPE\s+svg\s+PUBLIC\s+"-//W3C//DTD SVG 1\.1//EN"\s+'
    r'"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd"\s*>'
)
_XML_DECLARATION_RE = re.compile(r"<!\s*(?:doctype|entity)\b", re.IGNORECASE)
_UNSAFE_SVG_ATTRIBUTE_RE = re.compile(
    r"(?:url\s*\(|@import\b|[a-z][a-z0-9+.\-]*\s*:|//|/\*|\\)",
    re.IGNORECASE,
)
_SVG_ELEMENT_ATTRIBUTES = {
    "svg": frozenset(
        (
            ("", "height"),
            ("", "viewbox"),
            ("", "width"),
        )
    ),
    "g": frozenset(
        (
            ("", "class"),
            ("", "id"),
            ("", "transform"),
        )
    ),
    "title": frozenset(),
    "polygon": frozenset(
        (
            ("", "fill"),
            ("", "points"),
            ("", "stroke"),
            ("", "stroke-width"),
        )
    ),
    "path": frozenset(
        (
            ("", "d"),
            ("", "fill"),
            ("", "stroke"),
            ("", "stroke-dasharray"),
            ("", "stroke-width"),
        )
    ),
    "text": frozenset(
        (
            ("", "fill"),
            ("", "font-family"),
            ("", "font-size"),
            ("", "text-anchor"),
            ("", "x"),
            ("", "y"),
            (XML_NAMESPACE, "space"),
        )
    ),
    "image": frozenset(
        (
            ("", "height"),
            ("", "href"),
            ("", "preserveaspectratio"),
            ("", "width"),
            ("", "x"),
            ("", "y"),
            (XLINK_NAMESPACE, "href"),
        )
    ),
}
_PASSTHROUGH_ENV_KEYS = frozenset(
    (
        "HOME",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    )
)
_TERMINATION_GRACE_SECONDS = 0.25
_SKILL_ROOT = Path(__file__).resolve(strict=True).parents[2]


class BundleBuildError(RuntimeError):
    """Raised when a bundle cannot be rendered or safely promoted."""


class BundleCollisionError(FileExistsError):
    """Raised when a target exists and update mode was not requested."""


class ArtifactValidationError(BundleBuildError):
    """Raised when a renderer output is absent or malformed."""


Loader = Callable[..., Mapping[str, Any]]
Validator = Callable[..., None]
PreflightChecker = Callable[[], Any]
Compiler = Callable[..., Any]
CommandRunner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _ExecutableIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _VerifiedDot:
    path: Path
    identity: _ExecutableIdentity
    sha256: str


def build_bundle(
    spec_path: Union[Path, Mapping[str, Any]],
    workspace_root: Path,
    formats: Sequence[str] = ("svg",),
    update: bool = False,
    timeout_seconds: float = 30,
    *,
    load_and_validate_func: Optional[Loader] = None,
    validate_func: Optional[Validator] = None,
    preflight_func: Optional[PreflightChecker] = None,
    compile_func: Optional[Compiler] = None,
    command_runner: Optional[CommandRunner] = None,
    base_environment: Optional[Mapping[str, str]] = None,
) -> Path:
    """Render an approved spec into ``workspace_root/diagrams/<slug>``.

    Every generated file is written and verified in a staging directory under
    ``workspace_root/diagrams``. The target is changed only after the complete
    bundle is valid. ``spec_path`` may be a JSON path or an in-memory mapping;
    the latter lets the CLI accept stdin without persisting a separate input
    file. Existing targets require ``update=True``.
    """

    workspace = Path(workspace_root).resolve()
    if not workspace.is_dir():
        raise BundleBuildError(
            "Workspace root does not exist or is not a directory: {}".format(workspace)
        )
    if _paths_overlap(workspace, _SKILL_ROOT):
        raise BundleBuildError(
            "Installed skill path overlaps the target workspace; use a copied "
            "global installation outside the target repository and workspace"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than zero")

    loader = load_and_validate_func or _default_loader()
    preflight_checker = preflight_func or _default_preflight_checker(workspace)
    compiler = compile_func or _default_compiler()
    runner = command_runner

    if isinstance(spec_path, Mapping):
        validator = validate_func or _default_validator()
        validator(spec_path, mode="render")
        spec = spec_path
    else:
        input_path = Path(spec_path)
        if not input_path.is_absolute():
            input_path = workspace / input_path
        spec = loader(input_path, mode="render")
    slug = _validated_slug(spec)
    selected_formats = _normalize_formats(formats)

    diagrams_root = workspace / "diagrams"
    initial_diagrams_identity = _path_identity(diagrams_root)
    if _identity_is_symlink(initial_diagrams_identity):
        raise BundleBuildError("Refusing to write through a symlinked diagrams directory")
    if initial_diagrams_identity is not None and not stat.S_ISDIR(
        initial_diagrams_identity.mode
    ):
        raise BundleBuildError("Diagrams path is not a directory: {}".format(diagrams_root))

    target = diagrams_root / slug
    initial_target_identity = _path_identity(target)
    target_exists = initial_target_identity is not None
    if target_exists and not update:
        raise BundleCollisionError(
            "Bundle target already exists: {}. Pass update=True to replace it.".format(
                target
            )
        )
    if _identity_is_symlink(initial_target_identity):
        raise BundleBuildError("Refusing to replace a symlinked bundle target: {}".format(target))
    if target_exists and not stat.S_ISDIR(initial_target_identity.mode):
        raise BundleBuildError("Bundle target is not a directory: {}".format(target))

    preflight = preflight_checker()
    verified_dot = _require_preflight(preflight, workspace)

    compiled = compiler(spec, formats=selected_formats)
    source = _normalized_source(getattr(compiled, "source", None))
    warnings = tuple(str(item) for item in getattr(compiled, "warnings", ()))

    if initial_diagrams_identity is None:
        try:
            diagrams_root.mkdir()
        except FileExistsError as exc:
            raise BundleBuildError(
                "Diagrams path appeared while preparing the render: {}".format(
                    diagrams_root
                )
            ) from exc
    else:
        _assert_same_identity(
            diagrams_root,
            initial_diagrams_identity,
            "diagrams directory",
        )
    diagrams_identity = _path_identity(diagrams_root)
    if diagrams_identity is None or _identity_is_symlink(diagrams_identity):
        raise BundleBuildError(
            "Diagrams directory changed while preparing the render: {}".format(
                diagrams_root
            )
        )
    _assert_same_identity(target, initial_target_identity, "bundle target")
    stage = Path(tempfile.mkdtemp(prefix=".{}-stage-".format(slug), dir=str(diagrams_root)))
    _assert_same_identity(diagrams_root, diagrams_identity, "diagrams directory")
    stage_identity = _path_identity(stage)
    if stage_identity is None or not stat.S_ISDIR(stage_identity.mode):
        raise BundleBuildError("Could not establish renderer staging identity")

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
            verified_dot,
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

        _assert_same_identity(diagrams_root, diagrams_identity, "diagrams directory")
        _assert_same_identity(stage, stage_identity, "renderer staging directory")
        _assert_same_identity(target, initial_target_identity, "bundle target")
        _promote_stage(
            stage,
            target,
            replace_existing=target_exists,
            diagrams_root=diagrams_root,
            diagrams_identity=diagrams_identity,
            stage_identity=stage_identity,
            target_identity=initial_target_identity,
        )
    except subprocess.TimeoutExpired as exc:
        raise BundleBuildError(
            "Diagram rendering exceeded the {:.3g}-second timeout".format(timeout_seconds)
        ) from exc
    except OSError as exc:
        raise BundleBuildError("Could not execute the generated diagram: {}".format(exc)) from exc
    finally:
        _safe_cleanup_tree(
            stage,
            expected_identity=stage_identity,
            parent=diagrams_root,
            expected_parent_identity=diagrams_identity,
        )

    return target


def _paths_overlap(first: Path, second: Path) -> bool:
    if first == second or first in second.parents or second in first.parents:
        return True
    for component in (second,) + tuple(second.parents):
        try:
            if first.samefile(component):
                return True
        except FileNotFoundError:
            continue
    for component in (first,) + tuple(first.parents):
        try:
            if second.samefile(component):
                return True
        except FileNotFoundError:
            continue
    return False


def _default_loader() -> Loader:
    from .validation import load_and_validate

    return load_and_validate


def _default_validator() -> Validator:
    from .validation import validate_spec

    return validate_spec


def _default_preflight_checker(workspace: Path) -> PreflightChecker:
    from .preflight import run_preflight, runtime_forbidden_roots

    return lambda: run_preflight(
        forbidden_roots=runtime_forbidden_roots(workspace)
    )


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


def _require_preflight(preflight: Any, workspace: Path) -> _VerifiedDot:
    if not getattr(preflight, "ok", False):
        messages = [
            str(getattr(check, "message", "preflight check failed"))
            for check in getattr(preflight, "checks", ())
            if not getattr(check, "ok", False)
        ]
        detail = "; ".join(messages) or "one or more runtime checks failed"
        raise BundleBuildError("Preflight failed: {}".format(detail))

    raw_dot_path = getattr(preflight, "dot_path", None)
    if not isinstance(raw_dot_path, str) or not raw_dot_path:
        raise BundleBuildError(
            "Preflight did not provide the verified Graphviz dot executable path"
        )

    dot_path = Path(raw_dot_path)
    if not dot_path.is_absolute() or dot_path.stem.casefold() != "dot":
        raise BundleBuildError(
            "Preflight returned an invalid Graphviz dot executable path: {}".format(
                raw_dot_path
            )
        )

    try:
        canonical_dot_path = dot_path.resolve(strict=True)
    except OSError as exc:
        raise BundleBuildError(
            "Verified Graphviz dot executable is no longer available: {}".format(dot_path)
        ) from exc

    if canonical_dot_path != dot_path:
        raise BundleBuildError(
            "Preflight Graphviz dot path is no longer canonical: {}".format(dot_path)
        )

    for candidate in (dot_path, canonical_dot_path):
        for forbidden_root in _runtime_forbidden_roots(workspace):
            try:
                candidate.relative_to(forbidden_root)
            except ValueError:
                continue
            raise BundleBuildError(
                "Refusing Graphviz dot from the workspace or repository: {}".format(
                    dot_path
                )
            )

    trust_failure = _posix_dot_trust_failure(dot_path)
    if trust_failure is not None:
        raise BundleBuildError(trust_failure)

    if not dot_path.is_file() or not os.access(str(dot_path), os.X_OK):
        raise BundleBuildError(
            "Verified Graphviz dot path is not executable: {}".format(dot_path)
        )

    expected_identity = _coerce_executable_identity(
        getattr(preflight, "dot_identity", None)
    )
    expected_sha256 = getattr(preflight, "dot_sha256", None)
    if not isinstance(expected_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ) is None:
        raise BundleBuildError("Preflight did not provide a valid Graphviz dot SHA-256")

    try:
        actual_identity, actual_sha256 = _capture_executable(dot_path)
    except (OSError, ValueError) as exc:
        raise BundleBuildError(
            "Could not revalidate Graphviz dot after preflight: {}".format(exc)
        ) from exc
    if actual_identity != expected_identity or actual_sha256 != expected_sha256:
        raise BundleBuildError(
            "Graphviz dot changed after preflight; refusing to render"
        )
    return _VerifiedDot(dot_path, expected_identity, expected_sha256)


def _coerce_executable_identity(value: Any) -> _ExecutableIdentity:
    fields = ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
    try:
        values = tuple(int(getattr(value, field)) for field in fields)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BundleBuildError(
            "Preflight did not provide a valid Graphviz executable identity"
        ) from exc
    return _ExecutableIdentity(*values)


def _capture_executable(path: Path) -> Tuple[_ExecutableIdentity, str]:
    executable = Path(path)
    if not executable.is_absolute() or executable.resolve(strict=True) != executable:
        raise ValueError("Graphviz dot path is no longer canonical")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(executable), flags)
    try:
        before = _renderer_executable_identity(os.fstat(descriptor))
        if not stat.S_ISREG(before.mode):
            raise ValueError("Graphviz dot is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = _renderer_executable_identity(os.fstat(descriptor))
        if after != before:
            raise ValueError("Graphviz dot changed while it was being hashed")
        return before, digest.hexdigest()
    finally:
        os.close(descriptor)


def _renderer_executable_identity(result: os.stat_result) -> _ExecutableIdentity:
    return _ExecutableIdentity(
        device=int(result.st_dev),
        inode=int(result.st_ino),
        mode=int(result.st_mode),
        size=int(result.st_size),
        mtime_ns=int(getattr(result, "st_mtime_ns", result.st_mtime * 1_000_000_000)),
        ctime_ns=int(getattr(result, "st_ctime_ns", result.st_ctime * 1_000_000_000)),
    )


def _posix_dot_trust_failure(path: Path) -> Optional[str]:
    from .preflight import posix_executable_trust_failure

    return posix_executable_trust_failure(path)


def _runtime_forbidden_roots(workspace: Path) -> Tuple[Path, ...]:
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


def _minimal_environment(
    source: Mapping[str, str],
    verified_dot: _VerifiedDot,
) -> Dict[str, str]:
    environment = {
        key: str(value)
        for key, value in source.items()
        if key.upper() in _PASSTHROUGH_ENV_KEYS
    }
    environment["PATH"] = str(verified_dot.path.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _run_generated_source(
    runner: Optional[CommandRunner],
    stage: Path,
    timeout_seconds: float,
    base_environment: Mapping[str, str],
    verified_dot: _VerifiedDot,
) -> subprocess.CompletedProcess:
    _verify_dot_snapshot(verified_dot)
    command = [sys.executable, "-I", "diagram.py"]
    arguments = {
        "cwd": stage,
        "env": _minimal_environment(base_environment, verified_dot),
        "shell": False,
        "timeout": timeout_seconds,
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if runner is not None:
        return runner(command, **arguments)
    return _run_process_group(
        command,
        cwd=stage,
        env=arguments["env"],
        timeout=timeout_seconds,
    )


def _verify_dot_snapshot(verified_dot: _VerifiedDot) -> None:
    trust_failure = _posix_dot_trust_failure(verified_dot.path)
    if trust_failure is not None:
        raise BundleBuildError(trust_failure)
    try:
        identity, sha256 = _capture_executable(verified_dot.path)
    except (OSError, ValueError) as exc:
        raise BundleBuildError(
            "Graphviz dot could not be revalidated immediately before rendering: {}".format(
                exc
            )
        ) from exc
    if identity != verified_dot.identity or sha256 != verified_dot.sha256:
        raise BundleBuildError(
            "Graphviz dot changed after preflight; refusing to spawn the renderer"
        )
    trust_failure = _posix_dot_trust_failure(verified_dot.path)
    if trust_failure is not None:
        raise BundleBuildError(trust_failure)


def _run_process_group(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess:
    popen_arguments: Dict[str, Any] = {
        "cwd": cwd,
        "env": dict(env),
        "shell": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_arguments["start_new_session"] = True
    elif os.name == "nt":
        popen_arguments["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )

    process = subprocess.Popen(list(command), **popen_arguments)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = _bounded_post_kill_drain(process, exc)
        raise subprocess.TimeoutExpired(
            list(command),
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc

    return subprocess.CompletedProcess(
        list(command),
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
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
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
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
            timeout=_TERMINATION_GRACE_SECONDS
        )
        return drained_stdout, drained_stderr
    except subprocess.TimeoutExpired as drain_error:
        if drain_error.output is not None:
            stdout = drain_error.output
        if drain_error.stderr is not None:
            stderr = drain_error.stderr
        _close_process_pipes(process)
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
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
            data = _verify_svg(data)
            path.write_bytes(data)
        elif render_format == "png":
            _verify_png(data)
        artifacts[name] = data
    return artifacts


def _verify_svg(data: bytes) -> bytes:
    return _sanitize_svg(data)


def _sanitize_svg(data: bytes, *, resources_root: Optional[Path] = None) -> bytes:
    """Return a deterministic, self-contained SVG with inert labels and icons."""

    if len(data) > MAX_INPUT_SVG_BYTES:
        raise ArtifactValidationError("Rendered diagram.svg exceeds the input size limit")

    try:
        source_text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(
            "Rendered diagram.svg must be UTF-8 XML"
        ) from exc
    # Graphviz emits this exact inert SVG 1.1 declaration on every SVG render.
    # Strip one copy before parsing; internal subsets, ENTITY declarations, any
    # other DOCTYPE, or duplicate standard declarations remain and fail closed.
    graphviz_doctypes = tuple(_GRAPHVIZ_SVG_DOCTYPE_RE.finditer(source_text))
    if len(graphviz_doctypes) > 1:
        raise ArtifactValidationError(
            "Rendered diagram.svg contains duplicate DOCTYPE declarations"
        )
    if graphviz_doctypes:
        declaration = graphviz_doctypes[0]
        source_text = source_text[: declaration.start()] + source_text[declaration.end() :]
    if _XML_DECLARATION_RE.search(source_text) is not None:
        raise ArtifactValidationError(
            "Rendered diagram.svg contains an unsupported DOCTYPE or ENTITY declaration"
        )

    try:
        root = ElementTree.fromstring(source_text)
    except ElementTree.ParseError as exc:
        raise ArtifactValidationError("Rendered diagram.svg is not parseable XML") from exc

    images = _validate_svg_tree(root, require_embedded_images=False)

    trusted_root = Path(resources_root).resolve() if resources_root is not None else None
    icon_count = 0
    icon_total_bytes = 0

    for element, attribute_name, href in images:
        if icon_count >= MAX_EMBEDDED_ICON_COUNT:
            raise ArtifactValidationError(
                "Rendered diagram.svg exceeds the embedded icon count limit"
            )

        if href.casefold().startswith("data:"):
            icon_data = _decode_png_data_uri(href)
        else:
            if trusted_root is None:
                trusted_root = _trusted_resources_root()
            icon_data = _read_trusted_icon(href, trusted_root)

        icon_count += 1
        icon_total_bytes += len(icon_data)
        if icon_total_bytes > MAX_EMBEDDED_ICON_TOTAL_BYTES:
            raise ArtifactValidationError(
                "Rendered diagram.svg exceeds the embedded icon byte limit"
            )
        element.set(attribute_name, _png_data_uri(icon_data))

    ElementTree.register_namespace("", SVG_NAMESPACE)
    ElementTree.register_namespace("xlink", XLINK_NAMESPACE)
    sanitized = ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    # Escaped HTML labels are inert text by this point. Encoding the colon in
    # dangerous URI-looking text also keeps scanners from mistaking that text
    # for an executable scheme while preserving the visible label exactly.
    sanitized = _DANGEROUS_SCHEME_RE.sub(
        lambda match: match.group(1) + b"&#58;", sanitized
    ) + b"\n"
    if len(sanitized) > MAX_OUTPUT_SVG_BYTES:
        raise ArtifactValidationError("Rendered diagram.svg exceeds the output size limit")
    if _XML_DECLARATION_RE.search(sanitized.decode("utf-8")) is not None:
        raise ArtifactValidationError(
            "Sanitized diagram.svg contains a DOCTYPE or ENTITY declaration"
        )

    try:
        reparsed = ElementTree.fromstring(sanitized)
    except ElementTree.ParseError as exc:
        raise ArtifactValidationError(
            "Sanitized diagram.svg is not parseable XML"
        ) from exc
    _validate_svg_tree(reparsed, require_embedded_images=True)
    return sanitized


def _validate_svg_tree(
    root: ElementTree.Element,
    *,
    require_embedded_images: bool,
) -> Tuple[Tuple[ElementTree.Element, str, str], ...]:
    """Validate the exact inert SVG vocabulary emitted by Diagram Grill."""

    root_namespace, root_name = _xml_name(root.tag)
    if root_name != "svg" or root_namespace not in ("", SVG_NAMESPACE):
        raise ArtifactValidationError("Rendered diagram.svg does not have an SVG root element")

    images = []
    embedded_icon_total = 0
    for element in root.iter():
        element_namespace, element_name = _xml_name(element.tag)
        if element_namespace != root_namespace:
            raise ArtifactValidationError(
                "Rendered diagram.svg contains an unexpected element namespace"
            )
        allowed_attributes = _SVG_ELEMENT_ATTRIBUTES.get(element_name)
        if allowed_attributes is None or (element is not root and element_name == "svg"):
            raise ArtifactValidationError(
                "Rendered diagram.svg contains unsupported <{}> content".format(
                    element_name or "unknown"
                )
            )

        href_attributes = []
        for attribute_name, value in tuple(element.attrib.items()):
            attribute = _xml_name(attribute_name)
            if attribute not in allowed_attributes:
                raise ArtifactValidationError(
                    "Rendered diagram.svg contains unsupported attribute {} on <{}>".format(
                        attribute[1] or "unknown",
                        element_name,
                    )
                )
            if attribute[1] == "href":
                href_attributes.append((attribute_name, value))
            else:
                _require_inert_svg_attribute(element_name, attribute[1], value)

        if element_name != "image":
            if href_attributes:
                raise ArtifactValidationError(
                    "Rendered diagram.svg contains a link outside an image element"
                )
            continue
        if len(href_attributes) != 1:
            raise ArtifactValidationError(
                "Every SVG image must contain exactly one href attribute"
            )

        attribute_name, href = href_attributes[0]
        if require_embedded_images:
            if not href.casefold().startswith("data:"):
                raise ArtifactValidationError(
                    "Sanitized SVG images must be embedded PNG data"
                )
            icon_data = _decode_png_data_uri(href)
            embedded_icon_total += len(icon_data)
            if len(images) >= MAX_EMBEDDED_ICON_COUNT:
                raise ArtifactValidationError(
                    "Rendered diagram.svg exceeds the embedded icon count limit"
                )
            if embedded_icon_total > MAX_EMBEDDED_ICON_TOTAL_BYTES:
                raise ArtifactValidationError(
                    "Rendered diagram.svg exceeds the embedded icon byte limit"
                )
        images.append((element, attribute_name, href))

    return tuple(images)


def _require_inert_svg_attribute(
    element_name: str,
    attribute_name: str,
    value: Any,
) -> None:
    if not isinstance(value, str) or _UNSAFE_SVG_ATTRIBUTE_RE.search(value) is not None:
        raise ArtifactValidationError(
            "Rendered diagram.svg contains an unsafe {} attribute on <{}>".format(
                attribute_name,
                element_name,
            )
        )


def _xml_name(name: Any) -> Tuple[str, str]:
    if not isinstance(name, str):
        return "", ""
    if name.startswith("{") and "}" in name:
        namespace, _, local_name = name[1:].partition("}")
    else:
        namespace, local_name = "", name
    return namespace, local_name.casefold()


def _trusted_resources_root() -> Path:
    from .catalog import _validated_package_dir

    package_dir = _validated_package_dir()
    resources_root = (package_dir.parent / "resources").resolve()
    if not resources_root.is_dir():
        raise ArtifactValidationError(
            "The pinned diagrams distribution has no resources directory"
        )
    return resources_root


def _decode_png_data_uri(href: str) -> bytes:
    match = _DATA_PNG_RE.fullmatch(href)
    if match is None:
        raise ArtifactValidationError(
            "SVG image data must be a base64-encoded image/png URI"
        )
    try:
        icon_data = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactValidationError("SVG image contains invalid base64 data") from exc
    _verify_bounded_icon(icon_data)
    return icon_data


def _read_trusted_icon(href: str, resources_root: Path) -> bytes:
    source_path = _local_icon_path(href)
    try:
        resolved_source = source_path.resolve(strict=True)
        resolved_root = Path(resources_root).resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ArtifactValidationError(
            "SVG icon source is outside the pinned diagrams resources root"
        ) from exc

    if resolved_source.suffix.casefold() != ".png" or not resolved_source.is_file():
        raise ArtifactValidationError("SVG icon source is not a trusted PNG file")
    if resolved_source.stat().st_size > MAX_EMBEDDED_ICON_BYTES:
        raise ArtifactValidationError("SVG icon exceeds the embedded icon size limit")

    try:
        with resolved_source.open("rb") as icon_file:
            icon_data = icon_file.read(MAX_EMBEDDED_ICON_BYTES + 1)
    except OSError as exc:
        raise ArtifactValidationError("Could not read the trusted SVG icon") from exc
    _verify_bounded_icon(icon_data)
    return icon_data


def _local_icon_path(href: str) -> Path:
    direct_path = Path(href)
    if direct_path.is_absolute():
        return direct_path

    parsed = urlsplit(href)
    if parsed.scheme.casefold() != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ArtifactValidationError(
            "SVG images may only reference local icons from the pinned resources root"
        )
    file_path = Path(unquote(parsed.path))
    if not file_path.is_absolute():
        raise ArtifactValidationError("SVG icon source must be an absolute local path")
    return file_path


def _verify_bounded_icon(icon_data: bytes) -> None:
    if len(icon_data) > MAX_EMBEDDED_ICON_BYTES:
        raise ArtifactValidationError("SVG icon exceeds the embedded icon size limit")
    if len(icon_data) <= len(PNG_SIGNATURE) or not icon_data.startswith(PNG_SIGNATURE):
        raise ArtifactValidationError("SVG icon does not contain valid PNG data")


def _png_data_uri(icon_data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(icon_data).decode("ascii")


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
    dot_sha256 = getattr(preflight, "dot_sha256", None)
    if (
        isinstance(dot_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", dot_sha256) is not None
        and "dot" in runtime
    ):
        runtime["dot"]["sha256"] = dot_sha256
    return runtime


def _path_identity(path: Path) -> Optional[_PathIdentity]:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    return _PathIdentity(
        device=int(result.st_dev),
        inode=int(result.st_ino),
        mode=int(result.st_mode),
    )


def _identity_is_symlink(identity: Optional[_PathIdentity]) -> bool:
    return identity is not None and stat.S_ISLNK(identity.mode)


def _assert_same_identity(
    path: Path,
    expected: Optional[_PathIdentity],
    description: str,
) -> None:
    actual = _path_identity(path)
    if actual == expected:
        return

    if expected is None:
        change = "appeared"
    elif actual is None:
        change = "disappeared"
    elif _identity_is_symlink(actual):
        change = "became a symlink"
    else:
        change = "changed identity"
    raise BundleBuildError(
        "Refusing promotion because the {} {} during rendering: {}".format(
            description,
            change,
            path,
        )
    )


def _safe_cleanup_tree(
    path: Path,
    *,
    expected_identity: _PathIdentity,
    parent: Path,
    expected_parent_identity: _PathIdentity,
) -> bool:
    """Remove only the exact directory observed beneath the exact parent."""

    if _path_identity(parent) != expected_parent_identity:
        return False
    actual_identity = _path_identity(path)
    if (
        actual_identity != expected_identity
        or _identity_is_symlink(actual_identity)
        or not stat.S_ISDIR(actual_identity.mode)
    ):
        return False
    try:
        shutil.rmtree(str(path))
    except OSError:
        return False
    return True


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


def _promote_stage(
    stage: Path,
    target: Path,
    *,
    replace_existing: bool,
    diagrams_root: Path,
    diagrams_identity: _PathIdentity,
    stage_identity: _PathIdentity,
    target_identity: Optional[_PathIdentity],
) -> None:
    _assert_same_identity(diagrams_root, diagrams_identity, "diagrams directory")
    _assert_same_identity(stage, stage_identity, "renderer staging directory")
    _assert_same_identity(target, target_identity, "bundle target")

    if not replace_existing:
        os.replace(str(stage), str(target))
        return

    backup = target.parent / ".{}-backup-{}".format(target.name, uuid.uuid4().hex)
    if _path_identity(backup) is not None:
        raise BundleBuildError("Refusing to replace an existing backup path")
    os.replace(str(target), str(backup))
    moved_identity = _path_identity(backup)
    if moved_identity != target_identity:
        _restore_backup_if_safe(
            backup,
            target,
            backup_identity=moved_identity,
            diagrams_root=diagrams_root,
            diagrams_identity=diagrams_identity,
        )
        raise BundleBuildError(
            "Bundle target changed while it was being moved to the update backup"
        )

    try:
        _assert_same_identity(diagrams_root, diagrams_identity, "diagrams directory")
        _assert_same_identity(stage, stage_identity, "renderer staging directory")
        _assert_same_identity(target, None, "bundle target")
        os.replace(str(stage), str(target))
    except Exception:
        _restore_backup_if_safe(
            backup,
            target,
            backup_identity=moved_identity,
            diagrams_root=diagrams_root,
            diagrams_identity=diagrams_identity,
        )
        raise

    if not _safe_cleanup_tree(
        backup,
        expected_identity=moved_identity,
        parent=diagrams_root,
        expected_parent_identity=diagrams_identity,
    ):
        raise BundleBuildError(
            "Updated bundle was promoted, but the moved backup changed identity and "
            "was retained for safety: {}".format(backup)
        )


def _restore_backup_if_safe(
    backup: Path,
    target: Path,
    *,
    backup_identity: Optional[_PathIdentity],
    diagrams_root: Path,
    diagrams_identity: _PathIdentity,
) -> bool:
    if backup_identity is None:
        return False
    if _path_identity(diagrams_root) != diagrams_identity:
        return False
    if _path_identity(backup) != backup_identity or _path_identity(target) is not None:
        return False
    os.replace(str(backup), str(target))
    return True
