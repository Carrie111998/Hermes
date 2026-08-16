"""Regression tests for the read-only ``hermes config check`` contract."""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import config as config_mod


def _write_explicit_gate_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": config_mod.DEFAULT_CONFIG["_config_version"],
                "memory": {"write_approval": False},
                "skills": {"write_approval": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _fingerprint(path):
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_mtime_ns


def test_config_check_preserves_config_bytes_and_mtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = _write_explicit_gate_config(tmp_path)
    before = _fingerprint(config_path)

    config_mod.config_command(SimpleNamespace(config_command="check"))

    assert _fingerprint(config_path) == before
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is False
    assert persisted["skills"]["write_approval"] is False
    capsys.readouterr()


def test_config_check_rejects_transitive_config_write(tmp_path, monkeypatch, capsys):
    """A diagnostic imported by ``config check`` must not persist config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "--profile", "isolated", "config", "check"],
    )
    config_path = _write_explicit_gate_config(tmp_path)
    before = _fingerprint(config_path)

    def diagnostic_with_write_side_effect():
        config = config_mod.load_config()
        config["memory"]["write_approval"] = True
        config["skills"]["write_approval"] = True
        config_mod.save_config(config)
        return []

    monkeypatch.setattr(
        config_mod,
        "get_missing_config_fields",
        diagnostic_with_write_side_effect,
    )

    with pytest.raises(RuntimeError, match=r"config check.*read-only"):
        config_mod.config_command(SimpleNamespace(config_command="check"))

    assert _fingerprint(config_path) == before
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is False
    assert persisted["skills"]["write_approval"] is False
    capsys.readouterr()
