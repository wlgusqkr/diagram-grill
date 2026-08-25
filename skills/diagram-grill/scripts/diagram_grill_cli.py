#!/usr/bin/env python3
"""Public command-line interface for Diagram Grill."""

from __future__ import annotations

import sys


# Every supported command starts with read-only inspection.  Keep imports from
# mutating the installed skill package before the user approves any output.
sys.dont_write_bytecode = True


if __name__ == "__main__" and (
    not sys.flags.isolated or not sys.flags.no_site
):
    sys.stderr.write(
        "diagram-grill: Python isolated no-site mode is required.\n"
        "Run: /absolute/path/to/trusted/python -I -S "
        "/absolute/path/to/diagram_grill_cli.py --target-root "
        "/absolute/path/to/target <command> [options]\n"
    )
    raise SystemExit(2)


import argparse
import json
import math
import os
from pathlib import Path
import stat
import sysconfig
from typing import Any, Dict, List, Mapping, Optional, Sequence


if __name__ == "__main__" and not Path(__file__).is_absolute():
    sys.stderr.write(
        "diagram-grill: an absolute CLI script path is required.\n"
        "Run: /absolute/path/to/trusted/python -I -S "
        "/absolute/path/to/diagram_grill_cli.py --target-root "
        "/absolute/path/to/target <command> [options]\n"
    )
    raise SystemExit(2)


_CLI_PATH = Path(__file__).absolute()
_SKILL_ROOT = _CLI_PATH.parents[1]
_PYTHON_EXECUTABLE = Path(sys.executable).absolute()
_APPLICATION_IMPORTS_ACTIVE = False


MAX_STDIN_SPEC_BYTES = 1024 * 1024


def _executable_environment_root() -> Optional[Path]:
    # Before Python 3.14, ``-S`` can leave sys.prefix at the base interpreter.
    # Recognize a normal venv from its executable layout without importing site
    # or evaluating pyvenv.cfg.
    executable = Path(sys.executable).absolute()
    if executable.parent.name.casefold() in ("bin", "scripts"):
        candidate = executable.parent.parent
        configuration = candidate / "pyvenv.cfg"
        try:
            result = configuration.lstat()
        except OSError:
            pass
        else:
            if stat.S_ISREG(result.st_mode):
                return candidate
    return None


def _environment_roots() -> Sequence[Path]:
    selected: List[Path] = []
    values: List[Any] = [
        _executable_environment_root(),
        sys.prefix,
        sys.exec_prefix,
        sys.base_prefix,
        sys.base_exec_prefix,
    ]
    for value in values:
        if not value:
            continue
        candidate = Path(value).absolute()
        if candidate not in selected:
            selected.append(candidate)
    return tuple(selected)


def _application_package_paths() -> Sequence[Path]:
    """Return package directories without processing executable ``.pth`` files."""

    path_sets = []
    scheme = "nt" if os.name == "nt" else "posix_prefix"
    environment_root = _executable_environment_root()
    if environment_root is not None:
        variables = {
            "base": str(environment_root),
            "platbase": str(environment_root),
            "installed_base": str(environment_root),
            "installed_platbase": str(environment_root),
        }
        path_sets.append(sysconfig.get_paths(scheme=scheme, vars=variables))
    path_sets.append(sysconfig.get_paths())
    selected: List[Path] = []
    for paths in path_sets:
        for name in ("purelib", "platlib"):
            value = paths.get(name)
            if not value:
                continue
            candidate = Path(value).absolute()
            if candidate not in selected:
                selected.append(candidate)
    return tuple(selected)


def _python_runtime_paths() -> Sequence[Path]:
    selected: List[Path] = []
    values: List[Any] = [
        _PYTHON_EXECUTABLE,
        getattr(sys, "_base_executable", None),
    ]
    values.extend(_environment_roots())
    values.extend(_application_package_paths())
    for value in values:
        if not value:
            continue
        candidate = Path(value).absolute()
        if candidate not in selected:
            selected.append(candidate)
    return tuple(selected)


def _activate_application_imports() -> None:
    """Expose only checked skill and package roots; never execute site hooks."""

    global _APPLICATION_IMPORTS_ACTIVE
    if _APPLICATION_IMPORTS_ACTIVE:
        return
    for package_path in _application_package_paths():
        if package_path.is_dir() and str(package_path) not in sys.path:
            sys.path.append(str(package_path))
    scripts_root = _CLI_PATH.resolve(strict=True).parent
    if str(scripts_root) not in sys.path:
        sys.path.insert(0, str(scripts_root))
    _APPLICATION_IMPORTS_ACTIVE = True


def run_preflight(*args: Any, **kwargs: Any) -> Any:
    from diagram_grill.preflight import run_preflight as implementation

    return implementation(*args, **kwargs)


def search_node_catalog(*args: Any, **kwargs: Any) -> Any:
    from diagram_grill.catalog import search_node_catalog as implementation

    return implementation(*args, **kwargs)


def load_and_validate(*args: Any, **kwargs: Any) -> Any:
    from diagram_grill.validation import load_and_validate as implementation

    return implementation(*args, **kwargs)


def build_bundle(*args: Any, **kwargs: Any) -> Any:
    from diagram_grill.renderer import build_bundle as implementation

    return implementation(*args, **kwargs)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return parsed


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diagram-grill",
        description="Validate and render approved architecture diagram specifications.",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        required=True,
        metavar="PATH",
        help="Absolute repository or workspace root treated as untrusted input.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser(
        "preflight", help="Check local runtime dependencies without installing them."
    )
    preflight.add_argument("--json", action="store_true", help="Emit JSON output.")
    preflight.set_defaults(handler=_command_preflight)

    evidence = commands.add_parser(
        "evidence", help="Read bounded UTF-8 evidence without following symlinks."
    )
    evidence.add_argument("--repo", type=Path, required=True, metavar="PATH")
    evidence.add_argument("paths", nargs="+", metavar="FILE")
    evidence.add_argument("--json", action="store_true", help="Emit JSON output.")
    evidence.set_defaults(handler=_command_evidence)

    catalog = commands.add_parser(
        "catalog", help="Search the pinned diagrams node catalog."
    )
    catalog.add_argument("query", nargs="?", default="", metavar="QUERY")
    catalog.add_argument("--provider")
    catalog.add_argument("--category")
    catalog.add_argument("--limit", type=_positive_int, default=20)
    catalog.add_argument("--json", action="store_true", help="Emit JSON output.")
    catalog.set_defaults(handler=_command_catalog)

    validate = commands.add_parser(
        "validate", help="Validate a diagram specification without rendering it."
    )
    validate.add_argument("spec", type=Path, metavar="SPEC")
    validate.add_argument("--mode", choices=("draft", "render"), default="draft")
    validate.add_argument("--json", action="store_true", help="Emit JSON output.")
    validate.set_defaults(handler=_command_validate)

    brief = commands.add_parser(
        "brief", help="Print a read-only Architecture Brief for a draft specification."
    )
    brief.add_argument("spec", type=Path, metavar="SPEC")
    brief.set_defaults(handler=_command_brief)

    build = commands.add_parser(
        "build", help="Build an approved SVG bundle and optionally include PNG."
    )
    build.add_argument(
        "spec",
        type=Path,
        metavar="SPEC",
        help="Approved JSON file, or '-' to read approved JSON from stdin.",
    )
    build.add_argument("--workspace", type=Path, default=Path("."), metavar="PATH")
    build.add_argument("--png", action="store_true", help="Also render diagram.png.")
    build.add_argument(
        "--update",
        action="store_true",
        help="Explicitly replace an existing bundle with the same slug.",
    )
    build.add_argument("--timeout", type=_positive_float, default=30.0, metavar="SECONDS")
    build.add_argument("--json", action="store_true", help="Emit JSON output.")
    build.set_defaults(handler=_command_build)

    return parser


def _write_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _issue_payload(issue: Any) -> Dict[str, str]:
    return {
        "code": issue.code,
        "path": issue.path,
        "message": issue.message,
    }


def _failure_payload(error: Exception) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": False,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    issues = getattr(error, "issues", None)
    if type(error).__name__ == "DiagramValidationError" and isinstance(
        issues, (list, tuple)
    ):
        payload["error"]["issues"] = [
            _issue_payload(issue) for issue in issues
        ]
    return payload


def _report_failure(error: Exception, json_output: bool) -> int:
    if json_output:
        _write_json(_failure_payload(error))
    else:
        print("diagram-grill: {}".format(error), file=sys.stderr)
    return 1


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


def _path_forms(path: Path) -> Sequence[Path]:
    lexical = Path(path).absolute()
    forms = [lexical]
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError):
        resolved = lexical.resolve(strict=False)
    if resolved not in forms:
        forms.append(resolved)
    return tuple(forms)


def _operation_targets(args: argparse.Namespace) -> Sequence[Path]:
    target_root = Path(args.target_root)
    if not target_root.is_absolute():
        raise RuntimeError("--target-root must be an absolute path")
    try:
        target_result = target_root.lstat()
    except OSError as error:
        raise RuntimeError("--target-root must be an existing real directory") from error
    if stat.S_ISLNK(target_result.st_mode) or not stat.S_ISDIR(
        target_result.st_mode
    ):
        raise RuntimeError("--target-root must be an existing real directory")

    targets = [target_root]
    if args.command == "evidence":
        targets.append(Path(args.repo))
    elif args.command == "build":
        targets.append(Path(args.workspace))
        if args.spec != Path("-"):
            targets.append(Path(args.spec).absolute().parent)
    elif args.command in ("validate", "brief"):
        targets.append(Path(args.spec).absolute().parent)
    return tuple(targets)


def _overlaps_any(first: Path, targets: Sequence[Path]) -> bool:
    return any(
        _paths_overlap(first_form, target_form)
        for first_form in _path_forms(first)
        for target in targets
        for target_form in _path_forms(target)
    )


def _require_external_skill_install(args: argparse.Namespace) -> None:
    targets = _operation_targets(args)
    skill_paths = (_CLI_PATH, _SKILL_ROOT)
    if any(_overlaps_any(path, targets) for path in skill_paths):
        raise RuntimeError(
            "Refusing an installed skill path that overlaps the target {}: {}. "
            "Install diagram-grill globally with --copy outside the target "
            "repository and workspace.".format(args.command, _SKILL_ROOT)
        )
    for runtime_path in _python_runtime_paths():
        if _overlaps_any(runtime_path, targets):
            raise RuntimeError(
                "Refusing a Python runtime path that overlaps the target {}: {}. "
                "Use a trusted absolute interpreter and dependency environment "
                "outside the target repository and workspace.".format(
                    args.command, runtime_path
                )
            )


def _check_payload(check: Any) -> Dict[str, Any]:
    return {
        "name": str(getattr(check, "name", "unknown")),
        "ok": bool(getattr(check, "ok", False)),
        "message": str(getattr(check, "message", "")),
        "expected": getattr(check, "expected", None),
        "actual": getattr(check, "actual", None),
    }


def _command_preflight(args: argparse.Namespace) -> int:
    result = run_preflight()
    checks = [_check_payload(check) for check in result.checks]
    ok = bool(result.ok)
    if args.json:
        _write_json({"ok": ok, "checks": checks})
    else:
        output = sys.stdout if ok else sys.stderr
        print("Preflight {}.".format("passed" if ok else "failed"), file=output)
        for check in checks:
            marker = "ok" if check["ok"] else "failed"
            print("[{}] {}: {}".format(marker, check["name"], check["message"]), file=output)
    return 0 if ok else 1


def _command_evidence(args: argparse.Namespace) -> int:
    from diagram_grill.evidence import read_evidence_files

    files = read_evidence_files(args.repo, args.paths)
    if args.json:
        _write_json(
            {
                "ok": True,
                "files": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                        "lines": [
                            {"number": number, "text": line}
                            for number, line in enumerate(item.lines, start=1)
                        ],
                    }
                    for item in files
                ],
            }
        )
        return 0

    for index, item in enumerate(files):
        if index:
            print()
        print("== {} (sha256: {}) ==".format(item.path, item.sha256))
        for number, line in enumerate(item.lines, start=1):
            print("{}: {}".format(number, line))
    return 0


def _catalog_entry_payload(entry: Any) -> Dict[str, str]:
    return {
        "import_path": entry.import_path,
        "provider": entry.provider,
        "category": entry.category,
        "class_name": entry.class_name,
    }


def _command_catalog(args: argparse.Namespace) -> int:
    entries = search_node_catalog(
        args.query,
        provider=args.provider,
        category=args.category,
        limit=args.limit,
    )
    results = [_catalog_entry_payload(entry) for entry in entries]
    if args.json:
        _write_json({"ok": True, "results": results})
    elif results:
        for entry in results:
            print(entry["import_path"])
    else:
        print("No catalog entries matched.")
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    load_and_validate(args.spec, mode=args.mode)
    if args.json:
        _write_json(
            {
                "ok": True,
                "mode": args.mode,
                "spec": str(args.spec),
            }
        )
    else:
        print("Valid for {}: {}".format(args.mode, args.spec))
    return 0


def _inline(value: Any) -> str:
    return " ".join(str(value).split()).replace("`", "'")


def _evidence_text(evidence: Mapping[str, Any]) -> str:
    if evidence.get("kind") == "decision":
        return "decision `{}`".format(_inline(evidence.get("decision_id", "unknown")))

    path = _inline(evidence.get("path", "unknown"))
    line_start = evidence.get("line_start")
    line_end = evidence.get("line_end")
    if line_start is not None and line_end is not None:
        return "`{}:{}-{}`".format(path, line_start, line_end)
    if line_start is not None:
        return "`{}:{}`".format(path, line_start)
    return "`{}`".format(path)


def _evidence_list(item: Mapping[str, Any]) -> str:
    evidence = item.get("evidence", [])
    if not isinstance(evidence, list) or not evidence:
        return "none"
    return "; ".join(
        _evidence_text(entry) for entry in evidence if isinstance(entry, Mapping)
    ) or "none"


def _group_lines(groups: Sequence[Mapping[str, Any]]) -> List[str]:
    children: Dict[Optional[str], List[Mapping[str, Any]]] = {}
    for group in groups:
        parent = group.get("parent")
        children.setdefault(parent if isinstance(parent, str) else None, []).append(group)

    lines: List[str] = []

    def visit(group: Mapping[str, Any], depth: int) -> None:
        group_id = str(group.get("id", "unknown"))
        lines.append(
            "{}- `{}` — {}".format(
                "  " * depth,
                _inline(group_id),
                _inline(group.get("label", group_id)),
            )
        )
        for child in children.get(group_id, []):
            visit(child, depth + 1)

    for root in children.get(None, []):
        visit(root, 0)
    return lines


def format_architecture_brief(spec: Mapping[str, Any]) -> str:
    """Format an already draft-validated specification as concise Markdown."""

    approval = spec.get("approval", {})
    status = approval.get("status", "unknown") if isinstance(approval, Mapping) else "unknown"
    groups = spec.get("groups", [])
    nodes = spec.get("nodes", [])
    edges = spec.get("edges", [])
    decisions = spec.get("decisions", [])
    assumptions = spec.get("assumptions", [])

    lines = [
        "# Architecture Brief: {}".format(_inline(spec.get("title", "Untitled"))),
        "",
        "- Status: `{}`".format(_inline(status)),
        "- Direction: `{}`".format(_inline(spec.get("direction", "unknown"))),
        "- Slug: `{}`".format(_inline(spec.get("slug", "unknown"))),
        "",
        "## Groups",
        "",
    ]
    group_lines = _group_lines(groups if isinstance(groups, list) else [])
    lines.extend(group_lines or ["- None."])

    lines.extend(["", "## Nodes", ""])
    if isinstance(nodes, list) and nodes:
        for node in nodes:
            group = node.get("group") or "ungrouped"
            fallback = node.get("icon_fallback_for")
            fallback_text = (
                "; generic fallback for: {}".format(_inline(fallback)) if fallback else ""
            )
            lines.append(
                "- `{}` — {} — status: `{}`; group: `{}`; icon: `{}`{}".format(
                    _inline(node.get("id", "unknown")),
                    _inline(node.get("label", "unknown")),
                    _inline(node.get("status", "unknown")),
                    _inline(group),
                    _inline(node.get("icon", "unknown")),
                    fallback_text,
                )
            )
            lines.append("  - Evidence: {}".format(_evidence_list(node)))
    else:
        lines.append("- None.")

    lines.extend(["", "## Connections", ""])
    if isinstance(edges, list) and edges:
        for edge in edges:
            label = edge.get("label")
            label_text = " — {}".format(_inline(label)) if label else ""
            lines.append(
                "- `{}` → `{}` — `{}`{}".format(
                    _inline(edge.get("from", "unknown")),
                    _inline(edge.get("to", "unknown")),
                    _inline(edge.get("type", "unknown")),
                    label_text,
                )
            )
            lines.append("  - Evidence: {}".format(_evidence_list(edge)))
    else:
        lines.append("- None.")

    lines.extend(["", "## Decisions", ""])
    if isinstance(decisions, list) and decisions:
        for decision in decisions:
            lines.append(
                "- `{}` — {} → {}".format(
                    _inline(decision.get("id", "unknown")),
                    _inline(decision.get("question", "unknown")),
                    _inline(decision.get("answer", "unknown")),
                )
            )
    else:
        lines.append("- None.")

    lines.extend(["", "## Assumptions", ""])
    if isinstance(assumptions, list) and assumptions:
        for assumption in assumptions:
            lines.append(
                "- `{}` (`{}`) — {}".format(
                    _inline(assumption.get("id", "unknown")),
                    _inline(assumption.get("status", "unknown")),
                    _inline(assumption.get("statement", "unknown")),
                )
            )
            lines.append("  - Evidence: {}".format(_evidence_list(assumption)))
    else:
        lines.append("- None.")

    unknowns: List[str] = []
    if isinstance(nodes, list):
        unknowns.extend(
            "- Node `{}` — {}".format(
                _inline(node.get("id", "unknown")),
                _inline(node.get("label", "unknown")),
            )
            for node in nodes
            if node.get("status") == "unknown"
        )
    if isinstance(assumptions, list):
        unknowns.extend(
            "- Assumption `{}` — {}".format(
                _inline(assumption.get("id", "unknown")),
                _inline(assumption.get("statement", "unknown")),
            )
            for assumption in assumptions
            if assumption.get("status") == "unresolved"
        )
    lines.extend(["", "## Explicit unknowns", ""])
    lines.extend(unknowns or ["- None."])

    node_count = len(nodes) if isinstance(nodes, list) else 0
    view_plan = (
        "overview plus focused detail diagrams"
        if node_count > 12
        else "one architecture diagram"
    )
    lines.extend(
        [
            "",
            "## Render plan",
            "",
            "- View: {}.".format(view_plan),
            "- Bundle: `diagrams/{}/`.".format(_inline(spec.get("slug", "unknown"))),
            "- Required outputs: canonical JSON, generated Python, SVG, and manifest.",
            "- Visual system: fixed defaults; no theme selection.",
            "- Approval: no files will be written until this brief is explicitly approved.",
        ]
    )
    return "\n".join(lines) + "\n"


def _command_brief(args: argparse.Namespace) -> int:
    spec = load_and_validate(args.spec, mode="draft")
    print(format_architecture_brief(spec), end="")
    return 0


def _read_stdin_spec() -> Any:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    payload = stream.read(MAX_STDIN_SPEC_BYTES + 1)
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    else:
        payload_bytes = payload
    if len(payload_bytes) > MAX_STDIN_SPEC_BYTES:
        raise ValueError(
            "stdin specification exceeds the {}-byte limit".format(
                MAX_STDIN_SPEC_BYTES
            )
        )
    return json.loads(payload_bytes.decode("utf-8"))


def _command_build(args: argparse.Namespace) -> int:
    formats = ("svg", "png") if args.png else ("svg",)
    spec_source: Any = _read_stdin_spec() if args.spec == Path("-") else args.spec
    bundle = build_bundle(
        spec_source,
        args.workspace,
        formats=formats,
        update=args.update,
        timeout_seconds=args.timeout,
    )
    if args.json:
        _write_json(
            {
                "ok": True,
                "bundle": str(bundle),
                "formats": list(formats),
                "updated": bool(args.update),
            }
        )
    else:
        print("Built diagram bundle: {}".format(bundle))
    return 0


_OPERATIONAL_ERRORS = (json.JSONDecodeError, OSError, RuntimeError)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        _require_external_skill_install(args)
        _activate_application_imports()
        return int(args.handler(args))
    except _OPERATIONAL_ERRORS as error:
        return _report_failure(error, bool(getattr(args, "json", False)))
    except (TypeError, ValueError) as error:
        # Public catalog and renderer helpers use these for invalid operational
        # values discovered after argparse has validated the CLI surface.
        return _report_failure(error, bool(getattr(args, "json", False)))


if __name__ == "__main__":
    raise SystemExit(main())
