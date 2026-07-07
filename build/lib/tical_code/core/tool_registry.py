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

"""
Tool Registry System (v0.3 P0 #1)
==================================

Core philosophy: "Do NOT trust AI output, assume AI will hallucinate"

This module provides:
- ToolDefinition: Standardized tool specification with JSON Schema params
- ToolRegistry: Central registry for tool management
- ToolExecutor: Parses AI instructions and dispatches to appropriate tools
- Force-Verify integration: Every tool execution is verified

References:
- tical_code.core.verify: Force-Verify system
- tical_code.plugins: Plugin framework with ToolResult
"""

# DESIGNED-NOT-DEAD: Tool registry with sandbox levels. Superset of tool_executor's tool management. Awaiting unified tool system. DO NOT DELETE - tool marketplace + permission foundation.


import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Import Force-Verify components
try:
    from .verify import VerifyLevel, VerifyResult, force_verify, verify_result
except ImportError:
    # Fallback for standalone usage
    class VerifyLevel:
        NONE = 0
        BASIC = 1
        SCHEMA = 2
        DUAL = 3
        HUMAN = 4
        IDENTITY = 5

    @dataclass
    class VerifyResult:
        passed: bool = True
        level: VerifyLevel = None
        method: str = "none"
        details: str = ""
        elapsed_ms: float = 0.0
        timestamp: float = field(default_factory=time.time)

    def force_verify(level, tool_name=""):
        def decorator(func):
            return func
        return decorator

    def verify_result(result, level, tool_name=""):
        return VerifyResult(passed=result is not None, level=level, method="basic")


# Import Plugin components
try:
    from tical_code.plugins import ToolResult
except ImportError:
    @dataclass
    class ToolResult:
        """Result from a tool execution."""
        success: bool
        data: Any = None
        error: Optional[str] = None
        verified: bool = False
        elapsed_ms: float = 0.0

        def to_dict(self) -> Dict:
            return {
                'success': self.success,
                'data': self.data,
                'error': self.error,
                'verified': self.verified,
                'elapsed_ms': self.elapsed_ms,
            }


# =============================================================================
# Sandbox Level Enum (P1-6 Fix)
# =============================================================================

class SandboxLevel:
    """
    Sandbox execution levels for progressive security.
    
    Attributes:
        NONE: No sandbox needed, tool is safe
        RECOMMENDED: Tool should run in sandbox if available
        REQUIRED: Tool MUST run in sandbox for security
    """
    NONE = "none"        # No sandbox required, tool is safe
    RECOMMENDED = "recommended"  # Sandbox recommended (e.g., file read/write)
    REQUIRED = "required"      # Sandbox required (e.g., shell_exec)


# =============================================================================
# Tool Definition
# =============================================================================

@dataclass
class ToolDefinition:
    """
    Standardized tool specification with verification settings.

    Attributes:
        name: Unique identifier for the tool
        description: Human-readable description for AI understanding
        params: JSON Schema format parameter definitions
        handler: The actual function to execute
        verify_level: Verification strictness level (default: SCHEMA)
        timeout: Execution timeout in seconds (default: 30)
        edition: Which edition supports this tool: lite/full/both
        requires_confirmation: Whether human confirmation is needed
        allowed_roles: List of roles allowed to call this tool (default: ["all"])
                       - "all" means all roles can call
                       - Specific roles like ["admin", "operator"] restrict access
        sandbox_level: Sandbox execution level (default: NONE)
                      - "none": no sandbox required
                      - "recommended": sandbox recommended
                      - "required": sandbox required
    """
    name: str
    description: str
    params: Dict[str, Any]
    handler: Callable[..., Any]
    verify_level: VerifyLevel = VerifyLevel.SCHEMA
    timeout: int = 30
    edition: str = "lite"  # lite, full, both
    requires_confirmation: bool = False
    allowed_roles: List[str] = field(default_factory=lambda: ["all"])  # v2 0d
    sandbox_level: str = SandboxLevel.NONE  # P1-6: sandboxprotectlevel

    def validate_params(self, params: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate parameters against JSON Schema.

        Args:
            params: Parameters to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        required = self.params.get('required', [])

        # Check required parameters
        for req_param in required:
            if req_param not in params:
                return False, f"Missing required parameter: {req_param}"

        # Check parameter types
        properties = self.params.get('properties', {})
        for param_name, param_value in params.items():
            if param_name in properties:
                expected_type = properties[param_name].get('type')
                if expected_type:
                    if not self._check_type(param_value, expected_type):
                        return False, f"Invalid type for {param_name}: expected {expected_type}"

        return True, None

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected JSON Schema type."""
        type_map = {
            'string': str,
            'number': (int, float),
            'integer': int,
            'boolean': bool,
            'array': list,
            'object': dict,
            'null': type(None),
        }
        expected = type_map.get(expected_type)
        if expected is None:
            return True  # Unknown type, skip check
        return isinstance(value, expected)


# =============================================================================
# Tool Registry
# =============================================================================

class ToolRegistry:
    """
    Central registry for tool management.

    Singleton pattern ensures single source of truth for all tools.
    Thread-safe for concurrent access.

    Example:
        registry = ToolRegistry()
        registry.register(my_tool_def)
        tool = registry.get("read_file")
        results = registry.list_tools(edition="lite")
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, ToolDefinition] = {}
            cls._instance._lock = asyncio.Lock()
        return cls._instance

    def __init__(self):
        """Initialize the registry (called on each import, but singleton ensures idempotency)."""
        pass

    async def register(self, tool_def: ToolDefinition) -> bool:
        """
        Register a tool in the registry.

        Args:
            tool_def: ToolDefinition to register

        Returns:
            True if registration successful
        """
        async with self._lock:
            if tool_def.name in self._tools:
                logger.warning(f"Tool '{tool_def.name}' already registered, overwriting")
            self._tools[tool_def.name] = tool_def
            logger.info(f"Registered tool: {tool_def.name} (edition={tool_def.edition})")
            return True

    def register_sync(self, tool_def: ToolDefinition) -> bool:
        """
        Synchronous version of register for non-async contexts.

        Args:
            tool_def: ToolDefinition to register

        Returns:
            True if registration successful
        """
        if tool_def.name in self._tools:
            logger.warning(f"Tool '{tool_def.name}' already registered, overwriting")
        self._tools[tool_def.name] = tool_def
        logger.info(f"Registered tool: {tool_def.name} (edition={tool_def.edition})")
        return True

    async def unregister(self, name: str) -> bool:
        """
        Unregister a tool from the registry.

        Args:
            name: Tool name to unregister

        Returns:
            True if tool was removed, False if not found
        """
        async with self._lock:
            if name in self._tools:
                del self._tools[name]
                logger.info(f"Unregistered tool: {name}")
                return True
            return False

    def unregister_sync(self, name: str) -> bool:
        """Synchronous version of unregister."""
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Unregistered tool: {name}")
            return True
        return False

    # Legacy alias mapping: some models output old-style tool names.
    # Aliases are resolved before the main dict lookup.
    _TOOL_ALIASES: Dict[str, str] = {
        "file_read": "read_file",
        "file_write": "write_file",
    }

    def get(self, name: str) -> Optional[ToolDefinition]:
        """
        Get a tool by name.

        Args:
            name: Tool name

        Returns:
            ToolDefinition if found, None otherwise
        """
        # Resolve legacy alias before dict lookup
        name = self._TOOL_ALIASES.get(name, name)
        return self._tools.get(name)

    def get_sandbox_level(self, name: str) -> str:
        """
        Get sandbox level for a tool (P1-6 Fix).

        Args:
            name: Tool name

        Returns:
            Sandbox level: "none", "recommended", or "required"
        """
        tool = self._tools.get(name)
        if tool:
            return tool.sandbox_level
        return SandboxLevel.NONE

    def get_all(self) -> Dict[str, ToolDefinition]:
        """Get all registered tools."""
        return self._tools.copy()

    def list_tools(self, edition: Optional[str] = None) -> List[ToolDefinition]:
        """
        List all available tools, optionally filtered by edition.

        Args:
            edition: Filter by edition (lite/full/both), None for all

        Returns:
            List of ToolDefinition objects
        """
        tools = list(self._tools.values())
        if edition:
            tools = [t for t in tools if t.edition in (edition, "both")]
        return tools

    def to_prompt(self, edition: Optional[str] = None) -> str:
        """
        Generate AI-readable tool list for LLM prompts.

        Args:
            edition: Filter by edition, None for all

        Returns:
            Formatted string describing all available tools
        """
        tools = self.list_tools(edition=edition)

        lines = ["# Available Tools", ""]
        lines.append(f"Total tools available: {len(tools)}")
        lines.append("")

        for tool in sorted(tools, key=lambda t: t.name):
            lines.append(f"## {tool.name}")
            lines.append(f"Description: {tool.description}")
            lines.append(f"Timeout: {tool.timeout}s")
            lines.append(f"Edition: {tool.edition}")

            # Parameters
            if tool.params.get('properties'):
                lines.append("Parameters:")
                for param_name, param_info in tool.params['properties'].items():
                    param_type = param_info.get('type', 'any')
                    required = param_name in tool.params.get('required', [])
                    required_str = "[required]" if required else "[optional]"
                    description = param_info.get('description', '')
                    lines.append(f"  - {param_name}: {param_type} {required_str} - {description}")

            lines.append("")

        return "\n".join(lines)

    def find_similar(self, name: str, threshold: float = 0.6) -> List[Tuple[str, float]]:
        """
        Find tools with similar names using fuzzy matching.

        Args:
            name: Tool name to match
            threshold: Minimum similarity score (0-1)

        Returns:
            List of (tool_name, similarity_score) tuples
        """
        results = []
        for tool_name in self._tools.keys():
            score = SequenceMatcher(None, name.lower(), tool_name.lower()).ratio()
            if score >= threshold:
                results.append((tool_name, score))
        return sorted(results, key=lambda x: x[1], reverse=True)


# =============================================================================
# Tool Executor
# =============================================================================

class ToolExecutor:
    """
    Executes tools based on AI instructions.

    Features:
    - JSON instruction parsing: {"tool": "name", "params": {...}}
    - Natural language fuzzy matching for tool names
    - Force-Verify integration for all executions
    - Timeout handling
    - Error truncation

    Example:
        executor = ToolExecutor(registry)
        result = await executor.dispatch('{"tool": "read_file", "params": {"path": "/tmp/test.txt"}}')
    """

    def __init__(self, registry: Optional[ToolRegistry] = None):
        """
        Initialize tool executor.

        Args:
            registry: ToolRegistry instance (creates singleton if None)
        """
        self.registry = registry or ToolRegistry()

    async def dispatch(self, instruction: str) -> ToolResult:
        """
        Parse and execute a tool instruction from AI.

        Supports multiple instruction formats:
        1. JSON: {"tool": "name", "params": {...}}
        2. Natural language with tool name: "Use read_file to read /tmp/test.txt"
        3. Simple: "read_file /tmp/test.txt"

        Args:
            instruction: AI instruction string

        Returns:
            ToolResult with execution details
        """
        start_time = time.time()

        try:
            # Parse instruction
            tool_name, params = self._parse_instruction(instruction)

            if not tool_name:
                return ToolResult(
                    success=False,
                    error="Could not identify tool from instruction",
                    elapsed_ms=(time.time() - start_time) * 1000,
                )


            # Find tool
            tool = self.registry.get(tool_name)

            # Fuzzy match if exact match not found
            if tool is None:
                similar = self.registry.find_similar(tool_name)
                if similar:
                    matched_name, score = similar[0]
                    logger.info(f"Tool '{tool_name}' not found, matched to '{matched_name}' (score={score:.2f})")
                    tool = self.registry.get(matched_name)
                else:
                    available = [t.name for t in self.registry.list_tools()]
                    return ToolResult(
                        success=False,
                        error=f"Tool '{tool_name}' not found. Available: {available}",
                        elapsed_ms=(time.time() - start_time) * 1000,
                    )

            # Validate parameters
            valid, error_msg = tool.validate_params(params)
            if not valid:
                return ToolResult(
                    success=False,
                    error=f"Parameter validation failed: {error_msg}",
                    elapsed_ms=(time.time() - start_time) * 1000,
                )

            # Check role permission (v2 0d DoD)
            # AI worker dispatches with admin role by default
            permission_result = self._check_role_permission(tool, {'role': 'admin'})
            if not permission_result.success:
                return permission_result

            # Execute tool with Force-Verify
            result = await self._execute_with_verify(tool, params)

            result.elapsed_ms = (time.time() - start_time) * 1000
            return result

        except json.JSONDecodeError as e:
            return ToolResult(
                success=False,
                error=f"Invalid JSON in instruction: {e}",
                elapsed_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return ToolResult(
                success=False,
                error=self._truncate_error(str(e)),
                elapsed_ms=(time.time() - start_time) * 1000,
            )

    def _parse_instruction(self, instruction: str) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Parse AI instruction to extract tool name and parameters.

        Args:
            instruction: Raw instruction string

        Returns:
            Tuple of (tool_name, params_dict)
        """
        instruction = instruction.strip()

        # Try JSON format first
        if instruction.startswith('{'):
            try:
                data = json.loads(instruction)
                tool_name = data.get('tool') or data.get('name')
                params = data.get('params') or data.get('arguments') or {}
                return tool_name, params
            except json.JSONDecodeError:
                logger.debug("tool_registry: instructionJSONparseFailed,attemptnaturallanguageMatch")

        # Natural language patterns
        # Pattern 1: "Use <tool> to ..." or "Use <tool> for ..."
        use_pattern = r'use\s+(\w+)\s+(?:to|for|that)\s+'
        match = re.search(use_pattern, instruction, re.IGNORECASE)
        if match:
            tool_name = match.group(1)
            params = self._extract_params_from_nl(instruction, tool_name)
            return tool_name, params

        # Pattern 2: "<tool> ..." at the start
        first_word = instruction.split()[0] if instruction.split() else ""
        if first_word and first_word.isidentifier():
            # Check if it matches a known tool
            if self.registry.get(first_word):
                params = self._extract_params_from_nl(instruction, first_word)
                return first_word, params

        # Pattern 3: Extract quoted strings as potential paths
        quoted = re.findall(r'["\']([^"\']+)["\']', instruction)
        if quoted:
            # Use the first quoted string as "path" or "content" parameter
            params = {'input': quoted[0]}
            # Try to identify the tool from context
            tool_candidates = ['read_file', 'write_file', 'list_dir', 'shell_exec', 'http_get', 'search_files']
            for candidate in tool_candidates:
                if candidate.replace('_', ' ') in instruction.lower() or candidate in instruction.lower():
                    return candidate, params

        # Default: return instruction as-is for the AI to retry with proper format
        return None, {}

    def _extract_params_from_nl(self, instruction: str, tool_name: str) -> Dict[str, Any]:
        """
        Extract parameters from natural language instruction.

        Args:
            instruction: Full instruction
            tool_name: Identified tool name

        Returns:
            Extracted parameters
        """
        params = {}

        # Extract file paths (quoted or unquoted)
        paths = re.findall(r'(?:file|path)[:\s]+["\']?([^"\'\s,]+)["\']?', instruction, re.IGNORECASE)
        if paths:
            params['path'] = paths[0]

        # Extract URLs
        urls = re.findall(r'https?://[^\s\'"]+', instruction)
        if urls:
            params['url'] = urls[0]

        # Extract quoted content for write operations
        if tool_name in ('write_file', 'shell_exec'):
            quoted = re.findall(r'["\']([^"\']+)["\']', instruction)
            if quoted:
                # For write_file, last quoted string is likely content
                if tool_name == 'write_file':
                    params['content'] = quoted[-1]
                # For shell_exec, first quoted string is likely command
                else:
                    params['cmd'] = quoted[0]

        return params

    async def _execute_with_verify(self, tool: ToolDefinition, params: Dict[str, Any]) -> ToolResult:
        """
        Execute tool with Force-Verify integration.

        Args:
            tool: ToolDefinition to execute
            params: Parameters to pass

        Returns:
            ToolResult with verified output
        """
        start_time = time.time()

        try:
            # Execute with timeout
            if asyncio.iscoroutinefunction(tool.handler):
                result_data = await asyncio.wait_for(
                    tool.handler(**params),
                    timeout=tool.timeout
                )
            else:
                # Run sync functions in executor
                loop = asyncio.get_event_loop()
                result_data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: tool.handler(**params)),
                    timeout=tool.timeout
                )

            # Check if handler returned a dict with 'success' field
            handler_success = True
            handler_error = None
            if isinstance(result_data, dict):
                handler_success = result_data.get('success', True)
                handler_error = result_data.get('error')

            # Force-Verify: Check handler reported success
            verified = result_data is not None and handler_success

            # Additional schema verification if configured
            if verified and tool.verify_level == VerifyLevel.SCHEMA:
                # Basic check: result should be serializable
                try:
                    json.dumps(result_data)
                except (TypeError, ValueError):
                    verified = False
                    result_data = f"[Verify Warning] Result not JSON serializable: {type(result_data)}"

            elapsed_ms = (time.time() - start_time) * 1000

            return ToolResult(
                success=handler_success,
                data=result_data,
                error=handler_error,
                verified=verified,
                elapsed_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Tool '{tool.name}' timed out after {tool.timeout}s",
                verified=False,
                elapsed_ms=(time.time() - start_time) * 1000,
            )
        except TypeError as e:
            # Likely wrong parameters
            return ToolResult(
                success=False,
                error=f"Parameter error: {self._truncate_error(str(e))}",
                verified=False,
                elapsed_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=self._truncate_error(str(e)),
                verified=False,
                elapsed_ms=(time.time() - start_time) * 1000,
            )

    def _truncate_error(self, error: str, max_length: int = 2000) -> str:
        """Truncate error message to prevent log flooding."""
        if len(error) <= max_length:
            return error
        return error[:max_length] + f"... [truncated, total {len(error)} chars]"

    def _check_role_permission(
        self,
        tool: ToolDefinition,
        context: Dict[str, Any]
    ) -> ToolResult:
        """
        Check if caller has permission to use the tool.
        
        Permission rules (v2 0d DoD):
        - If tool.allowed_roles contains "all", allow all callers
        - Otherwise, check if caller's role is in allowed_roles list
        
        Args:
            tool: ToolDefinition to check
            context: Execution context containing caller info
            
        Returns:
            ToolResult - success=True if allowed, error result if denied
        """
        allowed_roles = getattr(tool, 'allowed_roles', ['all'])
        
        # Default role if not specified
        caller_role = context.get('role', 'guest')
        
        # Check permission
        if 'all' in allowed_roles:
            logger.debug(f"[Permission] Tool '{tool.name}' allows all roles")
            return ToolResult(success=True)
        
        if caller_role in allowed_roles:
            logger.debug(f"[Permission] Caller role '{caller_role}' allowed for '{tool.name}'")
            return ToolResult(success=True)
        
        # Permission denied
        logger.warning(
            f"[Permission] DENIED: role '{caller_role}' not in allowed_roles for '{tool.name}'"
        )
        return ToolResult(
            success=False,
            error=f"PERMISSION_DENIED: role '{caller_role}' not allowed to call '{tool.name}'. "
                  f"Allowed roles: {allowed_roles}"
        )


# =============================================================================
# Role Checker Utility
# =============================================================================

class RoleChecker:
    """
    Utility class for role-based access control.
    
    Predefined roles:
    - all: Everyone can access
    - admin: Administrator only
    - operator: Operator and above
    - user: Regular user
    - guest: Guest (most restricted)
    - system: System internal calls
    """
    
    ROLE_HIERARCHY = {
        'system': 100,
        'admin': 90,
        'operator': 70,
        'user': 50,
        'guest': 10,
    }
    
    @classmethod
    def has_permission(
        cls,
        caller_role: str,
        allowed_roles: List[str]
    ) -> bool:
        """
        Check if caller has permission.
        
        Args:
            caller_role: Role of the caller
            allowed_roles: List of allowed roles
            
        Returns:
            True if caller has permission
        """
        if 'all' in allowed_roles:
            return True
        return caller_role in allowed_roles
    
    @classmethod
    def get_minimum_role(
        cls,
        allowed_roles: List[str]
    ) -> Optional[str]:
        """
        Get the minimum role level from allowed list.
        
        Args:
            allowed_roles: List of allowed roles
            
        Returns:
            Minimum role name or None
        """
        if 'all' in allowed_roles:
            return 'guest'
        
        min_level = 999
        min_role = None
        for role in allowed_roles:
            level = cls.ROLE_HIERARCHY.get(role, 0)
            if level < min_level:
                min_level = level
                min_role = role
        return min_role


# =============================================================================
# Global instances (lazy initialization)
# =============================================================================

_global_registry: Optional[ToolRegistry] = None
_global_executor: Optional[ToolExecutor] = None


def get_registry() -> ToolRegistry:
    """Get the global ToolRegistry singleton."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def get_executor() -> ToolExecutor:
    """Get the global ToolExecutor instance."""
    global _global_executor
    if _global_executor is None:
        _global_executor = ToolExecutor(get_registry())
    return _global_executor
