"""Regression tests for /undo's in-memory / on-disk persistence contract.

``undo_last()`` always truncates ``conversation_history`` in memory (by
design — see the original ``feat(undo)`` commit 3f7d1c801d, "keep the
in-memory undo but skip the soft-delete"). What must NOT happen is the
agent's flush cursor (``_last_flushed_db_idx``) advancing past on-disk rows
that are still ``active=1`` when the soft-delete failed — that lets a later
flush append new rows alongside the "undone" ones instead of replacing them,
so the undone content resurfaces on reload/resume/crash and stays a hit in
FTS search.

These tests pin the fix: the flush cursor only advances when the on-disk
transcript is actually known to agree with the in-memory truncation.
"""

from tests.cli.test_cli_init import _make_cli


class _FakeAgent:
    """Minimal agent double exposing only what undo_last() touches."""

    def __init__(self, last_flushed_db_idx=999):
        self._last_flushed_db_idx = last_flushed_db_idx
        self._memory_manager = None

    def _invalidate_system_prompt(self):
        pass


class _SucceedingRewindDB:
    """Fake SessionDB whose rewind_to_message succeeds."""

    def list_recent_user_messages(self, session_id, limit=10):
        return [{"id": 42, "content": "second message"}]

    def rewind_to_message(self, session_id, target_message_id):
        return {
            "rewound_count": 2,
            "target_message": {"content": "second message"},
            "new_head_id": 1,
        }


class _RaisingRewindDB:
    """Fake SessionDB whose rewind_to_message raises (any failure mode)."""

    def __init__(self, exc):
        self._exc = exc

    def list_recent_user_messages(self, session_id, limit=10):
        return [{"id": 42, "content": "second message"}]

    def rewind_to_message(self, session_id, target_message_id):
        raise self._exc


class _EmptyRecentsDB:
    """Fake SessionDB with no recent user messages to rewind against."""

    def list_recent_user_messages(self, session_id, limit=10):
        return []

    def rewind_to_message(self, session_id, target_message_id):
        raise AssertionError("must not be called when recents is empty")


def _cli_with_two_turn_history():
    cli = _make_cli()
    cli.session_id = "test-session"
    cli.conversation_history = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second message"},
        {"role": "assistant", "content": "second reply"},
    ]
    return cli


def test_undo_advances_flush_cursor_when_soft_delete_succeeds():
    """Control: a successful soft-delete still reconciles the cursor as before."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _SucceedingRewindDB()
    cli.agent = _FakeAgent(last_flushed_db_idx=999)

    cli.undo_last()

    assert cli.agent._last_flushed_db_idx == len(cli.conversation_history)


def test_undo_does_not_advance_flush_cursor_when_soft_delete_raises_value_error():
    """The bug: a ValueError from rewind_to_message must not move the cursor."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _RaisingRewindDB(ValueError("rewind target must be a 'user' message"))
    cli.agent = _FakeAgent(last_flushed_db_idx=999)

    cli.undo_last()

    # In-memory truncation still happens (unchanged, intentional behavior).
    assert cli.conversation_history == [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "first reply"},
    ]
    # But the flush cursor must NOT have been advanced to the new (shorter)
    # length, since the DB still holds the "undone" rows as active.
    assert cli.agent._last_flushed_db_idx == 999


def test_undo_does_not_advance_flush_cursor_when_soft_delete_raises_generic_exception():
    """Same contract for a non-ValueError failure (DB locked, I/O error, etc.)."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _RaisingRewindDB(RuntimeError("database is locked"))
    cli.agent = _FakeAgent(last_flushed_db_idx=999)

    cli.undo_last()

    assert cli.agent._last_flushed_db_idx == 999


def test_undo_does_not_advance_flush_cursor_when_no_recent_messages_on_disk():
    """No rows to rewind against on disk is also an unreconciled state, not a no-op success."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _EmptyRecentsDB()
    cli.agent = _FakeAgent(last_flushed_db_idx=999)

    cli.undo_last()

    assert cli.agent._last_flushed_db_idx == 999


def test_undo_advances_flush_cursor_when_there_is_no_session_db():
    """Control: no DB in play at all is harmless — nothing to diverge from."""
    cli = _cli_with_two_turn_history()
    cli._session_db = None
    cli.agent = _FakeAgent(last_flushed_db_idx=999)

    cli.undo_last()

    assert cli.agent._last_flushed_db_idx == len(cli.conversation_history)


def test_undo_warns_user_when_soft_delete_fails(capsys):
    """The user must see that persistence didn't happen, not just a silent DEBUG log."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _RaisingRewindDB(RuntimeError("database is locked"))
    cli.agent = _FakeAgent()

    cli.undo_last()

    out = capsys.readouterr().out
    assert "Could not confirm this on disk" in out


def test_undo_does_not_warn_user_when_soft_delete_succeeds(capsys):
    """Control: the warning is specific to the unreconciled case, not always printed."""
    cli = _cli_with_two_turn_history()
    cli._session_db = _SucceedingRewindDB()
    cli.agent = _FakeAgent()

    cli.undo_last()

    out = capsys.readouterr().out
    assert "Could not confirm this on disk" not in out
