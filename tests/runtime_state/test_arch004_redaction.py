"""ARCH-004 retry taxonomy, epoch, digest, and redaction contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
import sqlite3

import pytest

from runtime_state import (
    RETRY_EXHAUSTED,
    RUNTIME_STATE_WRITER_EPOCH,
    WRITE_ABORT,
    RuntimeStateDB,
    RetryConfig,
    cas_claim_owner,
    create_session_state,
    is_transient_sqlite_error,
)
from runtime_state.journal import JournalWriteError, run_journaled_mutation


def _fast_retry_config() -> RetryConfig:
    return RetryConfig(
        busy_timeout_ms=1,
        max_retries=2,
        base_delay_ms=0,
        delay_cap_ms=0,
        jitter_min_ms=0,
        jitter_max_ms=0,
    )


def test_retry_classifier_is_closed_and_message_neutral():
    assert is_transient_sqlite_error(sqlite3.OperationalError("database is locked"))
    assert is_transient_sqlite_error(sqlite3.OperationalError("database table is locked"))
    assert not is_transient_sqlite_error(
        sqlite3.OperationalError("database is locked; SECRET-SENTINEL")
    )
    assert not is_transient_sqlite_error(sqlite3.OperationalError("no such table"))
    assert not is_transient_sqlite_error(sqlite3.IntegrityError("constraint"))


def test_transient_mutation_retries_three_total_attempts(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path, retry_config=_fast_retry_config()) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        calls = 0

        def mutate():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise sqlite3.OperationalError("database is locked")
            db.connection.execute(
                "UPDATE session_state SET status='degraded' WHERE profile_name=? AND session_id=?",
                ("p", "s"),
            )
            return SimpleNamespace(success=True)

        result = run_journaled_mutation(
            db.connection, "session_state", "p", "s", "update_columns", mutate, key_required=False
        )
        assert result.success
        assert calls == 3
        assert db.connection.execute(
            "SELECT status FROM session_state WHERE profile_name='p' AND session_id='s'"
        ).fetchone()[0] == "degraded"


def test_transient_mutation_exhaustion_rolls_back_and_redacts(tmp_path):
    path = tmp_path / "state.db"
    sentinel = "SECRET-BUSINESS-KEY-SENTINEL"
    with RuntimeStateDB(path, retry_config=_fast_retry_config()) as db:
        assert create_session_state(db.connection, "p", sentinel, user_id="u", workspace="w").success
        calls = 0

        def mutate():
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("database table is locked")

        with pytest.raises(JournalWriteError) as caught:
            run_journaled_mutation(
                db.connection,
                "session_state",
                "p",
                sentinel,
                "untrusted-" + sentinel,
                mutate,
            )
        error = caught.value
        assert error.code == RETRY_EXHAUSTED
        assert calls == 3
        assert sentinel not in str(error)
        assert sentinel not in json.dumps(error.metadata, sort_keys=True)
        assert db.connection.execute(
            "SELECT status FROM session_state WHERE profile_name='p' AND session_id=?",
            (sentinel,),
        ).fetchone()[0] == "active"


def test_non_transient_mutation_fails_once_without_raw_error(tmp_path):
    path = tmp_path / "state.db"
    sentinel = "SECRET-ERROR-SENTINEL"
    with RuntimeStateDB(path, retry_config=_fast_retry_config()) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        calls = 0

        def mutate():
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("no such table: " + sentinel)

        with pytest.raises(JournalWriteError) as caught:
            run_journaled_mutation(
                db.connection, "session_state", "p", "s", "update_columns", mutate
            )
        assert caught.value.code == WRITE_ABORT
        assert calls == 1
        assert sentinel not in str(caught.value)
        assert sentinel not in json.dumps(caught.value.metadata, sort_keys=True)


def test_digest_contract_is_independently_reproducible_and_redacted(tmp_path):
    path = tmp_path / "state.db"
    sentinel = "SECRET-BUSINESS-KEY-SENTINEL"
    with RuntimeStateDB(path) as db:
        assert create_session_state(db.connection, "profile", sentinel, user_id="u", workspace="w").success
        key = db.key_custody.load() if db.key_custody is not None else None
        # RuntimeStateDB intentionally keeps custody private; load the same
        # temporary auth fixture through the public custody class instead.
        if key is None:
            from runtime_state.key_custody import AuthJsonKeyCustody

            key = AuthJsonKeyCustody(path.with_name("auth.json")).load()
        expected = hmac.new(
            key,
            "\0".join(("profile", "session_state", sentinel)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        row = db.connection.execute(
            "SELECT entity_digest, digest_parameter_id, operation_category "
            "FROM runtime_state_journal WHERE entity_category='session_state'"
        ).fetchone()
        assert row == (expected, "hmac-sha256:v1:128", "create_session")
        journal_text = json.dumps(
            db.connection.execute("SELECT * FROM runtime_state_journal").fetchall()
        )
        assert sentinel not in journal_text


def test_arch004_default_epoch_is_newer_and_old_writer_is_blocked(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        assert db.writer_epoch == RUNTIME_STATE_WRITER_EPOCH
    with RuntimeStateDB(path, writer_epoch=0) as old_writer:
        result = old_writer.connection.execute(
            "SELECT downgrade_unsafe FROM runtime_state_journal_meta WHERE id=1"
        ).fetchone()
        assert result[0] == 1
        blocked = cas_claim_owner(old_writer.connection, "session_state", "p", "s", "owner", 0)
        assert not blocked.success and blocked.error == WRITE_ABORT
