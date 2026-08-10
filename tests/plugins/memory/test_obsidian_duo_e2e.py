import json

from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import MemoryRecord


def configure(home, vault):
    home.mkdir(parents=True, exist_ok=True)
    ObsidianDuoConfig(vault_path=str(vault)).save(home)


def test_same_remote_home_shares_memory_duo_database_across_surfaces(tmp_path):
    home = tmp_path / "remote"
    vault = tmp_path / "vault"
    configure(home, vault)
    paths = []
    for platform in ("desktop", "telegram", "cli"):
        provider = ObsidianDuoMemoryProvider()
        provider.initialize(f"session-{platform}", hermes_home=str(home), platform=platform)
        paths.append(provider._broker._broker.store.path)
        provider.shutdown()

    assert paths == [paths[0], paths[0], paths[0]]


def test_different_remote_homes_are_isolated(tmp_path):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    configure(home_a, tmp_path / "vault-a")
    configure(home_b, tmp_path / "vault-b")
    first = ObsidianDuoMemoryProvider()
    second = ObsidianDuoMemoryProvider()
    first.initialize("a", hermes_home=str(home_a), platform="cli")
    second.initialize("b", hermes_home=str(home_b), platform="cli")

    first._broker._broker.store.upsert_memory(MemoryRecord("a_only", "A", "fact", "global"), "test")

    assert second.prefetch("A") == ""
    first.shutdown()
    second.shutdown()


def test_delegation_is_candidate_evidence_not_user_confirmed(tmp_path):
    home, vault = tmp_path / "home", tmp_path / "vault"
    configure(home, vault)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("parent", hermes_home=str(home), platform="cli")

    provider.on_delegation("investigate X", "I think Y", child_session_id="child-1")
    provider._broker._broker.flush("test", 5)
    row = provider._broker._broker.store.connection().execute(
        "SELECT payload FROM candidates ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    assert row is not None
    assert "child-1" in json.dumps(row[0]) or "investigate X" in row[0]
    provider.shutdown()
