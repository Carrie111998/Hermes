"""WhatsApp DM/group allowlist must resolve phone↔LID aliases at intake.

Regression for #14486: WhatsApp now delivers inbound DM senders in LID form
(``<id>@lid``) while operators configure the allowlist with phone numbers.
The adapter-level gate (``_is_dm_allowed`` / ``_is_group_allowed`` →
``_should_process_message``) did a raw set-membership check with no LID
resolution, so every DM from an allowed user was silently dropped before the
gateway authz layer ever ran.

The fix routes the adapter gate through the shared
``gateway.whatsapp_identity.expand_whatsapp_aliases`` helper, which reads the
bridge's ``lid-mapping-*.json`` session files (the same source the gateway
authz and session-key paths already use).
"""

import json
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from hermes_constants import get_hermes_home


PHONE = "351912345678"
LID = "77214955630717"


def _make_adapter(dm_policy=None, allow_from=None, group_policy=None, group_allow_from=None):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    extra = {}
    if dm_policy is not None:
        extra["dm_policy"] = dm_policy
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if group_policy is not None:
        extra["group_policy"] = group_policy
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from

    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = str(extra.get("dm_policy", "open")).strip().lower()
    adapter._allow_from = WhatsAppAdapter._coerce_allow_list(extra.get("allow_from"))
    adapter._group_policy = str(extra.get("group_policy", "open")).strip().lower()
    adapter._group_allow_from = WhatsAppAdapter._coerce_allow_list(
        extra.get("group_allow_from")
    )
    return adapter


def _write_lid_mapping(phone=PHONE, lid=LID):
    """Mirror what the JS bridge writes: phone→lid and lid→phone (reverse)."""
    session_dir = get_hermes_home() / "whatsapp" / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"lid-mapping-{phone}.json").write_text(json.dumps(lid), encoding="utf-8")
    (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
        json.dumps(phone), encoding="utf-8"
    )


# --------------------------------------------------------------------- DM gate

def test_dm_phone_allowlist_matches_lid_sender():
    """allow_from has the phone number; inbound sender arrives as @lid (the bug)."""
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[PHONE])

    assert adapter._is_dm_allowed(f"{LID}@lid") is True


def test_dm_phone_with_plus_allowlist_matches_lid_sender():
    """A ``+``-prefixed phone allowlist entry still resolves to the LID sender."""
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[f"+{PHONE}"])

    assert adapter._is_dm_allowed(f"{LID}@lid") is True


def test_dm_lid_allowlist_matches_phone_sender():
    """Reverse direction: allow_from has the LID, sender arrives as phone JID."""
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[LID])

    assert adapter._is_dm_allowed(f"{PHONE}@s.whatsapp.net") is True


def test_dm_exact_phone_jid_still_matches():
    """allow_from with the bare phone matches a phone-JID sender without any mapping."""
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[PHONE])

    assert adapter._is_dm_allowed(f"{PHONE}@s.whatsapp.net") is True


def test_dm_wildcard_allows_any_sender():
    adapter = _make_adapter(dm_policy="allowlist", allow_from=["*"])

    assert adapter._is_dm_allowed(f"{LID}@lid") is True


def test_dm_unlisted_lid_sender_blocked():
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[PHONE])

    assert adapter._is_dm_allowed("99999999999999@lid") is False


def test_dm_empty_allowlist_blocks_everyone():
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[])

    assert adapter._is_dm_allowed(f"{LID}@lid") is False


def test_dm_disabled_policy_blocks_even_allowlisted():
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="disabled", allow_from=[PHONE])

    assert adapter._is_dm_allowed(f"{LID}@lid") is False


def test_dm_open_policy_allows_anyone_with_opt_in(monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    adapter = _make_adapter(dm_policy="open")

    assert adapter._is_dm_allowed("anyone@lid") is True


def test_dm_open_policy_blocked_without_opt_in():
    adapter = _make_adapter(dm_policy="open")

    assert adapter._is_dm_allowed("anyone@lid") is False


# ------------------------------------------------------------------ group gate

def test_group_jid_exact_match_still_works():
    """Group allowlists use full ``@g.us`` JIDs — exact match must pass through."""
    adapter = _make_adapter(
        group_policy="allowlist", group_allow_from=["120363001234567890@g.us"]
    )

    assert adapter._is_group_allowed("120363001234567890@g.us") is True


def test_group_unlisted_jid_blocked():
    adapter = _make_adapter(
        group_policy="allowlist", group_allow_from=["120363001234567890@g.us"]
    )

    assert adapter._is_group_allowed("120363009999999999@g.us") is False


# ------------------------------------------------------ end-to-end intake gate

def test_should_process_message_dm_phone_allowlist_lid_sender():
    """Full intake path: a DM from a phone-allowlisted contact arriving as @lid."""
    _write_lid_mapping()
    adapter = _make_adapter(dm_policy="allowlist", allow_from=[PHONE])

    data = {
        "isGroup": False,
        "body": "hello",
        "senderId": f"{LID}@lid",
        "from": f"{LID}@lid",
        "botIds": [],
        "mentionedIds": [],
    }
    assert adapter._should_process_message(data) is True


# ------------------------------------- strict WHATSAPP_ALLOWED_USERS backstop

def _strict_adapter(session_dir):
    """Adapter stub for the strict-allowlist backstop path.

    ``_expand_whatsapp_strict_aliases`` reads ``self._session_path`` directly
    rather than going through ``gateway.whatsapp_identity``, so this path needs
    its own coverage.
    """
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._session_path = session_dir
    return adapter


def _write_strict_mapping(session_dir, phone=PHONE, lid=LID):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"lid-mapping-{phone}.json").write_text(
        json.dumps(lid), encoding="utf-8"
    )
    (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
        json.dumps(phone), encoding="utf-8"
    )


def test_strict_alias_expansion_resolves_lid_to_phone(tmp_path):
    """Regression for the F821 NameError on bare ``json`` in the adapter.

    ``json.loads`` inside ``_expand_whatsapp_strict_aliases`` sat in a
    ``try/except Exception``, so the NameError was swallowed and every
    ``lid-mapping-*.json`` file was silently skipped — the expansion returned
    only the seed identifier.
    """
    session = tmp_path / "session"
    _write_strict_mapping(session)
    adapter = _strict_adapter(session)

    assert adapter._expand_whatsapp_strict_aliases(f"{LID}@lid") == {LID, PHONE}


def test_strict_alias_expansion_resolves_phone_to_lid(tmp_path):
    session = tmp_path / "session"
    _write_strict_mapping(session)
    adapter = _strict_adapter(session)

    assert adapter._expand_whatsapp_strict_aliases(
        f"{PHONE}@s.whatsapp.net"
    ) == {PHONE, LID}


def test_strict_allowlist_does_not_drop_phone_allowlisted_lid_sender(tmp_path, monkeypatch):
    """The security backstop must not drop an allowlisted user's own message.

    With alias expansion broken, a sender delivered in LID form never matched a
    phone-form ``WHATSAPP_ALLOWED_USERS`` entry, so the backstop added after the
    2026-04-19 allowlist leak silently dropped legitimate inbound DMs.
    """
    session = tmp_path / "session"
    _write_strict_mapping(session)
    adapter = _strict_adapter(session)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", PHONE)

    blocked = adapter._is_sender_blocked_by_strict_allowlist(
        {"senderId": f"{LID}@lid"}, f"{LID}@lid"
    )
    assert blocked is False


def test_strict_allowlist_still_drops_unlisted_sender(tmp_path, monkeypatch):
    """The backstop must keep blocking senders that resolve to nothing allowed."""
    session = tmp_path / "session"
    _write_strict_mapping(session)
    adapter = _strict_adapter(session)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", PHONE)

    blocked = adapter._is_sender_blocked_by_strict_allowlist(
        {"senderId": "99999999999999@lid"}, "99999999999999@lid"
    )
    assert blocked is True


def test_strict_alias_expansion_survives_corrupt_mapping_file(tmp_path):
    """A malformed mapping file is skipped without taking down expansion."""
    session = tmp_path / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / f"lid-mapping-{LID}_reverse.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    adapter = _strict_adapter(session)

    assert adapter._expand_whatsapp_strict_aliases(f"{LID}@lid") == {LID}
