"""Integration coverage for the installed profile Team JSON-RPC handlers."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tui_gateway import server


TEAM = {
    "id": "crew",
    "name": "Crew",
    "lead": "lead",
    "members": ["lead", "slow", "fast"],
}


def request(method: str, params: dict | None = None) -> dict:
    return server.handle_request({"jsonrpc": "2.0", "id": "test", "method": method, "params": params or {}})


class FakeRegistry:
    teams: dict[str, dict] = {TEAM["id"]: dict(TEAM, members=list(TEAM["members"]))}

    def list(self):
        return [dict(team, members=list(team["members"])) for team in self.teams.values()]

    def get(self, team_id):
        team = self.teams.get(team_id)
        return dict(team, members=list(team["members"])) if team else None

    def create(self, *, team_id, name, lead, members):
        team = {"id": team_id, "name": name, "lead": lead, "members": list(members)}
        self.teams[team_id] = team
        return dict(team, members=list(members))

    def update(self, team_id, *, name, lead, members):
        return self.create(team_id=team_id, name=name, lead=lead, members=members)

    def delete(self, team_id):
        return self.teams.pop(team_id, None) is not None


class FakeDispatcher:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.behavior:
            return self.behavior(**kwargs)
        return SimpleNamespace(
            state="completed",
            text=f"reply:{kwargs['profile']}",
            error=None,
            session_id=f"session:{kwargs['profile']}",
            duration_ms=7,
        )


@pytest.fixture
def rpc(monkeypatch):
    import hermes_cli.profile_peer as profile_peer
    import hermes_cli.profile_teams as profile_teams
    import hermes_cli.profiles as profiles

    FakeRegistry.teams = {TEAM["id"]: dict(TEAM, members=list(TEAM["members"]))}
    dispatcher = FakeDispatcher()
    events = []
    monkeypatch.setattr(profile_teams, "ProfileTeamRegistry", FakeRegistry)
    monkeypatch.setattr(profile_peer, "get_profile_peer_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in TEAM["members"])
    monkeypatch.setattr(server, "_broadcast_global_event", lambda event, payload: events.append((event, payload)))
    return SimpleNamespace(dispatcher=dispatcher, events=events, monkeypatch=monkeypatch)


def assert_4xx(response: dict, code: int | None = None) -> None:
    assert "error" in response
    assert 4000 <= response["error"]["code"] < 5000
    if code is not None:
        assert response["error"]["code"] == code


def test_handlers_are_installed_and_team_crud_smoke(rpc) -> None:
    for method in (
        "profiles.team_list",
        "profiles.team_upsert",
        "profiles.team_delete",
        "profiles.peer_call",
        "profiles.peer_fanout",
    ):
        assert method in server._methods

    listed = request("profiles.team_list")["result"]["teams"]
    assert listed == [TEAM]

    created = request(
        "profiles.team_upsert",
        {"team": {"id": "pair", "name": "Pair", "lead": "lead", "members": ["lead", "fast"]}},
    )
    assert created["result"]["team"]["id"] == "pair"
    updated = request(
        "profiles.team_upsert",
        {"team": {"id": "pair", "name": "Renamed", "lead": "fast", "members": ["lead", "fast"]}},
    )
    assert updated["result"]["team"]["name"] == "Renamed"
    assert request("profiles.team_delete", {"team_id": "pair"})["result"] == {"deleted": True}


def test_peer_call_rejects_self_nonlead_nonmember_and_missing_profile(rpc, monkeypatch) -> None:
    base = {"team_id": "crew", "from_profile": "lead", "to_profile": "fast", "message": "hello"}

    assert_4xx(request("profiles.peer_call", {**base, "to_profile": "lead"}), 4079)
    assert_4xx(request("profiles.peer_call", {**base, "from_profile": "slow"}), 4070)
    assert_4xx(request("profiles.peer_call", {**base, "to_profile": "outsider"}), 4071)

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name != "fast")
    assert_4xx(request("profiles.peer_call", base), 4080)
    assert rpc.dispatcher.calls == []


def test_peer_call_validates_missing_team_message_and_numeric_timeout(rpc) -> None:
    assert_4xx(
        request("profiles.peer_call", {"team_id": "missing", "from_profile": "lead", "to_profile": "fast", "message": "x"}),
        4069,
    )
    assert_4xx(
        request("profiles.peer_call", {"team_id": "crew", "from_profile": "lead", "to_profile": "fast", "message": ""}),
        4072,
    )
    assert_4xx(
        request(
            "profiles.peer_call",
            {"team_id": "crew", "from_profile": "lead", "to_profile": "fast", "message": "x", "timeout": "nope"},
        ),
        4081,
    )


def test_fanout_default_targets_every_member_once_including_lead(rpc) -> None:
    response = request(
        "profiles.peer_fanout",
        {"team_id": "crew", "from_profile": "lead", "message": "hello", "turn_id": "turn"},
    )

    results = response["result"]["results"]
    assert [item["author_profile"] for item in results] == TEAM["members"]
    assert [call["profile"] for call in rpc.dispatcher.calls].count("lead") == 1
    assert sorted(call["profile"] for call in rpc.dispatcher.calls) == sorted(TEAM["members"])
    assert len(rpc.events) == len(TEAM["members"])


@pytest.mark.parametrize("targets", ["fast", {"fast": True}, ["fast", 1], 7])
def test_fanout_rejects_invalid_target_types_with_4xx(rpc, targets) -> None:
    response = request(
        "profiles.peer_fanout",
        {"team_id": "crew", "from_profile": "lead", "message": "hello", "targets": targets},
    )
    assert_4xx(response, 4082)
    assert rpc.dispatcher.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"timeout": "later"},
        {"max_concurrency": "many"},
        {"timeout": object()},
        {"max_concurrency": {"many": True}},
    ],
)
def test_fanout_rejects_invalid_numeric_params_with_4xx(rpc, params) -> None:
    response = request(
        "profiles.peer_fanout",
        {"team_id": "crew", "from_profile": "lead", "message": "hello", "targets": ["fast"], **params},
    )
    assert_4xx(response, 4084)
    assert rpc.dispatcher.calls == []


def test_fanout_results_preserve_requested_order_not_completion_order(rpc) -> None:
    def out_of_order(**kwargs):
        if kwargs["profile"] == "slow":
            time.sleep(0.04)
        return SimpleNamespace(
            state="completed",
            text=kwargs["profile"],
            error=None,
            session_id="session",
            duration_ms=1,
        )

    rpc.dispatcher.behavior = out_of_order
    response = request(
        "profiles.peer_fanout",
        {
            "team_id": "crew",
            "from_profile": "lead",
            "message": "hello",
            "targets": ["slow", "fast", "lead"],
            "max_concurrency": 3,
        },
    )
    assert [item["author_profile"] for item in response["result"]["results"]] == ["slow", "fast", "lead"]


def test_partial_exception_has_stable_task_and_exactly_one_event_per_target(rpc) -> None:
    def partly_broken(**kwargs):
        if kwargs["profile"] == "slow":
            raise RuntimeError("secret failure")
        return SimpleNamespace(
            state="completed",
            text="ok",
            error=None,
            session_id="private-session",
            duration_ms=2,
        )

    rpc.dispatcher.behavior = partly_broken
    response = request(
        "profiles.peer_fanout",
        {"team_id": "crew", "from_profile": "lead", "message": "hello", "targets": ["slow", "fast"]},
    )
    results = response["result"]["results"]
    assert [item["state"] for item in results] == ["failed", "completed"]
    assert len(rpc.events) == 2
    assert {payload["author_profile"] for _, payload in rpc.events} == {"slow", "fast"}
    for result in results:
        event_payloads = [payload for _, payload in rpc.events if payload["author_profile"] == result["author_profile"]]
        assert len(event_payloads) == 1
        assert event_payloads[0]["task_id"] == result["task_id"]
        assert "session_id" not in event_payloads[0]


def test_publish_false_suppresses_success_and_failure_events(rpc) -> None:
    params = {
        "team_id": "crew",
        "from_profile": "lead",
        "message": "hello",
        "targets": ["fast"],
        "publish": False,
    }
    success = request("profiles.peer_fanout", params)
    assert success["result"]["results"][0]["state"] == "completed"

    rpc.dispatcher.behavior = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    failure = request("profiles.peer_fanout", params)
    assert failure["result"]["results"][0]["state"] == "failed"
    assert rpc.events == []


def test_output_is_redacted_truncated_and_event_omits_session_id(rpc, monkeypatch) -> None:
    from plugins.platforms.a2a import security

    monkeypatch.setattr(security, "filter_inbound", lambda value: value)
    monkeypatch.setattr(security, "audit", lambda *args: None)
    monkeypatch.setattr(security, "redact_outbound", lambda value: value.replace("SECRET", "[redacted]"))
    rpc.dispatcher.behavior = lambda **kwargs: SimpleNamespace(
        state="failed",
        text="SECRET" + ("x" * 100_100),
        error="SECRET" + ("e" * 3_000),
        session_id="private-session",
        duration_ms=3,
    )

    response = request(
        "profiles.peer_call",
        {"team_id": "crew", "from_profile": "lead", "to_profile": "fast", "message": "hello"},
    )
    result = response["result"]
    assert result["truncated"] is True
    assert len(result["text"]) == 100_000
    assert "SECRET" not in result["text"]
    assert len(result["error"]) == 2_000
    assert "SECRET" not in result["error"]
    assert result["session_id"] == "private-session"
    assert len(rpc.events) == 1
    event, payload = rpc.events[0]
    assert event == "team.message"
    assert "session_id" not in payload
    assert payload["task_id"] == result["task_id"]
