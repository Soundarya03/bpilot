"""Smoke tests for the `bpilot` CLI dispatch.

We exercise the parser, the `--version` flag, and the early-error paths
that don't require a git repo (or use a temp one). The full `port`
orchestration is exercised via integration tests against a temp repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bpilot import __version__
from bpilot.cli import main
from bpilot.session import Session, save_session
from bpilot.skill_loader import KNOWN_SKILL_NAMES


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


def _git_repo(tmp_path: Path) -> Path:
    """Initialise a git repo in tmp_path and return it."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    return tmp_path


def test_port_scaffolds_skills_dir_on_first_run(tmp_path, capsys, monkeypatch):
    """`bpilot port` with no bpilot/skills/ scaffolds it before proceeding.

    The run will fail later (no remote to fetch from), but the scaffolding
    happens before the fetch step, so the directory exists after the run.
    """
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["port", "abc123", "8.0/edge", "--no-llm"])
    # Fetch fails (no remote) → rc 1, but init has already run.
    assert rc == 1
    skills_dir = repo / "bpilot" / "skills"
    assert skills_dir.is_dir(), "skills directory was not scaffolded"
    for name in KNOWN_SKILL_NAMES:
        assert (skills_dir / name / "SKILL.md").is_file()


def test_port_no_init_does_not_scaffold(tmp_path, capsys, monkeypatch):
    """`bpilot port --no-init` with no skills dir does not scaffold."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["port", "abc123", "8.0/edge", "--no-llm", "--no-init"])
    assert rc == 1  # fetch fails
    assert not (repo / "bpilot" / "skills").exists()


def test_port_dry_run_still_scaffolds(tmp_path, capsys, monkeypatch):
    """`bpilot port --dry-run` triggers init so users can scaffold without a real port."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["port", "abc123", "8.0/edge", "--no-llm", "--dry-run"])
    # Fetch fails before the dry-run short-circuit, but init has already run.
    assert rc == 1
    assert (repo / "bpilot" / "skills").is_dir()


def test_port_no_init_emits_note_when_no_skills(tmp_path, capsys, monkeypatch):
    """With --no-init and no skills dir (LLM enabled), a degradation note is printed.

    We force use_llm by injecting an API key via env so the note fires.
    """
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    main(["port", "abc123", "8.0/edge", "--no-init"])
    err = capsys.readouterr().err
    assert "no skills directory" in err


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


def test_finalize_with_session_no_skills_dir_errors(tmp_path, capsys, monkeypatch):
    """finalize with a session but no skills dir errors with 'run bpilot port first'."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    # Plant a session so we get past the session check.
    save_session(
        Session(
            port_head_sha="abc123",
            target_branch="8.0/edge",
            backport_branch="backport/x-to-8.0/edge",
            backup_ref="bpilot/backup/1",
            original_branch="main",
        ),
        repo_root=repo,
    )
    rc = main(["finalize"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "run `bpilot port` first" in err
    # finalize must not scaffold.
    assert not (repo / "bpilot" / "skills").exists()


def test_port_skill_load_error_exits_nonzero(tmp_path, capsys, monkeypatch):
    """A malformed skill file is a hard error (not silent degradation)."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    skills_dir = repo / "bpilot" / "skills" / "version-control"
    skills_dir.mkdir(parents=True)
    # name mismatch: frontmatter says 'wrong-name' but dir is 'version-control'.
    (skills_dir / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: x\n---\n## Branch Conventions\n- main\n"
    )
    rc = main(["port", "abc123", "8.0/edge", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "must match parent directory" in err


# --- `bpilot init` ---


def test_init_scaffolds_skills_and_bpilot_dir(tmp_path, capsys, monkeypatch):
    """`bpilot init --no-llm` scaffolds the five skills + .bpilot/ dir."""
    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("# Project\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)

    rc = main(["init", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 0
    assert (repo / "bpilot" / "skills").is_dir()
    for name in KNOWN_SKILL_NAMES:
        assert (repo / "bpilot" / "skills" / name / "SKILL.md").is_file()
    assert (repo / ".bpilot").is_dir()
    assert ".bpilot/" in (repo / ".gitignore").read_text()
    assert "scaffolded" in err.lower()
    assert "skipped" in err.lower()  # no-llm skips inference


def test_init_refuses_existing_skills_dir(tmp_path, capsys, monkeypatch):
    """`bpilot init` on an already-initialised repo errors (no clobbering)."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    # Pre-create the skills dir as if a prior init had run.
    (repo / "bpilot" / "skills").mkdir(parents=True)
    rc = main(["init", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "already exists" in err
    # Nothing should have been overwritten / added.
    assert not (repo / ".bpilot").is_dir()


def test_init_requires_git_repo(tmp_path, capsys, monkeypatch):
    """`bpilot init` outside a git repo errors with the standard message."""
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not inside a git repository" in err


def test_init_with_llm_refines_skills(tmp_path, capsys, monkeypatch):
    """`bpilot init` with an API key calls the LLM and writes inferred content."""
    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("# Project\nA Python project using ruff.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: init"], cwd=repo, check=True, capture_output=True
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    # Patch LLMClient to return scripted skill bodies.
    from bpilot import cli as cli_mod
    from bpilot.llm_client import LLMResponse

    class FakeLLMClient:
        def __init__(self, *a, **kw):
            pass

        @property
        def usage(self):
            return None

        def query_llm(self, prompt, *, system=""):
            if "Verification Checks" in prompt or "verification" in prompt:
                return LLMResponse(
                    text="## Verification Checks\n- Format: `ruff format src/ tests/`\n",
                    usage={},
                )
            return LLMResponse(
                text="## Branch Conventions\n- `main`: active development.\n",
                usage={},
            )

    monkeypatch.setattr(cli_mod, "LLMClient", FakeLLMClient)
    rc = main(["init"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "inferred" in err.lower()

    vc_file = repo / "bpilot" / "skills" / "verification-checks" / "SKILL.md"
    vc_content = vc_file.read_text()
    assert "ruff format" in vc_content
    assert "## Verification Checks" in vc_content
    # Frontmatter is preserved.
    assert vc_content.startswith("---\n")
    assert "name: verification-checks" in vc_content

    vctrl_file = repo / "bpilot" / "skills" / "version-control" / "SKILL.md"
    assert "main" in vctrl_file.read_text()


def test_init_no_llm_when_no_api_key(tmp_path, capsys, monkeypatch):
    """`bpilot init` without an API key scaffolds placeholders + a note."""
    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("# Project\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    rc = main(["init"])
    err = capsys.readouterr().err
    assert rc == 0
    assert (repo / "bpilot" / "skills").is_dir()
    assert "no OpenRouter API key" in err
    # Skill files are placeholders (empty bodies, no real commands).
    from bpilot.skill_loader import load_skill_set

    skill_set = load_skill_set(repo / "bpilot" / "skills")
    assert skill_set is not None
    vc = skill_set.get("verification-checks")
    assert vc is not None
    assert vc.is_empty is True
    assert vc.verification_checks == []


# --- --no-fetch ---


def test_no_fetch_skips_fetch_and_uses_local_target_ref(tmp_path, capsys, monkeypatch):
    """--no-fetch skips the fetch step entirely; the backport is based on
    the local target ref as-is."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod

    fetch_calls: list[int] = []
    monkeypatch.setattr(
        cli_mod,
        "fetch_target_branch",
        lambda *a, **kw: fetch_calls.append(1),
    )

    rc = main(["port", sha, "target", "--no-llm", "--no-init", "--no-fetch"])
    out = capsys.readouterr().out
    assert rc == 0
    assert fetch_calls == []  # fetch never ran
    assert "skipping fetch" in out
    # Run completed against the local ref: backport branch, session, report.
    assert "backport/" in _git(repo, "branch", "--list").stdout
    assert (repo / ".bpilot" / "session.json").exists()
    assert (repo / "BACKPORT_REPORT.md").exists()


def test_fetch_runs_by_default(tmp_path, capsys, monkeypatch):
    """Without --no-fetch, the target branch is fetched before branching."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod

    fetch_calls: list[str] = []
    real_fetch = cli_mod.fetch_target_branch

    def recording_fetch(branch, *, cwd, remote="origin"):
        fetch_calls.append(branch)
        return real_fetch(branch, cwd=cwd)

    monkeypatch.setattr(cli_mod, "fetch_target_branch", recording_fetch)

    rc = main(["port", sha, "target", "--no-llm", "--no-init"])
    assert rc == 0
    assert fetch_calls == ["target"]


# --- baseline gate (`--skip-baseline`, baseline failure / success) ---


def _port_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build a git repo with a target branch and a feature commit to backport.

    Layout:
      main:    A (initial)
      target:  A -> B (target branch tip)
      feature: A -> C (commit to backport onto target; applies cleanly)

    Returns (repo, feature_commit_sha). We start on `feature`.
    """
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("line1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "checkout", "-b", "target")
    (repo / "target_only.txt").write_text("on target\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "target branch commit")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "feature.txt").write_text("feature content\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature commit")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_baseline_failure_aborts_and_cleans_up(tmp_path, capsys, monkeypatch):
    """Baseline check failure -> exit 1, original branch restored, backport
    branch deleted, triage message on stderr, no session/report written."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.validator import CommandResult, VerificationResult

    failing = VerificationResult(
        ok=False,
        commands=[
            CommandResult(
                command="tox -e unit",
                ok=False,
                stdout="",
                stderr="collected 5 items\n3 failed\nFAIL AssertionError",
                returncode=1,
            )
        ],
    )
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: failing)

    rc = main(["port", sha, "target", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    # Triage message: all three causes + the `bpilot init` review note.
    assert "unmodified target branch (target)" in err
    assert "Missing dependencies" in err
    assert "Incorrect verification commands" in err
    assert "target branch itself is broken" in err
    assert "bpilot init" in err
    assert "review" in err.lower()
    # Failing command + its exit code is shown.
    assert "tox -e unit" in err
    assert "exit 1" in err
    # Truncated output is shown.
    assert "3 failed" in err
    # Original branch restored, backport branch deleted.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature"
    # The backport branch name is backport/<short>-to-target.
    branches = _git(repo, "branch", "--list").stdout
    assert "backport/" not in branches
    # No session file, no report file.
    assert not (repo / ".bpilot" / "session.json").exists()
    assert not (repo / "BACKPORT_REPORT.md").exists()


def test_baseline_pass_proceeds_to_cherry_pick(tmp_path, capsys, monkeypatch):
    """A green baseline lets the run proceed; the cherry-pick happens."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.validator import VerificationResult

    green = VerificationResult(ok=True, commands=[])
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: green)

    cherry_pick_calls: list[str] = []
    real_cherry_pick = cli_mod.cherry_pick

    def recording_cherry_pick(commit, *, cwd):
        cherry_pick_calls.append(commit)
        return real_cherry_pick(commit, cwd=cwd)

    monkeypatch.setattr(cli_mod, "cherry_pick", recording_cherry_pick)

    rc = main(["port", sha, "target", "--no-llm"])
    assert rc == 0
    assert cherry_pick_calls == [sha]
    # Session + report were written (run completed).
    assert (repo / ".bpilot" / "session.json").exists()
    assert (repo / "BACKPORT_REPORT.md").exists()


def test_baseline_runs_after_branch_creation_before_cherry_pick(tmp_path, capsys, monkeypatch):
    """The baseline call happens on the backport branch (after creation) and
    before the first cherry-pick."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.validator import VerificationResult

    order: list[str] = []
    baseline_branch: list[str] = []

    def baseline_mock(**kw):
        # Record the branch we're on when the baseline runs.
        baseline_branch.append(_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip())
        order.append("baseline")
        return VerificationResult(ok=True, commands=[])

    monkeypatch.setattr(cli_mod, "run_verification_checks", baseline_mock)

    def cherry_pick_mock(commit, *, cwd):
        order.append("cherry_pick")
        # Return a clean result without actually running git, so we can
        # assert ordering without cherry-pick side effects muddying it.
        from bpilot.git_ops import CherryPickResult

        return CherryPickResult(commit=commit, applied=True)

    monkeypatch.setattr(cli_mod, "cherry_pick", cherry_pick_mock)

    rc = main(["port", sha, "target", "--no-llm"])
    assert rc == 0
    # Baseline ran on the backport branch (not the original feature branch).
    assert baseline_branch
    assert baseline_branch[0].startswith("backport/")
    # Baseline ran before the cherry-pick.
    assert order == ["baseline", "cherry_pick"]


def test_skip_baseline_skips_gate_and_fixer_gets_false(tmp_path, capsys, monkeypatch):
    """--skip-baseline skips the baseline call and the fixer is invoked with
    baseline_passed=False."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.git_ops import CherryPickResult
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    baseline_calls: list[int] = []

    def baseline_mock(**kw):
        baseline_calls.append(1)
        return VerificationResult(ok=True, commands=[])

    monkeypatch.setattr(cli_mod, "run_verification_checks", baseline_mock)
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(commit=commit, applied=True),
    )

    fixer_calls: list[bool] = []

    def fixer_mock(**kw):
        fixer_calls.append(kw.get("baseline_passed"))
        return FixResult(
            ok=True, iterations=[], final_result=VerificationResult(ok=True, commands=[])
        )

    monkeypatch.setattr(cli_mod, "run_verification_checks_with_fixes", fixer_mock)

    rc = main(["port", sha, "target", "--no-llm", "--skip-baseline"])
    out = capsys.readouterr().out
    assert rc == 0
    assert baseline_calls == []  # baseline never ran
    assert "skipping baseline verification checks" in out.lower()
    assert fixer_calls == [False]


def test_dry_run_never_runs_baseline(tmp_path, capsys, monkeypatch):
    """--dry-run exits before branch creation; baseline never runs."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod

    baseline_calls: list[int] = []
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: baseline_calls.append(1))

    rc = main(["port", sha, "target", "--no-llm", "--dry-run"])
    assert rc == 0
    assert baseline_calls == []


def test_no_llm_still_runs_baseline(tmp_path, capsys, monkeypatch):
    """--no-llm does not skip the baseline gate (it's deterministic)."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.git_ops import CherryPickResult
    from bpilot.validator import VerificationResult

    baseline_calls: list[int] = []

    def baseline_mock(**kw):
        baseline_calls.append(1)
        return VerificationResult(ok=True, commands=[])

    monkeypatch.setattr(cli_mod, "run_verification_checks", baseline_mock)
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(commit=commit, applied=True),
    )

    rc = main(["port", sha, "target", "--no-llm"])
    assert rc == 0
    assert len(baseline_calls) == 1


def test_baseline_vacuously_green_with_no_commands(tmp_path, capsys, monkeypatch):
    """No skill and no pyproject.toml -> baseline passes vacuously and the
    run proceeds (no mocking of run_verification_checks; real path)."""
    repo, sha = _port_repo(tmp_path)
    # Ensure no pyproject.toml (so auto-detect doesn't fire) and no skills dir.
    assert not (repo / "pyproject.toml").exists()
    monkeypatch.chdir(repo)

    rc = main(["port", sha, "target", "--no-llm", "--no-init"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "running baseline verification checks" in out
    # Run completed: cherry-pick happened, session + report written.
    assert (repo / ".bpilot" / "session.json").exists()
    assert (repo / "BACKPORT_REPORT.md").exists()


def test_baseline_failure_output_truncated_to_40_lines(tmp_path, capsys, monkeypatch):
    """Each failing command's output is truncated to the last 40 lines."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.validator import CommandResult, VerificationResult

    long_stderr = "\n".join(f"line {i}" for i in range(100))
    failing = VerificationResult(
        ok=False,
        commands=[CommandResult(command="pytest", ok=False, stderr=long_stderr, returncode=1)],
    )
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: failing)

    rc = main(["port", sha, "target", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    # The first 60 lines are dropped; only the last 40 survive.
    assert "line 0\n" not in err
    assert "line 59" not in err
    assert "line 60" in err
    assert "line 99" in err


def test_baseline_failure_wipes_preexisting_session_state(tmp_path, capsys, monkeypatch):
    """A baseline failure must clear any pre-existing `.bpilot/` session and
    `BACKPORT_REPORT.md` left over from a prior `port` run, not just delete
    the current backport branch. Otherwise `bpilot reset` / `finalize`
    would later operate on stale state pointing at a deleted branch."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.session import Session
    from bpilot.validator import CommandResult, VerificationResult

    # Simulate leftover state from a previous, successful `port` run.
    stale_session = Session(
        port_head_sha="deadbeef",
        target_branch="target",
        backport_branch="backport/old-to-target",
        backup_ref="bpilot/backup/old",
        original_branch="feature",
    )
    save_session(stale_session, repo_root=repo)
    (repo / "BACKPORT_REPORT.md").write_text("# stale report\n")

    failing = VerificationResult(
        ok=False,
        commands=[CommandResult(command="tox", ok=False, stdout="", stderr="boom", returncode=2)],
    )
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: failing)

    rc = main(["port", sha, "target", "--no-llm"])
    assert rc == 1
    # Stale session + report are gone.
    assert not (repo / ".bpilot").exists()
    assert not (repo / "BACKPORT_REPORT.md").exists()
    # And the current run's backport branch is gone too.
    branches = _git(repo, "branch", "--list").stdout
    assert "backport/" not in branches
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature"


def test_baseline_failure_clears_session_even_when_checkout_fails(tmp_path, capsys, monkeypatch):
    """If the rollback checkout fails (e.g. dirty tree), session state is
    still wiped and a warning is printed — the user is never left with
    stale `.bpilot/` data they must clean up by hand."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.git_ops import GitError
    from bpilot.session import Session
    from bpilot.validator import CommandResult, VerificationResult

    stale_session = Session(
        port_head_sha="cafebabe",
        target_branch="target",
        backport_branch="backport/old-to-target",
        backup_ref="bpilot/backup/old",
        original_branch="feature",
    )
    save_session(stale_session, repo_root=repo)
    (repo / "BACKPORT_REPORT.md").write_text("# stale report\n")

    failing = VerificationResult(
        ok=False,
        commands=[CommandResult(command="tox", ok=False, stdout="", stderr="boom", returncode=2)],
    )
    monkeypatch.setattr(cli_mod, "run_verification_checks", lambda **kw: failing)
    # Checkout fails (simulating a dirty tree); delete_branch must still be
    # attempted and session state must still be cleared.
    monkeypatch.setattr(cli_mod, "checkout_branch", lambda *a, **kw: (_ for _ in ()).throw(
        GitError("git checkout failed (dirty tree)")
    ))

    rc = main(["port", sha, "target", "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    # Session wiped despite the checkout failure.
    assert not (repo / ".bpilot").exists()
    assert not (repo / "BACKPORT_REPORT.md").exists()
    # Warning surfaced.
    assert "could not check out" in err
    assert "manual cleanup may be required" in err


# --- gap analysis wiring (Phase 7) ---


def test_port_runs_gap_analysis_between_verification_and_snapshot(tmp_path, capsys, monkeypatch):
    """With LLM enabled and a successful pick, analyze_gaps is invoked and
    its findings land in the session + report."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult
    from bpilot.git_ops import CherryPickResult
    from bpilot.session import GapFinding, PotentialGap
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    # Baseline + verification checks both green; cherry-pick clean.
    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(commit=commit, applied=True),
    )
    monkeypatch.setattr(
        cli_mod,
        "run_verification_checks_with_fixes",
        lambda **kw: FixResult(
            ok=True,
            iterations=[],
            final_result=VerificationResult(ok=True, commands=[]),
        ),
    )

    calls: list[dict] = []

    def fake_analyze(**kw):
        calls.append(kw)
        return GapResult(
            findings=[
                GapFinding(
                    commit="fixsha",
                    checklist_item="refresh behaviour",
                    severity="critical",
                    file="src/app.py",
                    description="add foo on refresh",
                )
            ],
            potential_gaps=[
                PotentialGap(
                    checklist_item="refresh behaviour",
                    question="should foo run on refresh?",
                    files_examined=["src/app.py"],
                )
            ],
        )

    monkeypatch.setattr(cli_mod, "analyze_gaps", fake_analyze)

    rc = main(["port", sha, "target"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["target_branch"] == "target"

    # Session + report carry the findings.
    from bpilot.session import load_session

    session = load_session(repo_root=repo)
    assert session is not None
    assert len(session.gap_findings) == 1
    assert session.gap_findings[0].commit == "fixsha"
    assert len(session.potential_gaps) == 1
    report_md = (repo / "BACKPORT_REPORT.md").read_text()
    assert "add foo on refresh" in report_md
    assert "### Potential gaps" in report_md
    assert "should foo run on refresh?" in report_md


def test_port_pauses_and_skips_cherry_pick_continue_on_unresolvable(tmp_path, capsys, monkeypatch):
    """A conflict the LLM cannot resolve pauses the run (no abort). The
    failed file is left in the tree and the session is paused for --continue."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.git_ops import CherryPickResult
    from bpilot.resolver import ResolutionResult
    from bpilot.validator import VerificationResult

    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )
    # Cherry-pick conflicts; LLM resolution fails → paused.
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(
            commit=commit, applied=False, conflicts=True, conflicted_files=["file.txt"]
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_conflicts",
        lambda **kw: ResolutionResult(commit=kw["commit"], failed_files=["file.txt"]),
    )
    # The partial commit needs somewhere to land; stub it.
    monkeypatch.setattr(cli_mod, "commit_partial_cherry_pick", lambda *a, **kw: "head")

    from bpilot.gap_analyzer import GapResult

    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    rc = main(["port", sha, "target"])
    assert rc == 1

    from bpilot.session import load_session

    session = load_session(repo_root=repo)
    assert session is not None and session.paused
    assert session.paused_commit == sha
    assert session.paused_failed_files == ["file.txt"]


def test_port_no_llm_marks_gap_section_skipped(tmp_path, capsys, monkeypatch):
    """--no-llm skips gap analysis and the report shows the skipped reason."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.git_ops import CherryPickResult
    from bpilot.validator import VerificationResult

    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(commit=commit, applied=True),
    )

    rc = main(["port", sha, "target", "--no-llm"])
    assert rc == 0
    report_md = (repo / "BACKPORT_REPORT.md").read_text()
    assert "Skipped (`--no-llm`)" in report_md


def test_port_no_skill_marks_gap_section_skipped_with_reason(tmp_path, capsys, monkeypatch):
    """With LLM enabled but no skills dir (--no-init), gap analysis is
    skipped with the 'no gap-analysis skill found' reason."""
    repo, sha = _port_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.git_ops import CherryPickResult
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )
    monkeypatch.setattr(
        cli_mod,
        "cherry_pick",
        lambda commit, *, cwd: CherryPickResult(commit=commit, applied=True),
    )
    monkeypatch.setattr(
        cli_mod,
        "run_verification_checks_with_fixes",
        lambda **kw: FixResult(
            ok=True,
            iterations=[],
            final_result=VerificationResult(ok=True, commands=[]),
        ),
    )

    rc = main(["port", sha, "target", "--no-init"])
    assert rc == 0
    report_md = (repo / "BACKPORT_REPORT.md").read_text()
    assert "Skipped (no gap-analysis skill found)." in report_md
    assert "No gaps found." not in report_md


# --- pause + --continue flow ---


def _conflict_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a repo where backporting `sha_bad` onto `target` conflicts in
    `app.py`, followed by a second, clean commit `sha_ok` on `other.py`.

    Layout:
      main:    A (app.py v1, other.py v1)
      target:  A -> T (app.py line edited → conflicts with B's edit)
      feature: A -> B (app.py same line edited) -> C (other.py edited, clean)

    Returns (repo, sha_bad, sha_ok). Starts on `feature`.
    """
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("x = 1\ny = 1\n")
    (repo / "other.py").write_text("z = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    _git(repo, "checkout", "-b", "target")
    (repo / "app.py").write_text("x = 100  # target rewrite\ny = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "target rewrite")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "app.py").write_text("x = 2  # feature change\ny = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature app change")
    sha_bad = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "other.py").write_text("z = 2  # feature other change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature other change")
    sha_ok = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha_bad, sha_ok


def _failing_resolver(commit, **kw):
    """A resolver that always fails on app.py (records a failed attempt)."""
    from bpilot.resolver import ResolutionAttempt, ResolutionResult

    return ResolutionResult(
        commit=commit,
        failed_files=["app.py"],
        attempts=[
            ResolutionAttempt(file_path="app.py", attempt=3, succeeded=False, error="boom")
        ],
    )


def _green_checks(monkeypatch):
    from bpilot import cli as cli_mod
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )
    monkeypatch.setattr(
        cli_mod,
        "run_verification_checks_with_fixes",
        lambda **kw: FixResult(
            ok=True,
            iterations=[],
            final_result=VerificationResult(ok=True, commands=[]),
        ),
    )


def test_port_pauses_on_unresolvable_conflict(tmp_path, capsys, monkeypatch):
    """Case A setup: an unresolvable conflict pauses the run — resolved
    state committed, failed file left in the tree, session paused."""
    repo, sha_bad, _ = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod

    _green_checks(monkeypatch)
    monkeypatch.setattr(cli_mod, "resolve_conflicts", lambda **kw: _failing_resolver(kw["commit"]))
    # Real cherry-pick runs (produces the conflict); skip gap analysis.
    from bpilot.gap_analyzer import GapResult

    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    rc = main(["port", sha_bad, "target", "--no-init", "--skip-baseline"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "pausing for manual resolution" in err

    # Cherry-pick state ended; failed file left as uncommitted changes.
    assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()
    status = _git(repo, "status", "--porcelain").stdout
    assert "app.py" in status

    from bpilot.session import load_session

    session = load_session(repo_root=repo)
    assert session is not None and session.paused
    assert session.paused_commit == sha_bad
    assert session.paused_failed_files == ["app.py"]
    assert session.remaining_commits == []

    report_md = (repo / "BACKPORT_REPORT.md").read_text()
    assert "PAUSED" in report_md
    assert "app.py" in report_md
    assert "--continue" in report_md


def test_port_continue_completes_single_commit(tmp_path, capsys, monkeypatch):
    """Case A: after manual fix + `git add`, --continue amends the paused
    commit and finishes the run (verification + gap analysis run)."""
    repo, sha_bad, _ = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult

    _green_checks(monkeypatch)
    monkeypatch.setattr(cli_mod, "resolve_conflicts", lambda **kw: _failing_resolver(kw["commit"]))
    gap_calls: list[dict] = []
    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: gap_calls.append(kw) or GapResult())

    rc = main(["port", sha_bad, "target", "--no-init", "--skip-baseline"])
    assert rc == 1

    # Manual resolution: write the merged content and stage it.
    (repo / "app.py").write_text("x = 2  # manually merged\n")
    _git(repo, "add", "app.py")
    monkeypatch.setenv("GIT_EDITOR", "true")  # don't open an editor in --amend

    rc = main(["port", "--continue", "--skip-baseline"])
    captured = capsys.readouterr()
    out = captured.out
    assert rc == 0, f"stderr: {captured.err}"
    assert "amending staged manual fixes" in out
    assert "backport complete" in out

    # Manual fix landed on the backport branch; gap analysis ran on continue.
    content = (repo / "app.py").read_text()
    assert "manually merged" in content
    assert len(gap_calls) >= 1

    from bpilot.session import load_session

    session = load_session(repo_root=repo)
    assert session is not None and not session.paused

    report_md = (repo / "BACKPORT_REPORT.md").read_text()
    assert "PAUSED" not in report_md


def test_port_continue_requires_staged_fixes(tmp_path, capsys, monkeypatch):
    """--continue with nothing staged (and paused commit not applied) errors."""
    repo, sha_bad, _ = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult

    _green_checks(monkeypatch)
    monkeypatch.setattr(cli_mod, "resolve_conflicts", lambda **kw: _failing_resolver(kw["commit"]))
    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    assert main(["port", sha_bad, "target", "--no-init", "--skip-baseline"]) == 1
    # User leaves the conflicted file unstaged.
    rc = main(["port", "--continue", "--skip-baseline"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no manual fixes staged" in err


def test_port_continue_resumes_remaining_commits(tmp_path, capsys, monkeypatch):
    """Case B: a multi-commit range pauses on the first commit; --continue
    finishes it and cherry-picks the rest of the queue."""
    repo, sha_bad, sha_ok = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult

    _green_checks(monkeypatch)
    monkeypatch.setattr(cli_mod, "resolve_conflicts", lambda **kw: _failing_resolver(kw["commit"]))
    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    rc = main(["port", f"{sha_bad} {sha_ok}", "target", "--no-init", "--skip-baseline"])
    assert rc == 1

    from bpilot.session import load_session

    session = load_session(repo_root=repo)
    assert session is not None and session.paused
    assert session.paused_commit == sha_bad
    assert session.remaining_commits == [sha_ok]

    # Manual fix + continue: rest of the queue is picked.
    (repo / "app.py").write_text("x = 2  # manually merged\n")
    _git(repo, "add", "app.py")
    monkeypatch.setenv("GIT_EDITOR", "true")

    rc = main(["port", "--continue", "--skip-baseline"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "backport complete" in out

    # The remaining clean commit was cherry-picked onto the branch.
    log = _git(repo, "log", "--format=%s", "target..HEAD").stdout
    assert "feature other change" in log
    assert (repo / "other.py").read_text().startswith("z = 2")


def test_port_continue_without_paused_session_errors(tmp_path, capsys, monkeypatch):
    """--continue with no (paused) session is a clear error."""
    repo, _, _ = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["port", "--continue"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no paused bpilot session" in err


# --- auto-stash of regenerable artifacts that block cherry-pick ---


def _lock_regen_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo with a `poetry.lock` that the verification command regenerates
    (dirties), plus a clean feature commit to backport."""
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "poetry.lock").write_text("lock v1\n")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "target")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "code.py").write_text("x = 2  # feature\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature change")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


def test_port_auto_stashes_regenerable_dirty_tree(tmp_path, capsys, monkeypatch):
    """When the baseline checks dirty the tree (e.g. tox regenerating
    poetry.lock), bpilot auto-stashes so the cherry-pick can proceed,
    and restores the stash after a successful run."""
    repo, sha = _lock_regen_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    def dirtying_checks(*, cwd, **kw):
        (cwd / "poetry.lock").write_text("lock REGENERATED\n")
        return VerificationResult(ok=True, commands=[])

    monkeypatch.setattr(cli_mod, "run_verification_checks", dirtying_checks)
    monkeypatch.setattr(
        cli_mod,
        "run_verification_checks_with_fixes",
        lambda **kw: FixResult(
            ok=True, iterations=[], final_result=VerificationResult(ok=True, commands=[])
        ),
    )
    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    rc = main(["port", sha, "target", "--no-llm", "--no-init"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stashed uncommitted changes" in out
    assert "restored auto-stashed changes" in out
    assert "backport complete" in out
    # Cherry-pick succeeded despite the dirty tree; lock was regenerated.
    assert (repo / "code.py").read_text() == "x = 2  # feature\n"
    assert "REGENERATED" in (repo / "poetry.lock").read_text()
    # Stash fully consumed (nothing left dangling).
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_port_pop_conflict_keeps_backport_version(tmp_path, capsys, monkeypatch):
    """When the backport itself modifies the file the stash dirtied, the
    pop conflicts; the backport's regenerated version wins and the stash
    is dropped (not left dangling)."""
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "poetry.lock").write_text("lock v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "target")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feature")
    # Feature branch modifies the lock too.
    (repo / "poetry.lock").write_text("lock from feature\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature lock change")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.gap_analyzer import GapResult
    from bpilot.validator import VerificationResult
    from bpilot.verification_fixer import FixResult

    def dirtying_checks(*, cwd, **kw):
        (cwd / "poetry.lock").write_text("lock REGENERATED\n")
        return VerificationResult(ok=True, commands=[])

    monkeypatch.setattr(cli_mod, "run_verification_checks", dirtying_checks)
    monkeypatch.setattr(
        cli_mod,
        "run_verification_checks_with_fixes",
        lambda **kw: FixResult(
            ok=True, iterations=[], final_result=VerificationResult(ok=True, commands=[])
        ),
    )
    monkeypatch.setattr(cli_mod, "analyze_gaps", lambda **kw: GapResult())

    rc = main(["port", sha, "target", "--no-llm", "--no-init"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "dropped in favour of the backport" in err
    # The lock reflects the backport's committed version, not the stashed copy.
    assert (repo / "poetry.lock").read_text() == "lock from feature\n"
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_port_git_error_still_writes_session(tmp_path, capsys, monkeypatch):
    """A non-conflict cherry-pick failure (git refuses) must still write a
    session + report, so the run is diagnosable and `finalize`/`reset` work."""
    repo, sha = _lock_regen_repo(tmp_path)
    monkeypatch.chdir(repo)

    from bpilot import cli as cli_mod
    from bpilot.git_ops import GitError
    from bpilot.validator import VerificationResult

    monkeypatch.setattr(
        cli_mod, "run_verification_checks", lambda **kw: VerificationResult(ok=True, commands=[])
    )

    def boom(commit, *, cwd):
        raise GitError(f"cherry-pick of {commit} failed without conflicts:\nfatal: simulated")

    monkeypatch.setattr(cli_mod, "cherry_pick", boom)

    rc = main(["port", sha, "target", "--no-llm", "--no-init", "--skip-baseline"])
    assert rc == 1
    assert (repo / ".bpilot" / "session.json").exists()
    assert "simulated" in (repo / "BACKPORT_REPORT.md").read_text()
