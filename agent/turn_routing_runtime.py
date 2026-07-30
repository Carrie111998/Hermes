"""Transactional application of a route for exactly one user turn."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import threading
import time
from typing import Any

from agent.message_content import flatten_message_text
from agent.turn_router_budget import normalize_provider_submission_id
from agent.turn_router import (
    RouteAuthorization,
    RouteDecision,
    RouteTarget,
    authorize_route,
    enforce_hard_budget_target,
    hard_budget_slot_count,
    route_decision_payload,
)


class RouteApplicationError(RuntimeError):
    """Raised when a route target cannot be applied safely."""

    def __init__(
        self,
        message: str,
        *,
        restore_failed: bool = False,
        decision: RouteDecision | None = None,
    ):
        super().__init__(message)
        self.restore_failed = restore_failed
        self.decision = decision


class RouteAuthorizationError(RuntimeError):
    """A selected explicit target failed a non-bypassable hard gate."""

    def __init__(self, decision: RouteDecision):
        super().__init__(decision.reason_code or "route_not_authorized")
        self.decision = decision


class RouteBudgetDispatchBlocked(RuntimeError):
    """A hard-budget turn tried to dispatch without active authorization."""

    def __init__(self, state: str | None) -> None:
        self.reason_code = "route_budget_dispatch_blocked"
        self.budget_state = state
        super().__init__(self.reason_code)


LIVE_AUTOMATIC_ROUTING_ENABLED = False


@dataclass(frozen=True)
class PreparedTurnMessage:
    """API and persistence message shapes produced after route authorization."""

    user_message: Any
    persist_user_message: Any = None


@dataclass(frozen=True)
class BudgetRouteContext:
    """Core-minted durable reservation attached to one real user turn."""

    ledger: Any
    reservation: Any
    cooldown_seconds: float
    protects_resident_fallback: bool = False


@dataclass
class TurnRoutingSessionState:
    """Non-prompt session routing state shared across product surfaces.

    This state is intentionally process-local and fail-safe across restart: a
    restart drops affinity rather than resurrecting a stale automatic route.
    Persistent user model pins remain owned by the existing session store.
    """

    affinity_route: str | None = None
    affinity_target: dict[str, Any] | None = None
    affinity_remaining: int = 0
    consecutive_failures: int = 0
    fail_off: bool = False
    fail_off_reason: str | None = None
    latest_event: str | None = None
    latest_payload: dict[str, Any] | None = None
    turn_sequence: int = 0
    affinity_window: int = 0
    failure_limit: int = 3

    def reset(self) -> None:
        self.affinity_route = None
        self.affinity_target = None
        self.affinity_remaining = 0
        self.consecutive_failures = 0
        self.fail_off = False
        self.fail_off_reason = None
        self.latest_event = None
        self.latest_payload = None


@dataclass(frozen=True)
class TurnRoutingRequest:
    """Typed top-level opt-in for one core-owned routing lifecycle."""

    surface: str
    session_id: str | None = None
    user_text: Any = ""
    explicit_turn_override: bool = False
    explicit_moa_override: bool = False
    explicit_target: RouteTarget | Mapping[str, Any] | None = None
    session_pinned: bool = False
    manual_mode: bool = False
    # Backend rollout authority. Product surfaces intentionally inherit the
    # locked default; deterministic tests/evaluators may opt in explicitly.
    allow_automatic: bool = LIVE_AUTOMATIC_ROUTING_ENABLED
    allowed_routes: frozenset[str] | None = None
    authorization: RouteAuthorization | Mapping[str, Any] | None = None
    moa_config: Mapping[str, Any] | None = None
    prepare_user_message: Callable[[Any], PreparedTurnMessage] | None = None
    config_loader: Callable[[], dict[str, Any]] | None = None
    emit: Callable[[str, dict[str, Any]], None] | None = None
    quarantine: Callable[[Any, str], None] | None = None
    session_state: TurnRoutingSessionState | None = None


def load_turn_routing_config() -> dict[str, Any]:
    """Load the canonical profile-local ``routing`` block read-only."""

    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    if not isinstance(config, dict):
        return {}
    value = config.get("routing")
    return value if isinstance(value, dict) else {}


def load_turn_moa_config() -> dict[str, Any]:
    """Load normalized MoA slots for hard-budget identity checks."""

    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.moa_config import normalize_moa_config

        config = load_config_readonly()
        raw = config.get("moa") if isinstance(config, dict) else {}
        return normalize_moa_config(raw or {})
    except Exception:
        # An opaque MoA runtime may contain Grok. Identity lookup failure is a
        # hard-budget denial, never an ordinary routing fail-open.
        return {"_identity_unavailable": True}


_MISSING = object()

_TRANSIENT_RUNTIME_ATTRS = (
    "model",
    "provider",
    "requested_provider",
    "base_url",
    "api_mode",
    "api_key",
    "client",
    "_anthropic_client",
    "_anthropic_api_key",
    "_anthropic_base_url",
    "_is_anthropic_oauth",
    "_client_kwargs",
    "_credential_pool",
    "_credential_pool_entry_id",
    "_config_context_length",
    "_use_prompt_caching",
    "_use_native_cache_layout",
    "reasoning_config",
    "_transport_cache",
    "context_compressor",
    "_consecutive_stale_streams",
)

_TRANSIENT_GUARD_ATTRS = (
    "_cached_system_prompt",
    "_primary_runtime",
    "_fallback_chain",
    "_fallback_index",
    "_fallback_activated",
    "_rate_limited_until",
)


def _clone_container(value: Any) -> Any:
    """Copy built-in containers while preserving opaque resource identities."""

    if isinstance(value, dict):
        return {key: _clone_container(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_container(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_container(item) for item in value)
    if isinstance(value, set):
        return {_clone_container(item) for item in value}
    return value


def _state_matches(current: Any, original: Any, frozen: Any) -> bool:
    if isinstance(original, (dict, list, set)):
        return current is original and current == frozen
    if isinstance(original, tuple):
        return current == frozen
    if isinstance(original, (str, int, float, bool, bytes, type(None))):
        return current == original
    return current is original


@dataclass(frozen=True)
class _AttributeSnapshot:
    exists: bool
    original: Any = None
    frozen: Any = None

    @classmethod
    def capture(cls, owner: Any, name: str) -> "_AttributeSnapshot":
        value = getattr(owner, name, _MISSING)
        if value is _MISSING:
            return cls(exists=False)
        return cls(exists=True, original=value, frozen=_clone_container(value))

    def matches(self, owner: Any, name: str) -> bool:
        current = getattr(owner, name, _MISSING)
        if not self.exists:
            return current is _MISSING
        if current is _MISSING:
            return False
        return _state_matches(current, self.original, self.frozen)

    def restore(self, owner: Any, name: str) -> None:
        if not self.exists:
            if hasattr(owner, name):
                delattr(owner, name)
            return

        original = self.original
        if isinstance(original, dict):
            original.clear()
            original.update(_clone_container(self.frozen))
        elif isinstance(original, list):
            original[:] = _clone_container(self.frozen)
        elif isinstance(original, set):
            original.clear()
            original.update(_clone_container(self.frozen))
        setattr(owner, name, original)


@dataclass(frozen=True)
class TransientRuntimeSnapshot:
    """Exact process-local state captured before one request-scoped route."""

    runtime: dict[str, _AttributeSnapshot]
    guards: dict[str, _AttributeSnapshot]
    compressor_state: dict[str, Any] | None

    @classmethod
    def capture(cls, agent: Any) -> "TransientRuntimeSnapshot":
        runtime = {
            name: _AttributeSnapshot.capture(agent, name)
            for name in _TRANSIENT_RUNTIME_ATTRS
        }
        guards = {
            name: _AttributeSnapshot.capture(agent, name)
            for name in _TRANSIENT_GUARD_ATTRS
        }
        compressor = getattr(agent, "context_compressor", None)
        try:
            compressor_state = _clone_container(vars(compressor))
        except TypeError:
            compressor_state = None
        return cls(
            runtime=runtime,
            guards=guards,
            compressor_state=compressor_state,
        )

    @property
    def original_resource_ids(self) -> frozenset[int]:
        resources = []
        for name in ("client", "_anthropic_client"):
            captured = self.runtime.get(name)
            if captured is not None and captured.exists and captured.original is not None:
                resources.append(id(captured.original))
        return frozenset(resources)

    def restore(self, agent: Any) -> bool:
        """Repair all captured state and report whether guard invariants held."""

        try:
            guards_intact = all(
                captured.matches(agent, name)
                for name, captured in self.guards.items()
            )
            for name, captured in self.runtime.items():
                captured.restore(agent, name)

            compressor = getattr(agent, "context_compressor", None)
            if self.compressor_state is not None and compressor is not None:
                compressor_vars = vars(compressor)
                compressor_vars.clear()
                compressor_vars.update(_clone_container(self.compressor_state))

            for name, captured in self.guards.items():
                captured.restore(agent, name)

            runtime_restored_exactly = all(
                captured.matches(agent, name)
                for name, captured in self.runtime.items()
            )
            guards_restored_exactly = all(
                captured.matches(agent, name)
                for name, captured in self.guards.items()
            )
            restored_exactly = runtime_restored_exactly and guards_restored_exactly
            if self.compressor_state is not None and compressor is not None:
                restored_exactly = (
                    restored_exactly
                    and vars(compressor) == self.compressor_state
                )
            return guards_intact and restored_exactly
        except Exception:
            return False


@dataclass(frozen=True)
class TransientRouteToken:
    """Idempotent owner of one applied transient runtime and its resources."""

    agent: Any
    snapshot: TransientRuntimeSnapshot
    decision: RouteDecision | None = None
    transient_resources: tuple[Any, ...] = ()
    _finalized: bool = False
    _restore_result: bool = False

    def restore(self) -> bool:
        if self._finalized:
            return self._restore_result

        restored = self.snapshot.restore(self.agent)
        original_ids = self.snapshot.original_resource_ids
        for resource in self.transient_resources:
            if resource is None or id(resource) in original_ids:
                continue
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                restored = False

        object.__setattr__(self, "_restore_result", restored)
        object.__setattr__(self, "_finalized", True)
        return restored


@dataclass
class TurnRoutingLifecycle:
    """Per-call holder bridging turn setup to outer-call finalization."""

    agent: Any
    request: Any
    turn_id: str | None = None
    turn_sequence: int = 0
    token: Any = None
    decision: RouteDecision | None = None
    budget_context: BudgetRouteContext | None = None
    budget_state: str | None = None
    provider_invoked: bool = False
    provider_submission_id: str | None = None
    prepared: bool = False
    terminal_event_emitted: bool = False
    turn_failed: bool = False
    _budget_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def _session_state(self) -> TurnRoutingSessionState | None:
        state = getattr(self.request, "session_state", None)
        return state if isinstance(state, TurnRoutingSessionState) else None

    def _automatic_decision(self) -> bool:
        return bool(
            isinstance(self.decision, RouteDecision)
            and self.decision.source
            in {"rule", "configured", "classifier", "affinity"}
            and self.decision.route != "current"
        )

    def mark_turn_failed(self, reason_code: str) -> None:
        self.turn_failed = True
        state = self._session_state()
        if state is None or not self._automatic_decision():
            return
        state.affinity_route = None
        state.affinity_target = None
        state.affinity_remaining = 0
        state.consecutive_failures += 1
        if state.consecutive_failures >= max(1, int(state.failure_limit)):
            state.fail_off = True
            state.fail_off_reason = str(reason_code or "route_failed")

    def _record_success(self) -> None:
        state = self._session_state()
        decision = self.decision
        if (
            state is None
            or self.turn_failed
            or not isinstance(decision, RouteDecision)
            or decision.route == "current"
        ):
            return
        if decision.source not in {"rule", "configured", "classifier", "affinity"}:
            return
        state.consecutive_failures = 0
        if decision.source == "affinity":
            state.affinity_remaining = max(0, state.affinity_remaining - 1)
            if state.affinity_remaining == 0:
                state.affinity_route = None
                state.affinity_target = None
            return
        window = max(0, int(state.affinity_window))
        if window == 0:
            return
        state.affinity_route = decision.route
        state.affinity_target = dict(decision.target)
        state.affinity_remaining = window

    def _capture_route(
        self,
        decision: RouteDecision,
        budget_context: BudgetRouteContext | None,
    ) -> None:
        self.decision = decision
        self.budget_context = budget_context
        if budget_context is not None:
            self.budget_state = str(budget_context.reservation.state)
        else:
            self.budget_state = None

    def _budget_reservation_id(self) -> str | None:
        context = self.budget_context
        if context is None:
            return None
        return str(context.reservation.reservation_id or "") or None

    def _raise_budget_accounting_failed(self, exc: BaseException) -> None:
        """Contain an indeterminate durable-ledger transition without retrying."""

        self.budget_state = "accounting_failed"
        self._quarantine("route_budget_accounting_failed")
        self._emit(
            "route.degraded",
            self._payload(
                stage="budget_accounting",
                reason_code="route_budget_accounting_failed",
            ),
        )
        raise RouteBudgetDispatchBlocked(self.budget_state) from exc

    def provider_submission_started(self, api_request_id: str) -> None:
        with self._budget_lock:
            if self.budget_context is None or self.budget_state == "committed":
                return
            if self.budget_state != "reserved":
                raise RouteBudgetDispatchBlocked(self.budget_state)
            self.provider_invoked = True
            self.provider_submission_id = normalize_provider_submission_id(api_request_id)

    @contextmanager
    def provider_submission_scope(self, target_agent: Any, api_request_id: str):
        """Install exact request-local callbacks around one provider attempt."""

        if target_agent is not self.agent:
            raise ValueError("turn routing lifecycle used with a different agent")
        callbacks = {
            "_turn_route_budget_submission_started": (
                lambda: self.provider_submission_started(api_request_id)
            ),
            "_turn_route_budget_submission_accepted": (
                lambda response: self.provider_submission_accepted(
                    response,
                    api_request_id,
                )
            ),
            "_turn_route_budget_submission_failed": (
                lambda error: self.provider_submission_failed(
                    error,
                    api_request_id,
                )
            ),
        }
        previous = {
            name: (hasattr(target_agent, name), getattr(target_agent, name, None))
            for name in callbacks
        }
        for name, callback in callbacks.items():
            setattr(target_agent, name, callback)
        try:
            yield
        finally:
            for name, (existed, value) in previous.items():
                if existed:
                    setattr(target_agent, name, value)
                else:
                    try:
                        delattr(target_agent, name)
                    except AttributeError:
                        pass

    def provider_submission_accepted(self, response: Any, api_request_id: str) -> None:
        with self._budget_lock:
            context = self.budget_context
            reservation_id = self._budget_reservation_id()
            if context is None or reservation_id is None or self.budget_state != "reserved":
                return
            safe_id = normalize_provider_submission_id(
                getattr(response, "id", "") or self.provider_submission_id or api_request_id
            )
            try:
                context.ledger.commit(
                    reservation_id,
                    provider_submission_id=safe_id,
                )
            except Exception as exc:
                self._raise_budget_accounting_failed(exc)
            self.provider_invoked = True
            self.provider_submission_id = safe_id
            self.budget_state = "committed"

    def provider_submission_failed(self, error: BaseException, api_request_id: str) -> None:
        with self._budget_lock:
            context = self.budget_context
            reservation_id = self._budget_reservation_id()
            if context is None or reservation_id is None or self.budget_state != "reserved":
                return
            status = getattr(error, "status_code", None)
            try:
                status_code = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_code = None
            try:
                if status_code in {402, 403, 429}:
                    reason = (
                        "provider_rate_limited"
                        if status_code == 429
                        else "provider_entitlement_rejected"
                    )
                    context.ledger.release(
                        reservation_id,
                        reason_code=reason,
                    )
                    context.ledger.set_cooldown(
                        scope="grok",
                        reason_code=reason,
                        until_at=time.time() + context.cooldown_seconds,
                    )
                    self.budget_state = "released"
                    self.mark_turn_failed(reason)
                    return
                if self.provider_invoked:
                    safe_id = normalize_provider_submission_id(
                        self.provider_submission_id or api_request_id
                    )
                    context.ledger.commit(
                        reservation_id,
                        provider_submission_id=safe_id,
                    )
                    self.provider_submission_id = safe_id
                    self.budget_state = "committed"
                    return
                self.release_before_provider_submission("provider_not_dispatched")
                self.mark_turn_failed("provider_not_dispatched")
            except RouteBudgetDispatchBlocked:
                raise
            except Exception as exc:
                self._raise_budget_accounting_failed(exc)

    def _settle_unfinished_budget(self, reason_code: str) -> None:
        """Settle a reservation before restore while worker callbacks may race.

        Once an SDK invocation started, absence of an accepted/failed callback is
        an uncertain provider outcome, not proof that no request was billed.
        Commit conservatively before removing request-local callbacks.  The
        lifecycle lock makes a late worker callback observe the terminal state
        instead of attempting a conflicting second ledger transition.
        """

        with self._budget_lock:
            if self.budget_state != "reserved":
                return
            if self.provider_invoked:
                context = self.budget_context
                reservation_id = self._budget_reservation_id()
                if context is None or reservation_id is None:
                    return
                safe_id = normalize_provider_submission_id(
                    self.provider_submission_id
                    or f"turn:{self.turn_id or 'unknown'}:{reason_code}"
                )
                try:
                    context.ledger.commit(
                        reservation_id,
                        provider_submission_id=safe_id,
                    )
                except Exception as exc:
                    self._raise_budget_accounting_failed(exc)
                self.provider_submission_id = safe_id
                self.budget_state = "committed"
                return
            self.release_before_provider_submission(reason_code)

    def release_before_provider_submission(self, reason_code: str) -> None:
        with self._budget_lock:
            context = self.budget_context
            reservation_id = self._budget_reservation_id()
            if context is None or reservation_id is None or self.budget_state != "reserved":
                return
            try:
                context.ledger.release(
                    reservation_id,
                    reason_code=reason_code,
                )
            except Exception as exc:
                self._raise_budget_accounting_failed(exc)
            self.budget_state = "released"

    def _payload(self, *, stage: str, reason_code: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": str(getattr(self.request, "session_id", "") or ""),
            "turn_id": str(self.turn_id or ""),
            "surface": str(getattr(self.request, "surface", "") or ""),
        }
        turn_sequence = max(0, int(self.turn_sequence or 0))
        if turn_sequence > 0:
            payload["turn_sequence"] = turn_sequence
        decision = self.decision or getattr(self.token, "decision", None)
        if isinstance(decision, RouteDecision):
            decision_payload = route_decision_payload(decision)
            payload.update(decision_payload)
            payload["selection_reason_code"] = decision_payload["reason_code"]
        if self.budget_state:
            payload["budget_state"] = self.budget_state
        if self.provider_submission_id:
            payload["provider_submission_id"] = self.provider_submission_id
        payload["stage"] = stage
        payload["reason_code"] = reason_code
        return payload

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        state = self._session_state()
        if state is not None:
            state.latest_event = str(event)
            state.latest_payload = dict(payload)
        callback = getattr(self.request, "emit", None)
        if not callable(callback):
            return
        try:
            callback(event, payload)
            if event in {"route.completed", "route.degraded"}:
                self.terminal_event_emitted = True
        except Exception:
            # Observability is subordinate to restoring the user's runtime.
            pass

    def _quarantine(self, reason: str) -> None:
        callback = getattr(self.request, "quarantine", None)
        if not callable(callback):
            return
        try:
            callback(self.agent, reason)
        except Exception:
            # A surface may already have evicted the resident agent.
            pass

    def prepare_message(
        self,
        user_message: Any,
        persist_user_message: Any,
    ) -> PreparedTurnMessage:
        callback = getattr(self.request, "prepare_user_message", None)
        if not callable(callback):
            return PreparedTurnMessage(user_message, persist_user_message)
        prepared = callback(user_message)
        if not isinstance(prepared, PreparedTurnMessage):
            raise TypeError("turn message preparer must return PreparedTurnMessage")
        return prepared

    def prepare(self, target_agent: Any, *, user_message: Any, turn_id: str) -> None:
        if target_agent is not self.agent:
            raise ValueError("turn routing lifecycle used with a different agent")
        if self.prepared:
            raise RuntimeError("turn routing lifecycle prepared more than once")
        self.prepared = True
        self.turn_id = turn_id
        try:
            try:
                self.token = prepare_turn_route(
                    target_agent,
                    self.request,
                    user_message=user_message,
                    turn_id=turn_id,
                    decision_sink=self._capture_route,
                )
            finally:
                state = self._session_state()
                if state is not None:
                    self.turn_sequence = max(0, int(state.turn_sequence or 0))
            token_decision = getattr(self.token, "decision", None)
            if isinstance(token_decision, RouteDecision):
                self.decision = token_decision
        except RouteAuthorizationError as exc:
            self.token = None
            self.decision = exc.decision
            raise
        except Exception as exc:
            self.token = None
            exception_decision = getattr(exc, "decision", None)
            if isinstance(exception_decision, RouteDecision):
                self.decision = exception_decision
            restore_failed = bool(getattr(exc, "restore_failed", False))
            if not (
                self.budget_context is not None
                and self.budget_context.protects_resident_fallback
            ):
                try:
                    self.release_before_provider_submission("route_apply_failed")
                except Exception:
                    restore_failed = True
            if restore_failed:
                self._quarantine("route_restore_failed")
            self._emit(
                "route.degraded",
                self._payload(
                    stage="prepare",
                    reason_code=(
                        "route_restore_failed"
                        if restore_failed
                        else "route_prepare_failed"
                    ),
                ),
            )
            self.mark_turn_failed(
                "route_restore_failed" if restore_failed else "route_prepare_failed"
            )
            if restore_failed:
                raise

    def finish(self) -> None:
        """Restore the request-scoped token, when one was applied."""

        try:
            self._settle_unfinished_budget("turn_finished_before_provider_submission")
        except RouteBudgetDispatchBlocked:
            pass
        except Exception:
            self._quarantine("route_budget_finalize_failed")
            self._emit(
                "route.degraded",
                self._payload(
                    stage="budget_finalize",
                    reason_code="route_budget_finalize_failed",
                ),
            )

        if self.token is None:
            if not self.terminal_event_emitted and isinstance(self.decision, RouteDecision):
                authorization = self.decision.authorization
                if self.budget_state == "accounting_failed":
                    terminal_reason = "route_budget_accounting_failed"
                elif (
                    self.decision.reason_code
                    in {"target_not_allowed", "allowlist_unavailable"}
                    or (authorization is not None and not authorization.allowed)
                ):
                    terminal_reason = "route_authorization_denied"
                else:
                    terminal_reason = "route_completed"
                if terminal_reason == "route_completed":
                    self._record_success()
                self._emit(
                    "route.completed",
                    self._payload(
                        stage="complete",
                        reason_code=terminal_reason,
                    ),
                )
            return

        restored = False
        try:
            restored = self.token.restore() is not False
        except Exception:
            restored = False

        if not restored:
            self.mark_turn_failed("route_restore_failed")
            self._quarantine("route_restore_failed")
            self._emit(
                "route.degraded",
                self._payload(
                    stage="restore",
                    reason_code="route_restore_failed",
                ),
            )
            return

        # A terminal routing event must always carry the immutable decision
        # and authorization provenance that justified application.  Restore
        # malformed/legacy tokens defensively, but do not emit an incomplete
        # event that consumers could mistake for an auditable route outcome.
        if not isinstance(self.decision, RouteDecision) and not isinstance(
            getattr(self.token, "decision", None), RouteDecision
        ):
            return

        self._record_success()
        self._emit(
            "route.completed",
            self._payload(
                stage="restore",
                reason_code="route_completed",
            ),
        )


def _load_routing_config(request: Any) -> dict[str, Any]:
    loader = getattr(request, "config_loader", None)
    if callable(loader):
        config = loader()
    else:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly()
        config = loaded.get("routing", {}) if isinstance(loaded, dict) else {}
    if not isinstance(config, dict):
        raise TypeError("routing config loader must return a mapping")
    return config


def _authorize_with_core_budget(
    decision: RouteDecision,
    request: Any,
    *,
    turn_id: str,
    force_nonapplying: bool = False,
) -> tuple[RouteDecision, BudgetRouteContext | None]:
    """Mint hard-budget authorization after stable turn identity exists."""

    supplied = getattr(request, "authorization", None)
    if (
        decision.mode == "observe"
        and not decision.should_apply
        and not force_nonapplying
    ):
        return decision, None
    if not bool(decision.target.get("budgeted", False)):
        return authorize_route(decision, supplied), None

    try:
        config = _load_routing_config(request)
        raw_budget = config.get("budget")
        if not isinstance(raw_budget, Mapping):
            raise TypeError("routing.budget must be a mapping")
        weekly_limit = int(raw_budget.get("grok_weekly_limit", 0))
        lease_seconds = float(raw_budget.get("reservation_lease_seconds", 300))
        cooldown_seconds = float(raw_budget.get("cooldown_seconds", 3600))
        if weekly_limit <= 0:
            raise ValueError("grok weekly budget is disabled")
        if lease_seconds <= 0 or cooldown_seconds <= 0:
            raise ValueError("routing budget durations must be positive")
        slots = hard_budget_slot_count(
            decision.target,
            moa_config=getattr(request, "moa_config", None),
        )
        if slots is None or slots <= 0:
            denied = RouteAuthorization(
                allowed=False,
                reason_code="budget_identity_unavailable",
            )
            return authorize_route(decision, denied), None

        from agent.turn_router_budget import TurnRouterBudgetLedger

        ledger = TurnRouterBudgetLedger(
            weekly_limit=weekly_limit,
            lease_seconds=lease_seconds,
        )
        reservation = ledger.reserve(
            turn_id=turn_id,
            route_id=decision.route,
            slots=slots,
            cooldown_scope="grok",
        )
    except Exception:
        denied = RouteAuthorization(
            allowed=False,
            reason_code="budget_authorization_unavailable",
        )
        return authorize_route(decision, denied), None

    authorization = RouteAuthorization(
        allowed=bool(reservation.allowed),
        reason_code=str(reservation.reason_code or "budget_denied"),
        reservation_id=reservation.reservation_id,
    )
    authorized = authorize_route(decision, authorization)
    context = (
        BudgetRouteContext(
            ledger=ledger,
            reservation=reservation,
            cooldown_seconds=cooldown_seconds,
        )
        if reservation.allowed and reservation.reservation_id
        else None
    )
    return authorized, context


def _target_route_ids(
    target: RouteTarget,
    config: Mapping[str, Any],
) -> frozenset[str]:
    from agent.turn_router import normalize_turn_routing_config
    from hermes_cli.models import normalize_provider

    normalized = normalize_turn_routing_config(config)
    target_kind = target.kind.casefold()
    target_provider = normalize_provider(str(target.provider or ""))
    target_model = str(target.model or "").strip().casefold()
    target_preset = str(target.preset or "").strip().casefold()
    matches: set[str] = set()
    for route_id, raw_route in normalized.get("routes", {}).items():
        route_target = RouteTarget.from_mapping(raw_route)
        if not route_target.enabled:
            continue
        if route_target.kind.casefold() != target_kind:
            continue
        if target_kind == "model":
            if (
                normalize_provider(str(route_target.provider or "")) == target_provider
                and str(route_target.model or "").strip().casefold() == target_model
            ):
                matches.add(str(route_id))
        elif target_kind == "moa" and (
            str(route_target.preset or "").strip().casefold() == target_preset
        ):
            matches.add(str(route_id))
        elif target_kind == "current":
            matches.add(str(route_id))
    return frozenset(matches)


def _configured_allowed_route_ids(config: Mapping[str, Any]) -> frozenset[str] | None:
    """Return the configured enabled-route allow-list, if one was declared.

    An absent or empty ``routing.routes`` keeps legacy explicit model switching
    available while routing is inert. Once a user declares routes, those route
    targets become the hard allow-list for every explicit/session-pinned path.
    """

    raw_routes = config.get("routes")
    if not isinstance(raw_routes, Mapping) or not raw_routes:
        return None

    from agent.turn_router import normalize_turn_routing_config

    normalized = normalize_turn_routing_config(config)
    return frozenset(
        str(route_id)
        for route_id, raw_route in normalized.get("routes", {}).items()
        if str(route_id) != "current" and RouteTarget.from_mapping(raw_route).enabled
    )


def prepare_turn_route(
    agent: Any,
    request: Any,
    *,
    user_message: Any,
    turn_id: str,
    decision_sink: Callable[[RouteDecision, BudgetRouteContext | None], None]
    | None = None,
) -> Any:
    """Decide and optionally apply one core-owned request-scoped route."""

    state = getattr(request, "session_state", None)
    turn_sequence = 0
    if isinstance(state, TurnRoutingSessionState):
        try:
            state.turn_sequence = max(0, int(state.turn_sequence)) + 1
        except (TypeError, ValueError):
            state.turn_sequence = 1
        turn_sequence = state.turn_sequence
    explicit_reason = None
    if bool(getattr(request, "explicit_moa_override", False)):
        explicit_reason = "explicit_moa_override"
    elif bool(getattr(request, "explicit_turn_override", False)):
        explicit_reason = "explicit_turn_override"
    elif bool(getattr(request, "session_pinned", False)):
        explicit_reason = "session_pin"
    elif bool(getattr(request, "manual_mode", False)):
        explicit_reason = "manual_mode"

    if explicit_reason is not None:
        provider = str(getattr(agent, "provider", "") or "")
        model = str(getattr(agent, "model", "") or "")
        # Authorization must describe the resident runtime that will receive
        # the first provider request.  ``explicit_target`` is useful transport
        # provenance, but a stale surface value must not hide an actual
        # budgeted XAI/Grok runtime from the hard gate.
        resident_target = (
            {"kind": "moa", "preset": model.removeprefix("moa:")}
            if provider.casefold() == "moa"
            else {"kind": "model", "provider": provider, "model": model}
        )
        resident_target = enforce_hard_budget_target(
            RouteTarget.from_mapping(resident_target),
            moa_config=getattr(request, "moa_config", None),
        )
        raw_explicit_target = getattr(request, "explicit_target", None)
        target = (
            enforce_hard_budget_target(
                RouteTarget.from_mapping(raw_explicit_target),
                moa_config=getattr(request, "moa_config", None),
            )
            if isinstance(raw_explicit_target, Mapping)
            else resident_target
        )
        target_kind = target.kind.strip().casefold()
        same_current = False
        if target_kind == "model":
            same_current = (
                str(target.provider or "").strip().casefold()
                == provider.strip().casefold()
                and str(target.model or "").strip().casefold()
                == model.strip().casefold()
            )
        elif target_kind == "moa":
            preset = str(target.preset or "").strip().casefold()
            same_current = (
                provider.strip().casefold() == "moa"
                and model.strip().casefold() in {preset, f"moa:{preset}"}
            )
        should_apply = raw_explicit_target is not None and not same_current
        decision = RouteDecision(
            route="explicit" if should_apply else "current",
            target=target,
            mode="manual",
            source="explicit",
            reason_code=explicit_reason,
            confidence=1.0,
            should_apply=should_apply,
        )
        budget_context = None
        configured_routes = frozenset()
        denial_reason = "target_not_allowed"
        try:
            routing_config = _load_routing_config(request)
            allowed_routes = getattr(request, "allowed_routes", None)
            if allowed_routes is None:
                allowed_routes = _configured_allowed_route_ids(routing_config)
            if allowed_routes is not None:
                configured_routes = _target_route_ids(
                    target,
                    routing_config,
                )
        except Exception:
            allowed_routes = frozenset()
            denial_reason = "allowlist_unavailable"
        if allowed_routes is not None:
            if not (configured_routes & frozenset(allowed_routes)):
                decision = replace(
                    decision,
                    reason_code=denial_reason,
                    should_apply=False,
                    requires_confirmation=False,
                )
        if decision.reason_code not in {"target_not_allowed", "allowlist_unavailable"}:
            decision, budget_context = _authorize_with_core_budget(
                decision,
                request,
                turn_id=turn_id,
            )
        # A stale/malicious surface target cannot hide a resident Grok/xAI
        # runtime that would otherwise receive the first provider request.
        # Gate that actual runtime independently, while keeping the requested
        # target as selection provenance when the resident guard passes.
        if resident_target.budgeted and resident_target != target:
            resident_guard, resident_budget_context = _authorize_with_core_budget(
                replace(
                    decision,
                    route="resident_runtime",
                    target=resident_target,
                    authorization=None,
                    should_apply=False,
                ),
                request,
                turn_id=turn_id,
            )
            resident_authorization = resident_guard.authorization
            if (
                resident_authorization is None
                or not resident_authorization.allowed
                or not str(resident_authorization.reservation_id or "").strip()
            ):
                decision = resident_guard
                budget_context = None
            elif budget_context is None and resident_budget_context is not None:
                budget_context = replace(
                    resident_budget_context,
                    protects_resident_fallback=True,
                )
            elif budget_context is not None:
                budget_context = replace(
                    budget_context,
                    protects_resident_fallback=True,
                )
        if callable(decision_sink):
            decision_sink(decision, budget_context)
        payload = {
            "session_id": str(getattr(request, "session_id", "") or ""),
            "turn_id": str(turn_id or ""),
            "surface": str(getattr(request, "surface", "") or ""),
            **route_decision_payload(decision),
        }
        if turn_sequence > 0:
            payload["turn_sequence"] = turn_sequence
        emit = getattr(request, "emit", None)
        if callable(emit):
            try:
                emit("route.decided", payload)
            except Exception:
                pass
        authorization = decision.authorization
        if decision.reason_code in {"target_not_allowed", "allowlist_unavailable"} or (
            authorization is not None
            and not authorization.allowed
        ) or (
            bool(decision.target.get("budgeted", False))
            and (
                authorization is None
                or not authorization.allowed
                or not str(authorization.reservation_id or "").strip()
            )
        ):
            raise RouteAuthorizationError(decision)
        if not decision.should_apply:
            return None
        token = build_transient_route(agent, decision)
        if token is None:
            raise RouteApplicationError(
                "transient route builder returned no token",
                decision=decision,
            )
        if (
            budget_context is not None
            and budget_context.protects_resident_fallback
            and not bool(decision.target.get("budgeted", False))
        ):
            try:
                budget_context.ledger.release(
                    str(budget_context.reservation.reservation_id),
                    reason_code="route_target_applied",
                )
            except Exception as exc:
                restored = False
                try:
                    restored = token.restore() is not False
                except Exception:
                    restored = False
                raise RouteApplicationError(
                    "resident fallback budget release failed",
                    decision=decision,
                    restore_failed=not restored,
                ) from exc
            budget_context = None
            if callable(decision_sink):
                decision_sink(decision, None)
        if callable(emit):
            try:
                emit("route.applied", payload)
            except Exception:
                pass
        return token

    config = _load_routing_config(request)
    if isinstance(state, TurnRoutingSessionState):
        try:
            state.affinity_window = max(0, int(config.get("affinity_turns", 2)))
        except (TypeError, ValueError):
            state.affinity_window = 2
        try:
            state.failure_limit = max(1, int(config.get("failure_limit", 3)))
        except (TypeError, ValueError):
            state.failure_limit = 3

    request_text = getattr(request, "user_text", "")
    raw_text = request_text if request_text not in (None, "") else user_message
    text = flatten_message_text(raw_text)

    from agent.turn_router import (
        classify_ambiguous_turn,
        decide_turn_route,
        normalize_turn_routing_config,
    )

    normalized = normalize_turn_routing_config(config)
    if normalized["mode"] == "auto" and not bool(
        getattr(request, "allow_automatic", False)
    ):
        config = dict(config)
        config["mode"] = "observe"
        normalized = normalize_turn_routing_config(config)
    resident_provider = str(getattr(agent, "provider", "") or "")
    resident_model = str(getattr(agent, "model", "") or "")
    resident_target = enforce_hard_budget_target(
        RouteTarget.from_mapping(
            (
                {"kind": "moa", "preset": resident_model.removeprefix("moa:")}
                if resident_provider.casefold() == "moa"
                else {
                    "kind": "model",
                    "provider": resident_provider,
                    "model": resident_model,
                }
            )
        ),
        moa_config=getattr(request, "moa_config", None),
    )
    decision = None
    if isinstance(state, TurnRoutingSessionState) and state.fail_off:
        decision = RouteDecision(
            route="current",
            target=RouteTarget(kind="current"),
            mode=str(normalized.get("mode") or "off"),
            source="fail_off",
            reason_code=str(state.fail_off_reason or "route_failure_limit"),
            confidence=1.0,
            should_apply=False,
        )
    elif (
        isinstance(state, TurnRoutingSessionState)
        and state.affinity_route
        and state.affinity_remaining > 0
    ):
        raw_affinity_target = normalized.get("routes", {}).get(state.affinity_route)
        affinity_target = RouteTarget.from_mapping(raw_affinity_target)
        if affinity_target.enabled and affinity_target.kind != "current":
            decision = RouteDecision(
                route=str(state.affinity_route),
                target=affinity_target,
                mode=str(normalized.get("mode") or "off"),
                source="affinity",
                reason_code="sticky_route",
                confidence=1.0,
                should_apply=normalized.get("mode") == "auto",
            )
        else:
            state.affinity_route = None
            state.affinity_target = None
            state.affinity_remaining = 0

    if decision is None:
        decision = decide_turn_route(text, config)
    if decision.mode != "off" and decision.reason_code == "default_route":
        classified = classify_ambiguous_turn(text, config)
        if classified is not None:
            decision = classified
    hard_target = enforce_hard_budget_target(
        decision.target,
        moa_config=getattr(request, "moa_config", None),
    )
    if hard_target != decision.target:
        decision = replace(decision, target=hard_target)

    allowed_routes = getattr(request, "allowed_routes", None)
    budget_context = None
    if decision.mode == "off":
        pass
    elif allowed_routes is not None and decision.route not in allowed_routes:
        decision = replace(
            decision,
            reason_code="target_not_allowed",
            should_apply=False,
            requires_confirmation=False,
        )
    else:
        decision, budget_context = _authorize_with_core_budget(
            decision,
            request,
            turn_id=turn_id,
        )

    # The hard budget follows the runtime that will actually submit the first
    # provider request, even when routing is off/observe or a surface did not
    # declare a manual/session pin.  A semantic recommendation that is not
    # applied must never hide an already-resident Grok/XAI/MoA runtime.
    if resident_target.budgeted and budget_context is None:
        resident_guard, resident_budget_context = _authorize_with_core_budget(
            replace(
                decision,
                route="resident_runtime",
                target=resident_target,
                authorization=None,
                should_apply=False,
            ),
            request,
            turn_id=turn_id,
            force_nonapplying=True,
        )
        resident_authorization = resident_guard.authorization
        if (
            resident_authorization is None
            or not resident_authorization.allowed
            or not str(resident_authorization.reservation_id or "").strip()
            or resident_budget_context is None
        ):
            if callable(decision_sink):
                decision_sink(resident_guard, None)
            raise RouteAuthorizationError(resident_guard)
        budget_context = replace(
            resident_budget_context,
            protects_resident_fallback=True,
        )
        if decision.mode == "off":
            decision = resident_guard

    if decision.mode == "off" and not resident_target.budgeted:
        return None

    target_kind = str(decision.target.get("kind") or "").strip().casefold()
    current_provider = str(getattr(agent, "provider", "") or "").strip().casefold()
    current_model = str(getattr(agent, "model", "") or "").strip().casefold()
    same_current = False
    if target_kind == "model":
        target_provider = str(
            decision.target.get("provider") or ""
        ).strip().casefold()
        target_model = str(decision.target.get("model") or "").strip().casefold()
        same_current = (
            bool(target_model)
            and current_provider == target_provider
            and current_model == target_model
        )
    elif target_kind == "moa":
        preset = str(decision.target.get("preset") or "").strip().casefold()
        same_current = (
            current_provider == "moa"
            and current_model in {preset, f"moa:{preset}"}
        )
    if decision.should_apply and same_current:
        decision = replace(
            decision,
            reason_code="same_current",
            should_apply=False,
        )

    if callable(decision_sink):
        decision_sink(decision, budget_context)

    payload = {
        "session_id": str(getattr(request, "session_id", "") or ""),
        "turn_id": str(turn_id or ""),
        "surface": str(getattr(request, "surface", "") or ""),
        **route_decision_payload(decision),
    }
    if turn_sequence > 0:
        payload["turn_sequence"] = turn_sequence
    emit = getattr(request, "emit", None)
    if callable(emit):
        try:
            emit("route.decided", payload)
        except Exception:
            pass

    if not decision.should_apply:
        return None

    token = build_transient_route(agent, decision)
    if token is None:
        raise RouteApplicationError(
            "transient route builder returned no token",
            decision=decision,
        )
    if (
        budget_context is not None
        and budget_context.protects_resident_fallback
        and not bool(decision.target.get("budgeted", False))
    ):
        try:
            budget_context.ledger.release(
                str(budget_context.reservation.reservation_id),
                reason_code="route_target_applied",
            )
        except Exception as exc:
            restored = False
            try:
                restored = token.restore() is not False
            except Exception:
                restored = False
            raise RouteApplicationError(
                "resident fallback budget release failed",
                decision=decision,
                restore_failed=not restored,
            ) from exc
        budget_context = None
        if callable(decision_sink):
            decision_sink(decision, None)
    if callable(emit):
        try:
            emit("route.applied", payload)
        except Exception:
            pass
    return token


@contextmanager
def turn_routing_lifecycle(agent: Any, request: Any):
    """Own one opt-in route scope across the complete conversation call.

    The lifecycle is intentionally inert until ``build_turn_context`` prepares
    it with a real turn id. Surfaces provide typed request facts and event /
    quarantine callbacks; this shared lifecycle exclusively owns apply and
    verified restore.
    """

    lifecycle = TurnRoutingLifecycle(agent=agent, request=request)
    try:
        yield lifecycle
    except BaseException:
        lifecycle.mark_turn_failed("turn_exception")
        raise
    finally:
        lifecycle.finish()


def _resolve_route_switch(
    agent: Any,
    decision: RouteDecision,
    *,
    switch_resolver: Callable[..., Any] | None = None,
    user_providers: dict[str, Any] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
) -> Any:
    target = decision.target
    kind = str(target.get("kind") or "").strip().lower()
    if kind == "model":
        raw_input = str(target.get("model") or "").strip()
        explicit_provider = str(target.get("provider") or "").strip()
    elif kind == "moa":
        raw_input = str(target.get("preset") or "").strip()
        explicit_provider = "moa"
    else:
        raise RouteApplicationError(
            f"Unsupported route target kind: {kind or '<empty>'}"
        )
    if not raw_input:
        raise RouteApplicationError(
            f"Route {decision.route!r} has no model or preset"
        )

    if switch_resolver is None:
        from hermes_cli.model_switch import switch_model as switch_resolver

    result = switch_resolver(
        raw_input=raw_input,
        current_provider=str(getattr(agent, "provider", "") or ""),
        current_model=str(getattr(agent, "model", "") or ""),
        current_base_url=str(getattr(agent, "base_url", "") or ""),
        current_api_key=str(getattr(agent, "api_key", "") or ""),
        explicit_provider=explicit_provider,
        user_providers=user_providers,
        custom_providers=custom_providers,
    )
    if not getattr(result, "success", False):
        message = str(
            getattr(result, "error_message", "")
            or "Route model resolution failed"
        )
        raise RouteApplicationError(message)
    return result


def _transient_resources(
    agent: Any,
    snapshot: TransientRuntimeSnapshot,
) -> tuple[Any, ...]:
    original_ids = snapshot.original_resource_ids
    resources = []
    seen: set[int] = set()
    for name in ("client", "_anthropic_client"):
        resource = getattr(agent, name, None)
        resource_id = id(resource)
        if (
            resource is None
            or resource_id in original_ids
            or resource_id in seen
        ):
            continue
        seen.add(resource_id)
        resources.append(resource)
    return tuple(resources)


def build_transient_route(
    agent: Any,
    decision: RouteDecision,
    *,
    switch_resolver: Callable[..., Any] | None = None,
    runtime_switcher: Callable[..., Any] | None = None,
    user_providers: dict[str, Any] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
) -> TransientRouteToken | None:
    """Resolve and apply one request-scoped route without persistent switching."""

    kind = str(decision.target.get("kind") or "").strip().lower()
    if not decision.should_apply or kind == "current":
        return None

    try:
        result = _resolve_route_switch(
            agent,
            decision,
            switch_resolver=switch_resolver,
            user_providers=user_providers,
            custom_providers=custom_providers,
        )
    except RouteApplicationError as exc:
        raise RouteApplicationError(
            str(exc),
            restore_failed=exc.restore_failed,
            decision=decision,
        ) from exc
    if runtime_switcher is None:
        from agent.agent_runtime_helpers import switch_model as runtime_switcher

    snapshot = TransientRuntimeSnapshot.capture(agent)
    try:
        runtime_switcher(
            agent,
            new_model=result.new_model,
            new_provider=result.target_provider,
            api_key=result.api_key,
            base_url=result.base_url,
            api_mode=result.api_mode,
            persist=False,
        )
    except Exception as exc:
        resources = _transient_resources(agent, snapshot)
        token = TransientRouteToken(
            agent=agent,
            snapshot=snapshot,
            decision=decision,
            transient_resources=resources,
        )
        restored = token.restore()
        raise RouteApplicationError(
            str(exc) or type(exc).__name__,
            restore_failed=not restored,
            decision=decision,
        ) from exc

    return TransientRouteToken(
        agent=agent,
        snapshot=snapshot,
        decision=decision,
        transient_resources=_transient_resources(agent, snapshot),
    )
