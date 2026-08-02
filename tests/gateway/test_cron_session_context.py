"""Cron execution provenance must be task-local inside long-lived hosts."""

from contextvars import Context

from gateway.session_context import (
    is_cron_session,
    reset_cron_session,
    set_cron_session,
)


def test_cron_session_context_is_local_and_resettable(monkeypatch):
    for name in (
        "HERMES_CRON_SESSION",
        "HERMES_EXEC_ASK",
        "HERMES_GATEWAY_SESSION",
        "_HERMES_GATEWAY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert is_cron_session() is False
    token = set_cron_session()
    try:
        assert is_cron_session() is True
        assert Context().run(is_cron_session) is False
    finally:
        reset_cron_session(token)
    assert is_cron_session() is False


def test_process_env_marker_is_legacy_only_not_gateway_authority(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    assert is_cron_session() is True

    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    assert is_cron_session() is False

    token = set_cron_session()
    try:
        assert is_cron_session() is True
    finally:
        reset_cron_session(token)
