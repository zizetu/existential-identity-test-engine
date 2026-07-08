# EITE TOOL_SANDBOX 2026-07-09e
# Lightweight safety pre-check used by tool_executor (EITE light profile).
# Not a container jail — blocks clearly destructive shell patterns before exec.
"""Shell/tool safety pre-check for EITE agent tool_executor."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("eite.tool_sandbox")

_DANGEROUS = [
    re.compile(r"rm\s+-rf\s+/", re.I),
    re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force).*(/\s*$|/\s+\*|\s+/\s)", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", re.I),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
    re.compile(r"curl\s+[^|]*\|\s*(ba)?sh", re.I),
    re.compile(r"wget\s+[^|]*\|\s*(ba)?sh", re.I),
    re.compile(r"chmod\s+-R\s+777\s+/", re.I),
    re.compile(r"\biptables\s+-F\b", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
]


class SandboxRunner:
    def pre_check(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        args = args or {}
        if tool_name in {"shell_exec", "bash", "run_shell"}:
            cmd = str(args.get("command") or args.get("cmd") or "")
            for pat in _DANGEROUS:
                if pat.search(cmd):
                    return False, "blocked by tool_sandbox: dangerous pattern in command"
        return True, "ok"

    def check_command(self, command: str) -> Tuple[bool, str]:
        return self.pre_check("shell_exec", {"command": command})


_RUNNER: Optional[SandboxRunner] = None


def get_sandbox_runner() -> SandboxRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = SandboxRunner()
    return _RUNNER
