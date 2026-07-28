"""Tests for the bundled Fluxer messaging platform adapter."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType


def _make_adapter(**extra):
    from plugins.platforms.fluxer.adapter import FluxerAdapter

    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "api_url": "https://api.example.test/v1",
            "gateway_url": "wss://gateway.example.test",
            **extra,
        },
    )
    return FluxerAdapter(config)


class TestFluxerPluginRegistration:
    def test_dynamic_platform_member_is_discoverable(self):
        assert Platform("fluxer").value == "fluxer"

    def test_register_exposes_full_platform_integration(self):
        from plugins.platforms.fluxer.adapter import register

        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "fluxer"
        assert kwargs["required_env"] == ["FLUXER_BOT_TOKEN"]
        assert kwargs["allowed_users_env"] == "FLUXER_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "FLUXER_ALLOW_ALL_USERS"
        assert kwargs["cron_deliver_env_var"] == "FLUXER_HOME_CHANNEL"
        assert kwargs["max_message_length"] == 4000
        assert callable(kwargs["env_enablement_fn"])
        assert callable(kwargs["standalone_sender_fn"])

    def test_env_enablement_seeds_token_urls_and_home(self, monkeypatch):
        from plugins.platforms.fluxer.adapter import _env_enablement

        monkeypatch.setenv("FLUXER_BOT_TOKEN", "secret")
        monkeypatch.setenv("FLUXER_API_URL", "https://self.example/v1/")
        monkeypatch.setenv("FLUXER_GATEWAY_URL", "wss://gw.self.example/")
        monkeypatch.setenv("FLUXER_HOME_CHANNEL", "123")
        monkeypatch.setenv("FLUXER_HOME_CHANNEL_NAME", "Ops")

        seed = _env_enablement()
        assert seed == {
            "token": "secret",
            "api_url": "https://self.example/v1",
            "gateway_url": "wss://gw.self.example/",
            "home_channel": {"chat_id": "123", "name": "Ops"},
        }

    def test_env_enablement_requires_token(self, monkeypatch):
        from plugins.platforms.fluxer.adapter import _env_enablement

        monkeypatch.delenv("FLUXER_BOT_TOKEN", raising=False)
        assert _env_enablement() is None

    def test_env_enablement_does_not_override_yaml_api_url_with_default(
        self, monkeypatch
    ):
        from plugins.platforms.fluxer.adapter import _env_enablement

        monkeypatch.setenv("FLUXER_BOT_TOKEN", "secret")
        monkeypatch.delenv("FLUXER_API_URL", raising=False)

        seed = _env_enablement()

        assert seed is not None
        assert seed["token"] == "secret"
        assert "api_url" not in seed


class TestFluxerConfiguration:
    def test_defaults_to_production_endpoints(self):
        from plugins.platforms.fluxer.adapter import FluxerAdapter

        adapter = FluxerAdapter(PlatformConfig(enabled=True, token="tok", extra={}))
        assert adapter._api_url == "https://api.fluxer.app/v1"
        assert adapter._gateway_url == ""

    def test_bot_authorization_header_uses_bot_scheme(self):
        adapter = _make_adapter()
        assert adapter._headers()["Authorization"] == "Bot test-token"

    def test_insecure_remote_api_and_gateway_urls_are_rejected(self):
        with pytest.raises(ValueError, match="HTTPS"):
            _make_adapter(api_url="http://api.attacker.invalid/v1")

        with pytest.raises(ValueError, match="WSS"):
            _make_adapter(gateway_url="ws://gateway.attacker.invalid/")

    @pytest.mark.parametrize(
        ("field", "url"),
        [
            ("api_url", "https://:443/v1"),
            ("api_url", "https://example.com:bad/v1"),
            ("api_url", "https://../v1"),
            ("gateway_url", "wss://:443/gateway"),
            ("gateway_url", "wss://example.com:bad/gateway"),
            ("gateway_url", "wss://../gateway"),
        ],
    )
    def test_malformed_token_bearing_endpoints_are_rejected(self, field, url):
        with pytest.raises(ValueError, match="valid hostname and port"):
            _make_adapter(**{field: url})

    def test_loopback_http_and_websocket_are_allowed_for_local_development(self):
        adapter = _make_adapter(
            api_url="http://127.0.0.1:9000/v1",
            gateway_url="ws://localhost:9001/gateway",
        )

        assert adapter._api_url == "http://127.0.0.1:9000/v1"
        assert adapter._gateway_url == "ws://localhost:9001/gateway"

    def test_validate_requires_token_and_https_api(self):
        from plugins.platforms.fluxer.adapter import validate_fluxer_config

        assert validate_fluxer_config(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={"api_url": "https://api.fluxer.app/v1"},
            )
        )
        assert not validate_fluxer_config(
            PlatformConfig(
                enabled=True, token="", extra={"api_url": "https://api.fluxer.app/v1"}
            )
        )
        assert not validate_fluxer_config(
            PlatformConfig(
                enabled=True, token="tok", extra={"api_url": "file:///tmp/nope"}
            )
        )

    def test_validate_rejects_insecure_gateway_from_environment(self, monkeypatch):
        from plugins.platforms.fluxer.adapter import validate_fluxer_config

        monkeypatch.setenv("FLUXER_GATEWAY_URL", "ws://gateway.attacker.invalid/")
        config = PlatformConfig(
            enabled=True,
            token="tok",
            extra={"api_url": "https://api.fluxer.app/v1"},
        )

        assert not validate_fluxer_config(config)


class TestFluxerOutbound:
    def test_bounded_file_reader_rejects_growth_past_limit(self, tmp_path):
        from plugins.platforms.fluxer.adapter import (
            _UploadTooLarge,
            _read_file_bounded,
        )

        path = tmp_path / "grew-during-read.bin"
        path.write_bytes(b"12345")

        with pytest.raises(_UploadTooLarge):
            _read_file_bounded(path, 4)

    @pytest.mark.asyncio
    async def test_standalone_send_rejects_oversized_media(self, monkeypatch, tmp_path):
        import aiohttp
        from plugins.platforms.fluxer.adapter import FluxerAdapter, _standalone_send

        path = tmp_path / "large.bin"
        path.write_bytes(b"12345")
        fake_session = MagicMock()
        fake_session.close = AsyncMock()
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: fake_session)
        send_files = AsyncMock()
        monkeypatch.setattr(FluxerAdapter, "_send_files", send_files)

        result = await _standalone_send(
            PlatformConfig(
                enabled=True,
                token="test-token",
                extra={
                    "api_url": "https://api.example.test/v1",
                    "max_upload_bytes": 4,
                },
            ),
            "chan",
            "caption",
            media_files=[str(path)],
        )

        assert "upload limit" in result["error"]
        send_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_posts_safe_mentions_and_reply_reference(self):
        adapter = _make_adapter()
        adapter._api = AsyncMock(return_value={"id": "msg-2"})

        result = await adapter.send("chan-1", "hello @everyone", reply_to="msg-1")

        assert result.success is True
        assert result.message_id == "msg-2"
        method, path = adapter._api.call_args.args[:2]
        payload = adapter._api.call_args.kwargs["json"]
        assert (method, path) == ("POST", "channels/chan-1/messages")
        assert payload["content"] == "hello @everyone"
        assert payload["allowed_mentions"] == {"parse": [], "replied_user": False}
        assert payload["message_reference"] == {
            "message_id": "msg-1",
            "channel_id": "chan-1",
            "type": 0,
        }
        assert isinstance(payload["nonce"], str) and payload["nonce"]

    @pytest.mark.asyncio
    async def test_send_chunks_at_fluxer_limit(self):
        adapter = _make_adapter()
        adapter._api = AsyncMock(side_effect=[{"id": "one"}, {"id": "two"}])

        result = await adapter.send("chan", "x" * 5000)

        assert result.success is True
        assert result.message_id == "two"
        assert result.continuation_message_ids == ("one",)
        assert adapter._api.await_count == 2
        for call in adapter._api.await_args_list:
            assert len(call.kwargs["json"]["content"]) <= 4000

    @pytest.mark.asyncio
    async def test_send_maps_rate_limit_to_retryable_result(self):
        adapter = _make_adapter()
        adapter._api = AsyncMock(return_value=None)
        adapter._last_http_status = 429
        adapter._last_http_error = "rate limited"
        adapter._last_retry_after = 2.5

        result = await adapter.send("chan", "hello")

        assert result.success is False
        assert result.error_kind == "rate_limited"
        assert result.retryable is True
        assert result.retry_after == 2.5

    @pytest.mark.asyncio
    async def test_multipart_rate_limit_honors_retry_after_header(self):
        adapter = _make_adapter()
        response = MagicMock()
        response.status = 429
        response.headers = {"Retry-After": "3.5"}
        response.text = AsyncMock(return_value="{}")
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        adapter._session = MagicMock()
        adapter._session.request.return_value = context

        result = await adapter._api_multipart(
            "POST",
            "channels/chan/messages",
            payload={"content": "x"},
            files=[("x.txt", b"x", "text/plain")],
        )

        assert result is None
        assert adapter._last_retry_after == 3.5

    @pytest.mark.asyncio
    async def test_typing_and_edit_use_fluxer_rest_routes(self):
        adapter = _make_adapter()
        adapter._api = AsyncMock(side_effect=[{}, {"id": "edited"}])

        await adapter.send_typing("chan")
        result = await adapter.edit_message("chan", "msg", "new text")

        assert result.success is True
        assert adapter._api.await_args_list[0].args[:2] == (
            "POST",
            "channels/chan/typing",
        )
        assert adapter._api.await_args_list[1].args[:2] == (
            "PATCH",
            "channels/chan/messages/msg",
        )
        assert adapter._api.await_args_list[1].kwargs["json"]["allowed_mentions"] == {
            "parse": [],
            "replied_user": False,
        }

    @pytest.mark.asyncio
    async def test_local_file_uses_fluxer_multipart_shape(self, tmp_path):
        adapter = _make_adapter()
        adapter._api_multipart = AsyncMock(return_value={"id": "file-msg"})
        path = tmp_path / "report.txt"
        path.write_text("report body")

        result = await adapter.send_document("chan", str(path), caption="Report")

        assert result.success is True
        assert result.message_id == "file-msg"
        payload = adapter._api_multipart.call_args.kwargs["payload"]
        files = adapter._api_multipart.call_args.kwargs["files"]
        assert payload["content"] == "Report"
        assert payload["attachments"][0]["id"] == 0
        assert payload["attachments"][0]["filename"] == "report.txt"
        assert files == [("report.txt", b"report body", "text/plain")]

    @pytest.mark.asyncio
    async def test_multipart_caption_is_capped_at_fluxer_limit(self):
        adapter = _make_adapter()
        adapter._api_multipart = AsyncMock(return_value={"id": "file-msg"})

        result = await adapter._send_files(
            "chan", [("x.txt", b"x", "text/plain")], "c" * 5000, None
        )

        assert result.success is True
        payload = adapter._api_multipart.call_args.kwargs["payload"]
        assert len(payload["content"]) == 4000

    @pytest.mark.asyncio
    async def test_document_rejects_file_above_upload_limit(self, tmp_path):
        adapter = _make_adapter()
        adapter._max_upload_bytes = 4
        adapter._api_multipart = AsyncMock()
        path = tmp_path / "large.bin"
        path.write_bytes(b"12345")

        result = await adapter.send_document("chan", str(path))

        assert result.success is False
        assert result.error_kind == "file_too_large"
        adapter._api_multipart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_document_reads_file_off_event_loop(self, monkeypatch, tmp_path):
        adapter = _make_adapter()
        adapter._api_multipart = AsyncMock(return_value={"id": "file-msg"})
        path = tmp_path / "report.txt"
        path.write_text("report body")
        to_thread = AsyncMock(return_value=b"report body")
        monkeypatch.setattr(asyncio, "to_thread", to_thread)

        result = await adapter.send_document("chan", str(path))

        assert result.success is True
        to_thread.assert_awaited_once()
        assert to_thread.await_args is not None
        read_callable, read_path, read_limit = to_thread.await_args.args
        assert read_callable.__name__ == "_read_file_bounded"
        assert read_path == path
        assert read_limit == adapter._max_upload_bytes


class TestFluxerGatewayProtocol:
    @pytest.mark.asyncio
    async def test_gateway_socket_is_closed_before_reconnect(self):
        from plugins.platforms.fluxer.adapter import _ReconnectRequested

        adapter = _make_adapter()
        ws = MagicMock()
        ws.__aiter__.return_value = []
        ws.closed = False
        ws.close_code = 1000
        ws.close = AsyncMock()
        adapter._session = MagicMock()
        adapter._session.ws_connect = AsyncMock(return_value=ws)

        with pytest.raises(_ReconnectRequested):
            await adapter._gateway_once()

        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_sequence_close_clears_resume_state(self):
        from plugins.platforms.fluxer.adapter import _ReconnectRequested

        adapter = _make_adapter()
        adapter._session_id = "stale-session"
        adapter._sequence = 42
        ws = MagicMock()
        ws.__aiter__.return_value = []
        ws.closed = False
        ws.close_code = 4007
        ws.close = AsyncMock()
        adapter._session = MagicMock()
        adapter._session.ws_connect = AsyncMock(return_value=ws)

        with pytest.raises(_ReconnectRequested):
            await adapter._gateway_once()

        assert adapter._session_id == ""
        assert adapter._sequence == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("close_code", [4004, 4010, 4011, 4012])
    async def test_configuration_close_is_permanent(self, close_code):
        from plugins.platforms.fluxer.adapter import _PermanentGatewayError

        adapter = _make_adapter()
        ws = MagicMock()
        ws.__aiter__.return_value = []
        ws.closed = False
        ws.close_code = close_code
        ws.close = AsyncMock()
        adapter._session = MagicMock()
        adapter._session.ws_connect = AsyncMock(return_value=ws)

        with pytest.raises(_PermanentGatewayError):
            await adapter._gateway_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("close_code", [4003, 4005, 4013])
    async def test_recoverable_protocol_close_reconnects(self, close_code):
        from plugins.platforms.fluxer.adapter import _ReconnectRequested

        adapter = _make_adapter()
        ws = MagicMock()
        ws.__aiter__.return_value = []
        ws.closed = False
        ws.close_code = close_code
        ws.close = AsyncMock()
        adapter._session = MagicMock()
        adapter._session.ws_connect = AsyncMock(return_value=ws)

        with pytest.raises(_ReconnectRequested):
            await adapter._gateway_once()

    @pytest.mark.asyncio
    async def test_missing_heartbeat_ack_closes_socket(self, monkeypatch):
        adapter = _make_adapter()
        adapter._heartbeat_acknowledged = False
        ws = MagicMock()
        ws.closed = False

        async def close(*_args, **_kwargs):
            ws.closed = True

        ws.close = AsyncMock(side_effect=close)
        ws.send_json = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await adapter._heartbeat_loop(ws, 45.0)

        ws.close.assert_awaited_once()
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_ack_is_tracked(self):
        adapter = _make_adapter()
        adapter._heartbeat_acknowledged = False

        await adapter._handle_gateway_payload({"op": 11, "d": None}, AsyncMock())

        assert adapter._heartbeat_acknowledged is True

    @pytest.mark.asyncio
    async def test_server_reconnect_opcode_requests_immediate_retry(self):
        from plugins.platforms.fluxer.adapter import _ReconnectRequested

        adapter = _make_adapter()
        with pytest.raises(_ReconnectRequested) as raised:
            await adapter._handle_gateway_payload({"op": 7, "d": None}, AsyncMock())

        assert raised.value.retry_delay == 0.0

    @pytest.mark.asyncio
    async def test_invalid_session_requests_protocol_delay(self, monkeypatch):
        from plugins.platforms.fluxer.adapter import _ReconnectRequested

        adapter = _make_adapter()
        monkeypatch.setattr(
            "plugins.platforms.fluxer.adapter.random.uniform", lambda _a, _b: 3.0
        )
        with pytest.raises(_ReconnectRequested) as raised:
            await adapter._handle_gateway_payload({"op": 9, "d": False}, AsyncMock())

        assert raised.value.retry_delay == 3.0
        assert adapter._session_id == ""
        assert adapter._sequence == 0

    @pytest.mark.asyncio
    async def test_ready_reconnect_resets_backoff(self, monkeypatch):
        from plugins.platforms.fluxer.adapter import (
            _PermanentGatewayError,
            _ReconnectRequested,
        )

        adapter = _make_adapter()
        attempts = 0

        async def gateway_once():
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                adapter._gateway_was_ready = True
                raise _ReconnectRequested("test reconnect")
            raise _PermanentGatewayError("stop")

        adapter._gateway_once = gateway_once
        sleep = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep)
        monkeypatch.setattr(
            "plugins.platforms.fluxer.adapter.random.random", lambda: 0.0
        )

        await adapter._gateway_loop()

        assert [call.args[0] for call in sleep.await_args_list] == [2.0, 2.0]

    @pytest.mark.asyncio
    async def test_connect_cancellation_releases_session_and_token_lock(
        self, monkeypatch
    ):
        import aiohttp
        import gateway.status as status_mod

        adapter = _make_adapter()
        expected_lock_id = __import__("hashlib").sha256(b"test-token").hexdigest()[:24]
        fake_session = MagicMock()
        fake_session.closed = False
        fake_session.close = AsyncMock()
        release = MagicMock()
        monkeypatch.setattr(
            status_mod, "acquire_scoped_lock", lambda *_a, **_kw: (True, None)
        )
        monkeypatch.setattr(status_mod, "release_scoped_lock", release)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: fake_session)
        adapter._api = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await adapter.connect()

        fake_session.close.assert_awaited_once()
        release.assert_called_once_with("fluxer", expected_lock_id)

    @pytest.mark.asyncio
    async def test_connect_uses_base_platform_lock_helper(self, monkeypatch):
        import aiohttp
        import gateway.status as status_mod

        adapter = _make_adapter()
        expected_lock_id = __import__("hashlib").sha256(b"test-token").hexdigest()[:24]
        adapter._acquire_platform_lock = MagicMock(return_value=True)
        fake_session = MagicMock()
        fake_session.closed = False
        fake_session.close = AsyncMock()
        monkeypatch.setattr(
            status_mod, "acquire_scoped_lock", lambda *_a, **_kw: (True, None)
        )
        monkeypatch.setattr(status_mod, "release_scoped_lock", MagicMock())
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: fake_session)
        adapter._api = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await adapter.connect()

        adapter._acquire_platform_lock.assert_called_once_with(
            "fluxer", expected_lock_id, "Fluxer bot token"
        )

    @pytest.mark.asyncio
    async def test_hello_identifies_with_bot_token(self):
        adapter = _make_adapter()
        ws = AsyncMock()
        adapter._start_heartbeat = MagicMock()

        await adapter._handle_gateway_payload(
            {"op": 10, "d": {"heartbeat_interval": 45000}}, ws
        )

        adapter._start_heartbeat.assert_called_once_with(ws, 45000)
        identify = ws.send_json.await_args.args[0]
        assert identify["op"] == 2
        assert identify["d"]["token"] == "test-token"
        assert identify["d"]["properties"]["browser"] == "hermes-agent"
        assert "MESSAGE_UPDATE" in identify["d"]["ignored_events"]

    @pytest.mark.asyncio
    async def test_gateway_permanent_failure_marks_adapter_disconnected(self):
        from plugins.platforms.fluxer.adapter import _PermanentGatewayError

        adapter = _make_adapter()
        adapter._gateway_once = AsyncMock(
            side_effect=_PermanentGatewayError("bad token")
        )
        adapter._ready_event.set()
        adapter._mark_disconnected = MagicMock()
        adapter._notify_fatal_error = AsyncMock()

        await adapter._gateway_loop()

        adapter._mark_disconnected.assert_called_once()
        adapter._notify_fatal_error.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_preserves_permanent_gateway_failure(self, monkeypatch):
        import aiohttp
        from plugins.platforms.fluxer.adapter import _PermanentGatewayError

        adapter = _make_adapter()
        fake_session = MagicMock()
        fake_session.closed = False
        fake_session.close = AsyncMock()
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: fake_session)
        adapter._api = AsyncMock(return_value={"id": "bot-1", "username": "Hermes"})
        adapter._gateway_once = AsyncMock(
            side_effect=_PermanentGatewayError("bad gateway credential")
        )
        adapter._notify_fatal_error = AsyncMock()

        real_wait_for = asyncio.wait_for

        async def wait_for_gateway(awaitable, timeout):
            gateway_task = adapter._gateway_task
            assert gateway_task is not None
            await gateway_task
            if hasattr(awaitable, "close"):
                awaitable.close()
            if adapter._ready_event.is_set():
                return True
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", wait_for_gateway)
        try:
            assert await adapter.connect() is False
        finally:
            monkeypatch.setattr(asyncio, "wait_for", real_wait_for)

        assert adapter._fatal_error_code == "fluxer_gateway_auth"
        assert adapter._fatal_error_retryable is False
        adapter._notify_fatal_error.assert_not_awaited()
        fatal_message = adapter._fatal_error_message
        assert fatal_message is not None
        assert "bad gateway credential" in fatal_message

    @pytest.mark.asyncio
    async def test_hello_resumes_existing_session(self):
        adapter = _make_adapter()
        adapter._session_id = "session-1"
        adapter._sequence = 42
        ws = AsyncMock()
        adapter._start_heartbeat = MagicMock()

        await adapter._handle_gateway_payload(
            {"op": 10, "d": {"heartbeat_interval": 45000}}, ws
        )

        resume = ws.send_json.await_args.args[0]
        assert resume == {
            "op": 6,
            "d": {"token": "test-token", "session_id": "session-1", "seq": 42},
        }

    @pytest.mark.asyncio
    async def test_server_heartbeat_request_is_acknowledged_with_sequence(self):
        adapter = _make_adapter()
        adapter._sequence = 9
        ws = AsyncMock()

        await adapter._handle_gateway_payload({"op": 1, "d": None}, ws)

        ws.send_json.assert_awaited_once_with({"op": 1, "d": 9})

    @pytest.mark.asyncio
    async def test_ready_records_session_and_signals_connection(self):
        adapter = _make_adapter()
        adapter._ready_event = asyncio.Event()
        ws = AsyncMock()

        await adapter._handle_gateway_payload(
            {"op": 0, "s": 7, "t": "READY", "d": {"session_id": "sid"}}, ws
        )

        assert adapter._sequence == 7
        assert adapter._session_id == "sid"
        assert adapter._ready_event.is_set()

    @pytest.mark.asyncio
    async def test_ready_retains_valid_resume_gateway_url(self):
        adapter = _make_adapter()

        await adapter._handle_gateway_payload(
            {
                "op": 0,
                "s": 7,
                "t": "READY",
                "d": {
                    "session_id": "sid",
                    "resume_gateway_url": "wss://resume.example.test/socket",
                },
            },
            AsyncMock(),
        )

        assert adapter._resume_gateway_url == "wss://resume.example.test/socket"
        assert adapter._gateway_connect_url().startswith(
            "wss://resume.example.test/socket?"
        )

    @pytest.mark.asyncio
    async def test_ready_rejects_insecure_resume_gateway_url(self):
        adapter = _make_adapter()

        await adapter._handle_gateway_payload(
            {
                "op": 0,
                "s": 7,
                "t": "READY",
                "d": {
                    "session_id": "sid",
                    "resume_gateway_url": "ws://attacker.invalid/socket",
                },
            },
            AsyncMock(),
        )

        assert adapter._resume_gateway_url == ""

    @pytest.mark.asyncio
    async def test_ready_without_resume_url_clears_stale_route(self):
        adapter = _make_adapter()
        adapter._resume_gateway_url = "wss://stale.example.test/socket"

        await adapter._handle_gateway_payload(
            {"op": 0, "s": 8, "t": "READY", "d": {"session_id": "new-sid"}},
            AsyncMock(),
        )

        assert adapter._resume_gateway_url == ""

    @pytest.mark.asyncio
    async def test_message_create_dispatches_to_ingress(self):
        adapter = _make_adapter()
        adapter._handle_message_create = AsyncMock()
        ws = AsyncMock()
        payload = {"id": "m1"}

        await adapter._handle_gateway_payload(
            {"op": 0, "s": 8, "t": "MESSAGE_CREATE", "d": payload}, ws
        )

        adapter._handle_message_create.assert_awaited_once_with(payload)


class TestFluxerInbound:
    @pytest.mark.asyncio
    async def test_transient_channel_lookup_failure_is_not_cached_as_guild_channel(
        self,
    ):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._api = AsyncMock(
            side_effect=[None, {"id": "dm-1", "type": 1, "name": "Direct message"}]
        )
        adapter.handle_message = AsyncMock()

        base_message = {
            "channel_id": "dm-1",
            "type": 0,
            "content": "hello",
            "author": {"id": "user-1", "username": "Kait"},
            "attachments": [],
        }
        await adapter._handle_message_create({**base_message, "id": "msg-lookup-1"})
        await adapter._handle_message_create({**base_message, "id": "msg-lookup-2"})

        assert adapter._api.await_count == 2
        assert adapter.handle_message.await_count == 2
        assert adapter._channel_cache["dm-1"]["type"] == 1

    @pytest.mark.asyncio
    async def test_unauthorized_sender_is_rejected_before_attachment_download(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(return_value={"id": "dm-1", "type": 1})
        adapter._download_attachment = AsyncMock()
        adapter._authorization_check = lambda user_id, chat_type, chat_id: False
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": "msg-blocked",
            "channel_id": "dm-1",
            "type": 0,
            "content": "",
            "author": {"id": "attacker", "username": "Mallory"},
            "attachments": [
                {"url": "https://cdn.example.test/large.bin", "filename": "large.bin"}
            ],
        })

        adapter._download_attachment.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()
        call = adapter.handle_message.await_args
        assert call is not None
        event = call.args[0]
        assert event.source.user_id == "attacker"
        assert event.media_urls == []
        assert event.media_types == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authorization_state", ["missing", "raises"])
    async def test_indeterminate_authorization_never_downloads_attachment(
        self, authorization_state
    ):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(return_value={"id": "dm-1", "type": 1})
        adapter._download_attachment = AsyncMock()
        if authorization_state == "raises":

            def broken_check(*_args):
                raise RuntimeError("authorization backend unavailable")

            adapter._authorization_check = broken_check
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": f"msg-{authorization_state}",
            "channel_id": "dm-1",
            "type": 0,
            "content": "",
            "author": {"id": "unknown", "username": "Unknown"},
            "attachments": [
                {"url": "https://cdn.example.test/large.bin", "filename": "large.bin"}
            ],
        })

        adapter._download_attachment.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()
        call = adapter.handle_message.await_args
        assert call is not None
        event = call.args[0]
        assert event.source.user_id == "unknown"
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_dm_message_becomes_normalized_event(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(
            return_value={"id": "chan", "type": 1, "name": "DM"}
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": "m1",
            "channel_id": "chan",
            "content": "hello",
            "type": 0,
            "author": {"id": "user-1", "username": "kait", "bot": False},
            "attachments": [],
            "mentions": [],
        })

        call = adapter.handle_message.await_args
        assert call is not None
        event = call.args[0]
        assert event.text == "hello"
        assert event.message_type == MessageType.TEXT
        assert event.source.platform.value == "fluxer"
        assert event.source.chat_type == "dm"
        assert event.source.user_id == "user-1"
        assert event.message_id == "m1"

    @pytest.mark.asyncio
    async def test_guild_channel_requires_and_strips_bot_mention(self, monkeypatch):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(
            return_value={"id": "chan", "type": 0, "name": "general", "guild_id": "g1"}
        )
        adapter.handle_message = AsyncMock()
        base = {
            "channel_id": "chan",
            "type": 0,
            "author": {"id": "user-1", "username": "kait", "bot": False},
            "attachments": [],
        }

        await adapter._handle_message_create({
            **base,
            "id": "m1",
            "content": "ignored",
            "mentions": [],
        })
        adapter.handle_message.assert_not_awaited()

        await adapter._handle_message_create({
            **base,
            "id": "m2",
            "content": "<@bot-1> diagnose this",
            "mentions": [{"id": "bot-1"}],
        })
        call = adapter.handle_message.await_args
        assert call is not None
        event = call.args[0]
        assert event.text == "diagnose this"
        assert event.source.chat_type == "channel"
        assert event.source.guild_id == "g1"

    @pytest.mark.asyncio
    async def test_free_response_channel_bypasses_mention(self, monkeypatch):
        monkeypatch.setenv("FLUXER_FREE_RESPONSE_CHANNELS", "chan")
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(
            return_value={"id": "chan", "type": 0, "guild_id": "g1"}
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": "m1",
            "channel_id": "chan",
            "content": "hello room",
            "type": 0,
            "author": {"id": "user-1", "username": "kait"},
            "attachments": [],
            "mentions": [],
        })

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allowed_channel_whitelist_blocks_other_channels(self, monkeypatch):
        monkeypatch.setenv("FLUXER_ALLOWED_CHANNELS", "allowed")
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(
            return_value={"id": "blocked", "type": 0, "guild_id": "g1"}
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": "m1",
            "channel_id": "blocked",
            "content": "<@bot-1> hello",
            "type": 0,
            "author": {"id": "user-1", "username": "kait"},
            "attachments": [],
            "mentions": [{"id": "bot-1"}],
        })

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_own_messages_other_bots_and_duplicates(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._get_channel = AsyncMock(return_value={"id": "chan", "type": 1})
        adapter.handle_message = AsyncMock()
        base = {
            "channel_id": "chan",
            "content": "hello",
            "type": 0,
            "attachments": [],
            "mentions": [],
        }

        await adapter._handle_message_create({
            **base,
            "id": "own",
            "author": {"id": "bot-1"},
        })
        await adapter._handle_message_create({
            **base,
            "id": "other",
            "author": {"id": "bot-2", "bot": True},
        })
        duplicate = {**base, "id": "dup", "author": {"id": "user"}}
        await adapter._handle_message_create(duplicate)
        await adapter._handle_message_create(duplicate)

        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_attachment_is_cached_and_reply_context_is_preserved(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-1"
        adapter._authorization_check = lambda *_args: True
        adapter._get_channel = AsyncMock(return_value={"id": "chan", "type": 1})
        adapter._download_attachment = AsyncMock(
            return_value=("/tmp/image.png", "image/png")
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_message_create({
            "id": "m2",
            "channel_id": "chan",
            "content": "see this",
            "type": 19,
            "author": {"id": "user", "username": "kait"},
            "mentions": [],
            "attachments": [
                {
                    "url": "https://cdn.example/x",
                    "filename": "x.png",
                    "content_type": "image/png",
                }
            ],
            "message_reference": {"message_id": "m1", "channel_id": "chan"},
            "referenced_message": {
                "content": "previous",
                "author": {"id": "other", "username": "alex"},
            },
        })

        call = adapter.handle_message.await_args
        assert call is not None
        event = call.args[0]
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == ["/tmp/image.png"]
        assert event.media_types == ["image/png"]
        assert event.reply_to_message_id == "m1"
        assert event.reply_to_text == "previous"
        assert event.reply_to_author_id == "other"
        assert event.reply_to_author_name == "alex"
