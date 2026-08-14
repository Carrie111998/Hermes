"""Regression coverage for the fail-closed minimal-lane contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import test_selection_audit as audit


def test_manifest_preserves_rationale_and_direct_rerun_selectors() -> None:
    data = audit.load_manifest()
    assert data["manifest_version"] == "2.0"
    assert len(data["entries"]) == 37
    assert all(entry["rationale"] and entry["direct_rerun"] == entry["path"] for entry in data["entries"])


def test_mixed_file_keeps_core_nodes_and_deselects_only_declared_nodes() -> None:
    path = audit.ROOT / "tests/agent/test_auxiliary_transport_autodetect.py"
    assert path in audit.select_minimal([path])
    args = audit.deselect_args(path)
    assert args[0] == "--deselect"
    assert args[1].startswith("tests/agent/test_auxiliary_transport_autodetect.py::")


def test_unknown_lane_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads(audit.MANIFEST.read_text(encoding="utf-8"))
    data["entries"][0]["lane"] = "optional/unknown"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(audit, "MANIFEST", manifest)
    with pytest.raises(ValueError, match="unknown manifest lane"):
        audit.load_manifest()


def test_duplicate_ownership_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads(audit.MANIFEST.read_text(encoding="utf-8"))
    data["entries"][1]["path"] = data["entries"][0]["path"]
    data["entries"][1]["direct_rerun"] = data["entries"][0]["path"]
    data["entries"][1]["selectors"] = data["entries"][0]["selectors"]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(audit, "MANIFEST", manifest)
    with pytest.raises(ValueError, match="duplicate manifest ownership"):
        audit.load_manifest()


def test_outside_root_is_not_manifest_audited() -> None:
    outside = Path("/tmp/forge-selection-probe.py")
    # The runner's explicit-file path bypasses this API; this assertion pins
    # that the audit itself never silently claims an external file as core.
    with pytest.raises(ValueError, match="outside-root"):
        audit.audit_file_ownership([outside])
