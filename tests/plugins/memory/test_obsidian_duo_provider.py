from pathlib import Path

from agent.plugin_llm import PluginLlm
from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig


def test_provider_captures_host_owned_llm():
    llm = PluginLlm(plugin_id="obsidian_duo")
    provider = ObsidianDuoMemoryProvider(llm=llm)

    assert provider.name == "obsidian_duo"
    assert provider._llm is llm
    assert provider.get_tool_schemas() == []


def test_provider_initializes_profile_scoped_home(tmp_path):
    provider = ObsidianDuoMemoryProvider()

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
