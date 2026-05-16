import sqlite3

from hermes_state import SessionDB


def test_pa_behavior_history_schema_and_indexes_created(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        conn = sqlite3.connect(tmp_path / "state.db")
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'pa_behavior_events'"
            ).fetchone()
            assert table is not None

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'pa_behavior_events'"
                ).fetchall()
            }
            assert "idx_pa_behavior_events_session" in indexes
            assert "idx_pa_behavior_events_job_type" in indexes
            assert "idx_pa_behavior_events_timestamp" in indexes
        finally:
            conn.close()
    finally:
        db.close()


def test_recording_two_pa_behavior_events_returns_timestamp_id_order(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first_id = db.record_pa_behavior_event(
            constitution_id="constitution-a",
            constitution_hash="hash-a",
            job_type="inbox",
            job_hash="job-hash-a",
            session_id="session-a",
            session_source="gmail",
            source_metadata={"thread_id": "thread-1"},
            actor="admin@example.com",
            source="pa-admin",
            timestamp=100.0,
            metadata={"reason": "initial"},
        )
        second_id = db.record_pa_behavior_event(
            constitution_id="constitution-b",
            constitution_hash="hash-b",
            job_type="calendar",
            job_hash="job-hash-b",
            session_id="session-b",
            session_source="calendar",
            source_metadata={"event_id": "event-1"},
            actor="admin@example.com",
            source="pa-admin",
            timestamp=100.0,
            metadata={"reason": "changed"},
        )

        events = db.list_pa_behavior_events()

        assert [event["id"] for event in events] == [first_id, second_id]
        assert events[0]["constitution_id"] == "constitution-a"
        assert events[0]["constitution_hash"] == "hash-a"
        assert events[0]["job_type"] == "inbox"
        assert events[0]["job_hash"] == "job-hash-a"
        assert events[0]["session_id"] == "session-a"
        assert events[0]["session_source"] == "gmail"
        assert events[0]["source_metadata"] == {"thread_id": "thread-1"}
        assert events[0]["actor"] == "admin@example.com"
        assert events[0]["source"] == "pa-admin"
        assert events[0]["timestamp"] == 100.0
        assert events[0]["metadata"] == {"reason": "initial"}
    finally:
        db.close()


def test_list_pa_behavior_events_filters_by_session_id_and_job_type(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_behavior_event(
            constitution_id="constitution-a",
            job_type="inbox",
            session_id="session-a",
            timestamp=1.0,
        )
        db.record_pa_behavior_event(
            constitution_id="constitution-b",
            job_type="calendar",
            session_id="session-a",
            timestamp=2.0,
        )
        db.record_pa_behavior_event(
            constitution_id="constitution-c",
            job_type="inbox",
            session_id="session-b",
            timestamp=3.0,
        )

        by_session = db.list_pa_behavior_events(session_id="session-a")
        by_job_type = db.list_pa_behavior_events(job_type="inbox")
        by_both = db.list_pa_behavior_events(
            session_id="session-a",
            job_type="inbox",
        )

        assert [event["constitution_id"] for event in by_session] == [
            "constitution-a",
            "constitution-b",
        ]
        assert [event["constitution_id"] for event in by_job_type] == [
            "constitution-a",
            "constitution-c",
        ]
        assert [event["constitution_id"] for event in by_both] == [
            "constitution-a",
        ]
    finally:
        db.close()
