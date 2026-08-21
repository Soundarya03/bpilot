"""Tests for bpilot.skill_loader — single-file SKILL.md parsing (frontmatter + sections)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bpilot.skill_loader import (
    SECTION_KNOWN_DIVERGENCES,
    SkillLoadError,
    load_skill_file,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skills"


def _skill_path(name: str) -> Path:
    return FIXTURES / name / "SKILL.md"


def test_load_skill_file_parses_frontmatter_and_sections():
    skill = load_skill_file(_skill_path("version-control"))
    assert skill.name == "version-control"
    assert "branch" in skill.description.lower() or "branch" in skill.description
    assert "Branch Conventions" in skill.sections
    assert "Commit Conventions" in skill.sections


def test_get_section_returns_content():
    skill = load_skill_file(_skill_path("version-control"))
    content = skill.get_section("Branch Conventions")
    assert "main" in content
    assert "8.4/edge" in content


def test_get_section_missing_returns_empty():
    skill = load_skill_file(_skill_path("version-control"))
    assert skill.get_section("Nonexistent Section") == ""


def test_get_sections_concatenates_multiple():
    skill = load_skill_file(_skill_path("general-context"))
    combined = skill.get_sections(["Lifecycle Hooks", "Files of Interest"])
    assert "## Lifecycle Hooks" in combined
    assert "## Files of Interest" in combined


def test_checklist_items_extracted():
    skill = load_skill_file(_skill_path("gap-analysis"))
    items = skill.checklist_items
    assert len(items) == 2
    assert "Hook coverage on machine charm" in items[0]
    assert "K8s vs machine parity" in items[1]


def test_test_commands_alias_mirrors_verification_checks():
    skill = load_skill_file(_skill_path("verification-checks"))
    commands = skill.test_commands
    # "Verification Checks" (3 commands) takes priority over "Test Commands".
    assert len(commands) == 3
    assert any("pytest" in c for c in commands)
    assert any("ruff" in c for c in commands)
    assert skill.test_commands == skill.verification_checks


def test_verification_checks_preferred_over_test_commands():
    skill = load_skill_file(_skill_path("verification-checks"))
    commands = skill.verification_checks
    assert len(commands) == 3
    assert any("format" in c for c in commands)
    assert any("ruff check" in c for c in commands)
    assert any("pytest" in c for c in commands)


def test_verification_checks_falls_back_to_test_commands(tmp_path: Path):
    """When 'Verification Checks' is absent, fall back to 'Test Commands'."""
    skill_dir = tmp_path / "fallback-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: fallback-skill\n"
        "description: A skill with only Test Commands.\n"
        "---\n"
        "# Title\n\n## Test Commands\n- `pytest tests/`\n- `ruff check .`\n"
    )
    skill = load_skill_file(skill_dir / "SKILL.md")
    commands = skill.verification_checks
    assert commands == ["pytest tests/", "ruff check ."]
    assert skill.test_commands == commands


def test_verification_checks_empty_when_neither_section(tmp_path: Path):
    skill_dir = tmp_path / "other-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-skill\ndescription: x\n---\n# Title\n\n## Other\n\nSome content.\n"
    )
    skill = load_skill_file(skill_dir / "SKILL.md")
    assert skill.verification_checks == []


def test_known_divergences_section():
    skill = load_skill_file(_skill_path("general-context"))
    divergences = skill.get_section(SECTION_KNOWN_DIVERGENCES)
    assert "is_data_dir_initialised" in divergences
    assert "charmed-stats" in divergences


def test_skip_files_extracted():
    skill = load_skill_file(_skill_path("conflict-resolution"))
    patterns = skill.skip_files
    assert "poetry.lock" in patterns
    assert "*.lock" in patterns
    assert "go.sum" in patterns


# --- Frontmatter validation ---


def test_frontmatter_name_missing_raises(tmp_path: Path):
    skill_dir = tmp_path / "missing-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\ndescription: x\n---\n## S\nbody\n")
    with pytest.raises(SkillLoadError, match="name"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_description_missing_raises(tmp_path: Path):
    skill_dir = tmp_path / "missing-desc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: missing-desc\n---\n## S\nbody\n")
    with pytest.raises(SkillLoadError, match="description"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_name_directory_mismatch_raises(tmp_path: Path):
    skill_dir = tmp_path / "general-context"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: context\ndescription: x\n---\n## S\nbody\n")
    with pytest.raises(SkillLoadError, match="must match parent directory"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_name_uppercase_raises(tmp_path: Path):
    skill_dir = tmp_path / "BadName"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: BadName\ndescription: x\n---\n## S\nbody\n")
    with pytest.raises(SkillLoadError, match="name"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_name_leading_hyphen_raises(tmp_path: Path):
    skill_dir = tmp_path / "-leading"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: -leading\ndescription: x\n---\n## S\nbody\n")
    with pytest.raises(SkillLoadError, match="name"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_name_consecutive_hyphens_raises(tmp_path: Path):
    skill_dir = tmp_path / "double--hyphen"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: double--hyphen\ndescription: x\n---\n## S\nbody\n"
    )
    with pytest.raises(SkillLoadError, match="name"):
        load_skill_file(skill_dir / "SKILL.md")


def test_frontmatter_optional_fields_parse(tmp_path: Path):
    skill_dir = tmp_path / "with-optional"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: with-optional\n"
        "description: x\n"
        "license: Apache-2.0\n"
        "compatibility: needs git\n"
        "metadata:\n"
        "  author: tester\n"
        "allowed-tools: bash grep\n"
        "---\n## S\nbody\n"
    )
    skill = load_skill_file(skill_dir / "SKILL.md")
    assert skill.name == "with-optional"


def test_load_skill_file_handles_empty_sections(tmp_path: Path):
    skill_dir = tmp_path / "empty-sections"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: empty-sections\ndescription: x\n---\n"
        "# Title\n\n## Empty Section\n\n## Next Section\ncontent\n"
    )
    skill = load_skill_file(skill_dir / "SKILL.md")
    assert skill.get_section("Empty Section") == ""
    assert skill.get_section("Next Section") == "content"
