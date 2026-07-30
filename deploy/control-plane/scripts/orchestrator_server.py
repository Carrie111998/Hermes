#!/usr/bin/env python3
"""Hermes orchestrator API — mTLS only.

Agents talk cluster/heartbeat/activate here. No client certificate signed by
the control-plane CA → TLS handshake fails (connection dropped).

Default listen: 0.0.0.0:8744 (HTTPS)

  GET  /health
  GET  /cluster/status
  POST /cluster/activate   JSON { "target": "...", "reason": "..." }
  POST /cluster/heartbeat  JSON { node fields... }
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_storage.mtls import peer_cert_cn, server_ssl_context  # noqa: E402

CONTROL = Path(os.environ.get("HERMES_CONTROL_DIR", str(_REPO / "deploy" / "control-plane"))).resolve()
PORT = int(os.environ.get("HERMES_ORCHESTRATOR_PORT", "8744"))
CERTS = CONTROL / "certs"
SERVER_PEM = Path(os.environ.get("HERMES_ORCH_SERVER_PEM", str(CERTS / "mongo-server.pem")))
CA_CRT = Path(os.environ.get("HERMES_ORCH_CA", str(CERTS / "ca.crt")))


def _get_storage():
    """Admin/SCRAM path for orchestrator process (has CA + app credentials)."""
    # Prefer env URI for the orchestrator daemon itself (SCRAM root/app user).
    uri = os.environ.get("HERMES_ORCH_MONGO_URI", "").strip()
    if not uri:
        creds = CERTS / "app-credentials.txt"
        hosts = os.environ.get("HERMES_MONGO_HOSTS", "localhost:27017").strip()
        replica = os.environ.get("HERMES_REPLICA_SET", "rs0")
        user = os.environ.get("HERMES_APP_USER", "hermesApp")
        password = os.environ.get("HERMES_APP_PASSWORD", "")
        if creds.is_file():
            for line in creds.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    if k.strip() == "HERMES_APP_USER":
                        user = v.strip()
                    elif k.strip() == "HERMES_APP_PASSWORD":
                        password = v.strip()
        if password:
            uri = (
                f"mongodb://{user}:{password}@{hosts}/"
                f"?replicaSet={replica}&authSource=admin"
            )
    if not uri:
        raise RuntimeError(
            "Set HERMES_ORCH_MONGO_URI or ensure certs/app-credentials.txt exists"
        )

    from pymongo import MongoClient
    from hermes_storage.mongo.stores import MongoClusterStore

    client = MongoClient(uri, serverSelectionTimeoutMS=8000, retryWrites=True)
    shared = client[os.environ.get("HERMES_SHARED_DB", "hermes_shared")]
    return client, MongoClusterStore(shared)


_CLIENT = None
_CLUSTER = None


def storage():
    global _CLIENT, _CLUSTER
    if _CLUSTER is None:
        _CLIENT, _CLUSTER = _get_storage()
    return _CLUSTER


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        cn = "?"
        try:
            cn = peer_cert_cn(self.connection.getpeercert()) or "?"
        except Exception:
            pass
        print(f"[orch] cn={cn} {self.address_string()} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _client_cn(self) -> str:
        try:
            return peer_cert_cn(self.connection.getpeercert()) or ""
        except Exception:
            return ""

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        cn = self._client_cn()
        if not cn:
            self._json(401, {"error": "client certificate required"})
            return
        if path == "/health":
            self._json(200, {"ok": True, "cn": cn})
            return
        if path == "/cluster/status":
            try:
                cluster = storage()
                self._json(200, {
                    "ok": True,
                    "caller_cn": cn,
                    "state": cluster.get_state(),
                    "nodes": cluster.list_nodes(),
                })
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        cn = self._client_cn()
        if not cn:
            self._json(401, {"error": "client certificate required"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        try:
            cluster = storage()
            if path == "/cluster/heartbeat":
                node = dict(body)
                node.setdefault("machine_id", cn)
                node.setdefault("cert_cn", cn)
                if "node_id" not in node:
                    self._json(400, {"error": "node_id required"})
                    return
                cluster.heartbeat(node)
                self._json(200, {"ok": True})
                return
            if path == "/cluster/activate":
                target = str(body.get("target") or "").strip()
                reason = str(body.get("reason") or f"orch:{cn}")
                if not target:
                    self._json(400, {"error": "target required"})
                    return
                # Resolve like HermesStorage.activate
                nodes = cluster.list_nodes(online_within_s=120.0)
                match = None
                needle = target.lower()
                for node in nodes:
                    for field in ("node_id", "machine_id", "hostname"):
                        val = str(node.get(field) or "").lower()
                        if val == needle or val.startswith(needle):
                            match = node
                            break
                    if match:
                        break
                if not match:
                    self._json(404, {"error": f"no node matched {target!r}"})
                    return
                state = cluster.set_active(match["node_id"], reason=reason)
                self._json(200, {"ok": True, "state": state, "activated_by_cn": cn})
                return
        except Exception as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    if not SERVER_PEM.is_file() or not CA_CRT.is_file():
        raise SystemExit(
            f"Missing TLS material:\n  server={SERVER_PEM}\n  ca={CA_CRT}\n"
            "Run scripts/install-control-plane.sh / gen-ca.sh first."
        )
    ctx = server_ssl_context(server_pem=SERVER_PEM, ca_crt=CA_CRT)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"[orch] mTLS orchestrator on 0.0.0.0:{PORT}")
    print(f"[orch] CA={CA_CRT}  server_pem={SERVER_PEM}")
    print("[orch] connections WITHOUT a valid agent client cert are dropped at handshake")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
