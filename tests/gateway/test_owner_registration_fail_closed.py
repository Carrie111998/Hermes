from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import active_sessions


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = {}
    runner._is_session_running = lambda _key: False
    runner._active_session_limit_message = lambda _key: None
    return runner


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat",
        user_id="user",
    )


def test_gateway_owner_registration_exception_rejects_turn(monkeypatch):
    def fail_registration(**_kwargs):
        raise OSError("registry unavailable")

    monkeypatch.setattr(active_sessions, "try_acquire_active_session", fail_registration)

    lease, message = _runner()._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "ownership" in message.lower()


def test_gateway_corrupt_owner_registry_rejects_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "active_sessions.json").write_text("{corrupt", encoding="utf-8")

    lease, message = _runner()._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "ownership" in message.lower()


def test_gateway_missing_process_start_identity_rejects_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda _pid: None)

    lease, message = _runner()._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "ownership" in message.lower()


def test_gateway_registry_lock_failure_rejects_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fail_lock(_self):
        raise OSError("lock unavailable")

    monkeypatch.setattr(active_sessions._FileLock, "__enter__", fail_lock)

    lease, message = _runner()._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "ownership" in message.lower()


def test_gateway_missing_owner_lease_rejects_turn(monkeypatch):
    monkeypatch.setattr(
        active_sessions,
        "try_acquire_active_session",
        lambda **_kwargs: (None, None),
    )

    lease, message = _runner()._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "ownership" in message.lower()


def test_gateway_running_race_rejects_duplicate_turn():
    runner = _runner()
    runner._is_session_running = lambda session_key: True

    lease, message = runner._claim_active_session_slot("session", _source())

    assert lease is None
    assert message is not None
    assert "already active" in message.lower()
