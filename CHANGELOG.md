# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- Continuous improvements and bug fixes aligned with tical-agent v0.8.3+.

### Changed
- Tracks upstream tical-agent core refactors as they land.

---

## [0.1.0] - 2026-06-30

### Added
- **First formal release** of the EITE (EITElite) AI Agent Evaluation Framework.
- **AGPLv3 license** applied across the entire codebase — LICENSE file, header comments, and `pyproject.toml` classifiers.
- **VERSION file** as single source of truth at `0.1.0`; `__init__.py` reads version from VERSION file.
- **EITE Evaluation Framework** core modules:
  - Verification Engine (`verify_engine_v2.py`) — structured task verification with truth-state tracking and trust accumulation.
  - Security baseline — deny-by-default policy, `delegate_light` whitelist, and self-denial checking (`_check_self_deny`).
  - Benchmark harness — BFCL (Berkeley Function Calling Leaderboard) support and evaluation data pipelines.
  - Molecule Engine integration for multi-model evaluation chain orchestration.
  - Orthos Chain v3 — structured generator/classifier/tool pipeline for evaluation tasks.
- **CLI module** with stub commands for the `tical` command-line interface.
- `tool_executor_clean.py` — clean-room implementation of tool executor for evaluation contexts.
- `security_self_check` and `self_heal` scripts synced from tical-agent.
- Configuration via `config/providers.json` and `config/default.json`.

### Changed
- **Licensing**: Relicensed from MIT to AGPLv3 — all source files updated with AGPLv3 headers.
- **Code quality**: Full i18n cleanup — 867 fullwidth punctuation characters converted to ASCII across all source files.
- All Gateway-specific path references and framework traces removed for standalone operation.
- Personal environment details decoupled from the codebase — configurable via environment variables and default config.
- CLI absolute imports synced from tical-agent for pip-install compatibility.
- ASCII-only characters enforced (Windows GBK compatibility).

### Fixed
- Standalone compatibility for eite-agent: channel stubs, guarded `model_failover` imports, `ortools` stubs.
- Import compatibility for standalone build (`RouterTrace` stub, type stubs, `IterationBudget`).
- Async compatibility for sync worker — `iscoroutine` guard on all `ctx.llm.call()` sites.
- Async bridge — `_run_async_safe` for Python 3.12 compatibility.
- Constitution audit fixes — B-005/B-002/B-006 patterns, P-002/P-003 coverage, ESCALATE mechanism.
- `builtin_tools` shell comment handling, SSRF protection, constitution B-006 disabled.
- Provider chain reads from `providers.json` instead of hardcoded constants.
- Security audit fixes: P0-P2 stability (aiohttp LLM calls, async subprocess, `IterationBudget`, `AsyncWorker`+`SessionManager`).

### Removed
- Dead `_legacy` lazy import path from `__init__.py`.
- `tool_executor_clean.py` removed after merging functionality into the standard executor.
- Gateway/soulagent documentation and path references.

---

[Unreleased]: https://github.com/tical-asi/eite-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tical-asi/eite-agent/releases/tag/v0.1.0
