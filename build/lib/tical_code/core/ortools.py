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

"""EITE Optimization Tools - cost tracking and evaluation analytics.

This module provides utilities for the EITE evaluation framework,
including cost tracking, model answer comparison, and verification
audit data structures used during multi-model evaluation runs.

Design principle: these utilities are woven into the EITE evaluation
pipeline and used by verification_broadcast and trace_recorder.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eite.ortools")


# ------------------------------------------------------------------
# Cost tracking - per-evaluation metrics
# ------------------------------------------------------------------

@dataclass
class EiteCostEntry:
    """Per-request cost and usage record for evaluation tracking.

    Tracks token usage, latency, and estimated cost for each
    LLM call during an evaluation run.
    """
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "model": self.model,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 2),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "timestamp": self.timestamp,
        }


@dataclass
class EiteCostSummary:
    """Aggregated cost summary for an evaluation session.

    Accumulates all per-request costs and provides rollup metrics.
    """
    total_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    by_model: Dict[str, Dict] = field(default_factory=dict)
    by_provider: Dict[str, Dict] = field(default_factory=dict)

    def add(self, entry: EiteCostEntry):
        """Add a cost entry to the summary."""
        self.total_requests += 1
        self.total_prompt_tokens += entry.prompt_tokens
        self.total_completion_tokens += entry.completion_tokens
        self.total_cost_usd += entry.estimated_cost_usd
        self.total_latency_ms += entry.latency_ms

        # By model
        if entry.model not in self.by_model:
            self.by_model[entry.model] = {"requests": 0, "tokens": 0, "cost": 0.0}
        self.by_model[entry.model]["requests"] += 1
        self.by_model[entry.model]["tokens"] += entry.total_tokens
        self.by_model[entry.model]["cost"] += entry.estimated_cost_usd

        # By provider
        if entry.provider not in self.by_provider:
            self.by_provider[entry.provider] = {"requests": 0, "tokens": 0, "cost": 0.0}
        self.by_provider[entry.provider]["requests"] += 1
        self.by_provider[entry.provider]["tokens"] += entry.total_tokens
        self.by_provider[entry.provider]["cost"] += entry.estimated_cost_usd

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "total_requests": self.total_requests,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "by_model": self.by_model,
            "by_provider": self.by_provider,
        }

    def to_summary(self) -> str:
        """Compact text summary for logging."""
        return (
            f"[EITE Cost] {self.total_requests} requests, "
            f"{self.total_prompt_tokens + self.total_completion_tokens} tokens, "
            f"${self.total_cost_usd:.4f}, "
            f"{self.total_latency_ms / 1000:.1f}s total"
        )


# ------------------------------------------------------------------
# Model Answer - single model response in evaluation
# ------------------------------------------------------------------

@dataclass
class ModelAnswer:
    """Single model's answer in a multi-model evaluation comparison.

    Captures the model's response content, tool calls, latency,
    and any errors encountered during the evaluation run.
    """
    model_id: str
    provider_name: str
    content: str
    tool_calls: Optional[List[Dict]] = None
    latency_seconds: float = 0.0
class RouterTrace:
    """Stub: full RouterTrace not available in light build."""
    def __init__(self):
        self.strategy = "unknown"
        self.endpoints_selected_provider = "unknown"
        self.endpoints_tried = 0
        self.total_latency = 0.0
    
    @classmethod
    def from_response(cls, metadata: dict) -> "RouterTrace":
        return cls()


def enrich_headers(provider_name: str, provider_endpoint: str) -> Dict[str, str]:
    """Stub: full enrich_headers. Returns OpenRouter metadata header."""
    if "openrouter" not in provider_endpoint.lower():
        return {}
    return {"X-OpenRouter-Metadata": "enabled"}


def enrich_body(
    messages: List[Dict],
    tools: Optional[List[Dict]],
    provider_name: str,
    provider_endpoint: str,
    enable_compression: bool = True,
    enable_structured_outputs: bool = False,
    tool_schema: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Stub: full enrich_body. Adds OpenRouter context compression plugin."""
    extra: Dict[str, Any] = {}
    is_or = "openrouter" in provider_endpoint.lower()
    if not is_or:
        return extra
    if enable_compression:
        extra["plugins"] = [{"id": "context-compression"}]
    if enable_structured_outputs and tool_schema:
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": tool_schema,
        }
    return extra


def extract_metadata(response: Dict[str, Any]) -> Optional[RouterTrace]:
    """Stub: full extract_metadata."""
    meta = response.get("openrouter_metadata")
    if not meta:
        return None
    return RouterTrace.from_response(meta)


def detect_cache_hit(response: Dict[str, Any]) -> bool:
    """Stub: heuristic cache-hit detection."""
    usage = response.get("usage", {})
    if usage.get("prompt_tokens", 1) == 0 and usage.get("completion_tokens", 0) > 0:
        return True
    return False

    router_trace: Optional[Any] = None
    error: Optional[str] = None
    cost_entry: Optional[EiteCostEntry] = None


# ------------------------------------------------------------------
# Verification Audit - multi-model comparison results
# ------------------------------------------------------------------

@dataclass
class VerificationAudit:
    """Result of multi-model evaluation verification.

    Contains all model answers plus consensus analysis, divergence
    scoring, and recommendations for the evaluation framework.
    """
    prompt: str
    answers: List[ModelAnswer] = field(default_factory=list)
    consensus: str = ""             # majority-agreed answer or synthesis
    divergence_score: float = 0.0   # 0.0 = unanimous, 1.0 = completely divergent
    recommendations: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def unanimous(self) -> bool:
        """True if all non-error answers are identical in content."""
        contents = [a.content.strip() for a in self.answers if not a.error]
        if len(contents) < 2:
            return False
        return len(set(contents)) == 1

    def error_count(self) -> int:
        """Number of models that returned errors."""
        return sum(1 for a in self.answers if a.error)

    def best_answer(self) -> Optional[ModelAnswer]:
        """Return the answer from the fastest non-error model."""
        ok = [a for a in self.answers if not a.error]
        if not ok:
            return None
        return min(ok, key=lambda a: a.latency_seconds)

    def to_summary(self) -> str:
        """Compact text summary for logging and display."""
        lines = [
            "=== Verification Audit ===",
            f"Prompt: {self.prompt[:100]}...",
            f"Models queried: {len(self.answers)}",
            f"Errors: {self.error_count()}",
            f"Unanimous: {self.unanimous()}",
            f"Divergence: {self.divergence_score:.2f}",
            f"Consensus: {self.consensus[:200]}",
            "",
            "Per-model:",
        ]
        for a in self.answers:
            status = "ERROR" if a.error else f"{a.latency_seconds:.1f}s"
            lines.append(f"  {a.model_id} ({a.provider_name}): {status}")
            if a.error:
                lines.append(f"    Error: {a.error[:100]}")
            else:
                lines.append(f"    Answer: {a.content[:200]}")
        if self.recommendations:
            lines.append("")
            lines.append("Recommendations:")
            for r in self.recommendations:
                lines.append(f"  - {r}")
        return "\n".join(lines)


def compare_answers(answers: List[ModelAnswer]) -> VerificationAudit:
    """Analyze multi-model answers and produce an audit with consensus.

    Uses string similarity (normalized) to detect agreement between
    model responses. Designed for the EITE evaluation framework.

    Args:
        answers: List of ModelAnswer objects from each model.

    Returns:
        VerificationAudit with consensus, divergence score, and
        recommendations for the evaluation framework.
    """
    ok = [a for a in answers if not a.error]
    audit = VerificationAudit(
        prompt="(from broadcast)",
        answers=answers,
    )

    if not ok:
        audit.consensus = "All models failed."
        audit.divergence_score = 1.0
        audit.recommendations.append(
            "Check provider health -- all models returned errors."
        )
        return audit

    if len(ok) == 1:
        audit.consensus = ok[0].content
        audit.divergence_score = 0.0
        return audit

    # Check for exact match
    contents = [a.content.strip().lower() for a in ok if a.content]
    unique = set(contents)
    if len(unique) == 1:
        audit.consensus = ok[0].content
        audit.divergence_score = 0.0
        return audit

    # Compute simple divergence: fraction of unique answers
    audit.divergence_score = (len(unique) - 1) / len(ok)

    # Pick the majority answer as consensus
    from collections import Counter
    majority_content, majority_count = Counter(contents).most_common(1)[0]
    audit.consensus = ok[contents.index(majority_content)].content

    if audit.divergence_score > 0.5:
        audit.recommendations.append(
            "High divergence across models -- verify results manually before acting."
        )
    if audit.divergence_score > 0.3:
        audit.recommendations.append(
            "Models disagree. Consider using the fastest model's answer with extra verification."
        )

    return audit
