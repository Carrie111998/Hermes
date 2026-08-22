"""The runner's status glyphs must not crash narrow console encodings.

On native Windows, piped or legacy-console stdio defaults to cp1252, which
cannot encode the runner's ✓/✗ progress glyphs — before the fix, the first
per-file status line killed the whole run with UnicodeEncodeError. The
failure depends only on the stream's encoding, so these tests pin it on
every OS by building a cp1252 stream explicitly.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_PATH = REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_tests_parallel", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cp1252_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252", errors="strict"), raw


def test_cp1252_stream_reproduces_the_crash_without_the_fix() -> None:
    # Baseline for the bug: a strict cp1252 stream cannot take the glyph.
    stream, _raw = _cp1252_stream()
    try:
        stream.write("✓")
    except UnicodeEncodeError:
        return
    raise AssertionError("expected UnicodeEncodeError on strict cp1252")


def test_glyph_safe_stdio_survives_cp1252(monkeypatch) -> None:
    mod = _load_runner()
    stream, raw = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    mod._make_stdio_glyph_safe()
    print("✓ tests/foo.py (3 tests, 1.2s) ✗")
    sys.stdout.flush()

    out = raw.getvalue()
    assert "✓".encode("utf-8") in out, "stream should now carry UTF-8 glyphs"
    assert b"tests/foo.py (3 tests, 1.2s)" in out, "line content must survive"


def test_glyph_safe_stdio_noop_without_reconfigure(monkeypatch) -> None:
    # Streams without .reconfigure (e.g. pytest's capture buffers, plain
    # StringIO) must pass through untouched instead of raising.
    mod = _load_runner()
    plain = io.StringIO()
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", plain)

    mod._make_stdio_glyph_safe()
    print("✓ still fine")

    assert "✓ still fine" in plain.getvalue()


def test_nonzero_child_without_output_gets_infrastructure_diagnostic(
    monkeypatch, tmp_path: Path
) -> None:
    """A child killed before pytest starts must not render as a blank failure."""
    mod = _load_runner()
    test_file = tmp_path / "test_never_started.py"
    test_file.write_text("def test_never_started():\n    assert True\n", encoding="utf-8")

    class _SilentFailedProcess:
        pid = 12345
        returncode = 9

        def communicate(self, timeout=None):
            return "", None

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: _SilentFailedProcess())
    monkeypatch.setattr(mod, "_kill_tree", lambda *args, **kwargs: None)

    _file, rc, output, summary, _wall = mod._run_one_file_once(
        test_file, [], tmp_path, 30
    )

    assert rc == 9
    assert summary == {}
    assert "produced no output" in output
    assert "infrastructure" in output.lower()


def test_exit_five_without_output_stays_per_file_no_collection(
    monkeypatch, tmp_path: Path
) -> None:
    """Pytest's no-collection exit remains tolerated for one gated file."""
    mod = _load_runner()
    test_file = tmp_path / "test_platform_gated.py"
    test_file.write_text("", encoding="utf-8")

    class _NoCollectionProcess:
        pid = 12345
        returncode = 5

        def communicate(self, timeout=None):
            return "", None

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: _NoCollectionProcess())
    monkeypatch.setattr(mod, "_kill_tree", lambda *args, **kwargs: None)

    _file, rc, output, summary, _wall = mod._run_one_file_once(
        test_file, [], tmp_path, 30
    )

    assert rc == 0
    assert summary == {}
    assert "infrastructure failure" not in output


def test_worker_exception_is_printed_in_inline_failure_box(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A Popen-level runner crash must be visible at the failing progress line."""
    mod = _load_runner()
    test_file = tmp_path / "test_never_spawned.py"
    test_file.write_text("def test_never_spawned():\n    assert True\n", encoding="utf-8")

    def _raise_before_pytest(*args, **kwargs):
        raise OSError("simulated process creation failure")

    monkeypatch.setattr(mod, "_run_one_file", _raise_before_pytest)
    monkeypatch.setattr(mod.sys, "argv", [str(_RUNNER_PATH), "--files", str(test_file), "-j", "1"])

    assert mod.main() == 1
    output = capsys.readouterr().out
    box_start = output.index("Failed:")
    box_end = output.index("Repro:", box_start)
    inline_box = output[box_start:box_end]
    assert "runner crashed before pytest completed" in inline_box
    assert "simulated process creation failure" in inline_box
