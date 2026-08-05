"""Polling-resilience methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift that
produced ``gateway/authz_mixin.py``. This mixin owns the getUpdates polling
lifecycle: draining wedged connection pools, generation-scoped progress
tracking, the network-error reconnect ladder, the CLOSE-WAIT heartbeat probe,
and conflict (409) recovery.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO, and ``PollingMixin`` precedes
``BasePlatformAdapter`` in the bases so resolution order is what it was when
these methods sat on the class.

Two details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``. ``getLogger`` returns the same
  singleton object the adapter module holds.
* ``Update`` is imported under the same ``ImportError`` guard the adapter
  uses, falling back to ``Any``.
"""

import asyncio
import logging
import time
from contextvars import ContextVar
from typing import Any, Optional

from plugins.platforms.telegram.adapter import (
    _await_with_thread_deadline,
    _redact_telegram_error_text,
    _shutdown_abandoned_app,
    _UPDATER_STOP_TIMEOUT,
)

try:
    from telegram import Update
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Update = Any

# Bind the adapter's logger by name so log records lifted with these methods
# are emitted under exactly the name they were before.
logger = logging.getLogger("plugins.platforms.telegram.adapter")



async def _first_completed(*futures: "asyncio.Future") -> None:
    """Return when the first of ``futures`` completes.

    Used by the strict cold-start readiness gate to wait on "progress OR
    polling error", whichever fires first (#67498). Does not cancel the
    losers — the caller owns their lifecycle.
    """
    await asyncio.wait(set(futures), return_when=asyncio.FIRST_COMPLETED)


# start_polling() can also hang when the connection pool is in a degraded state
# after _drain_polling_connections(), particularly when both primary and fallback
# Telegram endpoints are unreachable. Bounding start_polling() prevents the
# reconnect ladder from stalling indefinitely and allows the heartbeat loop to
# trigger its own recovery path. Refs: NousResearch/hermes-agent#59614
_UPDATER_START_TIMEOUT = 30.0
# Initial connect is not healthy until the dedicated getUpdates request completes
# one successful round trip. Unlike reconnect, initial bootstrap must fail closed
# so GatewayRunner disposes the partial adapter and retries with a fresh PTB app.
_INITIAL_POLLING_PROGRESS_TIMEOUT = 60.0
# shutdown()/initialize() on the getUpdates httpx request close and rebuild the
# connection pool. When a connection is wedged on a stale CLOSE-WAIT socket that
# close can block forever, hanging _drain_polling_connections() and freezing the
# whole reconnect ladder (the tracked _polling_error_task never completes, so
# every escalation path stays gated behind its in-flight guard). Bound the drain
# so the ladder always advances toward the fatal-restart escalation. Matches
# _UPDATER_STOP_TIMEOUT. Refs: NousResearch/hermes-agent#66377
_DRAIN_TIMEOUT = 15.0
# Cause-agnostic wedged-recovery watchdog (#66377). Every recovery path (the
# reconnect ladder's re-entry, the pending-update probe, PTB's error callback)
# gates new recovery on ``_polling_error_task.done()``; if that task ever wedges
# on a hung await that no local bound covers, the whole gateway goes silently
# deaf with nothing retrying. The heartbeat loop force-escalates a recovery task
# that stays in-flight far longer than any healthy ladder attempt could take —
# stop (_UPDATER_STOP_TIMEOUT) + drain (2x_DRAIN_TIMEOUT) + start
# (_UPDATER_START_TIMEOUT) + max backoff (60s) is ~135s, so 300s is
# unambiguously stuck.
_POLLING_ERROR_TASK_STUCK_TIMEOUT = 300.0
# A generation is not healthy until the dedicated getUpdates request returns
# successfully. This exceeds a normal long-poll cycle for healthy idle bots.
_POLLING_PROGRESS_TIMEOUT = 60.0
_POLLING_GENERATION_CONTEXT: ContextVar[Optional[int]] = ContextVar(
    "telegram_polling_generation", default=None
)


def _adapter_timeout(name: str, default: float) -> float:
    """Resolve a timeout constant from the adapter module so tests that
    monkeypatch adapter.<name> (the pre-extraction binding) keep working."""
    try:
        from plugins.platforms.telegram import adapter as _adapter_mod
        return getattr(_adapter_mod, name, default)
    except Exception:
        return default

class _PollingLifecycleAbort(RuntimeError):
    """Internal control flow for polling startup fenced by teardown."""


class PollingMixin:
    """Polling-resilience cluster lifted verbatim from ``TelegramAdapter``."""

    async def _drain_polling_connections(self) -> None:
        """Reset the httpx connection pool used for getUpdates polling.

        Network errors (especially through proxies like sing-box) can leave
        httpx connections in a half-closed state that still occupy pool slots.
        After enough reconnect cycles the pool fills up entirely, causing
        ``Pool timeout: All connections in the connection pool are occupied.``

        We reset ONLY ``_request[0]`` (the getUpdates request) — the general
        request (``_request[1]``) is left untouched so concurrent
        ``send_message`` / ``edit_message`` calls are never interrupted.

        Implementation note: accesses ``Bot._request[0]`` which is the
        get-updates ``BaseRequest`` in the PTB 22.x internal tuple
        ``(get_updates_request, general_request)``.  There is no public
        accessor for the polling request; review if upgrading to PTB 23+.
        """
        if not (self._app and self._app.bot):
            return
        try:
            # PTB 22.x: _request is a (get_updates, general) tuple;
            # no public accessor exists for the polling request.
            polling_req = self._app.bot._request[0]  # noqa: SLF001
        except Exception:
            return
        try:
            # Bounded: a wedged CLOSE-WAIT socket can make this close hang
            # forever and freeze the reconnect ladder (#66377).
            await asyncio.wait_for(polling_req.shutdown(), timeout=_adapter_timeout("_DRAIN_TIMEOUT", _DRAIN_TIMEOUT))
        except Exception:
            logger.debug(
                "[%s] Polling request shutdown failed/timed out (non-fatal)",
                self.name, exc_info=True,
            )
        try:
            await asyncio.wait_for(polling_req.initialize(), timeout=_adapter_timeout("_DRAIN_TIMEOUT", _DRAIN_TIMEOUT))
            logger.debug(
                "[%s] Polling request pool drained before reconnect", self.name
            )
        except Exception:
            logger.debug(
                "[%s] Polling request re-initialize failed/timed out (non-fatal)",
                self.name, exc_info=True,
            )

    def _begin_polling_generation(self) -> tuple[int, asyncio.Event]:
        """Start accepting progress for a new getUpdates polling generation."""
        if getattr(self, "_polling_teardown_started", False):
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            progress = getattr(self, "_polling_progress_event", None)
            if progress is None:
                progress = asyncio.Event()
                self._polling_progress_event = progress
            return getattr(self, "_polling_generation", 0), progress

        verifier = getattr(self, "_polling_progress_verifier_task", None)
        if verifier is not None and not verifier.done():
            verifier.cancel()
        self._polling_progress_verifier_task = None
        self._polling_generation = getattr(self, "_polling_generation", 0) + 1
        self._polling_progress_event = asyncio.Event()
        self._polling_progress_accepting = True
        self._send_path_degraded = True
        return self._polling_generation, self._polling_progress_event

    def _record_polling_progress(self, generation: int) -> None:
        """Record successful getUpdates I/O for the current generation only."""
        if getattr(self, "_polling_teardown_started", False):
            return
        if not self._polling_progress_accepting:
            return
        if generation != self._polling_generation:
            return
        self._polling_progress_event.set()
        self._polling_network_error_count = 0
        if generation == self._polling_conflict_recovery_generation:
            self._polling_conflict_recovery_generation = None
        else:
            self._polling_conflict_count = 0
        self._send_path_degraded = False

    def _observe_polling_request_result(self, request, generation, result):
        """Record getUpdates progress from an observed do_request result.

        Purely observational: PTB still parses the untouched payload and owns
        any resulting exception. Kept as its own method so the observation
        logic is shared and independently testable.
        """
        status_code, payload = result
        if generation is None or not (200 <= status_code < 300):
            return
        try:
            # Use the request's own parser so health observation agrees
            # exactly with PTB's authoritative response handling (e.g.
            # UTF-8 replacement decoding and BOM rejection).
            envelope = request.parse_json_payload(payload)
        except Exception:
            return
        if (
            isinstance(envelope, dict)
            and envelope.get("ok") is True
            and "result" in envelope
        ):
            self._record_polling_progress(generation)

    def _instrument_polling_request(self, request):
        """Instrument one dedicated PTB getUpdates request with progress tracking.

        PTB's request classes (``BaseRequest`` / ``HTTPXRequest``) use
        ``__slots__``. On Python 3.13 their instances no longer carry a
        ``__dict__`` (the ``AbstractAsyncContextManager`` MRO stopped yielding
        one), so ``request.do_request = wrapper`` raises
        ``AttributeError: 'HTTPXRequest' object attribute 'do_request' is
        read-only`` and the whole Telegram connect fails (#64482). It only
        appeared to work on Python 3.12, where those instances still had a
        ``__dict__``.

        Instead of monkey-patching the instance, re-tag it to a thin subclass
        that overrides ``do_request``. This is portable across Python versions
        and works for both the real request and the test doubles. The subclass
        declares ``__slots__ = ()`` so its instance layout stays identical to
        the base, which is what makes the ``__class__`` swap legal on a slotted
        instance.
        """
        adapter = self
        base_cls = type(request)

        class _InstrumentedPollingRequest(base_cls):
            __slots__ = ()

            async def do_request(self, *args, **kwargs):
                generation = _POLLING_GENERATION_CONTEXT.get()
                result = await super().do_request(*args, **kwargs)
                adapter._observe_polling_request_result(self, generation, result)
                return result

        request.__class__ = _InstrumentedPollingRequest
        return request

    async def _start_polling_once(
        self,
        app,
        *,
        drop_pending_updates: bool,
        error_callback,
        abandon_app_on_timeout: bool = False,
        schedule_verifier: bool = True,
    ) -> tuple[int, asyncio.Event]:
        """Start one generation and verify real getUpdates progress.

        Returns the ``(generation, progress_event)`` pair created for this
        polling generation so callers that must gate on readiness (strict
        cold start, #67498) can bind to exactly this generation instead of
        re-reading ``self._polling_progress_event`` — which a concurrent
        recovery task may have replaced with a newer generation's event.
        """
        if getattr(self, "_polling_teardown_started", False):
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        generation, progress = self._begin_polling_generation()
        if not self._polling_progress_accepting:
            raise _PollingLifecycleAbort("Telegram polling teardown started")

        def _generation_error_callback(error: Exception) -> None:
            if getattr(self, "_polling_teardown_started", False):
                return
            if generation != self._polling_generation:
                return
            if error_callback is not None:
                callback_context_token = _POLLING_GENERATION_CONTEXT.set(None)
                try:
                    error_callback(error)
                finally:
                    _POLLING_GENERATION_CONTEXT.reset(callback_context_token)

        context_token = _POLLING_GENERATION_CONTEXT.set(generation)
        try:
            # asyncio.wait_for can wait forever for cancellation to escape
            # httpcore/AnyIO shielded scopes (#58236/#67498). Reuse the
            # proven wall-deadline helper and abandon the partial updater;
            # caller recovery will dispose/rebuild the whole adapter.
            await _await_with_thread_deadline(
                app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=drop_pending_updates,
                    error_callback=_generation_error_callback,
                ),
                timeout=_adapter_timeout("_UPDATER_START_TIMEOUT", _UPDATER_START_TIMEOUT),
                on_abandon=(
                    (lambda app=app: _shutdown_abandoned_app(app))
                    if abandon_app_on_timeout
                    else None
                ),
            )
        finally:
            _POLLING_GENERATION_CONTEXT.reset(context_token)
        if getattr(self, "_polling_teardown_started", False):
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        if schedule_verifier:
            self._schedule_polling_progress_verifier(generation, progress)
        return generation, progress

    def _schedule_polling_progress_verifier(
        self, generation: int, progress: asyncio.Event
    ) -> None:
        """Own exactly one tracked verifier for the current generation."""
        if getattr(self, "_polling_teardown_started", False):
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            return
        previous = getattr(self, "_polling_progress_verifier_task", None)
        if previous is not None and not previous.done():
            previous.cancel()

        task = asyncio.get_running_loop().create_task(
            self._verify_polling_after_reconnect(generation, progress)
        )
        self._polling_progress_verifier_task = task
        self._background_tasks.add(task)

        def _clear_finished_verifier(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            if self._polling_progress_verifier_task is finished:
                self._polling_progress_verifier_task = None

        task.add_done_callback(_clear_finished_verifier)

    def _get_general_request_drain_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_general_request_drain_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._general_request_drain_lock = lock
        return lock

    async def _drain_general_connections_after_pool_timeout(self) -> None:
        """Reset the Bot API request pool after a confirmed send pool timeout.

        ``send_message`` uses PTB's general request pool (``_request[1]``).
        When httpx reports that this pool is exhausted, PTB says the request
        was not sent, so it is safe to reset the wedged pool before retrying.
        """
        bot = getattr(getattr(self, "_app", None), "bot", None)
        if bot is None:
            bot = getattr(self, "_bot", None)
        if bot is None:
            return
        try:
            # PTB 22.x: _request is (get_updates_request, general_request).
            general_req = bot._request[1]  # noqa: SLF001
        except Exception:
            return
        async with self._get_general_request_drain_lock():
            try:
                await general_req.shutdown()
            except Exception:
                logger.debug(
                    "[%s] General request shutdown failed after pool timeout (non-fatal)",
                    self.name, exc_info=True,
                )
            try:
                await general_req.initialize()
                logger.warning(
                    "[%s] General request pool drained after Telegram pool timeout",
                    self.name,
                )
            except Exception:
                logger.debug(
                    "[%s] General request re-initialize failed after pool timeout (non-fatal)",
                    self.name, exc_info=True,
                )

    def _schedule_polling_recovery(self, error: Exception, *, reason: str) -> None:
        """Schedule polling recovery without failing gateway startup.

        A Telegram bootstrap failure (deleteWebhook / initial start_polling)
        caused by a transient network error should degrade only the Telegram
        adapter: the gateway process stays alive and the existing reconnect
        ladder (``_handle_polling_network_error``) recovers in the background.
        """
        if getattr(self, "_polling_teardown_started", False):
            return
        if self.has_fatal_error:
            return
        if self._polling_error_task and not self._polling_error_task.done():
            logger.debug(
                "[%s] Telegram polling recovery already scheduled; ignoring %s: %s",
                self.name, reason, _redact_telegram_error_text(error),
            )
            return
        self._send_path_degraded = True
        logger.warning(
            "[%s] Telegram polling degraded (%s); gateway stays alive and will retry. Error: %s",
            self.name, reason, _redact_telegram_error_text(error),
        )
        loop = asyncio.get_running_loop()
        self._polling_error_task = loop.create_task(self._handle_polling_network_error(error))
        self._background_tasks.add(self._polling_error_task)
        self._polling_error_task.add_done_callback(self._background_tasks.discard)

    async def _delete_webhook_best_effort(
        self, *, require_success: bool = False
    ) -> bool:
        """Clear stale webhook, optionally failing closed on initial connect.

        Reconnect can recover a transient error in background. Cold startup uses
        ``require_success`` so GatewayRunner disposes the partial adapter and
        retries with a fresh PTB Application instead of publishing degraded state.
        """
        if not self._bot:
            return False
        delete_webhook = getattr(self._bot, "delete_webhook", None)
        if not callable(delete_webhook):
            return True
        try:
            # Same shielded-cancellation class as initialize/start_polling:
            # never let a wedged duplicate deleteWebhook pin initial connect.
            await _await_with_thread_deadline(
                delete_webhook(drop_pending_updates=False),
                timeout=_adapter_timeout("_UPDATER_START_TIMEOUT", _UPDATER_START_TIMEOUT),
            )
            return True
        except Exception as err:
            if self._looks_like_network_error(err):
                if require_success:
                    raise OSError(
                        "Telegram deleteWebhook did not complete during initial connect"
                    ) from err
                logger.warning(
                    "[%s] deleteWebhook failed with a recoverable network error; "
                    "continuing to polling so getUpdates/retry can recover: %s",
                    self.name, _redact_telegram_error_text(err),
                )
                self._send_path_degraded = True
                return False
            raise

    async def _start_polling_resilient(
        self,
        *,
        drop_pending_updates: bool,
        error_callback,
        require_progress: bool = False,
    ) -> bool:
        """Start PTB polling and optionally require real getUpdates readiness.

        Reconnects may recover in background. Initial connect sets
        ``require_progress`` so a bootstrap failure or missing first successful
        getUpdates response raises; GatewayRunner then disposes this partial
        adapter and retries with a fresh PTB Application.
        """
        if getattr(self, "_polling_teardown_started", False):
            return False
        if not (self._app and self._app.updater):
            raise RuntimeError("Telegram application/updater not initialized")

        # Strict cold start (#67498): background recovery must not run while
        # the readiness gate is waiting. A G1 polling error would otherwise
        # schedule _handle_polling_network_error(), which starts generation
        # G2 on the same partial application while this coroutine still waits
        # on G1's event — the cold connect then either times out on G1 despite
        # G2 succeeding, or G2 "heals" the partial app so GatewayRunner never
        # disposes it and retries fresh. Instead, capture the first polling
        # error and fail the cold attempt immediately; GatewayRunner owns
        # disposal and retry with a fresh adapter.
        strict_error: list[BaseException] = []
        strict_error_event = asyncio.Event()
        strict_gate_open = True
        effective_callback = error_callback
        if require_progress:
            loop = asyncio.get_running_loop()

            def _strict_error_callback(error: Exception) -> None:
                # PTB registers this callback for the whole polling
                # generation. After the readiness gate closes (success),
                # delegate to the real callback so ongoing polling errors
                # keep flowing into background recovery.
                if not strict_gate_open:
                    if error_callback is not None:
                        error_callback(error)
                    return
                if not strict_error:
                    strict_error.append(error)
                # PTB invokes error callbacks from the polling task; the
                # event must be set on the loop to wake the strict waiter.
                loop.call_soon_threadsafe(strict_error_event.set)

            effective_callback = _strict_error_callback
        try:
            # Same watchdog bound as the reconnect ladders: a wedged httpx
            # connection pool can hang start_polling() forever at bootstrap
            # too (#59614). A propagating TimeoutError is a builtins
            # TimeoutError (OSError subclass), so the except below classifies
            # it via _looks_like_network_error and schedules background
            # recovery instead of blocking connect() indefinitely.
            generation, progress = await self._start_polling_once(
                self._app,
                drop_pending_updates=drop_pending_updates,
                error_callback=effective_callback,
                abandon_app_on_timeout=require_progress,
                # The strict gate below IS the cold-start verifier; the
                # background verifier would only race it on the partial app.
                schedule_verifier=not require_progress,
            )
            if require_progress:
                # Bind to THIS generation's progress event (returned above),
                # not self._polling_progress_event — a concurrent task could
                # have replaced it with a later generation's event.
                progress_wait = asyncio.ensure_future(progress.wait())
                error_wait = asyncio.ensure_future(strict_error_event.wait())
                try:
                    await _await_with_thread_deadline(
                        _first_completed(progress_wait, error_wait),
                        timeout=_adapter_timeout("_INITIAL_POLLING_PROGRESS_TIMEOUT", _INITIAL_POLLING_PROGRESS_TIMEOUT),
                    )
                except asyncio.TimeoutError as exc:
                    raise OSError(
                        "Telegram getUpdates made no progress within "
                        f"{_adapter_timeout('_INITIAL_POLLING_PROGRESS_TIMEOUT', _INITIAL_POLLING_PROGRESS_TIMEOUT):.0f}s during initial "
                        "connect — failing startup so the gateway retries with a "
                        "fresh adapter (#67498)"
                    ) from exc
                finally:
                    for fut in (progress_wait, error_wait):
                        if not fut.done():
                            fut.cancel()
                    await asyncio.gather(
                        progress_wait, error_wait, return_exceptions=True
                    )
                if strict_error and not progress.is_set():
                    raise OSError(
                        "Telegram polling errored before first getUpdates "
                        "success during initial connect: "
                        f"{_redact_telegram_error_text(strict_error[0])}"
                    ) from strict_error[0]
                if not progress.is_set():
                    raise OSError(
                        "Telegram getUpdates did not become ready during initial connect"
                    )
                # Readiness proven — close the strict gate so any later
                # polling error flows to the real background-recovery
                # callback instead of the (now finished) cold-start gate.
                strict_gate_open = False
                self._polling_error_callback_ref = error_callback
            return True
        except _PollingLifecycleAbort:
            return False
        except Exception as err:
            if getattr(self, "_polling_teardown_started", False):
                return False
            if require_progress:
                raise
            if self._looks_like_polling_conflict(err):
                logger.warning(
                    "[%s] Telegram polling bootstrap conflict; gateway stays alive "
                    "while conflict retry runs: %s",
                    self.name, _redact_telegram_error_text(err),
                )
                loop = asyncio.get_running_loop()
                self._polling_error_task = loop.create_task(self._handle_polling_conflict(err))
                self._background_tasks.add(self._polling_error_task)
                self._polling_error_task.add_done_callback(self._background_tasks.discard)
                return False
            if self._looks_like_network_error(err):
                self._schedule_polling_recovery(err, reason="polling bootstrap")
                return False
            raise

    async def _handle_polling_network_error(self, error: Exception) -> None:
        """Reconnect polling after a transient network interruption.

        Triggered by NetworkError/TimedOut in the polling error callback, which
        happen when the host loses connectivity (Mac sleep, WiFi switch, VPN
        reconnect, etc.).  The gateway process stays alive but the long-poll
        connection silently dies; without this handler the bot never recovers.

        Strategy: exponential back-off (5s, 10s, 20s, 40s, 60s cap) up to
        MAX_NETWORK_RETRIES attempts, then mark the adapter retryable-fatal so
        the supervisor restarts the gateway process.
        """
        if getattr(self, "_polling_teardown_started", False):
            return
        if self.has_fatal_error:
            return

        MAX_NETWORK_RETRIES = 10
        BASE_DELAY = 5
        MAX_DELAY = 60

        self._polling_network_error_count += 1
        self._send_path_degraded = True
        attempt = self._polling_network_error_count

        if attempt > MAX_NETWORK_RETRIES:
            message = (
                "Telegram polling could not reconnect after %d network error retries. "
                "Escalating to gateway recovery." % MAX_NETWORK_RETRIES
            )
            logger.error("[%s] %s Last error: %s", self.name, message, _redact_telegram_error_text(error))
            self._set_fatal_error("telegram_network_error", message, retryable=True)
            await self._handoff_polling_fatal_error()
            return

        delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
        safe_error = _redact_telegram_error_text(error)
        logger.warning(
            "[%s] Telegram network error (attempt %d/%d), reconnecting in %ds. Error: %s",
            self.name, attempt, MAX_NETWORK_RETRIES, delay, safe_error,
        )
        await asyncio.sleep(delay)

        if getattr(self, "_polling_teardown_started", False):
            return

        # Capture a stable local reference: self._app can be reassigned to None
        # by a concurrent disconnect() while we're suspended across the awaits
        # below, and re-reading self._app after that point would silently swap
        # in None mid-sequence instead of failing fast in one place.
        app = self._app

        try:
            if app and app.updater and app.updater.running:
                try:
                    # Guard stop() with a timeout: when the underlying TCP
                    # connection is in CLOSE-WAIT the PTB polling task is
                    # blocked on epoll on the dead socket and never wakes up,
                    # so an unguarded stop() hangs indefinitely.  The result
                    # is that _polling_error_task stays alive-but-blocked
                    # forever, every subsequent heartbeat probe sees it as
                    # "in-flight" and skips triggering a new reconnect, and
                    # the gateway silently drops messages for hours.
                    # Bounding stop() lets the reconnect ladder always advance.
                    # Refs: NousResearch/hermes-agent#58270
                    await asyncio.wait_for(app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] updater.stop() timed out during network-error "
                        "reconnect (likely CLOSE-WAIT socket); forcing drain "
                        "and restart without clean stop",
                        self.name,
                    )
        except Exception:
            pass

        if getattr(self, "_polling_teardown_started", False):
            return
        await self._drain_polling_connections()

        if getattr(self, "_polling_teardown_started", False):
            return

        try:
            if not app:
                raise RuntimeError("Telegram application was torn down during reconnect")
            await self._start_polling_once(
                app,
                drop_pending_updates=False,
                error_callback=self._polling_error_callback_ref,
            )
            logger.info(
                "[%s] Telegram polling restarted after network error (attempt %d); "
                "health pending getUpdates progress",
                self.name, attempt,
            )
        except _PollingLifecycleAbort:
            return
        except Exception as retry_err:
            if getattr(self, "_polling_teardown_started", False):
                return
            safe_retry_error = _redact_telegram_error_text(retry_err)
            logger.warning("[%s] Telegram polling reconnect failed: %s", self.name, safe_retry_error)
            # start_polling failed — polling is dead and no further error
            # callbacks will fire, so schedule the next retry ourselves.
            if (
                not self.has_fatal_error
                and not getattr(self, "_polling_teardown_started", False)
            ):
                task = asyncio.ensure_future(
                    self._handle_polling_network_error(retry_err)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                # This chained retry IS the in-flight recovery attempt — it
                # must replace the reentrancy guard, otherwise the heartbeat
                # loop, the pending-updates probe, and the PTB error callback
                # all see _polling_error_task as "done" and can each start a
                # second, concurrent recovery for the same outage.
                self._polling_error_task = task

    async def _polling_heartbeat_loop(self) -> None:
        """Detect dead Telegram TCP sockets (CLOSE-WAIT) by periodic probing.

        PTB's long-poll task blocks on epoll waiting for Telegram to push an
        update.  When the underlying TCP connection enters CLOSE-WAIT (the remote
        sent a FIN but the httpx pool has not yet noticed), epoll still reports
        the socket as readable and no exception is raised — so PTB's
        ``error_callback`` never fires and the gateway silently stops receiving
        messages.

        This loop probes ``get_me()`` every ``HEARTBEAT_INTERVAL`` seconds on the
        *general* request path (not the getUpdates pool), so a healthy long-poll
        waiting for the 30-second Telegram window is never interrupted.  On any
        connect-level failure the loop hands off to
        ``_handle_polling_network_error`` — the same path triggered by PTB's own
        ``error_callback`` — which drains the dead pool and restarts polling.

        Unlike the generation verifier (a one-shot progress deadline after
        every polling start), this loop runs for the full lifetime of the
        polling connection, so it catches a socket that wedges later during
        steady-state operation without any prior error event.
        """
        HEARTBEAT_INTERVAL = 90   # seconds between probes
        PROBE_TIMEOUT = 15        # seconds before declaring the path dead

        # Wedged-recovery watchdog state (#66377). Tracked locally so no
        # _polling_error_task assignment site needs to stamp a timestamp: the
        # heartbeat notes when it first observes a given recovery task still
        # in-flight, and force-escalates if the *same* task object is still
        # running after _POLLING_ERROR_TASK_STUCK_TIMEOUT. A healthy ladder
        # attempt completes (task done) or chains to a new task well before
        # then, so a single long-lived task is unambiguously wedged.
        stuck_task_ref: Optional[asyncio.Task] = None
        stuck_task_since = 0.0

        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if getattr(self, "_polling_teardown_started", False):
                    return
                if self.has_fatal_error:
                    return

                # Independent wedged-recovery watchdog (#66377): if the tracked
                # recovery task has hung (any await no local bound covers), every
                # other recovery path is gated behind it and returns early
                # forever — the gateway stays alive but deaf. Force a
                # retryable-fatal so the background reconnector rebuilds the
                # adapter instead of relying on the frozen ladder.
                recovery_task = self._polling_error_task
                if recovery_task is not None and not recovery_task.done():
                    now = time.monotonic()
                    if recovery_task is not stuck_task_ref:
                        stuck_task_ref = recovery_task
                        stuck_task_since = now
                    elif now - stuck_task_since > _POLLING_ERROR_TASK_STUCK_TIMEOUT:
                        stuck_for = now - stuck_task_since
                        logger.error(
                            "[%s] Telegram reconnect task wedged for %.0fs with no "
                            "ladder progress; forcing retryable-fatal so the gateway "
                            "reconnects instead of staying silently deaf.",
                            self.name, stuck_for,
                        )
                        try:
                            recovery_task.cancel()
                        except Exception:
                            pass
                        self._set_fatal_error(
                            "telegram_network_error",
                            "Telegram reconnect task wedged for %.0fs; forcing "
                            "gateway reconnect." % stuck_for,
                            retryable=True,
                        )
                        await self._handoff_polling_fatal_error()
                        return
                else:
                    stuck_task_ref = None

                bot = self._app.bot if self._app else None
                if bot is None:
                    continue
                # A real PTB Bot always exposes get_me(); if it's absent the
                # app isn't a live polling client (e.g. torn down or a test
                # double), so there is nothing to probe — exit rather than spin.
                if not callable(getattr(bot, "get_me", None)):
                    return
                await asyncio.wait_for(bot.get_me(), PROBE_TIMEOUT)
                # get_me() refreshes PTB's cached bot user in place, so this is
                # also where a BotFather rename gets picked up: adopt whatever
                # handle Telegram just reported before anything routes on it.
                self._bot_identity_checked_at = time.monotonic()
                self._note_bot_username(getattr(bot, "username", None))
                # get_me() succeeded — the general/send request path is healthy.
                # That does NOT prove the getUpdates consumer is alive: PTB can
                # report updater.running=True while the long-poll task is wedged,
                # so DMs queue in the Bot API and never reach handlers (#42909).
                # get_me() is blind to this; get_webhook_info() exposes it via
                # pending_update_count. Escalate only after two consecutive
                # probes see a non-zero queue while we believe we're polling, so
                # a single in-flight update (consumed before the next probe)
                # never trips recovery.
                await self._probe_pending_updates(bot, PROBE_TIMEOUT)
            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, OSError) as probe_err:
                self._schedule_polling_recovery(probe_err, reason="heartbeat probe")
            except Exception as probe_err:
                if self._looks_like_network_error(probe_err):
                    self._schedule_polling_recovery(probe_err, reason="heartbeat probe")
                    continue
                # Non-connectivity errors (e.g. TelegramError 401) are not
                # CLOSE-WAIT symptoms — let PTB's own handlers surface them.
                pass

    async def _probe_pending_updates(self, bot, probe_timeout: float) -> None:
        """Detect a wedged getUpdates consumer via pending_update_count.

        PTB can report ``updater.running == True`` while its long-poll task is
        silently stuck (e.g. a socket that epoll keeps reporting readable on
        WSL2). ``get_me()`` stays healthy because it uses the general request
        path, so the CLOSE-WAIT heartbeat never fires — yet DMs queue in the
        Bot API and never reach handlers (#42909).

        ``get_webhook_info().pending_update_count`` is the one signal that
        exposes this: a growing/stuck queue while we believe we're polling means
        the consumer is dead. We only escalate after two consecutive stuck
        probes so a single update that's simply in-flight between probes does
        not trip a needless recovery. Recovery reuses
        ``_handle_polling_network_error`` — the same ladder PTB's own
        ``error_callback`` feeds — so no new restart machinery is introduced.

        This also covers the harsher case where the updater has stopped
        entirely (``running=False``) with no reconnect in flight: the long-poll
        task is gone rather than wedged, so even ``get_webhook_info`` can't
        report a queue against a live consumer. We detect the stopped updater
        directly and feed the same ladder (#55769).
        """
        if getattr(self, "_polling_teardown_started", False):
            return
        # Only meaningful in polling mode; in webhook mode Telegram pushes
        # updates and holds no server-side queue.
        if self._webhook_mode:
            return
        # A reconnect already in flight owns recovery — don't double-trigger,
        # and don't misread its brief stop()->start_polling() window (where
        # updater.running is transiently False) as a dead updater below.
        if self._polling_error_task and not self._polling_error_task.done():
            self._polling_not_running_count = 0
            return
        updater = getattr(self._app, "updater", None) if self._app else None
        if updater is None:
            self._polling_pending_stuck_count = 0
            return
        if not getattr(updater, "running", False):
            # We are in polling mode with no reconnect in flight, yet PTB's
            # updater has stopped entirely. This is distinct from the
            # wedged-but-running consumer handled below: the long-poll task is
            # gone, get_me()/get_webhook_info() on the general request path
            # still succeed, so no error_callback or connectivity probe ever
            # fires and the gateway silently stops receiving messages while the
            # process stays alive (#55769). Escalate through the same reconnect
            # ladder as a wedged consumer, debounced over two consecutive probes
            # so a just-starting updater never trips it.
            self._polling_pending_stuck_count = 0
            self._polling_not_running_count += 1
            logger.warning(
                "[%s] Telegram polling heartbeat: updater stopped while in "
                "polling mode (stuck probe %d/2)",
                self.name, self._polling_not_running_count,
            )
            if self._polling_not_running_count >= 2:
                self._polling_not_running_count = 0
                if getattr(self, "_polling_teardown_started", False):
                    return
                logger.warning(
                    "[%s] Telegram updater is not running (long-poll task "
                    "gone); triggering polling restart",
                    self.name,
                )
                loop = asyncio.get_running_loop()
                self._polling_error_task = loop.create_task(
                    self._handle_polling_network_error(
                        RuntimeError("Telegram updater stopped while in polling mode")
                    )
                )
            return
        self._polling_not_running_count = 0
        get_webhook_info = getattr(bot, "get_webhook_info", None)
        if not callable(get_webhook_info):
            return
        try:
            info = await asyncio.wait_for(get_webhook_info(), probe_timeout)  # type: ignore[arg-type]
        except (asyncio.TimeoutError, OSError):
            # A failed probe is a connectivity symptom the get_me() path or the
            # outer handler will catch; don't treat it as a stuck-queue signal.
            return
        pending = int(getattr(info, "pending_update_count", 0) or 0)
        if pending <= 0:
            self._polling_pending_stuck_count = 0
            return
        self._polling_pending_stuck_count += 1
        logger.warning(
            "[%s] Telegram polling heartbeat: %d update(s) queued but not "
            "consumed (stuck probe %d/2)",
            self.name, pending, self._polling_pending_stuck_count,
        )
        if self._polling_pending_stuck_count >= 2:
            self._polling_pending_stuck_count = 0
            if getattr(self, "_polling_teardown_started", False):
                return
            logger.warning(
                "[%s] getUpdates consumer appears wedged (queue not draining); "
                "triggering polling restart",
                self.name,
            )
            loop = asyncio.get_running_loop()
            self._polling_error_task = loop.create_task(
                self._handle_polling_network_error(
                    RuntimeError("getUpdates consumer wedged: pending updates not draining")
                )
            )

    async def _verify_polling_after_reconnect(
        self,
        generation: Optional[int] = None,
        progress: Optional[asyncio.Event] = None,
    ) -> None:
        """Require getUpdates progress, using getMe only to classify failure.

        The generation-bound event is set only by a successful response on the
        dedicated getUpdates request. A general-path getMe success can classify
        connectivity, but cannot heal polling health. Connectivity failures
        enter the guarded recovery ladder; auth/validation errors do not churn.
        """
        PROBE_TIMEOUT = 10
        if getattr(self, "_polling_teardown_started", False):
            return
        if generation is None:
            generation = self._polling_generation
        if progress is None:
            progress = self._polling_progress_event

        try:
            await asyncio.wait_for(
                progress.wait(), timeout=_adapter_timeout("_POLLING_PROGRESS_TIMEOUT", _POLLING_PROGRESS_TIMEOUT)
            )
        except asyncio.TimeoutError:
            pass

        if getattr(self, "_polling_teardown_started", False):
            return
        if progress.is_set() or self.has_fatal_error:
            return
        if not self._polling_progress_accepting:
            return
        if generation != self._polling_generation:
            return
        if progress is not self._polling_progress_event:
            return

        app = self._app
        if not (app and app.updater and app.updater.running):
            logger.warning(
                "[%s] Updater made no getUpdates progress and is not running",
                self.name,
            )
            self._schedule_polling_recovery(
                RuntimeError("Updater not running after polling progress deadline"),
                reason="polling progress verifier: updater not running",
            )
            return

        try:
            await asyncio.wait_for(app.bot.get_me(), PROBE_TIMEOUT)
        except Exception as probe_err:
            if getattr(self, "_polling_teardown_started", False):
                return
            if self.has_fatal_error or not self._polling_progress_accepting:
                return
            if generation != self._polling_generation:
                return
            if progress is not self._polling_progress_event or progress.is_set():
                return
            if not self._looks_like_network_error(probe_err):
                logger.warning(
                    "[%s] Polling progress verifier hit a non-connectivity error"
                    " (not retrying): %s",
                    self.name, _redact_telegram_error_text(probe_err),
                )
                return
            logger.warning(
                "[%s] Polling progress verifier connectivity probe failed: %s",
                self.name, _redact_telegram_error_text(probe_err),
            )
            self._schedule_polling_recovery(
                probe_err,
                reason="polling progress verifier connectivity failure",
            )
            return

        if getattr(self, "_polling_teardown_started", False):
            return
        if self.has_fatal_error or not self._polling_progress_accepting:
            return
        if generation != self._polling_generation:
            return
        if progress is not self._polling_progress_event or progress.is_set():
            return
        self._schedule_polling_recovery(
            RuntimeError("getUpdates made no progress before verifier deadline"),
            reason="polling progress verifier: general path healthy but getUpdates stalled",
        )

    def _disarm_ptb_retry_loop(self) -> None:
        """Synchronously stop PTB's internal polling retry loop.

        PTB wraps ``getUpdates`` in ``network_retry_loop`` with
        ``max_retries=-1`` (retry forever).  When a ``TelegramError`` (including
        a 409 ``Conflict``) fires, that loop calls our ``error_callback``
        *synchronously*, then sleeps and re-checks ``while is_running()`` before
        polling again.  Our ``error_callback`` only schedules an async recovery
        task (``loop.create_task(...)``) and returns immediately, so PTB's loop
        keeps polling while our handler concurrently runs
        ``stop -> sleep -> start_polling``.  The two polling sessions overlap and
        Telegram returns a fresh 409 — a self-inflicted conflict loop on a
        ~31s cadence.

        The loop is wired with ``is_running=lambda: updater.running`` and a
        private ``stop_event`` (``do_action`` races that event and returns the
        moment it is set).  Setting that event *synchronously inside the
        callback* — before it returns — makes PTB's loop exit on its own next
        tick instead of racing our recovery.  Our async handler then performs
        the real ``await updater.stop()`` (idempotent) followed by
        drain + ``start_polling()``, which builds a fresh ``stop_event`` so the
        restart is not poisoned.

        Best-effort and defensive: PTB names the attribute differently across
        versions (``_Updater__polling_task_stop_event`` via name-mangling), so
        we probe for both spellings.  If neither is found we do nothing and
        fall back to the prior behaviour (async ``updater.stop()`` racing PTB) —
        i.e. we never make things worse than before.

        We deliberately do NOT fall back to flipping ``updater._running``:
        ``stop()`` raises ``RuntimeError`` when ``running`` is already False and
        our recovery handler guards its ``stop()`` call on ``running``, so
        clearing the flag here would skip the real teardown and leave PTB's
        stop_event uncleared — poisoning the subsequent ``start_polling()``.
        The stop_event lever leaves ``_running`` True, so the handler's
        ``await updater.stop()`` still runs, drains the polling task, and clears
        the event for a clean restart.
        """
        updater = getattr(self._app, "updater", None) if self._app else None
        if updater is None:
            return
        # Preferred (and only) lever: PTB's polling stop_event. Name-mangled on
        # Updater, so probe both the mangled and unmangled spellings.
        for attr in (
            "_Updater__polling_task_stop_event",
            "_polling_task_stop_event",
        ):
            stop_event = getattr(updater, attr, None)
            if isinstance(stop_event, asyncio.Event):
                if not stop_event.is_set():
                    stop_event.set()
                    logger.debug(
                        "[%s] Disarmed PTB polling retry loop via %s",
                        self.name, attr,
                    )
                return
        logger.debug(
            "[%s] Could not disarm PTB polling retry loop "
            "(stop_event not found on this PTB version); "
            "falling back to async stop()",
            self.name,
        )

    async def _handle_polling_conflict(self, error: Exception) -> None:
        if getattr(self, "_polling_teardown_started", False):
            return
        if self.has_fatal_error and self.fatal_error_code == "telegram_polling_conflict":
            return
        # Transient 409 Conflict errors arise when the previous gateway process
        # has been killed (e.g. during `hermes update` or `--replace` handoffs)
        # but its long-poll connection hasn't yet expired on Telegram's servers.
        # Telegram holds open getUpdates sessions for up to ~30s after the
        # client disconnects, so a new gateway starting immediately will receive
        # a 409 until that server-side session expires.
        #
        # Strategy: stop the local updater, wait long enough for Telegram's
        # server-side session to expire (RETRY_DELAY grows with each attempt),
        # drain the connection pool, then restart polling.  We attempt this
        # MAX_CONFLICT_RETRIES times before declaring a fatal error.
        #
        # Crucially, a failed retry must NOT leave polling in an ambiguous
        # state.  If start_polling() raises, the updater is neither running
        # nor fatal — messages are silently dropped.  We schedule another
        # retry attempt instead of returning silently, and only escalate to
        # fatal after all retries are exhausted.
        self._polling_conflict_count += 1

        MAX_CONFLICT_RETRIES = 5
        # Delay grows with each attempt: 15s, 25s, 35s, 45s, 55s.
        # Telegram server-side getUpdates sessions typically expire within
        # 30s; the increasing back-off ensures we clear that window without
        # hammering the API on fast-restart loops.
        RETRY_DELAY = 10 + (self._polling_conflict_count * 10)  # seconds

        if self._polling_conflict_count <= MAX_CONFLICT_RETRIES:
            logger.warning(
                "[%s] Telegram polling conflict (%d/%d) — previous session still "
                "held open on Telegram's servers. Waiting %ds for it to expire. "
                "Error: %s",
                self.name, self._polling_conflict_count, MAX_CONFLICT_RETRIES,
                RETRY_DELAY, _redact_telegram_error_text(error),
            )
            # Stop the local updater cleanly before sleeping.  If it's already
            # stopped (e.g. PTB raised before updater.running was set) this is
            # a no-op.  Bounded with a timeout for the same reason as the
            # network-error path: a CLOSE-WAIT socket can wedge stop() on epoll
            # forever, which would stall the conflict-retry ladder.
            try:
                if self._app and self._app.updater and self._app.updater.running:
                    try:
                        await asyncio.wait_for(self._app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] updater.stop() timed out during conflict "
                            "retry (likely CLOSE-WAIT socket); continuing",
                            self.name,
                        )
            except Exception:
                pass

            await asyncio.sleep(RETRY_DELAY)
            if getattr(self, "_polling_teardown_started", False):
                return
            await self._drain_polling_connections()
            if getattr(self, "_polling_teardown_started", False):
                return

            # Capture a stable local reference: self._app can be reassigned to
            # None by a concurrent disconnect() while we're suspended across
            # the awaits above (same race #55992 fixed on the network path).
            # Re-reading self._app after that point would raise
            # AttributeError deep inside start_polling instead of failing fast
            # here, where the except below reschedules or escalates to fatal.
            app = self._app
            expected_generation = self._polling_generation + 1
            if not app:
                raise RuntimeError("Telegram application was torn down during conflict reconnect")
            # drop_pending_updates=True tells Telegram to terminate any
            # other active getUpdates sessions for this bot token.  The
            # competing session is either a zombie from the previous
            # gateway process (whose long-poll hasn't expired server-side
            # yet) or our own previous retry's still-expiring session.
            # Without this, each retry starts a new getUpdates session
            # that immediately gets 409'd by the previous one, creating
            # the very conflict we are trying to recover from (#75017).
            self._polling_conflict_recovery_generation = expected_generation
            try:
                await self._start_polling_once(
                    app,
                    drop_pending_updates=True,
                    error_callback=self._polling_error_callback_ref,
                )
                logger.info(
                    "[%s] Telegram polling restarted after conflict retry %d/%d; "
                    "health pending getUpdates progress",
                    self.name, self._polling_conflict_count, MAX_CONFLICT_RETRIES,
                )
                return
            except _PollingLifecycleAbort:
                return
            except Exception as retry_err:
                if getattr(self, "_polling_teardown_started", False):
                    return
                logger.warning(
                    "[%s] Telegram polling retry %d/%d failed: %s. "
                    "Scheduling next attempt.",
                    self.name, self._polling_conflict_count, MAX_CONFLICT_RETRIES,
                    _redact_telegram_error_text(retry_err),
                )
                # Schedule the next retry rather than returning silently.
                # Returning here without either restarting polling or setting
                # a fatal error leaves the adapter in a limbo state: the
                # gateway process is alive and reports "connected" but
                # no messages are received or sent.
                if (
                    self._polling_conflict_count < MAX_CONFLICT_RETRIES
                    and not getattr(self, "_polling_teardown_started", False)
                ):
                    # We are inside a running coroutine, so the running loop is
                    # guaranteed to exist. asyncio.get_event_loop() is deprecated
                    # and raises "RuntimeError: There is no current event loop in
                    # thread 'MainThread'" on Python 3.10+ when invoked from a
                    # context without an attached loop (which can happen when PTB
                    # dispatches this error callback). Use get_running_loop().
                    loop = asyncio.get_running_loop()
                    self._polling_error_task = loop.create_task(
                        self._handle_polling_conflict(retry_err)
                    )
                    return
                # Fall through to fatal on the last retry.
            finally:
                if self._polling_conflict_recovery_generation == expected_generation:
                    self._polling_conflict_recovery_generation = None

        if getattr(self, "_polling_teardown_started", False):
            return

        # Exhausted all retries — declare a fatal error so the gateway
        # runner can surface this clearly and the user knows to act.
        message = (
            "Telegram polling could not recover after %d retries (%ds total wait). "
            "The previous gateway session is still held open on Telegram's servers, "
            "or another process is using the same bot token. "
            "To recover: ensure no other Hermes or OpenClaw instance is running "
            "with this token, then restart the gateway with 'hermes gateway restart'."
            % (MAX_CONFLICT_RETRIES, sum(10 + i * 10 for i in range(1, MAX_CONFLICT_RETRIES + 1)))
        )
        logger.error(
            "[%s] %s Original error: %s",
            self.name, message, _redact_telegram_error_text(error),
        )
        # Snapshot whether we are the call that actually transitions to fatal.
        # A concurrent retry task scheduled by an earlier conflict may already
        # be suspended past the entry guard; once _set_fatal_error flips the
        # flag, adding an await below (the bounded stop()) yields the loop and
        # lets that task reach this branch too — double-notifying the fatal
        # handler.  Only the first transition notifies.
        _already_fatal = (
            self.has_fatal_error
            and self.fatal_error_code == "telegram_polling_conflict"
        )
        self._set_fatal_error("telegram_polling_conflict", message, retryable=False)
        try:
            if self._app and self._app.updater:
                await asyncio.wait_for(self._app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] updater.stop() timed out after exhausting conflict "
                "retries (likely CLOSE-WAIT socket); proceeding to fatal notify",
                self.name,
            )
        except Exception as stop_error:
            logger.warning(
                "[%s] Failed stopping Telegram updater after exhausting conflict retries: %s",
                self.name, stop_error, exc_info=True,
            )
        if not _already_fatal:
            await self._handoff_polling_fatal_error()

    async def _handoff_polling_fatal_error(self) -> None:
        """Notify the runner without letting child teardown cancel this owner.

        The runner bounds adapter cleanup in a child task.  ``disconnect()``
        cancels the tracked polling-recovery task and the heartbeat task, so
        retaining the current notifier in either field would cancel the fatal
        callback before the runner can finish its reconnect or shutdown
        decision.  Release only the current owner from whichever field tracks
        it; unrelated tasks remain under teardown control.
        """
        current_task = asyncio.current_task()
        if self._polling_error_task is current_task:
            self._polling_error_task = None
        if getattr(self, "_polling_heartbeat_task", None) is current_task:
            self._polling_heartbeat_task = None
        await self._notify_fatal_error()
