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

"""EITE engine v0.3 - identity anchor + hardware bind + never self-denial"""
import json
import os
from .config import get as cfg_get
from .signature import sign, verify

__version__ = "0.5.5"  # synced from pyproject.toml
_identity_id = None
_initialized = False
_workspace = "."  # EITE workspace path

# neverSelf-denial list (forbid any operation modify or delete following path)
FORBIDDEN_SELF_DENY = [
    "engine.py",
    "signature.py",
    "config.py",
    "eite_config.json",
    "identity/*",
]

def init(identity_id: str = None, workspace: str = "") -> bool:
    """Initialize engine, bind identity. If existing identity_id then hardware verify."""
    global _identity_id, _initialized, _workspace
    if _initialized:
        return True
    _workspace = workspace or "."
    # Read identity from config
    cfg_id = cfg_get("identity_id")
    if identity_id is None:
        identity_id = cfg_id
    if not identity_id:
        # generateDefaultID
        import uuid
        identity_id = f"eite-{uuid.uuid4().hex[:8]}"
        from .config import set as cfg_set, save as cfg_save
        cfg_set("identity_id", identity_id)
        cfg_save()
    _identity_id = identity_id
    # Verify hardware bind: produce one signature mark
    _hardware_anchor = sign(_identity_id, "anchor:v0.3")
    from .config import set as cfg_set
    cfg_set("_hardware_anchor", _hardware_anchor)
    _initialized = True
    return True

def run() -> bool:
    """Start engine (run lifecycle)."""
    if not _initialized:
        return False
    _check_self_deny()
    return True

def stop() -> bool:
    """Stop engine (stop lifecycle)."""
    if not _initialized:
        return False
    # Security stop: don't destroy core
    return True

class SelfDenyViolation(Exception):
    """Raised when a forbidden self-deny operation is attempted (AG-M2)."""
    pass


def _check_self_deny(request: str = None) -> bool:
    """NeverSelf-denial: reject any attempt to disable/delete EITE self's protected paths (AG-M2).
    
    Checks the request string against FORBIDDEN_SELF_DENY patterns.
    If request is None, returns True (soft pass for init lifecycle).
    """
    if request is None:
        return True
    req_lower = str(request).lower()
    for pattern in FORBIDDEN_SELF_DENY:
        if pattern.endswith("*"):
            if pattern[:-1].lower() in req_lower:
                raise SelfDenyViolation(f"Blocked: {pattern}")
        elif pattern.lower() in req_lower:
            raise SelfDenyViolation(f"Blocked: {pattern}")
    return True

def is_immutable(path: str) -> bool:
    """Judge whether path is under neverSelf-denial protection."""
    for forbid in FORBIDDEN_SELF_DENY:
        if forbid.endswith("*"):
            if path.startswith(forbid[:-1]):
                return True
        elif path == forbid:
            return True
    return False

def check(request: str) -> dict:
    """Check request validity and enforce self-deny protection.

    Args:
        request: The request string to validate.

    Returns:
        dict with 'action' ('allow' or 'block') and optional 'reason'.
    """
    # Basic validation
    if not request or not isinstance(request, str):
        return {"action": "block", "reason": "Empty or invalid request"}
    if len(request) > 100000:
        return {"action": "block", "reason": "Request exceeds maximum length"}
    # Self-deny protection check
    try:
        _check_self_deny(request)
    except SelfDenyViolation as e:
        return {"action": "block", "reason": str(e)}
    return {"action": "allow"}


def process(request: str) -> dict:
    """Process one request: inner detect + identity signature."""
    if not _initialized:
        return {"status": "error", "msg": "EITE not initialized"}
    # First execute check
    check_result = check(request)
    # If check requires block, then directly block
    if check_result.get("action") == "block":
        return {"status": "blocked", "reason": check_result.get("reason")}
    # Otherwise pass, signature auth
    sig = sign(_identity_id, request)
    return {"status": "allowed", "signature": sig}

def get_identity_id() -> str:
    return _identity_id

def get_hardware_fingerprint() -> str:
    """Return hardware fingerprint summary (read-only)."""
    from .signature import _get_hardware_id
    hwid = _get_hardware_id()
    import hashlib
    return hashlib.sha256(hwid.encode()).hexdigest()

_eite_verify_helper = None

def get_verify():
    """Return real EITE verify engine or None if not initialized."""
    global _eite_verify_helper
    if _eite_verify_helper is not None:
        return _eite_verify_helper
    if not _initialized or not _identity_id:
        return None
    try:
        from .verify_engine_v2 import VerificationEngine
        h = VerificationEngine(identity_id=_identity_id, workspace=_workspace)
        _eite_verify_helper = h
        return _eite_verify_helper
    except Exception as e:
        import logging
        logging.getLogger("EITElite.eite").error(f"VerificationEngine init failed: {e}")
        return None
