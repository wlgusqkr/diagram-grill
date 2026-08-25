import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPOSITORY_ROOT / "evals" / "runtime"
BWRAP_WRAPPER = RUNTIME_ROOT / "bwrap-wrapper.sh"
SUPERVISOR = RUNTIME_ROOT / "supervisor.py"
SNAPSHOT_EXPORT = RUNTIME_ROOT / "snapshot_export.py"
SNAPSHOT_IMPORT = RUNTIME_ROOT / "snapshot_import.py"

REQUIRED_BWRAP_ARGUMENTS = [
    "--unshare-user",
    "--unshare-pid",
    "--unshare-net",
    "--proc",
    "/proc",
    "--cap-drop",
    "ALL",
    "--",
    "/bin/true",
]
CODEX_SYSTEM_BWRAP_PROBE = [
    "--unshare-user",
    "--unshare-net",
    "--ro-bind",
    "/",
    "/",
    "/bin/true",
]


def expected_bwrap_arguments(arguments):
    forwarded = list(arguments)
    delimiter = forwarded.index("--")
    forwarded[delimiter:delimiter] = ["--tmpfs", "/tmp"]
    return forwarded

SUPERVISOR_BOOTSTRAP_PROBE = r"""
import importlib.util
import json
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
sentinel_path = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("eval_supervisor_probe", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.SENTINEL_PATH = str(sentinel_path)
captured = {}

def intercept_exec(executable, arguments, environment):
    captured["executable"] = executable
    captured["arguments"] = list(arguments)
    captured["environment"] = dict(environment)
    raise SystemExit(23)

module.os.execve = intercept_exec
try:
    module.main()
except SystemExit as error:
    if error.code != 23:
        raise

sentinel = sentinel_path.read_text(encoding="ascii")
environment = captured["environment"]
print(json.dumps({
    "arguments": captured["arguments"],
    "bootstrapped": environment[module.BOOTSTRAPPED_ENV],
    "environment_names": sorted(environment),
    "executable": captured["executable"],
    "mode": stat.S_IMODE(sentinel_path.stat().st_mode),
    "sentinel": sentinel,
    "sentinel_environment": environment[module.SENTINEL_ENV],
}, sort_keys=True))
"""

SNAPSHOT_EXPORT_PROBE = r"""
import importlib.util
import sys
import tarfile
from pathlib import Path

source = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("eval_snapshot_export_probe", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._require_quiescent_processes = lambda: None
try:
    raise SystemExit(module.main(sys.argv[2:]))
except (OSError, RuntimeError, tarfile.TarError) as error:
    print("diagram-grill-eval-snapshot: {}".format(error), file=sys.stderr)
    raise SystemExit(1)
"""


def credential_free_environment():
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class BwrapWrapperTests(unittest.TestCase):
    def wrapper_fixture(self, directory):
        wrapper = Path(directory) / "bwrap"
        shutil.copy2(BWRAP_WRAPPER, wrapper)
        wrapper.chmod(0o700)
        real = wrapper.with_name(wrapper.name + ".real")
        real.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@"\n',
            encoding="utf-8",
        )
        real.chmod(0o700)
        return wrapper

    def run_wrapper(self, wrapper, arguments):
        return subprocess.run(
            [str(wrapper), *arguments],
            env=credential_free_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )

    def test_complete_isolation_contract_is_forwarded_to_pinned_bwrap(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)

            completed = self.run_wrapper(wrapper, REQUIRED_BWRAP_ARGUMENTS)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            expected_bwrap_arguments(REQUIRED_BWRAP_ARGUMENTS),
            completed.stdout.splitlines(),
        )
        self.assertEqual("", completed.stderr)

    def test_exact_codex_system_bwrap_probe_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)

            completed = self.run_wrapper(wrapper, CODEX_SYSTEM_BWRAP_PROBE)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(CODEX_SYSTEM_BWRAP_PROBE, completed.stdout.splitlines())
        self.assertEqual("", completed.stderr)

    def test_exact_codex_capability_probe_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)

            completed = self.run_wrapper(wrapper, ["--help"])

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(["--help"], completed.stdout.splitlines())
        self.assertEqual("", completed.stderr)

    def test_each_required_isolation_argument_is_enforced(self):
        omissions = {
            "user namespace": {"--unshare-user"},
            "PID namespace": {"--unshare-pid"},
            "network namespace": {"--unshare-net"},
            "fresh proc": {"--proc", "/proc"},
            "capability drop": {"--cap-drop", "ALL"},
            "command delimiter": {"--"},
        }
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)
            for label, omitted in omissions.items():
                with self.subTest(label=label):
                    arguments = [
                        argument
                        for argument in REQUIRED_BWRAP_ARGUMENTS
                        if argument not in omitted
                    ]
                    completed = self.run_wrapper(wrapper, arguments)

                    self.assertEqual(125, completed.returncode)
                    self.assertIn("isolation contract is incomplete", completed.stderr)
                    self.assertEqual("", completed.stdout)

    def test_forbidden_or_weakened_isolation_arguments_are_rejected(self):
        cases = (
            ("share network", ["--share-net"], "forbidden bubblewrap argument"),
            ("disable userns", ["--disable-userns"], "forbidden bubblewrap argument"),
            (
                "userns block fd",
                ["--userns-block-fd"],
                "forbidden bubblewrap argument",
            ),
            (
                "host proc",
                [
                    "--unshare-user",
                    "--unshare-pid",
                    "--unshare-net",
                    "--proc",
                    "/host/proc",
                ],
                "unsafe bubblewrap argument after --proc",
            ),
            (
                "partial capability drop",
                [
                    "--unshare-user",
                    "--unshare-pid",
                    "--unshare-net",
                    "--proc",
                    "/proc",
                    "--cap-drop",
                    "CAP_SYS_ADMIN",
                ],
                "unsafe bubblewrap argument after --cap",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)
            for label, prefix, diagnostic in cases:
                with self.subTest(label=label):
                    arguments = prefix + ["--", "/bin/true"]
                    completed = self.run_wrapper(wrapper, arguments)

                    self.assertEqual(125, completed.returncode)
                    self.assertIn(diagnostic, completed.stderr)
                    self.assertEqual("", completed.stdout)

    def test_missing_pinned_bwrap_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.wrapper_fixture(directory)
            wrapper.with_name(wrapper.name + ".real").unlink()

            completed = self.run_wrapper(wrapper, REQUIRED_BWRAP_ARGUMENTS)

        self.assertEqual(125, completed.returncode)
        self.assertIn("pinned bubblewrap binary is unavailable", completed.stderr)


class SupervisorTests(unittest.TestCase):
    def bootstrap_probe(self, sentinel_path):
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                SUPERVISOR_BOOTSTRAP_PROBE,
                str(SUPERVISOR),
                str(sentinel_path),
            ],
            env=credential_free_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def patched_supervisor(self, directory, sentinel_path, ready_path=None):
        source = SUPERVISOR.read_text(encoding="utf-8")
        original = 'SENTINEL_PATH = "/tmp/outer-parent-sentinel"'
        replacement = "SENTINEL_PATH = {!r}".format(str(sentinel_path))
        self.assertIn(original, source)
        source = source.replace(original, replacement)
        if ready_path is not None:
            handler_marker = "    signal.signal(signal.SIGINT, _stop)\n"
            self.assertIn(handler_marker, source)
            source = source.replace(
                handler_marker,
                handler_marker
                + "    Path({!r}).write_text('ready', encoding='ascii')\n".format(
                    str(ready_path)
                ),
            )
        target = Path(directory) / "supervisor.py"
        target.write_text(source, encoding="utf-8")
        target.chmod(0o700)
        return target

    def wait_for_path(self, path, process, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    "supervisor exited before creating its sentinel: {}{}".format(
                        stdout, stderr
                    )
                )
            time.sleep(0.01)
        self.fail("supervisor did not create its sentinel before the deadline")

    def test_bootstrap_creates_a_fresh_credential_free_parent_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.bootstrap_probe(Path(directory) / "sentinel-one")
            second = self.bootstrap_probe(Path(directory) / "sentinel-two")

        for result in (first, second):
            self.assertRegex(result["sentinel"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["sentinel"], result["sentinel_environment"])
            self.assertEqual("1", result["bootstrapped"])
            self.assertEqual(0o400, result["mode"])
            self.assertEqual(sys.executable, result["executable"])
            self.assertEqual("-I", result["arguments"][1])
            self.assertEqual("-S", result["arguments"][2])
            credential_names = {
                name
                for name in result["environment_names"]
                if re.search(r"(?:API_KEY|ACCESS_TOKEN|AUTH_JSON|CREDENTIAL)", name)
            }
            self.assertEqual(set(), credential_names)
        self.assertNotEqual(first["sentinel"], second["sentinel"])

    def test_supervisor_bootstraps_then_terminates_without_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "outer-parent-sentinel"
            ready = Path(directory) / "supervisor-ready"
            supervisor = self.patched_supervisor(directory, sentinel, ready)
            environment = credential_free_environment()
            environment["DIAGRAM_GRILL_MAX_LIFETIME_SECONDS"] = "30"
            process = subprocess.Popen(
                [sys.executable, "-I", str(supervisor)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.wait_for_path(ready, process)
                self.assertTrue(sentinel.is_file())
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3)
            except BaseException:
                process.kill()
                process.communicate()
                raise

        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("", stdout)
        self.assertEqual("", stderr)

    def test_supervisor_watchdog_exits_without_host_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "outer-parent-sentinel"
            ready = Path(directory) / "supervisor-ready"
            supervisor = self.patched_supervisor(directory, sentinel, ready)
            environment = credential_free_environment()
            environment["DIAGRAM_GRILL_MAX_LIFETIME_SECONDS"] = "1"
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-I", str(supervisor)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=4,
            )
            ready_observed = ready.is_file()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(ready_observed)

    def test_preexisting_sentinel_is_rejected_without_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "outer-parent-sentinel"
            sentinel.write_text("already-used", encoding="ascii")
            supervisor = self.patched_supervisor(directory, sentinel)

            completed = subprocess.run(
                [sys.executable, "-I", str(supervisor)],
                env=credential_free_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=3,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("File exists", completed.stderr)


class SnapshotExporterTests(unittest.TestCase):
    def export(self, root, *, max_entries=32, max_bytes=4096, excludes=()):
        command = [
            sys.executable,
            "-I",
            "-c",
            SNAPSHOT_EXPORT_PROBE,
            str(SNAPSHOT_EXPORT),
            str(root),
            "--max-entries",
            str(max_entries),
            "--max-bytes",
            str(max_bytes),
        ]
        for excluded in excludes:
            command.extend(("--exclude", excluded))
        return subprocess.run(
            command,
            env=credential_free_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

    def test_safe_snapshot_is_a_normalized_link_free_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            (root / "nested").mkdir(parents=True)
            (root / "repo").mkdir()
            (root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
            (root / "nested" / "beta.txt").write_bytes(b"beta\x00")
            (root / "repo" / "excluded.txt").write_text(
                "not exported", encoding="utf-8"
            )

            completed = self.export(root, excludes=("repo",))

        self.assertEqual(0, completed.returncode, completed.stderr.decode())
        self.assertEqual(b"", completed.stderr)
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            members = {member.name.rstrip("/"): member for member in archive}
            self.assertEqual(
                {"alpha.txt", "nested", "nested/beta.txt"}, set(members)
            )
            self.assertEqual(b"alpha\n", archive.extractfile(members["alpha.txt"]).read())
            self.assertEqual(
                b"beta\x00", archive.extractfile(members["nested/beta.txt"]).read()
            )
            self.assertTrue(members["nested"].isdir())
            for name, member in members.items():
                self.assertNotIn("..", Path(name).parts)
                self.assertFalse(member.issym())
                self.assertFalse(member.islnk())
                self.assertEqual(0, member.uid)
                self.assertEqual(0, member.gid)
                self.assertEqual(0, member.mtime)
                self.assertEqual(0o700 if member.isdir() else 0o600, member.mode)

    def test_symlinks_and_special_files_are_rejected_before_archive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            symlink_root = base / "symlink"
            symlink_root.mkdir()
            (symlink_root / "target.txt").write_text("target", encoding="utf-8")
            (symlink_root / "link.txt").symlink_to("target.txt")

            symlink_result = self.export(symlink_root)

            fifo_root = base / "fifo"
            fifo_root.mkdir()
            os.mkfifo(fifo_root / "pipe")
            fifo_result = self.export(fifo_root)

        self.assertEqual(1, symlink_result.returncode)
        self.assertEqual(b"", symlink_result.stdout)
        self.assertIn(b"snapshot contains a symlink", symlink_result.stderr)
        self.assertEqual(1, fifo_result.returncode)
        self.assertEqual(b"", fifo_result.stdout)
        self.assertIn(b"snapshot contains a special file", fifo_result.stderr)

    def test_entry_and_byte_limits_fail_closed_before_archive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            root.mkdir()
            (root / "one.txt").write_bytes(b"1234")
            (root / "two.txt").write_bytes(b"5678")

            entry_result = self.export(root, max_entries=1)
            byte_result = self.export(root, max_bytes=7)
            invalid_result = self.export(root, max_entries=0)

        cases = (
            (entry_result, b"snapshot exceeds its entry limit"),
            (byte_result, b"snapshot exceeds its byte limit"),
            (invalid_result, b"snapshot limits must be positive"),
        )
        for result, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(1, result.returncode)
                self.assertEqual(b"", result.stdout)
                self.assertIn(diagnostic, result.stderr)


class SnapshotImporterTests(unittest.TestCase):
    def archive(self, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.mtime = 0
                if kind == "directory":
                    member.type = tarfile.DIRTYPE
                    member.mode = 0o700
                    archive.addfile(member)
                elif kind == "file":
                    member.type = tarfile.REGTYPE
                    member.mode = 0o600
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                elif kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = content.decode("utf-8")
                    archive.addfile(member)
                else:
                    raise AssertionError(kind)
        return output.getvalue()

    def run_import(self, target, archive, max_entries=32, max_bytes=4096):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(SNAPSHOT_IMPORT),
                str(target),
                "--max-entries",
                str(max_entries),
                "--max-bytes",
                str(max_bytes),
            ],
            input=archive,
            env=credential_free_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

    def test_safe_archive_is_imported_with_normalized_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot"
            target.mkdir()
            archive = self.archive(
                [
                    ("nested/", "directory", b""),
                    ("alpha.txt", "file", b"alpha\n"),
                    ("nested/beta.bin", "file", b"beta\x00"),
                ]
            )

            completed = self.run_import(target, archive)

            self.assertEqual(0, completed.returncode, completed.stderr.decode())
            self.assertEqual(b"alpha\n", (target / "alpha.txt").read_bytes())
            self.assertEqual(b"beta\x00", (target / "nested" / "beta.bin").read_bytes())
            self.assertEqual(0o600, stat.S_IMODE((target / "alpha.txt").stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE((target / "nested").stat().st_mode))

    def test_traversal_links_and_casefold_aliases_are_rejected(self):
        cases = (
            ([('../escape', 'file', b'x')], b"escapes its root"),
            ([('link', 'symlink', b'target')], b"link or special file"),
            (
                [('Readme', 'file', b'a'), ('README', 'file', b'b')],
                b"duplicate member",
            ),
        )
        for entries, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "snapshot"
                target.mkdir()
                completed = self.run_import(target, self.archive(entries))
                self.assertEqual(1, completed.returncode)
                self.assertIn(diagnostic, completed.stderr)
                self.assertEqual([], list(target.iterdir()))

    def test_entry_byte_transport_and_nonempty_root_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = self.archive(
                [('one', 'file', b'1234'), ('two', 'file', b'5678')]
            )
            entry_target = base / "entry"
            byte_target = base / "byte"
            transport_target = base / "transport"
            nonempty_target = base / "nonempty"
            for target in (entry_target, byte_target, transport_target, nonempty_target):
                target.mkdir()
            (nonempty_target / "existing").write_text("x", encoding="utf-8")

            entry = self.run_import(entry_target, archive, max_entries=1)
            byte = self.run_import(byte_target, archive, max_bytes=7)
            transport = self.run_import(
                transport_target, b"x" * (8 * 1024 * 1024 + 2), max_bytes=1
            )
            nonempty = self.run_import(nonempty_target, archive)

        self.assertIn(b"entry limit", entry.stderr)
        self.assertIn(b"byte limit", byte.stderr)
        self.assertIn(b"transport limit", transport.stderr)
        self.assertIn(b"must be empty", nonempty.stderr)


if __name__ == "__main__":
    unittest.main()
