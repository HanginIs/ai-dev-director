"""
v3.2.3 (HanginIs fork): Claude Code is started read-only wherever the Director uses it. HEAD is a read-only role,
but only Codex was started in a read-only sandbox; a Claude HEAD ran the project's tests itself (2026-11-12).
"""
import pytest

from agents.claude_head import ClaudeHead
from agents.claude_reviewer import ClaudeReviewer
from core import claude_cli
from core.process_runner import ProcessResult, ProcessTreeRunner
from core.provider_manager import ErrorCategory
from core.research_runner import TriadResearchRunner
from schemas.order import OrderSchema

EXPECTED = ["--tools", "Read,Grep,Glob", "--disallowedTools", "mcp__*", "--permission-prompts", "none",
            "--setting-sources", "user"]
ORDER = OrderSchema(goal="g", allowed_paths=["src/"], protected_paths=[".git/"], acceptance_criteria=["ok"])


def capture(monkeypatch, result):
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs.get("argv") or args[0])
        return result
    monkeypatch.setattr(ProcessTreeRunner, "run", run)
    return calls


def ok(stdout):
    return ProcessResult(exit_code=0, stdout=stdout, stderr="", timed_out=False, duration_sec=0.1, pid=1)


def test_default_arguments_remove_every_tool_that_writes_or_runs():
    assert claude_cli.read_only_args() == EXPECTED
    tools = EXPECTED[EXPECTED.index("--tools") + 1].split(",")
    assert not {"Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch"} & set(tools)


def test_head_starts_claude_read_only(monkeypatch, tmp_path):
    calls = capture(monkeypatch, ok('{"result": "{\\"decision\\": \\"APPROVE\\", \\"reason\\": \\"r\\"}"}'))
    head = ClaudeHead()
    head.executable = __file__                                   # any existing path
    head._invoke_cli("prompt", cwd=str(tmp_path))
    assert calls[0][1:4] == ["--print", "--output-format", "json"] and calls[0][4:] == EXPECTED
    assert "--tools Read,Grep,Glob" in head.last_execution_info["invocation_command"]


def test_reviewer_starts_claude_read_only(monkeypatch, tmp_path):
    calls = capture(monkeypatch, ok('```json\n{"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok"}\n```'))
    reviewer = ClaudeReviewer()
    reviewer.executable = "dummy"
    assert reviewer._review_with_cli(ORDER, "", {}, str(tmp_path)).verdict == "pass"
    assert calls[0][1] == "--print" and calls[0][2:] == EXPECTED


def test_research_and_proposal_builder_start_claude_read_only():
    argv, stdin, described = TriadResearchRunner(executables={"ANTHROPIC": "claude"})._command("ANTHROPIC", "claude", "p")
    assert argv[4:] == EXPECTED and stdin == "p" and "--setting-sources user" in described


def test_arguments_can_be_replaced_or_removed_in_config(monkeypatch):
    import director
    monkeypatch.setattr(director, "load_config", lambda: {"claude_cli": {"read_only_args": ["--restricted"]}})
    assert claude_cli.read_only_args() == ["--restricted"]
    monkeypatch.setattr(director, "load_config", lambda: {"claude_cli": {"read_only_args": []}})
    assert claude_cli.read_only_args() == []
    monkeypatch.setattr(director, "load_config", lambda: {"claude_cli": {"read_only_args": "--tools Bash"}})
    assert claude_cli.read_only_args() == EXPECTED                # not a list: ignored, the safe default stays


def test_old_cli_is_reported_as_a_configuration_error_with_the_fix(monkeypatch, tmp_path):
    stderr = "error: unknown option '--permission-prompts'"
    capture(monkeypatch, ProcessResult(exit_code=1, stdout="", stderr=stderr, timed_out=False, duration_sec=0.1, pid=1))
    head = ClaudeHead()
    head.executable = __file__
    with pytest.raises(RuntimeError) as raised:
        head._invoke_cli("prompt", cwd=str(tmp_path))
    message = str(raised.value)
    assert "2.1.259" in message and "claude_cli.read_only_args" in message
    assert ErrorCategory.classify(message) == ErrorCategory.CONFIG_ERROR
    reviewer = ClaudeReviewer()
    reviewer.executable = "dummy"
    review = reviewer._review_with_cli(ORDER, "", {}, str(tmp_path))
    assert review.verdict == "changes_required" and "CONFIG_ERROR" in review.p0[0]


def test_other_failures_are_not_called_a_configuration_error():
    assert claude_cli.old_cli_hint(1, "Usage limit reached") == ""
    assert claude_cli.old_cli_hint(0, "unknown option --tools") == ""
