"""Narrow GatewayRunner integration for the opt-in Codex bridge lane."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from gateway.codex.protocol import BridgeOrigin, BridgeReply, BridgeRequest, ProgressEvent
from gateway.codex.service import CodexBridgeService, validate_workspace
from gateway.codex.settings import CodexBridgeSettings, load_codex_bridge_settings


@dataclass(frozen=True)
class GatewayBridgeResult:
    handled: bool
    response: str | None = None


class GatewayCodexBridgeMixin:
    """Narrow GatewayRunner integration point for the opt-in bridge lane."""

    _codex_bridge_service: CodexBridgeService | None = None
    _codex_bridge_settings_cache: CodexBridgeSettings | None = None

    def _codex_bridge_settings(self) -> CodexBridgeSettings:
        cached = getattr(self, "_codex_bridge_settings_cache", None)
        if cached is None:
            cached = load_codex_bridge_settings()
            self._codex_bridge_settings_cache = cached
        return cached

    def _ensure_codex_bridge_service(
        self, settings: CodexBridgeSettings
    ) -> CodexBridgeService:
        if (
            self._codex_bridge_service is None
            or self._codex_bridge_service.settings != settings
        ):
            self._codex_bridge_service = CodexBridgeService(settings)
        return self._codex_bridge_service

    async def _start_codex_bridge_projection(self) -> None:
        """Start the optional projector at Gateway startup so backlog self-drains."""

        settings = self._codex_bridge_settings()
        if not settings.enabled:
            return
        service = self._ensure_codex_bridge_service(settings)
        service.start_projection()

    async def _stop_codex_bridge_projection(self) -> None:
        service = getattr(self, "_codex_bridge_service", None)
        if service is not None:
            await service.stop_projection()

    def _build_bridge_request(
        self, event: Any, settings: CodexBridgeSettings
    ) -> BridgeRequest | None:
        source = event.source
        origin_type = str(getattr(getattr(source, "platform", None), "value", "") or "")
        if not settings.enabled or origin_type not in settings.allowed_origins:
            return None

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        text = str(event.text or "")
        explicit = metadata.get("codex_bridge_request") is True
        prefix = settings.command_prefix
        if not explicit:
            if not text.lower().startswith(prefix.lower()):
                return None
            text = text[len(prefix) :].lstrip()
        if not text:
            raise ValueError("Codex bridge prompt is empty")

        message_id = str(event.message_id or metadata.get("source_message_id") or "").strip()
        chat_id = str(getattr(source, "chat_id", "") or "").strip()
        if not message_id or not chat_id:
            raise ValueError("Codex bridge origin requires message_id and conversation_id")
        workspace_raw = metadata.get("workspace") or settings.default_workspace
        workspace = validate_workspace(str(workspace_raw or ""), settings.workspace_allowlist)
        idempotency_key = str(
            metadata.get("idempotency_key")
            or f"{origin_type}:{chat_id}:{message_id}"
        )
        job_id = str(
            metadata.get("hermes_job_id")
            or "job_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        )
        return BridgeRequest(
            hermes_job_id=job_id,
            idempotency_key=idempotency_key,
            origin=BridgeOrigin(
                type=origin_type,
                conversation_id=chat_id,
                message_id=message_id,
                user_id=str(getattr(source, "user_id", "") or "") or None,
                thread_id=str(getattr(source, "thread_id", "") or "") or None,
            ),
            workspace=str(workspace),
            prompt=text,
        )

    def _build_bridge_reply(
        self, event: Any, settings: CodexBridgeSettings
    ) -> BridgeReply | None:
        source = event.source
        origin_type = str(getattr(getattr(source, "platform", None), "value", "") or "")
        if not settings.enabled or origin_type not in settings.allowed_origins:
            return None
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        prompt_id = str(metadata.get("codex_bridge_prompt_id") or "").strip()
        if not prompt_id:
            return None
        answer = str(event.text or "").strip()
        message_id = str(event.message_id or metadata.get("source_message_id") or "").strip()
        conversation_id = str(getattr(source, "chat_id", "") or "").strip()
        if not answer:
            raise ValueError("Codex bridge reply is empty")
        if not message_id or not conversation_id:
            raise ValueError("Codex bridge reply requires message_id and conversation_id")
        return BridgeReply(
            prompt_id=prompt_id,
            idempotency_key=str(
                metadata.get("idempotency_key")
                or f"{origin_type}:{conversation_id}:{message_id}"
            ),
            origin=BridgeOrigin(
                type=origin_type,
                conversation_id=conversation_id,
                message_id=message_id,
                user_id=str(getattr(source, "user_id", "") or "") or None,
                thread_id=str(getattr(source, "thread_id", "") or "") or None,
            ),
            answer=answer,
        )

    async def _maybe_handle_codex_bridge(
        self,
        event: Any,
        notify_override: Callable[[ProgressEvent], Any] | None = None,
    ) -> GatewayBridgeResult:
        settings = self._codex_bridge_settings()
        try:
            reply = self._build_bridge_reply(event, settings)
            request = None if reply else self._build_bridge_request(event, settings)
        except ValueError as exc:
            return GatewayBridgeResult(True, f"Codex bridge rejected request: {exc}")
        if request is None and reply is None:
            return GatewayBridgeResult(False)

        service = self._ensure_codex_bridge_service(settings)
        adapter = None if notify_override is not None else self._adapter_for_source(event.source)
        if notify_override is None and adapter is None:
            return GatewayBridgeResult(
                True, "Codex bridge origin adapter is unavailable."
            )

        origin = reply.origin if reply else request.origin

        async def notify(progress: ProgressEvent) -> None:
            # Final result is returned through the gateway's normal response path.
            # These are compact interim notices only.
            if notify_override is not None:
                result = notify_override(progress)
                if inspect.isawaitable(result):
                    await result
                return
            metadata: dict[str, Any] = {
                "_interim_send": True,
                "codex_bridge_event": progress.as_dict(),
            }
            if origin.thread_id:
                metadata["thread_id"] = origin.thread_id
            await adapter.send(
                origin.conversation_id,
                progress.summary,
                reply_to=origin.message_id,
                metadata=metadata,
            )

        try:
            response = (
                await service.resume_with_reply(reply, notify)
                if reply
                else await service.execute(request, notify)
            )
        except ValueError as exc:
            kind = "reply" if reply else "request"
            return GatewayBridgeResult(True, f"Codex bridge rejected {kind}: {exc}")
        return GatewayBridgeResult(True, response)
