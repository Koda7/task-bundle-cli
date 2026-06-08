"""Generic runner: the language-agnostic fallback.

When we don't have (or don't want) a framework-specific parser, we fall back to
the universal contract every test runner already honors: the process exit code.
Exit 0 means the invoked tests passed; nonzero means they failed. We attribute
that single verdict to every requested test id.

This is intentionally coarse -- it can't tell you *which* of several tests failed
in one invocation -- but it lets the harness run literally any command
(``make test``, ``cargo test``, ``npm test``, a shell script) with zero parser
work. For per-test granularity with this runner, invoke one test id at a time.
"""

from __future__ import annotations

from task_bundle.runners.base import RunResult, Runner, Status, register_runner


class GenericRunner(Runner):
    name = "generic"

    def parse(
        self, *, stdout: str, stderr: str, exit_code: int, test_ids: list[str]
    ) -> RunResult:
        verdict = Status.passed if exit_code == 0 else Status.failed
        outcomes = {tid: verdict for tid in test_ids}
        # If no specific ids were requested, record a synthetic aggregate.
        if not test_ids:
            outcomes["<all>"] = verdict
        return RunResult(
            outcomes=outcomes,
            exit_code=exit_code,
            raw_output=stdout + "\n" + stderr,
        )


register_runner("generic", GenericRunner)
