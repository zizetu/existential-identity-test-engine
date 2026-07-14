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
Iron Wall Security Detection Engine -- Python rewrite of security-watchdog.sh.

Detects real-time threats without bash dependency:
  - SSH brute force attacks (auth.log Failed password patterns)
  - SSH successful auth tracking (auto-whitelist verified IPs)
  - Unauthorized SSH connections from unknown IPs
  - Web application attacks (nginx access log patterns)
  - Reverse shells in running processes
  - New listening ports on non-localhost interfaces
  - Suspicious files in /tmp and /var/tmp
  - SSH authorized_keys changes

All checks use Python stdlib only (subprocess, os, re, pathlib).
Results feed into Vigil's responder for deterministic + LLM-based triage.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tical_code.core.paths import get_guardian_dir


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

# SSH brute force thresholds
BF_THRESHOLD = int(os.environ.get("IRON_WALL_BF_THRESHOLD", "5"))
BF_WINDOW_SECONDS = int(os.environ.get("IRON_WALL_BF_WINDOW", "300"))

# Paths
AUTH_LOG = os.environ.get("IRON_WALL_AUTH_LOG", "/var/log/auth.log")
NGINX_ACCESS_LOG = os.environ.get("IRON_WALL_NGINX_LOG", "/var/log/nginx/access.log")

# Web attack patterns
WEB_ATTACK_PATTERNS = [
    (r'(\.\./){2,}', "Path traversal"),
    (r'union\s+select', "SQL injection (UNION SELECT)"),
    (r"select.*from.*information_schema", "SQL injection (information_schema)"),
    (r'(<script|%3Cscript)', "XSS attempt"),
    (r'wget\s+http', "Remote file download via URL"),
    (r'curl\s+http', "Remote file download via URL"),
    (r'/cgi-bin/', "CGI exploit scan"),
    (r'\.env\b', ".env file probe"),
    (r'wp-admin', "WordPress admin probe"),
    (r'\.git/', ".git directory probe"),
    (r'\/etc\/passwd', "/etc/passwd probe"),
]


@dataclass
class ThreatFinding:
    """A single threat detection result."""
    category: str           # ssh_brute_force, unauthorized_ssh, reverse_shell, new_port,
                            # tmp_malware, key_change, web_attack, port_scan
    severity: str           # CRITICAL, HIGH, MEDIUM
    target: str             # IP, port, filename, PID
    detail: str             # Human-readable description
    evidence: Dict = field(default_factory=dict)  # Raw data for LLM analysis
    timestamp: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# DYNAMIC SAFE IP RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════

def get_dynamic_safe_ips() -> Set[str]:
    """Return the complete set of IPs that should never trigger alerts.

    Sources (union of all):
    1. Static env whitelist (IRON_WALL_SSH_WHITELIST)
    2. Current SSH session IP (SSH_CLIENT / SSH_CONNECTION env vars)
    3. Recently successful SSH auth IPs (parsed from auth.log)
    4. Mesh IPs (EITE_MESH_IPS env var)
    """
    safe: Set[str] = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

    # Static whitelist
    for ip in WHITELIST_SSH_IPS:
        ip = ip.strip()
        if ip:
            safe.add(ip)

    # Current SSH session
    for env_var in ("SSH_CLIENT", "SSH_CONNECTION"):
        val = os.environ.get(env_var, "")
        if val:
            safe.add(val.split()[0])

    # Mesh peers
    mesh_ips = os.environ.get("EITE_MESH_IPS", "")
    for ip in mesh_ips.split(","):
        ip = ip.strip()
        if ip:
            safe.add(ip)

    # Recently successful SSH auth IPs (last 24h)
    success_ips = _get_recent_successful_ssh_ips()
    safe.update(success_ips)

    return safe


def _get_recent_successful_ssh_ips(hours: int = 24) -> Set[str]:
    """Parse auth.log for recently successful SSH publickey authentications."""
    ips: Set[str] = set()
    try:
        if not os.path.exists(AUTH_LOG):
            return ips
        # Accept lines from last N hours
        cutoff = time.time() - (hours * 3600)
        accept_re = re.compile(
            r'Accepted\s+(?:publickey|password)\s+for\s+\S+\s+from\s+(\S+)'
        )
        for line in _read_log_tail(AUTH_LOG, lines=2000):
            ts = _parse_log_timestamp(line)
            if ts and ts < cutoff:
                continue
            m = accept_re.search(line)
            if m:
                ip = m.group(1)
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    ips.add(ip)
    except Exception:
        pass
    return ips


# ═══════════════════════════════════════════════════════════════════════════
# BASELINE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def ensure_baseline() -> Path:
    """Create baseline snapshot on first run if missing. Returns baseline path."""
    gdir = get_guardian_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    baseline_file = gdir / "baseline.json"

    if not baseline_file.exists():
        baseline = _build_baseline()
        baseline_file.write_text(json.dumps(baseline, indent=2, default=str))
        import logging
        logging.getLogger("EITElite.iron-wall").info(
            "Baseline created: %d SSH keys, %d listening ports",
            len(baseline.get("ssh_keys", [])),
            len(baseline.get("listening_ports", [])),
        )

    return baseline_file


def _build_baseline() -> Dict:
    """Capture current system state as baseline.

    Stores SHA256 fingerprints (compatible with Vigil L3 SSHSentinel format),
    NOT raw key lines. This prevents false NEW SSH KEY alerts from the
    older Vigil patrol that also reads this baseline file.
    """
    baseline = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ssh_keys": [],          # SHA256 fingerprints (compatible with old Vigil)
        "ssh_key_lines": [],     # raw key lines (for our check_ssh_key_change)
        "listening_ports": [],
        "ports": {},             # old Vigil PortPatrol format
        "last_check": None,
        "created": datetime.now(timezone.utc).isoformat(),
    }

    # SSH keys — compute SHA256 fingerprints
    ak = Path.home() / ".ssh" / "authorized_keys"
    if ak.exists():
        try:
            raw_lines = [l.strip() for l in ak.read_text().splitlines()
                         if l.strip() and not l.strip().startswith('#')]
            baseline["ssh_key_lines"] = raw_lines
            for line in raw_lines:
                try:
                    r = subprocess.run(
                        ["ssh-keygen", "-lf", "/dev/stdin"],
                        input=line, capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0 and r.stdout:
                        # Format: "256 SHA256:xxx comment (ED25519)"
                        fp = r.stdout.strip().split()[1] if len(r.stdout.split()) >= 2 else ""
                        if fp.startswith("SHA256:"):
                            baseline["ssh_keys"].append(fp)
                except Exception:
                    pass
        except Exception:
            pass

    # Listening ports
    try:
        for line in _run(["ss", "-tlnp"], timeout=10).splitlines():
            if 'LISTEN' not in line:
                continue
            m = re.search(r'(\S+):(\d+)\s', line)
            if m:
                addr, port = m.group(1), int(m.group(2))
                proc = "unknown"
                pm = re.search(r'"([^"]+)"', line)
                if pm:
                    proc = pm.group(1)
                baseline["listening_ports"].append({
                    "addr": addr, "port": port, "process": proc,
                })
    except Exception:
        pass

    return baseline


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


def _read_log_tail(path: str, lines: int = 2000) -> List[str]:
    """Read last N lines of a log file efficiently."""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.splitlines()
    except Exception:
        return []


def _parse_log_timestamp(line: str) -> Optional[float]:
    """Parse syslog timestamp (e.g. 'Jul 13 09:00:00') to epoch float.

    Returns None if unparseable.
    """
    try:
        now = datetime.now()
        ts_str = line[:15].strip()
        parsed = datetime.strptime(f"{now.year} {ts_str}", "%Y %b %d %H:%M:%S")
        if parsed > now:
            parsed = parsed.replace(year=now.year - 1)
        return parsed.timestamp()
    except Exception:
        return None


def check_ssh_brute_force() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect SSH brute force attacks by parsing auth.log Failed password entries.

    Groups failures by IP within BF_WINDOW_SECONDS. IPs with >= BF_THRESHOLD
    failures are flagged. IPs that have also had successful authentications
    in the same window are excluded (false positive prevention).
    """
    findings: List[ThreatFinding] = []
    if not os.path.exists(AUTH_LOG):
        return True, "auth.log not found, skipping brute force check", []

    now = time.time()
    window_start = now - BF_WINDOW_SECONDS
    safe_ips = get_dynamic_safe_ips()

    # Collect failures per IP
    failures: Dict[str, List[Dict]] = defaultdict(list)
    fail_re = re.compile(
        r'Failed\s+password\s+for\s+(?:invalid user\s+)?(\S+)\s+from\s+(\S+)\s+port'
    )

    for line in _read_log_tail(AUTH_LOG, lines=2000):
        ts = _parse_log_timestamp(line)
        if ts is None or ts < window_start:
            continue
        m = fail_re.search(line)
        if not m:
            continue
        ip = m.group(2)
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
            continue
        if ip in safe_ips:
            continue
        failures[ip].append({
            "user": m.group(1),
            "timestamp": ts,
            "line": line.strip(),
        })

    # Flag IPs exceeding threshold
    for ip, attempts in failures.items():
        if len(attempts) >= BF_THRESHOLD:
            unique_users = set(a["user"] for a in attempts)
            findings.append(ThreatFinding(
                category="ssh_brute_force",
                severity="CRITICAL" if len(attempts) >= BF_THRESHOLD * 2 else "HIGH",
                target=ip,
                detail=f"SSH brute force: {len(attempts)} failures from {ip} "
                       f"(users: {', '.join(sorted(unique_users)[:5])})",
                evidence={
                    "ip": ip,
                    "failure_count": len(attempts),
                    "users": sorted(unique_users),
                    "window_seconds": BF_WINDOW_SECONDS,
                    "first_seen": min(a["timestamp"] for a in attempts),
                    "last_seen": max(a["timestamp"] for a in attempts),
                },
            ))

    if findings:
        return False, f"{len(findings)} IP(s) brute forcing SSH", findings
    return True, "No SSH brute force detected", []


def check_ssh_successful_auth() -> Tuple[bool, str, List[ThreatFinding]]:
    """Parse auth.log for successful SSH authentications (informational only).

    This does NOT generate threats -- it's used by get_dynamic_safe_ips()
    to auto-whitelist verified IPs. We run it as a check so the patrol loop
    stays aware of recent successful logins.
    """
    if not os.path.exists(AUTH_LOG):
        return True, "auth.log not found", []

    now = time.time()
    window_start = now - 3600  # Last 1 hour
    accept_re = re.compile(
        r'Accepted\s+(publickey|password)\s+for\s+(\S+)\s+from\s+(\S+)'
    )
    recent: List[Dict] = []

    for line in _read_log_tail(AUTH_LOG, lines=500):
        ts = _parse_log_timestamp(line)
        if ts is None or ts < window_start:
            continue
        m = accept_re.search(line)
        if m:
            recent.append({
                "method": m.group(1),
                "user": m.group(2),
                "ip": m.group(3),
                "timestamp": ts,
            })

    if recent:
        ips = set(r["ip"] for r in recent)
        return True, f"{len(recent)} successful auth(s) from {len(ips)} IP(s) in last hour", []
    return True, "No recent successful SSH auths", []


def check_unauthorized_ssh() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect SSH connections from IPs NOT in the dynamic safe list.

    Unlike the old version that flagged ALL external IPs, this only flags
    IPs that are not in the dynamically-resolved safe set (whitelist +
    SSH_CLIENT + mesh IPs + recent successful auth IPs).
    """
    findings: List[ThreatFinding] = []
    safe_ips = get_dynamic_safe_ips()
    output = _run(["ss", "-tnp"], timeout=10)

    for line in output.splitlines():
        if ':22 ' not in line or 'ESTAB' not in line:
            continue
        parts = line.split()
        for p in parts:
            if ':' in p and not p.startswith('['):
                ip = p.rsplit(':', 1)[0]
                if ip in safe_ips:
                    continue
                # Check if it's an external IP
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    octets = ip.split('.')
                    first = int(octets[0])
                    second = int(octets[1])
                    # Skip RFC1918
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
                        detail=f"SSH connection from unknown IP {ip}",
                        evidence={"ip": ip, "line": line.strip()},
                    ))
                break

    if findings:
        return False, f"{len(findings)} unknown SSH connection(s)", findings
    return True, "No unknown SSH connections", []


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
    """Detect listening ports on non-localhost that are not whitelisted.

    On first run, auto-discovers currently listening ports and adds them
    to the whitelist to avoid false positives on the user's services.
    """
    findings: List[ThreatFinding] = []
    output = _run(["ss", "-tlnp"], timeout=10)

    # Dynamic whitelist: start with static, add ports from baseline
    dynamic_whitelist = set(WHITELIST_PORTS)
    try:
        bf = get_guardian_dir() / "baseline.json"
        if bf.exists():
            bl = json.loads(bf.read_text())
            for entry in bl.get("listening_ports", []):
                dynamic_whitelist.add(entry.get("port", 0))
    except Exception:
        pass

    for line in output.splitlines():
        if 'LISTEN' not in line:
            continue

        addr_match = re.search(r'(\S+):(\d+)\s', line)
        if not addr_match:
            continue
        addr = addr_match.group(1)
        port = int(addr_match.group(2))

        # Skip localhost
        if addr in ('127.0.0.1', '::1', '127.0.0.53', '127.0.0.54', '[::1]'):
            continue
        # Skip whitelisted ports
        if port in dynamic_whitelist:
            continue

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


def check_ssh_key_change(baseline_file: str = "") -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect changes to SSH authorized_keys vs baseline.

    On first run without baseline, auto-creates one.
    """
    if not baseline_file:
        baseline_file = str(ensure_baseline())
    findings: List[ThreatFinding] = []
    ak_file = Path.home() / ".ssh" / "authorized_keys"

    if not ak_file.exists():
        return True, "No authorized_keys file (no keys to monitor)", []

    try:
        current = [l.strip() for l in ak_file.read_text().splitlines()
                   if l.strip() and not l.strip().startswith('#')]
    except Exception:
        return False, "Cannot read authorized_keys", []
    current_count = len(current)

    # Try to load baseline — use ssh_key_lines (raw lines, not fingerprints)
    baseline_count = current_count
    bf = Path(baseline_file)
    if bf.exists():
        try:
            bl = json.loads(bf.read_text())
            baseline_count = len(bl.get("ssh_key_lines", bl.get("ssh_keys", [])))
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


def check_web_attacks() -> Tuple[bool, str, List[ThreatFinding]]:
    """Detect web application attacks from nginx access log.

    Scans for SQL injection, XSS, path traversal, and other common patterns.
    Only runs if nginx access log exists.
    """
    findings: List[ThreatFinding] = []
    if not os.path.exists(NGINX_ACCESS_LOG):
        return True, f"No nginx log at {NGINX_ACCESS_LOG}", []

    now = time.time()
    window_start = now - BF_WINDOW_SECONDS  # Reuse same window as brute force

    # Count per IP to avoid noise
    hits_per_ip: Dict[str, List[str]] = defaultdict(list)

    for line in _read_log_tail(NGINX_ACCESS_LOG, lines=1000):
        for pattern, desc in WEB_ATTACK_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                # Extract IP (first field in combined log format)
                ip = line.split()[0] if line.split() else "unknown"
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    hits_per_ip[ip].append(desc)
                break  # One pattern match per line

    for ip, attack_types in hits_per_ip.items():
        if len(attack_types) >= 3:  # 3+ attack patterns = confirmed scanner
            unique = list(set(attack_types))
            findings.append(ThreatFinding(
                category="web_attack",
                severity="MEDIUM",
                target=ip,
                detail=f"Web attack scanner: {len(attack_types)} hits, "
                       f"types: {', '.join(unique[:5])}",
                evidence={"ip": ip, "hit_count": len(attack_types), "types": unique},
            ))

    if findings:
        return False, f"{len(findings)} web attack IP(s)", findings
    return True, "No web attacks detected", []


# ═══════════════════════════════════════════════════════════════════════════
# COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_CHECK_REGISTRY: List[Tuple[callable, str, str]] = [
    (check_ssh_brute_force,      "ssh_brute_force",      "P0"),
    (check_unauthorized_ssh,      "unauthorized_ssh",      "P0"),
    (check_reverse_shell,         "reverse_shell",         "P0"),
    (check_new_ports,             "new_ports",             "P0"),
    (check_tmp_malware,           "tmp_malware",           "P1"),
    (check_ssh_key_change,        "ssh_key_change",        "P0"),
    (check_ssh_successful_auth,   "ssh_successful_auth",   "P1"),
    (check_web_attacks,           "web_attacks",           "P1"),
]


def run_all_security_checks() -> List[Tuple[str, bool, str, List[ThreatFinding]]]:
    """Run all security checks, return (name, ok, detail, findings)."""
    results = []
    for check_fn, name, _severity in SECURITY_CHECK_REGISTRY:
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
