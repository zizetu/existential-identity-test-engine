# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Licensed under AGPLv3
# Original repository: https://github.com/zizetu/eite-agent
#
# provenance:ticalasi-zzt-2026​
"""
Worker Framework compatibility shim for eite-agent.
eite-agent uses unified_worker.py as its primary worker implementation.
This module re-exports key types so that scripts can import from
worker_framework without depending on the full tical-agent codebase.
"""
import logging
from typing import Any

from .unified_worker import UnifiedWorker as WorkerFramework
from .unified_worker import WorkerConfig, WorkerState

logger = logging.getLogger("tical-code.worker_framework")


def _init_tool_system(framework: Any) -> int:
    """Initialize tool system on the framework."""
    if hasattr(framework, "_init_tools"):
        try:
            return framework._init_tools()
        except Exception as e:
            logger.warning("Tool system init failed: %s", e)
    return 0


__all__ = ["WorkerFramework", "WorkerConfig", "WorkerState", "_init_tool_system"]
