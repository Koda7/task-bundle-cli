"""Implementation of the heavier commands (validate, run).

Kept separate from cli.py so the Typer layer stays a thin argument-parsing shell
and the orchestration logic is independently testable.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from rich.table import Table

from task_bundle import db, git_ops, phases
from task_bundle.bundle import Bundle
from task_bundle.console import console, error, info, success, warn
from task_bundle.console import status_markup as sm
from task_bundle.docker_env import TaskContainer, ensure_image
from task_bundle.paths import run_dir
from task_bundle.proc import CommandRecorder
from task_bundle.runners import Status


# ===========================================================================
# validate
# ===========================================================================


def run_validate(
    conn: sqlite3.Connection,
    cmd: db.CommandLog,
    bundle: Bundle,
    *,
    offline: bool,
    keep_logs: bool = True,
    repeat: int = 1,
) -> bool:
    """Return True iff all guardrails pass."""
    if offline:
        return _validate_offline(bundle)
    return _validate_with_docker(conn, cmd, bundle, keep_logs=keep_logs, repeat=repeat)


def _validate_offline(bundle: Bundle) -> bool:
    """Schema + file structure checks, no Docker. Fast CI path."""
    from task_bundle.bundle import check_bundle_files

    info(f"Validating bundle [id]{bundle.name}[/id] (offline: schema + files)")
    problems = check_bundle_files(bundle, require_hidden=_needs_hidden_patch(bundle))
    if problems:
        for p in problems:
            error(f"  {p}")
        error("offline validation FAILED")
        return False
    success("offline validation passed (schema + required files present)")
    info("  note: --offline does not run tests; use a Docker run for guardrails.")
    return True


def _needs_hidden_patch(bundle: Bundle) -> bool:
    return bundle.spec.setup.hidden_test_mode.value == "patch"


def _validate_with_docker(
    conn: sqlite3.Connection,
    cmd: db.CommandLog,
    bundle: Bundle,
    *,
    keep_logs: bool,
    repeat: int = 1,
) -> bool:
    spec = bundle.spec
    rec = CommandRecorder()
    info(f"Validating bundle [id]{bundle.name}[/id]")
    info(f"Ensuring image {spec.environment.image} ...")
    digest = ensure_image(
        spec.environment.image, platform=spec.environment.platform, recorder=rec
    )
    cmd.set_image(spec.environment.image, digest)
    info(f"  image digest {digest}")

    started = time.time()
    flaky: dict[str, set[str]] = {}
    with TaskContainer(
        spec.environment.image,
        workdir=spec.environment.workdir,
        platform=spec.environment.platform,
        memory=spec.environment.memory,
        cpus=spec.environment.cpus,
        pids_limit=spec.environment.pids_limit,
        recorder=rec,
    ) as c:
        # --- baseline phase: hidden tests staged, NO code patch ---
        info("[info]Phase 1/2:[/info] baseline (hidden tests staged, no fix applied)")
        phases.prepare_baseline(c, bundle)
        baseline = phases.run_grading_tests(c, bundle)

        # --- golden phase: reset, stage hidden tests, apply golden patch ---
        info("[info]Phase 2/2:[/info] golden (fix applied)")
        phases.prepare_baseline(c, bundle)  # reset + re-stage hidden tests
        if bundle.has_golden_patch():
            phases.apply_golden(c, bundle)
        else:
            warn("  no golden patch present; golden phase will mirror baseline")
        golden = phases.run_grading_tests(c, bundle)

        # --- optional flaky detection: re-run golden tests N-1 more times ---
        if repeat > 1:
            info(f"[info]Flaky check:[/info] re-running golden tests {repeat - 1} more time(s)")
            observed: dict[str, set[str]] = {
                tid: {st.value} for tid, st in {**golden.fail_to_pass, **golden.pass_to_pass}.items()
            }
            for i in range(repeat - 1):
                extra = phases.run_grading_tests(c, bundle)
                for tid, st in {**extra.fail_to_pass, **extra.pass_to_pass}.items():
                    observed.setdefault(tid, set()).add(st.value)
            flaky = {tid: sts for tid, sts in observed.items() if len(sts) > 1}

    duration_ms = int((time.time() - started) * 1000)

    if repeat > 1:
        if flaky:
            warn(f"  flaky tests detected ({len(flaky)}): results varied across runs")
            for tid, sts in flaky.items():
                warn(f"    {_short_test(tid)}: {sorted(sts)}")
        else:
            success(f"  no flaky tests across {repeat} runs")

    # --- evaluate the guardrail contract ---
    checks = _evaluate_guardrails(baseline, golden)
    _render_validate_table(bundle, baseline, golden, checks)

    all_ok = all(ok for (_, ok, _) in checks)

    # persist as a run (solver='validate') for queryability + artifacts
    rid = db.record_run(
        conn,
        cid=cmd.cid,
        bundle=str(bundle.root),
        instance_id=spec.source.instance_id,
        solver="validate",
        model=None,
        resolved=all_ok,
        f2p_total=golden.f2p_total,
        f2p_passed=golden.f2p_passed,
        p2p_total=golden.p2p_total,
        p2p_passed=golden.p2p_passed,
        artifact_path=None,
        duration_ms=duration_ms,
    )
    db.record_test_results(conn, run_id=rid, phase="baseline", rows=baseline.db_rows())
    db.record_test_results(conn, run_id=rid, phase="golden", rows=golden.db_rows())

    if keep_logs:
        _write_validate_artifacts(rid, bundle, baseline, golden, checks, rec, digest)

    if all_ok:
        success(f"All guardrails passed. (run [id]{rid}[/id])")
    else:
        error(f"Guardrails FAILED. (run [id]{rid}[/id])  See: task log {rid}")
    return all_ok


def _evaluate_guardrails(
    baseline: phases.PhaseResult, golden: phases.PhaseResult
) -> list[tuple[str, bool, str]]:
    """Return [(check_name, ok, detail)]."""
    checks: list[tuple[str, bool, str]] = []
    # Baseline: every fail2pass must FAIL.
    checks.append(
        (
            "baseline fail2pass all FAIL",
            baseline.all_f2p_fail,
            f"{baseline.f2p_total - baseline.f2p_passed}/{baseline.f2p_total} failing",
        )
    )
    # Baseline: every pass2pass must PASS.
    checks.append(
        (
            "baseline pass2pass all PASS",
            baseline.all_p2p_pass,
            f"{baseline.p2p_passed}/{baseline.p2p_total} passing",
        )
    )
    # Golden: every fail2pass must PASS.
    checks.append(
        (
            "golden fail2pass all PASS",
            golden.all_f2p_pass,
            f"{golden.f2p_passed}/{golden.f2p_total} passing",
        )
    )
    # Golden: every pass2pass must PASS.
    checks.append(
        (
            "golden pass2pass all PASS",
            golden.all_p2p_pass,
            f"{golden.p2p_passed}/{golden.p2p_total} passing",
        )
    )
    return checks


def _render_validate_table(
    bundle: Bundle,
    baseline: phases.PhaseResult,
    golden: phases.PhaseResult,
    checks: list[tuple[str, bool, str]],
) -> None:
    # Guardrail summary table.
    t = Table(title=f"Guardrails: {bundle.name}", show_lines=False)
    t.add_column("Check")
    t.add_column("Result")
    t.add_column("Detail", style="dim")
    for name, ok, detail in checks:
        t.add_row(name, "[ok]PASS[/ok]" if ok else "[fail]FAIL[/fail]", detail)
    console.print(t)

    # Per-test detail table (baseline vs golden).
    dt = Table(title="Per-test (baseline -> golden)", show_lines=False)
    dt.add_column("Bucket")
    dt.add_column("Test")
    dt.add_column("Baseline")
    dt.add_column("Golden")
    for tid in baseline.fail_to_pass:
        dt.add_row(
            "fail2pass",
            _short_test(tid),
            sm(baseline.fail_to_pass[tid].value),
            sm(golden.fail_to_pass.get(tid, Status.missing).value),
        )
    for tid in baseline.pass_to_pass:
        dt.add_row(
            "pass2pass",
            _short_test(tid),
            sm(baseline.pass_to_pass[tid].value),
            sm(golden.pass_to_pass.get(tid, Status.missing).value),
        )
    console.print(dt)


def _short_test(test_id: str, width: int = 60) -> str:
    if len(test_id) <= width:
        return test_id
    return "..." + test_id[-(width - 3):]


def _write_validate_artifacts(
    run_id: str,
    bundle: Bundle,
    baseline: phases.PhaseResult,
    golden: phases.PhaseResult,
    checks: list[tuple[str, bool, str]],
    rec: CommandRecorder,
    digest: str,
) -> None:
    d = run_dir(run_id)
    (d / "logs" / "baseline_tests.log").write_text(baseline.log, encoding="utf-8")
    (d / "logs" / "golden_tests.log").write_text(golden.log, encoding="utf-8")
    report = {
        "kind": "validate",
        "run_id": run_id,
        "bundle": bundle.name,
        "instance_id": bundle.spec.source.instance_id,
        "image": bundle.spec.environment.image,
        "image_digest": digest,
        "guardrails": [
            {"check": name, "ok": ok, "detail": detail} for (name, ok, detail) in checks
        ],
        "passed": all(ok for (_, ok, _) in checks),
        "baseline": {
            "fail_to_pass": {k: v.value for k, v in baseline.fail_to_pass.items()},
            "pass_to_pass": {k: v.value for k, v in baseline.pass_to_pass.items()},
        },
        "golden": {
            "fail_to_pass": {k: v.value for k, v in golden.fail_to_pass.items()},
            "pass_to_pass": {k: v.value for k, v in golden.pass_to_pass.items()},
        },
    }
    (d / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(
            {"image_digest": digest, "docker_commands": rec.as_list()}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    info(f"  artifacts: {d}")


# ===========================================================================
# build
# ===========================================================================


def run_build(
    cmd: db.CommandLog,
    bundle: Bundle,
    *,
    force_pull: bool,
    smoke: bool,
    pin_digest: bool,
    clone: bool = False,
) -> bool:
    """Prepare the image, record digest, optional clone + smoke test + digest pin."""
    spec = bundle.spec
    rec = CommandRecorder()
    info(f"Building environment for [id]{bundle.name}[/id]")
    if force_pull or not _image_present(spec.environment.image):
        info(f"Pulling {spec.environment.image} (may be large + emulated) ...")
    else:
        info("Image already present locally; skipping pull (idempotent).")
    digest = ensure_image(
        spec.environment.image,
        platform=spec.environment.platform,
        recorder=rec,
        force_pull=force_pull,
    )
    cmd.set_image(spec.environment.image, digest)
    success(f"Image ready. digest {digest}")

    # Optional: clone an arbitrary repo into the image at base_commit. This needs
    # network, so we use a NON-sealed build container (a deliberate, build-time
    # exception). We commit the result back to the image tag so later sealed runs
    # have the repo without any network -- which produces a NEW image id, so we
    # re-resolve the digest below and pin THAT (not the pre-clone one).
    if clone:
        if not _clone_into_image(spec, rec):
            return False
        from task_bundle.docker_env import image_digest as _image_digest

        digest = _image_digest(spec.environment.image) or digest
        cmd.set_image(spec.environment.image, digest)
        info(f"  post-clone image digest {digest}")

    # Pin AFTER any clone so task.json records the image that actually contains
    # the repo, not the pristine base.
    if pin_digest and digest and digest != "unknown":
        _pin_digest(bundle, digest)
        info("  pinned image_digest into task.json")

    if not smoke:
        return True

    info("Smoke test: repo present + reset + test runner discoverable ...")
    with TaskContainer(
        spec.environment.image,
        workdir=spec.environment.workdir,
        platform=spec.environment.platform,
        recorder=rec,
    ) as c:
        # repo at workdir?
        head = c.bash("git rev-parse HEAD")
        if not head.ok:
            error(f"  no git repo at {spec.environment.workdir}: {head.stderr.strip()}")
            if not clone:
                error("  hint: pass --clone to clone the repo into the image first.")
            return False
        info(f"  repo HEAD: {head.stdout.strip()[:12]}")
        # reset cleanly?
        try:
            git_ops.reset_to_base(c, spec)
            info("  reset to base: ok")
        except git_ops.GitError as e:
            error(f"  reset failed: {e}")
            return False
        # runner present? (best-effort: pytest importable / command resolvable)
        probe = _runner_probe(spec.test.runner.value)
        if probe:
            r = c.bash(probe)
            mark = "ok" if r.ok else "not found"
            (success if r.ok else warn)(f"  runner '{spec.test.runner.value}': {mark}")
    success("Smoke test passed.")
    return True


def _image_present(image: str) -> bool:
    from task_bundle.docker_env import image_exists_locally

    return image_exists_locally(image)


def _clone_into_image(spec, rec: CommandRecorder) -> bool:
    """Clone the repo at base_commit into the image, in a networked container.

    Commits the resulting filesystem back to the image tag so subsequent SEALED
    runs have the repo present with no network access.
    """
    from task_bundle.docker_env import TaskContainer, commit_container

    repo_url = _repo_clone_url(spec.source.repo)
    info(f"Cloning {repo_url} @ {spec.source.base_commit[:12]} into image (networked build)")
    # Networked, but still cap-dropped/no-new-privileges: only relaxes egress.
    c = TaskContainer(
        spec.environment.image,
        workdir=spec.environment.workdir,
        platform=spec.environment.platform,
        network="bridge",
        recorder=rec,
    ).start()
    try:
        git_ops.clone_repo_into_workdir(c, spec, repo_url=repo_url)
        head = c.bash("git rev-parse HEAD")
        if not head.ok or spec.source.base_commit not in head.stdout:
            error("  clone did not land at the expected commit")
            return False
        commit_container(c.container_id, spec.environment.image, recorder=rec)
        success(f"  cloned and committed to image (HEAD {head.stdout.strip()[:12]})")
        return True
    except git_ops.GitError as e:
        error(f"  {e}")
        return False
    finally:
        c.stop()


def _repo_clone_url(repo: str) -> str:
    """Best-effort clone URL: pass through a full URL, else assume GitHub slug."""
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        return repo
    return f"https://github.com/{repo}.git"


def _runner_probe(runner: str) -> str | None:
    if runner == "pytest":
        return "python -m pytest --version"
    if runner == "gotest":
        return "go version"
    if runner == "jest":
        return "npx jest --version || node --version"
    return None  # generic: nothing to probe


def _pin_digest(bundle: Bundle, digest: str) -> None:
    """Write the resolved digest back into the bundle's task.json."""
    raw = json.loads(bundle.task_json_path.read_text(encoding="utf-8"))
    raw.setdefault("environment", {})["image_digest"] = digest
    bundle.task_json_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


# ===========================================================================
# run
# ===========================================================================


def run_solver(
    conn: sqlite3.Connection,
    cmd: db.CommandLog,
    bundle: Bundle,
    *,
    solver_name: str,
    model: str | None,
    strip_git_history: bool,
    analyze: bool,
    solver_kwargs: dict | None = None,
) -> tuple[bool, str]:
    """Execute the full solve+grade flow. Returns (resolved, run_id)."""
    from task_bundle.solvers import get_solver

    spec = bundle.spec
    rec = CommandRecorder()
    solver = get_solver(solver_name, **(solver_kwargs or {}))

    info(f"Running solver [id]{solver_name}[/id] on [id]{bundle.name}[/id]")
    info(f"Ensuring image {spec.environment.image} ...")
    digest = ensure_image(
        spec.environment.image, platform=spec.environment.platform, recorder=rec
    )
    cmd.set_image(spec.environment.image, digest)

    started = time.time()
    with TaskContainer(
        spec.environment.image,
        workdir=spec.environment.workdir,
        platform=spec.environment.platform,
        memory=spec.environment.memory,
        cpus=spec.environment.cpus,
        pids_limit=spec.environment.pids_limit,
        recorder=rec,
    ) as c:
        # 1) reset to a pristine base -- hidden tests are NOT present here, so the
        #    solver can never see fail2pass / pass2pass.
        info("Resetting repo to baseline (hidden tests absent)")
        git_ops.reset_to_base(c, bundle.spec)

        # Optional anti-cheat: strip future git history so an agent can't read
        # the fix commit out of the log.
        if strip_git_history:
            info("Stripping future git history (anti-cheat)")
            git_ops.strip_git_history(c, bundle.spec)

        # 2) run the solver (mutates the repo in place)
        info("Invoking solver ...")
        solver_result = solver.solve(c, bundle)
        if solver_result.ok:
            info(f"  solver: {solver_result.message} ({solver_result.steps} steps)")
        else:
            warn(f"  solver reported a problem: {solver_result.message}")

        # 3) normalize whatever the solver did to a single diff (for repro)
        solver_patch = git_ops.capture_diff(c, bundle.spec)

        # 4) stage hidden tests NOW (after the solver, never before)
        info("Staging hidden tests and grading")
        hidden = bundle.read_hidden_patch() if bundle.has_hidden_patch() else ""
        git_ops.stage_hidden_tests(c, bundle.spec, hidden)

        # 5) grade
        graded = phases.run_grading_tests(c, bundle)

    duration_ms = int((time.time() - started) * 1000)

    _render_run_table(bundle, solver_name, graded)

    rid = db.record_run(
        conn,
        cid=cmd.cid,
        bundle=str(bundle.root),
        instance_id=spec.source.instance_id,
        solver=solver_name,
        model=model,
        resolved=graded.resolved,
        f2p_total=graded.f2p_total,
        f2p_passed=graded.f2p_passed,
        p2p_total=graded.p2p_total,
        p2p_passed=graded.p2p_passed,
        artifact_path=None,
        duration_ms=duration_ms,
    )
    db.record_test_results(conn, run_id=rid, phase="post_solver", rows=graded.db_rows())

    # Optional failure analysis (stretch; only meaningful for unresolved agent runs).
    failure_analysis = None
    if analyze and not graded.resolved:
        failure_analysis = _maybe_analyze(bundle, solver_result, graded, model)

    artifact_path = _write_run_artifacts(
        rid,
        bundle,
        solver_name,
        model,
        graded,
        solver_result,
        solver_patch,
        rec,
        digest,
        duration_ms,
        failure_analysis,
    )
    # backfill artifact path
    conn.execute(
        "UPDATE runs SET artifact_path = ? WHERE run_id = ?", (str(artifact_path), rid)
    )
    conn.commit()

    if graded.resolved:
        success(f"RESOLVED \U0001f680  (run [id]{rid}[/id])")
    else:
        warn(
            f"UNRESOLVED: {graded.f2p_passed}/{graded.f2p_total} fail2pass, "
            f"{graded.p2p_passed}/{graded.p2p_total} pass2pass passing. "
            f"(run [id]{rid}[/id]  ->  task log {rid})"
        )
    info(f"  report: {artifact_path}")
    return graded.resolved, rid


def _render_run_table(bundle: Bundle, solver_name: str, graded: phases.PhaseResult) -> None:
    t = Table(title=f"Solver '{solver_name}' on {bundle.name}", show_lines=False)
    t.add_column("Bucket")
    t.add_column("Test")
    t.add_column("Result")
    for tid, st in graded.fail_to_pass.items():
        t.add_row("fail2pass", _short_test(tid), sm(st.value))
    for tid, st in graded.pass_to_pass.items():
        t.add_row("pass2pass", _short_test(tid), sm(st.value))
    console.print(t)
    verdict = "[ok]RESOLVED[/ok]" if graded.resolved else "[fail]UNRESOLVED[/fail]"
    console.print(
        f"  Verdict: {verdict}  "
        f"(fail2pass {graded.f2p_passed}/{graded.f2p_total}, "
        f"pass2pass {graded.p2p_passed}/{graded.p2p_total})"
    )


def _maybe_analyze(
    bundle: Bundle,
    solver_result,
    graded: phases.PhaseResult,
    model: str | None,
):
    """Best-effort failure analysis; never fails the run."""
    try:
        from task_bundle.analysis import analyze_failure

        return analyze_failure(bundle, solver_result, graded, model=model)
    except Exception as e:  # noqa: BLE001 -- analysis is optional
        warn(f"  failure analysis skipped: {e}")
        return None


def _write_run_artifacts(
    run_id: str,
    bundle: Bundle,
    solver_name: str,
    model: str | None,
    graded: phases.PhaseResult,
    solver_result,
    solver_patch: str,
    rec: CommandRecorder,
    digest: str,
    duration_ms: int,
    failure_analysis,
) -> Path:
    d = run_dir(run_id)
    (d / "solver.patch").write_text(solver_patch, encoding="utf-8")
    (d / "logs" / "solver.log").write_text(solver_result.log or "", encoding="utf-8")
    (d / "logs" / "post_solver_tests.log").write_text(graded.log, encoding="utf-8")
    if solver_result.transcript:
        (d / "logs" / "transcript.json").write_text(
            json.dumps(solver_result.transcript, indent=2) + "\n", encoding="utf-8"
        )

    report = {
        "kind": "run",
        "run_id": run_id,
        "bundle": bundle.name,
        "instance_id": bundle.spec.source.instance_id,
        "repo": bundle.spec.source.repo,
        "base_commit": bundle.spec.source.base_commit,
        "solver": solver_name,
        "model": model,
        "image": bundle.spec.environment.image,
        "image_digest": digest,
        "resolved": graded.resolved,
        "duration_ms": duration_ms,
        "summary": {
            "fail_to_pass": {"passed": graded.f2p_passed, "total": graded.f2p_total},
            "pass_to_pass": {"passed": graded.p2p_passed, "total": graded.p2p_total},
        },
        "tests": {
            "fail_to_pass": [
                {"id": tid, "status": st.value} for tid, st in graded.fail_to_pass.items()
            ],
            "pass_to_pass": [
                {"id": tid, "status": st.value} for tid, st in graded.pass_to_pass.items()
            ],
        },
        "solver_report": {
            "ok": solver_result.ok,
            "message": solver_result.message,
            "steps": solver_result.steps,
        },
    }
    if failure_analysis is not None:
        report["failure_analysis"] = failure_analysis

    report_path = d / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(
            {
                "image_digest": digest,
                "duration_ms": duration_ms,
                "docker_commands": rec.as_list(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report_path
