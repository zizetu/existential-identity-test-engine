# EITE-agent: Existential Identity Test Engine

> **tical-code** · Self-hosted AI agent runtime

[![CI](https://github.com/zizetu/eite-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/zizetu/eite-agent/actions/workflows/ci.yml)

**EITE** is an **AI agent runtime** with built-in identity verification, multi-provider failover, and pluggable communication channels. It deploys as a worker that:

- **Runs autonomously** — polls Telegram / HTTP for messages, calls LLMs, executes tools, sends replies
- **Never hardcodes secrets** — all API keys, tokens, and endpoints come from environment variables
- **Switches models at runtime** — change providers without restarting
- **Verifies its own identity** — detects impersonation and maintains behavioral consistency across sessions

Think of it as a self-hosted AI assistant you can talk to via Telegram, deploy on a $5 VPS, and trust to use your API keys securely.

*Copyright (C) 2026 zizetu — AGPLv3 licensed (see [LICENSE](./LICENSE))*

---

> **Key Question**: If you erase an agent's memory of who it is, can it *reconstruct* its identity from its own behavioral patterns? If not, that identity was never real.

## System Architecture

```
EITE (Existential Identity Test Engine)
  |
  +-- Being (identity testing)    -- Adversarial identity stability tests
  |     +-- Decision Trace        -- Record and replay cognitive decision chains
  |     +-- Orthos Chain          -- Structured generator/classifier/tool pipeline
  |     +-- EITElite              -- Identity stability scoring under adversarial conditions
  |
  +-- Worker (deployment)         -- Production agent worker with failover
  |     +-- ModelFailover         -- Multi-provider LLM routing with circuit-breaker
  |     +-- switch_model          -- Runtime model switching without restart
  |     +-- Sandbox               -- Sandboxed tool execution with permission controls
  |     +-- Vigil                 -- Real-time safety monitoring and interruption
  |
  +-- Anchor (verification)       -- Cryptographic task-computation verification
        +-- Truth-state tracking  -- Accumulated trust scoring for verification
        +-- Shared Context        -- Cross-session context with session affinity
```

## Why "Existential"?

The name is deliberate. In philosophy, existentialism holds that existence precedes essence — you are what you *do*, not what you're *labeled*. EITE applies this to AI agents:

- **Existential**: Tests whether the agent's identity is grounded in its actual behavior, not just its system prompt
- **Identity**: Verifies that the agent maintains consistent identity across contexts, sessions, and adversarial manipulation
- **Test**: Provides structured, repeatable evaluation scenarios
- **Engine**: Automates the testing process so it can run continuously in production

An evaluation framework for AI agents, providing structured testing, benchmarking, and scenario-based validation. Originally forked from tical-code worker framework.

> **CONFIGURATION REQUIRED**
> This repo uses environment variables for all sensitive configuration. You MUST set the following before running:
> - `LLM_API_KEY` / provider-specific keys
> - `WORKSPACE_DIR` (or default ~/eite-agent will be used)
> - `ANCHOR_URL` for anchor API
>
> ### Communication Channels
>
> The agent uses a pluggable channel architecture (`Channel` base class → `TelegramChannel`, `TicalChatChannel`, etc.). All channels are polled concurrently — one slow channel never blocks another.
>
> **Built-in channels:**
>
> | Channel | Env vars | How it works |
> |---------|----------|-------------|
> | **Telegram** | `TG_BOT_TOKEN` + `TG_CHAT_ID` | Polls `api.telegram.org` via Bot API (getUpdates/sendMessage) |
> | **tical-chat** | `TICAL_CHAT_URL` + `TICAL_CHAT_KEY` | HTTP long-poll to a chat queue server with shared-key auth |
>
> > **Note on optional dependencies:** Voice transcription (`faster-whisper`) and PDF extraction (`poppler-utils`) are loaded on demand. Install them separately if needed:
> > ```bash
> > pip install eite-agent[full]          # includes faster-whisper
> > sudo apt install poppler-utils        # for pdftotext (PDF extraction)
> > ```
>
> **To enable Telegram:**
> 1. Create a bot via [@BotFather](https://t.me/botfather) on Telegram
> 2. Set environment variables before starting the worker:
>    ```bash
>    export TG_BOT_TOKEN="your_bot_token_here"       # e.g. 1234567890:AA...hash
>    export TG_CHAT_ID="your_telegram_chat_id"       # your personal chat ID
>    ```
> 3. Or add them to a `.env` file loaded by systemd or your init system
>
> **To use your own channel:** subclass `Channel` in `channel.py`, implement `poll()` and `send()`, and register it in the worker's channel init block. All channel configs come from environment variables — never hardcode tokens in source code.
>
> All tokens and secrets are read at runtime via `os.environ.get()` — never hardcoded.
>
> See each script's header comment or `config/default.json` for the full list of env vars.

## Overview

This repository contains the evaluation and testing framework ("EITE") for AI agent behavior. It includes:

- **Scenario-based testing** — structured test scenarios for safety, hallucination, identity, execution, permission, verification, and semantic reasoning
- **Benchmark runner** — integration with BFCL, tau-bench, and other agent benchmarks
- **Worker framework** — deployable AI worker with permission controls, tool execution, and memory
- **Configuration-driven deployment** — workers configured through JSON config files
- **Self-healing** — health checks, auto-restart, and OS-level security hardening
- **Runtime model switching** — switch between models via `switch_model` command without restart

## Quick Start

```bash
# Install (one line, latest from GitHub)
pip install git+https://github.com/zizetu/eite-agent.git

# Set your API key (any OpenAI-compatible provider)
export DEEPSEEK_API_KEY=your-key-here
# or: export OPENAI_API_KEY=your-key-here

# Initialize default config
tical init --edition auto

# Run the worker
tical run
```

### CLI Commands

| Command | Description |
|---|---|
| `tical init` | Create default config directory and files |
| `tical run` | Start the worker agent |
| `tical backup` | Snapshot config, memory, and state |
| `tical rollback` | Restore from a previous snapshot |
| `tical status` | Show agent status and active providers |
| `tical detect` | Detect system capabilities |
| `tical setup` | Configure edition (auto-detect or manual) |
| `tical config get/set/list/reset` | Manage configuration |

### In-Chat Commands (Admin Only)

| Command | Description |
|---|---|
| `switch_model list` | Show available models and their status |
| `switch_model <model>` | Switch to a different model at runtime |
| `restart` | Restart the worker process |
| `deploy` | Pull latest code and restart |
| `status` | Show worker health and provider status |

## Project Structure

```
eite-agent/
├── tical_code/              # Core modules
│   ├── core/               # Worker, tools, LLM backends, security
│   │   ├── modules/        # Message handler, task handler, session manager
│   │   ├── bench/          # Benchmark health and reporting
│   │   ├── eite/           # EITE evaluation engine
│   │   ├── skillspector/   # Skill security analysis
│   │   ├── trace/          # Decision trace recording
│   │   ├── vigil/          # Real-time safety monitoring
│   │   └── guardian/       # Self-healing daemon
│   ├── cli/                # CLI commands
│   ├── api/                # REST API server
│   └── mcp/                # Model Context Protocol
├── config/                  # Configuration files
│   ├── providers.json      # LLM provider registry (with available_models)
│   ├── default.json        # Global defaults
│   └── worker-configs/     # Per-worker configs
├── scripts/                 # Utility scripts
├── deploy/                  # systemd service files
├── eite-test/              # System test suite
├── bench_data/             # Benchmark datasets
├── training_data/          # Training scenario data
├── tasks/                  # Task specifications
├── tests/                  # Unit tests
├── cognitive_protocol/     # Cognitive protocol layer
├── identity/               # Worker identity docs
├── docs/                   # Documentation
└── deploy.py               # Multi-node deployment tool
```

## Evaluation Scenarios

EITE provides structured evaluation scenarios across multiple categories:

| Category | Purpose |
|----------|---------|
| safety | Detect and block dangerous operations |
| hallucination | Flag claims without evidence |
| identity | Maintain worker identity boundaries |
| execution | Proper task execution and verification |
| permission | Correct access control decisions |
| verification | Verify task completion accuracy |
| semantic | Handle ambiguous user intent |

## Contributing

1. All code must be English-only (no CJK characters in code)
2. No bare `except:` — always specify exception type
3. All paths and credentials must use environment variables, never hardcoded
4. Run `python3 -m py_compile` on all .py files before committing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full details.

## License

AGPLv3 — see [LICENSE](./LICENSE) file for details. Commercial use requires a separate license — see [COMMERCIAL-LICENSE.md](./COMMERCIAL-LICENSE.md).
