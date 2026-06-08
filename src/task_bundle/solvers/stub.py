"""Stub solver: do nothing.

The no-op solver leaves the repo at its baseline state. Running it should produce
the baseline outcome: fail2pass tests FAIL (the bug is unfixed) while pass2pass
tests PASS. It's the control case -- proof that the harness reports an UNRESOLVED
task correctly, and a zero-cost smoke test of the full run/grade pipeline.
"""

from __future__ import annotations

from task_bundle.bundle import Bundle
from task_bundle.docker_env import TaskContainer
from task_bundle.solvers.base import Solver, SolverResult, register_solver


class StubSolver(Solver):
    name = "stub"

    def solve(self, container: TaskContainer, bundle: Bundle) -> SolverResult:
        return SolverResult(
            ok=True,
            message="stub solver made no changes",
            log="(no-op: repo left at baseline)",
            steps=0,
        )


register_solver("stub", StubSolver)
