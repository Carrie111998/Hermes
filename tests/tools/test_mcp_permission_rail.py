"""Config-driven MCP prepare/execute permission rail.

All tests use mocked MCP sessions. No real MCP servers, network calls, OAuth,
or live config are touched.
"""

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _future_expiry(seconds=30):
    return time.time() + seconds


def _call_result(payload, *, is_error=False, structured=None):
    if isinstance(payload, dict) and "proposal_token" in payload and "preview" in payload:
        payload = {
            "ok": True,
            "data": {
                "proposal_token": payload["proposal_token"],
                "expires_at": payload.get("expires_at", _future_expiry()),
                "action": {"preview": payload["preview"]},
                **(
                    {"confirmation_code": payload["confirmation_code"]}
                    if "confirmation_code" in payload
                    else {}
                ),
            },
        }
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True)
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        isError=is_error,
        structuredContent=structured,
    )


def _server(name, session):
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask(name)
    server.session = session
    server._tools = []
    return server


def _run_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    return asyncio.run(coro)


def _policy(*, ttl=60.0, deny_execute_tools=None):
    return {
        "mcp_permission_rails": {
            "servers": {
                "dps": {
                    "enabled": True,
                    "ttl_seconds": ttl,
                    "tools": {
                        "prepare": ["proposal.prepare", "alt.prepare"],
                        "execute": ["proposal.execute", "alt.execute"],
                        "status": ["proposal.status"],
                        "read": ["proposal.read"],
                    },
                    "bindings": [
                        {
                            "prepare": "proposal.prepare",
                            "execute": "proposal.execute",
                        },
                        {
                            "prepare": "alt.prepare",
                            "execute": "alt.execute",
                        },
                    ],
                    "deny_execute_tools": list(deny_execute_tools or []),
                    "result_paths": {
                        "proposal_token": ["data.proposal_token"],
                        "preview": ["data.action.preview"],
                        "expires_at": ["data.expires_at"],
                        "confirmation_code": ["data.confirmation_code"],
                    },
                    "execute_token_paths": ["proposal_token"],
                    "execute_confirmation_code_paths": ["confirmation_code"],
                },
                "other": {
                    "enabled": True,
                    "ttl_seconds": ttl,
                    "tools": {
                        "prepare": ["proposal.prepare"],
                        "execute": ["proposal.execute"],
                    },
                    "bindings": [
                        {
                            "prepare": "proposal.prepare",
                            "execute": "proposal.execute",
                        }
                    ],
                    "result_paths": {
                        "proposal_token": ["data.proposal_token"],
                        "preview": ["data.action.preview"],
                        "expires_at": ["data.expires_at"],
                    },
                    "execute_token_paths": ["proposal_token"],
                },
            }
        }
    }


def _dps_policy(**overrides):
    cfg = _policy(**{k: v for k, v in overrides.items() if k in {"ttl", "deny_execute_tools"}})
    server = cfg["mcp_permission_rails"]["servers"]["dps"]
    server["tools"] = {
        "prepare": ["dps_prepare_*"],
        "execute": ["dps_execute_*"],
        "status": ["dps_status_*"],
        "read": ["dps_get_*", "dps_list_*"],
    }
    server.pop("bindings", None)
    server["prepare_prefix"] = "dps_prepare_"
    server["execute_prefix"] = "dps_execute_"
    server["result_paths"] = {
        "proposal_token": ["data.proposal_token"],
        "preview": ["data.action.preview"],
        "expires_at": ["data.expires_at"],
        "confirmation_code": ["data.confirmation_code"],
    }
    server["execute_token_paths"] = ["proposal_token"]
    server["execute_confirmation_code_paths"] = ["confirmation_code"]
    for key, value in overrides.items():
        if key not in {"ttl", "deny_execute_tools"}:
            server[key] = value
    return cfg


def _bind_context(
    *,
    platform="telegram",
    chat_id="chat-1",
    user_id="user-1",
    thread_id="thread-1",
    session_key="sess-1",
    session_id="sid-1",
    message_id="msg-1",
):
    from gateway.session_context import set_session_vars
    from tools.approval import set_current_session_key

    tokens = set_session_vars(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
        session_key=session_key,
        session_id=session_id,
        message_id=message_id,
    )
    approval_token = set_current_session_key(session_key)
    return tokens, approval_token


def _clear_context(tokens, approval_token):
    from gateway.session_context import clear_session_vars
    from tools.approval import reset_current_session_key

    reset_current_session_key(approval_token)
    clear_session_vars(tokens)


def _observed_context(
    *,
    platform="telegram",
    chat_id="chat-1",
    user_id="user-1",
    thread_id="thread-1",
    session_key="sess-1",
    session_id="sid-1",
    message_id="msg-1",
):
    return {
        "platform": platform,
        "chat_id": chat_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "session_key": session_key,
        "session_id": session_id,
        "message_id": message_id,
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _proposal_token(payload_hash=None, binding=None):
    payload_hash = payload_hash or ("a" * 64)
    proposal = {
        "v": 1,
        "iss": "dps-backend",
        "iat": 1000,
        "exp": 1300,
        "jti": "jti-1",
        "oauth_issuer": "https://tenant.example/",
        "subject": "auth0|marcial",
        "operator_id": "11111111-1111-4111-8111-111111111111",
        "actor_role": "marcial",
        "tool_name": "project.document.import",
        "tool_version": 1,
        "target_type": "project",
        "target_id": "123",
        "target_version": None,
        "payload": {"amount": 12.5},
        "payload_hash": payload_hash,
        "confirmation_code": "dale",
        "hermes_binding": binding or {
            "v": 1,
            "prepare_attestation_digest": "b" * 64,
            "prepare_mcp_tool_name": "dps_prepare_project_document_import",
            "base_context_digest": "c" * 64,
            "oauth_client_id": "hermes-client",
            "operator_subject_digest": "d" * 64,
            "originating_user_message_id_digest": "e" * 64,
            "prepare_nonce_digest": "f" * 64,
            "prepare_tool_args_digest": "1" * 64,
            "kid": "h1",
            "iat": 1000,
            "exp": 1300,
        },
    }
    encoded = _b64url(json.dumps(proposal, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64url(hmac.new(b"p" * 64, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}", proposal


def _attested_dps_policy(**overrides):
    cfg = _dps_policy(**overrides)
    cfg["mcp_permission_rails"]["servers"]["dps"]["attestation"] = {
        "enabled": True,
        "kid_env": "DPS_HERMES_ATTESTATION_KID",
        "key_env": "DPS_HERMES_ATTESTATION_KEY",
        "oauth_client_id_env": "DPS_HERMES_OAUTH_CLIENT_ID",
        "operator_subject_env": "DPS_HERMES_OPERATOR_SUBJECT",
    }
    return cfg


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    from tools import approval
    import tools.mcp_tool as mcp_tool

    with mcp_tool._lock:
        saved_servers = dict(mcp_tool._servers)
        mcp_tool._servers.clear()
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()
    approval._session_approved.clear()
    approval._permanent_approved.clear()
    approval._pending.clear()
    try:
        from tools import mcp_permission_rail

        mcp_permission_rail.clear_pending_proposals()
        mcp_permission_rail._known_protected_servers.clear()
    except Exception:
        pass
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    yield
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()
    try:
        from tools import mcp_permission_rail

        mcp_permission_rail.clear_pending_proposals()
        mcp_permission_rail._known_protected_servers.clear()
    except Exception:
        pass
    with mcp_tool._lock:
        mcp_tool._servers.clear()
        mcp_tool._servers.update(saved_servers)


def _handler(server_name, tool_name, session, *, config=None):
    import tools.mcp_tool as mcp_tool

    mcp_tool._servers[server_name] = _server(server_name, session)
    return mcp_tool._make_tool_handler(server_name, tool_name, 120)


def _handler_with_annotations(server_name, tool_name, session, annotations):
    import tools.mcp_tool as mcp_tool

    mcp_tool._servers[server_name] = _server(server_name, session)
    return mcp_tool._make_tool_handler(server_name, tool_name, 120, annotations)


def _prepare(handler, token="tok-1", preview="Safe exact preview"):
    return json.loads(
        handler({"draft": "not logged"})
    )


def test_unconfigured_mcp_execute_passthrough_regression():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("executed"))

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", return_value={}
    ):
        result = json.loads(_handler("dps", "proposal.execute", session)({"x": 1}))

    assert result["result"] == "executed"
    session.call_tool.assert_called_once_with("proposal.execute", arguments={"x": 1})


def test_protected_execute_without_prepare_denies_before_mcp_call():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("should not run"))

    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            result = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    assert "BLOCKED" in result["error"]
    session.call_tool.assert_not_called()


def test_prepare_registers_pending_but_auto_chain_same_turn_waits_for_human():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": "tok-1", "preview": "Import 3 rows"}),
            _call_result("should not execute"),
        ]
    )

    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy(ttl=0.01)
        ):
            prepare_result = json.loads(
                _handler("dps", "proposal.prepare", session)({"draft": "x"})
            )
            execute_result = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "Import 3 rows" in prepare_result["result"]
    assert "error" in execute_result
    assert session.call_tool.call_count == 1


def test_approved_path_calls_execute_once_and_returns_result():
    from tools.approval import register_gateway_notify, resolve_sensitive_gateway_approval

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": "tok-1", "preview": "Create record"}),
            _call_result("executed once"),
        ]
    )
    notified = []

    def notify(data):
        notified.append(dict(data))
        threading.Thread(
            target=lambda: resolve_sensitive_gateway_approval(
                "sess-1",
                "once",
                request_id=data["request_id"],
                observed_context=_observed_context(),
            ),
            daemon=True,
        ).start()

    register_gateway_notify("sess-1", notify)
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            json.loads(_handler("dps", "proposal.prepare", session)({"draft": "x"}))
            result = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert result["result"] == "executed once"
    assert "Create record" in notified[0]["command"]
    assert notified[0]["expected_context"]["session_id"] == "sid-1"
    assert session.call_tool.call_count == 2
    session.call_tool.assert_called_with(
        "proposal.execute", arguments={"proposal_token": "tok-1"}
    )


def test_denial_consumes_pending_and_execute_never_runs():
    from tools.approval import register_gateway_notify, resolve_sensitive_gateway_approval

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": "tok-1", "preview": "Create record"}),
            _call_result("should not execute"),
        ]
    )

    def notify(data):
        threading.Thread(
            target=lambda: resolve_sensitive_gateway_approval(
                "sess-1",
                "deny",
                request_id=data["request_id"],
                observed_context=_observed_context(),
            ),
            daemon=True,
        ).start()

    register_gateway_notify("sess-1", notify)
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            json.loads(_handler("dps", "proposal.prepare", session)({"draft": "x"}))
            denied = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
            replay = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in denied
    assert "denied" in denied["error"].lower()
    assert "error" in replay
    assert session.call_tool.call_count == 1


def test_wrong_token_tool_server_user_chat_thread_session_all_deny_without_execute():
    cases = [
        ("token", {"proposal_token": "wrong"}, {}, "proposal.execute", "dps"),
        ("tool", {"proposal_token": "tok-1"}, {}, "alt.execute", "dps"),
        ("server", {"proposal_token": "tok-1"}, {}, "proposal.execute", "other"),
        ("user", {"proposal_token": "tok-1"}, {"user_id": "user-2"}, "proposal.execute", "dps"),
        ("chat", {"proposal_token": "tok-1"}, {"chat_id": "chat-2"}, "proposal.execute", "dps"),
        ("thread", {"proposal_token": "tok-1"}, {"thread_id": "thread-2"}, "proposal.execute", "dps"),
        ("session_key", {"proposal_token": "tok-1"}, {"session_key": "sess-2"}, "proposal.execute", "dps"),
        ("session_id", {"proposal_token": "tok-1"}, {"session_id": "sid-2"}, "proposal.execute", "dps"),
    ]

    for _name, execute_args, context_override, execute_tool, execute_server in cases:
        tokens, approval_token = _bind_context()
        session = MagicMock()
        session.call_tool = AsyncMock(
            return_value=_call_result({"proposal_token": "tok-1", "preview": "Preview"})
        )
        try:
            with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
                "hermes_cli.config.load_config", return_value=_policy()
            ):
                json.loads(_handler("dps", "proposal.prepare", session)({"draft": "x"}))
        finally:
            _clear_context(tokens, approval_token)

        new_context = {
            "platform": "telegram",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "thread_id": "thread-1",
            "session_key": "sess-1",
            "session_id": "sid-1",
            **context_override,
        }
        tokens, approval_token = _bind_context(**new_context)
        try:
            execute_session = MagicMock()
            execute_session.call_tool = AsyncMock(return_value=_call_result("bad"))
            with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
                "hermes_cli.config.load_config", return_value=_policy()
            ):
                result = json.loads(
                    _handler(execute_server, execute_tool, execute_session)(execute_args)
                )
        finally:
            _clear_context(tokens, approval_token)

        assert "error" in result, _name
        execute_session.call_tool.assert_not_called()


def test_expired_pending_proposal_denies_and_audits_expired(caplog):
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": "tok-1", "preview": "Preview"})
    )
    try:
        with caplog.at_level("INFO", logger="tools.mcp_permission_rail"), patch(
            "tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop
        ), patch("hermes_cli.config.load_config", return_value=_policy(ttl=0.01)):
            json.loads(_handler("dps", "proposal.prepare", session)({"draft": "x"}))
            time.sleep(0.02)
            execute_session = MagicMock()
            execute_session.call_tool = AsyncMock(return_value=_call_result("bad"))
            result = json.loads(
                _handler("dps", "proposal.execute", execute_session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    execute_session.call_tool.assert_not_called()
    assert "event=expired" in "\n".join(r.getMessage() for r in caplog.records)


def test_prepare_parse_failure_fails_closed_and_registers_no_pending():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result({"preview": "No token"}))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            prepare = json.loads(_handler("dps", "proposal.prepare", session)({}))
            execute_session = MagicMock()
            execute_session.call_tool = AsyncMock(return_value=_call_result("bad"))
            execute = json.loads(
                _handler("dps", "proposal.execute", execute_session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in prepare
    assert "proposal_token" in prepare["error"] or "ok is not true" in prepare["error"]
    assert "error" in execute
    execute_session.call_tool.assert_not_called()


@pytest.mark.parametrize(
    "platform,user_id,env",
    [
        ("telegram", "user-1", {"HERMES_CRON_SESSION": "1"}),
        ("telegram", "user-1", {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}),
        ("telegram", "user-1", {"HERMES_KANBAN_TASK": "t_1"}),
        ("desktop", "", {}),
        ("tui", "", {}),
    ],
)
def test_non_human_contexts_fail_closed_for_execute(monkeypatch, platform, user_id, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    tokens, approval_token = _bind_context(platform=platform, user_id=user_id)
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": "tok-1", "preview": "Preview"})
    )
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            json.loads(_handler("dps", "proposal.prepare", session)({}))
            execute_session = MagicMock()
            execute_session.call_tool = AsyncMock(return_value=_call_result("bad"))
            execute = json.loads(
                _handler("dps", "proposal.execute", execute_session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in execute
    assert "Telegram" in execute["error"] or "human" in execute["error"]
    execute_session.call_tool.assert_not_called()


def test_read_status_and_unknown_tools_passthrough_under_policy():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("ok"))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            for tool in ("proposal.read", "proposal.status"):
                result = json.loads(_handler("dps", tool, session)({"x": tool}))
                assert result["result"] == "ok"
    finally:
        _clear_context(tokens, approval_token)

    assert session.call_tool.call_count == 2


def test_config_denylisted_execute_blocks_without_hardcoded_tool_name():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config",
            return_value=_policy(deny_execute_tools=["proposal.execute"]),
        ):
            result = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    assert "disabled by configuration" in result["error"]
    session.call_tool.assert_not_called()


def test_concurrent_execute_consumes_pending_once():
    from tools.approval import register_gateway_notify, resolve_sensitive_gateway_approval

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": "tok-1", "preview": "Preview"}),
            _call_result("executed"),
        ]
    )

    def notify(data):
        time.sleep(0.05)
        resolve_sensitive_gateway_approval(
            "sess-1",
            "once",
            request_id=data["request_id"],
            observed_context=_observed_context(),
        )

    register_gateway_notify("sess-1", notify)
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            json.loads(_handler("dps", "proposal.prepare", session)({}))
            handler = _handler("dps", "proposal.execute", session)
            barrier = threading.Barrier(3)
            results = []

            def run_execute():
                barrier.wait()
                results.append(json.loads(handler({"proposal_token": "tok-1"})))

            thread_contexts = [contextvars.copy_context(), contextvars.copy_context()]
            threads = [
                threading.Thread(target=ctx.run, args=(run_execute,))
                for ctx in thread_contexts
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
    finally:
        _clear_context(tokens, approval_token)

    assert len(results) == 2
    assert sum(1 for result in results if result.get("result") == "executed") == 1
    assert sum(1 for result in results if "error" in result) == 1
    assert session.call_tool.call_count == 2


def test_deny_execute_patterns_apply_before_execute_patterns_and_categories():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))
    cfg = _dps_policy(
        deny_execute_tools=["dps_execute_project_document_import", "dps_get_blocked"]
    )

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", return_value=cfg
    ):
        execute = json.loads(
            _handler("dps", "dps_execute_project_document_import", session)(
                {"proposal_token": "tok-1"}
            )
        )
        read_overlap = json.loads(
            _handler("dps", "dps_get_blocked", session)({})
        )

    assert "disabled by configuration" in execute["error"]
    assert "disabled by configuration" in read_overlap["error"]
    session.call_tool.assert_not_called()


def test_overlap_uses_safe_precedence_execute_before_read():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))
    cfg = _policy()
    cfg["mcp_permission_rails"]["servers"]["dps"]["tools"]["read"].append("proposal.execute")
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=cfg
        ):
            result = json.loads(
                _handler("dps", "proposal.execute", session)(
                    {"proposal_token": "tok-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    assert "no matching unexpired prepare" in result["error"]
    session.call_tool.assert_not_called()


@pytest.mark.parametrize(
    "annotations",
    [
        None,
        {},
        {"readOnlyHint": False},
        {"readOnlyHint": "true"},
    ],
)
def test_mutation_annotation_not_covered_fails_closed_before_mcp_call(annotations):
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", return_value=_policy()
    ):
        result = json.loads(
            _handler_with_annotations("dps", "catalog_like_create", session, annotations)({})
        )

    assert "mutation-capable" in result["error"]
    session.call_tool.assert_not_called()


def test_readonly_annotation_allows_unknown_tool_passthrough():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("ok"))

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", return_value=_policy()
    ):
        result = json.loads(
            _handler_with_annotations("dps", "catalog_like_status", session, {"readOnlyHint": True})({})
        )

    assert result["result"] == "ok"
    session.call_tool.assert_called_once()


def test_config_exception_for_known_protected_server_fails_closed():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))

    with patch("hermes_cli.config.load_config", return_value=_policy()):
        from tools.mcp_permission_rail import before_tool_call

        assert before_tool_call("dps", "proposal.read", {}).policy is not None

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", side_effect=OSError("cannot read")
    ):
        result = json.loads(
            _handler("dps", "proposal.read", session)({})
        )

    assert "failed closed" in result["error"]
    session.call_tool.assert_not_called()


def test_config_exception_never_yields_passthrough_even_without_known_cache():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", side_effect=OSError("cannot read")
    ):
        result = json.loads(_handler("unknown", "whatever", session)({}))

    assert "failed closed" in result["error"]
    session.call_tool.assert_not_called()


def test_invalid_enabled_config_empty_execute_pattern_fails_closed():
    cfg = _policy()
    cfg["mcp_permission_rails"]["servers"]["dps"]["tools"]["execute"] = []
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))

    with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
        "hermes_cli.config.load_config", return_value=cfg
    ):
        result = json.loads(_handler("dps", "proposal.read", session)({}))

    assert "configuration is invalid" in result["error"]
    session.call_tool.assert_not_called()


def test_real_dps_structured_shape_object_preview_text_mirror_and_hook_redaction():
    from tools.approval import register_gateway_notify, resolve_sensitive_gateway_approval

    tokens, approval_token = _bind_context()
    structured = {
        "ok": True,
        "data": {
            "proposal_token": "tok-secret",
            "confirmation_code": "code-secret",
            "expires_at": _future_expiry(30),
            "action": {
                "preview": {
                    "project_id": 123,
                    "customer": "private name",
                    "changes": ["import document"],
                }
            },
        },
    }
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"ok": False, "data": "text mirror should be ignored"}, structured=structured),
            _call_result("executed"),
        ]
    )
    hooks = []
    notified = []

    def notify(data):
        notified.append(dict(data))
        threading.Thread(
            target=lambda: resolve_sensitive_gateway_approval(
                "sess-1",
                "once",
                request_id=data["request_id"],
                observed_context=_observed_context(),
            ),
            daemon=True,
        ).start()

    register_gateway_notify("sess-1", notify)
    try:
        with patch("tools.approval._fire_approval_hook", side_effect=lambda name, **kw: hooks.append((name, kw))), patch(
            "tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop
        ), patch("hermes_cli.config.load_config", return_value=_dps_policy()):
            prepare = json.loads(
                _handler("dps", "dps_prepare_project_document_import", session)({"draft": "x"})
            )
            result = json.loads(
                _handler("dps", "dps_execute_project_document_import", session)(
                    {
                        "proposal_token": "tok-secret",
                        "confirmation_code": "code-secret",
                    }
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert prepare["structuredContent"] == structured
    assert result["result"] == "executed"
    assert "private name" in notified[0]["command"]
    hook_commands = [kw["command"] for _name, kw in hooks]
    assert hook_commands
    assert all("private name" not in command for command in hook_commands)
    assert all("tok-secret" not in command for command in hook_commands)
    assert all("proposal_digest=" in command for command in hook_commands)


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"ok": False, "data": {}}, "ok is not true"),
        (
            {
                "ok": True,
                "data": {
                    "proposal_token": "tok-1",
                    "expires_at": time.time() - 10,
                    "action": {"preview": {"x": 1}},
                },
            },
            "past expires_at",
        ),
        (
            {
                "ok": True,
                "data": {
                    "proposal_token": "tok-1",
                    "expires_at": time.time() + 90000,
                    "action": {"preview": {"x": 1}},
                },
            },
            "overlong expires_at",
        ),
    ],
)
def test_prepare_structured_contract_rejects_bad_backend_results(payload, error):
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("mirror", structured=payload))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_dps_policy()
        ):
            result = json.loads(_handler("dps", "dps_prepare_alpha", session)({}))
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    assert error in result["error"]


def test_confirmation_code_mismatch_denies_after_consuming_pending():
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result(
            {"proposal_token": "tok-1", "preview": {"x": 1}, "confirmation_code": "code-1"}
        )
    )
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_policy()
        ):
            json.loads(_handler("dps", "proposal.prepare", session)({}))
            execute_session = MagicMock()
            execute_session.call_tool = AsyncMock(return_value=_call_result("bad"))
            result = json.loads(
                _handler("dps", "proposal.execute", execute_session)(
                    {"proposal_token": "tok-1", "confirmation_code": "wrong"}
                )
            )
            replay = json.loads(
                _handler("dps", "proposal.execute", execute_session)(
                    {"proposal_token": "tok-1", "confirmation_code": "code-1"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "confirmation_code" in result["error"]
    assert "no matching unexpired prepare" in replay["error"]
    execute_session.call_tool.assert_not_called()


def test_pending_caps_replace_duplicates_and_cleanup_session():
    from tools import mcp_permission_rail

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": "tok-1", "preview": {"n": 1}}),
            _call_result({"proposal_token": "tok-1", "preview": {"n": 2}}),
            _call_result({"proposal_token": "tok-2", "preview": {"n": 3}}),
        ]
    )
    cfg = _policy()
    cfg["mcp_permission_rails"]["servers"]["dps"]["max_pending_per_session"] = 1
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=cfg
        ):
            handler = _handler("dps", "proposal.prepare", session)
            json.loads(handler({}))
            json.loads(handler({}))
            assert len(mcp_permission_rail._pending) == 1
            assert '"n":2' in mcp_permission_rail._pending[0].preview
            json.loads(handler({}))
            assert len(mcp_permission_rail._pending) == 1
            mcp_permission_rail.clear_session_pending_proposals("sess-1")
            assert mcp_permission_rail._pending == []
    finally:
        _clear_context(tokens, approval_token)


def _decode_attestation_from_meta(meta):
    encoded = meta["dps.hermes.attestation"].split(".")[1]
    padded = encoded + ("=" * (-len(encoded) % 4))
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def _attestation_env(monkeypatch, *, key="k" * 64, kid="h1", client="hermes-client", subject="auth0|marcial"):
    monkeypatch.setenv("DPS_HERMES_ATTESTATION_KID", kid)
    monkeypatch.setenv("DPS_HERMES_ATTESTATION_KEY", key)
    monkeypatch.setenv("DPS_HERMES_OAUTH_CLIENT_ID", client)
    monkeypatch.setenv("DPS_HERMES_OPERATOR_SUBJECT", subject)


def test_a2_prepare_attestation_is_meta_only_and_strips_model_spoof(monkeypatch):
    from tools.hermes_attestation import canonical_hermes_tool_args_digest

    _attestation_env(monkeypatch)
    token, _proposal = _proposal_token()
    tokens, approval_token = _bind_context(message_id="telegram-msg-9")
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": token, "preview": {"x": 1}})
    )
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_attested_dps_policy()
        ):
            result = json.loads(
                _handler("dps", "dps_prepare_project_document_import", session)(
                    {
                        "draft": "x",
                        "hermes_attestation": "model-spoof",
                        "__dps_hermes_attestation": "model-spoof-2",
                    }
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" not in result
    session.call_tool.assert_called_once()
    _name, _args, kwargs = session.call_tool.mock_calls[0]
    assert kwargs["arguments"] == {"draft": "x"}
    assert set(kwargs["meta"]) == {"dps.hermes.attestation"}
    payload = _decode_attestation_from_meta(kwargs["meta"])
    assert payload["phase"] == "prepare"
    assert payload["kid"] == "h1"
    assert payload["oauth_client_id"] == "hermes-client"
    assert payload["operator_subject"] == "auth0|marcial"
    assert payload["platform"] == "telegram"
    assert payload["originating_user_message_id"] == "telegram-msg-9"
    assert payload["tool_args_digest"] == canonical_hermes_tool_args_digest({"draft": "x"})
    assert payload["exp"] - payload["iat"] <= 300


@pytest.mark.parametrize(
    "env_patch,error_fragment",
    [
        ({"DPS_HERMES_ATTESTATION_KEY": ""}, "key env is missing"),
        ({"DPS_HERMES_ATTESTATION_KEY": "short"}, "key is weak"),
        ({"DPS_HERMES_ATTESTATION_KID": "bad kid"}, "kid is invalid"),
        ({"DPS_HERMES_OAUTH_CLIENT_ID": ""}, "oauth_client_id env is missing"),
        ({"DPS_HERMES_OPERATOR_SUBJECT": ""}, "operator_subject env is missing"),
    ],
)
def test_a2_attestation_missing_or_weak_env_fails_before_mcp(monkeypatch, env_patch, error_fragment):
    _attestation_env(monkeypatch)
    for key, value in env_patch.items():
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_attested_dps_policy()
        ):
            result = json.loads(_handler("dps", "dps_prepare_project_document_import", session)({}))
    finally:
        _clear_context(tokens, approval_token)

    assert error_fragment in result["error"]
    session.call_tool.assert_not_called()


def test_a2_attestation_requires_trusted_originating_message_id(monkeypatch):
    _attestation_env(monkeypatch)
    tokens, approval_token = _bind_context(message_id="")
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_call_result("bad"))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_attested_dps_policy()
        ):
            result = json.loads(
                _handler("dps", "dps_prepare_project_document_import", session)(
                    {"HERMES_SESSION_MESSAGE_ID": "model-spoof"}
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "originating message_id" in result["error"]
    session.call_tool.assert_not_called()


@pytest.mark.parametrize(
    "token_case,error",
    [
        ("malformed", "malformed"),
        ("oversized", "oversized"),
        ("missing_binding", "hermes binding"),
    ],
    ids=["malformed", "oversized", "missing_binding"],
)
def test_a2_prepare_rejects_malformed_proposal_metadata(monkeypatch, token_case, error):
    if token_case == "malformed":
        token = "not-base64.sig"
    elif token_case == "oversized":
        token = "x" * 48_001
    else:
        token = _proposal_token(binding=None)[0].split(".")[0] + ".sig"
        raw = json.loads(base64.urlsafe_b64decode((token.split(".")[0] + "=" * (-len(token.split(".")[0]) % 4)).encode("ascii")).decode("utf-8"))
        raw.pop("hermes_binding", None)
        token = _b64url(json.dumps(raw, separators=(",", ":")).encode("utf-8")) + ".sig"
    _attestation_env(monkeypatch)
    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": token, "preview": {"x": 1}})
    )
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_attested_dps_policy()
        ):
            result = json.loads(_handler("dps", "dps_prepare_project_document_import", session)({}))
    finally:
        _clear_context(tokens, approval_token)

    assert error in result["error"]


def test_a2_execute_attestation_contains_approval_and_proposal_digests(monkeypatch):
    from tools.approval import register_gateway_notify, resolve_sensitive_gateway_approval
    from tools.hermes_attestation import canonical_hermes_tool_args_digest, canonical_proposal_digest

    _attestation_env(monkeypatch)
    token, proposal = _proposal_token(payload_hash="f" * 64)
    tokens, approval_token = _bind_context(message_id="msg-execute")
    session = MagicMock()
    session.call_tool = AsyncMock(
        side_effect=[
            _call_result({"proposal_token": token, "preview": {"x": 1}}),
            _call_result({"ok": True}),
        ]
    )
    notified = []

    def notify(data):
        notified.append(dict(data))
        threading.Thread(
            target=lambda: resolve_sensitive_gateway_approval(
                "sess-1",
                "once",
                request_id=data["request_id"],
                observed_context=_observed_context(message_id="msg-execute"),
            ),
            daemon=True,
        ).start()

    register_gateway_notify("sess-1", notify)
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=_attested_dps_policy()
        ):
            json.loads(_handler("dps", "dps_prepare_project_document_import", session)({"draft": "x"}))
            result = json.loads(
                _handler("dps", "dps_execute_project_document_import", session)(
                    {
                        "proposal_token": token,
                        "confirmation_code": "dale",
                        "dps_hermes_attestation": "model-spoof",
                    }
                )
            )
    finally:
        _clear_context(tokens, approval_token)

    assert "error" not in result
    assert notified[0]["allow_session"] is False
    assert notified[0]["allow_permanent"] is False
    _name, _args, kwargs = session.call_tool.mock_calls[-1]
    assert kwargs["arguments"] == {"proposal_token": token, "confirmation_code": "dale"}
    payload = _decode_attestation_from_meta(kwargs["meta"])
    assert payload["phase"] == "execute"
    assert payload["approval_request_id"] == notified[0]["request_id"]
    assert payload["approval_event_id"].startswith("ape_")
    assert payload["approved_at"].endswith("Z")
    assert payload["proposal_token_digest"] == canonical_hermes_tool_args_digest(token)
    assert payload["proposal_digest"] == canonical_proposal_digest(proposal)
    assert payload["proposal_payload_digest"] == "f" * 64
    assert payload["tool_args_digest"] == canonical_hermes_tool_args_digest(
        {"proposal_token": token, "confirmation_code": "dale"}
    )


def test_prepare_preview_exact_max_registers_pending():
    from tools import mcp_permission_rail

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": "tok-1", "preview": "x" * 12})
    )
    cfg = _policy()
    cfg["mcp_permission_rails"]["servers"]["dps"]["max_preview_chars"] = 14
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=cfg
        ):
            result = json.loads(_handler("dps", "proposal.prepare", session)({}))
    finally:
        _clear_context(tokens, approval_token)

    assert "error" not in result
    assert len(mcp_permission_rail._pending) == 1
    assert mcp_permission_rail._pending[0].preview == '"xxxxxxxxxxxx"'


def test_prepare_preview_max_plus_one_blocks_without_pending_or_approval():
    from tools import mcp_permission_rail
    from tools.approval import register_gateway_notify

    tokens, approval_token = _bind_context()
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_call_result({"proposal_token": "tok-1", "preview": "x" * 13})
    )
    cfg = _policy()
    cfg["mcp_permission_rails"]["servers"]["dps"]["max_preview_chars"] = 14
    notified = []
    register_gateway_notify("sess-1", lambda data: notified.append(dict(data)))
    try:
        with patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_mcp_loop), patch(
            "hermes_cli.config.load_config", return_value=cfg
        ):
            result = json.loads(_handler("dps", "proposal.prepare", session)({}))
    finally:
        _clear_context(tokens, approval_token)

    assert "error" in result
    assert "BLOCKED" in result["error"]
    assert "narrow" in result["error"]
    assert "DPS Work" in result["error"]
    assert mcp_permission_rail._pending == []
    assert notified == []
