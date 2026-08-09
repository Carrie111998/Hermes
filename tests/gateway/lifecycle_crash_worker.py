"""Real-process worker for lifecycle durability crash tests.

This module deliberately installs failpoints only on the objects it creates;
production code never reads an environment variable to change lifecycle flow.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore, _now
from hermes_state import SessionDB


EXIT_CODE = 86


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="lifecycle-crash-chat",
        user_id="lifecycle-crash-user",
        chat_type="dm",
        thread_id="lifecycle-crash-thread",
    )


def _die_at(stage: str):
    requested = sys.argv[2]
    if stage == requested:
        os._exit(EXIT_CODE)


def _store(reset_policy: SessionResetPolicy | None = None) -> SessionStore:
    sessions_dir = Path(os.environ["LIFECYCLE_SESSIONS_DIR"])
    db_path = Path(os.environ["LIFECYCLE_DB"])
    store = SessionStore(
        sessions_dir=sessions_dir,
        config=GatewayConfig(
            default_reset_policy=reset_policy or SessionResetPolicy(mode="none")
        ),
    )
    # Do not rely on DEFAULT_DB_PATH import timing in a subprocess fixture.
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=db_path)
    store._test_lifecycle_failpoint = _die_at
    store._db._test_lifecycle_failpoint = _die_at
    return store


def _message() -> list[dict]:
    return [{"role": "user", "content": "compressed handoff"}]


def run(case: str) -> None:
    if case == "db-compression":
        db = SessionDB(db_path=Path(os.environ["LIFECYCLE_DB"]))
        db._test_lifecycle_failpoint = _die_at
        db.create_session(session_id="parent", source="telegram", session_key="key")
        db.publish_compression_child(
            parent_session_id="parent", child_session_id="child", source="telegram",
            messages=_message(), require_compression_lease=False,
        )
        return

    reset_policy = (
        SessionResetPolicy(mode="idle", idle_minutes=1, notify=False)
        if case == "auto-reset"
        else None
    )
    store = _store(reset_policy)
    source = _source()
    original = store.get_or_create_session(source)
    if case == "reset":
        store.reset_session(original.session_key)
    elif case == "auto-reset":
        original.updated_at = _now() - timedelta(minutes=2)
        store._save_entry(original.session_key, require_primary_db=True)
        store.get_or_create_session(source)
    elif case == "switch":
        target = "resume-target"
        store._db.create_session(
            session_id="resume-parent", source="telegram", user_id=source.user_id,
            session_key=original.session_key, chat_id=source.chat_id,
            chat_type=source.chat_type, thread_id=source.thread_id,
        )
        store._record_gateway_session_peer("resume-parent", original.session_key, source)
        store._db.publish_compression_child(
            parent_session_id="resume-parent", child_session_id=target,
            source="telegram", messages=_message(), require_compression_lease=False,
        )
        store._db.end_session(target, "named")
        store.switch_session(original.session_key, target)
    elif case == "compression-advance":
        child = "compression-child"
        store._db.publish_compression_child(
            parent_session_id=original.session_id, child_session_id=child,
            source="telegram", messages=_message(), require_compression_lease=False,
        )
        store.advance_compression_session(original.session_key, original.session_id, child)
    elif case == "prune":
        original.updated_at = _now() - timedelta(days=500)
        store._save_entries(require_primary_db=True)
        store.prune_old_entries(90)
    else:
        raise ValueError(case)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: lifecycle_crash_worker CASE FAILPOINT")
    run(sys.argv[1])


if __name__ == "__main__":
    main()
