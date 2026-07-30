"""Policy-neutral host-enforced outer-turn gate.

Providers decide admission and generation; Hermes owns lease lifetime and
revalidates before consequential tool, output, and child-process boundaries.
The core deliberately contains no product-, platform-, or provider-specific
approval policy.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping, Protocol, cast


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_PURPOSES = frozenset({"business", "reload"})


class GateState(str, Enum):
    OPEN = "OPEN"
    CLOSED_DRAINING = "CLOSED_DRAINING"
    RELOAD_ONLY = "RELOAD_ONLY"


class TurnGateBlocked(RuntimeError):
    """The configured host gate could not safely admit or continue a turn."""


class _TurnPoison:
    """Mutable poison cell shared by copied ContextVar contexts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None

    def set(self, reason: str) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason

    def get(self) -> str | None:
        with self._lock:
            return self._reason


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    machine_id: str
    profile: str
    surface: str
    session_instance_id: str
    gateway_instance_id: str
    turn_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "machine_id",
            "profile",
            "surface",
            "session_instance_id",
            "gateway_instance_id",
            "turn_id",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip() or "\x00" in value:
                raise ValueError(
                    f"runtime identity {field_name} must be non-empty text"
                )


@dataclass(frozen=True, slots=True)
class TurnGateRequest:
    entrypoint: str
    purpose: str
    identity: RuntimeIdentity | None
    task_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.entrypoint) is not str or not self.entrypoint.strip():
            raise ValueError("turn gate entrypoint must be non-empty text")
        if type(self.purpose) is not str or self.purpose not in _ALLOWED_PURPOSES:
            raise ValueError("turn gate purpose is unsupported")
        if self.identity is not None and not isinstance(
            self.identity, RuntimeIdentity
        ):
            raise ValueError("turn gate identity must be a RuntimeIdentity")
        if self.task_id is not None and (
            type(self.task_id) is not str or not self.task_id.strip()
        ):
            raise ValueError("turn gate task_id must be non-empty text")


@dataclass(frozen=True, slots=True)
class GateDecision:
    provider_id: str
    state: GateState
    lease_id: str
    generation: int
    allowed_tools: tuple[str, ...] = ()
    child_environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("provider_id", "lease_id"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip() or "\x00" in value:
                raise ValueError(f"gate decision {field_name} is invalid")
        if not isinstance(self.state, GateState):
            raise ValueError("gate state must be a GateState")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("gate generation must be a non-negative integer")
        if type(self.allowed_tools) is not tuple or any(
            type(name) is not str or not name.strip() or "\x00" in name
            for name in self.allowed_tools
        ):
            raise ValueError("allowed_tools must be a tuple of non-empty names")
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("allowed_tools must not contain duplicates")
        if type(self.child_environment) is not tuple:
            raise ValueError("child_environment must be a tuple")
        seen_environment: set[str] = set()
        for item in self.child_environment:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("child_environment entries must be pairs")
            name, value = item
            if (
                type(name) is not str
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name in seen_environment
            ):
                raise ValueError("child_environment variable name is invalid")
            if type(value) is not str or "\x00" in value:
                raise ValueError("child_environment variable value is invalid")
            seen_environment.add(name)


class TurnGateProvider(Protocol):
    def acquire(self, request: TurnGateRequest) -> GateDecision: ...

    def validate(self, decision: GateDecision, checkpoint: str) -> GateDecision: ...

    def release(self, decision: GateDecision) -> None: ...


@dataclass(frozen=True, slots=True)
class _ProviderRegistration:
    owner_id: str
    provider: TurnGateProvider


_registry_lock = threading.RLock()
_providers: dict[str, _ProviderRegistration] = {}
_required_provider_id: str | None = None
_allowed_child_environment: frozenset[str] = frozenset()
_runtime_machine_id: str | None = None
_configuration_error: str | None = None
_configuration_loaded = False
_gateway_instance_id = str(uuid.uuid4())
_session_identity_key = os.urandom(32)
_current_decision: ContextVar[GateDecision | None] = ContextVar(
    "hermes_turn_gate_decision", default=None
)
_current_request: ContextVar[TurnGateRequest | None] = ContextVar(
    "hermes_turn_gate_request", default=None
)
_current_poison: ContextVar[_TurnPoison | None] = ContextVar(
    "hermes_turn_gate_poison", default=None
)


def create_detached_task(coro, *, name: str | None = None) -> asyncio.Task:
    """Create an asyncio task with an empty context instead of copied leases."""
    if name is None:
        return Context().run(asyncio.create_task, coro)
    return Context().run(asyncio.create_task, coro, name=name)


def register_turn_gate_provider(
    provider_id: str,
    provider: TurnGateProvider,
    *,
    owner_id: str,
    replace: bool = False,
) -> None:
    """Register a provider owned by the plugin with the same manifest key."""
    if type(provider_id) is not str or not provider_id.strip():
        raise ValueError("turn gate provider id must be non-empty text")
    if type(owner_id) is not str or owner_id != provider_id:
        raise ValueError("turn gate provider id must match its plugin manifest owner")
    methods = tuple(getattr(provider, method, None) for method in (
        "acquire",
        "validate",
        "release",
    ))
    if not all(callable(method) for method in methods):
        raise ValueError("turn gate provider does not implement the contract")
    if any(inspect.iscoroutinefunction(method) for method in methods):
        raise ValueError("turn gate provider methods must be synchronous")
    with _registry_lock:
        existing = _providers.get(provider_id)
        if existing is not None and existing.provider is not provider and not replace:
            raise ValueError(f"turn gate provider already registered: {provider_id}")
        _providers[provider_id] = _ProviderRegistration(owner_id, provider)


def unregister_turn_gate_providers_by_owner(owner_id: str) -> None:
    if type(owner_id) is not str or not owner_id.strip():
        raise ValueError("turn gate provider owner must be non-empty text")
    with _registry_lock:
        for provider_id in tuple(_providers):
            if _providers[provider_id].owner_id == owner_id:
                _providers.pop(provider_id, None)


def snapshot_turn_gate_providers() -> dict[str, _ProviderRegistration]:
    with _registry_lock:
        return dict(_providers)


def restore_turn_gate_providers(
    snapshot: Mapping[str, _ProviderRegistration],
) -> None:
    if not isinstance(snapshot, Mapping):
        raise ValueError("turn gate provider snapshot must be a mapping")
    restored = dict(snapshot)
    if any(
        type(provider_id) is not str
        or not isinstance(registration, _ProviderRegistration)
        or registration.owner_id != provider_id
        for provider_id, registration in restored.items()
    ):
        raise ValueError("turn gate provider snapshot is invalid")
    with _registry_lock:
        _providers.clear()
        _providers.update(restored)


def _latch_configuration_error(reason: str) -> None:
    global _configuration_error, _configuration_loaded
    with _registry_lock:
        _configuration_error = reason
        _configuration_loaded = True


def mark_turn_gate_configuration_error(reason: str) -> None:
    """Latch an external configuration-load failure for fail-closed admission."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("turn gate configuration error must be non-empty text")
    _latch_configuration_error(reason.strip())


def configure_turn_gate_from_config(config: object) -> None:
    """Load the opt-in gate contract from config.yaml data.

    A malformed configured gate latches a fail-closed error. A later complete,
    valid configuration replaces that latch; removing the section disables the
    optional extension point and restores Hermes' default behavior.
    """
    global _required_provider_id, _allowed_child_environment
    global _runtime_machine_id, _configuration_error, _configuration_loaded

    agent_config = (
        cast(Mapping[str, Any], config).get("agent")
        if isinstance(config, Mapping)
        else None
    )
    gate_config = (
        cast(Mapping[str, Any], agent_config).get("turn_gate")
        if isinstance(agent_config, Mapping)
        else None
    )
    if gate_config is None:
        with _registry_lock:
            _required_provider_id = None
            _allowed_child_environment = frozenset()
            _runtime_machine_id = None
            _configuration_error = None
            _configuration_loaded = True
        return
    if not isinstance(gate_config, Mapping):
        reason = "agent.turn_gate must be a mapping"
        _latch_configuration_error(reason)
        raise TurnGateBlocked(reason)

    gate_mapping = cast(Mapping[str, Any], gate_config)
    required = gate_mapping.get("required_provider")
    if type(required) is not str or not required.strip():
        reason = "agent.turn_gate.required_provider must be non-empty text"
        _latch_configuration_error(reason)
        raise TurnGateBlocked(reason)

    runtime_identity = gate_mapping.get("runtime_identity")
    machine_id = (
        cast(Mapping[str, Any], runtime_identity).get("machine_id")
        if isinstance(runtime_identity, Mapping)
        else None
    )
    if type(machine_id) is not str or not machine_id.strip() or "\x00" in machine_id:
        reason = (
            "agent.turn_gate.runtime_identity.machine_id must be non-empty text"
        )
        _latch_configuration_error(reason)
        raise TurnGateBlocked(reason)

    raw_allowlist = gate_mapping.get("allowed_child_environment", [])
    if type(raw_allowlist) is not list or any(
        type(name) is not str or _ENVIRONMENT_NAME.fullmatch(name) is None
        for name in raw_allowlist
    ) or len(raw_allowlist) != len(set(raw_allowlist)):
        reason = (
            "agent.turn_gate.allowed_child_environment must be a unique list "
            "of environment variable names"
        )
        _latch_configuration_error(reason)
        raise TurnGateBlocked(reason)

    with _registry_lock:
        _required_provider_id = required.strip()
        _allowed_child_environment = frozenset(raw_allowlist)
        _runtime_machine_id = machine_id.strip()
        _configuration_error = None
        _configuration_loaded = True


def clear_turn_gate_registry_for_testing() -> None:
    global _required_provider_id, _allowed_child_environment
    global _runtime_machine_id, _configuration_error, _configuration_loaded
    global _gateway_instance_id, _session_identity_key
    with _registry_lock:
        _providers.clear()
        _required_provider_id = None
        _allowed_child_environment = frozenset()
        _runtime_machine_id = None
        _configuration_error = None
        _configuration_loaded = False
        _gateway_instance_id = str(uuid.uuid4())
        _session_identity_key = os.urandom(32)
    _current_decision.set(None)
    _current_request.set(None)
    _current_poison.set(None)


def build_runtime_identity(
    *,
    surface: str,
    session_scope: str,
    turn_id: str,
) -> RuntimeIdentity | None:
    """Build host-owned identity without exposing raw platform chat IDs."""
    for field_name, value in (
        ("surface", surface),
        ("session_scope", session_scope),
        ("turn_id", turn_id),
    ):
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError(f"runtime {field_name} must be non-empty text")
    with _registry_lock:
        machine_id = _runtime_machine_id
        gateway_instance_id = _gateway_instance_id
        identity_key = _session_identity_key
    if machine_id is None:
        return None
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception as exc:
        raise TurnGateBlocked("runtime profile identity is unavailable") from exc
    if type(profile) is not str or not profile.strip():
        raise TurnGateBlocked("runtime profile identity is unavailable")
    digest = hmac.new(
        identity_key,
        session_scope.encode("utf-8", errors="strict"),
        hashlib.sha256,
    ).hexdigest()
    return RuntimeIdentity(
        machine_id=machine_id,
        profile=profile,
        surface=surface,
        session_instance_id=f"session-{digest}",
        gateway_instance_id=gateway_instance_id,
        turn_id=turn_id,
    )


def current_turn_gate_decision() -> GateDecision | None:
    return _current_decision.get()


def current_turn_gate_request() -> TurnGateRequest | None:
    return _current_request.get()


def _required_provider() -> tuple[str, TurnGateProvider] | None:
    with _registry_lock:
        required = _required_provider_id
        configuration_error = _configuration_error
        registration = _providers.get(required) if required is not None else None
    if configuration_error is not None:
        raise TurnGateBlocked(
            f"mandatory turn gate configuration is invalid: {configuration_error}"
        )
    if required is None:
        return None
    if registration is None:
        raise TurnGateBlocked(f"required provider is not registered: {required}")
    return required, registration.provider


def _validate_decision(required_id: str, decision: Any) -> GateDecision:
    if not isinstance(decision, GateDecision):
        raise TurnGateBlocked("turn gate provider returned an invalid decision")
    if decision.provider_id != required_id:
        raise TurnGateBlocked("turn gate provider identity mismatch")
    return decision


def _require_synchronous_provider_result(result: Any, stage: str) -> Any:
    if not inspect.isawaitable(result):
        return result
    close = getattr(result, "close", None)
    if callable(close):
        close()
    else:
        cancel = getattr(result, "cancel", None)
        if callable(cancel):
            cancel()
    raise TurnGateBlocked(
        f"mandatory turn gate provider returned async work during {stage}"
    )


def _poison_current(reason: str) -> None:
    poison = _current_poison.get()
    if poison is not None:
        poison.set(reason)


def _revalidate_current(checkpoint: str) -> GateDecision | None:
    poison = _current_poison.get()
    poisoned_reason = poison.get() if poison is not None else None
    if poisoned_reason is not None:
        raise TurnGateBlocked(f"outer-turn lease is poisoned: {poisoned_reason}")

    decision = _current_decision.get()
    if decision is None:
        if _required_provider() is not None:
            raise TurnGateBlocked(
                "mandatory turn gate requires an active outer-turn lease"
            )
        return None

    with _registry_lock:
        registration = _providers.get(decision.provider_id)
    if registration is None:
        reason = "mandatory turn gate provider disappeared"
        _poison_current(reason)
        raise TurnGateBlocked(reason)
    try:
        refreshed = _validate_decision(
            decision.provider_id,
            _require_synchronous_provider_result(
                registration.provider.validate(decision, checkpoint),
                "validate",
            ),
        )
    except TurnGateBlocked as exc:
        _poison_current(str(exc))
        raise
    except Exception as exc:
        reason = f"mandatory turn gate revalidation failed at {checkpoint}"
        _poison_current(reason)
        raise TurnGateBlocked(reason) from exc

    drift_checks = (
        ("lease", refreshed.lease_id, decision.lease_id),
        ("generation", refreshed.generation, decision.generation),
        ("state", refreshed.state, decision.state),
        ("tool policy", refreshed.allowed_tools, decision.allowed_tools),
        (
            "child environment",
            refreshed.child_environment,
            decision.child_environment,
        ),
    )
    for label, actual, expected in drift_checks:
        if actual != expected:
            reason = f"turn gate {label} changed during outer turn"
            _poison_current(reason)
            raise TurnGateBlocked(reason)
    _current_decision.set(refreshed)
    return refreshed


def _enforce_entry_state(decision: GateDecision, request: TurnGateRequest) -> None:
    if decision.state is GateState.CLOSED_DRAINING:
        raise TurnGateBlocked("turn blocked by CLOSED_DRAINING fence")
    if decision.state is GateState.RELOAD_ONLY and request.purpose != "reload":
        raise TurnGateBlocked("business turn blocked by RELOAD_ONLY fence")


@contextmanager
def acquire_outer_turn(
    request: TurnGateRequest,
) -> Iterator[GateDecision | None]:
    """Acquire once at the public outer-turn boundary and release exactly once."""
    if not isinstance(request, TurnGateRequest):
        raise ValueError("outer turn requires a TurnGateRequest")
    current = _current_decision.get()
    current_request = _current_request.get()
    if current is not None:
        if (
            current.state is GateState.RELOAD_ONLY
            and current_request is not None
            and current_request.purpose == "reload"
            and request.purpose != "reload"
        ):
            raise TurnGateBlocked("nested turn cannot elevate a RELOAD_ONLY lease")
        _enforce_entry_state(current, request)
        yield current
        return

    required = _required_provider()
    if required is None:
        request_token = _current_request.set(request)
        try:
            yield None
        finally:
            _current_request.reset(request_token)
        return
    required_id, provider = required
    if request.identity is None:
        raise TurnGateBlocked(
            "mandatory turn gate requires host-owned runtime identity"
        )
    try:
        decision = _validate_decision(
            required_id,
            _require_synchronous_provider_result(provider.acquire(request), "acquire"),
        )
    except TurnGateBlocked:
        raise
    except Exception as exc:
        raise TurnGateBlocked("mandatory turn gate failed during acquire") from exc

    decision_token = _current_decision.set(decision)
    request_token = _current_request.set(request)
    poison_token = _current_poison.set(_TurnPoison())
    try:
        _enforce_entry_state(decision, request)
        yield decision
    finally:
        _current_request.reset(request_token)
        _current_decision.reset(decision_token)
        _current_poison.reset(poison_token)
        try:
            _require_synchronous_provider_result(provider.release(decision), "release")
        except Exception as exc:
            raise TurnGateBlocked("mandatory turn gate failed during release") from exc


def enforce_tool_allowed(tool_name: str) -> None:
    if type(tool_name) is not str or not tool_name.strip():
        raise ValueError("tool name must be non-empty text")
    decision = _revalidate_current(f"tool:{tool_name}")
    if decision is None:
        return
    if decision.state is GateState.CLOSED_DRAINING:
        reason = f"tool is blocked by CLOSED_DRAINING fence: {tool_name}"
    elif decision.state is GateState.RELOAD_ONLY and tool_name not in decision.allowed_tools:
        reason = f"tool is blocked by RELOAD_ONLY fence: {tool_name}"
    elif decision.allowed_tools and tool_name not in decision.allowed_tools:
        reason = f"tool is outside the exact gate allowlist: {tool_name}"
    else:
        return
    _poison_current(reason)
    raise TurnGateBlocked(reason)


def enforce_output_allowed() -> None:
    decision = _revalidate_current("output")
    if decision is None:
        return
    if decision.state is GateState.OPEN:
        return
    reason = f"output is blocked by {decision.state.value} fence"
    _poison_current(reason)
    raise TurnGateBlocked(reason)


def record_tool_observation(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    result: Any,
) -> None:
    """Record a host-observed successful main ``skill_view`` invocation."""
    if tool_name != "skill_view":
        return
    decision = _revalidate_current("tool-observation")
    if decision is None:
        return
    try:
        if not tool_call_id:
            raise ValueError("skill observation requires tool_call_id")
        if (
            not isinstance(tool_args, dict)
            or not isinstance(tool_args.get("name"), str)
            or not tool_args["name"]
        ):
            raise ValueError("skill observation requires a skill name")
        if tool_args.get("file_path") not in (None, ""):
            raise ValueError("linked skill files cannot satisfy reload observation")
        if not isinstance(result, str):
            raise ValueError("skill observation result must be JSON text")
        payload = json.loads(result)
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or payload.get("name") != tool_args["name"]
            or not isinstance(payload.get("skill_dir"), str)
            or not os.path.isabs(payload["skill_dir"])
            or payload.get("file_path") not in (None, "")
        ):
            raise ValueError(
                "skill observation result is not a successful main skill load"
            )
    except Exception as exc:
        reason = "skill observation evidence is invalid"
        _poison_current(reason)
        raise TurnGateBlocked(reason) from exc

    required = _required_provider()
    if required is None:
        return
    provider_id, provider = required
    recorder = getattr(provider, "record_tool_observation", None)
    if not callable(recorder):
        if decision.state is GateState.RELOAD_ONLY:
            reason = "required provider cannot record reload observations"
            _poison_current(reason)
            raise TurnGateBlocked(reason)
        return
    request = _current_request.get()
    if request is None:
        reason = "tool observation has no outer-turn identity"
        _poison_current(reason)
        raise TurnGateBlocked(reason)
    try:
        _require_synchronous_provider_result(
            recorder(
                decision,
                request,
                tool_name,
                tool_args,
                tool_call_id,
                result,
            ),
            "record_tool_observation",
        )
    except Exception as exc:
        reason = f"required provider observation failed closed: {provider_id}"
        _poison_current(reason)
        raise TurnGateBlocked(reason) from exc


def inject_turn_gate_child_environment(
    base: Mapping[str, str],
) -> dict[str, str]:
    """Strip caller copies, revalidate, then inject provider-owned values."""
    if not isinstance(base, Mapping) or any(
        type(name) is not str for name in base
    ):
        raise ValueError("child environment base must use text names")
    with _registry_lock:
        allowed = _allowed_child_environment
    child = dict(base)
    for name in allowed:
        child.pop(name, None)

    if _current_decision.get() is None:
        with _registry_lock:
            configuration_error = _configuration_error
        if configuration_error is not None:
            raise TurnGateBlocked(
                f"mandatory turn gate configuration is invalid: {configuration_error}"
            )
        return child

    decision = _revalidate_current("child-environment")
    assert decision is not None
    for name, value in decision.child_environment:
        if name not in allowed:
            reason = "provider child environment is outside the host allowlist"
            _poison_current(reason)
            raise TurnGateBlocked(reason)
        child[name] = value
    return child


def tool_block_message(tool_name: str) -> str | None:
    try:
        enforce_tool_allowed(tool_name)
    except TurnGateBlocked as exc:
        decision = _current_decision.get()
        generation = decision.generation if decision is not None else "unknown"
        return f"{exc}; generation={generation}"
    return None


def guard_output_callback(callback):
    if callback is None:
        return None

    def guarded(value, *args, **kwargs):
        if value not in (None, ""):
            enforce_output_allowed()
        return callback(value, *args, **kwargs)

    return guarded


__all__ = [
    "GateDecision",
    "GateState",
    "RuntimeIdentity",
    "TurnGateBlocked",
    "TurnGateProvider",
    "TurnGateRequest",
    "acquire_outer_turn",
    "build_runtime_identity",
    "clear_turn_gate_registry_for_testing",
    "configure_turn_gate_from_config",
    "create_detached_task",
    "current_turn_gate_decision",
    "current_turn_gate_request",
    "enforce_output_allowed",
    "enforce_tool_allowed",
    "guard_output_callback",
    "inject_turn_gate_child_environment",
    "mark_turn_gate_configuration_error",
    "record_tool_observation",
    "register_turn_gate_provider",
    "restore_turn_gate_providers",
    "snapshot_turn_gate_providers",
    "tool_block_message",
    "unregister_turn_gate_providers_by_owner",
]
