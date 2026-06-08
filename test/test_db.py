"""Database round-trip tests (no Docker)."""

from __future__ import annotations

from task_bundle import db


def test_command_lifecycle(tmp_path):
    dbfile = tmp_path / "db.sqlite3"
    with db.session(dbfile) as conn:
        cmd = db.start_command(conn, command="run", args={"solver": "golden"}, bundle="b")
        assert db.get_command(conn, cmd.cid)["status"] == "running"
        cmd.set_image("img:tag", "sha256:abc")
        cmd.finish(status="ok", exit_code=0, message="done")
        row = db.get_command(conn, cmd.cid)
        assert row["status"] == "ok"
        assert row["exit_code"] == 0
        assert row["image_digest"] == "sha256:abc"
        assert row["duration_ms"] is not None


def test_run_and_test_results(tmp_path):
    dbfile = tmp_path / "db.sqlite3"
    with db.session(dbfile) as conn:
        cmd = db.start_command(conn, command="run", args={}, bundle="b")
        rid = db.record_run(
            conn,
            cid=cmd.cid,
            bundle="b",
            instance_id="i",
            solver="golden",
            model=None,
            resolved=True,
            f2p_total=1,
            f2p_passed=1,
            p2p_total=2,
            p2p_passed=2,
            artifact_path="/tmp/r.json",
            duration_ms=10,
        )
        db.record_test_results(
            conn,
            run_id=rid,
            phase="post_solver",
            rows=[("fail_to_pass", "t::a", "passed"), ("pass_to_pass", "t::b", "passed")],
        )
        run = db.get_run(conn, rid)
        assert run["resolved"] == 1
        assert run["solver"] == "golden"

        results = db.get_test_results(conn, rid)
        assert len(results) == 2
        statuses = {r["test_id"]: r["status"] for r in results}
        assert statuses["t::a"] == "passed"

        # linkage: the run is discoverable from its command
        runs = db.get_runs_for_command(conn, cmd.cid)
        assert [r["run_id"] for r in runs] == [rid]


def test_listing(tmp_path):
    dbfile = tmp_path / "db.sqlite3"
    with db.session(dbfile) as conn:
        for _ in range(3):
            db.start_command(conn, command="init", args={})
        assert len(db.list_commands(conn)) == 3
        assert db.get_run(conn, "nope") is None
        assert db.get_command(conn, "nope") is None
