from __future__ import annotations

import json

import pytest

from hermes_state import SessionDB, detect_session_driver


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _origin(db: SessionDB, session_id: str) -> dict:
    with db._lock:
        row = db._conn.execute(
            "SELECT origin_json FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row["origin_json"]) if row["origin_json"] else {}


class TestDetectSessionDriver:
    def test_explicit_override_wins(self) -> None:
        environ = {"HERMES_SESSION_DRIVER": "codex", "CLAUDECODE": "1"}
        assert detect_session_driver(environ) == "codex"

    def test_claude_code_env_detected(self) -> None:
        assert detect_session_driver({"CLAUDECODE": "1"}) == "claude-code"
        assert (
            detect_session_driver({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
            == "claude-code"
        )

    def test_no_markers_means_none(self) -> None:
        assert detect_session_driver({}) is None
        assert detect_session_driver({"CLAUDECODE": ""}) is None

    def test_malformed_override_rejected(self) -> None:
        assert detect_session_driver({"HERMES_SESSION_DRIVER": "x" * 64}) is None
        assert detect_session_driver({"HERMES_SESSION_DRIVER": "bad name!"}) is None
        assert detect_session_driver({"HERMES_SESSION_DRIVER": "  "}) is None

    def test_override_normalized_to_lowercase(self) -> None:
        assert detect_session_driver({"HERMES_SESSION_DRIVER": "Codex"}) == "codex"


class TestCreateSessionDriverStamp:
    def test_cli_session_gets_driver_stamp(self, db, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        db.create_session(session_id="s-cli", source="cli")
        assert _origin(db, "s-cli") == {"driver": "claude-code"}

    def test_tui_session_gets_driver_stamp(self, db, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_SESSION_DRIVER", "codex")
        db.create_session(session_id="s-tui", source="tui")
        assert _origin(db, "s-tui") == {"driver": "codex"}

    def test_platform_session_never_stamped(self, db, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        db.create_session(session_id="s-tg", source="telegram")
        db.create_session(session_id="s-desktop", source="desktop")
        assert _origin(db, "s-tg") == {}
        assert _origin(db, "s-desktop") == {}

    def test_no_driver_env_means_no_stamp(self, db, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        monkeypatch.delenv("HERMES_SESSION_DRIVER", raising=False)
        db.create_session(session_id="s-plain", source="cli")
        assert _origin(db, "s-plain") == {}

    def test_existing_origin_json_never_clobbered(self, db, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        db.create_session(session_id="s-peer", source="cli")
        db.record_gateway_session_peer(
            "s-peer",
            source="cli",
            session_key="key-1",
            origin_json='{"routing": "data"}',
        )
        db.create_session(session_id="s-peer", source="cli")
        assert _origin(db, "s-peer") == {"routing": "data"}


class TestDriverInSessionListing:
    def test_list_sessions_rich_exposes_driver(self, db, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        db.create_session(session_id="s-driven", source="cli")
        monkeypatch.delenv("CLAUDECODE")
        db.create_session(session_id="s-undriven", source="cli")

        rows = {
            row["id"]: row
            for row in db.list_sessions_rich(source="cli", limit=10)
        }

        assert rows["s-driven"]["driver"] == "claude-code"
        assert rows["s-undriven"]["driver"] is None
