# tical-code -- AI Agent Platform
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

# provenance:ticalasi-zzt-2026​
"""Model Failover - session-affinity LRU selection with circuit-breaker health states.

Usage:
    from model_failover import ModelFailover

    fa = ModelFailover(providers=[
        {"name": "mimo-1", "model": "mimo-v2.5-pro", "endpoint": "...", "key": "..."},
        {"name": "mimo-2", "model": "mimo-v2.5-pro", "endpoint": "...", "key": "..."},
        {"name": "gpt-oss", "model": "openai/gpt-oss-120b:free", "endpoint": "...", "key": "..."},
        {"name": "deepseek", "model": "deepseek-v4-flash", "endpoint": "...", "key": "...", "is_fallback": True},
    ])

    result = fa.call(messages, tools=tools, preferred_family="mimo")

Strategy:
  1. If preferred_family is set, only pick from providers of that family first.
  2. LRU selection - pick least-recently-used provider from available pool.
  3. Enforce MIN_INTERVAL (2s) between calls to the same provider.
  4. Health state machine per provider:
       HEALTHY  → normal operation
       COOLED_DOWN → after consecutive failures, with exponential backoff
       HALF_OPEN → cooldown expired, allow exactly one probe request
       Probe succeeds → HEALTHY. Probe fails → COOLED_DOWN (doubled backoff).
  5. Error categorization:
       400 → permanent error, do NOT retry (bad request)
       401 → auth error, long cooldown (600s)
       429 → rate limit, exponential cooldown (60s base, doubling)
       5xx → server error, medium cooldown (120s)
       timeout → same as server error
       empty response → treated as server error
  6. Retry with jittered backoff (1-3s base) on recoverable errors.
  7. If ALL same-family providers are rate-limited → expand to all families.
  8. Fallback providers (is_fallback=True) are only used when all primaries exhausted.
"""

import asyncio
import copy
import ipaddress
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from tical_code.core.ortools import (
    RouterTrace,
    enrich_headers,
    enrich_body,
    extract_metadata,
    detect_cache_hit,
)
# Conditional: orthos_chain is a full-system feature, may not be present in light installs
try:
    from tical_code.core.orthos_chain import chain_call
except ImportError:
    async def chain_call(*args, **kwargs):
        """Stub: orthos_chain not available in this build."""
        return {"status": "unavailable", "reason": "orthos_chain not installed"}



logger = logging.getLogger("tical-code.failover")

# --- Helper: load failover config from providers.json ---

_PROVIDERS_JSON_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "providers.json"),
    os.path.join(os.getcwd(), "config", "providers.json"),
    os.path.expanduser("~/tical-code/config/providers.json"),
]


def _load_failover_config() -> dict:
    """Read the 'failover' block from providers.json, falling back to empty dict."""
    for path_str in _PROVIDERS_JSON_CANDIDATES:
        resolved = os.path.abspath(path_str)
        if os.path.exists(resolved):
            try:
                with open(resolved) as f:
                    cfg = json.load(f)
                failover = cfg.get("failover", {})
                if failover:
                    logger.info(
                        "[Failover] Loaded circuit-breaker config from %s",
                        resolved,
                    )
                    return failover
            except (json.JSONDecodeError, OSError) as e:
                logger.info("[Failover] Failed to read %s: %s", resolved, e)
    return {}


# Apply failover config at module load time
_FAILOVER_CFG = _load_failover_config()

# --- Constants ---

class HealthState:
    """Circuit-breaker health states for LLM provider failover.

    Models a state machine per provider:
      - HEALTHY: Normal operation. Provider accepts requests normally.
      - COOLED_DOWN: Consecutive failures triggered exponential backoff.
        Provider is excluded from selection until cooldown_until expires.
      - HALF_OPEN: Cooldown expired. Exactly one probe request is allowed.
        Probe success → HEALTHY (with reset). Probe failure → COOLED_DOWN
        (backoff doubled). This prevents thundering-herd recovery while
        allowing fast detection of restored health.
    """
    HEALTHY = "healthy"
    COOLED_DOWN = "cooled_down"
    HALF_OPEN = "half_open"

# Exponential backoff configuration (seconds)
# Values are read from providers.json["failover"] at module load, with
# hardcoded fallbacks when the config file is unavailable or incomplete.
COOLDOWN_BASE_429 = _FAILOVER_CFG.get("cooldown_base_429", 60)       # rate limit: start at 60s
COOLDOWN_BASE_5XX = _FAILOVER_CFG.get("cooldown_base_5xx", 120)      # server error: start at 120s
COOLDOWN_AUTH = _FAILOVER_CFG.get("cooldown_auth", 600)              # auth error: fixed 600s
COOLDOWN_TIMEOUT = _FAILOVER_CFG.get("cooldown_timeout", 30)         # timeout: fast cooldown
COOLDOWN_CAP = _FAILOVER_CFG.get("cooldown_cap", 300)                # maximum cooldown
MIN_INTERVAL_SECONDS = _FAILOVER_CFG.get("min_interval_seconds", 2.0)  # min gap between calls
RETRY_JITTER_MIN = _FAILOVER_CFG.get("retry_jitter_min", 0.5)        # min retry delay
RETRY_JITTER_MAX = _FAILOVER_CFG.get("retry_jitter_max", 2.0)        # max retry delay
PER_CALL_TIMEOUT = _FAILOVER_CFG.get("per_call_timeout", 60)         # per-provider HTTP timeout
GLOBAL_CALL_TIMEOUT = _FAILOVER_CFG.get("global_call_timeout", 300)  # overall wall-clock limit

# Fast circuit breaker - prevents repeated failover cycles when all providers
# are unhealthy. After CIRCUIT_BREAKER_THRESHOLD consecutive call() failures,
# enter fast-fail mode for CIRCUIT_BREAKER_COOLDOWN seconds, returning
# immediately without attempting any provider calls.
CIRCUIT_BREAKER_THRESHOLD = 5   # consecutive failures before tripping
CIRCUIT_BREAKER_COOLDOWN = 60   # seconds to stay in fast-fail mode

# Max consecutive failures before a provider is marked permanently dead.
# After this many consecutive failures (including probe failures), the provider
# enters a long cooldown (DEAD_COOLDOWN) instead of looping forever with
# exponential backoff. This prevents the death spiral where:
#   cooldown expires -> probe fails -> backoff doubles -> repeat forever
MAX_CONSECUTIVE_FAILURES = 10   # max failures before permanent dead state
DEAD_COOLDOWN = 1800            # 30 min dead time after max failures exceeded

# Family dead-time: after a provider family fails, block all providers in that
# family for this many seconds. Prevents rapid ping-pong where:
#   DeepSeek fails -> try MiMo -> MiMo fails -> try DeepSeek again -> ...
# without giving any provider time to recover.
FAMILY_DEAD_TIME = _FAILOVER_CFG.get("family_dead_time", 30)

# IPv4 reserved ranges for SSRF checks (not covered by ipaddress built-ins)
_IPV4_CGNAT = ipaddress.IPv4Network('100.64.0.0/10')
_IPV4_BENCHMARK = ipaddress.IPv4Network('198.18.0.0/15')
_IPV4_RESERVED = ipaddress.IPv4Network('240.0.0.0/4')
_IPV4_CURRENT_NET = ipaddress.IPv4Network('0.0.0.0/8')


# --- Data Classes ---

@dataclass
class Provider:
    """A single LLM provider with health tracking for circuit-breaker failover.

    Each provider holds connection configuration and runtime health state
    used by ModelFailover for provider selection, cooldown management, and
    exponential backoff.

    Attributes:
        name: Unique provider identifier (e.g., 'mimo-1', 'deepseek').
        model: Model name sent to the API (e.g., 'mimo-v2.5-pro').
        endpoint: API endpoint URL for chat completions.
        key: API authentication key.
        auth_style: Authentication style: 'bearer', 'api-key', or 'mimo-cli-subprocess'.
        is_fallback: If True, only used when all primary providers are exhausted.
        priority: Selection order (lower = higher priority). 0=free-channel, 10=normal, 20=fallback.
        timeout: Per-provider HTTP timeout in seconds. 0 uses PER_CALL_TIMEOUT default.
        cooldown_until: Unix timestamp when this provider becomes eligible again.
        consecutive_failures: Count of sequential failures (reset on success).
        fail_count: Total lifetime failure count (never reset).
        last_used: Unix timestamp of most recent successful or attempted call.
        state: Current health state (HealthState.HEALTHY / COOLED_DOWN / HALF_OPEN).
    """
    name: str
    model: str
    endpoint: str
    key: str
    auth_style: str = "bearer"  # "bearer" | "api-key" | "mimo-cli-subprocess"
    is_fallback: bool = False
    priority: int = 10          # lower = higher priority. 0=free-channel, 10=normal, 20=fallback
    timeout: int = 0        # 0 = use PER_CALL_TIMEOUT default
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    fail_count: int = 0
    last_used: float = 0.0
    state: str = HealthState.HEALTHY

@dataclass
class FailoverResult:
    """Result from a model call with provider metadata and router trace info.

    Returned by ModelFailover.call() and ModelFailover._call_single(). Provides
    dict-style access via get() for backward compatibility with callers that
    expect dictionary returns from older API wrappers.

    Attributes:
        content: The model's text response.
        tool_calls: Parsed tool call list (name/args at top level), or None.
        reasoning_content: Raw reasoning/thinking output, if the model emits it.
        provider_name: Name of the provider that served this response.
        model: Model identifier used for this call.
        retries: Number of retries before this successful result.
        provider_family: Family classification ('mimo', 'openrouter', 'deepseek').
        router_trace: OpenRouter metadata (model used, latency, tokens, etc.).
        cache_hit: Whether the response was served from OpenRouter cache.
    """

    content: str
    tool_calls: Optional[Any] = None
    reasoning_content: Optional[str] = None
    provider_name: str = ""
    model: str = ""
    retries: int = 0
    provider_family: str = ""
    router_trace: Optional[RouterTrace] = None  # OpenRouter metadata
    cache_hit: bool = False  # whether response was served from cache

    def get(self, key: str, default=None):
        """Dict-style attribute access for compatibility with older callers.

        Args:
            key: Attribute name to retrieve (e.g., 'content', 'tool_calls').
            default: Value to return if the attribute does not exist.

        Returns:
            The attribute value, or default if the key is not an attribute
            of this dataclass.
        """
        return getattr(self, key, default)


# --- Main Class ---


class ModelFailover:
    """Session-affinity LLM client with circuit-breaker health states.

    Incorporates OpenRouter-inspired patterns:
      - Half-open circuit breaker for exponential backoff
      - Error categorization (permanent vs transient)
      - Health state machine per provider
    """

    def __init__(self, providers: List[Dict]):
        self.providers: List[Provider] = [
            Provider(
                name=p["name"],
                model=p["model"],
                endpoint=p["endpoint"],
                key=p["key"],
                auth_style=p.get("auth_style", "bearer"),
                is_fallback=p.get("is_fallback", False),
                priority=p.get("priority", 10),
                timeout=p.get("timeout", 0),
            )
            for p in providers
        ]
        if not self.providers:
            raise ValueError("Need at least one provider")
        self._last_healthy: dict[str, str] = {}  # family → provider_name fast-path cache
        # Fast circuit breaker state - tracks consecutive global failures
        # across ALL providers to prevent repeated failover cycles.
        self._global_consecutive_failures: int = 0
        self._circuit_broken_until: float = 0.0
        self._family_dead_until: dict[str, float] = {}  # family -> timestamp when it becomes eligible again
        self._lock = threading.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    def __del__(self):
        """Cleanup aiohttp session on garbage collection."""
        if self._session is not None and not self._session.closed:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                loop.run_until_complete(self._session.close())
            except (RuntimeError, Exception):
                pass
            self._session = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp client session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=PER_CALL_TIMEOUT),
            )
        return self._session

    async def close(self):
        """Close the aiohttp client session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Family detection
    # ------------------------------------------------------------------

    @staticmethod
    def _get_family(provider: Provider) -> str:
        """Determine model family from provider name/model/endpoint."""
        if "mimo" in provider.name.lower() or "mimo" in provider.model.lower():
            return "mimo"
        if ("openrouter" in provider.endpoint.lower() or
                "gpt-oss" in provider.name.lower()):
            return "openrouter"
        return "deepseek"

    # ------------------------------------------------------------------
    # Health state machine
    # ------------------------------------------------------------------

    @staticmethod
    def _error_category(status_code: int) -> str:
        """Classify HTTP error as recoverable or permanent.

        Returns:
            'rate_limit'  - 429, recoverable with backoff
            'auth'        - 401, recoverable but needs long cooldown
            'server'      - 5xx / timeout / empty response, recoverable
            'permanent'   - 400, do NOT retry (bad request)
        """
        if status_code == 429:
            return "rate_limit"
        if status_code == 401:
            return "auth"
        if status_code in (400, 403, 404):
            return "permanent"
        if 500 <= status_code < 600:
            return "server"
        return "server"  # default: treat unknown as server error

    def _transition_health(self, provider: Provider, category: str):
        """Advance provider health state based on error category.

        Rate-limit and server errors use exponential backoff.
        Auth errors use fixed long cooldown.
        Permanent errors (400) do NOT transition - caller should abort.
        """
        try:
            now = time.time()

            if category == "permanent":
                return

            with self._lock:
                provider.fail_count += 1
                provider.consecutive_failures += 1

                # Check if max consecutive failures exceeded -> permanent dead state
                if provider.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    cooldown = DEAD_COOLDOWN
                    logger.warning(
                        f"[Failover] {provider.name} → DEAD "
                        f"(exceeded {MAX_CONSECUTIVE_FAILURES} consecutive failures, "
                        f"dead for {DEAD_COOLDOWN}s)"
                    )
                elif category == "auth":
                    family = self._get_family(provider)
                    if family:
                        self._family_dead_until[family] = time.time() + FAMILY_DEAD_TIME
                    cooldown = COOLDOWN_AUTH
                elif category == "rate_limit":
                    cooldown = min(
                        COOLDOWN_BASE_429 * (2 ** (provider.consecutive_failures - 1)),
                        COOLDOWN_CAP,
                    )
                elif category == "timeout":
                    cooldown = COOLDOWN_TIMEOUT
                else:  # server error
                    cooldown = min(
                        COOLDOWN_BASE_5XX * (2 ** (provider.consecutive_failures - 1)),
                        COOLDOWN_CAP,
                    )

                provider.cooldown_until = now + cooldown
                provider.state = HealthState.COOLED_DOWN

                logger.info(
                    f"[Failover] {provider.name} \u2192 {provider.state} "
                    f"(category={category}, backoff={cooldown}s, "
                    f"consecutive_failures={provider.consecutive_failures}, "
                    f"total_fails={provider.fail_count})"
                )
        except Exception as e:
            logger.error("[Failover] _transition_health crashed for %s: %s", provider.name, e)

    def _probe_transition(self, provider: Provider, success: bool):
        """Handle half-open probe result.

        Called after a single request is made to a HALF_OPEN provider.
        """
        with self._lock:
            if success:
                provider.state = HealthState.HEALTHY
                provider.consecutive_failures = 0
                provider.cooldown_until = 0.0
                logger.info(
                    f"[Failover] {provider.name} → HEALTHY "
                    f"(probe succeeded, reset failures)"
                )
            else:
                # Probe failed - transition back to COOLED_DOWN with doubled backoff.
                provider.consecutive_failures += 1
                provider.fail_count += 1
                # Check if max consecutive failures exceeded -> permanent dead state
                if provider.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    backoff = DEAD_COOLDOWN
                    logger.warning(
                        f"[Failover] {provider.name} → DEAD "
                        f"(probe exceeded {MAX_CONSECUTIVE_FAILURES} consecutive failures, "
                        f"dead for {DEAD_COOLDOWN}s)"
                    )
                else:
                    backoff = min(
                    COOLDOWN_BASE_5XX * (2 ** (provider.consecutive_failures - 1)),
                    COOLDOWN_CAP,
                )
                provider.cooldown_until = time.time() + backoff
                provider.state = HealthState.COOLED_DOWN
                logger.info(
                    f"[Failover] {provider.name} → COOLED_DOWN "
                    f"(probe failed, backoff={backoff}s, "
                    f"consecutive_failures={provider.consecutive_failures})"
                )

    def _mark_success(self, provider: Provider):
        """Reset health after a successful call."""
        with self._lock:
            if provider.state != HealthState.HEALTHY:
                logger.info(
                    f"[Failover] {provider.name} → HEALTHY (call succeeded)"
                )
            provider.state = HealthState.HEALTHY
            provider.consecutive_failures = 0
            provider.cooldown_until = 0.0
            # Reset global circuit breaker - a successful call means at least
            # one provider is healthy, so clear the fast-fail state.
            self._global_consecutive_failures = 0
            self._circuit_broken_until = 0.0
            # Update last-healthy cache for fast-path on next call.
            # Include ALL families including mimo so fast-path can use
            # whichever provider succeeded most recently, avoiding the
            # stale DeepSeek-only bias that caused repeated fallback cycles.
            family = self._get_family(provider)
            self._last_healthy[family] = provider.name

    # ------------------------------------------------------------------
    # Provider selection
    # ------------------------------------------------------------------

    def _available(self, include_fallback: bool = True,
                   family: Optional[str] = None,
                   bypass_min_interval: bool = False) -> List[Provider]:
        """Return available providers, sorted by LRU.

        Filtering rules:
          - Providers in COOLED_DOWN state are excluded (cooldown has not expired).
          - Providers in HALF_OPEN state are included if cooldown has expired.
          - Fallback providers (is_fallback=True) are excluded unless
            include_fallback is True.
          - Family filter: only include providers matching the given family.
          - Minimum interval: exclude providers used too recently.
        """
        now = time.time()
        pool = []
        for p in self.providers:
            if p.is_fallback and not include_fallback:
                continue
            # Fast-skip: COOLED_DOWN with >5s remaining - skip all further checks.
            if p.state == HealthState.COOLED_DOWN and p.cooldown_until > now + 5.0:
                continue
            if family and self._get_family(p) != family:
                continue
            # Family dead-time check: if this family is globally blocked, skip all its providers.
            p_family = self._get_family(p)
            if p_family in self._family_dead_until and now < self._family_dead_until[p_family]:
                continue
            if not bypass_min_interval and p.last_used > 0 and (now - p.last_used) < MIN_INTERVAL_SECONDS:
                # Fallback providers (is_fallback=True) are exempt from MIN_INTERVAL.
                if not p.is_fallback:
                    n_non_fallback = sum(1 for pp in self.providers if not pp.is_fallback)
                    if n_non_fallback <= 1:
                        pass
                    else:
                        continue
            if p.state == HealthState.COOLED_DOWN and now < p.cooldown_until:
                continue
            elif p.state == HealthState.HALF_OPEN:
                # HALF_OPEN is eligible - it means cooldown expired,
                # but we allow exactly one probe request.
                pass
            pool.append(p)
        # Sort by priority (lower = earlier), then least-recently-used to spread load.
        pool.sort(key=lambda p: (p.priority, p.last_used))
        return pool

    # ------------------------------------------------------------------
    # Single provider call
    # ------------------------------------------------------------------

    async def _call_single(self, provider: Provider, messages: List[Dict],
                     tools: Optional[List] = None, max_tokens: int = 2000,
                     temperature: float = 0.3) -> FailoverResult:
        """Make one API call to a single provider with provider-specific handling.

        Handles provider-specific quirks:
          - MiMo: api-key header, enable_thinking=False
          - DeepSeek: thinking=disabled, image filtering, tool-call patching,
            orphan tool message stripping
          - OpenRouter: standard Bearer auth, router metadata enrichment,
            context-compression plugin
          - MiMo CLI: subprocess-based CLI invocation (bypasses 403 on Token Plan)

        Note: These auth flows are dispatched automatically based on provider.auth_style.
        messages (cross-provider compatibility), strips orphan tool messages
        (immediate-predecessor check), and patches missing tool responses for
        DeepSeek to prevent 400 errors.

        Args:
            provider: The Provider to call.
            messages: Chat message list (role/content dicts).
            tools: Optional tool definitions for function calling.
            max_tokens: Maximum tokens to generate (default 2000).
            temperature: Sampling temperature (default 0.3, ignored by MiMo).

        Returns:
            A FailoverResult with content, tool_calls, provider metadata, and
            router trace information on success.

        Raises:
            PermanentError: for 400 (bad request) - caller must NOT retry.
            RecoverableError: for 401/403/429/5xx/timeout - caller may retry
                with a different provider.
        """
        body: Dict[str, Any] = {
            "model": provider.model,
            "messages": copy.deepcopy(messages),
            "max_tokens": max_tokens,
        }

        # Auto-switch to vision model (mimo-v2.5) when images are detected.
        # mimo-v2.5-pro (token-plan) does NOT support multimodal.
        _has_images = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages if isinstance(m.get("content"), list)
        )
        if _has_images and "mimo" in provider.name.lower():
            body["model"] = "mimo-v2.5"
            logger.info("Vision detected - auto-switched to mimo-v2.5")

        is_mimo = "mimo" in provider.name.lower() or "mimo" in provider.endpoint.lower()
        is_deepseek = ("deepseek" in provider.name.lower() or
                       "deepseek" in provider.endpoint.lower())
        is_openrouter = ("openrouter" in provider.endpoint.lower() or
                         "gpt-oss" in provider.name.lower())

        # Temperature: MiMo ignores it, others use it.
        if not is_mimo:
            body["temperature"] = temperature

        if tools:
            body["tools"] = tools

        # MiMo: disable thinking to conserve TPM.
        if is_mimo:
            body["enable_thinking"] = False

        # Strip reasoning_content across all providers.
        # When mixing MiMo (which emits reasoning_content) with other providers,
        # some require that if ANY assistant message has it, ALL must.
        # Stripping universally is the safest default.
        clean_messages = [
            {k: v for k, v in m.items() if k != "reasoning_content"}
            for m in body.get("messages", [])
        ]
        body["messages"] = clean_messages

        # DeepSeek: disable thinking mode explicitly.
        if is_deepseek:
            body["thinking"] = {"type": "disabled"}
            # DeepSeek v4 requires `type` field on every message object,
            # not just on content parts. Add it from the message role.
            fixed_ds = []
            for m in body.get("messages", []):
                msg_type = m.get("role", "assistant")
                content = m.get("content")
                if isinstance(content, list):
                    typed = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if "type" not in part:
                            part["type"] = "text"
                        typed.append(part)
                    fixed_ds.append({**m, "type": msg_type, "content": typed})
                elif content == "" or content is None:
                    fixed_ds.append({**m, "type": msg_type, "content": None if m.get("tool_calls") else ""})
                else:
                    fixed_ds.append({**m, "type": msg_type})
            body["messages"] = fixed_ds
            # Convert internal tool_calls (name/args) to OpenAI format
            # (function/name/arguments) for DeepSeek v4 which requires
            # the standard function-calling format.
            for mi, m in enumerate(body["messages"]):
                tcs = m.get("tool_calls")
                if tcs and any("function" not in tc for tc in tcs):
                    formatted = []
                    for tc in tcs:
                        fn = tc.get("function", {})
                        args_val = tc.get("args", fn.get("arguments", {}))
                        if isinstance(args_val, dict):
                            args_val = json.dumps(args_val)
                        formatted.append({
                            "id": tc.get("id", f"call_{mi}"),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", fn.get("name", "")),
                                "arguments": args_val,
                            }
                        })
                    body["messages"][mi] = {**m, "tool_calls": formatted}

        # MiMo: also needs OpenAI function-calling format (name/args → function/name/arguments)
        if is_mimo:
            for mi, m in enumerate(body["messages"]): 
                tcs = m.get("tool_calls")
                if tcs and any("function" not in tc for tc in tcs):
                    formatted = []
                    for tc in tcs:
                        fn = tc.get("function", {})
                        args_val = tc.get("args", fn.get("arguments", {}))
                        if isinstance(args_val, dict):
                            args_val = json.dumps(args_val)
                        formatted.append({
                            "id": tc.get("id", f"call_{mi}"),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", fn.get("name", "")),
                                "arguments": args_val,
                            }
                        })
                    body["messages"][mi] = {**m, "tool_calls": formatted}

        # Strip image_url parts for non-vision models only.
        # mimo-v2.5 (vision) keeps images; other models block them.
        if body.get("model") != "mimo-v2.5":
            filtered = []
            for m in body.get("messages", []):
                content = m.get("content", "")
                if isinstance(content, list):
                    text_parts = [p for p in content if p.get("type") == "text"]
                    if text_parts:
                        filtered.append({**m, "content": text_parts})
                else:
                    filtered.append(m)
            body["messages"] = filtered



        # DeepSeek: strip orphan tool_calls to prevent 400 errors.
        # DeepSeek requires every tool_call to be followed by a tool response.
        # Instead of injecting dummy tool responses (which pollute context),
        # remove any tool_calls from assistant messages that don't have
        # a matching tool response in the immediately following messages.
        if is_deepseek:
            msgs = body.get("messages", [])
            fixed = []
            for i, m in enumerate(msgs):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    tc_ids = {tc.get("id") for tc in m["tool_calls"]}
                    next_tool_ids = set()
                    for j in range(i + 1, len(msgs)):
                        if msgs[j].get("role") == "tool":
                            next_tool_ids.add(msgs[j].get("tool_call_id"))
                        else:
                            break
                    missing = tc_ids - next_tool_ids
                    if missing:
                        kept_tcs = [tc for tc in m["tool_calls"] if tc.get("id") not in missing]
                        if kept_tcs:
                            fixed.append({**m, "tool_calls": kept_tcs})
                            logger.info(
                                f"[Failover] Stripped {len(missing)} orphan tool_calls\n"
                            )
                        # If ALL tool_calls are orphaned, skip the assistant message entirely
                        # (no point sending an assistant msg with empty tool_calls)
                        continue
                    fixed.append(m)
                else:
                    fixed.append(m)
            body["messages"] = fixed

        # Pre-validation: strip orphaned tool messages (tool role without
        # a matching tool_call in the IMMEDIATELY PRECEDING assistant message).
        # DeepSeek (and OpenAI) require tool messages to directly follow
        # the assistant message that issued the matching tool_call.
        # v0.7.40: tightened from global-set check to immediate-predecessor check.
        msgs = body.get("messages", [])
        if msgs:
            clean = []
            stripped = 0
            active_tc_ids = set()  # tool_call_ids from the most recent assistant
            for m in msgs:
                role = m.get("role", "")
                if role == "assistant":
                    # New assistant message resets active tool_call_ids
                    active_tc_ids = set()
                    if m.get("tool_calls"):
                        for tc in m["tool_calls"]:
                            if tc.get("id"):
                                active_tc_ids.add(tc["id"])
                    clean.append(m)
                elif role == "tool":
                    tc_id = m.get("tool_call_id", "")
                    if tc_id and tc_id not in active_tc_ids:
                        stripped += 1
                        continue
                    clean.append(m)
                else:
                    # user/system - reset active set (tool messages can't follow these)
                    if role in ("user", "system"):
                        active_tc_ids = set()
                    clean.append(m)
            if stripped:
                logger.info(
                    f"[Failover] Stripped {stripped} orphan tool messages "
                    f"(immediate-predecessor check, total_msgs={len(msgs)})"
                )
            body["messages"] = clean

        # DeepSeek v4 FINAL CHECK: ensure every message has `type` field.
        # This runs AFTER all message transformations (orphan stripping, etc.)
        # to guarantee no message is missing `type` regardless of how it was
        # modified by earlier processing steps.
        if is_deepseek:
            _ds_fixed = 0
            for mi, m in enumerate(body.get("messages", [])):
                if "type" not in m:
                    _ds_fixed += 1
                    body["messages"][mi] = {**m, "type": m.get("role", "assistant")}
            if _ds_fixed:
                logger.info(
                    f"[Failover] DeepSeek final type-fix: added `type` to {_ds_fixed} messages"
                )

        # Build request - auth handling depends on provider style.
        data = json.dumps(body).encode("utf-8")
        
        if provider.auth_style == "mimo-cli-subprocess":
            # MiMo CLI subprocess: call official mimo binary (bypasses 403)
            try:
                from mimo_cli_wrapper import MimoCliProvider
                cli = MimoCliProvider(model=provider.model)
                return cli.call_for_failover(
                    messages=messages, tools=tools,
                    max_tokens=max_tokens, temperature=temperature,
                )
            except Exception as e:
                logger.error(f"[Failover] mimo-cli call failed: {e}")
                raise RecoverableError(f"mimo-cli: {e}") from e

        auth_header = (
            {"api-key": provider.key}
            if is_mimo
            else {"Authorization": f"Bearer {provider.key}"}
        )
        headers = {"Content-Type": "application/json", **auth_header}
        provider_endpoint = provider.endpoint
        # Normalize: ensure endpoint ends with /chat/completions
        # Accepts both https://api.example.com/v1 and https://api.example.com/v1/chat/completions
        _endpoint_norm = provider_endpoint.rstrip("/")
        if not _endpoint_norm.endswith("/chat/completions"):
            provider_endpoint = _endpoint_norm + "/chat/completions"
        else:
            provider_endpoint = _endpoint_norm
        or_headers = enrich_headers(provider.name, provider.endpoint)
        headers.update(or_headers)
        # Orthos Chain: route local Ollama providers through chain pipeline
        if "11434" in provider_endpoint or "localhost" in provider_endpoint:
            logger.debug("[ModelFailover] Local Ollama provider %s - routing through Orthos Chain", provider.name)
            chain_result = chain_call(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                deepseek_key="",  # Force local-only mode - orthos chain
                                   # is a local model router, not an API
                                   # fallback. API access is handled by
                                   # ModelFailover's own provider chain.
            )
            return FailoverResult(
                content=chain_result.get("content", ""),
                tool_calls=chain_result.get("tool_calls", []),
                provider_name=provider.name,
                model=chain_result.get("model", "orthos-chain"),
                cache_hit=chain_result.get("cached", False),
                router_trace=TraceRecorder(),
            )



        # OpenRouter: enrich body with context-compression plugin.
        or_body = enrich_body(
            messages=body.get("messages", []),
            tools=body.get("tools"),
            provider_name=provider.name,
            provider_endpoint=provider.endpoint,
            enable_compression=True,
        )
        if or_body:
            # Merge plugins into body - use request-level plugin enabling.
            # Context compression is a server-side plugin, no client logic needed.
            if "plugins" in or_body:
                body["plugins"] = or_body["plugins"]
            # Merge response_format for structured outputs (if enabled).
            if "response_format" in or_body:
                body["response_format"] = or_body["response_format"]
            data = json.dumps(body).encode("utf-8")

        # DEBUG: log last assistant tool_calls format for DeepSeek 400 diagnosis
        if is_deepseek:
            msgs = body.get("messages", [])
            msg2 = msgs[2] if len(msgs) > 2 else None
            logger.warning(
                f"[Failover] DeepSeek msgs[{len(msgs)}]: "
                f"msg[0] role={msgs[0].get('role')} content_type={'list' if isinstance(msgs[0].get('content'), list) else 'str'} "
                f"msg[1] role={msgs[1].get('role') if len(msgs)>1 else '?'} "
                f"msg[2] role={msg2.get('role') if msg2 else '?'} keys={list(msg2.keys()) if msg2 else 'N/A'}"
            )
            msgs = body.get("messages", [])
            last_tc = None
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    last_tc = m["tool_calls"][:1]  # first tool_call only
                    break
            last_tool = None
            for m in reversed(msgs):
                if m.get("role") == "tool":
                    last_tool = m.get("tool_call_id", "")[:30]
                    break
            logger.debug(
                f"[Failover] DeepSeek req: {len(msgs)} msgs, "
                f"last_tc_sample={json.dumps(last_tc)[:200] if last_tc else 'None'}, "
                f"last_tool_id={last_tool}"
            )

        # SSRF check: ensure LLM endpoint doesn't resolve to private IP
        _parsed = urllib.parse.urlparse(provider_endpoint)
        _host = _parsed.hostname
        if _host:
            try:
                # Resolve all addresses (IPv4 + IPv6 via getaddrinfo) and check each
                # Uses ipaddress module for proper classification including IPv6 ULA
                # (fd00::/8), link-local (fe80::/10), multicast, and loopback.
                for _family, _type, _proto, _canon, _sockaddr in socket.getaddrinfo(_host, None):
                    _resolved = _sockaddr[0]
                    try:
                        _ip = ipaddress.ip_address(_resolved)
                    except ValueError:
                        continue
                    # IPv4 private/reserved ranges
                    if isinstance(_ip, ipaddress.IPv4Address):
                        if (_ip.is_private or _ip.is_loopback or
                            _ip.is_link_local or _ip.is_multicast or
                            _ip in _IPV4_CGNAT or _ip in _IPV4_BENCHMARK or
                            _ip in _IPV4_RESERVED or _ip in _IPV4_CURRENT_NET):
                            raise PermanentError(
                                f"SSRF blocked: endpoint {provider_endpoint} resolves to private IP {_resolved}"
                            )
                    # IPv6 private/reserved ranges (ULA fd00::/8, link-local fe80::/10,
                    # loopback ::1, multicast ff00::/8)
                    elif isinstance(_ip, ipaddress.IPv6Address):
                        if (_ip.is_loopback or _ip.is_private or
                            _ip.is_link_local or _ip.is_multicast):
                            raise PermanentError(
                                f"SSRF blocked: endpoint {provider_endpoint} resolves to private IPv6 {_resolved}"
                            )
            except PermanentError:
                raise
            except Exception as e:
                raise PermanentError(f"SSRF check failed for endpoint {provider_endpoint}: {e}")
        _t_start = time.time()
        _t_latency_ms = 0  # default for network-failure path

        try:
            call_timeout = provider.timeout or PER_CALL_TIMEOUT
            session = await self._get_session()
            async with session.post(
                provider_endpoint,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=call_timeout),
                ssl=ssl.create_default_context(),
            ) as resp:
                _t_latency_ms = int((time.time() - _t_start) * 1000)
                if resp.status >= 400:
                    err_body = ""
                    try:
                        err_body = await resp.text()
                    except Exception:
                        pass
                    error_category = self._error_category(resp.status)
                    if error_category == "permanent":
                        # 400 Bad Request - never retry.
                        logger.error(
                            f"[Failover] {provider.name} {resp.status} Bad Request (permanent): "
                            f"{err_body[:500]}"
                        )
                        raise PermanentError(
                            f"{provider.name}: {resp.status} Bad Request - {err_body[:200]}"
                        )
                    # Recoverable: 401, 429, 5xx.
                    self._transition_health(provider, error_category)
                    try:
                        from .usage import get_tracker
                        get_tracker().record_api_call(
                            model=provider.model,
                            endpoint=provider.endpoint,
                            provider_name=provider.name,
                            status=f"error_{resp.status}",
                            latency_ms=_t_latency_ms,
                        )
                    except Exception:
                        pass
                    raise RecoverableError(
                        f"{provider.name} returned {resp.status} ({error_category})"
                    )
                result = await resp.json()
        except PermanentError:
            # 400 Bad Request - must NOT retry, must NOT trigger cooldown.
            # This must come BEFORE aiohttp.ClientError because PermanentError
            # raised inside async-with can be masked by ClientError during
            # response cleanup, causing it to fall through to the wrong handler.
            raise
        except asyncio.TimeoutError:
            # Network timeout: fast-track to next provider - short cooldown.
            self._transition_health(provider, "timeout")
            logger.warning(f"[Failover] {provider.name} timed out ({PER_CALL_TIMEOUT}s)")
            try:
                from .usage import get_tracker
                get_tracker().record_api_call(
                    model=provider.model,
                    endpoint=provider.endpoint,
                    provider_name=provider.name,
                    status="error_timeout",
                    latency_ms=int((time.time() - _t_start) * 1000),
                )
            except Exception:
                pass
            raise RecoverableError(f"{provider.name}: timeout after {call_timeout}s")
        except aiohttp.ClientError as e:
            # Network/connection error - treat as server error with standard backoff.
            self._transition_health(provider, "server")
            logger.error(f"[Failover] {provider.name} call failed: {e}")
            try:
                from .usage import get_tracker
                get_tracker().record_api_call(
                    model=provider.model,
                    endpoint=provider.endpoint,
                    provider_name=provider.name,
                    status="error_network",
                    latency_ms=int((time.time() - _t_start) * 1000),
                )
            except Exception:
                pass
            raise RecoverableError(f"{provider.name}: {e}")
        except Exception as e:
            # Other unexpected errors - treat as server error with standard backoff.
            self._transition_health(provider, "server")
            logger.error(f"[Failover] {provider.name} call failed: {e}")
            try:
                from .usage import get_tracker
                get_tracker().record_api_call(
                    model=provider.model,
                    endpoint=provider.endpoint,
                    provider_name=provider.name,
                    status="error_network",
                    latency_ms=int((time.time() - _t_start) * 1000),
                )
            except Exception:
                pass
            raise RecoverableError(f"{provider.name}: {e}")

        # Parse successful response.
        choice = result["choices"][0]["message"]
        provider.last_used = time.time()
        content = choice.get("content", "") or ""
        if not content and choice.get("reasoning_content"):
            content = choice["reasoning_content"]
        reasoning_content = choice.get("reasoning_content", "")

        # Extract OpenRouter metadata if present.
        trace = extract_metadata(result)
        cached = detect_cache_hit(result)
        if trace:
            logger.info("[Failover] %s %s", provider.name, trace.to_log())
        elif cached:
            logger.info("[Failover] %s cache hit detected", provider.name)

        # Usage tracking: record successful API call.
        try:
            from .usage import get_tracker
            t = get_tracker()
            usage = result.get("usage", {})
            t.record_tokens(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                model=provider.model,
            )
            t.record_api_call(
                model=provider.model,
                endpoint=provider.endpoint,
                provider_name=provider.name,
                latency_ms=_t_latency_ms,
                metadata={"cache_hit": cached},
            )
        except Exception:
            # Never let usage tracking break the main call path.
            pass

        return FailoverResult(
            content=content,
            tool_calls=_normalize_tc(choice.get("tool_calls")),
            reasoning_content=reasoning_content,
            provider_name=provider.name,
            model=provider.model,
            provider_family=self._get_family(provider),
            router_trace=trace,
            cache_hit=cached or (trace.cache_hit() if trace else False),
        )

    # ------------------------------------------------------------------
    # Public API: call
    # ------------------------------------------------------------------

    async def call(self, messages: List[Dict], tools: Optional[List] = None,
             max_tokens: int = 2000, temperature: float = 0.3,
             preferred_family: Optional[str] = None) -> FailoverResult:
        """Call LLM with session-affinity LRU selection and automatic failover.

        Implements a multi-phase retry strategy with circuit-breaker health
        states and exponential backoff:

        Phase 1: Try providers of preferred_family in LRU order (least-recently-used
        first). Providers in COOLED_DOWN state are skipped. HALF_OPEN providers
        get exactly one probe request.

        Phase 1b: If all same-family providers are exhausted, expand to all
        families (excluding fallback providers).

        Phase 2: Try fallback providers (is_fallback=True) - only used when all
        primary providers are exhausted.

        Phase 3: If all providers are exhausted, wait for the nearest cooldown
        expiry and retry. If global timeout (GLOBAL_CALL_TIMEOUT) is reached,
        return an error result.

        Args:
            messages: List of chat message dicts (role/content format).
            tools: Optional list of tool definitions for function calling.
            max_tokens: Maximum tokens in the response (default 2000).
            temperature: Sampling temperature (default 0.3). Ignored by MiMo.
            preferred_family: If set, only pick from providers of this family
                first (e.g., 'mimo', 'openrouter', 'deepseek').

        Returns:
            A FailoverResult with content, tool_calls, provider metadata, and
            retry count. On total failure, content is an error message string.
        """
        retries = 0
        tried: set = set()
        _call_start = time.time()  # wall-clock start for global timeout

        # Fast circuit breaker: if all providers have been failing repeatedly,
        # skip provider selection entirely for CIRCUIT_BREAKER_COOLDOWN seconds.
        # This prevents repeated failover cycles when the entire provider pool
        # is unhealthy (e.g., OpenRouter instability triggering all 7 fallbacks).
        if self._circuit_broken_until > _call_start:
            remaining = int(self._circuit_broken_until - _call_start)
            logger.warning(
                f"[Failover] Fast circuit breaker active - "
                f"{remaining}s remaining. Skipping provider selection."
            )
            return FailoverResult(
                content=(
                    f"All providers in circuit-breaker cooldown "
                    f"({remaining}s remaining). Please retry later."
                ),
                retries=retries,
            )

        # Fast-path: try last healthy provider for this family first (no jitter).
        if preferred_family and preferred_family in self._last_healthy:
            cached_name = self._last_healthy[preferred_family]
            cached_provider = None
            for p in self.providers:
                if p.name == cached_name:
                    cached_provider = p
                    break
            if (cached_provider is not None
                    and cached_provider.state == HealthState.HEALTHY
                    and (cached_provider.last_used == 0
                         or _call_start - cached_provider.last_used >= MIN_INTERVAL_SECONDS)):
                try:
                    result = await self._call_single(
                        cached_provider, messages, tools, max_tokens, temperature
                    )
                    result.retries = retries
                    self._mark_success(cached_provider)
                    logger.info(
                        f"[Failover] Fast-path hit: {cached_provider.name} "
                        f"(family={preferred_family})"
                    )
                    return result
                except PermanentError:
                    raise
                except RecoverableError:
                    retries += 1
                    # Cache miss - invalidate and fall through to normal pool.
                    with self._lock:
                        self._last_healthy.pop(preferred_family, None)
                    logger.info(
                        f"[Failover] Fast-path miss: {cached_provider.name} "
                        f"failed, cache invalidated for family={preferred_family}"
                    )

        # Phase 1: same-family providers.
        family = preferred_family
        while True:
            # Global timeout guard: abort if total call time exceeds limit.
            if time.time() - _call_start > GLOBAL_CALL_TIMEOUT:
                logger.error(
                    f"[Failover] Global timeout ({GLOBAL_CALL_TIMEOUT}s) - "
                    f"aborting after {retries} retries across {len(tried)} providers"
                )
                with self._lock:
                    self._global_consecutive_failures += 1
                    if self._global_consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        self._circuit_broken_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
                        logger.info(
                            f"[Failover] Fast circuit breaker TRIPPED - "
                            f"{self._global_consecutive_failures} consecutive failures, "
                            f"cooling down for {CIRCUIT_BREAKER_COOLDOWN}s"
                        )
                return FailoverResult(
                    content=(
                        f"All providers exhausted or timed out after "
                        f"{GLOBAL_CALL_TIMEOUT}s. Tried: {', '.join(sorted(tried))}. "
                        f"Please retry later."
                    ),
                    retries=retries,
                )

            pool = [
                p for p in self._available(include_fallback=True, family=family)
                if p.name not in tried
            ]
            if not pool:
                break
            pick = pool[0]  # LRU - least-recently-used first.
            tried.add(pick.name)

            # If HALF_OPEN, this is the probe request.
            is_probe = (pick.state == HealthState.HALF_OPEN)

            try:
                result = await self._call_single(
                    pick, messages, tools, max_tokens, temperature
                )
                result.retries = retries
                self._mark_success(pick)
                if retries > 0:
                    logger.info(
                        f"[Failover] Succeeded on {pick.name} "
                        f"(family={family}) after {retries} retries"
                    )
                return result
            except PermanentError:
                # Bad request - bubble up immediately, no retry.
                raise
            except RecoverableError:
                retries += 1
                if is_probe:
                    self._probe_transition(pick, success=False)
                await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                continue

        # Phase 1b: expand to ALL families (exclude fallback providers).
        if family:
            logger.warning(
                f"[Failover] All '{family}' providers exhausted, "
                f"expanding to all families"
            )
            while True:
                pool = [
                    p for p in self._available(include_fallback=False)
                    if p.name not in tried
                ]
                if not pool:
                    break
                pick = pool[0]
                tried.add(pick.name)
                is_probe = (pick.state == HealthState.HALF_OPEN)
                try:
                    result = await self._call_single(
                        pick, messages, tools, max_tokens, temperature
                    )
                    result.retries = retries
                    self._mark_success(pick)
                    logger.info(
                        f"[Failover] Emergency cross-family fallback to "
                        f"{pick.name}"
                    )
                    return result
                except PermanentError:
                    raise
                except RecoverableError:
                    retries += 1
                    if is_probe:
                        self._probe_transition(pick, success=False)
                    await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                    continue

        # Phase 2: fallback providers (is_fallback=True).
        pool = [
            p for p in self._available(include_fallback=True)
            if p.name not in tried
        ]
        for pick in pool:
            tried.add(pick.name)
            is_probe = (pick.state == HealthState.HALF_OPEN)
            try:
                result = await self._call_single(
                    pick, messages, tools, max_tokens, temperature
                )
                result.retries = retries
                self._mark_success(pick)
                logger.info(f"[Failover] Fell back to {pick.name}")
                return result
            except PermanentError:
                raise
            except RecoverableError:
                retries += 1
                if is_probe:
                    self._probe_transition(pick, success=False)
                await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                continue

        # Phase 3: all exhausted - if soonest cooldown is very short,
        # it likely means providers were blocked by MIN_INTERVAL (2s),
        # not a real cooldown. Sleep and retry instead of giving up.
        # SAFETY: skip retry when all providers have been tried already
        # to prevent infinite retry loop with a single provider.
        if self.providers:
            soonest = min(self.providers, key=lambda p: p.cooldown_until)
            wait = max(0, int(soonest.cooldown_until - time.time()))
            if wait <= 3 and len(tried) < len(self.providers):
                # Provider isn't really in cooldown - just blocked by
                # MIN_INTERVAL (subagent used it right before main agent).
                # Wait a moment and retry from expanded pool + fallback.
                logger.info(
                    f"[Failover] All exhausted but {soonest.name} cooldown "
                    f"only {wait}s - likely MIN_INTERVAL block, retrying"
                )
                await asyncio.sleep(max(1.0, wait + 0.5))
                # Retry: expanded pool (all non-fallback)
                pool = [
                    p for p in self._available(include_fallback=False, bypass_min_interval=True)
                    if p.name not in tried
                ]
                for pick in pool:
                    tried.add(pick.name)
                    is_probe = (pick.state == HealthState.HALF_OPEN)
                    try:
                        result = await self._call_single(
                            pick, messages, tools, max_tokens, temperature
                        )
                        result.retries = retries
                        self._mark_success(pick)
                        logger.info(
                            f"[Failover] Retry succeeded on {pick.name}"
                        )
                        return result
                    except PermanentError:
                        raise
                    except RecoverableError:
                        retries += 1
                        if is_probe:
                            self._probe_transition(pick, success=False)
                        continue
                # Retry: fallback providers
                pool = [
                    p for p in self._available(include_fallback=True, bypass_min_interval=True)
                    if p.name not in tried
                ]
                for pick in pool:
                    tried.add(pick.name)
                    is_probe = (pick.state == HealthState.HALF_OPEN)
                    try:
                        result = await self._call_single(
                            pick, messages, tools, max_tokens, temperature
                        )
                        result.retries = retries
                        self._mark_success(pick)
                        logger.info(
                            f"[Failover] Retry fallback succeeded on {pick.name}"
                        )
                        return result
                    except PermanentError:
                        raise
                    except RecoverableError:
                        retries += 1
                        if is_probe:
                            self._probe_transition(pick, success=False)
                        continue
            # All providers truly exhausted - track global failure for circuit breaker.
            self._global_consecutive_failures += 1
            if self._global_consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_broken_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
                logger.info(
                    f"[Failover] Fast circuit breaker TRIPPED - "
                    f"{self._global_consecutive_failures} consecutive failures, "
                    f"cooling down for {CIRCUIT_BREAKER_COOLDOWN}s"
                )
            return FailoverResult(
                content=(
                    f"All providers rate-limited. Nearest available: "
                    f"{soonest.name} in {wait}s. Please retry later."
                ),
                retries=retries,
            )

        return FailoverResult(content="No providers configured.", retries=retries)

    # ------------------------------------------------------------------
    # Streaming call
    # ------------------------------------------------------------------

    async def call_stream(self, messages: List[Dict], tools: Optional[List] = None,
                          max_tokens: int = 2000, temperature: float = 0.3,
                          preferred_family: Optional[str] = None):
        """Call LLM with streaming response and automatic failover.

        Yields chunks of response text (SSE-style) from the first available
        provider. On provider failure, falls through to the next provider
        in the same retry logic as call().

        Args:
            messages: List of chat message dicts (role/content format).
            tools: Optional list of tool definitions for function calling.
            max_tokens: Maximum tokens in the response (default 2000).
            temperature: Sampling temperature (default 0.3).
            preferred_family: If set, only pick from providers of this family first.

        Yields:
            str: Response text chunks as they arrive from the provider.
        """
        retries = 0
        tried: set = set()
        _call_start = time.time()

        # Fast circuit breaker check
        if self._circuit_broken_until > _call_start:
            remaining = int(self._circuit_broken_until - _call_start)
            logger.warning(
                f"[Failover] Fast circuit breaker active - "
                f"{remaining}s remaining. Skipping provider selection."
            )
            yield f"All providers in circuit-breaker cooldown ({remaining}s remaining). Please retry later."
            return

        # Build the request body (same as _call_single but with stream=True)
        body: Dict[str, Any] = {
            "model": None,  # filled per-provider
            "messages": copy.deepcopy(messages),
            "max_tokens": max_tokens,
            "stream": True,
        }

        family = preferred_family
        while True:
            if time.time() - _call_start > GLOBAL_CALL_TIMEOUT:
                logger.error(
                    f"[Failover] Global timeout ({GLOBAL_CALL_TIMEOUT}s) - "
                    f"aborting stream after {retries} retries"
                )
                with self._lock:
                    self._global_consecutive_failures += 1
                    if self._global_consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        self._circuit_broken_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
                return

            pool = [
                p for p in self._available(include_fallback=(family is None), family=family)
                if p.name not in tried
            ]
            if not pool:
                if family:
                    family = None  # expand to all families
                    continue
                break
            pick = pool[0]
            tried.add(pick.name)
            is_probe = (pick.state == HealthState.HALF_OPEN)

            # Fill per-provider body fields
            body["model"] = pick.model
            is_deepseek = ("deepseek" in pick.name.lower() or "deepseek" in pick.endpoint.lower())
            is_mimo = "mimo" in pick.name.lower() or "mimo" in pick.endpoint.lower()

            if not is_mimo:
                body["temperature"] = temperature
            if tools:
                body["tools"] = tools
            if is_deepseek:
                body["thinking"] = {"type": "disabled"}

            # Convert internal tool_calls to OpenAI format for DeepSeek and MiMo
            if is_deepseek or is_mimo:
                for mi, m in enumerate(body["messages"]):
                    tcs = m.get("tool_calls")
                    if tcs and any("function" not in tc for tc in tcs):
                        formatted = []
                        for tc in tcs:
                            fn = tc.get("function", {})
                            args_val = tc.get("args", fn.get("arguments", {}))
                            if isinstance(args_val, dict):
                                args_val = json.dumps(args_val)
                            formatted.append({
                                "id": tc.get("id", f"call_{mi}"),
                                "type": "function",
                                "function": {
                                    "name": tc.get("name", fn.get("name", "")),
                                    "arguments": args_val,
                                }
                            })
                        body["messages"][mi] = {**m, "tool_calls": formatted}

            data = json.dumps(body).encode("utf-8")

            auth_header = (
                {"api-key": pick.key}
                if is_mimo
                else {"Authorization": f"Bearer {pick.key}"}
            )
            headers = {"Content-Type": "application/json", **auth_header}

            try:
                session = await self._get_session()
                call_timeout = pick.timeout or PER_CALL_TIMEOUT
                async with session.post(
                    pick.endpoint,
                    data=data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=call_timeout),
                    ssl=ssl.create_default_context(),
                ) as resp:
                    if resp.status >= 400:
                        err_body = ""
                        try:
                            err_body = await resp.text()
                        except Exception:
                            pass
                        error_category = self._error_category(resp.status)
                        if error_category == "permanent":
                            logger.error(
                                f"[Failover] {pick.name} {resp.status} (permanent): {err_body[:200]}"
                            )
                            return
                        self._transition_health(pick, error_category)
                        if is_probe:
                            self._probe_transition(pick, success=False)
                        retries += 1
                        await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                        continue

                    # Stream the response line by line (SSE format)
                    async for line_bytes in resp.content:
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            chunk_data = line[6:]
                            if chunk_data == "[DONE]":
                                break
                            try:
                                chunk_json = json.loads(chunk_data)
                                delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except (json.JSONDecodeError, IndexError, KeyError):
                                continue
                    # Stream completed successfully
                    self._mark_success(pick)
                    return
            except asyncio.TimeoutError:
                self._transition_health(pick, "timeout")
                if is_probe:
                    self._probe_transition(pick, success=False)
                retries += 1
                await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                continue
            except aiohttp.ClientError:
                self._transition_health(pick, "server")
                if is_probe:
                    self._probe_transition(pick, success=False)
                retries += 1
                await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                continue
            except Exception:
                self._transition_health(pick, "server")
                if is_probe:
                    self._probe_transition(pick, success=False)
                retries += 1
                await asyncio.sleep(random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX))
                continue

        # All providers exhausted
        if self.providers:
            self._global_consecutive_failures += 1
            if self._global_consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_broken_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
        yield "All providers exhausted. Please retry later."

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> List[Dict]:
        """Return health status of all providers for monitoring and debugging.

        Each provider entry includes current state, availability, cooldown
        status, failure counts, and last-used timestamp.

        Returns:
            List of dicts, one per provider, with keys: name, model, family,
            fallback, state, available (bool), cooldown_remaining (seconds),
            consecutive_failures, total_failures, last_used (unix timestamp).
        """
        now = time.time()
        return [
            {
                "name": p.name,
                "model": p.model,
                "family": self._get_family(p),
                "fallback": p.is_fallback,
                "state": p.state,
                "available": p.state != HealthState.COOLED_DOWN or now >= p.cooldown_until,
                "cooldown_remaining": max(0, int(p.cooldown_until - now)),
                "consecutive_failures": p.consecutive_failures,
                "total_failures": p.fail_count,
                "last_used": int(p.last_used) if p.last_used else None,
            }
            for p in self.providers
        ]


# ------------------------------------------------------------------
# Error types
# ------------------------------------------------------------------

class RecoverableError(Exception):
    """Transient error - caller may retry with a different provider."""
    pass


class PermanentError(Exception):
    """Non-recoverable error - caller must NOT retry with this provider."""
    pass


# ------------------------------------------------------------------
# Tool call normalization
# ------------------------------------------------------------------

def _normalize_tc(tc_list):
    """Normalize API tool_calls format to expected format (name/args at top level)."""
    if not tc_list:
        return None
    result = []
    for t in tc_list:
        import logging as _lg
        _lg.warning("[DEBUG-tc-raw] raw tc keys=%s id=%s type=%s", list(t.keys()), t.get("id","")[:20], t.get("type",""))
        fn = t.get("function", {})
        if fn:
            _lg.warning("[DEBUG-tc-raw] fn keys=%s name=%r", list(fn.keys()), fn.get("name",""))
        else:
            _lg.warning("[DEBUG-tc-raw] NO function key in tc! tc=%s", str(t)[:200])
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        result.append({
            "id": t.get("id", ""),
            "name": fn.get("name", ""),
            "args": args,
        })
    return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    """Read an environment variable, returning default if not set."""
    return os.environ.get(key, default)


# Added for molecule engine wiring (imported by unified_worker.py)
_molecule_engine_global = None

def set_global_molecule_engine(engine):
    global _molecule_engine_global
    _molecule_engine_global = engine

def get_global_molecule_engine():
    return _molecule_engine_global
