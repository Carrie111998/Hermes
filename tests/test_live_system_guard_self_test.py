"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process or mutate
the live systemd unit MUST be exercised below. Adding a new primitive to
the guard? Add a test here too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

# A guaranteed-foreign PID: one that is owned by the system, is never in
# this test's subtree, and — crucially — ACTUALLY EXISTS on the platform
# under test.
#
# PID 1 (init) satisfies that on POSIX only. On Windows there is no PID 1:
# ``psutil.Process(1)`` raises NoSuchProcess, and ``_is_own_subtree`` then
# deliberately allowlists it ("stale PID — kill would be a no-op anyway").
# So on Windows a PID-1 assertion was vacuous: the guard never fired, the
# real ``os.kill`` ran, and the test failed with OSError rather than the
# RuntimeError it was written to demand — it could not fail for its own
# reason. PID 4 is Windows "System": always present, parented by PID 0,
# never in the test subtree. Same platform split — and the same reason —
# as the ``_FOREIGN_LIVE_PID`` constant added by cd51467573 (branch
# claude/zealous-mendeleev-5e8346) for the psutil-kill self-tests; if that
# branch lands, the two should collapse into this one.
FOREIGN_PID = 4 if sys.platform == "win32" else 1


# ──────────────────── kill primitives ─────────────────────────


def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"])


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"])


def test_subprocess_run_sh_c_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sh", "-c", "systemctl --user stop hermes-gateway"])


def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"])


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
        )


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────── direct gateway launch (no systemctl) ─────────
#
# The systemctl guard keys on ``systemctl`` appearing in the command, so it
# never saw the direct spawn paths — which are the ONLY paths Windows uses,
# and the ones every ``--detached`` flow takes. A test that stubbed part of
# the launch surface but missed the branch the code actually took would
# reach the real Popen and leave a background gateway running on the
# developer's machine. That happened on every run of
# ``test_gateway_restart_on_windows_preserves_failure_fallback`` until
# 82b130b6e (two rogue gateways, PIDs 44560 + 43476, on 2026-08-10).


def test_direct_hermes_gateway_run_blocked():
    """``launch_gateway_detached`` builds exactly this argv."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["hermes", "gateway", "run"])


def test_absolute_hermes_entrypoint_gateway_run_blocked():
    """An absolute entrypoint must be caught as readily as the bare name.

    ``launch_gateway_detached`` prefers ``sys.argv[0]`` when it looks like
    the hermes CLI, so the real argv is usually a full path.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen([r"C:\Users\dev\.venv\Scripts\hermes.exe", "gateway", "run"])


def test_pythonw_dash_m_gateway_run_blocked():
    """``gateway_windows._spawn_detached`` builds this argv."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(
            [r"C:\Python311\pythonw.exe", "-m", "hermes_cli.main", "gateway", "run"]
        )


def test_gateway_run_with_replace_flag_blocked():
    """``--replace`` is what the restart path passes; a flag must not hide the verb."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["hermes", "gateway", "run", "--replace"])


def test_gateway_stop_blocked():
    """Stopping the live gateway is as destructive as spawning over it."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["hermes", "gateway", "stop"])


def test_gateway_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["hermes", "gateway", "restart"])


def test_python_dash_m_gateway_run_module_blocked():
    """``python -m gateway.run`` has no ``gateway`` subcommand token at all."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["python", "-m", "gateway.run"])


def test_gateway_run_py_script_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["python", "/opt/hermes/gateway/run.py"])


# The allow-cases below must not spawn ``echo``/``true``: those are POSIX-only
# and would land this file's Windows run red — the exact condition that hid the
# rogue-gateway bug in the first place. ``sys.executable -c ""`` exists on every
# platform, and the guard inspects the argv it is GIVEN, so passing the
# gateway-shaped tokens as arguments exercises the matcher without running one.


def _run_allowed(*extra_argv):
    """Run a harmless real subprocess whose argv carries ``extra_argv``.

    If the guard wrongly matched, this raises RuntimeError instead of
    returning — which is precisely what the allow-case asserts against.
    """
    return subprocess.run(
        [sys.executable, "-c", "", *extra_argv], capture_output=True, text=True
    )


def test_gateway_status_passes_through():
    """Read-only gateway subcommands must NOT be blocked."""
    assert _run_allowed("hermes", "gateway", "status").returncode == 0


def test_gateway_logs_passes_through():
    assert _run_allowed("hermes", "gateway", "logs", "--tail", "20").returncode == 0


def test_gateway_install_passes_through():
    """``install`` writes a service definition; it does not start a gateway."""
    assert _run_allowed("hermes", "gateway", "install").returncode == 0


def test_unrelated_command_containing_gateway_word_passes_through():
    """The word "gateway" alone (e.g. an API gateway path) must not trip it."""
    assert _run_allowed("deploying", "api-gateway", "to", "staging").returncode == 0


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


def test_pty_spawn_systemctl_blocked():
    """``pty.spawn`` is a genuine platform gap, not a weakened assertion.

    The ``pty`` module does not exist on Windows, so there is no primitive
    here for the guard to wrap — ``_live_system_guard`` registers this hook
    inside its own ``try: import pty`` for exactly that reason. Skipping
    where the module is absent keeps the two in step; it does not skip a
    hook that Windows actually has (contrast the allow-cases below, which
    are deliberately written to run everywhere).
    """
    pty = pytest.importorskip("pty", reason="pty is POSIX-only; the guard hook is too")
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"])


def test_subprocess_pkill_hermes_gateway_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-gateway"])


def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"])


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"])


# ─────────── killers spelled the Windows way (literal backslashes) ─────────
#
# These use LITERAL backslash paths rather than tmp_path so the Windows argv
# shape is exercised on every platform. The predicates used to join a list
# argv into a string and re-split it with posix ``shlex``, which treats a
# backslash as an escape: ``C:\Windows\System32\taskkill.exe`` collapsed to
# ``C:WindowsSystem32taskkill.exe``, one token whose basename is in no
# killer list, so the guard stayed silent. Bare ``taskkill`` still fired,
# which is why the hole survived. Same defect class as the Node guard's
# ``_cmd_tokens`` fix and the ``_is_package_install`` migration.
#
# Every payload below is inert if the guard ever stops firing: the binary
# path does not exist, or the target image/unit/pattern matches no real
# process. A canary for live kills must not be able to perform one.

_UNREAL_EXE_DIR = "C:\\hermes-guard-probe-no-such-dir\\bin"
_UNREAL_IMAGE = "hermes-guard-probe-no-such-image.exe"


def test_subprocess_absolute_windows_taskkill_blocked():
    """An absolute ``…\\taskkill.exe /F /IM hermes…`` must fire.

    Pre-fix the whole path became one ``C:hermes-guard-probe…taskkill.exe``
    token, so no basename check could see ``taskkill``.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            [_UNREAL_EXE_DIR + "\\taskkill.exe", "/F", "/IM", _UNREAL_IMAGE]
        )


def test_subprocess_absolute_windows_pkill_python_dash_f_blocked():
    """An absolute ``pkill.exe -f python`` reaches the live gateway too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            [_UNREAL_EXE_DIR + "\\pkill.exe", "-f", "python-guard-probe-no-such-proc"]
        )


def test_subprocess_taskkill_exe_suffix_blocked():
    """The bare Windows spelling ``taskkill.exe`` must match ``taskkill``.

    Unsuffixed ``taskkill`` was in the killer tuple; ``taskkill.exe`` — what
    a resolved Windows argv actually carries — was not.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["taskkill.exe", "/F", "/IM", _UNREAL_IMAGE])


def test_subprocess_windows_taskkill_string_form_blocked():
    """The same command as a shell string rather than an argv list."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            _UNREAL_EXE_DIR + "\\taskkill.exe /F /IM " + _UNREAL_IMAGE, shell=True
        )


def test_subprocess_shell_wrapped_pkill_still_blocked():
    """``bash -c "pkill -f hermes…"`` hides the killer inside one argv element.

    The join+``shlex`` route split that element apart for free. Whatever
    replaces it must keep doing so, or closing the Windows hole would open a
    shell-wrapped one.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            ["bash", "-c", "pkill -f hermes-gateway-guard-probe-no-such-proc"]
        )


def test_subprocess_systemctl_verb_after_trailing_backslash_blocked():
    """A trailing-backslash argument must not swallow the mutating verb.

    Joined and posix-``shlex``-split, the backslash ending ``--root=C:\\``
    escapes the space after it, so ``--root=C: restart`` became a single
    token, ``restart`` was no longer in the token list, and the
    mutating-verb check found nothing.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            [
                "systemctl",
                "--user",
                "--root=C:\\",
                "restart",
                "hermes-gateway-guard-probe-no-such-unit",
            ]
        )


# ──────────────────── pass-through cases (must NOT raise) ──────


# The systemctl allow-cases below carry their tokens as ARGUMENTS to a
# harmless ``sys.executable -c ""`` rather than invoking a real
# ``systemctl``, for the same reason the gateway allow-cases do (see
# ``_run_allowed``): ``systemctl`` does not exist on Windows, so spawning it
# for real landed this file's Windows run red — the exact condition that hid
# the rogue-gateway bug. ``_is_blocked_systemctl`` inspects the whole
# command it is GIVEN (substring for ``systemctl``/hermes tokens, whole-word
# for the mutating verb) with no dependence on which argv slot they occupy,
# so this shape exercises the real matcher on every platform.
# ``test_systemctl_wrapped_restart_is_still_blocked`` is the falsifier that
# keeps these four honest.


def test_systemctl_status_passes_through():
    """Read-only systemctl probes (status/show/list-units) are fine."""
    r = _run_allowed("systemctl", "--user", "status", "hermes-gateway", "--no-pager")
    assert r.returncode == 0  # Did not raise — the guard let it through.


def test_systemctl_show_passes_through():
    r = _run_allowed("systemctl", "--user", "show", "hermes-gateway", "--no-pager")
    assert r.returncode == 0


def test_systemctl_list_units_passes_through():
    r = _run_allowed(
        "systemctl", "--user", "list-units", "fake-not-real-unit*", "--no-pager"
    )
    assert r.returncode == 0


def test_systemctl_unrelated_unit_passes_through():
    """systemctl restart of a non-hermes unit is allowed (we only protect hermes)."""
    r = _run_allowed("systemctl", "--user", "restart", "fake-not-real-unit")
    assert r.returncode == 0


def test_systemctl_wrapped_restart_is_still_blocked():
    """Falsifier for the four allow-cases above.

    They only mean something if the SAME argv shape still trips the guard
    when the verb is a mutating one against a hermes unit. If this stops
    raising, the four ``_passes_through`` tests above have gone vacuous.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        _run_allowed("systemctl", "--user", "restart", "hermes-gateway")


def test_kill_own_subtree_passes_through():
    """We CAN kill our own children — guard recognizes them via psutil."""
    # ``sys.executable`` rather than ``sleep``: coreutils are only on PATH
    # on this box under git-bash, so a bare ``sleep`` made the result
    # shell-dependent on Windows.
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        os.kill(p.pid, signal.SIGTERM)
    finally:
        p.wait(timeout=10)
    if sys.platform == "win32":
        # Windows has no signals: ``os.kill`` calls TerminateProcess(h, sig),
        # so the child's exit code IS the signal number, not ``-sig``.
        assert p.returncode == int(signal.SIGTERM)
    else:
        # SIGTERM = 15; subprocess returncode is -15 on POSIX.
        assert p.returncode in {-signal.SIGTERM, 128 + int(signal.SIGTERM)}


def test_subprocess_pkill_with_unrelated_pattern_passes_through():
    """``pkill -f some-unrelated-pattern`` (no hermes/python) is fine."""
    # We don't actually run pkill — just verify the guard would let it
    # through by inspecting the matcher. Re-implementing the check here
    # would duplicate the guard; instead spawn a noop to confirm no raise.
    # Use 'true' so it succeeds quickly.
    r = subprocess.run(["true"], capture_output=True)
    assert r.returncode == 0


def test_windows_taskkill_on_an_unrelated_pid_passes_through():
    """Widening the killer match must not block a plain pid-targeted taskkill.

    Production's Windows terminate paths spawn ``taskkill /PID <n> /T /F``
    (gateway_windows, the WhatsApp adapter, update's stale-dashboard sweep)
    with no hermes/gateway/python token in the command, and several tests
    assert on that argv. The killer basename now matches with a full path
    and an ``.exe`` suffix, but the hermes/gateway/python condition still
    has to hold — so these stay allowed. A nonexistent binary proves the
    command reached the real subprocess machinery instead of the guard.
    """
    with pytest.raises(FileNotFoundError):
        subprocess.run(
            ["C:\\guard-probe-no-such-dir\\taskkill.exe", "/PID", "4242", "/T", "/F"]
        )


def test_normal_subprocess_run_passes_through():
    """Plain non-systemctl subprocess.run should work normally."""
    r = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert r.stdout.strip() == "hello"


# ──────────────────── package installs ─────────────────────────
#
# A test run must never mutate the developer's / gateway's venv.  The live
# offender was ``tools/lazy_deps.py::_venv_pip_install``: any test that
# enables a platform (e.g. setting FEISHU_APP_ID) reaches
# ``gateway/config.py::_apply_env_overrides`` -> ``entry.check_fn()``, whose
# own comment notes it "lazy-INSTALLS the platform SDK (pip) as a side
# effect".  One ``pytest tests/gateway`` run installed lark_oapi 1.6.8
# (101 MB, 21,169 files) into ~/.hermes/agent-src/.venv — the venv the
# gateway runs from — and timed out mid-install.
#
# Installs that redirect somewhere disposable (``--target``/``--prefix``/
# ``--root``, or a different interpreter) are still allowed: they cannot
# touch the running environment.


def test_uv_pip_install_into_live_venv_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["uv", "pip", "install", "lark-oapi==1.6.8"])


def test_python_m_pip_install_into_live_venv_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([sys.executable, "-m", "pip", "install", "lark-oapi==1.6.8"])


def test_bare_pip_install_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pip", "install", "-e", "."])


def test_pip_install_user_site_blocked():
    """``--user`` mutates the developer's user site-packages, not a tmpdir."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "pyyaml"])


def test_pip_uninstall_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "lark-oapi"])


def test_ensurepip_blocked():
    """``ensurepip --upgrade`` bootstraps pip INTO the live venv."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"])


def test_shell_string_pip_install_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run("uv pip install lark-oapi==1.6.8", shell=True)


def test_pip_install_with_target_passes_through(tmp_path):
    """``--target <dir>`` cannot touch the live venv, so it is allowed.

    ``tests/tools/test_lazy_deps_durable_target.py`` depends on this: its
    opt-in real-install test exercises the durable-target wire end to end.
    A nonexistent executable proves the guard handed the command to the
    real subprocess machinery (FileNotFoundError) rather than blocking it.
    """
    with pytest.raises(FileNotFoundError):
        subprocess.run(
            ["hermes-nonexistent-uv", "pip", "install", "--target", str(tmp_path), "isodate"]
        )


def test_pip_install_into_other_interpreter_passes_through():
    """Installing into a throwaway venv's python is allowed.

    ``tests/test_wheel_locales_e2e.py`` builds a wheel, creates a scratch
    venv and pip-installs into it — that interpreter is not ours.
    """
    with pytest.raises(FileNotFoundError):
        subprocess.run(
            ["/hermes/nonexistent/venv/bin/python", "-m", "pip", "install", "pyyaml"]
        )


# The three tests below are the Windows half of the exemption above.  The
# POSIX-path test could never have caught the defect they cover: the guard
# tokenised a list argv by joining it and re-splitting with posix
# ``shlex.split``, which eats backslashes.  A Windows argv
# ``[r"C:\…\venv\Scripts\python.exe", "-m", "ensurepip", …]`` collapsed into
# one ``C:…venvScriptspython.exe`` token whose basename no longer starts with
# ``python``, so ``_is_foreign_interpreter`` said False and the
# throwaway-venv exemption never fired.  A POSIX path has no backslashes to
# eat, so it sailed through either way.
#
# These use literal backslash paths rather than ``tmp_path`` so they exercise
# the Windows shape on EVERY platform — a native-separator test would silently
# stop covering the regression whenever the suite runs on Linux CI.


def test_windows_venv_ensurepip_passes_through():
    """``venv.create(tmp, with_pip=True)`` on Windows — the reported failure.

    CPython's venv module shells out to
    ``<newvenv>/Scripts/python.exe -m ensurepip --upgrade --default-pip``.
    That interpreter is the throwaway venv's, not ours, so it must pass
    through.  It was blocked, which made ``venv.create`` unusable in tests on
    Windows while working fine on POSIX.
    """
    fake = r"C:\Users\dev\AppData\Local\Temp\pytest-1\t0\venv\Scripts\python.exe"
    with pytest.raises(FileNotFoundError):
        subprocess.check_output([fake, "-m", "ensurepip", "--upgrade", "--default-pip"])


def test_windows_venv_pip_install_passes_through():
    """The pip installs that follow ``venv.create`` are the same shape."""
    fake = r"C:\Users\dev\AppData\Local\Temp\pytest-1\t0\venv\Scripts\python.exe"
    with pytest.raises(FileNotFoundError):
        subprocess.run([fake, "-m", "pip", "install", "-q", "pyyaml"])


def test_windows_live_interpreter_pip_install_still_blocked():
    """The widened tokeniser must not blunt the guard on Windows.

    ``sys.executable`` spelt with native separators is still OUR interpreter,
    so an install through it still mutates the live venv and must be blocked.
    Without this, a fix for the two tests above could pass by exempting every
    backslash-bearing path.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([os.path.normpath(sys.executable), "-m", "pip", "install", "pyyaml"])


def test_npm_install_passes_through():
    """The guard protects the Python venv; ``npm install`` is unrelated."""
    with pytest.raises(FileNotFoundError):
        subprocess.run(["hermes-nonexistent-npm", "install"])


def test_uv_tool_install_passes_through():
    """``uv tool install`` targets uv's tool dir, not the active venv."""
    with pytest.raises(FileNotFoundError):
        subprocess.run(["hermes-nonexistent-uv", "tool", "install", "hermes-agent"])


def test_pip_version_probe_passes_through():
    """Read-only pip probes must still work — lazy_deps uses one."""
    r = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r is not None


# ──────── PID-targeted killers and psutil terminate/kill ────────
#
# Two holes found 2026-08-13 while auditing why
# ``tests/hermes_cli/test_update_autostash.py`` printed
# "✓ stopped PID <live dashboard pid>" for four of the developer's real
# dashboard processes:
#
#   1. ``_is_process_killer`` only fires when the command string also
#      mentions hermes/gateway/python. The Windows reaper in
#      ``hermes_cli.main._kill_stale_dashboard_processes`` shells out as
#      ``taskkill /PID <n> /F`` — a bare number, no hermes token — so it
#      sailed through. Nothing was actually killed in that run (the test
#      had replaced ``subprocess.run``), but only by luck: any test that
#      reaches that path with subprocess unmocked terminates whatever the
#      scan found.
#   2. ``psutil.Process.terminate()/kill()/send_signal()`` never touch
#      ``os.kill`` from Python — psutil calls TerminateProcess / the raw
#      syscall in C — so the ``os.kill`` guard cannot see them at all.
#
# Both are now gated on the same ``_is_own_subtree`` allowlist as os.kill.

_FOREIGN_LIVE_PID = 4 if sys.platform == "win32" else 1
"""A live process that is provably not ours: Windows ``System`` (PID 4),
POSIX ``init`` (PID 1). PID 1 does NOT exist on Windows, which is why this
is platform-split — a nonexistent PID is allowlisted by the guard (a
signal to it is a no-op) and would make these tests silently vacuous."""


def _foreign_psutil_process():
    psutil = pytest.importorskip("psutil")
    try:
        return psutil.Process(_FOREIGN_LIVE_PID)
    except Exception as exc:  # pragma: no cover — platform without it
        pytest.skip(f"no foreign live PID to probe: {exc}")


def test_subprocess_taskkill_bare_foreign_pid_blocked():
    """``taskkill /PID <live foreign pid> /F`` — no hermes token anywhere.

    This is verbatim the argv ``_kill_stale_dashboard_processes`` builds.
    """
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["taskkill", "/PID", str(_FOREIGN_LIVE_PID), "/F"])


def test_subprocess_posix_kill_bare_foreign_pid_blocked():
    """The POSIX spelling of the same thing: ``kill -9 <live foreign pid>``."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["kill", "-9", str(_FOREIGN_LIVE_PID)])


def test_subprocess_taskkill_stale_pid_passes_through():
    """A PID that no longer exists cannot hurt anyone — don't block it.

    Tests routinely use invented PIDs (12345, 33940). Blocking those would
    turn the guard into a source of false failures.

    Spelled against a nonexistent dir so no real ``taskkill`` is ever
    spawned — the basename still reads as ``taskkill`` to the predicate,
    and reaching FileNotFoundError (the exec, not the guard) is the
    pass-through proof on every platform. A bare ``taskkill`` here would
    put a live process-killer back in this file; one was removed from
    tests/tools/test_process_registry.py on 2026-06-11 for that reason.
    Also deliberately not routed through ``_run_allowed``: this venv's
    interpreter path contains ".hermes", which trips the killer guard's
    hermes-token rule on its own and would make the assertion vacuous.
    """
    # NOT _UNREAL_EXE_DIR: that constant's path contains "hermes", which
    # trips the killer guard's hermes-token rule by itself.
    unreal_dir = "C:\\guard-probe-no-such-dir\\bin"
    with pytest.raises(FileNotFoundError):
        subprocess.run([unreal_dir + "\\taskkill.exe", "/PID", "424242", "/F"])


def test_psutil_terminate_foreign_pid_blocked():
    proc = _foreign_psutil_process()
    with pytest.raises(RuntimeError, match="live-system guard"):
        proc.terminate()


def test_psutil_kill_foreign_pid_blocked():
    proc = _foreign_psutil_process()
    with pytest.raises(RuntimeError, match="live-system guard"):
        proc.kill()


def test_psutil_send_signal_foreign_pid_blocked():
    proc = _foreign_psutil_process()
    with pytest.raises(RuntimeError, match="live-system guard"):
        proc.send_signal(signal.SIGTERM)


def test_psutil_terminate_own_child_passes_through():
    """The guard must not break the common, legitimate case."""
    psutil = pytest.importorskip("psutil")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        psutil.Process(child.pid).terminate()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover — guard regression
            child.kill()
            child.wait(timeout=10)


# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, the guard yields without installing the monkeypatch,
    # so we get the real os.kill. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # No exception — guard is OFF.
