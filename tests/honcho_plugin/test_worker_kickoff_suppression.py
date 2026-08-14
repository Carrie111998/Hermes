"""Integration test: headless kanban-worker kickoffs must NOT reach Honcho.

Root cause (R4, kanban t_ef17f26b): headless dispatcher-spawned kanban workers
emit a user-role message shaped like ``work kanban task t_<id>``. With
saveMessages=true + hybrid recall, that message was ingested into the shared
``hermes`` workspace and Honcho's server-side Deriver auto-extracted a durable
conclusion for a transient task ID / session-state fact. Purge alone was
whack-a-mole: the store re-polluted within minutes of each purge.

The fix extends ``_INTERNAL_GATEWAY_TURN_RE`` (plugins/memory/honcho/__init__.py)
with ``work kanban task t_[a-zA-Z0-9_-]+``. ``sync_turn`` returns early for any
match (line ~1424) BEFORE ``get_or_create`` / ``add_message`` / ``save`` run,
so the message is never persisted and the Deriver has no input to turn into a
conclusion.

These tests pin that contract:
  1. The worker kickoff is recognized as an internal gateway turn (suppressed).
  2. Genuine human text that merely *mentions* the pattern is NOT over-matched.
  3. sync_turn with a worker kickoff performs zero writes to the Honcho manager
     (get_or_create / add_message / save never called) -- the mechanism that
     stops the Deriver conclusion.
"""

from unittest.mock import MagicMock

import pytest

from plugins.memory.honcho import HonchoMemoryProvider, _is_internal_gateway_turn


# ---------------------------------------------------------------------------
# 1. Regex contract -- the worker kickoff IS an internal gateway turn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "work kanban task t_ef17f26b",
        "work kanban task t_0b8c2b53",
        "work kanban task t_abc-123_xyz",
        "  work kanban task t_x1",  # leading whitespace tolerated
        "Work Kanban Task T_1A2b3c",  # case-insensitive
    ],
)
def test_worker_kickoff_recognized_as_internal(text: str):
    assert _is_internal_gateway_turn(text) is True


# ---------------------------------------------------------------------------
# 2. No over-match -- human text that merely discusses the topic stays valid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "she discussed the SQL role fit",  # the 07-28 misattribution subject
        "Random thought: work kanban task t_x",  # not anchored at start
        "work kanban task",  # bare phrase, no task id
        "I am interested in SQL/PQL roles",  # genuine preference modeling
        "please review the kanban board for me",
        "",
    ],
)
def test_human_text_not_over_matched(text: str):
    assert _is_internal_gateway_turn(text) is False


# ---------------------------------------------------------------------------
# 3. sync_turn with a worker kickoff persists NOTHING (no Deriver input)
# ---------------------------------------------------------------------------

def _provider() -> HonchoMemoryProvider:
    """Provider wired to a fake manager; sync_turn must never touch it for a kickoff."""
    p = HonchoMemoryProvider()
    p._config = MagicMock()
    p._config.save_messages = True
    p._config.message_max_chars = 25000
    p._manager = MagicMock()
    p._session_key = "test-session"
    p._session_initialized = True
    p._recall_mode = "hybrid"
    return p


def test_sync_turn_worker_kickoff_writes_nothing():
    p = _provider()
    p.sync_turn("work kanban task t_ef17f26b", "I'm working on it")
    if p._sync_thread is not None:
        p._sync_thread.join(timeout=5)
    # Nothing persisted -> the server-side Deriver has no message to extract a
    # transient task-ID / session-state conclusion from.
    p._manager.get_or_create.assert_not_called()
    p._manager.save.assert_not_called()


def test_sync_turn_normal_message_still_persists():
    """Guard: suppression must not swallow genuine user turns."""
    p = _provider()
    p.sync_turn("I am interested in SQL/PQL roles", "noted")
    if p._sync_thread is not None:
        p._sync_thread.join(timeout=5)
    p._manager.get_or_create.assert_called_once()
    p._manager.save.assert_called_once()
