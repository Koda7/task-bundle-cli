"""Subprocess helpers with verbatim command logging.

Every external command (docker, git) is run through here so we can (a) capture a
literal, copy-pasteable record of what executed and (b) keep a single place for
timeout handling. This logging is a deliberate reproducibility feature: the
``meta.json`` for a run contains the exact commands a human could replay.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def display(self) -> str:
        """A shell-quoted, copy-pasteable rendering of the command."""
        return " ".join(shlex.quote(a) for a in self.args)


@dataclass
class CommandRecorder:
    """Accumulates a transcript of every command run, for artifacts/meta.json."""

    entries: list[dict] = field(default_factory=list)

    def add(self, result: CommandResult) -> None:
        self.entries.append(
            {
                "cmd": result.display,
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            }
        )

    def as_list(self) -> list[dict]:
        return list(self.entries)


def run(
    args: list[str],
    *,
    timeout: float | None = None,
    input_text: str | None = None,
    recorder: CommandRecorder | None = None,
) -> CommandResult:
    """Run a command, capturing stdout/stderr; never raises on nonzero exit."""
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = 124  # conventional timeout exit code
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")) + (
            f"\n[timed out after {timeout}s]"
        )
    result = CommandResult(
        args=args,
        returncode=rc,
        stdout=out,
        stderr=err,
        duration_ms=int((time.time() - start) * 1000),
        timed_out=timed_out,
    )
    if recorder is not None:
        recorder.add(result)
    return result
