import importlib.util
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from importlib import metadata
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
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
        self.versions = {
            "diagrams": "0.25.1",
            "jsonschema": "4.25.1",
        }

    def run_checks(self, command_runner, versions=None):
        selected_versions = versions or self.versions

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
            dot_timeout_seconds=0.25,
        )

    @staticmethod
    def by_name(result):
        return {check.name: check for check in result.checks}

    def test_success_returns_immutable_structured_result(self):
        def command_runner(command, **kwargs):
            self.assertEqual(["dot", "-V"], command)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(0.25, kwargs["timeout"])
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
        with self.assertRaises(FrozenInstanceError):
            result.ok = False

    def test_missing_dot_is_actionable(self):
        def command_runner(command, **kwargs):
            raise FileNotFoundError("dot")

        result = self.run_checks(command_runner)
        dot = self.by_name(result)["dot"]

        self.assertFalse(result.ok)
        self.assertFalse(dot.ok)
        self.assertIn("not found on PATH", dot.message)
        self.assertTrue(all(check.ok for check in result.checks if check.name != "dot"))

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

    def test_dot_timeout_fails_without_raising(self):
        def command_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = self.run_checks(command_runner)
        dot = self.by_name(result)["dot"]

        self.assertFalse(result.ok)
        self.assertFalse(dot.ok)
        self.assertIn("did not finish within", dot.message)

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


if __name__ == "__main__":
    unittest.main()
