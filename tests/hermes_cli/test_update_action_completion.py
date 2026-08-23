from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import hermes_cli.web_server as web_server


@pytest.fixture(autouse=True)
def _clear_action_state():
    for registry in (
        web_server._ACTION_PROCS,
        web_server._ACTION_COMMANDS,
        web_server._ACTION_IDS,
        web_server._ACTION_RESULTS,
    ):
        registry.pop("hermes-update", None)
    yield
    for registry in (
        web_server._ACTION_PROCS,
        web_server._ACTION_COMMANDS,
        web_server._ACTION_IDS,
        web_server._ACTION_RESULTS,
    ):
        registry.pop("hermes-update", None)


def test_synchronous_update_completion_is_correlated_and_ordered(
    monkeypatch, tmp_path
):
    action_id = "a" * 32
    monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", tmp_path)

    web_server._record_completed_action(
        "hermes-update",
        "pull and recreate",
        exit_code=2,
        action_id=action_id,
    )

    lines = (tmp_path / "hermes-update.log").read_text(encoding="utf-8").splitlines()
    correlated_marker = f"=== hermes-update completed {action_id} ==="
    legacy_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("=== hermes-update completed ")
        and line != correlated_marker
    )
    assert lines[legacy_index + 1] == correlated_marker
    assert web_server._ACTION_RESULTS["hermes-update"] == {
        "exit_code": 2,
        "pid": None,
        "action_id": action_id,
    }


def test_completed_action_log_failure_never_breaks_typed_result(
    monkeypatch, tmp_path
):
    action_id = "b" * 32
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", not_a_directory)

    web_server._record_completed_action(
        "hermes-update",
        "pull and recreate",
        exit_code=2,
        action_id=action_id,
    )

    assert web_server._ACTION_RESULTS["hermes-update"] == {
        "exit_code": 2,
        "pid": None,
        "action_id": action_id,
    }
