#!/usr/bin/env python3
"""Validate Diagram Grill's portable core and manual-invocation extensions."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPOSITORY_ROOT / "SKILL.md"
OPENAI_CONFIG_PATH = REPOSITORY_ROOT / "agents" / "openai.yaml"
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    ("name", "description", "license", "metadata", "disable-model-invocation")
)


def _frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    return "\n".join(lines[1:closing_index])


def _top_level_values(frontmatter: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in frontmatter.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9-]*):(?:\s*(.*))?", line)
        if match is None:
            raise ValueError("Invalid top-level frontmatter line: {}".format(line))
        values[match.group(1)] = (match.group(2) or "").strip().strip("\"")
    return values


def collect_issues(root: Path = REPOSITORY_ROOT) -> List[str]:
    issues: List[str] = []
    skill_path = root / "SKILL.md"
    config_path = root / "agents" / "openai.yaml"

    try:
        skill_text = skill_path.read_text(encoding="utf-8")
        values = _top_level_values(_frontmatter(skill_text))
    except (OSError, UnicodeError, ValueError) as exc:
        return [str(exc)]

    unknown_keys = sorted(set(values) - ALLOWED_TOP_LEVEL_KEYS)
    if unknown_keys:
        issues.append("Unknown top-level frontmatter keys: {}".format(", ".join(unknown_keys)))
    if values.get("name") != "diagram-grill":
        issues.append("frontmatter name must be diagram-grill")
    if not values.get("description"):
        issues.append("frontmatter description is required")
    if values.get("disable-model-invocation") != "true":
        issues.append("disable-model-invocation must be true for Claude Code and Cursor")
    if len(skill_text.splitlines()) >= 500:
        issues.append("SKILL.md must stay under 500 lines")

    try:
        openai_config = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(str(exc))
    else:
        if not re.search(r"(?m)^\s+allow_implicit_invocation:\s*false\s*$", openai_config):
            issues.append("agents/openai.yaml must disable implicit invocation")

    referenced_paths = re.findall(r"\]\((references/[^)]+)\)", skill_text)
    for relative_path in referenced_paths:
        if not (root / relative_path).is_file():
            issues.append("Referenced file does not exist: {}".format(relative_path))
    return issues


def main() -> int:
    issues = collect_issues()
    if issues:
        for issue in issues:
            print("ERROR: {}".format(issue), file=sys.stderr)
        return 1
    print("Skill contract is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
