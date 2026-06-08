"""Lightweight SQLite store for command logs and run results.

Schema (3 tables):

* ``commands``      -- one row per CLI invocation: what ran, args, outcome,
                       which image, timing. The "brief log" the assignment asks
                       for. Has a human-readable ``cid`` you can query.
* ``runs``          -- one row per ``task run``: solver, model, resolved?, and
                       fail2pass / pass2pass tallies. Linked to its command.
* ``test_results``  -- one row per (run, test): exact pass/fail per test id, so a
                       collaborator can answer "which tests failed in run X?"
                       without reproducing anything.

Everything uses stdlib ``sqlite3`` -- zero setup, single file, trivially shippable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from task_bundle.paths import db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    cid           TEXT PRIMARY KEY,        -- short human-friendly id (e.g. cmd_ab12cd34)
    ts            REAL NOT NULL,           -- epoch seconds (start)
    command       TEXT NOT NULL,           -- 'init' | 'validate' | 'run' | ...
    args_json     TEXT NOT NULL,           -- JSON of the invocation's args/flags
    bundle        TEXT,                    -- bundle path/name, if applicable
    status        TEXT NOT NULL,           -- 'running' | 'ok' | 'error'
    exit_code     INTEGER,                 -- process-style exit code (0 ok)
    duration_ms   INTEGER,
    message       TEXT,                    -- short human summary or error
    image         TEXT,
    image_digest  TEXT,
    host          TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,        -- e.g. run_ab12cd34
    cid           TEXT NOT NULL REFERENCES commands(cid),
    ts            REAL NOT NULL,
    bundle        TEXT NOT NULL,
    instance_id   TEXT,
    solver        TEXT NOT NULL,
    model         TEXT,
    resolved      INTEGER NOT NULL,        -- 1 if fail2pass all pass AND pass2pass all pass
    f2p_total     INTEGER NOT NULL,
    f2p_passed    INTEGER NOT NULL,
    p2p_total     INTEGER NOT NULL,
    p2p_passed    INTEGER NOT NULL,
    artifact_path TEXT,                    -- path to report.json
    duration_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS test_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    phase     TEXT NOT NULL,               -- 'baseline' | 'golden' | 'post_solver'
    bucket    TEXT NOT NULL,               -- 'fail_to_pass' | 'pass_to_pass'
    test_id   TEXT NOT NULL,
    status    TEXT NOT NULL                -- 'passed' | 'failed' | 'error' | 'missing'
);

CREATE INDEX IF NOT EXISTS idx_runs_cid ON runs(cid);
CREATE INDEX IF NOT EXISTS idx_tr_run ON test_results(run_id);
"""


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and lazily initialize) the database."""
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


@dataclass
class CommandLog:
    """Handle for an in-progress command row; finalize via ``finish``."""

    conn: sqlite3.Connection
    cid: str
    _start: float

    def set_image(self, image: Optional[str], digest: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE commands SET image = ?, image_digest = ? WHERE cid = ?",
            (image, digest, self.cid),
        )
        self.conn.commit()

    def finish(
        self, *, status: str, exit_code: int, message: str = ""
    ) -> None:
        self.conn.execute(
            "UPDATE commands SET status = ?, exit_code = ?, message = ?, duration_ms = ? "
            "WHERE cid = ?",
            (
                status,
                exit_code,
                message,
                int((time.time() - self._start) * 1000),
                self.cid,
            ),
        )
        self.conn.commit()


def start_command(
    conn: sqlite3.Connection,
    *,
    command: str,
    args: dict[str, Any],
    bundle: Optional[str] = None,
    host: Optional[str] = None,
) -> CommandLog:
    """Insert a 'running' command row and return a handle to finalize it."""
    cid = _short_id("cmd")
    start = time.time()
    conn.execute(
        "INSERT INTO commands (cid, ts, command, args_json, bundle, status, host) "
        "VALUES (?, ?, ?, ?, ?, 'running', ?)",
        (cid, start, command, json.dumps(args, default=str), bundle, host),
    )
    conn.commit()
    return CommandLog(conn=conn, cid=cid, _start=start)


def record_run(
    conn: sqlite3.Connection,
    *,
    cid: str,
    bundle: str,
    instance_id: Optional[str],
    solver: str,
    model: Optional[str],
    resolved: bool,
    f2p_total: int,
    f2p_passed: int,
    p2p_total: int,
    p2p_passed: int,
    artifact_path: Optional[str],
    duration_ms: int,
) -> str:
    """Insert a run summary row; returns the new run_id."""
    run_id = _short_id("run")
    conn.execute(
        "INSERT INTO runs (run_id, cid, ts, bundle, instance_id, solver, model, resolved, "
        "f2p_total, f2p_passed, p2p_total, p2p_passed, artifact_path, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            cid,
            time.time(),
            bundle,
            instance_id,
            solver,
            model,
            1 if resolved else 0,
            f2p_total,
            f2p_passed,
            p2p_total,
            p2p_passed,
            artifact_path,
            duration_ms,
        ),
    )
    conn.commit()
    return run_id


def record_test_results(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    phase: str,
    rows: list[tuple[str, str, str]],
) -> None:
    """Bulk insert per-test rows. ``rows`` = list of (bucket, test_id, status)."""
    conn.executemany(
        "INSERT INTO test_results (run_id, phase, bucket, test_id, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, phase, bucket, tid, status) for (bucket, tid, status) in rows],
    )
    conn.commit()


# --- queries -------------------------------------------------------------


def get_command(conn: sqlite3.Connection, cid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM commands WHERE cid = ?", (cid,)).fetchone()


def list_commands(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM commands ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def get_runs_for_command(conn: sqlite3.Connection, cid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE cid = ? ORDER BY ts", (cid,)
    ).fetchall()


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()


def get_test_results(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM test_results WHERE run_id = ? ORDER BY phase, bucket, test_id",
        (run_id,),
    ).fetchall()


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
