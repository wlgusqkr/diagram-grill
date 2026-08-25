import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SOURCE = REPOSITORY_ROOT / "evals" / "evaluator_bootstrap.py"


def _make_venv(path):
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-m",
            "venv",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    executable = path / "bin" / "python"
    if not executable.is_file():
        executable = path / "Scripts" / "python.exe"
    return executable


class EvaluatorBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.repository = cls.root / "source"
        cls.evals_root = cls.repository / "evals"
        cls.evals_root.mkdir(parents=True)
        shutil.copy2(BOOTSTRAP_SOURCE, cls.evals_root / "evaluator_bootstrap.py")
        (cls.evals_root / "codex_runner.py").write_text(
            "import json\n"
            "def main(argv=()):\n"
            "    value = {'runner': 'trusted', 'argv': list(argv)}\n"
            "    print(json.dumps(value, sort_keys=True))\n"
            "    return 0\n",
            encoding="utf-8",
        )
        cls.results = cls.root / "results"
        cls.state = cls.root / "session-state"
        cls.results.mkdir(mode=0o700)
        cls.state.mkdir(mode=0o700)
        cls.evaluator_prefix = cls.root / "dedicated-evaluator"
        cls.evaluator_python = _make_venv(cls.evaluator_prefix)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def command(self, *runner_args, python=None, prefix=None):
        return [
            str(python or self.evaluator_python),
            "-I",
            "-S",
            str(self.evals_root / "evaluator_bootstrap.py"),
            "--evaluator-prefix",
            str(prefix or self.evaluator_prefix),
            "--results-root",
            str(self.results),
            "--session-state-root",
            str(self.state),
            "--",
        ] + list(runner_args)

    def run_command(self, *runner_args, env=None, cwd=None, python=None, prefix=None):
        return subprocess.run(
            self.command(*runner_args, python=python, prefix=prefix),
            cwd=str(cwd or self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

    def test_documented_invocation_imports_exact_runner(self):
        completed = self.run_command("plan")
        self.assertEqual(0, completed.returncode, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual("trusted", value["runner"])
        self.assertEqual(["plan"], value["argv"])

    def test_pythonpath_and_sitecustomize_do_not_execute(self):
        attacker = self.root / "pythonpath-attacker"
        attacker.mkdir()
        sentinel = self.root / "pythonpath-executed"
        payload = (
            "from pathlib import Path; "
            "Path({!r}).write_text('executed')\n".format(str(sentinel))
        )
        (attacker / "sitecustomize.py").write_text(payload, encoding="utf-8")
        (attacker / "sysconfig.py").write_text(payload, encoding="utf-8")
        (attacker / "codex_runner.py").write_text(payload, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(attacker)
        env["PYTHONSTARTUP"] = str(attacker / "sitecustomize.py")

        completed = self.run_command("plan", env=env, cwd=attacker)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual("trusted", json.loads(completed.stdout)["runner"])

    def test_pth_payload_does_not_execute_when_library_root_is_inserted(self):
        site_packages = next(
            self.evaluator_prefix.glob("lib/python*/site-packages"), None
        )
        if site_packages is None:
            self.skipTest("test interpreter does not use a POSIX venv layout")
        pth_sentinel = self.root / "pth-executed"
        sitecustomize_sentinel = self.root / "sitecustomize-executed"
        (site_packages / "startup-payload.pth").write_text(
            "import pathlib; pathlib.Path({!r}).write_text('executed')\n".format(
                str(pth_sentinel)
            ),
            encoding="utf-8",
        )
        (site_packages / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            "Path({!r}).write_text('executed')\n".format(
                str(sitecustomize_sentinel)
            ),
            encoding="utf-8",
        )

        completed = self.run_command("plan")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(pth_sentinel.exists())
        self.assertFalse(sitecustomize_sentinel.exists())
        self.assertEqual("trusted", json.loads(completed.stdout)["runner"])

    def test_prepare_receives_only_bootstrap_validated_target_roots(self):
        completed = self.run_command("prepare", "--case", "test")
        self.assertEqual(0, completed.returncode, completed.stderr)
        argv = json.loads(completed.stdout)["argv"]
        self.assertEqual(
            [
                "prepare",
                "--results-root",
                str(self.results),
                "--session-state-root",
                str(self.state),
                "--case",
                "test",
            ],
            argv,
        )

    def test_runner_cannot_override_bootstrap_target_roots(self):
        for option in ("--results-root", "--results-r", "--session-state-r"):
            with self.subTest(option=option):
                completed = self.run_command(
                    "prepare", option, str(self.repository)
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn("bootstrap-owned", completed.stderr)

    def test_repository_local_interpreter_and_prefix_are_rejected(self):
        local_prefix = self.repository / "repo-local-python"
        local_python = _make_venv(local_prefix)

        local_interpreter = self.run_command(
            "plan", python=local_python, prefix=local_prefix
        )
        local_prefix_only = self.run_command("plan", prefix=local_prefix)

        self.assertEqual(2, local_interpreter.returncode)
        self.assertIn("overlaps repository root", local_interpreter.stderr)
        self.assertEqual(2, local_prefix_only.returncode)
        self.assertIn("overlaps repository root", local_prefix_only.stderr)

    def test_run_directory_must_be_absolute_and_under_results_root(self):
        outside = self.root / "outside-run"
        outside.mkdir()
        relative = self.run_command("status", "relative-run")
        escaped = self.run_command("status", str(outside))
        self.assertEqual(2, relative.returncode)
        self.assertIn("must be absolute", relative.stderr)
        self.assertEqual(2, escaped.returncode)
        self.assertIn("outside the fixed results root", escaped.stderr)

    def test_samefile_aliases_are_treated_as_overlapping(self):
        alias = self.root / "results-alias"
        try:
            alias.symlink_to(self.results, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlinks unavailable: {}".format(exc))
        spec = importlib.util.spec_from_file_location(
            "evaluator_bootstrap_for_test", BOOTSTRAP_SOURCE
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._load_stdlib()
        real_record = module._validate_existing_path(
            "results", str(self.results), "directory"
        )
        alias_record = module._validate_existing_path(
            "alias", str(alias), "directory"
        )
        self.assertTrue(module._records_overlap(real_record, alias_record))

    def test_case_alias_is_rejected_when_the_filesystem_accepts_it(self):
        case_alias = self.repository.with_name(self.repository.name.upper())
        if str(case_alias) == str(self.repository) or not case_alias.exists():
            self.skipTest("test volume is case-sensitive")
        if not os.path.samefile(case_alias, self.repository):
            self.skipTest("uppercase path is not a case alias")
        spec = importlib.util.spec_from_file_location(
            "evaluator_bootstrap_case_test", BOOTSTRAP_SOURCE
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._load_stdlib()
        with self.assertRaisesRegex(module.BootstrapError, "exact path spelling"):
            module._validate_existing_path(
                "case alias", str(case_alias), "directory"
            )

    def test_missing_isolation_flags_fail_before_runner_import(self):
        command = self.command("plan")
        command.remove("-I")
        command.remove("-S")
        completed = subprocess.run(
            command,
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("both -I and -S", completed.stderr)
        self.assertNotIn('"runner": "trusted"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
