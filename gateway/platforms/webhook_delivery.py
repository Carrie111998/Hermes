"""Delivery responsibilities for the webhook adapter."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import weakref
from typing import Any, Mapping, Optional

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

from gateway.config import Platform
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookEnvelope,
)
from gateway.platforms.webhook_ledger import (
    OperationAuthority,
    OperationState,
    Settlement,
    SettlementKind,
    TargetAttemptDisposition,
    WebhookLedgerError,
    WebhookLedgerTransitionError,
)

from gateway.platforms.webhook_common import (
    WebhookTargetDeliveryDisposition,
    WebhookTargetDeliveryResult,
    _is_webhook_silence_response,
    _plain_json_snapshot,
)

logger = logging.getLogger(__name__)


class WebhookDeliveryMixin:
    def _source_for_envelope(self, envelope: WebhookEnvelope):
        """Create the exact webhook source without mutable profile-route lookup."""

        from gateway.session import SessionSource

        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id=envelope.session_key,
            chat_name=f"webhook/{envelope.route.name}",
            chat_type="webhook",
            user_id=f"webhook:{envelope.route.name}",
            user_name=envelope.route.name,
            profile=envelope.authority_profile,
        )
        source._transport_adapter_ref = weakref.ref(self)
        return source

    def _stage_exact_delivery(
        self,
        authority: OperationAuthority,
        content: str,
        carrier_snapshot: Mapping[str, Any],
    ) -> OperationAuthority:
        """Stage once, rejecting every later widening or content substitution."""

        current = self._operation_ledger.lookup_session(authority.session_key)
        if current is None:
            raise WebhookLedgerTransitionError("webhook operation no longer exists")
        if current.delivery is not None:
            exact_carrier = _plain_json_snapshot(current.delivery.carrier)
            requested_carrier = _plain_json_snapshot(carrier_snapshot)
            if (
                current.delivery.content != content
                or exact_carrier != requested_carrier
            ):
                if current.state is not OperationState.SETTLED:
                    self._operation_ledger.mark_indeterminate(
                        current,
                        "multiple final webhook contents attempted for one target",
                    )
                raise WebhookLedgerTransitionError(
                    "webhook target already owns different final content"
                )
            return current
        return self._operation_ledger.stage_delivery(
            current,
            content=content,
            carrier_snapshot=carrier_snapshot,
        )

    def _settle_attempt_result(
        self,
        attempt: Any,
        settlement: Settlement,
        success_disposition: WebhookTargetDeliveryDisposition,
    ) -> WebhookTargetDeliveryResult:
        def preserve_unknown(reason: str) -> None:
            try:
                self._operation_ledger.settle_target(
                    attempt,
                    Settlement(SettlementKind.INDETERMINATE, error=reason),
                )
            except Exception:
                logger.exception(
                    "[webhook] Could not preserve failed target settlement"
                )

        try:
            committed = self._operation_ledger.settle_target(attempt, settlement)
        except BaseException as exc:
            logger.exception("[webhook] Target settlement commit failed")
            preserve_unknown(f"durable target settlement failed: {exc}")
            self._fence_intake_for_durable_transition_failure(
                "target settlement transition failure"
            )
            if not isinstance(exc, Exception):
                raise
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error=f"durable target settlement failed: {exc}",
            )
        if not committed:
            preserve_unknown("durable target settlement fence was lost")
            self._fence_intake_for_durable_transition_failure(
                "target settlement authority loss"
            )
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error="durable target settlement fence was lost",
            )
        return WebhookTargetDeliveryResult(
            success_disposition,
            message_id=settlement.external_id,
            error=settlement.error,
        )

    def _prepare_github_invocation(
        self,
        authority: OperationAuthority,
    ) -> tuple[Optional[str], Optional[dict[str, str]], Optional[str]]:
        """Resolve one profile-scoped GitHub executable and minimal environment."""

        source = self._source_from_authority(authority)
        with self._profile_runtime_context(source):
            executable = shutil.which("gh")
            if not executable:
                return None, None, "gh CLI is unavailable"
            try:
                from agent.secret_scope import get_secret, is_multiplex_active
                from tools.environments.local import build_subprocess_env

                env = build_subprocess_env()
                token = get_secret("GH_TOKEN") or get_secret("GITHUB_TOKEN")
                if is_multiplex_active() and not token:
                    return (
                        None,
                        None,
                        "multiplexed GitHub delivery requires a profile-scoped token",
                    )
                if token:
                    env["GH_TOKEN"] = token
                env["GH_PROMPT_DISABLED"] = "1"
                env["GH_NO_UPDATE_NOTIFIER"] = "1"
            except Exception as exc:
                return None, None, f"GitHub delivery environment failed: {exc}"
        return executable, env, None

    @staticmethod
    def _run_github_comment(
        executable: str,
        target: Mapping[str, Any],
        content: str,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                executable,
                "pr",
                "comment",
                str(target["pr_number"]),
                "--repo",
                str(target["repo"]),
                "--body",
                content,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=dict(env),
            check=False,
        )

    async def _invoke_staged_target(
        self,
        authority: OperationAuthority,
    ) -> WebhookTargetDeliveryResult:
        """Consume the exact durable target/content through one mutation gate."""

        try:
            current = self._operation_ledger.lookup_session(authority.session_key)
            if current is None or current.delivery is None:
                raise WebhookLedgerTransitionError(
                    "webhook delivery carrier is not durably staged"
                )
            target = self._validate_target_snapshot(current.target_snapshot)
            if target["profile"] != current.profile:
                raise WebhookContractError(
                    "durable target profile conflicts with operation authority"
                )
        except (WebhookContractError, WebhookLedgerError) as exc:
            logger.error("[webhook] Refusing invalid durable target: %s", exc)
            self._mark_indeterminate_or_fence(
                authority,
                exc,
                context="invalid durable target",
            )
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error=str(exc),
            )

        suppress = target["kind"] == "log" or _is_webhook_silence_response(
            current.delivery.content
        )
        adapter = None
        executable = None
        github_env = None
        pre_effect_error = None
        if not suppress and target["kind"] == "platform":
            adapter = self._resolve_target_adapter(
                target["platform"], target["profile"]
            )
            if adapter is None:
                pre_effect_error = (
                    f"platform {target['platform']!r} is not connected for "
                    f"profile {target['profile']!r}"
                )
            elif target[
                "platform"
            ] == Platform.SLACK.value and not self._live_slack_scope_matches(
                target, adapter
            ):
                pre_effect_error = "Slack target workspace authority is unavailable"
        elif not suppress and target["kind"] == "github_comment":
            executable, github_env, pre_effect_error = self._prepare_github_invocation(
                current
            )

        try:
            generation_is_current = await asyncio.to_thread(
                self._recovery_profile_generation_is_current,
                current,
            )
        except asyncio.CancelledError:
            self._mark_indeterminate_or_fence(
                current,
                "webhook target generation check was cancelled",
                context="target generation-check cancellation",
            )
            raise
        except BaseException as exc:
            self._mark_indeterminate_or_fence(
                current,
                exc,
                context="target generation-check failure",
            )
            if not isinstance(exc, Exception):
                raise
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error="webhook profile authority could not be checked",
            )
        if not generation_is_current:
            self._mark_indeterminate_or_fence(
                current,
                "webhook profile incarnation changed before target attempt",
                context="target profile generation mismatch",
            )
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error="webhook profile authority changed before delivery",
            )

        try:
            attempt = self._operation_ledger.begin_target(current)
        except BaseException as exc:
            self._fence_intake_for_durable_transition_failure(
                "target-attempt transition failure"
            )
            if not isinstance(exc, Exception):
                raise
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error=f"target mutation gate failed: {exc}",
            )
        if attempt.disposition is TargetAttemptDisposition.CACHED:
            return WebhookTargetDeliveryResult(WebhookTargetDeliveryDisposition.CACHED)
        if attempt.disposition is TargetAttemptDisposition.IN_PROGRESS:
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.IN_PROGRESS,
                error="webhook target attempt is already in progress",
            )
        if attempt.disposition is TargetAttemptDisposition.INDETERMINATE:
            return WebhookTargetDeliveryResult(
                WebhookTargetDeliveryDisposition.INDETERMINATE,
                error="webhook target outcome requires reconciliation",
            )
        if attempt.delivery is None:
            return self._settle_attempt_result(
                attempt,
                Settlement(
                    SettlementKind.INDETERMINATE,
                    error="target gate returned no durable delivery",
                ),
                WebhookTargetDeliveryDisposition.INDETERMINATE,
            )
        if pre_effect_error:
            return self._settle_attempt_result(
                attempt,
                Settlement(
                    SettlementKind.PRE_EFFECT_FAILED,
                    error=pre_effect_error,
                ),
                WebhookTargetDeliveryDisposition.PRE_EFFECT_FAILED,
            )
        if suppress:
            if target["kind"] == "log":
                logger.info(
                    "[webhook] Response for %s: %s",
                    current.session_key,
                    attempt.delivery.content[:200],
                )
            return self._settle_attempt_result(
                attempt,
                Settlement(SettlementKind.SUPPRESSED),
                WebhookTargetDeliveryDisposition.SUPPRESSED,
            )

        if target["kind"] == "platform":
            metadata: dict[str, str] = {}
            if target.get("thread_id"):
                metadata["thread_id"] = target["thread_id"]
            if target.get("scope_id"):
                metadata["scope_id"] = target["scope_id"]
            try:
                source = self._source_from_authority(current)
                with self._profile_runtime_context(source):
                    send_result = await adapter.send(
                        target["chat_id"],
                        attempt.delivery.content,
                        metadata=metadata or None,
                    )
            except asyncio.CancelledError:
                self._settle_attempt_result(
                    attempt,
                    Settlement(
                        SettlementKind.INDETERMINATE,
                        error="platform delivery was cancelled after invocation",
                    ),
                    WebhookTargetDeliveryDisposition.INDETERMINATE,
                )
                raise
            except BaseException as exc:
                result = self._settle_attempt_result(
                    attempt,
                    Settlement(SettlementKind.INDETERMINATE, error=str(exc)),
                    WebhookTargetDeliveryDisposition.INDETERMINATE,
                )
                if not isinstance(exc, Exception):
                    raise
                return result
            if getattr(send_result, "success", None) is True:
                message_id = getattr(send_result, "message_id", None)
                return self._settle_attempt_result(
                    attempt,
                    Settlement(
                        SettlementKind.CONFIRMED,
                        external_id=str(message_id) if message_id else None,
                    ),
                    WebhookTargetDeliveryDisposition.CONFIRMED,
                )
            return self._settle_attempt_result(
                attempt,
                Settlement(
                    SettlementKind.INDETERMINATE,
                    error=str(getattr(send_result, "error", None) or "send rejected"),
                ),
                WebhookTargetDeliveryDisposition.INDETERMINATE,
            )

        try:
            completed = await asyncio.to_thread(
                self._run_github_comment,
                executable,
                target,
                attempt.delivery.content,
                github_env,
            )
        except asyncio.CancelledError:
            self._settle_attempt_result(
                attempt,
                Settlement(
                    SettlementKind.INDETERMINATE,
                    error="GitHub delivery was cancelled after invocation",
                ),
                WebhookTargetDeliveryDisposition.INDETERMINATE,
            )
            raise
        except FileNotFoundError as exc:
            return self._settle_attempt_result(
                attempt,
                Settlement(SettlementKind.PRE_EFFECT_FAILED, error=str(exc)),
                WebhookTargetDeliveryDisposition.PRE_EFFECT_FAILED,
            )
        except BaseException as exc:
            result = self._settle_attempt_result(
                attempt,
                Settlement(SettlementKind.INDETERMINATE, error=str(exc)),
                WebhookTargetDeliveryDisposition.INDETERMINATE,
            )
            if not isinstance(exc, Exception):
                raise
            return result
        if completed.returncode == 0:
            return self._settle_attempt_result(
                attempt,
                Settlement(SettlementKind.CONFIRMED),
                WebhookTargetDeliveryDisposition.CONFIRMED,
            )
        return self._settle_attempt_result(
            attempt,
            Settlement(
                SettlementKind.INDETERMINATE,
                error=f"gh exited with status {completed.returncode}",
            ),
            WebhookTargetDeliveryDisposition.INDETERMINATE,
        )

    @staticmethod
    def _target_http_response(
        result: WebhookTargetDeliveryResult,
        *,
        route: str,
        delivery_id: str,
        target_kind: Optional[str] = None,
    ):
        payload: dict[str, Any] = {
            "route": route,
            "delivery_id": delivery_id,
            "settlement": result.disposition.value,
        }
        if target_kind:
            payload["target"] = target_kind
        if result.success:
            payload["status"] = (
                "suppressed"
                if result.disposition is WebhookTargetDeliveryDisposition.SUPPRESSED
                else "delivered"
            )
            return web.json_response(payload, status=200)
        if result.disposition is WebhookTargetDeliveryDisposition.PRE_EFFECT_FAILED:
            payload.update({
                "status": "unavailable",
                "error": "Webhook target is unavailable",
            })
            return web.json_response(
                payload,
                status=503,
                headers={"Retry-After": "5"},
            )
        if result.disposition is WebhookTargetDeliveryDisposition.IN_PROGRESS:
            payload["status"] = "in_progress"
            return web.json_response(payload, status=202)
        payload.update({
            "status": "indeterminate",
            "error": "Webhook target outcome requires reconciliation",
        })
        return web.json_response(payload, status=502)

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------
