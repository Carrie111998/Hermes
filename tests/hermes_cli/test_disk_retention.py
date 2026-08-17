"""Tests for hermes_cli.disk_retention — the disk guardian layer (OOF-250 / OOF-269).

Covers:
- truncate_log_tail (in-place tail truncation of diag logs)
- prune_files (age/count/total-size pruning)
- protected-path guards (never touch user data)
- run_retention_sweep (family coverage + exception-proofing)
- sweep_and_log (never raises)
- disk_status (low-space thresholds)
- disk_usage_summary
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import disk_retention as dr


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HERMES_HOME with the standard layout."""
    h = tmp_path / "hermes"
    for sub in ("logs", "sessions", "memories", "cache/images", "cache/audio",
                "cache/documents", "cache/screenshots"):
        (h / sub).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _write(path: Path, size: int, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = content * 79 + b"\n"
    with open(path, "wb") as f:
        while f.tell() < size:
            f.write(line)
    return path


def _age(path: Path, days: float) -> None:
    old = os.path.getmtime(path) - days * 86400
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# truncate_log_tail
# ---------------------------------------------------------------------------


class TestTruncateLogTail:
    def test_under_cap_untouched(self, home):
        f = _write(home / "logs" / "boot.log", 1000)
        reclaimed = dr.truncate_log_tail(f, max_bytes=2000, keep_bytes=500, home=home)
        assert reclaimed == 0
        assert f.stat().st_size == pytest.approx(1000, abs=100)

    def test_over_cap_truncates_in_place(self, home):
        f = _write(home / "logs" / "boot.log", 100_000)
        original_size = f.stat().st_size
        reclaimed = dr.truncate_log_tail(f, max_bytes=10_000, keep_bytes=2_000, home=home)
        assert reclaimed > 0
        new_size = f.stat().st_size
        assert new_size < original_size
        assert new_size <= 2_000 + len(dr._TRUNCATION_MARKER)
        # Same inode — in-place truncation, not unlink+recreate (OOF-2:
        # deleted-but-open files don't free space).
        assert f.exists()
        data = f.read_bytes()
        assert data.startswith(dr._TRUNCATION_MARKER)

    def test_kept_tail_starts_at_line_boundary(self, home):
        f = home / "logs" / "boot.log"
        f.write_bytes(b"".join(f"line-{i:06d}\n".encode() for i in range(10_000)))
        dr.truncate_log_tail(f, max_bytes=1_000, keep_bytes=500, home=home)
        body = f.read_bytes()[len(dr._TRUNCATION_MARKER):]
        assert body.startswith(b"line-")

    def test_appender_keeps_working_after_truncation(self, home):
        """An O_APPEND writer must continue appending after truncation."""
        f = _write(home / "logs" / "boot.log", 50_000)
        with open(f, "a", encoding="utf-8") as appender:
            dr.truncate_log_tail(f, max_bytes=10_000, keep_bytes=1_000, home=home)
            appender.write("post-truncation line\n")
        assert b"post-truncation line" in f.read_bytes()

    def test_missing_file_returns_zero(self, home):
        assert dr.truncate_log_tail(
            home / "logs" / "nope.log", max_bytes=10, keep_bytes=5, home=home
        ) == 0

    def test_protected_file_untouched(self, home):
        f = _write(home / "state.db", 100_000)
        assert dr.truncate_log_tail(f, max_bytes=10, keep_bytes=5, home=home) == 0
        assert f.stat().st_size >= 100_000

    def test_file_outside_home_untouched(self, home, tmp_path):
        f = _write(tmp_path / "outside.log", 100_000)
        assert dr.truncate_log_tail(f, max_bytes=10, keep_bytes=5, home=home) == 0
        assert f.stat().st_size >= 100_000


# ---------------------------------------------------------------------------
# prune_files
# ---------------------------------------------------------------------------


class TestPruneFiles:
    def test_age_prune_removes_old_keeps_new(self, home):
        old = _write(home / "cache" / "images" / "old.jpg", 100)
        new = _write(home / "cache" / "images" / "new.jpg", 100)
        _age(old, days=10)
        removed, reclaimed = dr.prune_files(
            (home / "cache" / "images").iterdir(), max_age_days=3, home=home
        )
        assert removed == 1
        assert reclaimed >= 100
        assert not old.exists()
        assert new.exists()

    def test_keep_count_protects_newest_even_when_expired(self, home):
        files = []
        for i in range(6):
            f = _write(home / f"state.db.malformed-{i}", 100)
            _age(f, days=30 + i)  # all expired; file 0 is newest
            files.append(f)
        removed, _ = dr.prune_files(
            home.glob("state.db.malformed-*"),
            keep_count=5, max_age_days=14, home=home,
        )
        assert removed == 1
        assert not files[5].exists()  # oldest removed
        assert all(f.exists() for f in files[:5])

    def test_max_total_bytes_removes_oldest_first(self, home):
        files = []
        for i in range(4):
            f = _write(home / "cache" / "audio" / f"a{i}.ogg", 1000)
            _age(f, days=i)  # a0 newest, a3 oldest
            files.append(f)
        removed, _ = dr.prune_files(
            (home / "cache" / "audio").iterdir(),
            max_total_bytes=2500, home=home,
        )
        assert removed == 2
        assert not files[3].exists() and not files[2].exists()
        assert files[0].exists() and files[1].exists()

    def test_never_prunes_protected_dirs(self, home):
        f = _write(home / "sessions" / "chat.jsonl", 100)
        _age(f, days=999)
        removed, _ = dr.prune_files([f], max_age_days=1, home=home)
        assert removed == 0
        assert f.exists()

    def test_never_prunes_protected_names(self, home):
        f = _write(home / "state.db", 100)
        _age(f, days=999)
        removed, _ = dr.prune_files([f], max_age_days=1, home=home)
        assert removed == 0
        assert f.exists()

    def test_vanished_file_is_skipped(self, home):
        removed, reclaimed = dr.prune_files(
            [home / "cache" / "images" / "ghost.jpg"], max_age_days=0, home=home
        )
        assert (removed, reclaimed) == (0, 0)


# ---------------------------------------------------------------------------
# disk_status
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, total, free):
        self.total = total
        self.free = free
        self.used = total - free


class TestDiskStatus:
    def test_healthy_disk(self, home):
        with patch.object(dr.shutil, "disk_usage",
                          return_value=_FakeUsage(1_000_000_000, 500_000_000)):
            status = dr.disk_status(
                home, min_free_bytes=200 * 1024 * 1024, min_free_percent=10.0
            )
        assert status["low_space"] is False
        assert status["free_bytes"] == 500_000_000
        assert status["percent_free"] == 50.0

    def test_low_space_by_bytes(self, home):
        # 15% free but only 150MB — bytes threshold trips.
        with patch.object(dr.shutil, "disk_usage",
                          return_value=_FakeUsage(1_000_000_000, 150_000_000)):
            status = dr.disk_status(
                home, min_free_bytes=200 * 1024 * 1024, min_free_percent=10.0
            )
        assert status["low_space"] is True

    def test_low_space_by_percent(self, home):
        # 500MB free but only 5% — percent threshold trips.
        with patch.object(dr.shutil, "disk_usage",
                          return_value=_FakeUsage(10_000_000_000, 500_000_000)):
            status = dr.disk_status(
                home, min_free_bytes=200 * 1024 * 1024, min_free_percent=10.0
            )
        assert status["low_space"] is True

    def test_thresholds_from_config(self, home):
        cfg = {
            "retention": dict(dr._DEFAULT_DISK_CONFIG["retention"]),
            "low_space": {"min_free_bytes": 1, "min_free_percent": 0.0},
        }
        with patch.object(dr, "get_disk_config", return_value=cfg), \
             patch.object(dr.shutil, "disk_usage",
                          return_value=_FakeUsage(1_000, 999)):
            status = dr.disk_status(home)
        assert status["low_space"] is False
        assert status["min_free_bytes"] == 1


# ---------------------------------------------------------------------------
# disk_usage_summary
# ---------------------------------------------------------------------------


class TestDiskUsageSummary:
    def test_reports_family_sizes(self, home):
        _write(home / "logs" / "agent.log", 5_000)
        _write(home / "sessions" / "x.jsonl", 3_000)
        _write(home / "state.db", 2_000)
        _write(home / "state.db.malformed-20260817", 1_000)
        summary = dr.disk_usage_summary(home)
        assert summary["logs"] >= 5_000
        assert summary["sessions"] >= 3_000
        assert summary["state_db"] >= 2_000
        assert summary["state_db_backups"] >= 1_000

    def test_missing_dirs_are_omitted(self, home):
        summary = dr.disk_usage_summary(home)
        assert "photon" not in summary


# ---------------------------------------------------------------------------
# run_retention_sweep
# ---------------------------------------------------------------------------


def _cfg(**retention_overrides):
    cfg = {
        "retention": dict(dr._DEFAULT_DISK_CONFIG["retention"]),
        "low_space": dict(dr._DEFAULT_DISK_CONFIG["low_space"]),
    }
    cfg["retention"].update(retention_overrides)
    return cfg


class TestRetentionSweep:
    def test_truncates_unrotated_diag_logs(self, home):
        boot = _write(home / "logs" / "container-boot.log", 5_000_000)
        report = dr.run_retention_sweep(home, _cfg())
        assert report["bytes_reclaimed"] > 0
        assert boot.stat().st_size < 5_000_000
        assert report["families"]["diag_logs"]["bytes_reclaimed"] > 0

    def test_skips_rotating_handler_managed_logs(self, home):
        agent = _write(home / "logs" / "agent.log", 5_000_000)
        backup = _write(home / "logs" / "agent.log.1", 5_000_000)
        dr.run_retention_sweep(home, _cfg())
        assert agent.stat().st_size >= 5_000_000
        assert backup.stat().st_size >= 5_000_000

    def test_prunes_db_malformed_backups(self, home):
        files = []
        for i in range(8):
            f = _write(home / f"state.db.malformed-2026081{i}", 100)
            _age(f, days=20 + i)
            files.append(f)
        report = dr.run_retention_sweep(home, _cfg())
        survivors = list(home.glob("state.db.malformed-*"))
        assert len(survivors) == 5  # keep_count default
        assert report["families"]["db_backups"]["files_removed"] == 3

    def test_state_db_itself_never_touched(self, home):
        db = _write(home / "state.db", 50_000_000)
        wal = _write(home / "state.db-wal", 10_000_000)
        dr.run_retention_sweep(home, _cfg())
        assert db.stat().st_size >= 50_000_000
        assert wal.stat().st_size >= 10_000_000

    def test_sessions_never_touched(self, home):
        s = _write(home / "sessions" / "chat.jsonl", 10_000_000)
        _age(s, days=999)
        dr.run_retention_sweep(home, _cfg())
        assert s.exists()

    def test_media_cache_backstop_prunes_old_audio(self, home):
        old = _write(home / "cache" / "audio" / "old.ogg", 1_000)
        new = _write(home / "cache" / "audio" / "new.ogg", 1_000)
        _age(old, days=10)
        report = dr.run_retention_sweep(home, _cfg())
        assert not old.exists()
        assert new.exists()
        assert report["families"]["media_caches"]["files_removed"] == 1

    def test_disabled_config_is_noop(self, home):
        f = _write(home / "logs" / "container-boot.log", 5_000_000)
        report = dr.run_retention_sweep(home, _cfg(enabled=False))
        assert report["enabled"] is False
        assert f.stat().st_size >= 5_000_000

    def test_family_failure_does_not_stop_other_families(self, home):
        old = _write(home / "cache" / "audio" / "old.ogg", 1_000)
        _age(old, days=10)
        with patch.object(dr, "truncate_log_tail", side_effect=RuntimeError("boom")):
            _write(home / "logs" / "container-boot.log", 5_000_000)
            report = dr.run_retention_sweep(home, _cfg())
        # diag_logs family failed…
        assert any("diag_logs" in e for e in report["errors"])
        # …but the media cache family still ran.
        assert not old.exists()

    def test_sweep_never_raises_even_on_config_failure(self, home):
        with patch.object(dr, "get_disk_config", side_effect=RuntimeError("cfg boom")):
            report = dr.run_retention_sweep(home, None)
        assert report["errors"]


# ---------------------------------------------------------------------------
# sweep_and_log
# ---------------------------------------------------------------------------


class TestSweepAndLog:
    def test_logs_one_line_and_returns_report(self, home, caplog):
        _write(home / "logs" / "container-boot.log", 5_000_000)
        with caplog.at_level("INFO", logger=dr.__name__):
            report = dr.sweep_and_log()
        assert report["bytes_reclaimed"] > 0
        lines = [r for r in caplog.records if "Disk retention sweep" in r.getMessage()]
        assert len(lines) == 1
        assert "reclaimed" in lines[0].getMessage()

    def test_never_raises_when_sweep_crashes(self, home):
        with patch.object(dr, "run_retention_sweep", side_effect=RuntimeError("boom")):
            report = dr.sweep_and_log()
        assert report["errors"] == ["boom"]

    def test_never_raises_when_disk_status_crashes(self, home):
        with patch.object(dr, "disk_status", side_effect=OSError("statvfs fail")):
            report = dr.sweep_and_log()
        assert "bytes_reclaimed" in report

    def test_warns_on_low_space(self, home, caplog):
        low = {
            "path": str(home), "total_bytes": 100, "free_bytes": 1,
            "percent_free": 1.0, "min_free_bytes": 50,
            "min_free_percent": 10.0, "low_space": True,
        }
        with patch.object(dr, "disk_status", return_value=low), \
             caplog.at_level("WARNING", logger=dr.__name__):
            dr.sweep_and_log()
        assert any("low_space=True" in r.getMessage() for r in caplog.records)
