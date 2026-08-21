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


def is_worktree_dirty(*, cwd: Path) -> bool:
    """True when there are uncommitted changes (staged, unstaged, or untracked).

    Used to decide whether to auto-stash before a cherry-pick: regenerable
    build artifacts (e.g. `poetry.lock` regenerated by `tox run -e format`)
    leave the tree dirty and would make git refuse the pick.
    """
    tracked = _run(["status", "--porcelain", "--untracked-files=no"], cwd=cwd, check=False)
    untracked = _run(["ls-files", "--others", "--exclude-standard"], cwd=cwd, check=False)
    return bool(tracked.stdout.strip() or untracked.stdout.strip())


def stash_push(*, cwd: Path, message: str = "bpilot auto-stash") -> bool:
    """Stash all uncommitted changes (including untracked). Returns True if
    something was stashed, False if the tree was already clean."""
    if not is_worktree_dirty(cwd=cwd):
        return False
    _run(["stash", "push", "-u", "-m", message], cwd=cwd)
    return True


def stash_pop(*, cwd: Path) -> bool:
    """Pop the most recent stash. Returns True on success.

    On conflict (the popped changes clash with what the backport did —
    e.g. the backport regenerated the same lock file), the backport's
    version wins: we check out our state for the conflicted paths and drop
    the stash. The stash entry is consumed either way, so this never leaves
    a lingering stash for a bpilot-created stash.
    """
    proc = _run(["stash", "pop"], cwd=cwd, check=False)
    if proc.returncode == 0:
        return True
    # Pop conflicts: the backport's version is authoritative. Reset the
    # conflicted paths to HEAD (the backport state), then drop the stash
    # entry the failed pop left behind.
    conflicted = detect_conflicts(cwd=cwd)
    if conflicted:
        _run(["checkout", "HEAD", "--", *conflicted], cwd=cwd, check=False)
    _run(["stash", "drop"], cwd=cwd, check=False)
    return False


def continue_cherry_pick(*, cwd: Path) -> None:
    """Continue an in-progress cherry-pick after conflicts are resolved.

    Uses GIT_EDITOR=true so git doesn't try to open an interactive editor
    for the commit message (it reuses the original commit's message).
    """
    import os

    env = dict(os.environ)
    env["GIT_EDITOR"] = "true"
    _run(["cherry-pick", "--continue"], cwd=cwd, check=True, env=env)


def commit_partial_cherry_pick(message: str, unresolved: list[str], *, cwd: Path) -> str:
    """Commit a partially-resolved cherry-pick, leaving `unresolved` files
    in the working tree for manual fixing.

    The resolved files are staged (by the resolver); the commit captures
    exactly those. `unresolved` files are still in git's unmerged state,
    which blocks any commit, so we first drop them from the index
    (`git rm --cached`), commit the resolved staged content, then restore
    the unresolved files' conflicted content into the working tree. Net
    effect: the commit contains only the resolved files; the failed files
    remain as untracked/unstaged working-tree changes the user can edit
    and `git add`.

    `-n` skips hooks — the tree is known-incomplete (unresolved files are
    deliberately excluded), so pre-commit checks would be noise. Returns
    the new HEAD.
    """
    import subprocess

    saved: dict[str, bytes] = {}
    for f in unresolved:
        p = cwd / f
        saved[f] = p.read_bytes() if p.exists() else None  # type: ignore[assignment]
        # Drop the unmerged/index entry so the commit ignores this file.
        subprocess.run(
            ["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", f],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    _run(["commit", "-n", "-m", message], cwd=cwd)
    # Restore the conflicted working-tree content as an untracked change.
    for f, content in saved.items():
        if content is not None:
            (cwd / f).write_bytes(content)
    return current_head(cwd=cwd)


def commit_staged(message: str, *, cwd: Path) -> str:
    """Commit the currently staged changes with `message`. Returns new HEAD.

    `-n` skips hooks. Used when the caller has already staged exactly what
    should be committed.
    """
    _run(["commit", "-n", "-m", message], cwd=cwd)
    return current_head(cwd=cwd)


def commit_amend_staged(*, cwd: Path) -> str:
    """Amend staged changes into HEAD, keeping the existing message.

    Used by `port --continue` to fold the user's staged manual fixes into
    the paused commit. `--no-edit` keeps the original message; bpilot never
    opens an interactive editor. Returns the new HEAD.
    """
    _run(["commit", "--amend", "--no-edit", "-n"], cwd=cwd)
    return current_head(cwd=cwd)


def is_index_clean(*, cwd: Path) -> bool:
    """True when there are no staged changes (index == HEAD)."""
    proc = _run(["diff", "--cached", "--quiet"], cwd=cwd, check=False)
    return proc.returncode == 0


def is_applied(commit: str, *, cwd: Path) -> bool:
    """True when `commit`'s changeset is already present on this branch.

    Uses `git cherry`, which detects patch-equivalent commits (a
    cherry-picked commit has a different SHA but the same patch-id).
    Used only as a best-effort guard on `--continue` (to notice when the
    user already committed the paused commit's fixes themselves); the
    resume queue is driven by the session's `remaining_commits`, not this.
    """
    proc = _run(["cherry", "HEAD", commit], cwd=cwd, check=False)
    if proc.returncode != 0:
        return False
    # Output: "+ <sha>" (not present) or "- <sha>" (already applied).
    return proc.stdout.strip().startswith("-")


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


def get_commit_diff(commit: str, path: str, *, cwd: Path) -> str:
    """Per-file diff introduced by `commit` (vs. its first parent).

    The first-parent comparison matches the `cherry_pick -m 1` semantics
    for merge commits. Used by the conflict resolver to ground the LLM in
    what the incoming commit actually changed. Raises GitError when the
    diff cannot be computed (e.g. unresolvable commit, root commit with
    no parent) — callers should degrade gracefully to no diff.
    """
    proc = _run(["diff", f"{commit}^1", commit, "--", path], cwd=cwd, check=False)
    if proc.returncode != 0:
        raise GitError(f"could not compute diff of {commit} for {path}: {proc.stderr}")
    return proc.stdout


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


def list_branches(*, cwd: Path, limit: int = 50) -> list[str]:
    """Return local + remote branch names (de-duplicated, sorted).

    Used by `bpilot init` to infer branch conventions. Remote branches
    are stripped of their `origin/` prefix when a local branch of the
    same name exists. HEAD (the current branch) is always first.
    """
    proc = _run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"],
        cwd=cwd,
        check=False,
    )
    seen: set[str] = set()
    branches: list[str] = []
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        # Strip remote prefix for de-duplication (e.g. origin/main -> main).
        short = name.split("/", 1)[1] if "/" in name else name
        if short in seen:
            continue
        seen.add(short)
        branches.append(short)
        if len(branches) >= limit:
            break
    current = current_branch(cwd=cwd)
    # Move the current branch to the front for prominence.
    if current in branches:
        branches.remove(current)
        branches.insert(0, current)
    return branches


def recent_commit_subjects(*, cwd: Path, limit: int = 50) -> list[str]:
    """Return the subjects (first line) of the most recent commits.

    Used by `bpilot init` to infer commit-message conventions. Returns
    oldest-first of the most recent `limit` commits on the current branch.
    """
    proc = _run(
        ["log", f"-{limit}", "--format=%s"],
        cwd=cwd,
        check=False,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]
