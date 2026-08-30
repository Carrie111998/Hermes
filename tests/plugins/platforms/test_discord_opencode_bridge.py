"""Tests for the OpenCode permission bridge (Discord plugin).

Covers the fail-closed contract end to end without network access:

- events: SSE assembly + ``permission.updated`` parsing (malformed dropped)
- config: opt-in gating, loopback enforcement, allowlist requirement
- discord: Accept/Reject buttons, allowlist authorization, timeout->reject,
  parallel requests resolving independently
- replies: official API body ``{"response": "once" | "reject"}`` via
  ``POST /session/{id}/permissions/{permissionID}`` (mock transport)
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from plugins.platforms.discord.opencode_bridge import (
    BridgePendingRegistry,
    OpenCodeBridge,
    OpenCodeBridgeClient,
    SseAssembler,
    is_loopback_url,
    parse_bridge_config,
    parse_permission_event,
)


def _permission_event(permission_id="perm-1", session_id="ses-abc", **overrides):
    props = {
        "id": permission_id,
        "type": "bash",
        "sessionID": session_id,
        "messageID": "msg-1",
        "title": "rm -rf build/",
        "metadata": {"command": ["rm", "-rf", "build/"]},
        "time": {"created": 1756500000},
    }
    props.update(overrides)
    return {"type": "permission.updated", "properties": props}


# ---------------------------------------------------------------------------
# Config parsing (fail-closed gating)
# ---------------------------------------------------------------------------


class TestBridgeConfig:
    def test_absent_section_is_disabled(self):
        config = parse_bridge_config({})
        assert config.enabled is False
        assert config.disabled_reason == "not configured"

    def test_explicitly_disabled(self):
        config = parse_bridge_config({"opencode_bridge": {"enabled": False}})
        assert config.enabled is False
        assert config.disabled_reason == "disabled"

    def test_empty_allowlist_disables(self):
        config = parse_bridge_config({
            "opencode_bridge": {
                "enabled": True,
                "channel_id": "123",
                "allowed_user_ids": [],
            }
        })
        assert config.enabled is False
        assert config.disabled_reason == "allowed_user_ids is empty"

    def test_non_loopback_base_url_disables(self):
        config = parse_bridge_config({
            "opencode_bridge": {
                "enabled": True,
                "base_url": "http://example.com:4096",
                "channel_id": "123",
                "allowed_user_ids": ["42"],
            }
        })
        assert config.enabled is False
        assert "loopback" in config.disabled_reason

    def test_missing_channel_disables(self):
        config = parse_bridge_config({
            "opencode_bridge": {"enabled": True, "allowed_user_ids": ["42"]}
        })
        assert config.enabled is False
        assert config.disabled_reason == "channel_id is missing"

    def test_valid_config_enabled(self):
        config = parse_bridge_config({
            "opencode_bridge": {
                "enabled": True,
                "channel_id": "123",
                "allowed_user_ids": ["42", "43"],
                "timeout_seconds": 120,
            }
        })
        assert config.enabled is True
        assert config.base_url == "http://127.0.0.1:4096"
        assert set(config.allowed_user_ids) == {"42", "43"}
        assert config.timeout_seconds == 120

    def test_string_allowlist_is_split(self):
        config = parse_bridge_config({
            "opencode_bridge": {
                "enabled": True,
                "channel_id": "123",
                "allowed_user_ids": "42, 43",
            }
        })
        assert config.enabled is True
        assert set(config.allowed_user_ids) == {"42", "43"}

    def test_timeout_is_clamped(self):
        base = {"enabled": True, "channel_id": "123", "allowed_user_ids": ["42"]}
        assert parse_bridge_config({
            "opencode_bridge": {**base, "timeout_seconds": 1}
        }).timeout_seconds == 30
        assert parse_bridge_config({
            "opencode_bridge": {**base, "timeout_seconds": 99999}
        }).timeout_seconds == 900
        assert parse_bridge_config({
            "opencode_bridge": {**base, "timeout_seconds": "garbage"}
        }).timeout_seconds == 300

    def test_non_dict_section_is_disabled(self):
        assert parse_bridge_config({"opencode_bridge": "yes"}).enabled is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:4096", True),
        ("http://localhost:4096", True),
        ("http://[::1]:4096", True),
        ("http://example.com", False),
        ("http://0.0.0.0:4096", False),
        ("not a url", False),
        ("", False),
    ],
)
def test_is_loopback_url(url, expected):
    assert is_loopback_url(url) is expected


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestPermissionEventParsing:
    def test_valid_event_parses(self):
        request = parse_permission_event(_permission_event())
        assert request is not None
        assert request.permission_id == "perm-1"
        assert request.session_id == "ses-abc"
        assert request.kind == "bash"
        assert request.title == "rm -rf build/"
        assert request.metadata == {"command": ["rm", "-rf", "build/"]}
        assert request.short_session_id == "ses-abc"[:12]

    def test_non_permission_event_dropped(self):
        assert parse_permission_event({"type": "session.idle"}) is None

    def test_missing_properties_dropped(self):
        assert parse_permission_event({"type": "permission.updated"}) is None

    def test_non_dict_payload_dropped(self):
        assert parse_permission_event("permission.updated") is None
        assert parse_permission_event(None) is None

    @pytest.mark.parametrize("props", [
        {"sessionID": "s", "title": "t"},           # no id
        {"id": "p", "title": "t"},                  # no sessionID
        {"id": 42, "sessionID": "s"},               # non-string id
        {"id": "p", "sessionID": 42},               # non-string sessionID
    ])
    def test_malformed_properties_dropped(self, props):
        payload = {"type": "permission.updated", "properties": props}
        assert parse_permission_event(payload) is None

    def test_pattern_list_is_joined(self):
        request = parse_permission_event(
            _permission_event(pattern=["src/*.env", ".env*"])
        )
        assert request is not None
        assert request.pattern == "src/*.env, .env*"

    def test_non_dict_metadata_becomes_empty(self):
        request = parse_permission_event(_permission_event(metadata="oops"))
        assert request is not None
        assert request.metadata == {}


class TestSseAssembler:
    def test_complete_event_yields_payload(self):
        assembler = SseAssembler()
        assert assembler.feed("data: " + json.dumps(_permission_event())) is None
        payload = assembler.feed("")
        assert payload is not None
        assert payload["type"] == "permission.updated"

    def test_multi_line_data_is_joined(self):
        assembler = SseAssembler()
        raw = json.dumps(_permission_event())
        # Split at a top-level token boundary: SSE joins data lines with
        # "\n", which is only valid JSON whitespace between tokens.
        split_at = raw.index(", ") + 1
        assembler.feed(f"data: {raw[:split_at]}")
        assembler.feed(f"data: {raw[split_at:]}")
        payload = assembler.feed("")
        assert payload == _permission_event()

    def test_non_json_block_returns_none(self):
        assembler = SseAssembler()
        assembler.feed("data: [DONE]")
        assert assembler.feed("") is None

    def test_comment_and_event_lines_are_ignored(self):
        assembler = SseAssembler()
        assembler.feed(": keepalive")
        assembler.feed("event: permission.updated")
        assert assembler.feed("") is None

    def test_two_events_in_sequence(self):
        assembler = SseAssembler()
        first = assembler.feed("")  # empty start is harmless
        assert first is None
        assembler.feed("data: " + json.dumps(_permission_event(permission_id="p1")))
        assembler.feed("")
        assembler.feed("data: " + json.dumps(_permission_event(permission_id="p2")))
        second = assembler.feed("")
        assert second["properties"]["id"] == "p2"


# ---------------------------------------------------------------------------
# Pending registry (dedup, first-wins, parallel independence)
# ---------------------------------------------------------------------------


class TestBridgePendingRegistry:
    def test_register_and_resolve(self):
        registry = BridgePendingRegistry()
        assert registry.register("p1") is True
        assert registry.is_pending("p1") is True
        assert registry.resolve("p1", "once") is True
        assert registry.is_pending("p1") is False

    def test_duplicate_register_dropped(self):
        registry = BridgePendingRegistry()
        assert registry.register("p1") is True
        assert registry.register("p1") is False

    def test_first_resolution_wins(self):
        registry = BridgePendingRegistry()
        registry.register("p1")
        assert registry.resolve("p1", "once") is True
        assert registry.resolve("p1", "reject") is False

    def test_resolved_requests_are_memoized(self):
        registry = BridgePendingRegistry()
        registry.register("p1")
        registry.resolve("p1", "once")
        # Redelivery after SSE reconnect must not re-arm a prompt.
        assert registry.register("p1") is False

    def test_parallel_requests_are_independent(self):
        registry = BridgePendingRegistry()
        assert registry.register("p1") is True
        assert registry.register("p2") is True
        assert registry.resolve("p1", "reject") is True
        assert registry.is_pending("p2") is True
        assert registry.resolve("p2", "once") is True

    def test_capacity_limit_drops_new_requests(self):
        registry = BridgePendingRegistry(max_concurrent=2)
        assert registry.register("p1") is True
        assert registry.register("p2") is True
        assert registry.register("p3") is False
        registry.resolve("p1", "once")
        assert registry.register("p3") is True


# ---------------------------------------------------------------------------
# Reply client (official API contract)
# ---------------------------------------------------------------------------


class TestOpenCodeBridgeClient:
    def _client(self, handler):
        return OpenCodeBridgeClient(
            "http://127.0.0.1:4096", transport=httpx.MockTransport(handler)
        )

    @pytest.mark.asyncio
    async def test_reply_once_posts_official_body(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=True)

        client = self._client(handler)
        delivered, status = await client.reply("ses-1", "perm-1", "once")
        await client.aclose()
        assert delivered is True
        assert status == 200
        assert seen["path"] == "/session/ses-1/permissions/perm-1"
        assert seen["body"] == {"response": "once"}

    @pytest.mark.asyncio
    async def test_reply_reject_posts_reject(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=True)

        client = self._client(handler)
        delivered, _ = await client.reply("ses-1", "perm-1", "reject")
        await client.aclose()
        assert delivered is True
        assert seen["body"] == {"response": "reject"}

    @pytest.mark.asyncio
    async def test_404_means_resolved_elsewhere(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        client = self._client(handler)
        delivered, status = await client.reply("ses-1", "perm-1", "once")
        await client.aclose()
        assert delivered is False
        assert status == 404

    def test_non_loopback_base_url_refused(self):
        with pytest.raises(ValueError):
            OpenCodeBridgeClient("http://example.com:4096")


# ---------------------------------------------------------------------------
# Discord prompt flow (views run against real discord.py UI plumbing)
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, embeds=None):
        self.embeds = embeds or []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage(embeds=[kwargs["embed"]] if kwargs.get("embed") else [])


class FakeDiscordClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        return self._channel


class FakeAdapter:
    def __init__(self, channel):
        self._client = FakeDiscordClient(channel)


class StubBridgeClient:
    """Replaces OpenCodeBridgeClient in orchestrator tests."""

    def __init__(self, delivered=True, status=200):
        self.calls = []
        self._result = (delivered, status)

    async def reply(self, session_id, permission_id, response):
        self.calls.append((session_id, permission_id, response))
        return self._result

    async def aclose(self):
        pass


def _bridge_config(**overrides):
    config = parse_bridge_config({
        "opencode_bridge": {
            "enabled": True,
            "channel_id": "123",
            "allowed_user_ids": ["111"],
            "timeout_seconds": 30,
            **overrides,
        }
    })
    assert config.enabled
    return config


def _make_bridge(**stub_kwargs):
    channel = FakeChannel()
    stub = StubBridgeClient(**stub_kwargs)
    bridge = OpenCodeBridge(FakeAdapter(channel), _bridge_config(), client=stub)
    return bridge, channel, stub


class FakeResponse:
    def __init__(self):
        self.calls = []

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send", content))

    async def edit_message(self, **kwargs):
        self.calls.append(("edit", kwargs))


def _interaction(uid, message=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        response=FakeResponse(),
        message=message or FakeMessage(),
    )


async def _drain():
    await asyncio.sleep(0)


class TestDiscordPromptFlow:
    @pytest.mark.asyncio
    async def test_accept_sends_once_reply_and_disables_buttons(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        assert len(channel.sent) == 1
        view = channel.sent[0]["view"]
        content = channel.sent[0]["content"]
        assert "Accept" in content and "Reject" in content

        interaction = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        assert stub.calls == [(request.session_id, request.permission_id, "once")]
        assert all(child.disabled for child in view.children)
        edit = interaction.response.calls[0]
        assert edit[0] == "edit"

    @pytest.mark.asyncio
    async def test_reject_sends_reject_reply(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        interaction = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().reject(view, interaction, None)
        await _drain()

        assert stub.calls == [(request.session_id, request.permission_id, "reject")]

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_answer(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        interaction = _interaction("999", message=FakeMessage())
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        assert stub.calls == []
        assert len(interaction.response.calls) == 1
        assert interaction.response.calls[0][0] == "send"
        assert interaction.response.calls[0][1]  # ephemeral notice text
        assert not any(child.disabled for child in view.children)

    @pytest.mark.asyncio
    async def test_double_click_answers_exactly_once(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        embeds = [channel.sent[0]["embed"]]
        first = _interaction("111", message=FakeMessage(embeds=embeds))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, first, None)
        second = _interaction("111", message=FakeMessage(embeds=embeds))
        await _get_view_class().accept(view, second, None)
        await _drain()

        assert len(stub.calls) == 1
        assert len(second.response.calls) == 1
        assert second.response.calls[0][0] == "send"  # already-resolved notice

    @pytest.mark.asyncio
    async def test_timeout_resolves_reject_fail_closed(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        await view.on_timeout()
        await _drain()

        assert stub.calls == [(request.session_id, request.permission_id, "reject")]
        assert all(child.disabled for child in view.children)

    @pytest.mark.asyncio
    async def test_click_after_timeout_does_not_double_reply(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        await view.on_timeout()
        interaction = _interaction("111", message=FakeMessage())
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        assert len(stub.calls) == 1
        assert stub.calls[0][2] == "reject"

    @pytest.mark.asyncio
    async def test_parallel_requests_resolve_independently(self):
        bridge, channel, stub = _make_bridge()
        request_a = parse_permission_event(_permission_event(permission_id="p-a"))
        request_b = parse_permission_event(_permission_event(permission_id="p-b"))
        await bridge._post_prompt(request_a)
        await bridge._post_prompt(request_b)
        await _drain()

        assert len(channel.sent) == 2
        view_a, view_b = channel.sent[0]["view"], channel.sent[1]["view"]
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        interaction_a = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        await _get_view_class().accept(view_a, interaction_a, None)
        interaction_b = _interaction("111", message=FakeMessage(embeds=[channel.sent[1]["embed"]]))
        await _get_view_class().reject(view_b, interaction_b, None)
        await _drain()

        assert stub.calls == [
            (request_a.session_id, "p-a", "once"),
            (request_b.session_id, "p-b", "reject"),
        ]
        assert all(child.disabled for child in view_a.children)
        assert all(child.disabled for child in view_b.children)

    @pytest.mark.asyncio
    async def test_resolved_elsewhere_is_annotated(self):
        bridge, channel, stub = _make_bridge(delivered=False, status=404)
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await _drain()

        view = channel.sent[0]["view"]
        interaction = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        assert stub.calls == [(request.session_id, request.permission_id, "once")]
        edit_kwargs = interaction.response.calls[0][1]
        assert "another OpenCode client" in edit_kwargs["embed"].footer.text

    @pytest.mark.asyncio
    async def test_redelivered_event_posts_only_one_prompt(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_permission_event())
        await bridge._post_prompt(request)
        await bridge._post_prompt(parse_permission_event(_permission_event()))
        await _drain()

        assert len(channel.sent) == 1

    @pytest.mark.asyncio
    async def test_prompt_content_is_self_contained(self):
        bridge, channel, stub = _make_bridge()
        await bridge._post_prompt(parse_permission_event(_permission_event()))
        await _drain()

        sent = channel.sent[0]
        # The command must be visible in plain content, not only in the
        # embed (some Discord clients hide embeds), matching the
        # exec-approval prompt contract.
        assert "rm -rf build/" in sent["content"]
        assert sent["view"] is not None
