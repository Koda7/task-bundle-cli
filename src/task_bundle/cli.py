"""``task`` -- the Task Bundle CLI.

Commands: init, validate, run, runs, log, report (+ stretch: build, shell).
Every invocation is logged to the SQLite store so any run/command is queryable
later by id.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Optional

import typer

from task_bundle import db
from task_bundle.bundle import BundleError, load_bundle
from task_bundle.console import error, info, success, warn

app = typer.Typer(
    name="task",
    help="Package, validate, and solve containerized coding tasks (SWE-bench style).",
    no_args_is_help=True,
    add_completion=False,
)


def _host() -> str:
    return platform.node() or "unknown"


# ---------------------------------------------------------------------------
# task init
# ---------------------------------------------------------------------------


@app.command()
def init(
    instance_id: Optional[str] = typer.Option(
        None,
        "--from-swebench-pro",
        "--instance-id",
        help="SWE-Bench Pro instance id (exact or unique substring) to scaffold from.",
    ),
    dest: Path = typer.Option(
        Path("bundles"),
        "--dest",
        "-d",
        help="Directory under which the bundle dir is created.",
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Override the bundle dir name."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Generic mode: repo URL/slug."),
    commit: Optional[str] = typer.Option(None, "--commit", help="Generic mode: base commit sha."),
    image: Optional[str] = typer.Option(
        None, "--image", help="Generic mode: Docker image for the task environment."
    ),
    runner: str = typer.Option("pytest", "--runner", help="Generic mode: test runner."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing bundle dir."),
) -> None:
    """Scaffold a task bundle.

    Two modes:

    * SWE-Bench Pro:  task init --from-swebench-pro <instance_id>
    * Generic:        task init --repo <url> --commit <sha> --image <image> --name <name>
    """
    from task_bundle.scaffold import (
        ScaffoldError,
        scaffold_from_swebench_pro,
        scaffold_generic,
    )

    args = {
        "instance_id": instance_id,
        "dest": str(dest),
        "name": name,
        "repo": repo,
        "commit": commit,
        "image": image,
        "runner": runner,
    }
    with db.session() as conn:
        cmd = db.start_command(conn, command="init", args=args, host=_host())
        try:
            if instance_id:
                info(f"Scaffolding bundle from SWE-Bench Pro instance: [id]{instance_id}[/id]")
                info("Downloading dataset parquet (cached after first fetch)...")
                bundle = scaffold_from_swebench_pro(
                    instance_id, dest, name=name, overwrite=overwrite
                )
            elif repo and commit and image:
                bundle = scaffold_generic(
                    dest,
                    name=name or repo.split("/")[-1],
                    repo=repo,
                    commit=commit,
                    image=image,
                    runner=runner,
                    overwrite=overwrite,
                )
            else:
                msg = (
                    "provide either --from-swebench-pro <id>, or all of "
                    "--repo --commit --image (generic mode)"
                )
                error(msg)
                cmd.finish(status="error", exit_code=2, message=msg)
                raise typer.Exit(2)

            cmd.set_image(bundle.spec.environment.image)
            success(f"Created bundle at [id]{bundle.root}[/id]")
            _print_bundle_summary(bundle)
            cmd.finish(
                status="ok",
                exit_code=0,
                message=f"scaffolded {bundle.name} ({bundle.spec.source.instance_id})",
            )
        except (ScaffoldError, BundleError) as e:
            error(str(e))
            cmd.finish(status="error", exit_code=1, message=str(e))
            raise typer.Exit(1)
        except Exception as e:  # network, dataset, etc.
            error(f"unexpected error: {e}")
            cmd.finish(status="error", exit_code=1, message=str(e))
            raise typer.Exit(1)


def _print_bundle_summary(bundle) -> None:
    s = bundle.spec
    info(f"  repo           {s.source.repo} @ {s.source.base_commit[:12]}")
    info(f"  image          {s.environment.image}")
    info(f"  runner         {s.test.runner.value}")
    info(
        f"  grading        {len(s.grading.fail_to_pass)} fail2pass, "
        f"{len(s.grading.pass_to_pass)} pass2pass"
    )
    if not bundle.has_golden_patch():
        warn("  patch.diff is empty -- fill it in (golden patch).")
    if not bundle.has_hidden_patch() and s.setup.hidden_test_mode.value == "patch":
        warn("  tests/hidden.patch is empty -- add the hidden test diff.")


def _load_or_exit(bundle_path: Path):
    try:
        return load_bundle(bundle_path)
    except BundleError as e:
        error(str(e))
        raise typer.Exit(1)


def _ensure_daemon_or_exit() -> None:
    from task_bundle.docker_env import daemon_available

    ok, msg = daemon_available()
    if not ok:
        error(f"Docker is not available: {msg}")
        error("Start Docker Desktop (or the daemon) and try again.")
        raise typer.Exit(3)


# ---------------------------------------------------------------------------
# task validate
# ---------------------------------------------------------------------------


@app.command()
def validate(
    bundle_path: Path = typer.Argument(..., help="Path to the task bundle directory."),
    offline: bool = typer.Option(
        False, "--offline", help="Validate schema + patch applicability without Docker."
    ),
    repeat: int = typer.Option(
        1,
        "--repeat",
        "-r",
        help="Re-run the golden tests N times to detect flaky/non-deterministic tests.",
        min=1,
    ),
    keep_logs: bool = typer.Option(
        True, "--keep-logs/--no-keep-logs", help="Write phase logs to the run artifact dir."
    ),
) -> None:
    """Run baseline + golden guardrails on a bundle.

    Stages hidden tests, then asserts: baseline -> fail2pass FAIL & pass2pass
    PASS; golden -> all PASS. This is the contract that proves a task is sound
    before any solver touches it. ``--repeat N`` re-runs the golden tests N times
    and flags any test whose result is not consistent (flaky).
    """
    from task_bundle.commands_impl import run_validate

    bundle = _load_or_exit(bundle_path)
    args = {"bundle": str(bundle.root), "offline": offline, "repeat": repeat}
    with db.session() as conn:
        cmd = db.start_command(
            conn, command="validate", args=args, bundle=str(bundle.root), host=_host()
        )
        cmd.set_image(bundle.spec.environment.image)
        try:
            ok = run_validate(
                conn, cmd, bundle, offline=offline, keep_logs=keep_logs, repeat=repeat
            )
            cmd.finish(
                status="ok" if ok else "error",
                exit_code=0 if ok else 1,
                message="guardrails passed" if ok else "guardrails FAILED",
            )
            raise typer.Exit(0 if ok else 1)
        except typer.Exit:
            raise
        except Exception as e:
            error(f"validate failed: {e}")
            cmd.finish(status="error", exit_code=1, message=str(e))
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# task run
# ---------------------------------------------------------------------------


@app.command()
def run(
    bundle_path: Path = typer.Argument(..., help="Path to the task bundle directory."),
    solver: str = typer.Option(
        "golden", "--solver", "-s", help="Solver: golden | stub | openai-agent."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Model id for the openai-agent solver (e.g. gpt-4o)."
    ),
    max_steps: int = typer.Option(
        40, "--max-steps", help="Agent: max tool-use steps."
    ),
    strip_git_history: Optional[bool] = typer.Option(
        None,
        "--strip-git-history/--allow-git-history",
        help="Rewrite repo history so an agent can't read the fix from git log. "
        "Default: ON for LLM/agent solvers (benchmark integrity), OFF for golden/stub.",
    ),
    analyze: bool = typer.Option(
        False, "--analyze", help="On failure, classify the agent trajectory (LLM-as-judge)."
    ),
) -> None:
    """Run a solver against a task and grade fail2pass / pass2pass.

    Flow: reset to base (no hidden tests) -> run solver -> capture final diff ->
    stage hidden tests -> grade -> write report.json. The solver never sees the
    hidden tests.

    Git-history stripping defaults ON for agent/LLM solvers so they can't recover
    the fix from the repo's git log (SWE-Bench Pro images can leak it). It is OFF
    for golden/stub, which don't read git. Override with --allow-git-history /
    --strip-git-history.
    """
    from task_bundle.commands_impl import run_solver
    from task_bundle.solvers import available_solvers, has_solver

    bundle = _load_or_exit(bundle_path)

    if not has_solver(solver):
        # openai-agent registers lazily; trigger import then re-check.
        if solver == "openai-agent":
            try:
                import task_bundle.solvers.openai_agent  # noqa: F401
            except Exception as e:
                error(f"openai-agent solver unavailable: {e}")
                error("Install the agent extra: pip install -e '.[agent]'")
                raise typer.Exit(2)
        if not has_solver(solver):
            error(f"unknown solver {solver!r}; available: {', '.join(available_solvers())}")
            raise typer.Exit(2)

    _ensure_daemon_or_exit()

    solver_kwargs: dict = {}
    if solver == "openai-agent":
        solver_kwargs = {"model": model or "gpt-4o", "max_steps": max_steps}

    # Resolve the git-history default: ON for LLM/agent solvers (they could read
    # the fix from the log), OFF for the deterministic golden/stub solvers (which
    # never touch git, so stripping just wastes time). Explicit flag wins.
    deterministic_solvers = {"golden", "stub"}
    if strip_git_history is None:
        effective_strip = solver not in deterministic_solvers
    else:
        effective_strip = strip_git_history
    if effective_strip and solver not in deterministic_solvers:
        info("git history will be stripped (anti-cheat; --allow-git-history to disable)")

    args = {
        "bundle": str(bundle.root),
        "solver": solver,
        "model": model,
        "strip_git_history": effective_strip,
        "analyze": analyze,
    }
    with db.session() as conn:
        cmd = db.start_command(
            conn, command="run", args=args, bundle=str(bundle.root), host=_host()
        )
        cmd.set_image(bundle.spec.environment.image)
        try:
            resolved, rid = run_solver(
                conn,
                cmd,
                bundle,
                solver_name=solver,
                model=model,
                strip_git_history=effective_strip,
                analyze=analyze,
                solver_kwargs=solver_kwargs,
            )
            cmd.finish(
                status="ok",
                exit_code=0,
                message=f"run {rid}: {'resolved' if resolved else 'unresolved'}",
            )
            raise typer.Exit(0)
        except typer.Exit:
            raise
        except Exception as e:
            error(f"run failed: {e}")
            cmd.finish(status="error", exit_code=1, message=str(e))
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# task build  (extra)
# ---------------------------------------------------------------------------


@app.command()
def build(
    bundle_path: Path = typer.Argument(..., help="Path to the task bundle directory."),
    force_pull: bool = typer.Option(
        False, "--force-pull", help="Re-pull even if the image is present locally."
    ),
    smoke: bool = typer.Option(
        True, "--smoke/--no-smoke", help="Run a quick smoke test (repo + tests discoverable)."
    ),
    pin_digest: bool = typer.Option(
        True, "--pin-digest/--no-pin-digest", help="Write the resolved digest into task.json."
    ),
    clone: bool = typer.Option(
        False,
        "--clone",
        help="Clone source.repo at base_commit into the image (for generic tasks "
        "whose image provides the toolchain but not the repo). Uses a networked "
        "build container, then commits the repo into the image for sealed runs.",
    ),
) -> None:
    """Prepare a bundle's image and record its digest (idempotent).

    Skips the (large, emulated) pull when the image is already present unless
    --force-pull. With --clone, clones the repo into the image at base_commit
    (useful for arbitrary non-SWE-Bench repos). Smoke-tests that the repo and
    test runner are usable, and pins the resolved digest into task.json.
    """
    from task_bundle.commands_impl import run_build

    bundle = _load_or_exit(bundle_path)
    _ensure_daemon_or_exit()
    args = {"bundle": str(bundle.root), "force_pull": force_pull, "smoke": smoke, "clone": clone}
    with db.session() as conn:
        cmd = db.start_command(
            conn, command="build", args=args, bundle=str(bundle.root), host=_host()
        )
        cmd.set_image(bundle.spec.environment.image)
        try:
            ok = run_build(
                cmd,
                bundle,
                force_pull=force_pull,
                smoke=smoke,
                pin_digest=pin_digest,
                clone=clone,
            )
            cmd.finish(
                status="ok" if ok else "error",
                exit_code=0 if ok else 1,
                message="image ready" if ok else "build/smoke failed",
            )
            raise typer.Exit(0 if ok else 1)
        except typer.Exit:
            raise
        except Exception as e:
            error(f"build failed: {e}")
            cmd.finish(status="error", exit_code=1, message=str(e))
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# task shell  (extra)
# ---------------------------------------------------------------------------


@app.command()
def shell(
    bundle_path: Path = typer.Argument(..., help="Path to the task bundle directory."),
    sealed: bool = typer.Option(
        True,
        "--sealed/--networked",
        help="Open the shell with no network (default) or with networking for debugging.",
    ),
    stage_hidden: bool = typer.Option(
        False, "--stage-hidden", help="Also stage hidden tests before dropping in."
    ),
) -> None:
    """Open an interactive shell inside the task container (debugging).

    Starts the same sealed container the harness uses, resets to baseline, and
    hands you a bash prompt. Useful for poking at the repo state by hand.
    """
    import os
    import shlex as _shlex

    from task_bundle import git_ops
    from task_bundle.console import info as _info
    from task_bundle.docker_env import DOCKER, TaskContainer, ensure_image

    bundle = _load_or_exit(bundle_path)
    _ensure_daemon_or_exit()
    spec = bundle.spec

    with db.session() as conn:
        cmd = db.start_command(
            conn, command="shell", args={"bundle": str(bundle.root)}, bundle=str(bundle.root)
        )
        ensure_image(spec.environment.image, platform=spec.environment.platform)
        c = TaskContainer(
            spec.environment.image,
            workdir=spec.environment.workdir,
            platform=spec.environment.platform,
            network="none" if sealed else "bridge",
        ).start()
        try:
            git_ops.reset_to_base(c, spec)
            if stage_hidden:
                hidden = bundle.read_hidden_patch() if bundle.has_hidden_patch() else ""
                git_ops.stage_hidden_tests(c, spec, hidden)
                _info("staged hidden tests")
            _info(
                f"Entering {'sealed ' if sealed else ''}container "
                f"{c.container_id[:12]} at {spec.environment.workdir}. Type 'exit' to leave."
            )
            cmd.finish(status="ok", exit_code=0, message="interactive shell")
            # Hand off to an interactive docker exec via the parent shell so the
            # TTY is wired up correctly, then clean up the container afterward.
            os.system(
                f"{DOCKER} exec -it --workdir {_shlex.quote(spec.environment.workdir)} "
                f"{c.container_id} bash"
            )
        finally:
            c.stop()


# ---------------------------------------------------------------------------
# task runs / log / report  (DB queries)
# ---------------------------------------------------------------------------


@app.command()
def runs(
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows to show."),
    commands: bool = typer.Option(
        False, "--commands", help="List the command log instead of runs."
    ),
) -> None:
    """List recorded runs (or, with --commands, the command log)."""
    from task_bundle.query_impl import render_commands, render_runs

    with db.session() as conn:
        if commands:
            render_commands(conn, limit)
        else:
            render_runs(conn, limit)


@app.command()
def log(
    ident: str = typer.Argument(..., help="A command id (cmd_...) or run id (run_...)."),
) -> None:
    """Show the log for a command or run id, including per-test results."""
    from task_bundle.query_impl import render_log

    with db.session() as conn:
        if not render_log(conn, ident):
            raise typer.Exit(1)


@app.command()
def report(
    run_id: str = typer.Argument(..., help="A run id (run_...)."),
) -> None:
    """Pretty-print the report.json artifact for a run."""
    from task_bundle.query_impl import render_report

    with db.session() as conn:
        if not render_report(conn, run_id):
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
