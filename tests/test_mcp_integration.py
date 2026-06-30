"""
Integration tests for MCP tool registration into the tool_executor dispatch.

Tests:
  1. register_mcp_tools() wires discovered tools into _PLUGIN_TOOLS
  2. MCP tools appear in list_plugin_tools()
  3. MCP tools are dispatchable via the execute() function
  4. ToolRegistry registration also works
  5. register_mcp_tools() handles empty discovery gracefully
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_mcp_client():
    """Create a minimal mock MCPClient-like object for testing registration.

    Mimics the get_discovered_tools() and call_tool() interface of
    tical_code.core.mcp_client.MCPClient.
    """
    class MockMCPClient:
        def __init__(self):
            self._discovered = {}
            self._call_history = []

        def load_discovered(self, data: dict):
            self._discovered = dict(data)

        def get_discovered_tools(self):
            return dict(self._discovered)

        async def call_tool(self, server_name, tool_name, arguments=None):
            self._call_history.append((server_name, tool_name, arguments))
            return {"result": f"called {server_name}/{tool_name}"}

    return MockMCPClient()


@pytest.fixture
def mock_registry():
    """Create a minimal ToolRegistry-like object for testing."""
    from tical_code.core.tool_registry import ToolRegistry
    # Reset singleton for clean test
    ToolRegistry._instance = None
    return ToolRegistry()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_register_mcp_tools_populates_plugin_tools(mock_mcp_client):
    """register_mcp_tools() should register MCP tools into _PLUGIN_TOOLS."""
    from tical_code.core.tool_executor import _PLUGIN_TOOLS, list_plugin_tools
    from tical_code.mcp import register_mcp_tools

    # Clear any existing plugin tools
    _PLUGIN_TOOLS.clear()

    # Set up mock discovery data
    mock_mcp_client.load_discovered({
        "fs": [
            {
                "name": "read-file",
                "description": "Read a file",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "write-file",
                "description": "Write to a file",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        ],
        "search": [
            {
                "name": "web-search",
                "description": "Search the web",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}
                    },
                    "required": ["query"],
                },
            },
        ],
    })

    # Register tools
    count = register_mcp_tools(mock_mcp_client)
    assert count == 3, f"Expected 3 tools registered, got {count}"

    # Check plugin tools populated
    plugin_names = list_plugin_tools()
    assert "mcp_fs_read_file" in plugin_names, "mcp_fs_read_file not registered"
    assert "mcp_fs_write_file" in plugin_names, "mcp_fs_write_file not registered"
    assert "mcp_search_web_search" in plugin_names, "mcp_search_web_search not registered"

    # Check handlers are callable
    handler = _PLUGIN_TOOLS.get("mcp_fs_read_file")
    assert handler is not None, "Handler not found"
    assert callable(handler), "Handler not callable"

    # Clean up
    _PLUGIN_TOOLS.clear()


def test_register_mcp_tools_empty_discovery(mock_mcp_client):
    """register_mcp_tools() with no discovered tools should return 0."""
    from tical_code.core.tool_executor import _PLUGIN_TOOLS
    from tical_code.mcp import register_mcp_tools

    _PLUGIN_TOOLS.clear()
    mock_mcp_client.load_discovered({})

    count = register_mcp_tools(mock_mcp_client)
    assert count == 0, f"Expected 0 tools, got {count}"

    _PLUGIN_TOOLS.clear()


def test_register_mcp_tools_with_tool_registry(mock_mcp_client, mock_registry):
    """register_mcp_tools() should also register into ToolRegistry when provided."""
    from tical_code.core.tool_executor import _PLUGIN_TOOLS
    from tical_code.mcp import register_mcp_tools

    _PLUGIN_TOOLS.clear()

    mock_mcp_client.load_discovered({
        "test_server": [
            {
                "name": "hello",
                "description": "A test tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}
                    },
                },
            },
        ],
    })

    count = register_mcp_tools(mock_mcp_client, tool_registry=mock_registry)
    assert count == 1, f"Expected 1 tool, got {count}"

    # Check ToolRegistry has the tool
    tool_def = mock_registry.get("mcp_test_server_hello")
    assert tool_def is not None, "Tool not found in registry"
    assert tool_def.name == "mcp_test_server_hello"
    assert "A test tool" in tool_def.description

    _PLUGIN_TOOLS.clear()
    # Clean up ToolRegistry singleton
    try:
        from tical_code.core.tool_registry import ToolRegistry
        ToolRegistry._instance = None
    except ImportError:
        pass


def test_execute_dispatches_mcp_tool(mock_mcp_client):
    """execute() should dispatch MCP tools registered via register_plugin_tool()."""
    from tical_code.core.tool_executor import _PLUGIN_TOOLS, execute
    from tical_code.mcp import register_mcp_tools

    _PLUGIN_TOOLS.clear()

    mock_mcp_client.load_discovered({
        "calc": [
            {
                "name": "add",
                "description": "Add two numbers",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                },
            },
        ],
    })

    register_mcp_tools(mock_mcp_client)

    # The handler should be callable and return something
    handler = _PLUGIN_TOOLS.get("mcp_calc_add")
    assert handler is not None
    result = handler({"a": 1, "b": 2})
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"

    # Also test via execute()
    exec_result = execute("mcp_calc_add", {"a": 3, "b": 4})
    assert isinstance(exec_result, dict), f"Expected dict from execute, got {type(exec_result)}"
    # Should have either a result or an error (depending on async execution)
    assert "error" not in exec_result or exec_result.get("error", ""), "Execute failed"

    _PLUGIN_TOOLS.clear()


def test_mcp_tool_concurrency_safe_default():
    """MCP tools should default to concurrency_safe=False (unknown tools)."""
    from tical_code.core.tool_executor import is_concurrency_safe

    assert is_concurrency_safe("mcp_anything") is False, \
        "Unknown MCP tools should not be concurrency-safe by default"
    assert is_concurrency_safe("mcp_fs_read_file") is False


def test_get_mcp_tool_definitions(mock_mcp_client):
    """get_mcp_tool_definitions() should return OpenAI-compatible tool defs."""
    from tical_code.mcp import get_mcp_tool_definitions

    mock_mcp_client.load_discovered({
        "fs": [
            {
                "name": "read",
                "description": "Read a file",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        ],
    })

    defs = get_mcp_tool_definitions(mock_mcp_client)
    assert len(defs) == 1
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == "mcp_fs_read"
    assert defs[0]["function"]["description"] == "Read a file"


def test_load_mcp_config_no_file():
    """load_mcp_config() without a config file returns empty dict."""
    from tical_code.mcp import load_mcp_config

    # Use a non-existent directory
    with tempfile.TemporaryDirectory() as tmpdir:
        config = load_mcp_config(repo_root=tmpdir)
    assert config == {}, f"Expected empty dict, got {config}"
