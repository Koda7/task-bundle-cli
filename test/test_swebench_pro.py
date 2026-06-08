"""SWE-Bench Pro field-parsing tests (no network -- exercises pure logic)."""

from __future__ import annotations

from task_bundle.swebench_pro import Instance, _as_list

# A synthetic row mirroring the dataset's column shapes (stringified lists, etc.).
ROW = {
    "instance_id": "instance_internetarchive__openlibrary-4a5d2a7d24c9e4c11d3069220c0685b736d5ecde-vDEADBEEF",
    "repo": "internetarchive/openlibrary",
    "base_commit": "90475fb6c168e8317e22bd5fbe057d98e570a715",
    "repo_language": "python",
    "dockerhub_tag": "internetarchive.openlibrary-...-vDEADBEEF",
    "fail_to_pass": "['openlibrary/tests/core/test_wikidata.py::test_get_statement_values']",
    "pass_to_pass": "['t::a', 't::b']",
    "selected_test_files_to_run": '["openlibrary/tests/core/test_wikidata.py"]',
    "before_repo_set_cmd": (
        "git reset --hard 90475fb6c168e8317e22bd5fbe057d98e570a715\n"
        "git clean -fd \n"
        "git checkout 90475fb6c168e8317e22bd5fbe057d98e570a715 \n"
        "git checkout 4a5d2a7d24c9e4c11d3069220c0685b736d5ecde -- openlibrary/tests/core/test_wikidata.py"
    ),
    "patch": "diff --git a/x b/x\n",
    "test_patch": "diff --git a/test b/test\n",
    "problem_statement": "Fix the thing.",
    "requirements": "Do X.",
    "interface": "None",
}


def test_as_list_variants():
    assert _as_list(None) == []
    assert _as_list("['a','b']") == ["a", "b"]
    assert _as_list('["x"]') == ["x"]
    assert _as_list(["a", "b"]) == ["a", "b"]
    assert _as_list("not a list") == ["not a list"]
    assert _as_list("") == []


def test_image_tag():
    inst = Instance(raw=ROW)
    assert inst.image == "jefzda/sweap-images:internetarchive.openlibrary-...-vDEADBEEF"


def test_fix_commit_recovered_from_instance_id():
    inst = Instance(raw=ROW)
    # the 40-hex sha that is NOT the base commit
    assert inst.fix_commit == "4a5d2a7d24c9e4c11d3069220c0685b736d5ecde"


def test_reset_commands_strip_hidden_checkout():
    inst = Instance(raw=ROW)
    cmds = inst.reset_commands()
    # the final "git checkout <fix> -- <tests>" line must be removed
    assert all("4a5d2a7d24c9e4c11d3069220c0685b736d5ecde -- " not in c for c in cmds)
    assert any("reset --hard" in c for c in cmds)


def test_grading_lists_parsed():
    inst = Instance(raw=ROW)
    assert inst.fail_to_pass == [
        "openlibrary/tests/core/test_wikidata.py::test_get_statement_values"
    ]
    assert inst.pass_to_pass == ["t::a", "t::b"]
    assert inst.selected_test_files == ["openlibrary/tests/core/test_wikidata.py"]


def test_description_includes_sections():
    from task_bundle.swebench_pro import build_description

    desc = build_description(Instance(raw=ROW))
    assert "## Problem Statement" in desc
    assert "Fix the thing." in desc
    assert "## Requirements" in desc
    # interface == "None" should be omitted
    assert "## Interface" not in desc
