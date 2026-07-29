"""Concurrency regressions for secure skill secret-capture routing."""

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import pytest

from tools.skills_tool import (
    _capture_required_environment_variables,
    _get_secret_capture_callback,
    bind_secret_capture_callback,
    reset_secret_capture_callback,
    set_secret_capture_callback,
)
from tools.thread_context import propagate_context_to_thread


def _missing_entry(name: str) -> list[dict[str, str]]:
    return [{"name": name, "prompt": "[REDACTED]"}]


def test_secret_capture_callbacks_are_isolated_between_concurrent_sessions(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    names = {
        "session-a": "HERMES_TEST_SECRET_CAPTURE_A",
        "session-b": "HERMES_TEST_SECRET_CAPTURE_B",
    }
    for name in names.values():
        monkeypatch.delenv(name, raising=False)

    barrier = threading.Barrier(2)
    observed = []
    observed_lock = threading.Lock()

    def run_session(label: str):
        def callback(var_name, _prompt, metadata=None):
            with observed_lock:
                observed.append((label, var_name, metadata["skill_name"]))
            return {"success": True, "skipped": True}

        assert set_secret_capture_callback(callback) is None
        try:
            barrier.wait(timeout=5)
            result = _capture_required_environment_variables(
                label, _missing_entry(names[label])
            )
            assert _get_secret_capture_callback() is callback
            return result
        finally:
            set_secret_capture_callback(None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run_session, names))

    assert set(observed) == {
        ("session-a", names["session-a"], "session-a"),
        ("session-b", names["session-b"], "session-b"),
    }
    assert all(result["setup_skipped"] is True for result in results)
    assert _get_secret_capture_callback() is None


def test_secret_capture_context_propagates_to_worker_and_does_not_leak(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    var_name = "HERMES_TEST_SECRET_CAPTURE_WORKER"
    monkeypatch.delenv(var_name, raising=False)
    caller_thread = threading.get_ident()
    callback_threads = []

    def callback(_var_name, _prompt, metadata=None):
        callback_threads.append((threading.get_ident(), metadata["skill_name"]))
        return {"success": True, "skipped": True}

    token = bind_secret_capture_callback(callback)
    try:
        worker_call = propagate_context_to_thread(
            lambda: _capture_required_environment_variables(
                "worker-session", _missing_entry(var_name)
            )
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(worker_call).result(timeout=5)
            worker_after = pool.submit(_get_secret_capture_callback).result(timeout=5)
    finally:
        reset_secret_capture_callback(token)

    assert result["setup_skipped"] is True
    assert callback_threads == [(callback_threads[0][0], "worker-session")]
    assert callback_threads[0][0] != caller_thread
    assert worker_after is None
    assert _get_secret_capture_callback() is None


def test_secret_capture_binding_restores_outer_context_after_exception(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    var_name = "HERMES_TEST_SECRET_CAPTURE_FAILURE"
    monkeypatch.delenv(var_name, raising=False)

    def outer_callback(*_args, **_kwargs):
        return {"success": True, "skipped": True}

    def failing_callback(*_args, **_kwargs):
        raise RuntimeError("synthetic callback failure")

    outer_token = bind_secret_capture_callback(outer_callback)
    try:
        inner_token = bind_secret_capture_callback(failing_callback)
        try:
            result = _capture_required_environment_variables(
                "failing-session", _missing_entry(var_name)
            )
            assert result["setup_skipped"] is True
            assert result["missing_names"] == [var_name]
        finally:
            reset_secret_capture_callback(inner_token)

        assert _get_secret_capture_callback() is outer_callback
    finally:
        reset_secret_capture_callback(outer_token)

    assert _get_secret_capture_callback() is None


def test_worker_context_is_clean_after_timeout_and_interrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    def callback(*_args, **_kwargs):
        return {"success": True, "skipped": True}

    entered = threading.Event()
    release = threading.Event()

    def blocked_worker():
        assert _get_secret_capture_callback() is callback
        entered.set()
        assert release.wait(timeout=5)

    def interrupted_worker():
        assert _get_secret_capture_callback() is callback
        raise KeyboardInterrupt("synthetic interrupt")

    token = bind_secret_capture_callback(callback)
    try:
        blocked_call = propagate_context_to_thread(blocked_worker)
        interrupted_call = propagate_context_to_thread(interrupted_worker)
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked_future = pool.submit(blocked_call)
            assert entered.wait(timeout=5)
            with pytest.raises(FutureTimeout):
                blocked_future.result(timeout=0.01)
            release.set()
            blocked_future.result(timeout=5)
            assert pool.submit(_get_secret_capture_callback).result(timeout=5) is None

            with pytest.raises(KeyboardInterrupt, match="synthetic interrupt"):
                pool.submit(interrupted_call).result(timeout=5)
            assert pool.submit(_get_secret_capture_callback).result(timeout=5) is None
    finally:
        release.set()
        reset_secret_capture_callback(token)

    assert _get_secret_capture_callback() is None
