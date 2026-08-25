#!/usr/bin/env python3
"""Start the host evaluator without ambient Python startup code.

Invoke this file with an absolute, dedicated evaluator interpreter and both
``-I`` and ``-S``.  This module intentionally imports only ``sys`` until those
flags have been verified.  It validates every interpreter and import root
before making the evaluator package importable or allowing ``codex_runner`` to
read a credential from stdin.
"""

import sys


class BootstrapError(RuntimeError):
    """Raised when the host evaluator trust boundary is not explicit."""


_BOOTSTRAP_OPTIONS = (
    "--evaluator-prefix",
    "--results-root",
    "--session-state-root",
)
_RUN_DIRECTORY_COMMANDS = frozenset(
    ("launch", "reply", "close", "finalize-review", "status")
)
_RUNNER_COMMANDS = frozenset(
    ("plan", "prepare", "launch", "reply", "close", "finalize-review", "status")
)


def _require_isolated_startup():
    if sys.version_info < (3, 9):
        raise BootstrapError("the evaluator requires Python 3.9 or newer")
    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.ignore_environment != 1
    ):
        raise BootstrapError(
            "invoke the dedicated evaluator Python with both -I and -S"
        )
    if "site" in sys.modules:
        raise BootstrapError("site was imported before evaluator validation")


def _load_stdlib():
    # These imports happen only after -I/-S and environment isolation are
    # established.  No repository or site-packages path is available yet.
    import _imp

    if sys.implementation.name != "cpython" or not all(
        _imp.is_frozen(name) for name in ("os", "stat")
    ):
        raise BootstrapError("the evaluator requires frozen CPython path modules")
    global os, stat
    import os
    import stat

    if os.name != "posix":
        raise BootstrapError("the Docker evaluator bootstrap requires POSIX")


def _parse_bootstrap_args(argv):
    values = {}
    index = 0
    while index < len(argv) and argv[index] != "--":
        option = argv[index]
        if option not in _BOOTSTRAP_OPTIONS:
            raise BootstrapError(
                "unknown bootstrap option {!r}; expected {} then --".format(
                    option, ", ".join(_BOOTSTRAP_OPTIONS)
                )
            )
        if option in values:
            raise BootstrapError("bootstrap option was repeated: {}".format(option))
        index += 1
        if index >= len(argv) or argv[index] == "--":
            raise BootstrapError("bootstrap option needs a value: {}".format(option))
        values[option] = argv[index]
        index += 1
    if index >= len(argv) or argv[index] != "--":
        raise BootstrapError("separate bootstrap and runner arguments with --")
    missing = [option for option in _BOOTSTRAP_OPTIONS if option not in values]
    if missing:
        raise BootstrapError(
            "missing bootstrap option(s): {}".format(", ".join(missing))
        )
    runner_argv = list(argv[index + 1 :])
    if not runner_argv:
        raise BootstrapError("a codex_runner command is required after --")
    if runner_argv[0] not in _RUNNER_COMMANDS:
        raise BootstrapError("unknown codex_runner command: {}".format(runner_argv[0]))
    target_options = ("--results-root", "--session-state-root")
    for argument in runner_argv[1:]:
        option_name = argument.split("=", 1)[0]
        if option_name.startswith("--") and any(
            target.startswith(option_name) for target in target_options
        ):
            raise BootstrapError(
                "results and session-state roots are bootstrap-owned arguments"
            )
    return values, runner_argv


def _check_exact_case(path, label):
    if not os.path.isabs(path):
        raise BootstrapError("{} must be absolute".format(label))
    current = os.path.sep
    components = path.split(os.path.sep)[1:]
    for component in components:
        if not component:
            raise BootstrapError(
                "{} must use one canonical path spelling".format(label)
            )
        try:
            with os.scandir(current) as entries:
                if not any(entry.name == component for entry in entries):
                    raise BootstrapError(
                        "{} does not use the filesystem's exact path spelling".format(
                            label
                        )
                    )
        except BootstrapError:
            raise
        except OSError as exc:
            raise BootstrapError("{} cannot be inspected".format(label)) from exc
        current = os.path.join(current, component)


def _identity(path, follow_links):
    result = os.stat(path) if follow_links else os.lstat(path)
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _validate_existing_path(label, value, kind):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BootstrapError("{} is missing or malformed".format(label))
    if not os.path.isabs(value):
        raise BootstrapError("{} must be absolute".format(label))
    raw = os.path.normpath(value)
    if raw != value:
        raise BootstrapError("{} must use one canonical path spelling".format(label))
    _check_exact_case(raw, label)
    resolved = os.path.realpath(raw)
    _check_exact_case(resolved, label + " (resolved)")
    try:
        followed = os.stat(raw)
        if not os.path.samefile(raw, resolved):
            raise BootstrapError("{} changed while resolving".format(label))
    except OSError as exc:
        raise BootstrapError("{} does not exist".format(label)) from exc
    if kind == "directory" and not stat.S_ISDIR(followed.st_mode):
        raise BootstrapError("{} must be a directory".format(label))
    if kind == "file" and not stat.S_ISREG(followed.st_mode):
        raise BootstrapError("{} must be a regular file".format(label))
    if followed.st_mode & 0o022:
        raise BootstrapError("{} must not be group/world writable".format(label))
    return {
        "label": label,
        "raw": raw,
        "resolved": resolved,
        "kind": kind,
        "lidentity": _identity(raw, False),
        "identity": _identity(raw, True),
    }


def _ancestors(path):
    current = os.path.normpath(path)
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _samefile(left, right):
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _record_is_within(root, child):
    for child_path in (child["raw"], child["resolved"]):
        for ancestor in _ancestors(child_path):
            if _samefile(root["raw"], ancestor) or _samefile(
                root["resolved"], ancestor
            ):
                return True
    return False


def _records_overlap(left, right):
    return _record_is_within(left, right) or _record_is_within(right, left)


def _require_disjoint(candidate, forbidden):
    for root in forbidden:
        if _records_overlap(candidate, root):
            raise BootstrapError(
                "{} overlaps {}".format(candidate["label"], root["label"])
            )


def _revalidate(record):
    try:
        if os.path.realpath(record["raw"]) != record["resolved"]:
            raise BootstrapError("{} changed after validation".format(record["label"]))
        if _identity(record["raw"], False) != record["lidentity"]:
            raise BootstrapError("{} changed after validation".format(record["label"]))
        if _identity(record["raw"], True) != record["identity"]:
            raise BootstrapError("{} changed after validation".format(record["label"]))
        if not os.path.samefile(record["raw"], record["resolved"]):
            raise BootstrapError("{} changed after validation".format(record["label"]))
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            "{} changed after validation".format(record["label"])
        ) from exc


def _lexically_within(root, path):
    try:
        return os.path.commonpath((root["resolved"], path)) == root["resolved"]
    except ValueError:
        return False


def _validate_initial_sys_path(forbidden, allowed):
    for index, entry in enumerate(sys.path):
        label = "initial sys.path[{}]".format(index)
        if not isinstance(entry, str) or not entry or not os.path.isabs(entry):
            raise BootstrapError("{} must be a non-empty absolute path".format(label))
        normalized = os.path.normpath(entry)
        if normalized != entry:
            raise BootstrapError("{} is not canonically spelled".format(label))
        if os.path.exists(entry):
            record = _validate_existing_path(
                label, entry, "directory" if os.path.isdir(entry) else "file"
            )
            _require_disjoint(record, forbidden)
            if not any(_record_is_within(root, record) for root in allowed):
                raise BootstrapError(
                    "{} is outside the validated base prefixes".format(label)
                )
            continue
        resolved = os.path.realpath(entry)
        for root in forbidden:
            try:
                common = os.path.commonpath((resolved, root["resolved"]))
            except ValueError:
                continue
            if common in (resolved, root["resolved"]):
                raise BootstrapError("{} overlaps {}".format(label, root["label"]))
        if not any(_lexically_within(root, resolved) for root in allowed):
            raise BootstrapError(
                "{} is outside the validated base prefixes".format(label)
            )


def _load_sysconfig(base_prefix, base_exec_prefix):
    global sysconfig
    import sysconfig

    source = getattr(sysconfig, "__file__", None)
    if not isinstance(source, str):
        raise BootstrapError("sysconfig has no inspectable standard-library source")
    record = _validate_existing_path("sysconfig source", source, "file")
    if not (
        _record_is_within(base_prefix, record)
        or _record_is_within(base_exec_prefix, record)
    ):
        raise BootstrapError("sysconfig was imported outside the base prefixes")
    return record


def _manual_library_roots(evaluator_prefix, base_prefix, base_exec_prefix):
    scheme = "nt" if os.name == "nt" else "posix_prefix"
    if scheme not in sysconfig.get_scheme_names():
        raise BootstrapError("the evaluator Python has no supported install scheme")
    paths = sysconfig.get_paths(
        scheme=scheme,
        vars={
            "base": evaluator_prefix["resolved"],
            "platbase": evaluator_prefix["resolved"],
            "installed_base": base_prefix["resolved"],
            "installed_platbase": base_exec_prefix["resolved"],
            "prefix": evaluator_prefix["resolved"],
            "exec_prefix": evaluator_prefix["resolved"],
        },
    )
    records = []
    for name in ("purelib", "platlib"):
        value = paths.get(name)
        record = _validate_existing_path(
            "manual evaluator {}".format(name), value, "directory"
        )
        if not any(_samefile(record["raw"], item["raw"]) for item in records):
            records.append(record)
    if not records:
        raise BootstrapError("the evaluator has no validated library root")
    return records


def _validate_command_targets(runner_argv, results_root):
    command = runner_argv[0]
    if command not in _RUN_DIRECTORY_COMMANDS:
        return None
    if len(runner_argv) < 2:
        raise BootstrapError("{} requires an absolute run directory".format(command))
    run_dir = _validate_existing_path(
        "run directory", runner_argv[1], "directory"
    )
    if not _record_is_within(results_root, run_dir):
        raise BootstrapError("run directory is outside the fixed results root")
    return run_dir


def _run(argv):
    values, runner_argv = _parse_bootstrap_args(argv)

    script = _validate_existing_path("evaluator bootstrap", __file__, "file")
    if os.path.basename(script["raw"]) != "evaluator_bootstrap.py":
        raise BootstrapError("the evaluator bootstrap has an unexpected filename")
    evals_root = _validate_existing_path(
        "evaluator source root", os.path.dirname(script["raw"]), "directory"
    )
    repository_root = _validate_existing_path(
        "repository root", os.path.dirname(evals_root["raw"]), "directory"
    )
    expected_script = os.path.join(
        evals_root["resolved"], "evaluator_bootstrap.py"
    )
    if not _samefile(script["raw"], expected_script):
        raise BootstrapError(
            "the bootstrap is not inside the exact evaluator source root"
        )

    results_root = _validate_existing_path(
        "results root", values["--results-root"], "directory"
    )
    state_root = _validate_existing_path(
        "session-state root", values["--session-state-root"], "directory"
    )
    evaluator_prefix = _validate_existing_path(
        "dedicated evaluator prefix", values["--evaluator-prefix"], "directory"
    )
    roots = (repository_root, results_root, state_root)
    _require_disjoint(results_root, (repository_root, state_root))
    _require_disjoint(state_root, (repository_root, results_root))
    _require_disjoint(evaluator_prefix, roots)

    executable = _validate_existing_path("sys.executable", sys.executable, "file")
    base_executable_value = getattr(sys, "_base_executable", None)
    base_executable = _validate_existing_path(
        "sys._base_executable", base_executable_value, "file"
    )
    prefix = _validate_existing_path("sys.prefix", sys.prefix, "directory")
    exec_prefix = _validate_existing_path(
        "sys.exec_prefix", sys.exec_prefix, "directory"
    )
    base_prefix = _validate_existing_path(
        "sys.base_prefix", sys.base_prefix, "directory"
    )
    base_exec_prefix = _validate_existing_path(
        "sys.base_exec_prefix", sys.base_exec_prefix, "directory"
    )
    interpreter_records = (
        executable,
        base_executable,
        prefix,
        exec_prefix,
        base_prefix,
        base_exec_prefix,
    )
    for record in interpreter_records:
        _require_disjoint(record, roots)

    if not _record_is_within(evaluator_prefix, executable):
        raise BootstrapError("sys.executable is outside the dedicated evaluator prefix")
    if _records_overlap(evaluator_prefix, base_prefix) or _records_overlap(
        evaluator_prefix, base_exec_prefix
    ):
        raise BootstrapError(
            "the evaluator prefix must be dedicated, not the base Python"
        )
    if not (
        _samefile(prefix["raw"], evaluator_prefix["raw"])
        or _samefile(prefix["raw"], base_prefix["raw"])
    ):
        raise BootstrapError("sys.prefix is neither the evaluator nor base prefix")
    if not (
        _samefile(exec_prefix["raw"], evaluator_prefix["raw"])
        or _samefile(exec_prefix["raw"], base_exec_prefix["raw"])
    ):
        raise BootstrapError("sys.exec_prefix is neither the evaluator nor base prefix")
    if not (
        _record_is_within(base_prefix, base_executable)
        or _record_is_within(base_exec_prefix, base_executable)
    ):
        raise BootstrapError("sys._base_executable is outside the base prefixes")

    _validate_initial_sys_path(roots, (base_prefix, base_exec_prefix))
    sysconfig_source = _load_sysconfig(base_prefix, base_exec_prefix)
    library_roots = _manual_library_roots(
        evaluator_prefix, base_prefix, base_exec_prefix
    )
    for record in library_roots:
        _require_disjoint(record, roots)
        if not _record_is_within(evaluator_prefix, record):
            raise BootstrapError(
                "{} is outside the dedicated evaluator prefix".format(record["label"])
            )

    run_dir = _validate_command_targets(runner_argv, results_root)

    if runner_argv[0] == "prepare":
        runner_argv = [
            "prepare",
            "--results-root",
            results_root["resolved"],
            "--session-state-root",
            state_root["resolved"],
        ] + runner_argv[1:]

    validated = [
        script,
        evals_root,
        repository_root,
        results_root,
        state_root,
        evaluator_prefix,
        sysconfig_source,
    ] + list(interpreter_records) + list(library_roots)
    if run_dir is not None:
        validated.append(run_dir)
    for record in validated:
        _revalidate(record)

    sys.path.insert(0, evals_root["resolved"])
    for record in library_roots:
        already_present = any(
            _samefile(record["resolved"], entry)
            for entry in sys.path
            if os.path.exists(entry)
        )
        if not already_present:
            sys.path.append(record["resolved"])

    runner_source = _validate_existing_path(
        "codex_runner source",
        os.path.join(evals_root["resolved"], "codex_runner.py"),
        "file",
    )
    _revalidate(runner_source)
    codex_runner = __import__("codex_runner")
    imported_source = getattr(codex_runner, "__file__", None)
    if not isinstance(imported_source, str) or not _samefile(
        runner_source["raw"], imported_source
    ):
        raise BootstrapError(
            "codex_runner was not imported from the exact evaluator root"
        )
    return int(codex_runner.main(runner_argv))


def main(argv=None):
    try:
        _require_isolated_startup()
        _load_stdlib()
        arguments = sys.argv[1:] if argv is None else list(argv)
        return _run(arguments)
    except (BootstrapError, OSError, ValueError) as exc:
        print("diagram-grill-evaluator-bootstrap: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
