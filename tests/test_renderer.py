import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS_ROOT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "diagram-grill"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_ROOT))
try:
    from diagram_grill import renderer as RENDERER
finally:
    sys.path.remove(str(SCRIPTS_ROOT))


VALID_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>\n'
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tools_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.dot_path = Path(self.tools_directory.name) / "dot"
        self.dot_path.write_text("test executable", encoding="utf-8")
        self.dot_path.chmod(0o755)
        self.dot_path = self.dot_path.resolve()
        self.spec_path = self.workspace / "input.json"
        self.spec = {
            "schema_version": "1.0",
            "title": "Example",
            "slug": "example",
        }
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")

    def tearDown(self):
        self.tools_directory.cleanup()
        self.temporary_directory.cleanup()

    def loader(self, path, *, mode):
        self.assertEqual("render", mode)
        with Path(path).open("r", encoding="utf-8") as spec_file:
            return json.load(spec_file)

    def preflight(self):
        dot_identity, dot_sha256 = RENDERER._capture_executable(self.dot_path)
        return SimpleNamespace(
            ok=True,
            dot_path=str(self.dot_path),
            dot_identity=dot_identity,
            dot_sha256=dot_sha256,
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
                    expected="0.20.3",
                    actual="0.20.3",
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
            "validate_func": lambda spec, mode: None,
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
        self.assertNotIn(str(self.dot_path).encode("utf-8"), manifest_bytes)
        self.assertEqual("0.25.1", manifest["runtime"]["diagrams"]["actual"])
        self.assertEqual("12.2.1", manifest["runtime"]["dot"]["actual"])
        self.assertNotIn("executable", manifest["runtime"]["dot"])
        self.assertEqual(
            hashlib.sha256(self.dot_path.read_bytes()).hexdigest(),
            manifest["runtime"]["dot"]["sha256"],
        )
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

    def test_in_memory_spec_builds_without_loading_an_input_file(self):
        target = self.build(
            spec_path=self.spec,
            load_and_validate_func=mock.Mock(
                side_effect=AssertionError("path loader must not be called")
            ),
        )

        self.assertEqual(self.spec, json.loads((target / "diagram.json").read_text()))
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

    def test_svg_icons_are_bounded_embedded_and_reproducible(self):
        resources_root = self.workspace / "trusted-resources"
        resources_root.mkdir()
        icon_path = resources_root / "icon.png"
        icon_path.write_bytes(VALID_PNG)
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<image xlink:href="{}" /></svg>\n'.format(icon_path)
        ).encode("utf-8")

        first = RENDERER._sanitize_svg(source, resources_root=resources_root)
        second = RENDERER._sanitize_svg(source, resources_root=resources_root)

        self.assertEqual(first, second)
        self.assertNotIn(str(icon_path).encode("utf-8"), first)
        self.assertIn(b"data:image/png;base64,", first)
        self.assertIn(base64.b64encode(VALID_PNG), first)

    def test_svg_sanitizer_strips_only_one_standard_graphviz_doctype(self):
        standard_doctype = (
            '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"\n'
            ' "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
        )
        source = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + standard_doctype
            + '\n<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>'
        ).encode("utf-8")

        sanitized = RENDERER._sanitize_svg(source)

        self.assertNotIn(b"<!DOCTYPE", sanitized)
        self.assertNotIn(b"<!ENTITY", sanitized)

        invalid_sources = {
            "duplicate standard declarations": (
                standard_doctype
                + standard_doctype
                + '<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>'
            ),
            "standard declaration with internal subset": (
                '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
                '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" '
                '[<!ENTITY leak "unsafe">]>'
                '<svg xmlns="http://www.w3.org/2000/svg"><text>&leak;</text></svg>'
            ),
        }
        for name, invalid_source in invalid_sources.items():
            with self.subTest(name=name):
                with self.assertRaises(RENDERER.ArtifactValidationError):
                    RENDERER._sanitize_svg(invalid_source.encode("utf-8"))

    def test_svg_sanitizer_rejects_active_content_and_untrusted_links(self):
        resources_root = self.workspace / "trusted-resources"
        resources_root.mkdir()
        outside_icon = self.workspace / "outside.png"
        outside_icon.write_bytes(VALID_PNG)
        oversized_icon = VALID_PNG + b"x" * RENDERER.MAX_EMBEDDED_ICON_BYTES
        oversized_data_uri = (
            "data:image/png;base64," + base64.b64encode(oversized_icon).decode("ascii")
        )

        invalid_sources = {
            "doctype": (
                b'<!DOCTYPE svg SYSTEM "https://attacker.invalid/diagram.dtd">'
                b'<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>'
            ),
            "entity": (
                b'<!DOCTYPE svg [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"><text>&leak;</text></svg>'
            ),
            "script": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<script>alert(1)</script></svg>'
            ),
            "foreignObject": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<foreignObject><p>unsafe</p></foreignObject></svg>'
            ),
            "event handler": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<g onclick="alert(1)" /></svg>'
            ),
            "style import": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<style>@import url(https://attacker.invalid/leak);</style></svg>'
            ),
            "smil set href": (
                b'<svg xmlns="http://www.w3.org/2000/svg"><a>'
                b'<set attributeName="href" to="javascript:alert(1)" begin="0s" />'
                b'<text>click</text></a></svg>'
            ),
            "external presentation url": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<rect width="1" height="1" '
                b'fill="url(https://attacker.invalid/leak)" /></svg>'
            ),
            "allowed element external url": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<path d="M0,0" fill="url(https://attacker.invalid/leak)" />'
                b'</svg>'
            ),
            "allowed element dangerous scheme": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<path d="M0,0" fill="javascript:alert(1)" /></svg>'
            ),
            "allowed element network reference": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<path d="M0,0" fill="//attacker.invalid/leak" /></svg>'
            ),
            "non-data link": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<a href="javascript:alert(1)">click</a></svg>'
            ),
            "non-data xlink": (
                b'<svg xmlns="http://www.w3.org/2000/svg" '
                b'xmlns:xlink="http://www.w3.org/1999/xlink">'
                b'<a xlink:href="https://example.com/">click</a></svg>'
            ),
            "outside icon": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<image href="{}" /></svg>'.format(outside_icon).encode("utf-8")
            ),
            "non-png data": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<image href="data:text/html;base64,PHNjcmlwdD4=" /></svg>'
            ),
            "oversized data": (
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<image href="{}" /></svg>'.format(oversized_data_uri).encode("ascii")
            ),
        }

        for name, source in invalid_sources.items():
            with self.subTest(name=name):
                with self.assertRaises(RENDERER.ArtifactValidationError):
                    RENDERER._sanitize_svg(source, resources_root=resources_root)

        for element_name in (
            "animate",
            "animateMotion",
            "animateTransform",
            "discard",
            "set",
        ):
            with self.subTest(smil=element_name):
                source = (
                    '<svg xmlns="http://www.w3.org/2000/svg"><{} /></svg>'.format(
                        element_name
                    )
                ).encode("ascii")
                with self.assertRaises(RENDERER.ArtifactValidationError):
                    RENDERER._sanitize_svg(source, resources_root=resources_root)

    def test_svg_sanitizer_revalidates_the_serialized_tree(self):
        active_output = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<style>@import url(https://attacker.invalid/leak);</style></svg>'
        )

        with mock.patch.object(
            RENDERER.ElementTree,
            "tostring",
            return_value=active_output,
        ):
            with self.assertRaises(RENDERER.ArtifactValidationError):
                RENDERER._sanitize_svg(VALID_SVG)

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
        self.assertEqual([sys.executable, "-I", "diagram.py"], capture["command"])
        kwargs = capture["kwargs"]
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["capture_output"])
        render_cwd = Path(kwargs["cwd"])
        self.assertEqual(target.parent, render_cwd.parent)
        self.assertTrue(render_cwd.name.startswith(".example-stage-"))
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("SECRET_TOKEN", kwargs["env"])
        self.assertEqual(str(self.dot_path.parent), kwargs["env"]["PATH"])
        self.assertEqual("1", kwargs["env"]["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual("0", kwargs["env"]["PYTHONHASHSEED"])
        self.assertEqual("1", kwargs["env"]["PYTHONNOUSERSITE"])
        self.assertEqual("1", kwargs["env"]["PYTHONUTF8"])
        self.assertEqual("diagram.py", capture["command"][2])

    def test_in_place_dot_mutation_before_spawn_fails_closed(self):
        runner = mock.Mock()

        def mutating_compiler(spec, *, formats):
            self.dot_path.write_text("mutated in place", encoding="utf-8")
            self.dot_path.chmod(0o755)
            return SimpleNamespace(source="print('render')\n", warnings=())

        with self.assertRaisesRegex(RENDERER.BundleBuildError, "changed after preflight"):
            self.build(compile_func=mutating_compiler, command_runner=runner)

        runner.assert_not_called()
        self.assertFalse((self.workspace / "diagrams" / "example").exists())

    def test_dot_trust_is_rechecked_after_snapshot_before_spawn(self):
        identity, sha256 = RENDERER._capture_executable(self.dot_path)
        verified_dot = RENDERER._VerifiedDot(self.dot_path, identity, sha256)
        runner = mock.Mock()

        with mock.patch.object(
            RENDERER,
            "_posix_dot_trust_failure",
            side_effect=(None, "Graphviz path trust changed before spawn"),
        ) as trust_check:
            with self.assertRaisesRegex(
                RENDERER.BundleBuildError,
                "trust changed before spawn",
            ):
                RENDERER._run_generated_source(
                    runner,
                    self.workspace,
                    1.0,
                    {},
                    verified_dot,
                )

        self.assertEqual(2, trust_check.call_count)
        runner.assert_not_called()

    def test_workspace_cannot_overlap_the_installed_skill(self):
        with mock.patch.object(
            RENDERER,
            "_SKILL_ROOT",
            self.workspace.resolve().parent,
        ):
            with self.assertRaisesRegex(
                RENDERER.BundleBuildError,
                "Installed skill path overlaps the target workspace",
            ):
                self.build()

    def test_dot_symlink_swap_before_spawn_fails_closed(self):
        runner = mock.Mock()
        original = self.dot_path.parent / "original-dot"
        replacement_root = self.dot_path.parent / "replacement"
        replacement_root.mkdir()
        replacement = replacement_root / "dot"
        replacement.write_text("replacement", encoding="utf-8")
        replacement.chmod(0o755)

        def swapping_compiler(spec, *, formats):
            self.dot_path.rename(original)
            self.dot_path.symlink_to(replacement)
            return SimpleNamespace(source="print('render')\n", warnings=())

        with self.assertRaisesRegex(RENDERER.BundleBuildError, "immediately before"):
            self.build(compile_func=swapping_compiler, command_runner=runner)

        runner.assert_not_called()
        self.assertTrue(self.dot_path.is_symlink())

    def test_target_appearing_during_render_blocks_promotion_and_is_preserved(self):
        target = self.workspace / "diagrams" / "example"

        def racing_runner(command, **kwargs):
            stage = Path(kwargs["cwd"])
            (stage / "diagram.svg").write_bytes(VALID_SVG)
            target.mkdir()
            (target / "racer.txt").write_text("keep", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with self.assertRaisesRegex(RENDERER.BundleBuildError, "target appeared"):
            self.build(command_runner=racing_runner)

        self.assertEqual("keep", (target / "racer.txt").read_text(encoding="utf-8"))
        self.assertEqual([], self.hidden_work_directories())

    def test_target_inode_change_during_update_blocks_promotion(self):
        target = self.build()
        preserved = target.parent / "preserved-original"

        def racing_runner(command, **kwargs):
            stage = Path(kwargs["cwd"])
            (stage / "diagram.svg").write_bytes(VALID_SVG)
            target.rename(preserved)
            target.mkdir()
            (target / "racer.txt").write_text("replacement", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with self.assertRaisesRegex(RENDERER.BundleBuildError, "changed identity"):
            self.build(update=True, command_runner=racing_runner)

        self.assertEqual(
            "replacement",
            (target / "racer.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue((preserved / "render-manifest.json").is_file())
        self.assertEqual([], self.hidden_work_directories())

    def test_target_disappearance_and_symlink_change_are_detected(self):
        target = self.workspace / "diagrams" / "example"
        target.mkdir(parents=True)
        identity = RENDERER._path_identity(target)

        preserved = target.parent / "preserved"
        target.rename(preserved)
        with self.assertRaisesRegex(RENDERER.BundleBuildError, "disappeared"):
            RENDERER._assert_same_identity(target, identity, "bundle target")

        external = self.workspace / "external"
        external.mkdir()
        target.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RENDERER.BundleBuildError, "became a symlink"):
            RENDERER._assert_same_identity(target, identity, "bundle target")

        self.assertTrue(target.is_symlink())
        self.assertTrue(preserved.is_dir())

    def test_cleanup_skips_replaced_parent_and_stage_paths(self):
        diagrams_root = self.workspace / "cleanup-diagrams"
        stage = diagrams_root / ".stage"
        stage.mkdir(parents=True)
        (stage / "owned.txt").write_text("owned", encoding="utf-8")
        diagrams_identity = RENDERER._path_identity(diagrams_root)
        stage_identity = RENDERER._path_identity(stage)

        moved_root = self.workspace / "moved-cleanup-diagrams"
        diagrams_root.rename(moved_root)
        replacement_stage = diagrams_root / ".stage"
        replacement_stage.mkdir(parents=True)
        marker = replacement_stage / "external.txt"
        marker.write_text("preserve", encoding="utf-8")

        cleaned = RENDERER._safe_cleanup_tree(
            stage,
            expected_identity=stage_identity,
            parent=diagrams_root,
            expected_parent_identity=diagrams_identity,
        )

        self.assertFalse(cleaned)
        self.assertEqual("preserve", marker.read_text(encoding="utf-8"))
        self.assertTrue((moved_root / ".stage" / "owned.txt").is_file())

    def test_changed_update_backup_is_verified_and_retained(self):
        diagrams_root = self.workspace / "promotion"
        stage = diagrams_root / ".stage"
        target = diagrams_root / "example"
        stage.mkdir(parents=True)
        target.mkdir()
        (stage / "new.txt").write_text("new", encoding="utf-8")
        (target / "old.txt").write_text("old", encoding="utf-8")
        diagrams_identity = RENDERER._path_identity(diagrams_root)
        stage_identity = RENDERER._path_identity(stage)
        target_identity = RENDERER._path_identity(target)
        preserved_old = diagrams_root / "preserved-old"
        real_replace = os.replace

        def racing_replace(source, destination):
            real_replace(source, destination)
            if Path(source) == stage and Path(destination) == target:
                backup = next(diagrams_root.glob(".example-backup-*"))
                backup.rename(preserved_old)
                backup.mkdir()
                (backup / "racer.txt").write_text("preserve", encoding="utf-8")

        with mock.patch.object(RENDERER.os, "replace", side_effect=racing_replace):
            with self.assertRaisesRegex(RENDERER.BundleBuildError, "retained for safety"):
                RENDERER._promote_stage(
                    stage,
                    target,
                    replace_existing=True,
                    diagrams_root=diagrams_root,
                    diagrams_identity=diagrams_identity,
                    stage_identity=stage_identity,
                    target_identity=target_identity,
                )

        changed_backup = next(diagrams_root.glob(".example-backup-*"))
        self.assertEqual(
            "preserve", (changed_backup / "racer.txt").read_text(encoding="utf-8")
        )
        self.assertEqual("old", (preserved_old / "old.txt").read_text(encoding="utf-8"))
        self.assertEqual("new", (target / "new.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_timeout_terminates_the_renderer_process_group(self):
        process = mock.Mock()
        process.pid = 43210
        process.communicate.side_effect = [
            subprocess.TimeoutExpired([sys.executable, "-I", "diagram.py"], 0.1),
            ("partial output", ""),
        ]
        process.wait.side_effect = subprocess.TimeoutExpired("wait", 0.25)
        process.poll.return_value = None

        with mock.patch.object(RENDERER.subprocess, "Popen", return_value=process) as popen:
            with mock.patch.object(RENDERER.os, "killpg") as killpg:
                with self.assertRaises(subprocess.TimeoutExpired):
                    RENDERER._run_process_group(
                        [sys.executable, "-I", "diagram.py"],
                        cwd=self.workspace,
                        env={"PATH": str(self.dot_path.parent)},
                        timeout=0.1,
                    )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
            killpg.call_args_list,
        )
        process.kill.assert_called_once_with()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_timeout_closes_pipes_when_post_kill_drain_stays_blocked(self):
        process = mock.Mock()
        process.pid = 43211
        process.communicate.side_effect = [
            subprocess.TimeoutExpired([sys.executable, "-I", "diagram.py"], 0.1),
            subprocess.TimeoutExpired(
                [sys.executable, "-I", "diagram.py"],
                RENDERER._TERMINATION_GRACE_SECONDS,
                output="partial",
                stderr="held",
            ),
        ]
        process.wait.side_effect = subprocess.TimeoutExpired("wait", 0.25)
        process.poll.return_value = None

        with mock.patch.object(RENDERER.subprocess, "Popen", return_value=process):
            with mock.patch.object(RENDERER.os, "killpg"):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    RENDERER._run_process_group(
                        [sys.executable, "-I", "diagram.py"],
                        cwd=self.workspace,
                        env={"PATH": str(self.dot_path.parent)},
                        timeout=0.1,
                    )

        self.assertEqual("partial", caught.exception.output)
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()
        process.stdin.close.assert_called_once_with()

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
