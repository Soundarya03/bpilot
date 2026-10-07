"""Tests for bpilot.validator — static checks on resolved files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bpilot.skill_loader import SkillSet, load_skill_set
from bpilot.validator import (
    build_tool_env,
    run_verification_checks,
    validate_changes,
    validate_file,
    validate_files,
)


def _skill_set_with_checks(tmp_path: Path, body: str) -> SkillSet:
    """Build a SkillSet whose `verification-checks` skill has the given body."""
    skills_dir = tmp_path / "bpilot" / "skills"
    vc_dir = skills_dir / "verification-checks"
    vc_dir.mkdir(parents=True)
    (vc_dir / "SKILL.md").write_text(
        "---\n"
        "name: verification-checks\n"
        "description: Format, lint, and unit-test commands.\n"
        "---\n"
        f"{body}\n"
    )
    return load_skill_set(skills_dir)


def test_valid_python_file_passes(tmp_path: Path):
    path = tmp_path / "good.py"
    path.write_text("x = 1\nprint(x)\n")
    result = validate_file("good.py", cwd=tmp_path)
    assert result.ok is True
    assert result.errors == []


def test_conflict_markers_fail(tmp_path: Path):
    path = tmp_path / "conflict.py"
    path.write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> branch\n")
    result = validate_file("conflict.py", cwd=tmp_path)
    assert result.ok is False
    assert any("conflict markers" in e for e in result.errors)


def test_syntax_error_fails(tmp_path: Path):
    path = tmp_path / "bad.py"
    path.write_text("def f(\n")
    result = validate_file("bad.py", cwd=tmp_path)
    assert result.ok is False
    assert any("syntax" in e.lower() for e in result.errors)


def test_placeholder_todo_fails(tmp_path: Path):
    path = tmp_path / "todo.py"
    path.write_text("# TODO: fix this later\nx = 1\n")
    result = validate_file("todo.py", cwd=tmp_path)
    assert result.ok is False
    assert any("TODO" in e for e in result.errors)


def test_placeholder_fixme_fails(tmp_path: Path):
    path = tmp_path / "fixme.py"
    path.write_text("# FIXME: broken\nx = 1\n")
    result = validate_file("fixme.py", cwd=tmp_path)
    assert result.ok is False
    assert any("FIXME" in e for e in result.errors)


def test_non_python_file_skips_syntax_check(tmp_path: Path):
    path = tmp_path / "config.md"
    path.write_text("# Some config\n\nThis is fine.\n")
    result = validate_file("config.md", cwd=tmp_path)
    assert result.ok is True


def test_missing_file_fails(tmp_path: Path):
    result = validate_file("nonexistent.py", cwd=tmp_path)
    assert result.ok is False
    assert any("not found" in e for e in result.errors)


def test_validate_files_returns_one_per_path(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("def f(\n")
    results = validate_files(["a.py", "b.py"], cwd=tmp_path)
    assert len(results) == 2
    assert results[0].ok is True
    assert results[1].ok is False


def test_valid_complex_python_file_passes(tmp_path: Path):
    path = tmp_path / "complex.py"
    path.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def main() -> int:\n"
        "    p = Path('.')\n"
        "    for f in p.iterdir():\n"
        "        print(f)\n"
        "    return 0\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    result = validate_file("complex.py", cwd=tmp_path)
    assert result.ok is True


def test_validate_changes_passes_clean_files(tmp_path: Path):
    """validate_changes runs per-file checks on all changed files."""
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "good2.py").write_text("y = 2\n")
    result = validate_changes(changed_files=["good.py", "good2.py"], cwd=tmp_path)
    assert result.ok is True
    assert len(result.file_results) == 2


def test_validate_changes_detects_conflict_markers(tmp_path: Path):
    """validate_changes catches conflict markers in changed files."""
    (tmp_path / "bad.py").write_text("<<<<<<< HEAD\nx = 1\n=======\n>>>>>>> branch\n")
    result = validate_changes(changed_files=["bad.py"], cwd=tmp_path)
    assert result.ok is False
    assert "bad.py" in result.failed_files


def test_validate_changes_skips_lock_regen_when_no_dependency_change(tmp_path: Path):
    """No lock regeneration when dependency files weren't changed."""
    (tmp_path / "app.py").write_text("x = 1\n")
    result = validate_changes(changed_files=["app.py"], cwd=tmp_path)
    assert result.lock_regen == []


def _fake_tool_run(calls: list) -> object:
    """subprocess.run stand-in that records (cmd, cwd) and succeeds."""

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append((list(cmd), cwd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def test_validate_changes_regenerates_root_lock(tmp_path: Path, monkeypatch):
    """A changed root pyproject.toml regenerates the root poetry.lock."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "poetry.lock").write_text("# lock\n")
    calls: list = []
    monkeypatch.setattr("bpilot.validator._has_command", lambda _cmd: True)
    monkeypatch.setattr("bpilot.validator.subprocess.run", _fake_tool_run(calls))
    result = validate_changes(changed_files=["pyproject.toml"], cwd=tmp_path)
    assert (["poetry", "lock"], tmp_path) in calls
    assert result.lock_regen == ["poetry lock"]


def test_validate_changes_regenerates_lock_in_nested_module(tmp_path: Path, monkeypatch):
    """A changed `machines/pyproject.toml` triggers `poetry lock` in machines/
    (monorepo layout), not just at the repo root."""
    (tmp_path / "machines").mkdir()
    (tmp_path / "machines" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "machines" / "poetry.lock").write_text("# lock\n")
    calls: list = []
    monkeypatch.setattr("bpilot.validator._has_command", lambda _cmd: True)
    monkeypatch.setattr("bpilot.validator.subprocess.run", _fake_tool_run(calls))
    result = validate_changes(changed_files=["machines/pyproject.toml"], cwd=tmp_path)
    assert (["poetry", "lock"], tmp_path / "machines") in calls
    assert result.lock_regen == ["poetry lock (in machines)"]


def test_validate_changes_no_lint_without_pyproject(tmp_path: Path):
    """No verification-check commands run via validate_changes (they live in run_verification_checks)."""
    (tmp_path / "app.py").write_text("x = 1\n")
    result = validate_changes(changed_files=["app.py"], cwd=tmp_path)
    assert result.errors == []  # per-file checks only


def test_run_verification_checks_passes_clean_files(tmp_path: Path):
    """run_verification_checks returns ok when commands pass."""
    skill_set = _skill_set_with_checks(tmp_path, "## Verification Checks\n- `true`\n- `true`\n")
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is True
    assert len(result.commands) == 2
    assert all(c.ok for c in result.commands)


def test_run_verification_checks_reports_failures(tmp_path: Path):
    """run_verification_checks captures failing command output."""
    skill_set = _skill_set_with_checks(tmp_path, "## Verification Checks\n- `false`\n")
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is False
    assert len(result.failures) == 1
    assert result.failures[0].command == "false"
    assert result.failures[0].returncode != 0


def test_run_verification_checks_no_commands_returns_ok(tmp_path: Path):
    """With no skill and no pyproject, no commands run; result is ok."""
    result = run_verification_checks(cwd=tmp_path, skill_set=None)
    assert result.ok is True
    assert result.commands == []


def test_run_verification_checks_falls_back_to_test_commands(tmp_path: Path):
    """run_verification_checks uses 'Test Commands' when 'Verification Checks' is absent."""
    skill_set = _skill_set_with_checks(tmp_path, "## Test Commands\n- `true`\n")
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is True
    assert len(result.commands) == 1
    assert result.commands[0].command == "true"


def test_verification_checks_section_preferred_over_test_commands(tmp_path: Path):
    """When both sections exist, 'Verification Checks' wins."""
    skill_set = _skill_set_with_checks(
        tmp_path, "## Verification Checks\n- `true`\n\n## Test Commands\n- `false`\n"
    )
    verification_skill = skill_set.get("verification-checks")
    assert verification_skill is not None
    commands = verification_skill.verification_checks
    assert commands == ["true"]


# --- build_tool_env / snap env sanitization ---


def test_build_tool_env_returns_none_when_not_snap(monkeypatch):
    """Outside the snap, the environment is inherited unchanged (None)."""
    monkeypatch.delenv("SNAP", raising=False)
    assert build_tool_env() is None


def test_build_tool_env_strips_four_vars_under_snap(monkeypatch):
    """Under snap, the four snap-injected vars are stripped; everything else preserved."""
    monkeypatch.setenv("SNAP", "/snap/bpilot/current")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/snap/bpilot/current/lib")
    monkeypatch.setenv("PYTHONPATH", "/snap/bpilot/current/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/snap/bpilot/current/usr")
    monkeypatch.setenv("VIRTUAL_ENV", "/snap/bpilot/current")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/home/user")
    monkeypatch.setenv("SNAP_NAME", "bpilot")  # other SNAP* vars preserved
    monkeypatch.setenv("CUSTOM", "keep-me")

    env = build_tool_env()
    assert env is not None
    for var in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        assert var not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["HOME"] == "/home/user"
    assert env["SNAP_NAME"] == "bpilot"
    assert env["CUSTOM"] == "keep-me"
    # The returned dict is a copy — mutating it doesn't touch os.environ.
    env["NEW"] = "x"
    assert "NEW" not in os.environ


def test_build_tool_env_missing_snap_vars_are_noop(monkeypatch):
    """Under snap with none of the four vars set, the env is returned unchanged (minus nothing)."""
    monkeypatch.setenv("SNAP", "/snap/bpilot/current")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = build_tool_env()
    assert env is not None
    assert env["PATH"] == "/usr/bin"
    assert env["SNAP"] == "/snap/bpilot/current"


def test_run_verification_checks_passes_sanitized_env_under_snap(tmp_path, monkeypatch):
    """Under snap, run_verification_checks passes build_tool_env() to subprocess."""
    monkeypatch.setenv("SNAP", "/snap/bpilot/current")
    monkeypatch.setenv("PYTHONPATH", "/snap/bpilot/current/site-packages")
    skill_set = _skill_set_with_checks(tmp_path, "## Verification Checks\n- `true`\n")

    captured_envs: list[dict | None] = []
    real_run = __import__("bpilot.validator", fromlist=["subprocess"]).subprocess.run

    def fake_run(*args, **kwargs):
        captured_envs.append(kwargs.get("env"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr("bpilot.validator.subprocess.run", fake_run)
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is True
    assert len(captured_envs) == 1
    env = captured_envs[0]
    assert env is not None
    assert "PYTHONPATH" not in env
    assert env["SNAP"] == "/snap/bpilot/current"


def test_run_verification_checks_passes_none_env_outside_snap(tmp_path, monkeypatch):
    """Outside snap, run_verification_checks passes env=None (inherit unchanged)."""
    monkeypatch.delenv("SNAP", raising=False)
    skill_set = _skill_set_with_checks(tmp_path, "## Verification Checks\n- `true`\n")

    captured_envs: list[dict | None] = []
    real_run = __import__("bpilot.validator", fromlist=["subprocess"]).subprocess.run

    def fake_run(*args, **kwargs):
        captured_envs.append(kwargs.get("env"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr("bpilot.validator.subprocess.run", fake_run)
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is True
    assert captured_envs == [None]


def test_run_verification_checks_inline_prefix_survives_sanitization(tmp_path, monkeypatch):
    """An inline `VAR=value` prefix in a skill command is executed unchanged.

    The shell sets the variable for that command only; scrubbing the parent's
    snap-injected value doesn't touch the command string. We verify the command
    string passed to subprocess is exactly as written in the skill.
    """
    monkeypatch.setenv("SNAP", "/snap/bpilot/current")
    monkeypatch.setenv("PYTHONPATH", "/snap/bpilot/current/site-packages")
    skill_set = _skill_set_with_checks(
        tmp_path,
        "## Verification Checks\n- `PYTHONPATH=src:lib true`\n",
    )

    captured_cmds: list[str] = []
    real_run = __import__("bpilot.validator", fromlist=["subprocess"]).subprocess.run

    def fake_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("bpilot.validator.subprocess.run", fake_run)
    result = run_verification_checks(cwd=tmp_path, skill_set=skill_set)
    assert result.ok is True
    assert captured_cmds == ["PYTHONPATH=src:lib true"]


def test_regen_locks_uses_sanitized_env_under_snap(tmp_path, monkeypatch):
    """Under snap, lock regeneration passes the sanitized env to subprocess."""
    monkeypatch.setenv("SNAP", "/snap/bpilot/current")
    monkeypatch.setenv("PYTHONPATH", "/snap/bpilot/current/site-packages")
    # poetry.lock present + poetry available (mocked) triggers lock regen path.
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\nversion='0'\n")
    monkeypatch.setattr("bpilot.validator._has_command", lambda cmd: cmd == "poetry")

    captured_envs: list[dict | None] = []

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        captured_envs.append(kwargs.get("env"))
        return _FakeProc()

    monkeypatch.setattr("bpilot.validator.subprocess.run", fake_run)
    validate_changes(changed_files=["pyproject.toml"], cwd=tmp_path)
    # The lock regen subprocess.run call used the sanitized env.
    assert captured_envs
    env = captured_envs[0]
    assert env is not None
    assert "PYTHONPATH" not in env
    assert env["SNAP"] == "/snap/bpilot/current"
