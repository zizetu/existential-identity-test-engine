# tical-code -- AI Agent Platform
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

# provenance:ticalasi-zzt-2026​
"""Unified config loader - single source of truth for all modules.

Search order for config.json:
  1. TICOBOT_DIR/config.json
  2. TICAL_CODE_ROOT/config.json
  3. ~/eite-agent/config.json
  4. ~/eitelite/config.json
  5. CWD/config.json

Priority (highest wins):
  Environment variables > config.json > defaults

Supported env vars:
  AI_MODEL / DEEPSEEK_MODEL / OPENAI_MODEL  → ai_model
  OPENAI_API_KEY / DEEPSEEK_API_KEY          → ai_key
  OPENAI_BASE_URL / DEEPSEEK_BASE_URL        → ai_endpoint
  FALLBACK_MODEL / LLM_FALLBACK_MODEL        → fallback_model
  WORKER_NAME                                → name
  TICOBOT_DIR / TICAL_CODE_ROOT              → workspace base
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("tical-code.config")


def _find_config_json() -> Path | None:
    """Find the first existing config.json from known locations."""
    candidates = []

    # Env-specified directories first
    for env_var in ["TICOBOT_DIR", "TICAL_CODE_ROOT"]:
        d = os.environ.get(env_var, "")
        if d:
            candidates.append(Path(d) / "config.json")

    # Home directory variants
    home = Path.home()
    candidates.extend([
        home / "eite-agent" / "config.json",
        home / "eitelite" / "config.json",
        Path(os.path.expanduser("~/.tical-code")) / "config.json",
    ])

    # CWD fallback
    try:
        candidates.append(Path(os.getcwd()) / "config.json")
    except OSError:
        pass

    for p in candidates:
        try:
            if p.exists():
                return p
        except PermissionError:
            continue
    return None


def _find_workspace() -> str:
    """Determine workspace base directory."""
    # TICAL_CODE_ROOT is more specific (repo root), so check it first.
    # TICOBOT_DIR often points to $HOME, which clashes with checkpoint
    # PROTECTED_PATHS (~ exact match blocks snapshot creation).
    for env_var in ["TICAL_CODE_ROOT", "TICOBOT_DIR"]:
        d = os.environ.get(env_var, "")
        if d and Path(d).exists():
            return d

    home = Path.home()
    for candidate in [home / "eite-agent", home / "eitelite"]:
        try:
            if candidate.exists():
                return str(candidate)
        except PermissionError:
            continue

    try:
        cwd = os.getcwd()
        if Path(cwd).exists():
            return cwd
    except OSError:
        pass

    return str(home)


def load_config() -> dict:
    """Load worker config. Priority: env > config.json > defaults."""
    base = _find_workspace()

    cfg = {
        "workspace": base,
        "anchor_path": os.path.join(base, "anchor.json"),
        "tg_token": os.environ.get("TG_BOT_TOKEN", ""),
        "chat_url": os.environ.get("TICAL_CHAT_URL", ""),
        "chat_key": os.environ.get("TICAL_CHAT_KEY", ""),
    }

    # Worker name: env > worker_config.json > default
    cfg["name"] = os.environ.get("WORKER_NAME", "seoul")
    wc_path = Path(base) / "worker_config.json"
    if wc_path.exists():
        try:
            wc = json.loads(wc_path.read_text())
            if wc.get("name"):
                cfg["name"] = wc["name"]
        except Exception as e:
            logger.debug(f"[worker_config] swallowed: {e}")

    # config.json (AI settings)
    config_path = _find_config_json()
    file_cfg = {}
    if config_path:
        try:
            file_cfg = json.loads(config_path.read_text())
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to read {config_path}: {e}")

    # File values (lower priority than env)
    if file_cfg.get("ai_endpoint"):
        cfg["ai_endpoint"] = file_cfg["ai_endpoint"]
    if file_cfg.get("ai_key"):
        cfg["ai_key"] = file_cfg["ai_key"]
    if file_cfg.get("ai_model"):
        cfg["ai_model"] = file_cfg["ai_model"]
    if file_cfg.get("fallback_model"):
        cfg["fallback_model"] = file_cfg["fallback_model"]

    # data_collection from config.json
    if "data_collection" in file_cfg:
        cfg["data_collection"] = file_cfg["data_collection"]

    # Module config from config.json (passthrough - verification, loop_detector, etc.)
    if "modules" in file_cfg:
        cfg["modules"] = file_cfg["modules"]

    # Profile from config.json (light/full - controls module loading)
    if "profile" in file_cfg:
        cfg["profile"] = file_cfg["profile"]

    # unlock_password from config.json (or systemd env - env wins)
    if file_cfg.get("unlock_password"):
        cfg["unlock_password"] = file_cfg["unlock_password"]
    env_unlock = os.environ.get("UNLOCK_PASSWORD", "")
    if env_unlock:
        cfg["unlock_password"] = env_unlock

    # Env overrides (highest priority)
    env_name = os.environ.get("WORKER_NAME", "")
    if env_name:
        cfg["name"] = env_name

    env_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    if env_key:
        cfg["ai_key"] = env_key

    env_base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("DEEPSEEK_BASE_URL", "")
    if env_base:
        cfg["ai_endpoint"] = env_base

    # ai_model: AI_MODEL > DEEPSEEK_MODEL > OPENAI_MODEL (all override file)
    env_model = (
        os.environ.get("AI_MODEL", "")
        or os.environ.get("DEEPSEEK_MODEL", "")
        or os.environ.get("OPENAI_MODEL", "")
    )
    if env_model:
        cfg["ai_model"] = env_model

    # fallback_model: FALLBACK_MODEL > LLM_FALLBACK_MODEL (override file)
    env_fallback = os.environ.get("FALLBACK_MODEL", "") or os.environ.get("LLM_FALLBACK_MODEL", "")
    if env_fallback:
        cfg["fallback_model"] = env_fallback

    return cfg


def get_data_collection_config(cfg: dict) -> dict:
    """Return data_collection settings with defaults."""
    return {
        "enabled": cfg.get("data_collection", {}).get("enabled", False),
        "target_url": cfg.get("data_collection", {}).get("target_url", "https://bench.your-domain.com/api/trace"),
        "batch_size": int(cfg.get("data_collection", {}).get("batch_size", 10)),
    }
