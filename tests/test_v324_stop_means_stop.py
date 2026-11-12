"""
v3.2.4 (HanginIs fork): a stopped mission starts nothing further, is not completed and is not applied.

Reproduced on this fork on 2026-11-13 before the fix: the operator stopped a mission while its Builder ran; the
Builder was retried, the Reviewer and HEAD ran, the run reached PASS, the mission was reported "미션 완료" and its
three files were applied to the project. The defect and the approach to the fix come from
digilogd-cmd/ai-dev-director PR #1 (edge0303), found there with real CLI runs.
"""
import threading

import pytest

from core import process_runner
from core.mission import MissionRunner, MissionStore, stop_request
from core.process_runner import OperationStopped, ProcessTreeRunner, check_stop, set_stop_guard
from core.state_machine import State, StateMachine
from schemas.decision import DecisionSchema
from schemas.order import OrderSchema
from schemas.review_result import ReviewResultSchema
from tests.test_v306_head_proxy_autonomy import ORDER, good_build, proxy_orch, quiet

pytestmark = pytest.mark.head_proxy


@pytest.fixture(autouse=True)
def no_guard_left_behind():
    yield
    set_stop_guard(None)


def stopped_mission(tmp_path, monkeypatch, stop_during):
    """Run a one-step mission whose operator presses stop during `stop_during` ('build' or 'review')."""
    store = MissionStore(tmp_path / "missions")
    log, ident, swaps, blamed = [], {}, [], []

    def stop_now():
        stop_request(ident["id"], reason="test", store=store)

    def factory():
        orch = proxy_orch()
        quiet(monkeypatch, orch)
        good = good_build([])

        def build(order, worktree_path, builder_fn=None):
            log.append("build")
            if stop_during == "build":
                stop_now()
                raise RuntimeError("builder process killed")
            return good(order, worktree_path)
        monkeypatch.setattr(orch.builder, "run", build)
        monkeypatch.setattr(orch, "_swap_agent", lambda *a: swaps.append(a[0]) or False)
        monkeypatch.setattr(orch.pm, "parse_cli_error", lambda *a: blamed.append(a))

        def review(rid, o, v, *a):
            log.append("review")
            if stop_during == "review":
                stop_now()
            r = ReviewResultSchema(verdict="pass", summary="review")
            orch._save_json(rid, "review.json", r)
            return r, None
        monkeypatch.setattr(orch, "run_reviewer_with_failover", review)
        monkeypatch.setattr(orch, "run_decision_with_failover", lambda rid, o, b, v, r, *a: (
            log.append("decide") or DecisionSchema(decision="PASS", rationale="evidence", next_steps=[]), None))
        monkeypatch.setattr(orch.head, "plan_next",
                            lambda *a, **k: log.append("plan") or {"status": "DONE", "summary": "done"}, raising=False)
        return orch

    said = []
    runner = MissionRunner(factory, store, lambda text, run_id=None, role="SYSTEM": said.append(text))
    project = tmp_path / "proj"
    mission = runner.create(str(project), "double", OrderSchema(**ORDER), config=factory().config, auto_apply="on_pass")
    ident["id"] = mission["mission_id"]
    final = runner.run(mission["mission_id"])
    return final, log, said, project, swaps, blamed


def test_stop_during_the_builder_starts_nothing_further_and_applies_nothing(tmp_path, monkeypatch):
    final, log, said, project, swaps, blamed = stopped_mission(tmp_path, monkeypatch, "build")
    assert log == ["build"]                                       # no retry, no review, no decision, no planning
    assert final["status"] == "STOPPED" and final.get("applied") is None
    assert not (project / "feature.py").exists()                  # nothing reached the project
    assert not any("미션 완료" in text for text in said)
    state = StateMachine().get_run(final["steps"][0]["run_id"])["current_state"]
    assert state == State.FAIL
    assert swaps == [] and blamed == []                           # the killed Builder is not a provider failure


def test_stop_during_the_review_does_not_let_the_run_pass(tmp_path, monkeypatch):
    final, log, said, project, swaps, blamed = stopped_mission(tmp_path, monkeypatch, "review")
    assert "plan" not in log
    assert final["status"] == "STOPPED" and final.get("applied") is None
    assert not (project / "feature.py").exists()
    assert StateMachine().get_run(final["steps"][0]["run_id"])["current_state"] != State.PASS


def test_stopped_status_survives_a_save_of_a_finished_copy(tmp_path):
    store = MissionStore(tmp_path / "missions")
    mission = {"mission_id": "MSN-STOPSAVE", "status": "RUNNING", "steps": []}
    store.save(mission)
    stop_request("MSN-STOPSAVE", store=store)
    for status in ("RUNNING", "DONE", "INCOMPLETE", "FAILED"):
        store.save(dict(mission, status=status))
        assert store.load("MSN-STOPSAVE")["status"] == "STOPPED"
    store.save(dict(mission, status="RUNNING"), force=True)       # reopen() is the only way back
    assert store.load("MSN-STOPSAVE")["status"] == "RUNNING"


def test_no_cli_is_started_and_no_run_advances_once_the_guard_reports_a_stop(tmp_path):
    sm = StateMachine()
    sm.create_run("RUN-TEST-STOPGUARD", str(tmp_path), "g")
    set_stop_guard(lambda: "operator")
    with pytest.raises(OperationStopped):
        ProcessTreeRunner.run(argv=["definitely-not-a-cli"], cwd=str(tmp_path))
    with pytest.raises(OperationStopped):
        sm.transition_to("RUN-TEST-STOPGUARD", State.PLANNING)
    sm.transition_to("RUN-TEST-STOPGUARD", State.FAIL, details="stopped")      # ending is still allowed
    assert sm.get_run("RUN-TEST-STOPGUARD")["current_state"] == State.FAIL


def test_guard_belongs_to_one_thread_only():
    set_stop_guard(lambda: "operator")
    seen = []
    worker = threading.Thread(target=lambda: seen.append(check_stop()))
    worker.start()
    worker.join()
    assert seen == [None]                                          # another mission's thread is not stopped
    with pytest.raises(OperationStopped):
        check_stop()
    set_stop_guard(None)
    check_stop()


def test_scoped_stop_terminates_owned_child_when_process_table_is_unavailable(monkeypatch):
    from unittest.mock import Mock

    research_child = Mock()
    research_child.poll.return_value = None
    other_child = Mock()
    monkeypatch.setattr(process_runner, "_ACTIVE", {101: "claude", 102: "codex"})
    monkeypatch.setattr(process_runner, "_OWNER", {101: "TRIAD-test", 102: "MSN-test"})
    monkeypatch.setattr(process_runner, "_HANDLES", {101: research_child, 102: other_child})

    def process_table_denied(pid):
        assert pid == 101
        raise PermissionError("process table access denied")

    monkeypatch.setattr(ProcessTreeRunner, "kill_process_tree", process_table_denied)
    assert ProcessTreeRunner.kill_active("TRIAD-test") == [101]
    research_child.kill.assert_called_once_with()
    other_child.kill.assert_not_called()
    assert process_runner.active_processes() == {102: "codex"}


def test_mission_thread_leaves_no_guard_behind(tmp_path, monkeypatch):
    stopped_mission(tmp_path, monkeypatch, "build")
    assert getattr(process_runner._STOP, "guard", None) is None


# ------------------------------------------------------------------ test runs stay out of the source tree

def test_suite_writes_run_evidence_outside_the_source_tree(tmp_path):
    from core.db_path import SOURCE_ROOT, get_runs_dir
    from core.result_manager import ResultManager
    from tests.test_v0234_resume_parity import _mock_orch
    runs = get_runs_dir()
    assert SOURCE_ROOT not in runs.parents and runs != SOURCE_ROOT / "runs"
    orch = _mock_orch()
    assert orch._get_run_dir("RUN-TEST-RUNSDIR").parent == runs
    assert ResultManager.run_dir("RUN-TEST-RUNSDIR").parent == runs
    assert not (SOURCE_ROOT / "runs" / "RUN-TEST-RUNSDIR").exists()


def test_runs_folder_override_only_applies_in_test_mode(monkeypatch, tmp_path):
    from core.db_path import SOURCE_ROOT, get_runs_dir
    monkeypatch.setenv("TEST_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("DIRECTOR_TESTING", "0")
    assert get_runs_dir() == SOURCE_ROOT / "runs"


def test_mission_saves_do_not_share_one_temp_file(tmp_path, monkeypatch):
    import os as _os
    store = MissionStore(tmp_path / "missions")
    used = []
    real = _os.replace
    monkeypatch.setattr("core.mission.os.replace", lambda src, dst: used.append(str(src)) or real(src, dst))
    for _ in range(3):
        store.save({"mission_id": "MSN-TMP", "status": "RUNNING", "steps": []})
    assert len(set(used)) == 3 and store.load("MSN-TMP")["status"] == "RUNNING"
    assert [p.name for p in (tmp_path / "missions").iterdir()] == ["MSN-TMP.json"]
