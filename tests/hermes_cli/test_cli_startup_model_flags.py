"""Startup --model/--provider survive session boundaries (#74329).

``/new`` (and every wake-word session, which calls ``new_session(silent=True)``)
is a full conversation boundary: session-scoped runtime overrides — ``/model
--session``, ``/fast``, ``/model --once`` — deliberately do not carry forward
(#67979, #48055, #23131).

The startup flags are a different thing. They describe how the process was
launched, so they are the baseline the boundary resets *to*, not an override it
resets away. Before this fix the reset re-derived from ``config.yaml`` only, so
a headless voice appliance started with explicit flags answered every wake turn
on ``model.default`` instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.model_switch import ModelSwitchResult


class _FakeAgent:
    def __init__(self):
        self.model = "startup/model"
        self.provider = "startup-provider"

    def switch_model(self, **kwargs):
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]

    def __getattr__(self, name):
        # new_session pokes a number of agent attributes/methods after the
        # switch; none of them are what this test is about. Return a callable
        # that also tolerates attribute access.
        return _Anything()


class _Anything:
    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        return _Anything()

    def __bool__(self):
        return False


def _make_cli(cli_mod, *, startup_model, startup_provider, current_model):
    stub = SimpleNamespace()
    stub.session_id = "old_session"
    stub.session_start = None
    stub.conversation_history = []
    stub.agent = _FakeAgent()
    stub._session_db = None
    stub._pending_title = None
    stub._resumed = False
    stub._pending_one_turn_model_restore = "leftover"
    stub.reasoning_config = None
    stub.service_tier = None
    stub.model = current_model
    stub.provider = "current-provider"
    stub.requested_provider = "current-provider"
    stub.api_key = "sk-current"
    stub.base_url = "https://current/v1"
    stub._explicit_api_key = "sk-current"
    stub._explicit_base_url = "https://current/v1"
    stub.api_mode = "chat_completions"
    stub._startup_model = startup_model
    stub._startup_provider = startup_provider
    stub._launch_session_boundary_memory_flush = lambda *a, **k: None
    stub._notify_session_boundary = lambda *a, **k: None
    stub._discard_session_if_empty = lambda *a, **k: None
    stub.new_session = cli_mod.HermesCLI.new_session.__get__(stub)
    return stub


@pytest.fixture()
def switch_calls(monkeypatch):
    """Capture what new_session asks the shared switch_model pipeline for."""
    calls = []

    def _fake_switch(**kwargs):
        calls.append(kwargs)
        return ModelSwitchResult(
            success=True,
            new_model=kwargs["raw_input"],
            target_provider=kwargs.get("explicit_provider") or "resolved-provider",
            api_key="sk-new",
            base_url="https://new/v1",
            api_mode="chat_completions",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _fake_switch)
    return calls


@pytest.fixture()
def config_default(monkeypatch):
    import cli as cli_mod

    monkeypatch.setitem(
        cli_mod.CLI_CONFIG, "model",
        {"default": "config/default-model", "provider": "config-provider"},
    )
    return cli_mod


class TestStartupFlagsSurviveNewSession:
    def test_startup_model_wins_over_config_default(self, config_default, switch_calls):
        cli = _make_cli(
            config_default,
            startup_model="startup/model",
            startup_provider=None,
            current_model="something/else",
        )

        cli.new_session(silent=True)

        assert switch_calls, "new_session should have re-derived a model"
        assert switch_calls[-1]["raw_input"] == "startup/model", (
            "a session started with --model must not fall back to model.default"
        )

    def test_startup_provider_is_carried_too(self, config_default, switch_calls):
        cli = _make_cli(
            config_default,
            startup_model="startup/model",
            startup_provider="startup-provider",
            current_model="something/else",
        )

        cli.new_session(silent=True)

        assert switch_calls[-1]["explicit_provider"] == "startup-provider"

    def test_without_startup_flags_config_default_still_wins(
        self, config_default, switch_calls,
    ):
        """The #67979 contract is unchanged for an unflagged launch."""
        cli = _make_cli(
            config_default,
            startup_model=None,
            startup_provider=None,
            current_model="something/else",
        )

        cli.new_session(silent=True)

        assert switch_calls[-1]["raw_input"] == "config/default-model"
        assert switch_calls[-1]["explicit_provider"] == "config-provider"

    def test_session_scoped_override_still_does_not_carry_forward(
        self, config_default, switch_calls,
    ):
        """/model --session picked 'session/override'; /new must drop it.

        It resets to the startup selection, not to the override and not to
        config.yaml.
        """
        cli = _make_cli(
            config_default,
            startup_model="startup/model",
            startup_provider=None,
            current_model="session/override",
        )

        cli.new_session(silent=True)

        assert switch_calls[-1]["raw_input"] == "startup/model"
        assert cli.model == "startup/model"

    def test_one_turn_restore_is_still_cleared(self, config_default, switch_calls):
        cli = _make_cli(
            config_default,
            startup_model="startup/model",
            startup_provider=None,
            current_model="something/else",
        )

        cli.new_session(silent=True)

        assert cli._pending_one_turn_model_restore is None

    def test_no_switch_when_already_on_the_startup_model(
        self, config_default, switch_calls,
    ):
        """Nothing to re-derive — the guard must stay a no-op."""
        cli = _make_cli(
            config_default,
            startup_model="startup/model",
            startup_provider=None,
            current_model="startup/model",
        )

        cli.new_session(silent=True)

        assert not switch_calls


class TestStartupSelectionCapture:
    def test_flags_are_recorded_only_when_passed(self, monkeypatch):
        """An unflagged launch must record nothing, so config keeps winning."""
        import inspect

        import cli as cli_mod

        src = inspect.getsource(cli_mod.HermesCLI.__init__)
        assert "self._startup_model = (model or \"\").strip() or None" in src
        assert "self._startup_provider = (provider or \"\").strip() or None" in src
