"""BACKPORT_REPORT.md generation.

The report is the human-facing summary of a `bpilot port` run. It records
what was cherry-picked, what conflicted, what the LLM did (if anything),
what gap fixes were applied, and how to roll back.

This module renders a list of structured results into markdown. Keeping
rendering separate from the orchestration in cli.py makes it easy to
test and to extend later (e.g. machine-readable JSON output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bpilot.git_ops import CherryPickResult
from bpilot.llm_client import UsageStats
from bpilot.session import ConflictResolution, GapFinding, PotentialGap

REPORT_FILE = "BACKPORT_REPORT.md"


@dataclass
class PortReport:
    """Inputs for a `bpilot port` report."""

    source_commits: list[str]
    target_branch: str
    backport_branch: str
    backup_ref: str
    cherry_pick_results: list[CherryPickResult] = field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = field(default_factory=list)
    gap_findings: list[GapFinding] = field(default_factory=list)
    potential_gaps: list[PotentialGap] = field(default_factory=list)
    gap_skipped: bool = False
    gap_skip_reason: str = ""
    gap_verification_notes: list[str] = field(default_factory=list)
    llm_usage: UsageStats | None = None
    no_llm: bool = False
    validation_notes: list[str] = field(default_factory=list)
    verification_notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    paused_commit: str = ""
    paused_failed_files: list[str] = field(default_factory=list)

    @property
    def paused(self) -> bool:
        return bool(self.paused_commit)

    def render(self) -> str:
        """Render the report as markdown text."""
        lines: list[str] = []
        lines.append("# Backport Report")
        lines.append("")
        lines.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**Source commits:** {', '.join(self.source_commits) or '(none)'}")
        lines.append(f"**Target branch:** {self.target_branch}")
        lines.append(f"**Backport branch:** {self.backport_branch}")
        lines.append(f"**Backup ref:** {self.backup_ref}")
        lines.append(self._render_cost())
        lines.append("")

        lines.append("## Cherry-pick Result")
        if not self.cherry_pick_results:
            lines.append("- (no commits cherry-picked)")
        resolved_paths = {
            r.path: r.attempts for r in self.conflict_resolutions if r.applied and r.attempts > 0
        }
        failed_paths = {r.path for r in self.conflict_resolutions if not r.applied}
        for r in self.cherry_pick_results:
            if r.clean:
                lines.append(f"- applied cleanly: `{r.commit}`")
            elif r.conflicts:
                if self.paused and r.commit == self.paused_commit:
                    lines.append(f"- PAUSED (manual resolution needed): `{r.commit}`")
                elif any(f in failed_paths for f in r.conflicted_files):
                    lines.append(f"- partial resolution: `{r.commit}` — {r.message}")
                elif any(f in resolved_paths for f in r.conflicted_files):
                    lines.append(f"- conflicts resolved automatically: `{r.commit}`")
                else:
                    lines.append(f"- conflicts: `{r.commit}` — {r.message}")
                for f in r.conflicted_files:
                    if f in resolved_paths:
                        lines.append(
                            f"  - `{f}` — resolved by LLM (attempt {resolved_paths[f]})"
                        )
                    elif f in failed_paths:
                        lines.append(f"  - `{f}` — FAILED, needs manual resolution")
                    else:
                        lines.append(f"  - `{f}`")
            else:
                lines.append(f"- failed: `{r.commit}` — {r.message}")
        lines.append("")

        if self.validation_notes:
            lines.append("## Validation")
            for note in self.validation_notes:
                lines.append(f"- {note}")
            lines.append("")

        if self.verification_notes:
            lines.append("## Verification Checks (format / lint / unit tests)")
            for note in self.verification_notes:
                lines.append(f"- {note}")
            lines.append("")

        if self.gap_findings or self.potential_gaps or self.gap_verification_notes:
            lines.append("## Gap Analysis")
            for i, finding in enumerate(self.gap_findings, start=1):
                lines.append(f"### Gap {i}: {finding.description}")
                lines.append(f"- **Severity:** {finding.severity}")
                lines.append(f"- **Checklist item:** {finding.checklist_item}")
                lines.append(f"- **File:** `{finding.file}`")
                if finding.applied:
                    lines.append(f"- **Fix commit:** `{finding.commit}`")
                else:
                    lines.append("- **Fix:** not applied (manual review needed)")
            if self.potential_gaps:
                lines.append("")
                lines.append("### Potential gaps")
                for pg in self.potential_gaps:
                    lines.append(f"- **Checklist item:** {pg.checklist_item}")
                    lines.append(f"  **Question:** {pg.question}")
                    if pg.files_examined:
                        lines.append(f"  **Files examined:** {', '.join(pg.files_examined)}")
            if self.gap_verification_notes:
                lines.append("")
                for note in self.gap_verification_notes:
                    lines.append(f"- {note}")
            lines.append("")
        elif self.gap_skipped:
            lines.append("## Gap Analysis")
            lines.append(f"- Skipped ({self.gap_skip_reason}).")
            lines.append("")
        elif self.no_llm:
            lines.append("## Gap Analysis")
            lines.append("- Skipped (`--no-llm`). LLM features were not invoked.")
            lines.append("")
        else:
            lines.append("## Gap Analysis")
            lines.append("- No gaps found.")
            lines.append("")

        if self.errors:
            lines.append("## Errors")
            for err in self.errors:
                lines.append(f"- {err}")
            lines.append("")

        if self.paused:
            lines.append("## Next Steps — Manual Resolution Required")
            lines.append(
                "bpilot could not auto-resolve all conflicts. The resolved files in "
                f"the paused commit `{self.paused_commit}` were committed; the files "
                "below are left as uncommitted working-tree changes."
            )
            lines.append("")
            lines.append("1. Resolve these files (conflict markers are in the working tree):")
            for f in self.paused_failed_files:
                lines.append(f"   - `{f}`")
            lines.append("2. Stage your fixes: `git add <file>`")
            lines.append(
                "3. Run `bpilot port <commits> <target> --continue` with the same "
                "arguments. Your staged fixes will be amended into the paused commit "
                "(you can edit the commit message), then the run resumes."
            )
            lines.append(
                "   To abandon instead, run `bpilot reset` (discards the backport "
                "branch and session)."
            )
            lines.append("")
        else:
            lines.append("## Next Steps")
            lines.append(
                "1. Review any `bpilot(gap):` commits (`git log`, `git show`); drop any you disagree with."
            )
            if self.potential_gaps:
                lines.append(
                    "2. Answer the potential-gap questions above; if any reveal a real "
                    "gap, fix it and let `bpilot finalize` learn from it."
                )
                lines.append("3. Re-run tests after any manual changes.")
                lines.append(
                    "4. Run `bpilot finalize` to propose SKILL.md updates from manual changes."
                )
                lines.append("5. Push branch and open PR.")
            else:
                lines.append("2. Re-run tests after any manual changes.")
                lines.append(
                    "3. Run `bpilot finalize` to propose SKILL.md updates from manual changes."
                )
                lines.append("4. Push branch and open PR.")
            lines.append("")

        lines.append("## Rollback")
        lines.append("If anything went wrong, restore the pre-backport state:")
        lines.append("```")
        lines.append(f"git reset --hard {self.backup_ref}")
        lines.append("```")
        lines.append("")
        return "\n".join(lines)

    def _render_cost(self) -> str:
        if self.no_llm:
            return "**LLM cost:** skipped (--no-llm)"
        if not self.llm_usage or self.llm_usage.calls == 0:
            return "**LLM cost:** 0 calls (no LLM inference was needed)"
        u = self.llm_usage
        return (
            f"**LLM cost:** {u.calls} call(s) — "
            f"{u.prompt_tokens:,} prompt tokens + "
            f"{u.completion_tokens:,} completion tokens "
            f"(model: {u.model})"
        )

    def write(self, repo_root: Path) -> Path:
        """Render and write the report to `repo_root/BACKPORT_REPORT.md`."""
        path = repo_root / REPORT_FILE
        path.write_text(self.render())
        return path


@dataclass
class FinalizeReport:
    """Inputs for a `bpilot finalize` report."""

    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    human_added_diff: str = ""
    proposed_skill_diff: str = ""
    llm_usage: UsageStats | None = None
    no_llm: bool = False

    def render(self) -> str:
        lines: list[str] = []
        lines.append("# Finalize Report")
        lines.append("")
        lines.append("## Classification")
        lines.append(f"- **Applied suggestions:** {len(self.applied)}")
        for s in self.applied:
            lines.append(f"  - {s}")
        lines.append(f"- **Rejected suggestions:** {len(self.rejected)}")
        for s in self.rejected:
            lines.append(f"  - {s}")
        human_added_count = 1 if self.human_added_diff.strip() else 0
        lines.append(f"- **Human-added changes:** {human_added_count} block(s)")
        lines.append("")

        if self.human_added_diff.strip():
            lines.append("## Human-Added Changes")
            lines.append("```diff")
            lines.append(self.human_added_diff.rstrip())
            lines.append("```")
            lines.append("")

        if self.proposed_skill_diff.strip():
            lines.append("## Proposed SKILL.md Update")
            lines.append("```diff")
            lines.append(self.proposed_skill_diff.rstrip())
            lines.append("```")
            lines.append("")
        elif self.no_llm:
            lines.append("## Proposed SKILL.md Update")
            lines.append("- Skipped (`--no-llm`). Classification only.")
            lines.append("")
        elif not self.human_added_diff.strip():
            lines.append("## Proposed SKILL.md Update")
            lines.append("- No new lessons learned; SKILL.md unchanged.")
            lines.append("")

        if self.llm_usage and self.llm_usage.calls:
            u = self.llm_usage
            lines.append(
                f"**LLM cost:** {u.calls} call(s) — "
                f"{u.prompt_tokens:,} + {u.completion_tokens:,} tokens"
            )
        return "\n".join(lines)
