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
import re
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


def format_final_reply(content: str) -> str:
    """STRUCTURED_TABLE_REPLY 2026-07-09f: long answers become scannable tables.

    Short replies: whitespace normalize only.
    Long / multi-section replies: prepend a Category|Content table derived
    from headings, bullets, or key:value lines. Does not invent facts.
    """
    if content is None:
        return ""
    text = str(content).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""
    if len(text) < 280 and text.count("\n") < 6:
        return text
    if re.search(r"^\|.*\|\s*$", text, re.M) and re.search(r"^\|\s*[-:]+", text, re.M):
        return text

    sections = []
    parts = re.split(r"(?m)^(#{1,3}\s+.+)$", text)
    if len(parts) > 1:
        preamble = parts[0].strip()
        i = 1
        while i < len(parts) - 1:
            title = re.sub(r"^#{1,3}\s+", "", parts[i]).strip()
            body = parts[i + 1].strip()
            if title:
                sections.append((title, body[:200].replace("\n", " ")))
            i += 2
        if preamble and not sections:
            sections.append(("Summary", preamble[:200].replace("\n", " ")))
    else:
        bullets = re.findall(r"(?m)^(?:[-*] |\d+[.)] )(.+)$", text)
        kvs = re.findall(r"(?m)^([A-Za-z0-9_ ./\-]{2,40})\s*[:=]\s*(.+)$", text)
        if len(kvs) >= 3:
            for k, v in kvs[:12]:
                sections.append((k.strip(), v.strip()[:160]))
        elif len(bullets) >= 3:
            for n, b in enumerate(bullets[:12], 1):
                sections.append((f"Item {n}", b.strip()[:160]))
        else:
            first = text.split("\n\n", 1)[0].replace("\n", " ")[:200]
            sections.append(("Summary", first))
            if len(text) > 400:
                sections.append(("Detail", f"{len(text)} chars — see body below"))

    if not sections:
        return text
    rows = ["| Category | Content |", "|---|---|"]
    for title, body in sections[:15]:
        title_c = title.replace("|", "\\|")[:40]
        body_c = body.replace("|", "\\|")[:160]
        rows.append(f"| {title_c} | {body_c} |")
    table = "\n".join(rows)
    if text.startswith("| Category |"):
        return text
    return f"{table}\n\n{text}"




