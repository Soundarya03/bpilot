"""Static validator — deterministic checks after cherry-pick and conflict
resolution.

No LLM inference happens here. The validator has three entry points:

- `validate_file()`: per-file checks (conflict markers, syntax, placeholders).
  Used by the resolver during conflict resolution, with errors fed back to
  the LLM for retries.

- `validate_changes()`: post-cherry-pick validation pass that runs on ALL
  changed files (even clean picks): per-file checks plus dependency lock
  regeneration. No shell-command checks (those live in `run_verification_checks`).

- `run_verification_checks()`: runs the project's format, lint, and
  unit-test commands sourced from the SKILL.md "Verification Checks"
  section (falling back to "Test Commands", then to auto-detected
  defaults). Used by the verification fixer's LLM repair loop.

Per BACKPORT_HELPER_PLAN.md §6 + §6b, the validator runs always — clean
cherry-picks AND after conflict resolution. The verification check
commands (form / lint / unit tests) are run separately so the fixer can
retry them in a bounded loop (max 5 iterations) with LLM-assisted repair.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.skill_loader import SkillSet

# Snap-injected environment variables that must be stripped before spawning
# host project tools (verification checks, lock regeneration). The classic-
# confined snap bundles its own python3.12 + libraries; leaving these in the
# environment makes host binaries load the snap's libraries and break in
# opaque ways. Stripped only when running under snap (SNAP env var present);
# outside the snap a developer's exported PYTHONPATH/VIRTUAL_ENV is legitimate
# and must not be touched.
_SNAP_ENV_VARS = ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")


def build_tool_env() -> dict[str, str] | None:
    """Environment for spawned project tools (verification checks, lock regen).

    Returns None (inherit unchanged) when not running under snap. When running
    as a snap (SNAP env var present), returns a copy of ``os.environ`` with
    snap-injected variables removed, so host binaries never load the snap's
    bundled libraries.

    Inline ``VAR=value`` prefixes in a skill command (e.g.
    ``PYTHONPATH=src:lib poetry run pytest``) set that variable via the shell
    for that command only, so scrubbing the parent's snap-injected value does
    not interfere.
    """
    if "SNAP" not in os.environ:
        return None
    env = dict(os.environ)
    for var in _SNAP_ENV_VARS:
        env.pop(var, None)
    return env


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
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    @property
    def failed_files(self) -> list[str]:
        return [r.path for r in self.file_results if not r.ok]


@dataclass
class CommandResult:
    """Outcome of running a single static-check command."""

    command: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr, stripped."""
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass
class VerificationResult:
    """Outcome of running all verification check commands once."""

    ok: bool = True
    commands: list[CommandResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CommandResult]:
        return [c for c in self.commands if not c.ok]

    @property
    def failure_summary(self) -> str:
        """Human-readable summary of failing commands + their output."""
        if self.ok:
            return ""
        lines: list[str] = []
        for f in self.failures:
            lines.append(f"$ {f.command} (exit {f.returncode})")
            out = f.output
            if out:
                lines.append(out)
            lines.append("")
        return "\n".join(lines).strip()


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
    skill_set: SkillSet | None = None,
) -> RepoValidationResult:
    """Post-cherry-pick per-file validation pass. Runs on ALL changed files.

    This is the main entry point for the CLI after all cherry-picks
    complete (even clean ones). It runs:

    1. Per-file checks (conflict markers, syntax, placeholders) on every
       changed file.
    2. Regenerates dependency lock files if pyproject.toml etc. were
       modified.

    It does NOT run the project's format/lint/unit-test commands — those
    live in `run_verification_checks()`, which the verification fixer
    invokes in its LLM repair loop.

    Failures are collected but don't abort the backport — they're
    surfaced in the report for the user to address.

    `skill_set` is accepted for API symmetry with `run_verification_checks`
    but is not currently used by the per-file pass.
    """
    result = RepoValidationResult()

    # 1. Per-file validation.
    result.file_results = validate_files(changed_files, cwd=cwd)
    for fr in result.file_results:
        if not fr.ok:
            for err in fr.errors:
                result.add_error(f"{fr.path}: {err}")

    # 2. Dependency lock regeneration. Compare basenames so monorepo
    # layouts (e.g. `machines/pyproject.toml`) also trigger regen.
    if any(Path(f).name in _DEPENDENCY_FILES or f.endswith(".lock") for f in changed_files):
        _regen_locks(cwd, result, changed_files)

    return result


def run_verification_checks(
    *,
    cwd: Path,
    skill_set: SkillSet | None = None,
) -> VerificationResult:
    """Run the project's format, lint, and unit-test commands once.

    Commands are sourced from (in priority order):
      1. The `verification-checks` skill's "Verification Checks" section
         (falling back to "Test Commands").
      2. Auto-detected defaults for Python projects (ruff check + format).

    Each command runs via the shell (subprocess) and its stdout/stderr +
    exit code are captured. Returns a VerificationResult aggregating all
    commands. Used by the verification fixer's bounded LLM repair loop.
    """
    commands: list[str] = []
    if skill_set is not None:
        verification_skill = skill_set.get("verification-checks")
        if verification_skill is not None:
            commands.extend(verification_skill.verification_checks)

    # Auto-detect ruff if no skill commands.
    if not commands and (cwd / "pyproject.toml").is_file() and _has_command("ruff"):
        commands.extend(["ruff check src/ tests/", "ruff format --check src/ tests/"])

    result = VerificationResult()
    for cmd_str in commands:
        print(f"  running: {cmd_str} ...")
        proc = subprocess.run(
            cmd_str,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            env=build_tool_env(),
        )
        cr = CommandResult(
            command=cmd_str,
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
        result.commands.append(cr)
        if not cr.ok:
            result.ok = False
    return result


def _regen_locks(cwd: Path, result: RepoValidationResult, changed_files: list[str]) -> None:
    """Regenerate dependency lock files if the project uses a known tool.

    Detects the tool from lock files present in the repo and runs the
    regeneration command in each directory that holds one — the repo root
    plus every directory containing a changed dependency/lock file, so
    monorepo layouts (e.g. `machines/poetry.lock`) are covered:
    - poetry.lock -> `poetry lock` (Poetry 2.x locks without updating by
      default; the old `--no-update` flag no longer exists)
    - uv.lock -> `uv lock`
    - package-lock.json -> `npm install --package-lock-only`
    - Cargo.lock -> `cargo generate-lockfile`
    - go.sum -> `go mod tidy`

    Failures are non-fatal (collected as warnings).
    """
    candidates: list[Path] = [cwd]
    for f in changed_files:
        parent = (cwd / f).parent
        if parent != cwd and parent.is_dir() and parent not in candidates:
            candidates.append(parent)

    for d in candidates:
        lock_commands: list[tuple[str, list[str]]] = []

        if (d / "poetry.lock").is_file() and _has_command("poetry"):
            lock_commands.append(("poetry", ["poetry", "lock"]))
        elif (d / "uv.lock").is_file() and _has_command("uv"):
            lock_commands.append(("uv", ["uv", "lock"]))
        elif (d / "package-lock.json").is_file() and _has_command("npm"):
            lock_commands.append(("npm", ["npm", "install", "--package-lock-only"]))
        elif (d / "Cargo.lock").is_file() and _has_command("cargo"):
            lock_commands.append(("cargo", ["cargo", "generate-lockfile"]))
        elif (d / "go.sum").is_file() and _has_command("go"):
            lock_commands.append(("go", ["go", "mod", "tidy"]))

        for name, cmd in lock_commands:
            rel = "." if d == cwd else str(d.relative_to(cwd))
            print(f"  regenerating lock file via {name} ({rel}) ...")
            proc = subprocess.run(
                cmd,
                cwd=d,
                capture_output=True,
                text=True,
                check=False,
                env=build_tool_env(),
            )
            cmd_str = " ".join(cmd)
            result.lock_regen.append(cmd_str if d == cwd else f"{cmd_str} (in {rel})")
            if proc.returncode != 0:
                result.add_error(
                    f"lock regeneration failed ({cmd_str} in {rel}): {proc.stderr.strip()}"
                )


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
