from pathlib import Path

import hermes_state


def test_default_session_db_path_uses_current_hermes_home(monkeypatch, tmp_path):
    """A fresh HERMES_HOME must not inherit the import-time DB path."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert hermes_state._resolve_default_db_path() == hermes_home / "state.db"


def test_session_db_constructor_uses_current_hermes_home_without_writing(
    monkeypatch, tmp_path
):
    """SessionDB() resolves the fresh home before opening SQLite."""
    hermes_home = tmp_path / "fresh-hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    captured = {}

    class FakeConnection:
        row_factory = None

        def execute(self, *_args, **_kwargs):
            return self

    def fake_connect(path, **_kwargs):
        captured["path"] = path
        return FakeConnection()

    monkeypatch.setattr(hermes_state.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(hermes_state, "apply_wal_with_fallback", lambda *_a, **_k: None)
    monkeypatch.setattr(hermes_state.SessionDB, "_init_schema", lambda _self: None)

    database = hermes_state.SessionDB()

    assert database.db_path == hermes_home / "state.db"
    assert Path(captured["path"]) == hermes_home / "state.db"


def test_explicit_default_db_path_override_still_wins(monkeypatch, tmp_path):
    """Legacy tests that override DEFAULT_DB_PATH keep their precedence."""
    override = tmp_path / "overridden-state.db"
    hermes_home = tmp_path / "fresh-hermes-home"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", override)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert hermes_state._resolve_default_db_path() == override
