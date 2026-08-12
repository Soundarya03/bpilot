"""Command-line entry point for bpilot.

Three commands:
  bpilot port <commits> <target>   — produce a backport branch.
  bpilot finalize                  — learn from a finished backport.
  bpilot reset                     — discard session and return to original branch.

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
from pathlib import Path

from bpilot import __version__
from bpilot.config import load_config
from bpilot.git_ops import (
    GitError,
    abort_cherry_pick,
    checkout_branch,
    cherry_pick,
    continue_cherry_pick,
    create_backport_branch,
    create_backup_ref,
    current_branch,
    current_head,
    delete_branch,
    expand_commit_range,
    fetch_target_branch,
    get_changed_files,
    get_commit_message,
    has_cherry_pick_in_progress,
    is_git_repo,
    short_sha,
)
from bpilot.llm_client import LLMClient, LLMError
from bpilot.report import PortReport
from bpilot.resolver import resolve_conflicts
from bpilot.session import ConflictResolution, Session, clear_session, save_session
from bpilot.skill_loader import load_skill
from bpilot.validator import validate_changes


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command not in ("port", "finalize", "reset"):
        parser.print_help()
        return 2

    repo_root = Path.cwd()
    if not is_git_repo(repo_root):
        print("error: not inside a git repository", file=sys.stderr)
        return 2

    try:
        if args.command == "port":
            return _cmd_port(args, repo_root)
        if args.command == "finalize":
            return _cmd_finalize(args, repo_root)
        if args.command == "reset":
            return _cmd_reset(args, repo_root)
    except GitError as err:
        print(f"git error: {err}", file=sys.stderr)
        return 1

    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpilot",
        description="Intelligent backport helper (snap-packaged).",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    sub = parser.add_subparsers(dest="command")

    port = sub.add_parser("port", help="Backport <commits> onto <target-branch>.")
    port.add_argument("commits", help="Commit hash or range (e.g. abc123 or HEAD~3..HEAD).")
    port.add_argument("target", help="Target branch to backport onto.")
    port.add_argument(
        "--no-llm", action="store_true", help="Skip LLM inference; only cherry-pick."
    )
    port.add_argument(
        "--skill-file",
        default="bpilot/SKILL.md",
        help="Path to repo-specific skill file (default: bpilot/SKILL.md).",
    )
    port.add_argument("--model", default=None, help="Override configured model.")
    port.add_argument("--dry-run", action="store_true", help="Don't create branches; just report.")
    port.add_argument("--yes", action="store_true", help="Skip confirmation prompts (bot use).")

    fin = sub.add_parser("finalize", help="Learn from a finished backport.")
    fin.add_argument(
        "--no-llm", action="store_true", help="Classification only; no SKILL.md suggestion."
    )
    fin.add_argument("--commit", action="store_true", help="Commit the proposed SKILL.md change.")
    fin.add_argument("--yes", action="store_true", help="Skip confirmation prompts (bot use).")
    fin.add_argument("--model", default=None, help="Override configured model.")

    sub.add_parser("reset", help="Discard session and return to original branch.")
    sub.choices["reset"].add_argument(
        "--no-prompt", action="store_true", help="Skip confirmation prompt."
    )

    return parser


def _cmd_port(args: argparse.Namespace, repo_root: Path) -> int:
    """Orchestrate the `bpilot port` flow.

    Flow: fetch -> backup -> branch -> cherry-pick -> resolve conflicts
    (LLM) -> validate -> report. When --no-llm is set (or no API key
    is configured), LLM features are skipped: on conflict, the run
    aborts cleanly per BACKPORT_HELPER_PLAN.md §10.
    """
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

    # Load the SKILL.md for LLM context (resolver + gap analyzer).
    skill = None
    if use_llm:
        skill = load_skill(repo_root / args.skill_file)
        if skill is None:
            print(
                f"note: no skill file found at {args.skill_file}; "
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

    # 1. Fetch target.
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

    commits_id = short_sha(commits[0], cwd=repo_root)

    # 4. Create the backport branch.
    if args.dry_run:
        print(f"[dry-run] would create backport branch from {args.target}")
        print(f"[dry-run] would cherry-pick {len(commits)} commit(s): {', '.join(commits)}")
        return 0

    backport_branch = create_backport_branch(args.target, commits_id, cwd=repo_root)
    print(f"created backport branch: {backport_branch}")

    # 5. Cherry-pick each commit, resolving conflicts via LLM if enabled.
    results = []
    conflict_resolutions: list[ConflictResolution] = []
    aborted = False
    for sha in commits:
        print(f"cherry-picking {sha} ...")
        result = cherry_pick(sha, cwd=repo_root)
        results.append(result)
        if result.clean:
            print("  applied cleanly")
            continue
        if result.conflicts:
            print(f"  {result.message}: {result.conflicted_files}", file=sys.stderr)
            if not use_llm or llm is None:
                abort_cherry_pick(cwd=repo_root)
                print(
                    "conflict encountered and LLM is not enabled; "
                    "cherry-pick aborted. Resolve manually or rerun with "
                    "LLM features enabled (OPENROUTER_API_KEY).",
                    file=sys.stderr,
                )
                aborted = True
                break

            # LLM-assisted conflict resolution.
            commit_msg = get_commit_message(sha, cwd=repo_root)
            resolution = resolve_conflicts(
                commit=sha,
                conflicted_files=result.conflicted_files,
                target_branch=args.target,
                commit_message=commit_msg,
                llm=llm,
                skill=skill,
                cwd=repo_root,
            )

            for attempt in resolution.attempts:
                conflict_resolutions.append(
                    ConflictResolution(
                        commit=sha,
                        path=attempt.file_path,
                        attempts=attempt.attempt,
                        applied=attempt.succeeded,
                    )
                )

            if resolution.all_resolved:
                print("  all conflicts resolved; continuing cherry-pick")
                continue_cherry_pick(cwd=repo_root)
            else:
                print(
                    f"  could not resolve {len(resolution.failed_files)} file(s); "
                    f"aborting cherry-pick for manual intervention.",
                    file=sys.stderr,
                )
                abort_cherry_pick(cwd=repo_root)
                aborted = True
                break

    head_sha = current_head(cwd=repo_root)

    # 6. Post-cherry-pick validation (runs always — clean picks too).
    validation_notes: list[str] = []
    if not aborted:
        print("validating changes ...")
        changed_files = get_changed_files(args.target, cwd=repo_root)
        validation = validate_changes(
            changed_files=changed_files,
            cwd=repo_root,
            skill=skill,
        )
        for fr in validation.file_results:
            if not fr.ok:
                validation_notes.append(f"{fr.path}: {'; '.join(fr.errors)}")
        for cmd in validation.lock_regen:
            validation_notes.append(f"lock regen: {cmd}")
        for line in validation.lint_results:
            validation_notes.append(line)
        if validation.errors:
            validation_notes.extend(validation.errors)
        # If lock files were regenerated, amend the last commit to include them.
        if validation.lock_regen:
            print("  amending commit to include regenerated lock files ...")
            _stage_all_and_amend(cwd=repo_root)
            head_sha = current_head(cwd=repo_root)

    # 7. Snapshot for finalize.
    session = Session(
        port_head_sha=head_sha,
        target_branch=args.target,
        backport_branch=backport_branch,
        backup_ref=backup_ref,
        original_branch=original_branch,
        source_commits=commits,
        conflict_resolutions=conflict_resolutions,
    )
    save_session(session, repo_root=repo_root)

    # 8. Report.
    report = PortReport(
        source_commits=commits,
        target_branch=args.target,
        backport_branch=backport_branch,
        backup_ref=backup_ref,
        cherry_pick_results=results,
        no_llm=not use_llm,
    )
    if llm is not None:
        report.llm_usage = llm.usage
    report.validation_notes = validation_notes
    if aborted:
        report.errors.append(
            f"cherry-pick aborted at {results[-1].commit} due to unresolved conflicts."
        )
    report_path = report.write(repo_root)
    print(f"wrote {report_path}")

    if aborted:
        return 1
    print(f"backport complete on branch {backport_branch}")
    print(f"rollback with: git reset --hard {backup_ref}")
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


def _cmd_finalize(args: argparse.Namespace, repo_root: Path) -> int:
    """`bpilot finalize` — Phase 11 (stubbed).

    MVP: report that finalize is not yet implemented, but verify a
    session exists so the error path is graceful.
    """
    from bpilot.session import load_session

    session = load_session(repo_root=repo_root)
    if session is None:
        print(
            "no bpilot session found on this branch. Run `bpilot port <commits> <target>` first.",
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
