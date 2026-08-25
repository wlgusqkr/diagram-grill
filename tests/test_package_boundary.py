import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"


class InstallablePackageBoundaryTests(unittest.TestCase):
    def test_runtime_package_has_the_supported_single_skill_layout(self):
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file())
        self.assertTrue((SKILL_ROOT / "agents" / "openai.yaml").is_file())
        self.assertTrue((SKILL_ROOT / "references" / "diagram.schema.json").is_file())
        self.assertTrue((SKILL_ROOT / "scripts" / "diagram_grill_cli.py").is_file())
        self.assertTrue((SKILL_ROOT / "requirements.txt").is_file())
        self.assertTrue((SKILL_ROOT / "LICENSE").is_file())
        self.assertTrue((SKILL_ROOT / "THIRD_PARTY_NOTICES.md").is_file())

    def test_runtime_package_excludes_development_and_oracle_material(self):
        forbidden_names = {
            ".git",
            "evals",
            "fixtures",
            "oracle.json",
            "operator-answers.json",
            "tests",
        }
        packaged_names = {path.name for path in SKILL_ROOT.rglob("*")}
        self.assertTrue(forbidden_names.isdisjoint(packaged_names))

    def test_repository_root_is_not_itself_an_installable_skill(self):
        self.assertFalse((REPOSITORY_ROOT / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
