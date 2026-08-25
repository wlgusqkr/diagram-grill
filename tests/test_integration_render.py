import importlib.metadata as importlib_metadata
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from diagram_grill.renderer import build_bundle  # noqa: E402


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "specs" / "valid.json"


class RenderIntegrationTests(unittest.TestCase):
    def test_real_toolchain_builds_reproducible_svg_bundles(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            first = build_bundle(FIXTURE_PATH, Path(first_root))
            second = build_bundle(FIXTURE_PATH, Path(second_root))

            expected_files = {
                "diagram.json",
                "diagram.py",
                "diagram.svg",
                "render-manifest.json",
            }
            self.assertEqual(expected_files, {path.name for path in first.iterdir()})
            self.assertEqual(expected_files, {path.name for path in second.iterdir()})

            for name in expected_files:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

            svg = (first / "diagram.svg").read_text(encoding="utf-8")
            self.assertIn("<svg", svg)
            self.assertIn("Checkout API", svg)
            self.assertIn("Orders", svg)

            manifest = json.loads((first / "render-manifest.json").read_text())
            self.assertEqual("0.25.1", manifest["runtime"]["diagrams"]["actual"])
            self.assertIn("dot", manifest["runtime"])

    def test_html_javascript_payload_is_inert_in_every_label_context(self):
        payload = (
            '<<TABLE><TR><TD HREF="javascript:alert(1)">click</TD>'
            '</TR></TABLE>>'
        )
        with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
            spec = json.load(fixture_file)
        spec["title"] = payload
        spec["groups"][0]["label"] = payload
        spec["nodes"][0]["label"] = payload
        spec["edges"][0]["label"] = payload

        with tempfile.TemporaryDirectory() as input_root:
            spec_path = Path(input_root) / "payload.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            with tempfile.TemporaryDirectory() as first_root:
                first = build_bundle(spec_path, Path(first_root))
                with tempfile.TemporaryDirectory() as second_root:
                    second = build_bundle(spec_path, Path(second_root))

                    for name in (
                        "diagram.json",
                        "diagram.py",
                        "diagram.svg",
                        "render-manifest.json",
                    ):
                        self.assertEqual(
                            (first / name).read_bytes(),
                            (second / name).read_bytes(),
                        )

                    svg = (first / "diagram.svg").read_bytes()
                    lowered = svg.lower()
                    resources_root = Path(
                        importlib_metadata.distribution("diagrams").locate_file(
                            "resources"
                        )
                    ).resolve()
                    self.assertNotIn(b"javascript:", lowered)
                    self.assertNotIn(str(resources_root).encode("utf-8"), svg)
                    self.assertNotIn(str(REPOSITORY_ROOT).encode("utf-8"), svg)
                    self.assertNotIn(b"file://", lowered)
                    self.assertNotIn(b"<script", lowered)
                    self.assertNotIn(b"foreignobject", lowered)

                    root = ElementTree.fromstring(svg)
                    hrefs = []
                    for element in root.iter():
                        for attribute_name, value in element.attrib.items():
                            local_name = attribute_name.rsplit("}", 1)[-1].lower()
                            self.assertFalse(local_name.startswith("on"))
                            if local_name == "href":
                                hrefs.append(value)
                    self.assertEqual(2, len(hrefs))
                    self.assertTrue(
                        all(
                            value.startswith("data:image/png;base64,")
                            for value in hrefs
                        )
                    )
                    self.assertIn("javascript:alert(1)", "".join(root.itertext()))


if __name__ == "__main__":
    unittest.main()
