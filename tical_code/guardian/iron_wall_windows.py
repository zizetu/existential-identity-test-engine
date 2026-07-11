"""
Iron Wall Security Detection Engine — Windows Edition
Adapted from tical-code guardian/iron_wall.py for Windows 10/11.

Detects:
  - Unauthorized SSH connections
  - Reverse shells / suspicious processes
  - New listening ports on non-localhost interfaces
  - Malware in TEMP directories
  - SSH authorized_keys changes
  - Suspicious scheduled tasks
  - Windows Defender status changes

All checks use Python stdlib only (subprocess, os, re, pathlib, json).
"""
from __future__ import annotations

import os
import re
import json
import time
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────
WHITELIST_SSH_IPS: Set[str] = set(
    os.environ.get("IRON_WALL_SSH_WHITELIST", "43.133.234.190").split(",")
)

WHITELIST_PORTS: Set[int] = {
    22, 135, 139, 445, 5040,  # SSH, RPC, NetBIOS, SMB, CDP
    14013, 14016, 14019, 14022, 14023,  # WeChat
    9998,  # Iron Wall HTTP transfer
}

WHITELIST_PROCESS_NAMES: Set[str] = {
    "sshd.exe", "ssh.exe", "cmd.exe", "powershell.exe",
    "python.exe", "python3.exe", "pythonw.exe",
    "svchost.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "smss.exe", "spoolsv.exe",
    "explorer.exe", "dwm.exe", "sihost.exe", "taskhostw.exe",
    "fontdrvhost.exe", "ctfmon.exe", "runtimebroker.exe",
    "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "systemsettings.exe",
    "onedrive.exe", "securityhealthsystray.exe",
    "msedge.exe", "chrome.exe", "firefox.exe",
    "weixin.exe", "wechat.exe", "steam.exe",
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "vssvc.exe", "wlms.exe", "wslservice.exe",
}

REVERSE_SHELL_PATTERNS = [
    r'nc\s+-e', r'ncat\s+-e', r'socat\s+exec',
    r'python.*socket.*connect',
    r'powershell.*-e\s+\w{20,}',       # encoded PS
    r'powershell.*-enc\s+\w{20,}',     # base64 PS
    r'cmd.*/c.*nc\s',                   # cmd + netcat
    r'mshta\s+http',                    # mshta download
    r'regsvr32.*/s.*http',             # regsvr32 download
    r'rundll32.*javascript',            # rundll32 JS
    r'certutil.*-urlcache.*-f\s+http', # certutil download
]

SUSPICIOUS_TMP_GLOBS = [
    '.b8f*.dll', '.fefc*', '*.so',
    '*token*', '*key*', '*secret*', '*password*',
    'socks5*.py', '*keepalive*.py',
]

MALICIOUS_TASK_WHITELIST = [
    r'Pro7Tunnel',  # legitimate SSH tunnel task
]

MALICIOUS_TASK_PATTERNS = [
    r'socks5', r'rustdesk', r'anydesk',
    r'testwhoami', r'cdptest', r'ctrlsrv',
    r'keepalive', r'tunnel',
    r'b8f', r'fefc',
]

HOME = Path.home()
AK_FILE = HOME / ".ssh" / "authorized_keys"
AK_HASH_FILE = HOME / "iron_wall_ak.sha256"
BASELINE_FILE = HOME / "iron_wall_baseline.json"
LOG_FILE = HOME / "iron_wall.log"

TEMP_DIRS = [
    os.environ.get("TEMP", str(HOME / "AppData" / "Local" / "Temp")),
    os.environ.get("WINDIR", "C:\\Windows") + "\\Temp",
]


@dataclass
class ThreatFinding:
    category: str
    severity: str
    target: str
    detail: str
    evidence: Dict = field(default_factory=dict)
    timestamp: str = ""


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run(cmd: List[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return r.stdout
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# CHECK 1: Unauthorized SSH connections
# ═══════════════════════════════════════════════════════════════

def check_unauthorized_ssh() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["netstat", "-ano", "-p", "tcp"], timeout=10)

    for line in output.splitlines():
        if ":22 " not in line or "ESTABLISHED" not in line:
            continue
        parts = line.split()
        for p in parts:
            if ":" in p and not p.startswith("["):
                ip = p.rsplit(":", 1)[0]
                if ip in WHITELIST_SSH_IPS:
                    continue
                if ip in ("127.0.0.1", "::1", "0.0.0.0", "*", "[::1]"):
                    continue
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    octets = ip.split(".")
                    first = int(octets[0])
                    second = int(octets[1])
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
                        detail=f"SSH from {ip}",
                        evidence={"ip": ip, "line": line.strip()},
                    ))
                break

    return (
        (True, "No unauthorized SSH", [])
        if not findings
        else (False, f"{len(findings)} unauthorized SSH", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 2: Reverse shell / suspicious process
# ═══════════════════════════════════════════════════════════════

def check_reverse_shell() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["tasklist", "/v", "/fo", "csv"], timeout=15)

    for pattern in REVERSE_SHELL_PATTERNS:
        pat = re.compile(pattern, re.IGNORECASE)
        for line in output.splitlines():
            if pat.search(line):
                findings.append(ThreatFinding(
                    category="reverse_shell",
                    severity="CRITICAL",
                    target="suspicious_cmd",
                    detail=f"Pattern: {pattern}",
                    evidence={"cmdline": line.strip()[:200], "pattern": pattern},
                ))

    return (
        (True, "No reverse shells", [])
        if not findings
        else (False, f"{len(findings)} suspicious commands", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 3: New listening ports (non-localhost)
# ═══════════════════════════════════════════════════════════════

def check_new_ports() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["netstat", "-ano", "-p", "tcp"], timeout=10)

    for line in output.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr_port = parts[1]
        pid = parts[-1] if parts[-1].isdigit() else "?"

        if ":" not in addr_port:
            continue
        addr, port_str = addr_port.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            continue

        if addr in ("127.0.0.1", "::1", "[::1]"):
            continue  # skip localhost-only

        if port in WHITELIST_PORTS:
            continue
        if 49152 <= port <= 65535:
            continue  # dynamic RPC ports

        findings.append(ThreatFinding(
            category="new_port",
            severity="HIGH",
            target=f"{addr}:{port}",
            detail=f"Port {port} open on {addr} (PID {pid})",
            evidence={"addr": addr, "port": port, "pid": pid, "line": line.strip()},
        ))

    return (
        (True, "All ports whitelisted", [])
        if not findings
        else (False, f"{len(findings)} new ports", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 4: Malware in TEMP
# ═══════════════════════════════════════════════════════════════

def check_tmp_malware() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    
    for tmp_dir in TEMP_DIRS:
        if not os.path.isdir(tmp_dir):
            continue
        try:
            files = os.listdir(tmp_dir)
        except PermissionError:
            continue

        for fname in files:
            name_lower = fname.lower()
            suspicious = False
            reason = ""

            if ".b8f" in fname and fname.endswith(".dll"):
                suspicious = True
                reason = "Malware .b8f*.dll pattern"
            elif fname.startswith(".fefc") and (fname.endswith(".so") or "." not in fname[5:]):
                suspicious = True
                reason = "Malware .fefc* pattern"
            elif fname.endswith(".so") and fname.startswith("."):
                suspicious = True
                reason = "Hidden .so (possible rootkit)"
            elif any(kw in name_lower for kw in ("token", "key", "secret", "password")) and os.path.isfile(os.path.join(tmp_dir, fname)):
                suspicious = True
                reason = f"Credential leak: {fname}"

            if not suspicious:
                continue

            full = os.path.join(tmp_dir, fname)
            findings.append(ThreatFinding(
                category="tmp_malware",
                severity="HIGH",
                target=full,
                detail=reason,
                evidence={"path": full, "size": os.path.getsize(full) if os.path.isfile(full) else 0},
            ))

    return (
        (True, "TEMP clean", [])
        if not findings
        else (False, f"{len(findings)} suspicious files", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 5: authorized_keys integrity
# ═══════════════════════════════════════════════════════════════

def check_ssh_key_change() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []

    if not AK_FILE.exists():
        return (False, "authorized_keys missing", [
            ThreatFinding(category="key_change", severity="CRITICAL",
                          target=str(AK_FILE), detail="authorized_keys missing")
        ])

    try:
        raw = AK_FILE.read_bytes()
        current_hash = hashlib.sha256(raw).hexdigest()
    except Exception:
        return (False, "Cannot read authorized_keys", [])

    # Check for NULL byte injection
    if b"\x00" in raw:
        findings.append(ThreatFinding(
            category="key_change",
            severity="CRITICAL",
            target=str(AK_FILE),
            detail="NULL BYTE injection in authorized_keys",
            evidence={"technique": "NULL byte SSH backdoor"},
        ))

    # Check hash vs baseline
    if AK_HASH_FILE.exists():
        try:
            saved_hash = AK_HASH_FILE.read_text().strip()
            if current_hash != saved_hash:
                findings.append(ThreatFinding(
                    category="key_change",
                    severity="CRITICAL",
                    target=str(AK_FILE),
                    detail="authorized_keys hash changed",
                    evidence={"old_hash": saved_hash, "new_hash": current_hash},
                ))
        except Exception:
            pass
    else:
        AK_HASH_FILE.write_text(current_hash)
        log(f"Baseline SHA256 saved: {current_hash[:16]}...")

    return (
        (True, f"SSH keys OK ({current_hash[:8]})", [])
        if not findings
        else (False, "authorized_keys tampered", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 6: Suspicious scheduled tasks (Windows-specific)
# ═══════════════════════════════════════════════════════════════

def check_scheduled_tasks() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["schtasks", "/query", "/fo", "list", "/v"], timeout=30)

    for pattern in MALICIOUS_TASK_PATTERNS:
        pat = re.compile(pattern, re.IGNORECASE)
        for line in output.splitlines():
            if "TaskName:" in line:
                task_name = line.split(":", 1)[1].strip()
                # Skip whitelisted tasks
                whitelisted = False
                for wl in MALICIOUS_TASK_WHITELIST:
                    if re.search(wl, task_name, re.IGNORECASE):
                        whitelisted = True
                        break
                if whitelisted:
                    continue
                if pat.search(task_name):
                    findings.append(ThreatFinding(
                        category="malicious_task",
                        severity="HIGH",
                        target=task_name,
                        detail=f"Suspicious task '{task_name}' matches '{pattern}'",
                        evidence={"pattern": pattern, "task_name": task_name},
                    ))

    return (
        (True, "No malicious tasks", [])
        if not findings
        else (False, f"{len(findings)} suspicious tasks", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 7: Malicious services (Windows-specific)
# ═══════════════════════════════════════════════════════════════

MALICIOUS_SERVICE_PATTERNS = [
    r'rustdesk', r'anydesk', r'teamviewer', r'vnc',
    r'logmein', r'splashtop', r'ultra', r'screenconnect',
    r'b8f', r'fefc',
]

def check_services() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["sc", "query", "state=", "all"], timeout=20)

    for pattern in MALICIOUS_SERVICE_PATTERNS:
        pat = re.compile(pattern, re.IGNORECASE)
        for line in output.splitlines():
            if "SERVICE_NAME" in line and pat.search(line):
                findings.append(ThreatFinding(
                    category="malicious_service",
                    severity="CRITICAL",
                    target=line.strip(),
                    detail=f"Malicious service: {line.strip()}",
                    evidence={"line": line.strip(), "pattern": pattern},
                ))

    return (
        (True, "No malicious services", [])
        if not findings
        else (False, f"{len(findings)} malicious services", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK 8: Windows Defender status
# ═══════════════════════════════════════════════════════════════

def check_defender() -> Tuple[bool, str, List[ThreatFinding]]:
    findings: List[ThreatFinding] = []
    output = _run(["powershell", "-c",
        "Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled | ConvertTo-Json"],
        timeout=15)

    try:
        status = json.loads(output)
        if not status.get("AntivirusEnabled"):
            findings.append(ThreatFinding(
                category="defender_off",
                severity="CRITICAL",
                target="Windows Defender",
                detail="Antivirus disabled",
            ))
        if not status.get("RealTimeProtectionEnabled"):
            findings.append(ThreatFinding(
                category="defender_off",
                severity="CRITICAL",
                target="Windows Defender",
                detail="Real-time protection disabled",
            ))
    except json.JSONDecodeError:
        pass

    return (
        (True, "Defender OK", [])
        if not findings
        else (False, "Defender issues", findings)
    )


# ═══════════════════════════════════════════════════════════════
# CHECK REGISTRY
# ═══════════════════════════════════════════════════════════════

SECURITY_CHECK_REGISTRY = [
    (check_unauthorized_ssh,   "unauthorized_ssh"),
    (check_reverse_shell,      "reverse_shell"),
    (check_new_ports,          "new_ports"),
    (check_tmp_malware,        "tmp_malware"),
    (check_ssh_key_change,     "ssh_key_change"),
    (check_scheduled_tasks,    "scheduled_tasks"),
    (check_services,           "services"),
    (check_defender,           "defender"),
]


def run_all_security_checks() -> List[Tuple[str, bool, str, List[ThreatFinding]]]:
    results = []
    for check_fn, name in SECURITY_CHECK_REGISTRY:
        try:
            ok, detail, findings = check_fn()
        except Exception as exc:
            ok = False
            detail = f"Exception: {exc}"
            findings = []
        results.append((name, ok, detail, findings))
    return results


def get_threat_summary(check_results) -> List[ThreatFinding]:
    all_findings: List[ThreatFinding] = []
    for _, _, _, findings in check_results:
        all_findings.extend(findings)
    return all_findings


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log("=== Iron Wall Windows — Scan Start ===")
    results = run_all_security_checks()
    threats = get_threat_summary(results)

    for name, ok, detail, findings in results:
        status = "OK" if ok else "!!"
        log(f"  [{status}] {name}: {detail}")

    log(f"=== Scan Complete: {len(threats)} threats found ===")

    # Print JSON for external consumption (Gateway agent / Telegram)
    if threats:
        report = {
            "timestamp": datetime.now().isoformat(),
            "threats": [
                {"category": t.category, "severity": t.severity,
                 "target": t.target, "detail": t.detail}
                for t in threats
            ]
        }
        print("\n--- THREAT REPORT ---")
        print(json.dumps(report, indent=2))
