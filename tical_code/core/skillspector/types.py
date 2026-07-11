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

"""Shared types for SkillSpector integrations."""
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Finding:
    rule_id: str
    message: str
    severity: str  # LOW / MEDIUM / HIGH / CRITICAL
    file_path: str
    line: int
    confidence: float  # 0.0-1.0
    context: str = ""
    matched_text: str = ""
    tags: list[str] = field(default_factory=list)

@dataclass
class AuditResult:
    findings: list[Finding]
    analyzer: str
    duration_ms: float = 0.0
    error: Optional[str] = None

    @property
    def has_issues(self) -> bool:
        return len(self.findings) > 0

    @property
    def high_plus(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("HIGH", "CRITICAL")]
