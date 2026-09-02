"""Native Warp terminal "CLI coding agent" notification support for Hermes.

WHY THIS EXISTS
  Warp (warp.dev) gives first-class treatment to CLI coding agents it
  recognizes -- rich input editor, vertical tabs with live status, and
  desktop/in-app notifications on task completion, permission requests, and
  idle prompts. Warp's own docs (docs.warp.dev/agents/cli-agents/overview)
  explicitly list Hermes as a *recognized* agent, but note it "doesn't
  support agent notifications yet" -- unlike Claude Code, Codex, and
  OpenCode, which each implement Warp's notification protocol.

  Root cause (2026-09-02 investigation): Claude Code and OpenCode ship a
  small Warp plugin (github.com/warpdotdev/claude-code-warp) that emits an
  OSC 777 escape sequence at a few lifecycle points. Codex does it via a
  native config flag. Hermes has neither -- so Warp has no signal to key
  off, which is why bell_on_complete (a plain \\a BEL) doesn't produce
  Warp's tab status/notification treatment: Warp is looking for OSC 777
  with a specific JSON payload, not a bare bell.

  This module ports that same protocol natively into Hermes, using the
  officially recognized `agent: "hermes"` slot the payload schema already
  supports (the schema is agent-name-keyed, not Claude-specific).

PROTOCOL (reverse-engineered from the public claude-code-warp plugin source,
commit as of 2026-09-02, MIT-equivalent OSS license -- see LICENSE there):
  - Escape sequence: OSC 777 notify, i.e. `\\033]777;notify;<title>;<body>\\007`
  - `title` is always the literal string `warp://cli-agent` -- this is how
    Warp distinguishes a structured agent-protocol notification from a
    plain OSC 777 desktop notification.
  - `body` is a compact JSON object:
      {
        "v": <protocol version, negotiated via WARP_CLI_AGENT_PROTOCOL_VERSION>,
        "agent": "hermes",
        "event": "stop" | "idle_prompt" | "permission_request" | ...,
        "session_id": "...",
        "cwd": "...",
        "project": "<basename of cwd>",
        ... event-specific fields (e.g. "summary", "query", "response")
      }
  - Gating: only emit when Warp has advertised protocol support via the
    WARP_CLI_AGENT_PROTOCOL_VERSION env var (set by the Warp app itself in
    the child shell it launches) AND WARP_CLIENT_VERSION is present. This
    mirrors should-use-structured.sh in the reference plugin -- it exists
    because older/broken Warp builds set the version var without actually
    being able to render the structured payload, so a version floor
    protects against sending noise those builds can't display.
  - Delivery: this module writes directly to /dev/tty (POSIX) so it works
    even when stdout is being captured/redirected elsewhere in the CLI
    render loop. On non-POSIX or when /dev/tty is unavailable, this is a
    silent no-op -- never raise, never block the agent loop.

SCOPE
  Deliberately minimal: only the two events Hermes's own lifecycle exposes
  cleanly today -- response-complete ("stop", same moment bell_on_complete
  already fires) and approval-requested ("permission_request", same moment
  the approval UI is painted). Idle-prompt / notification-type events can
  be added later if a matching Hermes lifecycle hook exists; this module's
  public functions are additive and safe to call from anywhere.
"""
from __future__ import annotations

import json
import os
import sys

# The protocol version this module knows how to produce. Mirrors the
# reference plugin's PLUGIN_CURRENT_PROTOCOL_VERSION.
_PROTOCOL_VERSION = 1


def _negotiate_protocol_version() -> int:
    """min(our version, Warp's advertised version), defaulting to 1 if Warp
    doesn't advertise one at all (matches build-payload.sh's negotiation)."""
    try:
        warp_version = int(os.environ.get("WARP_CLI_AGENT_PROTOCOL_VERSION", "1"))
    except ValueError:
        warp_version = 1
    return min(warp_version, _PROTOCOL_VERSION)


def _should_notify() -> bool:
    """Only emit when running inside Warp AND Warp has advertised structured
    CLI-agent-notification support. Mirrors should-use-structured.sh: a
    missing protocol-version or client-version var means either we're not
    in Warp, or we're in a Warp build old enough that it can't render the
    payload even though it might set the var -- either way, staying silent
    is strictly safer than emitting escape codes nobody can parse."""
    if os.environ.get("TERM_PROGRAM") != "WarpTerminal":
        return False
    if not os.environ.get("WARP_CLI_AGENT_PROTOCOL_VERSION"):
        return False
    if not os.environ.get("WARP_CLIENT_VERSION"):
        return False
    return True


def _emit_osc777(title: str, body: str) -> None:
    """Write the OSC 777 notify sequence directly to /dev/tty. Never raises;
    a failure here must never interrupt the agent loop."""
    seq = f"\033]777;notify;{title};{body}\007"
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
            tty.flush()
    except OSError:
        pass


def _build_payload(event: str, session_id: str = "", cwd: str = "", **extra) -> str:
    cwd = cwd or os.getcwd()
    payload = {
        "v": _negotiate_protocol_version(),
        "agent": "hermes",
        "event": event,
        "session_id": session_id or "",
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
    }
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":"))


def notify_stop(session_id: str = "", query: str = "", response: str = "") -> None:
    """Call when the agent finishes a response turn (the same moment
    bell_on_complete fires). Maps to Claude Code's Stop hook / "event":
    "stop"."""
    if not _should_notify():
        return
    body = _build_payload(
        "stop",
        session_id=session_id,
        query=(query or "")[:200],
        response=(response or "")[:200],
    )
    _emit_osc777("warp://cli-agent", body)


def notify_permission_request(
    tool_name: str, summary: str = "", session_id: str = ""
) -> None:
    """Call when the approval UI is shown for a dangerous command / tool
    call. Maps to Claude Code's PermissionRequest hook."""
    if not _should_notify():
        return
    body = _build_payload(
        "permission_request",
        session_id=session_id,
        tool_name=tool_name,
        summary=(summary or f"Wants to run {tool_name}")[:200],
    )
    _emit_osc777("warp://cli-agent", body)
