"""Ingress conformance generator tests (Phase 6 ingress mirror).

Three contract layers:
  1. Generator invariants — determinism, shape, unique ids (the egress
     suite's discipline).
  2. ORACLE FIDELITY — the pure parse cores must agree with the REAL
     adapters on the same payloads. The Telegram core is checked against
     the adapter's classmethod delegate (kept as a shim) with PTB-style
     mock objects; the WhatsApp core against the adapter's
     _build_message_event_from_cloud output on a state-stubbed adapter.
     This is what makes the extraction honest: if the adapter regains
     divergent inline logic, these fail.
  3. Committed-vectors lockstep (the openapi.json discipline).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_ingress_vectors import (  # noqa: E402
    BOT_USER_ID,
    DISCORD_CORPUS,
    SLACK_BOT_USER_ID,
    SLACK_CORPUS,
    TELEGRAM_CORPUS,
    WHATSAPP_CORPUS,
    WA_CONTACTS,
    WA_METADATA,
    discord_expected,
    generate,
    slack_expected,
    telegram_expected,
    whatsapp_expected,
)

PLATFORMS = ("discord", "slack", "telegram", "whatsapp")


# ── layer 1: generator invariants ────────────────────────────────────────


def test_corpus_ids_unique():
    for corpus in (TELEGRAM_CORPUS, WHATSAPP_CORPUS, DISCORD_CORPUS, SLACK_CORPUS):
        ids = [vid for vid, _ in corpus]
        assert len(ids) == len(set(ids))
    assert len(TELEGRAM_CORPUS) >= 12 and len(WHATSAPP_CORPUS) >= 12
    assert len(DISCORD_CORPUS) >= 12 and len(SLACK_CORPUS) >= 10


def test_generation_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a)
    generate(b)
    for platform in PLATFORMS:
        assert (a / f"{platform}.json").read_text(encoding="utf-8") == (
            b / f"{platform}.json"
        ).read_text(encoding="utf-8")


def test_vector_file_shape(tmp_path):
    generate(tmp_path)
    for platform in PLATFORMS:
        doc = json.loads((tmp_path / f"{platform}.json").read_text(encoding="utf-8"))
        assert doc["platform"] == platform
        assert doc["direction"] == "ingress"
        assert doc["oracle"]["repo"] == "NousResearch/hermes-agent"
        for v in doc["vectors"]:
            assert v["payload"], v["id"]
            assert "expected" in v, v["id"]


# ── layer 2: oracle fidelity (core ≡ adapter) ────────────────────────────


def _ptb_like(payload: Dict[str, Any]) -> Any:
    """Recursively wrap a Bot API dict as attribute-accessible objects,
    mimicking python-telegram-bot's surface for the fields the parser reads
    (including `from` → `from_user` and absent-field None semantics)."""

    class _Node:
        def __init__(self, d: Dict[str, Any]):
            self._d = d

        def __getattr__(self, name: str) -> Any:
            key = "from" if name == "from_user" else name
            if key not in self._d:
                return None
            return _wrap(self._d[key])

        def __bool__(self) -> bool:
            return bool(self._d)

    def _wrap(value: Any) -> Any:
        return _Node(value) if isinstance(value, dict) else value

    return _Node(payload)


def test_telegram_core_matches_adapter_thread_rules():
    """The adapter's _effective_message_thread_id (now a delegate shim) and
    the pure core must agree on every corpus payload — via PTB-style
    objects on the adapter side and raw dicts on the core side, proving the
    dual access model holds."""
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.telegram.telegram_parse import (
        effective_message_thread_id,
    )

    for vid, payload in TELEGRAM_CORPUS:
        core_on_dict = effective_message_thread_id(payload)
        adapter_on_obj = TelegramAdapter._effective_message_thread_id(
            _ptb_like(payload)
        )
        assert core_on_dict == adapter_on_obj, vid


def test_telegram_core_reply_context_matches_adapter_rules():
    from plugins.platforms.telegram.telegram_parse import extract_reply_context

    for vid, payload in TELEGRAM_CORPUS:
        rid_dict, rtext_dict = extract_reply_context(payload)
        rid_obj, rtext_obj = extract_reply_context(_ptb_like(payload))
        assert (rid_dict, rtext_dict) == (rid_obj, rtext_obj), vid
        if "reply_to_message" in payload:
            assert rid_dict == str(payload["reply_to_message"]["message_id"]), vid


@pytest.mark.asyncio
async def test_whatsapp_core_matches_adapter_event_fields():
    """Drive the REAL _build_message_event_from_cloud (state stubbed: no
    gating, no media download, no store) and assert its MessageEvent agrees
    with the pure core on every derivable field."""
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
    from gateway.platforms.whatsapp_cloud_parse import (
        document_fallback_body,
        parse_cloud_message,
    )

    adapter = WhatsAppCloudAdapter.__new__(WhatsAppCloudAdapter)
    adapter.config = PlatformConfig()
    adapter.platform = Platform.WHATSAPP  # build_source reads self.platform
    adapter._last_inbound_wamid_by_chat = {}
    adapter._should_process_message = lambda data: True  # bypass gating
    adapter._bounded_put = lambda cache, k, v: None

    async def _no_download(media_id: str, ext_hint: Optional[str] = None):
        return None, None

    adapter._download_media_to_cache = _no_download

    async def _no_dispatch(raw, contacts):
        return False

    adapter._dispatch_interactive_reply = _no_dispatch

    for vid, payload in WHATSAPP_CORPUS:
        parsed = parse_cloud_message(payload, WA_CONTACTS, WA_METADATA)
        event = await adapter._build_message_event_from_cloud(
            payload, WA_CONTACTS, WA_METADATA
        )
        if parsed.group_shaped:
            assert event is None, vid  # refusal path
            continue
        assert event is not None, vid
        assert event.source.chat_id == parsed.chat_id, vid
        assert event.source.user_id == parsed.sender_id, vid
        assert event.message_id == parsed.wamid, vid
        assert event.message_type == parsed.message_type, vid
        assert event.reply_to_message_id == parsed.reply_to_id, vid
        assert event.reply_to_is_own_message == parsed.reply_to_is_own, vid
        # Body: for caption-less documents the adapter substitutes the
        # [Document: name] placeholder (media_id present ⇒ applied even
        # when the download itself fails) — the core models that rule as
        # document_fallback_body().
        assert event.text == document_fallback_body(parsed), vid


# ── layer 3: committed-vectors lockstep ──────────────────────────────────


def test_committed_ingress_vectors_match_regeneration(tmp_path):
    committed_dir = REPO_ROOT / "tests" / "conformance" / "ingress_vectors"
    if not committed_dir.exists():
        pytest.skip("no committed ingress vectors in this checkout")
    generate(tmp_path)
    for platform in PLATFORMS:
        fresh = json.loads((tmp_path / f"{platform}.json").read_text(encoding="utf-8"))
        committed = json.loads(
            (committed_dir / f"{platform}.json").read_text(encoding="utf-8")
        )
        fresh["oracle"].pop("commit")
        committed["oracle"].pop("commit")
        assert fresh == committed, (
            f"{platform} ingress vectors drifted — run "
            "`python scripts/generate_ingress_vectors.py` and commit"
        )


def test_expected_fields_cover_scar_rules():
    """The corpus must actually exercise the named inbound scar tissue."""
    tg = {vid: telegram_expected(p) for vid, p in TELEGRAM_CORPUS}
    assert tg["forum-general-topic"]["thread_id"] == "1"  # #22423
    assert tg["group-reply-anchor-not-thread"]["thread_id"] is None  # #3206
    assert tg["forum-topic-message"]["thread_id"] == "55"
    assert tg["dm-topic-message"]["thread_id"] == "9"
    assert tg["reply-partial-quote"]["reply_to_text"] == "just this part"  # #22619
    assert tg["reply-to-caption-only"]["reply_to_text"] == "photo caption"
    assert tg["channel-post"]["user_id"] == tg["channel-post"]["chat_id"]
    assert tg["bot-authored-message"]["user_is_bot"] is True

    wa = {vid: whatsapp_expected(p) for vid, p in WHATSAPP_CORPUS}
    assert wa["reply-to-bot-message"]["reply_to_is_own"] is True
    assert wa["reply-to-own-message"]["reply_to_is_own"] is False
    assert wa["group-shaped-refused"]["group_shaped"] is True
    assert wa["voice-note"]["message_type"] == "voice"
    assert wa["sticker"]["message_type"] == "photo"
    assert wa["list-reply-title"]["body"] == "Option Two"
    assert wa["button-reply"]["body"] == "Approve"
    assert wa["document-with-filename"]["document_filename"] == "notes.txt"
    assert wa["unknown-type-defaults-text"]["message_type"] == "text"

    dc = {vid: discord_expected(p) for vid, p in DISCORD_CORPUS}
    assert dc["guild-mention-stripped"]["text"] == "summarize this"
    assert dc["guild-nickname-mention-form"]["mentions_bot"] is True  # <@!id> form
    assert dc["addressed-command"]["message_type"] == "command"  # strip THEN detect
    assert dc["voice-note-attachment"]["message_type"] == "voice"
    assert dc["plain-audio-attachment"]["message_type"] == "audio"
    assert dc["document-attachment-unknown-type"]["message_type"] == "document"
    assert dc["forwarded-snapshot-text"]["text"] == "forwarded wisdom"
    assert dc["reply-inherits-referenced-attachment"]["message_type"] == "document"
    assert dc["forum-thread-naming"]["chat_name"] == "Eng / reports / bug report"
    assert dc["thread-message"]["chat_name"] == "Eng / #general / build issue"
    assert dc["thread-message"]["thread_id"] == "t1"
    assert dc["display-name-preference"]["user_name"] == "Benjamin"
    assert dc["bot-authored"]["user_is_bot"] is True

    sl = {vid: slack_expected(p) for vid, p in SLACK_CORPUS}
    assert sl["thread-root-equals-ts"]["chat_type"] == "channel"  # #15464
    assert sl["thread-root-equals-ts"]["session_thread_ts"] == "100.7"
    assert sl["channel-thread-reply"]["chat_type"] == "thread"
    assert sl["channel-thread-reply"]["session_thread_ts"] == "100.4"
    assert sl["mpim-is-dm-but-not-one-to-one"]["is_dm"] is True
    assert sl["mpim-is-dm-but-not-one-to-one"]["is_one_to_one_dm"] is False
    assert sl["dm-prefix-fallback"]["is_dm"] is True
    assert sl["channel-mention"]["mentions_bot"] is True
    assert sl["channel-mention"]["text"] == "do the thing"  # token stripped
    assert sl["bot-message-subtype"]["user_is_bot"] is True
    assert sl["bot-id-without-subtype"]["user_is_bot"] is True


# ── layer 2 (discord/slack): oracle fidelity ─────────────────────────────


def test_discord_core_matches_adapter_helpers():
    """The adapter's remaining helper entry points (delegate shims + the
    staticmethod voice check) must agree with the pure core — proving the
    delegation is real, not parallel logic."""
    from types import SimpleNamespace

    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.discord.discord_parse import (
        AttachmentView,
        is_voice_message_attachment,
    )

    cases = [
        AttachmentView(is_voice_message=True),
        AttachmentView(is_voice_message=False, duration=1.0, waveform="x"),
        AttachmentView(duration=2.0, waveform="y"),
        AttachmentView(duration=2.0),  # waveform missing → not voice
        AttachmentView(),
    ]
    for av in cases:
        sdk_like = SimpleNamespace(
            is_voice_message=av.is_voice_message,
            duration=av.duration,
            waveform=av.waveform,
        )
        assert DiscordAdapter._is_discord_voice_message_attachment(
            sdk_like
        ) == is_voice_message_attachment(av), av


def test_discord_thread_naming_matches_adapter():
    """_format_thread_chat_name (now a delegate) ≡ core thread_chat_name
    across the naming shapes, via SDK-like objects."""
    from types import SimpleNamespace

    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)

    forum_parent = SimpleNamespace(
        name="reports", guild=SimpleNamespace(name="Eng"), type="forum"
    )
    text_parent = SimpleNamespace(
        name="general", guild=SimpleNamespace(name="Eng"), type="text"
    )

    def _is_forum(parent):
        return parent is forum_parent

    adapter._is_forum_parent = _is_forum

    thread_in_forum = SimpleNamespace(
        id=1, name="bug report", parent=forum_parent, guild=None
    )
    thread_in_text = SimpleNamespace(
        id=2, name="build issue", parent=text_parent, guild=None
    )
    orphan = SimpleNamespace(id=3, name="lonely", parent=None, guild=None)

    assert adapter._format_thread_chat_name(thread_in_forum) == "Eng / reports / bug report"
    assert adapter._format_thread_chat_name(thread_in_text) == "Eng / #general / build issue"
    assert adapter._format_thread_chat_name(orphan) == "lonely"


def test_slack_core_matches_adapter_rules():
    """The Slack adapter now delegates DM classification + channel scoping
    to the core; assert the core reproduces the adapter's documented legacy
    behaviors on the corpus (payload-only rules)."""
    from plugins.platforms.slack.slack_parse import (
        slack_is_dm,
        slack_is_one_to_one_dm,
        slack_session_thread_ts,
    )

    for vid, event in SLACK_CORPUS:
        exp = slack_expected(event)
        assert slack_is_dm(event) == exp["is_dm"], vid
        assert slack_is_one_to_one_dm(event) == exp["is_one_to_one_dm"], vid
        assert slack_session_thread_ts(event) == exp["session_thread_ts"], vid

    # reply_in_thread=false → shared channel session (thread key None) for
    # top-level channel messages, but genuine thread replies keep their key.
    top = dict(SLACK_CORPUS[3][1])  # channel-top-level
    reply = dict(SLACK_CORPUS[4][1])  # channel-thread-reply
    assert slack_session_thread_ts(top, reply_in_thread=False) is None
    assert slack_session_thread_ts(reply, reply_in_thread=False) == "100.4"
