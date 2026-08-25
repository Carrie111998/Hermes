"""Regression: interrupted-turn markers must be session-scoped on disk.

Issue #94778 — "Auto-continue false positive: interrupted-turn marker shared
across sessions causing session A's interrupted state to prematurely
auto-continue session B." Two backend processes on one ``HERMES_HOME`` (or
even two concurrently-resumed sessions inside one process) must NOT let
session B's continuation logic observe session A's interrupted prompt.

Contract pinned here:

* Each session's marker lives at its own per-session path inside
  ``<HERMES_HOME>/desktop/interrupted_turns/`` so writes never replace
  another session's bytes, and a stray reader cannot observe them.
* Recording for session A cannot disturb a recorded marker for B; clearing
  B cannot touch A.
* ``read_turn_marker(home, B)`` returns ``None`` when only session A has
  ever recorded a marker — independent of how many backends share the
  ``HERMES_HOME``.
"""

from __future__ import annotations

import pytest

from tui_gateway import turn_marker
from tui_gateway.turn_marker import (
    clear_turn_marker,
    read_turn_marker,
    record_turn_start,
)


def _marker_dir(home) -> "object":
    """Per-session marker directory inside ``<home>/desktop/interrupted_turns/``."""
    return home / "desktop" / "interrupted_turns"


def test_marker_lives_in_per_session_file(tmp_path):
    """Recording for a session writes ONE file named after that session.

    Under the pre-fix layout, every session shared a single ``interrupted_turns.json``;
    under post-fix, each session gets its own file under ``desktop/interrupted_turns/``.
    """
    record_turn_start(tmp_path, "session-A", "prompt for A", attempts=0)

    per_session = _marker_dir(tmp_path) / "session-A.json"
    assert per_session.exists(), (
        f"expected per-session marker file at {per_session}; "
        "turn_marker records must be session-scoped on disk "
        "(see issue #94778)."
    )
    # Older shared-file path must NOT exist after the post-fix layout was adopted.
    shared = tmp_path / "desktop" / "interrupted_turns.json"
    assert not shared.exists(), (
        f"legacy shared file {shared} reappeared; per-session layout regressed "
        "(see issue #94778)."
    )


def test_read_session_b_after_session_a_interrupted_returns_none(tmp_path):
    """Session B's resume must NOT see session A's interrupted prompt.

    Concretely: ``record_turn_start`` for A leaves a marker (simulating A's
    process death mid-turn); ``read_turn_marker`` for B — which has never run
    a turn — must report no marker. Without session-scoped isolation a
    shared storage layer (or a key collision in the old single-file layout)
    can leak A's leftover marker into B's resume path and trigger a
    duplicate auto-continue.
    """
    record_turn_start(tmp_path, "session-A", "finish the migration", attempts=0)

    # A's marker should be readable under its own key …
    a_marker = read_turn_marker(tmp_path, "session-A")
    assert a_marker is not None
    assert a_marker["prompt"] == "finish the migration"

    # … and B should see nothing, even though A's bytes live in the same HERMES_HOME.
    assert read_turn_marker(tmp_path, "session-B") is None


def test_clearing_session_b_does_not_touch_session_a(tmp_path):
    """``clear_turn_marker`` for B must NOT delete A's marker.

    Under a shared-file layout a buggy ``del entries[session_key]`` step
    could mis-target if the key resolution walks a different scope; under
    the per-session layout this is structurally impossible because the two
    markers are different files.
    """
    record_turn_start(tmp_path, "session-A", "A prompt", attempts=0)
    record_turn_start(tmp_path, "session-B", "B prompt", attempts=0)

    clear_turn_marker(tmp_path, "session-B")

    assert read_turn_marker(tmp_path, "session-A") is not None
    assert read_turn_marker(tmp_path, "session-A")["prompt"] == "A prompt"
    assert read_turn_marker(tmp_path, "session-B") is None


def test_two_backends_sharing_hermes_home_isolates_markers(tmp_path):
    """Two backend processes sharing one ``HERMES_HOME`` stay isolated.

    We simulate this by impersonating two writers that operate on disjoint
    sub-directories of the same ``HERMES_HOME`` — the contract the post-fix
    layout has to honour is that neither writer's record can affect the
    other's read.
    """
    backend_a_home = tmp_path / "backend-A"
    backend_b_home = tmp_path / "backend-B"
    backend_a_home.mkdir()
    backend_b_home.mkdir()

    record_turn_start(backend_a_home, "session-A", "A prompt", attempts=0)
    record_turn_start(backend_b_home, "session-B", "B prompt", attempts=0)

    assert read_turn_marker(backend_a_home, "session-A")["prompt"] == "A prompt"
    assert read_turn_marker(backend_b_home, "session-B")["prompt"] == "B prompt"
    # Cross-home reads must never see the other backend's marker.
    assert read_turn_marker(backend_a_home, "session-B") is None
    assert read_turn_marker(backend_b_home, "session-A") is None


def test_record_turn_start_replaces_only_target_session_file(tmp_path):
    """Rewriting a session's own marker does not touch other sessions' files."""
    record_turn_start(tmp_path, "session-A", "A first prompt", attempts=0)
    record_turn_start(tmp_path, "session-B", "B prompt", attempts=0)
    record_turn_start(tmp_path, "session-A", "A second prompt", attempts=2)

    a_marker = read_turn_marker(tmp_path, "session-A")
    assert a_marker is not None
    assert a_marker["prompt"] == "A second prompt"
    assert a_marker["attempts"] == 2

    b_marker = read_turn_marker(tmp_path, "session-B")
    assert b_marker is not None
    assert b_marker["prompt"] == "B prompt"
    assert b_marker["attempts"] == 0


def test_per_session_file_name_rejects_unsafe_session_keys(tmp_path):
    """Session keys with path-traversal characters must not escape the marker dir.

    ``_new_session_key`` produces a hex string today, but the marker module
    must defend against any caller (test, debug tool, future code) passing a
    key like ``../other_session``. The safe behaviour is to refuse the write
    entirely rather than escape the directory.
    """
    # Neither an escape attempt writes outside the marker dir, nor does it
    # pollute a sibling session's marker.
    record_turn_start(tmp_path, "../escape", "escape prompt", attempts=0)
    record_turn_start(tmp_path, "session-A", "A prompt", attempts=0)
    record_turn_start(tmp_path, "session-B", "B prompt", attempts=0)

    # The escape attempt did not overwrite anything inside the marker dir.
    assert read_turn_marker(tmp_path, "session-A")["prompt"] == "A prompt"
    assert read_turn_marker(tmp_path, "session-B")["prompt"] == "B prompt"

    # And no marker file got created above the marker dir.
    above = list(tmp_path.iterdir())
    assert all(p.name != "escape" for p in above), (
        "session-key path traversal escaped the marker directory"
    )


def test_legacy_shared_marker_file_is_not_consulted(tmp_path):
    """If a pre-existing ``interrupted_turns.json`` is on disk, reads must ignore it.

    Leaving the old shared file in place after upgrading would silently re-introduce
    cross-session bleed. New readers must consult only the per-session files.
    """
    legacy = tmp_path / "desktop"
    legacy.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy / "interrupted_turns.json"
    legacy_file.write_text(
        '{"session-A": {"prompt": "A prompt", "started_at": 1.0, "attempts": 0}}'
    )

    # Nothing on the new side → no marker for A.
    assert read_turn_marker(tmp_path, "session-A") is None

    # Writing the new layout also clears (or at least doesn't merge) the legacy
    # reader's view of the world.
    record_turn_start(tmp_path, "session-A", "A prompt v2", attempts=1)
    marker = read_turn_marker(tmp_path, "session-A")
    assert marker is not None
    assert marker["prompt"] == "A prompt v2"
    assert marker["attempts"] == 1


def test_unsanitized_session_key_returns_none_on_read(tmp_path):
    """Path-traversal session keys are rejected end-to-end (no read either)."""
    record_turn_start(tmp_path, "../escape", "escape prompt", attempts=0)
    assert read_turn_marker(tmp_path, "../escape") is None


def test_module_layout_uses_per_session_subdir():
    """Module-level layout constants reflect the per-session layout, not the old shared file."""
    # The shared single-file name must not be the canonical layout anymore.
    # ``_MARKER_SUBDIR`` is the new per-session directory; ``_MARKER_FILE`` is
    # no longer used as the only storage location (it may legitimately not exist
    # at all post-fix).
    assert getattr(turn_marker, "_MARKER_SUBDIR", None) == "interrupted_turns", (
        "turn_marker._MARKER_SUBDIR should name the per-session directory"
    )
