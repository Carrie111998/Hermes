from pathlib import Path
import json

from agent.plugin_llm import PluginLlm
from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import Authority, MemoryRecord


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

    trusted = {
        "write_origin": "assistant_tool",
        "user_memory_intent": "explicit_remember",
        "host_confirmed_user_memory": True,
    }
    provider.on_memory_write("add", "memory", "durable fact", trusted)
    provider.on_memory_write("add", "user", "prefers dark mode", trusted)

    records = provider._broker._broker.store.connection().execute(
        "SELECT memory_type, authority FROM memories ORDER BY rowid"
    ).fetchall()
    assert [row[0] for row in records] == ["fact", "preference"]
    assert all(row[1] == Authority.USER.value for row in records)
    provider.shutdown()


def test_write_origin_user_alone_cannot_grant_user_authority(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))

    provider.on_memory_write("add", "memory", "spoofed authority", {"write_origin": "user"})

    candidate = provider._broker._broker.store.connection().execute(
        "SELECT payload FROM candidates"
    ).fetchone()
    assert candidate is not None
    assert '"authority": "agent"' in candidate[0]
    assert '"verification": "unverified"' in candidate[0]
    provider.shutdown()


def test_assistant_tool_without_host_intent_stays_unverified(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))

    provider.on_memory_write(
        "add",
        "memory",
        "assistant-only proposal",
        {"write_origin": "assistant_tool", "execution_context": "foreground"},
    )

    candidate = provider._broker._broker.store.connection().execute(
        "SELECT payload FROM candidates"
    ).fetchone()
    assert candidate is not None
    assert '"authority": "agent"' in candidate[0]
    assert '"verification": "unverified"' in candidate[0]
    provider.shutdown()


def test_explicit_replace_supersedes_exact_memory(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))
    broker = provider._broker._broker
    old = MemoryRecord("mem_old", "old deployment phrase", "fact", "global")
    broker.store.upsert_memory(old, "test setup")
    broker.vault.write_managed_note(old)
    broker.vault.scan_managed_changes(broker.store)

    provider.on_memory_write(
        "replace",
        "memory",
        "new deployment phrase",
        {
            "write_origin": "assistant_tool",
            "execution_context": "foreground",
            "user_memory_intent": "explicit_update",
            "host_confirmed_user_memory": True,
            "old_text": "old deployment phrase",
        },
    )

    assert broker.store.get_memory("mem_old").status.value == "superseded"
    active = broker.store.connection().execute(
        "SELECT content, authority, verification FROM memories WHERE status='active'"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in active] == [
        ("new deployment phrase", "user", "user_confirmed")
    ]
    provider.shutdown()


def test_explicit_remove_archives_exact_memory_without_empty_fact(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))
    broker = provider._broker._broker
    old = MemoryRecord("mem_old", "old removable phrase", "fact", "global")
    broker.store.upsert_memory(old, "test setup")
    note = broker.vault.write_managed_note(old)
    broker.vault.scan_managed_changes(broker.store)

    provider.on_memory_write(
        "remove",
        "memory",
        "",
        {
            "write_origin": "assistant_tool",
            "execution_context": "foreground",
            "user_memory_intent": "explicit_forget",
            "host_confirmed_user_memory": True,
            "old_text": "old removable phrase",
        },
    )

    assert broker.store.get_memory("mem_old").status.value == "archived"
    assert broker.vault.parse_note(note).body == "old removable phrase"
    assert broker.store.connection().execute(
        "SELECT COUNT(*) FROM memories WHERE content=''"
    ).fetchone()[0] == 0
    provider.shutdown()


def test_ambiguous_explicit_replace_stages_without_mutating_any_memory(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    ObsidianDuoConfig(vault_path=str(vault), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))
    broker = provider._broker._broker
    for memory_id in ("mem_a", "mem_b"):
        broker.store.upsert_memory(
            MemoryRecord(memory_id, "duplicate old phrase", "fact", "global"),
            "test setup",
        )

    provider.on_memory_write(
        "replace",
        "memory",
        "new phrase",
        {
            "write_origin": "assistant_tool",
            "execution_context": "foreground",
            "user_memory_intent": "explicit_update",
            "host_confirmed_user_memory": True,
            "old_text": "duplicate old phrase",
        },
    )

    assert broker.store.connection().execute(
        "SELECT COUNT(*) FROM memories WHERE content='new phrase'"
    ).fetchone()[0] == 0
    assert broker.store.connection().execute(
        "SELECT COUNT(*) FROM candidates"
    ).fetchone()[0] == 1
    assert all(
        row[0] == "active"
        for row in broker.store.connection().execute(
            "SELECT status FROM memories WHERE memory_id IN ('mem_a', 'mem_b')"
        ).fetchall()
    )
    provider.shutdown()
