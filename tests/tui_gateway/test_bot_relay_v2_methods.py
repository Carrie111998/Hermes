"""Behavior tests for v2 target admission and durable recipient receipts."""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

import tui_gateway.server as srv
from tools import bot_relay
from tools.bot_mode_dm import MESSAGE_MAX_CHARS


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".hermes"
    (path / "profiles" / "ops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


def _result(envelope: dict) -> dict:
    assert "error" not in envelope, envelope
    return envelope["result"]


def _v2_params(event_id: str = "a" * 32, **overrides) -> dict:
    params = {
        "id": event_id,
        "profile": "ops",
        "body": "ping",
        "message": "legacy attribution must be ignored",
        "from_profile": "default",
        "from_handle": "hermes",
        "source_install_id": "source-install",
        "target_install_id": "",
        "courier_namespace_id": "desktop-namespace-a",
    }
    params.update(overrides)
    return params


class _Proc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode


def _successful_runner(calls: list[dict], *, reply: str = "pong"):
    def run(argv, **kwargs):
        query_path = Path(argv[-1])
        calls.append({"argv": argv, "message": query_path.read_text(encoding="utf-8")})
        kwargs["stdout"].write(reply)
        return _Proc()

    return run


def test_v2_recipient_receipt_replays_without_a_second_bot_turn(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("subprocess.run", _successful_runner(calls))
    params = _v2_params()

    first = _result(srv._methods["bot_relay.deliver"](1, params))
    assert first == {
        "protocol_version": 2,
        "durable_receipt": True,
        "event_id": params["id"],
        "status": "completed",
        "reply": "pong",
        "error": "",
    }
    assert calls[0]["message"] == "Message from 🤖 hermes (@hermes): ping"
    query_path = Path(calls[0]["argv"][-1])
    assert not query_path.exists()

    replay = _result(srv._methods["bot_relay.deliver"](2, dict(params)))
    assert replay["protocol_version"] == 2
    assert replay["durable_receipt"] is True
    assert replay["deduplicated"] is True
    assert replay["status"] == "completed"
    assert replay["reply"] == "pong"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"body": "different body"},
        {"from_handle": "forged-sender"},
    ],
)
def test_v2_recipient_rejects_same_event_with_different_payload_or_identity(
    home: Path, monkeypatch: pytest.MonkeyPatch, change: dict
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("subprocess.run", _successful_runner(calls))
    params = _v2_params()
    _result(srv._methods["bot_relay.deliver"](1, params))

    conflict = srv._methods["bot_relay.deliver"](2, {**params, **change})
    assert conflict["error"]["code"] == 4098
    assert any(
        phrase in conflict["error"]["message"].lower()
        for phrase in ("conflict", "different content")
    )
    assert len(calls) == 1


def test_v2_target_install_fence_fails_closed_before_admission(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("identity mismatch reached the Bot turn")

    monkeypatch.setattr("subprocess.run", must_not_run)
    monkeypatch.setattr(
        "hermes_cli.web_server.get_install_id", lambda: "actual-install"
    )
    mismatch = srv._methods["bot_relay.deliver"](
        1,
        _v2_params("9" * 32, target_install_id="different-install"),
    )
    assert mismatch["error"]["code"] == 4097

    monkeypatch.setattr("hermes_cli.web_server.get_install_id", lambda: "")
    unavailable = srv._methods["bot_relay.deliver"](
        2,
        _v2_params("8" * 32, target_install_id="expected-install"),
    )
    assert unavailable["error"]["code"] == 5094


def test_concurrent_duplicate_observes_processing_and_never_starts_twice(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = Event()
    release = Event()
    call_lock = Lock()
    subprocess_calls = 0

    def blocked_run(argv, **kwargs):
        nonlocal subprocess_calls
        with call_lock:
            subprocess_calls += 1
        entered.set()
        assert release.wait(timeout=5)
        kwargs["stdout"].write("done")
        return _Proc()

    monkeypatch.setattr("subprocess.run", blocked_run)
    params = _v2_params("b" * 32)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(srv._methods["bot_relay.deliver"], 1, params)
        assert entered.wait(timeout=5)
        duplicate = _result(
            srv._methods["bot_relay.deliver"](2, dict(params))
        )
        try:
            assert duplicate["protocol_version"] == 2
            assert duplicate["durable_receipt"] is True
            assert duplicate["deduplicated"] is True
            assert duplicate["status"] == "processing"
            assert duplicate["retry_after_seconds"] == 300
            assert subprocess_calls == 1
        finally:
            release.set()
        first = _result(first_future.result(timeout=5))

    assert first["status"] == "completed"
    assert subprocess_calls == 1


def test_one_profile_lane_serializes_distinct_events(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = Event()
    release = Event()
    calls: list[str] = []

    def blocked_first(argv, **kwargs):
        calls.append(Path(argv[-1]).read_text(encoding="utf-8"))
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        kwargs["stdout"].write("done")
        return _Proc()

    monkeypatch.setattr("subprocess.run", blocked_first)
    first_params = _v2_params("1" * 32, body="first")
    second_params = _v2_params("2" * 32, body="second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            srv._methods["bot_relay.deliver"], 1, first_params
        )
        assert entered.wait(timeout=5)
        busy = _result(srv._methods["bot_relay.deliver"](2, second_params))
        assert busy["status"] == "processing"
        assert len(calls) == 1
        release.set()
        assert _result(first_future.result(timeout=5))["status"] == "completed"

    second = _result(srv._methods["bot_relay.deliver"](3, second_params))
    assert second["status"] == "completed"
    assert len(calls) == 2
    assert calls[1] == "Message from 🤖 hermes (@hermes): second"


def test_expired_processing_receipt_becomes_indeterminate_without_reexecution(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = _v2_params("c" * 32)
    admission = bot_relay.begin_recipient_delivery(
        home,
        event_id=params["id"],
        target_profile="ops",
        body=params["body"],
        from_profile=params["from_profile"],
        from_handle=params["from_handle"],
        source_install_id=params["source_install_id"],
        target_install_id=params["target_install_id"],
        courier_namespace_id=params["courier_namespace_id"],
        now=time.time() - bot_relay.DELIVERY_PROCESSING_SECONDS - 5,
    )
    assert admission["action"] == "execute"

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("an indeterminate tool-capable turn was re-executed")

    monkeypatch.setattr("subprocess.run", must_not_run)
    result = _result(srv._methods["bot_relay.deliver"](1, params))
    assert result["protocol_version"] == 2
    assert result["durable_receipt"] is True
    assert result["deduplicated"] is True
    assert result["status"] == "indeterminate"
    assert result["reply"] == ""


def test_v1_delivery_without_event_id_keeps_legacy_shape(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        "subprocess.run", _successful_runner(calls, reply="legacy pong")
    )
    result = _result(
        srv._methods["bot_relay.deliver"](
            1, {"profile": "ops", "message": "legacy ping"}
        )
    )
    assert result == {"reply": "legacy pong"}
    assert calls[0]["message"] == "legacy ping"


def test_v2_input_and_output_are_bounded_and_oversize_result_is_replayed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def oversized_run(_argv, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["stdout"].write("x" * 200_001)
        return _Proc()

    monkeypatch.setattr("subprocess.run", oversized_run)
    too_large = srv._methods["bot_relay.deliver"](
        1,
        _v2_params("d" * 32, body="x" * (MESSAGE_MAX_CHARS + 1)),
    )
    assert too_large["error"]["code"] == 4091
    assert calls == 0

    params = _v2_params("e" * 32)
    first = _result(srv._methods["bot_relay.deliver"](2, params))
    assert first["status"] == "failed"
    assert "exceeded" in first["error"]
    assert first["reply"] == ""
    assert calls == 1

    replay = _result(srv._methods["bot_relay.deliver"](3, dict(params)))
    assert replay["status"] == "failed"
    assert replay["deduplicated"] is True
    assert "exceeded" in replay["error"]
    assert calls == 1


def test_v2_timeout_is_durably_indeterminate_not_a_blind_retry(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def timeout(_argv, **_kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=600)

    monkeypatch.setattr("subprocess.run", timeout)
    params = _v2_params("f" * 32)
    first = _result(srv._methods["bot_relay.deliver"](1, params))
    assert first["status"] == "indeterminate"
    assert "timed out" in first["error"]
    replay = _result(srv._methods["bot_relay.deliver"](2, dict(params)))
    assert replay["status"] == "indeterminate"
    assert replay["deduplicated"] is True
    assert calls == 1


def test_v2_delivery_uses_the_canonical_finish_result(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("subprocess.run", _successful_runner(calls, reply="optimistic"))
    monkeypatch.setattr(
        "tools.bot_relay.finish_recipient_delivery",
        lambda *_args, **_kwargs: {
            "action": "cached",
            "status": "failed",
            "result": {
                "status": "failed",
                "reply": "",
                "error": "canonical durable failure",
            },
        },
    )

    result = _result(srv._methods["bot_relay.deliver"](1, _v2_params("0" * 32)))
    assert result["status"] == "failed"
    assert result["reply"] == ""
    assert result["error"] == "canonical durable failure"
    assert len(calls) == 1


def test_v2_finish_failure_is_not_reported_as_a_retryable_timeout(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def runner(_argv, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["stdout"].write("reply")
        return _Proc()

    monkeypatch.setattr("subprocess.run", runner)
    monkeypatch.setattr(
        "tools.bot_relay.finish_recipient_delivery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    response = srv._methods["bot_relay.deliver"](1, _v2_params("9" * 32))
    assert response["error"]["code"] == 5094
    assert "finalization unavailable" in response["error"]["message"]
    assert calls == 1


def test_outbox_nack_requires_a_json_boolean_retryable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_nack(root, **kwargs):
        calls.append(kwargs)
        return {"state": "failed"}

    monkeypatch.setattr("tools.bot_relay.nack_envelope", fake_nack)
    base = {
        "id": "1" * 32,
        "courier_id": "courier",
        "lease_token": "token",
        "lease_generation": 1,
        "error": "no",
    }
    for malformed in ("false", 0, 1, None):
        response = srv._methods["bot_relay.outbox.nack"](
            1, {**base, "retryable": malformed}
        )
        assert response["error"]["code"] == 4096
    assert calls == []

    for valid in (False, True):
        response = srv._methods["bot_relay.outbox.nack"](
            2, {**base, "retryable": valid}
        )
        assert "result" in response
    assert [call["retryable"] for call in calls] == [False, True]
