"""pytest runner: parse the ``-rA`` short-test-summary into per-test outcomes.

We rely on pytest's ``-rA`` flag (present in the default command template), which
prints an explicit per-test summary section:

    PASSED path/to/test.py::test_a
    FAILED path/to/test.py::test_b
    ERROR  path/to/test.py::test_c

This is robust to:
* ``pytest-rerunfailures`` (the summary reflects the FINAL status; intermediate
  "rerun" lines appear in the progress area, not the ``-rA`` summary), and
* parametrized ids like ``test_x[a-b-c]``.

We match on the requested test ids so unrelated collected tests don't pollute the
result, and we substring-match to tolerate pytest printing a longer/rootdir-
relative id than the one requested.
"""

from __future__ import annotations

import re

from task_bundle.runners.base import RunResult, Runner, Status, register_runner

# Lines like: "PASSED openlibrary/tests/core/test_wikidata.py::test_get_statement_values"
# Outcome keywords pytest emits in the -rA summary.
_SUMMARY_RE = re.compile(
    r"^(?P<outcome>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(?P<nodeid>\S.*)$"
)

_OUTCOME_MAP = {
    "PASSED": Status.passed,
    "XPASS": Status.passed,
    "FAILED": Status.failed,
    "ERROR": Status.error,
    "XFAIL": Status.passed,  # expected failure that failed -> treated as passing
    "SKIPPED": Status.missing,
}


class PytestRunner(Runner):
    name = "pytest"

    def parse(
        self, *, stdout: str, stderr: str, exit_code: int, test_ids: list[str]
    ) -> RunResult:
        text = stdout + "\n" + stderr
        # Collect every outcome line from the -rA summary.
        reported: dict[str, Status] = {}
        for line in text.splitlines():
            m = _SUMMARY_RE.match(line.strip())
            if not m:
                continue
            nodeid = m.group("nodeid").strip()
            status = _OUTCOME_MAP.get(m.group("outcome"))
            if status is None:
                continue
            # Keep the worst-known status if a node appears more than once.
            prev = reported.get(nodeid)
            reported[nodeid] = _merge(prev, status)

        outcomes: dict[str, Status] = {}
        for tid in test_ids:
            outcomes[tid] = _lookup(reported, tid)

        return RunResult(
            outcomes=outcomes,
            exit_code=exit_code,
            raw_output=text,
        )


def _merge(prev: Status | None, new: Status) -> Status:
    if prev is None:
        return new
    severity = {Status.passed: 0, Status.missing: 1, Status.failed: 2, Status.error: 3}
    return prev if severity[prev] >= severity[new] else new


def _lookup(reported: dict[str, Status], test_id: str) -> Status:
    """Find the outcome for a requested id, tolerating prefix differences."""
    if test_id in reported:
        return reported[test_id]
    # pytest may report a rootdir-relative path; match on suffix/substring.
    for nodeid, status in reported.items():
        if nodeid.endswith(test_id) or test_id.endswith(nodeid) or test_id in nodeid:
            return status
    return Status.missing


register_runner("pytest", PytestRunner)
