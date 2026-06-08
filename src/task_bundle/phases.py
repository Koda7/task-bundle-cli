"""Harness phases shared by ``validate`` and ``run``.

A *phase* runs the grading tests against whatever state the repo is currently in
and returns per-test statuses. The orchestration (what state to put the repo in)
lives in the commands; this module just knows how to execute + grade tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from task_bundle import git_ops
from task_bundle.bundle import Bundle
from task_bundle.docker_env import TaskContainer
from task_bundle.runners import Status, get_runner


@dataclass
class PhaseResult:
    """Per-test outcomes for one grading pass, split by bucket."""

    fail_to_pass: dict[str, Status] = field(default_factory=dict)
    pass_to_pass: dict[str, Status] = field(default_factory=dict)
    log: str = ""

    # --- tallies ---------------------------------------------------------

    @property
    def f2p_total(self) -> int:
        return len(self.fail_to_pass)

    @property
    def f2p_passed(self) -> int:
        return sum(1 for s in self.fail_to_pass.values() if s is Status.passed)

    @property
    def p2p_total(self) -> int:
        return len(self.pass_to_pass)

    @property
    def p2p_passed(self) -> int:
        return sum(1 for s in self.pass_to_pass.values() if s is Status.passed)

    @property
    def all_f2p_pass(self) -> bool:
        return self.f2p_total > 0 and self.f2p_passed == self.f2p_total

    @property
    def all_p2p_pass(self) -> bool:
        return self.p2p_passed == self.p2p_total

    @property
    def all_f2p_fail(self) -> bool:
        return self.f2p_total > 0 and self.f2p_passed == 0

    @property
    def resolved(self) -> bool:
        """A solution is resolved iff every F2P passes AND every P2P passes."""
        return self.all_f2p_pass and self.all_p2p_pass

    def db_rows(self) -> list[tuple[str, str, str]]:
        """Rows for db.record_test_results: (bucket, test_id, status)."""
        rows: list[tuple[str, str, str]] = []
        for tid, st in self.fail_to_pass.items():
            rows.append(("fail_to_pass", tid, st.value))
        for tid, st in self.pass_to_pass.items():
            rows.append(("pass_to_pass", tid, st.value))
        return rows


def run_grading_tests(container: TaskContainer, bundle: Bundle) -> PhaseResult:
    """Run the fail2pass and pass2pass tests and collect per-test statuses.

    Tests are run together (one invocation) for speed; the runner attributes
    outcomes back to individual ids. Assumes hidden tests are already staged.
    """
    spec = bundle.spec
    runner = get_runner(spec.test.runner.value)
    all_tests = list(spec.grading.fail_to_pass) + list(spec.grading.pass_to_pass)
    if not all_tests:
        return PhaseResult(log="(no grading tests defined)")

    cmd = runner.build_command(spec.test.command_template, all_tests)
    res = container.bash(cmd, timeout=spec.test.timeout_seconds)
    parsed = runner.parse(
        stdout=res.stdout,
        stderr=res.stderr,
        exit_code=res.returncode,
        test_ids=all_tests,
    )

    f2p = {t: parsed.status_for(t) for t in spec.grading.fail_to_pass}
    p2p = {t: parsed.status_for(t) for t in spec.grading.pass_to_pass}
    log = f"$ {cmd}\n(exit {res.returncode}{', TIMED OUT' if res.timed_out else ''})\n\n" + (
        res.stdout + ("\n[stderr]\n" + res.stderr if res.stderr.strip() else "")
    )
    return PhaseResult(fail_to_pass=f2p, pass_to_pass=p2p, log=log)


def prepare_baseline(container: TaskContainer, bundle: Bundle) -> None:
    """Reset to base, then stage hidden tests (no code patch).

    This is the state for the baseline guardrail: hidden tests present but the
    fix NOT applied -> F2P should fail, P2P should pass.
    """
    git_ops.reset_to_base(container, bundle.spec)
    hidden = bundle.read_hidden_patch() if bundle.has_hidden_patch() else ""
    git_ops.stage_hidden_tests(container, bundle.spec, hidden)


def apply_golden(container: TaskContainer, bundle: Bundle) -> None:
    """Apply the golden patch on top of the current state."""
    git_ops.apply_patch(container, bundle.read_patch())
