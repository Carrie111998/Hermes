"""Plaintext transcript artifacts must be created owner-only.

``/save`` snapshots, agent trajectories, and MoA traces all persist verbatim
conversation content — message text, tool results, tool-call arguments. Each
was created through a bare ``open()``/``json.dump()``, so its permissions came
from the process umask (0o644 on a default install), leaving a full transcript
readable by every local account. Trajectories are the sharpest case: they
append to the CWD rather than the 0o700 ``HERMES_HOME``.

These tests assert the *contract* (no group/other bits on a freshly created
artifact), not a specific octal, and they exercise the real write paths against
a temp home with a deliberately permissive umask so a regression to
umask-derived modes fails here.

Content is intentionally NOT asserted to be redacted. These artifacts export
the same history the session DB replays, and masking a credential in a replayed
path poisons the replay (#43083) — see ``test_tool_call_arg_no_redaction.py``.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits; Windows at-rest protection is ACL-based",
)


@pytest.fixture
def permissive_umask():
    """Force a umask that would yield world-readable files if mode were unset."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_owner_only(path: Path) -> None:
    mode = _mode(path)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), (
        f"{path.name} is group/other-accessible ({oct(mode)}); a plaintext "
        "transcript artifact must be created owner-only"
    )


@posix_only
def test_trajectory_jsonl_created_owner_only(tmp_path, monkeypatch, permissive_umask):
    """save_trajectory writes to the CWD — the new file must not be 0o644."""
    monkeypatch.chdir(tmp_path)
    from agent.trajectory import save_trajectory

    save_trajectory(
        [{"from": "human", "value": "hi"}, {"from": "gpt", "value": "hello"}],
        "test-model",
        True,
    )

    written = tmp_path / "trajectory_samples.jsonl"
    assert written.is_file(), list(tmp_path.iterdir())
    _assert_owner_only(written)


@posix_only
def test_trajectory_jsonl_appends_and_keeps_content_verbatim(
    tmp_path, monkeypatch, permissive_umask
):
    """Hardening the mode must not change append semantics or content."""
    monkeypatch.chdir(tmp_path)
    from agent.trajectory import save_trajectory

    save_trajectory([{"from": "human", "value": "first"}], "m", True)
    save_trajectory([{"from": "human", "value": "second"}], "m", True)

    lines = (tmp_path / "trajectory_samples.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2, lines
    assert json.loads(lines[0])["conversations"][0]["value"] == "first"
    assert json.loads(lines[1])["conversations"][0]["value"] == "second"


@posix_only
def test_trajectory_existing_relaxed_file_is_not_retightened(
    tmp_path, monkeypatch, permissive_umask
):
    """A file the user deliberately widened keeps its mode (opt-in export)."""
    monkeypatch.chdir(tmp_path)
    from agent.trajectory import save_trajectory

    target = tmp_path / "trajectory_samples.jsonl"
    target.write_text("")
    os.chmod(target, 0o644)

    save_trajectory([{"from": "human", "value": "hi"}], "m", True)

    assert _mode(target) == 0o644, (
        "an existing trajectory file's permissions must be left as the user "
        "set them; only creation applies the private mode"
    )


@posix_only
def test_save_conversation_snapshot_owner_only(tmp_path, monkeypatch, permissive_umask):
    """/save writes a full transcript — the snapshot must be owner-only."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # No sys.modules purge and no Path.home patch here: save_conversation
    # resolves the directory through get_hermes_home() at call time, and that
    # reads HERMES_HOME from the environment on every call with no caching, so
    # the setenv above is sufficient no matter how early cli was imported.
    # Purging modules by ``startswith("cli")`` would also evict click and its
    # submodules, breaking module identity for the rest of the suite.
    import cli

    history = [
        {"role": "user", "content": "deploy with PGPASSWORD=hunter2"},
        {"role": "assistant", "content": "done"},
    ]
    stub = SimpleNamespace(
        conversation_history=history,
        model="test-model",
        session_id="20260101_120000_abc123",
        session_start=datetime(2026, 1, 1, 12, 0, 0),
    )
    cli.HermesCLI.save_conversation(stub)

    files = list((home / "sessions" / "saved").glob("hermes_conversation_*.json"))
    assert len(files) == 1, files
    _assert_owner_only(files[0])

    # Export stays verbatim: it snapshots the history the session DB replays.
    payload = json.loads(files[0].read_text())
    assert payload["messages"] == history


@posix_only
def test_moa_trace_jsonl_and_dir_owner_only(tmp_path, monkeypatch, permissive_umask):
    """MoA traces embed every reference model's full input messages."""
    import agent.moa_trace as moa_trace

    trace_dir = tmp_path / "moa-traces"
    monkeypatch.setattr(
        moa_trace, "_traces_enabled_and_dir", lambda: trace_dir
    )

    moa_trace.save_moa_turn(
        session_id="sess-1",
        preset_name="default",
        reference_outputs=[],
        aggregator_label="agg",
        aggregator_model="m",
        aggregator_provider="p",
        aggregator_temperature=0.7,
        aggregator_input_messages=[{"role": "user", "content": "hi"}],
        aggregator_output="hello",
        aggregator_streamed=False,
    )

    written = trace_dir / "sess-1.jsonl"
    assert written.is_file(), list(trace_dir.iterdir())
    _assert_owner_only(written)
    dir_mode = _mode(trace_dir)
    assert not dir_mode & (stat.S_IRWXG | stat.S_IRWXO), (
        f"moa-traces dir is group/other-accessible ({oct(dir_mode)})"
    )
