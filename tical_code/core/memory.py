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

"""
Memory Module
=============

Minimal memory store — provides enough for CLI introspection.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryType(Enum):
    """Type of memory entry."""
    FACT = "fact"
    PREFERENCE = "preference"
    HISTORY = "history"
    CONTEXT = "context"
    SEMANTIC = "semantic"


@dataclass
class MemoryEntry:
    """A single memory entry."""
    memory_type: MemoryType
    key: str
    value: Any
    access_count: int = 0
    created_at: float = field(default_factory=time.time)

    def get_age(self) -> float:
        return time.time() - self.created_at


class MemoryStore:
    """Simple in-memory store."""

    def __init__(self):
        self._entries: Dict[str, MemoryEntry] = {}
        self._access_count = 0

    @property
    def entries(self) -> Dict[str, MemoryEntry]:
        return self._entries

    def get_by_type(self, memory_type: MemoryType) -> List[MemoryEntry]:
        return [e for e in self._entries.values() if e.memory_type == memory_type]

    def get_stats(self) -> dict:
        return {
            "total_entries": len(self._entries),
            "total_accesses": self._access_count,
        }

    def set(self, key: str, value: Any, memory_type: MemoryType = MemoryType.FACT):
        entry = MemoryEntry(memory_type=memory_type, key=key, value=value)
        self._entries[key] = entry

    def get(self, key: str) -> Optional[Any]:
        entry = self._entries.get(key)
        if entry:
            entry.access_count += 1
            self._access_count += 1
            return entry.value
        return None


_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """Get or create the singleton memory store."""
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
