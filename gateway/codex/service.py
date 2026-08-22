"""Behavior-preserving orchestration for bridge execution and projection wakes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway.codex.executor import CodexSdkExecutor, CodexUserQuestion
from gateway.codex.protocol import (
    BridgeEventProjector,
    BridgeExecutionResult,
    BridgeOrigin,
    BridgeReply,
    BridgeRequest,
    CodexExecutor,
    ProgressEvent,
    _utc_now,
)
from gateway.codex.settings import CodexBridgeSettings
from gateway.codex.store import BridgeStore


logger = logging.getLogger("gateway.codex_bridge")

def validate_workspace(workspace: str, allowlist: tuple[str, ...]) -> Path:
    """Resolve a workspace and require it to be under an explicit allowlist."""

    if not workspace:
        raise ValueError("Codex bridge request is missing a workspace")
    if not allowlist:
        raise ValueError("codex_bridge.workspace_allowlist must contain at least one path")
    candidate = Path(workspace).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("Codex bridge workspace must be an existing directory")

    candidate_norm = os.path.normcase(str(candidate))
    for allowed in allowlist:
        try:
            root = Path(allowed).expanduser().resolve(strict=True)
            root_norm = os.path.normcase(str(root))
            if os.path.commonpath((candidate_norm, root_norm)) == root_norm:
                return candidate
        except (OSError, ValueError):
            continue
    raise ValueError("Codex bridge workspace is outside the configured allowlist")


def _needs_user_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("login", "authentication", "authorization", "approval", "credential")
    )


class CodexBridgeService:
    def __init__(
        self,
        settings: CodexBridgeSettings,
        *,
        store: BridgeStore | None = None,
        executor: CodexExecutor | None = None,
        instance_id: str | None = None,
        projector: BridgeEventProjector | None = None,
    ):
        self.settings = settings
        self.store = store or BridgeStore()
        self.executor = executor or CodexSdkExecutor(settings)
        self.instance_id = instance_id or uuid.uuid4().hex
        self.projector = projector or self._load_optional_projector()
        self._projection_worker: asyncio.Task[None] | None = None
        self._projection_wake = asyncio.Event()
        self._projection_stop = asyncio.Event()
        self._projection_idle = asyncio.Event()
        self._projection_idle.set()

    def _load_optional_projector(self) -> BridgeEventProjector | None:
        """Construct the projection consumer only behind its explicit flag."""

        try:
            from gateway.codex.kanban_projection import CodexKanbanProjector
            from gateway.codex.kanban_settings import (
                load_kanban_projection_settings,
            )

            projection_settings = load_kanban_projection_settings()
            if not projection_settings.enabled:
                return None
            return CodexKanbanProjector(self.store.path, projection_settings)
        except Exception:
            logger.warning(
                "Kanban projection initialization failed; Codex execution remains active",
                exc_info=True,
            )
            return None

    def _projection_retry_settings(self) -> tuple[float, float, float]:
        settings = getattr(self.projector, "settings", None)
        initial = max(0.05, float(getattr(settings, "retry_initial_seconds", 1.0)))
        maximum = max(initial, float(getattr(settings, "retry_max_seconds", 30.0)))
        shutdown = max(0.1, float(getattr(settings, "shutdown_timeout_seconds", 5.0)))
        return initial, maximum, shutdown

    async def _record_projection_retry(
        self, retry_count: int, next_retry_at: str | None, state: str
    ) -> None:
        recorder = getattr(self.projector, "record_retry_state", None)
        if callable(recorder):
            try:
                await asyncio.to_thread(
                    recorder, retry_count, next_retry_at, state
                )
            except Exception:
                logger.debug("Could not persist projection retry state", exc_info=True)

    async def _projection_has_pending(self) -> bool:
        status = getattr(self.projector, "status", None)
        if not callable(status):
            return False
        try:
            report = await asyncio.to_thread(status)
            return int(report.get("pending_count", 0)) > 0
        except Exception:
            logger.debug("Could not read projection pending count", exc_info=True)
            return False

    async def _projection_loop(self) -> None:
        initial, maximum, _ = self._projection_retry_settings()
        retry_count = 0
        await self._record_projection_retry(0, None, "starting")
        self._projection_wake.set()
        try:
            while not self._projection_stop.is_set():
                await self._projection_wake.wait()
                self._projection_wake.clear()
                if self._projection_stop.is_set():
                    break
                self._projection_idle.clear()
                try:
                    await asyncio.to_thread(self.projector.project_pending)
                except Exception:
                    retry_count += 1
                    delay = min(maximum, initial * (2 ** min(retry_count - 1, 20)))
                    next_retry = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    await self._record_projection_retry(
                        retry_count, next_retry, "backoff"
                    )
                    logger.warning(
                        "Kanban projection failed; retrying in %.2fs while Codex remains active",
                        delay,
                        exc_info=True,
                    )
                    self._projection_idle.set()
                    try:
                        await asyncio.wait_for(self._projection_stop.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        self._projection_wake.set()
                else:
                    retry_count = 0
                    await self._record_projection_retry(0, None, "idle")
                    self._projection_idle.set()
                    if await self._projection_has_pending():
                        try:
                            await asyncio.wait_for(
                                self._projection_stop.wait(), timeout=initial
                            )
                        except asyncio.TimeoutError:
                            self._projection_wake.set()
        finally:
            await self._record_projection_retry(0, None, "stopped")
            self._projection_idle.set()

    def start_projection(self) -> None:
        """Start one autonomous drain/retry worker and immediately scan backlog."""

        if self.projector is None or self._projection_stop.is_set():
            return
        if self._projection_worker is None or self._projection_worker.done():
            self._projection_worker = asyncio.create_task(self._projection_loop())
        self._projection_idle.clear()
        self._projection_wake.set()

    def _schedule_projection(self) -> None:
        """Wake the read model without placing it on the execution path."""

        if self.projector is None:
            return
        self.start_projection()
        self._projection_idle.clear()
        self._projection_wake.set()

    async def wait_for_projection(self, timeout: float = 30.0) -> None:
        """Wait for the current projection attempt, not the persistent worker."""

        if self.projector is None:
            return
        await asyncio.wait_for(self._projection_idle.wait(), timeout=timeout)

    async def stop_projection(self) -> None:
        """Stop retry scheduling and settle the worker within a bounded timeout."""

        worker = self._projection_worker
        if worker is None:
            return
        self._projection_stop.set()
        self._projection_wake.set()
        _, _, timeout = self._projection_retry_settings()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
        except asyncio.TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            logger.warning("Timed out stopping Codex Kanban projection worker")
        finally:
            self._projection_worker = None

    def _event(
        self,
        request: BridgeRequest,
        phase: str,
        summary: str,
        *,
        step: str,
        progress: Mapping[str, Any] | None = None,
    ) -> ProgressEvent:
        public_progress = {"current_step": step}
        if progress:
            public_progress.update(progress)
        return ProgressEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            task_id=request.hermes_job_id,
            executor="codex",
            phase=phase,
            summary=summary[:500],
            progress=public_progress,
            origin=request.origin.as_dict(),
            created_at=_utc_now(),
            idempotency_key=request.idempotency_key,
        )

    async def _notify(
        self,
        notify: Callable[[ProgressEvent], Any], event: ProgressEvent
    ) -> None:
        try:
            result = notify(event)
            if inspect.isawaitable(result):
                await result
        finally:
            self._schedule_projection()

    async def execute(
        self,
        request: BridgeRequest,
        notify: Callable[[ProgressEvent], Any],
    ) -> str:
        capture = self.store.capture(
            request,
            owner_instance_id=self.instance_id,
            stale_recovery_seconds=self.settings.stale_recovery_seconds,
        )
        if not capture.should_execute:
            if capture.mapping.final_result:
                return capture.mapping.final_result
            if capture.mapping.phase == "needs_user":
                pending = self.store.get_latest_pending_question(
                    capture.mapping.hermes_job_id
                )
                if pending:
                    return pending.question
            return "Request này đã được capture và đang được Codex xử lý."

        captured = self._event(
            request,
            "captured",
            "Hermes Gateway đã nhận request.",
            step="capture",
        )
        self.store.append_event(captured)
        await self._notify(notify, captured)

        working = self._event(
            request,
            "working",
            "Đang resume Codex thread đã persist."
            if capture.mapping.codex_thread_id
            else "Đang tạo Codex thread cho workspace đã được xác thực.",
            step="codex_start",
        )
        self.store.append_event(working)
        await self._notify(notify, working)

        try:
            outcome = await self._invoke_executor(
                request,
                codex_thread_id=capture.mapping.codex_thread_id,
                notify=notify,
            )
        except CodexUserQuestion as exc:
            return await self._record_needs_user(
                request, notify, question=exc.question
            )
        except Exception as exc:
            if _needs_user_error(exc):
                return await self._record_needs_user(request, notify)
            event = self._event(
                request,
                "failed",
                f"Codex execution failed: {type(exc).__name__}: {str(exc)[:240]}",
                step="failed",
            )
            self.store.append_event(event)
            await self._notify(notify, event)
            return event.summary

        final = outcome.final_response
        output_ready = self._event(
            request,
            "output_ready",
            "Codex đã hoàn tất và kết quả sẵn sàng để trả về origin.",
            step="delivery",
            progress={"artifacts": list(outcome.artifacts)},
        )
        self.store.append_event(
            output_ready, final_result=final, artifacts=outcome.artifacts
        )
        await self._notify(notify, output_ready)
        done = self._event(request, "done", "Kết quả đã được route về đúng origin.", step="done")
        self.store.append_event(done, final_result=final, artifacts=outcome.artifacts)
        self._schedule_projection()
        return final

    async def _invoke_executor(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        notify: Callable[[ProgressEvent], Any],
    ) -> BridgeExecutionResult:
        loop = asyncio.get_running_loop()
        last_progress_step = "codex_start"
        emitted_action_updates = 0

        def on_thread(thread_id: str) -> None:
            self.store.set_thread_id(request.hermes_job_id, thread_id)

        def on_progress(step: str, summary: str) -> None:
            nonlocal emitted_action_updates, last_progress_step
            if step == last_progress_step:
                return
            # Phase 1 promises compact progress, not an unbounded tool trace.
            # Together with captured + initial working this caps ordinary
            # in-flight updates at four meaningful messages per execution.
            if emitted_action_updates >= 2:
                return
            last_progress_step = step
            emitted_action_updates += 1
            event = self._event(request, "working", summary, step=step)
            self.store.append_event(event)
            future = asyncio.run_coroutine_threadsafe(self._notify(notify, event), loop)
            future.result(timeout=30)

        result = await asyncio.to_thread(
            self.executor.execute,
            request,
            codex_thread_id=codex_thread_id,
            on_thread=on_thread,
            on_progress=on_progress,
        )
        if isinstance(result, BridgeExecutionResult):
            return result
        return BridgeExecutionResult(str(result))

    async def _record_needs_user(
        self,
        request: BridgeRequest,
        notify: Callable[[ProgressEvent], Any],
        *,
        reply_id: str | None = None,
        question: str | None = None,
    ) -> str:
        question = question or (
            "Codex cần đăng nhập hoặc quyền bổ sung trước khi có thể tiếp tục."
        )
        pending = self.store.create_pending_question(
            request.hermes_job_id, question, request.origin
        )
        event = self._event(
            request,
            "needs_user",
            question,
            step="user_action",
            progress={"prompt_id": pending.prompt_id},
        )
        self.store.append_event(event)
        if reply_id:
            self.store.update_reply(reply_id, "needs_user", question)
        await self._notify(notify, event)
        return event.summary

    async def resume_with_reply(
        self,
        reply: BridgeReply,
        notify: Callable[[ProgressEvent], Any],
    ) -> str:
        capture = self.store.capture_reply(
            reply,
            owner_instance_id=self.instance_id,
            stale_recovery_seconds=self.settings.stale_recovery_seconds,
        )
        if not capture.should_execute:
            if capture.mapping.final_result:
                return capture.mapping.final_result
            return "Reply này đã được capture và đang được Codex xử lý."

        request = BridgeRequest(
            hermes_job_id=capture.job.hermes_job_id,
            idempotency_key=reply.idempotency_key,
            origin=BridgeOrigin(**capture.mapping.origin),
            workspace=capture.job.workspace,
            prompt=capture.mapping.answer,
        )
        working = self._event(
            request,
            "working",
            "Hermes Gateway đã nhận reply và đang resume Codex thread đã persist.",
            step="codex_resume",
            progress={"prompt_id": reply.prompt_id},
        )
        self.store.append_event(working)
        self.store.update_reply(capture.mapping.reply_id, "working")
        await self._notify(notify, working)

        try:
            outcome = await self._invoke_executor(
                request,
                codex_thread_id=capture.job.codex_thread_id,
                notify=notify,
            )
        except CodexUserQuestion as exc:
            return await self._record_needs_user(
                request,
                notify,
                reply_id=capture.mapping.reply_id,
                question=exc.question,
            )
        except Exception as exc:
            if _needs_user_error(exc):
                return await self._record_needs_user(
                    request, notify, reply_id=capture.mapping.reply_id
                )
            event = self._event(
                request,
                "failed",
                f"Codex execution failed: {type(exc).__name__}: {str(exc)[:240]}",
                step="failed",
            )
            self.store.append_event(event)
            self.store.update_reply(capture.mapping.reply_id, "failed", event.summary)
            await self._notify(notify, event)
            return event.summary

        final = outcome.final_response
        output_ready = self._event(
            request,
            "output_ready",
            "Codex đã hoàn tất sau reply và kết quả sẵn sàng để trả về origin.",
            step="delivery",
            progress={"artifacts": list(outcome.artifacts)},
        )
        self.store.append_event(
            output_ready, final_result=final, artifacts=outcome.artifacts
        )
        await self._notify(notify, output_ready)
        done = self._event(
            request, "done", "Kết quả đã được route về đúng origin.", step="done"
        )
        self.store.append_event(done, final_result=final, artifacts=outcome.artifacts)
        self._schedule_projection()
        self.store.update_reply(capture.mapping.reply_id, "done", final)
        return final
