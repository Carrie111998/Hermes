"""The layered fire-claim admission gate (2026-08-24).

``claim_job_for_fire`` used to admit purely on claim age, so a run legitimately
longer than the 300s TTL was indistinguishable from a crashed one and a second
fire was let through. The gate adds a second, evidence-based layer:

    1. fresh claim (age < TTL)                        -> refuse
    2. stale/absent claim, but the durable execution
       ledger PROVES another run of this job is alive -> refuse
    3. otherwise                                      -> admit

Layer 1 stays load-bearing: both production call sites claim BEFORE
``create_execution``, so there is a window in which a winner holds a claim but
has not yet written its ledger row. The TTL covers exactly that window and
therefore cannot go to zero.

Layer 2 refuses only on POSITIVE proof of life. "Cannot tell" admits. That is a
deliberate inversion of ``cron/executions.py:_owner_is_live``, which fails safe
to True: on the recovery path "assume still owned" is cheap and the next restart
retries, but as an admission gate it would wedge the job forever with no
recovery path — reintroducing the exact failure the TTL exists to prevent.
Positive-proof-only is never worse than the old behaviour in any case.

Design evidence for these choices lives in
``tests/cron/test_fire_claim_ttl_vs_run_duration.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.cron._fire_claim_helpers import live_foreign_run


INCIDENT_FIRST_CLAIM = datetime(2026, 8, 24, 2, 54, 58, 533257, tzinfo=timezone.utc)
INCIDENT_SECOND_CLAIM = datetime(2026, 8, 24, 3, 25, 9, 160865, tzinfo=timezone.utc)


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _point_ledger(monkeypatch, tmp_path):
    """``EXECUTIONS_FILE`` is resolved at import time — patch the attribute."""
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _freeze_now(monkeypatch, moment):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "_hermes_now", lambda: moment)


# Deliberately a day older than anything ``create_execution`` will stamp, and in
# the same local-offset format the ledger writes, so newest-first ordering is
# unambiguous no matter what the real wall clock says when the suite runs.
_OLDER_THAN_ANY_REAL_ROW = "2026-08-23T01:00:00.000000-04:00"


def _insert_row(executions, job_id, *, pid, process_started_at, status="running",
                claimed_at=_OLDER_THAN_ANY_REAL_ROW):
    """Write a ledger row with an arbitrary owner, bypassing create_execution."""
    import uuid

    with executions._lock, executions._connect() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, job_id, "direct", uuid.uuid4().hex, pid,
             process_started_at, status, claimed_at),
        )


# ---------------------------------------------------------------------------
# Layer 2 — refuse on proven life
# ---------------------------------------------------------------------------


def test_stale_claim_is_refused_while_the_prior_owner_is_provably_alive(
    temp_home, tmp_path, monkeypatch
):
    """The incident, at its measured 1810.63s gap, now blocked.

    This is the assertion that used to read ``is True``. The claim is stale by
    every clock in the system; the ledger is what refuses.
    """
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="jobflow-matcher")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    with live_foreign_run(ledger, jid):
        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        assert jobs.claim_job_for_fire(jid) is False


def test_the_refusal_does_not_disturb_the_prior_claim_or_the_schedule(
    temp_home, tmp_path, monkeypatch
):
    """A refused fire must be a pure no-op.

    ``claim_job_for_fire`` advances ``next_run_at`` when it WINS. A refusal that
    advanced it anyway would silently eat the job's next scheduled run — a
    duplicate-fire fix that causes missed fires.
    """
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    claim_before = jobs.get_job(jid)["fire_claim"]
    next_before = jobs.get_job(jid)["next_run_at"]

    with live_foreign_run(ledger, jid):
        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        assert jobs.claim_job_for_fire(jid) is False

    assert jobs.get_job(jid)["fire_claim"] == claim_before
    assert jobs.get_job(jid)["next_run_at"] == next_before


def test_a_live_run_of_a_DIFFERENT_job_does_not_block(
    temp_home, tmp_path, monkeypatch
):
    """The ledger query must be job-scoped."""
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    mine = jobs.create_job(prompt="x", schedule="every 6h", name="mine")
    other = jobs.create_job(prompt="x", schedule="every 6h", name="other")
    assert jobs.claim_job_for_fire(mine["id"]) is True

    with live_foreign_run(ledger, other["id"]):
        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        assert jobs.claim_job_for_fire(mine["id"]) is True


def test_an_own_process_row_never_blocks_and_never_wedges(
    temp_home, tmp_path, monkeypatch
):
    """A row owned by THIS process is excluded — deliberately.

    Its pid is alive by definition, and ``recover_interrupted_execution_records``
    skips own-process rows, so a stale one (a ``finish_execution`` that never
    landed) would otherwise read as a live run for the lifetime of the process
    and wedge the job permanently. In a long-lived gateway that is unbounded.
    The in-process case belongs to Guard #3, which is exact rather than
    inferred.

    This is the counterpart of the foreign-process test above: same shape, same
    open row, only the owner differs — and the verdict flips.
    """
    import cron.jobs as jobs

    executions = _point_ledger(monkeypatch, tmp_path)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    mine = executions.create_execution(jid, source="direct")
    executions.mark_execution_running(mine["id"])
    # The row really is open and really is ours.
    assert mine["pid"] == os.getpid()
    assert [r["id"] for r in executions.list_executions(job_id=jid)
            if r["status"] in ("claimed", "running")] == [mine["id"]]

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True
    assert executions.live_execution_for_job(jid) is None


def test_terminal_executions_never_block_a_fire(temp_home, tmp_path, monkeypatch):
    """A completed run owned by a live process is not an in-flight run.

    This process is genuinely alive, so only the row's STATUS distinguishes
    this from the blocking case — which is what makes it a real test.
    """
    import cron.jobs as jobs

    executions = _point_ledger(monkeypatch, tmp_path)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    for ok in (True, False):
        rec = executions.create_execution(jid, source="direct")
        executions.mark_execution_running(rec["id"])
        executions.finish_execution(rec["id"], success=ok, error=None if ok else "e")

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True


# ---------------------------------------------------------------------------
# Anti-wedge — the property the TTL existed for, now evidence-based
# ---------------------------------------------------------------------------


def test_stale_claim_is_admitted_once_the_prior_owner_is_dead(
    temp_home, tmp_path, monkeypatch
):
    """A real child writes a running row and exits. The job must stay fireable.

    Uses a genuine dead process rather than a fabricated pid so the recovery
    path is exercised against a pid that really did exist.
    """
    import cron.jobs as jobs

    executions = _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"
    repo = Path(__file__).resolve().parents[2]

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    child = textwrap.dedent(
        f"""
        import json, os, sys
        sys.path.insert(0, {str(repo)!r})
        from pathlib import Path
        import cron.executions as ex
        ex.EXECUTIONS_FILE = Path({str(ledger)!r})
        rec = ex.create_execution({jid!r}, source="direct")
        ex.mark_execution_running(rec["id"])
        print(json.dumps({{"pid": os.getpid()}}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, text=True, cwd=str(repo), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    child_pid = json.loads(proc.stdout.strip().splitlines()[-1])["pid"]

    # The row really is non-terminal and owned by the (now dead) child.
    open_rows = [
        r for r in executions.list_executions(job_id=jid)
        if r["status"] in ("claimed", "running")
    ]
    assert len(open_rows) == 1 and open_rows[0]["pid"] == child_pid

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True


def test_unprovable_liveness_admits_rather_than_wedging(
    temp_home, tmp_path, monkeypatch
):
    """"Cannot tell" must admit, or the job is unfireable with no recovery.

    Deliberately opposite to ``_owner_is_live``'s fail-safe-to-True. A probe
    that permanently fails for a given pid would otherwise refuse every future
    fire of that job forever.
    """
    import cron.jobs as jobs
    import gateway.status as status

    executions = _point_ledger(monkeypatch, tmp_path)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True
    rec = executions.create_execution(jid, source="direct")
    executions.mark_execution_running(rec["id"])

    def _boom(_pid):
        raise OSError("probe unavailable")

    monkeypatch.setattr(status, "_pid_exists", _boom)

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True


def test_a_broken_ledger_never_blocks_cron(temp_home, tmp_path, monkeypatch):
    """The gate must degrade to the old behaviour, not take the scheduler down.

    A raise here previously cost the gateway its entire scheduler (the
    2026-08-11 5h08m outage was this shape).
    """
    import cron.jobs as jobs
    import cron.executions as executions

    _point_ledger(monkeypatch, tmp_path)

    def _boom(_job_id):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(executions, "live_execution_for_job", _boom)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True


# ---------------------------------------------------------------------------
# Layer 1 stays first, and stays cheap
# ---------------------------------------------------------------------------


def test_a_fresh_claim_refuses_without_ever_touching_the_ledger(
    temp_home, tmp_path, monkeypatch
):
    """Ordering is load-bearing twice over.

    Correctness: the fresh-claim check is what closes the race between winning
    a claim and writing the ledger row, so it must run FIRST.

    Cost: a live-pid probe measured ~47ms on this host, and ``_jobs_lock`` is a
    cross-process advisory lock documented as "field updates only". The
    contended path must not pay it.
    """
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    calls = []
    real = jobs._job_has_live_execution
    monkeypatch.setattr(
        jobs, "_job_has_live_execution",
        lambda job_id: calls.append(job_id) or real(job_id),
    )

    # Same frozen instant -> claim age 0 -> refuse on layer 1 alone.
    assert jobs.claim_job_for_fire(jid) is False
    assert calls == []


def test_paused_and_missing_jobs_refuse_without_touching_the_ledger(
    temp_home, tmp_path, monkeypatch
):
    """Non-runnable jobs are rejected before any liveness work."""
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jobs.pause_job(job["id"])

    calls = []
    monkeypatch.setattr(
        jobs, "_job_has_live_execution", lambda job_id: calls.append(job_id) or False
    )

    assert jobs.claim_job_for_fire(job["id"]) is False
    assert jobs.claim_job_for_fire("nope-does-not-exist") is False
    assert calls == []


def test_liveness_probe_count_is_bounded(temp_home, tmp_path, monkeypatch):
    """A pathological pile of open rows cannot hold the cross-process lock.

    Probes are capped and short-circuit on the first proven-live owner.
    """
    import cron.jobs as jobs
    import cron.executions as executions
    import gateway.status as status

    _point_ledger(monkeypatch, tmp_path)
    cap = executions._MAX_ADMISSION_LIVENESS_PROBES

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    for i in range(cap * 3):
        _insert_row(executions, jid, pid=900000 + i, process_started_at=1)

    probes = []
    real = status._pid_exists
    monkeypatch.setattr(
        status, "_pid_exists",
        lambda pid: probes.append(pid) or real(pid),
    )

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    # Every owner is dead, so the gate admits...
    assert jobs.claim_job_for_fire(jid) is True
    # ...having probed at most the cap, not all 3x rows.
    assert 0 < len(probes) <= cap


def test_probing_stops_at_the_first_live_owner(temp_home, tmp_path, monkeypatch):
    """Short-circuit: the newest row is probed first and ends the scan.

    Ordering matters because the newest non-terminal row is the one most likely
    to be a live run, so the common blocking case costs exactly one probe
    rather than one per stale row left behind by past kills.
    """
    import cron.jobs as jobs
    import cron.executions as executions
    import gateway.status as status

    _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    # Older dead rows first; the live foreign run is stamped now, so it sorts
    # newest and must be the only row probed.
    for i in range(3):
        _insert_row(executions, jid, pid=900000 + i, process_started_at=1)

    with live_foreign_run(ledger, jid) as worker_pid:
        probes = []
        real = status._pid_exists
        monkeypatch.setattr(
            status, "_pid_exists", lambda pid: probes.append(pid) or real(pid)
        )
        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        assert jobs.claim_job_for_fire(jid) is False
        assert probes == [worker_pid]


def test_a_genuinely_separate_live_process_blocks_then_releases(
    temp_home, tmp_path, monkeypatch
):
    """The incident's real shape, end to end, with two real processes.

    Every other "live owner" case in this file is owned by ``os.getpid()``,
    which cannot distinguish "the gate reads the ledger" from "the gate
    happens to be looking at itself". Here a SEPARATE process holds the open
    row and stays alive across the parent's claim attempt — the exact
    two-``hermes cron run`` shape of 2026-08-24 — and the same claim is then
    re-attempted after that process dies.

    Refuse-while-alive and admit-once-dead in one test, because either half
    alone is satisfiable by a gate that is simply stuck in one answer.
    """
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"
    ready = tmp_path / "child.ready"
    repo = Path(__file__).resolve().parents[2]

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="jobflow-matcher")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    # The child reports its OWN pid through the ready file rather than the
    # parent using Popen.pid. On this box ``sys.executable`` is a ~256KB uv
    # trampoline stub that re-execs the real interpreter, so Popen.pid is the
    # stub and the process that actually writes the ledger row is its child.
    # Measured: Popen.pid 29096 vs os.getpid() 41112. Killing Popen.pid alone
    # would leave the true owner alive and the release half of this test would
    # fail for a reason that has nothing to do with the gate.
    child_src = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(repo)!r})
        from pathlib import Path
        import cron.executions as ex
        ex.EXECUTIONS_FILE = Path({str(ledger)!r})
        rec = ex.create_execution({jid!r}, source="direct")
        ex.mark_execution_running(rec["id"])
        Path({str(ready)!r}).write_text(str(os.getpid()))
        # Hard self-bound: the parent kills us, but a leaked child must never
        # outlive the suite if that fails.
        time.sleep(120)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_src],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    worker_pid = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if child.poll() is not None:
                _, err = child.communicate()
                pytest.fail(f"child died before opening its run: {err.decode()[-2000:]}")
            try:
                # Guard against reading the file mid-write.
                worker_pid = int(ready.read_text().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        assert worker_pid is not None, "child never opened its execution row"

        # The row is owned by ANOTHER process — the distinction this test
        # exists to make, and the one os.getpid()-owned cases cannot prove.
        open_rows = [
            r for r in _point_ledger(monkeypatch, tmp_path).list_executions(job_id=jid)
            if r["status"] in ("claimed", "running")
        ]
        assert len(open_rows) == 1
        assert open_rows[0]["pid"] == worker_pid != os.getpid()

        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        assert jobs.claim_job_for_fire(jid) is False, (
            "a live run in another process must block the duplicate fire"
        )
    finally:
        child.kill()
        try:
            child.wait(timeout=60)
        except Exception:
            pass
        if worker_pid is not None:
            try:
                import psutil

                psutil.Process(worker_pid).kill()
            except Exception:
                pass  # already gone

    # Wait for the owner to actually be gone, so the assertion below tests the
    # gate rather than racing process teardown.
    from gateway.status import _pid_exists

    gone_by = time.monotonic() + 60
    while _pid_exists(worker_pid) and time.monotonic() < gone_by:
        time.sleep(0.05)
    assert not _pid_exists(worker_pid), "worker survived; cannot test the release"

    # Same stale claim, same open row — only the owner's liveness changed.
    assert jobs.claim_job_for_fire(jid) is True, (
        "the job must become fireable again once the owner dies"
    )


# ---------------------------------------------------------------------------
# The ledger helper in isolation
# ---------------------------------------------------------------------------


def test_live_execution_for_job_returns_the_row_not_just_a_bool(
    tmp_path, monkeypatch
):
    """Callers get the row so a refusal can be explained (pid, execution id).

    Guard #5's event carries no ``prior_cron_started_event_id`` — that id only
    exists in the owning process's memory — so the pid and execution id in this
    row are the ONLY handle an operator gets on the blocking run.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    with live_foreign_run(ledger, "j") as worker_pid:
        found = executions.live_execution_for_job("j")
        assert found is not None
        assert found["pid"] == worker_pid
        assert found["owner_liveness"] == "live"
        assert found["id"] and found["claimed_at"]

    # Owner gone -> no proof of life -> None (anti-wedge).
    assert executions.live_execution_for_job("j") is None
