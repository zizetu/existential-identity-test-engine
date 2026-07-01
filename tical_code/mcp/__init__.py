"""
MCP integration for tical-code.

Wraps the existing MCPClient (from core/mcp_client.py) with:
  - Config file loading from config/mcp_servers.json
  - Startup integration
  - Prompt injection for tool definitions
  - Tool registration into the main tool_executor dispatch
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tical-code.mcp_integration")


def load_mcp_config(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """Load MCP server configuration from file.

    Sources (first found wins):
      1. config/mcp_servers.json under repo_root
      2. MCP_SERVERS env var (JSON string)

    Returns a dict suitable for MCPClient.load_servers():
      {"servers": {"name": {"command": ..., "args": ..., ...}, ...}}
    """
    # Try config file
    if repo_root:
        path = Path(repo_root) / "config" / "mcp_servers.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                # Accept both flat and {"servers": ...} format
                if "servers" in data:
                    return data
                return {"servers": data}

    # Try MCP_SERVERS env var
    env_json = os.environ.get("MCP_SERVERS", "")
    if env_json:
        try:
            data = json.loads(env_json)
            if "servers" in data:
                return data
            return {"servers": data}
        except json.JSONDecodeError:
            logger.warning("MCP_SERVERS env var is not valid JSON")

    return {}


def get_mcp_tool_definitions(client) -> List[Dict]:
    """Get OpenAI-compatible tool definitions from all MCP servers.

    Returns tool definitions for prompt injection so the AI knows
    what MCP tools are available.
    """
    tools = []
    discovered = client.get_discovered_tools() if hasattr(client, "get_discovered_tools") else {}
    for server_name, server_tools in discovered.items():
        prefix = f"mcp_{server_name}_"
        for t in server_tools:
            name = t.get("name", "unknown")
            safe_name = name.replace("-", "_").replace(".", "_")
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{prefix}{safe_name}",
                    "description": t.get("description", f"MCP tool from {server_name}"),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
    return tools


def register_mcp_tools(client, tool_registry=None) -> int:
    """Register all discovered MCP tools into the tool_executor dispatch table.

    Each tool is registered via tool_executor.register_plugin_tool() with
    a name prefix like ``mcp_<server>_<tool>``.  If ``tool_registry``
    (a ToolRegistry instance) is provided, tools are also registered there.

    MCP call handlers are async; this function wraps them in a synchronous
    bridge via ``asyncio.run()`` so the tool_executor's synchronous dispatch
    can call them.

    Args:
        client: An MCPClient instance with connected and discovered tools.
        tool_registry: Optional ToolRegistry for additional registration.

    Returns:
        Number of tools registered.
    """
    from tical_code.core.tool_executor import register_plugin_tool

    discovered = client.get_discovered_tools() if hasattr(client, "get_discovered_tools") else {}
    count = 0

    for server_name, server_tools in discovered.items():
        prefix = f"mcp_{server_name}_"
        for t in server_tools:
            raw_name = t.get("name", "unknown")
            safe_name = raw_name.replace("-", "_").replace(".", "_")
            full_name = f"{prefix}{safe_name}"
            description = t.get("description", f"MCP tool from {server_name}")
            input_schema = t.get("inputSchema", {"type": "object", "properties": {}})

            # Capture values for closure
            _server = server_name
            _tool = raw_name

            async def _async_handler(arguments: Dict[str, Any], _s=_server, _t=_tool) -> Dict[str, Any]:
                return await client.call_tool(_s, _t, arguments)

            def _sync_wrapper(args: dict, _h=_async_handler, _s=_server, _t=_tool) -> dict:
                """Synchronous bridge for async MCP handlers."""
                try:
                    import asyncio as _aio
                    loop = _aio.get_event_loop()
                    if loop.is_closed():
                        loop = _aio.new_event_loop()
                        _aio.set_event_loop(loop)
                    task = loop.create_task(_h(args))
                    return loop.run_until_complete(task)
                except Exception as exc:
                    return {"error": f"MCP tool {_s}/{_t} failed: {exc}"}

            # Register into tool_executor dispatch
            register_plugin_tool(full_name, _sync_wrapper)
            count += 1
            logger.debug("Registered MCP tool: %s", full_name)

            # Also register into ToolRegistry if provided
            if tool_registry is not None:
                try:
                    from tical_code.core.tool_registry import ToolDefinition, VerifyLevel

                    async def _reg_handler(arguments: Dict[str, Any], _s=_server, _t=_tool) -> Dict[str, Any]:
                        return await client.call_tool(_s, _t, arguments)

                    def _reg_sync(args: dict, _h=_reg_handler, _s=_server, _t=_tool) -> dict:
                        try:
                            import asyncio as _aio
                            loop = _aio.get_event_loop()
                            if loop.is_closed():
                                loop = _aio.new_event_loop()
                                _aio.set_event_loop(loop)
                            task = loop.create_task(_h(args))
                            return loop.run_until_complete(task)
                        except Exception as exc:
                            return {"error": f"MCP tool {_s}/{_t} failed: {exc}"}

                    tool_def = ToolDefinition(
                        name=full_name,
                        description=(
                            f"MCP tool from server '{server_name}': {description}"
                        ),
                        params=input_schema if isinstance(input_schema, dict) else {},
                        handler=_reg_sync,
                        verify_level=VerifyLevel.SCHEMA,
                        timeout=120,
                        edition="both",
                    )
                    tool_registry.register_sync(tool_def)
                except ImportError:
                    pass

    logger.info("MCP: registered %d tools into tool_executor dispatch", count)
    return count


async def init_mcp(repo_root: Optional[str] = None) -> Tuple[int, Any, List[Dict]]:
    """Initialize MCP: load config, connect, discover tools.

    Returns:
        (server_count, mcp_client, tool_definitions)
    """
    from tical_code.core.mcp_client import MCPClient

    # Load config
    config = load_mcp_config(repo_root)
    if not config:
        logger.info("No MCP servers configured (skip)")
        return 0, None, []

    # Create client and load servers
    client = MCPClient()
    client.load_servers(config)

    # Connect to all servers
    connected = 0
    for name in list(client._configs.keys()):
        result = await client.connect(name)
        if result.get("success"):
            connected += 1

    if connected == 0:
        logger.warning("No MCP servers could be connected")
        return 0, client, []

    # Discover tools
    discovery = await client.discover_tools()
    if not discovery.get("success"):
        logger.warning("MCP tool discovery failed: %s", discovery.get("error"))
    tool_defs = get_mcp_tool_definitions(client)
    logger.info("MCP: %d servers, %d tools", connected, len(tool_defs))

    return connected, client, tool_defs
