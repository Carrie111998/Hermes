"""Tests for agent/timeout_error_summary.py — factual transport-timeout summaries.

httpx timeout exceptions stringify to an EMPTY string; before this module,
``_summarize_api_error`` produced a blank summary for them ("API call failed
after 6 retries: " with nothing after the colon).  Ported from
block/buzz#4959.
"""

from types import SimpleNamespace

import httpx
import pytest

from agent.timeout_error_summary import (
    _resolve_configured_timeout,
    summarize_timeout_error,
)
from run_agent import AIAgent


# ── Pure classifier ─────────────────────────────────────────────────


def test_httpx_read_timeout_empty_str_precondition():
    """The bug's precondition: httpx timeouts stringify to nothing."""
    assert str(httpx.ReadTimeout("")) == ""
    assert str(httpx.ConnectTimeout("")) == ""


def test_read_timeout_names_read_phase_and_config_knob():
    summary = summarize_timeout_error(
        httpx.ReadTimeout(""), provider="openrouter", model="some/model"
    )
    assert summary is not None
    assert summary.startswith("read timeout")
    assert "providers.openrouter.request_timeout_seconds" in summary
    assert "config.yaml" in summary


def test_connect_timeout_names_connect_phase_not_config_knob():
    """Connect-phase failures must NOT advise raising the read timeout —
    the endpoint never answered at all."""
    summary = summarize_timeout_error(httpx.ConnectTimeout(""))
    assert summary is not None
    assert summary.startswith("connect timeout")
    assert "request_timeout_seconds" not in summary


def test_pool_and_write_timeouts_classify_as_read_phase():
    for exc in (httpx.PoolTimeout(""), httpx.WriteTimeout("")):
        summary = summarize_timeout_error(exc)
        assert summary is not None
        assert summary.startswith("read timeout")


def test_openai_sdk_api_timeout_error_classifies():
    openai = pytest.importorskip("openai")
    err = openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
    summary = summarize_timeout_error(err, provider="nous")
    assert summary is not None
    assert summary.startswith("read timeout")
    assert "providers.nous.request_timeout_seconds" in summary


def test_non_timeout_errors_return_none():
    for exc in (ValueError("boom"), ConnectionResetError("reset"), Exception("")):
        assert summarize_timeout_error(exc) is None


def test_missing_provider_uses_generic_knob():
    summary = summarize_timeout_error(httpx.ReadTimeout(""))
    assert summary is not None
    assert "providers.<provider>.request_timeout_seconds" in summary


def test_configured_timeout_value_embedded(monkeypatch):
    monkeypatch.setenv("HERMES_API_TIMEOUT", "240")
    summary = summarize_timeout_error(httpx.ReadTimeout(""))
    assert summary is not None
    assert "within 240s" in summary


def test_env_timeout_invalid_values_ignored(monkeypatch):
    for bad in ("abc", "-5", "0"):
        monkeypatch.setenv("HERMES_API_TIMEOUT", bad)
        assert _resolve_configured_timeout(None, None) is None


# ── Integration with AIAgent._summarize_api_error ───────────────────


def test_summarize_api_error_read_timeout_not_blank():
    """The user-visible regression: summary must never be empty for a
    transport timeout."""
    summary = AIAgent._summarize_api_error(
        httpx.ReadTimeout(""), provider="openrouter", model="m"
    )
    assert summary.strip()
    assert "read timeout" in summary


def test_summarize_api_error_backward_compatible_single_arg():
    """Existing single-arg call sites keep working and still get a
    non-blank summary."""
    summary = AIAgent._summarize_api_error(httpx.ReadTimeout(""))
    assert summary.strip()
    assert "read timeout" in summary


def test_summarize_api_error_non_timeout_path_unchanged():
    err = Exception("")
    err.status_code = 400
    err.body = {}
    err.response = SimpleNamespace(
        text='{"error": {"message": "model `foo` does not exist"}}'
    )
    summary = AIAgent._summarize_api_error(err, provider="openrouter")
    assert "model `foo` does not exist" in summary
