"""Codex app-server transport lifecycle used by the session bridge."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from agent.transports.codex_app_server import CodexAppServerError


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

    def __init__(self, factory: Callable[[], Any]) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        self._client = factory()
        self._lock = threading.RLock()
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
        with self._lock:
            if not self._logical_initialized and self._initialize_kwargs is not None:
                self._initialize_current(self._initialize_kwargs)
            try:
                return self._client.request(method, params, timeout)
            except Exception as exc:
                if not self._is_transport_failure(exc):
                    raise
                self._replace()
                if method not in self._REPLAYABLE_METHODS:
                    raise
                if self._initialize_kwargs is not None:
                    self._initialize_current(self._initialize_kwargs)
                return self._client.request(method, params, timeout)

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

    def _replace(self) -> None:
        old = self._client
        try:
            old.close()
        finally:
            self._client = self._factory()
            self._logical_initialized = not callable(
                getattr(self._client, "initialize", None)
            )

    def _initialize_current(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        method = getattr(self._client, "initialize", None)
        result = method(**kwargs) if callable(method) else {}
        if not isinstance(result, dict):
            raise TypeError("Codex app-server initialize response must be an object")
        self._logical_initialized = True
        return result

    @staticmethod
    def _is_transport_failure(exc: BaseException) -> bool:
        if isinstance(exc, CodexAppServerError):
            return False
        if isinstance(exc, (TimeoutError, BrokenPipeError, EOFError, OSError)):
            return True
        if not isinstance(exc, RuntimeError):
            return False
        message = str(exc).casefold()
        return "app-server" in message or "transport" in message
