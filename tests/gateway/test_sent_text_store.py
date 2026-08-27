"""Outbound sent-text store unit tests + Photon reply-context hydration tests.

Covers the generic outbound sent-text index (`gateway/sent_text_store.py`)
and the Photon adapter wiring that uses it to hydrate `reply_to_text` when
the sidecar reports a reply target ID but no quoted text (cron/background/
out-of-session replies — issue #1594 residual gap, issue #75131).
"""

from __future__ import annotations

import json

import pytest

import gateway.sent_text_store as sent_text_store
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.photon.adapter import PhotonAdapter


# ---------------------------------------------------------------------------
# sent_text_store: generic index behaviour
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def test_record_and_lookup_roundtrip(store):
    sent_text_store.record("+1555", "m-1", "Good morning brief")
    assert sent_text_store.lookup("+1555", "m-1") == "Good morning brief"


def test_lookup_missing_returns_none(store):
    assert sent_text_store.lookup("+1555", "nope") is None


def test_record_truncates_bounded_length(store):
    long = "x" * 5000
    sent_text_store.record("+1555", "m-2", long)
    got = sent_text_store.lookup("+1555", "m-2")
    assert got is not None and len(got) == sent_text_store._MAX_TEXT_CHARS


def test_capacity_trimmed_to_max_entries(store):
    for i in range(sent_text_store._MAX_ENTRIES + 50):
        sent_text_store.record("chat", f"m-{i}", f"text {i}")
    data = json.load(open(store / "state" / "sent_text_index.json"))
    assert len(data) == sent_text_store._MAX_ENTRIES
    # Oldest entries were evicted, newest retained.
    assert sent_text_store.lookup("chat", "m-0") is None
    last = f"m-{sent_text_store._MAX_ENTRIES + 49}"
    assert sent_text_store.lookup("chat", last) is not None


def test_per_chat_scoping(store):
    """Text recorded in one chat must not leak into another."""
    sent_text_store.record("chatA", "shared-id", "secret A")
    assert sent_text_store.lookup("chatB", "shared-id") is None


def test_noop_on_empty_inputs(store):
    sent_text_store.record("", "m", "text")
    sent_text_store.record(None, "m", "text")
    sent_text_store.record("c", "", "text")
    sent_text_store.record("c", None, "text")
    sent_text_store.record("c", "m", "")
    sent_text_store.record("c", "m", None)
    assert sent_text_store.lookup("", "m") is None
    assert sent_text_store.lookup("c", "") is None


# ---------------------------------------------------------------------------
# Photon adapter wiring: record on send, hydrate on inbound reply
# ---------------------------------------------------------------------------


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


def _capture(adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch) -> list:
    captured = []

    async def fake_handle(event) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


@pytest.fixture()
def photon_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class _FakeSidecar:
    def __init__(self) -> None:
        self.bodies = []

    async def __call__(self, path, body):  # mirrors _sidecar_call signature
        self.bodies.append((path, body))
        return {"messageId": f"spc-msg-out-{len(self.bodies)}"}


def _tapback_event(target_id: str, target_text=None) -> dict:
    """Reaction event on one of OUR messages, as parsed on current main."""
    return {
        "messageId": "react-evt-1",
        "platform": "iMessage",
        "space": {"id": "+155****4567", "type": "dm", "phone": "+155****4567"},
        "sender": {"id": "+155****4567"},
        "content": {
            "type": "reaction",
            "emoji": "👍",
            "targetMessageId": target_id,
            "targetDirection": "outbound",
            "targetText": target_text,
        },
        "timestamp": "2026-08-27T05:44:00.000Z",
    }


def _top_level_reply_event(target_id: str, target_text=None) -> dict:
    """Deployed-sidecar wire shape: reply correlation rides top-level fields
    next to a normal text payload."""
    return {
        "messageId": "inbound-evt-1",
        "platform": "iMessage",
        "space": {"id": "+155****4567", "type": "dm", "phone": "+155****4567"},
        "sender": {"id": "+155****4567"},
        "content": {"type": "text", "text": "30 wtf"},
        "replyToMessageId": target_id,
        "replyToText": target_text,
        "replyToIsOwnMessage": True,
        "timestamp": "2026-08-27T05:44:00.000Z",
    }


@pytest.mark.asyncio
async def test_photon_send_records_sent_text(photon_home, monkeypatch):
    adapter = _make_adapter(monkeypatch)
    fake = _FakeSidecar()
    monkeypatch.setattr(adapter, "_sidecar_call", fake)

    await adapter.send("+155****4567", "Good morning brief 👻")

    stored = sent_text_store.lookup("+155****4567", "spc-msg-out-1")
    assert stored is not None and "Good morning brief" in stored


@pytest.mark.asyncio
async def test_photon_send_failure_does_not_record(photon_home, monkeypatch):
    adapter = _make_adapter(monkeypatch)

    class _Boom:
        async def __call__(self, *a, **k):
            raise RuntimeError("sidecar down")

    monkeypatch.setattr(adapter, "_sidecar_call", _Boom())
    res = await adapter.send("+155****4567", "hi")
    assert res.success is False
    assert sent_text_store.lookup("+155****4567", "spc-msg-out-1") is None


@pytest.mark.asyncio
async def test_tapback_hydrates_text_from_outbound_index(
    photon_home,
    monkeypatch,
):
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    sent_text_store.record(
        "+155****4567",
        "spc-msg-cron-origin",
        "Scheduled good-morning message: how is your body and brain feeling?",
    )
    raw = _tapback_event("spc-msg-cron-origin", target_text="")
    await adapter._dispatch_inbound(raw)

    assert len(captured) == 1
    evt = captured[0]
    assert evt.text.startswith("reaction:added:")
    assert evt.reply_to_message_id == "spc-msg-cron-origin"
    assert evt.reply_to_is_own_message is True
    assert evt.reply_to_text and "good-morning" in evt.reply_to_text


@pytest.mark.asyncio
async def test_tapback_with_sidecar_text_not_overridden(
    photon_home,
    monkeypatch,
):
    """When the sidecar DID recover the text, never clobber it with ours."""
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)
    sent_text_store.record("+155****4567", "tgt-1", "index copy of the text")

    await adapter._dispatch_inbound(_tapback_event("tgt-1", "recovered"))
    assert captured[0].reply_to_text == "recovered"


@pytest.mark.asyncio
async def test_top_level_reply_hydrates_empty_quoted_text(
    photon_home,
    monkeypatch,
):
    """The production failure: '30 wtf' replying to a cron delivery whose
    quoted text never made it through. Deployed sidecars emit the reply
    correlation as top-level fields; the adapter must preserve them and
    hydrate the missing text from the outbound index."""
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    sent_text_store.record(
        "+155****4567",
        "spc-msg-6405bb16",
        "Good morning, Dan 👻 Amsterdam is overcast at about 18°C right now, "
        "heading towards 30°C today.",
    )
    await adapter._dispatch_inbound(_top_level_reply_event("spc-msg-6405bb16"))

    assert len(captured) == 1
    evt = captured[0]
    assert evt.text == "30 wtf"
    assert evt.message_type == MessageType.TEXT
    assert evt.reply_to_message_id == "spc-msg-6405bb16"
    assert evt.reply_to_is_own_message is True
    assert evt.reply_to_text and "Good morning" in evt.reply_to_text


@pytest.mark.asyncio
async def test_top_level_reply_unknown_target_degrades_silently(
    photon_home,
    monkeypatch,
):
    """No index entry → behave exactly like today (empty reply context);
    the gateway's current no-injection behaviour is preserved."""
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(_top_level_reply_event("spc-msg-unknown"))
    assert len(captured) == 1
    assert captured[0].reply_to_message_id == "spc-msg-unknown"
    assert not captured[0].reply_to_text


@pytest.mark.asyncio
async def test_standalone_cron_send_records_sent_text(
    photon_home,
    monkeypatch,
):
    """The standalone out-of-process send path (cron deliveries when the
    gateway isn't co-resident) must feed the same index — it is exactly the
    cron-delivery scenario from issue #75131."""
    import httpx

    monkeypatch.setenv("PHOTON_SIDECAR_TOKEN", "test-sidecar-token")
    posted = []

    class _Resp:
        status_code = 200

        def __init__(self, n):
            self._n = n

        def json(self):
            return {"ok": True, "messageId": f"spc-msg-cron-{self._n}"}

    class _Client:
        async def post(self, url, json=None, headers=None):
            posted.append(url)
            return _Resp(len(posted) - 1)

    class _Factory:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return _Client()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Factory)

    from plugins.platforms.photon import adapter as adapter_mod

    result = await adapter_mod._standalone_send(
        PlatformConfig(enabled=True, token="", extra={"sidecar_port": 41100}),
        "+155****4567",
        "Scheduled good-morning message from cron job 'morning-brief'.",
    )
    assert result.get("success") is True
    stored = sent_text_store.lookup("+155****4567", "spc-msg-cron-0")
    assert stored is not None and stored.startswith("Scheduled good-morning")


# ---------------------------------------------------------------------------
# Failure-safety: the store must never break sends or inbound dispatch
# ---------------------------------------------------------------------------


def test_corrupt_file_ignored(photon_home):
    p = photon_home / "state"
    p.mkdir(parents=True, exist_ok=True)
    (p / "sent_text_index.json").write_text("{not json at all")
    # record recovers by resetting, lookup returns None quietly
    sent_text_store.record("chatX", "m-9", "hello")
    assert sent_text_store.lookup("chatX", "m-9") == "hello"


@pytest.mark.asyncio
async def test_lookup_failure_never_breaks_inbound(photon_home, monkeypatch):
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)
    monkeypatch.setattr(
        sent_text_store,
        "lookup",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    await adapter._dispatch_inbound(_top_level_reply_event("some-target"))
    assert len(captured) == 1
    assert not captured[0].reply_to_text
