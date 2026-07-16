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
Module definitions - every optional worker module registered in one place.

Architecture overview
---------------------

**@register decorator flow:**
Each module init function is decorated with ``@register(...)`` from
``module_registry.py``.  The decorator instantiates a ``ModuleSpec``
dataclass containing the module's name, worker attribute name, config
gate key, dependencies, profile affiliation, and a human-readable
description.  The spec is stored in a module-global ``_registry`` dict.

**Profile filtering (full vs. light):**
Modules declare a ``profile`` parameter: ``"full"`` (tical‑code, the
heavy, feature-complete deployment) or ``"light"`` (EITElite, the
stripped-down public deployment).  When ``Worker.__init__`` calls
``load_modules(worker, cfg, profile=...)``, the loader skips any
module whose ``spec.profile == "full"`` when the requested profile is
``"light"``.  Light-profile modules load in both profiles.  This
mechanism keeps the EITElite binary lean while sharing the same
codebase.

**Prompt injection:**
The ``description`` string provided to each ``@register`` call is
NOT a developer comment - it is injected verbatim into the AI's
system prompt via ``get_active_descriptions()`` → ``prompt.py``.
Write each description as a capability statement that the AI can
reason about: what the module does, when to use it, and what trigger
phrases it responds to.  Avoid implementation details (class names,
internal interfaces) - the AI only needs to know WHAT IT CAN DO.

**Load order and dependencies:**
``load_modules()`` performs a topological sort of the registry so
that dependencies are initialized before dependents.  Each init
function receives the worker instance and config, returns an
instance (or ``True`` for modules that wire themselves into
tool_executor), and the result is set as a worker attribute.

This file replaces the ~470 lines of try/except blocks formerly in
unified_worker.py.  Adding a new module now requires only a single
@register decorator - no changes to Worker.__init__.
"""

import logging
import os
from pathlib import Path
from typing import Any

from tical_code.core.module_registry import register

logger = logging.getLogger("tical-code.modules")


# =============================================================================
# Core modules (always-on, light profile)
# =============================================================================

@register(
    name="sessions",
    config_key="sessions",
    default_enabled=True,
    description=(
        "Conversation memory: stores full chat history per user/session in SQLite. "
        "You CAN recall what was said earlier in a conversation or across restarts. "
        "Use when: user references something from earlier, asks 'do you remember', "
        "or you need context from a prior exchange."
    ),
    profile="light",
)
def _init_sessions(worker: Any, cfg: dict):
    from tical_code.core.modules.session_manager import SessionManager
    w = cfg.get("workspace", ".")
    return SessionManager(db_path=str(Path(w) / "sessions.db"))


@register(
    name="compactor",
    config_key="compactor",
    default_enabled=True,
    description=(
        "Context manager: automatically summarizes old messages when conversation "
        "approaches the model's token limit. You do NOT need to manually truncate "
        "history - the compactor runs transparently. Keeps latest 12 messages intact, "
        "compresses older ones. Works with server-side context-compression plugin "
        "for OpenRouter providers as an additional layer."
    ),
    profile="light",
)
def _init_compactor(worker: Any, cfg: dict):
    from tical_code.core.modules.context_compactor import ContextCompactor
    _workspace = cfg.get("workspace", ".")
    return ContextCompactor(
        max_tokens=24000,
        keep_recent=12,
        persist_dir=str(Path(_workspace) / ".tical" / "compactor"),
    )


# =============================================================================
# Safety modules (light profile - available for EITElite too)
# =============================================================================

@register(
    name="doom_detector",
    config_key="doom_loop",
    default_enabled=True,
    description=(
        "Loop protection: detects when you are stuck repeating the same actions "
        "(4 detection engines: repeat detection, ping-pong detection, poll-without-progress, "
        "cross-agent loops). When triggered, it FORCES you to break out with a direct reply. "
        "You do not control this - it is an automatic safety net."
    ),
    profile="light",
)
def _init_doom_detector(worker: Any, cfg: dict):
    from tical_code.core.doom_loop import DoomLoopDetector, DoomLoopConfig
    return DoomLoopDetector(DoomLoopConfig(
        enabled=True,
        history_size=30,
        warn_threshold_base=8,
        critical_threshold_base=15,
        adaptive_enabled=True,
        recovery_enabled=True,
    ))


@register(
    name="constitution",
    config_key="constitution",
    default_enabled=True,
    description=(
        "Safety boundaries: blocks destructive or unauthorized actions BEFORE execution. "
        "If you attempt rm -rf /, drop databases, modify system files outside workspace, "
        "or exceed permissions, this module blocks it. When you see [CONSTITUTION BLOCKED], "
        "do NOT retry the same action - find an alternative approach."
    ),
    profile="light",
)
def _init_constitution(worker: Any, cfg: dict):
    from tical_code.core.constitution import ConstitutionEnforcer, ConstitutionTemplate
    default_constitution = ConstitutionTemplate.get_template("default")
    return ConstitutionEnforcer(default_constitution)


@register(
    name="truth_reporter",
    config_key="truthful_reporting",
    default_enabled=True,
    description=(
        "Honesty verification: cross-checks your claims against actual tool outputs. "
        "If you say 'file written successfully' but the write failed, this module catches it. "
        "You CAN use this to verify your own claims before reporting to the user. "
        "Tracks trust score per agent."
    ),
    profile="light",
)
def _init_truth_reporter(worker: Any, cfg: dict):
    try:
        from tical_code.core.truthful_reporting import TruthReporter
    except ImportError:
        return None
    return TruthReporter()


@register(
    name="security_baseline",
    config_key="security_baseline",
    default_enabled=True,
    description=(
        "Security enforcement: validates all file paths (TOCTOU-safe), blocks SSRF attacks "
        "on private IPs (10.x, 127.x, 192.168.x, 172.16.x), and redacts API keys/tokens "
        "from logs and tool output. Runs automatically - you do not need to invoke it."
    ),
    profile="light",
)
def _init_security_baseline(worker: Any, cfg: dict):
    from tical_code.core.security_baseline import (
        PathSafetyConfig,
        URLSafetyConfig,
        OutboundConfig,
    )
    from tical_code.core.tool_executor import configure_security as _configure_tool_security
    _sandbox_allowed_dirs = [cfg.get("workspace", ".")]
    _security_path_cfg = PathSafetyConfig(
        allowed_dirs=_sandbox_allowed_dirs,
        deny_symlinks=True,
        deny_absolute=True,
    )
    _security_url_cfg = URLSafetyConfig(
        allowed_schemes=frozenset(('http', 'https')),
        allow_private_ip=False,
        check_dns_rebinding=True,
    )
    _security_outbound_cfg = OutboundConfig(url_config=_security_url_cfg)
    _configure_tool_security(
        path_cfg=_security_path_cfg,
        url_cfg=_security_url_cfg,
        outbound_cfg=_security_outbound_cfg,
    )
    # Security baseline wires into tool_executor; no instance to store.
    return True


# =============================================================================
# Full-profile modules (tical-code only - heavy features)
# =============================================================================

@register(
    name="verification",
    config_key="verification",
    default_enabled=True,
    description=(
        "Identity-bound verification: every tool output is verified against the "
        "EITE identity system. Reply scanning catches fabricated results. "
        "You CAN use verify_multi tool (if available) to cross-check answers "
        "across multiple AI models before taking high-stakes actions."
    ),
    profile="light",
)
def _init_verification(worker: Any, cfg: dict):
    from tical_code.core.eite.verify_engine_v2 import VerificationEngine
    from tical_code.core.trace.verification_recorder import VerificationEventRecorder
    worker.verif_recorder = VerificationEventRecorder()
    return VerificationEngine(
        identity_id=cfg["name"],
        workspace=cfg.get("workspace", ""),
    )


@register(
    name="trace_recorder",
    attr_name="tracer",
    config_key="trace_recorder",
    default_enabled=True,
    description=(
        "Training data: records conversation traces for future model improvement. "
        "Strips sensitive data before storage. Runs in background - no action needed."
    ),
    profile="full",
)
def _init_trace_recorder(worker: Any, cfg: dict):
    from tical_code.core.trace_recorder import TraceRecorder
    from tical_code.core.config import get_data_collection_config
    dc = get_data_collection_config(cfg)
    return TraceRecorder(system_name=cfg.get("name", "eitelite"), enabled=dc["enabled"])


@register(
    name="decision_engine",
    config_key="decision_engine",
    default_enabled=True,
    description=(
        "Structured reasoning: before taking any action, runs a 6-step pipeline: "
        "pre_check (validate request) -> clarify (ask if ambiguous) -> detect_conditions "
        "(identify constraints) -> tool_strategy (plan tool sequence) -> execute -> verify_results. "
        "You CAN rely on this to catch mistakes before they happen."
    ),
    dependencies=["constitution"],
    profile="light",  # v0.8.4: EITE anti-hallucination core - must load on all nodes
)
def _init_decision_engine(worker: Any, cfg: dict):
    try:
        from tical_code.core.decision_engine import DecisionEngine
    except ImportError:
        return None
    return DecisionEngine(
        max_iterations=cfg.get("max_tool_iterations", 15),
        constitution_enforcer=worker.constitution,
        agent_type=cfg.get("agent_type", "default"),
        cognitive_workspace=getattr(worker, 'cognitive_workspace', None),
    )


@register(
    name="cognitive_workspace",
    config_key="cognitive_workspace",
    default_enabled=False,
    description=(
        "Cognitive workspace: tracks goals, hypotheses, beliefs, and decision traces. "
        "Provides a shared state hub so modules can read/write cognitive context. "
        "When enabled, the agent maintains a structured internal state that is "
        "injected into the system prompt for self-aware reasoning."
    ),
    dependencies=[],
    profile="full",
)
def _init_cognitive_workspace(worker: Any, cfg: dict):
    try:
        from tical_code.core.feature_flags import flags
        from tical_code.core.workspace import Workspace
    except ImportError:
        return None
    if not flags.cognitive_enabled:
        return None
    ws_cfg = cfg.get("cognitive_workspace", {})
    ws_path = getattr(worker, 'workspace', '')
    persist_path = Path(ws_path) / ".cognitive" if ws_path else None
    return Workspace(
        node_id=cfg.get("name", "default"),
        persist_path=persist_path,
    )


@register(
    name="checkpoint",
    config_key="checkpoint",
    default_enabled=True,
    description=(
        "Crash recovery: automatically saves conversation state to disk every few turns. "
        "If the worker crashes or restarts, it resumes from the last checkpoint. "
        "Keeps 20 most recent snapshots (max 200MB). No manual action needed."
    ),
    profile="light",
)
def _init_checkpoint(worker: Any, cfg: dict):
    try:
        from tical_code.core.checkpoint import CheckpointManager, CheckpointConfig, PruningStrategy
    except ImportError:
        return None
    chk_cfg = CheckpointConfig(
        workspace=cfg.get("workspace", "."),
        pruning_strategy=PruningStrategy.KEEP_RECENT_N,
        pruning_keep_n=20,
        max_storage_mb=200,
    )
    manager = CheckpointManager(config=chk_cfg)
    # Inject into tool_executor for checkpoint_list / checkpoint_restore tools
    try:
        from tical_code.core.tool_executor import set_checkpoint_manager
        set_checkpoint_manager(manager)
    except Exception:
        pass
    return manager


@register(
    name="self_repair",
    config_key="self_repair",
    default_enabled=True,
    description=(
        "Auto-healing: monitors worker health (memory, error rate, response time). "
        "When it detects degradation, it can: (1) restart the worker, (2) restore "
        "from checkpoint, (3) run diagnostic checks. Triggered automatically; "
        "you do not control when it fires."
    ),
    dependencies=["checkpoint"],
    profile="light",
)
def _init_self_repair(worker: Any, cfg: dict):
    try:
        from tical_code.core.self_repair import SelfRepairEngine
    except ImportError:
        return None
    engine = SelfRepairEngine(framework=worker)
    # Inject into tool_executor so safe_modify tools can use this engine
    try:
        from tical_code.core.tool_executor import set_self_repair_engine
        set_self_repair_engine(engine)
    except Exception:
        pass
    return engine


@register(
    name="memory_store",
    config_key="memory_store",
    default_enabled=True,
    description=(
        "Knowledge base: FTS5 full-text search over past conversations, saved facts, "
        "and learned workflows. You CAN search past sessions to recall what was done "
        "before. Use the memory_search tool to query. Automatically indexed."
    ),
    profile="full",
)
def _init_memory_store(worker: Any, cfg: dict):
    try:
        from tical_code.core.memory_store import MemoryFTSStore
    except ImportError:
        return None
    mem_dir = str(Path.home() / ".tical-code" / "memory")
    return MemoryFTSStore(memory_dir=mem_dir)


@register(
    name="hunk_tracker",
    config_key="hunk_tracker",
    default_enabled=True,
    description=(
        "File version history: every write/patch is tracked as a unified diff hunk. "
        "You CAN view history with hunk_history, rollback to any prior version with "
        "hunk_rollback, and see diff_since a given turn. Git-aware: auto-baselines "
        "against HEAD for git-tracked files."
    ),
    profile="light",
)
def _init_hunk_tracker(worker: Any, cfg: dict):
    try:
        from tical_code.core.hunk_tracker import register_hunk_tools
        register_hunk_tools()
        return True
    except Exception:
        return None


@register(
    name="path_security",
    config_key="path_security",
    default_enabled=True,
    description=(
        "Path security layer: blocks operations on system-critical paths "
        "(/etc/, /sys/, .git/config, etc.) via configurable glob deny patterns. "
        "Every file read/write/patch is checked. Deny hits are audited to SQLite. "
        "Runs automatically - no manual invocation needed."
    ),
    profile="light",
)
def _init_path_security(worker: Any, cfg: dict):
    try:
        from tical_code.core.path_security import get_path_security
        get_path_security()  # init singleton with config
        from tical_code.core.tool_executor import set_path_security
        set_path_security(get_path_security())
        return True
    except Exception:
        return None



@register(
    name="message_adapter",
    attr_name="_msg_adapter",
    config_key="message_adapter",
    default_enabled=True,
    description=(
        "Provider compatibility: adapts message format between different model providers "
        "(MiMo, OpenRouter, DeepSeek). Handles reasoning_content stripping, tool-call "
        "format normalization, and 429 rate-limit backoff. Runs automatically."
    ),
    profile="full",
)
def _init_message_adapter(worker: Any, cfg: dict):
    try:
        from tical_code.core.message_adapter import MessageAdapter
    except ImportError:
        return None
    return MessageAdapter()


@register(
    name="memory_profiler",
    attr_name="_memprof",
    config_key="memory_profiler",
    default_enabled=True,
    description=(
        "Memory watchdog: tracks RSS memory usage, forces garbage collection when "
        "threshold is reached (1000MB), and triggers restart if memory keeps growing. "
        "Prevents the Taiwan memory leak from causing outages. Runs automatically."
    ),
    profile="full",
)
def _init_memory_profiler(worker: Any, cfg: dict):
    try:
        from tical_code.core.memory_profiler import MemoryProfiler
    except ImportError:
        return None
    prof = MemoryProfiler(
        worker_name=cfg['name'],
        sample_interval_steps=25,
        snapshot_dir=str(Path(cfg.get("workspace", ".")) / ".memory-snapshots"),
    )
    prof.start()
    return prof


@register(
    name="provider_failover",
    attr_name="_failover_mod",
    config_key="provider_failover",
    default_enabled=True,
    description=(
        "Multi-provider resilience: 7-model failover chain (MiMo×4 → GPT-OSS-120B → "
        "Kimi K2.6 → DeepSeek). If one provider is rate-limited or down, automatically "
        "falls back to next. Circuit-breaker health states with exponential backoff. "
        "OpenRouter providers get: context compression plugin + routing metadata trace + "
        "response caching. You CAN use verify_multi tool to compare answers across models."
    ),
    profile="full",
)
def _init_provider_failover(worker: Any, cfg: dict):
    """ModelFailover is already initialized in unified_worker.__init__.
    This registration exists solely for the prompt description.
    """
    return getattr(worker, 'llm', None)


@register(
    name="verify_broadcast",
    attr_name="_verify_broadcast",
    config_key="verify_broadcast",
    default_enabled=True,
    description=(
        "Multi-model verification: the verify_multi tool sends the same prompt to ALL "
        "available models (MiMo, GPT-OSS, Kimi, DeepSeek), compares their answers, "
        "and produces a consensus audit with divergence score. Use BEFORE high-stakes "
        "actions (deployments, file writes, system changes) to catch model-specific "
        "hallucinations. If divergence > 0.3, the action is flagged as risky."
    ),
    profile="full",
)
def _init_verify_broadcast(worker: Any, cfg: dict):
    """Verification broadcast capability - used by verify_multi tool.
    Returns a lightweight wrapper so the tool_executor can access ModelFailover.
    """
    try:
        from tical_code.core.verification_broadcast import execute_verify_multi
        # Return a marker object so the system knows verify_broadcast is active
        class VerifyBroadcast:
            def __init__(self, worker_ref):
                self.worker = worker_ref
            def verify(self, prompt: str, threshold: float = 0.3) -> dict:
                from tical_code.core.verification_broadcast import execute_verify_multi
                return execute_verify_multi(
                    failover=getattr(worker, 'llm', None),
                    prompt=prompt,
                    threshold=threshold,
                )
        return VerifyBroadcast(worker)
    except Exception:
        return True


# =============================================================================
# Off-by-default modules (full profile)
# =============================================================================

@register(
    name="usage_tracker",
    attr_name="usage",
    config_key="usage_tracker",
    default_enabled=True,
    description=(
        "Cost tracking: logs API token usage and cost per provider/model. "
        "You CAN query usage stats to understand spending patterns. "
        "Stored in SQLite at workspace/usage.db."
    ),
    profile="full",
)
def _init_usage_tracker(worker: Any, cfg: dict):
    from tical_code.core.usage import UsageTracker
    w = cfg.get("workspace", ".")
    return UsageTracker(db_path=str(Path(w) / "usage.db"))


@register(
    name="cron_scheduler",
    attr_name="_cron",
    config_key="cron_scheduler",
    default_enabled=True,
    description=(
        "Scheduled task system: runs periodic maintenance jobs (health checks, "
        "log cleanup, self-audit). You CAN use cron_add, cron_list, cron_remove "
        "tools to manage scheduled tasks. Responds to: schedule, cron, periodic task."
    ),
    profile="light",
)
def _init_cron_scheduler(worker: Any, cfg: dict):
    import asyncio
    from tical_code.core.cron import CronManager, CronJob, CronSchedule
    w = cfg.get("workspace", ".")
    db_path = str(Path(w) / "cron.db")
    manager = CronManager(framework=worker, db_path=db_path)

    # Default job 1: Health check - every 15 minutes
    health_job = CronJob(
        job_id="cron_health_check",
        name="Health check",
        description="Check all services active, disk<85%, memory<90%",
        schedule=CronSchedule.EVERY_15_MINUTES,
        task_type="shell",
        task_params={
            "cmd": (
                "echo '=== Health Check ===' && "
                "systemctl is-active unified-worker 2>/dev/null || echo 'unknown' && "
                "df -h / | awk 'NR==2{print $5}' && "
                "free -m | awk 'NR==2{printf \"%.0f%%\", $3/$2*100}'"
            ),
        },
        created_by="system",
    )
    asyncio.run(manager.add_job(health_job))

    # Default job 2: Log cleanup - every 6 hours
    log_cleanup_job = CronJob(
        job_id="cron_log_cleanup",
        name="Log cleanup",
        description="Truncate log files larger than 50MB",
        schedule=CronSchedule.EVERY_6_HOURS,
        task_type="shell",
        task_params={
            "cmd": (
                "find ~/.tical-code/logs/ -name '*.log' -size +50M "
                "-exec truncate -s 0 {} + 2>/dev/null; "
                "echo 'Log cleanup complete'"
            ),
        },
        created_by="system",
    )
    asyncio.run(manager.add_job(log_cleanup_job))

    # Default job 3: Self-audit - every 60 minutes
    self_audit_job = CronJob(
        job_id="cron_self_audit",
        name="Self-audit",
        description="Check for ERROR patterns in journalctl",
        schedule=CronSchedule.HOURLY,
        task_type="shell",
        task_params={
            "cmd": (
                "journalctl -u unified-worker --since '1 hour ago' 2>/dev/null | "
                "grep -ci 'ERROR\\|CRITICAL\\|FATAL' || echo 0"
            ),
        },
        created_by="system",
    )
    asyncio.run(manager.add_job(self_audit_job))

    logger.info("CronManager initialized with 3 default health jobs")
    return manager


@register(
    name="vigil",
    attr_name="_vigil",
    config_key="vigil",
    default_enabled=True,
    description=(
        "Security patrol: runs periodic checks for anomalies (unexpected file changes, "
        "unauthorized access patterns, configuration drift). Reports findings. "
        "Patrol interval: 5 minutes."
    ),
    profile="light",
)
def _init_vigil(worker: Any, cfg: dict):
    try:
        from tical_code.vigil import build_vigil
    except ImportError:
        return None
    if build_vigil is None:
        return None
    worker._vigil_patrol_interval = 300
    worker._last_patrol = 0
    # Also set on ctx so worker main loop reads correct object
    if hasattr(worker, '_ctx'):
        worker._ctx._last_patrol = 0
        worker._ctx._vigil_patrol_interval = worker._vigil_patrol_interval
    return build_vigil()


@register(
    name="sandbox",
    config_key="sandbox",
    default_enabled=True,
    description=(
        "Isolated execution: runs code in a subprocess sandbox with restricted "
        "filesystem and network access. You CAN use this to safely test code "
        "before applying changes to the real system."
    ),
    profile="full",
)
def _init_sandbox(worker: Any, cfg: dict):
    try:
        from tical_code.core.sandbox import SandboxExecutor
    except ImportError:
        return None
    return SandboxExecutor()



@register(
    name="reflection",
    config_key="reflection",
    default_enabled=False,  # ARCHIVED: no call sites (v0.8.5+). To revive, set reflection=true in modules config and wire call sites in task_handler.py / message_handler.py.
    description=(
        "[ARCHIVED] Self-improvement: after completing a task, analyzes what went well/poorly "
        "and suggests improvements for next time. Was default_enabled=False since v0.8.5 with "
        "no active call sites in unified_worker pipeline. Preserved for reference."
    ),
    profile="full",
)
def _init_reflection(worker: Any, cfg: dict):
    """Archived: reflection is not wired in the active code path."""
    return None



@register(
    name="subagent_manager",
    config_key="subagent_manager",
    default_enabled=True,
    description=(
        "Sub-agent delegation: offload independent subtasks to background "
        "sub-agents that run in parallel with their own session and tools. "
        "You CAN use delegate_task to start a sub-agent, get_subagent_result "
        "to retrieve its output, and list_subagent_tasks to monitor progress. "
        "Great for parallel research, fact-checking, or independent code analysis."
    ),
    profile="full",
)
def _init_subagent_manager(worker: Any, cfg: dict):
    """Initialize SubAgentManager and wire it into tool_executor dispatch."""
    try:
        from tical_code.core.subagent import SubAgentManager
        from tical_code.core.tool_executor import set_subagent_manager
    except ImportError:
        return None
    manager = SubAgentManager(worker)
    set_subagent_manager(manager)
    return manager


@register(
    name="plugin_host",
    config_key="plugin_host",
    default_enabled=True,
    description=(
        "Plugin system host: discovers and activates plugins (browser automation, "
        "web search, cloud device, messenger, trading, vision, X/Twitter integration). "
        "Plugins contribute additional tools that extend agent capabilities beyond "
        "the built-in toolset. You CAN use plugin tools like web_search, browser_navigate, "
        "browser_screenshot, and more when this module is active."
    ),
    profile="full",
)
def _init_plugin_host(worker: Any, cfg: dict):
    """Discover and wire all compatible plugins into the tool dispatch."""
    try:
        from tical_code.plugins import PluginManager
        from tical_code.core.tool_executor import register_plugin_tool
    except ImportError:
        return None

    mgr = PluginManager()
    plugin_classes = mgr.discover_plugins("tical_code.plugins")
    if not plugin_classes:
        logger.warning("plugin_host: no plugins discovered")
        return mgr

    wired = 0
    for cls in plugin_classes:
        try:
            inst = cls()
            if not inst.is_available():
                continue
            # Register the plugin instance
            if not mgr.register_plugin(inst):
                continue
            # Wire each plugin tool into the executor dispatch.
            # Plugin tools are async and take (args: dict) - we wrap
            # them in a sync bridge that runs the coroutine.
            import asyncio as _asyncio
            for tool_name, tool_handler in inst.get_tools().items():
                full_name = f"{inst.metadata.name}_{tool_name}"
                # Sync wrapper for async plugin tools (default arg captures current handler)
                # Uses a persistent event loop to avoid asyncio.run() create-destroy cycle
                # that breaks aiohttp sessions and other persistent async resources.
                _plugin_loop = None
                def _make_sync(handler):
                    def _wrapper(args: dict, _h=handler):
                        nonlocal _plugin_loop
                        if _plugin_loop is None or _plugin_loop.is_closed():
                            _plugin_loop = _asyncio.new_event_loop()
                            _asyncio.set_event_loop(_plugin_loop)
                        task = _plugin_loop.create_task(_h(args))
                        return _plugin_loop.run_until_complete(task)
                    return _wrapper
                register_plugin_tool(full_name, _make_sync(tool_handler))
                logger.info("plugin_host: wired %s", full_name)
            wired += 1
        except Exception as e:
            logger.warning("plugin_host: failed to load plugin %s: %s",
                           getattr(cls, 'metadata', cls.__name__), e)

    logger.info("plugin_host: %d plugins activated, total tools: %d",
                wired, len(mgr.get_all_tools()))
    return mgr


# =============================================================================
# Capability Integration Layer (full profile)
# =============================================================================

@register(
    name="capability_integrator",
    attr_name="_cap_integrator",
    config_key="capability_integrator",
    default_enabled=True,
    description=(
        "Capability discovery: auto-discovers all system modules and exposes "
        "their capabilities through a unified interface. Use capability_list "
        "to see what capabilities are available, and capability_call to invoke them. "
        "Provides access to workflow engine, hive coordination, memory evolution, "
        "identity anchoring, state persistence, and feature detection."
    ),
    dependencies=["hive", "identity"],
    profile="full",
)
def _init_capability_integrator(worker: Any, cfg: dict):
    """Initialize the CapabilityIntegrator and wire it into tool_executor."""
    try:
        from tical_code.core.capability_integrator import (
            CapabilityIntegrator,
            set_integrator,
        )
    except ImportError:
        return None

    integrator = CapabilityIntegrator()
    integrator.discover()

    # Wire into tool_executor so capability_list / capability_call tools work
    try:
        set_integrator(integrator)
        from tical_code.core.tool_executor import set_capability_integrator
        set_capability_integrator(integrator)
    except Exception:
        pass

    logger.info("capability_integrator: %d capabilities discovered across %d modules",
                len(integrator.list_capabilities()),
                len(set(c["module"] for c in integrator.list_capabilities())))
    return integrator


# =============================================================================
# DESIGNED-NOT-DEAD → Active: Workflow Engine (full profile)
# =============================================================================

@register(
    name="workflow",
    config_key="workflow",
    default_enabled=True,
    description=(
        "Workflow engine: DAG-based task orchestration with LLM nodes, "
        "Condition nodes (branching), HTTP nodes, Code nodes (sandboxed Python), "
        "and Parallel nodes (fan-out/fan-in). Use workflow_create to define a graph "
        "and workflow_execute to run it. Great for multi-step automation that "
        "requires conditional branching or parallel execution."
    ),
    dependencies=[],
    profile="full",
)
def _init_workflow(worker: Any, cfg: dict):
    """Workflow is lazy-initialized via capability_integrator at call time.
    Returns a marker so the module registry considers it active.
    """
    return "lazy"


# =============================================================================
# DESIGNED-NOT-DEAD → Active: Identity Registry (light profile)
# =============================================================================

@register(
    name="identity",
    config_key="identity",
    default_enabled=True,
    description=(
        "Identity registry: hardware fingerprint + deployment identity. "
        "You CAN use identity_info to confirm who you are, which VPS you run on, "
        "and what edition (full/light) is deployed. Prevents identity confusion."
    ),
    profile="light",
)
def _init_identity(worker: Any, cfg: dict):
    """Initialize IdentityRegistry."""
    try:
        from tical_code.core.identity import IdentityRegistry
    except ImportError:
        return None
    return IdentityRegistry()


# =============================================================================
# DESIGNED-NOT-DEAD → Active: Hive Coordination (full profile)
# =============================================================================

@register(
    name="hive",
    config_key="hive",
    default_enabled=True,
    description=(
        "Hive coordination: multi-worker capability sharing and collective "
        "wisdom. Workers can share learned patterns as CapabilityCapsules. "
        "Use hive_capsule_share to broadcast a successful strategy to other "
        "workers in the mesh. Categories: execution_discipline, memory, design, "
        "quality, mvp, planning, cross_domain."
    ),
    profile="full",
)
def _init_hive(worker: Any, cfg: dict):
    """Initialize Hive capability sharing.

    Returns a lightweight marker that the capability integrator uses
    to discover hive capabilities. Full init is lazy via the integrator.
    """
    try:
        from tical_code.core.hive import SoulAgentHiveClient
        logger.info("hive: SoulAgentHiveClient available")
    except ImportError:
        logger.debug("hive: not available (SoulAgent not installed)")
        return None
    return "available"


# =============================================================================
# DESIGNED-NOT-DEAD → Active: Memory Evolution (full profile)
# =============================================================================

@register(
    name="memory_evolve",
    config_key="memory_evolve",
    default_enabled=True,
    description=(
        "Memory evolution: AI-managed memory updates. The AI can autonomously "
        "revise its memory files based on experience. Use memory_evolve tool to "
        "update MEMORY.md or USER.md with learned facts. Frozen files (SOUL.md, "
        "SECRET.md) are protected from modification."
    ),
    dependencies=["memory_store"],
    profile="full",
)
def _init_memory_evolve(worker: Any, cfg: dict):
    """Initialize MemoryEvolver."""
    try:
        from tical_code.core.memory_evolve import MemoryEvolver
    except ImportError:
        return None
    w = cfg.get("workspace", ".")
    evolver = MemoryEvolver(memory_dir=w)
    logger.info("memory_evolve: initialized at %s", w)
    return evolver


# =============================================================================
# Sustained Task Queue — persistent recoverable tasks
# =============================================================================

@register(
    name="sustained_task",
    attr_name="sustained_task",
    config_key="sustained_task",
    default_enabled=True,
    description=(
        "Persistent task queue: survives restarts, tracks multi-step tasks "
        "with progress, timeouts, and automatic recovery. "
        "You CAN use task_create, task_list, task_update tools."
    ),
    profile="light",
)
def _init_sustained_task(worker: Any, cfg: dict):
    """Initialize SustainedTaskManager for persistent task queue."""
    try:
        from tical_code.core.modules.sustained_task import SustainedTaskManager
    except ImportError:
        return None
    db_path = cfg.get("sustained_task_db", "~/.tical-code/sustained_tasks.db")
    mgr = SustainedTaskManager(db_path=db_path)
    logger.info("sustained_task: initialized at %s", db_path)
    return mgr


# =============================================================================
# Self-Evolution Engine — error pattern tracking and usage insights
# =============================================================================

@register(
    name="self_evolve",
    attr_name="self_evolve",
    config_key="self_evolve",
    default_enabled=True,
    description=(
        "Self-evolution engine: tracks error patterns, usage insights, and "
        "code improvement suggestions. Learns from failures to prevent repeats."
    ),
    profile="light",
)
def _init_self_evolve(worker: Any, cfg: dict):
    """Initialize SelfEvolveEngine for error pattern tracking."""
    try:
        from tical_code.core.modules.self_evolve import SelfEvolveEngine
    except ImportError:
        return None
    db_path = cfg.get("self_evolve_db", os.path.expanduser("~/.tical-code/self_evolve.db"))
    engine = SelfEvolveEngine(db_path=db_path)
    logger.info("self_evolve: initialized")
    return engine


# =============================================================================
# DESIGNED-NOT-DEAD → Active: MCP Client (light profile)
# =============================================================================

@register(
    name="mcp_client",
    config_key="mcp_client",
    default_enabled=True,
    description=(
        "MCP (Model Context Protocol) client: connects to external MCP servers "
        "(stdio or HTTP) and exposes their tools as mcp_<server>_<tool> tools. "
        "You CAN call any tool discovered from connected MCP servers. "
        "Use when: you need filesystem access, web search, database queries, "
        "or other capabilities exposed via MCP protocol."
    ),
    dependencies=["tool_registry", "provider_registry"],
    profile="light",
)
def _init_mcp_client(worker: Any, cfg: dict) -> Any:
    """Initialize MCPClient and connect to configured MCP servers."""
    import asyncio

    try:
        from tical_code.core.mcp_client import MCPClient, MCPConfig
    except ImportError:
        logger.warning("mcp_client: module not available (missing dependencies)")
        return None

    import json as _json

    client = MCPClient()
    servers_path = cfg.get("modules", {}).get("mcp_client", {}).get(
        "servers_path", "config/mcp_servers.json"
    )
    abs_path = str(Path(cfg.get("workspace", ".")) / servers_path)
    try:
        with open(abs_path, encoding="utf-8") as fh:
            servers_data = _json.load(fh)
        client.load_servers(servers_data)
    except Exception as exc:
        logger.warning("mcp_client: failed to load servers from %s: %s", abs_path, exc)

    # Connect to all configured servers (synchronous bridge for async connect)
    connected = 0
    for name in list(client._configs.keys()):
        try:
            result = asyncio.run(client.connect(name))
            if result.get("success"):
                connected += 1
        except Exception as exc:
            logger.warning("mcp_client: failed to connect to %s: %s", name, exc)

    if connected > 0:
        # Discover tools
        try:
            discovery = asyncio.run(client.discover_tools())
            if discovery.get("success"):
                logger.info(
                    "mcp_client: %d servers connected, discovering tools...",
                    connected,
                )
        except Exception as exc:
            logger.warning("mcp_client: tool discovery failed: %s", exc)

        # Wire MCP tools into the tool_executor dispatch
        try:
            from tical_code.mcp import register_mcp_tools
            from tical_code.core.tool_registry import ToolRegistry

            registry = ToolRegistry()
            count = register_mcp_tools(client, tool_registry=registry)
            logger.info("mcp_client: registered %d tools into dispatch", count)
        except Exception as exc:
            logger.warning("mcp_client: failed to register tools: %s", exc)

    # Store client reference on worker for later discovery
    worker._mcp_client = client
    # Store a registry reference so prompt.py can query tool count
    try:
        from tical_code.core.tool_registry import ToolRegistry
        worker._mcp_registry = ToolRegistry()
    except Exception:
        pass

    return client


# =============================================================================
# DESIGNED-NOT-DEAD → Active: Feature Detection (light profile)
# =============================================================================

@register(
    name="detection",
    config_key="detection",
    default_enabled=True,
    description=(
        "Feature detection: auto-detect system edition (full/light) based on "
        "RAM, CPU cores, and available dependencies. Use detect_edition to check "
        "what features are available on this deployment."
    ),
    profile="light",
)
def _init_detection(worker: Any, cfg: dict):
    """Initialize feature detection. Module-level functions, returns marker."""
    try:
        from tical_code.core.detection import detect_edition
        edition = detect_edition()
        logger.info("detection: edition=%s", edition)
        return {"edition": edition}
    except ImportError:
        return None


# =============================================================================
# Anchor system (full profile)
# =============================================================================

@register(
    name="anchor",
    attr_name="anchor_manager",
    config_key="anchor",
    default_enabled=True,
    description=(
        "Identity anchoring: maintains stable reference points (identity, purpose, values, "
        "capabilities, limitations, context, relationships) that ground agent reasoning. "
        "You CAN use anchor context to recall who you are, what you can do, and how you "
        "relate to other agents. Compatible with SoulAgent seven-anchor system."
    ),
    profile="full",
)
def _init_anchor(worker: Any, cfg: dict):
    """Initialize the AnchorManager for identity anchoring."""
    try:
        from tical_code.core.anchor import AnchorManager
    except ImportError:
        return None
    w = cfg.get("workspace", ".")
    anchor_file = str(Path(w) / ".tical" / "anchors.json")
    return AnchorManager(anchor_file=anchor_file)


# =============================================================================
# Axiom engine (light profile)
# =============================================================================

# =============================================================================
# Performance Metrics Collector (light profile)
# =============================================================================

@register(
    name="metrics_collector",
    attr_name="_metrics",
    config_key="metrics",
    default_enabled=True,
    description=(
        "Performance metrics: records tool execution latency, LLM call latency, "
        "and error counts per tool. Uses a rolling window. "
        "You CAN use check_metrics to see average latencies, slowest calls, "
        "and error breakdowns. Useful for diagnosing slowdowns or tool failures."
    ),
    profile="light",
)
def _init_metrics(worker: Any, cfg: dict):
    """Initialize the MetricsCollector and wire it into tool_executor."""
    try:
        from tical_code.core.metrics import MetricsCollector
    except ImportError:
        return None
    collector = MetricsCollector(window_size=cfg.get("metrics_window", 200))
    # Wire into tool_executor globals for tool-level recording
    try:
        from tical_code.core.tool_executor import set_metrics_collector
        set_metrics_collector(collector)
        logger.info("MetricsCollector wired into tool_executor")
    except Exception as e:
        logger.warning("MetricsCollector not wired into tool_executor: %s", e)
    return collector


# =============================================================================
# Axiom engine (light profile)
# =============================================================================

@register(
    name="axioms",
    attr_name="axioms",
    config_key="axioms",
    default_enabled=True,
    description=(
        "Physical axioms: 6 built-in physics-based reasoning lenses (gravitation, "
        "entropy, least-action, symmetry-breaking, information-conservation, causality) "
        "that illuminate cognition without driving decisions. "
        "You CAN use axiom annotations to reflect on problems from a physics perspective."
    ),
    profile="light",
)
def _init_axioms(worker: Any, cfg: dict):
    """Initialize the AxiomEngine for physics-based cognition lenses."""
    try:
        from tical_code.core.axioms import AxiomEngine
    except ImportError:
        return None
    return AxiomEngine(enabled=True)


# =============================================================================
# System Coherence Monitor (light profile)
# =============================================================================

@register(
    name="coherence_monitor",
    attr_name="_coherence",
    config_key="coherence",
    default_enabled=True,
    description=(
        "System coherence monitor: reads live subsystem state (CognitiveMetabolism, "
        "MemoryEvolver, MetricsCollector, SelfRepairEngine) and produces a unified "
        "health/coherence report. "
        "You CAN use this to check system health, see tool diversity metrics, "
        "memory decay status, repair effectiveness, and active signal metabolism. "
        "Query it when diagnosing slowdowns, checking if memory is fragmented, "
        "or verifying the system is maintaining itself properly. "
        "Usage: accessible via status command or direct Python call."
    ),
    profile="light",
)
def _init_coherence(worker: Any, cfg: dict):
    """Initialize the CoherenceMonitor and auto-wire from worker state."""
    try:
        from tical_code.coherence import CoherenceMonitor
    except ImportError:
        logger.warning("coherence_monitor: module not available (tical_code.coherence not found)")
        return None

    try:
        cm = CoherenceMonitor()
        wired = cm.wire_all_from_worker(worker)
        if wired:
            logger.info("coherence_monitor: wired from worker: %s", wired)
        else:
            logger.info("coherence_monitor: no worker subsystems to wire (standby mode)")
        return cm
    except Exception as exc:
        logger.warning("coherence_monitor: init failed: %s", exc)
        return None


# =============================================================================
# EITE Constitutional Kernel (full profile)
# =============================================================================

@register(
    name="eite_kernel",
    attr_name="_eite",
    config_key="eite_kernel",
    default_enabled=False,          # OFF by default — enable via modules.eite_kernel=true
    description=(
        "EITE Constitutional Identity Kernel: 5 immutable axioms (data sovereignty, "
        "identity continuity, cognitive irreversibility, veracity, anti-circular) "
        "projection-guard the identity anchor against unauthorized drift. "
        "Non-blocking: on dissonance, injects reflection prompt into system context "
        "rather than blocking execution. Degrades gracefully if numpy unavailable."
    ),
    dependencies=["anchor", "axioms"],   # semantic dependencies, not strict
    profile="full",
)
def _init_eite_kernel(worker: Any, cfg: dict):
    """Initialize the EITE Constitutional Kernel."""
    try:
        from tical_code.core.eite_kernel import build_eite_kernel
    except ImportError:
        logger.warning("eite_kernel: eite_kernel.py not found, skipping")
        return None

    w = cfg.get("workspace", ".")
    dim = cfg.get("modules", {}).get("eite_dim", 64) if isinstance(cfg, dict) else 64
    try:
        kernel = build_eite_kernel(workspace=w, dim=dim)
        if not kernel.initialize():
            logger.warning("eite_kernel: init returned False, degraded mode")
            return None
        logger.info("eite_kernel: initialized (dim=%d, axioms=%d)", dim, len(kernel._immutable_basis))
        return kernel
    except Exception as exc:
        logger.warning("eite_kernel: init failed → degraded: %s", exc)
        return None


# =============================================================================
# Security Vigil — autonomous intrusion detection (light profile)
# =============================================================================

@register(
    name="security_vigil",
    config_key="vigil",
    default_enabled=True,
    description=(
        "Security Vigil — autonomous intrusion detection. "
        "Monitors: phishing URLs, C2 IPs, new public ports, "
        "unknown SSH connections, suspicious /tmp files, "
        "and module integrity. Alerts on anomalies. "
        "Zero token cost when system is clean. "
        "Use `mark_ip <ip> bad` to blocklist an attacker IP."
    ),
    profile="light",
)
def _init_vigil_def(worker: Any, cfg: dict):
    from tical_code.core.vigil import _init_vigil
    return _init_vigil(worker, cfg)
