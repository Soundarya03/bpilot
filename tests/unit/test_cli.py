"""Smoke tests for the `bpilot` CLI dispatch.

We exercise the parser, the `--version` flag, and the early-error paths
that don't require a git repo (or use a temp one). The full `port`
orchestration is exercised via integration tests against a temp repo.
"""

from __future__ import annotations

import subprocess

from bpilot import __version__
from bpilot.cli import main


def test_version_flag(capsys):
    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert __version__ in out


def test_no_args_prints_help(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage" in out.lower() or "bpilot" in out.lower()


def test_port_requires_git_repo(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["port", "abc123", "8.0/edge"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not inside a git repository" in err


def test_finalize_without_session_errors(tmp_path, capsys, monkeypatch):
    # Initialise a git repo so we get past the repo check.
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    monkeypatch.chdir(tmp_path)
    rc = main(["finalize"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no bpilot session" in err.lower()
