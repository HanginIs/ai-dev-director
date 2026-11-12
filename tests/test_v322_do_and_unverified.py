"""
v3.2.2 (HanginIs fork), found by running `director.py do ... --no-apply` on a real project (2026-11-12):

- the console server refused the request (HTTP 400) and `do` ran the mission in the terminal anyway, without
  --no-apply and without saying why the server had refused;
- the project had no verification command, the Reviewer rejected both milestones for missing test evidence, and
  nothing told the operator that no ORDER could have fixed that;
- the first ORDER of a mission is written by HEAD's chat prompt, which did not get the project's history.
"""
import io
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from core.db_path import get_runs_dir

import pytest
from click.testing import CliRunner

import director
from agents.claude_head import ClaudeHead
from agents.codex_head import CodexHead
from core import mission as mission_module
from core import project_history
from core.mission import NO_VERIFICATION_HINT, MissionRunner, MissionStore
from core.state_machine import State
from tests.test_v0234_resume_parity import _mock_orch
from tests.test_v301_companion_http import http_server, request  # noqa: F401

RULE = "Interrupted sends are never re-sent automatically."
CHANGELOG = "# Changelog\n\n## 1.9.8\n- " + RULE + "\n"
ORDER = {"goal": "g", "allowed_paths": ["src/**"], "protected_paths": [], "acceptance_criteria": ["ok"]}


# ------------------------------------------------------------------ director.py do

@pytest.fixture
def do_env(monkeypatch):
    """`do` with the network and the in-terminal mission replaced by recorders."""
    seen = {"requests": [], "missions": []}
    monkeypatch.setattr(director, "_bridge_token", lambda: "test-token")
    monkeypatch.setattr(director.mission, "callback", lambda **kw: seen["missions"].append(kw))
    return seen


def http_error(code, payload):
    return urllib.error.HTTPError("http://127.0.0.1:8081/bridge/missions", code, "Bad Request", {},
                                  io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")))


def test_do_stops_and_says_why_when_the_server_refuses(do_env, monkeypatch, tmp_path):
    def refuse(req, timeout=None):
        do_env["requests"].append(json.loads(req.data.decode("utf-8")))
        raise http_error(400, {"error": "솔로몬에 허용된 폴더 밖입니다"})
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    result = CliRunner().invoke(director.cli, ["do", "fix the login bug", "-p", str(tmp_path), "--no-apply"])
    assert result.exit_code == 1
    assert "HTTP 400" in result.output and "허용된 폴더 밖" in result.output and "solomon.allowed_roots" in result.output
    assert do_env["missions"] == []                               # the refusal is not worked around in the terminal
    assert do_env["requests"][0]["source"] == "cli" and do_env["requests"][0]["auto_apply"] == "off"


def test_do_does_not_start_a_second_mission_on_a_busy_project(do_env, monkeypatch, tmp_path):
    def busy(req, timeout=None):
        raise http_error(409, {"status": "busy", "error": "다른 작업이 진행 중입니다"})
    monkeypatch.setattr(urllib.request, "urlopen", busy)
    result = CliRunner().invoke(director.cli, ["do", "fix the login bug", "-p", str(tmp_path)])
    assert result.exit_code == 1 and "HTTP 409" in result.output and "다른 작업이 진행 중" in result.output
    assert do_env["missions"] == []


def test_do_runs_in_the_terminal_only_when_no_server_answers_and_keeps_no_apply(do_env, monkeypatch, tmp_path):
    def down(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", down)
    result = CliRunner().invoke(director.cli, ["do", "fix the login bug", "-p", str(tmp_path), "--profile", "quick",
                                               "--no-apply"])
    assert result.exit_code == 0 and "연결할 수 없어" in result.output
    assert len(do_env["missions"]) == 1
    started = do_env["missions"][0]
    assert started["no_apply"] is True and started["profile"] == "quick"
    assert started["project"] == str(Path(tmp_path).resolve()) and started["goal"] == "fix the login bug"


def test_do_without_no_apply_keeps_the_configured_policy_in_the_terminal(do_env, monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("no server")))
    CliRunner().invoke(director.cli, ["do", "fix the login bug", "-p", str(tmp_path)])
    assert do_env["missions"][0]["no_apply"] is False


class FakeHead:
    provider = "mock"

    def __init__(self):
        self.contexts = []

    def chat(self, message, project_context, history):
        self.contexts.append(dict(project_context))
        return {"type": "DEV_TASK", "message": "ok", "order": dict(ORDER)}


@pytest.fixture
def mission_env(monkeypatch, tmp_path):
    """The `mission` command with HEAD, the entry gate and the runner replaced by recorders."""
    seen = {"created": [], "head": FakeHead()}

    class Orch:
        config = {}

        def __init__(self, config=None):
            self.head = seen["head"]

        def _read_project_context(self, where):
            return {"project_path": where, "existing_files": []}
    monkeypatch.setattr(director, "Orchestrator", Orch)
    monkeypatch.setattr("core.bootstrap.prepare_project", lambda path: path)
    monkeypatch.setattr("core.mission.latest_deliverable", lambda project, store=None: None)
    monkeypatch.setattr("core.autopilot.entry_gate",
                        lambda order, project, head, cwd=None: {"order": order, "notes": [], "escalate": None})
    monkeypatch.setattr("dashboard_server.active_session_id", lambda: None)

    def create(self, project, goal, order, **kw):
        seen["created"].append(kw)
        return {"mission_id": "MSN-T"}
    monkeypatch.setattr(MissionRunner, "create", create)
    monkeypatch.setattr(MissionRunner, "run", lambda self, mission_id: {"status": "DONE"})
    return seen


def test_mission_command_no_apply_reaches_the_mission(mission_env, tmp_path):
    result = CliRunner().invoke(director.cli, ["mission", "-p", str(tmp_path), "-g", "fix the login bug", "--no-apply"])
    assert result.exit_code == 0, result.output
    assert mission_env["created"][0]["auto_apply"] == "off" and mission_env["created"][0]["source"] == "cli"


def test_mission_command_default_leaves_the_policy_to_the_config(mission_env, tmp_path):
    CliRunner().invoke(director.cli, ["mission", "-p", str(tmp_path), "-g", "fix the login bug"])
    assert mission_env["created"][0]["auto_apply"] is None


def test_mission_command_gives_head_the_project_history_for_the_first_order(mission_env, tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    CliRunner().invoke(director.cli, ["mission", "-p", str(tmp_path), "-g", "fix the login bug"])
    assert RULE in mission_env["head"].contexts[0]["decision_history"]["text"]


def test_server_records_a_do_mission_as_cli_not_as_solomon(http_server, monkeypatch, tmp_path):
    import companion_server
    monkeypatch.setattr(companion_server.d, "load_config",
                        lambda: {"solomon": {"allowed_roots": [str(tmp_path)], "denied": []}, "projects": {}})
    target = tmp_path / "app"
    target.mkdir()
    calls = []
    monkeypatch.setattr(companion_server.d, "start_goal_mission",
                        lambda path, goal, **kw: calls.append(kw) or {"status": "started", "mission_id": "MSN-C"})
    headers = {"X-Solomon-Bridge": "test-token", "Content-Type": "application/json"}
    for ident, source in (("cli-0123456789abcdef", "cli"), ("solomon-req-9001", None), ("other-req-000001", "admin")):
        body = {"request_id": ident, "project_path": str(target), "goal": "make a thing", "auto_apply": "off"}
        if source:
            body["source"] = source
        assert request(http_server, "POST", "/bridge/missions", body=json.dumps(body), headers=headers)[0] == 202
    assert [kw["source"] for kw in calls] == ["cli", "solomon", "solomon"]
    assert calls[0]["auto_apply"] == "off"


# ------------------------------------------------------------------ the first ORDER sees the project's history

def test_attach_adds_the_history_only_when_the_project_has_one(tmp_path):
    assert project_history.attach({"a": 1}, str(tmp_path)) == {"a": 1}
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    context = project_history.attach({"a": 1}, str(tmp_path))
    assert context["a"] == 1 and RULE in context["decision_history"]["text"]
    assert project_history.attach({}, None) == {}


@pytest.mark.parametrize("head_class", [ClaudeHead, CodexHead])
def test_chat_prompt_quotes_the_history_outside_the_project_json(monkeypatch, head_class):
    head, prompts = head_class(), []

    def invoke(prompt, cwd=None):
        prompts.append(prompt)
        return '{"type": "QUESTION", "message": "answer"}'
    monkeypatch.setattr(head, "_invoke_cli", invoke)
    context = {"project_path": None, "existing_files": ["a.py"],
               "decision_history": {"file": "CHANGELOG.md", "text": CHANGELOG.strip(), "truncated": False}}
    assert head.chat("중단된 전송을 정리해줘", context, [])["type"] == "QUESTION"
    assert head.chat("중단된 전송을 정리해줘", {"project_path": None, "existing_files": []}, [])["type"] == "QUESTION"
    with_history, without = prompts
    assert "PROJECT DECISION HISTORY (the project's own CHANGELOG.md" in with_history and RULE in with_history
    assert with_history.index(RULE) < with_history.index("PROJECT CONTEXT:")
    assert '"decision_history"' not in with_history and "decision_history" in context
    assert "PROJECT DECISION HISTORY" not in without


# ------------------------------------------------------------------ nothing verified the work: say so

def step(index, state, verification_status):
    return {"index": index, "order": dict(ORDER), "run_id": f"RUN-U{index}",
            "result": {"final_state": state, "verification_status": verification_status, "worktree_path": None}}


def runner_with_log(tmp_path, factory=lambda: None):
    said = []
    runner = MissionRunner(factory, store=MissionStore(tmp_path / "missions"),
                           notify=lambda text, run_id=None, role="SYSTEM": said.append(text))
    return runner, said


def mission_of(tmp_path, steps):
    return {"mission_id": "MSN-U", "goal": "g", "project_path": str(tmp_path), "base_worktree": None, "summary": "",
            "status": "RUNNING", "auto_apply": "off", "max_steps": 2, "max_consecutive_failures": 3, "steps": steps}


def test_final_message_names_the_missing_verification_command(tmp_path):
    runner, said = runner_with_log(tmp_path)
    runner._finish(mission_of(tmp_path, [step(1, State.FAIL, "NOT_RUN"), step(2, State.FAIL, "NOT_RUN")]),
                   "INCOMPLETE", summary="s", blocked="step budget (2) used")
    assert NO_VERIFICATION_HINT in said[-1] and "test_command" in said[-1]


def test_final_message_has_no_such_line_when_tests_ran(tmp_path):
    runner, said = runner_with_log(tmp_path)
    runner._finish(mission_of(tmp_path, [step(1, State.FAIL, "FAIL"), step(2, State.FAIL, "PASS")]),
                   "INCOMPLETE", summary="s", blocked="step budget (2) used")
    assert "검증 안 함" not in said[-1]


def test_operator_is_told_once_as_soon_as_an_unverified_step_fails(tmp_path, monkeypatch):
    class Sm:
        def get_run(self, run_id):
            return None

    class Orch:
        config, sm = {}, Sm()
    runner, said = runner_with_log(tmp_path, factory=lambda: Orch())
    mission = mission_of(tmp_path, [{"index": 1, "order": dict(ORDER), "run_id": "RUN-U1", "result": None}])
    monkeypatch.setattr(runner, "_execute", lambda orch, mission, step: None)
    monkeypatch.setattr(runner, "_evidence", lambda orch, run_id: {
        "run_id": run_id, "final_state": State.FAIL, "verification_status": "NOT_RUN", "worktree_path": None,
        "stop_detail": "review rejected"})
    monkeypatch.setattr(runner, "_plan", lambda orch, mission: {
        "status": "CONTINUE", "summary": "next", "order": dict(ORDER)})
    monkeypatch.setattr(mission_module.autopilot, "entry_gate",
                        lambda order, project, head, cwd=None: {"order": order, "notes": [], "escalate": None})
    Orch.head = None
    mission = runner._advance(mission)
    notices = [text for text in said if NO_VERIFICATION_HINT in text]
    assert len(notices) == 1 and notices[0].startswith("[미션 1/2]")
    assert mission["no_verification_notified"] is True and len(mission["steps"]) == 2
    # the second unverified failure does not repeat the notice; the final message carries it instead
    mission["steps"][-1]["run_id"] = "RUN-U2"
    runner._advance(mission)
    assert len([text for text in said if text.startswith("[미션") and NO_VERIFICATION_HINT in text]) == 1
    assert NO_VERIFICATION_HINT in said[-1]


def test_unverified_step_that_passed_is_not_reported_as_a_problem(tmp_path, monkeypatch):
    class Sm:
        def get_run(self, run_id):
            return None

    class Orch:
        config, sm, head = {}, Sm(), None
    runner, said = runner_with_log(tmp_path, factory=lambda: Orch())
    mission = mission_of(tmp_path, [{"index": 1, "order": dict(ORDER), "run_id": "RUN-U1", "result": None}])
    monkeypatch.setattr(runner, "_execute", lambda orch, mission, step: None)
    monkeypatch.setattr(runner, "_evidence", lambda orch, run_id: {
        "run_id": run_id, "final_state": State.PASS, "verification_status": "NOT_RUN", "worktree_path": None})
    monkeypatch.setattr(runner, "_plan", lambda orch, mission: {"status": "DONE", "summary": "done"})
    monkeypatch.setattr(runner, "_change_size_line", lambda mission: None)
    final = runner._advance(mission)
    assert final["status"] == "DONE" and not any("검증 안 함" in text for text in said)


def test_step_evidence_carries_the_verification_status(tmp_path):
    orch = _mock_orch()
    orch.sm.create_run("RUN-TEST-EVIDENCE-NOTRUN", str(tmp_path), "g")
    try:
        orch._save_json("RUN-TEST-EVIDENCE-NOTRUN", "verification.json",
                        {"passed": True, "status": "NOT_RUN", "summary": "No build/test commands configured"})
        assert MissionRunner._evidence(orch, "RUN-TEST-EVIDENCE-NOTRUN")["verification_status"] == "NOT_RUN"
    finally:
        shutil.rmtree(get_runs_dir() / "RUN-TEST-EVIDENCE-NOTRUN", ignore_errors=True)


def test_mission_command_no_apply_also_holds_for_a_continued_mission(mission_env, monkeypatch, tmp_path):
    """From digilogd-cmd/ai-dev-director PR #1: --no-apply was ignored when a mission was resumed."""
    saved = {"mission_id": "MSN-RESUME", "status": "INCOMPLETE", "auto_apply": "on_pass", "steps": []}
    monkeypatch.setattr(MissionStore, "load", lambda self, mission_id: saved)
    monkeypatch.setattr(MissionStore, "save", lambda self, mission, force=False: saved.update(mission))
    monkeypatch.setattr(MissionRunner, "reopen", lambda self, mission_id: saved)
    result = CliRunner().invoke(director.cli, ["mission", "-p", str(tmp_path), "-g", "g", "--mission-id", "MSN-RESUME",
                                               "--no-apply"])
    assert result.exit_code == 0, result.output
    assert saved["auto_apply"] == "off" and mission_env["created"] == []
