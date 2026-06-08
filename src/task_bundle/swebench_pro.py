"""Ingest a SWE-Bench Pro instance into a task bundle.

We download the dataset's single parquet file (``data/test-00000-of-00001.parquet``)
via ``hf_hub_download`` -- light, cached, and no heavy ``datasets`` dependency --
then look up one instance by id and translate its fields into our bundle format.

Field mapping (SWE-Bench Pro -> bundle):

    dockerhub_tag             -> environment.image = jefzda/sweap-images:<tag>
    base_commit               -> source.base_commit
    instance_id (->fix sha)   -> source.fix_commit
    before_repo_set_cmd       -> setup.reset_commands (MINUS the hidden-test
                                 checkout line, which we never run pre-solve)
    selected_test_files_to_run-> setup.hidden_test_paths + test.visible? (no:
                                 these ARE the hidden test files)
    test_patch                -> tests/hidden.patch
    patch                     -> patch.diff (golden)
    problem_statement+        -> description.md
      requirements+interface
    fail_to_pass / pass_to_pass -> grading
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DATASET_REPO = "ScaleAI/SWE-bench_Pro"
PARQUET_PATH = "data/test-00000-of-00001.parquet"
IMAGE_PREFIX = "jefzda/sweap-images"


class SWEBenchProError(Exception):
    """Problem downloading or parsing the SWE-Bench Pro dataset."""


@dataclass
class Instance:
    """A parsed SWE-Bench Pro row."""

    raw: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def instance_id(self) -> str:
        return str(self.raw["instance_id"])

    @property
    def repo(self) -> str:
        return str(self.raw["repo"])

    @property
    def base_commit(self) -> str:
        return str(self.raw["base_commit"])

    @property
    def repo_language(self) -> str:
        return str(self.raw.get("repo_language") or "python")

    @property
    def image(self) -> str:
        tag = str(self.raw["dockerhub_tag"])
        return f"{IMAGE_PREFIX}:{tag}"

    @property
    def fail_to_pass(self) -> list[str]:
        return _as_list(self.raw.get("fail_to_pass"))

    @property
    def pass_to_pass(self) -> list[str]:
        return _as_list(self.raw.get("pass_to_pass"))

    @property
    def selected_test_files(self) -> list[str]:
        return _as_list(self.raw.get("selected_test_files_to_run"))

    @property
    def fix_commit(self) -> Optional[str]:
        """The instance (fix) commit, recovered from the instance_id.

        Instance ids look like:
            instance_<owner>__<repo>-<FIX_SHA>-v<...>
        We extract the 40-hex SHA that follows the repo segment.
        """
        # The base_commit is one sha; the fix sha is a different 40-hex token.
        shas = re.findall(r"[0-9a-f]{40}", self.instance_id)
        for s in shas:
            if s != self.base_commit:
                return s
        # Fall back to parsing before_repo_set_cmd's final checkout.
        cmd = str(self.raw.get("before_repo_set_cmd") or "")
        m = re.search(r"git checkout ([0-9a-f]{40}) --", cmd)
        return m.group(1) if m else None

    def reset_commands(self) -> list[str]:
        """Reset commands derived from before_repo_set_cmd, EXCLUDING the line
        that checks out hidden tests (so the solver baseline has no hidden tests).
        """
        cmd = str(self.raw.get("before_repo_set_cmd") or "")
        lines = [ln.strip() for ln in cmd.splitlines() if ln.strip()]
        if not lines:
            # sensible default
            return [
                f"git reset --hard {self.base_commit}",
                "git clean -fd",
                f"git checkout {self.base_commit}",
            ]
        kept: list[str] = []
        fix = self.fix_commit
        for ln in lines:
            # Drop any "git checkout <fix_sha> -- <test files>" lines.
            if fix and ln.startswith("git checkout") and fix in ln and " -- " in ln:
                continue
            kept.append(ln)
        return kept


def _as_list(value: Any) -> list[str]:
    """Coerce a stringified-list / list field into a list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, (list, tuple)):
                return [str(v) for v in parsed]
        except (ValueError, SyntaxError):
            pass
        return [s]
    return [str(value)]


def _download_parquet(cache_dir: Optional[Path] = None) -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:  # pragma: no cover
        raise SWEBenchProError(
            "huggingface_hub is required to fetch SWE-Bench Pro; install with "
            "`pip install huggingface_hub`"
        ) from e
    try:
        path = hf_hub_download(
            repo_id=DATASET_REPO,
            filename=PARQUET_PATH,
            repo_type="dataset",
            cache_dir=str(cache_dir) if cache_dir else None,
        )
    except Exception as e:  # network / auth / not found
        raise SWEBenchProError(f"failed to download SWE-Bench Pro parquet: {e}") from e
    return Path(path)


def load_instance(instance_id: str, *, cache_dir: Optional[Path] = None) -> Instance:
    """Find a single instance by id. Matches exact or by unique substring."""
    import pyarrow.parquet as pq

    path = _download_parquet(cache_dir)
    table = pq.read_table(path)
    cols = table.column_names
    if "instance_id" not in cols:
        raise SWEBenchProError("dataset parquet has no instance_id column")

    ids = table.column("instance_id").to_pylist()
    # exact match first, then unique substring
    idx = None
    if instance_id in ids:
        idx = ids.index(instance_id)
    else:
        matches = [i for i, v in enumerate(ids) if instance_id in str(v)]
        if len(matches) == 1:
            idx = matches[0]
        elif len(matches) > 1:
            sample = ", ".join(str(ids[i]) for i in matches[:5])
            raise SWEBenchProError(
                f"{len(matches)} instances match {instance_id!r}; be more specific. "
                f"e.g. {sample}"
            )
    if idx is None:
        raise SWEBenchProError(
            f"instance {instance_id!r} not found in SWE-Bench Pro ({len(ids)} rows)"
        )

    row = {c: table.column(c)[idx].as_py() for c in cols}
    return Instance(raw=row)


def build_description(inst: Instance) -> str:
    """Assemble description.md from problem_statement + requirements + interface.

    Per the paper (3.2/4.2), the agent is given all three; the interface in
    particular prevents false-negative naming mismatches.
    """
    problem = str(inst.get("problem_statement") or "").strip()
    requirements = str(inst.get("requirements") or "").strip()
    interface = str(inst.get("interface") or "").strip()

    parts = [f"# {inst.repo} — {inst.instance_id}", ""]
    parts += ["## Problem Statement", "", problem or "_(none provided)_", ""]
    if requirements and requirements.lower() not in {"none", "no new interfaces are introduced."}:
        parts += ["## Requirements", "", requirements, ""]
    if interface and interface.lower() not in {
        "none",
        "no new interfaces are introduced",
        "no new interfaces are introduced.",
    }:
        parts += ["## Interface", "", interface, ""]
    return "\n".join(parts).strip() + "\n"


def to_bundle_dict(inst: Instance, *, name: str) -> dict[str, Any]:
    """Build the task.json document (as a dict) for an instance."""
    return {
        "schema_version": 1,
        "name": name,
        "source": {
            "instance_id": inst.instance_id,
            "repo": inst.repo,
            "base_commit": inst.base_commit,
            "fix_commit": inst.fix_commit,
            "repo_language": inst.repo_language,
        },
        "environment": {
            "image": inst.image,
            "platform": "linux/amd64",
            "workdir": "/app",
        },
        "setup": {
            "reset_commands": inst.reset_commands(),
            # SWE-Bench Pro stages hidden tests by checking them out from the fix
            # commit (exactly what before_repo_set_cmd's last line does). We mirror
            # that with git_checkout mode AND also write tests/hidden.patch so the
            # bundle is portable to machines without the fix commit reachable.
            "hidden_test_mode": "git_checkout",
            "hidden_test_paths": inst.selected_test_files,
        },
        "test": {
            "runner": "pytest" if inst.repo_language == "python" else "generic",
            "command_template": _command_template_for(inst.repo_language),
            "visible_test_paths": [],
            "timeout_seconds": 900,
        },
        "grading": {
            "fail_to_pass": inst.fail_to_pass,
            "pass_to_pass": inst.pass_to_pass,
        },
    }


def _command_template_for(language: str) -> str:
    if language == "python":
        return "python -m pytest -p no:cacheprovider -rA {tests}"
    if language == "go":
        return "go test -run {tests} ./..."
    if language in {"javascript", "typescript"}:
        return "npx jest {tests}"
    return "{tests}"
