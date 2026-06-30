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
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""EITE identity signature module - persistent secret key + HMAC signing + state change evidence chain."""

import hashlib
import json
import os
import hmac
import secrets
import logging

logger = logging.getLogger("tical-code.eite.signature")

_KEY_DIR = os.path.expanduser("~/.eite")
_KEY_FILE = os.path.join(_KEY_DIR, "secret.key")

_SIGNING_KEY_ENV = "HMAC_SIGNING_KEY"
_DEV_FALLBACK_KEY = "dev-key-fallback-do-not-use-in-production"
_IDENTITY_SEED = "eite-signature-v1"


def _get_signing_key() -> bytes:
    """Get HMAC signing key from env var, persistent key file, or dev fallback."""
    env_key = os.environ.get(_SIGNING_KEY_ENV)
    if env_key:
        return env_key.encode()
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    logger.warning(
        "HMAC_SIGNING_KEY not set and no key file at %s - "
        "using insecure dev fallback. Set %s for production.", _KEY_FILE, _SIGNING_KEY_ENV
    )
    return _DEV_FALLBACK_KEY.encode()


def _load_or_generate_key() -> bytes:
    """Load existing key or generate a new 32-byte random key."""
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    os.makedirs(_KEY_DIR, exist_ok=True)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    os.chmod(_KEY_FILE, 0o600)
    return key


def sign(identity_id: str, payload: str) -> str:
    """Generate HMAC-SHA256 signature for payload using persistent secret key."""
    secret = _load_or_generate_key()
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def verify(identity_id: str, payload: str, signature: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    expected = sign(identity_id, payload)
    return hmac.compare_digest(expected, signature)


def sign_state_change(state_key: str, action: str, result_hash: str) -> str:
    """Sign a state change with HMAC for tamper-evidence chain.

    Uses HMAC_SIGNING_KEY env var, persistent key file, or dev fallback.

    Args:
        state_key: Unique identifier (e.g., tool_name:call_id)
        action: What was done (e.g., 'file_write', 'bash_run')
        result_hash: SHA-256 hex digest of the result

    Returns:
        HMAC-SHA256 hex signature
    """
    key = _get_signing_key()
    payload = f"{state_key}:{action}:{result_hash}:{_IDENTITY_SEED}"
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def verify_state_change(state_key: str, action: str, result_hash: str, signature: str) -> bool:
    """Verify a state change signature.

    Returns True if valid (no tampering detected), False otherwise.
    """
    expected = sign_state_change(state_key, action, result_hash)
    return hmac.compare_digest(expected, signature)
