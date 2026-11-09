"""Runtime persistence path resolution."""

from __future__ import annotations

import os
from pathlib import Path


def default_data_root(environment: dict[str, str] | None = None) -> Path:
    """Resolve a state root outside the repository by configuration/XDG convention."""

    env = os.environ if environment is None else environment
    if configured := env.get("KASANE_DATA_ROOT"):
        return Path(configured).expanduser()
    if xdg_state := env.get("XDG_STATE_HOME"):
        return Path(xdg_state).expanduser() / "kasane"
    return Path.home() / ".local" / "state" / "kasane"


def default_database_path(environment: dict[str, str] | None = None) -> Path:
    return default_data_root(environment) / "vault.db"
