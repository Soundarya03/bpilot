# Backport Helper — Problem Statement & Implementation Plan

## Problem Statement

Backporting changes from one branch to another is a routine but error-prone task in software maintenance. A feature or fix developed on a `main` or `edge` branch often needs to be applied to one or more stable release branches. While `git cherry-pick` handles the mechanical part, it fails to account for:

1. **Merge conflicts** requiring manual resolution, which is tedious and introduces risk.
2. **Structural divergence** between branches — the target branch may lack a code path that the source branch has (e.g., a method that exists in 8.4 but was refactored away in 8.0), so the cherry-pick applies cleanly but the behaviour is incomplete.
3. **Logical gaps** — the hardest class of problem. The backported change may be syntactically correct but semantically incomplete because the target branch has different lifecycle hooks, different defaults, or different upgrade paths that the source branch doesn't account for.

### Concrete example (from this session)

The "enable metrics exporter by default" PR (#391) was originally made on the 8.4 branch of `canonical/mysql-operators`. When backporting to 8.0:

- **Cherry-pick succeeded** — most changes applied cleanly.
- **One call was missing** — `connect_mysql_exporter()` was not invoked in `workload_initialise` because 8.0's method has a single bootstrapping path, while 8.4 had an additional `is_data_dir_initialised()` shortcut branch. The cherry-pick of the 8.4 change didn't add the call to the 8.0-specific path.
- **A logical gap remained** — even after adding the call to `workload_initialise`, the `start` hook (which calls `workload_initialise`) is deferred during charm upgrades by a `upgrade.idle` guard in `_can_start`. So upgrading an existing 8.0 deployment wouldn't start the exporter. This gap only became apparent after reasoning about the upgrade lifecycle — something no cherry-pick or conflict resolver would catch.

This kind of gap — where the *semantic context* of the target branch differs from the source — is where an LLM-powered assistant adds real value over plain git tooling.

## Goals

1. **Two commands only** — `bpilot port` does the backport; `bpilot finalize` learns from it. No sprawling subcommand surface.
2. **Automate the mechanical parts** — branch creation, cherry-pick, conflict detection.
3. **LLM-assist only when needed** — don't invoke LLM inference for clean cherry-picks; only for conflict resolution, gap analysis, and finalize-time learning.
4. **Make it repo-agnostic** — use a user-authored `SKILL.md` file to encode repo-specific knowledge (lifecycle hooks, branch conventions, upgrade paths, test commands).
5. **Close the feedback loop** — after the human verifies/modifies the backport, `finalize` diffs what bpilot suggested vs. what was accepted, and proposes SKILL.md updates so the tool gets smarter over time (always human-reviewed; never auto-applied to the skill file).
6. **Package as a snap** — installable locally, usable as a comment-driven GH bot (`/port`, `/finalize`).
7. **Optionally skip LLM** — a `--no-llm` flag for offline / cost-free runs that only does the mechanical cherry-pick.
8. **Hard trust boundary** — the LLM is text-in/text-out only: it never sees git credentials and never executes git. All repo mutations go through the deterministic git-ops layer. bpilot never merges.

## Non-Goals (v1)

- **Merging, ever.** bpilot never runs `git merge`, never pushes to the target branch, and never merges a PR (the GH bot only opens draft PRs). Merge is always a human operation.
- Supporting non-git version control systems.
- Multi-target backport orchestration (backporting to N branches at once) — v1 is one target at a time.
- Semantic code rewriting across languages (e.g., auto-translating Python to Go) — out of scope.

## Proposed Name

**`bpilot`** — short for "backport pilot." Alternatives considered: `backport-helper` (descriptive but verbose), `bp` (too short, conflicts with other tools), `cherry` (too generic). `bpilot` is distinctive, easy to type, and conveys guidance.

CLI: `bpilot port <commit-range> <target-branch>` and `bpilot finalize`

## Architecture Overview

Two commands. `port` produces the backport; `finalize` learns from it.

```
bpilot port <commits> <target-branch>
│
├─▶ Git Operations (fetch, backup ref, branch, cherry-pick,
│                    merge-commit detect)
│
├─▶ Conflicts?
│   ├─ yes ─▶ LLM Resolver (complete-file rewrite + per-file validate
│   │         as retry gate, bounded retries; LLM never runs git)
│   └─ no ──────────┐       │   
│                   ▼       ▼ 
├─▶ Verification Checks (format + lint + unit tests,
│    per SKILL.md "Verification Checks" section)
│   └─▶ fail? ─▶ LLM Fixer (bounded loop, MAX 5 iterations:
│                 gather failures → propose fixes → apply → re-run checks)
│                 └─▶ still failing after 5 tries → surface in report,
│                      leave branch for manual intervention
│
├─▶ Gap Analyzer (checklist-driven, per SKILL.md "Things to Check")
│    — runs on the verified branch state
│
├─▶ Gap fixes applied as labelled commits (bpilot(gap): ...)
│
├─▶ Session Snapshot (.bpilot/session.json, or PR body for the bot:
│                      original head, gap findings, resolutions)
│
└─▶ BACKPORT_REPORT.md

            ⟦ human reviews (incl. gap-fix commits), edits, tests ⟧

bpilot finalize
│
├─▶ Retrieve session snapshot (.bpilot/session.json / PR body)
├─▶ Diff current branch state vs. snapshot
├─▶ Classify: applied suggestions / rejected suggestions /
│             human-added changes
├─▶ LLM analyzes human-added changes
│
└─▶ Propose SKILL.md updates (human reviews, commits, raises PR)
```

## Command UX

bpilot has exactly two commands. Both are run from the root of a project.

### `bpilot port <commit(s)> <target-branch>`

Does the backport: fetch → branch → cherry-pick → resolve conflicts → validate → gap analysis → snapshot → report.

**Local CLI usage:**
```bash
bpilot port abc123 8.0/edge          # single commit
bpilot port HEAD~3..HEAD 8.0/edge    # commit range
```

Produces:
- Local branch `backport/<short-hash>-to-<target>` with the cherry-picked + resolved changes, plus one labelled `bpilot(gap): ...` commit per applied gap fix.
- `BACKPORT_REPORT.md` summarizing what was done, gaps found (each mapped to its commit), and token cost.
- A session snapshot (`.bpilot/session.json`) recording bpilot's end state, used later by `finalize`.

The user then reviews the branch (including the labelled gap-fix commits — drop any with `git rebase -i`/`git reset`), makes manual edits, and runs tests.

**GH bot usage:**
Comment `/port <target-branch>` on a **merged** PR. The bot:
1. Runs the equivalent of `bpilot port <merge-commit-sha> <target-branch>`.
2. Pushes the backport branch.
3. Opens a **draft PR** to `<target-branch>`, tagging the commenter.
4. Embeds the session snapshot in the PR body (hidden HTML comment) so `/finalize` can retrieve it later.

The commenter (or any reviewer) can then push additional commits on top of bpilot's work in the draft PR.

### `bpilot finalize`

Learns from the finished backport: retrieves the snapshot → diffs against current state → classifies the delta → proposes SKILL.md updates.

**Local CLI usage:**
```bash
# After verifying / modifying the backport and running tests:
bpilot finalize
```

Produces:
- A classification of what changed since `port`:
  - **Applied suggestions** — gap fixes the user applied (bpilot was right).
  - **Rejected suggestions** — gap fixes the user didn't apply (bpilot may have been wrong, or the gap wasn't real).
  - **Human-added changes** — changes the user made that bpilot didn't suggest (candidate SKILL.md gaps).
- A proposed `SKILL.md` diff derived from the human-added changes (LLM-generated, human-reviewed).
- The user reviews, commits, and raises the PR manually.

**GH bot usage:**
Comment `/finalize` on the **draft PR** (after pushing any additional commits). The bot:
1. Runs the equivalent of `bpilot finalize --commit`.
2. Pushes a commit containing the proposed `SKILL.md` changes to the same draft PR.
3. Comments a summary of what was learned (applied / rejected / human-added).

Reviewers then review the full PR — both the backported changes and the SKILL.md suggestions — modify as needed, and merge.

### Session state

`finalize` needs to know where bpilot's work ended so it can diff. The snapshot is stored in two context-appropriate ways:

- **Local CLI:** an untracked file `.bpilot/session.json`. The common local flow — `port`, review, `finalize`, all on the same machine and working tree — needs nothing fancier. `.bpilot/` is added to `.gitignore` so it never pollutes the branch.
- **GH bot:** a hidden HTML comment embedded in the draft PR body (`<!-- bpilot:session {...json...} -->`). The `/port` and `/finalize` bot invocations are separate, stateless workflow runners, so the state must travel via the PR itself — and the bot controls the PR body, so it's always retrievable via the GitHub API.

Snapshot contents: the `port`-completion HEAD SHA, the list of gap findings (with their commit SHAs), the conflict resolutions, the original commit(s), and the target branch.

(Cross-machine local collaboration — one person ports, another finalizes elsewhere — is an edge case the file-based store doesn't cover. See Open Questions.)

### Edge cases

- **`finalize` without `port`:** errors gracefully — "No bpilot session found on this branch."
- **`finalize` with no human changes:** the diff is empty → reports "No new lessons learned; SKILL.md unchanged." (Idempotent.)
- **`finalize --no-llm`:** computes and prints the diff classification, but skips SKILL.md suggestions (they require LLM).
- **Rollback:** `port` creates a backup ref (`bpilot/backup/<timestamp>`) before starting. No dedicated rollback command — restore manually with `git reset --hard bpilot/backup/<timestamp>`.

## Security & Trust Boundaries

These invariants are non-negotiable and shape the implementation:

1. **The LLM is a pure text-in/text-out function.** `llm_client.py` does exactly one thing: send prompt text to OpenRouter over HTTPS and return response text. The LLM:
   - **Never sees git credentials, SSH keys, or host tokens.** The only credential anywhere near the LLM path is the OpenRouter API key, held solely inside `llm_client.py` and never included in prompts.
   - **Never executes anything.** No function-calling / tool-use, no shell access, no `git` invocation. Prompts carry code content and instructions only.
   - **Its output is treated as untrusted data.**

2. **Only `git_ops.py` touches the repository.** All git side effects — fetch, branch, cherry-pick, patch application, commit, push — live in the deterministic git-ops layer, the only component that runs in a context with the user's git credentials. The resolver and gap analyzer never call git directly; they return *patch text*, which `git_ops` applies.

3. **LLM-produced patches are validated before application.** Every patch (conflict resolution or gap fix) must pass, in order:
   1. `git apply --check` — applies cleanly;
   2. **Scope check** — touches only expected files (for conflict resolution: the conflicted file; for gap fixes: the files the finding declared);
   3. **Static validation** — no conflict markers, parses for its language.
   Failures are fed back to the LLM (bounded retries) or surfaced to the user.

4. **bpilot never merges.** No `git merge`, no push to the target branch, no PR merging. The GH bot only pushes the backport branch and opens a draft PR. Merge is always a human operation.

## Detailed Plan

### Phase 1: Project Skeleton & Snap Packaging

**Deliverable:** A snap that installs and runs a no-op `bpilot` command.

1. **Choose language: Python.** Reasons:
   - Rich ecosystem for git operations (`GitPython` or shelling out to `git`).
   - Simple HTTP client for OpenRouter API (`httpx` or `requests`).
   - Snapcraft has first-class Python support via the `python` plugin.
   - Markdown generation, file I/O, and subprocess management are straightforward.

2. **Project structure:**
   ```
   bpilot/
   ├── snapcraft.yaml
   ├── pyproject.toml
   ├── src/
   │   ├── bpilot/
   │   │   ├── __init__.py
   │   │   ├── cli.py              # entry point, argparse/click
   │   │   ├── git_ops.py          # git operations (cherry-pick, backup, rollback, merge-commit)
   │   │   ├── llm_client.py       # OpenRouter API client + cost tracking
   │   │   ├── resolver.py         # conflict resolution (unified diff + verification)
   │   │   ├── validator.py        # static checks only (no LLM)
   │   │   ├── gap_analyzer.py     # checklist-driven gap analysis
   │   │   ├── skill_loader.py     # parse bpilot/skills/<name>/SKILL.md into a SkillSet
   │   │   └── report.py           # markdown report generation
   │   └── main.py                 # __main__ shim
   ├── tests/
   │   ├── unit/
   │   └── fixtures/
   │       └── skills/             # 4 task skills + general-context, with frontmatter
   └── README.md
   ```

3. **Snapcraft.yaml** basics:
   - Use `python` plugin.
   - Confinement: `classic` (needs git access across filesystem).
   - Stage `git` as a stage-package (or rely on host git for `classic` confinement).
   - Expose `bpilot` command alias.

4. **CLI skeleton** (`cli.py`):
    ```python
    import argparse

    def main():
        parser = argparse.ArgumentParser(prog="bpilot", description="Intelligent backport helper")
        sub = parser.add_subparsers(dest="command")

        init = sub.add_parser("init", help="Scaffold bpilot/skills/ + .bpilot/, infer starter skills via LLM")
        init.add_argument("--no-llm", action="store_true", help="Scaffold placeholders only; skip LLM inference")
        init.add_argument("--skills-dir", default="bpilot/skills", help="Path to the skills directory")
        init.add_argument("--model", default=None, help="Override configured model")

        port = sub.add_parser("port")
        port.add_argument("commits", help="Commit hash or range (e.g. HEAD~3..HEAD or abc123)")
        port.add_argument("target", help="Target branch to backport onto")
        port.add_argument("--no-llm", action="store_true", help="Skip LLM inference; only cherry-pick")
        port.add_argument("--skills-dir", default="bpilot/skills", help="Path to the skills directory")
        port.add_argument("--no-init", action="store_true", help="Do not scaffold bpilot/skills/ when absent")
    port.add_argument("--model", default=None, help="Override configured model")
    port.add_argument("--dry-run", action="store_true", help="Don't create branches; just report")
    port.add_argument("--skip-baseline", action="store_true", help="Skip the baseline verification checks on the unmodified target branch")
    args = parser.parse_args()
    # ... dispatch
    ```

   `bpilot init` is the recommended first-run command: it scaffolds the
   five starter `SKILL.md` files, creates `.bpilot/` + adds it to
   `.gitignore`, and (when LLM is available) refines `verification-checks`
   (from README / CONTRIBUTING.md / pyproject.toml) and `version-control`
   (from README + git branch / commit history). The other three skills
   are left as placeholders, learned over time via `finalize`. `port`'s
   auto-init (placeholders only, no LLM) remains as a fallback. See
   Phase 8 for the skills-directory layout and the [Agent Skills
   specification](https://agentskills.io/specification) for the
   frontmatter contract.

### Phase 2: Git Operations Layer

**Deliverable:** `git_ops.py` that handles all git interactions. No LLM yet.

1. **`fetch_target_branch(branch_name)`** — `git fetch origin <branch>:<branch>` to get latest.
2. **`create_backup_ref()`** — creates `bpilot/backup/<timestamp>` at current HEAD before any changes. Enables full manual rollback with `git reset --hard bpilot/backup/<timestamp>` (no dedicated rollback command).
3. **`create_backport_branch(target_branch, commits_id)`** — creates `backport/<commit-hash>-to-<target>` off of target.
4. **`cherry_pick(commits)`** — attempts `git cherry-pick` for each commit in range.
5. **`detect_merge_commit(hash)`** — checks if a commit is a merge commit (has multiple parents). If so, automatically uses `git cherry-pick -m 1 <hash>` to pick the mainline parent.
6. **`detect_conflicts()`** — after cherry-pick, checks `git status --porcelain` for conflict markers (`UU`, `AA`, etc.).
7. **`get_conflict_diff()`** — for each conflicted file, returns the `diff` / merge conflict markers content for LLM consumption.
8. **`get_target_file_content(file_path, branch)`** — returns the target branch's version of a file (for conflict resolution context).
9. **`get_backport_diff(target_branch)`** — after resolution, produces a full diff of the backport branch vs. target, for gap analysis.
10. **`get_commit_messages(commits)`** — retrieves original commit messages for context.
11. **`apply_patch(patch_text, allowed_files)`** — the only path by which LLM-produced text touches the tree. Validates with `git apply --check`, rejects patches touching files outside `allowed_files` (scope check), then applies and commits. Used by both the conflict resolver and the gap analyzer.
12. **`stash_push()` / `stash_pop()`** — before the first cherry-pick, stash any uncommitted changes (the baseline / verification commands may regenerate build artifacts like `poetry.lock`, which would otherwise make `git cherry-pick` refuse to run). Popped after a successful run; on a pop conflict the backport's own regenerated version wins and the stash is dropped.

**Dirty-tree auto-stash:** verification commands (e.g. `tox run -e format`) can leave regenerable artifacts dirty in the tree, which blocks cherry-pick. Rather than aborting (or forcing a manual stash + `--continue` round-trip), `port` auto-stashes before picking and auto-pops afterward. These files are regenerated by the post-pick validation step anyway, so the stashed copies are normally redundant; popping only matters for genuinely hand-edited changes, and on conflict the backport's version is authoritative.

**Key design decision:** Shell out to `git` via `subprocess` rather than using `GitPython`. GitPython adds a heavy dependency and its API changes between versions. Shelling out is predictable, well-documented, and easy to debug.

### Phase 3: Configuration & Snap Setup

**Deliverable:** Users can configure API key and model preferences via snap.

1. **Snap config commands** (using `snap set` / `snap get`):
   ```bash
   sudo snap set bpilot openrouter-api-key="sk-or-..."
   sudo snap set bpilot model="openrouter/z-ai/glm-5.2"
   sudo snap set bpilot max-tokens=16000
   ```
   These are stored in `$SNAP_DATA/bpilot.conf` and read at runtime.

2. **Environment variable fallbacks** (for CI/GitHub Actions):
   - `OPENROUTER_API_KEY`
   - `BPILOT_MODEL`
   - `BPILOT_MAX_TOKENS`

3. **Defaults:**
   - Model: `openrouter/z-ai/glm-5.2` (good balance of quality and cost for code reasoning).
   - Max tokens: `16000`.
   - Temperature: `0.2` (deterministic-ish for code tasks).

### Phase 4: LLM Client

**Deliverable:** `llm_client.py` — thin OpenRouter client with cost tracking.

1. **Single function interface:**
   ```python
   def query_llm(prompt: str, system: str = "", model: str = None) -> tuple[str, dict]:
       """Send a prompt to OpenRouter, return (text_response, usage_stats).
       
       usage_stats: {"prompt_tokens": int, "completion_tokens": int, "model": str}
       """
   ```
2. Uses `httpx` with timeout handling and retry (3 attempts with backoff).
3. Reads API key from snap config or env var.
4. Supports `--model` CLI override.
5. **Cost tracking:** Accumulate token usage across all LLM calls in a run. Report total prompt tokens, completion tokens, and estimated cost in the final report. Warn if a single call exceeds a configurable threshold (default: 8,000 tokens — likely a runaway prompt).

### Phase 5: Conflict Resolver (LLM-assisted)

**Deliverable:** `resolver.py` — invoked only when cherry-pick conflicts exist.

1. **Input:** For each conflicted file:
   - The file path.
   - The conflict markers / diff.
   - The original commit message (for intent).
   - The target branch's version of the file (pre-cherry-pick), for context.
   - Relevant sections from SKILL.md (branch conventions, known divergences).

2. **LLM prompt structure:**
   ```
   You are resolving a git merge conflict during a backport.

   Target branch: <branch>
   Original commit: <hash> — "<message>"

   The following file has a conflict:
   File: <path>

   Conflict content:
   <conflict markers>

   Target branch's version of this file (before cherry-pick):
   <target file content>

   Relevant SKILL.md sections:
   <branch conventions, known divergences>

   Produce a unified diff against the target branch's version of this
   file that resolves the conflict. Preserve the intent of the original
   commit while adapting to the target branch's structure. Output ONLY
   the unified diff, no explanations.
   ```

3. **Apply and verify:**
   a. Hand the returned unified diff to `git_ops.apply_patch()`, scoped to the conflicted file (apply-check + scope check). The LLM never runs git itself.
   b. Verify: check the result has no conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).
   c. Verify: check the result parses (language-aware: `python -m py_compile`, `ruff check`, etc.).
   d. If verification fails, feed the error back to the LLM and retry (max 3 attempts).
   e. Continue the cherry-pick (`git cherry-pick --continue`).

4. **If all retries fail for one or more files (partial failure):** The run *pauses* rather than aborting. Files that resolved successfully are committed (preserving the original commit message, via `git_ops.commit_partial_cherry_pick()`); files that failed are left in the working tree with their conflict markers. The session records the paused commit, the failed files, and any not-yet-picked commits. The report directs the user to resolve the failed files, `git add` them, and resume with `bpilot port --continue`, which amends the manual fixes into the paused commit and resumes the queue.

### Phase 6: Validator (static checks only)

**Deliverable:** `validator.py` — runs after conflicts are resolved. No LLM.

1. **Purpose:** Fast, deterministic sanity-checks after conflict resolution. No LLM inference — the gap analyzer handles semantic validation.
2. **Checks:**
   - Does the diff still contain conflict markers? (regex check for `<<<<<<<`, `=======`, `>>>>>>>`)
   - Does the resolved file parse? (language-aware: `python -m py_compile`, `ruff check`, etc. — configurable via SKILL.md)
   - Are there obvious placeholders like `TODO`, `FIXME`, `???` left by the LLM?
   - Does the diff introduce any unintended file changes (e.g., files not in the original commit)?
3. **If validation fails:** Abort, report, and leave the branch in its current state for manual intervention.

### Phase 6b: Verification Checks + LLM Fixer (format / lint / unit tests)

**Deliverable:** `verification_fixer.py` — runs after the validator and (if
present) after gap fixes are applied. Runs the project's format, lint, and
unit-test commands (read from the SKILL.md **"Verification Checks"**
section, falling back to **"Test Commands"**), and if any fail, invokes the
LLM to repair the offending files in a bounded loop.

1. **Verification check commands:** sourced from the SKILL.md "Verification
   Checks" section (one command per line, e.g. `ruff format src/ tests/`,
   `ruff check src/ tests/`, `pytest tests/unit/ -q`). If the section is
   absent, the tool falls back to the legacy "Test Commands" section, then
   to auto-detected defaults (`ruff check`, `ruff format --check`).

2. **Loop (max 5 iterations):**
   a. Run all verification check commands; capture stdout/stderr + exit code.
   b. If all pass → done.
   c. If any fail → collect the failing command output + the content of
      the changed files referenced by the failures.
   d. Send the failures + file contents to the LLM with a prompt asking
      for the complete corrected file content for each affected file.
      The LLM is text-in/text-out only; it never runs the checks itself.
   e. Write the returned file contents to the working tree, re-run the
      verification checks. Repeat.
   f. After 5 iterations with remaining failures, stop, surface the
      remaining failures in the report, and leave the branch for manual
      intervention (the cherry-picked changes stay; the user fixes the
      remaining lint/test failures manually).

3. **Trust boundary:** Same as the resolver — the LLM returns file content;
   the fixer writes it via normal file I/O and the validator re-checks
   before staging. The LLM never runs shell commands, never sees git
   credentials, and never executes the test suite itself.

4. **Respects `--no-llm`:** verification checks still run (they're
   deterministic), but the LLM fix loop is skipped on failure — failures
   are just reported.

   > Naming note: this step is called "verification checks" rather than
   > "static checks" because the bundle includes **unit tests**, which
   > execute the code and are therefore *dynamic*. The per-file validator
   > in Phase 6 (conflict markers, syntax, placeholders) is genuinely
   > static and keeps that name.

5. **Baseline gate (default-on):** immediately after creating the backport
   branch (off the freshly-fetched target) and **before any cherry-pick**,
   `bpilot port` runs the verification checks on the untouched branch. The
   baseline uses the same command resolution as the post-backport run
   (skill "Verification Checks" → "Test Commands" fallback → auto-detected
   ruff), so a missing `tox` and a broken target branch both surface here
   — before a single cherry-pick or LLM token is spent.

   - **On failure:** abort, clean up (restore the original branch, force-delete
     the backport branch), and exit 1 with a triage message naming the three
     possible causes: (a) missing dependencies in the current environment,
     (b) wrong verification commands in the `verification-checks` skill (with
     a note that `bpilot init`-inferred commands need human review), (c) the
     target branch itself is broken. Each failing command is shown with its
     exit code and the last 40 lines of combined output. No session file and
     no `BACKPORT_REPORT.md` are written — nothing was backported. The backup
     ref is left in place for manual recovery (HEAD never diverged from the
     original branch).
   - **Vacuously green when no commands exist:** with no skill and no
     auto-detected commands, the baseline passes silently — current behaviour
     for such repos is unchanged.
   - **Runs regardless of LLM mode** (it's a deterministic gate); skipped by
     `--dry-run` (which exits before branch creation) and by `--skip-baseline`.
   - **Baseline-aware fixer:** when the baseline passed, the post-backport
     fixer-loop LLM prompt asserts "these checks all passed on the unmodified
     target branch immediately before the backport; the failures below were
     introduced by the cherry-picked changes" — sharpening the fix mandate.
     With `--skip-baseline` (`baseline_passed=False`), the assertion is
     omitted and the fixer behaves as before. There is deliberately **no
     per-command pre-existing-vs-new classification layer**: with a hard
     baseline gate, any baseline failure aborts, so whenever the fixer
     executes the baseline was green and every post-backport failure is new
     by definition.

   `--skip-baseline` is the sanctioned escape hatch for iterative runs (a
   known-sane environment or a known-flaky target branch). The sanctioned
   response to a persistently flaky target branch is `--skip-baseline`, **not**
   editing the skill to delete the flaky command. Without a baseline,
   post-backport check failures cannot be attributed to the backport.

   > Working-tree side effects: the baseline checks run in the user's tree and
   > may create `.tox/`, `.pytest_cache/`, coverage files, etc. — typically
   > gitignored, the same as a human running the commands. Documented, not
   > prevented.

6. **Sanitized subprocess env (snap):** verification commands and lock
   regeneration run with snap-injected environment variables stripped when
   bpilot is running as a snap. bpilot ships as a classic-confined snap
   bundling its own python3.12 and libraries; the snap wrapper injects
   `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV` (plus
   `SNAP*`). Host binaries spawned with that environment can load the snap's
   libraries and break in opaque ways. `validator.build_tool_env()` returns a
   copy of `os.environ` with those four variables removed when `SNAP` is
   present, and `None` (inherit unchanged) otherwise — so a developer's
   deliberately exported `PYTHONPATH`/`VIRTUAL_ENV` outside the snap is
   legitimate and untouched. Applied in exactly two places:
   `run_verification_checks` and `_regen_locks`. Inline `VAR=value` prefixes
   in a skill command (e.g. `PYTHONPATH=src:lib poetry run pytest`) still
   take effect — they set the variable via the shell for that command only.
   `git_ops._run` is deliberately **not** changed (the snap bundles its own
   git).

7. **Trust note (security):** the verification check commands are arbitrary
   shell sourced from a repo-committed file (`bpilot/skills/verification-checks/
   SKILL.md`), executed with the invoking user's privileges — the same trust
   level as a `Makefile` or `tox.ini`. This is by design: the commands are how
   the project verifies itself, and bpilot runs them verbatim. `bpilot init`-
   inferred commands are guesses that require human review before they're
   committed; the baseline triage message always reminds the user of this.

### Phase 7: Gap Analyzer (the key differentiator)

**Deliverable:** `gap_analyzer.py` — the most valuable and novel component.

The gap analyzer is **checklist-driven**, not a generic "find problems" prompt. Each item in the SKILL.md's **"Things to Check When Backporting"** section becomes a targeted LLM query with specific context. This is what makes it precise rather than noisy.

1. **How it works:**
   a. Parse SKILL.md to extract the "Things to Check When Backporting" checklist.
   b. For each checklist item, construct a targeted prompt that includes:
      - The specific concern from the checklist item
      - The full diff of the backport branch vs. target branch
      - The target branch's current content of the relevant file(s) mentioned in the checklist item
      - The original commit message (for intent)
      - Only the SKILL.md sections relevant to that checklist item (not the full file)
   c. Each query asks: *"Does this concern apply to the current backport? If yes, propose a patch."*
   d. Collect all findings into a structured report.

2. **Targeted prompt structure** (per checklist item):
   ```
   You are analyzing a backport for a specific concern.

   Checklist item: <e.g., "If the original PR added behaviour to
   workload_initialise, check whether _on_upgrade_granted in upgrade.py
   needs the same call">

   The following changes were backported from <source> to <target>:
   <diff>

   Original intent (from commit message / PR):
   <intent>

   Target branch's current content of the relevant file(s):
   <file content>

   SKILL.md context (relevant sections only):
   <relevant sections>

   Does this checklist item apply to this backport? Answer with:
   - APPLIES: <yes/no>
   - If yes, what is missing and where (file:line)
   - If yes, a unified diff patch that fixes it
   - Severity: [critical | important | minor]
   - If no, one-line reason why it doesn't apply
   ```

3. **Output and application:** For each finding that applies, the returned patch goes through `git_ops.apply_patch()` (apply-check + scope check against the finding's declared files + static validation), and is then **committed as its own labelled commit**:

   ```
   bpilot(gap): add connect_mysql_exporter to _on_upgrade_granted
   ```

   One commit per finding keeps gap fixes attributable and individually revertible (`git rebase -i`, `git reset`), while keeping the working tree complete — the user reviews the final branch state with normal git tooling (`git log`, `git show`, the draft PR diff), not a side-channel of patch files. The report maps each finding to its commit SHA.

4. **Verification:** After gap fixes are committed, run the test commands from SKILL.md and include results in the report. If a gap fix breaks tests, flag its commit so the user can drop it.

5. **Gap analyzer respects `--no-llm`:** If set, skip entirely and note in report that gap analysis was skipped.

### Phase 8: Skills Directory Format & Loading

**Deliverable:** A documented format for repo-specific knowledge, plus
`skill_loader.py` that parses a directory of named skills. See
`SKILLS_DIRECTORY_SPEC.md` for the full spec; this section is the summary.

Repo-specific knowledge lives in a **directory of named skills**, each in
its own subdirectory under `bpilot/skills/` containing a `SKILL.md`:

```
bpilot/
└── skills/
    ├── version-control/SKILL.md
    ├── verification-checks/SKILL.md
    ├── conflict-resolution/SKILL.md
    ├── gap-analysis/SKILL.md
    └── general-context/SKILL.md   # shared, cross-cutting sections
```

The set of skills is fixed (4 task skills + `general-context`). Each
`SKILL.md` conforms to the [Agent Skills
specification](https://agentskills.io/specification): YAML frontmatter
(required `name` matching the parent directory, and `description`) followed
by a markdown body of `## Section` headers.

**Section → skill mapping** (current single-file sections redistribute as):

| Section                                | New skill            |
|----------------------------------------|----------------------|
| Branch Conventions / Commit Conventions| version-control      |
| Verification Checks / Test Commands     | verification-checks  |
| Skip Files / Merge Conflict Resolution Rules | conflict-resolution |
| Things to Check When Backporting       | gap-analysis         |
| Lifecycle Hooks / Upgrade Path / Files of Interest / Known Divergences | general-context |

The shared `general-context` skill is auto-appended to every task's
context (callers do not pass it explicitly) because several of its
sections are read by more than one task.

**Loading (`skill_loader.py`):**
- `load_skill_set(skills_dir)` loads every `bpilot/skills/<name>/SKILL.md`
  into a `SkillSet`; returns `None` when the directory is absent (graceful
  degradation). A present-but-malformed skill file (missing `name` /
  `description`, name/directory mismatch, malformed name) raises
  `SkillLoadError` — a hard error, not silent degradation.
- `SkillSet.get(name)` returns a named `SkillFile`; `SkillSet.context_for(*names)`
  concatenates the named skills' bodies AND `general-context`, skipping
  empty/absent skills and stripping HTML-comment placeholders so
  scaffolding stubs never reach the LLM. Returns "" when all are empty.
- `SkillFile.is_empty` is True for freshly-scaffolded files (body has no
  non-whitespace, non-HTML-comment content). Consumers MUST omit the
  skill-context block from LLM prompts when all relevant skills are empty,
  so a first run against freshly-scaffolded skills produces LLM prompts
  identical to the no-skills-directory graceful-degradation path.
- `checklist_items` (gap-analysis), `verification_checks`
  (verification-checks, falling back to legacy `Test Commands`), and
  `skip_files` (conflict-resolution) all return `[]` on an empty skill.

**First-run auto-init:** when `bpilot port` is run from a repo root with no
`bpilot/skills/` directory, bpilot scaffolds the directory and writes the
five starter `SKILL.md` files (valid frontmatter + empty section bodies
with `<!-- ... -->` placeholders) before proceeding. `--no-init`
suppresses scaffolding (for CI/bot runs that shouldn't write files).
`bpilot port --dry-run` still triggers scaffolding. `finalize` does NOT
auto-init (it requires an existing session, which implies `port` already
ran); `reset` does NOT touch the skills directory. There is no separate
`bpilot init` subcommand — the command surface stays `port`, `finalize`,
`reset`.

**Starter templates** (written by `init_skills_dir`):

`bpilot/skills/version-control/SKILL.md`:
```markdown
---
name: version-control
description: Repo-specific branch and commit-message conventions for backports. Used by bpilot to name backport branches and to validate commit message style when porting.
---
## Branch Conventions
<!-- e.g. `main` / `edge`: active development. `8.4/edge`: release branch. -->

## Commit Conventions
<!-- e.g. conventional-commits, ticket prefixes, sign-off requirements. -->
```

`bpilot/skills/verification-checks/SKILL.md`:
```markdown
---
name: verification-checks
description: Format, lint, and unit-test commands to run after a backport. Used by bpilot to verify a ported change and to drive the LLM repair loop on failure.
---
## Verification Checks
<!-- One command per line, backtick-wrapped. -->
<!-- e.g. Format: `ruff format src/ tests/` -->
<!-- e.g. Lint:   `ruff check src/ tests/` -->
<!-- e.g. Tests:  `PYTHONPATH=src poetry run pytest tests/unit/ -q` -->

## Test Commands
<!-- Legacy alias for Verification Checks; used only if the section above is empty. -->
```

`bpilot/skills/conflict-resolution/SKILL.md`:
```markdown
---
name: conflict-resolution
description: Known branch divergences and project-specific rules for resolving cherry-pick conflicts. Used by bpilot's resolver when a port produces a conflict.
---
## Skip Files
<!-- Glob patterns to skip during conflict resolution (target version taken instead). -->
<!-- e.g. poetry.lock, *.lock, package-lock.json, Cargo.lock, go.sum -->

## Merge Conflict Resolution Rules
<!-- Project-specific guidance the LLM should follow when resolving conflicts. -->
```

`bpilot/skills/gap-analysis/SKILL.md`:
```markdown
---
name: gap-analysis
description: Checklist of things to verify when backporting (lifecycle hooks, upgrade paths, parity between flavours). Used by bpilot's gap analyzer to drive per-item checks.
---
## Things to Check When Backporting
<!-- Numbered list; each item becomes one targeted LLM query. -->
<!-- 1. If the original PR added behaviour to X, check whether Y also needs it. -->
```

`bpilot/skills/general-context/SKILL.md`:
```markdown
---
name: general-context
description: Shared repo context read by multiple bpilot tasks — lifecycle hooks, upgrade paths, files of interest, known branch divergences. Loaded alongside each task-specific skill.
---
## Lifecycle Hooks
<!-- e.g. install → start → config-changed; start calls workload_initialise. -->

## Upgrade Path
<!-- e.g. machine charm upgrades defer start; new "enable X by default" changes must also be added to _on_upgrade_granted. -->

## Known Divergences Between Branches
<!-- e.g. 8.4 workload_initialise has an is_data_dir_initialised() shortcut; 8.0 does not. -->

## Files of Interest
<!-- e.g. machines/src/charm.py — main charm logic. -->
```

**Why this works:** The skills are essentially the kind of context a
senior engineer would explain to a junior engineer doing their first
backport. They capture the *tacit knowledge* that git can't see. The gap
analyzer feeds this to the LLM as grounding context, and each task loads
only the skill(s) it needs by name — no parsing a monolithic file and
selecting sections.

**Maintenance:** The skill files are strictly manually maintained by
human experts. The tool never modifies them automatically (`finalize`
only *proposes* updates). Keeping them accurate and current is the
repository maintainer's responsibility. `finalize` never proposes edits
to `version-control` or `verification-checks` — those are
conventions/config, not learned from per-backport deltas.

### Phase 9: Report Generator

**Deliverable:** `report.py` — produces a `BACKPORT_REPORT.md` in the repo root.

**Report structure:**
```markdown
# Backport Report

**Date:** 2026-07-20
**Source commits:** abc123, def456
**Target branch:** 8.0/edge
**Backport branch:** backport/abc123-to-8.0/edge
**Backup branch:** bpilot/backup/2026-07-20-143022
**LLM cost:** 12,450 tokens (prompt) + 3,200 tokens (completion) ≈ $0.08

## Cherry-pick Result
- ✅ abc123 — applied cleanly
- ⚠️ def456 — 2 conflicts resolved via LLM
  - `machines/src/charm.py` (conflict in workload_initialise)
  - `machines/tests/unit/test_charm.py` (test signature mismatch)

## Validation
- ✅ no conflict markers in changed files
- ✅ all changed Python files parse

## Verification Checks (format / lint / unit tests)
- ✅ `ruff format --check` passed
- ✅ `ruff check` passed
- ⚠️ `pytest tests/unit/test_upgrade.py` — 1 failed
  → LLM fixer attempted 2 iterations; remaining failure surfaced for manual review
  (the Gap Analysis below subsequently identifies the missing upgrade-path
  call as the likely cause; applying that gap fix would resolve this test)

## Gap Analysis
### Gap 1: Missing `connect_mysql_exporter` in upgrade path
- **Severity:** critical
- **Checklist item:** "If the original PR added behaviour to `workload_initialise`,
  check whether `_on_upgrade_granted` in `upgrade.py` needs the same call"
- **File:** `machines/src/upgrade.py`, in `_on_upgrade_granted`
- **What's missing:** The backported change enables the exporter in
  `workload_initialise`, but `workload_initialise` is only called from
  `_on_start`, which is deferred during charm upgrades by the
  `upgrade.idle` guard in `_can_start`. Existing deployments upgrading
  to this charm version won't get the exporter started.
- **Fix:** applied as commit `9f8e7d6` — `bpilot(gap): add connect_mysql_exporter to upgrade path`

## Next Steps
1. Review the labelled `bpilot(gap):` commits (`git log`, `git show`); drop any you disagree with.
2. Re-run tests after any changes.
3. Run `bpilot finalize` to propose SKILL.md updates from any manual changes.
4. Push branch and open PR.

## Rollback
If anything went wrong, restore the pre-backport state:
```
git reset --hard bpilot/backup/2026-07-20-143022
```

### Phase 10: `--no-llm` Mode

**Deliverable:** A fully functional mechanical backport mode.

- Performs cherry-pick only.
- On conflict: reports conflicts and exits (no resolution).
- No gap analysis.
- Report notes: "LLM features skipped (--no-llm). Conflicts require manual resolution."

This mode is useful for:
- Air-gapped environments.
- CI runs where API cost is a concern.
- Simple backports with no expected conflicts.

### Phase 11: Finalize & Feedback Loop

**Deliverable:** `finalize.py` — the `bpilot finalize` command that closes the learning loop.

1. **Retrieve the session snapshot:**
   - Local: read `.bpilot/session.json` from the working tree.
   - GH bot: parse the `<!-- bpilot:session ... -->` comment from the PR body.
   - Error gracefully if none found.

2. **Diff current state vs. snapshot:**
   - `git diff <snapshot-head-sha>..HEAD` to get everything the human added/changed after `port`.
   - Also check which recorded gap findings are still present in the current tree — the user may have dropped some `bpilot(gap):` commits.

3. **Classify the delta:**
   - **Applied suggestions** — gap findings whose commits/effects remain in the current tree.
   - **Rejected suggestions** — gap findings the user dropped (bpilot may have been wrong, or the gap wasn't real).
   - **Human-added changes** — commits/hunks in the diff that don't correspond to any recorded gap fix.

4. **Propose per-skill updates (LLM, only if human-added changes exist):**

   With the multi-skill layout (Phase 8), finalize proposes per-skill diffs
   against the **correct** skill file(s) based on what the human-added
   changes look like:
   - New "Things to Check" entries → propose an update to
     `bpilot/skills/gap-analysis/SKILL.md`.
   - New "Known Divergences", lifecycle hooks, upgrade paths, or files of
     interest → propose an update to `bpilot/skills/general-context/SKILL.md`.
   - New conflict-resolution rules or skip-file patterns → propose an
     update to `bpilot/skills/conflict-resolution/SKILL.md`.
   - **Never** propose updates to `version-control` or
     `verification-checks` — those are conventions/config, not learned
     from per-backport deltas. The prompt names this as a hard constraint.

   The finalize LLM prompt receives the existing content of the candidate
   target skill file(s) (`gap-analysis`, `general-context`,
   `conflict-resolution` only), is told the structure (which sections live
   in which skill) so it targets the right file, and emits one unified
   diff per skill file it wants to update.

   ```
   A backport was performed from <source> to <target>. The tool
   suggested gap fixes; the human's final merged result differed.

   Human-added changes (changes the human made that the tool did NOT
   suggest):
   <diff of human-added changes>

   Applied suggestions: <list>
   Rejected suggestions: <list>

   Existing skills (gap-analysis / general-context / conflict-resolution):
   <contents of those three SKILL.md files>

   Propose minimal updates to the relevant skill file(s) that would
   have let the tool catch these human-added changes itself next time.
   Do not duplicate existing entries. Output one unified diff per skill
   file you want to update. Do NOT propose updates to version-control
   or verification-checks — those are read-only for finalize.
   ```

5. **Output:**
   - Print the classification summary (applied / rejected / human-added).
   - Print the proposed per-skill diff(s) for review.
   - With `--commit`: apply each proposed diff as **one commit per updated
     skill** (e.g. `chore(bpilot): update gap-analysis skill from backport
     learnings`), mirroring the per-gap-fix commit philosophy in Phase 7.
     Without `--commit`: leave the diffs for the user to apply manually.
   - With `--no-llm`: print the classification and diff, skip the
     skill-update suggestion step.

6. **Why this matters:** each human correction that bpilot missed becomes a candidate checklist item for the next backport — but only after a human reviews and accepts the skill update. The skill files converge toward a comprehensive, team-vetted map of branch divergences. One commit per updated skill keeps each update independently reviewable and revertible.

## CLI UX Summary

```bash
# Configure (one-time)
sudo snap set bpilot openrouter-api-key="sk-or-..."
sudo snap set bpilot model="openrouter/z-ai/glm-5.2"

# --- init (first-run per project) ---
bpilot init                              # scaffold bpilot/skills/ + .bpilot/, infer verification-checks & version-control via LLM
bpilot init --no-llm                     # scaffold placeholders only (no LLM inference)

# --- port ---
bpilot port abc123 8.0/edge                    # single commit (auto-scaffolds bpilot/skills/ on first run)
bpilot port HEAD~3..HEAD 8.0/edge              # commit range
bpilot port abc123 8.0/edge --no-llm           # mechanical only
bpilot port abc123 8.0/edge --no-init          # do not scaffold bpilot/skills/ when absent
bpilot port abc123 8.0/edge --skills-dir ./docs/skills  # override skills directory
bpilot port abc123 8.0/edge --dry-run          # show diff, no branch (still scaffolds)
bpilot port abc123 8.0/edge --model openrouter/anthropic/claude-3.5-sonnet

# review bpilot's work (incl. labelled bpilot(gap): commits), edit, run tests

# --- finalize ---
bpilot finalize                                # classify delta + propose per-skill updates
bpilot finalize --commit                       # also commit each proposed skill update (one commit per skill)
bpilot finalize --no-llm                       # classification only, no skill suggestion
```

## GitHub Bot Integration (comment-driven)

bpilot runs as a comment-driven bot rather than a fire-and-forget workflow. Two triggers: `/port` on a merged source PR, and `/finalize` on the resulting draft backport PR.

```yaml
# .github/workflows/bpilot.yml
name: bpilot
on:
  issue_comment:
    types: [created]

jobs:
  port:
    # Triggered by "/port <target-branch>" on a merged PR
    if: >
      github.event.issue.pull_request &&
      github.event.issue.state == 'closed' &&
      startsWith(github.event.comment.body, '/port ')
    runs-on: ubuntu-latest
    concurrency:
      group: bpilot-port-${{ github.event.issue.number }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          token: ${{ secrets.GITHUB_TOKEN }}
      - name: Install bpilot
        run: sudo snap install bpilot --classic
      - name: Configure
        run: |
          sudo snap set bpilot openrouter-api-key="${{ secrets.OPENROUTER_API_KEY }}"
          sudo snap set bpilot model="openrouter/z-ai/glm-5.2"
      - name: Parse target branch
        id: args
        run: echo "target=$(echo '${{ github.event.comment.body }}' | awk '{print $2}')" >> $GITHUB_OUTPUT
      - name: Run bpilot port
        run: |
          bpilot port ${{ github.event.issue.pull_request.merge_commit_sha }} \
            ${{ steps.args.outputs.target }} --yes
      - name: Push backport branch
        run: git push origin HEAD --force-with-lease
      - name: Open draft PR
        uses: actions/github-script@v7
        with:
          script: |
            // open draft PR to target, tag the commenter,
            // embed the bpilot session snapshot as a hidden comment
            // in the PR body for later /finalize retrieval
      - name: Upload report
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: bpilot-report-${{ github.event.issue.number }}
          path: BACKPORT_REPORT.md
      - name: Comment failure
        if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            // comment on the source PR that the backport failed,
            // link to the uploaded report

  finalize:
    # Triggered by "/finalize" on the draft backport PR
    if: >
      github.event.issue.pull_request &&
      startsWith(github.event.comment.body, '/finalize')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.issue.pull_request.head.ref }}
      - name: Install bpilot
        run: sudo snap install bpilot --classic
      - name: Configure
        run: |
          sudo snap set bpilot openrouter-api-key="${{ secrets.OPENROUTER_API_KEY }}"
      - name: Run bpilot finalize
        run: bpilot finalize --commit --yes
      - name: Push SKILL.md suggestion commit
        run: git push origin HEAD
      - name: Comment summary
        uses: actions/github-script@v7
        with:
          script: |
            // comment on the draft PR summarizing applied / rejected /
            // human-added changes, and what SKILL.md update was proposed
```

**Key design points:**
- **Comment-driven, not event-driven.** Backports happen only when a human explicitly asks (`/port`), not automatically on every merge. Prevents noise and uncontrolled LLM spend.
- **Draft PR as the review gate.** `/port` never touches the target branch directly; it always opens a draft PR that a human must review and merge.
- **Session state travels with the PR.** The snapshot is embedded in the draft PR body, so `/finalize` works even though the two bot invocations are separate, stateless workflow runs.
- **Concurrency guard** on `/port` per source PR prevents duplicate backports.
- **Failures surface on the source PR** so the commenter knows the backport didn't complete.

## Implementation Phases & Milestones

| Phase | Deliverable | Effort | Dependencies |
|-------|-------------|--------|--------------|
| 1 | Project skeleton + snap packaging | S | None |
| 2 | Git operations layer (cherry-pick, backup ref, merge-commit) | S | Phase 1 |
| 3 | Config & snap setup commands | S | Phase 1 |
| 4 | LLM client (OpenRouter) + cost tracking | S | Phase 3 |
| 5 | Conflict resolver (unified diff + verify) | M | Phases 2, 4 |
| 6 | Static validator | S | Phase 5 |
| 7 | Gap analyzer + SKILL.md format/loading | M | Phases 2, 4 |
| 8 | Report generator | S | Phases 5-7 |
| 9 | `--no-llm` mode | S | Phase 2 |
| 10 | Session snapshot (`.bpilot/session.json`) | S | Phase 2 |
| 11 | Finalize & feedback loop | M | Phases 4, 10 |
| 12 | GitHub bot (`/port`, `/finalize`) + docs | M | Phases 8, 11 |

**MVP (Phases 1-4, 9):** `bpilot port` with mechanical cherry-pick + `--no-llm`. Validates the snap works end-to-end.

**v1 (Phases 5-8, 10):** Full `bpilot port` — LLM-assisted conflict resolution, validation, gap analysis, session snapshot, and reporting.

**v1.1 (Phases 11-12):** `bpilot finalize` feedback loop and the comment-driven GH bot.

## Open Questions

1. **Gap fix application** — resolved: gap fixes are applied automatically as individual labelled commits (`bpilot(gap): ...`), after validation (apply-check + scope check). The user reviews them like any other commit and drops any they disagree with. No patch files, no extra subcommand — attribution and revertibility come from git itself.

2. **How to handle multi-file conflicts efficiently?** Option A: resolve each file independently (parallelizable, but may miss cross-file intent). Option B: send all conflicts in one LLM call (better context, but may hit token limits). Start with A, add B as a `--holistic` option.

3. **Pushing / PR creation** — resolved by context: local CLI never pushes (user reviews, then pushes and raises the PR manually); the GH bot always opens a **draft** PR (never pushes to the target branch directly).

4. **SKILL.md maintenance** — the skill file is strictly human-reviewed. `finalize` *proposes* updates derived from human corrections, but a human always reviews and merges them. The tool never silently edits the skill file.

5. **Token budget cap?** Should the tool abort if token usage exceeds a configurable threshold (e.g., 50,000 tokens)? This would prevent runaway retry loops from incurring unexpected costs. Lean: yes, default cap with `--max-tokens` override.

6. **Cross-machine local finalize** — the file-based snapshot (`.bpilot/session.json`) covers the common same-machine flow but not "person A ports, person B finalizes on another machine." Options: (a) commit a git note under `refs/notes/bpilot` and document the explicit fetch refspec; (b) embed the snapshot as a comment in `BACKPORT_REPORT.md` and commit that to the branch. Lean: (a), since the report file shouldn't be merged into the target branch anyway. Defer to post-v1 unless the need shows up.

7. **Finalize against local branch vs. merged PR?** Local `finalize` diffs against the local branch HEAD. But the team's source of truth is the *merged* PR. Should `finalize` support `--merged <pr-number>` to diff against the post-review merged state instead? Lean: v1 diffs against local HEAD; add `--merged` later if teams want post-merge learning.
