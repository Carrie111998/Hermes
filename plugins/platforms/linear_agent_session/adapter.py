"""Linear Agent Session platform adapter (Hermes plugin).

Receives Linear's ``AgentSessionEvent`` webhook and turns each Linear Agent
Session into a persistent Hermes chat session, so the agent shows live
progress in the Linear issue thread and can be steered mid-run — the same
UX Cursor/Devin-style delegates provide.

Why a stable per-session chat_id matters
-----------------------------------------
The generic ``webhook`` platform (``gateway/platforms/webhook.py``) keys
each POST to its own delivery_id, so every request gets a fresh, one-shot
agent turn. That is wrong for Linear Agent Sessions: a ``prompted`` event
(the user replying in the thread mid-run) must resume the SAME agent turn
that is already working the issue, not spawn a second one. Hermes already
resolves "same chat_id -> same/resumed session" for every other platform
(Telegram, WhatsApp, ...); this adapter gets that behavior for free by
keying ``chat_id`` on Linear's own ``agentSession.id``, which is stable for
the life of the delegation.

Division of labor
------------------
This adapter is transport only:
  - Linear event in -> Hermes MessageEvent (agent turn) out.
  - Agent output out -> Linear ``agentActivityCreate`` in.
It does NOT reimplement Paperclip issue creation/claiming/labelling itself.
The dispatched agent turn already has Paperclip + Linear MCP tools
available in this fleet, so resolving/creating/claiming the linked
Paperclip issue happens as part of the agent's own first turn — the same
way any other tool-using request works. This keeps business logic in one
place (the agent, following AGENT-SURFACE-ALIGNMENT.md) instead of forking
it into adapter code that would drift from it.

Timing contract (Linear Developer Preview, verified 2026-07-25)
------------------------------------------------------------------
  - Webhook receiver must ACK within 5s.
  - First ``agentActivityCreate`` must land within 10s of ``created`` or
    Linear marks the session unresponsive.
To make the 10s deadline deterministic regardless of how long the agent
turn takes to produce its first real output, this adapter synchronously
posts a "Claimed, starting work" thought activity itself, before handing
off to the agent.

Configuration in config.yaml::

    platforms:
      linear_agent_session:
        enabled: true
        extra:
          host: "127.0.0.1"   # default; loopback only unless proxied
          port: 8645

Environment variables:
    LINEAR_AGENTCORE_TOKEN   Linear OAuth app token (existing bridge identity)
    LINEAR_WEBHOOK_SECRET    Shared secret for Linear-Signature verification
    LINEAR_AGENT_SESSION_HOST / _PORT   Optional bind overrides
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8645
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
MAX_BODY_BYTES = 1_048_576  # 1MB, matches the generic webhook adapter's cap
IDEMPOTENCY_TTL_SECONDS = 3600


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe compare, tolerant of non-ASCII (attacker-controlled) input.

    Mirrors ``gateway/platforms/webhook.py::_hmac_str_equal`` — a raw
    ``hmac.compare_digest`` call raises ``TypeError`` on non-ASCII ``str``
    input, which would otherwise turn a hostile header into a 500 instead
    of a clean 401.
    """
    return hmac.compare_digest(provided.encode(), expected.encode())


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def validate_config(config: PlatformConfig) -> bool:
    return bool(os.getenv("LINEAR_AGENTCORE_TOKEN")) and bool(
        os.getenv("LINEAR_WEBHOOK_SECRET")
    )


def is_connected(adapter) -> bool:
    return bool(getattr(adapter, "_runner", None) is not None)


class LinearAgentSessionAdapter(BasePlatformAdapter):
    """Webhook-driven adapter mapping Linear Agent Sessions to Hermes sessions."""

    # No human is present to answer a "session restored — what next?"
    # prompt: these turns are event-triggered, same reasoning as the
    # generic webhook adapter (gateway/platforms/webhook.py).
    interactive_resume: bool = False

    # Auth is Linear-Signature HMAC on the webhook POST itself (see
    # _handle_webhook). Synthetic user_ids look like
    # "linear-agent-session:<agentSession.id>" and change per delegation, so a
    # human allowlist cannot cover them. Mirror the built-in WEBHOOK platform
    # and Relay: once the signature verifies, the event is authorized.
    # Without this, gateway authz default-denies every session
    # ("Unauthorized user: linear-agent-session:…") and the agent never runs
    # (AUR-1757 E2E, 2026-07-29).
    @property
    def authorization_is_upstream(self) -> bool:
        return True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("linear_agent_session"))
        self._host: str = (
            config.extra.get("host")
            or os.getenv("LINEAR_AGENT_SESSION_HOST")
            or DEFAULT_HOST
        )
        self._port: int = int(
            config.extra.get("port")
            or os.getenv("LINEAR_AGENT_SESSION_PORT")
            or DEFAULT_PORT
        )
        self._webhook_secret: str = os.getenv("LINEAR_WEBHOOK_SECRET", "")
        self._api_token: str = os.getenv("LINEAR_AGENTCORE_TOKEN", "")
        self._runner = None
        self.gateway_runner = None

        # chat_id ("linear-agent-session:<agentSession.id>") -> agentSession.id.
        # Trivial today (chat_id already encodes it) but kept as an explicit
        # map so send() doesn't need to re-parse the chat_id string, and so a
        # future change to the chat_id scheme doesn't have to touch send().
        self._session_ids: Dict[str, str] = {}

        # Idempotency: Linear retries webhooks on non-2xx / timeout. Dedup on
        # the AgentSessionEvent's own id (not agentSession.id, which repeats
        # across created/prompted).
        self._seen_deliveries: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._webhook_secret:
            raise ValueError(
                "[linear_agent_session] LINEAR_WEBHOOK_SECRET is not set. "
                "Refusing to start an unauthenticated webhook receiver — "
                "set the secret to the same value entered in Linear "
                "Settings -> API -> Webhooks."
            )
        if not self._api_token:
            raise ValueError(
                "[linear_agent_session] LINEAR_AGENTCORE_TOKEN is not set."
            )

        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/linear-agent-session/webhook", self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error(
                "[linear_agent_session] Could not bind %s:%d: %s",
                self._host,
                self._port,
                exc,
            )
            return False

        self._mark_connected()
        logger.info(
            "[linear_agent_session] Listening on %s:%d — POST /linear-agent-session/webhook. "
            "Register this URL (behind your reverse proxy / tunnel) in Linear "
            "Settings -> API -> Webhooks, category 'Agent session events'.",
            self._host,
            self._port,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[linear_agent_session] Disconnected")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "linear_agent_session"}

    # ------------------------------------------------------------------
    # Outbound: agent output -> Linear agentActivityCreate
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Post the agent's output into the Linear Agent Session as an activity.

        ``metadata.activity_type`` selects the Linear activity type
        (thought | action | response | elicitation | error). Defaults to
        "response" — the common case of the agent's final answer for a turn.
        Interim status/progress lines should be sent with
        ``metadata={"activity_type": "thought"}`` by whatever call site wants
        that distinction; absent that, everything still lands in the thread.
        """
        agent_session_id = self._session_ids.get(chat_id)
        if not agent_session_id:
            logger.warning(
                "[linear_agent_session] No agentSession.id for chat_id=%s; "
                "dropping activity (session was never registered via a "
                "created/prompted webhook)",
                chat_id,
            )
            return SendResult(success=False, error="Unknown agent session")

        activity_type = "response"
        if metadata and metadata.get("activity_type"):
            activity_type = metadata["activity_type"]

        ok = await self._create_activity(
            agent_session_id, activity_type, content
        )
        return SendResult(success=ok)

    async def _create_activity(
        self, agent_session_id: str, activity_type: str, body: str
    ) -> bool:
        mutation = """
        mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
          agentActivityCreate(input: $input) {
            success
          }
        }
        """
        variables = {
            "input": {
                "agentSessionId": agent_session_id,
                "content": {"type": activity_type, "body": body[:60000]},
            }
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    LINEAR_GRAPHQL_URL,
                    json={"query": mutation, "variables": variables},
                    headers={
                        "Authorization": self._api_token,
                        "Content-Type": "application/json",
                    },
                )
            data = resp.json()
            if resp.status_code >= 300 or data.get("errors"):
                logger.error(
                    "[linear_agent_session] agentActivityCreate failed: "
                    "status=%s body=%s",
                    resp.status_code,
                    str(data)[:500],
                )
                return False
            return bool(
                data.get("data", {})
                .get("agentActivityCreate", {})
                .get("success")
            )
        except Exception as e:
            logger.error(
                "[linear_agent_session] agentActivityCreate exception: %s", e
            )
            return False

    # Linear team key prefix -> Paperclip company UUID
    _PAPERCLIP_COMPANY_BY_PREFIX = {
        "HEA": "32375bd0-2a89-4a27-ad4a-e2050459224b",  # Healify
        "AUR": "c8435d96-f0c6-40c3-a6a0-53c0c5030707",  # Aurora Capital Group
        "SFB": "667127de-7bae-4941-8477-d8c2815908ea",  # Sam Feldt
        "HFT": "bd535bed-c589-40ef-a334-88cda5db85b9",  # Heartfeldt
    }

    async def _ensure_paperclip_issue(
        self,
        issue_identifier: str,
        issue_title: str,
        issue: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Find or create the Paperclip SSOT for a delegated Linear issue.

        Runs off the webhook hot path (background task). Uses paperclipai CLI
        so it shares the same identity/routing as the rest of the fleet.
        """
        import asyncio
        import re
        import subprocess

        prefix = (issue_identifier or "").split("-", 1)[0].upper()
        company = self._PAPERCLIP_COMPANY_BY_PREFIX.get(prefix)
        if not company:
            # Default to Healify for unknown product teams; Aurora for AUR-ish
            company = self._PAPERCLIP_COMPANY_BY_PREFIX["HEA"]
            logger.info(
                "[linear_agent_session] No company map for %s — using Healify",
                issue_identifier,
            )

        def _run(cmd: list) -> tuple[int, str]:
            try:
                out = subprocess.check_output(
                    cmd, stderr=subprocess.STDOUT, timeout=25, text=True
                )
                return 0, out
            except subprocess.CalledProcessError as e:
                return e.returncode, (e.output or "")[:4000]
            except Exception as e:
                return 1, str(e)

        # 1) Prefer existing open Paperclip issue matching linear:HEA-N
        match_key = issue_identifier
        code, out = await asyncio.to_thread(
            _run,
            [
                "paperclipai",
                "issue",
                "list",
                "-C",
                company,
                "--match",
                match_key,
                "--json",
            ],
        )
        if code == 0 and out.strip():
            try:
                items = json.loads(out)
            except json.JSONDecodeError:
                items = []
            if isinstance(items, list):
                for it in items:
                    body = (it.get("description") or "") + "\n" + (it.get("title") or "")
                    if (
                        f"linear:{issue_identifier}" in body
                        or issue_identifier in (it.get("title") or "")
                    ) and (it.get("status") or "") not in {
                        "done",
                        "cancelled",
                        "canceled",
                    }:
                        ident = it.get("identifier")
                        logger.info(
                            "[linear_agent_session] Reusing Paperclip %s for %s",
                            ident,
                            issue_identifier,
                        )
                        return ident

        # 2) Create
        # Title convention: Paperclip linked to Linear = [{linear_id}] {semantic}
        semantic = issue_title or issue_identifier
        # Strip existing [TEST]/… prefixes from Linear title carefully
        semantic = re.sub(r"^(\[[^\]]+\]\s*)+", "", semantic).strip() or issue_identifier
        title = f"[{issue_identifier}] {semantic}"[:240]
        description = (
            f"linear:{issue_identifier}\n"
            f"mirror:linear\n"
            f"origin:linear-agent-session\n\n"
            f"Delegated from Linear {issue_identifier}: {issue_title}\n"
        )
        if issue and issue.get("url"):
            description += f"\nOriginal Linear Issue: {issue.get('url')}\n"
        elif issue_identifier and issue_identifier != "?":
            description += (
                f"\nOriginal Linear Issue: "
                f"https://linear.app/lifecycle-innovations/issue/{issue_identifier}\n"
            )

        code, out = await asyncio.to_thread(
            _run,
            [
                "paperclipai",
                "issue",
                "create",
                "-C",
                company,
                "--title",
                title,
                "--description",
                description,
                "--status",
                "todo",
                "--priority",
                "medium",
                "--json",
            ],
        )
        if code != 0:
            logger.error(
                "[linear_agent_session] paperclip create failed for %s: %s",
                issue_identifier,
                out[:500],
            )
            return None
        try:
            created = json.loads(out)
        except json.JSONDecodeError:
            logger.error(
                "[linear_agent_session] paperclip create non-JSON for %s: %s",
                issue_identifier,
                out[:300],
            )
            return None
        ident = created.get("identifier")
        logger.info(
            "[linear_agent_session] Created Paperclip %s for Linear %s",
            ident,
            issue_identifier,
        )
        return ident

    # ------------------------------------------------------------------
    # Inbound: Linear webhook -> Hermes agent turn
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": "linear_agent_session"})

    def _validate_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """Linear-Signature: hex HMAC-SHA256 of the raw request body."""
        if not signature_header:
            return False
        expected = hmac.new(
            self._webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return _hmac_str_equal(signature_header, expected)

    def _record_delivery(self, delivery_id: str, now: float) -> bool:
        """Return True if this delivery should be processed (not a dup)."""
        cutoff = now - IDEMPOTENCY_TTL_SECONDS
        stale = [k for k, t in self._seen_deliveries.items() if t < cutoff]
        for k in stale:
            self._seen_deliveries.pop(k, None)
        if delivery_id in self._seen_deliveries:
            return False
        self._seen_deliveries[delivery_id] = now
        return True

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /linear-agent-session/webhook — AgentSessionEvent receiver.

        Auth-before-body-parsing, ACK-fast: signature is checked before any
        JSON parsing, and the HTTP response is returned before the agent
        turn (or even the synchronous first-activity post) blocks on
        anything slow, keeping this well under Linear's 5s ACK budget.
        """
        content_length = request.content_length or 0
        if content_length > MAX_BODY_BYTES:
            return web.json_response({"error": "Payload too large"}, status=413)

        try:
            raw_body = await request.read()
        except Exception as e:
            logger.error("[linear_agent_session] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        signature = request.headers.get("Linear-Signature", "")
        if not self._validate_signature(raw_body, signature):
            logger.warning("[linear_agent_session] Invalid or missing Linear-Signature")
            return web.json_response({"error": "Invalid signature"}, status=401)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Cannot parse body"}, status=400)

        event_type = payload.get("type", "")
        action = payload.get("action", "")

        if event_type != "AgentSessionEvent":
            # Not our event type (Linear can be configured to send other
            # categories to the same URL by mistake) — ack and ignore.
            return web.json_response({"status": "ignored", "type": event_type})

        delivery_id = request.headers.get(
            "Linear-Delivery", payload.get("webhookId", str(int(time.time() * 1000)))
        )
        now = time.time()
        if not self._record_delivery(f"{delivery_id}:{action}", now):
            return web.json_response({"status": "duplicate"}, status=200)

        agent_session = payload.get("agentSession", {}) or {}
        agent_session_id = agent_session.get("id")
        if not agent_session_id:
            logger.warning("[linear_agent_session] Event missing agentSession.id")
            return web.json_response({"error": "Missing agentSession.id"}, status=400)

        chat_id = f"linear-agent-session:{agent_session_id}"
        self._session_ids[chat_id] = agent_session_id

        issue = agent_session.get("issue") or {}
        issue_identifier = issue.get("identifier", "?")
        issue_title = issue.get("title", "")

        prompt_context = agent_session.get("promptContext", "")
        if action == "created":
            # Deterministic first activity — satisfies the 10s SLA regardless
            # of how long the dispatched agent turn takes to produce output.
            await self._create_activity(
                agent_session_id, "thought", "Claimed, starting work."
            )
            # Paperclip SSOT is ensured in the background task *before* the
            # agent turn so create is not dependent on model/tool thrash
            # (AUR-1757 criterion a). Webhook returns 202 immediately.
            text = None  # built after ensure_paperclip in background
            bg_kind = "created"
        elif action == "prompted":
            activity = payload.get("agentActivity", {}) or {}
            body = (activity.get("content", {}) or {}).get("body", "")
            signal = (activity.get("content", {}) or {}).get("signal")
            if signal == "stop":
                text = (
                    "The user sent a STOP signal in the Linear thread for "
                    f"{issue_identifier}. Wrap up safely and stop work on "
                    "this issue now."
                )
            else:
                text = (
                    f"New message from the user in the Linear thread for "
                    f"{issue_identifier} (continue the SAME session/task "
                    f"you were already working on):\n\n{body}"
                )
            bg_kind = "prompted"
        else:
            # created/prompted are the two documented actions as of the
            # 2026-07-25 Developer Preview docs; anything else is logged and
            # acked so an unrecognized future action doesn't retry forever.
            logger.info(
                "[linear_agent_session] Unhandled AgentSessionEvent action=%s",
                action,
            )
            return web.json_response({"status": "ignored", "action": action})

        logger.info(
            "[linear_agent_session] action=%s issue=%s session=%s",
            action,
            issue_identifier,
            agent_session_id,
        )

        async def _dispatch():
            nonlocal text
            paperclip_ident = None
            if bg_kind == "created":
                paperclip_ident = await self._ensure_paperclip_issue(
                    issue_identifier, issue_title, issue
                )
                if paperclip_ident:
                    await self._create_activity(
                        agent_session_id,
                        "thought",
                        f"Paperclip SSOT ready: {paperclip_ident}",
                    )
                pc_line = (
                    f"Linked Paperclip issue: {paperclip_ident}. Claim it and "
                    f"continue work there (do not create a duplicate).\n\n"
                    if paperclip_ident
                    else (
                        "Could not auto-create/find Paperclip SSOT — create "
                        f"one yourself for linear:{issue_identifier}, claim "
                        "it, then begin work.\n\n"
                    )
                )
                text = (
                    f"You have been delegated Linear issue {issue_identifier}: "
                    f"{issue_title}\n\n"
                    f"{pc_line}"
                    "Begin work now. Report meaningful progress as you go — "
                    "each response you send back becomes a live activity in "
                    "this Linear thread.\n\n"
                    f"Linear-provided context:\n{prompt_context}"
                )
            source = self.build_source(
                chat_id=chat_id,
                chat_name=f"linear/{issue_identifier}",
                chat_type="linear_agent_session",
                user_id=f"linear-agent-session:{agent_session_id}",
                user_name=issue_identifier,
            )
            event = MessageEvent(
                text=text or "",
                message_type=MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=f"{delivery_id}:{action}",
            )
            await self.handle_message(event)

        self._create_background_task(_dispatch())
        return web.json_response(
            {"status": "accepted", "action": action, "session": agent_session_id},
            status=202,
        )

    def _create_background_task(self, coro):
        import asyncio

        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="linear_agent_session",
        label="Linear Agent Session",
        adapter_factory=lambda cfg: LinearAgentSessionAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["LINEAR_AGENTCORE_TOKEN", "LINEAR_WEBHOOK_SECRET"],
        # Optional override; primary auth is HMAC + authorization_is_upstream.
        allowed_users_env="LINEAR_AGENT_SESSION_ALLOWED_USERS",
        allow_all_env="LINEAR_AGENT_SESSION_ALLOW_ALL_USERS",
        install_hint="httpx + aiohttp are already Hermes dependencies",
    )
