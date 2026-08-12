"""Tests for bpilot.session — snapshot (de)serialisation and PR-body transport."""

from __future__ import annotations

import json
from pathlib import Path

from bpilot.session import (
    GapFinding,
    Session,
    embed_in_pr_body,
    extract_from_pr_body,
    load_session,
    save_session,
)


def _sample_session() -> Session:
    return Session(
        port_head_sha="abc123",
        target_branch="8.0/edge",
        backport_branch="backport/abc123-to-8.0-edge",
        backup_ref="bpilot/backup/2026-07-20-143022",
        source_commits=["abc123"],
        gap_findings=[
            GapFinding(
                commit="def456",
                checklist_item="hook coverage",
                severity="critical",
                file="machines/src/upgrade.py",
                description="missing _on_upgrade_granted call",
            )
        ],
    )


def test_session_roundtrip(tmp_path: Path):
    session = _sample_session()
    path = save_session(session, repo_root=tmp_path)
    assert path == tmp_path / ".bpilot" / "session.json"
    loaded = load_session(repo_root=tmp_path)
    assert loaded is not None
    assert loaded.port_head_sha == session.port_head_sha
    assert loaded.target_branch == session.target_branch
    assert loaded.gap_findings[0].description == session.gap_findings[0].description
    assert loaded.gap_findings[0].applied is True


def test_load_session_missing(tmp_path: Path):
    assert load_session(repo_root=tmp_path) is None


def test_load_session_corrupt(tmp_path: Path):
    path = tmp_path / ".bpilot" / "session.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert load_session(repo_root=tmp_path) is None


def test_embed_and_extract_in_pr_body():
    session = _sample_session()
    body = "## Backport PR\n\nSome review notes."
    embedded = embed_in_pr_body(session, body=body)
    extracted = extract_from_pr_body(embedded)
    assert extracted is not None
    assert extracted.port_head_sha == session.port_head_sha
    assert extracted.gap_findings[0].file == session.gap_findings[0].file


def test_embed_in_pr_body_replaces_existing():
    session = _sample_session()
    body = "## PR\n"
    first = embed_in_pr_body(session, body=body)
    second = embed_in_pr_body(session, body=first)
    # Only one session comment should be present.
    assert second.count("<!-- bpilot:session") == 1


def test_extract_from_pr_body_absent():
    assert extract_from_pr_body("nothing here") is None


def test_embedded_payload_is_valid_json():
    session = _sample_session()
    embedded = embed_in_pr_body(session, body="")
    # Extract the JSON between the markers and verify it parses.
    start = embedded.find("<!-- bpilot:session ") + len("<!-- bpilot:session ")
    end = embedded.rfind(" -->")
    payload = embedded[start:end]
    parsed = json.loads(payload)
    assert parsed["port_head_sha"] == session.port_head_sha
