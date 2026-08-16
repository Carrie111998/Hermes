"""Tests for ``install_cua_driver`` upgrade semantics.

The cua-driver upstream installer always pulls the latest release tag, so
re-running it is the canonical upgrade path. ``install_cua_driver(upgrade=True)``
must:

* Be supported-platform-only — no-op silently elsewhere so ``hermes update``
  can call it unconditionally without warning unsupported-platform users.
* Re-run the installer even when the binary is already on PATH (this is the
  fix for the "we only pulled cua-driver once on enable" complaint).
* Preserve original ``upgrade=False`` behaviour for the toolset-enable flow:
  skip if installed, install otherwise, warn on unsupported platforms.

The pre-install arch probe that used to live alongside this function was
deleted (see top-of-file comment in tools_config.py) — the upstream
installer has CUA_DRIVER_RS_BAKED_VERSION baked in by CD and errors
cleanly on missing-arch assets, and the upgrade path uses
``cua_driver_update_check()`` (which shells `cua-driver check-update
--json` against the already-installed binary).
"""

from __future__ import annotations

from unittest.mock import patch


class TestInstallCuaDriverUpgrade:
    def test_upgrade_on_unsupported_platform_is_silent_noop(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="FreeBSD"):
            assert tools_config.install_cua_driver(upgrade=True) is False
            warn.assert_not_called()

    def test_non_upgrade_on_unsupported_platform_warns(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="FreeBSD"):
            assert tools_config.install_cua_driver(upgrade=False) is False
            warn.assert_called()

    def test_upgrade_on_macos_with_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            kwargs = runner.call_args.kwargs
            assert kwargs.get("verbose") is False

    def test_upgrade_on_macos_without_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()

    def test_upgrade_on_macos_non_writable_applications_skips_refresh(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_cua_install_target_writable",
                          return_value=False), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner, \
             patch.object(tools_config, "_print_info") as info:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_not_called()
            assert any(
                "/Applications is not writable" in call.args[0]
                for call in info.call_args_list
            )

    def test_fresh_install_on_macos_non_writable_applications_skips_install(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_cua_install_target_writable",
                          return_value=False), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner, \
             patch.object(tools_config, "_print_info") as info:
            assert tools_config.install_cua_driver(upgrade=False) is False
            runner.assert_not_called()
            assert any(
                "/Applications is not writable" in call.args[0]
                for call in info.call_args_list
            )

    def test_non_upgrade_on_macos_with_binary_skips_install(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_not_called()

    def test_non_upgrade_on_macos_without_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()


class TestArchProbeRemoval:
    """Regression tests for the deletion of `_check_cua_driver_asset_for_arch`.

    The old probe queried ``/releases/latest`` on trycua/cua and inspected
    asset names. That was wrong in two ways:

    1. cua-driver-rs releases are marked **prerelease** on every cut, so
       ``/releases/latest`` returns the Python ``cua-agent`` / ``cua-computer``
       package instead — a release with zero binary assets. The probe then
       reported "no asset for $arch" on Linux x86_64, Windows, macOS Intel,
       Linux arm64 — every non-Apple-Silicon host.
    2. Even with the right endpoint, it duplicated tag-resolution the upstream
       installer already does correctly via ``CUA_DRIVER_RS_BAKED_VERSION``
       (auto-baked by CD on every release).

    The fix: stop probing. Trust the upstream installer for fresh installs
    (it has the baked version + correct API fallback) and the
    ``cua-driver check-update --json`` MCP-binary native command for the
    upgrade path.
    """

    def test_probe_function_is_gone(self):
        from hermes_cli import tools_config
        assert not hasattr(tools_config, "_check_cua_driver_asset_for_arch")
        assert not hasattr(tools_config, "_latest_cua_driver_rs_release")

    def test_fresh_install_does_not_call_github_api(self):
        """Pre-install no longer probes the GitHub API — the upstream
        ``install.sh`` resolves the tag from its baked CUA_DRIVER_RS_BAKED_VERSION
        line. install.sh errors cleanly when the arch has no asset, so the
        probe was duplicate gatekeeping.
        """
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()
            urlopen.assert_not_called()

    def test_upgrade_with_binary_does_not_call_github_api_directly(self):
        """The upgrade path no longer hits GitHub from Python — it delegates
        to the upstream ``install.sh`` (which has the baked release tag and
        the proper API fallback). When cua-driver is already installed,
        ``cua_driver_update_check()`` (added in a separate change) further
        short-circuits the network re-install via the binary's native
        ``check-update --json`` verb.
        """
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in ("cua-driver", "curl") else None), \
             patch("urllib.request.urlopen") as urlopen, \
             patch("subprocess.run"), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            # Probe deleted — no direct GitHub API call from Python.
            urlopen.assert_not_called()


class TestStaleInstallLockClear:
    """_clear_stale_cua_install_lock: pre-clears the upstream installer's
    concurrent-install lock only when the holder is provably dead (or the
    lock is old and pid-less). Issue #58762."""

    def _make_lock(self, tmp_path, pid=None):
        import os
        home = tmp_path / ".cua-driver"
        lock = home / "packages" / ".install.lock.d"
        lock.mkdir(parents=True)
        if pid is not None:
            (lock / "info").write_text(f"pid={pid}\n")
        os.environ["CUA_DRIVER_RS_HOME"] = str(home)
        return lock

    def teardown_method(self):
        import os
        os.environ.pop("CUA_DRIVER_RS_HOME", None)

    def test_dead_holder_lock_is_cleared(self, tmp_path):
        from hermes_cli import tools_config

        dead_pid = 4194000  # above default pid_max on most systems
        lock = self._make_lock(tmp_path, pid=dead_pid)
        with patch.object(tools_config, "_print_info"):
            tools_config._clear_stale_cua_install_lock()
        assert not lock.exists()

    def test_live_holder_lock_is_kept(self, tmp_path):
        import os
        from hermes_cli import tools_config

        lock = self._make_lock(tmp_path, pid=os.getpid())
        tools_config._clear_stale_cua_install_lock()
        assert lock.exists()

    def test_pidless_fresh_lock_is_kept(self, tmp_path):
        from hermes_cli import tools_config

        lock = self._make_lock(tmp_path, pid=None)
        tools_config._clear_stale_cua_install_lock()
        assert lock.exists()

    def test_pidless_old_lock_is_cleared(self, tmp_path):
        import os
        import time
        from hermes_cli import tools_config

        lock = self._make_lock(tmp_path, pid=None)
        old = time.time() - (tools_config._CUA_LOCK_STALE_AFTER + 60)
        os.utime(lock, (old, old))
        with patch.object(tools_config, "_print_info"):
            tools_config._clear_stale_cua_install_lock()
        assert not lock.exists()

    def test_no_lock_is_noop(self, tmp_path):
        import os
        os.environ["CUA_DRIVER_RS_HOME"] = str(tmp_path / ".cua-driver")
        from hermes_cli import tools_config
        tools_config._clear_stale_cua_install_lock()  # must not raise


class TestInstallerTimeoutKillsProcessGroup:
    """On timeout the whole installer process group must be killed, so the
    `curl | bash` grandchildren can't survive holding the install lock."""

    def test_timeout_kills_process_group_and_returns_false(self, tmp_path):
        """Verbose path: timeout must killpg the whole group, and return False.

        Drives ``verbose=True`` because that is the branch that still owns a
        ``Popen`` -- it streams the installer live to the console, so it cannot
        capture.  The capturing branch moved to ``run_text_capture`` (see
        TestInstallerCapturePipeHazard) and its kill lives inside that helper.

        The kill itself is now ``_subprocess_compat._tree_kill``, so the
        ``killpg`` to intercept belongs to THAT module, not ``tools_config``.

        ⚠ ``patch("platform.system")`` is not enough to reach the POSIX arm any
        more: ``_tree_kill`` branches on ``_subprocess_compat.IS_WINDOWS``, a
        constant bound at import, which stays True on a Windows host no matter
        what ``platform.system()`` reports.  Patch the constant too or this
        silently exercises the Windows taskkill arm instead.
        """
        import subprocess
        from unittest.mock import MagicMock
        from hermes_cli import tools_config
        from hermes_cli import _subprocess_compat

        # Windows has neither os.getpgid/os.killpg nor signal.SIGKILL, so all
        # three need create=True to stand in.  Without the SIGKILL stub this
        # test fails on Windows for a reason that looks like the code under
        # test: _tree_kill raises AttributeError before reaching killpg, the
        # installer's broad `except Exception: return False` swallows it, and
        # the assertion reports "process group was not killed".  That is the
        # pre-existing red this test carried on this host.
        SIGKILL = getattr(__import__("signal"), "SIGKILL", 9)
        killed = {}

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        # First communicate() raises TimeoutExpired, second (post-kill) returns.
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            ("", None),
        ]

        def fake_killpg(pgid, sig):
            killed["pgid"] = pgid
            killed["sig"] = sig

        with patch("platform.system", return_value="Linux"), \
             patch.object(_subprocess_compat, "IS_WINDOWS", False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
             patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(_subprocess_compat.os, "getpgid", return_value=99999,
                          create=True), \
             patch.object(_subprocess_compat.os, "killpg", side_effect=fake_killpg,
                          create=True), \
             patch.object(_subprocess_compat.signal, "SIGKILL", SIGKILL,
                          create=True), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"):
            ok = tools_config._run_cua_driver_installer(label="Installing", verbose=True)

        assert ok is False
        assert killed.get("pgid") == 99999, (
            "the installer's process group was not killed -- surviving "
            "curl|bash grandchildren keep holding the install lock"
        )
        assert killed.get("sig") == SIGKILL
        # Post-kill reap happened.
        assert fake_proc.communicate.call_count == 2

    def test_timeout_ceiling_exceeds_upstream_lock_window(self):
        from hermes_cli import tools_config
        # The upstream installer waits up to 600s before reclaiming a stale
        # lock; our ceiling must give that window room to complete.
        assert tools_config._CUA_INSTALLER_TIMEOUT > tools_config._CUA_LOCK_STALE_AFTER

    def test_installer_runs_in_new_session_on_posix(self, tmp_path):
        """POSIX new-session must survive the move to run_text_capture.

        The capturing branch no longer builds its own ``popen_kwargs`` -- the
        helper does.  This still asserts on the real ``Popen`` call because the
        patch is on ``subprocess.Popen``, which ``run_text_capture`` calls
        internally, so the guarantee is checked end to end rather than by
        trusting the helper.  As above, ``IS_WINDOWS`` must be patched or the
        helper takes its Windows creationflags arm on this host.
        """
        import subprocess
        from unittest.mock import MagicMock
        from hermes_cli import tools_config
        from hermes_cli import _subprocess_compat

        captured = {}
        fake_proc = MagicMock()
        fake_proc.pid = 1
        fake_proc.returncode = 1
        fake_proc.communicate.return_value = ("", None)

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return fake_proc

        with patch("platform.system", return_value="Linux"), \
             patch.object(_subprocess_compat, "IS_WINDOWS", False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"):
            tools_config._run_cua_driver_installer(label="Refreshing", verbose=False)

        assert captured, "no process was spawned at all -- the seam is dead"
        assert captured.get("start_new_session") is True


class TestInstallerNoShell:
    """The POSIX installer path must not use shell=True or command
    substitution: the script is downloaded to a mkstemp file and exec'd
    as a plain argv list (salvage of #34974's intent, without the fixed
    /tmp path TOCTOU that PR introduced)."""

    def _run(self, download_rc=0):
        import subprocess
        from unittest.mock import MagicMock
        from hermes_cli import tools_config

        calls = []
        fake_proc = MagicMock()
        fake_proc.pid = 1
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("", None)

        def fake_run(cmd, **kw):
            calls.append(("run", cmd, kw))
            m = MagicMock()
            m.returncode = download_rc
            m.stderr = "curl: (6) could not resolve" if download_rc else ""
            return m

        def fake_popen(cmd, **kw):
            calls.append(("popen", cmd, kw))
            return fake_proc

        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(tools_config.shutil, "which", return_value="/usr/local/bin/cua-driver"), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_print_success"):
            ok = tools_config._run_cua_driver_installer(label="Refreshing", verbose=False)
        return ok, calls

    def test_posix_path_downloads_then_execs_argv_list(self):
        ok, calls = self._run()
        assert ok is True
        run_calls = [c for c in calls if c[0] == "run"]
        popen_calls = [c for c in calls if c[0] == "popen"]
        assert len(run_calls) == 1 and len(popen_calls) == 1
        # Download: plain argv curl, no shell.
        dl_cmd = run_calls[0][1]
        assert isinstance(dl_cmd, list) and dl_cmd[0] == "curl"
        # Exec: argv list ["/bin/bash", <mkstemp path>], shell=False.
        exec_cmd, exec_kw = popen_calls[0][1], popen_calls[0][2]
        assert isinstance(exec_cmd, list) and exec_cmd[0] == "/bin/bash"
        assert "cua-driver-install-" in exec_cmd[1]
        assert exec_kw.get("shell") is False

    def test_download_failure_returns_false_without_exec(self):
        ok, calls = self._run(download_rc=6)
        assert ok is False
        assert not [c for c in calls if c[0] == "popen"]

    def test_temp_script_removed_after_run(self, tmp_path):
        import os
        captured = {}
        import subprocess
        from unittest.mock import MagicMock
        from hermes_cli import tools_config

        fake_proc = MagicMock()
        fake_proc.pid = 1
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("", None)

        def fake_run(cmd, **kw):
            m = MagicMock(); m.returncode = 0; m.stderr = ""
            return m

        def fake_popen(cmd, **kw):
            captured["script"] = cmd[1]
            return fake_proc

        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(tools_config.shutil, "which", return_value="/usr/local/bin/cua-driver"), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_print_success"):
            tools_config._run_cua_driver_installer(label="Refreshing", verbose=False)

        assert "script" in captured
        assert not os.path.exists(captured["script"])


class TestInstallerCapturePipeHazard:
    """The capturing install path must not capture through PIPES.

    On Windows ``install_cmd`` is ``powershell -Command "irm …/install.ps1 |
    iex"`` -- an installer that certainly spawns grandchildren.  A grandchild
    inherits the capture pipe's write handle, so the pipe never reaches EOF,
    and the post-timeout ``proc.communicate()`` (which carried no timeout)
    blocked until that grandchild exited.  ``_CUA_INSTALLER_TIMEOUT`` was
    therefore not a bound at all on the ``hermes update`` refresh path.

    A tree-kill alone does not fix it -- measured, ``taskkill`` blows its own
    10s cap on a real installer tree and the grandchild survives.  The pipes
    have to go: ``run_text_capture`` captures into temp files (nothing to
    drain) and tree-kills.  See ``hermes_cli._subprocess_compat``.

    ⚠ EVERY test here MUST patch ``subprocess.Popen``, even ones that appear
    not to spawn.  While this class was being written, a test that patched
    only ``run_text_capture`` -- the seam the code did not use yet -- let the
    real ``irm … | iex`` run against the network and hang the session on the
    very reader-thread join it was written to prevent.  ``_no_spawn`` below
    fails closed so a dead seam can never reach a live installer again.
    """

    @staticmethod
    def _no_spawn(*args, **kwargs):
        raise AssertionError(
            f"real process spawn attempted in a unit test: {args!r} {kwargs!r}"
        )

    def test_capturing_path_uses_file_backed_capture_not_pipes(self):
        import subprocess
        from hermes_cli import tools_config

        seen = {}

        def fake_capture(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(list(argv), 0, "next steps", "")

        with patch("platform.system", return_value="Windows"), \
             patch("hermes_cli._subprocess_compat.run_text_capture", fake_capture), \
             patch("subprocess.Popen", side_effect=self._no_spawn), \
             patch.object(tools_config, "_cua_driver_cmd", return_value="cua-driver"), \
             patch.object(tools_config.shutil, "which", return_value=None), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_success"), \
             patch.object(tools_config, "_print_info"):
            tools_config._run_cua_driver_installer(label="Refreshing", verbose=False)

        assert "argv" in seen, (
            "the capturing install path did not go through run_text_capture -- "
            "back on Popen+PIPE means a surviving installer grandchild makes "
            "the post-timeout drain unbounded"
        )
        assert seen["argv"][0] == "powershell"
        assert seen["kwargs"]["timeout"] == tools_config._CUA_INSTALLER_TIMEOUT

    def test_capturing_path_timeout_returns_false(self):
        import subprocess
        from hermes_cli import tools_config

        called = []

        def fake_capture(argv, **kwargs):
            called.append(list(argv))
            raise subprocess.TimeoutExpired(list(argv), kwargs["timeout"])

        with patch("platform.system", return_value="Windows"), \
             patch("hermes_cli._subprocess_compat.run_text_capture", fake_capture), \
             patch("subprocess.Popen", side_effect=self._no_spawn), \
             patch.object(tools_config, "_cua_driver_cmd", return_value="cua-driver"), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"):
            ok = tools_config._run_cua_driver_installer(label="Refreshing", verbose=False)

        # Vacuity guard: the function swallows unexpected exceptions into
        # `return False`, so `ok is False` alone also passes when the seam is
        # dead and _no_spawn fired.  Prove the timeout came from the helper.
        assert called, (
            "run_text_capture was never reached -- `ok is False` came from the "
            "broad except swallowing the spawn guard, not from a timeout"
        )
        assert ok is False

    def test_verbose_windows_timeout_kills_the_whole_tree(self):
        """The verbose path keeps Popen (it streams live to the console), so
        its own kill must reach the tree.  ``proc.kill()`` reaps only
        powershell.exe and leaves install.ps1's children holding the upstream
        install lock -- which then wedges every later run.
        """
        import subprocess
        from unittest.mock import MagicMock
        from hermes_cli import tools_config

        fake_proc = MagicMock()
        fake_proc.pid = 4321
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            ("", None),
        ]
        tree_killed = []

        with patch("platform.system", return_value="Windows"), \
             patch("subprocess.Popen", return_value=fake_proc), \
             patch("hermes_cli._subprocess_compat._tree_kill",
                   side_effect=lambda p: tree_killed.append(p.pid)), \
             patch.object(tools_config, "_cua_driver_cmd", return_value="cua-driver"), \
             patch.object(tools_config, "_clear_stale_cua_install_lock"), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"):
            ok = tools_config._run_cua_driver_installer(label="Installing", verbose=True)

        assert ok is False
        assert tree_killed == [4321], (
            "verbose timeout did not tree-kill; a bare proc.kill() leaves the "
            "installer's grandchildren alive holding the install lock"
        )
        assert fake_proc.kill.call_count == 0, (
            "a bare proc.kill() reaps only the direct child -- on Windows that "
            "is powershell.exe, not the installer it launched"
        )
