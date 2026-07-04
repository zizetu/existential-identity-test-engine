# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Original repository: https://github.com/zizetu/EITE-agent
#
# Built on EITElite mesh infrastructure.
# Independent system, not a fork of any other agent framework.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Performance metrics collector — tool latency, LLM latency, error rates.

Records execution timing for every tool call and LLM round, stores
rolling-window aggregates, and exposes them via a check_metrics tool.

Registered in module_defs.py via @register decorator. Description text
is read by the agent at prompt-build time so it knows this capability
exists and how to query it.
"""

import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, List

logger = logging.getLogger("EITElite.metrics")


class MetricsCollector:
    """Rolling-window performance metrics with per-tool breakdown.

    Records three metric families:
      - tool_latency: per-tool execution time (seconds)
      - llm_latency:   per-LLM-call round-trip time (seconds)
      - error_count:   per-tool error occurrences

    Each family uses a deque with a fixed-size window (default 200
    entries) so memory stays bounded regardless of uptime.
    """

    def __init__(self, window_size: int = 200):
        self.window_size = window_size
        # tool_name -> deque of (timestamp, duration_seconds)
        self._tool_latency: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        # model_name -> deque of (timestamp, duration_seconds)
        self._llm_latency: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        # tool_name -> deque of (timestamp, error_message)
        self._errors: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        self._total_llm_calls = 0
        self._total_tool_calls = 0
        self._start_time = time.time()

    # ── Recording methods (called by tool_executor / message_handler) ──

    def record_tool_call(self, tool_name: str, duration: float) -> None:
        """Record a completed tool execution.

        Args:
            tool_name: Name of the tool that ran.
            duration: Wall-clock seconds the tool took.
        """
        self._tool_latency[tool_name].append((time.time(), duration))
        self._total_tool_calls += 1

    def record_tool_error(self, tool_name: str, error: str) -> None:
        """Record a tool execution that raised or was blocked.

        Args:
            tool_name: Name of the tool that failed.
            error: Short description of the error or block reason.
        """
        self._errors[tool_name].append((time.time(), error[:200]))
        self._total_tool_calls += 1

    def record_llm_call(self, model_name: str, duration: float) -> None:
        """Record an LLM API round-trip.

        Args:
            model_name: Model identifier (e.g. 'deepseek-v4-flash').
            duration: Wall-clock seconds for the full round trip.
        """
        self._llm_latency[model_name].append((time.time(), duration))
        self._total_llm_calls += 1

    # ── Reporting methods (called by the AI via check_metrics tool) ──

    def summary(self) -> Dict[str, Any]:
        """Return a compact metrics summary dict.

        Returns averages over the entire rolling window for each
        tool / model.  Empty dict entries are omitted.
        """
        result: Dict[str, Any] = {
            "uptime_seconds": int(time.time() - self._start_time),
            "total_tool_calls": self._total_tool_calls,
            "total_llm_calls": self._total_llm_calls,
        }

        # Per-tool average latency
        tool_avgs = {}
        for name, dq in self._tool_latency.items():
            if dq:
                tool_avgs[name] = round(
                    sum(e[1] for e in dq) / len(dq), 3
                )
        if tool_avgs:
            result["tool_avg_latency_sec"] = tool_avgs

        # Per-model average LLM latency
        llm_avgs = {}
        for name, dq in self._llm_latency.items():
            if dq:
                llm_avgs[name] = round(
                    sum(e[1] for e in dq) / len(dq), 3
                )
        if llm_avgs:
            result["llm_avg_latency_sec"] = llm_avgs

        # Error counts per tool
        error_counts = {}
        for name, dq in self._errors.items():
            if dq:
                error_counts[name] = len(dq)
                # Include last error as example
                result[f"last_error_{name}"] = dq[-1][1][:100]
        if error_counts:
            result["tool_error_counts"] = error_counts

        return result

    def top_slowest(self, n: int = 5) -> List[Dict[str, Any]]:
        """Return the N slowest tool calls across all tools."""
        all_calls = []
        for name, dq in self._tool_latency.items():
            for ts, dur in dq:
                all_calls.append((dur, name, ts))
        all_calls.sort(reverse=True)
        return [
            {"tool": name, "duration_sec": round(dur, 3)}
            for dur, name, ts in all_calls[:n]
        ]
