"""Tests for the scoped MCP boundary used by the Kanban review runner."""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_github as github
from hermes_cli import kanban_slack as slack
from hermes_cli.kanban_mcp_adapters import (
    GitHubMCPDeliveryTransport,
    GitHubMCPReadAdapter,
    MCPAdapterError,
    RegistryMCPToolCaller,
    SlackMCPDeliveryTransport,
    SlackMCPAcknowledgementProvider,
    build_review_runner_mcp_bundle,
)


HEAD = "a" * 40
OLD_HEAD = "b" * 40
NOW = 1_800_000_000
REPOSITORY = "nousresearch/hermes-agent"
CHANNEL = "C_STAGING"


class FakeCaller:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        response = self.responses[tool_name]
        if isinstance(response, BaseException):
            raise response
        return response


def _pr_payload(*, head_sha: str = HEAD) -> dict[str, Any]:
    return {
        "number": 41,
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/41",
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"ref": "feature/mcp", "sha": head_sha},
        "base": {"ref": "main"},
        "requested_reviewers": [{"login": "reviewer"}],
        "requested_teams": [],
    }


def _status_payload(
    *,
    state: str = "success",
    description: str = "CodeRabbit review complete",
    include_coderabbit: bool = True,
) -> dict[str, Any]:
    statuses = []
    if include_coderabbit:
        statuses.append({
            "id": 9,
            "sha": HEAD,
            "context": "CodeRabbit",
            "state": state,
            "description": description,
        })
    return {
        "state": state,
        "sha": HEAD,
        "total_count": len(statuses),
        "statuses": statuses,
    }


def _review_payload(
    body: str,
    *,
    head_sha: str = HEAD,
    review_id: int = 501,
) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": "coderabbitai[bot]"},
        "commit_id": head_sha,
        "state": "COMMENTED",
        "body": body,
        "submitted_at": "2027-01-15T08:00:00Z",
    }


def _comment_payload(
    *,
    head_sha: str = HEAD,
    comment_id: int = 601,
    position: int | None = 1,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "pull_request_review_id": 501,
        "user": {"login": "coderabbitai[bot]"},
        "commit_id": head_sha,
        "created_at": "2027-01-15T08:01:00Z",
        "position": position,
        "line": 10 if position is not None else None,
        "body": "Please address this exact-head issue.",
    }


def _github_caller(
    *,
    status: Mapping[str, Any] | None = None,
    reviews: list[Mapping[str, Any]] | None = None,
    comments: list[Mapping[str, Any]] | None = None,
) -> FakeCaller:
    return FakeCaller({
        "mcp__github__get_pull_request": _pr_payload(),
        "mcp__github__get_pull_request_status": status or _status_payload(),
        "mcp__github__get_pull_request_reviews": reviews or [],
        "mcp__github__get_pull_request_comments": comments or [],
    })


def _github_adapter(caller: FakeCaller) -> GitHubMCPReadAdapter:
    return GitHubMCPReadAdapter(
        caller,
        server_name="github",
        repositories=(REPOSITORY,),
        clock=lambda: NOW,
    )


def _github_intent(
    *,
    operation: github.GitHubOperation = "create_comment",
    surface: github.GitHubSurface = "pull_request_comments",
) -> github.GitHubOutboxIntent:
    key = (
        f"github-human-review:v1:{REPOSITORY}:pr:41:head:{HEAD}:"
        f"surface:{surface}:operation:{operation}"
    )
    return github.GitHubOutboxIntent(
        id="gho_delivery",
        gate_id="g_delivery",
        repository=REPOSITORY,
        pr_number=41,
        head_sha=HEAD,
        surface=surface,
        operation=operation,
        payload={"body": "Exact-head review evidence."},
        payload_sha256="0" * 64,
        idempotency_key=key,
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        external_id=None,
        last_snapshot_sha256=None,
        last_snapshot_observed_at=None,
        last_failure_kind=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        sent_at=None,
    )


def _slack_intent(
    *,
    surface: slack.SlackSurface = "channel",
    operation: slack.SlackOperation = "notify_human_review",
    thread_ts: str = "",
) -> slack.SlackOutboxIntent:
    thread_key = thread_ts or "root"
    key = (
        f"slack-human-review:v1:channel:{CHANNEL}:thread:{thread_key}:"
        f"{REPOSITORY}:pr:41:head:{HEAD}:surface:{surface}:operation:{operation}"
    )
    return slack.SlackOutboxIntent(
        id="slo_delivery",
        gate_id="g_delivery",
        source_intent_id=None,
        repository=REPOSITORY,
        pr_number=41,
        head_sha=HEAD,
        channel_id=CHANNEL,
        thread_ts=thread_ts,
        surface=surface,
        operation=operation,
        payload={"body": "Exact-head human-review notification."},
        payload_sha256="0" * 64,
        idempotency_key=key,
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        external_message_ts=None,
        delivered_thread_ts=None,
        last_snapshot_sha256=None,
        last_snapshot_observed_at=None,
        last_failure_kind=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        sent_at=None,
    )


def test_github_mcp_normalizes_exact_head_and_reuses_one_collection() -> None:
    caller = _github_caller(
        reviews=[_review_payload("1 actionable comment")],
        comments=[
            _comment_payload(),
            _comment_payload(head_sha=OLD_HEAD, comment_id=602),
        ],
    )
    adapter = _github_adapter(caller)

    snapshot = adapter.read_snapshot(repository=REPOSITORY, pr_number=41)
    review = adapter.read_review(
        repository=REPOSITORY,
        pr_number=41,
        expected_head_sha=HEAD,
    )
    assessment = coderabbit.assess_snapshot(review, current_head_sha=HEAD, now=NOW)

    assert snapshot.head_sha == HEAD
    assert snapshot.base_ref == "main"
    assert snapshot.requested_reviewers[0].principal == "reviewer"
    assert review.head_sha == HEAD
    assert assessment.state == "actionable"
    assert assessment.actionable_count == 1
    assert assessment.outdated_count == 1
    assert len(caller.calls) == 4
    assert {name for name, _ in caller.calls} == {
        "mcp__github__get_pull_request",
        "mcp__github__get_pull_request_status",
        "mcp__github__get_pull_request_reviews",
        "mcp__github__get_pull_request_comments",
    }


def test_github_mcp_replay_has_stable_observation_ids() -> None:
    caller = _github_caller(reviews=[_review_payload("No actionable comments")])
    adapter = _github_adapter(caller)

    first_pr = adapter.read_snapshot(repository=REPOSITORY, pr_number=41)
    first_review = adapter.read_review(
        repository=REPOSITORY,
        pr_number=41,
        expected_head_sha=HEAD,
    )
    second_pr = adapter.read_snapshot(repository=REPOSITORY, pr_number=41)
    second_review = adapter.read_review(
        repository=REPOSITORY,
        pr_number=41,
        expected_head_sha=HEAD,
    )

    assert second_pr.observation_id == first_pr.observation_id
    assert second_review.observation_id == first_review.observation_id
    assert second_review.normalized_dict() == first_review.normalized_dict()


@pytest.mark.parametrize(
    ("status", "reviews", "comments", "expected"),
    [
        (_status_payload(), [_review_payload("Review clean")], [], "clean"),
        (
            _status_payload(state="error", description="Review rate limit reached"),
            [],
            [],
            "rate_limited",
        ),
        (
            _status_payload(
                state="success", description="Review skipped by configuration"
            ),
            [],
            [],
            "skipped",
        ),
        (
            _status_payload(state="pending", description="Review in progress"),
            [],
            [],
            "pending",
        ),
        (
            _status_payload(),
            [_review_payload("No actionable comments")],
            [],
            "no_actionable_comments",
        ),
        (
            _status_payload(),
            [_review_payload("1 actionable comment")],
            [_comment_payload()],
            "actionable",
        ),
    ],
)
def test_coderabbit_semantic_states_are_not_collapsed(
    status: Mapping[str, Any],
    reviews: list[Mapping[str, Any]],
    comments: list[Mapping[str, Any]],
    expected: str,
) -> None:
    adapter = _github_adapter(
        _github_caller(status=status, reviews=reviews, comments=comments)
    )
    adapter.read_snapshot(repository=REPOSITORY, pr_number=41)

    review = adapter.read_review(
        repository=REPOSITORY,
        pr_number=41,
        expected_head_sha=HEAD,
    )

    assessment = coderabbit.assess_snapshot(review, current_head_sha=HEAD, now=NOW)
    assert assessment.state == expected


def test_coderabbit_old_head_review_does_not_count_as_current() -> None:
    adapter = _github_adapter(
        _github_caller(
            status=_status_payload(state="pending", description="Review pending"),
            reviews=[_review_payload("Review clean", head_sha=OLD_HEAD)],
        )
    )
    adapter.read_snapshot(repository=REPOSITORY, pr_number=41)

    review = adapter.read_review(
        repository=REPOSITORY,
        pr_number=41,
        expected_head_sha=HEAD,
    )

    assessment = coderabbit.assess_snapshot(review, current_head_sha=HEAD, now=NOW)
    assert assessment.state == "pending"
    assert review.review_generation == 0


def test_github_mcp_rejects_repository_outside_allowlist_before_call() -> None:
    caller = _github_caller()
    adapter = _github_adapter(caller)

    with pytest.raises(github.GitHubTransportFailure) as exc_info:
        adapter.read_snapshot(repository="other/repo", pr_number=41)

    assert exc_info.value.kind == "permission"
    assert caller.calls == []


def test_github_mcp_provider_permission_failure_is_typed_and_fail_closed() -> None:
    caller = _github_caller()
    caller.responses["mcp__github__get_pull_request"] = MCPAdapterError(
        "Permission Denied: Resource not accessible by personal access token",
        kind="permission",
    )
    adapter = _github_adapter(caller)

    with pytest.raises(github.GitHubTransportFailure) as exc_info:
        adapter.read_snapshot(repository=REPOSITORY, pr_number=41)

    assert exc_info.value.kind == "permission"
    assert [name for name, _ in caller.calls] == ["mcp__github__get_pull_request"]


def test_github_mcp_rejects_head_change_between_expected_and_readback() -> None:
    caller = _github_caller()
    caller.responses["mcp__github__get_pull_request"] = _pr_payload(head_sha=OLD_HEAD)
    caller.responses["mcp__github__get_pull_request_status"] = {
        "sha": OLD_HEAD,
        "statuses": [],
        "state": "pending",
    }
    adapter = _github_adapter(caller)

    with pytest.raises(coderabbit.CodeRabbitBoundaryError):
        adapter.read_review(
            repository=REPOSITORY,
            pr_number=41,
            expected_head_sha=HEAD,
        )


@pytest.mark.parametrize("surface", ["review", "comment"])
def test_github_mcp_fails_closed_when_exact_head_commit_id_is_null(
    surface: str,
) -> None:
    reviews: list[Mapping[str, Any]] = []
    comments: list[Mapping[str, Any]] = []
    if surface == "review":
        item = _review_payload("No actionable comments")
        item["commit_id"] = None
        reviews.append(item)
    else:
        item = _comment_payload()
        item["commit_id"] = None
        comments.append(item)
    adapter = _github_adapter(_github_caller(reviews=reviews, comments=comments))

    with pytest.raises(github.GitHubTransportFailure, match="commit_id") as exc_info:
        adapter.read_snapshot(repository=REPOSITORY, pr_number=41)

    assert exc_info.value.kind == "validation"


def test_slack_mcp_acknowledgements_are_channel_and_user_allowlisted() -> None:
    caller = FakeCaller({
        "mcp__slack__slack_get_thread_replies": {
            "messages": [
                {
                    "ts": "100.000",
                    "user": "BOT",
                    "text": "root",
                    "reactions": [{"name": "eyes", "users": ["U_REVIEWER"]}],
                },
                {
                    "ts": "101.000",
                    "user": "U_REVIEWER",
                    "text": "approve",
                },
                {"ts": "102.000", "user": "U_OTHER", "text": "approve"},
            ]
        }
    })
    provider = SlackMCPAcknowledgementProvider(
        caller,
        server_name="slack",
        channel_ids=("C_STAGING",),
        user_ids=("U_REVIEWER",),
        clock=lambda: NOW,
    )

    events = provider.read_acknowledgements(
        channel_id="C_STAGING",
        thread_ts="100.000",
    )

    assert [(event.source, event.value, event.user_id) for event in events] == [
        ("reaction", "eyes", "U_REVIEWER"),
        ("text", "approve", "U_REVIEWER"),
    ]
    assert caller.calls[0][1] == {
        "channel_id": "C_STAGING",
        "thread_ts": "100.000",
    }
    with pytest.raises(slack.SlackTransportFailure) as exc_info:
        provider.read_acknowledgements(channel_id="C_PROD", thread_ts="100.000")
    assert exc_info.value.kind == "permission"


def test_slack_mcp_invalid_auth_fails_closed() -> None:
    provider = SlackMCPAcknowledgementProvider(
        FakeCaller({
            "mcp__slack__slack_get_thread_replies": {
                "ok": False,
                "error": "invalid_auth",
            }
        }),
        server_name="slack",
        channel_ids=("C_STAGING",),
        user_ids=("U_REVIEWER",),
    )

    with pytest.raises(slack.SlackTransportFailure) as exc_info:
        provider.read_acknowledgements(
            channel_id="C_STAGING",
            thread_ts="100.000",
        )

    assert exc_info.value.kind == "auth"


def test_github_delivery_sends_exact_head_review_and_reads_marker_receipt() -> None:
    intent = _github_intent()
    send_caller = FakeCaller({
        "mcp__github__create_pull_request_review": {
            "id": 701,
            "commit_id": HEAD,
        }
    })
    transport = GitHubMCPDeliveryTransport(
        send_caller,
        server_name="github",
        repositories=(REPOSITORY,),
    )

    sent = transport.send_intent(intent)
    tool, arguments = send_caller.calls[0]

    assert sent.external_id == "github-review:701"
    assert tool == "mcp__github__create_pull_request_review"
    assert arguments["commit_id"] == HEAD
    assert arguments["event"] == "COMMENT"
    assert "hermes-review-receipt:" in arguments["body"]
    assert intent.idempotency_key not in arguments["body"]

    read_caller = FakeCaller({
        "mcp__github__get_pull_request_reviews": [
            {
                "id": 701,
                "commit_id": HEAD,
                "body": arguments["body"],
            }
        ]
    })
    replay = GitHubMCPDeliveryTransport(
        read_caller,
        server_name="github",
        repositories=(REPOSITORY,),
    ).find_delivery(idempotency_key=intent.idempotency_key)

    assert replay == sent
    assert read_caller.calls == [
        (
            "mcp__github__get_pull_request_reviews",
            {"owner": "nousresearch", "repo": "hermes-agent", "pull_number": 41},
        )
    ]


def test_github_delivery_fails_closed_for_unsupported_reviewer_request() -> None:
    caller = FakeCaller({})
    transport = GitHubMCPDeliveryTransport(
        caller,
        server_name="github",
        repositories=(REPOSITORY,),
    )

    with pytest.raises(github.GitHubTransportFailure) as exc_info:
        transport.send_intent(
            _github_intent(
                operation="request_reviewer",
                surface="review_requests",
            )
        )

    assert exc_info.value.kind == "validation"
    assert caller.calls == []


def test_github_delivery_retries_ambiguous_write_but_not_invalid_intent() -> None:
    caller = FakeCaller({"mcp__github__create_pull_request_review": "not-a-receipt"})
    transport = GitHubMCPDeliveryTransport(
        caller,
        server_name="github",
        repositories=(REPOSITORY,),
    )

    with pytest.raises(github.GitHubTransportFailure) as ambiguous:
        transport.send_intent(_github_intent())
    assert ambiguous.value.kind == "unavailable"

    with pytest.raises(github.GitHubTransportFailure) as invalid:
        transport.send_intent(replace(_github_intent(), payload={}))
    assert invalid.value.kind == "validation"
    assert len(caller.calls) == 1


@pytest.mark.parametrize(
    ("intent", "tool", "response", "delivered_thread"),
    [
        (
            _slack_intent(),
            "mcp__slack__slack_post_message",
            {"ok": True, "channel": CHANNEL, "ts": "200.001"},
            "200.001",
        ),
        (
            _slack_intent(
                surface="thread",
                operation="reply",
                thread_ts="100.001",
            ),
            "mcp__slack__slack_reply_to_thread",
            {
                "ok": True,
                "channel": CHANNEL,
                "ts": "200.002",
                "thread_ts": "100.001",
            },
            "100.001",
        ),
    ],
)
def test_slack_delivery_preserves_exact_channel_and_thread_routes(
    intent: slack.SlackOutboxIntent,
    tool: str,
    response: Mapping[str, Any],
    delivered_thread: str,
) -> None:
    caller = FakeCaller({tool: response})
    transport = SlackMCPDeliveryTransport(
        caller,
        server_name="slack",
        channel_ids=(CHANNEL,),
    )

    receipt = transport.send_intent(intent)
    called_tool, arguments = caller.calls[0]

    assert called_tool == tool
    assert arguments["channel_id"] == CHANNEL
    assert receipt.thread_ts == delivered_thread
    assert receipt.message_ts == response["ts"]
    assert "hermes-review-receipt:" in arguments["text"]
    assert intent.idempotency_key not in arguments["text"]


def test_slack_delivery_reads_existing_marker_before_replay() -> None:
    intent = _slack_intent(
        surface="thread",
        operation="reply",
        thread_ts="100.001",
    )
    send_caller = FakeCaller({
        "mcp__slack__slack_reply_to_thread": {
            "ok": True,
            "channel": CHANNEL,
            "ts": "200.002",
            "thread_ts": "100.001",
        }
    })
    sender = SlackMCPDeliveryTransport(
        send_caller,
        server_name="slack",
        channel_ids=(CHANNEL,),
    )
    sent = sender.send_intent(intent)
    body = send_caller.calls[0][1]["text"]
    read_caller = FakeCaller({
        "mcp__slack__slack_get_thread_replies": {
            "messages": [
                {
                    "ts": "200.002",
                    "thread_ts": "100.001",
                    "text": body,
                }
            ]
        }
    })

    replay = SlackMCPDeliveryTransport(
        read_caller,
        server_name="slack",
        channel_ids=(CHANNEL,),
    ).find_delivery(idempotency_key=intent.idempotency_key)

    assert replay == sent
    assert read_caller.calls == [
        (
            "mcp__slack__slack_get_thread_replies",
            {"channel_id": CHANNEL, "thread_ts": "100.001"},
        )
    ]


def test_slack_delivery_retries_ambiguous_write_but_not_invalid_intent() -> None:
    caller = FakeCaller({"mcp__slack__slack_post_message": "not-a-receipt"})
    transport = SlackMCPDeliveryTransport(
        caller,
        server_name="slack",
        channel_ids=(CHANNEL,),
    )

    with pytest.raises(slack.SlackTransportFailure) as ambiguous:
        transport.send_intent(_slack_intent())
    assert ambiguous.value.kind == "unavailable"

    with pytest.raises(slack.SlackTransportFailure) as invalid:
        transport.send_intent(replace(_slack_intent(), payload={}))
    assert invalid.value.kind == "validation"
    assert len(caller.calls) == 1


def test_registry_boundary_times_out_and_rejects_non_allowlisted_tools(
    monkeypatch,
) -> None:
    class SlowRegistry:
        @staticmethod
        def get_entry(name: str):
            del name
            return SimpleNamespace(toolset="mcp-timeout")

        @staticmethod
        def dispatch(name: str, arguments: Mapping[str, Any]):
            del name, arguments
            time.sleep(2)
            return '{"result": {"ok": true}}'

    import tools.registry as registry_module

    monkeypatch.setattr(registry_module, "registry", SlowRegistry())
    caller = RegistryMCPToolCaller(
        "timeout",
        frozenset({"mcp__timeout__read"}),
        1,
    )

    started = time.monotonic()
    with pytest.raises(MCPAdapterError) as exc_info:
        caller.call("mcp__timeout__read", {})
    elapsed = time.monotonic() - started

    assert exc_info.value.kind == "timeout"
    assert elapsed < 1.5
    with pytest.raises(MCPAdapterError) as denied:
        caller.call("mcp__timeout__write", {})
    assert denied.value.kind == "permission"


def test_bundle_registers_only_selected_servers_and_overrides_timeouts(
    monkeypatch,
) -> None:
    registered: dict[str, Any] = {}

    def fake_register(config: Mapping[str, Any]) -> list[str]:
        registered.update(config)
        return [
            f"mcp__{server_name}__{tool_name}"
            for server_name, server_config in config.items()
            for tool_name in server_config["tools"]["include"]
        ]

    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", fake_register)
    bundle = build_review_runner_mcp_bundle(
        provider_timeout_seconds=7,
        github_server_name="github",
        github_repositories=(REPOSITORY,),
        slack_server_name="slack",
        slack_channel_ids=("C_STAGING",),
        slack_user_ids=("U_REVIEWER",),
        mcp_servers={
            "github": {
                "command": "github",
                "timeout": 99,
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "github-test-token"},
                "tools": {"include": ["merge_pull_request"]},
            },
            "slack": {
                "command": "slack",
                "connect_timeout": 90,
                "env": {
                    "SLACK_BOT_TOKEN": "slack-test-token",
                    "SLACK_TEAM_ID": "T_TEST",
                },
                "tools": {"include": ["slack_post_message"]},
            },
            "notion": {"command": "notion", "secret": "must-not-cross-boundary"},
        },
        raw_mcp_servers={
            "github": {
                "command": "github",
                "env": {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}"
                },
            },
            "slack": {
                "command": "slack",
                "env": {
                    "SLACK_BOT_TOKEN": "${env:SLACK_BOT_TOKEN}",
                    "SLACK_TEAM_ID": "T_TEST",
                },
            },
        },
    )

    assert set(registered) == {"github", "slack"}
    assert registered["github"]["timeout"] == 7
    assert registered["github"]["tools"] == {
        "include": [
            "get_pull_request",
            "get_pull_request_comments",
            "get_pull_request_reviews",
            "get_pull_request_status",
        ],
        "prompts": False,
        "resources": False,
    }
    assert registered["slack"]["timeout"] == 7
    assert registered["slack"]["connect_timeout"] == 7
    assert registered["slack"]["tools"] == {
        "include": ["slack_get_thread_replies"],
        "prompts": False,
        "resources": False,
    }
    assert bundle.github_adapter is not None
    assert bundle.github_delivery_transport is None
    assert bundle.slack_delivery_transport is None
    assert bundle.slack_acknowledgement_provider is not None
    assert bundle.credential_preflight is not None
    assert bundle.credential_preflight["github"]["ready"] is True
    assert bundle.credential_preflight["slack"]["ready"] is True


def test_bundle_delivery_gate_registers_only_restricted_receipt_tools(
    monkeypatch,
) -> None:
    registered: dict[str, Any] = {}

    def fake_register(config: Mapping[str, Any]) -> list[str]:
        registered.update(config)
        return [
            f"mcp__{server_name}__{tool_name}"
            for server_name, server_config in config.items()
            for tool_name in server_config["tools"]["include"]
        ]

    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", fake_register)
    expanded = {
        "github": {
            "command": "github",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "github-test-token"},
        },
        "slack": {
            "command": "slack",
            "env": {
                "SLACK_BOT_TOKEN": "slack-test-token",
                "SLACK_TEAM_ID": "T_TEST",
            },
        },
    }
    raw = {
        "github": {
            "command": "github",
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}"
            },
        },
        "slack": {
            "command": "slack",
            "env": {
                "SLACK_BOT_TOKEN": "${env:SLACK_BOT_TOKEN}",
                "SLACK_TEAM_ID": "T_TEST",
            },
        },
    }

    bundle = build_review_runner_mcp_bundle(
        provider_timeout_seconds=7,
        github_server_name="github",
        github_repositories=(REPOSITORY,),
        github_delivery_enabled=True,
        slack_server_name="slack",
        slack_channel_ids=(CHANNEL,),
        slack_user_ids=("U_REVIEWER",),
        slack_delivery_enabled=True,
        mcp_servers=expanded,
        raw_mcp_servers=raw,
    )

    assert registered["github"]["tools"]["include"] == [
        "create_pull_request_review",
        "get_pull_request",
        "get_pull_request_comments",
        "get_pull_request_reviews",
        "get_pull_request_status",
    ]
    assert registered["slack"]["tools"]["include"] == [
        "slack_get_channel_history",
        "slack_get_thread_replies",
        "slack_post_message",
        "slack_reply_to_thread",
    ]
    exposed = {
        tool
        for provider in registered.values()
        for tool in provider["tools"]["include"]
    }
    assert not exposed.intersection({
        "approve_pull_request",
        "merge_pull_request",
        "request_reviewers",
        "slack_create_channel",
    })
    assert bundle.github_delivery_transport is not None
    assert bundle.slack_delivery_transport is not None


def test_bundle_requires_explicit_repository_channel_and_user_allowlists(
    monkeypatch,
) -> None:
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", lambda config: [])
    servers = {"github": {"command": "github"}, "slack": {"command": "slack"}}

    with pytest.raises(MCPAdapterError) as repository_error:
        build_review_runner_mcp_bundle(
            provider_timeout_seconds=5,
            github_server_name="github",
            mcp_servers=servers,
        )
    assert repository_error.value.kind == "permission"

    with pytest.raises(MCPAdapterError) as channel_error:
        build_review_runner_mcp_bundle(
            provider_timeout_seconds=5,
            slack_server_name="slack",
            slack_user_ids=("U_REVIEWER",),
            mcp_servers=servers,
        )
    assert channel_error.value.kind == "permission"


def test_bundle_blocks_plaintext_credentials_before_mcp_registration(
    monkeypatch,
) -> None:
    called = False

    def fake_register(config: Mapping[str, Any]) -> list[str]:
        nonlocal called
        called = True
        return []

    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", fake_register)
    with pytest.raises(MCPAdapterError) as exc_info:
        build_review_runner_mcp_bundle(
            provider_timeout_seconds=5,
            github_server_name="github",
            github_repositories=(REPOSITORY,),
            mcp_servers={
                "github": {
                    "command": "github",
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "plaintext-test-token"},
                }
            },
        )

    assert exc_info.value.kind == "auth"
    assert called is False
    assert "plaintext-test-token" not in str(exc_info.value)


def test_bundle_fails_when_required_read_tools_are_not_discovered(monkeypatch) -> None:
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", lambda config: [])
    with pytest.raises(MCPAdapterError) as exc_info:
        build_review_runner_mcp_bundle(
            provider_timeout_seconds=5,
            github_server_name="github",
            github_repositories=(REPOSITORY,),
            mcp_servers={
                "github": {
                    "command": "github",
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "github-test-token"},
                }
            },
            raw_mcp_servers={
                "github": {
                    "command": "github",
                    "env": {
                        "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}"
                    },
                }
            },
        )

    assert exc_info.value.kind == "unavailable"


@pytest.mark.parametrize(
    ("server_name", "normalized_server_name"),
    (("github", "github"), ("github-prod", "github_prod")),
)
def test_bundle_fails_when_selected_server_was_registered_with_write_tools(
    monkeypatch, server_name: str, normalized_server_name: str
) -> None:
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(
        mcp_tool,
        "register_mcp_servers",
        lambda config: [
            *(
                f"mcp__{normalized_server_name}__{name}"
                for name in config[server_name]["tools"]["include"]
            ),
            f"mcp__{normalized_server_name}__merge_pull_request",
            "mcp__other__unrelated_tool",
        ],
    )
    with pytest.raises(MCPAdapterError) as exc_info:
        build_review_runner_mcp_bundle(
            provider_timeout_seconds=5,
            github_server_name=server_name,
            github_repositories=(REPOSITORY,),
            mcp_servers={
                server_name: {
                    "command": "github",
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "github-test-token"},
                }
            },
            raw_mcp_servers={
                server_name: {
                    "command": "github",
                    "env": {
                        "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}"
                    },
                }
            },
        )

    assert exc_info.value.kind == "permission"


def test_bundle_preflights_both_providers_when_they_share_a_server(monkeypatch) -> None:
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(
        mcp_tool,
        "register_mcp_servers",
        lambda config: [
            f"mcp__combined__{name}" for name in config["combined"]["tools"]["include"]
        ],
    )
    expanded = {
        "combined": {
            "command": "combined",
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "github-test-token",
                "SLACK_BOT_TOKEN": "slack-test-token",
                "SLACK_TEAM_ID": "T_TEST",
            },
        }
    }
    raw = {
        "combined": {
            "command": "combined",
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_PERSONAL_ACCESS_TOKEN}",
                "SLACK_BOT_TOKEN": "${env:SLACK_BOT_TOKEN}",
                "SLACK_TEAM_ID": "T_TEST",
            },
        }
    }

    bundle = build_review_runner_mcp_bundle(
        provider_timeout_seconds=5,
        github_server_name="combined",
        github_repositories=(REPOSITORY,),
        slack_server_name="combined",
        slack_channel_ids=("C_STAGING",),
        slack_user_ids=("U_REVIEWER",),
        mcp_servers=expanded,
        raw_mcp_servers=raw,
    )

    assert bundle.credential_preflight is not None
    assert set(bundle.credential_preflight) == {"github", "slack"}
