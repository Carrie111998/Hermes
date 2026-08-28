"""The adapter-guard cache verdict must be keyed to the bytes it judged.

Before 2026-08-28 ``tests/gateway/conftest.py`` fingerprinted the gateway
test files by (mtime_ns, size) and then read them a second time for the
scan. On this box concurrent agent sessions rewrite files in the SHARED
checkout while suites run, so that shape had two suppression routes:

* stat-then-read TOCTOU — the persisted verdict's key described bytes the
  scan never saw;
* a same-size rewrite with a restored mtime reused a stale shared "clean"
  verdict for content that was never scanned — for EVERY concurrent pytest
  subprocess, since the verdict file is shared.

The fix reads each file once and derives the fingerprint from the sha256 of
those same bytes. These tests pin that semantic end to end via the factored
``_adapter_guard_check`` entry point.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.gateway.conftest import (
    _adapter_guard_check,
    _fingerprint_gateway_tests,
    _read_gateway_test_sources,
)

_VIOLATING = (
    "import sys\n"
    'sys.path.insert(0, "plugins/platforms/telegram")\n'
    "from adapter import TelegramAdapter\n"
)

_CLEAN = (
    "import sys\n"
    'PLUGIN_DIRS = ["plugins/platforms/telegram"]\n'
    'NOTE = "loads its adapter via load_plugin_adapter"\n'
)


def _pad_to_same_length(a: str, b: str) -> tuple[str, str]:
    """Append comment padding so both sources have identical byte length."""
    target = max(len(a.encode()), len(b.encode())) + 4
    def pad(s: str) -> str:
        need = target - len(s.encode())
        return s + "#" + "x" * (need - 2) + "\n"
    a2, b2 = pad(a), pad(b)
    assert len(a2.encode()) == len(b2.encode())
    return a2, b2


@pytest.fixture()
def gateway_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tests" / "gateway"
    d.mkdir(parents=True)
    return d


def _rewrite_preserving_stat(path: Path, new_content: str) -> None:
    """The adversarial rewrite: same size (by construction), restored mtime."""
    st = path.stat()
    path.write_text(new_content, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns, (
        "test setup failed to preserve (mtime, size) — the scenario no "
        "longer models the stat-key suppression"
    )


class TestFingerprintIsContentKeyed:
    def test_same_stat_different_content_changes_the_fingerprint(
        self, gateway_dir: Path
    ) -> None:
        clean, violating = _pad_to_same_length(_CLEAN, _VIOLATING)
        target = gateway_dir / "test_scratch.py"
        target.write_text(clean, encoding="utf-8")
        fp_before = _fingerprint_gateway_tests(
            _read_gateway_test_sources(gateway_dir)
        )

        _rewrite_preserving_stat(target, violating)
        fp_after = _fingerprint_gateway_tests(
            _read_gateway_test_sources(gateway_dir)
        )
        # Under the old (mtime, size) key these were EQUAL — the stale
        # shared "clean" verdict then suppressed the guard.
        assert fp_after != fp_before

    def test_same_content_different_mtime_keeps_the_fingerprint(
        self, gateway_dir: Path
    ) -> None:
        """The key must depend on nothing BUT the bytes: a touch (git
        checkout, editor save of identical content) must not churn the
        cache."""
        target = gateway_dir / "test_scratch.py"
        target.write_text(_CLEAN, encoding="utf-8")
        fp_before = _fingerprint_gateway_tests(
            _read_gateway_test_sources(gateway_dir)
        )

        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        fp_after = _fingerprint_gateway_tests(
            _read_gateway_test_sources(gateway_dir)
        )
        assert fp_after == fp_before


class TestStaleVerdictCannotSuppressTheGuard:
    def test_clean_verdict_does_not_cover_a_stat_preserving_rewrite(
        self, gateway_dir: Path, tmp_path: Path
    ) -> None:
        """End to end: pass clean (persisting the shared verdict), rewrite to
        a violation preserving (mtime, size), and the guard must still fire."""
        clean, violating = _pad_to_same_length(_CLEAN, _VIOLATING)
        cache_dir = tmp_path / ".pytest-cache"
        target = gateway_dir / "test_scratch.py"
        target.write_text(clean, encoding="utf-8")

        _adapter_guard_check(gateway_dir, cache_dir)  # persists "clean"
        assert any(cache_dir.glob("gw-adapter-guard-*"))

        _rewrite_preserving_stat(target, violating)
        with pytest.raises(pytest.UsageError, match="anti-pattern"):
            _adapter_guard_check(gateway_dir, cache_dir)

    def test_violation_verdict_is_cached_and_replayed(
        self, gateway_dir: Path, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / ".pytest-cache"
        target = gateway_dir / "test_scratch.py"
        target.write_text(_VIOLATING, encoding="utf-8")

        with pytest.raises(pytest.UsageError):
            _adapter_guard_check(gateway_dir, cache_dir)
        # Second call replays from the cache — still red, same key.
        with pytest.raises(pytest.UsageError):
            _adapter_guard_check(gateway_dir, cache_dir)

    def test_fixing_the_file_clears_the_guard(
        self, gateway_dir: Path, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / ".pytest-cache"
        target = gateway_dir / "test_scratch.py"
        target.write_text(_VIOLATING, encoding="utf-8")
        with pytest.raises(pytest.UsageError):
            _adapter_guard_check(gateway_dir, cache_dir)

        target.write_text(_CLEAN, encoding="utf-8")
        _adapter_guard_check(gateway_dir, cache_dir)  # must not raise


class TestScanJudgesTheHashedBytes:
    def test_scan_and_fingerprint_share_one_read(self, gateway_dir: Path) -> None:
        """The sources mapping is read once and handed to both consumers —
        the scan never re-reads the disk, so verdict and key cannot diverge."""
        target = gateway_dir / "test_scratch.py"
        target.write_text(_CLEAN, encoding="utf-8", newline="\n")
        sources = _read_gateway_test_sources(gateway_dir)
        assert sources == {target: _CLEAN.encode()}
        # Deleting the file after the read changes nothing for either
        # consumer: both operate on the captured bytes.
        target.unlink()
        from tests.gateway.conftest import _run_adapter_antipattern_scan

        assert _run_adapter_antipattern_scan(sources) == []
        assert _fingerprint_gateway_tests(sources)
