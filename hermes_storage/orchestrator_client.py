"""Agent → control-plane orchestrator client (mTLS)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _boot_tls_paths():
    from hermes_storage.bootstrap import get_bootstrap

    boot = get_bootstrap()
    if boot is None:
        return None, None, None
    url = getattr(boot, "orchestrator_url", None)
    if not url and isinstance(boot.extra, dict):
        orch = boot.extra.get("orchestrator")
        if isinstance(orch, dict):
            url = orch.get("url")
        url = url or boot.extra.get("orchestrator_url")
    url = url or __import__("os").environ.get("HERMES_ORCHESTRATOR_URL", "").strip() or None
    ca = boot.resolved_tls_ca()
    pem = boot.resolved_tls_cert_key()
    return url, ca, pem


def orchestrator_configured() -> bool:
    url, ca, pem = _boot_tls_paths()
    return bool(url and ca and pem and ca.is_file() and pem.is_file())


def _request(method: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
    from hermes_storage.mtls import client_ssl_context

    url, ca, pem = _boot_tls_paths()
    if not url or not ca or not pem:
        raise RuntimeError("orchestrator_url / tls certs not configured in bootstrap")
    base = url.rstrip("/")
    if not base.startswith("https://"):
        # Force HTTPS — plaintext orchestrator is not allowed
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        else:
            base = "https://" + base
    full = base + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(full, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = client_ssl_context(ca_crt=ca, client_pem=pem, check_hostname=False)
    try:
        with urlopen(req, context=ctx, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        try:
            return {"ok": False, "error": json.loads(err).get("error") or err, "status": exc.code}
        except Exception:
            return {"ok": False, "error": err or str(exc), "status": exc.code}
    except URLError as exc:
        # Typically SSLCertVerificationError / connection reset without cert
        raise RuntimeError(
            f"Orchestrator rejected the connection (mTLS required): {exc.reason}"
        ) from exc


def orch_cluster_status() -> dict[str, Any]:
    return _request("GET", "/cluster/status")


def orch_cluster_activate(target: str, *, reason: str = "agent") -> dict[str, Any]:
    return _request("POST", "/cluster/activate", {"target": target, "reason": reason})


def orch_heartbeat(node: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", "/cluster/heartbeat", node)
