"""Locate the local task store (``.taskbundle/``).

The store holds the SQLite DB and per-run artifacts. We resolve it once, with an
env override for tests and CI:

    TASKBUNDLE_HOME  -- absolute path to the store (default: ./.taskbundle)

Keeping all runtime state under a single, git-ignored directory makes the tool
self-contained and easy to reason about.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "TASKBUNDLE_HOME"
DEFAULT_DIRNAME = ".taskbundle"


def store_root() -> Path:
    """Return the task store directory, creating it if needed."""
    override = os.environ.get(ENV_HOME)
    root = Path(override).expanduser() if override else Path.cwd() / DEFAULT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return store_root() / "db.sqlite3"


def runs_dir() -> Path:
    d = store_root() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(run_id: str) -> Path:
    """Artifact directory for a single run (report.json, logs, patch, meta)."""
    d = runs_dir() / run_id
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d
