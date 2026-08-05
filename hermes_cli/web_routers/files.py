"""Managed-files dashboard routes + helpers (extracted verbatim from web_server.py).

Handler and helper bodies are byte-identical to their previous in-web_server
form.  The helpers they call that still live in web_server
(``_default_hermes_root_is_opt_data``, ``_path_is_under``, ``_profile_scope``,
``get_hermes_home``) are reached via the late-binding seam in
:mod:`hermes_cli.web_deps`, so ``monkeypatch.setattr(web_server, ...)`` keeps
working.  The shared numeric limits (``_MANAGED_FILE_MAX_BYTES``,
``_UPLOAD_CHUNK_BYTES``) also stay in web_server, read through
``LateState``/``late_attr``.
"""

import base64
import binascii
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from hermes_cli.web_deps import late, late_attr, LateState
from hermes_cli.web_models import (
    ChatImageUpload,
    ManagedDirectoryCreate,
    ManagedFileDelete,
    ManagedFileUpload,
)

router = APIRouter()

# Late-bound web_server helpers (resolved at call time; cycle-safe,
# monkeypatch-transparent).
_default_hermes_root_is_opt_data = late("_default_hermes_root_is_opt_data")
_path_is_under = late("_path_is_under")
_profile_scope = late("_profile_scope")
get_hermes_home = late("get_hermes_home")

# Live proxies for web_server-owned numeric limits (mutations/monkeypatches on
# web_server remain authoritative; resolved at operation/import time).
_MANAGED_FILE_MAX_BYTES = LateState("_MANAGED_FILE_MAX_BYTES")
_UPLOAD_CHUNK_BYTES = late_attr("_UPLOAD_CHUNK_BYTES")

_MANAGED_FILES_ROOT_ENV = "HERMES_DASHBOARD_FILES_ROOT"
_HOSTED_MANAGED_FILES_ROOT = Path("/opt/data")
@dataclass(frozen=True)
class ManagedFilesPolicy:
    default_path: Path
    locked_root: Path | None
    can_change_path: bool
# Filenames that must never be listed, read, or downloaded through the
# managed-files API.  These typically contain credentials (API keys, tokens)
# and exposing them through the dashboard file browser is a security leak —
# see issue #57505. The set mirrors the credential-file basenames of the two
# canonical credential guards elsewhere in the codebase
# (agent.file_safety.get_read_block_error and
# gateway.platforms.base._ROOT_CREDENTIAL_FILES) so the dashboard Files tab
# doesn't lag behind them — an operator can point the managed root at
# HERMES_HOME itself, at which point every one of these basenames is a live
# secret store sitting in the browsable tree.
_SENSITIVE_MANAGED_FILE_BASENAMES = frozenset({
    "auth.json",
    "auth.lock",
    "credentials",
    "config.yaml",
    ".anthropic_oauth.json",
    "google_token.json",
    "google_oauth_pending.json",
    "google_oauth.json",
    "webhook_subscriptions.json",
    "bws_cache.json",
    "bws_cache.enc.json",
    # git's credential-store helper cache (agent.file_safety blocks this too).
    ".git-credentials",
})
# Directory names whose entire subtree is credential material. Both canonical
# guards deny these as directory trees, not basenames:
#   * gateway.platforms.base._ROOT_CREDENTIAL_DIRS = {"pairing", "mcp-tokens"}
#   * agent.file_safety.get_read_block_error (mcp-tokens/ prefix match)
# The managed-files API lets the browser descend into subdirs, so a
# basename-only guard would still expose e.g. ``mcp-tokens/<server>.json``
# (live MCP OAuth tokens) and ``pairing/<x>``. We match on ANY path component
# so these trees are blocked wherever they appear under the browsable root,
# without needing to resolve them relative to HERMES_HOME.
_SENSITIVE_MANAGED_DIR_NAMES = frozenset({
    "mcp-tokens",
    "pairing",
})
def _is_sensitive_filename(name: str) -> bool:
    """Return True for a basename the managed-files API must never expose.

    Covers ``.env`` / ``.env.<suffix>`` / ``.envrc`` variants plus the
    canonical Hermes credential-store basenames (see
    ``_SENSITIVE_MANAGED_FILE_BASENAMES`` above).

    Case-insensitive so ``.ENV`` / ``.Env.local`` / ``Auth.JSON`` on
    case-insensitive filesystems (macOS/Windows mounts) can't slip past
    the guard.

    Basename-only: for the directory-tree credential stores
    (``mcp-tokens/``, ``pairing/``) that the canonical guards also deny,
    use :func:`_is_sensitive_path`, which the API call sites route through.
    """
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc":
        return True
    return lowered in _SENSITIVE_MANAGED_FILE_BASENAMES
def _is_sensitive_path(path: Path) -> bool:
    """Return True for any path the managed-files API must never expose.

    Combines the basename denylist (:func:`_is_sensitive_filename`) with a
    credential-directory-tree check: a path is sensitive if its own basename
    is sensitive OR any of its path components is a credential directory
    (``mcp-tokens`` / ``pairing``). The component match is case-insensitive
    and needs no HERMES_HOME resolution, so it blocks these trees wherever
    they sit under the operator-configured managed root — closing the gap
    the canonical guards cover as directory trees but a basename-only check
    would miss.

    Read-side only: this guards list/read/download (the #57505 exfil surface).
    The write endpoints (upload/mkdir/delete) are a separate threat class
    handled by the write-path checks; extending this guard to them is out of
    scope for this fix.
    """
    if _is_sensitive_filename(path.name):
        return True
    return any(part.lower() in _SENSITIVE_MANAGED_DIR_NAMES for part in path.parts)
def _canonical_path(path: Path, *, require_exists: bool = False) -> Path:
    try:
        return path.expanduser().resolve(strict=require_exists)
    except FileNotFoundError:
        if require_exists:
            raise HTTPException(status_code=404, detail="Path not found")
        raise
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")
def _ensure_managed_root(raw_path: str | Path) -> Path:
    root = Path(raw_path).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Managed files root is unavailable: {exc}")
    if not resolved.is_dir():
        raise HTTPException(status_code=500, detail="Managed files root is not a directory")
    return resolved
def _path_text(raw_path: str | None) -> str:
    text = str(raw_path or "").strip()
    if "\x00" in text:
        raise HTTPException(status_code=400, detail="Invalid path")
    return text
def _local_dashboard_request(request: Request) -> bool:
    if getattr(request.app.state, "auth_required", False):
        return False
    host = (request.url.hostname or "").lower()
    client_host = (request.client.host if request.client else "").lower()
    local_hosts = {"", "localhost", "127.0.0.1", "::1", "testserver", "testclient"}
    return host in local_hosts or client_host in local_hosts
def _managed_files_policy(request: Request, *, create_root: bool = True) -> ManagedFilesPolicy:
    raw_forced_root = os.environ.get(_MANAGED_FILES_ROOT_ENV, "").strip()
    if raw_forced_root:
        root = _ensure_managed_root(raw_forced_root) if create_root else _canonical_path(Path(raw_forced_root))
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    # Remote/OAuth access does not imply a hosted container. Users can expose a
    # local dashboard through the auth gate (for example a macOS launchd install)
    # and still expect the Files page to browse their local home directory. Lock
    # to /opt/data only when the installation's Hermes root is actually /opt/data
    # (the container/hosted layout) or when HERMES_DASHBOARD_FILES_ROOT is set.
    if _default_hermes_root_is_opt_data():
        root = _ensure_managed_root(_HOSTED_MANAGED_FILES_ROOT) if create_root else _HOSTED_MANAGED_FILES_ROOT
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    home = _canonical_path(Path.home())
    return ManagedFilesPolicy(default_path=home, locked_root=None, can_change_path=True)
def _resolve_managed_path(
    raw_path: str | None,
    request: Request,
    *,
    for_write: bool = False,
) -> tuple[ManagedFilesPolicy, Path, str]:
    policy = _managed_files_policy(request)
    text = _path_text(raw_path)
    root = policy.locked_root

    if root is not None and (not text or text in {".", "/"}):
        candidate = root
    elif not text:
        candidate = policy.default_path
    else:
        candidate = Path(text).expanduser()
        if root is not None and not candidate.is_absolute():
            if any(part == ".." for part in candidate.parts):
                raise HTTPException(status_code=400, detail="Path cannot contain '..'")
            candidate = root / candidate
        elif not candidate.is_absolute():
            raise HTTPException(status_code=400, detail="Path must be absolute")

    if ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Path cannot contain '..'")

    if for_write and not candidate.exists():
        parent = _canonical_path(candidate.parent)
        resolved = parent / candidate.name
    else:
        resolved = _canonical_path(candidate, require_exists=not for_write)

    if root is not None and not _path_is_under(root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    return policy, resolved, str(resolved)
def _managed_response_meta(policy: ManagedFilesPolicy) -> Dict[str, Any]:
    locked_root = str(policy.locked_root) if policy.locked_root is not None else None
    return {
        "root": locked_root,
        "locked_root": locked_root,
        "can_change_path": policy.can_change_path,
    }
def _managed_file_entry(policy: ManagedFilesPolicy, target: Path) -> Dict[str, Any]:
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if policy.locked_root is not None and not _path_is_under(policy.locked_root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    try:
        st = resolved.stat()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat path: {exc}")

    is_dir = resolved.is_dir()
    mime_type = None if is_dir else (mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")
    return {
        "name": target.name or resolved.name or str(resolved),
        "path": str(resolved),
        "is_directory": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "mime_type": mime_type,
    }
def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    text = (data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise HTTPException(status_code=400, detail="Upload payload must be a data URL")
    header, encoded = text.split(",", 1)
    mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" not in header:
        raise HTTPException(status_code=400, detail="Upload payload must be base64 encoded")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Upload payload is not valid base64")
    if len(data) > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")
    return data, mime_type
_CHAT_IMAGE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
_CHAT_IMAGE_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_CHAT_IMAGE_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_CHAT_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
def _sanitize_chat_image_filename(filename: str | None) -> str:
    candidate = Path(str(filename or "").strip()).name
    candidate = re.sub(r"[\x00-\x1f]+", "_", candidate)
    candidate = candidate.strip().strip(".")
    return candidate or "pasted-image"
def _chat_image_extension(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    for sig, ext in _CHAT_IMAGE_MAGIC:
        if head.startswith(sig):
            return ext
    return None
def _decode_chat_image_upload(payload: ChatImageUpload) -> tuple[bytes, str, str]:
    data, mime_type = _decode_data_url(payload.data_url)
    if not mime_type.lower().startswith("image/"):
        raise HTTPException(status_code=400, detail="Upload payload must be an image")
    if len(data) > _CHAT_IMAGE_UPLOAD_MAX_BYTES:
        mb = _CHAT_IMAGE_UPLOAD_MAX_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Image is too large; cap is {mb} MB")

    ext = _chat_image_extension(data)
    if ext not in _CHAT_IMAGE_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported image type")
    return data, mime_type, ext
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
