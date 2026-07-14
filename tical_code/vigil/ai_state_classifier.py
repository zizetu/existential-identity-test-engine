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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
AI execution state classifier for the Vigil guardian layer.

This module maps AISignals (raw execution metrics) into classified AI states
with confidence scores and evidence. It uses deterministic thresholds - no
ML model is involved. It is the interpretation half of the AI signal pipeline
(ai_signal_collector gathers data; this module classifies it).

STATE SPACE:

    STUCK       - Highest priority. AI is active but:
                  - No progress (token or tool call) for > 60 seconds, OR
                  - Same tool called >= 3 consecutive times.
                  Confidence 0.9.

    WAITING     - AI is idle or waiting for external input.
                  Confidence 0.95.

    GENERATING  - AI is producing output at a high token rate
                  (>= 10 tokens/sec). Confidence scales with rate,
                  0.6 + rate/50, capped at 0.95.

    REASONING   - AI is active with no tool calls, low token rate
                  (< 5 tokens/sec), unfinished output, and > 5 seconds
                  of task duration. Confidence 0.75.

    DEEP_WORK   - AI is actively executing tool calls.
"""
import time
from dataclasses import dataclass, field
from typing import List
from .ai_signal_collector import AISignal

@dataclass
class AIStateResult:
    state: str; confidence: float; evidence: List[str]; duration_seconds: float

_STUCK_NO_PROGRESS_SEC = 60; _STUCK_REPEAT_CALLS = 3
_DEEP_WORK_MIN_TOOLS = 2; _REASONING_TOKEN_RATE = 5.0; _GENERATING_TOKEN_RATE = 10.0

class AIStateClassifier:
    def classify(self, signal: AISignal) -> AIStateResult:
        evidence = []
        no_progress = signal.has_unfinished_output and (time.time() - signal.last_progress_time) > _STUCK_NO_PROGRESS_SEC
        repeat_tools = signal.tool_call_repeat_count >= _STUCK_REPEAT_CALLS
        if no_progress or repeat_tools:
            if no_progress: evidence.append(f"no_progress_{(time.time()-signal.last_progress_time):.0f}s")
            if repeat_tools: evidence.append(f"tool_repeat_{signal.tool_call_repeat_count}x")
            return AIStateResult("STUCK", 0.9, evidence, signal.task_duration_seconds)
        if signal.current_task_type in ("waiting", "idle"):
            evidence.append("no_active_task")
            return AIStateResult("WAITING", 0.95, evidence, signal.task_duration_seconds)
        if signal.token_consumption_rate >= _GENERATING_TOKEN_RATE and signal.has_unfinished_output:
            evidence.append(f"token_rate_{signal.token_consumption_rate:.1f}/s")
            conf = min(0.95, 0.6 + signal.token_consumption_rate / 50)
            return AIStateResult("GENERATING", conf, evidence, signal.task_duration_seconds)
        if signal.tool_call_count == 0 and signal.token_consumption_rate < _REASONING_TOKEN_RATE and signal.has_unfinished_output and signal.task_duration_seconds > 5:
            evidence.append("no_tool_calls"); evidence.append(f"low_token_rate_{signal.token_consumption_rate:.1f}/s")
            return AIStateResult("REASONING", 0.75, evidence, signal.task_duration_seconds)
        if signal.tool_call_count >= _DEEP_WORK_MIN_TOOLS and signal.has_unfinished_output:
            evidence.append(f"tool_calls_{signal.tool_call_count}")
            conf = min(0.9, 0.5 + signal.tool_call_count * 0.1)
            return AIStateResult("DEEP_WORK", conf, evidence, signal.task_duration_seconds)
        evidence.append(f"task_type_{signal.current_task_type}")
        return AIStateResult("DEEP_WORK", 0.5, evidence, signal.task_duration_seconds)
