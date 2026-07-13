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
# Original repository: https://github.com/zizetu/tical-agent
#

"""Module 2: Context Compaction - token-aware conversation trimming."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tical-code.context_compactor")

class ContextCompactor:
    """Token-aware conversation compactor that prunes, summarizes, or truncates message history.

    Implements a three-phase strategy to keep conversation context within a
    configurable token budget: Phase 1 prunes long tool outputs, Phase 2 uses
    LLM-based summarization of older messages, and Phase 3 falls back to keeping
    only the most recent messages plus the system prompt. Designed to integrate
    before every API call to prevent context-window overflow and reduce cost.

    Enhanced with persistent summary storage (absorbed from core/context_manager.py):
    - save_summary / load_summary persist compacted summaries to disk
    - Summary tracking prevents re-summarizing the same message range
    - persist_dir config determines where summaries are stored
    """

    _SUMMARY_PROMPT = (
        "Summarize the following conversation, preserving key decisions, "
        "verified results, and user preferences. Discard greetings, failed "
        "attempts, and intermediate reasoning. Be concise (max 500 tokens)."
    )

    def __init__(self, max_tokens: int = 128000, keep_recent: int = 12,
                 compact_threshold_pct: float = 0.8,
                 persist_dir: str = ""):
        """Initialize the context compactor with token budget and retention parameters.

        Args:
            max_tokens: Maximum total tokens allowed before hard compaction is required.
                Defaults to 96000, which is suitable for large-context models up to 128k.
            keep_recent: Number of most recent messages to always preserve intact,
                regardless of token budget. Defaults to 6.
            compact_threshold_pct: Fraction of max_tokens at which proactive compaction
                is triggered via compact_if_needed. Defaults to 0.8 (80%).
            persist_dir: Optional directory for persistent summary storage on disk.
                When set, summaries are saved/loaded from this directory for
                cross-session continuity. Empty string disables persistence.
        """
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self.compact_threshold_pct = compact_threshold_pct
        self.persist_dir = persist_dir

        # Force-compact flag: when set, the next compact_if_needed() call
        # will force compaction regardless of current threshold.
        # Set by doom_loop recovery FORCE_SUMMARIZE; cleared after one run.
        self._force_compact_pending = False

        # Summary tracking: maps (session_id, last_summary_step) to avoid
        # re-summarizing the same message range. Persisted to disk if persist_dir is set.
        self._summary_index: dict[str, int] = {}  # session_id -> last_summarized_message_count

    # -----------------------------------------------------------------------
    # Persistent summary methods (absorbed from core/context_manager.py)
    # -----------------------------------------------------------------------

    def _state_path(self, session_id: str) -> Path:
        """Return the path to the persistent summary state file for a session."""
        if not self.persist_dir:
            return Path("/dev/null")
        return Path(self.persist_dir) / "compactor" / f"{session_id}_summary.json"

    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary for a session from disk.

        Returns the summary text if found, or None if no persisted summary exists.

        Args:
            session_id: The session or task identifier to load summaries for.

        Returns:
            The summary text string, or None if not found.
        """
        if not self.persist_dir:
            return None
        sp = self._state_path(session_id)
        if not sp.exists():
            return None
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            self._summary_index[session_id] = data.get("last_count", 0)
            return data.get("summary", "")
        except Exception:
            logger.debug("Failed to load summary for session %s", session_id)
            return None

    def save_summary(self, session_id: str, summary: str, message_count: int) -> None:
        """Persist a summary for a session to disk.

        Writes the summary text and the message count at which it was generated,
        enabling de-duplication across restarts.

        Args:
            session_id: The session or task identifier.
            summary: The summary text to persist.
            message_count: The total message count at the time of summarization,
                used to skip re-summarization on subsequent calls.
        """
        if not self.persist_dir:
            return
        sp = self._state_path(session_id)
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "session_id": session_id,
                "summary": summary,
                "last_count": message_count,
            }
            # Atomic write via temp file + rename
            tmp = sp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.rename(str(tmp), str(sp))
            self._summary_index[session_id] = message_count
            logger.info("Summary persisted for session %s (at msg #%d)", session_id, message_count)
        except Exception as e:
            logger.warning("Failed to persist summary for session %s: %s", session_id, e)

    def has_summary(self, session_id: str) -> bool:
        """Check whether a persistent summary exists for the given session.

        Args:
            session_id: The session identifier to check.

        Returns:
            True if a summary has been saved for this session.
        """
        if not self.persist_dir:
            return False
        return self._state_path(session_id).exists()

    def get_last_summary_count(self, session_id: str) -> int:
        """Return the message count at which the last summary was generated.

        Used to avoid re-summarizing the same message range. Returns 0 if
        no summary has been saved for this session.

        Args:
            session_id: The session identifier to query.

        Returns:
            The message count of the last summary, or 0.
        """
        return self._summary_index.get(session_id, 0)

    # -----------------------------------------------------------------------
    # Token estimation
    # -----------------------------------------------------------------------

    @staticmethod
    def _count_cjk(text) -> int:
        return sum(
            1 for ch in text
            if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af"
        )

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Estimate the total token count for a list of chat messages using a heuristic formula.

        Counts CJK characters at roughly 2 chars per token and other characters at
        roughly 4 chars per token, then adds 4 tokens of overhead per message to
        account for role and formatting metadata. Tool call content is serialized
        and included in the estimate.

        Args:
            messages: A list of message dictionaries, each with at least a "content"
                key and optionally a "tool_calls" key.

        Returns:
            An integer representing the estimated token count for the entire message list.
        """
        total = 0
        for msg in messages:
            raw_content = msg.get("content", "")
            if isinstance(raw_content, str):
                text = raw_content
            elif isinstance(raw_content, list):
                text_parts = []
                for part in raw_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                text = "\n".join(text_parts)
            else:
                text = str(raw_content)
            if msg.get("tool_calls"):
                text += json.dumps(msg["tool_calls"])
            cjk = self._count_cjk(text)
            other = max(0, len(text) - cjk)
            total += (cjk + 1) // 2 + (other + 3) // 4 + 4  # +4 overhead per msg
        return total

    # -----------------------------------------------------------------------
    # Compaction trigger checks
    # -----------------------------------------------------------------------

    def needs_compaction(self, messages: list[dict]) -> bool:
        """Determine whether the message list exceeds the absolute maximum token budget.

        Returns True when estimated tokens exceed max_tokens, indicating that
        hard compaction is required before sending to the model. This is used
        as a safety gate to prevent context-window overflow errors.

        Args:
            messages: A list of message dictionaries to evaluate for token budget.

        Returns:
            True if the estimated token count exceeds max_tokens, False otherwise.
        """
        return self.estimate_tokens(messages) > self.max_tokens

    def should_compact(self, messages: list[dict]) -> bool:
        """Check whether tokens exceed the proactive compaction threshold percentage.

        Compares the estimated token count against a threshold derived from
        max_tokens multiplied by compact_threshold_pct (default 80%). When this
        returns True, compaction should be performed preemptively to maintain
        a healthy token budget before hitting the hard limit.

        Args:
            messages: A list of message dictionaries to evaluate against the
                proactive threshold.

        Returns:
            True if the estimated token count exceeds the configured threshold
            percentage of max_tokens, False otherwise.
        """
        threshold = int(self.max_tokens * self.compact_threshold_pct)
        return self.estimate_tokens(messages) > threshold

    # -----------------------------------------------------------------------
    # Compaction pipeline
    # -----------------------------------------------------------------------

    def compact_if_needed(self, messages: list[dict],
                          llm_call_fn: Callable,
                          session_id: str = "") -> list[dict]:
        """Proactively compact the message history if tokens exceed the threshold percentage.

        If estimated tokens exceed the configured threshold percentage of
        max_tokens, triggers LLM-based compaction via compact(). Otherwise
        returns the messages unchanged. This is the primary integration point:
        call it right before every API call to maintain a healthy token budget.

        When session_id is provided, the compacted summary is automatically
        persisted to disk via save_summary().

        Args:
            messages: A list of message dictionaries representing the full
                conversation history to potentially compact.
            llm_call_fn: A callable that accepts a list of messages and returns
                a response dictionary or string from an LLM, used to generate
                conversation summaries during compaction.
            session_id: Optional session identifier for persistent summary storage.
                When provided and persistence is enabled, the generated summary
                is saved to disk for cross-session continuity.

        Returns:
            The compacted message list if compaction was triggered, or the
            original messages unchanged if under the threshold.
        """
        threshold = int(self.max_tokens * self.compact_threshold_pct)
        estimate = self.estimate_tokens(messages)
        if estimate > threshold or self._force_compact_pending:
            self._force_compact_pending = False
            logger.info(
                "compaction triggered: ~%d tokens > %d threshold (max=%d, pct=%.0f%%)",
                estimate, threshold, self.max_tokens, self.compact_threshold_pct * 100,
            )
            compacted = self.compact(messages, llm_call_fn)
            # Persist summary if session_id and persist_dir are configured
            if session_id and self.persist_dir:
                # Extract summary text from compacted result (Phase 2 summary msg)
                for msg in compacted:
                    if msg.get("role") == "system" and msg.get("content", "").startswith("[Context summary]"):
                        self.save_summary(
                            session_id,
                            msg["content"].replace("[Context summary]\n", "", 1),
                            len(messages),
                        )
                        break
            return compacted
        logger.info(
            "compaction skipped: ~%d tokens <= %d threshold (max=%d)",
            estimate, threshold, self.max_tokens,
        )
        return messages

    def compact(self, messages: list[dict], llm_call_fn: Callable) -> list[dict]:
        """Execute the full three-phase compaction pipeline to reduce message history size.

        Phase 1 truncates any tool output messages exceeding 8000 characters.
        Phase 2 uses an LLM to generate a summary of older messages, preserving
        recent messages intact. Phase 3 falls back to keeping only the system
        prompt and the most recent messages if summarization fails or the
        message list is too short. Ensures tool-call/tool-result message pairs
        are never orphaned.

        Args:
            messages: A list of message dictionaries representing the full
                conversation history to compact.
            llm_call_fn: A callable that accepts a list of messages and returns
                an LLM response, used for summarization in Phase 2.

        Returns:
            A compacted list of message dictionaries suitable for sending to
            the model, guaranteed to have no orphaned tool messages.
        """
        try:
            if not messages:
                return messages

            system_msg = messages[0]  # never touch
            start_idx = 1 if messages[0].get("role") == "system" else 0

            # Phase 1: Prune LOW importance - truncate long tool outputs
            pruned: list[dict] = [system_msg] if start_idx else []
            for i in range(start_idx, len(messages)):
                msg = messages[i]
                new_msg = dict(msg)
                content = new_msg.get("content", "")
                if new_msg.get("role") == "tool" and len(content) > 8000:
                    new_msg["content"] = (
                        f"[output truncated: {content[:500]}... ({len(content)} chars total)]"
                    )
                pruned.append(new_msg)

            if not self.needs_compaction(pruned):
                return pruned

            # Phase 2: LLM summarization of older messages
            if len(pruned) <= self.keep_recent + start_idx:
                # Too short to summarize meaningfully
                tail = pruned[-self.keep_recent:]
                return ([system_msg] if start_idx else []) + tail

            to_summarize = pruned[start_idx:-self.keep_recent]
            intact_tail = pruned[-self.keep_recent:]

            summary_text = self._generate_summary(to_summarize, llm_call_fn)
            if summary_text:
                summary_msg = {"role": "system", "content": (
                    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
                    "to stay within the token budget. Do NOT answer questions or fulfill "
                    "requests mentioned in this summary — they were already addressed.\n"
                    f"\n{summary_text}"
                )}
                # Fix Phase 2 too: ensure tool messages in intact_tail have their parents
                tail = list(intact_tail)
                tool_ids_needed = set()
                for msg in tail:
                    if msg.get("role") == "tool" and msg.get("tool_call_id"):
                        tool_ids_needed.add(msg["tool_call_id"])
                for msg in reversed(to_summarize):
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        for tc in msg.get("tool_calls", []):
                            if tc.get("id") in tool_ids_needed:
                                tail.insert(0, msg)
                                tool_ids_needed.discard(tc.get("id"))
                                break
                    if not tool_ids_needed:
                        break
                result = ([system_msg] if start_idx else []) + [summary_msg] + tail
                return result

            # Phase 3: Fallback - keep system prompt + recent only
            tail = list(pruned[-self.keep_recent:])
            # Ensure tool messages are NEVER orphaned: scan forward from start_idx,
            # find all tool_call_ids referenced in kept tool messages,
            # then include any assistant messages with matching tool_calls from outside the window
            tool_ids_needed = set()
            for msg in tail:
                if msg.get("role") == "tool" and msg.get("tool_call_id"):
                    tool_ids_needed.add(msg["tool_call_id"])
            # Scan backward from the head of tail to find matching tool_calls
            for msg in reversed(pruned[start_idx:-self.keep_recent] if len(pruned) > self.keep_recent + start_idx else []):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        if tc.get("id") in tool_ids_needed:
                            # Insert this assistant message before the tail
                            tail.insert(0, msg)
                            tool_ids_needed.discard(tc.get("id"))
                            break
                if not tool_ids_needed:
                    break
            result = ([system_msg] if start_idx else []) + tail
            return result

        except Exception:
            logger.exception("compaction failed catastrophically")
            try:
                return [messages[0]] + messages[-self.keep_recent:]
            except Exception:
                return messages[-self.keep_recent:]

    def _generate_summary(self, messages: list[dict], llm_call_fn: Callable) -> str:
        try:
            parts = []
            for m in messages:
                role = m.get("role", "?")
                content = m.get("content", "")
                # Sanitize tool outputs: strip {{ and }} template markers to prevent injection
                if isinstance(content, str):
                    content = content.replace("{{", "\\{\\{").replace("}}", "\\}\\}")
                parts.append(f"[{role}] {content}")
            dialogue = "\n".join(parts)

            prompt = f"{self._SUMMARY_PROMPT}\n\n---\n{dialogue}\n---"
            call_msgs = [
                {"role": "system", "content": "You are a helpful summarizer."},
                {"role": "user", "content": prompt},
            ]
            response = llm_call_fn(call_msgs)
            if isinstance(response, dict):
                return (response.get("content") or "").strip()
            return str(response).strip()
        except Exception:
            logger.exception("LLM summary failed")
            return ""
