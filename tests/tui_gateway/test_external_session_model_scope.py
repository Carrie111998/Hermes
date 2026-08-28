"""Cross-process model scope for messaging sessions opened in Desktop."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY

import pytest

from tui_gateway import server


class _DB:
    def __init__(self, row):
        self.row = row

    def get_session(self, _session_id):
        return dict(self.row)


def _result():
    return SimpleNamespace(
        new_model="gpt-5.6-sol",
        target_provider="openai-codex",
        base_url="https://api.openai.com/v1",
    )


def _switch_result():
    return SimpleNamespace(
        success=True,
        new_model="gpt-5.6-sol",
        target_provider="openai-codex",
        base_url="https://api.openai.com/v1",
        api_key="resolved-token",
        api_mode="responses",
        model_info=None,
        warning_message="",
    )


def test_telegram_resume_updates_the_gateway_owned_routing_peer(monkeypatch, tmp_path):
    calls = []
    db = _DB({
        "id": "stored-telegram-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "telegram",
    })
    session: dict[str, Any] = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "stored-telegram-session",
        "source": "telegram",
    }

    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override",
        lambda home, **kwargs: calls.append((home, kwargs)) or {"applied": True},
    )

    server._sync_gateway_owned_model_override(session, _result())

    assert calls == [
        (
            tmp_path,
            {
                "session_key": "agent:main:telegram:dm:7616809568",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "base_url": "https://api.openai.com/v1",
            },
        )
    ]


def test_gateway_owned_switch_fails_closed_when_live_owner_is_unavailable(
    monkeypatch, tmp_path
):
    db = _DB({
        "id": "stored-telegram-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "telegram",
    })
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "stored-telegram-session",
        "source": "telegram",
    }
    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="telegram gateway is unavailable"):
        server._sync_gateway_owned_model_override(session, _result())


def test_gateway_timeout_reports_unknown_outcome_instead_of_unavailable(
    monkeypatch, tmp_path
):
    from gateway.control_socket import GatewayControlTimeoutError

    db = _DB({
        "id": "stored-telegram-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "telegram",
    })
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "stored-telegram-session",
        "source": "telegram",
    }

    def timed_out(*_args, **_kwargs):
        raise GatewayControlTimeoutError("response timed out")

    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override", timed_out
    )

    with pytest.raises(RuntimeError, match="outcome is unknown"):
        server._sync_gateway_owned_model_override(session, _result())


def test_gateway_durability_warning_is_returned_to_the_model_switch_caller(
    monkeypatch, tmp_path
):
    db = _DB({
        "id": "stored-telegram-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "telegram",
    })
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "stored-telegram-session",
        "source": "telegram",
    }
    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override",
        lambda *_args, **_kwargs: {
            "applied": True,
            "durability_warning": "saved transcript record could not be updated",
        },
    )

    response = server._sync_gateway_owned_model_override(session, _result())

    assert response == {
        "applied": True,
        "durability_warning": "saved transcript record could not be updated",
    }


def test_local_desktop_session_does_not_touch_gateway_control(monkeypatch, tmp_path):
    db = _DB({"id": "desktop-session", "session_key": None, "source": "desktop"})
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "desktop-session",
        "source": "desktop",
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("local sessions must not use gateway control")

    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override", unexpected
    )

    server._sync_gateway_owned_model_override(session, _result())


def test_completed_desktop_handoff_updates_the_gateway_owned_peer(
    monkeypatch, tmp_path
):
    calls = []
    db = _DB({
        "id": "desktop-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "telegram",
        "handoff_state": "completed",
        "handoff_platform": "telegram",
    })
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "desktop-session",
        "source": "desktop",
    }
    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override",
        lambda home, **kwargs: calls.append((home, kwargs)) or {"applied": True},
    )

    server._sync_gateway_owned_model_override(session, _result())

    assert calls == [
        (
            tmp_path,
            {
                "session_key": "agent:main:telegram:dm:7616809568",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "base_url": "https://api.openai.com/v1",
            },
        )
    ]


def test_incomplete_desktop_handoff_remains_locally_owned(monkeypatch, tmp_path):
    db = _DB({
        "id": "desktop-session",
        "session_key": "agent:main:telegram:dm:7616809568",
        "source": "desktop",
        "handoff_state": "pending",
        "handoff_platform": "telegram",
    })
    session = {
        "agent": SimpleNamespace(_session_db=db),
        "profile_home": str(tmp_path),
        "session_key": "desktop-session",
        "source": "desktop",
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("an incomplete handoff must remain locally owned")

    monkeypatch.setattr(
        "gateway.control_socket.set_gateway_session_model_override", unexpected
    )

    server._sync_gateway_owned_model_override(session, _result())


def test_telegram_switch_fails_closed_when_peer_row_is_missing(monkeypatch, tmp_path):
    session = {
        "agent": SimpleNamespace(_session_db=_DB({})),
        "profile_home": str(tmp_path),
        "session_key": "stored-telegram-session",
        "source": "telegram",
    }

    with pytest.raises(RuntimeError, match="no live gateway routing key"):
        server._sync_gateway_owned_model_override(session, _result())


def test_guarded_telegram_pick_does_not_reach_gateway_before_confirmation(
    monkeypatch,
):
    agent = SimpleNamespace(
        model="z-ai/glm-5.2",
        provider="openrouter",
        base_url="",
        api_key="old-token",
    )
    session = {"agent": agent, "source": "telegram", "history": []}
    parsed = SimpleNamespace(
        model_input="gpt-5.6-sol",
        explicit_provider="openai-codex",
        is_global=False,
        is_session=True,
        is_once=False,
    )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **_kw: _switch_result())
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.combined_selection_warning",
        lambda *_args, **_kwargs: SimpleNamespace(message="Confirmation required"),
    )
    monkeypatch.setattr(
        server,
        "_sync_gateway_owned_model_override",
        lambda *_args: pytest.fail("unconfirmed picks must not mutate the gateway"),
    )

    response = server._apply_model_switch(
        "telegram-live",
        session,
        "gpt-5.6-sol --provider openai-codex --session",
        parsed_flags=parsed,
        apply_local_agent=False,
    )

    assert response["confirm_required"] is True
    assert agent.model == "z-ai/glm-5.2"


def test_confirmed_telegram_pick_updates_gateway_without_mutating_replay_agent(
    monkeypatch,
):
    calls = []

    def unexpected_switch(**_kwargs):
        raise AssertionError("the busy replay agent must not be switched in place")

    agent = SimpleNamespace(
        model="z-ai/glm-5.2",
        provider="openrouter",
        base_url="",
        api_key="old-token",
        switch_model=unexpected_switch,
    )
    session = {"agent": agent, "source": "telegram", "history": []}
    parsed = SimpleNamespace(
        model_input="gpt-5.6-sol",
        explicit_provider="openai-codex",
        is_global=False,
        is_session=True,
        is_once=False,
    )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **_kw: _switch_result())
    def sync(received, result):
        calls.append((received, result.new_model))
        return {
            "applied": True,
            "durability_warning": "saved transcript record could not be updated",
        }

    monkeypatch.setattr(server, "_sync_gateway_owned_model_override", sync)

    response = server._apply_model_switch(
        "telegram-live",
        session,
        "gpt-5.6-sol --provider openai-codex --session",
        parsed_flags=parsed,
        confirm_expensive_model=True,
        apply_local_agent=False,
    )

    assert response["confirm_required"] is False
    assert response["warning"] == "saved transcript record could not be updated"
    assert calls == [(session, "gpt-5.6-sol")]
    assert agent.model == "z-ai/glm-5.2"


def test_busy_telegram_switch_updates_gateway_before_local_deferral(
    monkeypatch, tmp_path
):
    calls = []
    session: dict[str, Any] = {
        "agent": SimpleNamespace(),
        "session_key": "stored-telegram-session",
        "source": "telegram",
        "running": True,
    }
    server._sessions["telegram-live"] = session

    monkeypatch.setattr(
        server,
        "_gateway_owned_model_target",
        lambda _session: ("telegram", "agent:main:telegram:dm:7616809568", tmp_path),
    )

    def resolve(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "value": "gpt-5.6-sol",
            "warning": "",
            "confirm_required": False,
            "scope": "session",
        }

    monkeypatch.setattr(server, "_apply_model_switch", resolve)
    try:
        response = server.handle_request({
            "id": "1",
            "method": "config.set",
            "params": {
                "session_id": "telegram-live",
                "key": "model",
                "value": "gpt-5.6-sol --provider openai-codex --session",
            },
        })
    finally:
        server._sessions.pop("telegram-live", None)

    assert response is not None
    assert response["result"]["deferred"] is True
    assert calls == [
        {
            "confirm_expensive_model": False,
            "parsed_flags": ANY,
            "apply_local_agent": False,
        }
    ]
    pending = session["pending_model_switch"]
    assert isinstance(pending, dict)
    assert pending["display_model"] == "gpt-5.6-sol"
