"""Tests for the shellctl file/image bridge (host + install glue).

Covers the image-vs-file classification + attach-hint emission in the
host-side ``hermes-shellbridge`` orchestrator, plus a smoke test of the
``hermes install shellctl`` asset/token setup. The bridge script has no
``.py`` extension (it is copied to the client verbatim), so it is loaded
from source via ``importlib``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

_ASSETS = (
    Path(__file__).resolve().parents[2]
    / "hermes_cli"
    / "shellctl_assets"
)


def _load_shellbridge():
    src = _ASSETS / "hermes-shellbridge"
    loader = importlib.machinery.SourceFileLoader(
        "hermes_shellbridge_under_test", str(src)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_shellbridge()


@pytest.mark.parametrize(
    "name",
    [
        "photo.png", "shot.JPG", "a.jpeg", "x.gif", "y.webp",
        "z.bmp", "scan.tiff", "scan.tif", "pic.heic", "pic.heif",
        "logo.svg",
    ],
)
def test_image_names_are_images(bridge, name):
    assert bridge._is_image_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["doc.pdf", "sheet.csv", "notes.txt", "a.zip", "bin", "", None],
)
def test_non_image_names_are_not_images(bridge, name):
    assert bridge._is_image_name(name) is False


def test_get_image_emits_image_hint(bridge, tmp_path, monkeypatch,
                                     capsys):
    """A pulled image lands in IMAGES_DIR + carries an `/image` hint."""
    images = tmp_path / "images"
    files = tmp_path / "files"
    monkeypatch.setattr(bridge, "IMAGES_DIR", str(images))
    monkeypatch.setattr(bridge, "FILES_DIR", str(files))

    class _Resp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        bridge, "_client_req",
        lambda *a, **k: _Resp(b"\x89PNG fake bytes"),
    )

    class _Args:
        path = "/home/user/screenshot.png"

    rc = bridge.cmd_get(_Args())
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert out["hint"].startswith("/image ")
    assert os.path.dirname(out["path"]) == str(images)
    assert out["bytes"] == len(b"\x89PNG fake bytes")


def test_get_non_image_emits_plain_hint(bridge, tmp_path, monkeypatch,
                                        capsys):
    """A pulled pdf lands in FILES_DIR + carries a plain-path hint."""
    images = tmp_path / "images"
    files = tmp_path / "files"
    monkeypatch.setattr(bridge, "IMAGES_DIR", str(images))
    monkeypatch.setattr(bridge, "FILES_DIR", str(files))

    class _Resp:
        def read(self):
            return b"%PDF-1.4 fake"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        bridge, "_client_req", lambda *a, **k: _Resp()
    )

    class _Args:
        path = "/home/user/report.pdf"

    rc = bridge.cmd_get(_Args())
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert not out["hint"].startswith("/image ")
    assert out["hint"] == out["path"]
    assert os.path.dirname(out["path"]) == str(files)


def test_install_shellctl_writes_assets_and_token(tmp_path,
                                                  monkeypatch, capsys):
    """`hermes install shellctl` copies assets + mints a 0600 token."""
    import argparse

    home = tmp_path / "hhome"
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib

    import hermes_cli.install_cmd as install_cmd
    importlib.reload(install_cmd)

    args = argparse.Namespace(
        print_client=False, port=8765, ssh_host="myhost"
    )
    rc = install_cmd.cmd_install_shellctl(args)
    assert rc == 0

    sc = home / "shellctl"
    assert (sc / "hermes-shellctl").is_file()
    assert (sc / "hermes-shellbridge").is_file()
    token = (sc / "bridge-token").read_text(encoding="utf-8").strip()
    assert len(token) >= 32
    # Token file is chmod 0600.
    mode = (sc / "bridge-token").stat().st_mode & 0o777
    assert mode == 0o600
    out = capsys.readouterr().out
    assert "RemoteForward 127.0.0.1:8765" in out
    assert token in out
