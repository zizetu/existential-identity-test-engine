# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Central data-home path resolution.

All persistent agent data (memory, sessions, logs, skills, snapshots, …)
lives under a single home directory resolved as:

  1. ``TICAL_HOME`` environment variable (preferred)
  2. ``EITE_DATA_DIR`` environment variable (legacy alias)
  3. ``$HOME/.EITElite`` (default when neither env is set)

Use :func:`get_tical_home`, :func:`under_tical_home`, and
:func:`expand_data_path` instead of hardcoding the default data-home path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

_LEGACY_DIRNAME = ".EITElite"


def get_tical_home() -> Path:
    """Return the resolved data-home directory (does not create it)."""
    for key in ("TICAL_HOME", "EITE_DATA_DIR"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return Path(os.path.expanduser(raw)).expanduser()
    return Path.home() / _LEGACY_DIRNAME


def tical_home_str() -> str:
    """String form of :func:`get_tical_home`."""
    return str(get_tical_home())


def under_tical_home(*parts: str) -> str:
    """Join *parts* under the data home and return as string."""
    return str(get_tical_home().joinpath(*parts))


def ensure_tical_home(*parts: str) -> Path:
    """Return path under data home, creating parent directories as needed."""
    path = get_tical_home().joinpath(*parts) if parts else get_tical_home()
    path.mkdir(parents=True, exist_ok=True)
    return path


def expand_data_path(path: Union[str, Path]) -> str:
    """Expand user/env and rewrite legacy default data-home to TICAL_HOME.

    Accepts absolute paths, home-relative paths, and the literal legacy
    prefix ``$HOME/.EITElite``.  When the path points at (or under) the
    legacy default directory but ``TICAL_HOME`` is set, the path is
    rewritten to the configured home.
    """
    if path is None:
        return tical_home_str()
    raw = str(path)
    # Normalize common tilde form first
    expanded = os.path.expanduser(raw)
    legacy = str(Path.home() / _LEGACY_DIRNAME)
    home = tical_home_str()
    if home != legacy:
        if expanded == legacy:
            return home
        prefix = legacy + os.sep
        if expanded.startswith(prefix):
            return os.path.join(home, expanded[len(prefix):])
        # Also handle forward-slash legacy on Windows-mixed inputs
        legacy_fwd = legacy.replace("\\", "/")
        expanded_fwd = expanded.replace("\\", "/")
        if expanded_fwd == legacy_fwd:
            return home
        if expanded_fwd.startswith(legacy_fwd + "/"):
            rel = expanded_fwd[len(legacy_fwd) + 1:]
            return str(Path(home) / rel.replace("/", os.sep))
    return expanded


def get_guardian_dir() -> Path:
    """Guardian/iron-wall state directory.

    ``TICAL_GUARDIAN_DIR`` overrides; otherwise ``<TICAL_HOME>/guardian``.
    """
    raw = (os.environ.get("TICAL_GUARDIAN_DIR") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return get_tical_home() / "guardian"
