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


def _find_grok_target(http_url: str) -> Optional[str]:
    """Return a webSocketDebuggerUrl for an existing grok.com tab, if any."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{http_url}/json", timeout=3.0) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    for target in targets:
        if not isinstance(target, dict):
            continue
        url = str(target.get("url") or "")
        ws = str(target.get("webSocketDebuggerUrl") or "")
        if url.startswith(_GROK_ORIGIN) and target.get("type") == "page" and ws:
            return ws
    return None


def _new_grok_target(http_url: str) -> Optional[str]:
    """Open a background grok.com tab via /json/new and return its WS URL."""
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
    ws = str(target.get("webSocketDebuggerUrl") or "") if isinstance(target, dict) else ""
    return ws or None


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
    ws_url = _find_grok_target(http_url) or _new_grok_target(http_url)
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
