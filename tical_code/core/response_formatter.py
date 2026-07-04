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

"""EITE Response Formatting - tool results to human-readable text.

Used by the EITE evaluation framework to format tool execution results
into compact, readable strings for LLM consumption and logging.
"""

import json
import logging

logger = logging.getLogger("eite.formatter")


def format_error(name: str, error: str) -> str:
    """Format an error message for a named operation."""
    return f"[{name}] error: {error}"


def format_progress(name: str, status: str) -> str:
    """Format a progress status message for a named operation."""
    return f"[{name}] {status}"


def format_result(name: str, result: dict) -> str:
    """Tool execution result to one-line summary for LLM consumption.

    Args:
        name: Tool name
        result: Tool result dictionary

    Returns:
        Compact string representation of the result
    """
    if not result:
        return f"[{name}] no result"

    if "error" in result:
        return f"[{name}] {result['error']}"

    # bash
    if name == "bash" and "exit_code" in result:
        out = result.get("stdout", "")
        err = result.get("stderr", "")
        code = result.get("exit_code", -1)
        if code == 0 and out:
            return out[:16000]
        elif code != 0:
            return f"[bash] exit={code} {err[:500]}"
        return "[bash] done (no output)"

    # file_read
    if name == "file_read" and "content" in result:
        return f"[file] {result['path']}: {result['content'][:16000]}"

    # file_write
    if name == "file_write":
        return f"[file] written to {result.get('path', '?')}" if result.get("ok") else "[file] write failed"

    # memory
    if name == "memory_save":
        return f"[memory] saved key={result.get('key', '?')}"

    if name == "memory_load":
        entries = result.get("entries", {})
        if entries:
            return "[memory] " + "; ".join(
                f"{k}: {v.get('value', '')[:30]}"
                for k, v in list(entries.items())[:5]
            )
        return "[memory] no entries"

    # state
    if name == "state_save":
        return f"[state] saved {result.get('key', '?')}" if result.get("ok") else "[state] save failed"

    # chat_send
    if name == "chat_send":
        target = result.get("target", "?")
        return f"[chat] sent to {target}" if result.get("ok") else "[chat] send failed"

    return json.dumps(result, ensure_ascii=False)[:16000]
