"""Grok rate-limit scrape via a logged-in grok.com tab over CDP.

api.x.ai exposes no usage endpoint (and this box's XAI_API_KEY is
team_blocked), so the only automated source is the same
``POST /rest/rate-limits`` call grok.com's own web app makes -- which is
rejected without that tab's session cookies. Rather than decrypt Chrome's
app-bound-encrypted cookie store, we execute the fetch INSIDE the page over
the DevTools protocol, so Chrome attaches its own cookies.

Transport: plain websocket-client against ``http://127.0.0.1:9222/json``
(the automation-profile Chrome this machine already runs; discovery via
``hermes_cli.browser_connect.discover_local_cdp_url``, which also covers
[::1]). Everything here degrades to ``None``/``[]`` -- never raises -- so
a closed browser reads as "no data", not a collector crash.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_PORT, discover_local_cdp_url

logger = logging.getLogger(__name__)

_GROK_ORIGIN = "https://grok.com"
_RATE_LIMITS_PATH = "/rest/rate-limits"
# Grok's web app sends its model list here to get per-model windows back.
_DEFAULT_MODELS = ["grok-4", "grok-4-heavy"]


#: A frozen renderer answers the browser-level websocket handshake but never
#: replies to Runtime.evaluate, so liveness has to be probed with a real
#: evaluation and a short timeout rather than inferred from the connection.
_LIVENESS_TIMEOUT = 3.0
#: How long to wait for a tab WE opened to become evaluable. Polled, not slept:
#: the fetch needs only the origin's cookies, not a fully painted app, so it is
#: usually ready well before this.
_NEW_TAB_SETTLE_SECONDS = 10


def _find_grok_targets(http_url: str) -> list[tuple[str, str]]:
    """Return ``(target_id, webSocketDebuggerUrl)`` for every grok.com page tab.

    A LIST, not the first match: a long-backgrounded tab can be frozen while
    another is fine, and picking blindly is what left the xai row unavailable
    for 39h on 2026-08-25 (see ``_usable_grok_target``).
    """
    import urllib.request

    found: list[tuple[str, str]] = []
    try:
        with urllib.request.urlopen(f"{http_url}/json", timeout=3.0) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return found
    for target in targets:
        if not isinstance(target, dict):
            continue
        url = str(target.get("url") or "")
        ws = str(target.get("webSocketDebuggerUrl") or "")
        tid = str(target.get("id") or "")
        if url.startswith(_GROK_ORIGIN) and target.get("type") == "page" and ws and tid:
            found.append((tid, ws))
    return found


def _target_is_responsive(ws_url: str, *, timeout: float = _LIVENESS_TIMEOUT) -> bool:
    """Whether the tab's renderer actually executes JavaScript right now.

    Chrome freezes and discards background tabs. A frozen tab still appears in
    ``/json/list`` with a valid webSocketDebuggerUrl, and the websocket still
    connects -- only the evaluation never comes back. Nothing short of running
    an expression distinguishes the two.
    """
    try:
        _cdp_call(
            ws_url,
            "Runtime.evaluate",
            {"expression": "1", "returnByValue": True},
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _new_grok_target(http_url: str) -> Optional[tuple[str, str]]:
    """Open a background grok.com tab; return ``(target_id, ws_url)``.

    The id is returned so the caller can CLOSE what it opened -- a tab per
    collection would otherwise accumulate 288 grok.com tabs a day.
    """
    import urllib.request

    try:
        # PUT: newer Chrome requires the PUT method on /json/new.
        req = urllib.request.Request(
            f"{http_url}/json/new?{_GROK_ORIGIN}", method="PUT"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            target = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(target, dict):
        return None
    ws = str(target.get("webSocketDebuggerUrl") or "")
    tid = str(target.get("id") or "")
    return (tid, ws) if ws and tid else None


def _close_target(http_url: str, target_id: str) -> None:
    """Close a tab we opened. Best-effort: a leaked tab must not fail a fetch."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"{http_url}/json/close/{target_id}", timeout=5.0
        ) as resp:
            resp.read()
    except Exception as exc:
        logger.debug("grok_session: could not close target %s: %s", target_id, exc)


def _usable_grok_target(http_url: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(target_id_we_opened_or_None, ws_url_or_None)``.

    Prefers a RESPONSIVE tab the user already has open, so the ordinary case
    costs nothing and touches nothing. Falls back to opening our own only when
    every existing grok.com tab is frozen -- deliberately in preference to
    reviving theirs, since the only ways to thaw a tab (bringToFront, reload)
    either steal focus or discard an in-progress conversation.
    """
    for tid, ws in _find_grok_targets(http_url):
        if _target_is_responsive(ws):
            return None, ws
        logger.debug("grok_session: grok.com tab %s is frozen; skipping", tid)

    opened = _new_grok_target(http_url)
    if opened is None:
        return None, None
    tid, ws = opened
    import time

    for _ in range(_NEW_TAB_SETTLE_SECONDS):
        if _target_is_responsive(ws):
            return tid, ws
        time.sleep(1.0)
    # Hand it back anyway -- the caller closes it either way, and the fetch is
    # allowed one last try against its own longer timeout.
    return tid, ws


def _cdp_call(ws_url: str, method: str, params: dict, *, timeout: float) -> dict:
    """One CDP command/response round trip on a fresh connection."""
    from websocket import create_connection

    ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        deadline_guard = max(1, int(timeout))
        for _ in range(deadline_guard * 10):  # skip any event frames
            frame = ws.recv()
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8")
            message = json.loads(frame)
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"CDP error: {message['error']}")
                return message.get("result") or {}
        raise RuntimeError("CDP response timeout")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _evaluate_rate_limits(ws_url: str, *, timeout: float) -> Optional[list]:
    """Run fetch('/rest/rate-limits') inside the grok.com page context."""
    script = """
(async () => {
  const models = %s;
  const res = await fetch('%s', {
    method: 'POST',
    headers: {'content-type': 'text/plain;charset=UTF-8'},
    body: JSON.stringify({requestKind: 'default', modelName: models[0]}),
    credentials: 'include',
  });
  if (!res.ok) return {ok: false, status: res.status};
  return {ok: true, payload: await res.json()};
})()
""" % (
        json.dumps(_DEFAULT_MODELS),
        _RATE_LIMITS_PATH,
    )
    result = _cdp_call(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": script,
            "awaitPromise": True,
            "returnByValue": True,
        },
        timeout=timeout,
    )
    value = result.get("result") or {}
    if value.get("subtype") == "error" or result.get("exceptionDetails"):
        return None
    payload = value.get("value")
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    data = payload.get("payload")
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else None


def fetch_grok_rate_limits(
    *,
    base_url: Optional[str] = None,
    timeout: float = 15.0,
) -> Optional[tuple[float, float, Optional[datetime]]]:
    """Return ``(remainingQueries, totalQueries, reset_at)`` from grok.com.

    ``None`` when no CDP browser is reachable, no logged-in grok.com tab
    exists, the endpoint rejects us (logged out), or the shape is unknown.
    """
    port = DEFAULT_BROWSER_CDP_PORT
    http_url = discover_local_cdp_url(port, timeout=1.5)
    if not http_url:
        logger.debug("grok_session: no CDP browser on :%s", port)
        return None
    opened_id, ws_url = _usable_grok_target(http_url)
    if not ws_url:
        logger.debug("grok_session: no grok.com tab available")
        return None

    entries: Optional[list] = None
    try:
        # A freshly-opened tab needs a moment for the app + session to load;
        # retry once after a short settle before giving up.
        entries = _evaluate_rate_limits(ws_url, timeout=timeout)
        if entries is None:
            import time

            time.sleep(3.0)
            entries = _evaluate_rate_limits(ws_url, timeout=timeout)
    except Exception as exc:
        logger.debug("grok_session: evaluate failed: %s", exc)
        return None
    finally:
        # Only ever closes a tab THIS call opened; the user's own tab is left
        # exactly as found, open or frozen.
        if opened_id:
            _close_target(http_url, opened_id)
    if not entries:
        return None

    # Prefer the first entry with a usable remaining/total pair.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        window = entry.get("remainingWindow") or entry
        remaining = entry.get("remainingQueries")
        total = entry.get("totalQueries")
        if remaining is None or total in (None, 0):
            continue
        reset_at: Optional[datetime] = None
        raw_reset = (
            window.get("resetTime")
            if isinstance(window, dict)
            else entry.get("resetsAt")
        )
        if isinstance(raw_reset, str) and raw_reset:
            try:
                text = raw_reset.strip().replace("Z", "+00:00")
                reset_at = datetime.fromisoformat(text)
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
            except ValueError:
                reset_at = None
        return (float(remaining), float(total), reset_at)
    return None
