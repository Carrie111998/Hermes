"""Bale gateway adapter built on Hermes' Telegram-compatible transport.

Bale documents a Telegram-style Bot API, so the live gateway can reuse the
mature Telegram adapter while retaining a distinct platform identity and
independent ``BALE_*`` configuration. Telegram-only extensions that Bale does
not document are disabled here instead of being probed at runtime.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    check_telegram_requirements,
    telegram_deps_present,
)

BALE_API_BASE = "https://tapi.bale.ai/bot"
BALE_FILE_BASE = "https://tapi.bale.ai/file/bot"


def _get_bale_token(config: Any) -> str:
    """Return the configured Bale token without borrowing Telegram secrets."""
    token = getattr(config, "token", None)
    if token:
        return str(token).strip()

    try:
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            token = get_secret("BALE_BOT_TOKEN", "")
        except UnscopedSecretError:
            token = os.getenv("BALE_BOT_TOKEN", "")
    except ImportError:
        token = os.getenv("BALE_BOT_TOKEN", "")
    return str(token or "").strip()


def _bale_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Force Bale endpoints and disable undocumented Telegram extensions."""
    merged = dict(extra or {})
    merged.update(
        {
            "base_url": BALE_API_BASE,
            "base_file_url": BALE_FILE_BASE,
            "rich_messages": False,
            "rich_drafts": False,
            "disable_fallback_ips": True,
        }
    )
    return merged


class BaleAdapter(TelegramAdapter):
    """Run the Telegram-compatible gateway transport against Bale's API."""

    def __init__(self, config: PlatformConfig):
        """Initialize the shared transport with fixed Bale capabilities."""
        if not config.token:
            config.token = _get_bale_token(config) or None
        config.extra = _bale_extra(config.extra)
        super().__init__(config)
        self.platform = Platform("bale")

    def _reactions_enabled(self) -> bool:
        """Keep Telegram-only reaction lifecycle calls disabled for Bale."""
        return False

    async def _run_post_connect_housekeeping(self) -> None:
        """Skip Telegram command menus, status text, and private-chat topics."""
        return None

    def _webhook_url(self) -> str:
        """Keep Bale on polling even when Telegram has a webhook configured."""
        return ""

    def _proxy_url(self, _target_hosts: list[str]) -> str | None:
        """Use an explicit Bale proxy only; never inherit TELEGRAM_PROXY."""
        configured = self.config.extra.get("proxy_url")
        return str(configured).strip() if configured else None


def _build_adapter(config: PlatformConfig) -> BaleAdapter:
    """Construct the registered Bale gateway adapter."""
    return BaleAdapter(config)


def _is_connected(config: PlatformConfig) -> bool:
    """Report Bale as configured only when a Bale token is available."""
    return bool(_get_bale_token(config))


def _env_enablement() -> dict[str, Any] | None:
    """Seed an env-only Bale installation into the gateway configuration."""
    token = os.getenv("BALE_BOT_TOKEN", "").strip()
    if not token:
        return None

    seed: dict[str, Any] = {
        "base_url": BALE_API_BASE,
        "base_file_url": BALE_FILE_BASE,
    }
    home_channel = os.getenv("BALE_HOME_CHANNEL", "").strip()
    if home_channel:
        seed["home_channel"] = {
            "chat_id": home_channel,
            "name": os.getenv("BALE_HOME_CHANNEL_NAME", "").strip() or "Bale Home",
        }
    return seed


def _apply_yaml_config(
    _yaml_cfg: dict[str, Any], bale_cfg: dict[str, Any]
) -> dict[str, Any]:
    """Translate Bale YAML options into safe shared-transport extras."""
    config = bale_cfg if isinstance(bale_cfg, dict) else {}
    nested = config.get("extra")
    extras = dict(nested) if isinstance(nested, dict) else {}

    for key in ("disable_link_previews", "reply_to_mode"):
        if key in config:
            extras[key] = config[key]

    return _bale_extra(extras)


def _redact_token(text: str, token: str) -> str:
    """Remove the bot token from transport errors before returning them."""
    return text.replace(token, "<redacted>") if token else text


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Send a cron or notification message directly through Bale's Bot API."""
    if media_files or force_document:
        return {
            "error": "Bale standalone delivery does not support media attachments yet"
        }

    token = _get_bale_token(pconfig)
    if not token:
        return {"error": "BALE_BOT_TOKEN is not configured"}

    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": message}
    if thread_id:
        # Bale does not document Telegram forum topics, so the argument is
        # intentionally ignored instead of sending message_thread_id.
        pass

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{BALE_API_BASE}{token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return {"error": _redact_token(str(error), token)}

    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        return {"error": str(description or "Bale sendMessage failed")}

    result = data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return {"success": True, "message_id": str(message_id or "")}


def register(ctx: Any) -> None:
    """Register Bale as an independent Hermes gateway platform."""
    ctx.register_platform(
        name="bale",
        label="Bale (بله)",
        adapter_factory=_build_adapter,
        check_fn=telegram_deps_present,
        ensure_deps_fn=check_telegram_requirements,
        is_connected=_is_connected,
        required_env=["BALE_BOT_TOKEN"],
        install_hint="Run `hermes setup` to install messaging dependencies.",
        setup_fn=None,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="BALE_ALLOWED_USERS",
        allow_all_env="BALE_ALLOW_ALL_USERS",
        cron_deliver_env_var="BALE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4096,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are chatting through Bale, an Iranian messaging platform. "
            "Reply in Persian by default unless the user requests another language. "
            "Use concise mobile-friendly formatting and avoid Telegram-only features."
        ),
    )
