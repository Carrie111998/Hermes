from __future__ import annotations

import json
from pathlib import Path

import yaml

from hermes_cli import flight


def _normal_config() -> dict:
    return {
        "model": {"provider": "cliproxyapi", "default": "gpt-5.6-sol"},
        "providers": {
            "cliproxyapi": {
                "base_url": "http://127.0.0.1:8318/v1",
                "key_env": "CLIPROXYAPI_API_KEY",
                "api_mode": "openai_chat_completions",
            }
        },
        "fallback_providers": [
            {"provider": "cliproxyapi", "model": "grok-4.6"},
            {"provider": "nous", "model": "openai/gpt-5.6-terra"},
        ],
        "kanban": {"max_in_progress": 8, "max_in_progress_per_profile": 2},
    }


def test_local_config_replaces_only_routing_and_throttles_kanban():
    local = flight.build_local_config(
        _normal_config(),
        model="qwen3.5:35b-a3b-coding-nvfp4",
        base_url="http://127.0.0.1:11434/v1",
        max_in_progress=2,
    )

    assert local["model"] == {
        "provider": flight.LOCAL_PROVIDER,
        "default": "qwen3.5:35b-a3b-coding-nvfp4",
    }
    assert local["providers"][flight.LOCAL_PROVIDER]["base_url"] == "http://127.0.0.1:11434/v1"
    assert local["fallback_providers"] == []
    assert local["kanban"]["max_in_progress"] == 2
    assert local["kanban"]["max_in_progress_per_profile"] == 1
    assert local["providers"]["cliproxyapi"]["key_env"] == "CLIPROXYAPI_API_KEY"


def test_state_machine_requires_consecutive_failures_and_successes_with_cooldown():
    state = flight.new_state(now=100.0, failure_threshold=3, recovery_threshold=2, cooldown_seconds=10)

    for now in (101.0, 102.0):
        action = flight.observe_connectivity(state, normal_ok=False, local_ok=True, now=now)
        assert action is None
    assert flight.observe_connectivity(state, normal_ok=False, local_ok=True, now=103.0) == "enter"

    state["mode"] = "local"
    state["last_transition_at"] = 103.0
    assert flight.observe_connectivity(state, normal_ok=True, local_ok=True, now=105.0) is None
    assert state["normal_successes"] == 1
    assert flight.observe_connectivity(state, normal_ok=True, local_ok=True, now=106.0) is None
    assert flight.observe_connectivity(state, normal_ok=True, local_ok=True, now=114.0) == "exit"


def test_partial_connectivity_never_counts_as_normal_recovery():
    state = flight.new_state(now=1.0, failure_threshold=2, recovery_threshold=2, cooldown_seconds=0)
    state["mode"] = "local"

    assert flight.observe_connectivity(state, normal_ok=False, local_ok=True, now=2.0) is None
    assert flight.observe_connectivity(state, normal_ok=True, local_ok=True, now=3.0) is None
    assert flight.observe_connectivity(state, normal_ok=False, local_ok=True, now=4.0) is None
    assert state["normal_successes"] == 0


def test_task_porting_is_conservative_and_restore_is_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        safe = kb.create_task(conn, title="Refactor parser", body="Edit local files and run pytest", assignee="builder")
        network = kb.create_task(conn, title="Deploy API", body="Use GitHub and AWS production", assignee="builder")
        pinned = kb.create_task(
            conn,
            title="Local pinned",
            body="Run local tests",
            assignee="builder",
            model_override="existing-model",
            provider_override="existing-provider",
        )

        saved = flight.port_queued_tasks(
            conn,
            local_model="qwen3.5:35b-a3b-coding-nvfp4",
            local_provider=flight.LOCAL_PROVIDER,
        )
        assert saved[safe] == {"model": None, "provider": None}
        assert saved[pinned] == {"model": "existing-model", "provider": "existing-provider"}
        assert network not in saved
        assert kb.get_task(conn, safe).model_override == "qwen3.5:35b-a3b-coding-nvfp4"
        assert kb.get_task(conn, network).model_override is None

        flight.restore_task_overrides(conn, saved)
        assert kb.get_task(conn, safe).model_override is None
        assert kb.get_task(conn, pinned).model_override == "existing-model"
        assert kb.get_task(conn, pinned).provider_override == "existing-provider"


def test_enter_and_exit_restore_exact_config_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    original = "# keep me\nmodel:\n  provider: cliproxyapi\n  default: gpt-5.6-sol\n"
    config_path.write_text(original)

    manager = flight.FlightManager(home=tmp_path, probe=lambda *_args, **_kwargs: (True, "ok"))
    manager.enter(now=10.0, port_tasks=False)
    assert yaml.safe_load(config_path.read_text())["model"]["provider"] == flight.LOCAL_PROVIDER
    status = manager.status(probe=False)
    assert status["mode"] == "local"
    assert status["saved_restore_target"]["profiles"] == [str(tmp_path)]

    manager.exit(now=20.0)
    assert config_path.read_text() == original
    assert manager.status(probe=False)["mode"] == "online"


def test_tick_transitions_online_local_online_using_real_inference_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_normal_config(), sort_keys=False))
    results = iter([
        (False, "normal inference failed"),
        (False, "normal inference failed"),
        (True, "normal inference ok"),
        (True, "normal inference ok"),
    ])

    def probe(kind, *_args, **_kwargs):
        if kind == "local":
            return True, "local inference ok"
        return next(results)

    manager = flight.FlightManager(
        home=tmp_path,
        probe=probe,
        failure_threshold=2,
        recovery_threshold=2,
        cooldown_seconds=0,
    )
    assert manager.tick(now=1.0, port_tasks=False)["mode"] == "online"
    assert manager.tick(now=2.0, port_tasks=False)["mode"] == "local"
    assert manager.tick(now=3.0, port_tasks=False)["mode"] == "local"
    assert manager.tick(now=4.0, port_tasks=False)["mode"] == "online"

    persisted = json.loads((tmp_path / "flight-mode" / "state.json").read_text())
    assert persisted["last_evidence"]["normal"]["detail"] == "normal inference ok"
    assert persisted["last_transition"]["to"] == "online"
