"""Antigravity off (antigravity.enabled: false): Claude and Codex hold the three roles.

The role picker used to demand three different AIs, so with Antigravity switched off no role could be saved, and a
saved Antigravity role made every manual-roles run stop with "Manually selected CLI unavailable"."""
import json

import pytest

from core import role_settings as settings


@pytest.fixture
def two_ai(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "three_ai", lambda: False)
    monkeypatch.setattr(settings, "PATH", tmp_path / "roles.json")
    return tmp_path


@pytest.mark.parametrize("roles", [
    {"head": "openai", "builder": "openai", "reviewer": "claude"},
    {"head": "claude", "builder": "claude", "reviewer": "openai"},
    {"head": "claude", "builder": "openai", "reviewer": "claude"},
])
def test_two_ais_can_hold_the_three_roles(two_ai, roles):
    assert settings.save(roles) == roles
    assert settings.current(settings.apply({})) == roles


@pytest.mark.parametrize("roles,word", [
    ({"head": "openai", "builder": "openai", "reviewer": "openai"}, "BUILDER와 REVIEWER"),
    ({"head": "claude", "builder": "antigravity", "reviewer": "openai"}, "Antigravity가 꺼져"),
])
def test_builder_and_reviewer_differ_and_antigravity_is_refused(two_ai, roles, word):
    with pytest.raises(ValueError, match=word):
        settings.save(roles)
    assert not settings.PATH.exists()


def test_saved_antigravity_role_falls_back_to_config_roles(two_ai):
    settings.PATH.write_text(json.dumps({"head": "openai", "builder": "antigravity", "reviewer": "claude"}))
    base = {"head": {"provider": "codex"}, "builder": {"provider": "codex"}, "reviewer": {"provider": "claude"}}
    cfg = settings.apply(base)
    assert settings.current(cfg) == {"head": "openai", "builder": "openai", "reviewer": "claude"}
    assert "manual_roles" not in cfg


def test_three_ais_still_need_three_different(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "three_ai", lambda: True)
    monkeypatch.setattr(settings, "PATH", tmp_path / "roles.json")
    with pytest.raises(ValueError, match="서로 다른 AI"):
        settings.save({"head": "openai", "builder": "openai", "reviewer": "claude"})


def test_shipped_config_is_two_ai_and_independent():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["antigravity"]["enabled"] is False
    providers = {r: cfg[r]["provider"] for r in ("head", "builder", "reviewer")}
    assert "antigravity" not in providers.values()
    assert providers["reviewer"] not in (providers["head"], providers["builder"])
