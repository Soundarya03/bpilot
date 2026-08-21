"""LLM-driven initialization of skill files from repo analysis.

`bpilot init` scaffolds `bpilot/skills/` with the five starter
`SKILL.md` files, then (when LLM is available) refines two of them from
repo content:

- **verification-checks**: inferred from README.md, CONTRIBUTING.md,
  pyproject.toml, and other config files (Makefile, tox.ini,
  package.json, Cargo.toml, go.mod, .pre-commit-config.yaml).
- **version-control**: inferred from README.md plus a deterministic
  git analysis (branch list + recent commit subjects).

The other three skills (conflict-resolution, gap-analysis,
general-context) are left as placeholders — those are learned over time
via `finalize` as the tool is put to use.

Trust boundary (same as the resolver / fixer): the LLM is a pure
text-in/text-out function. It returns markdown section content; we write
it via normal file I/O. It never runs git, never sees credentials, and
never executes anything. Its output is treated as untrusted data: we
strip any frontmatter it might emit and preserve the scaffolded
frontmatter (which has the validated `name` matching the directory).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.git_ops import GitError, list_branches, recent_commit_subjects
from bpilot.llm_client import LLMClient
from bpilot.skill_loader import SkillLoadError, load_skill_file

# Files whose presence suggests a build / test toolchain; we tell the LLM
# which of these exist (not their contents — they're usually noise for
# command inference beyond what README/pyproject already convey).
_PROBE_FILES = (
    "Makefile",
    "makefile",
    "tox.ini",
    "package.json",
    "Cargo.toml",
    "go.mod",
    ".pre-commit-config.yaml",
    "setup.cfg",
    "noxfile.py",
)

# Files we read in full and feed to the LLM for verification-check inference.
_READ_FILES_VERIFICATION = (
    "README.md",
    "README.rst",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "pyproject.toml",
)

# Files we read in full and feed to the LLM for version-control inference.
_READ_FILES_VERSION_CONTROL = ("README.md", "README.rst")

# Cap the size of any one file we feed the LLM, to keep prompts bounded.
_MAX_FILE_CHARS = 4000

# How many branches / commit subjects to surface to the LLM.
_BRANCH_LIMIT = 50
_COMMIT_LIMIT = 50


@dataclass
class InitResult:
    """Outcome of `bpilot init`."""

    skills_dir: Path
    scaffolded: list[str] = field(default_factory=list)
    inferred: dict[str, str] = field(default_factory=dict)
    # skill name -> reason it was skipped (e.g. "no LLM", "no README found").
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def run_init(
    *,
    repo_root: Path,
    skills_dir: Path,
    llm: LLMClient | None,
) -> InitResult:
    """Scaffold the skills directory and refine two skills via LLM.

    `skills_dir` must not already exist — `init_skills_dir` raises
    `FileExistsError` if it does. The caller (CLI) is responsible for the
    "already initialised" guard and message.

    When `llm` is None, only the placeholder scaffold is written; the two
    inference steps are skipped with a "no LLM" reason recorded in the
    result. The run still succeeds — the user gets a concrete, human-
    editable file tree to fill in by hand.
    """
    # Local import to keep the module's top-level surface tight; the
    # CLI imports run_init directly and init_skills_dir transitively.
    from bpilot.skill_loader import init_skills_dir

    init_skills_dir(skills_dir)
    result = InitResult(skills_dir=skills_dir)
    result.scaffolded = [p.parent.name for p in sorted(skills_dir.glob("*/SKILL.md"))]

    if llm is None:
        for name in ("verification-checks", "version-control"):
            result.skipped[name] = "no LLM (re-run with an API key to infer)"
        return result

    _infer_verification_checks(repo_root, skills_dir, llm, result)
    _infer_version_control(repo_root, skills_dir, llm, result)
    return result


def _infer_verification_checks(
    repo_root: Path,
    skills_dir: Path,
    llm: LLMClient,
    result: InitResult,
) -> None:
    """Refine `verification-checks/SKILL.md` from README + config files."""
    gathered = _gather_files(repo_root, _READ_FILES_VERIFICATION)
    probe_present = [name for name in _PROBE_FILES if (repo_root / name).is_file()]
    if not gathered and not probe_present:
        result.skipped["verification-checks"] = "no README / config files found"
        return

    prompt = _build_verification_prompt(gathered, probe_present)
    try:
        response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
    except Exception as err:  # noqa: BLE001 — surface but keep going
        result.errors["verification-checks"] = f"LLM call failed: {err}"
        return

    body = _extract_body(response.text)
    if not body:
        result.errors["verification-checks"] = "LLM returned empty content"
        return

    written = _rewrite_skill_body(skills_dir, "verification-checks", body)
    if written is None:
        result.errors["verification-checks"] = "could not parse scaffolded file"
        return
    result.inferred["verification-checks"] = written


def _infer_version_control(
    repo_root: Path,
    skills_dir: Path,
    llm: LLMClient,
    result: InitResult,
) -> None:
    """Refine `version-control/SKILL.md` from README + git analysis."""
    gathered = _gather_files(repo_root, _READ_FILES_VERSION_CONTROL)
    try:
        branches = list_branches(cwd=repo_root, limit=_BRANCH_LIMIT)
        subjects = recent_commit_subjects(cwd=repo_root, limit=_COMMIT_LIMIT)
    except GitError as err:
        result.errors["version-control"] = f"git analysis failed: {err}"
        return

    if not gathered and not branches and not subjects:
        result.skipped["version-control"] = "no README and no git history"
        return

    prompt = _build_version_control_prompt(gathered, branches, subjects)
    try:
        response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
    except Exception as err:  # noqa: BLE001 — surface but keep going
        result.errors["version-control"] = f"LLM call failed: {err}"
        return

    body = _extract_body(response.text)
    if not body:
        result.errors["version-control"] = "LLM returned empty content"
        return

    written = _rewrite_skill_body(skills_dir, "version-control", body)
    if written is None:
        result.errors["version-control"] = "could not parse scaffolded file"
        return
    result.inferred["version-control"] = written


def _gather_files(repo_root: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read a set of files by name from `repo_root`, capping each at _MAX_FILE_CHARS.

    Missing files are omitted from the result. Read errors are treated as
    "missing" (the LLM gets what it can).
    """
    gathered: dict[str, str] = {}
    for name in names:
        path = repo_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + "\n... (truncated)\n"
        gathered[name] = text
    return gathered


def _build_verification_prompt(files: dict[str, str], probe_files: list[str]) -> str:
    """Construct the LLM prompt for verification-checks inference."""
    parts: list[str] = [
        "You are configuring the verification checks (format, lint, unit "
        "tests) for a software project so that a backport tool can run them "
        "automatically after each cherry-pick.",
        "",
        "Below are files from the project (truncated if large):",
    ]
    for name, content in files.items():
        parts.append(f"\n--- {name} ---\n{content}")
    if probe_files:
        parts.append(
            f"\nOther build/test config files present in the repo: {', '.join(probe_files)}."
        )
    parts.extend(
        [
            "",
            "Based on the project's tech stack and existing tooling, infer the "
            "exact shell commands to run for: (1) formatting, (2) linting, and "
            "(3) unit tests. Use the project's own invocation style (e.g. "
            "`poetry run ...`, `npm run ...`, `make ...`).",
            "",
            "Output the markdown body for the skill file, containing TWO sections in this order:",
            "  ## Verification Checks",
            "    One command per line, backtick-wrapped, with a leading "
            "'- ' list marker and a label prefix (Format:, Lint:, Tests:).",
            "  ## Test Commands",
            "    A legacy alias for Verification Checks; populate it with the "
            "unit-test command only, or leave a placeholder HTML comment if "
            "Verification Checks already covers it.",
            "",
            "Output ONLY the markdown body starting with '## Verification "
            "Checks'. No frontmatter, no code fences, no explanations. If you "
            "cannot infer a command, omit that line rather than guessing.",
        ]
    )
    return "\n".join(parts)


def _build_version_control_prompt(
    files: dict[str, str], branches: list[str], subjects: list[str]
) -> str:
    """Construct the LLM prompt for version-control inference."""
    parts: list[str] = [
        "You are inferring the branch and commit-message conventions for a "
        "software project so that a backport tool can name branches and "
        "validate commit style.",
        "",
    ]
    for name, content in files.items():
        parts.append(f"--- {name} ---\n{content}\n")
    parts.append(f"Branches (local + remote, de-duplicated, up to {_BRANCH_LIMIT}):")
    parts.append("\n".join(f"- {b}" for b in branches) or "(none)")
    parts.append("")
    parts.append(f"Recent commit subjects (up to {_COMMIT_LIMIT}, newest first):")
    parts.append("\n".join(subjects) or "(none)")
    parts.extend(
        [
            "",
            "Based on the README, the branch names, and the commit subjects, "
            "infer the project's conventions. Output the markdown body for the "
            "skill file, containing TWO sections in this order:",
            "  ## Branch Conventions",
            "    A bullet list: the active development branch, any release "
            "branches (e.g. `8.4/edge`), and the backport branch naming "
            "convention (e.g. `backport/<hash>-to-<target>`).",
            "  ## Commit Conventions",
            "    A bullet list of the inferred commit-message style: "
            "conventional-commits prefixes, ticket prefixes (e.g. 'JIRA-123:'), "
            "sign-off requirements, etc. If no clear convention is detectable, "
            "leave a placeholder HTML comment for the human to fill in.",
            "",
            "Output ONLY the markdown body starting with '## Branch "
            "Conventions'. No frontmatter, no code fences, no explanations. "
            "If you cannot infer something, use a placeholder HTML comment "
            "(<!-- e.g. ... -->) rather than guessing.",
        ]
    )
    return "\n".join(parts)


_SYSTEM_PROMPT = (
    "You are an expert at analysing software projects to configure a "
    "backport tool. You output ONLY markdown section content as "
    "instructed — no frontmatter, no code fences, no preamble, no "
    "explanations. You are precise and never invent commands or "
    "conventions that aren't evidenced by the provided files and git "
    "history."
)


def _extract_body(raw: str) -> str:
    """Extract the markdown body from an LLM response.

    The LLM is instructed to output only the body (starting with a `## `
    header). If it wraps the output in a markdown code fence or adds
    preamble, we strip the fence and find the first `## ` line.
    """
    text = raw.strip()
    # Strip a surrounding code fence if present.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop the opening fence line.
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        # Drop the closing fence if it's the last non-empty line.
        while lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines).strip()
    # Find the first `## ` header and take everything from there.
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            return "\n".join(text.splitlines()[i:]).strip() + "\n"
    return ""


def _rewrite_skill_body(skills_dir: Path, name: str, body: str) -> str | None:
    """Replace the body of a scaffolded SKILL.md, preserving its frontmatter.

    Returns the full written file content, or None if the scaffolded file
    could not be parsed (e.g. missing — shouldn't happen post-scaffold).
    """
    skill_path = skills_dir / name / "SKILL.md"
    try:
        existing = load_skill_file(skill_path)
    except SkillLoadError:
        return None
    # Re-emit the frontmatter + the new body. We keep the frontmatter
    # minimal (name + description) to avoid re-emitting optional fields
    # the LLM might have tried to add.
    content = f"---\nname: {existing.name}\ndescription: {existing.description}\n---\n{body}"
    skill_path.write_text(content)
    return content


def ensure_bpilot_dir(repo_root: Path) -> Path:
    """Create the `.bpilot/` session directory and ensure it's gitignored.

    Called by `bpilot init` so the session directory exists and is ignored
    from the start. Idempotent: a no-op if `.bpilot/` already exists, and
    does not duplicate the .gitignore entry.
    """
    bpilot_dir = repo_root / ".bpilot"
    bpilot_dir.mkdir(exist_ok=True)
    gitignore = repo_root / ".gitignore"
    entry = ".bpilot/"
    if gitignore.is_file():
        lines = gitignore.read_text().splitlines()
        if entry not in lines:
            # Append a section header + entry, preserving existing content.
            existing = gitignore.read_text()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            gitignore.write_text(f"{existing}\n# bpilot session state\n{entry}\n")
    else:
        gitignore.write_text(f"# bpilot session state\n{entry}\n")
    return bpilot_dir


def print_summary(result: InitResult, *, stream=None) -> None:
    """Print a human-readable summary of the init run to `stream` (stderr)."""
    if stream is None:
        stream = sys.stderr
    print(f"scaffolded {len(result.scaffolded)} skill(s):", file=stream)
    for name in result.scaffolded:
        print(f"  - {name}/SKILL.md", file=stream)
    if result.inferred:
        print(f"inferred {len(result.inferred)} skill(s) via LLM:", file=stream)
        for name in result.inferred:
            print(f"  - {name}/SKILL.md", file=stream)
    if result.skipped:
        print("skipped skill(s):", file=stream)
        for name, reason in result.skipped.items():
            print(f"  - {name}: {reason}", file=stream)
    if result.errors:
        print("error(s) during inference:", file=stream)
        for name, reason in result.errors.items():
            print(f"  - {name}: {reason}", file=stream)
    print(
        "\nedit bpilot/skills/*/SKILL.md to review and refine, then run "
        "`bpilot port <commits> <target>` to start backporting.",
        file=stream,
    )
