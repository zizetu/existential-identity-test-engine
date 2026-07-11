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

Message Adapter - EITE Evaluation Message Format Conversion
=============================================================

Problem: Different model families expect different message formats during
evaluation. MiMo strips reasoning_content; DeepSeek preserves it. Orphaned
tool_call_ids cause 400 errors on model switches.

Solution: Adapter that converts evaluation messages to target model format
before each call.

Features:
- Format adaptation (reasoning_content, tool_calls, roles)
- Orphaned tool_call_id repair
- Session-affinity enforcement (no model switch mid-evaluation)
- 429 rate-limit backoff (exponential, don't immediately failover)
- Message validation and repair

Author: Tical (Zize Tu)
"""

import copy
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Model families and their format requirements
MODEL_FAMILIES = {
    "mimo": {
        "strip_reasoning": True,
        "max_role_sequence": None,
        "native_tool_format": "openai",
    },
    "mimo-v2.5": {
        "strip_reasoning": True,
    },
    "deepseek": {
        "strip_reasoning": True,
        "native_tool_format": "openai",
    },
    "deepseek-v4": {
        "strip_reasoning": True,
    },
    "deepseek-v4-flash": {
        "strip_reasoning": True,
    },
    "deepseek-v4-pro": {
        "strip_reasoning": True,
    },
    "default": {
        "strip_reasoning": True,
        "native_tool_format": "openai",
    },
}

# 429 backoff parameters
BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_MULTIPLIER = 2.0
BACKOFF_JITTER = 0.5

# Allowed message roles in order
ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


# ---------------------------------------------------------------------------
# Message Adapter
# ---------------------------------------------------------------------------

class MessageAdapter:
    """Adapts evaluation message lists to target model format before each LLM call.

    Usage:
        adapter = MessageAdapter()
        adapted = adapter.adapt(messages, model_family="mimo")
        response = llm.call(adapted, ...)
    """

    def __init__(self):
        self._backoff: Dict[str, float] = {}  # provider_name -> backoff_until
        self._task_family: Optional[str] = None  # locked during evaluation

    def lock_family(self, family: str) -> None:
        """Lock model family for the current evaluation - no switching allowed."""
        self._task_family = family
        logger.info("[EITE msg-adapter] Model family locked: %s", family)

    def unlock_family(self) -> None:
        """Release model family lock after evaluation completion."""
        self._task_family = None

    @property
    def locked_family(self) -> Optional[str]:
        return self._task_family

    def adapt(
        self,
        messages: List[Dict],
        model_family: str = "default",
        task_locked: bool = False,
    ) -> List[Dict]:
        """Adapt message list for target model family.

        Args:
            messages: Original message list (system/user/assistant/tool).
            model_family: Target model family name.
            task_locked: If True, don't switch model family.

        Returns:
            Adapted message list safe for the target model.
        """
        if task_locked and self._task_family:
            model_family = self._task_family

        family_config = MODEL_FAMILIES.get(model_family, MODEL_FAMILIES["default"])

        adapted = copy.deepcopy(messages)

        if family_config.get("strip_reasoning", True):
            adapted = self._strip_reasoning(adapted)

        adapted = self._fix_orphaned_tool_calls(adapted)
        adapted = self._validate_roles(adapted)
        adapted = self._normalize_tool_calls(adapted)

        return adapted

    def _strip_reasoning(self, messages: List[Dict]) -> List[Dict]:
        """Remove reasoning_content from all messages."""
        for msg in messages:
            if msg.get("role") == "assistant":
                msg.pop("reasoning_content", None)
                for tc in msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        tc.pop("reasoning_content", None)
        return messages

    def _fix_orphaned_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        """Fix or remove orphaned tool_call_ids.

        An orphaned tool_call_id is a tool message whose tool_call_id
        doesn't match any assistant message's tool_calls.
        """
        known_ids: Set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        known_ids.add(tc_id)

        fixed = []
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id and tc_id not in known_ids:
                    fixed.append({
                        "role": "system",
                        "content": f"[Orphaned tool response repaired: tool_call_id={tc_id} was lost -- skipping]"
                    })
                    logger.debug("[EITE msg-adapter] Repaired orphaned tool_call_id: %s", tc_id)
                    continue
            fixed.append(msg)

        return fixed

    def _validate_roles(self, messages: List[Dict]) -> List[Dict]:
        """Validate and fix message roles.

        Every message must have a valid role. Unknown roles become 'system'.
        """
        for msg in messages:
            role = msg.get("role", "")
            if role not in ALLOWED_ROLES:
                msg["role"] = "system"
                logger.debug("[EITE msg-adapter] Fixed invalid role: %s -> system", role)
        return messages

    def _normalize_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        """Ensure tool_calls have the standard OpenAI format.

        Fixes:
        - Missing 'type': 'function' field
        - Missing 'id' field
        - Non-dict tool_calls entries
        """
        for msg in messages:
            if msg.get("role") != "assistant":
                continue

            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                continue

            normalized = []
            for i, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    logger.debug("[EITE msg-adapter] Skipping non-dict tool_call at index %d", i)
                    continue

                if "type" not in tc:
                    tc["type"] = "function"

                if "id" not in tc:
                    tc["id"] = f"call_{int(time.time())}_{i}"

                func = tc.get("function", {})
                if isinstance(func, dict) and "arguments" in func:
                    args = func["arguments"]
                    if isinstance(args, dict):
                        func["arguments"] = json.dumps(args, ensure_ascii=False)
                    elif not isinstance(args, str):
                        func["arguments"] = str(args)

                normalized.append(tc)

            msg["tool_calls"] = normalized

        return messages

    # ------------------------------------------------------------------
    # 429 Backoff management
    # ------------------------------------------------------------------

    def should_retry(self, provider_name: str) -> bool:
        """Check if we should retry a 429'd provider."""
        until = self._backoff.get(provider_name, 0)
        return time.time() >= until

    def record_429(self, provider_name: str, attempt: int = 1) -> float:
        """Record a 429 from a provider and calculate backoff.

        Returns the backoff duration in seconds.
        """
        import random
        delay = min(
            BACKOFF_INITIAL * (BACKOFF_MULTIPLIER ** (attempt - 1)),
            BACKOFF_MAX
        )
        delay += random.uniform(0, BACKOFF_JITTER)
        self._backoff[provider_name] = time.time() + delay
        logger.warning(
            "[EITE msg-adapter] Provider %s 429'd -- backoff %.1fs (attempt %d)",
            provider_name, delay, attempt
        )
        return delay

    def get_backoff_status(self) -> Dict[str, float]:
        """Get backoff status for all providers."""
        now = time.time()
        return {
            name: max(0, until - now)
            for name, until in self._backoff.items()
            if until > now
        }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_default_adapter = MessageAdapter()


def adapt_messages(
    messages: List[Dict],
    model_family: str = "default",
    task_locked: bool = False,
) -> List[Dict]:
    """Convenience wrapper for MessageAdapter.adapt()."""
    return _default_adapter.adapt(messages, model_family, task_locked)


def lock_family(family: str) -> None:
    """Lock model family globally."""
    _default_adapter.lock_family(family)


def unlock_family() -> None:
    """Unlock model family globally."""
    _default_adapter.unlock_family()
