"""Hermes direct-write guard for David's ConsuConstruct vault Markdown.

The Brain MCP `vault_note_write` / `obsidian_write` path is the only sanctioned
writer for vault .md files because it enforces planning_complete, reason, canon
frontmatter/CONTEXT, and a Postgres audit event.
"""

import json
from pathlib import Path

from tools import file_tools
from tools.file_tools import patch_tool, write_file_tool


def _set_fake_vault(monkeypatch, tmp_path: Path) -> Path:
    vault = tmp_path / "My Brain" / "ConsuConstruct"
    vault.mkdir(parents=True)
    monkeypatch.setattr(file_tools, "_CONSUCONSTRUCT_VAULT_ROOT", vault.resolve())
    return vault


class TestConsuConstructVaultMarkdownWriteGuard:
    def test_write_file_rejects_md_inside_vault(self, tmp_path: Path, monkeypatch):
        vault = _set_fake_vault(monkeypatch, tmp_path)
        target = vault / "00 Notes" / "audit.md"

        result = json.loads(write_file_tool(str(target), "---\nstatus: active\n---\n"))

        assert result.get("error")
        assert "ConsuConstruct vault Markdown" in result["error"]
        assert "vault_note_write" in result["error"]
        assert not target.exists()

    def test_write_file_allows_non_markdown_inside_vault(self, tmp_path: Path, monkeypatch):
        vault = _set_fake_vault(monkeypatch, tmp_path)
        target = vault / "00 Notes" / "asset.txt"

        result = json.loads(write_file_tool(str(target), "plain text artifact"))

        assert not result.get("error")
        assert target.read_text(encoding="utf-8") == "plain text artifact"

    def test_write_file_allows_markdown_outside_vault(self, tmp_path: Path, monkeypatch):
        _set_fake_vault(monkeypatch, tmp_path)
        target = tmp_path / "outside.md"

        result = json.loads(write_file_tool(str(target), "# outside"))

        assert not result.get("error")
        assert target.read_text(encoding="utf-8") == "# outside"

    def test_patch_replace_rejects_md_inside_vault(self, tmp_path: Path, monkeypatch):
        vault = _set_fake_vault(monkeypatch, tmp_path)
        target = vault / "00 Notes" / "existing.md"
        target.parent.mkdir(parents=True)
        target.write_text("old", encoding="utf-8")

        result = json.loads(patch_tool(path=str(target), old_string="old", new_string="new"))

        assert result.get("error")
        assert "ConsuConstruct vault Markdown" in result["error"]
        assert target.read_text(encoding="utf-8") == "old"

    def test_v4a_patch_rejects_md_inside_vault(self, tmp_path: Path, monkeypatch):
        vault = _set_fake_vault(monkeypatch, tmp_path)
        target = vault / "00 Notes" / "v4a.md"
        patch = f"""*** Begin Patch
*** Add File: {target}
+# v4a
*** End Patch
"""

        result = json.loads(patch_tool(mode="patch", patch=patch))

        assert result.get("error")
        assert "ConsuConstruct vault Markdown" in result["error"]
        assert not target.exists()
