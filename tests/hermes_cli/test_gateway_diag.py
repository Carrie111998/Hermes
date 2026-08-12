"""Tests for hermes_cli.gateway_diag — the gateway lifecycle diagnostic log.

The invariant under test is *when* records are written, not what they say. A
``gateway.start`` record that arrives after the expensive startup work is
useless for its actual purpose: detecting a double-spawn whose losing racer
dies during that very work (2026-08-10, PID 54392 — killed by the duplicate
guard, no trace in any log).
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import gateway_diag


@pytest.fixture(autouse=True)
def _clear_spawn_sentinel(monkeypatch):
    """Drop the once-per-process sentinel between tests.

    It lives in os.environ (see SPAWN_LOGGED_PID_VAR), which is process-wide,
    so without this the first test to log would silence every later one.
    """
    monkeypatch.delenv(gateway_diag.SPAWN_LOGGED_PID_VAR, raising=False)


def _records():
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "logs" / gateway_diag.DIAG_LOG_NAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["hermes", "gateway", "run"], True),
        (["hermes", "gateway"], True),  # bare `gateway` defaults to run
        (["hermes", "gateway", "run", "--replace"], True),
        (["hermes", "--profile", "main", "gateway", "run"], True),
        (["hermes", "gateway", "-v", "run"], True),
        # The detached Windows launcher's real argv.
        (["main.py", "--profile", "main", "gateway", "run"], True),
        (["hermes", "gateway", "status"], False),
        (["hermes", "gateway", "restart"], False),
        (["hermes", "gateway", "stop"], False),
        (["hermes", "gateway", "install", "--start-now"], False),
        (["hermes", "chat"], False),
        (["pytest", "tests/gateway/test_run.py"], False),
        (["hermes"], False),
        ([], False),
    ],
)
def test_argv_selects_gateway_run(argv, expected):
    assert gateway_diag.argv_selects_gateway_run(argv) is expected


def test_emit_gateway_spawn_diag_writes_for_a_gateway_run():
    argv = ["hermes", "--profile", "main", "gateway", "run"]

    assert gateway_diag.emit_gateway_spawn_diag(argv) is True

    records = _records()
    assert len(records) == 1
    record = records[0]
    assert record["tag"] == "gateway.spawn"
    assert record["pid"] == os.getpid()
    # The pre-strip argv: `_apply_profile_override()` removes `--profile` from
    # sys.argv before this runs, so reading sys.argv here would silently drop
    # it and misreport how the process was launched.
    assert record["argv"] == argv
    assert record["python"] == sys.version.split()[0]


def test_emit_gateway_spawn_diag_is_once_per_process(monkeypatch):
    """A re-executed module body must not fake a second spawn.

    Under ``python -m hermes_cli.main`` the module body runs twice — once as
    ``__main__``, once when something imports ``hermes_cli.main`` by name — and
    those are separate module objects, so a module-global guard cannot see the
    first pass. Observed live: PID 40772 logged spawn at proc_age_s=12.5 and
    again at 144.9.
    """
    monkeypatch.delenv(gateway_diag.SPAWN_LOGGED_PID_VAR, raising=False)
    argv = ["hermes", "gateway", "run"]

    assert gateway_diag.emit_gateway_spawn_diag(argv) is True
    assert gateway_diag.emit_gateway_spawn_diag(argv) is False
    assert gateway_diag.emit_gateway_spawn_diag(argv) is False

    assert [r["tag"] for r in _records()] == ["gateway.spawn"]


def test_spawn_suppression_does_not_leak_to_a_child_process(monkeypatch):
    """Inheriting the sentinel must not silence a freshly spawned gateway.

    ``gateway restart`` spawns the new gateway as a child, so it inherits this
    env var. The child is exactly the process whose spawn we most need
    recorded — hence a PID comparison rather than a bare flag.
    """
    # Simulate inheriting a parent's sentinel: same var, different PID.
    monkeypatch.setenv(gateway_diag.SPAWN_LOGGED_PID_VAR, str(os.getpid() + 1))

    assert gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "run"]) is True
    assert [r["tag"] for r in _records()] == ["gateway.spawn"]


def test_emit_gateway_spawn_diag_stays_silent_for_other_commands():
    assert gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "status"]) is False
    assert _records() == []


def test_spawn_tag_is_distinct_from_start_tag():
    """The double-spawn signature is a PAIR of ``gateway.start`` records.

    A ``gateway.spawn`` record must never be mistakable for one, or the early
    record would manufacture the exact false positive it exists to catch.
    """
    gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "run"])
    gateway_diag.write_diag("gateway.start", replace=False)

    tags = [r["tag"] for r in _records()]
    assert tags == ["gateway.spawn", "gateway.start"]
    assert tags.count("gateway.start") == 1


def test_diag_opt_out_silences_every_record(monkeypatch):
    monkeypatch.setenv(gateway_diag.DIAG_ENV_VAR, "0")

    assert gateway_diag.diag_enabled() is False
    gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "run"])
    gateway_diag.write_diag("gateway.start")

    assert _records() == []


def test_write_diag_never_raises(monkeypatch):
    """A diagnostic must not be able to take the gateway down with it."""

    def exploding_home():
        raise RuntimeError("no home for you")

    monkeypatch.setattr("hermes_constants.get_hermes_home", exploding_home)

    gateway_diag.write_diag("gateway.start")  # must not raise


def test_process_start_age_is_a_small_positive_number():
    age = gateway_diag.process_start_age_s()

    if age is None:  # psutil unavailable — documented degradation
        pytest.skip("psutil not available")
    assert age >= 0
    # Sanity bound: this test process cannot have been alive for a day.
    assert age < 86_400


# ── late path resolution / test-isolation leak ───────────────────────────────
# A record must land in the home that was current when its writer was *bound*,
# not the one current when it finally fires. The gap between those two moments
# is what let the test suite write into production (see the subprocess test
# below for the full mechanism).


def test_write_diag_honors_a_log_dir_captured_earlier(tmp_path, monkeypatch):
    """A captured log dir must win over whatever HERMES_HOME says at write time."""
    captured_home = tmp_path / "captured"
    captured_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(captured_home))
    log_dir = gateway_diag.resolve_log_dir()

    later_home = tmp_path / "later"
    later_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(later_home))

    gateway_diag.write_diag("atexit.hook", log_dir=log_dir)

    assert (captured_home / "logs" / gateway_diag.DIAG_LOG_NAME).exists()
    assert not (later_home / "logs").exists()


def test_captured_log_dir_is_not_written_once_its_home_is_gone(tmp_path, monkeypatch):
    """A vanished home must not be recreated just to hold a diagnostic.

    pytest deletes its tmp dirs, so a hook bound to one would otherwise
    resurrect the directory at interpreter exit purely to write a record
    nobody will ever read.
    """
    captured_home = tmp_path / "captured"
    captured_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(captured_home))
    log_dir = gateway_diag.resolve_log_dir()

    shutil.rmtree(captured_home)

    gateway_diag.write_diag("atexit.hook", log_dir=log_dir)  # must not raise

    assert not captured_home.exists()


def test_register_exit_hook_binds_the_home_that_is_current_now(tmp_path, monkeypatch):
    """``run_gateway``'s registration must capture the home, not defer it.

    This is the leak's actual site: the hook is registered deep inside
    ``run_gateway``, and everything that makes it dangerous happens later.
    """
    hooks = []
    monkeypatch.setattr(gateway_diag.atexit, "register", lambda fn: hooks.append(fn) or fn)

    registration_home = tmp_path / "registration"
    registration_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(registration_home))

    gateway_diag.register_exit_hook()

    restored_home = tmp_path / "restored"
    restored_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(restored_home))

    assert len(hooks) == 1
    hooks[0]()  # the interpreter firing the hook, post-teardown

    assert (registration_home / "logs" / gateway_diag.DIAG_LOG_NAME).exists()
    assert not (restored_home / "logs").exists()


def test_a_bound_hook_never_writes_to_a_restored_hermes_home(tmp_path):
    """The regression guard for the 2026-08-11 production-log pollution.

    The suite reaches ``run_gateway`` far enough to register its atexit hook
    while HERMES_HOME points at a tmp dir. The hook then fires at *interpreter
    exit* — after monkeypatch teardown has restored the real HERMES_HOME — so a
    late-resolved path sent 7 ``atexit.hook`` records from a pytest PID into the
    live ``~/.hermes/profiles/main/logs`` log, corrupting the primary artifact
    of the gateway double-spawn investigation.

    Runs in a subprocess because that is the only way to exercise real atexit
    ordering: the hook must genuinely outlive the env restore.
    """
    registration_home = tmp_path / "registration"
    restored_home = tmp_path / "restored"
    registration_home.mkdir()
    restored_home.mkdir()

    repo_root = Path(gateway_diag.__file__).resolve().parents[1]
    script = tmp_path / "exiting_process.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import atexit, os, sys
            sys.path.insert(0, {str(repo_root)!r})
            from hermes_cli import gateway_diag

            # Test body: HERMES_HOME is the per-test tmp dir.
            os.environ["HERMES_HOME"] = {str(registration_home)!r}
            log_dir = gateway_diag.resolve_log_dir()
            atexit.register(
                lambda: gateway_diag.write_diag("atexit.hook", log_dir=log_dir)
            )

            # monkeypatch teardown: the real HERMES_HOME comes back.
            os.environ["HERMES_HOME"] = {str(restored_home)!r}
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert not (restored_home / "logs" / gateway_diag.DIAG_LOG_NAME).exists(), (
        "atexit hook wrote into the RESTORED home — this is the production leak"
    )
    records = [
        json.loads(line)
        for line in (registration_home / "logs" / gateway_diag.DIAG_LOG_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [r["tag"] for r in records] == ["atexit.hook"]
