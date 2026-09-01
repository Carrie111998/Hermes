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
import os
import time
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
    def __init__(self, embeds=None, content=""):
        self.embeds = embeds or []
        self.content = content
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
        return FakeMessage(embeds=[kwargs["embed"]] if kwargs.get("embed") else [], content=kwargs.get("content", ""))


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
        self.apis = []
        self._result = (delivered, status)

    async def reply(self, session_id, permission_id, response, api="legacy"):
        self.calls.append((session_id, permission_id, response))
        self.apis.append(api)
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

    async def send_modal(self, modal):
        self.calls.append(("modal", modal))


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


# ---------------------------------------------------------------------------
# OpenCode >= 1.18: permission.asked / v2 reply endpoint
# ---------------------------------------------------------------------------


def _asked_event(permission_id="req-1", session_id="ses-abc", **overrides):
    props = {
        "id": permission_id,
        "sessionID": session_id,
        "permission": "bash",
        "patterns": ["cat ~/foo"],
        "always": ["cat *"],
        "metadata": {"command": "cat ~/foo"},
    }
    props.update(overrides)
    return {"type": "permission.asked", "properties": props}


class TestPermissionAskedEvent:
    def test_asked_event_parses_as_v2(self):
        request = parse_permission_event(_asked_event())
        assert request is not None
        assert request.reply_api == "v2"
        assert request.kind == "bash"
        assert request.title == "cat ~/foo"
        assert request.pattern == "cat ~/foo"
        assert request.is_guard is False

    def test_asked_event_without_command_uses_patterns(self):
        request = parse_permission_event(_asked_event(metadata={}, patterns=["a", "b"]))
        assert request.title == "a, b"

    @pytest.mark.asyncio
    async def test_v2_reply_uses_permission_endpoint(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(204)

        client = OpenCodeBridgeClient("http://127.0.0.1:4096", transport=httpx.MockTransport(handler))
        delivered, status = await client.reply("ses-1", "req-1", "once", "v2")
        await client.aclose()
        assert delivered is True
        assert status == 204
        assert seen["path"] == "/permission/req-1/reply"
        assert seen["body"] == {"reply": "once"}

    @pytest.mark.asyncio
    async def test_bridge_routes_v2_request_to_v2_api(self):
        bridge, channel, stub = _make_bridge()
        request = parse_permission_event(_asked_event())
        await bridge._post_prompt(request)
        outcome = await bridge.resolve(request, "once", "discord")
        assert outcome == "delivered"
        assert stub.apis == ["v2"]


# ---------------------------------------------------------------------------
# Command-guard spool (befehlswaechter.js / Claude Code hook counterpart)
# ---------------------------------------------------------------------------

from plugins.platforms.discord.opencode_bridge import (  # noqa: E402
    GUARD_DECISION_TTL_SECONDS,
    GuardSpool,
    parse_guard_request,
)

NOW = 1_800_000_000.0


def _guard_payload(**overrides):
    payload = {
        "version": 1,
        "id": "abcdef12-3456",
        "agent": "opencode",
        "created_at": NOW,
        "expires_at": NOW + 300,
        "session_id": "ses-guard-1",
        "project": "/Users/me/Projekt",
        "command": "cat ~/Notizen/todo.md",
        "path": "/Users/me/Notizen/todo.md",
        "access": "lesen",
    }
    payload.update(overrides)
    return payload


class TestGuardRequestParsing:
    def test_valid_request_parses(self):
        request = parse_guard_request(_guard_payload(), now=NOW)
        assert request is not None
        assert request.is_guard is True
        assert request.permission_id == "abcdef12-3456"
        assert request.session_id == "ses-guard-1"
        assert request.command == "cat ~/Notizen/todo.md"
        assert request.path == "/Users/me/Notizen/todo.md"
        assert request.project == "/Users/me/Projekt"
        assert request.access == "lesen"
        assert request.agent == "opencode"
        assert request.expires_at == NOW + 300

    def test_missing_session_becomes_dash(self):
        payload = _guard_payload()
        del payload["session_id"]
        assert parse_guard_request(payload, now=NOW).session_id == "-"

    @pytest.mark.parametrize("overrides", [
        {"version": 2},
        {"id": "short"},
        {"id": "has space in it"},
        {"id": "../../etc/passwd"},
        {"agent": "unknown-agent"},
        {"command": ""},
        {"command": 42},
        {"path": ""},
        {"access": "alles"},
        {"expires_at": NOW - 1},
        {"expires_at": NOW + 7200},
        {"expires_at": NOW - 10, "created_at": NOW - 20},
        {"created_at": True, "expires_at": True},
        {"created_at": "now"},
    ])
    def test_unclear_requests_are_refused(self, overrides):
        assert parse_guard_request(_guard_payload(**overrides), now=NOW) is None

    def test_non_dict_refused(self):
        assert parse_guard_request("nope", now=NOW) is None
        assert parse_guard_request(None, now=NOW) is None


def _write_request(spool, payload, name=None):
    spool.ensure()
    target = spool.requests_dir / f"{name or payload['id']}.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


class TestGuardSpool:
    def test_scan_returns_valid_request(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        _write_request(spool, _guard_payload())
        found = spool.scan(now=NOW)
        assert [r.permission_id for r in found] == ["abcdef12-3456"]

    def test_scan_ignores_malformed_and_logs_once(self, tmp_path, caplog):
        spool = GuardSpool(str(tmp_path))
        _write_request(spool, _guard_payload(access="alles"))
        with caplog.at_level("WARNING"):
            assert spool.scan(now=NOW) == []
            assert spool.scan(now=NOW) == []
        assert sum("malformed guard request" in r.message for r in caplog.records) == 1
        # the file stays (the guard removes it after its own timeout)
        assert (spool.requests_dir / "abcdef12-3456.json").exists()

    def test_scan_ignores_id_mismatch(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        _write_request(spool, _guard_payload(), name="zzzzzzzz-0000")
        assert spool.scan(now=NOW) == []

    def test_scan_drops_expired_file(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        target = _write_request(spool, _guard_payload(expires_at=NOW - 5, created_at=NOW - 60))
        assert spool.scan(now=NOW) == []
        assert not target.exists()

    def test_scan_skips_non_json_and_bad_names(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.ensure()
        (spool.requests_dir / ".tmp-abcdef12-3456").write_text("{}", encoding="utf-8")
        (spool.requests_dir / "bad name.json").write_text(json.dumps(_guard_payload()), encoding="utf-8")
        (spool.requests_dir / "notes.txt").write_text("hi", encoding="utf-8")
        assert spool.scan(now=NOW) == []

    def test_scan_ignores_symlinked_request(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.ensure()
        real = tmp_path / "outside.json"
        real.write_text(json.dumps(_guard_payload()), encoding="utf-8")
        (spool.requests_dir / "abcdef12-3456.json").symlink_to(real)
        assert spool.scan(now=NOW) == []

    def test_scan_skips_already_decided(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        _write_request(spool, _guard_payload())
        spool.write_decision("abcdef12-3456", "once", "discord", now=NOW)
        assert spool.scan(now=NOW) == []

    def test_write_decision_is_json_with_contract(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.write_decision("abcdef12-3456", "once", "discord", now=NOW)
        decision = json.loads((spool.decisions_dir / "abcdef12-3456.json").read_text())
        assert decision == {
            "version": 1,
            "id": "abcdef12-3456",
            "decision": "once",
            "source": "discord",
            "decided_at": NOW,
        }
        assert not any(n.startswith(".tmp-") for n in os.listdir(spool.decisions_dir))

    def test_write_decision_never_emits_always(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.write_decision("abcdef12-3456", "always", "discord", now=NOW)
        decision = json.loads((spool.decisions_dir / "abcdef12-3456.json").read_text())
        assert decision["decision"] == "reject"

    def test_write_decision_refuses_bad_id(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        with pytest.raises(ValueError):
            spool.write_decision("../escape", "once", "discord")

    def test_sweep_removes_orphaned_decisions(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.write_decision("abcdef12-3456", "reject", "timeout", now=NOW)
        target = spool.decisions_dir / "abcdef12-3456.json"
        old = time.time() - GUARD_DECISION_TTL_SECONDS - 10
        os.utime(target, (old, old))
        spool.sweep()
        assert not target.exists()

    def test_sweep_keeps_fresh_decisions(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.write_decision("abcdef12-3456", "reject", "timeout")
        spool.sweep()
        assert (spool.decisions_dir / "abcdef12-3456.json").exists()


def _make_guard_bridge(tmp_path):
    channel = FakeChannel()
    stub = StubBridgeClient()
    config = _bridge_config(guard_dir=str(tmp_path))
    assert config.guard_enabled and config.guard_dir == str(tmp_path)
    bridge = OpenCodeBridge(FakeAdapter(channel), config, client=stub)
    return bridge, channel, stub


def _read_decision(bridge, request_id):
    path = bridge.spool.decisions_dir / f"{request_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class TestGuardPromptFlow:
    @pytest.mark.asyncio
    async def test_request_file_becomes_german_prompt(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        posted = await bridge.poll_guard_spool_once()
        await _drain()

        assert posted == 1
        sent = channel.sent[0]
        content = sent["content"]
        assert "Befehlswächter" in content
        assert "cat ~/Notizen/todo.md" in content
        assert "/Users/me/Notizen/todo.md" in content
        assert "/Users/me/Projekt" in content
        assert "lesen" in content
        assert "OpenCode" in content
        labels = [child.label for child in sent["view"].children]
        assert labels == ["Einmal erlauben", "Ablehnen"]
        assert sent["view"].timeout <= 300

    @pytest.mark.asyncio
    async def test_second_poll_does_not_repost(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        await bridge.poll_guard_spool_once()
        assert len(channel.sent) == 1

    @pytest.mark.asyncio
    async def test_accept_writes_once_decision_and_never_calls_api(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        interaction = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        decision = _read_decision(bridge, "abcdef12-3456")
        assert decision["decision"] == "once"
        assert decision["source"] == "discord"
        assert stub.calls == []
        assert all(child.disabled for child in view.children)
        assert interaction.response.calls[0][1]["embed"].footer.text == "Einmal erlaubt"

    @pytest.mark.asyncio
    async def test_reject_writes_reject_decision(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        interaction = _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]]))
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().reject(view, interaction, None)
        await _drain()

        assert _read_decision(bridge, "abcdef12-3456")["decision"] == "reject"
        assert interaction.response.calls[0][1]["embed"].footer.text == "Abgelehnt"

    @pytest.mark.asyncio
    async def test_timeout_writes_reject(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        await view.on_timeout()
        await _drain()

        decision = _read_decision(bridge, "abcdef12-3456")
        assert decision["decision"] == "reject"
        assert decision["source"] == "timeout"

    @pytest.mark.asyncio
    async def test_unauthorized_click_writes_nothing(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        interaction = _interaction("999", message=FakeMessage())
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().accept(view, interaction, None)
        await _drain()

        assert _read_decision(bridge, "abcdef12-3456") is None
        assert interaction.response.calls[0][0] == "send"

    @pytest.mark.asyncio
    async def test_double_click_writes_exactly_one_decision(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(expires_at=time.time() + 300, created_at=time.time()))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        embeds = [channel.sent[0]["embed"]]
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        await _get_view_class().reject(view, _interaction("111", message=FakeMessage(embeds=embeds)), None)
        second = _interaction("111", message=FakeMessage(embeds=embeds))
        await _get_view_class().accept(view, second, None)
        await _drain()

        assert _read_decision(bridge, "abcdef12-3456")["decision"] == "reject"
        assert second.response.calls[0][0] == "send"

    @pytest.mark.asyncio
    async def test_malformed_request_is_never_answered(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _guard_payload(access="alles", expires_at=time.time() + 300, created_at=time.time()))
        posted = await bridge.poll_guard_spool_once()
        assert posted == 0
        assert channel.sent == []
        assert _read_decision(bridge, "abcdef12-3456") is None

    @pytest.mark.asyncio
    async def test_parallel_guard_requests_are_independent(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        now = time.time()
        _write_request(bridge.spool, _guard_payload(id="aaaaaaaa-0001", expires_at=now + 300, created_at=now))
        _write_request(bridge.spool, _guard_payload(id="bbbbbbbb-0002", expires_at=now + 300, created_at=now, access="schreiben"))
        await bridge.poll_guard_spool_once()
        assert len(channel.sent) == 2
        from plugins.platforms.discord.opencode_bridge import _get_view_class
        views = {sent["view"]._request.permission_id: sent["view"] for sent in channel.sent}
        await _get_view_class().accept(views["aaaaaaaa-0001"], _interaction("111", message=FakeMessage(embeds=[channel.sent[0]["embed"]])), None)
        await _get_view_class().reject(views["bbbbbbbb-0002"], _interaction("111", message=FakeMessage(embeds=[channel.sent[1]["embed"]])), None)
        await _drain()
        assert _read_decision(bridge, "aaaaaaaa-0001")["decision"] == "once"
        assert _read_decision(bridge, "bbbbbbbb-0002")["decision"] == "reject"

    @pytest.mark.asyncio
    async def test_guard_disabled_config_has_no_spool(self, tmp_path):
        channel = FakeChannel()
        config = _bridge_config(guard_dir=str(tmp_path), guard_enabled=False)
        bridge = OpenCodeBridge(FakeAdapter(channel), config, client=StubBridgeClient())
        assert bridge.spool is None


# ---------------------------------------------------------------------------
# Mentions, permission requests from Claude Code, question requests
# ---------------------------------------------------------------------------

from plugins.platforms.discord.opencode_bridge import _get_question_classes  # noqa: E402


def _fresh(**overrides):
    now = time.time()
    return _guard_payload(expires_at=now + 300, created_at=now, **overrides)


def _question_payload(**overrides):
    payload = _fresh(
        id="frage000-0001",
        agent="claude-code",
        kind="question",
        questions=[
            {
                "question": "Welche Farbe?",
                "header": "Farbe",
                "options": [
                    {"label": "Rot", "description": "warm"},
                    {"label": "Blau", "description": "kalt"},
                ],
                "multiSelect": False,
            },
            {
                "question": "Welche Kapitel?",
                "header": "Kapitel",
                "options": [{"label": "1"}, {"label": "2"}, {"label": "3"}],
                "multiSelect": True,
            },
        ],
    )
    for key in ("command", "path", "access"):
        payload.pop(key, None)
    payload.update(overrides)
    return payload


class TestMentionsAndToolPermissions:
    @pytest.mark.asyncio
    async def test_guard_prompt_mentions_allowlisted_user(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _fresh())
        await bridge.poll_guard_spool_once()
        sent = channel.sent[0]
        assert sent["content"].startswith("<@111>")
        assert sent["allowed_mentions"] is not None

    def test_permission_request_with_tool_and_details(self):
        request = parse_guard_request(
            _fresh(agent="claude-code", tool="Edit", details="{\"file_path\": \"x\"}", path="-", access="schreiben"),
            now=time.time(),
        )
        assert request is not None
        assert request.tool == "Edit"
        assert request.details.startswith("{")
        assert request.is_question is False

    @pytest.mark.asyncio
    async def test_tool_permission_prompt_shows_tool_and_hides_dash_path(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _fresh(agent="claude-code", tool="WebFetch", details="https://example.org", path="-", access="netz", command="WebFetch https://example.org"))
        await bridge.poll_guard_spool_once()
        content = channel.sent[0]["content"]
        assert "Claude Code bittet um Erlaubnis: WebFetch" in content
        assert "https://example.org" in content
        assert "**Pfad:**" not in content
        assert "netz" in content

    def test_unknown_access_kind_still_refused(self):
        assert parse_guard_request(_fresh(access="egal"), now=time.time()) is None


class TestQuestionParsing:
    def test_valid_question_request(self):
        request = parse_guard_request(_question_payload(), now=time.time())
        assert request is not None
        assert request.is_question is True
        assert len(request.questions) == 2
        assert request.questions[0]["options"][1]["label"] == "Blau"
        assert request.questions[1]["multiSelect"] is True
        assert request.command == "Welche Farbe?; Welche Kapitel?"

    @pytest.mark.parametrize("questions", [
        [],
        "nope",
        [{"question": "", "options": [{"label": "a"}]}],
        [{"question": "q", "options": []}],
        [{"question": "q", "options": [{"label": ""}]}],
        [{"question": "q", "options": [{"label": "a"}, {"label": "a"}]}],
        [{"question": "q", "options": [{"label": "a"}]}] * 5,
        [{"question": "q", "options": [{"label": str(i)} for i in range(9)]}],
        [{"question": "q", "options": [{"label": "a"}]}, {"question": "q", "options": [{"label": "b"}]}],
    ])
    def test_unclear_questions_refused(self, questions):
        assert parse_guard_request(_question_payload(questions=questions), now=time.time()) is None

    def test_unknown_kind_refused(self):
        assert parse_guard_request(_fresh(kind="wunsch"), now=time.time()) is None


def _click(view, label):
    for child in view.children:
        if getattr(child, "label", None) == label:
            return child.callback
    raise AssertionError(f"no button {label!r} in {[c.label for c in view.children]}")


class TestQuestionFlow:
    @pytest.mark.asyncio
    async def test_questions_become_one_message_each(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload())
        posted = await bridge.poll_guard_spool_once()
        assert posted == 1
        assert len(channel.sent) == 2
        first, second = channel.sent
        assert first["content"].startswith("<@111>")
        assert "Welche Farbe?" in first["content"] and "**Rot** — warm" in first["content"]
        assert [c.label for c in first["view"].children] == ["Rot", "Blau", "Andere Antwort…"]
        assert [c.label for c in second["view"].children] == ["1", "2", "3", "Andere Antwort…", "Fertig"]
        # a second poll must not repost
        await bridge.poll_guard_spool_once()
        assert len(channel.sent) == 2

    @pytest.mark.asyncio
    async def test_answers_are_written_once_all_questions_answered(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload())
        await bridge.poll_guard_spool_once()
        v1, v2 = channel.sent[0]["view"], channel.sent[1]["view"]

        i1 = _interaction("111", message=FakeMessage(content="f1"))
        await _click(v1, "Blau")(i1)
        assert _read_decision(bridge, "frage000-0001") is None
        assert all(c.disabled for c in v1.children)
        assert "Antwort gespeichert" in i1.response.calls[0][1]["content"]

        i2 = _interaction("111", message=FakeMessage(content="f2"))
        await _click(v2, "1")(i2)
        await _click(v2, "3")(_interaction("111", message=FakeMessage(content="f2")))
        assert _read_decision(bridge, "frage000-0001") is None
        i3 = _interaction("111", message=FakeMessage(content="f2"))
        await _click(v2, "Fertig")(i3)
        decision = _read_decision(bridge, "frage000-0001")
        assert decision["decision"] == "answer"
        assert decision["answers"] == {"Welche Farbe?": "Blau", "Welche Kapitel?": ["1", "3"]}
        assert stub.calls == []
        assert "Antwort übermittelt" in i3.response.calls[0][1]["content"]

    @pytest.mark.asyncio
    async def test_multiselect_toggle_and_empty_done(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload())
        await bridge.poll_guard_spool_once()
        v2 = channel.sent[1]["view"]
        await _click(v2, "2")(_interaction("111", message=FakeMessage()))
        await _click(v2, "2")(_interaction("111", message=FakeMessage()))  # abwählen
        i = _interaction("111", message=FakeMessage())
        await _click(v2, "Fertig")(i)
        assert i.response.calls[0][0] == "send"  # Hinweis: erst auswählen
        assert not v2._done

    @pytest.mark.asyncio
    async def test_free_text_via_modal(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload(questions=[{
            "question": "Wie heißt die Datei?", "options": [{"label": "a.typ"}], "multiSelect": False,
        }]))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        i = _interaction("111", message=FakeMessage())
        await _click(view, "Andere Antwort…")(i)
        assert i.response.calls[0][0] == "modal"
        modal = i.response.calls[0][1]
        modal.text._value = "  b.typ  "
        i2 = _interaction("111", message=FakeMessage(content="frage"))
        await modal.on_submit(i2)
        assert _read_decision(bridge, "frage000-0001")["answers"] == {"Wie heißt die Datei?": "b.typ"}

    @pytest.mark.asyncio
    async def test_timeout_of_one_question_rejects_all(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload())
        await bridge.poll_guard_spool_once()
        v1, v2 = channel.sent[0]["view"], channel.sent[1]["view"]
        await _click(v1, "Rot")(_interaction("111", message=FakeMessage()))
        await v2.on_timeout()
        decision = _read_decision(bridge, "frage000-0001")
        assert decision["decision"] == "reject" and decision["source"] == "timeout"
        assert "answers" not in decision

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_answer_questions(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload())
        await bridge.poll_guard_spool_once()
        v1 = channel.sent[0]["view"]
        i = _interaction("999", message=FakeMessage())
        await _click(v1, "Rot")(i)
        assert i.response.calls[0][0] == "send"
        assert not v1._done
        assert _read_decision(bridge, "frage000-0001") is None

    @pytest.mark.asyncio
    async def test_second_answer_to_same_question_is_ignored(self, tmp_path):
        bridge, channel, stub = _make_guard_bridge(tmp_path)
        _write_request(bridge.spool, _question_payload(questions=[{
            "question": "Nur eine", "options": [{"label": "A"}, {"label": "B"}],
        }]))
        await bridge.poll_guard_spool_once()
        view = channel.sent[0]["view"]
        await _click(view, "A")(_interaction("111", message=FakeMessage()))
        late = _interaction("111", message=FakeMessage())
        await _click(view, "B")(late)
        assert late.response.calls[0][0] == "send"
        assert _read_decision(bridge, "frage000-0001")["answers"] == {"Nur eine": "A"}

    def test_write_decision_answer_without_answers_becomes_reject(self, tmp_path):
        spool = GuardSpool(str(tmp_path))
        spool.write_decision("frage000-0001", "answer", "discord", now=NOW)
        assert json.loads((spool.decisions_dir / "frage000-0001.json").read_text())["decision"] == "reject"
