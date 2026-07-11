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

"""Vigil - Quiet watcher for EITElite.
Public API: build_vigil(), Vigil.patrol(), Vigil.evaluate_instruction()
"""
from .vigil_config import VigilConfig, load_config
from .signal_collector import SignalCollector, CombinedSignal, InteractionSignal, PhysioSignal
from .ai_signal_collector import AISignalCollector, AISignal
from .state_classifier import StateClassifier, StateResult, StateRecord
from .ai_state_classifier import AIStateClassifier, AIStateResult
from .vigil_judge import VigilJudge, VigilVerdict, InterventionRequest
from .interrupt_evaluator import AIInterruptEvaluator, NewInstruction, InterruptVerdict
from .instruction_queue import InstructionQueue, QueuedInstruction
from .trace_log import VigilTraceStore, VigilTrace
from .actions import VigilActions
import time
from typing import Optional, List


class Vigil:
    """Top-level facade - patrol() and evaluate_instruction() entry points."""

    def __init__(self, config=None, send_message=None, smtp_config=None, trace_log_path=None):
        self.config = config or load_config()
        self.signal_collector = SignalCollector()
        self.ai_signal_collector = AISignalCollector()
        self.state_classifier = StateClassifier(self.config.classifier)
        self.ai_state_classifier = AIStateClassifier()
        self.judge = VigilJudge(self.config.guardian)
        self.ai_evaluator = AIInterruptEvaluator()
        self.instruction_queue = InstructionQueue()
        self.trace = VigilTraceStore(log_path=trace_log_path)
        self.actions = VigilActions(config=self.config.guardian, send_message=send_message, smtp_config=smtp_config)
        self._state_history: List[StateRecord] = []

    async def patrol(self) -> None:
        """Human-guardian proactive sweep + AI stuck detection (call every 5 min)."""
        signal = self.signal_collector.collect()
        state = self.state_classifier.classify(signal, self._state_history)
        verdict = self.judge.evaluate_proactive(state)
        trace_id = self.trace.record(state=state, verdict=verdict, physio_available=(signal.physio is not None))
        await self.actions.execute(verdict, trace_id=trace_id)
        self._state_history.append(StateRecord(state.state, state.confidence, time.time()))
        if len(self._state_history) > 200:
            self._state_history = self._state_history[-200:]
        if self.ai_signal_collector.is_stuck():
            ai_signal = self.ai_signal_collector.collect()
            ai_state = self.ai_state_classifier.classify(ai_signal)
            if ai_state.state == "STUCK":
                self.ai_signal_collector.task_completed()
                await self.actions._send("AI execution detected stuck, auto-interrupted.")
        expired = self.instruction_queue.cleanup_expired()
        if expired:
            await self.actions._send(f"{len(expired)} queued instructions expired and cleaned.")

    def evaluate_instruction(self, instruction: NewInstruction, human_state: Optional[StateResult] = None) -> InterruptVerdict:
        ai_signal = self.ai_signal_collector.collect()
        ai_state = self.ai_state_classifier.classify(ai_signal)
        return self.judge.evaluate_ai_instruction(instruction=instruction, ai_state=ai_state, human_state=human_state)

    async def ack_emergency(self, trace_id: str) -> None:
        await self.actions.handle_ack(trace_id)
        self.trace.update_outcome(trace_id, human_response="acknowledged")


def build_vigil(config_path=None, send_message=None, smtp_config=None):
    config = load_config(config_path)
    return Vigil(config=config, send_message=send_message, smtp_config=smtp_config)


__all__ = [
    "Vigil", "build_vigil", "VigilConfig", "load_config",
    "SignalCollector", "CombinedSignal", "InteractionSignal", "PhysioSignal",
    "AISignalCollector", "AISignal",
    "StateClassifier", "StateResult", "StateRecord",
    "AIStateClassifier", "AIStateResult",
    "VigilJudge", "VigilVerdict", "InterventionRequest",
    "AIInterruptEvaluator", "NewInstruction", "InterruptVerdict",
    "InstructionQueue", "QueuedInstruction",
    "VigilTraceStore", "VigilTrace",
    "VigilActions",
]
