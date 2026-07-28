from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import cooldown_remaining, record_cooldown


def _entry(**overrides):
    entry = {
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet",
        "cooldown_seconds": 300,
    }
    entry.update(overrides)
    return entry


def test_recorded_cooldown_survives_module_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert record_cooldown(_entry(), now=1_000) == 1_300
    assert cooldown_remaining(_entry(), now=1_100) == 200
    assert cooldown_remaining(_entry(), now=1_301) == 0


def test_unconfigured_entry_does_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert record_cooldown(_entry(cooldown_seconds=None), now=1_000) == 0
    assert not (tmp_path / "fallback_cooldowns.json").exists()


def test_invalid_or_negative_cooldown_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert record_cooldown(_entry(cooldown_seconds="later"), now=1_000) == 0
    assert record_cooldown(_entry(cooldown_seconds=-10), now=1_000) == 0


def test_cooldown_is_visible_to_another_process(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record_cooldown(_entry(), now=1_000)

    code = (
        "import json; "
        "from agent.fallback_cooldown import cooldown_remaining; "
        f"print(json.dumps(cooldown_remaining({json.dumps(_entry())}, now=1100)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env={**dict(__import__("os").environ), "HERMES_HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == 200


def test_activation_skips_entry_in_persistent_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = _entry(cooldown_seconds=300)
    record_cooldown(entry)
    agent = SimpleNamespace(
        _fallback_chain=[entry],
        _fallback_index=0,
        _fallback_activated=False,
        _primary_runtime={"provider": "openai"},
        _rate_limited_until=0,
        provider="openai",
    )
    agent._try_activate_fallback = lambda reason=None: try_activate_fallback(agent, reason)

    assert try_activate_fallback(agent) is False
    assert agent._fallback_index == 1


def test_rate_limited_active_fallback_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = _entry(cooldown_seconds=120)
    agent = SimpleNamespace(
        _fallback_chain=[entry],
        _fallback_index=1,
        _fallback_activated=True,
        _active_fallback_entry=entry,
        _primary_runtime={"provider": "openai"},
        _rate_limited_until=0,
        provider="openrouter",
    )

    assert try_activate_fallback(agent, FailoverReason.rate_limit) is False
    assert cooldown_remaining(entry) > 100
