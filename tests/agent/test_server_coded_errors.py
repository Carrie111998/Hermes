"""Server-classified coded errors from the Nous Inference API.

The gateway attaches a machine-readable ``code`` to some error bodies
alongside the structured extras (``reason``, ``retry_after``, ...). For
those errors the server's ``message`` is authoritative and user-actionable,
and the HTTP status carries the retry semantics:

* a 403 with a code is terminal for that request — retrying, refreshing,
  or rotating credentials cannot change the server's decision;
* a 429 with a code is a per-user limit that resets on its own schedule
  (Retry-After is set) — credential rotation and provider fallback both
  follow the user's account, so neither helps.

The classifier expresses these as ``entitlement_blocked`` (terminal,
non-rotating, non-falling-back) and ``user_rate_limit`` (retryable with
Retry-After, non-rotating, non-falling-back) — but ONLY when the code is
not one the classifier already recognizes (billing codes,
``resource_exhausted``, ``model_not_found``, ...). Errors with no ``code``
field are untouched.

All error codes in fixtures are synthetic — they stand in for whatever
codes the server ships, deliberately resembling none of them.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.error_classifier import (
    FailoverReason,
    _known_structured_error_codes,
    classify_api_error,
)


class MockAPIError(Exception):
    """Simulates an OpenAI SDK APIStatusError with a structured body."""

    def __init__(self, message, status_code=None, body=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.response = SimpleNamespace(headers=headers or {})


def _coded_error(status, code, message):
    """A Nous-shaped coded error body: {status, message, code?}."""
    return MockAPIError(
        message,
        status_code=status,
        body={"status": status, "message": message, "code": code},
    )


# ── Classifier: unrecognized-code 403 ─────────────────────────────────

class TestCoded403EntitlementBlocked:
    def test_unknown_code_403_is_entitlement_blocked(self):
        e = _coded_error(
            403,
            "example_terminal_entitlement",
            "This account may not use that capability. Manage it in the portal.",
        )
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.entitlement_blocked
        assert result.retryable is False
        assert result.should_rotate_credential is False
        assert result.should_fallback is False

    def test_server_message_preserved_verbatim(self):
        message = "This account may not use that capability. Manage it in the portal."
        e = _coded_error(403, "example_terminal_entitlement", message)
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.message == message

    def test_not_auth_reason(self):
        """is_auth drives the credential-refresh machinery — must stay off."""
        e = _coded_error(403, "example_terminal_entitlement", "denied")
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.is_auth is False

    def test_provider_aliases_accepted(self):
        for provider in ("nous", "Nous", "nous-portal", "nousresearch"):
            e = _coded_error(403, "example_terminal_entitlement", "denied")
            result = classify_api_error(e, provider=provider, model="m")
            assert result.reason == FailoverReason.entitlement_blocked, provider

    def test_other_providers_untouched(self):
        """The rule is Nous-scoped: a coded 403 elsewhere stays generic auth."""
        e = _coded_error(403, "example_terminal_entitlement", "denied")
        result = classify_api_error(e, provider="openrouter", model="x")
        assert result.reason == FailoverReason.auth
        assert result.should_fallback is True


# ── Classifier: unrecognized-code 429 ─────────────────────────────────

class TestCoded429UserRateLimit:
    def test_unknown_code_429_is_user_rate_limit(self):
        e = _coded_error(
            429,
            "example_user_limit",
            "You have used up your request budget. It resets automatically.",
        )
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.user_rate_limit
        assert result.retryable is True
        assert result.should_rotate_credential is False
        assert result.should_fallback is False

    def test_server_message_preserved(self):
        message = "You have used up your request budget. It resets automatically."
        e = _coded_error(429, "example_user_limit", message)
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.message == message

    def test_retry_after_body_extra_accepted(self):
        """The structured retry_after extra is valid on a coded body."""
        e = MockAPIError(
            "limited",
            status_code=429,
            body={
                "status": 429,
                "message": "You have used up your request budget.",
                "code": "example_user_limit",
                "retry_after": 120,
            },
        )
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.user_rate_limit

    def test_other_providers_untouched(self):
        e = _coded_error(429, "example_user_limit", "too many requests")
        result = classify_api_error(e, provider="openrouter", model="x")
        assert result.reason == FailoverReason.rate_limit
        assert result.should_rotate_credential is True
        assert result.should_fallback is True


# ── Classifier: recognized codes and bare errors keep behavior ────────

class TestKnownCodesAndBareErrorsUntouched:
    def test_known_billing_code_403_stays_billing(self):
        e = _coded_error(403, "insufficient_credits", "insufficient credits")
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.billing
        assert result.should_rotate_credential is True
        assert result.should_fallback is True

    def test_known_resource_exhausted_429_stays_rate_limit(self):
        e = _coded_error(429, "resource_exhausted", "resource exhausted")
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.rate_limit

    def test_known_model_not_found_404_untouched(self):
        e = _coded_error(404, "model_not_found", "model not found")
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.model_not_found

    def test_bare_403_without_code_stays_auth(self):
        e = MockAPIError("Forbidden", status_code=403)
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.auth
        assert result.should_fallback is True

    def test_bare_429_without_code_stays_rate_limit(self):
        e = MockAPIError("Too Many Requests", status_code=429)
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.rate_limit
        assert result.should_rotate_credential is True
        assert result.should_fallback is True

    def test_billing_message_shape_wins_over_unknown_code(self):
        """A coded 403 whose message matches billing patterns keeps billing."""
        e = _coded_error(
            403,
            "example_terminal_entitlement",
            "This request requires available credits. Your account balance is too low.",
        )
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.billing

    def test_usage_limit_reached_429_keeps_existing_disambiguation(self):
        """``usage_limit_reached`` has its own status-path behavior."""
        e = _coded_error(
            429, "usage_limit_reached", "Your account has reached its usage limit."
        )
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.billing
        assert result.retryable is False

    def test_overload_message_429_stays_overloaded(self):
        e = _coded_error(429, "example_user_limit", "The service is overloaded.")
        result = classify_api_error(e, provider="nous", model="hermes-4")
        assert result.reason == FailoverReason.overloaded

    def test_known_code_registry_covers_classifier_claims(self):
        """The known-code registry is a contract with ``_classify_by_error_code``:
        every code that function claims must be in the registry, so the
        unrecognized-code rule can never steal an established classification."""
        from agent.error_classifier import _classify_by_error_code

        def _claim_probe(code):
            return _classify_by_error_code(
                code, "", lambda reason, **kw: SimpleNamespace(reason=reason)
            )

        claimed = {
            "resource_exhausted", "throttled", "rate_limit_exceeded",
            "model_not_found", "model_not_available", "invalid_model",
            "context_length_exceeded", "max_tokens_exceeded",
            "invalid_encrypted_content",
        }
        for code in claimed:
            assert _claim_probe(code) is not None, code
            assert code in _known_structured_error_codes(), code


# ── Agent loop: coded 403 aborts with the server message, no recovery ──

def _make_nous_agent():
    """A minimal AIAgent on the Nous provider that never touches the network."""
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://inference-api.nousresearch.com/v1",
            provider="nous",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = set()
    agent.client = MagicMock()
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None
    return agent


_SERVER_403_MESSAGE = (
    "This account may not use that capability. Manage it in the portal."
)


class _Coded403(Exception):
    status_code = 403
    body = {
        "status": 403,
        "message": _SERVER_403_MESSAGE,
        "code": "example_terminal_entitlement",
    }

    def __str__(self):
        return "Error code: 403 — request rejected"


class TestCoded403AbortPath:
    def test_aborts_with_server_message_no_refresh_no_fallback(self):
        agent = _make_nous_agent()

        def _fail(api_kwargs):
            raise _Coded403()

        agent._interruptible_api_call = _fail

        fallback = MagicMock(return_value=True)
        with (
            patch.object(agent, "_try_activate_fallback", fallback),
            patch.object(
                agent,
                "_try_refresh_nous_client_credentials",
                MagicMock(
                    side_effect=AssertionError(
                        "Nous credential refresh must not run for a coded 403"
                    )
                ),
            ),
        ):
            result = agent.run_conversation("hello")

        assert result["failed"] is True
        assert result["failure_reason"] == "entitlement_blocked"
        assert result["failure_retryable"] is False
        assert _SERVER_403_MESSAGE in result["error"], (
            "the server's own message must surface verbatim in the abort notice"
        )
        assert "Non-retryable error (HTTP 403)" not in result["error"]
        fallback.assert_not_called()

    def test_pool_recovery_never_rotates_or_refreshes(self):
        """The real pool-recovery helper must not touch the pool for this
        reason — no rotation, no refresh, no exhaustion marking."""
        from agent.agent_runtime_helpers import recover_with_credential_pool

        agent = _make_nous_agent()
        pool = MagicMock()
        pool.provider = ""  # unscoped pool: the provider guard is skipped
        agent._credential_pool = pool

        recovered, has_retried = recover_with_credential_pool(
            agent,
            status_code=403,
            has_retried_429=False,
            classified_reason=FailoverReason.entitlement_blocked,
        )
        assert recovered is False
        pool.mark_exhausted_and_rotate.assert_not_called()
        pool.try_refresh_matching.assert_not_called()


# ── Agent loop: coded 429 waits per Retry-After, rotates/falls back nothing ──

class _Coded429(Exception):
    status_code = 429
    body = {
        "status": 429,
        "message": "You have used up your request budget. It resets automatically.",
        "code": "example_user_limit",
    }

    def __init__(self, retry_after):
        self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})

    def __str__(self):
        return "Error code: 429 — limited"


class TestCoded429RetryPath:
    def _run_against_persistent_429(self, agent, retry_after, captured=None):
        err = _Coded429(retry_after)

        def _fail(api_kwargs):
            raise err

        agent._interruptible_api_call = _fail
        if captured is not None:
            original_buffer = agent._buffer_status

            def _capture(msg, *args, **kwargs):
                captured.append(msg)
                return original_buffer(msg, *args, **kwargs)

            agent._buffer_status = _capture
        return agent.run_conversation("hello")

    def test_retry_after_honored_verbatim(self):
        """The wait status echoes the server's Retry-After value, not backoff."""
        agent = _make_nous_agent()
        agent._credential_pool = None
        captured = []
        original_buffer = agent._buffer_status

        def _capture(msg, *args, **kwargs):
            captured.append(msg)
            # Interrupt during the wait so the test doesn't sleep 37 real
            # seconds; the status line was already emitted before the sleep.
            if "Waiting" in msg:
                agent._interrupt_requested = True
            return original_buffer(msg, *args, **kwargs)

        agent._buffer_status = _capture
        fallback = MagicMock(return_value=True)
        with patch.object(agent, "_try_activate_fallback", fallback):
            self._run_against_persistent_429(agent, 37, captured)

        waits = [m for m in captured if "Waiting" in m]
        assert waits, "expected a rate-limit wait status"
        assert "Waiting 37.0s" in waits[0], waits[0]
        fallback.assert_not_called()

    def test_terminal_after_retries_without_fallback_or_rotation(self):
        """Retries exhaust (tiny Retry-After keeps it fast), then the terminal
        result names the reason — and no fallback/pool rotation ever ran."""
        agent = _make_nous_agent()
        agent._credential_pool = None
        captured = []
        fallback = MagicMock(return_value=True)
        rotate = MagicMock(return_value=None)
        with (
            patch.object(agent, "_try_activate_fallback", fallback),
            patch.object(agent, "_swap_credential", rotate),
        ):
            result = self._run_against_persistent_429(agent, 0.1, captured)

        assert result["failed"] is True
        assert result["failure_reason"] == "user_rate_limit"
        assert result["failure_retryable"] is True
        server_message = _Coded429.body["message"]
        assert server_message in result["error"], (
            "the server's own message must surface in the terminal notice"
        )
        fallback.assert_not_called()
        rotate.assert_not_called()

    def test_never_writes_shared_rate_limit_state(self):
        """A coded per-user 429 must not feed the cross-session Nous breaker
        (agent/nous_rate_guard.py) — tripwire for anyone widening that gate
        to include the new reason."""
        import agent.nous_rate_guard as guard

        agent = _make_nous_agent()
        agent._credential_pool = None
        with (
            patch.object(guard, "record_nous_rate_limit") as record,
            patch.object(guard, "is_genuine_nous_rate_limit", return_value=True),
        ):
            self._run_against_persistent_429(agent, 0.1)

        record.assert_not_called()

    def test_pool_recovery_never_rotates_or_refreshes(self):
        from agent.agent_runtime_helpers import recover_with_credential_pool

        agent = _make_nous_agent()
        pool = MagicMock()
        pool.provider = ""  # unscoped pool: the provider guard is skipped
        agent._credential_pool = pool

        recovered, has_retried = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=True,
            classified_reason=FailoverReason.user_rate_limit,
        )
        assert recovered is False
        pool.mark_exhausted_and_rotate.assert_not_called()
        pool.try_refresh_matching.assert_not_called()
