"""Real Model Agent - connecting to EITElite LLM infrastructure

Supports 3 backends:
1. worker mode: directly calls call_ai_raw (inside worker process)
2. MiMo API: via MIMO_API_KEY env var
3. OpenAI-compatible: via OPENAI_API_KEY + OPENAI_BASE_URL

Supports 3 modes:
- raw: bare call, no validation, no retry
- schema: schema validation + bounded retry
- cognitive: reserved, temporarily downgrades to schema

Usage:
    from benchmarks.real_agent import create_real_agent
    
    agent = create_real_agent(backend="mimo", mode="schema")
    agent = create_real_agent(backend="deepseek", model="deepseek-v4-flash", mode="raw")
"""

import copy
import json
import os
import sys
import time
import logging
import urllib.request
import ssl
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("EITElite.benchmark.real_agent")

# EITElite root directory
# Error category constants
ERROR_CATEGORIES = ["wrong function name", "wrong parameter type", "missing parameter", "wrong enum value", "extra parameter", "wrong format"]


def _fix_schema_types(schema: dict):
    """Fix non-standard JSON Schema types to OpenAI-compatible format"""
    if not isinstance(schema, dict):
        return
    type_map = {"dict": "object", "float": "number", "double": "number", "int": "integer", "str": "string", "bool": "boolean"}
    t = schema.get("type", "")
    if t in type_map:
        schema["type"] = type_map[t]
    props = schema.get("properties", {})
    if isinstance(props, dict):
        for key, val in props.items():
            if isinstance(val, dict):
                _fix_schema_types(val)
    items = schema.get("items")
    if isinstance(items, dict):
        _fix_schema_types(items)
    for key in ("anyOf", "oneOf", "allOf"):
        subs = schema.get(key)
        if isinstance(subs, list):
            for sub in subs:
                if isinstance(sub, dict):
                    _fix_schema_types(sub)


def normalize_tool_schema(tools: List[Dict]) -> List[Dict]:
    """Normalize different providers' tool schemas to internal format
    
    Output: [{"name": str, "parameters": {"type":"object", "properties":{...}, "required":[...]}}]
    Supports: OpenAI / Anthropic / Generic (three formats)
    """
    normalized = []
    for t in tools:
        try:
            params = {}
            if "function" in t:
                func = t["function"]
                params = func.get("parameters", {}) or {}
                normalized.append({"name": func.get("name"), "parameters": params})
            elif "input_schema" in t:
                params = t.get("input_schema", {}) or {}
                normalized.append({"name": t.get("name"), "parameters": params})
            elif "name" in t and ("parameters" in t or "schema" in t):
                params = t.get("parameters") or t.get("schema") or {}
                normalized.append({"name": t.get("name"), "parameters": params})
            else:
                logger.warning("Unknown tool schema format: %s", list(t.keys()))
                continue
            # Defensive fix-up
            item = normalized[-1]
            if not isinstance(item["parameters"], dict):
                item["parameters"] = {}
            p = item["parameters"]
            p.setdefault("type", "object")
            if not isinstance(p.get("properties"), dict):
                p["properties"] = {}
            if not isinstance(p.get("required"), list):
                p["required"] = []
        except Exception as e:
            logger.exception("normalize_tool_schema failed: %s", e)
    return normalized


def _call_openai_compat(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    *,
    api_key: str,
    base_url: str,
    model: str,
    max_tokens: int = 4000,
    timeout: int = 90,
) -> Dict:
    """OpenAI-compatible API call (MiMo/DeepSeek/any compatible endpoint)"""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools

    is_mimo = "mimo" in base_url.lower()
    auth_header = api_key if is_mimo else f"Bearer {api_key}"
    auth_key = "api-key" if is_mimo else "Authorization"

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={auth_key: auth_header, "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.loads(resp.read())

    msg = data.get("choices", [{}])[0].get("message", {})
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        func = tc.get("function", {})
        args_str = func.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "args": args,
        })

    return {"content": msg.get("content", "") or "", "tool_calls": tool_calls}


def _call_worker_raw(
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    *,
    system_prompt: str = "",
    max_tokens: int = 4000,
) -> Dict:
    """Direct call to DeepSeek API replacing legacy worker"""
    msgs = [{"role": "system", "content": system_prompt}] + messages if system_prompt else messages
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    return _call_openai_compat(
        msgs, tools, api_key=api_key, base_url=base_url,
        model="deepseek-chat", max_tokens=max_tokens,
    )


class SemanticValidator:
    """Semantic validation - checks whether model output truly answers the user question
    
    Differences from SchemaValidator:
    - Schema: checks format (function name exists, param types correct, required fields present)
    - Semantic: checks content (param values match user intent, key entities are used)
    
    Strategy: heuristically extract key entities from user messages (numbers, quoted content),
    validate whether they appear in tool call parameters. Zero extra LLM calls.
    """

    # Common stop-words / short numbers excluded from matching
    _SKIP_NUMBERS = {'0', '1', '2', '3', '4', '5', '10', '100'}

    # Semantic validation config
    MISSING_RATIO_THRESHOLD = 0.7   # entity missing ratio threshold (only flag above 70% to avoid false alarms)
    MIN_ENTITY_COUNT = 4            # minimum entities to extract (skip validation if too few, to avoid noise)
    MIN_MISSING_COUNT = 3           # minimum missing entities (do not trigger below this count)

    def validate(self, messages: List[Dict], tool_calls: List[Dict]) -> Tuple[bool, str, Dict]:
        """Validate semantic correctness of tool_calls
        
        Returns: (is_valid, error_msg, trace_dict)
        
        Design principle: better to miss than false alarm (false positive is more fatal than false negative -
        false alarm breaks correct calls; missed detection only fails to rescue what could have been fixed)
        """
        if not tool_calls:
            return True, "", {}

        # 1. Extract key entities from user messages
        user_entities = self._extract_entities(messages)
        if len(user_entities) < self.MIN_ENTITY_COUNT:
            # Too few entities, likely noise, skip semantic validation
            return True, "", {}

        # 2. Collect all parameter values from tool calls
        arg_values = self._collect_arg_values(tool_calls)

        # 3. Check entity coverage
        missing = self._find_missing_entities(user_entities, arg_values)
        
        # High threshold: flag only when >70% entities uncovered AND >=3 entities missing
        if (len(missing) > len(user_entities) * self.MISSING_RATIO_THRESHOLD 
                and len(missing) >= self.MIN_MISSING_COUNT):
            hint = f"User mentioned key info {missing[:5]}, but your call may not have used these values. Please verify parameter accuracy."
            return False, f"Semantically suspicious: key info mentioned by user not used {missing[:3]}", {
                "error_type": "semantically suspicious",
                "missing_entities": missing[:5],
                "hint": hint,
            }

        return True, "", {}

    def _extract_entities(self, messages: List[Dict]) -> List[str]:
        """Extract key entities from user messages"""
        import re
        entities = []
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            # Extract numbers (including decimals)
            for num in re.findall(r'\b\d+\.?\d*\b', content):
                if num not in self._SKIP_NUMBERS:
                    entities.append(num)
            # Extract quoted content
            for quoted in re.findall(r'["\']([^"\']+)["\']', content):
                if len(quoted) > 1 and len(quoted) < 50:
                    entities.append(quoted)
        return entities

    def _collect_arg_values(self, tool_calls: List[Dict]) -> List[str]:
        """Collect string representations of all parameter values in tool calls"""
        values = []
        for tc in tool_calls:
            func = tc.get("function", {})
            args_raw = func.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except Exception:
                    continue
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                continue
            self._flatten_values(args, values)
        return values

    def _flatten_values(self, obj: Any, out: List[str]):
        """Recursively flatten nested values"""
        if isinstance(obj, dict):
            for v in obj.values():
                self._flatten_values(v, out)
        elif isinstance(obj, list):
            for v in obj:
                self._flatten_values(v, out)
        else:
            out.append(str(obj))

    def _find_missing_entities(self, entities: List[str], arg_values: List[str]) -> List[str]:
        """Find entities mentioned by user but absent from parameters"""
        arg_str = " ".join(arg_values).lower()
        missing = []
        for entity in entities:
            # Exact number match
            if entity.replace('.', '', 1).isdigit():
                if entity not in arg_str:
                    missing.append(entity)
            else:
                # String substring match
                if entity.lower() not in arg_str:
                    missing.append(entity)
        return missing


class RealAgent:
    """Real model Agent, compatible with benchmarks agent_fn signature
    
    Supports three modes:
    - raw: bare model call, no validation, no retry
    - schema: schema validation on model output, repair and retry on failure (bounded retry + echo chamber guard)
    - cognitive: schema validation + semantic validation, two-layer defense, repair semantic-level errors
    """

    def __init__(
        self,
        backend: str = "auto",
        model: str = "",
        mode: str = "raw",
        system_prompt: str = "You are a helpful AI assistant. Complete the task accurately.",
        max_tokens: int = 4000,
        max_rounds: int = 10,
    ):
        self.mode = mode
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        self.call_count = 0
        self.total_latency_ms = 0.0

        # Resolve backend config
        self.backend, self.model, self._call_fn = self._resolve_backend(backend, model)

        # schema/retry related state
        self._normalized_schemas = []
        self.current_retry_stats = {}
        self.current_error_breakdown = {}
        self.current_validation_trace = []

        # Semantic validator (cognitive mode only)
        self._semantic_validator = SemanticValidator()

        # Error pattern accumulation (persisted across tasks)
        self._accumulated_patterns = {
            "semantic_missing": {},  # {func_name: [missing_entities]}
        }

    def _resolve_backend(self, backend: str, model: str) -> tuple:
        """Auto-detect and configure available LLM backend"""
        mimo_key = os.environ.get("MIMO_API_KEY", "")
        mimo_url = os.environ.get(
            "MIMO_API_URL", "https://api.example.com/v1/chat/completions"
        )
        mimo_base = mimo_url.rsplit("/chat/completions", 1)[0] if "/chat/completions" in mimo_url else mimo_url

        openai_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
        openai_base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

        if backend == "auto":
            if mimo_key:
                backend = "mimo"
            elif openai_key:
                backend = "openai"
            else:
                raise ValueError("No LLM backend available.")

        if backend == "mimo":
            if not mimo_key:
                raise ValueError("MIMO_API_KEY not set")
            _model = model or os.environ.get("MIMO_MODEL", "mimo-v2-pro")
            logger.info(f"RealAgent: MiMo backend, model={_model}, mode={self.mode}")
            def call_fn(msgs, tools=None):
                return _call_openai_compat(msgs, tools, api_key=mimo_key, base_url=mimo_base, model=_model, max_tokens=self.max_tokens)
            return "mimo", _model, call_fn

        elif backend in ("openai", "deepseek"):
            if not openai_key:
                raise ValueError("OPENAI_API_KEY/DEEPSEEK_API_KEY not set")
            _model = model or os.environ.get("OPENAI_MODEL", "deepseek-chat")
            logger.info(f"RealAgent: OpenAI-compat backend, model={_model}, base={openai_base}, mode={self.mode}")
            def call_fn(msgs, tools=None):
                return _call_openai_compat(msgs, tools, api_key=openai_key, base_url=openai_base, model=_model, max_tokens=self.max_tokens)
            return "openai", _model, call_fn

        elif backend == "ollama":
            _model = model or "orthos-v0.1"
            ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://<redacted-ollama>:11434")
            logger.info(f"RealAgent: Ollama backend, model={_model}, base={ollama_base}, mode={self.mode}")
            def call_fn(msgs, tools=None):
                return _call_openai_compat(msgs, tools, api_key="", base_url=ollama_base, model=_model, max_tokens=self.max_tokens)
            return "ollama", _model, call_fn

        elif backend == "worker":
            _model = model or "worker-default"
            logger.info(f"RealAgent: worker backend, mode={self.mode}")
            def call_fn(msgs, tools=None):
                return _call_worker_raw(msgs, tools, system_prompt=self.system_prompt, max_tokens=self.max_tokens)
            return "worker", _model, call_fn

        else:
            raise ValueError(f"Unknown backend: {backend}")

    # =====================
    # VALIDATOR
    # =====================

    def _validate_tool_calls(self, tool_calls: List[Dict]) -> Tuple[bool, str, Dict]:
        """Validate whether model output tool_calls conform to schema
        
        Returns: (is_valid, error_msg, trace_dict)
        """
        if not tool_calls:
            return True, "", {}

        schema_map = {t["name"]: t for t in self._normalized_schemas if t.get("name")}

        for call in tool_calls:
            function_block = call.get("function", {}) or {}
            func_name = function_block.get("name")

            # function does not exist
            if func_name not in schema_map:
                return False, f"wrong function name: [{func_name}] does not exist.", {
                    "error_type": "wrong function name", "tool": func_name
                }

            schema = schema_map[func_name]

            # arguments parsing - defensive against None/non-string/non-dict
            args_raw = function_block.get("arguments", "{}")
            if args_raw is None:
                args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    return False, "wrong format: arguments is not valid JSON.", {
                        "error_type": "wrong format", "tool": func_name
                    }
            else:
                return False, "wrong format: arguments type is illegal.", {
                    "error_type": "wrong format", "tool": func_name, "raw_type": type(args_raw).__name__
                }

            parameters = schema.get("parameters", {}) or {}
            properties = parameters.get("properties", {}) or {}
            required = parameters.get("required", []) or []

            # missing parameter
            for req in required:
                if req not in args:
                    return False, f"missing parameter: [{func_name}] missing [{req}].", {
                        "error_type": "missing parameter", "tool": func_name, "field": req
                    }

            # extra parameter + type + enum
            type_mapping = {
                "string": str, "integer": int, "number": (int, float),
                "boolean": bool, "array": list, "object": dict,
            }

            for param_name, param_value in args.items():
                if param_name not in properties:
                    return False, f"extra parameter: [{param_name}] is not defined.", {
                        "error_type": "extra parameter", "tool": func_name, "field": param_name
                    }

                param_meta = properties.get(param_name, {}) or {}
                expected_type = param_meta.get("type")

                if expected_type in type_mapping:
                    if expected_type == "boolean" and isinstance(param_value, bool):
                        pass
                    elif expected_type == "integer" and isinstance(param_value, bool):
                        return False, f"wrong parameter type: [{param_name}] expected integer, got boolean.", {
                            "error_type": "wrong parameter type", "tool": func_name,
                            "field": param_name, "expected": "integer", "actual": "boolean"
                        }
                    elif not isinstance(param_value, type_mapping[expected_type]):
                        return False, f"wrong parameter type: [{param_name}] expected [{expected_type}].", {
                            "error_type": "wrong parameter type", "tool": func_name,
                            "field": param_name, "expected": expected_type,
                            "actual": type(param_value).__name__
                        }

                if "enum" in param_meta:
                    if param_value not in param_meta["enum"]:
                        return False, f"wrong enum value: [{param_name}] is illegal.", {
                            "error_type": "wrong enum value", "tool": func_name,
                            "field": param_name, "allowed": param_meta["enum"]
                        }

        return True, "", {}

    def _build_error_signature(self, trace: Dict) -> Tuple:
        """Build error signature - (error_type, tool, field) triplet
        
        Granularity: same type + same function + same field = same error
        "missing parameter: func1 missing A" vs "missing parameter: func1 missing B" → different signatures
        "missing parameter: func1 missing A" appears twice → same signature, triggers echo chamber guard
        """
        return (
            trace.get("error_type"),
            trace.get("tool"),
            trace.get("field"),
        )

    def _build_repair_message(self, error_msg: str, trace: Dict) -> Dict:
        """Build repair hint - uses system role, does not pollute assistant/tool conversation chain"""
        error_type = trace.get("error_type", "unknown error")
        content = (
            "Your function call does not conform to schema constraints.\n\n"
            f"Error type: {error_type}\n"
            f"Error details: {error_msg}\n\n"
            "Requirements:\n"
            "1. Preserve original user intent\n"
            "2. Only fix the erroneous field\n"
            "3. Do not add non-existent parameters\n"
            "4. Regenerate the complete tool call\n"
            "5. Do not output explanatory text\n"
        )
        return {"role": "system", "content": content}

    # =====================
    # MAIN CALL
    # =====================

    def __call__(self, messages, tools=None):
        """agent_fn signature: (messages, tools) -> response dict"""
        t0 = time.time()
        self.call_count += 1

        # Build complete message list
        full_msgs = [{"role": "system", "content": self.system_prompt}]
        if isinstance(messages, list):
            full_msgs.extend(messages)
        else:
            full_msgs.append({"role": "user", "content": str(messages)})

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                if isinstance(t, dict) and "type" in t:
                    tool = json.loads(json.dumps(t))
                    func = tool.get("function", {})
                    params = func.get("parameters", {})
                    _fix_schema_types(params)
                    openai_tools.append(tool)
                elif isinstance(t, dict) and "function" in t:
                    tool = {"type": "function", "function": json.loads(json.dumps(t["function"]))}
                    _fix_schema_types(tool["function"].get("parameters", {}))
                    openai_tools.append(tool)
                elif isinstance(t, dict):
                    params = t.get("parameters", {"type": "object", "properties": {}})
                    if isinstance(params, dict):
                        _fix_schema_types(params)
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", "unknown"),
                            "description": t.get("description", ""),
                            "parameters": params,
                        },
                    })

        # ---- raw mode: direct call ----
        if self.mode == "raw":
            return self._raw_call(full_msgs, openai_tools, t0)

        # ---- schema mode: format validation + retry ----
        if self.mode == "schema":
            return self._schema_call(full_msgs, openai_tools, t0)

        # ---- cognitive mode: format validation + semantic validation + retry ----
        return self._cognitive_call(full_msgs, openai_tools, t0)

    def _raw_call(self, full_msgs, openai_tools, t0):
        """Bare call mode - original logic"""
        all_msgs = list(full_msgs)
        for round_i in range(self.max_rounds):
            resp = self._call_fn(all_msgs, openai_tools)
            content = resp.get("content", "")
            tc_list = resp.get("tool_calls", [])

            if not tc_list:
                latency = (time.time() - t0) * 1000
                self.total_latency_ms += latency
                return {
                    "choices": [{
                        "message": {
                            "content": content,
                            "tool_calls": [],
                        }
                    }]
                }

            openai_tc = []
            for tc in tc_list:
                openai_tc.append({
                    "id": tc.get("id", f"call_{self.call_count}_{round_i}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("args", {})),
                    },
                })

            latency = (time.time() - t0) * 1000
            self.total_latency_ms += latency
            return {
                "choices": [{
                    "message": {
                        "content": content,
                        "tool_calls": openai_tc,
                    }
                }]
            }

        return {
            "choices": [{
                "message": {
                    "content": "[max rounds reached]",
                    "tool_calls": [],
                }
            }]
        }

    def _schema_call(self, full_msgs, openai_tools, t0):
        """schema validation mode - with bounded retry + echo chamber guard"""
        max_repair_rounds = 3
        current_round = 0
        last_signature = None
        same_signature_count = 0
        final_output = {}

        # Deep copy messages to avoid retry pollution
        working_messages = copy.deepcopy(full_msgs)

        # Initialize schema
        if openai_tools:
            self._normalized_schemas = normalize_tool_schema(openai_tools)
        else:
            self._normalized_schemas = []

        # Initialize metrics
        self.current_retry_stats = {
            "total_retries": 0,
            "retry_triggered": False,
            "repair_success_count": 0,
            "repair_failed_count": 0,
        }
        self.current_error_breakdown = {k: 0 for k in ERROR_CATEGORIES}
        self.current_validation_trace = []

        while current_round < max_repair_rounds:
            current_round += 1

            # Call model
            resp = self._call_fn(working_messages, openai_tools)
            content = resp.get("content", "")
            tc_list = resp.get("tool_calls", [])

            # Convert to OpenAI format
            openai_tc = []
            for tc in tc_list:
                openai_tc.append({
                    "id": tc.get("id", f"call_{self.call_count}_{current_round}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("args", {})) if isinstance(tc.get("args"), dict) else str(tc.get("args", "")),
                    },
                })

            final_output = {
                "choices": [{
                    "message": {
                        "content": content,
                        "tool_calls": openai_tc,
                    }
                }]
            }

            # No tool_calls → cannot validate, return directly
            if not tc_list:
                latency = (time.time() - t0) * 1000
                self.total_latency_ms += latency
                return final_output

            # schema validation
            is_valid, error_msg, trace = self._validate_tool_calls(openai_tc)

            if is_valid:
                if self.current_retry_stats["retry_triggered"]:
                    self.current_retry_stats["repair_success_count"] += 1
                latency = (time.time() - t0) * 1000
                self.total_latency_ms += latency
                return final_output

            # Record error
            error_type = trace.get("error_type", "wrong format")
            if error_type in self.current_error_breakdown:
                self.current_error_breakdown[error_type] += 1
            self.current_validation_trace.append({
                "round": current_round, "trace": trace, "error_msg": error_msg
            })

            # Max rounds reached
            if current_round >= max_repair_rounds:
                break

            self.current_retry_stats["total_retries"] += 1
            self.current_retry_stats["retry_triggered"] = True

            # Echo chamber guard
            signature = self._build_error_signature(trace)
            if signature == last_signature:
                same_signature_count += 1
                if same_signature_count >= 2:
                    logger.warning("Echo chamber guard triggered, stopping repair: %s", signature)
                    break
            else:
                same_signature_count = 0
                last_signature = signature

            # Append repair hint (system role)
            repair_message = self._build_repair_message(error_msg, trace)
            working_messages.append(repair_message)

        # All 3 rounds failed or echo chamber triggered
        if self.current_retry_stats["retry_triggered"]:
            self.current_retry_stats["repair_failed_count"] += 1

        latency = (time.time() - t0) * 1000
        self.total_latency_ms += latency
        return final_output

    def stats(self) -> Dict:
        """Return call statistics"""
        return {
            "backend": self.backend,
            "model": self.model,
            "mode": self.mode,
            "total_calls": self.call_count,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "avg_latency_ms": round(
                self.total_latency_ms / max(self.call_count, 1), 1
            ),
        }

    # =====================
    # COGNITIVE CALL (step 2: semantic-level repair)
    # =====================

    def _cognitive_call(self, full_msgs, openai_tools, t0):
        """cognitive mode - schema validation + semantic validation, two-layer defense
        
        Flow:
        1. First run schema validation (same as schema mode)
        2. After schema passes, add semantic validation layer
        3. Semantic validation fails → inject targeted hint and retry
        4. Accumulate error patterns for future warm-up injection
        """
        max_repair_rounds = 3
        current_round = 0
        last_signature = None
        same_signature_count = 0
        final_output = {}

        working_messages = copy.deepcopy(full_msgs)

        if openai_tools:
            self._normalized_schemas = normalize_tool_schema(openai_tools)
        else:
            self._normalized_schemas = []

        # Initialize metrics (add semantically suspicious category on top of schema)
        self.current_retry_stats = {
            "total_retries": 0,
            "retry_triggered": False,
            "repair_success_count": 0,
            "repair_failed_count": 0,
            "semantic_retries": 0,
            "semantic_success": 0,
        }
        self.current_error_breakdown = {k: 0 for k in ERROR_CATEGORIES}
        self.current_error_breakdown["semantically suspicious"] = 0
        self.current_validation_trace = []

        while current_round < max_repair_rounds:
            current_round += 1

            resp = self._call_fn(working_messages, openai_tools)
            content = resp.get("content", "")
            tc_list = resp.get("tool_calls", [])

            openai_tc = []
            for tc in tc_list:
                openai_tc.append({
                    "id": tc.get("id", f"call_{self.call_count}_{current_round}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("args", {})) if isinstance(tc.get("args"), dict) else str(tc.get("args", "")),
                    },
                })

            final_output = {
                "choices": [{
                    "message": {
                        "content": content,
                        "tool_calls": openai_tc,
                    }
                }]
            }

            if not tc_list:
                latency = (time.time() - t0) * 1000
                self.total_latency_ms += latency
                return final_output

            # ---- Layer 1: schema validation ----
            is_valid, error_msg, trace = self._validate_tool_calls(openai_tc)

            if not is_valid:
                error_type = trace.get("error_type", "wrong format")
                if error_type in self.current_error_breakdown:
                    self.current_error_breakdown[error_type] += 1
                self.current_validation_trace.append({
                    "round": current_round, "layer": "schema",
                    "trace": trace, "error_msg": error_msg
                })

                if current_round >= max_repair_rounds:
                    break

                self.current_retry_stats["total_retries"] += 1
                self.current_retry_stats["retry_triggered"] = True

                # Echo chamber guard
                signature = self._build_error_signature(trace)
                if signature == last_signature:
                    same_signature_count += 1
                    if same_signature_count >= 2:
                        break
                else:
                    same_signature_count = 0
                    last_signature = signature

                repair_message = self._build_repair_message(error_msg, trace)
                working_messages.append(repair_message)
                continue

            # ---- Layer 2: semantic validation (entered only after schema passes) ----
            sem_valid, sem_error, sem_trace = self._semantic_validator.validate(
                working_messages, openai_tc
            )

            if not sem_valid:
                self.current_error_breakdown["semantically suspicious"] += 1
                self.current_validation_trace.append({
                    "round": current_round, "layer": "semantic",
                    "trace": sem_trace, "error_msg": sem_error
                })

                # Accumulate error patterns
                for tc in openai_tc:
                    fname = tc.get("function", {}).get("name", "")
                    if fname:
                        missing = sem_trace.get("missing_entities", [])
                        if fname not in self._accumulated_patterns["semantic_missing"]:
                            self._accumulated_patterns["semantic_missing"][fname] = []
                        self._accumulated_patterns["semantic_missing"][fname].extend(missing)

                if current_round >= max_repair_rounds:
                    break

                self.current_retry_stats["total_retries"] += 1
                self.current_retry_stats["retry_triggered"] = True
                self.current_retry_stats["semantic_retries"] += 1

                # Semantic-level repair hint
                hint = sem_trace.get("hint", sem_error)
                semantic_repair = {
                    "role": "system",
                    "content": (
                        f"Your function call may not accurately reflect user requirements.\n\n"
                        f"Problem: {sem_error}\n"
                        f"Hint: {hint}\n\n"
                        f"Please carefully verify that parameter values precisely match the data mentioned by the user, and regenerate the tool call."
                    ),
                }
                working_messages.append(semantic_repair)

                # Echo chamber guard (applies to semantic layer too)
                sem_signature = ("semantically suspicious", sem_trace.get("missing_entities", [""])[0])
                if sem_signature == last_signature:
                    same_signature_count += 1
                    if same_signature_count >= 2:
                        break
                else:
                    same_signature_count = 0
                    last_signature = sem_signature
                continue

            # ---- Both validation layers passed ----
            if self.current_retry_stats["retry_triggered"]:
                self.current_retry_stats["repair_success_count"] += 1
                if self.current_retry_stats["semantic_retries"] > 0:
                    self.current_retry_stats["semantic_success"] += 1

            latency = (time.time() - t0) * 1000
            self.total_latency_ms += latency
            return final_output

        # All 3 rounds failed
        if self.current_retry_stats["retry_triggered"]:
            self.current_retry_stats["repair_failed_count"] += 1

        latency = (time.time() - t0) * 1000
        self.total_latency_ms += latency
        return final_output


def create_real_agent(
    backend: str = "auto",
    model: str = "",
    mode: str = "raw",
    system_prompt: str = "",
    max_tokens: int = 4000,
    max_rounds: int = 10,
) -> RealAgent:
    """Factory function"""
    return RealAgent(
        backend=backend,
        model=model,
        mode=mode,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
    )


# ============ Convenience presets ============

def mimo_agent(model: str = "mimo-v2-pro", mode: str = "raw", **kwargs) -> RealAgent:
    return create_real_agent(backend="mimo", model=model, mode=mode, **kwargs)

def deepseek_agent(model: str = "deepseek-chat", mode: str = "raw", **kwargs) -> RealAgent:
    return create_real_agent(backend="openai", model=model, mode=mode, **kwargs)

def deepseek_reasoner_agent(model: str = "deepseek-reasoner", mode: str = "raw", **kwargs) -> RealAgent:
    return create_real_agent(backend="openai", model=model, mode=mode, max_tokens=8000, **kwargs)

def worker_agent(mode: str = "raw", **kwargs) -> RealAgent:
    return create_real_agent(backend="worker", mode=mode, **kwargs)
