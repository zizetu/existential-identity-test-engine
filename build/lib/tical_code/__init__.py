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
tical_code - AI Agent Deployment System
======================================

Version: Single source of truth

Active entry point:
    from tical_code.core.unified_worker import Worker
"""
import os
import pathlib

_version_path = pathlib.Path(__file__).parent.parent / "VERSION"
try:
    __version__ = _version_path.read_text().strip()
except (FileNotFoundError, OSError):
    __version__ = "0.0.0"
