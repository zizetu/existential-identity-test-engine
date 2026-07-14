# eite-agent

Version: 0.2.0 — AI Agent Evaluation Framework

## Iron Rules for All Code Changes

1. **ALL code MUST be English-only.** Comments, docstrings, variable names, log messages — everything. Zero Chinese, no Chinglish, no mixed-language.
2. **No `bare except:` ever.** Use `except Exception:` or a specific exception type.
3. **Read VERSION file** before making changes.
4. **Verify with `python3 -m py_compile`** after every edit.
5. **Regex patterns must be fully English.**
6. **Run `grep -cP '[\x{4e00}-\x{9fff}]' <file>`** after editing any .py file. Result must be 0.
7. **Run `grep -rn 'except:' --include='*.py' | grep -v 'except Exception' | grep -v 'except (' | wc -l`** — result must be 0 before commit.

## Architecture

### Core Modules (in tical_code/core/)
- `unified_worker.py` — Main worker loop; entry point for all worker nodes.
- `prompt.py` — System prompt builder with capability self-description.
- `decision_engine.py` — Structured decision pipeline with protocol interfaces.
- `axioms.py` — Physics axioms as cognition lenses.
- `llm_backend.py` — LLM client abstraction (OpenAI-compatible, Gemini, MiMo).
- `model_failover.py` — Session-affinity LRU failover with circuit-breaker health states.
- `mimo_free.py` — MiMo Free channel provider; per-worker fingerprint + JWT auth.
- `mimo_free_tool_adapter.py` — Maps MiMo model tool_call names/params to standard names.
- `context_compactor.py` — LLM self-summarization compaction at 80% token budget threshold.
- `tool_executor.py` — Tool execution with parallel batch processing.
- `tool_executor_clean.py` — Clean-room implementation of tool executor for evaluation contexts.
- `permission_checker.py` — 5-tier permission system.
- `provider_registry.py` — Git-managed provider configuration from config/providers.json.
- `message_handler.py` — Message dispatch: CMD detection -> LLM -> tool execution -> response.
- `memory_boot.py` — Memory system bootstrapping.
- `identity.py` — Worker identity registry.
- `self_repair.py` — Health checks, fix execution, and audit logging.
- `doom_loop.py` — Loop detection with configurable severity levels and recovery actions.
- `enhanced_router.py` — Enhanced model router with quality feedback loop.
- `hive.py` — Privacy filtering, capability extraction, and multi-agent coordination.
- `constitution.py` — Trading constitution with risk-control immutability rules.
- `ortools.py` — Optimization tools and cost tracking.

### Configuration (git-tracked)
- `config/providers.json` — Provider registry.
- `config/default.json` — Global default configuration.
- `config/worker-configs/*.json` — Per-worker configuration overrides.

### [CMD] Commands (defined in message_handler.py)
- `ping`, `help` — Worker level
- `status`, `deploy`, `report`, `switch_model` — Admin level
- `exec`, `escalate` — Master level
- `restart`, `log` — Worker level
- `permission [mode]` — Toggle permission mode
- `context` — Show context window status
- `providers [health|switch <name>]` — Provider management

## Node Topology
You are Kael, running on a remote VPS. WireGuard VPN server for the mesh.

### Mesh Nodes
| Node | Role | Access |
|------|------|--------|
| Cang | tical-agent runner | SSH via config alias |
| Seoul | Dev / orchestration | SSH via config alias |
| Pro7 | Windows worker (tico/choi888) | SSH via WireGuard tunnel |

### Pro7
- Windows 10, WireGuard mesh node
- User: surface, SSH: `ssh pro7`
- Local path: user desktop directory
- SSH config on all mesh nodes (`ssh pro7` works)
