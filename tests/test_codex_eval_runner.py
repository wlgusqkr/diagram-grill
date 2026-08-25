import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import codex_runner  # noqa: E402


SESSION_ID = "019cc483-7f50-7f12-8abc-0123456789ab"


def execution_metadata(clean=True):
    return {
        "client": "Codex CLI",
        "client_version": "codex-cli 0.149.1",
        "client_executable": "/usr/local/bin/codex",
        "model_requested": "test-model",
        "model_version_declared": "test-model-2026-08-25",
        "operating_system": "test-os",
        "runner_environment": {
            "python_version": "3.14.4",
            "graphviz_version": "runner-dot-99.0",
            "diagrams_version": "runner-diagrams-99.0",
        },
        "skill_commit": "a" * 40,
        "repository_clean": clean,
        "tested_at": "2026-08-25T20:00:00+09:00",
    }


def write_skill_package(root):
    package = Path(root) / "skill-package"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: diagram-grill\ndescription: Test package.\n---\n\n# Diagram Grill\n",
        encoding="utf-8",
    )
    (package / "scripts" / "diagram_grill_cli.py").write_text(
        "print('safe')\n", encoding="utf-8"
    )
    (package / "requirements.lock").write_text(
        "diagrams==0.25.1\njsonschema==4.25.1\n", encoding="utf-8"
    )
    return package


def write_test_runtime(root):
    runtime_bin = Path(root) / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    python3 = runtime_bin / "python3"
    python3.write_text("test executable placeholder\n", encoding="utf-8")
    python3.chmod(0o755)
    freeze = "diagrams==0.25.1\njsonschema==4.25.1\n"
    return runtime_bin, {
        "runtime_bin": str(runtime_bin.resolve()),
        "python3": str(python3.resolve()),
        "preflight": {
            "ok": True,
            "checks": [
                {"name": "python", "actual": "3.9.6", "ok": True},
                {"name": "diagrams", "actual": "0.25.1", "ok": True},
                {"name": "jsonschema", "actual": "4.25.1", "ok": True},
                {"name": "dot", "actual": "15.1.1", "ok": True},
            ],
        },
        "preflight_stdout_sha256": "b" * 64,
        "pip_freeze": freeze,
        "pip_freeze_sha256": "c" * 64,
    }


def fake_invoker(calls):
    def invoke(run_dir, state, prompt, turn, initial, timeout_seconds):
        calls.append(
            {
                "run_dir": run_dir,
                "prompt": prompt,
                "turn": turn,
                "initial": initial,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "turn": turn,
            "initial": initial,
            "started_at": "2026-08-25T20:00:00+09:00",
            "finished_at": "2026-08-25T20:00:01+09:00",
            "exit_code": 0,
            "timed_out": False,
            "stdout": "raw/turn-{:03d}.jsonl".format(turn),
            "stderr": "raw/turn-{:03d}.stderr.txt".format(turn),
            "last_message": None,
            "session_id": SESSION_ID if initial else None,
        }

    return invoke


class PrelaunchPlanTests(unittest.TestCase):
    def test_plan_has_twelve_runs_per_condition_and_alternates_order(self):
        plan = codex_runner.build_prelaunch_plan()

        self.assertEqual(24, len(plan))
        self.assertEqual(list(range(1, 25)), [item["ordinal"] for item in plan])
        self.assertEqual(12, sum(item["condition"] == "baseline" for item in plan))
        self.assertEqual(12, sum(item["condition"] == "diagram-grill" for item in plan))
        self.assertEqual(
            ["baseline", "diagram-grill", "diagram-grill", "baseline"],
            [item["condition"] for item in plan[:4]],
        )

    def test_fixed_base_prompts_do_not_name_the_treatment(self):
        for case in codex_runner.harness.load_case_index():
            prompt = (
                codex_runner.EVAL_ROOT / str(case["prompt_path"])
            ).read_text(encoding="utf-8")
            self.assertNotIn("diagram-grill", prompt.lower(), case["id"])
            self.assertIn(codex_runner.harness.WORKSPACE_CONTRACT, prompt, case["id"])


class SkillPackageBoundaryTests(unittest.TestCase):
    def test_runtime_package_is_accepted_and_evaluation_material_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = write_skill_package(directory)
            manifest = codex_runner.validate_skill_package(package)
            self.assertEqual(3, manifest["file_count"])

            (package / "evals").mkdir()
            (package / "evals" / "oracle.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(codex_runner.RunnerError, "forbidden path"):
                codex_runner.validate_skill_package(package)

    def test_symlinked_package_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = write_skill_package(directory)
            (package / "linked.py").symlink_to(package / "scripts" / "run.py")

            with self.assertRaisesRegex(codex_runner.RunnerError, "symlinks"):
                codex_runner.validate_skill_package(package)

    def test_runtime_inside_repository_is_rejected_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(codex_runner.RunnerError, "outside"):
                codex_runner.validate_runtime(
                    REPOSITORY_ROOT / ".venv" / "bin",
                    REPOSITORY_ROOT / "skills" / "diagram-grill",
                    Path(directory) / "results",
                )


class RunLifecycleTests(unittest.TestCase):
    def prepare(self, directory, condition="diagram-grill", experiment="pilot"):
        package = write_skill_package(directory)
        runtime_bin, runtime_data = write_test_runtime(directory)
        return codex_runner.prepare_run(
            Path(directory) / "results",
            experiment,
            "01-checkout",
            condition,
            "test-model",
            "test-model-2026-08-25",
            runtime_bin,
            package,
            execution_metadata=execution_metadata(),
            runtime_validation=runtime_data,
        )

    def test_prepare_copies_only_prompt_repo_and_sanitized_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)

            self.assertTrue((run_dir / "workspace" / "repo" / "README.md").is_file())
            self.assertTrue(
                (
                    run_dir
                    / "workspace"
                    / ".agents"
                    / "skills"
                    / "diagram-grill"
                    / "SKILL.md"
                ).is_file()
            )
            relative_files = {
                path.relative_to(run_dir).as_posix()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            self.assertFalse(any("oracle" in path for path in relative_files))
            self.assertFalse(any("operator-answers" in path for path in relative_files))
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual("prepared", state["status"])
            self.assertEqual(2, state["planned_ordinal"])
            base_prompt = (run_dir / "input" / "prompt.md").read_text(encoding="utf-8")
            delivered = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(codex_runner.SKILL_INVOCATION_PREFIX + base_prompt, delivered)
            self.assertEqual(
                codex_runner.SKILL_INVOCATION_PREFIX,
                state["input"]["skill_invocation_prefix"],
            )
            self.assertTrue((run_dir / "input" / "runtime-manifest.json").is_file())
            self.assertTrue((run_dir / "input" / "pip-freeze.txt").is_file())
            protocol_hashes = state["input"]["protocol_hashes"]
            self.assertEqual(
                {
                    "operator_answers_sha256",
                    "case_oracle_sha256",
                    "cases_index_sha256",
                    "run_schema_sha256",
                },
                set(protocol_hashes),
            )
            self.assertTrue(
                all(
                    len(digest) == 64
                    and set(digest).issubset(set("0123456789abcdef"))
                    for digest in protocol_hashes.values()
                )
            )
            self.assertEqual(
                {
                    "model_reasoning_effort": "medium",
                    "service_tier": "default",
                    "approval_policy": "never",
                    "sandbox": "workspace-write",
                    "ignore_user_config": True,
                    "ignore_rules": True,
                    "runtime_bin": str((Path(directory) / "runtime" / "bin").resolve()),
                    "shell_environment_policy": "inherit-all-with-fixed-path-and-zdotdir",
                },
                state["execution"]["treatment_settings"],
            )
            self.assertEqual("3.9.6", state["execution"]["python_version"])
            self.assertEqual("0.25.1", state["execution"]["diagrams_version"])
            self.assertEqual("4.25.1", state["execution"]["jsonschema_version"])
            self.assertEqual("15.1.1", state["execution"]["graphviz_version"])
            self.assertEqual(
                "3.14.4",
                state["execution"]["runner_environment"]["python_version"],
            )
            self.assertEqual(
                {
                    "python": "3.9.6",
                    "diagrams": "0.25.1",
                    "jsonschema": "4.25.1",
                    "graphviz_dot": "15.1.1",
                },
                state["execution"]["runtime"]["authoritative_versions"],
            )
            shell_profile = (
                run_dir / "workspace" / ".eval-shell" / ".zprofile"
            ).read_text(encoding="utf-8")
            self.assertIn(str((Path(directory) / "runtime" / "bin").resolve()), shell_profile)

    def test_baseline_validates_but_does_not_install_the_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(
                directory, condition="baseline", experiment="baseline-treatment"
            )
            self.assertFalse((run_dir / "workspace" / ".agents").exists())
            base_prompt = (run_dir / "input" / "prompt.md").read_text(encoding="utf-8")
            delivered = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(base_prompt, delivered)
            self.assertFalse((run_dir / "input" / "skill-package").exists())

    def test_launch_and_reply_preserve_exact_scripted_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            invoke = fake_invoker(calls)

            launched = codex_runner.launch_run(run_dir, invoke=invoke)
            self.assertEqual("active", launched["status"])
            self.assertEqual(SESSION_ID, launched["session_id"])
            expected_prompt = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(expected_prompt, calls[0]["prompt"])

            with self.assertRaisesRegex(codex_runner.RunnerError, "brief-reviewed"):
                codex_runner.reply_run(run_dir, approve=True, invoke=invoke)

            answered = codex_runner.reply_run(
                run_dir, decision_ids=("payment-timing",), invoke=invoke
            )
            expected_answer = (
                "Authorize payment synchronously through an external payment provider before "
                "writing the order; do not create the order when authorization fails."
            )
            self.assertEqual(expected_answer, calls[1]["prompt"])
            self.assertEqual(["payment-timing"], answered["revealed_decision_ids"])

            approved = codex_runner.reply_run(
                run_dir, approve=True, brief_reviewed=True, invoke=invoke
            )
            self.assertEqual(codex_runner.APPROVAL_PHRASE, calls[2]["prompt"])
            self.assertTrue(approved["approval_sent"])
            events = [
                json.loads(line)
                for line in (run_dir / "raw" / "operator-turns.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                ["initial-prompt", "fixed-decision-answer", "architecture-brief-approval"],
                [event["kind"] for event in events],
            )

    def test_approval_is_withheld_until_all_decisions_were_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            invoke = fake_invoker([])
            codex_runner.launch_run(run_dir, invoke=invoke)

            with self.assertRaisesRegex(codex_runner.RunnerError, "approval is withheld"):
                codex_runner.reply_run(
                    run_dir, approve=True, brief_reviewed=True, invoke=invoke
                )

    def test_reply_refuses_protocol_input_drift_before_sending_text(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            invoke = fake_invoker(calls)
            state = codex_runner.launch_run(run_dir, invoke=invoke)
            changed = dict(state["input"]["protocol_hashes"])
            changed["operator_answers_sha256"] = "0" * 64

            with mock.patch.object(
                codex_runner, "_protocol_hashes", return_value=changed
            ):
                with self.assertRaisesRegex(
                    codex_runner.RunnerError, "frozen protocol input changed"
                ):
                    codex_runner.reply_run(
                        run_dir,
                        decision_ids=("payment-timing",),
                        invoke=invoke,
                    )

            self.assertEqual(1, len(calls))
            operator_events = (
                run_dir / "raw" / "operator-turns.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(operator_events))

    def test_indexed_and_unindexed_questions_can_share_one_fixed_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            invoke = fake_invoker(calls)
            codex_runner.launch_run(run_dir, invoke=invoke)

            codex_runner.reply_run(
                run_dir,
                decision_ids=("payment-timing",),
                unknown=True,
                invoke=invoke,
            )

            self.assertTrue(calls[1]["prompt"].endswith(codex_runner.UNKNOWN_PHRASE))
            event = json.loads(
                (run_dir / "raw" / "operator-turns.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual("fixed-mixed-answer", event["kind"])
            self.assertTrue(event["includes_unknown_answer"])

    def test_unknown_reply_is_fixed_and_source_changes_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline", experiment="baseline-pilot")
            calls = []
            invoke = fake_invoker(calls)
            codex_runner.launch_run(run_dir, invoke=invoke)
            codex_runner.reply_run(run_dir, unknown=True, invoke=invoke)
            self.assertEqual(codex_runner.UNKNOWN_PHRASE, calls[1]["prompt"])

            (run_dir / "workspace" / "repo" / "README.md").write_text(
                "modified evidence\n", encoding="utf-8"
            )
            closed = codex_runner.close_run(run_dir, "agent-finished")
            self.assertEqual("closed", closed["status"])
            self.assertEqual(
                ["evidence repository changed after preparation"],
                closed["source_integrity_violations"],
            )

    def test_missing_or_symlinked_inputs_are_recorded_without_interrupting_close(self):
        scenarios = (
            (
                "repo-deleted",
                "repo",
                False,
                "evidence repository is missing, symlinked, or unreadable",
            ),
            (
                "repo-symlinked",
                "repo",
                True,
                "evidence repository is missing, symlinked, or unreadable",
            ),
            (
                "skill-deleted",
                "skill",
                False,
                "installed skill is missing, symlinked, or unreadable",
            ),
            (
                "skill-symlinked",
                "skill",
                True,
                "installed skill is missing, symlinked, or unreadable",
            ),
        )
        for experiment, target_name, make_symlink, expected in scenarios:
            with self.subTest(experiment=experiment), tempfile.TemporaryDirectory() as directory:
                run_dir = self.prepare(directory, experiment=experiment)
                invoke = fake_invoker([])
                codex_runner.launch_run(run_dir, invoke=invoke)
                if target_name == "repo":
                    target = run_dir / "workspace" / "repo"
                else:
                    target = (
                        run_dir
                        / "workspace"
                        / ".agents"
                        / "skills"
                        / "diagram-grill"
                    )
                moved = target.with_name(target.name + "-removed")
                target.rename(moved)
                if make_symlink:
                    target.symlink_to(moved, target_is_directory=True)

                closed = codex_runner.close_run(run_dir, "operator-terminated")

                self.assertEqual("closed", closed["status"])
                self.assertIn(expected, closed["source_integrity_violations"])

    def test_real_invocation_fixes_codex_shell_environment_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            stdout = json.dumps({"type": "thread.started", "thread_id": SESSION_ID}) + "\n"
            completed = mock.Mock(returncode=0, stdout=stdout, stderr="")

            with mock.patch.object(codex_runner.subprocess, "run", return_value=completed) as run:
                result = codex_runner._invoke_codex(
                    run_dir, state, "prompt", 1, True, 30
                )

            command = run.call_args.args[0]
            self.assertIn("shell_environment_policy.inherit=all", command)
            self.assertTrue(
                any(
                    value.startswith("shell_environment_policy.set.PATH=")
                    for value in command
                )
            )
            self.assertTrue(
                any(
                    value.startswith("shell_environment_policy.set.ZDOTDIR=")
                    for value in command
                )
            )
            environment = run.call_args.kwargs["env"]
            self.assertEqual(
                str(run_dir / "workspace" / ".eval-shell"), environment["ZDOTDIR"]
            )
            self.assertEqual(SESSION_ID, result["session_id"])


if __name__ == "__main__":
    unittest.main()
