#!/usr/bin/env python3

# tical-code -- AI Agent Platform
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

"""
tical_guardian.py - Autonomous System Guardian
=============================================
Functions: Ollama Self-healing / Benchmark Stuck Detection / API Rate-limit Auto-switch / SSH Tunnel Maintenance / Memory/Disk Cleanup
Exit Codes: 0=OK 1=Fixed 2=Fix Failed
Install: cron */5 * * * * cd $PROJECT_DIR && python3 scripts/tical_guardian.py
"""
import os, sys, json, time, fcntl, signal, shutil, socket, tempfile, hashlib
import subprocess, shlex, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

# ==================== Base Paths ====================
HOME = Path.home()
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", str(HOME / "project")))
SCRIPTS = PROJECT_DIR / "scripts"
STATE_DIR = PROJECT_DIR / ".guardian"
STATE_DIR.mkdir(parents=True, exist_ok=True)

LOCK_FILE = STATE_DIR / "guardian.lock"
LOG_FILE = "/tmp/guardian.log"
ALERT_FILE = "/tmp/guardian.alert"

# All worker configs (only execute local one)
WORKERS = {
    "worker-1": {
        "config": HOME / "worker/assistant-01/config.json",
        "service": "worker-1.service",
        "ollama": True,
        "tunnel": False,
    },
    "worker-2": {
        "config": Path(os.environ.get("WORKER_2_CONFIG", str(HOME / "worker/assistant-02/config.json"))),
        "service": "worker-2.service",
        "ollama": False,
        "tunnel": False,
    },
    "worker-3": {
        "config": Path(os.environ.get("WORKER_3_CONFIG", str(HOME / "worker/assistant-03/config.json"))),
        "service": "worker-3.service",
        "ollama": False,
        "tunnel": False,
    },
    "worker-4": {
        "config": Path(os.environ.get("WORKER_4_CONFIG", str(HOME / "worker/assistant-04/config.json"))),
        "service": "worker-4.service",
        "ollama": False,
        "tunnel": False,
    },
}

# Auto-detect which node this machine is
def detect_node():
    host = os.uname().nodename
    if "worker-1" in host.lower():
        return "worker-1"
    if "worker-2" in host.lower():
        return "worker-2"
    if "worker-3" in host.lower():
        return "worker-3"
    if "worker-4" in host.lower():
        return "worker-4"
    return "worker-1"  # fallback

VPS = os.environ.get("WORKER_NAME", detect_node())
CFG = WORKERS.get(VPS, WORKERS["worker-1"])
FIXED = False
FAILED = False

# ==================== Utility Functions ====================
def utc_now():
    return datetime.utcnow().isoformat()[:19]

def log(msg):
    line = f"[{utc_now()}] [{VPS}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def alert(msg):
    line = f"[{utc_now()}] [{VPS}] ALERT: {msg}"
    with open(ALERT_FILE, "a") as f:
        f.write(line + "\n")

def mark_fixed(msg):
    global FIXED; FIXED = True; log(f"✅ {msg}")

def mark_failed(msg):
    global FAILED; FAILED = True; log(f"❌ {msg}"); alert(msg)

def run(cmd, timeout=30, shell=False):
    try:
        if isinstance(cmd, str) and not shell:
            cmd = shlex.split(cmd)
        r = subprocess.run(cmd, shell=shell,
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)

def read_json(path):
    try: return json.loads(Path(path).read_bytes())
    except (FileNotFoundError, json.JSONDecodeError, OSError): return None

def atomic_write(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.remove(tmp)

def acquire_lock():
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        print("guardian already running")
        sys.exit(0)

# ==================== 1. Ollama Health & Self-healing ====================
def check_ollama():
    if not CFG.get("ollama"):
        return

    fail_file = STATE_DIR / "ollama_fail"
    fail = int(fail_file.read_text()) if fail_file.exists() else 0

    try:
        r = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
        ok = r.status == 200
    except Exception:
        ok = False

    if ok:
        fail_file.write_text("0")
        return

    fail += 1
    fail_file.write_text(str(fail))
    log(f"ollama unhealthy ({fail}/3)")

    if fail >= 3:
        log("Restarting ollama...")
        run("sudo systemctl restart ollama", timeout=60)
        # ollama model loading takes time, wait up to 60s
        for i in range(12):
            time.sleep(5)
            try:
                urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
                mark_fixed("ollama recovered")
                fail_file.write_text("0")
                return
            except (urllib.error.URLError, OSError):
                continue
        mark_failed("ollama unable to recover (no response after 60s)")

# ==================== 2. Benchmark Stuck Detection ====================
def check_benchmark():
    state_file = STATE_DIR / "bench_state.json"
    old = {}
    if state_file.exists():
        try: old = json.loads(state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError): pass

    code, out, _ = run(["pgrep", "-af", "benchmarks.runner"])
    if not out:
        state_file.write_text("{}")
        return

    new = {}
    for line in out.split("\n"):
        parts = line.strip().split(None, 1)
        if len(parts) != 2: continue
        pid = parts[0]
        if not pid.isdigit(): continue
        pid = int(pid)

        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read().split()
                utime, stime, starttime = int(stat[13]), int(stat[14]), stat[21]
            with open(f"/proc/{pid}/io") as f:
                io = next(int(l.split()[1]) for l in f if l.startswith("read_bytes:"))
            new[str(pid)] = {"cpu": utime + stime, "io": io, "ts": time.time()}
        except (FileNotFoundError, OSError, ValueError):
            continue

        prev = old.get(str(pid))
        if prev and prev["cpu"] == new[str(pid)]["cpu"] and prev["io"] == new[str(pid)]["io"]:
            log(f"benchmark stuck: PID {pid}")
            os.kill(pid, signal.SIGKILL)
            mark_fixed(f"Killed stuck process {pid}")

    atomic_write(state_file, new)

# ==================== 3. API Rate-limit Auto-failover ====================
def check_api_failover():
    cfg = read_json(CFG["config"]) or {}
    if not cfg: return
    models = cfg.get("available_models", [])
    if len(models) < 2: return

    current = cfg.get("ai_model", "")
    if current not in models: return

    # Check if log contains 429/500/502
    log_text = Path(LOG_FILE).read_text(errors="ignore") if Path(LOG_FILE).exists() else ""
    triggers = ["429", "500", "502", "rate limit", "Too Many Requests", "timeout"]
    if not any(t in log_text for t in triggers):
        return

    # Cooldown (no repeat switch within 30 min)
    cooldown = STATE_DIR / "model_switch_ts"
    now = time.time()
    if cooldown.exists():
        try:
            if now - float(cooldown.read_text()) < 1800:
                return
        except (FileNotFoundError, ValueError): pass

    # Switch to next
    idx = models.index(current)
    next_model = models[(idx + 1) % len(models)]
    if next_model == current:
        return  # only one model

    log(f"API rate limited: {current} → {next_model}")
    cfg["ai_model"] = next_model
    atomic_write(CFG["config"], cfg)
    cooldown.write_text(str(now))

    r = run(f"sudo systemctl restart {CFG['service']}", timeout=30)
    if r[0] == 0:
        mark_fixed(f"Switched to {next_model}")
    else:
        mark_failed(f"Restart {CFG['service']} failed")

# ==================== 4. SSH Tunnel Maintenance ====================
def check_ssh_tunnel():
    if not CFG.get("tunnel"):
        return

    try:
        s = socket.socket(); s.settimeout(3)
        s.connect(("127.0.0.1", 11434)); s.close()
        return  # tunnel OK
    except (socket.error, OSError):
        log("SSH tunnel down")

    run(["pkill", "-f", "ssh.*11434"])
    time.sleep(1)

    # Tunnel command uses env vars for SSH host and key
    mesh_host = os.environ.get("MESH_SSH_HOST", "YOUR_MESH_HOST")
    tunnel = ("autossh -M 0 -f -N "
              "-o StrictHostKeyChecking=no "
              "-o ServerAliveInterval=30 "
              "-o ServerAliveCountMax=3 "
              "-L 11434:localhost:11434 "
              f"ubuntu@{mesh_host} -i /root/.ssh/tunnel_key")
    r = run(tunnel)
    time.sleep(2)
    try:
        socket.create_connection(("127.0.0.1", 11434), timeout=3)
        mark_fixed("SSH tunnel rebuilt")
    except (socket.error, OSError):
        mark_failed("SSH tunnel rebuild failed")

# ==================== 5. Memory Cleanup ====================
def check_memory():
    try:
        with open("/proc/meminfo") as f:
            avail = next(int(l.split()[1]) for l in f if l.startswith("MemAvailable:"))
        avail_mb = avail // 1024
        if avail_mb >= 2048:
            return
        log(f"Low memory: {avail_mb}MB")
        run(["sync"])
        subprocess.run(["sudo", "tee", "/proc/sys/vm/drop_caches"], input=b"3\n", capture_output=True, timeout=30)
        run(["ollama", "gc"])
        run(["sudo", "journalctl", "--vacuum-time=3d"])
        mark_fixed(f"Memory cleanup done (was {avail_mb}MB)")
    except Exception as e:
        log(f"Memory check failed: {e}")

# ==================== 6. Disk Cleanup ====================
def check_disk():
    try:
        total, used, free = shutil.disk_usage("/")
        pct = int(used / total * 100)
        if pct <= 85:
            return
        log(f"Disk >85%: {pct}%")
        run(["sudo", "journalctl", "--vacuum-time=7d"])
        run(["pip", "cache", "purge"])
        run(["npm", "cache", "clean", "--force"])
        run(["find", "/tmp", "-type", "f", "-mtime", "+7", "-delete"])
        mark_fixed(f"Disk cleanup done (was {pct}%)")
    except Exception as e:
        log(f"Disk check failed: {e}")

# ==================== Main Loop ====================
def main():
    acquire_lock()
    log("=" * 50)
    log("guardian started")
    log(f"Worker: {VPS}, Config: {CFG['config']}, Service: {CFG['service']}")

    check_ollama()
    check_benchmark()
    check_api_failover()
    check_ssh_tunnel()
    check_memory()
    check_disk()

    if FAILED:
        log(f"Exit code: 2 (fix failed)")
        sys.exit(2)
    if FIXED:
        log(f"Exit code: 1 (fixed)")
        sys.exit(1)
    log(f"Exit code: 0 (OK)")
    sys.exit(0)

if __name__ == "__main__":
    main()
