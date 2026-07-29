#!/usr/bin/env python3
"""Ingress conformance-vector generator — native inbound parsers as spec.

The egress twin (generate_conformance_vectors.py) renders text through the
native RENDERERS; this renders synthetic PLATFORM PAYLOADS through the
native inbound PARSERS (the pure cores extracted in this change:
plugins.platforms.telegram.telegram_parse, gateway.platforms.
whatsapp_cloud_parse) and dumps payload→expected-fields JSON vectors. The
gateway-gateway connector commits them under conformance/ingress/ and its
vitest runner asserts the CONNECTOR normalizers (telegram.ts / whatsapp.ts
+ the poller mappers) derive the SAME fields — so inbound parsing drift
(dropped categories, wrong thread routing, mangled reply context) breaks a
test instead of silently eating messages.

Platform scope:
  telegram  raw Bot API update dicts → thread routing (#3206/#22423 rules),
            chat-type normalization, reply context w/ partial quotes,
            source identity.
  whatsapp  Cloud API message objects → type mapping, body extraction,
            reply context (id + is_own), media identification, the
            group-shape refusal.
  discord   SDK-view IR dicts (layer 2 — see discord_parse.py's layer
            model) → chat-type classification, mention stripping,
            forwarded-snapshot folding, attachment→type classification,
            reply references, thread naming. Layer 1 (payload→SDK object)
            is the documented SDK-equivalence axiom: discord.py and the
            connector's raw MESSAGE_CREATE handling are assumed to resolve
            the same view fields.
  slack     Events API message events → DM/MPIM classification, thread_ts
            session scoping (#15421/#15464), mention detection, bot-message
            classification.

Field vocabulary is the WIRE MessageEvent's (chat_id, chat_type, thread_id,
user_id, message_id, text, reply_to_*, message_type/media) — the shared
language of both repos' inbound planes.

Run:  python scripts/generate_ingress_vectors.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

GENERATOR_VERSION = 1

# ── telegram corpus: raw Bot API message dicts ───────────────────────────

def _tg_chat(**kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"id": -1001234, "type": "supergroup", "title": "Eng Group"}
    base.update(kw)
    return base


def _tg_user(**kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"id": 777, "is_bot": False, "first_name": "Ben", "username": "ben"}
    base.update(kw)
    return base


TELEGRAM_CORPUS: List[tuple] = [
    (
        "dm-plain-text",
        {"message_id": 1, "chat": {"id": 555, "type": "private", "first_name": "Ben"},
         "from": _tg_user(), "text": "hello"},
    ),
    (
        "group-plain-text",
        {"message_id": 2, "chat": _tg_chat(), "from": _tg_user(), "text": "hi group"},
    ),
    (
        "channel-post",
        {"message_id": 3, "chat": {"id": -1009, "type": "channel", "title": "Announce"},
         "text": "channel news"},  # channel posts carry no `from`
    ),
    (
        "forum-topic-message",
        {"message_id": 4, "chat": _tg_chat(is_forum=True), "from": _tg_user(),
         "text": "topic chat", "message_thread_id": 55, "is_topic_message": True},
    ),
    (
        # #22423: forum General topic delivers thread_id=None; routes to "1".
        "forum-general-topic",
        {"message_id": 5, "chat": _tg_chat(is_forum=True), "from": _tg_user(),
         "text": "general chat"},
    ),
    (
        # #3206: a plain group reply carries a reply-UI anchor in
        # message_thread_id — NOT a routable thread.
        "group-reply-anchor-not-thread",
        {"message_id": 6, "chat": _tg_chat(), "from": _tg_user(), "text": "re",
         "message_thread_id": 42,
         "reply_to_message": {"message_id": 42, "chat": _tg_chat(),
                              "from": _tg_user(id=888, first_name="Alice"),
                              "text": "original"}},
    ),
    (
        "dm-topic-message",
        {"message_id": 7, "chat": {"id": 555, "type": "private", "first_name": "Ben"},
         "from": _tg_user(), "text": "dm topic", "message_thread_id": 9,
         "is_topic_message": True},
    ),
    (
        "reply-with-text",
        {"message_id": 8, "chat": _tg_chat(), "from": _tg_user(), "text": "re",
         "reply_to_message": {"message_id": 7, "chat": _tg_chat(),
                              "from": _tg_user(id=999, is_bot=True, first_name="Hermes"),
                              "text": "what the bot said"}},
    ),
    (
        # #22619: native partial quote wins over the full replied-to text.
        "reply-partial-quote",
        {"message_id": 9, "chat": _tg_chat(), "from": _tg_user(), "text": "re",
         "quote": {"text": "just this part"},
         "reply_to_message": {"message_id": 7, "chat": _tg_chat(),
                              "from": _tg_user(id=888, first_name="Alice"),
                              "text": "a long message with many parts"}},
    ),
    (
        "reply-to-caption-only",
        {"message_id": 10, "chat": _tg_chat(), "from": _tg_user(), "text": "re",
         "reply_to_message": {"message_id": 7, "chat": _tg_chat(),
                              "from": _tg_user(id=888, first_name="Alice"),
                              "caption": "photo caption"}},
    ),
    (
        "bot-authored-message",
        {"message_id": 11, "chat": _tg_chat(), "from": _tg_user(id=999, is_bot=True,
         first_name="OtherBot"), "text": "bot chatter"},
    ),
    (
        "command-message",
        {"message_id": 12, "chat": {"id": 555, "type": "private", "first_name": "Ben"},
         "from": _tg_user(), "text": "/status"},
    ),
    (
        "cjk-emoji-text",
        {"message_id": 13, "chat": _tg_chat(), "from": _tg_user(),
         "text": "中文 — test 🎉 (100%)"},
    ),
    (
        "empty-text-media-placeholder",
        {"message_id": 14, "chat": _tg_chat(), "from": _tg_user()},  # no text at all
    ),
]

# ── whatsapp corpus: Cloud API message objects ───────────────────────────

WA_METADATA = {"display_phone_number": "15559998888", "phone_number_id": "PNID1"}
WA_CONTACTS = {"15551110000": "Ben", "15559998888": "Hermes Biz"}

WHATSAPP_CORPUS: List[tuple] = [
    (
        "text-message",
        {"from": "15551110000", "id": "wamid.t1", "type": "text",
         "text": {"body": "hello"}},
    ),
    (
        "command-text",
        {"from": "15551110000", "id": "wamid.t2", "type": "text",
         "text": {"body": "/status"}},
    ),
    (
        "image-with-caption",
        {"from": "15551110000", "id": "wamid.m1", "type": "image",
         "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "look"}},
    ),
    (
        "voice-note",
        {"from": "15551110000", "id": "wamid.m2", "type": "audio",
         "audio": {"id": "MEDIA2", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
    ),
    (
        "document-with-filename",
        {"from": "15551110000", "id": "wamid.m3", "type": "document",
         "document": {"id": "MEDIA3", "mime_type": "text/plain", "filename": "notes.txt"}},
    ),
    (
        "sticker",
        {"from": "15551110000", "id": "wamid.m4", "type": "sticker",
         "sticker": {"id": "MEDIA4", "mime_type": "image/webp"}},
    ),
    (
        "reply-to-bot-message",
        {"from": "15551110000", "id": "wamid.r1", "type": "text",
         "text": {"body": "re"},
         "context": {"id": "wamid.orig", "from": "15559998888"}},
    ),
    (
        "reply-to-own-message",
        {"from": "15551110000", "id": "wamid.r2", "type": "text",
         "text": {"body": "re self"},
         "context": {"id": "wamid.mine", "from": "15551110000"}},
    ),
    (
        "button-reply",
        {"from": "15551110000", "id": "wamid.b1", "type": "button",
         "button": {"text": "Approve", "payload": "approve-1"}},
    ),
    (
        "list-reply-title",
        {"from": "15551110000", "id": "wamid.b2", "type": "interactive",
         "interactive": {"type": "list_reply",
                         "list_reply": {"id": "opt-2", "title": "Option Two"}}},
    ),
    (
        "group-shaped-refused",
        {"from": "15551110000", "id": "wamid.g1", "type": "text",
         "chat": "1203630xxxx@g.us", "text": {"body": "group msg"}},
    ),
    (
        "unknown-type-defaults-text",
        {"from": "15551110000", "id": "wamid.u1", "type": "reaction",
         "reaction": {"emoji": "👍", "message_id": "wamid.t1"}},
    ),
    (
        "cjk-emoji-body",
        {"from": "15551110000", "id": "wamid.c1", "type": "text",
         "text": {"body": "中文 — test 🎉 (100%)"}},
    ),
]


# ── discord corpus: SDK-view IR dicts (layer 2) ──────────────────────────

BOT_USER_ID = "424242"

DISCORD_CORPUS: List[tuple] = [
    (
        "dm-plain-text",
        {"id": "1001", "content": "hello", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben", "display_name": "Ben"}},
    ),
    (
        "guild-mention-stripped",
        {"id": "1002", "content": f"<@{BOT_USER_ID}> summarize this", "channel_id": "c1",
         "channel_name": "general", "channel_kind": "guildText", "guild_id": "g1",
         "guild_name": "Eng", "author": {"id": "777", "username": "ben"},
         "mentions": [{"id": BOT_USER_ID}]},
    ),
    (
        "guild-nickname-mention-form",
        {"id": "1003", "content": f"<@!{BOT_USER_ID}> hi", "channel_id": "c1",
         "channel_name": "general", "channel_kind": "guildText", "guild_id": "g1",
         "author": {"id": "777", "username": "ben"}, "mentions": []},
    ),
    (
        "addressed-command",
        {"id": "1004", "content": f"<@{BOT_USER_ID}> /new", "channel_id": "c1",
         "channel_kind": "guildText", "guild_id": "g1",
         "author": {"id": "777", "username": "ben"}, "mentions": [{"id": BOT_USER_ID}]},
    ),
    (
        "thread-message",
        {"id": "1005", "content": "in thread", "channel_id": "t1",
         "channel_name": "build issue", "channel_kind": "thread", "guild_id": "g1",
         "guild_name": "Eng", "parent_channel_id": "c1", "parent_channel_name": "general",
         "author": {"id": "777", "username": "ben"}},
    ),
    (
        "forum-thread-naming",
        {"id": "1006", "content": "forum post", "channel_id": "t2",
         "channel_name": "bug report", "channel_kind": "thread", "guild_id": "g1",
         "guild_name": "Eng", "parent_channel_id": "f1", "parent_channel_name": "reports",
         "parent_is_forum": True, "author": {"id": "777", "username": "ben"}},
    ),
    (
        "image-attachment",
        {"id": "1007", "content": "look", "channel_id": "c1", "channel_kind": "guildText",
         "guild_id": "g1", "author": {"id": "777", "username": "ben"},
         "attachments": [{"content_type": "image/png", "filename": "a.png",
                          "url": "https://cdn.discordapp.com/a.png"}]},
    ),
    (
        "voice-note-attachment",
        {"id": "1008", "content": "", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben"},
         "attachments": [{"content_type": "audio/ogg", "is_voice_message": True,
                          "duration": 3.2, "waveform": "AAA="}]},
    ),
    (
        "plain-audio-attachment",
        {"id": "1009", "content": "", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben"},
         "attachments": [{"content_type": "audio/mpeg", "filename": "song.mp3"}]},
    ),
    (
        "document-attachment-unknown-type",
        {"id": "1010", "content": "", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben"},
         "attachments": [{"filename": "data.bin"}]},  # no content_type ⇒ DOCUMENT
    ),
    (
        "forwarded-snapshot-text",
        {"id": "1011", "content": "", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben"},
         "message_snapshots": [{"content": "forwarded wisdom"}]},
    ),
    (
        "reply-with-reference",
        {"id": "1012", "content": "re", "channel_id": "c1", "channel_kind": "guildText",
         "guild_id": "g1", "author": {"id": "777", "username": "ben"},
         "referenced_message": {"id": "999", "attachments": []}},
    ),
    (
        "reply-inherits-referenced-attachment",
        {"id": "1013", "content": "what is this file?", "channel_id": "c1",
         "channel_kind": "guildText", "guild_id": "g1",
         "author": {"id": "777", "username": "ben"},
         "referenced_message": {"id": "999",
                                "attachments": [{"content_type": "application/pdf",
                                                 "filename": "spec.pdf"}]}},
    ),
    (
        "bot-authored",
        {"id": "1014", "content": "bot says", "channel_id": "c1",
         "channel_kind": "guildText", "guild_id": "g1",
         "author": {"id": "999", "username": "otherbot", "bot": True}},
    ),
    (
        "display-name-preference",
        {"id": "1015", "content": "hi", "channel_id": "d1", "channel_kind": "dm",
         "author": {"id": "777", "username": "ben", "display_name": "Benjamin"}},
    ),
]

# ── slack corpus: Events API message events ──────────────────────────────

SLACK_BOT_USER_ID = "U0BOT"

SLACK_CORPUS: List[tuple] = [
    (
        "dm-plain-text",
        {"channel": "D111", "channel_type": "im", "ts": "100.1", "user": "U777",
         "team": "T1", "text": "hello"},
    ),
    (
        "dm-prefix-fallback",
        {"channel": "D222", "ts": "100.2", "user": "U777", "text": "no channel_type"},
    ),
    (
        "mpim-is-dm-but-not-one-to-one",
        {"channel": "G333", "channel_type": "mpim", "ts": "100.3", "user": "U777",
         "team": "T1", "text": "group dm"},
    ),
    (
        "channel-top-level",
        {"channel": "C444", "channel_type": "channel", "ts": "100.4", "user": "U777",
         "team": "T1", "text": "top level"},
    ),
    (
        "channel-thread-reply",
        {"channel": "C444", "channel_type": "channel", "ts": "100.6", "user": "U777",
         "team": "T1", "text": "in thread", "thread_ts": "100.4"},
    ),
    (
        # #15464: thread_ts == ts is a thread-ROOT shape, not a reply.
        "thread-root-equals-ts",
        {"channel": "C444", "channel_type": "channel", "ts": "100.7", "user": "U777",
         "team": "T1", "text": "root shape", "thread_ts": "100.7"},
    ),
    (
        "channel-mention",
        {"channel": "C444", "channel_type": "channel", "ts": "100.8", "user": "U777",
         "team": "T1", "text": f"<@{SLACK_BOT_USER_ID}> do the thing"},
    ),
    (
        "dm-thread-reply",
        {"channel": "D111", "channel_type": "im", "ts": "100.9", "user": "U777",
         "text": "dm threaded", "thread_ts": "100.1"},
    ),
    (
        "bot-message-subtype",
        {"channel": "C444", "channel_type": "channel", "ts": "101.0",
         "subtype": "bot_message", "bot_id": "B99", "text": "integration says"},
    ),
    (
        "bot-id-without-subtype",
        {"channel": "C444", "channel_type": "channel", "ts": "101.1", "bot_id": "B99",
         "text": "workflow post"},
    ),
    (
        "private-group-channel",
        {"channel": "G555", "channel_type": "group", "ts": "101.2", "user": "U777",
         "team": "T1", "text": "private channel"},
    ),
    (
        "cjk-emoji-text",
        {"channel": "C444", "channel_type": "channel", "ts": "101.3", "user": "U777",
         "team": "T1", "text": "中文 — test 🎉 (100%)"},
    ),
]

# ── expected-field derivation via the pure cores ─────────────────────────


def telegram_expected(payload: Dict[str, Any]) -> Dict[str, Any]:
    from plugins.platforms.telegram.telegram_parse import parse_telegram_message

    p = parse_telegram_message(payload)
    return {
        "chat_id": p.chat_id,
        "chat_type": p.chat_type,
        "chat_name": p.chat_name,
        "thread_id": p.thread_id,
        "user_id": p.user_id,
        "user_name": p.user_name,
        "user_is_bot": p.user_is_bot,
        "message_id": p.message_id,
        "text": p.text,
        "reply_to_message_id": p.reply_to_id,
        "reply_to_text": p.reply_to_text,
    }


def whatsapp_expected(payload: Dict[str, Any]) -> Dict[str, Any]:
    from gateway.platforms.whatsapp_cloud_parse import parse_cloud_message

    p = parse_cloud_message(payload, WA_CONTACTS, WA_METADATA)
    return {
        "msg_type": p.msg_type_str,
        "message_type": p.message_type.value
        if hasattr(p.message_type, "value")
        else str(p.message_type),
        "body": p.body,
        "sender_id": p.sender_id,
        "sender_name": p.sender_name,
        "chat_id": p.chat_id,
        "message_id": p.wamid,
        "reply_to_message_id": p.reply_to_id,
        "reply_to_is_own": p.reply_to_is_own,
        "media_id": p.media_id,
        "media_mime": p.media_mime,
        "document_filename": p.document_filename,
        "group_shaped": p.group_shaped,
    }


def discord_expected(payload: Dict[str, Any]) -> Dict[str, Any]:
    from plugins.platforms.discord.discord_parse import (
        parse_discord_message,
        view_from_dict,
    )

    p = parse_discord_message(view_from_dict(payload), BOT_USER_ID)
    return {
        "chat_type": p.chat_type,
        "chat_id": p.chat_id,
        "chat_name": p.chat_name,
        "thread_id": p.thread_id,
        "parent_chat_id": p.parent_chat_id,
        "guild_id": p.guild_id,
        "user_id": p.user_id,
        "user_name": p.user_name,
        "user_is_bot": p.user_is_bot,
        "message_id": p.message_id,
        "text": p.text,
        "message_type": p.message_type.value
        if hasattr(p.message_type, "value")
        else str(p.message_type),
        "reply_to_message_id": p.reply_to_message_id,
        "mentions_bot": p.mentions_bot,
    }


def slack_expected(payload: Dict[str, Any]) -> Dict[str, Any]:
    from plugins.platforms.slack.slack_parse import parse_slack_event

    p = parse_slack_event(payload, SLACK_BOT_USER_ID)
    return {
        "chat_type": p.chat_type,
        "chat_id": p.chat_id,
        "is_dm": p.is_dm,
        "is_one_to_one_dm": p.is_one_to_one_dm,
        "session_thread_ts": p.session_thread_ts,
        "user_id": p.user_id,
        "team_id": p.team_id,
        "message_id": p.message_id,
        "text": p.text,
        "mentions_bot": p.mentions_bot,
        "user_is_bot": p.user_is_bot,
    }


def _oracle_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def generate(out_dir: Path) -> Dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    commit = _oracle_commit()
    summary: Dict[str, int] = {}
    corpora = {
        "telegram": (TELEGRAM_CORPUS, telegram_expected,
                     {"contacts": None, "metadata": None}),
        "whatsapp": (WHATSAPP_CORPUS, whatsapp_expected,
                     {"contacts": WA_CONTACTS, "metadata": WA_METADATA}),
        "discord": (DISCORD_CORPUS, discord_expected,
                    {"bot_user_id": BOT_USER_ID}),
        "slack": (SLACK_CORPUS, slack_expected,
                  {"bot_user_id": SLACK_BOT_USER_ID}),
    }
    for platform, (corpus, derive, ctx) in sorted(corpora.items()):
        vectors = []
        for vid, payload in corpus:
            vectors.append({
                "id": vid,
                "payload": payload,
                "expected": derive(payload),
            })
        doc = {
            "$comment": (
                "GENERATED — do not hand-edit. Regenerate with hermes-agent "
                "scripts/generate_ingress_vectors.py; the native inbound "
                "parse cores are the oracle (executable spec)."
            ),
            "oracle": {
                "repo": "NousResearch/hermes-agent",
                "commit": commit,
                "generator": "scripts/generate_ingress_vectors.py",
                "generator_version": GENERATOR_VERSION,
            },
            "platform": platform,
            "direction": "ingress",
            "context": {k: v for k, v in ctx.items() if v is not None},
            "vectors": vectors,
        }
        path = out_dir / f"{platform}.json"
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary[platform] = len(vectors)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "tests" / "conformance" / "ingress_vectors"),
        help="Output directory for <platform>.json ingress vector files",
    )
    args = parser.parse_args()
    for platform, count in sorted(generate(Path(args.out)).items()):
        print(f"{platform}: {count} ingress vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
