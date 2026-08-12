"""Conflict resolver — LLM-assisted resolution of cherry-pick conflicts.

Invoked only when `git_ops.cherry_pick()` reports conflicts. For each
conflicted file, the resolver:

1. Gathers context: the conflicted file's content (with markers), the
   target branch's pre-cherry-pick version, the original commit message,
   and relevant SKILL.md sections.
2. Asks the LLM to produce the **complete resolved file content** (not a
   diff — LLMs are unreliable at producing valid unified diffs).
3. Writes the resolved content to the working tree.
4. Runs the static validator (conflict markers, syntax, placeholders).
5. If validation fails, feeds the error back to the LLM and retries
   (max 3 attempts).
6. On success, stages the resolved file so `git cherry-pick --continue`
   can proceed.

Trust boundary: the LLM never runs git. It returns file content; we
write it via normal file I/O and validate before staging. The LLM never
sees credentials and never executes anything.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.git_ops import GitError, get_conflict_info, get_file_content
from bpilot.llm_client import LLMClient
from bpilot.skill_loader import SkillFile
from bpilot.validator import validate_file

MAX_RETRIES = 3

# SKILL.md sections most relevant to conflict resolution.
_RELEVANT_SECTIONS = [
    "Branch Conventions",
    "Known Divergences Between Branches",
    "Lifecycle Hooks",
]


@dataclass
class ResolutionAttempt:
    """Record of one LLM attempt to resolve a single conflicted file."""

    file_path: str
    attempt: int
    succeeded: bool
    error: str = ""


@dataclass
class ResolutionResult:
    """Outcome of resolving all conflicts for a single cherry-pick."""

    commit: str
    resolved_files: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    attempts: list[ResolutionAttempt] = field(default_factory=list)

    @property
    def all_resolved(self) -> bool:
        """True when every conflicted file was successfully resolved."""
        return not self.failed_files


def resolve_conflicts(
    *,
    commit: str,
    conflicted_files: list[str],
    target_branch: str,
    commit_message: str,
    llm: LLMClient,
    skill: SkillFile | None,
    cwd: Path,
) -> ResolutionResult:
    """Resolve all conflicts for a single cherry-pick via the LLM.

    Each file is resolved independently. On success, the resolved file is
    staged so `git cherry-pick --continue` can complete the pick.

    If any file cannot be resolved after MAX_RETRIES, the cherry-pick is
    left in its conflicted state for manual intervention — the caller
    (CLI) decides whether to abort or hand off.
    """
    result = ResolutionResult(commit=commit)

    skill_context = _build_skill_context(skill)
    skip_patterns = skill.skip_files if skill else []

    for file_path in conflicted_files:
        if _should_skip(file_path, skip_patterns):
            print(f"  skipping {file_path} (in SKILL.md Skip Files) — taking target version")
            _resolve_by_checkout(file_path, target_branch=target_branch, cwd=cwd)
            result.resolved_files.append(file_path)
            result.attempts.append(
                ResolutionAttempt(
                    file_path=file_path,
                    attempt=0,
                    succeeded=True,
                    error="skipped via SKILL.md",
                )
            )
            continue

        print(f"  resolving {file_path} ...")
        attempt_result = _resolve_single_file(
            file_path=file_path,
            target_branch=target_branch,
            commit_message=commit_message,
            llm=llm,
            skill_context=skill_context,
            cwd=cwd,
        )
        result.attempts.append(attempt_result)
        if attempt_result.succeeded:
            result.resolved_files.append(file_path)
            print(f"    resolved on attempt {attempt_result.attempt}")
        else:
            result.failed_files.append(file_path)
            print(
                f"    failed after {attempt_result.attempt} attempt(s): {attempt_result.error}",
                file=sys.stderr,
            )

    return result


def _resolve_single_file(
    *,
    file_path: str,
    target_branch: str,
    commit_message: str,
    llm: LLMClient,
    skill_context: str,
    cwd: Path,
) -> ResolutionAttempt:
    """Resolve one conflicted file, with up to MAX_RETRIES LLM attempts.

    On each attempt:
      1. Build the prompt from current file state + context.
      2. Ask the LLM for the complete resolved file content.
      3. Write it to the working tree and validate (markers, syntax, etc.).
      4. On failure, feed the error back for the next attempt.
    """
    conflict_info = get_conflict_info(file_path, cwd=cwd)

    # Fetch the target branch's pre-cherry-pick version for context.
    try:
        target_content = get_file_content(file_path, cwd=cwd, ref=target_branch)
    except GitError:
        target_content = "(file does not exist on target branch)"

    last_error = ""

    for attempt_num in range(1, MAX_RETRIES + 1):
        prompt = _build_prompt(
            file_path=file_path,
            conflict_content=conflict_info.content,
            target_content=target_content,
            target_branch=target_branch,
            commit_message=commit_message,
            skill_context=skill_context,
            previous_error=last_error,
            attempt=attempt_num,
        )

        try:
            response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
        except Exception as err:
            last_error = f"LLM call failed: {err}"
            continue

        resolved_content = _extract_content(response.text)
        if not resolved_content:
            last_error = "LLM returned an empty response"
            continue

        # Write the resolved content to the working tree.
        file_path_obj = cwd / file_path
        try:
            file_path_obj.write_text(resolved_content)
        except OSError as err:
            last_error = f"could not write file: {err}"
            continue

        # Static validation (conflict markers, syntax, placeholders).
        validation = validate_file(file_path, cwd=cwd)
        if not validation.ok:
            last_error = "; ".join(validation.errors)
            continue

        # Success: stage the resolved file.
        try:
            _stage_file(file_path, cwd=cwd)
        except GitError as err:
            last_error = f"could not stage resolved file: {err}"
            continue

        return ResolutionAttempt(
            file_path=file_path,
            attempt=attempt_num,
            succeeded=True,
        )

    return ResolutionAttempt(
        file_path=file_path,
        attempt=MAX_RETRIES,
        succeeded=False,
        error=last_error,
    )


def _stage_file(file_path: str, *, cwd: Path) -> None:
    """Stage a resolved file so cherry-pick --continue can proceed."""
    import subprocess

    subprocess.run(
        ["git", "add", file_path],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


_SYSTEM_PROMPT = (
    "You are an expert at resolving git merge conflicts during backports. "
    "You produce the complete resolved file content, with all conflict "
    "markers removed. You preserve the original commit's intent while "
    "adapting to the target branch's structure. "
    "You output ONLY the file content, with no explanations or markdown fences."
)


def _extract_content(raw: str) -> str:
    """Extract file content from an LLM response.

    LLMs sometimes wrap output in markdown fences (```python ... ```) or
    add preamble text. We strip the fences and return the content.
    """
    text = raw
    # Strip markdown code fences if present.
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = text.splitlines()
        # Remove the opening fence (```python, ```yaml, or just ```).
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        # Remove the closing fence if present.
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
        # Preserve trailing newline if the original had one.
        if raw.rstrip("\n") != raw and not text.endswith("\n"):
            text += "\n"
    return text


def _build_prompt(
    *,
    file_path: str,
    conflict_content: str,
    target_content: str,
    target_branch: str,
    commit_message: str,
    skill_context: str,
    previous_error: str,
    attempt: int,
) -> str:
    """Construct the LLM prompt for a single conflict resolution attempt.

    If `previous_error` is non-empty, it's included so the LLM can
    correct its previous failed attempt.
    """
    parts: list[str] = [
        "You are resolving a git merge conflict during a backport.",
        "",
        f"Target branch: {target_branch}",
        f"Original commit message: {commit_message}",
        "",
        f"File with conflict: {file_path}",
        "",
        "Conflict content (working tree, with markers):",
        "```",
        conflict_content,
        "```",
        "",
        "Target branch's version of this file (before cherry-pick):",
        "```",
        target_content,
        "```",
    ]

    if skill_context:
        parts.extend(
            [
                "",
                "Relevant SKILL.md context:",
                "```",
                skill_context,
                "```",
            ]
        )

    if previous_error:
        parts.extend(
            [
                "",
                f"Your previous attempt (#{attempt - 1}) failed with:",
                f"  {previous_error}",
                "Please correct the issue and try again.",
            ]
        )

    parts.extend(
        [
            "",
            "Produce the COMPLETE resolved file content. Remove all "
            "conflict markers (<<<<<<<, =======, >>>>>>>). Preserve the "
            "intent of the original commit while adapting to the target "
            "branch's structure.",
            "",
            "Output ONLY the file content. No explanations, no markdown fences, no diff markers.",
        ]
    )

    return "\n".join(parts)


def _build_skill_context(skill: SkillFile | None) -> str:
    """Extract the SKILL.md sections relevant to conflict resolution.

    Returns an empty string if no skill file is available — the resolver
    still works, just with less grounding context.
    """
    if skill is None:
        return ""
    return skill.get_sections(_RELEVANT_SECTIONS)


def _should_skip(file_path: str, patterns: list[str]) -> bool:
    """True when `file_path` matches any glob pattern in `patterns`."""
    return any(fnmatch.fnmatch(file_path, pat) for pat in patterns)


def _resolve_by_checkout(file_path: str, *, target_branch: str, cwd: Path) -> None:
    """Resolve a conflict by taking the target branch's version.

    Used for files in the SKILL.md "Skip Files" section (e.g. lock files)
    that should be regenerated by the user rather than merged by the LLM.
    """
    import subprocess

    subprocess.run(
        ["git", "checkout", "--theirs", file_path],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", file_path],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
