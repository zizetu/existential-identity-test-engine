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
"""State Classifier - Shared Vigil alert classifier.
Human state classifier for the Vigil guardian layer.

This module maps raw CombinedSignals (interaction + physio) into classified
human states with confidence scores and supporting evidence. It uses a
deterministic rule-based pipeline - no ML model is involved.

STATE SPACE (in order of evaluation priority):

    1. DISTRESS - Highest priority. Triggered by:
       - Abnormal physiological readings (HRV < 20ms, HR < 45 or > 120 bpm,
         SpO2 < 94%, EDA spike > 20 uS) - confidence 0.85
       - Prolonged silence (> 30 min) without physio data and not during
         typical rest hours - confidence 0.35
       - Excessive rest (> 8 hours) during non-rest hours - confidence 0.25

    2. FATIGUE - Triggered when:
       - Consecutive work exceeds fatigue_work_hours (default 4h) AND
         error rate > 15% OR response lengths declining OR task switching > 5/hr
       - HRV below 30ms
       - Confidence scales: 0.5 + (duration_in_state / 120), capped at 0.9

    3. REST - Good silence during rest hours (22:00-07:00) or prolonged gaps
       with low variance. Confidence 0.75.

    4. FOCUS / FLOW / INSPIRATION / UNCERTAIN - additional states for
       nuanced human state tracking.
"""
import time, datetime
from dataclasses import dataclass, field
from typing import List, Optional
from .signal_collector import CombinedSignal, InteractionSignal, PhysioSignal
from .vigil_config import ClassifierConfig

@dataclass
class StateResult:
    state: str; confidence: float; evidence: List[str]; duration_minutes: float

@dataclass
class StateRecord:
    state: str; confidence: float; timestamp: float = field(default_factory=time.time)

class StateClassifier:
    def __init__(self, config: Optional[ClassifierConfig] = None):
        self._cfg = config or ClassifierConfig()
    def classify(self, signal: CombinedSignal, history: List[StateRecord]) -> StateResult:
        ia = signal.interaction; ph = signal.physio; evidence = []
        gap_minutes = self._input_gap_minutes(ia); is_silent = gap_minutes > 10
        # DISTRESS
        if ph is not None and ph.source != "none":
            distress, d_ev = self._check_physio_distress(ph, ia, gap_minutes)
            if distress:
                evidence += d_ev; dur = self._duration_in_current_state(history)
                return StateResult("DISTRESS", 0.85, evidence, dur)
        else:
            if is_silent and gap_minutes >= self._cfg.distress_no_signal_gap and not self._looks_like_rest_time(ia):
                evidence.append(f"input_gap_{gap_minutes:.0f}min_no_physio")
                return StateResult("DISTRESS", 0.35, evidence, self._duration_in_current_state(history))
        # FATIGUE
        if self._is_fatigue(ia, ph):
            evidence += self._fatigue_evidence(ia, ph); dur = self._duration_in_current_state(history)
            conf = min(0.9, 0.5 + dur / 120)
            return StateResult("FATIGUE", conf, evidence, dur)
        # Good silence
        if is_silent:
            state, conf, ev = self._classify_good_silence(ia, ph, gap_minutes, history)
            evidence += ev; return StateResult(state, conf, evidence, self._duration_in_current_state(history))
        # Active
        state, conf, ev = self._classify_active(ia, ph, history)
        evidence += ev; dur = self._duration_in_current_state(history)
        if state in ("FOCUS", "INSPIRATION") and ia.consecutive_work_hours > self._cfg.focus_max_hours:
            evidence.append(f"focus_too_long_{ia.consecutive_work_hours:.1f}h")
            return StateResult("FATIGUE", 0.65, evidence, dur)
        return StateResult(state, conf, evidence, dur)
    def _check_physio_distress(self, ph, ia, gap_minutes):
        evidence = []
        if ph.hrv > 0 and ph.hrv < 20: evidence.append(f"hrv_critically_low_{ph.hrv:.0f}ms")
        if ph.heart_rate > 0 and (ph.heart_rate > 120 or ph.heart_rate < 45): evidence.append(f"hr_abnormal_{ph.heart_rate:.0f}bpm")
        if ph.spo2 > 0 and ph.spo2 < 94: evidence.append(f"spo2_low_{ph.spo2:.0f}pct")
        if ph.eda > 20: evidence.append(f"eda_spike_{ph.eda:.1f}uS")
        if gap_minutes > 30 and not self._looks_like_rest_time(ia): evidence.append(f"input_gap_{gap_minutes:.0f}min")
        spo2_alone = any("spo2" in e for e in evidence)
        return (spo2_alone or len(evidence) >= 2), evidence
    def _is_fatigue(self, ia, ph):
        if ia.consecutive_work_hours >= self._cfg.fatigue_work_hours:
            if ia.input_error_rate > 0.15 or ia.response_length_trend == "decreasing" or ia.task_switch_frequency > 5:
                return True
        if ph and ph.hrv > 0 and ph.hrv < 30: return True
        return False
    def _fatigue_evidence(self, ia, ph):
        ev = []
        if ia.consecutive_work_hours >= self._cfg.fatigue_work_hours: ev.append(f"work_{ia.consecutive_work_hours:.1f}h")
        if ia.input_error_rate > 0.15: ev.append(f"error_rate_{ia.input_error_rate:.0%}")
        if ia.response_length_trend == "decreasing": ev.append("response_length_decreasing")
        if ia.task_switch_frequency > 5: ev.append(f"task_switches_{ia.task_switch_frequency:.0f}/h")
        if ph and ph.hrv > 0 and ph.hrv < 30: ev.append(f"hrv_low_{ph.hrv:.0f}ms")
        return ev
    def _classify_good_silence(self, ia, ph, gap_minutes, history):
        evidence = []
        is_rest_hours = self._looks_like_rest_time(ia)
        low_variance = ia.input_interval_variance < 5.0
        if is_rest_hours or (gap_minutes > 20 and low_variance):
            evidence.append(f"gap_{gap_minutes:.0f}min")
            if is_rest_hours: evidence.append("rest_time_of_day")
            if ia.consecutive_work_hours == 0 and gap_minutes > self._cfg.rest_max_hours * 60:
                evidence.append(f"rest_too_long_{gap_minutes/60:.1f}h")
                return "DISTRESS", 0.25, evidence
            return "REST", 0.75, evidence
        if ia.response_length_trend == "increasing":
            evidence.append("response_length_increasing"); evidence.append(f"gap_{gap_minutes:.0f}min")
            return "INSPIRATION", 0.7, evidence
        evidence.append(f"gap_{gap_minutes:.0f}min")
        evidence.append("low_variance" if low_variance else "moderate_variance")
        return "FOCUS", 0.65, evidence
    def _classify_active(self, ia, ph, history):
        evidence = []
        if ia.input_interval_variance > 50 and ia.response_length_trend == "increasing" and ia.task_switch_frequency < 2:
            evidence += ["bursty_input", "response_length_increasing", "low_task_switches"]
            return "INSPIRATION", 0.75, evidence
        if ia.input_interval_variance < 20 and ia.input_error_rate < 0.1 and ia.task_switch_frequency < 2:
            evidence += ["steady_input", "low_error_rate", "low_task_switches"]
            return "FOCUS", 0.8, evidence
        evidence.append("normal_activity")
        return "FOCUS", 0.55, evidence
    @staticmethod
    def _input_gap_minutes(ia): return (time.time() - ia.last_input_time) / 60.0 if ia.last_input_time > 0 else 0.0
    @staticmethod
    def _looks_like_rest_time(ia): return datetime.datetime.now().hour < 7 or datetime.datetime.now().hour >= 22
    @staticmethod
    def _duration_in_current_state(history):
        if not history: return 0.0
        last_state = history[-1].state; duration = 0.0
        for record in reversed(history):
            if record.state == last_state: duration = (time.time() - record.timestamp) / 60.0
            else: break
        return duration
