"""Google Workspace OAuth routes for the desktop integrations UI.

The Google Workspace skill remains the owner of the credentials on disk. These
routes only provide the browser-friendly loopback flow that lets the desktop
UI replace the skill's manual copy/paste step. Tokens and client secrets never
leave the active Hermes profile.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hermes_cli.web_deps import late

router = APIRouter()
_log = logging.getLogger("hermes_cli.web_server")

_profile_scope = late("_profile_scope")
_require_token = late("_require_token")

_FLOW_TTL_SECONDS = 15 * 60
_MAX_PENDING_FLOWS = 4
_flows: dict[str, "GoogleWorkspaceOAuthFlow"] = {}
_flows_lock = threading.Lock()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


def _registry() -> dict[str, "GoogleWorkspaceOAuthFlow"]:
    return _flows


def _registry_lock() -> threading.Lock:
    return _flows_lock


@dataclass
class GoogleWorkspaceOAuthFlow:
    flow_id: str
    hermes_home: str
    oauth_flow: Any
    authorization_url: str
    state: str
    created_at: float = field(default_factory=time.time)
    status: str = "authorization_required"
    error: Optional[str] = None
    callback_code: Optional[str] = None
    callback_error: Optional[str] = None
    callback_received: threading.Event = field(default_factory=threading.Event)
    worker_done: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "status": self.status,
            "authorization_url": self.authorization_url,
            "error": self.error,
            "connected": self.status == "approved",
        }

    def deliver_callback(
        self,
        *,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str],
    ) -> None:
        if state is None or not secrets.compare_digest(self.state, state):
            raise ValueError("OAuth state mismatch")
        if self.callback_received.is_set():
            raise ValueError("OAuth callback already received")

        self.callback_code = code
        self.callback_error = error
        self.callback_received.set()


def _gc_flows() -> None:
    cutoff = time.time() - _FLOW_TTL_SECONDS
    registry = _registry()
    lock = _registry_lock()
    with lock:
        stale = [
            flow_id
            for flow_id, flow in registry.items()
            if flow.created_at < cutoff
        ]
        for flow_id in stale:
            registry.pop(flow_id, None)


def _profile_paths(profile: Optional[str]) -> tuple[Path, Path, Path]:
    from hermes_constants import get_hermes_home

    with _profile_scope(profile):
        home = get_hermes_home().expanduser().resolve(strict=False)
    return home, home / "google_client_secret.json", home / "google_token.json"


def _callback_url(request: Request) -> str:
    """Build a loopback callback URL on the local Hermes backend."""
    base = urlparse(str(request.base_url))
    return urlunparse(
        base._replace(
            path="/api/google-workspace/oauth/callback",
            params="",
            query="",
            fragment="",
        )
    )


def _ensure_loopback_request(request: Request) -> None:
    hostname = (urlparse(str(request.base_url)).hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(
            status_code=400,
            detail="Google Workspace desktop sign-in requires a local Hermes backend.",
        )


def _google_setup_script(home: Path) -> Optional[Path]:
    candidates = [
        home / "skills" / "productivity" / "google-workspace" / "scripts" / "setup.py",
        Path(__file__).resolve().parents[2]
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "setup.py",
    ]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _ensure_google_dependencies(home: Path) -> None:
    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except ImportError as exc:
        setup_script = _google_setup_script(home)
        if setup_script is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Google API dependencies are not installed and the Google Workspace "
                    "setup script is unavailable."
                ),
            ) from exc
        try:
            result = subprocess.run(
                [sys.executable, str(setup_script), "--install-deps"],
                capture_output=True,
                check=False,
                env={**os.environ, "HERMES_HOME": str(home)},
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as install_error:
            raise HTTPException(
                status_code=503,
                detail="Google API dependencies could not be installed automatically.",
            ) from install_error
        if result.returncode != 0:
            _log.error("Google Workspace dependency install failed: %s", result.stdout[-2000:])
            raise HTTPException(
                status_code=503,
                detail="Google API dependencies could not be installed automatically.",
            ) from exc

    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except ImportError as import_error:
        raise HTTPException(
            status_code=503,
            detail="Google API dependencies remain unavailable after setup.",
        ) from import_error


def _load_google_flow(home: Path, client_secret: Path, redirect_uri: str) -> tuple[Any, str, str]:
    _ensure_google_dependencies(home)
    from google_auth_oauthlib.flow import Flow

    try:
        flow = Flow.from_client_secrets_file(
            str(client_secret),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=True,
        )
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
    except Exception as exc:
        _log.exception("Could not start Google Workspace OAuth")
        raise HTTPException(status_code=400, detail=f"Could not start Google sign-in: {exc}") from exc

    return flow, authorization_url, state


def _save_token(flow: GoogleWorkspaceOAuthFlow) -> None:
    payload = json.loads(flow.oauth_flow.credentials.to_json())
    payload["type"] = payload.get("type") or "authorized_user"
    granted_scopes = getattr(flow.oauth_flow.credentials, "granted_scopes", None)
    if granted_scopes:
        payload["scopes"] = list(granted_scopes)

    token_path = Path(flow.hermes_home) / "google_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_name(f".{token_path.name}.{flow.flow_id}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(token_path)


def _run_flow(flow: GoogleWorkspaceOAuthFlow) -> None:
    try:
        if not flow.callback_received.wait(timeout=_FLOW_TTL_SECONDS):
            raise RuntimeError("Google sign-in timed out. Please try again.")
        if flow.callback_error:
            raise RuntimeError(f"Google authorization was denied: {flow.callback_error}")
        if not flow.callback_code:
            raise RuntimeError("Google did not return an authorization code.")

        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.oauth_flow.fetch_token(code=flow.callback_code)
        _save_token(flow)
        flow.status = "approved"
    except Exception as exc:
        flow.error = str(exc)
        flow.status = "error"
        _log.exception("Google Workspace OAuth flow failed")
    finally:
        flow.worker_done = True


@router.get("/api/google-workspace/status")
async def google_workspace_status(request: Request, profile: Optional[str] = None):
    """Return safe connection metadata; never return credential contents."""
    _require_token(request)
    _, client_secret, token = _profile_paths(profile)
    payload: dict[str, Any] = {}
    if token.exists():
        try:
            payload = json.loads(token.read_text(encoding="utf-8"))
        except Exception:
            payload = {}

    token_present = token.exists() and bool(payload.get("refresh_token") or payload.get("token"))
    return {
        "configured": client_secret.exists(),
        "connected": token_present,
        "scopes": list(payload.get("scopes") or []),
    }


@router.post("/api/google-workspace/oauth/start")
async def start_google_workspace_oauth(request: Request, profile: Optional[str] = None):
    _require_token(request)
    _ensure_loopback_request(request)
    _gc_flows()
    home, client_secret, _ = _profile_paths(profile)
    if not client_secret.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                "Google Workspace is not configured for this profile. Store the Google "
                "Desktop OAuth client secret with the Google Workspace skill first."
            ),
        )

    oauth_flow, authorization_url, state = _load_google_flow(home, client_secret, _callback_url(request))
    flow = GoogleWorkspaceOAuthFlow(
        flow_id=secrets.token_urlsafe(24),
        hermes_home=str(home),
        oauth_flow=oauth_flow,
        authorization_url=authorization_url,
        state=state,
    )

    registry = _registry()
    lock = _registry_lock()
    with lock:
        pending = sum(not item.worker_done for item in registry.values())
        if pending >= _MAX_PENDING_FLOWS:
            raise HTTPException(status_code=429, detail="Too many Google sign-in attempts are in progress.")
        if any(
            item.hermes_home == flow.hermes_home and not item.worker_done
            for item in registry.values()
        ):
            raise HTTPException(status_code=409, detail="Google Workspace sign-in is already in progress.")
        registry[flow.flow_id] = flow

    threading.Thread(
        target=_run_flow,
        args=(flow,),
        daemon=True,
        name="google-workspace-oauth",
    ).start()
    return flow.snapshot()


@router.get("/api/google-workspace/oauth/flows/{flow_id}")
async def google_workspace_oauth_status(flow_id: str, request: Request):
    _require_token(request)
    _gc_flows()
    flow = _registry().get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Google sign-in expired or was not found.")
    return flow.snapshot()


@router.get("/api/google-workspace/oauth/callback")
async def google_workspace_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    _gc_flows()
    candidates = [flow for flow in _registry().values() if flow.status == "authorization_required"]
    flow = next(
        (
            candidate
            for candidate in candidates
            if state is not None and secrets.compare_digest(candidate.state, state)
        ),
        None,
    )
    if flow is None:
        return HTMLResponse(
            "<h1>Google sign-in expired</h1><p>Return to Hermes and try again.</p>",
            status_code=404,
        )
    try:
        flow.deliver_callback(code=code, state=state, error=error)
    except ValueError:
        return HTMLResponse(
            "<h1>Google callback rejected</h1><p>Return to Hermes and try again.</p>",
            status_code=409,
        )
    if error:
        return HTMLResponse(
            "<h1>Google authorization failed</h1><p>Return to Hermes for details.</p>",
            status_code=400,
        )
    return HTMLResponse(
        "<h1>Google connected</h1><p>You can close this tab and return to Hermes.</p>"
    )
