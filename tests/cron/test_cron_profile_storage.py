"""Tests for cron storage path resolution under profile vs root HERMES_HOME.

Regression coverage for the per-profile JOBS_FILE behavior that was deleted
by revert #51116. The cron module computes HERMES_DIR, CRON_DIR, and JOBS_FILE
at module-import time from hermes_constants.get_hermes_home(). When
HERMES_HOME points to a profile directory (e.g. ~/.hermes/profiles/coder),
cron storage must anchor at that profile path, not the root.
"""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def reload_cron_jobs(monkeypatch, tmp_path):
    """Context manager to reload cron.jobs under a monkeypatched HERMES_HOME.

    Sets HERMES_HOME to the provided path, reloads cron.jobs to pick up the
    new paths, yields the reloaded module, then restores HERMES_HOME and
    reloads again to avoid leaking state to other tests.
    """
    import cron.jobs as cron_jobs

    original_hermes_dir = cron_jobs.HERMES_DIR
    original_cron_dir = cron_jobs.CRON_DIR
    original_jobs_file = cron_jobs.JOBS_FILE

    class _ReloadContext:
        def __init__(self):
            self.module = None

        def reload_with_home(self, home_path: Path):
            """Reload cron.jobs with HERMES_HOME set to home_path."""
            monkeypatch.setenv("HERMES_HOME", str(home_path))
            self.module = importlib.reload(cron_jobs)
            return self.module

    ctx = _ReloadContext()
    yield ctx

    monkeypatch.delenv("HERMES_HOME", raising=False)
    importlib.reload(cron_jobs)
    cron_jobs.HERMES_DIR = original_hermes_dir
    cron_jobs.CRON_DIR = original_cron_dir
    cron_jobs.JOBS_FILE = original_jobs_file


class TestCronProfileStorage:
    """Test that cron storage paths respect HERMES_HOME (profile-aware)."""

    def test_cron_storage_anchors_at_profile_when_profile_active(
        self, reload_cron_jobs, tmp_path
    ):
        """When HERMES_HOME points to a profile path, JOBS_FILE anchors there."""
        profile_home = tmp_path / "profiles" / "coder"
        profile_home.mkdir(parents=True)

        cron_jobs = reload_cron_jobs.reload_with_home(profile_home)

        expected_jobs_file = profile_home / "cron" / "jobs.json"
        assert cron_jobs.HERMES_DIR == profile_home.resolve()
        assert cron_jobs.CRON_DIR == profile_home.resolve() / "cron"
        assert cron_jobs.JOBS_FILE == expected_jobs_file

    def test_cron_storage_at_root_when_no_profile(
        self, reload_cron_jobs, tmp_path
    ):
        """When HERMES_HOME points to root (no profile), JOBS_FILE is at root."""
        root_home = tmp_path / ".hermes"
        root_home.mkdir(parents=True)

        cron_jobs = reload_cron_jobs.reload_with_home(root_home)

        expected_jobs_file = root_home / "cron" / "jobs.json"
        assert cron_jobs.HERMES_DIR == root_home.resolve()
        assert cron_jobs.CRON_DIR == root_home.resolve() / "cron"
        assert cron_jobs.JOBS_FILE == expected_jobs_file

    def test_cron_storage_profile_path_differs_from_root(
        self, reload_cron_jobs, tmp_path
    ):
        """JOBS_FILE under a profile must differ from the root JOBS_FILE."""
        root_home = tmp_path / ".hermes"
        root_home.mkdir(parents=True)
        profile_home = tmp_path / ".hermes" / "profiles" / "coder"
        profile_home.mkdir(parents=True)

        cron_jobs = reload_cron_jobs.reload_with_home(root_home)
        root_jobs_file = cron_jobs.JOBS_FILE

        cron_jobs = reload_cron_jobs.reload_with_home(profile_home)
        profile_jobs_file = cron_jobs.JOBS_FILE

        assert profile_jobs_file != root_jobs_file
        assert "profiles/coder/cron/jobs.json" in str(profile_jobs_file)
        assert "profiles" not in str(root_jobs_file)
