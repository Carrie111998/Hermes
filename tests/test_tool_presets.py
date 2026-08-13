"""Unit tests for tool_presets — the per-chat preset CRUD + resolution module.

Focus is the empty-list-vs-None invariant that the whole feature hinges on:
``enabled_toolsets == []`` (chat-only) is a real posture and must never collapse
to ``None`` (profile default / "Full") on any round-trip.
"""

import pytest

import tool_presets
from tool_presets import CHAT_ONLY, FULL


@pytest.fixture
def fake_config(monkeypatch):
    """In-memory stand-in for hermes_cli.config load/save.

    tool_presets imports load_config/save_config lazily inside each writer, so
    patching the module attributes is enough; returns the backing store so a
    test can seed or assert persisted state.
    """
    store = {"cfg": {}}
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: dict(store["cfg"]))

    def _save(cfg):
        store["cfg"] = dict(cfg)

    monkeypatch.setattr(config_mod, "save_config", _save)
    return store


# ── _normalize_list: the [] vs None boundary ─────────────────────────────────

def test_normalize_list_none_stays_none():
    assert tool_presets._normalize_list(None) is None


def test_normalize_list_empty_stays_empty_not_none():
    result = tool_presets._normalize_list([])
    assert result == []
    assert result is not None


def test_normalize_list_strips_and_drops_blanks():
    assert tool_presets._normalize_list([" web ", "", "  ", "memory"]) == ["web", "memory"]


def test_normalize_list_non_list_becomes_none():
    assert tool_presets._normalize_list(42) is None


def test_normalize_list_scalar_string_wraps():
    assert tool_presets._normalize_list("web") == ["web"]


# ── Virtual built-ins ────────────────────────────────────────────────────────

def test_list_presets_includes_both_builtins():
    presets = tool_presets.list_presets(cfg={})
    by_name = {p["name"]: p for p in presets}
    assert CHAT_ONLY in by_name
    assert FULL in by_name
    assert by_name[CHAT_ONLY]["builtin"] is True
    assert by_name[FULL]["builtin"] is True


def test_resolve_chat_only_is_empty_list_not_none():
    resolved = tool_presets.resolve_preset(CHAT_ONLY, cfg={})
    assert resolved is not None
    assert resolved["enabled_toolsets"] == []
    assert resolved["tool_preset"] == CHAT_ONLY


def test_resolve_full_is_none():
    resolved = tool_presets.resolve_preset(FULL, cfg={})
    assert resolved is not None
    assert resolved["enabled_toolsets"] is None


def test_resolve_unknown_and_falsy_return_none():
    assert tool_presets.resolve_preset(None, cfg={}) is None
    assert tool_presets.resolve_preset("", cfg={}) is None
    assert tool_presets.resolve_preset("does-not-exist", cfg={}) is None


# ── User preset round-trips through config ───────────────────────────────────

def test_save_then_resolve_preserves_empty_list(fake_config):
    tool_presets.save_preset({"name": "Lean", "enabled_toolsets": []})
    resolved = tool_presets.resolve_preset("Lean")
    assert resolved is not None
    # The chat-only [] must survive save + reload, not become None.
    assert resolved["enabled_toolsets"] == []


def test_save_maps_config_fields_to_runtime_axes(fake_config):
    tool_presets.save_preset(
        {
            "name": "Custom",
            "enabled_toolsets": ["web"],
            "allowed_tools": ["read_file"],
            "disabled_tools": ["shell"],
            "disabled_skills": ["pdf"],
        }
    )
    resolved = tool_presets.resolve_preset("Custom")
    assert resolved["enabled_toolsets"] == ["web"]
    assert resolved["allowed_tool_names"] == ["read_file"]
    assert resolved["denied_tool_names"] == ["shell"]
    assert resolved["disabled_skills"] == ["pdf"]


def test_save_drops_null_fields_but_keeps_empty_list(fake_config):
    tool_presets.save_preset({"name": "Lean", "enabled_toolsets": []})
    rows = fake_config["cfg"]["tool_presets"]
    row = next(r for r in rows if r["name"] == "Lean")
    assert row["enabled_toolsets"] == []
    # Null axes are not persisted (config stays terse).
    assert "allowed_tools" not in row
    assert "disabled_tools" not in row


def test_save_requires_name(fake_config):
    with pytest.raises(ValueError):
        tool_presets.save_preset({"enabled_toolsets": []})


def test_delete_user_preset_removes_it(fake_config):
    tool_presets.save_preset({"name": "Temp", "enabled_toolsets": ["web"]})
    assert any(p["name"] == "Temp" for p in tool_presets.list_presets())
    tool_presets.delete_preset("Temp")
    assert not any(p["name"] == "Temp" for p in tool_presets.list_presets())


# ── Built-in overrides: editable, non-deletable (delete = reset) ─────────────

def test_builtin_override_is_editable_and_stays_builtin(fake_config):
    tool_presets.save_preset({"name": CHAT_ONLY, "enabled_toolsets": ["web"]})
    by_name = {p["name"]: p for p in tool_presets.list_presets()}
    assert by_name[CHAT_ONLY]["enabled_toolsets"] == ["web"]
    assert by_name[CHAT_ONLY]["builtin"] is True
    # Resolution honors the override.
    assert tool_presets.resolve_preset(CHAT_ONLY)["enabled_toolsets"] == ["web"]


def test_delete_builtin_resets_to_default(fake_config):
    tool_presets.save_preset({"name": CHAT_ONLY, "enabled_toolsets": ["web"]})
    tool_presets.delete_preset(CHAT_ONLY)
    # Built-in still present, back to its default [] posture.
    by_name = {p["name"]: p for p in tool_presets.list_presets()}
    assert CHAT_ONLY in by_name
    assert by_name[CHAT_ONLY]["builtin"] is True
    assert tool_presets.resolve_preset(CHAT_ONLY)["enabled_toolsets"] == []


# ── Default preset (for NEW chats) ───────────────────────────────────────────

def test_default_preset_round_trip_and_clear(fake_config):
    assert tool_presets.get_default_preset() is None
    tool_presets.set_default_preset("Lean")
    assert tool_presets.get_default_preset() == "Lean"
    tool_presets.set_default_preset(None)
    assert tool_presets.get_default_preset() is None


def test_get_default_preset_normalizes_blank_to_none():
    assert tool_presets.get_default_preset(cfg={"default_tool_preset": "   "}) is None
