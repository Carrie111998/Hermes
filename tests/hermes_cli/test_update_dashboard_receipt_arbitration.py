from __future__ import annotations

import asyncio
import builtins
import os

import pytest

pytest.importorskip("fastapi")

import hermes_cli.web_server as web_server


TERMINAL_OUTCOMES = (
    ("success", 0),
    ("partial", 1),
    ("failed", 1),
    ("refused", 2),
)


def _receipt(
    *,
    correlation_id: str | None,
    outcome: str = "refused",
    started_at: str = "2026-08-23T12:00:00+00:00",
    finished_at: str = "2026-08-23T12:00:01+00:00",
) -> dict:
    return {
        "outcome": outcome,
        "correlation_id": correlation_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "stop_reason": (
            "image_managed_update_refused" if outcome == "refused" else None
        ),
        "refusal": (
            {
                "code": "image_managed_update_refused",
                "message": "pull the image and recreate the runtime",
                "update_command": "docker compose pull && docker compose up -d",
            }
            if outcome == "refused"
            else None
        ),
        "pre_sha": None,
        "post_sha": None,
        "post_version": None,
        "fleet_states": [],
    }


def _write_update_completion(log_dir, action_id: str) -> None:
    (log_dir / "update.log").write_text(
        "=== hermes update started 2026-08-23 12:00:00 ===\n"
        f"=== hermes-update completed {action_id} ===\n",
        encoding="utf-8",
    )


def _write_action_log(log_dir, *lines: str) -> None:
    (log_dir / "hermes-update.log").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _correlated_start(action_id: str) -> tuple[str, str]:
    return (
        "=== hermes-update started 2026-08-23 12:00:00 ===",
        f"=== hermes-update action {action_id} started ===",
    )


@pytest.fixture(autouse=True)
def _isolated_action_state(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", log_dir)
    web_server._ACTION_PROCS.clear()
    web_server._ACTION_COMMANDS.clear()
    web_server._ACTION_IDS.clear()
    web_server._ACTION_RESULTS.clear()
    yield log_dir
    web_server._ACTION_PROCS.clear()
    web_server._ACTION_COMMANDS.clear()
    web_server._ACTION_IDS.clear()
    web_server._ACTION_RESULTS.clear()


@pytest.mark.parametrize(("outcome", "exit_code"), TERMINAL_OUTCOMES)
def test_receipt_only_restart_recovery_preserves_every_terminal_outcome(
    monkeypatch, outcome, exit_code
):
    action_id = "a" * 32
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=action_id, outcome=outcome),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] == exit_code
    assert status["action_id"] == action_id
    assert status["receipt"]["outcome"] == outcome


@pytest.mark.parametrize(("outcome", "exit_code"), TERMINAL_OUTCOMES)
def test_legacy_receipt_without_correlation_recovers_only_without_generation(
    monkeypatch, outcome, exit_code
):
    summary = _receipt(correlation_id=None, outcome=outcome)
    summary.pop("correlation_id")
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] == exit_code
    assert "action_id" not in status
    assert status["receipt"] == summary


@pytest.mark.parametrize("generation", ["known", "unknown"])
def test_legacy_receipt_never_arbitrates_over_existing_generation(
    monkeypatch, generation
):
    summary = _receipt(correlation_id=None, outcome="refused")
    summary.pop("correlation_id")
    result = {"exit_code": 7, "pid": 123}
    if generation == "known":
        result["action_id"] = "b" * 32
    web_server._ACTION_RESULTS["hermes-update"] = result
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] == 7
    assert "receipt" not in status
    if generation == "known":
        assert status["action_id"] == "b" * 32
    else:
        assert "action_id" not in status


@pytest.mark.parametrize(
    "outcome",
    [None, {}, [], 0, True, "", "running", "unknown"],
)
def test_malformed_receipt_outcome_never_arbitrates(monkeypatch, outcome):
    summary = _receipt(correlation_id="a" * 32, outcome="success")
    summary["outcome"] = outcome
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


@pytest.mark.parametrize(
    "correlation_id",
    [None, {}, [], 0, True, "", "a" * 31, "A" * 32, "g" * 32],
)
def test_malformed_receipt_correlation_id_never_arbitrates(
    monkeypatch, correlation_id
):
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=correlation_id, outcome="success"),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
def test_missing_receipt_timestamp_never_arbitrates(monkeypatch, field):
    summary = _receipt(correlation_id="a" * 32, outcome="success")
    summary.pop(field)
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
@pytest.mark.parametrize("value", [None, {}, [], 0, True, "", "   "])
def test_malformed_receipt_timestamp_never_arbitrates(
    monkeypatch, field, value
):
    summary = _receipt(correlation_id="a" * 32, outcome="success")
    summary[field] = value
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


@pytest.mark.parametrize(
    "summary",
    [
        {},
        {"outcome": "success"},
        {"correlation_id": "b" * 32},
        [],
        "success",
        1,
        True,
    ],
)
def test_missing_or_non_object_receipt_summary_never_arbitrates(
    monkeypatch, summary
):
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


def test_malformed_receipt_cannot_hide_exact_durable_completion(
    monkeypatch, _isolated_action_state
):
    action_id = "c" * 32
    _write_update_completion(_isolated_action_state, action_id)
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: {"outcome": {}, "correlation_id": [action_id]},
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == action_id
    assert status["exit_code"] == 0
    assert "receipt" not in status


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {"outcome": {}, "correlation_id": "d" * 32},
        {"outcome": "refused", "correlation_id": ["d" * 32]},
    ],
)
def test_completion_only_refusal_without_valid_receipt_never_becomes_success(
    monkeypatch, _isolated_action_state, summary
):
    refusal_id = "d" * 32
    _write_action_log(
        _isolated_action_state,
        "=== hermes-update completed 2026-08-23 12:00:01 ===",
        f"=== hermes-update completed {refusal_id} ===",
        "image_managed_update_refused: pull and recreate",
    )
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: summary,
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is False
    assert status["exit_code"] is None
    assert status["action_id"] == refusal_id
    assert "receipt" not in status


@pytest.mark.parametrize("field", ["code", "message", "update_command"])
@pytest.mark.parametrize("value", [{}, [], 7, True])
def test_malformed_nested_refusal_scalar_is_sanitized(
    monkeypatch, field, value
):
    import hermes_cli.update_receipt as update_receipt

    action_id = "e" * 32
    payload = {
        "outcome": "refused",
        "correlation_id": action_id,
        "started_at": "2026-08-23T12:00:00+00:00",
        "finished_at": "2026-08-23T12:00:01+00:00",
        "stop_reason": "image_managed_update_refused",
        "refusal": {
            "code": "image_managed_update_refused",
            "message": "pull and recreate",
            "update_command": "docker compose pull",
        },
        "pre_update": {"sha": "a" * 40},
        "post_update": {"sha": "a" * 40, "version": "0.20.5"},
        "fleet": [{"state": "current"}],
    }
    payload["refusal"][field] = value
    monkeypatch.setattr(update_receipt, "read_latest_receipt", lambda: payload)

    summary = web_server._latest_update_receipt_summary()
    assert summary is not None
    assert summary["refusal"][field] is None

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["exit_code"] == 2
    assert status["action_id"] == action_id
    assert status["receipt"]["refusal"][field] is None


def test_latest_receipt_summary_sanitizes_all_other_exposed_scalars(monkeypatch):
    import hermes_cli.update_receipt as update_receipt

    action_id = "f" * 32
    payload = {
        "outcome": "refused",
        "correlation_id": action_id,
        "started_at": {},
        "finished_at": [],
        "stop_reason": 9,
        "refusal": {
            "code": "image_managed_update_refused",
            "message": "pull and recreate",
            "update_command": "docker compose pull",
        },
        "pre_update": {"sha": {}},
        "post_update": {"sha": [], "version": 20},
        "fleet": [
            {"state": "current"},
            {"state": {}},
            {"state": []},
            {"state": 1},
            "not-an-entry",
        ],
    }
    monkeypatch.setattr(update_receipt, "read_latest_receipt", lambda: payload)

    summary = web_server._latest_update_receipt_summary()

    assert summary == {
        "outcome": "refused",
        "correlation_id": action_id,
        "started_at": None,
        "finished_at": None,
        "stop_reason": None,
        "refusal": {
            "code": "image_managed_update_refused",
            "message": "pull and recreate",
            "update_command": "docker compose pull",
        },
        "pre_sha": None,
        "post_sha": None,
        "post_version": None,
        "fleet_states": ["current"],
    }

    status = asyncio.run(web_server.get_action_status("hermes-update"))
    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status


def test_latest_receipt_summary_sanitizes_core_identity_fields(monkeypatch):
    import hermes_cli.update_receipt as update_receipt

    monkeypatch.setattr(
        update_receipt,
        "read_latest_receipt",
        lambda: {
            "outcome": {},
            "correlation_id": ["a" * 32],
            "pre_update": [],
            "post_update": "invalid",
        },
    )

    summary = web_server._latest_update_receipt_summary()

    assert summary is not None
    assert summary["outcome"] is None
    assert summary["correlation_id"] is None
    assert summary["pre_sha"] is None
    assert summary["post_sha"] is None
    assert summary["post_version"] is None


def test_exact_correlation_outranks_wall_clock_fields(monkeypatch, _isolated_action_state):
    action_id = "b" * 32
    _write_update_completion(_isolated_action_state, action_id)
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(
            correlation_id=action_id,
            outcome="refused",
            finished_at="2000-01-01T00:00:00+00:00",
        ),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == action_id
    assert status["exit_code"] == 2
    assert status["receipt"]["outcome"] == "refused"


@pytest.mark.parametrize(
    "finished_at",
    [
        "2000-01-01T00:00:00+00:00",
        "2099-01-01T00:00:00+00:00",
        None,
        "not-a-date",
    ],
)
def test_mismatched_legacy_receipt_can_never_defeat_exact_completion(
    monkeypatch, _isolated_action_state, finished_at
):
    completed_id = "c" * 32
    stale_receipt_id = "d" * 32
    _write_update_completion(_isolated_action_state, completed_id)
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(
            correlation_id=stale_receipt_id,
            outcome="refused",
            finished_at=finished_at,
        ),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == completed_id
    assert status["exit_code"] == 0
    assert "receipt" not in status
    assert f"=== hermes-update completed {completed_id} ===" in status["lines"]


def test_correlated_refusal_in_action_log_supersedes_old_update_log_success(
    monkeypatch, _isolated_action_state
):
    old_id = "1" * 32
    refusal_id = "2" * 32
    _write_update_completion(_isolated_action_state, old_id)
    _write_action_log(
        _isolated_action_state,
        f"=== hermes-update completed {old_id} ===",
        "=== hermes-update completed 2026-08-23 12:00:01 ===",
        f"=== hermes-update completed {refusal_id} ===",
    )
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=refusal_id, outcome="refused"),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == refusal_id
    assert status["exit_code"] == 2
    assert status["receipt"]["refusal"]["code"] == (
        "image_managed_update_refused"
    )
    assert f"=== hermes-update completed {old_id} ===" in status["lines"]


@pytest.mark.parametrize(("stale_outcome", "_exit_code"), TERMINAL_OUTCOMES)
def test_noisy_pending_generation_defeats_every_older_receipt_after_restart(
    monkeypatch, _isolated_action_state, stale_outcome, _exit_code
):
    current_id = "3" * 32
    old_id = "4" * 32
    _write_update_completion(_isolated_action_state, old_id)
    _write_action_log(
        _isolated_action_state,
        *_correlated_start(current_id),
        *(f"verbose update output {index}" for index in range(2501)),
    )
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=old_id, outcome=stale_outcome),
    )

    status = asyncio.run(
        web_server.get_action_status("hermes-update", lines=2000)
    )

    assert status["running"] is False
    assert status["exit_code"] is None
    assert status["action_id"] == current_id
    assert "receipt" not in status
    assert f"=== hermes-update completed {old_id} ===" not in status["lines"]
    assert all("action " not in line for line in status["lines"])


def test_matching_update_log_completion_settles_correlated_start_without_receipt(
    monkeypatch, _isolated_action_state
):
    action_id = "5" * 32
    _write_action_log(
        _isolated_action_state,
        *_correlated_start(action_id),
        "last stdout line before action-log rotation",
    )
    _write_update_completion(_isolated_action_state, action_id)
    monkeypatch.setattr(web_server, "_latest_update_receipt_summary", lambda: None)

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == action_id
    assert status["exit_code"] == 0
    assert f"=== hermes-update completed {action_id} ===" in status["lines"]


def test_mismatched_update_log_completion_cannot_settle_correlated_start(
    monkeypatch, _isolated_action_state
):
    action_id = "5" * 32
    stale_id = "6" * 32
    _write_action_log(_isolated_action_state, *_correlated_start(action_id))
    _write_update_completion(_isolated_action_state, stale_id)
    monkeypatch.setattr(web_server, "_latest_update_receipt_summary", lambda: None)

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == action_id
    assert status["exit_code"] is None
    assert f"=== hermes-update completed {stale_id} ===" not in status["lines"]


def test_late_mismatched_action_completion_cannot_settle_newer_start(
    monkeypatch, _isolated_action_state
):
    current_id = "5" * 32
    stale_id = "6" * 32
    _write_action_log(
        _isolated_action_state,
        *_correlated_start(current_id),
        f"=== hermes-update completed {stale_id} ===",
    )
    _write_update_completion(_isolated_action_state, stale_id)
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=stale_id, outcome="success"),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == current_id
    assert status["exit_code"] is None
    assert "receipt" not in status


def test_unreadable_action_history_fails_closed_against_older_durable_sources(
    monkeypatch, _isolated_action_state
):
    old_id = "6" * 32
    _write_update_completion(_isolated_action_state, old_id)
    monkeypatch.setattr(
        web_server,
        "_read_durable_correlated_update_action",
        lambda _path: (None, True, True),
    )
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=old_id, outcome="success"),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["exit_code"] is None
    assert "action_id" not in status
    assert "receipt" not in status
    assert f"=== hermes-update completed {old_id} ===" not in status["lines"]


@pytest.mark.parametrize(("outcome", "exit_code"), TERMINAL_OUTCOMES)
def test_matching_receipt_settles_pending_generation_after_restart(
    monkeypatch, _isolated_action_state, outcome, exit_code
):
    current_id = "5" * 32
    old_id = "6" * 32
    _write_update_completion(_isolated_action_state, old_id)
    _write_action_log(
        _isolated_action_state,
        *_correlated_start(current_id),
        "child output before the dashboard restarted",
    )
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=current_id, outcome=outcome),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == current_id
    assert status["exit_code"] == exit_code
    assert status["receipt"]["outcome"] == outcome
    assert f"=== hermes-update completed {old_id} ===" not in status["lines"]


@pytest.mark.parametrize(("outcome", "expected_exit"), TERMINAL_OUTCOMES)
@pytest.mark.parametrize("process_exit", [0, 1, 2])
def test_correlated_receipt_outranks_every_contradictory_in_memory_exit(
    monkeypatch, outcome, expected_exit, process_exit
):
    action_id = "7" * 32
    web_server._ACTION_RESULTS["hermes-update"] = {
        "action_id": action_id,
        "exit_code": process_exit,
        "pid": 42,
    }
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=action_id, outcome=outcome),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["action_id"] == action_id
    assert status["exit_code"] == expected_exit
    assert status["receipt"]["outcome"] == outcome


def test_running_process_identity_defeats_terminal_receipt_for_other_generation(
    monkeypatch,
):
    class Running:
        pid = 42

        @staticmethod
        def poll():
            return None

    action_id = "8" * 32
    web_server._ACTION_PROCS["hermes-update"] = Running()
    web_server._ACTION_IDS["hermes-update"] = action_id
    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id="9" * 32, outcome="refused"),
    )

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["running"] is True
    assert status["exit_code"] is None
    assert status["pid"] == 42
    assert status["action_id"] == action_id
    assert "receipt" not in status


def test_reaped_process_keeps_action_id_for_later_receipt_arbitration(monkeypatch):
    class Finished:
        pid = 73

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def wait(timeout):
            assert timeout == 1
            return 1

    action_id = "a" * 32
    web_server._ACTION_PROCS["hermes-update"] = Finished()
    web_server._ACTION_IDS["hermes-update"] = action_id
    monkeypatch.setattr(web_server, "_latest_update_receipt_summary", lambda: None)

    first = asyncio.run(web_server.get_action_status("hermes-update"))

    assert first["exit_code"] == 1
    assert first["action_id"] == action_id
    assert web_server._ACTION_RESULTS["hermes-update"]["action_id"] == action_id

    monkeypatch.setattr(
        web_server,
        "_latest_update_receipt_summary",
        lambda: _receipt(correlation_id=action_id, outcome="success"),
    )
    second = asyncio.run(web_server.get_action_status("hermes-update"))

    assert second["exit_code"] == 0
    assert second["action_id"] == action_id
    assert second["receipt"]["outcome"] == "success"


def test_legacy_action_start_plus_update_log_completion_remains_compatible(
    monkeypatch, _isolated_action_state
):
    action_id = "b" * 32
    _write_action_log(
        _isolated_action_state,
        "=== hermes-update started 2026-08-17 11:19:34 ===",
        "legacy output",
    )
    _write_update_completion(_isolated_action_state, action_id)
    monkeypatch.setattr(web_server, "_latest_update_receipt_summary", lambda: None)

    status = asyncio.run(web_server.get_action_status("hermes-update"))

    assert status["exit_code"] == 0
    assert status["action_id"] == action_id
    assert f"=== hermes-update completed {action_id} ===" in status["lines"]


def test_spawn_persists_exact_generation_before_registering_process(
    monkeypatch, _isolated_action_state
):
    class Process:
        pid = 91

    spawned = []

    def fake_popen(command, **kwargs):
        spawned.append((command, kwargs))
        return Process()

    action_id = "c" * 32
    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)

    process = web_server._spawn_hermes_action(
        ["update"],
        "hermes-update",
        env_overrides={"HERMES_ACTION_ID": action_id},
    )

    assert process.pid == 91
    assert web_server._ACTION_IDS["hermes-update"] == action_id
    assert len(spawned) == 1
    lines = (
        _isolated_action_state / "hermes-update.log"
    ).read_text(encoding="utf-8").splitlines()
    assert lines[-2].startswith("=== hermes-update started ")
    assert lines[-1] == f"=== hermes-update action {action_id} started ==="


def _fail_if_opened(*_args, **_kwargs):
    raise AssertionError("non-regular action log must be rejected before open")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_durable_action_log_fifo_fails_closed_before_open(monkeypatch, tmp_path):
    path = tmp_path / "hermes-update.log"
    os.mkfifo(path)
    monkeypatch.setattr(builtins, "open", _fail_if_opened)
    monkeypatch.setattr(web_server.os, "open", _fail_if_opened)

    assert web_server._read_durable_correlated_update_action(path) == (
        None,
        True,
        True,
    )
    assert web_server._tail_lines(path, 200) == []


def test_durable_action_log_symlink_fails_closed_before_open(monkeypatch, tmp_path):
    target = tmp_path / "target.log"
    target.write_text(
        f"=== hermes-update completed {'d' * 32} ===\n",
        encoding="utf-8",
    )
    path = tmp_path / "hermes-update.log"
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    monkeypatch.setattr(builtins, "open", _fail_if_opened)
    monkeypatch.setattr(web_server.os, "open", _fail_if_opened)

    assert web_server._read_durable_correlated_update_action(path) == (
        None,
        True,
        True,
    )
    assert web_server._tail_lines(path, 200) == []


def test_unreadable_regular_action_log_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "hermes-update.log"
    path.write_text("ordinary log\n", encoding="utf-8")

    def deny_open(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(web_server.os, "open", deny_open)

    assert web_server._read_durable_correlated_update_action(path) == (
        None,
        True,
        True,
    )
    assert web_server._tail_lines(path, 200) == []


def test_action_log_path_swap_to_other_regular_file_fails_closed(
    monkeypatch, tmp_path
):
    observed = tmp_path / "hermes-update.log"
    observed.write_text("observed\n", encoding="utf-8")
    replacement = tmp_path / "replacement.log"
    replacement.write_text(
        f"=== hermes-update completed {'e' * 32} ===\n",
        encoding="utf-8",
    )
    real_open = os.open

    def swap_open(_path, flags):
        return real_open(replacement, flags)

    monkeypatch.setattr(web_server.os, "open", swap_open)

    assert web_server._read_durable_correlated_update_action(observed) == (
        None,
        True,
        True,
    )
    assert web_server._tail_lines(observed, 200) == []


def test_missing_action_log_remains_clean_absence(tmp_path):
    path = tmp_path / "missing.log"

    assert web_server._read_durable_correlated_update_action(path) == (
        None,
        False,
        False,
    )
    assert web_server._tail_lines(path, 200) == []


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], (None, False)),
        ([f"=== hermes-update completed {'c' * 32} ==="], ("c" * 32, False)),
        (
            [
                f"=== hermes-update completed {'c' * 32} ===",
                *_correlated_start("d" * 32),
            ],
            ("d" * 32, True),
        ),
        (
            [
                *_correlated_start("d" * 32),
                f"=== hermes-update completed {'d' * 32} ===",
            ],
            ("d" * 32, False),
        ),
        (
            [
                *_correlated_start("d" * 32),
                f"=== hermes-update completed {'d' * 32} ===",
                *_correlated_start("e" * 32),
            ],
            ("e" * 32, True),
        ),
        (
            [
                *_correlated_start("d" * 32),
                f"=== hermes-update completed {'e' * 32} ===",
            ],
            ("d" * 32, True),
        ),
        (
            [
                *_correlated_start("d" * 32),
                f"=== hermes-update completed {'e' * 32} ===",
                f"=== hermes-update completed {'d' * 32} ===",
            ],
            ("d" * 32, False),
        ),
    ],
)
def test_correlated_action_parser_uses_exact_id_and_file_order(lines, expected):
    assert web_server._durable_correlated_update_action(lines) == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], None),
        ([f"=== hermes-update completed {'f' * 32} ==="], "f" * 32),
        (
            [
                f"=== hermes-update completed {'f' * 32} ===",
                "=== hermes update started newer ===",
            ],
            None,
        ),
        (
            [
                "=== hermes update started first ===",
                f"=== hermes-update completed {'f' * 32} ===",
                "=== hermes update started second ===",
                f"=== hermes-update completed {'0' * 32} ===",
            ],
            "0" * 32,
        ),
    ],
)
def test_legacy_completion_parser_rejects_superseded_markers(lines, expected):
    assert web_server._durable_completed_update_action_id(lines) == expected
