# Module Status Map

> Updated 2026-06-08 02:12. All modules now wired into unified_worker.py.

## Active Modules (directly imported & wired by unified_worker.py)

| Module | Lines | Function | Wire Point |
|--------|-------|----------|------------|
| unified_worker.py | ~1200 | Main worker loop | — |
| tool_executor.py | 898 | Bash/file/web tool execution + security | tool call phase |
| model_failover.py | 392 | LLM provider failover chain | Worker init |
| llm_backend.py | 314 | LLM API backend abstraction | Worker init fallback |
| config.py | 174 | Unified config loader | Worker init |
| prompt.py | ~108 | System prompt builder | LLM call |
| channel.py | 346 | Telegram + TicalChat channel adapters | Worker init |
| response_formatter.py | 64 | Response formatting | tool output |
| modules/session_manager.py | 259 | SQLite session persistence + cleanup | message save |
| modules/context_compactor.py | 149 | Context window management | LLM call |
| security_baseline.py | 775 | Path/URL/outbound security policy | tool_executor + init |
| usage.py | 672 | Token usage tracking | Worker init |
| eite/verify_engine_v2.py | 628 | EITE verification engine | tool verify |
| trace_recorder.py | 236 | Trace data collection | task lifecycle |
| trace/verification_recorder.py | 161 | Verification event recording | verify phase |
| doom_loop.py | 925 | 4-detector loop detection + recovery | tool call + outcome |
| truthful_reporting.py | 1152 | Lie detection + trust tracking | reply phase |
| constitution.py | 1308 | Behavioral constraints/rules engine | pre-tool execution |
| self_repair.py | 2471 | Auto health check + code repair | periodic (100 msgs) |
| checkpoint.py | 1413 | State snapshot + restore | periodic (50 msgs) |
| sandbox.py | 906 | Python code sandbox | self_repair dependency |
| reflection.py | 736 | Task quality reflection | Worker init (OFF default) |
| enhanced_router.py | 640 | Smart model routing | Worker init (OFF default) |
| memory_store.py | 628 | FTS5 full-text memory search | Worker init (ON default) |
| cron_scheduler.py | 750 | Scheduled task execution | Worker init (OFF default) |
| session_snapshot.py | 516 | Session crash recovery | startup + timeout |
| prompt_generator.py | 335 | Dynamic prompt generation | import ready |
| tool_call_parser.py | 307 | Robust tool call parsing | import ready |
| eval.py | 844 | Honesty evaluation | import ready |
| web_sense.py | 336 | Safe web fetching | import ready |
| memory_sense.py | 366 | Memory indexing + search | import ready |

## Legacy Modules (in _legacy/, replaced by active modules)

| Module | Lines | Replaced By |
|--------|-------|-------------|
| worker_loop.py | 1887 | unified_worker.py |
| model_router.py | — | model_failover.py + enhanced_router.py |
| session.py | — | modules/session_manager.py |
| trace.py | — | trace_recorder.py |
| verify_pipeline.py | — | eite/verify_engine_v2.py |
| compaction.py | — | modules/context_compactor.py |
| llm_interface.py | — | llm_backend.py |
| worker.py | — | unified_worker.py |

## Third-Tier Modules (concept/stub, not yet wired)

| Module | Lines | Purpose |
|--------|-------|---------|
| decision_engine.py | 2085 | Multi-criteria decision (needs axioms) |
| hive.py | 1978 | Multi-worker coordination |
| axioms.py | 381 | Core axioms for decision engine |
| memory_evolve.py | 1026 | Memory evolution (needs agent_runtime) |
| plugin_interface.py | 461 | Plugin system interface |
| detection.py | 329 | Anomaly detection |
| state_sense.py | 228 | State perception |
| anchor.py | 388 | Identity anchoring |
| errors.py | 430 | Error taxonomy |
| identity.py | 169 | Agent identity |
