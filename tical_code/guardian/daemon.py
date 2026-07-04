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

# provenance:ticalasi-zzt-2026​
#!/usr/bin/env python3
"""
Autonomous self-healing daemon for eite-agent workers.

Zero dependency on EITElite worker modules - runs independently.
Polls GitHub → runs checks.py → applies healer.py actions → alerts via Telegram.

Usage:
    python3 -m tical_code.guardian.daemon /path/to/repo
    python3 -m tical_code.guardian.daemon /path/to/repo --poll-interval 300
    python3 -m tical_code.guardian.daemon --generate-unit  # print systemd unit
"""

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL = 300  # 5 minutes
MAX_RESTART_FAILURES = 3
MAX_CONSECUTIVE_RESTARTS = 3
DEFAULT_WORKER_SERVICE = os.environ.get("GUARDIAN_SERVICE", "unified-worker")

SYSTEMD_UNIT = """[Unit]
Description=EITElite Guardian - Autonomous Self-Healing Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
ExecStart={python} -m tical_code.guardian.daemon {workdir} --poll-interval {interval}
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=tical-guardian
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH={workdir}
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


class Guardian:
    """Autonomous daemon: poll → check → heal → alert."""

    def __init__(self, repo_path: str, poll_interval: int = DEFAULT_POLL_INTERVAL):
        self.repo = Path(repo_path).resolve()
        self.poll_interval = poll_interval
        self._running = True

        # Node identity — worker service from GUARDIAN_SERVICE env, fallback to DEFAULT_WORKER_SERVICE
        self.worker_service = os.environ.get("GUARDIAN_SERVICE", DEFAULT_WORKER_SERVICE)
        self.node = os.environ.get("GUARDIAN_NODE", os.uname().nodename)

        # Telegram config
        self.tg_token = os.environ.get("GUARDIAN_TG_TOKEN", os.environ.get("TG_BOT_TOKEN", ""))
        self.tg_chat = os.environ.get("GUARDIAN_TG_CHAT", os.environ.get("TG_CHAT_ID", ""))

        # Restart tracking
        self._restart_history: List[float] = []

        # Logging
        self.log = logging.getLogger("tical-guardian")
        self.log.setLevel(logging.INFO)
        if not self.log.handlers:
            h = logging.handlers.SysLogHandler(address="/dev/log")
            h.setFormatter(logging.Formatter(
                "tical-guardian[%(process)d]: %(levelname)s %(message)s"))
            self.log.addHandler(h)
        # Also log to stderr for foreground runs
        if not any(isinstance(h, logging.StreamHandler) for h in self.log.handlers):
            self.log.addHandler(logging.StreamHandler(sys.stderr))

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        self.log.info("Guardian init: repo=%s node=%s service=%s poll=%ds",
                      self.repo, self.node, self.worker_service, self.poll_interval)

    def _on_signal(self, signum: int, frame: object) -> None:
        self.log.info("Signal %d, shutting down", signum)
        self._running = False

    # ── Telegram Alerts ────────────────────────────────────────────────────

    def _discover_chat_id(self) -> str:
        """Auto-discover chat_id from recent bot messages (getUpdates)."""
        if not self.tg_token:
            return ""
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/getUpdates?limit=3"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data["result"]:
                for update in reversed(data["result"]):
                    msg = update.get("message") or update.get("channel_post")
                    if msg and msg.get("chat", {}).get("id"):
                        chat_id = str(msg["chat"]["id"])
                        self.log.info("Discovered chat_id=%s from getUpdates", chat_id)
                        return chat_id
        except Exception as exc:
            self.log.debug("getUpdates discovery failed: %s", exc)
        return ""

    def _resolve_chat_id(self) -> str:
        """Return configured chat_id, auto-discover from bot, or read from file."""
        if self.tg_chat:
            return self.tg_chat
        if not hasattr(self, "_discovered_chat_id"):
            self._discovered_chat_id = self._discover_chat_id()
            if not self._discovered_chat_id:
                # Fallback: read from file written by worker
                chat_file = Path.home() / ".guardian_chat_id"
                try:
                    if chat_file.exists():
                        self._discovered_chat_id = chat_file.read_text().strip()
                        self.log.info("Loaded chat_id=%s from %s",
                                      self._discovered_chat_id, chat_file)
                except Exception:
                    pass
            if self._discovered_chat_id:
                self.tg_chat = self._discovered_chat_id
        return self._discovered_chat_id

    LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    MIN_ALERT_LEVEL = os.environ.get("GUARDIAN_ALERT_LEVEL", "WARNING")

    def alert(self, message: str, level: str = "INFO") -> bool:
        """Send Telegram alert. Suppressed if level < GUARDIAN_ALERT_LEVEL (default: WARNING)."""
        if self.LEVEL_RANK.get(level, 1) < self.LEVEL_RANK.get(self.MIN_ALERT_LEVEL, 2):
            self.log.debug("Alert suppressed (below %s): %s", self.MIN_ALERT_LEVEL, message[:80])
            return False
        
        chat_id = self._resolve_chat_id()
        if not self.tg_token or not chat_id:
            self.log.info("Telegram not configured; alert skipped: %s", message[:80])
            return False

        emoji = {"INFO": "\u2139\ufe0f", "WARNING": "\u26a0\ufe0f",
                 "ERROR": "\U0001f6a8", "CRITICAL": "\U0001f534"}
        prefix = emoji.get(level, "\u2139\ufe0f")
        text = f"{prefix} *Guardian* [{self.node}]\n{message}"
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text,
                           "parse_mode": "Markdown"}).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status == 200
            self.log.info("Alert %s: %s", "sent" if ok else "failed", message[:100])
            return ok
        except Exception as exc:
            self.log.error("Alert send failed: %s", exc)
            return False

    # ── Git Helpers ────────────────────────────────────────────────────────

    def _git(self, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args, cwd=self.repo, capture_output=True, text=True, timeout=timeout)

    def has_new_commits(self) -> bool:
        """Fetch remote; return True if origin/main has new commits."""
        if self._git(["fetch", "origin"], timeout=90).returncode != 0:
            return False
        rev = self._git(["rev-list", "--count", "HEAD..origin/main"], timeout=30)
        try:
            count = int(rev.stdout.strip())
            if count > 0:
                self.log.info("%d new commit(s) on origin/main", count)
                return True
        except ValueError:
            pass
        return False

    def git_pull(self) -> bool:
        """Fast-forward pull from origin/main."""
        self.log.info("Action: git pull")
        proc = self._git(["pull", "--ff-only", "origin", "main"], timeout=90)
        if proc.returncode == 0:
            self.alert("git pull succeeded for %s" % self.repo.name, "INFO")
            return True
        self.alert("git pull FAILED: " + proc.stderr[:200], "ERROR")
        return False

    def git_rollback(self) -> bool:
        """Rollback to previous commit."""
        self.log.warning("Action: git rollback")
        # Save current HEAD
        self._git(["reset", "--hard", "HEAD~1"], timeout=30)
        self.alert("ROLLBACK executed - reverted to previous commit", "CRITICAL")
        return True

    # ── Worker Control ─────────────────────────────────────────────────────

    def restart_worker(self) -> bool:
        """Restart worker service, verify it comes back up."""
        now = time.time()
        self._restart_history = [t for t in self._restart_history if now - t < 3600]
        self._restart_history.append(now)

        if len(self._restart_history) > MAX_CONSECUTIVE_RESTARTS:
            self._enter_safe_mode()
            return False

        self.log.info("Action: restart %s", self.worker_service)
        proc = subprocess.run(
            ["sudo", "systemctl", "restart", self.worker_service],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            self.alert("systemctl restart failed: " + proc.stderr[:200], "ERROR")
            return False

        # Poll for 30s to verify it comes back up
        for _ in range(15):
            time.sleep(2)
            check = subprocess.run(
                ["systemctl", "is-active", self.worker_service],
                capture_output=True, text=True, timeout=10)
            if check.stdout.strip() == "active":
                self.alert("Worker restarted and active", "INFO")
                return True

        self.alert("Worker did not become active after 30s", "ERROR")
        return False

    def _enter_safe_mode(self) -> None:
        """Stop all automatic repairs after too many consecutive failures."""
        self.alert(
            f"SAFE MODE: {MAX_CONSECUTIVE_RESTARTS}+ restart failures in 1 hour. "
            "All automatic repairs halted. Manual intervention required.",
            "CRITICAL")
        self.log.critical("Entering safe mode")
        self._running = False

    # ── Self-Check (via checks.py) ─────────────────────────────────────────

    def run_checks(self) -> Tuple[int, str]:
        """Run checks.py programmatically, return (exit_code, output)."""
        try:
            sys.path.insert(0, str(self.repo))
            from tical_code.guardian.checks import run_all_checks
            results = run_all_checks()
            failures = [r for r in results if not r.ok]
            output_lines = []
            for r in results:
                status = "OK" if r.ok else "FAIL"
                output_lines.append(f"[{status}] [{r.severity}] {r.name}: {r.detail}")
            output = "\n".join(output_lines)
            exit_code = len(failures)
            self.log.info("self-check: %d/%d passed", len(results) - exit_code, len(results))
            return exit_code, output
        except Exception as exc:
            self.log.error("self-check import failed: %s", exc)
            # Fallback: run checks.py as subprocess
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "tical_code.guardian.checks"],
                    cwd=self.repo, capture_output=True, text=True, timeout=120,
                    env={**os.environ, "PYTHONPATH": str(self.repo)})
                output = (proc.stdout + "\n" + proc.stderr).strip()
                return proc.returncode, output
            except Exception as exc2:
                self.log.error("fallback check also failed: %s", exc2)
                return -99, f"ERROR: {exc2}"

    # ── Heal (via healer.py) ───────────────────────────────────────────────

    def heal(self, check_output: str) -> List[Dict]:
        """Run healer.py on check failures. Returns list of heal actions taken."""
        try:
            sys.path.insert(0, str(self.repo))
            from tical_code.guardian.checks import run_all_checks
            from tical_code.guardian.healer import heal as run_heal

            results = run_all_checks()
            failures = [r for r in results if not r.ok]
            if not failures:
                return []

            heal_results = run_heal(failures)
            actions = []
            for hr in heal_results:
                self.log.info("Heal action: %s success=%s detail=%s",
                              hr.action_taken, hr.success, hr.detail[:100])
                actions.append({
                    "action": hr.action_taken,
                    "success": hr.success,
                    "detail": hr.detail,
                })
                if hr.action_taken == "ALERT":
                    self.alert(f"[{hr.detail[:200]}]", "WARNING")
            return actions
        except Exception as exc:
            self.log.error("healer import failed: %s", exc)
            return [{"action": "ERROR", "success": False, "detail": str(exc)}]

    # ── Main Loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main daemon loop: poll GitHub → check → heal → sleep."""
        self.alert(f"Guardian started on {self.node} watching {self.repo.name}", "INFO")
        self.log.info("Guardian running - poll=%ds service=%s",
                      self.poll_interval, self.worker_service)

        while self._running:
            try:
                # 1) Check for new commits on GitHub
                if self.has_new_commits():
                    self.log.info("New commits detected - pulling")
                    if self.git_pull():
                        # 2) Run checks after pull
                        exit_code, output = self.run_checks()
                        if exit_code > 0:
                            self.log.warning("Checks failed after pull: %d issues", exit_code)
                            # 3) Try to heal
                            actions = self.heal(output)
                            for a in actions:
                                if a["action"] == "restart" and a["success"]:
                                    self.restart_worker()
                                elif a["action"] == "PULL" and not a["success"]:
                                    self.git_rollback()
                        else:
                            self.log.info("All checks passed after pull")
                    else:
                        self.alert("git pull failed - will retry next cycle", "WARNING")

                # 4) Periodic health check even without new commits
                exit_code, output = self.run_checks()
                if exit_code > 0 and not any(r["action"] == "ALERT" for r in (
                        self.heal(output) if exit_code > 0 else [])):
                    self.log.info("Periodic check: %d issues (no action)", exit_code)

            except Exception as exc:
                self.log.error("Main loop error: %s", exc)
                self.alert(f"Guardian loop error: {exc}", "ERROR")

            # Sleep in 1-second slices for responsive shutdown
            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1)

        self.alert("Guardian shutting down", "INFO")
        self.log.info("Guardian shutdown complete")


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="EITElite Guardian Daemon")
    parser.add_argument("repo", nargs="?", help="Path to repo (eite-agent)")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f"GitHub poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--generate-unit", action="store_true",
                        help="Print systemd unit file and exit")
    args = parser.parse_args()

    if args.generate_unit:
        print(SYSTEMD_UNIT.format(
            user=os.environ.get("USER", "ubuntu"),
            workdir=os.path.abspath(args.repo or "."),
            python=sys.executable,
            interval=args.poll_interval))
        return

    if not args.repo:
        parser.error("repo path required (or use --generate-unit)")

    guardian = Guardian(args.repo, args.poll_interval)
    guardian.run()


if __name__ == "__main__":
    main()
