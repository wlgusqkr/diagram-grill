import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import harness  # noqa: E402


def reviewed_run(case_id="01-checkout", condition="diagram-grill"):
    return {
        "schema_version": "1.0",
        "phase": "prelaunch-24",
        "case_id": case_id,
        "condition": condition,
        "repeat": 1,
        "execution": {
            "client": "Codex CLI",
            "client_version": "0.149.1",
            "model": "example-model",
            "model_version": "example-model-2026-08-25",
            "operating_system": "macOS",
            "python_version": "3.14.4",
            "graphviz_version": "15.1.1",
            "diagrams_version": "0.25.1",
            "skill_commit": "a" * 40,
            "tested_at": "2026-08-25T20:00:00+09:00",
        },
        "artifacts": {
            "transcript": "transcript.txt",
            "architecture_brief": "architecture-brief.md",
            "diagram_json": "diagram.json",
            "render_manifest": "render-manifest.json",
            "diagram_svg": "diagram.svg",
            "diagram_png": "diagram.png",
        },
        "adjudication": {
            "reviewer": "reviewer-1",
            "reviewed_at": "2026-08-25T21:00:00+09:00",
            "status": "completed",
            "evidence_fact_ids": [
                "fact-1",
                "fact-2",
                "fact-3",
                "fact-4",
                "fact-5",
            ],
            "critical_decision_ids": ["payment-timing"],
            "topology_node_concepts": [
                "web storefront",
                "Checkout API",
                "Pricing Service",
                "Orders PostgreSQL",
                "Orders queue",
                "Fulfillment Worker",
                "external Shipping Provider",
            ],
            "required_connection_ids": [
                "connection-1",
                "connection-2",
                "connection-3",
                "connection-4",
                "connection-5",
                "connection-6",
            ],
            "unsupported_assumptions": [],
            "architecture_claim_count": 14,
            "question_count": 1,
            "approval_requested_before_write": True,
            "files_written_before_approval": False,
            "render_succeeded": True,
            "visual_qa_performed": True,
            "visual_defects": [],
            "safety_violations": [],
        },
    }


class FixtureValidationTests(unittest.TestCase):
    def test_all_twelve_fixed_fixtures_are_consistent(self):
        self.assertEqual([], harness.validate_fixtures())
        self.assertEqual(12, len(harness.load_case_index()))
        self.assertEqual([], harness.validate_oss_metadata())


class RunScoringTests(unittest.TestCase):
    def write_run(self, directory, run):
        root = Path(directory)
        for artifact_path in run["artifacts"].values():
            path = root / artifact_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raw artifact", encoding="utf-8")
        path = root / "run.json"
        path.write_text(json.dumps(run), encoding="utf-8")
        return path

    def test_complete_reviewed_run_scores_without_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_run(directory, reviewed_run())
            score = harness.score_run_file(path)

        self.assertTrue(score["strict_success"])
        self.assertEqual(1.0, score["metrics"]["critical_ambiguity_coverage"])
        self.assertEqual(1.0, score["metrics"]["required_connection_coverage"])
        self.assertEqual(0, score["metrics"]["unsupported_assumption_count"])
        self.assertNotIn("weighted_score", score)

    def test_unknown_oracle_ids_and_missing_raw_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            run["adjudication"]["evidence_fact_ids"] = ["invented-fact"]
            path = self.write_run(directory, run)
            with self.assertRaisesRegex(harness.EvaluationError, "absent from the oracle"):
                harness.load_reviewed_run(path)

            run = reviewed_run()
            run["artifacts"]["transcript"] = "missing.txt"
            path = self.write_run(directory, run)
            (Path(directory) / "missing.txt").unlink()
            with self.assertRaisesRegex(harness.EvaluationError, "artifact is missing"):
                harness.load_reviewed_run(path)

    def test_claimed_render_and_visual_qa_require_raw_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            run = reviewed_run()
            run["artifacts"] = {"transcript": "transcript.txt"}
            path = self.write_run(directory, run)

            with self.assertRaisesRegex(
                harness.EvaluationError, "missing required artifact fields"
            ):
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
