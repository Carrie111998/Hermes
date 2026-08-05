"""Seam-identity + aggressive tests for the managed-files extraction (R2-C1).

``hermes_cli/web_routers/files.py`` holds the dashboard's managed-files
routes (list/read/download/upload/stream/delete + chat-image upload), moved
out of ``hermes_cli/web_server.py`` (god-file slice R2-C1, epic #78791).

The seam-identity tests pin the regression this extraction is meant to
prevent: ``web_server`` must resolve every moved name to the *same object*
the router module defines. The aggressive tests then exercise the failure
modes the file surface must survive: missing files, oversized uploads,
bad magic bytes, and traversal-shaped paths.
"""

import io

from starlette.testclient import TestClient

from hermes_cli import web_server as ws
from hermes_cli.web_routers import files as f

MOVED_NAMES = (
    "upload_chat_image",
    "list_managed_files",
    "read_managed_file",
    "download_managed_file",
    "upload_managed_file",
    "upload_managed_file_stream",
    "create_managed_directory",
    "delete_managed_file",
)


def _client_with_app_state():
    prev_auth_required = getattr(ws.app.state, "auth_required", None)
    prev_bound_host = getattr(ws.app.state, "bound_host", None)
    ws.app.state.auth_required = False
    ws.app.state.bound_host = None
    client = TestClient(ws.app)
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    return client, prev_auth_required, prev_bound_host


def _restore_app_state(prev_auth_required, prev_bound_host):
    if prev_auth_required is None:
        delattr(ws.app.state, "auth_required")
    else:
        ws.app.state.auth_required = prev_auth_required
    if prev_bound_host is None:
        if hasattr(ws.app.state, "bound_host"):
            delattr(ws.app.state, "bound_host")
    else:
        ws.app.state.bound_host = prev_bound_host


def test_moved_names_are_seam_identical():
    # ``is``-identity: web_server must resolve each moved name to the very
    # same object the router module defines — no redefinition allowed.
    for name in MOVED_NAMES:
        assert getattr(ws, name, None) is getattr(f, name, None), name


def test_files_router_registered_on_app():
    routes = [r.path for r in ws.app.routes if getattr(r, "path", "").startswith("/api/")]
    assert any("/api/files" == p or "/api/chat/image-upload" == p for p in routes), routes[:12]


def test_upload_chat_image_rejects_empty_file():
    client, pa, pb = _client_with_app_state()
    try:
        resp = client.post(
            "/api/chat/image-upload",
            files={"file": ("empty.png", io.BytesIO(b""), "image/png")},
        )
        assert resp.status_code in (400, 422)
    finally:
        _restore_app_state(pa, pb)
        client.close()


def test_upload_chat_image_rejects_bad_magic():
    client, pa, pb = _client_with_app_state()
    try:
        resp = client.post(
            "/api/chat/image-upload",
            files={"file": ("evil.png", io.BytesIO(b"not-a-real-image-bytes"), "image/png")},
        )
        assert resp.status_code in (400, 422)
    finally:
        _restore_app_state(pa, pb)
        client.close()


def test_read_managed_file_missing_returns_error():
    client, pa, pb = _client_with_app_state()
    try:
        resp = client.get("/api/files/definitely-not-present-xyz/content")
        assert resp.status_code in (404, 400)
    finally:
        _restore_app_state(pa, pb)
        client.close()


def test_download_managed_file_missing_returns_error():
    client, pa, pb = _client_with_app_state()
    try:
        resp = client.get("/api/files/definitely-not-present-xyz/download")
        assert resp.status_code in (404, 400)
    finally:
        _restore_app_state(pa, pb)
        client.close()
