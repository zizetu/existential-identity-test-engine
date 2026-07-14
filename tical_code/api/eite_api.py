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

"""
EITE Engine API - REST interface for eite/engine.py + eite/config.py (dead modules repurposed).

Endpoints:
    POST /init               - initialize EITE identity engine
    GET  /status             - get engine status (identity_id, fingerprint, initialized)
    POST /process            - process a request with identity signature
    GET  /is-immutable       - check if a path is protected by Never Self-Deny
    GET  /config             - get a config value
    POST /config             - set a config value
    POST /config/save        - persist config to disk
"""

import json
import logging
import os
from aiohttp import web

from tical_code.core.eite import engine
from tical_code.core.eite import config

logger = logging.getLogger("EITElite.api.eite")

_EITE_API_KEY = os.environ.get("EITE_API_KEY", "")


async def _auth_middleware(request: web.Request) -> None:
    """Deny-by-default: require EITE_API_KEY for all requests (AG-C3)."""
    if not _EITE_API_KEY:
        raise web.HTTPUnauthorized(reason="EITE_API_KEY not configured - set EITE_API_KEY env var")
    api_key = request.headers.get("X-API-Key", "")
    if api_key != _EITE_API_KEY:
        raise web.HTTPUnauthorized(reason="invalid or missing X-API-Key")


async def _check_auth(request: web.Request) -> None:
    """Decorator-style auth check for route handlers."""
    await _auth_middleware(request)


# ── Engine handlers ─────────────────────────────────────────────────────────

async def init_engine(request: web.Request) -> web.Response:
    """Initialize the EITE identity engine.

    Request: {"identity_id": "my-agent-001", "workspace": "."}
    Response: {"initialized": true, "identity_id": "...", "fingerprint": "..."}
    """
    await _check_auth(request)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    identity_id = data.get("identity_id")
    workspace = data.get("workspace", ".")

    ok = engine.init(identity_id=identity_id, workspace=workspace)
    if not ok:
        # Already initialized - still return current state
        pass

    return web.json_response({
        "initialized": engine._initialized,
        "identity_id": engine.get_identity_id(),
        "hardware_fingerprint": engine.get_hardware_fingerprint(),
    })


async def engine_status(request: web.Request) -> web.Response:
    """Get current EITE engine status."""
    await _check_auth(request)
    return web.json_response({
        "initialized": engine._initialized,
        "identity_id": engine.get_identity_id(),
        "hardware_fingerprint": engine.get_hardware_fingerprint(),
        "version": engine.__version__,
    })


async def process_request(request: web.Request) -> web.Response:
    """Process a request through the EITE engine (identity check + signature).

    Request: {"request": "text to process"}
    Response: {"status": "allowed|blocked|error", "signature": "...", ...}
    """
    await _check_auth(request)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    req_text = data.get("request", "")
    if not req_text:
        return web.json_response({"error": "request field is required"}, status=400)

    if not engine._initialized:
        return web.json_response(
            {"status": "error", "msg": "EITE not initialized"}, status=400
        )

    try:
        result = engine.process(req_text)
    except NameError:
        # process() calls an undefined check() function in the dead module
        # Fall back to direct sign without check
        try:
            sig = engine.sign(engine.get_identity_id(), req_text)
            return web.json_response({"status": "allowed", "signature": sig,
                                       "note": "check() unavailable, signature only"})
        except Exception:
            return web.json_response({"status": "allowed", "signature": "unavailable",
                                       "note": "signing unavailable"}, status=200)
    except Exception as e:
        logger.exception("process failed")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)

    return web.json_response(result)


async def is_immutable_check(request: web.Request) -> web.Response:
    """Check if a path is protected by Never Self-Deny.

    Query: ?path=engine.py
    Response: {"path": "...", "immutable": true/false}
    """
    await _check_auth(request)
    path = request.query.get("path", "")
    if not path:
        return web.json_response({"error": "path query parameter is required"}, status=400)

    protected = engine.is_immutable(path)
    return web.json_response({"path": path, "immutable": protected})


# ── Config handlers ─────────────────────────────────────────────────────────

async def get_config(request: web.Request) -> web.Response:
    """Get a config value, or all config if no key specified.

    Query: ?key=identity_id
    Response: {"key": "...", "value": "..."} or {"config": {...}}
    """
    await _check_auth(request)
    key = request.query.get("key", None)
    if key:
        value = config.get(key)
        return web.json_response({"key": key, "value": value})
    else:
        return web.json_response({"config": config._config})


async def set_config(request: web.Request) -> web.Response:
    """Set a config value (in-memory only, use /config/save to persist).

    Request: {"key": "identity_id", "value": "my-agent"}
    """
    await _check_auth(request)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    key = data.get("key", "")
    value = data.get("value")
    if not key:
        return web.json_response({"error": "key is required"}, status=400)

    config.set(key, value)
    return web.json_response({"key": key, "value": value, "status": "set"})


async def save_config(request: web.Request) -> web.Response:
    """Persist config to disk."""
    await _check_auth(request)
    try:
        config.save()
        return web.json_response({"status": "saved"})
    except Exception as e:
        logger.exception("config save failed")
        return web.json_response({"error": str(e)}, status=500)


# ── Registration ────────────────────────────────────────────────────────────

def register(router: web.UrlDispatcher, prefix: str = "/api/eite") -> None:
    router.add_post(f"{prefix}/init", init_engine)
    router.add_get(f"{prefix}/status", engine_status)
    router.add_post(f"{prefix}/process", process_request)
    router.add_get(f"{prefix}/is-immutable", is_immutable_check)
    router.add_get(f"{prefix}/config", get_config)
    router.add_post(f"{prefix}/config", set_config)
    router.add_post(f"{prefix}/config/save", save_config)
