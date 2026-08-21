# bpilot

Intelligent backport helper for git repositories. bpilot automates the
mechanical parts of backporting (fetch, branch, cherry-pick) and adds
an LLM-assisted layer for conflict resolution and semantic gap analysis.

The LLM is a pure text-in/text-out function. It never sees git
credentials and never executes anything — all repo mutation goes
through the deterministic git-ops layer. bpilot never merges.

## Usage

bpilot offers 4 commands. All to be run from the root of your project.

```bash
bpilot init                                # one-time setup, before first run: scaffold bpilot/skills/ + .bpilot/ for this project
bpilot port <commit(s)> <target>           # backport commits from current branch onto target branch
bpilot finalize                            # optional; propose SKILL.md updates from a finished backport
bpilot reset                               # discard session at any point in time during porting, return to original branch
```

### `bpilot init`

Scaffolds `bpilot/skills/` with five starter `SKILL.md` files
(valid frontmatter, empty section bodies) and the `.bpilot/` session
directory (added to `.gitignore`). When the LLM is available it
refines two of the skill files from repo content:

- `verification-checks` — inferred from `README.md`,
  `CONTRIBUTING.md`, `pyproject.toml`, and other config files.
- `version-control` — inferred from `README.md` + a git analysis of
  branch names and recent commit messages.

The other three skills (`conflict-resolution`, `gap-analysis`,
`general-context`) are left as placeholders; they're learned over time
via `finalize`. Refuses to clobber an existing `bpilot/skills/` directory.

`bpilot port` also auto-scaffolds on first run (unless `--no-init` is
given), so `init` is optional — but recommended for the richer
LLM-inferred starter content.

### `bpilot port <commits> <target>`

```bash
bpilot port abc123 8.0/edge                # single commit
bpilot port HEAD~3..HEAD 8.0/edge          # commit range
bpilot port abc123 8.0/edge --no-llm       # mechanical cherry-pick only
bpilot port abc123 8.0/edge --dry-run      # show what would happen (still scaffolds)
bpilot port abc123 8.0/edge --skip-baseline # skip the pre-cherry-pick baseline checks
bpilot port abc123 8.0/edge --no-fetch     # skip fetching; use the local target ref as-is
bpilot port --continue                      # resume a paused run after manual conflict fixes
```

If some conflicts can't be auto-resolved, the run **pauses**: the resolved
files are committed, the failed ones are left in the working tree with
conflict markers, and `BACKPORT_REPORT.md` explains what's outstanding.
Fix the failed files, `git add` them, then run `bpilot port --continue`
(no need to repeat `<commits>`/`<target>`) — your fixes are amended into
the paused commit and any remaining commits are cherry-picked. To abandon
a paused run instead, use `bpilot reset`.

By default the target branch is fetched from `origin` before branching.
With `--no-fetch` the fetch is skipped and the local target ref is used
as-is — useful when the environment running bpilot has no git
credentials (e.g. a dev VM): fetch elsewhere first, as the local ref
may be stale.

Produces:

- Local branch `backport/<short-hash>-to-<target>` with the
  cherry-picked + resolved changes, plus one labelled
  `bpilot(gap): ...` commit per applied gap fix.
- `BACKPORT_REPORT.md` — what was done, gaps found (each mapped to its
  commit), token cost.
- `.bpilot/session.json` — bpilot's end state, consumed later by
  `finalize`.

Review the branch (including the labelled `bpilot(gap):` commits —
drop any you disagree with via `git rebase -i` / `git reset`), make
manual edits, and run tests.

### `bpilot finalize`

Proposes per-skill `SKILL.md` updates from a finished backport —
diffing what bpilot suggested vs. what was accepted, classifying human
edits, and asking the LLM to refine the skill files accordingly.
Always human-reviewed; never auto-applied to a skill file.

### `bpilot reset`

Abort any in-progress cherry-pick, switch back to the original branch,
force-delete the backport branch, and remove any session files (`.bpilot/` and
`BACKPORT_REPORT.md`).

## API key setup

bpilot uses [OpenRouter](https://openrouter.ai) for LLM inference. Get
an API key from your OpenRouter dashboard, then provide it via one of:

**Environment variable** (works everywhere — local dev, CI, GitHub
Actions):

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

**Snap config** (when running the snap; persists across sessions):

```bash
sudo snap set bpilot openrouter-api-key="sk-or-..."
```

Without a key, bpilot falls back to `--no-llm` mode: mechanical
cherry-pick only, no conflict resolution, no gap analysis. The
`--no-llm` flag forces this regardless of key availability.

## Configure bpilot

Resolution order (highest precedence first):

1. CLI flags (`--model`, `--no-llm`, `--skills-dir`, ...)
2. Environment variables
3. Snap config (`snap set bpilot <key>=<value>`)
4. Built-in defaults

| Setting | Env var | Snap key | Default |
|---|---|---|---|
| OpenRouter API key | `OPENROUTER_API_KEY` | `openrouter-api-key` | (none) |
| Model | `BPILOT_MODEL` | `model` | `z-ai/glm-5.2` |
| Max tokens | `BPILOT_MAX_TOKENS` | `max-tokens` | `16000` |

The API key never leaves the config module — it is forwarded to
`llm_client` only and is never included in LLM prompts.

### Skills directory

bpilot grounds its LLM prompts in repo-specific knowledge stored under
`bpilot/skills/` — a directory of named skills, each in its own
subdirectory containing a `SKILL.md`:

```
bpilot/
└── skills/
    ├── version-control/SKILL.md       # branch + commit-message conventions
    ├── verification-checks/SKILL.md   # format / lint / unit-test commands
    ├── conflict-resolution/SKILL.md   # skip-file patterns + resolution rules
    ├── gap-analysis/SKILL.md          # "Things to Check When Backporting" checklist
    └── general-context/SKILL.md       # shared: lifecycle hooks, upgrade paths, files of interest
```

Each `SKILL.md` conforms to the [Agent Skills
specification](https://agentskills.io/specification): YAML frontmatter
(required `name` matching the parent directory, and `description`)
followed by a markdown body of `## Section` headers. The set of skills
is fixed (4 task skills + `general-context`); see
`SKILLS_DIRECTORY_SPEC.md` for the full spec and the section → skill
mapping.

Point bpilot at a non-default location with `--skills-dir`.

## Swimlane diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  bpilot port                                │
├──────────────┬──────────────┬──────────────┬──────────────┬─────────────────┤
│  Git ops     │  Resolver    │  Verifier    │  Gap analyzer│  Snapshot       │
│              │  (LLM)       │  + Fixer     │  (LLM)       │  + Report       │
├──────────────┼──────────────┼──────────────┼──────────────┼─────────────────┤
│ fetch target │              │              │              │                 │
│ backup ref   │              │              │              │                 │
│ create branch│              │              │              │                 │
│ ↓            │              │              │              │                 │
│ baseline     │              │  baseline    │              │                 │
│ verification │              │  green?      │              │                 │
│ ↓            │              │  (fail → abort)             │                 │
│ cherry-pick  │              │              │              │                 │
│ each commit  │              │              │              │                 │
│ ↓            │              │              │              │                 │
│ conflict?    │              │              │              │                 │
│  yes ────────┼─────────────▶│              │              │                 │
│              │ resolve file │              │              │                 │
│              │ validate     │              │              │                 │
│              │ retry × 3    │              │              │                 │
│  no ─────────┼──────────────┼─────────────▶│              │                 │
│              │              │ run checks   │              │                 │
│              │              │ (format/lint/│              │                 │
│              │              │  unit tests) │              │                 │
│              │              │  fail? ──────┼─────────────▶│                 │
│              │              │              │ propose fix  │                 │
│              │              │              │ validate     │                 │
│              │              │              │ retry × 5    │                 │
│              │              │  green ───────┼──────────────┼────────────────▶│
│              │              │              │              │ analyze per     │
│              │              │              │              │ checklist item  │
│              │              │              │              │ (explore files) │
│              │              │              │              │  applies → fix  │
│              │              │              │              │  + commit       │
│              │              │              │              │  `bpilot(gap):` │
│              │              │              │              │  uncertain →    │
│              │              │              │              │  potential gap  │
│              │              │              │              │  ↓               │
│              │              │ re-run checks│              │                 │
│              │              │ once (no LLM)│              │                 │
│              │              │  ────────────┼──────────────┼────────────────▶│
│              │              │              │              │ write session   │
│              │              │              │              │ + BACKPORT_     │
│              │              │              │              │   REPORT.md     │
└──────────────┴──────────────┴──────────────┴──────────────┴─────────────────┘

                        ⟦ human reviews, edits, tests ⟧

┌─────────────────────────────────────────────────────────────────────────────┐
│                                bpilot finalize                              │
├──────────────────┬──────────────────┬──────────────────┬───────────────────┤
│  Load session    │  Diff vs port    │  Classify        │  Propose          │
│                  │  end state       │                  │  SKILL.md updates │
├──────────────────┼──────────────────┼──────────────────┼───────────────────┤
│ read .bpilot/    │                  │                  │                   │
│   session.json   │                  │                  │                   │
│ ↓                │                  │                  │                   │
│                  │ git diff         │ applied /        │ LLM refines       │
│                  │ port_head_sha    │ rejected /       │ skill files from  │
│                  │ vs. HEAD         │ human-added      │ human-added edits │
│                  │                  │                  │                   │
│                  │                  │                  │ human reviews,    │
│                  │                  │                  │ commits, raises   │
│                  │                  │                  │ PR                │
└──────────────────┴──────────────────┴──────────────────┴───────────────────┘
```

## Future: GitHub bot

The CLI is transport-agnostic; the same `port` / `finalize` flows will
be drivable from a GitHub bot via PR comments. Planned commands:

- `/port <commits> <target>` — open a draft backport PR.
- `/finalize` — propose SKILL.md updates on a finished backport PR.

The bot carries the session snapshot as a hidden HTML comment embedded
in the PR body (the `session.py` (de)serialisation and PR-body
transport already exist). The trust boundary is unchanged: the LLM
still never runs git, never writes files directly, and never executes
anything — all mutation goes through the git-ops layer. The bot only
opens draft PRs; it never merges.
