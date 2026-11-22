"""The console's Antigravity switch (data/antigravity.json): it wins over config.yaml, and turning it on or off moves
the saved roles so they stay valid (three different AIs on; BUILDER != REVIEWER off)."""
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from core import env_utils, role_settings as settings


@pytest.fixture
def switch(monkeypatch, tmp_path):
    monkeypatch.delenv("DIRECTOR_TESTING", raising=False)      # the suite forces Antigravity on otherwise
    path = tmp_path / "antigravity.json"
    monkeypatch.setattr(env_utils, "ANTIGRAVITY_SWITCH", path)
    monkeypatch.setattr(env_utils, "root_config", lambda: {"antigravity": {"enabled": False}})
    monkeypatch.setattr(settings, "PATH", tmp_path / "roles.json")
    return path


def test_switch_file_wins_over_config(switch):
    assert env_utils.antigravity_enabled() is False                    # config says off
    switch.write_text('{"enabled": true}')
    assert env_utils.antigravity_enabled() is True
    switch.write_text('{"enabled": "yes"}')                           # not a bool: ignored
    assert env_utils.antigravity_enabled() is False


def test_turning_on_gives_antigravity_the_builder_and_off_gives_it_back(switch):
    settings.save({"head": "openai", "builder": "openai", "reviewer": "claude"})
    assert settings.set_antigravity(True) == {"head": "openai", "builder": "antigravity", "reviewer": "claude"}
    assert json.loads(switch.read_text()) == {"enabled": True} and env_utils.antigravity_enabled()
    assert settings.set_antigravity(False) == {"head": "openai", "builder": "openai", "reviewer": "claude"}
    assert env_utils.antigravity_enabled() is False and env_utils.resolve_cli("agy") is None


@pytest.mark.parametrize("roles,three,expected", [
    ({"head": "claude", "builder": "openai", "reviewer": "claude"}, True,
     {"head": "antigravity", "builder": "openai", "reviewer": "claude"}),
    ({"head": "antigravity", "builder": "claude", "reviewer": "openai"}, False,
     {"head": "claude", "builder": "claude", "reviewer": "openai"}),
    ({"head": "claude", "builder": "openai", "reviewer": "antigravity"}, False,
     {"head": "claude", "builder": "openai", "reviewer": "claude"}),
    ({"head": "claude", "builder": "antigravity", "reviewer": "openai"}, True,
     {"head": "claude", "builder": "antigravity", "reviewer": "openai"}),
])
def test_fit_roles(roles, three, expected):
    assert settings.fit_roles(roles, three) == expected


def test_bad_value_changes_nothing(switch):
    settings.save({"head": "openai", "builder": "openai", "reviewer": "claude"})
    with pytest.raises(ValueError):
        settings.set_antigravity("on")
    assert not switch.exists()


@pytest.fixture
def console(switch):
    from companion_server import CompanionHandler
    from dashboard_server import DashboardServer
    server = DashboardServer(("127.0.0.1", 0), CompanionHandler)
    server.bridge_token = "test-token"
    server.bridge = SimpleNamespace(submit=lambda p: {}, list=lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def post(server, body, headers):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("POST", "/ops/antigravity", body=body, headers=dict({"Content-Type": "application/json"}, **headers))
    response = conn.getresponse(); data = json.loads(response.read() or b"{}"); conn.close()
    return response.status, data


def test_console_switch_needs_a_session_and_reports_roles(console, switch):
    settings.save({"head": "openai", "builder": "openai", "reviewer": "claude"})
    assert post(console, '{"enabled": true}', {})[0] == 403
    assert not switch.exists()
    code, data = post(console, '{"enabled": true}', {"Cookie": "director_session=test-token"})
    assert code == 200 and data["enabled"] is True and data["roles"]["builder"] == "antigravity"
    assert "BUILDER=Antigravity" in data["message"]
    code, data = post(console, '{"enabled": false}', {"X-Solomon-Bridge": "test-token"})
    assert code == 200 and data["roles"] == {"head": "openai", "builder": "openai", "reviewer": "claude"}
    assert post(console, '{"enabled": 1}', {"X-Solomon-Bridge": "test-token"})[0] == 400


def test_status_line_does_not_claim_three_ais_when_switched_off(switch):
    from dashboard_i18n import mode_sentence
    statuses = {"OPENAI": "HEALTHY", "ANTHROPIC": "HEALTHY", "GOOGLE": "UNAVAILABLE"}
    assert mode_sentence("NORMAL", statuses) == "Codex와 Claude 모두 정상 사용 가능합니다. (Antigravity 꺼짐)"
    switch.write_text('{"enabled": true}')
    assert mode_sentence("NORMAL", dict(statuses, GOOGLE="HEALTHY")) == "3개 AI 모두 정상 사용 가능합니다."
