import pytest
from pathlib import Path

from plugins.memory.obsidian_duo.contracts import MemoryRecord
from plugins.memory.obsidian_duo.vault import ObsidianVault

from plugins.memory.obsidian_duo.security import (
    assert_safe_to_persist,
    redact_secrets,
    scan_for_secrets,
)
from plugins.memory.obsidian_duo.contracts import EvidenceRecord, MemoryCandidate
from plugins.memory.obsidian_duo.broker import EmbeddedMemoryBroker
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.policy import MemoryPolicy
from plugins.memory.obsidian_duo.retrieval import MemoryRetriever
from plugins.memory.obsidian_duo.store import SqliteMemoryStore


def make_broker(tmp_path):
    config = ObsidianDuoConfig(str(tmp_path / "Vault"))
    store = SqliteMemoryStore(tmp_path / "memory.db")
    vault = ObsidianVault(Path(config.vault_path), config.managed_folder)
    return EmbeddedMemoryBroker(
        config=config,
        store=store,
        vault=vault,
        policy=MemoryPolicy(),
        retriever=MemoryRetriever(store),
    )


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "token=ghp_1234567890abcdefghijklmnop",
        "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnop",
        "API_KEY=sk-proj-1234567890abcdefghijklmnop",
        "client_secret=Zx9Qv7Lm2Nw4Rt6Yp8Ks3Hd5Fg7Jk9Lm",
    ],
)
def test_secret_patterns_are_blocked(text):
    result = scan_for_secrets(text)

    assert result.matches
    with pytest.raises(ValueError, match="secret credentials detected"):
        assert_safe_to_persist(text)
    assert redact_secrets(text) != text


def test_non_secret_identifiers_are_allowed():
    text = "uuid=550e8400-e29b-41d4-a716-446655440000 sha=0123456789abcdef0123456789abcdef01234567"

    assert not scan_for_secrets(text).matches
    assert_safe_to_persist(text)


def test_rejection_never_logs_raw_secret(caplog):
    secret = "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnop"

    with pytest.raises(ValueError):
        assert_safe_to_persist(secret)

    assert all(secret not in record.getMessage() for record in caplog.records)


def test_candidate_evidence_and_metadata_secrets_are_rejected_before_sqlite(tmp_path, caplog):
    broker = make_broker(tmp_path)
    broker.start()
    secret = "API_KEY=sk-proj-1234567890abcdefghijklmnop"

    decision = broker.propose(MemoryCandidate(
        "safe durable fact",
        evidence=(EvidenceRecord("ev-secret", "tool", secret),),
        metadata={"tool_output": {"raw": secret}},
    ))

    assert decision.action == "reject"
    db_blob = " ".join(
        str(row[0]) for table in ("candidates", "evidence", "memory_versions", "journal")
        for row in broker.store.connection().execute(f"SELECT * FROM {table}").fetchall()
    )
    assert secret not in db_blob
    assert all(secret not in record.getMessage() for record in caplog.records)
    broker.shutdown(5)


def _record(memory_type: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id="memory-1",
        content="safe",
        memory_type=memory_type,
        scope="global",
    )


@pytest.mark.parametrize("memory_type", ["../", "../../", "a/b", "a\\b", "/absolute", "C:\\outside"])
def test_managed_path_rejects_untrusted_memory_type(tmp_path: Path, memory_type: str):
    vault = ObsidianVault(tmp_path / "vault", "Hermes Memory")

    with pytest.raises(ValueError, match="memory_type"):
        vault._managed_path(_record(memory_type))


def test_managed_path_uses_canonical_folder_mapping(tmp_path: Path):
    vault = ObsidianVault(tmp_path / "vault", "Hermes Memory")

    assert vault._managed_path(_record("project")).parent.name == "Projects"
    assert vault._managed_path(_record("fact")).parent.name == "Entities"
    assert vault._managed_path(_record("candidate")).parent.name == "Inbox"


def test_managed_folder_must_be_relative_and_inside_vault(tmp_path: Path):
    with pytest.raises(ValueError, match="managed_folder"):
        ObsidianVault(tmp_path / "vault", "../outside")
    with pytest.raises(ValueError, match="managed_folder"):
        ObsidianVault(tmp_path / "vault", str(tmp_path / "outside"))


def test_scan_rejects_unsupported_memory_type_frontmatter(tmp_path: Path):
    vault = ObsidianVault(tmp_path / "vault", "Hermes Memory")
    store = __import__("plugins.memory.obsidian_duo.store", fromlist=["SqliteMemoryStore"]).SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    path = vault.managed_root / "Entities" / "bad.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nmemory_id: bad\nmemory_type: ../outside\nscope: global\n---\nunsafe\n",
        encoding="utf-8",
    )

    result = vault.scan_managed_changes(store)

    assert result.reparsed_paths == ()
    assert result.malformed_paths == (path,)
