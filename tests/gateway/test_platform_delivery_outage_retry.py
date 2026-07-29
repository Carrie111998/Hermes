import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms import base as base_mod
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _RetryAdapter(BasePlatformAdapter):
    def __init__(self, outcomes, *, outage_cfg=None):
        super().__init__(
            PlatformConfig(extra={"network_outage_retry": outage_cfg or {}}),
            Platform.TELEGRAM,
        )
        self.outcomes = list(outcomes)
        self.calls = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        if self.outcomes:
            return self.outcomes.pop(0)
        return SendResult(success=True, message_id="ok")

    async def get_chat_info(self, chat_id: str):
        return {"name": str(chat_id), "type": "dm"}


@pytest.mark.asyncio
async def test_delivery_network_outage_retries_past_bounded_retry_budget(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(base_mod.asyncio, "sleep", fake_sleep)

    adapter = _RetryAdapter(
        [
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=True, message_id="delivered"),
        ],
        outage_cfg={
            "enabled": True,
            "active_window_seconds": 60,
            "sleep_seconds": 300,
        },
    )

    result = await adapter._send_with_retry("chat", "answer", max_retries=1, base_delay=0)

    assert result.success is True
    assert result.message_id == "delivered"
    assert len(adapter.calls) == 4
    assert sleeps == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_delivery_network_outage_uses_active_window_then_sleep_cycle(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)
    clock = {"now": 0.0}
    sleeps = []

    def fake_monotonic():
        return clock["now"]

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(base_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(base_mod.asyncio, "sleep", fake_sleep)

    adapter = _RetryAdapter(
        [
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=False, error="ConnectionError: network down", retryable=True),
            SendResult(success=True, message_id="delivered-after-sleep"),
        ],
        outage_cfg={
            "enabled": True,
            "active_window_seconds": 3,
            "sleep_seconds": 5,
        },
    )

    result = await adapter._send_with_retry("chat", "answer", max_retries=1, base_delay=2)

    assert result.success is True
    assert result.message_id == "delivered-after-sleep"
    assert len(adapter.calls) == 4
    assert sleeps == [2.0, 1.0, 5.0, 2.0]


@pytest.mark.asyncio
async def test_delivery_network_outage_honors_finite_max_wait(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)
    clock = {"now": 0.0}
    sleeps = []

    monkeypatch.setattr(base_mod.time, "monotonic", lambda: clock["now"])

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(base_mod.asyncio, "sleep", fake_sleep)
    adapter = _RetryAdapter(
        [
            SendResult(success=False, error="ConnectError: network down", retryable=True),
            SendResult(success=False, error="ConnectError: network down", retryable=True),
            SendResult(success=False, error="ConnectError: network down", retryable=True),
        ],
        outage_cfg={
            "enabled": True,
            "active_window_seconds": 10,
            "sleep_seconds": 5,
            "max_wait_seconds": 3,
        },
    )

    result = await adapter._send_with_retry(
        "chat", "answer", max_retries=0, base_delay=2
    )

    assert result.success is False
    assert len(adapter.calls) == 3
    assert sleeps == [2.0, 1.0]


@pytest.mark.asyncio
async def test_delivery_generic_timeout_does_not_retry_or_fallback(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)

    async def fail_if_sleep(_seconds):  # pragma: no cover - test fails if called
        raise AssertionError("timeout path should not sleep/retry")

    monkeypatch.setattr(base_mod.asyncio, "sleep", fail_if_sleep)
    adapter = _RetryAdapter(
        [SendResult(success=False, error="Timed out while sending", retryable=False)],
        outage_cfg={"enabled": True, "active_window_seconds": 3, "sleep_seconds": 5},
    )

    result = await adapter._send_with_retry("chat", "answer", max_retries=5, base_delay=0)

    assert result.success is False
    assert result.error == "Timed out while sending"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_delivery_ambiguous_disconnect_never_enters_long_retry(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(base_mod.asyncio, "sleep", fake_sleep)
    adapter = _RetryAdapter(
        [
            SendResult(
                success=False,
                error="RemoteProtocolError: server disconnected without response",
                retryable=True,
            ),
            SendResult(
                success=False,
                error="RemoteProtocolError: server disconnected without response",
                retryable=True,
            ),
            SendResult(success=False, error="notice path unavailable"),
        ],
        outage_cfg={"enabled": True, "active_window_seconds": 3, "sleep_seconds": 5},
    )

    result = await adapter._send_with_retry(
        "chat", "answer", max_retries=1, base_delay=0
    )

    assert result.success is False
    assert len(adapter.calls) == 3  # initial + bounded retry + one failure notice
    assert sleeps == [0.0]


@pytest.mark.asyncio
async def test_delivery_rate_limit_does_not_enter_outage_retry(monkeypatch):
    monkeypatch.setattr(base_mod.random, "uniform", lambda _a, _b: 0.0)

    async def fail_if_sleep(_seconds):  # pragma: no cover - test fails if called
        raise AssertionError("rate-limit path should not sleep/retry")

    monkeypatch.setattr(base_mod.asyncio, "sleep", fail_if_sleep)
    adapter = _RetryAdapter(
        [
            SendResult(
                success=False,
                error="Too Many Requests: retry after 30",
                retryable=False,
                error_kind="rate_limited",
            ),
            SendResult(
                success=False,
                error="Too Many Requests: retry after 30",
                retryable=False,
                error_kind="rate_limited",
            ),
        ],
        outage_cfg={"enabled": True, "active_window_seconds": 3, "sleep_seconds": 5},
    )

    result = await adapter._send_with_retry("chat", "answer", max_retries=5, base_delay=0)

    assert result.success is False
    assert len(adapter.calls) == 2  # original + existing plain-text fallback only
    assert adapter.calls[1]["content"].startswith("(Response formatting failed")


def test_gateway_config_bridges_agent_outage_policy_to_platforms(
    tmp_path, monkeypatch
):
    from gateway import config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(config_mod, "get_hermes_home", lambda: home)
    (home / "config.yaml").write_text(
        """
agent:
  network_outage_retry:
    enabled: true
    interval_seconds: 17
    max_wait_seconds: 0
platforms:
  telegram:
    enabled: true
    token: 123456:test-token
""".strip(),
        encoding="utf-8",
    )

    config = config_mod.load_gateway_config()
    policy = config.platforms[Platform.TELEGRAM].extra["network_outage_retry"]
    assert policy == {
        "enabled": True,
        "interval_seconds": 17,
        "max_wait_seconds": 0,
    }
