"""Tests for hermes_state.py â€” SessionDB SQLite CRUD, FTS5 search, export."""

import sqlite3
import time
import json
import threading
from unittest import mock

import pytest

import hermes_state
from agent.session_activity import ActivityProvenance
from hermes_state import SCHEMA_SQL, SCHEMA_VERSION, SessionDB


class _NoFtsCursor(sqlite3.Cursor):
    """Simulate a SQLite build without the fts5 module."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if "USING fts5" in probe:
            raise sqlite3.OperationalError("no such module: fts5")
        if probe in (
            "SELECT * FROM messages_fts LIMIT 0",
            "SELECT * FROM messages_fts_trigram LIMIT 0",
        ):
            raise sqlite3.OperationalError("no such table: " + probe.split()[-3])
        return super().execute(sql, parameters)

    def executescript(self, sql_script):
        if "USING fts5" in sql_script:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().executescript(sql_script)


class _NoFtsConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoFtsCursor)


class _NoFtsExistingTableCursor(_NoFtsCursor):
    """Simulate existing FTS virtual tables under a runtime without FTS5."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if probe in (
            "SELECT * FROM messages_fts LIMIT 0",
            "SELECT * FROM messages_fts_trigram LIMIT 0",
        ):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


class _NoFtsExistingTableConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoFtsExistingTableCursor)


class _NoTrigramCursor(sqlite3.Cursor):
    """Simulate a SQLite build with FTS5 but without the trigram tokenizer."""

    def executescript(self, sql_script):
        if "tokenize='trigram'" in sql_script:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return super().executescript(sql_script)


class _NoTrigramConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoTrigramCursor)


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


@pytest.fixture(autouse=True)
def _no_fts_rebuild_throttle(monkeypatch):
    """Zero the FTS-rebuild inter-chunk throttle for every test in this file.

    ``optimize_fts_storage`` sleeps ``max(_FTS_REBUILD_MIN_PAUSE,
    chunk_cost * _FTS_REBUILD_DUTY_FACTOR)`` between chunks so a LIVE
    gateway/CLI sharing the DB isn't starved of the write lock. Tests run
    against a private tmp-path DB with no concurrent process â€” the sleep
    protects nobody and was pure dead time (measured: 4.1s of a 4.6s
    migration test was time.sleep; ~20s across the file, whose total was
    ~52s). The duty-cycle POLICY (sleep >= 4x chunk cost) stays covered by
    the production constants themselves; no test asserts on wall-clock
    pacing.
    """
    monkeypatch.setattr(SessionDB, "_FTS_REBUILD_MIN_PAUSE", 0.0)
    monkeypatch.setattr(SessionDB, "_FTS_REBUILD_DUTY_FACTOR", 0.0)


# =========================================================================
# Connection lifecycle
# =========================================================================


class TestConnectionLifecycle:
    def test_failed_writable_open_does_not_leak_tracked_connection(
        self, tmp_path, monkeypatch
    ):
        """A failed schema init must close the connection opened before it."""
        from hermes_cli.sqlite_safe_read import has_live_connection

        db_path = tmp_path / "state.db"
        opened = []
        real_connect = hermes_state._connect_tracked_db

        def capture_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(hermes_state, "_connect_tracked_db", capture_connect)
        monkeypatch.setattr(
            SessionDB,
            "_init_schema",
            mock.Mock(side_effect=RuntimeError("schema init failed")),
        )

        try:
            with pytest.raises(RuntimeError, match="schema init failed"):
                SessionDB(db_path=db_path)
            assert has_live_connection(db_path) is False
        finally:
            for conn in opened:
                try:
                    conn.close()
                except Exception:
                    pass

    def test_failed_wal_read_open_does_not_leak_tracked_connection(
        self, tmp_path, monkeypatch
    ):
        """A post-open read setup failure must close its unregistered conn."""
        from hermes_cli import sqlite_safe_read

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        opened = []
        real_connect = hermes_state._connect_tracked_db
        real_pragmas = hermes_state.apply_database_pragmas

        def capture_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def fail_pragmas(*args, **kwargs):
            raise RuntimeError("read setup failed")

        monkeypatch.setattr(hermes_state, "_connect_tracked_db", capture_connect)
        monkeypatch.setattr(hermes_state, "apply_database_pragmas", fail_pragmas)
        before = dict(sqlite_safe_read._live_connections)
        db._wal_active = True

        try:
            with pytest.raises(RuntimeError, match="read setup failed"):
                db._get_read_conn()
            assert sqlite_safe_read._live_connections == before
        finally:
            monkeypatch.setattr(
                hermes_state, "apply_database_pragmas", real_pragmas
            )
            for conn in opened:
                try:
                    conn.close()
                except Exception:
                    pass
            db.close()

    def test_close_closes_wal_read_connection_created_on_worker_thread(
        self, tmp_path
    ):
        """SessionDB.close() must drain read conns created by other threads."""
        from hermes_cli.sqlite_safe_read import has_live_connection

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        db._wal_active = True
        opened = threading.Event()
        release = threading.Event()
        errors = []

        def open_read_connection():
            try:
                assert db._get_read_conn() is not None
                opened.set()
                release.wait(timeout=10)
            except BaseException as exc:
                errors.append(exc)
                opened.set()

        worker = threading.Thread(target=open_read_connection)
        worker.start()
        assert opened.wait(timeout=10)
        assert not errors

        db.close()
        assert has_live_connection(db_path) is False

        release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not errors

    def test_read_only_close_never_requests_wal_checkpoint(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("s1", source="cli")
        writable.close()

        executed = []
        read_only = SessionDB(db_path=db_path, read_only=True)
        read_only._conn.set_trace_callback(executed.append)
        read_only.close()

        assert not any("wal_checkpoint" in sql.lower() for sql in executed)

    def test_writable_close_retains_truncate_checkpoint(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        executed = []
        writable._conn.set_trace_callback(executed.append)

        writable.close()

        assert any(
            "pragma wal_checkpoint(truncate)" == " ".join(sql.lower().split())
            for sql in executed
        )

    def test_read_only_connection_keeps_fts_search_available(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("fts-read-only", source="cli")
        writable.append_message(
            "fts-read-only",
            role="user",
            content="readonlywoodpecker å¤§åˆ«å±±é¡¹ç›®",
        )
        writable.close()

        read_only = SessionDB(db_path=db_path, read_only=True)
        try:
            base_matches = read_only.search_messages("readonlywoodpecker")
            trigram_matches = read_only.search_messages("å¤§åˆ«å±±")
        finally:
            read_only.close()

        assert [match["session_id"] for match in base_matches] == [
            "fts-read-only"
        ]
        assert [match["session_id"] for match in trigram_matches] == [
            "fts-read-only"
        ]

    def test_failed_read_only_open_does_not_leak_tracked_connection(
        self, tmp_path
    ):
        """A malformed store makes the RO FTS probe raise DatabaseError.
        The connection must be closed on that failure path: a leaked tracked
        connection blocks _backup_db_file's raw-copy for the process
        lifetime, so the writable heal that follows would repair WITHOUT its
        forensic backup."""
        import sqlite3

        from hermes_cli.sqlite_safe_read import has_live_connection

        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("s1", source="cli")
        writable.append_message("s1", role="user", content="leak probe")
        writable.close()

        # Corrupt sqlite_master: duplicate messages_fts definition. Any
        # statement on a fresh connection then raises "malformed database
        # schema" (DatabaseError, not the OperationalError the probe eats).
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA writable_schema=ON")
        row = conn.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master "
            "WHERE name='messages_fts'"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO sqlite_master (type,name,tbl_name,rootpage,sql) "
            "VALUES (?,?,?,?,?)",
            row,
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.close()

        with pytest.raises(sqlite3.DatabaseError):
            SessionDB(db_path=db_path, read_only=True)

        assert has_live_connection(db_path) is False

        # The writable heal must still take its forensic backup.
        healed = SessionDB(db_path=db_path, read_only=False)
        healed.close()
        assert list(tmp_path.glob("*malformed-backup*"))


# =========================================================================
# Session lifecycle
# =========================================================================

class TestSessionLifecycle:
    def test_create_and_get_session(self, db):
        sid = db.create_session(
            session_id="s1",
            source="cli",
            model="test-model",
        )
        assert sid == "s1"

        session = db.get_session("s1")
        assert session is not None
        assert session["source"] == "cli"
        assert session["model"] == "test-model"
        assert session["ended_at"] is None





    def test_update_session_cwd_persists_git_branch(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo", git_branch="pets-feature")

        session = db.get_session("s1")
        assert session["cwd"] == "/work/repo"
        assert session["git_branch"] == "pets-feature"


















    def test_end_session_first_reason_wins_across_concurrent_connections(
        self, db
    ):
        """Concurrent finalizers perform one transition, not last-write-wins."""
        import threading

        db.create_session(session_id="s1", source="cron")
        db._conn.execute(
            "CREATE TABLE session_end_audit (reason TEXT NOT NULL)"
        )
        db._conn.execute(
            """
            CREATE TRIGGER audit_session_end
            AFTER UPDATE OF ended_at ON sessions
            WHEN OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL
            BEGIN
                INSERT INTO session_end_audit(reason) VALUES (NEW.end_reason);
            END
            """
        )

        peer = SessionDB(db_path=db.db_path)
        barrier = threading.Barrier(2)
        errors = []

        def _end(session_db, reason):
            try:
                barrier.wait(timeout=5)
                session_db.end_session("s1", reason)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_end, args=(db, "compression")),
            threading.Thread(target=_end, args=(peer, "cron_complete")),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert all(not thread.is_alive() for thread in threads)
            assert errors == []
            audit_rows = db._conn.execute(
                "SELECT reason FROM session_end_audit"
            ).fetchall()
            assert len(audit_rows) == 1
            assert db.get_session("s1")["end_reason"] == audit_rows[0]["reason"]
        finally:
            peer.close()











    def test_update_session_model_clears_browser_lock_and_preserves_lineage(self, db):
        """A later /model switch must replace, not compete with, a Browser lock."""
        db.create_session(
            session_id="s1",
            source="hermes_browser",
            model="x-ai/grok-4.5",
            model_config={
                "_branched_from": "parent-session",
                "browser_model_lock": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "confirmed": True,
                },
            },
        )

        db.update_session_model("s1", "anthropic/claude-opus-4.8")

        session = db.get_session("s1")
        model_config = json.loads(session["model_config"])
        assert session["model"] == "anthropic/claude-opus-4.8"
        assert "browser_model_lock" not in model_config
        assert model_config["_branched_from"] == "parent-session"








    def test_first_accounted_route_replaces_all_route_fielço4ÖÚ$z{-®éÜj×gFW"Ò°Ğ¢æÖS¢6öæâæW†V7WFR†b%$tÔ¶æÖWÒ"’æfWF6†öæR‚•³ĞĞ¢f÷"æÖR–â‚&66†U÷6—¦R"Â&ÖÖ÷6—¦R"Â'FV×÷7F÷&R"Ğ¢ĞĞ¢76W'BgFW"ÓÒ&Vf÷&PĞ¢f–æÆÇ“ Ğ¢6öæâæ6Æ÷6R‚Ğ Ğ Ğ¦6Æ72FW7D–ç6–v‡G5FööÄ6ÆÄ–æFWƒ Ğ¢""%F†R–ç6–v‡G276—7FçBFööÂÖ6ÆÂ66â†2&VF–6FRÖÆ–væVB–æFW‚àĞ Ğ¢–ç6–v‡G4Væv–æRåövWE÷FööÅ÷W6vVòövWE÷6¶–ÆÅ÷W6vVf–ÇFW"ÖW76vW2'Ğ¢&öÆRÒv76—7FçBräBFööÅö6ÆÇ2•2äõBåTÄÆâ'F–Â–æFW‚÷fW"F†@Ğ¢&VF–6FR¶VW2F†R66âöfbF†RgVÆÂÖW76vW6F&ÆRöâÆ&vR7FFRæF"àĞ¢"" Ğ Ğ¢ô”äDU‚Ò&–G…öÖW76vW5ö76—7FçEö6ÆÇ5ö'•÷6W76–öâ Ğ Ğ¢FVbö–æFW…öFVfâ‡6VÆbÂ6öæâ“ Ğ¢&÷rÒ6öæâæW†V7WFR€Ğ¢%4TÄT5B7Âe$ôÒ7Æ—FUöÖ7FW"t„U$RG—RÒv–æFW‚räBæÖRÒò"ÀĞ¢‡6VÆbåô”äDU‚Â’ÀĞ¢’æfWF6†öæR‚Ğ¢&WGW&â&÷u²'7Â%Ò–b&÷rVÇ6RæöæPĞ Ğ¢FVbFW7Eö–æFW…ö7&VFVEööåög&W6…öF"‡6VÆbÂF×÷F‚“ Ğ¢F"Ò6W76–öäD"†F%÷Fƒ×F×÷F‚ò&g&W6‚æF""Ğ¢G'“ Ğ¢7ÂÒ6VÆbåö–æFW…öFVfâ†F"åö6öæâĞ¢76W'B7Â—2æ÷BæöæRÂ''F–Â–æFW‚Ö—76–æröâg&W6‚FF&6R Ğ¢2'F–Â&VF–6FR×W7BÖF6‚F†RVW&–VB&÷w2W†7FÇ’àĞ¢76W'B'&öÆRÒv76—7FçBr"–â7ÀĞ¢76W'B'FööÅö6ÆÇ2•2äõBåTÄÂ"–â7ÀĞ¢f–æÆÇ“ Ğ¢F"æ6Æ÷6R‚Ğ Ğ¢FVbFW7Eö–æFW…ö7&VFVEööåöW†—7F–æuöF"‡6VÆbÂF×÷F‚“ Ğ¢""%&V÷Væ–ærD"F†B&VFFW2F†R–æFW‚×W7B7&VFR—B…44„TÔõ5Â—0Ğ¢&R×'VâöâWfW'’÷Vã²&öÆR÷FööÅö6ÆÇ2&R÷&–v–æÂ&6R6öÇVÖç2’â"" Ğ¢F%÷F‚ÒF×÷F‚ò&ÆVv7’æF" Ğ¢F"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢26–×VÆFRFF&6R7&VFVB&Vf÷&RF†R–æFW‚6†—VBàĞ¢F"åö6öæâæW†V7WFR†b$E$õ”äDU‚”bU„•5E2·6VÆbåô”äDU‡Ò"Ğ¢F"åö6öæâæ6öÖÖ—B‚Ğ¢76W'B6VÆbåö–æFW…öFVfâ†F"åö6öæâ’—2æöæPĞ¢F"æ6Æ÷6R‚Ğ Ğ¢F#"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢76W'B6VÆbåö–æFW…öFVfâ†F#"åö6öæâ’—2æ÷BæöæRÂ€Ğ¢&–æFW‚æ÷B&V7&VFVBv†Vâ&V÷Væ–ærâW†—7F–ærFF&6R Ğ¢Ğ¢f–æÆÇ“ Ğ¢F#"æ6Æ÷6R‚Ğ Ğ¢FVbFW7Eö–æFW…÷&VF–6FUö—5÷'F–Â‡6VÆbÂF"“ Ğ¢""%F†R–æFW‚6÷fW'2öæÇ’F†R76—7FçBFööÂÖ6ÆÂ&÷w2–ç6–v‡G2&VG2àĞ Ğ¢VW'’×Æâ6÷fW&vR‡F†BF†R–ç6–v‡G2VW&–W27GVÆÇ’6VÆV7BF†—0Ğ¢–æFW‚Âf÷"&÷F‚66÷W2Âv—F†÷WBäÅ•¤R’Æ—fW2v—F‚F†RVW&–W2–àĞ¢FW7G2övVçB÷FW7Eö–ç6–v‡G2ç’àĞ¢"" Ğ¢7ÂÒ6VÆbåö–æFW…öFVfâ†F"åö6öæâĞ¢76W'B7Â—2æ÷BæöæPĞ¢76W'B%t„U$R"–â7ÀĞ¢76W'B'&öÆRÒv76—7FçBr"–â7ÀĞ¢76W'B'FööÅö6ÆÇ2•2äõBåTÄÂ"–â7ÀĞ¦6Æ72FW7DgG5&V'V–ÆDf–æ—6…v—F†÷WEG&–w&Ó Ğ¢""$âeE2–æFW‚F†BF†R'VçF–ÖR6ææ÷BÖ–çF–â×W7Bæ÷BvVFvRF†R7F÷&RàĞ Ğ¢Gvò–æFWVæFVçBf–ÇW&R6—FW26†&VBöæR&ö÷B6†S¢6öFRF†Bw&—FW2FğĞ¢ÖW76vW5ögG5÷G&–w&Öv—F†÷WBf—'7B6†V6¶–ærF†RF&ÆR—27GVÆÇĞ¢&W6VçBâ—B—2ÆVv—F–ÖFVÇ’'6VçBv†VæWfW"F†RG&–w&Ò–æFW‚—0Ğ¢Væf–Æ&ÆR…5Æ—FR'V–ÆBv—F†÷WBF†RFö¶Væ—¦W"’ÂæB—B6âÇ6ò&RÆVg@Ğ¢'6VçB'’â–çFW''WFVBÖ–w&F–öâ÷"'F–ÆÇ’ÖÆ–VB66†VÖ6†ævRàĞ¢"" Ğ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷6VVB†F%÷F‚ÂãÓc“ Ğ¢6VVFVBÒ6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢6VVFVBæ7&VFU÷6W76–öâ‡6W76–öåö–CÒ'3"Â6÷W&6SÒ&6Æ’"Ğ¢f÷"’–â&ævR†â“ Ğ¢6VVFVBæVæEöÖW76vR€Ğ¢'3"ÀĞ¢&öÆSÒ‚'W6W""–b’R2ÓÒ Ğ¢VÇ6R&76—7FçB"–b’R2ÓÒVÇ6R'FööÂ"’ÀĞ¢6öçFVçCÖb'6VçF–æVÂ–ÆöB¶—Ò¦V'&"ÀĞ¢Ğ¢†–v…÷vFW"Ò6VVFVBåö6öæâæW†V7WFR€Ğ¢%4TÄT5B4ôÄU44R„Ô‚†–B’Â’e$ôÒÖW76vW2 Ğ¢’æfWF6†öæR‚•³ĞĞ¢f–æÆÇ“ Ğ¢6VVFVBæ6Æ÷6R‚Ğ¢&WGW&â†–v…÷vFW Ğ Ğ¢FVbFW7E÷&V'V–ÆEöf–æ—6…÷6¶—5÷G&–w&Õ÷v†Vå÷Væf–Æ&ÆR€Ğ¢6VÆbÂF×÷F‚ÂÖöæ¶W—F6€Ğ¢“ Ğ¢""&÷F–Ö—¦UögG5÷7F÷&vR‚’6ö×ÆWFW2v†VâF†RG&–w&Ò–æFW‚—2'6VçBàĞ Ğ¢gG5÷&V'V–ÆE÷7FW‚–Ç&VG’wV&G2—G2&6¶f–ÆÂ”å4U%BöàĞ¢÷G&–w&Õöf–Æ&ÆV²ögG5÷&V'V–ÆEöf–æ—6‚‚–w2&÷VæF'’7vVWF–@Ğ¢æ÷BÂ6òf–æ—6†–ærFVfW'&VB&V'V–ÆBöâG&–w&ÒÖÆW72'VçF–ÖR&—6V@Ğ¢æò7V6‚F&ÆS¢ÖW76vW5ögG5÷G&–w&ÖæB&÷'FVBF†Rv†öÆPĞ¢÷F–Ö—¦F–öââF†R&6R–æFW‚×W7B7F–ÆÂ&R7vWBæBF†RÖ&¶W'0Ğ¢6ÆV&VBàĞ¢"" Ğ¢F%÷F‚ÒF×÷F‚ò'7FFRæF" Ğ¢†–v…÷vFW"Ò6VÆbå÷6VVB†F%÷F‚Ğ Ğ¢&VÅö6öææV7BÒ7Æ—FS2æ6öææV7@Ğ Ğ¢FVb6öææV7E÷v—F†÷WE÷G&–w&Ò‚¦&w2Â¢¦·v&w2“ Ğ¢·v&w5²&f7F÷'’%ÒÒôæõG&–w&Ô6öææV7F–öàĞ¢&WGW&â&VÅö6öææV7B‚¦&w2Â¢¦·v&w2Ğ Ğ¢Ööæ¶W—F6‚ç6WFGG"€Ğ¢&†W&ÖW5÷7FFRç7Æ—FS2æ6öææV7B"Â6öææV7E÷v—F†÷WE÷G&–w&ĞĞ¢Ğ¢F"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢76W'BF"å÷G&–w&Õöf–Æ&ÆR—2fÇ6PĞ¢2G&–w&ÒÖÆW72'VçF–ÖRÆVfW2æòG&–w&Ò–æFW‚öâF—6²àĞ¢F"åö6öæâæW†V7WFR‚$E$õD$ÄR”bU„•5E2ÖW76vW5ögG5÷G&–w&Ò"Ğ¢F"åö6öæâæ6öÖÖ—B‚Ğ¢76W'BF"åögG5÷F&ÆUöW†—7G2‚&ÖW76vW5ögG5÷G&–w&Ò"’—2fÇ6PĞ Ğ¢2WBF†RD"–âF†RVæF–ærÖFVfW'&VB×&V'V–ÆB7FFRàĞ¢f÷"¶W’ÂfÇVR–â€Ğ¢‚&gG5÷&V'V–ÆEö†–v…÷vFW""Â7G"††–v…÷vFW"’’ÀĞ¢‚&gG5÷&V'V–ÆE÷&öw&W72"Â7G"††–v…÷vFW"’’ÀĞ¢“ Ğ¢F"åö6öæâæW†V7WFR€Ğ¢$”å4U%B”åDò7FFUöÖWF†¶W’ÂfÇVR’dÅTU2ƒòÂò’ Ğ¢$ôâ4ôädÄ”5B†¶W’’DòUDDR4UBfÇVRÒW†6ÇVFVBçfÇVR"ÀĞ¢†¶W’ÂfÇVR’ÀĞ¢Ğ¢F"åö6öæâæ6öÖÖ—B‚Ğ Ğ¢2&RÖf—‚F†—2&—6VB÷W&F–öæÄW'&÷"‚&æò7V6‚F&ÆS¢âââ"’àĞ¢F"åögG5÷&V'V–ÆEöf–æ—6‚‚Ğ Ğ¢2F†R7vVW&âFò6ö×ÆWF–öã¢Ö&¶W'26ÆV&VN(
`Ğ¢76W'BF"ævWEöÖWF‚&gG5÷&V'V–ÆEö†–v…÷vFW""’—2æöæPĞ¢76W'BF"ævWEöÖWF‚&gG5÷&V'V–ÆE÷&öw&W72"’—2æöæPĞ¢2(
fæBF†R&6R–æFW‚—27F–ÆÂW6&ÆR‡F†Rf—‚×W7Bæ÷BF—6&ÆPĞ¢2&VÂ6V&6‚FòFöFvRF†RW'&÷"’àĞ¢76W'BF"ç6V&6…öÖW76vW2‚'¦V'&"Ğ¢f–æÆÇ“ Ğ¢F"æ6Æ÷6R‚Ğ Ğ¢FVbFW7Eö÷F–Ö—¦UögG5÷7F÷&vU÷7V66VVG5÷v—F†÷WE÷G&–w&Ò€Ğ¢6VÆbÂF×÷F‚ÂÖöæ¶W—F6€Ğ¢“ Ğ¢""$VæB×FòÖVæC¢F†RV&Æ–2÷F–Ö—¦RVçG'’ö–çB&WGW&ç2ö³ÕG'VRâ"" Ğ¢F%÷F‚ÒF×÷F‚ò'7FFRæF" Ğ¢†–v…÷vFW"Ò6VÆbå÷6VVB†F%÷F‚Ğ Ğ¢&VÅö6öææV7BÒ7Æ—FS2æ6öææV7@Ğ Ğ¢FVb6öææV7E÷v—F†÷WE÷G&–w&Ò‚¦&w2Â¢¦·v&w2“ Ğ¢·v&w5²&f7F÷'’%ÒÒôæõG&–w&Ô6öææV7F–öàĞ¢&WGW&â&VÅö6öææV7B‚¦&w2Â¢¦·v&w2Ğ Ğ¢Ööæ¶W—F6‚ç6WFGG"€Ğ¢&†W&ÖW5÷7FFRç7Æ—FS2æ6öææV7B"Â6öææV7E÷v—F†÷WE÷G&–w&ĞĞ¢Ğ¢F"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢F"åö6öæâæW†V7WFR‚$E$õD$ÄR”bU„•5E2ÖW76vW5ögG5÷G&–w&Ò"Ğ¢F"åö6öæâæ6öÖÖ—B‚Ğ¢76W'BF"å÷G&–w&Õöf–Æ&ÆR—2fÇ6PĞ¢f÷"¶W’ÂfÇVR–â€Ğ¢‚&gG5÷&V'V–ÆEö†–v…÷vFW""Â7G"††–v…÷vFW"’’ÀĞ¢‚&gG5÷&V'V–ÆE÷&öw&W72"Â#"’ÀĞ¢“ Ğ¢F"åö6öæâæW†V7WFR€Ğ¢$”å4U%B”åDò7FFUöÖWF†¶W’ÂfÇVR’dÅTU2ƒòÂò’ Ğ¢$ôâ4ôädÄ”5B†¶W’’DòUDDR4UBfÇVRÒW†6ÇVFVBçfÇVR"ÀĞ¢†¶W’ÂfÇVR’ÀĞ¢Ğ¢F"åö6öæâæ6öÖÖ—B‚Ğ Ğ¢&W7VÇBÒF"æ÷F–Ö—¦UögG5÷7F÷&vR‡f7WVÓÔfÇ6RĞ¢76W'B&W7VÇE²&ö²%Ò—2G'VPĞ¢76W'BF"ævWEöÖWF‚&gG5÷&V'V–ÆEö†–v…÷vFW""’—2æöæPĞ¢76W'BF"ç6V&6…öÖW76vW2‚'¦V'&"Ğ¢f–æÆÇ“ Ğ¢F"æ6Æ÷6R‚Ğ Ğ Ğ Ğ¦6Æ72FW7EW&f÷&Öæ6U&vÖ4VæEFôVæC Ğ¢""$S$RwV&Bf÷""3ssSS¢6öæf–rÖvFVB66†U÷6—¦RòÖÖ÷6—¦RğĞ¢FV×÷7F÷&R×W7B&V6‚UdU%’6öææV7F–öâG—R‡w&—FW"Â&VBÖöæÇĞ¢7&÷72×&öf–ÆRGF6‚ÂtÂW"×F‡&VB&VFW"’(	BæBFVfVÇB–ç7FÆÇ0Ğ¢†æòFF&6S¦¶W—2’×W7B6VR'—FRÖ–FVçF–6Â5Æ—FRFVfVÇG2àĞ Ğ¢äõDS¢5Æ—FRw26ö×–ÆVBÖ–âFVfVÇBf÷"66†U÷6—¦V—2Ç&VGĞ¢Ó#Â6òF†R6öæf–wW&VBfÇVR†W&R—2Óc(	BfÇVRF†PĞ¢FW7B6â7GVÆÇ’F—67&–Ö–æFRg&öÒF†RFVfVÇB†&WfW'FVB&ö@Ğ¢6†ævR×W7Bd”ÂF†—2FW7BÂæ÷B66–FVçFÆÇ’72—B’àĞ¢"" Ğ Ğ¢$tÔ2Ò‚&66†U÷6—¦R"Â&ÖÖ÷6—¦R"Â'FV×÷7F÷&R"Ğ¢4ôäd”uU$TBÒ²&66†U÷6—¦R#¢ÓcÂ&ÖÖ÷6—¦R#¢CƒSsbÂ'FV×÷7F÷&R#¢'ĞĞ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷&VB†6öæâ“ Ğ¢&WGW&â°Ğ¢æÖS¢6öæâæW†V7WFR†b%$tÔ¶æÖWÒ"’æfWF6†öæR‚•³ĞĞ¢f÷"æÖR–â‚&66†U÷6—¦R"Â&ÖÖ÷6—¦R"Â'FV×÷7F÷&R"Ğ¢ĞĞ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷7Æ—FUöFVfVÇG2‡F×÷F‚“ Ğ¢–×÷'B7Æ—FS0Ğ Ğ¢6öæâÒ7Æ—FS2æ6öææV7B‡7G"‡F×÷F‚ò&&6VÆ–æRæF""’Ğ¢G'“ Ğ¢&WGW&â°Ğ¢æÖS¢6öæâæW†V7WFR†b%$tÔ¶æÖWÒ"’æfWF6†öæR‚•³ĞĞ¢f÷"æÖR–â‚&66†U÷6—¦R"Â&ÖÖ÷6—¦R"Â'FV×÷7F÷&R"Ğ¢ĞĞ¢f–æÆÇ“ Ğ¢6öæâæ6Æ÷6R‚Ğ Ğ¢FVbög&W6…ö†öÖR‡6VÆbÂF×÷F‚ÂÖöæ¶W—F6‚Â6öæf–u÷FW‡CÔæöæR“ Ğ¢–×÷'B†W&ÖW5÷7FFPĞ Ğ¢2Æö6ÂfVçg2Ö’'VæFÆRtÂ×&W6WB×gVÆæW&&ÆR5Æ—FR†Rærâ2ãCbã’ÀĞ¢2v†–6‚v÷VÆB6–ÆVçFÇ’F—6&ÆRtÂæB6¶—F†RW"×F‡&VB&VFW Ğ¢2F‚âf÷&6RtÂVÆ–v–&–Æ—G’6òövWE÷&VEö6öæâ—2G'VÇ’W†W&6—6V@Ğ¢2†W7F&Æ—6†VBGFW&âW6VB'’F†RtÂFW7G2&÷fR’àĞ¢Ööæ¶W—F6‚ç6WFGG"€Ğ¢†W&ÖW5÷7FFRÀĞ¢&—5÷7Æ—FU÷vÅ÷&W6WE÷gVÆæW&&ÆR"ÀĞ¢ÆÖ&FfW'6–öåö–æfóÔæöæS¢fÇ6RÀĞ¢Ğ¢†öÖRÒF×÷F‚ò&†W&ÖW5ö†öÖR Ğ¢†öÖRæÖ¶F—"‚Ğ¢Ööæ¶W—F6‚ç6WFVçb‚$„U$ÔU5ô„ôÔR"Â7G"††öÖR’Ğ¢–b6öæf–u÷FW‡B—2æ÷BæöæS Ğ¢††öÖRò&6öæf–rç–ÖÂ"’çw&—FU÷FW‡B†6öæf–u÷FW‡BĞ¢&WGW&â†öÖPĞ Ğ¢FVbFW7Eö6öæf–wW&VE÷&vÖ5÷&V6…öÆÅö6öææV7F–öå÷G—W2€Ğ¢6VÆbÂF×÷F‚ÂÖöæ¶W—F6€Ğ¢“ Ğ¢g&öÒ†W&ÖW5÷7FFR–×÷'B6W76–öäD Ğ Ğ¢†öÖRÒ6VÆbåög&W6…ö†öÖR€Ğ¢F×÷F‚ÀĞ¢Ööæ¶W—F6‚ÀĞ¢&FF&6S¥Æâ Ğ¢"66†U÷6—¦S¢ÓcÆâ Ğ¢"FV×÷7F÷&S¢%Æâ Ğ¢"ÖÖ÷6—¦S¢CƒSseÆâ"ÀĞ¢Ğ¢F%÷F‚Ò†öÖRò'7FFRæF" Ğ¢F"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢2w&—FW"6öææV7F–öâàĞ¢76W'B6VÆbå÷&VB†F"åö6öæâ’ÓÒ6VÆbä4ôäd”uU$T@Ğ¢2tÂW"×F‡&VB&VFW"àĞ¢&6öæâÒF"åövWE÷&VEö6öæâ‚Ğ¢76W'B&6öæâ—2æ÷BæöæRÂ%tÂ&VFW"W‡V7FVBöâÆö6Âf–ÆW7—7FVÒ Ğ¢76W'B6VÆbå÷&VB‡&6öæâ’ÓÒ6VÆbä4ôäd”uU$T@Ğ¢f–æÆÇ“ Ğ¢F"æ6Æ÷6R‚Ğ Ğ¢2&VBÖöæÇ’7&÷72×&öf–ÆRGF6‚àĞ¢&òÒ6W76–öäD"†F%÷FƒÖF%÷F‚Â&VEööæÇ“ÕG'VRĞ¢G'“ Ğ¢76W'B6VÆbå÷&VB‡&òåö6öæâ’ÓÒ6VÆbä4ôäd”uU$T@Ğ¢f–æÆÇ“ Ğ¢&òæ6Æ÷6R‚Ğ Ğ¢FVbFW7EöFVfVÇG5÷Væ6†ævVE÷v—F†÷WEö6öæf–r‡6VÆbÂF×÷F‚ÂÖöæ¶W—F6‚“ Ğ¢""$æòFF&6S¢¶W—2–â6öæf–rç–ÖÂ(i"5Æ—FRFVfVÇG2VçF÷V6†VBâ"" Ğ¢g&öÒ†W&ÖW5÷7FFR–×÷'B6W76–öäD Ğ Ğ¢FVfVÇG2Ò6VÆbå÷7Æ—FUöFVfVÇG2‡F×÷F‚Ğ¢†öÖRÒ6VÆbåög&W6…ö†öÖR‡F×÷F‚ÂÖöæ¶W—F6‚Â6öæf–u÷FW‡CÔæöæRĞ¢F%÷F‚Ò†öÖRò'7FFRæF" Ğ¢F"Ò6W76–öäD"†F%÷FƒÖF%÷F‚Ğ¢G'“ Ğ¢76W'B6VÆbå÷&VB†F"åö6öæâ’ÓÒFVfVÇG0Ğ¢&6öæâÒF"åövWE÷&VEö6öæâ‚Ğ¢–b&6öæâ—2æ÷BæöæS Ğ¢76W'B6VÆbå÷&VB‡&6öæâ’ÓÒFVfVÇG0Ğ¢f–æÆÇ“ Ğ¢F"æ6Æ÷6R‚Ğ Ğ¢&òÒ6W76–öäD"†F%÷FƒÖF%÷F‚Â&VEööæÇ“ÕG'VRĞ¢G'“ Ğ¢76W'B6VÆbå÷&VB‡&òåö6öæâ’ÓÒFVfVÇG0Ğ¢f–æÆÇ“ Ğ¢&òæ6Æ÷6R‚Ğ Ğ Ğ¦6Æ72FW7DgG3U6æ—F—¦W$6†&7FW$6Æ73 Ğ¢""$WfW'’6†&7FW"eE3R&V¦V7G2÷WG6–FRV÷FVB‡&6R×W7B&R7G&—VBàĞ Ğ¢7W'f—f÷"&V6†W2ÔD4‚&ræB&—6W2Âv†–6‚F†RW†V7WFR6—FR7vÆÆ÷w0Ğ¢–çFò¦W&ò&W7VÇG2(	B6òF†R6V&6‚6–ÆVçFÇ’f–æG2æ÷F†–ær&F†W"F†àĞ¢W'&÷&–ærâ76W'F–öç2'VâF†R6æ—F—¦VBFW‡Bv–ç7B&VÂeE3RF&ÆRàĞ¢"" Ğ Ğ¢7FF–6ÖWF†ö@Ğ¢FVbögG5÷F&ÆR‚“ Ğ¢–×÷'B7Æ—FS0Ğ Ğ¢6öæâÒ7Æ—FS2æ6öææV7B‚#¦ÖVÖ÷'“¢"Ğ¢6öæâæW†V7WFR‚$5$TDRd•%ETÂD$ÄRBU4”ärgG3R†6öçFVçB’"Ğ¢6öæâæW†V7WFR€Ğ¢$”å4U%B”åDòB†6öçFVçB’dÅTU2 Ğ¢"‚vÖVWBÖRBW6W"†÷7B&÷WBvFWv’'Vâ’—B2S"r’ Ğ¢Ğ¢&WGW&â6öæàĞ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷6æ—F—¦R‡VW'’“ Ğ¢g&öÒ†W&ÖW5÷7FFU÷6V&6‚–×÷'B6W76–öå6V&6„Ö—†–àĞ Ğ¢&WGW&â6W76–öå6V&6„Ö—†–âå÷6æ—F—¦UögG3U÷VW'’‡VW'’Ğ Ğ¢—FW7BæÖ&²ç&ÖWG&—¦R€Ğ¢'VW'’"ÀĞ¢°Ğ¢&—Bw2"Â2÷7G&÷†R(	B÷&F–æ'’&÷6PĞ¢&vFWv’÷'Vâç’"Â2F‚6W&F÷ Ğ¢'W6W$†÷7B"Â2VÖ–Âò†æFÆPĞ¢&Æ""Â26öÖÖĞ¢'v‡“ò"Â2VW7F–öâÖ&°Ğ¢&SÖÖ3""Â2WVÇ0Ğ¢&¶""Â&""Â&f""Â&Æ""Â'‡ç’"ÀĞ¢"7Fr"Â"FFöÆÆ""Â%¶'&6¶WEÒ"Â#ÇFsâ"ÀĞ¢"$3¥ÇF…Æf–ÆR"Â2&6·6Æ6€Ğ¢ÒÀĞ¢Ğ¢FVbFW7E÷VW'•÷7F—5÷'6&ÆR‡6VÆbÂVW'’“ Ğ¢6öæâÒ6VÆbåögG5÷F&ÆR‚Ğ¢6æ—F—¦VBÒ6VÆbå÷6æ—F—¦R‡VW'’Ğ¢–bæ÷B6æ—F—¦VBç7G&—‚“ Ğ¢&WGW&àĞ¢2&—6W27Æ—FS2ä÷W&F–öæÄW'&÷"–b7V6–Â6†&7FW"7W'f—fVBàĞ¢6öæâæW†V7WFR‚%4TÄT5B6÷VçB‚¢’e$ôÒBt„U$RBÔD4‚ò"Â‡6æ—F—¦VBÂ’’æfWF6†öæR‚Ğ Ğ¢FVbFW7E÷Æ–å÷FW&×5ö&U÷VçF÷V6†VB‡6VÆb“ Ğ¢76W'B6VÆbå÷6æ—F—¦R‚&†VÆÆòv÷&ÆB"’ç7Æ—B‚’ÓÒ²&†VÆÆò"Â'v÷&ÆB%ĞĞ Ğ¢FVbFW7E÷V÷FVE÷‡&6U÷7W'f—fW2‡6VÆb“ Ğ¢76W'Br&W†7B‡&6R"r–â6VÆbå÷6æ—F—¦R‚r&W†7B‡&6R"rĞ Ğ¢FVbFW7Eö‡—†VåöF÷GFVE÷FW&Õ÷7F–ÆÅ÷V÷FVB‡6VÆb“ Ğ¢27FWRw2&V†f–÷W"×W7Bæ÷B&Vw&W73¢×’Öæ6öæf–rçG27F—2öæRFW&ÒàĞ¢76W'Br&×’Öæ6öæf–rçG2"r–â6VÆbå÷6æ—F—¦R‚&×’Öæ6öæf–rçG2"Ğ Ğ¢FVbFW7E÷&Vf—…÷7F%÷7F–ÆÅ÷v÷&·2‡6VÆb“ Ğ¢6öæâÒ6VÆbåögG5÷F&ÆR‚Ğ¢6æ—F—¦VBÒ6VÆbå÷6æ—F—¦R‚&vFR¢"Ğ¢&÷w2Ò6öæâæW†V7WFR€Ğ¢%4TÄT5B6÷VçB‚¢’e$ôÒBt„U$RBÔD4‚ò"Â‡6æ—F—¦VBÂĞ¢’æfWF6†öæR‚Ğ¢76W'B&÷w5³ÒÓÒĞ Ğ¢FVbFW7E÷W&6VçE÷7G&—VEöf÷%öæöåö6¦µ÷VW'’‡6VÆb“ Ğ¢2R—2¶WBöæÇ’f÷"F†R4¤²Ä”´RfÆÆ&6³²æöâÔ4¤²VW'’æWfW Ğ¢2&V6†W2F†BfÆÆ&6²Â6òR×W7B&R7G&—VB&Vf÷&RÔD4‚àĞ¢6öæâÒ6VÆbåögG5÷F&ÆR‚Ğ¢6æ—F—¦VBÒ6VÆbå÷6æ—F—¦R‚#SR"Ğ¢76W'B"R"æ÷B–â6æ—F—¦V@Ğ¢6öæâæW†V7WFR€Ğ¢%4TÄT5B6÷VçB‚¢’e$ôÒBt„U$RBÔD4‚ò"Â‡6æ—F—¦VBÂĞ¢’æfWF6†öæR‚Ğ Ğ¢FVbFW7E÷W&6VçE÷&W6W'fVEöf÷%ö6¦µ÷VW'’‡6VÆb“ Ğ¢2F†R4¤²Ä”´RfÆÆ&6²'V–ÆG2—G2÷vâGFW&âg&öÒF†R6æ—F—¦V@Ğ¢2FW‡C²¶VWR–çF7BF†W&R‡&RÖW†—7F–ær6öçG&7B’àĞ¢6æ—F—¦VBÒ6VÆbå÷6æ—F—¦R‚.ZèÎh‰SR"Ğ¢76W'B"R"–â6æ—F—¦V@Ğ 