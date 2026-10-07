"""bpilot — intelligent backport helper.

bpilot automates the mechanical parts of backporting changes between git
branches (fetch, branch, cherry-pick) and provides an LLM-assisted layer
for conflict resolution and semantic gap analysis. Only the git-ops layer
ever mutates the repository; the LLM is a pure text-in/text-out function.

Public modules:
- cli: command-line entry point (`bpilot port`, `bpilot finalize`).
- config: runtime configuration (snap config + env var fallback).
- git_ops: the only component that runs git.
- llm_client: thin OpenRouter client with cost tracking.
- session: snapshot persistence for the finalize feedback loop.
- report: BACKPORT_REPORT.md generation.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
