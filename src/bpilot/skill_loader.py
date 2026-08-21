"""Skill loader — parse a directory of `bpilot/skills/<name>/SKILL.md` files.

Each skill lives in its own subdirectory under `bpilot/skills/` and
contains a `SKILL.md` file that conforms to the [Agent Skills
specification](https://agentskills.io/specification): YAML frontmatter
(required `name` matching the parent directory, and `description`) followed
by a markdown body of `## Section` headers.

The loader parses each file into a `SkillFile` (frontmatter + named
sections) and aggregates them into a `SkillSet`. Each bpilot task
loads only the skill(s) it needs by name, instead of parsing one large
SKILL.md and selecting sections — see SKILLS_DIRECTORY_SPEC.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Default location when --skills-dir is not specified.
DEFAULT_SKILLS_DIR = Path("bpilot/skills")

# Canonical section names used by the resolver and gap analyzer.
SECTION_BRANCH_CONVENTIONS = "Branch Conventions"
SECTION_LIFECYCLE_HOOKS = "Lifecycle Hooks"
SECTION_UPGRADE_PATH = "Upgrade Path"
SECTION_THINGS_TO_CHECK = "Things to Check When Backporting"
SECTION_VERIFICATION_CHECKS = "Verification Checks"
SECTION_TEST_COMMANDS = "Test Commands"  # legacy alias for Verification Checks
SECTION_KNOWN_DIVERGENCES = "Known Divergences Between Branches"
SECTION_FILES_OF_INTEREST = "Files of Interest"
SECTION_SKIP_FILES = "Skip Files"

# The fixed set of skills that first-run init scaffolds. Future skills
# are added by amending the spec, not by dropping a new directory.
KNOWN_SKILL_NAMES = (
    "version-control",
    "verification-checks",
    "conflict-resolution",
    "gap-analysis",
    "general-context",
)

# The shared skill appended to every task's context by `SkillSet.context_for`.
GENERAL_CONTEXT = "general-context"

_CHECKLIST_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.*)", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Constraints from the Agent Skills spec for the `name` field.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1024


class SkillLoadError(ValueError):
    """Raised when a SKILL.md has invalid frontmatter or a name/directory mismatch."""


@dataclass
class SkillFile:
    """One parsed SKILL.md — frontmatter + sectioned body.

    `name` and `description` come from the YAML frontmatter (required by
    the Agent Skills spec). `name` must equal the parent directory name;
    the loader validates this and raises `SkillLoadError` on mismatch.
    """

    name: str
    description: str
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
    def is_empty(self) -> bool:
        """True when the body has no usable content.

        A freshly-scaffolded skill file has valid frontmatter but empty
        section bodies (only placeholder HTML comments / whitespace).
        Such a file contributes nothing to an LLM prompt — passing
        placeholder stubs to the model wastes tokens and risks the LLM
        treating scaffolding cruft as instruction. Consumers MUST skip
        empty skills when building LLM context (see "Empty skills are
        skipped" in SKILLS_DIRECTORY_SPEC.md).

        HTML comments do NOT count as content. The starter templates
        use `<!-- ... -->` placeholders inside otherwise-empty section
        bodies, so a freshly-scaffolded file must be reported as empty.
        """
        bodies = (_HTML_COMMENT_RE.sub("", b) for b in self.sections.values())
        return not any(b.strip() for b in bodies)

    @property
    def checklist_items(self) -> list[str]:
        """Extract the numbered items from 'Things to Check When Backporting'.

        Returns a list of item texts (without the leading number), in order.
        Used by the gap analyzer to drive per-item LLM queries.

        HTML comments are stripped before parsing, so the starter
        template's `<!-- 1. If the original PR ... -->` placeholder
        contributes no items on a freshly-scaffolded skill.
        """
        section = self.get_section(SECTION_THINGS_TO_CHECK)
        if not section:
            return []
        section = _HTML_COMMENT_RE.sub("", section)
        items: list[str] = []
        for match in _CHECKLIST_ITEM_RE.finditer(section):
            items.append(match.group(2).strip())
        return items

    @property
    def verification_checks(self) -> list[str]:
        """Extract shell commands from the 'Verification Checks' section.

        Holds the format, lint, and unit-test commands the tool runs after
        a backport (with an LLM-assisted repair loop on failure). Falls back
        to the legacy 'Test Commands' section when 'Verification Checks'
        is absent, so existing skill files keep working.

        Each non-empty line is parsed for a backtick-wrapped command or, if
        no backticks are present, the whole line (after stripping list
        markers) is taken as a command. Lines that are plain prose with no
        recognisable tool name are skipped.

        HTML comments are stripped before parsing, so the starter
        template's `<!-- e.g. Format: `ruff format ...` -->` placeholders
        contribute no commands on a freshly-scaffolded skill.
        """
        commands = _extract_commands(self.get_section(SECTION_VERIFICATION_CHECKS))
        if commands:
            return commands
        return _extract_commands(self.get_section(SECTION_TEST_COMMANDS))

    @property
    def test_commands(self) -> list[str]:
        """Legacy alias for `verification_checks`.

        Returns the same list as `verification_checks` (Verification Checks
        section, falling back to Test Commands). Kept for backward
        compatibility with callers that predate the Verification Checks
        rename.
        """
        return self.verification_checks

    @property
    def skip_files(self) -> list[str]:
        """Extract glob patterns from the 'Skip Files' section.

        Each line is a glob pattern (e.g. `poetry.lock`, `*.lock`,
        `package-lock.json`). The resolver skips conflict resolution for
        matching files, taking the target branch's version and leaving
        regeneration to the user.

        HTML comments are stripped before parsing, so the starter
        template's `<!-- e.g. poetry.lock, *.lock, ... -->` placeholder
        contributes no patterns on a freshly-scaffolded skill.
        """
        section = self.get_section(SECTION_SKIP_FILES)
        if not section:
            return []
        section = _HTML_COMMENT_RE.sub("", section)
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


@dataclass
class SkillSet:
    """All loaded skills for a repo."""

    skills_dir: Path
    skills: dict[str, SkillFile] = field(default_factory=dict)

    def get(self, name: str) -> SkillFile | None:
        """Return the named skill, or None if absent."""
        return self.skills.get(name)

    def general_context(self) -> SkillFile | None:
        """Shortcut for the shared `general-context` skill."""
        return self.get(GENERAL_CONTEXT)

    def context_for(self, *names: str) -> str:
        """Concatenate the body content of the named skills AND
        `general-context`, skipping any skill that is empty or absent.

        `general-context` is always included (appended after the named
        skills) because every task reads shared context in addition to
        its own skill — callers do not pass it explicitly. Passing
        `context_for("conflict-resolution")` therefore concatenates
        `conflict-resolution` then `general-context`.

        Each included skill's body is prefixed with a `## <skill-name>`
        header (the directory name) so the LLM can tell which skill a
        block of context came from. Returns "" if every skill (named
        ones and general-context) is empty or missing, so the LLM
        prompt simply omits the skill-context block (no placeholder
        text, no bare header).
        """
        ordered: list[str] = list(names)
        if GENERAL_CONTEXT not in ordered:
            ordered.append(GENERAL_CONTEXT)

        blocks: list[str] = []
        for skill_name in ordered:
            skill = self.get(skill_name)
            if skill is None or skill.is_empty:
                continue
            # Build the body from non-empty sections, stripping HTML
            # comment placeholders so scaffolding stubs never reach the
            # LLM. A section whose only content was a `<!-- ... -->`
            # placeholder is dropped entirely.
            parts: list[str] = []
            for section_name in skill.sections:
                body = skill.get_section(section_name)
                body = _HTML_COMMENT_RE.sub("", body).strip()
                if body:
                    parts.append(f"## {section_name}\n{body}")
            if not parts:
                continue
            blocks.append(f"## {skill_name}\n" + "\n\n".join(parts))
        return "\n\n".join(blocks)


def _extract_commands(section: str) -> list[str]:
    """Parse a section body into a list of shell commands.

    Each non-empty line is considered. After stripping leading list
    markers (`- ` or `* `):
      - If the line contains a backtick-wrapped command, the content of
        the first backtick pair is taken as the command.
      - Otherwise, if the line names a known tool, the whole line is taken
        as the command.
      - Pure-prose lines (no backticks, no known tool) are skipped.

    HTML comments are stripped before parsing, so the starter template's
    `<!-- e.g. Format: `ruff format ...` -->` placeholders contribute no
    commands on a freshly-scaffolded skill.
    """
    if not section:
        return []
    section = _HTML_COMMENT_RE.sub("", section)
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


def load_skill_file(path: Path) -> SkillFile:
    """Parse a single SKILL.md (frontmatter + sections).

    Module-internal helper used by `load_skill_set`; tests may import it
    directly. It is not part of the public API surface and carries no
    stability guarantee, but it is the natural seam for unit-testing
    frontmatter + section parsing without a directory fixture.

    Validates that the frontmatter has a `name` matching the parent
    directory name and a non-empty `description`. Raises `SkillLoadError`
    on any violation.
    """
    text = path.read_text(errors="replace")
    frontmatter, body = _split_frontmatter(path, text)
    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not isinstance(name, str):
        raise SkillLoadError(f"{path}: frontmatter is missing required 'name' field")
    if not description or not isinstance(description, str):
        raise SkillLoadError(f"{path}: frontmatter is missing required 'description' field")
    _validate_name(path, name)
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise SkillLoadError(f"{path}: 'description' exceeds {_DESCRIPTION_MAX_LEN} characters")

    parent_name = path.parent.name
    if name != parent_name:
        raise SkillLoadError(
            f"{path}: frontmatter 'name' ({name!r}) must match parent "
            f"directory name ({parent_name!r})"
        )

    sections = _parse_sections(body)
    return SkillFile(name=name, description=description, path=path, sections=sections)


def load_skill_set(skills_dir: Path) -> SkillSet | None:
    """Load `bpilot/skills/` — returns None if the directory is absent.

    Raises `SkillLoadError` if a present SKILL.md has invalid frontmatter
    or a name/directory mismatch.

    The tool degrades gracefully only when the *directory* is absent:
    with no skills directory, LLM prompts run without repo-specific
    context and gap analysis is skipped (same as today's behavior when
    SKILL.md is absent). A present-but-malformed skill file is a hard
    error, not graceful degradation.
    """
    if not skills_dir.is_dir():
        return None
    skills: dict[str, SkillFile] = {}
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_path = child / "SKILL.md"
        if not skill_path.is_file():
            # Missing individual skill files are a human-maintenance
            # concern — treat that skill as absent.
            continue
        skill = load_skill_file(skill_path)
        skills[skill.name] = skill
    return SkillSet(skills_dir=skills_dir, skills=skills)


def init_skills_dir(skills_dir: Path) -> Path:
    """Scaffold `bpilot/skills/` with the five starter SKILL.md files.

    Called by the CLI's `port` command on first run when the skills
    directory does not exist (unless --no-init is given). Creates the
    directory and writes the starter templates. Returns the path to the
    created directory. Idempotent in the sense that it only runs when
    the directory is absent; it never overwrites an existing directory.
    Raises `FileExistsError` if called on an existing directory — the
    CLI-level check guards this and the helper asserts its precondition.
    """
    if skills_dir.exists():
        raise FileExistsError(f"skills directory already exists: {skills_dir}")
    skills_dir.mkdir(parents=True, exist_ok=False)
    for name in KNOWN_SKILL_NAMES:
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_text(_STARTER_TEMPLATES[name])
    return skills_dir


def _split_frontmatter(path: Path, text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter_dict, body_text).

    The file must begin with a YAML frontmatter block delimited by `---`
    lines. Returns ({}, body) when no frontmatter is present — but the
    caller validates required fields, so a missing block surfaces as a
    `SkillLoadError` for the missing `name`.
    """
    if not text.startswith("---"):
        # No frontmatter — required fields will be reported as missing.
        return {}, text
    # Find the closing `---` of the frontmatter block.
    rest = text[3:]
    # Skip a leading newline after the opening `---`.
    if rest.startswith("\n"):
        rest = rest[1:]
    end_idx = rest.find("\n---")
    if end_idx == -1:
        raise SkillLoadError(f"{path}: frontmatter is not closed by a '---' line")
    front_text = rest[:end_idx]
    # Body starts after the closing `---` line.
    body_start = end_idx + 4  # length of "\n---"
    body = rest[body_start:]
    if body.startswith("\n"):
        body = body[1:]
    try:
        frontmatter = yaml.safe_load(front_text) or {}
    except yaml.YAMLError as err:
        raise SkillLoadError(f"{path}: invalid YAML frontmatter: {err}") from err
    if not isinstance(frontmatter, dict):
        raise SkillLoadError(f"{path}: frontmatter must be a YAML mapping")
    return frontmatter, body


def _validate_name(path: Path, name: str) -> None:
    """Validate the frontmatter `name` against the Agent Skills constraints."""
    if len(name) > _NAME_MAX_LEN:
        raise SkillLoadError(f"{path}: 'name' exceeds {_NAME_MAX_LEN} characters")
    if not _NAME_RE.match(name):
        raise SkillLoadError(
            f"{path}: 'name' must be lowercase a-z, 0-9, hyphen-separated "
            f"(no leading/trailing/consecutive hyphens); got {name!r}"
        )


def _parse_sections(text: str) -> dict[str, str]:
    """Split markdown body into a dict of {section_name: body_text}.

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

    return sections


# Starter templates written by `init_skills_dir` (valid frontmatter +
# empty section bodies containing only placeholder HTML comments).
_STARTER_TEMPLATES: dict[str, str] = {
    "version-control": (
        "---\n"
        "name: version-control\n"
        "description: Repo-specific branch and commit-message conventions for "
        "backports. Used by bpilot to name backport branches and to validate "
        "commit message style when porting.\n"
        "---\n"
        "## Branch Conventions\n"
        "<!-- e.g. `main` / `edge`: active development. `8.4/edge`: release branch. -->\n"
        "\n"
        "## Commit Conventions\n"
        "<!-- e.g. conventional-commits, ticket prefixes, sign-off requirements. -->\n"
    ),
    "verification-checks": (
        "---\n"
        "name: verification-checks\n"
        "description: Format, lint, and unit-test commands to run after a backport. "
        "Used by bpilot to verify a ported change and to drive the LLM repair loop "
        "on failure.\n"
        "---\n"
        "## Verification Checks\n"
        "<!-- One command per line, backtick-wrapped. -->\n"
        "<!-- e.g. Format: `ruff format src/ tests/` -->\n"
        "<!-- e.g. Lint:   `ruff check src/ tests/` -->\n"
        "<!-- e.g. Tests:  `PYTHONPATH=src poetry run pytest tests/unit/ -q` -->\n"
        "\n"
        "## Test Commands\n"
        "<!-- Legacy alias for Verification Checks; used only if the section above is empty. -->\n"
    ),
    "conflict-resolution": (
        "---\n"
        "name: conflict-resolution\n"
        "description: Known branch divergences and project-specific rules for "
        "resolving cherry-pick conflicts. Used by bpilot's resolver when a port "
        "produces a conflict.\n"
        "---\n"
        "## Skip Files\n"
        "<!-- Glob patterns to skip during conflict resolution (target version taken instead). -->\n"
        "<!-- e.g. poetry.lock, *.lock, package-lock.json, Cargo.lock, go.sum -->\n"
        "\n"
        "## Merge Conflict Resolution Rules\n"
        "<!-- Project-specific guidance the LLM should follow when resolving conflicts. -->\n"
    ),
    "gap-analysis": (
        "---\n"
        "name: gap-analysis\n"
        "description: Checklist of things to verify when backporting (lifecycle "
        "hooks, upgrade paths, parity between flavours). Used by bpilot's gap "
        "analyzer to drive per-item checks.\n"
        "---\n"
        "## Things to Check When Backporting\n"
        "<!-- Numbered list; each item becomes one targeted LLM query. -->\n"
        "<!-- 1. If the original PR added behaviour to X, check whether Y also needs it. -->\n"
    ),
    "general-context": (
        "---\n"
        "name: general-context\n"
        "description: Shared repo context read by multiple bpilot tasks \u2014 "
        "lifecycle hooks, upgrade paths, files of interest, known branch "
        "divergences. Loaded alongside each task-specific skill.\n"
        "---\n"
        "## Lifecycle Hooks\n"
        "<!-- e.g. install \u2192 start \u2192 config-changed; start calls workload_initialise. -->\n"
        "\n"
        "## Upgrade Path\n"
        '<!-- e.g. machine charm upgrades defer start; new "enable X by default" changes must also be added to _on_upgrade_granted. -->\n'
        "\n"
        "## Known Divergences Between Branches\n"
        "<!-- e.g. 8.4 workload_initialise has an is_data_dir_initialised() shortcut; 8.0 does not. -->\n"
        "\n"
        "## Files of Interest\n"
        "<!-- e.g. machines/src/charm.py \u2014 main charm logic. -->\n"
    ),
}
