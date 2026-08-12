"""Tests for bpilot.report — markdown rendering of port/finalize reports."""

from __future__ import annotations

from bpilot.git_ops import CherryPickResult
from bpilot.llm_client import UsageStats
from bpilot.report import FinalizeReport, PortReport
from bpilot.session import GapFinding


def _usage() -> UsageStats:
    u = UsageStats(model="z-ai/glm-5.2")
    u.add(prompt_tokens=1000, completion_tokens=500, model="z-ai/glm-5.2")
    return u


def test_port_report_clean_pick():
    report = PortReport(
        source_commits=["abc123"],
        target_branch="8.0/edge",
        backport_branch="backport/abc123-to-8.0-edge",
        backup_ref="bpilot/backup/2026-07-20-143022",
        cherry_pick_results=[CherryPickResult(commit="abc123", applied=True)],
        llm_usage=_usage(),
    )
    md = report.render()
    assert "# Backport Report" in md
    assert "applied cleanly" in md
    assert "8.0/edge" in md
    assert "1 call(s)" in md


def test_port_report_with_conflicts():
    result = CherryPickResult(
        commit="abc123",
        applied=False,
        conflicts=True,
        conflicted_files=["machines/src/charm.py"],
        message="1 conflict(s)",
    )
    report = PortReport(
        source_commits=["abc123"],
        target_branch="8.0/edge",
        backport_branch="backport/abc123",
        backup_ref="bpilot/backup/x",
        cherry_pick_results=[result],
    )
    md = report.render()
    assert "conflicts" in md
    assert "machines/src/charm.py" in md


def test_port_report_no_llm():
    report = PortReport(
        source_commits=["abc123"],
        target_branch="8.0/edge",
        backport_branch="backport/abc123",
        backup_ref="bpilot/backup/x",
        no_llm=True,
    )
    md = report.render()
    assert "skipped (--no-llm)" in md
    assert "Skipped" in md  # gap analysis section


def test_port_report_with_gap_findings():
    finding = GapFinding(
        commit="def456",
        checklist_item="hook coverage",
        severity="critical",
        file="machines/src/upgrade.py",
        description="missing _on_upgrade_granted call",
    )
    report = PortReport(
        source_commits=["abc123"],
        target_branch="8.0/edge",
        backport_branch="backport/abc123",
        backup_ref="bpilot/backup/x",
        gap_findings=[finding],
    )
    md = report.render()
    assert "Gap 1" in md
    assert "missing _on_upgrade_granted call" in md
    assert "def456" in md
    assert "bpilot(gap):" not in md or "git reset" in md


def test_port_report_writes_file(tmp_path):
    report = PortReport(
        source_commits=["abc123"],
        target_branch="8.0/edge",
        backport_branch="backport/abc123",
        backup_ref="bpilot/backup/x",
    )
    path = report.write(tmp_path)
    assert path == tmp_path / "BACKPORT_REPORT.md"
    assert path.is_file()


def test_finalize_report_classification():
    report = FinalizeReport(
        applied=["gap fix 1"],
        rejected=["gap fix 2"],
        human_added_diff="diff --git a/file.py b/file.py\n",
    )
    md = report.render()
    assert "**Applied suggestions:** 1" in md
    assert "**Rejected suggestions:** 1" in md
    assert "Human-Added Changes" in md


def test_finalize_report_no_lessons():
    report = FinalizeReport()
    md = report.render()
    assert "No new lessons learned" in md
