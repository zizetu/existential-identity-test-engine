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

"""Shared state context for the unified worker.

Holds every mutable cross-cutting attribute that was formerly `self.xxx` on Worker.
Handler modules take `(ctx: SharedContext, ...)` instead of being methods on Worker.
This makes the god-object split possible without breaking shared state access.

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("EITElite.worker")


def _get_rss_mb() -> float:
    """Get current process RSS in MB."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


@dataclass
class SharedContext:
    """All shared mutable state for the worker loop.

    Attributes grouped by concern. Worker.__init__ populates this once
    and passes it to all handler modules.
    """

    # ── Core identity ──────────────────────────────────────────────
    cfg: dict  # Full worker configuration dict (YAML-loaded)
    name: str  # Agent identity name (e.g. "eitelite", "seoul")
    workspace: str  # Filesystem root for all agent file operations

    # ── Channels ───────────────────────────────────────────────────
    channels: list = field(default_factory=list)  # Active I/O channels (Telegram, CLI, etc.)
    _msg_count: int = 0  # Total messages processed since worker start

    # ── LLM & routing ──────────────────────────────────────────────
    llm: Any = None  # ModelFailover instance (multi-provider LLM router)
    _msg_adapter: Any = None  # MessageAdapter for provider format normalization
    _session_family: dict = field(default_factory=dict)  # Family-group routing map
    _session_power: dict = field(default_factory=dict)  # {session_id: True} for admin-unlocked sessions
    system_prompt: str = ""  # Rendered system prompt for the current turn
    _consecutive_failures: int = 0  # Consecutive LLM call failures (for circuit breaker)
    _circuit_open_until: float = 0.0  # Timestamp until which circuit breaker stays open

    # ── Tool execution ─────────────────────────────────────────────
    _tool_registry: Any = None  # Tool registry (name → handler mapping)
    _tool_executor: Any = None  # Tool executor (invocation + security gating)

    # ── Trace & observability ──────────────────────────────────────
    trace_logger: Any = None  # TraceLogger for structured JSONL tracing
    _current_trace_id: str = ""  # UUID prefix for the current turn's trace
    tracer: Any = None  # TraceRecorder for training-data capture

    # ── Error handling ─────────────────────────────────────────────
    error_logger: Any = None  # ErrorLogger for structured error aggregation

    # ── Conversation & sessions ────────────────────────────────────
    sessions: Any = None  # SessionManager for per-user chat history
    compactor: Any = None  # ContextCompactor for token-limit management
    _resume_conv: Optional[list] = None  # Checkpoint-resumed conversation messages

    # ── Verification & safety ──────────────────────────────────────
    verification: Any = None  # VerificationEngine for identity-bound checks
    verif_recorder: Any = None  # VerificationEventRecorder for audit trail
    constitution: Any = None  # ConstitutionEnforcer for behavior boundaries
    truth_reporter: Any = None  # TruthReporter for cross-checking claims
    decision_engine: Any = None  # DecisionEngine for structured reasoning pipeline
    _permission_checker: Any = None  # PermissionChecker for 5-tier mode gating

    # ── Async event loop ───────────────────────────────────────────
    _loop: Any = None  # Reusable asyncio event loop (avoids asyncio.run leaks)

    # ── Loop / doom detection ──────────────────────────────────────
    doom_detector: Any = None  # DoomLoopDetector for stuck-agent protection
    loop_detector: Any = None  # Legacy loop detection for repeated actions

    # ── Checkpoint & recovery ──────────────────────────────────────
    checkpoint: Any = None  # CheckpointManager for crash recovery snapshots
    self_repair: Any = None  # SelfRepairEngine for auto-healing

    # ── Task state ─────────────────────────────────────────────────
    _pending_task_file: Path = field(default_factory=Path)  # File path for pending task persistence
    _pending_task: Optional[dict] = None  # Currently queued asynchronous task
    _evidence_retry_count: int = 0  # Retry counter for evidence-gathering steps
    _current_task_id: str = ""  # ID of the currently executing task
    _current_task_step: int = 0  # Step index within the current task
    _task_counter: int = 0  # Global task sequence counter

    # ── Memory management ──────────────────────────────────────────
    memory_evolver: Any = None  # MemoryEvolver for long-term knowledge refinement
    memory_store: Any = None  # MemoryFTSStore for full-text search over past sessions
    _memprof: Any = None  # MemoryProfiler for RSS tracking and GC triggers
    _schedule_restart: bool = False  # Flag: True when restart is needed due to memory pressure

    # ── Skills ─────────────────────────────────────────────────────
    skill_extractor: Any = None  # SkillExtractor for auto-extracting workflows
    skill_loader: Any = None  # SkillLoader for dynamic skill injection
    skill_curator: Any = None  # SkillCurator for background lifecycle management

    # ── Active modules ─────────────────────────────────────────────
    _active_modules: dict = field(default_factory=dict)  # {name: instance} for successfully loaded modules

    # ── Heartbeat ──────────────────────────────────────────────────
    _heartbeat_file: Path = field(default_factory=Path)  # Touch file for watchdog liveness checks
    _start_time: float = 0.0  # Unix timestamp of worker process start

    # ── Optional off-by-default modules ────────────────────────────
    usage: Any = None  # UsageTracker for API token/cost accounting
    _vigil: Any = None  # Vigil instance for periodic security patrols
    _vigil_patrol_interval: int = 300  # Seconds between vigil patrol cycles
    _last_patrol: float = 0.0  # Timestamp of last vigil patrol run
    sandbox: Any = None  # SandboxExecutor for isolated code execution
    reflection: Any = None  # ReflectionEngine for post-task self-analysis

    _failover_mod: Any = None  # ProviderFailover for multi-model resilience
    _verify_broadcast: Any = None  # VerifyBroadcast for multi-model consensus
    security_baseline: Any = None  # SecurityBaseline for path/URL/redaction enforcement
    cron: Any = None  # CronManager for scheduled periodic tasks

    # ── Memory limits (moved from module globals) ──────────────────
    memory_limit_mb: int = 1000  # RSS threshold (MB) that triggers restart scheduling
    memory_check_interval: int = 100  # Steps between memory pressure checks
    memory_gc_interval: int = 20  # Steps between forced garbage collections

    # ── Tool limits ────────────────────────────────────────────────
    max_tool_iterations: int = 8  # Hard cap on tool calls per turn
    soft_hint_at: int = 5  # Emit a soft hint after this many tool calls
    hard_stop_at: int = 8  # Force-stop the turn after this many tool calls

    def __post_init__(self):
        if not self._start_time:
            self._start_time = time.time()

    # ── Convenience methods ────────────────────────────────────────

    def new_trace_id(self) -> str:
        """Generate a new trace ID for a turn."""
        tid = str(uuid.uuid4())[:8]
        self._current_trace_id = tid
        return tid

    def get_rss_mb(self) -> float:
        """Get current process RSS in MB."""
        return _get_rss_mb()

    def check_memory(self) -> bool:
        """Return True if memory limit exceeded and restart is needed."""
        rss = self.get_rss_mb()
        if rss > self.memory_limit_mb:
            logger.warning(
                "[memory] RSS %.0fMB > %dMB - scheduling restart",
                rss, self.memory_limit_mb,
            )
            self._schedule_restart = True
            return True
        return False

    def session_cleanup(self):
        """Prevent unbounded _session_family and _session_power growth."""
        if len(self._session_family) > 30:
            keys = list(self._session_family.keys())
            self._session_family = {
                k: v for k, v in self._session_family.items()
                if k.startswith("task-") or k in keys[-20:]
            }
        if len(self._session_power) > 500:
            keys = list(self._session_power.keys())
            self._session_power = {
                k: v for k, v in self._session_power.items()
                if k in keys[-200:]
            }

    def session_family_cleanup(self):
        """Prevent unbounded session_family growth."""
        if len(self._session_family) > 30:
            keys = list(self._session_family.keys())
            self._session_family = {
                k: v for k, v in self._session_family.items()
                if k.startswith("task-") or k in keys[-20:]
            }


    def run_async(self, coro):
        """Run an async coroutine synchronously using the worker's persistent event loop.

        Reuses ctx._loop (set by Worker during init) to avoid the asyncio.run()
        create-destroy cycle that breaks aiohttp sessions and other persistent
        async resources.  Falls back to asyncio.run() only when no loop is
        available (e.g. during unit tests or standalone CLI usage).

        Uses create_task() + run_until_complete() so that aiohttp 3.13+
        internal asyncio.timeout() works correctly (requires Task context).

        Args:
            coro: An awaitable coroutine object.

        Returns:
            The coroutine's return value.
        """
        import asyncio
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._loop = loop
        task = loop.create_task(coro)
        return loop.run_until_complete(task)

    def force_gc(self):
        """Force garbage collection."""
        import gc
        gc.collect()
