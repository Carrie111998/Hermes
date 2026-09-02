"""State-rebuild payload parity — issue #100795.

A session that has been serving turns from its LIVE in-memory transcript can
lose that transcript at any teardown boundary (gateway LRU / memory-pressure
eviction, background-review lifecycle, process restart). The next user turn is
then rebuilt from ``state.db`` and must produce the SAME request bytes the
provider's prompt cache was built from — otherwise the rebuilt request diverges
at the first rewritten message and the exact-prefix cache misses for one shot.

``get_messages_as_conversation`` replays user/assistant rows through
``sanitize_context(content).strip()``. Nothing on the outgoing path strips
message content, so any content carrying surrounding whitespace used to replay
SHORTER than it was sent. ``_flush_messages_to_session_db`` is the chokepoint
that captures the exact sent bytes in the ``api_content`` sidecar; these tests
pin that it covers the whole reload-divergence set, and that clean content
still grows no redundant sidecar.
"""

from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    a.client = MagicMock()
    return a


@pytest.fixture()
def persisted_agent(agent, tmp_path):
    """``agent`` bound to a real on-disk SessionDB with an open session row."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-100795"
    db.create_session(session_id, "cli", model="test-model")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._persist_disabled = False
    try:
        yield agent, db, session_id
    finally:
        db.close()


def _rebuilt_wire_content(row):
    """The content the api_messages build would send for a restored row.

    Historical user/assistant rows get their ``api_content`` sidecar
    substituted into ``content`` (see ``substitute_api_content``); rows
    without a sidecar are sent as stored.
    """
    sidecar = row.get("api_content")
    return sidecar if isinstance(sidecar, str) and sidecar else row.get("content")


@pytest.mark.parametrize(
    "live_content",
    [
        "Summarize the log below:\n",
        "  and now the diff",
        "trailing spaces   ",
        "\n\nleading and trailing\n",
    ],
    ids=["trailing-newline", "leading-spaces", "trailing-spaces", "both"],
)
def test_rebuilt_user_turn_replays_the_bytes_that_were_sent(
    persisted_agent, live_content
):
    """Whitespace-carrying user content survives the persist/rebuild round-trip.

    Without the sidecar capture the rebuilt request sends ``content.strip()``
    while the cache was warmed with ``content`` — a mid-history prefix break
    on the first shot after any agent teardown.
    """
    agent, db, session_id = persisted_agent
    live = [{"role": "user", "content": live_content}]

    agent._flush_messages_to_session_db([dict(m) for m in live], [])

    rebuilt = db.get_messages_as_conversation(session_id)
    assert len(rebuilt) == 1
    assert _rebuilt_wire_content(rebuilt[0]) == live_content


def test_rebuilt_assistant_turn_replays_the_bytes_that_were_sent(persisted_agent):
    """Assistant rows take the same reload strip, so they need the same cover.

    ``build_assistant_message`` strips the mainline assistant turn, but the
    recovery/continuation paths append ``final_response`` / partial text
    verbatim, so unstripped assistant content does reach the transcript.
    """
    agent, db, session_id = persisted_agent
    live_content = "Here is the patch:\n\n"
    live = [
        {"role": "user", "content": "patch it"},
        {"role": "assistant", "content": live_content},
    ]

    agent._flush_messages_to_session_db([dict(m) for m in live], [])

    rebuilt = db.get_messages_as_conversation(session_id)
    assert _rebuilt_wire_content(rebuilt[1]) == live_content


def test_clean_content_grows_no_sidecar(persisted_agent):
    """Content the reload would not rewrite must not duplicate itself on disk."""
    agent, db, session_id = persisted_agent
    live = [
        {"role": "user", "content": "no surrounding whitespace"},
        {"role": "assistant", "content": "none here either"},
    ]

    agent._flush_messages_to_session_db([dict(m) for m in live], [])

    rebuilt = db.get_messages_as_conversation(session_id)
    assert [r.get("api_content") for r in rebuilt] == [None, None]
    assert [r["content"] for r in rebuilt] == [m["content"] for m in live]


def test_injection_sidecar_is_not_overwritten(persisted_agent):
    """A prologue-stamped sidecar keeps priority over the reload capture."""
    agent, db, session_id = persisted_agent
    injected = "ask\n\n<memory-context>recalled</memory-context>"
    live = [{"role": "user", "content": "ask\n", "api_content": injected}]

    agent._flush_messages_to_session_db([dict(m) for m in live], [])

    rebuilt = db.get_messages_as_conversation(session_id)
    assert rebuilt[0]["api_content"] == injected
