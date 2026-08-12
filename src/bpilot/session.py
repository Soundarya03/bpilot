"""Session snapshot — the bridge between `port` and `finalize`.

`port` writes a snapshot recording bpilot's end state (HEAD SHA, gap
findings with their commits, conflict resolutions, original commits,
target branch). `finalize` reads it back to diff current state against
what `port` produced.

Storage (per BACKPORT_HELPER_PLAN.md §Session state):
  - Local CLI: `.bpilot/session.json` in the working tree. `.bpilot/`
    is untracked and expected to be added to `.gitignore`.
  - GH bot: a hidden HTML comment embedded in the draft PR body. That
    transport is implemented when the bot lands (Phase 12); this module
    provides the (de)serialisation shared by both transports.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SESSION_DIR = Path(".bpilot")
SESSION_FILE = SESSION_DIR / "session.json"

# Hidden HTML comment used to embed the snapshot in a PR body for the
# GH bot. Matches the form: <!-- bpilot:session {...json...} -->
PR_COMMENT_PREFIX = "<!-- bpilot:session "
PR_COMMENT_SUFFIX = " -->"
_PR_COMMENT_RE = re.compile(
    re.escape(PR_COMMENT_PREFIX) + r"(.*?)" + re.escape(PR_COMMENT_SUFFIX),
    re.DOTALL,
)


@dataclass
class ConflictResolution:
    """Record of one conflict resolved by the LLM."""

    commit: str
    path: str
    attempts: int
    applied: bool


@dataclass
class GapFinding:
    """Record of one gap-fix commit produced by the gap analyzer."""

    commit: str
    checklist_item: str
    severity: str  # "critical" | "important" | "minor"
    file: str
    description: str
    applied: bool = True


@dataclass
class Session:
    """Everything `finalize` needs to diff against `port`'s output."""

    port_head_sha: str
    target_branch: str
    backport_branch: str
    backup_ref: str
    original_branch: str = ""
    source_commits: list[str] = field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = field(default_factory=list)
    gap_findings: list[GapFinding] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        resolutions = [
            ConflictResolution(**r)
            for r in data.get("conflict_resolutions", [])
            if isinstance(r, dict)
        ]
        findings = [GapFinding(**f) for f in data.get("gap_findings", []) if isinstance(f, dict)]
        return cls(
            port_head_sha=data["port_head_sha"],
            target_branch=data["target_branch"],
            backport_branch=data["backport_branch"],
            backup_ref=data["backup_ref"],
            original_branch=data.get("original_branch", ""),
            source_commits=list(data.get("source_commits", [])),
            conflict_resolutions=resolutions,
            gap_findings=findings,
            llm_usage=dict(data.get("llm_usage", {})),
        )


def save_session(session: Session, *, repo_root: Path) -> Path:
    """Persist the session to `.bpilot/session.json` under `repo_root`.

    Creates the `.bpilot/` directory if missing. The directory is
    expected to be added to `.gitignore` by the user.
    """
    session_dir = repo_root / SESSION_DIR
    session_dir.mkdir(exist_ok=True)
    path = repo_root / SESSION_FILE
    path.write_text(json.dumps(session.to_dict(), indent=2))
    return path


def load_session(*, repo_root: Path) -> Session | None:
    """Load the session from `.bpilot/session.json`, or None if absent."""
    path = repo_root / SESSION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return Session.from_dict(data)
    except KeyError:
        return None


def clear_session(*, repo_root: Path) -> None:
    """Remove the `.bpilot/` directory and `BACKPORT_REPORT.md` if present.

    Used by `bpilot reset` to discard all session state.
    """
    import shutil

    bpilot_dir = repo_root / SESSION_DIR
    if bpilot_dir.is_dir():
        shutil.rmtree(bpilot_dir)
    report = repo_root / "BACKPORT_REPORT.md"
    if report.is_file():
        report.unlink()


def embed_in_pr_body(session: Session, *, body: str) -> str:
    """Append the session as a hidden HTML comment to a PR body."""
    payload = json.dumps(session.to_dict(), separators=(",", ":"))
    comment = f"{PR_COMMENT_PREFIX}{payload}{PR_COMMENT_SUFFIX}"
    # Replace any existing bpilot session comment (idempotent for re-runs).
    cleaned = _PR_COMMENT_RE.sub("", body).rstrip()
    return f"{cleaned}\n\n{comment}\n" if cleaned else f"{comment}\n"


def extract_from_pr_body(body: str) -> Session | None:
    """Parse the hidden session comment from a PR body, or None."""
    match = _PR_COMMENT_RE.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    try:
        return Session.from_dict(data)
    except KeyError:
        return None
