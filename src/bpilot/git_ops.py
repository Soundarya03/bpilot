"""Git operations layer — the only component that touches the repository.

Trust boundary: this module is the sole place that runs `git`. The
conflict resolver and gap analyzer never call git directly; they return
patch text, which `apply_patch()` validates and applies.

We shell out to `git` via `subprocess` rather than using GitPython: the
dependency is heavy, its API drifts between versions, and `git` itself
is well-documented and easy to debug.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Conflict marker status codes from `git status --porcelain`.
# UU = both modified, AA = both added, AU/UA = one side added / other modified,
# DD = both deleted, DU/UD = one side deleted / other modified.
CONFLICT_STATUS_PREFIXES = ("UU", "AA", "AU", "UA", "DD", "DU", "UD")


class GitError(RuntimeError):
    """Raised when a git command fails irrecoverably."""


@dataclass
class CherryPickResult:
    """Outcome of attempting to cherry-pick a single commit.

    `conflicted_files` is populated only when `conflicts` is True.
    """

    commit: str
    applied: bool
    conflicts: bool = False
    conflicted_files: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def clean(self) -> bool:
        """True when the commit applied without conflicts."""
        return self.applied and not self.conflicts


@dataclass
class PatchApplyError(Exception):
    """Raised when a patch fails validation before application."""

    reason: str
    details: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.details}" if self.details else self.reason


@dataclass
class ConflictInfo:
    """A single conflicted file with its marker content for LLM context."""

    path: str
    content: str  # working-tree content with conflict markers


def _run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the completed process.

    All git invocations go through here, which makes auditing trivial.
    """
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def is_git_repo(cwd: Path) -> bool:
    """True when `cwd` is inside a git working tree."""
    proc = _run(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def current_head(cwd: Path) -> str:
    """Return the SHA of HEAD."""
    proc = _run(["rev-parse", "HEAD"], cwd=cwd)
    return proc.stdout.strip()


def current_branch(cwd: Path) -> str:
    """Return the current branch name."""
    proc = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    name = proc.stdout.strip()
    return name or "HEAD"


def fetch_target_branch(branch: str, *, cwd: Path, remote: str = "origin") -> bool:
    """Fetch `branch` from `remote` and create/update a local tracking ref.

    Returns True if a fetch actually happened, False if the remote is
    absent (in which case we fall back to the existing local branch —
    common for local-only repos and integration tests).
    """
    # Probe for the remote first so a missing remote is not a hard error.
    probe = _run(["remote"], cwd=cwd, check=False)
    if probe.returncode != 0 or remote not in probe.stdout.split():
        return False
    _run(["fetch", remote, f"{branch}:{branch}"], cwd=cwd)
    return True


def create_backup_ref(*, cwd: Path, prefix: str = "bpilot/backup") -> str:
    """Create a `bpilot/backup/<timestamp>` ref at current HEAD.

    Returns the backup ref name. Enables manual rollback via
    `git reset --hard <ref>`; there is no dedicated rollback command.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    ref = f"{prefix}/{timestamp}"
    _run(["update-ref", ref, "HEAD"], cwd=cwd)
    return ref


def create_backport_branch(target_branch: str, commits_id: str, *, cwd: Path) -> str:
    """Create `backport/<commits_id>-to-<target>` off of `target_branch`.

    `commits_id` should be a short, filesystem-safe identifier for the
    backported commits (e.g. the short SHA of a single commit, or the
    range with `/` replaced).
    """
    safe_target = target_branch.replace("/", "-")
    safe_commits = commits_id.replace("/", "-")[:12]
    branch = f"backport/{safe_commits}-to-{safe_target}"
    _run(["checkout", "-b", branch, target_branch], cwd=cwd)
    return branch


def is_merge_commit(commit: str, *, cwd: Path) -> bool:
    """True when `commit` has more than one parent."""
    proc = _run(["rev-list", "--parents", "-n", "1", commit], cwd=cwd)
    parts = proc.stdout.strip().split()
    # Output: <commit> <parent1> [<parent2> ...]; merge = 2+ parents.
    return len(parts) > 2


def get_commit_message(commit: str, *, cwd: Path) -> str:
    """Return the subject (first line) of a commit's message."""
    proc = _run(["log", "-1", "--format=%s", commit], cwd=cwd)
    return proc.stdout.strip()


def get_commit_messages(commits: list[str], *, cwd: Path) -> list[tuple[str, str]]:
    """Return (sha, subject) pairs for each commit."""
    return [(c, get_commit_message(c, cwd=cwd)) for c in commits]


def expand_commit_range(spec: str, *, cwd: Path) -> list[str]:
    """Expand a commit-range spec into a list of individual commit SHAs.

    Accepts a single SHA, a `A..B` range (excludes A, includes B), or an
    `A B C` whitespace-separated list. Output is oldest-first.
    """
    spec = spec.strip()
    if ".." in spec:
        proc = _run(["rev-list", "--reverse", spec], cwd=cwd)
        return [line for line in proc.stdout.splitlines() if line]
    # Whitespace-separated list of individual commits.
    parts = spec.split()
    if not parts:
        raise GitError(f"empty commit spec: {spec!r}")
    # Resolve each to a full SHA, preserving order.
    resolved = []
    for p in parts:
        proc = _run(["rev-parse", p], cwd=cwd)
        sha = proc.stdout.strip()
        if not sha:
            raise GitError(f"could not resolve commit: {p!r}")
        resolved.append(sha)
    return resolved


def short_sha(sha: str, *, cwd: Path) -> str:
    """Return the abbreviated SHA for a commit."""
    proc = _run(["rev-parse", "--short", sha], cwd=cwd)
    return proc.stdout.strip()


def cherry_pick(commit: str, *, cwd: Path) -> CherryPickResult:
    """Cherry-pick a single commit onto the current branch.

    Handles merge commits automatically by using `-m 1` (mainline parent).
    On conflict, returns a `CherryPickResult` with `conflicts=True` and
    leaves the cherry-pick in progress for the resolver / caller to handle.
    """
    merge = is_merge_commit(commit, cwd=cwd)
    args = ["cherry-pick"]
    if merge:
        args += ["-m", "1"]
    args.append(commit)

    proc = _run(args, cwd=cwd, check=False)
    if proc.returncode == 0:
        return CherryPickResult(commit=commit, applied=True, message="applied cleanly")

    conflicted = detect_conflicts(cwd=cwd)
    if conflicted:
        return CherryPickResult(
            commit=commit,
            applied=False,
            conflicts=True,
            conflicted_files=conflicted,
            message=f"{len(conflicted)} conflict(s)",
        )

    # Non-conflict failure (e.g. empty commit after pick). Abort to clean state.
    _run(["cherry-pick", "--abort"], cwd=cwd, check=False)
    raise GitError(f"cherry-pick of {commit} failed without conflicts:\n{proc.stderr}")


def abort_cherry_pick(*, cwd: Path) -> None:
    """Abort an in-progress cherry-pick, returning the tree to a clean state."""
    _run(["cherry-pick", "--abort"], cwd=cwd, check=False)


def continue_cherry_pick(*, cwd: Path) -> None:
    """Continue an in-progress cherry-pick after conflicts are resolved.

    Uses GIT_EDITOR=true so git doesn't try to open an interactive editor
    for the commit message (it reuses the original commit's message).
    """
    import os

    env = dict(os.environ)
    env["GIT_EDITOR"] = "true"
    _run(["cherry-pick", "--continue"], cwd=cwd, check=True, env=env)


def checkout_branch(branch: str, *, cwd: Path) -> None:
    """Switch to an existing branch."""
    _run(["checkout", branch], cwd=cwd)


def delete_branch(branch: str, *, cwd: Path, force: bool = False) -> None:
    """Delete a branch. Use force=True for an unmerged branch."""
    flag = "-D" if force else "-d"
    _run(["branch", flag, branch], cwd=cwd)


def has_cherry_pick_in_progress(*, cwd: Path) -> bool:
    """True when there's an in-progress cherry-pick (CHERRY_PICK_HEAD exists)."""
    proc = _run(["rev-parse", "--verify", "CHERRY_PICK_HEAD"], cwd=cwd, check=False)
    return proc.returncode == 0


def detect_conflicts(*, cwd: Path) -> list[str]:
    """Return the list of paths with unresolved conflict markers."""
    proc = _run(["status", "--porcelain"], cwd=cwd)
    conflicted: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        xy = line[:2]
        path = line[3:].strip()
        # `path` may be a rename "old -> new"; take the post-image name.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(xy.startswith(c) or xy.endswith(c) for c in CONFLICT_STATUS_PREFIXES):
            conflicted.append(path.strip('"'))
    return conflicted


def get_file_content(path: str, *, cwd: Path, ref: str | None = None) -> str:
    """Return the content of a file, optionally at a git ref (e.g. branch)."""
    if ref is not None:
        proc = _run(["show", f"{ref}:{path}"], cwd=cwd, check=False)
        if proc.returncode != 0:
            raise GitError(f"could not read {path}@{ref}: {proc.stderr}")
        return proc.stdout
    return (cwd / path).read_text(errors="replace")


def get_conflict_info(path: str, *, cwd: Path) -> ConflictInfo:
    """Return the working-tree content (with conflict markers) of a path."""
    content = get_file_content(path, cwd=cwd)
    return ConflictInfo(path=path, content=content)


def get_backport_diff(target_branch: str, *, cwd: Path) -> str:
    """Full diff of the current branch vs. `target_branch` (for gap analysis)."""
    proc = _run(["diff", f"{target_branch}...HEAD"], cwd=cwd)
    return proc.stdout


def get_changed_files(target_branch: str, *, cwd: Path) -> list[str]:
    """Return the list of files changed between `target_branch` and HEAD.

    Used by the post-cherry-pick validator to know which files to check.
    """
    proc = _run(["diff", "--name-only", f"{target_branch}...HEAD"], cwd=cwd)
    return [line for line in proc.stdout.splitlines() if line]


def apply_patch(
    patch_text: str,
    *,
    allowed_files: list[str],
    cwd: Path,
    commit_message: str | None = None,
) -> str:
    """Apply a unified-diff patch and optionally commit it.

    This is the only path by which LLM-produced text touches the tree.
    Validation pipeline (per the trust boundary in the plan):
      1. `git apply --check` — the patch must apply cleanly.
      2. Scope check — the patch touches only `allowed_files`.
      3. Conflict-marker check — no leftover `<<<<<<<`, `=======`, `>>>>>>>`.

    The commit is the caller's responsibility: pass `commit_message` to
    commit immediately, or leave it None to leave the change staged.
    """
    # Write the patch to a temp file so `git apply` can consume it.
    patch_file = cwd / ".bpilot" / "current.patch"
    patch_file.parent.mkdir(exist_ok=True)
    patch_file.write_text(patch_text)

    # 1. Apply-check.
    check = _run(["apply", "--check", str(patch_file)], cwd=cwd, check=False)
    if check.returncode != 0:
        raise PatchApplyError("patch does not apply cleanly", details=check.stderr)

    # 2. Scope check: parse the patch for the files it touches.
    touched = _parse_patch_files(patch_text)
    out_of_scope = sorted(set(touched) - set(allowed_files))
    if out_of_scope:
        raise PatchApplyError(
            "patch touches files outside allowed scope",
            details=f"out-of-scope: {out_of_scope}",
        )

    # 3. Apply for real.
    _run(["apply", str(patch_file)], cwd=cwd)

    # 4. Conflict-marker check on the touched files.
    for path in touched:
        content = (cwd / path).read_text(errors="replace")
        if _has_conflict_markers(content):
            raise PatchApplyError("resolved file still contains conflict markers", details=path)

    patch_file.unlink(missing_ok=True)

    if commit_message:
        _run(["add", *touched], cwd=cwd)
        _run(["commit", "-m", commit_message], cwd=cwd)
        return current_head(cwd=cwd)
    return ""


def commit_all(message: str, *, cwd: Path) -> str:
    """Stage all tracked changes and commit. Returns the new HEAD SHA."""
    _run(["add", "-u"], cwd=cwd)
    _run(["commit", "-m", message], cwd=cwd)
    return current_head(cwd=cwd)


def commit_gap_fix(message: str, *, cwd: Path) -> str:
    """Commit staged gap-fix changes as a labelled `bpilot(gap):` commit.

    The caller is expected to have already staged the fix. We normalise
    the commit prefix here so gap fixes are always identifiable in `git log`.
    """
    prefix = "bpilot(gap): "
    full = message if message.startswith(prefix) else prefix + message
    _run(["commit", "-m", full], cwd=cwd)
    return current_head(cwd=cwd)


def _parse_patch_files(patch_text: str) -> list[str]:
    """Extract the list of file paths a unified diff touches.

    Looks for `+++ b/<path>` lines (the post-image path), which is the
    standard format `git diff` / `git format-patch` produce.
    """
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[len("+++ b/") :])
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            # Less common form without the b/ prefix.
            paths.append(line[len("+++ ") :])
    return paths


def _has_conflict_markers(text: str) -> bool:
    """True when `text` contains any of the standard conflict markers.

    `=======` is a common ASCII separator; require it on its own line to
    avoid matching legitimate uses (e.g. markdown rules).
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<<<<<<<") or stripped.startswith(">>>>>>>"):
            return True
        if stripped == "=======":
            return True
    return False
