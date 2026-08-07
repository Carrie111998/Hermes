from __future__ import annotations

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


def test_generic_soft_evict_never_releases_computer_use(runner):
    agent = _agent("live-session")
    with patch(
        "tools.computer_use.submit_computer_use_session_release"
    ) as release:
        returned = runner._release_evicted_agent_soft(agent)

    assert returned is None
    release.assert_not_called()
    agent.release_clients.assert_called_once_with()
