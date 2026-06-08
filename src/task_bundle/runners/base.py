"""Runner abstraction: build a test command and parse its output.

A *runner* knows how to (a) turn a list of test ids into a shell command and
(b) parse that command's stdout/stderr + exit code into per-test outcomes. This
plugin boundary is what makes the harness language-agnostic: pytest, go test,
jest, or anything else is just another runner. The ``generic`` runner needs no
parser at all -- it maps the process exit code onto the requested tests.
"""

from __future__ import annotations

import enum
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional


class Status(str, enum.Enum):
    passed = "passed"
    failed = "failed"
    error = "error"
    missing = "missing"  # test was requested but not reported by the framework


@dataclass
class TestOutcome:
    test_id: str
    status: Status


@dataclass
class RunResult:
    """Parsed result of one test invocation."""

    outcomes: dict[str, Status] = field(default_factory=dict)
    exit_code: int = 0
    raw_output: str = ""
    timed_out: bool = False

    def status_for(self, test_id: str) -> Status:
        return self.outcomes.get(test_id, Status.missing)


class Runner:
    """Base class for test runners."""

    name: str = "base"

    def build_command(self, command_template: str, test_ids: list[str]) -> str:
        """Render the shell command for the given tests."""
        quoted = " ".join(shlex.quote(t) for t in test_ids)
        if "{tests}" in command_template:
            return command_template.format(tests=quoted)
        return f"{command_template} {quoted}".strip()

    def parse(self, *, stdout: str, stderr: str, exit_code: int, test_ids: list[str]) -> RunResult:
        raise NotImplementedError


# --- registry ------------------------------------------------------------

_REGISTRY: dict[str, Callable[[], Runner]] = {}


def register_runner(name: str, factory: Callable[[], Runner]) -> None:
    _REGISTRY[name] = factory


def get_runner(name: str) -> Runner:
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown runner {name!r}; available: {available}")
    return _REGISTRY[name]()


def available_runners() -> list[str]:
    return sorted(_REGISTRY)
