"""jest runner: parse ``✓``/``✗`` verbose markers.

``jest --verbose`` prints a checkmark or cross per test:

    ✓ adds two numbers (3 ms)
    ✕ handles negatives (1 ms)

Jest test ids are human-readable names, so we substring-match the requested id
against the printed test title.
"""

from __future__ import annotations

import re

from task_bundle.runners.base import RunResult, Runner, Status, register_runner

# Jest prints ✓/✔ for pass, ✕/✗ for fail, ○/skipped for pending.
_LINE_RE = re.compile(r"^\s*(?P<mark>[\u2713\u2714\u2715\u2717\u25cb])\s+(?P<title>.+?)(?:\s+\(\d+\s*ms\))?\s*$")

_PASS = {"\u2713", "\u2714"}
_FAIL = {"\u2715", "\u2717"}


class JestRunner(Runner):
    name = "jest"

    def parse(
        self, *, stdout: str, stderr: str, exit_code: int, test_ids: list[str]
    ) -> RunResult:
        text = stdout + "\n" + stderr
        reported: list[tuple[str, Status]] = []
        for line in text.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            mark = m.group("mark")
            status = (
                Status.passed if mark in _PASS else Status.failed if mark in _FAIL else Status.missing
            )
            reported.append((m.group("title").strip(), status))

        outcomes: dict[str, Status] = {}
        for tid in test_ids:
            needle = tid.split("::")[-1].strip()
            found = Status.missing
            for title, st in reported:
                if needle in title or title in needle:
                    found = st
                    break
            outcomes[tid] = found
        # Fallback: if we requested a single test and parsed nothing, use exit code.
        if not reported and len(test_ids) >= 1:
            verdict = Status.passed if exit_code == 0 else Status.failed
            outcomes = {tid: verdict for tid in test_ids}
        return RunResult(outcomes=outcomes, exit_code=exit_code, raw_output=text)


register_runner("jest", JestRunner)
