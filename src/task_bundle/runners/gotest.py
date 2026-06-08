"""go test runner: parse ``--- PASS/FAIL: TestName`` lines.

``go test -v`` emits a line per test:

    --- PASS: TestParseResourcePath (0.00s)
    --- FAIL: TestServeHTTP (0.01s)

We map those onto the requested test ids (which for Go are typically the test
function names). Subtests (``TestX/sub``) are matched by suffix.
"""

from __future__ import annotations

import re

from task_bundle.runners.base import RunResult, Runner, Status, register_runner

_LINE_RE = re.compile(r"^\s*---\s+(PASS|FAIL|SKIP):\s+(?P<name>\S+)")

_MAP = {"PASS": Status.passed, "FAIL": Status.failed, "SKIP": Status.missing}


class GoTestRunner(Runner):
    name = "gotest"

    def parse(
        self, *, stdout: str, stderr: str, exit_code: int, test_ids: list[str]
    ) -> RunResult:
        text = stdout + "\n" + stderr
        reported: dict[str, Status] = {}
        for line in text.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            reported[m.group("name")] = _MAP[m.group(1)]

        outcomes: dict[str, Status] = {}
        for tid in test_ids:
            # Go test ids may be "Pkg::TestName" or just "TestName"; match the name.
            key = tid.split("::")[-1]
            status = reported.get(key)
            if status is None:
                # match subtest/suffix
                for name, st in reported.items():
                    if name.endswith(key) or key.endswith(name):
                        status = st
                        break
            outcomes[tid] = status if status is not None else Status.missing
        return RunResult(outcomes=outcomes, exit_code=exit_code, raw_output=text)


register_runner("gotest", GoTestRunner)
