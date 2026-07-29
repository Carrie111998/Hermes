from __future__ import annotations

from types import SimpleNamespace

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


def test_interactive_picker_uses_core_curses_without_inquirer(monkeypatch):
    import builtins

    inherited = subagent_model.SubagentModelStatus(
        model=None,
        provider=None,
        inherits_parent=True,
    )
    pinned = subagent_model.SubagentModelStatus(
        model="model-b",
        provider="provider-a",
        inherits_parent=False,
    )
    answers = iter([1, 1])
    picker_calls = []
    set_calls = []

    monkeypatch.setattr(
        subagent_model,
        "list_subagent_picker_providers",
        lambda **_kwargs: [
            {
                "slug": "provider-a",
                "name": "Provider A",
                "models": ["model-a", "model-b"],
            }
        ],
    )
    monkeypatch.setattr(subagent_model, "get_subagent_model_status", lambda: inherited)
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda model, provider=None: set_calls.append((model, provider)) or pinned,
    )

    def fake_picker(title, items, **kwargs):
        picker_calls.append((title, items, kwargs))
        return next(answers)

    monkeypatch.setattr("hermes_cli.curses_ui.curses_radiolist", fake_picker)
    real_import = builtins.__import__

    def reject_undeclared_picker(name, *args, **kwargs):
        if name == "InquirerPy":
            raise AssertionError("interactive picker must not import undeclared InquirerPy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_undeclared_picker)

    assert subagent_model.select_subagent_model_interactively() == pinned
    assert set_calls == [("model-b", "provider-a")]
    assert [call[0] for call in picker_calls] == [
        "Select subagent provider:",
        "Select subagent model:",
    ]
    assert all(call[2]["searchable"] is True for call in picker_calls)


def test_interactive_picker_first_choice_resets_to_parent(monkeypatch):
    pinned = subagent_model.SubagentModelStatus(
        model="model-a",
        provider="provider-a",
        inherits_parent=False,
    )
    inherited = subagent_model.SubagentModelStatus(None, None, True)
    reset_calls = []

    monkeypatch.setattr(
        subagent_model,
        "list_subagent_picker_providers",
        lambda **_kwargs: [
            {"slug": "provider-a", "name": "Provider A", "models": ["model-a"]}
        ],
    )
    monkeypatch.setattr(subagent_model, "get_subagent_model_status", lambda: pinned)
    monkeypatch.setattr(
        subagent_model,
        "reset_subagent_model",
        lambda: reset_calls.append(True) or inherited,
    )
    monkeypatch.setattr(
        "hermes_cli.curses_ui.curses_radiolist",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda *_args, **_kwargs: pytest.fail("reset must not select a model"),
    )

    assert subagent_model.select_subagent_model_interactively() == inherited
    assert reset_calls == [True]


def test_interactive_picker_cancel_returns_none_without_writing(monkeypatch):
    pinned = subagent_model.SubagentModelStatus(
        model="model-a",
        provider="provider-a",
        inherits_parent=False,
    )
    monkeypatch.setattr(
        subagent_model,
        "list_subagent_picker_providers",
        lambda **_kwargs: [
            {"slug": "provider-a", "name": "Provider A", "models": ["model-a"]}
        ],
    )
    monkeypatch.setattr(subagent_model, "get_subagent_model_status", lambda: pinned)
    monkeypatch.setattr(
        "hermes_cli.curses_ui.curses_radiolist",
        lambda *_args, **_kwargs: -1,
    )
    monkeypatch.setattr(
        subagent_model,
        "reset_subagent_model",
        lambda: pytest.fail("cancel must not reset the override"),
    )
    monkeypatch.setattr(
        subagent_model,
        "set_subagent_model",
        lambda *_args, **_kwargs: pytest.fail("cancel must not select a model"),
    )

    assert subagent_model.select_subagent_model_interactively() is None


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
