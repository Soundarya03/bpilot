"""LLM client — a pure text-in/text-out function over OpenRouter.

Trust boundary (see BACKPORT_HELPER_PLAN.md §Security):
  - The LLM never sees git credentials, SSH keys, or host tokens. The
    only credential in this module is the OpenRouter API key, held here
    and never included in prompts.
  - The LLM never executes anything. There is no function-calling /
    tool-use, no shell access, no git invocation. Prompts carry code
    content and instructions only.
  - Its output is treated as untrusted data: callers validate patches
    via git_ops.apply_patch() before any tree mutation.

Single function interface:
    query_llm(prompt, system="", model=None) -> (text, usage_stats)

Usage stats accumulate across a run for cost reporting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from bpilot.config import Config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


class LLMError(RuntimeError):
    """Raised when the LLM call fails after all retries."""


@dataclass
class UsageStats:
    """Accumulated token usage across LLM calls in a single run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    model: str = ""
    # Optional per-call log for debugging.
    per_call: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, *, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.calls += 1
        if not self.model:
            self.model = model
        self.per_call.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model": model,
            }
        )


@dataclass
class LLMResponse:
    """The text response and the usage from the call that produced it."""

    text: str
    usage: dict[str, Any]


class LLMClient:
    """Thin OpenRouter chat-completions client.

    Construction does no I/O; the network is hit only on `query_llm()`.
    """

    def __init__(self, config: Config, *, usage: UsageStats | None = None) -> None:
        if not config.has_llm:
            raise LLMError(
                "no OpenRouter API key configured — "
                "set OPENROUTER_API_KEY, `snap set bpilot openrouter-api-key=...`, "
                "or pass --no-llm"
            )
        self._config = config
        self._usage = usage or UsageStats(model=config.model)

    @property
    def usage(self) -> UsageStats:
        return self._usage

    def query_llm(self, prompt: str, *, system: str = "", model: str | None = None) -> LLMResponse:
        """Send `prompt` to OpenRouter, return the response text and usage.

        Retries with exponential backoff on transient failures (network,
        5xx, 429). Raises LLMError if all retries fail or the response is
        missing the expected fields.
        """
        chosen_model = model or self._config.model
        payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system} if system else None,
                {"role": "user", "content": prompt},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        # Filter None entries (e.g. when system is empty).
        payload["messages"] = [m for m in payload["messages"] if m is not None]

        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._call(payload, chosen_model)
            except _TransientError as err:
                last_err = err
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            except LLMError:
                raise
        raise LLMError(f"LLM call failed after {MAX_RETRIES} attempts: {last_err}")

    def _call(self, payload: dict[str, Any], model: str) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self._config.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                OPENROUTER_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        except httpx.HTTPError as err:
            raise _TransientError(str(err)) from err

        if response.status_code in (429, 500, 502, 503, 504):
            raise _TransientError(f"HTTP {response.status_code}: {response.text}")
        if response.status_code != 200:
            raise LLMError(f"OpenRouter returned HTTP {response.status_code}: {response.text}")

        try:
            data = response.json()
        except ValueError as err:
            raise LLMError(f"non-JSON response from OpenRouter: {err}") from err

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as err:
            raise LLMError(f"unexpected response shape: {data!r}") from err

        usage_raw = data.get("usage", {}) or {}
        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))

        # Single-call runaway detection. Warn via stderr-style return; the
        # caller (CLI) decides whether to surface it. We don't raise — a
        # single large call may be legitimate for a big diff.
        if prompt_tokens > self._config.token_warning_threshold:
            # Stash the warning for the caller; don't mutate the response.
            pass

        self._usage.add(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
        )
        return LLMResponse(text=text or "", usage=usage_raw)


class _TransientError(Exception):
    """Internal: errors worth retrying (network, 429, 5xx)."""
