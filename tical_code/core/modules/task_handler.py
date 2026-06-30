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

"""Task handler module - autonomous task execution loop.

Handles long-running autonomous tasks through a persistent execution
loop with crash recovery:

  1. **Task state machine** - loads task state from disk, transitions
     ``pending → running → completed/failed``, and persists every step
     so the task can resume after crashes or restarts.

  2. **Conversation checkpointing** - saves pre-step and post-step
     raw conversation snapshots via the checkpoint system for full
     replayability.  On resume, reconstructs the conversation from the
     last checkpoint or builds a basic resume hint from the saved plan.

  3. **LLM conversation loop** - iterates up to ``max_steps``, calling
     the model with tool schemas.  Supports context compression via
     ``ContextManager``, message adaptation for model families, and
     exponential backoff on 429 rate-limit responses.

  4. **Tool execution** - prefers the ``ToolExecutor`` dispatcher with
     legacy ``execute()`` fallback.  Each tool call is checked against
     the Constitution, DecisionEngine strategy rules, and verification
     phases.  Task completion is detected via ``chat_send``, ``reply``,
     or ``task_complete`` tool calls (with optional DecisionEngine
     result verification) or the ``[TASK_COMPLETE]`` text signal.

  5. **Vigil AI signal hooks** - ``task_started`` at loop entry,
     ``record_tokens`` on every LLM response, and ``task_completed``
     on success or failure.

  6. **Memory management** - periodic memory profiling, garbage
     collection, and session-family dictionary trimming.  Schedules a
     systemd restart if the RSS memory limit is exceeded mid-task.

  7. **Stuck detection** - if the task state accumulates repeated errors
     or hits the step limit without progress, it is marked ``failed``.

  8. **Pending task persistence** - ``save_pending`` writes a
     continuation hint to disk; ``load_pending`` reads and deletes it,
     enabling the next loop iteration to pick up where it left off.

Extracted from unified_worker.py._run_task (L618-1096), _load_pending,
and _save_pending.  Takes a SharedContext instead of self, enabling
the god-object split.

Author: Tical
Version: see tical_code.__version__"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

# ── tical-code internal imports ──────────────────────────────────────
from tical_code.core.shared_context import SharedContext, _get_rss_mb
from tical_code.core.trace import TraceEvent

# Conditional imports - may be None on light installs
try:
    from tical_code.core.errors import ErrorLogger, ErrorCategory
except ImportError:
    ErrorLogger = None
    ErrorCategory = None

try:
    from tical_code.core.task_state import (
        create_task,
        load_state,
        save_state,
        fail_task,
    )
except ImportError:
    create_task = None
    load_state = None
    save_state = None
    fail_task = None

try:
    from tical_code.core.modules.context_compactor import ContextCompactor
except ImportError:
    ContextCompactor = None

try:
    from tical_code.core.memory_profiler import MemoryProfiler, force_gc_collect
except ImportError:
    MemoryProfiler = None
    force_gc_collect = None

from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
from tical_code.core.response_formatter import format_result

logger = logging.getLogger("tical-code.task_handler")


# ────────────────────────────────────────────────────────────────────
# Helpers (extracted from Worker._load_pending / _save_pending)
#
# ``load_pending`` and ``save_pending`` manage a lightweight
# continuation mechanism: when the LLM signals "I still need to..."
# in a reply, the remaining work is serialised to a JSON file on disk.
# On the next worker loop iteration, ``load_pending`` reads and deletes
# that file, feeding the continuation task back into the task runner.
# ────────────────────────────────────────────────────────────────────

def load_pending(ctx: SharedContext) -> Optional[dict]:
    """Load a pending continuation task from disk and delete the file.

    Reads ``ctx._pending_task_file`` (a ``pathlib.Path``), deserialises
    its JSON content, then atomically removes the file so the task is
    not re-loaded on subsequent iterations.  The returned dict contains
    the keys ``"task"`` (the continuation goal), ``"iteration"``
    (the step at which it was saved), and ``"source": "continuation"``.

    Args:
        ctx: The shared context providing ``_pending_task_file``.

    Returns:
        A dict with the pending task data, or ``None`` if the file does
        not exist or cannot be read (errors are logged at DEBUG level
        and swallowed - the caller handles the None case gracefully)."""
    try:
        if ctx._pending_task_file.exists():
            data = json.loads(ctx._pending_task_file.read_text())
            ctx._pending_task_file.unlink(missing_ok=True)
            return data
    except Exception as e:
        logger.debug("[pending_task] swallowed: %s", e)
    return None


def save_pending(ctx: SharedContext, task: str, iteration: int = 0) -> None:
    """Save a continuation task to disk for the next loop iteration.

    Writes a JSON file containing the task goal, the current iteration
    number, and a ``"source": "continuation"`` marker.  Creates parent
    directories if they do not exist.

    This is triggered when the LLM includes the phrase ``"I still need
    to"`` in a reply, indicating that the current message turn did not
    fully complete the work and the remainder should be picked up on
    the next iteration.

    Args:
        ctx: The shared context providing ``_pending_task_file``.
        task: The continuation goal string (extracted from the LLM reply
            after ``"I still need to"``).
        iteration: The iteration number at which the task was saved
            (default 0)."""
    try:
        ctx._pending_task_file.parent.mkdir(parents=True, exist_ok=True)
        ctx._pending_task_file.write_text(json.dumps({
            "task": task,
            "iteration": iteration,
            "source": "continuation",
        }))
    except Exception as e:
        logger.warning("Failed to save pending task: %s", e)


# ────────────────────────────────────────────────────────────────────
# Autonomous task runner
#
# ``run_task`` is the core task execution loop.  It loads the task
# state from disk, builds a conversation (from checkpoint or scratch),
# then iterates: LLM call → tool execution → state save → next step.
# Each tool call passes through Constitution checks, DecisionEngine
# strategy enforcement, and result verification.  On completion or
# failure, Vigil is notified and the task state is persisted.
# ────────────────────────────────────────────────────────────────────

def run_task(ctx: SharedContext, task) -> None:
    """Run a single autonomous task through the full execution loop.

    This is the entry point for long-running autonomous tasks.  It
    implements the complete lifecycle:

    **Initialisation**
        * Loads the task state from disk via ``load_state()``.
        * Transitions ``pending → running`` status.
        * Generates a trace ID and starts skill-extraction tracking.
        * Notifies Vigil of task start.

    **Conversation construction**
        * If a checkpoint conversation exists (``ctx._resume_conv``),
          restores it and appends a resumption hint.
        * Otherwise builds a fresh conversation from the system prompt
          and the task goal.
        * If resuming without a checkpoint, appends the saved plan and
          recent errors as context.

    **Main loop (up to ``state.max_steps``)**
        For each step:

        1. Save a pre-step checkpoint.
        2. Build compressed context via ``ContextManager`` if active.
        3. Adapt messages for the target model family.
        4. Call the LLM with tool schemas, retrying on 429 errors
           with exponential backoff (up to 3 attempts).
        5. Trace the LLM call latency and token usage.
        6. For text-only responses: check for ``[TASK_COMPLETE]``.
        7. For tool-call responses:
           a. Check each tool against Constitution rules.
           b. Enforce DecisionEngine tool strategy.
           c. Execute via ``ToolExecutor`` (preferred) or legacy
              ``execute()`` fallback.
           d. Record in skill extractor and Vigil.
           e. Trace tool execution latency.
           f. Detect DoomLoop escalation.
           g. Verify task completion signals (``chat_send``, ``reply``,
              ``task_complete``).
        8. Save post-step state, run memory profiling and GC, update
           context manager, check memory limits.

    **Termination**
        * **Success** - detected via completion tool calls or
          ``[TASK_COMPLETE]`` text.  MemoryEvolver records the
          experience, session state is saved, Vigil is notified.
        * **Failure** - triggered by stuck detection (repeated errors),
          reaching ``max_steps``, or exceeding the RSS memory limit
          (triggers systemd restart).
        * **Crash recovery** - the per-step ``save_state()`` calls
          ensure the task can resume from the last successful step.

    Args:
        ctx: The shared context providing LLM, tools, config, task state
            helpers, Vigil, memory management, and all other subsystems.
        task: A task object with at least ``task_id``, ``goal``,
            ``status``, and ``workspace`` attributes (produced by
            ``create_task()``)."""
    if load_state is None:
        logger.warning("task_state module not available -- skipping task runner")
        return

    state = load_state(task.task_id, workspace=ctx.workspace)
    if state is None:
        logger.warning("Task %s state disappeared -- skipping", task.task_id)
        return

    # Generate a trace ID for this task run
    ctx._current_trace_id = ctx.trace_logger.new_trace_id()

    # Transition pending -> running
    if state.status == "pending":
        state.status = "running"
        save_state(state, workspace=ctx.workspace)

    logger.info("Task %s: starting run at step %d, max %d",
                 state.task_id, state.step, state.max_steps)

    # Start skill extraction -- track tool calls for auto-skill generation
    ctx.skill_extractor.start_task(task_id=state.task_id, goal=state.goal)

    # Vigil: notify AI signal collector of task start
    if ctx._vigil:
        ctx._vigil.ai_signal_collector.task_started(state.goal)

    # Build conversation for the task - resume from checkpoint if available
    _had_checkpoint_conv = ctx._resume_conv is not None
    if ctx._resume_conv is not None:
        conv = list(ctx._resume_conv)
        ctx._resume_conv = None  # consume once
        # Append resumption hint so the model knows it was interrupted
        conv.append({
            "role": "system",
            "content": f"[Resuming task '{state.goal}' from step {state.step}. You were interrupted mid-execution. Review the conversation and continue where you left off.]"
        })
        logger.info("Task %s: resuming from checkpoint with %d messages at step %d",
                    state.task_id, len(conv), state.step)
    else:
        conv = [
            {"role": "system", "content": ctx.system_prompt},
            {"role": "user", "content": f"[TASK] {state.goal}"},
        ]
    # If resuming from crash WITHOUT checkpoint conversation, add basic resume hint
    if state.step > 0 and not _had_checkpoint_conv:
        conv.append({
            "role": "system",
            "content": f"[Resuming task from step {state.step}. "
                       f"Plan: {json.dumps(state.plan)}. "
                       f"Last errors: {json.dumps(state.errors[-3:] if state.errors else [])}. "
                       f"Continue from where you left off.]"
        })
    elif state.plan:
        conv.append({
            "role": "system",
            "content": f"[Task plan: {json.dumps(state.plan)}]"
        })

    # Auto-skill lookup: search learned workflows for a matching skill.
    # If found, inject it as a system message so the AI starts with
    # proven methodology instead of rediscovering from scratch.
    _matched_skill = None
    if ctx.skill_loader and state.goal:
        try:
            _matched_skill = ctx.skill_loader.find_matching_skill(state.goal)
        except Exception:
            _matched_skill = None
    if _matched_skill:
        conv.append({
            "role": "system",
            "content": (
                "[SKILL MATCHED] You have previously completed a similar task. "
                "Use this learned workflow as your starting point:\n\n"
                + _matched_skill.get("content", "")[:1500]
            )
        })
        logger.info("Task %s: matched skill '%s' injected into conversation",
                    state.task_id, _matched_skill.get("name", "unknown"))

    # Run the task loop
    _family = state.model_family or None
    session_id = f"task-{state.task_id}"

    # Initialize context compactor for long conversations (replaces ContextManager)
    cmgr = ctx.compactor if ContextCompactor is not None and ctx.compactor is not None else None
    if cmgr is not None:
        logger.info("Task %s: compactor active (max %d tokens)",
                    state.task_id, cmgr.max_tokens)
        # Load any persisted summary for task continuity
        try:
            prev_summary = cmgr.load_summary(session_id)
            if prev_summary:
                logger.info("Task %s: loaded persisted summary (%d chars)",
                            state.task_id, len(prev_summary))
                # Inject summary into conversation if not already present
                has_summary = any(
                    m.get("content", "").startswith("[Context summary]")
                    for m in conv
                )
                if not has_summary:
                    conv.insert(1, {
                        "role": "system",
                        "content": f"[Context summary]\n{prev_summary}",
                    })
        except Exception:
            pass

    for step in range(state.step, state.max_steps):
        state.step = step
        state.touch()

        # Track for heartbeat
        ctx._current_task_id = state.task_id
        ctx._current_task_step = step

        # Save pre-step checkpoint (raw conversation for replayability)
        if ctx.checkpoint:
            try:
                ctx.checkpoint.save(
                    description=f"task-{state.task_id}-pre-step-{step}",
                    session_messages=conv,
                    raw_messages=list(conv),
                    session_id=session_id,
                    iteration=step,
                )
            except Exception:
                pass

        # Call model -- with context compression for long conversations
        try:
            # Build compressed context if context compactor is active
            _call_conv = conv
            if cmgr is not None:
                try:
                    _llm_fn = ctx.llm.call if hasattr(ctx.llm, 'call') else None
                    _call_conv = cmgr.compact_if_needed(conv, _llm_fn, session_id=session_id) if _llm_fn else cmgr.compact(conv, lambda x: "")
                except Exception:
                    pass

            # Adapt messages for target model family
            if ctx._msg_adapter:
                try:
                    _family_name = _family or "default"
                    _call_conv = ctx._msg_adapter.adapt(
                        _call_conv,
                        model_family=_family_name,
                        task_locked=True,
                    )
                except Exception:
                    pass

            # Call with 429 backoff support
            _trace_t0 = time.time()
            _attempt = 0
            _max_attempts = 3
            response = None
            while _attempt < _max_attempts:
                try:
                    response = ctx.llm.call(_call_conv, tools=TOOL_SCHEMAS, preferred_family=_family)
                    break
                except Exception as e:
                    _attempt += 1
                    if "429" in str(e) or "rate" in str(e).lower():
                        logger.warning("Task %s: 429 at step %d (attempt %d/%d)",
                                     state.task_id, step, _attempt, _max_attempts)
                        if ctx._msg_adapter:
                            ctx._msg_adapter.record_429(_family or "default", _attempt)
                        if _attempt < _max_attempts:
                            time.sleep(2 ** _attempt)  # exponential backoff (time already at module level)
                            continue
                    if _attempt >= _max_attempts:
                        raise

            if response is None:
                raise RuntimeError("Model call returned None after retries")
            if _family is None and hasattr(response, 'provider_family') and response.provider_family:
                # Only lock non-MIMO families for session affinity.
                # MIMO is too slow - always fall back to full provider pool.
                if response.provider_family != "mimo":
                    _family = response.provider_family
                    state.model_family = _family

            # Trace: log successful LLM call
            try:
                _last_user = ""
                for _m in reversed(_call_conv):
                    if _m.get("role") == "user":
                        _raw = _m.get("content", "")
                        _last_user = _raw if isinstance(_raw, str) else str(_raw)
                        break
                _trace_latency = (time.time() - _trace_t0) * 1000
                _provider = getattr(response, 'provider_name', '') or 'unknown'
                _content = response.get("content", "") or ""
                ctx.trace_logger.log_event(TraceEvent(
                    trace_id=ctx._current_trace_id,
                    event_type="llm_call",
                    provider=_provider,
                    latency_ms=round(_trace_latency, 2),
                    input_summary=_last_user[:200],
                    output_summary=_content[:200],
                ))
            except Exception:
                pass
        except Exception as e:
            if ctx.error_logger and ErrorCategory is not None:
                try:
                    ctx.error_logger.log(ErrorCategory.EXECUTION,
                        f"Task {state.task_id} model call failed at step {step}", exc=e)
                except Exception:
                    pass
            logger.error("Task %s model call failed at step %d: %s", state.task_id, step, e)
            # Trace: log LLM error
            try:
                ctx.trace_logger.log_event(TraceEvent(
                    trace_id=ctx._current_trace_id,
                    event_type="llm_error",
                    provider="",
                    latency_ms=0,
                    input_summary="",
                    output_summary=str(e)[:200],
                    metadata={"step": step, "error_type": type(e).__name__},
                ))
            except Exception:
                pass
            state.add_error("model_call", str(e), step)
            save_state(state, workspace=ctx.workspace)
            # Retry once
            try:
                response = ctx.llm.call(conv, tools=TOOL_SCHEMAS, preferred_family=_family)
            except Exception:
                fail_task(state, f"Model call failed twice at step {step}: {e}",
                          workspace=ctx.workspace)
                return

        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        if tool_calls:
            # Format and append assistant message
            formatted_tcs = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": json.dumps(tc.get("args", {}))}}
                for tc in tool_calls
            ]
            conv.append({"role": "assistant", "content": content or "", "tool_calls": formatted_tcs})

            responded = set()
            for tc in tool_calls:
                name = tc.get("name", "?")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")

                # Check for task completion signal
                if name in ("chat_send", "reply", "task_complete", "end_task"):
                    # DecisionEngine: verify results before marking complete
                    if ctx.decision_engine and getattr(ctx.decision_engine, '_enabled', False):
                        try:
                            passed, issues = ctx.decision_engine.verify_results(
                                state.context_window.recent_actions, content
                            )
                            if not passed:
                                logger.warning("Task %s: verification issues: %s", state.task_id, issues)
                                conv.append({
                                    "role": "tool", "tool_call_id": tc_id,
                                    "content": f"[VERIFY FAILED] Issues: {'; '.join(issues[:3])}. Continue or retry.",
                                })
                                responded.add(tc_id)
                                continue
                        except Exception:
                            pass

                    state.add_action({"action": "complete", "tool": name, "step": step})
                    state.status = "completed"
                    save_state(state, workspace=ctx.workspace)
                    ctx.skill_extractor.end_task(True)
                    logger.info("Task %s: completed via %s at step %d", state.task_id, name, step)
                    # Memory evolution: record completed task experience
                    ctx._task_counter += 1
                    if ctx.memory_evolver is not None:
                        try:
                            ctx.memory_evolver.evolve(
                                experience=f"Completed: {state.goal}"
                            )
                            if ctx._task_counter % 50 == 0:
                                ctx.memory_evolver.consolidate()
                                ctx.memory_evolver.decay()
                                logger.info("MemoryEvolver: consolidated & decayed after %d tasks",
                                           ctx._task_counter)
                        except Exception:
                            pass
                    # Save session state on task completion
                    try:
                        if ctx.sessions and hasattr(ctx.sessions, 'save_messages'):
                            ctx.sessions.save_messages(session_id, conv)
                    except Exception:
                        pass
                    # Vigil: notify AI signal collector of task completion
                    if ctx._vigil:
                        ctx._vigil.ai_signal_collector.task_completed()
                    return

                # Constitution check (aligned with handle_message)
                if ctx.constitution:
                    try:
                        const_result = ctx.constitution.check_action(name, context=args, mode="write")
                        if not const_result.allowed:
                            if const_result.action.value == "reject":
                                conv.append({
                                    "role": "tool", "tool_call_id": tc_id,
                                    "content": f"[CONSTITUTION BLOCKED] {name}: {const_result.reason}",
                                })
                                responded.add(tc_id)
                                continue
                            elif const_result.action.value in ("warn", "warning_first"):
                                logger.warning("[Constitution] Warning on %s: %s", name, const_result.reason)
                        elif const_result.is_warning:
                            logger.warning("[Constitution] First-warning on %s: %s", name, const_result.reason)
                    except Exception:
                        pass

                # DecisionEngine: tool strategy enforcement + snapshot (respects _enabled)
                if ctx.decision_engine and getattr(ctx.decision_engine, '_enabled', False):
                    try:
                        allowed, reason = ctx.decision_engine.check_tool_strategy(name, step)
                        if not allowed:
                            conv.append({
                                "role": "tool", "tool_call_id": tc_id,
                                "content": f"[STRATEGY BLOCKED] {name}: {reason}",
                            })
                            responded.add(tc_id)
                            continue
                        # Save snapshot before each write-type tool for rollback
                        ctx.decision_engine.save_snapshot(conv, step)
                    except Exception:
                        pass

                # Execute tool
                _tool_t0 = time.time()
                try:
                    # Preferred: ToolExecutor-based dispatch (from tool_registry.py)
                    if ctx._tool_executor is not None:
                        try:
                            _instruction = json.dumps({"tool": name, "params": args})
                            _tool_result = asyncio.run(ctx._tool_executor.dispatch(_instruction))
                            if _tool_result.success:
                                result = _tool_result.data or {}
                            else:
                                raise RuntimeError(_tool_result.error or "Tool failed")
                        except Exception as _te:
                            # Fall back to legacy dispatch if ToolExecutor fails
                            logger.warning("ToolExecutor dispatch failed for %s: %s, falling back to execute()",
                                         name, _te)
                            result = execute(name, args, base_dir=ctx.workspace)
                    else:
                        result = execute(name, args, base_dir=ctx.workspace)
                    ctx.skill_extractor.record_tool_call(
                        name, args, str(result)[:200],
                        is_error=isinstance(result, dict) and bool(result.get("error")),
                    )
                    # Trace: log tool execution
                    try:
                        _tool_latency = (time.time() - _tool_t0) * 1000
                        _rsum = str(result)[:200]
                        ctx.trace_logger.log_event(TraceEvent(
                            trace_id=ctx._current_trace_id,
                            event_type="tool_exec",
                            provider="",
                            latency_ms=round(_tool_latency, 2),
                            input_summary=f"{name}: {str(args)[:150]}",
                            output_summary=_rsum,
                        ))
                    except Exception:
                        pass
                except Exception as e:
                    if ctx.error_logger and ErrorCategory is not None:
                        try:
                            ctx.error_logger.log(ErrorCategory.EXECUTION,
                                f"Tool {name} failed", exc=e, worker=ctx.name,
                                task_id=state.task_id)
                        except Exception:
                            pass
                    # Trace: log tool error
                    try:
                        ctx.trace_logger.log_event(TraceEvent(
                            trace_id=ctx._current_trace_id,
                            event_type="tool_error",
                            provider="",
                            latency_ms=0,
                            input_summary=f"{name}: {str(args)[:150]}",
                            output_summary=str(e)[:200],
                            metadata={"step": step, "error_type": type(e).__name__},
                        ))
                    except Exception:
                        pass
                    state.add_error(f"tool:{name}", str(e), step)
                    conv.append({
                        "role": "tool", "tool_call_id": tc_id,
                        "content": f"[ERROR] {name}: {e}",
                    })
                    responded.add(tc_id)
                    continue

                formatted = format_result(name, result)
                conv.append({"role": "tool", "tool_call_id": tc_id, "content": formatted})
                responded.add(tc_id)

                state.add_action({
                    "step": step,
                    "tool": name,
                    "args_summary": str(args)[:200],
                    "result_summary": str(result)[:200],
                    "success": "error" not in str(result).lower() if isinstance(result, dict) else True,
                    "error": result.get("error", "") if isinstance(result, dict) else "",
                })

            # Fill missing responses
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                if tc_id not in responded:
                    conv.append({
                        "role": "tool", "tool_call_id": tc_id,
                        "content": "[interrupted]",
                    })

            # Use ContextCompactor for intelligent compression (replaces ContextManager)
            if cmgr is not None and cmgr.should_compact(conv):
                try:
                    _before = len(conv)
                    _llm_fn = ctx.llm.call if hasattr(ctx.llm, 'call') else None
                    conv = cmgr.compact_if_needed(conv, _llm_fn, session_id=session_id) if _llm_fn else cmgr.compact(conv, lambda x: "")
                    logger.debug("Task %s: compressed context to %d messages (was %d)",
                                 state.task_id, len(conv), _before)
                except Exception as e:
                    logger.warning("ContextCompactor build failed, falling back to crude truncation: %s", e)
                    # Fallback: keep system + goal + recent window
                    system = conv[0] if conv[0].get("role") == "system" else None
                    keep = [conv[0], conv[1]] if system else [conv[0]]
                    keep.extend(conv[-30:])
                    conv = keep
        else:
            # Text response - check for [TASK_COMPLETE] signal first
            conv.append({"role": "assistant", "content": content})
            if "[TASK_COMPLETE]" in content:
                state.status = "completed"
                save_state(state, workspace=ctx.workspace)
                if ctx.checkpoint:
                    try:
                        cps = ctx.checkpoint.list_checkpoints(status="incomplete")
                        for cp in cps:
                            ctx.checkpoint.mark_complete(cp["id"])
                    except Exception:
                        pass
                logger.info("Task %s: completed ([TASK_COMPLETE]) at step %d",
                            state.task_id, step)
                if ctx._vigil:
                    ctx._vigil.ai_signal_collector.task_completed()
                return

            # Text response without [TASK_COMPLETE] - heuristic completion detection
            _content_len = len(content.strip()) if content else 0
            _has_substantial_answer = _content_len > 100
            _late_in_loop = step >= 3

            if _has_substantial_answer and _late_in_loop:
                # Model gave a substantial answer but didn't signal completion.
                # Auto-complete: the answer IS the completion.
                logger.info(
                    "Task %s: auto-completing at step %d (substantial text answer, %d chars)",
                    state.task_id, step, _content_len
                )
                state.status = "completed"
                state.add_action({"action": "complete", "tool": "text_answer", "step": step})
                save_state(state, workspace=ctx.workspace)
                if ctx._vigil:
                    ctx._vigil.ai_signal_collector.task_completed()
                return
            elif _has_substantial_answer:
                # Early substantial answer - inject hint to use completion signal
                conv.append({
                    "role": "system",
                    "content": (
                        "[HINT] You appear to have answered. To complete this task, "
                        "call the `end_task` tool with success=true, or include "
                        "[TASK_COMPLETE] in your next text response."
                    ),
                })
            elif step >= 5:
                # Too many steps with no substantial answer - force completion
                logger.warning("Task %s: force-completing at step %d (no progress)",
                               state.task_id, step)
                state.status = "completed"
                state.add_action({"action": "complete", "tool": "force_timeout", "step": step})
                save_state(state, workspace=ctx.workspace)
                if ctx._vigil:
                    ctx._vigil.ai_signal_collector.task_completed()
                return
            # Otherwise: continue loop (model still trying tools)

        # Save state after each step
        save_state(state, workspace=ctx.workspace)

        # Memory management: profile + GC
        if ctx._memprof and step % ctx._memprof.sample_interval == 0:
            try:
                ctx._memprof.sample(step)
            except Exception:
                pass
        if force_gc_collect and step % ctx.memory_gc_interval == 0 and step > 0:
            try:
                force_gc_collect()
            except Exception:
                pass
        # Periodic session_family cleanup (prevents unbounded dict growth)
        if step % 50 == 0 and step > 0:
            ctx._session_family = {
                k: v for k, v in ctx._session_family.items()
                if k.startswith("task-") or k in list(ctx._session_family.keys())[-20:]
            }

        # Update context manager tracking
        if cmgr is not None:
            try:
                cmgr.after_step(conv, step)
            except Exception:
                pass

        # Save post-step checkpoint (raw conversation for replayability)
        if ctx.checkpoint:
            try:
                ctx.checkpoint.save(
                    description=f"task-{state.task_id}-post-step-{step}",
                    session_messages=conv,
                    raw_messages=list(conv),
                    session_id=session_id,
                    iteration=step,
                )
            except Exception:
                pass

        # Memory check
        if getattr(ctx, '_schedule_restart', False):
            logger.warning("Task %s: memory limit reached at step %d -- saving and restarting",
                           state.task_id, step)
            save_state(state, workspace=ctx.workspace)
            import subprocess
            subprocess.Popen(["systemctl", "restart",
                              os.environ.get("SERVICE_NAME", "unified-worker")])
            return

        # Stuck detection
        if state.is_stuck():
            fail_task(state, f"Task stuck at step {step}: repeated errors or step limit",
                      workspace=ctx.workspace)
            logger.warning("Task %s: failed (stuck at step %d)", state.task_id, step)
            return

    # Reached max_steps without completion
    fail_task(state, f"Reached max_steps ({state.max_steps}) without completion",
              workspace=ctx.workspace)
    logger.warning("Task %s: failed (max steps %d)", state.task_id, state.max_steps)
