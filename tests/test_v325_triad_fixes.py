"""
v3.2.5 (HanginIs fork): TRIAD fixes taken from digilogd-cmd/ai-dev-director PR #1 by edge0303, who found them by
running full TRIAD cycles with the real CLIs. Only these unit-level checks were run on this fork; a real TRIAD cycle
was not.
"""
import os
import subprocess

from core.research_experiment import TriadExperimentOrchestrator
from core.research_orchestrator import ResearchOrchestrator
from core.research_prompts import build_experiment_order_prompt, build_research_memory_prompt
from core.research_runner import TriadResearchRunner


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def test_greenfield_accepts_only_a_snapshot_with_unchanged_content(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "--allow-empty", "-qm", "start")
    start = git(repo, "rev-parse", "HEAD")
    same = TriadExperimentOrchestrator._same_content_as
    assert same(str(repo), start, start)
    git(repo, "commit", "--allow-empty", "-qm", "snapshot")
    assert same(str(repo), git(repo, "rev-parse", "HEAD"), start)           # the bootstrap snapshot adds nothing
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "content")
    assert not same(str(repo), git(repo, "rev-parse", "HEAD"), start)       # real content is not the qualified state
    assert not same(str(repo), None, start) and not same(str(repo), start, None)
    assert not same(str(repo), "0" * 40, start)


def test_prompts_state_what_the_director_enforces():
    order = build_experiment_order_prompt("c", {"synthesized_hypothesis": "h"}, {}, "GREENFIELD", "NA",
                                          verification_command=["swift", "test"])
    assert order["prompt"]["verification_command"] == ["swift", "test"]
    assert "verification_command" in order["prompt"]["instructions"]
    assert build_experiment_order_prompt("c", {"synthesized_hypothesis": "h"}, {}, "GREENFIELD", "NA")[
        "prompt"]["verification_command"] is None
    memory = build_research_memory_prompt({"synthesized_hypothesis": "h"}, {}, {"A": {}, "B": {}}, {"A": {}, "B": {}})
    assert "exact character-for-character copy" in memory["prompt"]["instructions"]


def test_planner_is_told_the_command_this_project_is_verified_with(tmp_path):
    registered = tmp_path / "registered"
    registered.mkdir()
    detected = tmp_path / "detected"
    (detected / "Tests").mkdir(parents=True)
    (detected / "Tests" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    config = {"verification": {"test_command": "python -m pytest tests/"},
              "projects": {str(registered): {"verification": {"test_command": ["make", "check"]}}}}
    orch = ResearchOrchestrator.__new__(ResearchOrchestrator)
    orch.experiment_config = config
    assert orch._verification_command(str(registered)) == ["make", "check"]
    assert orch._verification_command(str(detected)) == ["bash", "Tests/run.sh"]
    assert orch._verification_command(str(empty)) is None


def test_network_drop_is_retried_but_quota_is_not():
    retry = TriadResearchRunner._is_retryable
    assert retry("GOOGLE_STREAM_FAILED: request failed: read tcp 1.2.3.4:1->5.6.7.8:443: wsarecv: A connection attempt failed")
    assert retry("CLI failed: read: connection reset by peer")
    assert retry("net/http: TLS handshake timeout")
    assert not retry("GOOGLE_STREAM_FAILED: quota exhausted")
    assert not retry("authentication failed")
