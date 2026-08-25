from __future__ import annotations

from tui_gateway import server


class FakeAuth:
    def list_statuses(self, provider):
        return [
            {
                "label": "default",
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
            }
        ]

    def start(self, provider, account_id):
        return {"provider": provider, "account_id": account_id, "session_id": "auth-1", "status": "pending"}

    def poll(self, provider, account_id, session_id):
        return {"status": "pending", "auth_url": "https://example.test", "expects_code": True}

    def submit(self, provider, account_id, session_id, code):
        return {"status": "pending", "code_seen": code}

    def cancel(self, provider, account_id, session_id):
        return {"status": "cancelled"}


def call(method: str, params: dict):
    return server.handle_request({"jsonrpc": "2.0", "id": "r1", "method": method, "params": params})


def test_cli_auth_rpc_lifecycle(monkeypatch):
    monkeypatch.setattr("hermes_cli.cli_auth_broker.CliAuthBroker", FakeAuth)
    accounts = call("auth.cli.accounts", {"provider": "claude-code"})
    assert accounts["result"] == {
        "provider": "claude-code",
        "accounts": [
            {
                "label": "default",
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
            }
        ],
    }
    started = call("auth.cli.start", {"provider": "claude-code", "account_id": "work"})
    assert started["result"]["session_id"] == "auth-1"
    polled = call(
        "auth.cli.poll",
        {"provider": "claude-code", "account_id": "work", "session_id": "auth-1"},
    )
    assert polled["result"] == {
        "status": "pending",
        "auth_url": "https://example.test",
        "expects_code": True,
    }
    submitted = call(
        "auth.cli.submit",
        {
            "provider": "claude-code",
            "account_id": "work",
            "session_id": "auth-1",
            "code": "web-code",
        },
    )
    assert submitted["result"]["status"] == "pending"
    cancelled = call(
        "auth.cli.cancel",
        {"provider": "claude-code", "account_id": "work", "session_id": "auth-1"},
    )
    assert cancelled["result"]["status"] == "cancelled"


def test_cli_auth_rpc_requires_all_scoping_fields(monkeypatch):
    monkeypatch.setattr("hermes_cli.cli_auth_broker.CliAuthBroker", FakeAuth)
    for method in ("auth.cli.poll", "auth.cli.submit", "auth.cli.cancel"):
        response = call(method, {"provider": "claude-code", "account_id": "work"})
        assert response["error"]["code"] == 4003


def test_cli_auth_rpc_runs_off_socket_reader_thread():
    for method in ("auth.cli.accounts", "auth.cli.start", "auth.cli.poll", "auth.cli.submit", "auth.cli.cancel"):
        assert method in server._LONG_HANDLERS


def test_cli_auth_accounts_uses_requested_profile_scope(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    seen_homes = []

    class ScopedAuth(FakeAuth):
        def list_statuses(self, provider):
            from hermes_constants import get_hermes_home

            seen_homes.append(get_hermes_home())
            return super().list_statuses(provider)

    monkeypatch.setattr(
        server,
        "_profile_home",
        lambda profile: profile_home if profile == "work" else None,
    )
    monkeypatch.setattr("hermes_cli.cli_auth_broker.CliAuthBroker", ScopedAuth)

    response = call(
        "auth.cli.accounts",
        {"provider": "claude-code", "profile": "work"},
    )

    assert response["result"]["accounts"][0]["subscriptionType"] == "max"
    assert seen_homes == [profile_home]
