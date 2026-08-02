"""Gateway model-sovereignty and secret boundaries across chat surfaces."""

import pytest

from agent.conversation_compression import (
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE,
    ROUTINE_COMPRESSION_STATUS_SAMPLES,
)
from gateway.config import Platform
from gateway.run import (
    _prepare_gateway_status_message,
    _sanitize_gateway_final_response,
)

# Every human-facing chat surface that must receive authored text unchanged
# apart from the explicit secret-redaction safety boundary.
CHAT_PLATFORMS = [
    "telegram",
    "whatsapp",
    "discord",
    "slack",
    "signal",
    "matrix",
    "mattermost",
    "dingtalk",
    "feishu",
    "wecom",
    "weixin",
    "bluebubbles",
    "qqbot",
    "homeassistant",
    "sms",
]

STATUS_MESSAGES = [
    "🗜️ Preflight compression check before sending...",
    (
        "📦 Pre-API compression: ~123,456 tokens near the context/output limit. "
        "Compacting before the next model call."
    ),
    "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
    "💤 Resumed after 3600s idle — compacting ~120,000 tokens before continuing.",
    "⚠️  Session compressed 12 times — accuracy may degrade. Consider /new to start fresh.",
    "⚠ Compression summary failed: upstream error. Inserted a fallback context marker.",
    "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
    "⏳ Retrying in 4.2s (attempt 1/3)...",
    # Buffered overflow/attempt-cap retry chatter (replayed on retry exhaustion).
    "🗜️ Context too large (~250,000 tokens) — compressing (1/3)...",
    "🗜️ Compressed 30 → 12 messages, retrying...",
    "🗜️ Compressed ~250,000 → ~120,000 tokens, retrying...",
    "🗜️ Context reduced to 120,000 tokens (was 250,000), retrying...",
    # Post-#69332 auto-lower wording + aux-provider/lock chatter.
    (
        "⚠ Compression model small (openrouter) context is 32,000 tokens, but "
        "the main model big (anthropic)'s compression threshold was 100,000 "
        "tokens. Auto-lowered this session's threshold to 30,000 tokens so "
        "compression can run."
    ),
    (
        "⚠ Configured auxiliary compression provider 'openai' is unavailable — "
        "context compression will drop middle turns without a summary. Check "
        "auxiliary.compression in config.yaml and reauthenticate that provider."
    ),
    (
        "⚠ Skipping concurrent compression — another path is already "
        "compressing this session. Will retry after it finishes."
    ),
]

# Messages that must NEVER be swallowed by the compression-noise filter:
# deliberate carve-outs from routine-compression silence — manual /compress
# feedback (manual_compression_feedback.py headlines) and abort/failure
# notices that require user action.
VISIBLE_COMPRESSION_MESSAGES = [
    "Compressed: 30 → 12 messages",
    "Compression aborted: 30 messages preserved",
    "Compressed with fallback: 30 → 12 messages",
    "No changes from compression: 30 messages",
    (
        "⚠ Compression aborted: auth failure. No messages were dropped — "
        "conversation continues unchanged. Run /compress to retry, or /new "
        "to start a fresh session."
    ),
    (
        "⚠ Compression returned an empty transcript. No session split was "
        "performed; conversation continues unchanged."
    ),
    # Manual /compress lock-skip feedback (issue #57631): both the
    # confirmed-holder and unconfirmed-acquire wordings must reach the user.
    (
        "⏳ Compression already in progress for this session "
        "(holder: pid=12345:tid=7:agent=1:nonce=ab). Please wait for it to "
        "finish."
    ),
    (
        "⏳ Compression skipped: could not acquire this session's "
        "compression lock. Another compression may still be running, or "
        "the lock check failed — try again shortly."
    ),
    # Blocked-overflow warning (#62625/#62708): the context is over the
    # compression threshold but compression is blocked (summary-LLM cooldown
    # or the anti-thrash breaker). FAILURE-CLASS — must reach chat users so
    # they can /new or /compress before the session dies at the hard token
    # limit. Formatted from the SAME template the emit site uses, so a
    # rewording that drifts into the noise regex fails here.
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="cooldown:30"
    ),
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="ineffective"
    ),
]


def test_telegram_status_does_not_classify_auxiliary_or_retry_wording():
    """Status wording is authored data, not a keyword-routing authority."""
    messages = [
        "⚠ Auxiliary title generation failed: HTTP 400: Operation contains cybersecurity risk",
        "⚠ Compression summary failed: upstream error. Inserted a fallback context marker.",
        "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
        "ℹ Configured compression model 'small-model' failed (timeout). Recovered using main model — check auxiliary.compression.model in config.yaml.",
        "⏳ Retrying in 4.2s (attempt 1/3)...",
        "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
        "⚠️ Max retries (3) exhausted — trying fallback...",
    ]

    for message in messages:
        assert _prepare_gateway_status_message(Platform.TELEGRAM, "warn", message) == message


def test_programmatic_surfaces_keep_raw_status():
    """Programmatic surfaces (local/api/webhook) must keep raw diagnostics.

    Negative case for the invariant: the chat-noise filter must not touch
    CLI/TUI diagnostics, API JSON, or webhook payloads.
    """
    message = "⏳ Retrying in 4.2s (attempt 1/3)..."

    for platform in ("local", "api_server", "webhook", "msgraph_webhook"):
        assert (
            _prepare_gateway_status_message(platform, "lifecycle", message) == message
        )


@pytest.mark.parametrize("message", ["still on it", "⏳ Working — 3 min"])
def test_telegram_status_keeps_legitimate_heartbeat_messages(message):
    """The compression filter must not swallow user-facing work heartbeats."""
    assert _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", message) == message


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("message", STATUS_MESSAGES)
def test_all_chat_gateways_preserve_status_meaning(platform, message):
    assert _prepare_gateway_status_message(platform, "warn", message) == message


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize(
    "message", ROUTINE_COMPRESSION_STATUS_SAMPLES, ids=lambda m: m[:32]
)
def test_all_routine_compression_statuses_preserved_from_source_constants(
    platform, message
):
    """Every routine compression status keeps its authored meaning.

    Iterates the sample-formatted status strings built from the SAME
    constants the emission sites use (agent/conversation_compression.py's
    ROUTINE_COMPRESSION_STATUS_SAMPLES), so a reworded emit site that drifts
    remains covered without anyone remembering to re-copy the literal into
    this file. Delivery policy is structured; wording is never authority.
    """
    assert _prepare_gateway_status_message(platform, "lifecycle", message) == message


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("message", VISIBLE_COMPRESSION_MESSAGES, ids=lambda m: m[:32])
def test_manual_compress_feedback_and_failure_notices_stay_visible(platform, message):
    """Manual /compress feedback and abort notices must never be swallowed.

    These are the deliberate carve-outs from routine-compression silence
    (#16775 failures, manual_compression_feedback.py) — widening the noise
    regex must not start eating them.
    """
    assert _prepare_gateway_status_message(platform, "warn", message) == message


@pytest.mark.parametrize("platform", ["whatsapp", "slack", "signal", "matrix"])
def test_chat_gateways_preserve_model_authored_provider_error_bytes(platform):
    """Post-model regexes must not rewrite provider-authored response bytes."""
    raw = (
        "API call failed after 3 retries: HTTP 401 Unauthorized — "
        "Authorization: Bearer sk-ABCDEF0123456789abcdef0123"
    )

    assert _sanitize_gateway_final_response(platform, raw) == raw


@pytest.mark.parametrize("platform", ["whatsapp", "slack", "signal", "matrix"])
def test_chat_gateways_preserve_model_authored_non_error_bytes(platform):
    """The gateway is transport, not a semantic or content-rewrite authority."""
    raw = (
        "Sure — here is the example request you asked for: "
        "curl -H 'Authorization: Bearer sk-ABCDEF0123456789abcdef0123' "
        "https://api.example.com/v1/models"
    )

    assert _sanitize_gateway_final_response(platform, raw) == raw


def test_plugin_platform_string_preserves_status():
    message = "⏳ Retrying in 4.2s (attempt 1/3)..."

    assert _prepare_gateway_status_message("irc", "warn", message) == message


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_chat_gateways_keep_normal_answers(platform):
    """Normal assistant content must pass through unchanged on chat surfaces."""
    answer = "Here is the clean summary you asked for."

    assert _sanitize_gateway_final_response(platform, answer) == answer


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_chat_gateways_preserve_interrupt_shaped_model_bytes(platform):
    """Authored wording is opaque even when it resembles runtime metadata."""
    sentinel = "Operation interrupted: waiting for model response (1.7s elapsed)."

    assert _sanitize_gateway_final_response(platform, sentinel) == sentinel
    assert _sanitize_gateway_final_response("local", sentinel) == sentinel


def test_telegram_status_does_not_classify_provider_security_wording():
    raw = (
        "❌ API failed after 3 retries — HTTP 400: request blocked because "
        "Operation contains cybersecurity risk. request_id=req_123"
    )

    sanitized = _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", raw)

    assert sanitized == raw


def test_telegram_final_response_preserves_authored_provider_error_wording():
    raw = (
        "API call failed after 3 retries: HTTP 400: This request was blocked "
        "under the provider cybersecurity risk policy. request_id=req_abc"
    )

    sanitized = _sanitize_gateway_final_response(Platform.TELEGRAM, raw)

    assert sanitized == raw


def test_telegram_final_response_preserves_authored_auth_error_bytes():
    """The gateway must not regex-rewrite model-authored output."""
    raw = (
        "⚠️ Provider authentication failed: Incorrect API key provided: "
        "sk-live_abcdefghijklmnopqrstuvwxyz1234567890"
    )

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, raw) == raw


def test_telegram_final_response_keeps_normal_answers():
    """Normal assistant content should not be rewritten."""
    answer = "Here is the clean summary you asked for."

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, answer) == answer


# Synthetic strings that used to trigger post-model regex rewriting.
_ISSUE_23810_SECRET_SHAPES = {
    "openai_sk": "sk-" + "a1b2c3d4e5f6a7b8c9d0",
    "github_fine_grained_pat": "github_pat_" + "1A" * 41,
    "github_classic_pat": "ghp_" + "Ab3Cd4Ef5Gh6Ij7Kl8Mn9Op0Qr1St2Uv3Wx",
    "telegram_bot_token": "bot1234567890:" + "AAH" * 13 + "x",
    "openrouter_v1": "sk-or-v1-" + "Z9" * 36 + "q",
}


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("shape_name", sorted(_ISSUE_23810_SECRET_SHAPES))
def test_chat_gateways_preserve_all_model_authored_shapes(platform, shape_name):
    """No credential-shaped wording may become a semantic rewrite trigger."""
    secret = _ISSUE_23810_SECRET_SHAPES[shape_name]
    raw = f"Sure, here is the token you asked me to echo: {secret} — done."

    assert _sanitize_gateway_final_response(platform, raw) == raw
