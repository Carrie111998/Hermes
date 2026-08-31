"""A runtime must not start INSIDE the update's mutation window (#99450 R3-2).

The pre-mutation quiesce re-inventories the fleet at the gate and sweeps
again after the stops, which closes the window it can see. It cannot close
the window it cannot see: between the final recollection and the first byte
written to the checkout, a gateway/dashboard/serve can be launched — by a
supervisor, by the Desktop app, by an operator — and that interpreter will
import from a tree the update is halfway through replacing. Re-collecting
harder never fixes a TOCTOU; only a barrier the starter itself honours does.

So startup registration takes a durable, machine- and profile-aware lease
check. The updater acquires the barrier BEFORE its final inventory and
releases it only after the mutation and the relaunch cleanup are done; every
startup path consults it before it initializes anything. The barrier lives on
disk next to the spawn ledger, so it survives the detached-updater process
boundary that in-memory authorization cannot.

Fail-closed throughout: an unreadable barrier blocks startup, and only a
*provably dead* owner (or an expired lease whose owner cannot be probed at
all) clears one — the same "proof, not absence of evidence" rule the
supervised-stop authority uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

import hermes_cli.process_identity as pi
from hermes_cli import update_quiesce
from hermes_cli.update_inventory import UpdatePlan


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """A machine root of our own, for the ledger AND the barrier."""
    machine_root = tmp_path / "hermes-root"
    machine_root.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda: machine_root
    )
    pi.forget_startup_barrier_ownership()
    yield machine_root
    pi.forget_startup_barrier_ownership()


def _own_identity():
    import psutil

    return os.getpid(), float(psutil.Process(os.getpid()).create_time())


def _write_barrier(root, **overrides):
    pid, create = _own_identity()
    record = {
        "install": pi.install_id(),
        "machine": pi.machine_id(),
        "owner_pid": pid,
        "owner_create": create,
        "profiles": [],
        "phase": "mutating",
        "acquired_at": time.time(),
        "expires_at": time.time() + 600.0,
        "reason": "test",
    }
    record.update(overrides)
    path = root / pi.STARTUP_BARRIER_FILENAME
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.fixture()
def foreign_owner():
    """A real, live process that is not us — a plausible other updater."""
    import psutil

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc.pid, float(psutil.Process(proc.pid).create_time())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _dead_pid() -> tuple:
    """A ``(pid, create_time)`` pair for a process that is provably gone."""
    import psutil

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    create = float(psutil.Process(proc.pid).create_time())
    proc.wait(timeout=30)
    return proc.pid, create


# ---------------------------------------------------------------------------
# What blocks a startup, and what does not
# ---------------------------------------------------------------------------


class TestTheBarrierBlocksExactlyTheRightStartups:
    def test_no_barrier_file_is_a_clear_machine(self, root):
        assert pi.startup_barrier_reason("gateway") == ""

    def test_a_live_owner_blocks_startup(self, root):
        _write_barrier(root)
        reason = pi.startup_barrier_reason("gateway")
        assert reason
        assert "update" in reason.lower()

    def test_another_installs_barrier_does_not_block_us(self, root):
        _write_barrier(root, install="some-other-checkout")
        assert pi.startup_barrier_reason("gateway") == ""

    def test_another_machines_barrier_does_not_block_us(self, root):
        """A shared/NFS Hermes root must not stop an unrelated host."""
        _write_barrier(root, machine="some-other-host")
        assert pi.startup_barrier_reason("gateway") == ""

    def test_a_profile_scoped_barrier_blocks_only_its_profiles(self, root):
        _write_barrier(root, profiles=["ops"])
        assert pi.startup_barrier_reason("gateway", profile="ops")
        assert pi.startup_barrier_reason("gateway", profile="default") == ""

    def test_an_empty_profile_list_means_the_whole_checkout(self, root):
        _write_barrier(root, profiles=[])
        assert pi.startup_barrier_reason("gateway", profile="anything")

    def test_the_relaunching_phase_lets_the_updaters_own_restarts_through(
        self, root
    ):
        """Otherwise the update deadlocks against its own relaunch.

        The record stays on disk — it is still the recovery marker — but the
        mutation it guarded is finished, so a fresh interpreter reading the
        new tree is exactly what the update wants next.
        """
        path = _write_barrier(root, phase="relaunching")
        assert pi.startup_barrier_reason("gateway") == ""
        assert path.exists()


class TestTheBarrierFailsClosed:
    def test_an_unreadable_barrier_blocks_startup(self, root):
        (root / pi.STARTUP_BARRIER_FILENAME).write_text("{not json", encoding="utf-8")
        reason = pi.startup_barrier_reason("gateway")
        assert reason
        assert "unreadable" in reason.lower()

    def test_a_barrier_that_is_not_an_object_blocks_startup(self, root):
        (root / pi.STARTUP_BARRIER_FILENAME).write_text("[]", encoding="utf-8")
        assert pi.startup_barrier_reason("gateway")

    def test_an_owner_that_cannot_be_probed_blocks_until_it_expires(
        self, root, monkeypatch
    ):
        monkeypatch.setattr(pi, "_pid_alive_matches", lambda pid, create: None)
        path = _write_barrier(root, expires_at=time.time() + 600.0)
        assert pi.startup_barrier_reason("gateway")
        assert path.exists()

    def test_an_expired_unprovable_barrier_is_recovered(self, root, monkeypatch):
        """The wedge escape: an updater killed so hard we cannot even ask."""
        monkeypatch.setattr(pi, "_pid_alive_matches", lambda pid, create: None)
        path = _write_barrier(root, expires_at=time.time() - 1.0)
        assert pi.startup_barrier_reason("gateway") == ""
        assert not path.exists()

    def test_a_live_owner_is_not_expired_out_from_under_the_update(self, root):
        """A slow-but-alive updater keeps its barrier past the TTL."""
        _write_barrier(root, expires_at=time.time() - 1.0)
        assert pi.startup_barrier_reason("gateway")

    def test_a_provably_dead_owner_is_recovered(self, root):
        pid, create = _dead_pid()
        path = _write_barrier(root, owner_pid=pid, owner_create=create)
        assert pi.startup_barrier_reason("gateway") == ""
        assert not path.exists()


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


class TestStartupWaitsThenRefuses:
    def test_a_clear_machine_returns_immediately(self, root):
        pi.await_startup_clearance("gateway", timeout=0.0)

    def test_an_active_barrier_refuses_after_the_budget(self, root):
        _write_barrier(root)
        with pytest.raises(pi.StartupBarrierActive) as excinfo:
            pi.await_startup_clearance("gateway", timeout=0.2, poll_interval=0.05)
        assert "update" in str(excinfo.value).lower()

    def test_the_wait_budget_honours_the_operator_override(
        self, root, monkeypatch
    ):
        """A site whose supervisor start timeout is tighter than ours."""
        _write_barrier(root)
        monkeypatch.setenv(pi.STARTUP_BARRIER_WAIT_ENV, "0")
        slept: list = []
        with pytest.raises(pi.StartupBarrierActive):
            pi.await_startup_clearance(
                "gateway", sleep=lambda seconds: slept.append(seconds)
            )
        assert slept == [], "a zero budget must refuse without waiting"

    def test_an_unparsable_override_falls_back_to_the_default(
        self, root, monkeypatch
    ):
        monkeypatch.setenv(pi.STARTUP_BARRIER_WAIT_ENV, "soon")
        assert pi._default_barrier_wait() == pi.STARTUP_BARRIER_WAIT_TIMEOUT

    def test_a_barrier_released_mid_wait_lets_startup_through(self, root):
        path = _write_barrier(root)
        released = {"done": False}

        def _sleep(_seconds):
            path.unlink()
            released["done"] = True

        pi.await_startup_clearance(
            "gateway", timeout=5.0, poll_interval=0.01, sleep=_sleep
        )
        assert released["done"]


# ---------------------------------------------------------------------------
# Acquire / release, across process boundaries
# ---------------------------------------------------------------------------


class TestAcquireAndRelease:
    def test_acquire_writes_a_durable_record_and_blocks_startups(self, root):
        assert pi.acquire_startup_barrier(reason="update") is True
        path = root / pi.STARTUP_BARRIER_FILENAME
        assert path.exists()
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["install"] == pi.install_id()
        assert record["machine"] == pi.machine_id()
        assert record["owner_pid"] == os.getpid()
        assert record["phase"] == "mutating"
        assert pi.startup_barrier_reason("gateway")

    def test_a_second_updater_cannot_take_a_live_barrier(
        self, root, foreign_owner
    ):
        pid, create = foreign_owner
        _write_barrier(root, owner_pid=pid, owner_create=create)
        assert pi.acquire_startup_barrier(reason="second") is False

    def test_a_stale_barrier_is_taken_over(self, root):
        pid, create = _dead_pid()
        _write_barrier(root, owner_pid=pid, owner_create=create)
        assert pi.acquire_startup_barrier(reason="takeover") is True
        record = json.loads(
            (root / pi.STARTUP_BARRIER_FILENAME).read_text(encoding="utf-8")
        )
        assert record["owner_pid"] == os.getpid()

    def test_an_unreadable_barrier_refuses_acquisition(self, root):
        """We cannot prove nobody else is mutating, so we do not mutate."""
        (root / pi.STARTUP_BARRIER_FILENAME).write_text("garbage", encoding="utf-8")
        assert pi.acquire_startup_barrier(reason="update") is False

    def test_release_removes_our_own_record(self, root):
        pi.acquire_startup_barrier(reason="update")
        assert pi.release_startup_barrier() is True
        assert not (root / pi.STARTUP_BARRIER_FILENAME).exists()
        assert pi.startup_barrier_reason("gateway") == ""

    def test_release_never_drops_another_owners_record(self, root):
        """A parent command boundary must not free the detached child's lease."""
        path = _write_barrier(root, owner_pid=os.getpid() + 1, owner_create=None)
        assert pi.release_startup_barrier() is False
        assert path.exists()

    def test_the_phase_flip_is_owner_scoped_and_durable(self, root):
        pi.acquire_startup_barrier(reason="update")
        assert pi.set_startup_barrier_phase("relaunching") is True
        record = json.loads(
            (root / pi.STARTUP_BARRIER_FILENAME).read_text(encoding="utf-8")
        )
        assert record["phase"] == "relaunching"
        assert pi.startup_barrier_reason("gateway") == ""

    def test_a_detached_updater_inherits_the_obligation_not_the_process(
        self, root
    ):
        """The record is the lease; the process that wrote it may be gone.

        This is the shape the real update takes on Windows and under
        ``systemd-run``: the command boundary that acquired is not the
        process that finishes the job.
        """
        result = _run_entry_point(
            root,
            """
            import sys
            import hermes_cli.process_identity as pi
            sys.exit(0 if pi.acquire_startup_barrier(reason="detached") else 3)
            """,
        )
        assert result.returncode == 0, result.stderr
        # The child exited; its lease is still on disk and still fail-closed
        # for anyone who cannot prove the owner is gone.
        record = json.loads(
            (root / pi.STARTUP_BARRIER_FILENAME).read_text(encoding="utf-8")
        )
        assert record["reason"] == "detached"
        # And because that owner IS provably dead now, the machine recovers
        # instead of staying wedged forever.
        assert pi.startup_barrier_reason("gateway") == ""


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The registration path itself
# ---------------------------------------------------------------------------


class TestRegistrationRefusesUnderTheBarrier:
    def test_register_self_refuses_and_writes_nothing(self, root):
        _write_barrier(root)
        assert pi.register_self("serve", detail={"profile": "default"}) is False
        assert not (root / pi.LEDGER_FILENAME).exists()

    def test_register_self_works_once_the_barrier_is_gone(self, root):
        assert pi.register_self("serve", detail={"profile": "default"}) is True
        assert (root / pi.LEDGER_FILENAME).exists()


# ---------------------------------------------------------------------------
# The real startup entry points, in real processes
# ---------------------------------------------------------------------------


def _run_entry_point(root, body: str, env_extra=None):
    script = textwrap.dedent(body)
    env = dict(os.environ)
    env["HERMES_HOME"] = str(root)
    env["PYTHONPATH"] = str(_repo_root())
    # The entry points wait out an ordinary mutation window before refusing.
    # These cases assert the REFUSAL, so shorten the budget rather than pay
    # 90 seconds per subprocess for it.
    env.setdefault(pi.STARTUP_BARRIER_WAIT_ENV, "1")
    env.pop("HERMES_SPAWN", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_repo_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestNoRuntimeInitializesInTheSkewWindow:
    """Real concurrent startups, launched while the gate is held.

    Each asserts the negative that matters: the process did not initialize
    and did not register — so it never imported the half-written checkout,
    and the updater's final inventory stays true through the mutation.
    """

    def test_a_real_serve_startup_refuses_before_it_initializes_anything(
        self, root
    ):
        _write_barrier(root)
        # `apply_nofile_soft_limit` is the first thing `start_server` does
        # after the gate. If the gate ever stops firing first, this exits 88
        # instead of binding a port — the test cannot leave a live server
        # behind either way.
        result = _run_entry_point(
            root,
            """
            import sys
            import hermes_cli.resource_limits as rl
            def _boom():
                raise SystemExit(88)
            rl.apply_nofile_soft_limit = _boom
            import hermes_cli.web_server as ws
            ws.apply_nofile_soft_limit = _boom
            try:
                ws.start_server(host="127.0.0.1", port=0, open_browser=False,
                                headless=True)
            except SystemExit as exc:
                sys.exit(exc.code if isinstance(exc.code, int) else 1)
            sys.exit(0)
            """,
        )
        assert result.returncode not in (0, 88), (
            f"serve initialized inside the mutation window: rc="
            f"{result.returncode}\n{result.stdout}\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "update" in combined.lower()
        assert not (root / pi.LEDGER_FILENAME).exists(), "it registered anyway"

    def test_a_real_gateway_startup_refuses_before_it_registers(self, root):
        _write_barrier(root)
        result = _run_entry_point(
            root,
            """
            import sys
            sys.argv = ["hermes-gateway", "--config", "/nonexistent/g.yaml"]
            import gateway.run as g
            g.main()
            sys.exit(0)
            """,
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "update" in combined.lower()
        assert "FileNotFoundError" not in combined, (
            "the gateway got past the gate and reached argument handling"
        )
        assert not (root / pi.LEDGER_FILENAME).exists()

    def test_a_concurrent_startup_waits_and_proceeds_after_the_release(
        self, root
    ):
        """The other half of the contract: the gate is a barrier, not a wall."""
        path = _write_barrier(root)
        marker = root / "past-the-gate"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import pathlib
                    import hermes_cli.process_identity as pi
                    pi.await_startup_clearance("serve", timeout=120.0,
                                               poll_interval=0.05)
                    pathlib.Path({str(marker)!r}).write_text("started")
                    """
                ),
            ],
            cwd=str(_repo_root()),
            env=dict(
                os.environ, HERMES_HOME=str(root), PYTHONPATH=str(_repo_root())
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Long enough to have booted and be waiting, short enough that
            # the evidence costs seconds rather than a minute.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and proc.poll() is None:
                if marker.exists():
                    break
                time.sleep(0.05)
            assert not marker.exists(), "startup ran while the barrier was held"
            assert proc.poll() is None, "startup exited instead of waiting"

            path.unlink()
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        assert proc.returncode == 0, f"{out}\n{err}"
        assert marker.exists()


# ---------------------------------------------------------------------------
# The updater side
# ---------------------------------------------------------------------------


def _quiesce(root, **kw):
    collected: list = []

    def _recollect():
        collected.append(pi.startup_barrier_reason("gateway"))
        return UpdatePlan()

    kw.setdefault("recollect", _recollect)
    report = update_quiesce.run_pre_mutation_quiesce(
        UpdatePlan(),
        stop_runtime=lambda runtime: True,
        pid_alive=lambda pid: False,
        assess_isolation=lambda plan: update_quiesce.IsolationResult(
            isolated=True, reason="test"
        ),
        persist_state=False,
        **kw,
    )
    return report, collected


class TestTheQuiesceHoldsTheBarrierAcrossTheWindow:
    @pytest.fixture(autouse=True)
    def _reset(self):
        update_quiesce.reset_mutation_authorization()
        yield
        update_quiesce.reset_mutation_authorization()

    def test_the_barrier_is_held_before_the_final_inventory(self, root):
        """Ordering is the whole point: inventory under the barrier, or the
        TOCTOU is merely narrower."""
        _report, reasons_seen_at_recollect = _quiesce(root)
        assert reasons_seen_at_recollect, "the gate never re-collected"
        assert all(reasons_seen_at_recollect), (
            "the final inventory ran while startups were still allowed"
        )
        assert (root / pi.STARTUP_BARRIER_FILENAME).exists()

    def test_an_unacquirable_barrier_aborts_before_anything_is_touched(
        self, root, foreign_owner
    ):
        stopped: list = []
        pid, create = foreign_owner  # someone else already owns the mutation
        _write_barrier(root, owner_pid=pid, owner_create=create)
        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_quiesce.run_pre_mutation_quiesce(
                UpdatePlan(),
                stop_runtime=lambda runtime: stopped.append(runtime) or True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda plan: update_quiesce.IsolationResult(
                    isolated=True, reason="test"
                ),
                persist_state=False,
                recollect=lambda: UpdatePlan(),
            )
        assert "barrier" in str(excinfo.value).lower()
        assert stopped == []
        assert update_quiesce.authorized_report() is None

    def test_an_abort_releases_the_barrier_it_took(self, root):
        """Nothing was mutated, so nothing may stay blocked."""
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                UpdatePlan(),
                stop_runtime=lambda runtime: True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda plan: update_quiesce.IsolationResult(
                    isolated=False, reason="updater shares the fleet cgroup"
                ),
                persist_state=False,
                recollect=lambda: UpdatePlan(),
            )
        assert pi.startup_barrier_reason("gateway") == ""
        assert not (root / pi.STARTUP_BARRIER_FILENAME).exists()

    def test_a_successful_quiesce_keeps_holding_it(self, root):
        """Release belongs to the command boundary, after relaunch cleanup."""
        _quiesce(root)
        assert pi.startup_barrier_reason("gateway")
