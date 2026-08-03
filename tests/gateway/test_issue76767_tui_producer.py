"""TUI producer path for gateway-bound delivery obligations (#76767 / #76796).

When a gateway-bound Telegram (or other platform) session is resumed from the
desktop/TUI backend, the reply is generated in THIS process
(``tui_gateway/server.py``), not in the gateway process — so the gateway never
sees the turn, ``delivery_obligations`` stays empty and the reply is never sent
to the bound chat (#76767).  The producer hook
``tui_gateway/server.py::_record_bound_platform_delivery`` re-reads the stored
session row's gateway routing fields (``source``/``chat_id``/``thread_id``/
``session_key``) and records an EXTERNAL-owner obligation (NULL pid) that the
gateway's delivery sweep claims and sends.

These tests drive the real producer against a real ``SessionDB`` in a temp
``profile_home`` (the same path the producer opens): they prove the exact
routing fields land in the ledger, that the row is claimable by the gateway
sweep, and the no-op guards (blank content, missing / non-gateway session key,
unknown session row, missing chat id, disabled ledger).
"""

import pytest

from gateway import delivery_ledger as dl
from tui_gateway import server

GATEWAY_KEY = "agent:main:telegram:dm:123"
REPLY = "the final answer"


@pytest.fixture(autouse=True)
def _fresh_ledger(tmp_path, monkeypatch):
    """Isolated delivery_obligations db per test (same pattern as the other
    delivery-ledger test files)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _seed_gateway_session(
    tmp_path,
    *,
    session_key=GATEWAY_KEY,
    source="telegram",
    chat_id="123",
    thread_id=None,
    content=REPLY,
):
    """Seed a temp profile_home state.db with a gateway-bound session row
    (routing peer fields) plus one assistant reply row.

    Returns ``(profile_home, message_id)`` — the same shape the desktop
    session dict carries (``profile_home`` + ``session_key``).
    """
    from hermes_state import SessionDB

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session(session_key, source=source)
    db.record_gateway_session_peer(
        session_key,
        source=source,
        session_key=session_key,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    message_id = db.append_message(session_key, role="assistant", content=content)
    db.close()
    return profile_home, message_id


def _tui_session(profile_home, *, session_key=GATEWAY_KEY):
    """The session dict the TUI holds for a gateway-bound session."""
    return {"session_key": session_key, "profile_home": str(profile_home)}


def _all_obligation_ids():
    with dl._connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT obligation_id FROM delivery_obligations"
            ).fetchall()
        ]


def _ledger_row(obligation_id):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT session_key, platform, chat_id, thread_id, content,
                      state, owner_pid
               FROM delivery_obligations WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
    if r is None:
        return None
    return {
        "session_key": r[0],
        "platform": r[1],
        "chat_id": r[2],
        "thread_id": r[3],
        "content": r[4],
        "state": r[5],
        "owner_pid": r[6],
    }


class TestTuiProducerRecordsExternalObligation:
    """A gateway-origin TUI session records the expected external obligation
    with the correct routing fields (platform/chat_id/thread_id/content)."""

    @pytest.mark.parametrize("thread_id", [None, "topic-9"])
    def test_routing_fields_land_in_ledger(self, tmp_path, thread_id):
        profile_home, message_id = _seed_gateway_session(tmp_path, thread_id=thread_id)

        server._record_bound_platform_delivery(_tui_session(profile_home), REPLY)

        # Stable id: same turn (message ref) + same content re-records
        # idempotently; proves the producer used the persisted assistant row
        # as the per-turn message ref rather than the time fallback.
        obligation_id = dl.compute_obligation_id(GATEWAY_KEY, str(message_id), REPLY)
        row = _ledger_row(obligation_id)
        assert row is not None
        assert row["session_key"] == GATEWAY_KEY
        assert row["platform"] == "telegram"
        assert row["chat_id"] == "123"
        assert row["thread_id"] == thread_id
        assert row["content"] == REPLY
        assert row["state"] == "pending"
        # External owner: NULL pid, so the gateway's sweep treats it as dead
        # and claims it while the desktop recorder stays alive (#76767).
        assert row["owner_pid"] is None

    def test_recorded_obligation_claimable_by_gateway_sweep(self, tmp_path):
        profile_home, message_id = _seed_gateway_session(tmp_path)
        server._record_bound_platform_delivery(_tui_session(profile_home), REPLY)

        claimed = dl.sweep_recoverable(deliverable_platforms={"telegram"})

        assert [c["obligation_id"] for c in claimed] == [
            dl.compute_obligation_id(GATEWAY_KEY, str(message_id), REPLY)
        ]
        assert claimed[0]["content"] == REPLY
        assert claimed[0]["needs_marker"] is False  # pending: send never started


class TestTuiProducerNoOps:
    """Guards: nothing is recorded for non-deliverable turns or sessions."""

    def test_blank_content_is_noop(self, tmp_path):
        profile_home, _ = _seed_gateway_session(tmp_path)
        session = _tui_session(profile_home)
        server._record_bound_platform_delivery(session, "")
        server._record_bound_platform_delivery(session, "   \n  ")
        server._record_bound_platform_delivery(session, None)
        assert _all_obligation_ids() == []

    def test_missing_session_key_is_noop(self, tmp_path):
        profile_home, _ = _seed_gateway_session(tmp_path)
        server._record_bound_platform_delivery(
            {"profile_home": str(profile_home)}, REPLY
        )
        assert _all_obligation_ids() == []

    def test_non_gateway_session_key_is_noop(self, tmp_path):
        # A local (non ``agent:``) session key must never be recorded even
        # when the row carries routing-looking fields.
        profile_home, _ = _seed_gateway_session(
            tmp_path, session_key="local-tui-1", source="tui", chat_id="C1"
        )
        server._record_bound_platform_delivery(
            _tui_session(profile_home, session_key="local-tui-1"), REPLY
        )
        assert _all_obligation_ids() == []

    def test_unknown_session_row_is_noop(self, tmp_path):
        # session_key present, but no matching row in state.db: the producer
        # cannot learn the routing fields, so it must not record.
        profile_home, _ = _seed_gateway_session(tmp_path)
        session = _tui_session(profile_home, session_key="agent:main:telegram:dm:999")
        server._record_bound_platform_delivery(session, REPLY)
        assert _all_obligation_ids() == []

    def test_missing_chat_id_is_noop(self, tmp_path):
        profile_home, _ = _seed_gateway_session(
            tmp_path, session_key=GATEWAY_KEY, source="telegram", chat_id=""
        )
        server._record_bound_platform_delivery(_tui_session(profile_home), REPLY)
        assert _all_obligation_ids() == []

    def test_ledger_disabled_is_noop(self, tmp_path, monkeypatch):
        profile_home, _ = _seed_gateway_session(tmp_path)
        monkeypatch.setattr(dl, "ledger_enabled", lambda: False)
        server._record_bound_platform_delivery(_tui_session(profile_home), REPLY)
        assert _all_obligation_ids() == []
