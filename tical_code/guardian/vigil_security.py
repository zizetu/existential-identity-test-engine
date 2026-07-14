# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Original repository: https://github.com/zizetu/EITE-agent

"""
Vigil Security Responder -- deterministic blocking + LLM-based triage.

Two-tier response:
  1. DETERMINISTIC (no LLM): SSH brute force, known attack patterns.
     Fast, reliable, works even if LLM is down.
  2. LLM-BASED: Complex threats (reverse shells, malware, new ports).
     Uses provider registry with auto-failover.

Anti-suicide: never blocks current SSH session IP, mesh peers, or IPs
that have successfully authenticated via SSH key in the last 24h.

All blocks are TEMPORARY (1 hour default), tracked in a ban journal.
On restart, expired bans are cleaned up and active bans re-applied.

Recovery: `eite-agent security unblock` clears all Vigil iptables rules.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from tical_code.guardian.iron_wall import (
    ThreatFinding,
    _get_recent_successful_ssh_ips,
    get_dynamic_safe_ips,
    get_threat_summary,
    run_all_security_checks,
)
from tical_code.core.paths import get_guardian_dir

logger = logging.getLogger("EITElite.vigil-security")

# ── Configuration ─────────────────────────────────────────────────────────

SECURITY_LEVEL = os.environ.get("EITE_SECURITY_LEVEL", "basic")
# "basic" (default): auto-block SSH brute force + port scanners + web attacks.
#                    Does NOT block all SSH -- only known-malicious IPs.
# "strict": mesh-only SSH + port whitelist. Opt-in only.

BF_THRESHOLD = int(os.environ.get("VIGIL_BF_THRESHOLD", "5"))
BF_WINDOW = int(os.environ.get("VIGIL_BF_WINDOW", "300"))
BAN_SECONDS = int(os.environ.get("VIGIL_BAN_SECONDS", "3600"))  # 1 hour
VIGIL_COMMENT = "vigil-auto"

# ── Ban journal path ──────────────────────────────────────────────────────

def _ban_journal_path() -> Path:
    return get_guardian_dir() / "ban_journal.json"


# ═══════════════════════════════════════════════════════════════════════════
# ANTI-SUICIDE
# ═══════════════════════════════════════════════════════════════════════════

def _get_safe_ips() -> Set[str]:
    """Return IPs that must NEVER be blocked.

    Uses the same dynamic resolution as iron_wall's get_dynamic_safe_ips()
    plus any additional Vigil-specific protections.
    """
    return get_dynamic_safe_ips()


# ═══════════════════════════════════════════════════════════════════════════
# BAN JOURNAL (persistent tracking across restarts)
# ═══════════════════════════════════════════════════════════════════════════

def _load_ban_journal() -> Dict:
    """Load ban journal from disk. Returns {ip: {expires_at, reason, ...}}."""
    jf = _ban_journal_path()
    if jf.exists():
        try:
            return json.loads(jf.read_text())
        except Exception:
            pass
    return {}


def _save_ban_journal(journal: Dict) -> None:
    """Save ban journal to disk atomically."""
    jf = _ban_journal_path()
    jf.parent.mkdir(parents=True, exist_ok=True)
    tmp = jf.with_suffix(".tmp")
    tmp.write_text(json.dumps(journal, indent=2))
    tmp.rename(jf)


def _cleanup_expired_bans() -> List[str]:
    """Remove expired bans from journal and iptables. Returns freed IPs."""
    journal = _load_ban_journal()
    now = time.time()
    freed: List[str] = []

    for ip, entry in list(journal.items()):
        if entry.get("expires_at", 0) < now:
            _remove_iptables_block(ip)
            del journal[ip]
            freed.append(ip)
            logger.info("Ban expired for %s (banned at %s)", ip, entry.get("banned_at"))

    if freed:
        _save_ban_journal(journal)
        _persist_iptables()
    return freed


def _replay_bans_on_startup() -> int:
    """Re-apply active bans from journal on service startup. Returns count."""
    _cleanup_expired_bans()
    journal = _load_ban_journal()
    now = time.time()
    count = 0

    for ip, entry in journal.items():
        if entry.get("expires_at", 0) > now:
            if _add_iptables_block(ip, entry.get("reason", "replay")):
                count += 1

    if count:
        _persist_iptables()
    logger.info("Replayed %d active bans from journal on startup", count)
    return count


# ═══════════════════════════════════════════════════════════════════════════
# IPTABLES OPERATIONS (with comment tagging for cleanup)
# ═══════════════════════════════════════════════════════════════════════════

def _iptables_rule_exists(ip: str) -> bool:
    """Check if a DROP rule already exists for this IP."""
    try:
        subprocess.run(
            ["sudo", "iptables", "-C", "INPUT", "-s", ip, "-j", "DROP",
             "-m", "comment", "--comment", VIGIL_COMMENT],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _add_iptables_block(ip: str, reason: str = "") -> bool:
    """Add iptables DROP rule for IP with Vigil comment tag. Returns success."""
    if _iptables_rule_exists(ip):
        return True  # Already blocked

    try:
        comment = f"{VIGIL_COMMENT}: {reason}"[:255] if reason else VIGIL_COMMENT
        subprocess.run(
            ["sudo", "iptables", "-I", "INPUT", "-s", ip, "-j", "DROP",
             "-m", "comment", "--comment", comment],
            capture_output=True, timeout=10, check=True,
        )
        logger.info("Blocked IP: %s (%s)", ip, reason or "no reason given")
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to block IP %s: %s", ip, e)
        return False


def _remove_iptables_block(ip: str) -> bool:
    """Remove iptables DROP rule for IP. Returns success."""
    try:
        # Remove all Vigil-tagged rules for this IP
        rules = subprocess.run(
            ["sudo", "iptables", "-L", "INPUT", "-n", "--line-numbers"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        for line in reversed(rules.splitlines()):
            if ip in line and VIGIL_COMMENT in line:
                num = line.split()[0]
                subprocess.run(
                    ["sudo", "iptables", "-D", "INPUT", num],
                    capture_output=True, timeout=5, check=True,
                )
        return True
    except Exception as e:
        logger.error("Failed to unblock IP %s: %s", ip, e)
        return False


def _persist_iptables() -> None:
    """Save current iptables rules to disk for reboot survival."""
    try:
        subprocess.run(
            ["sudo", "bash", "-c",
             "mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4"],
            capture_output=True, timeout=10, check=False,
        )
    except Exception as e:
        logger.error("Failed to persist iptables: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# DETERMINISTIC BLOCKING (no LLM required)
# ═══════════════════════════════════════════════════════════════════════════

def _deterministic_block(findings: List[ThreatFinding]) -> List[Dict]:
    """Apply deterministic blocking rules for well-understood threat types.

    No LLM needed -- these are pattern-matched attacks with clear responses.
    """
    results: List[Dict] = []
    safe_ips = _get_safe_ips()
    journal = _load_ban_journal()
    now = time.time()
    modified = False

    for f in findings:
        ip = f.target if f.category in ("ssh_brute_force", "unauthorized_ssh",
                                         "web_attack") else None
        if not ip:
            continue

        # Anti-suicide check
        if ip in safe_ips:
            logger.info("Skipping safe IP: %s (category: %s)", ip, f.category)
            continue

        # Skip if already banned and not expired
        existing = journal.get(ip, {})
        if existing.get("expires_at", 0) > now:
            continue

        # Determine ban reason and apply
        reason = f"{f.category}: {f.detail[:100]}"
        if _add_iptables_block(ip, reason):
            expires_at = now + BAN_SECONDS
            journal[ip] = {
                "banned_at": now,
                "expires_at": expires_at,
                "reason": reason,
                "category": f.category,
            }
            modified = True
            results.append({
                "action": "deterministic_block",
                "target": ip,
                "success": True,
                "reason": reason,
                "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            })

    if modified:
        _save_ban_journal(journal)
        _persist_iptables()

    return results


# ═══════════════════════════════════════════════════════════════════════════
# LLM-BASED TRIAGE (for complex threats)
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_TRIAGE_PROMPT = """You are Vigil, the autonomous security response system for EITE agent.

A security threat has been detected. Analyze the threat report and decide the response.

## Response Options (choose ONE):
- **INSTANT_BLOCK** -- Active attack. Block IP immediately, kill connections.
- **QUARANTINE** -- Malware file found. Move to quarantine, chmod 000.
- **INVESTIGATE** -- Suspicious but unclear. Collect intel first, then decide.
- **ALERT_ONLY** -- Low credibility or already handled. Alert human only.
- **FALSE_POSITIVE** -- Known safe. Log and dismiss.

## Decision Rules:
- SSH brute force from unknown IP -> INSTANT_BLOCK (already handled deterministically if you see this, it means the deterministic path missed it somehow)
- Reverse shell pattern -> INSTANT_BLOCK + kill process
- New port from unknown process -> INVESTIGATE (check if legitimate)
- Suspicious file in /tmp -> QUARANTINE
- SSH key count increase -> INSTANT_BLOCK (possible backdoor)
- Known mesh IP on unexpected port -> ALERT_ONLY

## Output Format (JSON only, no markdown):
```json
{
  "decision": "INSTANT_BLOCK|QUARANTINE|INVESTIGATE|ALERT_ONLY|FALSE_POSITIVE",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation (max 200 chars)",
  "actions": ["action1", "action2"],
  "escalate_to_human": true/false
}
```
"""


def _build_threat_report(findings: List[ThreatFinding], node: str) -> str:
    """Build a structured threat report for the LLM."""
    lines = [
        "## Security Threat Report",
        f"Node: {node}",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"Total Findings: {len(findings)}",
        "",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"### Threat {i}: [{f.severity}] {f.category}")
        lines.append(f"- Target: {f.target}")
        lines.append(f"- Detail: {f.detail}")
        if f.evidence:
            lines.append(f"- Evidence: {json.dumps(f.evidence, default=str)[:300]}")
        lines.append("")
    return "\n".join(lines)


def call_llm_for_decision(findings: List[ThreatFinding], node: str = "",
                         repo_root: str = "") -> Dict:
    """Call the agent's LLM via provider registry for complex threat triage.

    Only called for threats that the deterministic path can't handle
    (reverse shells, key changes, suspicious files).

    Falls back to INSTANT_BLOCK on CRITICAL findings on error (fail-safe).
    """
    if not findings:
        return {"decision": "FALSE_POSITIVE", "confidence": 1.0,
                "reasoning": "No threats found", "actions": [], "escalate_to_human": False}

    report = _build_threat_report(findings, node or os.uname().nodename)

    try:
        import asyncio
        from tical_code.core.provider_registry import from_registry

        root = repo_root or os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        failover = from_registry(repo_root=root)

        if not failover.providers:
            raise ValueError("No providers available")

        async def _call():
            return await failover.call(
                messages=[
                    {"role": "system", "content": SECURITY_TRIAGE_PROMPT},
                    {"role": "user", "content": report},
                ],
                max_tokens=500,
            )

        response = asyncio.run(_call())
        content = response.get("content", "")

        if not content:
            raise ValueError("Empty LLM response")

        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        if json_match:
            decision = json.loads(json_match.group())
            logger.info("LLM decision: %s (confidence=%.2f via %d providers)",
                        decision.get("decision"), decision.get("confidence", 0),
                        len(failover.providers))
            return decision
        else:
            raise ValueError(f"No JSON found in response: {content[:200]}")

    except Exception as e:
        logger.error("LLM decision failed: %s -- falling back to safe defaults", e)
        actions = []
        for f in findings:
            if f.severity == "CRITICAL" and f.category == "reverse_shell":
                actions.append(f"kill:{f.target}")
            elif f.category == "tmp_malware":
                actions.append(f"quarantine:{f.target}")
            elif f.category == "key_change" and f.severity == "CRITICAL":
                actions.append("alert")
        return {
            "decision": "ALERT_ONLY" if not actions else "INSTANT_BLOCK",
            "confidence": 0.5,
            "reasoning": f"LLM unavailable, safe fallback for {len(findings)} threats",
            "actions": actions[:3],
            "escalate_to_human": True,
        }


# ═══════════════════════════════════════════════════════════════════════════
# ACTION EXECUTORS
# ═══════════════════════════════════════════════════════════════════════════

def block_ip(ip: str) -> Dict:
    """Block IP via iptables + kill connections. Anti-suicide safe."""
    safe_ips = _get_safe_ips()
    if ip in safe_ips:
        return {"action": "block_ip", "target": ip, "success": False,
                "reason": "anti-suicide: IP is current SSH session or mesh peer"}

    # Already handled by deterministic path? Check journal
    journal = _load_ban_journal()
    if ip in journal and journal[ip].get("expires_at", 0) > time.time():
        return {"action": "block_ip", "target": ip, "success": True,
                "reason": "already banned"}

    # Add iptables rule
    if not _add_iptables_block(ip, "LLM decision"):
        return {"action": "block_ip", "target": ip, "success": False,
                "reason": "iptables command failed"}

    # Track in journal
    now = time.time()
    journal[ip] = {
        "banned_at": now,
        "expires_at": now + BAN_SECONDS,
        "reason": "LLM decision",
        "category": "llm",
    }
    _save_ban_journal(journal)
    _persist_iptables()

    # Kill existing connections
    try:
        ss_out = subprocess.run(
            ["ss", "-tnp"], capture_output=True, text=True, timeout=10).stdout
        killed = []
        for line in ss_out.splitlines():
            if ip in line:
                pid_match = re.search(r'pid=(\d+)', line)
                if pid_match:
                    pid = pid_match.group(1)
                    subprocess.run(["sudo", "kill", "-9", pid],
                                   capture_output=True, timeout=5, check=False)
                    killed.append(pid)
        if killed:
            logger.info("Killed %d connection(s) from %s", len(killed), ip)
    except Exception as e:
        logger.error("Failed to kill connections from %s: %s", ip, e)

    return {"action": "block_ip", "target": ip, "success": True,
            "steps": ["iptables DROP added", "journal updated"]}


def block_port(port: int) -> Dict:
    """Block a specific port via iptables."""
    result = {"action": "block_port", "target": str(port), "success": True, "steps": []}
    try:
        subprocess.run(
            ["sudo", "iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port),
             "-j", "DROP", "-m", "comment", "--comment", f"{VIGIL_COMMENT}: block_port"],
            capture_output=True, timeout=10, check=True,
        )
        _persist_iptables()
        result["steps"].append(f"iptables DROP port {port}")
    except Exception as e:
        result["success"] = False
        result["steps"].append(str(e))
    return result


def kill_process(pid: str) -> Dict:
    """Kill a process by PID."""
    result = {"action": "kill_process", "target": pid, "success": True, "steps": []}
    try:
        subprocess.run(["sudo", "kill", "-9", pid],
                       capture_output=True, timeout=10, check=False)
        result["steps"].append(f"Killed PID {pid}")
    except Exception as e:
        result["success"] = False
        result["steps"].append(str(e))
    return result


def quarantine_file(filepath: str) -> Dict:
    """Move file to quarantine directory, chmod 000."""
    result = {"action": "quarantine", "target": filepath, "success": True, "steps": []}
    qdir = get_guardian_dir() / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)

    src = Path(filepath)
    if not src.exists():
        result["success"] = False
        result["steps"].append(f"File not found: {filepath}")
        return result

    dest = qdir / f"{src.name}_{int(time.time())}"
    try:
        src.rename(dest)
        dest.chmod(0o000)
        result["steps"].append(f"Quarantined: {filepath} -> {dest}")
    except Exception as e:
        result["success"] = False
        result["steps"].append(str(e))
    return result


def collect_intel(ip: str) -> Dict:
    """Collect threat intelligence about an IP."""
    intel = {"ip": ip, "ipinfo": None, "processes": [], "timestamp": int(time.time())}
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://ipinfo.io/{ip}/json",
            headers={"User-Agent": "eite-vigil/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            intel["ipinfo"] = json.loads(resp.read().decode())
    except Exception:
        pass

    try:
        ss_out = subprocess.run(
            ["ss", "-tnp"], capture_output=True, text=True, timeout=10).stdout
        for line in ss_out.splitlines():
            if ip in line:
                intel["processes"].append(line.strip())
    except Exception:
        pass

    return intel


def execute_decision(decision: Dict, findings: List[ThreatFinding]) -> List[Dict]:
    """Execute the LLM's decision. Returns list of action results."""
    results = []
    actions = decision.get("actions", [])

    for action in actions:
        if not isinstance(action, str):
            continue

        if action.startswith("block_ip:"):
            ip = action.split(":", 1)[1]
            results.append(block_ip(ip))
        elif action.startswith("block_port:"):
            port = int(action.split(":", 1)[1])
            results.append(block_port(port))
        elif action.startswith("kill:"):
            pid = action.split(":", 1)[1]
            results.append(kill_process(pid))
        elif action.startswith("quarantine:"):
            path = action.split(":", 1)[1]
            results.append(quarantine_file(path))
        elif action == "investigate":
            for f in findings:
                if f.category in ("unauthorized_ssh", "reverse_shell"):
                    intel = collect_intel(f.target)
                    results.append({"action": "investigate", "target": f.target,
                                    "success": True, "intel": intel})
        elif action == "alert":
            results.append({"action": "alert", "success": True,
                            "detail": f"Human alert for {len(findings)} threats"})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# RECOVERY
# ═══════════════════════════════════════════════════════════════════════════

def unblock_all() -> Dict:
    """Remove ALL Vigil-added iptables rules and clear ban journal.

    This is the emergency recovery command. Safe to run anytime.
    Can be run locally even if SSH is blocked.
    """
    result = {"action": "unblock_all", "success": True, "removed": []}

    # Remove all iptables rules with Vigil comment
    try:
        rules = subprocess.run(
            ["sudo", "iptables", "-L", "INPUT", "-n", "--line-numbers"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout

        # Delete from bottom to top to preserve line numbers
        to_delete = []
        for line in rules.splitlines():
            if VIGIL_COMMENT in line:
                num = int(line.split()[0])
                to_delete.append(num)

        for num in reversed(to_delete):
            subprocess.run(
                ["sudo", "iptables", "-D", "INPUT", str(num)],
                capture_output=True, timeout=5, check=True,
            )
            result["removed"].append(num)

        _persist_iptables()
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        return result

    # Clear ban journal
    jf = _ban_journal_path()
    if jf.exists():
        jf.unlink()

    logger.info("Unblocked all: removed %d Vigil iptables rules", len(result["removed"]))
    return result


def security_status() -> Dict:
    """Return current Vigil security status for CLI display."""
    journal = _load_ban_journal()
    now = time.time()
    active_bans = {ip: e for ip, e in journal.items() if e.get("expires_at", 0) > now}
    expired_bans = {ip: e for ip, e in journal.items() if e.get("expires_at", 0) <= now}

    return {
        "security_level": SECURITY_LEVEL,
        "active_bans": len(active_bans),
        "expired_bans": len(expired_bans),
        "ban_duration_seconds": BAN_SECONDS,
        "brute_force_threshold": BF_THRESHOLD,
        "brute_force_window": BF_WINDOW,
        "bans": [
            {"ip": ip, "reason": e["reason"], "expires_at": e["expires_at"],
             "expires_in": max(0, int(e["expires_at"] - now))}
            for ip, e in sorted(active_bans.items(),
                                key=lambda x: x[1]["expires_at"])
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def run_security_scan(node: str = "", auto_respond: bool = True) -> Dict:
    """Run full security scan with deterministic + LLM-based response.

    Flow:
    1. Cleanup expired bans
    2. Run all Iron Wall checks
    3. Deterministic block for known attack patterns (SSH brute force, etc.)
    4. LLM triage for complex threats (reverse shells, malware, etc.)
    5. Return structured result

    Args:
        node: Node identifier (default: hostname)
        auto_respond: If True, execute blocking/triage actions automatically

    Returns:
        Dict with keys: findings, deterministic_results, llm_decision,
                        action_results, timestamp
    """
    if not node:
        node = os.uname().nodename

    logger.info("Starting security scan on %s", node)

    # 1. Cleanup expired bans
    freed = _cleanup_expired_bans()
    if freed:
        logger.info("Cleaned %d expired ban(s): %s", len(freed), freed)

    # 2. Run all Iron Wall checks
    check_results = run_all_security_checks()
    findings = get_threat_summary(check_results)

    # Build check status summary
    check_status = []
    for name, ok, detail, _ in check_results:
        check_status.append({"check": name, "ok": ok, "detail": detail})

    result = {
        "node": node,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(check_results),
        "checks_passed": sum(1 for _, ok, _, _ in check_results if ok),
        "total_findings": len(findings),
        "check_status": check_status,
        "findings": [{"category": f.category, "severity": f.severity,
                       "target": f.target, "detail": f.detail}
                      for f in findings],
        "deterministic_results": [],
        "llm_decision": None,
        "action_results": [],
    }

    if not findings:
        logger.info("Security scan: all clear on %s", node)
        return result

    # 3. Deterministic blocking for known patterns
    deterministic_findings = [
        f for f in findings
        if f.category in ("ssh_brute_force", "web_attack")
    ]
    if deterministic_findings and auto_respond:
        det_results = _deterministic_block(deterministic_findings)
        result["deterministic_results"] = det_results
        logger.info("Deterministic: blocked %d IP(s)", len(det_results))

    # 4. LLM triage for complex threats (only what deterministic can't handle)
    llm_required = [
        f for f in findings
        if f.category not in ("ssh_brute_force", "web_attack", "ssh_successful_auth")
    ]
    if llm_required:
        logger.warning("Security scan: %d complex threat(s) on %s", len(llm_required), node)
        decision = call_llm_for_decision(llm_required, node)
        result["llm_decision"] = decision

        if auto_respond and decision["decision"] != "FALSE_POSITIVE":
            action_results = execute_decision(decision, llm_required)
            result["action_results"] = action_results
            logger.info("LLM triage: executed %d action(s)", len(action_results))

    return result


# ── Startup hook ──────────────────────────────────────────────────────────

def on_startup() -> None:
    """Called when Vigil service starts. Replays bans, creates baseline."""
    from tical_code.guardian.iron_wall import ensure_baseline
    ensure_baseline()
    count = _replay_bans_on_startup()
    if count:
        logger.info("Vigil startup: replayed %d active bans", count)
    else:
        logger.info("Vigil startup: no active bans to replay")
