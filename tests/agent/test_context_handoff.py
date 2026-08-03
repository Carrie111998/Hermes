import json
import os
import pytest

from agent.context_handoff import handoff_artifact_path, handoff_is_due, write_handoff_artifact


def test_handoff_policy_is_threshold_and_config_driven():
    assert handoff_is_due(enabled=True, estimated_tokens=100000, threshold_tokens=100000)
    assert not handoff_is_due(enabled=True, estimated_tokens=99999, threshold_tokens=100000)
    assert not handoff_is_due(enabled=False, estimated_tokens=200000, threshold_tokens=100000)
    assert not handoff_is_due(enabled=True, estimated_tokens=200000, threshold_tokens=0)


def test_handoff_artifact_is_bounded_atomic_and_resumable(tmp_path):
    path = write_handoff_artifact(
        hermes_home=tmp_path,
        session_id="session/with spaces",
        estimated_tokens=123456,
        threshold_tokens=100000,
        model="coding",
    )

    assert path == handoff_artifact_path(hermes_home=tmp_path, session_id="session/with spaces")
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "session/with spaces"
    assert payload["resumable"] is True
    assert payload["resume_command"] == "hermes --resume session/with spaces"
    assert payload["conversation_source"] == "SessionDB"
    assert "messages" not in payload
    assert path.stat().st_size < 4096
    assert not list(path.parent.glob(".*.tmp"))

    # Repeated crossings replace one marker instead of growing a directory.
    write_handoff_artifact(
        hermes_home=tmp_path,
        session_id="session/with spaces",
        estimated_tokens=200000,
        threshold_tokens=100000,
    )
    assert len(list(path.parent.glob("*.json"))) == 1
    assert json.loads(path.read_text())["estimated_tokens"] == 123456


def test_session_ids_that_sanitize_alike_get_distinct_artifacts(tmp_path):
    first = write_handoff_artifact(
        hermes_home=tmp_path, session_id="a/b", estimated_tokens=100000, threshold_tokens=100000
    )
    second = write_handoff_artifact(
        hermes_home=tmp_path, session_id="a_b", estimated_tokens=100000, threshold_tokens=100000
    )
    assert first != second
    assert json.loads(first.read_text())["session_id"] == "a/b"
    assert json.loads(second.read_text())["session_id"] == "a_b"


def test_qualifying_repeat_is_deduplicated(tmp_path):
    path = write_handoff_artifact(
        hermes_home=tmp_path, session_id="sid", estimated_tokens=100000, threshold_tokens=100000
    )
    path.touch()
    touched = path.stat().st_mtime_ns
    assert write_handoff_artifact(
        hermes_home=tmp_path, session_id="sid", estimated_tokens=200000, threshold_tokens=100000
    ) == path
    assert path.stat().st_mtime_ns == touched


def test_qualifying_repeat_runs_retention_cleanup(tmp_path):
    path = write_handoff_artifact(
        hermes_home=tmp_path,
        session_id="sid",
        estimated_tokens=100000,
        threshold_tokens=100000,
    )
    directory = path.parent
    for index in range(256):
        (directory / f"old-{index}.json").write_text("{}", encoding="utf-8")
    # Keep the deduplicated marker newer than the synthetic backlog.
    os.utime(path, None)

    write_handoff_artifact(
        hermes_home=tmp_path,
        session_id="sid",
        estimated_tokens=200000,
        threshold_tokens=100000,
    )

    assert len(list(directory.glob("*.json"))) == 256
    assert path.is_file()


@pytest.mark.parametrize("value", [True, False, 1.5, "1.5"])
def test_invalid_handoff_threshold_is_rejected(tmp_path, value):
    assert not handoff_is_due(enabled=True, estimated_tokens=100000, threshold_tokens=value)
    assert write_handoff_artifact(
        hermes_home=tmp_path, session_id="sid", estimated_tokens=100000, threshold_tokens=value
    ) is None


def test_write_failure_does_not_leave_partial_artifact(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agent.context_handoff.tempfile.mkstemp", fail)
    with pytest.raises(OSError, match="disk full"):
        write_handoff_artifact(
            hermes_home=tmp_path, session_id="sid", estimated_tokens=100000, threshold_tokens=100000
        )
    assert not list(tmp_path.rglob("*.json"))


def test_handoff_artifact_skips_invalid_boundary(tmp_path):
    assert write_handoff_artifact(
        hermes_home=tmp_path,
        session_id="sid",
        estimated_tokens=99999,
        threshold_tokens=100000,
    ) is None
    assert not (tmp_path / "sessions").exists()
    assert write_handoff_artifact(
        hermes_home=tmp_path,
        session_id=None,
        estimated_tokens=100000,
        threshold_tokens=100000,
    ) is None
