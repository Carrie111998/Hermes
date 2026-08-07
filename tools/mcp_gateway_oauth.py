"""Gateway-mediated MCP OAuth consent bridge (#78169 / #78174).

When Hermes needs browser authorization for an OAuth MCP server during a
gateway session (Telegram/Discord/Slack/…), the authorize URL is delivered
to the originating chat instead of only the host terminal.

The MCP SDK still owns PKCE, state, DCR, and token exchange. This module:

- publishes the authorization URL into the active gateway session
- waits for the OAuth callback (public ``redirect_uri``, loopback, or a
  pasted ``?code=&state=`` reply from the same user/session)
- keeps flows keyed by session so User A's paste cannot complete User B's flow
"""

from __future__ import annotations

import contextvars
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


@dataclass
class GatewayOAuthFlow:
    """In-flight OAuth consent flow owned by one gateway session."""

    server_name: str
    session_key: str
    user_key: str = ""
    created_at: float = field(default_factory=time.time)
    authorization_url: str | None = None
    expected_state: str | None = field(default=None, init=False)
    _callback: tuple[str, str | None] | None = field(default=None, init=False, repr=False)
    _callback_error: str | None = field(default=None, init=False, repr=False)
    _authorization_ready: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _callback_ready: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    async def publish_authorization_url(self, url: str) -> None:
        state = parse_qs(urlparse(url).query).get("state", [None])[0]
        if not state:
            raise ValueError("OAuth authorization URL did not include state")
        with self._lock:
            if self._callback_ready.is_set():
                raise RuntimeError("OAuth flow already ended")
            self.expected_state = state
            self.authorization_url = url
            self._authorization_ready.set()

        # Deliver into the originating platform (never log the full URL —
        # it can contain sensitive query material).
        _deliver_consent_url_to_gateway(
            session_key=self.session_key,
            server_name=self.server_name,
            authorization_url=url,
        )

    async def wait_for_callback(self, timeout: float = 300.0) -> tuple[str, str | None]:
        ready = await __import__("asyncio").to_thread(
            self._callback_ready.wait, timeout
        )
        if not ready:
            raise TimeoutError(
                f"Timed out waiting for MCP OAuth callback for '{self.server_name}'"
            )
        if self._callback_error:
            raise RuntimeError(f"OAuth authorization failed: {self._callback_error}")
        if self._callback is None:
            raise RuntimeError("OAuth callback did not include an authorization code")
        return self._callback

    def deliver_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if self._callback_ready.is_set():
                raise ValueError("OAuth callback already received")
            if (
                self.expected_state is None
                or state is None
                or not secrets.compare_digest(self.expected_state, state)
            ):
                raise ValueError("OAuth callback state mismatch")
            if error:
                self._callback_error = error
            elif code:
                self._callback = (code, state)
            else:
                self._callback_error = "OAuth callback did not include code or error"
            self._callback_ready.set()


_current_gateway_flow: contextvars.ContextVar[GatewayOAuthFlow | None] = (
    contextvars.ContextVar("mcp_gateway_oauth_flow", default=None)
)

# session_key → active flow (for inbound paste / correlation)
_active_flows_by_session: dict[str, GatewayOAuthFlow] = {}
_active_flows_lock = threading.Lock()


@contextmanager
def gateway_oauth_flow(flow: GatewayOAuthFlow) -> Iterator[None]:
    """Bind *flow* for the current context and register it by session."""
    token = _current_gateway_flow.set(flow)
    with _active_flows_lock:
        _active_flows_by_session[flow.session_key] = flow
    try:
        yield
    finally:
        with _active_flows_lock:
            current = _active_flows_by_session.get(flow.session_key)
            if current is flow:
                _active_flows_by_session.pop(flow.session_key, None)
        _current_gateway_flow.reset(token)


def get_gateway_oauth_flow() -> GatewayOAuthFlow | None:
    return _current_gateway_flow.get()


def get_active_gateway_oauth_flow(session_key: str) -> GatewayOAuthFlow | None:
    with _active_flows_lock:
        return _active_flows_by_session.get(session_key)


def gateway_oauth_available() -> bool:
    """True when a gateway notify callback exists for the current session."""
    try:
        from tools.approval import (
            _gateway_notify_cbs,
            _is_gateway_approval_context,
            _lock,
            get_current_session_key,
        )
    except Exception:
        return False
    if not _is_gateway_approval_context():
        return False
    try:
        session_key = get_current_session_key()
    except Exception:
        return False
    if not session_key:
        return False
    with _lock:
        return session_key in _gateway_notify_cbs


def try_deliver_oauth_paste(session_key: str, text: str) -> bool:
    """If *text* is an OAuth redirect/paste for an active flow, complete it.

    Returns True when the message was consumed as an OAuth callback (caller
    should not queue it as a normal agent turn).
    """
    flow = get_active_gateway_oauth_flow(session_key)
    if flow is None or not text:
        return False

    code, state, error = _parse_oauth_paste(text)
    if not code and not error:
        return False
    try:
        flow.deliver_callback(code=code, state=state, error=error)
    except ValueError as exc:
        logger.info(
            "MCP OAuth paste for session %s rejected: %s", session_key, exc
        )
        notify_gateway_consent_result(
            session_key=session_key,
            server_name=flow.server_name,
            outcome="state_mismatch",
            detail=str(exc),
        )
        return False
    return True


# Consent failure / result copy delivered via the same notify path as the
# authorize URL (kind: mcp_oauth_consent_result). Never include full URLs.
_CONSENT_RESULT_MESSAGES = {
    "timeout": (
        "MCP OAuth timed out waiting for authorization for `{server}`. "
        "Ask me to retry the MCP action when you are ready to authorize, "
        "or paste the redirect URL sooner after approving in the browser."
    ),
    "cancelled": (
        "MCP OAuth was cancelled for `{server}`. "
        "Ask me to retry when you want to authorize again."
    ),
    "state_mismatch": (
        "That paste did not match the pending MCP OAuth flow for `{server}` "
        "(wrong or expired state). Open the authorize link from this chat "
        "again, then paste the redirect URL from the same browser session."
    ),
    "failed": (
        "MCP OAuth failed for `{server}`. "
        "Ask me to retry the action, or have an operator run "
        "`hermes mcp login {server}` on the host."
    ),
}


def notify_gateway_consent_result(
    *,
    session_key: str,
    server_name: str,
    outcome: str,
    detail: str = "",
) -> None:
    """Send an actionable in-chat message for a finished/failed consent flow."""
    from tools.approval import _gateway_notify_cbs, _lock

    template = _CONSENT_RESULT_MESSAGES.get(outcome) or _CONSENT_RESULT_MESSAGES["failed"]
    message = template.format(server=server_name)
    if detail and outcome == "failed":
        # Keep detail short and URL-free for the chat surface.
        cleaned = detail.replace("\n", " ").strip()
        if "http://" in cleaned or "https://" in cleaned:
            cleaned = cleaned.split("http", 1)[0].strip(" :")
        if cleaned and len(cleaned) < 200:
            message = f"{message} ({cleaned})"

    with _lock:
        notify_cb = _gateway_notify_cbs.get(session_key)
    if notify_cb is None:
        logger.debug(
            "No gateway notify for MCP OAuth consent result (%s / %s)",
            server_name, outcome,
        )
        return
    try:
        notify_cb(
            {
                "kind": "mcp_oauth_consent_result",
                "server_name": server_name,
                "outcome": outcome,
                "command": message,
                "description": message,
                "pattern_key": "mcp_oauth_consent_result",
                "pattern_keys": ["mcp_oauth_consent_result"],
            }
        )
    except Exception as exc:
        logger.warning(
            "Failed to deliver MCP OAuth consent result for '%s': %s",
            server_name, exc,
        )


def classify_oauth_consent_failure(exc: BaseException) -> str:
    """Map an OAuth/connect exception to a consent-result outcome key."""
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in name:
        return "timeout"
    if "user_skipped" in text or ("skip" in text and "oauth" in text):
        return "cancelled"
    if "access_denied" in text or "cancelled" in text or "canceled" in text:
        return "cancelled"
    if "state mismatch" in text or ("state" in text and "mismatch" in text):
        return "state_mismatch"
    return "failed"


def notify_consent_failure_for_active_flow(
    *,
    session_key: str | None = None,
    exc: BaseException | None = None,
    outcome: str | None = None,
) -> None:
    """Notify the chat about a failed consent when a gateway flow is active."""
    flow = get_gateway_oauth_flow()
    if flow is None and session_key:
        flow = get_active_gateway_oauth_flow(session_key)
    if flow is None:
        return
    resolved = outcome or (
        classify_oauth_consent_failure(exc) if exc is not None else "failed"
    )
    notify_gateway_consent_result(
        session_key=flow.session_key,
        server_name=flow.server_name,
        outcome=resolved,
        detail=str(exc) if exc is not None else "",
    )


def _parse_oauth_paste(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract code/state/error from a pasted redirect URL or query string."""
    raw = (text or "").strip()
    if not raw:
        return None, None, None
    # Accept full URLs or bare query fragments.
    if "://" in raw or raw.startswith("?"):
        parsed = urlparse(raw if "://" in raw else f"http://localhost{raw}")
        qs = parse_qs(parsed.query)
    elif "code=" in raw or "error=" in raw:
        qs = parse_qs(raw.lstrip("?"))
    else:
        return None, None, None
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    error = (qs.get("error") or [None])[0]
    if code or error:
        return code, state, error
    return None, None, None


def start_gateway_reauth_and_wait_for_url(
    server_name: str,
    *,
    reconnect_fn,
    wait_url_timeout: float = 45.0,
) -> tuple[str | None, str]:
    """Begin gateway OAuth reauth; return ``(authorization_url, user_key)``.

    ``reconnect_fn`` is a zero-arg callable that rebuilds the MCP session
    (tokens already cleared by the caller). It runs in a background thread
    under :class:`GatewayOAuthFlow` so the redirect handler can publish the
    URL while this function waits for ``_authorization_ready``.

    The flow stays registered until ``reconnect_fn`` finishes (callback,
    timeout, or failure) so paste-back still works after this returns.
    """
    from tools.approval import get_current_session_key
    from tools.mcp_oauth import force_interactive_oauth
    from tools.mcp_oauth_identity import current_oauth_user_key

    if not gateway_oauth_available():
        return None, current_oauth_user_key(require=False)

    session_key = get_current_session_key()
    if not session_key:
        return None, current_oauth_user_key(require=False)

    user_key = current_oauth_user_key(require=False)
    flow = GatewayOAuthFlow(
        server_name=server_name,
        session_key=session_key,
        user_key=user_key,
    )

    done = threading.Event()
    worker_exc: list[BaseException] = []

    def _worker() -> None:
        try:
            with gateway_oauth_flow(flow), force_interactive_oauth():
                try:
                    reconnect_fn()
                except BaseException as exc:  # noqa: BLE001 — surface to chat
                    # Notify while the flow is still bound so session lookup works.
                    notify_gateway_consent_result(
                        session_key=session_key,
                        server_name=server_name,
                        outcome=classify_oauth_consent_failure(exc),
                        detail=str(exc),
                    )
                    raise
        except BaseException as exc:  # noqa: BLE001
            worker_exc.append(exc)
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker,
        name=f"mcp-oauth-reauth-{server_name}",
        daemon=True,
    )
    thread.start()

    # Wait until the authorize URL is published, or the worker ends early.
    while not flow._authorization_ready.wait(timeout=0.25):
        if done.is_set():
            break
        wait_url_timeout -= 0.25
        if wait_url_timeout <= 0:
            break

    return flow.authorization_url, user_key


def _deliver_consent_url_to_gateway(
    *,
    session_key: str,
    server_name: str,
    authorization_url: str,
) -> None:
    from tools.approval import _gateway_notify_cbs, _lock

    with _lock:
        notify_cb = _gateway_notify_cbs.get(session_key)
    if notify_cb is None:
        raise RuntimeError(
            f"No gateway notify callback for session {session_key}; "
            "cannot surface MCP OAuth consent URL"
        )
    try:
        notify_cb(
            {
                "kind": "mcp_oauth_consent",
                "server_name": server_name,
                "authorization_url": authorization_url,
                "command": authorization_url,
                "description": (
                    f"MCP OAuth authorization required for '{server_name}'"
                ),
                "pattern_key": "mcp_oauth_consent",
                "pattern_keys": ["mcp_oauth_consent"],
            }
        )
    except Exception as exc:
        logger.warning(
            "Failed to deliver MCP OAuth consent URL for '%s': %s",
            server_name, exc,
        )
        raise
