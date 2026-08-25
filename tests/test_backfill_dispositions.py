"""Tests for scripts/backfill_session_dispositions.py.

Covers the pure classify() rules, idempotency, and dry-run semantics.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill_session_dispositions import classify  # noqa: E402


class TestClassifyRules:
    def test_speed_test_is_junk(self):
        assert classify({"source": "speed-test-a", "title": "ping"}) == {
            "disposition": "junk",
            "project_group": None,
            "project": None,
        }

    def test_noise_sources_are_transient(self):
        for source in ["cron", "tool", "subagent", "kanban", "hermes_browser"]:
            assert classify({"source": source, "title": "anything"}) == {
                "disposition": "transient",
                "project_group": None,
                "project": None,
            }

    def test_probe_title_is_transient(self):
        for title in ["probe", "echo", "ping", "smoke test"]:
            assert classify({"source": "cli", "title": title})["disposition"] == "transient"

    def test_keyword_inference_sets_project_group_and_project(self):
        result = classify(
            {"source": "cli", "title": "Build the Fusion Router plugin"}
        )
        assert result == {
            "disposition": "project",
            "project_group": "Hermes",
            "project": "Fusion Router",
        }

    def test_plain_cli_session_is_project_unfiled(self):
        assert classify({"source": "desktop", "title": "random work"}) == {
            "disposition": "project",
            "project_group": None,
            "project": None,
        }

    def test_unknown_source_returns_none(self):
        assert classify({"source": "telegram", "title": "chat"}) is None


class TestBackfillIdempotency:
    def test_rerun_changes_nothing(self, tmp_path, monkeypatch):
        from hermes_state import SessionDB

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message(session_id="s1", role="user", content="Build the Fusion Router plugin")
            db.set_session_title("s1", "Build the Fusion Router plugin")
            db.create_session(session_id="s2", source="cron")
            db.append_message(session_id="s2", role="user", content="scheduled run")
            # Manual classification must survive a backfill run.
            db.create_session(session_id="s3", source="cli")
            db.append_message(session_id="s3", role="user", content="manual one")
            db.set_session_disposition("s3", "archive", "Manual", None)
        finally:
            db.close()

        from backfill_session_dispositions import main

        assert main(["--db", str(db_path)]) == 0
        db = SessionDB(db_path=db_path)
        try:
            rows = db.list_sessions_rich(limit=100, include_archived=True)
            by_id = {r["id"]: r for r in rows}
            assert by_id["s1"]["disposition"] == "project"
            assert by_id["s1"]["project"] == "Fusion Router"
            assert by_id["s2"]["disposition"] == "transient"
            # Manual archive disposition untouched.
            assert by_id["s3"]["disposition"] == "archive"
            assert by_id["s3"]["project_group"] == "Manual"
        finally:
            db.close()

        # Re-run: idempotent, nothing changes, exit 0.
        assert main(["--db", str(db_path)]) == 0
        db = SessionDB(db_path=db_path)
        try:
            rows = db.list_sessions_rich(limit=100, include_archived=True)
            by_id = {r["id"]: r for r in rows}
            assert by_id["s1"]["disposition"] == "project"
            assert by_id["s2"]["disposition"] == "transient"
            assert by_id["s3"]["disposition"] == "archive"
        finally:
            db.close()

    def test_dry_run_writes_nothing(self, tmp_path):
        from hermes_state import SessionDB

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message(session_id="s1", role="user", content="Build the Fusion Router plugin")
        finally:
            db.close()

        from backfill_session_dispositions import main

        assert main(["--db", str(db_path), "--dry-run"]) == 0
        db = SessionDB(db_path=db_path)
        try:
            rows = db.list_sessions_rich(limit=100, include_archived=True)
            assert rows[0]["disposition"] is None
        finally:
            db.close()
