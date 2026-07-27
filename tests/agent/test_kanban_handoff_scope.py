"""Behavior contracts for exact Gateway-origin short-task scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import kanban_handoff_scope as scope


def _identity(**overrides):
    identity = {
        "platform": "feishu",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "group-1",
        "thread_id": "thread-1",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "agent:default:feishu:group:group-1:user-1",
    }
    identity.update(overrides)
    return identity


def _config(*, enabled=True, allowed=None):
    if allowed is None:
        allowed = [
            {
                "platform": "feishu",
                "chat_type": "group",
                "chat_id": "group-1",
                "user_id": "user-1",
            }
        ]
    return {
        "agent": {"max_turns": 90},
        "terminal": {"backend": "local"},
        "kanban": {
            "failure_limit": 2,
            "short_task_handoff": {
                "enabled": enabled,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_origins": allowed,
                "allowed_workspace_roots": ["/tmp"],
            },
        },
    }


def test_exact_allowed_origin_receives_frozen_worker_policy():
    decision = scope.decide_gateway_origin(_config(), _identity())

    assert decision["authorized"] is True
    frozen = json.loads(decision["task_policy_json"])
    assert frozen["origin"] == _identity()
    assert frozen["matched_origin"] == {
        "platform": "feishu",
        "chat_type": "group",
        "chat_id": "group-1",
        "user_id": "user-1",
    }
    assert frozen["worker_policy"]["soft_iteration_limit"] == 4
    assert frozen["worker_policy"]["max_handoffs"] == 1
    assert frozen["worker_policy"]["allowed_workspace_roots"] == [
        str(Path("/tmp").resolve())
    ]


def test_missing_or_malformed_workspace_allowlist_fails_closed(tmp_path):
    missing = _config()
    missing["kanban"]["short_task_handoff"]["allowed_workspace_roots"] = []
    decision = scope.decide_gateway_origin(missing, _identity())
    assert decision["authorized"] is False
    assert "allowed_workspace_roots" in decision["validation_error"]

    relative = _config()
    relative["kanban"]["short_task_handoff"]["allowed_workspace_roots"] = [
        "relative/pilot"
    ]
    decision = scope.decide_gateway_origin(relative, _identity())
    assert decision["authorized"] is False
    assert "absolute" in decision["validation_error"]

    broad = _config()
    broad["kanban"]["short_task_handoff"]["allowed_workspace_roots"] = [
        str(Path("/").resolve())
    ]
    decision = scope.decide_gateway_origin(broad, _identity())
    assert decision["authorized"] is False
    assert "too broad" in decision["validation_error"]


def test_frozen_policy_rejects_a_forged_filesystem_root_allowlist():
    decision = scope.decide_gateway_origin(_config(), _identity())
    policy = json.loads(decision["task_policy_json"])
    policy["worker_policy"]["allowed_workspace_roots"] = [
        str(Path("/").resolve())
    ]

    with pytest.raises(ValueError, match="too broad"):
        scope.canonical_task_policy(policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "weixin"),
        ("chat_type", "dm"),
        ("chat_id", "group-2"),
        ("user_id", "user-2"),
    ],
)
def test_cross_channel_or_actor_mismatch_stays_closed(field, value):
    decision = scope.decide_gateway_origin(
        _config(),
        _identity(**{field: value}),
    )

    assert decision == {
        "authorized": False,
        "reason": "origin_not_allowed",
    }


@pytest.mark.parametrize(
    "field",
    [
        "platform",
        "chat_type",
        "chat_id",
        "user_id",
        "notifier_profile",
        "session_key",
    ],
)
def test_incomplete_authenticated_identity_stays_closed(field):
    decision = scope.decide_gateway_origin(
        _config(),
        _identity(**{field: ""}),
    )

    assert decision == {
        "authorized": False,
        "reason": "identity_incomplete",
    }


def test_optional_session_field_narrows_one_group_user():
    allowed = [
        {
            "platform": "feishu",
            "chat_type": "group",
            "chat_id": "group-1",
            "user_id": "user-1",
            "session_key": "approved-session",
        }
    ]

    assert scope.decide_gateway_origin(
        _config(allowed=allowed),
        _identity(session_key="approved-session"),
    )["authorized"] is True
    assert scope.decide_gateway_origin(
        _config(allowed=allowed),
        _identity(session_key="other-session"),
    )["authorized"] is False


def test_optional_narrowing_field_distinguishes_missing_from_mismatch():
    allowed = [
        {
            "platform": "feishu",
            "chat_type": "group",
            "chat_id": "group-1",
            "user_id": "user-1",
            "session_key": "approved-session",
        }
    ]

    missing = scope.match_short_task_allowed_source(
        _config(allowed=allowed),
        _identity(session_key=""),
    )
    assert missing["matched"] is False
    assert missing["candidate"] is True
    assert missing["reason"] == "source_identity_incomplete"
    assert scope.decide_gateway_origin(
        _config(allowed=allowed),
        _identity(session_key=""),
    ) == {"authorized": False, "reason": "identity_incomplete"}

    mismatch = scope.match_short_task_allowed_source(
        _config(allowed=allowed),
        _identity(session_key="different-session"),
    )
    assert mismatch == {
        "matched": False,
        "candidate": False,
        "reason": "origin_not_allowed",
    }


def test_missing_allowlist_and_disabled_feature_are_both_closed():
    assert scope.decide_gateway_origin(
        _config(allowed=[]),
        _identity(),
    ) == {"authorized": False, "reason": "allowlist_missing"}
    assert scope.decide_gateway_origin(
        _config(enabled=False),
        _identity(),
    ) == {"authorized": False, "reason": "feature_disabled"}


@pytest.mark.parametrize(
    "allowed",
    [
        "feishu:group-1:user-1",
        [{}],
        [
            {
                "platform": "feishu",
                "chat_type": "group",
                "chat_id": "group-1",
                "user_id": "user-1",
                "wildcard": "*",
            }
        ],
    ],
)
def test_malformed_allowlist_fails_closed(allowed):
    decision = scope.decide_gateway_origin(
        _config(allowed=allowed),
        _identity(),
    )

    assert decision["authorized"] is False
    assert decision["reason"] == "invalid_allowlist"
    assert decision["validation_error"]


def test_frozen_policy_cannot_be_rebound_to_another_actor():
    decision = scope.decide_gateway_origin(_config(), _identity())

    with pytest.raises(ValueError, match="does not match"):
        scope.canonical_task_policy(
            decision["task_policy_json"],
            control_identity=_identity(user_id="user-2"),
        )


def test_local_process_has_no_gateway_authority(monkeypatch):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    assert scope.current_gateway_identity() is None


def test_internal_gateway_turn_has_no_human_control_authority(monkeypatch):
    from gateway import session_context

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    tokens = session_context.set_session_vars(
        platform="feishu",
        chat_type="group",
        chat_id="group-1",
        user_id="user-1",
        session_key="session-1",
        profile="default",
        message_id="copied-message-id",
        internal=True,
    )
    try:
        assert scope.current_gateway_identity() is None
        assert scope.decide_current_gateway_origin()["authorized"] is False
    finally:
        session_context.clear_session_vars(tokens)
