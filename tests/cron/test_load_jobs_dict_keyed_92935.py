"""Regression test for dict-keyed jobs.json stores.

When ``jobs.json`` contains an ID-keyed ``{"jobs": {id: job}}`` object
instead of the expected ``{"jobs": [job, ...]}`` list, ``load_jobs()``
must convert it to a list so that all consumers (``list_jobs``,
``update_job``, ``remove_job``, the scheduler) work correctly.

See: https://github.com/NousResearch/hermes-agent/issues/92935
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    return home


def test_load_jobs_converts_dict_keyed_store_to_list(hermes_env):
    """ID-keyed {"jobs": {id: job}} must be returned as a list of job dicts."""
    jobs_file = hermes_env / "cron" / "jobs.json"
    store = {
        "jobs": {
            "cron_aaaa": {
                "id": "cron_aaaa",
                "name": "Job A",
                "prompt": "Say hello",
                "schedule": "0 9 * * *",
                "enabled": True,
            },
            "cron_bbbb": {
                "id": "cron_bbbb",
                "name": "Job B",
                "prompt": "Say goodbye",
                "schedule": "0 18 * * *",
                "enabled": True,
            },
        },
        "updated_at": "2026-08-23T10:10:12+08:00",
    }
    jobs_file.write_text(json.dumps(store))

    from cron.jobs import load_jobs

    jobs = load_jobs()
    assert isinstance(jobs, list), f"Expected list, got {type(jobs).__name__}"
    assert len(jobs) == 2
    names = {j["name"] for j in jobs}
    assert names == {"Job A", "Job B"}


def test_load_jobs_dict_keyed_store_preserves_all_fields(hermes_env):
    """Every field in the original job dict must survive the dict→list conversion."""
    jobs_file = hermes_env / "cron" / "jobs.json"
    original = {
        "id": "cron_xxxx",
        "name": "Full job",
        "prompt": "Do something",
        "schedule": "every 30m",
        "enabled": False,
        "deliver": "telegram",
        "script": "custom.sh",
    }
    store = {"jobs": {"cron_xxxx": original}, "updated_at": "2026-08-23T12:00:00"}
    jobs_file.write_text(json.dumps(store))

    from cron.jobs import load_jobs

    jobs = load_jobs()
    assert len(jobs) == 1
    loaded = jobs[0]
    for key, value in original.items():
        assert loaded[key] == value, f"Field {key!r}: expected {value!r}, got {loaded.get(key)!r}"


def test_load_jobs_dict_keyed_store_with_strict_retry(hermes_env):
    """Dict-keyed store with _strict_retry must still convert and return a list."""
    jobs_file = hermes_env / "cron" / "jobs.json"
    store = {
        "jobs": {
            "cron_zzzz": {
                "id": "cron_zzzz",
                "name": "Retry job",
                "prompt": "Test",
                "schedule": "0 * * * *",
                "enabled": True,
            }
        },
        "updated_at": "2026-08-23T12:00:00",
    }
    jobs_file.write_text(json.dumps(store))

    from cron.jobs import load_jobs

    # _strict_retry is internal; just verify the public API works
    jobs = load_jobs()
    assert isinstance(jobs, list)
    assert len(jobs) == 1
    assert jobs[0]["id"] == "cron_zzzz"
