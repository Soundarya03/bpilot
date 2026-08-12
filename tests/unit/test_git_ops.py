"""Tests for bpilot.git_ops against a real (temp) git repository.

These tests exercise the git layer end-to-end via subprocess against a
throwaway repo. No mocking — we want to catch real git behaviour drift.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bpilot.git_ops import (
    GitError,
    apply_patch,
    cherry_pick,
    create_backport_branch,
    create_backup_ref,
    current_branch,
    current_head,
    detect_conflicts,
    expand_commit_range,
    is_git_repo,
    is_merge_commit,
    short_sha,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialise a small git repo with two branches and one commit on each.

    Layout:
      main:     A (initial) -> B (target branch tip)
      feature:  A -> C (a commit to backport)
    """
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file.txt").write_text("line1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial commit")
    # Create a target branch off main with an unrelated change.
    _git(tmp_path, "checkout", "-b", "target")
    (tmp_path / "target_only.txt").write_text("on target\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "target branch commit")
    # Switch back, create the feature commit we want to backport.
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feature.txt").write_text("feature content\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feature commit")
    return tmp_path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_is_git_repo(repo: Path):
    assert is_git_repo(repo) is True


def test_is_git_repo_outside(tmp_path: Path):
    assert is_git_repo(tmp_path) is False


def test_current_head_and_branch(repo: Path):
    _git(repo, "checkout", "target")
    assert current_branch(repo) == "target"
    sha = current_head(repo)
    assert len(sha) == 40  # full SHA


def test_short_sha(repo: Path):
    sha = current_head(repo)
    short = short_sha(sha, cwd=repo)
    assert 7 <= len(short) <= 12
    assert sha.startswith(short)


def test_create_backup_ref(repo: Path):
    _git(repo, "checkout", "target")
    before = current_head(repo)
    ref = create_backup_ref(cwd=repo)
    assert ref.startswith("bpilot/backup/")
    # The ref points at the same commit as before.
    rev = subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=True
    )
    assert rev.stdout.strip() == before


def test_create_backport_branch(repo: Path):
    _git(repo, "checkout", "target")
    commits = expand_commit_range("feature", cwd=repo)
    short = short_sha(commits[0], cwd=repo)
    branch = create_backport_branch("target", short, cwd=repo)
    assert branch.startswith("backport/")
    assert current_branch(repo) == branch


def test_expand_commit_range_single(repo: Path):
    sha = current_head(repo)  # feature commit
    expanded = expand_commit_range(sha, cwd=repo)
    assert expanded == [sha]


def test_expand_commit_range_range(repo: Path):
    # HEAD~1..HEAD on feature should be the feature commit.
    _git(repo, "checkout", "feature")
    expanded = expand_commit_range("HEAD~1..HEAD", cwd=repo)
    assert len(expanded) == 1


def test_cherry_pick_clean(repo: Path):
    _git(repo, "checkout", "target")
    feature_sha = subprocess.run(
        ["git", "rev-parse", "feature"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    result = cherry_pick(feature_sha, cwd=repo)
    assert result.clean is True
    assert result.conflicts is False
    assert (repo / "feature.txt").exists()


def test_cherry_pick_conflict(repo: Path):
    # Set up a conflict: same line modified on both branches.
    _git(repo, "checkout", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add shared")
    # target modifies the line.
    _git(repo, "checkout", "-b", "target2")
    (repo / "shared.txt").write_text("target\n")
    _git(repo, "commit", "-am", "target edit")
    # main also modifies the same line.
    _git(repo, "checkout", "main")
    (repo / "shared.txt").write_text("main\n")
    _git(repo, "commit", "-am", "main edit")
    main_sha = current_head(repo)
    # Cherry-picking main onto target2 conflicts.
    _git(repo, "checkout", "target2")
    result = cherry_pick(main_sha, cwd=repo)
    assert result.conflicts is True
    assert "shared.txt" in result.conflicted_files
    # Detect_conflicts should agree.
    assert detect_conflicts(cwd=repo) == ["shared.txt"]
    # Clean up the in-progress cherry-pick so the fixture teardown is quiet.
    subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo, check=False)


def test_is_merge_commit(repo: Path):
    # The feature commit is not a merge.
    _git(repo, "checkout", "feature")
    assert is_merge_commit("HEAD", cwd=repo) is False
    # Make a real merge commit.
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "merge-target")
    _git(repo, "checkout", "-b", "merge-branch")
    (repo / "mb.txt").write_text("mb\n")
    _git(repo, "add", "mb.txt")
    _git(repo, "commit", "-m", "branch commit")
    _git(repo, "checkout", "merge-target")
    _git(repo, "merge", "--no-ff", "merge-branch", "-m", "merge them")
    assert is_merge_commit("HEAD", cwd=repo) is True


def test_apply_patch_validates_scope(repo: Path):
    # Create a patch that touches an out-of-scope file.
    _git(repo, "checkout", "feature")
    patch = (
        "diff --git a/extra.txt b/extra.txt\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/extra.txt\n"
        "@@ -0,0 +1 @@\n"
        "+sneaky\n"
    )
    with pytest.raises(Exception, match="out-of-scope"):
        apply_patch(patch, allowed_files=["feature.txt"], cwd=repo)


def test_apply_patch_rejects_conflict_markers(repo: Path):
    # Create a patch that, when applied, leaves conflict markers in place.
    _git(repo, "checkout", "feature")
    (repo / "feature.txt").write_text("feature content\n")
    _git(repo, "add", "feature.txt")
    # Write a patch that introduces conflict markers in feature.txt.
    patch = (
        "diff --git a/feature.txt b/feature.txt\n"
        "index e69de29..0123456 100644\n"
        "--- a/feature.txt\n"
        "+++ b/feature.txt\n"
        "@@ -1,1 +1,3 @@\n"
        " feature content\n"
        "+<<<<<<< HEAD\n"
        "+=======\n"
        "+>>>>>>> branch\n"
    )
    with pytest.raises(Exception, match="conflict markers"):
        apply_patch(patch, allowed_files=["feature.txt"], cwd=repo)


def test_apply_patch_applies_clean(repo: Path):
    _git(repo, "checkout", "feature")
    # Add a new file via patch.
    patch = (
        "diff --git a/new_file.txt b/new_file.txt\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/new_file.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )
    apply_patch(patch, allowed_files=["new_file.txt"], cwd=repo)
    assert (repo / "new_file.txt").read_text() == "hello\n"


def test_expand_commit_range_invalid(repo: Path):
    with pytest.raises(GitError):
        expand_commit_range("not-a-real-spec-zzz", cwd=repo)
