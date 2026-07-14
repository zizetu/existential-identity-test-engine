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
AI execution state signal collector for the Vigil guardian layer.

This module tracks the AI's execution behavior in real time to detect stuck
tasks, wasteful loops, and progress stalls. It maintains counters and event
logs for tool calls, token consumption, output length, and progress markers.

The AISignalCollector is the data-gathering half; ai_state_classifier is the
interpretation half that maps raw signals to classified AI states.

SIGNALS COLLECTED:
    - Task type (idle, waiting, or an active task label)
    - Task duration (seconds since task_started)
    - Tool call counts: total calls, consecutive repeat calls, identical results
    - Token consumption rate (tokens/sec over last 10 seconds)
    - Output length and whether output is still in progress
    - Time since last progress (for stuck detection)
    - Estimated completion (0.0-1.0)

STUCK DETECTION:
    is_stuck() returns True when the AI is active AND either:
    (a) No progress for > 60 seconds, OR
    (b) Same tool called >= 3 consecutive times
"""
import time
from dataclasses import dataclass, field
from typing import List

@dataclass
class AISignal:
    current_task_type: str = "idle"
    task_duration_seconds: float = 0.0
    tool_call_count: int = 0
    tool_call_repeat_count: int = 0
    tool_call_identical_results: int = 0
    token_consumption_rate: float = 0.0
    output_length: int = 0
    has_unfinished_output: bool = False
    last_progress_time: float = field(default_factory=time.time)
    estimated_completion: float = 0.0

@dataclass
class _ToolCallEvent:
    tool_name: str; timestamp: float

@dataclass
class _TokenEvent:
    count: int; timestamp: float

class AISignalCollector:
    _STUCK_REPEAT_THRESHOLD = 3
    _STUCK_NO_PROGRESS_SECONDS = 60
    def __init__(self):
        self._task_type = "idle"; self._task_start = 0.0
        self._tool_calls: List[_ToolCallEvent] = []
        self._token_events: List[_TokenEvent] = []
        self._output_length = 0; self._has_unfinished = False
        self._last_progress = time.time(); self._estimated_completion = 0.0
        self._is_active = False; self._identical_results = 0
    def task_started(self, task_type: str):
        self._task_type = task_type; self._task_start = time.time()
        self._tool_calls.clear(); self._token_events.clear()
        self._output_length = 0; self._has_unfinished = True
        self._last_progress = time.time(); self._estimated_completion = 0.0
        self._is_active = True
    def task_completed(self):
        self._has_unfinished = False; self._is_active = False
        self._task_type = "idle"; self._estimated_completion = 1.0
    def set_waiting(self):
        self._task_type = "waiting"; self._has_unfinished = False; self._is_active = False
    def record_tool_call(self, tool_name: str, result_hash: str = ""):
        self._tool_calls.append(_ToolCallEvent(tool_name, time.time()))
        if result_hash:
            self._last_result = result_hash
            self._identical_results = self._count_identical_results(result_hash)
        else:
            self._identical_results = 0
        self._last_progress = time.time()
    def _count_identical_results(self, result_hash: str) -> int:
        if not hasattr(self, '_result_history'):
            self._result_history = []
        self._result_history.append(result_hash)
        if len(self._result_history) < 2:
            return 0
        count = 0
        for h in reversed(self._result_history):
            if h == result_hash:
                count += 1
            else:
                break
        return count if count > 1 else 0
    def record_tokens(self, count: int):
        self._token_events.append(_TokenEvent(count, time.time()))
        if count > 0: self._last_progress = time.time()
    def record_output_length(self, length: int): self._output_length = length
    def set_estimated_completion(self, value: float): self._estimated_completion = max(0.0, min(1.0, value))
    def collect(self) -> AISignal:
        now = time.time(); duration = (now - self._task_start) if self._task_start else 0.0
        cutoff = now - 10
        recent_tokens = [e for e in self._token_events if e.timestamp >= cutoff]
        rate = sum(e.count for e in recent_tokens) / 10.0 if recent_tokens else 0.0
        return AISignal(current_task_type=self._task_type, task_duration_seconds=duration,
            tool_call_count=len(self._tool_calls), tool_call_repeat_count=self._count_tool_repeats(),
            tool_call_identical_results=getattr(self, '_identical_results', 0),
            token_consumption_rate=rate, output_length=self._output_length,
            has_unfinished_output=self._has_unfinished, last_progress_time=self._last_progress,
            estimated_completion=self._estimated_completion)
    def _count_tool_repeats(self) -> int:
        if len(self._tool_calls) < 2: return 0
        last = self._tool_calls[-1].tool_name; count = 0
        for ev in reversed(self._tool_calls):
            if ev.tool_name == last: count += 1
            else: break
        return count if count > 1 else 0
    def is_stuck(self) -> bool:
        sig = self.collect()
        no_progress = (time.time() - sig.last_progress_time) > self._STUCK_NO_PROGRESS_SECONDS
        repeat_stuck = sig.tool_call_repeat_count >= self._STUCK_REPEAT_THRESHOLD
        return self._is_active and (no_progress or repeat_stuck)
