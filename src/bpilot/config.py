"""Runtime configuration for bpilot.

Resolution order (highest precedence first):
  1. CLI flags (handled in cli.py, passed in explicitly as overrides).
  2. Environment variables — useful for CI / GitHub Actions.
  3. Snap config (`snap set bpilot <key>=<value>`) — stored under
     $SNAP_DATA/bpilot.conf, readable inside the snap.
  4. Built-in defaults.

The OpenRouter API key never leaves this module: it is read here and
forwarded to llm_client only; it is never included in LLM prompts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "z-ai/glm-5.2"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOKEN_WARNING_THRESHOLD = 8000
DEFAULT_TOKEN_BUDGET_CAP = 50000

# Snap stores keyed config under $SNAP_DATA/bpilot.conf as a flat JSON map.
# Outside the snap (e.g. local dev, CI) $SNAP_DATA is unset and this path is
# unusable, so env vars / CLI flags are the only way to configure bpilot there.
SNAP_CONFIG_PATH = Path(os.environ.get("SNAP_DATA", "")) / "bpilot.conf"


@dataclass
class Config:
    """Resolved runtime configuration.

    Fields are intentionally flat; nested config complicates snap set/get
    and offers no value to the rest of the codebase.
    """

    openrouter_api_key: str | None = None
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    token_warning_threshold: int = DEFAULT_TOKEN_WARNING_THRESHOLD
    token_budget_cap: int = DEFAULT_TOKEN_BUDGET_CAP
    # Where each resolved value came from ("env" | "snap" | "default" | "cli").
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def has_llm(self) -> bool:
        """Whether LLM features are usable (key present)."""
        return bool(self.openrouter_api_key)


def _read_snap_config() -> dict[str, Any]:
    """Read snap-stored config from $SNAP_DATA/bpilot.conf.

    Returns an empty dict when the file is absent or unreadable (e.g.
    running outside the snap). Snap config keys use dashes (snap
    convention); we normalise to underscores for attribute access.
    """
    if not SNAP_CONFIG_PATH.is_file():
        return {}
    try:
        raw = json.loads(SNAP_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {k.replace("-", "_"): v for k, v in raw.items()}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve(
    key: str,
    *,
    cli: str | None,
    env_var: str,
    snap_key: str,
    default: str | None,
    sources: dict[str, str],
) -> str | None:
    """Pick a config value with precedence: cli > env > snap > default.

    Records the chosen source in `sources` under `key` for debugging.
    """
    if cli:
        sources[key] = "cli"
        return cli
    env_val = os.environ.get(env_var)
    if env_val:
        sources[key] = "env"
        return env_val
    snap = _read_snap_config()
    snap_val = snap.get(snap_key)
    if snap_val:
        sources[key] = "snap"
        return str(snap_val)
    sources[key] = "default"
    return default


def load_config(
    *,
    model_override: str | None = None,
    api_key_override: str | None = None,
) -> Config:
    """Resolve the effective configuration.

    Optional overrides come from CLI flags and win over everything else.
    """
    sources: dict[str, str] = {}

    api_key = _resolve(
        "openrouter_api_key",
        cli=api_key_override,
        env_var="OPENROUTER_API_KEY",
        snap_key="openrouter_api_key",
        default=None,
        sources=sources,
    )
    model = _resolve(
        "model",
        cli=model_override,
        env_var="BPILOT_MODEL",
        snap_key="model",
        default=DEFAULT_MODEL,
        sources=sources,
    )
    max_tokens_raw = _resolve(
        "max_tokens",
        cli=None,
        env_var="BPILOT_MAX_TOKENS",
        snap_key="max_tokens",
        default=str(DEFAULT_MAX_TOKENS),
        sources=sources,
    )
    max_tokens = _coerce_int(max_tokens_raw, DEFAULT_MAX_TOKENS)

    return Config(
        openrouter_api_key=api_key,
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=DEFAULT_TEMPERATURE,
        token_warning_threshold=DEFAULT_TOKEN_WARNING_THRESHOLD,
        token_budget_cap=DEFAULT_TOKEN_BUDGET_CAP,
        sources=sources,
    )
