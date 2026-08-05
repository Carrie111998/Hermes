"""Regression coverage for local, config-driven compression continuity hooks."""

from types import SimpleNamespace

from agent import conversation_compression as compression
from hermes_cli import config as hermes_config


def test_compression_hook_runs_with_lifecycle_metadata(monkeypatch):
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "compression": {
                "hooks": {
                    "pre": {
                        "command": "/tmp/generate-handoff.sh",
                        "timeout_seconds": 17,
                    }
                }
            }
        },
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(compression.subprocess, "run", fake_run)
    agent = SimpleNamespace(
        session_id="session-current",
        platform="telegram",
        model="gpt-test",
        provider="openai-codex",
        context_compressor=SimpleNamespace(context_length=123456),
    )

    compression._run_compression_hook(
        agent,
        "pre",
        reason="preflight",
        old_session_id="session-old",
        message_count_before=42,
        tokens_before=100000,
        focus_topic="continuity",
    )

    assert captured["command"] == "/tmp/generate-handoff.sh"
    assert captured["shell"] is True
    assert captured["timeout"] == 17
    assert captured["check"] is False
    env = captured["env"]
    assert env["HERMES_COMPRESSION_PHASE"] == "pre"
    assert env["HERMES_COMPRESSION_REASON"] == "preflight"
    assert env["HERMES_SESSION_ID"] == "session-current"
    assert env["HERMES_OLD_SESSION_ID"] == "session-old"
    assert env["HERMES_PLATFORM"] == "telegram"
    assert env["HERMES_MODEL"] == "gpt-test"
    assert env["HERMES_PROVIDER"] == "openai-codex"
    assert env["HERMES_MESSAGE_COUNT_BEFORE"] == "42"
    assert env["HERMES_TOKENS_BEFORE"] == "100000"
    assert env["HERMES_CONTEXT_LENGTH"] == "123456"
    assert env["HERMES_FOCUS_TOPIC"] == "continuity"


def test_handoff_marker_points_to_existing_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    handoff = tmp_path / ".cc-observer" / "handoffs" / "latest.html"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("<html>handoff</html>")

    marker = compression._latest_handoff_marker()

    assert marker is not None
    assert str(handoff) in marker
    assert "CONTEXT COMPRESSION CONTINUITY ARTIFACT" in marker


def test_compression_hook_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"compression": {"hooks": {"post": {"command": "/tmp/nope"}}}},
    )

    def fail(*_args, **_kwargs):
        raise OSError("hook unavailable")

    monkeypatch.setattr(compression.subprocess, "run", fail)

    compression._run_compression_hook(SimpleNamespace(), "post")


def test_handoff_marker_not_real_user():
    from agent.conversation_compression import _is_real_user_message

    msg = {"role": "user", "content": "handoff context", "_handoff_marker_synthetic": True}
    assert not _is_real_user_message(msg)


def test_ensure_compressed_restores_real_user_when_only_handoff_marker_present():
    from agent.conversation_compression import (
        _ensure_compressed_has_user_turn,
        _is_real_user_message,
    )

    # compressed contains only a synthetic handoff-marker user turn — no real user content
    compressed = [
        {"role": "user", "content": "handoff context", "_handoff_marker_synthetic": True},
        {"role": "assistant", "content": "Sure, continuing from the handoff."},
    ]

    # original_messages has a genuine user turn
    original_messages = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
    ]

    _ensure_compressed_has_user_turn(original_messages, compressed)

    assert any(_is_real_user_message(m) for m in compressed), (
        "compressed must contain at least one real user message after restoration"
    )
