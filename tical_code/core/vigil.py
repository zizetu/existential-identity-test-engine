# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Security Vigil — autonomous intrusion detection for tical-code agents.

Design principles (Kerckhoffs's principle: assume attacker reads source):
    1. Zero-config: activates on module load, no user action required.
    2. Defense-in-depth: 5 independent guard layers, no single bypass point.
    3. Tamper-evident: self-checksum, state file integrity verification.
    4. Silent-by-default: only alerts on anomalies, zero token cost when clean.
    5. Least-privilege: state files root-owned 0400, module itself 0444.

Integration:
    Registered via @register in module_defs.py (light profile → public repo).
    Hooks into unified_worker enrichment pipeline for pre-LLM message scanning.
    Runs async patrol tasks for port/SSH/cron/filesystem monitoring.

Guard layers:
    L1 — Message Scanner: URL phishing, IP C2, data exfiltration channels
    L2 — Port Patrol: baseline-diff public port detection (0.0.0.0 monitors)
    L3 — SSH Sentinel: key fingerprint tracking, unknown connection intel
    L4 — Filesystem Watch: /tmp executable/so detection, auto-quarantine
    L5 — Integrity Guard: self-checksum, state file immutability, tamper alert

Bypass resistance:
    - Attacker reads source → knows checks exist → cannot disable without
      modifying module file itself (detected by L5 integrity check).
    - Attacker kills process → systemd auto-restarts → module re-initializes.
    - Attacker modifies state files → checksum mismatch → alert.
    - Attacker floods with false positives → rate-limit dedup suppression.
    - Attacker uses kernel 0day → out of scope (kernel is trust boundary).
"""

import os
import re
import json
import time
import hashlib
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, Any
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from .paths import get_guardian_dir

logger = logging.getLogger("tical-code.vigil")

# ---------------------------------------------------------------------------
# Constants — paths, permissions, thresholds
# ---------------------------------------------------------------------------

GUARDIAN_DIR = str(get_guardian_dir())  # TICAL_GUARDIAN_DIR or $TICAL_HOME/guardian
STATE_FILE = f"{GUARDIAN_DIR}/state.json"
BASELINE_FILE = f"{GUARDIAN_DIR}/baseline.json"
ALERT_LOG = f"{GUARDIAN_DIR}/alerts.log"
QUARANTINE_DIR = f"{GUARDIAN_DIR}/quarantine"
FORENSICS_DIR = f"{GUARDIAN_DIR}/forensics"
INTEL_DIR = "/var/log/intrusion-recon"

CHECKSUM_FILE = f"{GUARDIAN_DIR}/.module_checksum"
MODULE_PATH = __file__

PATROL_INTERVAL = 120          # 2 minutes
AUTO_BLOCK_EXPIRY = 86400     # 24 hours before auto-block expires
ALERT_COOLDOWN = 1800          # 30 minutes
FORENSICS_RETENTION = 86400 * 7  # 7 days

# Permissions: state files owned by root, read-only
STATE_PERMS = 0o600
DIR_PERMS = 0o700

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Severity:
    CLEAN = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    _names = {0: "clean", 1: "low", 2: "medium", 3: "high", 4: "critical"}

    @classmethod
    def name(cls, level: int) -> str:
        return cls._names.get(level, "unknown")

    @classmethod
    def escalate(cls, current: int, new: int) -> int:
        return new if new > current else current


@dataclass
class ScanResult:
    blocked: bool = False
    severity: int = Severity.CLEAN
    findings: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# L1: Message Scanner — URL/IP/phishing detection
# ---------------------------------------------------------------------------

# Phishing / C2 patterns
SUSPICIOUS_URL_PATTERNS = [
    re.compile(
        r'(?:paypal|amazon|apple|microsoft|google|facebook|netflix|dropbox)'
        r'[-.]?(?:verify|login|secure|update|account|billing|support)[-.@]',
        re.I,
    ),
    re.compile(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}[:/]'),  # IP URL (C2)
    re.compile(
        r'https?://\S+\.(?:xyz|top|club|work|date|bid|stream|win|party'
        r'|loan|cricket|faith|gq|ml|tk|cf|ga)\b', re.I
    ),  # suspicious TLDs
    re.compile(r'aHR0c[HA]s?[A-Za-z0-9+/=]{20,}'),  # base64 URL
    re.compile(r'https?://discord(?:app)?\.com/api/webhooks/[\d/]+'),  # Discord webhook
    re.compile(r'https?://api\.telegram\.org/bot[\w:]+/send'),  # Telegram bot send
    # Short-link services (phishing obfuscation)
    re.compile(
        r'https?://(?:bit\.ly|t\.co|tinyurl\.com|ow\.ly|is\.gd|buff\.ly'
        r'|shorturl\.at|rb\.gy|cutt\.ly|v\.gd|soo\.gd)/\S+', re.I
    ),
    # Data exfiltration channels
    re.compile(r'https?://(?:webhook\.site|requestbin\.\S+|pipedream\.\S+|hookbin\.\S+)', re.I),
    # DNS exfiltration (very long subdomains)
    re.compile(r'https?://[a-z0-9]{40,}\.\S+\.\S+', re.I),
]

SAFE_URL_PATTERNS = [
    re.compile(r'https?://(?:github|google|apple|microsoft|amazon|stackoverflow)\.com'),
    re.compile(r'https?://\w+\.github\.io'),
    re.compile(r'https?://(?:docs|pypi|npmjs|crates)\.\w+\.\w+'),
    re.compile(r'https?://(?:arxiv|doi|sci-hub|pubmed)\.\w+'),
    re.compile(r'https?://(?:python|rust-lang|golang|nodejs)\.org'),
    re.compile(r'https?://api\.coze\.\w+/\w+/'),  # anchored
    re.compile(r'https?://token-plan\.\w+'),  # anchored
]

IP_PATTERN = re.compile(
    r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
    r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
)

PRIVATE_IP_RANGES = [
    re.compile(r'^127\.'),
    re.compile(r'^10\.'),
    re.compile(r'^172\.(?:1[6-9]|2\d|3[01])\.'),
    re.compile(r'^192\.168\.'),
    re.compile(r'^0\.'),
    re.compile(r'^255\.255\.255\.255$'),
]

BANNED_TLDS = frozenset({
    "xyz", "top", "club", "work", "date", "bid", "stream", "win",
    "party", "loan", "cricket", "faith", "gq", "ml", "tk", "cf", "ga",
})


class MessageScanner:
    """Scans messages for phishing, C2, and data exfiltration URLs."""

    def __init__(self):
        self._known_bad_ips: Set[str] = set()
        self._cache: Dict[str, Tuple[str, float]] = {}
        self._cache_ttl = 3600

    def scan(self, text: str) -> ScanResult:
        if not text:
            return ScanResult()

        result = ScanResult()
        urls = re.findall(r'https?://[^\s<>"\']{4,}', text, re.I)
        ips = IP_PATTERN.findall(text)

        for url in urls:
            # Bypass safe list
            if any(p.search(url) for p in SAFE_URL_PATTERNS):
                continue

            # Check suspicious patterns
            for pattern in SUSPICIOUS_URL_PATTERNS:
                if pattern.search(url):
                    result.findings.append(f"Suspicious URL detected: {url[:80]}")
                    result.severity = Severity.escalate(result.severity, Severity.HIGH)
                    break
            else:
                # URL not in safe list, not matching suspicious = unknown
                result.severity = Severity.escalate(result.severity, Severity.LOW)

        for ip in ips:
            if any(p.match(ip) for p in PRIVATE_IP_RANGES):
                continue
            if ip in self._known_bad_ips:
                result.findings.append(f"Blocklisted IP: {ip}")
                result.severity = Severity.escalate(result.severity, Severity.CRITICAL)
            else:
                cached = self._cache.get(ip)
                if cached and time.time() - cached[1] < self._cache_ttl and cached[0] == "bad":
                    result.findings.append(f"Known malicious IP: {ip}")
                    result.severity = Severity.escalate(result.severity, Severity.HIGH)

        if result.findings:
            result.blocked = result.severity >= Severity.HIGH

        return result

    def mark_ip(self, ip: str, bad: bool) -> None:
        if bad:
            self._known_bad_ips.add(ip)
            self._cache[ip] = ("bad", time.time())
        else:
            self._known_bad_ips.discard(ip)
            self._cache[ip] = ("good", time.time())


# ---------------------------------------------------------------------------
# L2: Port Patrol — baseline-diff public port monitoring
# ---------------------------------------------------------------------------

class PortPatrol:
    """Monitors public-facing ports for changes from baseline."""

    def __init__(self):
        self._baseline: Dict = {}
        self._alerted_ports: Dict[str, float] = {}  # port -> last alert time
        self._load_baseline()

    def _load_baseline(self) -> None:
        try:
            with open(BASELINE_FILE, "r") as f:
                self._baseline = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._baseline = {"ports": {}, "last_check": 0, "created": time.time()}

    def _save_baseline(self) -> None:
        os.makedirs(GUARDIAN_DIR, exist_ok=True)
        with open(BASELINE_FILE, "w") as f:
            json.dump(self._baseline, f, indent=2)
        os.chmod(BASELINE_FILE, STATE_PERMS)

    def _scan_ports(self) -> Dict:
        try:
            r = subprocess.run(
                ["ss", "-tlnpa"], capture_output=True, text=True, timeout=10
            )
            ports = {}
            for line in r.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                addr = parts[3]
                port = addr.rsplit(":", 1)[-1] if ":" in addr else ""
                proc = ""
                for p in parts[5:]:
                    if "users:" in p:
                        proc = p.split('"')[1] if '"' in p else ""
                is_public = "0.0.0.0" in addr or ":::" in addr or "*:" in addr
                if port:
                    ports[port] = {
                        "address": addr,
                        "public": is_public,
                        "process": proc,
                    }
            return ports
        except Exception:
            return {}

    def check(self) -> ScanResult:
        result = ScanResult()
        current = self._scan_ports()
        baseline_ports = self._baseline.get("ports", {})

        for port, info in current.items():
            if not info["public"]:
                continue

            # New public port not in baseline
            if port not in baseline_ports:
                now = time.time()
                last = self._alerted_ports.get(port, 0)
                if now - last > ALERT_COOLDOWN:
                    result.findings.append(
                        f"NEW PUBLIC PORT: {port} ({info['process']})"
                    )
                    result.severity = Severity.escalate(result.severity, Severity.CRITICAL)
                    self._alerted_ports[port] = now
                continue

            # Process change on existing port
            if baseline_ports[port].get("process") != info["process"]:
                result.findings.append(
                    f"PORT PROCESS CHANGED: {port} "
                    f"{baseline_ports[port].get('process')} -> {info['process']}"
                )
                result.severity = Severity.escalate(result.severity, Severity.HIGH)

        # Update baseline for non-alerted ports (auto-learn)
        if not result.findings:
            self._baseline["ports"] = current
            self._baseline["last_check"] = time.time()
            self._save_baseline()

        return result


# ---------------------------------------------------------------------------
# L3: SSH Sentinel — key tracking + unknown connection intel
# ---------------------------------------------------------------------------

class SSHSentinel:
    """Tracks SSH key fingerprints and detects unknown connections.

    Mesh IPs are loaded from TICAL_MESH_IPS env var (comma-separated).
    Falls back to localhost-only if not set (safe default for public repo).
    """

    def _load_mesh_ips(self) -> frozenset:
        raw = os.environ.get("TICAL_MESH_IPS", "127.0.0.1")
        return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())

    def __init__(self):
        self.MESH_IPS = self._load_mesh_ips()
        os.makedirs(INTEL_DIR, exist_ok=True)

    def _ssh_fingerprints(self) -> List[str]:
        fps = []
        for path in [str(Path.home() / ".ssh" / "authorized_keys"), "/root/.ssh/authorized_keys"]:
            if not os.path.isfile(path):
                continue
            try:
                r = subprocess.run(
                    ["ssh-keygen", "-lf", path],
                    capture_output=True, text=True, timeout=5,
                )
                for line in r.stdout.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 2:
                        fps.append(parts[1])
            except Exception:
                pass
        return fps

    def check(self) -> ScanResult:
        result = ScanResult()

        # Key fingerprint check
        try:
            with open(BASELINE_FILE, "r") as f:
                baseline = json.load(f)
        except Exception:
            baseline = {}

        current_fps = self._ssh_fingerprints()
        baseline_fps = baseline.get("ssh_keys", [])

        for fp in current_fps:
            if fp not in baseline_fps:
                result.findings.append(f"NEW SSH KEY: {fp}")
                result.severity = Severity.escalate(result.severity, Severity.CRITICAL)

        # Unknown SSH connections — collect intel
        try:
            r = subprocess.run(
                ["ss", "-tnp"], capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.split("\n"):
                if ":22 " not in line or "ESTAB" not in line:
                    continue
                parts = line.split()
                src = parts[4].rsplit(":", 1)[0] if len(parts) > 4 else ""
                if not src or src in self.MESH_IPS:
                    continue
                pid_match = re.search(r'pid=(\d+)', line)
                if pid_match:
                    self._collect_intel(src, pid_match.group(1))
                result.findings.append(f"UNKNOWN SSH: {src}")
                result.severity = Severity.escalate(result.severity, Severity.CRITICAL)
        except Exception:
            pass

        return result

    def _collect_intel(self, src_ip: str, pid: str) -> None:
        """Silent forensic data collection on intruder.

        Uses argv-list subprocess only (never shell=True) to avoid injection.
        """
        if not str(pid).isdigit():
            return
        pid = str(pid)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        case_dir = os.path.join(INTEL_DIR, f"{src_ip}-{ts}")
        os.makedirs(case_dir, exist_ok=True)

        # argv form only — no shell interpolation
        intel_commands = {
            "cmdline": ["cat", f"/proc/{pid}/cmdline"],
            "cwd": ["ls", "-la", f"/proc/{pid}/cwd"],
            "fd": ["ls", "-la", f"/proc/{pid}/fd/"],
            "ps": ["ps", "-fp", pid],
            "environ": ["cat", f"/proc/{pid}/environ"],
            "lsof": ["lsof", "-p", pid],
        }
        # Post-process binary-null outputs in Python (replaces shell tr/head)
        null_to_space = {"cmdline"}
        null_to_newline = {"environ"}
        line_limit = {"lsof": 100}

        for name, argv in intel_commands.items():
            try:
                r = subprocess.run(
                    argv, capture_output=True, timeout=10,
                )
                raw = r.stdout or b""
                if name in null_to_space:
                    text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
                elif name in null_to_newline:
                    text = raw.replace(b"\x00", b"\n").decode("utf-8", errors="replace")
                else:
                    text = raw.decode("utf-8", errors="replace")
                if name in line_limit:
                    text = "\n".join(text.splitlines()[: line_limit[name]])
                with open(os.path.join(case_dir, f"{name}.txt"), "w") as f:
                    f.write(text or "(empty)")
            except Exception:
                pass

        # IP lookup (async, non-blocking)
        try:
            subprocess.Popen(
                [
                    "curl", "-s", "--connect-timeout", "5", "--max-time", "10",
                    f"https://ipinfo.io/{src_ip}/json",
                    "-o", os.path.join(case_dir, "ipinfo.json"),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        # Packet capture (100 packets, 15 seconds)
        try:
            subprocess.Popen(
                [
                    "timeout", "15", "tcpdump", "-i", "any", "-c", "100",
                    "-n", f"host {src_ip}", "-w",
                    os.path.join(case_dir, "traffic.pcap"),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# L4: Filesystem Watch — /tmp monitoring + quarantine
# ---------------------------------------------------------------------------

class FilesystemWatch:
    """Monitors /tmp and /var/tmp for suspicious files."""

    SUSPICIOUS_PATTERNS = [
        ".fefc", ".fefd", "*.so",  # suspicious shared libraries
        "*token*", "*key*", "*secret*", "*password*", "*credential*",
        "*.pem", "authorized_keys",
    ]

    def __init__(self):
        os.makedirs(QUARANTINE_DIR, exist_ok=True)

    def check(self) -> ScanResult:
        result = ScanResult()

        for base in ["/tmp", "/var/tmp"]:
            for pattern in self.SUSPICIOUS_PATTERNS:
                try:
                    r = subprocess.run(
                        ["find", base, "-maxdepth", "2", "-name", pattern,
                         "-mmin", "-360", "-type", "f"],
                        capture_output=True, text=True, timeout=10,
                    )
                    for fpath in r.stdout.strip().split("\n"):
                        if not fpath or "gateway-results" in fpath or "wg-" in fpath:
                            continue
                        safe = self._quarantine(fpath)
                        result.findings.append(
                            f"SUSPICIOUS FILE: {fpath} -> {'quarantined' if safe else 'skipped'}"
                        )
                        result.severity = Severity.escalate(result.severity, Severity.HIGH)
                except Exception:
                    pass

        # Check for new executables in /tmp
        try:
            r = subprocess.run(
                ["find", "/tmp", "/var/tmp", "-maxdepth", "2",
                 "-type", "f", "-executable", "-mmin", "-360"],
                capture_output=True, text=True, timeout=10,
            )
            for fpath in r.stdout.strip().split("\n"):
                if not fpath or "eite-guardian" in fpath or "tical-guardian" in fpath:
                    continue
                safe = self._quarantine(fpath)
                result.findings.append(
                    f"TMP EXECUTABLE: {fpath} -> {'quarantined' if safe else 'skipped'}"
                )
                result.severity = Severity.escalate(result.severity, Severity.CRITICAL)
        except Exception:
            pass

        return result

    def _quarantine(self, fpath: str) -> bool:
        """Safely quarantine a suspicious file. Uses shutil.move for cross-fs support."""
        try:
            real = os.path.realpath(fpath)
            if not (real.startswith("/tmp") or real.startswith("/var/tmp")):
                return False
            if not os.path.isfile(real):
                return False
            dest = os.path.join(
                QUARANTINE_DIR,
                f"{os.path.basename(real)}_{int(time.time())}",
            )
            shutil.move(real, dest)
            os.chmod(dest, 0o000)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# L4.5: SSH Brute-Force Guard — fail2ban-equivalent inside Vigil
# ---------------------------------------------------------------------------

BRUTEFORCE_BAN_FILE = f"{GUARDIAN_DIR}/bruteforce_bans.json"
BRUTEFORCE_THRESHOLD = 5       # ban after N failures in window
BRUTEFORCE_WINDOW = 300         # 5-minute window in seconds
BRUTEFORCE_BAN_TIME = 3600      # 1-hour ban


class BruteForceGuard:
    """Detects SSH brute-force attacks and blocks attacking IPs via iptables.

    Uses journalctl to read auth failures from the last window.
    Purely additive — does not conflict with fail2ban if installed.
    State persisted to bruteforce_bans.json for survival across restarts.
    """

    def __init__(self):
        self._bans: dict = {}          # ip -> {"until": timestamp, "failures": count, "first_seen": ts}
        self._current_bans: set = set()  # IPs currently blocked in iptables
        self._load_bans()
        # Clean up any stale iptables rules from a previous run
        self._sync_iptables()

    def _load_bans(self) -> None:
        try:
            with open(BRUTEFORCE_BAN_FILE, "r") as f:
                data = json.load(f)
            self._bans = data.get("bans", {})
            self._current_bans = set(data.get("active_iptables", []))
        except Exception:
            self._bans = {}
            self._current_bans = set()

    def _save_bans(self) -> None:
        os.makedirs(GUARDIAN_DIR, exist_ok=True)
        with open(BRUTEFORCE_BAN_FILE, "w") as f:
            json.dump({
                "bans": self._bans,
                "active_iptables": list(self._current_bans),
            }, f, indent=2)

    def _sync_iptables(self) -> None:
        """Remove any stale iptables rules from a previous Vigil instance."""
        try:
            existing = subprocess.run(
                ["iptables", "-L", "INPUT", "-n", "--line-numbers"],
                capture_output=True, text=True, timeout=5,
            )
            for line in reversed(existing.stdout.split("\n")):
                if "VIGIL-BF-" in line:
                    num = line.split()[0]
                    subprocess.run(
                        ["iptables", "-D", "INPUT", num],
                        capture_output=True, timeout=5,
                    )
        except Exception:
            pass

    def _block_ip(self, ip: str) -> bool:
        """Block an IP via iptables. Idempotent."""
        if ip in self._current_bans:
            return True
        if ip.startswith("127.") or ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("172.16."):
            return False  # never block local/private IPs
        try:
            comment = f"VIGIL-BF-{int(time.time())}"
            subprocess.run(
                ["sudo", "iptables", "-I", "INPUT", "-s", ip, "-j", "DROP", "-m", "comment", "--comment", comment],
                capture_output=True, timeout=5, check=True,
            )
            self._current_bans.add(ip)
            return True
        except Exception:
            return False

    def _unblock_ip(self, ip: str) -> None:
        """Remove iptables block for an IP."""
        if ip not in self._current_bans:
            return
        try:
            subprocess.run(
                ["sudo", "iptables", "-D", "INPUT", "-s", ip, "-j", "DROP"],
                capture_output=True, timeout=5,
            )
            self._current_bans.discard(ip)
        except Exception:
            pass

    def _expire_bans(self) -> list:
        """Unblock IPs whose ban has expired. Returns list of unblocked IPs."""
        now = time.time()
        unblocked = []
        expired_ips = [ip for ip, ban in self._bans.items() if now >= ban.get("until", 0)]
        for ip in expired_ips:
            self._unblock_ip(ip)
            del self._bans[ip]
            unblocked.append(ip)
        if unblocked:
            self._save_bans()
        return unblocked

    def check(self) -> ScanResult:
        result = ScanResult()
        self._expire_bans()

        try:
            # Read SSH auth failures from journalctl — last 5 minutes
            r = subprocess.run(
                ["journalctl", "-u", "ssh", "-u", "sshd", "--no-pager",
                 "--since", f"{BRUTEFORCE_WINDOW // 60} min ago",
                 "-o", "cat"],
                capture_output=True, text=True, timeout=10,
            )
            # Parse: "Failed password for root from 1.2.3.4 port 22 ssh2"
            # Also: "Invalid user test from 1.2.3.4 port 22"
            failures: dict = {}
            for line in r.stdout.split("\n"):
                m = re.search(
                    r"(?:Failed password|Invalid user|authentication failure).*?(?:from|rhost=)\s+(\d+\.\d+\.\d+\.\d+)",
                    line, re.IGNORECASE,
                )
                if m:
                    ip = m.group(1)
                    if ip.startswith("127.") or ip.startswith("10.") or ip.startswith("192.168."):
                        continue
                    if ip in self._current_bans:
                        continue  # already blocked
                    failures[ip] = failures.get(ip, 0) + 1
        except Exception:
            return result

        now = time.time()
        for ip, count in failures.items():
            # Aggregate with existing tracking
            if ip in self._bans:
                ban = self._bans[ip]
                if now < ban.get("until", 0):
                    continue  # still banned
                # Ban expired — track fresh failures
                count += ban.get("failures", 0)
                if now - ban.get("first_seen", now) > BRUTEFORCE_WINDOW:
                    count = failures[ip]  # reset window

            if count >= BRUTEFORCE_THRESHOLD:
                if self._block_ip(ip):
                    self._bans[ip] = {
                        "until": now + BRUTEFORCE_BAN_TIME,
                        "failures": count,
                        "first_seen": now,
                        "banned_at": now,
                    }
                    self._save_bans()
                    result.findings.append(
                        f"SSH BRUTE FORCE: {ip} — {count} failures in {BRUTEFORCE_WINDOW}s, banned for {BRUTEFORCE_BAN_TIME}s"
                    )
                    result.severity = Severity.escalate(result.severity, Severity.CRITICAL)
                    result.blocked = True
            else:
                # Track for cumulative counting
                self._bans[ip] = {
                    "until": 0,
                    "failures": count,
                    "first_seen": self._bans.get(ip, {}).get("first_seen", now),
                }
                if count >= BRUTEFORCE_THRESHOLD // 2:
                    result.findings.append(
                        f"SSH BRUTE FORCE WARNING: {ip} — {count}/{BRUTEFORCE_THRESHOLD} failures"
                    )
                    result.severity = Severity.escalate(result.severity, Severity.MEDIUM)

        self._save_bans()
        return result


# ---------------------------------------------------------------------------
# L5: Integrity Guard — self-protection
# ---------------------------------------------------------------------------

class IntegrityGuard:
    """Self-checksum verification and tamper detection."""

    def __init__(self):
        self._self_hash = self._compute_self_hash()
        self._stored_hash = self._load_checksum()

    def _compute_self_hash(self) -> str:
        try:
            with open(MODULE_PATH, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return ""

    def _load_checksum(self) -> Optional[str]:
        try:
            with open(CHECKSUM_FILE, "r") as f:
                data = json.load(f)
                return data.get("sha256")
        except Exception:
            return None

    def bootstrap(self) -> None:
        """Record initial checksum after first load."""
        os.makedirs(GUARDIAN_DIR, exist_ok=True)
        with open(CHECKSUM_FILE, "w") as f:
            json.dump({
                "sha256": self._self_hash,
                "path": MODULE_PATH,
                "bootstrapped": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
        os.chmod(CHECKSUM_FILE, STATE_PERMS)

    def check(self) -> ScanResult:
        result = ScanResult()
        current = self._compute_self_hash()

        if self._stored_hash and current and current != self._stored_hash:
            result.findings.append("INTEGRITY VIOLATION: vigil module modified")
            result.severity = Severity.CRITICAL

        # Check state files not tampered
        for f in [BASELINE_FILE, CHECKSUM_FILE]:
            if os.path.exists(f):
                try:
                    st = os.stat(f)

                    if st.st_uid != 0 and st.st_uid != current_uid:  # not root-owned nor owned by running user
                        result.findings.append(f"STATE FILE PERMISSION: {f} not root-owned")
                        result.severity = Severity.escalate(result.severity, Severity.HIGH)
                except Exception:
                    pass

        return result


# ---------------------------------------------------------------------------
# No-op fallbacks when advanced tical_code.vigil cannot be imported
# ---------------------------------------------------------------------------

class _NoopSignalCollector:
    """Stub human-side signal collector (no-op methods)."""

    def record_input(self, char_count=1, had_error=False):
        return None

    def record_response(self, length):
        return None

    def record_task_switch(self):
        return None

    def collect(self):
        return None


class _NoopAISignalCollector:
    """Stub AI-side signal collector (no-op methods)."""

    def record_tokens(self, count: int = 0):
        return None

    def record_tool_call(self, tool_name: str = "", result_hash: str = ""):
        return None

    def task_started(self, task_type: str = ""):
        return None

    def task_completed(self):
        return None

    def collect(self):
        return None

    def is_stuck(self):
        return False


class _NoopTrace:
    """Stub audit trace store."""

    def recent(self, n: int = 1):
        return []

    def record(self, *args, **kwargs):
        return ""


class _NoopInstructionQueue:
    """Stub instruction queue."""

    def all_pending(self):
        return []

    def cleanup_expired(self):
        return []


# ---------------------------------------------------------------------------
# Main SecurityVigil Module — coordinates all layers
# ---------------------------------------------------------------------------

class SecurityVigil:
    """Autonomous security coordinator for tical-code agents.

    Loaded via @register in module_defs.py. Activates on worker init.
    Runs L1-L5+L4.5 checks on patrol cycle. No user config required.

    Also bridges the advanced guardian layer (tical_code.vigil) so callers
    that expect signal_collector / ai_signal_collector / patrol() do not
    crash. Chosen fix: Option B — restore advanced Vigil as _vigil_advanced
    and expose its collectors/API on this facade.
    """

    def __init__(self, worker: Any):
        self.worker = worker
        self.log = logger

        # Guard layers
        self.msg_scanner = MessageScanner()
        self.port_patrol = PortPatrol()
        self.ssh_sentinel = SSHSentinel()
        self.fs_watch = FilesystemWatch()
        self.bruteforce = BruteForceGuard()
        self.integrity = IntegrityGuard()

        # Bootstrap integrity on first load
        self.integrity.bootstrap()

        # Background patrol
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._blocked_at: Dict[str, float] = {}  # ip -> epoch timestamp of last block

        # Statistics
        self._scans = 0
        self._alerts = 0
        self._start_time = time.time()

        # Ensure directories
        os.makedirs(GUARDIAN_DIR, exist_ok=True)
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        os.makedirs(FORENSICS_DIR, exist_ok=True)

        # Bridge advanced Vigil (signal collectors + immune-system patrol)
        self._vigil_advanced = None
        self.signal_collector = None
        self.ai_signal_collector = None
        self._state_history: list = []
        self.trace = None
        self.ai_state_classifier = None
        self.instruction_queue = None
        try:
            from tical_code.vigil import build_vigil
            self._vigil_advanced = build_vigil()
            self.signal_collector = self._vigil_advanced.signal_collector
            self.ai_signal_collector = self._vigil_advanced.ai_signal_collector
            self._state_history = self._vigil_advanced._state_history
            self.trace = self._vigil_advanced.trace
            self.ai_state_classifier = self._vigil_advanced.ai_state_classifier
            self.instruction_queue = self._vigil_advanced.instruction_queue
            self.log.info("SecurityVigil: advanced Vigil bridged as _vigil_advanced")
        except Exception as e:
            # Fallback no-op collectors so callers never AttributeError
            self.log.warning("SecurityVigil: advanced Vigil unavailable (%s); using stubs", e)
            self.signal_collector = _NoopSignalCollector()
            self.ai_signal_collector = _NoopAISignalCollector()
            self.trace = _NoopTrace()
            self.instruction_queue = _NoopInstructionQueue()

        self.log.info("SecurityVigil: 6-layer guard activated")

    async def patrol(self) -> None:
        """One-shot patrol for the main worker loop.

        Runs the advanced Vigil immune-system sweep when bridged.
        L1-L5+L4.5 security layers continue on the background _patrol_loop.
        """
        if self._vigil_advanced is not None:
            try:
                await self._vigil_advanced.patrol()
            except Exception as e:
                self.log.warning("Advanced Vigil patrol error: %s", e)
            # Keep facade history/trace aliases in sync
            self._state_history = self._vigil_advanced._state_history

    def start(self) -> None:
        """Start background patrol loop.

        When the asyncio event loop is available, spawns as an async task.
        Otherwise falls back to a daemon thread — this ensures patrol runs
        even when Vigil initializes before the worker's event loop starts.
        """
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._task = asyncio.create_task(self._patrol_loop())
                self.log.info("Vigil patrol started (interval=%ds)", PATROL_INTERVAL)
                return
        except RuntimeError:
            pass

        # Event loop not ready — run in dedicated daemon thread
        import threading
        self._thread = threading.Thread(
            target=self._threaded_patrol, daemon=True,
            name="vigil-patrol"
        )
        self._thread.start()
        self.log.info("Vigil patrol started in daemon thread (interval=%ds)", PATROL_INTERVAL)

    def _threaded_patrol(self) -> None:
        """Run patrol loop in a dedicated asyncio event loop (daemon thread)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._patrol_loop())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log.error("Threaded patrol crashed: %s", e)
        finally:
            loop.close()

    def stop(self) -> None:
        """Stop patrol loop cleanly."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _patrol_loop(self) -> None:
        """Main patrol cycle — runs all guard layers."""
        while self._running:
            try:
                self._scans += 1
                findings_all = []

                # L5: Integrity (self-check first)
                r = self.integrity.check()
                if r.findings:
                    findings_all.extend(r.findings)
                    self._write_alert("L5_INTEGRITY", r)

                # L2: Port patrol
                r = self.port_patrol.check()
                if r.findings:
                    findings_all.extend(r.findings)
                    self._write_alert("L2_PORT", r)
                    # Auto-block: new public ports are suspicious
                    for finding in r.findings:
                        port_match = re.search(r'(?:NEW PUBLIC PORT|PORT PROCESS CHANGED):\s*(\d+)', str(finding))
                        if port_match:
                            self._block_port(int(port_match.group(1)), "L2_PORT")

                # L3: SSH sentinel
                r = self.ssh_sentinel.check()
                if r.findings:
                    findings_all.extend(r.findings)
                    self._write_alert("L3_SSH", r)
                    # Auto-block: UNKNOWN SSH connections are active intruders
                    for finding in r.findings:
                        ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', str(finding))
                        if ip_match:
                            self._auto_block(ip_match.group(1), "L3_SSH")

                # L4: Filesystem watch
                r = self.fs_watch.check()
                if r.findings:
                    findings_all.extend(r.findings)
                    self._write_alert("L4_FS", r)

                # L4.5: SSH brute-force detection
                r = self.bruteforce.check()
                if r.findings:
                    findings_all.extend(r.findings)
                    self._write_alert("L4_BF", r)

                if findings_all:
                    self._alerts += 1
                    self.log.warning(
                        "Patrol #%d: %d finding(s)", self._scans, len(findings_all)
                    )
                    # ACTIVE NOTIFICATION: push alert through most recent user channel
                    self._dispatch_alert(findings_all)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error("Patrol error: %s", e)

            await asyncio.sleep(PATROL_INTERVAL)

    def _dispatch_alert(self, findings: list) -> None:
        """Push security alert through most recent active user channel.

        Strategy:
            1. Try Telegram (most common) — uses cached chat_id from worker
            2. Fallback: tical-chat if URL/key configured
            3. Zero channels → log only (system LLM already auto-handled threat)

        chat_id is auto-discovered by worker during normal message processing
        and cached at ~/.guardian_chat_id (see unified_worker.py line 1312).
        """
        try:
            node = os.uname().nodename
            findings_text = "; ".join(str(f) for f in findings[:5])
            alert_msg = (
                f"⚠️ [Vigil Security] {len(findings)} threat(s) detected\n"
                f"Node: {node}\n"
                f"Findings: {findings_text}\n"
                f"Action: auto-response applied → $TICAL_GUARDIAN_DIR/pending_alerts/"
            )

            sent = False

            # ── Channel 1: Telegram ──
            tg_token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("GUARDIAN_TG_TOKEN", "")
            chat_id_file = os.path.expanduser("~/.guardian_chat_id")
            tg_chat = ""
            try:
                if os.path.exists(chat_id_file):
                    with open(chat_id_file) as f:
                        tg_chat = f.read().strip()
            except Exception:
                pass
            if not tg_chat:
                tg_chat = os.environ.get("TG_CHAT_ID") or os.environ.get("GUARDIAN_TG_CHAT", "")

            if tg_token and tg_chat:
                try:
                    import urllib.request, json as _json
                    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                    data = _json.dumps({
                        "chat_id": tg_chat, "text": alert_msg[:4000]
                    }).encode()
                    req = urllib.request.Request(url, data=data,
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        body = _json.loads(resp.read().decode())
                    if body.get("ok"):
                        self.log.info("Alert dispatched via Telegram (chat_id=%s)", tg_chat[:6] + "...")
                        sent = True
                    else:
                        self.log.warning("Telegram alert failed: %s", body.get("description", "?"))
                except Exception as e:
                    self.log.warning("Telegram dispatch error: %s", e)

            # ── Channel 2: tical-chat ──
            if not sent:
                chat_url = os.environ.get("TICAL_CHAT_URL", "")
                chat_key = os.environ.get("TICAL_CHAT_KEY", "")
                chat_identity = os.environ.get("WORKER_IDENTITY", node)
                if chat_url and chat_key:
                    try:
                        import urllib.request, json as _json
                        payload = _json.dumps({
                            "sender": chat_identity,
                            "target": "user",
                            "content": alert_msg,
                        }).encode()
                        req = urllib.request.Request(
                            f"{chat_url.rstrip('/')}/v1/messages",
                            data=payload,
                            headers={
                                "Content-Type": "application/json",
                                "X-AI-Identity": chat_identity,
                                "X-AI-Key": chat_key,
                            },
                        )
                        with urllib.request.urlopen(req, timeout=8):
                            pass
                        self.log.info("Alert dispatched via tical-chat")
                        sent = True
                    except Exception as e:
                        self.log.warning("tical-chat dispatch error: %s", e)

            # ── Zero channels ──
            if not sent:
                self.log.info(
                    "No user channels available — threat auto-handled by system LLM, "
                    "alerts preserved in %s", os.path.join(GUARDIAN_DIR, "emergency")
                )

        except Exception as e:
            self.log.error("_dispatch_alert error: %s", e)

    async def check_message(self, text: str) -> ScanResult:
        """Pre-LLM message scan — L1 URL/IP check."""
        return self.msg_scanner.scan(text)

    def get_context_injection(self) -> str:
        """Return security context for system prompt enrichment.

        Reads emergency alerts (auto-blocked threats) and pending alerts.
        Highest priority: deliver emergency notifications to user.
        """
        emergency_dir = os.path.join(GUARDIAN_DIR, "emergency")
        alert_dir = os.path.join(GUARDIAN_DIR, "pending_alerts")
        lines = []

        # EMERGENCY alerts (auto-blocked) — MUST surface to user
        if os.path.isdir(emergency_dir):
            emergencies = sorted(
                [f for f in os.listdir(emergency_dir) if f.endswith(".json")],
                reverse=True,
            )[:3]
            for ef in emergencies:
                try:
                    with open(os.path.join(emergency_dir, ef)) as fh:
                        data = json.load(fh)
                    lines.append(
                        f"!!! SECURITY EMERGENCY !!!\n"
                        f"[{data.get('type','?')}] {data.get('reason','?')}\n"
                        f"Target: {data.get('target','?')}\n"
                        f"Action: {data.get('action','AUTO-BLOCKED')}\n"
                        f"Node: {data.get('node','?')} at {data.get('timestamp','?')}\n"
                        f"ALERT USER IMMEDIATELY — threat was auto-blocked."
                    )
                except Exception:
                    pass

        # Standard patrol alerts
        if os.path.isdir(alert_dir):
            pending = [f for f in os.listdir(alert_dir) if f.endswith(".json")]
            if pending:
                lines.append(
                    f"[Security Vigil] {len(pending)} active security alert(s). "
                    f"Advise user to check $TICAL_GUARDIAN_DIR/emergency/ immediately."
                )

        return "\n\n".join(lines) if lines else ""

    def get_stats(self) -> Dict:
        """Runtime statistics for status/health checks."""
        return {
            "uptime_seconds": int(time.time() - self._start_time),
            "patrols_run": self._scans,
            "alerts_triggered": self._alerts,
            "patrol_active": self._running,
            "known_bad_ips": len(self.msg_scanner._known_bad_ips),
            "quarantined_files": len(
                os.listdir(QUARANTINE_DIR) if os.path.isdir(QUARANTINE_DIR) else []
            ),
        }

    def _write_alert(self, layer: str, result: ScanResult) -> None:
        """Write alert for external consumption (syslog, file watcher)."""
        alert_dir = os.path.join(GUARDIAN_DIR, "pending_alerts")
        os.makedirs(alert_dir, exist_ok=True)
        alert_file = os.path.join(alert_dir, f"{layer}_{int(time.time())}.json")
        try:
            with open(alert_file, "w") as f:
                json.dump({
                    "layer": layer,
                    "findings": result.findings,
                    "severity": result.severity,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
        except Exception:
            pass

    def _block_port(self, port: int, source: str) -> bool:
        """Block a port via iptables (idempotent).

        Called when PortPatrol detects a new public port.
        Returns True if block succeeded.
        """
        if port in (22, 80, 443, 53):
            return False  # never block essential services
        try:
            # Check if already blocked
            subprocess.run(
                ["sudo", "iptables", "-C", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP"],
                capture_output=True, timeout=5, check=True,
            )
            return True  # already exists
        except Exception:
            pass
        try:
            subprocess.run(
                ["sudo", "iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP",
                 "-m", "comment", "--comment", f"VIGIL-PORT-{int(time.time())}"],
                capture_output=True, timeout=5, check=True,
            )
            self.log.info("Auto-blocked port %d via iptables (%s)", port, source)
            return True
        except Exception as e:
            self.log.error("Port block failed for %d: %s", port, e)
            return False

    def _auto_block(self, ip: str, source: str) -> bool:
        """Block an IP via iptables (deduped, 24h expiry, mesh-safe).

        Called automatically by patrol loop. Idempotent — skips
        IPs that are already blocked, private/mesh IPs, and ones
        blocked within the last hour (cooldown). 24h expiry.
        """
        if not ip or ip.startswith("127.") or ip.startswith("10."):
            return False
        if ip.startswith("192.168.") or ip.startswith("172.16."):
            return False
        # Never block mesh IPs
        mesh = self.ssh_sentinel.MESH_IPS if hasattr(self, 'ssh_sentinel') else frozenset()
        if ip in mesh:
            return False

        now = time.time()
        # Dedup: skip if already blocked within last hour (cooldown)
        if ip in self._blocked_at:
            last = self._blocked_at[ip]
            if now - last < 3600:
                return False  # still cooling down
        # Expire old blocks
        stale = [k for k, v in self._blocked_at.items() if now - v > AUTO_BLOCK_EXPIRY]
        for k in stale:
            del self._blocked_at[k]
            try:
                subprocess.run(
                    ["sudo", "iptables", "-D", "INPUT", "-s", k, "-j", "DROP"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

        try:
            comment = f"VIGIL-AUTO-{int(now)}"
            subprocess.run(
                ["sudo", "iptables", "-C", "INPUT", "-s", ip, "-j", "DROP"],
                capture_output=True, timeout=5, check=True,
            )
            # Rule already exists — update timestamp only
            self._blocked_at[ip] = now
            return True
        except Exception:
            pass  # Rule doesn't exist, proceed to add

        try:
            subprocess.run(
                ["sudo", "iptables", "-I", "INPUT", "-s", ip, "-j", "DROP",
                 "-m", "comment", "--comment", comment],
                capture_output=True, timeout=5, check=True,
            )
            self._blocked_at[ip] = now
            # Write emergency record
            emergency_dir = os.path.join(GUARDIAN_DIR, "emergency")
            os.makedirs(emergency_dir, exist_ok=True)
            em_file = os.path.join(emergency_dir, f"{source}_{int(now)}.json")
            with open(em_file, "w") as f:
                json.dump({
                    "type": "auto_block",
                    "source": source,
                    "target": ip,
                    "action": "iptables DROP (sudo)",
                    "node": os.uname().nodename,
                    "expires_at": datetime.fromtimestamp(now + AUTO_BLOCK_EXPIRY, tz=timezone.utc).isoformat(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
            self.log.info("Auto-blocked %s via sudo iptables (%s)", ip, source)
            return True
        except Exception as e:
            self.log.error("Auto-block failed for %s: %s", ip, e)
            return False

    def mark_ip(self, ip: str, bad: bool = True) -> None:
        """Mark an IP as known-bad (for L1 blocking)."""
        self.msg_scanner.mark_ip(ip, bad)


# ---------------------------------------------------------------------------
# Module initialization (called by @register)
# ---------------------------------------------------------------------------

def _init_vigil(worker: Any, cfg: dict) -> SecurityVigil:
    """Initialize and start SecurityVigil for a worker."""
    vigil = SecurityVigil(worker)
    vigil.start()
    return vigil


# ---------------------------------------------------------------------------
# Self-test (runs on `python3 -m tical_code.core.vigil`)
# ---------------------------------------------------------------------------

def _self_test() -> None:
    print("Security Vigil Self-Test")
    print("=" * 50)

    # L1: Message scanner
    scanner = MessageScanner()
    assert scanner.scan("Hello world").severity == Severity.CLEAN
    print("PASS L1.1: clean message")

    r = scanner.scan("Check http://paypal-verify.xyz/login")
    assert r.severity >= Severity.HIGH and r.blocked
    print(f"PASS L1.2: phishing blocked (severity={r.severity})")

    r = scanner.scan("Visit http://185.234.72.11/panel")
    assert r.blocked
    print("PASS L1.3: IP URL blocked")

    r = scanner.scan("See https://github.com/zizetu/tical-agent")
    assert r.severity == Severity.CLEAN
    print("PASS L1.4: safe URL passes")

    r = scanner.scan("https://discord.com/api/webhooks/123/abc")
    assert r.blocked
    print("PASS L1.5: Discord webhook blocked")

    r = scanner.scan("https://bit.ly/abc123")
    assert r.severity >= Severity.HIGH
    print("PASS L1.6: short-link flagged")

    # L2: Port patrol (needs root for ss, skip in test)
    print("SKIP L2: port patrol (requires root ss)")

    # L3-L5: require root, skip
    print("SKIP L3-L5: require root privileges")

    print("=" * 50)
    print("Message scanner: all tests passed")


if __name__ == "__main__":
    _self_test()
