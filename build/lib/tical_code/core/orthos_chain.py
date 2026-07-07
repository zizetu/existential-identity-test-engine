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

# provenance:ticalasi-zzt-2026
#!/usr/bin/env python3
"""Orthos Chain v4 - Auto-discovering model router with v5 tool support.

Called from model_failover._call_single when the active provider is ollama-based.
Queries Ollama /api/tags at startup to auto-discover models by name patterns,
so adding/renaming models requires no code changes.

Model name patterns (case-insensitive):
  - "chain" + "model" = classifier (safety gate + intent routing)
  - "chain" + "v5"    = reasoner (code/chain reasoning, prompt-based tool calls)
  - "chain" + "v4"    = generator (text generation, fallback for chat)
  - "vision"          = vision model
  - "diagnosis"/"bench" = system diagnosis
  - "ui"              = UI generation model
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("EITElite.orthos_chain")

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
DEEPSEEK_BASE = "https://api.deepseek.com/v1/chat/completions"

# Discovered model names (populated by _discover_models)
CHAIN_CLASSIFIER: str = ""
CHAIN_REASONER: str = ""     # v5 - prompt-based tool calling
CHAIN_GENERATOR: str = ""    # v4 - text generation
BENCH_DX_MODEL: str = ""
VISION_MODEL: str = ""
UI_MODEL: str = ""
DEEPSEEK_MODEL: str = "deepseek-chat"
_models_last_refresh: float = 0
_MODELS_REFRESH_TTL: float = 300.0

MAX_TOOLS_3B = 5
MAX_TOOL_PARAMS_3B = 12
CONTEXT_MAX_TURNS = 4
TOOL_CACHE_TTL = 60
MAX_CACHE = 500
OLLAMA_TIMEOUT = 60
DEEPSEEK_TIMEOUT = 30

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not DEEPSEEK_KEY:
    logger.warning("DEEPSEEK_API_KEY not set - chain will use local models only")

V5_SYSTEM_PROMPT = """You are a JSON output generator. Output ONLY a valid JSON object with name and arguments keys."""
ANTI_LABEL_PREFIX = """\n[INSTRUCTION] You are a code generation assistant.\nCRITICAL: NEVER output labels like SAFE, CODING, or any pipe format.\nOutput real code or text only."""

V4_SYSTEM_PROMPT = """You MUST output complete code or natural language text.
CRITICAL: NEVER output SAFE, UNSAFE, CODING, CHAT, ANALYSIS or any classification label.
NEVER use pipe-separated format. Always generate real content."""

_tool_cache: Dict[str, Dict] = {}


def _discover_models(force: bool = False) -> bool:
    """Query Ollama /api/tags and match models to roles by name patterns.

    Returns True if all core models (classifier + at least one generator) were found.
    """
    global CHAIN_CLASSIFIER, CHAIN_REASONER, CHAIN_GENERATOR
    global BENCH_DX_MODEL, VISION_MODEL, UI_MODEL
    global _models_last_refresh

    now = time.time()
    if not force and now - _models_last_refresh < _MODELS_REFRESH_TTL:
        return bool(CHAIN_CLASSIFIER and (CHAIN_REASONER or CHAIN_GENERATOR))

    req = urllib.request.Request(OLLAMA_TAGS_URL)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("[OrthosChain] Failed to discover models: %s", e)
        return bool(CHAIN_CLASSIFIER and (CHAIN_REASONER or CHAIN_GENERATOR))

    models = data.get("models", [])
    names = []
    for m in models:
        name = m.get("name", "")
        if ":" in name:
            name = name.split(":")[0]
        names.append(name)

    logger.info("[OrthosChain] Discovered %d Ollama models: %s",
                len(names), ", ".join(names))

    # Classifier: contains "chain" AND "model"
    for n in names:
        nl = n.lower()
        if "chain" in nl and "model" in nl:
            CHAIN_CLASSIFIER = n
            break

    # Reasoner (v5): contains "chain" AND "v5"
    for n in names:
        nl = n.lower()
        if "chain" in nl and "v5" in nl:
            CHAIN_REASONER = n
            break

    # Generator (v4): contains "chain" AND "v4"
    for n in names:
        nl = n.lower()
        if "chain" in nl and "v4" in nl:
            CHAIN_GENERATOR = n
            break

    # Fallback generator: any remaining "chain" model not used above
    if not CHAIN_GENERATOR:
        for n in names:
            nl = n.lower()
            if "chain" in nl and n not in (CHAIN_CLASSIFIER, CHAIN_REASONER) and "vision" not in nl:
                CHAIN_GENERATOR = n
                break

    # Vision
    for n in names:
        if "vision" in n.lower():
            VISION_MODEL = n
            break

    # Diagnosis
    for n in names:
        nl = n.lower()
        if "diagnosis" in nl or "bench" in nl:
            BENCH_DX_MODEL = n
            break

    # UI
    for n in names:
        if "ui" in n.lower() and n not in (CHAIN_CLASSIFIER, CHAIN_REASONER,
                                           CHAIN_GENERATOR, VISION_MODEL, BENCH_DX_MODEL):
            UI_MODEL = n
            break

    _models_last_refresh = now

    all_found = bool(CHAIN_CLASSIFIER and (CHAIN_REASONER or CHAIN_GENERATOR))
    logger.info("[OrthosChain] Model routing: classifier=%s reasoner=%s generator=%s "
                "vision=%s diagnosis=%s ui=%s",
                CHAIN_CLASSIFIER, CHAIN_REASONER or "-", CHAIN_GENERATOR or "-",
                VISION_MODEL or "-", BENCH_DX_MODEL or "-", UI_MODEL or "-")
    return all_found


def _ensure_models():
    """Ensure models are discovered, retrying on first call."""
    if not CHAIN_CLASSIFIER or not (CHAIN_REASONER or CHAIN_GENERATOR):
        _discover_models(force=True)


def _cache_key(messages: List[Dict], tools: Optional[List]) -> str:
    last = messages[-1] if messages else {}
    return hashlib.md5(
        json.dumps({"msg": last, "tools": tools}, sort_keys=True).encode()
    ).hexdigest()


def _get_cached(key: str) -> Optional[Dict]:
    entry = _tool_cache.get(key)
    if entry and time.time() - entry["ts"] < TOOL_CACHE_TTL:
        return entry["result"]
    return None


def _set_cache(key: str, result: Dict):
    _tool_cache[key] = {"result": result, "ts": time.time()}
    if len(_tool_cache) > MAX_CACHE:
        try:
            sorted_keys = sorted(_tool_cache, key=lambda k: _tool_cache[k]["ts"])
            for old_key in sorted_keys[:len(_tool_cache) - MAX_CACHE]:
                del _tool_cache[old_key]
        except Exception:
            pass


def _ollama_chat(model: str, msg: str, max_tokens: int = 50,
                 temperature: float = 0.1, timeout: int = OLLAMA_TIMEOUT,
                 system: str = "") -> Tuple[str, int]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": msg})
    body = json.dumps({
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_CHAT_URL, data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode())
        content = data.get("message", {}).get("content", "").strip()
        tokens = data.get("eval_count", 0)
        return content, tokens
    except Exception as e:
        logger.error("Ollama call to %s failed: %s", model, e)
        return f"ERROR: {e}", 0


def _classify_with_chain_model(user_msg: str, context: str) -> Tuple[str, str, int]:
    """Single call to the classifier model for unified safety + intent classification.

    Truncates input to CLASSIFIER_MAX_CHARS to avoid context overflow
    (classifier is a 3B Q8_0 model with ~32K context; audit prompts can be 80K+).
    """
    CLASSIFIER_MAX_CHARS = 2000
    _ensure_models()
    if not CHAIN_CLASSIFIER:
        logger.error("[OrthosChain] No classifier model available")
        return "SAFE", "CHAT", 0
    prompt = f"Context:\n{context}\n\nUser: {user_msg}" if context else f"User: {user_msg}"
    if len(prompt) > CLASSIFIER_MAX_CHARS:
        prompt = prompt[:CLASSIFIER_MAX_CHARS] + "\n...[truncated]"
        logger.debug("[OrthosChain] Classifier input truncated to %d chars", CLASSIFIER_MAX_CHARS)
    resp, tokens = _ollama_chat(CHAIN_CLASSIFIER, prompt, max_tokens=50, temperature=0.1)

    upper = resp.upper()

    safety = "SAFE"
    for label in ["UNSAFE", "SUSPICIOUS"]:
        if label in upper:
            safety = label
            break

    intent = "CHAT"
    for label in ["CODING", "ANALYSIS", "VISION", "SYSTEM"]:
        if label in upper:
            intent = label
            break

    logger.debug("[OrthosChain] classify: safety=%s intent=%s raw=%s", safety, intent, resp[:80])
    return safety, intent, tokens


def _deepseek_chat(msg: str, system: str = "", max_tokens: int = 500,
                   temperature: float = 0.3, timeout: int = DEEPSEEK_TIMEOUT,
                   tools: Optional[List] = None) -> Tuple[str, int, List]:
    if not DEEPSEEK_KEY:
        return "ERROR: DEEPSEEK_API_KEY not set", 0, []
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": msg})
    body: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        DEEPSEEK_BASE, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + DEEPSEEK_KEY},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode())
        msg_data = data.get("choices", [{}])[0].get("message", {})
        content = msg_data.get("content", "")
        tool_calls_raw = msg_data.get("tool_calls", [])
        tokens = data.get("usage", {}).get("completion_tokens", 0)
        ds_tool_calls = []
        for tc in tool_calls_raw:
            func = tc.get("function", {})
            args_raw = func.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {"raw": args_raw}
            ds_tool_calls.append({
                "id": tc.get("id", "ds_" + func.get("name", "unknown")),
                "name": func.get("name", "unknown"),
                "args": args,
            })
        return content.strip(), tokens, ds_tool_calls
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        logger.error("DeepSeek call failed (HTTP %d): %s | body=%s",
                     e.code, e.reason, body[:300])
        return f"ERROR: HTTP {e.code}: {e.reason}", 0, []
    except Exception as e:
        logger.error("DeepSeek call failed: %s", e)
        return f"ERROR: {e}", 0, []


def _extract_user_text(messages: List[Dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                return " ".join(text_parts)
    return ""


def _build_context(messages: List[Dict], max_turns: int = CONTEXT_MAX_TURNS) -> str:
    context_parts = []
    count = 0
    for m in reversed(messages):
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, str) and content.strip():
            context_parts.insert(0, f"{role}: {content[:500]}")
            count += 1
            if count >= max_turns * 2:
                break
    return "\n".join(context_parts)


def _tools_complexity_check(tools: List[Dict]) -> Tuple[bool, str]:
    if not tools:
        return True, "no_tools"
    if len(tools) > MAX_TOOLS_3B:
        return False, f"too_many_tools({len(tools)}>{MAX_TOOLS_3B})"
    total_params = 0
    for t in tools:
        func = t.get("function", t) if isinstance(t, dict) else t
        params = func.get("parameters", {}).get("properties", {})
        total_params += len(params)
        for pname, pdef in params.items():
            ptype = pdef.get("type", "string")
            if ptype in ("array", "object"):
                total_params += 3
            if ptype == "string" and pdef.get("enum"):
                total_params += 1
    if total_params > MAX_TOOL_PARAMS_3B:
        return False, f"too_many_params({total_params}>{MAX_TOOL_PARAMS_3B})"
    return True, f"simple({len(tools)}tools,{total_params}params)"


def _build_tool_prompt(tools: List[Dict], user_msg: str) -> str:
    tool_desc_lines = []
    for i, t in enumerate(tools):
        func = t.get("function", t) if isinstance(t, dict) else t
        name = func.get("name", f"tool_{i}")
        desc = func.get("description", "")
        params = func.get("parameters", {}).get("properties", {})
        req_list = func.get("parameters", {}).get("required", [])
        param_list = []
        for pname, pdef in params.items():
            ptype = pdef.get("type", "string")
            req = " (required)" if pname in req_list else ""
            penum = pdef.get("enum")
            enum_s = f" Choices: {penum}" if penum else ""
            param_list.append(f"  {pname}: {ptype}{req}{enum_s}")
        pl = "\n".join(param_list)
        tool_desc_lines.append(f"Function: {name}\n  {desc}\n{pl}")
    tools_text = "\n\n".join(tool_desc_lines)
    prompt = f"""Select a function and output ONLY a JSON object.

Available:
{tools_text}

User: {user_msg}

Output JSON: {{"name": "function_name", "arguments": {{"param1": "value1"}}}}
"""
    return prompt


def _parse_tool_call_response(raw: str) -> Optional[Dict]:
    raw = raw.strip()
    code_block = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', raw, re.DOTALL)
    if code_block:
        raw = code_block.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and ("name" in parsed or "function" in parsed):
            name = parsed.get("name") or parsed.get("function")
            arguments = parsed.get("arguments") or parsed.get("params") or parsed.get("parameters") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {"raw": arguments}
            if name:
                return {"name": str(name), "arguments": arguments, "raw_response": raw}
    except json.JSONDecodeError:
        pass
    brace_depth = 0
    json_start = -1
    for i, ch in enumerate(raw):
        if ch == '{':
            if brace_depth == 0:
                json_start = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and json_start >= 0:
                json_candidate = raw[json_start:i+1]
                try:
                    parsed = json.loads(json_candidate)
                    if isinstance(parsed, dict):
                        name = parsed.get("name") or parsed.get("function")
                        arguments = parsed.get("arguments") or parsed.get("params") or parsed.get("parameters") or {}
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (json.JSONDecodeError, TypeError):
                                arguments = {"raw": arguments}
                        if name:
                            return {"name": str(name), "arguments": arguments, "raw_response": raw}
                except json.JSONDecodeError:
                    pass
    return None


def _prompt_based_tool_call(user_msg: str, tools: List[Dict],
                            model: str) -> Tuple[Optional[Dict], int, str]:
    prompt = _build_tool_prompt(tools, user_msg)
    tool_system = "You are a precise JSON output generator. Always respond with exactly one valid JSON object, nothing else."
    raw, tokens = _ollama_chat(model, prompt, max_tokens=300, temperature=0.1,
                               system=tool_system)
    parsed = _parse_tool_call_response(raw)
    debug = f"3B_tool_call: model={model} parsed={'yes' if parsed else 'no'} raw={raw[:80]}"
    return parsed, tokens, debug


def chain_call(messages: List[Dict], max_tokens: int = 1000,
               temperature: float = 0.3, tools: Optional[List] = None,
               preferred_family: Optional[str] = None,
               deepseek_key: Optional[str] = None) -> Dict[str, Any]:
    """Main entry point for Orthos Chain v4.

    Called from model_failover._call_single when provider is ollama/local.
    Pipeline:
      1. Auto-discover models from Ollama /api/tags
      2. orthos-chain-model: unified safety + intent
      3. Route: v5 for simple tools, DeepSeek for complex/security, v4 for chat
    """
    _ensure_models()

    if tools:
        ckey = _cache_key(messages, tools)
        cached = _get_cached(ckey)
        if cached:
            logger.info("[OrthosChain] cache hit for tool call")
            return cached

    user_msg = _extract_user_text(messages)
    if not user_msg:
        return {"content": "No user message found.", "tool_calls": [],
                "provider": "orthos-chain", "model": CHAIN_CLASSIFIER,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    chain_log = []
    total_tokens = 0

    # Step 1: Unified classification
    context = _build_context(messages, max_turns=2)
    safety, intent, cls_tokens = _classify_with_chain_model(user_msg, context)
    total_tokens += cls_tokens
    chain_log.append(f"CLASSIFY={safety}/{intent}")

    # Safety gate
    if safety == "UNSAFE":
        return {"content": "[BLOCKED] Content flagged as unsafe by chain-model.",
                "tool_calls": [],
                "provider": "orthos-chain", "model": CHAIN_CLASSIFIER,
                "usage": {"prompt_tokens": 0, "completion_tokens": total_tokens},
                "chain": chain_log}

    if safety == "SUSPICIOUS":
        return {"content": "[SUSPICIOUS] Content flagged as suspicious by chain-model.",
                "tool_calls": [],
                "provider": "orthos-chain", "model": CHAIN_CLASSIFIER,
                "usage": {"prompt_tokens": 0, "completion_tokens": total_tokens},
                "chain": chain_log}

    # Step 2: Route to handler
    if tools:
        is_simple, reason = _tools_complexity_check(tools)
        chain_log.append(f"TOOLS={reason}")

        if is_simple and CHAIN_REASONER:
            # Try v5 for simple tool calls (prompt-based JSON)
            parsed, tc_tokens, debug = _prompt_based_tool_call(user_msg, tools, CHAIN_REASONER)
            total_tokens += tc_tokens
            if parsed:
                chain_log.append(f"V5_TOOL={parsed['name']}")
                logger.info("[OrthosChain] v5 tool call: %s", debug)
                result = {
                    "content": f"Tool call: {parsed['name']}",
                    "tool_calls": [parsed],
                    "provider": "orthos-chain", "model": CHAIN_REASONER,
                    "usage": {"prompt_tokens": 0, "completion_tokens": total_tokens,
                              "chain_tokens": total_tokens, "executor": "V5"},
                    "chain": chain_log,
                }
                _set_cache(_cache_key(messages, tools), result)
                return result
            chain_log.append("V5_FAILED_FALLBACK_TO_DS")

        # Complex tools or v5 failure: fall back to DeepSeek
        ds_system = "You are a helpful assistant with access to tools."
        if intent == "CODING":
            ds_system = "You are a precise coding assistant with tools."
        elif intent == "ANALYSIS":
            ds_system = "You are a precise analysis AI with tools."
        handler_response, handler_tokens, ds_tool_calls = _deepseek_chat(
            user_msg, system=ds_system, max_tokens=max_tokens, temperature=temperature,
            tools=tools)
        total_tokens += handler_tokens
        chain_log.append("DEEPSEEK_TOOL")
        result = {
            "content": handler_response, "tool_calls": ds_tool_calls,
            "provider": "orthos-chain", "model": DEEPSEEK_MODEL,
            "usage": {"prompt_tokens": 0, "completion_tokens": total_tokens,
                      "chain_tokens": total_tokens, "executor": "DEEPSEEK_API"},
            "chain": chain_log,
        }
        _set_cache(_cache_key(messages, tools), result)
        return result

    # No tools: route by intent
    security_kw = ["SECURITY", "VULNERAB", "AUDIT", "REVIEW", "REFACTOR",
                   "FIX", "SAFE", "UNSAFE"]
    is_security = any(w in user_msg.upper() for w in security_kw)
    handler_response = ""
    handler_tokens = 0
    handler_model = ""

    if intent == "CODING" or intent == "SYSTEM":
        if is_security and DEEPSEEK_KEY:
            handler_model = DEEPSEEK_MODEL
            handler_response, handler_tokens, _ = _deepseek_chat(
                user_msg, "You are a precise security code review assistant.",
                max_tokens, temperature)
        else:
            handler_model = CHAIN_GENERATOR or CHAIN_REASONER
            if handler_model:
                handler_response, handler_tokens = _ollama_chat(
                    handler_model, user_msg,
                    min(max_tokens, 400), temperature,
                    system=V4_SYSTEM_PROMPT)
    elif intent == "ANALYSIS":
        handler_model = DEEPSEEK_MODEL
        handler_response, handler_tokens, _ = _deepseek_chat(
            user_msg, "You are a precise analysis AI.", max_tokens, temperature)
    elif intent == "VISION":
        handler_model = VISION_MODEL or ""
        handler_response = f"[{handler_model}: requires image input]" if handler_model else "[VISION: no vision model available]"
    else:
        if is_security and DEEPSEEK_KEY:
            handler_model = DEEPSEEK_MODEL
            handler_response, handler_tokens, _ = _deepseek_chat(
                user_msg, "You are a helpful assistant.", max_tokens, temperature)
        else:
            handler_model = CHAIN_GENERATOR or CHAIN_REASONER
            if handler_model:
                handler_response, handler_tokens = _ollama_chat(
                    handler_model, user_msg,
                    min(max_tokens, 200), 0.5,
                    system=V4_SYSTEM_PROMPT)

    total_tokens += handler_tokens
    executor = "V4" if CHAIN_GENERATOR and CHAIN_GENERATOR in handler_model else "DEEPSEEK_API"
    chain_log.append("HANDLER=" + handler_model)

    logger.info("[OrthosChain] %s: %s -> %s (tokens=%d)",
                executor, CHAIN_CLASSIFIER, handler_model or "none", total_tokens)

    return {
        "content": handler_response or "(empty response)",
        "tool_calls": [],
        "provider": "orthos-chain",
        "model": handler_model or CHAIN_CLASSIFIER,
        "usage": {"prompt_tokens": 0, "completion_tokens": total_tokens,
                  "chain_tokens": total_tokens, "executor": executor},
        "chain": chain_log,
    }
