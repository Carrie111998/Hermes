"""Tests for tools/image_source.py — the unified vision image-source resolver.

Covers the delivery contract (data:/http/file/local/container source handling,
size cap, magic-byte sniff) AND the terminal-backend confinement security model
(GHSA-gpxw-6wxv-w3qq): under a non-local backend, host reads are confined to the
media caches and every other path is read inside the sandbox via exec-read.
"""

import base64
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64


def _reload(monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import tools.image_source as isrc
    importlib.reload(isrc)
    return isrc


class TestDataUrl:
    @pytest.mark.asyncio
    async def test_valid_data_url_resolves_to_bytes(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        b64 = base64.b64encode(PNG).decode()
        res = await isrc.resolve_image_source(
            f"data:image/png;base64,{b64}", isrc.ResolveContext())
        assert res.data == PNG
        assert res.mime == "image/png"
        assert res.origin == "data"

    @pytest.mark.asyncio
    async def test_non_image_data_url_rejected(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        b64 = base64.b64encode(b"not an image").decode()
        with pytest.raises(isrc.NotAnImage):
            await isrc.resolve_image_source(
                f"data:text/plain;base64,{b64}", isrc.ResolveContext())


class TestLocalBackend:
    @pytest.mark.asyncio
    async def test_local_backend_reads_any_host_path(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        img = tmp_path / "outside" / "pic.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(PNG)
        res = await isrc.resolve_image_source(str(img), isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"


    @pytest.mark.asyncio
    async def test_bare_relative_path_resolves(self, tmp_path, monkeypatch):
        """A cwd-relative bare filename ('pic.png') is a valid local source —
        main accepted it; the resolver must not regress it (PR review)."""
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        img = tmp_path / "pic.png"
        img.write_bytes(PNG)
        monkeypatch.chdir(tmp_path)
        res = await isrc.resolve_image_source("pic.png", isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"


    @pytest.mark.asyncio
    async def test_svg_passes_through_for_rasterization(self, tmp_path, monkeypatch):
        """SVG has no raster magic bytes but is passed through with mime
        image/svg+xml so the vision call sites can rasterize it to PNG."""
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        svg = tmp_path / "art.svg"
        svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        svg.write_bytes(svg_bytes)
        res = await isrc.resolve_image_source(str(svg), isrc.ResolveContext())
        assert res.mime == "image/svg+xml"
        assert res.data == svg_bytes


class TestNonLocalBackendConfinement:
    """The security model: under a sandbox backend, host reads are confined to
    the media caches; every other path is read inside the sandbox."""

    @pytest.mark.asyncio
    async def test_media_cache_path_host_read(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        cached = home / "cache" / "images" / "inbound.png"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(PNG)
        # No sandbox env needed — a cache path is host-read directly.
        res = await isrc.resolve_image_source(str(cached), isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"

    @pytest.mark.asyncio
    async def test_host_secret_outside_cache_routes_to_sandbox_not_host(self, tmp_path, monkeypatch):
        """A non-cache host path (e.g. /etc/passwd) must NOT be host-read — it
        routes to the in-sandbox exec-read, which reads the CONTAINER's file."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        # A real host file outside the caches, holding a "secret".
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"HOST-PRIVATE-KEY-DO-NOT-LEAK")

        # Fake sandbox env: its exec-read returns a *different* (container) image,
        # proving we read the container filesystem, not the host secret.
        container_png_b64 = base64.b64encode(PNG).decode()
        calls = {}

        def fake_execute(cmd, **kw):
            calls["cmd"] = cmd
            return {"returncode": 0, "output": container_png_b64}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            res = await isrc.resolve_image_source(str(secret), isrc.ResolveContext(task_id="t1"))

        # Read came from the sandbox exec-read, returning the container image —
        # the host secret bytes never appear.
        assert res.origin == "container"
        assert res.data == PNG
        assert b"HOST-PRIVATE-KEY" not in res.data
        assert "head -c" in calls["cmd"] and "< " in calls["cmd"]  # bounded, redirect-safe form

    @pytest.mark.asyncio
    async def test_non_cache_path_fails_closed_without_sandbox(self, tmp_path, monkeypatch):
        """No active sandbox env -> refuse rather than fall back to a host read."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"HOST-PRIVATE-KEY")

        with patch("tools.image_source._get_active_env", return_value=None):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(str(secret), isrc.ResolveContext(task_id="t1"))

    @pytest.mark.asyncio
    async def test_symlink_in_cache_pointing_outside_is_not_host_read(self, tmp_path, monkeypatch):
        """A symlink planted inside a cache dir that points at a host secret must
        not be host-read (resolve() escapes the cache) — it routes to sandbox."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        secret = tmp_path / "outside" / "id_rsa"
        secret.parent.mkdir(parents=True)
        secret.write_bytes(b"HOST-PRIVATE-KEY")
        cache_dir = home / "cache" / "images"
        cache_dir.mkdir(parents=True)
        link = cache_dir / "sneaky.png"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported")

        # Fails closed (no sandbox) rather than host-reading the symlink target.
        with patch("tools.image_source._get_active_env", return_value=None):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(str(link), isrc.ResolveContext(task_id="t1"))


class TestExecReadSafety:
    @pytest.mark.asyncio
    async def test_exec_read_is_bounded_and_redirect_safe(self, tmp_path, monkeypatch):
        """Leading-dash paths go through an input redirect (no argv exposure)
        and the read is size-bounded via head -c."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        captured = {}

        def fake_execute(cmd, **kw):
            captured["cmd"] = cmd
            return {"returncode": 0, "output": base64.b64encode(PNG).decode()}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            await isrc.resolve_image_source(
                "/workspace/-i-etc-shadow.png", isrc.ResolveContext(task_id="t1"))
        assert f"head -c {isrc._MAX_INGEST_BYTES + 1} < " in captured["cmd"]
        assert "'-i-etc-shadow.png'" in captured["cmd"] or "-i-etc-shadow.png" in captured["cmd"]


    @pytest.mark.asyncio
    async def test_exec_read_nonzero_returncode_raises(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        def fake_execute(cmd, **kw):
            return {"returncode": 1, "output": ""}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(
                    "/workspace/nope.png", isrc.ResolveContext(task_id="t1"))


class TestSvgNormalization:
    """SVG resolves end-to-end: the resolver passes it through as
    image/svg+xml and the vision call sites rasterize it to PNG via
    _normalize_to_supported_image (PR #52688, folded in)."""

    @pytest.mark.asyncio
    async def test_svg_rasterized_when_converter_available(self, tmp_path, monkeypatch):
        from tools import vision_tools as vt
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')

        def fake_rasterize(svg_path, out_path):
            out_path.write_bytes(PNG)
            return True

        with patch.object(vt, "_rasterize_svg_to_png", side_effect=fake_rasterize):
            res = await isrc.resolve_image_source(str(svg), isrc.ResolveContext())
            assert res.mime == "image/svg+xml"
            path, mime, err = vt._normalize_to_supported_image(svg, "image/svg+xml")
        assert err is None
        assert mime == "image/png"
        assert path.read_bytes() == PNG
        path.unlink()

    def test_svg_actionable_error_when_no_converter(self, tmp_path, monkeypatch):
        from tools import vision_tools as vt
        _reload(monkeypatch, tmp_path / "hermes")
        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with patch.object(vt, "_rasterize_svg_to_png", return_value=False):
            path, mime, err = vt._normalize_to_supported_image(svg, "image/svg+xml")
        assert path is None
        assert "rasterizer" in err

    @pytest.mark.parametrize(
        "content,error",
        [
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<script>alert(1)</script></svg>',
                "active",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="urn:test">'
                b"<x:script/></svg>",
                "active",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
                "event-handler",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<image href="https://example.com/tracker.png"/></svg>',
                "external references",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<image href="&#x68;ttps://example.com/tracker.png"/></svg>',
                "external references",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<image href="data:image/svg+xml;base64,PHN2Zy8+"/></svg>',
                "external references",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<style>@import url("https://example.com/style.css");</style></svg>',
                "external CSS",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<rect fill="url(https://example.com/pattern.svg)"/></svg>',
                "external CSS",
            ),
            (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<rect fill="u/**/rl(https://example.com/pattern.svg)"/></svg>',
                "external CSS",
            ),
            (
                b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>',
                "declarations",
            ),
        ],
    )
    def test_svg_unsafe_content_rejected_before_renderer(
        self, tmp_path, monkeypatch, content, error
    ):
        from tools import vision_tools as vt
        _reload(monkeypatch, tmp_path / "hermes")
        svg = tmp_path / "unsafe.svg"
        svg.write_bytes(content)

        with patch.object(vt, "_rasterize_svg_to_png") as rasterize:
            path, mime, err = vt._normalize_to_supported_image(
                svg, "image/svg+xml"
            )

        assert path is None
        assert mime is None
        assert error in err
        rasterize.assert_not_called()

    def test_svg_allows_local_fragments_and_embedded_raster(
        self, tmp_path, monkeypatch
    ):
        from tools import vision_tools as vt
        _reload(monkeypatch, tmp_path / "hermes")
        svg = tmp_path / "self-contained.svg"
        svg.write_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<defs><clipPath id="clip"><rect width="4" height="4"/>'
            b'</clipPath></defs><image href="data:image/png;base64,iVBORw0KGgo="/>'
            b'<rect width="4" height="4" clip-path="url(#clip)"/></svg>'
        )

        with patch.object(vt, "_rasterize_svg_to_png") as rasterize:
            rasterize.side_effect = lambda _source, output: (
                output.write_bytes(PNG) is not None
            )
            path, mime, err = vt._normalize_to_supported_image(
                svg, "image/svg+xml"
            )

        assert err is None
        assert mime == "image/png"
        assert path.read_bytes() == PNG
        path.unlink()

    def test_stock_macos_quicklook_rasterizer(self, tmp_path):
        import sys
        from tools import vision_tools as vt

        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        output = tmp_path / "art.png"

        def fake_which(command):
            return "/usr/bin/qlmanage" if command == "qlmanage" else None

        def fake_run(command, **_kwargs):
            preview_dir = Path(command[command.index("-o") + 1])
            (preview_dir / "art.svg.png").write_bytes(PNG)
            return SimpleNamespace(returncode=0)

        with (
            patch.dict(
                sys.modules,
                {"cairosvg": None, "svglib": None, "svglib.svglib": None},
            ),
            patch("shutil.which", side_effect=fake_which),
            patch("subprocess.run", side_effect=fake_run) as run,
        ):
            assert vt._rasterize_svg_to_png(svg, output) is True

        assert output.read_bytes() == PNG
        assert run.call_args.args[0][0] == "qlmanage"

    def test_quicklook_invalid_output_is_rejected(self, tmp_path):
        import sys
        from tools import vision_tools as vt

        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        output = tmp_path / "art.png"

        def fake_which(command):
            return "/usr/bin/qlmanage" if command == "qlmanage" else None

        def fake_run(command, **_kwargs):
            preview_dir = Path(command[command.index("-o") + 1])
            (preview_dir / "art.svg.png").write_bytes(b"not a PNG")
            return SimpleNamespace(returncode=0)

        with (
            patch.dict(
                sys.modules,
                {"cairosvg": None, "svglib": None, "svglib.svglib": None},
            ),
            patch("shutil.which", side_effect=fake_which),
            patch("subprocess.run", side_effect=fake_run),
        ):
            assert vt._rasterize_svg_to_png(svg, output) is False

        assert not output.exists()
