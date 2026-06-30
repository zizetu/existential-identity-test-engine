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
# Original repository: https://github.com/zizetu/eite-agent
#

"""
Error Logger Module
===================

Minimal error logger — provides enough for CLI introspection.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ErrorRecord:
    """A single error record."""
    message: str
    source: str = "unknown"
    resolved: bool = False
    severity: str = "ERROR"


class ErrorLogger:
    """Simple in-memory error logger."""

    def __init__(self):
        self._errors: List[ErrorRecord] = []

    def log_error(self, message: str, source: str = "unknown", severity: str = "ERROR"):
        self._errors.append(ErrorRecord(message=message, source=source, severity=severity))

    def get_error_stats(self) -> dict:
        unresolved = sum(1 for e in self._errors if not e.resolved)
        return {
            "total": len(self._errors),
            "unresolved": unresolved,
            "resolved": len(self._errors) - unresolved,
        }

    def get_unresolved(self) -> List[ErrorRecord]:
        return [e for e in self._errors if not e.resolved]


_logger_instance: Optional[ErrorLogger] = None


def get_error_logger() -> ErrorLogger:
    """Get or create the singleton error logger."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = ErrorLogger()
    return _logger_instance
