"""Load and validate a task bundle directory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from task_bundle.schema import TaskSpec

TASK_JSON = "task.json"
DESCRIPTION_MD = "description.md"
PATCH_DIFF = "patch.diff"
HIDDEN_PATCH = "tests/hidden.patch"


class BundleError(Exception):
    """Raised when a bundle is missing files or has an invalid ``task.json``.

    The message is meant to be shown directly to the user, so it should be
    specific and actionable.
    """


def _format_validation_error(path: Path, err: ValidationError) -> str:
    """Turn a Pydantic error into a compact, human-readable list of problems."""
    lines = [f"Invalid {path}:"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


@dataclass
class Bundle:
    """A validated task bundle rooted at ``root``."""

    root: Path
    spec: TaskSpec

    # --- component paths -------------------------------------------------

    @property
    def task_json_path(self) -> Path:
        return self.root / TASK_JSON

    @property
    def description_path(self) -> Path:
        return self.root / DESCRIPTION_MD

    @property
    def patch_path(self) -> Path:
        return self.root / PATCH_DIFF

    @property
    def hidden_patch_path(self) -> Path:
        return self.root / HIDDEN_PATCH

    # --- convenience -----------------------------------------------------

    @property
    def name(self) -> str:
        return self.spec.name

    def read_description(self) -> str:
        return self.description_path.read_text(encoding="utf-8")

    def read_patch(self) -> str:
        return self.patch_path.read_text(encoding="utf-8")

    def read_hidden_patch(self) -> str:
        return self.hidden_patch_path.read_text(encoding="utf-8")

    def has_golden_patch(self) -> bool:
        return self.patch_path.is_file() and self.patch_path.stat().st_size > 0

    def has_hidden_patch(self) -> bool:
        return self.hidden_patch_path.is_file() and self.hidden_patch_path.stat().st_size > 0


def load_bundle(path: str | Path) -> Bundle:
    """Load and validate a bundle directory.

    Raises ``BundleError`` with a clear message if the directory, ``task.json``,
    or required components are missing/invalid.
    """
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise BundleError(f"Bundle path does not exist: {root}")
    if not root.is_dir():
        raise BundleError(f"Bundle path is not a directory: {root}")

    task_json = root / TASK_JSON
    if not task_json.is_file():
        raise BundleError(
            f"Missing {TASK_JSON} in {root}.\n"
            f"A bundle must contain {TASK_JSON}; scaffold one with `task init`."
        )

    try:
        raw = json.loads(task_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BundleError(f"{task_json} is not valid JSON: {e}") from e

    try:
        spec = TaskSpec.model_validate(raw)
    except ValidationError as e:
        raise BundleError(_format_validation_error(task_json, e)) from e

    return Bundle(root=root, spec=spec)


def check_bundle_files(bundle: Bundle, *, require_hidden: bool = True) -> list[str]:
    """Return a list of human-readable problems with the bundle's files.

    Used by ``task validate --offline``. An empty list means the bundle's files
    are structurally present. (Patch *applicability* is checked separately via
    git, since that may need the repo.)
    """
    problems: list[str] = []

    if not bundle.description_path.is_file():
        problems.append(f"missing {DESCRIPTION_MD} (the problem statement)")

    if bundle.spec.grading.fail_to_pass and not bundle.has_golden_patch():
        problems.append(
            f"missing or empty {PATCH_DIFF}; required because grading defines "
            f"fail_to_pass tests that only pass after the golden patch"
        )

    if require_hidden and not bundle.has_hidden_patch():
        problems.append(
            f"missing or empty {HIDDEN_PATCH}; the hidden tests (fail2pass/"
            f"pass2pass) must be stored here and withheld from the solver"
        )

    # If using git_checkout staging, we need a fix_commit and the file list.
    from task_bundle.schema import HiddenTestMode  # local import to avoid cycle

    if bundle.spec.setup.hidden_test_mode is HiddenTestMode.git_checkout:
        if not bundle.spec.source.fix_commit:
            problems.append(
                "setup.hidden_test_mode is 'git_checkout' but source.fix_commit is unset"
            )
        if not bundle.spec.setup.hidden_test_paths:
            problems.append(
                "setup.hidden_test_mode is 'git_checkout' but setup.hidden_test_paths is empty"
            )

    return problems
