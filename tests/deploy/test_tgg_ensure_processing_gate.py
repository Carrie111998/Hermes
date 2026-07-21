from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/tgg/christopher/scripts/ensure_processing_gate.py"


def _module():
    spec = importlib.util.spec_from_file_location("tgg_ensure_processing_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("enabled", [False, True])
def test_existing_valid_gate_is_preserved_byte_exact(tmp_path: Path, enabled: bool) -> None:
    module = _module()
    path = tmp_path / "processing-gate.json"
    before = json.dumps(
        {"version": 1, "enabled": enabled, "generation": 3, "marker": "keep"},
        indent=2,
    ) + "\n"
    path.write_text(before, encoding="utf-8")

    state = module.ensure_processing_gate(path)

    assert state["enabled"] is enabled
    assert path.read_text(encoding="utf-8") == before


def test_missing_gate_is_created_disabled(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "processing-gate.json"

    state = module.ensure_processing_gate(path)

    assert state["enabled"] is False
    assert state["generation"] == 0
    assert json.loads(path.read_text(encoding="utf-8")) == state


@pytest.mark.parametrize(
    "state",
    [
        {"enabled": "true", "generation": 3},
        {"enabled": True, "generation": -1},
        {"enabled": True, "generation": "3"},
        {"enabled": True, "generation": False},
    ],
)
def test_invalid_existing_gate_refuses(tmp_path: Path, state: dict[str, object]) -> None:
    module = _module()
    path = tmp_path / "processing-gate.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RuntimeError):
        module.ensure_processing_gate(path)
