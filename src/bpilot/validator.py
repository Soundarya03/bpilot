"""Static validator — deterministic checks after cherry-pick and conflict
resolution.

No LLM inference happens here. The validator has two entry points:

- `validate_file()`: per-file checks (conflict markers, syntax, placeholders).
  Used by the resolver during conflict resolution, with errors fed back to
  the LLM for retries.

- `validate_changes()`: post-cherry-pick validation pass that runs on ALL
  changed files (even clean picks), plus repo-wide checks:
  1. Per-file checks on every changed file.
  2. Dependency lock regeneration if pyproject.toml etc. were modified.
  3. Linting and formatting (ruff, etc.) if configured in SKILL.md or
     detected from the project's tooling.

Per BACKPORT_HELPER_PLAN.md §6, the validator runs always — clean cherry-
picks AND after conflict resolution.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.skill_loader import SkillFile

# Files with these extensions get language-aware syntax checks.
_PY_EXTENSIONS = {".py"}

# Placeholders that indicate the LLM didn't finish the job.
_PLACEHOLDER_MARKERS = ("TODO", "FIXME", "???", "XXX", "PLACEHOLDER")

# Dependency files that, if changed, should trigger a lock regeneration.
_DEPENDENCY_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
}


@dataclass
class ValidationResult:
    """Outcome of validating a single file."""

    path: str
    ok: bool
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False


@dataclass
class RepoValidationResult:
    """Outcome of the post-cherry-pick validation pass."""

    ok: bool = True
    file_results: list[ValidationResult] = field(default_factory=list)
    lock_regen: list[str] = field(default_factory=list)  # commands run
    lint_results: list[str] = field(default_factory=list)  # output lines
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    @property
    def failed_files(self) -> list[str]:
        return [r.path for r in self.file_results if not r.ok]


def validate_file(path: str, *, cwd: Path) -> ValidationResult:
    """Run per-file static checks on a single file in the working tree.

    Returns a ValidationResult with `ok=True` if all checks pass, or
    `ok=False` with a list of error messages describing each failure.
    """
    result = ValidationResult(path=path, ok=True)
    file_path = cwd / path

    if not file_path.is_file():
        result.add_error(f"file not found: {path}")
        return result

    content = file_path.read_text(errors="replace")

    # 1. Conflict markers.
    if _has_conflict_markers(content):
        result.add_error("file still contains git conflict markers")

    # 2. Placeholder check.
    for marker in _PLACEHOLDER_MARKERS:
        if marker in content:
            for line in content.splitlines():
                stripped = line.lstrip("#").lstrip()
                if stripped.startswith(marker):
                    result.add_error(f"placeholder marker found: {marker}")
                    break

    # 3. Syntax check for Python files.
    if file_path.suffix in _PY_EXTENSIONS:
        syntax_err = _check_python_syntax(file_path)
        if syntax_err:
            result.add_error(f"syntax error: {syntax_err}")

    return result


def validate_files(paths: list[str], *, cwd: Path) -> list[ValidationResult]:
    """Validate multiple files. Returns one result per path."""
    return [validate_file(p, cwd=cwd) for p in paths]


def validate_changes(
    *,
    changed_files: list[str],
    cwd: Path,
    skill: SkillFile | None = None,
) -> RepoValidationResult:
    """Post-cherry-pick validation pass. Runs on ALL changed files.

    This is the main entry point for the CLI after all cherry-picks
    complete (even clean ones). It:

    1. Runs per-file checks (conflict markers, syntax, placeholders) on
       every changed file.
    2. Regenerates dependency lock files if pyproject.toml etc. were
       modified.
    3. Runs linters and formatters if available.

    Failures are collected but don't abort the backport — they're
    surfaced in the report for the user to address.
    """
    result = RepoValidationResult()

    # 1. Per-file validation.
    result.file_results = validate_files(changed_files, cwd=cwd)
    for fr in result.file_results:
        if not fr.ok:
            for err in fr.errors:
                result.add_error(f"{fr.path}: {err}")

    # 2. Dependency lock regeneration.
    if any(f in _DEPENDENCY_FILES or f.endswith(".lock") for f in changed_files):
        _regen_locks(cwd, result)

    # 3. Lint and format.
    _run_linters(cwd, result, skill)

    return result


def _regen_locks(cwd: Path, result: RepoValidationResult) -> None:
    """Regenerate dependency lock files if the project uses a known tool.

    Detects the tool from files present in the repo:
    - poetry.lock -> `poetry lock --no-update`
    - uv.lock -> `uv lock`
    - package-lock.json -> `npm install --package-lock-only`
    - Cargo.lock -> `cargo generate-lockfile`
    - go.sum -> `go mod tidy`

    Failures are non-fatal (collected as warnings).
    """
    lock_commands: list[tuple[str, list[str]]] = []

    if (cwd / "poetry.lock").is_file() and _has_command("poetry"):
        lock_commands.append(("poetry", ["poetry", "lock", "--no-update"]))
    elif (cwd / "uv.lock").is_file() and _has_command("uv"):
        lock_commands.append(("uv", ["uv", "lock"]))
    elif (cwd / "package-lock.json").is_file() and _has_command("npm"):
        lock_commands.append(("npm", ["npm", "install", "--package-lock-only"]))
    elif (cwd / "Cargo.lock").is_file() and _has_command("cargo"):
        lock_commands.append(("cargo", ["cargo", "generate-lockfile"]))
    elif (cwd / "go.sum").is_file() and _has_command("go"):
        lock_commands.append(("go", ["go", "mod", "tidy"]))

    for name, cmd in lock_commands:
        print(f"  regenerating lock file via {name} ...")
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        cmd_str = " ".join(cmd)
        result.lock_regen.append(cmd_str)
        if proc.returncode != 0:
            result.add_error(f"lock regeneration failed ({cmd_str}): {proc.stderr.strip()}")


def _run_linters(cwd: Path, result: RepoValidationResult, skill: SkillFile | None) -> None:
    """Run linters and formatters available in the project.

    Priority:
    1. Commands from SKILL.md "Test Commands" section (if present).
    2. Auto-detected tools (ruff for Python projects).

    Only format checks run here (not unit tests). Failures are non-fatal.
    """
    commands: list[str] = []

    if skill and skill.test_commands:
        commands.extend(skill.test_commands)

    # Auto-detect ruff if no skill commands.
    if not commands and (cwd / "pyproject.toml").is_file() and _has_command("ruff"):
        commands.extend(["ruff check src/ tests/", "ruff format --check src/ tests/"])

    for cmd_str in commands:
        print(f"  running: {cmd_str} ...")
        cmd = cmd_str.split()
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            # Capture the first few lines of output for the report.
            output = (proc.stdout + proc.stderr).strip()
            lines = output.splitlines()[:10]
            for line in lines:
                result.lint_results.append(f"[{cmd_str}] {line}")
            result.add_error(f"check failed: {cmd_str}")
        else:
            result.lint_results.append(f"[{cmd_str}] passed")


def _has_command(cmd: str) -> bool:
    """True when `cmd` is available on PATH."""
    import shutil

    return shutil.which(cmd) is not None


def _has_conflict_markers(text: str) -> bool:
    """True when `text` contains standard git conflict markers.

    `=======` must be on its own line to avoid false positives (e.g.
    markdown horizontal rules).
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<<<<<<<") or stripped.startswith(">>>>>>>"):
            return True
        if stripped == "=======":
            return True
    return False


def _check_python_syntax(file_path: Path) -> str:
    """Run `python -m py_compile` on a file. Returns error string or "".

    Uses the same Python interpreter that bpilot is running under.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        lines = stderr.splitlines()
        for line in reversed(lines):
            if "Error" in line:
                return line.strip()
        return stderr.splitlines()[-1] if stderr else "unknown syntax error"
    return ""
