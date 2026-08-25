import copy
import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from diagram_grill.compiler import EDGE_STYLES, GRAPH_ATTR, compile_spec  # noqa: E402


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "specs" / "valid.json"


def valid_spec():
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def accepting_resolver(import_path):
    return import_path


class CompilerTests(unittest.TestCase):
    def test_compiles_stable_source_with_exact_imports_and_node_ids(self):
        spec = valid_spec()

        first = compile_spec(spec, node_resolver=accepting_resolver)
        second = compile_spec(copy.deepcopy(spec), node_resolver=accepting_resolver)

        self.assertEqual(first, second)
        self.assertEqual(("svg",), first.formats)
        self.assertIn("from diagrams.aws.compute import ECS as _Icon0", first.source)
        self.assertIn("from diagrams.aws.database import RDS as _Icon1", first.source)
        self.assertIn("nodeid='api'", first.source)
        self.assertIn("nodeid='database'", first.source)
        compile(first.source, "diagram.py", "exec")

    def test_emits_three_nested_cluster_levels(self):
        source = compile_spec(
            valid_spec(), node_resolver=accepting_resolver
        ).source

        self.assertIn("        with Cluster(escape('Cloud')", source)
        self.assertIn("            with Cluster(escape('Production')", source)
        self.assertIn("                with Cluster(escape('Services')", source)

    def test_uses_fixed_non_theme_edge_semantics(self):
        spec = valid_spec()
        edge = spec["edges"][0]
        extra_edges = []
        for edge_type in ("sync", "async", "event"):
            item = copy.deepcopy(edge)
            item["id"] = "{}-edge".format(edge_type)
            item["type"] = edge_type
            extra_edges.append(item)
        spec["edges"].extend(extra_edges)

        source = compile_spec(spec, node_resolver=accepting_resolver).source

        for edge_type in EDGE_STYLES:
            self.assertIn("EDGE_STYLES['{}']".format(edge_type), source)
        self.assertNotIn("theme", source.casefold())

    def test_fixed_canvas_stays_readable_without_a_theme(self):
        self.assertEqual("#FFFFFF", GRAPH_ATTR["bgcolor"])
        self.assertEqual("t", GRAPH_ATTR["labelloc"])
        self.assertEqual("l", GRAPH_ATTR["labeljust"])

    def test_generic_fallback_is_visible_and_reported(self):
        spec = valid_spec()
        node = spec["nodes"][0]
        node["icon"] = "diagrams.generic.blank.Blank"
        node["icon_fallback_for"] = "Acme payment appliance"

        compiled = compile_spec(spec, node_resolver=accepting_resolver)

        self.assertIn("[generic icon]", compiled.source)
        self.assertNotIn("[generic: Acme payment appliance]", compiled.source)
        self.assertEqual(1, len(compiled.warnings))
        self.assertIn("Acme payment appliance", compiled.warnings[0])

    def test_resolves_every_unique_icon_exactly_once(self):
        calls = []

        compile_spec(valid_spec(), node_resolver=lambda path: calls.append(path))

        self.assertEqual(
            ["diagrams.aws.compute.ECS", "diagrams.aws.database.RDS"], calls
        )

    def test_every_user_label_is_a_graphviz_escaped_string_literal(self):
        spec = valid_spec()
        payload = (
            '<<TABLE><TR><TD HREF="javascript:alert(1)">click</TD>'
            '</TR></TABLE>>'
        )
        spec["title"] = payload
        spec["groups"][0]["label"] = payload
        spec["nodes"][0]["label"] = payload
        spec["edges"][0]["label"] = payload

        source = compile_spec(spec, node_resolver=accepting_resolver).source

        compile(source, "diagram.py", "exec")
        self.assertIn("from graphviz import escape", source)
        self.assertIn("with Diagram(\n        escape(", source)
        self.assertIn("with Cluster(escape(", source)
        self.assertIn("_Icon0(escape(", source)
        self.assertIn("Edge(label=escape(", source)
        self.assertEqual(4, source.count("escape({})".format(repr(payload))))

    def test_svg_is_mandatory_and_png_is_optional(self):
        spec = valid_spec()

        compiled = compile_spec(
            spec, formats=("png", "svg"), node_resolver=accepting_resolver
        )
        self.assertEqual(("svg", "png"), compiled.formats)
        self.assertIn("outformat=['svg', 'png']", compiled.source)

        with self.assertRaisesRegex(ValueError, "svg output is required"):
            compile_spec(spec, formats=("png",), node_resolver=accepting_resolver)
        with self.assertRaisesRegex(ValueError, "unsupported output format"):
            compile_spec(spec, formats=("svg", "pdf"), node_resolver=accepting_resolver)
        with self.assertRaises(TypeError):
            compile_spec(spec, formats="svg", node_resolver=accepting_resolver)

    def test_unapproved_spec_is_not_compiled(self):
        spec = valid_spec()
        spec["approval"]["status"] = "draft"

        with self.assertRaisesRegex(ValueError, "approved before rendering"):
            compile_spec(spec, node_resolver=accepting_resolver)


if __name__ == "__main__":
    unittest.main()
