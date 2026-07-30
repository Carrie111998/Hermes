"""Load the local-only Mongo connection bootstrap.

On disk the agent keeps only this file (plus optional certs). Everything else
is remote.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from hermes_constants import get_hermes_home

_CACHE: Optional["BootstrapConfig"] = None
_PROFILE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass
class BootstrapConfig:
    """Minimal local config: how to reach Mongo and which profile to use.

    Auth modes:
      - ``x509`` — client certificate (preferred for multi-PC fleets)
      - ``scram`` / ``uri`` — credentials embedded in ``mongo_uri``
    """

    mongo_uri: str
    profile: str = "default"
    machine_id: Optional[str] = None
    shared_db: str = "hermes_shared"
    auth_mode: str = "uri"  # uri | scram | x509
    tls_ca_file: Optional[str] = None
    tls_cert_key_file: Optional[str] = None  # combined PEM (cert + key)
    tls_allow_invalid_hostnames: bool = False
    orchestrator_url: Optional[str] = None  # https://host:8744 — mTLS required
    source_path: Optional[Path] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def profile_slug(self) -> str:
        slug = _PROFILE_SLUG_RE.sub("_", (self.profile or "default").strip()).strip("_")
        return slug.lower() or "default"

    @property
    def profile_db(self) -> str:
        return f"hermes_profile_{self.profile_slug}"

    def resolved_tls_ca(self) -> Optional[Path]:
        return self._resolve_path(self.tls_ca_file)

    def resolved_tls_cert_key(self) -> Optional[Path]:
        return self._resolve_path(self.tls_cert_key_file)

    def _resolve_path(self, value: Optional[str]) -> Optional[Path]:
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute() and self.source_path is not None:
            path = (self.source_path.parent / path).resolve()
        return path


def bootstrap_path() -> Path:
    """Resolve bootstrap.yaml path.

    Order: ``HERMES_BOOTSTRAP`` env → ``{HERMES_HOME}/bootstrap.yaml``.
    """
    override = os.environ.get("HERMES_BOOTSTRAP", "").strip()
    if override:
        return Path(override)
    return get_hermes_home() / "bootstrap.yaml"


def load_bootstrap(*, force: bool = False) -> Optional[BootstrapConfig]:
    """Load bootstrap from env/file. Returns None when Mongo mode is off."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    uri = os.environ.get("HERMES_MONGO_URI", "").strip()
    profile = os.environ.get("HERMES_PROFILE", "").strip() or "default"
    machine_id = os.environ.get("HERMES_MACHINE_ID", "").strip() or None
    shared_db = os.environ.get("HERMES_SHARED_DB", "").strip() or "hermes_shared"
    auth_mode = os.environ.get("HERMES_MONGO_AUTH", "").strip() or "uri"
    tls_ca = os.environ.get("HERMES_MONGO_TLS_CA", "").strip() or None
    tls_cert = os.environ.get("HERMES_MONGO_TLS_CERT", "").strip() or None
    orch_url = os.environ.get("HERMES_ORCHESTRATOR_URL", "").strip() or None
    source: Optional[Path] = None
    extra: dict[str, Any] = {}
    allow_invalid = False

    path = bootstrap_path()
    if path.is_file():
        source = path
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            uri = str(raw.get("mongo_uri") or raw.get("uri") or uri).strip()
            profile = str(raw.get("profile") or profile).strip() or "default"
            mid = raw.get("machine_id")
            if mid:
                machine_id = str(mid).strip() or machine_id
            shared_db = str(raw.get("shared_db") or shared_db).strip() or "hermes_shared"
            auth_mode = str(raw.get("auth_mode") or auth_mode).strip() or "uri"
            tls = raw.get("tls") if isinstance(raw.get("tls"), dict) else {}
            tls_ca = str(tls.get("ca_file") or raw.get("tls_ca_file") or tls_ca or "").strip() or None
            tls_cert = str(
                tls.get("cert_key_file")
                or raw.get("tls_cert_key_file")
                or tls_cert
                or ""
            ).strip() or None
            allow_invalid = bool(
                tls.get("allow_invalid_hostnames")
                or raw.get("tls_allow_invalid_hostnames")
            )
            orch = raw.get("orchestrator") if isinstance(raw.get("orchestrator"), dict) else {}
            orch_url = str(
                orch.get("url") or raw.get("orchestrator_url") or orch_url or ""
            ).strip() or None
            # Auto-detect x509 when cert paths are present
            if auth_mode in ("uri", "") and tls_cert:
                auth_mode = "x509"
            reserved = {
                "mongo_uri", "uri", "profile", "machine_id", "shared_db",
                "auth_mode", "tls", "tls_ca_file", "tls_cert_key_file",
                "tls_allow_invalid_hostnames", "orchestrator", "orchestrator_url",
            }
            extra = {k: v for k, v in raw.items() if k not in reserved}

    if not uri:
        _CACHE = None
        return None

    _CACHE = BootstrapConfig(
        mongo_uri=uri,
        profile=profile,
        machine_id=machine_id,
        shared_db=shared_db,
        auth_mode=auth_mode.lower(),
        tls_ca_file=tls_ca,
        tls_cert_key_file=tls_cert,
        tls_allow_invalid_hostnames=allow_invalid,
        orchestrator_url=orch_url,
        source_path=source,
        extra=extra,
    )
    return _CACHE


def get_bootstrap() -> Optional[BootstrapConfig]:
    return load_bootstrap()


def is_mongo_mode() -> bool:
    return get_bootstrap() is not None


def reset_bootstrap_cache() -> None:
    global _CACHE
    _CACHE = None
