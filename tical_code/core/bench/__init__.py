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

"""EITE Bench - remote health monitoring and self-healing communication module.

Connects to the bench URL (configurable via BENCH_URL env var) for:
- Heartbeat upload (system resources, module health, provider status)
- Command pull (remote repair, self-check, config update)
- Self-check result push

Zero additional dependencies. Works on 1C1G.
"""

from .health_collector import HealthCollector
from .reporter import BenchReporter
from .listener import BenchListener

__all__ = ["HealthCollector", "BenchReporter", "BenchListener"]
