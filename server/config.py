"""Configuration for the interfaze-agent product API.

Non-secret behavior is read from ``config.yaml`` under ``interfaze_server``.
Environment variables are reserved for deployment paths and credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hermes_constants import get_hermes_home


def _config_values() -> dict:
    path = get_hermes_home() / "config.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    values = data.get("interfaze_server") or {}
    return values if isinstance(values, dict) else {}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    database_url: str = ""
    auth_mode: str = "local"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://localhost:5173")
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    credential_key: str = ""
    # Absolute origin used to build opt-out links in outbound email. Must be
    # publicly reachable, so it cannot be inferred from the request host.
    public_base_url: str = "http://localhost:8000"
    upload_dir: Path = field(default_factory=lambda: get_hermes_home() / "interfaze" / "uploads")
    webui_enabled: bool = True
    max_upload_bytes: int = 25 * 1024 * 1024
    chat_enabled: bool = True
    chat_model: str = ""
    chat_toolset: str = "none"
    auth_max_attempts: int = 8
    auth_window_seconds: int = 300
    # One OAuth app per provider, shared by every tenant — the standard SaaS
    # shape. Tenants authorize against it; they never supply client secrets.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    microsoft_oauth_client_id: str = ""
    microsoft_oauth_client_secret: str = ""
    microsoft_oauth_tenant: str = "common"
    # Daily rhythm. The agent assembles a plan each morning and a report each
    # evening so the operator opens the app to a briefing rather than a control
    # panel (company-packs/silverline/business-rules.md:17-19). Off by default:
    # a background loop that writes tenant rows must be switched on knowingly.
    scheduler_enabled: bool = False
    digest_plan_hour: int = 8
    digest_report_hour: int = 18
    scheduler_interval_seconds: int = 300

    @classmethod
    def load(cls) -> "Settings":
        cfg = _config_values()
        home = get_hermes_home() / "interfaze"
        origins = cfg.get("cors_origins") or [
            "http://localhost:3000",
            "http://localhost:5173",
        ]
        return cls(
            database_path=Path(os.environ.get(
                "INTERFAZE_DATABASE_PATH",
                cfg.get("database_path") or home / "interfaze.db",
            )).expanduser(),
            database_url=os.environ.get("SUPABASE_DB_URL", ""),
            auth_mode=str(cfg.get("auth_mode") or "local").lower(),
            supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
            supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY", ""),
            supabase_service_role_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            cors_origins=tuple(str(origin) for origin in origins),
            bootstrap_admin_email=os.environ.get("INTERFAZE_BOOTSTRAP_ADMIN_EMAIL", ""),
            bootstrap_admin_password=os.environ.get("INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD", ""),
            credential_key=os.environ.get("INTERFAZE_CREDENTIAL_KEY", ""),
            public_base_url=str(
                os.environ.get("INTERFAZE_PUBLIC_BASE_URL")
                or cfg.get("public_base_url")
                or "http://localhost:8000"
            ).rstrip("/"),
            upload_dir=Path(cfg.get("upload_dir") or home / "uploads").expanduser(),
            webui_enabled=cfg.get("webui_enabled") is not False,
            max_upload_bytes=max(0, int(cfg.get("max_upload_bytes", 25 * 1024 * 1024))),
            chat_enabled=cfg.get("chat_enabled") is not False,
            chat_model=str(cfg.get("chat_model") or ""),
            chat_toolset=str(cfg.get("chat_toolset") or "none").lower(),
            auth_max_attempts=max(1, int(cfg.get("auth_max_attempts", 8))),
            auth_window_seconds=max(30, int(cfg.get("auth_window_seconds", 300))),
            google_oauth_client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            google_oauth_client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            microsoft_oauth_client_id=os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", ""),
            microsoft_oauth_client_secret=os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET", ""),
            microsoft_oauth_tenant=os.environ.get("MICROSOFT_OAUTH_TENANT", "common"),
            scheduler_enabled=bool(cfg.get("scheduler_enabled")),
            digest_plan_hour=min(23, max(0, int(cfg.get("digest_plan_hour", 8)))),
            digest_report_hour=min(23, max(0, int(cfg.get("digest_report_hour", 18)))),
            scheduler_interval_seconds=max(30, int(cfg.get("scheduler_interval_seconds", 300))),
        )
