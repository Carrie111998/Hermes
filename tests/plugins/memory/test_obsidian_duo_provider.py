from pathlib import Path
import json

from agent.plugin_llm import PluginLlm
from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import Authority


def test_provider_captures_host_owned_llm():
    llm = PluginLlm(plugin_id="obsidian_duo")
    provider = ObsidianDuoMemoryProvider(llm=llm)

    assert provider.name == "obsidian_duo"
    assert provider._llm is llm
    assert provider.get_tool_schemas()[0]["name"] == "memory_duo"


def test_provider_initializes_profile_scoped_home(tmp_path):
    provider = ObsidianDuoMemoryProvider()
    ObsidianDuoConfig(vault_path=str(tmp_path / "Vault")).save(tmp_path)

    provider.initialize("session-1", hermes_home=str(tmp_path))

    assert provider._hermes_home == Path(tmp_path)


def test_bundled_provider_discovery_registers_host_owned_llm(monkeypatch):
    from plugins.memory import load_memory_provider

    monkeypatch.setattr(
        ObsidianDuoConfig,
        "find_config",
        classmethod(lambda cls, hermes_home=None: Path("/tmp/obsidian_duo.json")),
    )

    provider = load_memory_provider("obsidian_duo")

    assert provider is not None
    assert provider.name == "obsidian_duo"
    assert isinstance(provider._llm, PluginLlm)


def test_provider_lifecycle_uses_profile_scoped_broker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    ObsidianDuoConfig(vault_path=str(tmp_path / "Vault")).save(home)
    monkeypatch.setattr("plugins.memory.obsidian_duo.get_hermes_home", lambda: home, raising=False)
    provider = ObsidianDuoMemoryProvider()

    provider.initialize("session-1", hermes_home=str(home))

    assert provider._broker is not None
    assert provider.prefetch("thanks") == ""
    assert provider.get_tool_schemas()[0]["name"] == "memory_duo"
    assert '"commit"' not in json.dumps(provider.get_tool_schemas())
    provider.shutdown()


def test_hot_memory_bridge_maps_memory_and_user_targets_to_supported_types(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))

    provider.on_memory_write("add", "memory", "durable fact", {"write_origin": "user"})
    provider.on_memory_write("add", "user", "prefers dark mode", {"write_origin": "user"})

    records = provider._broker._broker.store.connection().execute(
        "SELECT memory_type, authority FROM memories ORDER BY rowid"
    ).fetchall()
    assert [row[0] for row in records] == ["fact", "preference"]
    assert all(row[1] == Authority.USER.value for row in records)
    provider.shutdown()
