import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import harness  # noqa: E402


def execution_record():
    return {
        "client": "Codex CLI",
        "client_version": "0.149.1",
        "model": "example-model",
        "model_version": "example-model-2026-08-25",
        "model_version_source": "requested-identifier",
        "operating_system": "Linux",
        "python_version": "3.11.13",
        "graphviz_version": "12.2.1",
        "diagrams_version": "0.25.1",
        "skill_commit": "a" * 40,
        "agent_image_id": "sha256:" + "b" * 64,
        "proxy_image_id": "sha256:" + "c" * 64,
        "reasoning_effort": "medium",
        "service_tier": "default",
        "approval_policy": "never",
        "sandbox_mode": "workspace-write",
        "tool_network": "disabled",
        "web_search": "disabled",
        "tested_at": "2026-08-25T20:00:00+09:00",
    }


def reviewed_run(case_id="01-checkout", condition="diagram-grill"):
    _, oracle = harness._case_and_oracle(case_id, harness.EVAL_ROOT)
    execution = execution_record()
    artifacts = {
        "transcript": "transcript.txt",
        "primary_diagram": "diagram.svg",
    }
    is_treatment = condition == "diagram-grill"
    if is_treatment:
        artifacts.update(
            {
                "architecture_brief": "architecture-brief.md",
                "diagram_json": "diagram.json",
                "diagram_python": "diagram.py",
                "render_manifest": "render-manifest.json",
                "diagram_svg": "diagram.svg",
            }
        )
    return {
        "schema_version": "1.1",
        "phase": "prelaunch-24",
        "case_id": case_id,
        "condition": condition,
        "repeat": 1,
        "execution": execution,
        "execution_fingerprint": harness.compute_execution_fingerprint(execution),
        "protocol_hashes": harness.expected_protocol_hashes(case_id, condition),
        "integrity": {
            "source_violations": [],
            "session_violations": [],
            "isolation_succeeded": True,
            "teardown_succeeded": True,
        },
        "artifacts": artifacts,
        "adjudication": {
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
        },
    }


class FixtureValidationTests(unittest.TestCase):
    def test_all_twelve_fixed_fixtures_are_consistent(self):
        self.assertEqual([], harness.validate_fixtures())
        self.assertEqual(12, len(harness.load_case_index()))
        self.assertEqual([], harness.validate_oss_metadata())

    def test_every_base_prompt_has_the_neutral_viewable_output_contract(self):
        for case in harness.load_case_index():
            prompt = (harness.EVAL_ROOT / str(case["prompt_path"])).read_text(
                encoding="utf-8"
            )
            self.assertIn(harness.WORKSPACE_CONTRACT, prompt)
            self.assertNotIn("diagram-grill", prompt.lower())


class RunScoringTests(unittest.TestCase):
    def write_run(self, directory, run, filename="run.json"):
        root = Path(directory)
        value = copy.deepcopy(run)
        artifact_paths = {
            artifact["path"] if isinstance(artifact, dict) else artifact
            for artifact in value["artifacts"].values()
        }
        for artifact_path in artifact_paths:
            path = root / artifact_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".svg":
                content = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n'
            elif path.suffix == ".json":
                content = b"{}\n"
            elif path.suffix == ".py":
                content = b"# retained generated source\n"
            else:
                content = b"raw artifact\n"
            path.write_bytes(content)
        value["artifacts"] = {
            name: self.artifact_ref(
                root,
                artifact["path"] if isinstance(artifact, dict) else artifact,
            )
            for name, artifact in value["artifacts"].items()
        }
        path = root / filename
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    @staticmethod
    def artifact_ref(root, artifact_path):
        path = root / artifact_path
        return {
            "path": artifact_path,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    def write_matrix(self, directory, mutate=None):
        paths = []
        ordinal = 0
        for case in harness.load_case_index():
            for condition in harness.PHASE_CONDITIONS["prelaunch-24"]:
                run = reviewed_run(str(case["id"]), condition)
                if mutate is not None:
                    mutate(run, ordinal)
                run_dir = Path(directory) / "run-{:02d}".format(ordinal)
                run_dir.mkdir()
                paths.append(self.write_run(run_dir, run))
                ordinal += 1
        return paths

    def test_treatment_scores_primary_success_and_complete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_run(directory, reviewed_run())
            score = harness.score_run_file(path)

        self.assertTrue(score["primary_success"])
        self.assertTrue(score["metrics"]["diagram_grill_bundle_complete"])
        self.assertEqual(1.0, score["metrics"]["critical_ambiguity_coverage"])
        self.assertEqual(1.0, score["metrics"]["required_connection_coverage"])
        self.assertEqual(0, score["metrics"]["unsupported_assumption_count"])
        self.assertNotIn("strict_success", score)
        self.assertNotIn("weighted_score", score)

    def test_fair_baseline_can_succeed_without_approval_or_skill_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_run(directory, reviewed_run(condition="baseline"))
            score = harness.score_run_file(path)

        self.assertTrue(score["primary_success"])
        self.assertFalse(score["metrics"]["approval_fidelity"])
        self.assertFalse(score["metrics"]["diagram_grill_bundle_complete"])
        self.assertFalse(score["metrics"]["agent_visual_qa_performed"])

    def test_svg_can_receive_human_visual_review_without_png(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run(condition="baseline")
            self.assertNotIn("diagram_png", run["artifacts"])
            path = self.write_run(directory, run)
            score = harness.score_run_file(path)

        self.assertTrue(score["metrics"]["human_visual_review_performed"])
        self.assertTrue(score["primary_success"])

    def test_render_or_human_review_requires_a_primary_diagram(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run(condition="baseline")
            del run["artifacts"]["primary_diagram"]
            path = self.write_run(directory, run)

            with self.assertRaisesRegex(
                harness.EvaluationError, "missing required artifact fields: primary_diagram"
            ):
                harness.load_reviewed_run(path)

    def test_diagram_python_is_required_for_bundle_but_not_primary_success(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            del run["artifacts"]["diagram_python"]
            path = self.write_run(directory, run)
            score = harness.score_run_file(path)

        self.assertTrue(score["primary_success"])
        self.assertFalse(score["metrics"]["diagram_grill_bundle_complete"])

    def test_process_failures_do_not_change_primary_success(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            run["adjudication"]["approval_requested_before_write"] = False
            run["adjudication"]["files_written_before_approval"] = True
            del run["artifacts"]["architecture_brief"]
            path = self.write_run(directory, run)
            score = harness.score_run_file(path)

        self.assertFalse(score["metrics"]["approval_fidelity"])
        self.assertTrue(score["primary_success"])

    def test_unknown_oracle_ids_and_missing_raw_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            run["adjudication"]["evidence_fact_ids"] = ["invented-fact"]
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(harness.EvaluationError, "absent from the oracle"):
                harness.load_reviewed_run(path)

            run = reviewed_run()
            path = self.write_run(directory, run)
            (Path(directory) / "transcript.txt").unlink()
            with self.assertRaisesRegex(harness.EvaluationError, "artifact is missing"):
                harness.load_reviewed_run(path)

    def test_artifact_content_is_bound_by_size_and_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_run(directory, reviewed_run())
            diagram = Path(directory) / "diagram.svg"
            diagram.write_bytes(b"Z" * diagram.stat().st_size)

            with self.assertRaisesRegex(harness.EvaluationError, "SHA256 does not match"):
                harness.load_reviewed_run(path)

    def test_execution_fingerprint_and_protocol_hashes_are_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            run["execution_fingerprint"] = "f" * 64
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(
                harness.EvaluationError, "execution fingerprint does not match"
            ):
                harness.load_reviewed_run(path)

            run = reviewed_run()
            run["protocol_hashes"]["base_prompt_sha256"] = "f" * 64
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(harness.EvaluationError, "protocol hashes do not match"):
                harness.load_reviewed_run(path)

    def test_partial_protocol_withholds_aggregate_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_run(directory, reviewed_run())
            result = harness.aggregate_run_files([path], "prelaunch-24")

        self.assertFalse(result["protocol_complete"])
        self.assertFalse(result["claim_ready"])
        self.assertIsNone(result["aggregate_metrics"])
        self.assertEqual(24, result["expected_runs"])
        self.assertEqual(1, result["received_runs"])

    def test_complete_matrix_claim_gate_rejects_mixed_fingerprints(self):
        def change_one_execution(run, ordinal):
            if ordinal == 23:
                run["execution"]["model_version"] = "different-requested-model"
                run["execution_fingerprint"] = harness.compute_execution_fingerprint(
                    run["execution"]
                )

        with tempfile.TemporaryDirectory() as directory:
            paths = self.write_matrix(directory, change_one_execution)
            result = harness.aggregate_run_files(paths, "prelaunch-24")

        self.assertTrue(result["protocol_complete"])
        self.assertFalse(result["one_execution_fingerprint"])
        self.assertFalse(result["claim_ready"])
        self.assertIn("mixed-execution-fingerprints", result["claim_blockers"])
        self.assertIsNotNone(result["aggregate_metrics"])

    def test_complete_matrix_claim_gate_rejects_integrity_failure(self):
        def add_one_violation(run, ordinal):
            if ordinal == 23:
                run["integrity"]["session_violations"] = ["session was reused"]
                run["integrity"]["teardown_succeeded"] = False

        with tempfile.TemporaryDirectory() as directory:
            paths = self.write_matrix(directory, add_one_violation)
            result = harness.aggregate_run_files(paths, "prelaunch-24")

        self.assertTrue(result["protocol_complete"])
        self.assertTrue(result["one_execution_fingerprint"])
        self.assertFalse(result["integrity_ready"])
        self.assertFalse(result["claim_ready"])
        self.assertIn("integrity-failures", result["claim_blockers"])

    def test_complete_clean_matrix_is_claim_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.write_matrix(directory)
            result = harness.aggregate_run_files(paths, "prelaunch-24")

        self.assertTrue(result["claim_ready"])
        self.assertEqual([], result["claim_blockers"])
        self.assertEqual(
            12, result["aggregate_metrics"]["baseline"]["primary_successes"]
        )
        self.assertIsNone(
            result["aggregate_metrics"]["baseline"]["process_metrics"][
                "diagram_grill_bundle_complete_rate"
            ]
        )

    def test_phase_rejects_unplanned_condition_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run(condition="renderer-only")
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(harness.EvaluationError, "is not part"):
                harness.load_reviewed_run(path)

            run = reviewed_run()
            run["repeat"] = 2
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(harness.EvaluationError, "repeat 2"):
                harness.load_reviewed_run(path)


if __name__ == "__main__":
    unittest.main()
