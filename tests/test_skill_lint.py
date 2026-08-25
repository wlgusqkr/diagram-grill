import importlib.util
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LINTER_PATH = REPOSITORY_ROOT / "scripts" / "lint_skill.py"
MODULE_SPEC = importlib.util.spec_from_file_location("diagram_grill_skill_lint", LINTER_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("Could not load skill linter")
LINTER = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(LINTER)


class SkillLintTests(unittest.TestCase):
    def test_repository_skill_contract_is_valid(self):
        self.assertEqual([], LINTER.collect_issues(REPOSITORY_ROOT))

    def test_manual_invocation_extensions_are_required(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "agents").mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: diagram-grill\ndescription: test\n---\n# Test\n",
                encoding="utf-8",
            )
            (root / "agents" / "openai.yaml").write_text(
                "policy:\n  allow_implicit_invocation: true\n",
                encoding="utf-8",
            )

            issues = LINTER.collect_issues(root)

        self.assertTrue(any("disable-model-invocation" in issue for issue in issues))
        self.assertTrue(any("implicit invocation" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
