"""A delegate subagent transcript can never own a gateway routing key (#92859).

Reported failure: a ``delegate_task`` batch dispatched from a Discord DM ended
with the DM's routing key bound to one of the *spawned children*. That bind
went through ``SessionStore.switch_session()``, which promotes the outgoing
session to the un-resurrectable ``session_switch`` boundary — so the human
parent was killed mid-batch, every in-flight delegation from that parent was
then classified ``terminal`` and dropped, and the user's next DM was routed
into a leaf subagent with no knowledge of the conversation.

Two invariants are pinned here, both against real ``SessionDB`` rows:

1. ``switch_session()`` — the single sink every route rebind funnels through
   (async-completion pinning, /resume, CLI handoff, Telegram topic rebinding)
   — refuses a delegate/subagent target and leaves the incumbent route and
   its session row untouched.
2. ``find_latest_gateway_session_for_peer()`` — the restart-recovery path —
   never returns a subagent row, so a route that was hijacked before this fix
   (or by any other means) is not re-adopted after a gateway restart.
"""

import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore, is_internal_subagent_row


@pytest.fixture
def store_and_db(tmp_path):
    from unittest.mock import patch

    from hermes_state import SessionDB

    config = GatewayConfig()
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    try:
        yield store, db
    finally:
        db.close()


def _dm_source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1540910309580214372",
        chat_type="dm",
        user_id="user-1",
        user_name="karan",
    )


class TestSwitchSessionRefusesSubagentTargets:
    def test_delegate_child_cannot_take_over_its_parents_route(self, store_and_db):
        """The exact reported sequence: parent DM, live child, rebind attempt."""
        store, db = store_and_db
        parent_entry = store.get_or_create_session(_dm_source())
        parent_id = parent_entry.session_id

        # A subagent child exactly as delegate_tool creates it: platform
        # source 'subagent', durable _delegate_from marker, no routing key.
        db.create_session(
            "child_leaf",
            source="subagent",
            parent_session_id=parent_id,
            model_config={"_delegate_from": parent_id},
        )

        switched = store.switch_session(parent_entry.session_key, "child_leaf")

        assert switched is None, "a subagent row must never become a route owner"
        # The route still points at the human conversation...
        assert (
            store.get_or_create_session(_dm_source()).session_id == parent_id
        )
        # ...and, decisively, the parent was NOT ended at the hard
        # session_switch boundary, which is what made the batch's own
        # completion undeliverable in the incident.
        parent_row = db.get_session(parent_id)
        assert parent_row["ended_at"] is None
        assert parent_row["end_reason"] is None
        # The child never acquired the routing key.
        assert not db.get_session("child_leaf")["session_key"]

    def test_refusal_holds_for_nested_and_already_hijacked_rows(self, store_and_db):
        """Provenance, not the parent edge, decides — two harder shapes.

        ``parent_session_id == current route`` is not the invariant: a
        grandchild's parent edge points at another child, and a row hijacked
        before this fix has had ``source`` overwritten with the platform name
        by ``record_gateway_session_peer``. Both must still be refused.
        """
        store, db = store_and_db
        parent_entry = store.get_or_create_session(_dm_source())
        parent_id = parent_entry.session_id

        db.create_session(
            "child_leaf", source="subagent", parent_session_id=parent_id,
            model_config={"_delegate_from": parent_id},
        )
        # Grandchild: parent edge is the CHILD, not the routed session.
        db.create_session(
            "grandchild", source="subagent", parent_session_id="child_leaf",
            model_config={"_delegate_from": "child_leaf"},
        )
        # A row that was already hijacked once: source is now the platform,
        # only the _delegate_from marker still betrays what it is (this is
        # the literal shape of session 20260823_041917_519cdb on the
        # reporter's machine).
        db.create_session(
            "rehijack", source="discord", parent_session_id=parent_id,
            model_config={"_delegate_from": parent_id},
        )

        for target in ("grandchild", "rehijack"):
            assert store.switch_session(parent_entry.session_key, target) is None
            assert db.get_session(parent_id)["ended_at"] is None

    def test_real_gateway_session_still_switches(self, store_and_db):
        """The guard must not break /resume — the feature switch_session exists for."""
        store, db = store_and_db
        parent_entry = store.get_or_create_session(_dm_source())
        parent_id = parent_entry.session_id

        db.create_session("older_convo", source="discord", user_id="user-1")
        db.end_session("older_convo", end_reason="user_exit")

        switched = store.switch_session(parent_entry.session_key, "older_convo")

        assert switched is not None
        assert switched.session_id == "older_convo"
        assert db.get_session("older_convo")["ended_at"] is None
        assert db.get_session(parent_id)["end_reason"] == "session_switch"


class TestPeerRecoveryExcludesSubagentRows:
    def test_hijacked_child_is_not_re_adopted_after_restart(self, store_and_db):
        """Restart recovery must prefer the real conversation, not the child.

        The incident left a subagent row carrying the DM's routing key and
        still open, ranked *above* the real parent by recency. Recovery would
        hand the chat straight back to the subagent on the next restart.
        """
        store, db = store_and_db
        key = "agent:main:discord:dm:1540910309580214372"

        db.create_session(
            "real_convo", source="discord", session_key=key,
            user_id="user-1", chat_id="1540910309580214372", chat_type="dm",
        )
        db.append_message(session_id="real_convo", role="user", content="hi")
        # Hijacked child: same key, newer, holds messages too.
        db.create_session(
            "hijacked_child", source="discord", session_key=key,
            user_id="user-1", chat_id="1540910309580214372", chat_type="dm",
            parent_session_id="real_convo",
            model_config={"_delegate_from": "real_convo"},
        )
        db.append_message(session_id="hijacked_child", role="user", content="task")

        recovered = db.find_latest_gateway_session_for_peer(
            source="discord", session_key=key, user_id="user-1",
            chat_id="1540910309580214372", chat_type="dm",
        )

        assert recovered is not None
        assert recovered["id"] == "real_convo"

    def test_peer_tuple_fallback_also_excludes_subagent_rows(self, store_and_db):
        """The keyless fallback query needs the same fence, not just the keyed one."""
        store, db = store_and_db
        db.create_session(
            "subagent_only", source="discord", user_id="user-1",
            chat_id="1540910309580214372", chat_type="dm",
            model_config={"_delegate_from": "some_parent"},
        )
        db.append_message(session_id="subagent_only", role="user", content="task")

        recovered = db.find_latest_gateway_session_for_peer(
            source="discord", session_key="agent:main:discord:dm:no-such-key",
            user_id="user-1", chat_id="1540910309580214372", chat_type="dm",
        )

        assert recovered is None


class TestIsInternalSubagentRow:
    @pytest.mark.parametrize(
        "row",
        [
            {"source": "subagent"},
            {"source": "discord", "model_config": {"_delegate_from": "p"}},
            {"source": "discord", "model_config": json.dumps({"_delegate_from": "p"})},
        ],
    )
    def test_detects_subagent_rows(self, row):
        assert is_internal_subagent_row(row) is True

    @pytest.mark.parametrize(
        "row",
        [
            None,
            {},
            {"source": "discord"},
            {"source": "discord", "model_config": "not json"},
            {"source": "discord", "model_config": json.dumps(["not", "a", "dict"])},
            {"source": "discord", "model_config": {"_delegate_from": ""}},
            # A /branch child is a real, user-addressable conversation.
            {"source": "discord", "model_config": {"_branched_from": "p"}},
        ],
    )
    def test_leaves_real_sessions_alone(self, row):
        assert is_internal_subagent_row(row) is False
