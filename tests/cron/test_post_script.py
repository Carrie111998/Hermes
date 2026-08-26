"""Coverage for the per-job ``post_script`` hook.

``post_script`` runs after the run has been recorded (``finish_execution``),
on both the success and the failure path, so a job can declare its outcome
to an external tracker. It shares ``_run_job_script``'s containment rules:
the script must live inside HERMES_HOME/scripts/, and its environment goes
through ``build_subprocess_env``.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def scripts_dir(tmp_path, monkeypatch):
    """Point HERMES_HOME at tmp_path and return its scripts/ directory."""
    import cron.scheduler as scheduler

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    return d


def _write_marker_script(scripts_dir: Path, name: str = "post.py") -> Path:
    """A post_script that records the env Hermes handed it."""
    marker = scripts_dir.parent / "marker.txt"
    script = scripts_dir / name
    script.write_text(
        "import os\n"
        f"open({str(marker)!r}, 'w').write(\n"
        "    os.environ.get('HERMES_JOB_ID', '') + '|'\n"
        "    + os.environ.get('HERMES_RUN_ID', '')\n"
        ")\n",
        encoding="utf-8",
    )
    return marker


class TestExecution:
    def test_runs_with_job_and_run_id_in_env(self, scripts_dir):
        import cron.scheduler as scheduler

        marker = _write_marker_script(scripts_dir)
        scheduler._run_post_script(
            {"id": "job-42", "post_script": "post.py"}, "exec-7"
        )
        assert marker.read_text() == "job-42|exec-7"

    def test_absolute_path_inside_scripts_dir_allowed(self, scripts_dir):
        import cron.scheduler as scheduler

        marker = _write_marker_script(scripts_dir)
        scheduler._run_post_script(
            {"id": "job-42", "post_script": str(scripts_dir / "post.py")},
            "exec-7",
        )
        assert marker.read_text() == "job-42|exec-7"

    @pytest.mark.skipif(
        not Path("/bin/bash").is_file(), reason="bash not available"
    )
    def test_bash_script_runs_under_bash(self, scripts_dir):
        import cron.scheduler as scheduler

        marker = scripts_dir.parent / "marker.txt"
        script = scripts_dir / "post.sh"
        script.write_text(
            f'printf "%s|%s" "$HERMES_JOB_ID" "$HERMES_RUN_ID" > {str(marker)!r}\n',
            encoding="utf-8",
        )
        scheduler._run_post_script(
            {"id": "job-42", "post_script": "post.sh"}, "exec-7"
        )
        assert marker.read_text() == "job-42|exec-7"

    def test_no_post_script_is_a_noop(self, scripts_dir):
        import cron.scheduler as scheduler

        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script({"id": "job-42"}, "exec-7")
            scheduler._run_post_script(
                {"id": "job-42", "post_script": ""}, "exec-7"
            )
        mock_run.assert_not_called()


class TestNeverRaises:
    """The run is already recorded by the time this hook fires — a broken
    post_script must never propagate out of it."""

    def test_missing_script_logged_not_raised(self, scripts_dir, caplog):
        import cron.scheduler as scheduler

        scheduler._run_post_script(
            {"id": "job-42", "post_script": "nope.py"}, "exec-7"
        )
        assert "not found" in caplog.text

    def test_nonzero_exit_logged_not_raised(self, scripts_dir, caplog):
        import cron.scheduler as scheduler

        (scripts_dir / "post.py").write_text(
            "import sys; sys.stderr.write('boom'); sys.exit(3)\n",
            encoding="utf-8",
        )
        scheduler._run_post_script(
            {"id": "job-42", "post_script": "post.py"}, "exec-7"
        )
        assert "rc=3" in caplog.text

    def test_subprocess_failure_logged_not_raised(self, scripts_dir, caplog):
        import cron.scheduler as scheduler

        (scripts_dir / "post.py").write_text("pass\n", encoding="utf-8")
        with patch(
            "subprocess.run", side_effect=OSError("no exec for you")
        ):
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "post.py"}, "exec-7"
            )
        assert "no exec for you" in caplog.text


class TestContainment:
    """Same guarantees as _run_job_script: nothing outside scripts/ runs."""

    def test_traversal_blocked(self, scripts_dir, tmp_path, caplog):
        import cron.scheduler as scheduler

        outside = tmp_path / "outside.py"
        outside.write_text("open('/tmp/should-not-exist-hermes','w')\n", encoding="utf-8")
        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "../outside.py"}, "exec-7"
            )
        mock_run.assert_not_called()
        assert "outside the scripts directory" in caplog.text

    def test_absolute_path_outside_blocked(self, scripts_dir, tmp_path, caplog):
        import cron.scheduler as scheduler

        outside = tmp_path / "outside.py"
        outside.write_text("pass\n", encoding="utf-8")
        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script(
                {"id": "job-42", "post_script": str(outside)}, "exec-7"
            )
        mock_run.assert_not_called()
        assert "outside the scripts directory" in caplog.text

    @pytest.mark.skipif(
        not hasattr(Path, "symlink_to"), reason="symlinks unsupported"
    )
    def test_symlink_escape_blocked(self, scripts_dir, tmp_path, caplog):
        import cron.scheduler as scheduler

        outside = tmp_path / "outside.py"
        outside.write_text("pass\n", encoding="utf-8")
        link = scripts_dir / "link.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted")
        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "link.py"}, "exec-7"
            )
        mock_run.assert_not_called()
        assert "outside the scripts directory" in caplog.text

    def test_nul_byte_rejected(self, scripts_dir, caplog):
        import cron.scheduler as scheduler

        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "post\x00.py"}, "exec-7"
            )
        mock_run.assert_not_called()
        assert "NUL byte" in caplog.text

    def test_directory_is_not_runnable(self, scripts_dir, caplog):
        import cron.scheduler as scheduler

        (scripts_dir / "adir.py").mkdir()
        with patch("subprocess.run") as mock_run:
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "adir.py"}, "exec-7"
            )
        mock_run.assert_not_called()
        assert "not found" in caplog.text


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so jobs/scripts don't leak between tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


class TestJobPlumbing:
    def test_create_job_stores_post_script(self, hermes_env):
        from cron.jobs import create_job, get_job

        (hermes_env / "scripts" / "post.sh").write_text("echo hi\n", encoding="utf-8")
        job = create_job(
            prompt="do the thing",
            schedule="every 5m",
            post_script="post.sh",
            deliver="local",
        )
        assert get_job(job["id"])["post_script"] == "post.sh"

    def test_create_job_without_post_script_stores_none(self, hermes_env):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="p", schedule="every 5m", deliver="local")
        assert get_job(job["id"])["post_script"] is None

    def test_update_job_sets_and_clears_post_script(self, hermes_env):
        from cron.jobs import create_job, get_job, update_job

        (hermes_env / "scripts" / "post.sh").write_text("echo hi\n", encoding="utf-8")
        job = create_job(prompt="p", schedule="every 5m", deliver="local")

        update_job(job["id"], {"post_script": "  post.sh  "})
        assert get_job(job["id"])["post_script"] == "post.sh"

        # Empty string clears, same contract as the monitor fields.
        update_job(job["id"], {"post_script": ""})
        assert get_job(job["id"])["post_script"] is None


class TestToolBoundaryValidation:
    """The tool boundary is stricter than the scheduler: absolute paths and
    ~ expansion are rejected outright so prompt injection cannot name one."""

    def test_absolute_path_rejected(self, hermes_env):
        from tools.cronjob_tools import _validate_cron_script_path

        assert _validate_cron_script_path("/etc/evil.sh") is not None

    def test_traversal_rejected(self, hermes_env):
        from tools.cronjob_tools import _validate_cron_script_path

        assert _validate_cron_script_path("../../evil.sh") is not None

    def test_plain_relative_name_accepted(self, hermes_env):
        from tools.cronjob_tools import _validate_cron_script_path

        assert _validate_cron_script_path("post.sh") is None


class TestCallSites:
    """run_one_job must fire the hook on both terminal paths — and only
    those. A run whose outcome belongs to another worker (fire-claim taken
    over, gateway shutdown) must not run it.

    ``run_job`` returns ``(success, output, final_response, error)``; the
    jobs here carry no ``fire_claim`` so the owner-fencing branches stay
    inert and the genuine terminal path is what gets exercised.
    """

    def _job(self):
        return {
            "id": "job-ps",
            "name": "post script",
            "prompt": "p",
            "post_script": "post.py",
        }

    def _patches(self, run_side_effect):
        return (
            patch("cron.scheduler.claim_dispatch", return_value=True),
            patch("agent.secret_scope.set_secret_scope", return_value=None),
            patch("agent.secret_scope.build_profile_secret_scope", return_value=None),
            patch("agent.secret_scope.reset_secret_scope"),
            patch("cron.scheduler.run_job", side_effect=run_side_effect),
            patch("cron.scheduler._deliver_result", return_value=None),
        )

    def test_success_path_runs_hook(self):
        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(
            lambda *a, **kw: (True, "out", "final answer", None)
        )
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark, \
             patch("cron.scheduler.finish_execution") as mock_finish, \
             patch("cron.scheduler._run_post_script") as mock_hook:
            assert sched.run_one_job(self._job()) is True

        # Guard against a harness that silently lands on a fenced path:
        # this must be the real success terminal write.
        assert mock_mark.call_args.args[:3] == ("job-ps", True, None)
        assert mock_finish.call_args.kwargs["success"] is True
        mock_hook.assert_called_once()
        assert mock_hook.call_args.args[0]["id"] == "job-ps"

    def test_agent_failure_path_runs_hook(self):
        """A failed run is still a recorded run — the hook fires, which is
        precisely when an external tracker most needs to hear about it."""
        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(
            lambda *a, **kw: (False, "out", "", "model exploded")
        )
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark, \
             patch("cron.scheduler.finish_execution") as mock_finish, \
             patch("cron.scheduler._run_post_script") as mock_hook:
            sched.run_one_job(self._job())

        assert mock_mark.call_args.args[1] is False
        assert mock_finish.call_args.kwargs["success"] is False
        mock_hook.assert_called_once()

    def test_monitor_suppressed_tick_runs_hook(self):
        """A monitor job's no-change tick suppresses the AGENT and skips
        delivery, but the tick itself is a recorded, successful run — so the
        hook fires."""
        import cron.scheduler as sched

        from cron.scheduler import SILENT_MARKER

        p1, p2, p3, p4, p5, p6 = self._patches(
            lambda *a, **kw: (True, "# no_change", SILENT_MARKER, None)
        )
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True), \
             patch("cron.scheduler.finish_execution") as mock_finish, \
             patch("cron.scheduler._run_post_script") as mock_hook:
            sched.run_one_job(self._job())

        assert mock_finish.call_args.kwargs["success"] is True
        mock_hook.assert_called_once()

    def test_exception_path_runs_hook(self):
        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(RuntimeError("boom"))
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark, \
             patch("cron.scheduler.finish_execution") as mock_finish, \
             patch("cron.scheduler._run_post_script") as mock_hook:
            assert sched.run_one_job(self._job()) is False

        assert mock_mark.call_args.args[:3] == ("job-ps", False, "boom")
        assert mock_finish.call_args.kwargs["success"] is False
        mock_hook.assert_called_once()

    def test_gateway_shutdown_skips_hook(self):
        """A CancelledError means the gateway is tearing down: the hook's
        subprocess would hold shutdown open for its whole timeout, and this
        run's outcome is not final anyway."""
        import asyncio

        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(asyncio.CancelledError())
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True), \
             patch("cron.scheduler.finish_execution"), \
             patch("cron.scheduler._run_post_script") as mock_hook:
            with pytest.raises(asyncio.CancelledError):
                sched.run_one_job(self._job())

        mock_hook.assert_not_called()

    def test_fire_claim_taken_over_skips_hook(self):
        """Owner fencing: when this worker's fire claim is taken over while
        the run is in flight, the outcome belongs to the replacement worker
        — the stale worker must not fire the hook."""
        import cron.scheduler as sched

        job = dict(self._job(), fire_claim={"by": "owner-ps"})
        # True for the pre-execution ownership check, False afterwards: the
        # claim is taken over while run_job is in flight.
        beats = iter([True])

        def _heartbeat(*a, **kw):
            return next(beats, False)

        p1, p2, p3, p4, p5, p6 = self._patches(
            lambda *a, **kw: (True, "out", "final answer", None)
        )
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.heartbeat_fire_claim", side_effect=_heartbeat), \
             patch("cron.scheduler.mark_job_run", return_value=True), \
             patch("cron.scheduler.finish_execution") as mock_finish, \
             patch("cron.scheduler._run_post_script") as mock_hook:
            sched.run_one_job(job)

        assert mock_finish.called
        assert mock_finish.call_args.kwargs["success"] is False
        mock_hook.assert_not_called()


class TestEnvironmentHygiene:
    def test_env_goes_through_build_subprocess_env(self, scripts_dir):
        """Provider credentials must not leak into the hook (SECURITY.md
        §2.3) — the env is built by build_subprocess_env, not inherited."""
        import cron.scheduler as scheduler

        (scripts_dir / "post.py").write_text("pass\n", encoding="utf-8")
        with (
            patch(
                "tools.environments.local.build_subprocess_env",
                return_value={"SAFE": "1"},
            ) as mock_env,
            patch("subprocess.run") as mock_run,
        ):
            scheduler._run_post_script(
                {"id": "job-42", "post_script": "post.py"}, "exec-7"
            )
        mock_env.assert_called_once()
        passed = mock_run.call_args.kwargs["env"]
        assert passed["SAFE"] == "1"
        assert passed["HERMES_JOB_ID"] == "job-42"
        assert passed["HERMES_RUN_ID"] == "exec-7"
