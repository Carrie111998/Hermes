"""Lost-update defense for the cron store (t_b793ddfc).

All tests run against a TEMP cron store via ``jobs.use_cron_store(tmp_path)``;
the live profile store is never touched.
"""

import json
import logging
import os

import pytest

from cron import jobs as jobs_mod


def _job(jid, enabled=True):
    return {"id": jid, "name": jid, "enabled": enabled, "schedule": "1h", "prompt": "x"}


@pytest.fixture
def store(tmp_path):
    """Yield a helper bound to an isolated temp cron store."""
    home = tmp_path / "home"

    class _Store:
        home = None
        cron_dir = home

        @staticmethod
        def seed(job_list):
            with jobs_mod.use_cron_store(home):
                jobs_mod.save_jobs(list(job_list))

        @staticmethod
        def read():
            with jobs_mod.use_cron_store(home):
                return jobs_mod.load_jobs()

        @staticmethod
        def save(job_list, **kw):
            with jobs_mod.use_cron_store(home):
                return jobs_mod.save_jobs(list(job_list), **kw)

        @staticmethod
        def journal_path():
            return home / "cron" / jobs_mod._REMOVALS_JOURNAL_NAME

        @classmethod
        def journal_lines(cls):
            p = cls.journal_path()
            if not p.exists():
                return []
            return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]

    _Store.home = home
    return _Store


def _ids(job_list):
    return sorted(j["id"] for j in job_list)


def test_concurrent_add_survives(store):
    """A stale writer that never saw C must not delete C."""
    a, b, c = _job("A"), _job("B"), _job("C")
    store.seed([a, b])
    # concurrent writer adds C
    store.save([a, b, c])

    # stale writer only knows about A and B
    dropped = store.save([a, b], loaded_ids={"A", "B"})

    assert _ids(store.read()) == ["A", "B", "C"]
    assert dropped == 0
    assert not store.journal_path().exists()


def test_silent_drop_journaled(store):
    a, b = _job("A"), _job("B", enabled=True)
    store.seed([a, b])

    dropped = store.save([a], loaded_ids={"A", "B"}, removed_ids=set())

    assert _ids(store.read()) == ["A"]
    assert dropped == 1
    lines = store.journal_lines()
    assert len(lines) == 1
    assert lines[0]["id"] == "B"
    assert lines[0]["pid"] == os.getpid()
    assert lines[0]["enabled"] is True


def test_explicit_removal_clean(store):
    a, b = _job("A"), _job("B")
    store.seed([a, b])

    dropped = store.save([a], loaded_ids={"A", "B"}, removed_ids={"B"})

    assert _ids(store.read()) == ["A"]
    assert dropped == 0
    assert not store.journal_path().exists()


def test_mass_drop_errors(store, caplog):
    a, b, c = _job("A"), _job("B"), _job("C")
    store.seed([a, b, c])

    with caplog.at_level(logging.ERROR, logger=jobs_mod.logger.name):
        dropped = store.save([a], loaded_ids={"A", "B", "C"}, removed_ids=set())

    assert dropped == 2
    assert _ids(store.read()) == ["A"]
    lines = store.journal_lines()
    assert sorted(ln["id"] for ln in lines) == ["B", "C"]
    assert any("Lost-update guard" in r.message for r in caplog.records
               if r.levelno >= logging.ERROR)


def test_incoming_wins_on_values(store):
    store.seed([_job("A", enabled=False)])

    dropped = store.save([_job("A", enabled=True)], loaded_ids={"A"})

    result = store.read()
    assert len(result) == 1
    assert result[0]["enabled"] is True
    assert dropped == 0
    assert not store.journal_path().exists()


def test_stale_snapshot_cannot_regress_completion_metadata(store):
    """C2 (t_95fbd07c): a stale writer's older last_run_at must not erase a
    fresher completion's metadata on a shared job. Structural fields (e.g.
    enabled) still follow the caller's intent."""
    fresh = _job("A")
    fresh["last_run_at"] = "2026-08-03T13:31:13.020842+01:00"
    fresh["last_status"] = "ok"
    fresh["last_error"] = None
    fresh["next_run_at"] = "2026-08-03T14:31:00+01:00"
    store.seed([fresh])

    # A stale snapshot writer that loaded A earlier (last_run_at from 08:57)
    # and now disables the job must not regress last_run_at back to 08:57.
    stale = _job("A", enabled=False)
    stale["last_run_at"] = "2026-08-03T08:57:14.473529+01:00"
    stale["last_status"] = "ok"

    dropped = store.save([stale], loaded_ids={"A"})

    result = store.read()
    assert len(result) == 1
    assert result[0]["enabled"] is False  # structural intent honored
    assert result[0]["last_run_at"] == "2026-08-03T13:31:13.020842+01:00"
    assert result[0]["last_status"] == "ok"
    assert result[0]["next_run_at"] == "2026-08-03T14:31:00+01:00"
    assert dropped == 0


def test_incoming_newer_completion_wins(store):
    """C2 (t_95fbd07c): when the incoming writer is genuinely fresher, its
    completion metadata wins (normal mark_job_run path)."""
    old = _job("A")
    old["last_run_at"] = "2026-08-03T13:31:13.020842+01:00"
    store.seed([old])

    newer = _job("A")
    newer["last_run_at"] = "2026-08-03T13:35:00.123456+01:00"
    newer["last_status"] = "error"
    newer["last_error"] = "boom"

    store.save([newer], loaded_ids={"A"})

    result = store.read()
    assert len(result) == 1
    assert result[0]["last_run_at"] == "2026-08-03T13:35:00.123456+01:00"
    assert result[0]["last_status"] == "error"
    assert result[0]["last_error"] == "boom"


def test_mark_job_run_not_found_journaled(store):
    """C2 (t_95fbd07c): mark_job_run for a job absent from the store must
    journal a probe-visible skip line instead of silently dropping the
    completion write (cf46180e12ee class)."""
    with jobs_mod.use_cron_store(store.home):
        jobs_mod.mark_job_run("cf46180e12ee", True, None)

    skip_path = store.home / "cron" / jobs_mod._MARK_JOB_RUN_SKIPS_JOURNAL_NAME
    assert skip_path.exists()
    lines = [json.loads(ln) for ln in skip_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["job_id"] == "cf46180e12ee"
    assert lines[0]["success"] is True
    assert lines[0]["pid"] == os.getpid()
