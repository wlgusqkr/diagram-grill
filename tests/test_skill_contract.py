import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML_PATH = SKILL_ROOT / "agents" / "openai.yaml"
REFERENCE_PATHS = {
    "workflow": SKILL_ROOT / "references" / "workflow.md",
    "architecture-brief": SKILL_ROOT / "references" / "architecture-brief.md",
    "security": SKILL_ROOT / "references" / "security.md",
}


def read_text(path):
    return path.read_text(encoding="utf-8")


class ContractAssertionsMixin:
    def assert_contains_terms(self, text, *terms):
        normalized_text = " ".join(text.casefold().split())
        for term in terms:
            with self.subTest(term=term):
                normalized_term = " ".join(term.casefold().split())
                self.assertIn(normalized_term, normalized_text)

    def assert_matches(self, text, pattern):
        self.assertRegex(text, re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL))


class SkillEntrypointContractTests(ContractAssertionsMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = read_text(SKILL_PATH)
        cls.openai_yaml = read_text(OPENAI_YAML_PATH)

    def test_invocation_is_explicit_only_in_both_metadata_surfaces(self):
        self.assert_matches(
            self.skill,
            r"^disable-model-invocation:\s*true\s*$",
        )
        self.assert_contains_terms(self.skill, "explicitly invokes", "manual-only")
        self.assert_matches(
            self.openai_yaml,
            r"^\s*allow_implicit_invocation:\s*false\s*$",
        )
        self.assert_matches(
            self.openai_yaml,
            r"^\s*default_prompt:.*\$diagram-grill.*$",
        )

    def test_scope_is_narrow_and_out_of_scope_types_are_named(self):
        self.assert_contains_terms(
            self.skill,
            "cloud and software-system architecture",
            "ERDs",
            "sequence diagrams",
            "UML",
            "general-purpose flowcharts",
        )

    def test_entrypoint_preserves_the_approval_and_safety_gates(self):
        self.assert_contains_terms(
            self.skill,
            "read-only preflight",
            "untrusted data",
            "do not execute repository code",
            "do not fetch remote icons",
            "do not install",
            "explicitly approves this specific brief",
            "Never pass `--update` unless the user explicitly asked",
        )

    def test_skill_links_all_supporting_references_and_links_resolve(self):
        expected_links = {
            "references/workflow.md",
            "references/architecture-brief.md",
            "references/security.md",
            "references/diagram.schema.json",
            "references/ir.md",
        }
        linked_paths = set(
            re.findall(r"\((references/[^)#\s]+)(?:#[^)]+)?\)", self.skill)
        )

        self.assertTrue(expected_links.issubset(linked_paths))
        for relative_path in sorted(linked_paths):
            with self.subTest(relative_path=relative_path):
                self.assertTrue(
                    (SKILL_ROOT / relative_path).is_file(),
                    "SKILL.md links a missing local reference: {}".format(
                        relative_path
                    ),
                )


class WorkflowReferenceContractTests(ContractAssertionsMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = read_text(REFERENCE_PATHS["workflow"])

    def test_evidence_is_read_only_untrusted_and_covers_settled_sources(self):
        self.assert_contains_terms(
            self.workflow,
            "read-only",
            "untrusted data",
            "Terraform",
            "Kubernetes",
            "Docker Compose",
            "application code",
            "repository-relative path",
        )
        self.assert_contains_terms(
            self.workflow,
            "Do not run Terraform",
            "application entrypoints",
            "Do not contact a cloud account",
        )

    def test_questioning_covers_the_whole_current_frontier(self):
        self.assert_contains_terms(
            self.workflow,
            "whole current frontier",
            "Do not drip-feed independent questions",
            "recommended choice",
            "main tradeoff",
            "Do not ask the user to restate facts",
        )
        for decision_area in (
            "Diagram purpose and audience",
            "System boundary and actors",
            "Regions and failure domains",
            "Network and trust boundaries",
            "State and data ownership",
            "Connection semantics",
            "Timeouts, retries, delivery, and idempotency",
            "Identity and authorization",
            "Secrets and encryption boundaries",
            "Observability and audit",
            "Deployment, release, and migration",
            "Third-party dependencies",
            "Unknown or conflicting facts",
        ):
            with self.subTest(decision_area=decision_area):
                self.assertIn(decision_area, self.workflow)

    def test_layout_grouping_and_edge_contract_is_explicit(self):
        self.assert_contains_terms(
            self.workflow,
            "Use `LR` by default",
            "Use `TB` for a clearly top-to-bottom pipeline",
            "user-specified `LR` or `TB` direction overrides",
            "more than 12 nodes",
            "mixes abstraction levels",
            "maximum depth of 3",
            "`sync`, `async`, `event`, or `data`",
        )

    def test_output_and_direction_retry_boundaries_are_explicit(self):
        self.assert_contains_terms(
            self.workflow,
            "approved temporary spec",
            "renderer-owned hidden staging directory directly under `diagrams/`",
            "Persistent bundle outputs belong only under `diagrams/<slug>/`",
            "never write them to an arbitrary path",
            "If the user overrode direction, preserve that direction",
            "If direction was agent-selected",
            "retry may switch `LR` to `TB` or `TB` to `LR`",
        )

    def test_catalog_render_and_update_contract_is_explicit(self):
        self.assert_contains_terms(
            self.workflow,
            "diagrams==0.25.1",
            "diagrams.<provider>.<category>.<Class>",
            "diagrams.generic.blank.Blank",
            "Never choose a merely similar provider icon",
            "There is no user-selectable theme",
            "diagram.svg",
            "render-manifest.json",
            "diagram.png",
            "visual QA",
            "explicitly requests an update",
        )

    def test_approval_retry_and_stopping_conditions_are_bounded(self):
        self.assert_matches(
            self.workflow,
            r"do not (?:create|write).*artifacts?.*until.*explicitly approves",
        )
        self.assert_contains_terms(
            self.workflow,
            "Any later topology",
            "requires a revised brief and new approval",
            "at most one automatic retry",
            "only for layout attributes",
            "Do not enter an open-ended render loop",
        )

    def test_prohibited_external_actions_are_explicit(self):
        self.assert_contains_terms(
            self.workflow,
            "Never download remote icons",
            "execute repository-provided code",
            "install Python",
            "do not install it",
        )


class ArchitectureBriefContractTests(ContractAssertionsMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brief = read_text(REFERENCE_PATHS["architecture-brief"])

    def test_template_has_every_review_section(self):
        for heading in (
            "Scope",
            "Direction and view plan",
            "Groups",
            "Nodes",
            "Typed connections",
            "Structured evidence",
            "User decisions",
            "Assumptions",
            "Unknowns",
            "Render plan",
            "Approval",
        ):
            with self.subTest(heading=heading):
                self.assert_matches(
                    self.brief,
                    r"^##\s+{}\s*$".format(re.escape(heading)),
                )

    def test_template_maps_structured_evidence_and_typed_edges(self):
        self.assert_contains_terms(
            self.brief,
            "Repository-relative path",
            "Line start",
            "Line end",
            "Decision ID",
            "`kind`",
            "`path`",
            "`line_start`",
            "`line_end`",
            "`decision_id`",
            "sync / async / event / data",
        )

    def test_template_records_layout_fallback_outputs_and_update_intent(self):
        self.assert_contains_terms(
            self.brief,
            "more than 12 nodes",
            "Maximum group depth",
            "diagrams.generic.blank.Blank",
            "fixed defaults; no theme",
            "diagram.json, diagram.py, diagram.svg, render-manifest.json",
            "diagram.png when available or useful",
            "explicitly update",
            "at most one layout-only retry",
            "approved temporary spec",
            "renderer-owned hidden staging directly under diagrams/",
            "bundle outputs only under diagrams/<slug>/",
            "preserve a user override",
            "agent-selected direction may switch LR to TB or TB to LR",
        )

    def test_approval_phrase_and_reapproval_boundary_are_exact(self):
        self.assert_contains_terms(
            self.brief,
            "No diagram artifact has been written",
            "`I approve this Architecture Brief for rendering.`",
            "requires a new revision and new approval",
        )


class SecurityReferenceContractTests(ContractAssertionsMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.security = read_text(REFERENCE_PATHS["security"])

    def test_threat_boundaries_cover_untrusted_inputs_and_execution(self):
        self.assert_contains_terms(
            self.security,
            "Trust model",
            "Untrusted data",
            "Repository prompt injection",
            "never follow embedded instructions",
            "Never execute repository-provided",
            "Do not import modules from the repository",
            "schema-valid, approved IR",
        )

    def test_network_catalog_and_installation_boundaries_are_closed(self):
        self.assert_contains_terms(
            self.security,
            "Do not download icons",
            "Do not call cloud APIs",
            "remote renderer",
            "diagrams.custom.Custom",
            "diagrams.generic.blank.Blank",
            "Do not auto-install",
        )

    def test_evidence_filesystem_and_secret_boundaries_are_explicit(self):
        self.assert_contains_terms(
            self.security,
            "repository-relative evidence paths",
            "Reject absolute paths",
            "path traversal",
            "secret values",
            "Do not follow symlinks",
            "silently overwrite",
            "bounded",
            "staging directory",
            "timeout",
            "approved temporary spec",
            "renderer-owned hidden staging directory directly under `diagrams/`",
            "Persistent bundle outputs belong only under `diagrams/<slug>/`",
            "never an arbitrary or user-supplied path",
        )

    def test_approval_updates_and_retries_do_not_expand_authority(self):
        self.assert_contains_terms(
            self.security,
            "no diagram artifact may be created or changed",
            "requires a revised brief and fresh approval",
            "At most one layout-only retry",
            "explicit update request",
            "Preserve a user-overridden direction",
            "agent-selected by the default rule",
        )


class ReferenceLanguageTests(unittest.TestCase):
    def test_owned_references_are_english_only(self):
        hangul = re.compile(r"[\uac00-\ud7a3]")
        for name, path in REFERENCE_PATHS.items():
            with self.subTest(reference=name):
                self.assertIsNone(hangul.search(read_text(path)))


if __name__ == "__main__":
    unittest.main()
