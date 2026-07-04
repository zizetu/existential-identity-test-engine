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

"""SkillAuditRunner - run all SkillSpector distill analyzers on a target.

Orchestrates prompt injection, supply chain CVE, and AST dangerous code
detection.  Returns a combined AuditResult with findings from all three.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from .ast_danger import ASTDangerAnalyzer
from .prompt_injection import PromptInjectAnalyzer
from .supply_chain import SupplyChainAnalyzer
from .types import AuditResult, Finding

logger = logging.getLogger("EITElite.skillspector")

DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class SkillAuditRunner:
    """Run all three distilled analyzers on a target file or directory.

    Usage:
        runner = SkillAuditRunner()
        result = runner.scan_path("/path/to/skill.md")
        for f in result.findings:
            print(f"{f.severity}: {f.message} ({f.file_path}:{f.line})")
    """

    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_BYTES,
        enable_prompt_injection: bool = True,
        enable_supply_chain: bool = True,
        enable_ast_danger: bool = True,
    ):
        self.max_bytes = max_bytes
        self.enable_prompt_injection = enable_prompt_injection
        self.enable_supply_chain = enable_supply_chain
        self.enable_ast_danger = enable_ast_danger

    def scan_path(self, path: str) -> AuditResult:
        """Scan a file or directory with all enabled analyzers.

        For directories, walks recursively and scans every readable file.
        For files, detects type by extension and runs relevant analyzers.
        """
        if not os.path.exists(path):
            return AuditResult(
                findings=[],
                analyzer="skillspector",
                error=f"Path does not exist: {path}",
            )

        t0 = time.monotonic()
        all_findings: list[Finding] = []

        if os.path.isfile(path):
            all_findings.extend(self._scan_file(path))
        elif os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        if os.path.getsize(fpath) > self.max_bytes:
                            continue
                    except OSError:
                        continue
                    all_findings.extend(self._scan_file(fpath))

        duration = (time.monotonic() - t0) * 1000
        return AuditResult(
            findings=all_findings,
            analyzer="skillspector",
            duration_ms=round(duration, 1),
        )

    def scan_text(self, content: str, file_path: str = "inline") -> AuditResult:
        """Scan in-memory text content (no file I/O)."""
        t0 = time.monotonic()
        all_findings: list[Finding] = []

        if self.enable_prompt_injection:
            try:
                _pi = PromptInjectAnalyzer()
                r = _pi.analyze(content, file_path)
                all_findings.extend(r.findings)
            except Exception as e:
                logger.warning("PromptInjectAnalyzer error: %s", e)

        if self.enable_supply_chain and (file_path.endswith(".txt") or file_path.endswith(".toml")):
            try:
                _sc = SupplyChainAnalyzer()
                r = _sc.analyze(content, file_path)
                all_findings.extend(r.findings)
            except Exception as e:
                logger.warning("SupplyChainAnalyzer error: %s", e)

        if self.enable_ast_danger and file_path.endswith(".py"):
            try:
                r = ASTDangerAnalyzer.analyze(content, file_path)
                all_findings.extend(r.findings)
            except Exception as e:
                logger.warning("ASTDangerAnalyzer error: %s", e)

        duration = (time.monotonic() - t0) * 1000
        return AuditResult(
            findings=all_findings,
            analyzer="skillspector",
            duration_ms=round(duration, 1),
        )

    def _scan_file(self, path: str) -> list[Finding]:
        """Run applicable analyzers on a single file."""
        findings: list[Finding] = []
        ext = os.path.splitext(path)[1].lower()

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(self.max_bytes)
        except (OSError, UnicodeDecodeError):
            return findings

        # Prompt injection: scan all text files (md, py, txt, yaml, etc.)
        if self.enable_prompt_injection:
            try:
                _pi = PromptInjectAnalyzer()
                r = _pi.analyze(content, path)
                findings.extend(r.findings)
            except Exception as e:
                logger.warning("PromptInjectAnalyzer error on %s: %s", path, e)

        # Supply chain: only dep files
        if self.enable_supply_chain:
            if ext in (".txt", ".toml") and os.path.basename(path).startswith(
                ("requirements", "pyproject")
            ):
                try:
                    _sc = SupplyChainAnalyzer()
                    r = _sc.analyze(content, path)
                    findings.extend(r.findings)
                except Exception as e:
                    logger.warning("SupplyChainAnalyzer error on %s: %s", path, e)

        # AST danger: only Python files
        if self.enable_ast_danger and ext == ".py":
            try:
                r = ASTDangerAnalyzer.analyze(content, path)
                findings.extend(r.findings)
            except Exception as e:
                logger.warning("ASTDangerAnalyzer error on %s: %s", path, e)

        return findings
