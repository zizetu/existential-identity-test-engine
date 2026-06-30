"""SignalProtocol — Cognitive Calibration Protocol

All cognitive data must pass through SignalProtocol to enter the system.
Data that doesn't pass the protocol doesn't exist in the system, just as packets that don't pass IP don't exist on the internet.

Dual-layer calibration:
- Layer 1: Human signal calibration — when a human says X, what is the real signal?
- Layer 2: AI signal calibration — when AI thinks Y, is that thought reliable?

The two directions have different noise sources, but the calibration logic is isomorphic:
infer latent reality from noisy observations.
"""

import time
import math
import re
import json
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from cognitive_protocol.signal import (
    Signal, SignalSource, SignalQuality, SOURCE_PRIOR
)


# ============ Layer 1: Human Signal Calibration ============

class HumanSignalCalibrator:
    """Layer 1: Extract calibrated signal from human input

    Core formula: Signal = FactImpact × Confidence
    Emotion ≠ Impact

    Human feedback is a noisy observation, not a label.
    Calibration goal: infer latent reality from feedback.
    """

    # Emotion intensity keywords (Chinese)
    _EMOTION_AMPLIFIERS = [
        r"damn", r"fuck", r"fuckoff", r"useless", r"garbage", r"stupid", r"idiot",
        r"！！", r"!!!", r"？？", r"\?\?\?",
    ]
    _EMOTION_DAMPENERS = [
        r"maybe", r"perhaps", r"probably", r"seems", r"slightly",
    ]

    # Fact keywords (mark high-impact facts)
    _FACT_MARKERS = [
        r"hallucination", r"fabricated", r"not done", r"not completed", r"fake",
        r"error", r"bug", r"failure", r"crash", r"leaked",
        r"security", r"privacy", r"password", r"secret key",
    ]

    def calibrate(self, raw_text: str, user_id: str = "default",
                  user_baseline: Optional[Dict] = None) -> Dict[str, Any]:
        """Calibrate human input signal

        Returns:
            {
                "fact": str,              # extracted fact
                "fact_impact": float,     # fact impact 0-1
                "emotion_raw": float,     # raw emotion intensity 0-1
                "emotion_calibrated": float,  # calibrated emotion 0-1
                "fact_clarity": float,    # fact clarity 0-1
                "confidence": float,      # signal confidence 0-1
            }
        """
        if not raw_text or not raw_text.strip():
            return self._empty_signal()

        # 1. Emotion intensity assessment
        emotion_raw = self._assess_emotion(raw_text)

        # 2. User baseline calibration
        emotion_calibrated = self._apply_baseline(
            emotion_raw, user_baseline or {}
        )

        # 3. Fact extraction and impact assessment
        fact, fact_impact, fact_clarity = self._extract_fact(raw_text)

        # 4. Signal confidence = fact_clarity × (1 - emotion_noise)
        # Stronger emotion = more noise, but impact is not determined by emotion
        emotion_noise = abs(emotion_calibrated - 0.5) * 0.4
        confidence = fact_clarity * (1.0 - emotion_noise)

        # 5. emotional_burst downgrade: high emotion + low clarity → force impact downgrade
        if fact_clarity < 0.3 and emotion_raw > 0.6:
            fact_impact = min(fact_impact, 0.1)
            confidence = 0.2

        return {
            "fact": fact,
            "fact_impact": min(1.0, fact_impact),
            "emotion_raw": round(emotion_raw, 4),
            "emotion_calibrated": round(emotion_calibrated, 4),
            "fact_clarity": round(fact_clarity, 4),
            "confidence": round(max(0.05, min(1.0, confidence)), 4),
        }

    def _assess_emotion(self, text: str) -> float:
        """Assess raw emotion intensity"""
        score = 0.3  # baseline
        for pattern in self._EMOTION_AMPLIFIERS:
            if re.search(pattern, text):
                score += 0.2
        for pattern in self._EMOTION_DAMPENERS:
            if re.search(pattern, text):
                score -= 0.1
        # Punctuation intensity
        excl = text.count("！") + text.count("!")
        quest = text.count("？") + text.count("?")
        if excl > 2:
            score += 0.1
        if quest > 2:
            score += 0.05
        return max(0.0, min(1.0, score))

    def _apply_baseline(self, emotion_raw: float,
                        baseline: Dict) -> float:
        """Calibrate emotion based on user baseline"""
        b_min = baseline.get("emotion_min", 0.0)
        b_max = baseline.get("emotion_max", 1.0)
        if b_max <= b_min:
            b_max = b_min + 1.0
        calibrated = (emotion_raw - b_min) / (b_max - b_min)
        return max(0.0, min(1.0, calibrated))

    def _extract_fact(self, text: str) -> Tuple[str, float, float]:
        """Extract fact, assess impact and clarity"""
        fact_impact = 0.3  # default low impact
        fact_clarity = 0.5  # default medium clarity

        # Check fact markers
        matched_facts = []
        for pattern in self._FACT_MARKERS:
            if re.search(pattern, text.lower()):
                matched_facts.append(pattern)
                fact_impact += 0.15

        # High-impact domains
        high_impact_keywords = ["security", "privacy", "password", "secret key", "leaked", "hallucination"]
        for kw in high_impact_keywords:
            if kw in text:
                fact_impact += 0.1

        # Clarity assessment
        text_len = len(text)
        if text_len > 20:
            fact_clarity += 0.1
        if matched_facts:
            fact_clarity += 0.2
        if "you" in text and ("should" in text or "must" in text or "don't" in text):
            fact_clarity += 0.1  # directive statement, high clarity

        # Pure emotion penalty: many emotion keywords but few fact keywords → clarity drops
        emotion_hits = sum(1 for p in self._EMOTION_AMPLIFIERS if re.search(p, text))
        if emotion_hits > 1 and not matched_facts:
            fact_clarity -= 0.3  # multiple emotion words + zero facts = pure emotional burst

        fact = text[:100]  # truncate first 100 chars as fact description
        return fact, min(1.0, fact_impact), min(1.0, fact_clarity)

    def _empty_signal(self) -> Dict[str, Any]:
        return {
            "fact": "",
            "fact_impact": 0.0,
            "emotion_raw": 0.0,
            "emotion_calibrated": 0.5,
            "fact_clarity": 0.0,
            "confidence": 0.0,
        }


# ============ Layer 2: AI Signal Calibration ============

class AISignalCalibrator:
    """Layer 2: Calibrate AI internal output signals

    AI internal signals are also noisy:
    - "I completed it" — could be hallucination, confidence is not 1.0
    - Retrieved a rule from memory — could be outdated/corrupted by compression
    - Decided to use a subtask — the judgment itself could be wrong

    Core principle: Every internal AI output should carry a confidence label.
    Insufficient confidence — don't act, verify first.
    """

    def calibrate(self, ai_output: str, source: SignalSource,
                  evidence: Optional[Dict] = None) -> Dict[str, Any]:
        """Calibrate AI internal signal

        Args:
            ai_output: AI output content
            source: Signal source type
            evidence: Evidence supporting this output

        Returns:
            {
                "output": str,
                "confidence": float,
                "confidence_factors": Dict,
                "should_act": bool,
            }
        """
        evidence = evidence or {}

        # 1. Source prior
        prior = SOURCE_PRIOR.get(source, 0.3)

        # 2. Evidence boost
        evidence_boost = 0.0
        if evidence.get("has_actual_result"):
            evidence_boost += 0.2  # has actual execution result
        if evidence.get("has_user_confirmation"):
            evidence_boost += 0.15  # user confirmation
        if evidence.get("has_test_passed"):
            evidence_boost += 0.2  # test passed
        if evidence.get("has_file_output"):
            evidence_boost += 0.1  # has file output

        # 3. Hallucination risk downgrade
        hallucination_risk = 0.0
        hallucination_keywords = ["completed", "already", "success", "all done"]
        for kw in hallucination_keywords:
            if kw in ai_output and not evidence.get("has_actual_result"):
                hallucination_risk += 0.15  # claims completion without evidence

        # 4. Promise statement additional risk
        promise_risk = 0.0
        promise_keywords = ["I will", "next", "immediately", "about to"]
        for kw in promise_keywords:
            if kw in ai_output:
                promise_risk += 0.1  # future promise, discount confidence

        # 5. Final confidence
        confidence = prior + evidence_boost - hallucination_risk - promise_risk
        confidence = max(0.05, min(1.0, confidence))

        # 6. Action threshold
        should_act = confidence >= 0.5

        return {
            "output": ai_output,
            "confidence": round(confidence, 4),
            "confidence_factors": {
                "prior": prior,
                "evidence_boost": round(evidence_boost, 4),
                "hallucination_risk": round(hallucination_risk, 4),
                "promise_risk": round(promise_risk, 4),
            },
            "should_act": should_act,
        }


# ============ SignalProtocol: Protocol Main Entry ============

class SignalProtocol:
    """Cognitive calibration protocol — all data flows must pass through

    Usage:
        protocol = SignalProtocol()

        # Layer 1: Human input
        signal = protocol.ingest_human("why the hell are you hallucinating again")

        # Layer 2: AI output
        signal = protocol.ingest_ai("I have completed the deployment", source=AI_REASONING)

        # Pre-action check
        if signal.can_act:
            do_something()
        else:
            gather_more_evidence()

    Data that doesn't pass the protocol doesn't exist in the system.
    """

    def __init__(self, action_threshold: float = 0.5):
        self.action_threshold = action_threshold
        self._human_calibrator = HumanSignalCalibrator()
        self._ai_calibrator = AISignalCalibrator()
        self._user_baselines: Dict[str, Dict] = {}

    def ingest_human(self, raw_text: str, user_id: str = "default",
                     metadata: Optional[Dict] = None) -> Signal:
        """Layer 1: Ingest human input, return calibrated Signal

        Human feedback → emotion/fact separation → baseline calibration → Signal
        """
        calibration = self._human_calibrator.calibrate(
            raw_text, user_id, self._user_baselines.get(user_id)
        )

        # Empty input → confidence=0, don't use source prior
        if calibration["confidence"] == 0.0:
            forced_confidence = 0.0
        else:
            forced_confidence = calibration["confidence"]

        # Build Signal
        signal = Signal(
            payload=raw_text,
            source=SignalSource.HUMAN_INPUT,
            confidence=forced_confidence,
            metadata=metadata or {},
            calibration_trace={
                "layer": 1,
                "fact": calibration["fact"],
                "fact_impact": calibration["fact_impact"],
                "emotion_raw": calibration["emotion_raw"],
                "emotion_calibrated": calibration["emotion_calibrated"],
                "fact_clarity": calibration["fact_clarity"],
            },
        )

        # Update user baseline
        self._update_baseline(user_id, calibration["emotion_raw"])

        return signal

    def ingest_ai(self, ai_output: str, source: SignalSource = SignalSource.AI_REASONING,
                  evidence: Optional[Dict] = None,
                  metadata: Optional[Dict] = None) -> Signal:
        """Layer 2: Ingest AI internal output, return calibrated Signal

        AI reasoning → hallucination risk assessment → evidence verification → Signal
        """
        calibration = self._ai_calibrator.calibrate(
            ai_output, source, evidence
        )

        signal = Signal(
            payload=ai_output,
            source=source,
            confidence=calibration["confidence"],
            metadata=metadata or {},
            calibration_trace={
                "layer": 2,
                "confidence_factors": calibration["confidence_factors"],
                "should_act": calibration["should_act"],
            },
        )

        return signal

    def ingest_memory(self, content: str, memory_type: str = "raw",
                      verified: bool = False,
                      metadata: Optional[Dict] = None) -> Signal:
        """Ingest memory data, return calibrated Signal

        Memory → source determination → confidence assignment → Signal
        """
        # Memory source mapping
        source_map = {
            "essence": SignalSource.ESSENCE,
            "axiom": SignalSource.ESSENCE,
            "scar": SignalSource.ESSENCE,
            "verified": SignalSource.VERIFICATION,
            "raw": SignalSource.MEMORY,
        }
        source = source_map.get(memory_type, SignalSource.MEMORY)

        # Base confidence
        base_conf = SOURCE_PRIOR[source]
        if verified:
            base_conf = min(1.0, base_conf + 0.2)

        signal = Signal(
            payload=content,
            source=source,
            confidence=base_conf,
            metadata=metadata or {},
            calibration_trace={
                "layer": 2,
                "memory_type": memory_type,
                "verified": verified,
            },
        )

        return signal

    def check_action(self, signal: Signal) -> bool:
        """Action threshold check — Signal below threshold cannot trigger action"""
        return signal.confidence >= self.action_threshold

    def _update_baseline(self, user_id: str, emotion_raw: float):
        """EMA update user emotion baseline"""
        if user_id not in self._user_baselines:
            self._user_baselines[user_id] = {
                "emotion_min": 0.0,
                "emotion_max": 1.0,
            }
        baseline = self._user_baselines[user_id]
        alpha = 0.05  # slow update
        if emotion_raw > baseline["emotion_max"]:
            baseline["emotion_max"] = (
                (1 - alpha) * baseline["emotion_max"] + alpha * emotion_raw
            )
        if emotion_raw < baseline["emotion_min"]:
            baseline["emotion_min"] = (
                (1 - alpha) * baseline["emotion_min"] + alpha * emotion_raw
            )
