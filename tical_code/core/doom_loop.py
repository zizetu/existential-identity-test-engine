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

"""
Doom Loop Detection - AgentLoop/stagnantdetect
==========================================

Detect agent stuck in tool call loops and provide automatic recovery strategies.

Core design:
- Sliding window signature matching with four orthogonal detectors covering different loop patterns
- Adaptive threshold: dynamically adjust detection sensitivity based on tool call frequency
- Auto-recovery after detection: switch tool/params, rollback N steps, degrade model, force summary
- Cross-agent loop detection: multi-agent collaboration when A→B→A round-trip loop
- semantic similarity judgment: not only detects identical content, also detects "same substance, different surface"

four types of detectors:
1. generic_repeat - same(Tool,Parameter)repeatcall
2. poll_no_progress - same-args-same-result polling (no substantive progress)
3. ping_pong - A→B→A→Balternating-oscillation
4. cross_agent - cross-agent loop calls

Security design (Audit fix):
- Bounded state: detector short-circuit logic, return on CRITICAL
- Thread-safe: all state operations protected by threading.Lock
- State reset mechanism: clear detector internal state after recovery
- Detector result cache: same batch of records not recomputed

Design principles:
- Default close, explicit enable (production-grade posture)
- Zero external dependencies, pure stdlib
- each agent has independent detector instance, no global state
- detection and recovery strategy decoupled, independently configurable

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Constants and enums
# =============================================================================

class LoopLevel(Enum):
    """Loop detection severity level."""
    NORMAL = "normal"       # normal, no loop
    WARNING = "warning"     # warning, may be stuck in loop
    CRITICAL = "critical"   # Severe, must interrupt


class RecoveryAction(Enum):
    """Recovery action after loop detection."""
    NONE = "none"                               # no needrecover
    RETRY_DIFFERENT_ARGS = "retry_different_args"  # retry with different params
    SWITCH_TOOL = "switch_tool"                 # switch tool
    ROLLBACK_STEPS = "rollback_steps"           # Rollback N steps
    DOWNGRADE_MODEL = "downgrade_model"         # Degrademodel
    FORCE_SUMMARIZE = "force_summarize"         # Force summarycurrentstatus


class DetectorType(Enum):
    """Detector type."""
    GENERIC_REPEAT = "generic_repeat"       # genericRepetition detection
    POLL_NO_PROGRESS = "poll_no_progress"   # poll-no-progress
    PING_PONG = "ping_pong"                 # alternating-oscillation
    CROSS_AGENT = "cross_agent"             # cross-agent loop


# =============================================================================
# Data structure
# =============================================================================

@dataclass
class ToolCallRecord:
    """Single tool call record.

    Attributes:
        tool_name: Tool name
        args_hash: deterministic hash of parameters (sorted JSON serialize + SHA-256)
        result_hash: deterministic hash of result (backfilled after execution)
        result_text: Resulttext(used forsemanticsimilarityjudge)
        timestamp: calltimestamp
        agent_id: caller agent ID (used for cross-agent detection)
    """
    tool_name: str
    args_hash: str
    fuzzy_hash: str = ""   # Fuzzy hash for near-duplicate detection (bash commands)
    result_hash: str = ""
    result_text: str = ""
    timestamp: float = field(default_factory=time.time)
    agent_id: str = ""


@dataclass
class LoopDetectionResult:
    """Loop detection result.

    Attributes:
        stuck: whetherstuckLoop
        level: severelevel
        detector: triggering detector type
        count: repeat/oscillationcount
        message: humanreadablemessage
        recovery: recommended recovery action
        details: detail info (for audit and trace)
    """
    stuck: bool = False
    level: LoopLevel = LoopLevel.NORMAL
    detector: Optional[DetectorType] = None
    count: int = 0
    message: str = ""
    recovery: RecoveryAction = RecoveryAction.NONE
    details: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            'stuck': self.stuck,
            'level': self.level.value,
            'detector': self.detector.value if self.detector else None,
            'count': self.count,
            'message': self.message,
            'recovery': self.recovery.value,
            'details': self.details,
        }


@dataclass
class DoomLoopConfig:
    """Loop detection configuration.

    base threshold + adaptive adjustment strategy:
    - high-freq calls (>1 per second) → reduce threshold (faster loop detection)
    - low-freq calls (<0.1 per second) → increase threshold (avoid false alarms)

    Attributes:
        enabled: whetherenabledetect(Defaultclose)
        history_size: slidingwindowsize
        warn_threshold_base: warning threshold base value
        critical_threshold_base: critical threshold base value
        adaptive_enabled: whetherenableadaptivethreshold
        cross_agent_enabled: whether to enable cross-agent detection
        semantic_similarity_enabled: whetherenablesemanticsimilarityjudge
        semantic_threshold: semantic similarity threshold (0-1, above this treated as "same")
        recovery_enabled: whetherenableAuto-recovery
    """
    enabled: bool = True
    history_size: int = 30
    warn_threshold_base: int = 2
    critical_threshold_base: int = 3
    adaptive_enabled: bool = True
    cross_agent_enabled: bool = True
    semantic_similarity_enabled: bool = True
    semantic_threshold: float = 0.85
    recovery_enabled: bool = True


# =============================================================================
# coreToolfunction
# =============================================================================

def _deterministic_hash(data: Any) -> str:
    """Compute deterministic hash of data.

    Uses sorted JSON serialize to ensure same data always produces same signature,
    unaffected by dict traversal order.

    Args:
        data: data to hash (dict/list/str/number/bool/None)

    Returns:
        SHA-256 hex digest
    """
    try:
        serialized = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(data)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _fuzzy_args_hash(tool_name: str, args: Dict[str, Any]) -> str:
    """Fuzzy hash for near-duplicate detection - strips variable parts from bash commands.

    For 'bash' tool calls, normalizes the command by stripping:
    - timeout values (timeout N, timeout Ns, timeout=N)
    - heredoc content (everything after << 'PYEOF' or << 'EOF')

    Other tools use exact hash (fallback to _deterministic_hash).
    """
    if tool_name != "bash":
        return _deterministic_hash(args)

    cmd = args.get("command", "")
    if not cmd:
        return _deterministic_hash(args)

    import re
    # Normalize: strip timeout prefixes
    normalized = re.sub(r'\btimeout\s+\d+s?\b', 'timeout X', cmd)
    normalized = re.sub(r'\btimeout=\d+\b', 'timeout=X', normalized)
    # Strip heredoc content - keep only the marker
    normalized = re.sub(r"(<<\s*'?PYEOF'?\s*\n).*", r'\1[...heredoc...]', normalized, flags=re.DOTALL)
    normalized = re.sub(r"(<<\s*'?EOF'?\s*\n).*", r'\1[...heredoc...]', normalized, flags=re.DOTALL)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _text_similarity(text_a: str, text_b: str) -> float:
    """Compute semantic similarity of two text segments (based on Jaccard word set).

    Not only detects fully identical text, but also "same substance, different surface" cases,
    e.g. poll results that differ only in timestamp but have the same core content.

    Optimized for Chinese: uses character bigram tokenization rather than space tokenization.

    Args:
        text_a: first segment of text
        text_b: second segment of text

    Returns:
        similarity [0.0, 1.0]
    """
    if not text_a or not text_b:
        return 0.0

    def _tokenize(text: str) -> set:
        """Mixed Chinese-English tokenize: Chinese bigrams + English words."""
        tokens = set()
        # Englishword
        words = text.lower().split()
        tokens.update(f"w:{w}" for w in words if len(w) > 1)
        # ChineseCharacterbigram
        cjk_chars = []
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                cjk_chars.append(ch)
        for i in range(len(cjk_chars) - 1):
            tokens.add(f"c:{cjk_chars[i]}{cjk_chars[i + 1]}")
        return tokens

    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)

    if not tokens_a or not tokens_b:
        # return character-level comparison
        return 1.0 if text_a == text_b else 0.0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


# =============================================================================
# DoomLoopDetector - core detector
# =============================================================================

class DoomLoopDetector:
    """
    Agent loop/stagnation detector.

    four types of detectors covering different loop patterns:
    1. generic_repeat - generic repeat: same (tool, params) repeated calls
    2. poll_no_progress - poll-no-progress: same-args-same-result polling
    3. ping_pong - alternating-oscillation: A→B→A→B oscillation
    4. cross_agent - cross-agent loop: A→B→A round-trip

    Adaptive threshold: dynamically adjust warn/critical thresholds based on call frequency.
    Auto-recovery strategy: provide specific recovery suggestions after detection and can execute callbacks.

    Security fix:
    - Bounded state: detector short-circuit logic, return on any CRITICAL
    - Thread-safe: all state operations protected by threading.Lock
    - State reset mechanism: clear detector internal state after recovery
    - Detector result cache: same batch of records not recomputed

    Usage:
        detector = DoomLoopDetector(config)
        # record before each tool call
        detector.record_tool_call("read_file", {"path": "/tmp/a.txt"}, agent_id="main")
        # record result after each tool call
        detector.record_tool_outcome("read_file", {"path": "/tmp/a.txt"}, result_text)
        # detectwhetherstuckLoop
        result = detector.detect()
        if result.stuck:
            await detector.execute_recovery(result)
    """

    def __init__(self, config: Optional[DoomLoopConfig] = None):
        self.config = config or DoomLoopConfig()
        # sliding window: latest records on the right side
        self._history: deque = deque(maxlen=self.config.history_size)
        # callfrequencytrack(used foradaptivethreshold)
        self._call_timestamps: deque = deque(maxlen=100)
        # Recovery strategy map: default recovery action for each detector type
        self._recovery_strategies: Dict[DetectorType, RecoveryAction] = {
            DetectorType.GENERIC_REPEAT: RecoveryAction.SWITCH_TOOL,
            DetectorType.POLL_NO_PROGRESS: RecoveryAction.FORCE_SUMMARIZE,
            DetectorType.PING_PONG: RecoveryAction.ROLLBACK_STEPS,
            DetectorType.CROSS_AGENT: RecoveryAction.DOWNGRADE_MODEL,
        }
        # custom recovery callback
        self._recovery_callbacks: Dict[RecoveryAction, Optional[Callable]] = {
            action: None for action in RecoveryAction
        }
        # [Security fix] thread safety lock
        self._lock = threading.Lock()
        # [Security fix] detector result cache
        self._last_detect_hash: str = ""  # hash of history at last detection
        self._cached_result: Optional[LoopDetectionResult] = None
        # [Security fix] detector internal state (used for state reset)
        self._detector_state: Dict[str, Any] = {}

    def record_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        agent_id: str = "",
    ) -> None:
        """Record a tool call (before call).

        Args:
            tool_name: Tool name
            args: Tool parameters
            agent_id: caller agent ID
        """
        if not self.config.enabled:
            return

        args_hash = _deterministic_hash(args)
        fuzzy_hash = _fuzzy_args_hash(tool_name, args)
        record = ToolCallRecord(
            tool_name=tool_name,
            args_hash=args_hash,
            fuzzy_hash=fuzzy_hash,
            agent_id=agent_id,
        )

        # [Security fix]threadsecurity
        with self._lock:
            self._history.append(record)
            self._call_timestamps.append(time.time())
            # after new record is added, cache invalid
            self._last_detect_hash = ""
            self._cached_result = None

    def record_tool_outcome(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result_text: str,
        agent_id: str = "",
    ) -> None:
        """Record tool call result (backfill after call).

        Find matching unfilled record in sliding window and backfill result hash.

        Args:
            tool_name: Tool name
            args: Tool parameters
            result_text: Tool execution result text
            agent_id: caller agent ID
        """
        if not self.config.enabled:
            return

        args_hash = _deterministic_hash(args)
        result_hash = _deterministic_hash(result_text)

        # [Security fix]threadsecurity
        with self._lock:
            # find matching not-yet-backfilled record from tail
            for record in reversed(self._history):
                if (record.tool_name == tool_name
                        and record.args_hash == args_hash
                        and not record.result_hash
                        and record.agent_id == agent_id):
                    record.result_hash = result_hash
                    record.result_text = result_text
                    # after result update, cache is invalid
                    self._last_detect_hash = ""
                    self._cached_result = None
                    return

            # if no matching record found (history size may be too small), record a complete record
            record = ToolCallRecord(
                tool_name=tool_name,
                args_hash=args_hash,
                result_hash=result_hash,
                result_text=result_text,
                agent_id=agent_id,
            )
            self._history.append(record)
            self._last_detect_hash = ""
            self._cached_result = None

    def detect(self) -> LoopDetectionResult:
        """Run all detectors, return the earliest detected loop.

        Detectors run by priority: generic_repeat → poll_no_progress → ping_pong → cross_agent.
        [Security fix] short-circuit logic: any detector triggering CRITICAL immediately returns, no further detection.
        [Security fix] Result cache: same batch of records not recomputed.

        Returns:
            LoopDetectionResult
        """
        if not self.config.enabled:
            return LoopDetectionResult()

        # [Security fix]threadsecurityread
        with self._lock:
            history_snapshot = list(self._history)
            timestamps_snapshot = list(self._call_timestamps)

        if len(history_snapshot) < 3:
            return LoopDetectionResult()

        # [Security fix]Detector result cache: same batch of records not recomputed
        current_hash = _deterministic_hash([
            (r.tool_name, r.args_hash, r.result_hash, r.agent_id)
            for r in history_snapshot
        ])
        if current_hash == self._last_detect_hash and self._cached_result is not None:
            return self._cached_result

        # computeadaptivethreshold
        warn_threshold, critical_threshold = self._adaptive_thresholds(
            timestamps_snapshot
        )

        # [Security fix] sequentially run detectors, CRITICAL short-circuit
        for detector_fn in [
            self._detect_generic_repeat,
            self._detect_poll_no_progress,
            self._detect_ping_pong,
        ]:
            result = detector_fn(history_snapshot, warn_threshold, critical_threshold)
            if result.stuck:
                # CRITICAL short-circuit: immediately return, do not continue detection
                if result.level == LoopLevel.CRITICAL:
                    self._update_cache(current_hash, result)
                    return result
                # WARNING also returns, but may still need subsequent detection
                self._update_cache(current_hash, result)
                return result

        # cross-agent loop detection
        if self.config.cross_agent_enabled:
            result = self._detect_cross_agent(
                history_snapshot, warn_threshold, critical_threshold
            )
            if result.stuck:
                self._update_cache(current_hash, result)
                return result

        result = LoopDetectionResult()
        self._update_cache(current_hash, result)
        return result

    def register_recovery_callback(
        self, action: RecoveryAction, callback: Callable
    ) -> None:
        """Register recovery strategy callback.

        Args:
            action: Recovery action type
            callback: callback function, signature: async def callback(result: LoopDetectionResult) -> bool
        """
        self._recovery_callbacks[action] = callback

    def set_recovery_strategy(
        self, detector_type: DetectorType, action: RecoveryAction
    ) -> None:
        """Set the recovery strategy for a detector.

        Args:
            detector_type: detector type
            action: Recovery action
        """
        self._recovery_strategies[detector_type] = action

    async def execute_recovery(self, result: LoopDetectionResult) -> bool:
        """Execute recovery strategy.

        auto-execute the corresponding recovery strategy after detecting a loop,
        Rather than only returning stuck status and letting upper layer process on its own.

        [Security fix] after recovery, clear the corresponding detector internal state,
        avoid residual state affecting subsequent detection.

        Args:
            result: detectResult

        Returns:
            True indicates recovery success, can continue executing
        """
        if not self.config.recovery_enabled:
            logger.warning(
                f"[DoomLoop] loop detected but recovery strategy not enabled: {result.message}"
            )
            return False

        action = self._recovery_strategies.get(
            result.detector, RecoveryAction.FORCE_SUMMARIZE
        )
        result.recovery = action

        callback = self._recovery_callbacks.get(action)
        if callback:
            try:
                success = await callback(result)
                # [Security fix] after recovery success, clear corresponding detector internal state
                if success:
                    self._reset_detector_state(result.detector)
                    # Post-recovery validation: re-detect to confirm recovery actually resolved the loop
                    post_recovery = self.detect()
                    if post_recovery.stuck:
                        logger.warning(
                            f"[DoomLoop] post-recovery validation FAILED - loop persists after {action.value}. "
                            f"Escalating: {post_recovery.message}"
                        )
                        return False
                return success
            except Exception as e:
                logger.error(f"[DoomLoop] Recovery strategyExecuteFailed: {e}")
                return False
        else:
            logger.info(
                f"[DoomLoop] recommended recovery strategy: {action.value}, but no callback registered."
                f"detectResult: {result.message}"
            )
            return False

    def reset(self) -> None:
        """Reset detector state (call when processing a new message)."""
        with self._lock:
            self._history.clear()
            self._call_timestamps.clear()
            self._detector_state.clear()
            self._last_detect_hash = ""
            self._cached_result = None

    # =========================================================================
    # internal detectors
    # =========================================================================

    def _update_cache(self, current_hash: str, result: LoopDetectionResult) -> None:
        """Update detection result cache."""
        self._last_detect_hash = current_hash
        self._cached_result = result

    def _reset_detector_state(self, detector_type: Optional[DetectorType]) -> None:
        """[Security fix] Reset specified detector internal state.

        Called after recovery success, avoid residual state affecting subsequent detection.

        Args:
            detector_type: detector type to reset
        """
        if detector_type is None:
            return

        with self._lock:
            state_key = detector_type.value
            if state_key in self._detector_state:
                self._detector_state[state_key] = {}
                logger.debug(
                    f"[DoomLoop] detector '{state_key}' internal state reset"
                )

    def _adaptive_thresholds(
        self, timestamps: Optional[List[float]] = None
    ) -> Tuple[int, int]:
        """Compute adaptive threshold.

        based on call frequency dynamic adjustment:
        - high-freq calls (>1 per second) → reduce threshold (faster loop detection)
        - low-freq calls (<0.1 per second) → increase threshold (avoid false alarms)

        Args:
            timestamps: timestamplist(threadsecuritysnapshot)

        Returns:
            (warn_threshold, critical_threshold)
        """
        if not self.config.adaptive_enabled:
            return self.config.warn_threshold_base, self.config.critical_threshold_base

        if timestamps is None:
            with self._lock:
                timestamps = list(self._call_timestamps)

        if len(timestamps) < 2:
            return self.config.warn_threshold_base, self.config.critical_threshold_base

        now = time.time()
        recent = [ts for ts in timestamps if now - ts < 60]
        if len(recent) < 2:
            return self.config.warn_threshold_base, self.config.critical_threshold_base

        # per-secondcallcount
        duration = recent[-1] - recent[0]
        freq = len(recent) / duration if duration > 0 else len(recent)

        warn = self.config.warn_threshold_base
        critical = self.config.critical_threshold_base

        if freq > 1.0:
            # high-freq: reduce threshold, faster detection
            warn = max(5, int(warn * 0.5))
            critical = max(10, int(critical * 0.5))
        elif freq > 0.5:
            # medium-high freq: slightly lower
            warn = max(6, int(warn * 0.7))
            critical = max(12, int(critical * 0.7))
        elif freq < 0.1:
            # low-freq: increase threshold, avoid false alarms
            warn = min(int(warn * 1.5), 50)  # Cap at 50 max
            critical = min(int(critical * 1.5), 100)  # Cap at 100 max

        return warn, critical

    def _detect_generic_repeat(
        self,
        history: List[ToolCallRecord],
        warn_threshold: int,
        critical_threshold: int,
    ) -> LoopDetectionResult:
        """Detector 1: generic repeat - same (tool, params) repeated calls.

        count calls within window where (tool_name, args_hash) are fully identical,
        report if threshold exceeded.

        Args:
            history: historyrecordsnapshot
            warn_threshold: warningthreshold
            critical_threshold: criticalthreshold

        Returns:
            LoopDetectionResult
        """
        counts: Dict[Tuple[str, str], int] = {}
        for record in history:
            # For bash: use fuzzy hash to catch near-duplicates (diff timeout, same command)
            fh = record.fuzzy_hash or record.args_hash
            key = (record.tool_name, fh)
            counts[key] = counts.get(key, 0) + 1

        # check in descending order of repeat count
        for (tool_name, args_hash), count in sorted(
            counts.items(), key=lambda x: -x[1]
        ):
            if count >= critical_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    level=LoopLevel.CRITICAL,
                    detector=DetectorType.GENERIC_REPEAT,
                    count=count,
                    message=(
                        f"Tool '{tool_name}' repeated call with same params {count} times, "
                        f"exceeds critical threshold {critical_threshold}. May be stuck in infinite loop."
                    ),
                    details={
                        'tool_name': tool_name,
                        'args_hash': args_hash[:16],
                        'count': count,
                        'threshold': critical_threshold,
                    },
                )
            elif count >= warn_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    level=LoopLevel.WARNING,
                    detector=DetectorType.GENERIC_REPEAT,
                    count=count,
                    message=(
                        f"Tool '{tool_name}' repeated call with same params {count} times, "
                        f"exceeds warning threshold {warn_threshold}. Suggest switching strategy."
                    ),
                    details={
                        'tool_name': tool_name,
                        'args_hash': args_hash[:16],
                        'count': count,
                        'threshold': warn_threshold,
                    },
                )

        return LoopDetectionResult()

    def _detect_poll_no_progress(
        self,
        history: List[ToolCallRecord],
        warn_threshold: int,
        critical_threshold: int,
    ) -> LoopDetectionResult:
        """Detector 2: poll-no-progress - same-args-same-result polling.

        count consecutive "no-progress" from tail forward.
        No need for keyword matching to judge poll tool (too coarse), rather look at whether result changes.

        Semantic similarity enhancement: timestamp changed but content unchanged also counts as no-progress.

        Args:
            history: historyrecordsnapshot
            warn_threshold: warningthreshold
            critical_threshold: criticalthreshold

        Returns:
            LoopDetectionResult
        """
        no_progress_count = 0
        last_args_hash = None
        last_result_hash = None
        last_result_text = None

        for record in reversed(history):
            if not record.result_hash:
                continue

            if record.args_hash == last_args_hash:
                if record.result_hash == last_result_hash:
                    # params and result fully same → no-progress
                    no_progress_count += 1
                elif (self.config.semantic_similarity_enabled
                      and last_result_text is not None):
                    # semantic similarity judgment: surface different but substance same
                    sim = _text_similarity(record.result_text, last_result_text)
                    if sim >= self.config.semantic_threshold:
                        no_progress_count += 1
                    else:
                        break
                else:
                    break
            elif last_args_hash is None:
                # first record, initialization
                last_args_hash = record.args_hash
                last_result_hash = record.result_hash
                last_result_text = record.result_text
                no_progress_count = 1
            else:
                break

        if no_progress_count >= critical_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CRITICAL,
                detector=DetectorType.POLL_NO_PROGRESS,
                count=no_progress_count,
                message=(
                    f"consecutive {no_progress_count} calls returned same result (no-progress), "
                    f"exceeds critical threshold {critical_threshold}. Poll may be hanging."
                ),
                details={'count': no_progress_count, 'threshold': critical_threshold},
            )
        elif no_progress_count >= warn_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.WARNING,
                detector=DetectorType.POLL_NO_PROGRESS,
                count=no_progress_count,
                message=(
                    f"consecutive {no_progress_count} calls returned same result, "
                    f"exceeds warning threshold {warn_threshold}. Suggest summarizing current status."
                ),
                details={'count': no_progress_count, 'threshold': warn_threshold},
            )

        return LoopDetectionResult()

    def _detect_ping_pong(
        self,
        history: List[ToolCallRecord],
        warn_threshold: int,
        critical_threshold: int,
    ) -> LoopDetectionResult:
        """Detector 3: alternating-oscillation - A→B→A→B oscillation.

        detect if the window tail has two different call patterns strictly alternating.
        not only detects param alternation, also detects tool name alternation.

        Args:
            history: historyrecordsnapshot
            warn_threshold: warningthreshold
            critical_threshold: criticalthreshold

        Returns:
            LoopDetectionResult
        """
        if len(history) < 4:
            return LoopDetectionResult()

        tail_records = list(history)[-20:]
        if len(tail_records) < 4:
            return LoopDetectionResult()

        signatures = [(r.tool_name, r.args_hash) for r in tail_records]

        # find two different signatures from tail
        pattern_a = signatures[-1]
        pattern_b = None
        for sig in reversed(signatures[:-1]):
            if sig != pattern_a:
                pattern_b = sig
                break

        if pattern_b is None:
            return LoopDetectionResult()

        # Verifytailwhetherstrictalternate
        alternating_count = 0
        expected = pattern_a
        for sig in reversed(signatures):
            if sig == expected:
                alternating_count += 1
                expected = pattern_b if expected == pattern_a else pattern_a
            else:
                break

        if alternating_count >= critical_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CRITICAL,
                detector=DetectorType.PING_PONG,
                count=alternating_count,
                message=(
                    f"detected alternating-oscillation pattern: "
                    f"'{pattern_a[0]}' ↔ '{pattern_b[0]}', "
                    f"alternated {alternating_count} times. "
                    f"Agent oscillates back and forth between two states."
                ),
                details={
                    'pattern_a': pattern_a[0],
                    'pattern_b': pattern_b[0],
                    'count': alternating_count,
                },
            )
        elif alternating_count >= warn_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.WARNING,
                detector=DetectorType.PING_PONG,
                count=alternating_count,
                message=(
                    f"may exist alternating-oscillation: "
                    f"'{pattern_a[0]}' ↔ '{pattern_b[0]}', "
                    f"alternated {alternating_count} times."
                ),
                details={
                    'pattern_a': pattern_a[0],
                    'pattern_b': pattern_b[0],
                    'count': alternating_count,
                },
            )

        return LoopDetectionResult()

    def _detect_cross_agent(
        self,
        history: List[ToolCallRecord],
        warn_threshold: int,
        critical_threshold: int,
    ) -> LoopDetectionResult:
        """Detector 4: cross-agent loop - A→B→A round-trip.

        In multi-agent scenario, Agent A calls Agent B, B calls back A, forming a cross-agent loop.
        detection method: count alternating patterns of agent_id in adjacent records.

        Args:
            history: historyrecordsnapshot
            warn_threshold: warningthreshold
            critical_threshold: criticalthreshold

        Returns:
            LoopDetectionResult
        """
        if len(history) < 4:
            return LoopDetectionResult()

        records_with_agent = [r for r in history if r.agent_id]
        if len(records_with_agent) < 4:
            return LoopDetectionResult()

        tail_agents = [r.agent_id for r in records_with_agent[-20:]]
        if len(set(tail_agents)) < 2:
            return LoopDetectionResult()

        agent_a = tail_agents[-1]
        agent_b = None
        for aid in reversed(tail_agents[:-1]):
            if aid != agent_a:
                agent_b = aid
                break

        if agent_b is None:
            return LoopDetectionResult()

        # VerifyalternateMode
        alternating_count = 0
        expected = agent_a
        for aid in reversed(tail_agents):
            if aid == expected:
                alternating_count += 1
                expected = agent_b if expected == agent_a else agent_a
            else:
                break

        if alternating_count >= critical_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CRITICAL,
                detector=DetectorType.CROSS_AGENT,
                count=alternating_count,
                message=(
                    f"detected cross-agent loop: "
                    f"'{agent_a}' ↔ '{agent_b}', "
                    f"alternated {alternating_count} times."
                ),
                details={
                    'agent_a': agent_a,
                    'agent_b': agent_b,
                    'count': alternating_count,
                },
            )
        elif alternating_count >= warn_threshold:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.WARNING,
                detector=DetectorType.CROSS_AGENT,
                count=alternating_count,
                message=(
                    f"may exist cross-agent loop: "
                    f"'{agent_a}' ↔ '{agent_b}', "
                    f"alternated {alternating_count} times."
                ),
                details={
                    'agent_a': agent_a,
                    'agent_b': agent_b,
                    'count': alternating_count,
                },
            )

        return LoopDetectionResult()

    # =========================================================================
    # Helper method
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get detector statistics info."""
        with self._lock:
            return {
                'enabled': self.config.enabled,
                'history_size': len(self._history),
                'max_history': self.config.history_size,
                'call_timestamps': len(self._call_timestamps),
                'adaptive_enabled': self.config.adaptive_enabled,
                'cross_agent_enabled': self.config.cross_agent_enabled,
            }
