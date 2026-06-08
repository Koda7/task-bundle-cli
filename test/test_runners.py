"""Runner parser tests (no Docker)."""

from __future__ import annotations

from task_bundle.runners import available_runners, get_runner
from task_bundle.runners.base import Status


def test_all_runners_registered():
    assert set(available_runners()) >= {"pytest", "generic", "gotest", "jest"}


def test_pytest_summary_parse_with_reruns():
    out = """
test_x.py::test_a RERUN
test_x.py::test_a PASSED
=== short test summary info ===
PASSED test_x.py::test_a
FAILED test_x.py::test_b - AssertionError
ERROR test_x.py::test_c
"""
    res = get_runner("pytest").parse(
        stdout=out,
        stderr="",
        exit_code=1,
        test_ids=["test_x.py::test_a", "test_x.py::test_b", "test_x.py::test_c", "test_x.py::test_d"],
    )
    assert res.outcomes["test_x.py::test_a"] is Status.passed
    assert res.outcomes["test_x.py::test_b"] is Status.failed
    assert res.outcomes["test_x.py::test_c"] is Status.error
    assert res.outcomes["test_x.py::test_d"] is Status.missing


def test_pytest_parametrized_ids():
    out = "PASSED t.py::test_x[a-b-c]\nFAILED t.py::test_x[d-e-f]\n"
    res = get_runner("pytest").parse(
        stdout=out, stderr="", exit_code=1, test_ids=["t.py::test_x[a-b-c]", "t.py::test_x[d-e-f]"]
    )
    assert res.outcomes["t.py::test_x[a-b-c]"] is Status.passed
    assert res.outcomes["t.py::test_x[d-e-f]"] is Status.failed


def test_generic_runner_uses_exit_code():
    gr = get_runner("generic")
    ok = gr.parse(stdout="", stderr="", exit_code=0, test_ids=["a", "b"])
    bad = gr.parse(stdout="", stderr="x", exit_code=2, test_ids=["a", "b"])
    assert all(s is Status.passed for s in ok.outcomes.values())
    assert all(s is Status.failed for s in bad.outcomes.values())


def test_generic_runner_no_ids_aggregate():
    gr = get_runner("generic")
    res = gr.parse(stdout="", stderr="", exit_code=0, test_ids=[])
    assert res.outcomes == {"<all>": Status.passed}


def test_gotest_parse():
    out = "--- PASS: TestA (0.0s)\n--- FAIL: TestB (0.1s)\n--- SKIP: TestC\n"
    res = get_runner("gotest").parse(
        stdout=out, stderr="", exit_code=1, test_ids=["TestA", "TestB", "TestC", "TestD"]
    )
    assert res.outcomes["TestA"] is Status.passed
    assert res.outcomes["TestB"] is Status.failed
    assert res.outcomes["TestD"] is Status.missing


def test_jest_parse():
    out = "  \u2713 adds (3 ms)\n  \u2715 subtracts (1 ms)\n"
    res = get_runner("jest").parse(
        stdout=out, stderr="", exit_code=1, test_ids=["adds", "subtracts"]
    )
    assert res.outcomes["adds"] is Status.passed
    assert res.outcomes["subtracts"] is Status.failed


def test_build_command_substitution():
    r = get_runner("pytest")
    assert r.build_command("pytest -rA {tests}", ["a", "b"]) == "pytest -rA a b"
    # quoting of ids with brackets
    cmd = r.build_command("pytest {tests}", ["t.py::x[a b]"])
    assert "'t.py::x[a b]'" in cmd
    # template without {tests} appends
    assert r.build_command("pytest", ["a"]) == "pytest a"
