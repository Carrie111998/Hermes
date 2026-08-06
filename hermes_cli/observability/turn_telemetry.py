"""Content-free, per-turn operational accounting.

This observer deliberately keeps a closed data model: route identifiers,
numeric counters, lineage, timing, and fixed terminal classifications only.
It never persists prompts, messages, tool data, provider payloads, headers,
exception messages, or arbitrary metadata.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Optional
import uuid

logger = logging.getLogger(__name__)

_HANDLED_HOOKS = frozenset(
    {"pre_api_request", "post_api_request", "api_request_error"}
)
_CURRENT_TURN: contextvars.ContextVar[Optional["_TurnCollector"]] = (
    contextvars.ContextVar("hermes_turn_telemetry", default=None)
)

RECORD_VERSION = 1
_TURN_ID_PREFIX = "turn-"
_GATEWAY_TURN_ID_PREFIX = "gateway-"
_EVENT_TYPES = frozenset({"turn_terminal", "gateway_terminal"})
_TASK_CLASSES = frozenset(
    {"interactive", "scheduled", "delegated", "agent_turn", "gateway_preflight"}
)
_ROUTE_TYPES = frozenset(
    {
        "primary",
        "explicit_override",
        "delegated_specialist",
        "fallback",
        "auxiliary",
        "local_triage",
    }
)
_DISPOSITIONS = frozenset(
    {
        "completed",
        "retried",
        "fell_back",
        "triaged",
        "held",
        "refused",
        "failed",
        "cancelled",
    }
)
_OUTCOMES = frozenset({"success", "held", "refused", "failed", "cancelled"})
_COST_STATUSES = frozenset({"actual", "estimated", "included", "unknown"})

_FAILURE_CLASSES = frozenset(
    {
        "auth",
        "billing",
        "cancelled",
        "connection",
        "content_filter",
        "gateway_preflight",
        "gateway_refused",
        "triage_held",
        "refused",
        "incomplete",
        "internal_error",
        "interrupted",
        "max_iterations",
        "provider",
        "rate_limit",
        "timeout",
        "unknown",
    }
)


def _bounded_identifier(value: Any, limit: int = 256) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return text.strip()[:limit]


def new_turn_id() -> str:
    """Return a bounded opaque UUID4 identity for one logical agent turn."""
    return f"{_TURN_ID_PREFIX}{uuid.uuid4().hex}"


def _new_gateway_turn_id() -> str:
    """Return an opaque UUID4 identity for one pre-agent gateway terminal."""
    return f"{_GATEWAY_TURN_ID_PREFIX}{uuid.uuid4().hex}"


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _route(provider: Any, model: Any) -> tuple[str, str]:
    return _bounded_identifier(provider), _bounded_identifier(model)


def _source_name(agent: Any) -> str:
    source = getattr(agent, "platform", "") or ""
    return _bounded_identifier(getattr(source, "value", source), 128)


def _task_class(source: str, *, is_delegated: bool) -> str:
    if is_delegated:
        return "delegated"
    if source == "cron":
        return "scheduled"
    if source in {"cli", "tui", "desktop"}:
        return "interactive"
    return "agent_turn"


def _merge_cost_status(current: str, candidate: Any) -> str:
    candidate_text = _bounded_identifier(candidate, 32).lower()
    if candidate_text not in _COST_STATUSES:
        candidate_text = "unknown"
    priority = {"unknown": 0, "included": 1, "estimated": 2, "actual": 3}
    return candidate_text if priority[candidate_text] > priority[current] else current


def _nonnegative_float(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return 0.0
    return number


def _active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return _bounded_identifier(get_active_profile_name() or "default", 128)
    except Exception:
        return "default"


def _failure_from_status(status_code: Any) -> str:
    try:
        status = int(status_code)
    except (TypeError, ValueError, OverflowError):
        return ""
    if status in {401, 403}:
        return "auth"
    if status == 402:
        return "billing"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "provider"
    return ""


def _classify_token(value: Any) -> str:
    """Map a result/error label to a fixed class without retaining its text."""
    token = _bounded_identifier(value, 160).lower().replace("-", "_").replace(" ", "_")
    if token in _FAILURE_CLASSES:
        return token
    if "cancel" in token:
        return "cancelled"
    if "interrupt" in token:
        return "interrupted"
    if "rate" in token and "limit" in token or "429" in token or "quota" in token:
        return "rate_limit"
    if "bill" in token or "credit" in token or "payment" in token:
        return "billing"
    if "auth" in token or "credential" in token or "unauthorized" in token:
        return "auth"
    if "timeout" in token or "timed_out" in token:
        return "timeout"
    if "connection" in token or "transport" in token or "network" in token:
        return "connection"
    if (
        "content_filter" in token
        or "contentfilter" in token
        or "content_policy" in token
        or ("policy" in token and ("block" in token or "refus" in token))
    ):
        return "content_filter"
    if "max_iteration" in token or "iteration_limit" in token:
        return "max_iterations"
    if "incomplete" in token or "partial" in token or "truncat" in token:
        return "incomplete"
    if "provider" in token or "api_error" in token:
        return "provider"
    return "unknown" if token else ""


def _classify_exception(error: BaseException) -> str:
    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, InterruptedError)):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    status_class = _failure_from_status(getattr(error, "status_code", None))
    if status_class:
        return status_class
    return _classify_token(type(error).__name__) or "internal_error"


def _usage_value(usage: Any, *keys: str) -> int:
    for key in keys:
        if isinstance(usage, dict):
            value = usage.get(key)
        else:
            value = getattr(usage, key, None)
        if value is not None:
            return _nonnegative_int(value)
    return 0


@dataclass
class _TurnCollector:
    agent: Any
    turn_id: str
    session_id: str
    parent_session_id: str
    parent_turn_id: str
    profile_name: str
    requested_profile: str
    source: str
    task_class: str
    is_delegated: bool
    requested_provider: str
    requested_model: str
    started_at: float
    started_monotonic: float
    started_cost_usd: float
    started_cost_status: str
    main_initial_route: tuple[str, str]
    main_last_route: tuple[str, str]
    main_request_ids: set[str] = field(default_factory=set)
    auxiliary_request_ids: set[str] = field(default_factory=set)
    auxiliary_last_routes: dict[str, tuple[str, str]] = field(default_factory=dict)
    main_attempt_count: int = 0
    auxiliary_attempt_count: int = 0
    retry_count: int = 0
    fallback_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    auxiliary_cost_usd: float = 0.0
    cost_status: str = "unknown"
    last_failure_class: str = ""
    finalized: bool = False

    def _add_usage(self, usage: Any) -> None:
        # Normalize the full event before mutating aggregate state. A malformed
        # observer payload is therefore all-or-nothing and remains non-fatal.
        input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
        cache_read_tokens = _usage_value(usage, "cache_read_tokens")
        cache_write_tokens = _usage_value(usage, "cache_write_tokens")
        reasoning_tokens = _usage_value(usage, "reasoning_tokens")
        explicit_total = _usage_value(usage, "total_tokens")
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        self.reasoning_tokens += reasoning_tokens
        self.total_tokens += explicit_total if explicit_total else input_tokens + output_tokens

    def _add_cost(self, amount_usd: Any, status: Any) -> None:
        amount = _nonnegative_float(amount_usd)
        cost_status = _merge_cost_status(self.cost_status, status)
        self.auxiliary_cost_usd += amount
        self.cost_status = cost_status

    def note_main_attempt(self, kwargs: dict[str, Any]) -> None:
        request_id = _bounded_identifier(kwargs.get("api_request_id"), 512)
        route = _route(kwargs.get("provider"), kwargs.get("model"))
        if not request_id:
            request_id = f"main-{self.main_attempt_count + 1}"
        if request_id in self.main_request_ids:
            self.retry_count += 1
        else:
            self.main_request_ids.add(request_id)
        self.main_attempt_count += 1

        if route != ("", ""):
            if self.main_last_route != ("", "") and route != self.main_last_route:
                self.fallback_count += 1
            self.main_last_route = route

    def note_main_error(self, kwargs: dict[str, Any]) -> None:
        classification = _failure_from_status(kwargs.get("status_code"))
        error = kwargs.get("error")
        error_type = error.get("type") if isinstance(error, dict) else None
        classification = classification or _classify_token(error_type)
        # `reason` can be a stable internal enum or arbitrary provider text. It
        # is used only for fixed classification and is never retained.
        classification = classification or _classify_token(kwargs.get("reason"))
        if classification:
            self.last_failure_class = classification

    def note_auxiliary_attempt(
        self,
        *,
        request_id: Any,
        provider: Any,
        model: Any,
        retry_count: Any,
    ) -> None:
        logical_id = _bounded_identifier(request_id, 512)
        route = _route(provider, model)
        published_retry_count = _nonnegative_int(retry_count)
        if not logical_id:
            logical_id = f"aux-{self.auxiliary_attempt_count + 1}"
        if logical_id in self.auxiliary_request_ids:
            self.retry_count += 1
        else:
            self.auxiliary_request_ids.add(logical_id)
        # Preserve an explicitly published retry count when a wrapper entered
        # after an earlier attempt that this context did not observe.
        self.retry_count = max(self.retry_count, published_retry_count)
        self.auxiliary_attempt_count += 1

        previous = self.auxiliary_last_routes.get(logical_id)
        if previous is not None and route != ("", "") and route != previous:
            self.fallback_count += 1
        if route != ("", ""):
            self.auxiliary_last_routes[logical_id] = route


@dataclass
class TurnBinding:
    """Opaque ContextVar ownership for one logical turn."""

    collector: Optional[_TurnCollector]
    token: Optional[contextvars.Token]


def begin_turn(agent: Any, turn_id: str, *, started_at: Optional[float] = None) -> TurnBinding:
    """Bind a best-effort content-free collector before any model call."""
    try:
        ambient = _CURRENT_TURN.get()
        primary = getattr(agent, "_primary_runtime", None)
        primary = primary if isinstance(primary, dict) else {}
        provider = _bounded_identifier(
            primary.get("provider") or getattr(agent, "provider", "")
        )
        model = _bounded_identifier(primary.get("model") or getattr(agent, "model", ""))
        requested_provider = _bounded_identifier(
            getattr(agent, "requested_provider", "")
            or primary.get("requested_provider")
            or provider
        )
        profile_name = _active_profile_name()
        requested_profile = _bounded_identifier(
            getattr(agent, "_delegate_profile_name", "") or profile_name,
            128,
        )
        source = _source_name(agent)
        is_delegated = bool(
            getattr(agent, "_parent_session_id", "")
            or getattr(agent, "is_subagent", False)
        )
        collector = _TurnCollector(
            agent=agent,
            turn_id=_bounded_identifier(turn_id, 512),
            session_id=_bounded_identifier(getattr(agent, "session_id", ""), 512),
            parent_session_id=_bounded_identifier(
                getattr(agent, "_parent_session_id", ""), 512
            ),
            parent_turn_id=_bounded_identifier(
                getattr(agent, "_parent_turn_id", "")
                or (ambient.turn_id if ambient is not None else ""),
                512,
            ),
            profile_name=profile_name,
            requested_profile=requested_profile,
            source=source,
            task_class=_task_class(source, is_delegated=is_delegated),
            is_delegated=is_delegated,
            requested_provider=requested_provider,
            requested_model=model,
            started_at=float(started_at if started_at is not None else time.time()),
            started_monotonic=time.monotonic(),
            started_cost_usd=_nonnegative_float(
                getattr(agent, "session_estimated_cost_usd", 0.0)
            ),
            started_cost_status=_bounded_identifier(
                getattr(agent, "session_cost_status", "unknown"), 32
            ).lower(),
            main_initial_route=(provider, model),
            main_last_route=(provider, model),
        )
        if not collector.turn_id or not collector.session_id:
            return TurnBinding(None, None)
        return TurnBinding(collector, _CURRENT_TURN.set(collector))
    except Exception as exc:
        # Exception bodies may contain provider or caller data. Telemetry
        # diagnostics retain only the class, just like the durable record.
        logger.debug(
            "Turn telemetry initialization failed (%s)", type(exc).__name__
        )
        return TurnBinding(None, None)


def handles_hook(hook_name: str) -> bool:
    return hook_name in _HANDLED_HOOKS


def _observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    collector = _CURRENT_TURN.get()
    if collector is None or collector.finalized or hook_name not in _HANDLED_HOOKS:
        return
    event_turn = _bounded_identifier(kwargs.get("turn_id"), 512)
    event_session = _bounded_identifier(kwargs.get("session_id"), 512)
    if event_turn and event_turn != collector.turn_id:
        return
    if event_session and event_session != collector.session_id:
        return
    if hook_name == "pre_api_request":
        collector.note_main_attempt(kwargs)
    elif hook_name == "post_api_request":
        collector._add_usage(kwargs.get("usage"))
        route = _route(kwargs.get("provider"), kwargs.get("model"))
        if route != ("", ""):
            collector.main_last_route = route
    elif hook_name == "api_request_error":
        collector.note_main_error(kwargs)


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Consume fixed hook metadata without ever affecting ordinary calls."""
    try:
        _observe_lifecycle(hook_name, **kwargs)
    except Exception as exc:
        logger.debug("Turn telemetry hook failed (%s)", type(exc).__name__)


def record_auxiliary_attempt(
    *,
    request_id: str,
    task: str = "",
    provider: str = "",
    model: str = "",
    retry_count: int = 0,
) -> None:
    """Record one physical auxiliary attempt; `task` is intentionally discarded."""
    del task
    collector = _CURRENT_TURN.get()
    if collector is None or collector.finalized:
        return
    collector.note_auxiliary_attempt(
        request_id=request_id,
        provider=provider,
        model=model,
        retry_count=retry_count,
    )


def record_auxiliary_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost_usd: Optional[float] = None,
    cost_status: str = "unknown",
) -> None:
    collector = _CURRENT_TURN.get()
    if collector is None or collector.finalized:
        return
    collector._add_usage(
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
        }
    )
    collector._add_cost(estimated_cost_usd, cost_status)


def record_auxiliary_terminal(
    *,
    request_id: str,
    outcome: str,
    error: Optional[BaseException] = None,
    error_message: Any = None,
) -> None:
    """Classify an auxiliary terminal without retaining either error payload."""
    del request_id, error_message
    collector = _CURRENT_TURN.get()
    if collector is None or collector.finalized:
        return
    if error is not None:
        collector.last_failure_class = _classify_exception(error)
    elif _bounded_identifier(outcome, 32).lower() not in {"success", "completed"}:
        collector.last_failure_class = _classify_token(outcome) or "unknown"


def _terminal_classification(
    collector: _TurnCollector,
    result: Any,
    error: Optional[BaseException],
) -> tuple[str, str]:
    if error is not None:
        failure = _classify_exception(error)
        if failure == "cancelled":
            return "cancelled", "cancelled"
        if failure in {"content_filter", "refused"}:
            return "refused", failure
        return "failed", failure or "internal_error"
    if isinstance(result, dict):
        status = _bounded_identifier(result.get("status"), 32).lower()
        if result.get("interrupted"):
            return "cancelled", "cancelled"
        if (
            result.get("held") is True
            or status == "held"
            or result.get("compression_deferred") is True
        ):
            return "held", "triage_held"
        if result.get("refused") is True or status == "refused":
            return "refused", "refused"
        if result.get("failed") is True:
            failure = _classify_token(
                result.get("failure_reason")
                or result.get("turn_exit_reason")
                or result.get("error")
            )
            failure = failure or collector.last_failure_class or "unknown"
            if failure in {"content_filter", "refused"}:
                return "refused", failure
            return "failed", failure
        if result.get("completed") is False:
            if result.get("interrupt_message") or result.get("cancelled"):
                return "cancelled", "cancelled"
            failure = _classify_token(
                result.get("failure_reason")
                or result.get("turn_exit_reason")
                or result.get("error")
            )
            failure = failure or collector.last_failure_class or "incomplete"
            if failure in {"content_filter", "refused"}:
                return "refused", failure
            return "failed", failure
    return "success", ""


def _route_type(collector: _TurnCollector) -> str:
    if collector.fallback_count:
        return "fallback"
    if collector.is_delegated:
        return "delegated_specialist"
    if collector.main_attempt_count == 0 and collector.auxiliary_attempt_count:
        return "auxiliary"
    initial_provider, _ = collector.main_initial_route
    if (
        collector.requested_provider
        and initial_provider
        and collector.requested_provider != initial_provider
    ):
        return "explicit_override"
    return "primary"


def _disposition(
    collector: _TurnCollector, outcome: str, route_type: str = ""
) -> str:
    if route_type == "local_triage":
        return "triaged"
    if outcome == "held":
        return "held"
    if outcome == "refused":
        return "refused"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "failed":
        return "failed"
    if collector.fallback_count:
        return "fell_back"
    if collector.retry_count:
        return "retried"
    return "completed"


def _reset_binding(binding: TurnBinding) -> None:
    token = binding.token
    binding.token = None
    if token is None:
        return
    try:
        _CURRENT_TURN.reset(token)
    except (RuntimeError, ValueError):
        # A copied Context owns values, not the originating token. Never let
        # cleanup of observation-only state alter a turn unwind.
        pass


def reset_turn(binding: TurnBinding) -> None:
    """Idempotently release observation context at the caller cleanup boundary."""
    _reset_binding(binding)


def finish_turn(
    binding: TurnBinding,
    *,
    result: Any = None,
    error: Optional[BaseException] = None,
    ended_at: Optional[float] = None,
) -> None:
    """Persist one terminal row and always restore the prior collector."""
    collector = binding.collector
    if collector is None or collector.finalized:
        _reset_binding(binding)
        return
    collector.finalized = True
    try:
        wall_ended = float(ended_at if ended_at is not None else time.time())
        if ended_at is None:
            duration_ms = max(
                0, round((time.monotonic() - collector.started_monotonic) * 1000)
            )
        else:
            duration_ms = max(0, round((wall_ended - collector.started_at) * 1000))
        outcome, failure_class = _terminal_classification(collector, result, error)
        route_type = _route_type(collector)
        disposition = _disposition(collector, outcome, route_type)
        effective_provider, effective_model = collector.main_last_route
        if effective_provider == "" and effective_model == "":
            effective_provider, effective_model = _route(
                getattr(collector.agent, "provider", ""),
                getattr(collector.agent, "model", ""),
            )
        current_cost = _nonnegative_float(
            getattr(collector.agent, "session_estimated_cost_usd", 0.0)
        )
        main_cost = max(0.0, current_cost - collector.started_cost_usd)
        cost_status = collector.cost_status
        current_cost_status = _bounded_identifier(
            getattr(collector.agent, "session_cost_status", "unknown"), 32
        ).lower()
        if (
            collector.main_attempt_count
            or current_cost != collector.started_cost_usd
            or current_cost_status != collector.started_cost_status
        ):
            cost_status = _merge_cost_status(cost_status, current_cost_status)
        db = getattr(collector.agent, "_session_db", None)
        if db is None:
            return
        db.record_turn_telemetry(
            event_type="turn_terminal",
            turn_id=collector.turn_id,
            correlation_id=collector.turn_id,
            session_id=collector.session_id,
            parent_session_id=collector.parent_session_id,
            parent_turn_id=collector.parent_turn_id,
            profile_name=collector.profile_name,
            requested_profile=collector.requested_profile,
            effective_profile=_active_profile_name(),
            source=collector.source,
            platform=collector.source,
            task_class=collector.task_class,
            route_type=route_type,
            disposition=disposition,
            is_delegated=collector.is_delegated,
            started_at=collector.started_at,
            ended_at=wall_ended,
            duration_ms=duration_ms,
            requested_provider=collector.requested_provider,
            requested_model=collector.requested_model,
            effective_provider=effective_provider,
            effective_model=effective_model,
            attempt_count=collector.main_attempt_count,
            retry_count=collector.retry_count,
            fallback_count=collector.fallback_count,
            auxiliary_attempt_count=collector.auxiliary_attempt_count,
            input_tokens=collector.input_tokens,
            output_tokens=collector.output_tokens,
            cache_read_tokens=collector.cache_read_tokens,
            cache_write_tokens=collector.cache_write_tokens,
            reasoning_tokens=collector.reasoning_tokens,
            total_tokens=collector.total_tokens,
            estimated_cost_usd=main_cost + collector.auxiliary_cost_usd,
            cost_status=cost_status,
            outcome=outcome,
            failure_class=failure_class,
            recorded_at=time.time(),
            record_version=RECORD_VERSION,
        )
    except Exception as exc:
        # Ordinary observer faults cannot alter the primary turn result. Newly
        # delivered BaseException signals propagate after the binding is reset.
        logger.debug("Turn telemetry finalization failed (%s)", type(exc).__name__)
    finally:
        _reset_binding(binding)


def record_gateway_terminal(
    db: Any,
    *,
    session_id: str,
    turn_id: str = "",
    source: str = "gateway",
    failure_class: str = "gateway_preflight",
    requested_provider: str = "",
    requested_model: str = "",
    error_message: Any = None,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
) -> None:
    """Record a terminal path with an existing opaque session ID or event ID."""
    del turn_id, error_message
    if db is None:
        return
    try:
        started = float(started_at if started_at is not None else time.time())
        ended = float(ended_at if ended_at is not None else time.time())
        terminal_turn_id = _new_gateway_turn_id()
        terminal_session_id = _bounded_identifier(session_id, 512) or terminal_turn_id
        profile_name = _active_profile_name()
        platform = _bounded_identifier(source, 128)
        db.record_turn_telemetry(
            event_type="gateway_terminal",
            turn_id=terminal_turn_id,
            correlation_id=terminal_turn_id,
            session_id=terminal_session_id,
            parent_session_id="",
            parent_turn_id="",
            profile_name=profile_name,
            requested_profile=profile_name,
            effective_profile=profile_name,
            source=platform,
            platform=platform,
            task_class="gateway_preflight",
            route_type="local_triage",
            disposition="triaged",
            is_delegated=False,
            started_at=started,
            ended_at=max(started, ended),
            duration_ms=max(0, round((ended - started) * 1000)),
            requested_provider=_bounded_identifier(requested_provider),
            requested_model=_bounded_identifier(requested_model),
            effective_provider="",
            effective_model="",
            attempt_count=0,
            retry_count=0,
            fallback_count=0,
            auxiliary_attempt_count=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            total_tokens=0,
            estimated_cost_usd=0.0,
            cost_status="unknown",
            outcome="refused",
            failure_class=_classify_token(failure_class) or "gateway_preflight",
            recorded_at=time.time(),
            record_version=RECORD_VERSION,
        )
    except Exception as exc:
        logger.debug("Gateway terminal telemetry failed (%s)", type(exc).__name__)
