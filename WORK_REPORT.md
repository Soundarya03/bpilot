# Work Report

## Summary

bpilot is a snap-packaged backport helper that automates git cherry-picks
and provides LLM-assisted conflict resolution. This report tracks
implementation progress against BACKPORT_HELPER_PLAN.md.

## Completed

### Phase 1: Project Skeleton & Snap Packaging
- Python project with `pyproject.toml` (Poetry build system).
- Snap packaged with `snapcraft.yaml` (core24, classic confinement,
  poetry plugin). See LEARNINGS.md and FRICTION_LOG.md for the packaging
  journey.
- CLI entry point (`bpilot` console script) with `port` and `finalize`
  subcommands.

### Phase 2: Git Operations Layer (`git_ops.py`)
- Cherry-pick (with merge-commit detection via `-m 1`).
- Backup ref creation (`bpilot/backup/<timestamp>`).
- Backport branch creation (`backport/<hash>-to-<target>`).
- Conflict detection, file content retrieval, patch application.
- `apply_patch()` — the sole path for LLM-produced text to touch the
  tree, with apply-check + scope check + conflict-marker check.

### Phase 3: Configuration (`config.py`)
- Snap config (`$SNAP_DATA/bpilot.conf`) → env vars → defaults.
- OpenRouter API key, model, max tokens, temperature.

### Phase 4: LLM Client (`llm_client.py`)
- Thin OpenRouter chat-completions client.
- Retry with backoff (3 attempts), cost tracking (UsageStats).

### Phase 5: Conflict Resolver (`resolver.py`)
- LLM-assisted resolution of cherry-pick conflicts.
- Per-file resolution with up to 3 retries.
- Prompt includes: conflicted content, target branch version, commit
  message, relevant SKILL.md sections, previous error feedback.
- Patch extracted from LLM response (strips markdown fences), applied
  via `git_ops.apply_patch()`, validated by `validator.py`.
- Wired into CLI `port` flow: on conflict, resolves each file, then
  `git cherry-pick --continue`.

### Phase 6: Static Validator (`validator.py`)
- Conflict-marker check (no leftover `<<<<<<<`, `=======`, `>>>>>>>`).
- Syntax check (`python -m py_compile` for .py files).
- Placeholder check (TODO, FIXME, ???, XXX).

### Phase 8: SKILL.md Loader (`skill_loader.py`)
- Parses `## Section` headers into a dict.
- `get_section()` / `get_sections()` for targeted LLM context.
- `checklist_items` for gap analyzer (Phase 7, not yet implemented).
- `test_commands` for validator (Phase 6).

### Phase 9: `--no-llm` Mode
- Mechanical cherry-pick only; aborts on conflict.

### Phase 10: Session Snapshot (`session.py`)
- `.bpilot/session.json` for local CLI.
- Hidden HTML comment embedding for GH bot (PR body transport).

## Test Coverage
- 66 unit tests, all passing.
- `ruff check` and `ruff format --check` clean.
- Tests cover: config resolution, git ops (against temp repos), session
  round-trip, report rendering, skill loader parsing, validator checks,
  resolver apply-validate-retry loop (with mocked LLM).

## Open Items / Next Steps

1. **Phase 7: Gap Analyzer** — checklist-driven LLM analysis for
   semantic gaps (the key differentiator). Needs `gap_analyzer.py`.
2. **Phase 11: Finalize** — feedback loop that diffs human changes vs
   bpilot's suggestions and proposes SKILL.md updates.
3. **Phase 12: GitHub Bot** — comment-driven `/port` and `/finalize`
   workflow.
4. **Integration test with real LLM** — the resolver tests use mocked
   LLM responses; a real end-to-end test needs an API key.
5. **`poetry.lock` regeneration** — the lock file is inconsistent with
   the pyproject.toml (warning during snap build, non-blocking).
