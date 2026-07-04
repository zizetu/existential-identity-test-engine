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

"""Guardian - Autonomous self-healing daemon for EITElite workers.

Modules:
    checks  - Programmatic code-quality checks (shell=True, CJK, compile, etc.)
    healer  - Decision table + fix actions (PULL, RESTART, PATCH, ALERT, ROLLBACK)
    daemon  - Main loop: poll GitHub → run checks → apply healer → alert Telegram

Usage:
    python3 -m tical_code.guardian.daemon /path/to/repo --poll-interval 300
"""

from tical_code.guardian.checks import CheckResult, run_all_checks
from tical_code.guardian.healer import HealAction, HealResult, heal

__all__ = [
    "CheckResult", "run_all_checks",
    "HealAction", "HealResult", "heal",
]
