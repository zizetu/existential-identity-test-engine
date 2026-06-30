"""cognitive_layer.py - Cognitive protocol layer integration bridge with worker

Integrates the cognitive_protocol four-layer architecture into v0.10.0 worker:
  User input -> [L1 calibration] -> existing processing -> [L2 calibration] -> output
                              <-->
                         Metabolism (maintenance cycle)
                              <-->
                           Hive (collective aggregation)

3 integration points, minimal intrusion:
1. before call_ai: calibrate_human_input() -> calibrate user input
2. after call_ai: calibrate_ai_output() -> anti-hallucination check
3. maintenance: run_maintenance() -> metabolism+Hive

Does not replace HUMAN_PROMPT / PromptGenerator / call_ai signatures.
"""

import time
import json
import os
import logging
from typing import Any, Dict, List, Optional

from cognitive_protocol.signal import Signal, SignalSource, SignalQuality
from cognitive_protocol.protocol import SignalProtocol
from cognitive_protocol.metabolism import CognitiveMetabolism, MetabolismConfig
from cognitive_protocol.hive import HiveProtocol, HiveConfig, CollectivePattern

logger = logging.getLogger("tical-code.cognitive_layer")


class CognitiveLayer:
    """Cognitive protocol bridge layer

    Minimal integration: 3 methods exposed externally
    - calibrate_human_input(): user message calibration
    - calibrate_ai_output(): AI output calibration
    - run_maintenance(): metabolism+Hive
    """

    def __init__(self, action_threshold: float = 0.5,
                 data_dir: str = ""):
        self.protocol = SignalProtocol(action_threshold=action_threshold)

        meta_config = MetabolismConfig()
        if data_dir:
            meta_config.ARCHIVE_PATH = os.path.join(data_dir, "signal_archive")
        self.metabolism = CognitiveMetabolism(config=meta_config)

        hive_config = HiveConfig()
        if data_dir:
            hive_config.HIVE_DB_PATH = os.path.join(data_dir, "hive_collective.json")
        self.hive = HiveProtocol(config=hive_config)

        self._last_metabolism = time.time()
        self._stats = {"human_in": 0, "ai_out": 0, "actions_blocked": 0}

        # Load persisted state
        if data_dir:
            self.metabolism.load_state()
            self.hive.load_state()

    # --- Entry Point 1: User Input Calibration -----------------------------

    def calibrate_human_input(self, raw_text: str,
                              user_id: str = "default") -> Dict[str, Any]:
        """Calibrate human input, return calibration results for worker decisions

        Returns:
            {
                "signal": Signal,
                "should_process": bool,
                "fact": str,
                "fact_impact": float,
                "emotion_calibrated": float,
                "is_emotional_burst": bool,
            }
        """
        signal = self.protocol.ingest_human(raw_text, user_id)
        self.metabolism.register(signal)
        self._stats["human_in"] += 1

        trace = signal.calibration_trace
        is_burst = (
            trace.get("fact_clarity", 1.0) < 0.3
            and trace.get("emotion_raw", 0.0) > 0.6
        )

        result = {
            "signal": signal,
            "should_process": signal.can_act or not is_burst,
            "fact": trace.get("fact", raw_text),
            "fact_impact": trace.get("fact_impact", 0.3),
            "emotion_calibrated": trace.get("emotion_calibrated", 0.5),
            "is_emotional_burst": is_burst,
        }

        if is_burst:
            logger.info(
                f"[CognitiveLayer] Emotional burst from {user_id}, downgrading")

        return result

    # --- Entry Point 2: AI Output Calibration ------------------------------

    def calibrate_ai_output(self, ai_output: str,
                            evidence: Optional[Dict] = None,
                            source: SignalSource = SignalSource.AI_REASONING
                            ) -> Dict[str, Any]:
        """Calibrate AI output, anti-hallucination

        Returns:
            {
                "signal": Signal,
                "should_send": bool,
                "confidence": float,
                "hallucination_risk": float,
                "warning": str,
            }
        """
        signal = self.protocol.ingest_ai(ai_output, source, evidence)
        self.metabolism.register(signal)
        self._stats["ai_out"] += 1

        trace = signal.calibration_trace
        factors = trace.get("confidence_factors", {})
        hall_risk = factors.get("hallucination_risk", 0.0)
        promise_risk = factors.get("promise_risk", 0.0)

        warning = ""
        if hall_risk > 0.2:
            warning = f"High hallucination risk ({hall_risk:.2f}): claims completion but no evidence"
        elif promise_risk > 0.1:
            warning = f"Contains future promise ({promise_risk:.2f}): requires execution verification"

        # Don't block sending, but flag risk
        should_send = True
        if not signal.can_act and hall_risk > 0.3:
            warning += " [requires verification before trust]"

        result = {
            "signal": signal,
            "should_send": should_send,
            "confidence": signal.confidence,
            "hallucination_risk": hall_risk,
            "warning": warning,
        }

        if warning:
            self._stats["actions_blocked"] += 1
            logger.info(f"[CognitiveLayer] AI flagged: {warning[:60]}")

        return result

    # --- Entry Point 3: Metabolism Maintenance -----------------------------

    def run_maintenance(self, force: bool = False) -> Dict[str, Any]:
        """Run metabolism + Hive during maintenance cycle

        Returns:
            {"metabolism": {...}, "hive_patterns": int, "hive_stats": {...}}
        """
        now = time.time()
        interval = self.metabolism.config.METABOLISM_INTERVAL

        if force or (now - self._last_metabolism) > interval:
            meta_result = self.metabolism.run_cycle(now)
            self._last_metabolism = now
        else:
            meta_result = {"active": len(self.metabolism._signals), "archived": 0}

        # Hive pattern discovery
        new_patterns = self.hive.discover_patterns()

        # Persist
        try:
            self.metabolism.save_state()
            self.hive.save_state()
        except Exception as e:
            logger.warning(f"[CognitiveLayer] save_state failed: {e}")

        return {
            "metabolism": meta_result,
            "hive_patterns": len(new_patterns),
            "hive_stats": self.hive.get_hive_stats(),
        }

    # --- Verification Interface -----------------------------------------

    def verify_signal(self, signal_id: str, success: bool = True
                      ) -> Optional[Signal]:
        """Verify signal - backfill worker execution result"""
        return self.metabolism.verify(signal_id, success)

    def submit_to_hive(self, signal: Signal, agent_id: str = "agent") -> Optional[str]:
        """Submit signal to Hive"""
        return self.hive.submit(signal, agent_id)

    def query_hive(self, keyword: str) -> List[CollectivePattern]:
        """Query collective intelligence"""
        return self.hive.query(keyword)

    # --- Integration with essence/distillation ---------------------------

    def distill_high_impact(self, signal: Signal, project_id: str = ""
                            ) -> Optional[str]:
        """Feed high-impact Signal into essence distillation"""
        trace = signal.calibration_trace
        impact = trace.get("fact_impact", 0.0)
        if impact < 0.6 or signal.confidence < 0.5:
            return None

        EssenceEngine = None
        try:
            from essence.engine import EssenceEngine
        except ImportError as e:
            logger.warning("EssenceEngine import failed: %s, cognitive layer running in degraded mode", e)
        except Exception as e:
            logger.error("EssenceEngine initialization exception: %s", e)

        if EssenceEngine is None:
            return None

        try:
            engine = EssenceEngine()
            return engine.distill(
                raw_text=str(signal.payload),
                project_id=project_id,
                impact=impact,
            )
        except Exception as e:
            logger.warning("Cognitive layer distillation failed: %s", e)
            return None

    # --- Integration with benchmarks -------------------------------------

    def record_benchmark_result(self, task_id: str, passed: bool,
                                score: float) -> Signal:
        """Record benchmark test result as verification signal"""
        signal = self.protocol.ingest_ai(
            f"benchmark: {task_id} {'PASS' if passed else 'FAIL'} (score={score})",
            source=SignalSource.VERIFICATION,
            evidence={"has_test_passed": passed, "has_actual_result": True},
        )
        signal = signal.verify(success=passed)
        self.metabolism.register(signal)
        return signal

    # --- Statistics -----------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "metabolism": self.metabolism.get_metabolism_stats(),
            "hive": self.hive.get_hive_stats(),
            "action_threshold": self.protocol.action_threshold,
        }
