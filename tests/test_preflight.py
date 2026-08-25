import hashlib
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "diagram-grill"
    / "scripts"
    / "diagram_grill"
    / "preflight.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "diagram_grill_preflight_test_target",
    MODULE_PATH,
)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Could not load preflight module from {MODULE_PATH}")
PREFLIGHT = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = PREFLIGHT
MODULE_SPEC.loader.exec_module(PREFLIGHT)
run_preflight = PREFLIGHT.run_preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.workspace_directory = tempfile.TemporaryDirectory()
        self.tools_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.workspace_directory.name)
        self.dot_path = Path(self.tools_directory.name) / "dot"
        self.dot_path.write_text("test executable", encoding="utf-8")
        self.dot_path.chmod(0o755)
        self.dot_path = self.dot_path.resolve()
        self.versions = {
            "diagrams": "0.25.1",
            "graphviz": "0.20.3",
            "jsonschema": "4.25.1",
        }

    def tearDown(self):
        self.tools_directory.cleanup()
        self.workspace_directory.cleanup()

    def run_checks(self, command_runner, versions=None, dot_candidate="default"):
        selected_versions = versions or self.versions
        selected_dot = self.dot_path if dot_candidate == "default" else dot_candidate

        def version_getter(distribution):
            try:
                return selected_versions[distribution]
            except KeyError:
                raise metadata.PackageNotFoundError(distribution)

        return run_preflight(
            python_version=(3, 11, 9),
            version_getter=version_getter,
            module_importer=lambda name: object(),
            command_runner=command_runner,
            executable_finder=lambda name, **kwargs: (
                str(selected_dot) if selected_dot is not None else None
            ),
            environment={"PATH": os.pathsep.join(("/test/bin", "/other/bin"))},
            forbidden_roots=(self.workspace,),
            dot_timeout_seconds=0.25,
        )

    @staticmethod
    def by_name(result):
        return {check.name: check for check in result.checks}

    def test_success_returns_immutable_structured_result(self):
        def command_runner(command, **kwargs):
            self.assertEqual([str(self.dot_path), "-V"], command)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(0.25, kwargs["timeout"])
            self.assertEqual(str(self.dot_path.parent), kwargs["cwd"])
            self.assertEqual(
                {
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": str(self.dot_path.parent),
                },
                kwargs["env"],
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="dot - graphviz version 12.2.1 (20241206.2353)",
            )

        result = self.run_checks(command_runner)

        self.assertTrue(result.ok)
        self.assertEqual(5, len(result.checks))
        self.assertEqual("12.2.1", self.by_name(result)["dot"].actual)
        self.assertEqual(str(self.dot_path), result.dot_path)
        self.assertEqual(
            hashlib.sha256(self.dot_path.read_bytes()).hexdigest(),
            result.dot_sha256,
        )
        self.assertEqual(self.dot_path.stat().st_ino, result.dot_identity.inode)
        with self.assertRaises(FrozenInstanceError):
            result.ok = False

    def test_missing_dot_is_actionable(self):
        def command_runner(command, **kwargs):
            self.fail("dot -V must not run when executable resolution fails")

        result = self.run_checks(command_runner, dot_candidate=None)
        dot = self.by_name(result)["dot"]

        self.assertFalse(result.ok)
        self.assertFalse(dot.ok)
        self.assertIn("not found on PATH", dot.message)
        self.assertTrue(all(check.ok for check in result.checks if check.name != "dot"))

    def test_relative_and_workspace_dot_candidates_are_rejected_before_execution(self):
        def command_runner(command, **kwargs):
            self.fail("an untrusted dot candidate must not execute")

        relative = self.run_checks(command_runner, dot_candidate="bin/dot")
        self.assertIn("relative", self.by_name(relative)["dot"].message)
        self.assertIsNone(relative.dot_path)

        repository_dot = self.workspace / "bin" / "dot"
        repository_dot.parent.mkdir()
        repository_dot.write_text("untrusted", encoding="utf-8")
        repository_dot.chmod(0o755)
        contained = self.run_checks(command_runner, dot_candidate=repository_dot)
        self.assertIn("workspace or repository", self.by_name(contained)["dot"].message)
        self.assertIsNone(contained.dot_path)

    @unittest.skipUnless(shutil.which("dot"), "Graphviz dot is not installed")
    def test_installed_dot_satisfies_the_local_path_trust_policy(self):
        installed_dot = Path(shutil.which("dot")).resolve()
        result = self.run_checks(subprocess.run, dot_candidate=installed_dot)

        self.assertTrue(self.by_name(result)["dot"].ok)
        self.assertEqual(str(installed_dot), result.dot_path)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_world_writable_parent_is_rejected(self):
        managed_root = Path(self.tools_directory.name) / "managed"
        managed_root.mkdir()
        managed_dot = managed_root / "dot"
        managed_dot.write_text("managed", encoding="utf-8")
        managed_dot.chmod(0o755)

        completed = lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="dot - graphviz version 12.2.1 (0)",
            stderr="",
        )
        managed_root.chmod(0o777)
        rejected = self.run_checks(completed, dot_candidate=managed_dot.resolve())
        self.assertFalse(self.by_name(rejected)["dot"].ok)
        self.assertIn("world-writable", self.by_name(rejected)["dot"].message)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_group_write_has_only_a_narrow_macos_homebrew_admin_exception(self):
        component_stat = SimpleNamespace(st_mode=0o40775, st_uid=501, st_gid=80)
        admin_group = SimpleNamespace(
            gr_name="admin", gr_gid=80, gr_mem=["root", "current"]
        )
        users = {
            "root": SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0),
            "current": SimpleNamespace(pw_name="current", pw_uid=501, pw_gid=20),
        }
        lookup_user = users.__getitem__
        all_users = lambda: list(users.values())

        accepted = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "darwin",
            group_lookup=lambda gid: admin_group,
            user_lookup=lookup_user,
            all_users_lookup=all_users,
        )
        linux = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "linux",
            group_lookup=lambda gid: admin_group,
            user_lookup=lookup_user,
            all_users_lookup=all_users,
        )
        other_tree = PREFLIGHT._posix_group_write_trust_failure(
            Path("/usr/local/bin"),
            component_stat,
            501,
            "darwin",
            group_lookup=lambda gid: admin_group,
            user_lookup=lookup_user,
            all_users_lookup=all_users,
        )

        self.assertIsNone(accepted)
        self.assertIn("outside the narrow", linux)
        self.assertIn("outside the narrow", other_tree)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_homebrew_group_write_rejects_unexpected_group_or_lookup_failure(self):
        component_stat = SimpleNamespace(st_mode=0o40775, st_uid=501, st_gid=81)
        wrong_group = SimpleNamespace(gr_name="wheel", gr_gid=81, gr_mem=[])

        mismatch = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "darwin",
            group_lookup=lambda gid: wrong_group,
        )

        def failed_group_lookup(group_id):
            raise KeyError(group_id)

        lookup_failure = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "darwin",
            group_lookup=failed_group_lookup,
        )

        self.assertIn("unexpected group", mismatch)
        self.assertIn("could not be resolved completely", lookup_failure)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_homebrew_group_write_rejects_untrusted_supplementary_member(self):
        component_stat = SimpleNamespace(st_mode=0o40775, st_uid=501, st_gid=80)
        admin_group = SimpleNamespace(
            gr_name="admin", gr_gid=80, gr_mem=["root", "current", "attacker"]
        )
        users = {
            "root": SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0),
            "current": SimpleNamespace(pw_name="current", pw_uid=501, pw_gid=20),
            "attacker": SimpleNamespace(pw_name="attacker", pw_uid=502, pw_gid=20),
        }

        failure = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "darwin",
            group_lookup=lambda gid: admin_group,
            user_lookup=users.__getitem__,
            all_users_lookup=lambda: list(users.values()),
        )

        self.assertIn("untrusted admin-group principal(s): attacker", failure)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_homebrew_group_write_rejects_untrusted_primary_group_member(self):
        component_stat = SimpleNamespace(st_mode=0o40775, st_uid=501, st_gid=80)
        admin_group = SimpleNamespace(
            gr_name="admin", gr_gid=80, gr_mem=["root", "current"]
        )
        users = {
            "root": SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0),
            "current": SimpleNamespace(pw_name="current", pw_uid=501, pw_gid=20),
        }
        primary_attacker = SimpleNamespace(
            pw_name="primary-attacker", pw_uid=503, pw_gid=80
        )

        failure = PREFLIGHT._posix_group_write_trust_failure(
            Path("/opt/homebrew/Cellar"),
            component_stat,
            501,
            "darwin",
            group_lookup=lambda gid: admin_group,
            user_lookup=users.__getitem__,
            all_users_lookup=lambda: list(users.values()) + [primary_attacker],
        )

        self.assertIn("untrusted admin-group principal(s): primary-attacker", failure)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_path_component_owned_by_an_untrusted_uid_is_rejected(self):
        component_stat = SimpleNamespace(
            st_mode=0o40755,
            st_uid=502,
            st_gid=20,
        )

        with mock.patch.object(Path, "stat", return_value=component_stat):
            failure = PREFLIGHT.posix_executable_trust_failure(
                Path("/opt/homebrew/bin/dot"),
                effective_uid=501,
                platform_name="darwin",
                acl_checker=lambda component, platform: None,
            )

        self.assertIn("owned by untrusted uid 502", failure)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_extended_acl_failure_is_propagated(self):
        component_stat = SimpleNamespace(st_mode=0o40755, st_uid=501, st_gid=20)

        with mock.patch.object(Path, "stat", return_value=component_stat):
            failure = PREFLIGHT.posix_executable_trust_failure(
                Path("/opt/homebrew/bin/dot"),
                effective_uid=501,
                platform_name="darwin",
                acl_checker=lambda component, platform: "extended ACL rejected",
            )

        self.assertEqual("extended ACL rejected", failure)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL integration only")
    def test_real_macos_extended_acl_is_rejected(self):
        acl_root = Path(self.tools_directory.name) / "acl-root"
        acl_root.mkdir()
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow add_file,delete_child", str(acl_root)],
            check=True,
        )
        try:
            failure = PREFLIGHT._darwin_acl_trust_failure(acl_root)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(acl_root)], check=True)

        self.assertIn("extended macOS ACL", failure)

    def test_linux_acl_metadata_is_rejected(self):
        with mock.patch.object(
            PREFLIGHT.os,
            "listxattr",
            return_value=["system.posix_acl_access"],
            create=True,
        ):
            failure = PREFLIGHT._posix_acl_trust_failure(
                Path("/usr/bin/dot"),
                "linux",
            )

        self.assertIn("extended ACL metadata", failure)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy only")
    def test_dot_trust_is_rechecked_immediately_before_version_spawn(self):
        command_runner = mock.Mock()
        with mock.patch.object(
            PREFLIGHT,
            "posix_executable_trust_failure",
            side_effect=(None, "Graphviz path trust changed before spawn"),
        ) as trust_check:
            result = self.run_checks(command_runner)

        self.assertFalse(self.by_name(result)["dot"].ok)
        self.assertIn("trust changed before spawn", self.by_name(result)["dot"].message)
        self.assertEqual(2, trust_check.call_count)
        command_runner.assert_not_called()

    def test_symlink_candidate_returns_the_canonical_executable(self):
        alias_root = Path(self.tools_directory.name) / "alias"
        alias_root.mkdir()
        alias = alias_root / "dot"
        alias.symlink_to(self.dot_path)

        def command_runner(command, **kwargs):
            self.assertEqual([str(self.dot_path), "-V"], command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="dot - graphviz version 12.2.1 (0)",
                stderr="",
            )

        result = self.run_checks(command_runner, dot_candidate=alias)
        self.assertTrue(self.by_name(result)["dot"].ok)
        self.assertEqual(str(self.dot_path), result.dot_path)

    def test_dot_mutation_during_version_check_fails_closed(self):
        def command_runner(command, **kwargs):
            self.dot_path.write_text("mutated executable", encoding="utf-8")
            self.dot_path.chmod(0o755)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="dot - graphviz version 12.2.1 (0)",
                stderr="",
            )

        result = self.run_checks(command_runner)
        self.assertFalse(self.by_name(result)["dot"].ok)
        self.assertIn("changed during", self.by_name(result)["dot"].message)
        self.assertIsNone(result.dot_sha256)

    def test_mismatched_diagrams_version_fails(self):
        def command_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="dot - graphviz version 12.2.1 (0)",
                stderr="",
            )

        versions = dict(self.versions, diagrams="0.26.0")
        result = self.run_checks(command_runner, versions=versions)
        diagrams = self.by_name(result)["diagrams"]

        self.assertFalse(result.ok)
        self.assertEqual("0.25.1", diagrams.expected)
        self.assertEqual("0.26.0", diagrams.actual)
        self.assertIn("requires exactly", diagrams.message)

    def test_mismatched_graphviz_python_version_fails(self):
        def command_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="dot - graphviz version 12.2.1 (0)",
                stderr="",
            )

        versions = dict(self.versions, graphviz="0.21")
        result = self.run_checks(command_runner, versions=versions)
        graphviz_python = self.by_name(result)["graphviz_python"]

        self.assertFalse(result.ok)
        self.assertEqual("0.20.3", graphviz_python.expected)
        self.assertEqual("0.21", graphviz_python.actual)
        self.assertIn("requires exactly", graphviz_python.message)

    def test_dot_timeout_fails_without_raising(self):
        def command_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = self.run_checks(command_runner)
        dot = self.by_name(result)["dot"]

        self.assertFalse(result.ok)
        self.assertFalse(dot.ok)
        self.assertIn("did not finish within", dot.message)

    def test_dot_version_check_drops_loader_and_graphviz_environment(self):
        captured = {}

        def command_runner(command, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="dot - graphviz version 12.2.1 (0)",
                stderr="",
            )

        def version_getter(distribution):
            return self.versions[distribution]

        result = run_preflight(
            python_version=(3, 11, 9),
            version_getter=version_getter,
            module_importer=lambda name: object(),
            command_runner=command_runner,
            executable_finder=lambda name, **kwargs: str(self.dot_path),
            environment={
                "PATH": "/hostile/bin",
                "LD_PRELOAD": "/hostile/inject.so",
                "DYLD_INSERT_LIBRARIES": "/hostile/inject.dylib",
                "GV_FILE_PATH": "/hostile/graphviz",
            },
            forbidden_roots=(self.workspace,),
            dot_timeout_seconds=0.25,
        )

        self.assertTrue(self.by_name(result)["dot"].ok)
        self.assertEqual(str(self.dot_path.parent), captured["cwd"])
        self.assertNotIn("LD_PRELOAD", captured["env"])
        self.assertNotIn("DYLD_INSERT_LIBRARIES", captured["env"])
        self.assertNotIn("GV_FILE_PATH", captured["env"])

    def test_dot_command_failure_includes_exit_code(self):
        def command_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout="",
                stderr="dot: configuration error",
            )

        result = self.run_checks(command_runner)
        dot = self.by_name(result)["dot"]

        self.assertFalse(result.ok)
        self.assertEqual("exit code 2", dot.actual)
        self.assertIn("configuration error", dot.message)

    @unittest.skipUnless(os.name == "posix", "POSIX detached-process test")
    def test_bounded_runner_returns_when_detached_descendant_holds_pipes(self):
        child_source = "import time; time.sleep(60)"
        parent_source = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',{}], start_new_session=True); "
            "print(child.pid, flush=True); time.sleep(60)"
        ).format(repr(child_source))

        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            PREFLIGHT._run_bounded_command(
                [sys.executable, "-c", parent_source],
                capture_output=True,
                text=True,
                timeout=0.05,
                check=False,
                shell=False,
            )
        elapsed = time.monotonic() - started

        output = caught.exception.output
        if isinstance(output, bytes):
            output = output.decode("utf-8")
        child_pid = int(str(output).strip())
        try:
            self.assertLess(elapsed, 1.5)
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
