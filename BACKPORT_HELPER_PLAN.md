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
│   ├─ yes ─▶ LLM Resolver (unified diff + verify, bounded retries)
│   │         └─▶ Static Validator (no LLM)
│   └─ no ──────────┐
│                   ▼
├─▶ Gap Analyzer (checklist-driven, per SKILL.md "Things to Check")
│    — runs ALWAYS: clean cherry-picks AND after conflict resolution
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
   │   │   ├── skill_loader.py     # parse bpilot/SKILL.md into sections
   │   │   └── report.py           # markdown report generation
   │   └── main.py                 # __main__ shim
   ├── tests/
   │   ├── unit/
   │   └── fixtures/
   │       └── sample_skill.md
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
       parser.add_argument("commits", help="Commit hash or range (e.g. HEAD~3..HEAD or abc123)")
       parser.add_argument("target", help="Target branch to backport onto")
       parser.add_argument("--no-llm", action="store_true", help="Skip LLM inference; only cherry-pick")
       parser.add_argument("--skill-file", default="bpilot/SKILL.md", help="Path to repo-specific skill file")
       parser.add_argument("--model", default=None, help="Override configured model")
       parser.add_argument("--dry-run", action="store_true", help="Don't create branches; just report")
       args = parser.parse_args()
       # ... dispatch
   ```

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

4. **If all retries fail:** Abort cherry-pick, leave the conflicted file in the working tree, and report the failure with the conflict markers for manual resolution.

### Phase 6: Validator (static checks only)

**Deliverable:** `validator.py` — runs after conflicts are resolved. No LLM.

1. **Purpose:** Fast, deterministic sanity-checks after conflict resolution. No LLM inference — the gap analyzer handles semantic validation.
2. **Checks:**
   - Does the diff still contain conflict markers? (regex check for `<<<<<<<`, `=======`, `>>>>>>>`)
   - Does the resolved file parse? (language-aware: `python -m py_compile`, `ruff check`, etc. — configurable via SKILL.md)
   - Are there obvious placeholders like `TODO`, `FIXME`, `???` left by the LLM?
   - Does the diff introduce any unintended file changes (e.g., files not in the original commit)?
3. **If validation fails:** Abort, report, and leave the branch in its current state for manual intervention.

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

### Phase 8: SKILL.md Format & Loading

**Deliverable:** A documented format for repo-specific knowledge, plus `skill_loader.py` that parses it into structured sections.

A `SKILL.md` file lives at `bpilot/SKILL.md` in the repository (or is specified via `--skill-file`). It is plain markdown with structured sections that the gap analyzer and conflict resolver read and include in LLM prompts.

**Loading (`skill_loader.py`):**
- Parse the SKILL.md into named sections (e.g., "Branch Conventions", "Lifecycle Hooks", "Known Divergences", "Things to Check", "Test Commands").
- Provide a `get_section(name)` API so each LLM call receives only the relevant sections, not the full file. This saves tokens and improves focus.
- Extract the "Things to Check When Backporting" checklist as a list of items for the gap analyzer.
- Extract "Test Commands" as a list of shell commands for the validator and gap fix verification.

**Template:**
```markdown
# Backport Skill File: <repo-name>

## Branch Conventions
- `main` / `edge`: active development.
- `8.4/edge`: release branch for 8.4.
- `8.0/edge`: stable release branch for 8.0.
- Backport branches: `backport/<feature>-to-<target>`

## Lifecycle Hooks
- **Machine charm:** `install` → `start` → `config-changed`. The `start`
  hook calls `workload_initialise`. During charm upgrade (`juju refresh`),
  `start` fires but is deferred by `_can_start` if `upgrade.idle` is False.
  The upgrade framework handles workload lifecycle via `_on_upgrade_granted`
  in `upgrade.py`.
- **K8s charm:** `pebble_ready` fires on pod restart, which calls
  `_reconcile_pebble_layer`. Pebble services with `startup: enabled` start
  automatically on pod churn.

## Upgrade Path
- Machine charm upgrades: snap is refreshed in-place. The `start` hook is
  deferred during upgrade. Any new "enable X by default" change must also
  be added to `_on_upgrade_granted` in `upgrade.py` to take effect on
  existing deployments.
- K8s charm upgrades: pod restarts → `pebble_ready` → services reconcile.

## Things to Check When Backporting
1. **Hook coverage on machine charm:** If the original PR added behaviour
   to `workload_initialise` (runs under `start`), check whether
   `_on_upgrade_granted` in `upgrade.py` needs the same call — because
   `start` is deferred during charm upgrades by the `upgrade.idle` guard
   in `_can_start`.
2. **K8s vs machine parity:** If the original PR touched both `kubernetes/`
   and `machines/`, verify the K8s side doesn't need a separate fix — K8s
   uses pebble layers, not snap services.

## Test Commands
- Unit tests: `PYTHONPATH=src:lib poetry run pytest tests/unit/ -q`
- Lint: `poetry run ruff check src/ tests/`
- Format check: `poetry run ruff format --check --diff src/ tests/`

## Known Divergences Between Branches
- 8.4 `workload_initialise` has an `is_data_dir_initialised()` shortcut
  branch; 8.0 does not.
- 8.4 uses `charmed-stats` as monitoring username; 8.0 uses `monitoring`.
- 8.4 `_on_set_password` doesn't guard exporter restart on `has_cos_relation`;
  8.0 had the guard (must be removed when backporting).

## Files of Interest
- `machines/src/charm.py` — main charm logic, hooks, workload_initialise.
- `machines/src/upgrade.py` — upgrade handling, `_on_upgrade_granted`.
- `machines/lib/charms/mysql/v0/mysql.py` — shared MySQL library.
- `machines/tests/unit/test_charm.py` — unit tests for charm.
```

**Why this works:** The SKILL.md is essentially the kind of context a senior engineer would explain to a junior engineer doing their first backport. It captures the *tacit knowledge* that git can't see. The gap analyzer feeds this to the LLM as grounding context.

**Maintenance:** The SKILL.md is strictly manually maintained by human experts. It represents the kind of context a senior engineer would explain to a junior engineer doing their first backport. The tool never modifies it automatically. Keeping it accurate and current is the repository maintainer's responsibility.

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
- ✅ `ruff check` passed
- ✅ `pytest tests/unit/test_charm.py` — 32 passed
- ⚠️ `pytest tests/unit/test_upgrade.py` — 1 failed
  (gap analyzer flagged missing upgrade.py change — see below)

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

4. **Propose SKILL.md updates (LLM, only if human-added changes exist):**
   ```
   A backport was performed from <source> to <target>. The tool
   suggested gap fixes; the human's final merged result differed.

   Human-added changes (changes the human made that the tool did NOT
   suggest):
   <diff of human-added changes>

   Applied suggestions: <list>
   Rejected suggestions: <list>

   Current SKILL.md:
   <content>

   Propose minimal updates to the SKILL.md "Things to Check When
   Backporting" and "Known Divergences" sections that would have let
   the tool catch these human-added changes itself next time. Do not
   duplicate existing entries. Output a unified diff against SKILL.md.
   ```

5. **Output:**
   - Print the classification summary (applied / rejected / human-added).
   - Print the proposed SKILL.md diff for review.
   - With `--commit`: apply the diff and commit it as `chore(bpilot): update SKILL.md from backport learnings`. Without: leave the diff for the user to apply manually.
   - With `--no-llm`: print the classification and diff, skip the SKILL.md suggestion step.

6. **Why this matters:** each human correction that bpilot missed becomes a candidate checklist item for the next backport — but only after a human reviews and accepts the SKILL.md update. The skill file converges toward a comprehensive, team-vetted map of branch divergences.

## CLI UX Summary

```bash
# Configure (one-time)
sudo snap set bpilot openrouter-api-key="sk-or-..."
sudo snap set bpilot model="openrouter/z-ai/glm-5.2"

# --- port ---
bpilot port abc123 8.0/edge                    # single commit
bpilot port HEAD~3..HEAD 8.0/edge              # commit range
bpilot port abc123 8.0/edge --no-llm           # mechanical only
bpilot port abc123 8.0/edge --skill-file ./docs/SKILL.md
bpilot port abc123 8.0/edge --dry-run          # show diff, no branch
bpilot port abc123 8.0/edge --model openrouter/anthropic/claude-3.5-sonnet

# review bpilot's work (incl. labelled bpilot(gap): commits), edit, run tests

# --- finalize ---
bpilot finalize                                # classify delta + propose SKILL.md updates
bpilot finalize --commit                       # also commit the SKILL.md suggestion
bpilot finalize --no-llm                       # classification only, no SKILL.md suggestion
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
