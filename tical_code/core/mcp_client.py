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

"""
MCP (Model Context Protocol) Client Module.

Provides an MCPClient class that connects to MCP servers via stdio
(subprocess stdin/stdout) or HTTP transport, discovers tools, and
registers them into the ToolRegistry.

JSON-RPC 2.0 is used for all MCP communication. The 'mcp' Python
package is intentionally NOT imported — raw JSON-RPC is implemented
over subprocess stdin/stdout (stdio transport) or urllib (HTTP transport).
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tical-code.mcp_client")

# ---------------------------------------------------------------------------
# Allowed environment variables for subprocess sandboxing
# ---------------------------------------------------------------------------
_ALLOWED_ENV_VARS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})


def _filter_env(custom_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build a sandboxed environment dict from current process.

    Only passes PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR,
    and any XDG_* variables.  Custom env vars from the MCP config are
    merged on top.
    """
    env: Dict[str, str] = {}
    for key, val in os.environ.items():
        if key in _ALLOWED_ENV_VARS or key.startswith("XDG_"):
            env[key] = val
    if custom_env:
        env.update(custom_env)
    return env


# ---------------------------------------------------------------------------
# MCPConfig — dataclass for server configuration
# ---------------------------------------------------------------------------

@dataclass
class MCPConfig:
    """Configuration for a single MCP server.

    Exactly one of ``command`` or ``url`` must be provided (stdio vs HTTP).
    """
    name: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    timeout: int = 120
    connect_timeout: int = 60

    def __post_init__(self) -> None:
        if self.args is None:
            self.args = []
        if bool(self.command) == bool(self.url):
            raise ValueError(
                f"MCPConfig '{self.name}': exactly one of 'command' or 'url' "
                f"must be set (got command={self.command!r}, url={self.url!r})"
            )


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helper
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "eite-agent", "version": "0.1.0"}

_MSG_ID = 0


def _next_id() -> int:
    global _MSG_ID
    _MSG_ID += 1
    return _MSG_ID


def _build_request(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    }


def _initialize_request() -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        },
    }


def _tools_list_request() -> Dict[str, Any]:
    return _build_request("tools/list")


def _tools_call_request(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _build_request("tools/call", {"name": tool_name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class _Connection:
    """Wraps either a subprocess (stdio) or an HTTP endpoint."""

    def __init__(self, config: MCPConfig) -> None:
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False

    # ---- properties -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- connect / disconnect ---------------------------------------------

    async def connect(self) -> None:
        """Open transport connection and run MCP initialize handshake."""
        if self._config.command:
            await self._connect_stdio()
        elif self._config.url:
            await self._connect_http()
        else:
            raise RuntimeError(f"No transport method for {self._config.name}")

        # Run initialize handshake
        init_req = _initialize_request()
        resp = await self.send_request(init_req)
        if "error" in resp:
            raise RuntimeError(
                f"MCP initialize failed for {self._config.name}: "
                f"{resp['error']}"
            )
        logger.info(
            "MCP connected: %s (transport=%s)",
            self._config.name,
            "stdio" if self._config.command else "http",
        )
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
            self._process = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    # ---- stdio transport --------------------------------------------------

    async def _connect_stdio(self) -> None:
        """Start subprocess and attach async reader/writer."""
        cmd = self._config.command
        if cmd is None:
            raise RuntimeError("stdio transport requires 'command' to be set")
        args = self._config.args or []
        env = _filter_env(self._config.env)

        loop = asyncio.get_event_loop()

        self._process = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Wrap stdin/stdout for line-oriented JSON-RPC
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(
            lambda: protocol, self._process.stdout
        )

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.flowcontrol.FlowControlMixin,
            self._process.stdin,
        )

        self._reader = reader
        self._writer = asyncio.StreamWriter(
            writer_transport, writer_protocol, None, loop
        )

    # ---- HTTP transport ---------------------------------------------------

    async def _connect_http(self) -> None:
        """No-op for HTTP — connection is established per-request."""
        pass

    # ---- send / receive ---------------------------------------------------

    async def send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and return the response dict.

        Returns {"error": ...} on failure instead of raising.
        """
        if self._config.command:
            return await self._send_stdio(request)
        elif self._config.url:
            return await self._send_http(request)
        return {"error": {"message": "No transport configured"}}

    async def _send_stdio(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Write one JSON line to subprocess stdin, read one line from stdout."""
        if self._writer is None or self._reader is None:
            return {"error": {"message": "stdio connection not established"}}

        try:
            line = json.dumps(request, ensure_ascii=False) + "\n"
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()

            raw = await asyncio.wait_for(
                self._reader.readline(),
                timeout=self._config.timeout,
            )
            if not raw:
                return {"error": {"message": "stdio connection closed (empty response)"}}
            return json.loads(raw.decode("utf-8"))
        except asyncio.TimeoutError:
            return {
                "error": {
                    "message": f"stdio timeout after {self._config.timeout}s "
                    f"for {self._config.name}"
                }
            }
        except json.JSONDecodeError as exc:
            return {"error": {"message": f"Invalid JSON from stdio: {exc}"}}
        except Exception as exc:
            return {"error": {"message": f"stdio transport error: {exc}"}}

    async def _send_http(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """POST JSON to the HTTP(S) endpoint and return the parsed response."""
        url = self._config.url
        if not url:
            return {"error": {"message": "No HTTP URL configured"}}

        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            req = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib.request.urlopen(
                    req, timeout=self._config.timeout
                ),
            )
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            return {"error": {"message": f"HTTP {exc.code}: {detail}"}}
        except urllib.error.URLError as exc:
            return {"error": {"message": f"HTTP request failed: {exc.reason}"}}
        except asyncio.TimeoutError:
            return {
                "error": {
                    "message": f"HTTP timeout after {self._config.timeout}s "
                    f"for {url}"
                }
            }
        except json.JSONDecodeError as exc:
            return {"error": {"message": f"Invalid JSON from HTTP: {exc}"}}
        except Exception as exc:
            return {"error": {"message": f"HTTP transport error: {exc}"}}


# ---------------------------------------------------------------------------
# MCPClient — main class
# ---------------------------------------------------------------------------

def _sanitize_tool_name(name: str) -> str:
    """Replace hyphens and dots with underscores."""
    return name.replace("-", "_").replace(".", "_")


class MCPClient:
    """Manages connections to MCP servers.

    Supports both stdio (subprocess) and HTTP transport.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, _Connection] = {}
        self._configs: Dict[str, MCPConfig] = {}
        self._discovered_tools: Dict[str, List[Dict[str, Any]]] = {}
        # {server_name: [{name, description, inputSchema}, ...]}

    # ---- configuration loading --------------------------------------------

    def load_servers(self, servers_cfg: Dict[str, Any]) -> None:
        """Load server configurations from a parsed JSON dict.

        The dict structure::

            {"servers": {"name": {"command": ..., "args": ..., ...}, ...}}

        Each server must have exactly one of ``command`` (stdio) or ``url`` (HTTP).
        """
        raw_servers = servers_cfg.get("servers", {})
        for name, raw in raw_servers.items():
            config = MCPConfig(
                name=name,
                command=raw.get("command"),
                args=raw.get("args", []),
                url=raw.get("url"),
                env=raw.get("env", {}),
                timeout=raw.get("timeout", 120),
                connect_timeout=raw.get("connect_timeout", 60),
            )
            self._configs[name] = config
            logger.debug("MCP server loaded: %s", name)

    # ---- connection management --------------------------------------------

    async def connect(self, server_name: str, config: Optional[MCPConfig] = None) -> Dict[str, Any]:
        """Connect to an MCP server.

        Args:
            server_name: Name of the server.
            config: Optional MCPConfig. If not provided, looks up by name.

        Returns:
            {"success": True} or {"success": False, "error": "..."}
        """
        if config is None:
            config = self._configs.get(server_name)
        if config is None:
            return {"success": False, "error": f"Unknown MCP server: {server_name}"}

        conn = _Connection(config)
        last_error = ""
        for attempt in range(4):  # 0-indexed → up to 3 retries
            try:
                await conn.connect()
                self._connections[server_name] = conn
                logger.info("MCP connected to %s (attempt %d)", server_name, attempt + 1)
                return {"success": True}
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "MCP connect %s attempt %d failed: %s",
                    server_name, attempt + 1, last_error,
                )
                if attempt < 3:
                    wait = 2 ** attempt  # exponential backoff: 1, 2, 4s
                    logger.info("MCP retrying %s in %ds...", server_name, wait)
                    await asyncio.sleep(wait)

        return {"success": False, "error": last_error}

    async def disconnect(self, server_name: str) -> Dict[str, Any]:
        """Disconnect from an MCP server."""
        conn = self._connections.pop(server_name, None)
        if conn:
            await conn.disconnect()
            return {"success": True}
        return {"success": False, "error": f"Not connected: {server_name}"}

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        for name in list(self._connections.keys()):
            await self.disconnect(name)

    # ---- tool discovery ---------------------------------------------------

    async def discover_tools(self) -> Dict[str, Any]:
        """Call ``tools/list`` on every connected server.

        Returns:
            {"success": True, "tools": {server_name: [tool_dict, ...]}}
            or {"success": False, "error": "..."}
        """
        result: Dict[str, List[Dict[str, Any]]] = {}
        errors: List[str] = []

        for name, conn in self._connections.items():
            if not conn.connected:
                errors.append(f"{name}: not connected")
                continue

            req = _tools_list_request()
            resp = await conn.send_request(req)

            if "error" in resp:
                errors.append(f"{name}: {resp['error'].get('message', resp['error'])}")
                continue

            tools = resp.get("result", {}).get("tools", [])
            result[name] = tools
            logger.info("MCP discovered %d tools from %s", len(tools), name)

        self._discovered_tools = result

        if not errors:
            return {"success": True, "tools": result}
        return {"success": False, "error": "; ".join(errors), "tools": result}

    # ---- tool calling -----------------------------------------------------

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call a tool on the specified MCP server.

        Args:
            server_name: Name of the connected server.
            tool_name: Tool name as defined by the MCP server.
            arguments: Tool arguments dict.

        Returns:
            JSON-RPC response dict (with "result" or "error").
        """
        conn = self._connections.get(server_name)
        if conn is None:
            return {"error": {"message": f"Not connected to server: {server_name}"}}
        if not conn.connected:
            return {"error": {"message": f"Connection lost to server: {server_name}"}}

        req = _tools_call_request(tool_name, arguments or {})
        resp = await conn.send_request(req)

        # If connection dropped, attempt auto-reconnect once
        if resp.get("error") and "connection" in str(resp.get("error", {})).lower():
            logger.info("MCP auto-reconnecting %s after connection drop...", server_name)
            reconnect_result = await self.connect(server_name)
            if reconnect_result.get("success"):
                # Retry the tool call
                req = _tools_call_request(tool_name, arguments or {})
                resp = await conn.send_request(req)
            else:
                return {"error": {"message": f"Auto-reconnect failed for {server_name}"}}

        return resp

    # ---- tool registration into ToolRegistry ------------------------------

    def register_all_tools(self, tool_registry: Any) -> Dict[str, Any]:
        """Register all discovered MCP tools into the ToolRegistry.

        Each tool is named ``mcp_{server_name}_{tool_name}`` with hyphens
        and dots replaced by underscores.

        Args:
            tool_registry: A ``ToolRegistry`` instance (with a
                ``register_sync(ToolDefinition)`` method).

        Returns:
            {"success": True, "count": N} or {"success": False, "error": "..."}
        """
        try:
            from tical_code.core.tool_registry import ToolDefinition, VerifyLevel
        except ImportError:
            return {"success": False, "error": "ToolRegistry not importable"}

        count = 0
        for server_name, tools in self._discovered_tools.items():
            for tool in tools:
                mcp_tool_name = tool.get("name", "unknown")
                safe_name = _sanitize_tool_name(mcp_tool_name)
                full_name = f"mcp_{server_name}_{safe_name}"
                description = tool.get("description", "") or ""
                input_schema = tool.get("inputSchema", {})

                # Capture values for closure
                _server = server_name
                _tool = mcp_tool_name

                async def _handler(arguments: Dict[str, Any], _s=_server, _t=_tool) -> Dict[str, Any]:
                    return await self.call_tool(_s, _t, arguments)

                tool_def = ToolDefinition(
                    name=full_name,
                    description=(
                        f"MCP tool from server '{server_name}': {description}"
                    ),
                    params=input_schema if isinstance(input_schema, dict) else {},
                    handler=_handler,
                    verify_level=VerifyLevel.SCHEMA,
                    timeout=self._configs.get(server_name, MCPConfig(name="")).timeout,
                    edition="both",
                )
                tool_registry.register_sync(tool_def)
                count += 1
                logger.debug("Registered MCP tool: %s", full_name)

        logger.info("MCP registered %d tools into ToolRegistry", count)
        return {"success": True, "count": count}

    # ---- utility ----------------------------------------------------------

    def get_connected_servers(self) -> List[str]:
        """Return list of currently connected server names."""
        return [n for n, c in self._connections.items() if c.connected]

    def get_discovered_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return discovered tools dict: {server_name: [tool_dict, ...]}."""
        return dict(self._discovered_tools)
