"""Bundle loader + offline-validation tests (no Docker)."""

from __future__ import annotations

import json

import pytest

from task_bundle.bundle import BundleError, check_bundle_files, load_bundle

GOOD_TASK = {
    "name": "demo",
    "source": {
        "instance_id": "i",
        "repo": "a/b",
        "base_commit": "abcd1234",
        "fix_commit": "ef567890",
    },
    "environment": {"image": "img:tag"},
    "setup": {"hidden_test_mode": "patch"},
    "grading": {"fail_to_pass": ["t::a"], "pass_to_pass": ["t::b"]},
}


def _write_bundle(root, task=GOOD_TASK, patch="diff --git\n", hidden="diff --git\n", desc="# d\n"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.json").write_text(json.dumps(task), encoding="utf-8")
    if desc is not None:
        (root / "description.md").write_text(desc, encoding="utf-8")
    if patch is not None:
        (root / "patch.diff").write_text(patch, encoding="utf-8")
    if hidden is not None:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "hidden.patch").write_text(hidden, encoding="utf-8")


def test_load_valid_bundle(tmp_path):
    root = tmp_path / "b"
    _write_bundle(root)
    bundle = load_bundle(root)
    assert bundle.name == "demo"
    assert bundle.has_golden_patch()
    assert bundle.has_hidden_patch()
    assert check_bundle_files(bundle) == []


def test_missing_task_json(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(BundleError, match="Missing task.json"):
        load_bundle(tmp_path / "empty")


def test_invalid_json(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    (root / "task.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BundleError, match="not valid JSON"):
        load_bundle(root)


def test_invalid_schema_message(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    (root / "task.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    with pytest.raises(BundleError, match="Invalid"):
        load_bundle(root)


def test_check_files_flags_missing_patch(tmp_path):
    root = tmp_path / "b"
    _write_bundle(root, patch="")  # empty golden patch
    bundle = load_bundle(root)
    problems = check_bundle_files(bundle)
    assert any("patch.diff" in p for p in problems)


def test_nonexistent_path(tmp_path):
    with pytest.raises(BundleError, match="does not exist"):
        load_bundle(tmp_path / "nope")
