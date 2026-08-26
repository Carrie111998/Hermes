"""The characterization store must resolve identically in every process.

Characterization reports prove facts about the machine's installed provider
CLIs, so the store is machine-global.  ``get_hermes_home()`` resolves
profile-scoped when ``HERMES_HOME`` (or the serve ``--config-home`` context
override) names ``<root>/profiles/<name>``, which forked the store in two on
2026-08-25: ``characterize`` wrote a passing report to the root store while
the running service kept reading a stale profile store and failed every
visibility cycle.  These tests pin the repaired contract: one store, anchored
at the Hermes root, for readers and writers alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from session_bridge.characterize import (
    characterization_source_root,
    characterization_store_root,
    resolve_characterization_gate,
    write_characterization_report,
)


def _report(characterization_id: str, versions: dict[str, str]) -> dict[str, object]:
    def _provider(used_registration_turn: bool) -> dict[str, object]:
        return {
            "create": True,
            "discover": True,
            "read": True,
            "resume": True,
            "cleanup": "archived",
            "error_code": None,
            "used_registration_turn": used_registration_turn,
        }

    return {
        "schema_version": 2,
        "characterization_id": characterization_id,
        "created_at": "2026-08-25T12:00:00+00:00",
        "automatic_mirroring_enabled": False,
        "versions": dict(versions),
        "bridge_revision": {provider: "sha256:" + "0" * 64 for provider in versions},
        "providers": {
            "claude": _provider(False),
            "codex": _provider(True),
        },
    }


def test_store_root_ignores_profile_scoped_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    resolved = characterization_store_root()

    assert resolved == root / "session-bridge" / "characterization"


def test_store_root_respects_custom_deployment_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "custom-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    resolved = characterization_store_root()

    assert resolved == home / "session-bridge" / "characterization"


def test_context_override_does_not_redirect_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve --config-home`` scoping must not fork the store.

    The override is a per-task profile-scoping tool; the characterization
    store stays anchored to the process root, matching ``events.paths``.
    """

    home = tmp_path / "hermes-root"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(tmp_path / "elsewhere")
    try:
        resolved = characterization_store_root()
    finally:
        reset_hermes_home_override(token)

    assert resolved == home / "session-bridge" / "characterization"


def test_source_root_lives_inside_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert (
        characterization_source_root()
        == root / "session-bridge" / "characterization" / "claude-visibility-sources"
    )


def test_writer_and_gate_share_one_store_across_profile_scoping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-25 split-brain: written under one env, read under another."""

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    profile.mkdir(parents=True)
    characterization_id = "22222222-2222-4222-8222-222222222222"
    versions = {"claude": "1.2.3", "codex": "4.5.6"}

    monkeypatch.setenv("HERMES_HOME", str(root))
    report_path = write_characterization_report(
        _report(characterization_id, versions),
        characterization_id=characterization_id,
    )

    monkeypatch.setenv("HERMES_HOME", str(profile))
    gate = resolve_characterization_gate(current_versions=versions)

    assert report_path.parent == root / "session-bridge" / "characterization"
    assert gate.report_path == report_path
    assert gate.characterization_id == characterization_id
