"""The ``fire_claim`` TTL is a clock, not a liveness check (2026-08-24).

``claim_job_for_fire(job_id, claim_ttl_seconds=300)`` honours an existing
``fire_claim`` only while ``0 <= age < claim_ttl_seconds``. Past 300s the claim
is treated as stale and overwritten, on the documented theory that "a machine
that crashed after claiming but before completing doesn't wedge the job
forever".

That anti-wedge property is correct and these tests preserve it. The defect is
that **a live run past 5 minutes is indistinguishable from a crashed one** to a
pure age check, while real recurring runs on this box last 20-40 minutes.

OBSERVED (event bus + profiles/main/cron/executions.db, read-only):
job ``b74186b2eaa5`` (jobflow-matcher, ``0 */6 * * *``) was claimed and started
at 2026-08-24T02:54:58Z by pid 48908 (execution ``8a77fa81``, source=direct).
Its execution row was still non-terminal when a second fire was claimed at
2026-08-24T03:25:09Z by pid 51448 (execution ``ef782174``, also source=direct,
also ``caller=hermes_cli:cron_run``). Gap between the two claim stamps:
1810.63s. Both fires ran.

FIXED by the layered admission gate — see
``tests/cron/test_fire_claim_liveness_gate.py`` for the gate's own behaviour.
This file is the DESIGN EVIDENCE: it records the incident's real numbers and
the measurements that ruled the alternatives in or out, so a later change
cannot quietly re-adopt a rejected option. Chiefly: raising or deriving the TTL
was rejected here, on arithmetic, before the gate was written.

Nothing here touches the live cron store — every test runs against a temp
HERMES_HOME and a temp execution ledger.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.cron._fire_claim_helpers import live_foreign_run


# The two claim stamps from the incident, to the microsecond, and the gap
# between them. Every TTL proposal below is judged against this one number.
INCIDENT_FIRST_CLAIM = datetime(2026, 8, 24, 2, 54, 58, 533257, tzinfo=timezone.utc)
INCIDENT_SECOND_CLAIM = datetime(2026, 8, 24, 3, 25, 9, 160865, tzinfo=timezone.utc)
INCIDENT_GAP_SECONDS = (INCIDENT_SECOND_CLAIM - INCIDENT_FIRST_CLAIM).total_seconds()


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json never touches the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _point_ledger(monkeypatch, tmp_path):
    """Point the durable execution ledger at a temp file.

    ``EXECUTIONS_FILE`` is a module-level constant resolved from
    ``get_hermes_home()`` at IMPORT time, so setting HERMES_HOME in a fixture is
    not enough once the module is already loaded — it must be patched directly.
    """
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _freeze_now(monkeypatch, moment):
    """Pin ``cron.jobs._hermes_now`` so claim ages are exact, not wall-clock."""
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "_hermes_now", lambda: moment)


# ---------------------------------------------------------------------------
# 1. The defect itself
# ---------------------------------------------------------------------------


def test_the_ledger_row_is_the_only_thing_separating_refuse_from_admit(
    temp_home, tmp_path, monkeypatch
):
    """The incident at its measured timings, both arms, one test.

    Same job, same 1810.63s gap, same stale claim. The ONLY difference between
    the two arms is whether a non-terminal execution row with a live owner
    exists. That isolates the fix: the TTL layer still reads this claim as
    stale in both arms (arm 2 proves it, by admitting), so layer 2 is doing the
    refusing in arm 1 and nothing else changed.
    """
    import cron.jobs as jobs

    executions = _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="jobflow-matcher")
    jid = job["id"]

    # Fire A wins the claim, then opens the row — the order both production
    # call sites use (claim first, create_execution second).
    assert jobs.claim_job_for_fire(jid) is True

    # --- arm 1: a live run is open -> refused (this used to be admitted) ---
    with live_foreign_run(ledger, jid):
        _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
        still_open = [
            r for r in executions.list_executions(job_id=jid)
            if r["status"] in ("claimed", "running")
        ]
        assert len(still_open) == 1, "run A must be non-terminal for this to bite"
        assert jobs.claim_job_for_fire(jid) is False

    # --- arm 2: identical claim, identical clock, owner now gone -> admitted ---
    assert jobs.claim_job_for_fire(jid) is True


def test_POSITIVE_CONTROL_a_ttl_longer_than_the_gap_does_block(
    temp_home, tmp_path, monkeypatch
):
    """Proves the test above fails for the reason claimed, not a broken fixture.

    Identical setup, identical 1810.63s gap, only the TTL changed. If the claim
    were not being stamped, or the job not found, or the frozen clock not
    taking, this would ALSO return True and the characterization test would be
    vacuous. It returns False, so the TTL boundary is genuinely what admits the
    duplicate.
    """
    import cron.jobs as jobs

    _point_ledger(monkeypatch, tmp_path)

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="jobflow-matcher")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid, claim_ttl_seconds=1811) is False
    # ...and one second under the gap flips it back. The boundary is exact.
    assert jobs.claim_job_for_fire(jid, claim_ttl_seconds=1810) is True


def test_the_TTL_LAYER_alone_still_cannot_tell_a_live_owner_from_a_dead_one(
    temp_home, monkeypatch
):
    """The epistemic core, and why layer 2 had to exist at all.

    With no ledger row there is nothing but the clock, and the clock re-admits
    regardless of whether the holder is alive. The ``fire_claim`` itself cannot
    close this: it records a ``by``, but that field is documented as
    attribution and "NOT correctness", and nothing resolves it back to a
    process. Hence a second, durable source of truth rather than a bigger
    number.
    """
    import cron.jobs as jobs

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True
    stamped = jobs.get_job(jid)["fire_claim"]
    assert stamped is not None

    # The claim records WHO holds it — but "by" is documented as attribution
    # only ("NOT correctness"), and nothing on the admission path resolves it
    # back to a process, so it cannot answer "is that holder still running?".
    assert set(stamped) == {"at", "by"}

    _freeze_now(monkeypatch, INCIDENT_SECOND_CLAIM)
    assert jobs.claim_job_for_fire(jid) is True


def test_anti_wedge_recovery_must_survive_any_fix(temp_home, monkeypatch):
    """The property the TTL exists for. A fix must not regress this.

    Owner crashed, claim never cleared, no live run anywhere: the next fire has
    to be admitted or the job is wedged forever.
    """
    import cron.jobs as jobs

    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM)
    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    jid = job["id"]
    assert jobs.claim_job_for_fire(jid) is True

    # ...owner dies here, leaving no execution row and no live process...
    _freeze_now(monkeypatch, INCIDENT_FIRST_CLAIM + timedelta(days=1))
    assert jobs.claim_job_for_fire(jid) is True


# ---------------------------------------------------------------------------
# 2. Option A — derive the TTL from the timeout budget
# ---------------------------------------------------------------------------


def test_recurring_jobs_have_no_per_job_timeout_budget_to_derive_from(temp_home):
    """"The job's own timeout budget" does not exist as a field.

    A cron job record carries no timeout of any kind. The only budget in the
    system is the process-wide ``HERMES_CRON_TIMEOUT`` env var, so "derive from
    the job's own budget" can only mean "derive from that global env var" —
    which is what the one-shot helper already does.
    """
    import cron.jobs as jobs

    job = jobs.create_job(prompt="x", schedule="every 6h", name="m")
    assert not [k for k in job if "timeout" in k.lower()]


def test_derived_ttl_at_DEFAULT_config_would_not_have_stopped_the_incident(
    monkeypatch,
):
    """Option A fails this incident by 10.6 seconds at stock configuration.

    With ``HERMES_CRON_TIMEOUT`` unset the helper resolves 600s * 3 headroom =
    1800s, floored at ONESHOT_RUN_CLAIM_TTL_SECONDS (also 1800). The observed
    gap was 1810.63s. The second fire would still have been admitted.
    """
    import cron.jobs as jobs

    monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)
    derived = jobs._oneshot_run_claim_ttl_seconds()

    assert derived == 1800.0
    assert INCIDENT_GAP_SECONDS > derived
    assert round(INCIDENT_GAP_SECONDS - derived, 2) == 10.63


def test_derived_ttl_only_covers_the_incident_because_this_host_sets_1800(
    monkeypatch,
):
    """Option A's adequacy is a property of local .env, not of the design.

    This box sets ``HERMES_CRON_TIMEOUT=1800`` (~/.hermes/.env:54 and
    profiles/main/.env:144), which derives 5400s and would have held. Unset it
    — a fresh checkout, a process that never loaded the dotenv — and the same
    code silently drops to 1800s and re-admits.
    """
    import cron.jobs as jobs

    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "1800")
    assert jobs._oneshot_run_claim_ttl_seconds() == 5400.0
    assert INCIDENT_GAP_SECONDS < 5400.0

    monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)
    assert jobs._oneshot_run_claim_ttl_seconds() == 1800.0
    assert INCIDENT_GAP_SECONDS > 1800.0


def test_the_derived_budget_is_an_inactivity_limit_so_no_multiple_bounds_a_run(
    monkeypatch,
):
    """No finite TTL is provably safe, because the input is not a wall clock.

    ``HERMES_CRON_TIMEOUT`` is an INACTIVITY limit — the scheduler's own
    comment says a job "can run for hours if it's actively calling tools".
    A run that keeps emitting stream deltas never trips it, so a TTL derived
    from it bounds nothing: pick any multiplier and a busy run can outlive it.
    Setting it to 0 (unlimited) makes that explicit — the helper falls back to
    a flat constant precisely because there is no bound to derive.
    """
    import cron.jobs as jobs

    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    assert jobs._oneshot_run_claim_ttl_seconds() == float(
        jobs.ONESHOT_RUN_CLAIM_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# 3. Option B — gate admission on live execution
# ---------------------------------------------------------------------------


def test_scheduler_in_flight_is_blind_across_processes_but_the_ledger_is_not(
    tmp_path, monkeypatch
):
    """Why ``_in_flight`` cannot be the source of truth, and the ledger can.

    The incident's two fires ran in two separate CLI processes (pid 48908,
    pid 51448). ``cron.scheduler._in_flight`` is a plain ``dict`` guarded by a
    ``threading.Lock`` — per-process by construction — so neither could see the
    other. This spawns a real child that registers in BOTH places and asserts
    the parent sees only one of them.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    ledger = tmp_path / "cron" / "executions.db"
    repo = Path(__file__).resolve().parents[2]

    child = textwrap.dedent(
        f"""
        import json, os, sys
        sys.path.insert(0, {str(repo)!r})
        import cron.executions as ex
        from pathlib import Path
        ex.EXECUTIONS_FILE = Path({str(ledger)!r})
        import cron.scheduler as sched

        sched._try_register_in_flight("shared-job", "shared-job")
        rec = ex.create_execution("shared-job", source="direct")
        ex.mark_execution_running(rec["id"])
        print(json.dumps({{"pid": os.getpid(),
                           "in_flight": list(sched._in_flight)}}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, text=True, cwd=str(repo), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    info = json.loads(proc.stdout.strip().splitlines()[-1])

    # The child really did register in its own process...
    assert info["in_flight"] == ["shared-job"]

    # ...and the parent cannot see it. This is the whole problem with using
    # _in_flight as an admission gate across processes.
    import cron.scheduler as sched

    assert "shared-job" not in sched._in_flight
    assert "shared-job" not in sched.get_running_job_ids()

    # The durable ledger, by contrast, carries the child's run across the
    # process boundary — pid and process start time included.
    census = executions.nonterminal_execution_census()
    rows = [r for r in census if r["job_id"] == "shared-job"]
    assert len(rows) == 1
    assert rows[0]["pid"] == info["pid"]

    # And because the child has exited, the ledger reports its owner dead — so
    # a liveness gate would ADMIT here, preserving the anti-wedge property.
    assert rows[0]["owner_liveness"] == "dead"


def test_the_ledger_reports_a_still_running_owner_as_live(tmp_path, monkeypatch):
    """The other arm: an open run whose owner is alive is reported ``live``.

    A gate reading this would refuse the second fire — the exact case the 300s
    TTL gets wrong. The row is written by THIS process, so its pid and start
    time are genuinely current.
    """
    executions = _point_ledger(monkeypatch, tmp_path)

    rec = executions.create_execution("live-job", source="direct")
    executions.mark_execution_running(rec["id"])

    rows = [
        r for r in executions.nonterminal_execution_census()
        if r["job_id"] == "live-job"
    ]
    assert len(rows) == 1
    assert rows[0]["pid"] == os.getpid()
    assert rows[0]["owner_liveness"] == "live"
    assert rows[0]["owner_liveness_evidence"]["reason"] == "process_start_time_matches"


def test_liveness_probe_rejects_a_recycled_pid(tmp_path, monkeypatch):
    """Recycled PIDs cannot fake liveness — the start time is compared too.

    Matters for an admission gate: on a box that has churned through the pid
    space, "pid 48908 exists" alone would refuse fires forever.
    """
    executions = _point_ledger(monkeypatch, tmp_path)

    # Our own pid, but a start time that is not ours: the impostor shape.
    assert executions._owner_is_live(os.getpid(), 1) is False
    # Our own pid with our own recorded start time: genuinely live.
    real_start = executions._process_start_time(os.getpid())
    if real_start is None:
        pytest.skip("process start time unavailable on this host")
    assert executions._owner_is_live(os.getpid(), real_start) is True


def test_liveness_probe_fails_safe_when_it_cannot_prove_death(
    tmp_path, monkeypatch
):
    """An unprovable probe answers "still owned".

    On the recovery path that means "don't rewrite state". Reused as an
    admission gate it means "refuse this fire" — a missed run, which is the
    direction cron already chose for recurring jobs ("missing one run is far
    better than firing dozens", ``advance_next_run``). Worth stating
    explicitly because the same return value carries opposite risk in the two
    callers.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    import gateway.status as status

    def _boom(_pid):
        raise OSError("probe unavailable")

    monkeypatch.setattr(status, "_pid_exists", _boom)
    assert executions._owner_is_live(999999, 1) is True


# ---------------------------------------------------------------------------
# 4. What else was (not) guarding this path
# ---------------------------------------------------------------------------


def test_min_interval_guard_is_tick_only_and_never_reaches_a_manual_fire():
    """Guard #4 exists and would have caught this — on a path this fire misses.

    ``_job_min_seconds_between_fires`` is consulted exactly once, inside
    ``_tick_admitted``. ``hermes cron run`` goes through
    ``_execute_job_now`` -> ``run_one_job`` -> ``_run_one_job_admitted``, which
    never calls it. Pinned so a future refactor that wires it in has to update
    this expectation deliberately.
    """
    source = (
        Path(__file__).resolve().parents[2] / "cron" / "scheduler.py"
    ).read_text(encoding="utf-8")
    call_sites = source.count("_job_min_seconds_between_fires(job)")
    assert call_sites == 1, (
        "Guard #4 gained or lost a call site; re-check whether the manual "
        "fire path is now covered."
    )


def test_guard3_dup_timeout_default_is_six_times_the_fire_claim_ttl():
    """Two duplicate guards on the same path disagree about "how long is a run".

    Guard #3's wedged-fire threshold defaults to 1800s; the fire claim's TTL
    defaults to 300s. Same question, same code path, 6x apart. Whatever fix
    lands should reconcile these rather than add a third number.
    """
    import cron.scheduler as sched
    import cron.jobs as jobs
    import inspect

    assert sched._DEFAULT_DUP_GUARD_TIMEOUT_S == 1800.0
    default_ttl = inspect.signature(
        jobs.claim_job_for_fire
    ).parameters["claim_ttl_seconds"].default
    assert default_ttl == 300
    assert sched._DEFAULT_DUP_GUARD_TIMEOUT_S == 6 * default_ttl
