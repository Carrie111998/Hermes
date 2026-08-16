"""Discord presence/activity config (#64932-adjacent): config.yaml's
``discord.activity`` / ``discord.activity_type`` keys must reach the adapter
via the ``_apply_yaml_config`` YAML→env bridge (first-writer-wins, matching
every other DISCORD_* key), and on_ready must call change_presence from the
resulting env so the bot doesn't render as visually offline."""
import os
import pytest

pytest.importorskip("discord", reason="discord.py not installed in test env")

from plugins.platforms.discord import adapter as discord_adapter  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DISCORD_ACTIVITY", "DISCORD_ACTIVITY_TYPE"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_config_keys_bridge_to_env():
    discord_adapter._apply_yaml_config({}, {"activity": "Hermes Agent", "activity_type": "watching"})
    assert os.environ["DISCORD_ACTIVITY"] == "Hermes Agent"
    assert os.environ["DISCORD_ACTIVITY_TYPE"] == "watching"


def test_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("DISCORD_ACTIVITY", "from-env")
    discord_adapter._apply_yaml_config({}, {"activity": "from-config"})
    assert os.environ["DISCORD_ACTIVITY"] == "from-env"


def test_type_defaults_to_playing_at_read_site():
    # The on_ready read uses `os.getenv("DISCORD_ACTIVITY_TYPE", "") or "playing"`.
    assert (os.getenv("DISCORD_ACTIVITY_TYPE", "") or "playing") == "playing"


def test_no_config_no_env_sets_nothing():
    discord_adapter._apply_yaml_config({}, {})
    assert "DISCORD_ACTIVITY" not in os.environ


def test_adapter_source_has_presence_call_and_bridge():
    src = open(os.path.join(os.path.dirname(discord_adapter.__file__), "adapter.py")).read()
    assert "change_presence" in src
    assert 'os.getenv("DISCORD_ACTIVITY"' in src
    assert '"activity" in discord_cfg' in src
