"""Tests for hermes events doctor CLI diagnostic."""
import atexit
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from events.bus import EventBus
from events.schema import EventType
from hermes_cli.events_doctor import check_code_drift, print_dead_letters, run_doctor


def test_doctor_reports_missing_topics_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_AGENT_SRC", str(tmp_path / "no-repo"))
    (tmp_path / "events").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()

    rc = run_doctor(check_telegram_api=False)
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "FAIL" in captured or "missing" in captured.lower()
    assert rc != 0


def test_doctor_all_green_on_healthy_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_AGENT_SRC", str(tmp_path / "no-repo"))
    (tmp_path / "events").mkdir()
    (tmp_path / "telegram").mkdir()
    (tmp_path / "notifications").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()
    (tmp_path / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-1", "topics": {}}))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({}))
    (tmp_path / "notifications" / "quiet_hours.json").write_text(
        json.dumps({"enabled": True}))

    run_doctor(check_telegram_api=False)
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "quiet_hours.json" in captured


class TestDeadLettersFlag:
    """SR-109: `events_doctor --dead-letters` surface."""

    def _setup_bus_with_dead_letter(self, tmp_path):
        db = tmp_path / "events" / "event_bus.db"
        bus = EventBus(db_path=db)
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", eid, "KeyError: 'score'")
        bus.close()
        return db, eid

    def test_prints_message_when_empty(self, tmp_path, capsys):
        (tmp_path / "events").mkdir()
        db = tmp_path / "events" / "event_bus.db"
        # Create an empty DB with schema
        EventBus(db_path=db).close()

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "No dead-letter" in captured

    def test_prints_row_when_present(self, tmp_path, capsys):
        db, eid = self._setup_bus_with_dead_letter(tmp_path)

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "digest-composer" in captured
        assert "KeyError" in captured
        assert "cron_completed" in captured

    def test_limit_respected(self, tmp_path, capsys):
        db = tmp_path / "events" / "event_bus.db"
        bus = EventBus(db_path=db)
        for i in range(5):
            eid = bus.emit(EventType.CRON_COMPLETED, "scout", {"i": i})
            bus.record_dead_letter("sub", eid, f"err-{i}")
        bus.close()

        print_dead_letters(db_path=db, limit=2)
        captured = capsys.readouterr().out
        # 2 data lines + 2 header lines
        assert captured.count("sub ") == 2 or captured.count("sub\n") == 0  # tolerant
        # At most 2 error messages should appear
        errs = [ln for ln in captured.splitlines() if "err-" in ln]
        assert len(errs) == 2

    def test_missing_db_returns_nonzero(self, tmp_path, capsys):
        rc = print_dead_letters(db_path=tmp_path / "does-not-exist.db")
        assert rc == 1

    def test_missing_table_reports_migration_hint(self, tmp_path, capsys):
        """Old DB without dead_letters table should emit a hint, not crash."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE events (event_id TEXT)")
        conn.close()

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "dead_letters" in captured and "migrate" in captured.lower()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# Git-spawn budget.
#
# ``TestCodeDrift`` builds a real git repo per test, and on Windows every
# ``git`` spawn costs ~0.5s (``git init`` ~1.2s). At 16 spawns per test that
# put 11 of the 12 tests at 22-43s against the repo's 30s ``--timeout`` cap
# from pyproject -- and because ``--timeout-method=thread`` kills the
# interpreter, the first one over took the other 17 tests in the file with it.
# Measured with the cap lifted (``--timeout=600``): 23 passed in 402s, worst
# test 42.85s.
#
# The helpers below keep the same repo shapes and the same assertions, and only
# stop paying for spawns that produce information we can read off disk, or that
# can be paid once for the whole module instead of once per test:
#
#   * repo creation  4 spawns -> 0   (copy a once-built template)
#   * ``_commit``    3 spawns -> 2   (read HEAD instead of ``rev-parse``)
#                      -> 1 after the first (``commit -a``, no separate add)
#   * the whole per-test setup -> 0   (``_shape`` builds each distinct repo
#                                      shape once at import; tests copy it)
#
# The probe under test (``sample_code_drift``, 5 spawns) is untouched -- that
# is the behaviour these tests exist to cover, and it is now essentially all
# that is left inside the timed window.
#
# The last step matters specifically because the cap is PER TEST. Building the
# shapes at import does not reduce total work much -- it moves it into
# collection, which pytest-timeout does not cover -- but that is exactly the
# budget that was being blown. Per-test setup had to reach ~0 for the tests to
# survive `run_tests_parallel -j 8`, where every spawn costs several seconds.
# ---------------------------------------------------------------------------

def _build_repo_template():
    """One ``git init`` + identity config, copied per test.

    A plain ``git init`` embeds no absolute paths, so copying the directory is
    equivalent to re-running it. The identity is written straight into the
    repo's own ``.git/config`` rather than through three ``git config`` spawns;
    writing it into the repo (not HOME) keeps it independent of the conftest
    ``_hermetic_environment`` fixture, which repoints HOME per test.
    """
    base = Path(tempfile.mkdtemp(prefix="events-doctor-git-template-"))
    atexit.register(shutil.rmtree, base, True)
    repo = base / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "[user]\n\temail = t@test\n\tname = t\n"
        + "[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    return repo


# Built at import, NOT lazily on first use. `git init` is the single most
# expensive spawn here (~1.2s idle, several seconds on a loaded box), and a
# lazy build bills all of it to whichever test builds a repo first -- which is
# exactly what kept `test_in_sync_detached_head_is_ok` over the cap under
# parallel load after the other spawn cuts had landed. Module scope puts it in
# collection, which pytest-timeout does not cover.
_REPO_TEMPLATE = _build_repo_template()


def _head_sha(repo):
    """``git rev-parse HEAD`` without the spawn.

    Falls back to the real command if HEAD is anything other than a loose ref
    or a raw SHA, so a packed-refs repo cannot make this silently wrong.
    """
    head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    ref = repo / ".git" / head[len("ref: "):].strip()
    if ref.is_file():
        return ref.read_text(encoding="utf-8").strip()
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo, msg):
    f = repo / "f.txt"
    tracked = f.exists()
    f.write_text(f.read_text() + msg + "\n" if tracked else msg + "\n")
    if tracked:
        # ``f.txt`` is the only file these tests ever touch, so once it is
        # tracked ``commit -a`` covers it and the separate ``git add`` spawn
        # is pure cost. The first commit still needs ``add`` -- ``-a`` does
        # not stage a file git has never seen.
        assert _git(repo, "commit", "-a", "-m", msg).returncode == 0
    else:
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-m", msg).returncode == 0
    return _head_sha(repo)


# --- repo shapes, each built once at import and copied per test -------------
#
# A step is one of:
#   ("commit", msg)   commit msg, recording its sha
#   ("rename", name)  git branch -m main <name>
#   ("detach", None)  git checkout --detach          (tip)
#   ("detach", i)     git checkout --detach <sha i>  (i indexes recorded shas)
#
# Commit messages are part of the shape because some tests assert on them
# (e.g. the missed-tip subject in the LAGS remediation line), so shapes are
# only shared where every sharing test's assertions still hold.
_SHAPE_SPECS = {
    "in_sync": [("commit", "a"), ("commit", "b"), ("detach", None)],
    "lagging": [("commit", "first fix"),
                ("commit", "landed but not deployed"),
                ("detach", 0)],
    "ahead": [("commit", "a"), ("detach", None), ("commit", "unlanded work")],
    "diverged": [("commit", "a"), ("commit", "b on main"),
                 ("detach", 0), ("commit", "c detached")],
    "one_commit_detached": [("commit", "a"), ("detach", None)],
    "one_commit_on_main": [("commit", "a")],
    "master_no_main": [("commit", "a"), ("rename", "master")],
    "master_lagging": [("rename", "master"), ("commit", "first"),
                       ("commit", "landed on master, never deployed"),
                       ("detach", 0)],
    "master_in_sync": [("rename", "master"), ("commit", "a"),
                       ("detach", None)],
}


def _build_shape(steps):
    base = Path(tempfile.mkdtemp(prefix="events-doctor-git-shape-"))
    atexit.register(shutil.rmtree, base, True)
    repo = base / "repo"
    shutil.copytree(_REPO_TEMPLATE, repo)
    shas = []
    for kind, arg in steps:
        if kind == "commit":
            shas.append(_commit(repo, arg))
        elif kind == "rename":
            assert _git(repo, "branch", "-m", "main", arg).returncode == 0
        elif kind == "detach":
            target = ["--detach"] if arg is None else ["--detach", shas[arg]]
            assert _git(repo, "checkout", *target).returncode == 0
        else:  # pragma: no cover - guards a typo in _SHAPE_SPECS
            raise AssertionError(f"unknown shape step {kind!r}")
    return repo, shas


_SHAPES = {name: _build_shape(steps) for name, steps in _SHAPE_SPECS.items()}


def _shape(tmp_path, name):
    """Copy a prebuilt shape into ``tmp_path``; returns ``(repo, shas)``.

    The copy is a plain directory copy -- no git spawns -- so a test's setup
    costs nothing against the 30s per-test cap. Each test still gets its own
    private repo, so tests that mutate theirs (dirty trees, extra commits)
    cannot affect any other.
    """
    src, shas = _SHAPES[name]
    repo = tmp_path / "repo"
    shutil.copytree(src, repo)
    return repo, shas


class TestCodeDrift:
    """Detached working tree vs landed `main` — the 07-20 stale-deploy trap."""

    def test_in_sync_detached_head_is_ok(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "in_sync")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 0
        assert "[OK]" in out and "in sync" in out

    def test_lagging_head_warns_with_count_and_remediation(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "lagging")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out
        assert "LAGS" in out
        assert "1 commit" in out
        assert "landed but not deployed" in out  # missed tip subject listed
        assert "merge --ff-only main" in out  # remediation hint
        assert "restart the gateway" in out

    def test_ahead_head_warns_unlanded(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "ahead")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out
        assert "AHEAD" in out and "unlanded" in out.lower()

    def test_diverged_head_warns(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "diverged")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out and "DIVERGED" in out

    def test_dirty_tree_noted_but_not_counted(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "one_commit_detached")
        (repo / "scratch.txt").write_text("uncommitted")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 0  # dirty alone is a note, not a failure
        assert "DIRTY" in out

    def test_missing_repo_skips_without_issue(self, tmp_path, capsys):
        issues = check_code_drift(repo_path=tmp_path / "does-not-exist")
        out = capsys.readouterr().out
        assert issues == 0
        assert "skip" in out.lower()

    def test_missing_configured_trunk_ref_is_loud_not_a_silent_pass(
        self, tmp_path, capsys
    ):
        """THE 2026-07-28 DEFECT, inverted.

        A repo that is PRESENT but whose configured trunk ref does not exist
        must FAIL, not skip. Until 2026-07-28 this returned 0 with a "skip"
        note, which is why pointing the checker at ~/.hermes (trunk
        `master`, no `main` branch) would have reported a clean bill of
        health forever instead of erroring.
        """
        repo, _ = _shape(tmp_path, "master_no_main")   # no `main` ref left

        issues = check_code_drift(repo_path=repo, trunk_ref="refs/heads/main")
        out = capsys.readouterr().out

        assert issues == 1, "unresolvable configured trunk must count as an issue"
        assert "[FAIL]" in out
        assert "refs/heads/main" in out
        assert "UNMONITORED" in out
        assert "skip" not in out.lower()

    def test_master_trunk_repo_is_evaluated_not_skipped(self, tmp_path, capsys):
        """The ~/.hermes shape: trunk is `master`, and drift is real.

        Guards the other half of the fix — parameterising the ref must
        actually WORK, not merely stop erroring.
        """
        repo, _ = _shape(tmp_path, "master_lagging")

        issues = check_code_drift(repo_path=repo, trunk_ref="refs/heads/master")
        out = capsys.readouterr().out

        assert issues == 1
        assert "LAGS master" in out
        assert "landed on master, never deployed" in out
        # Remediation must name the repo's OWN trunk: "ff-only main" is a
        # fatal command in a repo that has no main.
        assert "merge --ff-only master" in out
        assert "ff-only main" not in out

    def test_master_trunk_repo_in_sync_is_ok(self, tmp_path, capsys):
        repo, _ = _shape(tmp_path, "master_in_sync")

        issues = check_code_drift(repo_path=repo, trunk_ref="refs/heads/master")
        out = capsys.readouterr().out
        assert issues == 0
        assert "[OK]" in out and "in sync" in out

    def test_watched_repos_pair_every_path_with_its_own_trunk(self):
        """The config surface itself: ~/.hermes must be watched, and must
        NOT inherit agent-src's `main` (it has no such branch)."""
        from events.producers.code_drift_monitor import watched_repos

        by_name = {r.name: r for r in watched_repos()}
        assert "hermes" in by_name, "~/.hermes parent repo must be watched"
        assert by_name["hermes"].trunk_ref == "refs/heads/master"
        assert by_name["agent-src"].trunk_ref == "refs/heads/main"
        assert by_name["hermes"].path != by_name["agent-src"].path

    def test_check_never_mutates_repo(self, tmp_path):
        repo, shas = _shape(tmp_path, "lagging")
        sha_a = shas[0]
        (repo / "scratch.txt").write_text("uncommitted")

        check_code_drift(repo_path=repo)

        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == sha_a
        assert (repo / "scratch.txt").read_text() == "uncommitted"
        status = _git(repo, "status", "--porcelain").stdout
        assert "scratch.txt" in status  # still untracked, nothing committed/stashed

    def test_doctor_renders_the_producer_probe_rather_than_its_own(
        self, tmp_path, monkeypatch, capsys
    ):
        """Spec decision 2: ONE probe, two renderings.

        Until 2026-07-28 this module carried a near-duplicate copy of
        sample_code_drift() and both copies independently degraded an
        unresolvable trunk ref to a quiet skip.  A blind spot closed in one
        surface but not the other is worse than one closed nowhere, because
        the fixed surface gets cited as proof the box is clean.

        Stubbing the PRODUCER's sampler must therefore change what the
        DOCTOR prints.  If someone reintroduces a local git probe here this
        test fails, because the doctor would ignore the stub and report on
        the real (in-sync) repo instead.
        """
        from events.producers.code_drift_monitor import DriftSample
        import hermes_cli.events_doctor as doctor

        repo, _ = _shape(tmp_path, "one_commit_on_main")   # REAL repo, in sync

        monkeypatch.setattr(doctor, "sample_code_drift", lambda *a, **k: DriftSample(
            state="behind", head="dead" * 10, trunk="beef" * 10,
            behind_count=7, missed_subjects=("abc1234 only via the producer",),
        ))

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out

        assert issues == 1
        assert "LAGS main by 7 commit" in out
        assert "only via the producer" in out

    def test_doctor_asks_the_probe_for_an_UNGATED_sample(
        self, tmp_path, monkeypatch
    ):
        """Sharing the probe must NOT drag the producer's alert gate along.

        The producer narrows ALERTS to executed-dir changes so a phone does
        not buzz for inert docs churn.  The doctor is a diagnostic run on
        purpose, so it reports every divergence -- it opts out by passing
        executed_dirs=().  Gating here would hide real drift from the one
        surface whose whole job is to show it.
        """
        import hermes_cli.events_doctor as doctor

        real = doctor.sample_code_drift
        seen = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(doctor, "sample_code_drift", _spy)

        repo, _ = _shape(tmp_path, "one_commit_on_main")
        check_code_drift(repo_path=repo)

        assert seen.get("executed_dirs") == (), (
            "doctor must request an ungated sample; gating it would suppress "
            "the very drift the operator ran the doctor to see"
        )

    def test_unresolvable_HEAD_is_a_skip_noop(
        self, tmp_path, monkeypatch, capsys
    ):
        """Transient HEAD failure must never fabricate drift or recovery."""
        import hermes_cli.events_doctor as doctor

        monkeypatch.setattr(doctor, "sample_code_drift", lambda *a, **k: None)

        issues = check_code_drift(repo_path=tmp_path / "anything")
        out = capsys.readouterr().out

        assert issues == 0
        assert "skip" in out.lower()
        assert "UNMONITORED" not in out

    def test_no_repo_path_checks_every_registry_entry_with_its_trunk(
        self, tmp_path, monkeypatch, capsys
    ):
        from events.producers.code_drift_monitor import DriftSample, WatchedRepo
        import hermes_cli.events_doctor as doctor

        repos = [
            WatchedRepo("agent-src", tmp_path / "a", "refs/heads/main"),
            WatchedRepo("hermes", tmp_path / "b", "refs/heads/master"),
        ]
        seen = []
        monkeypatch.setattr(doctor, "watched_repos", lambda: repos)

        def sample(path, trunk_ref, **kwargs):
            seen.append((path, trunk_ref, kwargs["executed_dirs"]))
            return DriftSample(
                state="in_sync", head="a" * 40, trunk="a" * 40,
                trunk_ref=trunk_ref,
            )

        monkeypatch.setattr(doctor, "sample_code_drift", sample)

        assert check_code_drift() == 0
        out = capsys.readouterr().out
        assert seen == [
            (tmp_path / "a", "refs/heads/main", ()),
            (tmp_path / "b", "refs/heads/master", ()),
        ]
        assert "[agent-src]" in out and "[hermes]" in out

    def test_run_doctor_surfaces_drift_and_fails(self, tmp_path, monkeypatch, capsys):
        repo, _ = _shape(tmp_path, "lagging")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_AGENT_SRC", str(repo))
        (tmp_path / "events").mkdir()
        sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()

        rc = run_doctor(check_telegram_api=False)
        out = capsys.readouterr().out
        assert rc != 0
        assert "code drift" in out and "LAGS" in out
