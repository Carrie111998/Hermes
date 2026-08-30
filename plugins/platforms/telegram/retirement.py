"""Ownership and cleanup for retired python-telegram-bot generations."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import logging
import threading
import weakref
from typing import Any, Awaitable, Optional, Set

from agent.deadline import run_bounded_async

logger = logging.getLogger(__name__)

MAX_RETIRED_GENERATIONS = 1
CAPACITY_WAIT_TIMEOUT = 5.0
CLEANUP_RETRY_DELAY = 0.5
FINAL_SHUTDOWN_DRAIN_TIMEOUT = 4.0


class RetirementCapacityError(RuntimeError):
    """A Telegram generation could not transfer into bounded retirement."""


def _registry_key(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _request_resources(app: Any) -> tuple[Any, ...]:
    """Capture PTB requests when public shutdown is initialization-gated."""
    bot = getattr(app, "bot", None)
    return tuple(getattr(bot, "_request", ()) or ())  # noqa: SLF001


@dataclasses.dataclass
class RetiredTelegramGeneration:
    generation_id: int
    app: Any
    updater: Any
    bot: Any
    requests: tuple[Any, ...]
    registry: "TelegramRetirementRegistry"
    state: str = "RETIRING"
    cleanup_task: Optional[asyncio.Task] = None
    active_tasks: Set[asyncio.Task] = dataclasses.field(default_factory=set)
    completed_steps: Set[str] = dataclasses.field(default_factory=set)
    retry_when_idle: bool = False
    retry_handle: Optional[asyncio.Handle] = None


_REGISTRIES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, TelegramRetirementRegistry]]" = weakref.WeakKeyDictionary()
_REGISTRIES_LOCK = threading.Lock()


class TelegramRetirementRegistry:
    """Own at most one retired PTB generation for one bot token."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        token_key: str,
        *,
        updater_stop_timeout: float,
        disconnect_step_timeout: float,
    ) -> None:
        self._loop_ref = weakref.ref(loop)
        self.token_key = token_key
        self.updater_stop_timeout = updater_stop_timeout
        self.disconnect_step_timeout = disconnect_step_timeout
        self.entries: list[RetiredTelegramGeneration] = []
        self._capacity_waiters: set[
            weakref.ReferenceType[asyncio.Future[None]]
        ] = set()

    def update_timeouts(
        self, *, updater_stop_timeout: float, disconnect_step_timeout: float
    ) -> None:
        self.updater_stop_timeout = updater_stop_timeout
        self.disconnect_step_timeout = disconnect_step_timeout

    def _loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop_ref()
        if loop is None or loop.is_closed():
            raise RuntimeError("Telegram retirement event loop is unavailable")
        return loop

    def find(self, app: Any) -> Optional[RetiredTelegramGeneration]:
        return next((entry for entry in self.entries if entry.app is app), None)

    def retire(
        self,
        app: Any,
        *,
        generation_id: int,
        start: bool = True,
    ) -> Optional[RetiredTelegramGeneration]:
        if app is None:
            return None
        existing = self.find(app)
        if existing is not None:
            if start:
                self.start(existing)
            return existing
        if len(self.entries) >= MAX_RETIRED_GENERATIONS:
            return None
        entry = RetiredTelegramGeneration(
            generation_id=generation_id,
            app=app,
            updater=getattr(app, "updater", None),
            bot=getattr(app, "bot", None),
            requests=_request_resources(app),
            registry=self,
        )
        self.entries.append(entry)
        if start:
            self.start(entry)
        return entry

    def start(self, entry: RetiredTelegramGeneration) -> None:
        task = entry.cleanup_task
        if task is not None and not task.done():
            return
        if entry.retry_handle is not None:
            entry.retry_handle.cancel()
            entry.retry_handle = None
        entry.cleanup_task = self._loop().create_task(
            self._cleanup(entry),
            name=f"telegram-retired-cleanup:{entry.generation_id}",
        )
        entry.cleanup_task.add_done_callback(
            lambda done, owned=entry: self._cleanup_done(owned, done)
        )

    def start_soon(self, entry: RetiredTelegramGeneration) -> None:
        self._loop().call_soon(self.start, entry)

    def track_external(
        self, entry: RetiredTelegramGeneration, task: asyncio.Task
    ) -> None:
        """Keep the exact abandoned lifecycle task under the owner record."""
        if task.done() or task in entry.active_tasks:
            return
        entry.active_tasks.add(task)

        def _forget(done: asyncio.Task) -> None:
            entry.active_tasks.discard(done)
            if (
                entry.retry_when_idle
                and not entry.active_tasks
                and entry.cleanup_task is not None
                and entry.cleanup_task.done()
                and entry.state != "CLEANED"
            ):
                self._schedule_retry(entry)

        task.add_done_callback(_forget)

    def _schedule_retry(self, entry: RetiredTelegramGeneration) -> None:
        if entry.state == "CLEANED" or entry.retry_handle is not None:
            return

        def _restart() -> None:
            entry.retry_handle = None
            if entry.state == "CLEANED" or entry.active_tasks:
                entry.retry_when_idle = True
                return
            entry.retry_when_idle = False
            entry.cleanup_task = None
            self.start(entry)

        try:
            entry.retry_handle = self._loop().call_later(
                CLEANUP_RETRY_DELAY, _restart
            )
        except RuntimeError:
            entry.retry_when_idle = True

    def _cleanup_done(
        self, entry: RetiredTelegramGeneration, task: asyncio.Task
    ) -> None:
        if entry.state == "CLEANED":
            self._wake_capacity_waiters()
            _remove_empty_registry(self)
            return
        if not task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass
        entry.retry_when_idle = True
        if not entry.active_tasks:
            self._schedule_retry(entry)

    async def _owned_step(
        self,
        entry: RetiredTelegramGeneration,
        awaitable: Awaitable[Any],
        *,
        label: str,
        timeout: float,
    ) -> bool:
        if label in entry.completed_steps:
            return True
        if not inspect.isawaitable(awaitable):
            entry.completed_steps.add(label)
            return True
        task = asyncio.ensure_future(awaitable)
        entry.active_tasks.add(task)

        def _forget(done: asyncio.Task) -> None:
            entry.active_tasks.discard(done)
            if not done.cancelled():
                try:
                    if done.exception() is None:
                        entry.completed_steps.add(label)
                except BaseException:
                    pass
            if (
                entry.retry_when_idle
                and not entry.active_tasks
                and entry.cleanup_task is not None
                and entry.cleanup_task.done()
                and entry.state != "CLEANED"
            ):
                self._schedule_retry(entry)

        task.add_done_callback(_forget)
        try:
            result = await run_bounded_async(
                task,
                timeout,
                label=f"telegram-retired:{label}",
            )
            if result.timed_out:
                entry.retry_when_idle = True
                logger.warning(
                    "Telegram retired generation %d %s exceeded %.1fs; "
                    "ownership retained",
                    entry.generation_id,
                    label,
                    timeout,
                )
                return False
            return True
        except BaseException as exc:
            logger.warning(
                "Telegram retired generation %d cleanup step %s failed: %s",
                entry.generation_id,
                label,
                type(exc).__name__,
            )
            return False

    async def _cleanup(self, entry: RetiredTelegramGeneration) -> None:
        entry.state = "RETIRING"
        if entry.active_tasks:
            entry.retry_when_idle = True
            return
        updater = entry.updater
        if (
            updater is not None
            and getattr(updater, "running", False)
            and "updater.stop()" not in entry.completed_steps
            and not await self._owned_step(
                entry,
                updater.stop(),
                label="updater.stop()",
                timeout=self.updater_stop_timeout,
            )
        ):
            return

        app = entry.app
        if (
            app is not None
            and getattr(app, "running", False)
            and "Application.stop()" not in entry.completed_steps
            and not await self._owned_step(
                entry,
                app.stop(),
                label="Application.stop()",
                timeout=self.disconnect_step_timeout,
            )
        ):
            return
        if (
            app is not None
            and "Application.shutdown()" not in entry.completed_steps
            and not await self._owned_step(
                entry,
                app.shutdown(),
                label="Application.shutdown()",
                timeout=self.disconnect_step_timeout,
            )
        ):
            return

        for request in entry.requests:
            shutdown = getattr(request, "shutdown", None)
            label = f"HTTPXRequest.shutdown:{id(request)}"
            if (
                callable(shutdown)
                and label not in entry.completed_steps
                and not await self._owned_step(
                    entry,
                    shutdown(),
                    label=label,
                    timeout=self.disconnect_step_timeout,
                )
            ):
                return

        if entry.active_tasks:
            await asyncio.gather(*tuple(entry.active_tasks), return_exceptions=True)
        entry.state = "CLEANED"
        if entry.retry_handle is not None:
            entry.retry_handle.cancel()
            entry.retry_handle = None
        entry.app = None
        entry.updater = None
        entry.bot = None
        entry.requests = ()
        if entry in self.entries:
            self.entries.remove(entry)

    def _wake_capacity_waiters(self) -> None:
        dead: set[weakref.ReferenceType[asyncio.Future[None]]] = set()
        for waiter_ref in self._capacity_waiters:
            waiter = waiter_ref()
            if waiter is None:
                dead.add(waiter_ref)
            elif not waiter.done():
                waiter.set_result(None)
        self._capacity_waiters.difference_update(dead)

    async def wait_for_entry(
        self, entry: RetiredTelegramGeneration, timeout: float
    ) -> bool:
        task = entry.cleanup_task
        if task is None:
            return entry.state == "CLEANED"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return entry.state == "CLEANED"

    async def wait_for_capacity(self, timeout: float = CAPACITY_WAIT_TIMEOUT) -> bool:
        if len(self.entries) < MAX_RETIRED_GENERATIONS:
            return True
        loop = asyncio.get_running_loop()
        if loop is not self._loop_ref():
            raise RuntimeError("Telegram retirement registry used from another loop")
        waiter: asyncio.Future[None] = loop.create_future()
        waiter_ref = weakref.ref(waiter)
        self._capacity_waiters.add(waiter_ref)
        try:
            if len(self.entries) < MAX_RETIRED_GENERATIONS:
                return True
            await asyncio.wait_for(asyncio.shield(waiter), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return False
        finally:
            self._capacity_waiters.discard(waiter_ref)
        return len(self.entries) < MAX_RETIRED_GENERATIONS

    async def drain(self, timeout: float) -> bool:
        if not self.entries:
            return True
        return await self.wait_for_capacity(timeout)


def get_retirement_registry(
    token: object,
    *,
    updater_stop_timeout: float,
    disconnect_step_timeout: float,
    create: bool = True,
) -> Optional[TelegramRetirementRegistry]:
    loop = asyncio.get_running_loop()
    key = _registry_key(token)
    with _REGISTRIES_LOCK:
        per_loop = _REGISTRIES.get(loop)
        registry = per_loop.get(key) if per_loop is not None else None
        if registry is None and create:
            if per_loop is None:
                per_loop = {}
                _REGISTRIES[loop] = per_loop
            registry = TelegramRetirementRegistry(
                loop,
                key,
                updater_stop_timeout=updater_stop_timeout,
                disconnect_step_timeout=disconnect_step_timeout,
            )
            per_loop[key] = registry
        elif registry is not None:
            registry.update_timeouts(
                updater_stop_timeout=updater_stop_timeout,
                disconnect_step_timeout=disconnect_step_timeout,
            )
        return registry


def _remove_empty_registry(registry: TelegramRetirementRegistry) -> None:
    if registry.entries:
        return
    loop = registry._loop_ref()
    if loop is None:
        return
    with _REGISTRIES_LOCK:
        per_loop = _REGISTRIES.get(loop)
        if (
            per_loop is None
            or per_loop.get(registry.token_key) is not registry
            or registry.entries
        ):
            return
        per_loop.pop(registry.token_key, None)
        if not per_loop:
            _REGISTRIES.pop(loop, None)


def registry_count(loop: Optional[asyncio.AbstractEventLoop] = None) -> int:
    loop = loop or asyncio.get_running_loop()
    with _REGISTRIES_LOCK:
        return len(_REGISTRIES.get(loop, {}))


async def drain_retired_generations(
    timeout: float = FINAL_SHUTDOWN_DRAIN_TIMEOUT,
) -> bool:
    """Drain all retired generations on this loop under one total deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    failed = False
    last_pending: tuple[TelegramRetirementRegistry, ...] = ()

    while True:
        with _REGISTRIES_LOCK:
            per_loop = _REGISTRIES.get(loop)
            registries = tuple(per_loop.values()) if per_loop is not None else ()
        pending = tuple(registry for registry in registries if registry.entries)
        if not pending:
            return not failed
        last_pending = pending
        for registry in pending:
            for entry in tuple(registry.entries):
                registry.start(entry)
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        results = await asyncio.gather(
            *(registry.drain(remaining) for registry in pending),
            return_exceptions=True,
        )
        for result in results:
            if result is not True:
                failed = True
                if isinstance(result, BaseException):
                    logger.error(
                        "Telegram global retired-generation drain failed: %s",
                        type(result).__name__,
                    )

    for registry in last_pending:
        for entry in tuple(registry.entries):
            logger.error(
                "Gateway shutdown budget ended with retired Telegram generation "
                "%d still owned (state=%s active_tasks=%d)",
                entry.generation_id,
                entry.state,
                len(entry.active_tasks),
            )
    return False


__all__ = [
    "CAPACITY_WAIT_TIMEOUT",
    "FINAL_SHUTDOWN_DRAIN_TIMEOUT",
    "RetiredTelegramGeneration",
    "RetirementCapacityError",
    "TelegramRetirementRegistry",
    "drain_retired_generations",
    "get_retirement_registry",
    "registry_count",
]
