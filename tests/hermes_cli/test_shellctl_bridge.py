"""Tests for the shellctl file/image bridge and install glue."""
from __future__ import annotations

import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import threading
from urllib.parse import quote

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "hermes_cli" / "shellctl_assets"


def _load_asset(name: str, module_name: str):
    src = _ASSETS / name
    loader = importlib.machinery.SourceFileLoader(module_name, str(src))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_asset("hermes-shellbridge", "hermes_shellbridge_under_test")


@pytest.fixture
def shellctl_server(tmp_path):
    """Run the real stdlib HTTP handler on an OS-assigned port."""
    mod = _load_asset("hermes-shellctl", "hermes_shellctl_under_test")
    mod._TOKEN = "test-token"
    mod._DOWNLOAD_DIR = str(tmp_path / "downloads")
    mod._ALLOWED_ROOT = ""
    server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod._Handler)
    assert server.server_port != 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield mod, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http(port, method, path, body=None, token="test-token", headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["X-Shellctl-Token"] = token
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=request_headers)
    response = conn.getresponse()
    payload = response.read()
    status = response.status
    conn.close()
    return status, payload


@pytest.mark.parametrize(
    "name",
    [
        "photo.png", "shot.JPG", "a.jpeg", "x.gif", "y.webp", "z.bmp",
        "scan.tiff", "scan.tif", "pic.heic", "pic.heif", "logo.svg",
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


def test_get_image_emits_image_hint(bridge, tmp_path, monkeypatch, capsys):
    images = tmp_path / "images"
    files = tmp_path / "files"
    monkeypatch.setattr(bridge, "IMAGES_DIR", str(images))
    monkeypatch.setattr(bridge, "FILES_DIR", str(files))

    class _Resp:
        def read(self):
            return b"\x89PNG fake bytes"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(bridge, "_client_req", lambda *args, **kwargs: _Resp())

    class _Args:
        path = "/home/user/screenshot.png"

    assert bridge.cmd_get(_Args()) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["hint"].startswith("/image ")
    assert os.path.dirname(out["path"]) == str(images)
    assert out["bytes"] == len(b"\x89PNG fake bytes")


def test_get_non_image_emits_plain_hint(bridge, tmp_path, monkeypatch, capsys):
    images = tmp_path / "images"
    files = tmp_path / "files"
    monkeypatch.setattr(bridge, "IMAGES_DIR", str(images))
    monkeypatch.setattr(bridge, "FILES_DIR", str(files))

    class _Resp:
        def read(self):
            return b"%PDF-1.4 fake"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(bridge, "_client_req", lambda *args, **kwargs: _Resp())

    class _Args:
        path = "/home/user/report.pdf"

    assert bridge.cmd_get(_Args()) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["hint"] == out["path"]
    assert os.path.dirname(out["path"]) == str(files)


def test_host_pull_deduplicates_existing_name(bridge, tmp_path):
    first = Path(bridge._save_into(str(tmp_path), b"one", "same.txt"))
    second = Path(bridge._save_into(str(tmp_path), b"two", "same.txt"))
    assert first.name == "same.txt"
    assert second.name == "same-1.txt"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_http_authentication_and_pull(shellctl_server, tmp_path):
    mod, port = shellctl_server
    status, _ = _http(port, "GET", "/ping", token=None)
    assert status == 401
    status, payload = _http(port, "GET", "/ping")
    assert status == 200
    assert json.loads(payload)["ok"] is True

    source = tmp_path / "arbitrary.txt"
    source.write_bytes(b"arbitrary client bytes")
    status, payload = _http(port, "GET", "/pull?path=" + quote(str(source)))
    assert status == 200
    assert payload == source.read_bytes()

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    mod._ALLOWED_ROOT = os.path.realpath(allowed)
    status, _ = _http(port, "GET", "/pull?path=" + quote(str(source)))
    assert status == 403

    symlink = allowed / "escape.txt"
    try:
        symlink.symlink_to(source)
    except OSError:
        pass
    else:
        status, _ = _http(port, "GET", "/pull?path=" + quote(str(symlink)))
        assert status == 403


def test_http_push_deduplicates_collisions(shellctl_server):
    mod, port = shellctl_server
    status, first = _http(
        port, "POST", "/push?name=result.txt&open=0", b"one"
    )
    assert status == 200
    status, second = _http(
        port, "POST", "/push?name=result.txt&open=0", b"two"
    )
    assert status == 200
    first_path = Path(json.loads(first)["path"])
    second_path = Path(json.loads(second)["path"])
    assert first_path != second_path
    assert first_path.read_bytes() == b"one"
    assert second_path.read_bytes() == b"two"
    assert second_path.name == "result-1.txt"
    assert first_path.parent == Path(mod._DOWNLOAD_DIR)


def test_http_oversized_and_malformed_bodies(shellctl_server):
    mod, port = shellctl_server
    status, payload = _http(
        port,
        "POST",
        "/push?name=huge.bin&open=0",
        headers={"Content-Length": str(mod._MAX_BYTES + 1)},
    )
    assert status == 413
    assert json.loads(payload)["error"] == "request body too large"

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/push?name=bad.bin&open=0")
    conn.putheader("X-Shellctl-Token", "test-token")
    conn.putheader("Content-Length", "not-a-number")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400
    assert json.loads(response.read())["error"] == "invalid Content-Length"
    conn.close()


def test_install_cli_parser_accepts_shellctl_options():
    import argparse

    from hermes_cli.install_cmd import register_cli

    parser = argparse.ArgumentParser()
    install_parser = parser.add_subparsers(dest="command", required=True).add_parser(
        "install"
    )
    register_cli(install_parser)
    args = parser.parse_args(
        [
            "install", "shellctl", "--port", "9001", "--ssh-host", "remote",
            "--allowed-root", "/safe/files",
        ]
    )
    assert args.install_target == "shellctl"
    assert args.port == 9001
    assert args.ssh_host == "remote"
    assert args.allowed_root == "/safe/files"


def test_install_shellctl_writes_assets_and_token(tmp_path, capsys, monkeypatch):
    import argparse

    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    import hermes_cli.install_cmd as install_cmd
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"shellctl": {"max_open_capture_secs": 42}},
    )

    home = tmp_path / "profile-home"
    override = set_hermes_home_override(home)
    try:
        args = argparse.Namespace(
            print_client=False,
            port=8765,
            ssh_host="myhost",
            allowed_root="~/shared",
            max_open_capture_secs=None,
        )
        assert install_cmd.cmd_install_shellctl(args) == 0
    finally:
        reset_hermes_home_override(override)

    sc = home / "shellctl"
    assert (sc / "hermes-shellctl").is_file()
    assert (sc / "hermes-shellbridge").is_file()
    token = (sc / "bridge-token").read_text(encoding="utf-8").strip()
    assert len(token) >= 32
    assert (sc / "bridge-token").stat().st_mode & 0o777 == 0o600
    assert (sc / "bridge.env").stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "RemoteForward 127.0.0.1:8765" in out
    assert "ExitOnForwardFailure yes" in out
    assert "--token-file ~/.hermes-shellctl-token" in out
    assert "--max-open-capture-secs 42" in out
    assert "--allowed-root '~/shared'" in out
    assert token not in out
