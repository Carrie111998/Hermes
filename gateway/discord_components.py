"""Core-owned dispatch for plugin Discord components.

Discord adapters receive raw ``discord.Interaction`` objects, but plugins must
not.  This module is the narrow boundary between those two trust domains:

* adapters project an interaction into :class:`DiscordComponentTransportEvent`;
* core authorization runs before a replay claim or plugin code;
* the interaction is acknowledged before the plugin handler starts; and
* plugins receive only an immutable, scalar-only context.

Custom IDs intentionally have one strict form::

    hermes-plugin:<namespace>:<action>

Namespaces are exact registrations.  Prefix-overlapping registrations are
rejected as well as duplicates so a later adapter cannot accidentally switch
to prefix routing and change ownership semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias


logger = logging.getLogger(__name__)

DISCORD_COMPONENT_CUSTOM_ID_PREFIX = "hermes-plugin"
DISCORD_COMPONENT_CUSTOM_ID_MAX_LENGTH = 100
DISCORD_COMPONENT_RESPONSE_MAX_LENGTH = 2000
DISCORD_COMPONENT_BUTTON_LABEL_MAX_LENGTH = 80
DISCORD_COMPONENT_BUTTONS_MAX = 5
# Discord requires an initial interaction response within three seconds. Keep
# a transport/scheduling margin even when the caller configures a larger
# general callback timeout.
DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS = 2.5

_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)
_ACTION_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}\Z", re.ASCII)
_CUSTOM_ID_RE = re.compile(
    rf"{DISCORD_COMPONENT_CUSTOM_ID_PREFIX}:"
    r"(?P<namespace>[a-z][a-z0-9_-]{0,31}):"
    r"(?P<action>[a-z0-9][a-z0-9_.-]{0,47})\Z",
    re.ASCII,
)

_INVALID_RESPONSE = "This action is unavailable."
_UNAUTHORIZED_RESPONSE = "You are not authorized to use this action."
_DUPLICATE_RESPONSE = "This action was already handled."
_CAPACITY_RESPONSE = "This action is temporarily unavailable."
_TIMEOUT_RESPONSE = "This action timed out."
_ERROR_RESPONSE = "This action could not be completed."


class InvalidDiscordComponentId(ValueError):
    """The custom ID does not use the strict Hermes plugin grammar."""


class DuplicateDiscordComponentNamespace(ValueError):
    """An exact namespace already has a registered owner."""


class OverlappingDiscordComponentNamespace(ValueError):
    """A namespace prefix overlaps an existing fixed registration."""


@dataclass(frozen=True, slots=True)
class ParsedDiscordComponentId:
    """Validated namespace and action extracted from a Discord custom ID."""

    namespace: str
    action: str


@dataclass(frozen=True, slots=True)
class DiscordComponentTransportEvent:
    """Scalar projection supplied by the Discord adapter.

    The adapter deliberately cannot attach a raw interaction, bot client,
    token, or transport callback to this object.  Callbacks are separate
    keyword-only arguments to :meth:`DiscordComponentRegistry.dispatch`.
    """

    custom_id: str
    guild_id: str | None
    channel_id: str
    message_id: str
    user_id: str
    session_key: str
    interaction_id: str
    platform: str = "discord"

    def __post_init__(self) -> None:
        if self.platform != "discord":
            raise ValueError("platform must be 'discord'")
        _require_scalar("custom_id", self.custom_id)
        if len(self.custom_id) > DISCORD_COMPONENT_CUSTOM_ID_MAX_LENGTH:
            raise ValueError("custom_id exceeds Discord's maximum length")
        if self.guild_id is not None:
            _require_scalar("guild_id", self.guild_id)
        _require_scalar("channel_id", self.channel_id)
        _require_scalar("message_id", self.message_id)
        _require_scalar("user_id", self.user_id)
        _require_scalar("session_key", self.session_key)
        _require_scalar("interaction_id", self.interaction_id)


@dataclass(frozen=True, slots=True)
class DiscordComponentInteraction:
    """Immutable, scalar-only context delivered to a plugin handler.

    ``idempotency_key`` is stable for the same message, actor, namespace, and
    action even when Discord assigns a new interaction ID to a repeated click.
    Core rejects short-window duplicates in memory. A plugin that performs a
    durable or irreversible side effect must also persist this key alongside
    its business result so replay remains harmless after the TTL or a gateway
    restart.
    """

    platform: str
    guild_id: str | None
    channel_id: str
    message_id: str
    user_id: str
    session_key: str
    interaction_id: str
    idempotency_key: str
    namespace: str
    action: str


@dataclass(frozen=True, slots=True)
class DiscordComponentAuthorization:
    """Core authorization input, including the recorded plugin owner."""

    interaction: DiscordComponentInteraction
    plugin_owner: str


@dataclass(frozen=True, slots=True)
class DiscordComponentResponse:
    """A normalized response that can only be delivered ephemerally."""

    content: str

    def __post_init__(self) -> None:
        _require_scalar("content", self.content)
        if len(self.content) > DISCORD_COMPONENT_RESPONSE_MAX_LENGTH:
            raise ValueError("response exceeds Discord's maximum length")

    @property
    def ephemeral(self) -> Literal[True]:
        """Public delivery is not representable on this surface."""

        return True


class DiscordComponentButtonStyle(StrEnum):
    """Discord button styles exposed without a discord.py dependency."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    SUCCESS = "success"
    DANGER = "danger"


@dataclass(frozen=True, slots=True)
class DiscordComponentButton:
    """Plugin-authored button intent before core namespace binding."""

    action: str
    label: str
    style: DiscordComponentButtonStyle | str = (
        DiscordComponentButtonStyle.SECONDARY
    )
    disabled: bool = False

    def __post_init__(self) -> None:
        _validate_action(self.action)
        _require_scalar("label", self.label)
        if len(self.label) > DISCORD_COMPONENT_BUTTON_LABEL_MAX_LENGTH:
            raise ValueError("button label exceeds Discord's maximum length")
        try:
            style = DiscordComponentButtonStyle(self.style)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported Discord component button style") from exc
        object.__setattr__(self, "style", style)
        if not isinstance(self.disabled, bool):
            raise TypeError("disabled must be a bool")


@dataclass(frozen=True, slots=True)
class DiscordComponentRenderedButton:
    """Core-bound button delivered to the shared Discord adapter."""

    custom_id: str
    label: str
    style: DiscordComponentButtonStyle
    disabled: bool


@dataclass(frozen=True, slots=True)
class DiscordComponentMessage:
    """Validated message the adapter may render as one fixed button row."""

    content: str
    namespace: str
    plugin_owner: str
    buttons: tuple[DiscordComponentRenderedButton, ...]

    def __post_init__(self) -> None:
        _require_scalar("content", self.content)
        if len(self.content) > DISCORD_COMPONENT_RESPONSE_MAX_LENGTH:
            raise ValueError("message exceeds Discord's maximum length")
        _validate_namespace(self.namespace)
        _validate_plugin_owner(self.plugin_owner)
        if not 1 <= len(self.buttons) <= DISCORD_COMPONENT_BUTTONS_MAX:
            raise ValueError("a component message requires 1 to 5 buttons")


@dataclass(frozen=True, slots=True)
class DiscordComponentSendReceipt:
    """Transport-neutral, secret-free result returned to plugin code."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class DiscordComponentDispatchStatus(StrEnum):
    """Stable result codes for adapter metrics and tests."""

    HANDLED = "handled"
    INVALID_CUSTOM_ID = "invalid_custom_id"
    UNKNOWN_NAMESPACE = "unknown_namespace"
    UNAUTHORIZED = "unauthorized"
    AUTHORIZATION_ERROR = "authorization_error"
    DUPLICATE = "duplicate"
    REPLAY_CAPACITY_EXHAUSTED = "replay_capacity_exhausted"
    DEFER_ERROR = "defer_error"
    HANDLER_TIMEOUT = "handler_timeout"
    HANDLER_ERROR = "handler_error"


@dataclass(frozen=True, slots=True)
class DiscordComponentDispatchOutcome:
    """Dispatcher result without raw interaction or exception details."""

    status: DiscordComponentDispatchStatus
    response: DiscordComponentResponse
    namespace: str | None
    plugin_owner: str | None
    handler_invoked: bool
    response_sent: bool


DiscordComponentHandlerResult: TypeAlias = DiscordComponentResponse | str
DiscordComponentHandler: TypeAlias = Callable[
    [DiscordComponentInteraction],
    Awaitable[DiscordComponentHandlerResult],
]
DiscordComponentAuthorize: TypeAlias = Callable[
    [DiscordComponentAuthorization],
    Awaitable[bool],
]
DiscordComponentDefer: TypeAlias = Callable[[], Awaitable[None]]
DiscordComponentRespond: TypeAlias = Callable[
    [DiscordComponentResponse],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _Registration:
    namespace: str
    plugin_owner: str
    handler: DiscordComponentHandler


class _ReplayClaimStatus(StrEnum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    CAPACITY_EXHAUSTED = "capacity_exhausted"


class _ReplayClaims:
    """Thread-safe, bounded first-claim store.

    Unexpired entries are never evicted merely to make room.  When capacity is
    exhausted, new interactions fail closed until an entry expires. This is a
    transport-level short-window guard, not durable business idempotency;
    handlers receive a stable key for that stronger boundary.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float],
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, key: str) -> _ReplayClaimStatus:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if key in self._entries:
                return _ReplayClaimStatus.DUPLICATE
            if len(self._entries) >= self._max_entries:
                return _ReplayClaimStatus.CAPACITY_EXHAUSTED
            self._entries[key] = now + self._ttl_seconds
            return _ReplayClaimStatus.CLAIMED

    def size(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._entries)

    def _prune_locked(self, now: float) -> None:
        while self._entries:
            _, expires_at = next(iter(self._entries.items()))
            if expires_at > now:
                break
            self._entries.popitem(last=False)


class DiscordComponentRegistry:
    """Fixed namespace registry and security-ordered async dispatcher."""

    def __init__(
        self,
        *,
        replay_ttl_seconds: float = 300.0,
        max_replay_entries: int = 8192,
        handler_timeout_seconds: float = 15.0,
        callback_timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler_timeout_seconds = _positive_finite(
            "handler_timeout_seconds",
            handler_timeout_seconds,
        )
        self._callback_timeout_seconds = _positive_finite(
            "callback_timeout_seconds",
            callback_timeout_seconds,
        )
        self._ack_timeout_seconds = min(
            self._callback_timeout_seconds,
            DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS,
        )
        replay_ttl_seconds = _positive_finite(
            "replay_ttl_seconds",
            replay_ttl_seconds,
        )
        if (
            not isinstance(max_replay_entries, int)
            or isinstance(max_replay_entries, bool)
            or max_replay_entries <= 0
        ):
            raise ValueError("max_replay_entries must be a positive integer")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._registrations: dict[str, _Registration] = {}
        self._registration_lock = threading.Lock()
        self._replay_claims = _ReplayClaims(
            ttl_seconds=replay_ttl_seconds,
            max_entries=max_replay_entries,
            clock=clock,
        )

    def register(
        self,
        namespace: str,
        *,
        plugin_owner: str,
        handler: DiscordComponentHandler,
    ) -> None:
        """Register one exact namespace for one immutable plugin owner."""

        _validate_namespace(namespace)
        _validate_plugin_owner(plugin_owner)
        if not _is_async_callable(handler):
            raise TypeError("Discord component handlers must be async callables")

        with self._registration_lock:
            for existing in self._registrations:
                if namespace == existing:
                    raise DuplicateDiscordComponentNamespace(
                        f"namespace already registered: {namespace}"
                    )
                if namespace.startswith(existing) or existing.startswith(namespace):
                    raise OverlappingDiscordComponentNamespace(
                        f"namespace overlaps existing registration: {existing}"
                    )
            self._registrations[namespace] = _Registration(
                namespace=namespace,
                plugin_owner=plugin_owner,
                handler=handler,
            )

    def unregister(self, namespace: str, *, plugin_owner: str) -> bool:
        """Remove one namespace only when its core-recorded owner matches.

        This is used by plugin-load rollback.  Requiring the recorded owner
        prevents a failed plugin from causing the loader to remove another
        plugin's pre-existing namespace.
        """

        _validate_namespace(namespace)
        _validate_plugin_owner(plugin_owner)
        with self._registration_lock:
            registration = self._registrations.get(namespace)
            if registration is None:
                return False
            if registration.plugin_owner != plugin_owner:
                raise PermissionError(
                    "Discord component namespace owner does not match"
                )
            del self._registrations[namespace]
            return True

    def owner_for(self, namespace: str) -> str | None:
        """Return the core-recorded owner for an exact namespace."""

        with self._registration_lock:
            registration = self._registrations.get(namespace)
        return registration.plugin_owner if registration is not None else None

    def build_message(
        self,
        namespace: str,
        *,
        plugin_owner: str,
        content: str,
        buttons: tuple[DiscordComponentButton, ...] | list[DiscordComponentButton],
    ) -> DiscordComponentMessage:
        """Bind one plugin's typed buttons to its exact registered namespace."""

        registration = self._registration_for(namespace)
        if registration is None:
            raise LookupError(
                f"Discord component namespace is not registered: {namespace}"
            )
        if registration.plugin_owner != plugin_owner:
            raise PermissionError(
                "Discord component namespace owner does not match"
            )
        if not isinstance(buttons, (tuple, list)):
            raise TypeError("buttons must be a list or tuple")
        if not 1 <= len(buttons) <= DISCORD_COMPONENT_BUTTONS_MAX:
            raise ValueError("a component message requires 1 to 5 buttons")

        rendered: list[DiscordComponentRenderedButton] = []
        seen_actions: set[str] = set()
        for button in buttons:
            if not isinstance(button, DiscordComponentButton):
                raise TypeError(
                    "buttons must contain DiscordComponentButton values"
                )
            if button.action in seen_actions:
                raise ValueError("button actions must be unique per message")
            seen_actions.add(button.action)
            rendered.append(
                DiscordComponentRenderedButton(
                    custom_id=build_discord_component_custom_id(
                        namespace,
                        button.action,
                    ),
                    label=button.label,
                    style=DiscordComponentButtonStyle(button.style),
                    disabled=button.disabled,
                )
            )

        return DiscordComponentMessage(
            content=content,
            namespace=namespace,
            plugin_owner=plugin_owner,
            buttons=tuple(rendered),
        )

    @property
    def registered_namespaces(self) -> tuple[str, ...]:
        """Sorted immutable snapshot of exact registered namespaces."""

        with self._registration_lock:
            return tuple(sorted(self._registrations))

    @property
    def replay_claim_count(self) -> int:
        """Current bounded replay-store size, after TTL pruning."""

        return self._replay_claims.size()

    async def dispatch(
        self,
        event: DiscordComponentTransportEvent,
        *,
        authorize: DiscordComponentAuthorize,
        defer: DiscordComponentDefer,
        respond: DiscordComponentRespond,
    ) -> DiscordComponentDispatchOutcome:
        """Acknowledge, authorize, claim, and dispatch one interaction.

        Ordering is intentionally fail-closed:

        ``parse -> exact owner lookup -> defer -> core authorize -> replay claim
        -> plugin handler -> ephemeral response``.

        Deferral is only Discord protocol acknowledgement; it conveys no
        authorization and exposes no plugin code. Core authorization still
        completes before the replay claim and before the handler is invoked.
        The in-memory replay claim covers the configured TTL. Durable side
        effects remain responsible for transactionally recording the stable
        ``interaction.idempotency_key`` supplied to the handler.
        """

        try:
            parsed = parse_discord_component_custom_id(event.custom_id)
        except InvalidDiscordComponentId:
            return await self._finish(
                status=DiscordComponentDispatchStatus.INVALID_CUSTOM_ID,
                response=DiscordComponentResponse(_INVALID_RESPONSE),
                respond=respond,
            )

        registration = self._registration_for(parsed.namespace)
        if registration is None:
            return await self._finish(
                status=DiscordComponentDispatchStatus.UNKNOWN_NAMESPACE,
                response=DiscordComponentResponse(_INVALID_RESPONSE),
                respond=respond,
                namespace=parsed.namespace,
            )

        interaction = DiscordComponentInteraction(
            platform=event.platform,
            guild_id=event.guild_id,
            channel_id=event.channel_id,
            message_id=event.message_id,
            user_id=event.user_id,
            session_key=event.session_key,
            interaction_id=event.interaction_id,
            idempotency_key=(
                "discord-component:v1:"
                + hashlib.sha256(
                    "\x1f".join(
                        (
                            event.guild_id or "",
                            event.channel_id,
                            event.message_id,
                            event.user_id,
                            event.custom_id,
                        )
                    ).encode("utf-8")
                ).hexdigest()
            ),
            namespace=parsed.namespace,
            action=parsed.action,
        )
        authorization = DiscordComponentAuthorization(
            interaction=interaction,
            plugin_owner=registration.plugin_owner,
        )

        # Acknowledge as soon as the strict namespace owner is known. Core
        # authorization may involve an external policy lookup and must not
        # consume Discord's three-second acknowledgement window.
        try:
            await asyncio.wait_for(
                defer(),
                timeout=self._ack_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Discord component defer timed out; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.DEFER_ERROR,
                response=DiscordComponentResponse(_ERROR_RESPONSE),
                respond=respond,
                registration=registration,
            )
        except Exception:
            logger.warning(
                "Discord component defer failed; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.DEFER_ERROR,
                response=DiscordComponentResponse(_ERROR_RESPONSE),
                respond=respond,
                registration=registration,
            )

        try:
            authorized = await asyncio.wait_for(
                authorize(authorization),
                timeout=self._callback_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Discord component authorization timed out; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.AUTHORIZATION_ERROR,
                response=DiscordComponentResponse(_UNAUTHORIZED_RESPONSE),
                respond=respond,
                registration=registration,
            )
        except Exception:
            logger.warning(
                "Discord component authorization failed; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.AUTHORIZATION_ERROR,
                response=DiscordComponentResponse(_UNAUTHORIZED_RESPONSE),
                respond=respond,
                registration=registration,
            )

        if authorized is not True:
            return await self._finish(
                status=DiscordComponentDispatchStatus.UNAUTHORIZED,
                response=DiscordComponentResponse(_UNAUTHORIZED_RESPONSE),
                respond=respond,
                registration=registration,
            )

        claim_status = self._replay_claims.claim(interaction.idempotency_key)
        if claim_status is _ReplayClaimStatus.DUPLICATE:
            return await self._finish(
                status=DiscordComponentDispatchStatus.DUPLICATE,
                response=DiscordComponentResponse(_DUPLICATE_RESPONSE),
                respond=respond,
                registration=registration,
            )
        if claim_status is _ReplayClaimStatus.CAPACITY_EXHAUSTED:
            return await self._finish(
                status=DiscordComponentDispatchStatus.REPLAY_CAPACITY_EXHAUSTED,
                response=DiscordComponentResponse(_CAPACITY_RESPONSE),
                respond=respond,
                registration=registration,
            )

        try:
            handler_result = await asyncio.wait_for(
                registration.handler(interaction),
                timeout=self._handler_timeout_seconds,
            )
            response = normalize_discord_component_response(handler_result)
        except TimeoutError:
            logger.warning(
                "Discord component handler timed out; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.HANDLER_TIMEOUT,
                response=DiscordComponentResponse(_TIMEOUT_RESPONSE),
                respond=respond,
                registration=registration,
                handler_invoked=True,
            )
        except Exception:
            # Deliberately omit exception text/traceback.  A plugin exception
            # may contain credentials or user content.
            logger.warning(
                "Discord component handler failed; namespace=%s owner=%s",
                registration.namespace,
                registration.plugin_owner,
            )
            return await self._finish(
                status=DiscordComponentDispatchStatus.HANDLER_ERROR,
                response=DiscordComponentResponse(_ERROR_RESPONSE),
                respond=respond,
                registration=registration,
                handler_invoked=True,
            )

        return await self._finish(
            status=DiscordComponentDispatchStatus.HANDLED,
            response=response,
            respond=respond,
            registration=registration,
            handler_invoked=True,
        )

    def _registration_for(self, namespace: str) -> _Registration | None:
        with self._registration_lock:
            return self._registrations.get(namespace)

    async def _finish(
        self,
        *,
        status: DiscordComponentDispatchStatus,
        response: DiscordComponentResponse,
        respond: DiscordComponentRespond,
        registration: _Registration | None = None,
        namespace: str | None = None,
        handler_invoked: bool = False,
    ) -> DiscordComponentDispatchOutcome:
        response_sent = False
        try:
            await asyncio.wait_for(
                respond(response),
                timeout=self._callback_timeout_seconds,
            )
            response_sent = True
        except TimeoutError:
            logger.warning(
                "Discord component response timed out; namespace=%s owner=%s",
                registration.namespace if registration else namespace,
                registration.plugin_owner if registration else None,
            )
        except Exception:
            logger.warning(
                "Discord component response failed; namespace=%s owner=%s",
                registration.namespace if registration else namespace,
                registration.plugin_owner if registration else None,
            )

        return DiscordComponentDispatchOutcome(
            status=status,
            response=response,
            namespace=registration.namespace if registration else namespace,
            plugin_owner=registration.plugin_owner if registration else None,
            handler_invoked=handler_invoked,
            response_sent=response_sent,
        )


def parse_discord_component_custom_id(custom_id: str) -> ParsedDiscordComponentId:
    """Parse the strict ``hermes-plugin:<namespace>:<action>`` grammar."""

    if not isinstance(custom_id, str):
        raise InvalidDiscordComponentId("custom_id must be a string")
    if len(custom_id) > DISCORD_COMPONENT_CUSTOM_ID_MAX_LENGTH:
        raise InvalidDiscordComponentId("custom_id exceeds Discord's maximum length")
    match = _CUSTOM_ID_RE.fullmatch(custom_id)
    if match is None:
        raise InvalidDiscordComponentId("invalid Hermes plugin component custom_id")
    return ParsedDiscordComponentId(
        namespace=match.group("namespace"),
        action=match.group("action"),
    )


def build_discord_component_custom_id(namespace: str, action: str) -> str:
    """Build a validated custom ID for a registered plugin component."""

    _validate_namespace(namespace)
    _validate_action(action)
    custom_id = f"{DISCORD_COMPONENT_CUSTOM_ID_PREFIX}:{namespace}:{action}"
    if len(custom_id) > DISCORD_COMPONENT_CUSTOM_ID_MAX_LENGTH:
        raise InvalidDiscordComponentId("custom_id exceeds Discord's maximum length")
    return custom_id


def normalize_discord_component_response(
    result: DiscordComponentHandlerResult,
) -> DiscordComponentResponse:
    """Normalize a handler result without permitting a public response."""

    if isinstance(result, DiscordComponentResponse):
        return result
    if isinstance(result, str):
        return DiscordComponentResponse(result)
    raise TypeError("handler must return str or DiscordComponentResponse")


def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or _NAMESPACE_RE.fullmatch(namespace) is None:
        raise InvalidDiscordComponentId("invalid Discord component namespace")


def _validate_action(action: str) -> None:
    if not isinstance(action, str) or _ACTION_RE.fullmatch(action) is None:
        raise InvalidDiscordComponentId("invalid Discord component action")


def _validate_plugin_owner(plugin_owner: str) -> None:
    _require_scalar("plugin_owner", plugin_owner)
    if len(plugin_owner) > 128:
        raise ValueError("plugin_owner exceeds maximum length")


def _require_scalar(field_name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")


def _positive_finite(field_name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be positive and finite")
    return normalized


def _is_async_callable(value: object) -> bool:
    if not callable(value):
        return False
    if inspect.iscoroutinefunction(value):
        return True
    return inspect.iscoroutinefunction(getattr(value, "__call__", None))


__all__ = [
    "DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS",
    "DISCORD_COMPONENT_CUSTOM_ID_PREFIX",
    "DISCORD_COMPONENT_CUSTOM_ID_MAX_LENGTH",
    "DISCORD_COMPONENT_BUTTON_LABEL_MAX_LENGTH",
    "DISCORD_COMPONENT_BUTTONS_MAX",
    "DISCORD_COMPONENT_RESPONSE_MAX_LENGTH",
    "DiscordComponentAuthorization",
    "DiscordComponentButton",
    "DiscordComponentButtonStyle",
    "DiscordComponentAuthorize",
    "DiscordComponentDefer",
    "DiscordComponentDispatchOutcome",
    "DiscordComponentDispatchStatus",
    "DiscordComponentHandler",
    "DiscordComponentHandlerResult",
    "DiscordComponentInteraction",
    "DiscordComponentMessage",
    "DiscordComponentRenderedButton",
    "DiscordComponentRegistry",
    "DiscordComponentRespond",
    "DiscordComponentResponse",
    "DiscordComponentSendReceipt",
    "DiscordComponentTransportEvent",
    "DuplicateDiscordComponentNamespace",
    "InvalidDiscordComponentId",
    "OverlappingDiscordComponentNamespace",
    "ParsedDiscordComponentId",
    "build_discord_component_custom_id",
    "normalize_discord_component_response",
    "parse_discord_component_custom_id",
]
