from cron.scheduler import _summarize_cron_failure_for_delivery


def _job() -> dict[str, str]:
    return {"id": "job-1", "name": "Weekly report"}


def test_inactivity_timeout_names_terminal_tool_instead_of_provider() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(),
        "TimeoutError: Cron job 'Weekly report' idle for 3618s (limit 600s) — "
        "last activity: executing tool: terminal",
    )

    assert message == (
        "⚠️ Cron 'Weekly report' failed: job inactivity timeout while executing terminal. "
        "Full details saved in cron output."
    )
    assert "provider" not in message
    assert "Fallback chain" not in message


def test_api_wait_inactivity_is_provider_not_fake_tool() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(),
        "TimeoutError: Cron job idle for 601s — last activity: "
        "waiting for non-streaming API response",
    )

    assert "provider inactivity timeout" in message
    assert "executing waiting" not in message


def test_concurrent_activity_inactivity_does_not_invent_tool_name() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(),
        "TimeoutError: Cron job idle for 601s — last activity: concurrent tools pending",
    )

    assert "job inactivity timeout" in message
    assert "executing concurrent" not in message


def test_pending_approval_timeout_names_unattended_approval_block() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(),
        "Tool terminal returned error: status=pending_approval approval_pending=true timeout",
    )

    assert message == (
        "⚠️ Cron 'Weekly report' failed: unattended tool approval timed out. "
        "Full details saved in cron output."
    )


def test_provider_timeout_only_claims_fallback_exhaustion_when_evidenced() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(),
        "TimeoutError: Codex stream produced no SSE events for 60s",
    )
    exhausted = _summarize_cron_failure_for_delivery(
        _job(),
        "Provider timeout; fallback chain exhausted",
    )

    assert message == (
        "⚠️ Cron 'Weekly report' failed: provider timeout. "
        "Full details saved in cron output."
    )
    assert exhausted == (
        "⚠️ Cron 'Weekly report' failed: provider timeout. "
        "Fallback chain was exhausted or unavailable. "
        "Full details saved in cron output."
    )


def test_generic_timeout_does_not_guess_provider() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(), "TimeoutError: operation timed out"
    )

    assert message == (
        "⚠️ Cron 'Weekly report' failed: operation timed out. "
        "Full details saved in cron output."
    )


def test_rate_limit_does_not_claim_unobserved_fallback_exhaustion() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(), "HTTP 429 rate limit exceeded"
    )

    assert "provider rate limit" in message
    assert "Fallback chain" not in message


def test_real_approval_timeout_wording_is_classified() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(), "BLOCKED: Action timed out without user response after 600 seconds"
    )

    assert "unattended tool approval timed out" in message
    assert "provider" not in message


def test_tool_timeout_names_tool() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(), "Error executing tool 'terminal': timed out after 300.0s"
    )

    assert "tool 'terminal' timed out" in message
    assert "provider" not in message


def test_api_timeout_exception_is_provider_timeout() -> None:
    message = _summarize_cron_failure_for_delivery(
        _job(), "APITimeoutError: Request timed out"
    )

    assert "provider timeout" in message
    assert "Fallback chain" not in message
