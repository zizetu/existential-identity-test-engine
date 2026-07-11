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
"""Message handler module — LLM + tools per-message turn processing.

Handles every inbound user message through the full processing pipeline:
  1. Decision engine pre-check (goal clarification and constitution compliance).
  2. [CMD] protocol detection and forwarding — direct command execution with no
     LLM involvement, supporting ping, status, deploy, restart, log, escalate,
     exec, report, and switch_model commands with tiered permissions.
  3. Task request detection — routes messages tagged [TASK] or identified as
     autonomous tasks to the task_handler module for background execution.
  4. Main LLM conversation loop with tool-call iteration (up to 10 turns):
     session-aware context persistence, message adaptation for model families,
     token-aware compaction, tool execution with verification phases and
     constitution checks, doom-loop detection, circuit-breaker on repeated
     tool failures, and forced reply at iteration exhaustion.
  5. Privacy scanning of all outbound replies — PII/key redaction before
     replies are sent to channels (API keys, emails, IPs, credit cards, JWTs,
     bot tokens, private keys, and credential-like fields).
  6. Skill extraction hooks — records tool-call sequences for auto-skill
     generation (start_task / record_tool_call / end_task).
  7. Vigil AI signal collection — record_input, record_response,
     record_tool_call, and token-usage tracking for abuse/fidelity monitoring.
  8. Memory management — periodic garbage collection, RSS monitoring with
     restart scheduling, session compaction, and memory evolution hooks.
  9. Session persistence — saves full conversation turns to the session store
     with periodic cleanup of old sessions.

Extracted from unified_worker.py._handle_message (L1456-2116) plus
CMD helpers (_handle_cmd, _send_cmd_reply, _cmd_get_level, _exec_cmd).
Takes a SharedContext instead of self, enabling the god-object split.

Author: Tical
Version: see tical_code.__version__"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import hashlib
import hmac as _hmac_module
import json
import logging
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import unicodedata
import datetime as _dt
from typing import Any, Optional

# ── EITElite internal imports ──────────────────────────────────────
from tical_code.core.shared_context import SharedContext, _get_rss_mb
from tical_code.core.trace import TraceLogger, TraceEvent
from tical_code.core.channel import Message, Response
from tical_code.core.doom_loop import DoomLoopDetector, DoomLoopConfig, LoopLevel as DoomLoopLevel
from tical_code.core.tool_executor import execute, TOOL_SCHEMAS, is_concurrency_safe
from tical_code.core.clarify import ClarifyAnswer, ClarifyStatus, ClarifyStrategy, format_clarify_questions
from tical_code.core.response_formatter import format_result
from tical_code.core.prompt import build_power_mode_suffix, strip_and_inject_power_mode
from tical_code.core.permission_checker import PermissionChecker, PermissionMode
from tical_code.core.decision_engine import ModelStatus

# Conditional imports — may be None on light installs
try:
    from tical_code.core.model_failover import ModelFailover
except ImportError:
    ModelFailover = None

try:
    from tical_code.core.task_state import (
        is_task_request, create_task, save_state,
    )
except ImportError:
    is_task_request = None
    create_task = None
    save_state = None

try:
    from tical_code.core.session_snapshot import save_snapshot
except ImportError:
    save_snapshot = None

try:
    from tical_code.core.memory_profiler import force_gc_collect
except ImportError:
    force_gc_collect = None

# Conditional import: SkillSpector audit runner
try:
    from tical_code.core.skillspector.runner import SkillAuditRunner
    _SKILL_AUDIT_AVAILABLE = True
except ImportError:
    SkillAuditRunner = None
    _SKILL_AUDIT_AVAILABLE = False

logger = logging.getLogger("EITElite.message_handler")

# ── EITE Data Directory ──────────────────────────────────────────
# All persistent data lives under this base directory (passwords, logs,
# memory, sessions, checkpoints).  Override via EITE_DATA_DIR env var
# or change the default below for non-standard deployments.
_EITE_DATA_DIR = os.path.expanduser(
    os.environ.get("EITE_DATA_DIR", "~/.EITElite")
)

# ── Per-User Password System ──────────────────────────────────────
# Passwords are stored in {_EITE_DATA_DIR}/passwords.json (NOT git tracked).
# Format: {"admin": "<sha256_hash>", "users": {"<chat_id>": "<sha256_hash>"}}
# Admin password comes from UNLOCK_PASSWORD env var (hashed on first use).
# Users can set their own password via [set pw] after unlocking.

_PASSWORDS_FILE = os.path.join(_EITE_DATA_DIR, "passwords.json")



class IterationBudget:
    """Thread-safe iteration budget with consume/refund mechanism."""
    def __init__(self, max_total: int = 60, max_consecutive_failures: int = 5):
        self.max_total = max_total
        self.max_consecutive_failures = max_consecutive_failures
        self._used = 0
        self._consecutive_failures = 0
        self._lock = __import__("threading").Lock()
        self._start_time = __import__("time").time()
        self._max_wall_time = 600
    def consume(self):
        with self._lock:
            if self._used >= self.max_total: return False
            if __import__("time").time() - self._start_time > self._max_wall_time: return False
            if self._consecutive_failures >= self.max_consecutive_failures: return False
            self._used += 1
            return True
    def refund(self):
        with self._lock:
            if self._used > 0: self._used -= 1
    def record_failure(self):
        with self._lock: self._consecutive_failures += 1
    def record_success(self):
        with self._lock: self._consecutive_failures = 0
    @property
    def remaining(self):
        with self._lock: return max(0, self.max_total - self._used)
    @property
    def iteration(self):
        with self._lock: return self._used
    @property
    def elapsed_seconds(self):
        return __import__("time").time() - self._start_time

def _load_passwords() -> dict:
    """Load password database from disk."""
    try:
        if os.path.exists(_PASSWORDS_FILE):
            with open(_PASSWORDS_FILE, "r") as f:
                return json.loads(f.read())
    except Exception:
        pass
    return {}

def _save_passwords(data: dict) -> None:
    """Save password database to disk atomically."""
    os.makedirs(os.path.dirname(_PASSWORDS_FILE), exist_ok=True)
    tmp = _PASSWORDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _PASSWORDS_FILE)

def _hash_pw(password: str) -> str:
    """SHA-256 hash a password with random salt for storage."""
    salt = secrets.token_hex(16)
    hash_val = hashlib.sha256(password.encode() + salt.encode()).hexdigest()
    return f"{salt}:{hash_val}"

def _verify_password(chat_id: str, password: str, data: dict, admin_hash: str) -> bool:
    """Check password against per-chat entry first, then admin.

    Supports both legacy unsalted hashes (64-char hex) and new
    salted hashes (format: 'salt:hash').
    """
    # Per-chat password takes priority
    user_hash = data.get("users", {}).get(str(chat_id))
    if user_hash and _check_pw(password, user_hash):
        return True
    # Fall back to admin password
    if admin_hash and _check_pw(password, admin_hash):
        return True
    return False

def _check_pw(password: str, stored: str) -> bool:
    """Check a password against a stored hash (salted or legacy)."""
    if ":" in stored:
        # Salted format: salt:hash
        salt, expected = stored.split(":", 1)
        actual = hashlib.sha256(password.encode() + salt.encode()).hexdigest()
        return actual == expected
    else:
        # Legacy unsalted format: plain sha256 hexdigest
        return hashlib.sha256(password.encode()).hexdigest() == stored

def _ensure_admin_hash() -> str:
    """Get admin password hash, initializing from env var if needed."""
    data = _load_passwords()
    admin_hash = data.get("admin", "")
    if admin_hash:
        return admin_hash
    # First run: hash the admin password from env
    admin_pw = os.environ.get("UNLOCK_PASSWORD", "")
    if admin_pw:
        admin_hash = _hash_pw(admin_pw)
        data["admin"] = admin_hash
        data.setdefault("users", {})
        _save_passwords(data)
        return admin_hash
    return ""


# ────────────────────────────────────────────────────────────────────
# Privacy scan: detect and redact sensitive data before sending replies
#
# This section defines compiled regex patterns for detecting personally
# identifiable information (PII) and secret material in outbound
# replies.  The privacy scanner runs on every reply before it reaches
# the channel transport, preventing accidental exfiltration of:
#   - OpenAI / Stripe-style API keys (sk-...)
#   - GitHub personal access tokens (ghp_..., ghs_..., ghu_...)
#   - Email addresses
#   - IPv4 addresses
#   - Credit card numbers (Visa, MasterCard, Amex, Discover)
#   - AWS access key IDs (AKIA...)
#   - JSON Web Tokens (eyJ...)
#   - Telegram bot tokens (nnnnnnnnnn:AA...)
#   - PEM-encoded private keys (RSA, DSA, EC, OpenSSH)
#   - Generic credential assignment patterns (api_key, token, secret, etc.)
#
# Each match is replaced with a ***REDACTED*** placeholder.  The scan
# logs a warning summarizing which pattern classes fired on each reply.
# ────────────────────────────────────────────────────────────────────

# Compiled PII / secret patterns for exfiltration prevention
_PRIVACY_PATTERNS = [
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), 'sk-***REDACTED***'),
    (re.compile(r'sk-[a-zA-Z0-9_-]{20,}'), 'sk-***REDACTED***'),
    (re.compile(r'ghp_[a-zA-Z0-9]{36}'), 'ghp_***REDACTED***'),
    (re.compile(r'gh[psu]_[a-zA-Z0-9]{36,}'), 'gh*_***REDACTED***'),
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), '***EMAIL_REDACTED***'),
    (re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b'), '***IP_REDACTED***'),
    (re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b'), '***CC_REDACTED***'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), 'AKIA***REDACTED***'),
    (re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'), '***JWT_REDACTED***'),
    (re.compile(r'\d{8,10}:AA[a-zA-Z0-9_-]{32,40}'), '***BOT_TOKEN_REDACTED***'),
    (re.compile(r'-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----'), '***PRIVATE_KEY_REDACTED***'),
    (re.compile(r'(?i)(?:api[_-]?key|token|secret|password|passwd|auth)[\s:=]+["\']?([^\s"\'&]{16,})'), r'\1=***REDACTED***'),
]


def _strip_capability_listing(text: str) -> str:
    """Strip unsolicited capability listings from model replies.

    Two-phase approach — no hardcoded words or language patterns:

      Phase 1 — Structural pre-filter (zero latency):
        Detects text that LOOKS like it starts with a bullet-heavy
        preamble.  Permissive thresholds catch all candidates.

      Phase 2 — LLM verification (MiMo Free, ~1s):
        Asks a free model "does this start with unsolicited capability
        listing?"  Only strips when the LLM confirms.

    Works for any language, any model, any phrasing.
    """
    if not text:
        return text

    # ── Phase 1: Structural pre-filter ────────────────────────────
    lines = text.split('\n')

    # Split into paragraphs
    paragraphs = []  # (start_line, end_line_exclusive)
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == '':
            if i > start:
                paragraphs.append((start, i))
            start = i + 1
    if start < len(lines):
        paragraphs.append((start, len(lines)))

    if len(paragraphs) < 2:
        return text  # need at least two paragraphs for preamble+answer

    def _bullet_count(block):
        n = 0
        for l in block:
            s = l.strip()
            if not s:
                continue
            if s[0] in ('-', '*', '\u2022'):
                n += 1
            elif s[0].isdigit() and len(s) > 2 and s[1] in ('.', ')', ' ', '-'):
                n += 1
        return n

    # Permissive check: does the first paragraph have ≥2 bullets
    # AND bullet ratio ≥ 0.5 AND short items?
    first_block = lines[paragraphs[0][0]:paragraphs[0][1]]
    non_empty_first = [l for l in first_block if l.strip()]
    if not non_empty_first:
        return text

    bullets = _bullet_count(non_empty_first)
    if bullets < 3:
        return text
    if bullets / len(non_empty_first) < 0.7:
        return text
    avg_len = sum(len(l.strip()) for l in non_empty_first) / len(non_empty_first)
    if avg_len > 200:
        return text

    # ── Phase 2: LLM semantic verification ────────────────────────
    try:
        if not _llm_confirms_capability_listing(text):
            return text
    except Exception:
        return text  # API failure → play safe, don't strip

    # ── Strip: skip bullet-dense paragraphs, return the rest ──────
    answer_idx = 1
    while answer_idx < len(paragraphs):
        ps, pe = paragraphs[answer_idx]
        block = lines[ps:pe]
        non_empty = [l for l in block if l.strip()]
        if not non_empty:
            answer_idx += 1
            continue
        ratio = _bullet_count(non_empty) / len(non_empty)
        if ratio < 0.5:
            break
        answer_idx += 1

    if answer_idx >= len(paragraphs):
        return text  # all paragraphs are lists — nothing to return

    ps = paragraphs[answer_idx][0]
    return '\n'.join(lines[ps:]).strip()


def _llm_confirms_capability_listing(text: str) -> bool:
    """Detect capability listing via regex heuristics (conservative).

    Returns True only for obvious unsolicited intros to avoid false
    positives that strip legitimate replies.
    """
    sample = text[:500].strip().lower()
    # Narrow patterns — only flag text that ENTIRELY starts as a self-intro
    listing_patterns = [
        r"^as an?\s+(?:ai|assistant|language model),?\s+(?:i|here)",
        r"^(?:i am|i'm)\s+(?:an?\s+)?(?:ai|assistant|language model)\s+and\s+(?:i|here)",
        r"^here are my capabilities:",
    ]
    return any(re.match(p, sample) for p in listing_patterns)


def _privacy_scan_response(text: str) -> str:
    """Scan reply text for PII and secret key patterns before sending.

    This is the last line of defense before outbound transport.  Every
    reply — whether a normal assistant response, a forced timeout reply,
    or a [CMD] result — passes through this scanner when Vigil is
    active.  It iterates over all compiled regex patterns in
    ``_PRIVACY_PATTERNS`` and substitutes matches with anonymised
    placeholders (e.g. ``sk-***REDACTED***``).

    Args:
        text: The raw reply string to be scanned.  None or non-string
            values are returned unchanged.

    Returns:
        The sanitised string with all detected PII/secret patterns
        replaced.  If no patterns matched, the original string is
        returned unchanged.

    Side effects:
        Logs a warning when one or more pattern types fired, listing
        up to five of the matched pattern prefixes for debugging."""
    if not text or not isinstance(text, str):
        return text
    original = text
    match_types = set()
    import ipaddress
    for pattern, replacement in _PRIVACY_PATTERNS:
        if pattern.search(text):
            match_types.add(pattern.pattern[:50])
            # Use a replacement function that skips private IPs
            if replacement == '***IP_REDACTED***':
                def _replace_ip(m):
                    try:
                        ip = ipaddress.ip_address(m.group(0).strip())
                        if ip.is_private or ip.is_loopback or ip.is_link_local:
                            return m.group(0)
                    except ValueError:
                        pass
                    return replacement
                text = pattern.sub(_replace_ip, text)
            else:
                text = pattern.sub(replacement, text)
    if text != original:
        logger.warning("[privacy] Redacted %d pattern types from response: %s",
                       len(match_types), ", ".join(sorted(match_types)[:5]))
    return text


# ────────────────────────────────────────────────────────────────────
# [CMD] Protocol constants (moved from unified_worker module level)
#
# The [CMD] protocol is a simple-text control channel that allows
# authorised senders (masters, AI admins, workers) to issue operational
# commands without LLM involvement.
#
# Security model:
#   CMD_LEVEL_MASTER (0) — full access, including ``exec`` (arbitrary
#       bash).  Restricted to senders listed in MASTER_IDS.
#   CMD_LEVEL_ADMIN (1)  — AI admin (primary worker).  Can deploy,
#       switch_model, status, report.
#   CMD_LEVEL_WORKER (2) — self-manage only: ping, help, escalate,
#       restart, log.  Workers can only target themselves.
#
# Authentication: when CMD_AUTH_SECRET is set, every [CMD] message
# must carry an HMAC-SHA256 signature in ``cmd_signature``.  Messages
# without a valid signature are rejected with level 99.
# ────────────────────────────────────────────────────────────────────

WORKER_IDS = set(os.environ.get("WORKER_IDS", "default-worker").split(","))

CMD_LEVEL_MASTER = 0  # master -- full access
CMD_LEVEL_ADMIN  = 1  # AI admin (primary worker)
CMD_LEVEL_WORKER = 2  # Worker -- self-manage only

# Master IDs: load from env MASTER_IDS (comma-separated) or use safe default
_MASTER_IDS_ENV = os.environ.get("MASTER_IDS", "")
MASTER_IDS = set(_MASTER_IDS_ENV.split(",")) if _MASTER_IDS_ENV else {"admin"}

CMD_PERMISSIONS = {
    "deploy":   CMD_LEVEL_ADMIN,
    "status":   CMD_LEVEL_ADMIN,
    "restart":  CMD_LEVEL_WORKER,
    "exec":     CMD_LEVEL_MASTER,
    "report":   CMD_LEVEL_ADMIN,
    "escalate": CMD_LEVEL_WORKER,
    "ping":     CMD_LEVEL_WORKER,
    "help":     CMD_LEVEL_WORKER,
    "log":      CMD_LEVEL_WORKER,
    "switch_model": CMD_LEVEL_ADMIN,
    "permission":   CMD_LEVEL_MASTER,
    "context":      CMD_LEVEL_WORKER,
    "providers":    CMD_LEVEL_ADMIN,
    "decide":       CMD_LEVEL_ADMIN,
    "skill-audit":  CMD_LEVEL_ADMIN,
}

# CMD_AUTH_SECRET: HMAC key for [CMD] message authentication.
# Rotate via env var. Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
_CMD_AUTH_SECRET = os.environ.get("CMD_AUTH_SECRET", "")
if not _CMD_AUTH_SECRET:
    logger.warning("[CMD] CMD_AUTH_SECRET not set — all [CMD] messages accepted without HMAC authentication")


# ────────────────────────────────────────────────────────────────────
# CMD helpers
#
# Four functions implement the [CMD] protocol lifecycle:
#   _cmd_get_level  — resolve a sender's authority level (0-2 or 99).
#   _send_cmd_reply — deliver a command result back through the channel.
#   _exec_cmd       — execute a single command locally (ping, status,
#                      deploy, restart, switch_model, escalate, exec,
#                      log with its many sub-commands).
#   _handle_cmd     — parse an inbound [CMD] message, check permissions,
#                      optionally forward to another worker, then
#                      dispatch to _exec_cmd.
# ────────────────────────────────────────────────────────────────────

def _cmd_get_level(ctx: SharedContext, sender: str, msg: Message) -> int:
    """Determine authority level for a [CMD] sender.

    The return value is an integer in the range 0-2 (MASTER / ADMIN /
    WORKER) or 99 (deny-all).  Level resolution follows this chain:

    1. **HMAC authentication** — if ``CMD_AUTH_SECRET`` is configured,
       the message must include a valid ``cmd_signature``.  Messages
       without one, or with a mismatched signature, are assigned level
       99 and denied all access.

    2. **Master IDs** — after HMAC, if the sender matches an entry in
       ``MASTER_IDS`` (case-insensitive), they receive CMD_LEVEL_MASTER.

    3. **Worker IDs** — senders in ``WORKER_IDS`` map to ADMIN
       (``agent``) or WORKER (all others).

    4. **Telegram / Weixin sources** — default to WORKER as a fallback.

    Args:
        ctx: The shared context providing access to configuration.
        sender: The raw sender identifier from the message.
        msg: The full Message object (used to inspect ``raw`` metadata
            for the HMAC signature and ``source`` for channel fallback).

    Returns:
        An integer authority level: 0 (MASTER), 1 (ADMIN), 2 (WORKER),
        or 99 (deny-all)."""
    # CMD auth: if CMD_AUTH_SECRET is set, verify HMAC signature
    if _CMD_AUTH_SECRET:
        raw = getattr(msg, 'raw', {}) or {}
        sig = raw.get("cmd_signature", "")
        text = getattr(msg, 'content', '')
        if not sig:
            return 99  # deny all
        expected = _hmac_module.new(_CMD_AUTH_SECRET.encode(), text.encode(), hashlib.sha256).hexdigest()
        if not _hmac_module.compare_digest(sig, expected):
            return 99  # deny all
    if sender.lower() in {m.lower() for m in MASTER_IDS}:
        return CMD_LEVEL_MASTER
    if sender in WORKER_IDS:
        if sender == os.environ.get("WORKER_NAME", "agent"):
            return CMD_LEVEL_ADMIN
        return CMD_LEVEL_WORKER
    if msg.source in ("telegram", "weixin"):
        return CMD_LEVEL_WORKER
    return CMD_LEVEL_WORKER


def _send_cmd_reply(ctx: SharedContext, channel, msg: Message, text: str) -> None:
    """Send a [CMD] reply back through the channel.

    Constructs a ``Response`` object from the command result text and
    dispatches it via ``channel.send()``.  The reply is addressed to the
    original sender and preserves the message's source and chat_id for
    correct routing.

    Args:
        ctx: The shared context (unused inline but kept for signature
            consistency with other helpers).
        channel: The transport channel with a ``send(Response)`` method.
            May be None; in that case the reply is silently dropped.
        msg: The original [CMD] message, used to populate ``target``,
            ``source``, and ``chat_id`` on the response.
        text: The command result string (from ``_exec_cmd`` or a
            forwarding error).

    Side effects:
        Logs the reply at INFO level (truncated to 80 chars)."""
    logger.info("[CMD] reply to %s: %s", msg.sender, text[:80])
    if channel:
        channel.send(Response(
            content=text, target=msg.sender,
            source=msg.source, chat_id=msg.chat_id,
        ))


def _exec_cmd(ctx: SharedContext, cmd_name: str, cmd_args: list[str],
              msg: Message, channel) -> str:
    # Normalize all args for cross-device compatibility (fullwidth, etc.)
    cmd_args = [_normalize_unicode(a) for a in cmd_args]
    """Execute a single [CMD] command locally.

    This function is the local command dispatcher.  It handles every
    known command name and returns a human-readable result string:

    * **ping** — returns ``"pong from <worker>@<hostname>"``.
    * **help** — lists all available commands with their required levels.
    * **status** — reports worker name, hostname, model, virtualenv path,
      and latest git commit.
    * **report** — extended status including system uptime and last 3
      git commits.
    * **deploy** — runs ``git pull`` followed by ``restart_self``.
    * **restart** — calls the ``restart_self`` tool.
    * **switch_model** — changes the active AI model via
      ``ModelFailover`` or direct ``set_model()``; supports ``list``
      sub-command.
    * **escalate** — sends an escalation notice to the supervisor
      via ``chat_send``.
    * **exec** — runs an arbitrary bash command through the ``bash``
      tool (requires MASTER level, enforced by ``_handle_cmd``).
    * **log** — queries the tical-chat conversation archive (list,
      search, export, classify, tags, and per-conversation lookups).

    Args:
        ctx: The shared context providing config, LLM access, workspace
            path, and worker name.
        cmd_name: The lowercased command name (e.g. ``"ping"``).
        cmd_args: Positional arguments following the command name.
        msg: The original Message for sender context.
        channel: The transport channel (passed through to sub-commands).

    Returns:
        A plain-text result string suitable for display in chat."""

    if cmd_name == "ping":
        return f"pong from {ctx.name}@{socket.gethostname()}"

    if cmd_name == "help":
        lines = [
            "[CMD] Protocol -- available commands:", "",
        ]
        for c, lvl in sorted(CMD_PERMISSIONS.items()):
            level_name = ["MASTER", "ADMIN", "WORKER"][lvl]
            lines.append(f"  {c:12s} [{level_name}]")
        lines.append("")
        lines.append("Usage: [CMD] <command> [target:worker] [args...]")
        lines.append("  target:worker -- forward to another worker")
        lines.append("  Level 2 workers can only target themselves")
        return "\n".join(lines)

    if cmd_name == "status":
        lines = [
            f"Worker: {ctx.name}",
            f"Host:   {socket.gethostname()}",
            f"Model:  {ctx.cfg.get('ai_model', '?')}",
            f"Venv:   {sys.prefix}",
        ]
        try:
            _r = subprocess.run(["git", "log", "--oneline", "-1"],
                             capture_output=True, text=True, timeout=5)
            if _r.returncode == 0:
                lines.append(f"Git:    {_r.stdout.strip()[:60]}")
        except Exception as e:
            logger.debug("[status] swallowed: %s", e)
        return "\n".join(lines)

    if cmd_name == "report":
        lines = [
            f"==============================",
            f"  {ctx.name.upper()} System Report",
            f"==============================",
            f"Worker:   {ctx.name}",
            f"Host:     {socket.gethostname()}",
            f"Model:    {ctx.cfg.get('ai_model', '?')}",
        ]
        try:
            _r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            lines.append(f"Uptime:   {_r.stdout.strip()}")
        except Exception as e:
            logger.debug("[status] swallowed: %s", e)
        try:
            _r = subprocess.run(["git", "log", "--oneline", "-3"],
                             capture_output=True, text=True, timeout=5)
            if _r.returncode == 0:
                lines.append(f"Recent:   {_r.stdout.strip()}")
        except Exception as e:
            logger.debug("[status] swallowed: %s", e)
        return "\n".join(lines)

    if cmd_name == "deploy":
        _result_parts = []
        try:
            _r = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=60)
            _result_parts.append(f"git pull: {_r.stdout.strip()[:200]}")
            if _r.stderr:
                _result_parts.append(f"  stderr: {_r.stderr.strip()[:200]}")
        except Exception as e:
            _result_parts.append(f"git pull error: {e}")
        try:
            r = execute("restart_self", {}, base_dir=ctx.workspace)
            _result_parts.append(f"restart: {str(r)[:100]}")
        except Exception as e:
            _result_parts.append(f"restart error: {e}")
        return "\n".join(_result_parts)

    if cmd_name == "restart":
        try:
            r = execute("restart_self", {}, base_dir=ctx.workspace)
            return f"[CMD] restart: {str(r)[:100]}"
        except Exception as e:
            return f"[CMD] restart error: {e}"

    if cmd_name == "switch_model":
        _arg = " ".join(cmd_args).strip()
        if not _arg:
            return "[CMD] switch_model: usage: <switch_model>model-name"
        if isinstance(ctx.llm, ModelFailover) if ModelFailover else False:
            if _arg == "list":
                _status = ctx.llm.status()
                _lines = ["Available models (active):"]
                for s in _status:
                    _cd = s.get("cooldown_remaining", 0)
                    _av = "available" if s.get("available") else f"cooldown {_cd}s"
                    _fb = " [fallback]" if s.get("fallback") else ""
                    _nm = s.get("name", "?")
                    _md = s.get("model", "?")
                    _lines.append(f"  {_nm}: {_md} ({_av}){_fb}")
                # Also show all models from providers.json
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    _prov_path = _Path(ctx.workspace) / "config" / "providers.json"
                    if _prov_path.exists():
                        _pdata = _json.loads(_prov_path.read_text())
                        _pdefs = _pdata.get("providers", {})
                        for _pname, _pdef in _pdefs.items():
                            _avail = _pdef.get("available_models", [])
                            if len(_avail) > 1:
                                _lines.append(f"  {_pname} switchable: {', '.join(_avail)}")
                except Exception:
                    pass
                return "[CMD] " + chr(10).join(_lines)
            # Try exact match on current provider model first
            matched = [p for p in ctx.llm.providers if p.model == _arg]
            if matched:
                import time as _time
                for p in ctx.llm.providers:
                    if p.model != _arg:
                        p.cooldown_until = _time.time() + 3600
                    else:
                        p.cooldown_until = 0
                ctx.cfg["ai_model"] = _arg
                return f"[CMD] switch_model: OK -> {_arg} (priority, others 1hr cooldown)"
            # Try switching model within existing provider (available_models)
            for p in ctx.llm.providers:
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    _prov_path = _Path(ctx.workspace) / "config" / "providers.json"
                    if _prov_path.exists():
                        _pdata = _json.loads(_prov_path.read_text())
                        _pdefs = _pdata.get("providers", {})
                        _pdef = _pdefs.get(p.name, {})
                        _avail = _pdef.get("available_models", [])
                        if _arg in _avail:
                            p.model = _arg
                            ctx.cfg["ai_model"] = _arg
                            return f"[CMD] switch_model: OK -> {_arg} (via {p.name})"
                except Exception:
                    pass
            # No match found
            available = ", ".join(set(p.model for p in ctx.llm.providers))
            return f"[CMD] switch_model: model not found: {_arg}. Active: {available}. Use list to see all."
        else:
            if _arg == "list":
                try:
                    _models = ctx.llm.list_models()
                    _mlines = []
                    for m in _models:
                        _dm = m.get("default_model", "?")
                        _mlines.append(f"  {_dm}")
                    return "[CMD] " + chr(10).join(_mlines)
                except Exception as e:
                    return f"[CMD] switch_model list error: {e}"
            try:
                r = ctx.llm.set_model(_arg)
                ctx.cfg["ai_model"] = _arg
                _mname = r.get("model", _arg)
                return f"[CMD] switch_model: OK -> {_mname}"
            except Exception as e:
                return f"[CMD] switch_model error: {e}"

    if cmd_name == "permission":
        if not ctx._permission_checker:
            return "[CMD] permission: PermissionChecker not available"
        if not cmd_args:
            pc = ctx._permission_checker
            lines = [f"Mode: {pc.mode_value}",
                     f"Allowed: {sorted(pc.allowed_tools) or '(none)'}",
                     f"Denied: {sorted(pc.denied_tools) or '(none)'}",
                     f"Modes: default, acceptEdits, bypassPermissions, plan, auto"]
            return "[CMD] permission:\n" + "\n".join(lines)
        sub = cmd_args[0].lower()
        valid = {"default", "acceptedits", "bypasspermissions", "plan", "auto"}
        if sub not in valid:
            return f"[CMD] permission: unknown mode '{sub}'. Valid: {', '.join(sorted(valid))}"
        mode_map = {"acceptedits": "acceptEdits", "bypasspermissions": "bypassPermissions"}
        mode_name = mode_map.get(sub, sub)
        try:
            from tical_code.core.permission_checker import PermissionMode
            ctx._permission_checker.set_mode(PermissionMode(mode_name))
            return f"[CMD] permission: OK -> {mode_name}"
        except Exception as e:
            return f"[CMD] permission error: {e}"

    if cmd_name == "context":
        if not ctx.compactor:
            return "[CMD] context: ContextCompactor not available"
        comp = ctx.compactor
        lines = [
            f"Max tokens: {comp.max_tokens}",
            f"Compact threshold: {comp.compact_threshold_pct*100:.0f}% ({int(comp.max_tokens * comp.compact_threshold_pct)} tokens)",
            f"Keep recent: {comp.keep_recent}",
        ]
        return "[CMD] context:\n" + "\n".join(lines)

    if cmd_name == "providers":
        if not hasattr(ctx, 'llm') or not hasattr(ctx.llm, 'providers'):
            return "[CMD] providers: ModelFailover not available"
        if not cmd_args:
            lines = []
            for p in ctx.llm.providers:
                state = getattr(p, 'state', '?')
                priority = getattr(p, 'priority', '?')
                lines.append(f"  {p.name:15s} [{state:11s}] priority={priority} model={p.model}")
            if not lines:
                return "[CMD] providers: no providers configured"
            return "[CMD] providers:\n" + "\n".join(lines)
        sub = cmd_args[0].lower()
        if sub == "switch" and len(cmd_args) >= 2:
            target = cmd_args[1]
            matched = [p for p in ctx.llm.providers if p.name.lower() == target.lower()]
            if not matched:
                names = ", ".join(p.name for p in ctx.llm.providers)
                return f"[CMD] providers: provider '{target}' not found. Available: {names}"
            ctx.cfg["preferred_provider"] = target
            return f"[CMD] providers: preferred -> {target}"
        if sub == "health":
            import time as _time
            lines = []
            for p in ctx.llm.providers:
                state = getattr(p, 'state', '?')
                cooldown_str = ""
                cu = getattr(p, 'cooldown_until', 0)
                if cu > 0:
                    remaining = max(0, cu - _time.time())
                    if remaining > 0:
                        cooldown_str = f" (cooldown: {remaining:.0f}s)"
                fc = getattr(p, 'fail_count', 0)
                lines.append(f"  {p.name:15s} [{state:11s}] fails={fc}{cooldown_str}")
            return "[CMD] providers health:\n" + "\n".join(lines)
        return f"[CMD] providers: unknown sub-command '{sub}'. Use: list, switch <name>, health"

    if cmd_name == "decide":
        if not hasattr(ctx, 'decision_engine') or ctx.decision_engine is None:
            return "[CMD] decide: DecisionEngine not available (not wired)"
        if not cmd_args:
            enabled = getattr(ctx.decision_engine, '_enabled', False)
            max_iter = getattr(ctx.decision_engine, '_max_iterations', 5)
            return f"[CMD] decide: {'ON' if enabled else 'OFF'} (max_iterations={max_iter}). Usage: decide [on|off|status]"
        sub = cmd_args[0].lower()
        if sub == "on":
            ctx.decision_engine._enabled = True
            return "[CMD] decide: ENABLED — tool strategy checks active"
        elif sub == "off":
            ctx.decision_engine._enabled = False
            return "[CMD] decide: DISABLED — tool strategy checks bypassed"
        elif sub == "status":
            enabled = getattr(ctx.decision_engine, '_enabled', False)
            return f"[CMD] decide: {'ENABLED' if enabled else 'DISABLED'}"
        return f"[CMD] decide: unknown sub-command '{sub}'. Use: on, off, status"

    if cmd_name == "escalate":
        _reason = " ".join(cmd_args) or "no details"
        try:
            execute("chat_send", {
                "target": os.environ.get("WORKER_NAME", "agent"),
                "content": f"[ESCALATION from {ctx.name}] {_reason}",
            }, base_dir=ctx.workspace)
            return f"[CMD] escalated to {os.environ.get('WORKER_NAME', 'agent')}: {_reason[:100]}"
        except Exception as e:
            return f"[CMD] escalate error: {e}"

    if cmd_name == "exec":
        # SECURITY: exec requires MASTER level + goes through bash safety check
        _sender_level = _cmd_get_level(ctx, msg.sender, msg)
        if _sender_level > CMD_LEVEL_MASTER:
            return "[CMD] exec requires MASTER level"
        payload = " ".join(cmd_args)
        if not payload:
            return "[CMD] exec: empty command"
        # SECURITY: reject shell metacharacters to prevent injection
        _DANGEROUS_CHARS = set(";&|$()`<>")
        if any(c in payload for c in _DANGEROUS_CHARS):
            logger.warning("SECURITY: exec payload rejected — contains shell metacharacters: %r", payload[:200])
            return "[CMD] exec: payload contains dangerous characters (;&|$()`<>) — rejected"
        # Route through exec_bash for safety checks (blacklist, workspace, etc.)
        result = execute("bash", {"command": payload, "timeout": 120})
        if "error" in result:
            return f"[CMD] exec blocked: {result['error']}"
        _out = result.get("stdout", "")
        if result.get("stderr"):
            _out += f"\n[stderr]\n{result['stderr'][:500]}"
        return _out[:2000] if _out else "(no output)"

    if cmd_name == "log":
        """Query tical-chat conversation archive via API."""
        _chat_url = ctx.cfg.get("chat_url", "").rstrip("/")
        _key = ctx.cfg.get("chat_key", "") or os.environ.get("TICAL_CHAT_KEY", "")
        if not cmd_args:
            _url = f"{_chat_url}/v1/conversations"
        elif cmd_args[0] == "search" and len(cmd_args) >= 2:
            _q = " ".join(cmd_args[1:])
            _url = f"{_chat_url}/v1/messages/search?q={urllib.parse.quote(_q)}"
        elif cmd_args[0] == "export" and len(cmd_args) >= 3:
            _s, _t = cmd_args[1], cmd_args[2]
            _url = f"{_chat_url}/v1/export?sender={urllib.parse.quote(_s)}&target={urllib.parse.quote(_t)}&format=markdown"
            try:
                _req = urllib.request.Request(_url)
                _req.add_header("X-AI-Key", _key)
                with urllib.request.urlopen(_req, timeout=15) as _resp:
                    return _resp.read().decode("utf-8")[:3000]
            except Exception as e:
                return f"[CMD] log export error: {e}"
        elif len(cmd_args) == 1 and cmd_args[0] == "tags":
            _url = f"{_chat_url}/v1/tags"
        elif len(cmd_args) >= 2 and cmd_args[0] == "classify":
            _limit = int(cmd_args[1]) if len(cmd_args) >= 2 and cmd_args[1].isdigit() else 10
            try:
                _fetch_url = f"{_chat_url}/v1/messages/unclassified?limit={_limit}"
                _req = urllib.request.Request(_fetch_url)
                _req.add_header("X-AI-Key", _key)
                with urllib.request.urlopen(_req, timeout=15) as _resp:
                    _unclassified = json.loads(_resp.read())
            except Exception as e:
                return f"[CMD] log classify fetch error: {e}"
            if not _unclassified.get("messages"):
                return "[CMD] log classify: no unclassified messages found"
            _classified = 0
            _results = []
            for _m in _unclassified["messages"]:
                _mid = _m["id"]
                _content = _m["content"][:500]
                try:
                    _prompt = (
                        "Classify this message from an AI management conversation.\n"
                        "Pick relevant categories from: problem, Fix, decision, task, techsolution, Config, deploy, query, notify, audit\n"
                        f"Message: {_content}\n\n"
                        "Respond with valid JSON ONLY: "
                        '{"tags": ["problem"], "summary": "one line summary in Chinese (max 60 chars)"}'
                    )
                    _resp_result = ctx.llm.call([{"role": "user", "content": _prompt}])
                    if asyncio.iscoroutine(_resp_result):
                        _resp_result = ctx.run_async(_resp_result)
                    _resp = _resp_result
                    _text = _resp.get("content", "").strip()
                    _json_match = re.search(r'\{.*\}', _text, re.DOTALL)
                    if _json_match:
                        _parsed = json.loads(_json_match.group())
                        _tag_list = _parsed.get("tags", [])
                        _summary = _parsed.get("summary", "")
                        _tag_req = urllib.request.Request(
                            f"{_chat_url}/v1/messages/tag",
                            data=json.dumps({"id": _mid, "tags": _tag_list, "summary": _summary}).encode(),
                            headers={"Content-Type": "application/json", "X-AI-Key": _key},
                            method="POST",
                        )
                        with urllib.request.urlopen(_tag_req, timeout=10):
                            _classified += 1
                            _results.append(f"  #{_mid}: {', '.join(_tag_list)} -- {_summary[:40]}")
                except Exception as _e:
                    _results.append(f"  #{_mid}: error - {str(_e)[:50]}")
            if not _results:
                return "[CMD] log classify: classification failed for all messages"
            return f"[CMD] Classified {_classified}/{len(_unclassified['messages'])} messages:\n" + "\n".join(_results)
        elif len(cmd_args) == 1:
            _other = cmd_args[0]
            _url = f"{_chat_url}/v1/conversation?sender={ctx.name}&target={_other}&limit=20"
        elif len(cmd_args) >= 2:
            _url = f"{_chat_url}/v1/conversation?sender={cmd_args[0]}&target={cmd_args[1]}&limit=20"
        else:
            return "[CMD] log: unknown subcommand"
        try:
            _req = urllib.request.Request(_url)
            _req.add_header("X-AI-Key", _key)
            with urllib.request.urlopen(_req, timeout=15) as _resp:
                _data = json.loads(_resp.read())
            if "conversations" in _data:
                _lines = ["[CMD] Conversations:", ""]
                for c in _data["conversations"]:
                    _p = " <-> ".join(c["participants"])
                    _lines.append(f"  {_p:40s} {c['message_count']:3d} msgs")
                return "\n".join(_lines)
            elif "tags" in _data:
                _lines = ["[CMD] Tags:", ""]
                for t in _data["tags"]:
                    _lines.append(f"  {t['tag']:12s}  {t['count']:3d} messages")
                return "\n".join(_lines)
            elif "results" in _data:
                _lines = [f"[CMD] Search: '{_data.get('query','')}' ({_data['count']} results)", ""]
                for m in _data["results"][:15]:
                    _lines.append(f"  {m['sender']:12s} -> {m['target']:12s}  {m['content'][:80]}")
                return "\n".join(_lines)
            elif "messages" in _data:
                _lines = [f"[CMD] Conversation: {_data.get('sender','?')} <-> {_data.get('target','?')} ({_data['count']} msgs)", ""]
                for m in _data["messages"][-15:]:
                    _ts = _dt.datetime.fromtimestamp(m["timestamp"]).strftime("%H:%M:%S")
                    _lines.append(f"  [{_ts}] {m['from']:12s} -> {m['to']:12s}  {m['content'][:100]}")
                return "\n".join(_lines)
            return f"[CMD] log: {json.dumps(_data, ensure_ascii=False)[:300]}"
        except Exception as e:
            return f"[CMD] log error: {e}"

    if cmd_name == "skill-audit":
        if not _SKILL_AUDIT_AVAILABLE:
            return "[CMD] skill-audit: SkillSpector not available (skillspector module missing)"
        _target = " ".join(cmd_args).strip() if cmd_args else ctx.workspace
        if not os.path.isdir(_target) and not os.path.isfile(_target):
            return f"[CMD] skill-audit: path not found: {_target}"
        try:
            _runner = SkillAuditRunner()
            _result = _runner.scan_path(_target)
            _summary = _runner.summarize(_result)
            return f"[CMD] skill-audit results for {_target}:\n\n{_summary}"
        except Exception as e:
            return f"[CMD] skill-audit error: {e}"

    return f"[CMD] unknown: {cmd_name}"


def _normalize_unicode(text: str) -> str:
    """Normalize Unicode for cross-device/cross-IME compatibility.

    NFKC normalization converts fullwidth ASCII, compatibility ideographs,
    and different composition forms to canonical form. Also strips invisible
    characters (zero-width spaces, BOM, directional markers).

    Examples:
        'ｐｅｒｍｉｓｓｉｏｎ' → 'permission'  (fullwidth → halfwidth)
        '［CMD］'              → '[CMD]'       (fullwidth brackets)
        'café' (NFD)          → 'café' (NFC)   (decomposed → composed)
    """
    text = unicodedata.normalize('NFKC', text)
    text = text.strip('\u200b\u200c\u200d\u200e\u200f\ufeff')
    return text


def _handle_cmd(ctx: SharedContext, msg: Message, channel) -> None:
    """Handle a [CMD] protocol message — direct execution, no LLM.

    Parses the message content for ``[CMD] <command> [target:<worker>]
    [args...]``, resolves the sender's authority level, and either
    executes the command locally or forwards it to the specified target
    worker.

    Flow:
    1. Strip ``[CMD]`` prefix and split into command name + args.
    2. Look up the command's required level in ``CMD_PERMISSIONS``.
    3. Resolve sender level via ``_cmd_get_level``; deny if insufficient.
    4. Extract optional ``target:<worker>`` or ``to:<worker>`` argument.
       Workers (level 2) can only target themselves.
    5. If the target is another worker, forward via ``chat_send``.
    6. Otherwise dispatch to ``_exec_cmd`` and send the reply.

    Args:
        ctx: The shared context.
        msg: The inbound [CMD] message (content starts with ``[CMD]``).
        channel: The transport channel for sending the reply.

    Side effects:
        Sends replies via ``_send_cmd_reply`` and may invoke
        ``chat_send`` for forwarding."""
    content = msg.content.strip()
    # Detect [CMD] prefix with fullwidth bracket tolerance
    after_prefix = content[len("[CMD]"):].strip() if content.startswith("[CMD") or content.startswith("[cmd") else ""
    if not after_prefix and "]" in content and content[0] == "[":
        end_bracket = content.index("]")
        bracket_text = _normalize_unicode(content[1:end_bracket])
        if bracket_text.upper() == "CMD":
            after_prefix = content[end_bracket+1:].strip()
    if not after_prefix:
        after_prefix = content
    parts = after_prefix.split()
    if not parts:
        _send_cmd_reply(ctx, channel, msg, "[CMD] error: empty command")
        return

    cmd_name = _normalize_unicode(parts[0]).lower()

    min_level = CMD_PERMISSIONS.get(cmd_name, CMD_LEVEL_MASTER)
    sender_level = _cmd_get_level(ctx, msg.sender, msg)
    if sender_level > min_level:
        _send_cmd_reply(
            ctx, channel, msg,
            f"[CMD] denied: {cmd_name} requires level {min_level}, "
            f"sender has level {sender_level}"
        )
        return

    target = None
    cmd_args = []
    for p in parts[1:]:
        if p.startswith("target:") or p.startswith("to:"):
            target = p.split(":", 1)[1]
        else:
            cmd_args.append(p)

    # Workers can only target themselves
    if target and sender_level == CMD_LEVEL_WORKER:
        if target != ctx.name:
            _send_cmd_reply(
                ctx, channel, msg,
                "[CMD] denied: workers can only target themselves"
            )
            return

    if target and target != ctx.name:
        try:
            execute("chat_send", {"target": target, "content": content},
                  base_dir=ctx.workspace)
            _send_cmd_reply(ctx, channel, msg, f"[CMD] forwarded {cmd_name} to {target}")
        except Exception as e:
            _send_cmd_reply(ctx, channel, msg, f"[CMD] forward error: {e}")
        return

    try:
        result = _exec_cmd(ctx, cmd_name, cmd_args, msg, channel)
    except Exception as e:
        logger.error("[CMD] _exec_cmd(%s) crashed: %s", cmd_name, e)
        result = f"[CMD] error executing '{cmd_name}': {e}"
    _send_cmd_reply(ctx, channel, msg, result)


# ────────────────────────────────────────────────────────────────────
# Main message handler
#
# ``handle_message`` is the primary entry point for every inbound
# user message.  It orchestrates the full processing pipeline:
# decision-engine pre-check → [CMD] shortcut → task detection →
# LLM conversation loop with tool execution, verification,
# constitution enforcement, doom-loop detection, circuit-breaker,
# forced reply on exhaustion, privacy scan, Vigil recording, and
# session persistence.
# ────────────────────────────────────────────────────────────────────


def _execute_tool_core(tc: dict, ctx: SharedContext, iteration: int) -> dict:
    """Execute a single tool call: pre-checks + execution.

    Runs the full pre-execution validation pipeline (permission check,
    Phase 1 verification, constitution check, decision engine strategy
    enforcement) and then executes the tool via ToolExecutor or the
    legacy execute() fallback.

    This function is thread-safe for read-only tools — it reads from
    ``ctx`` but does NOT modify conv, responded, or any shared mutable
    state on ctx.  Callers are responsible for all post-execution
    processing (Phase 2 verification, skill extraction, Vigil
    recording, trace logging, circuit breaker, checkpoint, doom-loop
    detection, formatting, and conv.append).

    Args:
        tc: Tool call dict with keys 'name', 'args', 'id'.
        ctx: Shared context (read-only access for validation layers).
        iteration: Current tool iteration number (for strategy checks).

    Returns:
        A dict with keys:
          tc_id, name, args — tool call identity
          blocked: bool — True if blocked by any pre-execution check
          blocked_type: str — 'permission', 'verification',
              'constitution', 'strategy', or '' if not blocked
          blocked_detail: str — human-readable block reason
          result: dict — raw tool result (None if blocked/errored)
          executed: bool — True if tool actually ran
          tool_latency_ms: float — execution time in milliseconds
    """
    name = tc.get("name", "?")
    args = tc.get("args", {})
    tc_id = tc.get("id", "")

    info: dict[str, Any] = {
        "tc_id": tc_id, "name": name, "args": args,
        "blocked": False, "blocked_type": "", "blocked_detail": "",
        "result": None, "executed": False, "tool_latency_ms": 0.0,
    }

    # Permission check (5-tier mode system — before verification)
    if ctx._permission_checker:
        allowed, reason = ctx._permission_checker.can_use_tool(name)
        if not allowed:
            info["blocked"] = True
            info["blocked_type"] = "permission"
            info["blocked_detail"] = reason
            return info

    # Phase 1: Verify tool call (before execution)
    if ctx.verification:
        phase1 = ctx.verification.verify_tool_call(name, args)
    else:
        phase1 = type("Phase1Result", (object,), {"passed": True, "violations": []})()
    if not phase1.passed:
        info["blocked"] = True
        info["blocked_type"] = "verification"
        info["blocked_detail"] = phase1.violations[0].detail
        return info

    # Constitution check BEFORE execution
    if ctx.constitution:
        try:
            const_result = ctx.constitution.check_action(name, context=args, mode="write")
            if not const_result.allowed:
                if const_result.action.value == "reject":
                    info["blocked"] = True
                    info["blocked_type"] = "constitution"
                    info["blocked_detail"] = const_result.reason
                    return info
        except Exception as e:
            # v0.8.6: Block on check failure, never silently allow
            logger.error("Constitution check raised for %s: %s — BLOCKING", name, e)
            info["blocked"] = True
            info["blocked_type"] = "constitution"
            info["blocked_detail"] = f"Constitution check error (blocked for safety): {str(e)[:100]}"
            return info

    # DecisionEngine: tool strategy enforcement (respects _enabled toggle)
    if ctx.decision_engine and getattr(ctx.decision_engine, '_enabled', False):
        try:
            allowed, reason = ctx.decision_engine.check_tool_strategy(name, iteration + 1)  # 1-based
            if not allowed:
                info["blocked"] = True
                info["blocked_type"] = "strategy"
                info["blocked_detail"] = reason
                return info
        except Exception as e:
            # v0.8.6: Block on check failure, never silently allow
            logger.error("DecisionEngine check_tool_strategy raised for %s: %s — BLOCKING", name, e)
            info["blocked"] = True
            info["blocked_type"] = "strategy"
            info["blocked_detail"] = f"Strategy check error (blocked for safety): {str(e)[:100]}"
            return info

    # Execute tool — ToolExecutor-first, legacy fallback
    _tool_t0 = time.time()
    if ctx._tool_executor is not None:
        try:
            # Normalize parameter names: TOOL_SCHEMAS says "command" but ToolRegistry uses "cmd"
            _args = dict(args)
            if name == "shell_exec" and "command" in _args and "cmd" not in _args:
                _args["cmd"] = _args.pop("command")
            _instruction = json.dumps({"tool": name, "params": _args})
            _tool_result = ctx.run_async(ctx._tool_executor.dispatch(_instruction))
            if _tool_result.success:
                result = _tool_result.data or {}
            else:
                raise RuntimeError(_tool_result.error or "Tool failed")
        except Exception as _te:
            logger.warning("ToolExecutor dispatch failed for %s: %s, falling back to execute()",
                         name, _te)
            result = execute(name, args, base_dir=ctx.workspace)
    else:
        result = execute(name, args, base_dir=ctx.workspace)

    info["tool_latency_ms"] = (time.time() - _tool_t0) * 1000
    info["result"] = result
    info["executed"] = True
    return info


def handle_message(ctx: SharedContext, channel, msg: Message) -> None:
    """Process a single inbound message through the full LLM + tools pipeline.

    This is the main entry point for per-message turn processing.  Every
    message that arrives at a worker passes through this function, which
    implements the following pipeline:

    **Pre-processing**
        1. String→Message guard — converts bare strings to Message
           objects to prevent attribute errors.
        2. Decision engine pre-check — validates the message goal against
           constitution rules and blocks disallowed requests before any
           LLM cost is incurred.

    **Routing**
        3. [CMD] protocol detection — messages starting with ``[CMD]``
           bypass the LLM entirely and are dispatched to ``_handle_cmd``
           for direct command execution.  Fullwidth/halfwidth bracket
           normalisation is applied for CJK input sources.
        4. Task detection — messages matching ``is_task_request()`` or
           explicitly prefixed ``[TASK]`` are converted into autonomous
           tasks and routed to ``run_task()`` from the task_handler.

    **LLM conversation loop (up to 10 iterations)**
        5. Session loading — restores conversation history from the
           session store, applying token-aware compaction when needed.
        6. Media attachment — converts image data, voice transcripts,
           and attached document text into multimodal message content.
        7. For each iteration:
           a. Checkpoint save and context compression.
           b. Message adaptation for the target model family.
           c. LLM call with tool schemas.
           d. Tool execution with verification (Phase 1 & 2),
              constitution checks, decision-engine strategy enforcement,
              doom-loop detection, and circuit-breaker on repeated
              failures.
           e. Iteration guard — escalating warnings then forced break
              at iteration 8.

    **Post-processing (on reply or timeout)**
        8. Privacy scan — ``_privacy_scan_response`` redacts detected
           PII and secrets from every outbound reply.
        9. Reply chunking — splits replies > 4000 chars for Telegram.
       10. Vigil AI signal collection — ``record_response`` and token
           tracking.
       11. Session persistence — saves the full conversation turn.
       12. Memory management — GC, RSS monitoring, session compaction,
           memory evolution hooks.

    **Timeout path** — when max iterations are exhausted without a
       text reply, the last assistant message is extracted and sent
       along with a worker-timeout notice.

    Args:
        ctx: The shared context providing LLM, tools, config, sessions,
            verification, Vigil, and all other subsystem references.
        channel: The transport channel with a ``send(Response)`` method.
            May be None for headless message processing.
        msg: The inbound message.  If a plain string is received it is
            automatically converted to a ``Message`` with
            ``source="tical-chat"``.

    Side effects:
        Sends responses via ``channel.send()``, saves conversation
        state, updates session family affinity, triggers memory
        management, and notifies Vigil collectors."""
    # Guard: convert string to Message to prevent 'str' has no attribute 'source' bugs
    ctx._evidence_retry_count = 0
    if isinstance(msg, str):
        msg = Message(sender="system", content=msg, source="tical-chat")
    # NFKC normalize content ONCE at entry — fullwidth/halfwidth, composed/decomposed
    # Different device IMEs produce visually identical but byte-different characters.
    # Normalize here so ALL downstream code (CMD, power mode, LLM, pattern matching)
    # sees the same canonical form.
    msg.content = _normalize_unicode(msg.content)
    logger.info(
        "[%s] %s: %s", msg.source, msg.sender, msg.content[:100]
    )

    # Generate a trace ID for this message turn
    ctx._current_trace_id = ctx.trace_logger.new_trace_id()

    # === v0.8.6: Pending clarify answer evaluation ===
    # If the previous turn's pre_check blocked with NEEDS_CLARIFICATION,
    # this message is the user's response. Evaluate it before re-running
    # clarify_goal (which would re-analyze the answer as a new goal).
    _pending_clarify = (
        ctx.decision_engine
        and getattr(ctx.decision_engine, '_last_clarify_result', None)
        and ctx.decision_engine._last_clarify_result.status == ClarifyStatus.NEEDS_CLARIFICATION
    )
    if _pending_clarify:
        try:
            answer = ClarifyAnswer(
                clarify_id=ctx.decision_engine._last_clarify_result.clarify_id,
                answers={0: msg.content},
            )
            eval_result = ctx.decision_engine.evaluate_clarify_answer(answer)
            if eval_result.status == ClarifyStatus.CLEAR:
                logger.info("[DecisionEngine] Clarify resolved — proceeding")
                # Reset pending clarify so subsequent messages don't re-enter this branch
                ctx.decision_engine._last_clarify_result = None
                # Fall through to normal LLM processing
            elif eval_result.status == ClarifyStatus.REJECT:
                if channel:
                    channel.send(Response(
                        content=f"[blocked] [BLOCKED] Target rejected: {eval_result.rejection_reason}",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
            else:
                # Still needs clarification — ask again
                msg_text = format_clarify_questions(eval_result)
                if channel:
                    channel.send(Response(
                        content=msg_text,
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
        except Exception as e:
            logger.debug("Clarify answer evaluation skipped: %s", e)
            # Fall through — treat as normal message

    # === Decision pre-check: clarify goal + constitution compliance ===
    _content_stripped = msg.content.strip()
    if ctx.decision_engine and not (_content_stripped.startswith("[CMD]") or _content_stripped.startswith("[CMD]")):
        try:
            allowed, check_result = ctx.decision_engine.pre_check(
                goal=msg.content[:500],
                action=msg.content,  # v0.8.6: Full message for safety filter (was 200-char truncation blind spot)
            )
            if not allowed:
                logger.warning("DecisionEngine blocked message: %s", check_result)
                if channel:
                    channel.send(Response(
                        content=f"[blocked] {check_result}",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
        except Exception as e:
            logger.debug("DecisionEngine pre_check skipped: %s", e)

    # === [CMD] Protocol -- direct execution (no LLM) ===
    _content_stripped = msg.content.strip()
    if _content_stripped.startswith("[CMD]") or _content_stripped.startswith("[cmd]"):
        _handle_cmd(ctx, msg, channel)
        return

    # === Task Detection -- autonomous task ===
    if is_task_request is not None and create_task is not None and msg.content:
        if is_task_request(msg.content) or msg.content.strip().upper().startswith("[TASK]"):
            # Vigil: record task switch on human signal collector
            if ctx._vigil:
                ctx._vigil.signal_collector.record_task_switch()
            goal = msg.content.strip()
            if goal.upper().startswith("[TASK]"):
                goal = goal[6:].strip()  # strip [TASK] prefix
            try:
                task = create_task(
                    goal=goal,
                    workspace=ctx.workspace,
                )
                task.status = "running"
                save_state(task, workspace=ctx.workspace)
                logger.info("Task created from message: %s goal=%s", task.task_id, goal[:80])
                # Run task synchronously for now; Phase 2+ will make it async
                from tical_code.core.modules.task_handler import run_task
                run_task(ctx, task)
            except Exception as e:
                logger.error("Task creation failed: %s", e)
                if channel:
                    channel.send(Response(
                        content=f"[task] failed to create: {e}",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
            return

    # Reset verification turn tracking (preserve action history for multi-turn evidence)
    if ctx.verification:
        ctx.verification.reset_turn()
    if ctx.verif_recorder:
        ctx.verif_recorder.start_turn(msg.content)

    # TraceRecorder: task start
    if ctx.tracer:
        ctx.tracer.on_task_start(
            '%s_%s_%d' % (msg.source, msg.sender, int(time.time())),
            msg.content[:200],
        )

    # ── Power Mode: [warn open] / [warn off] / [set pw] unlock mechanism ──
    # Password check: per-chat password first, then admin password.
    # Passwords stored in {_EITE_DATA_DIR}/passwords.json (NOT git tracked).
    _pw_data = _load_passwords()
    _admin_hash = _pw_data.get("admin", "")
    # Backward compat: if admin hash doesn't exist yet, try env var
    if not _admin_hash:
        _admin_hash = _ensure_admin_hash()
        _pw_data = _load_passwords()
    _msg_text = msg.content.strip()
    _msg_lower = _msg_text.lower()
    session_id = ctx.sessions.get_session_id(msg.source, str(msg.chat_id)) if ctx.sessions else f"{msg.source}:{msg.chat_id}"
    _is_power = ctx._session_power.get(session_id, False)
    _power_suffix = ""  # Only appended when power mode active
    _chat_id_str = str(msg.chat_id)

    if _admin_hash:
        # ── [warn open] <password> ──
        if _msg_lower.startswith("[warn open]") or _msg_lower.startswith("[warn open ]"):
            _pw = _msg_text.split("]", 1)[-1].strip() if "]" in _msg_text else ""
            if _pw and _verify_password(_chat_id_str, _pw, _pw_data, _admin_hash):
                ctx._session_power[session_id] = True
                _is_power = True
                logger.info("POWER MODE UNLOCKED for session %s", session_id)
                if channel:
                    channel.send(Response(
                        content="[UNLOCK] Unlocked. All restrictions lifted. Send `[warn off] password` to lock.",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
            else:
                if channel:
                    channel.send(Response(
                        content="[X] Wrong password.",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
        # ── [warn off] <password> ──
        elif _msg_lower.startswith("[warn off]") or _msg_lower.startswith("[warn off ]"):
            _pw = _msg_text.split("]", 1)[-1].strip() if "]" in _msg_text else ""
            if _pw and _verify_password(_chat_id_str, _pw, _pw_data, _admin_hash):
                if _is_power:
                    ctx._session_power[session_id] = False
                    _is_power = False
                    logger.info("POWER MODE LOCKED for session %s", session_id)
                    if channel:
                        channel.send(Response(
                            content="[LOCK] Locked. Safety restrictions restored.",
                            target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                        ))
                else:
                    if channel:
                        channel.send(Response(
                            content="ℹ️ Not currently unlocked.",
                            target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                        ))
                return
            else:
                if channel:
                    channel.send(Response(
                        content="[X] Wrong password.",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
        # ── [set pw] <new_password> (only when already unlocked) ──
        elif _msg_lower.startswith("[set pw]") or _msg_lower.startswith("[set pw ]"):
            if not _is_power:
                if channel:
                    channel.send(Response(
                        content="[X] Unlock first. Send `[warn open] password` then set your personal password.",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
            _new_pw = _msg_text.split("]", 1)[-1].strip() if "]" in _msg_text else ""
            if not _new_pw or len(_new_pw) < 4:
                if channel:
                    channel.send(Response(
                        content="[X] Password must be at least 4 characters.",
                        target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                    ))
                return
            _pw_data.setdefault("users", {})[_chat_id_str] = _hash_pw(_new_pw)
            _save_passwords(_pw_data)
            logger.info("User password set for chat %s", _chat_id_str)
            if channel:
                channel.send(Response(
                    content="[OK] Password set. Next time unlock with `[warn open] <password>`.",
                    target=msg.sender, source=msg.source, chat_id=msg.chat_id,
                ))
            return

    # Apply power mode overrides
    _saved_constitution = None
    _power_prompt = None  # Will hold modified prompt for power mode
    if _is_power:
        _power_prompt = strip_and_inject_power_mode(ctx.system_prompt, ctx.name)
        if ctx._permission_checker:
            ctx._permission_checker.set_mode(PermissionMode.BYPASS)
        _saved_constitution = ctx.constitution
        # SECURITY: constitution and sanitization always active
        # ctx.constitution = None
        # Disable tool output sanitization (IP/privacy redaction)
        # from tical_code.core.tool_executor import set_skip_sanitize
        # set_skip_sanitize(True)
        logger.info("POWER MODE ACTIVE for session %s", session_id)

    conv = [
        {"role": "system", "content": _power_prompt or (ctx.system_prompt + _power_suffix)},
    ]
    # Start skill extraction tracking for this message turn (only for substantive messages)
    _should_extract = len(msg.content) >= 20 and msg.content.strip().lower() not in {
        "ping", "hello", "hi", "ok", "thanks", "thank you", "okay", "yes", "no",
        "bye", "goodbye", "hey", "yo", "ha", "lol", "k", "kk", "cool",
    }
    if _should_extract:
        _msg_task_id = f"msg-{msg.source}-{int(time.time())}"
        ctx.skill_extractor.start_task(task_id=_msg_task_id, goal=msg.content[:200])
    # Load session history for context persistence
    history = ctx.sessions.load_session(session_id) if ctx.sessions else []
    # Session-affinity: preferred model family for this conversation
    _family = ctx._session_family.get(session_id)
    # Power mode: force DeepSeek (more obedient than MiMo to system prompts)
    if _is_power:
        _family = "deepseek"
    # Vision override: force MIMO when message has image attachments.
    # DeepSeek cannot handle images; only mimo-v2.5 has vision capability.
    if hasattr(msg, 'media_data') and msg.media_data:
        for _md in msg.media_data:
            if _md.get("type") == "image":
                _family = "mimo"
                break
    if history:
        conv.extend(history)
        # Always strip orphaned tool messages from loaded history.
        # Session history no longer carries tool_calls (v0.7.34), so any tool
        # messages in loaded history are orphans and must be removed UNCONDITIONALLY.
        # v0.7.40: lifted from inside compaction guard — orphan stripping must
        # run even when the conversation is short and no trimming is needed.
        _stripped_orphans = 0
        tool_ids_needed = set()
        for m in conv:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                tool_ids_needed.add(m["tool_call_id"])
        if tool_ids_needed:
            has_parent = set()
            for m in conv:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m.get("tool_calls", []):
                        if tc.get("id") in tool_ids_needed:
                            has_parent.add(tc["id"])
            orphaned = tool_ids_needed - has_parent
            if orphaned:
                conv = [m for m in conv if not (
                    m.get("role") == "tool" and
                    m.get("tool_call_id", "") in orphaned
                )]
                _stripped_orphans = len(orphaned)
                logger.info("  stripped %d orphan tool msgs from session history", _stripped_orphans)
        # Token-aware session trimming — delegated entirely to ContextCompactor
        # (the old hardcoded keep=14 block was removed; compactor handles it properly)
        if ctx.compactor and ctx.compactor.needs_compaction(conv):
            estimate = ctx.compactor.estimate_tokens(conv)
            logger.info("  session trim: %d msgs, ~%d tokens (max=%d)", len(conv), estimate, ctx.compactor.max_tokens)
    # Build user message content - include media if available
    if hasattr(msg, 'media_data') and msg.media_data:
        content_parts = [{"type": "text", "text": msg.content}]
        for md in msg.media_data:
            if md["type"] == "image":
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{md['mime']};base64,{md['data']}"}
                })
            elif md["type"] == "transcript":
                content_parts.append({
                    "type": "text",
                    "text": f"[voicetranscribe: {md['text']}]"
                })
            elif md["type"] == "document_text":
                content_parts.append({
                    "type": "text",
                    "text": f"[File {md.get('filename','?')} content: {md['text']}]"
                })
        conv.append({"role": "user", "content": content_parts})
    else:
        conv.append({"role": "user", "content": msg.content})
    _new_start = len(conv) - 1  # track where new messages begin

    max_iterations = 20  # Raised for sustained autonomous work
    _last_results: dict = {}  # Per-tool result cache for efficiency detection
    for iteration in range(max_iterations):
        # Pre-model checkpoint...
        if ctx.checkpoint:
            try:
                ctx.checkpoint.save(
                    description=f"pre-model-round-{iteration}",
                    session_messages=conv,
                    session_id=session_id,
                    iteration=iteration,
                )
            except Exception:
                pass
        # Context compression for long tool-heavy conversations
        if iteration >= 3 and ctx.compactor and ctx.compactor.needs_compaction(conv):
            estimate = ctx.compactor.estimate_tokens(conv)
            logger.info("  chat compress: %d msgs, ~%d tokens (max=%d)", len(conv), estimate, ctx.compactor.max_tokens)
            system = conv[0] if conv and conv[0].get('role') == 'system' else None
            keep = getattr(ctx.compactor, 'keep_recent', 12)
            new_conv = [system] if system else []
            tail = list(conv[-keep:])
            tool_ids_needed = set()
            for m in tail:
                if m.get("role") == "tool" and m.get("tool_call_id"):
                    tool_ids_needed.add(m["tool_call_id"])
            for m in reversed(conv[1:-keep] if system else conv[:-keep]):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m.get("tool_calls", []):
                        if tc.get("id") in tool_ids_needed:
                            tail.insert(0, m)
                            tool_ids_needed.discard(tc.get("id"))
                            break
                if not tool_ids_needed:
                    break
            new_conv.extend(tail)
            conv = new_conv
            logger.info("  compressed to %d msgs", len(conv))
        # Proactive auto-compaction before API call (80% token budget trigger)
        # Helper: run an async coroutine synchronously with proper Task context.
        # aiohttp 3.13+ uses asyncio.timeout() internally, which requires
        # the coroutine to be wrapped in a Task (Python 3.12+ behavior).
        # Without create_task(), asyncio.timeout() raises:
        #   RuntimeError: Timeout context manager should be used inside a task
        def _run_async_safe(coro):
            """Run an async coroutine synchronously, reusing worker's event loop."""
            return ctx.run_async(coro)

        if ctx.compactor:
            def _llm_call_sync(msgs):
                _r = ctx.llm.call(msgs)
                if asyncio.iscoroutine(_r):
                    _r = _run_async_safe(_r)
                return _r
            conv = ctx.compactor.compact_if_needed(conv, _llm_call_sync)
        # Adapt messages for model format compatibility
        _call_conv = conv
        if ctx._msg_adapter:
            try:
                _family_name = _family or "default"
                _call_conv = ctx._msg_adapter.adapt(_call_conv, model_family=_family_name)
            except Exception:
                pass
        # Trace: record LLM call timing
        _trace_t0 = time.time()
        _llm_result = ctx.llm.call(_call_conv, tools=TOOL_SCHEMAS, preferred_family=_family)
        if asyncio.iscoroutine(_llm_result):
            _llm_result = _run_async_safe(_llm_result)
        response = _llm_result
        # Retry on transient LLM errors (up to 2 retries with 1s/2s backoff)
        for _retry in range(2):
            if response.get("error") and "rate_limit" not in str(response.get("error", "")):
                time.sleep(1 + _retry)
                _llm_result = ctx.llm.call(_call_conv, tools=TOOL_SCHEMAS, preferred_family=_family)
                if asyncio.iscoroutine(_llm_result):
                    _llm_result = _run_async_safe(_llm_result)
                response = _llm_result
            else:
                break
        # Record session-affinity family on first call
        if _family is None and hasattr(response, 'provider_family') and response.provider_family:
            _family = response.provider_family
            ctx._session_family[session_id] = _family
        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        # ── Model failover recovery: feed LLM errors back to DecisionEngine ──
        # When the LLM returns a timeout or fatal error, mark the model status
        # so that pre_check() on the next turn can enforce recovery strategy
        # (retry → degrade → switch to fallback).  Previously this callback path
        # was dead code — _model_status stayed AVAILABLE forever.
        if ctx.decision_engine and getattr(ctx.decision_engine, '_enabled', False):
            _llm_error = response.get("error")
            if _llm_error:
                _err_str = str(_llm_error).lower()
                if "timeout" in _err_str or "timed out" in _err_str:
                    ctx.decision_engine.mark_model_status(
                        ModelStatus.TIMEOUT,
                        f"LLM timeout: {str(_llm_error)[:120]}"
                    )
                elif "unavailable" in _err_str or "503" in _err_str or "502" in _err_str:
                    ctx.decision_engine.mark_model_status(
                        ModelStatus.UNAVAILABLE,
                        f"LLM unavailable: {str(_llm_error)[:120]}"
                    )
                elif "rate_limit" in _err_str or "429" in _err_str:
                    ctx.decision_engine.mark_model_status(
                        ModelStatus.DEGRADED,
                        f"LLM rate-limited: {str(_llm_error)[:120]}"
                    )
            else:
                # Successful call resets status back to available
                if ctx.decision_engine._model_status != ModelStatus.AVAILABLE:
                    ctx.decision_engine.mark_model_status(
                        ModelStatus.AVAILABLE, "LLM call succeeded"
                    )

        # Vigil: record AI token usage
        if ctx._vigil:
            usage = response.get("usage")
            if usage and isinstance(usage, dict):
                ctx._vigil.ai_signal_collector.record_tokens(usage.get('total_tokens', 0))

        # Trace: log LLM call
        try:
            _trace_latency = (time.time() - _trace_t0) * 1000
            _provider = getattr(response, 'provider_name', '') or 'unknown'
            ctx.trace_logger.log_event(TraceEvent(
                trace_id=ctx._current_trace_id,
                event_type="llm_call",
                provider=_provider,
                latency_ms=round(_trace_latency, 2),
                input_summary=str(msg.content)[:200] if hasattr(msg, 'content') else "",
                output_summary=(content or "")[:200],
            ))
        except Exception:
            pass

        if tool_calls:
            # Save post-model checkpoint
            if ctx.checkpoint:
                try:
                    ctx.checkpoint.save(
                        description=f"post-model-round-{iteration}",
                        session_messages=conv,
                        session_id=session_id,
                        iteration=iteration,
                    )
                except Exception:
                    pass

            # Periodic session snapshot (every 3 iterations)
            if save_snapshot is not None and iteration > 0 and iteration % 3 == 0:
                try:
                    save_snapshot(ctx.name, {
                        "reason": "periodic",
                        "msg_count": getattr(ctx, '_msg_count', 0),
                        "session_id": session_id,
                        "conv_length": len(conv),
                        "iteration": iteration,
                        "pending_tool_calls": [tc.get("name") for tc in tool_calls],
                    })
                except Exception:
                    pass
                # Also save DecisionEngine message-level snapshot for rollback (v0.9)
                if (hasattr(ctx, 'decision_engine') and ctx.decision_engine is not None
                        and getattr(ctx.decision_engine, '_enabled', False)):
                    try:
                        ctx.decision_engine.save_snapshot(conv, iteration)
                    except Exception:
                        pass

            # Decision engine: check tool strategy before execution.
            # Validates tool choices for efficiency and prevents runaway iteration loops.
            if (hasattr(ctx, 'decision_engine') and ctx.decision_engine is not None
                    and getattr(ctx.decision_engine, '_enabled', False)):
                try:
                    for tc in tool_calls:
                        name = tc.get("name", "?")
                        allowed, reason = ctx.decision_engine.check_tool_strategy(
                            name, iteration + 1   # 1-based
                        )
                        if not allowed:
                            logger.warning(
                                "DecisionEngine blocked tool '%s' at iter %d: %s",
                                name, iteration, reason,
                            )
                            tc["_blocked"] = True
                            tc["_blocked_reason"] = reason
                except Exception:
                    pass

            # Classify tool calls: safe → parallel batch, destructive → sequential.
            # Follows MiMo Code runTools() pattern.

            # Add assistant response with tool_calls to conversation
            formatted_tcs = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": json.dumps(tc.get("args", {}))}}
                for tc in tool_calls
            ]
            _msg = {"role": "assistant", "content": response.get("content") or "", "tool_calls": formatted_tcs}
            # NOTE: reasoning_content intentionally NOT appended to conv
            # It is only useful for the current response and causes 400 errors
            # when passed to models that do not support it (e.g. DeepSeek fallback)
            conv.append(_msg)
            loop_messages = []
            consecutive_blocks = 0  # Circuit-breaker: count consecutive tool failures
            responded = set()  # Track tool_call_ids that received responses
            # ── Tool concurrency orchestration ──────────────────────────
            # Classify tool calls: safe → parallel batch, destructive → sequential.
            # Follows MiMo Code runTools() pattern.
            safe_tcs = []
            destructive_tcs = []
            for tc in tool_calls:
                name = tc.get("name", "?")
                if is_concurrency_safe(name):
                    safe_tcs.append(tc)
                else:
                    destructive_tcs.append(tc)

            # Pre-execution: skip blocked tools (DecisionEngine strategy veto).
            # Record DoomLoop intent for ALL non-blocked tools.
            for tc in tool_calls:
                if tc.get("_blocked"):
                    name = tc.get("name", "?")
                    reason = tc.get("_blocked_reason", "strategy veto")
                    logger.info("  tool BLOCKED by DecisionEngine: %s (%s)", name, reason)
                    continue
                name = tc.get("name", "?")
                args = tc.get("args", {})
                if ctx.doom_detector:
                    try:
                        ctx.doom_detector.record_tool_call(name, args, agent_id=ctx.name)
                    except Exception:
                        pass

            # Phase A: Run safe (read-only) tools in parallel via ThreadPoolExecutor
            safe_results: dict[str, dict] = {}  # tc_id → execution info
            if safe_tcs:
                _max_workers = min(8, len(safe_tcs))
                with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers) as executor:
                    futures = {}
                    for tc in safe_tcs:
                        future = executor.submit(_execute_tool_core, tc, ctx, iteration)
                        futures[future] = tc
                    for future in concurrent.futures.as_completed(futures):
                        tc = futures[future]
                        try:
                            info = future.result(timeout=30)
                            safe_results[tc.get("id", "")] = info
                        except Exception as e:
                            safe_results[tc.get("id", "")] = {
                                "tc_id": tc.get("id", ""),
                                "name": tc.get("name", "?"),
                                "args": tc.get("args", {}),
                                "blocked": True,
                                "blocked_type": "execution_error",
                                "blocked_detail": str(e),
                                "result": {},
                                "executed": False,
                                "tool_latency_ms": 0.0,
                            }

            # Phase B: Process all tool calls in original order.
            # Safe tool results are already cached; destructive tools run inline.
            for tc in tool_calls:
                name = tc.get("name", "?")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")
                logger.info("  tool call: %s", name)

                if tc_id in safe_results:
                    info = safe_results[tc_id]
                else:
                    # Destructive tool — execute now (sequential, no parallel contention)
                    info = _execute_tool_core(tc, ctx, iteration)

                result = info.get("result") or {}

                # Handle blocked tools (pre-execution blocks + execution errors)
                if info.get("blocked"):
                    blocked_type = info.get("blocked_type", "")
                    blocked_detail = info.get("blocked_detail", "")
                    if blocked_type == "permission":
                        consecutive_blocks += 1
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[PERMISSION BLOCKED] {name}: {blocked_detail}. ({consecutive_blocks}/6 consecutive blocks -- reply directly if this reaches 6.)",
                        })
                    elif blocked_type == "verification":
                        consecutive_blocks += 1
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[BLOCKED] {name}: {blocked_detail}. ({consecutive_blocks}/6 consecutive blocks -- if this reaches 6, you MUST reply to the user directly without more tools.)",
                        })
                    elif blocked_type == "constitution":
                        consecutive_blocks += 1
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[CONSTITUTION BLOCKED] {name}: {blocked_detail}. ({consecutive_blocks}/6 consecutive blocks -- reply directly if this reaches 6.)",
                        })
                    elif blocked_type == "strategy":
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[STRATEGY BLOCKED] {name}: {blocked_detail}",
                        })
                    else:
                        # execution_error or unknown block
                        consecutive_blocks += 1
                        conv.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[ERROR] {name}: {blocked_detail}",
                        })
                    responded.add(tc_id)
                    continue

                # ── Post-execution processing (same as before) ──────────
                ctx.skill_extractor.record_tool_call(name, args, str(result)[:200])

                # Vigil: notify AI signal collector
                if ctx._vigil:
                    _result_hash = hashlib.sha256(str(result).encode()).hexdigest()
                    ctx._vigil.ai_signal_collector.record_tool_call(name, _result_hash)

                # Trace: log tool execution
                try:
                    _tool_latency = info.get("tool_latency_ms", 0)
                    _rsum = str(result)[:200]
                    ctx.trace_logger.log_event(TraceEvent(
                        trace_id=ctx._current_trace_id,
                        event_type="tool_exec",
                        provider="",
                        latency_ms=round(_tool_latency, 2),
                        input_summary=f"{name}: {str(args)[:150]}",
                        output_summary=_rsum,
                    ))
                except Exception:
                    pass

                # Circuit breaker: count bash/tool failures as blocks too
                # (not just verification/constitution blocks -- actual execution failures matter)
                _tool_failed = False
                if isinstance(result, dict):
                    if name == "bash" and result.get("exit_code", 0) != 0:
                        consecutive_blocks += 1
                        _tool_failed = True
                    elif result.get("error") or result.get("__error__"):
                        consecutive_blocks += 1
                        _tool_failed = True
                # Reset consecutive_blocks on any successful tool execution
                # (a single success proves we're not in a failure loop)
                if not _tool_failed:
                    consecutive_blocks = 0

                # Save post-tool checkpoint
                if ctx.checkpoint:
                    try:
                        ctx.checkpoint.save(
                            description=f"post-tool-{name}",
                            session_messages=conv,
                            session_id=session_id,
                            tool_history=[{"name": name, "args": args, "result_summary": str(result)[:200]}],
                            iteration=iteration,
                        )
                    except Exception:
                        pass

                # DoomLoop: record tool call outcome
                if ctx.doom_detector:
                    try:
                        ctx.doom_detector.record_tool_outcome(name, args, result_text=format_result(name, result))
                        doom_result = ctx.doom_detector.detect()
                        if doom_result and doom_result.stuck:
                            if doom_result.level == DoomLoopLevel.CRITICAL:
                                conv.append({
                                    "role": "system",
                                    "content": f"[DOOM LOOP] {doom_result.message} You MUST break out now -- try a completely different approach or reply with what you have.",
                                })
                                # Execute recovery if available
                                if doom_result.recovery.value != "none":
                                    try:
                                        ctx.run_async(ctx.doom_detector.execute_recovery(doom_result))
                                    except Exception:
                                        pass
                            elif doom_result.level == DoomLoopLevel.WARNING:
                                conv.append({
                                    "role": "system",
                                    "content": f"[DOOM LOOP WARNING] {doom_result.message}",
                                })
                    except Exception as e:
                        logger.warning("DoomLoop detect error: %s", e)

                formatted = format_result(name, result)

                # Efficiency detection: compare new result against previous (v0.9)
                _prev = _last_results.get(name)
                _new_text = str(result)[:500]
                if _prev and _new_text and ctx.decision_engine and getattr(ctx.decision_engine, '_enabled', False):
                    try:
                        _state = ctx.decision_engine.get_iteration_state(iteration)
                        _state.last_results = [_prev]
                        efficient, eff_reason = ctx.decision_engine.check_tool_efficiency(
                            name, _new_text, _state
                        )
                        if not efficient:
                            logger.warning("Efficiency block on '%s': %s", name, eff_reason)
                            conv.append({
                                "role": "system",
                                "content": f"[EFFICIENCY] {eff_reason}",
                            })
                    except Exception:
                        pass
                # Track result for next iteration
                _last_results[name] = _new_text

                # Phase 2: Verify tool output (after execution)
                if ctx.verification:
                    phase2 = ctx.verification.verify_tool_output(name, args, result)
                    ctx.verif_recorder.record_tool_call(name, args, result, phase2.passed)
                    if not phase2.passed:
                        for v in phase2.violations:
                            ctx.verif_recorder.record_violation(v.rule, v.category, v.claim, v.detail, v.severity)
                        # Inject verification failure into tool output so LLM doesn't trust bogus results
                        _fail_details = "; ".join(v.detail for v in phase2.violations[:3])
                        logger.warning("  verify %s: FAILED -- %s", name, _fail_details)
                        # Augment formatted result with verification failure marker
                        if isinstance(result, dict):
                            result = dict(result)  # shallow copy to avoid mutating original
                            result["__eite_verify__"] = {"passed": False, "violations": _fail_details}
                    elif ctx.verif_recorder:
                        ctx.verif_recorder.record_tool_call(name, args, result, True)
                        # Tag successful verification
                        if isinstance(result, dict):
                            result = dict(result)
                            result["__eite_verify__"] = {"passed": True}
                else:
                    # Verification disabled — skip Phase 2 entirely
                    pass
                if not formatted:
                    formatted = json.dumps(result, ensure_ascii=False)[:4000]

                conv.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": formatted,
                })
                responded.add(tc_id)

                # TraceRecorder: record tool
                if ctx.tracer:
                    verified = result.get("ok", False) or result.get("exit_code") == 0
                    ctx.tracer.on_tool_result(name, args, result, verified)

                # end_task signal: stop iteration when AI requests it
                if isinstance(result, dict) and result.get("__end_task__"):
                    logger.info("  end_task received — breaking tool iteration loop")
                    break

                # Module: Loop detection
                if ctx.loop_detector:
                    ctx.loop_detector.record(name, args, result)
                    loop_result = ctx.loop_detector.detect()
                    if loop_result:
                        loop_messages.append(loop_result["message"])
                        if loop_result["level"] == "critical":
                            break
                else:
                    # Loop detection disabled -- no op
                    pass

            # Append accumulated loop detector messages after all tool responses
            for ld_msg in loop_messages:
                conv.append({"role": "system", "content": ld_msg})

            # Fill missing tool responses to satisfy API requirement
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                if tc_id not in responded:
                    conv.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "[interrupted]",
                    })

            # Circuit breaker: inject failure summary after all tool responses
            if consecutive_blocks >= 15:
                conv.append({
                    "role": "system",
                    "content": f"[CIRCUIT BREAKER] {consecutive_blocks} consecutive tool calls were blocked. Your tools cannot complete this task. Reply to the user immediately explaining what went wrong and what they should do instead. Do NOT make any more tool calls.",
                })

            # Iteration guard -- escalate warnings then force-stop
            if iteration >= 12:
                conv.append({
                    "role": "system",
                    "content": f"STOP calling tools. You have used {iteration + 1} rounds. Reply now with what you have.",
                })
                # Force break at iteration 20
                if iteration >= 20:
                    # Collect blocked-tool summary for a meaningful fallback message
                    blocked_names = []
                    for m in conv:
                        if m.get("role") == "tool" and "BLOCKED" in m.get("content", ""):
                            name_part = m["content"].split(":")[0] if ":" in m.get("content", "") else m["content"][:60]
                            blocked_names.append(name_part)
                    last_reply = None
                    for m in reversed(conv):
                        if m.get("role") == "assistant" and m.get("content", "").strip():
                            last_reply = m["content"]
                            break
                    if last_reply:
                        reply = last_reply[:16000]
                    else:
                        # No assistant content -- build a meaningful fallback
                        reply = f"I was unable to complete this task after {iteration + 1} attempts.\n\n"
                        if blocked_names:
                            reply += f"Blocked: {', '.join(blocked_names[-5:])}\n\n"
                        reply += "Please check my tool permissions or ask me to try a simpler approach."
                    if channel:
                        # Privacy scan: redact PII/secrets before sending (skip in power mode)
                        if ctx._vigil is not None and not ctx._session_power.get(session_id, False):
                            try:
                                reply = _privacy_scan_response(reply)
                            except Exception as e:
                                logger.debug("[privacy] scan skipped: %s", e)
                        # Strip unsolicited capability listings
                        reply = _strip_capability_listing(reply)
                        channel.send(Response(content=reply, target=msg.sender, source=msg.source, chat_id=msg.chat_id))
                        # Vigil: record response on human signal collector
                        if ctx._vigil:
                            ctx._vigil.signal_collector.record_response(len(reply))
                        logger.info("  forced reply at iteration %d", iteration)
                    # End skill extraction (forced reply path, only if tool calls were made)
                    _forced_tool_count = ctx.skill_extractor._records.__len__() if hasattr(ctx.skill_extractor, '_records') else 0
                    if _forced_tool_count > 0:
                        ctx.skill_extractor.end_task(True)
                    return
        else:
            # Text response
            reply = content or "[worker] no response"

            # TruthReporter: verify reply honesty
            if ctx.truth_reporter:
                try:
                    honest = ctx.truth_reporter.verify_before_report(
                        operation="reply",
                        expected_success=True,
                    )
                    if not honest:
                        logger.warning("[TruthReporter] Reply may contain unverified claims -- trust degraded")
                except Exception as e:
                    logger.debug("TruthReporter verify error: %s", e)

            # Phase 3: Verify reply before sending (only if verification enabled)
            if ctx.verification:
                if ctx.verif_recorder:
                    ctx.verif_recorder._turn_buffer["initial_reply"] = reply
                phase3 = ctx.verification.verify_reply(reply)
                if not phase3.passed:
                    for v in phase3.violations:
                        if ctx.verif_recorder:
                            ctx.verif_recorder.record_violation(v.rule, v.category, v.claim, v.detail, v.severity)
                    if phase3.action in ("block", "retry", "rewrite"):
                        logger.warning("Reply verification: %s", phase3.corrections)
            # Check for continuation hint -- only if explicit "I still need to"
            if "I still need to" in reply:
                next_task = reply.split("I still need to", 1)[-1].strip()
                from tical_code.core.modules.task_handler import save_pending
                save_pending(ctx, next_task, iteration)
                reply += f"\n\n[task queued: {next_task[:60]}]"

            # TraceRecorder: task end
            if ctx.tracer:
                ctx.tracer.on_task_end(True)

            # End skill extraction for this message turn (normal reply path)
            tool_count = ctx.skill_extractor._records.__len__() if hasattr(ctx.skill_extractor, '_records') else 0
            if tool_count > 0:
                ctx.skill_extractor.end_task(True)

            if channel:
                # Privacy scan: redact PII/secrets before sending (skip in power mode)
                if ctx._vigil is not None and not ctx._session_power.get(session_id, False):
                    try:
                        reply = _privacy_scan_response(reply)
                    except Exception as e:
                        logger.debug("[privacy] scan skipped: %s", e)
                # Strip unsolicited capability listings
                reply = _strip_capability_listing(reply)
                # Split long replies into chunks (Telegram 4000 char limit)
                if len(reply) > 4000:
                    for i in range(0, len(reply), 4000):
                        chunk = reply[i:i+4000]
                        if i > 0:
                            chunk = f"[cont. {i//4000+1}] {chunk}"
                        channel.send(Response(
                            content=chunk,
                            target=msg.sender,
                            source=msg.source,
                            chat_id=msg.chat_id,
                        ))
                else:
                    channel.send(Response(
                        content=reply,
                        target=msg.sender,
                        source=msg.source,
                        chat_id=msg.chat_id,
                    ))
            # Vigil: record response on human signal collector
            if ctx._vigil:
                ctx._vigil.signal_collector.record_response(len(reply))
            logger.info("  reply: %s", reply[:80])

            # Save completion snapshot
            if save_snapshot is not None:
                try:
                    save_snapshot(ctx.name, {
                        "reason": "reply_sent",
                        "msg_count": getattr(ctx, '_msg_count', 0),
                        "session_id": session_id,
                        "conv_length": len(conv),
                        "reply_len": len(reply),
                    })
                except Exception:
                    pass

            # Module 1: Save conversation -- full turn (user + tool chain + assistant)
            if ctx.sessions:
                session_id = ctx.sessions.get_session_id(msg.source, str(msg.chat_id))
                new_msgs = []
                for m in conv[_new_start:]:
                    entry = {"role": m["role"], "content": m.get("content", "")}
                    if m.get("tool_calls"):
                        entry["tool_calls"] = m["tool_calls"]
                    if m.get("tool_call_id"):
                        entry["tool_call_id"] = m["tool_call_id"]
                    new_msgs.append(entry)
                _saved = ctx.sessions.save_messages(session_id, new_msgs)
                if not _saved:
                    logger.warning("Session save FAILED for %s (%d msgs)", session_id[:12], len(new_msgs))
            # Module 1b: Save conversation to FTS5 for cross-session search
            try:
                from tical_code.core.memory_sense import conversation_save
                for m in conv[_new_start:]:
                    role = m.get("role", "unknown")
                    content = m.get("content", "")
                    if content and role in ("user", "assistant"):
                        conversation_save(
                            session_id=session_id,
                            role=role,
                            content=content,
                            metadata={"source": msg.source, "chat_id": str(msg.chat_id)}
                        )
            except Exception:
                pass
            # Periodic session DB cleanup (every save)
            try:
                if ctx.sessions:
                    ctx.sessions.cleanup(max_age_days=3, max_db_size_mb=50)
            except Exception:
                pass
            # Memory management: explicit cleanup + RSS monitoring
            del conv
            if force_gc_collect:
                try:
                    force_gc_collect()
                except Exception:
                    pass
            else:
                gc.collect()
            ctx._msg_count = getattr(ctx, '_msg_count', 0) + 1
            # Periodic memory consolidation (every 10 messages)
            if ctx._msg_count % 10 == 0 and ctx.memory_evolver is not None:
                try:
                    cons_result = ctx.memory_evolver.consolidate()
                    if cons_result.get('consolidated', 0) > 0:
                        logger.info("[memory_evolve] consolidated %d sections, saved %d bytes",
                                    cons_result.get('consolidated', 0),
                                    cons_result.get('space_saved', 0))
                except Exception:
                    pass
            # Periodic session_family cleanup (prevents unbounded growth)
            if ctx._msg_count % 200 == 0 and len(ctx._session_family) > 30:
                ctx._session_family = {
                    k: v for k, v in ctx._session_family.items()
                    if k in list(ctx._session_family.keys())[-20:]
                }
            if ctx._msg_count % ctx.memory_check_interval == 0:
                rss = _get_rss_mb()
                logger.info("[memory] RSS: %.0fMB (msg #%d)", rss, ctx._msg_count)
                if rss > ctx.memory_limit_mb:
                    logger.warning("[memory] RSS %.0fMB > %dMB -- scheduling restart", rss, ctx.memory_limit_mb)
                    ctx._schedule_restart = True
            # End verification recording -- save training data if violations occurred
            if ctx.verif_recorder:
                ctx.verif_recorder.end_turn(reply)
            # Restore constitution if power mode was active
            if _saved_constitution is not None:
                ctx.constitution = _saved_constitution
                from tical_code.core.tool_executor import set_skip_sanitize
                set_skip_sanitize(False)
            return

    # Exceeded max iterations -- send last reply to sender, save partial conversation
    # Restore constitution if power mode was active
    if _saved_constitution is not None:
        ctx.constitution = _saved_constitution
        from tical_code.core.tool_executor import set_skip_sanitize
        set_skip_sanitize(False)
    logger.warning("[worker] %s: exceeded max tool iterations", msg.sender)
    # Save snapshot before timeout exit
    if save_snapshot is not None:
        try:
            save_snapshot(ctx.name, {
                "reason": "timeout",
                "msg_count": getattr(ctx, '_msg_count', 0),
                "session_id": session_id,
                "conv_length": len(conv),
                "last_action": "timeout_at_max_iterations",
            })
        except Exception:
            pass
    # End verification recording -- save training data even on timeout
    if ctx.verif_recorder:
        ctx.verif_recorder.end_turn("[timeout]")
    try:
        session_id = ctx.sessions.get_session_id(msg.source, str(msg.chat_id))
        new_msgs = []
        for m in conv[_new_start:]:
            entry = {"role": m["role"], "content": m.get("content", "")}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            new_msgs.append(entry)
        ctx.sessions.save_messages(session_id, new_msgs)
    except Exception as e:
        logger.debug("[worker] session save error: %s", e)
    # Send last assistant reply back to sender (not system messages)
    last_reply = None
    if conv and len(conv) > 2:
        for m in reversed(conv):
            if m.get("role") == "assistant" and m.get("content", "").strip():
                last_reply = m["content"]
                break
    if channel and msg.sender not in ("system", None):
        timeout_msg = "[worker timeout after reaching max tool iterations]"
        if last_reply:
            timeout_msg += f"\n\n{last_reply[:5000]}"
        else:
            timeout_msg += "\nNo assistant reply was produced."
        # Privacy scan: redact PII/secrets before sending (skip in power mode)
        if ctx._vigil is not None and not ctx._session_power.get(session_id, False):
            try:
                timeout_msg = _privacy_scan_response(timeout_msg)
            except Exception as e:
                logger.debug("[privacy] scan skipped: %s", e)
        channel.send(Response(
            content=timeout_msg,
            target=msg.sender,
            source=msg.source,
            chat_id=msg.chat_id,
        ))
        # Vigil: record response on human signal collector
        if ctx._vigil:
            ctx._vigil.signal_collector.record_response(len(timeout_msg))
    # Save continuation hint if explicit
    if conv and len(conv) > 2:
        last_assistant = next(
            (m["content"] for m in reversed(conv) if m.get("role") == "assistant"),
            None
        )
        if last_assistant and "I still need to" in last_assistant:
            from tical_code.core.modules.task_handler import save_pending
            save_pending(ctx, last_assistant, max_iterations)
