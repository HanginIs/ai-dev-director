"""
v3.2.1 (HanginIs fork): the project's own change history reaches HEAD's step planning and the Reviewer, the mission's
total change size is reported in its final message, and a Builder that timed out is not asked again.

HEAD and the Reviewer do not remember earlier missions. A plan that reverses a rule the project adopted on purpose
looks like any other change unless they are shown that the rule exists.
"""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from core.db_path import get_runs_dir

import pytest

from agents.claude_reviewer import ClaudeReviewer
from agents.codex_reviewer import CodexReviewer
from agents.head import BaseHead
from core import project_history
from core.mission import MissionRunner, MissionStore
from core.process_runner import ProcessResult, ProcessTreeRunner
from core.result_manager import ResultManager
from core.state_machine import State, StateMachine
from schemas.order import OrderSchema
from tests.test_v0234_resume_parity import _mock_orch

RULE = "Interrupted sends are never re-sent automatically."
CHANGELOG = "# Changelog\n\n## 1.9.8\n- " + RULE + "\n\n## 1.9.7\n- Older entry.\n"


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


# ------------------------------------------------------------------ finding and reading the history file

def test_history_is_read_from_the_project_root(tmp_path):
    write(tmp_path, "CHANGELOG.md", CHANGELOG)
    found = project_history.read(str(tmp_path))
    assert found["file"] == "CHANGELOG.md" and RULE in found["text"] and found["truncated"] is False


def test_project_without_a_history_file_gets_nothing(tmp_path):
    write(tmp_path, "README.md", "# App\n")
    assert project_history.read(str(tmp_path)) is None
    assert project_history.planning_block(None) == "" and project_history.review_block(None) == ""


def test_long_history_keeps_only_the_newest_part_and_cuts_at_a_line(tmp_path):
    entries = "".join(f"## 1.0.{n}\n- entry number {n}\n\n" for n in range(400, 0, -1))
    original = "# Changelog\n\n" + entries
    write(tmp_path, "CHANGELOG.md", original)
    found = project_history.read(str(tmp_path), {"project_history": {"max_chars": 300}})
    assert found["truncated"] is True and len(found["text"]) <= 300
    assert "entry number 400" in found["text"] and "## 1.0.1\n" not in found["text"] + "\n"
    assert found["text"].splitlines()[-1] in original.splitlines()           # no line is cut in half


def test_max_chars_zero_turns_it_off(tmp_path):
    write(tmp_path, "CHANGELOG.md", CHANGELOG)
    assert project_history.read(str(tmp_path), {"project_history": {"max_chars": 0}}) is None


def test_configured_file_names_are_used_in_order(tmp_path):
    write(tmp_path, "CHANGELOG.md", CHANGELOG)
    write(tmp_path, "docs/RULES.md", "# Rules\n- Keep the login flow.\n")
    found = project_history.read(str(tmp_path), {"project_history": {"files": ["docs/RULES.md", "CHANGELOG.md"]}})
    assert found["file"] == "docs/RULES.md" and "login flow" in found["text"]


def test_file_name_must_match_exactly(tmp_path):
    """macOS disks ignore case: changelog.md must not be taken for CHANGELOG.md."""
    write(tmp_path, "changelog.md", CHANGELOG)
    assert project_history.read(str(tmp_path)) is None


def test_project_kept_in_one_sub_folder_is_found(tmp_path):
    write(tmp_path, "app/CHANGELOG.md", CHANGELOG)
    write(tmp_path, "tools/readme.txt", "x")
    assert project_history.read(str(tmp_path))["file"] == "app/CHANGELOG.md"


def test_several_sub_folders_with_a_history_are_ambiguous(tmp_path):
    write(tmp_path, "app/CHANGELOG.md", CHANGELOG)
    write(tmp_path, "site/CHANGELOG.md", CHANGELOG)
    assert project_history.read(str(tmp_path)) is None


def test_dependency_folders_are_not_searched(tmp_path):
    write(tmp_path, "node_modules/CHANGELOG.md", CHANGELOG)
    write(tmp_path, "deps/CHANGELOG.md", CHANGELOG)
    assert project_history.read(str(tmp_path)) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_history_and_paths_outside_the_project_are_ignored(tmp_path):
    outside = write(tmp_path / "outside", "secret.md", "# outside\n- private\n")
    project = tmp_path / "proj"
    project.mkdir()
    os.symlink(outside, project / "CHANGELOG.md")
    assert project_history.read(str(project)) is None
    config = {"project_history": {"files": ["../outside/secret.md", str(outside)]}}
    assert project_history.read(str(project), config) is None


def test_binary_file_is_not_quoted(tmp_path):
    (tmp_path / "CHANGELOG.md").write_bytes(b"\x00\x01\x02binary")
    assert project_history.read(str(tmp_path)) is None


# ------------------------------------------------------------------ HEAD planning

class PlanningHead(BaseHead):
    provider = "mock"

    def __init__(self):
        self.prompts = []

    def create_order(self, goal, project_context): raise NotImplementedError
    def decide(self, order, builder_result, verification, review_result, cwd=None, **kwargs): raise NotImplementedError
    def get_provenance(self, exit_code=0): raise NotImplementedError
    def chat(self, message, project_context, history): raise NotImplementedError

    def _invoke_cli(self, prompt, cwd=None):
        self.prompts.append(prompt)
        return '{"status": "DONE", "summary": "ok", "reason": "r", "order": null}'

    def _extract_json(self, text):
        return json.loads(text)


def test_planning_prompt_quotes_the_history_outside_the_file_list():
    head = PlanningHead()
    context = {"project_path": "/p", "existing_files": ["a.py"],
               "decision_history": {"file": "CHANGELOG.md", "text": CHANGELOG.strip(), "truncated": False}}
    assert head.plan_next("goal", [], context)["status"] == "DONE"
    prompt = head.prompts[0]
    assert "PROJECT DECISION HISTORY (the project's own CHANGELOG.md" in prompt and RULE in prompt
    assert "Do not plan a step that reverses" in prompt
    assert prompt.index(RULE) < prompt.index("CURRENT PROJECT FILES:")
    assert "decision_history" not in prompt                       # not repeated inside the capped JSON
    assert "decision_history" in context                          # the caller's dict is left alone


def test_planning_prompt_is_unchanged_without_a_history():
    head = PlanningHead()
    head.plan_next("goal", [], {"project_path": "/p", "existing_files": []})
    assert "PROJECT DECISION HISTORY" not in head.prompts[0]


def test_mission_planning_reads_the_history_from_the_project_not_the_worktree(tmp_path):
    project, worktree = tmp_path / "proj", tmp_path / "wt"
    write(project, "CHANGELOG.md", CHANGELOG)
    write(worktree, "CHANGELOG.md", "# Changelog\n\n## 9.9.9\n- Builder rewrote the rules.\n")
    seen = {}

    class Head:
        def plan_next(self, goal, history, context, cwd=None):
            seen.update(context)
            return {"status": "DONE", "summary": "done"}

    class Orch:
        head, config = Head(), {}
        def _read_project_context(self, where): return {"project_path": where, "existing_files": []}
        def _get_agent_domain(self, agent): return "MOCK"

    mission = {"mission_id": "MSN-H", "goal": "g", "project_path": str(project), "base_worktree": str(worktree),
               "steps": [{"index": 1, "order": {"goal": "g"}, "run_id": "RUN-1", "result": None}]}
    plan = MissionRunner(lambda: None, store=MissionStore(tmp_path / "missions"))._plan(Orch(), mission)
    assert plan["status"] == "DONE"
    assert RULE in seen["decision_history"]["text"] and "Builder rewrote" not in seen["decision_history"]["text"]


# ------------------------------------------------------------------ Reviewer

def test_reviewer_context_gets_the_history_of_the_runs_project(tmp_path):
    project = tmp_path / "proj"
    write(project, "CHANGELOG.md", CHANGELOG)
    orch = _mock_orch()
    orch.sm.create_run("RUN-HISTORY-CTX", str(project), "g")
    context = orch._with_decision_history("RUN-HISTORY-CTX", {})
    assert context["decision_history"]["file"] == "CHANGELOG.md" and RULE in context["decision_history"]["text"]
    bare = tmp_path / "bare"
    bare.mkdir()
    orch.sm.create_run("RUN-HISTORY-NONE", str(bare), "g")
    assert orch._with_decision_history("RUN-HISTORY-NONE", {"k": 1}) == {"k": 1}


def test_reviewer_history_can_be_turned_off(tmp_path):
    project = tmp_path / "proj"
    write(project, "CHANGELOG.md", CHANGELOG)
    orch = _mock_orch(project_history={"max_chars": 0})
    orch.sm.create_run("RUN-HISTORY-OFF", str(project), "g")
    assert orch._with_decision_history("RUN-HISTORY-OFF", {}) == {}


@pytest.mark.parametrize("reviewer_class", [ClaudeReviewer, CodexReviewer])
def test_review_prompt_quotes_the_history_before_the_diff(monkeypatch, tmp_path, reviewer_class):
    sent = []

    def run(*args, **kwargs):
        sent.append(repr(args) + repr(kwargs))
        return ProcessResult(exit_code=0, stdout='```json\n{"verdict": "pass", "p0": [], "p1": [], "p2": [], '
                             '"summary": "ok"}\n```', stderr="", timed_out=False, duration_sec=0.1, pid=1)
    monkeypatch.setattr(ProcessTreeRunner, "run", run)
    order = OrderSchema(goal="g", allowed_paths=["src/"], protected_paths=[".git/"], acceptance_criteria=["ok"])
    reviewer = reviewer_class()
    reviewer.executable = "dummy"
    history = {"decision_history": {"file": "CHANGELOG.md", "text": CHANGELOG.strip(), "truncated": False}}
    reviewer._review_with_cli(order, "DIFF-MARKER", {}, str(tmp_path), history)
    reviewer._review_with_cli(order, "DIFF-MARKER", {}, str(tmp_path))
    with_history, without = sent[-2], sent[-1]
    assert RULE in with_history and "report it as P1" in with_history
    assert with_history.index(RULE) < with_history.index("DIFF-MARKER")
    assert "PROJECT DECISION HISTORY" not in without and "DIFF-MARKER" in without


# ------------------------------------------------------------------ mission-wide change size

RUNS = get_runs_dir()


@pytest.fixture
def passed_run(tmp_path):
    """Factory: a project, and a run whose worktree holds `files_after` on top of it."""
    made = []

    def make(files_before, files_after):
        project = tmp_path / "proj"
        project.mkdir()
        git(project, "init", "-q")
        for rel, text in files_before.items():
            write(project, rel, text)
        git(project, "add", "-A")
        git(project, "commit", "-qm", "init")
        base_ref = git(project, "rev-parse", "HEAD").strip()
        run_id = "RUN-TEST-SIZE-" + uuid.uuid4().hex[:12].upper()
        made.append(run_id)
        worktree = tmp_path / run_id
        git(project, "worktree", "add", "-q", "-b", run_id, str(worktree))
        for rel, text in files_after.items():
            write(worktree, rel, text)
        sm = StateMachine()
        sm.create_run(run_id, str(project), "g")
        sm.update_run_worktree(run_id, str(worktree), run_id)
        (RUNS / run_id).mkdir(parents=True, exist_ok=True)
        (RUNS / run_id / "source_snapshot.json").write_text(json.dumps({"base_ref": base_ref}), encoding="utf-8")
        return project, worktree, run_id
    yield make
    for run_id in made:
        shutil.rmtree(RUNS / run_id, ignore_errors=True)


def test_change_totals_count_files_and_lines_against_the_project(passed_run):
    project, worktree, run_id = passed_run(
        {"a.py": "one\ntwo\nthree\n", "keep.py": "same\n"},
        {"a.py": "one\nTWO\nthree\nfour\n", "new.py": "x\ny\n", "logo.bin": "\x00\x01"})
    totals = ResultManager().change_totals(run_id)
    assert totals == {"files": 3, "new_files": 2, "added": 4, "removed": 1}


def test_change_totals_are_none_when_nothing_differs(passed_run):
    project, worktree, run_id = passed_run({"a.py": "one\n"}, {})
    assert ResultManager().change_totals(run_id) is None


def finished_mission(passed_run, tmp_path, files_after, safety=None):
    project, worktree, run_id = passed_run({"a.py": "one\ntwo\n"}, files_after)
    mission = {"mission_id": "MSN-SIZE", "goal": "g", "project_path": str(project), "base_worktree": str(worktree),
               "summary": "", "status": "RUNNING", "auto_apply": "off", "max_steps": 2,
               "steps": [{"index": 1, "order": {"goal": "g"}, "run_id": run_id,
                          "result": {"final_state": State.PASS, "worktree_path": str(worktree)}}]}

    class Orch:
        config = {"safety": safety or {"max_files_changed": 15, "max_diff_lines": 1500}}
    said = []
    runner = MissionRunner(lambda: Orch(), store=MissionStore(tmp_path / "missions"),
                           notify=lambda text, run_id=None, role="SYSTEM": said.append(text))
    return runner._finish(mission, "DONE", summary="done"), said[-1]


def test_final_message_reports_the_size_of_the_whole_mission(passed_run, tmp_path):
    mission, text = finished_mission(passed_run, tmp_path, {"a.py": "one\ntwo\nthree\n", "b.py": "x\n"})
    assert "미션 전체 변경: 2개 파일 (새 파일 1개), +2줄 / -0줄" in text
    assert "상한" not in text
    assert mission["change_totals"] == {"files": 2, "new_files": 1, "added": 2, "removed": 0}


def test_final_message_says_when_the_mission_adds_up_to_more_than_one_step_may_change(passed_run, tmp_path):
    mission, text = finished_mission(passed_run, tmp_path, {"a.py": "one\ntwo\nthree\n", "b.py": "x\n", "c.py": "y\n"},
                                     safety={"max_files_changed": 2, "max_diff_lines": 1500})
    assert "미션 전체 변경: 3개 파일" in text
    assert "한 단계 상한(2개 파일, 1500줄)을 넘는 누적 변경" in text
    assert mission["status"] == "DONE"                               # reported, never blocked


def test_final_message_has_no_size_line_without_a_deliverable(tmp_path):
    said = []
    runner = MissionRunner(lambda: None, store=MissionStore(tmp_path / "missions"),
                           notify=lambda text, run_id=None, role="SYSTEM": said.append(text))
    mission = {"mission_id": "MSN-NONE", "goal": "g", "project_path": str(tmp_path), "base_worktree": None,
               "summary": "", "status": "RUNNING", "auto_apply": "off", "max_steps": 2,
               "steps": [{"index": 1, "order": {"goal": "g"}, "run_id": "RUN-X",
                          "result": {"final_state": State.FAIL}}]}
    runner._finish(mission, "INCOMPLETE", summary="")
    assert "미션 전체 변경" not in said[-1]
