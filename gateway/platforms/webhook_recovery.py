"""Recovery responsibilities for the webhook adapter."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable, Mapping

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.platforms.webhook_contract import (
    WebhookContractError,
)
from gateway.platforms.webhook_ledger import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    MAXIMUM_RECOVERY_PROFILES,
    OperationAuthority,
    OperationState,
    RecoveryBatch,
    WebhookLedgerError,
    WebhookLedgerTransitionError,
)
from gateway.platforms.webhook_terminal import (
    WebhookTerminalOutcome,
    terminal_outcome_carrier,
    terminal_outcome_notice,
)

from gateway.platforms.webhook_common import (
    WebhookMessageEvent,
    _RECOVERY_CONCURRENCY_LIMIT,
    _RECOVERY_PAGE_BUDGET,
    _clear_quarantined_retirement_owner,
    _plain_json_snapshot,
    _quarantined_retirement_owners,
)

logger = logging.getLogger(__name__)


class WebhookRecoveryMixin:
    def _event_from_authority(
        self, authority: OperationAuthority
    ) -> WebhookMessageEvent:
        snapshot = authority.event_snapshot
        if not isinstance(snapshot, Mapping) or snapshot.get("v") != 1:
            raise WebhookContractError("durable event snapshot is invalid")
        if set(snapshot) != {
            "v",
            "mode",
            "text",
            "payload",
            "message_id",
            "source",
        }:
            raise WebhookContractError("durable event snapshot has unknown fields")
        if snapshot.get("mode") != "agent":
            raise WebhookContractError("durable event is not an agent operation")
        text = snapshot.get("text")
        message_id = snapshot.get("message_id")
        payload = snapshot.get("payload")
        if not isinstance(text, str) or not isinstance(message_id, str):
            raise WebhookContractError("durable event text or message_id is invalid")
        if not isinstance(payload, Mapping):
            raise WebhookContractError("durable event payload is invalid")
        return WebhookMessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=self._source_from_authority(authority),
            raw_message=_plain_json_snapshot(payload),
            message_id=message_id,
            webhook_authority=authority,
            webhook_envelope=None,
            allow_gateway_control=False,
        )

    async def _recover_event_ready(self, authority: OperationAuthority) -> None:
        snapshot = authority.event_snapshot
        if not isinstance(snapshot, Mapping):
            raise WebhookContractError("recovered event snapshot is missing")
        mode = snapshot.get("mode")
        if mode == "agent":
            event = self._event_from_authority(authority)
            # Revalidate durable profile authority before Base can cross the
            # RUNNING gate or dispatch an agent. A profile directory may still
            # exist after the operator removes it from the served allowlist.
            with self._profile_runtime_context(event.source):
                pass
            await self.handle_message(event)
            # Base dispatch is intentionally backgrounded. Keep this recovery
            # carrier registered until that exact task crosses RUNNING and
            # completes; otherwise a refill page can observe generation>=2
            # READY after the wrapper returns and schedule it a second time.
            from gateway.session import build_session_key

            session_key = build_session_key(
                event.source,
                group_sessions_per_user=self.config.extra.get(
                    "group_sessions_per_user", True
                ),
                thread_sessions_per_user=self.config.extra.get(
                    "thread_sessions_per_user", False
                ),
                profile=self._session_key_profile(event.source),
            )
            processing_task = self._session_tasks.get(session_key)
            if processing_task is not None:
                await processing_task
            current = self._operation_ledger.lookup_session(authority.session_key)
            if current is not None and current.state is OperationState.READY:
                raise WebhookLedgerTransitionError(
                    "recovered event dispatch did not cross the running gate"
                )
            return
        if mode != "direct":
            raise WebhookContractError("recovered event mode is invalid")
        if set(snapshot) != {
            "v",
            "mode",
            "text",
            "payload",
            "message_id",
            "source",
        }:
            raise WebhookContractError("recovered direct event has unknown fields")
        content = snapshot.get("text")
        if not isinstance(content, str):
            raise WebhookContractError("recovered direct content is invalid")
        # Cross-check source/profile authority even though direct mode does not
        # enter the agent pipeline.
        self._source_from_authority(authority)
        try:
            generation_is_current = await asyncio.to_thread(
                self._recovery_profile_generation_is_current,
                authority,
            )
        except asyncio.CancelledError:
            self._mark_indeterminate_or_fence(
                authority,
                "recovered direct generation check was cancelled",
                context="recovered direct generation-check cancellation",
            )
            raise
        except BaseException as exc:
            self._mark_indeterminate_or_fence(
                authority,
                exc,
                context="recovered direct generation-check failure",
            )
            raise
        if not generation_is_current:
            self._mark_indeterminate_or_fence(
                authority,
                "webhook profile incarnation changed before recovered direct execution",
                context="recovered direct profile generation mismatch",
            )
            return
        try:
            entered_running = self._operation_ledger.mark_running(authority)
        except BaseException:
            self._fence_intake_for_durable_transition_failure(
                "recovered direct running-gate transition failure"
            )
            raise
        if not entered_running:
            self._fence_intake_for_durable_transition_failure(
                "recovered direct running-gate authority loss"
            )
            raise WebhookLedgerTransitionError(
                "recovered direct operation lost its running gate"
            )
        staged = self._stage_exact_delivery(
            authority, content, {"v": 1, "kind": "direct"}
        )
        await self._invoke_staged_target(staged)

    def _track_recovery_task(
        self,
        task: "asyncio.Task",
        authority: OperationAuthority,
        label: str,
        started: list[bool],
    ) -> None:
        self._background_tasks.add(task)
        self._recovery_tasks_by_operation[authority.operation_id] = task

        def done(completed: "asyncio.Task") -> None:
            self._background_tasks.discard(completed)
            if (
                self._recovery_tasks_by_operation.get(authority.operation_id)
                is completed
            ):
                self._recovery_tasks_by_operation.pop(authority.operation_id, None)
            failed = completed.cancelled()
            error: object = f"{label} task was cancelled"
            if failed and not started[0]:
                try:
                    if self._operation_ledger.relinquish_recovery_claim(authority):
                        self._recovery_restart_dead_scan = True
                        self._recovery_backlog_pending = True
                        schedule_retry = getattr(
                            self.gateway_runner,
                            "_schedule_webhook_recovery_retry",
                            None,
                        )
                        if callable(schedule_retry):
                            schedule_retry(self)
                        return
                except Exception:
                    logger.exception(
                        "[webhook] Failed to relinquish unstarted %s task", label
                    )
                    self._fence_intake_for_durable_transition_failure(
                        f"{label} claim relinquishment failure"
                    )
                    return
                self._fence_intake_for_durable_transition_failure(
                    f"{label} claim relinquishment authority loss"
                )
                return
            if not failed:
                try:
                    exception = completed.exception()
                    failed = exception is not None
                    if exception is not None:
                        error = exception
                except BaseException as exc:
                    failed = True
                    error = exc
            if failed:
                self._mark_indeterminate_or_fence(
                    authority,
                    error,
                    context=f"{label} task failure",
                )
            if self._recovery_backlog_pending:
                schedule_retry = getattr(
                    self.gateway_runner,
                    "_schedule_webhook_recovery_retry",
                    None,
                )
                if callable(schedule_retry):
                    schedule_retry(self)

        task.add_done_callback(done)

    def _schedule_recovery_task(
        self,
        authority: OperationAuthority,
        label: str,
        operation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Schedule one carrier per operation without creating it prematurely."""

        existing = self._recovery_tasks_by_operation.get(authority.operation_id)
        if existing is not None and not existing.done():
            return False
        active_count = sum(
            not active.done() for active in self._recovery_tasks_by_operation.values()
        )
        if active_count >= _RECOVERY_CONCURRENCY_LIMIT:
            return False

        started = [False]

        async def run() -> None:
            started[0] = True
            await operation()

        task = asyncio.create_task(run())
        self._track_recovery_task(task, authority, label, started)
        return True

    def _served_recovery_profiles(self) -> tuple[str, ...]:
        """Freeze the exact physical profile domains served by this gateway."""

        from hermes_cli.profiles import get_active_profile_name, profiles_to_serve

        runner_config = getattr(self.gateway_runner, "config", None)
        multiplex = getattr(runner_config, "multiplex_profiles", False) is True
        if multiplex:
            served = profiles_to_serve(
                multiplex=True,
                profile_allowlist=getattr(
                    runner_config,
                    "multiplex_profile_allowlist",
                    None,
                ),
            )
            profiles = {name for name, _home in served}
        else:
            active = get_active_profile_name() or "default"
            profiles = {"default" if active == "custom" else active}
        if len(profiles) > MAXIMUM_RECOVERY_PROFILES:
            raise WebhookContractError(
                "webhook recovery profile authority exceeds its bounded limit"
            )
        return tuple(sorted(profiles))

    def _schedule_recovery_batch(self, batch: RecoveryBatch) -> int:
        """Schedule every authority returned by one slot-bounded claim page."""

        scheduled = 0
        for authority in batch.event_ready:
            if not self._recovery_profile_generation_is_current(authority):
                self._mark_indeterminate_or_fence(
                    authority,
                    "webhook profile incarnation changed before event recovery",
                    context="recovered event profile generation mismatch",
                )
                continue
            if self._schedule_recovery_task(
                authority,
                "event recovery",
                lambda authority=authority: self._recover_event_ready(authority),
            ):
                scheduled += 1
        for authority in batch.delivery_ready:
            if not self._recovery_profile_generation_is_current(authority):
                self._mark_indeterminate_or_fence(
                    authority,
                    "webhook profile incarnation changed before delivery recovery",
                    context="recovered delivery profile generation mismatch",
                )
                continue
            if self._schedule_recovery_task(
                authority,
                "delivery recovery",
                lambda authority=authority: self._invoke_staged_target(authority),
            ):
                scheduled += 1
        return scheduled

    def _recovery_profile_generation_is_current(
        self,
        authority: OperationAuthority,
    ) -> bool:
        """Require a recovered carrier to belong to the same profile instance."""

        grant = authority.grant_snapshot
        durable_generation = (
            grant.get("profile_generation") if isinstance(grant, Mapping) else None
        )
        if not isinstance(durable_generation, str) or not durable_generation:
            return False
        try:
            current_generation = self._current_profile_authority_generation(
                authority.profile,
                route_name=authority.route,
            )
        except Exception:
            logger.exception(
                "[webhook] Could not resolve current profile generation for %s/%s",
                authority.profile,
                authority.route,
            )
            return False
        return secrets.compare_digest(durable_generation, current_generation)

    async def recover_pending_operations(self, *, trigger: str = "manual") -> int:
        """Pump bounded recovery pages and resume only replay-safe carriers."""

        self._accepting_webhooks = False
        try:
            async with self._recovery_pump_lock:
                if self._lifecycle_retiring:
                    self._recovery_backlog_pending = True
                    return 0

                if not self._recovery_cycle_active:
                    self._recovery_cycle_active = True
                    self._dead_owner_recovery_cursor = None
                    self._dead_owner_recovery_complete = False
                    self._current_recovery_cursor = None
                    self._current_recovery_complete = False
                    self._recovery_authority_profiles = await asyncio.to_thread(
                        self._served_recovery_profiles
                    )
                if self._recovery_restart_dead_scan:
                    self._dead_owner_recovery_cursor = None
                    self._dead_owner_recovery_complete = False
                    self._recovery_restart_dead_scan = False

                page_budget = _RECOVERY_PAGE_BUDGET
                scheduled = 0
                released = 0
                indeterminate = 0
                progress = False

                # A same-process predecessor has our PID, so ordinary owner
                # liveness cannot identify it as stale. Retire exact marked
                # owners first, keeping each marker until its final page.
                quarantined_owners = _quarantined_retirement_owners(
                    self._operation_ledger
                )
                while quarantined_owners and page_budget > 0:
                    prior_owner = quarantined_owners[0]
                    retired = await self._run_ledger_mutation_barrier(
                        lambda prior_owner=prior_owner: (
                            self._operation_ledger.retire_owner_instance(
                                prior_owner,
                            )
                        )
                    )
                    page_budget -= 1
                    released += len(retired.released)
                    indeterminate += len(retired.indeterminate)
                    progress = progress or retired.scanned_count > 0
                    if not retired.has_more:
                        _clear_quarantined_retirement_owner(
                            self._operation_ledger,
                            prior_owner,
                        )
                    quarantined_owners = _quarantined_retirement_owners(
                        self._operation_ledger
                    )
                    if quarantined_owners and page_budget > 0:
                        await asyncio.sleep(0)

                # Rediscover current-owner delivery carriers and any claim
                # committed immediately before a cancelled scheduling handoff.
                # Do this before new dead-owner claims, so every newly claimed
                # authority returned below can be scheduled synchronously and
                # a completed dead scan needs no second discovery pass.
                while (
                    not quarantined_owners
                    and page_budget > 0
                    and not self._current_recovery_complete
                ):
                    active_count = sum(
                        not task.done()
                        for task in self._recovery_tasks_by_operation.values()
                    )
                    free_slots = _RECOVERY_CONCURRENCY_LIMIT - active_count
                    if free_slots <= 0:
                        break
                    cursor = self._current_recovery_cursor
                    page = await asyncio.to_thread(
                        self._operation_ledger.list_current_recovery_ready,
                        limit=min(DEFAULT_RECOVERY_BATCH_SIZE, free_slots),
                        after=cursor,
                        profiles=self._recovery_authority_profiles,
                    )
                    page_budget -= 1
                    progress = progress or page.scanned_count > 0
                    page_scheduled = self._schedule_recovery_batch(page)
                    scheduled += page_scheduled
                    if page.has_more:
                        if page.next_cursor is None:
                            raise WebhookLedgerError(
                                "current recovery page lost its continuation"
                            )
                        self._current_recovery_cursor = page.next_cursor
                    else:
                        self._current_recovery_cursor = None
                        self._current_recovery_complete = True
                        self._recovery_current_scan_required = False
                    if page_scheduled:
                        page_budget = 0
                    elif page_budget > 0:
                        await asyncio.sleep(0)

                # Never claim work while any exact same-process owner remains
                # unfenced or while previously claimed work is not scheduled.
                if not quarantined_owners and self._current_recovery_complete:
                    while page_budget > 0 and not self._dead_owner_recovery_complete:
                        active_count = sum(
                            not task.done()
                            for task in self._recovery_tasks_by_operation.values()
                        )
                        free_slots = _RECOVERY_CONCURRENCY_LIMIT - active_count
                        if free_slots <= 0:
                            break
                        claim_limit = min(
                            DEFAULT_RECOVERY_BATCH_SIZE,
                            free_slots,
                        )
                        cursor = self._dead_owner_recovery_cursor
                        profiles = self._recovery_authority_profiles
                        self._dead_claim_handoff_in_progress = True
                        batch = await self._run_ledger_mutation_barrier(
                            lambda cursor=cursor, profiles=profiles, claim_limit=claim_limit: (
                                self._operation_ledger.recover_dead_owners_page(
                                    limit=claim_limit,
                                    after=cursor,
                                    profiles=profiles,
                                )
                            )
                        )
                        page_budget -= 1
                        released += len(batch.released)
                        indeterminate += len(batch.indeterminate)
                        progress = progress or batch.scanned_count > 0
                        page_scheduled = self._schedule_recovery_batch(batch)
                        self._dead_claim_handoff_in_progress = False
                        scheduled += page_scheduled
                        if batch.has_more:
                            if batch.next_cursor is None:
                                raise WebhookLedgerError(
                                    "bounded recovery page lost its continuation"
                                )
                            self._dead_owner_recovery_cursor = batch.next_cursor
                        else:
                            self._dead_owner_recovery_cursor = None
                            self._dead_owner_recovery_complete = True
                        if page_scheduled:
                            page_budget = 0
                        elif page_budget > 0:
                            await asyncio.sleep(0)

                self._recovery_backlog_pending = bool(
                    quarantined_owners
                    or not self._dead_owner_recovery_complete
                    or not self._current_recovery_complete
                )
                self._recovery_last_progress = progress
                self._recovery_last_error = False
                if not self._recovery_backlog_pending:
                    self._recovery_cycle_active = False
                    self._dead_owner_recovery_cursor = None
                    self._current_recovery_cursor = None
                else:
                    schedule_retry = getattr(
                        self.gateway_runner,
                        "_schedule_webhook_recovery_retry",
                        None,
                    )
                    if callable(schedule_retry):
                        schedule_retry(self)

                if scheduled or released or indeterminate or progress:
                    logger.info(
                        "[webhook] Recovery trigger=%s scheduled=%d released=%d "
                        "indeterminate=%d quarantined_owners=%d backlog=%s",
                        trigger,
                        scheduled,
                        released,
                        indeterminate,
                        len(quarantined_owners),
                        self._recovery_backlog_pending,
                    )
                return scheduled
        except BaseException:
            self._accepting_webhooks = False
            self._recovery_backlog_pending = True
            self._recovery_last_progress = False
            self._recovery_last_error = True
            self._recovery_cycle_active = False
            self._dead_owner_recovery_cursor = None
            self._dead_owner_recovery_complete = False
            self._current_recovery_cursor = None
            self._current_recovery_complete = False
            self._recovery_current_scan_required = bool(
                self._recovery_current_scan_required
                or self._dead_claim_handoff_in_progress
            )
            self._dead_claim_handoff_in_progress = False
            raise

    async def on_processing_start(self, event: "MessageEvent") -> None:
        """Cross the durable execution gate before the agent or any tool runs."""

        authority = getattr(event, "webhook_authority", None)
        if not isinstance(authority, OperationAuthority):
            raise WebhookLedgerTransitionError(
                "webhook event has no exact durable operation authority"
            )
        try:
            generation_is_current = await asyncio.to_thread(
                self._recovery_profile_generation_is_current,
                authority,
            )
        except asyncio.CancelledError:
            self._mark_indeterminate_or_fence(
                authority,
                "webhook agent generation check was cancelled",
                context="agent generation-check cancellation",
            )
            raise
        except BaseException as exc:
            self._mark_indeterminate_or_fence(
                authority,
                exc,
                context="agent generation-check failure",
            )
            raise
        if not generation_is_current:
            self._mark_indeterminate_or_fence(
                authority,
                "webhook profile incarnation changed before agent execution",
                context="agent profile generation mismatch",
            )
            raise WebhookLedgerTransitionError(
                "webhook profile authority changed before execution"
            )
        try:
            entered_running = self._operation_ledger.mark_running(authority)
        except BaseException:
            self._fence_intake_for_durable_transition_failure(
                "agent running-gate transition failure"
            )
            raise
        if not entered_running:
            self._fence_intake_for_durable_transition_failure(
                "agent running-gate authority loss"
            )
            raise WebhookLedgerTransitionError(
                "webhook operation could not enter running state"
            )

    async def _run_processing_hook(
        self, hook_name: str, *args: Any, **kwargs: Any
    ) -> None:
        """Make the webhook start gate fail closed; keep completion best-effort."""

        if hook_name == "on_processing_start":
            await self.on_processing_start(*args, **kwargs)
            return
        await super()._run_processing_hook(hook_name, *args, **kwargs)

    async def on_processing_complete(
        self, event: "MessageEvent", outcome: ProcessingOutcome
    ) -> None:
        """Reconcile exact durable state, then close the one-shot session."""

        authority = getattr(event, "webhook_authority", None)
        try:
            if not isinstance(authority, OperationAuthority):
                logger.error("[webhook] Completion event lacks durable authority")
                return
            current = self._operation_ledger.lookup_session(authority.session_key)
            if current is None:
                logger.error(
                    "[webhook] Completion authority disappeared for %s",
                    authority.operation_id,
                )
                return
            if current.state in {
                OperationState.SETTLED,
                OperationState.INDETERMINATE,
                OperationState.DELIVERY_READY,
            }:
                return
            if current.state is OperationState.RUNNING:
                if outcome is ProcessingOutcome.SUCCESS:
                    if not self._operation_ledger.settle_no_effect(
                        current, "agent produced no final webhook effect"
                    ):
                        self._operation_ledger.mark_indeterminate(
                            current,
                            "agent completed without a provable final settlement",
                        )
                elif outcome is ProcessingOutcome.FAILURE:
                    try:
                        staged = self._stage_exact_delivery(
                            current,
                            terminal_outcome_notice(WebhookTerminalOutcome.ERROR),
                            terminal_outcome_carrier(WebhookTerminalOutcome.ERROR),
                        )
                    except BaseException as exc:
                        self._mark_indeterminate_or_fence(
                            current,
                            exc,
                            context="terminal error carrier staging failure",
                        )
                        if not isinstance(exc, Exception):
                            raise
                        return
                    # This is the same durable target gate used by normal finals.
                    # A pre-effect failure remains recoverable; anything after
                    # invocation is settled indeterminate and never retried blind.
                    await self._invoke_staged_target(staged)
                else:
                    self._operation_ledger.mark_indeterminate(
                        current,
                        f"agent processing ended with {outcome.value}",
                    )
                return
            self._operation_ledger.mark_indeterminate(
                current,
                f"agent completion observed unexpected state {current.state.value}",
            )
        except Exception:
            logger.exception("[webhook] Durable completion reconciliation failed")
        finally:
            await self._end_webhook_session(event, event.source.chat_id)

    async def _end_webhook_session(
        self, event: "MessageEvent", session_chat_id: str
    ) -> None:
        """Mark the per-delivery webhook session ended in state.db.

        Resolves the persisted ``session_id`` from the gateway session store
        using the SAME source the run was keyed on (so profile multiplexing
        and key construction match exactly), then closes it via the existing
        ``SessionDB.end_session`` API — never a hand-written UPDATE.
        """
        runner = self.gateway_runner
        if runner is None:
            return

        async def _close_in_active_profile_scope() -> None:
            # Both handles resolve through the active HERMES_HOME. They must be
            # acquired inside the admitted profile scope, never before it.
            session_db = getattr(runner, "_session_db", None)
            store = getattr(runner, "session_store", None)
            if session_db is None or store is None:
                return
            key_fn = getattr(runner, "_session_key_for_source", None)
            if key_fn is None:
                return
            session_key = key_fn(event.source)
            # Resolve the persisted session_id via the store's public,
            # lock-held accessor (peek_session_id) rather than reaching into
            # the private _entries dict without the store lock. Fall back to
            # the private path only for older stores / test doubles that
            # predate the accessor.
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                session_id = peek(session_key)
            else:
                if hasattr(store, "_ensure_loaded"):
                    try:
                        store._ensure_loaded()
                    except Exception:
                        pass
                entries = getattr(store, "_entries", {}) or {}
                entry = entries.get(session_key)
                session_id = getattr(entry, "session_id", None) if entry else None
            if not session_id:
                logger.debug(
                    "[webhook] No session_id to close for %s (key=%s)",
                    session_chat_id,
                    session_key,
                )
                return
            # AsyncSessionDB forwards end_session via asyncio.to_thread; a
            # plain SessionDB exposes it synchronously.  Handle both.
            _end = session_db.end_session
            result = _end(session_id, "webhook_complete")
            if asyncio.iscoroutine(result):
                await result
            logger.debug(
                "[webhook] Closed session %s for delivery %s",
                session_id,
                session_chat_id,
            )

        try:
            config = getattr(runner, "config", None)
            if getattr(config, "multiplex_profiles", False) is True:
                resolve_home = getattr(runner, "_resolve_profile_home_for_source", None)
                if not callable(resolve_home):
                    logger.warning(
                        "[webhook] Cannot close multiplexed session %s: "
                        "profile resolver is unavailable",
                        session_chat_id,
                    )
                    return
                profile_home = resolve_home(event.source)
                from gateway.run import _profile_runtime_scope

                with _profile_runtime_scope(profile_home):
                    await _close_in_active_profile_scope()
            else:
                await _close_in_active_profile_scope()
        except Exception as e:
            logger.debug(
                "[webhook] Failed to close session for %s: %s",
                session_chat_id,
                e,
            )

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------
