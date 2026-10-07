"""Gap analyzer — checklist-driven semantic analysis of a backport.

For each item in the gap-analysis skill's "Things to Check When
Backporting" checklist, reasons about whether the item applies to the
current backport, reading unchanged target-branch files via a bounded
exploration loop when needed. Applicable findings are fixed via the
complete-file protocol, validated, and committed as labelled
`bpilot(gap):` commits. Uncertain findings are surfaced as potential
gaps for the user.

Trust boundary (same as the resolver / verification fixer): the LLM is
a pure text-in/text-out function. It returns structured verdicts and
proposed file contents; bpilot does all reading (NEED: requests are
served by bpilot, never by the LLM) and all mutation (file I/O +
`commit_gap_fix`). Every fix passes deterministic validation before
commit. The LLM never runs git, never lists directories, and never
executes anything.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.git_ops import commit_gap_fix, get_backport_diff, get_changed_files
from bpilot.llm_client import LLMClient
from bpilot.session import GapFinding, PotentialGap
from bpilot.skill_loader import (
    SECTION_FILES_OF_INTEREST,
    SkillSet,
)
from bpilot.validator import validate_file

MAX_NEED_ROUNDS = 2  # max file-request rounds per checklist item
MAX_CONTEXT_FILES = 8  # total files supplied per item (initial + requested)
MAX_FILE_LINES = 400  # per-file content cap (truncated with marker)
MAX_GAP_FIX_ATTEMPTS = 2  # fix validation retries per applicable finding

# Safety valve for pathological diffs: truncate at this many lines and
# append a changed-file summary. The default remains full-diff-per-item.
_MAX_DIFF_LINES = 2000

_GAP_SKILL = "gap-analysis"

_VALID_SEVERITIES = ("critical", "important", "minor")


@dataclass
class GapResult:
    """Outcome of the full gap-analysis pass."""

    findings: list[GapFinding] = field(default_factory=list)
    potential_gaps: list[PotentialGap] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    llm_errors: list[str] = field(default_factory=list)


def analyze_gaps(
    *,
    target_branch: str,
    source_commits: list[str],
    commit_messages: list[tuple[str, str]],
    skill_set: SkillSet | None,
    llm: LLMClient | None,
    cwd: Path,
) -> GapResult:
    """Run checklist-driven gap analysis on the current branch state.

    The analyzer runs on the verified branch state — the caller invokes
    it after the verification-fixer loop, so the diff it reads reflects
    the final backported state including any verification amendments.

    Skip conditions (checked in order): no LLM, no gap-analysis skill,
    empty checklist. Each checklist item is analyzed in its own bounded
    conversation (see `_analyze_one_item`).
    """
    if llm is None:
        return GapResult(skipped=True, skip_reason="LLM features skipped (--no-llm)")

    gap_skill = skill_set.get(_GAP_SKILL) if skill_set is not None else None
    if gap_skill is None:
        return GapResult(skipped=True, skip_reason="no gap-analysis skill found")

    items = gap_skill.checklist_items
    if not items:
        return GapResult(skipped=True, skip_reason="gap-analysis checklist is empty")

    files_of_interest = _files_of_interest(skill_set)
    changed_files = get_changed_files(target_branch, cwd=cwd)

    findings: list[GapFinding] = []
    potential_gaps: list[PotentialGap] = []
    llm_errors: list[str] = []

    for item in items:
        print(f"  gap analysis: {item[:80]}")
        outcome = _analyze_one_item(
            item=item,
            target_branch=target_branch,
            source_commits=source_commits,
            commit_messages=commit_messages,
            skill_set=skill_set,
            llm=llm,
            cwd=cwd,
            changed_files=changed_files,
            files_of_interest=files_of_interest,
        )
        if outcome is None:
            # APPLIES: no — discarded entirely.
            continue
        if isinstance(outcome, PotentialGap):
            potential_gaps.append(outcome)
            continue
        if isinstance(outcome, _LLMError):
            llm_errors.append(outcome.message)
            continue
        # GapFinding (applied or not).
        findings.append(outcome)

    return GapResult(
        findings=findings,
        potential_gaps=potential_gaps,
        llm_errors=llm_errors,
    )


@dataclass
class _LLMError:
    """Internal tag for a malformed/unrecoverable response for one item."""

    message: str


def _analyze_one_item(
    *,
    item: str,
    target_branch: str,
    source_commits: list[str],
    commit_messages: list[tuple[str, str]],
    skill_set: SkillSet | None,
    llm: LLMClient,
    cwd: Path,
    changed_files: list[str],
    files_of_interest: list[str],
) -> GapFinding | PotentialGap | _LLMError | None:
    """Analyse one checklist item; return its outcome.

    Returns:
      - None — APPLIES: no (discarded).
      - PotentialGap — APPLIES: uncertain, or exploration budget exhausted.
      - GapFinding — APPLIES: yes (applied=True or False on fix failure).
      - _LLMError — malformed response after retry.
    """
    shown_files: set[str] = set(changed_files)
    files_examined: list[str] = list(changed_files)

    skill_context = _build_skill_context(skill_set)

    # Round 0 — initial prompt.
    diff = _bounded_diff(target_branch, cwd=cwd)
    repo_map = _build_repo_map(
        cwd=cwd, changed_files=changed_files, files_of_interest=files_of_interest
    )
    prompt = _build_round0_prompt(
        item=item,
        target_branch=target_branch,
        source_commits=source_commits,
        commit_messages=commit_messages,
        diff=diff,
        skill_context=skill_context,
        repo_map=repo_map,
    )

    last_parse_error = ""
    need_rounds = 0

    for _round_num in range(MAX_NEED_ROUNDS + 1):  # 0, 1, 2 → up to 3 LLM calls
        try:
            response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
        except Exception as err:  # noqa: BLE001 — record and bail on this item
            return _LLMError(message=f"LLM call failed for item: {err}")

        parsed = _parse_response(response.text)

        if parsed.malformed:
            if last_parse_error:
                # Already retried once — record and give up on this item.
                return _LLMError(message=f"malformed response for item: {parsed.error}")
            last_parse_error = parsed.error
            prompt = _build_retry_prompt(prompt, error=parsed.error)
            # Retry consumes a "round" — continue the loop without
            # incrementing need_rounds (this isn't a NEED round).
            continue

        if parsed.need_paths and parsed.verdict is not None:
            # Mixed NEED + APPLIES — treat as malformed.
            if last_parse_error:
                return _LLMError(
                    message="malformed response for item: mixed NEED: and APPLIES: verdict"
                )
            last_parse_error = "response contained both NEED: and APPLIES: — pick one"
            prompt = _build_retry_prompt(prompt, error=last_parse_error)
            continue

        if parsed.need_paths:
            # Exploration round.
            if need_rounds >= MAX_NEED_ROUNDS:
                # Budget exhausted — never silently drop.
                return PotentialGap(
                    checklist_item=item,
                    question=(
                        "analysis incomplete: needed more context than the "
                        "exploration budget allows"
                    ),
                    files_examined=files_examined,
                )
            need_rounds += 1

            valid: list[str] = []
            unavailable: list[str] = []
            for path in parsed.need_paths:
                if len(files_examined) + len(valid) >= MAX_CONTEXT_FILES:
                    unavailable.append(path)
                    continue
                if _validate_need_path(path, cwd=cwd):
                    valid.append(path)
                else:
                    unavailable.append(path)
                    print(f"  dropping requested path {path!r} (invalid)", file=sys.stderr)

            if valid:
                supplied = _supply_files(valid, cwd=cwd)
                files_examined.extend(valid)
                shown_files.update(valid)
                prompt = _build_followup_prompt(
                    prev_prompt=prompt,
                    supplied=supplied,
                    unavailable=unavailable,
                )
            elif unavailable:
                # All requested paths invalid — tell the LLM and re-query
                # without consuming another NEED round (we didn't supply
                # anything new).
                prompt = _build_followup_prompt(
                    prev_prompt=prompt, supplied=[], unavailable=unavailable
                )
                need_rounds -= 1
            continue

        # Final verdict reached.
        if parsed.verdict == "no":
            print(f"  APPLIES: no — {parsed.reason}")
            return None

        if parsed.verdict == "uncertain":
            if parsed.file_blocks:
                print(
                    "  ignoring speculative <bpilot-file> blocks in uncertain response",
                    file=sys.stderr,
                )
            return PotentialGap(
                checklist_item=item,
                question=parsed.question or "(no question provided)",
                files_examined=files_examined,
            )

        if parsed.verdict == "yes":
            return _apply_yes_finding(
                item=item,
                parsed=parsed,
                shown_files=shown_files,
                llm=llm,
                prev_prompt=prompt,
                cwd=cwd,
            )

        return _LLMError(message=f"unhandled verdict: {parsed.verdict!r}")

    # Exhausted all rounds without a final verdict.
    return PotentialGap(
        checklist_item=item,
        question="analysis incomplete: needed more context than the exploration budget allows",
        files_examined=files_examined,
    )


# --- response parsing -----------------------------------------------------


@dataclass
class _ParsedResponse:
    """Structured parse of one LLM response."""

    verdict: str | None = None  # "yes" | "no" | "uncertain" | None
    severity: str = ""
    file: str = ""
    description: str = ""
    reason: str = ""
    question: str = ""
    need_paths: list[str] = field(default_factory=list)
    file_blocks: dict[str, str] = field(default_factory=dict)
    malformed: bool = False
    error: str = ""


_FILE_BLOCK_RE = re.compile(
    r'<bpilot-file\s+path="([^"]+)">\s*(.*?)\s*</bpilot-file>',
    re.DOTALL,
)
_NEED_RE = re.compile(r"^\s*NEED:\s*(.+?)\s*$", re.MULTILINE)


def _parse_response(raw: str) -> _ParsedResponse:
    """Parse an LLM response into a structured verdict.

    Recognises three final verdicts (APPLIES: yes/no/uncertain) plus
    NEED: file-request lines. Mixed NEED + APPLIES is flagged malformed
    (caller decides retry vs. record). Missing/ambiguous verdict →
    malformed. Severity outside the allowed set is coerced to
    `important` (caller notes via stderr).
    """
    parsed = _ParsedResponse()

    # File blocks (parsed regardless — uncertain responses may carry
    # speculative blocks the caller will ignore).
    for match in _FILE_BLOCK_RE.finditer(raw):
        path = match.group(1).strip()
        content = match.group(2)
        if not content.endswith("\n"):
            content += "\n"
        parsed.file_blocks[path] = content

    # NEED: lines.
    parsed.need_paths = [m.group(1).strip() for m in _NEED_RE.finditer(raw)]

    # Verdict + header fields.
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().upper()
        value = value.strip()
        if key in ("APPLIES", "SEVERITY", "FILE", "DESCRIPTION", "REASON", "QUESTION") and (
            key not in fields
        ):
            fields[key] = value

    applies = fields.get("APPLIES", "").lower()
    if applies in ("yes", "no", "uncertain"):
        parsed.verdict = applies
    elif applies:
        parsed.malformed = True
        parsed.error = f"unrecognised APPLIES value: {applies!r}"
        return parsed
    # If no APPLIES line at all, fall through — malformed below unless
    # NEED: lines are present (exploration round, no verdict yet).

    parsed.severity = fields.get("SEVERITY", "")
    parsed.file = fields.get("FILE", "")
    parsed.description = fields.get("DESCRIPTION", "")
    parsed.reason = fields.get("REASON", "")
    parsed.question = fields.get("QUESTION", "")

    if parsed.verdict is None and not parsed.need_paths:
        parsed.malformed = True
        parsed.error = "no APPLIES: verdict and no NEED: request"
        return parsed

    if parsed.verdict == "yes":
        if not parsed.description:
            parsed.malformed = True
            parsed.error = "APPLIES: yes requires a DESCRIPTION"
            return parsed
        # Severity coercion.
        sev = parsed.severity.lower()
        if sev not in _VALID_SEVERITIES:
            if parsed.severity:
                print(
                    f"  coercing unknown severity {parsed.severity!r} to 'important'",
                    file=sys.stderr,
                )
            parsed.severity = "important"
        if not parsed.file_blocks:
            parsed.malformed = True
            parsed.error = "APPLIES: yes requires at least one <bpilot-file> block"
            return parsed

    if parsed.verdict == "uncertain" and not parsed.question:
        parsed.malformed = True
        parsed.error = "APPLIES: uncertain requires a QUESTION"
        return parsed

    return parsed


# --- NEED path validation + file supply -----------------------------------


def _validate_need_path(path: str, *, cwd: Path) -> bool:
    """True when `path` is a safe relative file in the working tree.

    Rejects: absolute paths, `..` traversal, `.git/` internals,
    nonexistent paths, and directories.
    """
    if not path or path != path.strip():
        return False
    if os.path.isabs(path):
        return False
    if ".." in Path(path).parts:
        return False
    if path == ".git" or path.startswith(".git/"):
        return False
    target = cwd / path
    return target.is_file()


def _supply_files(paths: list[str], *, cwd: Path) -> list[tuple[str, str]]:
    """Read + truncate the requested files for inclusion in the prompt.

    Returns (path, content_block) pairs. Each file's content is capped
    at `MAX_FILE_LINES` with a `# ... (truncated)` marker.
    """
    supplied: list[tuple[str, str]] = []
    for path in paths:
        try:
            raw = (cwd / path).read_text(errors="replace")
        except OSError as err:
            supplied.append((path, f'<bpilot-file path="{path}">READ_ERROR: {err}</bpilot-file>'))
            continue
        lines = raw.splitlines()
        if len(lines) > MAX_FILE_LINES:
            body = "\n".join(lines[:MAX_FILE_LINES]) + "\n# ... (truncated)\n"
        else:
            body = raw if raw.endswith("\n") else raw + "\n"
        supplied.append((path, f'<bpilot-file path="{path}">\n{body}</bpilot-file>'))
    return supplied


# --- fix application pipeline --------------------------------------------


def _apply_yes_finding(
    *,
    item: str,
    parsed: _ParsedResponse,
    shown_files: set[str],
    llm: LLMClient,
    prev_prompt: str,
    cwd: Path,
) -> GapFinding | _LLMError:
    """Apply an APPLIES: yes finding: scope-check, write, validate, commit.

    Bounded to `MAX_GAP_FIX_ATTEMPTS` retries. On a final failure the
    finding is recorded with `applied=False` (report renders "not
    applied"). A no-op fix (no diff vs. HEAD) is not committed and is
    recorded `applied=False` with an "already covered" note.
    """
    current = parsed
    last_error = ""

    for attempt in range(1, MAX_GAP_FIX_ATTEMPTS + 1):
        # 1. Scope check — the LLM may only touch files it was shown.
        out_of_scope = sorted(set(current.file_blocks) - shown_files)
        if out_of_scope:
            last_error = f"out-of-scope files: {out_of_scope}"
        else:
            # 2. Write.
            touched: list[str] = []
            write_err = ""
            try:
                for path, content in current.file_blocks.items():
                    (cwd / path).write_text(content)
                    touched.append(path)
            except OSError as err:
                write_err = f"could not write file: {err}"

            if not write_err:
                # 3. Static validation per touched file.
                errors: list[str] = []
                for path in touched:
                    vr = validate_file(path, cwd=cwd)
                    if not vr.ok:
                        errors.append(f"{path}: {'; '.join(vr.errors)}")
                if not errors:
                    # 4. No-op guard.
                    if not _has_diff_vs_head(cwd=cwd, paths=touched):
                        return GapFinding(
                            commit="",
                            checklist_item=item,
                            severity=current.severity,
                            file=current.file,
                            description=(
                                f"{current.description} (already covered by an earlier commit)"
                            ),
                            applied=False,
                        )

                    # 5. Commit.
                    _stage_files(touched, cwd=cwd)
                    try:
                        sha = commit_gap_fix(current.description, cwd=cwd)
                    except Exception as err:  # noqa: BLE001
                        return GapFinding(
                            commit="",
                            checklist_item=item,
                            severity=current.severity,
                            file=current.file,
                            description=f"{current.description} (commit failed: {err})",
                            applied=False,
                        )
                    return GapFinding(
                        commit=sha,
                        checklist_item=item,
                        severity=current.severity,
                        file=current.file,
                        description=current.description,
                        applied=True,
                    )
                last_error = "; ".join(errors)
            else:
                last_error = write_err

        # Retry: re-query the LLM with the error fed back.
        if attempt >= MAX_GAP_FIX_ATTEMPTS:
            break
        prompt = _build_retry_prompt(prev_prompt, error=last_error)
        try:
            response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
        except Exception as err:  # noqa: BLE001
            return _LLMError(message=f"LLM call failed during fix retry: {err}")
        current = _parse_response(response.text)
        if current.malformed:
            last_error = current.error
            continue
        if current.verdict != "yes" or not current.file_blocks:
            last_error = "retry did not produce an applicable fix"
            continue

    # Exhausted retries.
    return GapFinding(
        commit="",
        checklist_item=item,
        severity=current.severity,
        file=current.file,
        description=f"{current.description} (not applied: {last_error})",
        applied=False,
    )


def _has_diff_vs_head(*, cwd: Path, paths: list[str]) -> bool:
    """True when any of `paths` differs from HEAD (staged or unstaged)."""
    import subprocess

    if not paths:
        return False
    proc = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode != 0


def _stage_files(paths: list[str], *, cwd: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


# --- prompt construction --------------------------------------------------


def _build_skill_context(skill_set: SkillSet | None) -> str:
    """Concatenate gap-analysis + general-context skill bodies (mirrors the
    resolver's `context_for("conflict-resolution")` contract).

    Each included skill is prefixed with a `## <skill-name>` header;
    empty/absent skills are skipped; the whole block is "" when both
    are empty.
    """
    if skill_set is None:
        return ""
    return skill_set.context_for(_GAP_SKILL)


def _bounded_diff(target_branch: str, *, cwd: Path) -> str:
    """Full backport diff vs. target, truncated at ~2000 lines.

    Pathological diffs are truncated with a changed-file summary
    appended so the LLM still has enough to reason about the change.
    """
    diff = get_backport_diff(target_branch, cwd=cwd)
    lines = diff.splitlines()
    if len(lines) <= _MAX_DIFF_LINES:
        return diff
    changed = get_changed_files(target_branch, cwd=cwd)
    summary = "\n".join(f"  {f}" for f in changed)
    return (
        "\n".join(lines[:_MAX_DIFF_LINES])
        + f"\n# ... (diff truncated at {_MAX_DIFF_LINES} lines)\n"
        + f"# Changed files ({len(changed)}):\n{summary}\n"
    )


def _build_repo_map(*, cwd: Path, changed_files: list[str], files_of_interest: list[str]) -> str:
    """Bounded repo map: changed files + Files of Interest + directory listings."""
    parts: list[str] = []

    parts.append("Files changed by the backport:")
    if changed_files:
        for f in changed_files:
            parts.append(f"  {f}")
    else:
        parts.append("  (none)")
    parts.append("")

    parts.append("Files of Interest (from general-context skill):")
    if files_of_interest:
        for f in files_of_interest:
            parts.append(f"  {f}")
    else:
        parts.append("  (none)")
    parts.append("")

    dirs_to_list: list[Path] = [cwd]
    for f in changed_files:
        parent = (cwd / f).parent
        if parent != cwd and parent.is_dir():
            dirs_to_list.append(parent)

    for d in dirs_to_list:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        rel = d.relative_to(cwd) if d != cwd else Path(".")
        names = [e.name + "/" if e.is_dir() else e.name for e in entries]
        parts.append(f"Listing of {rel}:")
        parts.append("  " + "  ".join(names) if names else "  (empty)")
        parts.append("")

    return "\n".join(parts)


def _files_of_interest(skill_set: SkillSet | None) -> list[str]:
    """Parse the general-context 'Files of Interest' section into paths."""
    if skill_set is None:
        return []
    gc = skill_set.general_context()
    if gc is None:
        return []
    section = gc.get_section(SECTION_FILES_OF_INTEREST)
    if not section:
        return []
    paths: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        # Strip trailing inline comments: "path — description".
        line = re.split(r"\s+(?:—|-{1,2})\s+", line, maxsplit=1)[0].strip()
        if line and not line.startswith("<!--"):
            paths.append(line)
    return paths


def _build_round0_prompt(
    *,
    item: str,
    target_branch: str,
    source_commits: list[str],
    commit_messages: list[tuple[str, str]],
    diff: str,
    skill_context: str,
    repo_map: str,
) -> str:
    """Construct the round-0 (initial) per-item prompt."""
    commits_block = (
        "\n".join(f"- {sha}: {subject}" for sha, subject in commit_messages) or "(none)"
    )

    parts: list[str] = [
        "You are analyzing a backport for a specific concern.",
        "",
        "Checklist item (the concern to reason about):",
        item,
        "",
        f"Target branch: {target_branch}",
        "Source commits and their messages (the backport's intent):",
        commits_block,
        "",
        "Full backport diff (current branch vs. target):",
        "```diff",
        diff,
        "```",
        "",
        "Repo map:",
        repo_map,
    ]

    if skill_context:
        parts.extend(["Relevant skill context:", "```", skill_context, "```", ""])

    parts.extend(_RESPONSE_INSTRUCTIONS)
    return "\n".join(parts)


def _build_followup_prompt(
    *,
    prev_prompt: str,
    supplied: list[tuple[str, str]],
    unavailable: list[str],
) -> str:
    """Append supplied file contents / unavailable notes to the conversation."""
    parts: list[str] = [prev_prompt, ""]
    if supplied:
        parts.append("Requested files (complete contents):")
        for _path, block in supplied:
            parts.append(block)
        parts.append("")
    if unavailable:
        parts.append("The following requested paths were unavailable:")
        for p in unavailable:
            parts.append(f"  {p}")
        parts.append("")
    parts.extend(
        [
            "Continue your analysis with the additional context above, then emit "
            "your final verdict using the structured format. You may request more "
            "files with NEED: (remaining request rounds: see the caps above).",
        ]
    )
    return "\n".join(parts)


def _build_retry_prompt(prev_prompt: str, *, error: str) -> str:
    """Re-prompt after a malformed response or fix failure."""
    return (
        f"{prev_prompt}\n\n"
        f"Your previous response could not be used: {error}\n"
        "Please re-emit your response following the exact structured format."
    )


_RESPONSE_INSTRUCTIONS = [
    "Reason about whether the concern above applies to THIS backport. The",
    "concern is a principle to reason with, not necessarily a file-scoped rule.",
    "You may request unchanged files you need to see with lines of the form:",
    "  NEED: relative/path/to/file.py",
    f"  (max {MAX_NEED_ROUNDS} request rounds; max {MAX_CONTEXT_FILES} files per item).",
    "",
    "When the diff plus any requested files are enough to decide, emit your",
    "final verdict using EXACTLY one of these formats:",
    "",
    "If the concern does not apply:",
    "  APPLIES: no",
    "  REASON: <one line>",
    "",
    "If you cannot decide from the code alone (depends on deployment policy,",
    "rollout intent, etc.):",
    "  APPLIES: uncertain",
    "  QUESTION: <precise question for the human>",
    "",
    "If the concern applies and you can fix it:",
    "  APPLIES: yes",
    "  SEVERITY: critical | important | minor",
    "  FILE: <primary file (informational)>",
    "  DESCRIPTION: <one line — becomes the commit subject>",
    "",
    '  <bpilot-file path="relative/path/to/file.py">',
    "  ...complete corrected file content...",
    "  </bpilot-file>",
    "",
    "Rules:",
    "- NEED: and APPLIES: never appear in the same response.",
    "- <bpilot-file> blocks contain COMPLETE file content, never diffs.",
    "- Only touch files bpilot has shown you (changed files or files it",
    "  supplied in response to NEED: requests).",
    "- Preserve the intent of the backported change; adapt to the target",
    "  branch's structure.",
    "- Output nothing outside the structured fields.",
]


_SYSTEM_PROMPT = (
    "You are an expert at analyzing git backports for semantic gaps. You "
    "analyze a backport for one specific checklist concern at a time. The "
    "concern is a principle to reason with, not necessarily a file-scoped "
    "rule. You may request unchanged target-branch files with NEED: lines "
    f"when the diff is insufficient to decide (bounded: at most {MAX_NEED_ROUNDS} "
    f"request rounds and {MAX_CONTEXT_FILES} files per item). Your final "
    "verdict uses the exact structured format with APPLIES: yes | no | "
    "uncertain. <bpilot-file> blocks contain COMPLETE file content, never "
    "diffs. Fixes preserve the intent of the backported change and adapt to "
    "the target branch's structure. You may only touch files bpilot has "
    "shown you. When the answer depends on information no file can provide "
    "(deployment policy, rollout intent), answer APPLIES: uncertain with a "
    "precise question rather than guessing. Output nothing outside the "
    "structured fields."
)
