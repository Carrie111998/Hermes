"""Tests for cron job script injection feature.

Tests cover:
- Script field in job creation / storage / update
- Script execution and output injection into prompts
- Error handling (missing script, timeout, non-zero exit)
- Path resolution (absolute, relative to HERMES_HOME/scripts/)
"""

import json
import os
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Clear cached module-level paths
    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


class TestJobScriptField:
    """Test that the script field is stored and retrieved correctly."""

    def test_create_job_with_script(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(
            prompt="Analyze the data",
            schedule="every 30m",
            script="/path/to/monitor.py",
        )
        assert job["script"] == "/path/to/monitor.py"

        loaded = get_job(job["id"])
        assert loaded["script"] == "/path/to/monitor.py"


    def test_update_job_add_script(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="Hello", schedule="every 1h")
        assert job.get("script") is None

        updated = update_job(job["id"], {"script": "/new/script.py"})
        assert updated["script"] == "/new/script.py"


def test_cronjob_tool_rejects_stale_past_one_shot(cron_env, monkeypatch):
    from tools.cronjob_tools import cronjob

    now = datetime(2026, 3, 18, 4, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    stale = (now - timedelta(minutes=5)).isoformat()

    result = json.loads(cronjob(action="create", prompt="Too late", schedule=stale))

    assert result["success"] is False
    assert "past and cannot be scheduled" in result["error"]


class TestRunJobScript:
    """Test the _run_job_script() function."""

    def test_successful_script(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "test.py"
        script.write_text('print("hello from script")\n')

        success, output = _run_job_script(str(script))
        assert success is True
        assert output == "hello from script"

    def test_script_relative_path(self, cron_env):
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "relative.py"
        script.write_text('print("relative works")\n')

        success, output = _run_job_script("relative.py")
        assert success is True
        assert output == "relative works"

    @pytest.mark.parametrize(
        "launcher_args",
        [
            "--brand panvola --live --target 20 --pass1-time-budget-seconds 5400 "
            "--pass2-time-budget-seconds 1200 --pass2-candidate-cap 20",
            "--pass2-candidate-cap 20 --target 20 --live --brand panvola "
            "--pass2-time-budget-seconds 1200 --pass1-time-budget-seconds 5400",
        ],
    )
    def test_design_flip_outer_timeout_comes_from_v2_policy_not_launcher_argv(
        self, cron_env, monkeypatch, launcher_args,
    ):
        """The 7,500-second outer deadline is policy-owned and order-free.

        The launcher forwards user/controller arguments, so its flag order is
        not a trustworthy timeout source.  The scheduler must instead read
        the adjacent canonical policy and validate target/cap/timeboxes by
        key before it spawns the shell wrapper.
        """
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        launcher = cron_env / "scripts" / "design-status-flip-panvola-live-weekly.sh"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f"python3 design-status-flip-weekly.py {launcher_args} \"$@\"\n",
            encoding="utf-8",
        )
        # Deliberately reverse the JSON member order too: contract lookup is
        # by key, not by position in an argv string or serialized policy.
        policy_values = {
            "contract_version": 2,
            "scheduler_timeout_seconds": 7500,
            "wrapper_timeout_seconds": 6600,
            "pass1_timeout_seconds": 5400,
            "pass2_timeout_seconds": 1200,
            "pass2_candidate_cap": 20,
            "panvola_target": 20,
        }
        policy = {key: policy_values[key] for key in reversed(tuple(policy_values))}
        (cron_env / "scripts" / "design-status-flip-policy.json").write_text(
            json.dumps(policy), encoding="utf-8",
        )

        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, *, timeout):
                captured["timeout"] = timeout
                return "ok\n", ""

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProcess()

        global_timeout_calls = []
        monkeypatch.setattr(
            sched_mod,
            "_get_script_timeout",
            lambda: global_timeout_calls.append(True) or 3600,
        )
        monkeypatch.setattr(sched_mod.subprocess, "Popen", fake_popen)

        success, output = _run_job_script(launcher.name)

        assert success is True
        assert output == "ok"
        assert captured["timeout"] == 7500
        assert global_timeout_calls == []
        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["env"][sched_mod._DESIGN_STATUS_FLIP_PARENT_TREE_ENV] == "v2"

    def test_design_flip_policy_rejects_missing_target_before_spawn(self, cron_env, monkeypatch):
        """A partial v2 policy may not fall back to the generic timeout."""
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        launcher = cron_env / "scripts" / "design-status-flip-panvola-live-weekly.sh"
        launcher.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (cron_env / "scripts" / "design-status-flip-policy.json").write_text(
            json.dumps({
                "contract_version": 2,
                "scheduler_timeout_seconds": 7500,
                "wrapper_timeout_seconds": 6600,
                "pass1_timeout_seconds": 5400,
                "pass2_timeout_seconds": 1200,
                "pass2_candidate_cap": 20,
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sched_mod.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("invalid policy must not spawn launcher"),
        )

        success, output = _run_job_script(launcher.name)

        assert success is False
        assert "policy contract rejected" in output.lower()
        assert "panvola_target" in output

    @pytest.mark.parametrize(
        "launcher_name",
        [
            "design-status-flip-panvola-live-weekly.sh",
            "design-status-flip-panvola-live-weekly.cmd",
        ],
    )
    def test_design_flip_policy_timeout_is_platform_launcher_independent(
        self, cron_env, launcher_name,
    ):
        """Both shipped launchers resolve the same policy-owned deadline."""
        from cron.scheduler import _script_timeout_for_path

        launcher = cron_env / "scripts" / launcher_name
        launcher.write_text("placeholder", encoding="utf-8")
        (cron_env / "scripts" / "design-status-flip-policy.json").write_text(
            json.dumps({
                "contract_version": 2,
                "scheduler_timeout_seconds": 7500,
                "wrapper_timeout_seconds": 6600,
                "pass1_timeout_seconds": 5400,
                "pass2_timeout_seconds": 1200,
                "pass2_candidate_cap": 20,
                "panvola_target": 20,
            }),
            encoding="utf-8",
        )

        assert _script_timeout_for_path(launcher) == 7500

    def test_windows_design_flip_cmd_launcher_uses_cmd_and_policy_timeout(
        self, cron_env, monkeypatch,
    ):
        """The installed .cmd launcher must never be treated as Python."""
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        launcher = cron_env / "scripts" / "design-status-flip-panvola-live-weekly.cmd"
        launcher.write_text("@echo off\r\n", encoding="utf-8")
        (cron_env / "scripts" / "design-status-flip-policy.json").write_text(
            json.dumps({
                "contract_version": 2,
                "scheduler_timeout_seconds": 7500,
                "wrapper_timeout_seconds": 6600,
                "pass1_timeout_seconds": 5400,
                "pass2_timeout_seconds": 1200,
                "pass2_candidate_cap": 20,
                "panvola_target": 20,
            }),
            encoding="utf-8",
        )
        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, *, timeout):
                captured["timeout"] = timeout
                return "ok\n", ""

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(sched_mod.sys, "platform", "win32")
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        monkeypatch.setattr(
            sched_mod,
            "windows_detach_flags_without_breakaway",
            lambda: 0x08000200,
        )
        monkeypatch.setattr(sched_mod.subprocess, "Popen", fake_popen)

        success, output = _run_job_script(launcher.name)

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [
            r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", str(launcher.resolve()),
        ]
        assert captured["timeout"] == 7500
        assert captured["kwargs"]["creationflags"] == 0x08000200
        assert captured["kwargs"]["env"][sched_mod._DESIGN_STATUS_FLIP_PARENT_TREE_ENV] == "v2"


    def test_script_subprocess_env_sanitized(self, cron_env, monkeypatch):
        """Cron scripts must not inherit Hermes provider env (SECURITY.md §2.3)."""
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
        from cron.scheduler import _run_job_script

        # sorted() so the probed var is deterministic across runs
        # (frozenset iteration order varies with PYTHONHASHSEED).
        blocked_var = sorted(_HERMES_PROVIDER_ENV_BLOCKLIST)[0]
        monkeypatch.setenv(blocked_var, "must_not_leak")

        script = cron_env / "scripts" / "env_probe.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import os
                key = {blocked_var!r}
                print("PRESENT" if os.environ.get(key) else "ABSENT")
                """
            )
        )

        success, output = _run_job_script("env_probe.py")
        assert success is True
        assert output == "ABSENT"

    @pytest.mark.windows_only
    def test_windows_uv_venv_python_script_bypasses_launcher(self, cron_env, tmp_path, monkeypatch):
        # Windows-only: the fake ``sys.platform`` could not reproduce the
        # ``Scripts/python.exe`` launcher layout or the CREATE_NO_WINDOW
        # creationflags this branch exists for.
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        venv = tmp_path / "venv"
        venv_scripts = venv / "Scripts"
        site_packages = venv / "Lib" / "site-packages"
        base = tmp_path / "base"
        venv_scripts.mkdir(parents=True)
        site_packages.mkdir(parents=True)
        base.mkdir()
        venv_python = venv_scripts / "python.exe"
        base_python = base / "python.exe"
        venv_python.write_text("", encoding="utf-8")
        base_python.write_text("", encoding="utf-8")
        (venv / "pyvenv.cfg").write_text(f"home = {base}\nuv = true\n", encoding="utf-8")

        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, *, timeout):
                assert timeout > 0
                return "ok\n", ""

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(sched_mod.sys, "executable", str(venv_python))
        monkeypatch.setattr(sched_mod.subprocess, "Popen", fake_popen)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [str(base_python), str(script.resolve())]
        assert captured["kwargs"]["creationflags"] == sched_mod.windows_hide_flags()
        env = captured["kwargs"]["env"]
        assert env["VIRTUAL_ENV"] == str(venv)
        assert str(site_packages) in env["PYTHONPATH"]


    def test_non_windows_script_preserves_default_text_decoding(self, cron_env, monkeypatch):
        # No platform patching: the Linux CI host already takes this branch.
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "probe.py"
        script.write_text('print("ok")\n')

        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, *, timeout):
                assert timeout > 0
                return "ok\n", ""

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(sched_mod.subprocess, "Popen", fake_popen)

        success, output = _run_job_script("probe.py")

        assert success is True
        assert output == "ok"
        assert captured["argv"] == [sys.executable, str(script.resolve())]
        assert captured["kwargs"]["text"] is True
        assert "creationflags" not in captured["kwargs"]
        assert "encoding" not in captured["kwargs"]
        assert "errors" not in captured["kwargs"]
        assert captured["kwargs"]["start_new_session"] is True

    def test_windows_timeout_uses_shared_tree_terminator(self, cron_env, monkeypatch):
        """Windows containment delegates to taskkill-capable shared cleanup."""
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "timeout.py"
        script.write_text('print("never reached")\n')
        captured = {}

        class TimeoutProcess:
            pid = 4321
            returncode = None

            def communicate(self, *, timeout):
                captured.setdefault("timeouts", []).append(timeout)
                raise sched_mod.subprocess.TimeoutExpired("python", timeout)

        proc = TimeoutProcess()

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return proc

        terminated = []
        monkeypatch.setattr(sched_mod.sys, "platform", "win32")
        monkeypatch.setattr(sched_mod, "windows_hide_flags", lambda: 0x08000000)
        monkeypatch.setattr(sched_mod, "_get_script_timeout", lambda: 17)
        monkeypatch.setattr(sched_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(
            sched_mod,
            "terminate_process_tree",
            lambda received, **_kwargs: terminated.append(received) or True,
        )

        success, output = _run_job_script("timeout.py")

        assert success is False
        assert "operational script timeout after 17s" in output.lower()
        assert "termination confirmed" in output.lower()
        assert terminated == [proc]
        assert captured["kwargs"]["creationflags"] == 0x08000000
        assert "start_new_session" not in captured["kwargs"]
        assert captured["timeouts"] == [17, 2]

    def test_operational_timeout_is_not_misreported_as_provider_failure(self):
        from cron.scheduler import _summarize_cron_failure_for_delivery

        summary = _summarize_cron_failure_for_delivery(
            {"name": "Design Flip"},
            "Operational script timeout after 6600s; process-tree termination confirmed",
        )

        assert "operational script deadline" in summary.lower()
        assert "provider fallback was not inferred" in summary.lower()
        assert "provider timeout" not in summary.lower()

    @pytest.mark.live_system_guard_bypass
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
    def test_timeout_terminates_wrapper_and_grandchild_process_tree(self, cron_env, monkeypatch):
        """A scheduler deadline must leave no script descendant running.

        This deliberately uses a real Python wrapper which spawns a real
        grandchild.  A Popen mock cannot prove the POSIX session/group cleanup
        path that prevents Design Flip's orphaned workers.
        """
        from cron.scheduler import _run_job_script

        marker = cron_env / "grandchild.pid"
        script = cron_env / "scripts" / "forking.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import pathlib
                import subprocess
                import sys
                import time

                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
                pathlib.Path({str(marker)!r}).write_text(str(child.pid), encoding="utf-8")
                time.sleep(300)
                """
            )
        )
        monkeypatch.setattr("cron.scheduler._get_script_timeout", lambda: 0.35)

        success, output = _run_job_script("forking.py")

        assert success is False
        assert "operational script timeout" in output.lower()
        assert marker.exists(), "fixture did not spawn its grandchild before timeout"
        grandchild_pid = int(marker.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            # Do not leak a fixture process if a regression makes the
            # assertion fail; the assertion still fails below.
            os.kill(grandchild_pid, 9)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)

    @pytest.mark.live_system_guard_bypass
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX setsid escape semantics")
    def test_design_flip_outer_timeout_prevents_inner_controller_child_escape(
        self, cron_env, monkeypatch,
    ):
        """A controller ``start_new_session`` child cannot escape outer cleanup.

        This is the cross-layer reproduction of the incident topology:
        scheduler -> shell launcher -> controller -> flip child.  Before the
        v2 parent-tree handshake, the controller's ``start_new_session=True``
        detached that last child; the outer group kill then left it alive.
        """
        from cron import scheduler as sched_mod
        from cron.scheduler import _run_job_script

        marker = cron_env / "escaped-child.pid"
        child = cron_env / "scripts" / "escaped_child.py"
        child.write_text(
            textwrap.dedent(
                f"""\
                import os
                import pathlib
                import sys
                import time

                pathlib.Path({str(marker)!r}).write_text(
                    f"{{os.getpid()}} {{os.getpgrp()}}", encoding="utf-8"
                )
                time.sleep(300)
                """
            ),
            encoding="utf-8",
        )
        controller = cron_env / "scripts" / "controller.py"
        controller.write_text(
            textwrap.dedent(
                f"""\
                import subprocess
                import sys
                import time

                scheduler_owns_parent_tree = (
                    __import__("os").environ.get(
                        "HERMES_DESIGN_STATUS_FLIP_PARENT_TREE"
                    ) == "v2"
                )
                subprocess.Popen(
                    [sys.executable, {str(child)!r}],
                    # Pre-fix this was unconditionally True.  Under the
                    # scheduler contract it must retain the outer PGID.
                    start_new_session=not scheduler_owns_parent_tree,
                )
                time.sleep(300)
                """
            ),
            encoding="utf-8",
        )
        launcher = cron_env / "scripts" / "design-status-flip-panvola-live-weekly.sh"
        launcher.write_text(
            f"#!/usr/bin/env bash\n{sys.executable!s} {controller!s}\n",
            encoding="utf-8",
        )

        # The policy resolver itself is covered above; keep this real process
        # fixture fast while retaining the special launcher's containment mode.
        monkeypatch.setattr(sched_mod, "_script_timeout_for_path", lambda _path: 0.35)

        success, output = _run_job_script(launcher.name)

        assert success is False
        assert "operational script timeout" in output.lower()
        deadline = time.monotonic() + 5.0
        child_pid = None
        child_pgid = None
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text(encoding="utf-8").strip():
                child_pid, child_pgid = map(int, marker.read_text(encoding="utf-8").split())
                break
            time.sleep(0.05)
        assert child_pid is not None, "fixture controller did not launch detached child"
        assert child_pgid != child_pid, "scheduler handshake did not retain outer process group"

        import psutil

        def child_is_live() -> bool:
            try:
                process = psutil.Process(child_pid)
                return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False

        while time.monotonic() < deadline and child_is_live():
            time.sleep(0.05)
        escaped_child_alive = child_is_live()
        if escaped_child_alive:
            # Preserve the no-leak property even when the assertion regresses.
            os.kill(child_pid, 9)
        assert not escaped_child_alive, "inner controller child survived outer timeout"

    def test_emoji_stdout_round_trips_through_script_capture(self, cron_env):
        """Emoji in script stdout must reach the caller intact (#42384).

        On Windows the fix is the utf-8 + errors='replace' popen kwargs
        (asserted above); on POSIX the UTF-8 locale default must already
        carry emoji through. Either way the delivery content is the real
        text, never an exception.
        """
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "emoji.py"
        script.write_text(
            'import sys\n'
            'sys.stdout.buffer.write("backup done \\N{PARTY POPPER} 日次".encode("utf-8"))\n',
            encoding="utf-8",
        )

        success, output = _run_job_script("emoji.py")

        assert success is True
        assert "backup done 🎉 日次" == output

    def test_invalid_utf8_stdout_does_not_raise(self, cron_env):
        """Truncated/invalid UTF-8 in script stdout must never escape as an
        exception (#47393) — a raised UnicodeDecodeError higher up would
        silently drop the whole delivery (#42384). The run may fail, but it
        must fail as a (False, message) result the scheduler can deliver.
        """
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "bad_bytes.py"
        # b'\xe6\x97' is the first two bytes of a three-byte CJK sequence —
        # a truncated write, exactly the shape reported in #47393.
        script.write_text(
            "import sys\n"
            "sys.stdout.buffer.write(b'partial \\xe6\\x97')\n",
            encoding="utf-8",
        )

        success, output = _run_job_script("bad_bytes.py")  # must not raise

        assert isinstance(success, bool)
        assert isinstance(output, str)
        assert output  # a message is always produced, never a silent drop


class TestBuildJobPromptWithScript:
    """Test that script output is injected into the prompt."""

    def test_script_output_injected(self, cron_env):
        from cron.scheduler import _build_job_prompt

        script = cron_env / "scripts" / "data.py"
        script.write_text('print("new PR: #123 fix typo")\n')

        job = {
            "prompt": "Report any notable changes.",
            "script": str(script),
        }
        prompt = _build_job_prompt(job)
        assert "## Script Output" in prompt
        assert "new PR: #123 fix typo" in prompt
        assert "Report any notable changes." in prompt

    def test_script_error_injected(self, cron_env):
        from cron.scheduler import _build_job_prompt

        job = {
            "prompt": "Report status.",
            "script": "nonexistent_monitor.py",
        }
        prompt = _build_job_prompt(job)
        assert "## Script Error" in prompt
        assert "not found" in prompt.lower()
        assert "Report status." in prompt

    def test_no_script_unchanged(self, cron_env):
        from cron.scheduler import _build_job_prompt

        job = {"prompt": "Simple job."}
        prompt = _build_job_prompt(job)
        assert "## Script Output" not in prompt
        assert "Simple job." in prompt


class TestCronjobToolScript:
    """Test the cronjob tool's script parameter."""


    def test_clear_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        create_result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="some_script.py",
        ))
        job_id = create_result["job_id"]

        update_result = json.loads(cronjob(
            action="update",
            job_id=job_id,
            script="",
        ))
        assert update_result["success"] is True
        assert "script" not in update_result["job"]

    def test_list_shows_script(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="data_collector.py",
        )

        list_result = json.loads(cronjob(action="list"))
        assert list_result["success"] is True
        assert len(list_result["jobs"]) == 1
        assert list_result["jobs"][0]["script"] == "data_collector.py"


class TestScriptPathContainment:
    """Regression tests for path containment bypass in _run_job_script().

    Prior to the fix, absolute paths and ~-prefixed paths bypassed the
    scripts_dir containment check entirely, allowing arbitrary script
    execution through the cron system.
    """

    def test_absolute_path_outside_scripts_dir_blocked(self, cron_env):
        """Absolute paths outside ~/.hermes/scripts/ must be rejected."""
        from cron.scheduler import _run_job_script

        # Create a script outside the scripts dir
        outside_script = cron_env / "outside.py"
        outside_script.write_text('print("should not run")\n')

        success, output = _run_job_script(str(outside_script))
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()


    def test_tilde_path_blocked(self, cron_env):
        """~ prefixed paths must be rejected (expanduser bypasses check)."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("~/evil.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_tilde_traversal_blocked(self, cron_env):
        """~/../../../tmp/evil.py must be rejected."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("~/../../../tmp/evil.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_relative_traversal_still_blocked(self, cron_env):
        """../../etc/passwd style traversal must still be blocked."""
        from cron.scheduler import _run_job_script

        success, output = _run_job_script("../../etc/passwd")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()

    def test_relative_path_inside_scripts_dir_allowed(self, cron_env):
        """Relative paths within the scripts dir should still work."""
        from cron.scheduler import _run_job_script

        script = cron_env / "scripts" / "good.py"
        script.write_text('print("ok")\n')

        success, output = _run_job_script("good.py")
        assert success is True
        assert output == "ok"

    def test_subdirectory_inside_scripts_dir_allowed(self, cron_env):
        """Relative paths to subdirectories within scripts/ should work."""
        from cron.scheduler import _run_job_script

        subdir = cron_env / "scripts" / "monitors"
        subdir.mkdir()
        script = subdir / "check.py"
        script.write_text('print("sub ok")\n')

        success, output = _run_job_script("monitors/check.py")
        assert success is True
        assert output == "sub ok"


    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks require elevated privileges on Windows",
    )
    def test_symlink_escape_blocked(self, cron_env, tmp_path):
        """Symlinks pointing outside scripts/ must be rejected."""
        from cron.scheduler import _run_job_script

        # Create a script outside the scripts dir
        outside = tmp_path / "outside_evil.py"
        outside.write_text('print("escaped")\n')

        # Create a symlink inside scripts/ pointing outside
        link = cron_env / "scripts" / "sneaky.py"
        link.symlink_to(outside)

        success, output = _run_job_script("sneaky.py")
        assert success is False
        assert "blocked" in output.lower() or "outside" in output.lower()


class TestCronjobToolScriptValidation:
    """Test API-boundary validation of cron script paths in cronjob_tools."""


    def test_create_with_traversal_script_rejected(self, cron_env, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        from tools.cronjob_tools import cronjob

        result = json.loads(cronjob(
            action="create",
            schedule="every 1h",
            prompt="Monitor things",
            script="../../etc/passwd",
        ))
        assert result["success"] is False
        assert "escapes" in result["error"].lower() or "traversal" in result["error"].lower()


class TestRunJobEnvVarCleanup:
    """Test that run_job() env vars are cleaned up even on early failure."""

    def test_env_vars_cleaned_on_early_error(self, cron_env, monkeypatch):
        """Origin env vars must be cleaned up even if run_job fails early."""
        # Ensure env vars are clean before test
        for key in (
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_CHAT_NAME",
        ):
            monkeypatch.delenv(key, raising=False)

        # Build a job with origin info that will fail during execution
        # (no valid model, no API key — will raise inside try block)
        job = {
            "id": "test-envleak",
            "name": "env-leak-test",
            "prompt": "test",
            "schedule_display": "every 1h",
            "origin": {
                "platform": "telegram",
                "chat_id": "12345",
                "chat_name": "Test Chat",
            },
        }

        from cron.scheduler import run_job

        # Expect it to fail (no model/API key), but env vars must be cleaned
        try:
            run_job(job)
        except Exception:
            pass

        # Verify env vars were cleaned up by the finally block
        assert os.environ.get("HERMES_SESSION_PLATFORM") is None
        assert os.environ.get("HERMES_SESSION_CHAT_ID") is None
        assert os.environ.get("HERMES_SESSION_CHAT_NAME") is None
