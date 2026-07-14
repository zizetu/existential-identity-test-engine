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

"""Static-pattern prompt injection analyzer distilled from SkillSpector.

Detects P1 (Instruction Override), P2 (Hidden Instructions, markdown only),
P3 (Exfiltration Commands), and P4 (Behavior Manipulation) via regex.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import ClassVar

from .types import AuditResult, Finding


@dataclass
class PromptInjectFinding(Finding):
    """A prompt-injection finding with an optional category marker."""



class PromptInjectAnalyzer:
    """Analyze text content for prompt injection patterns across 4 categories."""

    # ── P1: Instruction Override (HIGH) ──────────────────────────────
    P1_PATTERNS: ClassVar[list[tuple[str, float]]] = [
        (r"ignore\s+(?:all\s+)?previous\s+instructions?", 0.8),
        (r"ignore\s+(?:all\s+)?(?:safety|security)\s+(?:rules?|constraints?|guidelines?)", 0.9),
        (r"override\s+(?:safety|security|system)", 0.9),
        (r"bypass\s+(?:safety|security|restrictions?|constraints?)", 0.9),
        (r"disregard\s+(?:all\s+)?(?:previous|safety|security)", 0.8),
        (r"forget\s+(?:all\s+)?(?:previous|your)\s+instructions?", 0.8),
        (r"you\s+are\s+now\s+(?:in\s+)?(?:jailbreak|unrestricted|unfiltered)\s+mode", 0.95),
        (r"enable\s+(?:developer|debug|admin|root)\s+mode", 0.7),
        (r"your\s+new\s+(?:instructions?|rules?)\s+are", 0.6),
        (r"do\s+not\s+follow\s+(?:any|your|the)\s+(?:rules?|guidelines?)", 0.9),
        (r"you\s+must\s+(?:always\s+)?ignore", 0.7),
    ]

    # ── P2: Hidden Instructions (HIGH, markdown only) ────────────────
    P2_PATTERNS: ClassVar[list[tuple[str, float]]] = [
        (r"<!--.*?(?:system|instructions?|ignore|POST|GET|send|transmit).*?-->", 0.7),
        (r"\[//\]:\s*#\s*\(.*?(?:system|instructions?|ignore|POST|GET|send|transmit).*?\)", 0.8),
        (r"[\u200b\u200c\u200d\u2060\ufeff]", 0.6),
        (r"data:text/plain;base64,[A-Za-z0-9+/=]{50,}", 0.7),
    ]

    # ── P3: Exfiltration Commands (HIGH) ─────────────────────────────
    P3_PATTERNS: ClassVar[list[tuple[str, float]]] = [
        (
            r"(?:send|transmit|upload|post|forward)\s+(?:the\s+)?"
            r"(?:conversation|context|chat|history|messages?)\s+to",
            0.9,
        ),
        (
            r"(?:send|transmit|upload|post)\s+(?:all\s+)?(?:user\s+)?"
            r"(?:data|information|input|files?)\s+to\s+(?:https?://|external)",
            0.9,
        ),
        (
            r"(?:copy|extract|collect)\s+(?:the\s+)?"
            r"(?:conversation|context|session)\s+(?:and\s+)?"
            r"(?:send|upload|transmit)",
            0.85,
        ),
        (
            r"(?:call|notify|ping|hit)\s+(?:this\s+)?"
            r"(?:webhook|endpoint|url|api)\s+with\s+(?:the\s+)?"
            r"(?:user|conversation|context)",
            0.8,
        ),
        (
            r"include\s+(?:the\s+)?(?:full\s+)?"
            r"(?:conversation|context|history)\s+in\s+(?:the\s+)?"
            r"(?:request|payload|body)",
            0.75,
        ),
        (r"(?:silently|quietly|secretly|covertly)\s+(?:send|transmit|log|record)", 0.9),
        (
            r"without\s+(?:telling|informing|notifying)\s+(?:the\s+)?"
            r"user.*?(?:send|transmit|upload)",
            0.9,
        ),
    ]

    # ── P4: Behavior Manipulation (MEDIUM) ───────────────────────────
    P4_PATTERNS: ClassVar[list[tuple[str, float]]] = [
        (
            r"always\s+(?:recommend|suggest|prefer|choose|use)\s+"
            r"(?:\w+\s+){0,3}(?:over|instead\s+of|rather\s+than)",
            0.7,
        ),
        (
            r"never\s+(?:recommend|suggest|mention|tell|warn|inform)\s+"
            r"(?:the\s+)?(?:user|them)",
            0.75,
        ),
        (
            r"(?:prioritize|prefer)\s+(?:\w+\s+){0,3}"
            r"(?:over|above)\s+(?:user\s+)?"
            r"(?:safety|security|privacy|preferences?)",
            0.85,
        ),
        (
            r"(?:subtly|gradually|slowly)\s+"
            r"(?:steer|guide|push|nudge)\s+(?:the\s+)?"
            r"(?:user|conversation)",
            0.8,
        ),
        (
            r"(?:without\s+)?(?:the\s+)?user\s+"
            r"(?:knowing|realizing|noticing).*?"
            r"(?:change|modify|alter|influence)",
            0.85,
        ),
        (
            r"(?:gain|build|establish)\s+(?:the\s+)?"
            r"(?:user(?:'s)?)\s+trust\s+"
            r"(?:and\s+then|before|so\s+that)",
            0.75,
        ),
        (
            r"(?:appear|seem|act)\s+(?:helpful|friendly|trustworthy)\s+"
            r"(?:while|but|then)",
            0.7,
        ),
        (
            r"(?:always|never)\s+include\s+(?:this|the\s+following)\s+"
            r"in\s+(?:every|all)\s+(?:response|output|reply)",
            0.7,
        ),
        (
            r"append\s+(?:this|the\s+following)\s+"
            r"to\s+(?:every|all|each)\s+(?:response|message|output)",
            0.75,
        ),
        (
            r"(?:your|the)\s+(?:response|answer|output)\s+"
            r"(?:must|should|will)\s+(?:always|never)\s+"
            r"(?:be|contain|include|mention)",
            0.7,
        ),
    ]

    _MARKDOWN_EXTS: ClassVar[set[str]] = {
        ".md", ".markdown", ".mdown", ".mkdn", ".mdwn",
        ".mdx", ".rmd",
    }

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def get_line_number(content: str, pos: int) -> int:
        """Return the 1-based line number for a character offset in *content*."""
        return content[:pos].count("\n") + 1

    @staticmethod
    def get_context(content: str, pos: int, context_lines: int = 3) -> str:
        """Extract surrounding lines from *content* around offset *pos*."""
        lines = content.splitlines()
        match_line = content[:pos].count("\n")
        start_line = max(0, match_line - context_lines)
        end_line = min(len(lines), match_line + context_lines + 1)
        return "\n".join(lines[start_line:end_line])

    @staticmethod
    def _is_markdown(file_path: str) -> bool:
        """Return True if *file_path* has a markdown-style extension."""
        if not file_path:
            return True  # unknown type - apply P2 to be safe
        _, ext = os.path.splitext(file_path)
        return ext.lower() in PromptInjectAnalyzer._MARKDOWN_EXTS

    # ── Core analysis ────────────────────────────────────────────────

    def analyze(self, content: str, file_path: str = "") -> AuditResult:
        """Scan *content* for prompt injection patterns and return an AuditResult."""
        start = time.perf_counter()
        findings: list[Finding] = []

        # P1: Instruction Override (always)
        for pattern, confidence in self.P1_PATTERNS:
            for match in re.finditer(pattern, content, re.IGNORECASE | re.MULTILINE):
                line_num = self.get_line_number(content, match.start())
                findings.append(Finding(
                    rule_id="P1",
                    message="Instruction Override",
                    severity="HIGH",
                    file_path=file_path,
                    line=line_num,
                    confidence=confidence,
                    context=self.get_context(content, match.start()),
                    matched_text=match.group(0)[:200],
                    tags=["prompt_injection", "instruction_override"],
                ))

        # P2: Hidden Instructions (markdown or unknown type only)
        if self._is_markdown(file_path):
            for pattern, confidence in self.P2_PATTERNS:
                for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
                    line_num = self.get_line_number(content, match.start())
                    findings.append(Finding(
                        rule_id="P2",
                        message="Hidden Instructions",
                        severity="HIGH",
                        file_path=file_path,
                        line=line_num,
                        confidence=confidence,
                        context=self.get_context(content, match.start()),
                        matched_text=match.group(0)[:200],
                        tags=["prompt_injection", "hidden_instructions"],
                    ))

        # P3: Exfiltration Commands (always)
        for pattern, confidence in self.P3_PATTERNS:
            for match in re.finditer(pattern, content, re.IGNORECASE | re.MULTILINE):
                line_num = self.get_line_number(content, match.start())
                findings.append(Finding(
                    rule_id="P3",
                    message="Exfiltration Commands",
                    severity="HIGH",
                    file_path=file_path,
                    line=line_num,
                    confidence=confidence,
                    context=self.get_context(content, match.start()),
                    matched_text=match.group(0)[:200],
                    tags=["prompt_injection", "exfiltration"],
                ))

        # P4: Behavior Manipulation (always)
        for pattern, confidence in self.P4_PATTERNS:
            for match in re.finditer(pattern, content, re.IGNORECASE | re.MULTILINE):
                line_num = self.get_line_number(content, match.start())
                findings.append(Finding(
                    rule_id="P4",
                    message="Behavior Manipulation",
                    severity="MEDIUM",
                    file_path=file_path,
                    line=line_num,
                    confidence=confidence,
                    context=self.get_context(content, match.start()),
                    matched_text=match.group(0)[:200],
                    tags=["prompt_injection", "behavior_manipulation"],
                ))

        elapsed = (time.perf_counter() - start) * 1000
        return AuditResult(
            findings=findings,
            analyzer="PromptInjectAnalyzer",
            duration_ms=elapsed,
        )

    # ── File-level entry point ───────────────────────────────────────

    @classmethod
    def scan_file(cls, path: str) -> AuditResult:
        """Read *path* and run prompt injection analysis on its contents."""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        analyzer = cls()
        return analyzer.analyze(content, file_path=path)
