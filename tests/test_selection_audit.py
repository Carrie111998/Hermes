"""Regression coverage for the fail-closed minimal-lane contract."""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import test_selection_audit as audit


def test_manifest_preserves_rationale_and_direct_rerun_selectors() -> None:
    data = audit.load_manifest()
    assert data["manifest_version"] == "2.0"
    assert len(data["entries"]) == 38
    assert all(entry["rationale"] and entry["direct_rerun"] == entry["path"] for entry in data["entries"])


def test_stt_timing_exclusion_is_single_node_and_directly_runnable() -> None:
    data = audit.load_manifest()
    entry = next(
        item for item in data["entries"]
        if item["path"] == "tests/tools/test_transcription_tools.py"
    )
    assert entry["lane"] == "nondeterministic_timing"
    assert entry["selection_level"] == "node"
    assert entry["direct_rerun"] == entry["path"]
    assert entry["selectors"] == [
        "tests/tools/test_transcription_tools.py::TestRunCommandSttIdleTimeout::test_stderr_progress_extends_beyond_timeout"
    ]


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


def test_newly_added_undeclared_file_fails_closed() -> None:
    new_test = audit.ROOT / "tests/test_selection_audit_probe.py"
    new_test.write_text("def test_new(): pass\n", encoding="utf-8")
    try:
        # A path not in the frozen base inventory and not explicitly owned by
        # the manifest must never silently become core+dev.
        with pytest.raises(ValueError, match="unowned"):
            audit.audit_file_ownership([new_test])
    finally:
        new_test.unlink(missing_ok=True)


def test_nonexistent_node_selector_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads(audit.MANIFEST.read_text(encoding="utf-8"))
    entry = next(item for item in data["entries"] if item["selection_level"] == "node")
    entry["selectors"] = [f'{entry["path"]}::missing_test']
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(audit, "MANIFEST", manifest)
    with pytest.raises(ValueError, match="does not exist"):
        audit.load_manifest()


def test_collected_node_ids_are_cached_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        path = command[-1]
        return SimpleNamespace(
            returncode=0,
            stdout=f"{path}::test_example\n",
            stderr="",
        )

    audit._collected_node_ids.cache_clear()
    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    try:
        expected = frozenset({"tests/example/test_cached.py::test_example"})
        assert audit._collected_node_ids("tests/example/test_cached.py") == expected
        assert audit._collected_node_ids("tests/example/test_cached.py") == expected
        assert len(calls) == 1
    finally:
        audit._collected_node_ids.cache_clear()
