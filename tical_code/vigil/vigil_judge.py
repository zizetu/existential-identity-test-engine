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

# provenance:ticalasi-zzt-2026
"""
Guardian judge - the shared arbiter of the Vigil immune system.

The VigilJudge is the central decision-making component that evaluates
classified states (both human and AI) and produces actionable verdicts.
It serves as the single point of arbitration, ensuring that all intervention
decisions are consistent, cooldown-enforced, and traceable.

VERDICT SYSTEM:
    Instead of simple CLEAN/SUSPICIOUS/THREAT/DANGER labels, the judge
    produces a rich VigilVerdict with a specific action type:

        protect          - Suppress all interventions (human is in a protected
                           flow state: FOCUS, INSPIRATION, REST).
        notify           - Low-severity heads-up. Queued as a non-intrusive
                           notification; does not interrupt the human.
        prompt           - Medium-severity nudge. Sends a prompt message
                           but does not force interruption.
        interrupt        - High-severity intervention. Actively interrupts
                           the human's flow with a check-in message.
        alert_emergency  - Critical escalation. Sends an emergency message
                           AND starts a multi-retry escalation loop.
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional
from .state_classifier import StateResult
from .interrupt_evaluator import AIInterruptEvaluator, NewInstruction, InterruptVerdict
from .vigil_config import VigilCoreConfig

@dataclass
class InterventionRequest:
    requester: str; reason: str; urgency: float; proposed_action: str; timestamp: float = field(default_factory=time.time)

@dataclass
class VigilVerdict:
    action: str; target: str; confidence: float; reason: str; overruled_request: bool
    evidence: List[str]; cooldown_minutes: float; timestamp: float = field(default_factory=time.time)

class _CooldownTracker:
    def __init__(self): self._last = {}
    def is_cooling(self, action, cooldown_minutes): return (time.time() - self._last.get(action, 0.0)) < cooldown_minutes * 60
    def record(self, action): self._last[action] = time.time()

class VigilJudge:
    def __init__(self, config=None, ai_evaluator=None):
        self._cfg = config or VigilCoreConfig()
        self._ai_evaluator = ai_evaluator or AIInterruptEvaluator()
        self._cooldown = _CooldownTracker()
    def evaluate_intervention(self, request, state):
        return self._human_judge(state=state, request=request, proactive=False)
    def evaluate_proactive(self, state):
        return self._human_judge(state=state, request=None, proactive=True)
    def evaluate_ai_instruction(self, instruction, ai_state, human_state=None):
        if human_state is not None and human_state.state == "DISTRESS":
            return InterruptVerdict(action="execute_now", reason="Human DISTRESS overrides AI guardian", estimated_context_loss=1.0, queue_priority=1, cooldown_minutes=0)
        return self._ai_evaluator.evaluate_new_instruction(instruction, ai_state)
    def _human_judge(self, state, request, proactive):
        cfg = self._cfg; s = state.state; conf = state.confidence; dur = state.duration_minutes
        evidence = list(state.evidence)
        if s == "DISTRESS":
            action = "alert_emergency" if conf > 0.6 else "interrupt"
            return self._make_verdict(action, conf, f"DISTRESS (conf={conf:.2f})", request, evidence, 15)
        if s == "FATIGUE":
            if dur > 120: action, reason = "interrupt", f"Fatigue {dur:.0f}min"
            elif dur > 60: action, reason = "prompt", f"Fatigue {dur:.0f}min"
            else: action, reason = "notify", "Early fatigue"
            cd = cfg.cooldown_default_minutes
            if self._cooldown.is_cooling(action, cd):
                return self._make_verdict("protect", conf, f"In cooldown for {action}", request, evidence, cd)
            return self._make_verdict(action, conf, reason, request, evidence, cd)
        if s in cfg.protect_states:
            if request is None:
                return self._make_verdict("protect", conf, f"{s} - no intervention", request, evidence, cooldown=cfg.cooldown_default_minutes, overruled=False)
            urgency = request.urgency
            if urgency >= 0.8 and conf < 0.5:
                return self._make_verdict("notify", conf, f"Urgent but {s} low conf", request, evidence, cooldown=cfg.cooldown_default_minutes, overruled=True)
            return self._make_verdict("protect", conf, f"{s} protected", request, evidence, cooldown=cfg.cooldown_default_minutes, overruled=(urgency > 0))
        return self._make_verdict("notify", conf, "Unknown state", request, evidence, cooldown=cfg.cooldown_default_minutes)
    def _make_verdict(self, action, confidence, reason, request, evidence, cooldown, overruled=False):
        self._cooldown.record(action)
        return VigilVerdict(action=action, target="human", confidence=confidence, reason=reason,
            overruled_request=overruled, evidence=evidence, cooldown_minutes=cooldown)
