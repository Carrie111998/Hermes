"""Test auto-migration of lagging profile state.db on cross-profile endpoints.

After a Hermes update, a profile's ``state.db`` can lag the current schema
(because the gateway only migrates the **default** DB on startup).  When the
Desktop sidebar aggregator opens those DBs read-only, ``list_sessions_rich``
fails with ``sqlite3.OperationalError: no such column`` and the profile
contributes **zero** sessions — an empty sidebar that looks like data loss.

The fix: when ``get_profiles_sessions`` and ``get_profiles_sessions_sidebar``
catch a ``sqlite3.OperationalError`` whose message contains "no such column",
they close the read-only connection, open the DB read-write once (which
triggers ``_init_schema`` → column reconciliation), then re-open read-only
and retry the query.
"""

import sqlite3
from pathlib import Path

import pytest


class TestProfileSessionSchemaMigration:
    """Verify that lagging profile DBs are auto-migrated instead of
    silently returning zero sessions on both cross-profile endpoints."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        self.monkeypatch = monkeypatch
        self.hermes_home = get_hermes_home()

    def _create_lagging_db(self, db_path: Path, session_id: str = "laggy-session-1"):
        """Create a state.db whose sessions table is missing the ``archived``
        column, simulating a profile DB from before the feature was added.

        Strategy: create the DB with full schema via SessionDB, add a session,
        then DROP the ``archived`` column.
        """
        from hermes_state import SessionDB

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SessionDB(db_path=db_path, read_only=False)
        try:
            db.create_session(session_id=session_id, source="cli")
            db.append_message(
                session_id=session_id, role="user", content="hello from laggy"
            )
        finally:
            db.close()

        # Drop the archived column to simulate an older schema
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE sessions DROP COLUMN archived")
        conn.commit()
        conn.close()

    def _monkeypatch_profiles(self, extra_profiles: list[dict]):
        """Replace list_profiles with a controlled list that includes
        extra named profiles (with paths under the test HERMES_HOME)."""
        from hermes_cli import profiles as profiles_mod

        default_path = self.hermes_home
        entries = [
            profiles_mod.ProfileInfo(
                name="default",
                path=default_path,
                is_default=True,
                gateway_running=False,
            ),
        ]
        for p in extra_profiles:
            entries.append(
                profiles_mod.ProfileInfo(
                    name=p["name"],
                    path=p["path"],
                    is_default=False,
                    gateway_running=False,
                )
            )

        self.monkeypatch.setattr(profiles_mod, "list_profiles", lambda: entries)

    # ── get_profiles_sessions ──────────────────────────────────────────────

    def test_profiles_sessions_auto_migrates_lagging_schema(self):
        """GET /api/profiles/sessions auto-migrates a lagging profile DB
        instead of silently returning zero sessions for that profile."""
        profile_dir = self.hermes_home / "profiles" / "laggy"
        db_path = profile_dir / "state.db"
        self._create_lagging_db(db_path)
        self._monkeypatch_profiles([{"name": "laggy", "path": profile_dir}])

        resp = self.client.get("/api/profiles/sessions?limit=20&min_messages=0")
        assert resp.status_code == 200
        data = resp.json()

        # No errors for the laggy profile
        laggy_errors = [
            e for e in data.get("errors", []) if e.get("profile") == "laggy"
        ]
        assert len(laggy_errors) == 0, f"Unexpected errors: {laggy_errors}"

        # The laggy profile's session should appear
        sessions = data["sessions"]
        laggy = [s for s in sessions if s.get("profile") == "laggy"]
        assert len(laggy) == 1
        assert laggy[0]["id"] == "laggy-session-1"

    def test_profiles_sessions_migration_is_idempotent(self):
        """Second call to the endpoint should work without re-migrating."""
        profile_dir = self.hermes_home / "profiles" / "laggy2"
        db_path = profile_dir / "state.db"
        self._create_lagging_db(db_path, session_id="idempotent-session")
        self._monkeypatch_profiles([{"name": "laggy2", "path": profile_dir}])

        # First call triggers migration
        resp1 = self.client.get("/api/profiles/sessions?limit=20&min_messages=0")
        assert resp1.status_code == 200

        # Second call should also work (no re-migration needed)
        resp2 = self.client.get("/api/profiles/sessions?limit=20&min_messages=0")
        assert resp2.status_code == 200
        data2 = resp2.json()
        laggy = [s for s in data2["sessions"] if s.get("profile") == "laggy2"]
        assert len(laggy) == 1
        assert laggy[0]["id"] == "idempotent-session"
        assert len(data2.get("errors", [])) == 0

    def test_profiles_sessions_non_schema_operational_error_not_treated_as_migration(self):
        """A non-schema ``sqlite3.OperationalError`` (e.g. a real column missing
        that isn't an expected schema lag, or a corrupt index) should NOT be
        silently migrated — the error should still be reported.

        We simulate this by monkeypatching ``list_sessions_rich`` to raise
        a generic ``sqlite3.OperationalError`` that does NOT mention
        "no such column".
        """
        profile_dir = self.hermes_home / "profiles" / "strange"
        db_path = profile_dir / "state.db"

        from hermes_state import SessionDB
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SessionDB(db_path=db_path, read_only=False)
        try:
            db.create_session(session_id="strange-session", source="cli")
        finally:
            db.close()

        self._monkeypatch_profiles([{"name": "strange", "path": profile_dir}])

        # Monkeypatch SessionDB.list_sessions_rich to raise a non-schema
        # OperationalError (e.g. a disk-full / corrupt-index scenario)
        import hermes_state as hs
        original_list = hs.SessionDB.list_sessions_rich

        def broken_list(self, *args, **kwargs):
            if not hasattr(self, '_already_broken'):
                self._already_broken = True
                raise sqlite3.OperationalError("database disk image is malformed")
            return original_list(self, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hs.SessionDB, "list_sessions_rich", broken_list)
            resp = self.client.get("/api/profiles/sessions?limit=20&min_messages=0")

        assert resp.status_code == 200
        data = resp.json()
        strange_errors = [
            e for e in data.get("errors", []) if e.get("profile") == "strange"
        ]
        # The error should be reported, not silently migrated
        assert len(strange_errors) > 0, \
            f"Expected errors for 'strange' profile, got: {data.get('errors', [])}"

    # ── get_profiles_sessions_sidebar ──────────────────────────────────────

    def test_profiles_sidebar_auto_migrates_lagging_schema(self):
        """GET /api/profiles/sessions/sidebar auto-migrates a lagging
        profile DB instead of silently returning zero sessions."""
        profile_dir = self.hermes_home / "profiles" / "laggy3"
        db_path = profile_dir / "state.db"
        self._create_lagging_db(db_path, session_id="sidebar-laggy")
        self._monkeypatch_profiles([{"name": "laggy3", "path": profile_dir}])

        resp = self.client.get(
            "/api/profiles/sessions/sidebar"
            "?recents_profile=all&recents_limit=20"
            "&cron_limit=50&messaging_limit=100"
        )
        assert resp.status_code == 200
        data = resp.json()

        # No errors for the laggy profile
        laggy_errors = [
            e for e in data.get("errors", []) if e.get("profile") == "laggy3"
        ]
        assert len(laggy_errors) == 0, f"Unexpected errors: {laggy_errors}"

        # The session should appear in the recents slice (source=cli)
        recents = data["recents"]["sessions"]
        laggy_recents = [s for s in recents if s.get("profile") == "laggy3"]
        assert len(laggy_recents) == 1
        assert laggy_recents[0]["id"] == "sidebar-laggy"

    def test_profiles_sidebar_migration_is_idempotent(self):
        """Second call to the sidebar endpoint works without re-migrating."""
        profile_dir = self.hermes_home / "profiles" / "laggy4"
        db_path = profile_dir / "state.db"
        self._create_lagging_db(db_path, session_id="sidebar-idempotent")
        self._monkeypatch_profiles([{"name": "laggy4", "path": profile_dir}])

        # First call triggers migration
        resp1 = self.client.get(
            "/api/profiles/sessions/sidebar"
            "?recents_profile=all&recents_limit=20"
            "&cron_limit=50&messaging_limit=100"
        )
        assert resp1.status_code == 200

        # Second call should also work (already migrated)
        resp2 = self.client.get(
            "/api/profiles/sessions/sidebar"
            "?recents_profile=all&recents_limit=20"
            "&cron_limit=50&messaging_limit=100"
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        laggy = [s for s in data2["recents"]["sessions"] if s.get("profile") == "laggy4"]
        assert len(laggy) == 1
        assert laggy[0]["id"] == "sidebar-idempotent"
        assert len(data2.get("errors", [])) == 0
