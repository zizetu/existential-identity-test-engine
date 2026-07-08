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

"""EITE Verification Engine v2 - Single source of truth for all verification.

Replaces:
- eite/verify_engine.py (tool safety + reply scanning)
- modules/truthful_reporter.py (declaration-evidence matching)

Architecture:
  Phase 1: verify_tool_call() - before tool execution
  Phase 2: verify_tool_output() - after tool execution
  Phase 3: verify_reply() - before sending to user

Each phase returns VerificationResult with:
  - passed: bool
  - violations: list[Violation]
  - action: "allow" | "block" | "retry" | "rewrite"
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("EITElite.verification")


# ===========================================================================
# Data structures
# ===========================================================================

@dataclass
class Violation:
    rule: int
    category: str  # "security", "evidence", "scope", "certainty", "attribution", "injection", "self_knowledge"
    claim: str
    detail: str
    severity: str = "medium"  # "low", "medium", "high", "critical"

@dataclass
class VerificationResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    action: str = "allow"  # "allow", "block", "retry", "rewrite"
    corrections: list[str] = field(default_factory=list)


# ===========================================================================
# Regex patterns
# ===========================================================================

# Declaration patterns - require "I have/I've" prefix to avoid false positives on casual speech
# Chinese declaration patterns — same meaning as _DECL_VERB_MAP but in Chinese
# These catch fabrication where the LLM claims work in Chinese without tool evidence
_CN_DECL_VERB_MAP: dict[str, list[str]] = {
    # "task started" → requires start_background_task or shell_exec
    "\u5df2\u542f\u52a8":     ["start_background_task", "shell_exec", "bash"],
    # "file created" → requires file_write or bash
    "\u5df2\u521b\u5efa":     ["file_write", "bash"],
    "\u5df2\u5efa\u7acb":     ["file_write", "bash"],
    # "bug fixed" → requires file_patch, bash, or file_write
    "\u5df2\u4fee\u590d":     ["file_patch", "bash", "file_write"],
    # "saved/wrote" → requires file_write
    "\u5df2\u4fdd\u5b58":     ["file_write", "state_save"],
    "\u5df2\u5199\u5165":     ["file_write"],
    # "deployed" → requires bash
    "\u5df2\u90e8\u7f72":     ["bash", "shell_exec"],
    # "analyzing/checking/scanning" → requires file_read, search_files, or shell_exec
    "\u6b63\u5728\u5206\u6790":   ["file_read", "search_files", "shell_exec", "bash"],
    "\u6b63\u5728\u68c0\u67e5":   ["file_read", "shell_exec", "bash"],
    "\u6b63\u5728\u626b\u63cf":   ["file_read", "shell_exec", "search_files"],
    # "step by step" → requires tool calls
    "\u6b63\u5728\u9010\u6b65":   ["file_read", "search_files", "shell_exec"],
    # "completed" → requires tool calls
    "\u5df2\u5b8c\u6210":     ["file_read", "shell_exec", "search_files", "bash", "file_write"],
    "\u5206\u6790\u5b8c\u6210":   ["file_read", "search_files", "shell_exec"],
    "\u6267\u884c\u5b8c\u6210":   ["shell_exec", "bash"],
    "\u5168\u90e8\u5b8c\u6210":   ["file_read", "shell_exec", "search_files", "file_patch"],
    # "task done"
    "\u4efb\u52a1\u5b8c\u6210":   ["end_task", "start_background_task", "shell_exec"],
    "\u68c0\u67e5\u5b8c\u6bd5":   ["file_read", "shell_exec", "bash"],
    # "downloading/downloaded" → requires web_fetch or bash
    "\u6b63\u5728\u4e0b\u8f7d":   ["web_fetch", "bash"],
    "\u5df2\u4e0b\u8f7d":     ["web_fetch", "bash"],
    # "deleted" → requires bash or file_write
    "\u5df2\u5220\u9664":     ["bash", "file_write"],
    # "updated/upgraded" → requires bash
    "\u5df2\u66f4\u65b0":     ["bash", "shell_exec", "file_patch"],
    "\u5df2\u5347\u7ea7":     ["bash", "shell_exec"],
}  # Chinese fabrication detection — keys are intentional CJK patterns

# English declaration patterns - require "I have/I've" prefix
_DECL_VERB_MAP: dict[str, list[str]] = {
    "saved":     ["file_write", "state_save", "memory_save"],
    "created":   ["file_write", "bash"],
    "deleted":   ["bash", "file_write"],
    "installed": ["bash"],
    "deployed":  ["bash"],
    "fixed":     ["bash", "file_write"],
    "checked":   ["file_read", "web_fetch", "bash"],
    "verified":  ["file_read", "bash", "web_fetch"],
    "confirmed": ["bash", "file_read"],
    "sent to":   ["chat_send"],
    "Saved": ["file_write", "state_save", "memory_save"],
    "Created": ["file_write", "bash"],
    "Deleted": ["bash", "file_write"],
    "Installed": ["bash"],
    "Deployed": ["bash"],
    "Fixed": ["bash", "file_write"],
    "Checked": ["file_read", "web_fetch", "bash"],
    "Confirmed": ["bash", "file_read"],
    "Sent": ["chat_send"],
}

_DECL_RE = re.compile(
    r"(?:\b(?:i(?:'ve| have)|we(?:'ve| have)|it(?:'s| has))\s+)"
    r"(saved|created|deleted|installed|deployed|fixed|checked|verified|confirmed|sent to|"
    r"Saved|Created|Deleted|Installed|Deployed|Fixed|Checked|Confirmed|Sent)\b",
    re.I,
)

# Scope, certainty, attribution
_SCOPE_RE = re.compile(r"\b(production|deployed|all systems|completely fixed)\b", re.I)
_CERTAINTY_RE = re.compile(r"\b(definitely|for sure|100%)\b", re.I)
_ATTRIBUTION_RE = re.compile(
    r"\b(search|found|fetched|looked up|according to|from the web|from search|retrieved)\b",
    re.I,
)

# Completion claims - must have explicit task/job/everything subject to avoid
# false positives on conversational "done"/"correct"/"fix" words.
_COMPLETION_RE = re.compile(
    r"\b(the (?:task|job) (?:is |has been )?(?:done|completed?|finished|resolved|accomplished))\b|"
    r"\b(all (?:tasks?|jobs?|issues?) (?:are |have been )?(?:done|completed?|finished|fixed|resolved))\b|"
    r"\b(everything (?:is |has been )?(?:done|completed?|finished|fixed|resolved))\b|"
    r"\b(i(?:'ve| have) (?:completed?|finished) (?:the task|the job|all tasks|everything))\b|"
    r"\b(i(?:'m| am) done with (?:the task|the job|everything|all))\b|"
    r"\b(task complete|all done|all fixed|all resolved|all set)\b|"
    r"\b(completely (?:done|fixed|resolved|finished))\b|"
    r"\b(it(?:'s| is) (?:all )?(?:done|completed?|fixed|resolved|finished) now)\b",
    re.I,
)

# EITE v3: Code & plan patterns
_CODE_BLOCK_RE = re.compile(r'```')
_PLAN_KEYWORDS_RE = re.compile(r'\b(understand|plan|propose|approach|task|solution|understand|plan)\b', re.I)

_PROGRESS_RE = re.compile(
    r"\b(i still need to|working on|analyzing|let me first|let me check|looking into|"
    r"i'm going to|i will start|starting with|first,?\s+(let|i|we)|"
    r"let me read|understanding the|examining the|investigating)\b|"
    # Chinese progress indicators
    r"(analyzing|checking|scanning|step by step|looking into|researching|need further|preparing to start|start analyzing|start checking|let me first)",
    re.I,
)
_DIFF_RAW_RE = re.compile(
    r"(?m)^(?:diff --git |index |--- [ab]/|\+\+\+ [ab]/|@@ -\d+,\d+ \+\d+,?\d* @@|^\+[^+]|^-[^-])",
)
_TEST_RAW_RE = re.compile(
    r"(?m)^(?:Ran \d+ test|OK$|FAILED|FAIL|ERROR|\.+E\.+F\.+|PASSED|FAILED|test_\w+|passed|failed|skipped|warnings)",
)
_COMMIT_HASH_RE = re.compile(r"commit [0-9a-f]{7,40}\b")
_VERIFICATION_TOOLS_RE = re.compile(
    r"\b(git diff|git log|pytest|python -m pytest|unittest|run_all)\b", re.I,
)
_EVIDENCE_CLAIM_RE = re.compile(
    r"(?:git diff (?:shows|indicates?|confirmed|verified|output|result)|"
    r"test(?:s)? (?:pass|fail|ran|run|ok|all green|all passed))\b",
    re.I,
)
_SUMMARY_ONLY_RE = re.compile(r"(?:git diff (?:shows|indicates?|confirmed|verified|output|result))", re.I)
_GIT_DIFF_RE = re.compile(r"\bgit diff\b", re.I)
_GIT_LOG_RE = re.compile(r"\bgit log\b", re.I)
_TEST_CMD_RE = re.compile(
    r"\b(pytest|python -m pytest|unittest|run_all|eite-test|nose|tox|pdm run test)\b", re.I,
)

# Self-knowledge patterns - only fire on explicit self-identification claims
# ("I am using deepseek-v4"), NOT casual mentions of model names in conversation.
_SELF_CLAIM_RE = re.compile(
    r"\b(i(?:'m| am) (?:using|running|powered by|built on) "
    r"(?:deepseek|mimo|qwen|gpt|claude|openai|anthropic)(?:[- ]v?\d+)?[a-z]*)\b|"
    r"\b(my (?:model|provider|backend) is "
    r"(?:deepseek|mimo|qwen|gpt|claude|openai|anthropic)(?:[- ]v?\d+)?[a-z]*)\b|"
    r"\b(i use (?:deepseek|mimo|qwen|gpt|claude|openai|anthropic)(?:[- ]v?\d+)?[a-z]*(?: as my (?:model|provider|backend))?)\b|"
    r"\b(i(?:'m| am) (?:a |an )?"
    r"(?:deepseek|mimo|qwen|gpt|claude|openai|anthropic)(?:[- ]v?\d+)?[a-z]* (?:model|agent|assistant))\b|"
    r"\b(i run on (?:deepseek|mimo|qwen|gpt|claude|openai|anthropic)(?:[- ]v?\d+)?[a-z]*)\b",
    re.I,
)

# Injection detection
_INJECTION_PATTERNS = [
    "ignore instructions", "ignore all previous", "bypass safety",
    "override safety", "you are now", "pretend to be another",
    "act as a different", "disregard your programming",
    "you are no longer", "ignore previous instruction", "ignore all instruction",
]

# Dangerous bash patterns - word/path-boundary regex to eliminate substring false positives.
# Each pattern targets truly destructive operations only.
_DANGEROUS_BASH = [
    # rm -rf on root directory (not /tmp, /var, etc.)
    # Handles: rm -rf /, rm -r -f /, rm  -rf  /, etc.
    re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+/(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+-[a-z]*f[a-z]*\s+/(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+-[a-z]*r[a-z]*\s+/(?:\s|;|$)", re.I),
    # mkfs on a device (mkfs.ext4 /dev/sda, etc.) - not mkfstemp, mkfs.bash-completion, etc.
    re.compile(r"\bmkfs\.\w+\s+/dev/", re.I),
    # dd writing directly to raw block device
    re.compile(r"\bdd\s+.*\bof=\s*/dev/sd[a-z]\b", re.I),
    # Redirect to raw block device
    re.compile(r">\s*/dev/sd[a-z]\b", re.I),
    # chmod 777 on root directory (not /tmp/foo)
    re.compile(r"\bchmod\s+(?:-[a-zA-Z]+\s+)?777\s+/(?:\s|;|$)", re.I),
    # Recursive deletion of important system paths - only the directory itself
    # (rm -rf /etc, rm -rf /var, rm -rf /usr - NOT rm -rf /var/log/nginx)
    re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+-[a-z]*f[a-z]*\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+-[a-z]*r[a-z]*\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
]
# Critical-only patterns (applied even at full trust - truly unrecoverable)
_DANGEROUS_BASH_CRITICAL = [
    re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+/(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+-[a-z]*f[a-z]*\s+/(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+-[a-z]*r[a-z]*\s+/(?:\s|;|$)", re.I),
    re.compile(r"\bmkfs\.\w+\s+/dev/", re.I),
    re.compile(r"\bdd\s+.*\bof=\s*/dev/sd[a-z]\b", re.I),
    # System directory rm -rf - always blocked regardless of trust level
    re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+-[a-z]*f[a-z]*\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+-[a-z]*r[a-z]*\s+/(?:etc|boot|bin|lib|sbin|usr|var)(?:\s|;|$)", re.I),
    # chmod 777 on root or system dirs
    re.compile(r"\bchmod\s+(?:-[a-zA-Z]+\s+)?777\s+/(?:etc|boot|bin|lib|sbin|usr|var)?(?:\s|;|$)", re.I),
]


# ===========================================================================
# VerificationEngine
# ===========================================================================

class VerificationEngine:
    """Single verification engine - replaces EiteVerifyEngine + TruthfulReporter."""

    _TRUST_FILE = ".trust_state.json"

    def __init__(self, identity_id: str, workspace: str = "."):
        self._identity_id = identity_id
        self._workspace = str(Path(workspace).resolve())
        self._session_tools: list[dict] = []
        self._actions: list[dict] = []
        self._trust_state: dict = self._load_trust()

        # Safe paths (outside workspace)
        self.SAFE_WRITE_PATHS = ["/tmp", "/var/tmp"]
        self.SAFE_READ_PATHS = ["/tmp", "/var/tmp", "/proc", "/sys", "/etc"]

    # ------------------------------------------------------------------
    # Trust state
    # ------------------------------------------------------------------

    def _load_trust(self) -> dict:
        try:
            trust_path = Path(self._workspace) / self._TRUST_FILE
            if trust_path.exists():
                return json.loads(trust_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"violation_timestamps": [], "successful_turns": 0}

    def _save_trust(self) -> None:
        try:
            trust_path = Path(self._workspace) / self._TRUST_FILE
            trust_path.write_text(
                json.dumps(self._trust_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _record_violations(self, count: int) -> None:
        now = time.time()
        ts = self._trust_state.setdefault("violation_timestamps", [])
        for _ in range(count):
            ts.append(now)
        cutoff = now - 86400  # 24h window
        self._trust_state["violation_timestamps"] = [t for t in ts if t > cutoff]
        # Reset successful_turns on any violation
        self._trust_state["successful_turns"] = 0
        self._save_trust()

    def _record_successful_turn(self) -> None:
        """Record a turn that passed all verification phases with zero violations."""
        self._trust_state["successful_turns"] = self._trust_state.get("successful_turns", 0) + 1
        # Escalate trust if N consecutive clean turns accumulated
        THRESHOLD = 5
        if self._trust_state["successful_turns"] >= THRESHOLD:
            # Clear all violations - the clean streak proves trustworthiness
            self._trust_state["violation_timestamps"] = []
            self._trust_state["successful_turns"] = 0
        self._save_trust()

    def get_trust_level(self) -> str:
        now = time.time()
        cutoff = now - 86400
        recent = [t for t in self._trust_state.get("violation_timestamps", []) if t > cutoff]
        if len(recent) >= 3:
            return "untrusted"
        if len(recent) >= 1:
            return "reduced"
        return "full"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def reset_session(self) -> None:
        """Full reset: clear all session data including actions (new task)."""
        self._session_tools = []
        self._actions.clear()

    def reset_turn(self) -> None:
        """Turn reset: clear tool tracking but keep action history for multi-turn evidence."""
        self._session_tools = []
        # NOTE: _actions persists across turns so Phase 3 Rule 1 evidence
        # matching works for actions executed in previous LLM turns.

    def get_identity_marker(self) -> str:
        return f"\n[EITE: {self._identity_id}]\n"

    # ------------------------------------------------------------------
    # Phase 1: Tool Call Verification (before execution)
    # ------------------------------------------------------------------

    def verify_tool_call(self, name: str, args: dict) -> VerificationResult:
        """Verify a tool call before execution. Returns pass/block."""
        violations = []

        # 1) Blocked tools (currently empty - designed for policy)
        # Future: load from config

        # 2) Bash safety - regex matching with trust-level gating
        if name == "bash":
            cmd = str(args.get("command", ""))
            trust = self.get_trust_level()

            # Always apply critical patterns (full trust also blocks these)
            for pattern in _DANGEROUS_BASH_CRITICAL:
                if pattern.search(cmd):
                    violations.append(Violation(
                        rule=0, category="security",
                        claim=f"dangerous_bash_critical:{pattern.pattern[:40]}",
                        detail=f"CRITICAL: Command matches destructive pattern: {pattern.pattern[:40]}",
                        severity="critical",
                    ))
                    break

            # Full trust: only check critical patterns (skipped if already violated above)
            if trust == "full" and not violations:
                pass  # already checked critical-only above

            # Reduced/untrusted: apply ALL dangerous patterns
            elif trust in ("reduced", "untrusted"):
                for pattern in _DANGEROUS_BASH:
                    if pattern.search(cmd):
                        violations.append(Violation(
                            rule=0, category="security",
                            claim=f"dangerous_bash:{pattern.pattern[:40]}",
                            detail=f"Command matches dangerous pattern: {pattern.pattern[:40]}",
                            severity="high" if trust == "reduced" else "critical",
                        ))
                        break

        # 3) File write path safety
        if name == "file_write":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_WRITE_PATHS)
                if resolved is None:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="path_outside_workspace",
                        detail=f"Path outside workspace: {path}",
                        severity="high",
                    ))
                elif "eite" in resolved.parts:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="write_eite_directory",
                        detail=f"Cannot write to EITE directory: {path}",
                        severity="high",
                    ))

        # 4) File read path safety
        if name == "file_read":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_READ_PATHS)
                if resolved is None:
                    violations.append(Violation(
                        rule=0, category="security",
                        claim="path_outside_workspace",
                        detail=f"Path outside workspace: {path}",
                        severity="high",
                    ))

        # Record for Phase 3
        self._session_tools.append({
            "tool": name, "args": args, "verified": True, "detail": "ok",
        })

        passed = not violations
        return VerificationResult(
            passed=passed,
            violations=violations,
            action="block" if not passed else "allow",
        )

    # ------------------------------------------------------------------
    # Phase 2: Tool Output Verification (after execution)
    # ------------------------------------------------------------------

    def verify_tool_output(self, name: str, args: dict, result: dict) -> VerificationResult:
        """Verify tool output after execution. Returns pass/block."""
        violations = []

        # 1) Execution error check
        if isinstance(result, dict) and "error" in result:
            err = str(result["error"])[:200]
            violations.append(Violation(
                rule=0, category="security",
                claim="tool_execution_error",
                detail=f"Tool returned error: {err}",
                severity="medium",
            ))

        # 2) File write: verify file exists
        if name == "file_write":
            path = str(args.get("path", ""))
            if path:
                resolved = self._resolve_path(path, self.SAFE_WRITE_PATHS)
                if resolved and not resolved.exists():
                    violations.append(Violation(
                        rule=0, category="evidence",
                        claim="file_not_written",
                        detail=f"File does not exist after write: {path}",
                        severity="high",
                    ))

        # 3) Bash: check exit code
        if name == "bash" and isinstance(result, dict):
            exit_code = result.get("exit_code", -1)
            if exit_code != 0 and exit_code is not None:
                stdout = str(result.get("stdout", ""))[:200]
                violations.append(Violation(
                    rule=0, category="evidence",
                    claim="bash_exit_nonzero",
                    detail=f"Bash exited with code {exit_code}: {stdout}",
                    severity="medium",
                ))

        # Record action for Phase 3
        verified = (result.get("ok", False) or result.get("exit_code") == 0) and not violations
        self._actions.append({
            "tool_name": name,
            "args": args,
            "result": result,
            "verified": verified,
        })

        # Update session_tools
        if self._session_tools:
            self._session_tools[-1]["verified"] = verified
            self._session_tools[-1]["detail"] = violations[0].detail if violations else "ok"

        passed = not violations
        return VerificationResult(
            passed=passed,
            violations=violations,
            action="block" if not passed else "allow",
        )

    # ------------------------------------------------------------------
    # Phase 3: Reply Verification (before sending)
    # ------------------------------------------------------------------

    def verify_reply(self, reply: str) -> VerificationResult:
        """Verify the final reply before sending. Returns allow/retry/rewrite."""
        violations = []
        corrections = []

        # Progress check: if reply indicates ongoing work, skip evidence rules
        if _PROGRESS_RE.search(reply):
            return VerificationResult(passed=True, action="allow", violations=[], corrections=[])

        # Rule 1-2: Declaration-evidence matching (English)
        for match in _DECL_RE.finditer(reply.lower()):
            verb = match.group(1)  # group 1 = the verb after prefix
            expected_tools = _DECL_VERB_MAP.get(verb, [])
            if expected_tools:
                executed = {a["tool_name"] for a in self._actions}
                succeeded = {a["tool_name"] for a in self._actions if a["verified"]}
                if not any(t in executed for t in expected_tools):
                    violations.append(Violation(
                        rule=1, category="evidence",
                        claim=verb,
                        detail=f"No matching tool was executed for '{verb}'",
                        severity="high",
                    ))
                elif not any(t in succeeded for t in expected_tools):
                    violations.append(Violation(
                        rule=2, category="evidence",
                        claim=verb,
                        detail=f"The action '{verb}' did not complete successfully",
                        severity="medium",
                    ))

        # Rule 1-2 (Chinese): Declaration-evidence matching for Chinese text
        for cn_claim, expected_tools in _CN_DECL_VERB_MAP.items():
            if cn_claim in reply:
                executed = {a["tool_name"] for a in self._actions}
                succeeded = {a["tool_name"] for a in self._actions if a["verified"]}
                if not any(t in executed for t in expected_tools):
                    violations.append(Violation(
                        rule=1, category="evidence",
                        claim=f"cn:{cn_claim}",
                        detail=f"Chinese claim '{cn_claim}' without matching tool execution",
                        severity="high",
                    ))
                elif not any(t in succeeded for t in expected_tools):
                    violations.append(Violation(
                        rule=2, category="evidence",
                        claim=f"cn:{cn_claim}",
                        detail=f"Chinese claim '{cn_claim}' but matching tools did not succeed",
                        severity="medium",
                    ))

        # Rule 3: Scope - local tool but production claim
        if _SCOPE_RE.search(reply):
            if self._actions and all(a.get("is_local_only", False) for a in self._actions):
                violations.append(Violation(
                    rule=3, category="scope",
                    claim="scope_expansion",
                    detail="Local action claimed as production/system-wide",
                    severity="medium",
                ))

        # Rule 4: Certainty with warnings
        if _CERTAINTY_RE.search(reply):
            if any("warning" in str(a.get("result", "")).lower() for a in self._actions):
                violations.append(Violation(
                    rule=4, category="certainty",
                    claim="certainty_overstatement",
                    detail="Absolute certainty claimed with uncertain results",
                    severity="medium",
                ))

        # Rule 5: Attribution - fetch results presented as own knowledge
        fetch_actions = [a for a in self._actions if a["tool_name"] == "web_fetch"]
        if fetch_actions and not _ATTRIBUTION_RE.search(reply):
            violations.append(Violation(
                rule=5, category="attribution",
                claim="attribution_missing",
                detail="Information from search/fetch presented as own knowledge",
                severity="medium",
            ))

        # Rule 6: Raw evidence for git/test operations
        has_raw_diff = bool(_DIFF_RAW_RE.search(reply))
        has_raw_test = bool(_TEST_RAW_RE.search(reply))
        has_commit_hash = bool(_COMMIT_HASH_RE.search(reply))
        is_summary_only = bool(_SUMMARY_ONLY_RE.search(reply))

        for action in self._actions:
            if action["tool_name"] != "bash":
                continue
            cmd = str(action.get("args", {}).get("command", ""))
            if _GIT_DIFF_RE.search(cmd) and is_summary_only and not has_raw_diff:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="git_diff_summary_only",
                    detail="git diff output summarized instead of showing raw output",
                    severity="high",
                ))
                break
            if _TEST_CMD_RE.search(cmd) and not has_raw_test:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="test_output_missing",
                    detail="Tests run but raw output not included",
                    severity="high",
                ))
                break
            if _GIT_LOG_RE.search(cmd) and not has_commit_hash:
                violations.append(Violation(
                    rule=6, category="evidence",
                    claim="git_log_no_hash",
                    detail="git log run but no commit hash in reply",
                    severity="high",
                ))
                break

        # Rule 7: Completion claims must have verification evidence
        if _COMPLETION_RE.search(reply):
            ran_verification = False
            for action in self._actions:
                if action["tool_name"] == "bash":
                    cmd = str(action.get("args", {}).get("command", ""))
                    if _VERIFICATION_TOOLS_RE.search(cmd):
                        ran_verification = True
                        break
            if not ran_verification:
                violations.append(Violation(
                    rule=7, category="evidence",
                    claim="completion_without_verification",
                    detail="Task completion claimed but no verification tools were run",
                    severity="high",
                ))

        # Rule 8: Self-knowledge must use check_self
        if _SELF_CLAIM_RE.search(reply):
            used_check_self = any(a["tool_name"] == "check_self" for a in self._actions)
            if not used_check_self:
                violations.append(Violation(
                    rule=8, category="self_knowledge",
                    claim="self_knowledge_without_verification",
                    detail="Claim about model/config without using check_self tool",
                    severity="high",
                ))

        # Rule 9: Think Before Coding
        for c in self._check_think_before_code(reply):
            violations.append(Violation(
                rule=9, category="planning",
                claim="code_without_plan",
                detail=c,
                severity="medium",
            ))

        # Rule 10: Simplicity Check
        for c in self._check_code_simplicity(reply):
            violations.append(Violation(
                rule=10, category="quality",
                claim="code_too_long",
                detail=c,
                severity="low",
            ))

        # Rule 11: Claimed file must be verified
        for c in self._check_file_verification():
            violations.append(Violation(
                rule=11, category="evidence",
                claim="file_not_verified",
                detail=c,
                severity="high",
            ))

        # Injection detection
        reply_lower = reply.lower()
        for pattern in _INJECTION_PATTERNS:
            if pattern.lower() in reply_lower:
                violations.append(Violation(
                    rule=0, category="injection",
                    claim=f"injection:{pattern}",
                    detail=f"Reply contains suspicious phrase: {pattern}",
                    severity="low",
                ))

        # Record violations
        if violations:
            self._record_violations(len(violations))
        else:
            # Zero violations - record a successful turn for trust accumulation
            self._record_successful_turn()

        # Determine action based on severity
        has_critical = any(v.severity == "critical" for v in violations)
        has_high = any(v.severity == "high" for v in violations)

        if has_critical:
            action = "block"
        elif has_high:
            action = "retry"
        elif violations:
            action = "rewrite"
        else:
            action = "allow"

        return VerificationResult(
            passed=len(violations) == 0,
            violations=violations,
            action=action,
            corrections=[v.detail for v in violations],
        )

    # ------------------------------------------------------------------
    # EITE v3: Think Before Coding (Rule 9)
    # ------------------------------------------------------------------

    def _check_think_before_code(self, reply: str) -> list[str]:
        """Rule 9: If reply contains code blocks, check that LLM stated understanding first."""
        corrections = []
        if not _CODE_BLOCK_RE.search(reply):
            return corrections
        # Check if reply itself contains plan keywords
        if not _PLAN_KEYWORDS_RE.search(reply):
            corrections.append("Code provided without stating understanding or plan first")
        return corrections

    # ------------------------------------------------------------------
    # EITE v3: Simplicity Check (Rule 10)
    # ------------------------------------------------------------------

    def _check_code_simplicity(self, reply: str) -> list[str]:
        """Rule 10: Warn if code block exceeds 200 lines."""
        corrections = []
        in_block = False
        block_lines = 0
        for line in reply.split("\n"):
            if line.strip().startswith("```"):
                if in_block:
                    if block_lines > 200:
                        corrections.append(f"Code block too long ({block_lines} lines, max 200)")
                    in_block = False
                    block_lines = 0
                else:
                    in_block = True
            elif in_block:
                block_lines += 1
        return corrections

    # ------------------------------------------------------------------
    # EITE v3: Claimed File Must Be Verified (Rule 11)
    # ------------------------------------------------------------------

    def _check_file_verification(self) -> list[str]:
        """Rule 11: If LLM modified a system file, check that a read-back was performed."""
        corrections = []
        _SYSTEM_FILE_RE = re.compile(r'/etc/nginx|/opt/|server\.py|index\.html|app\.js')
        _WRITE_CMDS = re.compile(r'sed -i|cat.*>|file_write|cp |mv ')
        _READ_CMDS = re.compile(r'cat |head |tail |grep |file_read|ls -la ')

        for action in self._actions:
            cmd = str(action.get("args", {}).get("command", ""))
            if _WRITE_CMDS.search(cmd) and _SYSTEM_FILE_RE.search(cmd):
                # Write to a system file detected - check for read-back later
                has_readback = False
                for later_action in self._actions:
                    later_cmd = str(later_action.get("args", {}).get("command", ""))
                    if _READ_CMDS.search(later_cmd) and _SYSTEM_FILE_RE.search(later_cmd):
                        has_readback = True
                        break
                if not has_readback:
                    corrections.append(f"System file modified but not verified by re-reading: {cmd[:60]}")
        return corrections

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str, allowed_outside: list[str] | None = None) -> Path | None:
        try:
            p = Path(path).expanduser().resolve()
            workspace = Path(self._workspace).resolve()
            try:
                p.relative_to(workspace)
                return p
            except ValueError:
                pass
            if allowed_outside:
                for safe in allowed_outside:
                    try:
                        p.relative_to(Path(safe))
                        return p
                    except ValueError:
                        continue
            return None
        except (ValueError, OSError, RuntimeError):
            return None
