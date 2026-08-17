"""Tests for scripts/reap_stray_tests.py — the session-scoped stray-test reaper.

The regression this file exists for: on 2026-08-16 a concurrent Claude session ran
an ad-hoc psutil sweep that killed a SIBLING session's live `python -u -m pytest
.../tests/cron` run (exit 15, 3m43s into 1052 tests). Its only guard excluded
`os.getpid()` + `parents()`, which protects the sweeper's own subtree and nothing
else. Test 1 below is that exact scenario.

Process records are plain dicts so the logic is testable without a real process
tree: {pid, ppid, name, cmdline, create_time, rss}.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reap_stray_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("reap_stray_tests", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reap = _load()

NOW = 1_000_000.0
MIN = 60.0


def rec(pid, ppid, name, cmdline, age_min=10.0, rss=100 * 2**20):
    return {
        "pid": pid,
        "ppid": ppid,
        "name": name,
        "cmdline": list(cmdline),
        "create_time": NOW - age_min * MIN,
        "rss": rss,
    }


def two_session_box():
    """My session (100) and a sibling session (200), each with a pytest run.

    130 is MY stray pytest. 230 is the SIBLING's live run — the 2026-08-16 victim.
    120 is the reaper itself.
    """
    return [
        rec(100, 1, "claude.exe", ["claude.exe", "--output-format", "stream-json"], 120),
        rec(110, 100, "powershell.exe", ["powershell.exe", "-NoProfile", "-Command", "..."], 60),
        rec(120, 110, "python.exe", ["python", "scripts/reap_stray_tests.py"], 1),
        rec(130, 110, "python.exe", ["python", "-u", "-m", "pytest", "tests/tools", "-q"], 30),
        rec(200, 1, "claude.exe", ["claude.exe", "--output-format", "stream-json"], 120),
        rec(210, 200, "powershell.exe", ["powershell.exe", "-NoProfile", "-Command", "..."], 60),
        rec(230, 210, "python.exe",
            ["python", "-u", "-m", "pytest", r"C:\repo/tests/cron", "-q", "--timeout=300"], 4),
    ]


# ---------------------------------------------------------------- the regression

def test_sibling_sessions_pytest_run_is_never_killed():
    """THE regression: PID 230 belongs to another session and must survive."""
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW)
    assert 230 not in {v["pid"] for v in victims}


def test_own_session_stray_is_killed():
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW)
    assert {v["pid"] for v in victims} == {130}


def test_reaper_never_kills_itself_or_its_ancestors():
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW)
    pids = {v["pid"] for v in victims}
    assert pids.isdisjoint({120, 110, 100})


# ------------------------------------------------------- the self-match bug fix

def test_substring_in_a_single_argv_element_is_not_a_test_process():
    """The original sweep matched '-m pytest' in the JOINED cmdline, so its own
    `python -c "...-m pytest..."` source text matched and it killed itself."""
    assert reap.is_test_process(["python", "-c", "x = 'foo -m pytest bar'"]) is False


def test_dash_m_pytest_adjacency_is_a_test_process():
    assert reap.is_test_process(["python", "-u", "-m", "pytest", "tests/"]) is True


def test_dash_m_followed_by_something_else_is_not_a_test_process():
    assert reap.is_test_process(["python", "-m", "hermes_cli.main", "gateway", "run"]) is False


def test_run_tests_parallel_script_is_a_test_process():
    assert reap.is_test_process(["python", "scripts/run_tests_parallel.py", "--help"]) is True


# ------------------------------------------------------------------ default deny

def test_unresolvable_session_root_kills_nothing():
    """No claude.exe ancestor -> we cannot prove ownership -> reap nothing."""
    orphan = [
        rec(300, 1, "python.exe", ["python", "scripts/reap_stray_tests.py"], 1),
        rec(310, 1, "python.exe", ["python", "-m", "pytest", "tests/"], 30),
    ]
    victims, note = reap.build_plan(orphan, my_pid=300, now=NOW)
    assert victims == []
    assert "session root" in note.lower()


def test_electron_helper_is_not_a_session_root():
    """`--type=` marks an Electron helper, not a CLI session (cull-claude-sessions.py)."""
    assert reap.is_claude_session(
        rec(1, 0, "claude.exe", ["claude.exe", "--type=renderer", "claude-code"])
    ) is False


# --------------------------------------------------------- pid-reuse resistance

def test_child_older_than_its_parent_is_not_a_descendant():
    """A recycled/dangling ppid must not smuggle a foreign process into my subtree."""
    box = [
        rec(100, 1, "claude.exe", ["claude.exe", "--output-format", "stream-json"], 60),
        rec(120, 100, "python.exe", ["python", "scripts/reap_stray_tests.py"], 1),
        # ppid says 100, but it started BEFORE 100 existed -> ppid was recycled.
        rec(400, 100, "python.exe", ["python", "-m", "pytest", "tests/"], 900),
    ]
    victims, _ = reap.build_plan(box, my_pid=120, now=NOW)
    assert 400 not in {v["pid"] for v in victims}


# ------------------------------------------------------------------- the flags

def test_min_age_minutes_spares_young_processes():
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW, min_age_minutes=45)
    assert victims == []


def test_all_sessions_reaches_foreign_runs_and_attributes_them():
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW, all_sessions=True)
    by_pid = {v["pid"]: v for v in victims}
    assert set(by_pid) == {130, 230}
    assert by_pid[230]["session_root"] == 200
    assert by_pid[130]["session_root"] == 100


def test_plan_entries_carry_age_and_cmdline_for_the_printed_plan():
    victims, _ = reap.build_plan(two_session_box(), my_pid=120, now=NOW)
    v = victims[0]
    assert v["age_minutes"] == pytest.approx(30.0)
    assert "pytest" in " ".join(v["cmdline"])


# ------------------------------------------------ what main() PRINTS, and when
#
# Everything above tests build_plan, which decides. These test main(), which
# DISCLOSES -- and the disclosure is the whole safety argument for --all-sessions:
# `~/.claude/hooks/block-unscoped-process-kill.py` sends blocked agents here
# calling it a box-wide mode "that prints every victim with its owning session
# first". A warning that arrives after the kills would make that advice a lie
# while every test above still passed.

def unkillable_box():
    """`two_session_box()` with every pid negated.

    main() is driven for real below -- including the execution branch -- so the
    fake psutil is what should receive these pids. If that injection ever
    silently failed, real psutil.Process(-230) raises, where Process(230) would
    kill whatever pid 230 happens to be on this machine. No arrangement of these
    tests can kill a process.
    """
    box = []
    for record in two_session_box():
        record = dict(record)
        record["pid"] = -record["pid"]
        record["ppid"] = -record["ppid"]
        box.append(record)
    return box


def _fake_psutil(seen_pids: list) -> types.ModuleType:
    """A psutil stand-in for main()'s execution branch.

    main() does `import psutil` INSIDE the function, so putting this in
    sys.modules makes the real one unreachable -- which is what lets these tests
    exercise the kill branch at all.
    """
    module = types.ModuleType("psutil")

    class TimeoutExpired(Exception):
        pass

    class Process:
        def __init__(self, pid):
            seen_pids.append(pid)

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    module.TimeoutExpired = TimeoutExpired
    module.Process = Process
    return module


def _drive_main(monkeypatch, argv, my_pid=-120):
    """Run main() over `unkillable_box()` with a fixed clock and a fake psutil.

    build_plan stays REAL -- only its `my_pid`/`now` are pinned, so the plan the
    printing is asserted against is the one the logic above actually produces.
    Returns (stdout, exit_code, pids_the_fake_psutil_was_asked_to_kill, kwargs).
    """
    killed: list = []
    forwarded: dict = {}
    real_build_plan = reap.build_plan

    def _plan(records, *, my_pid=None, now=None, **kwargs):
        forwarded.update(kwargs)
        return real_build_plan(records, my_pid=pinned_pid, now=NOW, **kwargs)

    pinned_pid = my_pid
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(killed))
    monkeypatch.setattr(reap, "snapshot", lambda _psutil: unkillable_box())
    monkeypatch.setattr(reap, "build_plan", _plan)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = reap.main(argv)
    return out.getvalue(), code, killed, forwarded


def test_all_sessions_warns_and_names_every_owner_before_it_kills(monkeypatch):
    printed, code, killed, forwarded = _drive_main(monkeypatch, ["--all-sessions"])

    assert code == 0
    assert forwarded.get("all_sessions") is True, (
        f"--all-sessions never reached the planner: {forwarded}"
    )
    # Positive control: the execution branch really ran, so "the warning came
    # first" is not trivially true of a run that never acted.
    assert set(killed) == {-130, -230}, f"expected both strays reaped, got {killed}"

    warning = printed.find("WARNING")
    mine = printed.find("session=-100")
    foreign = printed.find("session=-200")
    executing = printed.find("=== EXECUTING ===")
    assert warning != -1, f"box-wide reap ran with no warning:\n{printed}"
    assert mine != -1 and foreign != -1, f"a victim was printed without its owner:\n{printed}"
    assert executing != -1, f"cannot locate the execution phase:\n{printed}"
    assert warning < executing, f"warning printed AFTER killing started:\n{printed}"
    assert max(mine, foreign) < executing, f"victims disclosed AFTER killing started:\n{printed}"


def test_scoped_run_does_not_warn_about_cross_session_kills(monkeypatch):
    """The warning has to be specific to --all-sessions.

    A reaper that warns on every run trains the caller to page past it, which
    costs exactly as much as not warning at all on the run that matters.
    """
    printed, code, killed, _ = _drive_main(monkeypatch, ["--dry-run"])

    assert code == 0
    # Not vacuous: this run really did plan a kill, it just planned only my own.
    assert "session=-100" in printed, f"scoped run found nothing to plan:\n{printed}"
    assert "-230" not in printed, f"scoped run listed the sibling's live run:\n{printed}"
    assert "WARNING" not in printed, (
        f"scoped run warns about cross-session kills it cannot do:\n{printed}"
    )
    assert killed == [], f"--dry-run reached the kill path: {killed}"
