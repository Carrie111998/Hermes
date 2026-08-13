"""Bot-identity matching with realistic Baileys multi-device JIDs.

The bridge's ``sock.user.id`` / ``sock.user.lid`` carry a device index
(``"15551230000:17@s.whatsapp.net"``) while inbound ``mentionedIds`` and
``quotedParticipant`` arrive without it.  ``botIds`` must normalize to the
clean form or every set-membership check against them silently fails —
which is exactly what happened when normalization folded the colon into an
``"@"`` instead of stripping the suffix (the existing gating tests never
caught it because they hand-write clean botIds).
"""

from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig

# What Baileys actually hands the bridge for the bot's own identity:
BOT_ID_RAW = "15551230000:17@s.whatsapp.net"
BOT_LID_RAW = "98765432101112:5@lid"
# What WhatsApp puts in mentionedJid / contextInfo.participant:
BOT_ID_CLEAN = "15551230000@s.whatsapp.net"
BOT_LID_CLEAN = "98765432101112@lid"


def _make_adapter(**extra):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    extra.setdefault("require_mention", True)
    extra.setdefault("group_policy", "open")
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = "pairing"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._mention_patterns = adapter._compile_mention_patterns()
    return adapter


def _group_message(body="hello", **overrides):
    data = {
        "isGroup": True,
        "body": body,
        "chatId": "120363001234567890@g.us",
        "senderId": "6281234567890@s.whatsapp.net",
        "senderName": "Alice",
        "mentionedIds": [],
        # Realistic device-suffixed identities, as the bridge computes them
        # from sock.user.id / sock.user.lid.
        "botIds": [BOT_ID_RAW, BOT_LID_RAW],
        "quotedParticipant": "",
    }
    data.update(overrides)
    return data


def test_normalize_strips_device_suffix_only_inside_jids():
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    norm = WhatsAppAdapter._normalize_whatsapp_id
    assert norm(BOT_ID_RAW) == BOT_ID_CLEAN
    assert norm(BOT_LID_RAW) == BOT_LID_CLEAN
    # Already-clean forms pass through untouched.
    assert norm(BOT_ID_CLEAN) == BOT_ID_CLEAN
    assert norm("120363001234567890@g.us") == "120363001234567890@g.us"
    assert norm("15551230000") == "15551230000"
    assert norm("") == ""
    assert norm(None) == ""


def test_native_mention_matches_device_suffixed_bot_ids():
    adapter = _make_adapter()

    # Body deliberately contains no digits: the only trigger signal is the
    # native mentionedIds entry, so this fails unless botIds normalize to
    # the clean form (the bare-number substring fallback can't save it).
    message = _group_message("what do you think?", mentionedIds=[BOT_ID_CLEAN])
    assert adapter._message_mentions_bot(message) is True
    assert adapter._should_process_message(message) is True


def test_quote_reply_to_bot_retriggers_under_require_mention():
    adapter = _make_adapter()

    # A resident replies (quotes) the bot's answer — WhatsApp reports the
    # quoted author without a device suffix.
    message = _group_message("thanks, and on friday?", quotedParticipant=BOT_ID_CLEAN)
    assert adapter._message_is_reply_to_bot(message) is True
    assert adapter._should_process_message(message) is True

    # Quoting anyone else still does not trigger.
    other = _group_message("what did he say?", quotedParticipant="6289999999999@s.whatsapp.net")
    assert adapter._message_is_reply_to_bot(other) is False
    assert adapter._should_process_message(other) is False


def test_lid_quote_reply_matches_too():
    adapter = _make_adapter()

    message = _group_message("replying", quotedParticipant=BOT_LID_CLEAN)
    assert adapter._message_is_reply_to_bot(message) is True


def test_suffixed_quoted_participant_matches_clean_bot_ids():
    # Defense in depth for the mirror case: if a producer ever emits the
    # quoted author WITH a device suffix, normalization on both sides still
    # converges on the same clean form.
    adapter = _make_adapter()

    message = _group_message(
        "replying",
        botIds=[BOT_ID_CLEAN, BOT_LID_CLEAN],
        quotedParticipant=BOT_ID_RAW,
    )
    assert adapter._message_is_reply_to_bot(message) is True


def test_mention_stripping_still_removes_bare_number():
    adapter = _make_adapter()

    data = _group_message("@15551230000 what is the weather?")
    cleaned = adapter._clean_bot_mention_text(data["body"], data)
    assert "15551230000" not in cleaned
    assert "weather" in cleaned
