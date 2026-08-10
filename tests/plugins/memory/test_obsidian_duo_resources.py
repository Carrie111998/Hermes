import sys

from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig


def test_provider_has_no_heavy_local_model_dependencies():
    import plugins.memory.obsidian_duo

    forbidden = {"torch", "sentence_transformers", "chromadb", "qdrant_client", "weaviate", "neo4j", "redis"}
    assert not forbidden.intersection(sys.modules)


def test_sync_none_initialization_has_no_broker_worker(tmp_path):
    home = tmp_path / "home"
    ObsidianDuoConfig(vault_path=str(tmp_path / "vault"), sync_mode="none").save(home)
    provider = ObsidianDuoMemoryProvider()

    provider.initialize("session", hermes_home=str(home), platform="cli")

    assert provider._broker._broker._worker is None
    provider.shutdown()
