#!/usr/bin/env python3
"""
gateway-guard — Gateway Agent integrity watchdog.
Runs as root for iptables access, monitors /home/ubuntu/.gateway
"""
import os, sys, json, time, hashlib, subprocess, logging, logging.handlers
from datetime import datetime, timezone
from typing import Dict, Optional

HOME = "/home/ubuntu"
GATEWAY_HOME = f"{HOME}/.gateway"
GATEWAY_VENV_BIN = f"{GATEWAY_HOME}/gateway-agent/venv/bin/python"
GATEWAY_CONFIG = f"{GATEWAY_HOME}/config.yaml"
GATEWAY_ENV = f"{GATEWAY_HOME}/.env"
AUTH_KEYS = f"{HOME}/.ssh/authorized_keys"
GUARDIAN_DIR = "/opt/tical-guardian"
BASELINE_FILE = f"{GUARDIAN_DIR}/gateway_baseline.json"
EMERGENCY_DIR = f"{GUARDIAN_DIR}/emergency"
PATROL_LOG = f"{GUARDIAN_DIR}/gateway_guard.log"
CHECKSUM_FILE = f"{GUARDIAN_DIR}/.gateway_checksums"
PATROL_INTERVAL = 120
GATEWAY_PORT = 8642

os.makedirs(GUARDIAN_DIR, exist_ok=True)
os.makedirs(EMERGENCY_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gateway-guard] %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(PATROL_LOG, maxBytes=1_000_000, backupCount=5),
              logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("gateway-guard")

def sha256_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None

def hash_all() -> Dict:
    return {
        "gateway_venv_bin": sha256_file(GATEWAY_VENV_BIN),
        "gateway_config": sha256_file(GATEWAY_CONFIG),
        "gateway_env": sha256_file(GATEWAY_ENV),
        "auth_keys": sha256_file(AUTH_KEYS),
        "gateway_env_perm": oct(os.stat(GATEWAY_ENV).st_mode)[-3:] if os.path.exists(GATEWAY_ENV) else "MISSING",
    }

def load_baseline() -> Optional[Dict]:
    try:
        with open(BASELINE_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_baseline(data: Dict) -> None:
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["hostname"] = os.uname().nodename
    with open(BASELINE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(BASELINE_FILE, 0o400)
    log.info("baseline saved")

def fire_emergency(reason: str, detail: str) -> None:
    alert = {"type":"GATEWAY_INTEGRITY","reason":reason,"detail":detail,
             "action":"iptables DROP","node":os.uname().nodename,
             "timestamp":datetime.now(timezone.utc).isoformat()}
    fname = f"gateway_{int(time.time())}.json"
    with open(os.path.join(EMERGENCY_DIR, fname), "w") as f:
        json.dump(alert, f, indent=2)
    try:
        subprocess.run(["iptables","-C","INPUT","-p","tcp","--dport",str(GATEWAY_PORT),"-j","DROP"], capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(["iptables","-I","INPUT","-p","tcp","--dport",str(GATEWAY_PORT),"-j","DROP"], check=False)
        log.critical("!!! iptables DROP %s inserted !!!", GATEWAY_PORT)
    subprocess.run(["logger","-t","gateway-guard","-p","crit",f"INTEGRITY_BREACH: {reason}"], check=False)
    log.critical("EMERGENCY: %s — %s", reason, detail)

def get_ports() -> set:
    ports = set()
    try:
        out = subprocess.check_output(["ss","-tlnp"], text=True, timeout=10)
        for line in out.splitlines():
            if "LISTEN" not in line: continue
            parts = line.split()
            for p in parts:
                if ":" in p and not p.startswith(("uid=","pid=","users:","(")):
                    addr, port = p.rsplit(":", 1)
                    if addr in ("0.0.0.0","[::]","*") and port.isdigit():
                        ports.add(port)
    except Exception:
        pass
    return ports

KNOWN_PORTS = {"22","80","443","51820","8642"}

def patrol(baseline: Dict) -> bool:
    cur = hash_all()
    violations = []
    if cur["gateway_env_perm"] not in ("600","400","MISSING"):
        violations.append(f".env perm {cur['gateway_env_perm']} (need 600)")
    for key, label in [("gateway_venv_bin","gateway binary"),("gateway_config","config.yaml"),
                        ("auth_keys","authorized_keys")]:
        if baseline.get(key) and cur.get(key) and baseline[key] != cur[key]:
            violations.append(f"{label} changed")
    new = get_ports() - set(baseline.get("ports",[])) - KNOWN_PORTS
    if new:
        violations.append(f"new port(s): {','.join(sorted(new))}")
    if violations:
        fire_emergency("integrity_violation", "; ".join(violations))
        return False
    return True

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--patrol", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    args = ap.parse_args()
    if args.baseline:
        data = hash_all()
        data["ports"] = sorted(get_ports())
        save_baseline(data)
        for k,v in sorted(data.items()):
            if isinstance(v, str) and len(v)>20: v = v[:20]+"..."
            print(f"  {k}: {v}")
        return 0
    baseline = load_baseline()
    if baseline is None:
        log.error("no baseline — run --baseline first")
        return 1
    if args.patrol:
        return 0 if patrol(baseline) else 1
    if args.daemon:
        log.info("daemon start — patrol %ds", PATROL_INTERVAL)
        while True:
            patrol(baseline)
            time.sleep(PATROL_INTERVAL)
    return 0

if __name__ == "__main__":
    sys.exit(main())
