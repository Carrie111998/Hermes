"""Gemini usage scrape via AI Studio's own apikey-page RPCs over CDP.

Google ships no usage/quota API for Gemini API billing (v1beta/rateLimits
is 404; BatchGetProjectUsageLimits has no public surface). But the AI
Studio web app itself calls internal MakerSuiteService RPCs when you open
its "API keys" page:

  ListImportedProjects -> [["projects/<id>",1]]        (project discovery)
  BatchGetProjectUsageLimits(["projects/<id>"]) ->
      [[ [<project>, null, ["USD", null, <spent_micros>], ["USD", "<budget_dollars>"]] ]]

so we drive an existing aistudio.google.com tab there over CDP (:9222,
the automation-profile Chrome), intercept at Fetch response stage, and
read the bodies straight off the wire. No request replay, no header
capture: the app builds and sends everything itself; we only listen.
Everything degrades to None -- never raises -- so a closed browser or a
logged-out session reads as "no data", not a collector crash.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_PORT, discover_local_cdp_url

logger = logging.getLogger(__name__)

_AISTUDIO_ORIGIN = "https://aistudio.google.com"
_APIKEY_URL = _AISTUDIO_ORIGIN + "/apikey"
# Response bodies are JSPB arrays; spent micros live at [0][0][2][2] and the
# budget-dollar string at [0][0][3][1] (verified on the wire 2026-08-23).
# BatchGetProjectUsageLimits fires late in the apikey page's lazy RPC chain
# (~15-20s after navigation), so the settle window must cover the full chain.
_SETTLE_SECONDS = 30.0


def _find_aistudio_target(http_url: str) -> Optional[str]:
    """Return a webSocketDebuggerUrl for an existing aistudio.google.com tab."""
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
        if url.startswith(_AISTUDIO_ORIGIN) and target.get("type") == "page" and ws:
            return ws
    return None


def _new_aistudio_target(http_url: str) -> Optional[str]:
    """Open a background aistudio.google.com/apikey tab via /json/new."""
    import urllib.request

    try:
        # PUT: newer Chrome requires the PUT method on /json/new.
        req = urllib.request.Request(
            f"{http_url}/json/new?{_APIKEY_URL}", method="PUT"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            target = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    ws = str(target.get("webSocketDebuggerUrl") or "") if isinstance(target, dict) else ""
    return ws or None


class _Interceptor:
    """One Fetch-domain session over a persistent websocket.

    Unlike grok_session's one-shot ``_cdp_call``, response-stage interception
    needs many commands interleaved with a stream of events on ONE connection,
    so this wraps send/recv with request-id matching.
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, *, timeout: float = 10.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline_guard = max(1, int(timeout)) * 20
        for _ in range(deadline_guard):
            frame = self._recv()
            message = json.loads(frame)
            if isinstance(message, dict) and message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"CDP error: {message['error']}")
                return message.get("result") or {}
        raise RuntimeError(f"CDP response timeout: {method}")

    def recv_event(self, seconds: float) -> Optional[dict]:
        """Next protocol event within ``seconds``, else None."""
        end = time.time() + seconds
        while time.time() < end:
            remaining = max(0.05, end - time.time())
            try:
                frame = self._recv(timeout=remaining)
            except Exception:
                return None
            message = json.loads(frame)
            if isinstance(message, dict) and "method" in message:
                return message
        return None

    def _recv(self, timeout: float = 5.0):
        self._ws.settimeout(timeout)
        frame = self._ws.recv()
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        return frame


def _decode_body(call_result: dict) -> str:
    body = call_result.get("body")
    if not isinstance(body, str):
        return ""
    if call_result.get("base64Encoded"):
        import base64

        try:
            body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:
            return ""
    return body


def _parse_usage_limits(jspb_text: str) -> Optional[tuple[float, float]]:
    """Extract ``(spent_usd, budget_usd)`` from BatchGetProjectUsageLimits JSPB.

    Wire shape (2026-08-23):
      [["projects/<id>",null,["USD",null,<spent_micros>],["USD","<budget>"]]]
    """
    try:
        data = json.loads(jspb_text)
    except ValueError:
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        return None
    for row in data[0]:
        if not isinstance(row, list) or len(row) < 4:
            continue
        spent_cell, budget_cell = row[2], row[3]
        if not (isinstance(spent_cell, list) and len(spent_cell) >= 3):
            continue
        raw_spent = spent_cell[2]
        raw_budget = budget_cell[1] if isinstance(budget_cell, list) and len(budget_cell) >= 2 else None
        try:
            spent_usd = float(raw_spent) / 1_000_000.0
            budget_usd = float(raw_budget)
        except (TypeError, ValueError):
            continue
        if budget_usd <= 0:
            continue
        used_pct = max(0.0, min(100.0, 100.0 * spent_usd / budget_usd))
        return (used_pct, budget_usd)
    return None


def fetch_gemini_budget_usage(
    *,
    timeout: float = 15.0,
) -> Optional[tuple[float, float]]:
    """Return ``(used_percent, budget_usd)`` from AI Studio's apikey page RPCs.

    ``None`` when no CDP browser is reachable, no aistudio.google.com tab can
    be opened, the page never issues the two RPCs (logged out), or the
    response shapes are unknown.
    """
    http_url = discover_local_cdp_url(DEFAULT_BROWSER_CDP_PORT, timeout=1.5)
    if not http_url:
        logger.debug("gemini_session: no CDP browser on :%s", DEFAULT_BROWSER_CDP_PORT)
        return None
    ws_url = _find_aistudio_target(http_url) or _new_aistudio_target(http_url)
    if not ws_url:
        logger.debug("gemini_session: no aistudio.google.com tab available")
        return None

    from websocket import create_connection

    try:
        ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
    except Exception as exc:
        logger.debug("gemini_session: connect failed: %s", exc)
        return None
    interceptor = _Interceptor(ws)
    limits: Optional[tuple[float, float]] = None
    try:
        interceptor.call(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*MakerSuiteService/*", "requestStage": "Response"}]},
            timeout=timeout,
        )
        interceptor.call("Page.enable", {}, timeout=timeout)
        interceptor.call("Page.navigate", {"url": _APIKEY_URL}, timeout=timeout)

        deadline = time.time() + _SETTLE_SECONDS
        while time.time() < deadline and limits is None:
            event = interceptor.recv_event(max(0.1, deadline - time.time()))
            if not event or event.get("method") != "Fetch.requestPaused":
                continue
            params = event.get("params") or {}
            url_tail = str((params.get("request") or {}).get("url", "")).rsplit("/", 1)[-1]
            if url_tail == "ListImportedProjects":
                # Project discovery is not required to produce a snapshot (the
                # usage-limits response names the project itself); parsed only
                # in tests via _parse_imported_projects.
                interceptor.call(
                    "Fetch.getResponseBody",
                    {"requestId": params.get("requestId")},
                    timeout=timeout,
                )
            elif url_tail == "BatchGetProjectUsageLimits":
                body = _decode_body(interceptor.call(
                    "Fetch.getResponseBody",
                    {"requestId": params.get("requestId")},
                    timeout=timeout,
                ))
                limits = _parse_usage_limits(body)
            interceptor.call(
                "Fetch.continueRequest", {"requestId": params.get("requestId")}, timeout=timeout
            )
    except Exception as exc:
        logger.debug("gemini_session: interception failed: %s", exc)
        limits = None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return limits


def _parse_imported_projects(jspb_text: str) -> Optional[list]:
    """Extract project refs from ListImportedProjects JSPB.

    Wire shape (2026-08-23): [null,null,null,null,[["projects/<id>",1]]]
    Kept for diagnostics/tests; the usage-limits response already names the
    project itself, so this is not required to produce a snapshot.
    """
    try:
        data = json.loads(jspb_text)
    except ValueError:
        return None
    projects: list = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            if (
                len(node) == 2
                and isinstance(node[0], str)
                and node[0].startswith("projects/")
            ):
                projects.append(node[0])
            else:
                stack.extend(node)
    return projects or None
