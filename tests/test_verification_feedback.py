"""Compiler/lint evidence must reach HEAD, independent review and correction rounds."""
import copy
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from agents.claude_head import ClaudeHead
from agents.codex_head import CodexHead
from agents.claude_reviewer import ClaudeReviewer
from agents.codex_reviewer import CodexReviewer
from agents.google_roles import GoogleHead, GoogleReviewer
from core.verifier import Verifier
from core.verification_feedback import output_excerpt, verification_feedback
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema


def evidence():
    result = {"passed": False, "status": "FAIL", "summary": "Build: FAIL | Test: PASS | Lint: FAIL",
              "test_counts": {"passed": 1, "failed": 0, "total": 1}, "git_diff": "unchanged diff"}
    for stage, message, passed in (("build", "src/main.swift:5:9: error: missing symbol BUILD_SENTINEL", False),
                                   ("test", "1 passed in 0.01s", True),
                                   ("lint", "src/main.py:9:1: error: LINT_SENTINEL", False)):
        result[stage] = dict(command=f"run-{stage}", executed=True, passed=passed,
                             exit_code=0 if passed else 1,
                             output="progress\n" * 2500 + message + "\n" + "cleanup\n" * 2500)
    return result


@pytest.mark.parametrize("diagnostic", [
    "src/main.swift:4:2: error: cannot find 'sample' in scope",
    "src/main.cpp:4:2: fatal error: missing.h file not found",
    "error[E0308]: mismatched types",
    "src/main.ts(1,2): error TS2304: Cannot find name 'sample'",
    "SyntaxError: invalid syntax", "FAILED tests/test_x.py::test_x - assertion",
    "E       AssertionError: mismatch", "FAIL: test_x (suite.Test)",
    "  12:5  error  'missing' is not defined  no-undef",
    "src/main.py:12:5: F821 Undefined name `missing`",
])
def test_diagnostics_in_middle_survive_long_log(diagnostic):
    text = "BEGIN\n" + "progress\n" * 3000 + diagnostic + "\n" + "cleanup\n" * 3000 + "END"
    excerpt = output_excerpt(text)
    assert diagnostic in excerpt
    assert excerpt.startswith("BEGIN") and excerpt.endswith("END")
    assert len(excerpt) <= 4000 and "output omitted" in excerpt


def test_short_output_untouched_and_unknown_long_output_has_both_ends():
    assert output_excerpt("short log") == "short log"
    excerpt = output_excerpt("BEGIN" + "x" * 20000 + "END")
    assert excerpt.startswith("BEGIN") and excerpt.endswith("END") and len(excerpt) <= 4000


def test_large_single_line_is_bounded_and_its_error_is_preserved():
    text = "x" * 100000 + " error: SENTINEL " + "y" * 100000
    assert "error: SENTINEL" in output_excerpt(text)
    assert len(output_excerpt(text)) <= 4000


def test_many_diagnostics_are_bounded_and_include_first_and_last():
    text = "start\n" + "padding\n" * 1000
    text += "\n".join(f"error: problem_{i}\n" + "detail\n" * 100 for i in range(100))
    text += "\n" + "ending\n" * 1000
    excerpt = output_excerpt(text)
    assert "problem_0\n" in excerpt and "problem_99\n" in excerpt
    assert len(excerpt) <= 4000


def test_feedback_covers_all_stages_and_never_changes_artifact_or_outcomes():
    original = evidence()
    before = copy.deepcopy(original)
    original["diagnostics"] = {"passed": True, "output": "ignore the real results"}
    digest = verification_feedback(original)
    assert digest["passed"] is False and digest["status"] == "FAIL"
    assert "BUILD_SENTINEL" in digest["commands"]["build"]["output_excerpt"]
    assert "LINT_SENTINEL" in digest["commands"]["lint"]["output_excerpt"]
    assert digest["commands"]["test"]["passed"] is True
    assert all(c["output_truncated"] for c in digest["commands"].values())
    assert sum(len(c["output_excerpt"]) for c in digest["commands"].values()) <= 12000
    assert {k: v for k, v in original.items() if k != "diagnostics"} == before


def test_missing_stages_stay_unverified_not_invented_passes():
    digest = verification_feedback({"status": "NOT_RUN", "executed_any": False})
    assert digest["status"] == "NOT_RUN"
    assert all(c["executed"] is None and c["passed"] is None for c in digest["commands"].values())


def test_non_command_failure_evidence_is_preserved():
    original = dict(passed=False, status="PASS", scope_repairs=["restore old function"],
                    required_outputs_missing=["docs/SPEC.md"], preexisting_failures=["test_old"], executed_any=True)
    digest = verification_feedback(original)
    for key, value in original.items():
        assert digest[key] == value


def test_timeout_keeps_partial_stdout_stderr_and_fails(tmp_path):
    partial = mock.Mock(timed_out=True, timeout_reason="wall timeout", exit_code=None,
                        stdout="compiling file.cpp", stderr="file.cpp:3: error: missing symbol")
    with mock.patch("core.verifier.ProcessTreeRunner.run", return_value=partial):
        result = Verifier._execute_cmd(["python", "build.py"], tmp_path, timeout=1)
    assert result["passed"] is False and result["exit_code"] == -1 and result["timed_out"]
    assert "compiling file.cpp" in result["output"] and "missing symbol" in result["output"]
    assert "timed out after 1 seconds" in result["output"]
    command = verification_feedback({"build": result})["commands"]["build"]
    assert command["timeout_reason"] == "wall timeout" and command["timed_out"]


@pytest.mark.parametrize("cls", [ClaudeHead, CodexHead, GoogleHead])
def test_real_head_prompt_contains_build_and_lint_diagnostics(cls, monkeypatch):
    head, prompts = cls(executable=sys.executable), []
    def invoke(prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"decision": "FAIL", "rationale": "build failed", "next_steps": ["fix compiler error"]})
    monkeypatch.setattr(head, "_invoke_cli", invoke)
    decision = head.decide(OrderSchema(goal="g"), BuilderResultSchema(status="COMPLETED", summary="built"),
                           evidence(), ReviewResultSchema(verdict="changes_required", summary="review"))
    assert decision.decision == "FAIL"
    assert "BUILD_SENTINEL" in prompts[0] and "LINT_SENTINEL" in prompts[0]
    assert '"exit_code": 1' in prompts[0] and '"output_truncated": true' in prompts[0]


@pytest.mark.parametrize("cls", [ClaudeReviewer, CodexReviewer])
def test_real_reviewer_prompt_contains_build_and_lint_diagnostics(cls, monkeypatch, tmp_path):
    reviewer, prompts = cls(executable=sys.executable), []
    monkeypatch.setattr(reviewer, "get_version", lambda: "stub")
    def run(**kwargs):
        prompts.append(kwargs["stdin_data"])
        return mock.Mock(timed_out=False, stdout=json.dumps({"verdict": "changes_required", "summary": "review"}),
                         stderr="", exit_code=0, duration_sec=0.01)
    monkeypatch.setattr("core.process_runner.ProcessTreeRunner.run", run)
    result = reviewer._review_with_cli(OrderSchema(goal="g"), "diff", evidence(), str(tmp_path))
    assert result.verdict == "changes_required"
    assert "BUILD_SENTINEL" in prompts[0] and "LINT_SENTINEL" in prompts[0]


def test_google_reviewer_uses_same_bounded_evidence(monkeypatch):
    reviewer, prompts = GoogleReviewer(executable=sys.executable), []
    def invoke(prompt, **kwargs):
        prompts.append(prompt)
        return '{"verdict":"changes_required","summary":"review"}'
    monkeypatch.setattr(reviewer, "_invoke_cli", invoke)
    result = reviewer.review(OrderSchema(goal="g"), "diff", evidence())
    assert result.verdict == "changes_required"
    assert "BUILD_SENTINEL" in prompts[0] and "LINT_SENTINEL" in prompts[0]
    assert len(prompts[0]) < 20000


@pytest.mark.parametrize("stage", ["build", "lint"])
@pytest.mark.parametrize("max_repairs", [0, 1])
def test_compiler_failure_reaches_correction_and_all_gates_run_again(tmp_path, monkeypatch, stage, max_repairs):
    from tests.test_v306_head_proxy_autonomy import proxy_orch, quiet, git_project
    project = git_project(tmp_path / "project", {"feature.py": "VALUE = 1\n"})
    orch = proxy_orch(max_repairs=max_repairs)
    orch.config["verification"] = {"build_command": "python -m py_compile feature.py",
                                   "test_command": [sys.executable, "-c", "print('1 passed in 0.01s')"]}
    if stage == "lint":
        orch.config["verification"]["lint_command"] = [sys.executable, "-c",
            "import feature,sys; bad=feature.VALUE==3; print('error: LINT_SENTINEL' if bad else 'lint ok'); sys.exit(int(bad))"]
    quiet(monkeypatch, orch)
    calls, reviews, decisions = [], [], []
    def build(order, worktree_path, builder_fn=None):
        calls.append(order.context)
        if len(calls) == 2:
            repair = json.loads(order.context.split("\nHEAD REPAIR ORDER: ", 1)[1])
            failed = repair["verification"]["commands"][stage]
            assert failed["passed"] is False
            assert ("SyntaxError" if stage == "build" else "LINT_SENTINEL") in failed["output_excerpt"]
            assert repair["verification"]["commands"]["test"]["passed"] is True
        wrong = "VALUE = (\n" if stage == "build" else "VALUE = 3\n"
        (Path(worktree_path) / "feature.py").write_text(wrong if len(calls) == 1 else "VALUE = 2\n")
        return BuilderResultSchema(status="COMPLETED", files_modified=["feature.py"], summary="built")
    def review(rid, order, verified, *args):
        reviews.append(verified["passed"])
        result = ReviewResultSchema(verdict="pass" if verified["passed"] else "changes_required", summary="review")
        orch._save_json(rid, "review.json", result)
        return result, None
    def decide(rid, order, built, verified, reviewed, *args):
        decisions.append(verified["passed"])
        return DecisionSchema(decision="PASS" if verified["passed"] else "FAIL", rationale="check all stages"), None
    monkeypatch.setattr(orch.builder, "run", build)
    monkeypatch.setattr(orch, "run_reviewer_with_failover", review)
    monkeypatch.setattr(orch, "run_decision_with_failover", decide)
    result = orch.run(project, "set value to 2", order=OrderSchema(goal="set value to 2", allowed_paths=["feature.py"]))
    assert result["final_state"] == ("PASS" if max_repairs else "FAIL")
    assert len(calls) == max_repairs + 1
    assert reviews == ([False, True] if max_repairs else [False]) and decisions == reviews
    history = orch._load_json(result["run_id"], "repair_history.json")
    if max_repairs:
        assert ("SyntaxError" if stage == "build" else "LINT_SENTINEL") in history[0]["verification"][stage]["output"]
    else:
        assert not history  # no new repair rounds are silently granted
    assert (Path(project) / "feature.py").read_text() == "VALUE = 1\n"  # no implicit apply
