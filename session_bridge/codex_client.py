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

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            result = self._initialize_current(kwargs)
            self._initialize_kwargs = dict(kwargs)
            return result

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("Codex app-server request timeout must be positive")
        deadline = self._monotonic() + float(timeout)
        with self._lock:
            self._raise_if_cancelled()
            if not self._logical_initialized and self._initialize_kwargs is not None:
                self._initialize_current(
                    self._initialize_kwargs,
                    timeout=self._remaining(deadline),
                )
            try:
                result = self._request_current(
                    method,
                    params,
                    timeout=self._remaining(deadline),
                )
            except Exception as exc:
                if not self._is_transport_failure(exc):
                    raise
                if method not in self._REPLAYABLE_METHODS:
                    try:
                        self._replace(deadline=deadline)
                    except Exception:
                        pass
                    raise
                self._replace(deadline=deadline)
                if self._initialize_kwargs is not None:
                    self._initialize_current(
                        self._initialize_kwargs,
                        timeout=self._remaining(deadline),
                    )
                result = self._request_current(
                    method,
                    params,
                    timeout=self._remaining(deadline),
                )
            self._remaining(deadline)
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._client.close()

    def is_alive(self) -> bool:
        with self._lock:
            method = getattr(self._client, "is_alive", None)
            return bool(method()) if callable(method) else not self._closed

    def stderr_tail(self, n: int = 20) -> list[str]:
        with self._lock:
            method = getattr(self._client, "stderr_tail", None)
            return list(method(n)) if callable(method) else []

    def _replace(self, *, deadline: float | None = None) -> None:
        old = self._client
        try:
            close = getattr(old, "close")
            if deadline is not None and self._accepts_keyword(close, "timeout"):
                # Replacement must not leak the failed subprocess when its
                # logical request consumed the whole budget. Permit only the
                # transport's 100ms minimum teardown slice after exhaustion.
                close(timeout=max(0.1, deadline - self._monotonic()))
            else:
                close()
        finally:
            self._client = self._factory()
            self._logical_initialized = not callable(
                getattr(self._client, "initialize", None)
            )

    def _initialize_current(
        self,
        kwargs: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        method = getattr(self._client, "initialize", None)
        if callable(method):
            call_kwargs = dict(kwargs)
            if timeout is not None and self._accepts_keyword(method, "timeout"):
                call_kwargs["timeout"] = timeout
            if self._cancel_event is not None and self._accepts_keyword(
                method, "cancel_event"
            ):
                call_kwargs["cancel_event"] = self._cancel_event
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
    ) -> dict[str, Any]:
        self._raise_if_cancelled()
        request = self._client.request
        if self._cancel_event is not None and self._accepts_keyword(
            request, "cancel_event"
        ):
            return request(
                method,
                params,
                timeout,
                cancel_event=self._cancel_event,
            )
        return request(method, params, timeout)

    def _remaining(self, deadline: float) -> float:
        self._raise_if_cancelled()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex app-server request deadline exhausted")
        return remaining

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
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
