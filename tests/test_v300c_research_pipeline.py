"""V3.0-C: concurrent independent research, cross critique, and evidence-led synthesis."""
import json
import threading
import time
from pathlib import Path

import pytest

import dashboard_server
from core.db_path import get_db_path
from core.process_runner import ProcessTreeRunner
from core.research_orchestrator import ResearchOrchestrator
from core.research_runner import TriadResearchRunner
from core.research_state import ResearchState, ResearchStateMachine


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "system.py").write_text("CURRENT = True\n", encoding="utf-8")
    return project


def _proposal(domain):
    return {
        "hypothesis": f"{domain} hypothesis", "rationale": f"{domain} rationale", "novelty": "novel",
        "assumptions": ["a1"], "falsifiable_prediction": "metric improves", "proposed_experiment": "run trial",
        "evidence_needed": ["metric"], "failure_modes": ["noise"], "kill_criteria": ["metric falls"],
        "risks": ["bias"], "confidence": 0.6, "freeform_insight": f"insight-{domain}",
    }


def _critique(domain):
    return {
        "strongest_point": "testable", "weakest_assumption": "stationarity", "logical_flaw": "none proven",
        "hidden_risk": "selection bias", "falsification_method": "holdout", "experiment_flaw": "sample size",
        "useful_combination": "combine mechanisms", "self_critique": f"{domain} may be overconfident",
        "target_observations": {"OPENAI": "good", "ANTHROPIC": "risky", "GOOGLE": "novel"},
    }


def _synthesis():
    return {
        "selection_type": "NEW_D", "title": "Derived D", "synthesized_hypothesis": "combine causal checks",
        "rationale": "critique exposed a shared confound", "source_domains": ["ANTHROPIC", "GOOGLE"],
        "critique_lessons": ["use holdout"], "falsifiable_prediction": "holdout metric improves",
        "proposed_experiment": "controlled comparison", "kill_criteria": ["no holdout gain"],
        "risks": ["small sample"], "confidence": 0.7,
    }


class DeterministicCalls:
    def __init__(self, fail_domain=None, invalid_stage=None):
        self.fail_domain = fail_domain
        self.invalid_stage = invalid_stage
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, domain, prompt, stage, cwd):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append({"domain": domain, "stage": stage, "prompt": prompt, "cwd": cwd})
        time.sleep(0.04)
        try:
            if domain == self.fail_domain and stage == ResearchState.INDEPENDENT_RESEARCH:
                raise RuntimeError("forced provider failure")
            if stage == self.invalid_stage:
                output = "not-json"
            elif stage == ResearchState.INDEPENDENT_RESEARCH:
                output = json.dumps(_proposal(domain))
            elif stage == ResearchState.CROSS_CRITIQUE:
                output = json.dumps(_critique(domain))
            else:
                output = json.dumps(_synthesis())
            return {
                "output": output, "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": f"stub {stage}", "started_at": "2026-01-01T00:00:00",
                "ended_at": "2026-01-01T00:00:01", "duration_sec": 1.0, "exit_code": 0,
                "token_usage": "UNKNOWN", "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }
        finally:
            with self.lock:
                self.active -= 1


def _ready(tmp_path, provider_state, calls=None):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    calls = calls or DeterministicCalls()
    orch = ResearchOrchestrator(
        state_machine=ResearchStateMachine(), provider_manager=pm,
        artifact_root=tmp_path / "research", provider_call=calls,
    )
    session = orch.start_session(str(_project(tmp_path)), "Find a new falsifiable mechanism")
    return orch, session, calls


@pytest.mark.parametrize(("providers", "google_status", "fail_domain"), [
    (("OPENAI", "ANTHROPIC", "GOOGLE"), "HEALTHY", None),
    (("OPENAI", "ANTHROPIC", "GOOGLE"), "HEALTHY", "GOOGLE"),
    (("OPENAI", "ANTHROPIC", "GOOGLE"), "RECOVERING", None),
    (("OPENAI", "ANTHROPIC"), "HEALTHY", None),
    (("OPENAI", "ANTHROPIC"), "QUOTA_EXHAUSTED", None),
    (("OPENAI", "GOOGLE"), "HEALTHY", None),
    (("ANTHROPIC", "GOOGLE"), "HEALTHY", None),
])
def test_dashboard_start_runs_in_temporary_worktrees_and_stops_at_synthesis(
    monkeypatch, provider_state, providers, google_status, fail_domain,
):
    provider_state(GOOGLE=google_status)
    calls = DeterministicCalls(fail_domain=fail_domain)
    entered = threading.Event()
    release = threading.Event()

    def invoke(_runner, domain, prompt, cwd, stage):
        entered.set()
        assert release.wait(10)
        assert (Path(cwd) / ".git").is_file()
        return calls(domain, prompt, stage, cwd)

    monkeypatch.setattr(TriadResearchRunner, "invoke", invoke)
    monkeypatch.setattr(dashboard_server, "_RESEARCH_JOB", {"session_id": None, "busy": False, "error": ""})
    assert dashboard_server.start_dashboard_research("   ")["status"] == "error"
    try:
        started = dashboard_server.start_dashboard_research("음원 파일명에서 버전명을 분리하는 규칙", providers)
        assert started["status"] == "started"
        sid = started["session_id"]
        assert entered.wait(10)
        live = dashboard_server.build_research_status_payload()
        assert live["session_id"] == sid and live["active"] and live["worker_busy"]
        assert live["current_stage"] == ResearchState.INDEPENDENT_RESEARCH
        assert dashboard_server.start_dashboard_research("두 번째 연구")["status"] == "busy"
    finally:
        release.set()
        for worker in threading.enumerate():
            if worker.name.startswith("director-research-"):
                worker.join(timeout=10)
                assert not worker.is_alive()

    final = dashboard_server.build_research_status_payload(sid)
    assert not final["active"] and not final["worker_busy"] and final["experiment"] is None
    assert (get_db_path().parent / "research" / sid).is_dir()
    assert not Path(final["project_path"]).exists()
    if fail_domain:
        assert final["status"] == ResearchState.HUMAN_REQUIRED
        assert "forced provider failure" in final["next_step"]
        assert len(calls.calls) == 3
    else:
        assert final["status"] == final["current_stage"] == ResearchState.SYNTHESIS_COMPLETE
        assert final["synthesis"]["synthesized_hypothesis"] == "combine causal checks"
        participants = set(providers)
        synthesizer = "OPENAI" if "OPENAI" in participants else "ANTHROPIC"
        synthesis_call = next(call for call in calls.calls if call["stage"] == ResearchState.SYNTHESIS)
        assert final["synthesis_domain"] == synthesis_call["domain"] == synthesizer
        synthesis_prompt = json.loads(synthesis_call["prompt"])
        assert synthesis_prompt["provider_domain"] == synthesizer
        assert f"Do not prefer the {synthesizer} proposal" in synthesis_prompt["instructions"]
        assert {row["domain"] for row in final["proposals"]} == participants
        assert {row["domain"] for row in final["cross_critiques"]} == participants
        assert {call["domain"] for call in calls.calls} == participants
        assert len(calls.calls) == 2 * len(participants) + 1
        if len(providers) == 2:
            assert final["readiness"] == "TRIAD_DEGRADED"
            assert set(final["excluded_domains"]) == {"OPENAI", "ANTHROPIC", "GOOGLE"} - participants
            assert ResearchStateMachine().get_session(sid)["degraded_approved"] == 1
            assert dashboard_server.ProviderManager().get_status("GOOGLE").value == google_status
        if google_status == "RECOVERING":
            assert dashboard_server.ProviderManager().get_status("GOOGLE").value == "HEALTHY"


@pytest.mark.parametrize("providers", [None, [], ["OPENAI"], ["OPENAI", "OPENAI"], ["OPENAI", "OTHER"]])
def test_dashboard_start_rejects_invalid_provider_selection(providers):
    assert dashboard_server.start_dashboard_research("Compare two hypotheses", providers)["status"] == "error"


def test_dashboard_stop_prevents_research_calls_in_later_stages(monkeypatch):
    started_calls = []
    both_entered = threading.Event()
    release = threading.Event()
    killed_owners = []
    call_lock = threading.Lock()

    def invoke(_runner, domain, prompt, cwd, stage):
        with call_lock:
            started_calls.append((domain, stage))
            if len(started_calls) == 2:
                both_entered.set()
        assert release.wait(10)
        return {"output": json.dumps(_proposal(domain))}

    def kill_active(owner_id):
        killed_owners.append(owner_id)
        release.set()
        return [123]

    monkeypatch.setattr(TriadResearchRunner, "invoke", invoke)
    monkeypatch.setattr(ProcessTreeRunner, "kill_active", kill_active)
    monkeypatch.setattr(dashboard_server, "_RESEARCH_JOB", {"session_id": None, "busy": False, "error": ""})
    started = dashboard_server.start_dashboard_research("Compare two hypotheses", ["ANTHROPIC", "GOOGLE"])
    try:
        assert started["status"] == "started"
        assert both_entered.wait(10)
        stopped = dashboard_server.stop_missions("test stop")
        assert stopped["status"] == "stopped"
        assert stopped["research_session_id"] == started["session_id"]
        assert killed_owners == [started["session_id"]]
    finally:
        release.set()
        for worker in threading.enumerate():
            if worker.name == f"director-research-{started.get('session_id')}":
                worker.join(timeout=10)
                assert not worker.is_alive()

    status = dashboard_server.build_research_status_payload(started["session_id"])
    assert status["status"] == status["current_stage"] == ResearchState.RESEARCH_STOPPED
    assert not status["worker_busy"]
    assert set(started_calls) == {
        ("ANTHROPIC", ResearchState.INDEPENDENT_RESEARCH), ("GOOGLE", ResearchState.INDEPENDENT_RESEARCH),
    }
    assert not status["proposals"] and not status["cross_critiques"] and status["synthesis"] is None


def test_research_does_not_call_a_recovering_provider_without_a_claim(tmp_path, provider_state, monkeypatch):
    calls = DeterministicCalls()
    pm = provider_state(GOOGLE="RECOVERING")
    assert pm.mark_trial_candidates() == ["GOOGLE"]
    monkeypatch.setattr(pm, "claim_trial", lambda domain: False)
    orch = ResearchOrchestrator(provider_manager=pm, artifact_root=tmp_path / "research", provider_call=calls)
    session = orch.start_session(str(_project(tmp_path)), "Compare two hypotheses")

    result = orch.run_independent_research(session["session_id"])

    assert result["human_required"]
    assert "RECOVERY_TRIAL_UNAVAILABLE: GOOGLE" in result["message"]
    assert {call["domain"] for call in calls.calls} == {"OPENAI", "ANTHROPIC"}


def test_two_provider_selection_blocks_if_claude_is_unavailable(tmp_path, provider_state):
    calls = DeterministicCalls()
    pm = provider_state(ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    orch = ResearchOrchestrator(provider_manager=pm, artifact_root=tmp_path / "research", provider_call=calls)
    result = orch.start_session(str(_project(tmp_path)), "Compare two hypotheses",
                                allow_degraded=True, synthesis_only=True, excluded_domains=("GOOGLE",))

    assert result["readiness"] == "TRIAD_BLOCKED" and result["human_required"]
    assert result["missing_domains"] == ["ANTHROPIC", "GOOGLE"]
    assert calls.calls == []


def test_1_independent_research_is_concurrent_and_undisclosed(tmp_path, provider_state):
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    orch, session, calls = _ready(tmp_path, provider_state)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.CROSS_CRITIQUE
    assert calls.max_active >= 2
    independent = [c for c in calls.calls if c["stage"] == ResearchState.INDEPENDENT_RESEARCH]
    assert len(independent) == 3
    for call in independent:
        prompt = json.loads(call["prompt"])
        assert "proposals" not in prompt and "critiques" not in prompt
        assert set(prompt["sources"]) == {
            "USER_CHALLENGE", "PROJECT_CONTEXT", "VERIFIED_EVIDENCE", "RESEARCH_MEMORY_FACTS"
        }
    assert len(ProcessTreeRunner.blocked_real_cli_calls) == before

    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    outputs = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert {o["domain"] for o in outputs} == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    provenance = ResearchStateMachine().get_call_provenance(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert len(provenance) == 3
    assert all(p["outcome"] == "SUCCESS" and p["token_usage"] == "UNKNOWN" for p in provenance)
    assert all(p["prompt_sha256"] and p["output_sha256"] and p["response_bytes"] > 0 for p in provenance)


def test_2_any_independent_failure_is_terminal_and_never_discloses_partial_work(tmp_path, provider_state):
    calls = DeterministicCalls(fail_domain="GOOGLE")
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert not [c for c in calls.calls if c["stage"] == ResearchState.CROSS_CRITIQUE]
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    assert len(ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)) == 2


def test_3_cross_critique_discloses_all_proposals_only_after_all_complete(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    orch.run_independent_research(session["session_id"])
    result = orch.run_cross_critique(session["session_id"])
    assert result["current_stage"] == ResearchState.SYNTHESIS
    critique_calls = [c for c in calls.calls if c["stage"] == ResearchState.CROSS_CRITIQUE]
    assert len(critique_calls) == 3 and calls.max_active >= 2
    for call in critique_calls:
        prompt = json.loads(call["prompt"])
        assert set(prompt["proposals"]) == {"OPENAI", "ANTHROPIC", "GOOGLE"}
        assert "self_critique" in prompt["instructions"]


def test_4_codex_synthesis_can_create_new_d_without_self_priority_or_majority_vote(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    orch.run_independent_research(session["session_id"])
    orch.run_cross_critique(session["session_id"])
    result = orch.run_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    synthesis_calls = [c for c in calls.calls if c["stage"] == ResearchState.SYNTHESIS]
    assert len(synthesis_calls) == 1 and synthesis_calls[0]["domain"] == "OPENAI"
    prompt = json.loads(synthesis_calls[0]["prompt"])
    instructions = prompt["instructions"].lower()
    assert "do not prefer the openai proposal" in instructions
    assert "do not use majority vote" in instructions
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    synthesis = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.SYNTHESIS)[0]["payload"]
    assert synthesis["selection_type"] == "NEW_D"
    assert synthesis["source_domains"] == ["ANTHROPIC", "GOOGLE"]


def test_5_end_to_end_stub_pipeline_uses_seven_calls_and_persists_raw_evidence(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    assert len(calls.calls) == 7
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    provenance = ResearchStateMachine().get_call_provenance(cycle_id)
    assert len(provenance) == 7
    assert all(Path(p["prompt_artifact"]).is_file() and Path(p["output_artifact"]).is_file() for p in provenance)
    assert all(p["api_key_used"] == 0 and p["authentication_mode"] == "subscription" for p in provenance)


def test_6_explicit_degraded_pipeline_stays_two_provider_and_uses_five_calls(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    calls = DeterministicCalls()
    orch = ResearchOrchestrator(ResearchStateMachine(), pm, tmp_path / "research", calls)
    session = orch.start_session(str(_project(tmp_path)), "Moonshot", allow_degraded=True)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    assert result["degraded"] is True and result["participant_count"] == 2
    assert len(calls.calls) == 5
    assert {c["domain"] for c in calls.calls if c["stage"] != ResearchState.SYNTHESIS} == {"OPENAI", "GOOGLE"}


def test_7_degraded_without_codex_fails_closed_before_synthesis_call(tmp_path, provider_state):
    pm = provider_state(OPENAI="UNAVAILABLE", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    calls = DeterministicCalls()
    orch = ResearchOrchestrator(ResearchStateMachine(), pm, tmp_path / "research", calls)
    session = orch.start_session(str(_project(tmp_path)), "Moonshot", allow_degraded=True)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert len(calls.calls) == 4
    assert not [c for c in calls.calls if c["stage"] == ResearchState.SYNTHESIS]


def test_8_invalid_structured_output_is_fail_closed_with_raw_artifact(tmp_path, provider_state):
    calls = DeterministicCalls(invalid_stage=ResearchState.INDEPENDENT_RESEARCH)
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    provenance = ResearchStateMachine().get_call_provenance(cycle_id)
    assert len(provenance) == 3 and all(p["outcome"] == "FAILED" for p in provenance)
    assert all(Path(p["output_artifact"]).read_text(encoding="utf-8") == "not-json" for p in provenance)


def test_8b_bare_string_for_a_list_field_is_coerced_not_rejected(tmp_path, provider_state):
    """A real H attempt (2026-09-10) captured Claude returning a well-formed, on-topic proposal where
    'assumptions' (List[str]) was a single bare string instead of a one-item list, even though the prompt's
    type contract was explicit. This is a JSON-shape slip, not a content problem - the fix widens a bare
    string into [string] only for fields the schema itself declares List[str], and must not touch any other
    field or mask a genuinely wrong type."""
    from schemas.research import ResearchProposalSchema

    class OneStringAssumption(DeterministicCalls):
        def __call__(self, domain, prompt, stage, cwd):
            if stage != ResearchState.INDEPENDENT_RESEARCH:
                return super().__call__(domain, prompt, stage, cwd)
            proposal = _proposal(domain)
            proposal["assumptions"] = "single assumption returned as a bare string, not a list"
            return {
                "output": json.dumps(proposal), "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
                "duration_sec": 1.0, "exit_code": 0, "token_usage": "UNKNOWN",
                "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }

    calls = OneStringAssumption()
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.CROSS_CRITIQUE and not result["human_required"]
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    outputs = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert len(outputs) == 3
    for row in outputs:
        assert row["payload"]["assumptions"] == ["single assumption returned as a bare string, not a list"]
        assert isinstance(row["payload"]["hypothesis"], str)   # a real string field must never be wrapped in a list


def test_8c_coercion_never_masks_a_genuinely_wrong_type(tmp_path, provider_state):
    """The same coercion must still fail closed when a list-typed field is a dict/number, not a string -
    only a bare string is ever widened; nothing else is tolerated."""
    calls = DeterministicCalls(invalid_stage=None)

    class WrongTypeAssumption(DeterministicCalls):
        def __call__(self, domain, prompt, stage, cwd):
            if stage != ResearchState.INDEPENDENT_RESEARCH:
                return super().__call__(domain, prompt, stage, cwd)
            proposal = _proposal(domain)
            proposal["assumptions"] = {"not": "a list or a string"}
            return {
                "output": json.dumps(proposal), "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
                "duration_sec": 1.0, "exit_code": 0, "token_usage": "UNKNOWN",
                "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }

    orch, session, _ = _ready(tmp_path, provider_state, WrongTypeAssumption())
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED and result["human_required"]


def test_9_official_cli_commands_are_read_only_and_subscription_only():
    runner = TriadResearchRunner(executables={"OPENAI": "codex", "ANTHROPIC": "claude", "GOOGLE": "agy"})
    codex, codex_stdin, _ = runner._command("OPENAI", "codex", "p")
    claude, claude_stdin, _ = runner._command("ANTHROPIC", "claude", "p")
    google, google_stdin, _ = runner._command("GOOGLE", "agy", "p")
    assert codex == ["codex", "exec", "--sandbox", "read-only", "-"] and codex_stdin == "p"
    assert claude[:4] == ["claude", "--print", "--output-format", "json"] and claude_stdin == "p"
    assert claude[4:] == ["--tools", "Read,Grep,Glob", "--disallowedTools", "mcp__*",
                          "--permission-prompts", "none", "--setting-sources", "user", "--strict-mcp-config"]
    assert google == ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"]
    google_message = json.loads(google_stdin)
    assert google_message["event"] == "user"
    assert google_message["message"]["content"][0]["text"] == "p"
    assert "accept-edits" not in google
