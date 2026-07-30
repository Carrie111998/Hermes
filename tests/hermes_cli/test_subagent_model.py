from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from hermes_cli import subagent_model


def test_status_treats_partial_legacy_provider_as_inheritance(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"provider": "openrouter", "max_spawn_depth": 2}},
    )

    status = subagent_model.get_subagent_model_status()

    assert status.inherits_parent is True
    assert status.model is None
    assert status.provider is None


def test_reset_preserves_unrelated_delegation_settings(monkeypatch):
    config = {
        "delegation": {
            "model": "old-model",
            "provider": "old-provider",
            "max_spawn_depth": 3,
            "max_concurrent_children": 2,
        }
    }
    saved = []
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config.copy())
    monkeypatch.setattr("hermes_cli.config.save_config", lambda value: saved.append(value))

    status = subagent_model.reset_subagent_model()

    assert status.inherits_parent is True
    assert saved == [
        {"delegation": {"max_spawn_depth": 3, "max_concurrent_children": 2}}
    ]


def test_set_uses_canonical_switch_pipeline_then_saves_normalized_pair(monkeypatch):
    context = SimpleNamespace(
        current_provider="nous",
        current_model="Hermes-4",
        current_base_url="https://inference.example/v1",
        user_providers={"local": {}},
        custom_providers={"custom": {}},
    )
    calls = []
    saved = []
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: context)

    def fake_switch_model(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            new_model="anthropic/claude-sonnet-4",
            target_provider="openrouter",
            error_message=None,
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", fake_switch_model)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"max_spawn_depth": 2}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda value: saved.append(value))

    status = subagent_model.set_subagent_model("sonnet", provider="openrouter")

    assert status == subagent_model.SubagentModelStatus(
        model="anthropic/claude-sonnet-4",
        provider="openrouter",
        inherits_parent=False,
    )
    assert calls == [
        {
            "raw_input": "sonnet",
            "current_provider": "nous",
            "current_model": "Hermes-4",
            "current_base_url": "https://inference.example/v1",
            "current_api_key": "",
            "is_global": False,
            "explicit_provider": "openrouter",
            "user_providers": {"local": {}},
            "custom_providers": {"custom": {}},
        }
    ]
    assert saved == [
        {
            "delegation": {
                "max_spawn_depth": 2,
                "model": "anthropic/claude-sonnet-4",
                "provider": "openrouter",
            }
        }
    ]


def test_failed_resolution_does_not_write_config(monkeypatch):
    context = SimpleNamespace(
        current_provider="nous",
        current_model="Hermes-4",
        current_base_url="",
        user_providers={},
        custom_providers={},
    )
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: context)
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: SimpleNamespace(
            success=False,
            error_message="Provider is not authenticated",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda _value: pytest.fail("failed model selection must not persist"),
    )

    with pytest.raises(ValueError, match="not authenticated"):
        subagent_model.set_subagent_model("private-model", provider="missing")


def test_full_picker_selection_capture_is_thread_local():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from hermes_cli.auth import capture_model_selection, record_model_selection

    barrier = Barrier(2)

    def capture_one(model_id: str) -> list[str]:
        with capture_model_selection() as selections:
            barrier.wait()
            record_model_selection(model_id)
            barrier.wait()
            return list(selections)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(capture_one, ("terra-model", "moon-model")))

    assert results == [["terra-model"], ["moon-model"]]


def _memory_config(monkeypatch, initial):
    import copy

    state = copy.deepcopy(initial)

    def load_config():
        return copy.deepcopy(state)

    def save_config(value):
        state.clear()
        state.update(copy.deepcopy(value))

    monkeypatch.setattr("hermes_cli.config.load_config", load_config)
    monkeypatch.setattr("hermes_cli.config.save_config", save_config)
    return state


def _stub_auth_restore(monkeypatch):
    from hermes_cli import fallback_cmd

    restored = []
    monkeypatch.setattr(fallback_cmd, "_snapshot_auth_active_provider", lambda: "parent-auth")
    monkeypatch.setattr(
        fallback_cmd,
        "_restore_auth_active_provider",
        lambda value: restored.append(value),
    )
    return restored


def test_full_picker_keeps_setup_side_effects_and_restores_primary(monkeypatch):
    import copy

    from hermes_cli import auth, main as hermes_main

    primary = {"default": "same-model", "provider": "parent-provider"}
    state = _memory_config(
        monkeypatch,
        {"model": primary, "delegation": {"max_spawn_depth": 2}},
    )
    restored_auth = _stub_auth_restore(monkeypatch)
    set_calls = []
    pinned = subagent_model.SubagentModelStatus(
        "same-model", "openrouter", False
    )

    def fake_full_picker():
        # Explicitly selecting the same model still counts as a selection.
        auth._save_model_choice("same-model")
        updated = copy.deepcopy(state)
        updated["model"]["provider"] = "openrouter"
        updated["providers"] = {"new-provider": {"api_key": "${NEW_KEY}"}}
        from hermes_cli.config import save_config

        save_config(updated)

    monkeypatch.setattr(hermes_main, "select_provider_and_model", fake_full_picker)
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda model, provider=None: set_calls.append((model, provider)) or pinned,
    )

    assert subagent_model.select_subagent_model_interactively() == pinned
    assert set_calls == [("same-model", "openrouter")]
    assert state["model"] == primary
    assert state["providers"] == {"new-provider": {"api_key": "${NEW_KEY}"}}
    assert restored_auth == ["parent-auth"]


def test_full_picker_cancel_keeps_setup_changes_without_pinning(monkeypatch):
    import copy

    from hermes_cli import main as hermes_main

    primary = {"default": "parent-model", "provider": "parent-provider"}
    state = _memory_config(monkeypatch, {"model": primary})
    restored_auth = _stub_auth_restore(monkeypatch)

    def fake_cancelled_picker():
        updated = copy.deepcopy(state)
        updated["providers"] = {"authenticated-only": {"key_env": "AUTH_KEY"}}
        from hermes_cli.config import save_config

        save_config(updated)

    monkeypatch.setattr(hermes_main, "select_provider_and_model", fake_cancelled_picker)
    assert subagent_model.select_subagent_model_interactively() is None
    assert state["model"] == primary
    assert state["providers"] == {"authenticated-only": {"key_env": "AUTH_KEY"}}
    assert restored_auth == ["parent-auth"]


def test_full_picker_adds_custom_provider_and_pins_canonical_slug(monkeypatch):
    import copy

    from hermes_cli import auth, main as hermes_main

    primary = {"default": "parent-model", "provider": "parent-provider"}
    state = _memory_config(monkeypatch, {"model": primary})
    restored_auth = _stub_auth_restore(monkeypatch)
    set_calls = []
    pinned = subagent_model.SubagentModelStatus(
        "moon-model", "custom:terra-to-moon", False
    )

    def fake_custom_picker():
        auth._save_model_choice("moon-model")
        updated = copy.deepcopy(state)
        updated["model"].update(
            {
                "provider": "custom",
                "base_url": "https://moon.example/v1/",
                "api_mode": "chat_completions",
            }
        )
        updated["custom_providers"] = [
            {
                "name": "Terra to Moon",
                "base_url": "https://moon.example/v1",
                "model": "moon-model",
            }
        ]
        from hermes_cli.config import save_config

        save_config(updated)

    monkeypatch.setattr(hermes_main, "select_provider_and_model", fake_custom_picker)
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda model, provider=None: set_calls.append((model, provider)) or pinned,
    )

    assert subagent_model.select_subagent_model_interactively() == pinned
    assert set_calls == [("moon-model", "custom:terra-to-moon")]
    assert state["model"] == primary
    custom_providers = cast(list[dict[str, Any]], state["custom_providers"])
    assert custom_providers[0]["name"] == "Terra to Moon"
    assert restored_auth == ["parent-auth"]


def test_shell_interactive_cancel_reports_cancelled(monkeypatch, capsys):
    from hermes_cli import main as hermes_main

    monkeypatch.setattr(hermes_main, "_require_tty", lambda _command: None)
    monkeypatch.setattr(
        subagent_model,
        "select_subagent_model_interactively",
        lambda **_kwargs: None,
    )

    hermes_main.cmd_subagent(
        SimpleNamespace(
            subagent_command="model",
            model=None,
            provider=None,
            reset=False,
            refresh=False,
        )
    )

    output = capsys.readouterr().out
    assert "selection cancelled" in output
    assert "Selected subagent model" not in output


def test_shell_positional_reset_restores_parent_inheritance(monkeypatch, capsys):
    from hermes_cli import main as hermes_main

    inherited = subagent_model.SubagentModelStatus(
        model=None,
        provider=None,
        inherits_parent=True,
    )
    reset_calls = []
    monkeypatch.setattr(
        subagent_model,
        "reset_subagent_model",
        lambda: reset_calls.append(True) or inherited,
    )
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda *_args, **_kwargs: pytest.fail("'reset' must not be pinned as a model"),
    )

    hermes_main.cmd_subagent(
        SimpleNamespace(
            subagent_command="model",
            model="reset",
            provider=None,
            reset=False,
            refresh=False,
        )
    )

    assert reset_calls == [True]
    assert "inherits parent" in capsys.readouterr().out


def test_tui_gateway_rpc_uses_shared_subagent_core(monkeypatch):
    from tui_gateway import server

    inherited = subagent_model.SubagentModelStatus(model=None, provider=None, inherits_parent=True)
    pinned = subagent_model.SubagentModelStatus(model="canon", provider="target", inherits_parent=False)
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(subagent_model, "get_subagent_model_status", lambda: inherited)
    monkeypatch.setattr(subagent_model, "reset_subagent_model", lambda: inherited)
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda model, provider=None: calls.append((model, provider)) or pinned,
    )

    handler = server._methods["delegation.model"]
    assert handler("read", {})["result"] == {
        "model": None,
        "provider": None,
        "inherits_parent": True,
    }
    assert handler("set", {"model": "alias", "provider": "target"})["result"] == {
        "model": "canon",
        "provider": "target",
        "inherits_parent": False,
    }
    assert calls == [("alias", "target")]
    assert handler("reset", {"reset": True})["result"]["inherits_parent"] is True
