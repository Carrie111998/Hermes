"""The remaining plaintext transcript artifacts must be created owner-only.

Follow-up to the four paths hardened in #77520 (``/save`` snapshots, agent
trajectories, MoA traces). The sites covered here were deliberately scoped out
of that PR to keep it narrow, and are the same defect at sibling call paths:

* ``plugins/platforms/a2a/protocol.py:persist_message`` — verbatim peer
  conversation turns, ``mkdir`` and append open both at umask (0o755 / 0o644).
* ``plugins/platforms/a2a/security.py:audit`` — 500-char summaries of each
  peer exchange, append open at umask (0o644).
* ``batch_runner.py:_process_batch_worker`` — full trajectories appended under
  the CWD's ``data/<run>/``, append open at umask (0o644). Closest sibling to
  ``agent/trajectory.py``, which #77520 fixed.
* ``cli.py:save_conversation`` — the ``sessions/saved`` *directory* (0o755).
  #77520 fixed the snapshot file inside it and left the directory as an
  explicit follow-up.

These assert the *contract* — no group/other bits on a freshly created
artifact — rather than freezing an octal, exercise the real write paths under a
deliberately permissive ``umask 022``, and are POSIX-only because Windows
at-rest protection is ACL-based (PR #77527's territory).

Content is intentionally NOT asserted to be redacted, and two tests assert the
opposite: these artifacts are replayed/audited full-fidelity, and masking a
credential in a replayed path poisons the replay (#43083, guarded by
``tests/agent/test_tool_call_arg_no_redaction.py``).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# batch_runner / cli are root-level modules, not an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits; Windows at-rest protection is ACL-based",
)


@pytest.fixture
def permissive_umask():
    """Force a umask that would yield group/world-readable artifacts."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def temp_hermes_home(tmp_path, monkeypatch):
    """A real HERMES_HOME the artifact writers resolve at call time."""
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_owner_only(path: Path) -> None:
    mode = _mode(path)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), (
        f"{path.name} is group/other-accessible ({oct(mode)}); a plaintext "
        "transcript artifact must be created owner-only"
    )


# ---------------------------------------------------------------------------
# A2A conversation logs (plugins/platforms/a2a/protocol.py)
# ---------------------------------------------------------------------------

@posix_only
def test_a2a_conversation_log_and_dir_owner_only(
    temp_hermes_home, permissive_umask
):
    """persist_message stores verbatim peer turns — dir and file owner-only."""
    from plugins.platforms.a2a import protocol

    protocol.persist_message("ctx-abc", "user", "deploy with PGPASSWORD=hunter2", "task-1")

    conv_dir = temp_hermes_home / "a2a_conversations"
    written = conv_dir / "ctx-abc.jsonl"
    assert written.is_file(), list(conv_dir.iterdir())
    _assert_owner_only(written)
    _assert_owner_only(conv_dir)


@posix_only
def test_a2a_conversation_log_appends_verbatim(temp_hermes_home, permissive_umask):
    """Hardening the mode must not change append semantics or content."""
    from plugins.platforms.a2a import protocol

    protocol.persist_message("ctx-abc", "user", "first PGPASSWORD=hunter2", "t1")
    protocol.persist_message("ctx-abc", "agent", "second", "t1")

    # Read back through the module's own loader: the log must stay replayable.
    loaded = protocol.load_conversation("ctx-abc")
    assert [r["role"] for r in loaded] == ["user", "agent"]
    assert loaded[0]["text"] == "first PGPASSWORD=hunter2", (
        "conversation logs are replayed into the model on context resume; "
        "masking a credential here poisons the replay (#43083)"
    )


@posix_only
def test_a2a_conversation_existing_relaxed_file_is_not_retightened(
    temp_hermes_home, permissive_umask
):
    """A file the operator deliberately widened keeps its mode."""
    from plugins.platforms.a2a import protocol

    conv_dir = temp_hermes_home / "a2a_conversations"
    conv_dir.mkdir(mode=0o700)
    target = conv_dir / "ctx-abc.jsonl"
    target.write_text("", encoding="utf-8")
    os.chmod(target, 0o644)

    protocol.persist_message("ctx-abc", "user", "hi", "t1")

    assert _mode(target) == 0o644, (
        "only the creating open applies the private mode; an existing file's "
        "permissions must be left as the operator set them"
    )


# ---------------------------------------------------------------------------
# A2A audit log (plugins/platforms/a2a/security.py)
# ---------------------------------------------------------------------------

@posix_only
def test_a2a_audit_log_owner_only(temp_hermes_home, permissive_umask):
    """audit() records a summary of each peer exchange — partial transcript."""
    from plugins.platforms.a2a import security

    security.audit("inbound", "peer.example", "task-1", "ran psql PGPASSWORD=hunter2")

    written = temp_hermes_home / "a2a_audit.jsonl"
    assert written.is_file(), list(temp_hermes_home.iterdir())
    _assert_owner_only(written)

    record = json.loads(written.read_text(encoding="utf-8").strip())
    assert record["summary"] == "ran psql PGPASSWORD=hunter2", (
        "the audit log is an audit trail; redacting it would defeat its purpose"
    )


@posix_only
def test_a2a_audit_log_appends(temp_hermes_home, permissive_umask):
    """Second record appends rather than truncating."""
    from plugins.platforms.a2a import security

    security.audit("inbound", "peer.example", "t1", "first")
    security.audit("outbound", "peer.example", "t2", "second")

    lines = (
        (temp_hermes_home / "a2a_audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert [json.loads(line)["summary"] for line in lines] == ["first", "second"]


# ---------------------------------------------------------------------------
# batch_runner trajectories (batch_runner.py)
# ---------------------------------------------------------------------------

def _batch_prompt_result() -> dict:
    return {
        "success": True,
        "trajectory": [
            {"role": "assistant", "content": "ran it"},
            {"role": "tool", "content": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI"},
        ],
        "reasoning_stats": {"has_any_reasoning": True},
        "tool_stats": {},
        "metadata": {},
        "completed": True,
        "api_calls": 1,
        "toolsets_used": [],
    }


@posix_only
def test_batch_trajectory_jsonl_owner_only(tmp_path, monkeypatch, permissive_umask):
    """batch_N.jsonl carries full trajectories under the CWD, not HERMES_HOME."""
    from batch_runner import _process_batch_worker

    monkeypatch.setattr(
        "batch_runner._process_single_prompt", lambda *a, **kw: _batch_prompt_result()
    )

    out_dir = tmp_path / "data" / "myrun"
    out_dir.mkdir(parents=True)
    _process_batch_worker((0, [(0, {"prompt": "hi"})], out_dir, set(), {"verbose": False}))

    written = out_dir / "batch_0.jsonl"
    assert written.is_file(), list(out_dir.iterdir())
    _assert_owner_only(written)

    # Trajectories are training data — they must stay full-fidelity.
    entry = json.loads(written.read_text(encoding="utf-8").strip())
    assert entry["conversations"][1]["content"] == "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI"


@posix_only
def test_batch_trajectory_appends_across_prompts(tmp_path, monkeypatch, permissive_umask):
    """Two prompts in one batch append two lines to the same file."""
    from batch_runner import _process_batch_worker

    monkeypatch.setattr(
        "batch_runner._process_single_prompt", lambda *a, **kw: _batch_prompt_result()
    )

    out_dir = tmp_path / "data" / "myrun"
    out_dir.mkdir(parents=True)
    _process_batch_worker(
        (0, [(0, {"prompt": "a"}), (1, {"prompt": "b"})], out_dir, set(), {"verbose": False})
    )

    lines = (out_dir / "batch_0.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["prompt_index"] for line in lines] == [0, 1]


@posix_only
def test_batch_trajectory_existing_relaxed_file_is_not_retightened(
    tmp_path, monkeypatch, permissive_umask
):
    """A resumed run must not re-tighten a file the operator widened."""
    from batch_runner import _process_batch_worker

    monkeypatch.setattr(
        "batch_runner._process_single_prompt", lambda *a, **kw: _batch_prompt_result()
    )

    out_dir = tmp_path / "data" / "myrun"
    out_dir.mkdir(parents=True)
    target = out_dir / "batch_0.jsonl"
    target.write_text("", encoding="utf-8")
    os.chmod(target, 0o644)

    _process_batch_worker((0, [(0, {"prompt": "hi"})], out_dir, set(), {"verbose": False}))

    assert _mode(target) == 0o644


# ---------------------------------------------------------------------------
# /save snapshot directory (cli.py) + the shared secure_mkdir helper
# ---------------------------------------------------------------------------

@posix_only
def test_save_conversation_dir_owner_only(temp_hermes_home, permissive_umask):
    """Every file in sessions/saved is a transcript, so the listing is too."""
    import cli

    stub = SimpleNamespace(
        conversation_history=[{"role": "user", "content": "deploy with PGPASSWORD=hunter2"}],
        model="test-model",
        session_id="20260101_120000_abc123",
        session_start=datetime(2026, 1, 1, 12, 0, 0),
    )
    cli.HermesCLI.save_conversation(stub)

    saved_dir = temp_hermes_home / "sessions" / "saved"
    assert saved_dir.is_dir()
    _assert_owner_only(saved_dir)
    assert list(saved_dir.glob("hermes_conversation_*.json")), list(saved_dir.iterdir())


@posix_only
def test_secure_mkdir_creates_owner_only_and_preserves_existing(
    tmp_path, permissive_umask
):
    """Mode is applied at creation; an existing directory keeps its own."""
    from hermes_cli.config import secure_mkdir

    fresh = tmp_path / "fresh"
    secure_mkdir(fresh)
    _assert_owner_only(fresh)

    relaxed = tmp_path / "relaxed"
    relaxed.mkdir(mode=0o755)
    secure_mkdir(relaxed)
    assert _mode(relaxed) == 0o755, (
        "an existing directory's permissions must be left alone, matching "
        "_secure_dir's and open_private_append's create-only stance"
    )


@posix_only
def test_secure_mkdir_defers_to_managed_group_sharing(
    tmp_path, monkeypatch, permissive_umask
):
    """Managed (NixOS) mode deliberately group-shares HERMES_HOME's subdirs.

    The module creates them setgid group-writable (2770) and runs the service
    with ``UMask=0007`` so interactive users in the hermes group can share
    state with the gateway. Forcing 0o700 at creation would silently revoke
    that, and unlike ``_secure_dir``'s chmod it could not be reconciled by a
    later activation pass — so ``secure_mkdir`` skips managed mode exactly the
    way ``_secure_dir`` does.
    """
    from hermes_cli.config import secure_mkdir

    monkeypatch.setenv("HERMES_MANAGED", "1")
    parent = tmp_path / "sessions"
    parent.mkdir()
    os.chmod(parent, 0o2770)

    previous = os.umask(0o007)
    try:
        target = parent / "saved"
        secure_mkdir(target)
    finally:
        os.umask(previous)

    assert _mode(target) & stat.S_IRWXG, (
        "managed mode must keep hermes-group access on HERMES_HOME subdirs"
    )


# ---------------------------------------------------------------------------
# request_dump_*.json — already correct, pinned so it stays that way
# ---------------------------------------------------------------------------

@posix_only
def test_request_dump_fresh_file_is_owner_only_without_explicit_mode(
    tmp_path, permissive_umask
):
    """``agent/agent_runtime_helpers.py`` calls ``atomic_json_write`` with no
    ``mode=``, and that is *already* correct for this artifact: the temp file
    comes from ``tempfile.mkstemp`` (0o600) and ``_restore_file_mode`` is a
    no-op when there was no pre-existing file, so a fresh dump lands 0o600.

    No production change is made there. This test pins the invariant the
    absent argument silently depends on, so a future refactor of
    ``atomic_json_write``'s temp-file creation cannot regress request dumps to
    umask permissions unnoticed.
    """
    from utils import atomic_json_write

    dump = tmp_path / "request_dump_sess_20260101_000000_000000.json"
    atomic_json_write(dump, {"body": {"messages": [{"role": "user", "content": "hi"}]}}, default=str)

    _assert_owner_only(dump)
