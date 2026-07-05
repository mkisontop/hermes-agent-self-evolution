"""Tests for skill module loading and parsing."""

import pytest
from pathlib import Path
from evolution.skills.skill_module import find_skill, load_skill, reassemble_skill


SAMPLE_SKILL = """---
name: test-skill
description: A skill for testing things
version: 1.0.0
metadata:
  hermes:
    tags: [testing]
---

# Test Skill — Testing Things

## When to Use
Use this when you need to test things.

## Procedure
1. First, do the thing
2. Then, verify it worked
3. Report results

## Pitfalls
- Don't forget to check edge cases
"""


class TestLoadSkill:
    def test_parses_frontmatter(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SAMPLE_SKILL)
        skill = load_skill(skill_file)

        assert skill["name"] == "test-skill"
        assert skill["description"] == "A skill for testing things"
        assert "version: 1.0.0" in skill["frontmatter"]

    def test_parses_body(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SAMPLE_SKILL)
        skill = load_skill(skill_file)

        assert "# Test Skill" in skill["body"]
        assert "## Procedure" in skill["body"]
        assert "Don't forget" in skill["body"]

    def test_raw_contains_everything(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SAMPLE_SKILL)
        skill = load_skill(skill_file)

        assert skill["raw"] == SAMPLE_SKILL

    def test_path_is_stored(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SAMPLE_SKILL)
        skill = load_skill(skill_file)

        assert skill["path"] == skill_file


def _make_skill(root: Path, dirname: str, name: str) -> Path:
    skill_dir = root / "skills" / "misc" / dirname
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: A {name} skill\n---\n\n# {name}\n")
    return path


class TestFindSkill:
    def test_direct_directory_match(self, tmp_path):
        path = _make_skill(tmp_path, "my-skill", "my-skill")
        assert find_skill("my-skill", tmp_path) == path

    def test_frontmatter_name_match(self, tmp_path):
        path = _make_skill(tmp_path, "some-dir", "actual-name")
        assert find_skill("actual-name", tmp_path) == path

    def test_name_prefix_does_not_match(self, tmp_path):
        """Searching 'git' must NOT match a skill named 'github-code-review' —
        the old substring check returned the wrong skill here."""
        _make_skill(tmp_path, "github-code-review", "github-code-review")
        assert find_skill("git", tmp_path) is None

    def test_exact_name_wins_over_prefix_sibling(self, tmp_path):
        _make_skill(tmp_path, "github-code-review", "github-code-review")
        git_path = _make_skill(tmp_path, "git-dir", "git")
        assert find_skill("git", tmp_path) == git_path

    def test_quoted_frontmatter_name(self, tmp_path):
        skill_dir = tmp_path / "skills" / "quoted-dir"
        skill_dir.mkdir(parents=True)
        path = skill_dir / "SKILL.md"
        path.write_text('---\nname: "quoted-name"\ndescription: x\n---\n\nbody\n')
        assert find_skill("quoted-name", tmp_path) == path

    def test_missing_skills_dir(self, tmp_path):
        assert find_skill("anything", tmp_path) is None


class TestReassembleSkill:
    def test_roundtrip(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SAMPLE_SKILL)
        skill = load_skill(skill_file)

        reassembled = reassemble_skill(skill["frontmatter"], skill["body"])
        assert "---" in reassembled
        assert "name: test-skill" in reassembled
        assert "# Test Skill" in reassembled

    def test_preserves_frontmatter(self):
        frontmatter = "name: my-skill\ndescription: Does stuff"
        body = "# My Skill\nDo the thing."
        result = reassemble_skill(frontmatter, body)

        assert result.startswith("---\n")
        assert "name: my-skill" in result
        assert "# My Skill" in result

    def test_evolved_body_replaces_original(self):
        frontmatter = "name: my-skill\ndescription: Does stuff"
        evolved_body = "# EVOLVED\nNew and improved procedure."
        result = reassemble_skill(frontmatter, evolved_body)

        assert "EVOLVED" in result
        assert "New and improved" in result
