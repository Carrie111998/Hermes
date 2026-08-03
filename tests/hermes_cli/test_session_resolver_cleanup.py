"""SessionDB lifecycle tests for CLI session-name resolution."""

from unittest.mock import MagicMock

from hermes_cli.main import _resolve_session_by_name_or_id


def test_session_resolver_closes_database_after_success(monkeypatch):
    db = MagicMock()
    db.get_session.return_value = {"id": "session-root"}
    db.get_compression_tip.return_value = "session-tip"
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

    result = _resolve_session_by_name_or_id("session-root")

    assert result == "session-tip"
    db.close.assert_called_once_with()


def test_session_resolver_closes_database_after_query_failure(monkeypatch):
    db = MagicMock()
    db.get_session.side_effect = RuntimeError("read failed")
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

    result = _resolve_session_by_name_or_id("session-root")

    assert result is None
    db.close.assert_called_once_with()


def test_session_resolver_keeps_result_when_close_fails(monkeypatch):
    db = MagicMock()
    db.get_session.return_value = {"id": "session-root"}
    db.get_compression_tip.return_value = "session-tip"
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

    result = _resolve_session_by_name_or_id("session-root")

    assert result == "session-tip"
    db.close.assert_called_once_with()
