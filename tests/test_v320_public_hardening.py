"""v3.2.0 (2026-11-20) - fixes for the public review of the release:
billing variables stripped by rule, scope caps no longer waived by default, referenced documents confined,
Antigravity opt-in, Claude roles read-only, console commands need a session."""
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from core import autopilot, env_utils
from core.builder_context import referenced_documents
from schemas.order import OrderSchema


# ------------------------------------------------------------------ billing variables
def test_billing_variables_are_stripped_by_rule(monkeypatch):
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "OPENAI_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("DIRECTOR_KEEP_ME", "1")
    env = env_utils.get_sanitized_env()
    assert not [k for k in env if env_utils.is_billing_variable(k)]
    assert env["DIRECTOR_KEEP_ME"] == "1" and "PATH" in {k.upper() for k in env}


def test_ordinary_variables_are_not_billing_variables():
    for name in ("PATH", "HOME", "API_KEYBOARD", "MY_API_KEY_FILE", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert not env_utils.is_billing_variable(name), name


# ------------------------------------------------------------------ scope caps
CAPS = ["Max files changed exceeded: 20 > 15", "Max diff lines exceeded: 1029 > 800",
        "Dependency configuration modified: requirements.txt", "Permission configuration modified: .claude/settings.json"]


def test_caps_stop_the_run_unless_the_operator_opted_in():
    assert autopilot.split_post_build(CAPS) == {"serious": CAPS, "waivable": [], "repairable": []}
    assert autopilot.split_post_build(CAPS, waive_caps=True)["waivable"] == CAPS


def test_configuration_changes_reach_the_user_and_size_overflows_replan():
    assert autopilot.run_stop_needs_user("Scope violation: " + CAPS[2])
    assert autopilot.run_stop_needs_user("Scope violation: " + CAPS[3])
    assert not autopilot.run_stop_needs_user("Scope violation: " + CAPS[0])


# ------------------------------------------------------------------ referenced documents
def test_document_outside_the_allowed_folders_is_refused(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "elsewhere"
    project.mkdir(); outside.mkdir()
    inside_doc, outside_doc = project / "spec.md", outside / "notes.md"
    inside_doc.write_text("SPEC", encoding="utf-8"); outside_doc.write_text("PRIVATE", encoding="utf-8")
    assert referenced_documents(OrderSchema(goal="read", reference_paths=[str(inside_doc)]), [project]) == {str(inside_doc): "SPEC"}
    with pytest.raises(ValueError, match="reference_roots"):
        referenced_documents(OrderSchema(goal="read", reference_paths=[str(outside_doc)]), [project])
    with pytest.raises(ValueError, match="reference_roots"):      # a path that only appears in the goal text
        referenced_documents(OrderSchema(goal="summarise " + str(outside_doc) + " please"), [project])
    assert referenced_documents(OrderSchema(goal="read", reference_paths=[str(outside_doc)]), [project, outside])


# ------------------------------------------------------------------ Antigravity opt-in
def test_antigravity_is_never_discovered_when_switched_off(monkeypatch):
    monkeypatch.delenv("DIRECTOR_TESTING", raising=False)
    monkeypatch.setattr(env_utils, "root_config", lambda: {"antigravity": {"enabled": False}})
    monkeypatch.setattr(env_utils.shutil, "which", lambda name: "C:/bin/" + name + ".exe")
    assert env_utils.antigravity_enabled() is False
    assert env_utils.resolve_cli("agy") is None
    monkeypatch.setattr(env_utils, "root_config", lambda: {})
    assert env_utils.antigravity_enabled() is True              # a config without the key keeps the old behaviour


# ------------------------------------------------------------------ Claude roles are read-only
def test_claude_roles_get_read_tools_only():
    from core.research_runner import TriadResearchRunner
    flags = env_utils.CLAUDE_READ_ONLY_FLAGS
    assert flags[flags.index("--tools") + 1] == "Read,Glob,Grep"
    assert flags[flags.index("--permission-mode") + 1] == "default"
    assert "--strict-mcp-config" in flags
    command = TriadResearchRunner._command(SimpleNamespace(), "ANTHROPIC", "claude", "p")[0]
    assert command[-len(flags):] == flags
    for word in ("Bash", "Write", "Edit", "bypassPermissions", "dangerously"):
        assert word not in " ".join(command)


# ------------------------------------------------------------------ console session
@pytest.fixture
def console():
    from companion_server import CompanionHandler
    from dashboard_server import DashboardServer
    server = DashboardServer(("127.0.0.1", 0), CompanionHandler)
    server.bridge_token = "test-token"
    server.bridge = SimpleNamespace(submit=lambda p: {"status": "submitted"}, list=lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def call(server, method, path, body="", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse(); data = response.read()
    out = (response.status, data, dict(response.getheaders()))
    conn.close(); return out


FORM = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
STATE_CHANGING = ["/chat", "/project", "/run/approve", "/run/reject", "/ops/mission/decide", "/result/apply",
                  "/result/discard", "/chat/new", "/providers/refresh"]


@pytest.mark.parametrize("path", STATE_CHANGING)
def test_local_program_without_a_session_cannot_send_commands(console, path):
    code, body, _ = call(console, "POST", path, "message=hello&run_id=x&path=C%3A%2Fx", FORM)
    assert code == 403 and "연결" in json.loads(body)["error"]


def test_token_is_not_handed_to_unauthenticated_callers(console):
    code, body, _ = call(console, "GET", "/ops/roles")
    assert code == 200 and json.loads(body)["csrf"] is None
    assert call(console, "GET", "/")[0] == 403 and call(console, "GET", "/classic")[0] == 403
    assert b"test-token" not in call(console, "GET", "/")[1]


def test_login_link_unlocks_this_browser_once(console):
    assert call(console, "GET", "/?token=wrong")[0] == 403
    code, _, headers = call(console, "GET", "/?token=test-token")
    cookie = headers["Set-Cookie"]
    assert code == 303 and headers["Location"] == "/" and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    session = {"Cookie": cookie.split(";")[0]}
    code, body, _ = call(console, "GET", "/", headers=session)
    assert code == 200 and "AI DEV".encode() in body
    assert json.loads(call(console, "GET", "/ops/roles", headers=session)[1])["csrf"] == "test-token"
    assert call(console, "POST", "/providers/refresh", "", dict(FORM, **session))[0] != 403
    foreign = dict(FORM, Origin="https://evil.example", **session)      # a session never excuses a foreign origin
    assert call(console, "POST", "/providers/refresh", "", foreign)[0] == 403
