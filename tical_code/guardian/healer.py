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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#
"""Self-Heal Engine - Guardian Daemon Auto-Recovery Module.

Maps check failures to automated fix actions with safety: dry-run before
commit, max 3 consecutive restarts, action history in /tmp/guardian_actions.json,
30s service health poll after restart. Author: Tical (Zize Tu)
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import CheckResult from checks.py - single source of truth
from tical_code.guardian.checks import CheckResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures, decision table, history management


@dataclass
class HealResult:
    """Result of a heal action attempt."""
    check_name: str
    action_taken: str
    success: bool
    detail: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class HealAction:
    """check_name -> ordered action chain + retry policy."""
    check_name: str
    action_types: List[str]
    max_retries: int = 1
    cooldown_seconds: int = 300


DECISION_TABLE: Dict[str, HealAction] = {
    # Code quality: cannot auto-fix, human must act
    "shell_true":    HealAction("shell_true",    ["alert"], max_retries=0, cooldown_seconds=0),
    "cjk":           HealAction("cjk",           ["alert"], max_retries=0, cooldown_seconds=0),
    "bare_except":   HealAction("bare_except",   ["alert"], max_retries=0, cooldown_seconds=0),
    "secrets_exposed": HealAction("secrets_exposed", ["alert"], max_retries=0, cooldown_seconds=0),
    # Repo issues: pull latest, verify, restart
    "compile":           HealAction("compile",           ["pull", "restart"], max_retries=2, cooldown_seconds=600),
    "version_mismatch":  HealAction("version_mismatch",  ["pull", "restart"], max_retries=2, cooldown_seconds=600),
    "module_file_missing": HealAction("module_file_missing", ["pull", "restart"], max_retries=2, cooldown_seconds=600),
    # Config patches: edit file, commit
    "gitignore_missing": HealAction("gitignore_missing", ["patch", "commit"], max_retries=1, cooldown_seconds=3600),
    "ip_exposure":       HealAction("ip_exposure",       ["patch", "commit"], max_retries=1, cooldown_seconds=3600),
    # Security patches: edit file, restart (no commit for SSRF - config-only)
    "ssrf_localhost":    HealAction("ssrf_localhost",    ["patch", "restart"], max_retries=1, cooldown_seconds=3600),
    # System resources: auto-create swap on low-RAM hosts
    "no_swap":           HealAction("no_swap",           ["swap"], max_retries=1, cooldown_seconds=86400),
}

# History & safety state

HISTORY_FILE = Path("/tmp/guardian_actions.json")
MAX_CONSECUTIVE_RESTARTS = 3
RESTART_POLL_TIMEOUT = 30


def _load_history() -> Dict[str, Any]:
    try:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning("Could not load action history: %s", e)
    return {"restarts": [], "actions": []}


def _save_history(h: Dict[str, Any]) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except OSError as e:
        logger.error("Failed to save action history: %s", e)


def _record_action(check_name: str, action_type: str, success: bool, detail: str) -> None:
    h = _load_history()
    entry = {
        "check_name": check_name, "action_type": action_type,
        "success": success, "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if action_type == "restart":
        h["restarts"].append(entry)
    h["actions"].append(entry)
    _save_history(h)


def _count_recent_restarts(window_seconds: int = 3600) -> int:
    h = _load_history()
    cutoff = time.time() - window_seconds
    count = 0
    for e in h.get("restarts", []):
        try:
            if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff:
                count += 1
        except (ValueError, KeyError): continue
    return count


def _last_action_ts(action_type: str) -> Optional[float]:
    h = _load_history()
    latest = None
    for e in h.get("actions", []):
        if e.get("action_type") == action_type:
            try:
                ts = datetime.fromisoformat(e["timestamp"]).timestamp()
                if latest is None or ts > latest:
                    latest = ts
            except (ValueError, KeyError): continue
    return latest


# Action functions

def action_pull(repo_path: str) -> HealResult:
    """git pull origin main in repo_path. Returns HealResult."""
    if not (Path(repo_path) / ".git").exists():
        return HealResult("", "pull", False, f"Not a git repo: {repo_path}")
    try:
        r = subprocess.run(["git", "pull", "origin", "main"], cwd=repo_path,
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return HealResult("", "pull", True, r.stdout.strip() or "Already up to date.")
        return HealResult("", "pull", False, r.stderr.strip())
    except subprocess.TimeoutExpired:
        return HealResult("", "pull", False, "git pull timed out after 60s")
    except Exception as e:
        return HealResult("", "pull", False, str(e))


def action_restart(service_name: str, poll_timeout: int = RESTART_POLL_TIMEOUT) -> HealResult:
    """systemctl restart + poll until active for up to poll_timeout seconds."""
    try:
        r = subprocess.run(["sudo", "systemctl", "restart", service_name],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return HealResult("", "restart", False, f"systemctl restart failed: {r.stderr.strip()}")
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            poll = subprocess.run(["systemctl", "is-active", "--quiet", service_name],
                                  capture_output=True, timeout=5)
            if poll.returncode == 0:
                return HealResult("", "restart", True, f"Service {service_name} active after restart.")
            time.sleep(2)
        return HealResult("", "restart", False, f"Service {service_name} not active within {poll_timeout}s")
    except subprocess.TimeoutExpired:
        return HealResult("", "restart", False, "systemctl restart timed out")
    except Exception as e:
        return HealResult("", "restart", False, str(e))


def action_patch(file_path: str, old_content: str, new_content: str) -> HealResult:
    """Targeted find-and-replace in a single file (exact match, must be unique)."""
    p = Path(file_path)
    if not p.exists():
        return HealResult("", "patch", False, f"File not found: {file_path}")
    try:
        content = p.read_text(encoding="utf-8")
        count = content.count(old_content)
        if count == 0:
            return HealResult("", "patch", False, f"Target string not found in {file_path}")
        if count > 1:
            return HealResult("", "patch", False, f"Target appears {count} times; must be unique")
        p.write_text(content.replace(old_content, new_content, 1), encoding="utf-8")
        return HealResult("", "patch", True, f"Patched {file_path}")
    except Exception as e:
        return HealResult("", "patch", False, str(e))


def action_commit(repo_path: str, message: str, dry_run: bool = True) -> HealResult:
    """git add + commit + push with mandatory dry-run safety gate."""
    if not (Path(repo_path) / ".git").exists():
        return HealResult("", "commit", False, f"Not a git repo: {repo_path}")
    try:
        dry = subprocess.run(["git", "add", "-A", "--dry-run"], cwd=repo_path,
                             capture_output=True, text=True, timeout=30)
        if not dry.stdout.strip():
            return HealResult("", "commit", False, "Nothing to commit (dry-run: no changes)")
        if dry_run:
            diff = subprocess.run(["git", "diff", "--cached", "--stat"], cwd=repo_path,
                                  capture_output=True, text=True, timeout=30)
            return HealResult("", "commit", True, f"DRY-RUN only - would commit:\n{diff.stdout.strip()}")
        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, text=True, timeout=30)
        cr = subprocess.run(["git", "commit", "-m", message], cwd=repo_path,
                            capture_output=True, text=True, timeout=30)
        if cr.returncode != 0:
            return HealResult("", "commit", False, f"Commit failed: {cr.stderr.strip()}")
        pr = subprocess.run(["git", "push", "origin", "main"], cwd=repo_path,
                            capture_output=True, text=True, timeout=60)
        if pr.returncode != 0:
            return HealResult("", "commit", False, f"Push failed: {pr.stderr.strip()}")
        return HealResult("", "commit", True, cr.stdout.strip())
    except subprocess.TimeoutExpired:
        return HealResult("", "commit", False, "Commit operation timed out")
    except Exception as e:
        return HealResult("", "commit", False, str(e))


def action_swap(size_mb: int = 512) -> HealResult:
    """Create and enable a swap file if none exists. Idempotent: skips if already active."""
    swapfile = "/swapfile"
    try:
        # Check if swap already active
        r = subprocess.run(["swapon", "--show", "--noheadings"],
                          capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            return HealResult("", "swap", True, "Swap already active, nothing to do")
        
        # Check if swapfile exists but not enabled
        if Path(swapfile).exists():
            subprocess.run(["sudo", "swapon", swapfile], capture_output=True, timeout=10)
            # Add to fstab if not already there
            fstab = Path("/etc/fstab").read_text()
            if swapfile not in fstab:
                subprocess.run(
                    ["sudo", "bash", "-c", f"echo '{swapfile} none swap sw 0 0' >> /etc/fstab"],
                    capture_output=True, timeout=5)
            return HealResult("", "swap", True, f"Re-enabled existing {swapfile}")
        
        # Create new swap file
        size_bytes = size_mb * 1024 * 1024
        subprocess.run(["sudo", "fallocate", "-l", str(size_bytes), swapfile],
                      capture_output=True, check=True, timeout=15)
        subprocess.run(["sudo", "chmod", "600", swapfile],
                      capture_output=True, check=True, timeout=5)
        subprocess.run(["sudo", "mkswap", swapfile],
                      capture_output=True, check=True, timeout=10)
        subprocess.run(["sudo", "swapon", swapfile],
                      capture_output=True, check=True, timeout=10)
        # Persist
        subprocess.run(
            ["sudo", "bash", "-c", f"echo '{swapfile} none swap sw 0 0' >> /etc/fstab"],
            capture_output=True, timeout=5)
        
        return HealResult("", "swap", True, f"Created and enabled {size_mb}MB swap at {swapfile}")
    except subprocess.CalledProcessError as e:
        return HealResult("", "swap", False, f"Swap creation failed: {e.stderr.strip() if e.stderr else str(e)}")
    except Exception as e:
        return HealResult("", "swap", False, f"Swap creation error: {e}")


def action_alert(message: str) -> str:
    """Log alert and return message for human attention."""
    logger.warning("GUARDIAN ALERT: %s", message)
    return f"ALERT: {message}"


# Action dispatch

def _dispatch_action(check: CheckResult, action_type: str,
                     repo_path: str, service_name: str) -> HealResult:
    """Route a single action type to its implementation function."""
    if action_type == "pull":
        return action_pull(repo_path)
    elif action_type == "restart":
        return action_restart(service_name)
    elif action_type == "patch":
        fp = check.file_path or check.metadata.get("file_path", "")
        old = check.metadata.get("old_content", "")
        new = check.metadata.get("new_content", "")
        if not fp or not old:
            return HealResult(check.name, "patch", False, "Missing file_path/old_content in metadata")
        return action_patch(fp, old, new)
    elif action_type == "commit":
        # Safety: dry-run first always
        dry = action_commit(repo_path, f"guardian: fix {check.name}", dry_run=True)
        if not dry.success and "DRY-RUN" not in dry.detail:
            return dry
        return action_commit(repo_path, f"guardian: fix {check.name}", dry_run=False)
    elif action_type == "alert":
        msg = f"[{check.name}] {check.detail}"
        action_alert(msg)
        return HealResult(check.name, "alert", True, msg)
    elif action_type == "swap":
        return action_swap()
    return HealResult(check.name, action_type, False, f"Unknown action: {action_type}")


def _verify_compile(repo_path: str, target_dir: str = "tical_code") -> bool:
    """Check all .py files in target_dir compile cleanly."""
    base = Path(repo_path) / target_dir
    if not base.exists():
        return False
    for pyf in base.rglob("*.py"):
        if "__pycache__" in pyf.parts:
            continue
        try:
            subprocess.run(["python3", "-m", "py_compile", str(pyf)],
                           capture_output=True, timeout=15, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False
    return True


# Main entry: heal()

def heal(
    check_results: List[CheckResult],
    repo_path: str = os.environ.get("REPO_PATH", os.path.expanduser("~/project")),
    service_name: str = os.environ.get("SERVICE_NAME", "worker.service"),
) -> List[HealResult]:
    """Run decision table against failed checks; execute fix action chains.

    Safety enforced per-invocation:
      - Cooldown between retries per action type.
      - Max 3 consecutive restarts within 1 hour - alert + stop if exceeded.
      - Dry-run before every auto-commit.
      - Post-pull compile verification; alert on failure.
    """
    results: List[HealResult] = []

    for check in check_results:
        if check.ok:
            continue

        ad = DECISION_TABLE.get(check.name)
        if ad is None:
            logger.info("No heal action defined for: %s", check.name)
            continue

        # Cooldown enforcement
        last = _last_action_ts(check.name)
        if last is not None and ad.cooldown_seconds > 0:
            if time.time() - last < ad.cooldown_seconds:
                continue

        # Restart storm guard
        if "restart" in ad.action_types and _count_recent_restarts() >= MAX_CONSECUTIVE_RESTARTS:
            msg = "RESTART STORM: {}/hr limit reached. Halted.".format(MAX_CONSECUTIVE_RESTARTS)
            logger.critical(msg)
            action_alert(f"{msg} Check: {check.name}")
            results.append(HealResult(check.name, "alert", False, msg))
            continue

        # Execute action chain with retries
        chain_success = False
        for attempt in range(ad.max_retries + 1):
            ok = True
            for atype in ad.action_types:
                r = _dispatch_action(check, atype, repo_path, service_name)
                r.check_name = check.name
                _record_action(check.name, atype, r.success, r.detail)
                results.append(r)
                if not r.success:
                    ok = False
                    break
            if ok:
                chain_success = True
                break
            if attempt < ad.max_retries:
                logger.info("Retrying %s (%d/%d)", check.name, attempt + 2, ad.max_retries + 1)
                time.sleep(10)

        # Post-pull compile verification
        if "pull" in ad.action_types and chain_success:
            if not _verify_compile(repo_path):
                msg = "Post-pull compile verification failed - manual intervention needed"
                action_alert(msg)
                results.append(HealResult(check.name, "alert", False, msg))

    return results


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "CheckResult", "HealResult", "HealAction", "DECISION_TABLE",
    "heal", "action_pull", "action_restart", "action_patch",
    "action_commit", "action_alert",
]
