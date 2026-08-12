"""SKILL.md loader — parse repo-specific knowledge into structured sections.

A SKILL.md file is plain markdown with `## Section Name` headers. The
gap analyzer and conflict resolver read it to ground LLM prompts in
repo-specific context (lifecycle hooks, branch conventions, known
divergences, a backport checklist, test commands).

This module parses the file into named sections so each LLM call can
receive only the relevant sections — saving tokens and improving focus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Default location when --skill-file is not specified.
DEFAULT_SKILL_PATH = Path("bpilot/SKILL.md")

# Canonical section names used by the resolver and gap analyzer.
SECTION_BRANCH_CONVENTIONS = "Branch Conventions"
SECTION_LIFECYCLE_HOOKS = "Lifecycle Hooks"
SECTION_UPGRADE_PATH = "Upgrade Path"
SECTION_THINGS_TO_CHECK = "Things to Check When Backporting"
SECTION_TEST_COMMANDS = "Test Commands"
SECTION_KNOWN_DIVERGENCES = "Known Divergences Between Branches"
SECTION_FILES_OF_INTEREST = "Files of Interest"
SECTION_SKIP_FILES = "Skip Files"

_CHECKLIST_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.*)", re.MULTILINE)


@dataclass
class SkillFile:
    """Parsed SKILL.md contents."""

    path: Path
    sections: dict[str, str] = field(default_factory=dict)

    def get_section(self, name: str) -> str:
        """Return the text of a section, or empty string if absent."""
        return self.sections.get(name, "")

    def get_sections(self, names: list[str]) -> str:
        """Return the concatenated text of multiple named sections."""
        parts = []
        for name in names:
            text = self.get_section(name)
            if text:
                parts.append(f"## {name}\n{text}")
        return "\n\n".join(parts)

    @property
    def checklist_items(self) -> list[str]:
        """Extract the numbered items from 'Things to Check When Backporting'.

        Returns a list of item texts (without the leading number), in order.
        Used by the gap analyzer to drive per-item LLM queries.
        """
        section = self.get_section(SECTION_THINGS_TO_CHECK)
        if not section:
            return []
        items: list[str] = []
        for match in _CHECKLIST_ITEM_RE.finditer(section):
            items.append(match.group(2).strip())
        return items

    @property
    def test_commands(self) -> list[str]:
        """Extract shell commands from the 'Test Commands' section.

        Each non-empty line starting with a backtick or containing a shell
        command is returned. Lines that are plain prose (no backticks) are
        skipped.
        """
        section = self.get_section(SECTION_TEST_COMMANDS)
        if not section:
            return []
        commands: list[str] = []
        for line in section.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading "- " or "* " list markers.
            if line.startswith(("-", "*")):
                line = line[1:].strip()
            # Extract command from backticks if present.
            if "`" in line:
                backtick = line.split("`")
                if len(backtick) >= 2:
                    commands.append(backtick[1])
                    continue
            # Lines that look like shell commands (contain a known tool).
            if any(cmd in line for cmd in ("pytest", "ruff", "python", "poetry", "tox", "make")):
                commands.append(line)
        return commands

    @property
    def skip_files(self) -> list[str]:
        """Extract glob patterns from the 'Skip Files' section.

        Each line is a glob pattern (e.g. `poetry.lock`, `*.lock`,
        `package-lock.json`). The resolver skips conflict resolution for
        matching files, taking the target branch's version and leaving
        regeneration to the user.
        """
        section = self.get_section(SECTION_SKIP_FILES)
        if not section:
            return []
        patterns: list[str] = []
        for line in section.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading "- " or "* " list markers.
            if line.startswith(("-", "*")):
                line = line[1:].strip()
            if line:
                patterns.append(line)
        return patterns


def load_skill(path: Path) -> SkillFile | None:
    """Parse a SKILL.md file into sections.

    Returns None if the file doesn't exist (the tool degrades gracefully —
    LLM prompts run without repo-specific context, and gap analysis is
    skipped since it's checklist-driven).
    """
    if not path.is_file():
        return None
    text = path.read_text(errors="replace")
    return _parse_sections(path, text)


def _parse_sections(path: Path, text: str) -> SkillFile:
    """Split markdown into a dict of {section_name: body_text}.

    A section starts at a `## Header` line and runs until the next `##`
    or `#` header (or end of file). The `# Title` (h1) is not included
    as a section — it's the document title.
    """
    sections: dict[str, str] = {}
    current_name = ""
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_name:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = line[3:].strip()
            current_lines = []
        elif line.startswith("# ") and current_name:
            # A top-level header ends the current section.
            sections[current_name] = "\n".join(current_lines).strip()
            current_name = ""
            current_lines = []
        elif current_name:
            current_lines.append(line)

    if current_name:
        sections[current_name] = "\n".join(current_lines).strip()

    return SkillFile(path=path, sections=sections)
