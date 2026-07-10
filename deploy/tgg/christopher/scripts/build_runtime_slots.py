#!/usr/bin/env python3
"""Build deployable Christopher runtime slots from the recovered June baseline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = DEPLOY_ROOT / "baselines" / "june-2026"
SLOTS_ROOT = DEPLOY_ROOT / "runtime-slots"
DEFAULT_MODEL = "gpt-5.4-mini"
ALLOWED_MODELS = (DEFAULT_MODEL, "gpt-5.6-luna")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one {label} marker, found {count}")
    return text.replace(old, new, 1)


def _safe_config(source: str, model: str) -> str:
    rendered = _replace_once(
        source,
        "pa:\n  enabled: true\n",
        "pa:\n  enabled: false\n",
        label="pa.enabled",
    )
    rendered = _replace_once(
        rendered,
        "platforms:\n  whatsapp:\n    enabled: true\n",
        "platforms:\n  whatsapp:\n    enabled: false\n",
        label="platforms.whatsapp.enabled",
    )
    if model != DEFAULT_MODEL:
        rendered = rendered.replace(DEFAULT_MODEL, model)
    return rendered


def _constitution(source: str, model: str) -> str:
    if model == DEFAULT_MODEL:
        return source
    rendered = source.replace(DEFAULT_MODEL, model)
    if rendered == source:
        raise RuntimeError(f"constitution has no {DEFAULT_MODEL} selectors")
    return rendered


def _validate(config_path: Path, constitution_path: Path, model: str) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))

    assert config["pa"]["enabled"] is False
    assert config["group_sessions_per_user"] is False
    assert config["platforms"]["whatsapp"]["enabled"] is False
    assert config["model"]["provider"] == "openai-direct-primary"
    assert config["model"]["default"] == model
    assert config["providers"]["openai-direct-primary"]["default_model"] == model
    for task in ("compression", "session_search", "title_generation"):
        assert config["auxiliary"][task]["model"] == model

    assert constitution["runtime"] == {
        "provider": "openai-direct-primary",
        "model": model,
    }
    for job in ("tgg_ops_ingest", "tgg_management"):
        assert constitution["job_briefs"][job]["runtime"] == {
            "model": model,
            "provider": "openai-direct-primary",
        }


def main() -> int:
    baseline_config = (BASELINE_ROOT / "config.live-2026-06-19.yaml").read_text(
        encoding="utf-8"
    )
    baseline_constitution = (
        BASELINE_ROOT / "christopher_tgg_constitution.live-2026-06-19.yaml"
    ).read_text(encoding="utf-8")

    slot_files: list[Path] = []
    for model in ALLOWED_MODELS:
        slot = SLOTS_ROOT / model
        slot.mkdir(parents=True, exist_ok=True)
        config_path = slot / "config.yaml"
        constitution_path = slot / "christopher_tgg_constitution.yaml"
        config_path.write_text(_safe_config(baseline_config, model), encoding="utf-8")
        constitution_path.write_text(
            _constitution(baseline_constitution, model), encoding="utf-8"
        )
        _validate(config_path, constitution_path, model)
        slot_files.extend((config_path, constitution_path))

    # The historical root paths remain the default authored deployment view.
    # They are generated from, and must remain byte-identical to, the default slot.
    default_slot = SLOTS_ROOT / DEFAULT_MODEL
    root_config = DEPLOY_ROOT / "config.yaml"
    root_constitution = DEPLOY_ROOT / "christopher_tgg_constitution.yaml"
    root_config.write_bytes((default_slot / "config.yaml").read_bytes())
    root_constitution.write_bytes(
        (default_slot / "christopher_tgg_constitution.yaml").read_bytes()
    )
    checksum_lines = []
    for path in slot_files:
        checksum_lines.append(f"{_sha256(path)}  {path.relative_to(SLOTS_ROOT)}")
    (SLOTS_ROOT / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
