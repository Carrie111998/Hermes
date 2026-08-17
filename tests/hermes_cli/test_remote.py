"""CLI coverage for ``hermes remote pair``, ``attach``, and ``sessions``."""

from __future__ import annotations

import argparse

from hermes_cli.config import load_config, save_config
from hermes_cli.subcommands.remote import (
    RemoteConnectionError,
    RemoteHTTPError,
    RemoteTimeoutError,
    _resolve_api_server_key,
    build_remote_parser,
    remote_command,
)


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_remote_parser(subparsers, cmd_remote=remote_command)
    return parser.parse_args(["remote", *argv])


def _successful_remote_responses(method, url, **kwargs):
    if url.endswith("/api/remote/pair"):
        assert method == "POST"
        assert kwargs["payload"] == {"code": "ABC234"}
        return {
            "token": "attach-token",
            "expires_at": "2026-08-17T12:00:00+00:00",
            "ttl_hours": 24,
        }
    assert method == "GET"
    assert url.endswith("/api/remote/sessions")
    assert kwargs["token"] == "attach-token"
    return {
        "hostname": "host-box",
        "profile": "default",
        "sessions": [{"id": "session-1", "title": "One"}],
    }


def test_pair_prints_code(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {
            "code": "JKM234",
            "expires_at": "2026-08-16T12:10:00+00:00",
            "ttl_minutes": 10,
        }

    monkeypatch.setattr(
        "hermes_cli.subcommands.remote._resolve_api_server_key",
        lambda: "local-api-key",
    )
    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", fake_request)

    result = remote_command(_args(["pair"]))

    assert result == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8642/api/remote/pair/code",
            {"token": "local-api-key"},
        )
    ]
    output = capsys.readouterr().out
    assert "JKM234" in output
    assert "2026-08-16T12:10:00+00:00" in output
    assert "10 minutes" in output


def test_pair_key_loads_from_api_server_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    save_config(
        {
            "gateway": {
                "api_server": {
                    "enabled": True,
                    "key": "local-config-key-1234",
                }
            }
        }
    )

    assert _resolve_api_server_key() == "local-config-key-1234"


def test_attach_with_code_saves_config_and_prints_success(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.subcommands.remote._request_json",
        _successful_remote_responses,
    )

    result = remote_command(
        _args(["attach", "host-box:9000", "--code", "abc234", "--name", "lab"])
    )

    assert result == 0
    connection = load_config()["remote"]["connections"]["lab"]
    assert connection == {
        "host": "host-box",
        "port": 9000,
        "token": "attach-token",
        "expires_at": "2026-08-17T12:00:00+00:00",
    }
    output = capsys.readouterr().out
    assert "Connected" in output
    assert "host-box" in output
    assert "default" in output
    assert "1 session" in output


def test_attach_without_code_prompts_and_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda _prompt: "abc234")
    monkeypatch.setattr(
        "hermes_cli.subcommands.remote._request_json",
        _successful_remote_responses,
    )

    result = remote_command(_args(["attach", "host-box"]))

    assert result == 0
    assert "host-box" in load_config()["remote"]["connections"]
    output = capsys.readouterr().out
    assert "Ask the host to run `hermes remote pair` and give you the code:" in output
    assert "Connected" in output


def test_invalid_pairing_code_has_clear_error(monkeypatch, capsys):
    def invalid_code(*_args, **_kwargs):
        raise RemoteHTTPError(401, "Invalid or expired pairing code")

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", invalid_code)

    result = remote_command(_args(["attach", "host-box", "--code", "BAD123"]))

    assert result == 1
    assert (
        "Pairing code invalid or expired, ask the host for a new one"
        in capsys.readouterr().err
    )


def test_unreachable_host_has_clear_error(monkeypatch, capsys):
    def unreachable(*_args, **_kwargs):
        raise RemoteConnectionError("connection refused")

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", unreachable)

    result = remote_command(_args(["attach", "offline:9123", "--code", "ABC234"]))

    assert result == 1
    assert "Cannot reach host at http://offline:9123" in capsys.readouterr().err


def test_timed_out_host_has_clear_error(monkeypatch, capsys):
    def timed_out(*_args, **_kwargs):
        raise RemoteTimeoutError("timed out")

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", timed_out)

    result = remote_command(_args(["attach", "slow-host", "--code", "ABC234"]))

    assert result == 1
    assert "Timed out connecting to host at http://slow-host:8642" in capsys.readouterr().err


def test_connection_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.subcommands.remote._request_json",
        _successful_remote_responses,
    )

    assert remote_command(_args(["attach", "host-box", "--code", "ABC234"])) == 0

    reloaded = load_config()
    assert reloaded["remote"]["connections"]["host-box"] == {
        "host": "host-box",
        "port": 8642,
        "token": "attach-token",
        "expires_at": "2026-08-17T12:00:00+00:00",
    }


def _save_remote_connections(connections):
    save_config({"remote": {"connections": connections}})


def test_sessions_lists_saved_connection_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "lab": {
                "host": "host-box",
                "port": 9000,
                "token": "lab-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            }
        }
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {
            "hostname": "host-box",
            "profile": "research",
            "sessions": [
                {
                    "id": "20260816_123456_abcdef",
                    "title": "Active investigation",
                    "status": "active",
                    "updated_at": "2026-08-16T12:34:56+00:00",
                },
                {
                    "id": "20260815_221500_fedcba",
                    "title": "Waiting room",
                    "status": "idle",
                    "updated_at": "2026-08-15T22:15:00+00:00",
                },
            ],
        }

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", fake_request)

    result = remote_command(_args(["sessions"]))

    assert result == 0
    assert calls == [
        (
            "GET",
            "http://host-box:9000/api/remote/sessions",
            {"token": "lab-token"},
        )
    ]
    output = capsys.readouterr().out
    assert "host-box" in output
    assert "research" in output
    assert "Session ID" in output
    assert "Title" in output
    assert "Status" in output
    assert "Updated" in output
    assert "20260816_123" in output
    assert "20260816_123456_abcdef" not in output
    assert "Active investigation" in output
    assert "active" in output
    assert "Waiting room" in output
    assert "idle" in output
    assert "2026-08-16 12:34" in output


def test_sessions_without_saved_connection_has_attach_hint(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_config({})

    result = remote_command(_args(["sessions"]))

    assert result == 1
    error = capsys.readouterr().err
    assert "No saved remote connection" in error
    assert "hermes remote attach" in error


def test_sessions_expired_token_has_reattach_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "lab": {
                "host": "host-box",
                "port": 9000,
                "token": "expired-token",
                "expires_at": "2026-08-15T12:00:00+00:00",
            }
        }
    )

    def expired(*_args, **_kwargs):
        raise RemoteHTTPError(401, "Invalid or expired attach token")

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", expired)

    result = remote_command(_args(["sessions"]))

    assert result == 1
    assert (
        "Attach token expired or invalid — run `hermes remote attach host-box:9000 "
        "--code ...` again" in capsys.readouterr().err
    )


def test_sessions_unreachable_host_has_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "offline": {
                "host": "offline",
                "port": 9123,
                "token": "attach-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            }
        }
    )

    def unreachable(*_args, **_kwargs):
        raise RemoteConnectionError("connection refused")

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", unreachable)

    result = remote_command(_args(["sessions"]))

    assert result == 1
    assert "Cannot reach host at http://offline:9123" in capsys.readouterr().err


def test_sessions_name_selects_specific_connection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "first": {
                "host": "first-host",
                "port": 8642,
                "token": "first-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            },
            "lab": {
                "host": "lab-host",
                "port": 9443,
                "token": "lab-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            },
        }
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"hostname": "lab-host", "profile": "default", "sessions": []}

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", fake_request)

    result = remote_command(_args(["sessions", "--name", "lab"]))

    assert result == 0
    assert calls == [
        (
            "GET",
            "http://lab-host:9443/api/remote/sessions",
            {"token": "lab-token"},
        )
    ]
    assert "No open sessions." in capsys.readouterr().out


def test_sessions_accepts_connection_name_as_positional(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "lab": {
                "host": "lab-host",
                "port": 9443,
                "token": "lab-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            }
        }
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"hostname": "lab-host", "profile": "default", "sessions": []}

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", fake_request)

    assert remote_command(_args(["sessions", "lab"])) == 0
    assert calls[0][1] == "http://lab-host:9443/api/remote/sessions"
    assert calls[0][2]["token"] == "lab-token"


def test_sessions_defaults_to_most_recent_saved_connection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _save_remote_connections(
        {
            "first": {
                "host": "first-host",
                "port": 9000,
                "token": "first-token",
                "expires_at": "2026-08-17T12:00:00+00:00",
            },
            "latest": {
                "host": "latest-host",
                "port": 8642,
                "token": "latest-token",
                "expires_at": "2026-08-17T13:00:00+00:00",
            },
        }
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"hostname": "latest-host", "profile": "default", "sessions": []}

    monkeypatch.setattr("hermes_cli.subcommands.remote._request_json", fake_request)

    assert remote_command(_args(["sessions"])) == 0
    assert calls == [
        (
            "GET",
            "http://latest-host:8642/api/remote/sessions",
            {"token": "latest-token"},
        )
    ]
