"""Request-time dashboard bundle recovery coverage (#82614/#82666)."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import update_cmd, web_server


def test_spa_and_assets_recover_when_dist_appears_after_mount(tmp_path, monkeypatch):
    application = FastAPI()
    dist = tmp_path / "web_dist"
    monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
    monkeypatch.setattr(web_server, "WEB_DIST", dist)

    web_server.mount_spa(application)

    with TestClient(application) as client:
        missing_index = client.get("/")
        missing_asset = client.get("/assets/app-deadbeef.js")

        assert missing_index.status_code == 404
        assert missing_index.json() == {
            "error": "Frontend not built. Run: cd web && npm run build"
        }
        assert missing_asset.status_code == 404

        def build_web_ui(_web_dir, *, fatal, require_fresh):
            assert fatal is True
            assert require_fresh is True
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text(
                "<html><head></head><body>Recovered dashboard</body></html>",
                encoding="utf-8",
            )
            (dist / "assets" / "app-deadbeef.js").write_text(
                "window.recovered = true;\n", encoding="utf-8"
            )
            return True

        monkeypatch.setattr(
            update_cmd,
            "_m",
            lambda: SimpleNamespace(
                PROJECT_ROOT=tmp_path,
                _build_web_ui=build_web_ui,
            ),
        )
        assert update_cmd._build_web_ui_for_update() is True

        recovered_index = client.get("/")
        recovered_asset = client.get("/assets/app-deadbeef.js")

    assert recovered_index.status_code == 200
    assert "Recovered dashboard" in recovered_index.text
    assert recovered_asset.status_code == 200
    assert recovered_asset.text == "window.recovered = true;\n"
    assert (
        recovered_asset.headers["cache-control"]
        == web_server._IMMUTABLE_ASSET_CACHE_CONTROL
    )


def test_headless_mount_never_starts_serving_a_late_bundle(tmp_path, monkeypatch):
    application = FastAPI()
    dist = tmp_path / "web_dist"
    monkeypatch.setenv("HERMES_SERVE_HEADLESS", "1")
    monkeypatch.setattr(web_server, "WEB_DIST", dist)

    web_server.mount_spa(application)
    dist.mkdir()
    (dist / "index.html").write_text(
        "<html>must not be served</html>", encoding="utf-8"
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 404
    assert "Headless backend" in response.json()["error"]
