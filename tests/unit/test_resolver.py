"""Tests for bpilot.resolver — LLM-assisted conflict resolution.

The resolver's LLM calls are mocked: we inject a fake LLMClient that
returns canned file content, so the tests exercise the
write-validate-retry loop without network access.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bpilot.resolver import (
    MAX_RETRIES,
    _build_prompt,
    _build_skill_context,
    _extract_content,
    resolve_conflicts,
)
from bpilot.skill_loader import load_skill

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_skill.md"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def conflict_repo(tmp_path: Path) -> Path:
    """A repo with a cherry-pick conflict in progress.

    Layout:
      main:    A (init with shared.py) -> B (modifies shared.py to "main")
      target:  A -> C (modifies shared.py to "target")

    Cherry-picking B onto target produces a conflict in shared.py.
    """
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "shared.py").write_text("value = 'base'\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    # target branch modifies shared.py
    _git(tmp_path, "checkout", "-b", "target")
    (tmp_path / "shared.py").write_text("value = 'target'\n")
    _git(tmp_path, "commit", "-am", "target edit")
    # main also modifies shared.py (will conflict)
    _git(tmp_path, "checkout", "main")
    (tmp_path / "shared.py").write_text("value = 'main'\n")
    _git(tmp_path, "commit", "-am", "main edit")
    # Start the cherry-pick to create the conflict.
    _git(tmp_path, "checkout", "target")
    subprocess.run(
        ["git", "cherry-pick", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return tmp_path


def _make_llm(resolved_content: str) -> MagicMock:
    """Create a mock LLMClient that returns the given resolved file content."""
    llm = MagicMock()
    response = MagicMock()
    response.text = resolved_content
    llm.query_llm.return_value = response
    return llm


def test_resolve_conflicts_success(conflict_repo: Path):
    """Correct resolved content from the LLM resolves the conflict."""
    llm = _make_llm("value = 'resolved'\n")
    result = resolve_conflicts(
        commit="abc123",
        conflicted_files=["shared.py"],
        target_branch="target",
        commit_message="main edit",
        llm=llm,
        skill=None,
        cwd=conflict_repo,
    )
    assert result.all_resolved is True
    assert "shared.py" in result.resolved_files
    assert llm.query_llm.call_count == 1
    # The file should be staged and clean.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=conflict_repo, capture_output=True, text=True
    )
    assert "UU" not in status.stdout
    # The file content should be the resolved version.
    assert (conflict_repo / "shared.py").read_text() == "value = 'resolved'\n"


def test_resolve_conflicts_retries_on_failure(conflict_repo: Path):
    """An empty first response triggers a retry; the second succeeds."""
    llm = MagicMock()
    bad_response = MagicMock()
    bad_response.text = ""
    good_response = MagicMock()
    good_response.text = "value = 'resolved'\n"
    llm.query_llm.side_effect = [bad_response, good_response]
    result = resolve_conflicts(
        commit="abc123",
        conflicted_files=["shared.py"],
        target_branch="target",
        commit_message="main edit",
        llm=llm,
        skill=None,
        cwd=conflict_repo,
    )
    assert result.all_resolved is True
    assert llm.query_llm.call_count == 2
    assert len(result.attempts) == 1
    assert result.attempts[0].succeeded is True
    assert result.attempts[0].attempt == 2


def test_resolve_conflicts_fails_after_max_retries(conflict_repo: Path):
    """All retries exhausted when the LLM keeps returning garbage."""
    llm = _make_llm("<<<<<<< STILL HAS MARKERS\n=======\n>>>>>>>\n")
    result = resolve_conflicts(
        commit="abc123",
        conflicted_files=["shared.py"],
        target_branch="target",
        commit_message="main edit",
        llm=llm,
        skill=None,
        cwd=conflict_repo,
    )
    assert result.all_resolved is False
    assert "shared.py" in result.failed_files
    assert llm.query_llm.call_count == MAX_RETRIES


def test_resolve_conflicts_syntax_error_retries(conflict_repo: Path):
    """A syntax error in the resolved content triggers a retry."""
    llm = MagicMock()
    bad_response = MagicMock()
    bad_response.text = "def broken(\n"  # syntax error
    good_response = MagicMock()
    good_response.text = "value = 'resolved'\n"
    llm.query_llm.side_effect = [bad_response, good_response]
    result = resolve_conflicts(
        commit="abc123",
        conflicted_files=["shared.py"],
        target_branch="target",
        commit_message="main edit",
        llm=llm,
        skill=None,
        cwd=conflict_repo,
    )
    assert result.all_resolved is True
    assert llm.query_llm.call_count == 2


def test_resolve_conflicts_strips_markdown_fences(conflict_repo: Path):
    """The resolver strips markdown fences from the LLM response."""
    llm = _make_llm("```python\nvalue = 'resolved'\n```\n")
    result = resolve_conflicts(
        commit="abc123",
        conflicted_files=["shared.py"],
        target_branch="target",
        commit_message="main edit",
        llm=llm,
        skill=None,
        cwd=conflict_repo,
    )
    assert result.all_resolved is True
    assert (conflict_repo / "shared.py").read_text() == "value = 'resolved'\n"


def test_build_prompt_includes_file_and_target():
    prompt = _build_prompt(
        file_path="src/app.py",
        conflict_content="<<<<< content",
        target_content="original",
        target_branch="8.0/edge",
        commit_message="fix: update app",
        skill_context="",
        previous_error="",
        attempt=1,
    )
    assert "src/app.py" in prompt
    assert "8.0/edge" in prompt
    assert "fix: update app" in prompt
    assert "<<<<< content" in prompt
    assert "original" in prompt
    assert "COMPLETE resolved file content" in prompt


def test_build_prompt_includes_previous_error():
    prompt = _build_prompt(
        file_path="src/app.py",
        conflict_content="content",
        target_content="original",
        target_branch="target",
        commit_message="msg",
        skill_context="",
        previous_error="file still contains conflict markers",
        attempt=2,
    )
    assert "previous attempt" in prompt.lower()
    assert "file still contains conflict markers" in prompt
    assert "#1" in prompt


def test_build_prompt_includes_skill_context():
    prompt = _build_prompt(
        file_path="src/app.py",
        conflict_content="content",
        target_content="original",
        target_branch="target",
        commit_message="msg",
        skill_context="## Known Divergences\n- 8.4 has shortcut",
        previous_error="",
        attempt=1,
    )
    assert "Known Divergences" in prompt
    assert "8.4 has shortcut" in prompt


def test_build_skill_context_with_skill():
    skill = load_skill(FIXTURE)
    assert skill is not None
    context = _build_skill_context(skill)
    assert "Branch Conventions" in context
    assert "Known Divergences" in context


def test_build_skill_context_without_skill():
    assert _build_skill_context(None) == ""


def test_extract_content_strips_fences():
    assert _extract_content("```python\nx = 1\n```\n") == "x = 1\n"
    assert _extract_content("```\nx = 1\n```\n") == "x = 1\n"
    assert _extract_content("x = 1\n") == "x = 1\n"


def test_extract_content_strips_yaml_fences():
    assert _extract_content("```yaml\nkey: value\n```\n") == "key: value\n"
