from __future__ import annotations

import logging

from agent.speculative_compression import (
    DEFAULT_SPECULATIVE_COMPRESSION_SETTINGS,
    is_builtin_compression_eligible,
    normalize_speculative_compression_settings,
    speculative_thresholds,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_runtime_defaults_are_disabled_and_match_checked_in_reference():
    settings = normalize_speculative_compression_settings({})
    assert settings == DEFAULT_SPECULATIVE_COMPRESSION_SETTINGS
    assert DEFAULT_CONFIG["compression"]["speculative"]["enabled"] is False



def test_invalid_ratios_warn_and_fall_back_to_safe_defaults(caplog):
    with caplog.at_level(logging.WARNING):
        settings = normalize_speculative_compression_settings({
            "enabled": True,
            "start_ratio": 0.9,
            "hard_ratio": 0.9,
            "hard_wait_seconds": -10,
        })

    assert settings.enabled is True
    assert settings.start_ratio == 0.70
    assert settings.hard_ratio == 0.85
    assert settings.hard_wait_seconds == 2.0
    assert "Invalid compression.speculative ratios" in caplog.text


def test_zero_hard_wait_is_a_valid_nonblocking_setting():
    settings = normalize_speculative_compression_settings({
        "hard_wait_seconds": 0,
        "max_age_seconds": 0,
    })
    assert settings.hard_wait_seconds == 0
    assert settings.max_age_seconds == 0


def test_thresholds_use_effective_input_window_after_output_reservation():
    class Compressor:
        context_length = 260_000
        max_tokens = 64_000

        @staticmethod
        def _compute_threshold_tokens(context_length, ratio, max_tokens):
            return int((context_length - max_tokens) * ratio)

    settings = normalize_speculative_compression_settings({
        "start_ratio": 0.70,
        "hard_ratio": 0.85,
    })
    assert speculative_thresholds(Compressor(), settings) == (137_200, 166_600)


def test_thresholds_respect_absolute_cap_below_ratio_watermark():
    class Compressor:
        context_length = 260_000
        max_tokens = 64_000
        threshold_tokens_cap = 100_000

        @staticmethod
        def _compute_threshold_tokens(context_length, ratio, max_tokens):
            return int((context_length - max_tokens) * ratio)

    settings = normalize_speculative_compression_settings({
        "start_ratio": 0.70,
        "hard_ratio": 0.85,
    })
    # Both watermarks cap at the operator's absolute admission point, and
    # hard never falls below soft.
    soft, hard = speculative_thresholds(Compressor(), settings)
    assert soft == 100_000
    assert hard == 100_000
    assert hard >= soft


def test_thresholds_ignore_cap_above_ratio_watermark():
    class Compressor:
        context_length = 260_000
        max_tokens = 64_000
        threshold_tokens_cap = 400_000

        @staticmethod
        def _compute_threshold_tokens(context_length, ratio, max_tokens):
            return int((context_length - max_tokens) * ratio)

    settings = normalize_speculative_compression_settings({
        "start_ratio": 0.70,
        "hard_ratio": 0.85,
    })
    assert speculative_thresholds(Compressor(), settings) == (137_200, 166_600)


def test_thresholds_collapse_to_hard_only_on_small_windows():
    """A small window whose floor collapses soft==hard still schedules at the
    collapse point (hard-only tool-wait overlap), never above the window."""
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(model="test", config_context_length=64_000)
    settings = normalize_speculative_compression_settings({})
    soft, hard = speculative_thresholds(compressor, settings)
    assert soft == hard == 54_400
    assert soft < 64_000


def test_invalid_boolean_warns_and_falls_back_to_default(caplog):
    with caplog.at_level(logging.WARNING):
        settings = normalize_speculative_compression_settings({
            "enabled": "treu",
            "during_tool_wait": "yess",
        })

    assert settings.enabled is False  # default
    assert settings.during_tool_wait is True  # default, not silently False
    assert "Invalid compression.speculative enabled" in caplog.text
    assert "Invalid compression.speculative during_tool_wait" in caplog.text


def test_codex_native_and_plugin_context_engines_are_not_eligible():
    from agent.context_compressor import ContextCompressor

    builtin = ContextCompressor(model="test", config_context_length=1_000)
    assert is_builtin_compression_eligible(
        api_mode="chat_completions", context_engine=builtin
    )
    assert not is_builtin_compression_eligible(
        api_mode="codex_app_server", context_engine=builtin
    )
    assert not is_builtin_compression_eligible(
        api_mode="chat_completions", context_engine=object()
    )
