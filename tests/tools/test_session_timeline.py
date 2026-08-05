"""Tests for tools/session_timeline.py — the live per-session step timeline.

Covers:
- record_start / record_end lifecycle (running -> succeeded/failed/blocked)
- file-backed persistence (read_timeline works from a fresh read, simulating
  a different process than the one that recorded the steps)
- ring buffer eviction at the TIMELINE_MAX_STEPS cap
- args_digest truncation + redaction (the safety-net pass on top of
  build_tool_preview, since that helper only redacts browser_type text)
- best-effort/non-raising behavior
"""

import json

import pytest

from tools import session_timeline as st


# Every test uses a distinct session_id so the module-level in-process
# cache (_states) can't leak between tests in this file (same interpreter,
# per this repo's per-file test-process isolation convention).


def test_record_start_creates_running_entry_with_no_duration():
    sid = "sess-start-1"
    step_n = st.record_start(sid, "call-1", "read_file", {"path": "foo.py"})
    assert step_n == 0

    data = st.read_timeline(sid)
    assert data["session_id"] == sid
    assert data["running"] is True
    assert len(data["steps"]) == 1
    step = data["steps"][0]
    assert step["step_n"] == 0
    assert step["tool"] == "read_file"
    assert step["status"] == "running"
    assert step["duration"] is None
    assert "foo.py" in step["args_digest"]


def test_record_end_updates_matching_entry_to_succeeded():
    sid = "sess-end-1"
    st.record_start(sid, "call-a", "terminal", {"command": "ls"})
    st.record_end(sid, "call-a", status="succeeded", duration=1.23)

    data = st.read_timeline(sid)
    assert data["running"] is False
    assert len(data["steps"]) == 1
    step = data["steps"][0]
    assert step["status"] == "succeeded"
    assert step["duration"] == 1.23


def test_record_end_failed_and_blocked_statuses():
    sid = "sess-end-2"
    st.record_start(sid, "call-fail", "terminal", {"command": "false"})
    st.record_end(sid, "call-fail", status="failed", duration=0.1)

    st.record_start(sid, "call-blocked", "write_file", {"path": "x"})
    st.record_end(sid, "call-blocked", status="blocked", duration=0.0)

    data = st.read_timeline(sid)
    statuses = {s["action_id"]: s["status"] for s in data["steps"]}
    assert statuses["call-fail"] == "failed"
    assert statuses["call-blocked"] == "blocked"
    assert data["running"] is False


def test_multiple_running_steps_mark_timeline_as_running():
    sid = "sess-running-flag"
    st.record_start(sid, "call-1", "terminal", {"command": "ls"})
    st.record_start(sid, "call-2", "read_file", {"path": "a.py"})
    st.record_end(sid, "call-1", status="succeeded", duration=0.5)

    data = st.read_timeline(sid)
    assert data["running"] is True  # call-2 still running
    by_id = {s["action_id"]: s["status"] for s in data["steps"]}
    assert by_id["call-1"] == "succeeded"
    assert by_id["call-2"] == "running"


def test_step_n_increments_monotonically_across_start_calls():
    sid = "sess-stepn"
    n0 = st.record_start(sid, "c0", "tool_a", {})
    n1 = st.record_start(sid, "c1", "tool_b", {})
    n2 = st.record_start(sid, "c2", "tool_c", {})
    assert (n0, n1, n2) == (0, 1, 2)


def test_read_timeline_missing_session_returns_empty_not_raise():
    data = st.read_timeline("sess-does-not-exist-at-all")
    assert data == {
        "session_id": "sess-does-not-exist-at-all",
        "steps": [],
        "running": False,
    }


def test_record_start_with_falsy_session_id_is_noop():
    assert st.record_start(None, "c", "tool", {}) is None
    assert st.record_start("", "c", "tool", {}) is None


def test_record_end_for_unknown_call_id_does_not_raise_or_mutate():
    sid = "sess-end-unknown"
    st.record_start(sid, "call-real", "terminal", {"command": "ls"})
    # Not the id that was started — must be a silent no-op, not an error.
    st.record_end(sid, "call-does-not-exist", status="succeeded", duration=1.0)

    data = st.read_timeline(sid)
    assert data["steps"][0]["status"] == "running"


def test_persistence_survives_fresh_in_process_state():
    """Simulates a different process reading the file: drop the in-memory
    cache and confirm read_timeline still returns the persisted content."""
    sid = "sess-persist"
    st.record_start(sid, "call-1", "terminal", {"command": "echo hi"})
    st.record_end(sid, "call-1", status="succeeded", duration=0.2)

    # Forget in-process state without touching the file — read_timeline must
    # come from disk, exactly as a separate web_server.py process would.
    with st._states_lock:
        st._states.pop(sid, None)

    data = st.read_timeline(sid)
    assert data["steps"][0]["status"] == "succeeded"
    assert data["steps"][0]["duration"] == 0.2


def test_file_is_written_atomically_no_partial_json():
    sid = "sess-atomic"
    st.record_start(sid, "call-1", "terminal", {"command": "echo hi"})
    path = st._session_path(sid)
    assert path.exists()
    # Must parse cleanly -- proves no partial/torn write landed on disk.
    json.loads(path.read_text(encoding="utf-8"))
    # No stray temp file left behind after a successful write.
    leftover_tmp = list(path.parent.glob(f".{path.name}.*.tmp"))
    assert leftover_tmp == []


# ---------------------------------------------------------------------------
# Ring buffer eviction
# ---------------------------------------------------------------------------


def test_ring_buffer_evicts_oldest_entries_past_cap():
    sid = "sess-evict"
    total = st.TIMELINE_MAX_STEPS + 50
    for i in range(total):
        cid = f"call-{i}"
        st.record_start(sid, cid, "terminal", {"command": f"echo {i}"})
        st.record_end(sid, cid, status="succeeded", duration=0.01)

    data = st.read_timeline(sid)
    steps = data["steps"]
    # Bounded: never more than the cap survives.
    assert len(steps) == st.TIMELINE_MAX_STEPS
    # FIFO: the oldest surviving entry is exactly `total - cap` steps in,
    # i.e. the first `total - cap` entries were evicted.
    earliest = min(s["step_n"] for s in steps)
    latest = max(s["step_n"] for s in steps)
    assert earliest == total - st.TIMELINE_MAX_STEPS
    assert latest == total - 1
    # step_n keeps counting up past eviction (never resets/reused).
    assert len({s["step_n"] for s in steps}) == st.TIMELINE_MAX_STEPS


def test_ring_buffer_eviction_on_disk_matches_in_memory():
    sid = "sess-evict-disk"
    total = st.TIMELINE_MAX_STEPS + 10
    for i in range(total):
        st.record_start(sid, f"call-{i}", "terminal", {"command": "x"})

    path = st._session_path(sid)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk["steps"]) == st.TIMELINE_MAX_STEPS
    assert min(s["step_n"] for s in on_disk["steps"]) == total - st.TIMELINE_MAX_STEPS


# ---------------------------------------------------------------------------
# args_digest: truncation + redaction
# ---------------------------------------------------------------------------

_BEARER = "sk-ant-api03-" + "R" * 24


def test_args_digest_redacts_bearer_token_in_terminal_command():
    """build_tool_preview() alone does NOT redact terminal commands (it only
    compacts/truncates via summarize_shell_command) -- session_timeline's
    extra redact_sensitive_text() safety-net pass is what must catch this."""
    sid = "sess-redact-terminal"
    st.record_start(
        sid, "call-1", "terminal",
        {"command": f'curl -H "Authorization: Bearer {_BEARER}" https://api.internal'},
    )
    data = st.read_timeline(sid)
    digest = data["steps"][0]["args_digest"]
    assert _BEARER not in digest
    assert "curl" in digest, "redaction must not gut the operational detail"


def test_args_digest_redacts_env_style_secret():
    sid = "sess-redact-env"
    secret = "sk-proj-" + "L" * 24
    st.record_start(
        sid, "call-1", "terminal",
        {"command": f"export OPENAI_API_KEY={secret} && run_job"},
    )
    data = st.read_timeline(sid)
    digest = data["steps"][0]["args_digest"]
    assert secret not in digest


def test_args_digest_benign_content_is_untouched():
    sid = "sess-digest-benign"
    st.record_start(sid, "call-1", "read_file", {"path": "src/parser.py"})
    data = st.read_timeline(sid)
    assert "parser.py" in data["steps"][0]["args_digest"]


def test_args_digest_empty_for_no_args():
    sid = "sess-digest-empty"
    st.record_start(sid, "call-1", "todo", {})
    data = st.read_timeline(sid)
    # No args -> build_tool_preview returns None -> digest is "".
    assert data["steps"][0]["args_digest"] == ""


def test_args_digest_never_raises_on_malformed_args():
    sid = "sess-digest-malformed"
    # A non-dict-friendly args shape must not blow up record_start.
    step_n = st.record_start(sid, "call-1", "weird_tool", {"nested": object()})
    assert step_n == 0
    data = st.read_timeline(sid)
    assert data["steps"][0]["tool"] == "weird_tool"


# ---------------------------------------------------------------------------
# Never raises into the caller (best-effort discipline)
# ---------------------------------------------------------------------------


def test_record_start_swallows_flush_failure(monkeypatch):
    sid = "sess-flush-fail"

    def _boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(st, "_write_atomic", _boom)
    # Must not raise even though every flush attempt fails.
    step_n = st.record_start(sid, "call-1", "terminal", {"command": "ls"})
    assert step_n == 0


def test_record_end_swallows_flush_failure(monkeypatch):
    sid = "sess-flush-fail-2"
    st.record_start(sid, "call-1", "terminal", {"command": "ls"})

    def _boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(st, "_write_atomic", _boom)
    st.record_end(sid, "call-1", status="succeeded", duration=1.0)  # must not raise


def test_read_timeline_survives_corrupt_file():
    sid = "sess-corrupt"
    path = st._session_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    data = st.read_timeline(sid)
    assert data == {"session_id": sid, "steps": [], "running": False}


def test_session_id_is_sanitized_for_path_safety():
    """A session_id with path-hostile characters must not escape the
    timeline root or raise -- it collapses to a safe filename with no
    separators and no ".." substrings."""
    sid = "../../etc/passwd"
    st.record_start(sid, "call-1", "terminal", {"command": "ls"})
    path = st._session_path(sid)
    assert path.parent == st.timeline_root()
    assert ".." not in path.name
    assert "/" not in path.name and "\\" not in path.name
    assert path.resolve().parent == st.timeline_root().resolve()


def test_clear_timeline_removes_state_and_file():
    sid = "sess-clear"
    st.record_start(sid, "call-1", "terminal", {"command": "ls"})
    path = st._session_path(sid)
    assert path.exists()
    st.clear_timeline(sid)
    assert not path.exists()
    with st._states_lock:
        assert sid not in st._states


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
