"""#71047: `hermes config set <top-level-key> <scalar>` must not silently
overwrite a dict-typed section (e.g. ``model:``) with a bare string."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _make_config_dir(tmp_path: Path) -> Path:
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    config = hermes_home / "config.yaml"
    config.write_text(
        "model:\n"
        "  default: gpt-4o\n"
        "  provider: openai-api\n"
        "  context_length: 128000\n"
        "platforms:\n"
        "  telegram:\n"
        "    streaming: true\n",
        encoding="utf-8",
    )
    return hermes_home


def test_set_scalar_refuses_to_overwrite_dict(tmp_path, monkeypatch):
    """`hermes config set model openai-codex/gpt-5.6-sol` must refuse and exit."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    with pytest.raises(SystemExit):
        set_config_value("model", "openai-codex/gpt-5.6-sol", force=True)

    # Verify the config was NOT modified
    import yaml
    with open(hermes_home / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    assert isinstance(cfg["model"], dict), "model section was overwritten!"
    assert cfg["model"]["default"] == "gpt-4o"
    assert cfg["model"]["provider"] == "openai-api"
    assert cfg["model"]["context_length"] == 128000


def test_set_dotted_key_still_works(tmp_path, monkeypatch):
    """`hermes config set model.default openai-codex/gpt-5.6-sol` must work."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    set_config_value("model.default", "openai-codex/gpt-5.6-sol", force=True)

    import yaml
    with open(hermes_home / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["default"] == "openai-codex/gpt-5.6-sol"
    # Siblings preserved
    assert cfg["model"]["provider"] == "openai-api"
    assert cfg["model"]["context_length"] == 128000


def test_set_scalar_on_scalar_key_still_works(tmp_path, monkeypatch):
    """Setting a scalar on a key that's already a scalar must work."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    # timezone is a scalar — setting it should work
    set_config_value("timezone", "America/New_York", force=True)

    import yaml
    with open(hermes_home / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    assert cfg["timezone"] == "America/New_York"