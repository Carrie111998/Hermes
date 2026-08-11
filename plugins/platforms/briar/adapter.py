"""Briar headless peer platform adapter.

Connects to a ``briar-headless`` instance running in HTTP mode.
Outbound messages use the REST API.  Inbound messages arrive over the
WebSocket API as ``ConversationMessageReceivedEvent``.

Required:
  - ``briar-headless`` installed and running on ``BRIAR_API_URL``
  - ``BRIAR_API_URL``, ``BRIAR_CONTACT_ID``, and ``BRIAR_API_TOKEN`` set

Token discovery order:
  1. ``BRIAR_API_TOKEN`` environment variable
  2. ``~/.briar/auth_token`` file created by briar-headless on first run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIAR_DEFAULT_API_URL = "http://127.0.0.1:7000"
BRIAR_DEFAULT_CONTACT_ID = ""
BRIAR_DEFAULT_TOKEN = ""
BRIAR_HEALTH_PATH = "/v1/contacts"
BRIAR_WS_PATH = "/v1/ws"
BRIAR_MESSAGES_PATH_TEMPLATE = "/v1/messages/{contact_id}"
BRIAR_AUTH_TOKEN_PATH = "~/.briar/auth_token"

REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=20.0, write=15.0)
WS_CLOSE_TIMEOUT = 5.0
WS_BACKOFF_INITIAL = 1.0
WS_BACKOFF_MAX = 60.0
MAX_MESSAGE_LENGTH = 4000


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resolve_token() -> str:
    token = os.getenv("BRIAR_API_TOKEN", "").strip()
    if token:
        return token
    path = Path(BRIAR_AUTH_TOKEN_PATH).expanduser()
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


def _parse_comma_list(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _normalize_api_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    return value.rstrip("/")


def _current_os() -> str:
    s = sys.platform
    if s.startswith("linux"):
        return "linux"
    if s == "darwin":
        return "macos"
    if s == "win32":
        return "windows"
    return "unknown"


def _os_install_instructions() -> str:
    os_name = _current_os()
    if os_name == "linux":
        arch = "x86_64" if platform.machine().endswith("64") else platform.machine()
        jar = f"briar-headless-linux-{arch}.jar"
        return (
            "Linux install:\n"
            "  1. Install JRE 8+: sudo apt install default-jre\n"
            "  2. Build JAR from https://code.briarproject.org/briar/briar\n"
            "     ./gradlew --configure-on-demand briar-headless:x86LinuxJar\n"
            f"  3. Run: java -jar briar-headless/build/libs/{jar}\n"
            "     On first run it asks for nickname + password and starts on :7000"
        )
    if os_name == "macos":
        return (
            "macOS install:\n"
            "  1. Install JRE: brew install openjdk\n"
            "  2. Build JAR from https://code.briarproject.org/briar/briar\n"
            "     ./gradlew --configure-on-demand briar-headless:x86MacOsJar\n"
            "     or aarch64MacOsJar on Apple Silicon\n"
            "  3. Sign bundled Tor binaries, then run:\n"
            "     java -jar briar-headless/build/libs/briar-headless-macos-*.jar"
        )
    if os_name == "windows":
        return (
            "Windows install:\n"
            "  1. Install JRE 8+\n"
            "  2. Build JAR from https://code.briarproject.org/briar/briar\n"
            "     ./gradlew --configure-on-demand briar-headless:windowsJar\n"
            "  3. Run: java -jar briar-headless\\build\\libs\\briar-headless-windows.jar"
        )
    return (
        "Install briar-headless from https://code.briarproject.org/briar/briar "
        "and run it on this machine."
    )


async def _discover_local_briar_headless() -> Tuple[Optional[str], Optional[str], List[Dict[str, Any]]]:
    """Try to reach a local briar-headless and return (api_url, token, contacts)."""
    api_url = os.getenv("BRIAR_API_URL", BRIAR_DEFAULT_API_URL).rstrip("/")
    token_path = Path(BRIAR_AUTH_TOKEN_PATH).expanduser()
    token = ""
    if token_path.is_file():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if not token:
        env_token = os.getenv("BRIAR_API_TOKEN", "").strip()
        if env_token:
            token = env_token
    if not token:
        return None, None, []
    headers = {"Authorization": f"Bearer {token}"}
    contacts: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            resp = await client.get(f"{api_url}/v1/contacts")
            if resp.status_code == 200:
                contacts = resp.json()
                return api_url, token, contacts
    except Exception:
        pass
    return None, None, []


def check_requirements() -> bool:
    api_url = _normalize_api_url(os.getenv("BRIAR_API_URL", ""))
    contact_id = os.getenv("BRIAR_CONTACT_ID", "").strip()
    token = _resolve_token()
    return bool(api_url and contact_id and token)


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    api_url = _normalize_api_url(
        os.getenv("BRIAR_API_URL", extra.get("api_url", ""))
    )
    contact_id = os.getenv("BRIAR_CONTACT_ID", extra.get("contact_id", "")).strip()
    token = _resolve_token()
    return bool(api_url and contact_id and token)


def _env_enablement() -> Optional[Dict[str, Any]]:
    api_url = _normalize_api_url(os.getenv("BRIAR_API_URL", ""))
    contact_id = os.getenv("BRIAR_CONTACT_ID", "").strip()
    home = os.getenv("BRIAR_HOME_CHANNEL", "").strip()
    token = _resolve_token()
    if not (api_url and contact_id and token):
        return None
    seed: Dict[str, Any] = {"api_url": api_url, "contact_id": contact_id}
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Home"}
    return seed


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BriarAdapter(BasePlatformAdapter):
    """Briar headless peer adapter.

    Communicates with a local ``briar-headless`` REST/WebSocket server.
    """

    splits_long_messages = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("briar"))
        extra = config.extra or {}

        self.api_url = _normalize_api_url(
            os.getenv("BRIAR_API_URL", extra.get("api_url", BRIAR_DEFAULT_API_URL))
        )
        self.contact_id = os.getenv(
            "BRIAR_CONTACT_ID", extra.get("contact_id", BRIAR_DEFAULT_CONTACT_ID)
        ).strip()
        self.token = _resolve_token() or extra.get("api_token", "")
        self.allowed_users = _parse_comma_list(
            os.getenv("BRIAR_ALLOWED_USERS", "")
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws: Optional[httpx.WebSocket] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.api_url or not self.contact_id or not self.token:
            logger.error(
                "[briar] missing api_url/contact_id/token; "
                "set BRIAR_API_URL, BRIAR_CONTACT_ID, and BRIAR_API_TOKEN"
            )
            return False
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers=self._auth_headers(),
        )
        try:
            healthy = await self._health_check()
            if not healthy:
                await self._safe_disconnect()
                return False
        except Exception:
            await self._safe_disconnect()
            return False

        self._mark_connected()
        self._ws_task = asyncio.create_task(self._run_websocket())
        logger.info("[briar] connected to %s", self.api_url)
        return True

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        await self._safe_disconnect()
        self._mark_disconnected()
        logger.info("[briar] disconnected")

    async def _safe_disconnect(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.aclose()
            except Exception:
                pass
            self._ws = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def _health_check(self) -> bool:
        assert self._client is not None
        url = f"{self.api_url}{BRIAR_HEALTH_PATH}"
        try:
            res = await self._client.get(url)
            return res.status_code == 200
        except Exception as exc:
            logger.warning("[briar] health check failed: %s", exc)
            return False

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        assert self._client is not None
        url = f"{self.api_url}{path}"
        res = await self._client.get(url, params=params)
        res.raise_for_status()
        return res.json()

    async def _api_post(self, path: str, payload: Dict[str, Any]) -> Any:
        assert self._client is not None
        url = f"{self.api_url}{path}"
        res = await self._client.post(url, json=payload)
        res.raise_for_status()
        if res.status_code == 204:
            return None
        return res.json()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if not self._client or not content:
            return SendResult(success=False, error="not connected")
        chat = chat_id or self.contact_id
        url = BRIAR_MESSAGES_PATH_TEMPLATE.format(contact_id=chat)
        payload: Dict[str, Any] = {"text": content}
        try:
            data = await self._api_post(url, payload)
            return SendResult(success=True, message_id=str(data.get("id", "")))
        except httpx.HTTPStatusError as exc:
            text = exc.response.text if exc.response is not None else ""
            return SendResult(success=False, error=f"HTTP {exc.response.status_code}: {text}")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id):
        # Briar headless has no typing indicator in the documented REST API.
        return None

    async def get_chat_info(self, chat_id):
        return {
            "name": chat_id or self.contact_id,
            "type": "dm",
            "chat_id": chat_id or self.contact_id,
        }

    # ------------------------------------------------------------------
    # Inbound — WebSocket
    # ------------------------------------------------------------------

    async def _run_websocket(self):
        backoff = WS_BACKOFF_INITIAL
        url = f"{self.api_url}{BRIAR_WS_PATH}"
        while True:
            try:
                if self._client is None:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, WS_BACKOFF_MAX)
                    continue
                stream = self._client.stream("GET", url, headers=self._auth_headers())
                response = await stream.__aenter__()
                try:
                    if getattr(response, "status_code", 0) != 200:
                        logger.warning(
                            "[briar] websocket connect unexpected status %s",
                            getattr(response, "status_code", "?"),
                        )
                        backoff = min(backoff * 2, WS_BACKOFF_MAX)
                        await asyncio.sleep(backoff)
                        continue
                    self._ws = response
                    backoff = WS_BACKOFF_INITIAL
                    logger.info("[briar] websocket connected")
                    iterator = response.aiter_text()
                    while True:
                        try:
                            message = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        await self._handle_incoming_message(message)
                finally:
                    self._ws = None
                    await stream.__aexit__(None, None, None)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[briar] websocket error: %s", exc)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_BACKOFF_MAX)

    async def _handle_incoming_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("name") != "ConversationMessageReceivedEvent":
            return
        msg = data.get("data") or {}
        text = msg.get("text") or ""
        sender = str(msg.get("contactId", ""))
        local = msg.get("local", False)
        if not text or local:
            return
        if self.allowed_users and sender not in self.allowed_users:
            return
        event = MessageEvent(
            source=SessionSource(
                platform=Platform("briar"),
                chat_id=sender,
                chat_type="dm",
                user_id=sender,
                user_name=sender,
            ),
            text=text,
            message_type=MessageType.TEXT,
            raw_message=msg,
            timestamp=msg.get("timestamp"),
        )
        await self.dispatch(event)


def interactive_setup() -> None:
    """Interactive setup for Briar platform."""
    from hermes_cli.colors import Colors, color
    from hermes_cli.setup import (
        prompt,
        prompt_choice,
        prompt_yes_no,
        print_info,
        print_success,
        print_warning,
        save_env_value,
    )

    print()
    print(color("  ─── 🪐 Briar Setup ───", Colors.CYAN))
    print()
    print_info("  This configures Hermes to talk to briar-headless.")
    print_info("  briar-headless is a headless Briar peer that exposes a REST/WebSocket API.")
    print()

    api_url = os.getenv("BRIAR_API_URL", BRIAR_DEFAULT_API_URL).strip()
    contact_id = os.getenv("BRIAR_CONTACT_ID", "").strip()
    token = _resolve_token()

    if api_url and contact_id and token:
        print_success("  Briar is already configured.")
        if not prompt_yes_no("  Reconfigure Briar?", False):
            return

    discovered_api_url, discovered_token, contacts = (
        asyncio.get_event_loop().run_until_complete(
            _discover_local_briar_headless()
        )
    )

    if discovered_api_url and discovered_token:
        print_success("  Discovered a running briar-headless.")
        if prompt_yes_no("  Use auto-detected values?", True):
            api_url = discovered_api_url
            token = discovered_token
            save_env_value("BRIAR_API_URL", api_url)
            save_env_value("BRIAR_API_TOKEN", token)
            print_success("  Saved BRIAR_API_URL")
            print_success("  Saved BRIAR_API_TOKEN")
            if contacts:
                print_info(f"  Found {len(contacts)} contact(s).")
                contact_choices = [
                    f"{c.get('alias') or c.get('author', {}).get('name', '?')} "
                    f"(contactId={c.get('contactId')})"
                    for c in contacts
                ]
                contact_choices.append("Enter manually")
                choice_idx = prompt_choice(
                    "  Default Briar contact ID:",
                    contact_choices,
                    default=0,
                )
                if choice_idx < len(contacts):
                    contact_id = str(contacts[choice_idx].get("contactId", ""))
                else:
                    contact_id = prompt(
                        "  Default Briar contact ID",
                        default=contact_id,
                        password=False,
                    ).strip()
            else:
                contact_id = prompt(
                    "  Default Briar contact ID",
                    default=contact_id,
                    password=False,
                ).strip()
            if contact_id:
                save_env_value("BRIAR_CONTACT_ID", contact_id)
                print_success("  Saved BRIAR_CONTACT_ID")
            else:
                print_warning("  Skipped — Briar won't work without contact ID.")
                return
            print()
            print_success("🪐 Briar configured!")
            return
        print_info("  Falling back to manual setup...")

    print()
    print_info("  briar-headless not detected locally.")
    print_info("  Install/start it before using Briar in Hermes.")
    print()
    for line in _os_install_instructions().splitlines():
        print_info(f"  {line}")
    print()

    setup_choices = [
        "I have briar-headless running — enter values manually",
        "Exit setup (install briar-headless first)",
    ]
    choice_idx = prompt_choice(
        "  Choose an option:",
        setup_choices,
        default=0,
    )
    if choice_idx != 0:
        print_info("  Setup cancelled. Configure Briar later with 'hermes setup gateway'.")
        return

    api_url = prompt(
        "  briar-headless API URL",
        default=api_url or BRIAR_DEFAULT_API_URL,
        password=False,
    ).strip()
    contact_id = prompt(
        "  Default Briar contact ID",
        default=contact_id,
        password=False,
    ).strip()
    token = prompt(
        "  briar-headless bearer token",
        default=token,
        password=True,
    ).strip()

    if api_url:
        save_env_value("BRIAR_API_URL", api_url)
        print_success("  Saved BRIAR_API_URL")
    else:
        print_warning("  Skipped — Briar won't work without API URL.")
        return

    if contact_id:
        save_env_value("BRIAR_CONTACT_ID", contact_id)
        print_success("  Saved BRIAR_CONTACT_ID")
    else:
        print_warning("  Skipped — Briar won't work without contact ID.")
        return

    if token:
        save_env_value("BRIAR_API_TOKEN", token)
        print_success("  Saved BRIAR_API_TOKEN")
    else:
        print_warning("  Skipped — Briar won't work without bearer token.")
        return

    print()
    print_success("🪐 Briar configured!")


def register(ctx):
    ctx.register_platform(
        name="briar",
        label="Briar",
        adapter_factory=lambda cfg: BriarAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        setup_fn=interactive_setup,
        required_env=["BRIAR_API_URL", "BRIAR_CONTACT_ID", "BRIAR_API_TOKEN"],
        install_hint="Install briar-headless and set BRIAR_API_URL, BRIAR_CONTACT_ID, BRIAR_API_TOKEN",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BRIAR_HOME_CHANNEL",
        allowed_users_env="BRIAR_ALLOWED_USERS",
        allow_all_env="",
        max_message_length=MAX_MESSAGE_LENGTH,
        platform_hint="You are chatting via Briar. Keep replies concise.",
        emoji="🪐",
    )
