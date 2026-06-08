"""Write task bundle directories to disk.

Two entry points back ``task init``:

* ``scaffold_from_swebench_pro`` -- pulls an instance from the dataset and writes
  a fully-populated bundle (the flagship path).
* ``scaffold_generic`` -- writes a skeleton bundle for a hand-written task given a
  repo URL + commit, leaving patch/tests/grading as TODO stubs for the author.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from task_bundle import swebench_pro as sp
from task_bundle.bundle import (
    DESCRIPTION_MD,
    HIDDEN_PATCH,
    PATCH_DIFF,
    TASK_JSON,
    Bundle,
    load_bundle,
)
from task_bundle.schema import TaskSpec


class ScaffoldError(Exception):
    pass


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_task_json(root: Path, spec_dict: dict) -> None:
    # Validate before writing so we never emit an invalid bundle.
    TaskSpec.model_validate(spec_dict)
    _write(root / TASK_JSON, json.dumps(spec_dict, indent=2) + "\n")


def scaffold_from_swebench_pro(
    instance_id: str,
    dest: Path,
    *,
    name: Optional[str] = None,
    overwrite: bool = False,
    cache_dir: Optional[Path] = None,
) -> Bundle:
    """Create a bundle from a SWE-Bench Pro instance id."""
    inst = sp.load_instance(instance_id, cache_dir=cache_dir)
    bundle_name = name or _default_name(inst.repo, inst.instance_id)
    root = (dest / bundle_name).resolve()

    if root.exists() and any(root.iterdir()) and not overwrite:
        raise ScaffoldError(
            f"destination {root} already exists and is non-empty; pass overwrite to replace"
        )
    root.mkdir(parents=True, exist_ok=True)

    spec_dict = sp.to_bundle_dict(inst, name=bundle_name)
    _write_task_json(root, spec_dict)
    _write(root / DESCRIPTION_MD, sp.build_description(inst))
    _write(root / PATCH_DIFF, str(inst.get("patch") or ""))
    # The hidden tests: SWE-Bench Pro's test_patch is the diff of test files.
    _write(root / HIDDEN_PATCH, str(inst.get("test_patch") or ""))

    return load_bundle(root)


def scaffold_generic(
    dest: Path,
    *,
    name: str,
    repo: str,
    commit: str,
    image: str,
    workdir: str = "/app",
    runner: str = "pytest",
    overwrite: bool = False,
) -> Bundle:
    """Create a skeleton bundle for a hand-written task.

    Writes a valid-but-stub bundle the author fills in: an empty patch.diff,
    an empty tests/hidden.patch, and grading with placeholder ids. We use
    ``hidden_test_mode='patch'`` here since there's no fix commit to check out.
    """
    root = (dest / name).resolve()
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise ScaffoldError(
            f"destination {root} already exists and is non-empty; pass overwrite to replace"
        )
    root.mkdir(parents=True, exist_ok=True)

    spec_dict = {
        "schema_version": 1,
        "name": name,
        "source": {
            "instance_id": name,
            "repo": repo,
            "base_commit": commit,
            "fix_commit": None,
            "repo_language": "python" if runner == "pytest" else "unknown",
        },
        "environment": {"image": image, "platform": "linux/amd64", "workdir": workdir},
        "setup": {
            "reset_commands": [
                f"git reset --hard {commit}",
                "git clean -fd",
                f"git checkout {commit}",
            ],
            "hidden_test_mode": "patch",
            "hidden_test_paths": [],
        },
        "test": {
            "runner": runner,
            "command_template": (
                "python -m pytest -p no:cacheprovider -rA {tests}"
                if runner == "pytest"
                else "{tests}"
            ),
            "visible_test_paths": [],
            "timeout_seconds": 900,
        },
        # Grading must be non-empty to validate; author replaces these.
        "grading": {
            "fail_to_pass": ["REPLACE::with_a_real_failing_test"],
            "pass_to_pass": [],
        },
    }
    _write_task_json(root, spec_dict)
    _write(
        root / DESCRIPTION_MD,
        f"# {name}\n\n## Problem Statement\n\n_Describe the task here._\n",
    )
    _write(root / PATCH_DIFF, "")
    _write(
        root / HIDDEN_PATCH,
        "# Put the hidden fail2pass/pass2pass test diff here (unified diff).\n",
    )
    return load_bundle(root)


def _default_name(repo: str, instance_id: str) -> str:
    base = repo.split("/")[-1]
    # short, filesystem-friendly suffix from the instance id
    suffix = instance_id.replace("instance_", "")[:12]
    return f"{base}-{suffix}".replace("/", "-")
