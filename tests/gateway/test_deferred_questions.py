from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


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
async def test_response_is_persisted_before_handler_and_resolves(
    tmp_path: Path,
) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    adapter = _DeliveryAdapter(service, active=False)
    service._adapters["plow_chat"] = (adapter, __import__("asyncio").get_running_loop())
    service.claim_for_delivery(question.id)
    service.mark_awaiting(question.id)
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
    with pytest.raises(KeyError):
        service.get(question.id)
    assert service.pending_for_session(question.session_key) is None


@pytest.mark.asyncio
async def test_unclear_response_reasks_same_question(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    adapter = _DeliveryAdapter(service, active=False)
    service._adapters["plow_chat"] = (adapter, __import__("asyncio").get_running_loop())
    service.claim_for_delivery(question.id)
    service.mark_awaiting(question.id)

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
    first_adapter = _DeliveryAdapter(first_service, active=False)
    first_service._adapters["plow_chat"] = (
        first_adapter,
        __import__("asyncio").get_running_loop(),
    )
    first_service.claim_for_delivery(question.id)
    first_service.mark_awaiting(question.id)

    async def fail(_record, _answer):
        raise RuntimeError("temporary")

    first_service.register_handler("plow-chat", "invite-consent", fail)
    with pytest.raises(RuntimeError, match="temporary"):
        await first_service.handle_response(question.session_key, "Sure!")

    captured = first_service.get(question.id)
    assert captured.state == "handling"
    assert captured.response == "Sure!"

    restarted = _service(path)
    restarted_adapter = _DeliveryAdapter(restarted, active=False)
    restarted._adapters["plow_chat"] = (
        restarted_adapter,
        __import__("asyncio").get_running_loop(),
    )
    answers = []

    async def recover(_record, answer):
        answers.append(answer)
        return DeferredQuestionResult.done("Recovered.")

    restarted.register_handler("plow-chat", "invite-consent", recover)
    results = await restarted.retry_handling()

    assert results == [(question.id, DeferredQuestionResult.done("Recovered."))]
    assert answers == ["Sure!"]
    with pytest.raises(KeyError):
        restarted.get(question.id)


@pytest.mark.asyncio
async def test_overlapping_adapter_binds_run_one_handling_recovery(
    tmp_path: Path,
) -> None:
    import asyncio

    from gateway.deferred_questions import DeferredQuestionResult

    path = tmp_path / "questions.sqlite3"
    first = _service(path)
    question = _enqueue(first)
    first_adapter = _DeliveryAdapter(first, active=False)
    first._adapters["plow_chat"] = (first_adapter, asyncio.get_running_loop())
    first.claim_for_delivery(question.id)
    first.mark_awaiting(question.id)

    async def fail(_record, _answer):
        raise RuntimeError("temporary")

    first.register_handler("plow-chat", "invite-consent", fail)
    with pytest.raises(RuntimeError, match="temporary"):
        await first.handle_response(question.session_key, "yes")

    restarted = _service(path)
    adapter = _DeliveryAdapter(restarted, active=False)
    calls = 0

    async def recover(_record, _answer):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return DeferredQuestionResult.done("recovered")

    restarted.bind_adapter("plow_chat", adapter)
    restarted.bind_adapter("plow_chat", adapter)
    restarted.register_handler("plow-chat", "invite-consent", recover)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if restarted._recovery_task is not None:
        await restarted._recovery_task

    assert calls == 1
    assert adapter.sent == [("home", "recovered")]
    with pytest.raises(KeyError):
        restarted.get(question.id)


@pytest.mark.asyncio
async def test_resolved_dedupe_key_can_be_enqueued_again(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    first = _enqueue(service)
    adapter = _DeliveryAdapter(service, active=False)
    service._adapters["plow_chat"] = (adapter, __import__("asyncio").get_running_loop())
    service.claim_for_delivery(first.id)
    service.mark_awaiting(first.id)

    async def handle(_record, _answer):
        return DeferredQuestionResult.done("done")

    service.register_handler("plow-chat", "invite-consent", handle)
    await service.handle_response(first.session_key, "yes")

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

    def active_session_generation(self, session_key: str) -> int | None:
        return 7 if self.active else None

    def register_post_delivery_callback(
        self, session_key, callback, *, generation=None
    ) -> None:
        self.callbacks[session_key] = (generation, callback)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        pending = self.service.pending_for_session("plow_chat:home:owner")
        assert pending is not None
        assert pending.state in {"delivering", "handling"}
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True, error=None)


class _GatewayAdapter(BasePlatformAdapter):
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


@pytest.mark.asyncio
async def test_busy_session_delivers_only_from_completion_callback(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "questions.sqlite3")
    question = _enqueue(service)
    adapter = _DeliveryAdapter(service, active=True)
    service.bind_adapter("plow_chat", adapter)

    await service.deliver_ready("plow_chat")

    assert adapter.sent == []
    assert service.get(question.id).state == "queued"
    generation, callback = adapter.callbacks[question.session_key]
    assert generation == 7
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
            assert self.service.get(question.id).state == "delivering"
            return SimpleNamespace(success=False, error="offline")

    adapter = FailingAdapter(service, active=False)
    service.bind_adapter("plow_chat", adapter)

    await service.deliver_ready("plow_chat")

    assert service.get(question.id).state == "queued"


@pytest.mark.asyncio
async def test_failed_delivery_retries_without_another_external_wake(
    tmp_path: Path,
) -> None:
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
async def test_wake_does_not_copy_the_enqueuers_context_into_delivery(
    tmp_path: Path,
) -> None:
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


def test_stale_handler_cleanup_does_not_remove_replacement(tmp_path: Path) -> None:
    service = _service(tmp_path / "questions.sqlite3")

    async def old(_record, _answer):
        raise AssertionError("not called")

    async def replacement(_record, _answer):
        raise AssertionError("not called")

    service.register_handler("plow-chat", "invite-consent", old)
    service.register_handler("plow-chat", "invite-consent", replacement)
    service.unregister_handler("plow-chat", "invite-consent", old)

    assert service._handlers[("plow-chat", "invite-consent")] is replacement


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


@pytest.mark.asyncio
async def test_binding_adapter_reclaims_interrupted_delivery(tmp_path: Path) -> None:
    path = tmp_path / "questions.sqlite3"
    first = _service(path)
    question = _enqueue(first)
    first.claim_for_delivery(question.id)
    assert first.get(question.id).state == "delivering"

    restarted = _service(path)
    adapter = _DeliveryAdapter(restarted, active=False)
    restarted.bind_adapter("plow_chat", adapter)
    await __import__("asyncio").sleep(0)
    await __import__("asyncio").sleep(0)

    assert adapter.sent == [("home", "May I send invites?")]
    assert restarted.get(question.id).state == "awaiting"


@pytest.mark.asyncio
async def test_only_oldest_question_in_a_session_is_delivered(tmp_path: Path) -> None:
    from gateway.deferred_questions import DeferredQuestionResult

    service = _service(tmp_path / "questions.sqlite3")
    first = _enqueue(service, dedupe_key="first")
    second = _enqueue(service, dedupe_key="second")
    adapter = _DeliveryAdapter(service, active=False)
    service.bind_adapter("plow_chat", adapter)
    await service.deliver_ready("plow_chat")

    assert adapter.sent == [("home", first.question)]
    assert service.get(first.id).state == "awaiting"
    assert service.get(second.id).state == "queued"

    async def handle(_record, _answer):
        return DeferredQuestionResult.done("first resolved")

    service.register_handler("plow-chat", "invite-consent", handle)
    await service.handle_response(first.session_key, "yes")

    assert adapter.sent == [
        ("home", first.question),
        ("home", "first resolved"),
        ("home", second.question),
    ]
    assert service.get(second.id).state == "awaiting"


def test_plugin_context_exposes_plugin_scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import deferred_questions
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    service = _service(tmp_path / "questions.sqlite3")
    monkeypatch.setattr(
        deferred_questions, "get_deferred_question_service", lambda: service
    )
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="Plow Chat", key="plow-chat"), manager)

    assert context.deferred_questions.plugin_id == "plow-chat"
    assert context.deferred_questions is context.deferred_questions

    async def handler(_record, _answer):
        raise AssertionError("not called")

    context.deferred_questions.register_handler("invite-consent", handler)
    assert service._handlers[("plow-chat", "invite-consent")] is handler
    assert manager.unload("plow-chat")
    assert ("plow-chat", "invite-consent") not in service._handlers


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
    assert first.path.name == "deferred_questions.db"


def test_gateway_setup_surfaces_deferred_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import deferred_questions

    def fail():
        raise OSError("store unavailable")

    monkeypatch.setattr(deferred_questions, "get_deferred_question_service", fail)
    adapter = _GatewayAdapter()

    with pytest.raises(OSError, match="store unavailable"):
        adapter.set_message_handler(AsyncMock())


@pytest.mark.asyncio
async def test_adapter_intercepts_deferred_reply_before_busy_queue(
    tmp_path: Path,
) -> None:
    from gateway.deferred_questions import DeferredQuestionResult
    from gateway.platforms.base import (
        MessageEvent,
        MessageType,
    )
    from gateway.session import SessionSource, build_session_key

    service = _service(tmp_path / "questions.sqlite3")
    adapter = _GatewayAdapter()
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
    service.mark_awaiting(question.id)

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
    with pytest.raises(KeyError):
        service.get(question.id)


@pytest.mark.asyncio
async def test_adapter_rejects_unauthorized_deferred_reply(tmp_path: Path) -> None:
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource, build_session_key

    service = _service(tmp_path / "questions.sqlite3")
    adapter = _GatewayAdapter()
    adapter.set_deferred_question_service(service)
    adapter.set_authorization_check(lambda *_args: False)
    adapter._message_handler = AsyncMock(return_value="ordinary")
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="shared",
        chat_type="group",
        user_id="outsider",
    )
    question = service.enqueue(
        plugin_id="plow-chat",
        platform="telegram",
        session_key=build_session_key(source),
        chat_id="shared",
        question="May I send invites?",
        handler_name="invite-consent",
        context={},
        dedupe_key="invite-consent",
    )
    service.claim_for_delivery(question.id)
    service.mark_awaiting(question.id)
    handler = AsyncMock()
    service.register_handler("plow-chat", "invite-consent", handler)

    await adapter.handle_message(
        MessageEvent(
            text="yes",
            source=source,
            message_id="msg-outsider",
            message_type=MessageType.TEXT,
        )
    )

    assert service.get(question.id).state == "awaiting"
    handler.assert_not_awaited()
    adapter._message_handler.assert_not_awaited()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_slash_command_bypasses_pending_deferred_question(tmp_path: Path) -> None:
    import asyncio

    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource, build_session_key

    service = _service(tmp_path / "questions.sqlite3")
    adapter = _GatewayAdapter()
    adapter.set_deferred_question_service(service)
    adapter._message_handler = AsyncMock(return_value="status response")
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="home", chat_type="dm", user_id="owner"
    )
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
    service.mark_awaiting(question.id)

    await adapter.handle_message(
        MessageEvent(
            text="/status",
            source=source,
            message_id="msg-command",
            message_type=MessageType.TEXT,
        )
    )
    await asyncio.gather(*tuple(adapter._background_tasks))

    assert service.get(question.id).state == "awaiting"
    adapter._message_handler.assert_awaited_once()
    assert adapter.sent == [("home", "status response")]
