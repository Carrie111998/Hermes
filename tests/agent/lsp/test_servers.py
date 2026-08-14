"""Regression tests for LSP executable resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.lsp.servers import ServerContext, _spawn_pyright


@pytest.mark.parametrize("suffix", [".cmd", ".exe"])
def test_pyright_uses_native_langserver_sibling(tmp_path: Path, monkeypatch, suffix: str):
    official = tmp_path / "pyright"
    sibling = tmp_path / f"pyright-langserver{suffix}"
    official.write_text("#!/bin/sh\n", encoding="utf-8")
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")
    official.chmod(0o755)
    sibling.chmod(0o755)

    def fake_which(name: str):
        return str(official) if name == "pyright" else None

    monkeypatch.setattr("agent.lsp.servers.shutil.which", fake_which)
    monkeypatch.setattr("agent.lsp.install.try_install", lambda *args, **kwargs: None)

    spec = _spawn_pyright(
        str(tmp_path),
        ServerContext(workspace_root=str(tmp_path), install_strategy="off"),
    )

    assert spec is not None
    assert spec.command == [str(sibling), "--stdio"]


def test_pyright_does_not_fallback_to_official_cli(tmp_path: Path, monkeypatch):
    official = tmp_path / "pyright"
    official.write_text("#!/bin/sh\n", encoding="utf-8")
    official.chmod(0o755)

    monkeypatch.setattr(
        "agent.lsp.servers.shutil.which",
        lambda name: str(official) if name == "pyright" else None,
    )
    monkeypatch.setattr("agent.lsp.install.try_install", lambda *args, **kwargs: None)

    spec = _spawn_pyright(
        str(tmp_path),
        ServerContext(workspace_root=str(tmp_path), install_strategy="off"),
    )

    assert spec is None


def test_pyright_rejects_noncanonical_langserver_wrapper(tmp_path: Path, monkeypatch):
    wrapper = tmp_path / "pyright-langserver-wrapper"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)

    monkeypatch.setattr(
        "agent.lsp.servers.shutil.which",
        lambda name: str(wrapper) if name == "pyright-langserver" else None,
    )
    monkeypatch.setattr("agent.lsp.install.try_install", lambda *args, **kwargs: None)

    spec = _spawn_pyright(
        str(tmp_path),
        ServerContext(workspace_root=str(tmp_path), install_strategy="off"),
    )

    assert spec is None