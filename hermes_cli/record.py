"""``hermes record`` — demonstrate a browser workflow, save it as a recording.

Attaches to the user's live Chromium-family browser over CDP (the same
endpoint resolution as ``/browser connect`` / the ``browser_cdp`` tool),
injects a small JavaScript event recorder into the active tab, and buffers
what the user does until Ctrl-C. The result is a recording JSON under
``HERMES_HOME/recordings/`` that ``/learn`` recognizes as a skill source:

    hermes record --slug checkout-flow
    ... click around in your browser ...
    Ctrl-C
    hermes chat "/learn recording ~/.hermes/recordings/checkout-flow-....json"

Captured event types (documented contract — see ``RECORDER_JS``):

- ``click``     — mouse clicks: CSS selector path, tag name, trimmed text.
- ``input``     — final value of a field, captured on ``change`` (NOT per
                  keystroke). Password fields are masked AT CAPTURE TIME in
                  the page: the value never leaves the browser — the recorder
                  substitutes a ``{SECRET:<name>}`` placeholder.
- ``enter``     — Enter-key submissions (selector of the focused element).
- ``navigate``  — top-frame navigations, observed CDP-side via
                  ``Page.frameNavigated`` (URL + timestamp).

Recording JSON schema (``RECORDING_VERSION``)::

    {
      "version": 1,
      "started_at": "2026-07-26T12:00:00+00:00",
      "url": "https://example.com/start",
      "steps": [
        {"t": 0.0, "type": "click", "selector": "#login > button", "text": "Sign in"},
        {"t": 1.2, "type": "input", "selector": "input[name=user]", "value": "alice"},
        {"t": 2.0, "type": "input", "selector": "input[name=pw]", "value": "{SECRET:pw}"},
        {"t": 2.5, "type": "enter", "selector": "input[name=pw]"},
        {"t": 3.1, "type": "navigate", "url": "https://example.com/home"}
      ]
    }

Secret handling is two-layered: the recorder JS masks ``type=password`` (and
autocomplete=current-password/new-password) fields in-page, and the Python
normalizer (:func:`normalize_event`) masks them AGAIN as defense-in-depth, so
a raw password value can never reach disk even if a page mutates input types
mid-flight.

If no CDP endpoint is reachable (or the ``websockets`` package is missing),
``hermes record --manual`` offers a fallback: the user performs the steps in
any browser and types what they did, one step per line; the same JSON schema
is written with ``type: "manual"`` steps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RECORDING_VERSION = 1

# Event types the recorder emits. Anything else coming over the binding is
# dropped by normalize_event() — forward-compat with newer recorder JS.
KNOWN_EVENT_TYPES = ("click", "input", "enter", "navigate", "manual")

# Input types / autocomplete hints whose values must never be stored raw.
SECRET_INPUT_TYPES = ("password",)
SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code", "cc-number", "cc-csc")

_SECRET_PLACEHOLDER_RE = re.compile(r"^\{SECRET:[^{}]*\}$")


# ---------------------------------------------------------------------------
# Recorder JavaScript
# ---------------------------------------------------------------------------
# Injected via Page.addScriptToEvaluateOnNewDocument (survives navigations)
# AND Runtime.evaluate (covers the already-loaded page). Events are shipped
# to Python through the Runtime.addBinding channel `__hermesRecord`.
#
# Captured event types: click, input (on `change`), enter. Navigations are
# observed CDP-side (Page.frameNavigated), not in-page.
#
# PASSWORD MASKING HAPPENS HERE, AT CAPTURE TIME: for type=password (or
# secret-ish autocomplete) fields the recorder never reads a placeholder from
# the real value — it emits `{SECRET:<field name>}` instead. The raw value
# never crosses the CDP wire.
RECORDER_JS = r"""
(() => {
  if (window.__hermesRecorderInstalled) return;
  window.__hermesRecorderInstalled = true;

  const SECRET_TYPES = ["password"];
  const SECRET_AUTOCOMPLETE = ["current-password", "new-password", "one-time-code", "cc-number", "cc-csc"];

  const send = (ev) => {
    try {
      if (typeof window.__hermesRecord === "function") {
        window.__hermesRecord(JSON.stringify(ev));
      }
    } catch (e) { /* recorder must never break the page */ }
  };

  // Robust-ish CSS path: prefer id, then unique-enough attribute hooks,
  // else nth-of-type chain up to the nearest id/body.
  const cssPath = (el) => {
    if (!(el instanceof Element)) return "";
    const parts = [];
    while (el && el.nodeType === Node.ELEMENT_NODE) {
      let part = el.nodeName.toLowerCase();
      if (el.id) {
        parts.unshift(part + "#" + CSS.escape(el.id));
        break;
      }
      const name = el.getAttribute("name");
      const testId = el.getAttribute("data-testid") || el.getAttribute("data-test-id");
      const aria = el.getAttribute("aria-label");
      if (testId) part += `[data-testid="${testId}"]`;
      else if (name) part += `[name="${name}"]`;
      else if (aria) part += `[aria-label="${aria}"]`;
      else {
        let sib = el, nth = 1;
        while ((sib = sib.previousElementSibling)) {
          if (sib.nodeName === el.nodeName) nth++;
        }
        if (nth > 1 || el.nextElementSibling) part += `:nth-of-type(${nth})`;
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const isSecretField = (el) => {
    const type = (el.type || "").toLowerCase();
    const ac = (el.getAttribute && (el.getAttribute("autocomplete") || "") || "").toLowerCase();
    return SECRET_TYPES.includes(type) || SECRET_AUTOCOMPLETE.includes(ac);
  };

  const fieldName = (el) =>
    el.name || el.id || el.getAttribute("aria-label") || el.placeholder || el.type || "secret";

  document.addEventListener("click", (e) => {
    const el = e.target instanceof Element ? e.target : null;
    if (!el) return;
    send({
      t: Date.now(),
      type: "click",
      selector: cssPath(el),
      tag: el.nodeName.toLowerCase(),
      text: (el.innerText || el.value || "").trim().slice(0, 120),
    });
  }, true);

  // Final value on change — not per keystroke. Passwords masked HERE.
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || typeof el.value === "undefined") return;
    const secret = isSecretField(el);
    send({
      t: Date.now(),
      type: "input",
      selector: cssPath(el),
      tag: (el.nodeName || "").toLowerCase(),
      inputType: (el.type || "").toLowerCase(),
      name: fieldName(el),
      value: secret ? "{SECRET:" + fieldName(el) + "}" : String(el.value).slice(0, 500),
    });
  }, true);

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const el = e.target instanceof Element ? e.target : null;
    send({
      t: Date.now(),
      type: "enter",
      selector: el ? cssPath(el) : "",
    });
  }, true);
})();
"""

BINDING_NAME = "__hermesRecord"


# ---------------------------------------------------------------------------
# Pure event-processing functions (unit-tested with synthetic CDP events)
# ---------------------------------------------------------------------------


def is_secret_placeholder(value: Any) -> bool:
    """True when ``value`` is already a ``{SECRET:name}`` placeholder."""
    return isinstance(value, str) and bool(_SECRET_PLACEHOLDER_RE.match(value))


def mask_value(value: Any, input_type: str = "", autocomplete: str = "", name: str = "") -> str:
    """Return the storable form of an input value, masking secrets.

    Defense-in-depth mirror of the in-page masking: even if the recorder JS
    somehow shipped a raw password (page swapped ``type`` after capture,
    older recorder build, hand-crafted event), the Python side masks again
    before anything is buffered or written.
    """
    text = "" if value is None else str(value)
    if is_secret_placeholder(text):
        return text
    itype = (input_type or "").strip().lower()
    ac = (autocomplete or "").strip().lower()
    if itype in SECRET_INPUT_TYPES or ac in SECRET_AUTOCOMPLETE:
        label = (name or "").strip() or itype or "secret"
        return "{SECRET:" + label + "}"
    return text


def selector_for(event: Dict[str, Any]) -> str:
    """Selector with fallbacks: explicit selector → tag[+text hint] → '*'.

    The recorder JS computes a css path, but events can arrive without one
    (detached nodes, shadow-DOM edges, manual events). Never return ''.
    """
    selector = str(event.get("selector") or "").strip()
    if selector:
        return selector
    tag = str(event.get("tag") or "").strip().lower()
    text = str(event.get("text") or "").strip()
    if tag and text:
        return f"{tag}:contains({text[:40]!r})"
    if tag:
        return tag
    return "*"


def normalize_event(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one recorder/CDP event into a recording step, or None to drop.

    Accepts the JSON payloads the recorder JS ships over the CDP binding
    (click/input/enter), synthesized ``navigate`` events from
    ``Page.frameNavigated``, and ``manual`` steps. Unknown types and events
    without a usable payload are dropped.
    """
    if not isinstance(raw, dict):
        return None
    etype = str(raw.get("type") or "").strip().lower()
    if etype not in KNOWN_EVENT_TYPES:
        return None

    try:
        t = float(raw.get("t") or 0.0)
    except (TypeError, ValueError):
        t = 0.0

    step: Dict[str, Any] = {"t": t, "type": etype}

    if etype == "navigate":
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        step["url"] = url
        return step

    if etype == "manual":
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        step["text"] = text
        return step

    step["selector"] = selector_for(raw)

    if etype == "click":
        text = str(raw.get("text") or "").strip()
        if text:
            step["text"] = text
    elif etype == "input":
        step["value"] = mask_value(
            raw.get("value"),
            input_type=str(raw.get("inputType") or ""),
            autocomplete=str(raw.get("autocomplete") or ""),
            name=str(raw.get("name") or ""),
        )
    # "enter" carries only t/type/selector.
    return step


def build_recording(
    url: str,
    started_at: str,
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the recording document: normalize, order, rebase timestamps.

    Steps are sorted by capture time and their ``t`` values rebased so the
    first step is ``t=0.0`` (seconds, wall-clock ms in → relative s out when
    the inputs look like epoch-milliseconds).
    """
    steps = [s for s in (normalize_event(e) for e in events) if s is not None]
    steps.sort(key=lambda s: s["t"])
    if steps:
        base = steps[0]["t"]
        for s in steps:
            rel = s["t"] - base
            # Epoch-ms deltas → seconds; already-relative small values pass through.
            s["t"] = round(rel / 1000.0, 3) if base > 1e10 else round(rel, 3)
    return {
        "version": RECORDING_VERSION,
        "started_at": started_at,
        "url": url,
        "steps": steps,
    }


def default_recordings_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "recordings"


def save_recording(recording: Dict[str, Any], slug: str, recordings_dir: Optional[Path] = None) -> Path:
    """Write the recording JSON to ``<dir>/<slug>-<ts>.json`` and return the path."""
    rec_dir = recordings_dir or default_recordings_dir()
    rec_dir.mkdir(parents=True, exist_ok=True)
    safe_slug = re.sub(r"[^a-z0-9-]+", "-", (slug or "recording").strip().lower()).strip("-") or "recording"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = rec_dir / f"{safe_slug}-{ts}.json"
    path.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def list_recordings(recordings_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List saved recordings (newest first) with step counts and start URLs."""
    rec_dir = recordings_dir or default_recordings_dir()
    if not rec_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(rec_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        entry: Dict[str, Any] = {"path": str(path), "name": path.name}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            entry["url"] = doc.get("url", "")
            entry["started_at"] = doc.get("started_at", "")
            entry["steps"] = len(doc.get("steps", []) or [])
        except Exception:
            entry["error"] = "unreadable"
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# CDP recording session
# ---------------------------------------------------------------------------


def _resolve_cdp_endpoint() -> str:
    """Same endpoint resolution as the ``browser_cdp`` tool (/browser connect)."""
    try:
        from tools.browser_cdp_tool import _resolve_cdp_endpoint as _resolve

        return _resolve()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("record: CDP endpoint resolution failed: %s", exc)
        return ""


async def _record_over_cdp(
    ws_url: str,
    events: List[Dict[str, Any]],
    stop: "threading.Event",
    state: Dict[str, Any],
) -> None:
    """Attach to the active page target, inject the recorder, buffer events.

    Runs until ``stop`` is set (Ctrl-C in the foreground thread). All raw
    payloads are appended to ``events``; normalization happens at save time
    via :func:`build_recording`.
    """
    import websockets

    async with websockets.connect(
        ws_url, max_size=None, open_timeout=15, close_timeout=5, ping_interval=None
    ) as ws:
        next_id = [0]

        async def send(method: str, params: Dict[str, Any] | None = None, session_id: str | None = None) -> int:
            next_id[0] += 1
            msg: Dict[str, Any] = {"id": next_id[0], "method": method, "params": params or {}}
            if session_id:
                msg["sessionId"] = session_id
            await ws.send(json.dumps(msg))
            return next_id[0]

        async def wait_result(call_id: int, timeout: float = 15.0) -> Dict[str, Any]:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"CDP call {call_id} timed out")
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if msg.get("id") == call_id:
                    if "error" in msg:
                        raise RuntimeError(f"CDP error: {msg['error']}")
                    return msg.get("result", {})
                _handle_event(msg, events, state)

        # 1. Find the active page target.
        targets = (await wait_result(await send("Target.getTargets"))).get("targetInfos", [])
        page = next(
            (t for t in targets if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")),
            None,
        )
        if page is None:
            raise RuntimeError("No page target found — open a tab in the connected browser first.")
        state["url"] = page.get("url", "")

        # 2. Attach (flattened session).
        attach = await wait_result(
            await send("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
        )
        sid = attach.get("sessionId")
        if not sid:
            raise RuntimeError("Target.attachToTarget did not return a sessionId")
        state["session_id"] = sid

        # 3. Enable domains, register the binding, inject the recorder.
        await wait_result(await send("Runtime.enable", session_id=sid))
        await wait_result(await send("Page.enable", session_id=sid))
        await wait_result(await send("Runtime.addBinding", {"name": BINDING_NAME}, session_id=sid))
        await wait_result(
            await send("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER_JS}, session_id=sid)
        )
        await wait_result(await send("Runtime.evaluate", {"expression": RECORDER_JS}, session_id=sid))
        state["ready"] = True

        # 4. Buffer events until told to stop.
        while not stop.is_set():
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            _handle_event(msg, events, state)


def _handle_event(msg: Dict[str, Any], events: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
    """Route one CDP protocol message into the raw event buffer.

    - ``Runtime.bindingCalled`` for our binding → parsed recorder payload.
    - ``Page.frameNavigated`` (top frame only) → synthesized navigate event.
    Anything else is ignored. Pure function of its inputs; unit-testable
    with synthetic CDP messages.
    """
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "Runtime.bindingCalled" and params.get("name") == BINDING_NAME:
        try:
            payload = json.loads(params.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(payload, dict):
            events.append(payload)
    elif method == "Page.frameNavigated":
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return  # subframe — only top-level navigations become steps
        url = str(frame.get("url") or "").strip()
        if not url or url.startswith(("about:", "chrome://", "devtools://")):
            return
        events.append({"t": time.time() * 1000.0, "type": "navigate", "url": url})


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def _cmd_record(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        return _cmd_list(args)
    if getattr(args, "manual", False):
        return _run_manual(args)
    return _run_cdp(args)


def _cmd_list(args: argparse.Namespace) -> int:
    recs = list_recordings()
    if not recs:
        print("No recordings yet. Start one with: hermes record --slug my-flow")
        return 0
    print(f"Recordings in {default_recordings_dir()}:\n")
    for r in recs:
        if "error" in r:
            print(f"  {r['name']}  (unreadable)")
        else:
            print(f"  {r['name']}  — {r['steps']} steps  {r.get('url', '')}")
    print('\nTurn one into a skill:  hermes chat "/learn recording <path>"')
    return 0


def _run_manual(args: argparse.Namespace) -> int:
    """Manual fallback: user narrates their steps, one per line."""
    print("Manual recording mode — perform your workflow in any browser and")
    print("describe each step here, one per line. Empty line or Ctrl-D to finish.\n")
    started_at = datetime.now(timezone.utc).isoformat()
    url = input("Start URL (optional): ").strip()
    events: List[Dict[str, Any]] = []
    step_no = 0
    while True:
        try:
            line = input(f"step {step_no + 1}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        step_no += 1
        events.append({"t": float(step_no), "type": "manual", "text": line})

    if not events:
        print("Nothing recorded.")
        return 1
    recording = build_recording(url, started_at, events)
    path = save_recording(recording, getattr(args, "slug", None) or "manual")
    print(f"\nSaved {len(recording['steps'])} steps -> {path}")
    print(f'Next: hermes chat "/learn recording {path}"')
    return 0


def _run_cdp(args: argparse.Namespace) -> int:
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("The 'websockets' package is required for CDP recording.")
        print("Install it (pip install websockets) or use: hermes record --manual")
        return 1

    ws_url = _resolve_cdp_endpoint()
    if not ws_url:
        print("No CDP endpoint available. Attach to your browser first:")
        print("  hermes chat, then /browser connect   (or set browser.cdp_url in config.yaml)")
        print("Or record without CDP:  hermes record --manual")
        return 1
    if not ws_url.startswith(("ws://", "wss://")):
        print(f"CDP endpoint is not a WebSocket URL: {ws_url!r}")
        return 1

    events: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"url": "", "ready": False}
    stop = threading.Event()
    error: List[BaseException] = []
    started_at = datetime.now(timezone.utc).isoformat()

    def _runner() -> None:
        try:
            asyncio.run(_record_over_cdp(ws_url, events, stop, state))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    # Wait for the recorder to be injected (or fail fast).
    for _ in range(100):
        if state.get("ready") or error:
            break
        time.sleep(0.1)
    if error:
        print(f"Could not start CDP recorder: {error[0]}")
        print("Fallback: hermes record --manual")
        return 1
    if not state.get("ready"):
        print("Timed out attaching the recorder. Is the browser still running?")
        return 1

    print(f"● Recording {state.get('url') or 'the active tab'}")
    print("  Clicks, typed values (passwords masked), Enter presses, and")
    print("  navigations are being captured. Press Ctrl-C to stop and save.\n")

    try:
        while thread.is_alive():
            time.sleep(0.25)
            if error:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join(timeout=5)

    if error and not events:
        print(f"\nRecorder stopped with an error and captured nothing: {error[0]}")
        return 1

    if not events:
        print("\nNo events captured — nothing to save.")
        return 1

    recording = build_recording(state.get("url", ""), started_at, events)
    path = save_recording(recording, getattr(args, "slug", None) or "recording")
    print(f"\nSaved {len(recording['steps'])} steps -> {path}")
    print(f'Next: hermes chat "/learn recording {path}"')
    return 0


def register_cli(parent: argparse.ArgumentParser) -> None:
    parent.add_argument(
        "--slug",
        default=None,
        help="Name stem for the recording file (default: 'recording').",
    )
    parent.add_argument(
        "--manual",
        action="store_true",
        help="Skip CDP: perform the steps yourself and narrate them, one per line.",
    )
    parent.add_argument(
        "--list",
        action="store_true",
        help="List saved recordings and exit.",
    )
    parent.set_defaults(func=_cmd_record)


if __name__ == "__main__":  # pragma: no cover
    _p = argparse.ArgumentParser(prog="hermes record")
    register_cli(_p)
    _a = _p.parse_args()
    sys.exit(_a.func(_a))
