"""Regression test: least_used request_count must survive a fresh load_pool().

The gateway resolves a fresh CredentialPool from disk for every message
(gateway/run.py -> resolve_runtime_provider -> load_pool). Before this fix,
the least_used branch of _select_unlocked only swapped the incremented entry
in memory (_replace_entry) and never persisted it, so every process restart
re-read request_count=0 for all entries. min() then broke the 0/0 tie by
list order and always returned the priority-0 entry — the strategy silently
degraded to fill_first across restarts.
"""
import json

from .test_credential_pool import _write_auth_store  # noqa: F401  (fixture helper)


def _seed_two_key_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: "least_used",
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "testprovider": [
                    {
                        "id": "key-a",
                        "label": "first",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-first",
                        "request_count": 0,
                    },
                    {
                        "id": "key-b",
                        "label": "second",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-second",
                        "request_count": 0,
                    },
                ]
            },
        },
    )


def test_least_used_counter_persists_across_fresh_pool_loads(tmp_path, monkeypatch):
    """Each fresh load_pool()+select() must see the persisted counter and alternate."""
    _seed_two_key_pool(tmp_path, monkeypatch)
    from agent.credential_pool import load_pool

    picks = []
    for _ in range(4):
        pool = load_pool("testprovider")  # fresh pool from disk, like the gateway
        entry = pool.select()
        assert entry is not None
        picks.append(entry.id)

    assert picks == ["key-a", "key-b", "key-a", "key-b"], (
        f"least_used degenerated to a single entry across fresh pool loads: {picks}. "
        "request_count is not being persisted to auth.json on select."
    )

    # The disk store must carry the incremented counters, not just memory.
    auth = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    counts = {
        e["id"]: e.get("request_count", 0)
        for e in auth["credential_pool"]["testprovider"]
    }
    assert counts == {"key-a": 2, "key-b": 2}, (
        f"Persisted request_counts wrong: {counts}"
    )


def test_least_used_skewed_disk_counters_drive_selection(tmp_path, monkeypatch):
    """A fresh pool must honour counters persisted by a previous process."""
    _seed_two_key_pool(tmp_path, monkeypatch)
    from agent.credential_pool import load_pool

    # Simulate a previous process having used key-a 5 times, key-b once.
    auth_path = tmp_path / "hermes" / "auth.json"
    auth = json.loads(auth_path.read_text())
    for e in auth["credential_pool"]["testprovider"]:
        e["request_count"] = 5 if e["id"] == "key-a" else 1
    auth_path.write_text(json.dumps(auth))

    pool = load_pool("testprovider")
    entry = pool.select()
    assert entry is not None and entry.id == "key-b", (
        "Fresh pool ignored persisted request_count skew; strategy is not "
        "load-balancing across processes."
    )
