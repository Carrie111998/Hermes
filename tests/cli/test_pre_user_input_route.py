from types import SimpleNamespace

from cli import HermesCLI, _SyntheticInputMessage, _is_real_user_input


def test_cli_input_route_rewrites_before_slash_dispatch(monkeypatch):
    cli = object.__new__(HermesCLI)
    cli.session_id = "cli-session"
    monkeypatch.setattr(
        HermesCLI, "_get_goal_manager", lambda _self: SimpleNamespace(is_active=lambda: False),
    )
    monkeypatch.setattr(
        "hermes_cli.lifecycle.route_pre_user_input",
        lambda **payload: ("/goal ship it", "Routed") if payload["text"] == "ship it" else (payload["text"], None),
    )

    assert cli._route_pre_user_input("ship it") == "/goal ship it"


def test_cli_input_route_skips_active_goal(monkeypatch):
    cli = object.__new__(HermesCLI)
    cli.session_id = "cli-session"
    calls = []
    monkeypatch.setattr(
        HermesCLI, "_get_goal_manager", lambda _self: SimpleNamespace(is_active=lambda: True),
    )
    monkeypatch.setattr(
        "hermes_cli.lifecycle.route_pre_user_input",
        lambda **payload: calls.append(payload),
    )

    assert cli._route_pre_user_input("follow up") == "follow up"
    assert calls == []


def test_cli_input_route_skips_unknown_goal_state(monkeypatch):
    cli = object.__new__(HermesCLI)
    cli.session_id = "cli-session"
    calls = []
    monkeypatch.setattr(HermesCLI, "_get_goal_manager", lambda _self: None)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.route_pre_user_input", lambda **payload: calls.append(payload),
    )

    assert cli._route_pre_user_input("follow up") == "follow up"
    assert calls == []


def test_cli_only_routes_real_user_queue_entries():
    assert _is_real_user_input("typed text") is True
    assert _is_real_user_input(_SyntheticInputMessage("heartbeat prompt")) is False
