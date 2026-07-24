import json
import threading
from pathlib import Path

from plugins.memory.obsidian import ObsidianMemoryProvider


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _provider(tmp_path, vault):
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "top_k": 3, "sync_interval_minutes": 0}
    )
    p.initialize(session_id="s", hermes_home=str(tmp_path))
    return p


def test_name_and_available(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    p = _provider(tmp_path, vault)
    assert p.name == "obsidian"
    assert p.is_available() is True


def test_prefetch_returns_relevant_note(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "forsakringar" / "bil.md", "# Bilförsäkring\nFolksam helförsäkring")
    p = _provider(tmp_path, vault)
    out = p.prefetch("vad har jag för bilförsäkring?")
    assert "Bilförsäkring" in out or "Folksam" in out
    assert "forsakringar/bil.md" in out


def test_prefetch_empty_when_no_match(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "a.md", "# A\nkaffe")
    p = _provider(tmp_path, vault)
    assert p.prefetch("kvantkromodynamik") == ""


def test_index_db_created_outside_vault(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    _provider(tmp_path, vault)
    assert (tmp_path / "obsidian_index.db").exists()
    assert not (vault / "obsidian_index.db").exists()


def test_get_tool_schemas_exposes_remember_only(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    schemas = _provider(tmp_path, vault).get_tool_schemas()
    assert [schema["name"] for schema in schemas] == ["obsidian_remember"]
    assert schemas[0]["parameters"]["required"] == ["content"]


def test_prefetch_works_from_background_thread(tmp_path):
    # Reproduces the MemoryManager per-turn background-thread prefetch.
    vault = tmp_path / "vault"
    (vault / "forsakringar").mkdir(parents=True)
    (vault / "forsakringar" / "bil.md").write_text(
        "# Bilförsäkring\nFolksam helförsäkring", encoding="utf-8"
    )
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "top_k": 3, "sync_interval_minutes": 0}
    )
    p.initialize(session_id="s", hermes_home=str(tmp_path))  # init on THIS thread

    result = {}

    def _bg():
        result["out"] = p.prefetch("bilförsäkring folksam")  # prefetch on ANOTHER thread

    t = threading.Thread(target=_bg)
    t.start(); t.join()
    assert "Bilförsäkring" in result["out"] or "Folksam" in result["out"]


def test_sync_once_picks_up_vault_edit(tmp_path):
    vault = tmp_path / "vault"
    note = vault / "project.md"
    _write(note, "# Projekt\nFörsta versionen")
    p = _provider(tmp_path, vault)

    _write(note, "# Projekt\nAndra versionen med zebrakod")
    summary = p._sync_once()

    assert summary["updated"] == 1
    assert "zebrakod" in p.prefetch("zebrakod")


def test_shutdown_stops_resync_thread(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "sync_interval_minutes": 0.001}
    )
    p.initialize(session_id="s", hermes_home=str(tmp_path))
    thread = p._sync_thread
    assert thread is not None and thread.is_alive()

    p.shutdown()

    assert not thread.is_alive()


def test_system_prompt_block_reads_pinned_note_and_strips_frontmatter(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "memory" / "daniel.md", "---\ntags: [person]\n---\n# Daniel\nGillar korta svar.")
    p = ObsidianMemoryProvider(
        config={
            "vault_path": str(vault),
            "pinned": ["memory/daniel.md"],
            "sync_interval_minutes": 0,
        }
    )

    block = p.system_prompt_block()

    assert "Gillar korta svar" in block
    assert "tags: [person]" not in block
    assert "memory/daniel.md" in block


def test_system_prompt_block_skips_missing_and_truncates(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "large.md", "x" * 5000)
    p = ObsidianMemoryProvider(
        config={
            "vault_path": str(vault),
            "pinned": ["missing.md", "large.md"],
            "sync_interval_minutes": 0,
        }
    )

    block = p.system_prompt_block()

    assert "missing.md" not in block
    assert "[trunkerad]" in block
    assert len(block) < 4300


def test_system_prompt_block_scrubs_secrets(tmp_path):
    vault = tmp_path / "vault"
    secret = "ghp_abc123def456ghi789jklmno"
    _write(vault / "pinned.md", f"Token: {secret}")
    p = ObsidianMemoryProvider(
        config={
            "vault_path": str(vault),
            "pinned": ["pinned.md"],
            "sync_interval_minutes": 0,
        }
    )

    block = p.system_prompt_block()

    assert secret not in block
    assert "***" in block


def test_remember_writes_scrubbed_note_and_indexes_immediately(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    p = _provider(tmp_path, vault)

    raw = p.handle_tool_call(
        "obsidian_remember",
        {
            "content": "Deploy-token: ghp_abc123def456ghi789jklmno",
            "title": "Deployment lärdom",
            "tags": ["projekt", "drift"],
        },
    )
    result = json.loads(raw)
    note = vault / result["path"]
    text = note.read_text(encoding="utf-8")

    assert "error" not in result
    assert result["secret_redacted"] is True
    assert "ghp_abc123def456ghi789jklmno" not in text
    assert "source: hermes" in text
    assert "Deployment lärdom" in text
    assert "projekt" in text
    assert "Deployment lärdom" in p.prefetch("deployment lärdom")


def test_remember_title_cannot_escape_namespace(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    p = _provider(tmp_path, vault)

    result = json.loads(
        p.handle_tool_call(
            "obsidian_remember", {"content": "säker fakta", "title": "../../.env"}
        )
    )

    assert "error" not in result
    assert result["path"].startswith("hermes/")
    assert ".." not in result["path"]
    assert not (tmp_path / ".env").exists()


def test_remember_refuses_gitignored_namespace(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    _write(vault / ".gitignore", "hermes/\n")
    p = _provider(tmp_path, vault)

    result = json.loads(
        p.handle_tool_call("obsidian_remember", {"content": "ska inte skrivas"})
    )

    assert "error" in result
    assert "gitignore" in result["error"].lower()
    assert not (vault / "hermes").exists()
