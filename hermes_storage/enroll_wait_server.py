"""Minimal in-process enroll wait server (used when enroll_server.py path differs)."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from hermes_storage.enroll_flow import (
    issue_agent_bundle,
    load_pending,
    mark_used,
    normalize_code,
    pack_bundle_tar_gz,
    redeem_code,
)


def serve_until_code_used(*, code: str, port: int, ttl: int, cp: Path) -> bool:
    code = normalize_code(code)
    used = {"ok": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"[enroll] {fmt % args}")

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/health":
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/enroll":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            try:
                got = normalize_code(str(body.get("code") or ""))
                pending = redeem_code(got, machine_name="x", cp=cp)
                name = str(body.get("name") or pending.name or f"agent-{got.split('-')[0]}").strip()
                bundle = issue_agent_bundle(name=name, profile=pending.profile, cp=cp)
                mark_used(got, used_by=name, cp=cp)
                data = pack_bundle_tar_gz(bundle)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if got == code:
                    used["ok"] = True
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + ttl
    try:
        while time.time() < deadline:
            p = load_pending(code, cp)
            if (p and p.used) or used["ok"]:
                used["ok"] = True
                break
            time.sleep(0.4)
    finally:
        server.shutdown()
    return bool(used["ok"])
