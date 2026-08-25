import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "diagram-grill"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import diagram_grill_cli  # noqa: E402
from diagram_grill.evidence import EvidenceReadError, read_evidence_files  # noqa: E402


class EvidenceReaderTests(unittest.TestCase):
    def test_reads_numbered_utf8_evidence_with_a_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "infra").mkdir()
            (root / "infra" / "main.tf").write_text(
                "resource \"example\" \"main\" {}\nsecond line\n",
                encoding="utf-8",
            )

            result = read_evidence_files(root, ("infra/main.tf",))

            self.assertEqual(
                ("resource \"example\" \"main\" {}", "second line"),
                result[0].lines,
            )
            self.assertEqual(64, len(result[0].sha256))
            self.assertEqual("infra/main.tf", result[0].path)

    def test_rejects_absolute_traversing_windows_and_non_normal_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for unsafe in (
                "/etc/passwd",
                "../secret",
                "infra/../secret",
                "C:\\Windows\\win.ini",
                "infra//main.tf",
            ):
                with self.subTest(path=unsafe):
                    with self.assertRaises(EvidenceReadError):
                        read_evidence_files(root, (unsafe,))

    def test_rejects_symlinked_files_and_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as outside:
                root = Path(directory)
                outside_root = Path(outside)
                (outside_root / "secret.txt").write_text(
                    "secret\n", encoding="utf-8"
                )
                (root / "file-link").symlink_to(outside_root / "secret.txt")
                (root / "dir-link").symlink_to(
                    outside_root, target_is_directory=True
                )

                for unsafe in ("file-link", "dir-link/secret.txt"):
                    with self.subTest(path=unsafe):
                        with self.assertRaisesRegex(EvidenceReadError, "symlink"):
                            read_evidence_files(root, (unsafe,))

    def test_rejects_binary_and_non_utf8_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "binary").write_bytes(b"text\x00more")
            (root / "invalid").write_bytes(b"\xff")

            with self.assertRaisesRegex(EvidenceReadError, "binary"):
                read_evidence_files(root, ("binary",))
            with self.assertRaisesRegex(EvidenceReadError, "UTF-8"):
                read_evidence_files(root, ("invalid",))


class EvidenceCommandTests(unittest.TestCase):
    def test_cli_emits_machine_readable_line_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("one\ntwo\n", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = diagram_grill_cli.main(
                    [
                        "--target-root",
                        str(root),
                        "evidence",
                        "--repo",
                        str(root),
                        "--json",
                        "README.md",
                    ]
                )

            self.assertEqual(0, status)
            self.assertEqual("", stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                [{"number": 1, "text": "one"}, {"number": 2, "text": "two"}],
                payload["files"][0]["lines"],
            )


if __name__ == "__main__":
    unittest.main()
