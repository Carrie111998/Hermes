from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.turn_gate import (
    GateDecision,
    GateState,
    TurnGateBlocked,
    TurnGateRequest,
    acquire_outer_turn,
    build_runtime_identity,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    current_turn_gate_request,
    register_turn_gate_provider as _register_turn_gate_provider,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner


def _configure_gate(provider_id: str) -> None:
    configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": provider_id,
                    "runtime_identity": {"machine_id": "test-machine"},
                }
            }
        }
    )


def register_turn_gate_provider(provider_id: str, provider) -> None:
    _register_turn_gate_provider(
        provider_id,
        provider,
        owner_id=provider_id,
    )


class DirectSendAdapter(BasePlatformAdapter):
    platform_name = "test-direct"

    def __init__(self):
        self.network_calls = 0
        self.follow_up_calls = 0
        self.named_output_calls = 0
        self.sync_output_calls = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        self.network_calls += 1
        return SendResult(success=True)

    async def send_follow_up(self, chat_id: str, content: str) -> SendResult:
        self.follow_up_calls += 1
        return SendResult(success=True)

    async def set_typing(self, chat_id: str) -> None:
        self.named_output_calls += 1

    async def mark_message_read(self, chat_id: str) -> None:
        self.named_output_calls += 1

    async def set_reaction(self, chat_id: str) -> None:
        self.named_output_calls += 1

    def send_sync_notice(self, chat_id: str) -> SendResult:
        self.sync_output_calls += 1
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class RecordingProvider:
    def __init__(self, events):
        self.events = events
        self.requests = []
        self.validation_decision: GateDecision | None = None
        self.validation_error: Exception | None = None

    def acquire(self, request):
        self.requests.append(request)
        self.events.append(("acquire", request.entrypoint, request.task_id))
        return GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id=f"lease-{request.entrypoint}",
            generation=26,
        )

    def validate(self, decision, checkpoint):
        if self.validation_error is not None:
            raise self.validation_error
        return self.validation_decision or decision

    def release(self, decision):
        self.events.append(("release", decision.lease_id))


@pytest.fixture(autouse=True)
def reset_gate():
    clear_turn_gate_registry_for_testing()
    yield
    clear_turn_gate_registry_for_testing()


@pytest.mark.asyncio
async def test_gateway_main_turn_acquires_before_any_inner_processing(monkeypatch):
    events = []
    register_turn_gate_provider("test-gate", RecordingProvider(events))
    _configure_gate("test-gate")

    async def fake_inner(self, *args, **kwargs):
        request = current_turn_gate_request()
        events.append(("body", request.entrypoint, request.task_id))
        return {"final_response": "ok"}

    monkeypatch.setattr(GatewayRunner, "_run_agent_inner_unleased", fake_inner)
    runner = object.__new__(GatewayRunner)
    result = await runner._run_agent_inner(
        "hello",
        "context",
        [],
        SimpleNamespace(platform="feishu"),
        "session-1",
        session_key="raw-key-not-persisted",
    )
    assert result == {"final_response": "ok"}
    assert events == [
        ("acquire", "gateway", "session-1"),
        ("body", "gateway", "session-1"),
        ("release", "lease-gateway"),
    ]


@pytest.mark.asyncio
async def test_gateway_background_turn_uses_same_outer_gate(monkeypatch):
    events = []
    register_turn_gate_provider("test-gate", RecordingProvider(events))
    _configure_gate("test-gate")

    async def fake_background(self, *args, **kwargs):
        request = current_turn_gate_request()
        events.append(("body", request.entrypoint, request.task_id))
        return None

    monkeypatch.setattr(GatewayRunner, "_run_background_task_unleased", fake_background)
    runner = object.__new__(GatewayRunner)
    await runner._run_background_task(
        "summarize",
        SimpleNamespace(platform="feishu"),
        "bg-1",
    )
    assert events == [
        ("acquire", "gateway-background", "bg-1"),
        ("body", "gateway-background", "bg-1"),
        ("release", "lease-gateway-background"),
    ]


@pytest.mark.asyncio
async def test_gateway_required_provider_missing_blocks_before_body(monkeypatch):
    _configure_gate("missing-gate")
    called = False

    async def fake_inner(self, *args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(GatewayRunner, "_run_agent_inner_unleased", fake_inner)
    runner = object.__new__(GatewayRunner)
    with pytest.raises(TurnGateBlocked, match="required provider"):
        await runner._run_agent_inner(
            "hello",
            "context",
            [],
            SimpleNamespace(platform="feishu"),
            "session-1",
        )
    assert called is False


@pytest.mark.asyncio
async def test_adapter_holds_lease_through_final_delivery():
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")

    async def fake_unleased(event, session_key):
        request = current_turn_gate_request()
        events.append(("handler", request.entrypoint))
        events.append(("send", request.entrypoint))

    adapter = SimpleNamespace(
        _process_message_background_unleased=fake_unleased,
    )
    await BasePlatformAdapter._process_message_background(
        adapter,
        SimpleNamespace(),
        "raw-session-key",
    )
    assert events[0][0:2] == ("acquire", "gateway-delivery")
    assert len(events[0][2]) == 36 and events[0][2].count("-") == 4
    assert "raw-session-key" not in events[0][2]
    assert events[1:] == [
        ("handler", "gateway-delivery"),
        ("send", "gateway-delivery"),
        ("release", "lease-gateway-delivery"),
    ]
    request = provider.requests[0]
    assert request.identity is not None
    assert request.identity.session_instance_id != "raw-session-key"
    assert request.identity.turn_id.startswith(f"{request.task_id}:")


@pytest.mark.asyncio
async def test_direct_adapter_output_without_outer_lease_fails_closed():
    events = []
    register_turn_gate_provider("test-gate", RecordingProvider(events))
    _configure_gate("test-gate")
    adapter = DirectSendAdapter()

    with pytest.raises(TurnGateBlocked, match="outer-turn lease"):
        await adapter.send("chat", "must not leak")

    assert adapter.network_calls == 0


@pytest.mark.asyncio
async def test_typing_read_and_reaction_outputs_without_lease_fail_closed():
    register_turn_gate_provider("test-gate", RecordingProvider([]))
    _configure_gate("test-gate")
    adapter = DirectSendAdapter()

    for method in (
        adapter.set_typing,
        adapter.mark_message_read,
        adapter.set_reaction,
    ):
        with pytest.raises(TurnGateBlocked, match="outer-turn lease"):
            await method("chat")
    with pytest.raises(TurnGateBlocked, match="outer-turn lease"):
        adapter.send_sync_notice("chat")

    assert adapter.named_output_calls == 0
    assert adapter.sync_output_calls == 0


@pytest.mark.asyncio
async def test_future_send_named_method_is_automatically_host_gated():
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")
    adapter = DirectSendAdapter()
    identity = build_runtime_identity(
        surface="gateway-delivery",
        session_scope="follow-up-session",
        turn_id="follow-up-turn",
    )
    assert identity is not None

    with acquire_outer_turn(
        TurnGateRequest(
            entrypoint="gateway-delivery",
            purpose="business",
            identity=identity,
        )
    ):
        provider.validation_decision = GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id="lease-gateway-delivery",
            generation=27,
        )
        with pytest.raises(TurnGateBlocked, match="generation changed"):
            await adapter.send_follow_up("chat", "must not leak")

    assert adapter.follow_up_calls == 0


@pytest.mark.asyncio
async def test_direct_adapter_send_is_host_gated_before_network_call():
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")
    adapter = DirectSendAdapter()
    identity = build_runtime_identity(
        surface="gateway-delivery",
        session_scope="direct-session",
        turn_id="direct-turn",
    )
    assert identity is not None

    with acquire_outer_turn(
        TurnGateRequest(
            entrypoint="gateway-delivery",
            purpose="business",
            identity=identity,
        )
    ):
        provider.validation_decision = GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id="lease-gateway-delivery",
            generation=27,
        )
        with pytest.raises(TurnGateBlocked, match="generation changed"):
            await adapter.send("chat", "must not leak")

    assert adapter.network_calls == 0


class SideEffectAdapter(BasePlatformAdapter):
    """Adapter overriding real platform side-effect entrypoints that are NOT
    covered by the ``send``/``edit``/``delete`` prefix set: thread handoff,
    thread/topic rename, and redaction. Each records a real underlying-effect
    counter so the host gate can be proven to run (or fail to run) before them.
    """

    platform_name = "test-side-effect"

    def __init__(self):
        self.handoff_calls = 0
        self.create_room_calls = 0
        self.invite_user_calls = 0
        self.rename_thread_calls = 0
        self.rename_dm_topic_calls = 0
        self.read_receipt_calls = 0
        self.redact_calls = 0
        self.presence_calls = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def create_handoff_thread(self, parent_chat_id: str, name: str):
        self.handoff_calls += 1
        return "thread-1"

    async def create_room(self, name: str = ""):
        self.create_room_calls += 1
        return "room-1"

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        self.invite_user_calls += 1
        return True

    async def rename_thread(self, chat_id: str, thread_id: str, name: str) -> bool:
        self.rename_thread_calls += 1
        return True

    async def rename_dm_topic(self, chat_id: str, topic_id: str, name: str) -> bool:
        self.rename_dm_topic_calls += 1
        return True

    async def send_read_receipt(self, chat_id: str, message_id: str) -> bool:
        self.read_receipt_calls += 1
        return True

    async def redact_message(self, chat_id: str, message_id: str) -> bool:
        self.redact_calls += 1
        return True

    async def set_presence(self, state: str = "online") -> bool:
        self.presence_calls += 1
        return True


@pytest.mark.asyncio
async def test_create_handoff_thread_without_outer_lease_has_no_side_effect():
    register_turn_gate_provider("test-gate", RecordingProvider([]))
    _configure_gate("test-gate")
    adapter = SideEffectAdapter()

    # A required provider with no active outer lease MUST fail closed before
    # the platform side effect — the new thread must never be created.
    with pytest.raises(TurnGateBlocked, match="outer-turn lease"):
        await adapter.create_handoff_thread("parent-chat", "handoff")

    assert adapter.handoff_calls == 0


@pytest.mark.asyncio
async def test_generation_change_blocks_create_rename_and_redact_side_effects():
    provider = RecordingProvider([])
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")
    adapter = SideEffectAdapter()
    identity = build_runtime_identity(
        surface="gateway-delivery",
        session_scope="handoff-session",
        turn_id="handoff-turn",
    )
    assert identity is not None

    with acquire_outer_turn(
        TurnGateRequest(
            entrypoint="gateway-delivery",
            purpose="business",
            identity=identity,
        )
    ):
        # The generation moves under the running turn; every representative
        # create/rename/redact entrypoint must revalidate and fail closed.
        provider.validation_decision = GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id="lease-gateway-delivery",
            generation=27,
        )
        with pytest.raises(TurnGateBlocked, match="generation changed"):
            await adapter.create_handoff_thread("parent-chat", "handoff")
        with pytest.raises(TurnGateBlocked):
            await adapter.create_room("room")
        with pytest.raises(TurnGateBlocked):
            await adapter.invite_user("room-1", "user-1")
        with pytest.raises(TurnGateBlocked):
            await adapter.rename_thread("chat", "thread-1", "renamed")
        with pytest.raises(TurnGateBlocked):
            await adapter.rename_dm_topic("chat", "topic-1", "renamed")
        with pytest.raises(TurnGateBlocked):
            await adapter.send_read_receipt("chat", "m-1")
        with pytest.raises(TurnGateBlocked):
            await adapter.redact_message("chat", "m-1")
        with pytest.raises(TurnGateBlocked):
            await adapter.set_presence("online")

    assert adapter.handoff_calls == 0
    assert adapter.create_room_calls == 0
    assert adapter.invite_user_calls == 0
    assert adapter.rename_thread_calls == 0
    assert adapter.rename_dm_topic_calls == 0
    assert adapter.read_receipt_calls == 0
    assert adapter.redact_calls == 0
    assert adapter.presence_calls == 0


@pytest.mark.asyncio
async def test_platform_send_revalidates_generation_before_network_call():
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")
    send_calls = 0

    async def send(**kwargs):
        nonlocal send_calls
        send_calls += 1
        return SimpleNamespace(success=True)

    adapter = SimpleNamespace(send=send)
    identity = build_runtime_identity(
        surface="gateway-delivery",
        session_scope="delivery-session",
        turn_id="delivery-turn",
    )
    assert identity is not None
    with acquire_outer_turn(
        TurnGateRequest(
            entrypoint="gateway-delivery",
            purpose="business",
            identity=identity,
        )
    ):
        provider.validation_decision = GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id="lease-gateway-delivery",
            generation=27,
        )
        with pytest.raises(TurnGateBlocked, match="generation changed"):
            await BasePlatformAdapter._send_with_retry(
                adapter,
                chat_id="chat",
                content="must not leak",
            )
    assert send_calls == 0
