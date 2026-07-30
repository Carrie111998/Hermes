"""#71047: bare scalar sets must not silently destroy dict-typed sections.

Documented exception: ``hermes config set model <id>`` is a shorthand for
``model.default`` when ``model`` is already a mapping — siblings are kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


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


def _read_cfg(hermes_home: Path) -> dict:
    with open(hermes_home / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_bare_model_shorthand_sets_default_and_keeps_siblings(tmp_path, monkeypatch):
    """`hermes config set model <id>` → model.default; provider/context preserved."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    set_config_value("model", "openai-codex/gpt-5.6-sol", force=True)

    cfg = _read_cfg(hermes_home)
    assert isinstance(cfg["model"], dict), "model section must remain a mapping"
    assert cfg["model"]["default"] == "openai-codex/gpt-5.6-sol"
    assert cfg["model"]["provider"] == "openai-api"
    assert cfg["model"]["context_length"] == 128000


def test_set_dotted_model_default_still_works(tmp_path, monkeypatch):
    """Explicit dotted form must still work and preserve siblings."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    set_config_value("model.default", "openai-codex/gpt-5.6-sol", force=True)

    cfg = _read_cfg(hermes_home)
    assert cfg["model"]["default"] == "openai-codex/gpt-5.6-sol"
    assert cfg["model"]["provider"] == "openai-api"
    assert cfg["model"]["context_length"] == 128000


def test_set_scalar_refuses_to_overwrite_non_model_dict(tmp_path, monkeypatch):
    """Bare set on a non-model mapping (e.g. platforms) must refuse and exit."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    with pytest.raises(SystemExit):
        set_config_value("platforms", "broken-scalar", force=True)

    cfg = _read_cfg(hermes_home)
    assert isinstance(cfg["platforms"], dict), "platforms section was overwritten!"
    assert cfg["platforms"]["telegram"]["streaming"] is True
    # model untouched
    assert cfg["model"]["default"] == "gpt-4o"


def test_set_scalar_on_scalar_key_still_works(tmp_path, monkeypatch):
    """Setting a scalar on a key that's already a scalar must work."""
    hermes_home = _make_config_dir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    set_config_value("timezone", "America/New_York", force=True)

    cfg = _read_cfg(hermes_home)
    assert cfg["timezone"] == "America/New_York"


def test_bare_model_when_model_absent_still_sets_top_level(tmp_path, monkeypatch):
    """If model key is not yet a mapping, bare set remains a simple write."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import set_config_value

    set_config_value("model", "gpt-4o", force=True)
    cfg = _read_cfg(hermes_home)
    # No pre-existing mapping → historical bare write is allowed.
    assert cfg["model"] == "gpt-4o"
