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
AI interrupt evaluator - intercepts and categorises new human instructions.

When the AI is busy executing a task and the human sends a new instruction,
this module decides whether to execute immediately, interrupt the current
task, queue for later, or reject outright. It balances responsiveness against
the cost of context-switching.

CATEGORIES (in evaluation priority order):

    urgent    - Contains explicit urgency keywords ("urgent", "emergency",
                "critical", "fire", "asap", "immediately") OR has an urgency
                hint >= 0.9. Always executes immediately.

    hurry     - Contains rushing language ("hurry", "rush", "speed up",
                "faster", "come on") but NO substantive content. These are
                REJECTED because they don't help - the AI is already working.

    redirect  - Contains direction-change keywords ("stop", "cancel", "abort",
                "restart", "start over", "wrong direction"). If context loss
                is < 0.5, interrupts immediately; otherwise queues with a
                notification that the current task is nearly done.

    parallel  - Non-urgent side requests ("by the way", "also", "quick
                question", "one more thing"). Always queued.

    keep_going - Encouragement ("continue", "keep going", "fine"). Suppressed.
"""
import re, time
from dataclasses import dataclass, field
from .ai_state_classifier import AIStateResult

@dataclass
class NewInstruction:
    content: str; source: str = "human"; urgency_hint: float = 0.0; timestamp: float = field(default_factory=time.time)

@dataclass
class InterruptVerdict:
    action: str; reason: str; estimated_context_loss: float; queue_priority: int; cooldown_minutes: float; notify_message: str = ""

_URGENT = re.compile(r"urgent|on-fire|emergency|emergency|urgent|critical|fire|asap|immediately", re.I)
_HURRY = re.compile(r"hurry|rush|hurry-up|speed|faster|hurry[^,]*|speed up|come on", re.I)
_REDIRECT = re.compile(r"wrong-direction|stop|stop|stop|cancel|abort|restart|start over", re.I)
_PARALLEL = re.compile(r"by-the-way|also-help|additionally|another-thing|by the way|also|quick question|one more thing", re.I)

_KEEP_GOING = re.compile(r"continue|fine|dont-mind-me|keep going|im fine|dont stop|keep-going", re.I)

class AIInterruptEvaluator:
    _CONTEXT_LOSS_MAP = {"DEEP_WORK": 0.8, "REASONING": 0.7, "GENERATING": 0.6, "WAITING": 0.0, "STUCK": 0.0}
    def evaluate_new_instruction(self, instruction, ai_state):
        state = ai_state.state; base_loss = self._CONTEXT_LOSS_MAP.get(state, 0.5)
        if state == "WAITING":
            return InterruptVerdict("execute_now", "AI idle", 0.0, 1, 0)
        if state == "STUCK":
            return InterruptVerdict("interrupt_current", "AI stuck", 0.0, 1, 0)
        category = self._categorise(instruction)
        # FATIGUE override: user says "keep going" - respect it
        if _KEEP_GOING.search(instruction.content):
            return InterruptVerdict("execute_now", "User overrides fatigue guard: keep going", 0.0, 1, 0)
        if category == "urgent":
            return InterruptVerdict("execute_now", "Urgent instruction", base_loss, 1, 0)
        if category == "hurry":
            return InterruptVerdict("reject", "Hurry with no content", 0.0, 5, 5, "AI is executing. Please wait.")
        if category == "redirect":
            estimated_loss = self._adjusted_loss(base_loss, ai_state)
            if estimated_loss < 0.5:
                return InterruptVerdict("interrupt_current", "Direction change", estimated_loss, 1, 0)
            else:
                return InterruptVerdict("queue", "Direction change but near done", estimated_loss, 1, 0,
                    notify_message="Current task near completion. Direction change queued.")
        if category == "parallel":
            return InterruptVerdict("queue", "Non-urgent parallel request", 0.0, 3, 0, "Request received, will process after current task.")
        if instruction.urgency_hint >= 0.8:
            return InterruptVerdict("execute_now", "High urgency hint", base_loss, 1, 0)
        return InterruptVerdict("queue", "General instruction while busy", 0.0, 3, 0, "Instruction queued.")
    def _categorise(self, instruction):
        text = instruction.content
        if instruction.urgency_hint >= 0.9 or _URGENT.search(text): return "urgent"
        if _HURRY.search(text.strip()): return "hurry"
        if _REDIRECT.search(text): return "redirect"
        if _PARALLEL.search(text): return "parallel"
        return "general"
    @staticmethod
    def _adjusted_loss(base_loss, state):
        progress_factor = min(1.0, state.duration_seconds / 120)
        return base_loss * (1 - progress_factor * 0.5)
