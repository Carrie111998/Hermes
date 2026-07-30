"""Tests for the BlueBubbles iMessage gateway adapter."""
import asyncio
import json

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter(monkeypatch, **extra):
    monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
    from gateway.platforms.bluebubbles import BlueBubblesAdapter

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "server_url": "http://localhost:1234",
            "password": "secret",
            **extra,
        },
    )
    return BlueBubblesAdapter(cfg)


class TestBlueBubblesConfigLoading:
    def test_apply_env_overrides_bluebubbles(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        monkeypatch.setenv("BLUEBUBBLES_WEBHOOK_PORT", "9999")
        monkeypatch.setenv("BLUEBUBBLES_REQUIRE_MENTION", "true")
        monkeypatch.setenv("BLUEBUBBLES_MENTION_PATTERNS", r'["(?i)^amos\\b"]')
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.BLUEBUBBLES in config.platforms
        bc = config.platforms[Platform.BLUEBUBBLES]
        assert bc.enabled is True
        assert bc.extra["server_url"] == "http://localhost:1234"
        assert bc.extra["password"] == "secret"
        assert bc.extra["webhook_port"] == 9999
        assert bc.extra["require_mention"] is True
        assert bc.extra["mention_patterns"] == ["(?i)^amos\\b"]


class TestBlueBubblesHelpers:
    def test_check_requirements(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
        monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
        from gateway.platforms.bluebubbles import check_bluebubbles_requirements

        assert check_bluebubbles_requirements() is True

    @pytest.mark.asyncio
    async def test_send_keeps_paragraphs_in_one_bubble_when_under_limit(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        async def fake_api_post(path, payload):
            sent.append(payload["message"])
            return {"data": {"guid": f"msg-{len(sent)}"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)

        content = "first thought\n\nsecond thought"
        result = await adapter.send("user@example.com", content)

        assert result.success is True
        assert sent == [content]

    @pytest.mark.asyncio
    async def test_send_deduplicates_concurrent_content_for_same_origin(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        async def fake_api_post(path, payload):
            sent.append(payload)
            await asyncio.sleep(0.01)
            return {"data": {"guid": "assistant-message-guid"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        metadata = {"reply_to_message_id": "origin-message-guid"}

        first, second = await asyncio.gather(
            adapter.send("user@example.com", "same answer", metadata=metadata),
            adapter.send("user@example.com", "same answer", metadata=metadata),
        )

        assert first.success is True
        assert second.success is True
        assert [payload["message"] for payload in sent] == ["same answer"]
        assert {first.raw_response.get("deduplicated"), second.raw_response.get("deduplicated")} == {None, True}

    @pytest.mark.asyncio
    async def test_send_without_origin_does_not_deduplicate_legitimate_repeats(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        sent = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        async def fake_api_post(path, payload):
            sent.append(payload["message"])
            return {"data": {"guid": f"message-{len(sent)}"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)

        await adapter.send("user@example.com", "legitimate repeat")
        await adapter.send("user@example.com", "legitimate repeat")

        assert sent == ["legitimate repeat", "legitimate repeat"]

    @pytest.mark.asyncio
    async def test_send_reuses_idempotency_key_after_ambiguous_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payloads = []

        async def fake_resolve_chat_guid(chat_id):
            return "iMessage;-;user@example.com"

        async def fake_api_post(path, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                raise TimeoutError("response lost")
            return {"data": {"guid": "assistant-message-guid"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        metadata = {"reply_to_message_id": "origin-message-guid"}

        first = await adapter.send("user@example.com", "same answer", metadata=metadata)
        second = await adapter.send("user@example.com", "same answer", metadata=metadata)

        assert first.success is False
        assert second.success is True
        assert payloads[0]["tempGuid"] == payloads[1]["tempGuid"]

    @pytest.mark.asyncio
    async def test_send_suppresses_only_structured_internal_notice_over_sms(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        api_calls = []

        async def fake_resolve_chat_guid(chat_id):
            return "SMS;-;+155****0100"

        async def fake_api_post(path, payload):
            api_calls.append((path, payload))
            return {"data": {"guid": "sent-message"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        text = "Gateway shutting down for maintenance"

        legitimate = await adapter.send("+155****0100", text)
        suppressed = await adapter.send(
            "+155****0100",
            text,
            metadata={"internal_notice": True},
        )

        assert legitimate.success is True
        assert suppressed.success is True
        assert suppressed.raw_response == {"suppressed": "internal_sms_notice"}
        assert [payload["message"] for _, payload in api_calls] == [text]

    @pytest.mark.asyncio
    async def test_internal_notice_does_not_create_unresolved_phone_chat(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        create_calls = []

        async def fake_resolve_chat_guid(chat_id):
            return None

        async def fake_create_chat(address, message, temp_guid=None):
            create_calls.append((address, message, temp_guid))
            raise AssertionError("internal notice must not create an unresolved phone chat")

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_create_chat_for_handle", fake_create_chat)

        result = await adapter.send(
            "+15555550100",
            "Gateway restarting",
            metadata={"internal_notice": True},
        )

        assert result.success is True
        assert result.raw_response == {"suppressed": "internal_sms_notice"}
        assert create_calls == []

    @pytest.mark.asyncio
    async def test_internal_notice_allows_plus_prefixed_email_handle(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        payloads = []

        async def fake_resolve_chat_guid(chat_id):
            return None

        async def fake_api_post(path, payload):
            payloads.append((path, payload))
            return {"data": {"guid": "new-chat-message"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)

        result = await adapter.send(
            "+15555550100@example.com",
            "Internal iMessage notice",
            metadata={"internal_notice": True},
        )

        assert result.success is True
        assert [path for path, _ in payloads] == ["/api/v1/chat/new"]

    @pytest.mark.asyncio
    async def test_new_chat_retry_reuses_idempotency_key(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        payloads = []

        async def fake_resolve_chat_guid(chat_id):
            return None

        async def fake_api_post(path, payload):
            assert path == "/api/v1/chat/new"
            payloads.append(payload)
            if len(payloads) == 1:
                raise TimeoutError("response lost")
            return {"data": {"guid": "new-chat-message"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        metadata = {"reply_to_message_id": "origin-message-guid"}

        first = await adapter.send("user@example.com", "hello", metadata=metadata)
        second = await adapter.send("user@example.com", "hello", metadata=metadata)

        assert first.success is False
        assert second.success is True
        assert payloads[0]["tempGuid"] == payloads[1]["tempGuid"]

    @pytest.mark.asyncio
    async def test_new_chat_sends_remaining_chunks_after_creation(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._private_api_enabled = True
        resolve_calls = 0
        payloads = []

        async def fake_resolve_chat_guid(chat_id):
            nonlocal resolve_calls
            resolve_calls += 1
            return None if resolve_calls == 1 else "iMessage;-;user@example.com"

        async def fake_api_post(path, payload):
            payloads.append((path, payload))
            return {"data": {"guid": f"message-{len(payloads)}"}}

        monkeypatch.setattr(adapter, "_resolve_chat_guid", fake_resolve_chat_guid)
        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        text = "x" * (adapter.MAX_MESSAGE_LENGTH + 1)

        result = await adapter.send(
            "user@example.com",
            text,
            metadata={"reply_to_message_id": "origin-message-guid"},
        )

        assert result.success is True
        assert [path for path, _ in payloads] == [
            "/api/v1/chat/new",
            "/api/v1/message/text",
        ]
        assert "".join(payload["message"] for _, payload in payloads) == text

    def test_format_message_preserves_underscores_in_identifiers(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        text = "Use /api_v2 with FEATURE_FLAG_NAME and config_file.json"
        assert adapter.format_message(text) == text

    def test_strip_markdown_headers(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter.format_message("## Heading\ntext") == "Heading\ntext"


    def test_init_normalizes_webhook_path(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_path="bluebubbles-webhook")
        assert adapter.webhook_path == "/bluebubbles-webhook"


    def test_server_url_normalized(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, server_url="http://localhost:1234/")
        assert adapter.server_url == "http://localhost:1234"


class _FakeBlueBubblesRequest:
    def __init__(self, payload, password="secret"):
        self.query = {"password": password}
        self.headers = {}
        self._body = json.dumps(payload).encode("utf-8")

    async def read(self):
        return self._body


class TestBlueBubblesMentionGating:
    @pytest.mark.asyncio
    async def test_group_message_without_mention_is_acknowledged_and_skipped(self, monkeypatch):
        adapter = _make_adapter(
            monkeypatch,
            require_mention=True,
            send_read_receipts=False,
        )
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        response = await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                "guid": "msg-1",
                "text": "casual family chatter",
                "handle": {"address": "+15555550100"},
                "isFromMe": False,
                "isGroup": True,
                "chats": [{"guid": "iMessage;+;group-chat"}],
            },
        }))
        await asyncio.sleep(0)

        assert response.status == 200
        assert handled == []

    @pytest.mark.asyncio
    async def test_duplicate_new_and_updated_events_are_handled_once(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        message = {
            "guid": "same-message-guid",
            "text": "hello once",
            "handle": {"address": "user@example.com"},
            "isFromMe": False,
            "chatIdentifier": "user@example.com",
        }

        first, second = await asyncio.gather(
            adapter._handle_webhook(_FakeBlueBubblesRequest({
                "type": "new-message",
                "data": {**message, "chatGuid": "iMessage;-;user@example.com"},
            })),
            adapter._handle_webhook(_FakeBlueBubblesRequest({
                "type": "updated-message",
                "data": message,
            })),
        )
        await asyncio.sleep(0)

        assert first.status == 200
        assert second.status == 200
        assert [event.text for event in handled] == ["hello once"]

    @pytest.mark.asyncio
    async def test_meaningful_text_revision_with_same_guid_is_processed(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        base = {
            "guid": "edited-message-guid",
            "handle": {"address": "user@example.com"},
            "isFromMe": False,
            "chatIdentifier": "user@example.com",
        }

        await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {**base, "text": "original text"},
        }))
        await asyncio.sleep(0)
        await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "updated-message",
            "data": {**base, "text": "edited text"},
        }))
        await asyncio.sleep(0)

        assert [event.text for event in handled] == ["original text", "edited text"]

    @pytest.mark.asyncio
    async def test_attachment_revision_with_same_guid_is_processed(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        async def fake_download_attachment(att_guid, att_meta):
            return f"/tmp/{att_guid}"

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        monkeypatch.setattr(adapter, "_download_attachment", fake_download_attachment)
        base = {
            "guid": "attachment-revision-guid",
            "text": "",
            "handle": {"address": "user@example.com"},
            "isFromMe": False,
            "chatIdentifier": "user@example.com",
        }

        await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "new-message",
            "data": {
                **base,
                "attachments": [
                    {
                        "guid": "same-attachment",
                        "mimeType": "application/octet-stream",
                        "uti": "public.data",
                    }
                ],
            },
        }))
        await asyncio.sleep(0)
        await adapter._handle_webhook(_FakeBlueBubblesRequest({
            "type": "updated-message",
            "data": {
                **base,
                "attachments": [
                    {
                        "guid": "same-attachment",
                        "mimeType": "application/octet-stream",
                        "uti": "public.caf",
                    }
                ],
            },
        }))
        await asyncio.sleep(0)

        assert [event.media_urls for event in handled] == [
            ["/tmp/same-attachment"],
            ["/tmp/same-attachment"],
        ]

    @pytest.mark.asyncio
    async def test_attachment_readiness_transition_is_processed(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        handled = []
        downloads = 0

        async def fake_handle_message(event):
            handled.append(event)

        async def fake_download_attachment(att_guid, att_meta):
            nonlocal downloads
            downloads += 1
            return None if downloads == 1 else f"/tmp/{att_guid}"

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        monkeypatch.setattr(adapter, "_download_attachment", fake_download_attachment)
        payload = {
            "type": "updated-message",
            "data": {
                "guid": "attachment-ready-guid",
                "text": "caption",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatIdentifier": "user@example.com",
                "attachments": [{"guid": "same-attachment", "mimeType": "image/png"}],
            },
        }

        await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)
        await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)

        assert [event.media_urls for event in handled] == [
            [],
            ["/tmp/same-attachment"],
        ]

    @pytest.mark.asyncio
    async def test_failed_dispatch_releases_guid_for_retry(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        attempts = 0
        handled = []

        async def fake_handle_message(event):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient dispatch failure")
            handled.append(event.text)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        payload = {
            "type": "new-message",
            "data": {
                "guid": "retryable-message-guid",
                "text": "retry me",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatIdentifier": "user@example.com",
            },
        }

        first = await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)
        second = await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)

        assert first.status == 200
        assert second.status == 200
        assert attempts == 2
        assert handled == ["retry me"]

    @pytest.mark.asyncio
    async def test_cancelled_dispatch_releases_identity_for_retry(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        attempts = 0
        handled = []

        async def fake_handle_message(event):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.CancelledError()
            handled.append(event.text)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        payload = {
            "type": "new-message",
            "data": {
                "guid": "cancelled-message-guid",
                "text": "retry cancellation",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
                "chatIdentifier": "user@example.com",
            },
        }

        await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)
        await adapter._handle_webhook(_FakeBlueBubblesRequest(payload))
        await asyncio.sleep(0)

        assert attempts == 2
        assert handled == ["retry cancellation"]

    @pytest.mark.asyncio
    async def test_pending_identity_is_not_evicted_by_completed_lru(self, monkeypatch):
        import gateway.platforms.bluebubbles as bluebubbles_module

        monkeypatch.setattr(bluebubbles_module, "_MESSAGE_DEDUP_SIZE", 1)
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        started = asyncio.Event()
        release = asyncio.Event()
        attempts = {"pending-guid": 0, "completed-guid": 0}

        async def fake_handle_message(event):
            attempts[event.message_id] += 1
            if event.message_id == "pending-guid":
                started.set()
                await release.wait()

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

        def payload(guid, text):
            return {
                "type": "new-message",
                "data": {
                    "guid": guid,
                    "text": text,
                    "handle": {"address": "user@example.com"},
                    "isFromMe": False,
                    "chatIdentifier": "user@example.com",
                },
            }

        await adapter._handle_webhook(
            _FakeBlueBubblesRequest(payload("pending-guid", "still running"))
        )
        await started.wait()
        await adapter._handle_webhook(
            _FakeBlueBubblesRequest(payload("completed-guid", "done"))
        )
        await asyncio.sleep(0)
        await adapter._handle_webhook(
            _FakeBlueBubblesRequest(payload("pending-guid", "still running"))
        )
        await asyncio.sleep(0)
        pending_attempts = attempts["pending-guid"]
        release.set()
        await asyncio.sleep(0)

        assert pending_attempts == 1


class TestBlueBubblesWebhookParsing:

    def test_webhook_can_fall_back_to_sender_when_chat_fields_missing(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "data": {
                "guid": "MESSAGE-GUID",
                "text": "hello",
                "handle": {"address": "user@example.com"},
                "isFromMe": False,
            }
        }
        record = adapter._extract_payload_record(payload) or {}
        chat_guid = adapter._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        chat_identifier = adapter._value(
            record.get("chatIdentifier"),
            record.get("identifier"),
            payload.get("chatIdentifier"),
            payload.get("identifier"),
        )
        sender = (
            adapter._value(
                record.get("handle", {}).get("address")
                if isinstance(record.get("handle"), dict)
                else None,
                record.get("sender"),
                record.get("from"),
                record.get("address"),
            )
            or chat_identifier
            or chat_guid
        )
        if not (chat_guid or chat_identifier) and sender:
            chat_identifier = sender
        assert chat_identifier == "user@example.com"


    def test_extract_payload_record_accepts_list_data(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        payload = {
            "type": "new-message",
            "data": [
                {
                    "text": "hello",
                    "chatGuid": "iMessage;-;user@example.com",
                    "chatIdentifier": "user@example.com",
                }
            ],
        }
        record = adapter._extract_payload_record(payload)
        assert record == payload["data"][0]


class TestBlueBubblesGuidResolution:


    @pytest.mark.asyncio
    async def test_participant_only_match_does_not_resolve_to_group(self, monkeypatch):
        """Regression for #24157: contact appearing as a participant in a group
        chat must NOT be selected when no DM with that exact chatIdentifier exists.

        Otherwise an outbound DM reply leaks into the group thread.
        """
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;+;chat0000000000-family-group",
                        "chatIdentifier": "chat0000000000",
                        "participants": [
                            {"address": "user@example.com"},
                            {"address": "+15555550100"},
                        ],
                    }
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        result = await adapter._resolve_chat_guid("user@example.com")
        assert result is None, (
            "participant-only match must not resolve to a group GUID — DM "
            "replies would leak into the group thread"
        )


    @pytest.mark.asyncio
    async def test_unresolved_target_is_not_cached(self, monkeypatch):
        """When no exact match is found, the resolver must NOT cache anything.

        Otherwise a later attempt — after the DM has been created — would
        keep returning the stale ``None`` from cache. Also guards against a
        latent variant of #24157 where a group GUID could be cached under a
        bare address key and persist across calls.
        """
        adapter = _make_adapter(monkeypatch)

        async def fake_api_post(path, payload):
            return {
                "data": [
                    {
                        "guid": "iMessage;+;chat0000000000-family-group",
                        "chatIdentifier": "chat0000000000",
                        "participants": [{"address": "user@example.com"}],
                    }
                ]
            }

        monkeypatch.setattr(adapter, "_api_post", fake_api_post)
        await adapter._resolve_chat_guid("user@example.com")
        assert "user@example.com" not in adapter._guid_cache


class TestBlueBubblesAttachmentDownload:
    """Verify _download_attachment routes to the correct cache helper."""

    def test_download_image_uses_image_cache(self, monkeypatch):
        """Image MIME routes to cache_image_from_bytes."""
        adapter = _make_adapter(monkeypatch)
        import asyncio

        # Mock the HTTP client response
        class MockResponse:
            status_code = 200
            content = b"\x89PNG\r\n\x1a\n"

            def raise_for_status(self):
                pass

        async def mock_get(*args, **kwargs):
            return MockResponse()

        adapter.client = type("MockClient", (), {"get": mock_get})()

        cached_path = None

        def mock_cache_image(data, ext):
            nonlocal cached_path
            cached_path = f"/tmp/test_image{ext}"
            return cached_path

        monkeypatch.setattr(
            "gateway.platforms.bluebubbles.cache_image_from_bytes",
            mock_cache_image,
        )

        att_meta = {"mimeType": "image/png", "transferName": "photo.png"}
        result = asyncio.get_event_loop().run_until_complete(
            adapter._download_attachment("att-guid-123", att_meta)
        )
        assert result == "/tmp/test_image.png"


# ---------------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------------


class TestBlueBubblesWebhookUrl:
    """_webhook_url property normalises local hosts to 'localhost'."""

    def test_default_host(self, monkeypatch):
        monkeypatch.delenv("BLUEBUBBLES_WEBHOOK_HOST", raising=False)
        adapter = _make_adapter(monkeypatch)
        # Default webhook_host is 0.0.0.0 → normalized to localhost
        assert "localhost" in adapter._webhook_url
        assert str(adapter.webhook_port) in adapter._webhook_url
        assert adapter.webhook_path in adapter._webhook_url


    def test_register_url_omits_query_when_no_password(self, monkeypatch):
        """If no password is configured, the register URL should be the bare URL."""
        monkeypatch.delenv("BLUEBUBBLES_PASSWORD", raising=False)
        from gateway.platforms.bluebubbles import BlueBubblesAdapter
        cfg = PlatformConfig(
            enabled=True,
            extra={"server_url": "http://localhost:1234", "password": ""},
        )
        adapter = BlueBubblesAdapter(cfg)
        assert adapter._webhook_register_url == adapter._webhook_url


class TestBlueBubblesWebhookRegistration:
    """Tests for _register_webhook, _unregister_webhook, _find_registered_webhooks."""

    @staticmethod
    def _mock_client(get_response=None, post_response=None, delete_ok=True):
        """Build a tiny mock httpx.AsyncClient."""

        async def mock_get(*args, **kwargs):
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return get_response or {"status": 200, "data": []}
            return R()

        async def mock_post(*args, **kwargs):
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return post_response or {"status": 200, "data": {}}
            return R()

        async def mock_delete(*args, **kwargs):
            class R:
                status_code = 200 if delete_ok else 500
                def raise_for_status(self_inner):
                    if not delete_ok:
                        raise Exception("delete failed")
            return R()

        return type(
            "MockClient", (),
            {"get": mock_get, "post": mock_post, "delete": mock_delete},
        )()

    # -- _find_registered_webhooks --

    def test_find_registered_webhooks_returns_matches(self, monkeypatch):
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_url
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 1, "url": url, "events": ["new-message"]},
                {"id": 2, "url": "http://other:9999/hook", "events": ["message"]},
            ]}
        )
        result = asyncio.get_event_loop().run_until_complete(
            adapter._find_registered_webhooks(url)
        )
        assert len(result) == 1
        assert result[0]["id"] == 1


    # -- _register_webhook --

    def test_register_fresh(self, monkeypatch):
        """No existing webhook → POST creates one."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": []},
            post_response={"status": 200, "data": {"id": 42}},
        )
        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is True


    def test_register_reuses_existing(self, monkeypatch):
        """Crash resilience — existing registration is reused, no POST needed."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 7, "url": url, "events": ["new-message"]},
            ]},
        )

        # Track whether POST was called
        post_called = False
        orig_api_post = adapter._api_post
        async def tracking_post(path, payload):
            nonlocal post_called
            post_called = True
            return await orig_api_post(path, payload)
        adapter._api_post = tracking_post

        ok = asyncio.get_event_loop().run_until_complete(
            adapter._register_webhook()
        )
        assert ok is True
        assert not post_called, "Should reuse existing, not POST again"


    # -- _unregister_webhook --


    def test_unregister_removes_all_duplicates(self, monkeypatch):
        """Multiple orphaned registrations for same URL — all get removed."""
        import asyncio
        adapter = _make_adapter(monkeypatch)
        url = adapter._webhook_register_url
        deleted_ids = []

        async def mock_delete(*args, **kwargs):
            # Extract ID from URL
            url_str = args[0] if args else ""
            deleted_ids.append(url_str)
            class R:
                status_code = 200
                def raise_for_status(self):
                    pass
            return R()

        adapter.client = self._mock_client(
            get_response={"status": 200, "data": [
                {"id": 1, "url": url},
                {"id": 2, "url": url},
                {"id": 3, "url": "http://other/hook"},
            ]},
        )
        adapter.client.delete = mock_delete

        ok = asyncio.get_event_loop().run_until_complete(
            adapter._unregister_webhook()
        )
        assert ok is True
        assert len(deleted_ids) == 2


