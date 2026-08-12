"""Tests for optional-skills/finance/excel-author/scripts/recalc.py

Specifically its ``_run_captured``: the Windows capture-pipe grandchild hang.
``soffice.exe`` is only a launcher — it spawns ``soffice.bin``, and that
grandchild inherits the capture pipe handles and holds the write end open, so
the pipe never reaches EOF. ``subprocess.run`` kills only the direct child on
timeout and then blocks re-draining, forever.

The fix is capture into temp files rather than pipes, open-coded in the script
because skill scripts under ``optional-skills/`` are standalone stdlib-only
programs and none of them import from the repo — so the shared
``hermes_cli._subprocess_compat.run_text_capture`` is deliberately not used
here. That duplication is exactly why these tests exist: the script's copy has
no other coverage.
"""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "finance"
    / "excel-author"
    / "scripts"
    / "recalc.py"
)


@pytest.fixture(scope="module")
def recalc():
    # Loaded by path, not sys.path-inserted: "recalc" is a generic name and the
    # module is not meant to be importable as part of the package.
    spec = importlib.util.spec_from_file_location("excel_author_recalc", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wedged_script(tmp_path):
    """A child that spawns a lingering grandchild inheriting its stdio.

    This is the shape of the real failure: the grandchild (soffice.bin) keeps
    the capture handle open long after the launcher is killed.
    """
    wedged = tmp_path / "wedged.py"
    wedged.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return wedged


def test_run_captured_returns_decoded_text_and_returncode(recalc):
    r = recalc._run_captured([sys.executable, "-c", "print('hello')"], timeout=30)
    assert r.returncode == 0
    # Decoded for the caller — recalc() must not .decode() the result — and
    # \r\n normalized so the error strings it builds are stable across hosts.
    assert isinstance(r.stdout, str)
    assert r.stdout == "hello\n"


def test_run_captured_reports_stderr_as_text_for_check_returncode(recalc):
    """recalc() open-codes check=True via check_returncode(); the stderr it
    formats into its error dict must already be text."""
    r = recalc._run_captured(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout=30,
    )
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        r.check_returncode()
    assert exc_info.value.returncode == 3
    assert isinstance(exc_info.value.stderr, str)
    assert exc_info.value.stderr == "boom"


@pytest.mark.timeout(120)  # backstop only; the assertion below is the real bound
def test_run_captured_bounds_a_wedged_grandchild(recalc, tmp_path):
    """The headline contract: abort near ``timeout``, not when the grandchild
    finally exits.

    Measured on Windows against this repro: the old
    ``subprocess.run(capture_output=True, timeout=3)`` took 63.1s — it waited
    out the grandchild's full 60s sleep — while the file-backed capture
    returned in 11.5s (the 3s budget plus the synchronous tree-kill tail,
    taskkill measured at ~8.5s). The 30s ceiling here sits between the two, so
    it fails if the pipe behaviour ever comes back.
    """
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        recalc._run_captured([sys.executable, str(_wedged_script(tmp_path))], timeout=3)
    elapsed = time.monotonic() - start

    assert elapsed < 30, (
        f"raised TimeoutExpired but took {elapsed:.1f}s — the call is waiting on "
        "a grandchild that outlived the timeout, i.e. capturing through pipes again"
    )


def test_recalc_reports_missing_libreoffice_without_spawning(recalc, tmp_path, monkeypatch):
    """The no-LibreOffice path must not reach the subprocess layer at all."""
    xlsx = tmp_path / "book.xlsx"
    xlsx.write_text("", encoding="utf-8")
    monkeypatch.setattr(recalc, "find_libreoffice", lambda: None)

    def _explode(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("_run_captured called despite no libreoffice")

    monkeypatch.setattr(recalc, "_run_captured", _explode)

    result = recalc.recalc(str(xlsx))
    assert result["status"] == "error"
    assert "libreoffice not found" in result["error"]
