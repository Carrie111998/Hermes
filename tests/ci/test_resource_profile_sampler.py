"""Tests for scripts/ci/resource_profile.py — the CI resource sampler.

Covers the pure, deterministic pieces: diskstats parsing (including the
busy-time field the util % is derived from) and the series downsampler.
The sampling loop itself is wall-clock bound and is exercised end to end
by CI rather than here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "resource_profile.py"
_spec = importlib.util.spec_from_file_location("resource_profile", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load resource_profile.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# /proc/diskstats layout:
# major minor name reads_completed reads_merged sectors_read time_reading
# writes_completed writes_merged sectors_written time_writing ios_in_flight
# io_ticks time_in_queue
def _diskstat_line(name: str, reads=0, sectors_read=0, writes=0,
                   sectors_written=0, io_ticks=0) -> str:
    return (f"   8       0 {name} {reads} 0 {sectors_read} 0 "
            f"{writes} 0 {sectors_written} 0 0 {io_ticks} 0\n")


def _write_diskstats(tmp_path, monkeypatch, lines: list[str]) -> None:
    p = tmp_path / "diskstats"
    p.write_text("".join(lines))
    monkeypatch.setattr(_mod, "_PROC_DISKSTATS", str(p))


# ── _read_diskstats ──────────────────────────────────────────────────────

def test_diskstats_returns_ops_sectors_and_busy_ms(tmp_path, monkeypatch):
    _write_diskstats(tmp_path, monkeypatch, [
        _diskstat_line("sda", reads=5, sectors_read=10, writes=3,
                       sectors_written=20, io_ticks=1234),
    ])
    ops, sectors, busy_ms = _mod._read_diskstats()["sda"]
    assert ops == 8          # reads + writes completed
    assert sectors == 30     # sectors read + written
    assert busy_ms == 1234   # io_ticks, the util %'s numerator


def test_diskstats_skips_partitions_to_avoid_double_counting(tmp_path, monkeypatch):
    _write_diskstats(tmp_path, monkeypatch, [
        _diskstat_line("sda"), _diskstat_line("sda1"),
        _diskstat_line("nvme0n1"), _diskstat_line("nvme0n1p3"),
        _diskstat_line("mmcblk0"), _diskstat_line("mmcblk0p1"),
    ])
    assert set(_mod._read_diskstats()) == {"sda", "nvme0n1", "mmcblk0"}


def test_diskstats_skips_virtual_devices(tmp_path, monkeypatch):
    _write_diskstats(tmp_path, monkeypatch, [
        _diskstat_line("sda"), _diskstat_line("loop0"),
        _diskstat_line("ram0"), _diskstat_line("sr0"),
    ])
    assert set(_mod._read_diskstats()) == {"sda"}


def test_diskstats_missing_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_PROC_DISKSTATS", str(tmp_path / "nope"))
    assert _mod._read_diskstats() == {}


# ── _downsample ──────────────────────────────────────────────────────────

def test_downsample_keeps_short_series_intact():
    assert _mod._downsample([1.4, 2.6, 3.0], max_points=10) == [1, 3, 3]


def test_downsample_caps_at_max_points():
    assert len(_mod._downsample([float(i % 100) for i in range(5000)], max_points=180)) == 180


def test_downsample_uses_bucket_mean_not_decimation():
    """A spike must raise its bucket, not vanish or dominate."""
    values = [0.0] * 100
    values[50] = 100.0
    out = _mod._downsample(values, max_points=10)
    assert sum(out) > 0                      # the spike survives
    assert max(out) == 10                    # mean of one 100 over a 10-wide bucket
    assert out.count(0) == 9                 # every other bucket stays flat


def test_downsample_clamps_into_0_100():
    assert _mod._downsample([-25.0, 150.0], max_points=10) == [0, 100]


def test_downsample_empty_series():
    assert _mod._downsample([], max_points=10) == []


@pytest.mark.parametrize("n", [1, 7, 179, 180, 181, 1000])
def test_downsample_never_exceeds_the_cap_or_returns_empty(n):
    out = _mod._downsample([50.0] * n, max_points=180)
    assert 0 < len(out) <= min(n, 180)
    assert all(0 <= v <= 100 for v in out)


# ── run_profiler output contract ─────────────────────────────────────────

def test_profiler_emits_series_and_util_alongside_the_summary(tmp_path, monkeypatch):
    """One short real run: the JSON must carry both the aggregate stats and
    a 0-100 series for each of cpu/mem/disk, with points agreeing."""
    monkeypatch.setattr(_mod, "_SAMPLE_INTERVAL_S", 0.01)
    out = tmp_path / "profile.json"

    _mod.run_profiler(str(out), "unit-test", timeout_s=0.05)

    import json
    data = json.loads(out.read_text())
    assert data["label"] == "unit-test"
    assert "avg_util_pct" in data["disk"] and "peak_util_pct" in data["disk"]
    assert 0 <= data["disk"]["avg_util_pct"] <= 100

    series = data["series"]
    lengths = {len(series[k]) for k in ("cpu_pct", "mem_pct", "disk_pct")}
    assert lengths == {series["points"]}
    for key in ("cpu_pct", "mem_pct", "disk_pct"):
        assert all(isinstance(v, int) and 0 <= v <= 100 for v in series[key])
