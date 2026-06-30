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
# Original repository: https://github.com/zizetu/eite-agent
#

"""
Pre-decision clarify mechanism - confirm first when target is fuzzy, then execute
========================================

Agent analyzes target clarity before making decisions, proposes critical questions to user when
fuzzy - don't guess blindly. This is the first line of defense against hallucination.

Core concepts:
- ClarifyPhase: pre-decision clarification phase, analyzes target clarity
- ClarifyResult: clarification result (CLEAR / NEEDS_CLARIFICATION / REJECT)
- ClarifyQuestion: clarification question structure (question + options + reason + default + whether required)
- ClarifyStrategy: PASS_THROUGH / SOFT_CLARIFY / HARD_CLARIFY / CONSTITUTION_GATE

Integration points with existing modules:
- decision_engine.py: insert clarify phase between goal and plan
- constitution.py: reference constitution rules during clarification
- session.py: persist clarification result
- trace.py: record clarification procedure

Design principles:
1. Clarification must not be excessive - simple tasks should not ask 3 questions
2. High-risk operations should be confirmed even if the target is explicit
3. Clarification results are retained in the session for subsequent decisions to reference
4. At most propose 3 critical questions per time

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

# DESIGNED-NOT-DEAD: Pre-decision clarification mechanism. Awaiting decision_engine integration.
# DO NOT DELETE - prevents AI from acting on ambiguous goals.


import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# enums
# =============================================================================

class ClarifyStatus(Enum):
    """Clarification result status."""
    CLEAR = "clear"                           # target explicit, execute directly
    NEEDS_CLARIFICATION = "needs_clarification"  # target fuzzy, requires confirmation
    REJECT = "reject"                         # violates constitution or cannot complete, reject directly


class ClarifyStrategy(Enum):
    """Clarification strategy."""
    PASS_THROUGH = "pass_through"       # low risk + target explicit → execute directly
    SOFT_CLARIFY = "soft_clarify"       # medium risk or slightly blurry → execute with prompt, non-blocking
    HARD_CLARIFY = "hard_clarify"       # high risk or severely blurry → must confirm with user before execution
    CONSTITUTION_GATE = "constitution_gate"  # touches constitution boundary → must explicitly authorize


class AmbiguityType(Enum):
    """Ambiguity type."""
    MISSING_INFO = "missing_info"       # critical info missing
    AMBIGUOUS_INTENT = "ambiguous_intent"  # ambiguity (target can have multiple interpretations)
    INSUFFICIENT_CONTEXT = "insufficient_context"  # insufficient context
    HIGH_RISK_OPERATION = "high_risk_operation"  # high-risk operation


# =============================================================================
# Data structure
# =============================================================================

@dataclass
class ClarifyQuestion:
    """Clarification question structure.

    Attributes:
        question: question text
        options: optional answer list (helps user select quickly)
        reason: why this question is required
        default: default answer (if user does not answer)
        required: whether must answer before continuing
        ambiguity_type: the ambiguity type that triggered this question
    """
    question: str
    options: List[str] = field(default_factory=list)
    reason: str = ""
    default: Optional[str] = None
    required: bool = True
    ambiguity_type: AmbiguityType = AmbiguityType.MISSING_INFO

    def to_dict(self) -> Dict[str, Any]:
        return {
            'question': self.question,
            'options': self.options,
            'reason': self.reason,
            'default': self.default,
            'required': self.required,
            'ambiguity_type': self.ambiguity_type.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ClarifyQuestion':
        return cls(
            question=data.get('question', ''),
            options=data.get('options', []),
            reason=data.get('reason', ''),
            default=data.get('default'),
            required=data.get('required', True),
            ambiguity_type=AmbiguityType(data.get('ambiguity_type', 'missing_info')),
        )


@dataclass
class ClarifyResult:
    """Result of the clarification phase.

    Attributes:
        status: clarification result status (CLEAR / NEEDS_CLARIFICATION / REJECT)
        strategy: applicable clarification strategy
        questions: list of questions to propose to the user
        confidence: target clarity (0.0-1.0)
        ambiguities: detected target ambiguity type list
        rejection_reason: if REJECT, reason for rejection
        clarify_id: clarification session ID (used for tracking)
        timestamp: clarification time
    """
    status: ClarifyStatus = ClarifyStatus.CLEAR
    strategy: ClarifyStrategy = ClarifyStrategy.PASS_THROUGH
    questions: List[ClarifyQuestion] = field(default_factory=list)
    confidence: float = 1.0
    ambiguities: List[AmbiguityType] = field(default_factory=list)
    rejection_reason: str = ""
    clarify_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status.value,
            'strategy': self.strategy.value,
            'questions': [q.to_dict() for q in self.questions],
            'confidence': self.confidence,
            'ambiguities': [a.value for a in self.ambiguities],
            'rejection_reason': self.rejection_reason,
            'clarify_id': self.clarify_id,
            'timestamp': self.timestamp,
        }


@dataclass
class ClarifyAnswer:
    """User's response to clarification questions.

    Attributes:
        clarify_id: corresponding clarification session ID
        answers: map of question to answer (question_index → answer_text)
        all_required_answered: whether all required questions have been answered
    """
    clarify_id: str = ""
    answers: Dict[int, str] = field(default_factory=dict)
    all_required_answered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'clarify_id': self.clarify_id,
            'answers': self.answers,
            'all_required_answered': self.all_required_answered,
        }


# =============================================================================
# Fuzziness evaluation - keyword and pattern
# =============================================================================

# high-risk operation critical keywords (trigger HARD_CLARIFY or CONSTITUTION_GATE)
_HIGH_RISK_PATTERNS = [
    # Delete/destroy
    (r'(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX|VIEW)|TRUNCATE\s+TABLE|FORMAT\s+|PURGE\s+)', "destructive database or storage operation"),
    # money/trade
    (r'(?:Transfer\s+(?:funds?|money|payment|crypto|token|eth|btc)|PAYMENT|PURCHASE|financial_trade)', "money/trade operation"),
    # system security boundary
    (r'(?:chmod\s+0(?:777|666|555|444)|chown\s+root|sudo\s+rm\s+[-]?rf\s+/)', "permission escalation or destructive system command"),
]

# Indicators for critical info missing (target missing "do-what", "to-whom", "how-many" etc. critical parameters)
_MISSING_INFO_INDICATORS = {
    # missing target object
    "target_missing": [
        r'(?:help the (?:user|him|her) (?:with|process|fix))',
        r'(?:process the (?:problem|request|matter))',
    ],
    # missing quantity/scope
    "scope_missing": [
        r'(?:small.amount|a.lot|batch)',
        r'(?:all.of.(?:them|these|those))',
    ],
    # missing concrete condition
    "condition_missing": [
        r'(?:at the right time|when appropriate|when needed|if necessary)',
    ],
    # missing time constraint
    "time_missing": [
        r'(?:as.early.as|asapprocess)',
    ],
}

# Ambiguity indicators (target can have multiple interpretations)
_AMBIGUITY_INDICATORS = [
    # pronoun ambiguity
    (r'(?:he|she|it|they|this|that)\s*(?:data|file|config|info)', "pronoun reference unclear"),
]

# Context insufficient indicators (require more info before making a decision)
_CONTEXT_INSUFFICIENT_INDICATORS = [
    r'(?:when (?:we|you|I) (?:were|was|had) (?:working|talking))',
]


# =============================================================================
# ClarifyPhase - pre-decision clarification phase
# =============================================================================

class ClarifyPhase:
    """Pre-decision clarification phase - analyzes whether the target requires clarification.

    Usage:
        clarify = ClarifyPhase()
        result = clarify.analyze_goal("help me delete that file", context={})
        if result.status == ClarifyStatus.NEEDS_CLARIFICATION:
            # propose result.questions to the user
            pass

    Integration with ConstitutionEnforcer:
        clarify = ClarifyPhase(constitution_enforcer=enforcer)
        # analyze_goal internally calls constitution check
        result = clarify.analyze_goal(goal, context)
        if result.strategy == ClarifyStrategy.CONSTITUTION_GATE:
            # must explicitly authorize before execution
            pass
    """

    # maximum number of questions to propose (avoid excessive clarification)
    MAX_QUESTIONS = 3

    # Confidence thresholds (v0.5.9: lowered thresholds to reduce excessive interception)
    CONFIDENCE_CLEAR = 0.3       # >= 0.3: target explicit - broad threshold for programming tasks
    CONFIDENCE_SOFT = 0.2        # 0.2-0.3: slightly blurry
    CONFIDENCE_HARD = 0.1        # 0.1-0.2: severely blurry
    # < 0.2: extremely fuzzy or violates constitution

    # empty-tail detect default threshold: consecutive N empty-tail appearances judged as orphan
    DEFAULT_ORPHAN_THRESHOLD = 2

    def __init__(
        self,
        constitution_enforcer: Optional[Any] = None,
        session_data: Optional[Dict[str, Any]] = None,
        orphan_threshold: int = DEFAULT_ORPHAN_THRESHOLD,
        auto_confirm: bool = False,
    ):
        """
        Args:
            constitution_enforcer: ConstitutionEnforcer instance (optional)
            session_data: current session data (optional, used for context analysis)
            orphan_threshold: empty-tail detect threshold (consecutive N empty-tail appearances judged as orphan, default 2)
            auto_confirm: auto-confirm slightly blurry tasks (SOFT_CLARIFY → PASS_THROUGH, default False)
        """
        self._enforcer = constitution_enforcer
        self._session_data = session_data or {}
        # generate clarification session ID sequence
        self._clarify_counter = 0
        # v0.13: empty-tail detect counter
        self._orphan_count: int = 0
        self._orphan_threshold: int = max(1, orphan_threshold)
        # v0.5.9: auto-confirm mode
        self._auto_confirm: bool = auto_confirm

    def analyze_goal(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> ClarifyResult:
        """Analyze whether the target requires clarification.

        Analysis process:
        1. Compute target clarity (confidence)
        2. Detect fuzziness types (ambiguities)
        3. Check high-risk operations
        4. Check constitution compliance
        5. Determine clarification strategy
        6. Generate clarification questions (if required)

        Args:
            goal: user target description
            context: context info

        Returns:
            ClarifyResult clarification result
        """
        if not goal or not isinstance(goal, str):
            return ClarifyResult(
                status=ClarifyStatus.REJECT,
                strategy=ClarifyStrategy.PASS_THROUGH,
                confidence=0.0,
                rejection_reason="target empty or format invalid",
                clarify_id=self._next_clarify_id(),
            )

        context = context or {}

        # step 1: detect fuzziness types
        ambiguities = []
        confidence = 1.0  # initially fully clear

        # 1a. critical info missing detection
        missing_info = self._detect_missing_info(goal)
        if missing_info:
            ambiguities.append(AmbiguityType.MISSING_INFO)
            # each type of missing info deducts points: target object missing deducts the most
            for mtype in missing_info:
                if mtype == "target_missing":
                    confidence -= 0.35  # missing operation object is the most severe info missing
                elif mtype == "scope_missing":
                    confidence -= 0.20  # missing scope
                else:
                    confidence -= 0.15  # other missing

        # 1b. ambiguity detection
        ambiguous = self._detect_ambiguity(goal)
        if ambiguous:
            ambiguities.append(AmbiguityType.AMBIGUOUS_INTENT)
            confidence -= 0.25 * len(ambiguous)

        # 1c. context insufficient detection
        insufficient = self._detect_insufficient_context(goal)
        if insufficient:
            ambiguities.append(AmbiguityType.INSUFFICIENT_CONTEXT)
            confidence -= 0.15 * len(insufficient)

        # step 2: high-risk operation detection
        high_risk = self._detect_high_risk(goal)
        if high_risk:
            ambiguities.append(AmbiguityType.HIGH_RISK_OPERATION)
            confidence -= 0.2

        # step 3: constitution compliance check
        constitution_violation = False
        constitution_reason = ""
        if self._enforcer:
            check_result = self._enforcer.check_action(goal, context, mode="read")
            if not check_result.allowed:
                constitution_violation = True
                constitution_reason = check_result.reason
                confidence = 0.0

        # clamp confidence to [0.0, 1.0]
        confidence = max(0.0, min(1.0, confidence))

        # step 4: determine status and strategy
        if constitution_violation:
            status = ClarifyStatus.REJECT
            strategy = ClarifyStrategy.CONSTITUTION_GATE
        elif confidence >= self.CONFIDENCE_CLEAR and not high_risk:
            status = ClarifyStatus.CLEAR
            strategy = ClarifyStrategy.PASS_THROUGH
        elif confidence >= self.CONFIDENCE_CLEAR and high_risk:
            # Target is explicit even if flagged as high-risk. Log but pass through
            # rather than blocking - the user knows what they are asking for.
            status = ClarifyStatus.CLEAR
            strategy = ClarifyStrategy.PASS_THROUGH
        elif confidence >= self.CONFIDENCE_SOFT:
            # slightly blurry → soft confirm (but pass directly when auto_confirm)
            if self._auto_confirm:
                status = ClarifyStatus.CLEAR
                strategy = ClarifyStrategy.PASS_THROUGH
            else:
                status = ClarifyStatus.NEEDS_CLARIFICATION
                strategy = ClarifyStrategy.SOFT_CLARIFY
        elif confidence >= self.CONFIDENCE_HARD:
            # severely blurry → hard confirm
            status = ClarifyStatus.NEEDS_CLARIFICATION
            strategy = ClarifyStrategy.HARD_CLARIFY
        else:
            # extremely fuzzy
            status = ClarifyStatus.NEEDS_CLARIFICATION
            strategy = ClarifyStrategy.HARD_CLARIFY

        # step 5: generate clarification questions
        questions = []
        if status == ClarifyStatus.NEEDS_CLARIFICATION:
            questions = self._generate_questions(
                goal, ambiguities, high_risk, context
            )

        result = ClarifyResult(
            status=status,
            strategy=strategy,
            questions=questions[:self.MAX_QUESTIONS],  # at most 3 questions
            confidence=round(confidence, 2),
            ambiguities=ambiguities,
            rejection_reason=constitution_reason,
            clarify_id=self._next_clarify_id(),
        )

        logger.info(
            f"[ClarifyPhase] target analysis: status={status.value}, "
            f"strategy={strategy.value}, confidence={confidence:.2f}, "
            f"questions={len(questions)}"
        )

        return result

    def evaluate_answer(
        self,
        clarify_result: ClarifyResult,
        answer: ClarifyAnswer,
    ) -> ClarifyResult:
        """Evaluate user's response to clarification questions, return updated ClarifyResult.

        Args:
            clarify_result: original clarification result
            answer: user response

        Returns:
            updated ClarifyResult
        """
        if answer.clarify_id != clarify_result.clarify_id:
            logger.warning(
                f"[ClarifyPhase] answer clarify_id does not match: "
                f"{answer.clarify_id} != {clarify_result.clarify_id}"
            )

        # check whether all required questions have been answered
        if not answer.all_required_answered:
            # still has required questions unanswered → maintain NEEDS_CLARIFICATION
            remaining = []
            for i, q in enumerate(clarify_result.questions):
                if q.required and i not in answer.answers:
                    remaining.append(q)
            return ClarifyResult(
                status=ClarifyStatus.NEEDS_CLARIFICATION,
                strategy=clarify_result.strategy,
                questions=remaining,
                confidence=clarify_result.confidence,
                ambiguities=clarify_result.ambiguities,
                clarify_id=clarify_result.clarify_id,
            )

        # all required questions answered → upgrade to CLEAR
        new_confidence = min(1.0, clarify_result.confidence + 0.3)

        # but if it is CONSTITUTION_GATE, still need caution after user confirmation
        if clarify_result.strategy == ClarifyStrategy.CONSTITUTION_GATE:
            return ClarifyResult(
                status=ClarifyStatus.CLEAR,
                strategy=ClarifyStrategy.CONSTITUTION_GATE,
                confidence=new_confidence,
                clarify_id=clarify_result.clarify_id,
            )

        return ClarifyResult(
            status=ClarifyStatus.CLEAR,
            strategy=ClarifyStrategy.PASS_THROUGH,
            confidence=new_confidence,
            clarify_id=clarify_result.clarify_id,
        )

    def _next_clarify_id(self) -> str:
        """Generate next clarification session ID."""
        self._clarify_counter += 1
        return f"clarify_{self._clarify_counter}_{int(time.time() * 1000)}"

    # --- empty-tail detect (v0.13) ---

    def detect_orphan_tool_tail(self, response: Dict[str, Any]) -> bool:
        """Detect empty-tail (orphan tool call) phenomenon in model response.

        Some models (e.g., MiMo) sometimes emit tool_calls but without text content following,
        causing the agent to continue feeding empty data to the model, entering a dead loop.

        Detect logic:
        1. response has tool_calls but content is empty or None
        2. response only has tool_calls with no text output
        3. consecutive N times (default 2) empty-tail appearances → judged as orphan, block

        Args:
            response: model response dict, usually contains content / tool_calls fields

        Returns:
            True indicates orphan empty-tail detected (should block), False indicates normal
        """
        try:
            if not isinstance(response, dict):
                # non-dict type undetermined, default not block
                self._reset_orphan_count()
                return False

            has_tool_calls = bool(response.get('tool_calls'))
            content = response.get('content')
            # content is None / empty string / pure whitespace - all treated as "no content"
            has_content = bool(content and str(content).strip())

            if has_tool_calls and not has_content:
                # has tool_calls but no text content → suspected empty-tail
                self._orphan_count += 1
                logger.warning(
                    f"[ClarifyPhase] detected empty-tail response: "
                    f"tool_calls={has_tool_calls}, content empty, "
                    f"consecutive count={self._orphan_count}/{self._orphan_threshold}"
                )
                if self._orphan_count >= self._orphan_threshold:
                    logger.error(
                        f"[ClarifyPhase] consecutive {self._orphan_count} empty-tail appearances, "
                        f"judged as orphan tool call, trigger HARD_CLARIFY block"
                    )
                    return True
                # not reached threshold, temporarily not block but record
                return False

            # has normal content → reset count
            if has_content:
                self._reset_orphan_count()

            return False

        except Exception as e:
            logger.warning(f"[ClarifyPhase] empty-tail detection exception: {e}")
            return False

    def _reset_orphan_count(self) -> None:
        """Reset empty-tail counter (called after receiving normal content)."""
        if self._orphan_count > 0:
            logger.debug("[ClarifyPhase] empty-tail counter reset")
        self._orphan_count = 0

    def get_orphan_status(self) -> Dict[str, Any]:
        """Get current empty-tail detection status.

        Returns:
            dict containing orphan_count / orphan_threshold
        """
        return {
            'orphan_count': self._orphan_count,
            'orphan_threshold': self._orphan_threshold,
            'is_orphan': self._orphan_count >= self._orphan_threshold,
        }

    # --- fuzziness detection methods ---

    @staticmethod
    def _detect_missing_info(goal: str) -> List[str]:
        """Detect critical info missing.

        Check whether the target is missing "do-what", "to-whom", "how-many" etc. critical parameters.

        Args:
            goal: Target description

        Returns:
            list of missing info types
        """
        missing = []
        for info_type, patterns in _MISSING_INFO_INDICATORS.items():
            for pattern in patterns:
                if re.search(pattern, goal, re.IGNORECASE):
                    missing.append(info_type)
                    break  # each type only record once
        return missing

    @staticmethod
    def _detect_ambiguity(goal: str) -> List[str]:
        """Detect ambiguity.

        Check whether the target can have multiple interpretations.

        Args:
            goal: Target description

        Returns:
            list of ambiguity descriptions
        """
        ambiguous = []
        for pattern, desc in _AMBIGUITY_INDICATORS:
            if re.search(pattern, goal, re.IGNORECASE):
                ambiguous.append(desc)
        return ambiguous

    @staticmethod
    def _detect_insufficient_context(goal: str) -> List[str]:
        """Detect insufficient context.

        Check whether existing info is sufficient to make a reliable decision.

        Args:
            goal: Target description

        Returns:
            list of context insufficient descriptions
        """
        insufficient = []
        for pattern in _CONTEXT_INSUFFICIENT_INDICATORS:
            if re.search(pattern, goal, re.IGNORECASE):
                insufficient.append(f"require more context: {pattern}")
        return insufficient

    @staticmethod
    def _detect_high_risk(goal: str) -> List[str]:
        """Detect high-risk operations.

        Even if the target is explicit, operations involving money, delete, send etc.
        irreversible operations should also be confirmed.

        Args:
            goal: Target description

        Returns:
            list of high-risk operation descriptions
        """
        risks = []
        for pattern, desc in _HIGH_RISK_PATTERNS:
            if re.search(pattern, goal, re.IGNORECASE):
                risks.append(desc)
        return risks

    # --- clarification question generation ---

    def _generate_questions(
        self,
        goal: str,
        ambiguities: List[AmbiguityType],
        high_risk: List[str],
        context: Dict[str, Any],
    ) -> List[ClarifyQuestion]:
        """Generate clarification questions based on fuzziness types.

        Strategy:
        - high-risk operation → generate confirmation question (must answer)
        - missing target object → ask "what is the operation object?"
        - missing scope → ask "what is the concrete scope?"
        - ambiguity → provide options for user to select
        - insufficient context → ask "can you provide more context?"

        At most generate MAX_QUESTIONS (3) questions.

        Args:
            goal: Target description
            ambiguities: detected target fuzziness types
            high_risk: high-risk operation descriptions
            context: context

        Returns:
            list of clarification questions
        """
        questions = []

        # priority 1: high-risk operation confirmation
        if high_risk and AmbiguityType.HIGH_RISK_OPERATION in ambiguities:
            risk_desc = ", ".join(high_risk[:2])
            questions.append(ClarifyQuestion(
                question=f"This operation involves {risk_desc}. Confirm execution?",
                options=["Confirm execution", "Cancel operation", "Check details first, then decide"],
                reason=f"Detected high-risk operation: {risk_desc}",
                required=True,
                ambiguity_type=AmbiguityType.HIGH_RISK_OPERATION,
            ))

        # priority 2: critical info missing
        if AmbiguityType.MISSING_INFO in ambiguities:
            missing_info = self._detect_missing_info(goal)
            q = self._generate_missing_info_question(goal, missing_info)
            if q:
                questions.append(q)

        # priority 3: ambiguity clarification
        if AmbiguityType.AMBIGUOUS_INTENT in ambiguities:
            ambiguous = self._detect_ambiguity(goal)
            q = self._generate_ambiguity_question(goal, ambiguous)
            if q:
                questions.append(q)

        # priority 4: context supplement
        if AmbiguityType.INSUFFICIENT_CONTEXT in ambiguities:
            questions.append(ClarifyQuestion(
                question="Can you provide more context info?",
                options=["I will supplement context briefly", "Just use current info to execute"],
                reason="existing context insufficient to make a reliable decision",
                default="Just use current info to execute",
                required=False,
                ambiguity_type=AmbiguityType.INSUFFICIENT_CONTEXT,
            ))

        return questions[:self.MAX_QUESTIONS]

    @staticmethod
    def _generate_missing_info_question(
        goal: str,
        missing_types: List[str],
    ) -> Optional[ClarifyQuestion]:
        """Generate question targeting info missing.

        Args:
            goal: Target description
            missing_types: list of missing info types

        Returns:
            clarification question, or None
        """
        if not missing_types:
            return None

        # generate different questions based on missing type
        type_question_map = {
            "target_missing": ClarifyQuestion(
                question="Please specify the operation target/object?",
                reason="target missing explicit operation object",
                required=True,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "scope_missing": ClarifyQuestion(
                question="Please specify the concrete scope and quantity of the operation?",
                options=["Current item only", "Specify range", "All"],
                reason="target missing explicit scope/quantity",
                required=True,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "condition_missing": ClarifyQuestion(
                question="Under what condition should this operation execute?",
                options=["Execute immediately", "Execute after condition is met", "Check condition first"],
                reason="target missing execution condition",
                default="Execute immediately",
                required=False,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
            "time_missing": ClarifyQuestion(
                question="At what time should this operation execute?",
                options=["Execute immediately", "Schedule for later", "Wait for notification"],
                reason="target missing time constraint",
                default="Execute immediately",
                required=False,
                ambiguity_type=AmbiguityType.MISSING_INFO,
            ),
        }

        # take the first matching missing type (most important question)
        for mtype in missing_types:
            if mtype in type_question_map:
                return type_question_map[mtype]

        return None

    @staticmethod
    def _generate_ambiguity_question(
        goal: str,
        ambiguous_descs: List[str],
    ) -> Optional[ClarifyQuestion]:
        """Generate question targeting ambiguity.

        Args:
            goal: Target description
            ambiguous_descs: list of ambiguity descriptions

        Returns:
            clarification question, or None
        """
        if not ambiguous_descs:
            return None

        desc = ambiguous_descs[0]  # take the most important ambiguity
        return ClarifyQuestion(
            question=f"Your target has ambiguity ({desc}). Please clarify your intent?",
            options=["Let me re-describe", "Execute with best common understanding", "Show different interpretations first, then decide"],
            reason=f"Detected ambiguity: {desc}",
            required=True,
            ambiguity_type=AmbiguityType.AMBIGUOUS_INTENT,
        )


# =============================================================================
# convenience functions
# =============================================================================

def clarify_goal(
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    constitution_enforcer: Optional[Any] = None,
) -> ClarifyResult:
    """Convenience function: quickly analyze whether the target requires clarification.

    Args:
        goal: Target description
        context: Context info
        constitution_enforcer: constitution enforcer (optional)

    Returns:
        ClarifyResult
    """
    phase = ClarifyPhase(constitution_enforcer=constitution_enforcer)
    return phase.analyze_goal(goal, context)


def format_clarify_questions(result: ClarifyResult) -> str:
    """Format clarification result into user-readable text.

    Args:
        result: clarification result

    Returns:
        formatted text
    """
    if result.status == ClarifyStatus.CLEAR:
        return "target explicit, execute directly."

    if result.status == ClarifyStatus.REJECT:
        return f"[BLOCKED] operation rejected: {result.rejection_reason}"

    # NEEDS_CLARIFICATION
    lines = [f"[CONFIRM] requires confirmation (Confidence: {result.confidence:.0%}):\n"]
    for i, q in enumerate(result.questions, 1):
        lines.append(f"  {i}. {q.question}")
        if q.reason:
            lines.append(f"     reason: {q.reason}")
        if q.options:
            option_str = " / ".join(q.options)
            lines.append(f"     options: {option_str}")
        if q.default:
            lines.append(f"     default: {q.default}")
        required_mark = "(must answer)" if q.required else "(optional)"
        lines.append(f"     {required_mark}")

    return "\n".join(lines)
