import contextvars
from concurrent.futures import ThreadPoolExecutor

from agent import firecrawl_run_state as state


def test_context_is_absent_until_installed_and_reset_restores_absence():
    assert state.current_firecrawl_run() is None
    run, token = state.install_firecrawl_run()
    try:
        assert state.current_firecrawl_run() is run
        assert run.circuit_open is False
    finally:
        state.reset_firecrawl_run(token)
    assert state.current_firecrawl_run() is None


def test_first_credit_failure_opens_once_with_only_stable_evidence():
    run, token = state.install_firecrawl_run()
    try:
        assert state.record_firecrawl_credits_exhausted() is True
        assert state.record_firecrawl_credits_exhausted() is False
        assert run.first_failure == {
            "code": "provider_credits_exhausted",
            "provider": "firecrawl",
            "scope": "account",
            "retryable": False,
        }
        assert run.fallback_decision == "continue_without_firecrawl"
    finally:
        state.reset_firecrawl_run(token)


def test_open_gate_raises_sanitized_circuit_error_only_after_402():
    _, token = state.install_firecrawl_run()
    try:
        state.raise_if_firecrawl_circuit_open()
        state.record_firecrawl_credits_exhausted()
        try:
            state.raise_if_firecrawl_circuit_open()
        except state.FirecrawlCircuitOpenError as exc:
            assert str(exc) == "Firecrawl account credit circuit is open"
            assert exc.error_info == dict(state.CIRCUIT_OPEN_INFO)
        else:
            raise AssertionError("open circuit did not reject admission")
    finally:
        state.reset_firecrawl_run(token)


def test_concurrent_402_recording_has_exactly_one_winner():
    _, token = state.install_firecrawl_run()
    try:
        contexts = [contextvars.copy_context() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda ctx: ctx.run(state.record_firecrawl_credits_exhausted),
                    contexts,
                )
            )
        assert results.count(True) == 1
        assert results.count(False) == 7
    finally:
        state.reset_firecrawl_run(token)


def test_fallback_provider_and_none_are_memoized_per_capability():
    _, token = state.install_firecrawl_run()
    calls = []
    try:
        search = object()
        assert (
            state.get_or_select_fallback_provider(
                "search", lambda: calls.append("search") or search
            )
            is search
        )
        assert (
            state.get_or_select_fallback_provider(
                "search", lambda: calls.append("unexpected")
            )
            is search
        )
        assert (
            state.get_or_select_fallback_provider(
                "extract", lambda: calls.append("extract") or None
            )
            is None
        )
        assert (
            state.get_or_select_fallback_provider(
                "extract", lambda: calls.append("unexpected-none") or object()
            )
            is None
        )
        assert calls == ["search", "extract"]
    finally:
        state.reset_firecrawl_run(token)


def test_credits_action_can_be_claimed_once_only_after_real_402():
    _, token = state.install_firecrawl_run()
    try:
        assert state.claim_credits_action() is False
        state.record_firecrawl_credits_exhausted()
        assert state.claim_credits_action() is True
        assert state.claim_credits_action() is False
    finally:
        state.reset_firecrawl_run(token)


def test_no_context_wrappers_preserve_non_run_behavior():
    selected = object()
    calls = []

    assert state.record_firecrawl_credits_exhausted() is False
    assert state.claim_credits_action() is False
    state.raise_if_firecrawl_circuit_open()
    assert (
        state.get_or_select_fallback_provider(
            "search", lambda: calls.append("search") or selected
        )
        is selected
    )
    assert calls == ["search"]


def test_next_installed_activation_starts_closed():
    first, first_token = state.install_firecrawl_run()
    try:
        state.record_firecrawl_credits_exhausted()
        assert first.circuit_open is True
    finally:
        state.reset_firecrawl_run(first_token)

    second, second_token = state.install_firecrawl_run()
    try:
        assert second is not first
        assert second.circuit_open is False
        assert second.first_failure is None
        assert second.fallback_decision is None
    finally:
        state.reset_firecrawl_run(second_token)
