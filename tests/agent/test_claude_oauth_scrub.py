"""Tests for Claude OAuth trigger scrubbing across direct and MoA routes."""

from types import SimpleNamespace

from agent import chat_completion_helpers as helpers


def test_scrubs_only_system_messages_for_direct_claude():
    agent = SimpleNamespace(model="claude-sonnet-5", base_url="https://example.test")
    messages = [
        {"role": "system", "content": "Use session_search and skill_view."},
        {"role": "user", "content": "Call session_search literally."},
    ]

    result = helpers._scrub_claude_oauth_triggers(agent, messages)

    assert result[0]["content"] == "Use sess_search and sk_view."
    assert result[1]["content"] == "Call session_search literally."
    assert messages[0]["content"] == "Use session_search and skill_view."


def test_non_claude_non_moa_returns_original_object():
    agent = SimpleNamespace(model="gpt-5.6", base_url="https://example.test")
    messages = [{"role": "system", "content": "session_search"}]

    assert helpers._scrub_claude_oauth_triggers(agent, messages) is messages


def test_moa_preset_detects_claude_reference(monkeypatch):
    agent = SimpleNamespace(model="default", base_url="moa://local")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"moa": {}})
    monkeypatch.setattr(
        "hermes_cli.moa_config.resolve_moa_preset",
        lambda _config, _name: {
            "reference_models": [{"model": "claude-fable-5"}],
            "aggregator": {"model": "gpt-5.6"},
        },
    )

    assert helpers._moa_preset_targets_claude(agent) is True


def test_scrubs_structured_system_blocks(monkeypatch):
    agent = SimpleNamespace(model="default", base_url="moa://local")
    monkeypatch.setattr(helpers, "_moa_preset_targets_claude", lambda _agent: True)
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "Use skill_manage."},
                {"type": "image", "url": "https://example.test/image"},
            ],
        }
    ]

    result = helpers._scrub_claude_oauth_triggers(agent, messages)

    assert result[0]["content"][0]["text"] == "Use sk_manage."
    assert result[0]["content"][1] == messages[0]["content"][1]


def test_media_tag_is_not_in_scrub_table():
    scrubbed_sources = {source for source, _ in helpers._CLAUDE_OAUTH_SCRUB}
    assert "MEDIA:" not in scrubbed_sources


def test_media_directive_survives_direct_claude_scrub():
    agent = SimpleNamespace(model="claude-sonnet-5", base_url="https://example.test")
    messages = [
        {
            "role": "system",
            "content": "Use MEDIA:/absolute/path/to/file and session_search.",
        }
    ]

    result = helpers._scrub_claude_oauth_triggers(agent, messages)

    assert "MEDIA:/absolute/path/to/file" in result[0]["content"]
    assert "M3DIA:" not in result[0]["content"]
    assert "sess_search" in result[0]["content"]


def test_media_directive_survives_structured_system_blocks(monkeypatch):
    agent = SimpleNamespace(model="default", base_url="moa://local")
    monkeypatch.setattr(helpers, "_moa_preset_targets_claude", lambda _agent: True)
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "Use MEDIA:/path/to/file.png."},
            ],
        }
    ]

    result = helpers._scrub_claude_oauth_triggers(agent, messages)

    assert "MEDIA:/path/to/file.png" in result[0]["content"][0]["text"]
