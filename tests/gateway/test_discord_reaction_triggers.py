"""Reaction-trigger feature: emoji normalization, gating, synthesis."""
import asyncio
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# discord.py is an optional dep; mock it so the adapter imports
# (same shim as test_discord_attachment_download).
# ---------------------------------------------------------------------------
def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object, button=lambda *a, **k: (lambda fn: fn), Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()


class _FakeEmoji:
    def __init__(self, s: str) -> None:
        self._s = s

    def __str__(self) -> str:
        return self._s


def _make_adapter() -> "Any":
    # EXACT mirror of test_discord_platform_events.py::_adapter (:79-86).
    # Bare instance skips heavy __init__ but MUST still set the three
    # attributes below: build_source reads self.platform/self.gateway_runner
    # (base.py:7152/:7145) and the `name` log-property derives from
    # self.platform (base.py:3627-3629).
    import plugins.platforms.discord.adapter as discord_adapter_module
    adapter_cls = discord_adapter_module.DiscordAdapter
    a = object.__new__(adapter_cls)
    a.platform = Platform.DISCORD
    a.config = SimpleNamespace(extra={})
    a.gateway_runner = None
    return a


def test_normalize_unicode_emoji_passthrough():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("👍")) == "👍"


def test_normalize_custom_emoji_strips_wrapper():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("<:paw:1234567890>")) == "paw"


def test_normalize_animated_custom_emoji():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("<a:zoom:42>")) == "zoom"


def test_normalize_empty_is_empty():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(None) == ""


def test_triggers_default_off(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    assert adapter._reaction_trigger_config() == (False, None)


def test_triggers_all_emoji_when_true(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", "true")
    assert adapter._reaction_trigger_config() == (True, None)


def test_triggers_allowlist_parsed(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", "👍, paw ,✅")
    enabled, allowlist = adapter._reaction_trigger_config()
    assert enabled is True
    assert allowlist == {"👍", "paw", "✅"}


def test_triggers_empty_allowlist_is_disabled(monkeypatch):
    # ",,," parses to an EMPTY allowlist -> must be (False, None), never
    # (True, set()): an inverted signal would read as "enabled for ALL emoji"
    # under the Slack-style convention where empty set == allow-all.
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", ",,,")
    assert adapter._reaction_trigger_config() == (False, None)


def test_yaml_list_maps_to_comma_env(monkeypatch):
    # exercise _apply_yaml_config the way cli-config loads it
    import os

    import plugins.platforms.discord.adapter as m
    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    yaml_cfg = {"discord": {"reaction_triggers": ["👍", "❤️"]}}
    discord_cfg = yaml_cfg["discord"]
    try:
        # call the real mapper with the same shape other keys use (mirror its callers)
        m._apply_yaml_config(yaml_cfg, discord_cfg)
        assert os.getenv("DISCORD_REACTION_TRIGGERS") == "👍,❤️"
    finally:
        # _apply_yaml_config writes os.environ DIRECTLY, which monkeypatch
        # does not track — pop manually or the value leaks into later tests.
        os.environ.pop("DISCORD_REACTION_TRIGGERS", None)


def test_yaml_bridge_skips_env_write_under_profile_scope(monkeypatch):
    # Multiplex profile loads (#72348) must seed PlatformConfig.extra WITHOUT
    # writing process-global env: a secondary profile's gates can never become
    # another profile's policy. Simulate _profile_scoped_config_load() -> True
    # and assert seed lands while env stays unset.
    import os

    import plugins.platforms.discord.adapter as m

    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    monkeypatch.setattr(m, "_profile_scoped_config_load", lambda: True)
    yaml_cfg = {"discord": {"reaction_triggers": ["👍"]}}
    try:
        seeded = m._apply_yaml_config(yaml_cfg, yaml_cfg["discord"])
        assert os.getenv("DISCORD_REACTION_TRIGGERS") is None
    finally:
        os.environ.pop("DISCORD_REACTION_TRIGGERS", None)
    assert seeded is not None
    assert seeded.get("reaction_triggers") == "👍"


def test_hydration_remember_and_lookup():
    adapter = _make_adapter()
    adapter._remember_outbound_snippet("111", "hello world")
    assert adapter._lookup_outbound_snippet("111") == "hello world"


def test_hydration_bounds_and_trim():
    adapter = _make_adapter()
    for i in range(600):
        adapter._remember_outbound_snippet(str(i), f"msg {i}")
    assert len(adapter._reaction_targets) == 512
    assert adapter._lookup_outbound_snippet("0") is None      # oldest evicted
    assert adapter._lookup_outbound_snippet("599") == "msg 599"


def test_hydration_snippet_capped_at_200_chars():
    adapter = _make_adapter()
    adapter._remember_outbound_snippet("222", "x" * 500)
    assert len(adapter._lookup_outbound_snippet("222")) == 200


def test_hydration_reremember_refreshes_recency_and_overwrites():
    adapter = _make_adapter()
    adapter._remember_outbound_snippet("1", "old")
    adapter._remember_outbound_snippet("2", "two")
    adapter._remember_outbound_snippet("1", "new")
    # overwrite wins
    assert adapter._lookup_outbound_snippet("1") == "new"
    # recency refresh: inserting up to the cap evicts "2" (older), not "1"
    for i in range(511):
        adapter._remember_outbound_snippet(str(1000 + i), f"m{i}")
    assert adapter._lookup_outbound_snippet("1") == "new"
    assert adapter._lookup_outbound_snippet("2") is None


# ===========================================================================
# Task 4: raw reaction listener + gate chain + synthesis (canonical order)
#
# Gate 1 validity drop -> Gate 2 self-drop (ABSOLUTE FIRST behavioral gate)
# -> Gate 3 hook fan-out (fires regardless of opt-in) -> Gate 4 opt-in off
# => hooks-only return -> Gate 5 allowlist -> Gate 6 target-authorship
# (6a message_author_id fast path / 6b single-fetch fallback) -> Gate 7
# channel policy (DMs SKIP; unresolvable guild channel drops) -> Gate 8
# reactor authorization -> synthesize MessageEvent via generic handle_message.
# ===========================================================================
from gateway.platforms.base import MessageType


@dataclasses.dataclass
class _FakePayload:
    user_id: int
    message_id: int
    channel_id: int
    guild_id: Optional[int] = None
    emoji: Any = None
    member: Any = None
    message_author_id: Optional[int] = None


class _FakeClientUser:
    id = 999


def _reaction_payload(**overrides) -> _FakePayload:
    defaults = dict(
        user_id=555, message_id=42, channel_id=2,
        guild_id=777, emoji=_FakeEmoji("👍"), member=None,
        message_author_id=999,  # bot-authored target -> Gate 6a fast path
    )
    defaults.update(overrides)
    return _FakePayload(**defaults)


def _patch_common(monkeypatch, adapter, *, authz_stub: bool = True):
    monkeypatch.setattr(type(adapter), "_reaction_trigger_config",
                        lambda self: (True, None), raising=False)
    # Gate 8 stub: real _is_allowed_user FAILS CLOSED on bare instances.
    # authz_stub=False keeps the REAL method (role-only deployment test).
    if authz_stub:
        monkeypatch.setattr(type(adapter), "_is_allowed_user",
                            lambda self, user_id, author=None, **kw: True,
                            raising=False)
    # Fake client: .user drives self-drop; get_channel/fetch_channel must
    # resolve ANY id so Gate 7 can build channel keys.
    def _get_channel(cid):
        return SimpleNamespace(id=int(cid), name="general",
                               parent_id=None, parent=None)
    adapter._client = type("C", (), {
        "user": _FakeClientUser(),
        "get_channel": staticmethod(_get_channel),
        "fetch_channel": staticmethod(_get_channel),
        "fetch_user": staticmethod(lambda uid: SimpleNamespace(id=int(uid))),
    })()
    dispatched = []

    async def fake_handle_message(event):
        dispatched.append(event)
        return None
    # INSTANCE attribute, not type(adapter): class-stored functions bind self
    monkeypatch.setattr(adapter, "handle_message", fake_handle_message,
                        raising=False)
    hooks = []
    adapter._reaction_handler = lambda ctx: hooks.append(ctx) or asyncio.sleep(0)
    return dispatched, hooks


@pytest.mark.asyncio
async def test_self_reaction_dropped_absolute_first(monkeypatch):
    # enabled=(True,None) ON PURPOSE: proves self-drop precedes dispatch AND
    # hook fan-out — the bot must never echo its own emoji acks anywhere.
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    payload = _FakePayload(user_id=999, message_id=1, channel_id=2,
                           emoji=_FakeEmoji("✅"), message_author_id=999)
    await adapter._handle_reaction_payload(payload, added=True)
    assert dispatched == []          # never echoes own acks
    assert hooks == []               # acks never fire the hook surface either


@pytest.mark.asyncio
async def test_opt_in_off_fires_hook_only(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    monkeypatch.setattr(type(adapter), "_reaction_trigger_config",
                        lambda self: (False, None), raising=False)
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert len(hooks) == 1           # hook fires regardless of opt-in (Gate 3)
    assert dispatched == []          # Gate 4: opt-in off => hooks-only return


@pytest.mark.asyncio
async def test_allowlist_filters_unlisted_emoji(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    monkeypatch.setattr(type(adapter), "_reaction_trigger_config",
                        lambda self: (True, {"👍"}), raising=False)
    await adapter._handle_reaction_payload(
        _reaction_payload(emoji=_FakeEmoji("🎱")), added=True)
    assert dispatched == []
    assert [h["reaction"] for h in hooks] == ["🎱"]   # hook fired even so
    await adapter._handle_reaction_payload(
        _reaction_payload(emoji=_FakeEmoji("👍")), added=True)
    assert len(dispatched) == 1      # listed emoji passes the allowlist gate


@pytest.mark.asyncio
async def test_foreign_authored_target_dropped(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    payload = _reaction_payload(message_author_id=123)  # someone else's message
    await adapter._handle_reaction_payload(payload, added=True)
    assert dispatched == []          # Gate 6a: only reactions to OUR messages
    assert len(hooks) == 1           # hook still fired (precedes Gate 6)


@pytest.mark.asyncio
async def test_remove_fetch_fallback_mismatch_drops(monkeypatch):
    # REACTION_REMOVE lacks message_author_id -> EVERY remove takes Gate 6b.
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    calls = []

    async def fake_resolve(mid, pl):
        calls.append((mid, pl))
        return False, None

    monkeypatch.setattr(adapter, "_resolve_reaction_target", fake_resolve,
                        raising=False)
    await adapter._handle_reaction_payload(
        _reaction_payload(message_author_id=None), added=False)
    assert len(calls) == 1           # EXACTLY ONE fetch attempt per reaction
    assert calls[0][0] == "42"
    assert calls[0][1] is not None
    assert dispatched == []
    assert len(hooks) == 1


@pytest.mark.asyncio
async def test_remove_fetch_fallback_match_proceeds(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    calls = []

    async def fake_resolve(mid, pl):
        calls.append(mid)
        return True, "snippet text"

    monkeypatch.setattr(adapter, "_resolve_reaction_target", fake_resolve,
                        raising=False)
    await adapter._handle_reaction_payload(
        _reaction_payload(message_author_id=None), added=False)
    assert calls == ["42"]
    assert len(dispatched) == 1
    assert dispatched[0].text == "reaction:removed:👍"
    assert dispatched[0].reply_to_text == "snippet text"


@pytest.mark.asyncio
async def test_channel_policy_allowed_and_ignored(monkeypatch):
    cases = [
        # ignored wins even when the channel is also allowed
        ({"allowed": {"*"}, "ignored": {"2"}}, False),
        # allowed set that excludes the reaction's channel drops it
        ({"allowed": {"other-channel"}, "ignored": set()}, False),
        # allowed set containing the channel passes
        ({"allowed": {"general"}, "ignored": set()}, True),
    ]
    for patched, expect_dispatch in cases:
        adapter = _make_adapter()
        dispatched, hooks = _patch_common(monkeypatch, adapter)
        monkeypatch.setattr(type(adapter), "_get_allowed_channels",
                            lambda self: set(patched["allowed"]), raising=False)
        monkeypatch.setattr(type(adapter), "_get_ignored_channels",
                            lambda self: set(patched["ignored"]), raising=False)
        await adapter._handle_reaction_payload(_reaction_payload(), added=True)
        got = len(dispatched)
        assert got == (1 if expect_dispatch else 0), patched
        assert len(hooks) >= 1       # hook fired regardless of channel policy


@pytest.mark.asyncio
async def test_synthesis_shape_with_hydrated_snippet(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    adapter._remember_outbound_snippet("42", "bot text")
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert len(dispatched) == 1
    ev = dispatched[0]
    assert ev.text == "reaction:added:👍"
    assert ev.message_type is MessageType.TEXT
    assert ev.message_id == str(42)
    assert ev.reply_to_message_id == str(42)
    assert ev.reply_to_is_own_message is True
    assert ev.reply_to_text == "bot text"


@pytest.mark.asyncio
async def test_synthesis_snippet_miss_still_dispatches(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert len(dispatched) == 1
    assert dispatched[0].reply_to_text is None   # unknown target still dispatches
    # Known-but-empty map hit ("": captionless attachment) must ALSO render as
    # None, not an empty pointer (photon precedent: content.get("targetText")
    # or None).
    adapter._remember_outbound_snippet("43", "")
    await adapter._handle_reaction_payload(_reaction_payload(message_id=43),
                                           added=True)
    assert len(dispatched) == 2
    assert dispatched[1].reply_to_text is None


@pytest.mark.asyncio
async def test_removed_action_yields_removed_event(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    await adapter._handle_reaction_payload(_reaction_payload(), added=False)
    assert dispatched[0].text == "reaction:removed:👍"
    assert hooks[0]["event_name"] == "reaction:removed"


@pytest.mark.asyncio
async def test_unauthorized_reactor_dropped(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    monkeypatch.setattr(type(adapter), "_is_allowed_user",
                        lambda self, user_id, author=None, **kw: False,
                        raising=False)
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert dispatched == []          # Gate 8 fails the reactor
    assert len(hooks) == 1           # hook already fired at Gate 3


@pytest.mark.asyncio
async def test_thread_payload_source_thread_aware(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    monkeypatch.setattr(type(adapter), "_thread_id_and_chat_for_channel",
                        lambda self, ch: ("555", "555"), raising=False)
    await adapter._handle_reaction_payload(
        _reaction_payload(channel_id=555), added=True)
    src = dispatched[0].source
    assert getattr(src, "thread_id", None) == "555"
    assert str(src.chat_id) == "555"


@pytest.mark.asyncio
async def test_dm_payload_source_chat_type_dm(monkeypatch):
    # guild_id=None mirrors a DM raw payload: Gate 7 skipped entirely and the
    # source MUST use chat_type="dm" (the thread/group helper hardcodes those).
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    await adapter._handle_reaction_payload(
        _reaction_payload(channel_id=888, guild_id=None), added=True)
    src = dispatched[0].source
    assert src.chat_type == "dm"
    assert str(src.chat_id) == "888"


@pytest.mark.asyncio
async def test_hook_dict_shape(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    payload = _reaction_payload(user_id=555, message_id=42, channel_id=2)
    await adapter._handle_reaction_payload(payload, added=True)
    assert len(hooks) == 1
    h = hooks[0]
    assert h["platform"] == "discord"
    assert h["event_name"].startswith("reaction:")
    assert h["event_name"] == "reaction:added"
    assert h["reaction"] == "👍"
    assert h["user_id"] == "555"
    # REACTION_ADD carries the TRUE author of the reacted-to message
    assert h["item_user_id"] == str(payload.message_author_id)
    assert h["channel_id"] == "2"
    assert h["message_ts"] == str(payload.message_id)
    assert isinstance(h["event_ts"], str)
    assert h["raw_event"] is payload
    # Discord parity omissions vs the Slack shape
    assert "item_type" not in h
    assert "team_id" not in h
    # REMOVE payloads lack message_author_id -> item_user_id is None
    await adapter._handle_reaction_payload(
        _reaction_payload(user_id=556, message_author_id=None), added=False)
    assert len(hooks) == 2
    assert hooks[1]["event_name"] == "reaction:removed"
    assert hooks[1]["item_user_id"] is None


# ===========================================================================
# Quality-review follow-ups: role_authorized propagation (gateway cold-path
# delegation route), Gate 1 precedence, hook-failure isolation, fail-closed
# channel resolution, remove-hydration semantics.
# ===========================================================================
@pytest.mark.asyncio
async def test_role_authorized_stamped_on_source(monkeypatch):
    # Deployments authorized via DISCORD_ALLOWED_ROLES pass Gate 8 with no env
    # user allowlist; the gateway cold path (_is_user_authorized delegation,
    # authz_mixin.py) then reads source.role_authorized — it MUST be True on
    # every synthesized source or every reaction event is dropped there.
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)   # authz stub: True
    adapter._allowed_role_ids = {"111222333"}                 # post-patch stamp
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert len(dispatched) == 1
    assert dispatched[0].source.role_authorized is True


@pytest.mark.asyncio
async def test_role_only_deployment_dispatches_with_real_authz(monkeypatch):
    # REAL _is_allowed_user — no authz stub. Invariant: a deployment authorized
    # ONLY by DISCORD_ALLOWED_ROLES lets a role-holding reactor through Gate 8,
    # and the synthesized source carries the grant for gateway cold-path authz.
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter, authz_stub=False)
    # Real _get_allowed_roles() stores INTS (int(str(entry)), adapter.py),
    # and _is_allowed_user matches `r.id in allowed_roles` directly — so the
    # configured id and the fake role id must both be ints to match.
    adapter._allowed_role_ids = {111222333}
    adapter._allowed_user_ids = set()
    reactor = SimpleNamespace(id=555000111222333444, name="reactor",
                              roles=[SimpleNamespace(id=111222333)])
    # The real matcher takes its DM-role branch when guild context is None
    # (disabled by default) — the resolved channel must carry a guild so the
    # guild-scoped member.roles path runs.
    def _guilded_channel(cid):
        return SimpleNamespace(id=int(cid), name="general", parent_id=None,
                               parent=None, guild=SimpleNamespace(id=777))

    async def _unresolvable(cid):
        return None

    adapter._client.get_channel = _guilded_channel
    adapter._client.fetch_channel = _unresolvable
    payload = _reaction_payload(user_id=555000111222333444, member=reactor)
    await adapter._handle_reaction_payload(payload, added=True)
    assert len(dispatched) == 1        # role-only grant authorizes dispatch
    assert dispatched[0].source.role_authorized is True


@pytest.mark.asyncio
async def test_gate_1_malformed_payload_silently_dropped(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    await adapter._handle_reaction_payload(
        _reaction_payload(emoji=None), added=True)
    await adapter._handle_reaction_payload(
        _reaction_payload(message_id=""), added=True)
    assert dispatched == []          # Gate 1 drops malformed payloads
    assert hooks == []               # ...and precedes hook fan-out (Gate 3)


@pytest.mark.asyncio
async def test_raising_hook_handler_does_not_break_dispatch(monkeypatch):
    adapter = _make_adapter()
    dispatched, _hooks = _patch_common(monkeypatch, adapter)

    async def _exploding_handler(ctx):
        raise RuntimeError("hook handler exploded")

    adapter._reaction_handler = _exploding_handler
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    # The swallow in _emit_reaction_hook protects the dispatch pipeline
    assert len(dispatched) == 1
    assert dispatched[0].text == "reaction:added:👍"


@pytest.mark.asyncio
async def test_unresolvable_guild_channel_drops_fail_closed(monkeypatch):
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    # Fresh fake client: get_channel AND fetch_channel both resolve nothing.
    async def _none_async(cid):
        return None

    adapter._client = type("C", (), {
        "user": _FakeClientUser(),
        "get_channel": staticmethod(lambda cid: None),
        "fetch_channel": staticmethod(_none_async),
        "fetch_user": staticmethod(lambda uid: SimpleNamespace(id=int(uid))),
    })()
    await adapter._handle_reaction_payload(_reaction_payload(), added=True)
    assert dispatched == []          # unresolvable guild channel -> drop
    assert len(hooks) == 1           # hook still fired (Gate 3 precedes Gate 7)


@pytest.mark.asyncio
async def test_remove_uses_map_snippet_over_fetch(monkeypatch):
    # REACTION_REMOVE hydration semantics: the single fetch still runs (it is
    # what proves bot authorship at Gate 6b), but a known map snippet wins for
    # reply_to_text over whatever the fetch returned.
    adapter = _make_adapter()
    dispatched, hooks = _patch_common(monkeypatch, adapter)
    adapter._remember_outbound_snippet(str(42), "map text")
    fetch_calls = []

    async def counting_resolve(mid, pl):
        fetch_calls.append(mid)
        return True, "fetched text"

    monkeypatch.setattr(adapter, "_resolve_reaction_target", counting_resolve,
                        raising=False)
    await adapter._handle_reaction_payload(
        _reaction_payload(message_author_id=None), added=False)
    assert len(fetch_calls) == 1     # EXACTLY ONE fetch, still for authorship
    assert len(dispatched) == 1
    assert dispatched[0].text == "reaction:removed:👍"
    assert dispatched[0].reply_to_text == "map text"   # map wins for hydration


# ===========================================================================
# Fix round Task A: Gate 6b merge must honor the hydration-map sentinel.
# "" = known-but-empty (a hit); None = unknown (fall back to the fetch).
# These exercise the REAL _resolve_reaction_target through a fake client
# whose fetch_message returns bot-authored text.
# ===========================================================================
def _client_with_bot_fetch(adapter, *, content: str) -> None:
    """Swap in a fake client whose channels resolve and whose fetch_message
    returns a bot-authored (id=999) message with the given content."""
    chan = SimpleNamespace(id=2, name="general", parent_id=None, parent=None)

    async def _fetch_message(mid):
        return SimpleNamespace(author=SimpleNamespace(id=999), content=content)

    chan.fetch_message = _fetch_message

    def _get_channel(cid):
        return chan if int(cid) == chan.id else None

    async def _fetch_channel(cid):
        return _get_channel(cid)

    adapter._client = type("C", (), {
        "user": _FakeClientUser(),
        "get_channel": staticmethod(_get_channel),
        "fetch_channel": staticmethod(_fetch_channel),
        "fetch_user": staticmethod(lambda uid: SimpleNamespace(id=int(uid))),
    })()


@pytest.mark.asyncio
async def test_remove_known_empty_sentinel_not_overwritten_by_fetch(monkeypatch):
    # Map holds "" (captionless attachment) for msg X; REMOVE takes Gate 6b,
    # whose authorship fetch returns real text. The fetched text must NOT
    # clobber the known-empty hit: reply_to_text renders None either way,
    # but only because "" was honored — not because the fetch won the merge.
    adapter = _make_adapter()
    dispatched, _hooks = _patch_common(monkeypatch, adapter)
    adapter._remember_outbound_snippet("42", "")
    _client_with_bot_fetch(adapter, content="fetched text")
    await adapter._handle_reaction_payload(
        _reaction_payload(message_author_id=None), added=False)
    assert len(dispatched) == 1      # authorship fetch passed Gate 6b
    assert dispatched[0].reply_to_text is None   # "" sentinel survived


@pytest.mark.asyncio
async def test_remove_unknown_target_falls_back_to_fetch_text(monkeypatch):
    # Map miss (None = unknown): the Gate 6b fetch result IS the hydration.
    adapter = _make_adapter()
    dispatched, _hooks = _patch_common(monkeypatch, adapter)
    _client_with_bot_fetch(adapter, content="fetched text")
    await adapter._handle_reaction_payload(
        _reaction_payload(message_author_id=None), added=False)
    assert len(dispatched) == 1
    assert dispatched[0].reply_to_text == "fetched text"
