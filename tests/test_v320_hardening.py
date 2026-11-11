"""
v3.2.0 hardening: one SQLite connection policy (WAL + wait), no nested git repositories, a stop that only kills the
mission asked for, and runs a quit app left open are closed on the next start.
"""
import os
import subprocess

import pytest

from core.state_machine import StateMachine, State


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_every_connection_uses_wal_and_waits_for_a_busy_writer(tmp_path):
    from core.db_path import connect
    db = tmp_path / "d.db"
    StateMachine(str(db))
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
    finally:
        conn.close()


def test_a_folder_inside_another_repository_never_gets_its_own(tmp_path):
    from core.bootstrap import prepare_project
    outer = tmp_path / "mono"
    (outer / "integration" / "tool").mkdir(parents=True)
    git(outer, "init", "-q")
    with pytest.raises(ValueError, match="저장소 루트"):
        prepare_project(outer / "integration" / "tool")
    assert not (outer / "integration" / "tool" / ".git").exists()
    assert prepare_project(outer) == str(outer.resolve())          # the repository root itself is fine
    fresh = tmp_path / "fresh"
    assert prepare_project(fresh) == str(fresh.resolve()) and (fresh / ".git").exists()


def test_stopping_one_mission_only_kills_the_processes_it_started(monkeypatch):
    from core import process_runner as pr
    killed = []
    monkeypatch.setattr(pr.ProcessTreeRunner, "kill_process_tree", staticmethod(lambda pid: killed.append(pid) or True))
    monkeypatch.setattr(pr, "_ACTIVE", {101: "codex", 102: "claude", 103: "agy"})
    monkeypatch.setattr(pr, "_OWNER", {101: "MSN-A", 102: "MSN-B", 103: None})
    assert pr.ProcessTreeRunner.kill_active("MSN-A") == [101]
    assert killed == [101] and set(pr._ACTIVE) == {102, 103}
    assert sorted(pr.ProcessTreeRunner.kill_active()) == [102, 103]          # no mission given: everything


def test_a_cli_started_inside_a_mission_is_tagged_with_it(monkeypatch):
    from core import process_runner as pr, usage
    usage.set_context(mission_id="MSN-TAG")
    try:
        assert pr._current_mission() == "MSN-TAG"
    finally:
        usage.set_context(mission_id=None)


def test_runs_left_open_by_a_quit_app_are_closed_on_the_next_start(tmp_path):
    from core.mission import MissionRunner, MissionStore, finalize_orphaned_runs
    from schemas.order import OrderSchema
    sm = StateMachine()
    store = MissionStore(tmp_path / "missions")
    runner = MissionRunner(lambda: None, store)

    def mission(run_id, status, owner):
        sm.create_run(run_id, str(tmp_path / "p"), "g")
        for nxt in (State.PLANNING, State.ORDER_READY, State.BUILDING):
            sm.transition_to(run_id, nxt, details="t")
        m = runner.create(str(tmp_path / "p"), "g", OrderSchema(goal="g", allowed_paths=["a.py"]), config={})
        m.update(status=status, owner_pid=owner)
        m["steps"][0]["run_id"] = run_id
        store.save(m, force=True)
        return m

    dead = 2 ** 22 + 12345                                        # no such process
    stopped = mission("RUN-ORPHAN", "STOPPED", dead)
    running = mission("RUN-STILL-RUNNING", "RUNNING", dead)        # resume-on-start owns RUNNING missions
    alive = mission("RUN-ALIVE", "STOPPED", os.getpid())           # its worker is still finishing
    assert finalize_orphaned_runs(store, sm) == ["RUN-ORPHAN"]
    assert sm.get_run("RUN-ORPHAN")["current_state"] == State.FAIL
    assert store.load(stopped["mission_id"])["owner_pid"] is None
    assert sm.get_run("RUN-STILL-RUNNING")["current_state"] == State.BUILDING
    assert sm.get_run("RUN-ALIVE")["current_state"] == State.BUILDING
    assert finalize_orphaned_runs(store, sm) == []                # idempotent


def test_cli_names_are_read_from_either_path_style():
    from core.process_runner import cli_basename, REAL_CLI_BASENAMES
    from core import usage
    for path in (r"C:\Users\x\AppData\Roaming\npm\claude.cmd", "/usr/local/bin/claude", "claude.exe", r"D:\t\CLAUDE.EXE"):
        assert cli_basename(path) in REAL_CLI_BASENAMES, path
        assert usage.provider_of(path) == "ANTHROPIC", path
    assert cli_basename("/usr/bin/python3") not in REAL_CLI_BASENAMES
    assert usage.provider_of(r"C:\x\codex.cmd") == "OPENAI" and usage.provider_of("/opt/homebrew/bin/agy") == "GOOGLE"


def test_posix_document_paths_in_an_order_are_read_and_lookalikes_are_not(tmp_path):
    from core.builder_context import referenced_documents
    from schemas.order import OrderSchema
    doc = tmp_path / "my notes" / "spec.md"
    doc.parent.mkdir()
    doc.write_text("SPEC", encoding="utf-8")
    order = OrderSchema(goal=f"Use http://127.0.0.1:1234/v1/readme.md and docs/SPEC.md and ~/x/y.md; read {doc} first.",
                        allowed_paths=["src/**"])
    assert referenced_documents(order) == {str(doc): "SPEC"}
