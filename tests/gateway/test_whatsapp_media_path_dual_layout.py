"""Regression: the WhatsApp inbound-media path validator accepts BOTH cache
layouts when both exist (#92685).

get_hermes_dir() picks ONE layout per kind — legacy ``audio_cache`` when it
has content, otherwise the consolidated ``cache/audio``. But a long-lived
bridge process may have been started while the OTHER layout was active (e.g.
the legacy dir gained its first file mid-session, flipping the resolution).
The bridge then writes media into a directory that is real and Hermes-owned
but is not today's resolved root, and _is_allowed_bridge_path() rejects it,
so voice notes are never transcribed.

The validator should accept paths under either layout's root, as its
docstring already promises ("covers both the canonical cache/<kind> layout
and the legacy <kind>_cache layout").
"""
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_validator_accepts_both_layouts(tmp_path, monkeypatch):
    """A path under the non-resolved sibling layout still validates."""
    import plugins.platforms.whatsapp.adapter as adapter

    home = tmp_path / "home"
    new_layout = home / "cache" / "audio"
    legacy = home / "audio_cache"
    new_layout.mkdir(parents=True)
    legacy.mkdir(parents=True)

    # Legacy has content -> get_audio_cache_dir() resolves to LEGACY...
    (legacy / "existing.ogg").write_bytes(b"OggS")
    # ...but bridge wrote into the NEW layout before the flip.
    bridged = new_layout / "aud_stale_bridge.ogg"
    bridged.write_bytes(b"OggS")

    token = set_hermes_home_override(str(home))
    monkeypatch.delenv("HERMES_AUDIO_CACHE_DIR", raising=False)
    try:
        from gateway.platforms.base import get_audio_cache_dir

        assert Path(get_audio_cache_dir()).resolve() == legacy.resolve(), (
            "precondition: legacy layout must be the resolved root"
        )
        # The stale-bridge path under the NEW layout must be accepted.
        assert adapter._is_allowed_bridge_path(str(bridged)) is True
    finally:
        reset_hermes_home_override(token)


def test_validator_still_rejects_outside_paths(tmp_path, monkeypatch):
    """Paths outside any Hermes cache layout remain rejected."""
    import plugins.platforms.whatsapp.adapter as adapter

    home = tmp_path / "home"
    (home / "cache" / "audio").mkdir(parents=True)
    evil = tmp_path / "elsewhere" / "secret.txt"
    evil.parent.mkdir(parents=True)
    evil.write_text("nope")

    token = set_hermes_home_override(str(home))
    try:
        assert adapter._is_allowed_bridge_path(str(evil)) is False
    finally:
        reset_hermes_home_override(token)
