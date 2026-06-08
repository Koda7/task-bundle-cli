"""Rendering for the DB-query commands (runs, log, report)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from rich.table import Table

from task_bundle import db
from task_bundle.console import console, error, info, warn
from task_bundle.console import status_markup as sm


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def render_runs(conn: sqlite3.Connection, limit: int) -> None:
    rows = db.list_runs(conn, limit=limit)
    if not rows:
        info("No runs recorded yet. Try: task run <bundle> --solver golden")
        return
    t = Table(title="Runs")
    t.add_column("run_id", style="id")
    t.add_column("when", style="dim")
    t.add_column("bundle")
    t.add_column("solver")
    t.add_column("model", style="dim")
    t.add_column("resolved")
    t.add_column("f2p")
    t.add_column("p2p")
    for r in rows:
        resolved = "[ok]yes[/ok]" if r["resolved"] else "[fail]no[/fail]"
        t.add_row(
            r["run_id"],
            _fmt_ts(r["ts"]),
            Path(r["bundle"]).name,
            r["solver"],
            r["model"] or "-",
            resolved,
            f"{r['f2p_passed']}/{r['f2p_total']}",
            f"{r['p2p_passed']}/{r['p2p_total']}",
        )
    console.print(t)


def render_commands(conn: sqlite3.Connection, limit: int) -> None:
    rows = db.list_commands(conn, limit=limit)
    if not rows:
        info("No commands recorded yet.")
        return
    t = Table(title="Command log")
    t.add_column("cid", style="id")
    t.add_column("when", style="dim")
    t.add_column("command")
    t.add_column("bundle", style="dim")
    t.add_column("status")
    t.add_column("ms", justify="right")
    for r in rows:
        status = r["status"]
        status_m = (
            "[ok]ok[/ok]"
            if status == "ok"
            else ("[fail]error[/fail]" if status == "error" else f"[warn]{status}[/warn]")
        )
        t.add_row(
            r["cid"],
            _fmt_ts(r["ts"]),
            r["command"],
            Path(r["bundle"]).name if r["bundle"] else "-",
            status_m,
            str(r["duration_ms"] or ""),
        )
    console.print(t)


def render_log(conn: sqlite3.Connection, ident: str) -> bool:
    """Show the log for a command id OR a run id. Returns False if not found."""
    # Try command first.
    crow = db.get_command(conn, ident)
    if crow is not None:
        _render_command_detail(conn, crow)
        return True
    rrow = db.get_run(conn, ident)
    if rrow is not None:
        _render_run_detail(conn, rrow)
        return True
    error(f"no command or run found with id {ident!r}")
    return False


def _render_command_detail(conn: sqlite3.Connection, crow: sqlite3.Row) -> None:
    console.print(f"[id]{crow['cid']}[/id]  [dim]{_fmt_ts(crow['ts'])}[/dim]")
    console.print(f"  command   : {crow['command']}")
    console.print(f"  status    : {crow['status']}  (exit {crow['exit_code']})")
    console.print(f"  duration  : {crow['duration_ms']} ms")
    console.print(f"  bundle    : {crow['bundle'] or '-'}")
    console.print(f"  image     : {crow['image'] or '-'}")
    if crow["image_digest"]:
        console.print(f"  digest    : {crow['image_digest']}")
    console.print(f"  host      : {crow['host'] or '-'}")
    if crow["message"]:
        console.print(f"  message   : {crow['message']}")
    try:
        args = json.loads(crow["args_json"])
        console.print(f"  args      : {json.dumps(args)}")
    except (json.JSONDecodeError, TypeError):
        pass

    runs = db.get_runs_for_command(conn, crow["cid"])
    if runs:
        console.print("\n  runs produced by this command:")
        for r in runs:
            console.print(
                f"    [id]{r['run_id']}[/id]  solver={r['solver']}  "
                f"resolved={'yes' if r['resolved'] else 'no'}  "
                f"(f2p {r['f2p_passed']}/{r['f2p_total']}, p2p {r['p2p_passed']}/{r['p2p_total']})"
            )
        console.print("\n  Tip: [info]task log <run_id>[/info] for per-test detail.")


def _render_run_detail(conn: sqlite3.Connection, rrow: sqlite3.Row) -> None:
    console.print(f"[id]{rrow['run_id']}[/id]  [dim]{_fmt_ts(rrow['ts'])}[/dim]")
    console.print(f"  command   : {rrow['cid']}")
    console.print(f"  bundle    : {Path(rrow['bundle']).name}")
    console.print(f"  instance  : {rrow['instance_id'] or '-'}")
    console.print(f"  solver    : {rrow['solver']}  model={rrow['model'] or '-'}")
    resolved = "[ok]RESOLVED[/ok]" if rrow["resolved"] else "[fail]UNRESOLVED[/fail]"
    console.print(
        f"  verdict   : {resolved}  "
        f"(fail2pass {rrow['f2p_passed']}/{rrow['f2p_total']}, "
        f"pass2pass {rrow['p2p_passed']}/{rrow['p2p_total']})"
    )
    if rrow["artifact_path"]:
        console.print(f"  report    : {rrow['artifact_path']}")

    results = db.get_test_results(conn, rrow["run_id"])
    if not results:
        return
    # Group by phase for readability.
    phases_seen: dict[str, list[sqlite3.Row]] = {}
    for r in results:
        phases_seen.setdefault(r["phase"], []).append(r)
    for phase, rows in phases_seen.items():
        t = Table(title=f"phase: {phase}")
        t.add_column("bucket")
        t.add_column("test")
        t.add_column("status")
        for r in rows:
            t.add_row(r["bucket"], _short(r["test_id"]), sm(r["status"]))
        console.print(t)


def render_report(conn: sqlite3.Connection, run_id: str) -> bool:
    rrow = db.get_run(conn, run_id)
    if rrow is None:
        error(f"no run found with id {run_id!r}")
        return False
    path = rrow["artifact_path"]
    if not path or not Path(path).is_file():
        warn(f"run {run_id} has no report.json artifact on disk; showing DB summary")
        _render_run_detail(conn, rrow)
        return True
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    console.print_json(json.dumps(data))
    return True


def _short(test_id: str, width: int = 64) -> str:
    if len(test_id) <= width:
        return test_id
    return "..." + test_id[-(width - 3):]
