"""Security contract for exact, actor-bound gateway approvals."""

from dataclasses import replace
from dataclasses import FrozenInstanceError
import threading
import time

import pytest


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _start_discord_wait(
    monkeypatch,
    *,
    session_key="session-1",
    hooks=None,
    timeout=2,
    tool_call_id="call-1",
    turn_id="turn-1",
    guild_id="456",
    notify_state=None,
):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: timeout)
    if notify_state is None:
        notify_state = {}
    notified = notify_state.setdefault("notified", [])
    result = {}
    if "callback" not in notify_state:
        notify_state["callback"] = lambda data: notified.append(data)
        notify_state["epoch"] = approval.register_gateway_notify(
            session_key,
            notify_state["callback"],
        )
    notify_cb = notify_state["callback"]
    notify_epoch = notify_state["epoch"]
    notified_index = len(notified)
    if hooks is not None:
        monkeypatch.setattr(
            approval,
            "_fire_approval_hook",
            lambda name, **kwargs: hooks.append((name, kwargs)),
        )

    def worker():
        session_tokens = set_session_vars(
            platform="discord",
            chat_id="123",
            scope_id=guild_id,
            user_id="operator-1",
            session_key=session_key,
            message_id="source-message-1",
        )
        session_token = approval.set_current_session_key(session_key)
        observation_tokens = approval.set_current_observability_context(
            turn_id=turn_id, tool_call_id=tool_call_id
        )
        notify_token = approval.set_current_gateway_notify_epoch(
            notify_epoch
        )
        try:
            result["value"] = approval._await_gateway_decision(
                session_key,
                notify_cb,
                {
                    "command": "<transfer> (plugin approval rule)",
                    "description": "Confirm one destructive operation",
                    "pattern_key": "plugin_rule:finance-transfer",
                    "pattern_keys": ["plugin_rule:finance-transfer"],
                    "tool_name": "transfer",
                    "tool_args": {"amount": 1000, "currency": "KRW"},
                    "plugin_identity": "finance-policy",
                    "tool_call_id": tool_call_id,
                    "turn_id": turn_id,
                    "decision_scope": "once",
                    "risk_class": "financial",
                    "allow_permanent": False,
                    "allow_session": False,
                },
            )
        finally:
            approval.reset_current_gateway_notify_epoch(notify_token)
            approval.reset_current_observability_context(observation_tokens)
            approval.reset_current_session_key(session_token)
            clear_session_vars(session_tokens)

    thread = threading.Thread(target=worker)
    thread.start()
    assert _wait_until(lambda: len(notified) > notified_index)
    return thread, notified[notified_index], result


def _bind_and_resolve(
    data,
    *,
    session_key="session-1",
    prompt_id="prompt-1",
    choice="once",
    **overrides,
):
    from tools.approval import (
        bind_gateway_approval_delivery,
        resolve_gateway_approval,
    )

    assert bind_gateway_approval_delivery(
        session_key,
        data["approval_id"],
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id=prompt_id,
    )
    values = {
        "approval_id": data["approval_id"],
        "actor_id": "operator-1",
        "actor_authorized": True,
        "platform": "discord",
        "guild_id": "456",
        "channel_id": "123",
        "message_id": prompt_id,
        "tool_call_id": data["tool_call_id"],
        "turn_id": data["turn_id"],
        "plugin_identity": data["plugin_identity"],
        "tool_name": data["tool_name"],
        "canonical_arguments_digest": data["canonical_arguments_digest"],
    }
    values.update(overrides)
    return resolve_gateway_approval(session_key, choice, **values)


def test_random_ids_actor_success_and_immutable_post_attestation(monkeypatch):
    hooks = []
    thread, data, result = _start_discord_wait(monkeypatch, hooks=hooks)

    assert data["approval_id"]
    assert _bind_and_resolve(data) == 1
    thread.join(timeout=2)
    assert not thread.is_alive()

    decision = result["value"]
    attestation = decision["attestation"]
    assert decision["choice"] == "once"
    assert attestation.decision is True
    assert attestation.choice == "once"
    assert attestation.actor_id == "operator-1"
    assert attestation.source_operator_id == "operator-1"
    assert attestation.tool_call_id == "call-1"
    assert attestation.turn_id == "turn-1"
    assert attestation.plugin_identity == "finance-policy"
    assert attestation.tool_name == "transfer"
    assert attestation.source_message_id == "source-message-1"
    assert len(attestation.canonical_arguments_digest) == 64
    with pytest.raises(FrozenInstanceError):
        attestation.actor_id = "forged"

    post = next(kwargs for name, kwargs in hooks if name == "post_approval_response")
    assert post["attestation"] is attestation
    assert post["choice"] == attestation.choice
    assert "tool_args" not in data
    assert "amount" not in repr(data)


def test_random_approval_id_uniqueness(monkeypatch):
    waits = [_start_discord_wait(monkeypatch, session_key=f"s-{i}") for i in range(8)]
    ids = [data["approval_id"] for _, data, _ in waits]
    assert len(set(ids)) == len(ids)
    for i, (thread, data, _) in enumerate(waits):
        assert _bind_and_resolve(data, session_key=f"s-{i}") == 1
        thread.join(timeout=2)

def test_discord_dm_uses_exact_empty_guild_binding(monkeypatch):
    from tools.approval import (
        bind_gateway_approval_delivery,
        resolve_gateway_approval,
    )

    thread, data, result = _start_discord_wait(
        monkeypatch,
        session_key="discord-dm",
        guild_id="",
    )
    assert data["source_guild_id"] == ""
    assert bind_gateway_approval_delivery(
        "discord-dm",
        data["approval_id"],
        platform="discord",
        guild_id="",
        channel_id="123",
        message_id="dm-prompt-1",
    )
    assert resolve_gateway_approval(
        "discord-dm",
        "once",
        approval_id=data["approval_id"],
        actor_id="operator-1",
        actor_authorized=True,
        platform="discord",
        guild_id="",
        channel_id="123",
        message_id="dm-prompt-1",
        tool_call_id=data["tool_call_id"],
        turn_id=data["turn_id"],
        plugin_identity=data["plugin_identity"],
        tool_name=data["tool_name"],
        canonical_arguments_digest=data[
            "canonical_arguments_digest"
        ],
    ) == 1
    thread.join(timeout=2)
    assert result["value"]["attestation"].source_guild_id == ""
    assert result["value"]["attestation"].interaction_guild_id == ""


def test_raw_terminal_arguments_bind_digest_without_transport_leak(
    monkeypatch,
):
    import tools.approval as approval

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 1)
    session_key = "raw-terminal-binding"
    raw_command = (
        "curl -H 'Authorization: Bearer top-secret' example.test"
    )
    displayed_command = (
        "curl -H 'Authorization: Bearer [REDACTED]' example.test"
    )
    notified = []

    def deny(data):
        notified.append(data)
        assert approval.resolve_gateway_approval(
            session_key,
            "deny",
            approval_id=data["approval_id"],
        ) == 1

    notify_epoch = approval.register_gateway_notify(session_key, deny)
    result = approval._await_gateway_decision(
        session_key,
        deny,
        {
            "command": displayed_command,
            "tool_args": {"command": raw_command},
            "tool_name": "terminal",
            "pattern_key": "shell-c",
        },
        notify_epoch=notify_epoch,
    )

    assert result["choice"] == "deny"
    assert notified[0]["canonical_arguments_digest"] == (
        approval.canonical_tool_arguments_digest(
            {"command": raw_command}
        )
    )
    assert "tool_args" not in notified[0]
    assert "top-secret" not in repr(notified[0])


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("actor_id", "wrong-actor"),
        ("actor_authorized", False),
        ("platform", "telegram"),
        ("guild_id", "wrong-guild"),
        ("channel_id", "wrong-channel"),
        ("message_id", "wrong-message"),
        ("tool_call_id", "wrong-call"),
        ("turn_id", "wrong-turn"),
        ("plugin_identity", "wrong-plugin"),
        ("tool_name", "wrong-tool"),
        ("canonical_arguments_digest", "0" * 64),
    ],
)
def test_mismatched_binding_is_rejected_without_consuming_waiter(
    monkeypatch, field, wrong_value
):
    thread, data, _ = _start_discord_wait(monkeypatch)
    assert _bind_and_resolve(data, **{field: wrong_value}) == 0
    # A correct response can still resolve the untouched waiter.
    from tools.approval import resolve_gateway_approval

    assert resolve_gateway_approval(
        "session-1",
        "once",
        approval_id=data["approval_id"],
        actor_id="operator-1",
        actor_authorized=True,
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
        tool_call_id=data["tool_call_id"],
        turn_id=data["turn_id"],
        plugin_identity=data["plugin_identity"],
        tool_name=data["tool_name"],
        canonical_arguments_digest=data["canonical_arguments_digest"],
    ) == 1
    thread.join(timeout=2)


def test_discord_delivery_must_match_request_source(monkeypatch):
    import tools.approval as approval

    thread, data, _ = _start_discord_wait(monkeypatch)
    assert approval.bind_gateway_approval_delivery(
        "session-1",
        data["approval_id"],
        platform="discord",
        guild_id="456",
        channel_id="wrong-channel",
        message_id="prompt-wrong",
    ) is False
    assert _bind_and_resolve(data) == 1
    thread.join(timeout=2)


@pytest.mark.parametrize(
    ("tool_call_id", "turn_id"),
    [
        ("", "turn-1"),
        ("call-1", ""),
    ],
)
def test_discord_approval_rejects_missing_observability_ids(
    monkeypatch, tool_call_id, turn_id
):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    notified = []
    tokens = set_session_vars(
        platform="discord",
        chat_id="123",
        scope_id="456",
        user_id="operator-1",
        session_key="session-1",
        message_id="source-message-1",
    )
    observation_tokens = approval.set_current_observability_context(
        turn_id=turn_id,
        tool_call_id=tool_call_id,
    )
    notify_cb = lambda data: notified.append(data)
    notify_epoch = approval.register_gateway_notify(
        "session-1",
        notify_cb,
    )
    try:
        result = approval._await_gateway_decision(
            "session-1",
            notify_cb,
            {
                "command": "<transfer>",
                "pattern_key": "plugin_rule:finance",
                "tool_name": "transfer",
                "tool_args": {"amount": 1},
                "plugin_identity": "finance-policy",
                "tool_call_id": tool_call_id,
                "turn_id": turn_id,
                "decision_scope": "once",
            },
            notify_epoch=notify_epoch,
        )
    finally:
        approval.reset_current_observability_context(observation_tokens)
        clear_session_vars(tokens)

    assert result["resolved"] is False
    assert result["invalid_context"] is True
    assert result["attestation"].choice == "invalid_context"
    assert notified == []
    assert approval.has_blocking_approval("session-1") is False


def test_wrong_session_unknown_id_and_replay_rejected(monkeypatch):
    from tools.approval import resolve_gateway_approval

    thread, data, _ = _start_discord_wait(monkeypatch)
    assert resolve_gateway_approval(
        "wrong-session", "once", approval_id=data["approval_id"]
    ) == 0
    assert resolve_gateway_approval(
        "session-1", "once", approval_id="wrong-id"
    ) == 0
    assert _bind_and_resolve(data) == 1
    assert resolve_gateway_approval(
        "session-1", "once", approval_id=data["approval_id"]
    ) == 0
    thread.join(timeout=2)

def test_unknown_choice_is_rejected_without_consuming_waiter(monkeypatch):
    from tools.approval import (
        bind_gateway_approval_delivery,
        resolve_gateway_approval,
    )

    thread, data, _ = _start_discord_wait(monkeypatch)
    assert bind_gateway_approval_delivery(
        "session-1",
        data["approval_id"],
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
    )
    binding = {
        "approval_id": data["approval_id"],
        "actor_id": "operator-1",
        "actor_authorized": True,
        "platform": "discord",
        "guild_id": "456",
        "channel_id": "123",
        "message_id": "prompt-1",
        "tool_call_id": data["tool_call_id"],
        "turn_id": data["turn_id"],
        "plugin_identity": data["plugin_identity"],
        "tool_name": data["tool_name"],
        "canonical_arguments_digest": data[
            "canonical_arguments_digest"
        ],
    }

    assert resolve_gateway_approval(
        "session-1",
        "bogus",
        **binding,
    ) == 0
    assert thread.is_alive()
    assert resolve_gateway_approval(
        "session-1",
        "once",
        **binding,
    ) == 1
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_once_only_backend_rejects_persistent_and_smart_choices(monkeypatch):
    from tools.approval import resolve_gateway_approval

    thread, data, _ = _start_discord_wait(monkeypatch)
    assert _bind_and_resolve(data, choice="session") == 0
    for choice in ("always", "smart_approve"):
        assert resolve_gateway_approval(
            "session-1",
            choice,
            approval_id=data["approval_id"],
            actor_id="operator-1",
            actor_authorized=True,
            platform="discord",
            guild_id="456",
            channel_id="123",
            message_id="prompt-1",
            tool_call_id=data["tool_call_id"],
            turn_id=data["turn_id"],
            plugin_identity=data["plugin_identity"],
            tool_name=data["tool_name"],
            canonical_arguments_digest=data["canonical_arguments_digest"],
        ) == 0
    assert resolve_gateway_approval(
        "session-1",
        "once",
        approval_id=data["approval_id"],
        actor_id="operator-1",
        actor_authorized=True,
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
        tool_call_id=data["tool_call_id"],
        turn_id=data["turn_id"],
        plugin_identity=data["plugin_identity"],
        tool_name=data["tool_name"],
        canonical_arguments_digest=data["canonical_arguments_digest"],
    ) == 1
    thread.join(timeout=2)


def test_same_session_concurrent_approvals_are_isolated(monkeypatch):
    notify_state = {}
    first = _start_discord_wait(
        monkeypatch,
        notify_state=notify_state,
    )
    second = _start_discord_wait(
        monkeypatch,
        notify_state=notify_state,
    )
    t1, d1, r1 = first
    t2, d2, r2 = second
    assert d1["approval_id"] != d2["approval_id"]
    assert _bind_and_resolve(d2, prompt_id="prompt-2") == 1
    assert _wait_until(lambda: "value" in r2)
    assert "value" not in r1
    assert _bind_and_resolve(d1, prompt_id="prompt-1") == 1
    t1.join(timeout=2)
    t2.join(timeout=2)


def test_expired_waiter_times_out_and_cannot_be_replayed(monkeypatch):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0.05)
    notified = []
    result = {}
    notify_cb = lambda data: notified.append(data)
    notify_epoch = approval.register_gateway_notify(
        "session-1",
        notify_cb,
    )

    def worker():
        tokens = set_session_vars(
            platform="discord",
            chat_id="123",
            scope_id="456",
            user_id="operator-1",
            session_key="session-1",
            message_id="source-message-1",
        )
        try:
            result["value"] = approval._await_gateway_decision(
                "session-1",
                notify_cb,
                {
                    "command": "<transfer>",
                    "pattern_key": "plugin_rule:finance",
                    "tool_name": "transfer",
                    "tool_args": {"amount": 1},
                    "plugin_identity": "finance-policy",
                    "tool_call_id": "call-1",
                    "turn_id": "turn-1",
                    "decision_scope": "once",
                },
                notify_epoch=notify_epoch,
            )
        finally:
            clear_session_vars(tokens)

    thread = threading.Thread(target=worker)
    thread.start()
    assert _wait_until(lambda: notified)
    assert approval.bind_gateway_approval_delivery(
        "session-1",
        notified[0]["approval_id"],
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
    )
    time.sleep(0.08)
    assert approval.resolve_gateway_approval(
        "session-1",
        "once",
        approval_id=notified[0]["approval_id"],
        actor_id="operator-1",
        actor_authorized=True,
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        plugin_identity="finance-policy",
        tool_name="transfer",
        canonical_arguments_digest=notified[0][
            "canonical_arguments_digest"
        ],
    ) == 0
    thread.join(timeout=2)
    assert result["value"]["attestation"].choice == "timeout"
    assert result["value"]["attestation"].decision is False


def test_notify_resolution_then_exception_preserves_terminal_decision(
    monkeypatch,
):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    hooks = []
    monkeypatch.setattr(
        approval,
        "_fire_approval_hook",
        lambda name, **kwargs: hooks.append((name, kwargs)),
    )
    tokens = set_session_vars(
        platform="discord",
        chat_id="123",
        scope_id="456",
        user_id="operator-1",
        session_key="notify-race",
        message_id="source-message-1",
    )
    observation_tokens = approval.set_current_observability_context(
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    def notify_then_raise(data):
        assert approval.bind_gateway_approval_delivery(
            "notify-race",
            data["approval_id"],
            platform="discord",
            guild_id="456",
            channel_id="123",
            message_id="prompt-1",
        )
        assert approval.resolve_gateway_approval(
            "notify-race",
            "once",
            approval_id=data["approval_id"],
            actor_id="operator-1",
            actor_authorized=True,
            platform="discord",
            guild_id="456",
            channel_id="123",
            message_id="prompt-1",
            tool_call_id=data["tool_call_id"],
            turn_id=data["turn_id"],
            plugin_identity=data["plugin_identity"],
            tool_name=data["tool_name"],
            canonical_arguments_digest=data[
                "canonical_arguments_digest"
            ],
        ) == 1
        raise RuntimeError("transport failed after committed response")

    notify_epoch = approval.register_gateway_notify(
        "notify-race",
        notify_then_raise,
    )
    try:
        result = approval._await_gateway_decision(
            "notify-race",
            notify_then_raise,
            {
                "command": "<transfer>",
                "pattern_key": "plugin_rule:finance",
                "tool_name": "transfer",
                "tool_args": {"amount": 1},
                "plugin_identity": "finance-policy",
                "tool_call_id": "call-1",
                "turn_id": "turn-1",
                "decision_scope": "once",
            },
            notify_epoch=notify_epoch,
        )
    finally:
        approval.reset_current_observability_context(observation_tokens)
        clear_session_vars(tokens)

    assert result["resolved"] is True
    assert result["choice"] == "once"
    assert result["attestation"].choice == "once"
    post = [
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    ]
    assert len(post) == 1
    assert post[0]["choice"] == "once"
    assert post[0]["attestation"] is result["attestation"]


@pytest.mark.parametrize(
    "tool_args",
    [
        {"value": object()},
        {"value": {1, 2}},
        {"value": float("nan")},
    ],
)
def test_once_only_noncanonical_arguments_fail_closed_without_prompt(
    tool_args,
):
    import tools.approval as approval

    prompts = []
    result = approval.request_tool_approval(
        "transfer",
        "confirm transfer",
        rule_key="finance",
        approval_callback=lambda *_args, **_kwargs: prompts.append(True),
        tool_args=tool_args,
        plugin_identity="finance-policy",
        tool_call_id="call-1",
        turn_id="turn-1",
        decision_scope="once",
        risk_class="financial",
    )

    assert result["approved"] is False
    assert "canonical JSON digest" in result["message"]
    assert prompts == []
    with pytest.raises(ValueError, match="canonically JSON serializable"):
        approval.canonical_tool_arguments_digest(tool_args, strict=True)


def test_cli_plugin_approval_emits_matching_typed_attestation(monkeypatch):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    hooks = []
    source_tokens = set_session_vars(
        platform="",
        chat_id="cli",
        scope_id="",
        user_id="request-operator",
        session_key="cli-plugin-approval",
        message_id="cli-source-message",
    )
    session_token = approval.set_current_session_key("cli-plugin-approval")
    observation_tokens = approval.set_current_observability_context(
        turn_id="turn-cli-1",
        tool_call_id="call-cli-1",
    )
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(
        approval,
        "_fire_approval_hook",
        lambda name, **kwargs: hooks.append((name, kwargs)),
    )
    try:
        result = approval.request_tool_approval(
            "transfer",
            "confirm transfer",
            rule_key="finance",
            approval_callback=lambda *_args, **_kwargs: "once",
            tool_args={"amount": 1000, "currency": "KRW"},
            plugin_identity="finance-policy",
            tool_call_id="call-cli-1",
            turn_id="turn-cli-1",
            decision_scope="once",
            risk_class="financial",
        )
    finally:
        approval.reset_current_observability_context(observation_tokens)
        approval.reset_current_session_key(session_token)
        clear_session_vars(source_tokens)

    assert result["approved"] is True
    assert result["choice"] == "once"
    request = next(
        kwargs["approval_request"]
        for name, kwargs in hooks
        if name == "pre_approval_request"
    )
    post = next(
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    )
    assert isinstance(request, approval.ApprovalRequest)
    assert isinstance(result["attestation"], approval.ApprovalAttestation)
    assert post["attestation"] is result["attestation"]
    assert post["attestation"].approval_id == request.approval_id
    assert post["attestation"].decision is True
    assert post["attestation"].choice == "once"
    assert post["attestation"].plugin_identity == "finance-policy"
    assert post["attestation"].tool_name == "transfer"
    assert post["attestation"].tool_call_id == "call-cli-1"
    assert post["attestation"].turn_id == "turn-cli-1"
    assert post["attestation"].source_operator_id == "request-operator"
    assert post["attestation"].actor_id == ""


def test_aux_policy_attestation_never_infers_request_operator_as_actor(
    monkeypatch,
):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    hooks = []
    source_tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        scope_id="",
        user_id="request-operator",
        session_key="smart-observer",
        message_id="source-message",
    )
    monkeypatch.setattr(
        approval,
        "_fire_approval_hook",
        lambda name, **kwargs: hooks.append((name, kwargs)),
    )
    try:
        payload = approval._prepare_smart_approval_observer(
            command="echo safe",
            description="observer test",
            pattern_key="observer",
            pattern_keys=["observer"],
            session_key="smart-observer",
        )
        approval._observe_smart_approval_verdict(payload, "approve")
    finally:
        clear_session_vars(source_tokens)

    post = next(
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    )
    assert post["attestation"].source_operator_id == "request-operator"
    assert post["attestation"].actor_id == ""
    assert post["decided_by"] == "aux_llm"


def test_once_only_ignores_yolo_off_mode_and_stored_approvals(
    monkeypatch,
):
    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars

    session_key = "once-policy-floor"
    pattern_key = "plugin_rule:finance"
    notified = []
    original_session = {
        key: set(values)
        for key, values in approval._session_approved.items()
    }
    original_permanent = set(approval._permanent_approved)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(
        approval,
        "is_current_session_yolo_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        approval,
        "_get_approval_mode",
        lambda: "off",
    )
    monkeypatch.setattr(
        approval,
        "_smart_approve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("aux LLM must not decide once-only approval")
        ),
    )
    monkeypatch.setattr(
        approval,
        "save_permanent_allowlist",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("once-only approval must not persist a rule")
        ),
    )
    approval._session_approved.setdefault(session_key, set()).add(pattern_key)
    approval._permanent_approved.add(pattern_key)

    def approve_exact(data):
        notified.append(data)
        assert approval.resolve_gateway_approval(
            session_key,
            "once",
            approval_id=data["approval_id"],
        ) == 1

    notify_epoch = approval.register_gateway_notify(
        session_key,
        approve_exact,
    )
    assert notify_epoch is not None
    notify_token = approval.set_current_gateway_notify_epoch(
        notify_epoch
    )
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    tokens = set_session_vars(
        platform="telegram",
        chat_id="123",
        scope_id="",
        user_id="operator-1",
        session_key=session_key,
        message_id="source-message-1",
    )
    session_token = approval.set_current_session_key(session_key)
    observation_tokens = approval.set_current_observability_context(
        turn_id="turn-1",
        tool_call_id="call-1",
    )
    session_after = set()
    permanent_after = set()
    try:
        result = approval.request_tool_approval(
            "transfer",
            "confirm transfer",
            rule_key="finance",
            tool_args={"amount": 1},
            plugin_identity="finance-policy",
            tool_call_id="call-1",
            turn_id="turn-1",
            decision_scope="once",
            risk_class="financial",
        )
        session_after = set(
            approval._session_approved.get(session_key, set())
        )
        permanent_after = set(approval._permanent_approved)
    finally:
        approval.reset_current_observability_context(observation_tokens)
        approval.reset_current_session_key(session_token)
        approval.reset_current_gateway_notify_epoch(notify_token)
        clear_session_vars(tokens)
        approval.unregister_gateway_notify(session_key, notify_epoch)
        approval._session_approved.clear()
        approval._session_approved.update(original_session)
        approval._permanent_approved.clear()
        approval._permanent_approved.update(original_permanent)

    assert result["approved"] is True
    assert result["choice"] == "once"
    assert len(notified) == 1
    assert session_after == {
        *original_session.get(session_key, set()),
        pattern_key,
    }
    assert permanent_after == {
        *original_permanent,
        pattern_key,
    }


def test_resolve_all_never_approves_an_expired_legacy_waiter():
    import tools.approval as approval

    session_key = "legacy-expired"
    entry = approval._ApprovalEntry(
        {
            "session_key": session_key,
            "command": "echo harmless",
            "pattern_key": "legacy-standard",
        }
    )
    entry.request = replace(
        entry.request,
        session_key=session_key,
        expires_at=time.time() - 1,
    )
    with approval._lock:
        approval._gateway_queues[session_key] = {
            entry.request.approval_id: entry
        }
    try:
        assert (
            approval.resolve_gateway_approval(
                session_key,
                "once",
                resolve_all=True,
            )
            == 0
        )
        assert entry.result is None
        assert entry.attestation is None
        assert entry.event.is_set() is False
    finally:
        with approval._lock:
            approval._gateway_queues.pop(session_key, None)


def test_resolution_commit_is_linearized_against_session_reset(monkeypatch):
    import tools.approval as approval

    session_key = "approval-reset-race"
    notify_epoch = approval.register_gateway_notify(
        session_key,
        lambda _data: None,
    )
    entry = approval._ApprovalEntry(
        {
            "session_key": session_key,
            "command": "echo harmless",
            "pattern_key": "legacy-standard",
        },
        notify_epoch=notify_epoch,
    )
    entry.request = replace(
        entry.request,
        session_key=session_key,
        expires_at=time.time() + 10,
    )
    with approval._lock:
        approval._gateway_queues[session_key] = {
            entry.request.approval_id: entry
        }

    entered_commit = threading.Event()
    release_commit = threading.Event()
    original_attestation_for = approval._attestation_for

    def slow_attestation(*args, **kwargs):
        entered_commit.set()
        assert release_commit.wait(timeout=2)
        return original_attestation_for(*args, **kwargs)

    monkeypatch.setattr(approval, "_attestation_for", slow_attestation)
    monkeypatch.setattr(
        approval,
        "_release_permission_mode_dependents",
        lambda _session_key: None,
    )
    outcomes = {}

    resolver = threading.Thread(
        target=lambda: outcomes.setdefault(
            "resolved",
            approval.resolve_gateway_approval(
                session_key,
                "once",
                approval_id=entry.request.approval_id,
            ),
        )
    )
    resetter = threading.Thread(
        target=lambda: (
            approval.clear_session(session_key),
            outcomes.setdefault("reset", True),
        )
    )
    resolver.start()
    assert entered_commit.wait(timeout=2)
    resetter.start()
    time.sleep(0.05)
    # The reset must block on the same lock until the terminal decision,
    # queue removal, and event signal are all committed.
    assert resetter.is_alive()
    release_commit.set()
    resolver.join(timeout=2)
    resetter.join(timeout=2)

    assert outcomes == {"resolved": 1, "reset": True}
    assert entry.result == "once"
    assert entry.attestation is not None
    assert entry.attestation.choice == "once"
    assert entry.event.is_set()


def test_unregister_before_enqueue_rejects_stale_run(monkeypatch):
    import tools.approval as approval

    session_key = "unregister-before-enqueue"
    notified = []
    notify_cb = lambda data: notified.append(data)
    notify_epoch = approval.register_gateway_notify(
        session_key,
        notify_cb,
    )
    assert approval.unregister_gateway_notify(
        session_key,
        notify_epoch,
    )

    result = approval._await_gateway_decision(
        session_key,
        notify_cb,
        {
            "command": "<destructive operation>",
            "pattern_key": "destructive",
        },
        notify_epoch=notify_epoch,
    )

    assert result["resolved"] is False
    assert result["invalid_context"] is True
    assert result["attestation"].decision is False
    assert notified == []
    assert approval.has_blocking_approval(session_key) is False


def test_callback_replacement_rejects_old_epoch_even_for_same_callback():
    import tools.approval as approval

    session_key = "callback-replacement"
    notified = []
    notify_cb = lambda data: notified.append(data)
    old_epoch = approval.register_gateway_notify(
        session_key,
        notify_cb,
    )
    new_epoch = approval.register_gateway_notify(
        session_key,
        notify_cb,
    )
    assert new_epoch != old_epoch

    result = approval._await_gateway_decision(
        session_key,
        notify_cb,
        {
            "command": "<destructive operation>",
            "pattern_key": "destructive",
        },
        notify_epoch=old_epoch,
    )

    assert result["invalid_context"] is True
    assert notified == []
    assert approval.unregister_gateway_notify(
        session_key,
        old_epoch,
    ) is False
    with approval._lock:
        assert approval._gateway_notify_epochs[session_key] == new_epoch
        assert approval._gateway_notify_cbs[session_key] is notify_cb
    assert approval.unregister_gateway_notify(
        session_key,
        new_epoch,
    ) is True


def test_stale_active_check_cannot_replace_newer_callback():
    import tools.approval as approval

    session_key = "stale-registration-preserves-newer"
    newer_cb = lambda _data: None
    stale_cb = lambda _data: None
    newer_epoch = approval.register_gateway_notify(
        session_key,
        newer_cb,
    )

    stale_epoch = approval.register_gateway_notify(
        session_key,
        stale_cb,
        active_check=lambda: False,
    )

    assert stale_epoch is None
    with approval._lock:
        assert approval._gateway_notify_epochs[session_key] == newer_epoch
        assert approval._gateway_notify_cbs[session_key] is newer_cb
    assert approval.unregister_gateway_notify(
        session_key,
        newer_epoch,
    )


def test_run_invalidation_and_callback_revoke_share_one_lock():
    import tools.approval as approval

    session_key = "atomic-run-invalidation"
    generation = {"value": 1}
    check_entered = threading.Event()
    release_check = threading.Event()
    registered = {}
    invalidated = {}

    def active_check():
        check_entered.set()
        assert release_check.wait(timeout=2)
        return generation["value"] == 1

    register_thread = threading.Thread(
        target=lambda: registered.setdefault(
            "epoch",
            approval.register_gateway_notify(
                session_key,
                lambda _data: None,
                active_check=active_check,
            ),
        )
    )
    register_thread.start()
    assert check_entered.wait(timeout=2)

    def invalidate():
        invalidated["generation"] = approval.invalidate_gateway_notify(
            session_key,
            lambda: generation.__setitem__("value", 2) or 2,
        )

    invalidate_thread = threading.Thread(target=invalidate)
    invalidate_thread.start()
    # Invalidation cannot mutate the generation in the middle of the
    # check/publish critical section.
    time.sleep(0.05)
    assert generation["value"] == 1
    release_check.set()
    register_thread.join(timeout=2)
    invalidate_thread.join(timeout=2)

    assert registered["epoch"] is not None
    assert invalidated == {"generation": 2}
    assert generation["value"] == 2
    with approval._lock:
        assert session_key not in approval._gateway_notify_epochs
        assert session_key not in approval._gateway_notify_cbs

    # An old worker reaching publication only after invalidation is rejected.
    assert approval.register_gateway_notify(
        session_key,
        lambda _data: None,
        active_check=lambda: generation["value"] == 1,
    ) is None


def test_resolution_uses_one_timestamp_for_expiry_and_attestation(
    monkeypatch,
):
    import tools.approval as approval

    session_key = "single-decision-clock"
    notify_epoch = approval.register_gateway_notify(
        session_key,
        lambda _data: None,
    )
    entry = approval._ApprovalEntry(
        {
            "session_key": session_key,
            "command": "echo bounded",
            "pattern_key": "bounded",
        },
        notify_epoch=notify_epoch,
    )
    entry.request = replace(entry.request, expires_at=100.0)
    with approval._lock:
        approval._gateway_queues[session_key] = {
            entry.request.approval_id: entry
        }

    clock_reads = []

    def boundary_clock():
        clock_reads.append(True)
        return 99.95 if len(clock_reads) == 1 else 100.01

    monkeypatch.setattr(approval.time, "time", boundary_clock)
    try:
        resolved = approval.resolve_gateway_approval(
            session_key,
            "once",
            approval_id=entry.request.approval_id,
        )
    finally:
        approval.unregister_gateway_notify(session_key, notify_epoch)

    assert resolved == 1
    assert len(clock_reads) == 1
    assert entry.result == "once"
    assert entry.attestation is not None
    assert entry.attestation.decision is True
    assert entry.attestation.decided_at == 99.95


@pytest.mark.asyncio
async def test_expired_discord_click_never_renders_approved(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    import tools.approval as approval
    from gateway.session_context import clear_session_vars, set_session_vars
    from plugins.platforms.discord.adapter import ExecApprovalView

    session_key = "expired-discord-ui"
    notify_epoch = approval.register_gateway_notify(
        session_key,
        lambda _data: None,
    )
    source_tokens = set_session_vars(
        platform="discord",
        chat_id="123",
        scope_id="456",
        user_id="operator-1",
        session_key=session_key,
        message_id="source-message",
    )
    try:
        entry = approval._ApprovalEntry(
            {
                "session_key": session_key,
                "command": "<transfer>",
                "pattern_key": "finance",
                "tool_call_id": "call-1",
                "turn_id": "turn-1",
                "plugin_identity": "finance-policy",
                "tool_name": "transfer",
                "tool_args": {"amount": 1000},
                "decision_scope": "once",
            },
            notify_epoch=notify_epoch,
        )
    finally:
        clear_session_vars(source_tokens)
    with approval._lock:
        approval._gateway_queues[session_key] = {
            entry.request.approval_id: entry
        }
    assert approval.bind_gateway_approval_delivery(
        session_key,
        entry.request.approval_id,
        platform="discord",
        guild_id="456",
        channel_id="123",
        message_id="prompt-1",
    )
    entry.request = replace(entry.request, expires_at=100.0)
    monkeypatch.setattr(approval.time, "time", lambda: 100.01)

    view = ExecApprovalView(
        session_key=session_key,
        approval_id=entry.request.approval_id,
        source_operator_id="operator-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        plugin_identity="finance-policy",
        tool_name="transfer",
        canonical_arguments_digest=(
            entry.request.canonical_arguments_digest
        ),
        allowed_user_ids={"operator-1"},
        authorization_check=lambda _interaction: (True, ""),
        allow_permanent=False,
        allow_session=False,
    )
    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id="operator-1",
            display_name="Operator",
            roles=[],
        ),
        guild=SimpleNamespace(id="456"),
        guild_id="456",
        channel=SimpleNamespace(id="123"),
        channel_id="123",
        message=SimpleNamespace(id="prompt-1", embeds=[]),
        response=response,
    )
    result_before_cleanup = None
    try:
        await view._resolve(
            interaction,
            "once",
            discord.Color.green(),
            "Approved once",
        )
        result_before_cleanup = entry.result
    finally:
        approval.unregister_gateway_notify(session_key, notify_epoch)

    assert view.resolved is False
    assert result_before_cleanup is None
    response.send_message.assert_awaited_once()
    response.edit_message.assert_not_awaited()
    assert (
        "not accepted"
        in response.send_message.await_args.args[0].lower()
    )


def test_expired_cli_once_attestation_fails_closed(monkeypatch):
    import tools.approval as approval

    hooks = []
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)
    monkeypatch.setattr(
        approval,
        "_fire_approval_hook",
        lambda name, **kwargs: hooks.append((name, kwargs)),
    )
    session_token = approval.set_current_session_key("expired-cli-once")
    try:
        result = approval.request_tool_approval(
            "transfer",
            "confirm transfer",
            rule_key="financial-transfer",
            approval_callback=lambda *_args, **_kwargs: "once",
            tool_args={"amount": 1000},
            plugin_identity="finance-policy",
            tool_call_id="call-expired",
            turn_id="turn-expired",
            decision_scope="once",
            risk_class="financial",
        )
    finally:
        approval.reset_current_session_key(session_token)

    assert result["approved"] is False
    post = next(
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    )
    assert post["choice"] == "once"
    assert post["attestation"].choice == "once"
    assert post["attestation"].decision is False
    assert (
        post["attestation"].decided_at
        >= post["attestation"].expires_at
    )


@pytest.mark.parametrize(
    "cancel",
    [
        lambda approval, session_key: approval.clear_session(session_key),
        lambda approval, session_key: approval.unregister_gateway_notify(
            session_key
        ),
    ],
    ids=["clear-session", "unregister-notify"],
)
def test_session_cancellation_commits_explicit_deny_attestation(
    monkeypatch, cancel
):
    import tools.approval as approval

    hooks = []
    session_key = "cancel-attestation"
    monkeypatch.setattr(
        approval,
        "_release_permission_mode_dependents",
        lambda _session_key: None,
    )
    thread, data, result = _start_discord_wait(
        monkeypatch,
        session_key=session_key,
        hooks=hooks,
    )

    cancel(approval, session_key)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result["value"]["resolved"] is True
    assert result["value"]["choice"] == "deny"
    attestation = result["value"]["attestation"]
    assert attestation.approval_id == data["approval_id"]
    assert attestation.choice == "deny"
    assert attestation.decision is False
    post = next(
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    )
    assert post["choice"] == "deny"
    assert post["attestation"] is attestation
    approval.unregister_gateway_notify(session_key)


def test_timeout_edge_uses_lock_committed_cancellation_outcome(monkeypatch):
    import tools.approval as approval

    hooks = []
    session_key = "timeout-cancel-race"
    thread, data, result = _start_discord_wait(
        monkeypatch,
        session_key=session_key,
        hooks=hooks,
        timeout=0.05,
    )

    # Hold the finalization lock across the deadline, then commit the same deny
    # terminal state used by clear_session/unregister. The waiter has already
    # observed its local timeout edge when it next acquires this lock.
    with approval._lock:
        time.sleep(0.08)
        entry = approval._gateway_queues[session_key][data["approval_id"]]
        entry.result = "deny"
        entry.attestation = approval._attestation_for(entry, choice="deny")
        approval._gateway_queues.pop(session_key, None)
        entry.event.set()

    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result["value"]["resolved"] is True
    assert result["value"]["choice"] == "deny"
    assert result["value"]["attestation"].choice == "deny"
    post = next(
        kwargs
        for name, kwargs in hooks
        if name == "post_approval_response"
    )
    assert post["choice"] == "deny"
    assert post["attestation"] is result["value"]["attestation"]


@pytest.mark.asyncio
async def test_gateway_discord_button_waiter_plugin_hook_integration(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    hooks = []
    thread, data, result = _start_discord_wait(monkeypatch, hooks=hooks)

    sent = {}
    prompt = SimpleNamespace(
        id="prompt-1",
        embeds=[],
        edit=AsyncMock(),
    )

    async def send(**kwargs):
        sent.update(kwargs)
        prompt.embeds = [kwargs["embed"]]
        return prompt

    channel = SimpleNamespace(
        id="123",
        guild=SimpleNamespace(id="456"),
        send=AsyncMock(side_effect=send),
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="redacted"))
    adapter._allowed_user_ids = {"operator-1"}
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    delivery = await adapter.send_exec_approval(
        chat_id="123",
        command=data["command"],
        session_key="session-1",
        description=data["description"],
        metadata=data,
        allow_permanent=False,
        allow_session=False,
    )
    assert delivery.success is True

    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id="operator-1", display_name="Operator", roles=[]
        ),
        guild=SimpleNamespace(id="456"),
        guild_id="456",
        channel=channel,
        channel_id="123",
        message=prompt,
        response=response,
    )
    await sent["view"]._resolve(
        interaction,
        "once",
        discord.Color.green(),
        "Approved once",
    )
    thread.join(timeout=2)
    assert not thread.is_alive()
    response.edit_message.assert_awaited_once()
    assert result["value"]["attestation"].actor_id == "operator-1"
    assert [name for name, _ in hooks] == [
        "pre_approval_request",
        "post_approval_response",
    ]
