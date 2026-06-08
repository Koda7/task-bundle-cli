"""Golden solver: apply the bundle's oracle patch.

This is the deterministic ground-truth solver. It applies ``patch.diff`` inside
the container, which (for a sound task) makes every fail2pass test pass. Its job
is to prove the harness itself works end-to-end -- if golden doesn't resolve a
task, the bundle or harness is broken, not the model.
"""

from __future__ import annotations

from task_bundle import git_ops
from task_bundle.bundle import Bundle
from task_bundle.docker_env import TaskContainer
from task_bundle.solvers.base import Solver, SolverResult, register_solver


class GoldenSolver(Solver):
    name = "golden"

    def solve(self, container: TaskContainer, bundle: Bundle) -> SolverResult:
        if not bundle.has_golden_patch():
            return SolverResult(
                ok=False,
                message="bundle has no golden patch (patch.diff is empty)",
                log="golden solver requires a non-empty patch.diff",
            )
        try:
            res = git_ops.apply_patch(container, bundle.read_patch())
        except git_ops.GitError as e:
            return SolverResult(ok=False, message="golden patch did not apply", log=str(e))
        return SolverResult(
            ok=True,
            message="applied golden patch.diff",
            log=res.stdout + res.stderr,
            steps=1,
        )


register_solver("golden", GoldenSolver)
