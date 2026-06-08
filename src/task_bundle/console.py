"""Shared Rich console + small output helpers for consistent, readable UX."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

_theme = Theme(
    {
        "ok": "bold green",
        "fail": "bold red",
        "warn": "yellow",
        "info": "cyan",
        "muted": "dim",
        "id": "bold magenta",
    }
)

console = Console(theme=_theme)
err_console = Console(stderr=True, theme=_theme)


def info(msg: str) -> None:
    console.print(f"[info]i[/info] {msg}")


def success(msg: str) -> None:
    console.print(f"[ok]\u2713[/ok] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/warn] {msg}")


def error(msg: str) -> None:
    err_console.print(f"[fail]\u2717[/fail] {msg}")


def status_markup(status: str) -> str:
    """Color a test status string for table rendering."""
    s = status.lower()
    if s == "passed":
        return "[ok]passed[/ok]"
    if s in {"failed", "error"}:
        return f"[fail]{s}[/fail]"
    return f"[muted]{s}[/muted]"
