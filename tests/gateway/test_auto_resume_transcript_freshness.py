"""Auto-resume freshness must be measured on the DURABLE transcript clock.

Regression for the 2026-08-18 incident:

A Telegram thread's session was interrupted on 2026-07-07 mid-``pip install``
and left ``resume_pending`` for six weeks.  On the next gateway restart the
startup auto-resume pass fired a synthetic empty-text continuation turn for it,
and the agent silently resumed and completed that six-week-old task with no
user request.

Two independent defects made this possible, and this module locks both:

1. ``_schedule_resume_pending_sessions`` read freshness from
   ``last_resume_marked_at`` / ``updated_at``.  Both are routing bookkeeping
   that a restart resets to the boot time (``mark_resume_pending`` stamps
   ``_now()``; DB session recovery builds the entry with ``updated_at=now``).
   So *every* resume-pending session looked 0s idle right after a restart and
   the 1-hour freshness window could never reject anything.  The module comment
   in ``gateway/run.py`` always specified "the timestamp of the last transcript
   row" — the code did not read it.

2. The synthetic continuation event was ``internal=True``, and the
   ``pre_gateway_dispatch`` plugin hook was skipped for all internal events.
   Guard plugins (e.g. ``stale-session-guard``) were therefore blind to the
   single highest-risk dispatch path.  The event now carries
   ``auto_resume=True`` and the hook runs for it.
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import should_run_pre_gateway_dispatch_hook
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


SIX_WEEKS_SECS = 42 * 86400


def _pending_entry(source, *, session_id="sid", marked_at=None):
    """A resume-pending entry whose in-memory markers all say "just now".

    This is precisely the post-restart shape: the restart watchdog stamped
    ``last_resume_marked_at`` at boot, and DB recovery set ``updated_at=now``,
    regardless of how old the transcript actually is.
    """
    now = marked_at or datetime.now()
    return SessionEntry(
        session_key="agent:main:telegram:group:resume-chat:227",
        session_id=session_id,
        created_at=now - timedelta(days=42),
        updated_at=now,
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="group",
        resume_pending=True,
        resume_reason="restart_timeout",
        last_resume_marked_at=now,
    )


def _attach_db(runner, *, last_ts):
    """Give the runner a session DB whose transcript clock returns ``last_ts``."""
    db = MagicMock()
    db.get_last_message_timestamp = MagicMock(return_value=last_ts)
    runner.session_db = db
    return db


# ---------------------------------------------------------------------------
# Defect 1 — freshness must come from the transcript, not the restart marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_transcript_is_not_auto_resumed_despite_fresh_restart_marker():
    """The incident case: 6-week-old transcript, restart-fresh markers.

    Before the fix this scheduled 1 resume and silently continued the old task.
    """
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)
    runner.session_store._entries = {entry.session_key: entry}
    _attach_db(runner, last_ts=time.time() - SIX_WEEKS_SECS)
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 0, "a six-week-old transcript must not auto-resume"
    adapter.handle_message.assert_not_awaited()
    # The marker itself is preserved: a real human message can still continue
    # the session deliberately.  We only refuse to continue it unprompted.
    assert entry.resume_pending is True
    # And the runner slot must not be left claimed by a resume we never ran.
    assert entry.session_key not in runner._running_agents


@pytest.mark.asyncio
async def test_genuinely_fresh_transcript_still_auto_resumes():
    """The legitimate case must keep working — this is a gate, not a kill switch."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)
    runner.session_store._entries = {entry.session_key: entry}
    _attach_db(runner, last_ts=time.time() - 120)  # interrupted 2 minutes ago
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_transcript_clock_falls_back_to_in_memory_markers():
    """No durable signal (legacy transcript / no DB) => previous behaviour.

    The fallback must not silently disable auto-resume for setups where the
    transcript timestamp is simply unavailable.
    """
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)
    runner.session_store._entries = {entry.session_key: entry}
    _attach_db(runner, last_ts=None)
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()


def test_transcript_marker_rejects_non_numeric_values():
    """A duck-typed proxy must never be coerced into a bogus 1970 epoch.

    ``float(MagicMock())`` succeeds and yields 1.0, which would date every
    session to 1970 and disable auto-resume wholesale.  Only real numbers count.
    """
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)

    _attach_db(runner, last_ts=MagicMock())
    assert runner._resume_transcript_marker_ts(entry) is None

    _attach_db(runner, last_ts="not-a-timestamp")
    assert runner._resume_transcript_marker_ts(entry) is None

    _attach_db(runner, last_ts=True)  # bool is an int subclass — reject it
    assert runner._resume_transcript_marker_ts(entry) is None

    _attach_db(runner, last_ts=1787014699.93238)
    assert runner._resume_transcript_marker_ts(entry) == pytest.approx(1787014699.93238)


def test_transcript_marker_probe_fails_soft():
    """A DB error must not break startup recovery — it degrades to fallback."""
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)

    db = MagicMock()
    db.get_last_message_timestamp = MagicMock(side_effect=RuntimeError("db locked"))
    runner.session_db = db

    assert runner._resume_transcript_marker_ts(entry) is None


def test_transcript_marker_reads_the_production_db_handle():
    """Locks the handle the PRODUCTION path actually uses.

    ``GatewayRunner`` has no ``session_db`` attribute — the live DB handle is
    ``session_store._db`` (same handle ``gateway.session`` itself reads).  If
    the lookup only checked ``runner.session_db`` the probe would silently
    return None on every real gateway and the freshness gate would degrade back
    to the buggy in-memory markers while all tests stayed green.
    """
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)

    assert not hasattr(runner, "session_db"), (
        "if GatewayRunner ever grows a real session_db attribute, revisit the "
        "lookup order in _resume_transcript_marker_ts"
    )
    db = MagicMock()
    db.get_last_message_timestamp = MagicMock(return_value=1787014699.93238)
    runner.session_store._db = db

    assert runner._resume_transcript_marker_ts(entry) == pytest.approx(
        1787014699.93238
    )
    db.get_last_message_timestamp.assert_called_once_with(entry.session_id)


def test_transcript_marker_returns_none_without_any_db():
    """No DB handle at all (unit scaffolding, SQLite unavailable) => fallback."""
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)
    runner.session_store._db = None

    assert runner._resume_transcript_marker_ts(entry) is None


# ---------------------------------------------------------------------------
# Defect 2 — guard plugins must see the synthetic auto-resume event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_resume_event_is_flagged_for_plugin_hooks():
    """The synthetic continuation event must be distinguishable from other
    internal events so ``pre_gateway_dispatch`` can run on it."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="227")
    entry = _pending_entry(source)
    runner.session_store._entries = {entry.session_key: entry}
    _attach_db(runner, last_ts=time.time() - 120)
    adapter.handle_message = AsyncMock()

    runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    event = adapter.handle_message.await_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.internal is True, "still bypasses auth — it is synthetic"
    assert event.auto_resume is True, (
        "must be flagged so pre_gateway_dispatch guard plugins are not blind "
        "to the highest-risk dispatch path"
    )
    assert event.text == ""


def test_default_message_event_is_not_flagged_auto_resume():
    """Only the startup resume path sets the flag; ordinary events must not."""
    source = make_restart_source()
    assert MessageEvent(text="hi", source=source).auto_resume is False
    assert MessageEvent(text="", source=source, internal=True).auto_resume is False


def test_hook_eligibility_predicate():
    """The gate itself: user events always, internal only when auto_resume."""
    source = make_restart_source()

    user_event = MessageEvent(text="hi", source=source)
    assert should_run_pre_gateway_dispatch_hook(user_event, False) is True

    # Ordinary internal event (background-process completion) stays excluded —
    # this fix must not widen the hook to all machinery traffic.
    bg_event = MessageEvent(text="job done", source=source, internal=True)
    assert should_run_pre_gateway_dispatch_hook(bg_event, True) is False

    # The auto-resume continuation event is the single admitted exception.
    resume_event = MessageEvent(
        text="", source=source, internal=True, auto_resume=True
    )
    assert should_run_pre_gateway_dispatch_hook(resume_event, True) is True

    # Legacy event objects without the attribute must not raise.
    class Bare:
        pass

    assert should_run_pre_gateway_dispatch_hook(Bare(), True) is False
    assert should_run_pre_gateway_dispatch_hook(Bare(), False) is True
