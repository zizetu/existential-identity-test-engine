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

"""
SELF-CHECK ENGINE - Programmatic equivalent of self_check.sh.
Runs security, code quality, and integrity checks on the EITElite repository.

No shell script dependency. Uses only Python stdlib.
"""

from __future__ import annotations

import os
import py_compile
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple


# ── Resolve repository root ───────────────────────────────────────────────
def _repo_root() -> Path:
    """Walk up from this file to find the repository root (contains VERSION)."""
    candidate = Path(__file__).resolve().parent
    for _ in range(6):
        if (candidate / "VERSION").exists() and (candidate / ".gitignore").exists():
            return candidate
        candidate = candidate.parent
    # Fallback: three levels up from guardian/
    return Path(__file__).resolve().parent.parent.parent


_REPO = _repo_root()
_CORE_DIR = _REPO / "tical_code" / "core"
_DOCS_DIR = _REPO / "docs"
_CHANNEL_PY = _CORE_DIR / "channel.py"
_GITIGNORE = _REPO / ".gitignore"
_VERSION_FILE = _REPO / "VERSION"
_CLAUDE_MD = _REPO / "CLAUDE.md"


# ── Dataclass ─────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    """Result of a single self-check."""
    name: str
    ok: bool
    detail: str
    severity: str  # P0 (critical), P1 (high), P2 (medium)
    file_path: str = ""
    metadata: dict = field(default_factory=dict)


# ── Core helpers ──────────────────────────────────────────────────────────
def _core_py_files() -> List[Path]:
    """Return all .py files under tical_code/core/, excluding __pycache__."""
    files: List[Path] = []
    for py_file in _CORE_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        files.append(py_file)
    return files


def _read_file(path: Path) -> str:
    """Read file contents, return empty string on error."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# CHECK FUNCTIONS - each returns (ok: bool, detail: str)
# ═══════════════════════════════════════════════════════════════════════════


def check_shell_true() -> Tuple[bool, str]:
    """Scan for shell=True usage in core/*.py (excludes comment/doc string patterns)."""
    hits: List[str] = []
    for py_file in _core_py_files():
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            if not re.search(r'shell\s*=\s*True', line):
                continue
            # Skip lines that are clearly documentation / comments referencing the rule
            if '# noqa' in line:
                continue
            if 'test_' in py_file.name:
                continue
            if 'never uses shell=True' in line:
                continue
            if 'never shell=True' in line:
                continue
            # Skip if line is purely a comment about the rule
            stripped = line.strip()
            if stripped.startswith('#') and 'shell' in stripped:
                continue
            hits.append(f"{py_file.relative_to(_REPO)}:{lineno}: {stripped[:80]}")
    if hits:
        return False, f"{len(hits)} shell=True usage(s): " + "; ".join(hits[:5])
    return True, "0 shell=True usages"


def check_bare_except() -> Tuple[bool, str]:
    """Scan for bare 'except:' (not except Exception / except (…) / except BaseException)."""
    hits: List[str] = []
    for py_file in _core_py_files():
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            if not re.match(r'\s*except\s*:', line):
                continue
            if 'except Exception' in line:
                continue
            if 'except (' in line:
                continue
            if 'except BaseException' in line:
                continue
            if '# noqa' in line:
                continue
            hits.append(f"{py_file.relative_to(_REPO)}:{lineno}: {line.strip()[:80]}")
    if hits:
        return False, f"{len(hits)} bare except(s): " + "; ".join(hits[:5])
    return True, "0 bare excepts"


def check_cjk() -> Tuple[bool, str]:
    """Scan for CJK (Chinese/Japanese/Korean) characters in core/*.py."""
    cjk_pattern = re.compile(r'[\u4e00-\u9fff]')
    hits: List[str] = []
    for py_file in _core_py_files():
        try:
            text = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = list(cjk_pattern.finditer(text))
        if matches:
            # Report file + count
            hits.append(f"{py_file.relative_to(_REPO)}: {len(matches)} CJK chars")
    if hits:
        return False, f"{len(hits)} file(s) with CJK: " + "; ".join(hits[:5])
    return True, "0 CJK characters"


def check_compile() -> Tuple[bool, str]:
    """py_compile all core/*.py files. Returns list of failures."""
    failures: List[str] = []
    for py_file in sorted(_core_py_files()):
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{py_file.relative_to(_REPO)}: {exc}")
        except Exception as exc:
            failures.append(f"{py_file.relative_to(_REPO)}: {exc}")
    if failures:
        return False, f"{len(failures)} compile error(s): " + "; ".join(failures[:5])
    return True, f"all {len(_core_py_files())} files compile cleanly"


def check_gitignore() -> Tuple[bool, str]:
    """Verify .gitignore contains all required security patterns."""
    required = ['credentials*', 'secrets*', '*.pem', '*.key', 'config.local.*']
    missing: List[str] = []
    try:
        gitignore_content = _GITIGNORE.read_text(encoding="utf-8")
    except Exception:
        return False, ".gitignore file not found or unreadable"

    for pattern in required:
        if pattern not in gitignore_content:
            missing.append(pattern)
    if missing:
        return False, f"missing patterns: {', '.join(missing)}"
    return True, f"all {len(required)} required patterns present"


def check_version_coherence() -> Tuple[bool, str]:
    """Verify VERSION file matches version string in CLAUDE.md."""
    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return False, "VERSION file missing or unreadable"

    try:
        claude_content = _CLAUDE_MD.read_text(encoding="utf-8")
    except Exception:
        return False, "CLAUDE.md file missing or unreadable"

    if version not in claude_content:
        return False, f"CLAUDE.md version mismatch (expected VERSION={version})"
    return True, f"CLAUDE.md matches VERSION ({version})"


def check_ssrf_localhost() -> Tuple[bool, str]:
    """Verify _ssrf_guard in channel.py has localhost exemption."""
    try:
        content = _CHANNEL_PY.read_text(encoding="utf-8")
    except Exception:
        return False, "channel.py not found or unreadable"

    # The localhost exemption should contain 'localhost', '127.0.0.1', or '::1'
    # within the _ssrf_guard function body.
    # Find the function definition and check the next ~10 lines.
    func_match = re.search(r'def _ssrf_guard\([^)]*\):', content)
    if not func_match:
        return False, "_ssrf_guard function not found in channel.py"

    # Extract the function body (roughly the next 15 lines)
    body_start = func_match.end()
    body_end = min(body_start + 800, len(content))
    body = content[body_start:body_end]

    # Look for a return/exit before the SSRF check for localhost/loopback
    has_localhost = bool(re.search(r'localhost', body))
    has_loopback_v4 = bool(re.search(r'127\.0\.0\.1', body))
    has_loopback_v6 = bool(re.search(r'::1', body))

    if has_localhost or has_loopback_v4 or has_loopback_v6:
        parts = []
        if has_localhost:
            parts.append('localhost')
        if has_loopback_v4:
            parts.append('127.0.0.1')
        if has_loopback_v6:
            parts.append('::1')
        return True, f"localhost exemption present ({', '.join(parts)})"
    return False, "No localhost exemption found in _ssrf_guard"


def check_secrets() -> Tuple[bool, str]:
    """Scan for hardcoded API keys (sk-, xai-, ghp_ prefixes) in code and docs."""
    # Patterns that match common API key formats
    patterns = [
        r'sk-[a-zA-Z0-9]{20,}',       # OpenAI / Anthropic style
        r'xai-[a-zA-Z0-9]{20,}',      # xAI style
        r'ghp_[a-zA-Z0-9]{20,}',      # GitHub personal access token (classic)
    ]
    combined = re.compile('|'.join(patterns))

    hits: List[str] = []
    scan_dirs = [_CORE_DIR, _DOCS_DIR] if _DOCS_DIR.exists() else [_CORE_DIR]

    for scan_dir in scan_dirs:
        for py_file in scan_dir.rglob("*"):
            if not py_file.is_file():
                continue
            if '.git/' in str(py_file):
                continue
            if '__pycache__' in py_file.parts:
                continue
            if '/tests/' in str(py_file):
                continue
            if 'test_' in py_file.name:
                continue
            # Only scan .py and .md files
            if py_file.suffix not in ('.py', '.md'):
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in combined.finditer(text):
                matched = m.group()
                # Skip redacted/placeholder patterns
                if '****' in matched or 'REDACTED' in matched or 'placeholder' in matched.lower():
                    continue
                if 'your-' in matched.lower() or 'example' in matched.lower():
                    continue
                hits.append(f"{py_file.relative_to(_REPO)}: {matched[:40]}")

    if hits:
        return False, f"{len(hits)} hardcoded API key pattern(s): " + "; ".join(hits[:5])
    return True, "0 hardcoded API key patterns"


def check_ip_exposure() -> Tuple[bool, str]:
    """Scan docs/ for real IP addresses that are not [REDACTED]."""
    if not _DOCS_DIR.exists():
        return True, "docs/ directory not found - nothing to scan"

    ip_pattern = re.compile(r'(?:[0-9]{1,3}\.){3}[0-9]{1,3}')
    # RFC-reserved prefixes we skip
    rfc_reserved = re.compile(
        r'^(?:127\.|10\.|192\.168\.|169\.254\.|'
        r'172\.(?:1[6-9]|2[0-9]|3[0-1])\.)'
    )
    # Known Cloudflare CDN IPs we skip
    cloudflare_ips = {
        '172.67.196.250', '104.21.34.42', '172.67.157.152', '104.21.33.18',
    }

    hits: List[str] = []
    for md_file in _DOCS_DIR.rglob("*.md"):
        try:
            lines = md_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            if '[REDACTED]' in line:
                continue
            if 'MESH_' in line and '_IP' in line:
                continue
            for m in ip_pattern.finditer(line):
                ip = m.group()
                if rfc_reserved.match(ip):
                    continue
                if ip in cloudflare_ips:
                    continue
                hits.append(f"{md_file.relative_to(_REPO)}:{lineno}: {ip}")

    if hits:
        return False, f"{len(hits)} real IP(s) in docs: " + "; ".join(hits[:5])
    return True, "no real IPs in docs"


def check_module_files() -> Tuple[bool, str]:
    """Verify all expected .py files exist in core/ (recursive find)."""
    expected = [
        "session_manager.py",
        "context_compactor.py",
        "doom_loop.py",
        "constitution.py",
        "truthful_reporting.py",
        "security_baseline.py",
        "trace_recorder.py",
        "memory_store.py",
        "message_adapter.py",
        "memory_profiler.py",
        "model_failover.py",
        "verification_broadcast.py",
        "cron.py",
        "memory_evolve.py",
    ]

    # Build a set of all basenames under core/
    found_names: set = set()
    for py_file in _core_py_files():
        found_names.add(py_file.name)

    missing: List[str] = []
    for mod_name in expected:
        if mod_name not in found_names:
            missing.append(mod_name)

    if missing:
        return False, f"{len(missing)} module(s) missing: {', '.join(missing)}"
    return True, f"all {len(expected)} expected modules present"


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

# Registry: (function, name, severity)
# ── Runtime health checks ────────────────────────────────────────────────

def _get_worker_service() -> str:
    """Detect the active worker systemd service name from GUARDIAN_SERVICE env var."""
    svc = os.environ.get("GUARDIAN_SERVICE", "")
    if svc:
        return svc
    import subprocess as _sp
    for candidate in ("unified-worker",):
        try:
            r = _sp.run(["systemctl", "is-active", candidate],
                       capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "active":
                return candidate
        except Exception:
            continue
    return ""


def check_worker_alive() -> Tuple[bool, str]:
    """Check if the worker systemd service is active."""
    svc = _get_worker_service()
    if not svc:
        return False, "No active worker service found"
    import subprocess as _sp
    try:
        r = _sp.run(["systemctl", "is-active", svc],
                   capture_output=True, text=True, timeout=5)
        if r.stdout.strip() == "active":
            return True, f"Worker {svc} is active"
        return False, f"Worker {svc} is {r.stdout.strip()}"
    except Exception as e:
        return False, f"Worker check failed: {e}"


def check_memory_pressure() -> Tuple[bool, str]:
    """Check if system memory pressure is too high."""
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 1)
        available = mem.get("MemAvailable", 0)
        pct_used = 100 - (available / total * 100) if total > 0 else 0
        if pct_used > 95:
            return False, f"Memory critical: {pct_used:.0f}% used ({available//1024}MB available)"
        elif pct_used > 85:
            return True, f"Memory high but OK: {pct_used:.0f}% used"
        return True, f"Memory normal: {pct_used:.0f}% used ({available//1024}MB free)"
    except Exception as e:
        return False, f"Memory check failed: {e}"


def check_swap() -> Tuple[bool, str]:
    """Check swap is enabled on low-RAM systems (<2GB total)."""
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
        total_kb = mem.get("MemTotal", 0)
        total_mb = total_kb // 1024

        # Only enforce on low-RAM hosts (< 2 GB)
        if total_mb >= 2048:
            return True, f"Swap check skipped: {total_mb}MB RAM is sufficient"

        # Check if any swap is active
        try:
            import subprocess as _sp
            r = _sp.run(["swapon", "--show", "--noheadings"],
                       capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                return True, f"Swap active ({total_mb}MB RAM)"
        except Exception:
            pass

        return False, f"No swap on low-RAM host ({total_mb}MB total)"
    except Exception as e:
        return False, f"Swap check failed: {e}"


def check_task_queue() -> Tuple[bool, str]:
    """Check for stuck or old task files."""
    tasks_dir = os.path.expanduser("~/.tical/tasks")
    if not os.path.isdir(tasks_dir):
        return True, "No task directory (lightweight mode)"
    import json as _json
    stuck = []
    for tf in sorted(os.listdir(tasks_dir)):
        if not tf.endswith(".json"):
            continue
        try:
            with open(os.path.join(tasks_dir, tf)) as fh:
                d = _json.load(fh)
            status = d.get("status", "?")
            step = d.get("step", 0)
            max_s = d.get("max_steps", 0)
            if status == "failed" or (max_s and step >= max_s):
                stuck.append(f"{tf}: {status} step={step}/{max_s}")
        except Exception:
            continue
    if stuck:
        return False, f"{len(stuck)} stuck/failed task(s): {'; '.join(stuck[:3])}"
    return True, f"0 stuck tasks ({len(os.listdir(tasks_dir))} total in queue)"


_CHECK_REGISTRY: List[Tuple[callable, str, str]] = [
    (check_shell_true,        "shell_true",       "P0"),
    (check_bare_except,       "bare_except",      "P0"),
    (check_secrets,           "secrets",          "P0"),
    (check_ssrf_localhost,    "ssrf_localhost",   "P0"),
    (check_cjk,               "cjk",              "P1"),
    (check_compile,           "compile",          "P1"),
    (check_version_coherence, "version_coherence", "P1"),
    (check_module_files,      "module_files",     "P1"),
    (check_gitignore,         "gitignore",        "P2"),
    (check_ip_exposure,       "ip_exposure",      "P2"),
    # ── Runtime health checks ──
    (check_worker_alive,      "worker_alive",     "P0"),
    (check_memory_pressure,   "memory_pressure",  "P1"),
    (check_swap,              "no_swap",          "P1"),
    (check_task_queue,        "task_queue",       "P1"),
]


def run_all_checks() -> List[CheckResult]:
    """Run all registered checks and return their results."""
    results: List[CheckResult] = []
    for check_fn, name, severity in _CHECK_REGISTRY:
        try:
            ok, detail = check_fn()
        except Exception as exc:
            ok = False
            detail = f"check raised exception: {exc}"
        results.append(CheckResult(
            name=name,
            ok=ok,
            detail=detail,
            severity=severity,
        ))
    return results


# ── CLI entry point (for direct execution) ────────────────────────────────
if __name__ == "__main__":
    import sys

    all_ok = True
    for result in run_all_checks():
        status = "OK" if result.ok else "FAIL"
        print(f"[{result.severity}] {result.name:25s} {status:4s}  {result.detail}")
        if not result.ok:
            all_ok = False

    print()
    if all_ok:
        print("Status: CLEAN")
    else:
        print("Status: NEEDS FIX")
    sys.exit(0 if all_ok else 1)
