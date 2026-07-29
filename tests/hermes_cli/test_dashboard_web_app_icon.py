"""Regression coverage for profile-scoped iPad Home Screen icons.

Safari's Add to Home Screen flow uses the ``apple-touch-icon`` link in the
served document.  The icon must be profile-local and must never turn the
Dashboard into a route for arbitrary local files.
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient


_JPEG = b"\xff\xd8\xff\xe0" + b"JFIF\x00" + b"profile-icon" + b"\xff\xd9"


def _client_with_profile_icon(tmp_path: Path, monkeypatch, icon_setting: str) -> TestClient:
    import hermes_cli.web_server as ws

    home = tmp_path / "profile-home"
    home.mkdir(exist_ok=True)
    dist = tmp_path / "web-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><title>Hermes</title></head><body>Dashboard</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(ws, "WEB_DIST", dist)
    monkeypatch.setattr(ws, "get_hermes_home", lambda: home)
    monkeypatch.setattr(ws, "load_config", lambda: {"display": {"web_app_icon": icon_setting}})

    app = FastAPI()
    ws.mount_spa(app)
    return TestClient(app)


def test_profile_icon_is_injected_and_served_for_safari_web_clip(tmp_path, monkeypatch):
    """A profile-local JPEG must drive the iPad Home Screen icon endpoint."""
    home = tmp_path / "profile-home"
    icon = home / "reference-images" / "amanda.jpg"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(_JPEG)

    client = _client_with_profile_icon(
        tmp_path, monkeypatch, "reference-images/amanda.jpg"
    )

    page = client.get("/", headers={"x-forwarded-prefix": "/hermes"})
    assert page.status_code == 200
    assert '<link rel="apple-touch-icon"' in page.text
    assert 'href="/hermes/apple-touch-icon.png?v=' in page.text

    icon_response = client.get("/apple-touch-icon.png")
    assert icon_response.status_code == 200
    assert icon_response.content == _JPEG
    assert icon_response.headers["content-type"].startswith("image/jpeg")
    assert "no-store" in icon_response.headers["cache-control"]


def test_icon_file_swap_cannot_serve_external_bytes(tmp_path, monkeypatch):
    """A replacement after validation must never turn the route into a file leak."""
    home = tmp_path / "profile-home"
    icon = home / "reference-images" / "amanda.jpg"
    outside = tmp_path / "outside.jpg"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(_JPEG)
    outside.write_bytes(b"\xff\xd8\xffexternal-profile-bytes")
    client = _client_with_profile_icon(
        tmp_path, monkeypatch, "reference-images/amanda.jpg"
    )

    # Render before the swap so this request exercises the endpoint's own
    # validation-to-response window rather than merely suppressing the link.
    assert "apple-touch-icon" in client.get("/").text
    original_open = Path.open
    swapped = False

    def swap_to_external_file(path, *args, **kwargs):
        nonlocal swapped
        if path == icon and not swapped:
            outside.replace(icon)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_to_external_file)
    response = client.get("/apple-touch-icon.png")

    assert swapped
    assert response.status_code == 404
    assert b"external-profile-bytes" not in response.content


def test_malformed_display_config_does_not_break_the_dashboard(tmp_path, monkeypatch):
    """A non-mapping display setting must disable the icon, not serve a 500."""
    import hermes_cli.web_server as ws

    client = _client_with_profile_icon(tmp_path, monkeypatch, "unused.jpg")
    monkeypatch.setattr(ws, "load_config", lambda: {"display": "broken"})

    page = client.get("/")
    assert page.status_code == 200
    assert "apple-touch-icon" not in page.text
    assert client.get("/apple-touch-icon.png").status_code == 404


def test_real_profile_config_drives_the_web_clip_icon(tmp_path, monkeypatch):
    """The real config loader resolves a profile-local image end to end."""
    from hermes_cli.config import load_config, save_config
    import hermes_cli.web_server as ws

    home = tmp_path / "profile-home"
    icon = home / "reference-images" / "amanda.jpg"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(_JPEG)
    monkeypatch.setenv("HERMES_HOME", str(home))

    config = load_config()
    config.setdefault("display", {})["web_app_icon"] = "reference-images/amanda.jpg"
    save_config(config)

    dist = tmp_path / "web-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head></head><body>Dashboard</body></html>", encoding="utf-8"
    )
    monkeypatch.setattr(ws, "WEB_DIST", dist)
    app = FastAPI()
    ws.mount_spa(app)
    client = TestClient(app)

    assert "apple-touch-icon" in client.get("/").text
    assert client.get("/apple-touch-icon.png").content == _JPEG


def test_profile_query_is_carried_to_the_icon_request(tmp_path, monkeypatch):
    """A Dashboard profile switch must not silently use the launch profile icon."""
    from contextlib import contextmanager
    import hermes_cli.web_server as ws

    home = tmp_path / "profile-home"
    icon = home / "reference-images" / "amanda.jpg"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(_JPEG)
    requested_profiles = []

    @contextmanager
    def fake_profile_scope(profile):
        requested_profiles.append(profile)
        yield None

    monkeypatch.setattr(ws, "_config_profile_scope", fake_profile_scope)
    client = _client_with_profile_icon(
        tmp_path, monkeypatch, "reference-images/amanda.jpg"
    )

    page = client.get("/?profile=amanda")
    assert page.status_code == 200
    assert "&profile=amanda\"" in page.text

    icon_response = client.get("/apple-touch-icon.png?profile=amanda")
    assert icon_response.status_code == 200
    assert requested_profiles == ["amanda", "amanda"]


def test_out_of_profile_icon_path_is_not_linked_or_served(tmp_path, monkeypatch):
    """A config path outside the profile must fail closed instead of exposing bytes."""
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_JPEG)
    client = _client_with_profile_icon(tmp_path, monkeypatch, str(outside))

    page = client.get("/")
    assert page.status_code == 200
    assert "apple-touch-icon" not in page.text

    icon_response = client.get("/apple-touch-icon.png")
    assert icon_response.status_code == 404


def test_non_image_profile_file_is_not_linked_or_served(tmp_path, monkeypatch):
    """Only image bytes may become a public web-app icon response."""
    home = tmp_path / "profile-home"
    not_an_image = home / "reference-images" / "private.txt"
    not_an_image.parent.mkdir(parents=True)
    not_an_image.write_text("private profile material", encoding="utf-8")
    client = _client_with_profile_icon(
        tmp_path, monkeypatch, "reference-images/private.txt"
    )

    page = client.get("/")
    assert page.status_code == 200
    assert "apple-touch-icon" not in page.text

    icon_response = client.get("/apple-touch-icon.png")
    assert icon_response.status_code == 404
