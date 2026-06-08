"""Solver abstraction.

A *solver* attempts to resolve the task by mutating the repo **inside the sealed
container**. It is given:

* the running ``TaskContainer`` (repo reset to base, hidden tests NOT staged), and
* the ``Bundle`` (so it can read description.md, visible tests, etc.).

Contract: the solver changes files in the container's working tree in place. The
harness then normalizes whatever happened to a single ``git diff`` -- solvers do
NOT return a patch to be re-applied. A solver may report a short transcript/log
for the artifacts.

Crucially, a solver is invoked while hidden tests are ABSENT, so it can never see
the fail2pass / pass2pass tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from task_bundle.bundle import Bundle
from task_bundle.docker_env import TaskContainer


@dataclass
class SolverResult:
    """What a solver reports back (the patch is captured separately by the harness)."""

    ok: bool = True
    message: str = ""
    log: str = ""
    # Optional structured trajectory (used by the agent solver for --analyze).
    transcript: list[dict] = field(default_factory=list)
    steps: int = 0


class Solver:
    """Base class for solvers."""

    name: str = "base"

    def solve(self, container: TaskContainer, bundle: Bundle) -> SolverResult:
        raise NotImplementedError


# --- registry ------------------------------------------------------------

_REGISTRY: dict[str, Callable[..., Solver]] = {}


def register_solver(name: str, factory: Callable[..., Solver]) -> None:
    _REGISTRY[name] = factory


def get_solver(name: str, **kwargs) -> Solver:
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown solver {name!r}; available: {available}")
    return _REGISTRY[name](**kwargs)


def available_solvers() -> list[str]:
    return sorted(_REGISTRY)


def has_solver(name: str) -> bool:
    return name in _REGISTRY
