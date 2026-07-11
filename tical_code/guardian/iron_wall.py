# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Original repository: https://github.com/zizetu/tical-agent

"""
Iron Wall Security Detection Engine — Python rewrite of security-watchdog.sh.

Detects real-time threats without bash dependency:
  - Unauthorized SSH connections
  - Reverse shells
  - New listening ports on non-localhost interfaces
  - Suspicious files in /tmp and /var/tmp
  - SSH authorized_keys changes

All checks use Python stdlib only (subprocess, os, re, pathlib).
Results feed into Vigil's LLM-based responder for intelligent triage.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ── Configuration ─────────────────────────────────────────────────────────
WHITELIST_SSH_IPS: Set[str] = set(
    os.environ.get("IRON_WALL_SSH_WHITELIST", "127.0.0.1").split(",")
)

WHITELIST_PORTS: Set[int] = {
    22, 80, 443, 51820, 2222, 2225, 53, 8642,
}

WHITELIST_PROCESS_NAMES: Set[str] = {
    "sshd", "nginx", "sslh", "python", "python3",
    "uvicorn", "gateway", "cloudflared", "systemd",
    "systemd-resolve", "systemd-network", "systemd-journal",
    "AliYunDun", "AliYunDunMonitor", "AliYunDunUpdate",
    "aliyun-service", "aliyun_assistant",
}

REVERSE_SHELL_PATTERNS = [
    r'bash -i',
    r'nc -e',
    r'ncat -e',
    r'socat exec',
    r'python.*socket.*connect.*\(',
    r'/dev/tcp/',
]

SUSPICIOUS_TMP_GLOBS = [
    '.fefc*', '*.so', '*token*', '*key*',
    '*secret*', '*password*', 'authorized_keys',
]


@dataclass
class ThreatFinding:
    """A single threat detection result."""
    category: str           # unauthorized_ssh, reverse_shell, new_port, tmp_malware, key_change
    severity: str           # CRITICAL, HIGH, MEDIUM
    target: str             # IP, port, filename, PID
    detail: str             # Human-readable description
    evidence: Dict = field(default_factory=dict)  # Raw data for LLM analysis
    timestamp: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# DETECTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _run(cmd: List[str], timeout: int = 10) -> str:
    """Run a command, return stdout or empty string."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def check_unauthorized_ssh() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect SSH connections from non-whitelist IPs."""
    findings: List[ThreatFinding] = []
    output = _run(["ss", "-tnp"], timeout=10)

    for line in output.splitlines():
        if ':22 ' not in line or 'ESTAB' not in line:
            continue
        # Extract remote IP
        parts = line.split()
        for p in parts:
            if ':' in p and not p.startswith('['):
                ip = p.rsplit(':', 1)[0]
                if ip in WHITELIST_SSH_IPS:
                    continue
                if ip in ('127.0.0.1', '::1', '0.0.0.0', '*'):
                    continue
                # Valid external IP? Skip RFC1918 internal ranges
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    parts_ip = ip.split('.')
                    first = int(parts_ip[0])
                    second = int(parts_ip[1])
                    # Skip RFC1918: 10.x, 172.16-31.x, 192.168.x
                    if first == 10:
                        continue
                    if first == 172 and 16 <= second <= 31:
                        continue
                    if first == 192 and second == 168:
                        continue
                    findings.append(ThreatFinding(
                        category="unauthorized_ssh",
                        severity="CRITICAL",
                        target=ip,
                        detail=f"Unauthorized SSH connection from {ip}",
                        evidence={"ip": ip, "line": line.strip()},
                    ))
                break

    if findings:
        return False, f"{len(findings)} unauthorized SSH connection(s)", findings
    return True, "No unauthorized SSH connections", []


def check_reverse_shell() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect reverse shell patterns in running processes."""
    findings: List[ThreatFinding] = []
    output = _run(["ps", "aux"], timeout=10)

    for pattern in REVERSE_SHELL_PATTERNS:
        pat = re.compile(pattern, re.IGNORECASE)
        for line in output.splitlines():
            if 'grep' in line:
                continue
            if pat.search(line):
                parts = line.split()
                pid = parts[1] if len(parts) > 1 else "?"
                findings.append(ThreatFinding(
                    category="reverse_shell",
                    severity="CRITICAL",
                    target=f"PID:{pid}",
                    detail=f"Reverse shell pattern detected: {pattern}",
                    evidence={"pid": pid, "cmdline": line.strip(), "pattern": pattern},
                ))

    if findings:
        return False, f"{len(findings)} reverse shell(s) detected", findings
    return True, "No reverse shells detected", []


def check_new_ports() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect listening ports on non-localhost that are not whitelisted."""
    findings: List[ThreatFinding] = []
    output = _run(["ss", "-tlnp"], timeout=10)

    for line in output.splitlines():
        if 'LISTEN' not in line:
            continue

        # Parse address:port
        addr_match = re.search(r'(\S+):(\d+)\s', line)
        if not addr_match:
            continue
        addr = addr_match.group(1)
        port = int(addr_match.group(2))

        # Skip localhost
        if addr in ('127.0.0.1', '::1', '127.0.0.53', '127.0.0.54', '[::1]'):
            continue
        # Skip whitelisted ports
        if port in WHITELIST_PORTS:
            continue

        # Extract process name
        proc = "unknown"
        proc_match = re.search(r'"([^"]+)"', line)
        if proc_match:
            proc = proc_match.group(1)

        findings.append(ThreatFinding(
            category="new_port",
            severity="HIGH",
            target=f"{addr}:{port}",
            detail=f"Non-whitelist port {port} open on {addr} ({proc})",
            evidence={"addr": addr, "port": port, "process": proc, "line": line.strip()},
        ))

    if findings:
        return False, f"{len(findings)} non-whitelist port(s)", findings
    return True, "All ports within whitelist", []


def check_tmp_malware() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect suspicious files in /tmp and /var/tmp."""
    findings: List[ThreatFinding] = []
    exclude = {'gateway-results', 'wg-'}

    for tmp_dir in ['/tmp', '/var/tmp']:
        if not os.path.isdir(tmp_dir):
            continue
        for entry in os.listdir(tmp_dir):
            full = os.path.join(tmp_dir, entry)
            if not os.path.isfile(full):
                continue

            # Check against suspicious patterns
            name_lower = entry.lower()
            suspicious = False
            reason = ""

            if entry.startswith('.fefc'):
                suspicious = True
                reason = "Hidden .fefc file (malware pattern)"
            elif entry.endswith('.so') and entry.startswith('.'):
                suspicious = True
                reason = "Hidden .so file (possible rootkit)"
            elif any(kw in name_lower for kw in ('token', 'key', 'secret', 'password')):
                suspicious = True
                reason = f"File with credential keyword: {entry}"
            elif entry == 'authorized_keys':
                suspicious = True
                reason = "authorized_keys in temp directory"

            if not suspicious:
                continue

            # Skip excluded
            if any(ex in entry for ex in exclude):
                continue

            findings.append(ThreatFinding(
                category="tmp_malware",
                severity="HIGH",
                target=full,
                detail=reason,
                evidence={"path": full, "size": os.path.getsize(full)},
            ))

    if findings:
        return False, f"{len(findings)} suspicious file(s) in /tmp", findings
    return True, "No suspicious files in /tmp", []


def check_ssh_key_change(baseline_file: str = "/opt/tical-guardian/baseline.json") -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect changes to SSH authorized_keys vs baseline."""
    findings: List[ThreatFinding] = []
    ak_file = Path.home() / ".ssh" / "authorized_keys"

    if not ak_file.exists():
        return False, "authorized_keys missing", [
            ThreatFinding(category="key_change", severity="CRITICAL",
                          target=str(ak_file), detail="authorized_keys file missing")
        ]

    try:
        current = [l.strip() for l in ak_file.read_text().splitlines()
                   if l.strip() and not l.strip().startswith('#')]
    except Exception:
        return False, "Cannot read authorized_keys", []

    current_count = len(current)

    # Try to load baseline
    baseline_count = current_count
    bf = Path(baseline_file)
    if bf.exists():
        try:
            import json
            bl = json.loads(bf.read_text())
            baseline_count = len(bl.get("ssh_keys", []))
        except Exception:
            pass

    if current_count > baseline_count + 1:
        findings.append(ThreatFinding(
            category="key_change",
            severity="CRITICAL",
            target=str(ak_file),
            detail=f"SSH keys changed: {baseline_count} -> {current_count}",
            evidence={"baseline_count": baseline_count, "current_count": current_count},
        ))
        return False, f"SSH key count changed ({baseline_count} -> {current_count})", findings

    return True, f"SSH keys unchanged ({current_count})", []


# ═══════════════════════════════════════════════════════════════════════════
# COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_CHECK_REGISTRY: List[Tuple[callable, str, str]] = [
    (check_unauthorized_ssh,   "unauthorized_ssh",   "P0"),
    (check_reverse_shell,      "reverse_shell",      "P0"),
    (check_new_ports,          "new_ports",          "P0"),
    (check_tmp_malware,        "tmp_malware",        "P1"),
    (check_ssh_key_change,     "ssh_key_change",     "P0"),
]


def run_all_security_checks() -> List[Tuple[str, bool, str, List[ThreatFinding]]]:
    """Run all security checks, return (name, ok, detail, findings)."""
    results = []
    for check_fn, name, severity in SECURITY_CHECK_REGISTRY:
        try:
            ok, detail, findings = check_fn()
        except Exception as exc:
            ok = False
            detail = f"Check raised exception: {exc}"
            findings = []
        results.append((name, ok, detail, findings))
    return results


def get_threat_summary(check_results) -> List[ThreatFinding]:
    """Extract all ThreatFindings from check results."""
    all_findings: List[ThreatFinding] = []
    for _, _, _, findings in check_results:
        all_findings.extend(findings)
    return all_findings
