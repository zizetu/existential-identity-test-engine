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

"""AST dangerous code analyzer - distilled from NVIDIA SkillSpector (Apache 2.0).

Detects dangerous execution patterns (exec, eval, subprocess, os exec-family,
dynamic import, compiled code, dynamic attr access, dangerous chains) using
the Python ast module with zero external dependencies.
"""

from __future__ import annotations

import ast
import time
from typing import Optional

from .types import AuditResult, Finding


# ---------------------------------------------------------------------------
# Helpers (adapted from SkillSpector common.py - inline, no deps)
# ---------------------------------------------------------------------------

def _resolve_dotted_name(node: ast.expr) -> str | None:
    """Build a dotted name from a Name or Attribute node.

    ``ast.Name(id='exec')`` → ``'exec'``
    ``ast.Attribute(value=Name('os'), attr='system')`` → ``'os.system'``
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        current: object = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def resolve_call_name(node: ast.Call) -> str | None:
    """Extract a dotted call name like ``'os.system'`` from a Call node."""
    return _resolve_dotted_name(node.func)


def get_context_from_lines(lines: list[str], lineno: int, window: int = 3) -> str:
    """Extract surrounding lines given pre-split *lines* and a 1-based *lineno*."""
    start = max(0, lineno - 1 - window)
    end = min(len(lines), lineno + window)
    return "\n".join(lines[start:end])


def _get_source_segment(lines: list[str], lineno: int, end_lineno: int | None) -> str:
    """Extract the source text for a given line range, truncated to 200 chars."""
    start = max(0, lineno - 1)
    end = end_lineno or lineno
    return "\n".join(lines[start:end])[:200]


# ---------------------------------------------------------------------------
# Danger signatures
# ---------------------------------------------------------------------------

_SUBPROCESS_CALLS: frozenset[str] = frozenset({
    "call", "run", "Popen", "check_output", "check_call",
    "getoutput", "getstatusoutput",
})

_OS_EXEC_CALLS: frozenset[str] = frozenset({
    "system", "popen", "execl", "execle", "execlp", "execlpe",
    "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp",
})

_RULE_MESSAGES: dict[str, str] = {
    "AST1": "exec() call detected",
    "AST2": "eval() call detected",
    "AST3": "Dynamic import via __import__()",
    "AST4": "subprocess module call",
    "AST5": "os.system() or os exec-family call",
    "AST6": "compile() call detected",
    "AST7": "Dynamic attribute access via getattr()",
    "AST8": "Dangerous execution chain",
}

_RULE_SEVERITIES: dict[str, str] = {
    "AST1": "HIGH",
    "AST2": "HIGH",
    "AST3": "MEDIUM",
    "AST4": "MEDIUM",
    "AST5": "HIGH",
    "AST6": "MEDIUM",
    "AST7": "LOW",
    "AST8": "CRITICAL",
}

_RULE_CONFIDENCES: dict[str, float] = {
    "AST1": 0.85,
    "AST2": 0.85,
    "AST3": 0.75,
    "AST4": 0.70,
    "AST5": 0.85,
    "AST6": 0.65,
    "AST7": 0.50,
    "AST8": 0.95,
}

_TAG = "Dangerous Code Execution"


# ---------------------------------------------------------------------------
# ASTDangerAnalyzer
# ---------------------------------------------------------------------------

class ASTDangerAnalyzer:
    """Detect dangerous Python execution patterns via AST analysis.

    Uses :mod:`ast` exclusively - no external dependencies.
    Mirrors the concept of SkillSpector's ``behavioral_ast`` analyzer.
    """

    @classmethod
    def scan_file(cls, path: str) -> AuditResult:
        """Read file at *path*, parse it, and run AST danger analysis."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except FileNotFoundError:
            return AuditResult(
                findings=[],
                analyzer="ast_danger",
                error=f"File not found: {path}",
            )
        except OSError as exc:
            return AuditResult(
                findings=[],
                analyzer="ast_danger",
                error=str(exc),
            )
        return cls.analyze(content, file_path=path)

    @staticmethod
    def analyze(content: str, file_path: str = "") -> AuditResult:
        """Analyze *content* (Python source) for dangerous AST patterns.

        Returns an ``AuditResult`` containing zero or more ``Finding`` objects
        matching the rules AST1–AST8.
        """
        start = time.perf_counter()

        try:
            tree = ast.parse(content, filename=file_path or "<string>")
        except SyntaxError:
            return AuditResult(
                findings=[],
                analyzer="ast_danger",
                duration_ms=(time.perf_counter() - start) * 1000,
                error="SyntaxError - skipping non-parseable content",
            )

        lines = content.splitlines()
        findings: list[Finding] = []

        def _is_chain_sink(node: ast.Call) -> bool:
            """True if this call is exec(), eval(), or compile()."""
            name = resolve_call_name(node)
            return name in ("exec", "eval", "compile")

        def _contains_dangerous_source(node: ast.AST) -> str | None:
            """Walk children to find a nested dangerous call that forms a chain."""
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                name = resolve_call_name(child)
                if name is None:
                    continue
                if name in ("compile", "__import__"):
                    return name
                if name.startswith("subprocess.") or name.startswith("os."):
                    return name
                if any(
                    part in name
                    for part in ("base64", "codecs", "marshal", "urllib", "requests", "httpx")
                ):
                    return name
            return None

        def _emit(
            rule_id: str,
            lineno: int,
            end_lineno: int | None,
            msg_override: str | None = None,
        ) -> None:
            findings.append(
                Finding(
                    rule_id=rule_id,
                    message=msg_override or _RULE_MESSAGES[rule_id],
                    severity=_RULE_SEVERITIES[rule_id],
                    file_path=file_path,
                    line=lineno,
                    confidence=_RULE_CONFIDENCES[rule_id],
                    context=get_context_from_lines(lines, lineno),
                    matched_text=_get_source_segment(lines, lineno, end_lineno),
                    tags=[_TAG],
                )
            )

        for ast_node in ast.walk(tree):
            if not isinstance(ast_node, ast.Call):
                continue

            call_name = resolve_call_name(ast_node)
            if call_name is None:
                continue

            lineno = getattr(ast_node, "lineno", 1)
            end_lineno = getattr(ast_node, "end_lineno", None)

            # --- AST1 / AST8: exec() ---
            if call_name == "exec":
                if _is_chain_sink(ast_node) and ast_node.args:
                    source = _contains_dangerous_source(ast_node.args[0])
                    if source:
                        _emit("AST8", lineno, end_lineno, f"Dangerous chain: exec() wrapping {source}")
                _emit("AST1", lineno, end_lineno)

            # --- AST2 / AST8: eval() ---
            elif call_name == "eval":
                if _is_chain_sink(ast_node) and ast_node.args:
                    source = _contains_dangerous_source(ast_node.args[0])
                    if source:
                        _emit("AST8", lineno, end_lineno, f"Dangerous chain: eval() wrapping {source}")
                _emit("AST2", lineno, end_lineno)

            # --- AST3: __import__() ---
            elif call_name == "__import__":
                _emit("AST3", lineno, end_lineno)

            # --- AST6: compile() ---
            elif call_name == "compile":
                _emit("AST6", lineno, end_lineno)

            # --- AST4: subprocess.{call,run,...} ---
            elif call_name.startswith("subprocess."):
                attr = call_name.split(".", 1)[1]
                if attr in _SUBPROCESS_CALLS:
                    _emit("AST4", lineno, end_lineno)

            # --- AST5: os.{system,popen,execl,...} ---
            elif call_name.startswith("os."):
                attr = call_name.split(".", 1)[1]
                if attr in _OS_EXEC_CALLS:
                    _emit("AST5", lineno, end_lineno)

            # --- AST7: getattr() with non-constant arg ---
            elif call_name == "getattr" and len(ast_node.args) >= 2:
                if not isinstance(ast_node.args[1], ast.Constant):
                    _emit("AST7", lineno, end_lineno)

        duration_ms = (time.perf_counter() - start) * 1000
        return AuditResult(
            findings=findings,
            analyzer="ast_danger",
            duration_ms=duration_ms,
        )
