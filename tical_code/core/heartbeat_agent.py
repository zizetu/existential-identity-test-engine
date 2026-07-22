"""Heartbeat pulse detector for tical-code workers.

OpenClaw-style active heartbeat: cron writes a pulse file → worker detects
the new pulse → reads HEARTBEAT.md → executes tasks → delivers results.

This is registered as a module loaded by unified_worker.py.
"""

from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("tical-code.heartbeat")

# --- paths ---
_HOME = Path.home()
_STATE_DIR = _HOME / ".hermes" / "state"
_PULSE_FILE = _STATE_DIR / "heartbeat.pulse"
_HEARTBEAT_MD = _HOME / ".hermes" / "HEARTBEAT.md"
_LAST_PULSE_FILE = _STATE_DIR / "heartbeat_agent_last.txt"

# --- public API called by unified_worker ---

def get_last_processed() -> float:
    """Return timestamp of the last pulse we acted on."""
    try:
        return float(_LAST_PULSE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0.0

def save_last_processed(ts: float) -> None:
    """Persist the last processed pulse timestamp."""
    _LAST_PULSE_FILE.write_text(str(ts))

def read_pulse() -> dict | None:
    """Read the current heartbeat pulse file. Returns None if missing."""
    try:
        data = _PULSE_FILE.read_text()
        return json.loads(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def has_new_pulse() -> tuple[bool, dict | None]:
    """Check if a new pulse has arrived since last check.

    Falls back to querying the backend API if no local pulse file.

    Returns:
        (has_new, pulse_data) — pulse_data is the parsed pulse if new.
    """
    pulse = read_pulse()
    if pulse is not None:
        ts = pulse.get("ts_unix", 0.0)
        last_ts = get_last_processed()
        if ts > last_ts:
            return True, pulse

    # Fallback: query backend API
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:8081/api/heartbeat", timeout=3)
        data = json.loads(resp.read().decode())
        backend_pulse = data.get("last_pulse")
        if backend_pulse:
            ts = backend_pulse.get("ts_unix", 0.0) or backend_pulse.get("_received_at", 0.0)
            last_ts = get_last_processed()
            if ts > last_ts:
                return True, backend_pulse
    except Exception:
        pass

    return False, None

def read_heartbeat_md() -> str:
    """Read the HEARTBEAT.md directives file."""
    try:
        return _HEARTBEAT_MD.read_text()
    except FileNotFoundError:
        return "# HEARTBEAT\nNo directives file found."

def build_heartbeat_message(pulse: dict) -> str:
    """Build a system message describing the heartbeat that the agent can act on."""
    health = pulse.get("health", {})
    directives = pulse.get("directives", [])
    pulse_id = pulse.get("pulse_id", "unknown")
    ts = pulse.get("timestamp", "?")

    lines = [
        f"[HEARTBEAT] Pulse {pulse_id} received at {ts}",
        "",
        "System Health:",
        f"  CPU load: {health.get('load_1m', '?')}",
        f"  Memory: {health.get('mem_pct', '?')}%",
        f"  Disk: {health.get('disk_pct', '?')}%",
        f"  Uptime: {health.get('uptime_hours', '?')}h",
        "",
        "Directives:",
    ]
    for d in directives:
        lines.append(f"  - {d}")
    lines.extend([
        "",
        "Read HEARTBEAT.md for full context and execute the pending tasks.",
        "Report results when complete.",
    ])
    return "\n".join(lines)

def check_and_ack() -> str | None:
    """Main entry point: check for new pulse, acknowledge it, return
    heartbeat message for agent processing (or None if no new pulse).

    Called by unified_worker's main loop every N ticks.
    """
    has_new, pulse = has_new_pulse()
    if not has_new:
        return None

    ts = pulse.get("ts_unix", time.time())
    save_last_processed(ts)

    logger.info(
        "Heartbeat pulse detected: %s — ack'ed at %.1f",
        pulse.get("pulse_id", "?"), ts,
    )

    return build_heartbeat_message(pulse)
