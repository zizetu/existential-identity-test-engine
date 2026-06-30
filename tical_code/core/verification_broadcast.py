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

"""EITE Multi-Model Verification - broadcast same prompt to all models, audit results.

This module integrates with the EITE evaluation framework to send the same
prompt to every available provider in parallel (or sequentially), collect
their answers, and produce a VerificationAudit with consensus analysis.

Usage:
    from tical_code.core.verification_broadcast import broadcast_and_verify

    audit = broadcast_and_verify(
        failover=eval_framework.llm,
        prompt="What is the capital of France?",
    )
    print(audit.to_summary())
"""

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tical_code.core.ortools import (
    ModelAnswer,
    VerificationAudit,
    compare_answers,
)

logger = logging.getLogger("eite.verify_broadcast")


def _query_one(
    failover,
    provider_name: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
) -> ModelAnswer:
    """Query a single provider and return ModelAnswer.

    Uses the evaluation framework's failover.call with preferred_family
    to target a specific provider. The failover's circuit-breaker still
    applies -- unavailable providers are skipped.

    Args:
        failover: ModelFailover instance with configured providers.
        provider_name: Name of the provider to query.
        messages: Conversation messages.
        tools: Optional tool schemas.
        max_tokens: Max tokens for response.
        temperature: Sampling temperature.

    Returns:
        ModelAnswer with the provider's response or error.
    """
    t0 = time.time()
    try:
        result = failover.call(
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            preferred_family=(
                failover._get_family(
                    next(p for p in failover.providers if p.name == provider_name)
                )
                if any(p.name == provider_name for p in failover.providers)
                else None
            ),
        )
        latency = time.time() - t0
        return ModelAnswer(
            model_id=result.model,
            provider_name=result.provider_name,
            content=result.content,
            tool_calls=result.tool_calls,
            latency_seconds=latency,
            router_trace=result.router_trace,
        )
    except Exception as e:
        latency = time.time() - t0
        return ModelAnswer(
            model_id="unknown",
            provider_name=provider_name,
            content="",
            latency_seconds=latency,
            error=str(e),
        )


def broadcast_and_verify(
    failover,
    prompt: str,
    conversation_history: Optional[List[Dict]] = None,
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    parallel: bool = True,
    timeout_per_model: int = 60,
) -> VerificationAudit:
    """Send the same prompt to all providers and produce a verification audit.

    This is the core multi-model evaluation function in the EITE framework.
    It queries all configured providers, compares their answers, and
    produces a consensus analysis with divergence scoring.

    Args:
        failover: ModelFailover instance with configured providers.
        prompt: The user prompt to send to all models.
        conversation_history: Optional conversation context messages.
        tools: Optional tool schemas (only works with tool-supporting providers).
        max_tokens: Max tokens per provider response.
        temperature: Sampling temperature.
        parallel: If True, query all providers concurrently via ThreadPoolExecutor.
                  If False, query sequentially (useful for rate-limited providers).
        timeout_per_model: Seconds to wait per provider (only in parallel mode).

    Returns:
        VerificationAudit with all answers and consensus analysis.

    Example:
        audit = broadcast_and_verify(eval_framework.llm, "What is 2+2?")
        if audit.divergence_score > 0.3:
            logger.warning("Model disagreement detected: %s", audit.to_summary())
    """
    messages = list(conversation_history) if conversation_history else []
    messages.append({"role": "user", "content": prompt})

    # Filter: only query distinct models (skip same-model duplicates).
    seen_models = set()
    targets = []
    for p in failover.providers:
        family = failover._get_family(p)
        if family in seen_models:
            continue
        seen_models.add(family)
        targets.append(p)

    logger.info(
        "[EITE VerifyBroadcast] Broadcasting to %d providers: %s",
        len(targets),
        ", ".join(p.name for p in targets),
    )

    if parallel and len(targets) > 1:
        # Parallel query using ThreadPoolExecutor.
        answers = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as ex:
            futures = {
                ex.submit(
                    _query_one,
                    failover,
                    p.name,
                    messages,
                    tools,
                    max_tokens,
                    temperature,
                ): p.name
                for p in targets
            }
            for fut in concurrent.futures.as_completed(
                futures, timeout=timeout_per_model * len(targets)
            ):
                try:
                    answer = fut.result(timeout=timeout_per_model)
                    answers.append(answer)
                except Exception as e:
                    name = futures[fut]
                    answers.append(ModelAnswer(
                        model_id="timeout",
                        provider_name=name,
                        content="",
                        error=f"Parallel query timeout: {e}",
                    ))
    else:
        # Sequential query.
        answers = []
        for p in targets:
            answer = _query_one(
                failover, p.name, messages, tools, max_tokens, temperature,
            )
            answers.append(answer)

    audit = compare_answers(answers)
    audit.prompt = prompt

    logger.info(
        "[EITE VerifyBroadcast] Audit complete: %d models, %.2f divergence, unanimous=%s",
        len(answers),
        audit.divergence_score,
        audit.unanimous(),
    )

    return audit


def build_verify_tool_schema() -> Dict[str, Any]:
    """Build the 'verify_multi' tool schema for OpenAI function calling.

    This allows evaluated agents to invoke multi-model verification as a
    tool call before executing high-stakes actions during evaluation.

    Returns:
        Tool schema dict compatible with OpenAI function calling format.
    """
    return {
        "type": "function",
        "function": {
            "name": "verify_multi",
            "description": (
                "Send the same prompt to multiple AI models, compare their answers, "
                "and produce a verification audit with consensus analysis. "
                "Use this before high-stakes actions (file writes, deployments, "
                "system changes) to catch model-specific hallucinations or errors. "
                "Returns a divergence score (0=unanimous, 1=completely divergent) "
                "and recommendations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The prompt or question to send to all models "
                            "for verification."
                        ),
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "Divergence threshold above which the action "
                            "should be blocked. Default 0.3."
                        ),
                        "default": 0.3,
                    },
                },
                "required": ["prompt"],
            },
        },
    }


def execute_verify_multi(
    failover,
    prompt: str,
    threshold: float = 0.3,
    conversation_history: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Execute a verify_multi tool call and return structured result.

    Called by tool_executor when the evaluated agent invokes the
    verify_multi tool during an evaluation run.

    Returns:
        Dict with keys: consensus, divergence_score, unanimous,
        recommendations, per_model, blocked (bool), summary.
    """
    audit = broadcast_and_verify(
        failover=failover,
        prompt=prompt,
        conversation_history=conversation_history,
    )

    blocked = audit.divergence_score > threshold

    per_model = []
    for a in audit.answers:
        entry = {
            "model": a.model_id,
            "provider": a.provider_name,
            "latency_seconds": round(a.latency_seconds, 2),
        }
        if a.error:
            entry["error"] = a.error
        else:
            entry["answer"] = a.content[:500]
        if a.router_trace:
            entry["routing"] = a.router_trace.summary
        per_model.append(entry)

    return {
        "consensus": audit.consensus[:1000],
        "divergence_score": round(audit.divergence_score, 2),
        "unanimous": audit.unanimous(),
        "blocked": blocked,
        "recommendations": audit.recommendations,
        "per_model": per_model,
        "summary": audit.to_summary(),
    }
