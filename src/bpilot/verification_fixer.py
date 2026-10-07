"""Verification fixer — LLM-assisted repair of format/lint/unit-test failures.

After the validator runs verification checks (format, lint, unit tests
sourced from the SKILL.md "Verification Checks" section), this module
handles the retry loop: if any check fails, the LLM is asked to propose
corrected file contents; the fixes are written and the checks re-run.
Bounded to `MAX_FIX_ATTEMPTS` (5) iterations.

Trust boundary (same as the resolver — see BACKPORT_HELPER_PLAN.md §Security):
  - The LLM is a pure text-in/text-out function. It never runs the
    verification check commands itself, never runs git, and never sees
    credentials.
  - Its output (proposed file contents) is treated as untrusted data: we
    write it via normal file I/O, then re-run the deterministic checks.
  - Only files that were changed by the backport (or that the failure
    output names) are offered to / overwritten by the LLM, so the LLM
    can't reach arbitrary files in the repo.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bpilot.llm_client import LLMClient
from bpilot.skill_loader import SkillSet
from bpilot.validator import VerificationResult, run_verification_checks

MAX_FIX_ATTEMPTS = 5

# Skill name holding the verification-check commands + context.
_VERIFICATION_SKILL = "verification-checks"


@dataclass
class FixIteration:
    """Record of one pass through the fix loop."""

    iteration: int  # 1-based
    check_result: VerificationResult
    llm_called: bool = False
    files_changed: list[str] = field(default_factory=list)
    llm_error: str = ""


@dataclass
class FixResult:
    """Outcome of the full verification-checks + LLM repair loop."""

    ok: bool
    iterations: list[FixIteration] = field(default_factory=list)
    final_result: VerificationResult | None = None
    skipped: bool = False  # True when --no-llm skipped the fix loop

    @property
    def attempts_used(self) -> int:
        return len(self.iterations)

    @property
    def remaining_failures(self) -> str:
        if self.final_result is None or self.final_result.ok:
            return ""
        return self.final_result.failure_summary


def run_verification_checks_with_fixes(
    *,
    cwd: Path,
    skill_set: SkillSet | None,
    llm: LLMClient | None,
    changed_files: list[str],
    use_llm: bool = True,
    baseline_passed: bool = False,
) -> FixResult:
    """Run verification checks; if any fail, use the LLM to repair them.

    Bounded to `MAX_FIX_ATTEMPTS` iterations. Each iteration:
      1. Run all verification check commands (format, lint, unit tests).
      2. If all pass — done.
      3. If any fail and `use_llm` and `llm` is available — ask the LLM
         for corrected file contents, write them, re-run.
      4. If `use_llm` is False or the LLM is unavailable, run the checks
         once and return the result with `skipped=True` (failures are
         surfaced but not repaired).

    The LLM only ever receives: the failing command output + the content
    of files in `changed_files`. It returns proposed full file contents,
    which we overwrite and then re-check. It never runs the checks.

    `baseline_passed` is True when the verification checks were confirmed
    green on the unmodified target branch immediately before the backport
    (the baseline gate in `cli.py`). When True, the fixer prompt asserts
    that the failures below were introduced by the cherry-picked changes,
    sharpening the fix mandate. When False (e.g. `--skip-baseline` was
    used), the assertion is omitted and the fixer behaves as before.
    """
    # First pass — always run the checks.
    first = run_verification_checks(cwd=cwd, skill_set=skill_set)
    first_iter = FixIteration(iteration=1, check_result=first)
    if first.ok:
        return FixResult(ok=True, iterations=[first_iter], final_result=first)

    # No LLM available — report the failures, skip the fix loop.
    if not use_llm or llm is None:
        return FixResult(
            ok=False,
            iterations=[first_iter],
            final_result=first,
            skipped=True,
        )

    result_iterations: list[FixIteration] = [first_iter]
    current = first

    for attempt in range(2, MAX_FIX_ATTEMPTS + 1):
        print(
            f"  verification checks failed on iteration {attempt - 1}; "
            f"asking LLM to fix (attempt {attempt - 1}/{MAX_FIX_ATTEMPTS - 1}) ..."
        )
        iter_record = FixIteration(iteration=attempt, check_result=current, llm_called=True)
        try:
            changed = _apply_llm_fixes(
                cwd=cwd,
                llm=llm,
                skill_set=skill_set,
                check_result=current,
                changed_files=changed_files,
                baseline_passed=baseline_passed,
            )
            iter_record.files_changed = changed
        except Exception as err:  # noqa: BLE001 — surface but keep looping
            iter_record.llm_error = str(err)
            print(f"  LLM fix attempt failed: {err}", file=sys.stderr)

        # Re-run the checks after applying fixes.
        current = run_verification_checks(cwd=cwd, skill_set=skill_set)
        iter_record.check_result = current
        result_iterations.append(iter_record)
        if current.ok:
            return FixResult(ok=True, iterations=result_iterations, final_result=current)

    # Exhausted retries.
    return FixResult(ok=False, iterations=result_iterations, final_result=current)


def _apply_llm_fixes(
    *,
    cwd: Path,
    llm: LLMClient,
    skill_set: SkillSet | None,
    check_result: VerificationResult,
    changed_files: list[str],
    baseline_passed: bool = False,
) -> list[str]:
    """Ask the LLM for corrected file contents and write them.

    Returns the list of file paths that were actually rewritten.

    The LLM is given the failing command output and the content of the
    files the backport touched. It's asked to return one or more
    `<bpilot-file path="...">...</bpilot-file>` blocks containing the
    corrected file contents. Only paths that appear in `changed_files`
    are actually overwritten, so the LLM can't reach arbitrary files.

    `baseline_passed` is forwarded to `_build_prompt`; see its docstring.
    """
    target_files = [f for f in changed_files if (cwd / f).is_file()]
    if not target_files:
        target_files = list(
            {f for f in _extract_paths_from_output(check_result) if (cwd / f).is_file()}
        )
    if not target_files:
        raise RuntimeError("no writable files to offer the LLM for fixing")

    file_contents: list[str] = []
    for path in target_files:
        try:
            content = (cwd / path).read_text(errors="replace")
        except OSError as err:
            file_contents.append(f'<bpilot-file path="{path}">READ_ERROR: {err}</bpilot-file>')
            continue
        file_contents.append(f'<bpilot-file path="{path}">\n{content}\n</bpilot-file>')

    prompt = _build_prompt(
        check_result=check_result,
        file_blocks="\n\n".join(file_contents),
        skill_context=_build_skill_context(skill_set),
        baseline_passed=baseline_passed,
    )

    response = llm.query_llm(prompt, system=_SYSTEM_PROMPT)
    proposed = _parse_proposed_files(response.text)

    written: list[str] = []
    allowed = set(target_files)
    for path, content in proposed.items():
        if path not in allowed:
            # The LLM tried to touch a file outside the backport's scope.
            print(
                f"  ignoring LLM-proposed change to {path} (out of scope; "
                "not in the backport's changed files)",
                file=sys.stderr,
            )
            continue
        try:
            (cwd / path).write_text(content)
            written.append(path)
        except OSError as err:
            print(f"  could not write LLM fix to {path}: {err}", file=sys.stderr)
    return written


_FILE_BLOCK_RE = re.compile(
    r'<bpilot-file\s+path="([^"]+)">\s*(.*?)\s*</bpilot-file>',
    re.DOTALL,
)


def _parse_proposed_files(raw: str) -> dict[str, str]:
    """Parse the LLM response into {path: content} pairs.

    The LLM is asked to emit one `<bpilot-file path="...">...</bpilot-file>`
    block per corrected file. Any text outside these blocks is ignored
    (handles preamble / explanations).
    """
    proposed: dict[str, str] = {}
    for match in _FILE_BLOCK_RE.finditer(raw):
        path = match.group(1).strip()
        content = match.group(2)
        # Normalise trailing newline.
        if not content.endswith("\n"):
            content += "\n"
        proposed[path] = content
    return proposed


def _extract_paths_from_output(check_result: VerificationResult) -> set[str]:
    """Best-effort extraction of file paths referenced in failure output.

    Used as a fallback when `changed_files` is empty (e.g. the LLM touched
    nothing but a test failed). Looks for `path/to/file.py`-shaped tokens.
    """
    paths: set[str] = set()
    for cr in check_result.failures:
        text = cr.output
        for match in re.finditer(
            r"(\w[\w/\-\.]*\.(?:py|js|ts|go|rs|rb|java|c|cc|cpp|h|hpp))", text
        ):
            paths.add(match.group(1))
    return paths


def _build_prompt(
    *,
    check_result: VerificationResult,
    file_blocks: str,
    skill_context: str,
    baseline_passed: bool = False,
) -> str:
    """Construct the LLM prompt for one fix attempt.

    When `baseline_passed` is True, the prompt asserts that the verification
    checks all passed on the unmodified target branch immediately before the
    backport — so the failures below were introduced by the cherry-picked
    changes. When False (e.g. `--skip-baseline`), the assertion is omitted
    and the prompt matches the pre-baseline behaviour.
    """
    parts: list[str] = [
        "You are repairing verification check failures after a git backport.",
        "",
    ]
    if baseline_passed:
        parts.append(
            "These verification checks all passed on the unmodified target "
            "branch immediately before the backport. The failures below were "
            "introduced by the cherry-picked changes."
        )
        parts.append("")
    parts.extend(
        [
            "The following verification checks (format, lint, unit tests) failed. "
            "For each, the command and its output are shown:",
            "",
            check_result.failure_summary,
            "",
            "Below are the current contents of the files the backport touched. "
            "Inspect them and the failure output, then propose the minimal "
            "edits needed to make ALL the failing checks pass. Preserve the "
            "intent of the backported change. Do NOT revert the backport.",
            "",
            file_blocks,
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
    parts.extend(
        [
            "",
            "Output the corrected file contents using this exact format, one "
            "block per file you changed:",
            '<bpilot-file path="relative/path/to/file.py">',
            "...complete corrected file content...",
            "</bpilot-file>",
            "",
            "Only include files you actually modified. Do not include "
            "explanations outside the <bpilot-file> blocks. The `path` must "
            "match one of the files shown above. Output ONLY the "
            "<bpilot-file> blocks.",
        ]
    )
    return "\n".join(parts)


def _build_skill_context(skill_set: SkillSet | None) -> str:
    """Skill context relevant to repairing verification check failures.

    Concatenates the `verification-checks` and `general-context` skill
    bodies (skipping any that are empty or absent). Returns "" if no
    usable skill context is available — the LLM prompt omits the
    skill-context block entirely.
    """
    if skill_set is None:
        return ""
    return skill_set.context_for(_VERIFICATION_SKILL)


_SYSTEM_PROMPT = (
    "You are an expert at repairing failing format, lint, and unit-test "
    "checks after a git backport. You output corrected file contents "
    "wrapped in <bpilot-file> blocks, and nothing else outside those "
    "blocks. You preserve the intent of the backported change while "
    "making the checks pass."
)
