from __future__ import annotations

import sqlite3

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION


def test_existing_v27_store_adds_account_usage_table_without_touching_sessions(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("existing", "feishu", model="gpt-old")
    db.close()

    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE session_account_usage")
    conn.execute("UPDATE schema_version SET version = 27")
    conn.commit()
    conn.close()

    reopened = SessionDB(db_path=path)
    try:
        assert reopened._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == SCHEMA_VERSION == 28
        assert reopened._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='session_account_usage'"
        ).fetchone()[0] == 1
        assert reopened.get_session("existing")["model"] == "gpt-old"
    finally:
        reopened.close()

    # Declarative table creation and the version bump must be idempotent.
    reopened_again = SessionDB(db_path=path)
    try:
        conn_again = reopened_again._conn
        assert conn_again is not None
        assert conn_again.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == SCHEMA_VERSION == 28
        assert conn_again.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='session_account_usage'"
        ).fetchone()[0] == 1
        existing = reopened_again.get_session("existing")
        assert existing is not None
        assert existing["model"] == "gpt-old"
    finally:
        reopened_again.close()


def test_account_usage_totals_group_duplicate_credentials_by_stable_account_key(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "feishu", model="gpt-test")
        db.update_token_counts(
            "s1",
            model="gpt-test",
            billing_provider="openai-codex",
            account_key="openai-codex:acct-a",
            input_tokens=10,
            cache_read_tokens=20,
            output_tokens=5,
            reasoning_tokens=2,
            api_call_count=1,
        )
        db.update_token_counts(
            "s1",
            model="gpt-test-2",
            billing_provider="openai-codex",
            account_key="openai-codex:acct-a",
            input_tokens=7,
            cache_write_tokens=3,
            output_tokens=4,
            reasoning_tokens=1,
            api_call_count=1,
        )

        rows = db.account_usage_totals(provider="openai-codex")

        assert len(rows) == 1
        assert rows[0]["account_key"] == "openai-codex:acct-a"
        assert rows[0]["api_call_count"] == 2
        assert rows[0]["input_tokens"] == 17
        assert rows[0]["cache_read_tokens"] == 20
        assert rows[0]["cache_write_tokens"] == 3
        assert rows[0]["output_tokens"] == 9
        assert rows[0]["reasoning_tokens"] == 3
        assert rows[0]["total_tokens"] == 49
    finally:
        db.close()


def test_account_usage_is_forward_only_when_no_account_key_is_supplied(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "feishu", model="gpt-test")
        db.update_token_counts(
            "s1",
            model="gpt-test",
            billing_provider="openai-codex",
            input_tokens=10,
            output_tokens=5,
            api_call_count=1,
        )

        assert db.account_usage_totals(provider="openai-codex") == []
    finally:
        db.close()


def test_deleting_session_removes_its_account_usage(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "feishu", model="gpt-test")
        db.update_token_counts(
            "s1",
            model="gpt-test",
            billing_provider="openai-codex",
            account_key="openai-codex:acct-a",
            input_tokens=10,
            output_tokens=5,
            api_call_count=1,
        )
        assert db.account_usage_totals(provider="openai-codex")

        assert db.delete_session("s1")

        assert db.account_usage_totals(provider="openai-codex") == []
    finally:
        db.close()


def test_async_account_usage_queue_never_coalesces_different_accounts(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "feishu", model="gpt-test")
        common = {
            "model": "gpt-test",
            "billing_provider": "openai-codex",
            "input_tokens": 10,
            "output_tokens": 5,
            "api_call_count": 1,
        }
        db.queue_token_counts(
            "s1", account_key="openai-codex:acct-a", **common
        )
        db.queue_token_counts(
            "s1", account_key="openai-codex:acct-b", **common
        )
        assert db.flush_token_counts(timeout=5.0)

        rows = db.account_usage_totals(provider="openai-codex")

        assert {row["account_key"] for row in rows} == {
            "openai-codex:acct-a",
            "openai-codex:acct-b",
        }
        assert sum(row["api_call_count"] for row in rows) == 2
    finally:
        db.close()
