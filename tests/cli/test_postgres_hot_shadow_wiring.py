from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from cli import HermesCLI


def _shell() -> HermesCLI:
    shell = HermesCLI.__new__(HermesCLI)
    shell._session_db = object()
    shell.conversation_history = [{"role": "user", "content": "sqlite authority"}]
    return shell


def test_postgres_hot_shadow_boundary_preserves_sqlite_authority(monkeypatch) -> None:
    calls: list[tuple[object, str]] = []

    def observe_sqlite_session(db: object, session_id: str) -> None:
        calls.append((db, session_id))

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.postgres_hot_shadow_runtime",
        types.SimpleNamespace(observe_sqlite_session=observe_sqlite_session),
    )
    shell = _shell()
    before = list(shell.conversation_history)

    shell._observe_postgres_hot_shadow("session-1")

    assert calls == [(shell._session_db, "session-1")]
    assert shell.conversation_history == before


def test_postgres_hot_shadow_boundary_is_fail_open_and_redacted(monkeypatch, caplog) -> None:
    secret = "postgresql://user:do-not-log@example.invalid/db"

    def observe_sqlite_session(_db: object, _session_id: str) -> None:
        raise RuntimeError(secret)

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.postgres_hot_shadow_runtime",
        types.SimpleNamespace(observe_sqlite_session=observe_sqlite_session),
    )
    shell = _shell()
    before = list(shell.conversation_history)

    shell._observe_postgres_hot_shadow("session-1")

    assert shell.conversation_history == before
    assert "PostgreSQL hot shadow observation failed open" in caplog.text
    assert secret not in caplog.text


def test_preload_observes_only_after_sqlite_history_is_authoritative() -> None:
    db = MagicMock()
    db.get_session.return_value = {"id": "session-1", "title": "demo"}
    db.resolve_resume_session_id.return_value = "session-1"
    db.get_messages_as_conversation.return_value = [
        {"role": "user", "content": "authoritative"}
    ]
    shell = HermesCLI.__new__(HermesCLI)
    shell._resumed = True
    shell._session_db = db
    shell.session_id = "session-1"
    shell.conversation_history = []
    shell._console_print = lambda *_args, **_kwargs: None
    shell._restore_session_cwd = lambda *_args, **_kwargs: None
    observed: list[list[dict[str, str]]] = []
    shell._observe_postgres_hot_shadow = lambda _session_id: observed.append(
        list(shell.conversation_history)
    )

    assert shell._preload_resumed_session() is True

    assert observed == [[{"role": "user", "content": "authoritative"}]]
