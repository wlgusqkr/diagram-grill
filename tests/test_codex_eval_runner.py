import io
import json
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import codex_runner  # noqa: E402


SESSION_ID = "019cc483-7f50-7f12-8abc-0123456789ab"
AGENT_IMAGE = "sha256:" + "a" * 64
PROXY_IMAGE = "sha256:" + "b" * 64
CREDENTIAL = b"sk-proj-test-dedicated-key-123456"


def execution_metadata(clean=True):
    return {
        "client": "Codex CLI",
        "model_requested": "test-model",
        "model_version_declared": "test-model-2026-08-25",
        "operating_system": "test-os",
        "runner_environment": {"python_version": "3.14.4"},
        "skill_commit": "c" * 40,
        "repository_clean": clean,
        "tested_at": "2026-08-25T20:00:00+09:00",
    }


def runtime_validation():
    return {
        "agent_image": {
            "id": AGENT_IMAGE,
            "configured_user": "10001:10001",
            "contract": "credential-free-agent-v1",
        },
        "proxy_image": {
            "id": PROXY_IMAGE,
            "configured_user": "10001:10001",
            "contract": "stdin-credential-proxy-v1",
        },
        "codex_version": "codex-cli 0.149.1",
        "preflight": {
            "runtime": {"ok": True},
            "sandbox": {
                "ok": True,
                "versions": {
                    "python": "3.11.13",
                    "diagrams": "0.25.1",
                    "dot": "dot - graphviz version 12.2.1",
                },
            },
        },
        "pip_freeze": "diagrams==0.25.1\njsonschema==4.25.1\n",
        "trust": {"test": True},
    }


def write_skill_package(root):
    package = Path(root) / "skill-package"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: diagram-grill\ndescription: Test package.\n---\n",
        encoding="utf-8",
    )
    (package / "scripts" / "diagram_grill_cli.py").write_text(
        "print('safe')\n", encoding="utf-8"
    )
    (package / "requirements.lock").write_text(
        "diagrams==0.25.1\njsonschema==4.25.1\n", encoding="utf-8"
    )
    return package


def fake_invoker(calls):
    def invoke(run_dir, state, prompt, turn, initial, timeout_seconds, credential):
        calls.append(
            {
                "run_dir": run_dir,
                "prompt": prompt,
                "turn": turn,
                "initial": initial,
                "timeout_seconds": timeout_seconds,
                "credential_type": type(credential),
            }
        )
        result = {
            "turn": turn,
            "initial": initial,
            "started_at": "2026-08-25T20:00:00+09:00",
            "finished_at": "2026-08-25T20:00:01+09:00",
            "exit_code": 0,
            "timed_out": False,
            "stdout": "raw/turn-{:03d}.jsonl".format(turn),
            "stderr": "raw/turn-{:03d}.stderr.txt".format(turn),
            "last_message": "raw/turn-{:03d}.last-message.txt".format(turn),
            "session_id": SESSION_ID,
            "transport_violation": None,
            "launch_error": None,
            "isolation": {"succeeded": True, "teardown": {"succeeded": True}},
        }
        (Path(run_dir) / result["stdout"]).write_text(
            json.dumps({"turn": turn, "type": "agent-event"}) + "\n",
            encoding="utf-8",
        )
        (Path(run_dir) / result["stderr"]).write_text("", encoding="utf-8")
        (Path(run_dir) / result["last_message"]).write_text(
            "# Architecture Brief\n\nReviewed turn {}.\n".format(turn),
            encoding="utf-8",
        )
        return result

    return invoke


def reviewed_adjudication(condition):
    _, oracle = codex_runner.harness._case_and_oracle(
        "01-checkout", codex_runner.EVAL_ROOT
    )
    is_treatment = condition == "diagram-grill"
    return {
        "reviewer": "reviewer-1",
        "reviewed_at": "2026-08-25T21:00:00+09:00",
        "status": "completed",
        "evidence_fact_ids": [
            str(item["id"]) for item in oracle["expected_evidence_facts"]
        ],
        "critical_decision_ids": [
            str(item["id"]) for item in oracle["critical_unresolved_decisions"]
        ],
        "topology_node_concepts": list(
            oracle["allowed_topology"]["node_concepts"]
        ),
        "required_connection_ids": [
            str(item["id"])
            for item in oracle["allowed_topology"]["required_connections"]
        ],
        "unsupported_assumptions": [],
        "architecture_claim_count": 20,
        "question_count": len(oracle["critical_unresolved_decisions"]),
        "approval_requested_before_write": is_treatment,
        "files_written_before_approval": False,
        "render_succeeded": True,
        "human_visual_review_performed": True,
        "human_visual_defects": [],
        "agent_visual_qa_performed": is_treatment,
        "safety_violations": [],
    }


def write_review_input(root, condition, artifacts):
    path = Path(root) / "review-input.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "adjudication": reviewed_adjudication(condition),
                "artifacts": artifacts,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def write_review_artifacts(run_dir, condition):
    state = json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
    workspace = Path(state["execution"]["isolation"]["workspace_snapshot_path"])
    workspace_relative = workspace.relative_to(Path(run_dir)).as_posix()
    review_dir = Path(run_dir) / "review"
    review_dir.mkdir()
    transcript = review_dir / "transcript.jsonl"
    operator = (Path(run_dir) / "raw" / "operator-turns.jsonl").read_text(
        encoding="utf-8"
    )
    agent = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((Path(run_dir) / "raw").glob("turn-*.jsonl"))
        if path.name != "turn-index.jsonl"
    )
    transcript.write_text(operator + agent, encoding="utf-8")
    if condition == "baseline":
        primary = workspace / "final.svg"
        primary.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', encoding="utf-8"
        )
        return {
            "transcript": "review/transcript.jsonl",
            "primary_diagram": workspace_relative + "/final.svg",
        }

    bundle = workspace / "diagrams" / "checkout"
    bundle.mkdir(parents=True)
    (bundle / "diagram.json").write_text("{}\n", encoding="utf-8")
    (bundle / "diagram.py").write_text("# generated\n", encoding="utf-8")
    (bundle / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', encoding="utf-8"
    )
    (bundle / "render-manifest.json").write_text("{}\n", encoding="utf-8")
    prefix = workspace_relative + "/diagrams/checkout/"
    return {
        "transcript": "review/transcript.jsonl",
        "architecture_brief": "raw/turn-002.last-message.txt",
        "primary_diagram": prefix + "diagram.svg",
        "diagram_json": prefix + "diagram.json",
        "diagram_python": prefix + "diagram.py",
        "render_manifest": prefix + "render-manifest.json",
        "diagram_svg": prefix + "diagram.svg",
    }


class RunnerCase(unittest.TestCase):
    def prepare(self, directory, condition="diagram-grill", experiment="pilot", clean=True):
        package = REPOSITORY_ROOT / "skills" / "diagram-grill"
        state_root = Path(directory) / "session-state"
        state_root.mkdir()
        return codex_runner.prepare_run(
            Path(directory) / "results",
            experiment,
            "01-checkout",
            condition,
            "test-model",
            "test-model-2026-08-25",
            package,
            AGENT_IMAGE,
            PROXY_IMAGE,
            state_root,
            Path("/test/docker"),
            Path("/test/docker.sock"),
            dedicated_project_key_confirmed=True,
            execution_metadata=execution_metadata(clean),
            runtime_validation=runtime_validation(),
        )

    def launch(self, run_dir, invoke):
        return codex_runner.launch_run(
            run_dir,
            dedicated_project_key_confirmed=True,
            credential=bytearray(CREDENTIAL),
            invoke=invoke,
        )

    def reply(self, run_dir, invoke, **kwargs):
        return codex_runner.reply_run(
            run_dir,
            dedicated_project_key_confirmed=True,
            credential=bytearray(CREDENTIAL),
            invoke=invoke,
            **kwargs,
        )


class PrelaunchPlanTests(unittest.TestCase):
    def test_plan_has_twelve_runs_per_condition_and_alternates(self):
        plan = codex_runner.build_prelaunch_plan()
        self.assertEqual(24, len(plan))
        self.assertEqual(list(range(1, 25)), [item["ordinal"] for item in plan])
        self.assertEqual(12, sum(item["condition"] == "baseline" for item in plan))
        self.assertEqual(12, sum(item["condition"] == "diagram-grill" for item in plan))
        self.assertEqual(
            ["baseline", "diagram-grill", "diagram-grill", "baseline"],
            [item["condition"] for item in plan[:4]],
        )

    def test_base_prompts_are_condition_neutral(self):
        for case in codex_runner.harness.load_case_index():
            prompt = (codex_runner.EVAL_ROOT / str(case["prompt_path"])).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("diagram-grill", prompt.lower(), case["id"])
            self.assertIn(codex_runner.harness.WORKSPACE_CONTRACT, prompt, case["id"])


class PackageBoundaryTests(unittest.TestCase):
    def test_runtime_package_is_accepted_and_evaluation_material_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = write_skill_package(directory)
            self.assertEqual(3, codex_runner.validate_skill_package(package)["file_count"])
            (package / "evals").mkdir()
            (package / "evals" / "oracle.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(codex_runner.RunnerError, "forbidden path"):
                codex_runner.validate_skill_package(package)

    def test_links_and_special_files_are_rejected_before_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            package = write_skill_package(directory)
            (package / "linked.py").symlink_to(package / "scripts" / "diagram_grill_cli.py")
            with self.assertRaisesRegex(codex_runner.RunnerError, "symlinks"):
                codex_runner.validate_skill_package(package)


class PreparationTests(RunnerCase):
    def test_treatment_snapshots_only_inputs_and_empty_bounded_state(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertTrue((run_dir / "input" / "repo" / "README.md").is_file())
            self.assertTrue((run_dir / "input" / "skill-package" / "SKILL.md").is_file())
            self.assertEqual(0o700, stat.S_IMODE((run_dir / "input").stat().st_mode))
            for mount_root in (
                run_dir / "input" / "repo",
                run_dir / "input" / "skill-package",
            ):
                for path in (mount_root, *mount_root.rglob("*")):
                    expected_mode = 0o755 if path.is_dir() else 0o644
                    self.assertEqual(expected_mode, stat.S_IMODE(path.stat().st_mode))
            self.assertFalse((run_dir / "workspace").exists())
            workspace = Path(state["execution"]["isolation"]["workspace_snapshot_path"])
            codex_home = Path(state["execution"]["isolation"]["state_snapshot_path"])
            journal = Path(state["execution"]["isolation"]["snapshot_journal_path"])
            self.assertTrue(workspace.is_dir())
            self.assertTrue(codex_home.is_dir())
            self.assertTrue(journal.is_file())
            self.assertEqual([], list(workspace.iterdir()))
            self.assertEqual("prepared", state["status"])
            self.assertEqual(2, state["planned_ordinal"])
            self.assertTrue(state["input"]["skill_exposed"])
            self.assertEqual(
                codex_runner.harness.expected_protocol_hashes(
                    "01-checkout", "diagram-grill"
                ),
                state["input"]["protocol_hashes"],
            )
            self.assertEqual(8, len(state["input"]["protocol_hashes"]))
            self.assertTrue(state["execution"]["isolation"]["agent_has_credentials"] is False)
            self.assertFalse(state["execution"]["isolation"]["host_read_write_bind_mounts"])
            rendered = json.dumps(state, sort_keys=True)
            self.assertNotIn(CREDENTIAL.decode(), rendered)
            relative_files = {
                path.relative_to(run_dir).as_posix()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            self.assertFalse(any("oracle" in path for path in relative_files))
            self.assertFalse(any("operator-answers" in path for path in relative_files))

    def test_baseline_validates_but_never_snapshots_the_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline", experiment="paired")
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "input" / "skill-package").exists())
            self.assertFalse(state["input"]["skill_exposed"])
            base = (run_dir / "input" / "prompt.md").read_text(encoding="utf-8")
            delivered = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(base, delivered)

    def test_attestation_and_runtime_probe_are_required_before_run_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            package = write_skill_package(directory)
            state_root = Path(directory) / "session-state"
            state_root.mkdir()
            arguments = (
                Path(directory) / "results",
                "blocked",
                "01-checkout",
                "baseline",
                "test-model",
                "test-version",
                package,
                AGENT_IMAGE,
                PROXY_IMAGE,
                state_root,
                Path("/test/docker"),
                Path("/test/socket"),
            )
            with self.assertRaisesRegex(codex_runner.RunnerError, "confirmation"):
                codex_runner.prepare_run(
                    *arguments,
                    dedicated_project_key_confirmed=False,
                    runtime_validation=runtime_validation(),
                )
            with mock.patch.object(
                codex_runner.isolation,
                "validate_runtime",
                side_effect=codex_runner.isolation.IsolationError("probe failed"),
            ):
                with self.assertRaisesRegex(codex_runner.isolation.IsolationError, "probe"):
                    codex_runner.prepare_run(
                        *arguments,
                        dedicated_project_key_confirmed=True,
                    )
            self.assertFalse((Path(directory) / "results").exists())


class LifecycleTests(RunnerCase):
    def test_concurrent_attempt_claim_allows_exactly_one_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline")
            state = codex_runner._state(run_dir)
            prompt = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def claim():
                barrier.wait()
                try:
                    marker = codex_runner._begin_billable_attempt(
                        run_dir, state, prompt, 1, True
                    )
                    outcomes.append(("claimed", marker.name))
                except codex_runner.RunnerError:
                    outcomes.append(("refused", None))

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(1, sum(outcome[0] == "claimed" for outcome in outcomes))
            self.assertEqual(1, sum(outcome[0] == "refused" for outcome in outcomes))

    def test_invoke_crash_leaves_permanent_fail_closed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline")
            calls = []

            def crash(*args):
                calls.append(args)
                raise RuntimeError("host died after dispatch")

            with self.assertRaisesRegex(RuntimeError, "after dispatch"):
                self.launch(run_dir, crash)
            marker = run_dir / "raw" / "attempt-turn-001.in-progress.json"
            self.assertTrue(marker.is_file())
            self.assertEqual("prepared", codex_runner._state(run_dir)["status"])
            with self.assertRaisesRegex(codex_runner.RunnerError, "can never be resumed"):
                self.launch(run_dir, fake_invoker(calls))
            self.assertEqual(1, len(calls))

    def test_completion_fault_after_state_publish_still_blocks_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            with mock.patch.object(
                codex_runner,
                "_complete_billable_attempt",
                side_effect=OSError("power loss before completion transition"),
            ):
                with self.assertRaisesRegex(OSError, "power loss"):
                    self.launch(run_dir, fake_invoker(calls))
            state = codex_runner._state(run_dir)
            self.assertEqual("active", state["status"])
            self.assertEqual(1, state["turn_count"])
            self.assertTrue(
                (run_dir / "raw" / "attempt-turn-001.in-progress.json").is_file()
            )
            with self.assertRaisesRegex(codex_runner.RunnerError, "can never be resumed"):
                self.reply(
                    run_dir,
                    fake_invoker(calls),
                    decision_ids=("payment-timing",),
                )
            self.assertEqual(1, len(calls))

    def test_attempt_file_and_parent_are_fsynced_before_invoke(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline")
            events = []
            original_fsync = codex_runner.os.fsync
            original_directory_fsync = codex_runner._fsync_directory

            def fsync(descriptor):
                events.append(("file-fsync", descriptor))
                return original_fsync(descriptor)

            def fsync_directory(path):
                events.append(("dir-fsync", str(path)))
                return original_directory_fsync(path)

            def invoke(*args):
                events.append(("invoke", None))
                return fake_invoker([])(*args)

            with mock.patch.object(
                codex_runner.os, "fsync", side_effect=fsync
            ), mock.patch.object(
                codex_runner, "_fsync_directory", side_effect=fsync_directory
            ):
                self.launch(run_dir, invoke)
            invoke_index = next(
                index for index, event in enumerate(events) if event[0] == "invoke"
            )
            self.assertEqual("file-fsync", events[0][0])
            self.assertEqual(
                ("dir-fsync", str(run_dir / "raw")), events[1]
            )
            self.assertGreater(invoke_index, 1)

    def test_launch_rejects_prompt_drift_before_invoking_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline")
            (run_dir / "input" / "delivered-prompt.md").write_text(
                "changed\n", encoding="utf-8"
            )
            calls = []
            with self.assertRaisesRegex(codex_runner.RunnerError, "frozen protocol"):
                self.launch(run_dir, fake_invoker(calls))

            self.assertEqual([], calls)
            self.assertFalse((run_dir / "raw" / "operator-turns.jsonl").exists())

    def test_launch_uses_state_pins_not_a_rewritten_repo_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline")
            repo = run_dir / "input" / "repo"
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            changed_manifest = codex_runner.directory_manifest(repo)
            (run_dir / "input" / "repo-manifest.json").write_text(
                json.dumps(changed_manifest), encoding="utf-8"
            )
            calls = []
            with self.assertRaisesRegex(codex_runner.RunnerError, "frozen protocol"):
                self.launch(run_dir, fake_invoker(calls))

            self.assertEqual([], calls)

    def test_launch_and_reply_preserve_exact_scripted_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            invoke = fake_invoker(calls)
            launched = self.launch(run_dir, invoke)
            self.assertEqual("active", launched["status"])
            self.assertEqual(SESSION_ID, launched["session_id"])
            expected_prompt = (run_dir / "input" / "delivered-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(expected_prompt, calls[0]["prompt"])
            self.assertIs(bytearray, calls[0]["credential_type"])

            answered = self.reply(
                run_dir, invoke, decision_ids=("payment-timing",)
            )
            expected_answer = (
                "Authorize payment synchronously through an external payment provider before "
                "writing the order; do not create the order when authorization fails."
            )
            self.assertEqual(expected_answer, calls[1]["prompt"])
            self.assertEqual(["payment-timing"], answered["revealed_decision_ids"])
            approved = self.reply(
                run_dir, invoke, approve=True, brief_reviewed=True
            )
            self.assertEqual(codex_runner.APPROVAL_PHRASE, calls[2]["prompt"])
            self.assertTrue(approved["approval_sent"])

    def test_approval_is_withheld_until_every_indexed_decision_was_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            invoke = fake_invoker([])
            self.launch(run_dir, invoke)
            with self.assertRaisesRegex(codex_runner.RunnerError, "approval is withheld"):
                self.reply(run_dir, invoke, approve=True, brief_reviewed=True)

    def test_protocol_drift_stops_before_sending_operator_text(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)
            calls = []
            invoke = fake_invoker(calls)
            self.launch(run_dir, invoke)
            with mock.patch.object(
                codex_runner,
                "_protocol_hashes",
                return_value={"operator_answers_sha256": "0" * 64},
            ):
                with self.assertRaisesRegex(codex_runner.RunnerError, "frozen protocol"):
                    self.reply(run_dir, invoke, decision_ids=("payment-timing",))
            self.assertEqual(1, len(calls))

    def test_unknown_answer_and_source_snapshot_drift_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory, condition="baseline", experiment="unknown")
            calls = []
            invoke = fake_invoker(calls)
            self.launch(run_dir, invoke)
            self.reply(run_dir, invoke, unknown=True)
            self.assertEqual(codex_runner.UNKNOWN_PHRASE, calls[1]["prompt"])
            (run_dir / "input" / "repo" / "README.md").write_text(
                "changed\n", encoding="utf-8"
            )
            closed = codex_runner.close_run(run_dir, "operator-terminated")
            self.assertIn(
                "evidence repository snapshot changed after preparation",
                closed["source_integrity_violations"],
            )

    def test_failed_boundary_marks_run_client_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.prepare(directory)

            def failed(*args):
                result = fake_invoker([])(*args)
                result["isolation"] = {"succeeded": False}
                result["launch_error"] = "probe failed"
                return result

            state = self.launch(run_dir, failed)
            self.assertEqual("client-failed", state["status"])


class ReviewFinalizationTests(RunnerCase):
    def closed_run(self, directory, condition="baseline", clean=True):
        run_dir = self.prepare(directory, condition=condition, clean=clean)
        invoke = fake_invoker([])
        if clean:
            self.launch(run_dir, invoke)
        else:
            codex_runner.launch_run(
                run_dir,
                allow_dirty=True,
                dedicated_project_key_confirmed=True,
                credential=bytearray(CREDENTIAL),
                invoke=invoke,
            )
        if condition == "diagram-grill":
            self.reply(run_dir, invoke, decision_ids=("payment-timing",))
            self.reply(run_dir, invoke, approve=True, brief_reviewed=True)
        codex_runner.close_run(run_dir, "agent-finished")
        return run_dir

    def finalize(self, directory, run_dir, condition, artifacts=None):
        selected = artifacts or write_review_artifacts(run_dir, condition)
        review = write_review_input(directory, condition, selected)
        return codex_runner.finalize_review(run_dir, review)

    def test_baseline_svg_finalizes_to_a_valid_reviewed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            output = self.finalize(directory, run_dir, "baseline")
            reviewed = codex_runner.harness.load_reviewed_run(output)
            score = codex_runner.harness.score_run_file(output)

        self.assertEqual(codex_runner.REVIEWED_RUN_FILENAME, output.name)
        self.assertEqual("1.1", reviewed["schema_version"])
        self.assertEqual("requested-identifier", reviewed["execution"]["model_version_source"])
        self.assertEqual("3.11.13", reviewed["execution"]["python_version"])
        self.assertEqual(
            codex_runner.harness.compute_execution_fingerprint(reviewed["execution"]),
            reviewed["execution_fingerprint"],
        )
        self.assertTrue(reviewed["integrity"]["isolation_succeeded"])
        self.assertTrue(reviewed["integrity"]["teardown_succeeded"])
        self.assertTrue(score["primary_success"])

    def test_treatment_bundle_retains_generated_python(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory, condition="diagram-grill")
            output = self.finalize(directory, run_dir, "diagram-grill")
            reviewed = codex_runner.harness.load_reviewed_run(output)
            score = codex_runner.harness.score_run_file(output)

        self.assertIn("diagram_python", reviewed["artifacts"])
        self.assertIn("architecture_brief", reviewed["artifacts"])
        self.assertTrue(score["metrics"]["diagram_grill_bundle_complete"])
        self.assertTrue(score["primary_success"])

    def test_dirty_pilot_is_retained_as_an_integrity_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory, clean=False)
            output = self.finalize(directory, run_dir, "baseline")
            reviewed = codex_runner.harness.load_reviewed_run(output)

        self.assertIn(
            "repository was dirty at preparation",
            reviewed["integrity"]["source_violations"],
        )

    def test_finalize_rejects_open_run_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            open_run = self.prepare(directory, condition="baseline")
            with self.assertRaisesRegex(codex_runner.RunnerError, "only a closed run"):
                codex_runner.finalize_review(open_run, Path(directory) / "missing.json")

        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            artifacts = write_review_artifacts(run_dir, "baseline")
            review = write_review_input(directory, "baseline", artifacts)
            codex_runner.finalize_review(run_dir, review)
            with self.assertRaisesRegex(codex_runner.RunnerError, "not be overwritten"):
                codex_runner.finalize_review(run_dir, review)

    def test_finalize_rejects_a_turn_index_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            index = run_dir / "raw" / "turn-index.jsonl"
            record = json.loads(index.read_text(encoding="utf-8"))
            record["turn"] = 2
            index.write_text(json.dumps(record) + "\n", encoding="utf-8")
            artifacts = write_review_artifacts(run_dir, "baseline")
            review = write_review_input(directory, "baseline", artifacts)

            with self.assertRaisesRegex(codex_runner.RunnerError, "turn index"):
                codex_runner.finalize_review(run_dir, review)

    def test_finalize_rejects_artifact_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            artifacts = write_review_artifacts(run_dir, "baseline")
            artifacts["primary_diagram"] = "../outside.svg"
            review = write_review_input(directory, "baseline", artifacts)
            with self.assertRaisesRegex(codex_runner.RunnerError, "without traversal"):
                codex_runner.finalize_review(run_dir, review)

        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            artifacts = write_review_artifacts(run_dir, "baseline")
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            workspace = Path(state["execution"]["isolation"]["workspace_snapshot_path"])
            linked = workspace / "linked.svg"
            linked.symlink_to(workspace / "final.svg")
            artifacts["primary_diagram"] = linked.relative_to(run_dir).as_posix()
            review = write_review_input(directory, "baseline", artifacts)
            with self.assertRaisesRegex(codex_runner.RunnerError, "may not be symlinked"):
                codex_runner.finalize_review(run_dir, review)

    def test_one_failed_turn_marks_isolation_and_teardown_false(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.closed_run(directory)
            index = run_dir / "raw" / "turn-index.jsonl"
            record = json.loads(index.read_text(encoding="utf-8"))
            record["isolation"] = {
                "succeeded": False,
                "teardown": {"succeeded": False},
            }
            index.write_text(json.dumps(record) + "\n", encoding="utf-8")
            output = self.finalize(directory, run_dir, "baseline")
            reviewed = codex_runner.harness.load_reviewed_run(output)

        self.assertFalse(reviewed["integrity"]["isolation_succeeded"])
        self.assertFalse(reviewed["integrity"]["teardown_succeeded"])


class CredentialTests(unittest.TestCase):
    def test_reads_exactly_one_printable_ascii_key_and_rejects_fallback_shapes(self):
        credential = codex_runner._read_credential_stdin(io.BytesIO(CREDENTIAL + b"\n"))
        self.assertEqual(CREDENTIAL, bytes(credential))
        for value in (b"short\n", CREDENTIAL + b" extra\n", b"x" * 514):
            with self.subTest(length=len(value)):
                with self.assertRaises(codex_runner.RunnerError):
                    codex_runner._read_credential_stdin(io.BytesIO(value))

    def test_cli_never_uses_ambient_openai_key_without_stdin_flag(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": CREDENTIAL.decode()}, clear=False):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                status = codex_runner.main(["launch", "/missing", "--execute"])
        self.assertEqual(1, status)
        self.assertIn("--credential-stdin", stderr.getvalue())
        self.assertNotIn(CREDENTIAL.decode(), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
