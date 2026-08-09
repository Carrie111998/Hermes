"""The TUI gateway's Desktop connection-mode plumbing (#82140).

The Desktop shell already resolves ``local``/``remote`` via
``window.hermesDesktop.getConnection()``. These tests pin the server side of
that announcement: where it is stored, when it is refreshed, and which sessions
are allowed to have one at all.

The helpers under test are pure dict/param transforms, so they run without
standing up a gateway.
"""

import pytest

from gateway.session_context import _DESKTOP_CONNECTION_MODE, _UNSET, _VAR_MAP


def _srv():
    import tui_gateway.server as srv

    return srv


@pytest.fixture(autouse=True)
def _reset_contextvars():
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _DESKTOP_CONNECTION_MODE.set(_UNSET)


def _desktop_session(**extra) -> dict:
    return {"session_key": "k", "source": "desktop", **extra}


class TestNormalizeParam:
    def test_reads_and_normalizes_the_param(self):
        assert _srv()._normalize_connection_mode_param({"connection_mode": "cloud"}) == "remote"

    @pytest.mark.parametrize("params", [None, {}, {"connection_mode": ""}, {"connection_mode": "nope"}])
    def test_missing_or_unknown_is_none(self, params):
        assert _srv()._normalize_connection_mode_param(params) is None


class TestSessionConnectionMode:
    def test_desktop_session_reports_its_mode(self):
        session = _desktop_session(connection_mode="remote")
        assert _srv()._session_connection_mode(session) == "remote"

    @pytest.mark.parametrize("source", ["tui", "telegram", "cli", "kanban"])
    def test_non_desktop_sources_never_report_a_mode(self, source):
        """A stray connection_mode from a non-Desktop client must not be honored."""
        session = {"session_key": "k", "source": source, "connection_mode": "local"}
        assert _srv()._session_connection_mode(session) is None

    def test_missing_session_is_none(self):
        assert _srv()._session_connection_mode(None) is None

    def test_desktop_session_without_an_announcement_is_none(self):
        assert _srv()._session_connection_mode(_desktop_session()) is None


class TestRememberConnectionMode:
    def test_refreshes_the_stored_mode(self):
        """This is what makes a mid-session connection switch land."""
        session = _desktop_session(connection_mode="local")
        _srv()._remember_connection_mode(session, {"connection_mode": "remote"})
        assert session["connection_mode"] == "remote"

    def test_omitted_param_leaves_the_stored_mode_alone(self):
        """An older Desktop build must not erase a mode a newer one announced."""
        session = _desktop_session(connection_mode="remote")
        _srv()._remember_connection_mode(session, {"text": "hello"})
        assert session["connection_mode"] == "remote"

    def test_explicit_unknown_value_clears_to_none(self):
        """Explicitly unknown is 'I don't know', not 'keep believing local'."""
        session = _desktop_session(connection_mode="local")
        _srv()._remember_connection_mode(session, {"connection_mode": "banana"})
        assert session["connection_mode"] is None

    def test_no_session_is_a_noop(self):
        _srv()._remember_connection_mode(None, {"connection_mode": "remote"})


class TestBindSessionContext:
    """``_set_session_context`` is what every turn runs through."""

    def test_binds_a_desktop_session_mode(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        session = _desktop_session(connection_mode="remote")
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        srv._set_session_context("k")
        assert desktop_connection_mode() == "remote"

    def test_non_desktop_session_binds_none(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        session = {"session_key": "k", "source": "tui", "connection_mode": "local"}
        monkeypatch.setattr(srv, "_sessions", {"s1": session}, raising=False)
        srv._set_session_context("k")
        assert desktop_connection_mode() is None

    def test_unknown_session_key_binds_none(self, monkeypatch):
        from gateway.session_context import desktop_connection_mode

        srv = _srv()
        monkeypatch.setattr(srv, "_sessions", {}, raising=False)
        srv._set_session_context("no-such-key")
        assert desktop_connection_mode() is None


def test_new_session_records_carry_a_connection_mode_slot():
    """Both live-session record shapes must have the field _set_session_context reads."""
    srv = _srv()
    record = srv._deferred_session_record(
        "key", cols=80, cwd="", history=[], lease=None, source="desktop",
        connection_mode="remote",
    )
    assert record["connection_mode"] == "remote"
