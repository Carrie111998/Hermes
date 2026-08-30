"""OpenCode permission bridge for the Discord gateway adapter.

Surfaces OpenCode's own permission requests (the agent asking before it
runs a tool) in a configured Discord channel and routes Accept/Reject
clicks back to OpenCode through its official server API.

Wire contract (verified against the official ``@opencode-ai/sdk`` types,
v1.18.25, ``packages/sdk/js/src/gen/types.gen.ts``):

- Events: ``GET {base_url}/event`` (SSE) delivers
  ``{"type": "permission.updated", "properties": Permission}`` where
  ``Permission = {id, type, pattern?, sessionID, messageID, callID?,
  title, metadata: {..}, time: {created}}``.  A reply is broadcast as
  ``permission.replied`` so every connected client (TUI included)
  observes the decision.
- Reply: ``POST {base_url}/session/{sessionID}/permissions/{permissionID}``
  with body ``{"response": "once" | "always" | "reject"}`` → 200 bool,
  400 bad request, 404 not found (already answered elsewhere).

Security posture (deliberate, non-negotiable):

- Off by default; enabled only via the discord platform ``extra`` key
  ``opencode_bridge`` (see :func:`parse_bridge_config`).
- Loopback-only ``base_url`` — a non-loopback target disables the bridge
  (fail-closed) instead of shipping permission metadata off-host.
- Only Discord users in the explicit ``allowed_user_ids`` allowlist may
  click; an empty allowlist disables the bridge entirely.
- Only ``"once"`` and ``"reject"`` replies are ever sent.  The ``"always"``
  response is intentionally unreachable from Discord — no persistent
  grants, no privilege escalation, no YOLO path.
- Timeout resolves to an explicit ``"reject"`` (fail-closed) so the
  OpenCode session never hangs on a missing reply.
- No secrets are read, stored, or transmitted; only event metadata the
  OpenCode server itself emits is displayed.

Out-of-repo counterpart: a small OpenCode plugin is only needed when the
TUI is not attached; any OpenCode client (TUI, IDE, this bridge) answers
through the same documented API above.  The bridge never writes OpenCode
configuration and never bypasses OpenCode's own permission engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard mirrors the adapter module
    import discord

    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover
    discord = None
    DISCORD_AVAILABLE = False


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

BRIDGE_CONFIG_KEY = "opencode_bridge"

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_TIMEOUT_SECONDS = 300
TIMEOUT_MIN_SECONDS = 30
TIMEOUT_MAX_SECONDS = 900
MAX_CONCURRENT_PROMPTS = 10
RESOLVED_MEMO_SIZE = 64

SSE_RECONNECT_MIN_SECONDS = 2.0
SSE_RECONNECT_MAX_SECONDS = 60.0

_TITLE_BUDGET = 400
_PATTERN_BUDGET = 200
_METADATA_BUDGET = 500

_TRUNCT_SUFFIX = "... [truncated]"


def _truncate(text: str, budget: int) -> str:
    text = str(text or "")
    if len(text) <= budget:
        return text
    return text[: max(0, budget - len(_TRUNCT_SUFFIX))] + _TRUNCT_SUFFIX


def is_loopback_url(url: str) -> bool:
    """Return True when the URL's host is a loopback address.

    The bridge refuses non-loopback targets so permission metadata can
    never be shipped to a remote host by misconfiguration.
    """
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


@dataclass(frozen=True)
class OpenCodeBridgeConfig:
    """Validated bridge configuration (from discord platform ``extra``).

    ``enabled`` is only True when every fail-closed precondition holds:
    explicit opt-in, a loopback base_url, a non-empty user allowlist, and
    a sane timeout.
    """

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    channel_id: str = ""
    allowed_user_ids: frozenset = frozenset()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    disabled_reason: str = ""


def parse_bridge_config(extra: Any) -> OpenCodeBridgeConfig:
    """Parse and validate the bridge config from a platform ``extra`` dict.

    Fail-closed: any invalid or missing precondition yields a disabled
    config with ``disabled_reason`` filled in (logged once by the caller).
    """
    section = extra.get(BRIDGE_CONFIG_KEY) if isinstance(extra, dict) else None
    if not isinstance(section, dict):
        return OpenCodeBridgeConfig(disabled_reason="not configured")

    if not section.get("enabled"):
        return OpenCodeBridgeConfig(disabled_reason="disabled")

    base_url = str(section.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    if not is_loopback_url(base_url):
        return OpenCodeBridgeConfig(
            base_url=base_url,
            disabled_reason=f"base_url is not loopback ({base_url})",
        )

    raw_users = section.get("allowed_user_ids") or []
    if isinstance(raw_users, str):
        raw_users = [p.strip() for p in raw_users.split(",")]
    allowed_user_ids = frozenset(
        str(u).strip() for u in raw_users if str(u).strip()
    )
    if not allowed_user_ids:
        return OpenCodeBridgeConfig(
            base_url=base_url,
            disabled_reason="allowed_user_ids is empty",
        )

    channel_id = str(section.get("channel_id") or "").strip()
    if not channel_id:
        return OpenCodeBridgeConfig(
            base_url=base_url,
            disabled_reason="channel_id is missing",
        )

    try:
        timeout_seconds = int(section.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = max(TIMEOUT_MIN_SECONDS, min(TIMEOUT_MAX_SECONDS, timeout_seconds))

    return OpenCodeBridgeConfig(
        enabled=True,
        base_url=base_url,
        channel_id=channel_id,
        allowed_user_ids=allowed_user_ids,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True)
class OpenCodePermissionRequest:
    """One OpenCode permission request, parsed from ``permission.updated``."""

    permission_id: str
    session_id: str
    kind: str = ""
    pattern: str = ""
    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def short_session_id(self) -> str:
        return self.session_id[:12]


def parse_permission_event(payload: Any) -> Optional[OpenCodePermissionRequest]:
    """Parse a ``permission.updated`` SSE payload into a request.

    Returns None for anything that is not a well-formed permission
    request — malformed events are dropped (fail-closed), never guessed
    into shape.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "permission.updated":
        return None
    props = payload.get("properties")
    if not isinstance(props, dict):
        return None
    permission_id = props.get("id")
    session_id = props.get("sessionID")
    if not isinstance(permission_id, str) or not permission_id:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    pattern = props.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        if isinstance(pattern, list):
            pattern = ", ".join(str(p) for p in pattern)
        else:
            pattern = json.dumps(pattern)
    metadata = props.get("metadata")
    return OpenCodePermissionRequest(
        permission_id=permission_id,
        session_id=session_id,
        kind=str(props.get("type") or ""),
        pattern=str(pattern) if pattern else "",
        title=str(props.get("title") or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


class SseAssembler:
    """Incremental SSE line parser yielding JSON ``data:`` payloads.

    Feed one line at a time (without trailing newline); a complete event
    (blank line) returns the concatenated data payload as parsed JSON,
    or None when the block was not JSON or carried no data.
    """

    def __init__(self) -> None:
        self._data_lines: List[str] = []

    def feed(self, line: str) -> Optional[dict]:
        line = line.rstrip("\r")
        if line == "":
            block = "\n".join(self._data_lines)
            self._data_lines = []
            if not block:
                return None
            try:
                payload = json.loads(block)
            except ValueError:
                logger.debug("OpenCode bridge: non-JSON SSE payload dropped")
                return None
            return payload if isinstance(payload, dict) else None
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip(" "))
        # event:/id:/retry:/comment lines are irrelevant: the JSON payload
        # itself carries the event type.
        return None


class BridgePendingRegistry:
    """Tracks in-flight bridge prompts; first resolution wins.

    Also memoizes recently resolved permission ids so a redelivered
    ``permission.updated`` (SSE reconnect replay) cannot post a second
    prompt for a request that was already answered.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_PROMPTS) -> None:
        self._max_concurrent = max_concurrent
        self._pending: Dict[str, str] = {}
        self._resolved_memo: List[str] = []
        self._resolved_set: Set[str] = set()

    def register(self, permission_id: str) -> bool:
        """Reserve a prompt slot; False when deduped or at capacity."""
        if permission_id in self._pending or permission_id in self._resolved_set:
            return False
        if len(self._pending) >= self._max_concurrent:
            logger.warning(
                "OpenCode bridge: %d prompts already pending, dropping %s",
                len(self._pending), permission_id,
            )
            return False
        self._pending[permission_id] = "pending"
        return True

    def resolve(self, permission_id: str, response: str) -> bool:
        """Record the first resolution; later calls are no-ops (False)."""
        if permission_id not in self._pending:
            return False
        del self._pending[permission_id]
        self._resolved_set.add(permission_id)
        self._resolved_memo.append(permission_id)
        if len(self._resolved_memo) > RESOLVED_MEMO_SIZE:
            dropped = self._resolved_memo.pop(0)
            self._resolved_set.discard(dropped)
        return True

    def is_pending(self, permission_id: str) -> bool:
        return permission_id in self._pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class OpenCodeBridgeClient:
    """HTTP client for the OpenCode server API (SSE events + replies).

    Loopback-only by construction: both endpoints refuse to run against
    a non-loopback base_url.
    """

    def __init__(self, base_url: str, *, transport: Optional[Any] = None) -> None:
        if not is_loopback_url(base_url):
            raise ValueError(f"OpenCode bridge refuses non-loopback base_url: {base_url}")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0, read=None),
            transport=transport,
        )

    async def stream_events(self) -> Any:
        """Yield parsed SSE payloads from ``GET /event``, reconnecting on drop.

        Async generator; exits on cancellation. Reconnects use a bounded
        linear backoff so a dead OpenCode server costs one attempt per
        minute, not a busy loop.
        """
        assembler = SseAssembler()
        backoff = SSE_RECONNECT_MIN_SECONDS
        while True:
            try:
                async with self._client.stream("GET", "/event") as response:
                    response.raise_for_status()
                    backoff = SSE_RECONNECT_MIN_SECONDS
                    async for line in response.aiter_lines():
                        payload = assembler.feed(line)
                        if payload is not None:
                            yield payload
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("OpenCode bridge: event stream dropped (%s)", exc)
            await asyncio.sleep(backoff)
            backoff = min(SSE_RECONNECT_MAX_SECONDS, backoff * 2)

    async def reply(
        self, session_id: str, permission_id: str, response: str
    ) -> tuple[bool, int]:
        """POST a permission reply. Returns (delivered, status_code).

        ``delivered`` is False when OpenCode answered 4xx (notably 404 —
        the request was already answered by another client, e.g. the TUI).
        """
        url = f"/session/{session_id}/permissions/{permission_id}"
        resp = await self._client.post(url, json={"response": response})
        if resp.status_code == 200:
            return True, 200
        logger.warning(
            "OpenCode bridge: reply %s/%s -> %s rejected (%s)",
            session_id, permission_id, response, resp.status_code,
        )
        return False, resp.status_code

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenCodeBridge:
    """Orchestrates event consumption, Discord prompts, and replies.

    Owns one asyncio task (see :meth:`run`) living inside the Discord
    adapter's event loop; per-request resolution happens either from a
    button click or the view's timeout, both funneled through
    :meth:`resolve` so first-wins semantics and the reply POST are in
    exactly one place.
    """

    def __init__(
        self,
        adapter: Any,
        config: OpenCodeBridgeConfig,
        client: Optional[OpenCodeBridgeClient] = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._client = client or OpenCodeBridgeClient(config.base_url)
        self._registry = BridgePendingRegistry()
        self._task: Optional[asyncio.Task] = None

    @property
    def config(self) -> OpenCodeBridgeConfig:
        return self._config

    @property
    def registry(self) -> BridgePendingRegistry:
        return self._registry

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def run(self) -> None:
        """Consume the event stream until cancelled."""
        logger.info(
            "OpenCode bridge: streaming %s into Discord channel %s",
            self._config.base_url, self._config.channel_id,
        )
        try:
            async for payload in self._client.stream_events():
                request = parse_permission_event(payload)
                if request is None:
                    continue
                try:
                    await self._post_prompt(request)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "OpenCode bridge: failed to post prompt for %s: %s",
                        request.permission_id, exc,
                    )
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Discord prompt
    # ------------------------------------------------------------------

    def _prompt_text(self, request: OpenCodePermissionRequest) -> tuple[str, str]:
        """Return (plain content, embed description) for the request."""
        title = _truncate(request.title or "(no title)", _TITLE_BUDGET)
        kind = request.kind or "permission"
        lines = [
            "🔐 **OpenCode Permission Request**",
            "",
            f"**Type:** `{_truncate(kind, 60)}`",
            f"**Request:**",
            f"```bash\n{title}\n```",
        ]
        if request.pattern:
            lines.append(f"**Pattern:** `{_truncate(request.pattern, _PATTERN_BUDGET)}`")
        lines.append(f"**Session:** `{request.short_session_id}`")
        lines.append("")
        lines.append("Answer **Accept** to allow this operation once, or **Reject** to deny it. Nothing is ever allowed persistently from Discord.")
        content = "\n".join(lines)

        embed_desc = f"```bash\n{title}\n```"
        return content, embed_desc

    async def _post_prompt(self, request: OpenCodePermissionRequest) -> None:
        if not self._registry.register(request.permission_id):
            return
        client = self._adapter._client
        if client is None:
            self._registry.resolve(request.permission_id, "drop")
            return
        channel = client.get_channel(int(self._config.channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(self._config.channel_id))
        if channel is None:
            self._registry.resolve(request.permission_id, "drop")
            logger.warning(
                "OpenCode bridge: channel %s not found, dropping %s",
                self._config.channel_id, request.permission_id,
            )
            return

        content, embed_desc = self._prompt_text(request)
        metadata_line = ""
        if request.metadata:
            metadata_line = _truncate(json.dumps(request.metadata, default=str), _METADATA_BUDGET)
        embed = discord.Embed(
            title="🔐 OpenCode Permission",
            description=embed_desc,
            color=discord.Color.gold(),
        )
        if metadata_line:
            embed.add_field(name="Details", value=f"```json\n{metadata_line}\n```", inline=False)

        view = _get_view_class()(
            bridge=self,
            request=request,
            allowed_user_ids=self._config.allowed_user_ids,
            timeout=self._config.timeout_seconds,
        )
        msg = await channel.send(content=content, embed=embed, view=view)
        view._message = msg

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve(
        self,
        request: OpenCodePermissionRequest,
        response: str,
        source: str,
    ) -> str:
        """Resolve a request and deliver the reply to OpenCode.

        ``response`` is ``"once"`` or ``"reject"``. Returns a short
        outcome label the caller renders in Discord. First-wins: a late
        click after another client (TUI) answered or the timeout fired
        never double-posts a reply.
        """
        if response not in ("once", "reject"):
            response = "reject"
        if not self._registry.resolve(request.permission_id, response):
            return "already-resolved"
        try:
            delivered, status = await self._client.reply(
                request.session_id, request.permission_id, response
            )
        except Exception as exc:
            logger.error(
                "OpenCode bridge: reply POST failed for %s: %s",
                request.permission_id, exc,
            )
            return "reply-failed"
        if not delivered:
            if status == 404:
                return "resolved-elsewhere"
            return "reply-failed"
        logger.info(
            "OpenCode bridge: %s answered %s (%s)", request.permission_id, response, source
        )
        return "delivered"


_VIEW_CLASS = None


def _get_view_class():
    """Build (once) the Accept/Reject view; requires discord.py."""
    global _VIEW_CLASS
    if _VIEW_CLASS is not None:
        return _VIEW_CLASS
    if not DISCORD_AVAILABLE:
        raise RuntimeError("discord.py is not installed")

    class OpenCodePermissionView(discord.ui.View):
        """Two-button Accept/Reject prompt for one OpenCode permission.

        Authorization is the bridge's explicit allowlist (fail-closed:
        nobody outside it can click). The timeout resolves to an explicit
        reject so the OpenCode session never hangs. There is deliberately
        no persistent-allow button.
        """

        def __init__(
            self,
            bridge: OpenCodeBridge,
            request: OpenCodePermissionRequest,
            allowed_user_ids: frozenset,
            timeout: int,
        ) -> None:
            super().__init__(timeout=timeout)
            self._bridge = bridge
            self._request = request
            self._allowed_user_ids = allowed_user_ids
            self._message = None
            self._finished = False

        def _authorized(self, interaction: discord.Interaction) -> bool:
            user = getattr(interaction, "user", None)
            uid = str(getattr(user, "id", "") or "")
            return bool(uid) and uid in self._allowed_user_ids

        async def _answer(self, interaction: discord.Interaction, response: str) -> None:
            if not self._authorized(interaction):
                await interaction.response.send_message(
                    "You're not allowed to answer OpenCode permission requests~",
                    ephemeral=True,
                )
                return
            outcome = await self._bridge.resolve(self._request, response, "discord")
            if outcome == "already-resolved":
                await interaction.response.send_message(
                    "This request was already resolved~", ephemeral=True
                )
                return
            await self._finalize(interaction, response, outcome)

        async def _finalize(
            self,
            interaction: discord.Interaction,
            response: str,
            outcome: str,
        ) -> None:
            self._finished = True
            for child in self.children:
                child.disabled = True
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                if response == "once":
                    embed.color = (
                        discord.Color.green()
                        if outcome == "delivered"
                        else discord.Color.dark_grey()
                    )
                else:
                    embed.color = (
                        discord.Color.red()
                        if outcome == "delivered"
                        else discord.Color.dark_grey()
                    )
                footer = {
                    "delivered": f"{'Accepted (once)' if response == 'once' else 'Rejected'}",
                    "resolved-elsewhere": "Already resolved by another OpenCode client",
                    "reply-failed": "Reply to OpenCode failed — answer in OpenCode directly",
                }.get(outcome, outcome)
                embed.set_footer(text=footer)
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                logger.debug("OpenCode bridge: could not edit prompt message")

        async def _annotate_only(self, response: str, outcome: str) -> None:
            """Edit the message without an interaction (timeout path)."""
            self._finished = True
            for child in self.children:
                child.disabled = True
            msg = self._message
            if msg is None:
                return
            embed = msg.embeds[0] if msg.embeds else None
            if embed:
                embed.color = discord.Color.dark_grey()
                embed.set_footer(text="⏱ Timed out — rejected (fail-closed)")
            try:
                await msg.edit(embed=embed, view=self)
            except Exception:
                pass  # message deleted or too old to edit

        @discord.ui.button(label="Accept (once)", style=discord.ButtonStyle.green)
        async def accept(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            await self._answer(interaction, "once")

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
        async def reject(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            await self._answer(interaction, "reject")

        async def on_timeout(self) -> None:
            if self._finished:
                return
            await self._bridge.resolve(self._request, "reject", "timeout")
            await self._annotate_only("reject", "timeout")

    _VIEW_CLASS = OpenCodePermissionView
    return _VIEW_CLASS
