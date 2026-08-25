import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

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


if __name__ == "__main__":
    unittest.main()
