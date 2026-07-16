"""Path deny/allow security layer (Grok Build distillation).

Checks filesystem paths against configurable glob deny patterns BEFORE
any read/write/delete/rename. Deny hits are audited to SQLite.

Config: config/security.json (or defaults if absent).
Zero external dependencies (stdlib only).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class PathDenied(Exception):
    """Raised when a path matches a deny pattern and is not allowlisted."""

    def __init__(
        self,
        path: str,
        operation: str,
        pattern: str,
        message: Optional[str] = None,
    ):
        self.path = path
        self.operation = operation
        self.pattern = pattern
        self.message = message or (
            f"Path denied for {operation}: {path} (matched {pattern})"
        )
        super().__init__(self.message)


_DEFAULT_DENY_PATTERNS = [
    "/etc/**",
    "/etc/*",
    "/sys/**",
    "/sys/*",
    "/proc/**",
    "/proc/*",
    "/dev/**",
    "/dev/*",
    "~/.ssh/**",
    "~/.ssh/*",
    "**/.git/config",
    "*/.git/config",
    "**/__pycache__/**",
    "*/__pycache__/*",
    # Windows-sensitive roots
    "C:/Windows/**",
    "C:\\Windows\\**",
    "**/ntuser.dat",
]

_DEFAULT_ALLOW_PATTERNS: List[str] = []

_DEFAULT_CONFIG = {
    "path_deny": _DEFAULT_DENY_PATTERNS,
    "path_allow": _DEFAULT_ALLOW_PATTERNS,
    "tool_allow": {},  # tool_name -> list of extra allow globs
    "audit_db": None,  # default under TICAL_HOME / path_security_audit.db
}


def _candidate_config_paths() -> List[Path]:
    paths: List[Path] = []
    # Repo-relative config
    here = Path(__file__).resolve()
    # tical_code/core/path_security.py -> repo root two levels up from tical_code
    for parent in list(here.parents)[:6]:
        paths.append(parent / "config" / "security.json")
    # Data home
    home = os.environ.get("TICAL_HOME") or os.environ.get("EITE_DATA_DIR")
    if home:
        paths.append(Path(home).expanduser() / "config" / "security.json")
    paths.append(Path.home() / ".tical" / "config" / "security.json")
    paths.append(Path.home() / ".tical-code" / "config" / "security.json")
    # Env override
    env = os.environ.get("TICAL_SECURITY_CONFIG")
    if env:
        paths.insert(0, Path(env).expanduser())
    return paths


def load_security_config() -> Dict[str, Any]:
    """Load security.json; return defaults if no file is found."""
    cfg = dict(_DEFAULT_CONFIG)
    cfg["path_deny"] = list(_DEFAULT_DENY_PATTERNS)
    cfg["path_allow"] = list(_DEFAULT_ALLOW_PATTERNS)
    cfg["tool_allow"] = {}
    for p in _candidate_config_paths():
        try:
            if p.is_file():
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "path_deny" in data and isinstance(data["path_deny"], list):
                        cfg["path_deny"] = list(data["path_deny"])
                    if "path_allow" in data and isinstance(data["path_allow"], list):
                        cfg["path_allow"] = list(data["path_allow"])
                    if "tool_allow" in data and isinstance(data["tool_allow"], dict):
                        cfg["tool_allow"] = dict(data["tool_allow"])
                    if data.get("audit_db"):
                        cfg["audit_db"] = data["audit_db"]
                    logger.info("path_security: loaded config from %s", p)
                    return cfg
        except Exception as e:
            logger.warning("path_security: failed to load %s: %s", p, e)
    return cfg


def _normalize_for_match(path: str) -> List[str]:
    """Produce path variants for cross-platform glob matching."""
    variants = set()
    raw = path or ""
    variants.add(raw)
    # Posix-style
    variants.add(raw.replace("\\", "/"))
    try:
        resolved = str(Path(raw).expanduser().resolve())
        variants.add(resolved)
        variants.add(resolved.replace("\\", "/"))
    except Exception:
        try:
            expanded = str(Path(raw).expanduser())
            variants.add(expanded)
            variants.add(expanded.replace("\\", "/"))
        except Exception:
            pass
    # Lowercase Windows drive variants
    out = []
    for v in variants:
        if v:
            out.append(v)
            if len(v) >= 2 and v[1] == ":":
                out.append(v[0].lower() + v[1:])
                out.append(v[0].upper() + v[1:])
    # Dedup preserve order
    seen = set()
    result = []
    for v in out:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch with ** support via simplified recursive semantics."""
    if not pattern:
        return False
    pat = pattern.replace("\\", "/")
    candidates = _normalize_for_match(path)
    for cand in candidates:
        c = cand.replace("\\", "/")
        # Direct fnmatch
        if fnmatch.fnmatch(c, pat):
            return True
        # Prefix-style: /etc/ matches /etc/passwd
        if pat.endswith("/") and c.startswith(pat.rstrip("/") + "/"):
            return True
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if c == prefix or c.startswith(prefix + "/"):
                return True
        # Leading **/ pattern
        if pat.startswith("**/"):
            suffix = pat[3:]
            if fnmatch.fnmatch(c, suffix) or fnmatch.fnmatch(os.path.basename(c), suffix):
                return True
            # any path segment prefix
            parts = c.split("/")
            for i in range(len(parts)):
                sub = "/".join(parts[i:])
                if fnmatch.fnmatch(sub, suffix) or fnmatch.fnmatch(sub, pat):
                    return True
        # Match basename-only patterns
        if fnmatch.fnmatch(os.path.basename(c), pat):
            return True
    return False


class PathSecurity:
    """Path deny/allow checker with audit logging."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._lock = threading.RLock()
        self._config = config if config is not None else load_security_config()
        self._deny = list(self._config.get("path_deny") or _DEFAULT_DENY_PATTERNS)
        self._allow = list(self._config.get("path_allow") or [])
        self._tool_allow: Dict[str, List[str]] = dict(self._config.get("tool_allow") or {})
        self._conn: Optional[sqlite3.Connection] = None
        self._audit_db = self._config.get("audit_db") or self._default_audit_db()
        self._init_audit_db()

    @staticmethod
    def _default_audit_db() -> str:
        home = os.environ.get("TICAL_HOME") or os.environ.get("EITE_DATA_DIR")
        if home:
            base = Path(home).expanduser()
        else:
            base = Path.home() / ".tical"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / "path_security_audit.db")

    def _init_audit_db(self) -> None:
        try:
            parent = os.path.dirname(self._audit_db)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self._audit_db, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS path_denies ("
                "id INTEGER PRIMARY KEY,"
                "timestamp REAL NOT NULL,"
                "tool TEXT,"
                "path TEXT NOT NULL,"
                "operation TEXT NOT NULL,"
                "pattern TEXT NOT NULL"
                ")"
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("path_security: audit db init failed: %s", e)
            self._conn = None

    def reload(self) -> None:
        """Reload patterns from disk config."""
        with self._lock:
            self._config = load_security_config()
            self._deny = list(self._config.get("path_deny") or _DEFAULT_DENY_PATTERNS)
            self._allow = list(self._config.get("path_allow") or [])
            self._tool_allow = dict(self._config.get("tool_allow") or {})

    def set_patterns(
        self,
        deny: Optional[Sequence[str]] = None,
        allow: Optional[Sequence[str]] = None,
    ) -> None:
        with self._lock:
            if deny is not None:
                self._deny = list(deny)
            if allow is not None:
                self._allow = list(allow)

    def audit_log(
        self,
        path: str,
        operation: str,
        pattern: str,
        tool: str = "",
    ) -> None:
        """Persist a deny hit to SQLite audit log."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT INTO path_denies (timestamp, tool, path, operation, pattern) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), tool or "", path, operation, pattern),
                )
                self._conn.commit()
            except Exception as e:
                logger.debug("path_security: audit_log failed: %s", e)
        logger.warning(
            "PATH DENIED tool=%s op=%s path=%s pattern=%s",
            tool, operation, path, pattern,
        )

    def _is_allowed(self, path: str, tool: Optional[str] = None) -> bool:
        for pat in self._allow:
            if _glob_match(path, pat):
                return True
        if tool and tool in self._tool_allow:
            for pat in self._tool_allow[tool]:
                if _glob_match(path, pat):
                    return True
        return False

    def _matching_deny(self, path: str) -> Optional[str]:
        for pat in self._deny:
            if _glob_match(path, pat):
                return pat
        return None

    def check(
        self,
        path: str,
        operation: str = "read",
        tool: Optional[str] = None,
        raise_on_deny: bool = True,
    ) -> Tuple[bool, str]:
        """Check path against deny/allow lists.

        Args:
            path: Filesystem path to validate.
            operation: One of read/write/delete/rename.
            tool: Optional tool name for tool-specific allow overrides.
            raise_on_deny: If True, raise PathDenied on deny.

        Returns:
            (allowed, reason) tuple.

        Raises:
            PathDenied: when denied and raise_on_deny is True.
        """
        if not path:
            return True, "empty path skipped"

        with self._lock:
            if self._is_allowed(path, tool=tool):
                return True, "allowlist"

            pattern = self._matching_deny(path)
            if pattern is None:
                return True, "ok"

            self.audit_log(path, operation, pattern, tool=tool or "")
            reason = f"matched deny pattern: {pattern}"
            if raise_on_deny:
                raise PathDenied(path, operation, pattern)
            return False, reason

    def check_safe(
        self,
        path: str,
        operation: str = "read",
        tool: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Non-raising variant of check()."""
        return self.check(path, operation=operation, tool=tool, raise_on_deny=False)

    def recent_denies(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT timestamp, tool, path, operation, pattern "
                    "FROM path_denies ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [
                    {
                        "timestamp": r[0],
                        "tool": r[1],
                        "path": r[2],
                        "operation": r[3],
                        "pattern": r[4],
                    }
                    for r in rows
                ]
            except Exception as e:
                logger.debug("path_security: recent_denies failed: %s", e)
                return []

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# Module singleton
_PATH_SECURITY: Optional[PathSecurity] = None
_PS_LOCK = threading.Lock()


def get_path_security() -> PathSecurity:
    """Return the process-wide PathSecurity singleton."""
    global _PATH_SECURITY
    with _PS_LOCK:
        if _PATH_SECURITY is None:
            _PATH_SECURITY = PathSecurity()
        return _PATH_SECURITY


def check_path(
    path: str,
    operation: str = "read",
    tool: Optional[str] = None,
    raise_on_deny: bool = True,
) -> Tuple[bool, str]:
    """Module-level convenience wrapper."""
    return get_path_security().check(
        path, operation=operation, tool=tool, raise_on_deny=raise_on_deny
    )
