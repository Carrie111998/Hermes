from pathlib import Path

import pytest

from plugins.memory.obsidian_duo.config import ObsidianDuoConfig


def test_config_defaults_are_lightweight(tmp_path):
    cfg = ObsidianDuoConfig(vault_path=str(tmp_path / "Vault"))

    assert cfg.managed_folder == "Hermes Memory"
    assert cfg.index_mode == "lazy"
    assert cfg.sync_mode == "none"
    assert cfg.inference_mode == "inherit_session"
    assert cfg.cost_policy == "no_paid_fallback"
    assert cfg.queue_maxsize == 256
    assert cfg.recall_max_memories == 12
    assert cfg.recall_max_tokens == 5000


def test_config_round_trips_without_secrets(tmp_path):
    home = tmp_path / "home"
    cfg = ObsidianDuoConfig(vault_path=str(tmp_path / "Vault"))

    cfg.save(home)
    loaded = ObsidianDuoConfig.load(home)

    assert loaded == cfg
    assert "api_key" not in (home / "obsidian_duo.json").read_text()


@pytest.mark.parametrize(
    ("field", "value"),
    [("index_mode", "eager"), ("sync_mode", "daemon"), ("queue_maxsize", 0)],
)
def test_config_rejects_unsupported_values(tmp_path, field, value):
    kwargs = {"vault_path": str(tmp_path / "Vault"), field: value}

    with pytest.raises(ValueError):
        ObsidianDuoConfig(**kwargs)
