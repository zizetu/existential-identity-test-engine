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

"""Hive coordination: multi-worker capability sharing and collective wisdom.

Provides SoulAgentHiveClient which enables workers to share learned
patterns as CapabilityCapsules across the mesh. Used by
capability_integrator to discover hive capabilities.

Categories: execution_discipline, memory, design, quality, mvp,
planning, cross_domain.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("EITElite.hive")


class CapabilityCapsule:
    """A shareable capability capsule for hive coordination.

    Attributes:
        category: Capsule category (execution_discipline, memory, etc.)
        name: Capsule name
        description: Human-readable description
        payload: The capability data
        source: Source worker identity
    """

    def __init__(
        self,
        category: str,
        name: str,
        description: str,
        payload: Any,
        source: str = "",
    ):
        self.category = category
        self.name = name
        self.description = description
        self.payload = payload
        self.source = source

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "name": self.name,
            "description": self.description,
            "source": self.source,
        }


class SoulAgentHiveClient:
    """Hive client for capability sharing across workers.

    Provides a lightweight interface for discovering and sharing
    capability capsules. In single-worker deployments this acts as
    a local registry; in multi-worker deployments it would connect
    to a shared hive mesh.
    """

    def __init__(self):
        self._capsules: Dict[str, CapabilityCapsule] = {}
        self._worker_name = os.environ.get("WORKER_NAME", "unknown")
        logger.info(
            "SoulAgentHiveClient: initialized for worker=%s",
            self._worker_name,
        )

    def share_capsule(
        self,
        category: str,
        name: str,
        description: str,
        payload: Any,
    ) -> bool:
        """Share a capability capsule to the hive."""
        capsule = CapabilityCapsule(
            category=category,
            name=name,
            description=description,
            payload=payload,
            source=self._worker_name,
        )
        key = f"{category}:{name}"
        self._capsules[key] = capsule
        logger.debug("Hive: shared capsule %s", key)
        return True

    def discover_capsules(
        self,
        category: Optional[str] = None,
    ) -> List[CapabilityCapsule]:
        """Discover available capsules, optionally filtered by category."""
        if category:
            return [
                c for c in self._capsules.values() if c.category == category
            ]
        return list(self._capsules.values())

    def get_capsule(self, category: str, name: str) -> Optional[CapabilityCapsule]:
        """Get a specific capsule by category and name."""
        return self._capsules.get(f"{category}:{name}")

    def list_categories(self) -> List[str]:
        """List all capsule categories available in the hive."""
        return list(set(c.category for c in self._capsules.values()))

    def to_dict(self) -> dict:
        """Return hive status as a dict for capability discovery."""
        return {
            "worker": self._worker_name,
            "capsule_count": len(self._capsules),
            "categories": self.list_categories(),
        }
