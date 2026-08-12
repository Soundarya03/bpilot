"""Tests for bpilot.config — config resolution order.

Verifies the precedence: CLI overrides > env vars > snap config > defaults.
Snap config is unreachable in the test environment (no $SNAP_DATA), so we
focus on CLI/env/default interactions which are the dev/CI paths.
"""

from __future__ import annotations

import pytest

from bpilot.config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    load_config,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip bpilot env vars so tests don't leak host configuration."""
    for var in ("OPENROUTER_API_KEY", "BPILOT_MODEL", "BPILOT_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_configured(clean_env):
    cfg = load_config()
    assert cfg.openrouter_api_key is None
    assert cfg.model == DEFAULT_MODEL
    assert cfg.max_tokens == DEFAULT_MAX_TOKENS
    assert cfg.has_llm is False
    assert cfg.sources["openrouter_api_key"] == "default"
    assert cfg.sources["model"] == "default"


def test_env_var_provides_api_key(clean_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cfg = load_config()
    assert cfg.openrouter_api_key == "sk-or-test"
    assert cfg.has_llm is True
    assert cfg.sources["openrouter_api_key"] == "env"


def test_cli_override_beats_env(clean_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    monkeypatch.setenv("BPILOT_MODEL", "env-model")
    cfg = load_config(api_key_override="sk-or-cli", model_override="cli-model")
    assert cfg.openrouter_api_key == "sk-or-cli"
    assert cfg.model == "cli-model"
    assert cfg.sources["openrouter_api_key"] == "cli"
    assert cfg.sources["model"] == "cli"


def test_max_tokens_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("BPILOT_MAX_TOKENS", "4096")
    cfg = load_config()
    assert cfg.max_tokens == 4096
    assert cfg.sources["max_tokens"] == "env"


def test_max_tokens_falls_back_on_garbage(clean_env, monkeypatch):
    monkeypatch.setenv("BPILOT_MAX_TOKENS", "not-a-number")
    cfg = load_config()
    assert cfg.max_tokens == DEFAULT_MAX_TOKENS


def test_model_override_takes_precedence_over_default(clean_env):
    cfg = load_config(model_override="openrouter/anthropic/claude-3.5-sonnet")
    assert cfg.model == "openrouter/anthropic/claude-3.5-sonnet"
    assert cfg.sources["model"] == "cli"
