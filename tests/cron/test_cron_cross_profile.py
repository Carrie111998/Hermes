"""Cross-profile cron access.

Cron stores are per-profile (#4707), which is right for isolation and wrong for
repair: a profile handed someone else's broken automation to fix can install
the fix but cannot fire the job to prove it worked, because the job genuinely
does not exist in its own store. The work then comes back unverified, which
defeats the point of routing it there at all.

These tests pin the narrow opening: another profile's schedule may be inspected
and fired, and nothing else.
"""

import json

import pytest


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """A root profile with a job, and a `dev` profile running the tool."""
    root = tmp_path / ".hermes"
    (root / "cron" / "output").mkdir(parents=True)
    (root / "scripts").mkdir()

    dev = root / "profiles" / "dev"
    (dev / "cron" / "output").mkdir(parents=True)
    (dev / "scripts").mkdir()

    # The caller is the dev profile.
    monkeypatch.setenv("HERMES_HOME", str(dev))
    monkeypatch.setenv("HERMES_PROFILE", "dev")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", dev)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", dev / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", dev / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", dev / "cron" / "output")

    # A job that exists ONLY in the root profile's store — exactly the shape of
    # the job dev gets asked to verify and cannot see.
    with jobs_mod.use_cron_store(root):
        (root / "scripts" / "sums.py").write_text("print('sum is 42')\n")
        job = jobs_mod.create_job(
            prompt="",
            schedule="0 9 1 * *",
            name="Monthly random sum example",
            script="sums.py",
            no_agent=True,
        )

    return {"root": root, "dev": dev, "job_id": job["id"]}


def test_own_store_does_not_show_the_other_profiles_job(two_profiles):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="list"))
    assert result["success"] is True
    assert result["jobs"] == []  # The isolation still holds by default.


def test_list_can_see_another_profiles_jobs(two_profiles):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="list", profile="default"))
    assert result["success"] is True
    assert [j["job_id"] for j in result["jobs"]] == [two_profiles["job_id"]]


def test_run_fires_the_other_profiles_job(two_profiles):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="run", job_id=two_profiles["job_id"], profile="default")
    )
    assert result["success"] is True
    # Inline, not a background handle: a detached run would not inherit the
    # redirection and would look for this job in the caller's own store. Only
    # the background path labels itself, so the absence of the label is the
    # evidence — as is having a real outcome instead of a handle.
    assert result["job"].get("execution_mode") != "background"
    assert result["job"]["executed"] is True
    assert result["job"]["execution_success"] is True


def test_override_is_unwound_after_the_call(two_profiles):
    from hermes_constants import get_hermes_home
    from tools.cronjob_tools import cronjob

    before = get_hermes_home()
    cronjob(action="list", profile="default")
    assert get_hermes_home() == before


def test_mutating_actions_are_refused_across_profiles(two_profiles):
    from tools.cronjob_tools import cronjob

    for action, kwargs in (
        ("create", {"schedule": "every 1h", "prompt": "hi"}),
        ("remove", {"job_id": two_profiles["job_id"]}),
        ("update", {"job_id": two_profiles["job_id"], "name": "renamed"}),
        ("pause", {"job_id": two_profiles["job_id"]}),
    ):
        result = json.loads(cronjob(action=action, profile="default", **kwargs))
        assert result["success"] is False, action
        assert "across profiles" in result["error"], action

    # And the other profile's job is untouched.
    listing = json.loads(cronjob(action="list", profile="default"))
    assert listing["jobs"][0]["name"] == "Monthly random sum example"


def test_unknown_profile_is_refused(two_profiles):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="list", profile="nope"))
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.parametrize("name", ["../root", "a/b", "..", "dev\\x"])
def test_profile_name_must_be_a_bare_name(two_profiles, name):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="list", profile=name))
    assert result["success"] is False
    assert "not a valid profile name" in result["error"]


def test_naming_your_own_profile_is_a_no_op(two_profiles):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="list", profile="dev"))
    assert result["success"] is True
    assert result["jobs"] == []
