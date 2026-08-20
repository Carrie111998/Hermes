import pytest
from plugins.notifiers import discover_notifier_providers, load_notifier_provider
from plugins.notifiers.gateway import GatewayNotifierProvider
from unittest.mock import AsyncMock, MagicMock

def test_discover_notifier_providers():
    providers = discover_notifier_providers()
    names = [p[0] for p in providers]
    assert "gateway" in names

def test_load_invalid_provider():
    provider = load_notifier_provider("nonexistent_provider_12345")
    assert provider is None

def test_load_gateway_provider():
    provider = load_notifier_provider("gateway")
    assert provider is not None
    assert isinstance(provider, GatewayNotifierProvider)

@pytest.mark.asyncio
async def test_gateway_notifier_provider_delivery():
    provider = GatewayNotifierProvider()
    provider.initialize()

    mock_adapter = AsyncMock()
    mock_adapter.send = AsyncMock(return_value=MagicMock(success=True))
    
    mock_runner = MagicMock()
    mock_task = MagicMock()
    mock_task.title = "Test Task"
    mock_task.status = "done"

    from types import SimpleNamespace
    events = [SimpleNamespace(kind="completed", payload={"summary": "Task completed successfully"})]
    sub = {"task_id": "test_task_1", "platform": "telegram", "chat_id": "123", "thread_id": ""}

    sub_fail_counts = {}
    
    success = await provider.deliver_kanban_event(
        events=events,
        subscription=sub,
        task=mock_task,
        board_slug="default",
        adapter=mock_adapter,
        gateway_runner=mock_runner,
        sub_key=("test_task_1", "telegram", "123", ""),
        sub_fail_counts=sub_fail_counts,
        max_send_failures=12
    )

    assert success is True
    assert mock_adapter.send.called
    call_args = mock_adapter.send.call_args[0]
    call_kwargs = mock_adapter.send.call_args[1]
    assert call_args[0] == "123"
    assert "Task completed successfully" in call_args[1]

@pytest.mark.asyncio
async def test_gateway_notifier_provider_delivery_failure():
    provider = GatewayNotifierProvider()
    provider.initialize()

    mock_adapter = AsyncMock()
    # Simulate an adapter that returns success=False
    mock_adapter.send = AsyncMock(return_value=MagicMock(success=False))
    
    mock_runner = MagicMock()
    mock_task = MagicMock()
    mock_task.title = "Test Task"

    from types import SimpleNamespace
    events = [SimpleNamespace(kind="completed", payload={"summary": "Task completed"})]
    sub = {"task_id": "test_task_1", "platform": "telegram", "chat_id": "123", "thread_id": ""}

    success = await provider.deliver_kanban_event(
        events=events,
        subscription=sub,
        task=mock_task,
        board_slug="default",
        adapter=mock_adapter,
        gateway_runner=mock_runner,
        sub_key=("test_task_1", "telegram", "123", ""),
        sub_fail_counts={},
        max_send_failures=12
    )

    # Provider should return False so the watcher can increment the failure count
    assert success is False
