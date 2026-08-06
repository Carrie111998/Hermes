"""Tests for the fail-closed read-only Linear OAuth MCP boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

import pytest

from hermes_cli import config as config_module
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_linear_mcp as linear_mcp
from hermes_cli.kanban_mcp_adapters import MCPAdapterError


TEAM_ID = "b70648f0-55b7-4a41-b197-0aadcb47e158"
LINEAR_URL = "https://mcp.linear.app/mcp"


class FakeMCPCaller:
    """Deterministic fake transport keyed by provider tool name."""

    def __init__(self, responses: Mapping[str, list[Any]]) -> None:
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        provider_tool = tool_name.rsplit("__", 1)[-1]
        values = self.responses.get(provider_tool)
        if not values:
            raise AssertionError(f"unexpected fake MCP call: {provider_tool}")
        value = values[0] if len(values) == 1 else values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)


def _config(**overrides: Any) -> linear_mcp.LinearMCPConfig:
    return linear_mcp.LinearMCPConfig(
        server_name=overrides.get("server_name", "linear"),
        provider_timeout_seconds=overrides.get("provider_timeout_seconds", 5),
        retry_attempts=overrides.get("retry_attempts", 3),
        page_size=overrides.get("page_size", 2),
        max_pages=overrides.get("max_pages", 3),
    )


def _adapter(
    responses: Mapping[str, list[Any]],
    *,
    sleeps: list[float] | None = None,
    **config_overrides: Any,
) -> tuple[linear_mcp.LinearMCPReadAdapter, FakeMCPCaller]:
    caller = FakeMCPCaller(responses)
    adapter = linear_mcp.LinearMCPReadAdapter(
        caller,
        config=_config(**config_overrides),
        sleeper=(sleeps.append if sleeps is not None else lambda _: None),
    )
    return adapter, caller


def _empty_caller_factory(
    server_name: str,
    allowed_tools: frozenset[str],
    timeout_seconds: int,
) -> FakeMCPCaller:
    del server_name, allowed_tools, timeout_seconds
    return FakeMCPCaller({})


def _no_tools_register(_servers: dict[str, dict[str, Any]]) -> list[str]:
    return []


@pytest.fixture
def issue_payload() -> dict[str, Any]:
    return {
        "id": "ECH-288",
        "title": "Phase 3: Linear OAuth MCP read path",
        "url": "https://linear.app/echlon/issue/ECH-288/phase-3-linear-oauth-mcp-read-path",
        "updatedAt": "2026-08-05T20:55:15.325Z",
        "status": "In Progress",
        "statusType": "started",
        "labels": ["Priority: Urgent", "Linear"],
        "team": "Echlon",
        "teamId": TEAM_ID,
        "project": "Finish 90-Day Plan",
        "projectId": "project-1",
        "attachments": [
            {
                "id": "attachment-1",
                "title": "feat(kanban): add Linear integration boundary",
                "url": "https://github.com/NousResearch/hermes-agent/pull/471",
            }
        ],
    }


def test_oauth_discovery_registers_only_explicit_read_tools() -> None:
    captured: dict[str, Any] = {}
    caller = FakeMCPCaller({})

    def register(servers: dict[str, dict[str, Any]]) -> list[str]:
        captured["servers"] = deepcopy(servers)
        return [f"mcp__linear__{name}" for name in linear_mcp.LINEAR_MCP_READ_TOOLS]

    def caller_factory(
        server_name: str,
        allowed_tools: frozenset[str],
        timeout_seconds: int,
    ) -> FakeMCPCaller:
        captured["server_name"] = server_name
        captured["allowed_tools"] = allowed_tools
        captured["timeout_seconds"] = timeout_seconds
        return caller

    bundle = linear_mcp.build_linear_mcp_adapter(
        config=_config(),
        mcp_servers={"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        register_servers=register,
        caller_factory=caller_factory,
        sleeper=lambda _: None,
    )

    selected = captured["servers"]["linear"]
    assert selected["auth"] == "oauth"
    assert set(selected["tools"]["include"]) == linear_mcp.LINEAR_MCP_READ_TOOLS
    assert selected["tools"]["prompts"] is False
    assert selected["tools"]["resources"] is False
    assert all("save_" not in name and "delete_" not in name for name in captured["allowed_tools"])
    assert bundle.oauth_configured is True
    assert bundle.write_enabled is False
    assert len(bundle.registered_read_tools) == len(linear_mcp.LINEAR_MCP_READ_TOOLS)


def test_issue_read_normalizes_identity_state_labels_links_and_revision(
    issue_payload: dict[str, Any],
) -> None:
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    snapshot = adapter.read_issue("ECH-288")

    expected_revision = int(
        datetime.fromisoformat("2026-08-05T20:55:15.325+00:00").timestamp()
        * 1_000_000
    )
    assert snapshot.issue_id == "ECH-288"
    assert snapshot.identifier == "ECH-288"
    assert snapshot.state == "In Progress"
    assert snapshot.state_type == "started"
    assert snapshot.labels == ("Linear", "Priority: Urgent")
    assert snapshot.team_id == TEAM_ID
    assert snapshot.team_name == "Echlon"
    assert snapshot.project_id == "project-1"
    assert snapshot.project_name == "Finish 90-Day Plan"
    assert snapshot.source_revision == expected_revision
    assert snapshot.attachments == (
        linear_mcp.linear.PullRequestRef("nousresearch/hermes-agent", 471),
    )
    assert snapshot.observation_id is not None
    assert snapshot.observation_id.startswith(
        f"linear-mcp:ECH-288:{expected_revision}:"
    )


def test_legacy_snapshot_digest_remains_compatible() -> None:
    snapshot = linear_mcp.linear.LinearIssueSnapshot(
        issue_id="issue-1",
        identifier="ECH-1",
        title="Legacy observation",
        issue_url="https://linear.app/echlon/issue/ECH-1/legacy-observation",
        source_revision=7,
        attachments=(
            linear_mcp.linear.PullRequestRef("NousResearch/hermes-agent", 471),
        ),
    )
    legacy_payload = {
        "issue_id": "issue-1",
        "identifier": "ECH-1",
        "title": "Legacy observation",
        "issue_url": "https://linear.app/echlon/issue/ECH-1/legacy-observation",
        "source_revision": 7,
        "attachments_complete": True,
        "attachments": [{
            "repository": "nousresearch/hermes-agent",
            "number": 471,
        }],
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert snapshot.digest() == expected


def test_missing_issue_is_permanent_and_not_retried() -> None:
    adapter, caller = _adapter({
        "get_issue": [MCPAdapterError("not found", kind="not_found")],
    })

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-404")

    assert exc_info.value.kind == "not_found"
    assert exc_info.value.retryable is False
    assert exc_info.value.attempts == 1
    assert len(caller.calls) == 1


def test_auth_failure_is_permanent_and_not_retried() -> None:
    adapter, caller = _adapter({
        "get_issue": [MCPAdapterError("unauthorized", kind="auth")],
    })

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-288")

    assert exc_info.value.kind == "auth"
    assert exc_info.value.retryable is False
    assert exc_info.value.attempts == 1
    assert len(caller.calls) == 1


def test_issue_identity_mismatch_fails_closed_for_uuid_request(
    issue_payload: dict[str, Any],
) -> None:
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("11111111-1111-1111-1111-111111111111")

    assert exc_info.value.kind == "ambiguous"


def test_negative_string_revision_is_rejected(
    issue_payload: dict[str, Any],
) -> None:
    issue_payload["updatedAt"] = "-1"
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-288")

    assert exc_info.value.kind == "validation"
    assert "non-negative" in str(exc_info.value)


def test_missing_structured_issue_state_is_rejected(
    issue_payload: dict[str, Any],
) -> None:
    issue_payload.pop("status")
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-288")

    assert exc_info.value.kind == "validation"
    assert "issue.status" in str(exc_info.value)


def test_duplicate_replayed_observation_returns_identical_snapshot(
    issue_payload: dict[str, Any],
) -> None:
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    first = adapter.read_issue("ECH-288")
    replay = adapter.read_issue("ECH-288")

    assert replay is first
    assert replay.observation_id == first.observation_id
    assert replay.digest() == first.digest()


def test_same_revision_with_different_state_fails_closed(
    issue_payload: dict[str, Any],
) -> None:
    changed = deepcopy(issue_payload)
    changed["status"] = "Done"
    changed["statusType"] = "completed"
    adapter, _caller = _adapter({"get_issue": [issue_payload, changed]})
    adapter.read_issue("ECH-288")

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-288")

    assert exc_info.value.kind == "validation"
    assert "reused a source revision" in str(exc_info.value)


def test_stale_revision_is_preserved_for_coordinator_gating(
    issue_payload: dict[str, Any],
) -> None:
    newer = deepcopy(issue_payload)
    newer["updatedAt"] = "2026-08-05T21:00:00Z"
    older = deepcopy(issue_payload)
    older["updatedAt"] = "2026-08-05T20:00:00Z"
    adapter, _caller = _adapter({"get_issue": [newer, older]})

    first = adapter.read_issue("ECH-288")
    stale = adapter.read_issue("ECH-288")

    assert stale.source_revision < first.source_revision
    assert stale.issue_id == first.issue_id


def test_out_of_order_observation_keeps_source_order_not_arrival_order(
    issue_payload: dict[str, Any],
) -> None:
    payloads = []
    for timestamp in (
        "2026-08-05T21:00:00Z",
        "2026-08-05T20:00:00Z",
        "2026-08-05T22:00:00Z",
    ):
        payload = deepcopy(issue_payload)
        payload["updatedAt"] = timestamp
        payloads.append(payload)
    adapter, _caller = _adapter({"get_issue": payloads})

    revisions = [adapter.read_issue("ECH-288").source_revision for _ in range(3)]

    assert revisions[1] < revisions[0] < revisions[2]


def test_missing_attachment_metadata_preserves_unknown_completeness(
    issue_payload: dict[str, Any],
) -> None:
    issue_payload["attachments"] = [{"id": "attachment-without-url"}]
    adapter, _caller = _adapter({"get_issue": [issue_payload]})

    snapshot = adapter.read_issue("ECH-288")

    assert snapshot.attachments is None
    assert snapshot.attachments_complete is False


def test_ambiguous_linear_diff_link_fails_closed(
    issue_payload: dict[str, Any],
) -> None:
    issue_payload["attachments"] = [{
        "id": "diff-1",
        "url": "https://linear.app/acme/review/review-1",
    }]
    adapter, _caller = _adapter({
        "get_issue": [issue_payload],
        "get_diff": [{
            "url": "https://github.com/acme/backend/pull/10",
            "fullIdentifier": "acme/frontend#11",
        }],
    })

    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        adapter.read_issue("ECH-288")

    assert exc_info.value.kind == "ambiguous"


def test_pagination_uses_cursor_and_deduplicates_exact_replay() -> None:
    first = {"id": "ECH-1", "title": "one"}
    second = {"id": "ECH-2", "title": "two"}
    adapter, caller = _adapter({
        "list_issues": [
            {"issues": [first], "hasNextPage": True, "cursor": "next-1"},
            {"issues": [first, second], "hasNextPage": False},
        ],
    })

    rows = adapter.list_issues(team=TEAM_ID, limit=3)

    assert [row["id"] for row in rows] == ["ECH-1", "ECH-2"]
    assert caller.calls[0][1]["team"] == TEAM_ID
    assert "cursor" not in caller.calls[0][1]
    assert caller.calls[1][1]["cursor"] == "next-1"


def test_timeout_retries_then_succeeds(issue_payload: dict[str, Any]) -> None:
    sleeps: list[float] = []
    adapter, caller = _adapter(
        {
            "get_issue": [
                MCPAdapterError("deadline exceeded", kind="timeout"),
                issue_payload,
            ],
        },
        sleeps=sleeps,
    )

    snapshot = adapter.read_issue("ECH-288")

    assert snapshot.issue_id == "ECH-288"
    assert len(caller.calls) == 2
    assert sleeps == [0.25]


def test_health_distinguishes_all_readiness_stages(
    issue_payload: dict[str, Any],
) -> None:
    adapter, caller = _adapter({
        "get_team": [{"id": TEAM_ID, "name": "Echlon"}],
        "get_issue": [issue_payload],
    })
    bundle = linear_mcp.LinearMCPAdapterBundle(
        adapter=adapter,
        config=adapter.config,
        oauth_configured=True,
        registered_read_tools=tuple(
            sorted(f"mcp__linear__{name}" for name in linear_mcp.LINEAR_MCP_READ_TOOLS)
        ),
    )

    payload = linear_mcp.diagnose_linear_mcp(
        config=adapter.config,
        mcp_servers={"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        team_query="ECH",
        issue_id="ECH-288",
        bundle_builder=lambda **_kwargs: bundle,
    )

    assert payload["status"] == "ready"
    assert payload["stages"] == {
        "configured": True,
        "connected": True,
        "discovered": True,
        "resource_authorized": True,
        "write_enabled": False,
    }
    assert payload["resource"]["team_id"] == TEAM_ID
    assert payload["resource"]["issue_identifier"] == "ECH-288"
    assert caller.calls[0][0].endswith("__get_team")
    assert caller.calls[0][1] == {"query": "ECH"}
    assert payload["webhooks_implemented"] is False
    assert payload["oauth_event_delivery"] is False
    assert payload["external_side_effects"] == "none"


def test_health_reports_unconfigured_before_connection() -> None:
    payload = linear_mcp.diagnose_linear_mcp(
        config=_config(),
        mcp_servers={},
    )

    assert payload["status"] == "blocked"
    assert payload["stages"]["configured"] is False
    assert payload["stages"]["connected"] is False
    assert payload["failure"]["kind"] == "unavailable"


def test_health_distinguishes_connected_from_partial_discovery() -> None:
    def partial_register(_servers: dict[str, dict[str, Any]]) -> list[str]:
        return ["mcp__linear__get_issue"]

    def build(**kwargs: Any) -> linear_mcp.LinearMCPAdapterBundle:
        return linear_mcp.build_linear_mcp_adapter(
            **kwargs,
            register_servers=partial_register,
            caller_factory=_empty_caller_factory,
        )

    payload = linear_mcp.diagnose_linear_mcp(
        config=_config(),
        mcp_servers={"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        bundle_builder=build,
    )

    assert payload["status"] == "blocked"
    assert payload["stages"]["configured"] is True
    assert payload["stages"]["connected"] is True
    assert payload["stages"]["discovered"] is False
    assert payload["stages"]["resource_authorized"] is False
    assert payload["failure"]["stage"] == "discovery"


def test_health_normalizes_connection_exception() -> None:
    def failed_register(_servers: dict[str, dict[str, Any]]) -> list[str]:
        raise TimeoutError("outer discovery timeout")

    def build(**kwargs: Any) -> linear_mcp.LinearMCPAdapterBundle:
        return linear_mcp.build_linear_mcp_adapter(
            **kwargs,
            register_servers=failed_register,
            caller_factory=_empty_caller_factory,
        )

    payload = linear_mcp.diagnose_linear_mcp(
        config=_config(),
        mcp_servers={"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        bundle_builder=build,
    )

    assert payload["status"] == "blocked"
    assert payload["stages"]["configured"] is True
    assert payload["stages"]["connected"] is False
    assert payload["failure"]["kind"] == "unavailable"
    assert payload["failure"]["stage"] == "connection"
    assert "outer discovery timeout" not in payload["failure"]["message"]


def test_builder_rejects_noncanonical_linear_endpoint() -> None:
    with pytest.raises(linear_mcp.LinearMCPReadError) as exc_info:
        linear_mcp.build_linear_mcp_adapter(
            config=_config(),
            mcp_servers={
                "linear": {
                    "url": f"{LINEAR_URL}?redirect=https://example.com",
                    "auth": "oauth",
                },
            },
            register_servers=_no_tools_register,
            caller_factory=_empty_caller_factory,
        )

    assert exc_info.value.kind == "validation"


def test_cli_health_dispatches_before_kanban_db_init(monkeypatch, capsys) -> None:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = parser.parse_args([
        "kanban",
        "linear-mcp",
        "health",
        "--team",
        TEAM_ID,
        "--issue-id",
        "ECH-288",
        "--json",
    ])
    health = {
        "status": "ready",
        "stages": {
            "configured": True,
            "connected": True,
            "discovered": True,
            "resource_authorized": True,
            "write_enabled": False,
        },
    }
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {"linear_mcp": {}},
            "mcp_servers": {"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        },
    )
    monkeypatch.setattr(
        linear_mcp,
        "diagnose_linear_mcp",
        lambda **_kwargs: health,
    )
    monkeypatch.setattr(
        kb,
        "init_db",
        lambda: pytest.fail("read-only Linear health must not initialize Kanban DB"),
    )

    assert kanban_cli.kanban_command(args) == 0
    assert json.loads(capsys.readouterr().out) == health


def test_cli_invalid_config_returns_structured_failure(monkeypatch, capsys) -> None:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = parser.parse_args([
        "kanban",
        "linear-mcp",
        "health",
        "--timeout-seconds",
        "0",
        "--json",
    ])
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {"linear_mcp": {}},
            "mcp_servers": {"linear": {"url": LINEAR_URL, "auth": "oauth"}},
        },
    )
    monkeypatch.setattr(
        kb,
        "init_db",
        lambda: pytest.fail("read-only Linear health must not initialize Kanban DB"),
    )

    assert kanban_cli.kanban_command(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["stages"]["configured"] is True
    assert payload["stages"]["connected"] is False
    assert payload["failure"]["kind"] == "validation"
    assert payload["failure"]["stage"] == "configuration"
