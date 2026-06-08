"""Docker container lifecycle + the isolation boundary.

We drive Docker through the ``docker`` CLI (not the SDK) on purpose: every action
is a literal, logged shell command a user can replay -- a concrete reproducibility
property. The container is the trust boundary for solver code:

* ``--network none``                -- no egress; an untrusted solver cannot phone
                                       home or reach API credentials.
* ``--cap-drop ALL``                -- drop Linux capabilities.
* ``--security-opt no-new-privileges`` -- no privilege escalation.
* ``--memory/--cpus/--pids-limit``  -- a runaway solver cannot exhaust the host.
* ``--entrypoint sleep ... <ttl>``  -- keepalive container we ``exec`` into. (The
                                       sweap images default-entrypoint to bash;
                                       per upstream guidance we don't invoke bash
                                       as the entrypoint, we override it.)

The LLM never runs inside here. The host orchestrator (trusted code) translates
agent tool calls into ``docker exec`` invocations against this sealed box.
"""

from __future__ import annotations

import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from task_bundle.proc import CommandRecorder, CommandResult, run

DOCKER = "docker"


class DockerError(Exception):
    """A docker command failed in a way we can't proceed from."""


def _require_ok(result: CommandResult, what: str) -> CommandResult:
    if not result.ok:
        raise DockerError(
            f"{what} failed (exit {result.returncode}):\n"
            f"  $ {result.display}\n"
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def daemon_available() -> tuple[bool, str]:
    """Return (ok, message). Used to give a friendly error before doing work."""
    res = run([DOCKER, "version", "--format", "{{.Server.Version}}"], timeout=15)
    if res.timed_out:
        return False, "Docker daemon did not respond (is Docker Desktop running?)"
    if not res.ok:
        return False, (res.stderr or res.stdout).strip() or "docker not available"
    return True, res.stdout.strip()


def image_exists_locally(image: str) -> bool:
    res = run([DOCKER, "image", "inspect", image], timeout=30)
    return res.ok


def image_digest(image: str) -> Optional[str]:
    """Return the local RepoDigest (sha256:...) for an image, if known."""
    res = run(
        [DOCKER, "image", "inspect", "--format", "{{json .RepoDigests}}", image],
        timeout=30,
    )
    if not res.ok:
        return None
    import json

    try:
        digests = json.loads(res.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None
    for d in digests:
        if "@" in d:
            return d.split("@", 1)[1]
    # Fall back to the image config Id if no RepoDigest (e.g. locally built).
    res2 = run([DOCKER, "image", "inspect", "--format", "{{.Id}}", image], timeout=30)
    return res2.stdout.strip() or None if res2.ok else None


def ensure_image(
    image: str,
    *,
    platform: str = "linux/amd64",
    recorder: Optional[CommandRecorder] = None,
    force_pull: bool = False,
) -> str:
    """Ensure ``image`` is available locally; pull if missing. Returns its digest.

    Idempotent: skips the (large, possibly emulated) pull when the image is
    already present unless ``force_pull`` is set.
    """
    if force_pull or not image_exists_locally(image):
        res = run(
            [DOCKER, "pull", "--platform", platform, image],
            timeout=3600,
            recorder=recorder,
        )
        _require_ok(res, f"docker pull {image}")
    digest = image_digest(image)
    return digest or "unknown"


def commit_container(
    container_id: str, image_tag: str, *, recorder: Optional[CommandRecorder] = None
) -> str:
    """Commit a container's filesystem to ``image_tag`` (used by `build --clone`)."""
    res = run([DOCKER, "commit", container_id, image_tag], timeout=300, recorder=recorder)
    _require_ok(res, f"docker commit -> {image_tag}")
    return res.stdout.strip()


def inspect_workdir(image: str) -> Optional[str]:
    res = run(
        [DOCKER, "image", "inspect", "--format", "{{.Config.WorkingDir}}", image],
        timeout=30,
    )
    if res.ok:
        wd = res.stdout.strip()
        return wd or None
    return None


@dataclass
class TaskContainer:
    """A running, sealed container scoped to a single task operation.

    Use as a context manager:

        with TaskContainer(image, workdir="/app") as c:
            c.exec(["python", "-m", "pytest", ...])
    """

    image: str
    workdir: str = "/app"
    platform: str = "linux/amd64"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 2048
    network: str = "none"
    ttl_seconds: int = 7200
    recorder: Optional[CommandRecorder] = None

    container_id: str = field(default="", init=False)

    # --- lifecycle -------------------------------------------------------

    def start(self) -> "TaskContainer":
        args = [
            DOCKER,
            "run",
            "-d",
            "--rm",
            "--platform",
            self.platform,
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--workdir",
            self.workdir,
            "--entrypoint",
            "sleep",
            self.image,
            str(self.ttl_seconds),
        ]
        res = _require_ok(run(args, timeout=180, recorder=self.recorder), "docker run")
        self.container_id = res.stdout.strip()
        return self

    def stop(self) -> None:
        if self.container_id:
            # --rm means stop also removes it.
            run([DOCKER, "kill", self.container_id], timeout=60, recorder=self.recorder)
            self.container_id = ""

    def __enter__(self) -> "TaskContainer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # --- exec ------------------------------------------------------------

    def exec(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        workdir: Optional[str] = None,
        user: Optional[str] = None,
    ) -> CommandResult:
        """Run a command inside the container (argv form, no shell)."""
        args = [DOCKER, "exec"]
        if workdir:
            args += ["--workdir", workdir]
        if user:
            args += ["--user", user]
        args += [self.container_id, *command]
        return run(args, timeout=timeout, recorder=self.recorder)

    def bash(self, script: str, *, timeout: float | None = None) -> CommandResult:
        """Run a bash snippet inside the container (in the workdir)."""
        return self.exec(
            ["bash", "-lc", f"cd {shlex.quote(self.workdir)} && {script}"],
            timeout=timeout,
        )

    # --- file transfer ---------------------------------------------------

    def copy_in_text(self, text: str, dest_path: str) -> None:
        """Write ``text`` to ``dest_path`` inside the container via ``docker cp``.

        Using ``docker cp`` (rather than echoing through a shell) avoids quoting
        pitfalls and is how the agent's ``write_file`` tool lands bytes safely.

        The host temp file is fully written, flushed, fsync'd and CLOSED before
        ``docker cp`` reads it (delete=False), then removed -- avoiding any race
        where the file is unlinked or partially written during the copy.
        """
        import os

        fd, tmp_name = tempfile.mkstemp(suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as tf:
                tf.write(text)
                tf.flush()
                os.fsync(tf.fileno())
            # docker cp preserves the host file's mode/uid. We run with
            # --cap-drop ALL, which strips CAP_DAC_OVERRIDE -- so even the
            # container's root cannot read a file whose mode excludes it.
            # Make the source world-readable so the in-container user can read
            # it regardless of uid mapping. (Contents are task data, not secret.)
            os.chmod(tmp_name, 0o644)
            res = run(
                [DOCKER, "cp", tmp_name, f"{self.container_id}:{dest_path}"],
                timeout=120,
                recorder=self.recorder,
            )
            _require_ok(res, f"docker cp -> {dest_path}")
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def copy_out(self, src_path: str, dest: Path) -> None:
        res = run(
            [DOCKER, "cp", f"{self.container_id}:{src_path}", str(dest)],
            timeout=120,
            recorder=self.recorder,
        )
        _require_ok(res, f"docker cp <- {src_path}")

    def read_file(self, path: str, *, timeout: float = 60) -> CommandResult:
        """Read a file inside the container (the agent's ``read_file`` tool)."""
        return self.exec(["cat", path], timeout=timeout)
