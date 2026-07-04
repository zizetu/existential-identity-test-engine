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

# provenance:ticalasi-zzt-2026​
"""Permission checker with 5-tier mode system for tool execution gating.

Modeled after MiMo Code's PermissionChecker. Provides centralized,
mode-driven tool gating that sits ahead of verification, constitution,
and decision-engine checks.

Modes:
  - default:           Allow reads, ask for destructive writes
  - acceptEdits:       Auto-approve file read/write/edit/search tools
  - bypassPermissions: Allow everything unconditionally
  - plan:              Read-only mode; block all write/execute tools
  - auto:              Rule-based with explicit allow/deny lists

Write approval gate:
  - write_approval (bool): When True, only write/execute tools need
    confirmation. Reads pass freely. Off by default.
"""

from __future__ import annotations

from typing import Dict, List, Optional
from enum import Enum


class PermissionMode(str, Enum):
    """Enumeration of the five permission gating modes for tool execution control.

    Each mode represents a different security posture: DEFAULT requires
    confirmation for destructive operations, ACCEPT_EDITS auto-approves file
    modifications, BYPASS allows everything, PLAN restricts to read-only tools,
    and AUTO uses explicit allow/deny lists for fine-grained control.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypassPermissions"
    PLAN = "plan"
    AUTO = "auto"


# Read-only tools allowed in plan mode
PLAN_READ_ONLY_TOOLS: set[str] = {
    "file_read", "file_search", "list_dir",
    "memory_load", "memory_search", "check_self",
    "web_fetch", "vigil_status", "get_subagent_result",
}

# Edit tools auto-approved in acceptEdits mode
ACCEPT_EDITS_TOOLS: set[str] = {
    "file_read", "file_write", "file_patch",
    "file_search", "list_dir", "web_fetch",
    # NOTE: bash_execute intentionally excluded — shell access requires
    # explicit user confirmation even in acceptEdits mode.
}

# Always-allowed tools regardless of mode (except plan overrides)
ALWAYS_ALLOWED: set[str] = {"check_self", "memory_load", "memory_search", "vigil_status"}

# Write tools that trigger write_approval gate when enabled
WRITE_TOOLS: set[str] = {
    "file_write", "file_patch", "bash",
    "shell_exec", "memory_save", "state_save",
    "chat_send", "delegate_task", "end_task",
}


class PermissionChecker:
    """Centralized permission gate for tool execution.

    Evaluates each tool call against the active mode and rule sets.
    Returns (allowed, reason) tuple consumed by message_handler's
    tool-execution loop.

    Usage:
        checker = PermissionChecker(mode=PermissionMode.DEFAULT)
        allowed, reason = checker.can_use_tool("file_write")
        if not allowed:
            # block the tool call
    """

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        allowed_tools: Optional[List[str]] = None,
        denied_tools: Optional[List[str]] = None,
        write_approval: bool = False,
    ):
        """Initialize a PermissionChecker with a mode and optional allow/deny lists.

        Sets up the permission checker with a default or specified permission
        mode, along with explicit lists of tools that are always allowed or
        always denied regardless of mode-specific rules. The allow and deny
        lists are primarily used in AUTO mode but affect all modes.

        Args:
            mode: The initial PermissionMode to use for tool gating decisions.
                Defaults to PermissionMode.DEFAULT, which allows reads and
                requires confirmation for destructive writes.
            allowed_tools: Optional list of tool name strings to add to the
                explicit allow set. These tools bypass mode-based restrictions.
            denied_tools: Optional list of tool name strings to add to the
                explicit deny set. These tools are blocked in all modes,
                overriding even the always-allowed set and bypass mode.
            write_approval: Optional boolean gate. When True, only write tools
                require confirmation. Reads pass freely. Default False.
        """
        self.mode = mode
        self.allowed_tools: set[str] = set(allowed_tools or [])
        self.denied_tools: set[str] = set(denied_tools or [])
        self.write_approval: bool = write_approval

    def can_use_tool(self, tool_name: str) -> tuple[bool, str]:
        """Check whether a tool is allowed under the current permission mode and rule sets.

        Evaluates the tool against explicit deny list (always blocks), explicit
        allow list (always permits), the always-allowed set, and then the
        active mode's specific rules. In DEFAULT mode, destructive tools
        return allowed=True with a reason indicating confirmation is needed.

        Args:
            tool_name: The string name of the tool being checked for execution
                permission, such as "file_write" or "bash_execute".

        Returns:
            A tuple of (allowed: bool, reason: str). When allowed is False the
            tool call should be blocked entirely. When allowed is True, the
            reason string describes why it was permitted and may indicate that
            user confirmation is still required in DEFAULT mode.
        """
        # --- Explicit deny overrides everything ---
        if tool_name in self.denied_tools:
            return False, f"Tool '{tool_name}' is denied by policy"

        # --- Explicit allow ---
        if tool_name in self.allowed_tools:
            return True, "Explicitly allowed"

        # --- Always-allowed regardless of mode ---
        if tool_name in ALWAYS_ALLOWED:
            return True, "Always allowed"

        # --- Mode: bypassPermissions ---
        if self.mode == PermissionMode.BYPASS:
            return True, "Bypass mode"

        # --- Mode: plan (read-only) ---
        if self.mode == PermissionMode.PLAN:
            if tool_name in PLAN_READ_ONLY_TOOLS:
                return True, "Read-only tool in plan mode"
            return False, f"Tool '{tool_name}' blocked in plan mode (read-only)"

        # --- Mode: acceptEdits ---
        if self.mode == PermissionMode.ACCEPT_EDITS:
            if tool_name in ACCEPT_EDITS_TOOLS:
                return True, "Auto-approved in acceptEdits mode"
            # Fall through to default behavior for other tools

        # --- Mode: auto ---
        if self.mode == PermissionMode.AUTO:
            # In auto mode, allowed_tools/denied_tools are the primary gate.
            # If neither list matches, fall through to default behavior.
            pass

        # --- DEFAULT mode (and fallback for acceptEdits/auto unmatched tools) ---
        if tool_name in PLAN_READ_ONLY_TOOLS:
            return True, "Default: read-only tool allowed"

        # Write approval gate: when enabled, block writes but pass reads freely
        if self.write_approval and tool_name in WRITE_TOOLS:
            return True, "Write approval required"
        if self.write_approval:
            return True, "Read tool allowed (write_approval gate)"

        # Destructive tools: allow but flag for confirmation
        return True, "Default: destructive tool requires confirmation"

    def can_use_tool_strict(self, tool_name: str) -> bool:
        """Return a strict boolean permission check with no confirmation ambiguity.

        Unlike can_use_tool, this method collapses the "requires confirmation"
        case in DEFAULT mode into a simple False, meaning only unconditionally
        permitted tools return True. Useful for automated pipelines where
        human-in-the-loop confirmation is not available.

        Args:
            tool_name: The string name of the tool to check for unconditional
                execution permission.

        Returns:
            True only if the tool is unconditionally permitted under the
            current mode (not merely allowed-with-confirmation), False otherwise.
        """
        allowed, _reason = self.can_use_tool(tool_name)
        # In strict mode, "requires confirmation" means denied
        if allowed and "requires confirmation" in _reason:
            return False
        return allowed

    @property
    def mode_value(self) -> str:
        """Return the string value of the currently active permission mode.

        Provides direct access to the mode's string representation (e.g.,
        "default", "acceptEdits", "bypassPermissions") without needing to
        access the underlying enum member, useful for serialization and logging.

        Returns:
            A string representing the current PermissionMode value.
        """
        return self.mode.value

    def set_mode(self, mode: PermissionMode) -> None:
        """Change the active permission mode at runtime without recreating the checker.

        Switches the checker's behavior to a new PermissionMode, immediately
        affecting all subsequent calls to can_use_tool and can_use_tool_strict.
        Any existing allow/deny lists are preserved across mode changes.

        Args:
            mode: The new PermissionMode enum value to activate for future
                tool permission checks.
        """
        self.mode = mode

    def add_allowed_tool(self, tool_name: str) -> None:
        """Add a tool name to the explicit allow list for unconditional permission.

        Once added, the tool will always be permitted regardless of the active
        mode, unless it also appears in the deny list (deny always wins).
        This is idempotent - adding a tool already in the set has no effect.

        Args:
            tool_name: The string name of the tool to add to the explicit
                allow set, e.g., "file_write" or "web_fetch".
        """
        self.allowed_tools.add(tool_name)

    def add_denied_tool(self, tool_name: str) -> None:
        """Add a tool name to the explicit deny list to block it unconditionally.

        Denied tools are blocked in all modes, including bypassPermissions,
        overriding the always-allowed set and any allow list entry. This is
        idempotent - adding a tool already in the deny set has no effect.

        Args:
            tool_name: The string name of the tool to add to the explicit
                deny set, e.g., "bash_execute" or "file_delete".
        """
        self.denied_tools.add(tool_name)

    def remove_allowed_tool(self, tool_name: str) -> None:
        """Remove a tool name from the explicit allow list if it is present.

        After removal, the tool will once again be subject to normal mode-based
        permission evaluation. If the tool is not in the allow set, calling
        this method has no effect and does not raise an error.

        Args:
            tool_name: The string name of the tool to remove from the explicit
                allow set.
        """
        self.allowed_tools.discard(tool_name)

    def remove_denied_tool(self, tool_name: str) -> None:
        """Remove a tool name from the explicit deny list if it is present.

        After removal, the tool is no longer unconditionally blocked and will
        be evaluated according to the active permission mode and other rule
        sets. If the tool is not in the deny set, this method has no effect.

        Args:
            tool_name: The string name of the tool to remove from the explicit
                deny set.
        """
        self.denied_tools.discard(tool_name)

    def to_dict(self) -> dict:
        """Serialize the current permission checker configuration to a dictionary.

        Produces a JSON-serializable dictionary containing the active mode
        string and sorted lists of allowed and denied tool names, suitable
        for persisting the checker's state to a configuration file or sending
        over the network for remote inspection.

        Returns:
            A dict with keys "mode" (str), "allowed_tools" (list[str]),
            and "denied_tools" (list[str]).
        """
        return {
            "mode": self.mode.value,
            "allowed_tools": sorted(self.allowed_tools),
            "denied_tools": sorted(self.denied_tools),
        }

    @classmethod
    def from_dict(cls, data: dict) -> PermissionChecker:
        """Instantiate a fully configured PermissionChecker from a serialized dictionary.

        Reconstructs a PermissionChecker with the mode and allow/deny lists
        stored in the given dictionary, typically produced by to_dict(). Falls
        back to DEFAULT mode if the stored mode string is unrecognized to
        ensure graceful degradation with outdated or corrupted configurations.

        Args:
            data: A dictionary with optional keys "mode" (str), "allowed_tools"
                (list[str]), and "denied_tools" (list[str]), matching the
                format produced by to_dict().

        Returns:
            A new PermissionChecker instance configured according to the
            provided dictionary.
        """
        mode_str = data.get("mode", "default")
        try:
            mode = PermissionMode(mode_str)
        except ValueError:
            mode = PermissionMode.DEFAULT
        return cls(
            mode=mode,
            allowed_tools=data.get("allowed_tools", []),
            denied_tools=data.get("denied_tools", []),
        )
