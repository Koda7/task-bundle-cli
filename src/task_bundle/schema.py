"""Pydantic models for ``task.json`` -- the single source of truth for a bundle.

A *bundle* is a self-contained directory describing one coding task:

    my-task/
        task.json          # this schema
        description.md      # problem statement shown to the solver
        patch.diff          # golden (oracle) code patch
        tests/hidden.patch  # fail2pass + pass2pass test code, HIDDEN from solver

The schema is intentionally a superset of the assignment's minimal example so the
same format works for SWE-bench-Pro instances *and* hand-written generic tasks.
``extra="forbid"`` is used everywhere so typos in ``task.json`` produce a loud,
actionable error instead of being silently ignored.
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


class _Base(BaseModel):
    # Reject unknown keys so a misspelled field (e.g. "commit" vs "base_commit")
    # surfaces immediately rather than being dropped.
    model_config = ConfigDict(extra="forbid")


class HiddenTestMode(str, enum.Enum):
    """How the hidden (fail2pass/pass2pass) tests get staged into the repo.

    ``patch``        -- apply ``tests/hidden.patch`` with ``git apply`` (default;
                        portable, works for any repo).
    ``git_checkout`` -- check the listed test files out from ``source.fix_commit``
                        (mirrors SWE-bench-Pro's ``before_repo_set_cmd`` exactly).
    """

    patch = "patch"
    git_checkout = "git_checkout"


class RunnerName(str, enum.Enum):
    """Built-in test runners. ``generic`` is the language-agnostic fallback."""

    pytest = "pytest"
    generic = "generic"
    gotest = "gotest"
    jest = "jest"


class Source(_Base):
    """Where the task's code comes from (provenance + reproducibility)."""

    instance_id: str = Field(
        ...,
        description="Stable unique id for the task (e.g. the SWE-bench-Pro instance_id).",
        min_length=1,
    )
    repo: str = Field(..., description="Repository slug, e.g. 'internetarchive/openlibrary'.")
    base_commit: str = Field(
        ...,
        description="Commit the solver starts from (the 'before' state).",
        min_length=4,
    )
    fix_commit: Optional[str] = Field(
        None,
        description="Commit that resolved the issue; source of the golden patch and "
        "hidden tests. Optional for hand-written tasks.",
    )
    repo_language: str = Field(
        "python",
        description="Primary language; informs the default runner. Free-form.",
    )


class Environment(_Base):
    """The container the task runs in."""

    image: str = Field(
        ...,
        description="Fully-qualified Docker image. For SWE-bench-Pro this is "
        "'jefzda/sweap-images:<dockerhub_tag>'; for generic tasks, any image.",
        min_length=1,
    )
    platform: str = Field(
        "linux/amd64",
        description="Docker --platform. SWE-bench-Pro images are amd64.",
    )
    workdir: str = Field(
        "/app",
        description="Absolute path to the repo checkout inside the image.",
    )
    image_digest: Optional[str] = Field(
        None,
        description="Resolved 'sha256:...' digest, pinned at build time for reproducibility.",
    )
    # Conservative resource caps applied to every container we start. These are
    # part of the isolation story (a runaway solver cannot exhaust the host).
    memory: str = Field("4g", description="Hard memory limit (docker --memory).")
    cpus: str = Field("2", description="CPU quota (docker --cpus).")
    pids_limit: int = Field(2048, description="Max processes (docker --pids-limit).", gt=0)


class Setup(_Base):
    """How to (re)create the baseline repo state and stage hidden tests."""

    # The exact, ordered shell commands that reset the repo to a clean baseline.
    # For SWE-bench-Pro this is derived from ``before_repo_set_cmd`` MINUS the
    # final hidden-test checkout line (that is handled separately, only at grading
    # time, so the solver never sees the hidden tests). Empty => use `git reset`.
    reset_commands: list[str] = Field(
        default_factory=list,
        description="Commands run (in workdir) to restore the pristine base state, "
        "with hidden tests NOT present.",
    )
    hidden_test_mode: HiddenTestMode = Field(
        HiddenTestMode.patch,
        description="Strategy for staging hidden tests at grading time.",
    )
    # Files the hidden tests live in. Used by git_checkout mode and, for either
    # mode, to reset just these files to base before staging (so a solver that
    # edited a test file can't poison grading).
    hidden_test_paths: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths of files containing the hidden tests.",
    )


class TestConfig(_Base):
    """How to run tests and collect results."""

    runner: RunnerName = Field(RunnerName.pytest, description="Which runner to use.")
    # {tests} is substituted with the space-joined, shell-quoted test ids/paths.
    # Kept overridable so unusual repos (custom markers, env vars) still work.
    command_template: str = Field(
        "python -m pytest -p no:cacheprovider -rA {tests}",
        description="Shell command template; '{tests}' is replaced with the selected tests.",
    )
    # Visible tests the solver MAY see and run (helps it iterate). These are the
    # repo's pre-existing tests, never the hidden fail2pass/pass2pass set.
    visible_test_paths: list[str] = Field(
        default_factory=list,
        description="Test files/dirs the solver is allowed to see and run.",
    )
    timeout_seconds: int = Field(
        900, description="Wall-clock cap for a single test invocation.", gt=0
    )


class Grading(_Base):
    """The pass/fail contract that defines a correct solution."""

    fail_to_pass: list[str] = Field(
        default_factory=list,
        description="Tests that must FAIL on baseline and PASS after the fix.",
    )
    pass_to_pass: list[str] = Field(
        default_factory=list,
        description="Tests that must PASS both before and after the fix.",
    )

    @model_validator(mode="after")
    def _non_empty(self) -> "Grading":
        if not self.fail_to_pass and not self.pass_to_pass:
            raise ValueError(
                "grading must define at least one fail_to_pass or pass_to_pass test"
            )
        return self


class TaskSpec(_Base):
    """Top-level ``task.json`` document."""

    schema_version: int = Field(
        SCHEMA_VERSION, description="Bundle schema version for forward compatibility."
    )
    name: str = Field(..., description="Human-friendly task name (the bundle dir name).")
    source: Source
    environment: Environment
    setup: Setup = Field(default_factory=Setup)
    test: TestConfig = Field(default_factory=TestConfig)
    grading: Grading

    @model_validator(mode="after")
    def _check_schema_version(self) -> "TaskSpec":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"this CLI understands version {SCHEMA_VERSION}"
            )
        return self
