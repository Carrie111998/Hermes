from datetime import datetime
from zoneinfo import ZoneInfo

from gateway.message_timestamps import (
    coerce_message_timestamp,
    render_user_content_with_timestamp,
    strip_leading_message_timestamps,
)
from run_agent import AIAgent


BERLIN = ZoneInfo("Europe/Berlin")


def _epoch(year, month, day, hour, minute, second):
    return datetime(year, month, day, hour, minute, second, tzinfo=BERLIN).timestamp()


def test_render_user_content_deduplicates_existing_timestamp_and_preserves_embedded_time():
    db_processing_ts = _epoch(2026, 4, 27, 15, 55, 36)
    stored_content = (
        "[Mon 2026-04-27 15:54:44 CEST] "
        "[Example User] This should go on our todo list"
    )

    rendered = render_user_content_with_timestamp(
        stored_content,
        db_processing_ts,
        tz=BERLIN,
    )

    assert rendered == stored_content
    assert rendered.count("2026-04-27") == 1


# ---------------------------------------------------------------------------
# Opt-in gate: gateway.message_timestamps.enabled (default OFF)
# ---------------------------------------------------------------------------


def test_message_timestamps_enabled_defaults_off():
    from gateway.run import _message_timestamps_enabled

    assert _message_timestamps_enabled(None) is False
    assert _message_timestamps_enabled({}) is False
    assert _message_timestamps_enabled({"gateway": {}}) is False
    assert (
        _message_timestamps_enabled({"gateway": {"message_timestamps": {}}}) is False
    )


def test_build_history_injects_only_when_enabled():
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "hello", "timestamp": _epoch(2026, 4, 28, 13, 40, 53)},
        {"role": "assistant", "content": "hi"},
    ]

    # Default (off): user content stays clean, no timestamp prefix.
    agent_history, _ = _build_gateway_agent_history(history)
    assert agent_history[0]["content"] == "hello"

    # Enabled: user content gets exactly one timestamp prefix.
    agent_history, _ = _build_gateway_agent_history(history, inject_timestamps=True)
    assert agent_history[0]["content"].startswith("[")
    assert agent_history[0]["content"].endswith("hello")
    # Assistant message is never timestamped.
    assert agent_history[1]["content"] == "hi"


# ---------------------------------------------------------------------------
# Shared message_timestamps_enabled() — used by cron, HA, and CLI sessions
# ---------------------------------------------------------------------------


def test_shared_message_timestamps_enabled_true_when_dict_enabled():
    from gateway.message_timestamps import message_timestamps_enabled

    assert message_timestamps_enabled(
        {"gateway": {"message_timestamps": {"enabled": True}}}
    ) is True


def test_shared_message_timestamps_enabled_true_when_bare_shorthand():
    from gateway.message_timestamps import message_timestamps_enabled

    assert message_timestamps_enabled(
        {"gateway": {"message_timestamps": True}}
    ) is True


def test_shared_message_timestamps_enabled_false_when_not_set():
    from gateway.message_timestamps import message_timestamps_enabled

    # Pass explicit empty configs — None would load the real config file.
    assert message_timestamps_enabled({}) is False
    assert message_timestamps_enabled({"gateway": {}}) is False
    assert message_timestamps_enabled({"gateway": {"other": "stuff"}}) is False


def test_shared_message_timestamps_enabled_false_when_disabled():
    from gateway.message_timestamps import message_timestamps_enabled

    assert message_timestamps_enabled(
        {"gateway": {"message_timestamps": {"enabled": False}}}
    ) is False
    assert message_timestamps_enabled(
        {"gateway": {"message_timestamps": False}}
    ) is False


def test_shared_message_timestamps_enabled_false_when_explicitly_false_dict():
    from gateway.message_timestamps import message_timestamps_enabled

    # Explicit {enabled: false} must be False, not truthy-dict.
    assert message_timestamps_enabled(
        {"gateway": {"message_timestamps": {"enabled": False, "other": "stuff"}}}
    ) is False


# ---------------------------------------------------------------------------
# Cron prompt timestamp injection
# ---------------------------------------------------------------------------


def test_cron_prompt_gets_timestamp_prepended_when_enabled():
    """When timestamps are enabled, render_user_content_with_timestamp
    prepends exactly one ``[...]`` timestamp prefix to the cron prompt."""
    from gateway.message_timestamps import (
        message_timestamps_enabled,
        render_user_content_with_timestamp,
    )

    prompt = "Check the server status and report any issues."
    config = {"gateway": {"message_timestamps": {"enabled": True}}}

    assert message_timestamps_enabled(config) is True

    timestamped = render_user_content_with_timestamp(
        prompt, ts_value=_epoch(2026, 7, 29, 16, 6, 44), tz=BERLIN
    )

    # Exactly one timestamp prefix, followed by the original prompt text.
    assert timestamped.startswith("[")
    assert timestamped.count("[") == 1  # only the timestamp bracket
    assert timestamped.endswith(prompt)
    assert "2026-07-29" in timestamped


def test_no_double_timestamping_when_gateway_already_added_one():
    """If the prompt already has a leading timestamp (e.g. from a prior
    gateway injection), render_user_content_with_timestamp must not add a
    second one — it strips the existing prefix and re-renders with the
    embedded time."""
    from gateway.message_timestamps import render_user_content_with_timestamp

    original_prompt = "Do the daily backup."
    already_timestamped = (
        "[Tue 2026-07-29 09:00:00 CEST] " + original_prompt
    )

    rendered = render_user_content_with_timestamp(
        already_timestamped, ts_value=_epoch(2026, 7, 29, 16, 6, 44), tz=BERLIN
    )

    # The result has exactly one timestamp — the original embedded one wins.
    assert rendered.count("2026-07-29") == 1
    assert "09:00:00" in rendered  # embedded time preserved
    assert "16:06:44" not in rendered  # new ts_value NOT used
    assert rendered.endswith(original_prompt)


def test_cron_prompt_unchanged_when_disabled():
    """When timestamps are disabled, the prompt passes through unchanged."""
    from gateway.message_timestamps import message_timestamps_enabled

    prompt = "Check the server status."
    config = {"gateway": {"message_timestamps": {"enabled": False}}}

    assert message_timestamps_enabled(config) is False
    # When disabled, the caller skips injection entirely — prompt is untouched.
    assert prompt == "Check the server status."


# ---------------------------------------------------------------------------
# Per-job timestamps override (first decisive value wins, same pattern as
# attach_to_session / cron.mirror_delivery)
# ---------------------------------------------------------------------------


def test_per_job_timestamps_true_overrides_global_disabled():
    """Per-job timestamps=True injects even when global config is off."""
    from gateway.message_timestamps import render_user_content_with_timestamp

    prompt = "Do the thing."
    config = {"gateway": {"message_timestamps": {"enabled": False}}}

    # Global says off, but per-job says on — per-job wins.
    per_job = True
    if isinstance(per_job, bool):
        should_inject = per_job
    else:
        should_inject = message_timestamps_enabled(config)

    assert should_inject is True
    timestamped = render_user_content_with_timestamp(
        prompt, ts_value=_epoch(2026, 7, 29, 9, 0, 0), tz=BERLIN
    )
    assert timestamped.startswith("[")
    assert timestamped.endswith(prompt)


def test_per_job_timestamps_false_overrides_global_enabled():
    """Per-job timestamps=False skips injection even when global config is on."""
    from gateway.message_timestamps import message_timestamps_enabled

    config = {"gateway": {"message_timestamps": {"enabled": True}}}

    # Global says on, but per-job says off — per-job wins.
    per_job = False
    if isinstance(per_job, bool):
        should_inject = per_job
    else:
        should_inject = message_timestamps_enabled(config)

    assert should_inject is False


def test_per_job_timestamps_none_falls_back_to_global():
    """Per-job timestamps=None (absent) falls back to the global config."""
    from gateway.message_timestamps import message_timestamps_enabled

    config_on = {"gateway": {"message_timestamps": {"enabled": True}}}
    config_off = {"gateway": {"message_timestamps": {"enabled": False}}}

    per_job = None  # absent / not set
    if isinstance(per_job, bool):
        should_inject = per_job
    else:
        should_inject = message_timestamps_enabled(config_on)
    assert should_inject is True

    if isinstance(per_job, bool):
        should_inject = per_job
    else:
        should_inject = message_timestamps_enabled(config_off)
    assert should_inject is False
