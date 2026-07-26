"""Regression coverage for Windows MSYS/Git Bash vision paths."""

from __future__ import annotations

import base64
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _reload(monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_constants

    importlib.reload(hermes_constants)
    import tools.image_source as isrc

    importlib.reload(isrc)
    return isrc


def test_translate_msys_path_uses_cygpath_without_shell(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return SimpleNamespace(stdout=r"C:\Users\Tony\Pictures\pic.png" + "\n")

    translated = isrc._translate_msys_path(
        "/c/Users/Tony/Pictures/pic.png",
        is_windows=True,
        run=fake_run,
    )

    assert translated == r"C:\Users\Tony\Pictures\pic.png"
    assert calls["argv"] == ["cygpath", "-w", "/c/Users/Tony/Pictures/pic.png"]
    assert calls["kwargs"] == {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 5,
    }


def test_translate_msys_path_skips_non_windows(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")

    def fail_run(*_args, **_kwargs):
        pytest.fail("cygpath must not run outside Windows")

    assert (
        isrc._translate_msys_path(
            "/workspace/pic.png",
            is_windows=False,
            run=fail_run,
        )
        is None
    )


@pytest.mark.asyncio
async def test_local_backend_reads_translated_msys_path(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    image = tmp_path / "translated.png"
    image.write_bytes(PNG)

    monkeypatch.setattr(isrc, "_looks_like_msys_path", lambda _candidate: True)
    monkeypatch.setattr(isrc, "_translate_msys_path", lambda _candidate: str(image))

    result = await isrc.resolve_image_source(
        "/c/Users/Tony/Pictures/pic.png",
        isrc.ResolveContext(),
    )

    assert result.data == PNG
    assert result.mime == "image/png"
    assert result.origin == "file"


@pytest.mark.asyncio
async def test_non_local_backend_keeps_container_path(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    def fail_translate(_candidate):
        pytest.fail("container paths must never pass through cygpath")

    monkeypatch.setattr(isrc, "_translate_msys_path", fail_translate)
    encoded = base64.b64encode(PNG).decode()

    with patch(
        "tools.image_source._get_active_env",
        return_value=SimpleNamespace(
            execute=lambda _command: {"returncode": 0, "output": encoded}
        ),
    ):
        result = await isrc.resolve_image_source(
            "/workspace/pic.png",
            isrc.ResolveContext(task_id="task-1"),
        )

    assert result.data == PNG
    assert result.origin == "container"


@pytest.mark.asyncio
async def test_failed_msys_translation_has_actionable_error(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(isrc, "_looks_like_msys_path", lambda _candidate: True)
    monkeypatch.setattr(isrc, "_translate_msys_path", lambda _candidate: None)

    with pytest.raises(isrc.SourceNotFound, match="cygpath"):
        await isrc.resolve_image_source(
            "/c/Users/Tony/Pictures/missing.png",
            isrc.ResolveContext(),
        )
