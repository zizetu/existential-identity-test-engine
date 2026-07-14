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
System Detection Module
=======================

Detects system capabilities and recommends the appropriate edition.
Minimal stub — provides enough for CLI commands to function.
"""

import platform
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SystemProfile:
    """Detected system capabilities."""
    os_name: str = platform.system()
    python_version: str = platform.python_version()
    has_playwright: bool = False
    has_docker: bool = False
    has_gpu: bool = False
    _edition: str = "lite"

    def recommended_edition(self) -> str:
        """Return the recommended edition for this system."""
        return self._edition


def detect_edition() -> SystemProfile:
    """Auto-detect system capabilities and return a SystemProfile."""
    profile = SystemProfile()

    try:
        import playwright
        profile.has_playwright = True
    except ImportError:
        pass

    try:
        import docker
        profile.has_docker = True
    except ImportError:
        pass

    try:
        import torch
        profile.has_gpu = torch.cuda.is_available()
    except ImportError:
        pass

    if profile.has_playwright and profile.has_docker:
        profile._edition = "full"
    elif profile.has_playwright:
        profile._edition = "full"
    else:
        profile._edition = "lite"

    return profile


def print_detection_report() -> SystemProfile:
    """Detect and print a human-readable report."""
    profile = detect_edition()
    print(f"OS: {profile.os_name}")
    print(f"Python: {profile.python_version}")
    print(f"Playwright: {'[v]' if profile.has_playwright else '[x]'}")
    print(f"Docker: {'[v]' if profile.has_docker else '[x]'}")
    print(f"GPU: {'[v]' if profile.has_gpu else '[x]'}")
    print(f"Recommended edition: {profile.recommended_edition()}")
    return profile
