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


@pytest.fixture
def managed_nixos_home(tmp_path, monkeypatch):
    """Simulate a managed NixOS install that shares state via the hermes group.

    Reproduces the three conditions ``nix/nixosModules.nix`` actually creates,
    all of which have to hold together for this to be a real reproduction:

    * ``HERMES_MANAGED=nixos``, as the systemd unit sets;
    * the parent at ``2770`` — setgid, group-rwx — which ``systemd.tmpfiles``
      pins for ``stateDir/.hermes`` in ``nix/nixosModules.nix`` so the gateway
      and interactive ``hostUsers`` share it;
    * ``umask 0007``, which the same module sets on the unit via
      ``UMask = "0007"``, commented "files created by the gateway should be
      group-writable so interactive users in the hermes group can read/write
      them".

    Setting only the env var would pass for the wrong reason: with a default
    ``umask 022`` and a non-setgid parent, a fresh dir lands 0755 and any
    "group access survived" assertion is satisfied by the ambient umask rather
    than by the carve-out under test.

    The dirs those tmpfiles rules DO pre-create are present; ``moa-traces`` is
    deliberately absent, because it is NOT in those rules. On a managed host it
    is therefore always created lazily at runtime, which is why the creation
    mode — not just the reconciliation — has to honour managed mode.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    os.chmod(home, 0o2770)
    # The dirs the tmpfiles rules DO pre-create, at the 2770 they pin. Present
    # so the fixture is a real managed host and not a half-built one, which
    # makes ``moa-traces``'s absence the *only* difference between it and its
    # siblings — the whole claim under test. Load-bearing, not decoration:
    # ``load_config()`` runs ``ensure_hermes_home()``, whose managed branch
    # raises when they are missing, and ``_traces_enabled_and_dir()`` swallows
    # that and returns None. Drop them and the test fails on "managed mode must
    # still create the trace dir" rather than passing while creating nothing.
    for subdir in ("cron", "sessions", "logs", "memories", "plugins"):
        d = home / subdir
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o2770)
    previous = os.umask(0o007)
    try:
        yield home
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

    lines = (
        (tmp_path / "trajectory_samples.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 2, lines
    assert json.loads(lines[0])["conversations"][0]["value"] == "first"
    assert json.loads(lines[1])["conversations"][0]["value"] == "second"


@posix_only
def test_failed_trajectory_jsonl_created_owner_only(
    tmp_path, monkeypatch, permissive_umask
):
    """The ``completed=False`` branch writes a different filename.

    ``save_trajectory`` picks ``failed_trajectories.jsonl`` instead of
    ``trajectory_samples.jsonl`` when the run did not complete. Both go through
    the same hardened open, but only the success filename was asserted, so the
    failure path was covered by implication rather than by a test. A failed
    trajectory holds the same verbatim tool output as a successful one — often
    more of it, since failures are what people debug and share.
    """
    monkeypatch.chdir(tmp_path)
    from agent.trajectory import save_trajectory

    save_trajectory([{"from": "human", "value": "boom"}], "m", False)

    written = tmp_path / "failed_trajectories.jsonl"
    assert written.is_file(), list(tmp_path.iterdir())
    _assert_owner_only(written)


@posix_only
def test_trajectory_existing_relaxed_file_is_not_retightened(
    tmp_path, monkeypatch, permissive_umask
):
    """A file the user deliberately widened keeps its mode (opt-in export)."""
    monkeypatch.chdir(tmp_path)
    from agent.trajectory import save_trajectory

    target = tmp_path / "trajectory_samples.jsonl"
    target.write_text("", encoding="utf-8")
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
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["messages"] == history


@posix_only
def test_save_conversation_retightens_a_pre_existing_permissive_snapshot(
    tmp_path, monkeypatch, permissive_umask
):
    """Overwriting an already-0644 snapshot must drop it to 0600.

    The sibling test above writes a *fresh* file, and on a fresh file
    ``atomic_json_write``'s ``mode=`` argument is a no-op: ``mkstemp`` already
    creates at 0600 and passing ``mode`` short-circuits the preserve/restore
    path. So a fresh-file assertion cannot tell whether ``mode=0o600`` is
    present at the call site at all — deleting it keeps that test green.

    Overwrite is the case that distinguishes them, because the default
    behaviour is to *preserve* the target's existing mode. It is reachable:
    the snapshot name has second resolution
    (``hermes_conversation_%Y%m%d_%H%M%S.json``), so two ``/save`` calls in the
    same second land on the same path, and a snapshot written by a pre-fix
    Hermes sits there at 0644 — receiving a fresh full transcript while keeping
    world-readable bits.

    The timestamp is frozen rather than raced so the collision is deterministic
    instead of dependent on wall-clock luck.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import cli

    frozen = datetime(2026, 1, 1, 12, 0, 0)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)

    target = home / "sessions" / "saved" / "hermes_conversation_20260101_120000.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"messages": ["stale"]}), encoding="utf-8")
    os.chmod(target, 0o644)

    history = [{"role": "user", "content": "fresh transcript"}]
    stub = SimpleNamespace(
        conversation_history=history,
        model="test-model",
        session_id="20260101_120000_abc123",
        session_start=frozen,
    )
    cli.HermesCLI.save_conversation(stub)

    assert json.loads(target.read_text(encoding="utf-8"))["messages"] == history, (
        "the overwrite did not land on the pre-existing path; the timestamp "
        "collision this test depends on did not happen"
    )
    _assert_owner_only(target)


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


@posix_only
def test_moa_trace_managed_mode_fresh_dir_keeps_group_sharing(managed_nixos_home):
    """A *newly created* trace dir on NixOS must stay group-shareable.

    This is the managed counterpart to the test above, and it is the case that
    matters on a managed host: ``moa-traces`` is not among the directories
    ``nix/nixosModules.nix`` pre-creates via ``systemd.tmpfiles`` (stateDir,
    .hermes, cron, sessions, logs, memories, plugins — all 2770), so it is
    always created lazily here and the mode passed at creation is the only
    thing that decides it. There is no reconciliation step to fall back on.

    Forcing 0700 here would not be cosmetic. The module shares ``$HERMES_HOME``
    between the gateway service and interactive ``hostUsers`` through the hermes
    group — 2770 + ``UMask = "0007"`` + a deliberate refusal to ``chown -R``,
    which strips setgid — and hostUsers get a ``~/.hermes`` symlink to that same
    stateDir. A 0700 trace dir created by whichever side runs first locks the
    other out with EACCES: the tracing feature itself, not just its permissions.

    Asserts group access survives rather than a literal octal: the exact bits
    depend on the inherited setgid and umask, and the contract is "the group can
    still get in", not "it is precisely 2770".

    Nothing is stubbed here. The claim under test is specifically that the
    *default*, ``get_hermes_home()``-derived ``moa-traces`` path is created
    lazily on a managed host, so the whole chain has to be real code: a real
    ``config.yaml`` turns the opt-in on, real ``load_config()`` reads it (which
    also runs ``ensure_hermes_home()``'s managed branch against the fixture's
    tmpfiles skeleton), and real ``_traces_enabled_and_dir()`` derives the path.
    """
    import agent.moa_trace as moa_trace

    trace_dir = managed_nixos_home / "moa-traces"
    assert not trace_dir.exists(), "fixture precondition: dir must be absent"

    (managed_nixos_home / "config.yaml").write_text(
        "moa:\n  save_traces: true\n  trace_dir: ''\n", encoding="utf-8"
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

    assert trace_dir.is_dir(), "managed mode must still create the trace dir"
    dir_mode = _mode(trace_dir)
    assert dir_mode & stat.S_IRWXG, (
        f"fresh managed-mode {trace_dir} dropped group access ({oct(dir_mode)}); "
        "the NixOS module's hermes-group sharing (2770 + UMask=0007) is broken, "
        "so an interactive hostUsers CLI and the gateway can no longer share "
        "the trace dir — one locks the other out with EACCES"
    )
    assert not dir_mode & stat.S_IRWXO, (
        f"managed mode must not widen to other ({oct(dir_mode)}); the umask "
        "0007 the unit sets is what bounds this, and it excludes other"
    )
