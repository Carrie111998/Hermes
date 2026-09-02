"""Gateway restart must re-arm persisted active heartbeat watches (#98298).

Heartbeat state survives restarts in SessionDB ``state_meta``, but the
in-memory ``_heartbeat_watch`` registry did not: after a restart an active
heartbeat was orphaned — ``/heartbeat status`` kept reporting "active, next
in ~Ns" while nothing could ever fire it. ``_restore_heartbeat_watches``
rebuilds watches from the session routing index on startup.

These tests pin the restore contract with a bare GatewayRunner (same
pattern as test_scale_to_zero_watcher.py): only active heartbeats on
entries that still carry an origin source are re-armed, and every failure
mode degrades to "no watches" instead of breaking startup.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource


def _source(chat_id: str = "111") -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")


def _entry(session_key: str, session_id: str, origin) -> SimpleNamespace:
    return SimpleNamespace(session_key=session_key, session_id=session_id, origin=origin)


class _StubStore:
    # GatewayRunner.async_session_store (a property) validates its cached
    # facade via ``facade._store is self.session_store`` — mirror that pair
    # so the stub survives the check instead of being replaced.
    _store = None

    def __init__(self, entries, error: Exception = None):
        self._entries = entries
        self._error = error

    async def list_sessions(self):
        if self._error is not None:
            raise self._error
        return list(self._entries)


def _runner(store) -> GatewayRunner:
    r = GatewayRunner.__new__(GatewayRunner)
    r.session_store = None
    r._heartbeat_watch = {}
    r._heartbeat_poll_task = None
    r._running_agents = {}
    r._background_tasks = set()
    r._async_session_store = store
    return r


async def _shutdown(r: GatewayRunner) -> None:
    task = getattr(r, "_heartbeat_poll_task", None)
    if task is not None and not task.done():
        task.cancel()
        import asyncio

        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restore_re_arms_active_heartbeat(monkeypatch):
    active = SimpleNamespace(
        status="active", prompt="Check CI", interval_seconds=600
    )
    paused = SimpleNamespace(
        status="paused", prompt="Check mail", interval_seconds=300
    )
    by_sid = {"sid-active": active, "sid-paused": paused}
    monkeypatch.setattr(
        "hermes_cli.heartbeat.load_heartbeat", lambda sid: by_sid.get(sid)
    )
    r = _runner(
        _StubStore(
            [
                _entry("telegram:dm:111", "sid-active", _source("111")),
                _entry("telegram:dm:222", "sid-paused", _source("222")),
            ]
        )
    )

    await r._restore_heartbeat_watches()
    try:
        assert set(r._heartbeat_watch) == {"telegram:dm:111"}
        source, sid = r._heartbeat_watch["telegram:dm:111"]
        assert sid == "sid-active"
        assert isinstance(source, SessionSource)
        assert source.chat_id == "111"
        # Registering a watch starts the gateway-wide poller.
        assert r._heartbeat_poll_task is not None and not r._heartbeat_poll_task.done()
    finally:
        await _shutdown(r)


@pytest.mark.asyncio
async def test_restore_skips_entries_without_origin(monkeypatch):
    active = SimpleNamespace(status="active", prompt="p", interval_seconds=60)
    monkeypatch.setattr(
        "hermes_cli.heartbeat.load_heartbeat", lambda sid: active if sid else None
    )
    r = _runner(
        _StubStore([_entry("telegram:dm:111", "sid-1", None)])
    )

    await r._restore_heartbeat_watches()
    try:
        assert r._heartbeat_watch == {}
    finally:
        await _shutdown(r)


@pytest.mark.asyncio
async def test_restore_survives_store_failure(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.heartbeat.load_heartbeat", lambda sid: None
    )
    r = _runner(_StubStore([], error=RuntimeError("state.db unavailable")))

    # A failing session store must not break gateway startup.
    await r._restore_heartbeat_watches()
    assert r._heartbeat_watch == {}


@pytest.mark.asyncio
async def test_restore_survives_db_read_failure(monkeypatch):
    def _boom(sid):
        raise RuntimeError("get_meta failed")

    monkeypatch.setattr("hermes_cli.heartbeat.load_heartbeat", _boom)
    r = _runner(
        _StubStore([_entry("telegram:dm:111", "sid-1", _source("111"))])
    )

    await r._restore_heartbeat_watches()
    try:
        assert r._heartbeat_watch == {}
    finally:
        await _shutdown(r)


@pytest.mark.asyncio
async def test_restart_end_to_end_delivers_active_without_resume(monkeypatch):
    """Startup path: after a restart, an active heartbeat delivers via the
    poller with no manual /heartbeat resume, while a paused one never
    registers a watch (#98298)."""
    active = SimpleNamespace(
        status="active", prompt="Check CI", interval_seconds=600
    )
    paused = SimpleNamespace(
        status="paused", prompt="Check mail", interval_seconds=300
    )
    by_sid = {"sid-active": active, "sid-paused": paused}
    monkeypatch.setattr(
        "hermes_cli.heartbeat.load_heartbeat", lambda sid: by_sid.get(sid)
    )
    # Restore starts the poller, so shorten its cadence before it closes
    # over POLL_SECONDS.
    monkeypatch.setattr("hermes_cli.heartbeat.POLL_SECONDS", 0.05)

    delivered = []

    class _DueManager:
        def __init__(self, session_id):
            self._sid = session_id

        def has_heartbeat(self):
            return True

        def due_prompt(self):
            # Fire once, then stay quiet: delivery cadence is the poller's
            # contract with the real HeartbeatManager, not this test's.
            if self._sid == "sid-active" and not delivered:
                return "Check CI"
            return None

    monkeypatch.setattr("hermes_cli.heartbeat.HeartbeatManager", _DueManager)

    r = _runner(
        _StubStore(
            [
                _entry("telegram:dm:111", "sid-active", _source("111")),
                _entry("telegram:dm:222", "sid-paused", _source("222")),
            ]
        )
    )

    async def _warm(_label):
        return None

    r._warm_goals_session_db = _warm
    r._adapter_for_source = lambda source: object()
    r._enqueue_fifo = lambda quick_key, event, adapter: delivered.append(
        (quick_key, event)
    )

    # Simulated restart: persisted state in the store above, empty
    # in-memory registry, restore called from the startup path.
    await r._restore_heartbeat_watches()
    try:
        assert set(r._heartbeat_watch) == {"telegram:dm:111"}
        for _ in range(200):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert [(key, event.text) for key, event in delivered] == [
            ("telegram:dm:111", "Check CI")
        ]
    finally:
        await _shutdown(r)


@pytest.mark.asyncio
async def test_repeated_restore_does_not_duplicate_registrations(monkeypatch):
    """Repeated startup/recovery passes must not stack registrations for
    the same session_key: the registry overwrites by key and the poller is
    a gateway-wide singleton."""
    active = SimpleNamespace(
        status="active", prompt="Check CI", interval_seconds=600
    )
    monkeypatch.setattr(
        "hermes_cli.heartbeat.load_heartbeat", lambda sid: active
    )
    r = _runner(_StubStore([_entry("telegram:dm:111", "sid-1", _source("111"))]))

    await r._restore_heartbeat_watches()
    try:
        first_task = r._heartbeat_poll_task
        assert first_task is not None and not first_task.done()

        await r._restore_heartbeat_watches()
        assert set(r._heartbeat_watch) == {"telegram:dm:111"}
        assert r._heartbeat_poll_task is first_task
    finally:
        await _shutdown(r)
