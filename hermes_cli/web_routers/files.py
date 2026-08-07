"""Managed-files dashboard routes (extracted verbatim from web_server.py).

Handler bodies are byte-identical to their previous in-web_server form; the
helpers they call (``_resolve_managed_path``, ``_managed_file_entry``,
``_is_sensitive_path``, ``_managed_response_meta``, ``_decode_data_url``,
``_decode_chat_image_upload``, ``_sanitize_chat_image_filename``,
``_profile_scope``, ``get_hermes_home``) still live in web_server and are
reached via the late-binding seam in :mod:`hermes_cli.web_deps`, so
``monkeypatch.setattr(web_server, ...)`` keeps working. The shared numeric
limits (``_MANAGED_FILE_MAX_BYTES``, ``_UPLOAD_CHUNK_BYTES``) also stay in
web_server, read through ``LateState`` live proxies.
"""

import base64
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from hermes_cli.web_deps import late, LateState
from hermes_cli.web_models import (
    ChatImageUpload,
    ManagedDirectoryCreate,
    ManagedFileDelete,
    ManagedFileUpload,
)

router = APIRouter()

# Late-bound web_server helpers (resolved at call time; cycle-safe,
# monkeypatch-transparent).
_decode_chat_image_upload = late("_decode_chat_image_upload")
_decode_data_url = late("_decode_data_url")
_is_sensitive_path = late("_is_sensitive_path")
_managed_file_entry = late("_managed_file_entry")
_managed_response_meta = late("_managed_response_meta")
_profile_scope = late("_profile_scope")
_resolve_managed_path = late("_resolve_managed_path")
_sanitize_chat_image_filename = late("_sanitize_chat_image_filename")
get_hermes_home = late("get_hermes_home")

# Live proxies for web_server-owned numeric limits (mutations/monkeypatches on
# web_server remain authoritative; resolved at operation/read time).
_MANAGED_FILE_MAX_BYTES = LateState("_MANAGED_FILE_MAX_BYTES")
_UPLOAD_CHUNK_BYTES = LateState("_UPLOAD_CHUNK_BYTES")

@router.post("/api/chat/image-upload")
async def upload_chat_image(payload: ChatImageUpload, profile: Optional[str] = None):
    """Persist a browser-provided chat image where the embedded TUI can read it.

    The dashboard /chat page runs Hermes inside an xterm.js PTY. Browser
    clipboard image bytes are not visible to the server-side clipboard, so the
    page uploads them here, then drives the TUI's ``/image <path>`` command
    with the returned gateway-visible path. Files land under
    ``HERMES_HOME/images/`` — the same directory ``clipboard.paste`` /
    ``image.attach`` already use.
    """
    data, mime_type, ext = _decode_chat_image_upload(payload)
    with _profile_scope(profile) as scoped_home:
        home = scoped_home or get_hermes_home()
        img_dir = Path(home) / "images"
        try:
            img_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Image directory is not writable")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not create image directory: {exc}")

        stem = Path(_sanitize_chat_image_filename(payload.filename)).stem or "pasted-image"
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._-") or "pasted-image"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = img_dir / f"dashboard_{ts}_{secrets.token_hex(4)}_{stem}{ext}"

        try:
            target.write_bytes(data)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Image directory is not writable")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not write image: {exc}")

    return {
        "ok": True,
        "path": str(target),
        "name": target.name,
        "bytes": len(data),
        "mime_type": mime_type,
    }


@router.get("/api/files")
async def list_managed_files(request: Request, path: Optional[str] = None):
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    try:
        entries = [
            _managed_file_entry(policy, child)
            for child in target.iterdir()
            if not _is_sensitive_path(child)
        ]
    except PermissionError:
        raise HTTPException(status_code=403, detail="Directory is not readable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read directory: {exc}")

    entries.sort(key=lambda item: (not item["is_directory"], str(item["name"]).lower()))
    locked_root = policy.locked_root
    parent = None
    if target.parent != target and (locked_root is None or target != locked_root):
        parent = str(target.parent)
    return {
        "path": display_path,
        "parent": parent,
        "entries": entries,
        **_managed_response_meta(policy),
    }


@router.get("/api/files/read")
async def read_managed_file(request: Request, path: str):
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")
    if _is_sensitive_path(target):
        raise HTTPException(status_code=403, detail="Access to sensitive files is not allowed")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat file: {exc}")
    if size > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    try:
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not readable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read file: {exc}")

    return {
        "name": target.name,
        "path": display_path,
        "size": size,
        "mime_type": mime_type,
        "data_url": f"data:{mime_type};base64,{encoded}",
        **_managed_response_meta(policy),
    }


@router.get("/api/files/download")
async def download_managed_file(request: Request, path: str):
    """Stream a managed file as an attachment download.

    Remote clients (desktop app, browser dashboard) open agent-written files
    that live on *this* gateway's disk, not theirs. Auth-gated like every other
    managed-files route — ``auth_middleware`` additionally accepts the session
    token as a ``?token=`` query param here so a shell/browser-opened download
    (which can't set the session header) still authenticates. See ``/api/pty``
    for the same query-token precedent.
    """
    policy, target, _display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")
    if _is_sensitive_path(target):
        raise HTTPException(status_code=403, detail="Access to sensitive files is not allowed")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat file: {exc}")
    if size > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

    return FileResponse(
        path=str(target),
        media_type=mime_type,
        filename=target.name,
        content_disposition_type="attachment",
    )


@router.post("/api/files/upload")
async def upload_managed_file(payload: ManagedFileUpload, request: Request):
    policy, target, display_path = _resolve_managed_path(payload.path, request, for_write=True)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=409, detail="A directory already exists at that path")
    if target.exists() and not payload.overwrite:
        raise HTTPException(status_code=409, detail="File already exists")

    data, _mime_type = _decode_data_url(payload.data_url)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


# Stream uploads to disk in fixed-size chunks. The legacy JSON endpoint above
# buffers the whole file as a base64 data URL in a JSON body, which (a) inflates
# the payload ~33%, (b) holds the entire file (plus its decoded copy) in memory,
# and (c) reliably trips upstream proxy body-size/timeout limits with a 502 on
# large backup archives (NS-501). This multipart endpoint reads the request body
# in 1 MiB chunks straight to a temp file, enforces the size cap as it goes, and
# atomically renames into place — constant memory, no base64 inflation.


@router.post("/api/files/upload-stream")
async def upload_managed_file_stream(
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(...),
    overwrite: bool = Form(True),
):
    policy, target, display_path = _resolve_managed_path(path, request, for_write=True)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=409, detail="A directory already exists at that path")
    if target.exists() and not overwrite:
        raise HTTPException(status_code=409, detail="File already exists")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create parent directory: {exc}")

    # Write to a sibling temp file first so a partial/aborted upload never
    # clobbers an existing file, then atomically rename into place.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".upload", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    total = 0
    renamed = False
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MANAGED_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File is too large")
                out.write(chunk)
        os.replace(tmp_path, target)
        renamed = True
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")
    finally:
        # Clean up the temp file on every non-success exit, including
        # BaseException paths the `except` clauses above don't catch — most
        # importantly asyncio.CancelledError when a browser aborts a large
        # upload mid-stream (the exact NS-501 scenario). os.replace clears
        # tmp_path on success, so only unlink when the rename didn't happen.
        if not renamed:
            tmp_path.unlink(missing_ok=True)
        await file.close()

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


@router.post("/api/files/mkdir")
async def create_managed_directory(payload: ManagedDirectoryCreate, request: Request):
    policy, target, display_path = _resolve_managed_path(payload.path, request, for_write=True)
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=409, detail="A file already exists at that path")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Directory is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create directory: {exc}")

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


@router.delete("/api/files")
async def delete_managed_file(payload: ManagedFileDelete, request: Request):
    policy, target, display_path = _resolve_managed_path(payload.path, request)
    if policy.locked_root is not None and target == policy.locked_root:
        raise HTTPException(status_code=400, detail="Cannot delete the managed files root")
    if target.parent == target:
        raise HTTPException(status_code=400, detail="Cannot delete the filesystem root")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        if target.is_dir():
            if payload.recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        status_code = 409 if target.is_dir() and not payload.recursive else 500
        raise HTTPException(status_code=status_code, detail=f"Could not delete path: {exc}")

    return {"ok": True, "path": display_path, **_managed_response_meta(policy)}
