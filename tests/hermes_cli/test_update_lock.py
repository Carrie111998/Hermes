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

import builtins
import errno
import os
import subprocess
import sys
import time

import pytest

import hermes_cli.update_lock as update_lock_mod
from hermes_cli.update_lock import (
    COORDINATOR_TAKEOVER_PID_ENV,
    HANDOFF_PID_ENV,
    UPDATE_MARKER_MAX_AGE_SECONDS,
    UpdateLock,
    claim_desktop_handoff_marker,
    describe_holder,
    read_live_update,
    update_marker_path,
)

# A pid no live process owns. os.kill(pid, 0) must report it dead so a crashed
# updater can never wedge every future update. Deliberately larger than any
# platform's pid_t so it also covers the corrupt-marker path (OverflowError).
DEAD_PID = 4294967294
REAL_PID_ALIVE = update_lock_mod._pid_alive


@pytest.fixture
def marker(tmp_path):
    return tmp_path / ".hermes-update-in-progress"


@pytest.fixture(autouse=True)
def _current_test_process_is_visible(monkeypatch):
    """The hermetic runner hides its own PID from host process probes."""
    visible = {os.getpid(), os.getppid()}
    monkeypatch.setattr(
        update_lock_mod,
        "_pid_alive",
        lambda pid: pid in visible or REAL_PID_ALIVE(pid),
    )


def test_marker_path_follows_process_hermes_home(tmp_path, monkeypatch):
    """A non-profile custom install keeps its configured root."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert update_marker_path() == tmp_path / ".hermes-update-in-progress"


def test_named_profiles_share_one_install_wide_marker(tmp_path, monkeypatch):
    """Profiles mutate one checkout and therefore must contend on one lock."""
    root = tmp_path / "hermes-root"
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))
    alpha = update_marker_path()

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "beta"))
    beta = update_marker_path()

    assert alpha == beta == root / ".hermes-update-in-progress"


@pytest.mark.linux_only
def test_pid_probe_import_failure_and_unknown_os_error_fail_closed(monkeypatch):
    """A changing-checkout import failure cannot make a live claim look dead."""
    real_import = builtins.__import__

    def reject_gateway_import(name, *args, **kwargs):
        if name == "gateway" or name.startswith("gateway."):
            raise ImportError("checkout is between revisions")
        return real_import(name, *args, **kwargs)

    def indeterminate_probe(pid, signal):
        raise OSError(errno.EIO, "indeterminate process probe")

    monkeypatch.setattr(builtins, "__import__", reject_gateway_import)
    monkeypatch.setattr(update_lock_mod.os, "kill", indeterminate_probe)

    assert REAL_PID_ALIVE(4242) is True


def test_initial_claim_publishes_complete_payload_without_clobbering_race(
    marker, monkeypatch
):
    """A cross-language winner is never replaced by a partial Python claim."""
    winner = f"{os.getpid() + 100_000}\n{int(time.time())}\n"
    staged = None

    def competing_publish(source, destination):
        nonlocal staged
        staged = source.read_text(encoding="utf-8")
        destination.write_text(winner, encoding="utf-8")
        raise FileExistsError(destination)

    monkeypatch.setattr(update_lock_mod.os, "link", competing_publish)

    with pytest.raises(FileExistsError):
        update_lock_mod._create_marker_exclusive(
            marker, pid=os.getpid(), lease_at=time.time()
        )

    assert staged is not None
    staged_lines = staged.splitlines()
    assert len(staged_lines) == 2
    assert staged_lines[0] == str(os.getpid())
    assert int(staged_lines[1]) == pytest.approx(time.time(), abs=5)
    assert marker.read_text(encoding="utf-8") == winner
    assert not list(marker.parent.glob(f".{marker.name}.*.claim"))


def test_initial_claim_retries_short_os_writes(marker, monkeypatch):
    real_write = os.write
    writes = 0

    def short_write(fd, payload):
        nonlocal writes
        writes += 1
        return real_write(fd, payload[:2])

    monkeypatch.setattr(update_lock_mod.os, "write", short_write)
    update_lock_mod._create_marker_exclusive(
        marker, pid=os.getpid(), lease_at=time.time()
    )

    lines = marker.read_bytes().decode().splitlines()
    assert lines[0] == str(os.getpid())
    assert len(lines) == 2
    assert writes > 1


def test_desktop_handoff_claim_preserves_live_foreign_marker(marker, monkeypatch):
    owner_pid = os.getpid() + 10_000
    foreign_pid = os.getpid() + 20_000
    lease_at = int(time.time())
    foreign_payload = f"{foreign_pid}\n{lease_at}\n"
    marker.write_text(foreign_payload, encoding="utf-8")
    monkeypatch.setattr(
        update_lock_mod,
        "_pid_alive",
        lambda pid: pid in {owner_pid, foreign_pid},
    )

    reason = claim_desktop_handoff_marker(
        marker,
        owner_pid=owner_pid,
        desktop_pid=os.getpid(),
        lease_at=lease_at,
    )

    assert reason is not None
    assert "live foreign pid" in reason
    assert marker.read_text(encoding="utf-8") == foreign_payload


def test_desktop_handoff_claim_cas_replaces_authorized_bridge(marker, monkeypatch):
    owner_pid = os.getpid() + 10_000
    desktop_pid = os.getpid()
    lease_at = int(time.time()) - 30
    marker.write_text(f"{desktop_pid}\n{lease_at}\n", encoding="utf-8")
    monkeypatch.setattr(
        update_lock_mod,
        "_pid_alive",
        lambda pid: pid in {owner_pid, desktop_pid},
    )

    reason = claim_desktop_handoff_marker(
        marker,
        owner_pid=owner_pid,
        desktop_pid=desktop_pid,
        lease_at=lease_at,
    )

    assert reason is None
    assert marker.read_text(encoding="utf-8") == f"{owner_pid}\n{lease_at}\n"


def test_acquire_writes_pid_and_start_time(marker):
    lock = UpdateLock(path=marker)

    assert lock.acquire() is True
    assert lock.acquired is True

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) == os.getpid(), (
        "the Electron gate probes this pid for liveness"
    )
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


def test_two_named_profiles_contend_on_the_same_live_lock(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))
    first = UpdateLock()
    assert first.acquire() is True

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "beta"))
    second = UpdateLock()
    assert second.path == first.path
    assert second.acquire() is False
    assert second.holder is not None

    first.release()


def test_heartbeat_keeps_a_live_owner_past_the_fixed_age_ceiling(marker, monkeypatch):
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
    lock = UpdateLock(path=marker, heartbeat_seconds=0.01)
    assert lock.acquire() is True

    ancient = int(time.time()) - UPDATE_MARKER_MAX_AGE_SECONDS - 60
    marker.write_text(f"{os.getpid()}\n{ancient}\n", encoding="utf-8")

    deadline = time.monotonic() + 2
    lease_at = ancient
    while lease_at == ancient and time.monotonic() < deadline:
        time.sleep(0.01)
        lease_at = int(marker.read_text(encoding="utf-8").splitlines()[1])

    assert lease_at > ancient, "the owner refreshes its lease in the background"
    contender = UpdateLock(path=marker)
    assert contender.acquire() is False
    assert contender.holder is not None
    lock.release()


def test_heartbeat_never_reclaims_a_marker_that_changed_owner(marker, monkeypatch):
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
    lock = UpdateLock(path=marker, heartbeat_seconds=60)
    assert lock.acquire() is True

    foreign_pid = os.getpid() + 100_000
    foreign_payload = f"{foreign_pid}\n{int(time.time())}\n"
    marker.write_text(foreign_payload, encoding="utf-8")

    assert lock.refresh_lease() is False
    assert marker.read_text(encoding="utf-8") == foreign_payload
    lock.release()
    assert marker.exists(), "the former owner's release also leaves the new claim"


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


def test_live_owner_past_the_age_ceiling_is_never_stolen(marker, monkeypatch):
    """A wall-clock jump cannot admit a second checkout mutator."""
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
    long_ago = int(time.time()) - UPDATE_MARKER_MAX_AGE_SECONDS - 60
    marker.write_text(f"{os.getpid()}\n{long_ago}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert lock.holder is not None
    assert marker.exists()


@pytest.mark.parametrize(
    "body",
    ["", "not-a-pid\n123\n", "\n\n", "12345"],
    ids=["empty", "garbage-pid", "blank-lines", "no-start-time"],
)
def test_malformed_markers_fail_closed_without_being_deleted(marker, body):
    marker.write_bytes(body.encode("utf-8"))

    observation = read_live_update(path=marker)
    assert observation is not None and observation.pid is None
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert lock.holder is not None and lock.holder.pid is None
    assert marker.read_bytes().decode("utf-8") == body


@pytest.mark.parametrize("lease", ["not-a-time", "nan", "inf", "-inf", "-1", "1.5"])
def test_malformed_lease_fails_closed_even_when_pid_is_live(marker, lease):
    marker.write_text(f"{os.getpid()}\n{lease}\n", encoding="utf-8")

    observation = read_live_update(path=marker)
    assert observation is not None and observation.pid is None
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert marker.read_text(encoding="utf-8") == f"{os.getpid()}\n{lease}\n"


def test_live_pid_with_missing_lease_fails_closed(marker):
    body = str(os.getpid())
    marker.write_text(body, encoding="utf-8")

    observation = read_live_update(path=marker)
    assert observation is not None and observation.pid is None
    assert UpdateLock(path=marker).acquire() is False
    assert marker.read_text(encoding="utf-8") == body


@pytest.mark.parametrize(
    "body",
    [
        "1e3\n123\n",
        "+42\n123\n",
        "0x10\n123\n",
        "4294967296\n123\n",
        "42\n1e3\n",
        "42\n+123\n",
        "42\n0x10\n",
        "42\n9007199254740992\n",
        "42\n123\nextra\n",
        "42\r123\r",
    ],
)
def test_noncanonical_or_overflow_wire_payload_fails_closed(marker, body):
    marker.write_bytes(body.encode("utf-8"))

    observation = read_live_update(path=marker)
    assert observation is not None and observation.pid is None
    assert UpdateLock(path=marker).acquire() is False
    assert marker.read_bytes().decode("utf-8") == body


def test_crlf_two_line_wire_payload_remains_compatible(marker):
    now = int(time.time())
    marker.write_bytes(f"{os.getpid()}\r\n{now}\r\n".encode())

    holder = read_live_update(path=marker)
    assert holder is not None and holder.pid == os.getpid()


def test_unreadable_existing_marker_fails_closed(marker):
    marker.mkdir()

    observation = read_live_update(path=marker)
    assert observation is not None and observation.pid is None
    assert UpdateLock(path=marker).acquire() is False
    assert marker.is_dir()


def test_stale_marker_is_removed_on_read(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert not marker.exists(), "whoever notices a stale marker clears it"


def test_stale_cleanup_never_unlinks_a_new_owner(marker, monkeypatch):
    """CAS recheck closes the stale-read/new-heartbeat deletion race."""
    now = int(time.time())
    new_pid = os.getpid() + 100_000
    marker.write_text(f"{DEAD_PID}\n{now}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: pid == new_pid)

    original_enter = update_lock_mod._MarkerMutex.__enter__

    def replace_before_cleanup(self):
        result = original_enter(self)
        marker.write_text(f"{new_pid}\n{now}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        update_lock_mod._MarkerMutex, "__enter__", replace_before_cleanup
    )

    holder = read_live_update(path=marker)
    assert holder is not None and holder.pid == new_pid
    assert marker.exists()
    assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == new_pid


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

    assert str(os.getpid()) in message, (
        "the user needs the pid to find the other update"
    )
    assert "already running" in message


def test_unwritable_marker_location_fails_closed(tmp_path):
    """Never mutate the checkout when exclusivity cannot be established."""
    lock = UpdateLock(path=tmp_path / "nonexistent-file" / "marker")
    (tmp_path / "nonexistent-file").write_text(
        "i am a file, not a dir", encoding="utf-8"
    )

    assert lock.acquire() is False
    assert lock.acquired is False, "nothing was written, so there is nothing to release"


def test_heartbeat_failure_is_reported_and_arms_the_failure_fence(marker, monkeypatch):
    lock = UpdateLock(path=marker, heartbeat_seconds=60)
    assert lock.acquire() is True

    def fail_write(*args, **kwargs):
        raise PermissionError("marker became unwritable")

    monkeypatch.setattr(update_lock_mod, "_atomic_write_marker", fail_write)
    assert lock.refresh_lease() is False
    assert lock._heartbeat_failed is True
    lock.release()


def test_missing_marker_is_a_heartbeat_failure_not_an_ownership_handoff(marker):
    lock = UpdateLock(path=marker, heartbeat_seconds=60)
    assert lock.acquire() is True
    marker.unlink()

    assert lock.refresh_lease() is False
    assert lock._heartbeat_failed is True
    lock.release()


def test_takeover_fails_closed_when_marker_mutex_is_unavailable(marker, monkeypatch):
    body = f"{os.getpid()}\n{int(time.time())}\n"
    marker.write_text(body, encoding="utf-8")
    monkeypatch.setenv(COORDINATOR_TAKEOVER_PID_ENV, str(os.getpid()))

    def fail_mutex(self):
        raise update_lock_mod.UpdateLockUnavailable("mutex denied")

    monkeypatch.setattr(update_lock_mod._MarkerMutex, "__enter__", fail_mutex)
    assert UpdateLock(path=marker).take_over_handoff() is False
    assert marker.read_text(encoding="utf-8") == body


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_marker_mutex_rejects_windows_junction(marker, tmp_path):
    target = tmp_path / "mutex-target"
    target.mkdir()
    mutex_path = marker.with_name(f"{marker.name}.mutex")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(mutex_path), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stdout} {result.stderr}")

    try:
        with pytest.raises(update_lock_mod.UpdateLockUnavailable):
            with update_lock_mod._MarkerMutex(marker):
                pass
        assert not (target / "unexpected").exists()
    finally:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "rmdir", str(mutex_path)],
            capture_output=True,
            check=False,
        )


@pytest.mark.windows_only
def test_marker_reader_rejects_reparse_parent_without_following_it(marker, tmp_path):
    target = tmp_path / "marker-target"
    target.mkdir()
    redirected = tmp_path / "redirected-marker-root"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirected), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stdout} {result.stderr}")

    redirected_marker = redirected / marker.name
    (target / marker.name).write_bytes(
        f"{os.getpid()}\\n{int(time.time())}\\n".encode("ascii")
    )
    try:
        observation = read_live_update(path=redirected_marker)
        assert observation is not None and observation.pid is None
        assert "reparse" in (observation.unavailable_reason or "")
    finally:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "rmdir", str(redirected)],
            capture_output=True,
            check=False,
        )


class TestHandoffFromOrchestratingUpdater:
    """The Tauri updater holds the marker, then spawns ``hermes update``.

    The regression: the child saw its own parent's live marker and exited 2,
    so every GUI update failed with "Hermes is still running" and retrying
    just re-ran the same self-deadlock. The parent names its pid in
    HANDOFF_PID_ENV; a live holder matching it is our own orchestrator.
    """

    def test_child_runs_under_the_parents_live_claim(self, marker, monkeypatch):
        # Stand in for the parent updater with our own (live) pid.
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()

    def test_long_running_child_refreshes_the_borrowed_parent_lease(
        self, marker, monkeypatch
    ):
        monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
        parent_pid = os.getpid()
        marker.write_text(f"{parent_pid}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(parent_pid))

        child_stage = UpdateLock(path=marker, heartbeat_seconds=0.01)
        assert child_stage.acquire() is True
        assert child_stage.acquired is False
        ancient = int(time.time()) - UPDATE_MARKER_MAX_AGE_SECONDS - 60
        marker.write_text(f"{parent_pid}\n{ancient}\n", encoding="utf-8")

        deadline = time.monotonic() + 2
        lease_at = ancient
        while lease_at == ancient and time.monotonic() < deadline:
            time.sleep(0.01)
            lease_at = int(marker.read_text(encoding="utf-8").splitlines()[1])

        assert lease_at > ancient
        monkeypatch.delenv(HANDOFF_PID_ENV)
        contender = UpdateLock(path=marker)
        assert contender.acquire() is False
        child_stage.release()
        assert marker.exists(), "borrowed ownership never unlinks the parent marker"

    def test_handoff_pid_that_is_not_the_live_holder_grants_nothing(
        self, marker, monkeypatch
    ):
        """The env var alone must not bypass the lock."""
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid() + 1))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None

    @pytest.mark.parametrize(
        "value",
        ["", "not-a-pid", "-1", "0"],
        ids=["empty", "garbage", "negative", "zero"],
    )
    def test_malformed_handoff_values_fall_back_to_refusal(
        self, marker, monkeypatch, value
    ):
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, value)

        assert UpdateLock(path=marker).acquire() is False

    def test_handoff_env_with_no_marker_claims_normally(self, marker, monkeypatch):
        """A handoff pid must not stop us writing our own claim when unlocked."""
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is True
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


class TestCoordinatorTakeover:
    @pytest.fixture(autouse=True)
    def _liveness_pinned_true(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)

    def test_explicit_child_atomically_takes_over_the_matching_parent(
        self, marker, monkeypatch
    ):
        parent_pid = os.getpid()
        parent = UpdateLock(path=marker, heartbeat_seconds=60)
        assert parent.acquire() is True

        child_pid = parent_pid + 100_000
        monkeypatch.setenv(COORDINATOR_TAKEOVER_PID_ENV, str(parent_pid))
        monkeypatch.setattr("hermes_cli.update_lock.os.getpid", lambda: child_pid)
        child = UpdateLock(path=marker, heartbeat_seconds=60)

        assert child.take_over_handoff() is True
        lines = marker.read_text(encoding="utf-8").splitlines()
        assert lines == [str(child_pid), lines[1]], "wire format remains two lines"

        parent.release()
        assert marker.exists(), "the former parent cannot delete the child's claim"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == child_pid

        child.release()
        assert not marker.exists()

    def test_takeover_env_grants_nothing_when_owner_does_not_match(
        self, marker, monkeypatch
    ):
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(COORDINATOR_TAKEOVER_PID_ENV, str(os.getpid() + 1))

        child = UpdateLock(path=marker)
        assert child.take_over_handoff() is False
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()

    @pytest.mark.parametrize("value", ["", "bad", "0", "-1"])
    def test_malformed_takeover_env_never_mutates_marker(
        self, marker, monkeypatch, value
    ):
        body = f"{os.getpid()}\n{int(time.time())}\n"
        marker.write_text(body, encoding="utf-8")
        monkeypatch.setenv(COORDINATOR_TAKEOVER_PID_ENV, value)

        assert UpdateLock(path=marker).take_over_handoff() is False
        assert marker.read_text(encoding="utf-8") == body


class TestAncestryHandoff:
    """Staged updaters older than the HANDOFF_PID_ENV export never send it.

    ``hermes-setup`` under ``~/.hermes`` is only refreshed by a full installer
    run, so an updated checkout (new lock) driven by a pre-handoff staged
    updater (old parent) deadlocks on exit 2 forever unless the child also
    recognizes a live holder that is its own process ancestor.

    ``_pid_alive`` is pinned True here because the hermetic conftest guards
    ``os.kill`` probes of pids outside the test subtree (our ppid included);
    liveness has its own coverage above — ancestry is what's under test.
    """

    @pytest.fixture(autouse=True)
    def _liveness_pinned_true(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "hermes_cli.update_lock._is_ancestor_pid",
            lambda pid: pid == os.getppid(),
        )

    def test_marker_owned_by_our_parent_process_is_our_orchestrator(self, marker):
        marker.write_text(f"{os.getppid()}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True, "a live ancestor's claim is the one we run under"
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getppid()

    def test_live_non_ancestor_holder_is_still_refused(self, marker):
        """Ancestry must not open the lock to unrelated concurrent updaters."""
        marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None
        assert lock.holder.pid == DEAD_PID
