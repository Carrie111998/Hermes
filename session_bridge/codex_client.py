"""Codex app-server transport lifecycle used by the session bridge."""

from __future__ import annotations

from collections.abc import Callable
import inspect
import math
import threading
import time
from typing import Any

from agent.transports.codex_app_server import (
    CodexAppServerError,
    CodexRequestCancelled,
)


class RecoveringCodexAppServerClient:
    """Own a replaceable Codex app-server client.

    Read-only requests may be repeated once on a fresh subprocess after a
    transport failure. Mutations are never replayed because their outcome may
    already have been committed by the old subprocess.
    """

    _REPLAYABLE_METHODS = frozenset({
        "thread/list",
        "thread/read",
        "thread/search",
    })

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._factory = factory
        self._client = factory()
        self._lock = threading.RLock()
        self._monotonic = monotonic
        self._cancel_event = cancel_event
        self._initialize_kwargs: dict[str, Any] | None = None
        self._logical_initialized = not callable(
            getattr(self._client, "initialize", None)
        )
        self._session_bridge_experimental_api = False
        self._closed = False

    @property
    def _initialized(self) -> bool:
        return self._logical_initialized

    def initialize(
        self,
        *,
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_client(cancel_event=cancel_event)
            result = self._initialize_current(kwargs, cancel_event=cancel_event)
            self._initialize_kwargs = dict(kwargs)
            return result

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("Codex app-server request timeout must be positive")
        with self._lock:
            # The caller's logical budget governs work on the owned transport,
            # not time queued behind another operation on this serialized client.
            # Starting the deadline before acquiring the lock made concurrent
            # catalog and visibility scans exhaust 30 seconds while waiting,
            # then recycle a healthy app-server without sending a request.
            deadline = self._monotonic() + float(timeout)
            self._raise_if_cancelled(cancel_event)
            self._ensure_client(cancel_event=cancel_event)
            if not self._logical_initialized and self._initialize_kwargs is not None:
                self._initialize_current(
                    self._initialize_kwargs,
                    timeout=self._remaining(deadline, cancel_event),
                    cancel_event=cancel_event,
                )
            try:
                result = self._request_current(
                    method,
                    params,
                    timeout=self._remaining(deadline, cancel_event),
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                if not self._is_transport_failure(exc):
                    raise
                if method not in self._REPLAYABLE_METHODS:
                    try:
                        self._replace(deadline=deadline, cancel_event=cancel_event)
                    except Exception:
                        pass
                    raise
                self._replace(deadline=deadline, cancel_event=cancel_event)
                if self._initialize_kwargs is not None:
                    self._initialize_current(
                        self._initialize_kwargs,
                        timeout=self._remaining(deadline, cancel_event),
                        cancel_event=cancel_event,
                    )
                result = self._request_current(
                    method,
                    params,
                    timeout=self._remaining(deadline, cancel_event),
                    cancel_event=cancel_event,
                )
            try:
                self._remaining(deadline, cancel_event)
            except TimeoutError:
                # A transport that returns after consuming the logical deadline
                # is not safe to reuse.  Recycle it for the next call, but never
                # replay this completed-but-late request: even a read may have
                # consumed remote state and the caller has already timed out.
                try:
                    self._replace(deadline=deadline, cancel_event=cancel_event)
                except Exception:
                    # Replacement is best-effort after the logical operation has
                    # already timed out. Preserve that timeout; _replace leaves a
                    # failed replacement unbound so the next call can recover.
                    pass
                raise
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
            if client is not None:
                client.close()

    def is_alive(self) -> bool:
        with self._lock:
            if self._client is None:
                return False
            method = getattr(self._client, "is_alive", None)
            return bool(method()) if callable(method) else not self._closed

    def stderr_tail(self, n: int = 20) -> list[str]:
        with self._lock:
            if self._client is None:
                return []
            method = getattr(self._client, "stderr_tail", None)
            return list(method(n)) if callable(method) else []

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        with self._lock:
            if self._client is None:
                return None
            method = getattr(self._client, "take_notification", None)
            if not callable(method):
                return None
            result = method(timeout=timeout)
            return result if isinstance(result, dict) else None

    def _replace(
        self,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        old = self._client
        self._client = None
        self._logical_initialized = False
        close_error: BaseException | None = None
        try:
            close = getattr(old, "close")
            if deadline is not None and self._accepts_keyword(close, "timeout"):
                # Replacement must not leak the failed subprocess when its
                # logical request consumed the whole budget. Permit only the
                # transport's 100ms minimum teardown slice after exhaustion.
                close(timeout=max(0.1, deadline - self._monotonic()))
            else:
                close()
        except BaseException as exc:
            close_error = exc
        self._raise_if_cancelled(cancel_event)
        replacement = self._factory()
        self._client = replacement
        self._logical_initialized = not callable(
            getattr(replacement, "initialize", None)
        )
        if close_error is not None:
            raise close_error

    def _ensure_client(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("Codex app-server client is closed")
        if self._client is not None:
            return
        self._raise_if_cancelled(cancel_event)
        replacement = self._factory()
        self._client = replacement
        self._logical_initialized = not callable(
            getattr(replacement, "initialize", None)
        )

    def _initialize_current(
        self,
        kwargs: dict[str, Any],
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        method = getattr(self._client, "initialize", None)
        if callable(method):
            call_kwargs = dict(kwargs)
            if timeout is not None and self._accepts_keyword(method, "timeout"):
                call_kwargs["timeout"] = timeout
            active_cancel_event = cancel_event or self._cancel_event
            if active_cancel_event is not None and self._accepts_keyword(
                method, "cancel_event"
            ):
                call_kwargs["cancel_event"] = active_cancel_event
            result = method(**call_kwargs)
        else:
            result = {}
        if not isinstance(result, dict):
            raise TypeError("Codex app-server initialize response must be an object")
        self._logical_initialized = True
        return result

    def _request_current(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancel_event)
        request = self._client.request
        active_cancel_event = cancel_event or self._cancel_event
        if active_cancel_event is not None and self._accepts_keyword(
            request, "cancel_event"
        ):
            return request(
                method,
                params,
                timeout,
                cancel_event=active_cancel_event,
            )
        return request(method, params, timeout)

    def _remaining(
        self,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> float:
        self._raise_if_cancelled(cancel_event)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex app-server request deadline exhausted")
        return remaining

    def _raise_if_cancelled(
        self, cancel_event: threading.Event | None = None
    ) -> None:
        active_cancel_event = cancel_event or self._cancel_event
        if active_cancel_event is not None and active_cancel_event.is_set():
            raise CodexRequestCancelled("codex app-server request cancelled")

    @staticmethod
    def _accepts_keyword(method: Callable[..., Any], keyword: str) -> bool:
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == keyword
            for parameter in parameters
        )

    @staticmethod
    def _is_transport_failure(exc: BaseException) -> bool:
        if isinstance(exc, (CodexAppServerError, CodexRequestCancelled)):
            return False
        if isinstance(exc, (TimeoutError, BrokenPipeError, EOFError, OSError)):
            return True
        if not isinstance(exc, RuntimeError):
            return False
        message = str(exc).casefold()
        return "app-server" in message or "transport" in message
