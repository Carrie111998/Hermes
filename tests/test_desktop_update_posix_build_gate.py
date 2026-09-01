"""Ordering contract for the POSIX Desktop self-update handoff."""

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "desktop-update" / "posix.sh"


def test_success_always_proves_desktop_artifact_before_install() -> None:
    """Even an already-current checkout must not install a stale release app."""
    source = SCRIPT.read_text(encoding="utf-8")
    update = source.index('OUT="$("$HERMES_BIN" update')
    build_gate = source.index('"$HERMES_BIN" desktop --build-only', update)
    finish_trigger = source.index('exit "$FINAL_CODE"', build_gate)

    assert update < build_gate < finish_trigger
    assert '"$HERMES_BIN" desktop --force-build --build-only' in source[build_gate:finish_trigger]
