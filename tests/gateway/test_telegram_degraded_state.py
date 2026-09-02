"""Degraded Telegram connect must publish 'degraded', not 'connected' (#101391).

A reconnect whose polling did not start still called _mark_connected(),
so gateway_state.json reported connected for hours while getUpdates was
unclaimed — platform health checks and `hermes send` both read the lie.
Now: degraded connect publishes 'degraded' (with an error code), and the
first successful poll (_record_polling_progress) flips it back to
'connected'.
"""

import asyncio
from types import SimpleNamespace

import pytest

import plugins.platforms.telegram.adapter as tg_adapter


def _bare_adapter():
    a = object.__new__(tg_adapter.TelegramAdapter)
    # Only the state-machine attrs the tested methods touch.
    a._background_tasks = set()
    from gateway.platforms.base import Platform

    a.platform = Platform.TELEGRAM  # name/namespace lookups in status writes
    a._polling_generation = 1
    a._polling_conflict_recovery_generation = None
    a._polling_conflict_count = 0
    a._polling_network_error_count = 0
    a._polling_teardown_started = False
    a._polling_progress_accepting = True
    a._polling_progress_event = asyncio.Event()
    a._polling_last_progress_monotonic = 0.0
    a._send_path_degraded = True
    a._platform_state_degraded = False
    a._webhook_mode = False
    return a


class _RecordedWrites:
    def __init__(self):
        self.writes = []

    def __call__(self, context, **kwargs):
        self.writes.append((context, kwargs))


@pytest.fixture
def recorded(monkeypatch):
    rec = _RecordedWrites()
    monkeypatch.setattr(
        "gateway.platforms.base.BasePlatformAdapter._write_runtime_status_safe",
        rec,
    )
    return rec


def _bare_base():
    """Concrete stand-in satisfying the abstract surface."""
    from gateway.platforms.base import BasePlatformAdapter

    class _Minimal(BasePlatformAdapter):
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get_chat_info(self, chat_id):
            return {}

        async def send(self, chat_id, text, **kwargs):
            pass

    adapter = object.__new__(_Minimal)
    adapter.platform = None
    adapter._platform_state_degraded = False
    return adapter


class TestDegradedConnectState:
    def test_mark_connected_degraded_publishes_degraded_with_error(self, recorded):
        adapter = _bare_base()
        adapter._mark_connected_degraded("telegram_polling_not_started", "retrying")
        assert adapter._platform_state_degraded is True
        context, kwargs = recorded.writes[-1]
        assert kwargs["platform_state"] == "degraded"
        assert kwargs["error_code"] == "telegram_polling_not_started"
        assert kwargs["error_message"]

    def test_plain_mark_connected_clears_degraded_flag(self, recorded):
        adapter = _bare_base()
        adapter._platform_state_degraded = True
        adapter._mark_connected()
        assert adapter._platform_state_degraded is False
        context, kwargs = recorded.writes[-1]
        assert kwargs["platform_state"] == "connected"


class TestRecoveryFlip:
    def test_first_successful_poll_flips_degraded_to_connected(self, recorded):
        adapter = _bare_adapter()
        # Degraded connect already happened.
        adapter._platform_state_degraded = True

        adapter._record_polling_progress(generation=1)

        assert adapter._platform_state_degraded is False, (
            "first successful getUpdates must end the degraded state (#101391)"
        )
        context, kwargs = recorded.writes[-1]
        assert kwargs["platform_state"] == "connected"
        assert kwargs["error_code"] is None

    def test_progress_while_healthy_does_not_restamp(self, recorded):
        adapter = _bare_adapter()
        adapter._platform_state_degraded = False

        adapter._record_polling_progress(generation=1)
        count_after_first = len(recorded.writes)

        adapter._record_polling_progress(generation=1)
        assert len(recorded.writes) == count_after_first, (
            "healthy polls must not re-write the state file"
        )

    def test_progress_from_stale_generation_does_not_flip(self, recorded):
        adapter = _bare_adapter()
        adapter._platform_state_degraded = True
        adapter._polling_generation = 7  # current gen is 7...

        adapter._record_polling_progress(generation=3)  # ...3 is stale

        assert adapter._platform_state_degraded is True, (
            "a stale generation's progress must not clear the degraded state"
        )
        assert recorded.writes == []


class TestConnectFlowStamping:
    """The connect() flow: polling_started=False must not reach
    _mark_connected(). Verified by source shape so the branch wiring is
    pinned without driving the full 11k-line connect()."""

    def test_degraded_polling_skips_plain_mark_connected(self):
        import inspect

        src = inspect.getsource(tg_adapter.TelegramAdapter.connect)
        # The degraded stamp happens inside the polling branch...
        assert "_mark_connected_degraded(" in src
        # ...and the post-branch stamp is guarded by polling_started.
        assert "if not polling_started and not self._webhook_mode:" in src

    def test_polling_started_initialized_for_webhook_mode(self):
        """polling_started must be defined before the webhook/polling split
        so the guard cannot NameError in webhook mode."""
        import inspect

        src = inspect.getsource(tg_adapter.TelegramAdapter.connect)
        init_at = src.index("polling_started = True")
        webhook_at = src.index("if webhook_url:")
        assert init_at < webhook_at, (
            "polling_started must be initialized before the webhook branch"
        )
