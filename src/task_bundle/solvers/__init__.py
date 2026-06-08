"""Solvers: produce a candidate code change for a task inside the sandbox.

Importing this package registers the built-in solvers. Core ships ``golden``
(apply the bundle's oracle patch) and ``stub`` (no-op). The ``openai-agent``
solver is registered lazily because its dependency (``openai``) is optional.
"""

from task_bundle.solvers import golden, stub  # noqa: F401  (register on import)
from task_bundle.solvers.base import (
    Solver,
    SolverResult,
    available_solvers,
    get_solver,
    has_solver,
    register_solver,
)

__all__ = [
    "Solver",
    "SolverResult",
    "available_solvers",
    "get_solver",
    "has_solver",
    "register_solver",
]
