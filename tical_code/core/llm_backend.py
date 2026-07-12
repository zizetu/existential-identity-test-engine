# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""
EITE LLM Backend - Evaluation Model Calling
=============================================

Unified LLM calling interface for evaluation. Supports calling models
for evaluation runs with structured output parsing, retry logic, and
result formatting.

EITE evaluation context:
- Evaluates models by sending test prompts and collecting responses
- Supports structured output parsing for scoring
- Tracks token usage per evaluation run
- Handles API errors gracefully during batch evaluation
- No circuit breaker (evaluation runs are short-lived and independent)
- No fallback model switching (evaluation uses a fixed model config)

Architecture:
    LLMBackend - Abstract base class with call() interface.
    OpenAIBackend - Primary implementation for OpenAI-compatible APIs.
    create_llm_backend() - Factory function for backend instantiation.

Author: EITE Team
"""
import json
import logging
import os
import ssl
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eite-agent.llm")


class LLMBackend:
    """Abstract base class for LLM calling backends.

    Defines the call() interface used by the evaluation runner.
    Subclasses implement _do_call() for provider-specific API logic.
    The base call() wraps _do_call() with empty response detection
    and generic error handling.
    """

    def call(
        self,
        messages: list,
        tools: list = None,
        max_tokens: int = 4000,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call the LLM with conversation messages.

        Args:
            messages: List of message dicts in OpenAI chat format.
            tools: Optional list of OpenAI function-calling tool schemas.
            max_tokens: Maximum tokens in the response (default 4000).

        Returns:
            Dict with keys:
                content: The model's text response (may be empty string).
                tool_calls: List of tool call dicts.
                error: Error description if the call failed.
                usage: Dict with prompt_tokens and completion_tokens.
        """
        try:
            result = self._do_call(messages, tools, max_tokens)
            content = result.get("content", "")
            if not content and not result.get("tool_calls"):
                logger.error("LLM returned empty response")
                return {"error": "empty_response", "content": "", "usage": {}}
            return result
        except TimeoutError as e:
            logger.error("LLM call timed out: %s", e)
            return {"error": f"timeout: {e}", "content": "", "usage": {}}
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return {"error": str(e), "content": "", "usage": {}}

    def _do_call(self, messages, tools, max_tokens):
        """Actual LLM API call - must be implemented by subclasses.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool schemas.
            max_tokens: Maximum output tokens.

        Returns:
            Dict with 'content', 'tool_calls', and 'usage' keys.

        Raises:
            NotImplementedError: Always in the base class.
        """
        raise NotImplementedError


class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend for evaluation model calling.

    Simplified for evaluation use:
    - Retry with exponential backoff (up to 3 attempts)
    - No circuit breaker (evaluation calls are independent)
    - Temperature control via LLM_TEMPERATURE env var (default 0.0 for deterministic eval)
    - Token usage tracking for evaluation statistics

    Attributes:
        _api_key: API key for authentication.
        _base_url: Base URL for the chat completions endpoint.
        _model: Active model name.
        _temperature: Sampling temperature.
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = float(os.environ.get("LLM_TEMPERATURE", "0.0"))
        logger.info(
            "EITE LLM backend: model=%s url=%s temperature=%.1f",
            model, base_url, self._temperature,
        )

    def get_model(self) -> str:
        """Return the currently active model name."""
        return self._model

    def set_model(self, model: str) -> None:
        """Set the model name for subsequent calls."""
        old = self._model
        self._model = model
        logger.info("EITE model changed: %s -> %s", old, model)

    def _do_call(self, messages, tools, max_tokens):
        """Execute an LLM API call with retry logic.

        Retries up to 3 times on network errors with exponential backoff.
        Parses response into content, tool_calls, and usage fields.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool schemas.
            max_tokens: Maximum output tokens.

        Returns:
            Dict with content (str), tool_calls (list), usage (dict).
        """
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = tools

        # Auth header
        auth_header = f"Bearer {self._api_key}"
        auth_key = "Authorization"

        # Determine if this is a MiMo-style endpoint
        is_mimo = "mimo" in self._base_url.lower()
        if is_mimo:
            auth_header = self._api_key
            auth_key = "api-key"

        # Retry loop
        RETRIABLE = (
            urllib.error.URLError, TimeoutError, ConnectionError,
            ConnectionResetError, ConnectionRefusedError, OSError,
        )

        for attempt in range(3):
            _t_start = time.time()
            req = urllib.request.Request(
                f"{self._base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={auth_key: auth_header, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=30, context=ssl.create_default_context()
                ) as resp:
                    data = json.loads(resp.read())
                    break  # Success
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:200]
                logger.error("EITE LLM HTTP %d: %s", e.code, detail)
                if e.code >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return {"content": f"[LLM error: HTTP {e.code}]", "tool_calls": [], "usage": {}}
            except RETRIABLE as e:
                if attempt == 2:
                    raise
                logger.warning("EITE LLM retry %d/3: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        else:
            logger.error("EITE LLM unreachable after 3 retries")
            return {"content": "[LLM unreachable after 3 retries]", "tool_calls": [], "usage": {}}

        # Parse response
        msg = data.get("choices", [{}])[0].get("message", {})
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "args": args,
            })

        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "") or ""
        if not content and reasoning:
            content = reasoning

        # Parse usage
        usage = data.get("usage", {})
        token_usage = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": token_usage,
            "reasoning_content": reasoning,
        }


def create_llm_backend(
    backend: str = "auto",
    model: str = "",
    api_key: str = "",
    base_url: str = "",
) -> LLMBackend:
    """Factory: create an LLM backend for evaluation.

    Resolves credentials from environment variables first, then prompts
    the user if none are found.

    Args:
        backend: Backend type ('auto' only - always creates OpenAIBackend).
        model: Model name (empty to auto-detect from env).
        api_key: API key (empty to auto-detect).
        base_url: Base URL (empty to auto-detect).

    Returns:
        An initialized OpenAIBackend instance.

    Raises:
        ValueError: If no credentials are found.
    """
    env_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    env_base = (
        os.environ.get("OPENAI_BASE_URL", "")
        or os.environ.get("DEEPSEEK_BASE_URL", "")
    )
    env_model = (
        os.environ.get("LLM_EVAL_MODEL", "")
        or os.environ.get("OPENAI_MODEL", "")
        or os.environ.get("DEEPSEEK_MODEL", "")
    )

    if env_key and not api_key:
        api_key = env_key
    if env_base and not base_url:
        base_url = env_base
    if env_model and not model:
        model = env_model

    if not api_key or not base_url:
        raise ValueError(
            "No LLM credentials found. Set OPENAI_API_KEY / DEEPSEEK_API_KEY "
            "and OPENAI_BASE_URL / DEEPSEEK_BASE_URL environment variables."
        )

    if not model:
        model = ""  # model will be selected by provider config or environment

    return OpenAIBackend(api_key=api_key, base_url=base_url, model=model)
