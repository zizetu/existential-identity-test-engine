# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Licensed under AGPLv3
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#
# provenance:ticalasi-zzt-2026

"""
Worker Framework for eite-agent.

Provides a full async lifecycle compatible with the P0 verification suite
and external scripts, while remaining compatible with unified_worker.Worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("EITElite.worker_framework")


class WorkerStatus(Enum):
    """Worker status states for lifecycle tracking."""
    INIT = "init"
    BOOTSTRAPPING = "bootstrapping"
    RUNNING = "running"
    MAINTENANCE = "maintenance"
    SHUTDOWN = "shutdown"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass
class WorkerConfig:
    """Configuration for a worker instance."""
    name: str = "worker"
    host: str = "localhost"
    profile: str = "full"
    repo: str = "eite-agent"
    bot: str = ""
    providers: List[str] = field(default_factory=lambda: ["deepseek"])
    preferred_provider: str = "deepseek"
    disabled_providers: List[str] = field(default_factory=list)
    edition: str = "community"
    model: str = "deepseek-v4-flash"
    deploy_id: str = ""
    anchor_path: str = "anchor.json"
    ai_backend: str = "openai"
    api_key_env: str = "OPENAI_API_KEY"
    api_endpoint: str = ""
    heartbeat_interval: int = 60
    maintenance_interval: int = 300
    log_level: str = "INFO"
    workspace: str = ""

    @classmethod
    def from_file(cls, path: str) -> "WorkerConfig":
        """Load config from a JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        if "name" not in kwargs:
            kwargs["name"] = data.get("name", "worker")
        return cls(**kwargs)

    @classmethod
    def from_anchor(cls, anchor_url: str, deploy_id: str) -> "WorkerConfig":
        """Load config from anchor server (for cloud deployments)."""
        import urllib.request
        url = f"{anchor_url}/api/deploy/{deploy_id}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            kwargs = {k: v for k, v in data.items() if k in known}
            kwargs.setdefault("name", data.get("name", deploy_id))
            kwargs.setdefault("deploy_id", deploy_id)
            return cls(**kwargs)
        except Exception as e:
            logger.warning("Anchor config fetch failed: %s, using defaults", e)
            return cls(name=deploy_id, deploy_id=deploy_id)

    def expand_paths(self) -> None:
        """Expand user paths in config fields (no-op if empty)."""
        if self.anchor_path:
            self.anchor_path = os.path.expanduser(self.anchor_path)
        if self.workspace:
            self.workspace = os.path.expanduser(self.workspace)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Worker constructor."""
        return {
            "name": self.name,
            "host": self.host,
            "profile": self.profile,
            "repo": self.repo,
            "bot": self.bot,
            "providers": self.providers,
            "preferred_provider": self.preferred_provider,
            "disabled_providers": self.disabled_providers,
            "edition": self.edition,
            "model": self.model,
            "ai_model": self.model,
            "ai_backend": self.ai_backend,
            "ai_endpoint": self.api_endpoint,
            "workspace": self.workspace or os.getcwd(),
        }


class _SimpleWorkerLoop:
    """Minimal worker loop used when full worker_loop module is unavailable.

    Provides ping fast-path and OpenAI-compatible LLM calls for smoke tests.
    """

    def __init__(self, framework: "WorkerFramework"):
        self.framework = framework
        self._llm = None

    def _ensure_llm(self):
        if self._llm is not None:
            return self._llm
        cfg = self.framework.config
        api_key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY", "") or os.environ.get(
            "OPENAI_API_KEY", ""
        )
        if not api_key:
            # Prefer MiMo token-plan key when available
            api_key = (
                os.environ.get("ANTHROPIC_API_KEY", "")
                or os.environ.get("MIMO_API_KEY_1", "")
                or os.environ.get("MIMO_API_KEY", "")
            )
        base_url = (
            cfg.api_endpoint
            or os.environ.get("OPENAI_BASE_URL", "")
            or os.environ.get("MIMO_ENDPOINT_1", "")
            or "https://api.x.ai/v1"
        )
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        model = cfg.model or os.environ.get("TICAL_TEST_MODEL", "grok-3-mini")
        if not api_key:
            return None
        try:
            from tical_code.core.llm_backend import OpenAIBackend
            self._llm = OpenAIBackend(api_key=api_key, base_url=base_url, model=model)
            return self._llm
        except Exception as e:
            logger.warning("LLM backend init failed: %s", e)
            return None

    async def handle_message(self, message) -> str:
        """Handle a UserMessage-like object or plain string content."""
        if hasattr(message, "content"):
            content = message.content or ""
            user_id = getattr(message, "user_id", "user")
        else:
            content = str(message)
            user_id = "user"
        low = content.strip().lower()
        name = self.framework.config.name
        # Fast path: ping
        if low in ("ping", "[cmd] ping", "cmd ping"):
            return f"pong from {name}"
        if low.startswith("[cmd] ping") or low == "ping":
            return f"pong from {name}@{socket.gethostname()}"

        llm = self._ensure_llm()
        if llm is None:
            return "LLM not configured"

        messages = [
            {
                "role": "system",
                "content": (
                    f"You are {name}, an eite-agent worker. "
                    "Answer briefly and accurately."
                ),
            },
            {"role": "user", "content": content},
        ]
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: llm.call(messages, max_tokens=256)
            )
        except Exception as e:
            return f"Error processing message: {e}"
        if not isinstance(result, dict):
            return str(result)
        if result.get("error"):
            return f"error: {result.get('error')}"
        return (result.get("content") or "").strip() or "(empty response)"


class WorkerFramework:
    """Async worker lifecycle manager for eite-agent.

    Implements bootstrap → run_loop → handle_message → shutdown for
    verification tests and lightweight deployments.
    """

    def __init__(self, config: WorkerConfig):
        if hasattr(config, "expand_paths"):
            config.expand_paths()
        self.config = config
        self.status = WorkerStatus.INIT
        self.start_time = time.time()
        self.loop_count = 0
        self.last_error: Optional[str] = None
        self._shutdown = asyncio.Event()
        self.worker_loop: Optional[_SimpleWorkerLoop] = None
        self._main_loop_task: Optional[asyncio.Task] = None
        # Keep a dict-style cfg for modules that expect worker.cfg
        self.cfg = config.to_dict() if hasattr(config, "to_dict") else {}
        self.name = config.name
        self.workspace = config.workspace or os.getcwd()
        self.logger = logging.getLogger(f"EITElite.worker.{config.name}")

    async def bootstrap(self) -> None:
        """Bootstrap worker: init loop, mark RUNNING."""
        logger.info("[WorkerFramework] Bootstrap starting for %s", self.config.name)
        self.status = WorkerStatus.BOOTSTRAPPING
        try:
            self.worker_loop = _SimpleWorkerLoop(self)
            self.status = WorkerStatus.RUNNING
            self._shutdown.clear()
            logger.info(
                "[WorkerFramework] Bootstrap complete: status=%s worker_loop=%s",
                self.status,
                self.worker_loop is not None,
            )
        except Exception as e:
            self.status = WorkerStatus.ERROR
            self.last_error = str(e)
            logger.error("[WorkerFramework] Bootstrap failed: %s", e)
            raise

    async def run_loop(self) -> None:
        """Main loop — heartbeat tick until shutdown."""
        logger.info("[WorkerFramework] Main loop started")
        while not self._shutdown.is_set():
            try:
                self.loop_count += 1
                interval = max(1, int(getattr(self.config, "heartbeat_interval", 60) or 60))
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=min(interval, 2))
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                logger.info("[WorkerFramework] Loop cancelled")
                break
            except Exception as e:
                self.last_error = str(e)
                logger.error("[WorkerFramework] Loop error: %s", e)
                await asyncio.sleep(1)
        logger.info("[WorkerFramework] Main loop ended")

    async def handle_message(self, user_id: str, content: str) -> str:
        """Handle a user message (ping fast-path or LLM)."""
        if not self.worker_loop:
            logger.warning("[WorkerFramework] WorkerLoop not initialized")
            return "Worker not ready. Please wait for initialization."
        try:
            # Duck-type UserMessage
            class _Msg:
                pass
            msg = _Msg()
            msg.user_id = user_id
            msg.content = content
            return await self.worker_loop.handle_message(msg)
        except Exception as e:
            logger.error("[WorkerFramework] handle_message failed: %s", e)
            return f"Error processing message: {e}"

    async def shutdown(self, skip_death_record: bool = False) -> None:
        """Standard shutdown sequence."""
        logger.info("[WorkerFramework] Shutdown initiated (skip_death_record=%s)", skip_death_record)
        self.status = WorkerStatus.SHUTDOWN
        self._shutdown.set()
        if self._main_loop_task and not self._main_loop_task.done():
            self._main_loop_task.cancel()
            try:
                await self._main_loop_task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = WorkerStatus.OFFLINE
        logger.info("[WorkerFramework] Shutdown complete")


@dataclass
class WorkerState:
    """Worker state snapshot."""
    name: str = ""
    status: str = "unknown"
    uptime: float = 0.0
    tasks_completed: int = 0


def _init_tool_system(framework: Any) -> int:
    """Initialize tool system on the framework."""
    if hasattr(framework, "_init_tools"):
        try:
            return framework._init_tools()
        except Exception as e:
            logger.warning("Tool system init failed: %s", e)
    return 0


__all__ = [
    "WorkerFramework",
    "WorkerConfig",
    "WorkerStatus",
    "WorkerState",
    "_init_tool_system",
]
