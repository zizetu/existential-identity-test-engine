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

"""SkillSpector integration - distilled security analyzers.

Three distilled modules from NVIDIA SkillSpector (Apache 2.0):
  1. prompt_injection - Static pattern detection (P1-P4)  [active]
  2. supply_chain - OSV.dev CVE scanning for Python dependencies  [active]
  3. ast_danger - Python AST dangerous code detection (AST1-AST8)  [active]

Integrated as a standalone subsystem called from message_handler.py
privacy-scan phase, or via [CMD] /audit-skill for manual scanning.
"""

from .ast_danger import ASTDangerAnalyzer
from .prompt_injection import PromptInjectAnalyzer, PromptInjectFinding
from .supply_chain import SupplyChainAnalyzer
from .types import AuditResult

__all__ = [
    "ASTDangerAnalyzer",
    "PromptInjectAnalyzer",
    "PromptInjectFinding",
    "SupplyChainAnalyzer",
    "AuditResult",
]
