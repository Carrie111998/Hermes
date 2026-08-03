"""Focused regression tests for Buzz adapter DM classification (t_bc8d998b).

Reproduces the group->dm session split: a DM message whose prose names the
agent ("Hey Orion, ...") was previously excluded from DM detection because it
"visibly mentions" the agent, so the first message dispatched as group and a
later un-mentioned message latched dm — splitting the conversation into two
sessions with no shared history.
"""

import sys
import types
from collections import OrderedDict
from unittest.mock import MagicMock

# Import the adapter module without executing its import-time plugin hooks.
sys.path.insert(0, "/home/solo/.hermes/hermes-agent/plugins/platforms/buzz")


def _make_adapter():
    """Build an adapter with the DM classification surface wired up."""
    import adapter as buzz_adapter

    obj = object.__new__(buzz_adapter.BuzzAdapter)
    obj._self_pubkey = "a" * 64
    obj._self_npub = "npub1test"
    obj._display_name = "Orion"
    obj._channel_meta = {
        # DM-shaped channel: name == "DM", empty description
        "dm-chat-1": {"name": "DM", "description": ""},
        # Real community channel: real name + description
        "eng-channel": {"name": "engineering", "description": "engineering dept"},
    }
    obj._channel_names = {}
    obj.channels = ["eng-channel"]
    obj._channel_state = {}
    return obj, buzz_adapter


def _event(pubkey, content, p_tag_self=True, kind=9, self_pubkey=None):
    # In a DM, the event p-tags the RECIPIENT (the agent).  Default to
    # tagging the agent's own pubkey ("a"*64) so the fixture mirrors a real
    # incoming DM to us.
    target = self_pubkey or ("a" * 64)
    tags = [["p", target], ["e", "root-id"]] if p_tag_self else []
    return {"id": "e" + pubkey[:8], "kind": kind, "pubkey": pubkey, "content": content, "tags": tags}


def _is_dm(adapter, channel_id, event):
    """Call the bound method via the class."""
    import adapter as buzz_adapter
    return buzz_adapter.BuzzAdapter._is_direct_message_event(adapter, channel_id, event)


def _latch(adapter, channel_id, state, event):
    import adapter as buzz_adapter
    return buzz_adapter.BuzzAdapter._maybe_latch_dm(adapter, channel_id, state, event)


def test_dm_prose_naming_agent_latches():
    adapter, mod = _make_adapter()
    event = _event("b" * 64, "Hey Orion, do you know why this job is progressing?")
    # Channel metadata already says DM -> p-tag alone latches, even with the
    # agent's name in prose.
    assert _is_dm(adapter, "dm-chat-1", event) is True

    state = {"chat_type": "group", "last_ts": 0, "seen": OrderedDict()}
    _latch(adapter, "dm-chat-1", state, event)
    assert state["chat_type"] == "dm", "DM prose-naming message must latch to dm"


def test_real_channel_typed_mention_not_reclassified():
    adapter, mod = _make_adapter()
    # Real community channel with a typed mention: p-tag + visible mention.
    event = _event("b" * 64, "@Orion please fix this")
    assert _is_dm(adapter, "eng-channel", event) is False

    state = {"chat_type": "group", "last_ts": 0, "seen": OrderedDict()}
    _latch(adapter, "eng-channel", state, event)
    assert state["chat_type"] == "group", "real channel must stay group"


def test_dm_mentionless_still_latches():
    adapter, mod = _make_adapter()
    event = _event("b" * 64, "Are you stuck. It has been awhile since you responded last?")
    assert _is_dm(adapter, "dm-chat-1", event) is True


def test_metadata_less_channel_typed_mention_not_reclassified():
    adapter, mod = _make_adapter()
    # Metadata-less conversation (not configured, no meta): typed mention must
    # NOT reclassify (protects real channels that leak in without metadata).
    event = _event("b" * 64, "@Orion look at this")
    adapter._channel_meta = {}
    assert _is_dm(adapter, "mystery-chat", event) is False


if __name__ == "__main__":
    tests = [
        test_dm_prose_naming_agent_latches,
        test_real_channel_typed_mention_not_reclassified,
        test_dm_mentionless_still_latches,
        test_metadata_less_channel_typed_mention_not_reclassified,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
