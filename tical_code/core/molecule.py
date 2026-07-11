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

"""Molecular Chain v3 - composable model architecture with infinite extensibility.

Core insight: Multi-model AUDIT (same prompt, vote) produces a subset of
the input. Molecular CHAIN (step A feeds step B) produces something that
did not exist in any single model's output - like H2O from H2 and O2.

v3 Changes from v2 (audit-driven):
  P0-1 FIX: provider_preference no longer mutates shared registry state
  P0-2 FIX: force_type returns None when no matching provider exists
  P0-3 FIX: CATALYZE retries tracked per-bond, not globally
  P0-4 FIX: template injection prevented by escaping model output
  P1-1 FIX: health_check with TTL cache (30s default)
  P1-2 FIX: string-based roles with dynamic registration (no enum limit)
  P1-3 FIX: BRANCH/MERGE raise NotImplementedError with clear message
  P1-4 FIX: step_timeout on ChainStep, enforced via threading
  P1-5 FIX: cost estimation uses registry metadata
  P1-6 FIX: optional output_schema on Atom for JSON validation
  P1-7 FIX: removed hardcoded provider_type from presets

v3 New Feature: DISTILLATE - personal cognitive model
  The distillate model represents the user's own cognitive patterns,
  distilled from decision traces, interaction history, and preferences.
  It both SERVES in chains (providing "what would the user think"
  judgments) and ACCUMULATES data (every interaction generates training
  data for continuous fine-tuning).

  Key design:
  - DistillateProvider: wraps the user's personal model endpoint
  - DistillateCollector: collects interaction traces as training data
  - DistillateTrainer: handles periodic fine-tuning from collected data
  - DISTILLATE role: participates in chains where user cognition
    matters (decision-making, preference judgment, style alignment)
  - Data flywheel: use → collect → train → better predictions → use more

v2 Architecture (preserved):
  ModelProvider  - abstract interface for any model source (API or local)
  ModelRegistry  - dynamic registry: register, discover, route models
  ChainRouter    - per-step routing: large API model or small local model
  Atom           - a model/agent with typed capabilities (role + provider)
  Bond           - a transformation between atoms
  Molecule       - a recipe: ordered list of (atom, bond) pairs
  MoleculeEngine - executes chains, composes results, supports nesting

Research-backed design (2026-06-15 deep research):
  - Llama Guard 3 (8B): safety F1=0.939 vs GPT-4 0.805 [arXiv:2411.10414]
  - BARRED Qwen2.5-3B: plan verification 0.98 vs GPT-4.1 0.58 [arXiv:2604.25203]
  - Interfaze: small-model chain AIME-2025 90% vs GPT-4.1 34.7% [arXiv:2602.04101]
  - DMoT: 4x1B chain BigCodeBench 2.6x improvement
  - SFT is core lever, DPO <0.6% marginal gain [CAPID arXiv:2603.20100]

Usage:
    from tical_code.core.molecule import MoleculeEngine, ModelRegistry

    registry = ModelRegistry()
    registry.register_api_provider("deepseek", failover=worker.llm)
    registry.register_local_provider("guard-1b", endpoint="http://localhost:8765")

    # Register the user's distillate model
    registry.register_distillate_provider(
        name="owner-distillate",
        endpoint="http://localhost:8780",
        data_dir="/data/distillate",
    )

    engine = MoleculeEngine(registry=registry)
    result = engine.react("code_review", user_prompt="Review this function...")
"""

import json
import logging
import os
import socket as _ssrf_socket
import threading
import time
import urllib.parse as urllib_parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


logger = logging.getLogger("EITElite.molecule")


# ═══════════════════════════════════════════════════════════════════
# AtomRole - string-based extensible role taxonomy
# ═══════════════════════════════════════════════════════════════════

class AtomRole:
    """Atomic role - what this model/agent does best.

    v3: String-based instead of Enum. New roles can be registered
    at runtime without modifying this class. Use AtomRole.register()
    to add custom roles.

    Core roles cover the minimum viable chain (3 dedicated small models).
    Custom roles can be added for independent function models.

    ┌──────────────────────────────────────────────────────────────┐
    │ DEDICATED SMALL MODEL PLAN (research-backed)                │
    ├──────────┬────────┬────────────────────────────────────────┤
    │ Model    │ Role   │ Status                                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │ guard-1b │ GUARD  │ TODO: 0.5-1B, safety+PII+policy       │
    │          │        │ Target: F1>0.93 (vs GPT-4 0.805)       │
    │          │        │ Training: 50K PII + 20K policy samples │
    │          │        │ Deploy: localhost:8765                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │vrfy-3b   │VERIFY  │ TODO: 3B, code+claim verification      │
    │          │        │ Target: F1>0.85 (vs GPT-4 0.80)        │
    │          │        │ Training: 100K review pairs + 50K claim │
    │          │        │ Deploy: localhost:8766                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │ synth-3b │ SYNTH  │ TODO: 3B, structured synthesis          │
    │          │        │ Target: coverage>0.90 (vs GPT-4 0.82)  │
    │          │        │ Training: 50K multi-input→summary pairs │
    │          │        │ Deploy: localhost:8767                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │testgen-3b│EXECUTOR│ TODO: 3B, code→test generation          │
    │          │        │ Target: pass@1>0.60 for routine code   │
    │          │        │ Training: code→test pairs from top repos│
    │          │        │ Deploy: localhost:8768                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │fmt-1b    │FORMAT  │ TODO: 1B, format conversion+JSON force │
    │          │        │ Target: valid-JSON rate>0.99            │
    │          │        │ Training: input→structured output pairs │
    │          │        │ Deploy: localhost:8769                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │rson-7b   │REASONER│ TODO: 7B, routine reasoning+planning   │
    │          │        │ Target: match GPT-4 on routine tasks    │
    │          │        │ Training: reasoning traces distillation │
    │          │        │ Deploy: localhost:8770 (needs GPU/8GB)  │
    └──────────┴────────┴────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────┐
    │ DISTILLATE - personal cognitive model (v3 new)              │
    ├──────────┬──────────────────────────────────────────────────┤
    │ Model    │ Role   │ Status                                  │
    ├──────────┼────────┼────────────────────────────────────────┤
    │owner-dist│DISTILL │ TODO: distilled from user's Decision    │
    │          │        │ Trace, interaction history, preferences │
    │          │        │ Flywheel: use→collect→train→better     │
    │          │        │ Deploy: localhost:8780                  │
    │          │        │ Training: continuous SFT from traces    │
    │          │        │ Target: predict user's decision >0.80  │
    └──────────┴────────┴────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────┐
    │ INDEPENDENT FUNCTION MODEL SLOTS (plug-in as needed)        │
    ├────────────────────┬────────────────────────────────────────┤
    │ Custom Role        │ Use Case                               │
    ├────────────────────┼────────────────────────────────────────┤
    │ TRANSLATOR         │ i18n translation, locale-aware         │
    │ SUMMARIZER         │ Long-doc compression, key extraction   │
    │ CLASSIFIER         │ Intent classification, routing         │
    │ RETRIEVER          │ RAG query embedding + ranking          │
    │ CRYPTOGRAPH        │ Encryption/decryption validation       │
    │ COMPLIANCE         │ Regulatory check (GDPR, SOC2, etc.)   │
    │ CUSTOM_<NAME>      │ Any future specialized function        │
    └────────────────────┴────────────────────────────────────────┘
    """

    # Core roles - covered by dedicated small model plan
    REASONER = "reasoner"           # Deep analysis, planning, CoT
    EXECUTOR = "executor"           # Fast, reliable task execution
    VERIFIER = "verifier"           # Fact-checking, evidence matching
    GUARD = "guard"                 # Safety, PII, policy enforcement
    SYNTHESIZER = "synthesizer"     # Combining multiple inputs
    FORMATTER = "formatter"         # Format conversion, JSON enforcement

    # Distillate - personal cognitive model (v3 new)
    DISTILLATE = "distillate"       # User's own cognitive patterns

    # Independent function model slots - plug in as needed
    TRANSLATOR = "translator"       # i18n, locale-aware translation
    SUMMARIZER = "summarizer"       # Long-doc compression
    CLASSIFIER = "classifier"       # Intent classification, routing
    RETRIEVER = "retriever"         # RAG query + ranking
    CRYPTOGRAPH = "cryptograph"     # Encryption/decryption validation
    COMPLIANCE = "compliance"       # Regulatory compliance check

    # ── Dynamic role registry (v3: string-based extensibility) ───

    _known_roles: Dict[str, str] = {}  # role_name → description
    _preferred_provider: Dict[str, str] = {  # role_name → "local"|"api"
        "guard": "local",
        "verifier": "local",
        "formatter": "local",
        "synthesizer": "local",
        "classifier": "local",
        "compliance": "local",
        "cryptograph": "local",
        "summarizer": "local",
        "translator": "local",
        "retriever": "local",
        "distillate": "local",     # Personal model: always local (privacy)
        "executor": "api",
        "reasoner": "api",
    }

    @classmethod
    def register_role(cls, name: str, description: str = "",
                      preferred: str = "local") -> None:
        """Register a custom role at runtime.

        Args:
            name: Role name (lowercase, hyphenated).
            description: What this role does.
            preferred: Preferred provider type - "local" or "api".
        """
        if name not in cls._known_roles:
            cls._known_roles[name] = description
            cls._preferred_provider[name] = preferred
            logger.info("[AtomRole] Registered custom role: %s (%s)", name, description)

    @classmethod
    def get_preferred_provider(cls, role: str) -> str:
        """Get the preferred provider type for a role."""
        return cls._preferred_provider.get(role, "api")

    @classmethod
    def validate(cls, role: str) -> str:
        """Validate and normalize a role string.

        If the role is not recognized (built-in or registered),
        it's automatically registered as a custom role.

        Returns:
            The normalized role string.
        """
        all_known = {
            cls.REASONER, cls.EXECUTOR, cls.VERIFIER, cls.GUARD,
            cls.SYNTHESIZER, cls.FORMATTER, cls.DISTILLATE,
            cls.TRANSLATOR, cls.SUMMARIZER, cls.CLASSIFIER,
            cls.RETRIEVER, cls.CRYPTOGRAPH, cls.COMPLIANCE,
        }
        if role in all_known or role in cls._known_roles:
            return role
        # Auto-register unknown roles as custom
        cls.register_role(role, f"Auto-registered custom role: {role}")
        return role


# ═══════════════════════════════════════════════════════════════════
# ModelProvider - abstract interface for any model source
# ═══════════════════════════════════════════════════════════════════

class ModelProvider(ABC):
    """Abstract base for model providers.

    All model sources - cloud API, local inference, mock, distillate -
    implement this interface so the chain engine can call them uniformly.
    """

    @abstractmethod
    def call(self, messages: List[Dict[str, str]], max_tokens: int = 2000,
             temperature: float = 0.3) -> "ProviderResult":
        """Call this model provider."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Check if this provider is alive and ready."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name."""
        ...

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """Provider type: 'api', 'local', 'distillate', or 'mock'."""
        ...


@dataclass
class ProviderResult:
    """Result from a model provider call."""
    content: str
    provider_name: str
    model: str
    latency_seconds: float = 0.0
    token_count: int = 0
    error: Optional[str] = None


class APIModelProvider(ModelProvider):
    """Cloud API provider - wraps ModelFailover for external model calls.

    Used for: creative generation, deep reasoning, any task requiring
    broad knowledge that only large models can provide.

    Fallback: if API fails, ChainRouter may route to a local model
    with the same role (if registered).
    """

    def __init__(self, provider_name: str, failover, family: str = ""):
        self._name = provider_name
        self._failover = failover
        self._family = family

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider_type(self) -> str:
        return "api"

    def call(self, messages: List[Dict[str, str]], max_tokens: int = 2000,
             temperature: float = 0.3) -> ProviderResult:
        t_start = time.time()
        try:
            result = self._failover.call(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                preferred_family=self._family,
            )
            return ProviderResult(
                content=result.content,
                provider_name=result.provider_name,
                model=result.model,
                latency_seconds=time.time() - t_start,
            )
        except Exception as e:
            return ProviderResult(
                content="",
                provider_name=self._name,
                model="",
                latency_seconds=time.time() - t_start,
                error=str(e),
            )

    def health_check(self) -> bool:
        try:
            result = self._failover.call(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                temperature=0.0,
                preferred_family=self._family,
            )
            return bool(result.content)
        except Exception:
            return False


class LocalModelProvider(ModelProvider):
    """Local inference provider - calls a small model served locally.

    Used for: all structured subtasks (VERIFIER, GUARD, FORMATTER, etc.)
    where specialized small models beat large API models on accuracy,
    latency, cost, and reliability.

    Deployment targets:
      - guard-1b:    localhost:8765 (llama.cpp, ~800MB RAM, Q4)
      - verifier-3b: localhost:8766 (llama.cpp, ~2GB RAM, Q4)
      - synth-3b:    localhost:8767 (llama.cpp, ~2GB RAM, Q4)
      - testgen-3b:  localhost:8768 (llama.cpp, ~2GB RAM, Q4)
      - fmt-1b:      localhost:8769 (llama.cpp, ~800MB RAM, Q4)
      - rson-7b:     localhost:8770 (llama.cpp/vLLM, ~4GB RAM, Q4)
    """

    def __init__(self, provider_name: str, endpoint: str,
                 model_name: str = "", timeout: float = 30.0):
        self._name = provider_name
        self._endpoint = endpoint.rstrip("/")
        self._model_name = model_name or provider_name
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider_type(self) -> str:
        return "local"

    def call(self, messages: List[Dict[str, str]], max_tokens: int = 2000,
             temperature: float = 0.3) -> ProviderResult:
        t_start = time.time()
        try:
            # SSRF check: ensure endpoint doesn't resolve to private IP
            _ssrf_host = urllib_parse.urlparse(self._endpoint).hostname
            if _ssrf_host:
                _ssrf_ip = _ssrf_socket.gethostbyname(_ssrf_host)
                _ssrf_parts = _ssrf_ip.split(".")
                if (_ssrf_parts[0] in ("10", "127", "0") or
                    (_ssrf_parts[0] == "172" and 16 <= int(_ssrf_parts[1]) <= 31) or
                    (_ssrf_parts[0] == "192" and _ssrf_parts[1] == "168") or
                    _ssrf_ip.startswith("169.254.") or _ssrf_ip == "::1"):
                    raise ValueError(f"SSRF blocked: endpoint {self._endpoint} resolves to private IP {_ssrf_ip}")
            import requests
            payload = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            resp = requests.post(
                f"{self._endpoint}/v1/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return ProviderResult(
                content=content,
                provider_name=self._name,
                model=self._model_name,
                latency_seconds=time.time() - t_start,
                token_count=data.get("usage", {}).get("completion_tokens", 0),
            )
        except Exception as e:
            logger.warning("[LocalProvider] %s call failed: %s", self._name, e)
            return ProviderResult(
                content="",
                provider_name=self._name,
                model=self._model_name,
                latency_seconds=time.time() - t_start,
                error=str(e),
            )

    def health_check(self) -> bool:
        try:
            import requests
            resp = requests.get(f"{self._endpoint}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════
# Distillate - personal cognitive model (v3 NEW)
# ═══════════════════════════════════════════════════════════════════

class DistillateProvider(ModelProvider):
    """Personal cognitive model provider - the user's distilled mind.

    This is NOT just another model. It represents the user's own
    cognitive patterns - their decision-making, preferences, style,
    and thinking patterns, distilled into a local model.

    Dual nature:
    1. SERVES: participates in chains where user cognition matters
       (decision-making, preference judgment, style alignment,
        "what would the user think/do" predictions)
    2. ACCUMULATES: every interaction generates training data.
       The DistillateCollector attached to this provider captures
       input/output pairs for continuous fine-tuning.

    Data flywheel:
      use in chain → collect trace → fine-tune → better predictions
      → more valuable in chains → use more → collect more ...

    Privacy: The distillate model is ALWAYS local (localhost:8780).
    User's cognitive data never leaves their infrastructure.

    Training pipeline:
      Phase 1: Bootstrap from Decision Trace archive (if available)
      Phase 2: Continuous SFT from interaction traces
      Phase 3: Periodic DPO for alignment refinement (marginal, <0.6%)

    Deploy: localhost:8780 (llama.cpp, ~2-4GB RAM, Q4)
    Base model: Qwen2.5-1.5B or Qwen2.5-3B (SFT on decision traces)
    Target: predict user's decision >0.80 accuracy
    """

    def __init__(self, provider_name: str, endpoint: str,
                 data_dir: str = "/data/distillate",
                 model_name: str = "owner-distillate",
                 timeout: float = 30.0,
                 collect: bool = True):
        """Initialize the distillate provider.

        Args:
            provider_name: Unique name for this provider.
            endpoint: Local inference URL (e.g., "http://localhost:8780").
            data_dir: Directory to store collected interaction traces.
            model_name: Model identifier for result metadata.
            timeout: Request timeout in seconds.
            collect: Whether to collect interaction traces for training.
        """
        self._name = provider_name
        self._endpoint = endpoint.rstrip("/")
        self._model_name = model_name
        self._timeout = timeout
        self._data_dir = data_dir
        self._collect = collect
        self._collector = DistillateCollector(data_dir) if collect else None
        self._lock = threading.Lock()
        self._trace_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider_type(self) -> str:
        return "distillate"

    @property
    def collector(self) -> Optional["DistillateCollector"]:
        """Access the trace collector for training data management."""
        return self._collector

    @property
    def trace_count(self) -> int:
        """Number of traces collected so far."""
        return self._trace_count

    def call(self, messages: List[Dict[str, str]], max_tokens: int = 2000,
             temperature: float = 0.3) -> ProviderResult:
        t_start = time.time()
        try:
            import requests
            payload = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            resp = requests.post(
                f"{self._endpoint}/v1/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = ProviderResult(
                content=content,
                provider_name=self._name,
                model=self._model_name,
                latency_seconds=time.time() - t_start,
                token_count=data.get("usage", {}).get("completion_tokens", 0),
            )

            # Collect trace for future fine-tuning
            if self._collect and self._collector and content:
                self._collect_trace(messages, content, result.token_count)

            return result
        except Exception as e:
            logger.warning("[DistillateProvider] %s call failed: %s", self._name, e)
            return ProviderResult(
                content="",
                provider_name=self._name,
                model=self._model_name,
                latency_seconds=time.time() - t_start,
                error=str(e),
            )

    def health_check(self) -> bool:
        try:
            import requests
            resp = requests.get(f"{self._endpoint}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def _collect_trace(self, input_messages: List[Dict[str, str]],
                       output: str, token_count: int) -> None:
        """Collect an interaction trace for training data.

        Each trace contains:
        - timestamp: when this interaction happened
        - input: the messages sent to the model
        - output: what the model produced
        - tokens: token count for cost estimation
        - context: optional metadata about the chain step

        Traces are appended to a daily JSONL file in data_dir/traces/.
        A background thread can periodically consolidate traces and
        trigger fine-tuning via DistillateTrainer.
        """
        trace = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "input": input_messages,
            "output": output,
            "token_count": token_count,
        }
        try:
            if self._collector:
                self._collector.append(trace)
                self._trace_count += 1
        except Exception as e:
            logger.warning("[DistillateProvider] Trace collection failed: %s", e)

    def get_training_stats(self) -> Dict[str, Any]:
        """Get statistics about collected training data."""
        return {
            "total_traces": self._trace_count,
            "data_dir": self._data_dir,
            "collecting": self._collect,
            "collector_stats": self._collector.stats() if self._collector else {},
        }


class DistillateCollector:
    """Collects interaction traces for distillate model fine-tuning.

    Traces are stored as JSONL files, one per day, in the configured
    data directory. This provides:
    - Chronological organization (easy to find recent data)
    - Append-only writes (safe for concurrent access)
    - Daily granularity for incremental training

    File structure:
      data_dir/
        traces/
          2026-06-15.jsonl
          2026-06-16.jsonl
          ...
        training/
          consolidated/
            batch_001.jsonl    # Merged traces ready for SFT
          checkpoints/
            v1/                # Model checkpoints after fine-tuning
        meta.json              # Collection statistics
    """

    def __init__(self, data_dir: str):
        """Initialize the collector.

        Args:
            data_dir: Root directory for trace storage.
        """
        self._data_dir = data_dir
        self._traces_dir = os.path.join(data_dir, "traces")
        self._training_dir = os.path.join(data_dir, "training")
        self._meta_path = os.path.join(data_dir, "meta.json")
        self._lock = threading.Lock()
        self._total_traces = 0
        self._total_bytes = 0

        # Ensure directories exist
        os.makedirs(self._traces_dir, exist_ok=True)
        os.makedirs(os.path.join(self._training_dir, "consolidated"), exist_ok=True)
        os.makedirs(os.path.join(self._training_dir, "checkpoints"), exist_ok=True)

        # Load existing stats
        self._load_meta()

    def _load_meta(self) -> None:
        """Load collection metadata from disk."""
        if os.path.exists(self._meta_path):
            try:
                with open(self._meta_path, "r") as f:
                    meta = json.load(f)
                self._total_traces = meta.get("total_traces", 0)
                self._total_bytes = meta.get("total_bytes", 0)
            except Exception:
                pass

    def _save_meta(self) -> None:
        """Save collection metadata to disk."""
        meta = {
            "total_traces": self._total_traces,
            "total_bytes": self._total_bytes,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            with open(self._meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.warning("[DistillateCollector] Meta save failed: %s", e)

    def append(self, trace: Dict[str, Any]) -> None:
        """Append a trace to the daily JSONL file.

        Thread-safe: uses a lock to prevent concurrent write corruption.

        Args:
            trace: A dict containing the interaction trace.
        """
        with self._lock:
            day_str = time.strftime("%Y-%m-%d")
            trace_path = os.path.join(self._traces_dir, f"{day_str}.jsonl")
            line = json.dumps(trace, ensure_ascii=False) + "\n"
            try:
                with open(trace_path, "a") as f:
                    f.write(line)
                self._total_traces += 1
                self._total_bytes += len(line.encode("utf-8"))
                # Save meta every 100 traces
                if self._total_traces % 100 == 0:
                    self._save_meta()
            except Exception as e:
                logger.warning("[DistillateCollector] Append failed: %s", e)

    def consolidate(self, days: Optional[int] = None) -> str:
        """Consolidate daily trace files into a training-ready batch.

        Reads all daily JSONL files (or the last N days), transforms
        them into SFT training format, and writes a consolidated file.

        SFT format (per line):
          {"messages": [{"role": "user", "content": "..."},
                         {"role": "assistant", "content": "..."}]}

        Args:
            days: Number of recent days to include. None = all.

        Returns:
            Path to the consolidated training file.
        """
        import glob

        trace_files = sorted(glob.glob(os.path.join(self._traces_dir, "*.jsonl")))
        if days is not None:
            trace_files = trace_files[-days:]

        batch_num = len(glob.glob(
            os.path.join(self._training_dir, "consolidated", "batch_*.jsonl"))) + 1
        batch_path = os.path.join(
            self._training_dir, "consolidated", f"batch_{batch_num:03d}.jsonl")

        sft_count = 0
        with open(batch_path, "w") as out_f:
            for tf in trace_files:
                with open(tf, "r") as in_f:
                    for line in in_f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            trace = json.loads(line)
                            # Convert trace to SFT format
                            input_msgs = trace.get("input", [])
                            output_text = trace.get("output", "")
                            if input_msgs and output_text:
                                sft_entry = {
                                    "messages": input_msgs + [
                                        {"role": "assistant", "content": output_text}
                                    ]
                                }
                                out_f.write(json.dumps(sft_entry, ensure_ascii=False) + "\n")
                                sft_count += 1
                        except (json.JSONDecodeError, KeyError):
                            continue

        logger.info("[DistillateCollector] Consolidated %d traces into %s",
                    sft_count, batch_path)
        return batch_path

    def stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        return {
            "total_traces": self._total_traces,
            "total_bytes": self._total_bytes,
            "data_dir": self._data_dir,
        }


class DistillateTrainer:
    """Manages fine-tuning of the distillate model from collected traces.

    Training pipeline:
      1. DistillateCollector.consolidate() → batch file
      2. DistillateTrainer.train() → fine-tune the model
      3. Hot-swap the model on the DistillateProvider endpoint

    Training approach (research-backed):
      - SFT is the core lever (CAPID: arXiv:2603.20100)
      - DPO provides <0.6% marginal gain - use only in Phase 3
      - Multi-task learning causes 15% degradation (MoCoGrad) - train
        single-task or use task-specific LoRA adapters
      - Start from Qwen2.5-1.5B or Qwen2.5-3B (good base for reasoning)

    Training phases:
      Phase 1: Bootstrap - SFT on Decision Trace archive
               (existing decision logs, conversation summaries)
      Phase 2: Continuous - periodic SFT from DistillateCollector traces
               (every 1000 traces or daily, whichever comes first)
      Phase 3: Refinement - DPO on preference pairs from user feedback
               (marginal improvement, <0.6% per CAPID research)

    Training command (example with unsloth/axolotl):
      python -m axolotl.cli train config_distillate.yaml
      # or with llama-factory:
      llamafactory-cli train distillate_sft.yaml
    """

    def __init__(self, data_dir: str = "/data/distillate",
                 base_model: str = "Qwen/Qwen2.5-1.5B",
                 checkpoint_dir: Optional[str] = None):
        """Initialize the trainer.

        Args:
            data_dir: Root directory for training data and checkpoints.
            base_model: HuggingFace model ID for base weights.
            checkpoint_dir: Directory for model checkpoints.
        """
        self._data_dir = data_dir
        self._base_model = base_model
        self._checkpoint_dir = checkpoint_dir or os.path.join(
            data_dir, "training", "checkpoints")
        self._training_config_path = os.path.join(
            data_dir, "training", "config.yaml")

    def prepare_training_data(self, days: Optional[int] = None) -> str:
        """Prepare consolidated training data from collected traces.

        Args:
            days: Number of recent days to include. None = all.

        Returns:
            Path to the consolidated training file.
        """
        collector = DistillateCollector(self._data_dir)
        return collector.consolidate(days=days)

    def generate_training_config(self, batch_path: str,
                                 output_dir: Optional[str] = None,
                                 epochs: int = 3,
                                 learning_rate: float = 2e-5,
                                 lora_rank: int = 16) -> str:
        """Generate a training configuration file.

        Creates a YAML config compatible with LLaMA-Factory or axolotl
        for SFT fine-tuning of the distillate model.

        Args:
            batch_path: Path to the consolidated training data.
            output_dir: Directory for training output.
            epochs: Number of training epochs.
            learning_rate: Learning rate for SFT.
            lora_rank: LoRA rank for parameter-efficient fine-tuning.

        Returns:
            Path to the generated config file.
        """
        if output_dir is None:
            version = len(os.listdir(
                os.path.dirname(self._checkpoint_dir))) if os.path.exists(
                os.path.dirname(self._checkpoint_dir)) else 0
            output_dir = os.path.join(
                self._checkpoint_dir, f"v{version + 1}")

        config = {
            "model_name_or_path": self._base_model,
            "stage": "sft",
            "do_train": True,
            "finetuning_type": "lora",
            "lora_rank": lora_rank,
            "lora_target": "all",
            "dataset": "distillate_sft",
            "dataset_dir": os.path.dirname(batch_path),
            "template": "qwen",
            "cutoff_len": 4096,
            "overwrite_output_dir": True,
            "per_device_train_batch_size": 4,
            "gradient_accumulation_steps": 4,
            "lr_scheduler_type": "cosine",
            "logging_steps": 10,
            "warmup_steps": 50,
            "save_steps": 500,
            "num_train_epochs": epochs,
            "learning_rate": learning_rate,
            "fp16": True,
            "output_dir": output_dir,
        }

        config_path = self._training_config_path
        try:
            import yaml
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False)
        except ImportError:
            # Fallback: write as JSON
            config_path = config_path.replace(".yaml", ".json")
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)

        logger.info("[DistillateTrainer] Training config written to %s", config_path)
        return config_path

    def train(self, batch_path: str, **kwargs) -> Dict[str, Any]:
        """Execute fine-tuning of the distillate model.

        This is a placeholder that generates the config and returns
        the training command. Actual training should be run on a
        machine with GPU access.

        Args:
            batch_path: Path to consolidated training data.
            **kwargs: Override training parameters.

        Returns:
            Dict with training plan and command.
        """
        config_path = self.generate_training_config(batch_path, **kwargs)

        return {
            "status": "ready",
            "config_path": config_path,
            "batch_path": batch_path,
            "base_model": self._base_model,
            "command": f"llamafactory-cli train {config_path}",
            "notes": [
                "SFT is the core lever (CAPID: DPO <0.6% marginal gain)",
                "Use LoRA rank 16 for parameter-efficient fine-tuning",
                "Train single-task to avoid 15% multi-task degradation (MoCoGrad)",
                "After training, hot-swap model on localhost:8780",
            ],
        }


class MockModelProvider(ModelProvider):
    """Mock provider for testing - returns canned responses."""

    def __init__(self, provider_name: str, response: str = "mock output"):
        self._name = provider_name
        self._response = response

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider_type(self) -> str:
        return "mock"

    def call(self, messages: List[Dict[str, str]], max_tokens: int = 2000,
             temperature: float = 0.3) -> ProviderResult:
        return ProviderResult(
            content=self._response,
            provider_name=self._name,
            model="mock",
            latency_seconds=0.01,
        )

    def health_check(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════════════
# ModelRegistry - dynamic model discovery and routing (v3)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RegistryEntry:
    """A registered model provider with metadata."""
    provider: ModelProvider
    roles: List[str]              # v3: string-based roles (was List[AtomRole])
    priority: int = 0             # Higher = preferred for given role
    cost_per_1k: float = 0.0      # Estimated cost per 1K tokens ($)
    avg_latency_ms: float = 0.0   # Average latency in milliseconds
    description: str = ""         # Human-readable description


class ModelRegistry:
    """Dynamic registry for model providers.

    v3 changes:
    - String-based roles (no Enum limitation)
    - Health check caching with TTL (P1-1 fix)
    - Distillate provider support
    - Thread-safe operations
    """

    # Health check cache TTL in seconds
    HEALTH_CACHE_TTL = 30.0

    def __init__(self):
        self._entries: Dict[str, RegistryEntry] = {}
        self._health_cache: Dict[str, Tuple[bool, float]] = {}  # name → (healthy, timestamp)
        self._lock = threading.Lock()

    def register(self, entry: RegistryEntry) -> None:
        """Register a model provider."""
        with self._lock:
            self._entries[entry.provider.name] = entry
        logger.info("[Registry] Registered %s for roles %s (priority=%d, type=%s)",
                    entry.provider.name,
                    entry.roles,
                    entry.priority,
                    entry.provider.provider_type)

    def register_api_provider(
        self,
        name: str,
        failover,
        family: str = "",
        roles: Optional[List[str]] = None,
        priority: int = 0,
        cost_per_1k: float = 0.005,
        avg_latency_ms: float = 2500.0,
        description: str = "",
    ) -> None:
        """Convenience: register an API model provider."""
        provider = APIModelProvider(name, failover, family)
        self.register(RegistryEntry(
            provider=provider,
            roles=roles or [AtomRole.REASONER, AtomRole.EXECUTOR,
                            AtomRole.SYNTHESIZER],
            priority=priority,
            cost_per_1k=cost_per_1k,
            avg_latency_ms=avg_latency_ms,
            description=description or f"API provider: {name}",
        ))

    def register_local_provider(
        self,
        name: str,
        endpoint: str,
        model_name: str = "",
        roles: Optional[List[str]] = None,
        priority: int = 10,
        cost_per_1k: float = 0.0,
        avg_latency_ms: float = 200.0,
        timeout: float = 30.0,
        description: str = "",
    ) -> None:
        """Convenience: register a local model provider."""
        provider = LocalModelProvider(name, endpoint, model_name, timeout)
        self.register(RegistryEntry(
            provider=provider,
            roles=roles or [AtomRole.GUARD],
            priority=priority,
            cost_per_1k=cost_per_1k,
            avg_latency_ms=avg_latency_ms,
            description=description or f"Local provider: {name}",
        ))

    def register_distillate_provider(
        self,
        name: str,
        endpoint: str = "http://localhost:8780",
        data_dir: str = "/data/distillate",
        model_name: str = "owner-distillate",
        roles: Optional[List[str]] = None,
        priority: int = 15,
        cost_per_1k: float = 0.0,
        avg_latency_ms: float = 300.0,
        timeout: float = 30.0,
        collect: bool = True,
        description: str = "",
    ) -> DistillateProvider:
        """Convenience: register the personal distillate model.

        The distillate model gets HIGHEST priority for DISTILLATE role
        and moderate priority for other roles it can serve (e.g., REASONER
        for user-aligned reasoning).

        Privacy: ALWAYS local. User cognitive data never leaves their infra.

        Args:
            name: Unique provider name.
            endpoint: Local inference URL.
            data_dir: Directory for trace storage.
            model_name: Model identifier.
            roles: Roles this provider serves.
            priority: Base priority (default 15 > local default 10).
            cost_per_1k: Cost per 1K tokens (0 = local).
            avg_latency_ms: Average latency.
            timeout: Request timeout.
            collect: Whether to collect traces for fine-tuning.
            description: Human-readable description.

        Returns:
            The DistillateProvider instance (for accessing collector/trainer).
        """
        provider = DistillateProvider(
            provider_name=name,
            endpoint=endpoint,
            data_dir=data_dir,
            model_name=model_name,
            timeout=timeout,
            collect=collect,
        )
        self.register(RegistryEntry(
            provider=provider,
            roles=roles or [AtomRole.DISTILLATE, AtomRole.REASONER],
            priority=priority,
            cost_per_1k=cost_per_1k,
            avg_latency_ms=avg_latency_ms,
            description=description or f"Distillate provider: {name} (personal cognitive model)",
        ))
        return provider

    def get_by_role(self, role: str) -> Optional[ModelProvider]:
        """Get the best provider for a given role.

        v3: Uses health check cache with TTL instead of calling
        health_check() on every invocation (P1-1 fix).

        Selection criteria (in order):
        1. Health check passes (cached)
        2. Highest priority
        3. Lowest cost (tiebreak)
        4. Lowest latency (tiebreak)
        """
        with self._lock:
            candidates = [
                e for e in self._entries.values()
                if role in e.roles
            ]

        if not candidates:
            return None

        # Filter to healthy providers using cache
        healthy = []
        for e in candidates:
            if self._is_healthy(e.provider):
                healthy.append(e)

        if not healthy:
            logger.warning("[Registry] No healthy provider for role %s, "
                           "trying unhealthy as fallback", role)
            healthy = candidates

        # Sort: priority desc, cost asc, latency asc
        healthy.sort(key=lambda e: (-e.priority, e.cost_per_1k, e.avg_latency_ms))
        return healthy[0].provider

    def get_by_name(self, name: str) -> Optional[ModelProvider]:
        """Get a specific provider by name."""
        with self._lock:
            entry = self._entries.get(name)
        return entry.provider if entry else None

    def get_entry_by_name(self, name: str) -> Optional[RegistryEntry]:
        """Get a full registry entry by name."""
        with self._lock:
            return self._entries.get(name)

    def list_available(self) -> List[Dict[str, Any]]:
        """List all registered providers with metadata."""
        with self._lock:
            entries = list(self._entries.values())
        return [
            {
                "name": e.provider.name,
                "type": e.provider.provider_type,
                "roles": e.roles,
                "priority": e.priority,
                "cost_per_1k": e.cost_per_1k,
                "avg_latency_ms": e.avg_latency_ms,
                "description": e.description,
            }
            for e in entries
        ]

    def health_check_all(self, force: bool = False) -> Dict[str, bool]:
        """Check health of all registered providers.

        Args:
            force: If True, bypass cache and check all providers.
        """
        results = {}
        with self._lock:
            entries = dict(self._entries)

        for name, entry in entries.items():
            if force:
                healthy = entry.provider.health_check()
                self._health_cache[name] = (healthy, time.time())
            else:
                healthy = self._is_healthy(entry.provider)
            results[name] = healthy
        return results

    def _is_healthy(self, provider: ModelProvider) -> bool:
        """Check provider health with TTL cache."""
        now = time.time()
        cached = self._health_cache.get(provider.name)
        if cached and (now - cached[1]) < self.HEALTH_CACHE_TTL:
            return cached[0]

        # Cache miss or expired - do actual health check
        healthy = provider.health_check()
        self._health_cache[provider.name] = (healthy, now)
        return healthy

    def get_cost_per_1k(self, provider_name: str) -> float:
        """Get the cost per 1K tokens for a provider."""
        entry = self.get_entry_by_name(provider_name)
        return entry.cost_per_1k if entry else 0.005


# ═══════════════════════════════════════════════════════════════════
# ChainRouter - automatic large/local model selection per step
# ═══════════════════════════════════════════════════════════════════

class ChainRouter:
    """Decide per-step: use a large API model or a small local model.

    v3 changes:
    - String-based roles
    - force_type returns None instead of falling through (P0-2 fix)
    - Distillate routing: DISTILLATE role always prefers local (privacy)
    """

    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def select_provider(self, role: str,
                        force_type: Optional[str] = None) -> Optional[ModelProvider]:
        """Select the best provider for a given role.

        v3 (P0-2 fix): When force_type is specified and no matching
        provider is found, returns None instead of falling through
        to a provider of the wrong type.

        Args:
            role: The atom role for this chain step.
            force_type: Override routing - "api", "local", or "distillate".

        Returns:
            A ModelProvider, or None if no suitable provider exists.
        """
        if force_type:
            # Filter to the requested type only
            candidates = []
            with self.registry._lock:
                for e in self.registry._entries.values():
                    if role in e.roles and e.provider.provider_type == force_type:
                        candidates.append(e)

            if candidates:
                # Check health with cache
                healthy = [
                    e for e in candidates
                    if self.registry._is_healthy(e.provider)
                ]
                if not healthy:
                    healthy = candidates  # fallback to unhealthy
                healthy.sort(key=lambda e: (-e.priority, e.cost_per_1k, e.avg_latency_ms))
                return healthy[0].provider

            # P0-2 FIX: Return None when force_type has no match
            # Do NOT fall through to general routing
            return None

        # Default: use registry's role-based routing
        provider = self.registry.get_by_role(role)

        # If preferred type doesn't match, try preferred type
        preferred = AtomRole.get_preferred_provider(role)
        if provider and provider.provider_type != preferred:
            preferred_candidates = []
            with self.registry._lock:
                for e in self.registry._entries.values():
                    if role in e.roles and e.provider.provider_type == preferred:
                        preferred_candidates.append(e)

            healthy_preferred = [
                e for e in preferred_candidates
                if self.registry._is_healthy(e.provider)
            ]
            if healthy_preferred:
                healthy_preferred.sort(
                    key=lambda e: (-e.priority, e.cost_per_1k, e.avg_latency_ms))
                return healthy_preferred[0].provider

        return provider


# ═══════════════════════════════════════════════════════════════════
# Atom / Bond / Molecule - chain definition primitives
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Atom:
    """A single model/agent in the molecular chain.

    v3 changes:
    - role is a string (not Enum), supporting custom roles
    - output_schema for JSON validation (P1-6 fix)
    - step_timeout for per-step timeout (P1-4 fix)
    """
    name: str
    role: str                                    # v3: string-based role
    prompt_template: str
    max_tokens: int = 2000
    temperature: float = 0.3
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None          # "api", "local", "distillate"
    output_schema: Optional[Dict[str, Any]] = None  # P1-6: JSON schema for validation
    step_timeout: Optional[float] = None          # P1-4: per-step timeout in seconds

    def __post_init__(self):
        """Validate and normalize the role."""
        self.role = AtomRole.validate(self.role)


class BondType(str, Enum):
    """How two atoms connect in the chain."""
    REFINE = "refine"
    VERIFY = "verify"
    TRANSFORM = "transform"
    BRANCH = "branch"         # v3: raises NotImplementedError (P1-3 fix)
    CATALYZE = "catalyze"
    MERGE = "merge"           # v3: raises NotImplementedError (P1-3 fix)


@dataclass
class Bond:
    """A connection between two atoms in a molecule."""
    bond_type: BondType
    template: str = ""
    on_fail: str = "skip"         # "stop" | "skip" | "retry"
    max_retries: int = 2


@dataclass
class ChainStep:
    """One step in a molecular chain."""
    atom: Atom
    bond: Bond


@dataclass
class Molecule:
    """A molecular chain recipe."""
    name: str
    description: str
    steps: List[ChainStep]
    metadata: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# ChainResult - execution output with telemetry
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ChainResult:
    """Output from executing a molecular chain."""
    molecule_name: str
    final_output: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    total_latency: float = 0.0
    emergent: bool = True
    cost_estimate: float = 0.0
    retry_count: int = 0
    distillate_traces: int = 0     # v3: traces collected by distillate provider

    def to_dict(self) -> Dict[str, Any]:
        return {
            "molecule": self.molecule_name,
            "output": self.final_output,
            "steps": self.steps,
            "latency_seconds": round(self.total_latency, 2),
            "emergent": self.emergent,
            "cost_estimate": round(self.cost_estimate, 6),
            "retry_count": self.retry_count,
            "distillate_traces": self.distillate_traces,
        }


# ═══════════════════════════════════════════════════════════════════
# RoutingContext - per-call routing preference (P0-1 fix)
# ═══════════════════════════════════════════════════════════════════

class RoutingContext:
    """Per-call routing preference that does NOT mutate shared registry.

    v3 (P0-1 fix): Instead of modifying registry priorities directly,
    this context wraps the routing decision with per-call preferences.
    The registry's priorities remain unchanged for other callers.
    """

    def __init__(self, preference: str = "auto"):
        """Initialize routing context.

        Args:
            preference: "auto", "prefer_local", "prefer_api", "local_only"
        """
        self.preference = preference

    def adjust_priority(self, base_priority: int, provider_type: str) -> int:
        """Adjust priority based on routing preference.

        Returns a modified priority WITHOUT changing the registry.

        Args:
            base_priority: The provider's registry priority.
            provider_type: "api", "local", "distillate", or "mock".

        Returns:
            Adjusted priority for this call only.
        """
        if self.preference == "auto":
            return base_priority
        elif self.preference == "prefer_local":
            if provider_type in ("local", "distillate"):
                return base_priority + 20
            return base_priority
        elif self.preference == "prefer_api":
            if provider_type == "api":
                return base_priority + 20
            return base_priority
        elif self.preference == "local_only":
            if provider_type == "api":
                return -999  # Effectively disable API providers
            return base_priority
        return base_priority


# ═══════════════════════════════════════════════════════════════════
# MoleculeEngine - chain executor with routing and telemetry
# ═══════════════════════════════════════════════════════════════════

class MoleculeEngine:
    """Execute molecular chains - composable model architecture.

    v3 changes:
    - String-based roles (P1-2 fix)
    - Per-bond CATALYZE retry tracking (P0-3 fix)
    - Template injection prevention (P0-4 fix)
    - Step-level timeout (P1-4 fix)
    - JSON schema validation (P1-6 fix)
    - RoutingContext for per-call preferences (P0-1 fix)
    - Cost estimation from registry (P1-5 fix)
    - Distillate model support
    - No hardcoded provider_type in presets (P1-7 fix)
    """

    def __init__(self, failover=None, registry: Optional[ModelRegistry] = None):
        if registry:
            self._registry = registry
        elif failover:
            self._registry = ModelRegistry()
            self._registry.register_api_provider(
                name="default-api",
                failover=failover,
                roles=[AtomRole.REASONER, AtomRole.EXECUTOR,
                       AtomRole.VERIFIER, AtomRole.GUARD,
                       AtomRole.SYNTHESIZER, AtomRole.FORMATTER],
                priority=0,
                cost_per_1k=0.005,
                avg_latency_ms=2500.0,
                description="Default API provider (legacy mode)",
            )
        else:
            self._registry = ModelRegistry()

        self._router = ChainRouter(self._registry)
        self._presets: Dict[str, Molecule] = {}
        self._cost_tracker: Dict[str, float] = {}
        self._register_default_presets()

    @property
    def registry(self) -> ModelRegistry:
        return self._registry

    # ── Safe Template Substitution (P0-4 fix) ────────────────────

    # Sentinel placeholders - used to prevent template injection.
    # Model output that contains {input}, {context}, etc. would
    # accidentally get substituted in the next step's template.
    # Solution: replace template vars with unique sentinels first,
    # then swap sentinels with actual values. Since sentinels are
    # highly unlikely to appear in model output, injection is blocked.
    _SENTINELS = {
        "input": "\x00MOL_INPUT\x00",
        "context": "\x00MOL_CONTEXT\x00",
        "previous_role": "\x00MOL_PREV_ROLE\x00",
    }

    @staticmethod
    def _render_template(template: str, **kwargs) -> str:
        """Safely substitute template variables.

        v3 (P0-4 fix): Uses sentinel-based substitution to prevent
        template injection. If a model's output contains {input},
        it should NOT be substituted in the next step's template.

        Algorithm:
        1. Replace {key} placeholders with unique sentinel strings
        2. Replace sentinel strings with actual values
        This ensures that model output containing {key} is never
        interpreted as a template variable.

        Args:
            template: Template string with {key} placeholders.
            **kwargs: Values to substitute.

        Returns:
            Rendered template string.
        """
        result = template
        # Step 1: Replace placeholders with sentinels
        for key in MoleculeEngine._SENTINELS:
            placeholder = "{" + key + "}"
            if placeholder in result:
                result = result.replace(placeholder, MoleculeEngine._SENTINELS[key])
        # Step 2: Replace sentinels with actual values
        for key, value in kwargs.items():
            sentinel = MoleculeEngine._SENTINELS.get(key)
            if sentinel and sentinel in result:
                result = result.replace(sentinel, str(value) if value else "")
        return result

    # ── JSON Schema Validation (P1-6 fix) ────────────────────────

    @staticmethod
    def _validate_output(output: str, schema: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        """Validate model output against a JSON schema.

        Args:
            output: The model's output text.
            schema: Optional JSON schema to validate against.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if schema is None:
            return True, None

        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            return False, f"Output is not valid JSON: {e}"

        # Basic schema validation - check required fields exist
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in data:
                return False, f"Missing required field: {field_name}"

        return True, None

    # ── Preset Molecules ──────────────────────────────────────────

    def _register_default_presets(self):
        """Register built-in molecular chain presets.

        v3 changes (P1-7 fix): No hardcoded provider_type on steps.
        Let ChainRouter decide based on registered providers.
        When local small models are registered, routing shifts automatically.
        """

        # CODE_REVIEW: executor → reasoner → verifier → guard
        # Small model slots: VERIFIER→verifier-3b, GUARD→guard-1b
        # Distillate slot: add DISTILLATE step for user-style alignment
        self.register(Molecule(
            name="code_review",
            description="Draft code, review, verify, safety scan, user-style align",
            steps=[
                ChainStep(
                    atom=Atom(
                        name="code-generator",
                        role=AtomRole.EXECUTOR,
                        prompt_template=(
                            "You are a fast, reliable code generator. "
                            "Produce clean, working code for the following request. "
                            "Output ONLY the code, no explanation.\n\n"
                            "Request: {input}"
                        ),
                        max_tokens=2000,
                        temperature=0.2,
                    ),
                    bond=Bond(bond_type=BondType.TRANSFORM, on_fail="stop"),
                ),
                ChainStep(
                    atom=Atom(
                        name="code-reviewer",
                        role=AtomRole.REASONER,
                        prompt_template=(
                            "You are a senior code reviewer. Analyze the following code "
                            "for bugs, security issues, edge cases, and style problems. "
                            "Be specific - cite line numbers and suggest fixes.\n\n"
                            "Code to review:\n```\n{input}\n```\n\n"
                            "Provide: (1) Critical issues (2) Suggestions (3) Improved code"
                        ),
                        max_tokens=3000,
                        temperature=0.3,
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="code-verifier",
                        # FUTURE: verifier-3b (localhost:8766)
                        # Will auto-shift to local when registered
                        role=AtomRole.VERIFIER,
                        prompt_template=(
                            "You are a verification engine. Check if the reviewed code "
                            "below correctly addresses the ORIGINAL request and if all "
                            "identified issues are valid. Flag any false positives from "
                            "the reviewer.\n\n"
                            "Reviewed code and analysis:\n{input}\n\n"
                            "Return JSON: {{\"verified\": bool, \"issues_found\": int, "
                            "\"false_positives\": int, \"final_code\": \"...\"}}"
                        ),
                        max_tokens=2000,
                        temperature=0.1,
                        output_schema={"required": ["verified", "issues_found"]},
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="code-guard",
                        # FUTURE: guard-1b (localhost:8765)
                        role=AtomRole.GUARD,
                        prompt_template=(
                            "You are a safety guard. Scan the verified code for:\n"
                            "1. Hardcoded credentials or API keys\n"
                            "2. SQL injection or XSS vulnerabilities\n"
                            "3. Unsafe file operations or command injection\n"
                            "4. Data exfiltration patterns\n\n"
                            "Verified code:\n{input}\n\n"
                            "Return JSON: {{\"safe\": bool, \"violations\": [...], "
                            "\"sanitized_code\": \"...\"}}"
                        ),
                        max_tokens=1500,
                        temperature=0.1,
                        output_schema={"required": ["safe"]},
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
                # v3 NEW: Distillate step - align with user's coding style
                # Falls back to API provider when no distillate model deployed
                ChainStep(
                    atom=Atom(
                        name="style-aligner",
                        provider_type="api",  # Fallback: use API provider
                        # Predicts: "Would the user accept this code style?"
                        # Uses personal cognitive model for alignment
                        role=AtomRole.DISTILLATE,
                        prompt_template=(
                            "Based on your understanding of the user's coding "
                            "preferences and style, evaluate the following code. "
                            "Would the user be satisfied with this style? "
                            "Suggest any style adjustments that match the user's "
                            "preferences.\n\n"
                            "Code:\n{input}\n\n"
                            "Return JSON: {{\"style_aligned\": bool, "
                            "\"style_notes\": [...], \"adjusted_code\": \"...\"}}"
                        ),
                        max_tokens=1000,
                        temperature=0.1,
                        output_schema={"required": ["style_aligned"]},
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
            ],
        ))

        # RESEARCH: reasoner → executor → synthesizer
        self.register(Molecule(
            name="research",
            description="Analyze topic, search for evidence, synthesize findings",
            steps=[
                ChainStep(
                    atom=Atom(
                        name="research-analyst",
                        role=AtomRole.REASONER,
                        prompt_template=(
                            "You are a research analyst. Break down the following question "
                            "into 3-5 key sub-questions that need to be answered. "
                            "For each sub-question, specify what type of evidence "
                            "would be needed.\n\n"
                            "Question: {input}"
                        ),
                        max_tokens=1500,
                        temperature=0.3,
                    ),
                    bond=Bond(bond_type=BondType.TRANSFORM, on_fail="stop"),
                ),
                ChainStep(
                    atom=Atom(
                        name="evidence-finder",
                        role=AtomRole.EXECUTOR,
                        prompt_template=(
                            "You are a fact-finding assistant. Based on the analysis below, "
                            "provide concrete answers for each sub-question. Include "
                            "specific data, dates, sources where possible.\n\n"
                            "Analysis:\n{input}"
                        ),
                        max_tokens=2000,
                        temperature=0.2,
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="research-synthesizer",
                        # FUTURE: synth-3b (localhost:8767)
                        role=AtomRole.SYNTHESIZER,
                        prompt_template=(
                            "You are a synthesis engine. Combine the analysis and evidence "
                            "below into a coherent, structured answer. Resolve any "
                            "contradictions. Cite which evidence supports which claim.\n\n"
                            "Analysis + Evidence:\n{input}\n\n"
                            "Output a structured report with: Executive Summary, "
                            "Key Findings, Evidence Chain, Confidence Level"
                        ),
                        max_tokens=2000,
                        temperature=0.2,
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
                # v3 NEW: Distillate step - would the user find this useful?
                # Falls back to API provider when no distillate model deployed
                ChainStep(
                    atom=Atom(
                        name="relevance-judge",
                        provider_type="api",  # Fallback: use API provider
                        role=AtomRole.DISTILLATE,
                        prompt_template=(
                            "Based on your understanding of the user's interests "
                            "and research preferences, evaluate whether the "
                            "research below would be useful to the user. "
                            "Highlight the parts most relevant to the user's "
                            "typical concerns.\n\n"
                            "Research:\n{input}\n\n"
                            "Return JSON: {{\"relevance_score\": 0.0-1.0, "
                            "\"user_relevant_highlights\": [...], "
                            "\"missing_angles\": [...]}}"
                        ),
                        max_tokens=800,
                        temperature=0.1,
                        output_schema={"required": ["relevance_score"]},
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
            ],
        ))

        # SAFETY_CHECK: executor → guard → verifier
        self.register(Molecule(
            name="safety_check",
            description="Execute action, safety scan, audit trail",
            steps=[
                ChainStep(
                    atom=Atom(
                        name="task-executor",
                        role=AtomRole.EXECUTOR,
                        prompt_template=(
                            "You are a task executor. Produce the output for "
                            "the following request. Be precise and factual.\n\n"
                            "Task: {input}"
                        ),
                        max_tokens=1500,
                        temperature=0.2,
                    ),
                    bond=Bond(bond_type=BondType.TRANSFORM, on_fail="stop"),
                ),
                ChainStep(
                    atom=Atom(
                        name="safety-guard",
                        # FUTURE: guard-1b (localhost:8765)
                        role=AtomRole.GUARD,
                        prompt_template=(
                            "You are a safety guard. Scan the following output for:\n"
                            "1. Personal information (emails, phone numbers, addresses)\n"
                            "2. API keys, tokens, or credentials\n"
                            "3. Harmful instructions or policy violations\n"
                            "4. Factual claims without evidence\n\n"
                            "Output to scan:\n{input}\n\n"
                            "Return JSON: {{\"safe\": bool, \"violations\": [...], "
                            "\"sanitized_output\": \"...\"}}"
                        ),
                        max_tokens=1500,
                        temperature=0.1,
                        output_schema={"required": ["safe"]},
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="safety-verifier",
                        # FUTURE: verifier-3b (localhost:8766)
                        role=AtomRole.VERIFIER,
                        prompt_template=(
                            "You are an audit verifier. Confirm that the safety scan "
                            "below was thorough and the sanitized output is correct. "
                            "Check for missed violations.\n\n"
                            "Safety scan result:\n{input}\n\n"
                            "Return JSON: {{\"audit_passed\": bool, "
                            "\"missed_violations\": [...], \"final_output\": \"...\"}}"
                        ),
                        max_tokens=1000,
                        temperature=0.1,
                        output_schema={"required": ["audit_passed"]},
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
            ],
        ))

        # DECISION: reasoner → distillate → verifier → guard
        # v3: Added distillate step - the user's own cognitive patterns
        # should weigh on decisions before final guard approval
        self.register(Molecule(
            name="decision",
            description="Evaluate options, apply user's judgment, verify, approve/reject",
            steps=[
                ChainStep(
                    atom=Atom(
                        name="decision-analyst",
                        role=AtomRole.REASONER,
                        prompt_template=(
                            "You are a decision analyst. Evaluate the following "
                            "decision request. List pros/cons, risks, and your "
                            "recommendation with confidence level.\n\n"
                            "Decision: {input}"
                        ),
                        max_tokens=1500,
                        temperature=0.3,
                    ),
                    bond=Bond(bond_type=BondType.TRANSFORM, on_fail="stop"),
                ),
                # v3 NEW: Distillate step - "what would the user decide?"
                # Falls back to API provider when no distillate model deployed
                ChainStep(
                    atom=Atom(
                        name="user-judgment",
                        provider_type="api",  # Fallback: use API provider
                        # This is the CORE use case for the distillate model:
                        # predict the user's decision given their cognitive patterns
                        role=AtomRole.DISTILLATE,
                        prompt_template=(
                            "Based on your understanding of the user's "
                            "decision-making patterns, risk tolerance, and "
                            "preferences, predict what the user would decide "
                            "for the following analysis. Consider: "
                            "the user's typical risk assessment, "
                            "preferred trade-offs, and past decisions.\n\n"
                            "Analysis:\n{input}\n\n"
                            "Return JSON: {{\"user_would\": \"approve|reject|modify\", "
                            "\"confidence\": 0.0-1.0, \"reasoning\": \"...\", "
                            "\"modifications\": [...]}}"
                        ),
                        max_tokens=1000,
                        temperature=0.1,
                        output_schema={"required": ["user_would", "confidence"]},
                    ),
                    bond=Bond(bond_type=BondType.REFINE, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="condition-verifier",
                        # FUTURE: verifier-3b (localhost:8766)
                        role=AtomRole.VERIFIER,
                        prompt_template=(
                            "You are a condition verifier. For the decision analysis "
                            "and user judgment below, verify each factual claim "
                            "and check if any preconditions are missing.\n\n"
                            "Analysis + User Judgment:\n{input}\n\n"
                            "Return JSON: {{\"claims_verified\": int, "
                            "\"claims_failed\": int, \"missing_conditions\": [...], "
                            "\"risk_adjustment\": \"increase|decrease|same\"}}"
                        ),
                        max_tokens=1000,
                        temperature=0.1,
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
                ChainStep(
                    atom=Atom(
                        name="decision-guard",
                        # FUTURE: guard-1b (localhost:8765)
                        role=AtomRole.GUARD,
                        prompt_template=(
                            "You are a decision gate. Based on the analysis, "
                            "user judgment, and verification below, make a final "
                            "APPROVE or REJECT decision.\n\n"
                            "Full context:\n{input}\n\n"
                            "Return JSON: {{\"decision\": \"APPROVE|REJECT\", "
                            "\"confidence\": 0.0-1.0, \"reason\": \"...\", "
                            "\"conditions\": [...]}}"
                        ),
                        max_tokens=800,
                        temperature=0.1,
                        output_schema={"required": ["decision", "confidence"]},
                    ),
                    bond=Bond(bond_type=BondType.VERIFY, on_fail="skip"),
                ),
            ],
        ))

    # ── Registration ──────────────────────────────────────────────

    def register(self, molecule: Molecule):
        """Register a molecule preset."""
        self._presets[molecule.name] = molecule
        logger.info("[Molecule] Registered preset: %s (%d steps)",
                    molecule.name, len(molecule.steps))

    def register_molecule(self, molecule: Molecule):
        self.register(molecule)

    def list_molecules(self) -> List[Dict[str, Any]]:
        """List available molecule presets with routing info."""
        return [
            {
                "name": m.name,
                "description": m.description,
                "steps": [
                    {
                        "atom": s.atom.name,
                        "role": s.atom.role,
                        "bond": s.bond.bond_type.value,
                        "provider_type": s.atom.provider_type or "auto",
                        "has_output_schema": s.atom.output_schema is not None,
                    }
                    for s in m.steps
                ],
            }
            for m in self._presets.values()
        ]

    # ── Dynamic Chain Construction ────────────────────────────────

    def build_chain(self, name: str, description: str,
                    steps: List[Tuple[Atom, Bond]]) -> Molecule:
        molecule = Molecule(
            name=name,
            description=description,
            steps=[ChainStep(atom=a, bond=b) for a, b in steps],
        )
        return molecule

    def react_dynamic(self, molecule: Molecule, user_prompt: str,
                      context: Optional[str] = None,
                      routing: Optional[RoutingContext] = None) -> ChainResult:
        return self._execute_chain(molecule, user_prompt, context, routing)

    # ── Chain Execution ───────────────────────────────────────────

    def react(self, molecule_name: str, user_prompt: str,
              context: Optional[str] = None,
              routing: Optional[RoutingContext] = None) -> ChainResult:
        molecule = self._presets.get(molecule_name)
        if not molecule:
            return ChainResult(
                molecule_name=molecule_name,
                final_output=f"Unknown molecule: {molecule_name}",
                emergent=False,
            )
        return self._execute_chain(molecule, user_prompt, context, routing)

    def _execute_chain(self, molecule: Molecule, user_prompt: str,
                       context: Optional[str] = None,
                       routing: Optional[RoutingContext] = None) -> ChainResult:
        """Core chain execution engine.

        v3 fixes applied:
        - P0-3: Per-bond retry tracking
        - P0-4: Template injection prevention
        - P1-4: Step-level timeout
        - P1-5: Cost from registry metadata
        - P1-6: Output schema validation
        """
        t_start = time.time()
        current_input = user_prompt
        steps_detail: List[Dict[str, Any]] = []
        total_cost = 0.0
        total_retries = 0
        distillate_traces = 0
        bond_retries: Dict[int, int] = {}  # P0-3: per-bond retry tracking
        step_idx = 0

        while step_idx < len(molecule.steps):
            step = molecule.steps[step_idx]

            # P1-3 fix: reject unimplemented bond types
            if step.bond.bond_type in (BondType.BRANCH, BondType.MERGE):
                logger.error("[Molecule] Bond type %s not yet implemented",
                             step.bond.bond_type.value)
                if step.bond.on_fail == "stop":
                    return ChainResult(
                        molecule_name=molecule.name,
                        final_output=f"Chain aborted: unimplemented bond type "
                                     f"{step.bond.bond_type.value} at step {step_idx}",
                        steps=steps_detail,
                        total_latency=time.time() - t_start,
                        emergent=False,
                        cost_estimate=total_cost,
                        retry_count=total_retries,
                    )
                step_idx += 1
                continue

            # Select provider (with routing context if provided)
            provider = self._select_provider_for_step(
                step.atom, routing=routing)

            if not provider:
                steps_detail.append({
                    "step": step_idx,
                    "atom": step.atom.name,
                    "role": step.atom.role,
                    "bond_in": step.bond.bond_type.value,
                    "provider": "none",
                    "model": "none",
                    "latency_seconds": 0.0,
                    "output_preview": "",
                    "error": f"No provider available for role: {step.atom.role}",
                    "cost": 0.0,
                })
                if step.bond.on_fail == "stop":
                    return ChainResult(
                        molecule_name=molecule.name,
                        final_output=f"Chain aborted: no provider for step {step_idx}",
                        steps=steps_detail,
                        total_latency=time.time() - t_start,
                        emergent=False,
                        cost_estimate=total_cost,
                        retry_count=total_retries,
                    )
                step_idx += 1
                continue

            step_start = time.time()

            # Build prompt - _render_template uses sentinel-based
            # substitution (P0-4 fix) so model output containing
            # {input} or {context} won't be accidentally substituted
            prompt = self._render_template(
                step.atom.prompt_template,
                input=current_input,
                context=context or "",
                previous_role=molecule.steps[step_idx - 1].atom.role if step_idx > 0 else "",
            )

            messages = [{"role": "user", "content": prompt}]

            # P1-4 fix: step-level timeout
            result = self._call_with_timeout(
                provider, messages,
                max_tokens=step.atom.max_tokens,
                temperature=step.atom.temperature,
                timeout=step.atom.step_timeout,
            )

            step_latency = time.time() - step_start

            # P1-5 fix: cost from registry metadata
            step_cost = self._estimate_step_cost(
                provider, step.atom.max_tokens, result.token_count)
            total_cost += step_cost

            error = result.error
            step_output = result.content

            # P1-6 fix: output schema validation
            if step_output and step.atom.output_schema:
                valid, validation_error = self._validate_output(
                    step_output, step.atom.output_schema)
                if not valid:
                    logger.warning("[Molecule] Step %d output failed validation: %s",
                                   step_idx, validation_error)
                    # Don't fail the chain, but log the validation error
                    error = (error or "") + f" [validation: {validation_error}]"

            steps_detail.append({
                "step": step_idx,
                "atom": step.atom.name,
                "role": step.atom.role,
                "bond_in": step.bond.bond_type.value,
                "provider": provider.name,
                "provider_type": provider.provider_type,
                "model": result.model,
                "latency_seconds": round(step_latency, 2),
                "output_preview": step_output[:300] if step_output else "",
                "error": error,
                "cost": step_cost,
            })

            # Track distillate trace collection
            if provider.provider_type == "distillate" and isinstance(provider, DistillateProvider):
                distillate_traces = provider.trace_count

            # Handle failure
            if error or not step_output:
                if step.bond.on_fail == "stop":
                    logger.error("[Molecule] Chain aborted at step %d", step_idx)
                    return ChainResult(
                        molecule_name=molecule.name,
                        final_output=f"Chain aborted at step {step_idx}: {error}",
                        steps=steps_detail,
                        total_latency=time.time() - t_start,
                        emergent=False,
                        cost_estimate=total_cost,
                        retry_count=total_retries,
                        distillate_traces=distillate_traces,
                    )
                elif step.bond.on_fail == "skip":
                    logger.warning("[Molecule] Step %d failed, passing through", step_idx)
                elif step.bond.on_fail == "retry":
                    try:
                        result = provider.call(
                            messages=messages,
                            max_tokens=step.atom.max_tokens,
                            temperature=step.atom.temperature,
                        )
                        if result.content and not result.error:
                            step_output = result.content
                            steps_detail[-1]["output_preview"] = step_output[:300]
                            steps_detail[-1]["error"] = None
                            current_input = step_output
                    except Exception as e2:
                        logger.warning("[Molecule] Retry also failed: %s", e2)
            else:
                current_input = step_output

            # Handle CATALYZE bond (feedback loop)
            # P0-3 fix: per-bond retry tracking
            if step.bond.bond_type == BondType.CATALYZE and step_idx > 0:
                bond_key = step_idx
                if bond_key not in bond_retries:
                    bond_retries[bond_key] = 0

                if bond_retries[bond_key] < step.bond.max_retries:
                    # Check if verification passed
                    try:
                        check = json.loads(step_output)
                        passed = check.get("verified", check.get("safe",
                                   check.get("audit_passed", True)))
                        if not passed:
                            logger.info("[Molecule] CATALYZE: verification failed, "
                                        "looping back to step %d (retry %d/%d)",
                                        step_idx - 1,
                                        bond_retries[bond_key] + 1,
                                        step.bond.max_retries)
                            bond_retries[bond_key] += 1
                            total_retries += 1
                            step_idx = max(0, step_idx - 1)
                            continue
                    except (json.JSONDecodeError, AttributeError):
                        pass

            logger.info(
                "[Molecule] Step %d/%d: %s (%s/%s) → %d chars in %.1fs",
                step_idx + 1, len(molecule.steps), step.atom.name,
                provider.name, provider.provider_type,
                len(step_output) if step_output else 0, step_latency,
            )

            step_idx += 1

        total_latency = time.time() - t_start
        logger.info(
            "[Molecule] %s complete: %d steps, %.1fs total, $%.6f est. cost, %d distillate traces",
            molecule.name, len(molecule.steps), total_latency, total_cost,
            distillate_traces,
        )

        return ChainResult(
            molecule_name=molecule.name,
            final_output=current_input,
            steps=steps_detail,
            total_latency=total_latency,
            emergent=len(molecule.steps) > 1,
            cost_estimate=total_cost,
            retry_count=total_retries,
            distillate_traces=distillate_traces,
        )

    def _select_provider_for_step(
        self, atom: Atom, routing: Optional[RoutingContext] = None
    ) -> Optional[ModelProvider]:
        """Select the best provider for a chain step.

        v3: Uses RoutingContext instead of mutating registry (P0-1 fix).
        """
        # Force specific provider by name
        if atom.provider_name:
            provider = self._registry.get_by_name(atom.provider_name)
            if provider:
                return provider
            logger.warning("[Molecule] Forced provider %s not found, falling back",
                           atom.provider_name)

        # Force provider type (api/local/distillate)
        if atom.provider_type:
            return self._router.select_provider(atom.role, force_type=atom.provider_type)

        # Default: role-based routing
        return self._router.select_provider(atom.role)

    def _call_with_timeout(
        self, provider: ModelProvider,
        messages: List[Dict[str, str]],
        max_tokens: int = 2000,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
    ) -> ProviderResult:
        """Call a provider with optional step-level timeout.

        v3 (P1-4 fix): If timeout is specified, the call is aborted
        if it takes longer than timeout seconds.

        Args:
            provider: The model provider to call.
            messages: Chat messages.
            max_tokens: Maximum tokens.
            temperature: Sampling temperature.
            timeout: Step-level timeout in seconds. None = no limit.

        Returns:
            ProviderResult, possibly with a timeout error.
        """
        if timeout is None:
            return provider.call(messages, max_tokens, temperature)

        result_holder = [None]
        error_holder = [None]

        def _call():
            try:
                result_holder[0] = provider.call(messages, max_tokens, temperature)
            except Exception as e:
                error_holder[0] = e

        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            # Thread is still running - timeout hit
            return ProviderResult(
                content="",
                provider_name=provider.name,
                model="",
                latency_seconds=timeout,
                error=f"Step timed out after {timeout}s",
            )

        if error_holder[0]:
            return ProviderResult(
                content="",
                provider_name=provider.name,
                model="",
                latency_seconds=timeout,
                error=str(error_holder[0]),
            )

        return result_holder[0]

    def _estimate_step_cost(self, provider: ModelProvider,
                            max_tokens: int,
                            actual_tokens: int) -> float:
        """Estimate cost for a single step.

        v3 (P1-5 fix): Uses registry's cost_per_1k instead of
        hardcoded $0.005/1K.
        """
        tokens = actual_tokens or max_tokens
        if provider.provider_type in ("local", "distillate"):
            return 0.0  # Local inference: marginal cost ≈ 0
        # Look up actual cost from registry
        cost_per_1k = self._registry.get_cost_per_1k(provider.name)
        return tokens * (cost_per_1k / 1000.0)


# ═══════════════════════════════════════════════════════════════════
# Tool Schema & Execution - interface for tool_executor.py
# ═══════════════════════════════════════════════════════════════════

def build_chain_exec_tool_schema() -> Dict[str, Any]:
    """Build the 'chain_exec' tool schema for OpenAI function calling.

    v3 additions:
    - 'distillate' as a role option
    - 'provider_preference' no longer mutates registry (P0-1 fix)
    """
    return {
        "type": "function",
        "function": {
            "name": "chain_exec",
            "description": (
                "Execute a molecular chain - a sequence of AI models where each "
                "model's output feeds into the next, producing emergent intelligence. "
                "Supports preset chains and dynamic chains. The engine auto-routes "
                "each step to the best provider: local small models for structured "
                "tasks, cloud API for creative tasks, distillate model for user-"
                "aligned judgments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "molecule": {
                        "type": "string",
                        "description": "Which preset chain to execute.",
                        "enum": ["code_review", "research",
                                 "safety_check", "decision"],
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The input prompt for the molecular chain.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to include in each step.",
                    },
                    "custom_steps": {
                        "type": "array",
                        "description": (
                            "Custom chain steps. role options: reasoner, executor, "
                            "verifier, guard, synthesizer, formatter, distillate, "
                            "translator, summarizer, classifier, retriever, "
                            "cryptograph, compliance, or any custom role."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "prompt_template": {"type": "string"},
                                "provider_type": {
                                    "type": "string",
                                    "enum": ["auto", "api", "local", "distillate"],
                                    "default": "auto",
                                },
                                "bond_type": {
                                    "type": "string",
                                    "enum": ["refine", "verify", "transform",
                                             "catalyze"],
                                    "default": "refine",
                                },
                            },
                            "required": ["role", "prompt_template"],
                        },
                    },
                    "provider_preference": {
                        "type": "string",
                        "enum": ["auto", "prefer_local", "prefer_api", "local_only"],
                        "default": "auto",
                    },
                },
                "required": ["prompt"],
            },
        },
    }


def execute_chain_exec(
    engine: MoleculeEngine,
    molecule: Optional[str] = None,
    prompt: str = "",
    context: Optional[str] = None,
    custom_steps: Optional[List[Dict[str, Any]]] = None,
    provider_preference: str = "auto",
) -> Dict[str, Any]:
    """Execute a chain_exec tool call and return structured result.

    v3 (P0-1 fix): Uses RoutingContext instead of mutating registry.
    """
    if not prompt:
        return {"error": "prompt is required for chain execution"}

    # P0-1 FIX: Create routing context instead of mutating registry
    routing = RoutingContext(preference=provider_preference)

    # Execute preset molecule
    if molecule and not custom_steps:
        result = engine.react(molecule_name=molecule, user_prompt=prompt,
                              context=context, routing=routing)
        return result.to_dict()

    # Execute dynamic chain
    if custom_steps:
        steps = []
        for i, step_def in enumerate(custom_steps):
            role_str = step_def.get("role", "executor")
            role = AtomRole.validate(role_str)  # v3: auto-registers custom roles

            atom = Atom(
                name=f"custom-{role}-{i}",
                role=role,
                prompt_template=step_def.get("prompt_template", "{input}"),
                max_tokens=step_def.get("max_tokens", 2000),
                temperature=step_def.get("temperature", 0.2),
                provider_type=step_def.get("provider_type")
                    if step_def.get("provider_type") != "auto" else None,
            )
            bond_type_str = step_def.get("bond_type", "refine")
            try:
                bond_type = BondType(bond_type_str)
            except ValueError:
                bond_type = BondType.REFINE

            bond = Bond(bond_type=bond_type, on_fail="skip")
            steps.append((atom, bond))

        mol = engine.build_chain(
            name="dynamic",
            description="Dynamically constructed chain",
            steps=steps,
        )
        result = engine.react_dynamic(mol, user_prompt=prompt,
                                      context=context, routing=routing)
        return result.to_dict()

    # Fallback: run safety_check
    result = engine.react(molecule_name="safety_check",
                          user_prompt=prompt, context=context, routing=routing)
    return result.to_dict()


