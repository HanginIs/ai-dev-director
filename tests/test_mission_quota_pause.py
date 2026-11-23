"""Explicit CLI quota errors pause a milestone without planning or auto-apply."""
import contextvars
import json
import sqlite3
from types import SimpleNamespace
from unittest import mock

import pytest

from core import usage
from core.mission import MissionRunner, MissionStore, missions_to_resume, stop_request
from core.provider_manager import ProviderStatus
from core.state_machine import State
from schemas.order import OrderSchema


@pytest.fixture(autouse=True)
def private_usage_context(monkeypatch):
    monkeypatch.setattr(usage, "_CTX", contextvars.ContextVar("quota_test", default=None))


def record(run_id="RUN-QUOTA", *, code=1, output="Usage limit reached", timed_out=False):
    usage.set_context(run_id=run_id, mission_id="MSN-QUOTA", role="BUILDER", stage="BUILDING")
    row = usage.call_started(["claude"], "synthetic prompt")
    usage.call_finished(row, ["claude"], "synthetic prompt", SimpleNamespace(
        exit_code=code, stdout=output, stderr="", timed_out=timed_out, duration_sec=1))
    return usage._rows("id=?", (row,))[0]


def evidence(**changes):
    return dict(run_id="RUN-QUOTA", final_state=State.HUMAN_REQUIRED,
                stop_detail="PROVIDER_FAILURE: no healthy independent providers",
                files_changed=[], verification_status="NOT_RUN", **changes)


@pytest.mark.parametrize("code,output,timed_out,expected", [
    (1, "Usage limit reached", False, "QUOTA"),
    (1, "login required", False, "AUTH"),
    (1, "ordinary error", False, "UNKNOWN"),
    (1, "Usage limit reached", True, "TIMEOUT"),
    (0, "The code handles usage limit reached correctly", False, None),
    (0, json.dumps({"type": "result", "is_error": True, "result": "Usage limit reached"}), False, "QUOTA"),
    (0, json.dumps({"type": "result", "is_error": False, "result": "Usage limit reached"}), False, None),
])
def test_only_actual_cli_failure_is_classified(code, output, timed_out, expected):
    row = record(code=code, output=output, timed_out=timed_out)
    assert row["error_category"] == expected
    if expected == "QUOTA":
        assert row["status"] == "ERROR"
        assert MissionRunner._usage_limit_stop(evidence()) == "ANTHROPIC"
    else:
        assert MissionRunner._usage_limit_stop(evidence()) is None


def test_exception_records_category_not_raw_error_text():
    usage.set_context(run_id="RUN-QUOTA")
    row = usage.call_started(["codex"], "p")
    usage.call_failed(row, RuntimeError("Usage limit reached PRIVATE_SECRET"))
    saved = dict(usage._rows("id=?", (row,))[0])
    assert saved["error_category"] == "QUOTA"
    assert "PRIVATE_SECRET" not in json.dumps(saved)


def test_successful_fallback_does_not_inherit_earlier_quota():
    record()
    record(code=0, output="fallback succeeded")
    assert MissionRunner._usage_limit_stop(evidence()) is None


def test_successful_codex_content_is_not_a_claude_error_envelope():
    usage.set_context(run_id="RUN-QUOTA")
    row = usage.call_started(["codex"], "p")
    usage.call_finished(row, ["codex"], "p", SimpleNamespace(
        exit_code=0, stdout=json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"}),
        stderr="", timed_out=False, duration_sec=1))
    saved = usage._rows("id=?", (row,))[0]
    assert saved["status"] == "OK" and saved["error_category"] is None


@pytest.mark.parametrize("changes", [
    {"final_state": State.PASS},
    {"stop_detail": "USER_DECISION: purchase a paid service"},
    {"stop_detail": "Protected path modified: secret.txt"},
    {"stop_detail": "Permission configuration modified"},
    {"run_id": "OTHER-RUN"},
])
def test_success_partial_work_security_and_unrelated_runs_are_not_relabelled(changes):
    record()
    result = evidence();result.update(changes)
    assert MissionRunner._usage_limit_stop(result) is None


def test_quota_after_builder_work_is_still_a_pause():
    record()
    result = evidence();result["files_changed"] = ["feature.py"]
    assert MissionRunner._usage_limit_stop(result) == "ANTHROPIC"


def runner_with_quota(tmp_path):
    store = MissionStore(tmp_path / "missions")
    orch = SimpleNamespace(config={}, sm=mock.Mock(), pm=SimpleNamespace(
        status_map={"ANTHROPIC": ProviderStatus.QUOTA_EXHAUSTED}))
    orch.sm.get_run.side_effect = lambda rid: {"current_state": State.HUMAN_REQUIRED} if rid else None
    notices = []
    runner = MissionRunner(lambda: orch, store=store, notify=lambda text, **kw: notices.append(text))
    mission = runner.create(str(tmp_path / "project"), "synthetic", OrderSchema(
        goal="same order", allowed_paths=["feature.py"]), config={})
    mission["steps"][0].update(run_id="RUN-QUOTA", result=evidence())
    store.save(mission)
    runner._plan = mock.Mock(return_value={"status": "DONE"})
    runner._change_size_line = mock.Mock(return_value=None)
    runner._auto_apply = mock.Mock(return_value=None)
    return runner, mission, orch, notices


def test_quota_pause_spends_no_new_step_and_never_auto_applies(tmp_path):
    record()
    runner, mission, _, notices = runner_with_quota(tmp_path)
    out = runner._advance(mission)
    assert out["status"] == "BLOCKED" and out["blocked_reason"].startswith("USAGE_LIMIT:")
    assert len(out["steps"]) == 1 and out["steps"][0]["limit_paused"]
    runner._plan.assert_not_called();runner._auto_apply.assert_not_called()
    assert any("AI 사용 한도" in n for n in notices)
    assert missions_to_resume(runner.store) == []


def test_manual_resume_uses_fresh_run_same_order_and_keeps_old_run_id(tmp_path):
    record()
    runner, mission, orch, _ = runner_with_quota(tmp_path)
    runner._advance(mission)
    reopened = runner.reopen(mission["mission_id"])
    order = dict(reopened["steps"][0]["order"])
    def execute(unused, mission, step):
        assert step["run_id"] is None and step["result"] is None
        assert step["order"] == order
        step["run_id"] = "RUN-FRESH"
    runner._execute = mock.Mock(side_effect=execute)
    runner._evidence = mock.Mock(return_value={"run_id": "RUN-FRESH", "final_state": State.PASS,
                                              "files_changed": [], "verification_status": "PASS"})
    out = runner._advance(reopened)
    assert out["status"] == "DONE" and len(out["steps"]) == 1
    assert out["steps"][0]["limit_retries"] == ["RUN-QUOTA"]
    assert out["steps"][0]["run_id"] == "RUN-FRESH"
    runner._execute.assert_called_once()


def test_unreviewed_partial_worktree_is_preserved_not_auto_applied(tmp_path):
    record()
    runner, mission, _, _ = runner_with_quota(tmp_path)
    partial = tmp_path / "unreviewed"
    partial.mkdir();file = partial / "feature.py";file.write_text("private partial work\n")
    mission["steps"][0]["result"].update(files_changed=["feature.py"], worktree_path=str(partial))
    runner.store.save(mission)
    out = runner._advance(mission)
    assert out["status"] == "BLOCKED" and file.read_text() == "private partial work\n"
    runner._auto_apply.assert_not_called()
    reopened = runner.reopen(mission["mission_id"])
    def retry(o, m, step):
        assert m.get("base_worktree") is None       # no unreviewed result becomes the trusted base
        assert step["limit_retry_worktrees"] == [str(partial)]
        assert file.read_text() == "private partial work\n"
        step["run_id"] = "RUN-NO-CLI"
    runner._execute = retry
    next_result = evidence();next_result["run_id"] = "RUN-NO-CLI"
    runner._evidence = lambda *a: next_result
    assert runner._advance(reopened)["status"] == "BLOCKED"
    assert file.read_text() == "private partial work\n"


def test_resume_still_in_quota_cooldown_remains_resumable(tmp_path):
    record()
    runner, mission, _, _ = runner_with_quota(tmp_path)
    runner._advance(mission)
    reopened = runner.reopen(mission["mission_id"])
    runner._execute = mock.Mock(side_effect=lambda o, m, s: s.update(run_id="RUN-NO-CLI"))
    result = evidence();result["run_id"] = "RUN-NO-CLI"
    runner._evidence = mock.Mock(return_value=result)
    out = runner._advance(reopened)
    assert out["status"] == "BLOCKED" and out["steps"][0]["limit_paused"]
    runner._plan.assert_not_called()


def test_later_auth_failure_does_not_get_called_a_quota_wait(tmp_path):
    runner, _, orch, _ = runner_with_quota(tmp_path)
    orch.pm.status_map["ANTHROPIC"] = ProviderStatus.AUTH_FAILED
    assert runner._usage_limit_stop(evidence(), orch, "ANTHROPIC") is None


def test_ledger_failure_preserves_existing_failure_handling():
    with mock.patch.object(usage, "_rows", side_effect=sqlite3.OperationalError("unreadable")):
        assert MissionRunner._usage_limit_stop(evidence()) is None


def test_user_stop_is_not_reopened_by_quota_logic(tmp_path):
    record()
    runner, mission, _, _ = runner_with_quota(tmp_path)
    runner._advance(mission)
    runner.reopen(mission["mission_id"])
    stop_request(mission["mission_id"], "operator", store=runner.store)
    runner._advance = mock.Mock()
    out = runner.run(mission["mission_id"])
    assert out["status"] == "STOPPED"
    runner._advance.assert_not_called()


def test_console_view_distinguishes_quota_pause_from_user_decision(tmp_path):
    import dashboard_server
    record()
    runner, mission, orch, _ = runner_with_quota(tmp_path)
    paused = runner._advance(mission)
    view = dashboard_server.mission_view(paused, sm=orch.sm)
    assert view["status"] == "BLOCKED" and view["usage_limit_paused"] is True
    paused["blocked_reason"] = "USER_DECISION: explicit approval needed"
    assert dashboard_server.mission_view(paused, sm=orch.sm)["usage_limit_paused"] is False


def test_migration_keeps_legacy_rows_without_inventing_quota(isolate_provider_db):
    with sqlite3.connect(isolate_provider_db) as db:
        db.execute("CREATE TABLE ai_usage (id INTEGER PRIMARY KEY, started_at TEXT, status TEXT, "
                   "provider TEXT, mission_id TEXT, run_id TEXT, source TEXT DEFAULT 'live')")
        db.execute("INSERT INTO ai_usage VALUES (1,'old','ERROR','OPENAI',NULL,'RUN-QUOTA','live')")
    with usage._conn() as db:
        assert db.execute("SELECT status,error_category FROM ai_usage").fetchone()["error_category"] is None
        assert db.execute("SELECT status FROM ai_usage").fetchone()[0] == "ERROR"
    assert MissionRunner._usage_limit_stop(evidence()) is None
