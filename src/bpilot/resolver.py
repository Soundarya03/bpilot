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

from bpilot.git_ops import GitError, get_commit_diff, get_conflict_info, get_file_content
from bpilot.llm_client import LLMClient
from bpilot.skill_loader import SkillSet
from bpilot.validator import validate_file

MAX_RETRIES = 3

# Skill name holding conflict-resolution rules + skip-file patterns.
_CONFLICT_SKILL = "conflict-resolution"


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
    skill_set: SkillSet | None,
    cwd: Path,
) -> ResolutionResult:
    """Resolve all conflicts for a single cherry-pick via the LLM.

    Each file is resolved independently. On success, the resolved file is
    staged so `git cherry-pick --continue` can complete the pick.

    If any file cannot be resolved after MAX_RETRIES, the cherry-pick is
    left in its conflicted state for manual intervention — the caller
    (CLI) decides whether to abort or hand off.

    Skill context comes from the `conflict-resolution` + `general-context`
    skills (loaded by name from `skill_set`). Skip-file patterns come
    from the `conflict-resolution` skill's "Skip Files" section.
    """
    result = ResolutionResult(commit=commit)

    skill_context = _build_skill_context(skill_set)
    skip_patterns: list[str] = []
    if skill_set is not None:
        conflict_skill = skill_set.get(_CONFLICT_SKILL)
        if conflict_skill is not None:
            skip_patterns = conflict_skill.skip_files

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
            commit=commit,
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
    commit: str,
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

    The prompt carries the incoming commit's per-file diff so the LLM can
    tell which side of the markers is intentional change vs. target-side
    divergence. After validation passes, a deterministic guard checks
    that lines the incoming commit ADDED survived the resolution; a miss
    is fed back as an error unless the LLM already repeated the same
    omission once (treated as a deliberate choice, accepted with a
    stderr note).
    """
    conflict_info = get_conflict_info(file_path, cwd=cwd)

    # Fetch the target branch's pre-cherry-pick version for context.
    try:
        target_content = get_file_content(file_path, cwd=cwd, ref=target_branch)
    except GitError:
        target_content = "(file does not exist on target branch)"

    # The incoming commit's per-file diff — grounds both the prompt and
    # the dropped-additions guard. Degrade to "" when uncomputable
    # (e.g. test doubles with fake SHAs); the guard then no-ops.
    try:
        incoming_diff = get_commit_diff(commit, file_path, cwd=cwd)
    except GitError:
        incoming_diff = ""
    incoming_additions = _diff_additions(incoming_diff)

    last_error = ""
    warned_missing: tuple[str, ...] | None = None

    for attempt_num in range(1, MAX_RETRIES + 1):
        prompt = _build_prompt(
            file_path=file_path,
            conflict_content=conflict_info.content,
            target_content=target_content,
            target_branch=target_branch,
            commit_message=commit_message,
            incoming_diff=incoming_diff,
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

        # Dropped-additions guard: every line the incoming commit added
        # should survive the resolution. Missing lines are fed back once;
        # an identical omission on the next attempt is treated as
        # deliberate (bounded loop guarantee) and accepted with a note.
        missing = _missing_additions(incoming_additions, resolved_content)
        if missing:
            missing_key = tuple(missing)
            if warned_missing != missing_key:
                warned_missing = missing_key
                last_error = (
                    "your resolution dropped lines the incoming commit adds:\n"
                    + "\n".join(f"  {m}" for m in missing)
                    + "\nInclude them unless they truly conflict with the "
                    "target branch's own lines."
                )
                continue
            print(
                f"    note: {file_path} resolution omits incoming lines "
                f"(LLM confirmed after feedback): {missing}",
                file=sys.stderr,
            )

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


def _diff_additions(diff: str) -> list[str]:
    """Extract the added lines of a unified diff (stripped, non-empty).

    Powers the deterministic guard that keeps the LLM from silently
    dropping changes the incoming commit introduces.
    """
    additions: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if stripped:
                additions.append(stripped)
    return additions


def _missing_additions(additions: list[str], resolved_content: str) -> list[str]:
    """Return the incoming additions absent (as substrings) from the resolution."""
    return [a for a in additions if a not in resolved_content]


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
    "markers removed. When the two sides of a conflict differ, you MERGE "
    "them: keep the target branch's own lines AND the incoming commit's "
    "changes, and never silently drop either side. You preserve the "
    "original commit's intent while adapting to the target branch's "
    "structure. "
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
    incoming_diff: str = "",
    skill_context: str = "",
    previous_error: str = "",
    attempt: int = 1,
) -> str:
    """Construct the LLM prompt for a single conflict resolution attempt.

    `incoming_diff` is the commit's per-file diff vs. its first parent —
    the clearest possible statement of what the incoming commit changed,
    so the LLM can tell intentional incoming change from target-side
    divergence and doesn't silently drop one side.

    If `previous_error` is non-empty, it's included so the LLM can
    correct its previous failed attempt.
    """
    parts: list[str] = [
        "You are resolving a git merge conflict during a backport.",
        "",
        f"Target branch: {target_branch}",
        f"Original commit message: {commit_message}",
    ]

    if incoming_diff:
        parts.extend(
            [
                "",
                "The incoming commit's change to this file (diff vs. its parent):",
                "```diff",
                incoming_diff,
                "```",
            ]
        )

    parts.extend(
        [
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
    )

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
            "conflict markers (<<<<<<<, =======, >>>>>>>). MERGE the two "
            "sides: keep the target branch's own lines AND the incoming "
            "commit's changes — never silently drop either side. Preserve "
            "the intent of the original commit while adapting to the "
            "target branch's structure.",
            "",
            "Output ONLY the file content. No explanations, no markdown fences, no diff markers.",
        ]
    )

    return "\n".join(parts)


def _build_skill_context(skill_set: SkillSet | None) -> str:
    """Extract the skill context relevant to conflict resolution.

    Concatenates the `conflict-resolution` and `general-context` skill
    bodies (skipping any that are empty or absent). Returns "" if no
    usable skill context is available — the resolver still works, just
    with less grounding context, and the LLM prompt omits the
    skill-context block entirely.
    """
    if skill_set is None:
        return ""
    return skill_set.context_for(_CONFLICT_SKILL)


def _should_skip(file_path: str, patterns: list[str]) -> bool:
    """True when `file_path` matches any glob pattern in `patterns`."""
    return any(fnmatch.fnmatch(file_path, pat) for pat in patterns)


def _resolve_by_checkout(file_path: str, *, target_branch: str, cwd: Path) -> None:
    """Resolve a conflict by taking the target branch's version.

    Used for files in the SKILL.md "Skip Files" section (e.g. lock files)
    that should be regenerated by bpilot's validator rather than merged by
    the LLM.

    NOTE: during a cherry-pick conflict, `--ours` is the branch being
    cherry-picked ONTO (HEAD, i.e. the target branch) and `--theirs` is
    the commit being picked (the source change) — the reverse of merge
    intuition. Keeping the target version therefore requires `--ours`.
    """
    import subprocess

    subprocess.run(
        ["git", "checkout", "--ours", file_path],
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
