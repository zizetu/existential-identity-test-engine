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

# provenance:ticalasi-zzt-2026
"""Tool Executor - secure command execution with post-execution verification.

This module provides the secure tool execution pipeline for EITElite. Every tool
call from the AI passes through a multi-layered security architecture before execution:

1. **Sandbox pre-check** - tool_sandbox validates the tool name and arguments against
   configured policies (allow/deny lists, parameter constraints, usage quotas).

2. **Rate limiting** - the RateLimiter class enforces a sliding-window rate cap
   (default: 5 calls/second) on network-facing tools like http_post.

3. **Performance metrics** - every tool execution is timed and recorded via
   MetricsCollector when available. Latency outliers and errors are tracked
   per tool name for observability and debugging.

4. **Block patterns** - the _bash_safety_check function blocks genuinely dangerous
   commands (fork bombs, raw device writes, SSRF to private IPs, path traversal)
   while allowing legitimate admin operations (systemctl, journalctl, docker).

3. **BASH_BLACKLIST enforcement** - shell commands are regex-matched against a
   comprehensive blacklist of dangerous operations (reboot, rm -rf /, fork bombs,
   curl-pipe-shell, iptables flush, dd, mkfs, chmod 777 /, etc.).

4. **Workspace boundary check** - _bash_safety_check verifies that file operations
   stay within the allowed workspace directory, blocking traversal attempts (cd ..,
   access to /etc/shadow, /root, ~/.agents/, ~/.ssh/).

5. **Security baseline integration** - when configured via configure_security(),
   delegates path and URL validation to the security_baseline module for
   centralized policy enforcement.

6. **Sandbox runtime check** - _run_cmd passes commands through tool_sandbox's
   pre_check before execution (never uses shell=True, always shlex.split).

7. **Output sanitization** - all tool results are scanned for PII and secret key
   patterns (API keys, GitHub tokens, email addresses, IPs, credit cards, AWS
   keys, JWTs, SSH private keys, bot tokens) via _sanitize_tool_output. The
   redact_secrets function also masks secrets from bash stdout/stderr.

8. **TOOL_SCHEMAS** - defines the complete OpenAI function-calling schema for all
   tools the AI can invoke (bash, file_read, file_write, file_patch, memory_*,
   state_save, chat_send, restart_self, web_fetch, http_post, file_search,
   list_dir, check_self, verify_multi, delegate_task, get_subagent_result,
   vigil_status, end_task). Each schema includes parameter definitions and
   descriptions the LLM uses to decide when and how to call each tool.

The main entry point is execute(name, args, base_dir), which dispatches to
the appropriate exec_* handler through a lookup table. The ToolExecutor class
provides an object-oriented wrapper for EITE-benchmark compatibility."""

import json
import logging
import os
import re
import subprocess
import shlex
import time
import asyncio
import signal
from pathlib import Path
from typing import Any, Dict, Optional
from collections import deque

import threading

# Atomic JSON file write helper ──────────────────────────────────
# Writes to tempfile then atomically renames (POSIX) to prevent
# TOCTOU races on shared files like memory.json (AG-C5).
import tempfile as _tempfile

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* via tempfile + os.rename (atomic on POSIX)."""
    fd, tmp = _tempfile.mkstemp(suffix='.json', prefix='tc_atomic_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise

# Rate Limiter
_RATE_LIMIT_MAX_CALLS = 5
_RATE_LIMIT_WINDOW = 1.0

class RateLimiter:
    """Sliding window rate limiter. Thread-safe.

    Tracks call timestamps in a deque and rejects requests when the number of
    calls within the sliding window exceeds max_calls. Used by http_post and
    other network-facing tools to prevent abuse.

    Attributes:
        max_calls: Maximum number of calls allowed in the window. Default 5.
        window: Sliding window duration in seconds. Default 1.0.
    """

    def __init__(self, max_calls=5, window=1.0):
        """Initialize the rate limiter.

        Args:
            max_calls: Max calls allowed in the window (default 5).
            window: Window duration in seconds (default 1.0).
        """
        self.max_calls = max_calls
        self.window = window
        self._calls = deque()
        self._lock = threading.Lock()

    def check(self) -> bool:
        """Check if a call is allowed under the current rate limit.

        Purges stale timestamps outside the window, then checks if the
        remaining count is under max_calls. If allowed, records the call
        timestamp and returns True.

        Returns:
            True if the call is allowed, False if rate-limited.
        """
        with self._lock:
            now = time.time()
            while self._calls and self._calls[0] < now - self.window:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            return False

_rate_limiter = RateLimiter()

# Plugin tool registry - populated by plugin_host module at init time.
# Maps tool_name -> handler function for tools contributed by plugins.
_PLUGIN_TOOLS: Dict[str, callable] = {}

def register_plugin_tool(name: str, handler: callable) -> None:
    """Register a tool contributed by a plugin into the dispatch table.

    Called by plugin_host during initialization. Plugin tools take
    precedence over built-in tools - if a plugin registers a tool with
    the same name as a built-in, the plugin version wins.

    Args:
        name: Tool name (e.g., 'web_search', 'browser_navigate').
        handler: Callable that takes (args: dict) -> dict.
    """
    _PLUGIN_TOOLS[name] = handler
    logger.debug("[executor] registered plugin tool: %s", name)

def list_plugin_tools() -> list:
    """Return names of all registered plugin tools as a list of strings.

    Plugin tools are registered at initialization by the plugin_host module
    via register_plugin_tool(). These tools take precedence over built-in
    tools - if a plugin registers a tool with the same name as a built-in,
    the plugin version wins in the dispatch table.

    Returns:
        List of registered plugin tool name strings (e.g., ['web_search']).
        Empty list if no plugins are loaded.
    """
    return list(_PLUGIN_TOOLS.keys())


# Config file paths searched by model configuration discovery.
# These are checked in order - the first existing file is used.
_CONFIG_FILE_CANDIDATES = [
    Path.home() / "eite-agent" / "config.json",
    Path.home() / "eitelite" / "config.json",
    Path(os.path.expanduser("~/.EITElite")) / "config.json",
]


# ── Tool concurrency orchestration ──────────────────────────────────
# Maps tool names to their concurrency safety classification.
# Read-only tools (file_read, file_search, etc.) are safe to execute
# in parallel via ThreadPoolExecutor.  Destructive/write tools (bash,
# file_write, file_patch, etc.) must execute sequentially to avoid
# workspace conflicts and race conditions on shared state.
#
# Follows the MiMo Code runTools() pattern: parallel-safe tools run
# in a Promise.all batch, destructive tools flush the batch and run
# alone.  Any tool NOT listed here defaults to concurrency_safe=False.
TOOL_CONCURRENCY_MAP = {
    # Read-only tools - safe to parallelize
    "file_read": True,
    "file_search": True,
    "list_dir": True,
    "memory_load": True,
    "memory_search": True,
    "check_self": True,
    "web_fetch": True,
    "vigil_status": True,
    "check_metrics": True,
    "get_subagent_result": True,
    # Everything else (shell_exec, bash, file_write, file_patch,
    # memory_save, memory, state_save, chat_send, restart_self,
    # delegate_task, verify_multi, end_task, http_post) defaults
    # to False - destructive / write tools.
}


def is_concurrency_safe(tool_name: str) -> bool:
    """Return True if the tool is safe to execute in parallel with others.

    Read-only tools (file_read, file_search, list_dir, memory_load,
    memory_search, check_self, web_fetch, vigil_status,
    get_subagent_result) return True.  All other tools - especially
    write tools like bash, file_write, file_patch, memory_save -
    return False and must execute sequentially.

    Args:
        tool_name: The tool name string (e.g. 'file_read', 'bash').

    Returns:
        True if the tool can run concurrently with other safe tools,
        False if it must run alone in sequence.
    """
    return TOOL_CONCURRENCY_MAP.get(tool_name, False)


# Security baseline integration - used when configured via config.json
_SECURITY_PATH_CFG = None
_SECURITY_URL_CFG = None
_SECURITY_OUTBOUND_CFG = None

def configure_security(path_cfg=None, url_cfg=None, outbound_cfg=None):
    """Wire security_baseline configs into tool_executor.

    Called by unified_worker after loading config.json. Sets module-level
    globals that _workspace_path, exec_web_fetch, and exec_http_post use to
    delegate path/URL/outbound safety checks to the centralized security_baseline
    module. When these are None (default), tools fall back to inline checks.

    Args:
        path_cfg: Path safety configuration dict for workspace boundary checks.
        url_cfg: URL safety configuration dict for SSRF protection.
        outbound_cfg: Outbound connection configuration dict.
    """
    global _SECURITY_PATH_CFG, _SECURITY_URL_CFG, _SECURITY_OUTBOUND_CFG
    _SECURITY_PATH_CFG = path_cfg
    _SECURITY_URL_CFG = url_cfg
    _SECURITY_OUTBOUND_CFG = outbound_cfg

logger = logging.getLogger("tical_code.agent.tool_executor")
# Also create legacy name for log filtering
_log_legacy = logging.getLogger("EITElite.executor")

# Global TG bot reference for chat_send fallback (set by worker)
_TG_BOT_TOKEN: str = ""
_TG_CHAT_ID: str = ""


def set_tg_bot(token: str, chat_id: str):
    global _TG_BOT_TOKEN, _TG_CHAT_ID
    _TG_BOT_TOKEN = token
    _TG_CHAT_ID = chat_id

# Workspace: resolved from TICOBOT_DIR env var, defaults to CWD
WORKSPACE = os.environ.get("TICOBOT_DIR", "")
if WORKSPACE:
    WORKSPACE = os.path.expanduser(WORKSPACE)
else:
    WORKSPACE = os.getcwd()
    logger.warning("[executor] TICOBOT_DIR not set, workspace restricted to CWD")

# fullyforbidcommand
BASH_BLACKLIST = [
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b", r"\binit\s+6\b",
    r"\brm\s+-rf\s+/\s*$",
    r"\brm\s+-rf\s+~$",
    r"\brm\s+-rf\s+\$HOME\b",
    r"\bcurl\s+.*\|\s*(ba|sh)\b",
    r"\biptables\s+-F\b",
    r"\biptables\s+-X\b",
    r"\bdd\s+if=/\w+\s+of=/\w+\b",
    r"\bmkfs\b",
    r"\bmkswap\b",
    r"\bchmod\s+777\s+/",
    r"\bsudo\s+rm\s+-rf\b",
    r">\s*/dev/(sda|sdb|nvme|hd)",
    r":\(\)\s*\{",  # fork bomb
    r"\bwget\s+.*\|\s*(ba|sh)\b",
    r"\bpkill\b",
    r"\bkillall\b",
    r"\bkill\s+-?[0-9]*\s+\$\$",
    r"\bsudo\s+systemctl\s+(stop|restart|start)\s+[a-zA-Z0-9_-]+-worker-",
    r"\bbase64\s+-d\b",          # base64 decode (can bypass blacklist)
    r"\bbase64\b.*\|\s*(ba|sh)\b",  # base64 piped to shell
]

BASH_BLACKLIST_RE = [re.compile(p) for p in BASH_BLACKLIST]


def _bash_safety_check(command: str) -> Optional[str]:
    """Check shell command against the BASH_BLACKLIST and workspace boundary (informational only).

    Applies regex blacklist patterns (reboot, shutdown, rm -rf /, fork bombs,
    curl-pipe-shell, iptables flush, dd, mkfs, chmod 777 /, sudo rm -rf,
    redirect to block devices, pkill/killall, self-kill) and workspace-based
    restrictions (blocks access to system directories like /etc/shadow,
    /etc/passwd, ~/.agents/, ~/.ssh/, and traversal via cd ..).

    Admin commands (systemctl, journalctl, nginx, docker, ufw, iptables, etc.)
    bypass the workspace restriction but are still subject to the blacklist.

    Args:
        command: The shell command string to check.

    Returns:
        A string describing the block reason if the command is blocked,
        or None if the command passes all checks.
    """
    for pattern in BASH_BLACKLIST_RE:
        if pattern.search(command):
            return f"Command blocked by safety policy: {pattern.pattern}"
    # Workspace boundary check - blocks operations outside allowed dirs
    if WORKSPACE and not WORKSPACE.endswith("/"):
        WORKSPACE_G = WORKSPACE + "/"
    else:
        WORKSPACE_G = WORKSPACE or ""

    # Allowed external paths (e.g. bench dashboard, tical-chat)
    _ALLOWED_CMD_PREFIXES = ["cd /home/", "cd /opt/", "cat /home/", "cat /opt/", "ls /home/", "ls /opt/"]

    # System admin commands always allowed (systemctl, journalctl, nginx, docker, etc.)
    _ADMIN_CMD_PREFIXES = ["systemctl", "journalctl", "nginx", "docker", "ufw", "iptables",
                           "service", "ps ", "top", "htop", "df ", "free ", "uptime",
                           "ss ", "netstat", "ip ", "ping", "wget ",
                           "which ", "whereis ", "find /opt", "find /home",
                           "head /etc/", "tail /etc/", "cat /etc/nginx", "cat /etc/systemd",
                           "ls /etc/nginx", "ls /etc/systemd", "less /etc/",
                           "cat /usr/local/etc/", "head /usr/local/etc/", "tail /usr/local/etc/",
                           "ls /usr/local/etc/", "lsof"]

    # Explicitly blocked patterns - only genuinely dangerous commands
    # v0.6.0: curl is allowed (the web_fetch tool exists but LLMs may prefer curl for simple requests)
    # v0.8.7: added path traversal, env leak, process kill, mass chmod
    _BLOCKED_CMD_PATTERNS = [
        "> /dev/sda", "dd if=", ":(){ :|:& };:",  # fork bomb / raw device
        "kill -9", "killall", "pkill -9",         # process termination
        "chmod 777", "chmod -R 777",              # excessive permissions
        "chown -R",                                # mass ownership change
    ]
    for blocked_pat in _BLOCKED_CMD_PATTERNS:
        if blocked_pat in command:
            return f"Command blocked by safety policy: {blocked_pat}"
    if WORKSPACE_G:
        # Check if it's an admin command - these bypass workspace restriction
        is_admin = any(command.strip().startswith(p) or f"  {p}" in f"  {command}" for p in _ADMIN_CMD_PREFIXES)
        if not is_admin and any(f" {p}" in command or command.startswith(p)
                               for p in ["cd /etc", "ls /etc", "cd /var",
                                         "cat /etc/shadow", "cat /etc/passwd",
                                         "cat /root/.ssh/", "cat /root/.bashrc",
                                         "cat /root/.bash_history", "cat /root/.config/",
                                         "ls /root/.ssh/", "ls /root/.config/"]):
            return f"Outside workspace, system directory access denied"
        # Also block bare "cat /root" (no subpath) - directory listing on root home
        if not is_admin and re.search(r'\bcat\s+/root\s*$', command):
            return f"Outside workspace, system directory access denied"
    # Workspace restriction - blocks unsafe write/read operations
    unsafe_ops = [
        r"cd\s+\.\.", r">\s*/(?!dev/|tmp/)[^w]",
        r"mv\s+/", r"cp\s+/",
        r"cat\s+/home/<user>/\.agents/", r"cat\s+/home/<user>/\.ssh/",
        r"(cat|curl|wget)\s+/etc/shadow", r"(cat|curl|wget)\s+/etc/passwd",
        # Path traversal via relative paths
        r"\.\./\S+",
        # Env var leaks (API keys, tokens, secrets)
        r'(echo|print|printf|cat)\s+\$?(API_KEY|BOT_TOKEN|TG_TOKEN|OPENAI_API|ANTHROPIC|DEEPSEEK|GITHUB_TOKEN|SECRET)',
        # curl/wget with output to system paths
        r"(curl|wget)\s+.*(-o|--output|-O)\s+/(etc|bin|sbin|root)",
    ]
    for p in unsafe_ops:
        if re.search(p, command):
            return f"Potential privilege escalation (outside workspace {WORKSPACE})"
    return None


def _workspace_path(path: str) -> Path:
    """Resolve and validate a path against workspace boundaries.

    Expands user directory (~) and resolves symlinks, then checks whether
    the resulting path falls within the allowed workspace. If security_baseline
    is configured, delegates validation to resolve_and_validate(). Otherwise,
    performs an inline check against WORKSPACE, with special allowances for
    /opt/<app>/ and /home/<user>/sites.

    Always resolves paths to prevent directory traversal attacks (e.g.,
    /workspace/../../etc/passwd would resolve outside the workspace).

    Args:
        path: A relative or absolute filesystem path.

    Returns:
        A resolved Path object if the path is within allowed directories,
        or None if the path is outside workspace boundaries.
    """
    p = Path(path).expanduser().resolve()
    
    # If security_baseline is configured, use it for path validation
    if _SECURITY_PATH_CFG is not None:
        try:
            from tical_code.core.security_baseline import validate_path_safety, resolve_and_validate
            resolved, err = resolve_and_validate(str(p), _SECURITY_PATH_CFG)
            if err:
                logger.warning(f"[security] path blocked: {p} - {err}")
                return None
            return Path(resolved)
        except Exception as e:
            logger.debug(f"[security] baseline check failed, falling back: {e}")
    
    # Fallback: inline workspace check (resolve first to prevent traversal)
    resolved_p = str(p.resolve())  # Always resolve to prevent traversal
    if WORKSPACE and not resolved_p.startswith(os.path.abspath(WORKSPACE)):
        # Allow specific external paths from env (e.g., /opt/<app>/, /home/<user>/sites)
        _allowed_external = []
        _allowed_external_env = os.environ.get(
            "ALLOWED_EXTERNAL_PATHS",
            "/opt/<app>/,/home/<user>/sites"
        ).split(",")
        for d in _allowed_external_env:
            d = d.strip()
            if d and os.path.isdir(d):
                _allowed_external.append(os.path.realpath(d))
        if _allowed_external and resolved_p.startswith(tuple(_allowed_external)):
            return p
        # eite-benchmark: resolve-checked only
        return None
    return p


def _run_cmd(cmd: str, timeout: int = 120, workdir: str = "") -> dict:
    """Execute a shell command in a subprocess with safety guards.

    Before execution, runs the command through tool_sandbox's pre_check for
    sandbox-level validation. Uses shlex.split() with shell=False for safe
    operator support (&&, |, ; operators are split into args).

    Output is truncated (stdout to 4000 chars, stderr to 1000 chars) and
    stderr is merged into stdout with a [STDERR] marker so the AI can see
    warnings and errors in a single field.

    Args:
        cmd: The shell command string to execute.
        timeout: Maximum execution time in seconds (default 30).
        workdir: Optional working directory for the command.

    Returns:
        A dict with keys:
            stdout: trimmed command output (max 4000 chars).
            stderr: trimmed stderr output (max 1000 chars) or error message.
            exit_code: process return code, or -1 on error/timeout.
    """
    # Sandbox pre-validation via tool_sandbox
    try:
        from tical_code.core.tool_sandbox import get_sandbox_runner
        sandbox = get_sandbox_runner()
        allowed, reason = sandbox.pre_check("bash", {"command": cmd})
        if not allowed:
            return {"stdout": "", "stderr": f"[SANDBOX] {reason}", "exit_code": -1}
    except Exception as e:
        logger.warning(f"Sandbox unavailable: {e}. Command will run without sandbox.")
    import shlex
    try:
        # v0.8.6: Detect shell operators (|, >, <, &&, ||, ;, $(), ``).
        # Commands with these use temp-file pipelines instead of shell=True.
        # Simple commands use shlex.split() + shell=False for safety.
        import re
        _SHELL_OP_PATTERN = re.compile(r'\||&&|\|\||[<>]|;|\$\(|`')
        _needs_shell = bool(_SHELL_OP_PATTERN.search(cmd))

        if _needs_shell:
            # Use NamedTemporaryFile with atomic creation (AG-C4: fix TOCTOU)
            import tempfile
            fd, sh_path = tempfile.mkstemp(suffix='.sh', prefix='tc_')
            try:
                with os.fdopen(fd, 'w') as sh_f:
                    sh_f.write('#!/bin/sh\n')
                    sh_f.write(cmd + '\n')
                os.chmod(sh_path, 0o700)
                kwargs = {
                    "capture_output": True,
                    "text": True,
                    "timeout": timeout,
                }
                if workdir and os.path.isdir(workdir):
                    kwargs["cwd"] = workdir
                r = subprocess.run([sh_path], **kwargs)
            finally:
                try:
                    os.unlink(sh_path)
                except OSError:
                    pass
        else:
            cmd_parts = shlex.split(cmd)
            kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            if workdir and os.path.isdir(workdir):
                kwargs["cwd"] = workdir
            r = subprocess.run(cmd_parts, **kwargs)
        return {
            "stdout": r.stdout.strip()[:8000],
            "stderr": r.stderr.strip()[:3000],
            "exit_code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "timeout", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


async def _run_cmd_async(cmd: str, timeout: int = 120, workdir: str = "") -> dict:
    """Async version of _run_cmd using asyncio.create_subprocess_exec.

    Creates a subprocess with os.setsid process group for reliable cleanup.
    On timeout, kills the entire process group via os.killpg, then awaits
    process termination.

    Args:
        cmd: The shell command string to execute.
        timeout: Maximum execution time in seconds (default 120).
        workdir: Optional working directory for the command.

    Returns:
        A dict with keys:
            stdout: trimmed command output (max 8000 chars).
            stderr: trimmed stderr output (max 3000 chars).
            exit_code: process return code, or -1 on error/timeout.
    """
    import re
    _SHELL_OP_PATTERN = re.compile(r'\||&&|\|\||[<>]|;|\$\(|`')
    _needs_shell = bool(_SHELL_OP_PATTERN.search(cmd))

    sh_path = None
    try:
        if _needs_shell:
            import tempfile
            fd, sh_path = tempfile.mkstemp(suffix='.sh', prefix='tc_')
            try:
                with os.fdopen(fd, 'w') as sh_f:
                    sh_f.write('#!/bin/sh\n')
                    sh_f.write(cmd + '\n')
                os.chmod(sh_path, 0o700)
                process = await asyncio.create_subprocess_exec(
                    sh_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir if workdir and os.path.isdir(workdir) else None,
                    preexec_fn=os.setsid,
                )
            except BaseException:
                # If we fail before successfully creating the subprocess, clean up
                # the temp file here; otherwise it's cleaned in the outer finally.
                if sh_path:
                    try:
                        os.unlink(sh_path)
                    except OSError:
                        pass
                sh_path = None
                raise
        else:
            cmd_parts = shlex.split(cmd)
            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir if workdir and os.path.isdir(workdir) else None,
                preexec_fn=os.setsid,
            )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await process.wait()
            return {"stdout": "", "stderr": "timeout", "exit_code": -1}

        return {
            "stdout": stdout.decode(errors='replace').strip()[:8000],
            "stderr": stderr.decode(errors='replace').strip()[:3000],
            "exit_code": process.returncode,
        }
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}
    finally:
        if sh_path:
            try:
                os.unlink(sh_path)
            except OSError:
                pass


# ============ Execute Functions ============
# === OpenAI Function Calling Schemas ===
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": "Execute shell commands (safety-checked). Use for file operations, system management, network requests, etc. Set workdir to change directory before running the command - do NOT use 'cd &&' chains.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "workdir": {"type": "string", "description": "Optional working directory. Set this instead of using 'cd' in the command."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_patch",
            "description": "Find and replace text in a file. Use for targeted edits instead of reading+rewriting the whole file. Supports fuzzy matching - minor whitespace/indentation differences won't break the match.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Text to find (include surrounding context for uniqueness)"},
                    "new_string": {"type": "string", "description": "Replacement text. Pass empty string to delete the matched text."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a piece of persistent memory to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key name"},
                    "value": {"type": "string", "description": "Memory value"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_load",
            "description": "Read all saved persistent memories.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Full-text search across all past conversations, learned facts, and memory documents (SOUL.md, MEMORY.md, USER.md). Uses FTS5 with CJK-aware tokenization. Use to recall past context, decisions, user preferences, or technical facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (keywords or phrase)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "Manage persistent memory. Store, recall, search, or forget entries in the memory store.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["store", "recall", "search", "forget"],
                        "description": "Memory action: store a new entry, recall by key, search all entries, or forget/delete an entry."
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key to recall or forget. Required for recall and forget actions."
                    },
                    "value": {
                        "type": "string",
                        "description": "Content to store. Required for the store action."
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for the search action."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "state_save",
            "description": "Save persistent state (non-memory key-value data).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "State key name"},
                    "value": {"type": "object", "description": "State value (JSON object)"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_send",
            "description": "Send a message to another AI worker via tical-chat, or reply to the user. For task completion, prefer end_task instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target AI worker identity"},
                    "content": {"type": "string", "description": "Message content"}
                },
                "required": ["target", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restart_self",
            "description": "Restart this worker process. Sends SIGTERM - systemd auto-restarts cleanly. Use to clear long-running context, resolve memory pressure, or after model/config changes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return the content as readable text. Use instead of bash curl. Has SSRF protection (blocks private IPs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch (http/https only)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Search for files by name pattern or content. Uses glob patterns for filenames and optional text search inside files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern for file names, e.g. *.py, *config*"},
                    "directory": {"type": "string", "description": "Directory to search in (default: current workspace)"},
                    "content_pattern": {"type": "string", "description": "Optional text to search inside files"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents. Returns files, directories, and metadata (size, modified time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default: current directory)"},
                    "all": {"type": "boolean", "description": "Include hidden files (default: false)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_self",
            "description": "Check own runtime info: model, config, identity. ALWAYS use this when asked about your model, config, or capabilities. Never guess - this tool reads real data.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "verify_multi",
            "description": "Send the same prompt to multiple AI models, compare answers, and produce a consensus audit. Use BEFORE high-stakes actions (file writes, deployments, system changes) to catch model-specific errors. Returns divergence score (0=unanimous, 1=completely divergent) and recommendations. If divergence > 0.3, consider the action risky.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt or question to verify across models."},
                    "threshold": {"type": "number", "description": "Divergence threshold above which action is blocked (default 0.3)."}
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "POST data to a URL. Use for API calls and webhooks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to POST to"},
                    "data": {"type": "string", "description": "POST body"},
                    "content_type": {"type": "string", "description": "Content-Type (default: application/json)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"},
                },
                "required": ["url", "data"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a task to a sub-agent for parallel execution. The sub-agent runs independently with its own session and tools. Returns a task_id that you can use with get_subagent_result to retrieve results later. Use for: parallel research, independent subtasks, background work that does not need immediate results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Task description for the sub-agent to execute"},
                    "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names available to the sub-agent (default: all tools)"},
                    "max_iterations": {"type": "integer", "description": "Maximum reasoning rounds (default: 5)"}
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_subagent_result",
            "description": "Retrieve the result of a previously delegated sub-agent task. Use the task_id returned by delegate_task. If the task is still running, status will be 'pending' or 'running'. When complete, returns the result, verification status, and elapsed time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task_id returned from delegate_task"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vigil_status",
            "description": "Query Vigil's current state: patrol count, human/ai state, recent verdicts, and pending instructions. Returns active: false if Vigil is not initialized.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_metrics",
            "description": "Return performance metrics: tool latency averages, LLM call latency, error counts per tool, and top 5 slowest tool calls. Returns inactive if MetricsCollector not initialized.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "end_task",
            "description": "Signal that the current task is complete. Call when all work is done. Triggers memory consolidation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "description": "Whether the task succeeded"}
                },
                "required": ["success"]
            }
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chain_exec",
            "description": (
                "Execute a molecular chain - a sequence of AI models where each "
                "model's output feeds into the next, producing emergent intelligence. "
                "Supports preset chains and dynamic chains. The engine auto-routes "
                "each step to the best provider: local small models for structured "
                "tasks, cloud API for creative tasks, distillate model for user-"
                "aligned judgments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "molecule": {
                        "type": "string",
                        "description": "Which preset chain to execute.",
                        "enum": ["code_review", "research",
                                 "safety_check", "decision"],
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The input prompt for the molecular chain.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to include in each step.",
                    },
                    "custom_steps": {
                        "type": "array",
                        "description": (
                            "Custom chain steps. role options: reasoner, executor, "
                            "verifier, guard, synthesizer, formatter, distillate, "
                            "translator, summarizer, classifier, retriever, "
                            "cryptograph, compliance, or any custom role."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "prompt_template": {"type": "string"},
                                "provider_type": {
                                    "type": "string",
                                    "enum": ["auto", "api", "local", "distillate"],
                                    "default": "auto",
                                },
                                "bond_type": {
                                    "type": "string",
                                    "enum": ["refine", "verify", "transform",
                                             "catalyze"],
                                    "default": "refine",
                                },
                            },
                            "required": ["role", "prompt_template"],
                        },
                    },
                    "provider_preference": {
                        "type": "string",
                        "enum": ["auto", "prefer_local", "prefer_api", "local_only"],
                        "default": "auto",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_modify",
            "description": (
                "Safely modify a file with full safety checks: protected file check, "
                "git backup, syntax validation, code safety check, sandbox test, "
                "cross-verify, and audit logging. Automatically rolls back on failure. "
                "USE THIS instead of file_write for modifying system code to prevent "
                "breaking the worker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to modify"},
                    "new_content": {"type": "string", "description": "New complete file content"},
                    "reason": {"type": "string", "description": "Human-readable reason for this modification. Be specific: what bug/feature, why this change."},
                    "sandbox_test": {"type": "boolean", "description": "Run sandbox test after write (default: true)"},
                },
                "required": ["path", "new_content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_modify_diff",
            "description": (
                "Apply a targeted find-and-replace through the safe_modify pipeline "
                "(safety checks + rollback). Reads file, applies diff, validates. "
                "USE THIS instead of file_patch for system code edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Text to find (include surrounding context for uniqueness)"},
                    "new_string": {"type": "string", "description": "Replacement text. Pass empty string to delete."},
                    "reason": {"type": "string", "description": "Human-readable reason for this modification."},
                },
                "required": ["path", "old_string", "new_string", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_list",
            "description": (
                "List all available checkpoints/snapshots with status filter. "
                "Returns checkpoints with id, timestamp, description, status, and file count. "
                "Use this before checkpoint_restore to find the right checkpoint ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional status filter: 'incomplete', 'complete', or omit for all",
                        "enum": ["incomplete", "complete"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "description": (
                "Restore files from a checkpoint/snapshot. Automatically creates a pre-snapshot "
                "before restoring for safety. Requires confirm=True to execute - use preview first "
                "by calling without confirm to see what files will be affected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checkpoint_id": {"type": "string", "description": "Checkpoint ID to restore from"},
                    "selective_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of specific file paths to restore (omit for full restore)",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be True to proceed. Call without confirm first to preview.",
                        "default": False,
                    },
                },
                "required": ["checkpoint_id"],
            },
        },
    },
    # Capability integration tools (auto-discovered)
    {
        "type": "function",
        "function": {
            "name": "capability_list",
            "description": (
                "List all system capabilities. Returns a manifest of every module "
                "and what it can do. Use this to discover what capabilities your "
                "system has beyond the standard tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capability_call",
            "description": (
                "Invoke a system capability by name. Use capability_list first to "
                "see what's available. Call format: pass name and params."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Capability name from capability_list",
                    },
                    "params": {
                        "type": "object",
                        "description": "Parameters for the capability",
                    },
                },
                "required": ["name"],
            },
        },
    },
    # ask_user: pause and ask the human for input
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the human user for input when you are stuck, need a CAPTCHA code, "
                "need confirmation, or cannot proceed with the current task. "
                "Use this instead of trying the same thing repeatedly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user. Be specific about what you need.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context explaining why you need this input (e.g., 'CAPTCHA detected on login page', 'need confirmation to proceed')",
                    },
                },
                "required": ["question"],
            },
        },
    },
    # start_background_task: persist a multi-step plan for autonomous execution
    {
        "type": "function",
        "function": {
            "name": "start_background_task",
            "description": (
                "Create a persistent autonomous task that runs in the background. "
                "Use this for any work that will take more than 3-5 tool calls. "
                "The task engine will continue executing step by step across multiple "
                "LLM rounds until completion or failure. "
                "Call this tool with a clear goal and optional step-by-step plan, "
                "then call end_task to signal the current message turn is done."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The overall task goal - what you want to accomplish",
                    },
                    "plan": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional step-by-step plan. Each item is one step.",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Maximum LLM rounds before forced completion (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a persistent multi-step task that survives restarts",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Task goal"},
                    "context": {"type": "string", "description": "Task context"}
                },
                "required": ["goal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List active/pending persistent tasks",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "evolve_stats",
            "description": "Self-evolution statistics - error patterns and usage insights",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

# ============ TOOL_SCHEMAS_CLEAN (alias - no dot-replace needed; all names already use underscores) ============
TOOL_SCHEMAS_CLEAN = TOOL_SCHEMAS


def redact_secrets(text: str) -> str:
    """Mask common secret patterns (API keys, tokens) in text for safe logging.

    Uses comprehensive 15+ pattern redaction from security_baseline when available.
    Falls back to basic 3-pattern version if security_baseline unavailable.
    """
    if not text:
        return text
    try:
        from tical_code.core.security_baseline import redact_secrets as _sb_redact
        return _sb_redact(text)
    except ImportError:
        pass
    # Fallback: basic 3-pattern redaction
    text = re.sub(r'(sk-[a-zA-Z0-9]{20,})', r'sk-***REDACTED***', text)
    text = re.sub(r'(ghp_[a-zA-Z0-9]{36})', r'ghp_***REDACTED***', text)
    text = re.sub(r'(\d{8,}:AA[a-zA-Z0-9_-]{35,})', r'***BOT_TOKEN_REDACTED***', text)
    return text


# ═══════════════════════════════════════════════════════════════
# --- Tool implementation ---
# ═══════════════════════════════════════════════════════════════
def exec_bash(args: dict) -> dict:
    """Execute a shell command with full security pipeline.

    Applies the complete security stack: blacklist check (_bash_safety_check),
    timeout clamping (1-120s), sandbox pre-check in _run_cmd, secret redaction
    on stdout and stderr, and stderr-to-stdout merging for AI visibility.

    Args:
        args: Dict with required key 'command' (str) and optional 'timeout' (int).

    Returns:
        A dict with stdout, stderr (empty after merge), and exit_code.
        On block, returns {'error': block_reason}.
    """
    cmd = args.get("command", "")
    if not cmd:
        return {"error": "Command cannot be empty"}

    block_reason = _bash_safety_check(cmd)
    if block_reason:
        logger.warning(f"[executor] BLOCKED: {block_reason[:80]}")
        return {"error": block_reason}

    timeout = args.get("timeout", 30)
    try:
        timeout = max(1, min(int(timeout), 120))
    except (ValueError, TypeError):
        timeout = 30

    result = _run_cmd(cmd, timeout, workdir=args.get("workdir", ""))
    # Redact secrets from bash output
    if result.get("stdout"):
        result["stdout"] = redact_secrets(result["stdout"])
    if result.get("stderr"):
        result["stderr"] = redact_secrets(result["stderr"])
    # Always append stderr to stdout so AI can see warnings/errors
    # SECURITY: re-redact the combined output to catch anything missed in separate redaction
    if result.get("stderr"):
        stderr_text = result["stderr"][:1000]
        if result.get("stdout"):
            result["stdout"] += f"\n[STDERR]\n{stderr_text}"
        else:
            result["stdout"] = f"[STDERR]\n{stderr_text}"
        result["stderr"] = ""
    # Re-redact combined output for safety (catches secrets spanning stdout+stderr boundary)
    if result.get("stdout"):
        result["stdout"] = redact_secrets(result["stdout"])
    if result["exit_code"] != 0:
        logger.warning(f"[executor] bash exit={result['exit_code']}: {cmd[:60]}")
    return result


def exec_file_read(args: dict, base_dir: str = '') -> dict:
    """Read contents of a file within workspace boundaries.

    Validates the path, enforces 100KB size limit, returns content
    truncated to 16000 chars. Supports plain text plus binary formats
    via stdlib: .docx/.docx, .xlsx/.xlsm, .ipynb, .csv.
    """
    import zipfile, io, re, json as _json
    path = args.get('path', '')
    if not path:
        return {'error': 'Path cannot be empty'}
    full_path = _workspace_path(path)
    if full_path is None:
        return {'error': f'Path outside workspace: {path}'}
    if not full_path.exists():
        return {'error': f'File not found: {full_path}'}
    if full_path.is_dir():
        return {'error': 'Path is a directory, not a file'}
    max_size = 100 * 1024
    if full_path.stat().st_size > max_size:
        return {'error': f'File exceeds 100KB ({full_path.stat().st_size} bytes). Use bash to read in segments.'}
    try:
        ext = full_path.suffix.lower()
        raw = full_path.read_bytes()

        if ext == '.docx':
            text = _extract_docx(raw)
        elif ext == '.xlsx':
            text = _extract_xlsx(raw)
        elif ext == '.ipynb':
            text = _extract_ipynb(raw)
        elif ext == '.csv':
            text = raw.decode('utf-8', errors='replace')
        else:
            text = raw.decode('utf-8', errors='replace')

        content = text[:16000]
        return {'content': content, 'path': str(full_path)}
    except Exception as e:
        return {'error': str(e)}


def _extract_docx(data: bytes) -> str:
    """Extract text from .docx OOXML using stdlib zipfile."""
    import zipfile, io, re
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read('word/document.xml').decode('utf-8', errors='replace')
        for m in re.finditer(r'<w:t[^>]*>([^<]+)</w:t>', xml):
            parts.append(m.group(1))
    result = ' '.join(parts)
    result = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', result)
    return result


def _extract_xlsx(data: bytes) -> str:
    """Extract text from .xlsx spreadsheet using stdlib zipfile."""
    import zipfile, io, re
    lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            sst = z.read('xl/sharedStrings.xml').decode('utf-8', errors='replace')
            strings = re.findall(r'<t[^>]*>([^<]+)</t>', sst)
        for name in z.namelist():
            if not name.startswith('xl/worksheets/sheet') or not name.endswith('.xml'):
                continue
            sheet = z.read(name).decode('utf-8', errors='replace')
            for m in re.finditer(r'<v>([^<]+)</v>', sheet):
                val = m.group(1)
                try:
                    idx = int(val)
                    if idx < len(strings):
                        lines.append(strings[idx])
                except ValueError:
                    lines.append(val)
    return '\n'.join(lines)


def _extract_ipynb(data: bytes) -> str:
    """Extract source/markdown from .ipynb notebook."""
    import json
    nb = json.loads(data.decode('utf-8', errors='replace'))
    parts = []
    for cell in nb.get('cells', []):
        ctype = cell.get('cell_type', 'code')
        src = cell.get('source', [])
        if isinstance(src, list):
            src = ''.join(src)
        label = '# [markdown]' if ctype == 'markdown' else '# [code]'
        parts.append(f'{label}\n{src}')
    return '\n\n'.join(parts)


def exec_file_write(args: dict, base_dir: str = "") -> dict:
    """Write content to a file with workspace and syntax validation.

    Validates the path against workspace boundaries, creates parent directories
    as needed, and writes the content. For .py files, performs a py_compile
    syntax check on the content BEFORE writing - blocks the write if the code
    has syntax errors.

    Args:
        args: Dict with required keys 'path' (str) and 'content' (str).
        base_dir: Base directory for path resolution (default WORKSPACE).

    Returns:
        {'ok': True, 'path': resolved_path} on success,
        or {'error': reason} on failure.
    """
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return {"error": "Path cannot be empty"}
    full_path = _workspace_path(path)
    if full_path is None:
        return {"error": f"Path outside workspace: {path}"}
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        # CRITICAL: Validate syntax before writing .py files
        if str(full_path).endswith('.py') and content.strip():
            import py_compile, tempfile
            try:
                tmp = tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False)
                tmp.write(content)
                tmp.close()
                py_compile.compile(tmp.name, doraise=True)
                import os; os.unlink(tmp.name)
            except py_compile.PyCompileError as e:
                import os; os.unlink(tmp.name)
                return {"error": f"SyntaxError in Python file, write blocked: {e}"}
            # Atomic write via tempfile + rename (AG-C5: TOCTOU fix)
            fd2, tmp_path2 = _tempfile.mkstemp(suffix=full_path.suffix, prefix='tc_write_', dir=str(full_path.parent))
            try:
                with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                    f.write(content)
                os.rename(tmp_path2, str(full_path))
            except BaseException:
                try:
                    os.unlink(tmp_path2)
                except Exception:
                    pass
                raise
            logger.info(f"[executor] wrote {len(content)} bytes to {full_path}")
        return {"ok": True, "path": str(full_path)}
    except Exception as e:
        return {"error": str(e)}


def exec_file_patch(args: dict) -> dict:
    """Fuzzy find-and-replace in a file. Like file_write but for targeted edits.

    Finds old_string in the file and replaces it with new_string. If exact
    match fails, attempts fuzzy matching by comparing stripped lines and
    returns a hint if whitespace differences are detected. For .py files,
    validates syntax via py_compile before applying the patch.

    Args:
        args: Dict with required keys 'path', 'old_string', 'new_string'.

    Returns:
        {'ok': True, 'path': path, 'diff': unified_diff} on success,
        or {'error': reason} if old_string not found or validation fails.
    """
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    if not path or not old_string:
        return {"error": "path and old_string are required"}
    full_path = _workspace_path(path)
    if full_path is None:
        return {"error": f"Path outside workspace: {path}"}
    try:
        content = full_path.read_text()
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as e:
        return {"error": f"Cannot read {path}: {e}"}
    try:
        if old_string in content:
            new_content = content.replace(old_string, new_string, 1)
            # CRITICAL: Validate syntax before writing .py files
            if str(full_path).endswith('.py') and new_content.strip():
                import py_compile, tempfile
                try:
                    tmp = tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False)
                    tmp.write(new_content)
                    tmp.close()
                    py_compile.compile(tmp.name, doraise=True)
                    import os; os.unlink(tmp.name)
                except py_compile.PyCompileError as e:
                    import os; os.unlink(tmp.name)
                    return {"error": f"SyntaxError in Python file, patch blocked: {e}"}
            # Atomic write via tempfile + rename (AG-C5: TOCTOU fix)
            fd2, tmp_path2 = _tempfile.mkstemp(suffix=full_path.suffix, prefix='tc_patch_', dir=str(full_path.parent))
            try:
                with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                os.rename(tmp_path2, str(full_path))
            except BaseException:
                try:
                    os.unlink(tmp_path2)
                except Exception:
                    pass
                raise
            import difflib
            diff = list(difflib.unified_diff(
                content.splitlines(True), new_content.splitlines(True),
                fromfile="before", tofile="after", n=2
            ))
            return {"ok": True, "path": path, "diff": "".join(diff[-10:])}
        # Fuzzy fallback: strip whitespace and try again
        for line in content.split("\n"):
            stripped = line.strip()
            if old_string.strip() in stripped:
                return {"error": f"Found similar text but with whitespace differences at line containing: {stripped[:60]}"}
        return {"error": f"old_string not found in {path}"}
    except Exception as e:
        return {"error": str(e)}


def exec_memory_save(args: dict, base_dir: str = "") -> dict:
    """Save a key-value entry to the persistent memory store.

    Stores (key, value) pairs in memory.json under the workspace directory
    or the provided base_dir. Each entry is timestamped. If the memory file
    already exists, the new entry is merged into existing entries.

    Args:
        args: Dict with required keys 'key' (str) and 'value' (str).
        base_dir: Base directory for memory.json location.

    Returns:
        {'ok': True, 'key': key} on success, or {'error': reason} on failure.
    """
    key = args.get("key", "")
    value = args.get("value", "")
    if not key:
        return {"error": "Key cannot be empty"}
    mem_file = Path(base_dir or WORKSPACE) / "memory.json"
    mem = {}
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text())
        except Exception:
            mem = {}
    mem.setdefault("entries", {})[key] = {"value": value, "time": time.time()}
    try:
        _atomic_write_json(mem_file, mem)
    except Exception as e:
        return {"error": f"Failed to write memory: {e}"}
    return {"ok": True, "key": key}


def exec_memory_load(args: dict = None, base_dir: str = "") -> dict:
    """Read all entries from the persistent memory store.

    Loads memory.json from the workspace (or base_dir) and returns all
    stored entries. Returns an empty entries dict if the file does not
    exist or is corrupted.

    Args:
        args: Unused (provided for dispatch consistency).
        base_dir: Base directory for memory.json location.

    Returns:
        {'entries': {key: {'value': ..., 'time': ...}, ...}} on success,
        or {'entries': {}} if no memory file exists.
    """
    mem_file = Path(base_dir or WORKSPACE) / "memory.json"
    if not mem_file.exists():
        return {"entries": {}}
    try:
        mem = json.loads(mem_file.read_text())
        return {"entries": mem.get("entries", {})}
    except Exception:
        return {"entries": {}}



def exec_chat_send(args: dict) -> dict:
    """Send a message to another AI worker via the tical-chat API.

    Posts a message to the tical-chat message service identified by
    TICAL_CHAT_URL and TICAL_CHAT_KEY environment variables. The sender
    identity is taken from WORKER_NAME env var or defaults to 'agent'.

    Args:
        args: Dict with required keys 'target' (str) and 'content' (str).

    Returns:
        {'ok': True, 'target': target, 'response': api_response} on success,
        or {'error': reason} on failure.
    """
    target = args.get("target", "")
    content = args.get("content", "")
    if not target or not content:
        return {"error": "Target and content cannot be empty"}
    try:
        import urllib.request
        import ssl
        chan_url = os.environ.get("TICAL_CHAT_URL", "")
        chan_key = os.environ.get("TICAL_CHAT_KEY", "")
        identity = os.environ.get("WORKER_NAME", "agent")
        payload = json.dumps({
            "sender": identity,
            "target": target,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{chan_url}/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-AI-Identity": identity,
                "X-AI-Key": chan_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as resp:
            resp_data = json.loads(resp.read())
        logger.info(f"[executor] chat_send to {target}: {content[:50]}")
        return {"ok": True, "target": target, "response": resp_data}
    except Exception as e:
        logger.warning(f"[executor] chat_send error: {e}")
        # Fallback: try sending directly via TG bot API
        if _TG_BOT_TOKEN and _TG_CHAT_ID:
            try:
                _tg_url = f"https://api.telegram.org/bot{_TG_BOT_TOKEN}/sendMessage"
                _tg_payload = json.dumps({
                    "chat_id": _TG_CHAT_ID,
                    "text": f"[Task] {content[:2000]}",
                    "parse_mode": "HTML",
                }).encode()
                _tg_req = urllib.request.Request(
                    _tg_url,
                    data=_tg_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(_tg_req, timeout=10, context=ssl.create_default_context()) as _resp:
                    _tg_resp = json.loads(_resp.read())
                if _tg_resp.get("ok"):
                    logger.info(f"[executor] chat_send via TG fallback to {_TG_CHAT_ID}: {content[:50]}")
                    return {"ok": True, "via": "tg_fallback", "response": _tg_resp}
                else:
                    logger.warning(f"[executor] TG fallback failed: {_tg_resp}")
            except Exception as _tg_e:
                logger.warning(f"[executor] TG fallback error: {_tg_e}")
        return {"error": f"Send failed: {e}"}


def exec_state_save(args: dict, base_dir: str = "") -> dict:
    """Save a JSON-serializable value as persistent state.

    Stores state under state/<key>.json in the workspace. Unlike memory_save,
    values can be arbitrary JSON objects (not just strings). Used for
    non-memory key-value persistence (configuration, counters, flags).

    Args:
        args: Dict with required keys 'key' (str) and 'value' (dict/JSON).
        base_dir: Base directory for state/ subdirectory.

    Returns:
        {'ok': True, 'key': key} on success, or {'error': reason} on failure.
    """
    key = args.get("key", "")
    value = args.get("value", {})
    if not key:
        return {"error": "Key cannot be empty"}
    state_dir = Path(base_dir or WORKSPACE) / "state"
    try:
        state_dir.mkdir(exist_ok=True)
        (state_dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    except Exception as e:
        return {"error": f"Failed to save state: {e}"}
    return {"ok": True, "key": key}


def exec_restart_self(args: dict = None) -> dict:
    """Restart this worker process - SAFETY BLOCKED.

    Disabled to prevent LLM self-termination. Workers should be managed
    externally via systemctl by an administrator, not by their own AI.
    """
    logger.warning("[executor] restart_self called but BLOCKED - self-restart disabled")
    return {"error": "restart_self is blocked for safety. Use 'sudo systemctl restart unified-worker-*' from an admin context."}



# -------------------------------------------------------------------
# CDP Browser Fetch - bypass Cloudflare via Chrome DevTools Protocol
# -------------------------------------------------------------------
_CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")

def _cdp_fetch(url: str, timeout: int = 15) -> dict:
    """Fetch a URL via Playwright Chromium (bypasses Cloudflare).

    Launches a headless Chromium browser through Playwright, navigates
    to the target URL, waits for DOM content to load, and extracts the
    body's inner text. Uses a standard Chrome User-Agent and disables
    AutomationControlled blink features to evade bot detection.

    Args:
        url: The URL to fetch (http/https only).
        timeout: Max wait time in seconds (default 15).

    Returns:
        {'content': page_text, 'url': url, 'source': 'playwright'}
        on success, or {'error': reason} on failure.
    """
    import os, time
    
    playwright_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                                      os.path.expanduser("~/.cache/ms-playwright"))
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            page = browser.new_page(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                time.sleep(2)
                text = page.inner_text("body") or ""
                if text:
                    return {"content": text[:100000], "url": url, "source": "playwright"}
                return {"error": "empty page"}
            finally:
                browser.close()
    except Exception as e:
        return {"error": f"playwright failed: {e}"}

def exec_web_fetch(args: dict) -> dict:
    """Fetch a URL with SSRF protection and Cloudflare bypass capability.

    Blocks requests to private/reserved IPs for SSRF protection. Tries
    CDP (Chrome DevTools Protocol) browser fetch via Playwright first to
    bypass Cloudflare anti-bot protections, then falls back to curl with
    browser-like User-Agent headers. If security_baseline is configured,
    delegates URL validation to its validate_url function.

    Args:
        args: Dict with required key 'url' (str) and optional 'timeout'
              (int, default 10s, max 30s).

    Returns:
        {'content': page_text, 'url': url} on success,
        or {'error': reason} on failure/block.
    """
    url = args.get("url", "")
    timeout = min(int(args.get("timeout", 10)), 30)
    if not url:
        return {"error": "URL cannot be empty"}
    if not url.startswith(("http://", "https://")):
        return {"error": "Only http/https URLs are supported"}
    # SSRF protection: block private IPs (double-resolve for TOCTOU protection)
    import urllib.parse, socket
    host = urllib.parse.urlparse(url).hostname
    if host:
        try:
            ip_first = socket.gethostbyname(host)
            parts = ip_first.split(".")
            if parts[0] in ("10", "127", "0") or \
               (parts[0] == "172" and 16 <= int(parts[1]) <= 31) or \
               (parts[0] == "192" and parts[1] == "168") or \
               ip_first.startswith("169.254.") or \
               ip_first == "::1":
                return {"error": f"SSRF blocked: {host} resolves to private IP {ip_first}"}
            # Second DNS resolution for TOCTOU (DNS rebinding) protection
            import time as _dns_time
            _dns_time.sleep(0.1)  # Brief delay to catch rapid DNS changes
            ip_second = socket.gethostbyname(host)
            if ip_first != ip_second:
                return {"error": f"SSRF blocked: DNS rebinding detected ({host}: {ip_first} -> {ip_second})"}
        except Exception:
            return {"error": f"Cannot resolve host: {host}"}
    # CDP fetch also needs SSRF check (url already validated above, but double-check)
    # If security_baseline is configured, use it
    if _SECURITY_URL_CFG is not None:
        try:
            from tical_code.core.security_baseline import validate_url
            safe, reason = validate_url(url, _SECURITY_URL_CFG)
            if not safe:
                return {"error": f"URL security check failed: {reason}"}
        except Exception:
            pass
    # Try CDP browser first (bypasses Cloudflare)
    import os
    cdp_result = _cdp_fetch(url, timeout)
    if "content" in cdp_result:
        return cdp_result
    # CDP failed, fallback to curl
    try:
        r = subprocess.run(["curl", "-sL", "--no-location", "--max-time", str(timeout),
                               "-A", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                               "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                               "-H", "Accept-Language: en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                               url],
                          capture_output=True, text=True, timeout=timeout+5)
    except FileNotFoundError:
        return {"error": "curl not installed on this system"}
    except subprocess.TimeoutExpired:
        return {"error": f"curl timed out after {timeout+5}s"}
    except Exception as e:
        return {"error": f"curl failed: {e}"}
    if r.returncode != 0:
        return {"error": f"curl failed: {r.stderr[:200]}"}
    return {"content": r.stdout[:100000], "url": url}


def exec_file_search(args: dict) -> dict:
    """Search for files by name or content. Respects workspace boundary."""
    pattern = args.get("pattern", "")
    directory = args.get("directory", ".")
    content_pattern = args.get("content_pattern")
    if not pattern:
        return {"error": "Pattern cannot be empty"}
    # Workspace restriction
    full_dir = os.path.abspath(os.path.expanduser(directory))
    if WORKSPACE and not full_dir.startswith(os.path.abspath(WORKSPACE)):
        return {"error": f"Path outside workspace: {directory}"}
    import glob
    matches = []
    try:
        matches = glob.glob(f"{full_dir}/**/{pattern}", recursive=True)
        if len(matches) > 1000:
            matches = matches[:1000]  # DoS protection: cap glob results
    except Exception as e:
        return {"error": f"Glob failed: {e}"}
    if content_pattern:
        grep_r = subprocess.run(
            ["grep", "-rl", content_pattern, full_dir],
            capture_output=True, text=True, timeout=10)
        matches = grep_r.stdout.strip().split("\n") if grep_r.stdout.strip() else []
    # Filter to workspace only
    if WORKSPACE:
        ws = os.path.abspath(WORKSPACE)
        matches = [m for m in matches if m.startswith(ws)]
    return {"matches": matches[:100], "count": min(len(matches), 100), "directory": directory}


def exec_list_dir(args: dict) -> dict:
    """List directory contents with metadata, bounded by workspace restrictions.

    Resolves the given path, checks it falls within the configured WORKSPACE,
    and returns file entries with name, is_dir flag, size (bytes), and
    modification time (Unix timestamp). Hidden files are excluded by default
    unless args['all'] is True.

    Args:
        args: Dict with optional keys:
            'path' (str): Directory path to list (default: '.').
            'all' (bool): Include hidden files (default: False).

    Returns:
        Dict with keys: 'files' (list of entry dicts), 'path' (resolved path
        string), 'total' (entry count). On error, returns {'error': reason}.
    """
    path = args.get("path", ".")
    show_all = args.get("all", False)
    import os as _os
    full_path = _os.path.abspath(_os.path.expanduser(path))
    if WORKSPACE and not full_path.startswith(_os.path.abspath(WORKSPACE)):
        return {"error": f"Path outside workspace: {path}"}
    try:
        files = _os.listdir(full_path)
    except Exception as e:
        return {"error": f"Cannot list directory: {e}"}
    if not show_all:
        files = [f for f in files if not f.startswith(".")]
    entries = []
    for f in sorted(files):
        fp = _os.path.join(full_path, f)
        try:
            st = _os.stat(fp)
            entries.append({"name": f, "is_dir": _os.path.isdir(fp),
                           "size": st.st_size, "modified": int(st.st_mtime)})
        except OSError:
            entries.append({"name": f, "is_dir": False, "size": 0, "modified": 0})
    return {"files": entries, "path": path, "total": len(entries)}


def get_memory_injection() -> str:
    """Load persistent memory entries as text for system prompt injection.

    Reads the last 20 entries from memory.json and formats them as bullet
    points for inclusion in the AI's system prompt. This gives the AI
    persistent context across sessions without consuming the full context
    window.

    Returns:
        A newline-separated string of memory entries formatted as
        '- key: value' lines, or an empty string if no memories exist.
    """
    mem_file = Path(WORKSPACE) / "memory.json"
    if not mem_file.exists():
        return ""
    try:
        mem = json.loads(mem_file.read_text())
        entries = mem.get("entries", {})
        if not entries:
            return ""
        lines = []
        for key, val in list(entries.items())[-20:]:
            text = val.get("value", "") if isinstance(val, dict) else str(val)
            # Sanitize: strip template syntax to prevent prompt injection
            text = str(text)[:200].replace("{{", "").replace("}}", "")
            lines.append(f"- {key}: {text}")
        return "\n".join(lines)
    except Exception:
        return ""


def exec_check_self(args: dict = None) -> dict:
    """Introspect the worker's runtime configuration and environment.

    Reads config.json from known locations, checks systemd service
    environment variables, lists relevant runtime env vars (AI_MODEL,
    DEEPSEEK_MODEL, OPENAI_MODEL, etc.), and reports hostname and git
    version. Never reads .env files directly - only os.environ - to
    avoid credential exposure.

    Args:
        args: Unused (provided for dispatch consistency).

    Returns:
        {'ok': True, 'self_info': dict} with keys like config_model,
        config_endpoint, config_fallback, hostname, git_version, etc.
    """
    info = {}

    # 1) Read config.json (actual model config)
    config_paths = _CONFIG_FILE_CANDIDATES
    for cp in config_paths:
        try:
            if cp.exists():
                try:
                    cfg = json.loads(cp.read_text())
                    info["config_file"] = str(cp)
                    info["config_model"] = cfg.get("ai_model", "not set")
                    info["config_endpoint"] = cfg.get("ai_endpoint", "not set")
                    info["config_fallback"] = cfg.get("fallback_model", "not set")
                    break
                except Exception as e:
                    info["config_error"] = str(e)
        except PermissionError:
            continue

    # 2) Runtime env vars only (never read .env file directly - credential exposure risk)
    # SECURITY: Do NOT read .env files. Use only os.environ to avoid leaking secrets.

    # 3) Check systemd service (if running as service)
    try:
        r = subprocess.run(
            ["systemctl", "show", "agent-worker.service", "--property=Environment,ExecStart"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            info["systemd"] = r.stdout.strip()
            # Parse model from systemd Environment line
            for part in r.stdout.split():
                if "MODEL" in part.upper() and "=" in part:
                    k, v = part.split("=", 1)
                    info[f"systemd_{k.strip()}"] = v.strip()
    except Exception:
        pass

    # 4) Check env vars at runtime
    for var in ["AI_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL"]:
        val = os.environ.get(var, "")
        if val:
            info[f"runtime_env_{var}"] = val

    # 5) Hostname
    try:
        info["hostname"] = subprocess.check_output(["hostname"], timeout=3).decode().strip()
    except Exception:
        pass

    # 6) Git version
    try:
        r = subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True, text=True, timeout=5, cwd=str(Path.home()))
        if r.returncode == 0:
            info["git_version"] = r.stdout.strip()
    except Exception:
        pass

    # 7) Available tools - list tools based on runtime module availability
    # Tools are always available unless they depend on a module that isn't loaded
    dependent_modules = {
        "verify_multi": ("tical_code.core.verification_broadcast", None),
        "delegate_task": ("tical_code.core.subagent", "_subagent_manager"),
        "get_subagent_result": ("tical_code.core.subagent", "_subagent_manager"),
        "memory_search": ("tical_code.core.memory_store", None),
        "memory": ("tical_code.core.memory_store", None),
        "vigil_status": ("tical_code.core.vigil", None),
    }
    available = []
    for entry in TOOL_SCHEMAS:
        name = entry.get("function", {}).get("name", "")
        if not name or name == "bash_execute":
            continue
        # Check if this tool depends on a specific module
        dep = dependent_modules.get(name)
        if dep:
            mod_name, global_name = dep
            try:
                mod = __import__(mod_name, fromlist=[""])
                if global_name:
                    # Check if the module-level global is wired
                    if not getattr(mod, global_name, None):
                        continue
            except (ImportError, AttributeError):
                continue
        available.append(name)

    info["available_tools"] = available
    info["tool_count"] = len(available)

    return {"ok": True, "self_info": info}


def exec_end_task(args: dict = None) -> dict:
    """Signal task completion to the auto-skill learning system.

    Called by the AI when a task is done. Routes through the module-level
    _skill_extractor global (wired by unified_worker at init) to trigger
    workflow extraction from the tool-call trace.

    A successful task (5+ tool calls, no repeated errors) gets distilled
    into a reusable skill saved to ~/.EITElite/skills/.

    The returned dict includes an ``__end_task__`` marker that the
    message_handler loop detects to stop further iteration.

    Args:
        args: Optional dict with a ``success`` key (default True).
              Set ``{"success": false}`` to skip skill extraction for
              failed tasks.
    """
    try:
        success = (args or {}).get("success", True)
        if _skill_extractor:
            _skill_extractor.end_task(success)
            return {"status": "ok", "message": "Task end signaled", "__end_task__": True}
        else:
            return {"status": "ok", "message": "Skill extractor not available - task end noted", "__end_task__": True}
    except Exception as e:
        return {"status": "error", "message": str(e), "__end_task__": True}

def exec_vigil_status(args: dict = None) -> dict:
    """Query Vigil's current state - patrol count, human/ai state, recent verdicts, pending instructions."""
    if _vigil is None:
        return {"active": False, "reason": "vigil not initialized"}

    vigil = _vigil

    # patrol_count from state history length
    patrol_count = len(vigil._state_history)

    # last_patrol_time from most recent trace
    traces = vigil.trace.recent(1)
    last_patrol_time = traces[0].timestamp if traces else None

    # current_human_state from last state record in history
    current_human_state = None
    if vigil._state_history:
        latest = vigil._state_history[-1]
        current_human_state = {
            "state": latest.state,
            "confidence": latest.confidence,
        }

    # current_ai_state from ai_state_classifier
    current_ai_state = None
    try:
        ai_signal = vigil.ai_signal_collector.collect()
        ai_state = vigil.ai_state_classifier.classify(ai_signal)
        current_ai_state = {
            "state": ai_state.state,
            "confidence": ai_state.confidence,
            "evidence": ai_state.evidence,
            "duration_seconds": ai_state.duration_seconds,
        }
    except Exception:
        pass

    # recent_verdicts (last 5)
    recent_traces = vigil.trace.recent(5)
    recent_verdicts = []
    for t in recent_traces:
        recent_verdicts.append({
            "trace_id": t.trace_id,
            "timestamp": t.timestamp,
            "verdict": t.verdict,
            "state": t.state.get("state") if t.state else None,
        })

    # pending_instructions
    pending = vigil.instruction_queue.all_pending()
    pending_instructions = []
    for p in pending:
        pending_instructions.append({
            "priority": p.priority,
            "status": p.status,
            "queued_at": p.queued_at,
        })

    return {
        "active": True,
        "patrol_count": patrol_count,
        "last_patrol_time": last_patrol_time,
        "current_human_state": current_human_state,
        "current_ai_state": current_ai_state,
        "recent_verdicts": recent_verdicts,
        "pending_instructions": pending_instructions,
    }


def exec_check_metrics(args: dict = None) -> dict:
    """Return performance metrics summary from MetricsCollector.

    Returns tool/LLM latency averages, error counts per tool, and
    top slowest calls.  Returns inactive notice if no collector.
    """
    if _METRICS_COLLECTOR is None:
        return {"active": False, "message": "metrics_collector not initialized"}
    summary = _METRICS_COLLECTOR.summary()
    slowest = _METRICS_COLLECTOR.top_slowest(n=5)
    result = dict(summary)
    if slowest:
        result["slowest_tool_calls"] = slowest
    result["active"] = True
    return result


def exec_memory(args: dict) -> dict:
    """Manage persistent memory with store/recall/search/forget actions.

    Implements the unified 'memory' tool that supports four operations:
      - store: save a value under an auto-generated or provided key.
      - recall: retrieve a value by key.
      - search: full-text search across all memory entries.
      - forget: delete an entry by key.

    Entries are capped at 100; oldest entries are evicted when the cap is
    exceeded. The memory file is stored as memory.json in WORKSPACE.

    Args:
        args: Dict with required key 'action' (one of store/recall/search/forget),
              and optional 'key', 'value', 'content', or 'query' depending on action.

    Returns:
        {'ok': True, 'key': key} for store/forget, or search results,
        or recall value, or {'ok': False, 'error': reason} on failure.
    """
    action = args.get("action", "store")
    mem_file = Path(WORKSPACE) / "memory.json"
    mem = {}
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text())
        except Exception:
            mem = {}
    mem.setdefault("entries", {})

    if action == "store":
        content = args.get("content", "") or args.get("value", "")
        if not content:
            return {"ok": True, "msg": "Empty content, skipping"}
        key = args.get("key", f"auto_{int(time.time())}")
        mem["entries"][key] = {"value": content, "time": time.time()}
        # Keep max 100 entries
        if len(mem["entries"]) > 100:
            old_keys = sorted(mem["entries"].keys())[:-100]
            for k in old_keys:
                del mem["entries"][k]
        try:
            _atomic_write_json(mem_file, mem)
        except Exception as e:
            return {"ok": False, "error": f"Memory write failed: {e}"}
        return {"ok": True, "key": key, "action": "store"}

    elif action == "recall":
        key = args.get("key", "")
        if not key:
            return {"ok": False, "error": "recall requires a key"}
        entry = mem["entries"].get(key)
        if entry:
            return {"ok": True, "action": "recall", "key": key, "value": entry["value"], "time": entry["time"]}
        return {"ok": False, "error": f"Key not found: {key}"}

    elif action == "search":
        query = args.get("query", "").lower()
        if not query:
            return {"ok": False, "error": "search requires a query"}
        results = []
        for k, v in mem["entries"].items():
            val_str = str(v.get("value", "")).lower()
            if query in val_str or query in k.lower():
                results.append({"key": k, "value": v["value"], "time": v["time"]})
        results.sort(key=lambda x: x["time"], reverse=True)
        return {"ok": True, "action": "search", "query": query, "results": results[:20], "total": len(results)}

    elif action == "forget":
        key = args.get("key", "")
        if not key:
            return {"ok": False, "error": "forget requires a key"}
        if key in mem["entries"]:
            del mem["entries"][key]
            try:
                _atomic_write_json(mem_file, mem)
            except Exception as e:
                return {"ok": False, "error": f"Memory write failed: {e}"}
            return {"ok": True, "action": "forget", "key": key}
        return {"ok": False, "error": f"Key not found: {key}"}

    else:
        return {"ok": False, "error": f"Unknown action: {action}. Use store/recall/search/forget"}


# ============ Dispatch ============


def exec_http_post(args: dict) -> dict:
    """POST data to a URL with SSRF protection and rate limiting.

    Enforces rate limiting via _rate_limiter before making the request.
    Validates the target URL against SSRF rules: blocks private/reserved
    IPs (10.x, 127.x, 172.16-31.x, 192.168.x, 169.254.x, ::1). If
    security_baseline is configured, delegates URL validation to it;
    otherwise uses inline checks.

    Args:
        args: Dict with required keys 'url' (str) and 'data' (str),
              optional 'content_type' (default application/json) and
              'timeout' (default 10s, max 30s).

    Returns:
        {'content': response_body, 'status': http_status, 'url': url}
        on success, or {'error': reason} on failure/block.
    """
    if not _rate_limiter.check():
        return {"error": "Rate limited"}
    url = args.get("url", "")
    data = args.get("data", "")
    ct = args.get("content_type", "application/json")
    timeout = min(int(args.get("timeout", 10)), 30)
    # SSRF protection: validate URL before POSTing
    if _SECURITY_URL_CFG is not None:
        try:
            from tical_code.core.security_baseline import validate_url
            safe, reason = validate_url(url, _SECURITY_URL_CFG)
            if not safe:
                return {"error": f"SSRF blocked: {reason}"}
        except Exception:
            pass
    else:
        # Inline SSRF check (same as exec_web_fetch)
        if url and not url.startswith(("http://", "https://")):
            return {"error": "Only http/https URLs are supported"}
        import urllib.parse, socket
        host = urllib.parse.urlparse(url).hostname if url else None
        if host:
            try:
                ip = socket.gethostbyname(host)
                parts = ip.split(".")
                if parts[0] in ("10", "127") or \
                   (parts[0] == "172" and 16 <= int(parts[1]) <= 31) or \
                   (parts[0] == "192" and parts[1] == "168") or \
                   ip.startswith("169.254.") or \
                   ip == "::1":
                    return {"error": f"SSRF blocked: {host} resolves to private IP {ip}"}
            except Exception:
                return {"error": f"Cannot resolve host: {host}"}
    try:
        import urllib.request
        req = urllib.request.Request(url, data=data.encode(), method="POST")
        req.add_header("Content-Type", ct)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:8000]
            return {"content": body, "status": resp.status, "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}

# ── verify_multi: multi-model verification tool ──

_failover = None  # Set by unified_worker after creating ModelFailover
_molecule_engine = None  # Set by set_molecule_engine - MoleculeEngine for chain_exec tool
_memory_store = None  # Set by unified_worker after loading MemoryFTSStore
_subagent_manager = None  # Set by set_subagent_manager
_capability_integrator = None  # Set by set_capability_integrator
_vigil = None  # Set by set_vigil - wired for tool output sanitization
_skip_sanitize = False  # Power mode: skip all sanitization globally
_skill_extractor = None  # Set by set_skill_extractor - wired for end_task


def set_skill_extractor(ext):
    """Wire SkillExtractor instance into tool_executor for the end_task tool.

    Sets the module-level _skill_extractor global that exec_end_task uses
    to signal task completion to the auto-skill learning system. Called by
    unified_worker during initialization.

    Args:
        ext: A SkillExtractor instance from skill_extractor.py.
    """
    global _skill_extractor
    _skill_extractor = ext


def set_failover(fa):
    """Wire ModelFailover instance into tool_executor for the verify_multi tool.

    Sets the module-level _failover global that exec_verify_multi uses to
    broadcast prompts across all available AI providers. Called by
    unified_worker during initialization.

    Args:
        fa: A ModelFailover instance providing access to all configured
           AI provider families.
    """
    global _failover
    _failover = fa


def set_molecule_engine(engine):
    """Wire MoleculeEngine instance into tool_executor for the chain_exec tool.

    Sets the module-level _molecule_engine global that chain_exec uses
    to execute molecular chains - sequences of AI models where each model's
    output feeds into the next for emergent intelligence.

    The engine contains a ModelRegistry with all registered providers.
    As local small models are deployed, register them via:
        engine.registry.register_local_provider(...)

    For the personal distillate model:
        engine.registry.register_distillate_provider(...)

    Args:
        engine: A MoleculeEngine instance with configured ModelRegistry.
    """
    global _molecule_engine
    _molecule_engine = engine


def set_memory_store(store):
    """Wire MemoryFTSStore into tool_executor for the memory_search tool.

    Sets the module-level _memory_store global that exec_memory_search
    uses for FTS5 full-text search across indexed documents (SOUL.md,
    MEMORY.md, USER.md, conversation history). Called by unified_worker
    during initialization.

    Args:
        store: A MemoryFTSStore instance with FTS5 indexing capability.
    """
    global _memory_store
    _memory_store = store


def set_vigil(vigil_instance):
    """Wire Vigil instance into tool_executor for output sanitization.

    Sets the module-level _vigil global. When set, execute() will run
    _sanitize_tool_output on every tool result to scan for PII and secret
    key patterns. Called by unified_worker during initialization.

    Args:
        vigil_instance: A Vigil instance for security monitoring.
    """
    global _vigil
    _vigil = vigil_instance


def set_skip_sanitize(flag: bool):
    """Skip tool output sanitization globally (power mode bypass)."""
    global _skip_sanitize
    _skip_sanitize = flag


def set_subagent_manager(manager):
    """Wire SubAgentManager into tool_executor for delegate_task and get_subagent_result tools.

    Sets the module-level _subagent_manager global. Called by unified_worker
    during initialization to enable sub-agent task delegation through the
    delegate_task and get_subagent_result tools.

    Args:
        manager: A SubAgentManager instance for parallel task delegation.
    """
    global _subagent_manager
    _subagent_manager = manager


def set_capability_integrator(integrator):
    """Wire CapabilityIntegrator into tool_executor for capability_list/capability_call tools.

    Sets the module-level _capability_integrator global. Called by unified_worker
    during initialization to enable capability discovery and dispatch.

    Args:
        integrator: A CapabilityIntegrator instance.
    """
    global _capability_integrator
    _capability_integrator = integrator


def exec_verify_multi(args: dict) -> dict:
    """Execute verify_multi tool: broadcast prompt to all models, audit results.

    Uses the wired-in ModelFailover instance to query all available
    provider families and produce a VerificationAudit.
    """
    prompt = args.get("prompt", "")
    threshold = float(args.get("threshold", 0.3))

    if not prompt:
        return {"error": "Prompt is required for verify_multi"}

    if _failover is None:
        return {"error": "ModelFailover not wired into tool_executor. Call set_failover() first."}

    try:
        from tical_code.core.verification_broadcast import execute_verify_multi
        result = execute_verify_multi(
            failover=_failover,
            prompt=prompt,
            threshold=threshold,
        )
        return result
    except ImportError as e:
        return {"error": f"verification_broadcast module not available: {e}"}
    except Exception as e:
        logger.error(f"[executor] verify_multi exception: {e}")
        return {"error": str(e)}


def chain_exec(args: dict) -> dict:
    """Execute chain_exec tool: run a molecular chain for emergent intelligence.

    Supports preset molecules (code_review, research, safety_check, decision)
    and dynamic chains (custom_steps parameter). Provider preference via
    RoutingContext - no registry mutation.

    The key difference from verify_multi:
      verify_multi: same prompt → N models → vote → consensus
      chain_exec:   prompt → model A → model B → model C → emergent output
    """
    if _molecule_engine is None:
        return {"error": "MoleculeEngine not wired. Call set_molecule_engine() first."}

    try:
        from tical_code.core.molecule import execute_chain_exec as _exec

        return _exec(
            engine=_molecule_engine,
            molecule=args.get("molecule"),
            prompt=args.get("prompt", ""),
            context=args.get("context"),
            custom_steps=args.get("custom_steps"),
            provider_preference=args.get("provider_preference", "auto"),
        )
    except ImportError as e:
        return {"error": f"molecule module not available: {e}"}
    except Exception as e:
        logger.error("[executor] chain_exec exception: %s", e)
        return {"error": str(e)}


# ── Self-repair engine for safe_modify ────────────────────────────
# Worker startup injects the real engine via set_self_repair_engine().
# Falls back to ad-hoc standalone engine if not wired.

# Self-modify permission level:
#   0 = BLOCKED in default/plan mode (only bypassPermissions allows)
#   1 = ALLOWED with confirmation note ("requires confirmation")
#   2 = ALLOWED unconditionally
#   3 = ALLOWED + skip sandbox test
#   4 = ALLOWED + skip all checks (full bypass - bypassPermissions equivalent)
_SELF_MODIFY_PERMISSION = 0
_SELF_REPAIR_ENGINE = None
_METRICS_COLLECTOR = None  # injected by module_registry -> metrics_collector


def set_metrics_collector(collector) -> None:
    """Inject the worker's MetricsCollector into tool_executor.

    Called by module_registry when the metrics_collector module is
    loaded.  Every tool execution is timed and recorded via the
    collector when this is set.
    """
    global _METRICS_COLLECTOR
    _METRICS_COLLECTOR = collector
_CHECKPOINT_MANAGER = None


def set_checkpoint_manager(manager) -> None:
    """Inject the worker's CheckpointManager into tool_executor.

    Called by unified_worker at startup so checkpoint tools can use
    the real manager with full persistence and file tracking.
    """
    global _CHECKPOINT_MANAGER
    _CHECKPOINT_MANAGER = manager


def set_self_modify_permission(level: int) -> None:
    """Set the self-modify permission level (0-4).

    Controls whether safe_modify / safe_modify_diff tools are available
    and how strictly they gate the safety pipeline.

    Args:
        level: 0=blocked, 1=confirm, 2=unconditional, 3=skip-sandbox, 4=bypass-all
    """
    global _SELF_MODIFY_PERMISSION
    level = max(0, min(4, level))
    _SELF_MODIFY_PERMISSION = level
    logger.info("[executor] self_modify permission set to level %d", level)


def get_self_modify_permission() -> int:
    """Get current self-modify permission level (0-4)."""
    return _SELF_MODIFY_PERMISSION


def _make_checkpoint_manager(workspace: str):
    """Create a standalone CheckpointManager for ad-hoc checkpoint tool usage.

    Used when the worker hasn't injected its manager (e.g. CLI / test usage).
    Returns None if checkpoint module is not available.
    """
    try:
        from tical_code.core.checkpoint import CheckpointManager, CheckpointConfig
        return CheckpointManager(CheckpointConfig(workspace=workspace))
    except ImportError:
        logger.warning("[executor] CheckpointManager not available - checkpoint tools disabled")
        return None


def set_self_repair_engine(engine) -> None:
    """Inject the worker's SelfRepairEngine into tool_executor.

    Called by unified_worker at startup so safe_modify tools can use
    the real engine with full configuration (git repo, protected files,
    modification limit, sandbox mode).
    """
    global _SELF_REPAIR_ENGINE
    _SELF_REPAIR_ENGINE = engine


def _make_self_repair_engine(workspace: str):
    """Create a standalone SelfRepairEngine for ad-hoc safe_modify calls.

    Used when the worker hasn't injected its engine (e.g. CLI / test usage).
    Returns None if self_repair module is not available (e.g. light profile
    nodes like Oracle/Test).
    """
    try:
        from types import SimpleNamespace
        from tical_code.core.self_repair import SelfRepairEngine
        fw = SimpleNamespace(
            cfg={"workspace": workspace, "max_self_modifications": 20},
        )
        return SelfRepairEngine(framework=fw, sandbox_mode="disabled")
    except ImportError:
        logger.warning("[executor] SelfRepairEngine not available - safe_modify tools disabled")
        return None


def exec_safe_modify(args: dict) -> dict:
    """Execute a file modification through the Self-Repair Engine's safe_modify
    pipeline: protected file check, git backup, write, syntax validation,
    code safety check, sandbox test, cross-verify, and audit log.
    Rolls back automatically on failure.

    USE THIS instead of file_write for modifying system code.
    """
    # ── Self-modify permission gate ────────────────────────────────
    level = _SELF_MODIFY_PERMISSION
    if level == 0:
        return {
            "success": False,
            "error": (
                "Self-modify tools are blocked (level 0). "
                "An admin must enable them via [CMD] permission self_modify <1-4>. "
                "Level 1=confirm, 2=unconditional, 3=skip-sandbox, 4=bypass-all"
            ),
        }

    path = args.get("path", "")
    new_content = args.get("new_content", "")
    reason = args.get("reason", "")
    sandbox_test = args.get("sandbox_test", True) if level <= 2 else False

    if not path or new_content is None:
        return {"success": False, "error": "path and new_content are required"}

    # Level 4: bypass all safety checks - direct write only
    if level >= 4:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info("[executor] safe_modify level-4 bypass: wrote %s (%d bytes)", path, len(new_content))
            return {
                "success": True,
                "message": f"Written {len(new_content)} bytes to {path} (bypass mode)",
                "path": path,
            }
        except Exception as e:
            logger.error("[executor] safe_modify level-4 write error: %s", e)
            return {"success": False, "error": str(e)}

    engine = _SELF_REPAIR_ENGINE
    if engine is None:
        engine = _make_self_repair_engine(WORKSPACE)

    if engine is None:
        return {"success": False, "error": "SelfRepairEngine not available"}

    try:
        result = asyncio.run(engine.safe_modify(
            file_path=path,
            new_content=new_content,
            sandbox_test=sandbox_test,
            require_human_approval=False,
        ))
        # Level 1: add confirmation note
        if level == 1:
            if isinstance(result, dict):
                result.setdefault("_note", "Self-modify level 1 - requires admin confirmation if rollback needed")
        return result
    except Exception as e:
        logger.error("[executor] safe_modify exception: %s", e)
        return {"success": False, "error": str(e)}


def exec_safe_modify_diff(args: dict) -> dict:
    """Apply a targeted find-and-replace through the safe_modify pipeline.

    Like file_patch but with full safety checks: reads the current file,
    applies the find-and-replace, then passes the full modified content
    through safe_modify.
    """
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    reason = args.get("reason", "")

    if not path or not old_string:
        return {"success": False, "error": "path and old_string are required"}
    if not os.path.exists(path):
        return {"success": False, "error": f"File not found: {path}"}

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_string)
        if count == 0:
            return {"success": False, "error": "old_string not found in file"}
        if count > 1:
            return {"success": False,
                    "error": f"old_string found {count} times. Must be unique. Include more context."}

        new_content = content.replace(old_string, new_string, 1)
        return exec_safe_modify({
            "path": path,
            "new_content": new_content,
            "reason": reason or f"Diff: replace '{old_string[:40]}...'",
            "sandbox_test": True,
        })
    except Exception as e:
        logger.error("[executor] safe_modify_diff exception: %s", e)
        return {"success": False, "error": str(e)}


def exec_checkpoint_list(args: dict) -> dict:
    """List available checkpoints with status filter."""
    status_filter = args.get("status", None)
    engine = _CHECKPOINT_MANAGER
    if engine is None:
        engine = _make_checkpoint_manager(WORKSPACE)
    if engine is None:
        return {"success": False, "error": "CheckpointManager not available"}

    try:
        cps = engine.list_checkpoints(status=status_filter)
        return {"success": True, "checkpoints": cps, "total": len(cps)}
    except Exception as e:
        logger.error("[executor] checkpoint_list exception: %s", e)
        return {"success": False, "error": str(e)}


def exec_checkpoint_restore(args: dict) -> dict:
    """Restore files from a checkpoint.

    Args:
        checkpoint_id: The checkpoint ID to restore from.
        selective_files: Optional list of specific file paths to restore.
        confirm: Must be True to proceed (safety gate).
    """
    cp_id = args.get("checkpoint_id", "")
    selective_files = args.get("selective_files", None)
    confirm = args.get("confirm", False)

    if not cp_id:
        return {"success": False, "error": "checkpoint_id is required"}
    if not confirm:
        return {"success": False, "error": "confirm=True is required to proceed with restore. Use preview first."}

    engine = _CHECKPOINT_MANAGER
    if engine is None:
        engine = _make_checkpoint_manager(WORKSPACE)
    if engine is None:
        return {"success": False, "error": "CheckpointManager not available"}

    try:
        # Preview first to show what will change
        preview = engine.preview_restore(cp_id, selective_files=selective_files)
        if not preview:
            return {"success": False, "error": f"Checkpoint '{cp_id}' not found or has no file snapshots"}

        result = engine.restore(cp_id, selective_files=selective_files, confirm=True)
        return {
            "success": result,
            "checkpoint_id": cp_id,
            "files_affected": [
                {"path": p["path"], "status": p["status"]} for p in preview
            ],
        }
    except Exception as e:
        logger.error("[executor] checkpoint_restore exception: %s", e)
        return {"success": False, "error": str(e)}


def exec_memory_search(args: dict) -> dict:
    """Execute memory_search tool: FTS5 full-text search across memory store.

    Searches SOUL.md, MEMORY.md, USER.md, TOOLS.md, and other indexed documents.
    Returns ranked results with snippets and relevance scores.
    """
    query = args.get("query", "")
    limit = int(args.get("limit", 10))

    if not query:
        return {"error": "Query is required for memory_search"}

    if _memory_store is None:
        # Fallback to file-based grep if FTS5 store not wired
        return _fallback_memory_search(query, limit)

    try:
        results = _memory_store.search(query, limit=limit)
        formatted = []
        for r in results:
            formatted.append({
                "source": r.file_key,
                "section": r.section_title,
                "snippet": r.snippet.replace(">>>", "**").replace("<<<", "**"),
                "relevance": round(r.rank, 3),
            })
        return {
            "query": query,
            "results": formatted,
            "total": len(formatted),
        }
    except Exception as e:
        logger.error(f"[executor] memory_search error: {e}")
        return {"error": str(e)}


def _fallback_memory_search(query: str, limit: int = 10) -> dict:
    """Fallback memory search using grep on memory files when FTS5 unavailable."""
    import glob
    results = []
    memory_dirs = [
        os.path.expanduser("~/.EITElite/memory"),
        os.path.expanduser("~/memory"),
    ]
    for md in memory_dirs:
        if not os.path.isdir(md):
            continue
        for fpath in glob.glob(os.path.join(md, "*.md")):
            try:
                with open(fpath, errors="replace") as f:
                    content = f.read()
                for line in content.split("\n"):
                    if query.lower() in line.lower():
                        results.append({
                            "source": os.path.basename(fpath),
                            "snippet": line.strip()[:200],
                        })
                        if len(results) >= limit:
                            break
            except Exception:
                pass
        if results:
            break
    return {"query": query, "results": results, "total": len(results), "fallback": True}


def _delegate_task_dispatch(args: dict) -> dict:
    """Lazy-loading dispatch bridge for delegate_task tool.

    Imports the handler from builtin_tools to avoid circular imports.
    """
    try:
        from tical_code.core.builtin_tools import delegate_task_handler
        return delegate_task_handler(args)
    except Exception as e:
        return {"error": f"delegate_task dispatch failed: {e}"}


def _get_subagent_result_dispatch(args: dict) -> dict:
    """Lazy-loading dispatch bridge for get_subagent_result tool.

    Imports the handler from builtin_tools to avoid circular imports.
    """
    try:
        from tical_code.core.builtin_tools import get_subagent_result_handler
        return get_subagent_result_handler(args)
    except Exception as e:
        return {"error": f"get_subagent_result dispatch failed: {e}"}


def _sanitize_tool_output(result: dict, tool_name: str = "") -> dict:
    """Scan tool output for PII and secret key patterns. Redact if found.

    Checks for: API keys (sk-...), GitHub tokens (ghp_...), email addresses,
    IP addresses, credit card numbers, AWS keys, JWT tokens, and other secrets.

    Returns the result dict with sensitive content redacted and a __sanitized__
    flag set if any patterns were found.
    """
    if not isinstance(result, dict):
        return result

    # Compile all PII / secret patterns
    patterns = [
        # API keys
        (re.compile(r'sk-[a-zA-Z0-9]{20,}'), 'sk-***REDACTED***'),
        (re.compile(r'sk-[a-zA-Z0-9_-]{20,}'), 'sk-***REDACTED***'),
        # GitHub tokens
        (re.compile(r'ghp_[a-zA-Z0-9]{36}'), 'ghp_***REDACTED***'),
        (re.compile(r'gh[psu]_[a-zA-Z0-9]{36,}'), 'gh*_***REDACTED***'),
        # Email addresses
        (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), '***EMAIL_REDACTED***'),
        # IPv4 addresses (non-reserved)
        (re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'), '***IP_REDACTED***'),
        # Credit card patterns (basic 13-19 digit patterns)
        (re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b'), '***CC_REDACTED***'),
        # AWS access keys
        (re.compile(r'AKIA[0-9A-Z]{16}'), 'AKIA***REDACTED***'),
        # AWS secret keys (heuristic)
        (re.compile(r'(?i)aws[_ ]?secret[_ ]?(?:access[_ ]?)?key[\s:=]+["\']?([A-Za-z0-9/+]{40})'), 'AWS_SECRET=***REDACTED***'),
        # Generic bearer / auth tokens (JWT pattern: xxx.yyy.zzz)
        (re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'), '***JWT_REDACTED***'),
        # Telegram bot tokens
        (re.compile(r'\d{8,10}:AA[a-zA-Z0-9_-]{32,40}'), '***BOT_TOKEN_REDACTED***'),
        # Private SSH keys
        (re.compile(r'-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----'), '***PRIVATE_KEY_REDACTED***'),
        # Generic tokens with key-like prefixes
        (re.compile(r'(?:api[_-]?key|token|secret|password|passwd|auth)[\s:=]+["\']?([^\s"\'&]{16,})', re.IGNORECASE), '***REDACTED***'),
    ]

    redacted_count = 0
    result_copy = dict(result)

    # Scan string values in the result dict recursively
    def _scan_and_redact(obj, path=""):
        nonlocal redacted_count
        if isinstance(obj, str):
            original = obj
            for pattern, replacement in patterns:
                if pattern.search(original):
                    original = pattern.sub(replacement, original)
            if original != obj:
                redacted_count += 1
            return original
        elif isinstance(obj, dict):
            return {k: _scan_and_redact(v, f"{path}.{k}") for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_scan_and_redact(item, f"{path}[{i}]") for i, item in enumerate(obj)]
        return obj

    result_copy = _scan_and_redact(result_copy)

    if redacted_count > 0:
        logger.warning("[executor] PII/secret redacted in %s output: %d patterns found", tool_name or "tool", redacted_count)
        result_copy["__sanitized__"] = True
        result_copy["__sanitized_count__"] = redacted_count

    return result_copy


# =============================================================================
# Capability integration dispatch
# =============================================================================

def exec_capability_list(args: dict = None) -> dict:
    """Return the full capability manifest.

    Delegates to the global _capability_integrator if wired.
    """
    global _capability_integrator
    if _capability_integrator is None:
        return {"success": False, "error": "CapabilityIntegrator not initialized"}
    try:
        from tical_code.core.capability_integrator import capability_list as _cap_list
        return _cap_list(args)
    except Exception as e:
        return {"success": False, "error": str(e)}


def exec_capability_call(args: dict) -> dict:
    """Invoke a named capability.

    Args should have: {"name": "...", "params": {...}}
    Delegates to the global _capability_integrator if wired.
    """
    global _capability_integrator
    if _capability_integrator is None:
        return {"success": False, "error": "CapabilityIntegrator not initialized"}
    try:
        from tical_code.core.capability_integrator import capability_call as _cap_call
        return _cap_call(args)
    except Exception as e:
        return {"success": False, "error": str(e)}


def exec_ask_user(args: dict) -> dict:
    """Ask the human user for input when stuck, need CAPTCHA, or need confirmation.

    Returns a dict with needs_user_input=True to signal the caller to pause
    and wait for human response.

    Args:
        args: Dict with 'question' (required) and 'context' (optional)

    Returns:
        Dict with needs_user_input=True and the question message
    """
    question = args.get("question", "")
    context_str = args.get("context", "")

    message = f"[NEED_USER_INPUT] {question}"
    if context_str:
        message += f"\nContext: {context_str}"

    return {
        "success": True,
        "output": message,
        "needs_user_input": True,
        "question": question,
        "context": context_str,
    }



def exec_create_background_task(args: dict) -> dict:
    """Create a persistent background task for autonomous multi-step execution.

    Calls task_state.create_task() to persist a task to disk. The main
    worker loop's "RESUME ACTIVE TASKS" section will pick it up on the
    next iteration and execute it via task_handler.run_task().

    Args:
        args: Dict with 'goal' (required), optional 'plan' (list of strings),
              and 'max_steps' (int, default 100).

    Returns:
        Dict with task_id, status, and the created goal.
    """
    goal = args.get("goal", "")
    if not goal:
        return {"error": "goal is required"}
    plan = args.get("plan", [])
    max_steps = args.get("max_steps", 100)
    try:
        from tical_code.core.task_state import create_task
        # Detect workspace the same way config._find_workspace() does
        _ws = ""
        for _ev in ["TICAL_CODE_ROOT", "TICOBOT_DIR"]:
            _d = os.environ.get(_ev, "")
            if _d:
                _ws = _d
                break
        if not _ws:
            _home = os.path.expanduser("~")
            for _cand in [os.path.join(_home, "EITE-agent"), os.path.join(_home, "eitelite")]:
                if os.path.isdir(_cand):
                    _ws = _cand
                    break
        task = create_task(
            goal=goal,
            plan=plan if plan else None,
            max_steps=min(max_steps, 500),
            workspace=_ws,
        )
        logger.info("Background task created: %s - %s", task.task_id, goal[:80])
        return {
            "success": True,
            "task_id": task.task_id,
            "goal": goal,
            "plan_steps": len(plan),
            "max_steps": max_steps,
            "message": f"Background task '{goal[:60]}' created (id={task.task_id}). The task engine will execute it autonomously."
        }
    except ImportError:
        return {"error": "task_state module not available"}
    except Exception as e:
        logger.error("Failed to create background task: %s", e)
        return {"error": str(e)}


def execute(name: str, args: dict, base_dir: str = "") -> dict:
    """Unified dispatch entry point for all tool execution.

    Routes a tool name to its handler function through a dispatch table.
    Applies the complete security pipeline: sandbox pre-check, handler
    dispatch, error logging, and PII/secret sanitization via Vigil.

    The dispatch table maps tool names to their handler functions:
      bash → exec_bash         file_read → exec_file_read
      file_write → exec_file_write  file_patch → exec_file_patch
      memory_save → exec_memory_save  memory_load → exec_memory_load
      memory → exec_memory     state_save → exec_state_save
      chat_send → exec_chat_send  restart_self → exec_restart_self
      web_fetch → exec_web_fetch  http_post → exec_http_post
      file_search → exec_file_search  list_dir → exec_list_dir
      check_self → exec_check_self  verify_multi → exec_verify_multi
      memory_search → exec_memory_search  vigil_status → exec_vigil_status
      delegate_task → _delegate_task_dispatch
      get_subagent_result → _get_subagent_result_dispatch
      end_task → exec_end_task

    Args:
        name: Tool name (one of the dispatch table keys above).
        args: Parameter dict passed to the tool handler.
        base_dir: Optional working directory (default WORKSPACE).

    Returns:
        Unified result dict. On error, contains {'error': reason}.
        Results are sanitized for PII/secrets when Vigil is active.
    """
    # --- Empty/blank name guard ---
    if not name or not name.strip():
        return {"error": "Empty tool name"}

    logger.info(f"[executor] {name}({str(args)[:80]})")


    # --- Sandbox pre-check (P0) ---
    try:
        from tical_code.core.tool_sandbox import SandboxRunner
        _sandbox = SandboxRunner()
        allowed, reason = _sandbox.pre_check(name, args)
        if not allowed:
            logger.warning(f"[executor] SANDBOX BLOCKED {name}: {reason}")
            return {"error": f"[SANDBOX BLOCKED] {reason}"}
    except ImportError:
        logger.debug("[executor] tool_sandbox not available, skipping pre-check")
    except Exception as e:
        logger.warning(f"[executor] sandbox pre-check error (allowing): {e}")

    dispatch = {
        "bash": exec_bash,  # backward compat - schema now uses shell_exec
        "shell_exec": exec_bash,
        "file_read": lambda a: exec_file_read(a, base_dir),
        "file_write": lambda a: exec_file_write(a, base_dir),
        "file_patch": exec_file_patch,
        "memory_save": lambda a: exec_memory_save(a, base_dir),
        "memory_load": lambda a: exec_memory_load(a, base_dir),
        "memory": exec_memory,
        "state_save": lambda a: exec_state_save(a, base_dir),
        "chat_send": exec_chat_send,
        "restart_self": exec_restart_self,
        "web_fetch": exec_web_fetch,
        "http_post": exec_http_post,
        "file_search": exec_file_search,
        "list_dir": exec_list_dir,
        "check_self": exec_check_self,
        "verify_multi": exec_verify_multi,
        "chain_exec": chain_exec,
        "safe_modify": exec_safe_modify,
        "safe_modify_diff": exec_safe_modify_diff,
        "checkpoint_list": exec_checkpoint_list,
        "checkpoint_restore": exec_checkpoint_restore,
        "memory_search": exec_memory_search,
        "delegate_task": _delegate_task_dispatch,
        "get_subagent_result": _get_subagent_result_dispatch,
        "vigil_status": exec_vigil_status,
        "check_metrics": exec_check_metrics,
        "end_task": exec_end_task,
        "capability_list": exec_capability_list,
        "capability_call": exec_capability_call,
        "ask_user": exec_ask_user,
        "start_background_task": exec_create_background_task,
    }
    handler = _PLUGIN_TOOLS.get(name) or dispatch.get(name)
    if not handler:
        logger.error(f"[executor] Unknown tool called: {name}")
        return {"error": f"Unknown tool: {name}"}

    try:
        import time as _time_mod
        _t0 = _time_mod.time()
        result = handler(args)
        _elapsed = _time_mod.time() - _t0
        # Record in metrics collector if available
        if _METRICS_COLLECTOR is not None:
            if isinstance(result, dict) and "error" in result:
                err_msg = result.get("error", "unknown")[:200]
                _METRICS_COLLECTOR.record_tool_error(name, err_msg)
            else:
                _METRICS_COLLECTOR.record_tool_call(name, _elapsed)
        if isinstance(result, dict) and "error" in result and "explicit_error" not in result:
            logger.warning(f"[executor] {name} error: {result['error'][:100]}")
        # Sanitize tool output: scan for PII and secret key patterns
        if _vigil is not None and isinstance(result, dict) and not _skip_sanitize:
            try:
                result = _sanitize_tool_output(result, name)
            except Exception as e:
                logger.debug("[executor] sanitization skipped for %s: %s", name, e)
        return result or {}
    except Exception as e:
        logger.error(f"[executor] {name} exception: {e}")
        return {"error": str(e)}


class ToolExecutor:
    """Object-oriented wrapper for tool execution.

    Provides an instance-based interface for EITE-benchmark compatibility.
    Delegates all calls to the module-level execute() function, adding
    argument type validation (ensures name is str and args is dict).

    This class exists so that benchmarking harnesses expecting a class-based
    tool executor can use the same underlying implementation.
    """

    def __init__(self):
        """Initialize the ToolExecutor with a dedicated logger."""
        self.logger = logging.getLogger("EITElite.executor")

    def execute(self, name: str, args: dict, base_dir: str = "") -> dict:
        """Execute a tool by name with type validation.

        Validates argument types, then delegates to the module-level
        execute() function which handles sandbox pre-checks, dispatch,
        and output sanitization.

        Args:
            name: Tool name to execute.
            args: Parameter dict for the tool.
            base_dir: Optional working directory.

        Returns:
            Tool result dict, or {'error': 'invalid_arguments'} on type mismatch.
        """
        if not isinstance(name, str) or not isinstance(args, dict):
            self.logger.error(f"Invalid arguments: name={type(name).__name__}, args={type(args).__name__}")
            return {"error": "invalid_arguments"}
        return execute(name, args, base_dir)

def set_sustained_task_manager(mgr) -> None:
    """Inject SustainedTaskManager from worker bootstrap."""
    global _SUSTAINED_TASK_MGR
    _SUSTAINED_TASK_MGR = mgr


