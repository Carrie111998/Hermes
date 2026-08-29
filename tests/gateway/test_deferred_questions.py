from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _service(path: Path):
    from gateway.deferred_questions import DeferredQuestionService

    return DeferredQuestionService(path)


def _enqueue(service, *, dedupe_key: str = "invite-consent"):
    return service.enqueue(
        plugin_id="plow-chat",
        platform="plow_chat",
        session_key="plow_chat:home:owner",
        chat_id="home",
        question="May I send invites?",
        handler_name="invite-consent",
        context={"source_chat_uid": "cht_source"},
        dedupe_key=dedupe_key,
    )


def test_enqueue_deduplicates_one_unresolved_question(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")

    first = _enqueue(service)
    second = _enqueue(service)

    assert second.id == first.id
    assert service.pending_for_session(first.session_key) == first


@pytest.mark.asyncio
async def test_response_is_persisted_before_handler_and_resolves(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    service.claim_for_delivery(question.id)
    observed = []

    async def handle(record, answer):
        reopened = _service(tmp_path / "questions.sqlite3")
        captured = reopened.get(record.id)
        observed.append((captured.state, captured.response, answer))
        return DeferredQuestionResult.done("Consent recorded.")

    service.register_handler("plow-chat", "invite-consent", handle)

    result = await service.handle_response(question.session_key, "Sure!")

    assert result == DeferredQuestionResult.done("Consent recorded.")
    assert observed == [("handling", "Sure!", "Sure!")]
    assert service.get(question.id).state == "resolved"
    assert service.pending_for_session(question.session_key) is None


@pytest.mark.asyncio
async def test_unclear_response_reasks_same_question(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    service.claim_for_delivery(question.id)

    async def handle(_record, _answer):
        return DeferredQuestionResult.clarify("Would you like me to send invites?")

    service.register_handler("plow-chat", "invite-consent", handle)

    result = await service.handle_response(question.session_key, "What do you mean?")

    assert result == DeferredQuestionResult.clarify(
        "Would you like me to send invites?"
    )
    pending = service.pending_for_session(question.session_key)
    assert pending is not None
    assert pending.id == question.id
    assert pending.state == "awaiting"
    assert pending.question == "Would you like me to send invites?"
    assert pending.response is None


@pytest.mark.asyncio
async def test_handling_response_retries_after_restart(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    path = tmp_path / "questions.sqlite3"
    first_service = _service(path)
    question = _enqueue(first_service)
    first_service.claim_for_delivery(question.id)

    async def fail(_record, _answer):
        raise RuntimeError("temporary")

    first_service.register_handler("plow-chat", "invite-consent", fail)
    with pytest.raises(RuntimeError, match="temporary"):
        await first_service.handle_response(question.session_key, "Sure!")

    captured = first_service.get(question.id)
    assert captured.state == "handling"
    assert captured.response == "Sure!"

    restarted = _service(path)
    answers = []

    async def recover(_record, answer):
        answers.append(answer)
        return DeferredQuestionResult.done("Recovered.")

    restarted.register_handler("plow-chat", "invite-consent", recover)
    results = await restarted.retry_handling()

    assert results == [(question.id, DeferredQuestionResult.done("Recovered."))]
    assert answers == ["Sure!"]
    assert restarted.get(question.id).state == "resolved"


def test_resolved_dedupe_key_can_be_enqueued_again(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")
    first = _enqueue(service)
    service.claim_for_delivery(first.id)
    service.resolve_without_handler(first.id, "done")

    second = _enqueue(service)

    assert second.id != first.id


class _DeliveryAdapter:
    def __init__(self, service, *, active: bool) -> None:
        self.service = service
        self.active = active
        self.callbacks = {}
        self.sent = []

    def is_session_active(self, session_key: str) -> bool:
        assert session_key == "plow_chat:home:owner"
        return self.active

    def register_post_delivery_callback(self, session_key, callback) -> None:
        self.callbacks[session_key] = callback

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        pending = self.service.pending_for_session("plow_chat:home:owner")
        assert pending is not None
        assert pending.state == "awaiting"
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True, error=None)


@pytest.mark.asyncio
async def test_busy_session_delivers_only_from_completion_callback(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    adapter = _DeliveryAdapter(service, active=True)
    service.bind_adapter("plow_chat", adapter)

    await service.deliver_ready("plow_chat")

    assert adapter.sent == []
    assert service.get(question.id).state == "queued"
    callback = adapter.callbacks[question.session_key]
    adapter.active = False
    await callback()

    assert adapter.sent == [("home", "May I send invites?")]
    assert service.get(question.id).state == "awaiting"


@pytest.mark.asyncio
async def test_failed_delivery_returns_question_to_queue(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)

    class FailingAdapter(_DeliveryAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            assert self.service.get(question.id).state == "awaiting"
            return SimpleNamespace(success=False, error="offline")

    adapter = FailingAdapter(service, active=False)
    service.bind_adapter("plow_chat", adapter)

    await service.deliver_ready("plow_chat")

    assert service.get(question.id).state == "queued"


@pytest.mark.asyncio
async def test_failed_delivery_retries_without_another_external_wake(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")
    service.delivery_retry_seconds = 0
    question = _enqueue(service)

    class FlakyAdapter(_DeliveryAdapter):
        def __init__(self, service) -> None:
            super().__init__(service, active=False)
            self.attempts = 0

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.attempts += 1
            if self.attempts == 1:
                return SimpleNamespace(success=False, error="offline")
            return await super().send(chat_id, content, reply_to, metadata)

    adapter = FlakyAdapter(service)
    service.bind_adapter("plow_chat", adapter)
    for _ in range(5):
        await __import__("asyncio").sleep(0)

    assert adapter.attempts == 2
    assert service.get(question.id).state == "awaiting"


@pytest.mark.asyncio
async def test_wake_does_not_copy_the_enqueuers_context_into_delivery(tmp_path: Path) -> None:
    import contextvars

    service = _service(tmp_path / "questions.sqlite3")
    marker = contextvars.ContextVar("deferred_delivery_marker", default=None)

    class ContextAdapter(_DeliveryAdapter):
        def __init__(self, service) -> None:
            super().__init__(service, active=False)
            self.context_values = []

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.context_values.append(marker.get())
            return await super().send(chat_id, content, reply_to, metadata)

    adapter = ContextAdapter(service)
    service.bind_adapter("plow_chat", adapter)
    token = marker.set("member-turn")
    try:
        _enqueue(service)
    finally:
        marker.reset(token)
    for _ in range(3):
        await __import__("asyncio").sleep(0)

    assert adapter.context_values == [None]


def test_plugin_client_namespaces_handler_and_dedupe_key(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionClient

    service = _service(tmp_path / "questions.sqlite3")
    client = DeferredQuestionClient(service, "plow-chat")

    async def handler(_record, _answer):
        raise AssertionError("not called")

    client.register_handler("invite-consent", handler)
    question = client.enqueue(
        platform="plow_chat",
        session_key="plow_chat:home:owner",
        chat_id="home",
        question="May I send invites?",
        context={"source_chat_uid": "cht_source"},
        dedupe_key="owner-consent",
        handler_name="invite-consent",
    )

    assert question.plugin_id == "plow-chat"
    assert question.handler_name == "invite-consent"
    assert question.dedupe_key == "owner-consent"


@pytest.mark.asyncio
async def test_binding_adapter_recovers_queued_question_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "questions.sqlite3"
    first = _service(path)
    question = _enqueue(first)

    restarted = _service(path)
    adapter = _DeliveryAdapter(restarted, active=False)
    restarted.bind_adapter("plow_chat", adapter)
    await __import__("asyncio").sleep(0)
    await __import__("asyncio").sleep(0)

    assert adapter.sent == [("home", "May I send invites?")]
    assert restarted.get(question.id).state == "awaiting"


def test_plugin_context_exposes_plugin_scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import deferred_questions
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    service = _service(tmp_path / "questions.sqlite3")
    monkeypatch.setattr(
        deferred_questions, "get_deferred_question_service", lambda: service
    )
    context = PluginContext(
        PluginManifest(name="Plow Chat", key="plow-chat"), PluginManager()
    )

    assert context.deferred_questions.plugin_id == "plow-chat"
    assert context.deferred_questions is context.deferred_questions


def test_host_service_is_scoped_to_the_active_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_constants
    from gateway import deferred_questions

    active = tmp_path / "profile-a"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: active)
    monkeypatch.setattr(deferred_questions, "_singletons", {})

    first = deferred_questions.get_deferred_question_service()
    again = deferred_questions.get_deferred_question_service()
    active = tmp_path / "profile-b"
    second = deferred_questions.get_deferred_question_service()

    assert again is first
    assert second is not first
    assert first.path.parent == tmp_path / "profile-a"
    assert second.path.parent == tmp_path / "profile-b"


def test_gateway_setup_surfaces_deferred_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import deferred_questions
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter

    class Adapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            raise AssertionError("not called")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    def fail():
        raise OSError("store unavailable")

    monkeypatch.setattr(deferred_questions, "get_deferred_question_service", fail)
    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)

    with pytest.raises(OSError, match="store unavailable"):
        adapter.set_message_handler(AsyncMock())


@pytest.mark.asyncio
async def test_adapter_intercepts_deferred_reply_before_busy_queue(
    tmp_path: Path,
) -> None:
    from gateway.config import Platform, PlatformConfig
    from gateway.deferred_questions import DeferredQuestionResult
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource, build_session_key

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
            self.sent = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append((chat_id, content))
            return SendResult(success=True, message_id="reply")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    service = _service(tmp_path / "questions.sqlite3")
    adapter = Adapter()
    adapter.set_deferred_question_service(service)
    adapter._message_handler = AsyncMock(return_value="ordinary")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="home",
        chat_type="dm",
        user_id="owner",
    )
    session_key = build_session_key(source)
    question = service.enqueue(
        plugin_id="plow-chat",
        platform="telegram",
        session_key=session_key,
        chat_id="home",
        question="May I send invites?",
        handler_name="invite-consent",
        context={},
        dedupe_key="invite-consent",
    )
    service.claim_for_delivery(question.id)

    async def handle(_record, answer):
        assert answer == "Sure!"
        return DeferredQuestionResult.done("Great — I’ll send it now.")

    service.register_handler("plow-chat", "invite-consent", handle)
    adapter._active_sessions[session_key] = __import__("asyncio").Event()
    event = MessageEvent(
        text="Sure!",
        source=source,
        message_id="msg-answer",
        message_type=MessageType.TEXT,
    )

    await adapter.handle_message(event)

    assert adapter.sent == [("home", "Great — I’ll send it now.")]
    adapter._message_handler.assert_not_awaited()
    adapter._busy_session_handler.assert_not_awaited()
    assert service.get(question.id).state == "resolved"


@pytest.mark.asyncio
async def test_slash_command_bypasses_pending_deferred_question(tmp_path: Path) -> None:
    import asyncio

    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
    from gateway.session import SessionSource, build_session_key

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
            self.sent = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append((chat_id, content))
            return SendResult(success=True, message_id="reply")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    service = _service(tmp_path / "questions.sqlite3")
    adapter = Adapter()
    adapter.set_deferred_question_service(service)
    adapter._message_handler = AsyncMock(return_value="status response")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="home", chat_type="dm", user_id="owner")
    question = service.enqueue(
        plugin_id="plow-chat",
        platform="telegram",
        session_key=build_session_key(source),
        chat_id="home",
        question="May I send invites?",
        handler_name="invite-consent",
        context={},
        dedupe_key="invite-consent",
    )
    service.claim_for_delivery(question.id)

    await adapter.handle_message(MessageEvent(
        text="/status",
        source=source,
        message_id="msg-command",
        message_type=MessageType.TEXT,
    ))
    await asyncio.gather(*tuple(adapter._background_tasks))

    assert service.get(question.id).state == "awaiting"
    adapter._message_handler.assert_awaited_once()
    assert adapter.sent == [("home", "status response")]
