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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
Worker Pool Module
==================

Minimal worker management — provides basic CLI introspection.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class WorkerStatus(Enum):
    """Worker node status."""
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class WorkerInfo:
    """Information about a worker node."""
    name: str
    host: str = "localhost"
    port: int = 22
    user: str = "root"
    identity_file: Optional[str] = None
    status: WorkerStatus = WorkerStatus.UNKNOWN


class WorkerPool:
    """Simple in-memory worker pool."""

    def __init__(self):
        self._workers: Dict[str, WorkerInfo] = {}

    def list_workers(self) -> list:
        return list(self._workers.values())

    def add_worker(self, worker: WorkerInfo):
        self._workers[worker.name] = worker

    def remove_worker(self, name: str):
        self._workers.pop(name, None)

    def execute_on(self, name: str, command: str):
        """Stub — always returns success for CLI ping."""
        from types import SimpleNamespace
        return SimpleNamespace(success=True, elapsed_ms=0.0, error=None)


_pool: Optional[WorkerPool] = None


def get_worker_pool() -> WorkerPool:
    """Get or create the singleton worker pool."""
    global _pool
    if _pool is None:
        _pool = WorkerPool()
    return _pool
