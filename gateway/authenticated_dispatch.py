"""Host-owned authenticated gateway dispatch provenance.

This module provides a narrow capability object for plugin commands and tools
that must only run for an authenticated gateway message. The object is not a
credential and does not make installed plugin code a security boundary. Its
purpose is to prevent model arguments, synthetic lookalikes, stale objects,
direct registry calls, local CLI/TUI calls, and derived agents from being
mistaken for an authenticated gateway dispatch.

Contexts are issued as short-lived leases. Validation succeeds only for the
exact object held in the live host registry; copies and reconstructed objects
fail closed. Callers must keep the lease open only around one command handler
or one top-level agent turn.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
from typing import cast, Iterator, Optional
import uuid


AUTHENTICATED_GATEWAY_COMMAND_DISPATCH_VERSION = 1
AUTHENTICATED_GATEWAY_TOOL_DISPATCH_VERSION = 1

_CONSTRUCTION_SEAL = object()
_live_lock = threading.RLock()
_agent_authority_lock = threading.RLock()


class _LiveLease:
    __slots__ = ("context", "active_uses", "condition")

    def __init__(self, context: "AuthenticatedGatewayDispatch") -> None:
        self.context = context
        self.active_uses = 0
        self.condition = threading.Condition()


_live_contexts: dict[str, _LiveLease] = {}


class _AuthenticatedGatewayToolClaim:
    """Private proof that one protected invocation passed atomic admission."""

    __slots__ = (
        "context",
        "lease",
        "tool_name",
        "tool_call_id",
        "active",
        "consumed",
        "lock",
    )

    def __init__(
        self,
        context: "AuthenticatedGatewayDispatch",
        lease: _LiveLease,
        tool_name: str,
        tool_call_id: Optional[str],
    ) -> None:
        self.context = context
        self.lease = lease
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.active = True
        self.consumed = False
        self.lock = threading.Lock()


_current_tool_claim: ContextVar[tuple[_AuthenticatedGatewayToolClaim, ...]] = (
    ContextVar("authenticated_gateway_tool_claim", default=())
)


class AuthenticatedGatewayDispatch:
    """Immutable, non-serializable view of one authenticated gateway message."""

    __slots__ = (
        "_sealed",
        "authorized",
        "dispatch_kind",
        "platform",
        "user_id",
        "chat_id",
        "thread_id",
        "message_id",
        "turn_id",
        "provenance_version",
    )

    def __init__(
        self,
        *,
        platform: str,
        user_id: str,
        chat_id: str,
        message_id: str,
        dispatch_kind: str,
        thread_id: Optional[str] = None,
        _seal: object = None,
        _turn_id: Optional[str] = None,
    ) -> None:
        if _seal is not _CONSTRUCTION_SEAL:
            raise TypeError("Authenticated gateway dispatch is host-issued only")
        object.__setattr__(self, "authorized", True)
        object.__setattr__(self, "dispatch_kind", dispatch_kind)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "chat_id", chat_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "turn_id", _turn_id)
        object.__setattr__(self, "provenance_version", 1)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("Authenticated gateway dispatch is immutable")

    def __copy__(self):
        raise TypeError("Authenticated gateway dispatch cannot be copied")

    def __deepcopy__(self, _memo):
        raise TypeError("Authenticated gateway dispatch cannot be copied")

    def __reduce__(self):
        raise TypeError("Authenticated gateway dispatch cannot be serialized")

    def __repr__(self) -> str:
        return "<AuthenticatedGatewayDispatch host-issued>"


def _required_string(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a built-in string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def validate_authenticated_gateway_dispatch(context: object) -> bool:
    """Return whether *context* is the exact object in a current live lease."""
    if type(context) is not AuthenticatedGatewayDispatch:
        return False
    if context.authorized is not True:
        return False
    if type(context.provenance_version) is not int or context.provenance_version != 1:
        return False
    if context.dispatch_kind not in {"command", "tool"}:
        return False
    if type(context.turn_id) is not str or not context.turn_id:
        return False
    with _live_lock:
        lease = _live_contexts.get(context.turn_id)
        return lease is not None and lease.context is context


def validate_authenticated_gateway_command_dispatch(context: object) -> bool:
    """Return whether *context* is an exact live command-only lease."""
    if type(context) is not AuthenticatedGatewayDispatch:
        return False
    typed_context = cast(AuthenticatedGatewayDispatch, context)
    return (
        validate_authenticated_gateway_dispatch(typed_context)
        and typed_context.dispatch_kind == "command"
        and typed_context.provenance_version
        == AUTHENTICATED_GATEWAY_COMMAND_DISPATCH_VERSION
    )


def validate_authenticated_gateway_tool_dispatch(context: object) -> bool:
    """Return whether *context* is an exact live tool-only lease."""
    if type(context) is not AuthenticatedGatewayDispatch:
        return False
    typed_context = cast(AuthenticatedGatewayDispatch, context)
    return (
        validate_authenticated_gateway_dispatch(typed_context)
        and typed_context.dispatch_kind == "tool"
        and typed_context.provenance_version
        == AUTHENTICATED_GATEWAY_TOOL_DISPATCH_VERSION
    )


def revoke_authenticated_gateway_dispatch(context: object) -> bool:
    """Close a lease to new claims without blocking already-admitted work.

    Existing claims remain responsible for leaving their protected pipeline.
    Control paths such as interrupt, timeout, and steering must never wait for
    a handler that may itself require the interrupt signal in order to finish.
    """
    if type(context) is not AuthenticatedGatewayDispatch:
        return False
    turn_id = context.turn_id
    if type(turn_id) is not str or not turn_id:
        return False
    with _live_lock:
        lease = _live_contexts.get(turn_id)
        if lease is None or lease.context is not context:
            return False
        del _live_contexts[turn_id]
    return True


def taint_and_revoke_authenticated_gateway_dispatch(agent: object) -> bool:
    """Atomically taint an agent turn and close any published lease."""
    with _agent_authority_lock:
        taint = getattr(agent, "_authenticated_gateway_turn_tainted", None)
        if taint is None:
            taint = threading.Event()
            setattr(agent, "_authenticated_gateway_turn_tainted", taint)
        try:
            taint.set()
        except Exception:
            # A malformed marker must deny future issuance.
            setattr(agent, "_authenticated_gateway_turn_tainted", True)
        context = getattr(agent, "_authenticated_gateway_context", None)
        return revoke_authenticated_gateway_dispatch(context)


@contextmanager
def use_authenticated_gateway_tool_dispatch(
    context: object,
    tool_name: str,
    *,
    tool_call_id: Optional[str] = None,
) -> Iterator[Optional[_AuthenticatedGatewayToolClaim]]:
    """Atomically claim a live tool lease for one protected handler use."""
    if type(context) is not AuthenticatedGatewayDispatch:
        yield None
        return
    typed_context = cast(AuthenticatedGatewayDispatch, context)
    claim: Optional[_AuthenticatedGatewayToolClaim] = None
    with _live_lock:
        lease = _live_contexts.get(typed_context.turn_id)
        if (
            lease is not None
            and lease.context is typed_context
            and validate_authenticated_gateway_tool_dispatch(typed_context)
        ):
            with lease.condition:
                lease.active_uses += 1
            claim = _AuthenticatedGatewayToolClaim(
                typed_context,
                lease,
                tool_name,
                tool_call_id,
            )
    if claim is None:
        yield None
        return
    try:
        yield claim
    finally:
        claim.active = False
        with lease.condition:
            lease.active_uses -= 1
            if lease.active_uses == 0:
                lease.condition.notify_all()


def current_authenticated_gateway_tool_claim_is_bound() -> bool:
    """Return whether this invocation context carries an admitted tool claim."""
    return bool(_current_tool_claim.get())


def current_authenticated_gateway_tool_claim_authorizes(
    context: object,
    tool_name: str,
    *,
    tool_call_id: Optional[str] = None,
    consume: bool = False,
) -> bool:
    """Authorize exactly one downstream registry dispatch per admitted call."""
    for claim in _current_tool_claim.get():
        if (
            type(claim) is not _AuthenticatedGatewayToolClaim
            or claim.active is not True
            or claim.context is not context
            or claim.tool_name != tool_name
            or (
                claim.tool_call_id is not None
                and claim.tool_call_id != tool_call_id
            )
        ):
            continue
        with claim.lock:
            if claim.active is not True or claim.consumed:
                continue
            if consume:
                claim.consumed = True
            return True
    return False


@contextmanager
def bind_authenticated_gateway_tool_claim(claim: object) -> Iterator[None]:
    """Propagate one admitted claim through the downstream tool pipeline."""
    if type(claim) is not _AuthenticatedGatewayToolClaim or claim.active is not True:
        raise PermissionError("Authenticated gateway tool claim is not active")
    token = _current_tool_claim.set((cast(_AuthenticatedGatewayToolClaim, claim),))
    try:
        yield
    finally:
        _current_tool_claim.reset(token)


@contextmanager
def issue_authenticated_gateway_dispatch(
    *,
    dispatch_kind: str,
    platform: str,
    user_id: str,
    chat_id: str,
    message_id: str,
    thread_id: Optional[str] = None,
) -> Iterator[AuthenticatedGatewayDispatch]:
    """Issue one live context and revoke it deterministically on scope exit."""
    dispatch_kind = _required_string("dispatch_kind", dispatch_kind)
    if dispatch_kind not in {"command", "tool"}:
        raise ValueError("dispatch_kind must be 'command' or 'tool'")
    platform = _required_string("platform", platform)
    user_id = _required_string("user_id", user_id)
    chat_id = _required_string("chat_id", chat_id)
    message_id = _required_string("message_id", message_id)
    if thread_id is not None:
        thread_id = _required_string("thread_id", thread_id)

    turn_id = uuid.uuid4().hex
    context = AuthenticatedGatewayDispatch(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        dispatch_kind=dispatch_kind,
        thread_id=thread_id,
        _seal=_CONSTRUCTION_SEAL,
        _turn_id=turn_id,
    )
    lease = _LiveLease(context)
    with _live_lock:
        _live_contexts[turn_id] = lease
    try:
        yield context
    finally:
        revoke_authenticated_gateway_dispatch(context)


@contextmanager
def bind_authenticated_gateway_dispatch(
    agent: object,
    context: object,
) -> Iterator[None]:
    """Bind one live lease to an agent and restore prior state on every exit."""
    if context is not None and not validate_authenticated_gateway_dispatch(context):
        raise PermissionError("Authenticated gateway turn lease is not live")
    missing = object()
    previous = getattr(agent, "_authenticated_gateway_context", missing)
    setattr(agent, "_authenticated_gateway_context", context)
    try:
        yield
    finally:
        if previous is missing:
            try:
                delattr(agent, "_authenticated_gateway_context")
            except AttributeError:
                pass
        else:
            setattr(agent, "_authenticated_gateway_context", previous)


@contextmanager
def authenticated_gateway_turn(
    agent: object,
    source: object,
    *,
    authenticated_gateway_request: bool = False,
    event_message_id: object = None,
) -> Iterator[Optional[AuthenticatedGatewayDispatch]]:
    """Issue a lease for one explicitly verified gateway request.

    Unsupported agents, incomplete identities, and callers without the exact
    post-authorization decision yield None. Source shape alone is never proof.
    """
    capability = getattr(
        agent,
        "authenticated_gateway_tool_dispatch_version",
        None,
    )
    source_platform = getattr(source, "platform", None)
    platform = getattr(source_platform, "value", source_platform)
    user_id = getattr(source, "user_id", None)
    chat_id = getattr(source, "chat_id", None)
    thread_id = getattr(source, "thread_id", None)
    source_message_id = getattr(source, "message_id", None)
    message_id = event_message_id
    mandatory_identity = (platform, user_id, chat_id, message_id)
    valid_source_message_id = (
        source_message_id is None
        or (
            type(source_message_id) is str
            and bool(source_message_id)
            and source_message_id == message_id
        )
    )
    valid_thread = (
        thread_id is None
        or (type(thread_id) is str and bool(thread_id))
    )
    if not (
        authenticated_gateway_request is True
        and type(capability) is int
        and capability == AUTHENTICATED_GATEWAY_TOOL_DISPATCH_VERSION
        and all(type(value) is str and value for value in mandatory_identity)
        and valid_source_message_id
        and valid_thread
    ):
        yield None
        return

    missing = object()
    context_manager = None
    context = None
    previous = missing
    with _agent_authority_lock:
        taint = getattr(agent, "_authenticated_gateway_turn_tainted", None)
        if taint is None:
            tainted = False
        else:
            try:
                tainted = taint.is_set() is not False
            except Exception:
                tainted = True
        if not tainted:
            context_manager = issue_authenticated_gateway_dispatch(
                dispatch_kind="tool",
                platform=cast(str, platform),
                user_id=cast(str, user_id),
                chat_id=cast(str, chat_id),
                thread_id=thread_id,
                message_id=cast(str, message_id),
            )
            context = context_manager.__enter__()
            previous = getattr(agent, "_authenticated_gateway_context", missing)
            setattr(agent, "_authenticated_gateway_context", context)
    if context is None:
        yield None
        return
    try:
        yield context
    finally:
        with _agent_authority_lock:
            if getattr(agent, "_authenticated_gateway_context", None) is context:
                if previous is missing:
                    try:
                        delattr(agent, "_authenticated_gateway_context")
                    except AttributeError:
                        pass
                else:
                    setattr(agent, "_authenticated_gateway_context", previous)
            assert context_manager is not None
            context_manager.__exit__(None, None, None)
