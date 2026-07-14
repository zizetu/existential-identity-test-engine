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
# Original repository: https://github.com/zizetu/EITE-agent
#

"""Response formatting - tool results to human-readable text."""

import json
import re
import logging

logger = logging.getLogger("EITElite.formatter")

def format_error(name: str, error: str) -> str:
    return f"[{name}] error: {error}"

def format_progress(name: str, status: str) -> str:
    return f"[{name}] {status}"

def format_result(name: str, result: dict) -> str:
    """Tool execution result to one-line summary."""
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
        return f"[chat] sent to {target}" if result.get("ok") else f"[chat] send failed: {target}"

    # generic
    try:
        s = json.dumps(result, ensure_ascii=False)
        return s[:16000]
    except Exception:
        return str(result)[:16000]


def sanitize_outbound_reply(content: str) -> str:
    """LIVE 2026-07-09j: strip Telegram-breaking garbage from agent replies.

    Collapses empty fenced code blocks, caps repeated fences, and aborts
    pure code-fence spam so the user never sees hundreds of ```html lines.
    """
    if content is None:
        return ""
    text = str(content).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""

    # Collapse empty fenced blocks: ```lang\\n``` or ```\\n```
    text = re.sub(r"```[a-zA-Z0-9_-]*\s*\n\s*```", "", text)
    # Collapse runs of bare fence lines
    text = re.sub(
        r"(?:^\s*```[a-zA-Z0-9_-]*\s*$\n?){3,}",
        "```\n[code omitted - empty dump]\n```\n",
        text,
        flags=re.M,
    )

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        fence_lines = sum(1 for ln in lines if re.match(r"^\s*```", ln))
        if fence_lines >= 8 and fence_lines / max(len(lines), 1) >= 0.5:
            return (
                "Previous UI/CSS rewrite produced garbage output and was stopped.\n"
                "Say a short order only (e.g. status / stop). Do not continue the CSS dump."
            )
        if len(lines) > 80 and fence_lines >= 20:
            prose = [ln for ln in lines if not re.match(r"^\s*```", ln)]
            if not prose or sum(len(x) for x in prose) < 40:
                return (
                    "Long code-fence spam blocked.\n"
                    "Task incomplete; send a one-line order to continue safely."
                )
            return "\n".join(prose[:40])[:4000]

    if len(text) > 12000:
        text = text[:12000] + "\n\n[truncated - reply too long]"
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text


def format_final_reply(content: str) -> str:
    """STRUCTURED_TABLE_REPLY 2026-07-09h2 + sanitize 2026-07-09j."""
    return sanitize_outbound_reply(_format_final_reply_inner(content))


def _format_final_reply_inner(content: str) -> str:
    """Tables for prose audits only. Never table-ify CSS/JS/code dumps."""
    if content is None:
        return ""
    text = str(content).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""

    has_md_headings = bool(re.search(r"(?m)^#{1,3}\s+\S", text))

    if not has_md_headings and len(text) < 280 and text.count("\n") < 6:
        return text

    if re.search(r"^\|.*\|\s*$", text, re.M) and re.search(r"^\|\s*[-:]+", text, re.M):
        return text

    code_signals = 0
    if "```" in text:
        code_signals += 3
    if re.search(r"(?m)^(function|const|let|var|import |export |class |def |#include)\b", text):
        code_signals += 3
    if re.search(r"(?m)^(\.[\w-]+|#(?!#|\s)[\w-]+)\s*\{", text):
        code_signals += 4
    if re.search(r"(?m)^(:root|@media|@keyframes)\b", text):
        code_signals += 4
    if re.search(r"(?m)^\s*--[\w-]+\s*:", text):
        code_signals += 4
    if text.count("{") >= 3 and text.count("}") >= 3:
        code_signals += 2
    if text.count(";") >= 8:
        code_signals += 2
    if re.search(r"(?m)^(CSSEOF|EOF|cat > |<<\s*'|shell_exec|tool_call)", text):
        code_signals += 3
    if re.search(r"</?(div|span|style|script|html|body)\b", text, re.I):
        code_signals += 2
    lines = [ln for ln in text.splitlines() if ln.strip()]
    short_colon = sum(
        1 for ln in lines if ":" in ln and len(ln) < 120 and not ln.strip().startswith("#")
    )
    if lines and short_colon / max(len(lines), 1) > 0.45 and len(lines) >= 6:
        code_signals += 4
    if code_signals >= 3:
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
                if body.count("{") >= 2 or body.count(";") >= 5 or re.search(r"(?m)^\s*--[\w-]+\s*:", body):
                    return text
                sections.append((title, body[:200].replace("\n", " ")))
            i += 2
        if preamble and not sections:
            sections.append(("Summary", preamble[:200].replace("\n", " ")))
    else:
        bullets = re.findall(r"(?m)^(?:[-*] |\d+[.)] )(.+)$", text)
        kvs = re.findall(
            r"(?m)^((?:Status|State|Result|Module|Item|Task|Node|Service|Error|Identity|Memory|Version|Path|Owner|Channel)[A-Za-z0-9_ ./\-]{0,30})\s*[:=]\s*(.+)$",
            text,
            flags=re.I,
        )
        if len(kvs) >= 3:
            for k, v in kvs[:12]:
                sections.append((k.strip(), v.strip()[:160]))
        elif len(bullets) >= 3:
            for n, b in enumerate(bullets[:12], 1):
                sections.append((f"Item {n}", b.strip()[:160]))
        else:
            return text

    if len(sections) < 2:
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
