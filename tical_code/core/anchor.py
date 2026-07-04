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

# provenance:ticalasi-zzt-2026​
"""
Anchor Module
=============

Minimal bootstrap anchor management — provides enough for CLI introspection.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class AnchorType(Enum):
    """Type of bootstrap anchor."""
    IDENTITY = "identity"
    BEHAVIOR = "behavior"
    CONSTRAINT = "constraint"
    MEMORY = "memory"


@dataclass
class Anchor:
    """An immutable anchor point."""
    anchor_type: AnchorType
    key: str
    value: str
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "anchor_type": self.anchor_type.value,
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
        }


class AnchorManager:
    """Simple in-memory anchor manager."""

    def __init__(self):
        self._anchors: Dict[str, Anchor] = {}

    def get_valid_anchors(self) -> List[Anchor]:
        return list(self._anchors.values())

    def get(self, anchor_type: AnchorType, key: str) -> Optional[Anchor]:
        for a in self._anchors.values():
            if a.anchor_type == anchor_type and a.key == key:
                return a
        return None

    def set(self, anchor: Anchor):
        self._anchors[f"{anchor.anchor_type.value}:{anchor.key}"] = anchor

    def get_context_prompt(self) -> str:
        if not self._anchors:
            return "No anchors configured."
        parts = ["Bootstrap anchors:"]
        for a in self._anchors.values():
            parts.append(f"  [{a.anchor_type.value}] {a.key} = {a.value} (confidence: {a.confidence})")
        return "\n".join(parts)


_manager: Optional[AnchorManager] = None


def get_anchor_manager() -> AnchorManager:
    """Get or create the singleton anchor manager."""
    global _manager
    if _manager is None:
        _manager = AnchorManager()
    return _manager
