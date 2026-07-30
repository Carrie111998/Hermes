"""Cross-process update mutual exclusion (``hermes_cli.update_lock``).

Three surfaces can start an update of one install tree: a terminal ``hermes
update``, the dashboard's Update button (which spawns that same command
detached), and the desktop's Update button (Tauri updater → install-mode
bootstrap on its failure screen). Before the shared lock, two of them could run
concurrently and rewrite source under a live interpreter — observed in the wild
as an installer ``git checkout`` rewinding the checkout ~9k commits while a
dashboard-spawned ``hermes update`` was mid-``npm install``, which then failed
against the rewound tree's manifests.

These exercise the real marker file against a temp home — no mocks — because
the contract that matters is what the Rust updater and the Electron gate see on
disk.
"""

from __future__ import annotations

import os
import sys
import time
import types

import pytest

import hermes_cli.update_lock as update_lock_module
from hermes_cli.update_lock import (
    UPDATE_MARKER_MAX_AGE_SECONDS,
    UpdateLock,
    describe_holder,
    read_live_update,
    update_marker_path,
)

# A pid no live process owns. os.kill(pid, 0) must report it dead so a crashed
# updater can never wedge every future update. Deliberately larger than any
# platform's pid_t so it also covers the corrupt-marker path (OverflowError).
DEAD_PID = 4294967294


@pytest.fixture
def marker(tmp_path):
    return tmp_path / ".hermes-update-in-progress"


def test_marker_path_follows_process_hermes_home(tmp_path, monkeypatch):
    """The lock must land where the Rust updater and Electron gate look.

    All three resolve the *process* HERMES_HOME; a profile-scoped path would
    put the lock somewhere the other two owners never read.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert update_marker_path() == tmp_path / ".hermes-update-in-progress"


def test_acquire_writes_pid_and_start_time(marker):
    lock = UpdateLock(path=marker)

    assert lock.acquire() is True
    assert lock.acquired is True

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) == os.getpid(), "the Electron gate probes this pid for liveness"
    assert int(lines[1]) == pytest.approx(time.time(), abs=5)
    assert len(lines) == 2, "wire format is exactly pid + started_at"


def test_second_acquire_is_refused_while_the_first_is_live(marker):
    """The bug: two updaters mutating one checkout at the same time."""
    first = UpdateLock(path=marker)
    assert first.acquire() is True

    second = UpdateLock(path=marker)
    assert second.acquire() is False
    assert second.holder is not None
    assert second.holder.pid == os.getpid()
    assert second.acquired is False


def test_descendant_borrows_identified_updater_ancestor_marker(marker, monkeypatch):
    """The identified updater owns the marker through launch shims and rebuild."""
    started_at = int(time.time())
    updater_pid = 220
    updater_name = "hermes-setup.exe" if os.name == "nt" else "hermes-setup"
    updater_exe = marker.parent / updater_name
    launcher = types.SimpleNamespace(pid=110)
    updater = types.SimpleNamespace(
        pid=updater_pid,
        exe=lambda: str(updater_exe),
        cmdline=lambda: [str(updater_exe), "--update", "--branch", "main"],
    )
    current = types.SimpleNamespace(parents=lambda: [launcher, updater])
    monkeypatch.setitem(
        sys.modules, "psutil", types.SimpleNamespace(Process=lambda: current)
    )
    monkeypatch.setattr(
        update_lock_module, "_pid_alive", lambda pid: pid == updater_pid
    )
    marker.write_text(f"{updater_pid}\n{started_at}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)

    assert lock.acquire() is True
    assert lock.acquired is False, "the child borrows rather than replaces the parent lock"
    assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == updater_pid

    lock.release()
    assert marker.exists(), "the parent owns the marker through the rebuild stage"


def test_unrelated_ancestor_cannot_lend_update_marker(marker, monkeypatch):
    started_at = int(time.time())
    updater_pid = 220
    unrelated = types.SimpleNamespace(
        pid=updater_pid,
        exe=lambda: str(marker.parent / "terminal.exe"),
        cmdline=lambda: ["terminal.exe", "--update"],
    )
    current = types.SimpleNamespace(parents=lambda: [unrelated])
    monkeypatch.setitem(
        sys.modules, "psutil", types.SimpleNamespace(Process=lambda: current)
    )
    monkeypatch.setattr(update_lock_module, "_pid_alive", lambda _pid: True)
    marker.write_text(f"{updater_pid}\n{started_at}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)

    assert lock.acquire() is False
    assert lock.holder is not None


def test_install_mode_helper_cannot_lend_update_marker(marker, monkeypatch):
    started_at = int(time.time())
    updater_pid = 220
    updater_name = "hermes-setup.exe" if os.name == "nt" else "hermes-setup"
    install_mode = types.SimpleNamespace(
        pid=updater_pid,
        exe=lambda: str(marker.parent / updater_name),
        cmdline=lambda: [str(marker.parent / updater_name)],
    )
    current = types.SimpleNamespace(parents=lambda: [install_mode])
    monkeypatch.setitem(
        sys.modules, "psutil", types.SimpleNamespace(Process=lambda: current)
    )
    monkeypatch.setattr(update_lock_module, "_pid_alive", lambda _pid: True)
    marker.write_text(f"{updater_pid}\n{started_at}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)

    assert lock.acquire() is False
    assert lock.holder is not None


def test_refused_lock_does_not_delete_the_live_owners_marker(marker):
    first = UpdateLock(path=marker)
    first.acquire()

    second = UpdateLock(path=marker)
    second.acquire()
    second.release()

    assert marker.exists(), "a refused claimant must never clear the live owner's lock"

    first.release()
    assert not marker.exists()


def test_release_leaves_a_marker_a_handoff_partner_now_owns(marker):
    """The desktop writes the marker, then the Tauri updater takes ownership.

    Releasing must not delete a marker whose pid is no longer ours — that would
    reopen the gate while the partner is still mid-update.
    """
    lock = UpdateLock(path=marker)
    lock.acquire()

    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")
    lock.release()

    assert marker.exists(), "the partner's marker is not ours to remove"


def test_dead_owner_is_reclaimed_not_honored(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is True
    assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


def test_owner_past_the_age_ceiling_is_reclaimed(marker):
    """A live-but-wedged updater must not hold the lock forever."""
    long_ago = int(time.time()) - UPDATE_MARKER_MAX_AGE_SECONDS - 60
    marker.write_text(f"{os.getpid()}\n{long_ago}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is True


@pytest.mark.parametrize(
    "body",
    ["", "not-a-pid\n123\n", "\n\n", "12345"],
    ids=["empty", "garbage-pid", "blank-lines", "no-start-time"],
)
def test_malformed_markers_never_block_an_update(marker, body):
    marker.write_text(body, encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert UpdateLock(path=marker).acquire() is True


def test_stale_marker_is_removed_on_read(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert not marker.exists(), "whoever notices a stale marker clears it"


def test_absent_marker_reports_no_live_update(marker):
    assert read_live_update(path=marker) is None


def test_context_manager_releases_even_on_exception(marker):
    with pytest.raises(RuntimeError):
        with UpdateLock(path=marker) as lock:
            assert lock.acquired is True
            raise RuntimeError("update blew up mid-flight")

    assert not marker.exists(), "a crashed update must not strand the lock"


def test_describe_holder_names_the_pid_and_elapsed_time(marker):
    lock = UpdateLock(path=marker)
    lock.acquire()

    holder = read_live_update(path=marker)
    assert holder is not None
    message = describe_holder(holder)

    assert str(os.getpid()) in message, "the user needs the pid to find the other update"
    assert "already running" in message


def test_unwritable_marker_location_does_not_block_the_update(tmp_path):
    """Degrade to pre-lock behavior rather than refusing to update at all.

    An unwritable marker path is a worse reason to block an update than the
    race the lock prevents.
    """
    lock = UpdateLock(path=tmp_path / "nonexistent-file" / "marker")
    (tmp_path / "nonexistent-file").write_text("i am a file, not a dir", encoding="utf-8")

    assert lock.acquire() is True
    assert lock.acquired is False, "nothing was written, so there is nothing to release"
