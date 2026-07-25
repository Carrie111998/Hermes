"""Opt-in compression progress notices on chat gateways (#52995).

Routine automatic compression is silent-by-design on human-facing chat
platforms. Emit sites tag those notices with the structured
``compression_progress`` event type.
`compression.progress_notices: true` (default: false) opens an opt-in gate
that lets those tagged notices through. Authored message wording is never a
routing authority: unrelated lifecycle/warning text remains visible and is
subject only to the secret-redaction boundary.
"""

import pytest

import gateway.run as gateway_run
from agent.conversation_compression import (
    COMPACTION_DONE_STATUS,
    ROUTINE_COMPRESSION_STATUS_SAMPLES,
)
from gateway.run import _prepare_gateway_status_message

# Chat surfaces the opt-in must deliver to (subset of the noise-filter
# suite's CHAT_PLATFORMS; telegram + discord are the required anchors).
CHAT_PLATFORMS = ["telegram", "discord", "slack", "whatsapp"]

# Statuses that are NOT routine compression progress. Their wording must not
# be used to suppress or reroute them.
NON_COMPRESSION_NOISE = [
    "⚠ Auxiliary title generation failed: HTTP 400: Operation contains cybersecurity risk",
    "⏳ Retrying in 4.2s (attempt 1/3)...",
    "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
    "⚠️ Max retries (3) exhausted — trying fallback...",
    "⚠ Compression summary failed: upstream error. Inserted a fallback context marker.",
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


@pytest.fixture
def progress_notices_enabled(monkeypatch):
    """Gateway config with compression.progress_notices: true."""
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"compression": {"progress_notices": True}},
    )


@pytest.fixture
def progress_notices_default(monkeypatch):
    """Gateway config without the key — the silent-by-design default."""
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize(
    "message", ROUTINE_COMPRESSION_STATUS_SAMPLES, ids=lambda m: m[:32]
)
def test_enabled_delivers_routine_compression_statuses(
    progress_notices_enabled, platform, message
):
    """Opt-in ON: every ROUTINE compression status reaches chat platforms.

    Iterates the sample strings formatted from the same template constants
    the emit sites use while keeping delivery bound to the structured type.
    """
    assert (
        _prepare_gateway_status_message(platform, "compression_progress", message)
        == message
    )


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize(
    "message", ROUTINE_COMPRESSION_STATUS_SAMPLES, ids=lambda m: m[:32]
)
def test_default_stays_silent(progress_notices_default, platform, message):
    """Default (key absent): routine compression statuses stay suppressed."""
    assert (
        _prepare_gateway_status_message(platform, "compression_progress", message)
        is None
    )


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_explicit_false_stays_silent(monkeypatch, platform):
    """compression.progress_notices: false behaves exactly like the default."""
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"compression": {"progress_notices": False}},
    )
    for message in ROUTINE_COMPRESSION_STATUS_SAMPLES:
        assert (
            _prepare_gateway_status_message(
                platform, "compression_progress", message
            )
            is None
        )


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("message", NON_COMPRESSION_NOISE, ids=lambda m: m[:32])
def test_enabled_preserves_non_compression_status_meaning(
    progress_notices_enabled, platform, message
):
    """The gate is scoped to the structured compression event type only."""
    assert _prepare_gateway_status_message(platform, "warn", message) == message


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "default"])
@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_compaction_completion_notice_reaches_chat(monkeypatch, platform, enabled):
    """The #69546 'compacted' lifecycle edge is deliverable on chat surfaces.

    COMPACTION_DONE_STATUS already flows through the status callback on
    compaction completion and is not matched by the noise regex — the opt-in
    gate must not change that in either mode, so users who enable
    progress_notices see the completion stat notice paired with the start.
    """
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"compression": {"progress_notices": enabled}},
    )
    assert (
        _prepare_gateway_status_message(platform, "compacted", COMPACTION_DONE_STATUS)
        == COMPACTION_DONE_STATUS
    )


def test_config_read_errors_fail_closed(monkeypatch):
    """A broken config read keeps the silent-by-design default."""
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(gateway_run, "_load_gateway_config", _boom)
    message = ROUTINE_COMPRESSION_STATUS_SAMPLES[0]
    assert (
        _prepare_gateway_status_message(
            "telegram", "compression_progress", message
        )
        is None
    )


def test_enabled_gate_does_not_leak_to_raw_platforms(progress_notices_enabled):
    """Programmatic surfaces keep raw text regardless of the gate."""
    message = ROUTINE_COMPRESSION_STATUS_SAMPLES[0]
    for platform in ("local", "api_server", "webhook", "msgraph_webhook"):
        assert (
            _prepare_gateway_status_message(
                platform, "compression_progress", message
            )
            == message
        )


def test_progress_notices_is_a_hot_reload_cache_busting_key():
    """Editing compression.progress_notices on a running gateway must take
    effect like every other compression.* key (hot-reload key list)."""
    assert ("compression", "progress_notices") in gateway_run.GatewayRunner._CACHE_BUSTING_CONFIG_KEYS


def test_message_wording_is_not_delivery_authority(progress_notices_default):
    """Routine-looking text under another event type remains authored data."""
    for message in ROUTINE_COMPRESSION_STATUS_SAMPLES:
        assert (
            _prepare_gateway_status_message("discord", "lifecycle", message)
            == message
        )
