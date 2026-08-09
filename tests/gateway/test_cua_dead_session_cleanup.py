from __future__ import annotations

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def runner():
    from gateway.run import GatewayRunner

    instance = GatewayRunner.__new__(GatewayRunner)
    instance._cleanup_agent_resources = MagicMock()
    return instance


def _agent(session_id: str):
    return SimpleNamespace(
        session_id=session_id,
        release_clients=MagicMock(),
        _session_messages=[{"role": "user", "content": "x"}],
        _db_flush_scan_prefix=[{"role": "user", "content": "x"}],
    )


def test_gateway_init_fails_closed_without_clobbering_existing_validator(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from tools.computer_use import (
        publish_computer_use_session,
        set_computer_use_session_validator,
        unpublish_computer_use_session,
    )

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    set_computer_use_session_validator(
        lambda route_key, sid: route_key == "existing-route" and sid == "existing"
    )
    try:
        with patch(
            "tools.computer_use.write_computer_use_runtime_attestation",
            side_effect=OSError("attestation storage unavailable"),
        ):
            with pytest.raises(RuntimeError, match="runtime attestation"):
                GatewayRunner(config)

        with pytest.raises(RuntimeError, match="authoritative route"):
            publish_computer_use_session("existing", route_key="wrong-route")
        publication = publish_computer_use_session(
            "existing", route_key="existing-route"
        )
        unpublish_computer_use_session(publication)
    finally:
        set_computer_use_session_validator(None)


def test_soft_evict_hard_release_uses_exact_agent_sid_and_generation(runner):
    from tools.computer_use.tool import ComputerUseReleaseResult

    agent = _agent("dead-session")
    result = ComputerUseReleaseResult(
        session_id="dead-session",
        generation=7,
        status="released",
        reason="dead_cached_session",
    )
    future: Future = Future()
    future.set_result(result)
    with patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=future,
    ) as release:
        returned = runner._release_evicted_agent_soft(
            agent,
            computer_use_release=("dead-session", 7),
        )

    release.assert_called_once_with(
        "dead-session",
        expected_generation=7,
        timeout=5.0,
        reason="dead_cached_session",
        max_attempts=3,
        retry_delay=0.25,
    )
    assert returned is future
    agent.release_clients.assert_called_once_with()
    assert agent._session_messages == []
    assert agent._db_flush_scan_prefix is None


@pytest.mark.parametrize(
    "hard_request",
    [
        ("", 1),
        ("dead-session", 0),
        ("dead-session", -1),
        ("dead-session", True),
        ("different-session", 1),
    ],
)
def test_invalid_hard_release_identity_is_rejected_but_generic_cleanup_continues(
    runner, hard_request
):
    agent = _agent("dead-session")
    with patch(
        "tools.computer_use.submit_computer_use_session_release"
    ) as release:
        returned = runner._release_evicted_agent_soft(
            agent,
            computer_use_release=hard_request,
        )

    release.assert_not_called()
    assert returned is None
    agent.release_clients.assert_called_once_with()


def test_failed_exact_release_is_truthful_and_does_not_skip_agent_cleanup(runner):
    from tools.computer_use.tool import ComputerUseReleaseResult

    agent = _agent("dead-session")
    result = ComputerUseReleaseResult(
        session_id="dead-session",
        generation=4,
        status="timed_out",
        reason="dead_cached_session",
        error="in-flight call did not quiesce",
    )
    future: Future = Future()
    future.set_result(result)
    with patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=future,
    ):
        returned = runner._release_evicted_agent_soft(
            agent,
            computer_use_release=("dead-session", 4),
        )

    assert returned is future
    assert returned.result().released is False
    agent.release_clients.assert_called_once_with()


def test_hard_release_is_submitted_without_blocking_agent_cleanup(runner):
    agent = _agent("dead-session")
    pending: Future = Future()
    with patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=pending,
    ) as submit:
        returned = runner._release_evicted_agent_soft(
            agent,
            computer_use_release=("dead-session", 9),
        )

    assert returned is pending
    assert pending.done() is False
    submit.assert_called_once()
    agent.release_clients.assert_called_once_with()


def test_gateway_wires_terminal_transition_into_session_store(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    token = object()

    with patch.object(
        runner, "_begin_terminal_computer_use_sync", return_value=token
    ) as begin, patch.object(
        runner, "_end_terminal_computer_use_sync"
    ) as end:
        returned = runner.session_store._before_auto_reset_fn(
            "old-session", "suspended"
        )
        runner.session_store._after_auto_reset_fn(
            "old-session", returned, True
        )

    begin.assert_called_once_with(
        "old-session", reason="auto_reset:suspended"
    )
    end.assert_called_once_with(token, succeeded=True)


def test_gateway_terminal_fence_spans_exact_release_through_route_publication(
    tmp_path,
):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="terminal-fence-user",
        chat_id="terminal-fence-chat",
        chat_type="dm",
    )
    entry = runner.session_store.get_or_create_session(source)
    backend = MagicMock()

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        return_value=backend,
    ):
        computer_use._get_backend(entry.session_id)
        original_apply = runner.session_store._apply_reset_session_impl

        def apply_while_fenced(*args, **kwargs):
            with pytest.raises(RuntimeError, match="terminal transition"):
                computer_use._get_backend(entry.session_id)
            return original_apply(*args, **kwargs)

        with patch.object(
            runner.session_store,
            "_apply_reset_session_impl",
            side_effect=apply_while_fenced,
        ):
            replacement = runner.session_store.reset_session(
                entry.session_key,
                reset_reason="session_reset",
            )

        assert replacement.session_id != entry.session_id
        assert backend.stop.call_count == 1
        assert computer_use.get_computer_use_session_generation(entry.session_id) is None

        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(entry.session_id)


def test_gateway_terminal_completion_evicts_route_state_before_admission_reopens(
    tmp_path,
):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    order = []
    old_agent = object()
    lease = (object(), object(), "auto_reset:session_expired")

    runner._evict_cached_agent = MagicMock(
        side_effect=lambda key, soft_release=True: order.append(
            ("evict", key, soft_release)
        )
        or old_agent
    )
    runner._clear_conversation_scope = MagicMock(
        side_effect=lambda key, reason: order.append(("clear", key, reason))
    )
    runner._cleanup_agent_resources = MagicMock(
        side_effect=lambda agent, strict=False: order.append(
            ("hard_cleanup", agent, strict)
        )
    )
    runner._end_terminal_computer_use_sync = MagicMock(
        side_effect=lambda value, succeeded: order.append(
            ("end", value, succeeded)
        )
    )

    runner.session_store._before_terminal_completion_fn(
        "old-expired-session",
        lease,
        "telegram:dm:expiry-key",
    )
    runner.session_store._after_auto_reset_fn(
        "old-expired-session",
        lease,
        True,
    )

    assert order == [
        ("evict", "telegram:dm:expiry-key", False),
        (
            "clear",
            "telegram:dm:expiry-key",
            "terminal_route_replaced",
        ),
        ("hard_cleanup", old_agent, True),
        ("end", lease, True),
    ]


def test_terminal_expiry_cleanup_failure_keeps_admission_closed():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._evict_cached_agent = MagicMock()
    old_agent = MagicMock()
    old_agent.close.side_effect = [RuntimeError("hard cleanup failed"), None]
    runner._evict_cached_agent.side_effect = [old_agent, None]
    runner._clear_conversation_scope = MagicMock()
    runner.session_store = MagicMock()
    runner.session_store.peek_session_id.return_value = "replacement-session"
    runner._terminal_cleanup_agents = {}
    runner._terminal_cleanup_agents_lock = __import__("threading").Lock()
    lease = (object(), object(), "auto_reset:session_expired")

    with pytest.raises(RuntimeError, match="hard cleanup failed"):
        runner._prepare_terminal_route_completion_sync(
            "old-session",
            lease,
            "telegram:dm:expiry-key",
        )

    runner._prepare_terminal_route_completion_sync(
        "old-session",
        lease,
        "telegram:dm:expiry-key",
    )
    assert old_agent.close.call_count == 2
    runner._evict_cached_agent.assert_called_once()
    assert not runner._terminal_cleanup_agents


def test_terminal_expiry_cleanup_retry_reuses_retained_evicted_agent(tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="expiry-cleanup-retry",
        user_id="expiry-cleanup-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    old_agent = MagicMock()
    old_agent.close.side_effect = [RuntimeError("hard cleanup failed"), None]
    runner._agent_cache[original.session_key] = old_agent

    with pytest.raises(RuntimeError, match="hard cleanup failed"):
        runner.session_store.reset_session(
            original.session_key,
            reset_reason="session_expired",
            expected_session_id=original.session_id,
        )

    retained = runner.session_store._entries[original.session_key]
    assert retained.session_id == original.session_id
    assert retained.metadata.get("terminal_transition")
    assert old_agent.close.call_count == 1
    assert runner._terminal_cleanup_agents

    replacement = runner.session_store.reset_session(
        original.session_key,
        reset_reason="session_expired",
        expected_session_id=original.session_id,
    )
    assert replacement is not None
    assert replacement.session_id != original.session_id
    assert old_agent.close.call_count == 2
    assert not runner._terminal_cleanup_agents


def test_switch_away_and_resume_historical_route_reactivates_cua_safely(tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="resume-historical-cua",
        user_id="resume-historical-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    first = MagicMock(permission_mode="standard")
    second = MagicMock(permission_mode="standard")

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=[first, second],
    ):
        assert computer_use._get_backend(original.session_id) is first
        switched = runner.session_store.switch_session(
            original.session_key,
            "intermediate-session",
        )
        assert switched.session_id == "intermediate-session"
        resumed = runner.session_store.switch_session(
            original.session_key,
            original.session_id,
        )
        assert resumed.session_id == original.session_id
        assert computer_use._get_backend(original.session_id) is second

    first.stop.assert_called_once_with()
    assert computer_use.get_computer_use_session_generation(original.session_id)


def test_terminal_persistence_failure_retains_fence_and_retry_recovers(tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="terminal-persist-failure",
        user_id="terminal-persist-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    backend = MagicMock()
    backend.permission_mode = "standard"
    backend.start.return_value = None
    backend.stop.return_value = None

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend
    ):
        computer_use._get_backend(original.session_id)
        original_persist = runner.session_store._persist_routing_data

        def fail_replacement(data, generation, **kwargs):
            routed = data[original.session_key]["session_id"]
            if routed != original.session_id:
                raise OSError("simulated terminal routing persistence failure")
            return original_persist(data, generation, **kwargs)

        with patch.object(
            runner.session_store,
            "_persist_routing_data",
            side_effect=fail_replacement,
        ):
            with pytest.raises(OSError, match="simulated terminal"):
                runner.session_store.reset_session(
                    original.session_key,
                    reset_reason="session_reset",
                )

        retained = runner.session_store._entries[original.session_key]
        assert retained.session_id == original.session_id
        assert retained.metadata["terminal_transition"]["session_id"] == original.session_id
        with pytest.raises(RuntimeError, match="terminal transition"):
            computer_use._get_backend(original.session_id)

        replacement = runner.session_store.reset_session(
            original.session_key,
            reset_reason="session_reset",
        )
        assert replacement.session_id != original.session_id
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(original.session_id)

    computer_use.reset_backend_for_tests()


def test_auto_reset_persistence_failure_retains_fence_and_retry_recovers(tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="auto-terminal-persist-failure",
        user_id="auto-terminal-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    original.suspended = True
    runner.session_store._save_entries()
    backend = MagicMock()
    backend.permission_mode = "standard"
    backend.start.return_value = None
    backend.stop.return_value = None

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend
    ):
        computer_use._get_backend(original.session_id)
        original_persist = runner.session_store._persist_routing_data

        def fail_replacement(data, generation, **kwargs):
            routed = data[original.session_key]["session_id"]
            if routed != original.session_id:
                raise OSError("simulated auto-reset persistence failure")
            return original_persist(data, generation, **kwargs)

        with patch.object(
            runner.session_store,
            "_persist_routing_data",
            side_effect=fail_replacement,
        ):
            with pytest.raises(OSError, match="simulated auto-reset"):
                runner.session_store.get_or_create_session(source)

        retained = runner.session_store._entries[original.session_key]
        assert retained.session_id == original.session_id
        assert retained.metadata.get("terminal_transition")
        with pytest.raises(RuntimeError, match="terminal transition"):
            computer_use._get_backend(original.session_id)

        replacement = runner.session_store.get_or_create_session(source)
        assert replacement.session_id != original.session_id
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(original.session_id)

    computer_use.reset_backend_for_tests()


@pytest.mark.parametrize("failure_layer", ["routing", "promote", "create"])
def test_auto_reset_sqlite_failure_retains_fence_and_retry_recovers(
    tmp_path, failure_layer
):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=f"auto-db-{failure_layer}",
        user_id="auto-db-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    original.suspended = True
    runner.session_store._save_entries()

    fake_db = MagicMock()
    fake_db.promote_to_session_reset.return_value = True
    fake_db.save_gateway_routing_entry.return_value = None
    fake_db.replace_gateway_routing_entries.return_value = None
    fake_db.record_gateway_session_peer.return_value = None
    fake_db.get_compression_tip.return_value = original.session_id
    fake_db.get_session.return_value = None
    if failure_layer == "routing":
        fake_db.replace_gateway_routing_entries.side_effect = [
            OSError("routing failed"),
            None,
        ]
    elif failure_layer == "promote":
        fake_db.promote_to_session_reset.side_effect = OSError("promote failed")
    else:
        fake_db.create_session.side_effect = OSError("create failed")
    runner.session_store._db = fake_db

    backend = MagicMock()
    backend.permission_mode = "standard"
    backend.start.return_value = None
    backend.stop.return_value = None
    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend
    ):
        computer_use._get_backend(original.session_id)
        with pytest.raises(RuntimeError, match="terminal"):
            runner.session_store.get_or_create_session(source)
        retained = runner.session_store._entries[original.session_key]
        assert retained.session_id == original.session_id
        assert retained.metadata.get("terminal_transition")
        with pytest.raises(RuntimeError, match="terminal transition"):
            computer_use._get_backend(original.session_id)

        fake_db.replace_gateway_routing_entries.side_effect = None
        fake_db.promote_to_session_reset.side_effect = None
        fake_db.create_session.side_effect = None
        replacement = runner.session_store.get_or_create_session(source)
        assert replacement.session_id != original.session_id
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(original.session_id)

    computer_use.reset_backend_for_tests()


@pytest.mark.parametrize("failure_layer", ["routing", "promote", "create"])
def test_terminal_sqlite_failure_retains_fence_and_retry_recovers(
    tmp_path, failure_layer
):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=f"terminal-db-{failure_layer}",
        user_id="terminal-db-user",
        chat_type="dm",
    )
    original = runner.session_store.get_or_create_session(source)
    fake_db = MagicMock()
    fake_db.promote_to_session_reset.return_value = True
    fake_db.save_gateway_routing_entry.return_value = None
    fake_db.replace_gateway_routing_entries.return_value = None
    fake_db.record_gateway_session_peer.return_value = None
    if failure_layer == "routing":
        fake_db.replace_gateway_routing_entries.side_effect = [
            OSError("routing failed"),
            None,
        ]
    elif failure_layer == "promote":
        fake_db.promote_to_session_reset.side_effect = OSError("promote failed")
    else:
        fake_db.create_session.side_effect = OSError("create failed")
    runner.session_store._db = fake_db

    backend = MagicMock()
    backend.permission_mode = "standard"
    backend.start.return_value = None
    backend.stop.return_value = None
    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend
    ):
        computer_use._get_backend(original.session_id)
        with pytest.raises(RuntimeError, match="terminal"):
            runner.session_store.reset_session(
                original.session_key,
                reset_reason="session_reset",
            )
        retained = runner.session_store._entries[original.session_key]
        assert retained.session_id == original.session_id
        assert retained.metadata.get("terminal_transition")
        with pytest.raises(RuntimeError, match="terminal transition"):
            computer_use._get_backend(original.session_id)

        fake_db.replace_gateway_routing_entries.side_effect = None
        fake_db.promote_to_session_reset.side_effect = None
        fake_db.create_session.side_effect = None
        replacement = runner.session_store.reset_session(
            original.session_key,
            reset_reason="session_reset",
        )
        assert replacement.session_id != original.session_id
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(original.session_id)

    computer_use.reset_backend_for_tests()


def test_terminal_boundary_release_submits_and_confirms_exact_generation(runner):
    from tools.computer_use.tool import ComputerUseReleaseResult

    result = ComputerUseReleaseResult(
        session_id="old-session",
        generation=11,
        status="released",
        reason="auto_reset",
    )
    future: Future = Future()
    future.set_result(result)
    transition = SimpleNamespace(generation=11)
    with patch(
        "tools.computer_use.begin_computer_use_terminal_transition",
        return_value=transition,
    ), patch(
        "tools.computer_use.end_computer_use_terminal_transition"
    ), patch(
        "tools.computer_use.get_computer_use_session_generation",
        return_value=None,
    ), patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=future,
    ) as submit:
        returned = asyncio.run(
            runner._release_terminal_computer_use(
                "old-session", reason="auto_reset"
            )
        )

    assert returned is result
    submit.assert_called_once_with(
        "old-session",
        expected_generation=11,
        timeout=5.0,
        reason="auto_reset",
        max_attempts=3,
        retry_delay=0.25,
    )


def test_terminal_boundary_release_detects_recreated_generation(runner):
    from tools.computer_use.tool import ComputerUseReleaseResult

    result = ComputerUseReleaseResult(
        session_id="old-session",
        generation=11,
        status="released",
        reason="session_reset",
    )
    future: Future = Future()
    future.set_result(result)
    transition = SimpleNamespace(generation=11)
    with patch(
        "tools.computer_use.begin_computer_use_terminal_transition",
        return_value=transition,
    ), patch(
        "tools.computer_use.end_computer_use_terminal_transition"
    ), patch(
        "tools.computer_use.get_computer_use_session_generation",
        return_value=12,
    ), patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=future,
    ):
        with pytest.raises(RuntimeError, match="remained active"):
            asyncio.run(
                runner._release_terminal_computer_use(
                    "old-session", reason="session_reset"
                )
            )


def test_terminal_boundary_release_fails_closed_when_exact_teardown_fails(runner):
    from tools.computer_use.tool import ComputerUseReleaseResult

    result = ComputerUseReleaseResult(
        session_id="old-session",
        generation=12,
        status="timed_out",
        reason="session_expired",
        error="in-flight action",
    )
    future: Future = Future()
    future.set_result(result)
    transition = SimpleNamespace(generation=12)
    with patch(
        "tools.computer_use.begin_computer_use_terminal_transition",
        return_value=transition,
    ), patch(
        "tools.computer_use.end_computer_use_terminal_transition"
    ), patch(
        "tools.computer_use.get_computer_use_session_generation",
        return_value=12,
    ), patch(
        "tools.computer_use.submit_computer_use_session_release",
        return_value=future,
    ):
        with pytest.raises(RuntimeError, match="timed_out"):
            asyncio.run(
                runner._release_terminal_computer_use(
                    "old-session", reason="session_expired"
                )
            )


def test_generic_soft_evict_never_releases_computer_use(runner):
    agent = _agent("live-session")
    with patch(
        "tools.computer_use.submit_computer_use_session_release"
    ) as release:
        returned = runner._release_evicted_agent_soft(agent)

    assert returned is None
    release.assert_not_called()
    agent.release_clients.assert_called_once_with()
