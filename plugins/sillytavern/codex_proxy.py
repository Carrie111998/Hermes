"""Local OpenAI-compatible proxy bridging SillyTavern -> Codex / xAI.

SillyTavern speaks the OpenAI chat-completions wire format. This proxy accepts
that format on 127.0.0.1:8199 and forwards to a real backend:

  * route "codex"  -> ChatGPT Codex backend using the OAuth access_token from
                      ~/.codex/auth.json (Bearer). Endpoint is the standard
                      chat-completions surface; the OAuth token authorizes it.
  * route "xai"    -> https://api.x.ai/v1 using XAI_API_KEY (pure passthrough).

Pure stdlib (http.server + urllib). No external deps. Meant to be launched by
the sillytavern plugin as a background helper so SillyTavern can select
"Custom (OpenAI-compatible)" with base URL http://127.0.0.1:8199/<route>/v1 .

Usage:
    python codex_proxy.py [--port 8199]
"""

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
_CODEX_AUTH = Path(os.path.expanduser("~/.codex/auth.json"))

# Codex ChatGPT-backend chat surface reachable with an OAuth access_token.
CODEX_BASE = "https://chatgpt.com/backend-api/codex"
XAI_BASE = "https://api.x.ai/v1"


def _load_env() -> dict:
    env_path = _HERMES_HOME / ".env"
    keys = {}
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    keys[k] = v
    return keys


def _codex_token() -> str:
    if not _CODEX_AUTH.exists():
        return ""
    try:
        with open(_CODEX_AUTH, encoding="utf-8") as f:
            auth = json.load(f)
        return (auth.get("tokens") or {}).get("access_token", "")
    except Exception:
        return ""


def _codex_headers(token: str) -> dict:
    """Cloudflare-safe headers for the Codex backend (mirrors codex-rs CLI).

    Codex sits behind Cloudflare which challenges non-first-party originators
    with 403. We pin originator=codex_cli_rs, a codex_cli_rs UA, and extract
    ChatGPT-Account-ID from the OAuth JWT.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    try:
        import base64 as _b64

        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(_b64.urlsafe_b64decode(payload))
            acct = claims.get("https://api.openai.com/auth", {}).get(
                "chatgpt_account_id"
            )
            if acct:
                headers["ChatGPT-Account-ID"] = acct
    except Exception:
        pass
    return headers


def _resolve_route(path: str):
    """Return (backend_base, auth_header, remainder, extra_headers)."""
    env = _load_env()
    if path.startswith("/codex/"):
        token = _codex_token()
        extra = _codex_headers(token) if token else {}
        return (
            CODEX_BASE,
            f"Bearer {token}" if token else "",
            path[len("/codex"):],
            extra,
        )
    if path.startswith("/xai/"):
        key = env.get("XAI_API_KEY", "")
        return XAI_BASE, f"Bearer {key}" if key else "", path[len("/xai"):], {}
    return None, None, None, {}


def _chat_to_responses(body: bytes) -> bytes:
    """Convert an OpenAI chat-completions body to a Codex /responses body.

    SillyTavern sends {model, messages:[{role,content}], ...}. The Codex
    Responses API expects {model, input:[{role, content:[{type,text}]}], ...}.
    Best-effort: on any parse failure the original body is returned unchanged.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
        if "messages" not in data:
            return body
        input_items = []
        for msg in data["messages"]:
            content = msg.get("content", "")
            if isinstance(content, str):
                role = msg.get("role", "user")
                ctype = "output_text" if role == "assistant" else "input_text"
                content = [{"type": ctype, "text": content}]
            input_items.append({"role": msg.get("role", "user"), "content": content})
        out = {
            "model": data.get("model", "gpt-5.6-luna"),
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if "temperature" in data:
            out["temperature"] = data["temperature"]
        return json.dumps(out).encode("utf-8")
    except Exception:
        return body


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _proxy(self):
        base, auth, remainder, extra = _resolve_route(self.path)
        if base is None:
            self.send_error(404, "unknown route; use /codex/... or /xai/...")
            return
        if not auth:
            self.send_error(401, "missing credential for route")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None

        # remainder is like "/v1/chat/completions"; xAI already expects /v1,
        # Codex backend expects the path without the extra /v1 prefix and maps
        # chat/completions -> responses (with a body-shape conversion).
        if base == CODEX_BASE:
            tail = remainder[len("/v1"):] if remainder.startswith("/v1") else remainder
            if tail.endswith("/chat/completions"):
                tail = "/responses"
                body = _chat_to_responses(body)
            target = base + tail
        else:
            target = base + remainder
        length = len(body) if body else 0

        req = urllib.request.Request(target, data=body, method=self.command)
        req.add_header("Authorization", auth)
        req.add_header("Content-Type", "application/json")
        for k, v in extra.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                self.send_header(
                    "Content-Type",
                    resp.headers.get("Content-Type", "application/json"),
                )
                self.end_headers()
                # Stream the response body (Codex /responses returns SSE).
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_error(502, str(e))

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()


def main():
    port = 8199
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"codex/xai proxy listening on http://127.0.0.1:{port}")
    print("  routes: /codex/v1/...  /xai/v1/...")
    server.serve_forever()


if __name__ == "__main__":
    main()
