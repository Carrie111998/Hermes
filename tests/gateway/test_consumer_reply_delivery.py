"""Management-selector reply delivery (teren 2026-07-21 ruling) tests.

The contract under test:
- delivery keys on the inbound chat's SELECTOR class: management only —
  a site/ingest-selector response must NEVER deliver (negative test);
- at-most-once per turn response via a durable pre-send claim;
- a bridge refusal / transport failure marks undelivered and never raises
  into the ingest path, and never retries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.durable_jsonl_consumer import (
    DurableInbox,
    InboxRecord,
    _management_selector_chats,
    _parse_captured_send,
    deliver_management_replies,
)

MGMT_CHAT = "120363426509183563@g.us"
SITE_CHAT = "120363421424519051@g.us"


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(
        "selectors:\n"
        "- job_type: tgg_ops_ingest\n"
        "  match:\n"
        "    source.platform: whatsapp\n"
        f"    source.chat_id: {SITE_CHAT}\n"
        "- job_type: tgg_management\n"
        "  match:\n"
        "    source.platform: whatsapp\n"
        f"    source.chat_id: {MGMT_CHAT}\n"
        "- job_type: tgg_management\n"
        "  match:\n"
        "    source.platform: telegram\n"
        "    source.chat_id: '-5295904349'\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        f"pa:\n  enabled: true\n  constitution_path: {constitution}\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture()
def inbox(tmp_path: Path) -> DurableInbox:
    return DurableInbox(tmp_path / "inbox.db")


def _captured(chat_id: str, content: str = "reply text", reply_to: str | None = "MSG1") -> dict:
    return {
        "message_id": "replay-1",
        "kind": "send",
        "args": [chat_id, content],
        "kwargs": {"reply_to": reply_to} if reply_to else {},
        "delivery_mode": "capture",
    }


def _record(chat_id: str, message_id: str = "MSG1") -> InboxRecord:
    return InboxRecord(
        seq=1,
        message_id=message_id,
        chat_id=chat_id,
        start_offset=0,
        end_offset=1,
        raw={"messageId": message_id, "chatId": chat_id},
    )


def test_selector_chats_are_whatsapp_management_only(config_path: Path) -> None:
    chats = _management_selector_chats(config_path)
    assert chats == frozenset({MGMT_CHAT})


def test_parse_extracts_send_and_rejects_other_kinds() -> None:
    parsed = _parse_captured_send(_captured(MGMT_CHAT))
    assert parsed == {"chat_id": MGMT_CHAT, "content": "reply text", "reply_to": "MSG1"}
    assert _parse_captured_send({**_captured(MGMT_CHAT), "kind": "send_image"}) is None
    assert _parse_captured_send({"kind": "send", "args": [], "kwargs": {}}) is None


def test_site_selector_response_never_delivers(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("no send expected")),
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(SITE_CHAT)],
        batch_records=[_record(SITE_CHAT)],
    )
    assert summary == {"delivered": 0, "undelivered": 0, "suppressed": 1, "duplicate": 0}
    assert not calls


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_mgmt_delivery_is_at_most_once(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG9"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    kwargs = dict(
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
    )
    first = deliver_management_replies(inbox, **kwargs)
    second = deliver_management_replies(inbox, **kwargs)
    assert first["delivered"] == 1 and second["delivered"] == 0
    assert second["duplicate"] == 1
    assert len(sent) == 1
    assert sent[0] == {
        "chatId": MGMT_CHAT,
        "message": "reply text",
        "replyTo": {"messageId": "MSG1"},
    }


def test_distinct_responses_to_same_anchor_each_deliver(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": f"WAMSG{len(sent)}"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(MGMT_CHAT, content="first answer"),
            _captured(MGMT_CHAT, content="second answer"),
        ],
        batch_records=[_record(MGMT_CHAT)],
    )
    assert summary["delivered"] == 2 and summary["duplicate"] == 0
    assert len(sent) == 2


def test_indeterminate_202_outcome_marks_undelivered(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=0: _FakeResponse(
            {"outcome": "unknown", "retrySafe": False}, status=202
        ),
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
    )
    assert summary == {"delivered": 0, "undelivered": 1, "suppressed": 0, "duplicate": 0}
    # claim consumed: no retry ever re-sends the indeterminate message
    retry = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
    )
    assert retry["duplicate"] == 1 and retry["delivered"] == 0


def test_bridge_refusal_marks_undelivered_and_never_raises(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError

    def refusing_urlopen(request, timeout=0):
        raise HTTPError(
            "http://bridge/send", 403, "refused", {}, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", refusing_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
    )
    assert summary["undelivered"] == 1
    # the claim consumed the key: a retry never re-sends
    retry = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
    )
    assert retry == {"delivered": 0, "undelivered": 0, "suppressed": 0, "duplicate": 1}
