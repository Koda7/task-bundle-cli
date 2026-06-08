"""Git operations performed *inside* the task container.

These functions take a running ``TaskContainer`` and manipulate the repo at its
workdir. Keeping all repo mutation in one place makes the harness phases
(baseline / golden / solver / grading) easy to read and audit.

The central invariant: hidden tests are staged ONLY by ``stage_hidden_tests``,
which is never called while a solver is running -- so the solver can never see
the fail2pass / pass2pass tests.
"""

from __future__ import annotations

import shlex

from task_bundle.docker_env import TaskContainer
from task_bundle.proc import CommandResult
from task_bundle.schema import HiddenTestMode, TaskSpec


class GitError(Exception):
    """A git operation inside the container failed."""


def _q(path: str) -> str:
    return shlex.quote(path)


def clone_repo_into_workdir(
    container: TaskContainer, spec: TaskSpec, *, repo_url: str
) -> CommandResult:
    """Clone ``repo_url`` at ``base_commit`` into the container's workdir.

    Used by the generic (non-SWE-Bench-Pro) path so an arbitrary repo can be
    packaged into a container that only provides the toolchain. Requires network
    inside the container, so this MUST run in a non-sealed build container -- it
    is a deliberate, build-time-only exception to the otherwise-sealed model.
    """
    workdir = shlex.quote(container.workdir)
    commit = shlex.quote(spec.source.base_commit)
    url = shlex.quote(repo_url)
    script = (
        f"set -e; "
        f"if [ -d {workdir}/.git ]; then echo 'repo already present'; exit 0; fi; "
        f"mkdir -p {workdir} && cd {workdir} && "
        f"git init -q && git remote add origin {url} && "
        # Fetch just the one commit when the server allows it; fall back to full.
        f"(git fetch -q --depth 1 origin {commit} || git fetch -q origin) && "
        f"git checkout -q {commit}"
    )
    # Use exec (not bash()) so we don't prepend `cd <workdir>` -- the workdir may
    # not exist yet in a bare toolchain image; the script creates it via mkdir -p.
    res = container.exec(["bash", "-lc", script], timeout=1800)
    if not res.ok:
        detail = (res.stderr or res.stdout).strip()
        hint = ""
        if "git: command not found" in detail or "git: not found" in detail:
            hint = (
                "\nhint: the image has no `git`. Use an image that includes it "
                "(e.g. python:3.12 rather than -slim), or pre-bake the repo."
            )
        raise GitError(
            f"failed to clone {repo_url} @ {spec.source.base_commit}:\n{detail}{hint}"
        )
    return res


def reset_to_base(container: TaskContainer, spec: TaskSpec) -> CommandResult:
    """Restore the pristine baseline repo state (hidden tests NOT present).

    Prefers the bundle's explicit ``setup.reset_commands`` (derived from the
    upstream ``before_repo_set_cmd`` minus the hidden-test checkout). Falls back
    to a standard hard reset + clean to the base commit.
    """
    base = spec.source.base_commit
    if spec.setup.reset_commands:
        script = " && ".join(spec.setup.reset_commands)
    else:
        script = (
            f"git reset --hard {shlex.quote(base)} && "
            f"git clean -fd && "
            f"git checkout {shlex.quote(base)}"
        )
    res = container.bash(script, timeout=300)
    if not res.ok:
        raise GitError(
            f"failed to reset repo to base {base}:\n{(res.stderr or res.stdout).strip()}"
        )
    return res


def apply_patch(
    container: TaskContainer, patch_text: str, *, dest: str = "/tmp/_apply.patch"
) -> CommandResult:
    """Apply a unified diff inside the container.

    Tries ``git apply --3way`` first (more robust to context drift), then a plain
    ``git apply``. Raises ``GitError`` if both fail, surfacing git's own message.
    """
    container.copy_in_text(patch_text, dest)
    # --3way can leave conflict markers if it partially applies; we treat a
    # nonzero exit as failure and fall back to a strict apply.
    res = container.bash(f"git apply --3way {dest}", timeout=180)
    if res.ok:
        return res
    strict = container.bash(f"git apply {dest}", timeout=180)
    if strict.ok:
        return strict
    raise GitError(
        "patch did not apply (tried --3way and strict):\n"
        f"{(res.stderr or res.stdout).strip()}\n---\n{(strict.stderr or strict.stdout).strip()}"
    )


def patch_applies(
    container: TaskContainer, patch_text: str, *, dest: str = "/tmp/_check.patch"
) -> bool:
    """Return True if the patch would apply cleanly (``git apply --check``)."""
    container.copy_in_text(patch_text, dest)
    res = container.bash(f"git apply --check {dest}", timeout=120)
    return res.ok


def stage_hidden_tests(
    container: TaskContainer,
    spec: TaskSpec,
    hidden_patch_text: str = "",
    *,
    dest: str = "/tmp/_hidden.patch",
) -> CommandResult:
    """Stage the hidden (fail2pass/pass2pass) tests into the repo.

    Called only at grading time. First resets the hidden-test files to base (so a
    solver that touched a test file cannot influence grading), then applies the
    hidden tests via the configured mode.
    """
    # Defensively reset just the hidden-test files to base before staging.
    if spec.setup.hidden_test_paths:
        paths = " ".join(_q(p) for p in spec.setup.hidden_test_paths)
        container.bash(
            f"git checkout {shlex.quote(spec.source.base_commit)} -- {paths} "
            f"2>/dev/null || true",
            timeout=120,
        )

    if spec.setup.hidden_test_mode is HiddenTestMode.git_checkout:
        if not spec.source.fix_commit or not spec.setup.hidden_test_paths:
            raise GitError(
                "git_checkout hidden-test mode requires source.fix_commit and "
                "setup.hidden_test_paths"
            )
        paths = " ".join(_q(p) for p in spec.setup.hidden_test_paths)
        res = container.bash(
            f"git checkout {shlex.quote(spec.source.fix_commit)} -- {paths}",
            timeout=180,
        )
        if res.ok:
            return res
        # The fix commit may be unreachable -- e.g. after --strip-git-history, or
        # on a machine where it was never fetched. Fall back to the stored
        # hidden.patch, which is portable and always written at init time.
        if hidden_patch_text.strip():
            return apply_patch(container, hidden_patch_text, dest=dest)
        raise GitError(
            f"failed to checkout hidden tests from {spec.source.fix_commit} and no "
            f"hidden.patch fallback available:\n{(res.stderr or res.stdout).strip()}"
        )

    # Default: apply the stored hidden.patch.
    if not hidden_patch_text.strip():
        raise GitError(
            "hidden_test_mode is 'patch' but no hidden patch text was provided "
            "(tests/hidden.patch is empty or missing)"
        )
    return apply_patch(container, hidden_patch_text, dest=dest)


def capture_diff(container: TaskContainer, spec: TaskSpec) -> str:
    """Return the working-tree diff vs base (the normalized 'solver patch').

    This is how we record whatever a solver did -- whether it mutated files in
    place (agent) or we applied a patch (golden). We diff against the base commit
    and EXCLUDE the hidden-test paths so the recorded patch is purely the code
    change, never the staged tests.
    """
    base = spec.source.base_commit
    exclude = ""
    if spec.setup.hidden_test_paths:
        # `git diff <base> -- . ':(exclude)path'` pathspecs.
        excludes = " ".join(f"':(exclude){p}'" for p in spec.setup.hidden_test_paths)
        exclude = f" -- . {excludes}"
    res = container.bash(
        f"git -c core.fileMode=false add -A >/dev/null 2>&1; "
        f"git -c core.fileMode=false diff --no-color {shlex.quote(base)}{exclude}",
        timeout=120,
    )
    if not res.ok:
        raise GitError(f"failed to capture diff:\n{(res.stderr or res.stdout).strip()}")
    return res.stdout


def strip_git_history(container: TaskContainer, spec: TaskSpec) -> CommandResult:
    """Remove future git history so an agent can't read the fix from the log.

    SWE-bench-Pro public images leak the fix commit (and feature branches) in the
    repo's git history, which a curious agent could ``git log`` its way into.

    Fast path: shallow-clone the repo locally (``--depth=1``) and swap the
    ``.git`` directory. This copies only the current commit and avoids the
    expensive ``git gc --aggressive`` walk (~2-3 s vs ~60+ s under emulation).
    Falls back to the legacy orphan-branch + GC approach if the clone fails.
    """
    wd = shlex.quote(container.workdir)
    fast = (
        f"git clone --depth=1 file://{wd} /tmp/_shallow_strip 2>/dev/null && "
        # Preserve submodule data (.git/modules/) so submodule worktrees
        # that reference ../../.git/modules/... don't break.
        f"if [ -d {wd}/.git/modules ]; then "
        f"cp -a {wd}/.git/modules /tmp/_shallow_strip/.git/modules; fi && "
        f"rm -rf {wd}/.git && "
        f"mv /tmp/_shallow_strip/.git {wd}/.git && "
        f"rm -rf /tmp/_shallow_strip && "
        "echo stripped"
    )
    res = container.bash(fast, timeout=120)
    if res.ok:
        return res

    container.bash("rm -rf /tmp/_shallow_strip", timeout=30)
    fallback = (
        f"git checkout -q --detach {shlex.quote(spec.source.base_commit)} && "
        "for b in $(git branch | sed 's/^[* ]*//'); do git branch -D \"$b\" >/dev/null 2>&1 || true; done && "
        "for r in $(git remote); do git remote remove \"$r\" >/dev/null 2>&1 || true; done && "
        "git tag -l | xargs -r git tag -d >/dev/null 2>&1 || true; "
        "git reflog expire --expire=now --all >/dev/null 2>&1 || true; "
        "git gc --prune=now --aggressive >/dev/null 2>&1 || true; "
        "echo stripped"
    )
    return container.bash(fallback, timeout=300)
