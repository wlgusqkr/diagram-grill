import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "diagram-grill"
    / "scripts"
    / "diagram_grill"
    / "renderer.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "diagram_grill_renderer_test_target",
    MODULE_PATH,
)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("Could not load renderer module from {}".format(MODULE_PATH))
RENDERER = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RENDERER
MODULE_SPEC.loader.exec_module(RENDERER)


VALID_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>\n'
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.spec_path = self.workspace / "input.json"
        self.spec = {
            "schema_version": "1.0",
            "title": "Example",
            "slug": "example",
        }
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def loader(self, path, *, mode):
        self.assertEqual("render", mode)
        with Path(path).open("r", encoding="utf-8") as spec_file:
            return json.load(spec_file)

    @staticmethod
    def preflight():
        return SimpleNamespace(
            ok=True,
            checks=(
                SimpleNamespace(
                    name="python",
                    ok=True,
                    expected=">=3.9",
                    actual="3.11.9",
                    message="ok",
                ),
                SimpleNamespace(
                    name="diagrams",
                    ok=True,
                    expected="0.25.1",
                    actual="0.25.1",
                    message="ok",
                ),
                SimpleNamespace(
                    name="jsonschema",
                    ok=True,
                    expected="4.25.1",
                    actual="4.25.1",
                    message="ok",
                ),
                SimpleNamespace(
                    name="graphviz_python",
                    ok=True,
                    expected="importable",
                    actual="importable",
                    message="ok",
                ),
                SimpleNamespace(
                    name="dot",
                    ok=True,
                    expected="available on PATH",
                    actual="12.2.1",
                    message="ok",
                ),
            ),
        )

    @staticmethod
    def compiler(source="print('render')\n", warnings=()):
        def compile_spec(spec, *, formats):
            return SimpleNamespace(source=source, warnings=tuple(warnings))

        return compile_spec

    @staticmethod
    def runner(
        *,
        svg=VALID_SVG,
        png=None,
        returncode=0,
        stderr="",
        capture=None,
    ):
        def run(command, **kwargs):
            if capture is not None:
                capture["command"] = command
                capture["kwargs"] = kwargs
            stage = Path(kwargs["cwd"])
            if svg is not None:
                (stage / "diagram.svg").write_bytes(svg)
            if png is not None:
                (stage / "diagram.png").write_bytes(png)
            return subprocess.CompletedProcess(
                command,
                returncode,
                stdout="",
                stderr=stderr,
            )

        return run

    def build(self, **overrides):
        arguments = {
            "spec_path": self.spec_path,
            "workspace_root": self.workspace,
            "load_and_validate_func": self.loader,
            "preflight_func": self.preflight,
            "compile_func": self.compiler(),
            "command_runner": self.runner(),
            "base_environment": {"PATH": "/usr/bin"},
        }
        arguments.update(overrides)
        return RENDERER.build_bundle(**arguments)

    def test_successful_initial_bundle_is_complete_and_hashed(self):
        target = self.build(compile_func=self.compiler(warnings=("fallback icon",)))

        self.assertEqual(self.workspace.resolve() / "diagrams" / "example", target)
        self.assertEqual(
            {
                "diagram.json",
                "diagram.py",
                "diagram.svg",
                "render-manifest.json",
            },
            {path.name for path in target.iterdir()},
        )

        manifest_bytes = (target / "render-manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertNotIn("timestamp", manifest)
        self.assertEqual("0.25.1", manifest["runtime"]["diagrams"]["actual"])
        self.assertEqual("12.2.1", manifest["runtime"]["dot"]["actual"])
        self.assertEqual(["fallback icon"], manifest["warnings"])
        self.assertEqual(
            self.sha256(target / "diagram.json"),
            manifest["spec"]["sha256"],
        )
        self.assertEqual(
            self.sha256(target / "diagram.py"),
            manifest["source"]["sha256"],
        )
        self.assertEqual(
            self.sha256(target / "diagram.svg"),
            manifest["artifacts"]["diagram.svg"]["sha256"],
        )
        self.assertEqual(manifest_bytes, RENDERER._canonical_json(manifest))
        self.assertEqual([], self.hidden_work_directories())

    def test_collision_refuses_to_change_existing_bundle(self):
        target = self.workspace / "diagrams" / "example"
        target.mkdir(parents=True)
        marker = target / "keep.txt"
        marker.write_text("original", encoding="utf-8")

        with self.assertRaises(RENDERER.BundleCollisionError):
            self.build()

        self.assertEqual("original", marker.read_text(encoding="utf-8"))
        self.assertEqual([], self.hidden_work_directories())

    def test_preflight_and_compiler_failures_create_no_stage_or_target(self):
        failed_preflight = lambda: SimpleNamespace(
            ok=False,
            checks=(
                SimpleNamespace(
                    name="dot",
                    ok=False,
                    message="dot is missing",
                ),
            ),
        )

        with self.subTest("preflight"):
            with self.assertRaisesRegex(RENDERER.BundleBuildError, "dot is missing"):
                self.build(preflight_func=failed_preflight)
            self.assertFalse((self.workspace / "diagrams").exists())

        def failing_compiler(spec, *, formats):
            raise RuntimeError("compiler failed")

        with self.subTest("compiler"):
            with self.assertRaisesRegex(RuntimeError, "compiler failed"):
                self.build(compile_func=failing_compiler)
            self.assertFalse((self.workspace / "diagrams").exists())

    def test_timeout_must_be_a_finite_positive_number(self):
        for invalid_timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=invalid_timeout):
                with self.assertRaisesRegex(ValueError, "greater than zero"):
                    self.build(timeout_seconds=invalid_timeout)
                self.assertFalse((self.workspace / "diagrams").exists())

    def test_successful_update_replaces_the_complete_bundle(self):
        target = self.build(compile_func=self.compiler("print('first')\n"))
        first_manifest = (target / "render-manifest.json").read_bytes()

        updated = self.build(
            update=True,
            compile_func=self.compiler("print('second')\n"),
            command_runner=self.runner(svg=VALID_SVG.replace(b"ok", b"updated")),
        )

        self.assertEqual(target, updated)
        self.assertEqual("print('second')\n", (updated / "diagram.py").read_text())
        self.assertNotEqual(first_manifest, (updated / "render-manifest.json").read_bytes())
        self.assertEqual([], self.hidden_work_directories())

    def test_timeout_and_command_failure_leave_no_bundle(self):
        def timeout_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.subTest("timeout"):
            with self.assertRaisesRegex(RENDERER.BundleBuildError, "timeout"):
                self.build(command_runner=timeout_runner, timeout_seconds=0.1)
            self.assertFalse((self.workspace / "diagrams" / "example").exists())
            self.assertEqual([], self.hidden_work_directories())

        with self.subTest("failure"):
            with self.assertRaisesRegex(RENDERER.BundleBuildError, "exit code 7"):
                self.build(
                    command_runner=self.runner(
                        svg=None,
                        returncode=7,
                        stderr="render failed",
                    )
                )
            self.assertFalse((self.workspace / "diagrams" / "example").exists())
            self.assertEqual([], self.hidden_work_directories())

    def test_malformed_artifacts_are_rejected_and_update_preserves_old_bundle(self):
        target = self.build()
        original_manifest = (target / "render-manifest.json").read_bytes()

        with self.subTest("invalid svg"):
            with self.assertRaises(RENDERER.ArtifactValidationError):
                self.build(
                    update=True,
                    command_runner=self.runner(svg=b"not xml"),
                )
            self.assertEqual(original_manifest, (target / "render-manifest.json").read_bytes())
            self.assertEqual([], self.hidden_work_directories())

        with self.subTest("invalid png"):
            with self.assertRaisesRegex(
                RENDERER.ArtifactValidationError,
                "invalid PNG signature",
            ):
                self.build(
                    update=True,
                    formats=("svg", "png"),
                    command_runner=self.runner(svg=VALID_SVG, png=b"bad png"),
                )
            self.assertEqual(original_manifest, (target / "render-manifest.json").read_bytes())
            self.assertEqual([], self.hidden_work_directories())

    def test_png_is_optional_but_svg_is_always_built(self):
        target = self.build(
            formats=("png",),
            command_runner=self.runner(svg=VALID_SVG, png=VALID_PNG),
        )

        self.assertTrue((target / "diagram.svg").is_file())
        self.assertTrue((target / "diagram.png").is_file())

    def test_subprocess_environment_is_sanitized(self):
        capture = {}
        target = self.build(
            command_runner=self.runner(capture=capture),
            base_environment={
                "PATH": "/safe/bin",
                "HOME": "/safe/home",
                "PYTHONPATH": "/untrusted/modules",
                "SECRET_TOKEN": "do-not-pass",
            },
        )

        self.assertTrue(target.is_dir())
        self.assertEqual([sys.executable, "diagram.py"], capture["command"])
        kwargs = capture["kwargs"]
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["capture_output"])
        render_cwd = Path(kwargs["cwd"])
        self.assertEqual(target.parent, render_cwd.parent)
        self.assertTrue(render_cwd.name.startswith(".example-stage-"))
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("SECRET_TOKEN", kwargs["env"])
        self.assertEqual("/safe/bin", kwargs["env"]["PATH"])
        self.assertEqual("1", kwargs["env"]["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual("1", kwargs["env"]["PYTHONUTF8"])
        self.assertEqual("diagram.py", capture["command"][1])

    @staticmethod
    def sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def hidden_work_directories(self):
        diagrams_root = self.workspace / "diagrams"
        if not diagrams_root.exists():
            return []
        return sorted(
            path.name
            for path in diagrams_root.iterdir()
            if path.name.startswith(".example-")
        )


if __name__ == "__main__":
    unittest.main()
