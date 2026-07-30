#!/usr/bin/env python3
"""Enroll HTTP API for Hermes control plane (one-time codes + legacy bearer).

POST /enroll
  Body JSON:
    { "code": "ABCD-EFGH", "name": "optional-override" }
  or legacy:
    Authorization: Bearer <HERMES_ENROLL_TOKEN>
    { "name": "home-pc", "profile": "default" }

GET /health → ok
GET /pending/<code> → {valid, expires_in, used}  (no secrets)
"""

from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Allow importing hermes_storage when run from deploy/control-plane
import sys

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_storage.enroll_flow import (  # noqa: E402
    control_plane_dir,
    issue_agent_bundle,
    load_pending,
    mark_used,
    normalize_code,
    pack_bundle_tar_gz,
    redeem_code,
)

CONTROL = Path(os.environ.get("HERMES_CONTROL_DIR", str(control_plane_dir()))).resolve()
TOKEN = os.environ.get("HERMES_ENROLL_TOKEN", "").strip()
PORT = int(os.environ.get("HERMES_ENROLL_PORT", "8743"))
HOSTS = os.environ.get("HERMES_MONGO_HOSTS", "").strip()
REPLICA = os.environ.get("HERMES_REPLICA_SET", "rs0").strip() or "rs0"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(raw: str) -> str:
    name = _SAFE_NAME.sub("-", (raw or "").strip()).strip("-")
    return name or "agent"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[enroll] {self.address_string()} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, code: int, data: bytes, *, filename: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"ok": True})
            return
        if parsed.path.startswith("/pending/"):
            code = normalize_code(parsed.path.split("/pending/", 1)[-1])
            pending = load_pending(code, CONTROL)
            if not pending:
                self._json(404, {"valid": False, "error": "unknown"})
                return
            self._json(200, {
                "valid": pending.is_valid(),
                "used": pending.used,
                "expired": pending.is_expired(),
                "expires_in": max(0, int(pending.expires_at - time.time())),
                "profile": pending.profile,
                "name": pending.name,
            })
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
            self._json(400, {"error": "invalid JSON"})
            return

        code = normalize_code(str(body.get("code") or ""))
        auth = self.headers.get("Authorization", "")
        bearer_ok = bool(TOKEN) and auth == f"Bearer {TOKEN}"

        try:
            if code:
                pending = redeem_code(code, machine_name="pending", cp=CONTROL)
                name = _safe_name(
                    str(body.get("name") or pending.name or "").strip()
                    or f"agent-{code.split('-')[0].lower()}"
                )
                profile = pending.profile
                bundle = issue_agent_bundle(
                    name=name,
                    profile=profile,
                    cp=CONTROL,
                    hosts=HOSTS or None,
                    replica_set=REPLICA,
                )
                mark_used(code, used_by=name, cp=CONTROL)
                data = pack_bundle_tar_gz(bundle)
                self._bytes(200, data, filename=f"{name}.tar.gz")
                print(f"[enroll] redeemed code {code} → {name}")
                return

            if bearer_ok:
                name = _safe_name(str(body.get("name") or ""))
                profile = str(body.get("profile") or "default").strip() or "default"
                if not name:
                    self._json(400, {"error": "name required"})
                    return
                bundle = issue_agent_bundle(
                    name=name,
                    profile=profile,
                    cp=CONTROL,
                    hosts=HOSTS or None,
                    replica_set=REPLICA,
                )
                data = pack_bundle_tar_gz(bundle)
                self._bytes(200, data, filename=f"{name}.tar.gz")
                return

            self._json(401, {"error": "code or bearer token required"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            print(f"[enroll] error: {exc}")
            self._json(500, {"error": str(exc)})


def main() -> None:
    os.environ.setdefault("HERMES_CONTROL_DIR", str(CONTROL))
    print(f"[enroll] control dir: {CONTROL}")
    print(f"[enroll] listening on 0.0.0.0:{PORT} (one-time codes + optional bearer)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
