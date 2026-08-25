import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import diagram_grill_cli as cli  # noqa: E402
from diagram_grill.catalog import CatalogEntry, NodeCatalogError  # noqa: E402
from diagram_grill.errors import DiagramValidationError, ValidationIssue  # noqa: E402
from diagram_grill.renderer import BundleBuildError  # noqa: E402


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "specs" / "valid.json"
CLI_PATH = (SKILL_ROOT / "scripts" / "diagram_grill_cli.py").resolve()


def fixture_spec():
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def invoke(arguments):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = cli.main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


class PreflightCommandTests(unittest.TestCase):
    def test_preflight_reports_missing_dependencies_without_import_traceback(self):
        command = [
            sys.executable,
            "-I",
            "-S",
            str(CLI_PATH),
            "preflight",
            "--json",
        ]

        completed = subprocess.run(command, capture_output=True, text=True, check=False)

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            ["python", "diagrams", "jsonschema", "graphviz_python", "dot"],
            [check["name"] for check in payload["checks"]],
        )

    def test_preflight_json_reports_structured_checks(self):
        result = SimpleNamespace(
            ok=True,
            checks=(
                SimpleNamespace(
                    name="python",
                    ok=True,
                    message="Python is supported.",
                    expected=">=3.9",
                    actual="3.11.9",
                ),
            ),
        )
        with mock.patch.object(cli, "run_preflight", return_value=result):
            status, stdout, stderr = invoke(["preflight", "--json"])

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("python", payload["checks"][0]["name"])
        self.assertEqual(">=3.9", payload["checks"][0]["expected"])

    def test_failed_human_preflight_uses_stderr_and_status_one(self):
        result = SimpleNamespace(
            ok=False,
            checks=(
                SimpleNamespace(
                    name="dot",
                    ok=False,
                    message="Graphviz dot is missing.",
                    expected="available",
                    actual=None,
                ),
            ),
        )
        with mock.patch.object(cli, "run_preflight", return_value=result):
            status, stdout, stderr = invoke(["preflight"])

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("Preflight failed", stderr)
        self.assertIn("Graphviz dot is missing", stderr)


class IsolatedEntrypointTests(unittest.TestCase):
    def test_supported_invocation_does_not_write_bytecode_into_skill_package(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_skill = Path(directory) / "diagram-grill"
            shutil.copytree(
                SKILL_ROOT,
                copied_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            copied_cli = (copied_skill / "scripts" / "diagram_grill_cli.py").resolve()

            completed = subprocess.run(
                [sys.executable, "-I", str(copied_cli), "--help"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual([], list(copied_skill.rglob("__pycache__")))
            self.assertEqual([], list(copied_skill.rglob("*.pyc")))

    def test_supported_invocation_ignores_pythonpath_json_and_sitecustomize(self):
        with tempfile.TemporaryDirectory() as directory:
            payload_root = Path(directory)
            marker = payload_root / "executed.txt"
            payload = (
                "with open({!r}, 'a', encoding='utf-8') as marker:\n"
                "    marker.write({!r})\n"
            )
            (payload_root / "json.py").write_text(
                payload.format(str(marker), "json\n"), encoding="utf-8"
            )
            (payload_root / "sitecustomize.py").write_text(
                payload.format(str(marker), "sitecustomize\n"), encoding="utf-8"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(payload_root)

            completed = subprocess.run(
                [sys.executable, "-I", str(CLI_PATH), "--help"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Validate and render approved", completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertFalse(marker.exists())

    def test_target_repository_cannot_contain_the_installed_skill(self):
        status, stdout, stderr = invoke(
            ["evidence", "--repo", str(REPOSITORY_ROOT), "README.md"]
        )

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("overlaps the target evidence", stderr)
        self.assertIn("globally with --copy", stderr)

    def test_installed_skill_cannot_contain_the_target_workspace(self):
        target = SKILL_ROOT / "nested-workspace"
        with mock.patch.object(cli, "_SKILL_ROOT", SKILL_ROOT.resolve()):
            status, stdout, stderr = invoke(
                ["build", "-", "--workspace", str(target)]
            )

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("overlaps the target build", stderr)

    def test_repository_local_python_interpreter_is_rejected(self):
        with mock.patch.object(
            cli,
            "_SKILL_ROOT",
            Path("/trusted-global-agent-skills/diagram-grill"),
        ):
            with mock.patch.object(
                cli,
                "_PYTHON_EXECUTABLE",
                REPOSITORY_ROOT / ".venv" / "bin" / "python",
            ):
                status, stdout, stderr = invoke(
                    ["evidence", "--repo", str(REPOSITORY_ROOT), "README.md"]
                )

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("Python interpreter that overlaps", stderr)
        self.assertIn("outside the target repository", stderr)

    @unittest.skipUnless(sys.platform == "darwin", "macOS path alias integration only")
    def test_case_variant_alias_cannot_bypass_the_install_boundary(self):
        repository_alias = Path(
            str(REPOSITORY_ROOT).replace("/Users/", "/users/", 1)
        )
        if not repository_alias.exists() or not REPOSITORY_ROOT.samefile(
            repository_alias
        ):
            self.skipTest("workspace filesystem is case-sensitive")

        status, stdout, stderr = invoke(
            ["evidence", "--repo", str(repository_alias), "README.md"]
        )

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("overlaps the target evidence", stderr)

    def test_nonisolated_execution_fails_before_application_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            payload_root = Path(directory)
            marker = payload_root / "json-imported.txt"
            (payload_root / "json.py").write_text(
                "open({!r}, 'w', encoding='utf-8').write('executed')\n".format(
                    str(marker)
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(payload_root)

            completed = subprocess.run(
                [sys.executable, str(CLI_PATH), "--help"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertIn("Python isolated mode is required", completed.stderr)
            self.assertIn(
                "/absolute/path/to/trusted/python -I /absolute/path/",
                completed.stderr,
            )
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(marker.exists())


class CatalogCommandTests(unittest.TestCase):
    def test_catalog_propagates_filters_and_emits_json(self):
        entry = CatalogEntry(
            import_path="diagrams.aws.compute.EC2",
            provider="aws",
            category="compute",
            class_name="EC2",
            search_text="aws compute ec2",
        )
        with mock.patch.object(
            cli, "search_node_catalog", return_value=(entry,)
        ) as search:
            status, stdout, stderr = invoke(
                [
                    "catalog",
                    "ec2",
                    "--provider",
                    "aws",
                    "--category",
                    "compute",
                    "--limit",
                    "3",
                    "--json",
                ]
            )

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        search.assert_called_once_with(
            "ec2", provider="aws", category="compute", limit=3
        )
        self.assertEqual(
            "diagrams.aws.compute.EC2",
            json.loads(stdout)["results"][0]["import_path"],
        )

    def test_catalog_operational_error_is_machine_readable(self):
        with mock.patch.object(
            cli,
            "search_node_catalog",
            side_effect=NodeCatalogError("catalog unavailable"),
        ):
            status, stdout, stderr = invoke(["catalog", "ec2", "--json"])

        self.assertEqual(1, status)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("NodeCatalogError", payload["error"]["type"])


class ValidateAndBriefCommandTests(unittest.TestCase):
    def test_validate_passes_mode_and_emits_json(self):
        with mock.patch.object(cli, "load_and_validate", return_value=fixture_spec()) as load:
            status, stdout, stderr = invoke(
                ["validate", "architecture.json", "--mode", "render", "--json"]
            )

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        load.assert_called_once_with(Path("architecture.json"), mode="render")
        self.assertEqual("render", json.loads(stdout)["mode"])

    def test_validation_error_includes_structured_issues(self):
        error = DiagramValidationError(
            [
                ValidationIssue(
                    code="approval-required",
                    path="$.approval.status",
                    message="Approval is required.",
                )
            ]
        )
        with mock.patch.object(cli, "load_and_validate", side_effect=error):
            status, stdout, stderr = invoke(
                ["validate", "architecture.json", "--mode", "render", "--json"]
            )

        self.assertEqual(1, status)
        self.assertEqual("", stderr)
        issue = json.loads(stdout)["error"]["issues"][0]
        self.assertEqual("approval-required", issue["code"])
        self.assertEqual("$.approval.status", issue["path"])

    def test_brief_is_nested_evidence_linked_and_explicit_about_unknowns(self):
        spec = copy.deepcopy(fixture_spec())
        spec["nodes"][0]["status"] = "unknown"
        spec["assumptions"][0]["status"] = "unresolved"
        with mock.patch.object(cli, "load_and_validate", return_value=spec) as load:
            with mock.patch.object(cli, "build_bundle") as build:
                status, stdout, stderr = invoke(["brief", "architecture.json"])

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        load.assert_called_once_with(Path("architecture.json"), mode="draft")
        build.assert_not_called()
        self.assertIn("# Architecture Brief: Checkout architecture", stdout)
        self.assertIn("  - `production`", stdout)
        self.assertIn("    - `services`", stdout)
        self.assertIn("status: `unknown`", stdout)
        self.assertIn("`infra/service.tf:12-18`", stdout)
        self.assertIn("`api` → `database` — `data`", stdout)
        self.assertIn("## Explicit unknowns", stdout)
        self.assertIn("Node `api`", stdout)
        self.assertIn("Assumption `single-region`", stdout)
        self.assertIn("## Render plan", stdout)
        self.assertIn("no theme selection", stdout)
        self.assertIn("no files will be written", stdout)


class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self.skill_root_patch = mock.patch.object(
            cli,
            "_SKILL_ROOT",
            Path("/trusted-global-agent-skills/diagram-grill"),
        )
        self.skill_root_patch.start()
        self.addCleanup(self.skill_root_patch.stop)

    def test_build_reads_approved_json_from_stdin_without_an_input_path(self):
        bundle = Path("/workspace/diagrams/checkout-architecture")
        stdin = io.StringIO(json.dumps(fixture_spec()))
        with mock.patch.object(cli, "build_bundle", return_value=bundle) as build:
            with mock.patch.object(sys, "stdin", stdin):
                status, stdout, stderr = invoke(
                    ["build", "-", "--workspace", "/workspace", "--png"]
                )

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        self.assertIn("Built diagram bundle", stdout)
        build.assert_called_once_with(
            fixture_spec(),
            Path("/workspace"),
            formats=("svg", "png"),
            update=False,
            timeout_seconds=30.0,
        )

    def test_build_rejects_invalid_stdin_json_without_calling_renderer(self):
        with mock.patch.object(cli, "build_bundle") as build:
            with mock.patch.object(sys, "stdin", io.StringIO("not-json")):
                status, stdout, stderr = invoke(["build", "-"])

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("Expecting value", stderr)
        build.assert_not_called()

    def test_build_rejects_oversized_stdin_without_calling_renderer(self):
        oversized = " " * (cli.MAX_STDIN_SPEC_BYTES + 1)
        with mock.patch.object(cli, "build_bundle") as build:
            with mock.patch.object(sys, "stdin", io.StringIO(oversized)):
                status, stdout, stderr = invoke(["build", "-"])

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("exceeds the", stderr)
        build.assert_not_called()

    def test_build_propagates_png_update_workspace_and_timeout(self):
        bundle = Path("/workspace/diagrams/checkout-architecture")
        with mock.patch.object(cli, "build_bundle", return_value=bundle) as build:
            status, stdout, stderr = invoke(
                [
                    "build",
                    "architecture.json",
                    "--workspace",
                    "/workspace",
                    "--png",
                    "--update",
                    "--timeout",
                    "4.5",
                    "--json",
                ]
            )

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        build.assert_called_once_with(
            Path("architecture.json"),
            Path("/workspace"),
            formats=("svg", "png"),
            update=True,
            timeout_seconds=4.5,
        )
        payload = json.loads(stdout)
        self.assertEqual(["svg", "png"], payload["formats"])
        self.assertTrue(payload["updated"])

    def test_svg_is_mandatory_and_update_is_opt_in(self):
        with mock.patch.object(
            cli,
            "build_bundle",
            return_value=Path("diagrams/checkout-architecture"),
        ) as build:
            status, stdout, stderr = invoke(["build", "architecture.json"])

        self.assertEqual(0, status)
        self.assertEqual("", stderr)
        self.assertIn("Built diagram bundle", stdout)
        build.assert_called_once_with(
            Path("architecture.json"),
            Path("."),
            formats=("svg",),
            update=False,
            timeout_seconds=30.0,
        )

    def test_build_failure_is_human_readable_on_stderr(self):
        with mock.patch.object(
            cli,
            "build_bundle",
            side_effect=BundleBuildError("target exists; use --update"),
        ):
            status, stdout, stderr = invoke(["build", "architecture.json"])

        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("target exists; use --update", stderr)

    def test_argparse_keeps_status_two_for_invalid_timeout(self):
        for timeout in ("0", "nan", "inf"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(SystemExit) as raised:
                    invoke(["build", "architecture.json", "--timeout", timeout])
                self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
