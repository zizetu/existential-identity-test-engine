# EMERGENCY FAST_ACK 2026-07-09c: typing ack + no-tool short replies + queue hygiene
# EMERGENCY LOAD_SESSION 2026-07-09: reload SQLite history on new in-memory sessions
# EMERGENCY 2026-07-09: throttle progress spam; longer hard timeout; pair with MemoryBoot.get_memory
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

# [ANCHOR] STRATEGIC CONTEXT — Read STRATEGY.md before making changes.
# This is the main worker entry for tical-agent (lite edition, in development).
#
# Sister project: eite-agent (full EITE edition, zizetu/eite-agent) — DO NOT MIX CODE.
# Security audit completed 2026-07-03 in sync with eite-agent.

from __future__ import annotations
from .paths import under_tical_home
#

# provenance:tic-al-agent
"""
tical-code unified worker - main orchestrator loop.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKER LIFECYCLE (the core loop, executed inside Worker.run())
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Resume active tasks  – autonomous continuation of any task
     that was interrupted mid-flight (saved to disk).
  2. Poll channels         – fetch new messages from all registered
     channels (Telegram, tical-chat).  All channels are polled
     concurrently via asyncio.gather so one slow poll never blocks
     another channel.
  3. LLM call              – build context from session history,
     inject system prompt, call the LLM backend (with multi-provider
     failover via ModelFailover).  Each session runs in its own
     asyncio Task so a slow LLM never blocks another session.
  4. Tool execute          – parse LLM response for tool-call blocks,
     dispatch to the tool executor via loop.run_in_executor (async),
     feed results back into the conversation loop.
     Concurrent-safe tools execute in parallel via the default thread
     pool; non-concurrent tools run one-by-one to avoid state races.
  5. Format                – convert the final LLM completion to a
     clean, markdown-formatted response.
  6. Reply                 – send the response back through the
     originating channel.

CHANNEL ARCHITECTURE:
  - TelegramChannel    – polls Telegram Bot API (long-polling via
    python-telegram-bot or raw HTTP getUpdates).
  - TicalChatChannel   – polls a standalone tical-chat HTTP API
    with shared-key authentication and identity binding.
  Multiple channels are polled concurrently via asyncio.gather
  each loop tick, so a slow Telegram poll never blocks TicalChat.

MODULE LOADING PIPELINE:
  Modules are registered via @register decorators in
  tical_code.core.module_defs.  Worker.__init__ calls load_modules()
  which introspects the registry, respecting the configured profile
  ("full" vs "light").  Each module receives a reference to self
  (the Worker) and attaches itself as an attribute.

SKILL SYSTEM:
  SkillExtractor auto-learns workflows from completed tasks and
  persists them to disk.  SkillLoader injects up to 5 learned
  skills into the system prompt at startup, so the AI can reuse
  proven strategies without re-learning each time.

CMD PROTOCOL:
  Messages starting with [CMD] are intercepted before the LLM
  sees them.  Each command has a minimum permission level
  (MASTER / ADMIN / WORKER) enforced by mapping sender IDs to
  levels.  Supported commands: deploy, status, restart, exec,
  report, escalate, ping, help, log, switch_model.

THREAD SAFETY:
  This module is single-threaded by design - one asyncio event
  loop, one Worker instance.  Channel polling, LLM calls, and
  tool execution all run synchronously within the same loop.

Replaces ticobot_worker_v0.10.0.py and worker_loop.py (pre-1.0
monolithic design).
"""
import asyncio
import gc
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# Add parent dir to path for imports (append avoids shadowing stdlib)
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import uuid as _uuid

from tical_code.core.channel import Message, Response, TelegramChannel, TicalChatChannel
from tical_code.core.trace import TraceLogger, TraceEvent
from tical_code.core.model_failover import ModelFailover
from tical_code.core.provider_registry import from_registry as failover_from_env
from tical_code.core.llm_backend import create_llm_backend  # fallback
from tical_code.core.tool_executor import execute, TOOL_SCHEMAS, TOOL_CONCURRENCY_MAP
from tical_code.core.response_formatter import format_result, format_progress, format_final_reply
from tical_code.core.prompt import build_system_prompt
from tical_code.core.config import load_config
from tical_code.core.modules.session_manager import SessionManager as SessionManagerSQLite
from tical_code.core.modules.context_compactor import ContextCompactor
from tical_code.core.doom_loop import DoomLoopDetector, DoomLoopConfig, LoopLevel as DoomLoopLevel
from tical_code.core.module_registry import load_modules, get_active_descriptions
from tical_code.core.skill_extractor import SkillExtractor
from tical_code.core.skill_loader import SkillLoader
from tical_code.core.skill_curator import SkillCurator
import tical_code.core.module_defs  # noqa: F401 - registers all modules
from tical_code.core.usage import UsageTracker
try:
    from tical_code.core.self_repair import SelfRepairEngine
except ImportError:
    SelfRepairEngine = None
try:
    from tical_code.core.checkpoint import CheckpointManager
except ImportError:
    CheckpointManager = None
try:
    from tical_code.core.sandbox import SandboxExecutor
except ImportError:
    SandboxExecutor = None
try:
    from tical_code.core.reflection import ReflectionEngine, ReflectionConfig
except ImportError:
    ReflectionEngine = None; ReflectionConfig = None
try:
    from tical_code.core.memory_store import MemoryFTSStore
except ImportError:
    MemoryFTSStore = None
try:
    from tical_code.core.session_snapshot import save_snapshot, load_latest_snapshot, record_death
except ImportError:
    save_snapshot = None; load_latest_snapshot = None; record_death = None
try:
    from tical_code.core.modules.sustained_task import SustainedTaskManager
except ImportError:
    SustainedTaskManager = None

try:
    from tical_code.core.modules.self_evolve import SelfEvolveEngine
except ImportError:
    SelfEvolveEngine = None

try:
    from tical_code.core.decision_engine import DecisionEngine
except ImportError:
    DecisionEngine = None

# Permission mode system
from tical_code.core.permission_checker import PermissionChecker, PermissionMode

try:
    from tical_code.core.errors import ErrorLogger, ErrorCategory
except ImportError:
    ErrorLogger = None; ErrorCategory = None

# MemoryEvolver is initialized once via module_defs (_init_memory_evolve → attr memory_evolver)

try:
    from tical_code.core.tool_registry import ToolRegistry, ToolExecutor, ToolDefinition
    from tical_code.core.builtin_tools import register_builtin_tools_sync
    _TOOL_REGISTRY_AVAILABLE = True
except ImportError:
    ToolRegistry = None; ToolExecutor = None; ToolDefinition = None
    register_builtin_tools_sync = None
    _TOOL_REGISTRY_AVAILABLE = False

try:
    from tical_code.core.task_state import (
        TaskState, create_task, load_state, save_state,
        list_active_tasks, complete_task, fail_task, is_task_request,
        cleanup_completed as task_cleanup_completed,
    )
except ImportError:
    TaskState = None; create_task = None; load_state = None; save_state = None
    list_active_tasks = None; complete_task = None; fail_task = None
    is_task_request = None; task_cleanup_completed = None

try:
    from tical_code.core.memory_profiler import MemoryProfiler, force_gc_collect
except ImportError:
    MemoryProfiler = None; force_gc_collect = None

try:
    from tical_code.core.message_adapter import MessageAdapter
except ImportError:
    MessageAdapter = None

logger = logging.getLogger("tical-code.worker")


# ─────────────────────────────────────────────────────────────
# SECTION: Memory Monitoring & Limits
# ─────────────────────────────────────────────────────────────

def _get_rss_mb() -> float:
    """Return the current process resident set size (RSS) in megabytes.

    Attempts to read from /proc/self/status (Linux only); falls back
    to resource.getrusage() on other platforms.  Returns 0.0 if all
    attempts fail.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # KB -> MB
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0

_MEMORY_LIMIT_MB = 700  # 15% below systemd MemoryMax 2G for headroom
_MEMORY_CHECK_INTERVAL = 100
_MEMORY_GC_INTERVAL = 20   # force GC every N messages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# Restore bash_execute in tool schema - AI needs shell access

# Handler modules (extracted from Worker for modularity)
from tical_code.core.shared_context import SharedContext
from tical_code.core.modules.task_handler import (
    run_task, load_pending as _load_pending_ctx, save_pending as _save_pending_ctx,
)
from tical_code.core.modules.message_handler import (
    handle_message as _handle_message_ctx,
)
from tical_code.core.workspace import Workspace
TOOL_SCHEMAS_CLEAN = TOOL_SCHEMAS  # Use full schema with bash_execute

# ─────────────────────────────────────────────────────────────
# SECTION: Tool Call Limits
# ─────────────────────────────────────────────────────────────
# Tool call limits
MAX_TOOL_ITERATIONS = 50  # Safety fuse -- model stops when work is done

# ─────────────────────────────────────────────────────────────
# SECTION: Worker Class (Central Orchestrator)
# ─────────────────────────────────────────────────────────────

class Worker:
    """Central orchestrator for the tical-code AI worker.

    The Worker is the top-level runtime: it wires every subsystem
    together and runs the main poll→dispatch→reply loop indefinitely.

    Responsibilities at initialization (__init__):
      * Trace system    – persistent observability ring-buffer
      * Channels        – Telegram + tical-chat polling endpoints
      * Error logging   – structured log rotation via ErrorLogger
      * Memory evolver  – autonomous memory pruning/evolution
      * Tool registry   – builtin tool registration + execution
      * LLM backend     – multi-provider ModelFailover for resilience
      * Module loading  – registry-based plug-in system (@register)
      * Skill system    – SkillExtractor + SkillLoader for learned workflows
      * System prompt   – build_system_prompt() + EITE identity + skills + axioms
      * SharedContext   – single mutable state bag for handler modules
      * Signal handlers – SIGTERM/SIGINT → graceful shutdown

    The main loop (run()) follows a 1-second tick cadence:
      1. Resume any active tasks from disk
      2. Poll all channels for new messages
      3. Dispatch each message through the full pipeline
      4. Handle pending continuations and memory-triggered restarts
      5. Periodically clean up completed tasks (every 100 ticks)
      6. Write heartbeat file for systemd watchdog (every 60 ticks)
      7. Run Vigil immune-system patrol sweep (every 300s)
    """

    def __init__(self, cfg: dict):
        """Wire every subsystem into a single Worker instance.

        The constructor performs an ordered bootstrap of all runtime
        components.  Failures are non-fatal where possible - optional
        subsystems log a warning and continue with the attribute set
        to None.

        Parameters
        ----------
        cfg : dict
            Configuration dict loaded by tical_code.core.config.
            Required keys: 'name', 'workspace'.
            Optional keys: 'tg_token', 'chat_url', 'chat_key',
            'ai_model', 'ai_key', 'ai_endpoint', 'profile',
            'modules' (sub-dict for per-module toggles).

        Bootstrap order:
          1. Trace logger      - TraceLogger ring buffer
          2. Channels          - TelegramChannel + TicalChatChannel
          3. Error logger      - ErrorLogger with rotation
          4. Memory evolver    - MemoryEvolver ($TICAL_HOME/memory)
          5. Tool registry     - ToolRegistry + builtin tools
          6. LLM backend       - ModelFailover (preferred) or create_llm_backend (fallback)
          7. Pending task file - .pending_task.json in workspace
          8. Module loading    - load_modules() via registry
          9. Skill system      - SkillExtractor + SkillLoader
         10. System prompt     - build_system_prompt + EITE + skills + axioms
         11. Heartbeat file    - /tmp/worker-heartbeat-{name}
         12. SharedContext     - single mutable state bag
         13. Signal handlers   - SIGTERM/SIGINT → graceful shutdown
        """
        self.cfg = cfg
        self.name = cfg['name']
        import logging; self.logger = logging.getLogger(f"tical-code.worker.{self.name}")
        self.workspace = cfg["workspace"]
        self._data_dir = under_tical_home("")

        # ─────────────────────────────────────────────────────
        # SECTION: Trace System
        # ─────────────────────────────────────────────────────
        # Trace system for observability (audit-recommended)
        self.trace_logger = TraceLogger()
        self._current_trace_id = ""

        # ─────────────────────────────────────────────────────
        # SECTION: Channels
        # ─────────────────────────────────────────────────────
        # Channels
        self.channels = []
        if cfg.get("tg_token"):
            self.channels.append(TelegramChannel(cfg["tg_token"]))
            logger.info("Telegram channel ready")
        if cfg.get("chat_url"):
            chat_key = cfg.get("chat_key", "") or ""
            if not chat_key:
                logger.warning("TICAL_CHAT_KEY not set - tical-chat channel will fail")
            self.channels.append(TicalChatChannel(
                base_url=cfg["chat_url"],
                identity=cfg['name'],
                shared_key=chat_key,
            ))
            logger.info(f"tical-chat channel ready ({cfg['chat_url']})")

        # ─────────────────────────────────────────────────────
        # SECTION: Error Logger
        # ─────────────────────────────────────────────────────
        # Structured error logger with rotation (from errors.py)
        self.error_logger = None
        if ErrorLogger is not None:
            try:
                self.error_logger = ErrorLogger(log_dir=under_tical_home("logs"))
                logger.info("ErrorLogger ready: %s", self.error_logger.error_log_path)
            except Exception as e:
                logger.warning("ErrorLogger init failed: %s", e)

        # ─────────────────────────────────────────────────────
        # SECTION: Memory bootstrap (MemoryEvolver via module_defs only)
        # ─────────────────────────────────────────────────────
        # memory_evolver is set by load_modules → _init_memory_evolve (attr_name="memory_evolver")
        self.memory_evolver = None
        _mem_dir = under_tical_home("memory")

        # Bootstrap memory files + cold-start identity load (independent of MemoryEvolver)
        try:
            from tical_code.core.memory_boot import ensure_memory_files
            ensure_memory_files(_mem_dir)
            logger.info("Memory files bootstrapped: %s", _mem_dir)
        except Exception as e:
            logger.warning("Memory files bootstrap failed: %s", e)

        # Boot memory: load identity/memory files into persistent store (TF-IDF index)
        self._memory_boot = None
        self._memory_boot_pending = False
        try:
            from tical_code.core.memory_boot import MemoryBoot
            self._memory_boot = MemoryBoot(
                memory_dir=_mem_dir,
                persistent_memory=getattr(self, 'memory_store', None)
            )
            self._memory_boot_pending = True
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            logger.info("MemoryBoot: cold-start identity/memory deferred to first run()")
        except Exception as e:
            logger.warning("MemoryBoot init failed: %s", e)

        # Build FTS5 index from memory markdown files (memory_sense)
        # Without this, memory_search() returns empty results.
        try:
            from tical_code.core.memory_sense import memory_index
            _indexed = memory_index(_mem_dir)
            logger.info("memory_sense FTS5 index built: %d files", _indexed)
        except Exception as e:
            logger.warning("memory_sense FTS5 index build failed: %s", e)

        self._task_counter = 0
        self.checkpoint = None  # set by CheckpointManager if available
        # Self-repair engine - autonomous health monitoring + auto-recovery
        # On full profile, @register handles initialization; manual init is
        # only needed on light profile (where self_repair is not registered).
        self.self_repair = None
        _profile = self.cfg.get("profile", "full") if isinstance(self.cfg, dict) else "full"
        if SelfRepairEngine is not None and _profile != "full":
            try:
                self.self_repair = SelfRepairEngine(framework=self)
                logger.info("SelfRepairEngine initialized (manual, light profile)")
                try:
                    from tical_code.core.tool_executor import set_self_repair_engine
                    set_self_repair_engine(self.self_repair)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("SelfRepairEngine init failed: %s", e)

        # Health scan tracking (Fix 2: message-count-based periodic health scan)
        self._health_scan_interval = cfg.get("modules", {}).get("health_scan_interval", 50) if isinstance(cfg, dict) else 50
        self._last_health_scan_msg_count = 0

        # ── Checkpoint manager (light profile: manual init) ─────────
        if CheckpointManager is not None and _profile != "full":
            try:
                from tical_code.core.checkpoint import CheckpointConfig
                chk_cfg = CheckpointConfig(workspace=self.cfg.get("workspace", ".") if isinstance(self.cfg, dict) else ".")
                self.checkpoint = CheckpointManager(config=chk_cfg)
                from tical_code.core.tool_executor import set_checkpoint_manager
                set_checkpoint_manager(self.checkpoint)
            except Exception as e:
                logger.debug("CheckpointManager manual init skipped: %s", e)

        # ─────────────────────────────────────────────────────
        # SECTION: Tool Registry
        # ─────────────────────────────────────────────────────
        # Tool registry + executor (from tool_registry.py)
        self._tool_registry = None
        self._tool_executor = None
        if _TOOL_REGISTRY_AVAILABLE and ToolRegistry is not None:
            try:
                self._tool_registry = ToolRegistry()
                self._tool_executor = ToolExecutor(self._tool_registry)
                register_builtin_tools_sync(self._tool_registry)
                logger.info("ToolRegistry ready: %d tools registered",
                           len(self._tool_registry.list_tools()))
            except Exception as e:
                logger.warning("ToolRegistry init failed: %s", e)
                self._tool_registry = None
                self._tool_executor = None

        # ─────────────────────────────────────────────────────
        # SECTION: Session Persistence (SQLite)
        # ─────────────────────────────────────────────────────
        # SQLite-based session store for conversation history persistence
        self.sessions = None
        if SessionManagerSQLite is not None:
            try:
                _session_db = os.path.join(cfg.get("workspace", "."), "sessions.db")
                self.sessions = SessionManagerSQLite(db_path=_session_db)
                logger.info("SessionManagerSQLite ready: %s", _session_db)
            except Exception as e:
                logger.warning("SessionManagerSQLite init failed: %s", e)

        # ─────────────────────────────────────────────────────
        # SECTION: LLM Backend
        # ─────────────────────────────────────────────────────
        # LLM backend - prefer ModelFailover for multi-provider resilience
        failover = None
        try:
            from tical_code.core.provider_registry import from_registry
            failover = from_registry(
                repo_root=cfg.get("workspace", os.getcwd()),
                worker_name=self.name,
            )
        except ImportError:
            logger.info("provider_registry not available, trying from_env")
        except Exception as e:
            logger.warning("from_registry failed: %s, trying from_env", e)

        if failover is None:
            try:
                failover = failover_from_env()
            except Exception as e:
                logger.warning("ModelFailover from_env failed: %s, falling back to create_llm_backend", e)

        if failover is not None:
            self.llm = failover
            from tical_code.core.tool_executor import set_failover
            set_failover(self.llm)
            logger.info("LLM: ModelFailover with %d providers", len(self.llm.providers))
        else:
            self.llm = create_llm_backend(
                model=cfg.get("ai_model", "deepseek-v4-flash"),
                api_key=cfg.get("ai_key", ""),
                base_url=cfg.get("ai_endpoint", ""),
            )

        # Pending task file for cross-poll continuation
        self._pending_task_file = Path(cfg.get("workspace", ".")) / ".pending_task.json"
        self._pending_task = self._load_pending()
        self._auto_resume_task_id = None

        # SustainedTaskManager - persistent task queue with auto-recovery
        if SustainedTaskManager is not None:
            self._sustained_task_mgr = SustainedTaskManager()
            # LIVE WIRE INJECT 2026-07-09f
            try:
                from tical_code.core.tool_executor import set_sustained_task_manager
                set_sustained_task_manager(self._sustained_task_mgr)
            except Exception as _inj_s:
                logger.warning("sustained inject failed: %s", _inj_s)
            self.logger.info("SustainedTaskManager initialized")
        else:
            self._sustained_task_mgr = None
            self.logger.warning("SustainedTaskManager unavailable")

        # SelfEvolveEngine - error pattern tracking and usage insights
        if SelfEvolveEngine is not None:
            self._self_evolve = SelfEvolveEngine(
                db_path=self._data_dir + "/self_evolve.db"
            )
            self.logger.info("SelfEvolveEngine initialized")
            # LIVE WIRE INJECT 2026-07-09f
            try:
                from tical_code.core.tool_executor import set_self_evolve_engine
                set_self_evolve_engine(self._self_evolve)
            except Exception as _inj_e:
                logger.warning("self_evolve inject failed: %s", _inj_e)
        else:
            self._self_evolve = None
            self.logger.warning("SelfEvolveEngine unavailable")

        self._session_family = {}  # session_id -> model family for session-affinity
        self._evidence_retry_count = 0

        # Legacy attributes referenced by worker code but not in registry
        self.loop_detector = None   # legacy loop detector (doom_detector is the active one)
        self.verif_recorder = None  # initialized by registry module, guarded by None checks

        # ─────────────────────────────────────────────────────
        # SECTION: Module Loading
        # ─────────────────────────────────────────────────────
        # Load all optional modules via registry
        profile = "full" if cfg.get("profile", "full") == "full" else "light"
        self._active_modules = load_modules(self, cfg, profile=profile)
        logger.info("Modules loaded: %d active (profile=%s)", len(self._active_modules), profile)

        # Capability descriptions for system prompt (LLM sees active module capabilities)
        self._module_descriptions = get_active_descriptions(self._active_modules)
        logger.info(
            "Active module descriptions: %d for prompt injection",
            len(self._module_descriptions),
        )

        # Verify critical subsystems loaded correctly
        if not getattr(self, 'memory_evolver', None):
            logger.warning("MemoryEvolver not loaded — autonomous memory evolution disabled")
        if not getattr(self, 'memory_store', None):
            logger.warning("MemoryFTSStore not loaded — memory_search will be empty")

        # ── FIX: Re-wire MemoryBoot.persistent_memory now that memory_store exists ──
        if self._memory_boot and getattr(self, 'memory_store', None):
            try:
                from tical_code.core.memory import PersistentMemory
                _pm_db = str(self._memory_dir / "persistent.db") if hasattr(self, '_memory_dir') else None
                if not _pm_db:
                    import os as _os
                    _pm_db = under_tical_home("memory.db")
                self._persistent_memory = PersistentMemory(db_path=_pm_db)
                self._memory_boot.persistent_memory = self._persistent_memory
                logger.info("MemoryBoot: persistent_memory re-wired to PersistentMemory (%s)", _pm_db)
            except Exception as _pm_err:
                # Fallback: create adapter for MemoryFTSStore
                class _FTSAdapter:
                    def __init__(self, fts): self._fts = fts
                    def store(self, key, value, category="", priority=5):
                        return self._fts.save_entry(key, value)
                self._memory_boot.persistent_memory = _FTSAdapter(self.memory_store)
                logger.info("MemoryBoot: persistent_memory wired to MemoryFTSStore adapter (%s)", _pm_err)

        # Cleanup old snapshots to prevent directory bloat
        try:
            from tical_code.core.session_snapshot import cleanup_old_snapshots
            cleanup_old_snapshots(self.name, keep=5)
        except Exception as e:
            logger.debug("Snapshot cleanup skipped: %s", e)

        # Register doom_loop recovery callbacks (Fix 1: close self-healing control loop)
        self._register_doom_loop_recovery_callbacks()

        # Wire memory_store and _vigil into tool_executor (must be AFTER load_modules
        # which sets self.memory_store and self._vigil)
        try:
            from tical_code.core.tool_executor import set_memory_store as te_set_memory_store, set_vigil
            if getattr(self, 'memory_store', None):
                te_set_memory_store(self.memory_store)
                logger.info("MemoryFTSStore wired into tool_executor for memory_search")
            if getattr(self, '_vigil', None) is not None:
                set_vigil(self._vigil)
                logger.info("Vigil wired into tool_executor for output sanitization")
        except Exception as e:
            logger.warning("Failed to wire memory_store/vigil into tool_executor: %s", e)

        # Wire globals into builtin_tools.py for tools that need runtime state
        try:
            from tical_code.core.builtin_tools import set_cron_manager, set_memory_store
            if getattr(self, '_cron', None):
                set_cron_manager(self._cron)
                logger.info("CronManager wired into builtin_tools for cron_add/list/remove")
            if getattr(self, 'memory_store', None):
                set_memory_store(self.memory_store)
                logger.info("MemoryFTSStore wired into builtin_tools for memory_search/save")

            # ── Molecular chain engine v3 (inside existing try/except) ──
            from tical_code.core.molecule import (
                MoleculeEngine, ModelRegistry, AtomRole,
            )
            from tical_code.core.tool_executor import set_molecule_engine

            registry = ModelRegistry()
            if getattr(self, 'llm', None) is not None:
                registry.register_api_provider(
                    name="default-api",
                    failover=self.llm,
                    roles=[
                        AtomRole.REASONER, AtomRole.EXECUTOR,
                        AtomRole.VERIFIER, AtomRole.GUARD,
                        AtomRole.SYNTHESIZER, AtomRole.FORMATTER,
                    ],
                    priority=0,
                    description="Default API provider via ModelFailover",
                )

            # Register local Ollama models (orthos-* models auto-discovered)
            logger.info("DEBUG: entering Ollama registration block")
            try:
                from tical_code.core.molecule_extensions import (
                    register_all_ollama_providers,
                )
                orthos = register_all_ollama_providers(
                    registry,
                    include_all=True,  # also register qwen2.5:1.5b etc.
                )
                if orthos:
                    logger.info(
                        "OllamaRegistry: registered %d local models: %s",
                        len(orthos), ", ".join(orthos),
                    )
                else:
                    logger.info("OllamaRegistry: no local models discovered")
            except Exception as ollama_err:
                logger.warning(
                    "OllamaRegistry: failed to register local models: %s",
                    ollama_err,
                )

            self._molecule_engine = MoleculeEngine(registry=registry)
            set_molecule_engine(self._molecule_engine)
            logger.info(
                "MoleculeEngine v3: initialized with %d providers, %d presets",
                len(registry.list_available()),
                len(self._molecule_engine.list_molecules()),
            )
        except Exception as e:
            logger.warning("Failed to wire builtin_tools or init MoleculeEngine: %s", e)

        # SubAgentManager: single init via module_defs._init_subagent_manager
        # (sets worker.subagent_manager + tool_executor.set_subagent_manager)

        # ─────────────────────────────────────────────────────
        # SECTION: Skill System
        # ─────────────────────────────────────────────────────
        # Skill system - auto-extracts workflows from completed tasks
        self.skill_extractor = SkillExtractor(
            workspace=cfg.get("workspace", "."),
            enabled=cfg.get("modules", {}).get("skill_extractor", True),
            llm=self.llm,  # LLM-driven step summarization
        )
        self.skill_loader = SkillLoader(max_in_prompt=5, llm=self.llm)  # LLM-driven semantic matching
        self.skill_curator = SkillCurator()  # background lifecycle: stale→archive, pin, backup
        logger.info("Skill system: %d learned skills loaded", self.skill_loader.get_skill_count())

        # Wire skill_extractor into tool_executor so end_task can trigger skill learning
        try:
            from tical_code.core.tool_executor import set_skill_extractor
            set_skill_extractor(self.skill_extractor)
            logger.info("SkillExtractor wired into tool_executor for end_task")
        except Exception as e:
            logger.warning("Failed to wire skill_extractor into tool_executor: %s", e)

        # ─────────────────────────────────────────────────────
        # SECTION: System Prompt
        # ─────────────────────────────────────────────────────
        # Build system prompt
        self.system_prompt = build_system_prompt(
            name=cfg['name'],
            hostname=self._get_hostname(),
            deploy_path=cfg.get("workspace", ""),
            target_model=cfg.get("ai_model", ""),
            active_modules=self._active_modules,  # prompt.py uses registry data
            cognitive_workspace=getattr(self, 'cognitive_workspace', None),
        )

        # EITE identity layer - integrated into VerificationEngine
        if hasattr(self, 'verification') and self.verification:
            self.system_prompt += self.verification.get_identity_marker()
        logger.info(f"EITE identity bound: {cfg['name']}")

        # Skill injection - auto-extracted workflows from past tasks
        _skill_prompt = self.skill_loader.get_prompt_injection()
        if _skill_prompt:
            self.system_prompt += _skill_prompt
            logger.info("Skills injected: %d learned workflows", self.skill_loader.get_skill_count())

        # Physical axioms - observational lenses for reasoning (from axioms.py)
        if self._active_modules.get("decision_engine"):
            try:
                from tical_code.core.axioms import AxiomEngine
                _axioms_engine = AxiomEngine(enabled=True)
                _axioms_prefix = _axioms_engine.build_prompt_prefix()
                if _axioms_prefix:
                    self.system_prompt = _axioms_prefix + "\n\n" + self.system_prompt
                    logger.info("Physical axioms injected into system prompt (6 axioms)")
            except Exception as e:
                logger.warning("AxiomEngine init failed: %s", e)



        # ── FIX: Inject SOUL.md identity from MemoryBoot ──
        if self._memory_boot and self._memory_boot.is_loaded():
            try:
                _identity = self._memory_boot.get_identity_prompt()
                if _identity:
                    self.system_prompt = f"## Your Identity\n{_identity}\n\n" + self.system_prompt
                    logger.info("MemoryBoot: identity prompt injected (%d chars)", len(_identity))
                _user_ctx = self._memory_boot.get_memory("user")
                if _user_ctx:
                    self.system_prompt += f"\n\n## User Context\n{_user_ctx[:1500]}"
                    logger.info("MemoryBoot: user context injected (%d chars)", len(_user_ctx))
            except Exception as _id_err:
                logger.warning("MemoryBoot identity injection failed: %s", _id_err)

        # Legacy modules loaded on-demand when needed

        # Resume conversation from checkpoint after crash
        self._resume_conv = None

        logger.info(
            f"Worker initialized: name={self.name} "
            f"model={cfg.get('ai_model', '?')} "
            f"channels={len(self.channels)} "
            f"prompt_len={len(self.system_prompt)}"
        )

        # ─────────────────────────────────────────────────────
        # SECTION: Heartbeat / Watchdog
        # ─────────────────────────────────────────────────────
        # Heartbeat file for systemd watchdog
        self._heartbeat_file = Path(f"/tmp/worker-heartbeat-{self.name}")
        self._start_time = time.time()
        self._current_task_id = ""
        self._current_task_step = 0

        # ─────────────────────────────────────────────────────
        # SECTION: Permission Checker
        # ─────────────────────────────────────────────────────
        # 5-tier permission mode system (default/acceptEdits/bypass/plan/auto)
        _perm_cfg = cfg.get("permissions", {})
        self._permission_checker = PermissionChecker.from_dict(_perm_cfg)
        _perm_mode = self._permission_checker.mode_value
        logger.info("PermissionChecker ready: mode=%s allowed=%d denied=%d",
                     _perm_mode,
                     len(self._permission_checker.allowed_tools),
                     len(self._permission_checker.denied_tools))

        # ─────────────────────────────────────────────────────
        # SECTION: SharedContext
        # ─────────────────────────────────────────────────────
        # Build SharedContext for handler modules (all mutable state in one place)
        self._ctx = SharedContext(
            cfg=self.cfg,
            name=self.name,
            workspace=self.workspace,
            channels=self.channels,
            llm=self.llm,
            _msg_adapter=getattr(self, '_msg_adapter', None),
            _session_family=self._session_family,
            system_prompt=self.system_prompt,
            _tool_registry=self._tool_registry,
            _tool_executor=self._tool_executor,
            trace_logger=self.trace_logger,
            tracer=getattr(self, 'tracer', None),
            _metrics=getattr(self, '_metrics', None),
            error_logger=self.error_logger,
            sessions=getattr(self, 'sessions', None),
            context_compactor=getattr(self, 'context_compactor', None),
            verification=getattr(self, 'verification', None),
            verif_recorder=self.verif_recorder,
            constitution=getattr(self, 'constitution', None),
            truth_reporter=getattr(self, 'truth_reporter', None),
            decision_engine=getattr(self, 'decision_engine', None),
            _permission_checker=getattr(self, '_permission_checker', None),
            doom_detector=getattr(self, 'doom_detector', None),
            loop_detector=self.loop_detector,
            checkpoint=getattr(self, 'checkpoint', None),
            self_repair=getattr(self, 'self_repair', None),
            _pending_task_file=self._pending_task_file,
            _pending_task=self._pending_task,
            _evidence_retry_count=self._evidence_retry_count,
            _task_counter=self._task_counter,
            memory_evolver=self.memory_evolver,
            memory_store=getattr(self, 'memory_store', None),
            skill_extractor=self.skill_extractor,
            skill_loader=self.skill_loader,
            skill_curator=self.skill_curator,
            _active_modules=self._active_modules,
            _heartbeat_file=self._heartbeat_file,
            _start_time=self._start_time,
            usage=getattr(self, 'usage', None),
            _vigil=getattr(self, '_vigil', None),
            _memprof=getattr(self, '_memprof', None),
            sandbox=getattr(self, 'sandbox', None),
            reflection=getattr(self, 'reflection', None),
            cron=getattr(self, '_cron', None),

        )
        # Propagate event loop to SharedContext for run_async()
        if hasattr(self, '_loop'):
            self._ctx._loop = self._loop

        # ── Runtime module wiring verification ──────────────────────────
        # Ensure every module in the registry's "active" list has its
        # attribute actually set on the worker and reachable via ctx.
        # Catches variable-name mismatches like context_compactor/context_compactor
        # that cause modules to silently return None at runtime.
        try:
            from tical_code.core.module_wiring_check import (
                verify_worker_wiring,
                verify_ctx_critical_attrs,
            )
            _wiring_errors = verify_worker_wiring(
                self, self._active_modules, self._ctx, strict=True
            )
            _ctx_errors = verify_ctx_critical_attrs(self._ctx)
        except Exception as _wiring_exc:
            logger.critical(
                "WIRING VERIFICATION FAILED — worker may malfunction: %s",
                _wiring_exc,
            )

        # Wire cognitive workspace into SharedContext (v0.9+)
        
        # Initialize cognitive workspace (activated 2026-07-08)
        try:
            from tical_code.core.feature_flags import FeatureFlags
            _flags = FeatureFlags()
            if _flags.cognitive_enabled:
                from pathlib import Path as _Path
                _ws_persist = _Path(os.environ.get("WORKSPACE_DIR", "/tmp")) / ".cognitive"
                self.cognitive_workspace = Workspace(
                    node_id=os.environ.get("NODE_ID", "seoul-1"),
                    persist_path=_ws_persist,
                    enabled=True,
                )
            else:
                self.cognitive_workspace = None
        except Exception:
            self.cognitive_workspace = None

        if hasattr(self, 'cognitive_workspace') and self.cognitive_workspace:
            self._ctx.cognitive_workspace = self.cognitive_workspace

        # ─────────────────────────────────────────────────────
        # SECTION: Signal Handlers
        # ─────────────────────────────────────────────────────
        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """Register SIGTERM/SIGINT handlers for graceful shutdown.

        On signal receipt the handler:
          1. Saves a checkpoint via CheckpointManager (if loaded).
          2. Writes a session snapshot via save_snapshot() (if available),
             capturing active task state and conversation context.
          3. Records a death log entry via record_death() with signal type,
             uptime, loop count, and last error (if any).
          4. Removes the heartbeat file.
          5. Calls sys.exit(0).

        This ensures in-flight work is preserved across restarts,
        crash forensics are available via death log, and systemd
        service reloads do not result in blind data loss.
        """
        def _handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.warning("[signal] Received %s - saving state before exit", sig_name)
            if self.checkpoint:
                try:
                    self.checkpoint.save(
                        description=f"shutdown-{sig_name}",
                        session_messages=[],
                        session_id="shutdown",
                        iteration=0,
                    )
                except Exception as e:
                    logger.error("Checkpoint save on shutdown failed: %s", e)
            if save_snapshot is not None:
                try:
                    save_snapshot(self.name, {
                        "reason": f"signal_{sig_name}",
                        "msg_count": getattr(self, '_msg_count', 0),
                        "uptime": time.time() - self._start_time,
                        "rss_mb": _get_rss_mb(),
                        "active_task_id": getattr(self, '_current_task_id', None),
                        "conv_preview": str(getattr(self, '_last_conv', ''))[-2000:],
                    })
                except Exception:
                    pass
            # Record death for crash forensics
            if record_death is not None:
                try:
                    record_death(
                        worker_name=self.name,
                        signal_type=signum,
                        uptime=time.time() - self._start_time,
                        loop_count=getattr(self, '_loop_iter', 0),
                        last_error=str(getattr(self, '_last_error', '')) or None,
                        session_status=getattr(self, '_current_task_id', None),
                    )
                except Exception:
                    pass
            try:
                self._heartbeat_file.unlink(missing_ok=True)
            except Exception:
                pass
            logger.info("[signal] Graceful shutdown complete (snapshot + death log saved)")
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        logger.info("Signal handlers registered (SIGTERM/SIGINT → save checkpoint + snapshot + death log + exit)")

    # ─────────────────────────────────────────────────────────────
    # SECTION: EITE Vectorizer (lightweight text→vector, no deps)
    # ─────────────────────────────────────────────────────────────
    def _eite_text_to_vector(self, text: str) -> "np.ndarray":
        """Convert text to a vector for EITE kernel usage.

        Uses deterministic hash-based vectorization — same text always produces
        the same vector. No external embedding model required. Replace with
        sentence-transformers when available.
        """
        import hashlib
        import struct
        import numpy as np

        dim = 64  # default EITE dimension
        if hasattr(self, '_eite') and self._eite and hasattr(self._eite, 'dim'):
            dim = self._eite.dim

        if not text or not text.strip():
            rng = np.random.RandomState(42)
            v = rng.randn(dim).astype(np.float64)
            v /= np.linalg.norm(v)
            return v

        # Hash each token to a 64-bit seed, combine into a vector
        tokens = []
        for word in (text or "").lower().split():
            clean = "".join(c for c in word if c.isalnum())[:12]
            if clean and len(clean) > 1:
                tokens.append(clean)
        if not tokens:
            tokens = ["__empty__"]

        result = np.zeros(dim, dtype=np.float64)
        for i, token in enumerate(set(tokens)):
            seed = struct.unpack("q", hashlib.sha256(
                f"{token}_{i}".encode()).digest()[:8])[0] % (2**31)
            rng = np.random.RandomState(seed)
            result += rng.randn(dim).astype(np.float64)

        norm = np.linalg.norm(result)
        return result / norm if norm > 1e-12 else result

    # ─────────────────────────────────────────────────────────────
    # SECTION: Doom Loop Recovery Callbacks (Fix 1)
    # ─────────────────────────────────────────────────────────────
    def _register_doom_loop_recovery_callbacks(self) -> None:
        """Register real recovery callbacks on the doom_loop detector.

        Called after module loading so that self.doom_detector is available.
        Each callback executes a concrete recovery action when the doom_loop
        detector identifies a CRITICAL loop.  See doom_loop.RecoveryAction
        for the full enum.
        """
        detector = getattr(self, 'doom_detector', None)
        if detector is None:
            logger.debug("doom_detector not loaded - skipping recovery callback registration")
            return

        from tical_code.core.doom_loop import RecoveryAction

        # ── RETRY_DIFFERENT_ARGS: inject a system hint to vary params ──
        # Actual arg modification happens via the LLM seeing the hint.
        # This callback sets a flag on ctx so message_handler.py can
        # append a [DOOM_LOOP_RECOVERY] system message telling the AI
        # to change its approach.
        async def _retry_different_args(result):
            logger.info("[doom_loop] recovery: RETRY_DIFFERENT_ARGS - setting change_approach flag on ctx")
            self._ctx._doom_loop_recovery_change_approach = True
            return True

        # ── SWITCH_TOOL: tell the model to use a different tool ──
        async def _switch_tool(result):
            logger.info("[doom_loop] recovery: SWITCH_TOOL - setting tool_switch constraint on ctx")
            self._ctx._doom_loop_recovery_tool_switch = True
            return True

        # ── ROLLBACK_STEPS: call checkpoint restore ──
        async def _rollback_steps(result):
            cp = getattr(self, 'checkpoint', None)
            if cp is None:
                logger.warning("[doom_loop] recovery: ROLLBACK_STEPS requested but no checkpoint manager")
                return False
            try:
                cp_list = cp.list_checkpoints(status="complete")
                if not cp_list:
                    logger.warning("[doom_loop] recovery: ROLLBACK_STEPS - no completed checkpoints to restore")
                    return False
                # Restore the most recent completed checkpoint
                latest = cp_list[-1]
                cp.restore(latest["id"], confirm=True)
                logger.info("[doom_loop] recovery: ROLLBACK_STEPS - restored checkpoint %s", latest["id"])
                return True
            except Exception as e:
                logger.warning("[doom_loop] recovery: ROLLBACK_STEPS failed: %s", e)
                return False

        # ── DOWNGRADE_MODEL: switch to a cheaper model ──
        async def _downgrade_model(result):
            llm = getattr(self, 'llm', None)
            # ModelFailover has a downgrade method; plain llm backends don't
            if llm is None:
                logger.warning("[doom_loop] recovery: DOWNGRADE_MODEL requested but no LLM backend")
                return False
            try:
                # Try ModelFailover.fallback() or similar downgrade path
                if hasattr(llm, 'fallback'):
                    llm.fallback()
                    logger.info("[doom_loop] recovery: DOWNGRADE_MODEL - switched to fallback provider")
                    return True
                # Some providers expose a degrade() method
                if hasattr(llm, 'degrade'):
                    llm.degrade()
                    logger.info("[doom_loop] recovery: DOWNGRADE_MODEL - degraded model tier")
                    return True
                logger.info("[doom_loop] recovery: DOWNGRADE_MODEL - no fallback/degrade method available")
                return False
            except Exception as e:
                logger.warning("[doom_loop] recovery: DOWNGRADE_MODEL failed: %s", e)
                return False

        # ── FORCE_SUMMARIZE: trigger aggressive context compaction ──
        async def _force_summarize(result):
            context_compactor = getattr(self, 'context_compactor', None)
            if context_compactor is None:
                logger.warning("[doom_loop] recovery: FORCE_SUMMARIZE requested but no context_compactor")
                return False
            try:
                # Set the force-compact flag so the next compact_if_needed()
                # call runs compaction regardless of token threshold.
                context_compactor._force_compact_pending = True
                logger.info("[doom_loop] recovery: FORCE_SUMMARIZE - set force-compact flag")
                return True
            except Exception as e:
                logger.warning("[doom_loop] recovery: FORCE_SUMMARIZE failed: %s", e)
                return False

        # Register all callbacks on the detector
        try:
            detector.register_recovery_callback(RecoveryAction.RETRY_DIFFERENT_ARGS, _retry_different_args)
            detector.register_recovery_callback(RecoveryAction.SWITCH_TOOL, _switch_tool)
            detector.register_recovery_callback(RecoveryAction.ROLLBACK_STEPS, _rollback_steps)
            detector.register_recovery_callback(RecoveryAction.DOWNGRADE_MODEL, _downgrade_model)
            detector.register_recovery_callback(RecoveryAction.FORCE_SUMMARIZE, _force_summarize)
            logger.info("Doom loop recovery callbacks registered: %d actions", len(RecoveryAction))
        except Exception as e:
            logger.warning("Failed to register doom loop recovery callbacks: %s", e)

    def _write_heartbeat(self) -> None:
        """Write a JSON heartbeat file for systemd watchdog monitoring.

        Writes atomically via a .tmp file + os.rename().  If the
        NOTIFY_SOCKET environment variable is set (systemd watchdog
        enabled), also sends a WATCHDOG=1 notification via sd_notify.

        The heartbeat payload includes: worker name, timestamp,
        uptime_seconds, rss_mb, current task_id and step, and
        total message count.
        """
        try:
            rss = _get_rss_mb()
            uptime = time.time() - self._start_time
            data = {
                "worker": self.name,
                "timestamp": time.time(),
                "uptime_seconds": round(uptime, 1),
                "rss_mb": round(rss, 1),
                "task_id": self._current_task_id,
                "task_step": self._current_task_step,
                "msg_count": getattr(self, '_msg_count', 0),
            }
            tmp = Path(str(self._heartbeat_file) + ".tmp")
            tmp.write_text(json.dumps(data))
            os.rename(str(tmp), str(self._heartbeat_file))

            if os.environ.get("NOTIFY_SOCKET"):
                try:
                    import ctypes
                    lib = ctypes.CDLL(None)
                    sd_notify = lib.sd_notify
                    sd_notify.argtypes = [ctypes.c_int, ctypes.c_char_p]
                    sd_notify.restype = ctypes.c_int
                    sd_notify(0, b"WATCHDOG=1")
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[heartbeat] write failed: %s", e)

    def _load_pending(self) -> dict | None:
        """Load and consume a pending task file from the workspace.

        Reads .pending_task.json, returns its decoded contents, and
        deletes the file so it is not replayed on the next startup.
        Returns None if the file doesn't exist or can't be parsed.
        """
        try:
            if self._pending_task_file.exists():
                data = json.loads(self._pending_task_file.read_text())
                self._pending_task_file.unlink(missing_ok=True)
                return data
        except Exception as e:
            logger.debug(f"[pending_task] swallowed: {e}")
        return None

    def _save_pending(self, task: str, iteration: int = 0):
        """Persist a pending task to disk for cross-poll continuation.

        Writes the task description and iteration counter to
        .pending_task.json in the workspace directory.  This file is
        consumed by _load_pending() on the next loop tick (or next
        startup) so long-running tasks survive across short pauses.
        """
        try:
            self._pending_task_file.parent.mkdir(parents=True, exist_ok=True)
            self._pending_task_file.write_text(json.dumps({
                "task": task, "iteration": iteration, "source": "continuation"
            }))
        except Exception as e:
            logger.warning(f"Failed to save pending task: {e}")

    def _get_hostname(self) -> str:
        """Return the system hostname, or 'unknown' on failure.

        Used when building the system prompt so the LLM knows which
        machine it is running on.
        """
        import socket
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def get_trace(self, trace_id: str = None):
        """Return trace events for a task.

        Args:
            trace_id: The trace ID to query. If None, uses the current task's trace ID.

        Returns:
            List of TraceEvent objects (may be empty if trace is no longer in the ring buffer).
        """
        if trace_id is None:
            trace_id = self._current_trace_id
        if not trace_id:
            return []
        return self.trace_logger.get_trace(trace_id)

    def run(self):
        """Enter the main orchestrator loop - blocks indefinitely.

        This is the heartbeat of the Worker.  Each iteration (tick)
        executes the following in order:

        **Phase 1 - Resume active tasks**
          Calls list_active_tasks() and re-enters any task that was
          interrupted mid-flight.  Skips channel polling until all
          active tasks have been processed (via continue).

        **Phase 2 - Poll channels**
          Iterates over self.channels, calling channel.poll() for
          new messages.  For each message: sends a typing indicator
          (Telegram only), records input on the Vigil signal collector
          (if loaded), and dispatches to _handle_message().

        **Per-tick housekeeping**
          • Pending task continuation - if _pending_task is set,
            wraps it in a synthetic Message and dispatches.
          • Memory-triggered restart - if _schedule_restart is True
            (RSS above limit), calls `systemctl restart` and returns.
          • Task cleanup - every 100 ticks runs task_cleanup_completed().
          • Heartbeat - every 60 ticks writes the systemd watchdog file.
          • Vigil patrol - every 300 seconds runs _vigil.patrol() for
            immune-system security sweeps.

        **Sleep**
          time.sleep(1) between ticks gives a 1 Hz cadence.
        """
        logger.info(f"Worker {self.name} entering main loop")

        # ─────────────────────────────────────────────────
        # Loop tracking (for crash forensics / signal handlers)
        # ─────────────────────────────────────────────────
        self._loop_iter = 0
        self._last_conv = ""
        self._last_error = None

        # Shared event loop for the entire worker lifecycle (avoids asyncio.run() OOM leak)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Deferred MemoryBoot execution (avoids asyncio.run() in __init__)
        if getattr(self, '_memory_boot_pending', False) and self._memory_boot:
            try:
                self._loop.run_until_complete(self._memory_boot.boot())
                self._memory_boot_pending = False
                logger.info("MemoryBoot: cold-start identity/memory loaded (deferred)")
                # Inject identity + user context into system prompt AFTER boot
                try:
                    _identity = self._memory_boot.get_identity_prompt()
                    if _identity:
                        self.system_prompt = "## Your Identity\n" + _identity + "\n\n" + self.system_prompt
                        logger.info("MemoryBoot: identity prompt injected (%d chars)", len(_identity))
                    _user_ctx = self._memory_boot.get_memory("user")
                    if _user_ctx:
                        self.system_prompt += "\n\n## User Context\n" + _user_ctx[:1500]
                        logger.info("MemoryBoot: user context injected (%d chars)", len(_user_ctx))
                    _mem = self._memory_boot.get_memory("memory")
                    if _mem:
                        self.system_prompt += "\n\n## Experience Memory\n" + _mem[:2000]
                        logger.info("MemoryBoot: experience memory injected (%d chars)", len(_mem))
                except Exception as _inject_err:
                    logger.warning("MemoryBoot post-boot injection failed: %s", _inject_err)
            except Exception as e:
                logger.warning("MemoryBoot deferred boot failed: %s", e)

        # ─────────────────────────────────────────────────
        # SECTION: Main Loop
        # ─────────────────────────────────────────────────
        # Periodic task cleanup counter
        _cleanup_counter = 0

        while True:
            try:
                # REFACTOR 2026-07-12: decouple task recovery from main poll loop.
                # Synchronous task resumption blocked Telegram channel polling
                # when stale task dirs remained. Recovery is now opt-in via the
                # sustained_task module, not a hard blocking step in the event loop.
                # To re-enable: spawn recovery in a daemon thread, not inline.

                # 2. POLL CHANNELS - normal message handling
                for channel in self.channels:
                    messages = channel.poll()
                    for msg in messages:
                        # Send typing indicator for Telegram messages
                        if hasattr(channel, 'send_action') and msg.source == "telegram" and msg.chat_id:
                            try:
                                channel.send_action("typing", msg.chat_id)
                            except Exception:
                                pass
                            # Export chat_id for Guardian daemon auto-discovery
                            try:
                                Path("~/.guardian_chat_id").expanduser().write_text(str(msg.chat_id))
                            except Exception:
                                pass
                        # Vigil: record human input on signal collector
                        if self._vigil:
                            self._vigil.signal_collector.record_input(char_count=len(msg.content or ""))
                        try:
                            self._handle_message(channel, msg)
                        except Exception as e:
                            self._last_error = str(e)
                            if self.error_logger and ErrorCategory is not None:
                                try:
                                    self.error_logger.log(ErrorCategory.WORKER, "handle error", exc=e)
                                except Exception:
                                    pass
                            logger.error(
                                f"handle error: {e}\n{traceback.format_exc()}"
                            )
                            # Self-repair: attempt auto recovery on errors
                            if self.self_repair:
                                try:
                                    self._loop.run_until_complete(self.self_repair.auto_repair_if_needed())
                                except Exception:
                                    pass
                            if channel:
                                channel.send(Response(
                                    content=f"[worker] error: {e}",
                                    target=msg.sender,
                                    source=msg.source,
                                    chat_id=msg.chat_id,
                                ))

            except Exception as e:
                self._last_error = str(e)
                if self.error_logger and ErrorCategory is not None:
                    try:
                        self.error_logger.log(ErrorCategory.CONNECTION, "poll error", exc=e)
                    except Exception:
                        pass
                logger.error(f"poll error: {e}\n{traceback.format_exc()}")
                # Self-repair: attempt auto recovery on poll errors
                if self.self_repair:
                    try:
                        self._loop.run_until_complete(self.self_repair.auto_repair_if_needed())
                    except Exception:
                        pass

            # Message-count-based health scan (Fix 2: periodic self-repair check)
            _msg_count = getattr(self._ctx, '_msg_count', 0)
            if _msg_count - self._last_health_scan_msg_count >= self._health_scan_interval:
                self._last_health_scan_msg_count = _msg_count
                if self.self_repair is not None:
                    try:
                        self._loop.run_until_complete(self.self_repair.auto_repair_if_needed())
                        logger.debug("Health scan completed at msg #%d (interval=%d)",
                                     _msg_count, self._health_scan_interval)
                    except Exception as e:
                        logger.warning("Health scan error at msg #%d: %s", _msg_count, e)

            # Periodic session persistence (every 10 iterations)
            if hasattr(self, '_save_counter'):
                self._save_counter += 1
            else:
                self._save_counter = 0
            if self._save_counter % 10 == 0:
                try:
                    if hasattr(self, 'session_manager') and self.session_manager:
                        self.session_manager.conn.commit()
                except Exception:
                    pass

            # Check for pending task continuation
            # Memory-triggered restart (check SharedContext, where it is set)
            if getattr(self._ctx, '_schedule_restart', False):
                logger.warning("[memory] restarting due to RSS limit")
                self._ctx._schedule_restart = False
                # Crash-safe: persist session before restart
                try:
                    if hasattr(self, 'session_manager') and self.session_manager:
                        self.session_manager.conn.commit()
                except Exception:
                    pass
                # Save checkpoint before restart (Fix P0-7)
                if self.checkpoint:
                    try:
                        self.checkpoint.save(
                            description="memory-restart",
                            session_messages=[],
                            session_id="shutdown",
                        )
                    except Exception as e:
                        logger.warning("[memory] checkpoint save failed: %s", e)
                import shutil
                import subprocess
                svc = os.environ.get("SERVICE_NAME", "")
                _allowed_services = set(
                    s.strip() for s in os.environ.get("ALLOWED_SERVICES", "unified-worker").split(",") if s.strip()
                )
                if svc not in _allowed_services:
                    logger.error(f"Rejected SERVICE_NAME: {svc!r} - not in whitelist")
                elif not shutil.which("systemctl"):
                    logger.error("systemctl not found on PATH - cannot restart")
                else:
                    subprocess.Popen(["systemctl", "restart", svc])
                return
            if self._pending_task:
                task = self._pending_task
                self._pending_task = None
                msg = Message(
                    sender="system",
                    content=f"[continue] {task['task']}",
                    source="system",
                )
                try:
                    self._handle_message(None, msg)
                except Exception as e:
                    if self.error_logger and ErrorCategory is not None:
                        try:
                            self.error_logger.log(ErrorCategory.WORKER, "pending task error", exc=e)
                        except Exception:
                            pass
                    logger.error(f"pending task error: {e}\n{traceback.format_exc()}")

            # Periodic task cleanup (every ~100 loop ticks, roughly every 2 minutes)
            _cleanup_counter += 1
            if _cleanup_counter % 100 == 0 and task_cleanup_completed is not None:
                try:
                    task_cleanup_completed(workspace=self.workspace)
                except Exception:
                    pass

            # Periodic heartbeat for systemd watchdog (every 60 loops ≈ 60s)
            if _cleanup_counter % 60 == 0:
                try:
                    self._write_heartbeat()
                except Exception:
                    pass

            # Vigil immune system patrol - periodic security sweep
            _vigil = getattr(self._ctx, '_vigil', None)
            if _vigil is not None:
                _now = time.time()
                _interval = getattr(self._ctx, '_vigil_patrol_interval', 300)
                if _now - self._ctx._last_patrol >= _interval:
                    try:
                        self._loop.run_until_complete(_vigil.patrol())
                    except Exception as e:
                        logger.warning("Vigil patrol error: %s", e)
                    self._ctx._last_patrol = _now

            # Skill curator - periodic lifecycle management (stale→archive, backup)
            # Gate to every 300 ticks (~5 min) to avoid per-tick I/O cost
            _curator = getattr(self._ctx, 'skill_curator', None)
            if _curator is not None and _cleanup_counter % 300 == 0:
                try:
                    _curator.curate()
                except Exception as e:
                    logger.warning("Skill curator error: %s", e)

            # Cron scheduler tick - execute due periodic jobs
            _cron = getattr(self._ctx, 'cron', None)
            if _cron is not None:
                try:
                    self._loop.run_until_complete(_cron.tick())
                except Exception as e:
                    logger.warning("Cron tick error: %s", e)

            # MemoryEvolver periodic consolidation - every 500 iterations (~10 min)
            _evolver = getattr(self._ctx, 'memory_evolver', None)
            if _evolver is not None and self._loop_iter > 0 and self._loop_iter % 500 == 0:
                try:
                    result = _evolver.consolidate()
                    if result.get('consolidated', 0) > 0 or result.get('space_saved', 0) > 0:
                        logger.info(
                            "[MemoryEvolver] consolidated=%d space_saved=%d",
                            result.get('consolidated', 0),
                            result.get('space_saved', 0),
                        )
                    self._ctx._evolution_timer = int(time.time())
                except Exception as e:
                    logger.warning("MemoryEvolver consolidation error: %s", e)

            # Loop tracking (for crash forensics - signal handlers read these)
            self._loop_iter += 1

            time.sleep(1)


    # ─────────────────────────────────────────────────────────
    # SECTION: Main Loop Delegates
    # ─────────────────────────────────────────────────────────
    # These thin wrappers forward to handler modules via
    # SharedContext.  They exist so the Worker class is the
    # single entry point for the orchestrator while the actual
    # logic lives in separate, testable handler modules.
    # ─────────────────────────────────────────────────────────

    def _run_task(self, task):
        """Resume or start an autonomous task from disk.

        Delegates to tical_code.core.modules.task_handler.run_task.
        """
        return run_task(self._ctx, task)

    def _handle_message(self, channel, msg: Message):
        """Process an incoming message through the full pipeline.

        This is the core dispatch: CMD detection → LLM call →
        tool execution → response formatting → reply.

        Delegates to tical_code.core.modules.message_handler.handle_message.
        """
        return _handle_message_ctx(self._ctx, channel, msg)


# ─────────────────────────────────────────────────────────────
# SECTION: SessionManager (LRU Session Lifecycle)
# ─────────────────────────────────────────────────────────────


class SessionManager:
    """LRU session lifecycle manager with idle timeout cleanup.

    Manages in-memory session instances with last-access tracking,
    LRU eviction at capacity, and periodic idle timeout sweep.
    Thread-safe via threading.Lock.

    This is distinct from the SQLite-based SessionManager in
    tical_code.core.modules.session_manager - it manages active
    in-memory session objects rather than persistent message storage.
    """

    def __init__(self, max_sessions: int = 100, idle_timeout: int = 1800):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout  # seconds (default 30 min)

    def get_or_create(self, session_id: str, factory=None):
        """Get existing session entry or create a new one.

        Returns (entry, is_new) tuple where entry is a dict with
        'data' and 'last_access' keys.
        """
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["last_access"] = time.time()
                return self._sessions[session_id], False
            if len(self._sessions) >= self.max_sessions:
                self._evict_lru()
            data = factory() if factory else {}
            entry: dict = {"data": data, "last_access": time.time()}
            self._sessions[session_id] = entry
            return entry, True

    def touch(self, session_id: str) -> None:
        """Update last_access timestamp for a session."""
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["last_access"] = time.time()

    def remove(self, session_id: str) -> None:
        """Remove a session from the manager."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup_expired(self) -> list[str]:
        """Remove all sessions past idle_timeout.

        Returns list of removed session IDs.
        """
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for sid, entry in list(self._sessions.items()):
                if now - entry["last_access"] > self.idle_timeout:
                    expired.append(sid)
            for sid in expired:
                self._sessions.pop(sid, None)
        return expired

    def active_count(self) -> int:
        """Return the number of currently tracked sessions."""
        with self._lock:
            return len(self._sessions)

    def get_entry(self, session_id: str):
        """Return the session entry dict, or None."""
        with self._lock:
            return self._sessions.get(session_id)

    def _evict_lru(self) -> None:
        """Evict the least recently used session."""
        if not self._sessions:
            return
        oldest_sid = min(
            self._sessions,
            key=lambda sid: self._sessions[sid]["last_access"],
        )
        self._sessions.pop(oldest_sid)


# ─────────────────────────────────────────────────────────────
# SECTION: AsyncWorker (Async Mainloop with Per-Session Tasks)
# ─────────────────────────────────────────────────────────────


class AsyncWorker:
    """Asynchronous worker with per-session task isolation.

    Each session gets its own asyncio.Task and asyncio.Queue, so one
    session's LLM call or tool execution never blocks another.  Channel
    polling is offloaded to a thread pool via loop.run_in_executor
    for non-blocking I/O.

    Session idle timeout is 5 minutes (configurable via config key
    ``async_session_timeout``).  The SessionManager uses a 30-minute
    long-term idle timeout for LRU lifecycle; AsyncWorker's per-session
    Task dies after the shorter timeout and the SessionManager entry
    stays until the longer timeout kicks in.

    Usage::

        cfg = load_config()
        worker = AsyncWorker(cfg)
        asyncio.run(worker.run())
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.workspace = cfg["workspace"]
        self.logger = logging.getLogger(f"tical-code.async_worker.{self.name}")

        # SustainedTaskManager - persistent task queue with auto-recovery
        if SustainedTaskManager is not None:
            self._sustained_task_mgr = SustainedTaskManager()
            # LIVE WIRE INJECT 2026-07-13
            try:
                from tical_code.core.tool_executor import set_sustained_task_manager
                set_sustained_task_manager(self._sustained_task_mgr)
            except Exception as _inj_s:
                self.logger.warning("sustained inject failed: %s", _inj_s)
            self.logger.info("SustainedTaskManager initialized")
        else:
            self._sustained_task_mgr = None
            self.logger.warning("SustainedTaskManager unavailable")

        # SelfEvolveEngine - error pattern tracking and usage insights
        if SelfEvolveEngine is not None:
            self._self_evolve = SelfEvolveEngine(
                db_path=os.path.expanduser("~/.tical-code/self_evolve.db")
            )
            # LIVE WIRE INJECT 2026-07-13
            try:
                from tical_code.core.tool_executor import set_self_evolve_engine
                set_self_evolve_engine(self._self_evolve)
            except Exception as _inj_e:
                self.logger.warning("self_evolve inject failed: %s", _inj_e)
            self.logger.info("SelfEvolveEngine initialized")
        else:
            self._self_evolve = None
            self.logger.warning("SelfEvolveEngine unavailable")

        # Session management (LRU, 30-min idle timeout for long-term tracking)
        self.session_manager = SessionManager(
            max_sessions=cfg.get("max_sessions", 100),
            idle_timeout=cfg.get("async_idle_timeout", 1800),
        )
        # SQLite session persistence for conversation history
        self.sessions = None
        if SessionManagerSQLite is not None:
            try:
                _session_db = os.path.join(cfg.get("workspace", "."), "sessions.db")
                self.sessions = SessionManagerSQLite(db_path=_session_db)
                self.logger.info("SessionManagerSQLite ready: %s", _session_db)
            except Exception as e:
                self.logger.warning("SessionManagerSQLite init failed: %s", e)
        # Per-session asyncio constructs - 5-minute session task timeout
        self._session_timeout = cfg.get("async_session_timeout", 300)

        # Hard timeout guards - prevent worker death when LLM API hangs
        self._llm_hard_timeout = 120   # max seconds for a single LLM call
        self._process_hard_timeout = 600  # LIVE 2026-07-09: 10min — Gateway-style, no hard time limit
        self._session_stuck_threshold = 300  # kill session task if stuck this long
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._async_lock = asyncio.Lock()

        # Channels
        self.channels: list = []
        if cfg.get("tg_token"):
            self.channels.append(TelegramChannel(cfg["tg_token"]))
            self.logger.info("Telegram channel ready")
        if cfg.get("chat_url"):
            chat_key = cfg.get("chat_key", "") or ""
            if not chat_key:
                self.logger.warning("TICAL_CHAT_KEY not set - tical-chat channel will fail")
            self.channels.append(TicalChatChannel(
                base_url=cfg["chat_url"],
                identity=cfg["name"],
                shared_key=chat_key,
            ))
            self.logger.info("tical-chat channel ready (%s)", cfg["chat_url"])

        # Error logger
        self.error_logger = None
        if ErrorLogger is not None:
            try:
                self.error_logger = ErrorLogger(log_dir=under_tical_home("logs"))
                self.logger.info("ErrorLogger ready")
            except Exception as e:
                self.logger.warning("ErrorLogger init failed: %s", e)

        # Module loading
        try:
            self._modules = load_modules(self, self.cfg)
            self.logger.info("Modules loaded: %d active", len(self._modules))
            if isinstance(self._modules, dict):
                self._module_descriptions = get_active_descriptions(self._modules)
            else:
                self._module_descriptions = []
        except Exception as e:
            self.logger.warning("Module loading failed: %s", e)
            self._modules = []
            self._module_descriptions = []

        # LIVE WIRE INJECT 2026-07-09f (AsyncWorker post-module load)
        try:
            from tical_code.core.tool_executor import set_sustained_task_manager, set_self_evolve_engine
            _stm = getattr(self, "_sustained_task_mgr", None) or getattr(self, "sustained_task", None)
            if _stm is not None:
                set_sustained_task_manager(_stm)
            _sev = getattr(self, "_self_evolve", None) or getattr(self, "self_evolve", None)
            if _sev is not None:
                set_self_evolve_engine(_sev)
        except Exception as _wire_e:
            self.logger.warning("live wire inject failed: %s", _wire_e)

        # System prompt — built with full identity, modules, and memory injection
        try:
            import socket as _socket
            _hostname = _socket.gethostname()
            self.system_prompt = build_system_prompt(
                name=self.name,
                hostname=_hostname,
                deploy_path=cfg.get("workspace", ""),
                target_model=cfg.get("ai_model", ""),
                active_modules=self._modules if isinstance(self._modules, dict) else None,
                cognitive_workspace=getattr(self, 'cognitive_workspace', None),
            )
            self.logger.info("System prompt built: %d chars", len(self.system_prompt))
        except Exception as e:
            self.logger.warning("System prompt build failed: %s", e)
            self.system_prompt = "You are a helpful AI assistant."

        # Skill injection — learned workflows from past tasks
        try:
            from tical_code.core.skill_loader import SkillLoader
            _skill_loader = SkillLoader(max_in_prompt=5, llm=self.llm if hasattr(self, 'llm') else None)
            _skill_prompt = _skill_loader.get_prompt_injection()
            if _skill_prompt:
                self.system_prompt += _skill_prompt
                self.logger.info("Skills injected: %d learned workflows", _skill_loader.get_skill_count())
        except Exception as e:
            self.logger.debug("Skill injection skipped: %s", e)

        # Memory boot — load persistent identity/memory into prompt
        try:
            _mem_dir = under_tical_home("memory")
            from tical_code.core.memory_boot import ensure_memory_files
            ensure_memory_files(_mem_dir)
            # Read memory files and inject into prompt
            _mem_files = [
                ("SOUL.md", "Base config/SOUL.md", "IDENTITY"),
                ("USER.md", "USER.md", "USER PROFILE"),
                ("MEMORY.md", "MEMORY.md", "MEMORY"),
                ("CLAUDE.md", "CLAUDE.md", "Project Context (CLAUDE.md)"),
            ]
            for _fname, _rel, _label in _mem_files:
                _fpath = os.path.join(_mem_dir, _rel)
                if os.path.exists(_fpath):
                    _content = open(_fpath).read().strip()
                    if _content:
                        self.system_prompt += f"\n\n## {_label}\n{_content[:2000]}"
            self.logger.info("Memory injected into system prompt")
        except Exception as e:
            self.logger.debug("Memory injection skipped: %s", e)

        # Build FTS5 index from memory markdown files (memory_sense)
        try:
            from tical_code.core.memory_sense import memory_index
            _indexed = memory_index(under_tical_home("memory"))
            self.logger.info("memory_sense FTS5 index built: %d files", _indexed)
        except Exception as e:
            self.logger.debug("memory_sense FTS5 index build failed: %s", e)

        # ─────────────────────────────────────────────────────
        # SECTION: LLM Backend (AsyncWorker)
        # ─────────────────────────────────────────────────────
        # LLM backend - prefer ModelFailover for multi-provider resilience
        _failover = None
        try:
            from tical_code.core.provider_registry import from_registry
            _failover = from_registry(
                repo_root=cfg.get("workspace", os.getcwd()),
                worker_name=self.name,
            )
        except ImportError:
            self.logger.info("provider_registry not available, trying from_env")
        except Exception as e:
            self.logger.warning("from_registry failed: %s, trying from_env", e)

        if _failover is None:
            try:
                _failover = failover_from_env()
            except Exception as e:
                self.logger.warning("ModelFailover from_env failed: %s, falling back to create_llm_backend", e)

        if _failover is not None:
            self.llm = _failover
            from tical_code.core.tool_executor import set_failover
            set_failover(self.llm)
            self.logger.info("LLM: ModelFailover with %d providers", len(self.llm.providers))
        else:
            self.llm = create_llm_backend(
                model=cfg.get("ai_model", "deepseek-v4-flash"),
                api_key=cfg.get("ai_key", ""),
                base_url=cfg.get("ai_endpoint", ""),
            )

        # LLM backend with failover — reuse self.llm (just set above)
        # instead of the broken self.failover init that called
        # failover_from_env(cfg) with wrong args.
        self.failover = self.llm

        # Context context_compactor
        self.context_compactor = None
        if ContextCompactor is not None:
            try:
                self.context_compactor = ContextCompactor()
            except Exception as e:
                self.logger.warning("ContextCompactor init failed: %s", e)

        # Doom loop detector
        self.doom_loop = None
        if DoomLoopDetector is not None:
            try:
                config = DoomLoopConfig()
                self.doom_loop = DoomLoopDetector(config)
            except Exception as e:
                self.logger.warning("DoomLoopDetector init failed: %s", e)

        # Register doom-loop recovery callbacks (inline — AsyncWorker does not
        # inherit from Worker, so _register_doom_loop_recovery_callbacks isn't
        # available here.  This mirrors Worker._register_doom_loop_recovery_callbacks.)
        _detector = getattr(self, 'doom_loop', None)
        if _detector is not None:
            try:
                from tical_code.core.doom_loop import RecoveryAction

                async def _retry_different_args(result):
                    self.logger.info("[doom_loop] recovery: RETRY_DIFFERENT_ARGS")
                    self._ctx._doom_loop_recovery_change_approach = True
                    return True

                async def _switch_tool(result):
                    self.logger.info("[doom_loop] recovery: SWITCH_TOOL")
                    self._ctx._doom_loop_recovery_tool_switch = True
                    return True

                async def _rollback_steps(result):
                    _cp = getattr(self, 'checkpoint', None)
                    if _cp is None:
                        self.logger.warning("[doom_loop] recovery: ROLLBACK_STEPS — no checkpoint")
                        return False
                    try:
                        _latest = _cp.get_latest()
                        if _latest is None:
                            self.logger.warning("[doom_loop] recovery: ROLLBACK_STEPS — no checkpoints")
                            return False
                        _cp.restore(_latest["id"])
                        self.logger.info("[doom_loop] recovery: ROLLBACK_STEPS restored %s", _latest["id"])
                        return True
                    except Exception as _e:
                        self.logger.warning("[doom_loop] recovery: ROLLBACK_STEPS failed: %s", _e)
                        return False

                async def _downgrade_model(result):
                    _llm = getattr(self, 'llm', None)
                    if _llm is None:
                        self.logger.warning("[doom_loop] recovery: DOWNGRADE_MODEL — no LLM")
                        return False
                    try:
                        if hasattr(_llm, 'fallback_provider') and _llm.fallback_provider:
                            _llm.switch_to_fallback()
                            self.logger.info("[doom_loop] recovery: DOWNGRADE_MODEL switched to fallback")
                            return True
                        if hasattr(_llm, 'degrade') and callable(_llm.degrade):
                            _llm.degrade()
                            self.logger.info("[doom_loop] recovery: DOWNGRADE_MODEL degraded tier")
                            return True
                    except Exception as _e:
                        self.logger.warning("[doom_loop] recovery: DOWNGRADE_MODEL failed: %s", _e)
                    return False

                async def _force_summarize(result):
                    _comp = getattr(self, 'context_compactor', None)
                    if _comp is None:
                        self.logger.warning("[doom_loop] recovery: FORCE_SUMMARIZE — no context_compactor")
                        return False
                    try:
                        _comp._force_compact_pending = True
                        self.logger.info("[doom_loop] recovery: FORCE_SUMMARIZE flag set")
                        return True
                    except Exception as _e:
                        self.logger.warning("[doom_loop] recovery: FORCE_SUMMARIZE failed: %s", _e)
                        return False

                _detector.register_recovery_callback(RecoveryAction.RETRY_DIFFERENT_ARGS, _retry_different_args)
                _detector.register_recovery_callback(RecoveryAction.SWITCH_TOOL, _switch_tool)
                _detector.register_recovery_callback(RecoveryAction.ROLLBACK_STEPS, _rollback_steps)
                _detector.register_recovery_callback(RecoveryAction.DOWNGRADE_MODEL, _downgrade_model)
                _detector.register_recovery_callback(RecoveryAction.FORCE_SUMMARIZE, _force_summarize)
                self.logger.info("Doom loop recovery callbacks registered: 5 actions")
            except Exception as _e:
                self.logger.warning("Failed to register doom loop recovery callbacks: %s", _e)

        # ── Runtime module wiring verification ──────────────────────────
        # Verify every module marked "active" by the registry actually has
        # its attribute set on self and is not None.
        # Catches: registry says active, but getattr(self, attr_name) returns None
        # due to variable-name mismatch (e.g. context_compactor vs context_compactor).
        # FATAL: worker refuses to start if miswired modules are detected.
        try:
            from tical_code.core.module_registry import _registry
            _miswired = []
            _wired_ok = 0
            _mods = getattr(self, '_active_modules', None) or getattr(self, '_modules', None) or {}
            for _name, _inst in _mods.items():
                _spec = _registry.get(_name)
                if _spec is None:
                    continue
                _val = getattr(self, _spec.attr_name, None)
                if _val is None:
                    _miswired.append(
                        f"{_name}: self.{_spec.attr_name}=None (registry says active)"
                    )
                else:
                    _wired_ok += 1
            if _miswired:
                _msg = (
                    f"WIRING MISMATCH: {len(_miswired)} module(s) in registry "
                    f"but None on self: {'; '.join(_miswired)}"
                )
                self.logger.critical(_msg)
                # Write report for guardian
                try:
                    import json, os as _os
                    _os.makedirs("/opt/tical-guardian", exist_ok=True)
                    with open("/opt/tical-guardian/module_wiring.json", "w") as _f:
                        json.dump({
                            "healthy": False,
                            "errors": _miswired,
                            "modules_wired": _wired_ok,
                            "modules_active": len(_mods),
                        }, _f)
                except Exception:
                    pass
                raise RuntimeError(_msg)
            else:
                self.logger.info(
                    "Module wiring verified: %d/%d modules correctly wired",
                    _wired_ok, len(_mods),
                )
                try:
                    import json, os as _os
                    _os.makedirs("/opt/tical-guardian", exist_ok=True)
                    with open("/opt/tical-guardian/module_wiring.json", "w") as _f:
                        json.dump({
                            "healthy": True,
                            "errors": [],
                            "modules_wired": _wired_ok,
                            "modules_active": len(_mods),
                        }, _f)
                except Exception:
                    pass
        except RuntimeError:
            raise
        except Exception as _we:
            self.logger.warning("Module wiring verification failed: %s", _we)

        # Tool schemas
        self.tool_schemas = TOOL_SCHEMAS_CLEAN

        # Usage tracker
        self.usage_tracker = UsageTracker()

        # SubAgentManager: single init via module_defs (load_modules above)

        # Wire memory_store and Vigil into tool_executor
        try:
            from tical_code.core.tool_executor import set_memory_store as _te_sms, set_vigil as _te_sv
            _mem_store = getattr(self, 'memory_store', None)
            if _mem_store:
                _te_sms(_mem_store)
                self.logger.info("MemoryFTSStore wired into tool_executor")
            _vg = getattr(self, '_vigil', None)
            if _vg is not None:
                _te_sv(_vg)
                self.logger.info("Vigil wired into tool_executor")
        except Exception as e:
            self.logger.debug("Memory/Vigil wiring skipped: %s", e)

        # Wire cron manager if loaded by modules
        try:
            _cron = getattr(self, '_cron', None)
            if _cron:
                from tical_code.core.builtin_tools import set_cron_manager
                set_cron_manager(_cron)
                self.logger.info("CronManager wired into builtin_tools")
        except Exception as e:
            self.logger.debug("Cron wiring skipped: %s", e)

        # Wire skill extractor for end_task
        try:
            from tical_code.core.skill_extractor import SkillExtractor
            from tical_code.core.tool_executor import set_skill_extractor
            _se = SkillExtractor(workspace=cfg.get("workspace", "."), enabled=True, llm=self.llm)
            set_skill_extractor(_se)
            self.logger.info("SkillExtractor wired into tool_executor")
        except Exception as e:
            self.logger.debug("SkillExtractor wiring skipped: %s", e)

        # Wire MoleculeEngine for chain_exec
        try:
            from tical_code.core.molecule import MoleculeEngine, ModelRegistry, AtomRole
            from tical_code.core.tool_executor import set_molecule_engine
            _registry = ModelRegistry()
            if self.llm:
                _registry.register_api_provider(
                    name="default-api", failover=self.llm,
                    roles=[AtomRole.REASONER, AtomRole.EXECUTOR, AtomRole.VERIFIER,
                           AtomRole.GUARD, AtomRole.SYNTHESIZER, AtomRole.FORMATTER],
                    priority=0, description="Default API provider via ModelFailover")
            self._molecule_engine = MoleculeEngine(registry=_registry)
            set_molecule_engine(self._molecule_engine)
            self.logger.info("MoleculeEngine wired into tool_executor")
        except Exception as e:
            self.logger.debug("MoleculeEngine wiring skipped: %s", e)

        self.logger.info("AsyncWorker %s initialized", self.name)

    # ─────────────────────────────────────────────────────────
    # Session Routing
    # ─────────────────────────────────────────────────────────

    def _get_session_id(self, msg: Optional[Message] = None) -> str:
        """Derive a stable session ID from the message source, or return worker name if no message."""
        if msg is None:
            return self.name
        return f"{msg.source}:{msg.sender or msg.chat_id or 'default'}"

    async def run(self):
        """Async main loop - manages per-session tasks and channel polling.

        Blocks indefinitely until cancelled.  Each iteration:
          1. Polls all channels via run_in_executor (non-blocking)
          2. Routes each message to its session queue
          3. Cleans up stale sessions every 60 ticks
          4. Sleeps 1 second
        """
        loop = asyncio.get_running_loop()
        self.logger.info("AsyncWorker %s entering async main loop", self.name)

        # Recover interrupted sustained tasks on startup
        if self._sustained_task_mgr is not None:
            try:
                recovered = await self._sustained_task_mgr.recover_pending_tasks()
                if recovered:
                    self.logger.info(
                        "Recovered %d pending tasks from previous run",
                        len(recovered),
                    )
                    # Auto-re-enter: queue first recovered task for execution
                    try:
                        _first = recovered[0]
                        # Set flag so next channel message triggers task continuation
                        self._auto_resume_task_id = _first.task_id
                        self.logger.info(
                            "Auto-resume queued for task %s: %s",
                            _first.task_id, _first.description[:80],
                        )
                    except Exception as _re:
                        self.logger.warning("Auto-resume setup failed: %s", _re)
            except Exception as exc:
                self.logger.warning("Task recovery failed: %s", exc)

        # Auto-resume recovered task if one was found
        if self._auto_resume_task_id:
            try:
                _task = await self._sustained_task_mgr.run_task(
                    self._auto_resume_task_id
                )
                self.logger.info(
                    "Auto-resumed task: %s — %s",
                    _task.task_id, getattr(_task, 'description', ''),
                )
            except Exception as _e:
                self.logger.warning("Auto-resume failed: %s", _e)
            finally:
                self._auto_resume_task_id = None

        cleanup_counter = 0

        while True:
            try:
                # Phase 1 - Poll all channels concurrently
                cleanup_counter += 1
                if cleanup_counter % 60 == 0:
                    self.logger.debug(
                        "[WORKER-POLL] channels=%d types=%s",
                        len(self.channels),
                        [type(c).__name__ for c in self.channels],
                    )
                poll_tasks = [
                    loop.run_in_executor(None, channel.poll)
                    for channel in self.channels
                ]
                poll_results = await asyncio.gather(*poll_tasks, return_exceptions=True)
                for channel, result in zip(self.channels, poll_results):
                    if isinstance(result, Exception):
                        self.logger.warning("Channel %s poll failed: %s",
                                            type(channel).__name__, result)
                        continue
                    for msg in result:
                        session_id = self._get_session_id(msg)
                        await self._dispatch_to_session(session_id, channel, msg)

                # Periodic self-repair health check (every 50 iterations)
                if not hasattr(self, '_health_counter'):
                    self._health_counter = 0
                self._health_counter += 1
                if self._health_counter % 50 == 0:
                    try:
                        if hasattr(self, 'self_repair') and self.self_repair:
                            self.self_repair.check_health()
                    except Exception:
                        pass

                # Phase 2 - Periodic housekeeping
                if cleanup_counter % 60 == 0:
                    await self._cleanup_sessions()
                    # Detect and kill stuck session tasks (processing hung >threshold)
                    await self._kill_stuck_sessions()

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                self.logger.info("AsyncWorker loop cancelled - shutting down")
                await self._shutdown()
                return
            except Exception as e:
                self.logger.error(
                    "AsyncWorker loop error: %s\n%s",
                    e, traceback.format_exc(),
                )
                await asyncio.sleep(1)

    async def _dispatch_to_session(self, session_id: str, channel, msg: Message):
        """Route a message to the appropriate session queue.

        Creates the session queue and background processing task on
        first contact with a session ID.
        """
        async with self._async_lock:
            if session_id not in self._session_queues:
                self._session_queues[session_id] = asyncio.Queue(maxsize=100)
                self.session_manager.get_or_create(session_id, factory=dict)
                task = asyncio.create_task(
                    self._session_processor(session_id, channel),
                )
                self._session_tasks[session_id] = task
                self.logger.debug("Created session task: %s", session_id)
            else:
                self.session_manager.touch(session_id)

        await self._session_queues[session_id].put((channel, msg))

    async def _session_processor(self, session_id: str, channel):
        """Process messages for a single session in its own Task.

        Messages within a session are processed serially to preserve
        conversation history integrity.  Each session gets its own
        Task, so different sessions already run concurrently.
        Exits after ``_session_timeout`` seconds of inactivity.
        """
        queue = self._session_queues.get(session_id)
        if queue is None:
            return

        self.logger.debug("Session processor started: %s", session_id)
        while True:
            try:
                ch, msg = await asyncio.wait_for(
                    queue.get(), timeout=self._session_timeout,
                )
                # LIVE 2026-07-09i: drain to latest BEFORE processing (never race-consume messages)
                dropped = 0
                while True:
                    try:
                        ch, msg = queue.get_nowait()
                        dropped += 1
                    except Exception:
                        break
                if dropped:
                    self.logger.info(
                        "Drained %d older queued messages for session %s — processing latest only",
                        dropped, session_id,
                    )
                try:
                    await asyncio.wait_for(
                        self._process_message(session_id, ch, msg),
                        timeout=self._process_hard_timeout,
                    )
                    # ===== EITE Hook 2: post-reply recording =====
                    if hasattr(self, '_eite') and self._eite and self._eite.is_initialized:
                        try:
                            impact_vec = self._eite_text_to_vector(msg.content)
                            self._eite.record_decision(
                                context=msg.content[:200],
                                tool_name="reply",
                                impact_vector=impact_vec,
                                accepted=True,
                                justification="session_reply",
                            )
                        except Exception:
                            pass  # silent degrade
                    # ==============================================
                except asyncio.TimeoutError:
                    self.logger.error(
                        "Session %s message processing hard timeout (%ds) - "
                        "recovering for next message",
                        session_id, self._process_hard_timeout,
                    )
                    try:
                        channel.send(Response(
                            content="[async-worker] stopped a stuck long task (timeout). Send a shorter order (one step).",
                            target=getattr(msg, "sender", "unknown"),
                            source=getattr(msg, "source", "unknown"),
                            chat_id=getattr(msg, "chat_id", None),
                        ))
                    except Exception:
                        pass

            except asyncio.TimeoutError:
                self.logger.debug("Session %s idle timeout (%ds)",
                                  session_id, self._session_timeout)
                async with self._async_lock:
                    self._session_tasks.pop(session_id, None)
                    self._session_queues.pop(session_id, None)
                    self.session_manager.remove(session_id)
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    "Session %s processor error: %s\n%s",
                    session_id, e, traceback.format_exc(),
                )
                try:
                    channel.send(Response(
                        content=f"[async-worker] session error: {e}",
                        target=getattr(msg, "sender", "unknown"),
                        source=getattr(msg, "source", "unknown"),
                        chat_id=getattr(msg, "chat_id", None),
                    ))
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        self.logger.debug("Session processor ended: %s", session_id)

    async def _process_message(self, session_id: str, channel, msg: Message):
        """Process a single message through LLM call + tool execution.

        Manages session-local message history, invokes the LLM,
        handles tool call iterations (up to MAX_TOOL_ITERATIONS),
        formats the final response, and sends it back via the channel.
        """
        # SECURITY: enforce sender allowlist (env ALLOWED_SENDERS, comma-separated)
        _allowed_senders = os.getenv("ALLOWED_SENDERS", "").strip()
        if _allowed_senders:
            _allowed_set = {s.strip() for s in _allowed_senders.split(",") if s.strip()}
            _sender = getattr(msg, "sender", "")
            if _sender and _sender not in _allowed_set:
                logger.warning(f"SECURITY: blocked unauthorized sender '{_sender}' (not in ALLOWED_SENDERS)")
                return

        entry, created = self.session_manager.get_or_create(session_id, factory=dict)
        session = entry["data"]

        # EMERGENCY FAST_ACK 2026-07-09c: show typing immediately so user is not "double-ping" waiting
        try:
            if hasattr(channel, "send_action") and getattr(msg, "source", "") == "telegram" and getattr(msg, "chat_id", None):
                channel.send_action("typing", msg.chat_id)
        except Exception:
            pass


        messages: list = session.get("messages", [])
        # EMERGENCY LOAD_SESSION 2026-07-09:
        # We always save to SQLite but never reloaded — restart = total amnesia.
        # Load durable history when this in-memory session has no turns yet.
        # REFACTOR 2026-07-14: expanded to 30 messages + session summary for
        # robust context persistence across restarts.
        if (not messages or (len(messages) == 1 and messages[0].get("role") == "system")) and getattr(self, "sessions", None):
            try:
                _sid = self.sessions.get_session_id(msg.source, str(msg.chat_id))
                _loaded = self.sessions.load_session(_sid, max_messages=30)
                # Inject session summary for context restoration
                if hasattr(self.sessions, 'get_summary'):
                    _summary = self.sessions.get_summary(_sid)
                    if _summary:
                        _loaded.insert(0, {"role": "system", "content": f"[Session Context] {_summary}"})
                if _loaded:
                    messages = [{"role": "system", "content": self.system_prompt}] + _loaded
                    session["messages"] = messages
            except Exception as _le:
                self.logger.warning("Session load failed: %s", _le)

        # Prepend system prompt — DeepSeek rejects requests lacking role
        # on message[0], and without it the AI has no persona context.
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": self.system_prompt})

        # ===== Pre-LLM Module Enrichment (non-blocking, try/except all) =====
        # Inject anchor context, axiom annotations, and EITE reflection
        _enrichments = []

        # 1. Anchor context (identity/capabilities grounding)
        if hasattr(self, 'anchor_manager') and self.anchor_manager:
            try:
                _ctx = self.anchor_manager.get_context_prompt()
                if _ctx:
                    _enrichments.append(_ctx)
            except Exception:
                pass

        # 2. Axiom annotations (physics axioms as cognitive lenses)
        if hasattr(self, 'axioms') and self.axioms:
            try:
                _ann = self.axioms.annotate_decision(
                    decision=msg.content[:500],
                    context=session_id,
                )
                if _ann:
                    _lens = "\n".join(str(a) for a in _ann[:3])
                    if _lens:
                        _enrichments.append(f"[AXIOM LENSES]\n{_lens}")
            except Exception:
                pass

        # 3. EITE cognitive alignment check (non-blocking reflection)
        if hasattr(self, '_eite') and self._eite and self._eite.is_initialized:
            try:
                impact_vec = self._eite_text_to_vector(msg.content)
                ok, reason = self._eite.validate_tool("llm_call", impact_vec)
                if not ok:
                    _enrichments.append(f"[EITE: {reason}]")
                    logger.info("[EITE] hook1: dissonance injected into system prompt")
            except Exception:
                pass  # silent degrade

        # 4. Security Vigil — message scanner + alert context
        if hasattr(self, 'vigil') and self.vigil:
            try:
                scan_result = await self.vigil.check_message(str(msg.content))
                if scan_result.blocked:
                    logger.warning("[VIGIL] Message blocked: %s", scan_result.findings)
                    return msg  # return raw msg, caller handles blocked response
                if scan_result.findings:
                    _enrichments.append(
                        "[Security Vigil] Suspicious content detected: "
                        + "; ".join(scan_result.findings[:3])
                    )
                # Inject active alert context if any
                _ctx = self.vigil.get_context_injection()
                if _ctx:
                    _enrichments.append(_ctx)
            except Exception:
                logger.error("[VIGIL] check_message FAILED — blocking message for safety (fail-closed)")
                return msg  # fail-closed: block message if security scanner is unavailable

        if _enrichments:
            messages[0]["content"] = (
                messages[0].get("content", "") +
                "\n\n" + "\n\n".join(_enrichments)
            )
        # =====================================================================

        # Build user message — include media (images, file content, transcripts)
        if hasattr(msg, 'media_data') and msg.media_data:
            content_parts = [{"type": "text", "text": msg.content}]
            for _md in msg.media_data:
                if _md.get("type") == "image":
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{_md['mime']};base64,{_md['data']}"}
                    })
                elif _md.get("type") == "transcript":
                    content_parts.append({"type": "text", "text": f"[voice transcript: {_md['text']}]"})
                elif _md.get("type") == "document_text":
                    content_parts.append({"type": "text", "text": f"[File {_md.get('filename','')} content:\n{_md['text']}]"})
                elif _md.get("type") == "binary_saved":
                    content_parts.append({"type": "text", "text": f"[File saved: {_md.get('filename','')} at {_md.get('path','')} — {_md.get('note','binary')}]"})
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": msg.content})

        # REFACTOR 2026-07-12: removed _no_tool heuristic.
        # Always pass tools to the model. The model decides when to invoke them.
        # tools=[] caused hallucinated tool-call text output when the model knew
        # tools existed from the system prompt but could not invoke them via API.
        _raw = (msg.content or "").strip()
        try:
            # History cleanup: scrub poisoned context (fence spam, CSS dumps, oversized messages)
            cleaned = []
            for _m in messages:
                if not isinstance(_m, dict):
                    cleaned.append(_m)
                    continue
                _c = str(_m.get("content") or "")
                _fence = _c.count("```")
                if _m.get("role") == "assistant" and _fence >= 6:
                    continue
                if _m.get("role") == "assistant" and ("--bg" in _c or ":root" in _c) and len(_c) > 800:
                    continue
                if _m.get("role") == "assistant" and len(_c) > 6000:
                    cleaned.append({**_m, "content": _c[:1500] + "\n[truncated]"})
                else:
                    cleaned.append(_m)
            if cleaned and cleaned[0].get("role") == "system":
                sys_m = cleaned[0]
                rest = cleaned[1:]
                cleaned = [sys_m] + rest[-12:]
            messages = cleaned
        except Exception:
            pass
        response = await self._async_llm_call(messages)

        tool_iterations = 0
        # Parse Gateway XML tool calls from content (e.g. <tool_call>check_self</tool_call>)
        # and inject them as structured tool_calls so the loop below executes them.
        _gateway_tc, _stripped_content = self._parse_gateway_tool_calls(response.get("content", ""))
        if _gateway_tc:
            self.logger.info("[RPLY] parsed %d Gateway XML tool call(s) from content", len(_gateway_tc))
            # LIVE 2026-07-10: fill content with text description so the assistant
            # message in history explains what tools it called.  Without this the
            # model sees an empty assistant message + tool_calls in history, thinks
            # it didn't do anything yet, and keeps generating more tool calls.
            if _stripped_content:
                response["content"] = _stripped_content
            else:
                _names = []
                for _tc in _gateway_tc:
                    _fn = _tc.get("function", {})
                    _names.append(_fn.get("name", "") or _tc.get("name", "?"))
                response["content"] = "[calling: " + ", ".join(_names) + "]"
            existing = response.get("tool_calls") or []
            response["tool_calls"] = existing + _gateway_tc
        while response.get("tool_calls") and tool_iterations < MAX_TOOL_ITERATIONS:
            tool_iterations += 1
            # LIVE WIRE QUEUE PREEMPT 2026-07-09f
            try:
                _q = self._session_queues.get(session_id)
                if _q is not None and not _q.empty():
                    self.logger.info("Preempting tool loop for session %s — newer message queued", session_id)
                    break
            except Exception:
                pass

            # Progress visibility: every tool round (and keep-alive typing)
            try:
                if hasattr(channel, "send_action") and getattr(msg, "source", "") == "telegram" and getattr(msg, "chat_id", None):
                    channel.send_action("typing", msg.chat_id)
            except Exception:
                pass
            try:
                # EMERGENCY PROGRESS 2026-07-09:
                # Previous every-1/2-round chat progress flooded users ("worse than before").
                # Keep typing indicator only; emit at most a few sparse progress lines.
                # LIVE 2026-07-10: further reduced — only at 5, 10, 15, 20, 25
                # to prevent progress spam when many tool iterations run.
                names = []
                for _tc in response.get("tool_calls", []) or []:
                    _fn = _tc.get("function", {}) if isinstance(_tc, dict) else {}
                    _n = (_fn.get("name") if isinstance(_fn, dict) else None) or (_tc.get("name") if isinstance(_tc, dict) else None) or "?"
                    names.append(str(_n))
                tool_label = ",".join(names[:3]) if names else "tools"
                # Sparse: only every 5 rounds starting from 5
                if tool_iterations >= 5 and tool_iterations % 5 == 0:  # LIVE 2026-07-09: progress every 2 rounds
                    progress_text = format_progress(
                        "progress",
                        f"{tool_iterations}/{MAX_TOOL_ITERATIONS} running: {tool_label}",
                    )
                    channel.send(Response(
                        content=progress_text,
                        target=msg.sender,
                        source=msg.source,
                        chat_id=msg.chat_id,
                    ))
            except Exception:
                pass
            messages.append({"role": "assistant", **response})

            # Batch process tool calls: concurrent-safe tools run in parallel
            # via run_in_executor, non-concurrent-safe tools run sequentially
            # one-by-one to avoid workspace conflicts on shared state.
            tool_results = {}
            concurrent_tools = []
            serial_tools = []

            for tc in response["tool_calls"]:
                # Support both OpenAI format {"function":{"name":...,"arguments":...}}
                # and internal flat format {"name":...,"args":...}
                fn = tc.get("function", {})
                func_name = fn.get("name", "") or tc.get("name", "")
                concurrency_safe = TOOL_CONCURRENCY_MAP.get(func_name, False)
                if concurrency_safe:
                    concurrent_tools.append(tc)
                else:
                    serial_tools.append(tc)

            # Execute concurrent-safe tools in parallel via asyncio.gather
            if concurrent_tools:
                loop = asyncio.get_running_loop()
                exec_data = []
                coros = []
                for tc in concurrent_tools:
                    fn = tc.get("function", {})
                    func_name = fn.get("name", "") or tc.get("name", "")
                    raw_args = fn.get("arguments", {}) or tc.get("args", {})
                    tool_call_id = tc.get("id", "")
                    if isinstance(raw_args, str):
                        raw_args = json.loads(raw_args)
                    exec_data.append((tool_call_id, func_name))
                    coros.append(
                        loop.run_in_executor(None, execute, func_name, raw_args),
                    )

                gathered = await asyncio.gather(*coros, return_exceptions=True)
                for (tool_call_id, func_name), result in zip(exec_data, gathered):
                    if isinstance(result, Exception):
                        self.logger.error(
                            "Tool %s error: %s", func_name, result,
                        )
                        result_str = f"Error: {result}"
                    else:
                        result_str = str(result)[:8192]
                    tool_results[tool_call_id] = result_str

            # Execute non-concurrent-safe tools sequentially
            for tc in serial_tools:
                fn = tc.get("function", {})
                func_name = fn.get("name", "") or tc.get("name", "")
                func_args = fn.get("arguments", {}) or tc.get("args", {})
                tool_call_id = tc.get("id", "")
                try:
                    if isinstance(func_args, str):
                        func_args = json.loads(func_args)
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, execute, func_name, func_args)
                    result_str = str(result)[:8192]
                except Exception as e:
                    self.logger.error(
                        "Tool %s error: %s", func_name, e,
                    )
                    result_str = f"Error: {e}"
                tool_results[tool_call_id] = result_str

            # Append results in original order
            for tc in response["tool_calls"]:
                tool_call_id = tc.get("id", "")
                result_str = tool_results[tool_call_id]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_str,
                })

            try:
                # Phase 1 compaction: truncate long tool outputs before each LLM call
                try:
                    for _i, _m in enumerate(messages):
                        if _m.get("role") == "tool" and isinstance(_m.get("content"), str) and len(_m["content"]) > 8000:
                            _m["content"] = "[output truncated: " + _m["content"][:500] + f"... (" + str(len(_m["content"])) + " chars total)]"
                except Exception:
                    pass
                response = await self._async_llm_call(messages)
            except Exception as e:
                self.logger.error(
                    "LLM call after tool failed for session %s: %s",
                    session_id, e,
                )
                break

        # Collect tool names used for potential fallback
        tool_names_used = []
        for tc in response.get("tool_calls", []):
            fn = tc.get("function", {})
            name = fn.get("name", "") or tc.get("name", "")
            if name:
                tool_names_used.append(name)

        content = response.get("content", "") or ""
        # LIVE 2026-07-09p: strip fake text tool calls GLM outputs in no-tool mode
        if tool_iterations == 0 and content:
            import re as _re
            _fake_patterns = [
                r'^\s*`{0,3}\s*(?:antml:)?invoke\b',
                r'^\s*`{0,3}\s*(?:antml:)?parameter\b',
                r'^\s*`{0,3}\s*(?:antml:)?function_call\b',
                r'^\s*`{0,3}\s*(?:antml:)?tool_call\b',
                r'<tool_call\b',
            ]
            _is_fake = any(_re.search(p, content) for p in _fake_patterns)
            if _is_fake:
                self.logger.warning("[RPLY] detected fake text tool call in no-tool mode, stripping")
                self.logger.info("[RPLY] raw content before strip: %r", content[:500])
                # Strip full <tool_call>...</tool_call> blocks FIRST (before the
                # line-level regex below, whose [^\n]* would swallow everything
                # after the first 'tool_call' keyword, leaving orphan '<').
                content = _re.sub(r'<tool_call>[^<]*</tool_call>', '', content)
                content = _re.sub(r'`{0,3}\s*(?:antml:)?(?:invoke|parameter|function_call|tool_call)\b[^\n]*\n?', '', content)
                content = _re.sub(r'\s*</?tool_call>\s*', '', content)
                content = _re.sub(r'\s*</?arg_key>\s*', '', content)
                content = _re.sub(r'\s*</?arg_value>\s*', '', content)
                content = content.strip()
                # Also strip orphan closing fences
                content = _re.sub(r'^\s*`{3}\s*$', '', content, flags=_re.MULTILINE).strip()
                if not content:
                    content = "[system: received empty response, please retry]"
        self.logger.info("[RPLY] tool_iterations=%d content_len=%d tools=%s",
                         tool_iterations, len(content), tool_names_used[:3])

        # If model did tools but no text reply, force a summary WITHOUT tools
        if not content.strip() and tool_iterations > 0:
            self.logger.info("[RPLY] forcing text summary after %d tool rounds", tool_iterations)
            messages.append({
                "role": "user",
                "content": "Now reply to the user with a clear summary of what you found. "
                           "Format the results — use bullet lists, group related findings, "
                           "and highlight key file names and their purposes. "
                           "Output the summary directly, do not call any tools."
            })
            try:
                # Call WITHOUT tools — model can't call tools, must produce text
                text_resp = await self._async_llm_call(messages,)
            except Exception as e:
                self.logger.error("[RPLY] summary call failed: %s", e)
                text_resp = {"content": "", "tool_calls": None}
            content = (text_resp.get("content") or "").strip()
            if not content:
                self.logger.info("[RPLY] summary still empty, using best tool result")
                best_proof = ""
                for m in reversed(messages):
                    if isinstance(m, dict) and m.get("role") == "tool":
                        r = (m.get("content") or "").strip()
                        if len(r) > len(best_proof):
                            best_proof = r
                content = f"[shell]\n{best_proof[:3000]}" if best_proof else f"[ops: {', '.join(tool_names_used[:6])}]"
            # Truth reporter verification before sending response
            _verified = content
            if getattr(self, 'truth_reporter', None) and self.truth_reporter:
                try:
                    if not self.truth_reporter.verify_before_report(operation="reply"):
                        self.logger.warning("[TruthReporter] Summary reply verification failed")
                except Exception:
                    pass
            channel.send(Response(
                content=_verified,
                target=msg.sender, source=msg.source, chat_id=msg.chat_id,
            ))
            messages.append({"role": "assistant", "content": content})
            session["messages"] = messages
            self.session_manager.touch(session_id)
            # Persist to SQLite for cross-restart memory
            if getattr(self, 'sessions', None):
                try:
                    # EMERGENCY SAVE DEDUP 2026-07-09: only append new turns (was re-inserting full history)
                    _sid = self.sessions.get_session_id(msg.source, str(msg.chat_id))
                    _prev = int(session.get("last_saved_len") or 0)
                    # skip system at [0] when counting durable turns if needed
                    _to_save = messages[_prev:] if _prev < len(messages) else []
                    # never re-save pure system-only
                    _to_save = [m for m in _to_save if m.get("role") != "system" or _prev == 0]
                    if _to_save:
                        self.sessions.save_messages(_sid, _to_save)
                    session["last_saved_len"] = len(messages)
                except Exception as _e:
                    self.logger.warning("Session save failed: %s", _e)

            return
        if content.strip():
            try:
                formatted = format_final_reply(content)
            except Exception:
                formatted = content
            # LIVE 2026-07-09k: kill useless stall phrases / residual fence spam
            try:
                _ft = (formatted or "").strip()
                _stall = (
                    _ft.count("```") >= 4
                    or "\u5148\u6838\u5bf9" in _ft
                )
                if _stall:
                    formatted = "Empty or fence-spam blocked. Send a concrete order."
            except Exception:
                pass

            # Truth reporter verification before sending
            if getattr(self, 'truth_reporter', None) and self.truth_reporter:
                try:
                    if not self.truth_reporter.verify_before_report(operation="reply"):
                        self.logger.warning("[TruthReporter] Reply verification failed — sending anyway")
                except Exception:
                    pass

            # LIVE 2026-07-09f: self-evolve success record
            try:
                if self._self_evolve and tool_iterations > 0:
                    import asyncio as _aio
                    loop = _aio.get_event_loop()
                    async def _rec_ok():
                        await self._self_evolve.record_success({
                            "category": "tool_turn",
                            "description": f"completed with {tool_iterations} tool rounds",
                        })
                    if loop.is_running():
                        loop.create_task(_rec_ok())
                    else:
                        loop.run_until_complete(_rec_ok())
            except Exception:
                pass
        # LIVE 2026-07-09n: flag for task continuation
        _explicit_continue = False
        try:
            if hasattr(msg, "text") and str(getattr(msg, "text", "")).strip() in ("\u505a", "\u505a\u554a", "\u7ee7\u7eed", "go", "do"):
                _explicit_continue = True
            if _explicit_continue and self._sustained_task_mgr:
                import asyncio as _aio2
                _pending = getattr(self._sustained_task_mgr, 'list_active', None)
                if _pending is None:
                    # SustainedTaskManager uses list_tasks() not list_active()
                    _tasks = _aio2.run(self._sustained_task_mgr.list_tasks(
                        state=None, limit=20
                    ))
                else:
                    _tasks = _pending()
                if _tasks:
                    self.logger.info("Task continuation requested - %d pending tasks", len(_tasks))
        except Exception:
            pass

        channel.send(Response(
            content=formatted,
            target=msg.sender,
            source=msg.source,
            chat_id=msg.chat_id,
        ))

        messages.append({"role": "assistant", "content": content})
        session["messages"] = messages
        self.session_manager.touch(session_id)
        # Persist to SQLite for cross-restart memory
        if getattr(self, 'sessions', None):
            try:
                _sid = self.sessions.get_session_id(msg.source, str(msg.chat_id))
                # EMERGENCY SAVE DEDUP 2026-07-09
                _prev = int(session.get('last_saved_len') or 0)
                _to_save = messages[_prev:] if _prev < len(messages) else []
                _to_save = [m for m in _to_save if m.get('role') != 'system' or _prev == 0]
                if _to_save:
                    self.sessions.save_messages(_sid, _to_save)
                session['last_saved_len'] = len(messages)
            except Exception as _e:
                self.logger.warning("Session save failed: %s", _e)

    async def _async_llm_call(self, messages: list, tools=None) -> dict:
        """Make an async LLM call via the worker's LLM backend.

        Uses self.llm (the AsyncWorker's backend — always properly
        initialised) with a fallback to self.failover.  Handles both
        ModelFailover (async .call()) and OpenAIBackend (sync .call())
        transparently.

        Protected by _llm_hard_timeout (default 120s) to prevent worker
        death when the LLM API hangs indefinitely. Without this guard,
        a single hung HTTP request can block the session processor
        forever, causing the entire worker to stop responding.
        """
        _backend = self.llm or self.failover
        if _backend is None:
            return {"content": "Error: LLM backend not available", "tool_calls": None}

        _call_fn = getattr(_backend, "call", None)
        if _call_fn is None:
            return {"content": "Error: LLM backend has no call() method", "tool_calls": None}

        _timeout = self._llm_hard_timeout
        tool_schemas_arg = self.tool_schemas if tools is None else tools
        try:
            if asyncio.iscoroutinefunction(_call_fn):
                # ModelFailover — async .call(), await with hard timeout
                result = await asyncio.wait_for(
                    _call_fn(messages, tools=tool_schemas_arg),
                    timeout=_timeout,
                )
            else:
                # OpenAIBackend — sync .call(), offload to executor with hard timeout
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, _call_fn, messages, tool_schemas_arg,
                    ),
                    timeout=_timeout,
                )
        except asyncio.TimeoutError:
            self.logger.error(
                "[AsyncWorker] LLM call hard timeout (%ds) - "
                "cancelling to prevent worker death",
                _timeout,
            )
            return {"content": "Error: LLM call timed out", "tool_calls": None}

        # FailoverResult is a dataclass, not a dict — callers expect
        # subscript access like result["tool_calls"].  Normalise.
        if not isinstance(result, dict):
            result = {
                "content": getattr(result, "content", ""),
                "tool_calls": getattr(result, "tool_calls", None) or [],
                "reasoning_content": getattr(result, "reasoning_content", ""),
            }
        return result

    def _parse_gateway_tool_calls(self, content: str) -> tuple:
        """Parse Gateway XML tool calls from LLM content text.

        Gateway models sometimes output tool calls as inline XML instead of
        structured JSON tool_calls.  The format is:

            <tool_call>tool_name
            <arg_key>param_name</arg_key>
            <arg_value>param_value</arg_value>
            </tool_call>

        Returns a tuple (parsed_list, stripped_content) where parsed_list
        contains OpenAI-format tool-call dicts and stripped_content has the
        XML tags removed.  Returns ([], content) if nothing to parse.
        """
        import re as _re, json, uuid
        if not content or "<tool_call" not in content:
            return ([], content)
        _parsed = []
        # Match each <tool_call>...</tool_call> block and strip them from content
        _stripped = _re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=_re.DOTALL).strip()
        for _block in _re.finditer(r'<tool_call>(.*?)</tool_call>', content, _re.DOTALL):
            _inner = _block.group(1).strip()
            if not _inner:
                continue
            # First line / first word is the tool name
            _lines = _inner.split('\n')
            _name = _lines[0].strip().split()[0] if _lines else ""
            if not _name:
                continue
            # Extract key-value pairs from <arg_key>/<arg_value> tags
            _args = {}
            for _kv in _re.finditer(r'<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>', _inner, _re.DOTALL):
                _k = _kv.group(1).strip()
                _v = _kv.group(2).strip()
                if _k:
                    _args[_k] = _v
            _arguments_json = json.dumps(_args)
            _parsed.append({
                "id": f"call_gateway_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": _name,
                    "arguments": _arguments_json,
                },
            })
        return (_parsed, _stripped)

    async def _kill_stuck_sessions(self):
        """Detect and kill session tasks that have been stuck too long.

        A session task is considered stuck if it has been running for
        longer than _session_stuck_threshold seconds without completing
        a message. This typically happens when the LLM API hangs and
        the hard timeout fails to cancel the coroutine (e.g., aiohttp
        not respecting cancellation).

        Killing the stuck task allows the session to be recreated on
        the next incoming message, restoring worker responsiveness.
        """
        import time as _t
        now = _t.time()
        async with self._async_lock:
            stuck_sids = []
            for sid, task in list(self._session_tasks.items()):
                if task.done():
                    continue
                # Check if task has been running too long by inspecting
                # when it was last active (session touch timestamp).
                entry = self.session_manager._sessions.get(sid)
                if entry is None:
                    continue
                last_active = entry.get("last_access", 0)
                if last_active > 0 and (now - last_active) > self._session_stuck_threshold:
                    stuck_sids.append(sid)

            for sid in stuck_sids:
                task = self._session_tasks.pop(sid, None)
                self._session_queues.pop(sid, None)
                self.session_manager.remove(sid)
                if task and not task.done():
                    task.cancel()
                    self.logger.warning(
                        "Killed stuck session task: %s (no activity for %ds)",
                        sid, self._session_stuck_threshold,
                    )

    async def _cleanup_sessions(self):
        """Remove expired sessions from SessionManager and cancel their tasks."""
        expired = self.session_manager.cleanup_expired()
        if expired:
            self.logger.info("Cleaned up %d expired sessions", len(expired))

        async with self._async_lock:
            active_sids: set[str] = set()
            with self.session_manager._lock:
                active_sids = set(self.session_manager._sessions.keys())

            for sid in list(self._session_tasks.keys()):
                if sid not in active_sids:
                    task = self._session_tasks.pop(sid, None)
                    if task and not task.done():
                        task.cancel()
                    self._session_queues.pop(sid, None)
                    self.logger.debug("Removed orphaned session task: %s", sid)

    async def _shutdown(self):
        """Cancel all session tasks and clear queues."""
        self.logger.info("Shutting down AsyncWorker - cancelling %d session tasks",
                         len(self._session_tasks))
        async with self._async_lock:
            for sid, task in list(self._session_tasks.items()):
                if not task.done():
                    task.cancel()
            self._session_tasks.clear()
            self._session_queues.clear()

    def get_session_status(self) -> dict:
        """Return diagnostics about active sessions."""
        return {
            "active_sessions": self.session_manager.active_count(),
            "session_timeout": self._session_timeout,
        }

def main():
    """Entry point for the tical-code unified worker.

    Performs startup in this order:
      1. Acquires a PID lock to prevent duplicate instances.
      2. Loads configuration via load_config().
      3. Instantiates the Worker with the loaded config.
      4. Restores session state from snapshot and checkpoint
         (both are best-effort, non-blocking on failure).
      5. Calls worker.run() - enters the main orchestrator loop.

    The PID lock file is always cleaned up in the finally block,
    even on crash or signal-triggered exit.
    """
    logger.info("tical-code worker starting")

    # PID lock - prevent duplicate instances
    PID_FILE = Path("/tmp/unified-worker.pid")
    try:
        existing = int(PID_FILE.read_text().strip())
        if os.path.exists(f"/proc/{existing}"):
            logger.error(f"Another worker is already running (PID={existing}) - exiting")
            sys.exit(1)
        else:
            logger.warning(f"Stale PID file ({existing}) - overwriting")
    except (FileNotFoundError, ValueError):
        pass
    try:
        PID_FILE.write_text(str(os.getpid()))
    except PermissionError:
        import logging
        logging.getLogger("tical-code.worker").error(
            f"Cannot write PID file {PID_FILE}: permission denied - exiting"
        )
        sys.exit(1)

    try:
        cfg = load_config()
        worker = Worker(cfg)
        # Restore from last snapshot if available
        if load_latest_snapshot is not None:
            try:
                snapshot = load_latest_snapshot(cfg['name'])
                if snapshot:
                    logger.info("Restored from session snapshot: %s", snapshot.get('_meta', {}).get('saved_at_iso', '?'))
                    pending = snapshot.get('pending_tool_calls')
                    if pending:
                        logger.info("Restoring %d pending tool calls from snapshot", len(pending))
                        worker._pending_task = snapshot
            except Exception as e:
                logger.warning("Snapshot restore failed (non-blocking): %s", e)
        # Restore from checkpoint if incomplete checkpoint exists
        if getattr(worker, 'checkpoint', None) is not None:
            try:
                incomplete = worker.checkpoint.list_checkpoints(status="incomplete")
                if incomplete:
                    cp_id = incomplete[0]["id"]
                    logger.info("Found %d incomplete checkpoints - restoring latest (%s)", len(incomplete), cp_id)
                    # Restore file snapshots (confirm=True - required by CheckpointConfig default)
                    worker.checkpoint.restore(cp_id, confirm=True)
                    # NOTE: Conversation messages are NOT restored from checkpoint.
                    # Loading old messages pollutes the new context and causes
                    # the AI to reply about stale topics (e.g. Pro7 tunnel debug
                    # mixed into "audit code" requests). Fresh conversation only.
            except Exception as e:
                logger.warning("Checkpoint restore failed (non-blocking): %s", e)
        worker.run()
    finally:
        PID_FILE.unlink(missing_ok=True)


def async_main():
    """Async entry point for the AsyncWorker.

    Performs startup in this order:
      1. Acquires a PID lock to prevent duplicate instances.
      2. Loads configuration via load_config().
      3. Instantiates the AsyncWorker with the loaded config.
      4. Calls asyncio.run(worker.run()) - enters the async
         main orchestrator loop.

    The PID lock file is always cleaned up in the finally block,
    even on crash or signal-triggered exit.

    To use: ASYNC_WORKER=1 python -m tical_code.core.unified_worker
    """
    # SIGTERM handler - ensures finally block runs on kill
    def _sigterm_handler(signum, frame):
        logger.warning("Received SIGTERM - shutting down")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # PID lock - prevent duplicate instances
    PID_FILE = Path("/tmp/unified-worker.pid")
    try:
        existing = int(PID_FILE.read_text().strip())
        if os.path.exists(f"/proc/{existing}"):
            logger.error(f"Another worker is already running (PID={existing}) - exiting")
            sys.exit(1)
        else:
            logger.warning(f"Stale PID file ({existing}) - overwriting")
    except (FileNotFoundError, ValueError):
        pass
    try:
        PID_FILE.write_text(str(os.getpid()))
    except PermissionError:
        import logging
        logging.getLogger("tical-code.worker").error(
            f"Cannot write PID file {PID_FILE}: permission denied - exiting"
        )
        sys.exit(1)
    logger.info("tical-code async-worker starting (PID=%d)", os.getpid())

    try:
        cfg = load_config()
        worker = AsyncWorker(cfg)
        asyncio.run(worker.run())
    finally:
        PID_FILE.unlink(missing_ok=True)

if __name__ == "__main__":
    try:
        # AsyncWorker is the default and only supported path.
        # Sync Worker is deprecated — use AsyncWorker for proper async/await,
        # single event loop, per-session task isolation, and hard timeouts.
        async_main()
    except Exception:
        import json as _json
        import traceback as _tb
        import sys as _sys
        import shutil as _shutil
        _diag = {
            "exception": repr(_sys.exc_info()[1]),
            "traceback": _tb.format_exc(),
            "stage": "startup",
            "tg_token_set": bool(os.environ.get("TG_BOT_TOKEN")),
            "api_key_set": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "disk_free_gb": _shutil.disk_usage("/").free // (1024**3),
        }
        try:
            with open("/tmp/crash_diagnostics.json", "w") as _f:
                _json.dump(_diag, _f, indent=2)
            logger.critical("Worker crash: diagnostics written to /tmp/crash_diagnostics.json")
        except Exception:
            pass
        raise
