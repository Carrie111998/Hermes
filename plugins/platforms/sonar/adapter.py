"""Sonar platform adapter (Hermes plugin).

Streams encrypted DMs via ``sonar-cli listen`` and replies with ``sonar-cli send``.
Full Hermes gateway integration: same agent loop as Telegram (tools, memory, MCP).

Requires: official ``sonar-cli`` from https://github.com/hedwig-corp/bitchat-to-sonar

Config (config.yaml)::

    gateway:
      platforms:
        sonar:
          enabled: true
          extra:
            authorized_senders:
              - npub1...
            sonar_cli_home: ~/.sonar-agent
            display_name: "Hermes Agent · Sonar"
            max_chunk_chars: 3200

Environment (override yaml)::

    SONAR_CLI_HOME, SONAR_CLI_PATH, SONAR_ALLOWED_SENDERS (comma-separated),
    SONAR_ALLOW_ALL_USERS, SONAR_HOME_CHANNEL, SONAR_DISPLAY_NAME
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_DISPLAY_NAME = "Hermes Agent · Sonar"
DEFAULT_MAX_CHUNK = 3200
SEND_TIMEOUT_SECS = 120
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEDUP_MAX = 2000


def _expand(path: str) -> str:
    return os.path.expanduser(path) if path else path


def _find_sonar_cli(explicit: Optional[str] = None) -> str:
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("sonar-cli")
    if found:
        return found
    for candidate in ("/usr/local/bin/sonar-cli", os.path.expanduser("~/bin/sonar-cli")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "sonar-cli"


def _parse_sender_list(raw: str) -> List[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _sonar_env(home: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["SONAR_CLI_HOME"] = home
    return env


def _run_sonar_json(args: List[str], home: str, cli: str, timeout: float = 30.0) -> Dict[str, Any]:
    proc = __import__("subprocess").run(
        [cli, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_sonar_env(home),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "sonar-cli failed").strip()[:500])
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if not line:
        return {}
    return json.loads(line)


def _split_chunks(text: str, max_chunk: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chunk:
        return [text]
    parts: List[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chunk:
            parts.append(rest)
            break
        cut = rest.rfind("\n\n", 0, max_chunk)
        if cut < max_chunk // 3:
            cut = rest.rfind("\n", 0, max_chunk)
        if cut < max_chunk // 3:
            cut = max_chunk
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts


def check_requirements() -> bool:
    cli = _find_sonar_cli(os.getenv("SONAR_CLI_PATH", "").strip() or None)
    if not shutil.which(cli) and not os.path.isfile(cli):
        return False
    home = _expand(os.getenv("SONAR_CLI_HOME", "~/.sonar-agent"))
    return os.path.isdir(home) and os.path.isfile(os.path.join(home, "config.json"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    senders = extra.get("authorized_senders") or _parse_sender_list(os.getenv("SONAR_ALLOWED_SENDERS", ""))
    if os.getenv("SONAR_ALLOW_ALL_USERS", "").lower() in ("1", "true", "yes"):
        return check_requirements()
    return bool(senders) and check_requirements()


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    if not check_requirements():
        return None
    senders = _parse_sender_list(os.getenv("SONAR_ALLOWED_SENDERS", ""))
    if not senders and os.getenv("SONAR_ALLOW_ALL_USERS", "").lower() not in ("1", "true", "yes"):
        return None
    home = _expand(os.getenv("SONAR_CLI_HOME", "~/.sonar-agent"))
    seed: dict = {
        "sonar_cli_home": home,
        "authorized_senders": senders,
        "display_name": os.getenv("SONAR_DISPLAY_NAME", DEFAULT_DISPLAY_NAME).strip() or DEFAULT_DISPLAY_NAME,
    }
    cli = os.getenv("SONAR_CLI_PATH", "").strip()
    if cli:
        seed["sonar_cli_path"] = cli
    try:
        max_chunk = int(os.getenv("SONAR_MAX_CHUNK_CHARS", str(DEFAULT_MAX_CHUNK)))
        seed["max_chunk_chars"] = max_chunk
    except ValueError:
        pass
    home_npub = os.getenv("SONAR_HOME_CHANNEL", "").strip()
    if home_npub:
        seed["home_channel"] = {
            "chat_id": home_npub,
            "name": home_npub[:16] + "…",
        }
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg) -> Optional[dict]:
    """Merge gateway.platforms.sonar from config.yaml into platform_cfg.extra."""
    gw = yaml_cfg.get("gateway") or {}
    platforms = gw.get("platforms") or {}
    sonar = platforms.get("sonar") or {}
    if not sonar:
        return None
    extra = dict(getattr(platform_cfg, "extra", None) or {})
    sonar_extra = sonar.get("extra") or {}
    extra.update(sonar_extra)
    if sonar.get("enabled") is True:
        platform_cfg.enabled = True
    return extra


class SonarAdapter(BasePlatformAdapter):
    """NIP-44 DM adapter using sonar-cli subprocess transport."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("sonar"))
        extra = getattr(config, "extra", {}) or {}

        self.sonar_home = _expand(
            os.getenv("SONAR_CLI_HOME") or extra.get("sonar_cli_home") or "~/.sonar-agent"
        )
        self.sonar_cli = _find_sonar_cli(
            os.getenv("SONAR_CLI_PATH", "").strip() or extra.get("sonar_cli_path")
        )
        self.display_name = (
            os.getenv("SONAR_DISPLAY_NAME") or extra.get("display_name") or DEFAULT_DISPLAY_NAME
        ).strip() or DEFAULT_DISPLAY_NAME

        env_senders = _parse_sender_list(os.getenv("SONAR_ALLOWED_SENDERS", ""))
        yaml_senders = extra.get("authorized_senders") or []
        self.authorized_senders: Set[str] = set(env_senders or yaml_senders)
        self.allow_all = os.getenv("SONAR_ALLOW_ALL_USERS", "").lower() in ("1", "true", "yes") or bool(
            extra.get("allow_all_users")
        )

        try:
            self.max_chunk_chars = int(
                os.getenv("SONAR_MAX_CHUNK_CHARS") or extra.get("max_chunk_chars") or DEFAULT_MAX_CHUNK
            )
        except (TypeError, ValueError):
            self.max_chunk_chars = DEFAULT_MAX_CHUNK

        self._listen_proc: Optional[asyncio.subprocess.Process] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._running = False
        self._seen_ids: Dict[str, float] = {}
        self._agent_npub: Optional[str] = None

        max_msg = extra.get("max_message_length")
        if max_msg is None:
            try:
                from gateway.platform_registry import platform_registry

                entry = platform_registry.get("sonar")
                if entry and entry.max_message_length:
                    max_msg = entry.max_message_length
            except Exception:
                pass
        self.max_message_length = int(max_msg or self.max_chunk_chars)

    @property
    def name(self) -> str:
        return "Sonar"

    def _is_authorized_sender(self, sender: str) -> bool:
        if self.allow_all:
            return True
        return sender in self.authorized_senders

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.allow_all and not self.authorized_senders:
            self._set_fatal_error(
                "config_missing",
                "Set SONAR_ALLOWED_SENDERS or gateway.platforms.sonar.extra.authorized_senders",
                retryable=False,
            )
            return False
        if not check_requirements():
            self._set_fatal_error(
                "sonar_cli_missing",
                "sonar-cli not found or SONAR_CLI_HOME not initialized (run sonar-cli init)",
                retryable=False,
            )
            return False

        try:
            ident = await asyncio.to_thread(
                _run_sonar_json, ["identity"], self.sonar_home, self.sonar_cli, 15.0
            )
            self._agent_npub = ident.get("npub")
        except Exception as e:
            logger.error("[Sonar] identity check failed: %s", e)
            self._set_fatal_error("identity_failed", str(e), retryable=True)
            return False

        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._mark_connected()
        logger.info("[Sonar] listening as %s", self._agent_npub or "?")
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._stop_listen_proc()
        logger.info("[Sonar] disconnected")

    async def _stop_listen_proc(self) -> None:
        proc = self._listen_proc
        self._listen_proc = None
        if not proc:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def _listen_loop(self) -> None:
        backoff_idx = 0
        while self._running:
            started = time.monotonic()
            try:
                await self._run_one_listen_session()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[Sonar] listen session error: %s", e)
            if not self._running:
                return
            if time.monotonic() - started >= 60:
                backoff_idx = 0
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[Sonar] reconnecting listen in %ss", delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def _run_one_listen_session(self) -> None:
        await self._stop_listen_proc()
        self._listen_proc = await asyncio.create_subprocess_exec(
            self.sonar_cli,
            "listen",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_sonar_env(self.sonar_home),
        )
        proc = self._listen_proc
        assert proc.stdout is not None
        while self._running and proc.returncode is None:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=120.0)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "message":
                continue
            await self._on_inbound(event)
        await self._stop_listen_proc()

    def _dedupe(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_ids) > DEDUP_MAX:
            cutoff = now - 86400
            self._seen_ids = {k: v for k, v in self._seen_ids.items() if v > cutoff}
        if msg_id in self._seen_ids:
            return True
        self._seen_ids[msg_id] = now
        return False

    async def _on_inbound(self, event: Dict[str, Any]) -> None:
        sender = event.get("sender") or ""
        content = (event.get("content") or "").strip()
        msg_id = event.get("id") or uuid.uuid4().hex
        if event.get("mine") or not sender or not content:
            return
        if not self._is_authorized_sender(sender):
            logger.debug("[Sonar] ignored unauthorized sender %s…", sender[:12])
            return
        if self._dedupe(msg_id):
            return

        source = self.build_source(
            chat_id=sender,
            chat_name=sender[:16] + "…",
            chat_type="dm",
            user_id=sender,
            user_name=sender[:16] + "…",
        )
        message_event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=event,
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self.handle_message(message_event)

    def _brand_reply(self, text: str) -> str:
        stripped = text.strip()
        signature = f"— {self.display_name}"
        if re.search(r"hermes agent", stripped, re.I) and "sonar" in stripped.lower():
            return stripped
        if not stripped.rstrip().endswith(self.display_name):
            stripped = f"{stripped.rstrip()}\n\n{signature}"
        return stripped

    async def _sonar_send(self, to_npub: str, text: str) -> Dict[str, Any]:
        def _sync_send():
            proc = __import__("subprocess").run(
                [self.sonar_cli, "send", "--to", to_npub, "--text", text],
                capture_output=True,
                text=True,
                timeout=SEND_TIMEOUT_SECS,
                env=_sonar_env(self.sonar_home),
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "send failed")[:500])
            line = (proc.stdout or "").strip().splitlines()[-1]
            return json.loads(line) if line else {"type": "sent"}

        return await asyncio.to_thread(_sync_send)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        peer = chat_id
        if not peer:
            return SendResult(success=False, error="missing chat_id (npub)")
        body = self._brand_reply(self.format_message(content))
        chunks = _split_chunks(body, self.max_chunk_chars)
        if not chunks:
            return SendResult(success=False, error="empty message")
        last_id = None
        try:
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    prefix = f"[{i + 1}/{len(chunks)}]\n"
                    room = self.max_chunk_chars - len(prefix)
                    chunk = prefix + (chunk[:room] if len(prefix) + len(chunk) > self.max_chunk_chars else chunk)
                result = await self._sonar_send(peer, chunk)
                last_id = result.get("group_id") or result.get("id") or str(int(time.time() * 1000))
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.4)
            return SendResult(success=True, message_id=last_id)
        except Exception as e:
            logger.error("[Sonar] send failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self._sonar_send(chat_id, f"⏳ {self.display_name} is thinking…")
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "chat_id": chat_id,
            "name": chat_id[:20] + "…" if len(chat_id) > 20 else chat_id,
            "type": "dm",
        }


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    extra = getattr(pconfig, "extra", {}) or {}
    home = _expand(os.getenv("SONAR_CLI_HOME") or extra.get("sonar_cli_home") or "~/.sonar-agent")
    cli = _find_sonar_cli(os.getenv("SONAR_CLI_PATH", "").strip() or extra.get("sonar_cli_path"))
    peer = chat_id or os.getenv("SONAR_HOME_CHANNEL", "").strip()
    if not peer:
        return {"error": "sonar standalone send: chat_id or SONAR_HOME_CHANNEL required"}
    try:
        max_chunk = int(os.getenv("SONAR_MAX_CHUNK_CHARS") or extra.get("max_chunk_chars") or DEFAULT_MAX_CHUNK)
    except (TypeError, ValueError):
        max_chunk = DEFAULT_MAX_CHUNK
    chunks = _split_chunks(message, max_chunk)
    last = None
    for chunk in chunks:
        result = await asyncio.to_thread(
            lambda c=chunk: _run_sonar_json(
                ["send", "--to", peer, "--text", c], home, cli, SEND_TIMEOUT_SECS
            )
        )
        last = result.get("group_id") or result.get("id")
    return {"success": True, "platform": "sonar", "chat_id": peer, "message_id": last or uuid.uuid4().hex[:12]}


def register(ctx) -> None:
    ctx.register_platform(
        name="sonar",
        label="Sonar",
        adapter_factory=lambda cfg: SonarAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="Install sonar-cli from https://github.com/hedwig-corp/bitchat-to-sonar",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="SONAR_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SONAR_ALLOWED_SENDERS",
        allow_all_env="SONAR_ALLOW_ALL_USERS",
        max_message_length=DEFAULT_MAX_CHUNK,
        emoji="📡",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are on Sonar encrypted direct messages (NIP-44 / Marmot). "
            "Use plain text only — no markdown tables, headers, or **bold** syntax. "
            "Keep replies concise; long answers are split across multiple DMs. "
            "You have the same tools, memory, and MCP servers as other Hermes channels."
        ),
    )