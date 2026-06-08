"""Schema validation tests (no Docker)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from task_bundle.schema import HiddenTestMode, RunnerName, TaskSpec

VALID = {
    "name": "demo",
    "source": {
        "instance_id": "i",
        "repo": "a/b",
        "base_commit": "90475fb6c168",
        "fix_commit": "4a5d2a7d",
    },
    "environment": {"image": "img:tag"},
    "grading": {"fail_to_pass": ["t::a"], "pass_to_pass": ["t::b"]},
}


def test_valid_spec_defaults():
    spec = TaskSpec.model_validate(VALID)
    assert spec.name == "demo"
    assert spec.environment.workdir == "/app"
    assert spec.environment.platform == "linux/amd64"
    assert spec.test.runner is RunnerName.pytest
    assert spec.setup.hidden_test_mode is HiddenTestMode.patch


def test_unknown_key_rejected():
    bad = {**VALID, "source": {**VALID["source"], "commit": "typo"}}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)


def test_empty_grading_rejected():
    bad = {**VALID, "grading": {"fail_to_pass": [], "pass_to_pass": []}}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)


def test_missing_required_field_rejected():
    bad = {"name": "x", "environment": {"image": "i"}, "grading": {"fail_to_pass": ["t"]}}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)


def test_bad_schema_version_rejected():
    bad = {**VALID, "schema_version": 999}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)
