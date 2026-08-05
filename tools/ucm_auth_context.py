"""Exact-call authorization capability for ``ucm_structured_process`` (Phase 1).

This module is the sole mint factory for a sealed, one-shot capability used by
the future UCM structured-process tool.  Phase 1 ships the capability and a
minimal dispatch integration in agent/tool_executor.py — no other production
site may mint yet.

Security properties (authoritative spec Section 16.3/16.4):
- private sealed type + module-private construction seal
- caller gate: only modules in _AUTHORIZED_MINT_MODULES may call the factory
- authorization-critical state lives only in factory closure cells (not
  assignable instance attributes)
- immutable enabled-tool snapshot at mint time
- fixed expected tool name ``ucm_structured_process``
- atomic one-shot ``consume`` under a lock
- owner-side ``invalidate`` (works even if never consumed)
- exact provenance (no duck typing / subclass authorization)
- frozen instances: ordinary setattr/delattr cannot attach or mutate state
- copy/deepcopy cannot create a second independently authorizing object
- pickle/JSON reconstruction rejected
- redacted ``repr`` / ``str``
- no TTL, no provider-id auth, no global/ContextVar/session storage
"""

from __future__ import annotations

import inspect
import threading
from typing import Any, Iterable, Optional

__all__ = [
    "EXPECTED_TOOL_NAME",
    "_AUTHORIZED_MINT_MODULES",
    "is_ucm_auth_capability",
    "mint_ucm_auth_context",
]

EXPECTED_TOOL_NAME = "ucm_structured_process"

# Authorized modules (path suffixes) that may call mint_ucm_auth_context.
# Phase 1: tool_executor.py (production dispatch) plus unit/gate tests.
# Phase 2 will add agent/agent_runtime_helpers.py for concurrent dispatch.
_AUTHORIZED_MINT_MODULES: frozenset[str] = frozenset(
    {
        "agent/tool_executor.py",
        "tests/tools/test_ucm_auth_context.py",
        "tests/tools/test_ucm_caller_gate.py",
    }
)

# Non-exported construction seal. Deliberate external subclassing without this
# seal must fail.
_MINT_SEAL = object()

_REDACTED_REPR = "<UcmAuthCapability redacted>"


def _assert_authorized_caller() -> None:
    """Raise PermissionError if the direct caller of mint_ucm_auth_context is not authorized.

    Frame layout when called from inside mint_ucm_auth_context:
      stack[0] = _assert_authorized_caller
      stack[1] = mint_ucm_auth_context
      stack[2] = external caller (the frame we check)
    """
    frame = inspect.stack()[2]
    filename = frame.filename.replace("\\", "/")
    if any(
        filename.endswith("/" + suffix) or filename == suffix
        for suffix in _AUTHORIZED_MINT_MODULES
    ):
        return
    raise PermissionError(
        f"mint_ucm_auth_context may only be called from an authorized module; "
        f"caller: {filename!r}. "
        f"Authorized callers: {sorted(_AUTHORIZED_MINT_MODULES)}"
    )


class _UcmAuthCapabilityBase:
    """Sealed capability base. Not useful alone; factory mints a sealed subclass.

    Authorization-critical mutable state is never stored on instances. Each
    mint creates a sealed subclass whose methods close over private nonlocal
    cells owned exclusively by that mint.
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        seal = kwargs.pop("_ucm_seal", None)
        if seal is not _MINT_SEAL:
            raise TypeError("UcmAuthCapability cannot be subclassed for authorization")
        super().__init_subclass__(**kwargs)

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: ANN401
        raise AttributeError("UcmAuthCapability attributes are frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("UcmAuthCapability attributes are frozen")

    def __copy__(self) -> "_UcmAuthCapabilityBase":
        # Share exact one-shot state: copy is not an independent authorization.
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_UcmAuthCapabilityBase":
        return self

    def __getstate__(self) -> None:
        raise TypeError("UcmAuthCapability cannot be pickled")

    def __setstate__(self, state: Any) -> None:  # noqa: ANN401
        raise TypeError("UcmAuthCapability cannot be unpickled")

    def __reduce__(self) -> None:
        raise TypeError("UcmAuthCapability cannot be pickled")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("UcmAuthCapability cannot be pickled")

    def __repr__(self) -> str:
        return _REDACTED_REPR

    def __str__(self) -> str:
        return _REDACTED_REPR

    # Base implementations never authorize. Factory subclasses override via
    # methods that close over mint-private state.
    def consume(self, tool_name: str) -> bool:  # noqa: ARG002
        return False

    def invalidate(self) -> None:
        return None


def mint_ucm_auth_context(
    enabled_tools: Iterable[str] | None,
    tool_call_id: Optional[str] = None,
) -> _UcmAuthCapabilityBase:
    """Mint a sealed one-shot capability for ``ucm_structured_process``.

    Only modules listed in _AUTHORIZED_MINT_MODULES may call this factory.
    Unauthorized callers receive a PermissionError at call time.

    Phase 1: authorized production site is agent/tool_executor.py (sequential
    dispatch only).  Phase 2 will add concurrent dispatch via
    agent/agent_runtime_helpers.py.

    ``tool_call_id`` is correlation metadata only and is never used for
    authorization decisions.  It is captured in a closure cell and is not
    exposed as an instance attribute.
    """
    _assert_authorized_caller()

    if enabled_tools is None:
        enabled: frozenset[str] = frozenset()
    else:
        enabled = frozenset(enabled_tools)

    # Authorization-critical mutable state: nonlocal cells only. Not attached
    # to the instance, not reachable as ordinary attributes.
    active = True
    consumed = False
    lock = threading.Lock()
    # Captured for future tracing correlation only; never consulted by consume.
    _correlation_id = tool_call_id  # noqa: F841

    class _UcmAuthCapability(_UcmAuthCapabilityBase, _ucm_seal=_MINT_SEAL):
        __slots__ = ()

        def consume(self, tool_name: str) -> bool:
            nonlocal active, consumed
            with lock:
                if not active:
                    return False
                if consumed:
                    return False
                if tool_name != EXPECTED_TOOL_NAME:
                    return False
                if EXPECTED_TOOL_NAME not in enabled:
                    return False
                consumed = True
                return True

        def invalidate(self) -> None:
            nonlocal active
            with lock:
                active = False

    # Class marker for exact provenance (not present on unauthorized subclasses).
    _UcmAuthCapability._ucm_factory_product = _MINT_SEAL  # type: ignore[attr-defined]

    return _UcmAuthCapability()


def is_ucm_auth_capability(value: Any) -> bool:  # noqa: ANN401
    """Exact provenance check — True only for the factory's direct product."""
    if not isinstance(value, _UcmAuthCapabilityBase):
        return False
    cls = type(value)
    if cls is _UcmAuthCapabilityBase:
        return False
    return getattr(cls, "_ucm_factory_product", None) is _MINT_SEAL
