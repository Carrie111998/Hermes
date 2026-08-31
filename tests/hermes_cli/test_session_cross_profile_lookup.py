"""GET /api/sessions/{id} must find rows that live in another profile store.

Bot chats are written to ``profiles/<bot>/state.db``. Desktop resume often
omits ``?profile=``, so a single-store open 404s even though the sidebar
already listed the session (#94609). These tests pin the fallback: metadata
and messages resolve without the query param, stamp the owning profile, a
missing id still 404s, and a miss must not bootstrap empty ``state.db``
files in unrelated profile directories.
"""

import pytest


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    bot_home = profiles_root / "basselect"
    empty_home = profiles_root / "emptybot"
    for home in (default_home, bot_home, empty_home):
        home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {
        "default": default_home,
        "basselect": bot_home,
        "emptybot": empty_home,
    }


@pytest.fixture
def client(monkeypatch, isolated_profiles):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    auth = TestClient(app)
    auth.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return auth


def _seed_session(db_path, session_id, source, content):
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    try:
        db.create_session(session_id, source=source)
        db.append_message(session_id, role="user", content=content)
    finally:
        db.close()


class TestSessionCrossProfileLookup:
    def test_detail_finds_bot_session_without_profile_query(self, client, isolated_profiles):
        _seed_session(
            isolated_profiles["basselect"] / "state.db",
            "sess-bot-1",
            "telegram",
            "hello from bot",
        )
        _seed_session(
            isolated_profiles["default"] / "state.db",
            "sess-default",
            "cli",
            "hello from default",
        )

        resp = client.get("/api/sessions/sess-bot-1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == "sess-bot-1"
        assert body["profile"] == "basselect"
        assert body["is_default_profile"] is False

        default_resp = client.get("/api/sessions/sess-default")
        assert default_resp.status_code == 200, default_resp.text
        default_body = default_resp.json()
        assert default_body["id"] == "sess-default"
        assert default_body["profile"] == "default"
        assert default_body["is_default_profile"] is True

    def test_messages_find_bot_session_without_profile_query(self, client, isolated_profiles):
        _seed_session(
            isolated_profiles["basselect"] / "state.db",
            "sess-bot-2",
            "telegram",
            "transcript lives here",
        )

        resp = client.get("/api/sessions/sess-bot-2/messages")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_id"] == "sess-bot-2"
        texts = [row.get("content") for row in body["messages"]]
        assert "transcript lives here" in texts

    def test_explicit_profile_still_resolves_and_unknown_id_404s(
        self, client, isolated_profiles
    ):
        _seed_session(
            isolated_profiles["basselect"] / "state.db",
            "sess-bot-3",
            "telegram",
            "owned by basselect",
        )

        resp = client.get("/api/sessions/sess-bot-3", params={"profile": "basselect"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile"] == "basselect"

        missing = client.get("/api/sessions/does-not-exist")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Session not found"

        missing_messages = client.get("/api/sessions/does-not-exist/messages")
        assert missing_messages.status_code == 404

    def test_fallback_does_not_bootstrap_empty_profile_stores(
        self, client, isolated_profiles
    ):
        empty_db = isolated_profiles["emptybot"] / "state.db"
        assert not empty_db.exists()

        _seed_session(
            isolated_profiles["basselect"] / "state.db",
            "sess-bot-4",
            "telegram",
            "do not mint emptybot",
        )

        resp = client.get("/api/sessions/sess-bot-4")
        assert resp.status_code == 200, resp.text
        assert not empty_db.exists()
