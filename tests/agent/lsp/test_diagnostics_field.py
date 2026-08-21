"""Tests for the ``lsp_diagnostics`` field on WriteResult / PatchResult.

The field exists so the agent can read syntax errors (``lint``) and
semantic errors (``lsp_diagnostics``) as separate signals rather than
having LSP output prepended to the lint string.
"""
from __future__ import annotations

from unittest.mock import patch


from tools.environments.local import LocalEnvironment
from tools.file_operations import (
    PatchResult,
    ShellFileOperations,
    WriteResult,
)


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------










def test_patchresult_to_dict_omits_field_when_none():
    r = PatchResult(success=True)
    assert "lsp_diagnostics" not in r.to_dict()




# ---------------------------------------------------------------------------
# Channel separation: lint and lsp_diagnostics stay independent
# ---------------------------------------------------------------------------


def test_lint_and_lsp_diagnostics_are_separate_channels():
    """A WriteResult can carry BOTH a syntax-error lint AND an LSP
    diagnostic block.  They belong in separate fields."""
    r = WriteResult(
        bytes_written=42,
        lint={"status": "error", "output": "SyntaxError: ..."},
        lsp_diagnostics="<diagnostics>ERROR [1:5] type mismatch</diagnostics>",
    )
    d = r.to_dict()
    assert "lint" in d
    assert "lsp_diagnostics" in d
    assert d["lint"]["output"] == "SyntaxError: ..."
    assert "type mismatch" in d["lsp_diagnostics"]


# ---------------------------------------------------------------------------
# write_file populates the field via _maybe_lsp_diagnostics
# ---------------------------------------------------------------------------






def test_write_file_skips_lsp_when_syntax_failed(tmp_path):
    """If the syntax check finds errors, the LSP layer should not be
    consulted (a file that won't parse won't yield meaningful semantic
    diagnostics)."""
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "broken.py"

    with patch.object(fops, "_maybe_lsp_diagnostics") as mock_lsp:
        res = fops.write_file(str(target), "def x(:\n")  # syntax error
    assert mock_lsp.call_count == 0
    assert res.lsp_diagnostics is None
    assert res.lint["status"] == "error"


# ---------------------------------------------------------------------------
# patch_replace propagates the field from the inner write_file
# ---------------------------------------------------------------------------


def test_patch_replace_propagates_lsp_diagnostics(tmp_path):
    """patch_replace's internal write_file populates lsp_diagnostics —
    the outer PatchResult must carry it forward."""
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    target.write_text("x = 1\n")

    block = "<diagnostics>ERROR [1:5] semantic issue</diagnostics>"

    with patch.object(fops, "_maybe_lsp_diagnostics", return_value=block):
        res = fops.patch_replace(str(target), "x = 1", "x = 2")

    assert res.success is True
    assert res.lsp_diagnostics == block


# ---------------------------------------------------------------------------
# enabled_for raising (deleted-CWD workspace resolution) must not break a write
# ---------------------------------------------------------------------------


class _ExplodingService:
    """Mimics LSPService.enabled_for crashing — as when
    resolve_workspace_for_file -> os.getcwd() raises ENOENT because the
    process CWD was deleted mid-run (t_886b35f5)."""

    def enabled_for(self, path):
        raise FileNotFoundError(2, "No such file or directory")


def test_maybe_lsp_diagnostics_swallows_enabled_for_crash(tmp_path):
    """enabled_for raising must return '' (no diagnostics), never propagate
    — the documented contract is that LSP can't break a write."""
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    with patch("agent.lsp.get_service", return_value=_ExplodingService()):
        block = fops._maybe_lsp_diagnostics(str(tmp_path / "x.py"))
    assert block == ""


def test_write_file_survives_lsp_enabled_for_crash(tmp_path):
    """End-to-end: a .py write with a crashing LSP enabled_for must succeed
    and land the file — the write itself is complete before diagnostics run."""
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    with patch("agent.lsp.get_service", return_value=_ExplodingService()):
        res = fops.write_file(str(target), "x = 1\n")
    assert res.error is None
    assert res.bytes_written == len("x = 1\n")
    assert target.read_text() == "x = 1\n"
