from pathlib import Path

from plugins.memory.obsidian import ObsidianMemoryProvider


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _provider(tmp_path, vault):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault), "top_k": 3})
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


def test_get_tool_schemas_empty_in_phase_a(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    assert _provider(tmp_path, vault).get_tool_schemas() == []
