# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Licensed under AGPLv3
# Original repository: https://github.com/zizetu/eite-agent
#
# provenance:ticalasi-zzt-2026

"""
Worker Framework compatibility shim for eite-agent.

eite-agent uses unified_worker.py as its primary worker implementation.
This module re-exports key types so that scripts can import from
worker_framework without depending on the full tical-agent codebase.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .unified_worker import Worker

logger = logging.getLogger("tical-code.worker_framework")


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

    @classmethod
    def from_file(cls, path: str) -> "WorkerConfig":
        """Load config from a JSON file."""
        with open(path, "r") as f:
            data = json.load(f)
        return cls(
            name=data.get("name", "worker"),
            host=data.get("host", "localhost"),
            profile=data.get("profile", "full"),
            repo=data.get("repo", "eite-agent"),
            bot=data.get("bot", ""),
            providers=data.get("providers", ["deepseek"]),
            preferred_provider=data.get("preferred_provider", "deepseek"),
            disabled_providers=data.get("disabled_providers", []),
            edition=data.get("edition", "community"),
            model=data.get("model", "deepseek-v4-flash"),
        )

    @classmethod
    def from_anchor(cls, anchor_url: str, deploy_id: str) -> "WorkerConfig":
        """Load config from anchor server (for cloud deployments)."""
        import urllib.request
        url = f"{anchor_url}/api/deploy/{deploy_id}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return cls(
                name=data.get("name", deploy_id),
                host=data.get("host", "localhost"),
                profile=data.get("profile", "full"),
                repo=data.get("repo", "eite-agent"),
                bot=data.get("bot", ""),
                providers=data.get("providers", ["deepseek"]),
                preferred_provider=data.get("preferred_provider", "deepseek"),
                disabled_providers=data.get("disabled_providers", []),
                edition=data.get("edition", "community"),
                model=data.get("model", "deepseek-v4-flash"),
            )
        except Exception as e:
            logger.warning("Anchor config fetch failed: %s, using defaults", e)
            return cls(name=deploy_id)

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
        }


class WorkerFramework(Worker):
    """Compatibility alias: WorkerFramework = Worker from unified_worker."""

    def __init__(self, config: WorkerConfig):
        super().__init__(config.to_dict())


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


__all__ = ["WorkerFramework", "WorkerConfig", "WorkerState", "_init_tool_system"]
