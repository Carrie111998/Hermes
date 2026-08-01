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

ALLOWLIST: only profiles in AGENT_ALLOWLIST can be brought up, and each carries
the kernel tool-groups it may call back into (enforced kernel-side via the
X-Agent header the run_agent tool sets). A profile not on the list is 403 — the
gate is closed by default, not open.

Runs INSIDE hermes-trainman-alpha on ai-shared (bind 0.0.0.0 is safe: the network
is internal/tailnet-gated, no host publish). Stdlib only — no pip in the image.
"""
from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The privilege-drop shim, NOT cli.py directly: it does s6-setuidgid to the
# hermes user (UID 1000) before exec'ing the venv binary. Calling cli.py raw runs
# as root and auth/permissions break (the "hermes is UID 1000" trap).
HERMES_SHIM = "/opt/hermes/bin/hermes"
PORT = int(os.environ.get("PROFILE_SERVE_PORT", "8642"))
MAX_TURNS = int(os.environ.get("PROFILE_SERVE_MAX_TURNS", "40"))
TIMEOUT = int(os.environ.get("PROFILE_SERVE_TIMEOUT", "540"))

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

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"status": "ok", "agents": sorted(AGENT_ALLOWLIST)})
        if self.path.rstrip("/") == "/v1/models":
            return self._send(200, {"object": "list", "data": [
                {"id": a, "object": "model", "owned_by": "hermes"} for a in sorted(AGENT_ALLOWLIST)]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
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
    print(f"profile-serve on :{PORT} — agents: {sorted(AGENT_ALLOWLIST)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
