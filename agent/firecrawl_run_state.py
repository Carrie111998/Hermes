from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

CREDITS_EXHAUSTED_INFO: Mapping[str, object] = MappingProxyType(
    {
        "code": "provider_credits_exhausted",
        "provider": "firecrawl",
        "scope": "account",
        "retryable": False,
    }
)
CIRCUIT_OPEN_INFO: Mapping[str, object] = MappingProxyType(
    {
        "code": "provider_circuit_open",
        "provider": "firecrawl",
        "scope": "account",
        "retryable": False,
    }
)

_UNSET = object()
_UNSET_NONE = object()


class FirecrawlCreditsExhaustedError(RuntimeError):
    error_info = CREDITS_EXHAUSTED_INFO

    def __init__(self) -> None:
        super().__init__("Firecrawl account credits are exhausted")


class FirecrawlCircuitOpenError(RuntimeError):
    error_info = CIRCUIT_OPEN_INFO

    def __init__(self) -> None:
        super().__init__("Firecrawl account credit circuit is open")


@dataclass
class FirecrawlRunState:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _first_failure: dict[str, object] | None = None
    fallback_decision: str | None = None
    _fallback_providers: dict[str, object] = field(default_factory=dict, repr=False)
    _credits_action_claimed: bool = False

    @property
    def circuit_open(self) -> bool:
        with self._lock:
            return self._first_failure is not None

    @property
    def first_failure(self) -> dict[str, object] | None:
        with self._lock:
            return dict(self._first_failure) if self._first_failure else None

    def record_credits_exhausted(self) -> bool:
        with self._lock:
            if self._first_failure is not None:
                return False
            self._first_failure = dict(CREDITS_EXHAUSTED_INFO)
            self.fallback_decision = "continue_without_firecrawl"
            return True

    def get_or_select_provider(
        self,
        capability: str,
        resolver: Callable[[], object | None],
    ) -> object | None:
        with self._lock:
            current = self._fallback_providers.get(capability, _UNSET)
            if current is not _UNSET:
                return None if current is _UNSET_NONE else current
            selected = resolver()
            self._fallback_providers[capability] = (
                _UNSET_NONE if selected is None else selected
            )
            return selected

    @property
    def credits_action_claimed(self) -> bool:
        with self._lock:
            return self._credits_action_claimed

    def claim_credits_action(self) -> bool:
        with self._lock:
            if self._first_failure is None or self._credits_action_claimed:
                return False
            self._credits_action_claimed = True
            return True


_current: contextvars.ContextVar[FirecrawlRunState | None] = contextvars.ContextVar(
    "firecrawl_run_state", default=None
)


def install_firecrawl_run() -> tuple[FirecrawlRunState, contextvars.Token]:
    run = FirecrawlRunState()
    return run, _current.set(run)


def reset_firecrawl_run(token: contextvars.Token) -> None:
    _current.reset(token)


def current_firecrawl_run() -> FirecrawlRunState | None:
    return _current.get()


def raise_if_firecrawl_circuit_open() -> None:
    run = current_firecrawl_run()
    if run is not None and run.circuit_open:
        raise FirecrawlCircuitOpenError()


def record_firecrawl_credits_exhausted() -> bool:
    run = current_firecrawl_run()
    return run.record_credits_exhausted() if run is not None else False


def get_or_select_fallback_provider(
    capability: str,
    resolver: Callable[[], object | None],
) -> object | None:
    run = current_firecrawl_run()
    if run is None:
        return resolver()
    return run.get_or_select_provider(capability, resolver)


def claim_credits_action() -> bool:
    run = current_firecrawl_run()
    return run.claim_credits_action() if run is not None else False
