"""Command-line entry point for bpilot.

Commands:
  bpilot init                       — scaffold bpilot/ and .bpilot/, infer
                                       starter skills via LLM (first-run).
  bpilot port <commits> <target>    — produce a backport branch.
  bpilot finalize                    — learn from a finished backport.
  bpilot reset                       — discard session and return to original branch.

The orchestration here is intentionally thin: each phase (git ops, LLM
calls, gap analysis, reporting) lives in its own module. The CLI wires
them together and owns user-facing messages and exit codes.

The `port` flow runs end-to-end: fetch, backup, branch, cherry-pick,
LLM-assisted conflict resolution (when enabled), validation, and
reporting. When `--no-llm` is set (or no API key is configured), LLM
features are skipped: on conflict, the run aborts cleanly with a
reportable error, per BACKPORT_HELPER_PLAN.md §10.

Gap analysis (Phase 7) and finalize (Phase 11) are stubbed here for
forward-compatibility and will be filled in by their respective phases.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bpilot import __version__
from bpilot.config import load_config
from bpilot.gap_analyzer import analyze_gaps
from bpilot.git_ops import (
    CherryPickResult,
    GitError,
    abort_cherry_pick,
    checkout_branch,
    cherry_pick,
    commit_amend_staged,
    commit_partial_cherry_pick,
    continue_cherry_pick,
    create_backport_branch,
    create_backup_ref,
    current_branch,
    current_head,
    delete_branch,
    detect_conflicts,
    expand_commit_range,
    fetch_target_branch,
    get_changed_files,
    get_commit_message,
    get_commit_messages,
    has_cherry_pick_in_progress,
    is_applied,
    is_git_repo,
    is_index_clean,
    short_sha,
    stash_pop,
    stash_push,
)
from bpilot.init_skills import ensure_bpilot_dir, print_summary, run_init
from bpilot.llm_client import LLMClient, LLMError, UsageStats
from bpilot.report import PortReport
from bpilot.resolver import resolve_conflicts
from bpilot.session import (
    ConflictResolution,
    GapFinding,
    PotentialGap,
    Session,
    clear_session,
    load_session,
    save_session,
)
from bpilot.skill_loader import (
    DEFAULT_SKILLS_DIR,
    SkillLoadError,
    SkillSet,
    init_skills_dir,
    load_skill_set,
)
from bpilot.validator import VerificationResult, run_verification_checks, validate_changes
from bpilot.verification_fixer import FixResult, run_verification_checks_with_fixes


@dataclass
class _PortSetup:
    """Shared, run-independent setup for `port`: LLM client and skills."""

    llm: LLMClient | None
    skill_set: SkillSet | None
    use_llm: bool


@dataclass
class _PortRunState:
    """Mutable state carried through a `port` run (fresh or --continue)."""

    commits: list[str]
    target_branch: str
    backport_branch: str
    backup_ref: str
    original_branch: str
    results: list[CherryPickResult] = field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = field(default_factory=list)
    gap_findings: list[GapFinding] = field(default_factory=list)
    potential_gaps: list[PotentialGap] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    baseline_passed: bool = False
    aborted: bool = False
    paused: bool = False
    paused_commit: str = ""
    paused_failed_files: list[str] = field(default_factory=list)
    remaining_commits: list[str] = field(default_factory=list)
    stashed: bool = False
    errors: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command not in ("init", "port", "finalize", "reset"):
        parser.print_help()
        return 2

    repo_root = Path.cwd()
    if not is_git_repo(repo_root):
        print("error: not inside a git repository", file=sys.stderr)
        return 2

    try:
        if args.command == "init":
            return _cmd_init(args, repo_root)
        if args.command == "port":
            return _cmd_port(args, repo_root)
        if args.command == "finalize":
            return _cmd_finalize(args, repo_root)
        if args.command == "reset":
            return _cmd_reset(args, repo_root)
    except GitError as err:
        print(f"git error: {err}", file=sys.stderr)
        return 1
    except SkillLoadError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpilot",
        description="Intelligent backport helper (snap-packaged).",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser(
        "init",
        help="Scaffold bpilot/skills/ and .bpilot/, infer starter skills "
        "(intended for first-time setup in a project).",
    )
    init_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Scaffold placeholders only; skip LLM inference of skill content.",
    )
    init_parser.add_argument(
        "--skills-dir",
        default=str(DEFAULT_SKILLS_DIR),
        help="Path to the skills directory (default: bpilot/skills).",
    )
    init_parser.add_argument("--model", default=None, help="Override configured model.")
    init_parser.add_argument(
        "--yes", action="store_true", help="Skip confirmation prompts (bot use)."
    )

    port = sub.add_parser("port", help="Backport <commits> onto <target-branch>.")
    port.add_argument(
        "commits",
        nargs="?",
        default=None,
        help="Commit hash or range (e.g. abc123 or HEAD~3..HEAD). Omit with --continue.",
    )
    port.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Target branch to backport onto. Omit with --continue.",
    )
    port.add_argument(
        "--no-llm", action="store_true", help="Skip LLM inference; only cherry-pick."
    )
    port.add_argument(
        "--skills-dir",
        default=str(DEFAULT_SKILLS_DIR),
        help="Path to the skills directory (default: bpilot/skills).",
    )
    port.add_argument(
        "--no-init",
        action="store_true",
        help="Do not scaffold bpilot/skills/ when absent; run without repo-specific context.",
    )
    port.add_argument("--model", default=None, help="Override configured model.")
    port.add_argument("--dry-run", action="store_true", help="Don't create branches; just report.")
    port.add_argument("--yes", action="store_true", help="Skip confirmation prompts (bot use).")
    port.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip fetching the target branch from origin; backport onto the "
        "local target ref as-is. Useful in environments without git "
        "credentials (e.g. a dev VM): fetch elsewhere first, as the local "
        "ref may be stale.",
    )
    port.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the baseline verification checks on the unmodified target branch. "
        "Escape hatch for iterative runs with a known-sane environment or a known-flaky "
        "target branch. Without a baseline, post-backport check failures cannot be "
        "attributed to the backport.",
    )
    port.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="Resume a paused run: amend your staged manual fixes into the paused "
        "commit, cherry-pick any remaining commits, and continue the pipeline. "
        "<commits> and <target> are read from the session and may be omitted.",
    )

    fin = sub.add_parser("finalize", help="Learn from a finished backport.")
    fin.add_argument(
        "--no-llm", action="store_true", help="Classification only; no skill update proposal."
    )
    fin.add_argument("--commit", action="store_true", help="Commit the proposed skill change.")
    fin.add_argument("--yes", action="store_true", help="Skip confirmation prompts (bot use).")
    fin.add_argument("--model", default=None, help="Override configured model.")
    fin.add_argument(
        "--skills-dir",
        default=str(DEFAULT_SKILLS_DIR),
        help="Path to the skills directory (default: bpilot/skills).",
    )

    sub.add_parser("reset", help="Discard session and return to original branch.")
    sub.choices["reset"].add_argument(
        "--no-prompt", action="store_true", help="Skip confirmation prompt."
    )

    return parser


def _cmd_init(args: argparse.Namespace, repo_root: Path) -> int:
    """`bpilot init` — scaffold bpilot/ and .bpilot/, infer starter skills.

    Intended to be run once when first adopting bpilot in a project:
      1. Scaffolds `bpilot/skills/` with the five starter SKILL.md files
         (valid frontmatter + empty section bodies).
      2. Creates the `.bpilot/` session directory and adds it to .gitignore.
      3. When LLM is available, refines two of the skill files from repo
         content:
         - verification-checks: inferred from README, CONTRIBUTING.md,
           pyproject.toml, and other config files.
         - version-control: inferred from README + git branch / commit
           history.
         The other three skills (conflict-resolution, gap-analysis,
         general-context) are left as placeholders; they're learned over
         time via `finalize`.

    Refuses to overwrite an existing skills directory — `init` is a
    first-run command; re-running it is a no-op error so the user's
    hand-edited skill files are never clobbered. Use `bpilot reset` to
    discard session state (it never touches bpilot/skills/).
    """
    skills_dir = (repo_root / args.skills_dir).resolve()
    if skills_dir.is_dir():
        print(
            f"error: skills directory already exists at {args.skills_dir}. "
            "`bpilot init` is a first-run command; edit the existing "
            "bpilot/skills/*/SKILL.md files by hand instead.",
            file=sys.stderr,
        )
        return 1

    config = load_config(model_override=args.model)
    use_llm = not args.no_llm
    llm: LLMClient | None = None
    if use_llm:
        if not config.has_llm:
            print(
                "note: no OpenRouter API key configured; scaffolding "
                "placeholders only. Set OPENROUTER_API_KEY or "
                "`snap set bpilot openrouter-api-key=...` to enable LLM "
                "inference of starter skills.",
                file=sys.stderr,
            )
            use_llm = False
        else:
            try:
                llm = LLMClient(config)
            except LLMError as err:
                print(
                    f"warning: LLM unavailable ({err}); scaffolding placeholders only.",
                    file=sys.stderr,
                )
                use_llm = False

    print(f"initializing {args.skills_dir} ...", file=sys.stderr)
    result = run_init(repo_root=repo_root, skills_dir=skills_dir, llm=llm)

    # Create the .bpilot/ session directory and gitignore it.
    ensure_bpilot_dir(repo_root)

    print_summary(result)
    return 0 if not result.errors else 1


def _cmd_port(args: argparse.Namespace, repo_root: Path) -> int:
    """Orchestrate the `bpilot port` flow.

    Fresh run: fetch -> backup -> branch -> baseline -> cherry-pick loop
    -> validate -> verify -> gap analysis -> snapshot -> report.

    `--continue` run: reload the paused session, amend the user's staged
    fixes into the paused commit, resume the cherry-pick loop with the
    remaining commits, then run the same post-pick pipeline.

    When a commit's conflicts cannot all be auto-resolved, the run
    *pauses*: resolved files are committed, failed files are left in the
    working tree, and the report explains how to finish manually and
    resume with `--continue`. When --no-llm is set (or no API key is
    configured), LLM features are skipped: on conflict, the run aborts
    cleanly per BACKPORT_HELPER_PLAN.md §10.
    """
    if args.continue_run:
        return _port_continue(args, repo_root)
    return _port_fresh(args, repo_root)


def _setup_port_run(args: argparse.Namespace, repo_root: Path) -> _PortSetup:
    """Shared setup for fresh and --continue runs: config, skills, LLM client."""
    config = load_config(model_override=args.model)

    use_llm = not args.no_llm
    if use_llm and not config.has_llm:
        print(
            "note: no OpenRouter API key configured; running in --no-llm mode. "
            "Set OPENROUTER_API_KEY or `snap set bpilot openrouter-api-key=...` "
            "to enable LLM features.",
            file=sys.stderr,
        )
        use_llm = False

    # Load the skills directory for LLM context (resolver + gap analyzer).
    # First-run auto-init: scaffold bpilot/skills/ when absent (unless
    # --no-init), so a fresh project gets a concrete, human-editable file
    # tree. Init runs regardless of --no-llm: the scaffolded files are
    # useful even if this run doesn't use LLM features.
    skills_dir = (repo_root / args.skills_dir).resolve()
    if not skills_dir.is_dir() and not args.no_init:
        print(f"initializing {args.skills_dir} with starter SKILL.md files ...")
        init_skills_dir(skills_dir)
        print(
            f"edit {args.skills_dir}/*/SKILL.md to add "
            "repo-specific context, then re-run bpilot port."
        )

    skill_set: SkillSet | None = None
    if skills_dir.is_dir():
        skill_set = load_skill_set(skills_dir)
    elif use_llm:
        print(
            f"note: no skills directory found at {args.skills_dir}; "
            "LLM features will run without repo-specific context.",
            file=sys.stderr,
        )

    # Construct the LLM client (only used if conflicts arise).
    llm: LLMClient | None = None
    if use_llm:
        try:
            llm = LLMClient(config)
        except LLMError as err:
            print(f"warning: LLM unavailable ({err}); running in --no-llm mode.", file=sys.stderr)
            use_llm = False

    return _PortSetup(llm=llm, skill_set=skill_set, use_llm=use_llm)


def _port_fresh(args: argparse.Namespace, repo_root: Path) -> int:
    """Start a new backport: branch, baseline, then the pick loop."""
    if not args.commits or not args.target:
        print(
            "error: `bpilot port` requires <commits> and <target> "
            "(they may only be omitted with --continue).",
            file=sys.stderr,
        )
        return 2

    setup = _setup_port_run(args, repo_root)

    # 1. Fetch target (unless --no-fetch: use the local ref as-is).
    if args.no_fetch:
        print(f"skipping fetch (--no-fetch); using local ref {args.target!r} as-is")
    else:
        try:
            fetch_target_branch(args.target, cwd=repo_root)
        except GitError as err:
            print(f"error: could not fetch target branch {args.target!r}: {err}", file=sys.stderr)
            return 1

    # 2. Backup ref (always created, enables manual rollback).
    backup_ref = create_backup_ref(cwd=repo_root)
    original_branch = current_branch(cwd=repo_root)

    # 3. Expand the commit spec and resolve to SHAs.
    try:
        commits = expand_commit_range(args.commits, cwd=repo_root)
    except GitError as err:
        print(f"error: could not resolve commits {args.commits!r}: {err}", file=sys.stderr)
        return 1
    if not commits:
        print(f"error: no commits resolved from {args.commits!r}", file=sys.stderr)
        return 1

    # 4. Create the backport branch.
    commits_id = short_sha(commits[0], cwd=repo_root)
    if args.dry_run:
        print(f"[dry-run] would create backport branch from {args.target}")
        print(f"[dry-run] would cherry-pick {len(commits)} commit(s): {', '.join(commits)}")
        return 0

    backport_branch = create_backport_branch(args.target, commits_id, cwd=repo_root)
    print(f"created backport branch: {backport_branch}")

    # 3b. Baseline: verify the untouched target state passes its own checks.
    # Runs after branch creation (we must be on the target's code to test it)
    # and before the first cherry-pick (the branch is still identical to the
    # freshly-fetched target). A green baseline proves any post-backport
    # failure was introduced by the backport.
    baseline_passed = False
    if not args.skip_baseline:
        print("running baseline verification checks on unmodified target ...")
        baseline = run_verification_checks(cwd=repo_root, skill_set=setup.skill_set)
        if not baseline.ok:
            _abort_baseline_failure(
                baseline,
                target_branch=args.target,
                original_branch=original_branch,
                backport_branch=backport_branch,
                repo_root=repo_root,
            )
            return 1
        baseline_passed = True
    else:
        print("skipping baseline verification checks (--skip-baseline)")

    state = _PortRunState(
        commits=commits,
        target_branch=args.target,
        backport_branch=backport_branch,
        backup_ref=backup_ref,
        original_branch=original_branch,
    )
    state.baseline_passed = baseline_passed

    # The baseline (and any pre-existing) checks may leave regenerable
    # build artifacts dirty in the tree (e.g. `poetry.lock` rewritten by
    # `tox run -e format`), which would make git refuse the cherry-pick.
    # Stash them away; they're popped after the run (and the regenerated
    # versions from the post-pick validation win on any conflict).
    if stash_push(cwd=repo_root):
        state.stashed = True
        print("stashed uncommitted changes (regenerable artifacts) before cherry-pick")

    state = _run_cherry_pick_loop(state, setup=setup, cwd=repo_root)
    return _run_post_pipeline(state, setup=setup, args=args, repo_root=repo_root)


def _port_continue(args: argparse.Namespace, repo_root: Path) -> int:
    """Resume a paused run: fold in the user's manual fixes and continue.

    The paused session supplies <commits>/<target> (and any flags the
    original run used) when they're omitted on the command line.

    Preconditions (all verified before touching anything):
      - a paused session exists (and matches any explicitly-passed args),
      - HEAD is the session's backport branch,
      - no cherry-pick is in progress,
      - the user's manual fixes are staged (index non-empty) — or the
        paused commit is already patch-equivalent on the branch (user
        committed the fixes themselves),
      - no leftover conflict markers anywhere in the tree.
    """
    session = load_session(repo_root=repo_root)
    if session is None or not session.paused:
        print(
            "error: no paused bpilot session found. "
            "`--continue` resumes a run that paused on unresolved conflicts; "
            "run `bpilot port <commits> <target>` (without --continue) to start one.",
            file=sys.stderr,
        )
        return 1

    # Reconstruct the invocation from the session when args are omitted.
    saved = session.port_args
    if not args.commits:
        args.commits = saved.get("commits") or " ".join(session.source_commits)
    if not args.target:
        args.target = session.target_branch
    # Inherit boolean flags from the original run when not passed now.
    for flag in ("no_llm", "skip_baseline", "no_fetch", "no_init"):
        if not getattr(args, flag, False) and saved.get(flag):
            setattr(args, flag, True)
    if args.model is None:
        args.model = saved.get("model")

    # If the user passed explicit positionals, they must match the session.
    if args.commits != saved.get("commits") and saved.get("commits"):
        try:
            given = expand_commit_range(args.commits, cwd=repo_root)
        except GitError as err:
            print(f"error: could not resolve commits {args.commits!r}: {err}", file=sys.stderr)
            return 1
        if given != session.source_commits:
            print(
                "error: --continue <commits> do not match the paused session.\n"
                f"  session: {' '.join(session.source_commits)}\n"
                f"  invoked: {' '.join(given)}",
                file=sys.stderr,
            )
            return 1
    if args.target != session.target_branch:
        print(
            f"error: --continue target {args.target!r} does not match the paused "
            f"session's target {session.target_branch!r}.",
            file=sys.stderr,
        )
        return 1
    commits = session.source_commits

    if current_branch(cwd=repo_root) != session.backport_branch:
        print(
            f"error: not on the backport branch {session.backport_branch!r}. "
            "Check it out before running --continue.",
            file=sys.stderr,
        )
        return 1

    if has_cherry_pick_in_progress(cwd=repo_root):
        print(
            "error: a cherry-pick is in progress. Resolve it with "
            "`git cherry-pick --continue` (or --abort), then re-run --continue.",
            file=sys.stderr,
        )
        return 1

    leftover = detect_conflicts(cwd=repo_root)
    if leftover:
        print(
            f"error: conflict markers still present in: {', '.join(leftover)}. "
            "Resolve and `git add` them before --continue.",
            file=sys.stderr,
        )
        return 1

    setup = _setup_port_run(args, repo_root)

    # Fold the user's manual fixes into the paused commit.
    if not is_index_clean(cwd=repo_root):
        print(f"amending staged manual fixes into {session.paused_commit} ...")
        try:
            commit_amend_staged(cwd=repo_root)
        except GitError as err:
            print(f"error: could not amend manual fixes: {err}", file=sys.stderr)
            return 1
    elif is_applied(session.paused_commit, cwd=repo_root):
        print(
            f"paused commit {session.paused_commit} is already fully applied; "
            "nothing staged to amend."
        )
    else:
        print(
            "error: no manual fixes staged. Resolve the failed files, "
            "`git add` them, then re-run --continue.\n"
            f"  failed files: {', '.join(session.paused_failed_files)}",
            file=sys.stderr,
        )
        return 1

    state = _PortRunState(
        commits=session.source_commits,
        target_branch=session.target_branch,
        backport_branch=session.backport_branch,
        backup_ref=session.backup_ref,
        original_branch=session.original_branch,
        conflict_resolutions=list(session.conflict_resolutions),
        gap_findings=list(session.gap_findings),
        potential_gaps=list(session.potential_gaps),
        usage=UsageStats(**session.llm_usage) if session.llm_usage else UsageStats(),
    )
    state.results.append(
        CherryPickResult(
            commit=session.paused_commit,
            applied=True,
            message="completed via --continue (manual resolution)",
        )
    )
    # --continue resumes an already-baselined branch: skip the baseline
    # gate but don't claim it passed for the fixer.
    state.baseline_passed = False

    # `remaining_commits` is recorded on pause (commits after the paused
    # one). It may legitimately be empty (the paused commit was the last),
    # in which case there is nothing left to pick — the amend above was
    # the whole job. Do NOT fall back to re-deriving a queue from
    # is_applied: the paused commit's patch-id changed on amend, so it
    # would be wrongly re-picked.
    queue = list(session.remaining_commits)
    if queue:
        print(f"resuming with {len(queue)} remaining commit(s)")
    state = _run_cherry_pick_loop(state, setup=setup, cwd=repo_root, queue=queue)
    return _run_post_pipeline(state, setup=setup, args=args, repo_root=repo_root)

def _run_cherry_pick_loop(
    state: _PortRunState,
    *,
    setup: _PortSetup,
    cwd: Path,
    queue: list[str] | None = None,
) -> _PortRunState:
    """Cherry-pick `queue` (default: all of state.commits), resolving
    conflicts via the LLM when enabled.

    On a partial resolution the run *pauses*: the resolved files are
    committed (preserving the original commit message), the failed files
    are left in the working tree, and `state.paused_*` is set so the
    caller can persist a resumable session.
    """
    for sha in queue if queue is not None else state.commits:
        print(f"cherry-picking {sha} ...")
        try:
            result = cherry_pick(sha, cwd=cwd)
        except GitError as err:
            # Non-conflict pick failure (git refused before/without marking
            # conflicts). cherry_pick() has already aborted the pick. Record
            # it as an abort so the session + report are still written, then
            # stop the loop — the run cannot continue past a failed pick.
            print(f"  git error: {err}", file=sys.stderr)
            state.results.append(
                CherryPickResult(commit=sha, applied=False, message=str(err))
            )
            state.errors.append(str(err))
            state.aborted = True
            break
        state.results.append(result)
        if result.clean:
            print("  applied cleanly")
            state.applied.append(sha)
            continue
        if not result.conflicts:
            continue

        print(f"  {result.message}: {result.conflicted_files}", file=sys.stderr)
        if not setup.use_llm or setup.llm is None:
            abort_cherry_pick(cwd=cwd)
            print(
                "conflict encountered and LLM is not enabled; "
                "cherry-pick aborted. Resolve manually or rerun with "
                "LLM features enabled (OPENROUTER_API_KEY).",
                file=sys.stderr,
            )
            state.aborted = True
            break

        # LLM-assisted conflict resolution.
        commit_msg = get_commit_message(sha, cwd=cwd)
        resolution = resolve_conflicts(
            commit=sha,
            conflicted_files=result.conflicted_files,
            target_branch=state.target_branch,
            commit_message=commit_msg,
            llm=setup.llm,
            skill_set=setup.skill_set,
            cwd=cwd,
        )

        for attempt in resolution.attempts:
            state.conflict_resolutions.append(
                ConflictResolution(
                    commit=sha,
                    path=attempt.file_path,
                    attempts=attempt.attempt,
                    applied=attempt.succeeded,
                )
            )

        if resolution.all_resolved:
            print("  all conflicts resolved; continuing cherry-pick")
            continue_cherry_pick(cwd=cwd)
            state.applied.append(sha)
            continue

        # Partial failure: keep the resolved work, pause for manual fixes.
        # `git commit` ends the cherry-pick state (git clears
        # CHERRY_PICK_HEAD); the failed files stay as uncommitted
        # working-tree changes for the user to fix and stage.
        print(
            f"  could not auto-resolve {len(resolution.failed_files)} file(s): "
            f"{resolution.failed_files}",
            file=sys.stderr,
        )
        print(
            "  committing resolved files and pausing for manual resolution.\n"
            "  Fix the files above, `git add` them, then resume with:\n"
            f"    bpilot port <commits> {state.target_branch} --continue",
            file=sys.stderr,
        )
        commit_partial_cherry_pick(commit_msg, resolution.failed_files, cwd=cwd)
        state.paused_commit = sha
        state.paused_failed_files = list(resolution.failed_files)
        full_queue = queue if queue is not None else state.commits
        state.remaining_commits = full_queue[full_queue.index(sha) + 1 :]
        state.paused = True
        break

    return state


def _run_post_pipeline(
    state: _PortRunState,
    *,
    setup: _PortSetup,
    args: argparse.Namespace,
    repo_root: Path,
) -> int:
    """Validation, verification, gap analysis, session snapshot, report.

    Runs after any cherry-pick loop — fresh, --continue, and even paused
    runs (a paused run still leaves commits on the branch that should be
    validated). Skips the mutation phases when the loop aborted with no
    LLM (the tree was rolled back).
    """
    head_sha = current_head(cwd=repo_root)
    proceed = not state.aborted

    # Post-cherry-pick validation (runs whenever there is tree state to check).
    validation_notes: list[str] = []
    verification_notes: list[str] = []
    if proceed:
        print("validating changes ...")
        changed_files = get_changed_files(state.target_branch, cwd=repo_root)
        validation = validate_changes(
            changed_files=changed_files,
            cwd=repo_root,
            skill_set=setup.skill_set,
        )
        for fr in validation.file_results:
            if not fr.ok:
                validation_notes.append(f"{fr.path}: {'; '.join(fr.errors)}")
        for cmd in validation.lock_regen:
            validation_notes.append(f"lock regen: {cmd}")
        if validation.errors:
            validation_notes.extend(validation.errors)
        # If lock files were regenerated, amend the last commit to include them.
        if validation.lock_regen:
            print("  amending commit to include regenerated lock files ...")
            _stage_all_and_amend(cwd=repo_root)
            head_sha = current_head(cwd=repo_root)

        # Verification checks (format, lint, unit tests) + LLM fix loop.
        print("running verification checks (format / lint / unit tests) ...")
        fix_result = run_verification_checks_with_fixes(
            cwd=repo_root,
            skill_set=setup.skill_set,
            llm=setup.llm,
            changed_files=changed_files,
            use_llm=setup.use_llm,
            baseline_passed=state.baseline_passed,
        )
        verification_notes.extend(_format_verification_notes(fix_result))
        # If the LLM made any file changes, fold them into the latest commit
        # so the branch stays clean (whether or not all checks ultimately pass).
        if any(it.files_changed for it in fix_result.iterations if it.llm_called):
            print("  amending commit to include LLM verification-check fixes ...")
            _stage_all_and_amend(cwd=repo_root)
            head_sha = current_head(cwd=repo_root)

    # Gap analysis — runs on the verified branch state.
    gap_findings: list[GapFinding] = list(state.gap_findings)
    potential_gaps: list[PotentialGap] = list(state.potential_gaps)
    gap_skipped = False
    gap_skip_reason = ""
    gap_verification_notes: list[str] = []
    # `no_llm` drives the "--no-llm" gap-section rendering; `gap_skipped`
    # is reserved for skill-level skips (missing skill / empty checklist)
    # so they render as "Skipped (<reason>)" rather than "No gaps found."
    if proceed and setup.use_llm and setup.llm is not None:
        print("running gap analysis ...")
        gap_result = analyze_gaps(
            target_branch=state.target_branch,
            source_commits=state.commits,
            commit_messages=get_commit_messages(state.commits, cwd=repo_root),
            skill_set=setup.skill_set,
            llm=setup.llm,
            cwd=repo_root,
        )
        gap_findings = gap_result.findings
        potential_gaps = gap_result.potential_gaps
        if gap_result.skipped:
            gap_skipped = True
            gap_skip_reason = gap_result.skip_reason
        if any(f.applied for f in gap_result.findings):
            # Post-gap verification re-run (once, no LLM fix loop).
            print("re-running verification checks after gap fixes ...")
            post = run_verification_checks(cwd=repo_root, skill_set=setup.skill_set)
            if not post.ok:
                gap_verification_notes = _suspect_gap_commits(
                    repo_root=repo_root,
                    failures=post,
                    gap_findings=gap_result.findings,
                )
            head_sha = current_head(cwd=repo_root)
        elif gap_result.llm_errors:
            gap_verification_notes.append(
                "gap analysis encountered LLM errors on "
                f"{len(gap_result.llm_errors)} item(s); see stderr."
            )
            for err in gap_result.llm_errors:
                print(f"  gap analysis error: {err}", file=sys.stderr)

    # Snapshot for finalize / --continue.
    session = Session(
        port_head_sha=head_sha,
        target_branch=state.target_branch,
        backport_branch=state.backport_branch,
        backup_ref=state.backup_ref,
        original_branch=state.original_branch,
        source_commits=state.commits,
        conflict_resolutions=state.conflict_resolutions,
        gap_findings=gap_findings,
        potential_gaps=potential_gaps,
        llm_usage=asdict(state.usage) if state.usage.calls else {},
        paused_commit=state.paused_commit,
        paused_failed_files=state.paused_failed_files,
        remaining_commits=state.remaining_commits,
        port_args={
            "commits": args.commits or "",
            "model": args.model,
            "no_llm": bool(args.no_llm),
            "skip_baseline": bool(args.skip_baseline),
            "no_fetch": bool(args.no_fetch),
            "no_init": bool(args.no_init),
        },
    )
    save_session(session, repo_root=repo_root)

    # Report.
    report = PortReport(
        source_commits=state.commits,
        target_branch=state.target_branch,
        backport_branch=state.backport_branch,
        backup_ref=state.backup_ref,
        cherry_pick_results=state.results,
        conflict_resolutions=state.conflict_resolutions,
        no_llm=not setup.use_llm,
        paused_commit=state.paused_commit,
        paused_failed_files=state.paused_failed_files,
    )
    if setup.llm is not None:
        report.llm_usage = setup.llm.usage
    report.validation_notes = validation_notes
    report.verification_notes = verification_notes
    report.gap_findings = gap_findings
    report.potential_gaps = potential_gaps
    report.gap_skipped = gap_skipped
    report.gap_skip_reason = gap_skip_reason
    report.gap_verification_notes = gap_verification_notes
    if state.aborted and not state.errors:
        report.errors.append(
            f"cherry-pick aborted at {state.results[-1].commit} due to unresolved conflicts."
        )
    for err in state.errors:
        report.errors.append(err)
    if state.paused:
        report.errors.append(
            f"paused at {state.paused_commit}: {len(state.paused_failed_files)} "
            "file(s) need manual resolution; resume with `bpilot port ... --continue`."
        )
    report_path = report.write(repo_root)
    print(f"wrote {report_path}")

    if state.aborted:
        # The pick was rolled back; restore anything we auto-stashed so the
        # user's tree isn't left stashed after a failed run.
        if state.stashed:
            stash_pop(cwd=repo_root)
        return 1
    if state.paused:
        print(
            f"backport paused on branch {state.backport_branch}: "
            f"{len(state.paused_failed_files)} file(s) need manual resolution."
        )
        print(
            f"fix, `git add`, then resume with: "
            f"bpilot port <commits> {state.target_branch} --continue"
        )
        return 1

    # Run completed: restore anything we auto-stashed before the picks.
    # The backport's own regenerated artifacts win if the pop conflicts.
    if state.stashed:
        if stash_pop(cwd=repo_root):
            print("restored auto-stashed changes")
        else:
            print(
                "note: auto-stashed changes were dropped in favour of the "
                "backport's regenerated files (run `git stash list` on the "
                "original branch if you need them).",
                file=sys.stderr,
            )

    print(f"backport complete on branch {state.backport_branch}")
    print(f"rollback with: git reset --hard {state.backup_ref}")
    return 0


def _stage_all_and_amend(*, cwd: Path) -> None:
    """Stage all changes and amend the current commit.

    Used after lock file regeneration to fold the updated lock into the
    cherry-picked commit.
    """
    import os

    from bpilot.git_ops import _run

    _run(["add", "-u"], cwd=cwd)
    env = dict(os.environ)
    env["GIT_EDITOR"] = "true"
    _run(["commit", "--amend", "--no-edit"], cwd=cwd, env=env)


# Truncate each failing command's combined output to this many lines in the
# triage message. Failures almost always surface at the end of test output;
# the full output is available by re-running the command manually.
MAX_BASELINE_OUTPUT_LINES = 40


def _abort_baseline_failure(
    baseline: VerificationResult,
    *,
    target_branch: str,
    original_branch: str,
    backport_branch: str,
    repo_root: Path,
) -> None:
    """Handle a baseline-check failure: print the triage message and clean up.

    1. Print the triage message (causes a/b/c + failing commands + truncated
       output) to stderr.
    2. Restore the original branch.
    3. Force-delete the backport branch.
    4. Wipe any session state in ``.bpilot/`` and ``BACKPORT_REPORT.md``.

    The caller then exits 1. No new session is written and no report is
    generated — nothing was backported. Cleanup is best-effort: a failure
    in one git step (e.g. a dirty tree blocking checkout) is reported as a
    warning but does not skip the remaining steps, so the user is never
    left with stale session data pointing at a half-cleaned-up branch.
    The backup ref created at the start of ``port`` is left in place for
    manual recovery if any cleanup step failed.
    """
    print(
        f"error: verification checks failed on the unmodified target branch "
        f"({target_branch}).\n"
        "The backport cannot proceed until these pass. Possible causes:\n"
        "\n"
        "  a) Missing dependencies — ensure the tools these commands need are\n"
        "     installed and available in the current environment\n"
        "     (e.g. `tox`, `poetry install`, ...).\n"
        "  b) Incorrect verification commands — review the commands in\n"
        "     bpilot/skills/verification-checks/SKILL.md. Note: if they were\n"
        "     auto-generated by `bpilot init`, they are inferred guesses and\n"
        "     require human review.\n"
        "  c) The target branch itself is broken — run the commands manually\n"
        f"     on {target_branch} to confirm.\n"
        "\n"
        "Failing commands:",
        file=sys.stderr,
    )
    for f in baseline.failures:
        print(f"  [FAIL] {f.command}  (exit {f.returncode})", file=sys.stderr)
        out = f.output
        if out:
            lines = out.splitlines()
            if len(lines) > MAX_BASELINE_OUTPUT_LINES:
                lines = lines[-MAX_BASELINE_OUTPUT_LINES:]
            print("\n".join(lines), file=sys.stderr)

    print("rolling back baseline abort ...", file=sys.stderr)
    cleanup_errors: list[str] = []
    try:
        checkout_branch(original_branch, cwd=repo_root)
    except GitError as err:
        cleanup_errors.append(f"could not check out {original_branch!r}: {err}")
    try:
        delete_branch(backport_branch, cwd=repo_root, force=True)
    except GitError as err:
        cleanup_errors.append(f"could not delete backport branch {backport_branch!r}: {err}")
    # Always wipe session state, even if the git ops above failed — a
    # baseline failure means nothing was backported, so any pre-existing
    # session.json / BACKPORT_REPORT.md (e.g. from a prior `port` run) is
    # stale and would mislead `bpilot reset` / `bpilot finalize`.
    clear_session(repo_root=repo_root)
    for msg in cleanup_errors:
        print(f"warning: {msg}", file=sys.stderr)
    if cleanup_errors:
        print(
            "manual cleanup may be required; the bpilot/backup/... ref "
            "remains for rollback.",
            file=sys.stderr,
        )


def _format_verification_notes(fix_result: FixResult) -> list[str]:
    """Render a FixResult into human-readable lines for the report."""
    notes: list[str] = []
    if fix_result.skipped:
        notes.append("verification checks: LLM fix loop skipped (--no-llm or no API key)")
    if not fix_result.iterations:
        notes.append("verification checks: not run")
        return notes
    last = fix_result.iterations[-1]
    total = len(last.check_result.commands)
    passed = sum(1 for c in last.check_result.commands if c.ok)
    notes.append(
        f"verification checks: {passed}/{total} command(s) passing "
        f"after {fix_result.attempts_used} iteration(s)"
    )
    for cr in last.check_result.commands:
        tag = "pass" if cr.ok else "FAIL"
        notes.append(f"  [{tag}] {cr.command}")
    if not fix_result.ok and fix_result.remaining_failures:
        notes.append("remaining failures:")
        for line in fix_result.remaining_failures.splitlines():
            notes.append(f"  {line}")
    return notes


def _suspect_gap_commits(
    *,
    repo_root: Path,
    failures: VerificationResult,
    gap_findings: list[GapFinding],
) -> list[str]:
    """Heuristic: flag `bpilot(gap):` commits touching files named in failing
    command output. Advises the user to re-run tests and consider dropping
    flagged commits. Nothing is auto-reverted.
    """
    import re as _re

    from bpilot.git_ops import _run

    notes: list[str] = ["post-gap verification re-run failed; suspect gap-fix commits:"]
    # Collect file-like tokens from failing command output.
    referenced: set[str] = set()
    pattern = _re.compile(r"(\w[\w/\-\.]*\.(?:py|js|ts|go|rs|rb|java|c|cc|cpp|h|hpp))")
    for cr in failures.failures:
        referenced.update(pattern.findall(cr.output))
    if not referenced:
        notes.append("  (could not attribute failures to specific files)")
        return notes
    flagged: list[str] = []
    for finding in gap_findings:
        if not finding.applied or not finding.commit:
            continue
        # Inspect the commit's touched files.
        proc = _run(["show", "--stat", "--name-only", "--format=", finding.commit], cwd=repo_root)
        touched = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        if touched & referenced:
            flagged.append(finding.commit)
    if flagged:
        for sha in flagged:
            notes.append(f"  - `{sha}` touches files named in failing checks; re-run tests")
    else:
        notes.append("  (no applied gap-fix commit touches the referenced files)")
    return notes


def _cmd_finalize(args: argparse.Namespace, repo_root: Path) -> int:
    """`bpilot finalize` — Phase 11 (stubbed).

    MVP: report that finalize is not yet implemented, but verify a
    session exists so the error path is graceful. Finalize does not
    auto-init the skills directory — it requires an existing session
    (which implies `port` has already run and already scaffolded).
    """
    from bpilot.session import load_session

    session = load_session(repo_root=repo_root)
    if session is None:
        print(
            "no bpilot session found on this branch. Run `bpilot port <commits> <target>` first.",
            file=sys.stderr,
        )
        return 1

    # Finalize needs the skills directory (it proposes per-skill diffs).
    skills_dir = (repo_root / args.skills_dir).resolve()
    if not skills_dir.is_dir():
        print(
            f"no skills directory found at {args.skills_dir}; run `bpilot port` first.",
            file=sys.stderr,
        )
        return 1

    print("finalize is not yet implemented (see BACKPORT_HELPER_PLAN.md Phase 11).")
    print(f"session: port_head_sha={session.port_head_sha} target={session.target_branch}")
    return 0


def _cmd_reset(args: argparse.Namespace, repo_root: Path) -> int:
    """`bpilot reset` — discard session and return to the original branch.

    1. Abort any in-progress cherry-pick.
    2. Switch back to the branch the user was on before `port`.
    3. Force-delete the backport branch.
    4. Remove `.bpilot/` and `BACKPORT_REPORT.md`.
    """
    from bpilot.session import load_session

    session = load_session(repo_root=repo_root)

    if session is None:
        print("no bpilot session found — nothing to reset.")
        return 0

    # Confirm with the user unless --no-prompt.
    if not args.no_prompt:
        print("This will:")
        print(f"  - switch back to branch '{session.original_branch}'")
        print(f"  - force-delete backport branch '{session.backport_branch}'")
        print("  - remove .bpilot/ and BACKPORT_REPORT.md")
        response = input("Proceed? [y/N] ")
        if response.lower() != "y":
            print("aborted.")
            return 0

    # 1. Abort any in-progress cherry-pick.
    if has_cherry_pick_in_progress(cwd=repo_root):
        print("aborting in-progress cherry-pick ...")
        abort_cherry_pick(cwd=repo_root)

    # 2. Switch back to the original branch.
    target_branch = session.original_branch or session.target_branch
    print(f"checking out {target_branch} ...")
    checkout_branch(target_branch, cwd=repo_root)

    # 3. Force-delete the backport branch.
    print(f"deleting branch {session.backport_branch} ...")
    delete_branch(session.backport_branch, cwd=repo_root, force=True)

    # 4. Clean up session artifacts.
    clear_session(repo_root=repo_root)
    print("reset complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
