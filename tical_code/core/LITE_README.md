# EITElite — Lightweight AI Worker for 1C1G VPS

Stripped from tical-code full system. Keeps EITE core: verification, anti-hallucination, loop detection, behavioral constraints.

## What's Included (~10K lines)

| Module | Function |
|--------|----------|
| unified_worker.py | Main poll→LLM→tool→reply loop |
| verify_engine_v2.py | EITE verification engine (3-phase) |
| truthful_reporting.py | Lie detection + trust tracking |
| constitution.py | Behavioral constraints pre-tool |
| doom_loop.py | 4-detector loop detection + recovery |
| verification_recorder.py | Verification event recording |
| tool_executor.py | Bash/file/web tool execution + security |
| model_failover.py | Multi-provider LLM failover |
| llm_backend.py | LLM API backend |
| channel.py | Telegram + TicalChat adapters |
| session_manager.py | SQLite session persistence |
| context_compactor.py | Context window management |
| security_baseline.py | Path/URL/outbound security |
| trace_recorder.py | Training data collection |
| usage.py | Token usage tracking |
| config.py | Unified config loader |
| prompt.py | System prompt builder |
| response_formatter.py | Response formatting |

## What's Removed (available in tical-code)

self_repair, checkpoint, sandbox, worker_framework, decision_engine, hive, workflow,
cron, subagent, memory_evolve, builtin_tools, eval, reflection, enhanced_router,
cron_scheduler, session_snapshot, prompt_generator, tool_call_parser, web_sense,
memory_sense, memory_store, all DESIGNED-NOT-DEAD modules, _legacy, plugins,
benchmarks, cli, vigil

## Deployment

- Oracle and Test VPS run EITElite
- Kael and Taiwan VPS run tical-code full
