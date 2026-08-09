"""Regression tests for #82154 (secondary observation 1).

``_format_exhausted_status`` renders the *stored* last-known error for an
exhausted pool entry without ever re-probing the provider. Every variant it
can render must carry a ``[cached]`` marker so iterative debugging (e.g.
retrying a fix within the ~60min exhaustion cooldown) doesn't mistake a
replayed pre-fix error for a fresh one.
"""

from types import SimpleNamespace

from hermes_cli.auth_commands import _format_exhausted_status
from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK


def _entry(**overrides):
    base = dict(
        last_status=STATUS_EXHAUSTED,
        last_status_at=None,
        last_error_code=400,
        last_error_reason="billing",
        last_error_message="You're out of extra usage.",
        last_error_reset_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_not_exhausted_entry_has_no_status():
    assert _format_exhausted_status(_entry(last_status=STATUS_OK)) == ""


def test_rate_limited_entry_is_labelled_cached():
    status = _format_exhausted_status(
        _entry(last_error_code=429, last_error_reason="rate_limit", last_error_message="rate limit hit")
    )
    assert "[cached]" in status


def test_auth_failed_entry_is_labelled_cached():
    status = _format_exhausted_status(
        _entry(last_error_code=401, last_error_reason="unauthorized", last_error_message="invalid token")
    )
    assert "[cached]" in status
    assert "re-auth may be required" in status


def test_no_reset_time_entry_is_labelled_cached():
    status = _format_exhausted_status(
        _entry(last_error_code=429, last_error_reason="rate_limit", last_error_reset_at=None)
    )
    assert "[cached]" in status
