from __future__ import annotations

import asyncio
import logging
import types
from unittest.mock import MagicMock

import pytest

from hermes_cli import postgres_hot_read_adapter as adapter


class InlineThread:
    def __init__(self, *, target, name: str, daemon: bool) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self.target()


def test_default_off_does_not_touch_sqlite_or_start_thread(monkeypatch) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    started: list[object] = []

    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={},
        _thread_factory=lambda **kwargs: started.append(kwargs),
    ) is False
    db.get_messages.assert_not_called()
    assert started == []


def test_enabled_without_dsn_fails_open_without_touching_sqlite(caplog) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    caplog.set_level(logging.WARNING)

    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={runtime.SHADOW_ENABLED_ENV: "1"},
    ) is False
    db.get_messages.assert_not_called()
    assert "DSN is unavailable" in caplog.text


def test_non_literal_gate_value_stays_disabled() -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={
            runtime.SHADOW_ENABLED_ENV: "true",
            runtime.SHADOW_DSN_ENV: "postgresql://private",
        },
    ) is False
    db.get_messages.assert_not_called()


def test_enabled_snapshots_bounded_sqlite_page_before_daemon_thread(monkeypatch) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    rows = [{column: None for column in adapter.MESSAGE_COLUMNS}]
    db.get_messages.return_value = rows
    captured: dict[str, object] = {}

    async def fake_shadow_once(dsn: str, request, sqlite_rows) -> None:
        captured.update(dsn=dsn, request=request, rows=sqlite_rows)

    monkeypatch.setattr(runtime, "_shadow_once", fake_shadow_once)
    threads: list[InlineThread] = []

    def factory(**kwargs) -> InlineThread:
        thread = InlineThread(**kwargs)
        threads.append(thread)
        return thread

    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={
            runtime.SHADOW_ENABLED_ENV: "1",
            runtime.SHADOW_DSN_ENV: "postgresql://private",
        },
        now_epoch_s=100_000.0,
        _thread_factory=factory,
    ) is True

    db.get_messages.assert_called_once_with(
        "session-1",
        include_inactive=False,
        limit=adapter.MAX_LIMIT,
        offset=0,
        since_timestamp=13_600.0,
    )
    assert threads[0].daemon is True
    assert captured["dsn"] == "postgresql://private"
    assert captured["rows"] is not rows
    request = captured["request"]
    assert request == adapter.HotReadRequest(
        session_id="session-1",
        cutoff_epoch_s=13_600.0,
        limit=adapter.MAX_LIMIT,
        offset=0,
        include_inactive=False,
    )


def test_single_flight_skips_when_observer_is_busy(monkeypatch) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    assert runtime._SHADOW_SLOT.acquire(blocking=False)
    try:
        assert runtime.observe_sqlite_session(
            db,
            "session-1",
            environ={
                runtime.SHADOW_ENABLED_ENV: "1",
                runtime.SHADOW_DSN_ENV: "postgresql://private",
            },
        ) is False
    finally:
        runtime._SHADOW_SLOT.release()
    db.get_messages.assert_not_called()


def test_worker_failure_is_redacted_and_releases_slot(monkeypatch, caplog) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    secret = "postgresql://user:do-not-log@example.invalid/db"
    db = MagicMock()
    db.get_messages.return_value = []

    async def fail(*_args) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(runtime, "_shadow_once", fail)
    caplog.set_level(logging.WARNING)

    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={
            runtime.SHADOW_ENABLED_ENV: "1",
            runtime.SHADOW_DSN_ENV: secret,
        },
        _thread_factory=lambda **kwargs: InlineThread(**kwargs),
    ) is True
    assert "worker failed open" in caplog.text
    assert secret not in caplog.text
    assert runtime._SHADOW_SLOT.acquire(blocking=False)
    runtime._SHADOW_SLOT.release()


def test_thread_start_failure_releases_slot_and_redacts(monkeypatch, caplog) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    secret = "postgresql://user:do-not-log@example.invalid/db"
    db = MagicMock()
    db.get_messages.return_value = []

    class BrokenThread:
        def start(self) -> None:
            raise RuntimeError(secret)

    caplog.set_level(logging.WARNING)
    assert runtime.observe_sqlite_session(
        db,
        "session-1",
        environ={
            runtime.SHADOW_ENABLED_ENV: "1",
            runtime.SHADOW_DSN_ENV: secret,
        },
        _thread_factory=lambda **_kwargs: BrokenThread(),
    ) is False
    assert "setup failed open" in caplog.text
    assert secret not in caplog.text
    assert runtime._SHADOW_SLOT.acquire(blocking=False)
    runtime._SHADOW_SLOT.release()


def test_keyboard_interrupt_propagates_and_releases_slot() -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime

    db = MagicMock()
    db.get_messages.return_value = []

    class InterruptThread:
        def start(self) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runtime.observe_sqlite_session(
            db,
            "session-1",
            environ={
                runtime.SHADOW_ENABLED_ENV: "1",
                runtime.SHADOW_DSN_ENV: "postgresql://private",
            },
            _thread_factory=lambda **_kwargs: InterruptThread(),
        )
    assert runtime._SHADOW_SLOT.acquire(blocking=False)
    runtime._SHADOW_SLOT.release()


@pytest.mark.asyncio
async def test_shadow_once_connects_once_and_closes_with_sanitized_metadata(monkeypatch, caplog) -> None:
    from hermes_cli import postgres_hot_shadow_runtime as runtime
    from hermes_cli.postgres_hot_migration import TargetConfig

    connect_calls: list[dict[str, object]] = []

    class Connection:
        async def close(self, *, timeout: float) -> None:
            assert timeout == runtime.CLOSE_TIMEOUT_SECONDS

        def terminate(self) -> None:
            raise AssertionError("terminate should not be needed")

    conn = Connection()

    async def connect(**kwargs):
        connect_calls.append(kwargs)
        return conn

    target = TargetConfig("db.example", 5432, "role", "private", "neondb", True)
    monkeypatch.setattr(runtime, "parse_target_dsn", lambda _dsn: target)
    monkeypatch.setitem(__import__("sys").modules, "asyncpg", types.SimpleNamespace(connect=connect))

    async def compare(**_kwargs):
        return adapter.ShadowComparison(
            adapter.ShadowOutcome.MATCH,
            {"hot_status": "ok", "sqlite_row_count": 1, "postgres_row_count": 1},
        )

    monkeypatch.setattr(runtime, "compare_shadow_messages", compare)
    caplog.set_level(logging.INFO)
    request = adapter.HotReadRequest("session-1", 1.0, 100, 0, False)

    await runtime._shadow_once("postgresql://private", request, tuple())

    assert len(connect_calls) == 1
    assert connect_calls[0]["password"] == "private"
    assert "outcome=match" in caplog.text
    assert "postgresql://private" not in caplog.text
