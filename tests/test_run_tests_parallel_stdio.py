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
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _cp1252_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252", errors="strict"), raw


def test_glyph_safe_stdio_survives_cp1252(monkeypatch) -> None:
    mod = _load_runner()
    stream, raw = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    mod._make_stdio_glyph_safe()
    # Must not raise on the runner's progress glyph.
    print("✓ file done", file=sys.stdout)
    sys.stdout.flush()
    assert raw.getvalue()  # something was written


def test_glyph_safe_stdio_noop_without_reconfigure(monkeypatch) -> None:
    """Streams without .reconfigure must not crash the helper."""
    mod = _load_runner()

    class _Bare:
        pass

    monkeypatch.setattr(sys, "stdout", _Bare())
    monkeypatch.setattr(sys, "stderr", _Bare())
    mod._make_stdio_glyph_safe()  # must not raise
