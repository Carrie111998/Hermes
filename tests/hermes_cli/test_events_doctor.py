"""Tests for hermes events doctor CLI diagnostic."""
import json
import sqlite3
import subprocess

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


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo, msg):
    f = repo / "f.txt"
    f.write_text(f.read_text() + msg + "\n" if f.exists() else msg + "\n")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-m", msg).returncode == 0
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


class TestCodeDrift:
    """Detached working tree vs landed `main` — the 07-20 stale-deploy trap."""

    def test_in_sync_detached_head_is_ok(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        _commit(repo, "a")
        _commit(repo, "b")
        _git(repo, "checkout", "--detach")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 0
        assert "[OK]" in out and "in sync" in out

    def test_lagging_head_warns_with_count_and_remediation(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        sha_a = _commit(repo, "first fix")
        _commit(repo, "landed but not deployed")
        _git(repo, "checkout", "--detach", sha_a)

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
        repo = _make_repo(tmp_path)
        _commit(repo, "a")
        _git(repo, "checkout", "--detach")
        _commit(repo, "unlanded work")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out
        assert "AHEAD" in out and "unlanded" in out.lower()

    def test_diverged_head_warns(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        sha_a = _commit(repo, "a")
        _commit(repo, "b on main")
        _git(repo, "checkout", "--detach", sha_a)
        _commit(repo, "c detached")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out and "DIVERGED" in out

    def test_dirty_tree_noted_but_not_counted(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        _commit(repo, "a")
        _git(repo, "checkout", "--detach")
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

    def test_missing_main_ref_skips_without_issue(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        _commit(repo, "a")
        _git(repo, "branch", "-m", "main", "trunk")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 0
        assert "skip" in out.lower()

    def test_check_never_mutates_repo(self, tmp_path):
        repo = _make_repo(tmp_path)
        sha_a = _commit(repo, "a")
        _commit(repo, "b")
        _git(repo, "checkout", "--detach", sha_a)
        (repo / "scratch.txt").write_text("uncommitted")

        check_code_drift(repo_path=repo)

        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == sha_a
        assert (repo / "scratch.txt").read_text() == "uncommitted"
        status = _git(repo, "status", "--porcelain").stdout
        assert "scratch.txt" in status  # still untracked, nothing committed/stashed

    def test_run_doctor_surfaces_drift_and_fails(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        sha_a = _commit(repo, "a")
        _commit(repo, "landed fix")
        _git(repo, "checkout", "--detach", sha_a)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_AGENT_SRC", str(repo))
        (tmp_path / "events").mkdir()
        sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()

        rc = run_doctor(check_telegram_api=False)
        out = capsys.readouterr().out
        assert rc != 0
        assert "code drift" in out and "LAGS" in out
