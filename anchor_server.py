"""EITElite / tical-code Anchor HTTP Server

Worker reads/writes shared state through this service:
  - Anchor data (ops-anchor.json + ai_workers)
  - Worker online status registration
  - Work task queue
  - Sibling worker status

All paths support /anchor prefix (nginx pass-through).
"""
import json
import os
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

PORT = int(os.getenv("ANCHOR_PORT", "9878"))
ANCHOR_FILE = Path(os.getenv("ANCHOR_FILE", str(Path.home() / "anchors/ops-anchor.json")))

# Auto-create anchor directory on first import
ANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)

# In-memory worker state (sibling worker status)
worker_states: dict = {}
state_lock = threading.Lock()

# Task queue
task_queue: list = []
task_lock = threading.Lock()
task_counter = 0


class AnchorHandler(BaseHTTPRequestHandler):
    
    def log_message(self, fmt, *args):
        pass
    
    # ─── Path normalization: strip /anchor prefix ───
    
    def _normalize(self, raw_path: str) -> str:
        """Convert /anchor/work → /work, /anchor/task/dequeue → /task/dequeue"""
        p = raw_path.split("?")[0].rstrip("/")
        # Strip /anchor prefix (nginx pass-through)
        if p.startswith("/anchor"):
            p = p[len("/anchor"):] or "/"
        return p
    
    # ─── Data loading ───
    
    def _load_anchor(self) -> dict:
        if ANCHOR_FILE.exists():
            try:
                return json.loads(ANCHOR_FILE.read_text())
            except Exception:
                pass
        return {"version": "unknown"}
    
    def _count_py_files(self, root_dir: str) -> dict:
        """Dynamic stats: scan .py file count and lines of code in directory"""
        import subprocess as _sp
        d = Path(root_dir)
        if not d.exists():
            return {"py_files": 0, "py_lines": 0, "path": root_dir, "status": "not_found"}
        try:
            r = _sp.run(
                ["find", str(d), "-name", "*.py", "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*/.git/*", "-not", "-path", "*/training_data/*"],
                capture_output=True, text=True, timeout=30
            )
            files = [f for f in r.stdout.strip().split("\n") if f.strip()]
            total_lines = 0
            for f in files:
                try:
                    total_lines += len(Path(f).read_text().split("\n"))
                except Exception:
                    pass
            return {"py_files": len(files), "py_lines": total_lines,
                    "path": str(d), "status": "ok"}
        except Exception as e:
            return {"py_files": 0, "py_lines": 0, "path": root_dir, "status": f"error: {e}"}
    
    def _get_systems(self) -> dict:
        """Real-time code stats for both systems. git clone if not available locally to count."""
        eite = self._count_py_files(os.getenv("EITELITE_PATH", "/home/YOUR_USER/eitelite"))
        tical = self._count_py_files(os.getenv("TICAL_CODE_PATH", "/home/YOUR_USER/project"))
        # eitelite incomplete → temp clone to count
        if eite.get("py_files", 0) < 10:
            try:
                import subprocess as _sp, tempfile
                tmp = tempfile.mkdtemp(prefix="eite_count_")
                _sp.run(["git", "clone", "--depth", "1",
                    "https://github.com/ticalzzt/eitelite-codebase-audit.git",
                    tmp + "/eitelite"], capture_output=True, timeout=60)
                eite = self._count_py_files(tmp + "/eitelite")
                _sp.run(["rm", "-rf", tmp], capture_output=True)
            except Exception:
                pass
        # tical-code incomplete -> same as above
        if tical.get("py_files", 0) < 10:
            try:
                import subprocess as _sp, tempfile
                tmp = tempfile.mkdtemp(prefix="tical_count_")
                _sp.run(["git", "clone", "--depth", "1",
                    "https://github.com/ticalzzt/tical-code.git",
                    tmp + "/tical-code"], capture_output=True, timeout=60)
                tical = self._count_py_files(tmp + "/tical-code")
                _sp.run(["rm", "-rf", tmp], capture_output=True)
            except Exception:
                pass
        return {"eitelite": eite, "tical-code": tical}
    
    def _worker_list(self) -> dict:
        """Return sibling worker status (for _anchor_api('anchor/work'))"""
        with state_lock:
            return dict(worker_states)
    
    # ─── Response helpers ───
    
    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))
    
    # ─── GET ───
    
    def do_GET(self):
        path = self._normalize(self.path)
        
        # Root path → return full anchor data
        if path in ("", "/"):
            data = self._load_anchor()
            with state_lock:
                data["_workers"] = dict(worker_states)
            return self._send_json(data)
        
        # /work or /workers → return sibling worker status
        if path in ("/work", "/workers"):
            return self._send_json(self._worker_list())
        
        # /task/list → return task queue
        if path == "/task/list":
            with task_lock:
                return self._send_json({"tasks": list(task_queue)})
        
        # /systems → dynamic system code stats
        if path == "/systems":
            return self._send_json(self._get_systems())
        
        self._send_json({"error": "not_found"}, 404)
    
    # ─── POST ───
    
    def do_POST(self):
        path = self._normalize(self.path)
        body = self._read_body()
        name = body.get("name", body.get("worker", "unknown"))
        
        # /register → Worker registration/heartbeat
        if path in ("/register", "/", "/anchor"):
            with state_lock:
                worker_states[name] = {
                    "hostname": body.get("hostname", ""),
                    "status": body.get("status", "online"),
                    "task": body.get("task", ""),
                    "progress": body.get("progress", ""),
                    "last_seen": time.time(),
                    "ip": self.client_address[0],
                }
            return self._send_json({"ok": True, "name": name})
        
        # /work → update work status (same as /register)
        if path == "/work":
            with state_lock:
                if name in worker_states:
                    worker_states[name].update({
                        "status": body.get("status", worker_states[name].get("status", "unknown")),
                        "task": body.get("task", worker_states[name].get("task", "")),
                        "progress": body.get("progress", worker_states[name].get("progress", "")),
                        "last_seen": time.time(),
                    })
                else:
                    worker_states[name] = {
                        "hostname": body.get("hostname", ""),
                        "status": body.get("status", "online"),
                        "task": body.get("task", ""),
                        "progress": body.get("progress", ""),
                        "last_seen": time.time(),
                    }
            return self._send_json({"ok": True, "name": name})
        
        # /task/enqueue → enqueue
        if path == "/task/enqueue":
            global task_counter
            with task_lock:
                task_counter += 1
                task_id = str(int(time.time())) + str(task_counter)
                task_queue.append({
                    "id": task_id,
                    "task": body.get("task", ""),
                    "target": body.get("target", ""),
                    "sender": body.get("sender", ""),
                    "status": "pending",
                    "created_at": time.time(),
                })
            return self._send_json({"ok": True, "task_id": task_id})
        
        # /task/dequeue → dequeue (mark as running, don't remove)
        if path == "/task/dequeue":
            worker = body.get("worker", "")
            with task_lock:
                for t in task_queue:
                    if t["status"] == "pending" and (not t["target"] or t["target"] == worker):
                        t["status"] = "running"
                        t["worker"] = worker
                        t["started_at"] = time.time()
                        return self._send_json({"ok": True, "task": t})
            return self._send_json({"ok": True, "task": None})
        
        # /task/complete → mark as done (don't remove)
        if path == "/task/complete":
            task_id = body.get("task_id", "")
            with task_lock:
                for t in task_queue:
                    if t["id"] == task_id:
                        t["status"] = "done"
                        t["completed_at"] = time.time()
                        break
            return self._send_json({"ok": True})
        
        self._send_json({"error": "not_found"}, 404)


def main():
    if not ANCHOR_FILE.exists():
        fallback = Path.home() / ".tical-code" / "anchor.json"
        if fallback.exists():
            os.environ["ANCHOR_FILE"] = str(fallback)
    
    print(f"Anchor server v0.2 -> {ANCHOR_FILE}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AnchorHandler)
    print(f"Listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
