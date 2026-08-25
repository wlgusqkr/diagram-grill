import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from diagram_grill.errors import DiagramValidationError  # noqa: E402
from diagram_grill.validation import (  # noqa: E402
    MAX_SPEC_BYTES,
    SCHEMA,
    collect_validation_issues,
    load_and_validate,
    validate_spec,
)
from jsonschema import Draft202012Validator  # noqa: E402


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "specs" / "valid.json"


def valid_spec():
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def issue_codes(spec, mode="draft"):
    return {issue.code for issue in collect_validation_issues(spec, mode=mode)}


class SchemaValidationTests(unittest.TestCase):
    def test_schema_is_draft_2020_12(self):
        Draft202012Validator.check_schema(SCHEMA)
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema",
            SCHEMA["$schema"],
        )

    def test_valid_fixture_passes_render_validation(self):
        spec = load_and_validate(FIXTURE_PATH, mode="render")
        self.assertEqual("checkout-architecture", spec["slug"])

    def test_file_loader_rejects_oversized_specs_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.json"
            oversized.write_bytes(b" " * (MAX_SPEC_BYTES + 1))

            with self.assertRaisesRegex(ValueError, "exceeds the"):
                load_and_validate(oversized, mode="render")

    def test_unknown_fields_are_rejected_at_every_level(self):
        cases = []

        root = valid_spec()
        root["theme"] = "dark"
        cases.append(root)

        node = valid_spec()
        node["nodes"][0]["color"] = "red"
        cases.append(node)

        evidence = valid_spec()
        evidence["nodes"][0]["evidence"][0]["url"] = "https://example.test"
        cases.append(evidence)

        for spec in cases:
            with self.subTest(spec=spec):
                self.assertIn("schema", issue_codes(spec))

    def test_version_direction_status_and_edge_type_are_closed_enums(self):
        mutations = (
            ("version", lambda spec: spec.__setitem__("schema_version", "2.0")),
            ("direction", lambda spec: spec.__setitem__("direction", "RL")),
            ("node status", lambda spec: spec["nodes"][0].__setitem__("status", "guessed")),
            ("edge type", lambda spec: spec["edges"][0].__setitem__("type", "http")),
        )
        for name, mutate in mutations:
            spec = valid_spec()
            mutate(spec)
            with self.subTest(name=name):
                self.assertIn("schema", issue_codes(spec))

    def test_safe_collection_limits_are_enforced(self):
        spec = valid_spec()
        template = spec["nodes"][0]
        spec["nodes"] = []
        for index in range(201):
            node = copy.deepcopy(template)
            node["id"] = "node-{}".format(index)
            spec["nodes"].append(node)
        self.assertIn("schema", issue_codes(spec))


class SemanticValidationTests(unittest.TestCase):
    def assert_has_issue(self, spec, code, mode="draft"):
        issues = collect_validation_issues(spec, mode=mode)
        matching = [issue for issue in issues if issue.code == code]
        self.assertTrue(matching, "Expected {!r} in {}".format(code, issues))
        for issue in matching:
            self.assertTrue(issue.path.startswith("$"))
            self.assertTrue(issue.message)

    def test_duplicate_ids_are_rejected_per_collection(self):
        collections = ("groups", "nodes", "edges", "decisions", "assumptions")
        for collection in collections:
            spec = valid_spec()
            spec[collection].append(copy.deepcopy(spec[collection][0]))
            with self.subTest(collection=collection):
                self.assert_has_issue(spec, "duplicate-id")

    def test_group_parent_must_exist(self):
        spec = valid_spec()
        spec["groups"][1]["parent"] = "missing"
        self.assert_has_issue(spec, "dangling-group")

    def test_node_group_must_exist(self):
        spec = valid_spec()
        spec["nodes"][0]["group"] = "missing"
        self.assert_has_issue(spec, "dangling-group")

    def test_node_may_be_ungrouped(self):
        spec = valid_spec()
        spec["nodes"][0]["group"] = None
        validate_spec(spec, mode="render")

    def test_group_parent_cycles_are_rejected(self):
        spec = valid_spec()
        spec["groups"][0]["parent"] = "services"
        self.assert_has_issue(spec, "group-cycle")

    def test_group_nesting_is_limited_to_three_levels(self):
        spec = valid_spec()
        spec["groups"].append(
            {"id": "workload", "label": "Workload", "parent": "services"}
        )
        self.assert_has_issue(spec, "group-depth")

    def test_edge_endpoints_must_reference_nodes(self):
        for endpoint in ("from", "to"):
            spec = valid_spec()
            spec["edges"][0][endpoint] = "missing"
            with self.subTest(endpoint=endpoint):
                self.assert_has_issue(spec, "dangling-node")

    def test_decision_evidence_must_reference_a_decision(self):
        spec = valid_spec()
        spec["nodes"][1]["evidence"][0]["decision_id"] = "missing"
        self.assert_has_issue(spec, "dangling-decision")

    def test_absolute_and_traversing_evidence_paths_are_rejected(self):
        unsafe_paths = (
            "/etc/passwd",
            "../secret.txt",
            "infra/../../secret.txt",
            "..\\secret.txt",
            "C:\\Users\\name\\secret.txt",
            "C:secret.txt",
            "\\secret.txt",
            "\\\\server\\share\\secret.txt",
        )
        for path in unsafe_paths:
            spec = valid_spec()
            spec["nodes"][0]["evidence"][0]["path"] = path
            with self.subTest(path=path):
                self.assert_has_issue(spec, "unsafe-evidence-path")

    def test_repository_relative_evidence_paths_are_allowed(self):
        spec = valid_spec()
        spec["nodes"][0]["evidence"][0]["path"] = "infra/environments/prod/main.tf"
        validate_spec(spec, mode="render")

    def test_custom_and_non_exact_icon_paths_are_rejected(self):
        cases = (
            ("diagrams.custom.Custom", "custom-icon"),
            ("diagrams.aws.compute", "icon-path"),
            ("os.path.Path", "icon-path"),
        )
        for icon, expected_code in cases:
            spec = valid_spec()
            spec["nodes"][0]["icon"] = icon
            with self.subTest(icon=icon):
                self.assert_has_issue(spec, expected_code)

    def test_verified_generic_import_path_is_allowed(self):
        spec = valid_spec()
        spec["nodes"][0]["icon"] = "diagrams.generic.blank.Blank"
        validate_spec(spec, mode="render")

    def test_generic_fallback_records_what_it_replaces(self):
        spec = valid_spec()
        spec["nodes"][0]["icon"] = "diagrams.generic.blank.Blank"
        spec["nodes"][0]["icon_fallback_for"] = "Acme payment appliance"
        validate_spec(spec, mode="render")

    def test_fallback_metadata_requires_the_generic_icon(self):
        spec = valid_spec()
        spec["nodes"][0]["icon_fallback_for"] = "Acme payment appliance"
        self.assert_has_issue(spec, "fallback-icon")

    def test_evidence_line_range_is_ordered(self):
        spec = valid_spec()
        evidence = spec["nodes"][0]["evidence"][0]
        evidence["line_start"] = 20
        evidence["line_end"] = 10
        self.assert_has_issue(spec, "invalid-line-range")

    def test_evidence_line_end_requires_line_start(self):
        spec = valid_spec()
        evidence = spec["nodes"][0]["evidence"][0]
        del evidence["line_start"]
        self.assert_has_issue(spec, "invalid-line-range")

    def test_draft_mode_allows_pending_approval_and_unresolved_assumptions(self):
        spec = valid_spec()
        spec["approval"]["status"] = "draft"
        spec["assumptions"][0]["status"] = "unresolved"
        validate_spec(spec, mode="draft")

    def test_render_mode_requires_approval_and_resolved_assumptions(self):
        spec = valid_spec()
        spec["approval"]["status"] = "draft"
        spec["assumptions"][0]["status"] = "unresolved"
        codes = issue_codes(spec, mode="render")
        self.assertIn("approval-required", codes)
        self.assertIn("unresolved-assumption", codes)

    def test_invalid_mode_is_a_programmer_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown validation mode"):
            collect_validation_issues(valid_spec(), mode="publish")

    def test_validate_spec_raises_structured_error(self):
        spec = valid_spec()
        spec["edges"][0]["to"] = "missing"
        with self.assertRaises(DiagramValidationError) as raised:
            validate_spec(spec)
        issue = raised.exception.issues[0]
        self.assertIsInstance(issue.code, str)
        self.assertIsInstance(issue.path, str)
        self.assertIsInstance(issue.message, str)


if __name__ == "__main__":
    unittest.main()
