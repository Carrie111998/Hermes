"""Verify scripts/run_tests_parallel.py kills test-spawned grandchildren.

Setup
-----
A test in this file spawns a long-lived Python grandchild that writes
its PID + a nonce to a tempfile, then exits without cleaning up.
With the old ``subprocess.run`` runner, that grandchild would orphan
and outlive the test (and the whole runner). With the current Popen +
``start_new_session`` + ``_kill_tree`` runner, the grandchild gets
SIGKILL'd via process-group kill when its file's pytest exits.

The leaker test always passes — its only job is to spawn a grandchild
and walk away. The verifier runs the runner over the leaker file in a
subprocess, then waits for the grandchild PID to disappear from the
kernel's process table.

POSIX-only: Windows has its own grandchild lifecycle (no shared session,
``taskkill /F /T`` semantics). Marked accordingly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import run_text_capture
from scripts import run_tests_parallel
from tests.timeout_budget import scaled


# ── Child deadlines ──────────────────────────────────────────────────────────
#
# Spawns in this file come in two depths, and they need differently sized
# bounds. Both kinds are safety nets against a *wedged* child, not assertions
# about how fast it is — no bound below is asserted on — so both are sized
# generously and scaled by ``HERMES_TEST_TIMEOUT_SCALE``. See
# ``tests/timeout_budget.py`` for the rationale, and note the one rule that
# governs all of them: a deadline that IS the assertion (the 2.0s poll-round
# sleep, the ``elapsed <`` bounds in the host-saturation tests) stays small
# and unscaled, because scaling it would weaken the claim.
#
# Two generations — ``spawns_runner``
# -----------------------------------
# We launch ``scripts/run_tests_parallel.py``, which launches one
# ``python -m pytest`` worker per probe file. Idle that costs a few seconds;
# under memory/CPU pressure on a loaded box, several times that.
#
# ``_FILE_TIMEOUT`` is the runner's own per-file deadline for the nested
# pytest. It stays well below ``_RUNNER_TIMEOUT`` so a wedged *file* is
# reported by the runner (which is the behaviour under test) instead of the
# whole runner being killed out from under it.
#
# The three bounds are deliberately ordered
# ``_FILE_TIMEOUT < _RUNNER_TIMEOUT < _TEST_TIMEOUT`` so a stall is reported
# by the innermost layer that can explain it, and the pytest-level cap never
# fires first (a pytest-timeout kill takes the summary line with it).
# ``_TEST_TIMEOUT`` overrides the global ``--timeout=30`` from pyproject.toml,
# which cannot bound a nested pytest run.
_FILE_TIMEOUT_SECONDS = scaled(120.0)
_FILE_TIMEOUT = str(int(_FILE_TIMEOUT_SECONDS))
_RUNNER_TIMEOUT = _FILE_TIMEOUT_SECONDS * 2
_TEST_TIMEOUT = _RUNNER_TIMEOUT + scaled(60.0)

spawns_runner = pytest.mark.timeout(_TEST_TIMEOUT)

# One generation — ``spawns_child``
# ---------------------------------
# The host-saturation guards below spawn a bare ``python -c`` probe (or a
# waiter thread), not the runner, so they get a much smaller cap than
# ``spawns_runner``. Their cost is dominated by interpreter start plus the
# ``scripts.run_tests_parallel`` import, which is cheap idle and slow under
# commit pressure — i.e. exactly the condition these tests exist to prevent,
# so they must tolerate it rather than fail on it.
#
# The same ordering rule applies: every inner bound fits inside
# ``_CHILD_TEST_TIMEOUT``, and ``_CHILD_TEST_TIMEOUT`` exceeds the global
# ``--timeout=30``. Without the mark, that global cap fires first and a
# thread-method kill takes the summary line with it — leaving no record of
# which test died.
_CHILD_TIMEOUT = scaled(60.0)
_CHILD_REAP_TIMEOUT = scaled(30.0)
_CHILD_TEST_TIMEOUT = _CHILD_TIMEOUT + (_CHILD_REAP_TIMEOUT * 2)

spawns_child = pytest.mark.timeout(_CHILD_TEST_TIMEOUT)


def _rootdir_flag(probe_root: Path) -> str:
    """Confine the nested pytest's collection tree to the probe directory.

    Every probe below lives under ``tmp_path`` — i.e. under the shared
    ``%TEMP%`` — while the runner launches pytest with ``cwd=repo_root``. The
    argument therefore sits OUTSIDE the rootdir pytest derives from that cwd,
    so pytest builds ``Dir`` collectors for the whole chain from the common
    ancestor down: ``C:\\Users`` → ``diego`` → ``AppData`` → ``Local`` →
    ``Temp`` → the probe dir. Collecting those directories walks the entire
    user profile.

    Two consequences, both measured on this host:

    * Collection of a two-line probe takes ~33s instead of ~0.01s — which is
      most of what the 60s child bounds used to be spent on, and why they
      tripped under load.
    * Enumerating ``%TEMP%`` races every other process that creates and
      deletes temp dirs there, so collection dies with
      ``FileNotFoundError: [WinError 2] ... 'C:\\...\\Temp\\<vanished-dir>'``
      before any test runs. Reproduced twice, naming a different vanished
      directory each time.

    Pinning ``--rootdir`` to the probe directory bounds the tree at the probe
    itself. It changes only nodeid display, which nothing here asserts on.
    """
    return f"--rootdir={probe_root}"


# Both tests share the same handoff file: the leaker writes here, the
# verifier reads here. We park it in $TMPDIR with a unique-per-run name
# so concurrent invocations of the suite don't clobber each other.
_HANDOFF_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "hermes-isolation-probe"
_HANDOFF_DIR.mkdir(exist_ok=True)


def test_canonical_runner_provides_a_resolvable_home() -> None:
    """The wrapper's clean environment must still support ``Path.home()``."""
    home = Path.home()

    assert home.is_absolute()
    assert home.exists()


def test_canonical_runner_forces_utf8_through_native_python_children() -> None:
    """The clean runner must make Unicode deterministic for its process tree."""
    assert os.environ.get("PYTHONUTF8") == "1"
    assert sys.flags.utf8_mode == 1

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('\\u23f5\\u2713'); sys.stdout.flush()",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert child.returncode == 0, child.stderr.decode("utf-8", errors="replace")
    assert child.stdout == "\u23f5\u2713".encode("utf-8")


def _handoff_path_for(nonce: str) -> Path:
    return _HANDOFF_DIR / f"grandchild-{nonce}.json"


def _pid_alive(pid: int) -> bool:
    """POSIX: send signal 0 to probe whether ``pid`` is still alive.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the process is
    gone, ``PermissionError`` if it exists but we can't signal it
    (someone else's pid). We treat PermissionError as "alive" because
    the process exists and that's all we need to know.
    """
    if sys.platform == "win32":  # pragma: no cover — POSIX-only test
        # On Windows we'd use OpenProcess + GetExitCodeProcess; this
        # test is skipped on Windows so the path is unreachable.
        raise RuntimeError("_pid_alive POSIX-only")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only probe")
@pytest.mark.live_system_guard_bypass
@spawns_runner
def test_grandchild_leak_is_killed_by_runner(tmp_path: Path) -> None:
    """Run the parallel runner over a probe file and verify cleanup.

    1. Materialize a probe file that spawns a long-lived grandchild and
       writes its PID to disk before exiting.
    2. Invoke ``scripts/run_tests_parallel.py`` against the probe file.
    3. Wait for the grandchild PID to vanish (poll for ~5s).
    4. Assert the runner exited cleanly AND the grandchild is dead.
    """
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    assert runner.exists(), f"runner missing at {runner}"

    # Probe lives in a temp dir, NOT under tests/, so the regular suite
    # never picks it up — only our explicit invocation does.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_probe_leaker.py"
    nonce = f"{os.getpid()}-{int(time.time() * 1000)}"
    handoff = _handoff_path_for(nonce)
    if handoff.exists():
        handoff.unlink()

    probe_src = textwrap.dedent(f"""
        import json, os, subprocess, sys, time
        from pathlib import Path

        HANDOFF = Path({str(handoff)!r})

        def test_spawns_grandchild_and_walks_away():
            # Long-lived grandchild: detached, ignores SIGTERM (we want
            # SIGKILL or process-group kill to be the only thing that
            # works, simulating a misbehaving server).
            child = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import os, signal, sys, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "sys.stdout.write(f'gc-pgid={{os.getpgid(0)}} gc-pid={{os.getpid()}}\\\\n'); "
                    "sys.stdout.flush(); "
                    "time.sleep(600)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # IMPORTANT: do NOT pass start_new_session here. We want
                # the grandchild to inherit the pytest subprocess's
                # process group, so when the runner kills the group the
                # grandchild dies too.
            )
            # Read the first line so we can record gc's pgid in the
            # handoff, then walk away — don't close the pipe (would
            # signal EOF and let the child see SIGPIPE on next write).
            first_line = child.stdout.readline().decode().strip()
            HANDOFF.write_text(json.dumps({{
                "pid": child.pid,
                "diag": first_line,
                "test_pid": os.getpid(),
                "test_pgid": os.getpgid(0),
            }}))
            assert child.pid > 0
    """).strip()
    probe.write_text(probe_src + "\n")

    # Run the parallel runner against just the probe file. The runner
    # discovers under ``tests/`` by default, so we override via --paths.
    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--paths",
            str(probe_dir),
            "-j",
            "1",
            # Bounded per-file timeout: the probe finishes in <1s, no
            # need for the runner's 10min default.
            "--file-timeout",
            _FILE_TIMEOUT,
            _rootdir_flag(probe_dir),
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=_RUNNER_TIMEOUT,
    )

    assert handoff.exists(), (
        f"probe never wrote handoff file; runner output:\n{proc.stdout}"
    )
    handoff_data = json.loads(handoff.read_text())
    grandchild_pid = handoff_data["pid"]
    diag = handoff_data.get("diag", "(no diag)")
    test_pid = handoff_data.get("test_pid")
    test_pgid = handoff_data.get("test_pgid")
    handoff.unlink()

    # The runner must have exited cleanly (probe test passes).
    assert proc.returncode == 0, (
        f"runner exited {proc.returncode}; output:\n{proc.stdout}"
    )

    # The grandchild must be gone. Poll for a bit because process-group
    # SIGKILL + reaping isn't synchronous; on a loaded box it can take
    # a beat.
    deadline = time.monotonic() + scaled(5.0)
    while time.monotonic() < deadline:
        if not _pid_alive(grandchild_pid):
            break
        time.sleep(0.05)
    else:
        # Test cleanup: kill the leaked grandchild ourselves so a
        # FAILED assertion doesn't leave a sleep(600) running.
        try:
            os.kill(grandchild_pid, 9)
        except ProcessLookupError:
            pass
        pytest.fail(
            f"grandchild PID {grandchild_pid} survived runner exit; "
            f"diag={diag!r} test_pid={test_pid} test_pgid={test_pgid}; "
            f"runner output:\n{proc.stdout}"
        )


# ── Bare pytest-flag passthrough ─────────────────────────────────────────────
#
# The runner routes any token starting with ``-`` that isn't one of its own
# options (``-j``/``--jobs``, ``--paths``, ``--slice``, ``--file-timeout``,
# ``--generate-slices``, ``--files``, ``--include-integration``) straight
# through to each per-file pytest invocation — no ``--`` separator required.
# Before this, a bare ``-q`` errored out with "unrecognized arguments",
# forcing a retry on every run. These tests are behavior contracts, not
# snapshots: they assert that bare flags reach pytest and that value-taking
# flags (``-k expr``) keep their value instead of having it stolen by the
# positional-path discovery.


def _make_probe_dir(tmp_path: Path) -> Path:
    """Two trivial passing tests, one named test_alpha, one test_beta."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_flagprobe.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n"
    )
    return probe_dir


def _merged(r: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """Re-merge stderr into stdout, the way ``stderr=subprocess.STDOUT`` did.

    ``run_text_capture`` captures the two streams into separate temp files, so
    callers asserting on a combined transcript get it back here rather than at
    every assertion site.
    """
    return subprocess.CompletedProcess(
        r.args, r.returncode, (r.stdout or "") + (r.stderr or ""), "",
    )


def _run_runner(probe_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    # run_text_capture, not stdout=PIPE: the runner spawns a pytest worker per
    # file, so every worker is a grandchild of this call and inherits the
    # capture pipe handles. A worker that outlives the runner holds the write
    # end open, the pipe never reaches EOF, and the timeout below stops
    # bounding anything — subprocess.run kills only the runner and then blocks
    # re-draining. That this runner leaks grandchildren is not hypothetical:
    # test_grandchild_leak_is_killed_by_runner exists to assert it reaps them,
    # and that test is POSIX-only because the reaping is killpg-based — so
    # Windows has no such guarantee and is exactly where the hang lands.
    return _merged(run_text_capture(
        [sys.executable, str(runner), "--paths", str(probe_dir),
         "-j", "1", "--file-timeout", _FILE_TIMEOUT,
         _rootdir_flag(probe_dir), *extra],
        cwd=repo_root,
        timeout=_RUNNER_TIMEOUT,
    ))


@spawns_runner
def test_bare_q_flag_passes_through(tmp_path: Path) -> None:
    """A bare ``-q`` (no ``--``) runs clean instead of erroring out."""
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-q")
    assert proc.returncode == 0, proc.stdout
    assert "unrecognized arguments" not in proc.stdout


@spawns_runner
def test_bare_value_flag_keeps_its_value(tmp_path: Path) -> None:
    """``-k test_alpha`` reaches pytest as a selector, not as a path.

    The value token (``test_alpha``) must NOT be swallowed by the runner's
    positional-path discovery — if it were, discovery would look for a path
    named ``test_alpha``, find nothing, and the run would degrade. We assert
    the run succeeds AND only one of the two tests was selected (proving the
    ``-k`` filter actually applied inside pytest).
    """
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-k", "test_alpha")
    assert proc.returncode == 0, proc.stdout
    # Exactly one test selected: the per-file summary shows "1✓" (1 passed).
    # test_beta is deselected by the -k filter.
    assert "1✓" in proc.stdout or "1 passed" in proc.stdout, proc.stdout
    assert "2✓" not in proc.stdout, (
        f"both tests ran — -k filter did not apply:\n{proc.stdout}"
    )


@spawns_runner
def test_explicit_double_dash_still_works(tmp_path: Path) -> None:
    """The legacy ``--`` separator keeps working alongside bare flags."""
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-q", "--", "--tb=short")
    assert proc.returncode == 0, proc.stdout
    assert "unrecognized arguments" not in proc.stdout


@spawns_runner
def test_positional_path_not_treated_as_flag(tmp_path: Path) -> None:
    """A positional path arg still overrides discovery (not routed to pytest)."""
    probe_dir = _make_probe_dir(tmp_path)
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    # Pass the probe dir positionally (no --paths), plus a bare -q.
    # run_text_capture: pytest workers are grandchildren here — see _run_runner.
    proc = _merged(run_text_capture(
        [sys.executable, str(runner), str(probe_dir), "-j", "1",
         "--file-timeout", _FILE_TIMEOUT, _rootdir_flag(probe_dir), "-q"],
        cwd=repo_root, timeout=_RUNNER_TIMEOUT,
    ))
    assert proc.returncode == 0, proc.stdout
    # Discovery found the probe file (2 tests), proving the positional path
    # was consumed as a root, not forwarded to pytest as a bad flag.
    assert "test_flagprobe.py" in proc.stdout, proc.stdout


# ── Per-file --basetemp isolation ────────────────────────────────────────────
#
# pytest's tmp_path fixture uses a shared per-user base
# (``<tmp>/pytest-of-<user>/pytest-<N>/``) with retention cleanup: it keeps
# the last 3 numbered dirs and rmtree's older ones. When N per-file pytest
# subprocesses run concurrently, one process's retention sweep deletes another
# live process's ``pytest-<N>`` mid-run, producing ``FileNotFoundError
# [WinError 3]`` teardown/setup errors that have nothing to do with the tests.
# The runner de-collides this by handing every per-file subprocess its own
# ``--basetemp`` under a single per-run root, so no numbered-dir retention race
# can exist.


def test_each_file_gets_a_unique_run_scoped_basetemp() -> None:
    """Distinct files — even with colliding basenames — get distinct basetemps
    under one shared per-run root (so cleanup is a single rmtree)."""
    repo_root = Path(__file__).resolve().parent.parent
    a = repo_root / "tests" / "gateway" / "test_status.py"
    b = repo_root / "tests" / "cron" / "test_status.py"  # same basename, diff dir

    bt_a = run_tests_parallel._basetemp_for(a, repo_root)
    bt_b = run_tests_parallel._basetemp_for(b, repo_root)

    # Unique per file even when basenames collide across directories.
    assert bt_a != bt_b, (bt_a, bt_b)
    # Both under a single per-run root, so end-of-run cleanup is one rmtree.
    assert bt_a.parent == bt_b.parent, (bt_a.parent, bt_b.parent)
    # Deterministic within a run (same file → same basetemp).
    assert run_tests_parallel._basetemp_for(a, repo_root) == bt_a


@spawns_runner
def test_basetemp_is_wired_into_the_pytest_subprocess(tmp_path: Path) -> None:
    """The unique basetemp actually reaches pytest — a probe reading its own
    ``tmp_path`` reports a dir under the runner's per-run basetemp root.

    Guards against the helper existing but never being threaded into the
    subprocess command (0-hits-armed == 0-hits-unwired)."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    handoff = tmp_path / "reported_basetemp.txt"
    probe = probe_dir / "test_basetemp_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            def test_reports_its_tmp_path(tmp_path):
                Path({str(handoff)!r}).write_text(str(tmp_path))
            """
        ),
        encoding="utf-8",
    )

    proc = _run_runner(probe_dir)

    assert proc.returncode == 0, proc.stdout
    assert handoff.exists(), f"probe never reported tmp_path:\n{proc.stdout}"
    reported = handoff.read_text().strip()
    # tmp_path == <basetemp>/test_reports_its_tmp_path0, and <basetemp> is our
    # per-run, per-file dir — so the marker appears in the path.
    assert "hermes-parallel" in reported, reported


def test_files_list_preserves_posix_and_relative_colon_syntax() -> None:
    """Colon-separated POSIX and relative file lists keep their existing grammar."""
    assert run_tests_parallel._split_file_list(
        "/tmp/test_one.py:/opt/tests/test_two.py:tests/test_three.py"
    ) == [
        "/tmp/test_one.py",
        "/opt/tests/test_two.py",
        "tests/test_three.py",
    ]


def test_files_list_preserves_windows_drive_colons() -> None:
    """Drive designators are path syntax, not separators between ``--files``."""
    assert run_tests_parallel._split_file_list(
        r"C:\temp\test_one.py:D:/work/tests/test_two.py"
    ) == [
        r"C:\temp\test_one.py",
        "D:/work/tests/test_two.py",
    ]


@spawns_runner
def test_file_retry_self_heals_and_prints_both_attempts(tmp_path: Path) -> None:
    """A pass-on-retry is green, loud, and retains the failing traceback."""
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    marker = tmp_path / "ran-once"
    probe = tmp_path / "test_flaky_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            def test_flaky_once():
                marker = Path({str(marker)!r})
                if not marker.exists():
                    marker.write_text("failed once")
                    assert False, "simulated first-attempt flake"
                assert True
            """
        ),
        encoding="utf-8",
    )

    # run_text_capture: pytest workers are grandchildren here, and the retry
    # path runs more than one generation of them — see _run_runner.
    proc = _merged(run_text_capture(
        [
            sys.executable,
            str(runner),
            "--files",
            str(probe),
            "--file-retries",
            "1",
            "-j",
            "1",
            _rootdir_flag(probe.parent),
            "-q",
        ],
        cwd=repo_root,
        timeout=_RUNNER_TIMEOUT,
    ))

    assert proc.returncode == 0, proc.stdout
    assert "FLAKY file" in proc.stdout
    assert "simulated first-attempt flake" in proc.stdout
    assert "first-attempt output" in proc.stdout
    assert "retry output" in proc.stdout


@spawns_runner
def test_file_retry_does_not_launder_deterministic_failure(tmp_path: Path) -> None:
    """A real regression fails both attempts and the runner remains red."""
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    probe = tmp_path / "test_red_probe.py"
    probe.write_text(
        "def test_always_red():\n    assert False, 'deterministic regression'\n",
        encoding="utf-8",
    )

    # run_text_capture: pytest workers are grandchildren here, and the retry
    # path runs more than one generation of them — see _run_runner.
    proc = _merged(run_text_capture(
        [
            sys.executable,
            str(runner),
            "--files",
            str(probe),
            "--file-retries",
            "1",
            "-j",
            "1",
            _rootdir_flag(probe.parent),
            "-q",
        ],
        cwd=repo_root,
        timeout=_RUNNER_TIMEOUT,
    ))

    assert proc.returncode == 1, proc.stdout
    assert "deterministic regression" in proc.stdout
    assert "FLAKY file" not in proc.stdout


# ── Host-saturation guards ──────────────────────────────────────────────
# Regression cover for the 2026-08-12 dashboard-starvation incident: ~50
# concurrent pytest subprocesses on a 12-core box drove commit charge to
# exhaustion, and a Hermes dashboard startup thrashed for 29 minutes
# without ever reaching its uvicorn bind. Three independent leaks let that
# happen, and each gets a test here:
#
#   1. the default worker count was cpu_count*2 (24 here), not the
#      cpu_count the module docstring advertised;
#   2. nothing coordinated ACROSS invocations, so two concurrent runs
#      stacked to ~48 workers;
#   3. nothing consulted memory pressure before spawning, even though
#      commit — not CPU — is what actually ran out.


def test_default_worker_count_does_not_oversubscribe_cores() -> None:
    """Default -j must never exceed the core count.

    The box has 12 cores; the historical ``cpu_count * 2`` default put 24
    pytest subprocesses in flight from a single invocation. Existing
    evidence is that -j 24 oversubscribes this host and the failure tail
    vanishes at -j 12.
    """
    cores = os.cpu_count() or 4
    assert run_tests_parallel._default_worker_count() <= cores


def test_default_worker_count_matches_documented_behaviour() -> None:
    """The docstring promised os.cpu_count(); the code must actually do it."""
    assert run_tests_parallel._default_worker_count() == (os.cpu_count() or 4)


def test_global_slot_capacity_is_bounded_by_cores() -> None:
    """The machine-global ceiling is the core count, not a multiple of it."""
    assert run_tests_parallel._global_slot_capacity() <= (os.cpu_count() or 4)


@spawns_child
def test_global_slots_are_exclusive_across_processes(tmp_path: Path) -> None:
    """A slot held by one PROCESS is unavailable to another.

    This is the property that makes the limiter cross-invocation: a
    thread-local semaphore would let two ``run_tests_parallel.py``
    invocations each spawn a full complement of workers.
    """
    slot_dir = tmp_path / "slots"
    # Hold the only slot in this process...
    with run_tests_parallel._acquire_global_slot(1, slot_dir):
        # ...and prove a separate process cannot also acquire it.
        probe = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(run_tests_parallel.__file__).parent.parent)!r})
            from scripts import run_tests_parallel as r
            got = r._try_acquire_any_slot(1, {str(slot_dir)!r})
            print("ACQUIRED" if got else "BLOCKED")
            """
        )
        out = run_text_capture(
            [sys.executable, "-c", probe], timeout=_CHILD_TIMEOUT
        )
        assert "BLOCKED" in out.stdout, out.stdout + out.stderr


@spawns_child
def test_slot_is_released_when_holder_dies(tmp_path: Path) -> None:
    """Slots must be reclaimed by the OS when a holder dies uncleanly.

    Without this the limiter would wedge the box permanently after any
    crashed or Ctrl-C'd run — a worse failure than the one it prevents.
    """
    slot_dir = tmp_path / "slots"
    grabber = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(run_tests_parallel.__file__).parent.parent)!r})
        from scripts import run_tests_parallel as r
        h = r._try_acquire_any_slot(1, {str(slot_dir)!r})
        print("HELD", flush=True)
        time.sleep(300)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", grabber],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        assert "HELD" in proc.stdout.readline()
        # Slot is taken while the holder lives.
        assert run_tests_parallel._try_acquire_any_slot(1, slot_dir) is None
    finally:
        proc.kill()
        proc.wait(timeout=_CHILD_REAP_TIMEOUT)

    # Killed without any cleanup path running — the OS must have dropped
    # the lock anyway. Poll briefly: Windows releases asynchronously.
    deadline = time.monotonic() + _CHILD_REAP_TIMEOUT
    handle = None
    while time.monotonic() < deadline:
        handle = run_tests_parallel._try_acquire_any_slot(1, slot_dir)
        if handle is not None:
            break
        time.sleep(0.1)
    assert handle is not None, "slot leaked after holder was killed"
    run_tests_parallel._release_slot(handle)


def test_commit_gate_reports_available_headroom() -> None:
    """The gate must read real commit headroom, not physical RAM.

    The incident was commit exhaustion with physical RAM still free, so a
    virtual_memory()-style reading would have shown nothing wrong.
    """
    avail = run_tests_parallel._available_commit_bytes()
    assert avail is None or avail > 0


def test_commit_gate_gives_up_rather_than_deadlocking() -> None:
    """An unsatisfiable memory threshold must time-box, not hang forever.

    If the box is genuinely out of commit for an unrelated reason, the
    runner has to make progress and complain — blocking indefinitely would
    turn a slow suite into a hung one.
    """
    started = time.monotonic()
    # Demand more commit than any machine has, with a tiny deadline.
    waited = run_tests_parallel._await_commit_headroom(
        min_free_bytes=2**60, deadline_seconds=1.0
    )
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"gate blocked {elapsed:.1f}s past its 1s deadline"
    assert waited is False, "gate must report that headroom was never reached"


@spawns_child
def test_waiting_for_a_contended_slot_survives_multiple_poll_rounds(
    tmp_path: Path,
) -> None:
    """The blocking wait must still be running after several poll cycles.

    The contention path is the one that matters and the one least likely
    to be exercised by a quick test: a limiter whose *acquire* path works
    but whose *wait* path raises would pass a naive test and then blow up
    only when two runs actually collide — the exact scenario it exists to
    handle. Hold the only slot, let a waiter poll through many rounds,
    then release and require a clean acquisition.
    """
    slot_dir = tmp_path / "slots"
    acquired: list[bool] = []
    failed: list[BaseException] = []

    holder = run_tests_parallel._acquire_global_slot(1, slot_dir)
    holder.__enter__()

    def _waiter() -> None:
        try:
            with run_tests_parallel._acquire_global_slot(1, slot_dir):
                acquired.append(True)
        except BaseException as exc:  # noqa: BLE001 — recording for assert
            failed.append(exc)

    import threading

    thread = threading.Thread(target=_waiter, daemon=True)
    thread.start()
    # Poll interval is 0.25s, so this is ~8 rounds through the wait loop.
    time.sleep(2.0)
    assert not failed, f"wait loop raised instead of waiting: {failed!r}"
    assert not acquired, "waiter acquired a slot that was still held"

    holder.__exit__(None, None, None)
    thread.join(timeout=_CHILD_REAP_TIMEOUT)
    assert not failed, f"wait loop raised after release: {failed!r}"
    assert acquired == [True], "waiter never acquired the released slot"


def test_unusable_slot_dir_degrades_to_unlimited_instead_of_spinning(
    tmp_path: Path,
) -> None:
    """An unlockable slot directory must not wedge the runner.

    'Cannot use slots at all' and 'every slot is busy' are different
    conditions with opposite correct responses — proceed vs wait. Collapsing
    them into a single ``None`` makes a read-only HOME (or any sandbox where
    the directory cannot be created) spin forever instead of degrading to
    the old unlimited behaviour, turning a graceful fallback into a hang.
    """
    # A regular file where the slot DIRECTORY should be — mkdir must fail.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    slot_dir = blocker / "slots"

    started = time.monotonic()
    with run_tests_parallel._acquire_global_slot(1, slot_dir):
        pass
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"spun {elapsed:.1f}s on an unusable slot dir"


from scripts.run_tests_parallel import _split_argv


def test_split_argv_routes_long_help_to_our_args():
    """--help must reach argparse, not pytest passthrough.

    Regression: any token starting with '-' that is not in _OUR_FLAGS is
    routed to pytest. --help was not in the set, so our_args ended up empty,
    discovery went unfiltered, and the runner started the FULL suite.
    """
    our, passthrough = _split_argv(["--help"])
    assert our == ["--help"]
    assert passthrough == []


def test_split_argv_routes_short_help_to_our_args():
    our, passthrough = _split_argv(["-h"])
    assert our == ["-h"]
    assert passthrough == []


def test_split_argv_still_routes_bare_pytest_flags():
    """The fix must not break deliberate bare-pytest-flag routing."""
    our, passthrough = _split_argv(["tests/foo.py", "-q", "-k", "expr"])
    assert our == ["tests/foo.py"]
    assert passthrough == ["-q", "-k", "expr"]


def test_split_argv_honours_explicit_separator():
    our, passthrough = _split_argv(["tests/foo.py", "--", "--tb=long"])
    assert our == ["tests/foo.py"]
    assert passthrough == ["--tb=long"]


def test_split_argv_keeps_our_own_flags():
    our, passthrough = _split_argv(["-j", "4", "--paths=tests/x", "tests/foo.py"])
    assert our == ["-j", "4", "--paths=tests/x", "tests/foo.py"]
    assert passthrough == []


def test_help_exits_without_starting_a_suite_run():
    """--help must exit 0 before discovery.

    Guarded by a short timeout on purpose: if this regresses, the failure
    mode is a full ~2384-file run, and a 30s bound keeps that from becoming
    a 12-worker stray run that needs a PID-tree kill.
    """
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_tests_parallel.py"), "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(repo_root),
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()
    assert "Discovered" not in proc.stdout


def _find_forwarding_loop_vars(body: str) -> "list[str] | None":
    """Every variable forwarded by any `for _var in ...; do` loop in run_tests.sh.

    Returns None if NO loop can be located, so a refactor that renames or
    restructures them fails loudly rather than silently passing.

    Scans ALL such loops, not just the first. That is not hypothetical
    tidiness: this helper originally used a single `re.search`, and when
    main grew a SECOND forwarding loop (`3bc7442b9b`, the Windows OS path
    vars) the search bound to that one instead and the junk-probe guard
    started reporting on the wrong list. It failed loudly, which is what it
    was built to do -- but the right fix is to stop caring which loop a
    variable lives in. Any of these loops appends to CLEAN_ENV, so
    membership in the union is exactly the property worth asserting.
    """
    matches = re.findall(r"for _var in (.*?); do", body, re.DOTALL)
    if not matches:
        return None
    names: "list[str]" = []
    for raw in matches:
        # Collapse backslash-newline continuations before splitting on
        # whitespace, so a multi-line variable list yields clean tokens.
        names.extend(re.sub(r"\\\s*\n", " ", raw).split())
    return names


def test_run_tests_sh_forwards_the_windows_os_path_vars():
    """SYSTEMDRIVE et al ARE forwarded now -- a deliberate reversal.

    This test previously asserted the OPPOSITE: that SYSTEMDRIVE must never
    enter the clean env, because `env -i` dropping it is the condition that
    makes a process expand `%SystemDrive%\ProgramData` as a RELATIVE path
    and build the junk tree under its cwd. Preserving that was deliberate --
    it kept a reproducer alive while the writer was unknown.

    Reversed on 2026-08-17 when a sibling session landed `3bc7442b9b`,
    forwarding the Windows OS path vars. Accepting theirs was the right call
    on three grounds:

    * The instruction to preserve the trap predates finding a writer. One was
      found and fixed (`run_secret_cli`), and `tests/cron` was cleared.
    * `run_tests.sh` CANNOT EXECUTE on this Windows box -- it probes POSIX
      `<venv>/bin/activate` and `HERMES_PYTHON`, neither of which exists here.
      So it was never the live reproducer locally, only a latent CI trap.
    * Decisively: the watcher this branch builds is RUNNER-AGNOSTIC. It
      watches the filesystem and catches any writer regardless of what a
      runner does to its environment. It no longer needs the bait.

    Guarding the forwarding now (rather than deleting the test) keeps the
    reversal deliberate: if these vars silently disappear again, that is a
    decision someone should have to make on purpose.
    """
    repo_root = Path(__file__).resolve().parent.parent
    body = (repo_root / "scripts" / "run_tests.sh").read_text(encoding="utf-8")

    loop_vars = _find_forwarding_loop_vars(body)
    assert loop_vars is not None, (
        "could not locate any `for _var in ...; do` forwarding loop in "
        "run_tests.sh -- cannot verify what reaches the clean env."
    )
    assert "SYSTEMDRIVE" in loop_vars, (
        "SYSTEMDRIVE is no longer forwarded. If that is intentional, this "
        "test and the comment above CLEAN_ENV both need updating -- and note "
        "it re-arms the %SystemDrive% junk-tree vector wherever run_tests.sh "
        "actually executes (CI)."
    )


def test_basetemp_root_is_reclaimed_when_the_runner_dies_without_finishing(
    tmp_path: Path,
) -> None:
    """An interrupted run must not strand its per-invocation basetemp root.

    ``_cleanup_basetemps()`` is called once, on the normal path, after every
    worker has exited. Nothing runs it when the process leaves by any other
    door — Ctrl-C, an unhandled exception, a file-timeout kill, or the
    host-saturation abort this runner has its own guards for. Each of those
    strands the whole ``hermes-parallel-*`` tree.

    Measured in %TEMP% on 2026-08-19: 60 stranded roots spanning 2.3 days,
    1526.3 MB, accumulating at roughly 26 per day. The shape matches this
    exit path exactly — 54 of the 60 were empty (the root is created lazily
    the moment the first file is scheduled, so a run that dies early leaves
    nothing but the root) and 6 held 76-374 MB from runs that died mid-flight.
    """

    script = tmp_path / "die_without_cleanup.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(run_tests_parallel.__file__).parent.parent)!r})\n"
        "from pathlib import Path\n"
        "from scripts import run_tests_parallel as r\n"
        # Create the root exactly the way a real run does, then leave by an
        # abnormal door without ever reaching _cleanup_basetemps().
        "bt = r._basetemp_for(Path('tests/x/test_a.py'), Path('.').resolve())\n"
        "print(r._BASETEMP_ROOT, flush=True)\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 3, proc.stderr
    root = Path(proc.stdout.strip().splitlines()[-1])
    assert root.name.startswith("hermes-parallel-"), root

    assert not root.exists(), (
        f"basetemp root {root} survived a run that exited without reaching "
        "_cleanup_basetemps() -- this is the %TEMP% leak"
    )
