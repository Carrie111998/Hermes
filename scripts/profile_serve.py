#!/usr/bin/env python3
"""
profile-serve — bring a Hermes profile up over HTTP.

The kernel's old `run_agent` was the OpenClaw launcher; it died with the SDK, so
"call an agent" stopped bringing anything up. This restores it against Hermes
profiles: agent name == profile name (aligned 2026-08-01), and a call here runs
that profile with its own persona, skills, auth and the shared /root/nexus-core-edge
spaces, then returns the answer.

Two front doors, one engine (`<profile> chat -q <task>`):
  (a) POST /run                     {"profile","task"}          -> {"output"}
  (c) POST /v1/chat/completions     OpenAI-shaped, model=profile -> OpenAI-shaped
      GET  /v1/models               lists the served profiles

GATE (the "clean shape", 2026-08-01):
  1. Single-homed bind — we bind THIS container's ai-shared address (172.x), not
     0.0.0.0. hermes is multi-homed (ai-shared + a tailnet iface); 0.0.0.0 would
     start answering on the tailnet the moment the node signs in — an unauthed
     hole that opens on login. Pinning the 172.x means growing an interface can
     never expose the runtime. Fail-closed: if no safe address is found we refuse
     to start rather than fall back to all-interfaces.
  2. Bearer token — POST endpoints require `Authorization: Bearer $PROFILE_SERVE_TOKEN`.
     This is a SEPARATE secret from FRIDAY_API_KEY (the kernel's front-door key):
     east-west, single-holder (only the kernel), rotatable on its own. No token
     configured => everything is refused (closed by default). Because only the
     kernel holds the token, an authenticated request's X-Allowed-Groups is
     trustworthy without extra signing — forging it needs BOTH the 172.x net AND
     the token.
  3. ALLOWLIST — only profiles in AGENT_ALLOWLIST can be brought up, each carrying
     the kernel tool-groups it may call back into. Off the list => 403.

Stdlib only — no pip in the image.
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The privilege-drop shim, NOT cli.py directly: it does s6-setuidgid to the
# hermes user (UID 1000) before exec'ing the venv binary. Calling cli.py raw runs
# as root and auth/permissions break (the "hermes is UID 1000" trap).
HERMES_SHIM = "/opt/hermes/bin/hermes"
PORT = int(os.environ.get("PROFILE_SERVE_PORT", "8642"))
MAX_TURNS = int(os.environ.get("PROFILE_SERVE_MAX_TURNS", "40"))
TIMEOUT = int(os.environ.get("PROFILE_SERVE_TIMEOUT", "540"))

# East-west bearer secret (gate #2). Shared ONLY with the kernel via friday-secrets
# (kernel.env + hermes.env). Distinct from FRIDAY_API_KEY. Empty => fail closed.
TOKEN = os.environ.get("PROFILE_SERVE_TOKEN", "").strip()

# ai-shared subnet prefix used to pick our bind address when PROFILE_SERVE_BIND is
# not set. Docker's bridge subnet is 172.x; the tailnet is 100.64/10 (CGNAT) — we
# never want that one. Override the prefix if the ai-shared subnet ever changes.
NET_PREFIX = os.environ.get("PROFILE_SERVE_NET_PREFIX", "172.").strip()


def resolve_bind() -> str:
    """Pick the ai-shared (172.x) address to bind. Never 0.0.0.0, never the tailnet.

    Explicit PROFILE_SERVE_BIND wins (and is validated). Otherwise we take the
    container's own addresses and keep the one on the ai-shared subnet. Binding a
    specific address means we physically cannot answer on any other interface the
    container grows later (tailnet, a second net) — single-homed by construction.
    Fail closed: no safe address => refuse to start (do NOT fall back to 0.0.0.0).
    """
    explicit = os.environ.get("PROFILE_SERVE_BIND", "").strip()
    if explicit:
        if explicit in ("0.0.0.0", "::", "") or explicit.startswith("127."):
            raise SystemExit(
                f"profile-serve: refusing PROFILE_SERVE_BIND={explicit!r} — must be a "
                f"specific ai-shared address, never all-interfaces/loopback.")
        return explicit
    candidates = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(res[4][0])
    except socket.gaierror:
        pass
    for ip in sorted(candidates):
        if ip.startswith(NET_PREFIX) and not ip.startswith("127."):
            return ip
    raise SystemExit(
        f"profile-serve: refusing to start — no ai-shared address matching prefix "
        f"{NET_PREFIX!r} found (candidates={sorted(candidates) or 'none'}). Set "
        f"PROFILE_SERVE_BIND explicitly. Will NOT fall back to 0.0.0.0.")

# Agent tool-group allowlist. Key = profile/agent; value = kernel tool GROUPS it
# may call back into (matches fridai/kernel/tool_groups.py). The kernel enforces
# it from the X-Agent header; here it gates who can be brought up at all.
# Least privilege: absent = not servable.
AGENT_ALLOWLIST: dict[str, list[str]] = {
    "fridai":    ["system", "state", "meta", "workspace", "rag", "comms", "agents"],
    "neo":       ["system", "state", "meta", "security", "rag"],
    "validator": ["system", "state", "meta", "workspace", "rag"],
    "librarian": ["system", "state", "meta", "workspace", "rag"],
    "atlas":     ["system", "state", "meta", "workspace", "rag"],
    "vector":    ["system", "state", "meta", "workspace", "rag", "system"],
    "orion":     ["system", "state", "meta", "workspace", "rag"],
    "venus":     ["system", "state", "meta", "comms", "workspace"],
    "hermes":    ["system", "state", "meta", "comms"],
}


def run_profile(profile: str, task: str) -> tuple[int, str]:
    """Bring the profile up for one task. Returns (status, text)."""
    p = (profile or "").strip().lower()
    if p not in AGENT_ALLOWLIST:
        return 403, f"agent '{profile}' is not on the allowlist"
    if not task or not task.strip():
        return 400, "task is required"
    try:
        # `hermes -p <profile> chat -q` == `<profile> chat -q`; the shim drops to
        # the hermes user and sets HERMES_HOME for the profile. --yolo for
        # unattended; the profile's own skills/toolsets/auth apply.
        r = subprocess.run(
            [HERMES_SHIM, "-p", p, "chat", "-q", task,
             "--max-turns", str(MAX_TURNS), "--yolo", "--quiet"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        return (200 if r.returncode == 0 else 500), out
    except subprocess.TimeoutExpired:
        return 504, f"agent '{p}' timed out after {TIMEOUT}s"
    except Exception as e:
        return 500, f"{type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("content-length", 0) or 0)
        if n > 256 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _authed(self) -> bool:
        """Bearer gate (#2). Fail closed: no configured token => refuse. Constant-time."""
        if not TOKEN:
            return False
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return False
        return hmac.compare_digest(got[7:].strip(), TOKEN)

    def do_GET(self):
        # /health stays open for liveness probes but leaks nothing (no roster).
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"status": "ok"})
        # Listing the served profiles reveals the roster — require auth.
        if self.path.rstrip("/") == "/v1/models":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"object": "list", "data": [
                {"id": a, "object": "model", "owned_by": "hermes"} for a in sorted(AGENT_ALLOWLIST)]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            data = self._body()
        except Exception as e:
            return self._send(400, {"error": f"bad request: {e}"})

        path = self.path.rstrip("/")
        # (a) the thin run endpoint
        if path == "/run":
            code, text = run_profile(data.get("profile", ""), data.get("task", ""))
            return self._send(code, {"output": text} if code == 200 else {"error": text})

        # (c) OpenAI-compatible: model=profile, last user message = task
        if path == "/v1/chat/completions":
            model = data.get("model", "")
            msgs = data.get("messages", []) or []
            task = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
            code, text = run_profile(model, task)
            if code != 200:
                return self._send(code, {"error": {"message": text, "type": "agent_error"}})
            return self._send(200, {
                "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
            })
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    bind = resolve_bind()  # fail-closed; raises SystemExit if no safe address
    gate = "bearer=on" if TOKEN else "bearer=OFF(fail-closed: all POSTs 401)"
    print(f"profile-serve on {bind}:{PORT} [{gate}] — agents: {sorted(AGENT_ALLOWLIST)}",
          flush=True)
    if not TOKEN:
        print("profile-serve: WARNING — PROFILE_SERVE_TOKEN unset; refusing every "
              "authenticated request until it is provided in hermes.env.", flush=True)
    ThreadingHTTPServer((bind, PORT), Handler).serve_forever()
