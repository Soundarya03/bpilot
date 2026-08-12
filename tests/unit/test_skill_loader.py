"""Tests for bpilot.skill_loader — SKILL.md parsing into sections."""

from __future__ import annotations

from pathlib import Path

from bpilot.skill_loader import (
    SECTION_KNOWN_DIVERGENCES,
    load_skill,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_skill.md"


def test_load_skill_parses_sections():
    skill = load_skill(FIXTURE)
    assert skill is not None
    assert "Branch Conventions" in skill.sections
    assert "Lifecycle Hooks" in skill.sections
    assert "Things to Check When Backporting" in skill.sections
    assert "Test Commands" in skill.sections


def test_get_section_returns_content():
    skill = load_skill(FIXTURE)
    assert skill is not None
    content = skill.get_section("Branch Conventions")
    assert "main" in content
    assert "8.4/edge" in content


def test_get_section_missing_returns_empty():
    skill = load_skill(FIXTURE)
    assert skill is not None
    assert skill.get_section("Nonexistent Section") == ""


def test_get_sections_concatenates_multiple():
    skill = load_skill(FIXTURE)
    assert skill is not None
    combined = skill.get_sections(["Branch Conventions", "Lifecycle Hooks"])
    assert "## Branch Conventions" in combined
    assert "## Lifecycle Hooks" in combined


def test_checklist_items_extracted():
    skill = load_skill(FIXTURE)
    assert skill is not None
    items = skill.checklist_items
    assert len(items) == 2
    assert "Hook coverage on machine charm" in items[0]
    assert "K8s vs machine parity" in items[1]


def test_test_commands_extracted():
    skill = load_skill(FIXTURE)
    assert skill is not None
    commands = skill.test_commands
    assert len(commands) == 2
    assert any("pytest" in c for c in commands)
    assert any("ruff" in c for c in commands)


def test_load_skill_missing_file(tmp_path: Path):
    assert load_skill(tmp_path / "nonexistent.md") is None


def test_load_skill_handles_empty_sections(tmp_path: Path):
    path = tmp_path / "skill.md"
    path.write_text("# Title\n\n## Empty Section\n\n## Next Section\ncontent\n")
    skill = load_skill(path)
    assert skill is not None
    assert skill.get_section("Empty Section") == ""
    assert skill.get_section("Next Section") == "content"


def test_checklist_items_empty_when_section_missing(tmp_path: Path):
    path = tmp_path / "skill.md"
    path.write_text("# Title\n\n## Other Section\n\nSome content.\n")
    skill = load_skill(path)
    assert skill is not None
    assert skill.checklist_items == []


def test_known_divergences_section():
    skill = load_skill(FIXTURE)
    assert skill is not None
    divergences = skill.get_section(SECTION_KNOWN_DIVERGENCES)
    assert "is_data_dir_initialised" in divergences
    assert "charmed-stats" in divergences
