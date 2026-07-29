"""Exact-address multi-customer coordination beside the personal coach flow."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import inspect
import json
import logging
import os
import sys
import fcntl
from contextlib import contextmanager, nullcontext
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
from gateway.platforms.physique_checkin import CallbackData, PhysiqueCheckinBridge, WizardPrompt, WizardReply
from gateway.platforms.physique_checkin_config import PhysiqueCheckinConfig
try:
    from checkin_cli.operator_console import (
        TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16,
        telegram_utf16_length,
    )
except ImportError:
    TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16 = 4096

    def telegram_utf16_length(text: str) -> int:
        """Return Telegram's single-message length in UTF-16 code units."""
        return len(text.encode("utf-16-le")) // 2

try:
    from checkin_cli.customer_coaching import CustomerRuntime, RegisteredCustomerBinding
    from checkin_cli.store import CanonicalEventTransaction
    from checkin_cli.adaptive_nutrition import (
        AdaptiveEventStore,
        ApprovedAdaptiveArtifacts,
        CustomerPolicy,
        Decision,
        DailyObservation,
        MacroTarget,
        MealConstraints,
        TrendSnapshot,
        NutritionProposal,
        MealPlan,
        MealSlot,
        build_snapshot,
        canonical_event_digest,
        canonical_json,
        compile_meal_plan,
        digest,
        load_approved_adaptive_artifacts,
        load_verified_dual_coach_risk_policy,
        project_canonical_events,
        propose,
        render_customer_body,
        render_operator_card,
        validate_explanation,
        solve_macros,
        validate_typed_safety,
    )
except ImportError:
    AdaptiveEventStore = None
    CustomerRuntime = None
    RegisteredCustomerBinding = None
    CanonicalEventTransaction = None
    CustomerPolicy = None
    Decision = None
    DailyObservation = None
    MacroTarget = None
    MealConstraints = None
    TrendSnapshot = None
    NutritionProposal = None
    MealPlan = None
    MealSlot = None
    build_snapshot = None
    canonical_json = None
    propose = None
    render_customer_body = None
    render_operator_card = None
    validate_explanation = None
    ApprovedAdaptiveArtifacts = None
    canonical_event_digest = None
    compile_meal_plan = None
    digest = None
    load_approved_adaptive_artifacts = None
    load_verified_dual_coach_risk_policy = None
    project_canonical_events = None
    validate_typed_safety = None
    feature_config_digest = None
    solve_macros = None
try:
    from checkin_cli.adaptive_nutrition import feature_config_digest
except ImportError:
    feature_config_digest = None
try:
    from checkin_cli.adaptive_nutrition import (
        CooldownResult,
        DailyNutritionTarget,
        WeeklyCarbCycle,
    )
except ImportError:
    CooldownResult = None
    DailyNutritionTarget = None
    WeeklyCarbCycle = None
try:
    from checkin_cli.customer_admin import (
        load_approved_adaptive_registration_inputs,
        reconcile_adaptive_nutrition_journals,
        profile_authority_lock,
        validate_review_space_disjoint,
    )
    from checkin_cli.customer_coaching import (
        AdaptiveRegistrationInputs,
        CustomerTrainingScheduleEntry,
    )
except ImportError:
    load_approved_adaptive_registration_inputs = None
    reconcile_adaptive_nutrition_journals = None
    profile_authority_lock = None
    validate_review_space_disjoint = None
    AdaptiveRegistrationInputs = None
    CustomerTrainingScheduleEntry = None

if TYPE_CHECKING:
    from checkin_cli.customer_coaching import CustomerRegistry, CustomerRuntime
    from checkin_cli.customer_grounding import CustomerSnapshot
_KST = ZoneInfo("Asia/Seoul")
_PRIVATE_TRAINER_SELECTION_PREFIX = "pt1"
_PRIVATE_TRAINER_SELECTION_TOKEN: re.Pattern[str] = re.compile(r"^[a-f0-9]{24}$")
_CUSTOMER_START_CALLBACK_PREFIX = "cs1"
_CUSTOMER_START_CALLBACK_TOKEN: re.Pattern[str] = re.compile(r"^[a-f0-9]{24}$")
_ADAPTIVE_SHADOW_FACTORY_TOKEN = object()
@dataclass(frozen=True, slots=True)
class _AdaptiveRegistrationBinding:
    """Canonical registration revision and its committed derived constraints."""

    registration_digest: str
    meal_constraints: object
    meal_constraints_digest: str
    policy_digest: str
    inputs: object
    catalog_digest: str

@dataclass(frozen=True, slots=True)
class AdaptiveCoachingFacts:
    """Exact non-rendered projection used by the adaptive coaching pipeline."""

    evaluation_day: str
    goal_mode: str
    goal_range: tuple[str, str]
    current_mean_kg: str | None
    prior_mean_kg: str | None
    weekly_rate_percent: str | None
    decision: str
    reason_category_ids: tuple[str, ...]
    target_macros: tuple[tuple[str, int], ...]
    carb_category_targets: tuple[
        tuple[str, tuple[tuple[str, int], ...]], ...
    ]
    safety_held: bool
    approval_state: str
    delivery_state: str
    proposal_digest: str
    revision: int
    revision_binding_digest: str
    source_cluster_ids: tuple[str, ...] = ("adaptive-proposal",)
    excluded_risk_ids: tuple[str, ...] = ("medical", "unsafe_nutrition")

    @property
    def proposal_revision_binding_digest(self) -> str:
        """Compatibility name for callers that bind both proposal and revision."""
        return self.revision_binding_digest


def _coaching_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coaching_text(value: object, *, limit: int = 80) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    compact = " ".join(str(value).split()).strip()
    return compact[:limit] if compact else None

_COACHING_SCALAR_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_COACHING_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_COACHING_OPAQUE_RE = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_ADAPTIVE_GOAL_MODES = frozenset(
    {"lean_mass_gain", "fat_loss", "maintenance", "unknown"}
)
_ADAPTIVE_CARD_STATES = frozenset(
    {
        "proposed",
        "edited",
        "released",
        "held",
        "approved",
        "activated",
        "delivery_enabled",
        "delivery_revoked",
    }
)
_COACHING_MISSING = object()


def _strict_coaching_text(
    value: object,
    *,
    field: str,
    allow_none: bool = False,
    scalar: bool = False,
    opaque: bool = False,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    try:
        text = " ".join(str(value).split()).strip()
    except Exception as exc:
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid") from exc
    if not text or len(text) > 80:
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    if scalar and _COACHING_SCALAR_RE.fullmatch(text) is None:
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    if opaque and _COACHING_OPAQUE_RE.fullmatch(text) is None:
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    return text


def _coaching_macro_pairs(value: object, *, field: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, tuple) or len(value) > 5:
        raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
    allowed = {"calories", "calories_kcal", "carbs_g", "protein_g", "fat_g"}
    result: list[tuple[str, int]] = []
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or item[0] not in allowed
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
            or item[1] < 0
            or item[1] > 10_000
        ):
            raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
        result.append((item[0], item[1]))
    return tuple(result)
def _coaching_macro_projection(value: object, *, field: str) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    pairs: list[tuple[str, int]] = []
    for name in ("calories", "carbs_g", "protein_g", "fat_g"):
        try:
            raw = getattr(value, name, _COACHING_MISSING)
        except Exception as exc:
            raise AdaptiveWorkflowError(f"adaptive {field} is invalid") from exc
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 10_000:
            raise AdaptiveWorkflowError(f"adaptive {field} is invalid")
        pairs.append((name, raw))
    return tuple(pairs)


def _adaptive_binding_projection(
    customer_key: object,
    facts: AdaptiveCoachingFacts,
) -> Mapping[str, object]:
    """Return the validated, complete typed projection used for revision binding."""
    if type(facts) is not AdaptiveCoachingFacts:
        raise AdaptiveWorkflowError("adaptive coaching facts are invalid")
    key = _strict_coaching_text(customer_key, field="coaching customer")
    if key is None:
        raise AdaptiveWorkflowError("adaptive coaching customer is invalid")
    if (
        not isinstance(facts.evaluation_day, str)
        or _COACHING_DATE_RE.fullmatch(facts.evaluation_day) is None
        or not isinstance(facts.goal_mode, str)
        or facts.goal_mode not in _ADAPTIVE_GOAL_MODES
        or not isinstance(facts.goal_range, tuple)
        or len(facts.goal_range) != 2
        or any(
            not isinstance(value, str)
            or (value and _COACHING_SCALAR_RE.fullmatch(value) is None)
            for value in facts.goal_range
        )
    ):
        raise AdaptiveWorkflowError("adaptive coaching facts are invalid")
    for field in ("current_mean_kg", "prior_mean_kg", "weekly_rate_percent"):
        value = getattr(facts, field)
        if (
            value is not None
            and (
                not isinstance(value, str)
                or _COACHING_SCALAR_RE.fullmatch(value) is None
            )
        ):
            raise AdaptiveWorkflowError("adaptive coaching facts are invalid")
    if (
        not isinstance(facts.decision, str)
        or _COACHING_OPAQUE_RE.fullmatch(facts.decision) is None
        or not isinstance(facts.reason_category_ids, tuple)
        or len(facts.reason_category_ids) > 8
        or any(
            not isinstance(value, str)
            or _COACHING_OPAQUE_RE.fullmatch(value) is None
            for value in facts.reason_category_ids
        )
        or type(facts.safety_held) is not bool
        or not isinstance(facts.approval_state, str)
        or facts.approval_state not in {"pending", "approved", "held"}
        or not isinstance(facts.delivery_state, str)
        or facts.delivery_state
        not in {"disabled", "enabled", "revoked", "not_delivered", "sent_audited", "delivery_unknown"}
        or not _is_adaptive_digest(facts.proposal_digest)
        or isinstance(facts.revision, bool)
        or not isinstance(facts.revision, int)
        or facts.revision < 1
        or facts.source_cluster_ids != ("adaptive-proposal",)
        or facts.excluded_risk_ids != ("medical", "unsafe_nutrition")
    ):
        raise AdaptiveWorkflowError("adaptive coaching facts are invalid")
    target_macros = _coaching_macro_pairs(facts.target_macros, field="target macros")
    if (
        not isinstance(facts.carb_category_targets, tuple)
        or len(facts.carb_category_targets) > 7
    ):
        raise AdaptiveWorkflowError("adaptive carb category targets are invalid")
    carb_targets: list[tuple[str, tuple[tuple[str, int], ...]]] = []
    for item in facts.carb_category_targets:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or item[0] not in {"high", "medium", "low"}
        ):
            raise AdaptiveWorkflowError("adaptive carb category targets are invalid")
        macros = _coaching_macro_pairs(item[1], field="carb category targets")
        carb_targets.append((item[0], macros))
    return {
        "customer_key": key,
        "evaluation_day": facts.evaluation_day,
        "current_mean_kg": facts.current_mean_kg,
        "prior_mean_kg": facts.prior_mean_kg,
        "weekly_rate_percent": facts.weekly_rate_percent,
        "goal_mode": facts.goal_mode,
        "goal_range": facts.goal_range,
        "decision": facts.decision,
        "judgment": facts.decision,
        "reason_category_ids": facts.reason_category_ids,
        "target_macros": target_macros,
        "carb_category_targets": tuple(carb_targets),
        "safety_held": facts.safety_held,
        "approval_state": facts.approval_state,
        "delivery_state": facts.delivery_state,
        "proposal_digest": facts.proposal_digest,
        "revision": facts.revision,
        "source_cluster_ids": facts.source_cluster_ids,
        "excluded_risk_ids": facts.excluded_risk_ids,
    }

def _coaching_reason_ids(reasons: object) -> tuple[str, ...]:
    if not isinstance(reasons, (tuple, list)):
        return ()
    ids: list[str] = []
    for value in reasons:
        text = str(value).casefold()
        if "safety" in text or "안전" in text:
            identifier = "safety_hold"
        elif "adher" in text or "순응" in text:
            identifier = "adherence"
        elif "cooldown" in text or "대기" in text:
            identifier = "cooldown"
        elif "sample" in text or "자료" in text or "데이터" in text:
            identifier = "insufficient_data"
        elif "trend" in text or "추세" in text or "rate" in text:
            identifier = "trend"
        else:
            identifier = "review"
        if identifier not in ids:
            ids.append(identifier)
    return tuple(ids[:8])

def customer_start_callback(customer_key: object) -> str:
    """Return a stable opaque Telegram callback for one customer topic."""
    key = str(customer_key or "").strip()
    if not key:
        raise ValueError("customer key is required")
    token = hashlib.sha256(f"customer-start\0{key}".encode("utf-8")).hexdigest()[:24]
    value = f"{_CUSTOMER_START_CALLBACK_PREFIX}:{token}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("customer start callback exceeds Telegram's 64-byte limit")
    return value


def parse_customer_start_callback(value: object) -> str | None:
    """Extract an opaque customer-start token without accepting customer data."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2 or parts[0] != _CUSTOMER_START_CALLBACK_PREFIX:
        return None
    token = parts[1]
    return token if _CUSTOMER_START_CALLBACK_TOKEN.fullmatch(token) else None


def trainer_private_selection_callback(trainer_user_id: object, customer_key: object) -> str:
    """Return a stable, opaque Telegram callback for one trainer/customer pair."""
    material = f"{str(trainer_user_id).strip()}\x00{str(customer_key).strip()}".encode("utf-8")
    token = hashlib.sha256(material).hexdigest()[:24]
    value = f"{_PRIVATE_TRAINER_SELECTION_PREFIX}:{token}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("trainer selection callback exceeds Telegram's 64-byte limit")
    return value


def parse_trainer_private_selection(value: object) -> str | None:
    """Extract an opaque trainer selection token without accepting customer data."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2 or parts[0] != _PRIVATE_TRAINER_SELECTION_PREFIX:
        return None
    token = parts[1]
    return token if _PRIVATE_TRAINER_SELECTION_TOKEN.fullmatch(token) else None


def _current_kst_date() -> date:
    return datetime.now(_KST).date()


def _coerce_kst_date(value: object) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(_KST).date()
        return value.date()
    return value if isinstance(value, date) else None


def _plan_window_start(customer: object) -> date | None:
    spec = getattr(customer, "spec", None)
    plan = getattr(spec, "plan", None)
    starts_on = getattr(plan, "starts_on", None)
    if isinstance(starts_on, str):
        try:
            starts_on = date.fromisoformat(starts_on)
        except ValueError:
            return None
    return _coerce_kst_date(starts_on)



@dataclass(frozen=True, slots=True)
class IncomingAddress:
    user_id: str
    chat_id: str
    topic_id: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.user_id, self.chat_id, self.topic_id)


@dataclass(frozen=True, slots=True)
class CallbackInput:
    data: str
    address: IncomingAddress
    message_id: str


@dataclass(frozen=True, slots=True)
class CompletionNotice:
    customer_key: str
    display_name: str
    kst_day: str
    request_token: str | None
    safety_held: bool = False
    safety_event: object | None = None
    hold_reasons: tuple[str, ...] = ()
    referral_guidance: str = ""
    role: str = "customer"


@dataclass(frozen=True, slots=True)
class CustomerTransition:
    reply: WizardReply
    completion: CompletionNotice | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCustomer:
    customer: CustomerRuntime
    bridge: PhysiqueCheckinBridge
    trainer_bridge: PhysiqueCheckinBridge | None = None
    trainer_dm_bridge: PhysiqueCheckinBridge | None = None

    @property
    def trainer_private_bridge(self) -> PhysiqueCheckinBridge | None:
        """Compatibility alias for the private trainer DM bridge."""
        return self.trainer_dm_bridge

@dataclass(frozen=True, slots=True)
class DraftSelection:
    customer: CustomerRuntime
    snapshot: CustomerSnapshot
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class DraftAction:
    accepted: bool
    draft_id: str | None = None
    text: str | None = None
    status: str = ""
    selection: DraftSelection | None = None
    error: str | None = None
    message_id: str | None = None
    transport_required: bool = False

class DraftLedgerError(ValueError):
    """Raised when the durable draft ledger cannot be trusted for a write."""

    def __init__(self, detail: str = "draft ledger is unavailable") -> None:
        detail = str(detail)
        super().__init__(detail)
        self.detail = detail
def _resolve_committed_registry_path(profile_root: Path) -> Path:
    """Resolve the profile package's canonical registry path without revalidating it."""
    from checkin_cli.customer_admin import _resolve_profile_root, _resolve_registry_path

    root = _resolve_profile_root(Path(profile_root))
    return _resolve_registry_path(root)


def load_committed_customer_registry(profile_root: Path) -> tuple[CustomerRegistry, Path]:
    """Load the profile registry through its committed-activation runtime gate."""
    from checkin_cli.customer_admin import load_runtime_customer_registry

    root = Path(profile_root)
    registry = load_runtime_customer_registry(root)
    return registry, _resolve_committed_registry_path(root)


class NutritionCoachingCoordinator:
    """Route customer submissions and owner draft requests without generic ingress."""

    def __init__(
        self,
        profile_root: Path,
        registry: CustomerRegistry,
        registry_path: Path | None = None,
        *,
        kst_date_provider: Callable[[], date | datetime] | None = None,
        customer_transport: object | None = None,
        delivery_enabled: bool = False,
        review_operator: object | None = None,
        owner_scheduled_routes: object = (),
        generic_reserved_routes: object = (),
    ) -> None:
        self._kst_date_provider = kst_date_provider or _current_kst_date
        self._customer_transport = customer_transport
        self._delivery_enabled = delivery_enabled is True
        self._review_operator = review_operator
        self._owner_scheduled_routes = owner_scheduled_routes
        self._generic_reserved_routes = generic_reserved_routes
        self._profile_root = Path(profile_root)
        self._registry_path = Path(registry_path) if registry_path is not None else None
        self._registry_generation = (
            self._registry_fingerprint(self._registry_path)
            if self._registry_path is not None
            else None
        )
        self._live_registry_valid = registry_path is None or self._registry_generation is not None
        self._live_registry_error: str | None = None
        owner_actions = self._profile_root / "data" / "owner-actions"
        self._requests_path = owner_actions / "draft-requests.json"
        self._drafts_path = owner_actions / "drafts.json"
        self._deliveries_path = owner_actions / "draft-deliveries.json"
        self._outbox_path = self._deliveries_path
        self._trainer_private_selected: dict[str, str] = {}
        self._configure_registry(registry)
        self.schedule_confirm_handler = _ScheduleConfirmationFacade(self)

    @staticmethod
    def _registry_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (
            int(getattr(stat, "st_ino", 0)),
            int(getattr(stat, "st_size", 0)),
            int(getattr(stat, "st_mtime_ns", 0)),
            int(getattr(stat, "st_ctime_ns", 0)),
        )

    def _configure_registry(self, registry: CustomerRegistry) -> None:
        from checkin_cli.wizard import WizardService

        self._registry = registry
        if self._review_operator is not None:
            validator = validate_review_space_disjoint
            if not callable(validator):
                raise RuntimeError("adaptive review-space validator is unavailable")
            customer_routes = []
            trainer_routes = []
            for candidate in registry.customers:
                spec = getattr(candidate, "spec", None)
                if getattr(spec, "enabled", False) is not True:
                    continue
                customer_routes.append(getattr(spec, "telegram", None))
                trainer = getattr(spec, "trainer", None)
                if trainer is not None:
                    trainer_routes.append(trainer)
            try:
                validator(
                    self._review_operator,
                    customer_routes=tuple(customer_routes),
                    trainer_routes=tuple(trainer_routes),
                    owner_scheduled_routes=(
                        registry.owner,
                        self._owner_scheduled_routes,
                    ),
                    generic_reserved_routes=self._generic_reserved_routes,
                )
            except Exception as exc:
                raise AdaptiveWorkflowError(
                    "adaptive review-space collision validation failed"
                ) from exc
        routes: dict[tuple[str, str, str], ResolvedCustomer] = {}
        trainer_routes: dict[tuple[str, str, str], ResolvedCustomer] = {}
        by_key: dict[str, ResolvedCustomer] = {}
        event_sources: dict[str, object] = {}
        for customer in registry.customers:
            if not customer.spec.enabled:
                continue
            telegram = customer.spec.telegram
            wizard_root = customer.data_root / "wizard"
            service = WizardService.for_registered(customer)
            event_source = getattr(service, "_events", None)
            if event_source is None or not callable(getattr(event_source, "_read_events", None)):
                raise RuntimeError("customer canonical EventStore is unavailable")
            config = PhysiqueCheckinConfig(
                telegram.user_id,
                telegram.chat_id,
                telegram.topic_id,
                43_200,
                False,
                False,
            )
            bridge = PhysiqueCheckinBridge(
                config,
                service,
                wizard_root / "telegram-bindings.json",
                customer_key=customer.spec.customer_key,
            )
            trainer_bridge: PhysiqueCheckinBridge | None = None
            trainer_dm_bridge: PhysiqueCheckinBridge | None = None
            trainer = getattr(customer.spec, "trainer", None)
            trainer_user_id = str(getattr(trainer, "user_id", "") or "").strip()
            if trainer_user_id:
                trainer_dm_config = PhysiqueCheckinConfig(
                    trainer_user_id,
                    trainer_user_id,
                    "0",
                    43_200,
                    False,
                    False,
                )
                trainer_dm_bridge = PhysiqueCheckinBridge(
                    trainer_dm_config,
                    service,
                    wizard_root / "trainer-private-telegram-bindings.json",
                    customer_key=customer.spec.customer_key,
                )
            if trainer is not None:
                trainer_config = PhysiqueCheckinConfig(
                    trainer_user_id,
                    str(getattr(trainer, "chat_id", "")),
                    str(getattr(trainer, "topic_id", "")),
                    43_200,
                    False,
                    False,
                )
                if all((trainer_config.owner_id, trainer_config.chat_id, trainer_config.topic_id)):
                    trainer_bridge = PhysiqueCheckinBridge(
                        trainer_config,
                        service,
                        wizard_root / "trainer-telegram-bindings.json",
                        customer_key=customer.spec.customer_key,
                    )
            resolved = ResolvedCustomer(customer, bridge, trainer_bridge, trainer_dm_bridge)
            if trainer_bridge is not None and trainer is not None:
                trainer_routes[trainer.key] = resolved
            routes[telegram.key] = resolved
            by_key[customer.spec.customer_key] = resolved
            event_sources[customer.spec.customer_key] = event_source
        self._routes = routes
        self._trainer_routes = trainer_routes
        self._by_key = by_key
        self._event_sources = event_sources
        self._spaces = {
            (chat_id, topic_id)
            for _, chat_id, topic_id in (*routes, *trainer_routes)
        }

    def _ensure_live_registry(self) -> bool:
        path = getattr(self, "_registry_path", None)
        if not isinstance(path, Path):
            return True
        if not callable(profile_authority_lock):
            self._live_registry_valid = False
            self._live_registry_error = "customer registry reload failed: authority_lock_unavailable"
            return False
        with profile_authority_lock(self._profile_root):
            try:
                registry, canonical_path = load_committed_customer_registry(self._profile_root)
                path = canonical_path
                self._registry_path = canonical_path
                generation = self._registry_fingerprint(path)
                if generation is None:
                    raise RuntimeError("customer registry is unavailable")
            except Exception as exc:
                self._live_registry_valid = False
                self._live_registry_error = (
                    f"customer registry reload failed: {type(exc).__name__}"
                )
                return False
            if (
                generation == getattr(self, "_registry_generation", None)
                and getattr(self, "_live_registry_valid", True)
            ):
                self._live_registry_error = None
                return True
            try:
                self._configure_registry(registry)
            except Exception as exc:
                self._live_registry_valid = False
                self._live_registry_error = (
                    f"customer registry reload failed: {type(exc).__name__}"
                )
                return False
            self._registry_generation = generation
            self._live_registry_valid = True
            self._live_registry_error = None
            return True

    def refresh_live_registry(self) -> bool:
        """Refresh on-disk customer authority and revalidate its activation receipt."""
        return self._ensure_live_registry()
    @property
    def owner_space(self) -> tuple[str, str]:
        return self._registry.owner.space_key

    @property
    def owner(self) -> IncomingAddress:
        return IncomingAddress(*self._registry.owner.key)

    @property
    def profile_root(self) -> Path:
        return self._profile_root

    @property
    def registry(self) -> CustomerRegistry:
        return self._registry

    def set_customer_transport(self, transport: object | None) -> None:
        """Install the authoritative registered-customer transport."""
        if transport is not None and not any(
            callable(getattr(transport, name, None))
            for name in ("send_customer", "send_adaptive_customer")
        ):
            raise AdaptiveWorkflowError("customer transport is unavailable")
        self._customer_transport = transport

    def set_delivery_enabled(self, enabled: bool) -> None:
        """Set the explicit production delivery gate."""
        if type(enabled) is not bool:
            raise AdaptiveWorkflowError("adaptive delivery gate is invalid")
        self._delivery_enabled = enabled

    def adaptive_nutrition_coordinator(
        self,
        customer_key: str,
        *,
        event_path: Path | str | None = None,
        customer_transport: object | None = None,
        delivery_enabled: bool | None = None,
    ) -> AdaptiveNutritionCoordinator:
        """Return the fixed-topic adaptive surface for one live customer."""
        if not self._ensure_live_registry():
            raise AdaptiveWorkflowError("customer registry is unavailable")
        key = str(customer_key)
        resolved = getattr(self, "_by_key", {}).get(key)
        if resolved is None or not bool(getattr(resolved.customer.spec, "enabled", False)):
            raise AdaptiveWorkflowError("adaptive customer route is unavailable")
        data_root = getattr(resolved.customer, "data_root", None)
        if not isinstance(data_root, Path):
            raise AdaptiveWorkflowError("adaptive customer data root is unavailable")
        canonical_event_path = data_root / "nutrition-plans" / "events.jsonl"
        if event_path is not None and Path(event_path).resolve() != canonical_event_path.resolve():
            raise AdaptiveWorkflowError("production adaptive events must use the customer runtime")
        event_source = self.event_source(key)
        if event_source is None or not callable(getattr(event_source, "_read_events", None)):
            raise AdaptiveWorkflowError("canonical customer EventStore is unavailable")
        return AdaptiveNutritionCoordinator(
            customer_key=key,
            starts_on=_plan_window_start(resolved.customer),
            event_path=canonical_event_path,
            profile_root=self._profile_root,
            registry_path=self._registry_path,
            canonical_event_source=event_source,
            customer_runtime=resolved.customer,
            authority=self,
            customer_transport=(
                customer_transport
                if customer_transport is not None
                else getattr(self, "_customer_transport", None)
            ),
            delivery_enabled=(
                self._delivery_enabled
                if delivery_enabled is None
                else delivery_enabled is True
            ),
        )

    adaptive_operator_coordinator = adaptive_nutrition_coordinator
    def set_adaptive_delivery(
        self,
        customer_key: str,
        enabled: bool,
        *,
        operator_id: object,
        topic_id: object = 59,
    ) -> Mapping[str, object]:
        adaptive = self.adaptive_nutrition_coordinator(customer_key)
        return adaptive.set_persisted_delivery(
            enabled,
            operator_id=operator_id,
            topic_id=topic_id,
        )
    def _registry_customer(self, customer_key: str) -> CustomerRuntime | None:
        for candidate in getattr(self._registry, "customers", ()):
            spec = getattr(candidate, "spec", None)
            if getattr(spec, "customer_key", None) == customer_key:
                return candidate
        return None

    def _customer_has_safety_hold(self, resolved: ResolvedCustomer) -> bool:
        events_path = getattr(resolved.customer, "data_root", None)
        events_path = (
            events_path / "wizard" / "events.jsonl"
            if isinstance(events_path, Path)
            else None
        )
        if events_path is None or not events_path.exists():
            return False
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                event = json.loads(line)
                if self._safety_value_has_hold(self._field(event, "safety")):
                    return True
        except (OSError, TypeError, ValueError):
            return True
        return False
    def _transport_kst_date(self, override: date | datetime | None = None) -> date | None:
        value: object = override
        if value is None:
            provider = getattr(self, "_kst_date_provider", None)
            try:
                value = provider() if callable(provider) else _current_kst_date()
            except Exception:
                return None
        return _coerce_kst_date(value)

    def _customer_plan_window_allows(
        self,
        customer: object,
        *,
        kst_date: date | datetime | None = None,
    ) -> bool:
        starts_on = _plan_window_start(customer)
        current = self._transport_kst_date(kst_date)
        return (
            starts_on is not None
            and current is not None
            and starts_on <= current <= starts_on + timedelta(days=27)
        )
    def _adaptive_plan_window_allows(
        self,
        customer: object,
        *,
        kst_date: date | datetime | None = None,
    ) -> bool:
        starts_on = _plan_window_start(customer)
        current = self._transport_kst_date(kst_date)
        if starts_on is None or current is None:
            return False
        baseline = starts_on <= current <= starts_on + timedelta(days=27)
        extension = starts_on + timedelta(days=28) <= current <= starts_on + timedelta(days=83)
        if not baseline and not extension:
            return False
        data_root = getattr(customer, "data_root", None)
        if not isinstance(data_root, Path):
            return False
        if extension:
            if not callable(load_approved_adaptive_artifacts):
                return False
            try:
                artifacts = load_approved_adaptive_artifacts(data_root)
            except (OSError, TypeError, ValueError):
                return False
            if (
                artifacts.policy.extended_through is None
                or current > artifacts.policy.extended_through
                or artifacts.policy.starts_on != starts_on
            ):
                return False
        try:
            epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
            if (
                epoch_path.is_symlink()
                or not epoch_path.is_file()
                or epoch_path.stat().st_mode & 0o077
            ):
                return False
            epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return False
        if not isinstance(epoch, Mapping):
            return False
        try:
            fields = AdaptiveNutritionCoordinator._feature_config_fields(epoch)
            configured = epoch.get("config_digest")
            expected = AdaptiveNutritionCoordinator._feature_config_digest(epoch)
        except (AdaptiveWorkflowError, TypeError, ValueError):
            return False
        if (
            epoch.get("schema_version") != "1.0"
            or set(epoch)
            != {
                "schema_version",
                "epoch",
                "config_digest",
                "analytics_shadow",
                "operator_candidates",
                "activation",
                "delivery",
            }
            or not isinstance(configured, str)
            or len(configured) != 64
            or any(character not in "0123456789abcdef" for character in configured)
            or not hmac.compare_digest(configured, expected)
            or fields.get("epoch") != epoch.get("epoch")
        ):
            return False
        return (
            epoch.get("activation") is True
            and epoch.get("delivery") is True
        )

    def coaching_processing_allowed(self, customer_key: str) -> bool:
        """Revalidate registered-customer authority for one coaching stage.

        This gate intentionally does not inspect a destination or grant
        delivery authority.  It is called immediately before each stage and
        before publication by the Telegram adapter.
        """
        if not isinstance(customer_key, str) or not customer_key.strip():
            return False
        if not self._ensure_live_registry():
            return False
        resolved = self._by_key.get(customer_key)
        if resolved is None:
            return False
        customer = getattr(resolved, "customer", None)
        spec = getattr(customer, "spec", None)
        if getattr(spec, "enabled", False) is not True:
            return False
        if self._consent_error(customer) is not None:
            return False
        return not self._customer_has_safety_hold(resolved)
    def customer_transport_allowed(
        self,
        customer_key: str,
        destination: object | None = None,
        *,
        kst_date: date | datetime | None = None,
        adaptive: bool = False,
    ) -> bool:
        """Revalidate customer authority immediately before customer transport."""
        if not self._ensure_live_registry():
            return False
        resolved = self._by_key.get(customer_key)
        if resolved is None or not bool(getattr(resolved.customer.spec, "enabled", False)):
            return False
        if self._consent_error(resolved.customer) is not None:
            return False
        window_allowed = (
            self._adaptive_plan_window_allows(resolved.customer, kst_date=kst_date)
            if adaptive
            else self._customer_plan_window_allows(resolved.customer, kst_date=kst_date)
        )
        if not window_allowed:
            return False
        canonical = resolved.customer.spec.telegram
        if destination is not None:
            destination_key = tuple(
                str(getattr(destination, field, "") or "")
                for field in ("user_id", "chat_id", "topic_id")
            )
            if destination_key != tuple(str(value) for value in canonical.key):
                return False
        return not self._customer_has_safety_hold(resolved)

    def validate_delivery_transport(
        self,
        draft_id: str,
        owner: IncomingAddress,
        destination: object | None = None,
        text: str | None = None,
        *,
        kst_date: date | datetime | None = None,
    ) -> bool:
        """Recheck an approved draft immediately before its transport call."""
        if not self._ensure_live_registry() or owner.key != self._registry.owner.key:
            return False
        try:
            drafts = self._read_drafts()
        except DraftLedgerError:
            return False
        record = drafts.get(draft_id)
        if self._record_eligibility_error(record, owner) is not None:
            return False
        selection = self._draft_selection_from_record(record, owner)
        if selection is None or self._selection_eligibility_error(selection) is not None:
            return False
        if record.get("status") not in {"approved", "sent"}:
            return False
        if self._approved_record_error(draft_id, selection, record) is not None:
            return False
        record_text = str(record.get("text", "")).strip()
        if text is not None and (
            not isinstance(text, str) or text.strip() != record_text
        ):
            return False
        if telegram_utf16_length(record_text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16:
            return False
        try:
            deliveries = self._read_deliveries()
        except DraftLedgerError:
            return False
        delivery = deliveries.get(self._delivery_key(draft_id, record_text))
        if not isinstance(delivery, dict) or delivery.get("status") != "pending":
            return False
        if (
            delivery.get("customer_key") != selection.customer.spec.customer_key
            or delivery.get("session_id") != str(record.get("session_id", ""))
            or delivery.get("text") != record_text
            or delivery.get("revision") != self._draft_revision(record_text)
            or delivery.get("approved_revision") != record.get("approved_revision")
            or delivery.get("approved_event_id") != record.get("approved_event_id")
        ):
            return False
        return self.customer_transport_allowed(
            selection.customer.spec.customer_key,
            destination if destination is not None else selection.customer.spec.telegram,
            kst_date=kst_date,
        )

    def customer(self, customer_key: str) -> CustomerRuntime | None:
        if not self._ensure_live_registry():
            return None
        resolved = self._by_key.get(customer_key)
        return resolved.customer if resolved is not None else None
    def event_source(self, customer_key: str) -> object | None:
        """Return the live profile EventStore for one enabled customer."""
        return getattr(self, "_event_sources", {}).get(customer_key)
    def event_store(self, customer_key: str) -> object | None:
        """Compatibility name for the canonical per-customer EventStore."""
        return self.event_source(customer_key)

    def resolve(self, address: IncomingAddress) -> ResolvedCustomer | None:
        if not self._ensure_live_registry():
            return None
        return self._routes.get(address.key)
    def customer_start_callback(self, customer_key: object) -> str:
        """Return the stable launcher callback for one live customer identity."""
        return customer_start_callback(customer_key)

    def resolve_customer_start(
        self,
        address: IncomingAddress,
        callback_data: object,
    ) -> ResolvedCustomer | None:
        """Resolve a customer launcher only through the live exact Telegram route."""
        if not self._ensure_live_registry():
            return None
        token = parse_customer_start_callback(callback_data)
        if token is None:
            return None
        expected = f"{_CUSTOMER_START_CALLBACK_PREFIX}:{token}"
        for resolved in self._by_key.values():
            spec = resolved.customer.spec
            if not bool(getattr(spec, "enabled", False)):
                continue
            telegram = getattr(spec, "telegram", None)
            if telegram is None or address.key != tuple(telegram.key):
                continue
            try:
                candidate = customer_start_callback(spec.customer_key)
            except ValueError:
                continue
            if candidate == expected:
                return resolved
        return None

    def resolve_trainer(self, address: IncomingAddress) -> ResolvedCustomer | None:
        """Resolve only the registry-bound trainer Telegram triple."""
        if not self._ensure_live_registry():
            return None
        return self._trainer_routes.get(address.key)

    def trainer_for(self, customer_key: str) -> ResolvedCustomer | None:
        if not self._ensure_live_registry():
            return None
        resolved = self._by_key.get(customer_key)
        return resolved if resolved is not None and resolved.trainer_bridge is not None else None
    def trainer_private_customers(self, trainer_user_id: object) -> tuple[ResolvedCustomer, ...]:
        """Return only enabled customers currently assigned to this trainer user."""
        if not self._ensure_live_registry():
            return ()
        user_id = str(trainer_user_id or "").strip()
        if not user_id:
            return ()
        matches: list[ResolvedCustomer] = []
        for resolved in self._by_key.values():
            spec = resolved.customer.spec
            trainer = getattr(spec, "trainer", None)
            assigned_user_id = str(getattr(trainer, "user_id", "") or "").strip()
            if (
                bool(getattr(spec, "enabled", False))
                and assigned_user_id == user_id
                and resolved.trainer_dm_bridge is not None
            ):
                matches.append(resolved)
        return tuple(sorted(matches, key=lambda item: str(item.customer.spec.customer_key)))

    def trainer_private_menu(self, trainer_user_id: object) -> WizardPrompt:
        """Build the private trainer customer picker from live registry state."""
        user_id = str(trainer_user_id or "").strip()
        customers = self.trainer_private_customers(user_id)
        buttons = tuple(
            (
                " ".join(str(getattr(item.customer.spec, "display_name", "")).split())[:80],
                trainer_private_selection_callback(user_id, item.customer.spec.customer_key),
            )
            for item in customers
        )
        rows = tuple(buttons[index:index + 2] for index in range(0, len(buttons), 2))
        if buttons:
            text = "오늘 PT 기록할 고객을 선택해 주세요."
        else:
            text = "현재 등록된 담당 활성 고객이 없습니다."
        return WizardPrompt(text, buttons, rows)

    @staticmethod
    def _is_trainer_private_address(address: IncomingAddress) -> bool:
        user_id = str(address.user_id or "").strip()
        return bool(user_id) and (
            user_id == str(address.chat_id or "").strip()
            and str(address.topic_id or "") == "0"
        )

    def resolve_trainer_private_selection(
        self,
        address: IncomingAddress,
        callback_data: object,
    ) -> ResolvedCustomer | None:
        """Resolve an opaque picker callback against the current assignment registry."""
        if not self._is_trainer_private_address(address):
            return None
        token = parse_trainer_private_selection(callback_data)
        if token is None:
            return None
        for resolved in self.trainer_private_customers(address.user_id):
            candidate = trainer_private_selection_callback(
                address.user_id,
                resolved.customer.spec.customer_key,
            )
            if candidate.endswith(f":{token}"):
                return resolved
        return None

    def open_trainer_private_launcher(
        self,
        address: IncomingAddress,
        callback_data: object,
    ) -> tuple[ResolvedCustomer, WizardReply] | None:
        """Open a scoped trainer wizard only after live picker resolution."""
        resolved = self.resolve_trainer_private_selection(address, callback_data)
        bridge = resolved.trainer_dm_bridge if resolved is not None else None
        if resolved is None or bridge is None:
            return None
        self._trainer_private_selected[address.user_id] = str(
            resolved.customer.spec.customer_key
        )
        return resolved, bridge.open_trainer_launcher()

    def trainer_private_active(self, address: IncomingAddress) -> ResolvedCustomer | None:
        """Find the one private-DM bridge carrying the current typed continuation."""
        if not self._is_trainer_private_address(address):
            return None
        assigned = self.trainer_private_customers(address.user_id)
        selected_key = self._trainer_private_selected.get(address.user_id)
        if selected_key:
            selected = next(
                (
                    item
                    for item in assigned
                    if str(item.customer.spec.customer_key) == selected_key
                ),
                None,
            )
            if selected is not None:
                bridge = selected.trainer_dm_bridge
                if bridge is not None and bridge.has_active_binding():
                    return selected
            self._trainer_private_selected.pop(address.user_id, None)
        active = tuple(
            item
            for item in assigned
            if item.trainer_dm_bridge is not None
            and item.trainer_dm_bridge.has_active_binding()
        )
        return active[0] if len(active) == 1 else None

    def trainer_private_bridge_for_callback(
        self,
        address: IncomingAddress,
        callback_data: object,
    ) -> ResolvedCustomer | None:
        """Find a callback's customer bridge without trusting callback contents."""
        if not self._is_trainer_private_address(address):
            return None
        parsed = CallbackData.parse(callback_data) if isinstance(callback_data, str) else None
        if parsed is None:
            return None
        for resolved in self.trainer_private_customers(address.user_id):
            bridge = resolved.trainer_dm_bridge
            if bridge is not None and bridge.has_binding(parsed.session_id):
                return resolved
        return None

    def handle_trainer_private_text(
        self,
        address: IncomingAddress,
        text: str,
    ) -> CustomerTransition:
        """Continue the active private wizard while retaining its customer scope."""
        resolved = self.trainer_private_active(address)
        bridge = resolved.trainer_dm_bridge if resolved is not None else None
        if resolved is None or bridge is None:
            return CustomerTransition(
                WizardReply(True, False, "먼저 오늘 PT 기록에서 고객을 선택해 주세요.", None, None)
            )
        reply = bridge.handle_text(text, *address.key)
        if reply is None:
            reply = WizardReply(True, False, "현재 질문의 버튼을 선택하거나 안내된 입력을 보내 주세요.", None, None)
        return CustomerTransition(reply)

    def handle_trainer_private_callback(self, incoming: CallbackInput) -> CustomerTransition:
        """Continue a private trainer wizard only through its assigned DM bridge."""
        resolved = self.trainer_private_bridge_for_callback(incoming.address, incoming.data)
        bridge = resolved.trainer_dm_bridge if resolved is not None else None
        if resolved is None or bridge is None:
            return CustomerTransition(
                WizardReply(True, False, "이 버튼을 사용할 수 없습니다.", None, None)
            )
        reply = bridge.handle_callback(
            incoming.data,
            *incoming.address.key,
            incoming.message_id,
        )
        callback = CallbackData.parse(incoming.data)
        if not reply.accepted or callback is None:
            return CustomerTransition(reply)
        safety = bridge.finalized_safety_snapshot(callback.session_id)
        if safety is not None:
            return CustomerTransition(
                reply,
                self._completion_from_safety(resolved, safety, role="trainer"),
            )
        trainer = bridge.finalized_trainer_snapshot(callback.session_id)
        if trainer is None:
            return CustomerTransition(reply)
        return CustomerTransition(
            reply,
            CompletionNotice(
                resolved.customer.spec.customer_key,
                resolved.customer.spec.display_name,
                str(trainer.get("kst_day", "")),
                None,
                role="trainer",
            ),
        )

    def current_kst_date(self) -> date:
        """Return the coordinator's current KST date provider value."""
        return self._transport_kst_date() or _current_kst_date()

    def owns_space(self, chat_id: str, topic_id: str) -> bool:
        if not self._ensure_live_registry():
            return False
        return (chat_id, topic_id) in self._spaces

    def owns_owner_space(self, chat_id: str, topic_id: str) -> bool:
        if not self._ensure_live_registry():
            return False
        return (str(chat_id), str(topic_id)) == self.owner_space

    def is_owner(self, address: IncomingAddress) -> bool:
        if not self._ensure_live_registry():
            return False
        return address.key == self._registry.owner.key

    @staticmethod
    def _terminal_launcher_reply(
        resolved: ResolvedCustomer,
        opening: WizardReply,
    ) -> WizardReply:
        """Turn a completed same-day launcher into a terminal, data-free notice."""
        callback_data = opening.callback_data
        callback = (
            CallbackData.parse(callback_data)
            if isinstance(callback_data, str)
            else None
        )
        if callback is None:
            return opening
        bridge = resolved.bridge
        safety_getter = getattr(bridge, "finalized_safety_snapshot", None)
        try:
            safety = safety_getter(callback.session_id) if callable(safety_getter) else None
        except Exception:
            safety = None
        if isinstance(safety, Mapping):
            notice = "오늘 체크인은 안전 기록으로 저장되어 코칭이 보류되었습니다."
            return WizardReply(True, True, notice, WizardPrompt(f"✅ {notice}"), None)
        snapshot_getter = getattr(bridge, "finalized_coaching_snapshot", None)
        try:
            snapshot = snapshot_getter(callback.session_id) if callable(snapshot_getter) else None
        except Exception:
            snapshot = None
        if isinstance(snapshot, Mapping):
            notice = "오늘 체크인은 이미 완료되었습니다."
            return WizardReply(True, True, notice, WizardPrompt(f"✅ {notice}"), None)
        return opening

    def open_launcher(self, customer_key: str) -> WizardReply:
        if not self._ensure_live_registry():
            return WizardReply(True, False, "고객 경로를 사용할 수 없습니다.", None, None)
        resolved = self._by_key.get(customer_key)
        if resolved is None:
            return WizardReply(True, False, "고객 경로를 사용할 수 없습니다.", None, None)
        opening = resolved.bridge.open_launcher("nutrition_daily")
        if not opening.accepted:
            return opening
        return self._terminal_launcher_reply(resolved, opening)

    def bind_launcher(self, customer_key: str, callback_data: str, message_id: str) -> bool:
        if not self._ensure_live_registry():
            return False
        callback = CallbackData.parse(callback_data)
        if callback is None:
            return False
        resolved = self._by_key.get(customer_key)
        return resolved is not None and resolved.bridge.bind_launcher_message(
            callback.session_id,
            message_id,
        )

    def open_trainer_launcher(self, customer_key: str) -> WizardReply:
        if not self._ensure_live_registry():
            return WizardReply(True, False, "트레이너 경로를 사용할 수 없습니다.", None, None)
        resolved = self.trainer_for(customer_key)
        if resolved is None or resolved.trainer_bridge is None:
            return WizardReply(True, False, "트레이너 주소가 등록되지 않았습니다.", None, None)
        return resolved.trainer_bridge.open_trainer_launcher()

    def bind_trainer_launcher(self, customer_key: str, callback_data: str, message_id: str) -> bool:
        if not self._ensure_live_registry():
            return False
        callback = CallbackData.parse(callback_data)
        resolved = self.trainer_for(customer_key)
        if callback is None or resolved is None or resolved.trainer_bridge is None:
            return False
        return resolved.trainer_bridge.bind_launcher_message(callback.session_id, message_id)

    def handle_text(self, address: IncomingAddress, text: str) -> CustomerTransition:
        resolved = self.resolve(address)
        if resolved is None:
            return CustomerTransition(WizardReply(True, False, "이 공간에서는 체크인을 제출할 수 없습니다.", None, None))
        if " ".join(text.split()) in {"오늘 체크인 수정", "체크인 수정"}:
            return CustomerTransition(resolved.bridge.open_nutrition_correction())
        reply = resolved.bridge.handle_text(text, *address.key)
        if reply is None:
            reply = WizardReply(True, False, "이 공간에서는 체크인 제출만 사용할 수 있습니다.", None, None)
        return CustomerTransition(reply)
    def handle_trainer_text(self, address: IncomingAddress, text: str) -> CustomerTransition:
        resolved = self.resolve_trainer(address)
        if resolved is None or resolved.trainer_bridge is None:
            return CustomerTransition(WizardReply(True, False, "이 트레이너 공간에서는 사용할 수 없습니다.", None, None))
        compact = "".join(text.split())
        spec = resolved.customer.spec
        display_name = " ".join(str(spec.display_name).split())
        customer_key = str(spec.customer_key)
        allowed_prefixes = {
            "",
            "트레이너",
            "운동",
            "PT",
            "트레이너세션",
            "".join(display_name.split()),
            "".join(customer_key.split()),
        }
        launch_requested = (
            compact in {
                "오늘PT기록",
                "PT기록",
                "오늘트레이너기록",
                "트레이너세션시작",
            }
            or compact.endswith("기록시작") and compact[:-4] in allowed_prefixes
        )
        if launch_requested:
            opening = resolved.trainer_bridge.open_trainer_launcher()
            today = _current_kst_date()
            notice = (
                f"{display_name} · {today.year}년 {today.month}월 {today.day}일\n"
                "트레이너 기록을 시작하거나 이어갈 수 있습니다."
            )
            return CustomerTransition(WizardReply(
                opening.handled,
                opening.accepted,
                notice,
                opening.prompt,
                opening.callback_data,
            ))
        reply = resolved.trainer_bridge.handle_text(text, *address.key)
        if reply is None:
            reply = WizardReply(True, False, "트레이너 기록만 사용할 수 있습니다.", None, None)
        return CustomerTransition(reply)

    def handle_trainer_callback(self, incoming: CallbackInput) -> CustomerTransition:
        resolved = self.resolve_trainer(incoming.address)
        if resolved is None or resolved.trainer_bridge is None:
            return CustomerTransition(WizardReply(True, False, "이 버튼을 사용할 수 없습니다.", None, None))
        bridge = resolved.trainer_bridge
        reply = bridge.handle_callback(incoming.data, *incoming.address.key, incoming.message_id)
        callback = CallbackData.parse(incoming.data)
        if not reply.accepted or callback is None:
            return CustomerTransition(reply)
        safety = bridge.finalized_safety_snapshot(callback.session_id)
        if safety is not None:
            return CustomerTransition(reply, self._completion_from_safety(resolved, safety, role="trainer"))
        trainer = bridge.finalized_trainer_snapshot(callback.session_id)
        if trainer is None:
            return CustomerTransition(reply)
        return CustomerTransition(
            reply,
            CompletionNotice(
                resolved.customer.spec.customer_key,
                resolved.customer.spec.display_name,
                str(trainer.get("kst_day", "")),
                None,
                role="trainer",
            ),
        )

    def handle_callback(self, incoming: CallbackInput) -> CustomerTransition:
        resolved = self.resolve(incoming.address)
        if resolved is None:
            return CustomerTransition(WizardReply(True, False, "이 버튼을 사용할 수 없습니다.", None, None))
        reply = resolved.bridge.handle_callback(
            incoming.data,
            *incoming.address.key,
            incoming.message_id,
        )
        callback = CallbackData.parse(incoming.data)
        if not reply.accepted or callback is None:
            return CustomerTransition(reply)
        safety = resolved.bridge.finalized_safety_snapshot(callback.session_id)
        if safety is not None:
            return CustomerTransition(reply, self._completion_from_safety(resolved, safety))
        if reply.notice not in {"체크인을 저장했습니다.", "안전 기록을 저장했습니다."}:
            return CustomerTransition(reply)
        snapshot = resolved.bridge.finalized_coaching_snapshot(callback.session_id)
        if snapshot is None:
            return CustomerTransition(reply)
        token = hashlib.sha256(
            f"{resolved.customer.spec.customer_key}:{callback.session_id}".encode()
        ).hexdigest()[:16]
        try:
            self._save_request(token, resolved.customer.spec.customer_key, callback.session_id)
        except DraftLedgerError as exc:
            logger.warning("customer nutrition request persistence failed: %s", exc.detail)
            return CustomerTransition(
                WizardReply(
                    True,
                    False,
                    "현재 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                    None,
                    None,
                )
            )
        return CustomerTransition(
            reply,
            CompletionNotice(
                resolved.customer.spec.customer_key,
                resolved.customer.spec.display_name,
                str(snapshot.get("kst_day", "")),
                token,
            ),
        )

    @staticmethod
    def _completion_from_safety(
        resolved: ResolvedCustomer,
        snapshot: dict[str, object],
        *,
        role: str = "customer",
    ) -> CompletionNotice:
        event = snapshot.get("event")
        reasons: tuple[str, ...] = ()
        referral = ""
        safety = getattr(event, "safety", None) if event is not None else None
        typed_reasons = tuple(getattr(safety, "reasons", ()) or ())
        if typed_reasons:
            reasons = tuple(
                NutritionCoachingCoordinator._format_safety_reason(reason)
                for reason in typed_reasons
            )[:6]
        elif event is not None:
            # Keep compatibility with profile versions that expose only a
            # ``safe_hold_reasons`` helper or plain excerpts.
            try:
                from checkin_cli.customer_grounding import safe_hold_reasons

                reasons = tuple(str(item)[:240] for item in safe_hold_reasons(event))[:6]
            except (ImportError, OSError, TypeError, ValueError):
                reasons = ()
        if event is not None:
            try:
                from checkin_cli.customer_grounding import referral_guidance

                referral = " ".join(str(referral_guidance(event)).split())[:500]
            except (ImportError, OSError, TypeError, ValueError):
                referral = ""
        if not referral:
            referral = (
                "증상이 있거나 악화되면 운동·식단 조절보다 의료기관 상담을 우선해 주세요."
            )
        return CompletionNotice(
            resolved.customer.spec.customer_key,
            resolved.customer.spec.display_name,
            str(snapshot.get("kst_day", "")),
            None,
            True,
            event,
            reasons,
            referral,
            role,
        )

    @staticmethod
    def _format_safety_reason(reason: object) -> str:
        rule = str(getattr(reason, "rule_id", "") or "").strip()
        class_value = getattr(reason, "class_name", None)
        if class_value is None:
            class_value = getattr(getattr(reason, "class_", None), "value", None)
        matched = getattr(reason, "matched_field", "")
        matched = getattr(matched, "value", matched)
        excerpt = " ".join(str(getattr(reason, "excerpt", "")).split())
        fields = tuple(item for item in (rule, str(class_value or ""), str(matched or "")) if item)
        prefix = " · ".join(fields)
        value = f"{prefix}: {excerpt}" if prefix else excerpt
        return value[:240]

    def resolve_draft(self, token: str, owner: IncomingAddress) -> DraftSelection | None:
        from checkin_cli.customer_grounding import CustomerSnapshot

        if not self._ensure_live_registry() or owner.key != self._registry.owner.key:
            return None
        try:
            request = self._read_requests().get(token)
        except DraftLedgerError:
            return None
        if request is None:
            return None
        customer_key, session_id = request
        resolved = self._by_key.get(customer_key)
        if resolved is None:
            return None
        snapshot = resolved.bridge.finalized_coaching_snapshot(session_id)
        current = self._current_customer(customer_key, resolved.customer)
        if snapshot is None:
            return None
        if self._consent_error(current) is not None:
            return None
        if self._bridge_safety_hold(resolved, session_id):
            return None
        try:
            selection = DraftSelection(
                current,
                CustomerSnapshot.model_validate(snapshot),
                session_id,
            )
        except (TypeError, ValueError):
            return None
        return None if self._selection_eligibility_error(selection) else selection
    def create_weekly_review_draft(
        self,
        customer_key: str,
        owner: IncomingAddress,
        source: object,
    ) -> DraftAction:
        """Persist one typed weekly source in the existing human-review lifecycle."""

        if not self._ensure_live_registry() or owner.key != self._registry.owner.key:
            return DraftAction(False, error="owner_only")
        resolved = self._by_key.get(str(customer_key or "").strip())
        if resolved is None:
            return DraftAction(False, error="customer_not_registered")
        try:
            from checkin_cli.customer_reporting import CustomerWeeklyReviewSource
        except ImportError:
            return DraftAction(False, error="weekly_review_api_unavailable")
        if not isinstance(source, CustomerWeeklyReviewSource):
            return DraftAction(False, error="weekly_review_source_invalid")
        if source.customer_key != resolved.customer.spec.customer_key:
            return DraftAction(False, error="weekly_review_customer_mismatch")
        snapshot_getter = getattr(
            resolved.bridge,
            "latest_finalized_coaching_snapshot",
            None,
        )
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        if not isinstance(snapshot, Mapping):
            return DraftAction(False, error="weekly_review_snapshot_unavailable")
        session_id = str(snapshot.get("session_id", "") or "").strip()
        if not session_id:
            return DraftAction(False, error="weekly_review_snapshot_unavailable")
        text = source.render_customer_body()
        source_digest = hashlib.sha256(
            canonical_json(
                {
                    "customer_key": source.customer_key,
                    "period_start": source.period_start.isoformat(),
                    "period_end": source.period_end.isoformat(),
                    "body": text,
                }
            ).encode("utf-8")
        ).hexdigest()
        token = hashlib.sha256(
            f"weekly-review:{source.customer_key}:{source_digest}".encode("utf-8")
        ).hexdigest()[:16]
        try:
            existing_request = self._read_requests().get(token)
            if existing_request is not None:
                if existing_request[0] != source.customer_key:
                    return DraftAction(
                        False,
                        error="weekly_review_request_conflict",
                    )
            else:
                self._save_request(token, source.customer_key, session_id)
        except DraftLedgerError:
            return DraftAction(False, error="draft_ledger_corrupt")
        return self.create_draft(token, owner, text)
    def create_draft(
        self,
        token: str,
        owner: IncomingAddress,
        text: str,
    ) -> DraftAction:
        with self._delivery_lock():
            return self._create_draft_locked(token, owner, text)

    def _create_draft_locked(
        self,
        token: str,
        owner: IncomingAddress,
        text: str,
    ) -> DraftAction:
        """Persist an AI draft and its audit event; never sends to a customer."""
        if not self._ensure_live_registry():
            return DraftAction(False, error="customer_registry_unavailable")
        candidate = " ".join(str(text).split()).strip()
        if (
            not candidate
            or len(candidate) > 8_000
            or telegram_utf16_length(candidate) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16
        ):
            return DraftAction(False, error="draft_request_invalid")
        try:
            # Validate all durable ledgers before appending any canonical event.
            # A malformed ledger must never be silently repaired by a write.
            drafts = self._read_drafts()
            requests = self._read_requests()
            self._read_deliveries()
        except DraftLedgerError:
            return DraftAction(False, error="draft_ledger_corrupt")
        selection = self.resolve_draft(token, owner)
        if selection is None:
            return DraftAction(False, error="draft_request_invalid")
        eligibility_error = self._selection_eligibility_error(selection)
        if eligibility_error is not None:
            return DraftAction(False, error=eligibility_error)
        existing = drafts.get(token)
        if existing is not None:
            approval_error = self._approved_record_error(token, selection, existing)
            if approval_error is not None:
                return DraftAction(False, draft_id=token, error=approval_error)
        if existing is not None:
            return DraftAction(
                True,
                token,
                str(existing.get("text", "")),
                str(existing.get("status", "created")),
                selection,
            )
        request = requests.get(token)
        if request is None:
            return DraftAction(False, error="draft_request_invalid")
        if not self._ensure_live_registry():
            return DraftAction(False, error="customer_registry_unavailable")
        if not self._append_draft_event(
            selection,
            "created",
            token,
            candidate,
            actor="ai",
        ):
            return DraftAction(False, error="draft_event_unavailable")
        drafts[token] = {
            "customer_key": selection.customer.spec.customer_key,
            "session_id": request[1],
            "text": candidate,
            "status": "created",
        }
        try:
            self._write_json_private(self._drafts_path, drafts)
        except (OSError, ValueError) as exc:
            return DraftAction(False, error=f"draft_ledger_write_failed:{type(exc).__name__}")
        return DraftAction(True, token, candidate, "created", selection)

    def edit_draft(self, draft_id: str, owner: IncomingAddress, text: str) -> DraftAction:
        with self._delivery_lock():
            return self._edit_draft_locked(draft_id, owner, text)

    def _edit_draft_locked(
        self,
        draft_id: str,
        owner: IncomingAddress,
        text: str,
    ) -> DraftAction:
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        candidate = " ".join(str(text).split()).strip()
        if (
            owner.key != self._registry.owner.key
            or not candidate
            or len(candidate) > 8_000
            or telegram_utf16_length(candidate) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16
        ):
            return DraftAction(False, draft_id=draft_id, error="owner_or_text_invalid")
        try:
            drafts = self._read_drafts()
            requests = self._read_requests()
            deliveries = self._read_deliveries()
        except DraftLedgerError:
            return DraftAction(False, draft_id=draft_id, error="draft_ledger_corrupt")
        record = drafts.get(draft_id)
        eligibility_error = self._record_eligibility_error(record, owner)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        selection = self._draft_selection_from_record(record, owner)
        if selection is not None:
            eligibility_error = self._selection_eligibility_error(selection)
            if eligibility_error is not None:
                return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        if record is not None and selection is not None and record.get("status") == "superseded":
            child_id = str(record.get("superseded_by_draft_id", "") or "")
            child = drafts.get(child_id)
            if (
                isinstance(child, Mapping)
                and child.get("parent_draft_id") == draft_id
                and child.get("text") == candidate
                and child.get("status") in {"edited", "approved", "sent"}
            ):
                return DraftAction(
                    True,
                    child_id,
                    candidate,
                    str(child["status"]),
                    selection,
                )
            return DraftAction(False, draft_id=draft_id, error="draft_not_editable")
        if record is not None and selection is not None and record.get("status") == "approved":
            if any(
                isinstance(delivery, Mapping)
                and delivery.get("draft_id") == draft_id
                for delivery in deliveries.values()
            ):
                return DraftAction(False, draft_id=draft_id, error="draft_not_editable")
            child_id = hashlib.sha256(
                f"{draft_id}\0{candidate}".encode("utf-8")
            ).hexdigest()[:16]
            existing_child = drafts.get(child_id)
            if existing_child is not None:
                if (
                    existing_child.get("customer_key") != record.get("customer_key")
                    or existing_child.get("session_id") != record.get("session_id")
                    or existing_child.get("text") != candidate
                    or existing_child.get("parent_draft_id") != draft_id
                ):
                    return DraftAction(
                        False,
                        draft_id=child_id,
                        error="draft_child_revision_conflict",
                    )
                return DraftAction(True, child_id, candidate, "edited", selection)
            session_id = str(record.get("session_id", "") or "")
            requests[child_id] = (
                str(record.get("customer_key", "") or ""),
                session_id,
            )
            try:
                self._write_json_private(self._requests_path, {
                    token: [customer_key, request_session_id]
                    for token, (customer_key, request_session_id) in requests.items()
                })
            except (OSError, ValueError) as exc:
                return DraftAction(
                    False,
                    draft_id=child_id,
                    error=f"draft_ledger_write_failed:{type(exc).__name__}",
                )
            if not self._append_draft_event(
                selection,
                "edited",
                child_id,
                candidate,
                actor="richard",
                link=draft_id,
            ):
                return DraftAction(
                    False,
                    draft_id=child_id,
                    error="draft_event_unavailable",
                )
            child = {
                "customer_key": str(record["customer_key"]),
                "session_id": session_id,
                "text": candidate,
                "status": "edited",
                "parent_draft_id": draft_id,
            }
            record["status"] = "superseded"
            record["superseded_by_draft_id"] = child_id
            drafts[draft_id] = record
            drafts[child_id] = child
            try:
                self._write_json_private(self._drafts_path, drafts)
            except (OSError, ValueError) as exc:
                return DraftAction(
                    False,
                    draft_id=child_id,
                    error=f"draft_ledger_write_failed:{type(exc).__name__}",
                )
            return DraftAction(True, child_id, candidate, "edited", selection)
        if record is None or selection is None or record.get("status") not in {"created", "edited"}:
            return DraftAction(False, draft_id=draft_id, error="draft_not_editable")
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if not self._append_draft_event(
            selection,
            "edited",
            draft_id,
            candidate,
            actor="richard",
            link=draft_id,
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_event_unavailable")
        record["text"] = candidate
        record["status"] = "edited"
        drafts[draft_id] = record
        try:
            self._write_json_private(self._drafts_path, drafts)
        except (OSError, ValueError) as exc:
            return DraftAction(False, draft_id=draft_id, error=f"draft_ledger_write_failed:{type(exc).__name__}")
        return DraftAction(True, draft_id, candidate, "edited", selection)

    def approve_draft(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        with self._delivery_lock():
            return self._approve_draft_locked(draft_id, owner)

    def _approve_draft_locked(
        self,
        draft_id: str,
        owner: IncomingAddress,
    ) -> DraftAction:
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if owner.key != self._registry.owner.key:
            return DraftAction(False, draft_id=draft_id, error="owner_only")
        try:
            drafts = self._read_drafts()
            self._read_requests()
            self._read_deliveries()
        except DraftLedgerError:
            return DraftAction(False, draft_id=draft_id, error="draft_ledger_corrupt")
        record = drafts.get(draft_id)
        eligibility_error = self._record_eligibility_error(record, owner)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        selection = self._draft_selection_from_record(record, owner)
        if selection is not None:
            eligibility_error = self._selection_eligibility_error(selection)
            if eligibility_error is not None:
                return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        if record is None or selection is None or record.get("status") not in {"created", "edited"}:
            return DraftAction(False, draft_id=draft_id, error="draft_not_approvable")
        text = str(record.get("text", "")).strip()
        if not text:
            return DraftAction(False, draft_id=draft_id, error="draft_event_unavailable")
        if telegram_utf16_length(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16:
            return DraftAction(False, draft_id=draft_id, error="draft_text_too_long")
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        approved_event_id = self._append_draft_event(
            selection,
            "approved",
            draft_id,
            text,
            actor="richard",
            link=draft_id,
        )
        if not approved_event_id:
            return DraftAction(False, draft_id=draft_id, error="draft_event_unavailable")
        if not self._has_canonical_approval(
            selection,
            draft_id,
            text,
            approved_event_id,
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_approval_evidence_missing")
        record["status"] = "approved"
        record["approved_revision"] = self._draft_revision(text)
        record["approved_event_id"] = str(approved_event_id)
        drafts[draft_id] = record
        try:
            self._write_json_private(self._drafts_path, drafts)
        except (OSError, ValueError) as exc:
            return DraftAction(False, draft_id=draft_id, error=f"draft_ledger_write_failed:{type(exc).__name__}")
        return DraftAction(True, draft_id, text, "approved", selection)

    @staticmethod
    def _draft_revision(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _delivery_key(cls, draft_id: str, text: str) -> str:
        return f"{draft_id}:{cls._draft_revision(text)}"

    def _delivery_ledger_path(self) -> Path:
        path = getattr(self, "_deliveries_path", None) or getattr(self, "_outbox_path", None)
        return path if isinstance(path, Path) else self._profile_root / "data" / "owner-actions" / "draft-deliveries.json"

    @staticmethod
    def _ledger_error(_: DraftLedgerError) -> str:
        return "draft_ledger_corrupt"
    @contextmanager
    def _delivery_lock(self) -> Iterator[None]:
        """Serialize outbox compare-and-reserve transitions across callers."""
        path = self._delivery_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a", encoding="utf-8") as handle:
            lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _field(value: object, name: str, default: object = None) -> object:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)
    def _current_customer(self, customer_key: str, fallback: CustomerRuntime) -> CustomerRuntime:
        for candidate in getattr(self._registry, "customers", ()):
            spec = getattr(candidate, "spec", None)
            if getattr(spec, "customer_key", None) == customer_key:
                return candidate
        return fallback

    @classmethod
    def _safety_value_has_hold(cls, value: object) -> bool:
        if value is None:
            return False
        for name in (
            "safety_signals",
            "safety_reasons",
            "signals",
            "reasons",
        ):
            candidate = cls._field(value, name)
            if candidate:
                return True
        safety = cls._field(value, "safety")
        if safety is None:
            return False
        if bool(cls._field(safety, "coaching_held", False)):
            return True
        level = cls._field(safety, "level")
        level_value = getattr(level, "value", level)
        return str(level_value or "").strip().lower() not in {"", "none"}

    @classmethod
    def _bridge_safety_hold(
        cls,
        resolved: ResolvedCustomer,
        session_id: str,
    ) -> bool:
        bridge = resolved.bridge
        safety_snapshot = getattr(bridge, "finalized_safety_snapshot", None)
        if callable(safety_snapshot):
            try:
                if safety_snapshot(session_id) is not None:
                    return True
            except Exception:
                return True
        finalized_event = getattr(bridge, "finalized_event", None)
        if callable(finalized_event):
            try:
                if cls._safety_value_has_hold(finalized_event(session_id)):
                    return True
            except Exception:
                return True
        service = getattr(bridge, "_service", None)
        storage = getattr(service, "_storage", None)
        load = getattr(storage, "load", None)
        if callable(load):
            try:
                session = load(session_id)
            except Exception:
                return True
            if cls._safety_value_has_hold(session):
                return True
        return False

    @staticmethod
    def _consent_error(customer: object) -> str | None:
        spec = getattr(customer, "spec", None)
        consent = NutritionCoachingCoordinator._field(spec, "ai_processing_consent")
        if not bool(NutritionCoachingCoordinator._field(consent, "granted", False)):
            return "customer_consent_required"
        try:
            from checkin_cli.customer_coaching import CONSENT_VERSION
        except ImportError:
            return "customer_consent_required"
        if NutritionCoachingCoordinator._field(consent, "recorded_on") is None:
            return "customer_consent_required"
        if NutritionCoachingCoordinator._field(consent, "notice_version") != CONSENT_VERSION:
            return "customer_consent_required"
        return None

    def _selection_eligibility_error(self, selection: DraftSelection) -> str | None:
        current = self._current_customer(
            selection.customer.spec.customer_key,
            selection.customer,
        )
        if not bool(getattr(current.spec, "enabled", True)):
            return "customer_disabled"
        consent_error = self._consent_error(current)
        if consent_error is not None:
            return consent_error
        if self._safety_value_has_hold(selection.snapshot):
            return "customer_safety_hold"
        if selection.session_id:
            resolved = self._by_key.get(selection.customer.spec.customer_key)
            if resolved is None or self._bridge_safety_hold(resolved, selection.session_id):
                return "customer_safety_hold"
        return None

    def _record_eligibility_error(
        self,
        record: dict[str, object] | None,
        owner: IncomingAddress,
    ) -> str | None:
        if not self._ensure_live_registry() or owner.key != self._registry.owner.key or not isinstance(record, dict):
            return None
        customer_key = record.get("customer_key")
        session_id = record.get("session_id")
        if not isinstance(customer_key, str) or not isinstance(session_id, str):
            return None
        resolved = self._by_key.get(customer_key)
        current = self._registry_customer(customer_key)
        if current is None:
            return "customer_route_unavailable"
        if not bool(getattr(current.spec, "enabled", True)):
            return "customer_disabled"
        if self._consent_error(current) is not None:
            return "customer_consent_required"
        if resolved is None:
            return "customer_route_unavailable"
        if self._bridge_safety_hold(resolved, session_id):
            return "customer_safety_hold"
        return None

    @staticmethod
    def _event_field(value: object, name: str, default: object = None) -> object:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    def _canonical_events(self, resolved: ResolvedCustomer) -> tuple[object, ...]:
        sources: list[object] = [
            getattr(resolved.bridge, "events", None),
            getattr(resolved.bridge, "_events", None),
        ]
        service = getattr(resolved.bridge, "_service", None)
        sources.extend(
            (
                getattr(service, "events", None),
                getattr(service, "_events", None),
            )
        )
        events: list[object] = []
        seen: set[int] = set()
        for source in sources:
            if source is None or id(source) in seen:
                continue
            seen.add(id(source))
            reader = getattr(source, "_read_events", None)
            try:
                values = reader() if callable(reader) else source
                if isinstance(values, Mapping) or isinstance(values, (str, bytes)):
                    continue
                events.extend(item for item in values if item is not None)
            except (OSError, TypeError, ValueError):
                continue
        data_root = getattr(resolved.customer, "data_root", None)
        events_path = data_root / "events.jsonl" if isinstance(data_root, Path) else None
        if events_path is not None and events_path.exists():
            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    if line:
                        events.append(json.loads(line))
            except (OSError, ValueError):
                return ()
        return tuple(events)

    def _has_canonical_approval(
        self,
        selection: DraftSelection,
        draft_id: str,
        text: str,
        approved_event_id: object,
    ) -> bool:
        if not isinstance(approved_event_id, str) or not approved_event_id:
            return False
        for event in self._canonical_events(
            self._by_key[selection.customer.spec.customer_key]
        ):
            event_id = self._event_field(event, "event_id")
            if event_id != approved_event_id:
                continue
            event_type = self._event_field(event, "event_type")
            event_type = getattr(event_type, "value", event_type)
            if str(event_type) != "draft_approved":
                continue
            status = self._event_field(event, "status")
            status = getattr(status, "value", status)
            if str(status) != "accepted":
                continue
            payload = self._event_field(event, "draft")
            if payload is None:
                continue
            actor = self._event_field(payload, "actor")
            actor = getattr(actor, "value", actor)
            if (
                self._event_field(payload, "draft_id") == draft_id
                and self._event_field(payload, "text") == text
                and str(actor) == "richard"
            ):
                return True
        return False
    def _has_canonical_sent(
        self,
        selection: DraftSelection,
        draft_id: str,
        text: str,
        message_id: str,
    ) -> bool:
        for event in self._canonical_events(
            self._by_key[selection.customer.spec.customer_key]
        ):
            event_type = self._event_field(event, "event_type")
            event_type = getattr(event_type, "value", event_type)
            if str(event_type) != "draft_sent":
                continue
            status = self._event_field(event, "status")
            status = getattr(status, "value", status)
            if str(status) != "accepted":
                continue
            payload = self._event_field(event, "draft")
            actor = self._event_field(payload, "actor")
            actor = getattr(actor, "value", actor)
            if (
                self._event_field(payload, "draft_id") == draft_id
                and self._event_field(payload, "text") == text
                and self._event_field(payload, "approved_message_id") == message_id
                and str(actor) == "richard"
            ):
                return True
        return False

    def _approved_record_error(
        self,
        draft_id: str,
        selection: DraftSelection,
        record: dict[str, object],
    ) -> str | None:
        if record.get("status") not in {"approved", "sent"}:
            return None
        text = str(record.get("text", "")).strip()
        if telegram_utf16_length(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16:
            return "draft_text_too_long"
        revision = self._draft_revision(text)
        approved_revision = record.get("approved_revision")
        if not isinstance(approved_revision, str) or not approved_revision:
            return "draft_approval_revision_missing"
        if approved_revision != revision:
            return "draft_approval_revision_mismatch"
        approved_event_id = record.get("approved_event_id")
        if not self._has_canonical_approval(
            selection,
            draft_id,
            text,
            approved_event_id,
        ):
            return "draft_approval_evidence_missing"
        return None

    def _record_identity(self, record: dict[str, object] | None) -> tuple[str, str] | None:
        if not isinstance(record, dict):
            return None
        customer_key = record.get("customer_key")
        session_id = record.get("session_id")
        if not isinstance(customer_key, str) or not isinstance(session_id, str):
            return None
        return customer_key, session_id

    def prepare_delivery(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        """Durably reserve an approved revision before any customer transport."""
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if owner.key != self._registry.owner.key:
            return DraftAction(False, draft_id=draft_id, error="owner_only")
        with self._delivery_lock():
            if not self._ensure_live_registry():
                return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
            try:
                drafts = self._read_drafts()
                self._read_requests()
                deliveries = self._read_deliveries()
            except DraftLedgerError as exc:
                return DraftAction(False, draft_id=draft_id, error=self._ledger_error(exc))
            return self._prepare_delivery_locked(draft_id, owner, drafts, deliveries)

    def _prepare_delivery_locked(
        self,
        draft_id: str,
        owner: IncomingAddress,
        drafts: dict[str, dict[str, object]],
        deliveries: dict[str, dict[str, object]],
    ) -> DraftAction:
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        record = drafts.get(draft_id)
        eligibility_error = self._record_eligibility_error(record, owner)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        selection = self._draft_selection_from_record(record, owner)
        if record is None or selection is None:
            return DraftAction(False, draft_id=draft_id, error="draft_not_approved")
        eligibility_error = self._selection_eligibility_error(selection)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        approval_error = self._approved_record_error(draft_id, selection, record)
        if approval_error is not None:
            return DraftAction(False, draft_id=draft_id, error=approval_error)
        text = str(record.get("text", "")).strip()
        if telegram_utf16_length(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16:
            return DraftAction(False, draft_id=draft_id, text=text, error="draft_text_too_long")
        revision = self._draft_revision(text)
        delivery_key = self._delivery_key(draft_id, text)
        existing = deliveries.get(delivery_key)
        for key, candidate in deliveries.items():
            if key != delivery_key and candidate.get("draft_id") == draft_id:
                return DraftAction(False, draft_id=draft_id, error="draft_delivery_revision_mismatch")
        if record.get("status") != "approved" and not (
            record.get("status") == "sent"
            and existing is not None
            and existing.get("status") == "sent_audited"
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_not_approved")
        approved_revision = str(record["approved_revision"])
        approved_event_id = str(record["approved_event_id"])
        if existing is None:
            intent = {
                "draft_id": draft_id,
                "customer_key": selection.customer.spec.customer_key,
                "session_id": str(record["session_id"]),
                "text": text,
                "revision": revision,
                "approved_revision": approved_revision,
                "approved_event_id": approved_event_id,
                "status": "approved",
            }
            try:
                next_deliveries = dict(deliveries)
                next_deliveries[delivery_key] = intent
                self._write_json_private(self._delivery_ledger_path(), next_deliveries)
                intent["status"] = "pending"
                self._write_json_private(self._delivery_ledger_path(), next_deliveries)
            except (OSError, ValueError) as exc:
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=text,
                    error=f"draft_delivery_write_failed:{type(exc).__name__}",
                )
            return DraftAction(True, draft_id, text, "pending", selection, None, None, True)
        status = str(existing.get("status", ""))
        if (
            existing.get("customer_key") != selection.customer.spec.customer_key
            or existing.get("session_id") != str(record["session_id"])
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_delivery_identity_mismatch")
        if (
            existing.get("text") != text
            or existing.get("revision") != revision
            or existing.get("approved_revision") != approved_revision
            or existing.get("approved_event_id") != approved_event_id
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_delivery_revision_mismatch")
        message_id = existing.get("message_id")
        if record.get("status") != "approved":
            if record.get("status") == "sent" and status == "sent_audited":
                return DraftAction(
                    True,
                    draft_id,
                    text,
                    "sent_audited",
                    selection,
                    None,
                    str(message_id) if message_id is not None else None,
                    False,
                )
            return DraftAction(False, draft_id=draft_id, error="draft_not_approved")
        if status == "approved":
            next_deliveries = dict(deliveries)
            pending = dict(existing)
            pending["status"] = "pending"
            next_deliveries[delivery_key] = pending
            try:
                self._write_json_private(self._delivery_ledger_path(), next_deliveries)
            except (OSError, ValueError) as exc:
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=text,
                    error=f"draft_delivery_write_failed:{type(exc).__name__}",
                )
            return DraftAction(True, draft_id, text, "pending", selection, None, None, True)
        if status not in {"pending", "delivered", "sent_audited"}:
            return DraftAction(False, draft_id=draft_id, error="draft_delivery_state_invalid")
        return DraftAction(
            True,
            draft_id,
            text,
            status,
            selection,
            None,
            str(message_id) if message_id is not None else None,
            False,
        )
    def prepare_send_draft(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        """Compatibility name for :meth:`prepare_delivery`."""
        return self.prepare_delivery(draft_id, owner)

    def mark_delivery_pending(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        """Ensure an approved revision has a durable pending reservation."""
        return self.prepare_delivery(draft_id, owner)

    def mark_delivered(
        self,
        draft_id: str,
        owner: IncomingAddress,
        message_id: str | int,
    ) -> DraftAction:
        """Persist the transport receipt before writing the canonical sent event."""
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        action = self.prepare_delivery(draft_id, owner)
        if not action.accepted or action.selection is None or action.text is None:
            return action
        receipt = str(message_id).strip()
        if not receipt or len(receipt) > 128:
            return DraftAction(False, draft_id=draft_id, error="delivery_message_id_invalid")
        with self._delivery_lock():
            if not self._ensure_live_registry():
                return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
            try:
                return self._mark_delivered_locked(action, draft_id, owner, receipt)
            except DraftLedgerError:
                return DraftAction(False, draft_id=draft_id, error="draft_ledger_corrupt")

    def _mark_delivered_locked(
        self,
        action: DraftAction,
        draft_id: str,
        owner: IncomingAddress,
        receipt: str,
    ) -> DraftAction:
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        record_gate = self._record_eligibility_error(
            self._read_drafts().get(draft_id),
            owner,
        )
        if record_gate is not None:
            return DraftAction(False, draft_id=draft_id, error=record_gate)
        selection_gate = self._selection_eligibility_error(action.selection)
        if selection_gate is not None:
            return DraftAction(False, draft_id=draft_id, error=selection_gate)
        if action.status == "sent_audited":
            if action.message_id != receipt:
                return DraftAction(False, draft_id=draft_id, error="delivery_receipt_conflict")
            return action
        deliveries = self._read_deliveries()
        key = self._delivery_key(draft_id, action.text)
        record = deliveries.get(key)
        if record is None or record.get("status") not in {"pending", "delivered"}:
            return DraftAction(False, draft_id=draft_id, error="delivery_not_pending")
        prior = record.get("message_id")
        if prior is not None and str(prior) != receipt:
            return DraftAction(False, draft_id=draft_id, error="delivery_receipt_conflict")
        if record.get("status") == "delivered":
            return DraftAction(
                True,
                draft_id,
                action.text,
                "delivered",
                action.selection,
                None,
                receipt,
                False,
            )
        next_deliveries = dict(deliveries)
        delivered = dict(record)
        delivered["status"] = "delivered"
        delivered["message_id"] = receipt
        next_deliveries[key] = delivered
        try:
            self._write_json_private(self._delivery_ledger_path(), next_deliveries)
        except (OSError, ValueError) as exc:
            return DraftAction(
                False,
                draft_id=draft_id,
                text=action.text,
                error=f"draft_delivery_write_failed:{type(exc).__name__}",
            )
        return DraftAction(
            True,
            draft_id,
            action.text,
            "delivered",
            action.selection,
            None,
            receipt,
            False,
        )

    def mark_sent_audited(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        """Reconcile a durable receipt into one canonical ``draft_sent`` event."""
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        action = self.prepare_delivery(draft_id, owner)
        if not action.accepted or action.selection is None or action.text is None:
            return action
        if action.status in {"approved", "pending"} or not action.message_id:
            return DraftAction(
                False,
                draft_id=draft_id,
                text=action.text,
                status=action.status,
                selection=action.selection,
                error="delivery_receipt_pending",
            )
        with self._delivery_lock():
            if not self._ensure_live_registry():
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=action.text,
                    status=action.status,
                    selection=action.selection,
                    message_id=action.message_id,
                    error="customer_registry_unavailable",
                )
            try:
                return self._mark_sent_audited_locked(action, draft_id, owner)
            except (DraftLedgerError, OSError, ValueError) as exc:
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=action.text,
                    status="delivered",
                    selection=action.selection,
                    message_id=action.message_id,
                    error=(
                        "draft_ledger_corrupt"
                        if isinstance(exc, DraftLedgerError)
                        else f"draft_ledger_write_failed:{type(exc).__name__}"
                    ),
                )

    def _mark_sent_audited_locked(
        self,
        action: DraftAction,
        draft_id: str,
        owner: IncomingAddress,
    ) -> DraftAction:
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        drafts = self._read_drafts()
        draft_record = drafts.get(draft_id)
        record_gate = self._record_eligibility_error(draft_record, owner)
        if record_gate is not None:
            return DraftAction(False, draft_id=draft_id, error=record_gate)
        selection_gate = self._selection_eligibility_error(action.selection)
        if selection_gate is not None:
            return DraftAction(False, draft_id=draft_id, error=selection_gate)
        if isinstance(draft_record, dict):
            approval_error = self._approved_record_error(draft_id, action.selection, draft_record)
            if approval_error is not None:
                return DraftAction(False, draft_id=draft_id, error=approval_error)
        deliveries = self._read_deliveries()
        key = self._delivery_key(draft_id, action.text)
        record = deliveries.get(key)
        if record is None or not record.get("message_id"):
            return DraftAction(False, draft_id=draft_id, error="delivery_receipt_missing")
        if record.get("status") == "sent_audited":
            if isinstance(draft_record, dict) and draft_record.get("status") != "sent":
                draft_record["status"] = "sent"
                drafts[draft_id] = draft_record
                self._write_json_private(self._drafts_path, drafts)
            return DraftAction(
                True,
                draft_id,
                action.text,
                "sent_audited",
                action.selection,
                None,
                str(record["message_id"]),
                False,
            )
        if record.get("status") != "delivered":
            return DraftAction(False, draft_id=draft_id, error="delivery_receipt_missing")
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if not self._has_canonical_sent(
            action.selection,
            draft_id,
            action.text,
            str(record["message_id"]),
        ):
            if not self._append_draft_event(
                action.selection,
                "sent",
                draft_id,
                action.text,
                actor="richard",
                link=draft_id,
                approved_message_id=str(record["message_id"]),
            ):
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=action.text,
                    status="delivered",
                    selection=action.selection,
                    message_id=str(record["message_id"]),
                    error="draft_event_unavailable",
                )
        next_deliveries = dict(deliveries)
        audited = dict(record)
        audited["status"] = "sent_audited"
        next_deliveries[key] = audited
        # Persist the receipt ledger before the mutable drafts convenience index.
        self._write_json_private(self._delivery_ledger_path(), next_deliveries)
        if isinstance(draft_record, dict):
            draft_record["status"] = "sent"
            drafts[draft_id] = draft_record
            self._write_json_private(self._drafts_path, drafts)
        return DraftAction(
            True,
            draft_id,
            action.text,
            "sent_audited",
            action.selection,
            None,
            str(record["message_id"]),
            False,
        )

    def reconcile_delivery(
        self,
        draft_id: str,
        owner: IncomingAddress,
        message_id: str | int | None = None,
    ) -> DraftAction:
        """Reconcile pending/delivered state without ever retrying transport blindly."""
        action = self.prepare_delivery(draft_id, owner)
        if not action.accepted:
            return action
        if action.status == "pending":
            if message_id is None:
                return DraftAction(
                    False,
                    draft_id=draft_id,
                    text=action.text,
                    status="pending",
                    selection=action.selection,
                    error="delivery_reconciliation_required",
                )
            delivered = self.mark_delivered(draft_id, owner, message_id)
            if not delivered.accepted:
                return delivered
        elif action.status == "sent_audited":
            return action
        elif action.status != "delivered":
            return DraftAction(False, draft_id=draft_id, error="delivery_receipt_pending")
        return self.mark_sent_audited(draft_id, owner)

    def reconcile_draft_delivery(
        self,
        draft_id: str,
        owner: IncomingAddress,
        message_id: str | int | None = None,
    ) -> DraftAction:
        """Compatibility alias for :meth:`reconcile_delivery`."""
        return self.reconcile_delivery(draft_id, owner, message_id)

    def mark_draft_sent(
        self,
        draft_id: str,
        owner: IncomingAddress,
        message_id: str | int | None = None,
    ) -> DraftAction:
        """Record a transport receipt and canonical sent audit."""
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if message_id is not None:
            return self.reconcile_delivery(draft_id, owner, message_id)
        if hasattr(self, "_deliveries_path") or hasattr(self, "_outbox_path"):
            return DraftAction(False, draft_id=draft_id, error="delivery_receipt_required")
        # Keep the pre-outbox test double/API behavior for profile snapshots that
        # construct this coordinator without durable delivery paths.
        action = self.prepare_send_draft(draft_id, owner)
        if not action.accepted or action.selection is None or action.text is None:
            return action
        try:
            drafts = self._read_drafts()
        except DraftLedgerError:
            return DraftAction(False, draft_id=draft_id, error="draft_ledger_corrupt")
        record = drafts.get(draft_id)
        eligibility_error = self._record_eligibility_error(record, owner)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        eligibility_error = self._selection_eligibility_error(action.selection)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        if not self._ensure_live_registry():
            return DraftAction(False, draft_id=draft_id, error="customer_registry_unavailable")
        if not self._append_draft_event(
            action.selection,
            "sent",
            draft_id,
            action.text,
            actor="richard",
            link=draft_id,
        ):
            return DraftAction(False, draft_id=draft_id, error="draft_event_unavailable")
        record = drafts.get(draft_id)
        if record is not None:
            record["status"] = "sent"
            drafts[draft_id] = record
            try:
                self._write_json_private(self._drafts_path, drafts)
            except (OSError, ValueError) as exc:
                return DraftAction(False, draft_id=draft_id, error=f"draft_ledger_write_failed:{type(exc).__name__}")
        return DraftAction(True, draft_id, action.text, "sent", action.selection)

    def send_draft(
        self,
        draft_id: str,
        owner: IncomingAddress,
        message_id: str | int | None = None,
    ) -> DraftAction:
        """Compatibility alias for the owner-confirmed send lifecycle step."""
        return self.mark_draft_sent(draft_id, owner, message_id)

    def draft(self, draft_id: str, owner: IncomingAddress) -> DraftAction:
        if not self._ensure_live_registry() or owner.key != self._registry.owner.key:
            return DraftAction(False, draft_id=draft_id, error="owner_only")
        try:
            drafts = self._read_drafts()
        except DraftLedgerError:
            return DraftAction(False, draft_id=draft_id, error="draft_ledger_corrupt")
        record = drafts.get(draft_id)
        selection = self._draft_selection_from_record(record, owner)
        if record is None or selection is None:
            return DraftAction(False, draft_id=draft_id, error="draft_not_found")
        eligibility_error = self._selection_eligibility_error(selection)
        if eligibility_error is not None:
            return DraftAction(False, draft_id=draft_id, error=eligibility_error)
        return DraftAction(True, draft_id, str(record.get("text", "")), str(record.get("status", "")), selection)

    def _draft_selection_from_record(
        self,
        record: dict[str, object] | None,
        owner: IncomingAddress,
    ) -> DraftSelection | None:
        if not self._ensure_live_registry() or owner.key != self._registry.owner.key or not isinstance(record, dict):
            return None
        customer_key = record.get("customer_key")
        session_id = record.get("session_id")
        if not isinstance(customer_key, str) or not isinstance(session_id, str):
            return None
        resolved = self._by_key.get(customer_key)
        if resolved is None:
            return None
        current = self._current_customer(customer_key, resolved.customer)
        snapshot = resolved.bridge.finalized_coaching_snapshot(session_id)
        if snapshot is None:
            return None
        try:
            from checkin_cli.customer_grounding import CustomerSnapshot

            return DraftSelection(
                current,
                CustomerSnapshot.model_validate(snapshot),
                session_id,
            )
        except (ImportError, TypeError, ValueError):
            return None

    def _append_draft_event(
        self,
        selection: DraftSelection,
        kind: str,
        draft_id: str,
        text: str,
        *,
        actor: str,
        link: str | None = None,
        approved_message_id: str | None = None,
    ) -> str | None:
        resolved = self._by_key.get(selection.customer.spec.customer_key)
        if resolved is None:
            return None
        try:
            from checkin_cli import models

            builder = getattr(models, f"build_draft_{kind}_event", None)
            if not callable(builder):
                return None
            values = {
                "customer_key": selection.customer.spec.customer_key,
                "draft_id": draft_id,
                "actor": actor,
                "text": text,
                "session_id": self._read_requests().get(draft_id, ("", None))[1],
                "edited_from_draft_id": link,
                "approved_from_draft_id": link,
                "approved_message_id": approved_message_id,
            }
            params = inspect.signature(builder).parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in params.values()
            )
            event = builder(
                **(
                    values
                    if accepts_kwargs
                    else {name: value for name, value in values.items() if name in params}
                )
            )
            appended = resolved.bridge.append_event(event)
            if appended is None:
                return None
            event_id = getattr(appended, "event_id", None) or getattr(event, "event_id", None)
            return str(event_id) if event_id else None
        except Exception:
            return None

    def _read_deliveries(self) -> dict[str, dict[str, object]]:
        path = self._delivery_ledger_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DraftLedgerError(
                f"{path.name} is unreadable; repair or restore it before retrying ({type(exc).__name__})"
            ) from exc
        if not isinstance(payload, dict):
            raise DraftLedgerError(f"{path.name} root must be an object")
        deliveries: dict[str, dict[str, object]] = {}
        required = {
            "draft_id",
            "customer_key",
            "session_id",
            "text",
            "revision",
            "approved_revision",
            "approved_event_id",
            "status",
        }
        allowed_states = {"approved", "pending", "delivered", "sent_audited"}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise DraftLedgerError(f"{path.name} contains a malformed record")
            if not required.issubset(value):
                raise DraftLedgerError(f"{path.name} record {key!r} is malformed")
            if not all(isinstance(value[item], str) for item in required):
                raise DraftLedgerError(f"{path.name} record {key!r} is malformed")
            if any(not value[item] for item in required):
                raise DraftLedgerError(f"{path.name} record {key!r} is malformed")
            if (
                not 1 <= len(value["text"]) <= 8_000
                or telegram_utf16_length(value["text"]) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16
            ):
                raise DraftLedgerError(f"{path.name} record {key!r} has invalid text")
            if value["status"] not in allowed_states:
                raise DraftLedgerError(f"{path.name} record {key!r} has an invalid status")
            if value["revision"] != self._draft_revision(value["text"]):
                raise DraftLedgerError(f"{path.name} record {key!r} has an invalid revision")
            if key != self._delivery_key(value["draft_id"], value["text"]):
                raise DraftLedgerError(f"{path.name} record {key!r} has an invalid identity")
            approved_revision = value.get("approved_revision")
            if approved_revision is not None and approved_revision != value["revision"]:
                raise DraftLedgerError(f"{path.name} record {key!r} has an invalid approved revision")
            message_id = value.get("message_id")
            if message_id is not None and (not isinstance(message_id, str) or not 0 < len(message_id) <= 128):
                raise DraftLedgerError(f"{path.name} record {key!r} has an invalid message id")
            if value["status"] in {"approved", "pending"} and message_id is not None:
                raise DraftLedgerError(f"{path.name} record {key!r} has an early receipt")
            if value["status"] in {"delivered", "sent_audited"} and not message_id:
                raise DraftLedgerError(f"{path.name} record {key!r} is missing its receipt")
            deliveries[key] = value
        return deliveries
    def _read_drafts(self) -> dict[str, dict[str, object]]:
        if not self._drafts_path.exists():
            return {}
        try:
            payload = json.loads(self._drafts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DraftLedgerError(
                f"drafts.json is unreadable; repair or restore it before retrying ({type(exc).__name__})"
            ) from exc
        if not isinstance(payload, dict):
            raise DraftLedgerError("drafts.json root must be an object")
        records: dict[str, dict[str, object]] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise DraftLedgerError("drafts.json contains a malformed record")
            required = {"customer_key", "session_id", "text", "status"}
            if not required.issubset(value) or not all(isinstance(value[item], str) for item in required):
                raise DraftLedgerError(f"drafts.json record {key!r} is malformed")
            if value["status"] not in {"created", "edited", "approved", "superseded", "sent"}:
                raise DraftLedgerError(f"drafts.json record {key!r} has an invalid status")
            if (
                not 1 <= len(value["text"]) <= 8_000
                or telegram_utf16_length(value["text"]) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16
            ):
                raise DraftLedgerError(f"drafts.json record {key!r} has invalid text")
            if "approved_revision" in value and not isinstance(value["approved_revision"], str):
                raise DraftLedgerError(f"drafts.json record {key!r} has a malformed approved revision")
            if "approved_event_id" in value and not isinstance(value["approved_event_id"], str):
                raise DraftLedgerError(f"drafts.json record {key!r} has a malformed approved event id")
            records[key] = value
        return records

    @staticmethod
    def _write_json_private(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)

    def _save_request(
        self,
        token: str,
        customer_key: str,
        session_id: str,
    ) -> tuple[str, str]:
        with self._delivery_lock():
            requests = self._read_requests()
            self._read_drafts()
            self._read_deliveries()
            existing = requests.get(token)
            if existing is not None:
                if existing[0] != customer_key:
                    raise DraftLedgerError(
                        "draft request token is already bound to another customer"
                    )
                return existing
            requests[token] = (customer_key, session_id)
            try:
                self._write_json_private(self._requests_path, requests)
            except (OSError, ValueError) as exc:
                raise DraftLedgerError(
                    f"draft-requests.json could not be persisted; repair or restore it before retrying ({type(exc).__name__})"
                ) from exc
            return requests[token]

    def _read_requests(self) -> dict[str, tuple[str, str]]:
        if not self._requests_path.exists():
            return {}
        try:
            payload = json.loads(self._requests_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DraftLedgerError(
                f"draft-requests.json is unreadable; repair or restore it before retrying ({type(exc).__name__})"
            ) from exc
        if not isinstance(payload, dict):
            raise DraftLedgerError("draft-requests.json root must be an object")
        requests: dict[str, tuple[str, str]] = {}
        for token, value in payload.items():
            if (
                not isinstance(token, str)
                or not isinstance(value, list)
                or len(value) != 2
                or not all(isinstance(item, str) for item in value)
            ):
                raise DraftLedgerError("draft-requests.json contains a malformed record")
            requests[token] = (value[0], value[1])
        return requests


OPERATOR_REVIEW_TOPIC_ID = 59


_ADAPTIVE_ACTIONS = frozenset(
    {
        "select",
        "create",
        "view",
        "edit_note",
        "hold",
        "release",
        "rollback",
        "approve",
        "approve_and_send",
        "schedule_confirm",
        "activate",
        "delivery_enable",
        "delivery_revoke",
        "send",
        "reconcile",
        "back",
    }
)
_ADAPTIVE_CALLBACK_RE = re.compile(
    r"^an1:([a-f0-9]{24}):(select|create|view|edit_note|hold|release|approve|approve_and_send|schedule_confirm|activate|"
    r"delivery_enable|delivery_revoke|send|reconcile|back)$"
)

_ADAPTIVE_SESSION_STATES: Mapping[str, frozenset[str]] = {
    "issued": frozenset(
        {
            "issued",
            "claimed",
            "consumed",
            "awaiting_input",
            "expired",
            "revoked",
            "publish_pending",
            "publish_claimed",
            "published",
        }
    ),
    "claimed": frozenset({"claimed", "consumed", "awaiting_input", "expired", "revoked"}),
    "awaiting_input": frozenset({"consumed", "expired", "revoked"}),
    "publish_pending": frozenset({"publish_claimed", "published"}),
    "publish_claimed": frozenset({"publish_pending", "publish_claimed", "published"}),
    "consumed": frozenset({"consumed", "publish_pending", "schedule_confirmed"}),
    "schedule_confirmed": frozenset({"schedule_confirmed", "consumed"}),
    "expired": frozenset(),
    "revoked": frozenset(),
    "published": frozenset(),
}

_ADAPTIVE_SESSION_IMMUTABLE_FIELDS = (
    "schema_version",
    "session_id",
    "token_hash",
    "nonce_digest",
    "action",
    "action_allowlist",
    "customer_key",
    "proposal_digest",
    "revision",
    "schedule_event_id",
    "schedule_event_digest",
    "epoch_digest",
    "source_digest",
    "registration_digest",
    "policy_digest",
    "catalog_digest",
    "meal_constraints_digest",
    "authority_digest",
    "config_digest",
    "registry_digest",
    "consent_digest",
    "activation_digest",
    "review_operator",
    "review_config_version",
    "canonical_owner_snapshot",
    "canonical_owner_version",
    "issued_kst",
    "expires_kst",
    "schedule_confirm_intent",
)


def _is_adaptive_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class AdaptiveOperatorCapability:
    """Short-lived, typed authority minted from the configured review ingress."""

    schema_version: str
    capability_id: str
    review_operator: tuple[str, str, str]
    review_operator_version: int
    canonical_owner: tuple[str, str, str]
    canonical_owner_version: int
    customer_key: str
    action: str
    proposal_digest: str | None
    revision: int | None
    config_digest: str
    registry_digest: str
    consent_digest: str
    activation_digest: str
    issued_kst: str
    expires_kst: str
    nonce_digest: str
    epoch_digest: str = ""
    source_digest: str = ""
    registration_digest: str = ""
    originating_message_id: str = ""
    originating_chat_id: str = ""
    originating_topic_id: str = ""
    provenance_digest: str = ""
    policy_digest: str = ""
    catalog_digest: str = ""
    meal_constraints_digest: str = ""
    schedule_event_id: str | None = None
    schedule_event_digest: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise AdaptiveWorkflowError("adaptive capability version is invalid")
        if (
            not isinstance(self.capability_id, str)
            or not re.fullmatch(r"[a-f0-9]{24,64}", self.capability_id)
        ):
            raise AdaptiveWorkflowError("adaptive capability id is invalid")
        if self.action not in _ADAPTIVE_ACTIONS:
            raise AdaptiveWorkflowError("adaptive capability action is invalid")
        if not isinstance(self.customer_key, str) or not self.customer_key.strip():
            raise AdaptiveWorkflowError("adaptive capability customer is invalid")
        for address in (self.review_operator, self.canonical_owner):
            if (
                not isinstance(address, tuple)
                or len(address) != 3
                or any(not isinstance(value, str) or not value.strip() for value in address)
            ):
                raise AdaptiveWorkflowError("adaptive capability address is invalid")
        if (
            isinstance(self.review_operator_version, bool)
            or not isinstance(self.review_operator_version, int)
            or self.review_operator_version < 1
            or isinstance(self.canonical_owner_version, bool)
            or not isinstance(self.canonical_owner_version, int)
            or self.canonical_owner_version < 1
        ):
            raise AdaptiveWorkflowError("adaptive capability authority version is invalid")
        if self.revision is not None and (
            isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1
        ):
            raise AdaptiveWorkflowError("adaptive capability revision is invalid")
        if (
            not isinstance(self.nonce_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.nonce_digest)
            or not hmac.compare_digest(
                self.nonce_digest,
                hashlib.sha256(self.capability_id.encode("ascii")).hexdigest(),
            )
        ):
            raise AdaptiveWorkflowError("adaptive capability nonce is invalid")
        try:
            issued = datetime.fromisoformat(str(self.issued_kst))
            expires = datetime.fromisoformat(str(self.expires_kst))
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive capability expiry is invalid") from exc
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=_KST)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_KST)
        if expires <= issued:
            raise AdaptiveWorkflowError("adaptive capability expiry is invalid")
        if self.action == "schedule_confirm":
            if (
                not isinstance(self.schedule_event_id, str)
                or not self.schedule_event_id.strip()
                or not _is_adaptive_digest(self.schedule_event_digest)
            ):
                raise AdaptiveWorkflowError("adaptive schedule confirmation reference is invalid")
        elif self.schedule_event_id is not None or self.schedule_event_digest is not None:
            raise AdaptiveWorkflowError("adaptive schedule confirmation reference is unexpected")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "review_operator": list(self.review_operator),
            "review_operator_version": self.review_operator_version,
            "canonical_owner": list(self.canonical_owner),
            "canonical_owner_version": self.canonical_owner_version,
            "customer_key": self.customer_key,
            "action": self.action,
            "proposal_digest": self.proposal_digest,
            "revision": self.revision,
            "config_digest": self.config_digest,
            "registry_digest": self.registry_digest,
            "consent_digest": self.consent_digest,
            "activation_digest": self.activation_digest,
            "issued_kst": self.issued_kst,
            "expires_kst": self.expires_kst,
            "nonce_digest": self.nonce_digest,
            "epoch_digest": self.epoch_digest,
            "source_digest": self.source_digest,
            "registration_digest": self.registration_digest,
            "originating_message_id": self.originating_message_id,
            "originating_chat_id": self.originating_chat_id,
            "originating_topic_id": self.originating_topic_id,
            "provenance_digest": self.provenance_digest,
            "policy_digest": self.policy_digest,
            "catalog_digest": self.catalog_digest,
            "meal_constraints_digest": self.meal_constraints_digest,
            "schedule_event_id": self.schedule_event_id,
            "schedule_event_digest": self.schedule_event_digest,
        }


@dataclass(frozen=True, slots=True)
class ScheduleConfirmationReference:
    event_id: str
    event_digest: str

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not _is_adaptive_digest(self.event_digest):
            raise AdaptiveWorkflowError("adaptive schedule confirmation reference is invalid")


@dataclass(frozen=True, slots=True)
class ScheduleConfirmationRequest:
    capability: AdaptiveOperatorCapability
    schedule_event_id: str
    schedule_event_digest: str

    def __post_init__(self) -> None:
        if self.capability.action != "schedule_confirm":
            raise AdaptiveWorkflowError("adaptive schedule confirmation capability is invalid")
        if (
            self.schedule_event_id != self.capability.schedule_event_id
            or self.schedule_event_digest != self.capability.schedule_event_digest
        ):
            raise AdaptiveWorkflowError("adaptive schedule confirmation reference is stale")
class _ScheduleConfirmationFacade:
    """Adapt gateway review authority to the profile's canonical confirmation API."""

    def __init__(self, coordinator: NutritionCoachingCoordinator) -> None:
        self._coordinator = coordinator

    def _registered_customer(self, customer_key: object) -> CustomerRuntime:
        key = str(customer_key or "").strip()
        if not key or not self._coordinator._ensure_live_registry():
            raise AdaptiveWorkflowError("adaptive schedule confirmation is unavailable")
        resolved = getattr(self._coordinator, "_by_key", {}).get(key)
        customer = getattr(resolved, "customer", None)
        if customer is None or getattr(getattr(customer, "spec", None), "enabled", False) is not True:
            raise AdaptiveWorkflowError("adaptive schedule confirmation customer is unavailable")
        return customer

    @staticmethod
    def _reference(customer: CustomerRuntime, customer_key: str) -> ScheduleConfirmationReference:
        from checkin_cli.customer_coaching import RegisteredCustomerDualCoachCoordinator
        from checkin_cli.store import CanonicalEventTransaction

        registered = RegisteredCustomerDualCoachCoordinator(customer)
        event = registered.current_reference(customer_key)
        if event is None:
            raise AdaptiveWorkflowError("adaptive schedule confirmation reference is unavailable")
        return ScheduleConfirmationReference(
            event.event_id,
            CanonicalEventTransaction.schedule_reference_digest(event),
        )

    def current_reference(self, customer_key: str) -> ScheduleConfirmationReference:
        customer = self._registered_customer(customer_key)
        return self._reference(customer, str(customer_key))

    def _actor_pin(
        self,
        request: ScheduleConfirmationRequest,
        customer: CustomerRuntime,
    ) -> tuple[str, str]:
        capability = request.capability
        review_user, review_chat, review_topic = capability.review_operator
        expected_review = AdaptiveOperatorService._review_tuple(
            self._coordinator._review_operator
        )
        expected_version = AdaptiveOperatorService._review_version(
            self._coordinator._review_operator
        )
        if (
            capability.customer_key != customer.spec.customer_key
            or capability.originating_chat_id != review_chat
            or capability.originating_topic_id != review_topic
            or capability.review_operator != expected_review
            or capability.review_operator_version != expected_version
        ):
            raise AdaptiveWorkflowError("adaptive schedule confirmation authority is stale")
        pin = _coaching_digest(
            {
                "customer_key": customer.spec.customer_key,
                "actor": review_user,
                "review_operator": capability.review_operator,
                "review_operator_version": capability.review_operator_version,
                "canonical_owner": capability.canonical_owner,
                "canonical_owner_version": capability.canonical_owner_version,
                "registry_digest": capability.registry_digest,
                "consent_digest": capability.consent_digest,
                "activation_digest": capability.activation_digest,
                "provenance_digest": capability.provenance_digest,
            }
        )
        return review_user, pin

    def confirm(self, request: ScheduleConfirmationRequest) -> Mapping[str, object]:
        if not isinstance(request, ScheduleConfirmationRequest):
            raise TypeError("adaptive schedule confirmation request is required")
        customer = self._registered_customer(request.capability.customer_key)
        reference = self._reference(customer, request.capability.customer_key)
        if (
            request.schedule_event_id != reference.event_id
            or request.schedule_event_digest != reference.event_digest
        ):
            raise AdaptiveWorkflowError("adaptive schedule confirmation reference is stale")
        actor_id, pin_digest = self._actor_pin(request, customer)
        from checkin_cli.customer_coaching import (
            RegisteredCustomerDualCoachCoordinator,
            ScheduleConfirmRequest,
        )
        from checkin_cli.models import build_schedule_confirmation_event

        event = build_schedule_confirmation_event(
            customer.spec.customer_key,
            reference.event_id,
            reference.event_digest,
            actor_id,
            pin_digest,
            occurred_at_kst=request.capability.issued_kst,
            recorded_at_kst=request.capability.issued_kst,
        )
        receipt = RegisteredCustomerDualCoachCoordinator(customer).confirm(
            ScheduleConfirmRequest(customer.spec.customer_key, event)
        )
        return {
            "canonical_event": dict(receipt.canonical_event),
            "canonical_sequence": dict(receipt.canonical_sequence),
            "adaptive_projection": dict(receipt.adaptive_projection),
        }
def adaptive_delivery_result_text(result: object) -> str:
    """Map durable delivery outcomes to Korean no-retry-safe operator text."""
    payload = result if isinstance(result, Mapping) else {}
    event_type = str(payload.get("event_type", payload.get("status", "")) or "")
    if event_type == "delivery_preflight_rejected":
        return "고객 전송 전에 안전하게 중단되었습니다. 최신 검토 카드에서 다시 시도해 주세요."
    if event_type in {"delivery_unknown", "unknown"} or payload.get("unknown") is True:
        return "전송 결과를 확인할 수 없습니다. 다시 보내지 마세요. 조정이 필요합니다."
    if event_type in {"audit_pending", "delivered_audit_pending"} or payload.get("audit_pending") is True:
        return "고객 전송 영수증은 확인됐습니다. 재전송하지 말고 감사 기록을 복구해 주세요."
    if event_type in {"duplicate", "already_attempted"} or payload.get("duplicate") is True:
        return "이미 처리된 전송입니다."
    if event_type in {"delivered", "sent_audited", "success"}:
        return "고객 전송과 감사 기록이 완료되었습니다."
    return "처리를 완료하지 못했습니다. 최신 상태를 확인해 주세요."


def _audited_delivery_result(row: Mapping[str, object]) -> Mapping[str, object]:
    """Expose a successful delivery only after its durable audit row exists."""
    payload = row.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    result = dict(row)
    result.update(
        {
            "event_type": "sent_audited",
            "status": "sent_audited",
            "delivery_id": payload.get("delivery_id"),
            "provider_receipt": payload.get("provider_receipt")
            or payload.get("message_id"),
            "text": adaptive_delivery_result_text({"event_type": "sent_audited"}),
        }
    )
    return result


class AdaptiveOperatorService:
    """Gateway-owned typed operator control plane for adaptive nutrition."""

    def __init__(
        self,
        coordinator: object,
        *,
        review_operator: object,
        profile_root: Path | str | None = None,
        session_path: Path | str | None = None,
        now_provider: Callable[[], datetime] | None = None,
        expiry_minutes: int = 60,
        schedule_confirm_handler: object | None = None,
        schedule_confirm_enabled: bool = False,
    ) -> None:
        self.coordinator = coordinator
        self.review_operator = self._review_tuple(review_operator)
        self.review_operator_version = self._review_version(review_operator)
        validator = validate_review_space_disjoint
        if not callable(validator):
            raise AdaptiveWorkflowError("adaptive review-space validator is unavailable")
        try:
            customer_routes = getattr(coordinator, "_routes", ())
            if isinstance(customer_routes, Mapping):
                customer_routes = tuple(customer_routes.keys())
            trainer_routes = getattr(coordinator, "_trainer_routes", ())
            if isinstance(trainer_routes, Mapping):
                trainer_routes = tuple(trainer_routes.keys())
            validator(
                self.review_operator,
                customer_routes=customer_routes,
                trainer_routes=trainer_routes,
                owner_scheduled_routes=(
                    getattr(coordinator, "owner", None),
                    getattr(coordinator, "_owner_scheduled_routes", ()),
                ),
                generic_reserved_routes=getattr(
                    coordinator,
                    "_generic_reserved_routes",
                    (),
                ),
            )
        except Exception as exc:
            raise AdaptiveWorkflowError(
                "adaptive review-space collision validation failed"
            ) from exc
        if (
            isinstance(expiry_minutes, bool)
            or not isinstance(expiry_minutes, int)
            or expiry_minutes < 1
            or expiry_minutes > 60
        ):
            raise AdaptiveWorkflowError("adaptive operator session expiry is invalid")
        self._expiry_minutes = expiry_minutes
        self._schedule_confirm_handler = schedule_confirm_handler
        self._schedule_confirm_enabled = schedule_confirm_enabled is True
        self._now_provider = now_provider or (lambda: datetime.now(_KST))
        root = Path(profile_root) if profile_root is not None else None
        self.profile_root = root
        if session_path is None and root is not None:
            session_path = root / "data" / "owner-actions" / "adaptive-operator-sessions.jsonl"
        self.session_path = Path(session_path) if session_path is not None else None
        self._session_lock = RLock()
        self._replay_cache: dict[str, Mapping[str, object]] = {}

    @staticmethod
    def _review_tuple(value: object) -> tuple[str, str, str]:
        candidate = getattr(value, "review_operator", value)
        raw = getattr(candidate, "key", candidate)
        if isinstance(raw, Mapping):
            raw = tuple(raw.get(field) for field in ("user_id", "chat_id", "topic_id"))
        if not isinstance(raw, (tuple, list)) or len(raw) != 3:
            raise AdaptiveWorkflowError("adaptive review operator is incomplete")
        key = tuple(str(item).strip() for item in raw)
        if not all(key) or key[2] != str(OPERATOR_REVIEW_TOPIC_ID):
            raise AdaptiveWorkflowError("adaptive review operator is invalid")
        return key

    @staticmethod
    def _review_version(value: object) -> int:
        candidate = getattr(value, "review_operator", value)
        version = getattr(candidate, "version", None)
        if isinstance(candidate, Mapping):
            version = candidate.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise AdaptiveWorkflowError("adaptive review operator version is invalid")
        return version

    @staticmethod
    def _address_key(value: object) -> tuple[str, str, str] | None:
        raw = getattr(value, "key", value)
        if isinstance(raw, Mapping):
            raw = tuple(raw.get(field) for field in ("user_id", "chat_id", "topic_id"))
        if not isinstance(raw, (tuple, list)) or len(raw) != 3:
            return None
        key = tuple(str(item).strip() for item in raw)
        return key if all(key) else None

    def accepts(self, address: object) -> bool:
        return self._address_key(address) == self.review_operator

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=_KST)
        return value.astimezone(_KST)

    def _owner(self, *, _under_authority_lock: bool = False) -> tuple[tuple[str, str, str], int]:
        """Refresh the live authority before minting or validating a session."""
        authority = getattr(self.coordinator, "authority", None)
        if authority is None:
            authority = self.coordinator
        refresh = getattr(authority, "refresh_live_registry", None)
        if callable(refresh):
            try:
                if refresh() is not True:
                    raise AdaptiveWorkflowError("adaptive owner registry is unavailable")
            except AdaptiveWorkflowError:
                raise
            except Exception as exc:
                raise AdaptiveWorkflowError("adaptive owner registry is unavailable") from exc
        owner = getattr(authority, "owner", None)
        if owner is None:
            owner = getattr(self.coordinator, "owner", None)
        key = self._address_key(owner)
        if key is None:
            raise AdaptiveWorkflowError("configured adaptive owner is unavailable")
        version = getattr(owner, "version", None)
        registry = getattr(authority, "registry", None)
        if version is None:
            version = getattr(registry, "version", None)
        if version is None:
            model_dump = getattr(registry, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump(mode="json")
                except TypeError:
                    dumped = model_dump()
                if isinstance(dumped, Mapping):
                    version = dumped.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            version = 1
        return key, version

    def _enabled_customer_keys(self) -> tuple[str, ...]:
        values = getattr(self.coordinator, "_by_key", {})
        if not isinstance(values, Mapping):
            return ()
        keys = []
        for key, resolved in values.items():
            customer = getattr(resolved, "customer", resolved)
            spec = getattr(customer, "spec", None)
            if getattr(spec, "enabled", False) is True:
                value = str(key).strip()
                if value:
                    keys.append(value)
        return tuple(sorted(set(keys)))

    def coaching_facts_for_current_card(
        self,
        customer_key: str,
        *,
        proposal: object | None = None,
    ) -> AdaptiveCoachingFacts:
        """Return a bounded adaptive projection without reading rendered card text."""
        if not isinstance(customer_key, str) or not customer_key.strip():
            raise AdaptiveWorkflowError("adaptive coaching customer is invalid")
        customer_key = customer_key.strip()
        if proposal is not None:
            supplied_digest = getattr(proposal, "digest", _COACHING_MISSING)
            if not _is_adaptive_digest(supplied_digest):
                raise AdaptiveWorkflowError("adaptive proposal digest is invalid")
            supplied_revision = getattr(proposal, "revision", _COACHING_MISSING)
            if (
                isinstance(supplied_revision, bool)
                or not isinstance(supplied_revision, int)
                or supplied_revision < 1
            ):
                raise AdaptiveWorkflowError("adaptive proposal revision is invalid")
        resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
        if not callable(resolver):
            raise AdaptiveWorkflowError("adaptive coaching service is unavailable")
        try:
            adaptive = resolver(customer_key)
            current = proposal
            if current is None:
                latest = getattr(adaptive, "_latest_production_proposal", None)
                if not callable(latest):
                    raise AdaptiveWorkflowError("adaptive proposal is unavailable")
                current = latest()
        except AdaptiveWorkflowError:
            raise
        except Exception as exc:
            raise AdaptiveWorkflowError("adaptive proposal is unavailable") from exc
        if current is None:
            raise AdaptiveWorkflowError("adaptive proposal is unavailable")
        proposal_customer_key = getattr(current, "customer_key", _COACHING_MISSING)
        if (
            proposal_customer_key is _COACHING_MISSING
            or not isinstance(proposal_customer_key, str)
            or proposal_customer_key.strip() != customer_key
        ):
            raise AdaptiveWorkflowError("adaptive proposal customer is invalid")
        digest_value = getattr(current, "digest", _COACHING_MISSING)
        if not _is_adaptive_digest(digest_value):
            raise AdaptiveWorkflowError("adaptive proposal digest is invalid")
        revision = getattr(current, "revision", _COACHING_MISSING)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise AdaptiveWorkflowError("adaptive proposal revision is invalid")
        snapshot = getattr(current, "snapshot", _COACHING_MISSING)
        if snapshot is _COACHING_MISSING or snapshot is None:
            raise AdaptiveWorkflowError("adaptive evaluation snapshot is invalid")
        evaluation_day_value = getattr(snapshot, "evaluation_day", _COACHING_MISSING)
        if not isinstance(evaluation_day_value, date) or isinstance(evaluation_day_value, datetime):
            raise AdaptiveWorkflowError("adaptive evaluation day is invalid")
        evaluation_day = evaluation_day_value.isoformat()
        live_customer = getattr(adaptive, "_live_customer", None)
        if not callable(live_customer):
            raise AdaptiveWorkflowError("adaptive customer is unavailable")
        try:
            live_result = live_customer()
        except AdaptiveWorkflowError:
            raise
        except Exception as exc:
            raise AdaptiveWorkflowError("adaptive customer is unavailable") from exc
        if not isinstance(live_result, (tuple, list)) or len(live_result) != 3:
            raise AdaptiveWorkflowError("adaptive customer is invalid")
        spec = live_result[2]
        plan = getattr(spec, "plan", None)
        if plan is None:
            goal_mode = "unknown"
            goal_min = None
            goal_max = None
        else:
            goal_mode_value = getattr(plan, "goal_mode", None)
            goal_mode = (
                "unknown"
                if goal_mode_value is None
                else _strict_coaching_text(
                    goal_mode_value,
                    field="goal mode",
                    opaque=True,
                )
            )
            if goal_mode not in _ADAPTIVE_GOAL_MODES:
                raise AdaptiveWorkflowError("adaptive goal mode is invalid")
            goal_min = _strict_coaching_text(
                getattr(plan, "weekly_rate_min", None),
                field="goal minimum rate",
                allow_none=True,
                scalar=True,
            )
            goal_max = _strict_coaching_text(
                getattr(plan, "weekly_rate_max", None),
                field="goal maximum rate",
                allow_none=True,
                scalar=True,
            )
        goal_range = (goal_min or "", goal_max or "")
        decision_value = getattr(current, "decision", _COACHING_MISSING)
        if decision_value is _COACHING_MISSING:
            raise AdaptiveWorkflowError("adaptive decision is invalid")
        decision_value = getattr(decision_value, "value", decision_value)
        decision = _strict_coaching_text(
            decision_value,
            field="decision",
            opaque=True,
        )
        raw_reasons = getattr(current, "reasons", _COACHING_MISSING)
        if (
            not isinstance(raw_reasons, (tuple, list))
            or len(raw_reasons) > 8
            or any(
                not isinstance(value, str) or not value.strip()
                for value in raw_reasons
            )
        ):
            raise AdaptiveWorkflowError("adaptive reasons are invalid")
        reasons = _coaching_reason_ids(raw_reasons)
        target = getattr(current, "target", _COACHING_MISSING)
        if target is _COACHING_MISSING:
            raise AdaptiveWorkflowError("adaptive target is invalid")
        target_macros = _coaching_macro_projection(target, field="target")
        cycle = getattr(current, "weekly_carb_cycle", _COACHING_MISSING)
        if cycle is _COACHING_MISSING:
            raise AdaptiveWorkflowError("adaptive carb cycle is invalid")
        carb_targets: list[tuple[str, tuple[tuple[str, int], ...]]] = []
        if cycle is not None:
            raw_targets = getattr(cycle, "targets", _COACHING_MISSING)
            if not isinstance(raw_targets, (tuple, list)) or len(raw_targets) > 7:
                raise AdaptiveWorkflowError("adaptive carb cycle is invalid")
            for item in raw_targets:
                category_value = getattr(item, "category", _COACHING_MISSING)
                category = _strict_coaching_text(
                    category_value,
                    field="carb category",
                    opaque=True,
                )
                if category not in {"high", "medium", "low"}:
                    raise AdaptiveWorkflowError("adaptive carb category is invalid")
                macro = getattr(item, "target", _COACHING_MISSING)
                if macro is _COACHING_MISSING or macro is None:
                    raise AdaptiveWorkflowError("adaptive carb target is invalid")
                macros = _coaching_macro_projection(macro, field="carb target")
                candidate = (category, macros)
                if candidate not in carb_targets:
                    carb_targets.append(candidate)
        card_state = getattr(self, "_proposal_card_state", None)
        if card_state is None:
            state = "proposed"
        elif not callable(card_state):
            raise AdaptiveWorkflowError("adaptive card state is invalid")
        else:
            try:
                state = card_state(adaptive, current)
            except AdaptiveWorkflowError:
                raise
            except Exception as exc:
                raise AdaptiveWorkflowError("adaptive card state is unavailable") from exc
        if not isinstance(state, str) or state not in _ADAPTIVE_CARD_STATES:
            raise AdaptiveWorkflowError("adaptive card state is invalid")
        approval_state = (
            "approved"
            if state in {"approved", "activated", "delivery_enabled"}
            else ("held" if state in {"held", "delivery_revoked"} else "pending")
        )
        delivery_state = (
            "enabled"
            if state == "delivery_enabled"
            else ("revoked" if state == "delivery_revoked" else "not_delivered")
        )
        safety_value = getattr(snapshot, "safety_held", _COACHING_MISSING)
        if type(safety_value) is not bool:
            raise AdaptiveWorkflowError("adaptive safety state is invalid")
        facts = AdaptiveCoachingFacts(
            evaluation_day=evaluation_day,
            goal_mode=goal_mode,
            goal_range=goal_range,
            current_mean_kg=_strict_coaching_text(
                getattr(snapshot, "current_mean_kg", _COACHING_MISSING),
                field="current mean",
                allow_none=True,
                scalar=True,
            ),
            prior_mean_kg=_strict_coaching_text(
                getattr(snapshot, "prior_mean_kg", _COACHING_MISSING),
                field="prior mean",
                allow_none=True,
                scalar=True,
            ),
            weekly_rate_percent=_strict_coaching_text(
                getattr(snapshot, "weekly_rate_percent", _COACHING_MISSING),
                field="weekly rate",
                allow_none=True,
                scalar=True,
            ),
            decision=decision,
            reason_category_ids=reasons,
            target_macros=target_macros,
            carb_category_targets=tuple(carb_targets),
            safety_held=safety_value,
            approval_state=approval_state,
            delivery_state=delivery_state,
            proposal_digest=digest_value,
            revision=revision,
            revision_binding_digest="",
        )
        try:
            binding = _coaching_digest(_adaptive_binding_projection(customer_key, facts))
        except AdaptiveWorkflowError:
            raise
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive coaching binding is invalid") from exc
        return replace(facts, revision_binding_digest=binding)

    def current_coaching_facts_match_binding(
        self,
        customer_key: str,
        expected_revision_binding_digest: object,
        *,
        proposal: object | None = None,
    ) -> bool:
        """Regenerate current facts and compare a caller-supplied binding read-only."""
        if not _is_adaptive_digest(expected_revision_binding_digest):
            return False
        try:
            resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
            if not callable(resolver):
                return False
            adaptive = resolver(customer_key)
            latest_resolver = getattr(adaptive, "_latest_production_proposal", None)
            if not callable(latest_resolver):
                return False
            latest = latest_resolver()
            if proposal is not None and (
                getattr(proposal, "digest", None) != getattr(latest, "digest", None)
                or getattr(proposal, "revision", None) != getattr(latest, "revision", None)
            ):
                return False
            facts = self.coaching_facts_for_current_card(
                customer_key,
                proposal=latest,
            )
        except Exception:
            return False
        return hmac.compare_digest(
            facts.revision_binding_digest,
            expected_revision_binding_digest,
        )
    def _pins(self, customer_key: str) -> dict[str, str]:
        owner, _version = self._owner()
        resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
        if callable(resolver):
            try:
                adaptive = resolver(customer_key)
                live_pins = getattr(adaptive, "_live_operator_pins", None)
                if (
                    getattr(adaptive, "_production_mode", False) is True
                    and callable(live_pins)
                ):
                    pins = dict(live_pins())
                    required = (
                        "config_digest",
                        "registry_digest",
                        "consent_digest",
                        "activation_digest",
                        "policy_digest",
                        "catalog_digest",
                        "meal_constraints_digest",
                    )
                    if any(not _is_adaptive_digest(pins.get(field)) for field in required):
                        raise AdaptiveWorkflowError(
                            "adaptive production capability pins are incomplete"
                        )
                    pins["epoch_digest"] = pins["config_digest"]
                    return {
                        field: str(pins[field])
                        for field in (
                            "config_digest",
                            "registry_digest",
                            "consent_digest",
                            "activation_digest",
                            "epoch_digest",
                            "policy_digest",
                            "catalog_digest",
                            "meal_constraints_digest",
                        )
                    }
            except AdaptiveWorkflowError:
                raise
            except Exception as exc:
                raise AdaptiveWorkflowError(
                    "adaptive production capability pins are unavailable"
                ) from exc
        registry = getattr(self.coordinator, "registry", None)
        registry_value: object = None
        model_dump = getattr(registry, "model_dump", None)
        if callable(model_dump):
            try:
                registry_value = model_dump(mode="json")
            except TypeError:
                registry_value = model_dump()
        if registry_value is None:
            if self._production_session_mode():
                raise AdaptiveWorkflowError("adaptive production capability pins are unavailable")
            registry_value = {"owner": owner, "customer_key": customer_key}
        elif self._production_session_mode():
            raise AdaptiveWorkflowError("adaptive production capability pins are unavailable")
        registry_digest = self._safe_digest(registry_value)
        epoch_digest = ""
        resolved = getattr(self.coordinator, "_by_key", {}).get(customer_key)
        customer = getattr(resolved, "customer", resolved)
        root = getattr(customer, "data_root", None)
        if isinstance(root, Path):
            path = root / "nutrition-plans" / "feature-epoch.json"
            try:
                epoch_digest = self._safe_digest(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                epoch_digest = ""
        return {
            "config_digest": epoch_digest,
            "registry_digest": registry_digest,
            "consent_digest": self._safe_digest(
                getattr(getattr(getattr(customer, "spec", None), "ai_processing_consent", None), "__dict__", {})
            ),
            "activation_digest": self._safe_digest(
                {"customer_key": customer_key, "enabled": getattr(getattr(customer, "spec", None), "enabled", False)}
            ),
            "epoch_digest": epoch_digest,
            "policy_digest": "",
            "catalog_digest": "",
            "meal_constraints_digest": "",
        }
    def _schedule_reference(self, customer_key: str) -> ScheduleConfirmationReference:
        handler = self._schedule_confirm_handler
        resolver = getattr(handler, "current_reference", None)
        if not self._schedule_confirm_enabled or not callable(resolver):
            raise AdaptiveWorkflowError("adaptive schedule confirmation is unavailable")
        try:
            value = resolver(customer_key)
        except Exception as exc:
            raise AdaptiveWorkflowError("adaptive schedule confirmation is unavailable") from exc
        if type(value) is ScheduleConfirmationReference:
            return value
        if isinstance(value, Mapping):
            try:
                return ScheduleConfirmationReference(
                    str(value.get("event_id", "")),
                    str(value.get("event_digest", "")),
                )
            except (TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError(
                    "adaptive schedule confirmation reference is invalid"
                ) from exc
        raise AdaptiveWorkflowError("adaptive schedule confirmation reference is invalid")

    @staticmethod
    def _safe_digest(value: object) -> str:
        if not callable(canonical_json):
            raise AdaptiveWorkflowError("adaptive canonical JSON encoder is unavailable")
        try:
            encoded = canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive canonical digest input is invalid") from exc
        if not isinstance(encoded, str):
            raise AdaptiveWorkflowError("adaptive canonical digest encoding is invalid")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _production_session_mode(self) -> bool:
        resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
        if callable(resolver):
            try:
                keys = self._enabled_customer_keys()
                if keys:
                    return bool(getattr(resolver(keys[0]), "_production_mode", False))
            except Exception:
                pass
        return bool(
            getattr(self.coordinator, "_production_mode", False)
            or getattr(self.coordinator, "canonical_event_source", None) is not None
            or getattr(self.coordinator, "authority", None) is not None
        )

    @contextmanager
    def _authority_session_lock(self) -> Iterator[None]:
        lock = (
            profile_authority_lock(self.profile_root)
            if self.profile_root is not None and callable(profile_authority_lock)
            else nullcontext()
        )
        with lock:
            with self._session_lock:
                yield

    def _read_rows(self) -> list[dict[str, object]]:
        with self._authority_session_lock():
            return self._read_rows_unlocked()

    def _validate_session_rows(
        self,
        rows: list[dict[str, object]],
        *,
        production: bool,
    ) -> list[dict[str, object]]:
        latest_by_session: dict[str, dict[str, object]] = {}
        nonce_sessions: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict) or not _is_adaptive_digest(row.get("row_digest")):
                raise AdaptiveWorkflowError("adaptive operator session ledger is invalid")
            expected = self._safe_digest(
                {key: value for key, value in row.items() if key != "row_digest"}
            )
            if not hmac.compare_digest(str(row["row_digest"]), expected):
                raise AdaptiveWorkflowError("adaptive operator session ledger digest mismatch")
            session_id = row.get("session_id")
            if not isinstance(session_id, str) or re.fullmatch(r"[a-f0-9]{24}", session_id) is None:
                raise AdaptiveWorkflowError("adaptive operator session id is invalid")
            token_hash = hashlib.sha256(session_id.encode("ascii")).hexdigest()
            if (
                row.get("token_hash") != token_hash
                or row.get("nonce_digest") != token_hash
            ):
                raise AdaptiveWorkflowError("adaptive operator session nonce is invalid")
            prior_session = nonce_sessions.setdefault(token_hash, session_id)
            if prior_session != session_id:
                raise AdaptiveWorkflowError("adaptive operator session nonce is reused")
            action = row.get("action")
            allowlist = row.get("action_allowlist")
            if (
                not isinstance(action, str)
                or action not in _ADAPTIVE_ACTIONS
                or not isinstance(allowlist, list)
                or allowlist != [action]
            ):
                raise AdaptiveWorkflowError("adaptive operator session action is invalid")
            customer_key = row.get("customer_key")
            if not isinstance(customer_key, str) or not customer_key.strip():
                raise AdaptiveWorkflowError("adaptive operator session customer is invalid")
            proposal_digest = row.get("proposal_digest")
            revision = row.get("revision")
            if action in {"select", "create"}:
                if proposal_digest is not None or revision is not None:
                    raise AdaptiveWorkflowError("adaptive operator pre-proposal pin is invalid")
            elif (
                not _is_adaptive_digest(proposal_digest)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise AdaptiveWorkflowError("adaptive operator proposal pin is invalid")
            schedule_event_id = row.get("schedule_event_id")
            schedule_event_digest = row.get("schedule_event_digest")
            if action == "schedule_confirm":
                if (
                    not isinstance(schedule_event_id, str)
                    or not schedule_event_id.strip()
                    or not _is_adaptive_digest(schedule_event_digest)
                ):
                    raise AdaptiveWorkflowError("adaptive schedule confirmation reference is invalid")
            elif schedule_event_id is not None or schedule_event_digest is not None:
                raise AdaptiveWorkflowError("adaptive schedule confirmation reference is unexpected")
            intent = row.get("schedule_confirm_intent")
            if action == "schedule_confirm":
                if not isinstance(intent, Mapping) or intent != {
                    "customer_key": customer_key,
                    "schedule_event_id": schedule_event_id,
                    "schedule_event_digest": schedule_event_digest,
                    "capability_id": session_id,
                }:
                    raise AdaptiveWorkflowError("adaptive schedule confirmation intent is invalid")
            elif intent is not None:
                raise AdaptiveWorkflowError("adaptive schedule confirmation intent is unexpected")
            if row.get("schema_version") != "1.0":
                raise AdaptiveWorkflowError("adaptive operator session version is invalid")
            digest_fields = (
                "epoch_digest",
                "source_digest",
                "registration_digest",
                "policy_digest",
                "catalog_digest",
                "meal_constraints_digest",
                "authority_digest",
                "config_digest",
                "registry_digest",
                "consent_digest",
                "activation_digest",
            )
            required_digest_fields = tuple(
                field
                for field in digest_fields
                if action not in {"select", "create"}
                or field not in {"source_digest", "registration_digest"}
            )
            if production and any(
                not _is_adaptive_digest(row.get(field)) for field in required_digest_fields
            ):
                raise AdaptiveWorkflowError("adaptive operator session pins are incomplete")
            if any(not isinstance(row.get(field), str) for field in digest_fields):
                raise AdaptiveWorkflowError("adaptive operator session pins are invalid")
            if action in {"select", "create"} and production:
                for field in ("source_digest", "registration_digest"):
                    if row.get(field) != "":
                        raise AdaptiveWorkflowError("adaptive operator pre-proposal pin is invalid")
            if not isinstance(row.get("review_operator"), list) or len(row["review_operator"]) != 3:
                raise AdaptiveWorkflowError("adaptive operator session provenance is invalid")
            review = tuple(str(value).strip() for value in row["review_operator"])
            if review != self.review_operator:
                raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
            review_version = row.get("review_config_version")
            if isinstance(review_version, bool) or not isinstance(review_version, int) or review_version < 1:
                raise AdaptiveWorkflowError("adaptive operator session provenance is invalid")
            if review_version != self.review_operator_version:
                raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
            for field in ("originating_message_id", "originating_chat_id", "originating_topic_id"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise AdaptiveWorkflowError("adaptive operator session provenance is invalid")
            if (
                row["originating_chat_id"] != self.review_operator[1]
                or row["originating_topic_id"] != self.review_operator[2]
            ):
                raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
            provenance = {
                "review_operator": self.review_operator,
                "review_config_version": self.review_operator_version,
                "originating_message_id": row["originating_message_id"],
                "originating_chat_id": row["originating_chat_id"],
                "originating_topic_id": row["originating_topic_id"],
            }
            if row.get("provenance_digest") != self._safe_digest(provenance):
                raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
            owner = row.get("canonical_owner_snapshot")
            if (
                not isinstance(owner, list)
                or len(owner) != 3
                or any(not isinstance(value, str) or not value.strip() for value in owner)
            ):
                raise AdaptiveWorkflowError("adaptive operator session authority is invalid")
            owner_version = row.get("canonical_owner_version")
            if isinstance(owner_version, bool) or not isinstance(owner_version, int) or owner_version < 1:
                raise AdaptiveWorkflowError("adaptive operator session authority is invalid")
            try:
                issued = datetime.fromisoformat(str(row.get("issued_kst", "")))
                expires = datetime.fromisoformat(str(row.get("expires_kst", "")))
            except (TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive operator session expiry is invalid") from exc
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=_KST)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_KST)
            if expires <= issued:
                raise AdaptiveWorkflowError("adaptive operator session expiry is invalid")
            state = row.get("state")
            if not isinstance(state, str) or state not in _ADAPTIVE_SESSION_STATES:
                raise AdaptiveWorkflowError("adaptive operator session state is invalid")
            prior = latest_by_session.get(session_id)
            predecessor = row.get("predecessor")
            if prior is None:
                if predecessor not in (None, ""):
                    raise AdaptiveWorkflowError("adaptive operator session predecessor is invalid")
            else:
                if predecessor != prior.get("row_digest"):
                    raise AdaptiveWorkflowError("adaptive operator session predecessor mismatch")
                prior_state = prior.get("state")
                if state not in _ADAPTIVE_SESSION_STATES.get(str(prior_state), frozenset()):
                    raise AdaptiveWorkflowError("adaptive operator session transition is invalid")
                for field in _ADAPTIVE_SESSION_IMMUTABLE_FIELDS:
                    if row.get(field) != prior.get(field):
                        if field in {
                            "originating_message_id",
                            "originating_chat_id",
                            "originating_topic_id",
                            "provenance_digest",
                        } and prior_state == "issued" and state == "issued":
                            continue
                        raise AdaptiveWorkflowError("adaptive operator session pin changed")
            if state in {"publish_pending", "publish_claimed", "published"} and not isinstance(
                row.get("card_payload"), Mapping
            ):
                raise AdaptiveWorkflowError("adaptive review card publication payload is invalid")
            if state == "published":
                if not isinstance(row.get("published_message_id"), str) or not row["published_message_id"].strip():
                    raise AdaptiveWorkflowError("adaptive review card publication receipt is invalid")
            if state == "publish_claimed":
                if not isinstance(row.get("claim_id"), str) or not row["claim_id"].strip():
                    raise AdaptiveWorkflowError("adaptive review card publication claim is invalid")
            latest_by_session[session_id] = row
        return rows

    def _read_rows_unlocked(self) -> list[dict[str, object]]:
        path = self.session_path
        if path is None or not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise AdaptiveWorkflowError("adaptive operator session ledger is unavailable")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AdaptiveWorkflowError("adaptive operator session ledger is unavailable") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise AdaptiveWorkflowError("adaptive operator session ledger is invalid")
        rows: list[dict[str, object]] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive operator session ledger is invalid") from exc
            if not isinstance(row, dict):
                raise AdaptiveWorkflowError("adaptive operator session ledger is invalid")
            rows.append(row)
        return self._validate_session_rows(rows, production=self._production_session_mode())

    def _append_row(self, body: Mapping[str, object]) -> dict[str, object]:
        with self._authority_session_lock():
            return self._append_row_unlocked(body)


    def _append_row_unlocked(self, body: Mapping[str, object]) -> dict[str, object]:
        row = dict(body)
        row["row_digest"] = self._safe_digest(row)
        try:
            existing = self._read_rows_unlocked()
            self._validate_session_rows(
                [*existing, row],
                production=self._production_session_mode(),
            )
        except AdaptiveWorkflowError:
            raise
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive operator session ledger is invalid") from exc
        if self.session_path is None:
            return row
        path = self.session_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive operator session could not be persisted") from exc
        return row

    @staticmethod
    def _session_token() -> str:
        return hashlib.sha256(os.urandom(32)).hexdigest()[:24]

    def _issue(
        self,
        *,
        action: str,
        customer_key: str,
        proposal_digest: str | None = None,
        revision: int | None = None,
        source_digest: str = "",
        registration_digest: str = "",
        originating_message_id: object = "",
        originating_chat_id: object = "",
        originating_topic_id: object = 59,
        predecessor: str | None = None,
    ) -> str:
        if action not in _ADAPTIVE_ACTIONS:
            raise AdaptiveWorkflowError("adaptive operator action is invalid")
        if action in {"select", "create"}:
            if proposal_digest is not None or revision is not None:
                raise AdaptiveWorkflowError("adaptive pre-proposal session is invalid")
        elif not isinstance(proposal_digest, str) or not proposal_digest.strip() or (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise AdaptiveWorkflowError("adaptive operator proposal pin is required")
        customer_key = str(customer_key or "").strip()
        if not customer_key or customer_key not in self._enabled_customer_keys():
            raise AdaptiveWorkflowError("adaptive customer selection is unavailable")
        reference = (
            self._schedule_reference(customer_key)
            if action == "schedule_confirm"
            else None
        )
        origin_message = str(originating_message_id or "").strip()
        if not origin_message:
            raise AdaptiveWorkflowError("adaptive operator origin message is required")
        token = self._session_token()
        now = self._now()
        expires = now + timedelta(minutes=self._expiry_minutes)
        owner, owner_version = self._owner()
        pins = self._pins(customer_key)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        origin_chat = str(originating_chat_id or self.review_operator[1]).strip()
        origin_topic = str(originating_topic_id or self.review_operator[2]).strip()
        provenance = {
            "review_operator": self.review_operator,
            "review_config_version": self.review_operator_version,
            "originating_message_id": origin_message,
            "originating_chat_id": origin_chat,
            "originating_topic_id": origin_topic,
        }
        body: dict[str, object] = {
            "schema_version": "1.0",
            "session_id": token,
            "token_hash": token_hash,
            "nonce_digest": token_hash,
            "action": action,
            "action_allowlist": [action],
            "customer_key": customer_key,
            "proposal_digest": proposal_digest,
            "revision": revision,
            "schedule_event_id": reference.event_id if reference is not None else None,
            "schedule_event_digest": reference.event_digest if reference is not None else None,
            "schedule_confirm_intent": (
                {
                    "customer_key": customer_key,
                    "schedule_event_id": reference.event_id,
                    "schedule_event_digest": reference.event_digest,
                    "capability_id": token,
                }
                if reference is not None
                else None
            ),
            "epoch_digest": pins["config_digest"],
            "source_digest": str(source_digest or ""),
            "registration_digest": str(registration_digest or ""),
            "authority_digest": self._safe_digest({"owner": owner, **pins}),
            "config_digest": pins["config_digest"],
            "registry_digest": pins["registry_digest"],
            "consent_digest": pins["consent_digest"],
            "activation_digest": pins["activation_digest"],
            "policy_digest": pins["policy_digest"],
            "catalog_digest": pins["catalog_digest"],
            "meal_constraints_digest": pins["meal_constraints_digest"],
            "originating_message_id": origin_message,
            "originating_chat_id": origin_chat,
            "originating_topic_id": origin_topic,
            "provenance_digest": self._safe_digest(provenance),
            "review_operator": list(self.review_operator),
            "review_config_version": self.review_operator_version,
            "canonical_owner_snapshot": list(owner),
            "canonical_owner_version": owner_version,
            "issued_kst": now.isoformat(),
            "expires_kst": expires.isoformat(),
            "state": "issued",
            "predecessor": predecessor,
        }
        with self._authority_session_lock():
            self._append_row_unlocked(body)
        return f"an1:{token}:{action}"

    def issue_session(self, **kwargs: object) -> str:
        return self._issue(**kwargs)


    def terminal_delivery_card(
        self,
        callback_data: object,
        result: object,
    ) -> Mapping[str, object]:
        payload = result if isinstance(result, Mapping) else {}
        terminal_state = str(
            payload.get("event_type", payload.get("status", "")) or ""
        )
        if terminal_state not in {
            "sent_audited",
            "success",
            "duplicate",
            "already_attempted",
            "delivery_unknown",
            "unknown",
            "audit_pending",
            "delivered_audit_pending",
        }:
            raise AdaptiveWorkflowError("adaptive delivery result is not terminal")
        text = adaptive_delivery_result_text(payload)
        buttons: list[dict[str, str]] = []
        if terminal_state in {"audit_pending", "delivered_audit_pending"}:
            parts = str(callback_data or "").split(":")
            session = (
                self._find_session(parts[1], "send")
                if len(parts) == 3 and parts[0] == "an1"
                else None
            )
            if session is not None:
                buttons.append(
                    {
                        "label": "감사 기록 복구",
                        "callback_data": self._issue(
                            action="reconcile",
                            customer_key=str(session["customer_key"]),
                            proposal_digest=str(session["proposal_digest"]),
                            revision=int(session["revision"]),
                            source_digest=str(session.get("source_digest", "") or ""),
                            registration_digest=str(
                                session.get("registration_digest", "") or ""
                            ),
                            originating_message_id=session.get(
                                "originating_message_id", ""
                            ),
                            originating_chat_id=session.get(
                                "originating_chat_id", ""
                            ),
                            originating_topic_id=session.get(
                                "originating_topic_id", 59
                            ),
                        ),
                    }
                )
        if terminal_state in {"sent_audited", "success"}:
            heading = "✅ 고객 전송 및 감사 기록 완료"
        elif terminal_state in {"audit_pending", "delivered_audit_pending"}:
            heading = "⚠️ 고객 전송 완료 · 감사 기록 복구 필요"
        elif terminal_state in {"delivery_unknown", "unknown"}:
            heading = "⚠️ 전송 결과 확인 불가 · 재전송 금지"
        else:
            heading = "ℹ️ 이미 처리된 전송"
        return {
            "status": "view",
            "terminal_state": terminal_state,
            "text": f"{heading}\n\n{text}",
            "buttons": buttons,
        }
    @staticmethod
    def _session_id_value(value: object) -> str:
        raw = str(value or "").strip()
        if raw.startswith("an1:"):
            parts = raw.split(":")
            if len(parts) == 3:
                raw = parts[1]
        if not re.fullmatch(r"[a-f0-9]{24}", raw):
            raise AdaptiveWorkflowError("adaptive operator session id is invalid")
        return raw

    def _latest_session(self, session_id: object) -> dict[str, object]:
        token = self._session_id_value(session_id)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        latest: dict[str, object] | None = None
        for row in self._read_rows():
            if row.get("session_id") == token and row.get("token_hash") == token_hash:
                latest = row
        if latest is None:
            raise AdaptiveWorkflowError("adaptive operator session is unavailable")
        return latest

    def _latest_session_unlocked(self, session_id: object) -> dict[str, object]:
        token = self._session_id_value(session_id)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        latest: dict[str, object] | None = None
        for row in self._read_rows_unlocked():
            if row.get("session_id") == token and row.get("token_hash") == token_hash:
                latest = row
        if latest is None:
            raise AdaptiveWorkflowError("adaptive operator session is unavailable")
        return latest

    @staticmethod
    def _publish_receipt(value: object) -> str:
        if isinstance(value, Mapping):
            if value.get("ok") is False or value.get("success") is False:
                return ""
            value = value.get("message_id", value.get("id"))
        else:
            if getattr(value, "ok", True) is False or getattr(value, "success", True) is False:
                return ""
            value = getattr(value, "message_id", value)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return ""
        result = str(value).strip()
        return result if result and len(result) <= 128 else ""
    def _validated_card_payload(self, value: object) -> dict[str, object]:
        """Normalize one persisted review card before it can be published."""
        if not isinstance(value, Mapping):
            raise AdaptiveWorkflowError("adaptive review card payload is invalid")
        text = value.get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or telegram_utf16_length(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16
        ):
            raise AdaptiveWorkflowError("adaptive review card text is invalid")
        if value.get("status") == "card":
            envelope = value.get("envelope")
            if not isinstance(envelope, Mapping):
                raise AdaptiveWorkflowError("adaptive review card envelope is invalid")
            required = ("customer_label", "kst_day")
            if any(
                not isinstance(envelope.get(field), str)
                or not str(envelope[field]).strip()
                for field in required
            ):
                raise AdaptiveWorkflowError(
                    "adaptive review card envelope is invalid"
                )
            if any(str(envelope[field]) not in text for field in required):
                raise AdaptiveWorkflowError(
                    "adaptive review card envelope is invalid"
                )
            customer_key = value.get("customer_key")
            proposal_digest = value.get("proposal_digest")
            revision = value.get("revision")
            lifecycle_state = value.get("lifecycle_state")
            customer_preview = value.get("customer_preview")
            preview_digest = value.get("customer_preview_digest")
            if (
                not isinstance(customer_key, str)
                or not customer_key
                or not isinstance(proposal_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", proposal_digest) is None
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(lifecycle_state, str)
                or not lifecycle_state
                or not isinstance(customer_preview, str)
                or not customer_preview
                or not isinstance(preview_digest, str)
                or hashlib.sha256(customer_preview.encode("utf-8")).hexdigest()
                != preview_digest
            ):
                raise AdaptiveWorkflowError(
                    "adaptive review card hidden pins are invalid"
                )
            if any(
                token in text.casefold()
                for token in (
                    "revision:",
                    "digest:",
                    "epoch:",
                    "recovery:",
                    "reservation:",
                )
            ):
                raise AdaptiveWorkflowError(
                    "adaptive review card exposes lifecycle diagnostics"
                )
            resolver = getattr(
                self.coordinator,
                "adaptive_nutrition_coordinator",
                None,
            )
            if not callable(resolver):
                raise AdaptiveWorkflowError(
                    "adaptive review card authority is unavailable"
                )
            adaptive = resolver(customer_key)
            proposal = adaptive._latest_production_proposal()
            expected_preview = adaptive.preview_registered_daily_projection(
                proposal
            )
            expected_state = self._proposal_card_state(adaptive, proposal)
            if (
                proposal.digest != proposal_digest
                or proposal.revision != revision
                or expected_state != lifecycle_state
                or expected_preview != customer_preview
                or f"\n고객 전달 미리보기\n{customer_preview}\n\n안전·과장 점검:"
                not in text
            ):
                raise AdaptiveWorkflowError(
                    "adaptive review card authority is stale"
                )
        raw_buttons = value.get("buttons", [])
        if not isinstance(raw_buttons, list):
            raise AdaptiveWorkflowError("adaptive review card buttons are invalid")
        buttons: list[object] = []
        for item in raw_buttons:
            if isinstance(item, Mapping):
                callback_data = item.get("callback_data")
                if not isinstance(callback_data, str) or _ADAPTIVE_CALLBACK_RE.fullmatch(
                    callback_data
                ) is None:
                    raise AdaptiveWorkflowError("adaptive review card callback is invalid")
                label = str(item.get("label", item.get("customer_key", "선택")) or "").strip()
                if not label:
                    raise AdaptiveWorkflowError("adaptive review card button label is invalid")
                normalized = dict(item)
                normalized["callback_data"] = callback_data
                normalized["label"] = label[:64]
                buttons.append(normalized)
            elif isinstance(item, str) and _ADAPTIVE_CALLBACK_RE.fullmatch(item) is not None:
                buttons.append(item)
            else:
                raise AdaptiveWorkflowError("adaptive review card button is invalid")
        payload = dict(value)
        payload["text"] = text
        payload["buttons"] = buttons
        return payload

    def validated_inline_buttons(
        self,
        card_payload: object,
        *,
        require_sessions: bool = False,
    ) -> tuple[tuple[str, str], ...]:
        """Return validated ``(label, callback_data)`` pairs for a persisted card."""
        payload = self._validated_card_payload(card_payload)
        result: list[tuple[str, str]] = []
        for item in payload["buttons"]:
            if isinstance(item, Mapping):
                callback_data = str(item["callback_data"])
                label = str(item["label"])
            else:
                callback_data = str(item)
                label = callback_data.rsplit(":", 1)[-1]
            if require_sessions:
                match = _ADAPTIVE_CALLBACK_RE.fullmatch(callback_data)
                if match is None:
                    raise AdaptiveWorkflowError("adaptive review card callback session is unavailable")
                session = self._find_session(match.group(1), match.group(2))
                if session is None:
                    raise AdaptiveWorkflowError("adaptive review card callback session is unavailable")
                self._capability(session, action=match.group(2))
            result.append((label[:64], callback_data))
        return tuple(result)

    @contextmanager
    def _publication_ledger_lock(self) -> Iterator[None]:
        """Serialize publication claims across processes sharing one profile."""
        if self.profile_root is not None:
            yield
            return
        path = self.session_path
        if path is None:
            yield
            return
        lock_path = path.with_name(f"{path.name}.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path.parent.chmod(0o700)
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AdaptiveWorkflowError("adaptive publication claim lock is unavailable") from exc

    @staticmethod
    def _publication_claim_id() -> str:
        return hashlib.sha256(os.urandom(32)).hexdigest()[:24]

    def _publication_lease_active(self, row: Mapping[str, object]) -> bool:
        if row.get("state") != "publish_claimed":
            return False
        try:
            expires = datetime.fromisoformat(str(row.get("lease_expires_kst", "")))
        except (TypeError, ValueError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_KST)
        return self._now() < expires.astimezone(_KST)

    def _claim_pending_card(
        self,
        session_id: object,
        expected: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Claim one pending publication durably before invoking Telegram."""
        del expected
        token = self._session_id_value(session_id)
        with self._authority_session_lock(), self._publication_ledger_lock():
            latest: Mapping[str, object] | None = None
            for row in self._read_rows_unlocked():
                if row.get("session_id") == token:
                    latest = row
            if latest is None:
                return None
            state = latest.get("state")
            if state in {"published", "publish_claimed"}:
                return None
            if state != "publish_pending":
                return None
            payload = self._validated_card_payload(latest.get("card_payload"))
            claim_id = self._publication_claim_id()
            now = self._now()
            expires = now + timedelta(minutes=1)
            body = dict(latest)
            body.pop("row_digest", None)
            body.update(
                {
                    "state": "publish_claimed",
                    "claim_id": claim_id,
                    "lease_expires_kst": expires.isoformat(),
                    "predecessor": latest.get("row_digest", ""),
                    "card_payload": payload,
                }
            )
            return self._append_row_unlocked(body)

    def _release_publication_claim(
        self,
        session_id: object,
        claim_id: object,
    ) -> Mapping[str, object] | None:
        token = self._session_id_value(session_id)
        claim = str(claim_id or "").strip()
        if not claim:
            return None
        with self._authority_session_lock(), self._publication_ledger_lock():
            latest: Mapping[str, object] | None = None
            for row in self._read_rows_unlocked():
                if row.get("session_id") == token:
                    latest = row
            if (
                latest is None
                or latest.get("state") != "publish_claimed"
                or str(latest.get("claim_id", "")) != claim
            ):
                return latest
            body = dict(latest)
            body.pop("row_digest", None)
            body["state"] = "publish_pending"
            body["predecessor"] = latest.get("row_digest", "")
            body.pop("claim_id", None)
            body.pop("lease_expires_kst", None)
            return self._append_row_unlocked(body)

    def mark_publish_pending(
        self,
        session_id: object,
        *,
        card_payload: object,
        origin_message_id: object = "",
    ) -> Mapping[str, object]:
        """Durably reserve an already-rendered review card for publication."""
        if not isinstance(card_payload, Mapping) or not card_payload:
            raise AdaptiveWorkflowError("adaptive review card payload is invalid")
        with self._authority_session_lock(), self._publication_ledger_lock():
            session = self._latest_session_unlocked(session_id)
            state = str(session.get("state", ""))
            if state == "published":
                return session
            try:
                if not callable(canonical_json):
                    raise AdaptiveWorkflowError("adaptive canonical JSON encoder is unavailable")
                payload = json.loads(canonical_json(dict(card_payload)))
            except (AdaptiveWorkflowError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive review card payload is invalid") from exc
            if not isinstance(payload, Mapping):
                raise AdaptiveWorkflowError("adaptive review card payload is invalid")
            payload = self._validated_card_payload(payload)
            persisted_origin = str(session.get("originating_message_id", "") or "")
            supplied_origin = str(origin_message_id or "").strip()
            if supplied_origin and supplied_origin != persisted_origin:
                raise AdaptiveWorkflowError("adaptive review card provenance conflict")
            body = {
                "schema_version": "1.0",
                "session_id": self._session_id_value(session_id),
                "token_hash": session.get("token_hash"),
                "nonce_digest": session.get("nonce_digest"),
                "state": "publish_pending",
                "action_allowlist": [session.get("action")],
                "action": session.get("action"),
                "customer_key": session.get("customer_key"),
                "proposal_digest": session.get("proposal_digest"),
                "revision": session.get("revision"),
                "schedule_event_id": session.get("schedule_event_id"),
                "schedule_event_digest": session.get("schedule_event_digest"),
                "review_operator": session.get("review_operator"),
                "review_config_version": session.get("review_config_version"),
                "canonical_owner_snapshot": session.get("canonical_owner_snapshot"),
                "canonical_owner_version": session.get("canonical_owner_version"),
                "authority_digest": session.get("authority_digest"),
                "config_digest": session.get("config_digest"),
                "registry_digest": session.get("registry_digest"),
                "consent_digest": session.get("consent_digest"),
                "activation_digest": session.get("activation_digest"),
                "policy_digest": session.get("policy_digest"),
                "catalog_digest": session.get("catalog_digest"),
                "meal_constraints_digest": session.get("meal_constraints_digest"),
                "epoch_digest": session.get("epoch_digest", session.get("config_digest", "")),
                "source_digest": session.get("source_digest"),
                "registration_digest": session.get("registration_digest"),
                "originating_message_id": persisted_origin,
                "originating_chat_id": session.get("originating_chat_id"),
                "originating_topic_id": session.get("originating_topic_id"),
                "provenance_digest": session.get("provenance_digest"),
                "issued_kst": session.get("issued_kst"),
                "expires_kst": session.get("expires_kst"),
                "card_payload": payload,
                "predecessor": session.get("row_digest", ""),
            }
            if state == "publish_claimed":
                if session.get("card_payload") != payload:
                    raise AdaptiveWorkflowError("adaptive review card publication conflict")
                return session
            prior = next(
                (
                    row
                    for row in reversed(self._read_rows_unlocked())
                    if row.get("session_id") == body["session_id"]
                    and row.get("state") == "publish_pending"
                ),
                None,
            )
            if prior is not None:
                if prior.get("card_payload") != body["card_payload"]:
                    raise AdaptiveWorkflowError("adaptive review card publication conflict")
                return prior
            return self._append_row_unlocked(body)
    def _rebind_card_button_sessions(
        self,
        card_payload: object,
        published_message_id: str,
    ) -> None:
        """Bind recovered button sessions to the newly published card message."""
        payload = self._validated_card_payload(card_payload)
        callback_tokens: list[tuple[str, str]] = []
        for item in payload["buttons"]:
            callback_data = (
                str(item.get("callback_data"))
                if isinstance(item, Mapping)
                else str(item)
            )
            match = _ADAPTIVE_CALLBACK_RE.fullmatch(callback_data)
            if match is not None:
                callback_tokens.append((match.group(1), match.group(2)))
        if not callback_tokens:
            return
        rows = self._read_rows_unlocked()
        latest_by_session: dict[str, Mapping[str, object]] = {}
        for row in rows:
            session_id = row.get("session_id")
            if isinstance(session_id, str) and session_id:
                latest_by_session[session_id] = row
        for token, action in callback_tokens:
            session = latest_by_session.get(token)
            if (
                session is None
                or session.get("action") != action
                or session.get("state") != "issued"
                or str(session.get("originating_message_id", "")) == published_message_id
            ):
                continue
            body = dict(session)
            body.pop("row_digest", None)
            origin_chat = str(session.get("originating_chat_id", "") or "")
            origin_topic = str(session.get("originating_topic_id", "") or "")
            body["originating_message_id"] = published_message_id
            body["provenance_digest"] = self._safe_digest(
                {
                    "review_operator": self.review_operator,
                    "review_config_version": self.review_operator_version,
                    "originating_message_id": published_message_id,
                    "originating_chat_id": origin_chat,
                    "originating_topic_id": origin_topic,
                }
            )
            body["predecessor"] = session.get("row_digest", "")
            self._append_row_unlocked(body)

    def mark_published(
        self,
        session_id: object,
        *,
        published_message_id: object,
        claim_id: object = "",
    ) -> Mapping[str, object]:
        """Record one successful Telegram publication receipt."""
        receipt = self._publish_receipt(published_message_id)
        if not receipt:
            raise AdaptiveWorkflowError("adaptive review card publication receipt is invalid")
        with self._authority_session_lock(), self._publication_ledger_lock():
            session = self._latest_session_unlocked(session_id)
            if session.get("state") == "published":
                if str(session.get("published_message_id", "")) != receipt:
                    raise AdaptiveWorkflowError("adaptive review card publication receipt conflict")
                return session
            state = str(session.get("state", ""))
            if state not in {"publish_pending", "publish_claimed"}:
                raise AdaptiveWorkflowError("adaptive review card publication is not pending")
            supplied_claim = str(claim_id or "").strip()
            if state == "publish_claimed" and (
                not supplied_claim or supplied_claim != str(session.get("claim_id", ""))
            ):
                raise AdaptiveWorkflowError("adaptive review card publication claim is stale")
            body = dict(session)
            body.pop("row_digest", None)
            body["state"] = "published"
            body["published_message_id"] = receipt
            body["predecessor"] = session.get("row_digest", "")
            published = self._append_row_unlocked(body)
            try:
                self._rebind_card_button_sessions(session.get("card_payload"), receipt)
            except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                # The publication itself is durable; do not replay the provider call.
                pass
            return published


    def _pending_card_rows(self) -> tuple[tuple[str, Mapping[str, object]], ...]:
        with self._authority_session_lock():
            latest_by_session: dict[str, dict[str, object]] = {}
            for row in self._read_rows_unlocked():
                session_id = row.get("session_id")
                if isinstance(session_id, str) and session_id:
                    latest_by_session[session_id] = row
        return tuple(
            (session_id, row)
            for session_id, row in latest_by_session.items()
            if row.get("state") == "publish_pending"
        )

    def recover_pending_cards(self, publisher: Callable[[Mapping[str, object]], object]) -> Mapping[str, object]:
        """Publish pending cards once; lifecycle rows are never replayed."""
        if not callable(publisher):
            raise AdaptiveWorkflowError("adaptive review card publisher is unavailable")
        recovered: list[Mapping[str, object]] = []
        pending: list[Mapping[str, object]] = []
        for session_id, row in self._pending_card_rows():
            action = row.get("action")
            if not isinstance(action, str):
                pending.append(row)
                continue
            claimed: Mapping[str, object] | None = None
            try:
                self._capability(row, action=action)
                claimed = self._claim_pending_card(session_id, row)
                if claimed is None:
                    pending.append(row)
                    continue
                card = self._validated_card_payload(claimed.get("card_payload"))
                self.validated_inline_buttons(card, require_sessions=True)
                receipt = publisher(dict(card))
                if inspect.isawaitable(receipt):
                    pending.append(claimed)
                    continue
                message_id = self._publish_receipt(receipt)
                if not message_id:
                    pending.append(claimed)
                    continue
                recovered.append(
                    self.mark_published(
                        session_id,
                        published_message_id=message_id,
                        claim_id=claimed.get("claim_id"),
                    )
                )
            except Exception:
                pending.append(claimed or row)
        return {"recovered": tuple(recovered), "pending": tuple(pending)}

    async def recover_pending_cards_async(
        self,
        publisher: Callable[[Mapping[str, object]], object],
    ) -> Mapping[str, object]:
        """Async restart recovery for Telegram publishers."""
        if not callable(publisher):
            raise AdaptiveWorkflowError("adaptive review card publisher is unavailable")
        recovered: list[Mapping[str, object]] = []
        pending: list[Mapping[str, object]] = []
        for session_id, row in self._pending_card_rows():
            action = row.get("action")
            if not isinstance(action, str):
                pending.append(row)
                continue
            claimed: Mapping[str, object] | None = None
            try:
                self._capability(row, action=action)
                claimed = self._claim_pending_card(session_id, row)
                if claimed is None:
                    pending.append(row)
                    continue
                card = self._validated_card_payload(claimed.get("card_payload"))
                self.validated_inline_buttons(card, require_sessions=True)
                receipt = publisher(dict(card))
                if inspect.isawaitable(receipt):
                    receipt = await receipt
                message_id = self._publish_receipt(receipt)
                if not message_id:
                    pending.append(claimed)
                    continue
                recovered.append(
                    self.mark_published(
                        session_id,
                        published_message_id=message_id,
                        claim_id=claimed.get("claim_id"),
                    )
                )
            except Exception:
                pending.append(claimed or row)
        return {"recovered": tuple(recovered), "pending": tuple(pending)}

    def open_menu(
        self,
        address: object,
        *,
        message_id: object = "",
        chat_id: object = "",
        topic_id: object = 59,
    ) -> Mapping[str, object]:
        if not self.accepts(address):
            return {"status": "rejected", "text": "이 공간에서는 적응형 영양 검토를 사용할 수 없습니다."}
        keys = self._enabled_customer_keys()
        if not keys:
            return {"status": "rejected", "text": "검토 가능한 고객이 없습니다."}
        buttons = []
        for key in keys:
            resolved = getattr(self.coordinator, "_by_key", {}).get(key)
            customer = getattr(resolved, "customer", resolved)
            label = str(getattr(getattr(customer, "spec", None), "display_name", key) or key).strip()[:80]
            callback = self._issue(
                action="select",
                customer_key=key,
                originating_message_id=message_id,
                originating_chat_id=chat_id,
                originating_topic_id=topic_id,
            )
            buttons.append({"customer_key": key, "label": label, "callback_data": callback})
        return {"status": "menu", "text": "적응형 영양 검토\n고객을 선택하세요.", "buttons": buttons}
    def _card_buttons(
        self,
        *,
        customer_key: str,
        proposal: object,
        message_id: object,
        chat_id: object,
        topic_id: object,
        state: str,
        source_digest: object = "",
        registration_digest: object = "",
    ) -> list[dict[str, str]]:
        state_actions = {
            "proposed": (
                ("승인하고 보내기", "approve_and_send"),
                ("내용 수정", "edit_note"),
                ("보류", "hold"),
                ("고급 · 내용 보기", "view"),
            ),
            "edited": (
                ("승인하고 보내기", "approve_and_send"),
                ("내용 수정", "edit_note"),
                ("보류", "hold"),
                ("고급 · 내용 보기", "view"),
            ),
            "released": (
                ("승인하고 보내기", "approve_and_send"),
                ("내용 수정", "edit_note"),
                ("보류", "hold"),
                ("고급 · 내용 보기", "view"),
            ),
            "held": (
                ("검토 다시 시작", "release"),
                ("고급 · 내용 보기", "view"),
            ),
            "approved": (
                ("승인하고 보내기", "approve_and_send"),
                ("고급 · 활성화하기", "activate"),
                ("고급 · 내용 보기", "view"),
            ),
            "activated": (
                ("승인하고 보내기", "approve_and_send"),
                ("고급 · 고객 전송 허용", "delivery_enable"),
                ("고급 · 내용 보기", "view"),
            ),
            "delivery_enabled": (
                ("승인하고 보내기", "approve_and_send"),
                ("고급 · 고객에게 전송", "send"),
                ("고급 · 전송 권한 회수", "delivery_revoke"),
                ("고급 · 전송 기록 확인", "reconcile"),
                ("고급 · 내용 보기", "view"),
            ),
            "delivery_revoked": (
                ("승인하고 보내기", "approve_and_send"),
                ("고급 · 고객 전송 다시 허용", "delivery_enable"),
                ("고급 · 내용 보기", "view"),
            ),
        }
        actions = state_actions.get(
            str(state or ""),
            (("고급 · 내용 보기", "view"), ("이전", "back")),
        )
        if not self._schedule_confirm_enabled or self._schedule_confirm_handler is None:
            actions = tuple(
                item for item in actions if item[1] != "schedule_confirm"
            )
        buttons: list[dict[str, str]] = []
        for label, action in actions:
            buttons.append(
                {
                    "label": label,
                    "callback_data": self._issue(
                        action=action,
                        customer_key=customer_key,
                        proposal_digest=str(getattr(proposal, "digest", "")),
                        revision=getattr(proposal, "revision", None),
                        source_digest=str(source_digest or ""),
                        registration_digest=str(registration_digest or ""),
                        originating_message_id=message_id,
                        originating_chat_id=chat_id,
                        originating_topic_id=topic_id,
                    ),
                }
            )
        return buttons

    def _customer_display_label(self, customer_key: str) -> str:
        resolved = getattr(self.coordinator, "_by_key", {}).get(customer_key)
        customer = getattr(resolved, "customer", resolved)
        if customer is None:
            customer = getattr(self.coordinator, "customer_runtime", None)
        if customer is None:
            live_customer = getattr(self.coordinator, "_live_customer", None)
            if callable(live_customer):
                try:
                    resolved_live = live_customer()
                    candidate = (
                        resolved_live[2]
                        if isinstance(resolved_live, tuple) and len(resolved_live) > 2
                        else None
                    )
                    customer = SimpleNamespace(spec=candidate)
                except Exception:
                    customer = None
        spec = getattr(customer, "spec", None)
        if spec is None:
            spec = getattr(getattr(customer, "customer", None), "spec", None)
        value = getattr(spec, "display_name", None)
        if not value:
            live_customer = getattr(self.coordinator, "_live_customer", None)
            if callable(live_customer):
                try:
                    resolved_live = live_customer()
                    if isinstance(resolved_live, tuple) and len(resolved_live) > 2:
                        value = getattr(resolved_live[2], "display_name", None)
                except Exception:
                    value = None
        if not value:
            registry = getattr(self.coordinator, "registry", None)
            model_dump = getattr(registry, "model_dump", None)
            if callable(model_dump):
                try:
                    raw_registry = model_dump(mode="json")
                except TypeError:
                    raw_registry = model_dump()
                if isinstance(raw_registry, Mapping):
                    customers = raw_registry.get("customers", ())
                    if isinstance(customers, (list, tuple)):
                        for entry in customers:
                            if (
                                isinstance(entry, Mapping)
                                and entry.get("customer_key") == customer_key
                            ):
                                value = entry.get("display_name")
                                break
        label = " ".join(str(value or "").split())
        return label[:80] if label else str(customer_key).strip()[:80]

    def _proposal_kst_day(self, proposal: object) -> str:
        value = getattr(getattr(proposal, "snapshot", None), "evaluation_day", None)
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(_KST)
            value = value.date()
        if isinstance(value, date):
            return value.isoformat()
        return self._now().date().isoformat()

    @staticmethod
    def _action_card_state(action: object) -> str | None:
        return {
            "create": "proposed",
            "edit_note": "edited",
            "hold": "held",
            "release": "released",
            "approve": "approved",
            "activate": "activated",
            "delivery_enable": "delivery_enabled",
            "delivery_revoke": "delivery_revoked",
        }.get(str(action or ""))

    def _proposal_card_state(self, adaptive: object, proposal: object) -> str:
        digest_value = str(getattr(proposal, "digest", "") or "")
        store = getattr(adaptive, "store", None)
        reader = getattr(store, "read", None)
        if callable(reader) and digest_value:
            try:
                rows = reader()
            except (OSError, TypeError, ValueError):
                rows = ()
            if not isinstance(rows, (tuple, list)):
                rows = ()
            state = "proposed"
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                payload = row.get("payload")
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("proposal_digest") != digest_value
                ):
                    continue
                event_type = row.get("event_type")
                if event_type == "plan_approved":
                    state = "approved"
                elif event_type == "adaptive_plan_activated":
                    state = "activated"
                elif event_type == "plan_edited":
                    revision_action = payload.get("revision_action")
                    state = (
                        str(revision_action)
                        if revision_action in {"held", "released"}
                        else "held"
                        if revision_action == "hold"
                        else "released"
                        if revision_action == "release"
                        else "edited"
                    )
                elif event_type == "plan_proposed":
                    state = "proposed"
            if state == "activated":
                return (
                    "delivery_enabled"
                    if bool(getattr(adaptive, "delivery_enabled", False))
                    else "delivery_revoked"
                )
            return state
        return "proposed"

    @staticmethod
    def _normal_card_actions(body: str) -> tuple[str, ...]:
        """Keep normal cards actionable without exposing lifecycle diagnostics."""
        blocked = (
            "revision", "digest", "epoch", "reservation", "recovery",
            "internal reason", "내부 사유", "복구", "예약", "리비전",
        )
        actions: list[str] = []
        for raw_line in body.splitlines():
            line = " ".join(raw_line.strip().lstrip("-•0123456789. ").split())
            if (
                not line
                or any(token in line.lower() for token in blocked)
                or len(line) > 180
            ):
                continue
            if raw_line.lstrip().startswith(("-", "•")):
                actions.append(line)
            if len(actions) == 3:
                break
        return tuple(actions) or ("승인 후 고객에게 전달할 행동을 확인합니다.",)

    def _operator_card_envelope(
        self,
        proposal: object,
        *,
        customer_key: str,
        state: str,
        body: object,
        customer_preview: object | None = None,
    ) -> tuple[str, Mapping[str, object]]:
        text = str(body or "").strip()
        if not text:
            raise AdaptiveWorkflowError("adaptive operator card text is unavailable")
        revision = getattr(proposal, "revision", None)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise AdaptiveWorkflowError("adaptive operator card revision is invalid")
        normalized_state = " ".join(str(state or "").split())
        if not normalized_state:
            raise AdaptiveWorkflowError("adaptive operator card state is invalid")
        customer_preview = str(
            customer_preview
            if customer_preview is not None
            else getattr(proposal, "customer_body", "")
        ).strip()
        if not customer_preview:
            raise AdaptiveWorkflowError(
                "adaptive customer delivery preview is unavailable"
            )
        envelope: Mapping[str, object] = {
            "customer_label": self._customer_display_label(customer_key),
            "kst_day": self._proposal_kst_day(proposal),
        }
        actions = self._normal_card_actions(customer_preview)
        compact = (
            f"고객: {envelope['customer_label']} · 날짜: {envelope['kst_day']}\n"
            "이전 기록과 이번 입력을 함께 반영했습니다.\n"
            "판단: 현재 근거 범위에서만 잠금 · 승인 전 고객에게 전송되지 않습니다.\n\n"
            "실행 제안\n"
            + "\n".join(f"{index}. {action}" for index, action in enumerate(actions, 1))
            + "\n\n고객 전달 미리보기\n"
            + customer_preview
            + "\n\n안전·과장 점검: 확인된 기록 범위를 넘는 단정이나 전송은 하지 않습니다."
        )
        return compact, envelope

    @staticmethod
    def _card_hidden_pins(
        proposal: object,
        state: str,
        customer_preview: str,
    ) -> dict[str, object]:
        return {
            "proposal_digest": str(getattr(proposal, "digest", "") or ""),
            "revision": int(getattr(proposal, "revision", 0) or 0),
            "lifecycle_state": str(state or ""),
            "customer_preview": customer_preview,
            "customer_preview_digest": hashlib.sha256(
                customer_preview.encode("utf-8")
            ).hexdigest(),
        }

    def _card_with_envelope(
        self,
        proposal: object,
        *,
        customer_key: str,
        state: str,
        body: object,
        customer_preview: object | None = None,
    ) -> tuple[str, Mapping[str, object]]:
        return self._operator_card_envelope(
            proposal,
            customer_key=customer_key,
            state=state,
            body=body,
            customer_preview=customer_preview,
        )
    def _render_latest_card(
        self,
        adaptive: object,
        customer_key: str,
        *,
        message_id: object,
        chat_id: object,
        topic_id: object,
        lifecycle_state: str | None = None,
    ) -> Mapping[str, object]:
        latest_resolver = getattr(adaptive, "_latest_production_proposal", None)
        renderer = getattr(adaptive, "render_proposal", None)
        registration_pin = getattr(adaptive, "_registration_pin", None)
        previewer = getattr(adaptive, "preview_registered_daily_projection", None)
        if (
            not callable(latest_resolver)
            or not callable(renderer)
            or not callable(previewer)
        ):
            raise AdaptiveWorkflowError("adaptive latest proposal is unavailable")
        proposal = latest_resolver()
        registration_digest = (
            str(registration_pin(proposal, required=True))
            if callable(registration_pin)
            else ""
        )
        state = lifecycle_state or self._proposal_card_state(adaptive, proposal)
        customer_preview = previewer(proposal)
        text, envelope = self._card_with_envelope(
            proposal,
            customer_key=customer_key,
            state=state,
            body=renderer(proposal, topic_id=OPERATOR_REVIEW_TOPIC_ID),
            customer_preview=customer_preview,
        )
        return {
            "status": "card",
            "customer_key": customer_key,
            "text": text,
            "envelope": envelope,
            "buttons": self._card_buttons(
                customer_key=customer_key,
                proposal=proposal,
                message_id=message_id,
                chat_id=chat_id,
                topic_id=topic_id,
                state=state,
                source_digest=getattr(proposal, "source_digest", ""),
                registration_digest=registration_digest,
            ),
            **self._card_hidden_pins(proposal, state, customer_preview),
        }
    def _transition_capability(
        self,
        session: Mapping[str, object],
        *,
        action: str,
    ) -> AdaptiveOperatorCapability:
        """Mint and claim one fresh, action-bound capability for a composite step."""
        callback = self._issue(
            action=action,
            customer_key=str(session["customer_key"]),
            proposal_digest=str(session["proposal_digest"]),
            revision=int(session["revision"]),
            source_digest=str(session.get("source_digest", "") or ""),
            registration_digest=str(session.get("registration_digest", "") or ""),
            originating_message_id=session.get("originating_message_id", ""),
            originating_chat_id=session.get("originating_chat_id", ""),
            originating_topic_id=session.get("originating_topic_id", 59),
        )
        parts = callback.split(":")
        claimed = self._claim_issued_callback(parts[1], action)
        if claimed is None or claimed.get("_claim_winner") is not True:
            raise AdaptiveWorkflowError("adaptive composite transition capability is stale")
        self._consume(claimed, state="consumed")
        return self._capability(
            self._find_session(parts[1], action) or claimed,
            action=action,
        )

    def _schedule_confirmation_is_current(
        self,
        adaptive: object,
        customer_key: str,
    ) -> bool:
        if not self._schedule_confirm_enabled:
            return True
        try:
            reference = self._schedule_reference(customer_key)
            rows = adaptive.store.read()
        except (AdaptiveWorkflowError, AttributeError, OSError, TypeError, ValueError):
            return False
        return any(
            isinstance(row, Mapping)
            and row.get("event_type") == "schedule_strategy_confirmed"
            and isinstance(row.get("payload"), Mapping)
            and row["payload"].get("source_reference_id") == reference.event_id
            for row in rows
        )

    def _approve_and_send(
        self,
        adaptive: object,
        session: Mapping[str, object],
        *,
        customer_key: str,
        proposal_digest: str,
    ) -> object:
        """Resume the durable lifecycle from its current state using fresh authority."""
        state = self._proposal_card_state(
            adaptive,
            adaptive._proposal_for_digest(proposal_digest),
        )
        if state in {"proposed", "edited", "released"}:
            if (
                self._schedule_confirm_enabled
                and not self._schedule_confirmation_is_current(
                    adaptive,
                    customer_key,
                )
            ):
                schedule_capability = self._transition_capability(
                    session, action="schedule_confirm",
                )
                schedule_session = self._find_session(
                    schedule_capability.capability_id, "schedule_confirm",
                )
                if schedule_session is None:
                    raise AdaptiveWorkflowError(
                        "adaptive schedule confirmation capability is stale"
                    )
                self._resume_schedule_confirmation(
                    schedule_session,
                    schedule_capability,
                )
                latest_session = self._find_session(
                    str(session.get("session_id", "")),
                    "approve_and_send",
                )
                if latest_session is None:
                    raise AdaptiveWorkflowError(
                        "adaptive composite session is stale"
                    )
                self._consume(latest_session, state="schedule_confirmed")
                return {
                    "status": "operator_input_required",
                    "text": (
                        "일정 확인을 반영했습니다. 최신 근거로 새 초안을 생성해 "
                        "다시 검토해 주세요."
                    ),
                }
            adaptive.approve_latest(
                proposal_digest,
                operator_id=self._transition_capability(session, action="approve"),
            )
            state = "approved"
        if state == "approved":
            adaptive.activate_latest(
                proposal_digest,
                operator_id=self._transition_capability(session, action="activate"),
            )
            state = "activated"
        if state in {"activated", "delivery_revoked"} or not bool(
            getattr(adaptive, "delivery_enabled", False)
        ):
            self.coordinator.set_adaptive_delivery(
                customer_key,
                True,
                operator_id=self._transition_capability(session, action="delivery_enable"),
            )
        return adaptive.deliver_latest_once(
            proposal_digest,
            operator_id=self._transition_capability(session, action="send"),
        )

    def _find_session(self, token: str, action: str) -> dict[str, object] | None:
        if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{24}", token) is None:
            return None
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        latest: dict[str, object] | None = None
        for row in self._read_rows():
            if (
                row.get("token_hash") == token_hash
                and row.get("session_id") == token
                and row.get("action") == action
                and tuple(row.get("action_allowlist", ())) == (action,)
            ):
                latest = row
        return latest
    def _claim_issued_callback(
        self,
        token: str,
        action: str,
    ) -> dict[str, object] | None:
        """Claim one issued callback before validating or dispatching it."""
        if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{24}", token) is None:
            return None
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._authority_session_lock(), self._publication_ledger_lock():
            latest: dict[str, object] | None = None
            for row in self._read_rows_unlocked():
                if (
                    row.get("token_hash") == token_hash
                    and row.get("session_id") == token
                    and row.get("action") == action
                    and tuple(row.get("action_allowlist", ())) == (action,)
                ):
                    latest = row
            if latest is None or latest.get("state") != "issued":
                return latest
            body = dict(latest)
            body.pop("row_digest", None)
            body["state"] = "claimed"
            body["predecessor"] = latest.get("row_digest", "")
            claimed = self._append_row_unlocked(body)
            return {**claimed, "_claim_winner": True}


    def _capability(
        self,
        session: Mapping[str, object],
        *,
        action: str,
    ) -> AdaptiveOperatorCapability:
        session_id = str(session.get("session_id", ""))
        token_hash = hashlib.sha256(session_id.encode("ascii")).hexdigest() if re.fullmatch(
            r"[a-f0-9]{24}", session_id
        ) else ""
        if (
            session.get("action") != action
            or tuple(session.get("action_allowlist", ())) != (action,)
            or not hmac.compare_digest(str(session.get("token_hash", "")), token_hash)
            or not hmac.compare_digest(str(session.get("nonce_digest", "")), token_hash)
        ):
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session action or nonce is stale")
        try:
            issued = datetime.fromisoformat(str(session.get("issued_kst", "")))
            expires = datetime.fromisoformat(str(session.get("expires_kst", "")))
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=_KST)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_KST)
            now = self._now()
            if now < issued.astimezone(_KST) or now >= expires.astimezone(_KST):
                self._consume(session, state="expired")
                raise AdaptiveWorkflowError("adaptive operator session is expired")
        except AdaptiveWorkflowError:
            raise
        except (TypeError, ValueError) as exc:
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session expiry is invalid") from exc
        persisted_review = tuple(session.get("review_operator", ()))
        if persisted_review != self.review_operator:
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
        origin_chat = str(session.get("originating_chat_id", "") or "").strip()
        origin_topic = str(session.get("originating_topic_id", "") or "").strip()
        provenance = {
            "review_operator": self.review_operator,
            "review_config_version": self.review_operator_version,
            "originating_message_id": str(session.get("originating_message_id", "") or ""),
            "originating_chat_id": origin_chat,
            "originating_topic_id": origin_topic,
        }
        expected_provenance = self._safe_digest(provenance)
        if (
            origin_chat != self.review_operator[1]
            or origin_topic != self.review_operator[2]
            or session.get("review_config_version") != self.review_operator_version
            or session.get("provenance_digest") != expected_provenance
        ):
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
        owner, owner_version = self._owner()
        if tuple(session.get("canonical_owner_snapshot", ())) != owner:
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session authority is stale")
        if session.get("canonical_owner_version") != owner_version:
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator session authority version is stale")
        current_pins = self._pins(str(session.get("customer_key", "")))
        for field in (
            "config_digest",
            "registry_digest",
            "consent_digest",
            "activation_digest",
            "policy_digest",
            "catalog_digest",
            "meal_constraints_digest",
            "epoch_digest",
        ):
            expected = current_pins["config_digest"] if field == "epoch_digest" else current_pins[field]
            if session.get(field) != expected:
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive operator session pins are stale")
        expected_authority = self._safe_digest({"owner": owner, **current_pins})
        if session.get("authority_digest") != expected_authority:
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator authority pin is stale")
        proposal_digest = (
            str(session["proposal_digest"])
            if isinstance(session.get("proposal_digest"), str)
            else None
        )
        revision = session.get("revision") if isinstance(session.get("revision"), int) else None
        if action not in {"select", "create"} and (
            not proposal_digest
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            self._consume(session, state="revoked")
            raise AdaptiveWorkflowError("adaptive operator proposal pin is stale")
        if action == "schedule_confirm":
            try:
                reference = self._schedule_reference(str(session.get("customer_key", "")))
            except AdaptiveWorkflowError:
                self._consume(session, state="revoked")
                raise
            if (
                session.get("schedule_event_id") != reference.event_id
                or session.get("schedule_event_digest") != reference.event_digest
            ):
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive schedule confirmation reference is stale")
        if action not in {"select", "create"} and (
            session.get("source_digest") or session.get("registration_digest")
        ):
            resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
            if not callable(resolver):
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive operator live pins are unavailable")
            try:
                adaptive = resolver(str(session.get("customer_key", "")))
                latest = adaptive._latest_production_proposal()
                if latest.digest != proposal_digest or latest.revision != revision:
                    raise AdaptiveWorkflowError("adaptive operator proposal is stale")
                if session.get("source_digest") != getattr(latest, "source_digest", None):
                    raise AdaptiveWorkflowError("adaptive operator source pin is stale")
                registration_pin = getattr(adaptive, "_registration_pin", None)
                if session.get("registration_digest") != (
                    registration_pin(latest, required=True) if callable(registration_pin) else None
                ):
                    raise AdaptiveWorkflowError("adaptive operator registration pin is stale")
            except AdaptiveWorkflowError:
                self._consume(session, state="revoked")
                raise
            except Exception as exc:
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive operator live pins are unavailable") from exc
        return AdaptiveOperatorCapability(
            schema_version="1.0",
            capability_id=session_id,
            review_operator=self.review_operator,
            review_operator_version=self.review_operator_version,
            canonical_owner=owner,
            canonical_owner_version=owner_version,
            customer_key=str(session.get("customer_key", "")),
            action=action,
            proposal_digest=proposal_digest,
            revision=revision,
            schedule_event_id=(
                str(session["schedule_event_id"])
                if isinstance(session.get("schedule_event_id"), str)
                else None
            ),
            schedule_event_digest=(
                str(session["schedule_event_digest"])
                if isinstance(session.get("schedule_event_digest"), str)
                else None
            ),
            config_digest=str(session.get("config_digest", "")),
            registry_digest=str(session.get("registry_digest", "")),
            consent_digest=str(session.get("consent_digest", "")),
            activation_digest=str(session.get("activation_digest", "")),
            issued_kst=str(session.get("issued_kst", "")),
            expires_kst=str(session.get("expires_kst", "")),
            nonce_digest=str(session.get("nonce_digest", "")),
            epoch_digest=str(session.get("epoch_digest", session.get("config_digest", ""))),
            source_digest=str(session.get("source_digest", "")),
            registration_digest=str(session.get("registration_digest", "")),
            originating_message_id=str(session.get("originating_message_id", "")),
            originating_chat_id=origin_chat,
            originating_topic_id=origin_topic,
            provenance_digest=str(session.get("provenance_digest", "")),
            policy_digest=str(session.get("policy_digest", "")),
            catalog_digest=str(session.get("catalog_digest", "")),
            meal_constraints_digest=str(session.get("meal_constraints_digest", "")),
        )

    def _consume(self, session: Mapping[str, object], *, state: str) -> None:
        session_id = str(session.get("session_id", ""))
        predecessor = str(session.get("row_digest", ""))
        body = dict(session)
        body.pop("row_digest", None)
        body["state"] = state
        body["predecessor"] = predecessor
        body["session_id"] = session_id
        with self._authority_session_lock(), self._publication_ledger_lock():
            self._append_row_unlocked(body)
    def _schedule_confirm_request(
        self,
        session: Mapping[str, object],
        capability: AdaptiveOperatorCapability,
    ) -> ScheduleConfirmationRequest:
        """Rebuild the durable, action-specific confirmation intent."""
        intent = session.get("schedule_confirm_intent")
        expected = {
            "customer_key": capability.customer_key,
            "schedule_event_id": capability.schedule_event_id,
            "schedule_event_digest": capability.schedule_event_digest,
            "capability_id": capability.capability_id,
        }
        if not isinstance(intent, Mapping) or dict(intent) != expected:
            raise AdaptiveWorkflowError("adaptive schedule confirmation intent is invalid")
        return ScheduleConfirmationRequest(
            capability,
            str(capability.schedule_event_id or ""),
            str(capability.schedule_event_digest or ""),
        )

    def _resume_schedule_confirmation(
        self,
        session: Mapping[str, object],
        capability: AdaptiveOperatorCapability,
    ) -> Mapping[str, object]:
        """Finish a durable confirmation intent after an interrupted callback."""
        handler = self._schedule_confirm_handler
        confirm = getattr(handler, "confirm", None)
        if not callable(confirm):
            raise AdaptiveWorkflowError("adaptive schedule confirmation is unavailable")
        result = confirm(self._schedule_confirm_request(session, capability))
        if not isinstance(result, Mapping):
            raise AdaptiveWorkflowError("adaptive schedule confirmation result is invalid")
        return result


    def handle_callback(
        self,
        value: object,
        address: object,
        *,
        message_id: object = "",
    ) -> Mapping[str, object]:
        if not isinstance(value, str):
            return {"status": "rejected", "text": "만료되었거나 사용할 수 없는 영양 검토 버튼입니다."}
        match = _ADAPTIVE_CALLBACK_RE.fullmatch(value)
        if match is None or not self.accepts(address):
            return {"status": "rejected", "text": "이 버튼은 운영자 검토실에서만 사용할 수 있습니다."}
        token, action = match.groups()
        try:
            replay_safe = action in {"view", "back"}
            if replay_safe:
                session = self._find_session(token, action)
                if session is None:
                    raise AdaptiveWorkflowError("adaptive operator session is stale")
                claim_winner = False
            else:
                session = self._claim_issued_callback(token, action)
                if session is None:
                    raise AdaptiveWorkflowError("adaptive operator session is stale")
                claim_winner = session.pop("_claim_winner", False) is True
            if replay_safe:
                if session.get("state") != "issued":
                    raise AdaptiveWorkflowError("adaptive operator session is stale")
            elif session.get("state") != "issued" and not (
                session.get("state") == "claimed" and claim_winner
            ):
                if (
                    action == "schedule_confirm"
                    and session.get("state") in {"claimed", "consumed"}
                ):
                    capability = self._capability(session, action=action)
                    result = self._resume_schedule_confirmation(session, capability)
                    if session.get("state") != "consumed":
                        self._consume(session, state="consumed")
                    response = dict(result)
                    response.update({"status": "duplicate", "duplicate": True})
                    return response
                if (
                    action == "approve_and_send"
                    and session.get("state") in {"claimed", "consumed", "schedule_confirmed"}
                ):
                    if (
                        str(session.get("originating_message_id", "") or "")
                        and str(message_id or "")
                        != str(session.get("originating_message_id", "") or "")
                    ):
                        raise AdaptiveWorkflowError(
                            "adaptive operator session message mismatch"
                        )
                    key = str(session.get("customer_key", "") or "")
                    digest_value = str(session.get("proposal_digest", "") or "")
                    adaptive = self.coordinator.adaptive_nutrition_coordinator(key)
                    result = self._approve_and_send(
                        adaptive,
                        session,
                        customer_key=key,
                        proposal_digest=digest_value,
                    )
                    if inspect.isawaitable(result):
                        return {
                            "status": "delivery_pending",
                            "delivery": result,
                            "proposal_digest": digest_value,
                        }
                    response = dict(result) if isinstance(result, Mapping) else {}
                    response["duplicate"] = True
                    return response
                if session.get("state") in {"claimed", "consumed"}:
                    return {
                        "status": "duplicate",
                        "text": (
                            "이미 처리된 전송입니다."
                            if action == "send"
                            else "이미 처리된 검토 버튼입니다."
                        ),
                    }
                raise AdaptiveWorkflowError("adaptive operator session is already consumed")
            expires = datetime.fromisoformat(str(session.get("expires_kst", "")))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_KST)
            if self._now() >= expires.astimezone(_KST):
                self._consume(session, state="expired")
                raise AdaptiveWorkflowError("adaptive operator session is expired")
            origin_message = str(session.get("originating_message_id", "") or "")
            if (
                origin_message
                and str(message_id or "") != origin_message
            ):
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive operator session message mismatch")
            if (
                str(session.get("originating_chat_id", "") or "") != self.review_operator[1]
                or str(session.get("originating_topic_id", "") or "") != self.review_operator[2]
            ):
                self._consume(session, state="revoked")
                raise AdaptiveWorkflowError("adaptive operator session provenance is stale")
            capability = self._capability(session, action=action)
            if replay_safe:
                cached = self._replay_cache.get(token)
                if isinstance(cached, Mapping):
                    return dict(cached)
            key = capability.customer_key
            if action == "select":
                self._consume(session, state="consumed")
                callback = self._issue(
                    action="create",
                    customer_key=key,
                    originating_message_id=message_id,
                    originating_chat_id=session.get("originating_chat_id", ""),
                    originating_topic_id=session.get("originating_topic_id", 59),
                )
                return {"status": "selected", "customer_key": key, "callback_data": callback}
            resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
            if not callable(resolver):
                raise AdaptiveWorkflowError("adaptive operator customer service is unavailable")
            adaptive = resolver(key)
            if isinstance(capability, AdaptiveOperatorCapability):
                adaptive._last_authenticated_review_operator = capability.review_operator
                adaptive._last_review_operator_version = capability.review_operator_version
            if action != "create":
                latest_resolver = getattr(adaptive, "_latest_production_proposal", None)
                if not callable(latest_resolver):
                    raise AdaptiveWorkflowError("adaptive latest proposal is unavailable")
                latest = latest_resolver()
                if (
                    latest.digest != capability.proposal_digest
                    or latest.revision != capability.revision
                ):
                    raise AdaptiveWorkflowError("adaptive operator proposal is stale")
            digest_value = capability.proposal_digest
            if action == "create":
                self._consume(session, state="consumed")
                proposal, raw_text = adaptive.create_production_proposal(
                    _current_kst_date(),
                    operator_id=capability,
                    topic_id=OPERATOR_REVIEW_TOPIC_ID,
                )
                customer_preview = adaptive.preview_registered_daily_projection(
                    proposal
                )
                text, envelope = self._card_with_envelope(
                    proposal,
                    customer_key=key,
                    state=self._action_card_state(action) or "proposed",
                    body=raw_text,
                    customer_preview=customer_preview,
                )
                registration_digest = ""
                registration_pin = getattr(adaptive, "_registration_pin", None)
                if callable(registration_pin):
                    registration_digest = str(registration_pin(proposal, required=True))
                buttons = self._card_buttons(
                    customer_key=key,
                    proposal=proposal,
                    message_id=message_id,
                    chat_id=session.get("originating_chat_id", ""),
                    topic_id=session.get("originating_topic_id", 59),
                    state="proposed",
                    source_digest=str(getattr(proposal, "source_digest", "") or ""),
                    registration_digest=registration_digest,
                )
                return {
                    "status": "card",
                    "customer_key": key,
                    "text": text,
                    "envelope": envelope,
                    "buttons": buttons,
                    **self._card_hidden_pins(
                        proposal,
                        "proposed",
                        customer_preview,
                    ),
                }
            if action == "view":
                if not digest_value:
                    raise AdaptiveWorkflowError("adaptive operator proposal is unavailable")
                proposal = adaptive._proposal_for_digest(digest_value)
                raw_text = adaptive.render_proposal(
                    proposal,
                    topic_id=OPERATOR_REVIEW_TOPIC_ID,
                )
                text, envelope = self._card_with_envelope(
                    proposal,
                    customer_key=key,
                    state=self._proposal_card_state(adaptive, proposal),
                    body=raw_text,
                    customer_preview=adaptive.preview_registered_daily_projection(
                        proposal
                    ),
                )
                response = {
                    "status": "view",
                    "text": text,
                    "envelope": envelope,
                    "proposal_digest": digest_value,
                }
                self._replay_cache[token] = response
                return response
            if action == "back":
                response = self.open_menu(
                    address,
                    message_id=message_id,
                    chat_id=session.get("originating_chat_id", ""),
                    topic_id=session.get("originating_topic_id", 59),
                )
                self._replay_cache[token] = dict(response)
                return response
            if not digest_value:
                raise AdaptiveWorkflowError("adaptive operator proposal is unavailable")
            if action == "edit_note":
                self._consume(session, state="awaiting_input")
                return {
                    "status": "operator_input_required",
                    "action": action,
                    "proposal_digest": digest_value,
                    "customer_key": key,
                    "text": (
                        "메모 입력 대기 중입니다. 이 검토 카드에 답장으로 "
                        "수정할 메모를 보내주세요."
                    ),
                }
            self._consume(session, state="consumed")
            if action == "hold":
                result = adaptive.hold_latest(digest_value, operator_id=capability)
            elif action == "release":
                result = adaptive.release_latest(digest_value, operator_id=capability)
            elif action == "approve":
                result = adaptive.approve_latest(digest_value, operator_id=capability)
            elif action == "approve_and_send":
                result = self._approve_and_send(
                    adaptive,
                    session,
                    customer_key=key,
                    proposal_digest=digest_value,
                )
                if inspect.isawaitable(result):
                    return {
                        "status": "delivery_pending",
                        "delivery": result,
                    }
            elif action == "schedule_confirm":
                # The intent is issued with the capability and survives the
                # generic consume row, so an exact replay can safely resume it.
                result = self._resume_schedule_confirmation(session, capability)
            elif action == "activate":
                result = adaptive.activate_latest(digest_value, operator_id=capability)
            elif action in {"delivery_enable", "delivery_revoke"}:
                result = self.coordinator.set_adaptive_delivery(
                    key,
                    action == "delivery_enable",
                    operator_id=capability,
                )
            elif action == "send":
                result = adaptive.deliver_latest_once(
                    digest_value,
                    operator_id=capability,
                )
                if inspect.isawaitable(result):
                    return {
                        "status": "delivery_pending",
                        "delivery": result,
                        "proposal_digest": digest_value,
                    }
            elif action == "reconcile":
                reconciled = adaptive.reconcile_delivery_receipts(
                    digest_value,
                    operator_id=capability,
                )
                if isinstance(reconciled, Mapping):
                    result = reconciled
                elif isinstance(reconciled, (tuple, list)) and reconciled:
                    result = (
                        dict(reconciled[-1])
                        if isinstance(reconciled[-1], Mapping)
                        else {"status": "reconciled"}
                    )
                else:
                    result = {"status": "reconciled", "text": "감사 기록을 확인했습니다."}
            else:
                raise AdaptiveWorkflowError("adaptive operator action is unavailable")
            if action in {
                "hold",
                "release",
                "approve",
                "activate",
                "delivery_enable",
                "delivery_revoke",
            }:
                response = self._render_latest_card(
                    adaptive,
                    key,
                    message_id=message_id,
                    chat_id=session.get("originating_chat_id", ""),
                    topic_id=session.get("originating_topic_id", 59),
                    lifecycle_state=self._action_card_state(action),
                )
            else:
                response = dict(result) if isinstance(result, Mapping) else {"status": action}
                response.setdefault("status", action)
            if action in {"approve_and_send", "send", "reconcile"}:
                response["text"] = adaptive_delivery_result_text(response)
            return response
        except AdaptiveWorkflowError as exc:
            logger.warning(
                "adaptive operator callback rejected: action=%s customer=%s error=%s",
                action,
                session.get("customer_key", "") if isinstance(session, Mapping) else "",
                exc,
            )
            if "already attempted" in str(exc):
                return {"status": "duplicate", "text": "이미 처리된 전송입니다."}
            return {"status": "rejected", "text": "만료되었거나 사용할 수 없는 영양 검토 버튼입니다."}
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "adaptive operator callback failed: action=%s customer=%s error=%s",
                action,
                session.get("customer_key", "") if isinstance(session, Mapping) else "",
                exc,
            )
            return {"status": "rejected", "text": "만료되었거나 사용할 수 없는 영양 검토 버튼입니다."}

    def handle_text(
        self,
        address: object,
        *,
        message_id: object = "",
        text: object = "",
        chat_id: object = "",
        topic_id: object = 59,
    ) -> Mapping[str, object]:
        if not self.accepts(address):
            return {"status": "rejected", "text": "이 공간에서는 적응형 영양 검토를 사용할 수 없습니다."}
        raw_text = str(text or "")
        if len(raw_text.encode("utf-16-le")) // 2 > 4000:
            return {"status": "rejected", "text": "입력이 너무 깁니다. 메모는 4000자 이내로 보내 주세요."}
        try:
            awaiting = next(
                (
                    row
                    for row in reversed(self._read_rows())
                    if row.get("state") == "awaiting_input"
                    and tuple(row.get("action_allowlist", ())) == ("edit_note",)
                    and str(row.get("originating_message_id", "") or "") == str(message_id or "")
                ),
                None,
            )
            if awaiting is not None:
                expires = datetime.fromisoformat(str(awaiting.get("expires_kst", "")))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=_KST)
                if self._now() >= expires.astimezone(_KST):
                    self._consume(awaiting, state="expired")
                    raise AdaptiveWorkflowError("adaptive note session is expired")
                capability = self._capability(awaiting, action="edit_note")
                resolver = getattr(self.coordinator, "adaptive_nutrition_coordinator", None)
                if not callable(resolver) or not capability.proposal_digest:
                    raise AdaptiveWorkflowError("adaptive note session is unavailable")
                adaptive = resolver(capability.customer_key)
                proposal = adaptive._proposal_for_digest(capability.proposal_digest)
                revised = adaptive.revise_note(
                    proposal,
                    topic_id=OPERATOR_REVIEW_TOPIC_ID,
                    note=raw_text,
                    operator_id=capability,
                )
                self._consume(awaiting, state="consumed")
                customer_preview = adaptive.preview_registered_daily_projection(
                    revised
                )
                text, envelope = self._card_with_envelope(
                    revised,
                    customer_key=capability.customer_key,
                    state=self._action_card_state("edit_note") or "edited",
                    body=adaptive.render_proposal(
                        revised,
                        topic_id=OPERATOR_REVIEW_TOPIC_ID,
                    ),
                    customer_preview=customer_preview,
                )
                registration_digest = ""
                registration_pin = getattr(adaptive, "_registration_pin", None)
                if callable(registration_pin):
                    registration_digest = str(registration_pin(revised, required=True))
                buttons = self._card_buttons(
                    customer_key=capability.customer_key,
                    proposal=revised,
                    message_id=message_id,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    state="edited",
                    source_digest=str(getattr(revised, "source_digest", "") or ""),
                    registration_digest=registration_digest,
                )
                return {
                    "status": "card",
                    "customer_key": capability.customer_key,
                    "proposal_digest": revised.digest,
                    "revision": revised.revision,
                    "text": text,
                    "envelope": envelope,
                    "buttons": buttons,
                    **self._card_hidden_pins(
                        revised,
                        "edited",
                        customer_preview,
                    ),
                }
        except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
            return {"status": "rejected", "text": "메모를 반영할 수 없습니다. 최신 검토 카드에서 다시 시작해 주세요."}
        normalized = " ".join(str(text or "").split())
        if normalized in {"적응형 영양 검토", "/adaptive_review"}:
            return self.open_menu(
                address,
                message_id=message_id,
                chat_id=chat_id,
                topic_id=topic_id,
            )
        return {"status": "rejected", "text": "이 공간에서는 적응형 영양 검토 메뉴만 사용할 수 있습니다."}

class AdaptiveWorkflowError(ValueError):
    """Raised when an adaptive proposal crosses its operator-only boundary."""

class AdaptiveTransportPreflightRejected(RuntimeError):
    """Raised only when adaptive transport proves provider invocation did not start."""



class AdaptiveNutritionCoordinator:
    """Build and audit immutable adaptive proposals in operator topic 59.

    Production delivery is available only through the registered customer
    transport; the provider call is strictly single-shot and its receipt is
    audited before the sent audit event.
    """

    @classmethod
    def _for_shadow_test(
        cls,
        *,
        _shadow_factory_token: object,
        **kwargs: object,
    ) -> "AdaptiveNutritionCoordinator":
        """Construct the caller-fed test surface through the sealed factory."""
        if (
            cls is not AdaptiveNutritionCoordinator
            or _shadow_factory_token is not _ADAPTIVE_SHADOW_FACTORY_TOKEN
        ):
            raise AdaptiveWorkflowError("adaptive shadow construction is sealed")
        if "_shadow_factory_token" in kwargs:
            raise AdaptiveWorkflowError("adaptive shadow construction is sealed")
        return cls(
            **kwargs,
            _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
        )

    def __init__(
        self,
        *,
        customer_key: str,
        starts_on: date | None = None,
        event_path: Path | str | None = None,
        operator_topic_id: int = OPERATOR_REVIEW_TOPIC_ID,
        store: object | None = None,
        profile_root: Path | str | None = None,
        registry_path: Path | str | None = None,
        canonical_event_source: object | None = None,
        customer_runtime: object | None = None,
        authority: object | None = None,
        customer_transport: object | None = None,
        delivery_enabled: bool = False,
        _shadow_factory_token: object | None = None,
    ) -> None:
        if type(operator_topic_id) is not int or operator_topic_id != OPERATOR_REVIEW_TOPIC_ID:
            raise AdaptiveWorkflowError("adaptive proposals require operator topic 59")
        key = str(customer_key or "").strip()
        if not key or len(key) > 64:
            raise AdaptiveWorkflowError("adaptive customer key is invalid")
        shadow_test_only = _shadow_factory_token is _ADAPTIVE_SHADOW_FACTORY_TOKEN
        if _shadow_factory_token is not None and not shadow_test_only:
            raise AdaptiveWorkflowError("adaptive shadow construction is sealed")
        if type(delivery_enabled) is not bool:
            raise AdaptiveWorkflowError("adaptive delivery gate is invalid")
        registered_runtime = (
            CustomerRuntime is not None
            and type(customer_runtime) is CustomerRuntime
            and RegisteredCustomerBinding is not None
            and type(getattr(customer_runtime, "binding", None))
            is RegisteredCustomerBinding
        )
        if not shadow_test_only and (
            customer_runtime is not None
            or canonical_event_source is not None
            or authority is not None
        ) and not registered_runtime:
            raise AdaptiveWorkflowError("adaptive registered customer runtime is required")
        if customer_transport is not None and not any(
            callable(getattr(customer_transport, name, None))
            for name in ("send_customer", "send_adaptive_customer")
        ):
            raise AdaptiveWorkflowError("customer transport is unavailable")
        if AdaptiveEventStore is None and store is None:
            raise AdaptiveWorkflowError("adaptive profile APIs are unavailable")
        if store is None:
            if event_path is None:
                runtime_root = getattr(customer_runtime, "data_root", None)
                if isinstance(runtime_root, Path):
                    event_path = runtime_root / "nutrition-plans" / "events.jsonl"
            if event_path is None or not str(event_path):
                raise AdaptiveWorkflowError("adaptive event path is required")
            try:
                if CustomerRuntime is not None and type(customer_runtime) is CustomerRuntime:
                    store = AdaptiveEventStore.for_registered(customer_runtime)
                elif (
                    shadow_test_only
                    and CanonicalEventTransaction is not None
                    and isinstance(getattr(customer_runtime, "data_root", None), Path)
                ):
                    runtime_root = customer_runtime.data_root
                    transaction = CanonicalEventTransaction(
                        runtime_root / "wizard" / "events.jsonl",
                        runtime_root / "nutrition-plans" / "canonical-sequence.jsonl",
                    )
                    store = AdaptiveEventStore(
                        Path(event_path),
                        canonical_transaction=transaction,
                        root=Path(event_path).parent,
                    )
                else:
                    store = AdaptiveEventStore(Path(event_path))
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive event store is unavailable") from exc
        if not callable(getattr(store, "append", None)) or not callable(
            getattr(store, "read", None)
        ):
            raise AdaptiveWorkflowError("adaptive event store is unavailable")
        if (
            canonical_event_source is not None
            and not isinstance(canonical_event_source, Mapping)
            and not any(
                callable(getattr(canonical_event_source, name, None))
                for name in (
                    "_read_events",
                    "read_reconciled_events",
                    "read_reconciled",
                    "reconciled_events",
                    "read_canonical_events",
                    "events_for",
                    "for_customer",
                    "store_for",
                )
            )
        ):
            raise AdaptiveWorkflowError("canonical customer EventStore is unavailable")
        if profile_root is not None:
            try:
                resolved_profile_root = Path(profile_root).resolve()
            except (OSError, RuntimeError, TypeError) as exc:
                raise AdaptiveWorkflowError("adaptive profile root is unavailable") from exc
        else:
            resolved_profile_root = None
        self.customer_key = key
        self.starts_on = starts_on
        self.operator_topic_id = OPERATOR_REVIEW_TOPIC_ID
        self.store = store
        self.profile_root = resolved_profile_root
        self.registry_path = Path(registry_path).resolve() if registry_path is not None else None
        self.canonical_event_source = canonical_event_source
        self.customer_runtime = customer_runtime
        self.authority = authority
        self.customer_transport = customer_transport
        self.delivery_enabled = delivery_enabled
        self._shadow_test_only = shadow_test_only
        self._lifecycle_lock = RLock()
        self._production_mode = canonical_event_source is not None or authority is not None
        self._registration_pins: dict[str, str] = {}

    @staticmethod
    def _profile_api_ready() -> bool:
        return all(
            callable(value)
            for value in (
                build_snapshot,
                canonical_json,
                propose,
                render_operator_card,
                render_customer_body,
                canonical_event_digest,
                load_approved_adaptive_artifacts,
                MealConstraints,
                project_canonical_events,
                validate_typed_safety,
            )
        ) and AdaptiveEventStore is not None

    @contextmanager
    def _authority_lock(self) -> Iterator[None]:
        """Acquire shared authority before lifecycle and store mutation locks."""
        if self.profile_root is not None and callable(profile_authority_lock):
            with profile_authority_lock(self.profile_root):
                yield
            return
        if self._production_mode:
            raise AdaptiveWorkflowError("adaptive authority lock is unavailable")
        yield
    def _topic(self, topic_id: object) -> None:
        if type(topic_id) is not int or topic_id != OPERATOR_REVIEW_TOPIC_ID:
            raise AdaptiveWorkflowError(
                "adaptive action rejected outside operator topic 59"
            )
    def _require_non_diagnostic_delivery_runtime(self) -> None:
        """Keep the generic nutrition path separate from diagnostic delivery."""
        runtime = self.customer_runtime
        binding = getattr(runtime, "registered_binding", None)
        if binding is None:
            return
        if type(binding) is not RegisteredCustomerBinding:
            raise AdaptiveWorkflowError(
                "sealed registered binding is required for ordinary delivery"
            )
        if binding.mode == "diagnostic_isolated_v1":
            raise AdaptiveWorkflowError("diagnostic delivery requires DiagnosticHost")

    def set_customer_transport(self, transport: object | None) -> None:
        """Replace the registered transport only at the host boundary."""
        if transport is not None:
            method = (
                getattr(transport, "send_adaptive_customer", None)
                if self._production_mode
                else getattr(transport, "send_customer", None)
            )
            if not callable(method):
                raise AdaptiveWorkflowError("customer transport is unavailable")
        self.customer_transport = transport

    def set_delivery_enabled(self, enabled: bool) -> None:
        """Update the explicit production delivery gate."""
        if type(enabled) is not bool:
            raise AdaptiveWorkflowError("adaptive delivery gate is invalid")
        self.delivery_enabled = enabled
    def set_persisted_delivery(
        self,
        enabled: bool,
        *,
        operator_id: object,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
    ) -> Mapping[str, object]:
        """Persist an owner-approved delivery capability transition."""
        self._require_non_diagnostic_delivery_runtime()
        if type(enabled) is not bool:
            raise AdaptiveWorkflowError("adaptive delivery gate is invalid")
        self._topic(topic_id)
        actor = self._require_operator_owner(
            operator_id,
            required_action="delivery_enable" if enabled else "delivery_revoke",
        )
        owner_snapshot = self._live_owner_key()
        owner_version = self._live_owner_version()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock:
            self._ensure_live_adaptive_journals()
            with locked():
                _customer, data_root, _spec = self._live_customer()
                self._validate_production_journals()
                epoch_path, prior = self._feature_epoch(data_root)
                risk_policy = self._risk_policy_evidence() if enabled else None
                if prior.get("delivery") is enabled:
                    if enabled:
                        latest_enable = self._latest_committed_delivery_enable_locked()
                        if latest_enable is None or any(
                            latest_enable.get(key) != risk_policy[key]
                            for key in (
                                "risk_policy_version",
                                "risk_policy_digest",
                                "risk_policy_document_digest",
                            )
                        ):
                            raise AdaptiveWorkflowError(
                                "adaptive delivery enable requires explicit owner reapproval"
                            )
                    return prior
                customer_keys = self._enabled_customer_keys()
                updated = dict(prior)
                updated["epoch"] = int(prior["epoch"]) + 1
                updated["delivery"] = enabled
                updated = self._with_feature_config_digest(updated)
                self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                self._append_config_epoch_locked(
                    int(updated["epoch"]),
                    str(updated["config_digest"]),
                    customer_keys,
                    state="prepared",
                    approved_by=self._live_owner_key(),
                    risk_policy=risk_policy,
                    delivery=enabled,
                )
                try:
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    self._write_feature_epoch(epoch_path, updated)
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    self._append_config_epoch_locked(
                        int(updated["epoch"]),
                        str(updated["config_digest"]),
                        customer_keys,
                        state="committed",
                        approved_by=self._live_owner_key(),
                        risk_policy=risk_policy,
                        delivery=enabled,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    try:
                        self._write_feature_epoch(epoch_path, prior)
                        self._append_config_epoch_locked(
                            int(updated["epoch"]),
                            str(updated["config_digest"]),
                            customer_keys,
                            state="abandoned",
                            approved_by=self._live_owner_key(),
                            risk_policy=risk_policy,
                            delivery=enabled,
                        )
                    except (OSError, TypeError, ValueError):
                        raise AdaptiveWorkflowError(
                            "adaptive delivery transition requires recovery"
                        ) from exc
                    raise AdaptiveWorkflowError(
                        "adaptive delivery transition was not committed"
                    ) from exc
                self.delivery_enabled = enabled
                return {
                    **updated,
                    "approved_by": list(self._live_owner_key()),
                    "operator_id": actor,
                    **(risk_policy or {}),
                }

    def _require_profile_api(self) -> None:
        if not self._profile_api_ready():
            raise AdaptiveWorkflowError("adaptive profile APIs are unavailable")

    def _require_production(self) -> None:
        if (
            not self._production_mode
            or self.profile_root is None
            or self.canonical_event_source is None
        ):
            raise AdaptiveWorkflowError(
                "production adaptive lifecycle requires the canonical customer boundary"
            )

    @staticmethod
    def _owner_key(value: object) -> tuple[str, str, str] | None:
        """Normalize one owner address without accepting a user id alone."""
        raw = getattr(value, "key", value)
        if isinstance(raw, Mapping):
            raw = tuple(raw.get(field) for field in ("user_id", "chat_id", "topic_id"))
        if not isinstance(raw, (tuple, list)) or len(raw) != 3:
            return None
        values = tuple(str(item).strip() if isinstance(item, str) else "" for item in raw)
        return values if all(values) else None

    def _live_owner_key(self) -> tuple[str, str, str]:
        authority = self.authority
        owner = getattr(authority, "owner", None) if authority is not None else None
        if owner is None and authority is not None:
            owner = getattr(getattr(authority, "registry", None), "owner", None)
        key = self._owner_key(owner)
        if key is None:
            raise AdaptiveWorkflowError("configured adaptive owner is unavailable")
        return key

    def _live_owner_version(self) -> int:
        authority = self.authority
        owner = getattr(authority, "owner", None) if authority is not None else None
        version = getattr(owner, "version", None)
        registry = getattr(authority, "registry", None) if authority is not None else None
        if version is None:
            version = getattr(registry, "version", None)
        if version is None and registry is not None:
            model_dump = getattr(registry, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump(mode="json")
                except TypeError:
                    dumped = model_dump()
                if isinstance(dumped, Mapping):
                    version = dumped.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return 1
        return version

    def _assert_owner_snapshot(
        self,
        operator_id: object,
        expected_key: tuple[str, str, str],
        expected_version: int,
    ) -> tuple[str, str, str]:
        if isinstance(operator_id, AdaptiveOperatorCapability):
            supplied_key = operator_id.canonical_owner
            supplied_version = operator_id.canonical_owner_version
        else:
            self._require_operator_owner(operator_id)
            supplied_key = self._owner_key(operator_id)
            supplied_version = expected_version
        if supplied_key != expected_key or supplied_version != expected_version:
            raise AdaptiveWorkflowError("adaptive lifecycle owner authority is stale")
        current_key = self._live_owner_key()
        current_version = self._live_owner_version()
        if current_key != expected_key or current_version != expected_version:
            raise AdaptiveWorkflowError("adaptive lifecycle owner authority is stale")
        return current_key

    def _configured_owner_id(self) -> str:
        """Return the live owner identity required by production actions."""
        return self._live_owner_key()[0]
    def _operator_audit_fields(self) -> dict[str, object]:
        review = getattr(self, "_last_authenticated_review_operator", None)
        review_version = getattr(self, "_last_review_operator_version", 0)
        return {
            "authenticated_review_operator": list(review) if isinstance(review, tuple) else None,
            "review_operator_version": review_version,
            "canonical_owner_snapshot": list(self._live_owner_key()),
            "canonical_owner_version": self._live_owner_version(),
        }

    @staticmethod
    def _operator_session_digest(value: object) -> str:
        if not callable(canonical_json):
            raise AdaptiveWorkflowError("adaptive canonical JSON encoder is unavailable")
        try:
            encoded = canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive canonical digest input is invalid") from exc
        if not isinstance(encoded, str):
            raise AdaptiveWorkflowError("adaptive canonical digest encoding is invalid")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _operator_session_rows(self) -> list[dict[str, object]]:
        if self.profile_root is None:
            raise AdaptiveWorkflowError("adaptive operator capability durable session ledger is unavailable")
        path = (
            self.profile_root
            / "data"
            / "owner-actions"
            / "adaptive-operator-sessions.jsonl"
        )
        if path.is_symlink() or not path.is_file():
            raise AdaptiveWorkflowError("adaptive operator capability durable session ledger is unavailable")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AdaptiveWorkflowError("adaptive operator capability durable session ledger is unavailable") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise AdaptiveWorkflowError("adaptive operator session ledger is invalid")
        rows: list[dict[str, object]] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive operator session ledger is invalid") from exc
            if not isinstance(row, dict):
                raise AdaptiveWorkflowError("adaptive operator session ledger is invalid")
            rows.append(row)
        if not rows:
            return []
        review = rows[0].get("review_operator")
        review_version = rows[0].get("review_config_version")
        if (
            not isinstance(review, list)
            or len(review) != 3
            or isinstance(review_version, bool)
            or not isinstance(review_version, int)
        ):
            raise AdaptiveWorkflowError("adaptive operator session provenance is invalid")
        verifier = object.__new__(AdaptiveOperatorService)
        verifier.review_operator = tuple(str(value).strip() for value in review)
        verifier.review_operator_version = review_version
        return verifier._validate_session_rows(rows, production=True)

    def _live_operator_pins(self) -> dict[str, str]:
        (
            _customer,
            _data_root,
            _spec,
            _events,
            source_digest,
            artifacts,
            _epoch_path,
            epoch,
            registration_binding,
        ) = self._production_context()
        snapshot = getattr(self, "_journal_snapshot", None)
        authority_rows = (
            self._journal_rows(snapshot, "authority")
            if isinstance(snapshot, Mapping)
            else ()
        )
        committed = [
            self._journal_payload(row)
            for row in authority_rows
            if self._journal_payload(row).get("state") == "committed"
        ]
        if not committed:
            raise AdaptiveWorkflowError("adaptive authority mirror is incomplete")
        authority = committed[-1]
        values = {
            "config_digest": epoch.get("config_digest"),
            "registry_digest": authority.get("registry_digest"),
            "consent_digest": authority.get("consent_digest"),
            "activation_digest": authority.get("activation_receipt_digest"),
            "policy_digest": getattr(artifacts, "policy_digest", None),
            "catalog_digest": getattr(artifacts, "catalog_digest", None),
            "meal_constraints_digest": registration_binding.meal_constraints_digest,
            "epoch_digest": epoch.get("config_digest"),
            "source_digest": source_digest,
            "registration_digest": registration_binding.registration_digest,
        }
        if any(not _is_adaptive_digest(value) for value in values.values()):
            raise AdaptiveWorkflowError("adaptive production capability pins are incomplete")
        return {key: str(value) for key, value in values.items()}

    def _validate_capability_session(
        self,
        capability: AdaptiveOperatorCapability,
        *,
        required_action: str | None,
        owner: tuple[str, str, str],
        owner_version: int,
    ) -> None:
        rows = self._operator_session_rows()
        matching = [
            row
            for row in rows
            if row.get("session_id") == capability.capability_id
        ]
        if not matching:
            raise AdaptiveWorkflowError("adaptive operator capability is not in the durable session ledger")
        session = matching[-1]
        token_hash = hashlib.sha256(capability.capability_id.encode("ascii")).hexdigest()
        if (
            session.get("token_hash") != token_hash
            or session.get("nonce_digest") != capability.nonce_digest
            or session.get("state") not in {"claimed", "consumed", "awaiting_input"}
        ):
            raise AdaptiveWorkflowError("adaptive operator capability session is not authorized")
        row_review = tuple(session.get("review_operator", ()))
        row_owner = tuple(session.get("canonical_owner_snapshot", ()))
        if (
            row_review != capability.review_operator
            or session.get("review_config_version") != capability.review_operator_version
            or row_owner != capability.canonical_owner
            or session.get("canonical_owner_version") != capability.canonical_owner_version
        ):
            raise AdaptiveWorkflowError("adaptive operator capability authority pin is stale")
        for field in (
            "customer_key",
            "action",
            "proposal_digest",
            "revision",
            "config_digest",
            "registry_digest",
            "consent_digest",
            "activation_digest",
            "policy_digest",
            "catalog_digest",
            "meal_constraints_digest",
            "epoch_digest",
            "source_digest",
            "registration_digest",
            "issued_kst",
            "expires_kst",
            "originating_message_id",
            "originating_chat_id",
            "originating_topic_id",
            "provenance_digest",
        ):
            if session.get(field) != getattr(capability, field):
                raise AdaptiveWorkflowError("adaptive operator capability session does not match")
        if (
            tuple(session.get("action_allowlist", ())) != (capability.action,)
            or required_action is not None
            and capability.action != required_action
        ):
            raise AdaptiveWorkflowError("adaptive capability action is not authorized")
        if capability.customer_key != self.customer_key:
            raise AdaptiveWorkflowError("adaptive capability customer is invalid")
        if owner != capability.canonical_owner or owner_version != capability.canonical_owner_version:
            raise AdaptiveWorkflowError("adaptive lifecycle owner authority is stale")
        try:
            issued = datetime.fromisoformat(str(session.get("issued_kst", "")))
            expires = datetime.fromisoformat(str(session.get("expires_kst", "")))
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive operator capability expiry is invalid") from exc
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=_KST)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_KST)
        now = datetime.now(_KST)
        if now < issued.astimezone(_KST) or now >= expires.astimezone(_KST):
            raise AdaptiveWorkflowError("adaptive operator capability is expired")
        origin_chat = str(session.get("originating_chat_id", "") or "").strip()
        origin_topic = str(session.get("originating_topic_id", "") or "").strip()
        provenance = {
            "review_operator": row_review,
            "review_config_version": session.get("review_config_version"),
            "originating_message_id": str(session.get("originating_message_id", "") or ""),
            "originating_chat_id": origin_chat,
            "originating_topic_id": origin_topic,
        }
        if (
            origin_chat != capability.review_operator[1]
            or origin_topic != capability.review_operator[2]
            or session.get("provenance_digest")
            != self._operator_session_digest(provenance)
        ):
            raise AdaptiveWorkflowError("adaptive operator capability provenance is stale")
        pins = self._live_operator_pins()
        for field in (
            "config_digest",
            "registry_digest",
            "consent_digest",
            "activation_digest",
            "policy_digest",
            "catalog_digest",
            "meal_constraints_digest",
            "epoch_digest",
        ):
            expected = pins["config_digest"] if field == "epoch_digest" else pins[field]
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
                or session.get(field) != expected
                or getattr(capability, field) != expected
            ):
                raise AdaptiveWorkflowError("adaptive operator capability live pins are stale")
        expected_authority = self._operator_session_digest(
            {
                "owner": owner,
                **{
                    key: value
                    for key, value in pins.items()
                    if key not in {"source_digest", "registration_digest"}
                },
            }
        )
        if session.get("authority_digest") != expected_authority:
            raise AdaptiveWorkflowError("adaptive operator capability authority pin is stale")
        if capability.action not in {"select", "create"}:
            if (
                not _is_adaptive_digest(session.get("source_digest"))
                or not _is_adaptive_digest(session.get("registration_digest"))
            ):
                raise AdaptiveWorkflowError("adaptive operator capability live pins are incomplete")
            latest = self._latest_production_proposal()
            if (
                latest.digest != capability.proposal_digest
                or latest.revision != capability.revision
            ):
                raise AdaptiveWorkflowError("adaptive operator capability proposal is stale")
            if session.get("source_digest") != getattr(latest, "source_digest", ""):
                raise AdaptiveWorkflowError("adaptive operator capability source pin is stale")
            registration_pin = getattr(self, "_registration_pin", None)
            if not callable(registration_pin) or session.get("registration_digest") != registration_pin(
                latest, required=True
            ):
                raise AdaptiveWorkflowError("adaptive operator capability registration pin is stale")
    def _require_operator_owner(
        self,
        operator_id: object,
        *,
        required_action: str | None = None,
    ) -> str:
        """Require a typed capability and refreshed canonical owner in production."""
        self._require_production()
        capability = operator_id if isinstance(operator_id, AdaptiveOperatorCapability) else None
        shadow_compat = bool(getattr(self, "_shadow_test_only", False))
        if capability is None and not shadow_compat:
            raise AdaptiveWorkflowError("adaptive lifecycle requires a typed operator capability")
        if capability is not None:
            if capability.review_operator[2] != str(OPERATOR_REVIEW_TOPIC_ID):
                raise AdaptiveWorkflowError("adaptive capability review topic is invalid")
            if capability.customer_key != self.customer_key:
                raise AdaptiveWorkflowError("adaptive capability customer is invalid")
            if required_action is not None and capability.action != required_action:
                raise AdaptiveWorkflowError("adaptive capability action is not authorized")
            supplied = self._owner_key(capability.canonical_owner)
            if supplied is None:
                raise AdaptiveWorkflowError("adaptive capability owner is invalid")
            if capability.action not in _ADAPTIVE_ACTIONS:
                raise AdaptiveWorkflowError("adaptive capability action is invalid")
            supplied_version = capability.canonical_owner_version
        else:
            supplied = self._owner_key(operator_id)
            supplied_version = None
        authority = self.authority
        refresh = getattr(authority, "refresh_live_registry", None) if authority is not None else None
        with self._authority_lock():
            if not callable(refresh) or refresh() is not True:
                raise AdaptiveWorkflowError("customer registry is unavailable")
            current_key = self._live_owner_key()
            current_version = self._live_owner_version()
            if supplied is None or supplied != current_key:
                raise AdaptiveWorkflowError("adaptive lifecycle is owner-only")
            if supplied_version is not None and supplied_version != current_version:
                raise AdaptiveWorkflowError("adaptive lifecycle owner authority is stale")
            if capability is not None and not shadow_compat:
                self._validate_capability_session(
                    capability,
                    required_action=required_action,
                    owner=current_key,
                    owner_version=current_version,
                )
            if capability is not None:
                self._last_authenticated_review_operator = capability.review_operator
                self._last_review_operator_version = capability.review_operator_version
            else:
                self._last_authenticated_review_operator = None
                self._last_review_operator_version = 0
            return supplied[0]
    def _live_customer(self) -> tuple[object, Path, object]:
        self._require_production()
        authority = self.authority
        if authority is not None:
            refresh = getattr(authority, "refresh_live_registry", None)
            if not callable(refresh) or refresh() is not True:
                raise AdaptiveWorkflowError("customer registry is unavailable")
            lookup = getattr(authority, "customer", None)
            customer = lookup(self.customer_key) if callable(lookup) else None
        else:
            customer = self.customer_runtime
        spec = getattr(customer, "spec", None)
        if customer is None or spec is None or getattr(spec, "enabled", False) is not True:
            raise AdaptiveWorkflowError("adaptive customer is not enabled")
        data_root = getattr(customer, "data_root", None)
        if not isinstance(data_root, Path):
            raise AdaptiveWorkflowError("registered customer data root is unavailable")
        try:
            resolved_root = data_root.resolve()
            expected_root = (
                self.profile_root / "data" / "customers" / self.customer_key
            ).resolve()
        except (OSError, RuntimeError) as exc:
            raise AdaptiveWorkflowError("registered customer data root is unavailable") from exc
        if (
            data_root.is_symlink()
            or resolved_root != expected_root
            or not resolved_root.exists()
            or not resolved_root.is_dir()
        ):
            raise AdaptiveWorkflowError("registered customer data root is invalid")
        registry_path = self.registry_path
        if registry_path is None and authority is not None:
            candidate = getattr(authority, "_registry_path", None)
            if isinstance(candidate, Path):
                registry_path = candidate
        if registry_path is None:
            for candidate in (
                self.profile_root / "customers" / "registry.json",
                self.profile_root / "registry.json",
            ):
                if candidate.exists():
                    registry_path = candidate.resolve()
                    break
        if registry_path is None:
            raise AdaptiveWorkflowError("committed customer activation is unavailable")
        try:
            from checkin_cli.customer_admin import validate_committed_activation

            validate_committed_activation(
                self.profile_root,
                registry_path,
                self.customer_key,
            )
        except Exception as exc:
            raise AdaptiveWorkflowError(
                "committed customer activation is unavailable"
            ) from exc
        try:
            from checkin_cli.customer_coaching import CONSENT_VERSION
        except (ImportError, AttributeError) as exc:
            raise AdaptiveWorkflowError("customer consent contract is unavailable") from exc
        consent = getattr(spec, "ai_processing_consent", None)
        if (
            getattr(consent, "granted", False) is not True
            or getattr(consent, "recorded_on", None) is None
            or getattr(consent, "notice_version", None) != CONSENT_VERSION
        ):
            raise AdaptiveWorkflowError("customer AI processing consent is unavailable")
        return customer, resolved_root, spec

    @staticmethod
    def _journal_payload(row: Mapping[str, object]) -> Mapping[str, object]:
        payload = row.get("payload")
        return payload if isinstance(payload, Mapping) else row

    @classmethod
    def _journal_rows(
        cls,
        result: Mapping[str, object],
        key: str,
    ) -> tuple[Mapping[str, object], ...]:
        raw = result.get(key)
        if not isinstance(raw, (tuple, list)) or not raw:
            raise AdaptiveWorkflowError(f"adaptive {key} journal is incomplete")
        rows = tuple(row for row in raw if isinstance(row, Mapping))
        if len(rows) != len(raw):
            raise AdaptiveWorkflowError(f"adaptive {key} journal is invalid")
        return rows

    @staticmethod
    def _validate_journal_digest(row: Mapping[str, object]) -> None:
        row_digest = row.get("row_digest")
        if row_digest is None:
            return
        if not isinstance(row_digest, str) or not row_digest:
            raise AdaptiveWorkflowError("adaptive journal row digest is invalid")
        try:
            body = {key: value for key, value in row.items() if key != "row_digest"}
            valid = digest(body) == row_digest
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise AdaptiveWorkflowError("adaptive journal row digest mismatch")

    @classmethod
    def _validate_journal_terminals(
        cls,
        rows: Iterable[Mapping[str, object]],
        label: str,
    ) -> None:
        transitions: dict[str, list[Mapping[str, object]]] = {}
        for row in rows:
            cls._validate_journal_digest(row)
            payload = cls._journal_payload(row)
            state = payload.get("state")
            intent = payload.get("intent_id")
            if state is None:
                continue
            if (
                not isinstance(state, str)
                or state not in {"prepared", "committed", "abandoned"}
                or not isinstance(intent, str)
                or not intent
            ):
                raise AdaptiveWorkflowError(f"adaptive {label} journal transition is invalid")
            prior = transitions.setdefault(intent, [])
            if not prior:
                if state != "prepared":
                    raise AdaptiveWorkflowError(
                        f"adaptive {label} journal terminal row has no prepared row"
                    )
            elif len(prior) != 1 or prior[0].get("state") != "prepared":
                raise AdaptiveWorkflowError(
                    f"adaptive {label} journal has multiple terminal rows"
                )
            elif state not in {"committed", "abandoned"}:
                raise AdaptiveWorkflowError(
                    f"adaptive {label} journal terminal transition is invalid"
                )
            elif payload.get("prepared_digest") != prior[0].get("row_digest"):
                raise AdaptiveWorkflowError(
                    f"adaptive {label} journal prepared digest mismatch"
                )
            prior.append(row)
        if not transitions or any(len(values) == 1 for values in transitions.values()):
            raise AdaptiveWorkflowError(f"adaptive {label} journal is unreconciled")
        if not any(
            values[-1].get("state") == "committed"
            for values in transitions.values()
        ):
            raise AdaptiveWorkflowError(f"adaptive {label} journal has no committed record")

    def _canonical_events(self) -> tuple[object, ...]:
        source = self.canonical_event_source
        if isinstance(source, Mapping):
            source = source.get(self.customer_key)
        else:
            resolver = getattr(source, "events_for", None)
            if callable(resolver):
                source = resolver(self.customer_key)
            else:
                for name in ("for_customer", "store_for"):
                    resolver = getattr(source, name, None)
                    if callable(resolver):
                        source = resolver(self.customer_key)
                        break
        reader = None
        for name in (
            "read_reconciled_events",
            "read_reconciled",
            "reconciled_events",
            "read_canonical_events",
            "_read_events",
        ):
            candidate = getattr(source, name, None)
            if callable(candidate):
                reader = candidate
                break
        if reader is None:
            raise AdaptiveWorkflowError("canonical customer EventStore is unavailable")
        try:
            raw_events = reader()
        except TypeError:
            try:
                raw_events = reader(self.customer_key)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("canonical customer events are unavailable") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise AdaptiveWorkflowError("canonical customer events are unavailable") from exc
        if isinstance(raw_events, Mapping):
            raw_events = raw_events.get("events", raw_events.get("canonical_events"))
        if not isinstance(raw_events, (tuple, list)):
            raise AdaptiveWorkflowError("canonical customer events are unavailable")
        events = tuple(raw_events)
        if any(not callable(getattr(event, "model_dump", None)) for event in events):
            raise AdaptiveWorkflowError("production adaptive events must be canonical Event models")
        snapshot = getattr(self, "_journal_snapshot", None)
        sequence_rows = (
            self._journal_rows(snapshot, "canonical_sequence")
            if isinstance(snapshot, Mapping)
            else ()
        )
        if not sequence_rows:
            raise AdaptiveWorkflowError("adaptive canonical sequence is unavailable")
        ordered = sorted(
            sequence_rows,
            key=lambda row: row.get("append_sequence", row.get("sequence", 0)),
        )
        event_by_id: dict[str, object] = {}
        for event in events:
            try:
                record = event.model_dump(mode="json")
            except TypeError:
                record = event.model_dump()
            if not isinstance(record, Mapping):
                raise AdaptiveWorkflowError("canonical event record is invalid")
            event_id = record.get("event_id")
            if isinstance(event_id, str) and event_id:
                event_by_id[event_id] = event
        selected: list[object] = []
        indices: list[int] = []
        for row in ordered:
            event_id = row.get("canonical_event_id", row.get("event_id"))
            if not isinstance(event_id, str) or not event_id:
                raise AdaptiveWorkflowError("canonical sequence event binding is invalid")
            event = event_by_id.get(event_id)
            index = row.get("event_index")
            if isinstance(index, bool) or not isinstance(index, int):
                index = None
            if event is None and index is not None and 0 <= index < len(events):
                event = events[index]
            if event is None:
                raise AdaptiveWorkflowError("canonical sequence does not match EventStore")
            try:
                record = event.model_dump(mode="json")
            except TypeError:
                record = event.model_dump()
            if not isinstance(record, Mapping) or record.get("event_id") != event_id:
                raise AdaptiveWorkflowError("canonical sequence does not match EventStore")
            expected_digest = row.get("canonical_event_digest", row.get("event_digest"))
            if expected_digest is not None:
                try:
                    accepted_digests = {digest(dict(record))}
                    canonical_event = (
                        event
                        if callable(getattr(event, "model_dump_json", None))
                        else getattr(event, "_event", None)
                    )
                    if callable(getattr(canonical_event, "model_dump_json", None)):
                        canonical_json_line = (
                            canonical_event.model_dump_json(exclude_none=True) + "\n"
                        )
                        accepted_digests.add(
                            hashlib.sha256(canonical_json_line.encode("utf-8")).hexdigest()
                        )
                    compact_record = event.model_dump(mode="json", exclude_none=True)
                    if isinstance(compact_record, Mapping):
                        accepted_digests.add(digest(dict(compact_record)))
                    base = getattr(event, "_event", None)
                    if callable(getattr(base, "model_dump", None)):
                        base_record = base.model_dump(mode="json")
                        if isinstance(base_record, Mapping):
                            accepted_digests.add(digest(dict(base_record)))
                        compact_base = base.model_dump(mode="json", exclude_none=True)
                        if isinstance(compact_base, Mapping):
                            accepted_digests.add(digest(dict(compact_base)))
                except (TypeError, ValueError):
                    raise AdaptiveWorkflowError("canonical sequence event digest is invalid")
                if expected_digest not in accepted_digests:
                    raise AdaptiveWorkflowError("canonical sequence event digest mismatch")
            selected.append(event)
            if index is not None:
                indices.append(index)
        if indices:
            prefix_end = max(indices) + 1
            if prefix_end > len(events):
                raise AdaptiveWorkflowError("canonical sequence prefix is truncated")
            if prefix_end != len(events):
                raise AdaptiveWorkflowError(
                    "adaptive canonical source is stale or extends beyond the reconciled prefix"
                )
            # Preserve any immutable legacy prefix before the reconciled rows.
            return tuple(events[:prefix_end])
        return tuple(selected)
    @staticmethod
    def _feature_config_fields(epoch: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(epoch, Mapping):
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid")
        try:
            value = epoch.get("epoch")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError
            flags = {
                "analytics_shadow": epoch["analytics_shadow"],
                "operator_candidates": epoch["operator_candidates"],
                "activation": epoch["activation"],
                "delivery": epoch["delivery"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid") from exc
        if any(type(flag) is not bool for flag in flags.values()):
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid")
        return {"epoch": value, **flags}

    @classmethod
    def _feature_config_digest(cls, epoch: Mapping[str, object]) -> str:
        fields = cls._feature_config_fields(epoch)
        helper = feature_config_digest
        if callable(helper):
            try:
                value = helper(fields["epoch"], {
                    key: fields[key]
                    for key in (
                        "analytics_shadow",
                        "operator_candidates",
                        "activation",
                        "delivery",
                    )
                })
            except (TypeError, ValueError):
                try:
                    value = helper(fields["epoch"], **{
                        key: fields[key]
                        for key in (
                            "analytics_shadow",
                            "operator_candidates",
                            "activation",
                            "delivery",
                        )
                    })
                except (TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError("adaptive feature config digest is invalid") from exc
            if not isinstance(value, str):
                raise AdaptiveWorkflowError("adaptive feature config digest is invalid")
            return value
        try:
            return digest(fields)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive feature config digest is invalid") from exc

    @classmethod
    def _with_feature_config_digest(cls, epoch: Mapping[str, object]) -> dict[str, object]:
        updated = dict(epoch)
        updated["config_digest"] = cls._feature_config_digest(updated)
        return updated

    def _feature_epoch(self, data_root: Path) -> tuple[Path, dict[str, object]]:
        path = data_root / "nutrition-plans" / "feature-epoch.json"
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise AdaptiveWorkflowError("adaptive feature epoch is unavailable")
        try:
            if path.stat().st_mode & 0o077:
                raise AdaptiveWorkflowError("adaptive feature epoch permissions are invalid")
        except OSError as exc:
            raise AdaptiveWorkflowError("adaptive feature epoch is unavailable") from exc
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid") from exc
        if not isinstance(payload, dict):
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid")
        if set(payload) != {
            "schema_version",
            "epoch",
            "config_digest",
            "analytics_shadow",
            "operator_candidates",
            "activation",
            "delivery",
        }:
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid")
        if payload.get("schema_version") != "1.0":
            raise AdaptiveWorkflowError("adaptive feature epoch version is invalid")
        self._feature_config_fields(payload)
        configured = payload.get("config_digest")
        if (
            not isinstance(configured, str)
            or len(configured) != 64
            or any(character not in "0123456789abcdef" for character in configured)
        ):
            raise AdaptiveWorkflowError("adaptive feature config digest is invalid")
        expected = self._feature_config_digest(payload)
        if not hmac.compare_digest(configured, expected):
            raise AdaptiveWorkflowError("adaptive feature config digest mismatch")
        return path, payload

    @staticmethod
    def _text_digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _authority_digest(self, spec: object) -> str:
        owner_key = self._live_owner_key()
        telegram = getattr(spec, "telegram", None)
        destination = tuple(
            str(getattr(telegram, field, "") or "")
            for field in ("user_id", "chat_id", "topic_id")
        )
        if not all(destination):
            raise AdaptiveWorkflowError("adaptive authority snapshot is unavailable")
        return digest({
            "customer_key": self.customer_key,
            "owner": owner_key,
            "destination": destination,
        })

    @classmethod
    def _read_persisted_journal_rows(
        cls,
        path_value: object,
        label: str,
    ) -> tuple[Mapping[str, object], ...]:
        if not isinstance(path_value, (str, Path)):
            raise AdaptiveWorkflowError(f"adaptive {label} journal is unavailable")
        path = Path(path_value)
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise AdaptiveWorkflowError(f"adaptive {label} journal is unavailable")
        try:
            if path.stat().st_mode & 0o077:
                raise AdaptiveWorkflowError(f"adaptive {label} journal permissions are invalid")
            raw = path.read_bytes()
        except OSError as exc:
            raise AdaptiveWorkflowError(f"adaptive {label} journal is unavailable") from exc
        if not raw or not raw.endswith(b"\n"):
            raise AdaptiveWorkflowError(f"adaptive {label} journal is invalid")
        rows: list[Mapping[str, object]] = []
        for line in raw.splitlines():
            if not line:
                raise AdaptiveWorkflowError(f"adaptive {label} journal is invalid")
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdaptiveWorkflowError(f"adaptive {label} journal is invalid") from exc
            if not isinstance(row, Mapping):
                raise AdaptiveWorkflowError(f"adaptive {label} journal is invalid")
            if not isinstance(row.get("row_digest"), str):
                raise AdaptiveWorkflowError(f"adaptive {label} journal digest is missing")
            cls._validate_journal_digest(row)
            rows.append(row)
        if not rows:
            raise AdaptiveWorkflowError(f"adaptive {label} journal is incomplete")
        return tuple(rows)

    def _validate_production_journals(
        self,
        *,
        allow_prepared_config_epoch: bool = False,
    ) -> Mapping[str, object]:
        path_names = {
            "canonical_sequence": "canonical_sequence_path",
            "source_day": "source_intent_path",
            "source_day_mappings": "source_day_path",
            "authority": "authority_path",
            "config_epoch": "config_epoch_path",
        }
        result: dict[str, object] = {}
        for key, attribute in path_names.items():
            result[key] = self._read_persisted_journal_rows(
                getattr(self.store, attribute, None),
                key,
            )
        journal_rows = {
            key: self._journal_rows(result, key)
            for key in path_names
        }
        for key, rows in journal_rows.items():
            expected_kind = {
                "source_day": "source_day",
                "authority": "authority",
                "config_epoch": "config_epoch",
            }.get(key)
            for row in rows:
                expected_schema = (
                    "canonical_sequence_v1"
                    if key == "canonical_sequence"
                    else "1.0"
                )
                if row.get("schema_version") != expected_schema:
                    raise AdaptiveWorkflowError(f"adaptive {key} journal schema is invalid")
                if expected_kind is not None and row.get("kind") != expected_kind:
                    raise AdaptiveWorkflowError(f"adaptive {key} journal schema is invalid")
        self._validate_journal_terminals(journal_rows["source_day"], "source-day")
        self._validate_journal_terminals(journal_rows["authority"], "authority")
        owned_config_epoch_rows = tuple(
            row
            for row in journal_rows["config_epoch"]
            if isinstance(row.get("customer_keys"), (tuple, list))
            and self.customer_key in tuple(row.get("customer_keys", ()))
        )
        if allow_prepared_config_epoch:
            for row in journal_rows["config_epoch"]:
                self._validate_journal_digest(row)
        else:
            self._validate_journal_terminals(
                owned_config_epoch_rows,
                "config-epoch",
            )
        sequence_rows = journal_rows["canonical_sequence"]
        sequence_numbers = [
            row.get("append_sequence", row.get("sequence"))
            for row in sequence_rows
        ]
        if any(isinstance(number, bool) or not isinstance(number, int) for number in sequence_numbers):
            raise AdaptiveWorkflowError("adaptive canonical sequence is invalid")
        if sequence_numbers != list(range(1, len(sequence_numbers) + 1)):
            raise AdaptiveWorkflowError("adaptive canonical sequence is not contiguous")
        if any(
            not isinstance(row.get("canonical_event_id", row.get("event_id")), str)
            or not row.get("canonical_event_id", row.get("event_id"))
            or not isinstance(row.get("canonical_event_digest", row.get("event_digest")), str)
            or not row.get("canonical_event_digest", row.get("event_digest"))
            for row in sequence_rows
        ):
            raise AdaptiveWorkflowError("adaptive canonical sequence binding is invalid")
        for previous, current in zip(sequence_rows, sequence_rows[1:]):
            previous_digest = previous.get("resulting_prefix_digest")
            current_previous = current.get("previous_prefix_digest")
            if previous_digest is not None and current_previous is not None and previous_digest != current_previous:
                raise AdaptiveWorkflowError("adaptive canonical sequence prefix is forked")
        try:
            self.store.validate_canonical_prefix()
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive canonical sequence binding is invalid") from exc
        try:
            result["adaptive_events"] = tuple(self.store.read())
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive production ledger is invalid") from exc
        overlay_path = getattr(self.store, "overlay_path", None)
        resolver = getattr(self.store, "resolve_overlay", None)
        if isinstance(overlay_path, (str, Path)):
            overlay_file = Path(overlay_path)
            if overlay_file.is_symlink():
                raise AdaptiveWorkflowError("adaptive overlay journal is unavailable")
            if overlay_file.exists():
                if not overlay_file.is_file():
                    raise AdaptiveWorkflowError("adaptive overlay journal is unavailable")
                try:
                    if overlay_file.stat().st_mode & 0o077:
                        raise AdaptiveWorkflowError("adaptive overlay journal permissions are invalid")
                except OSError as exc:
                    raise AdaptiveWorkflowError("adaptive overlay journal is unavailable") from exc
                if not callable(resolver):
                    raise AdaptiveWorkflowError("adaptive overlay journal is unavailable")
                try:
                    resolver(effective_kst=_current_kst_date())
                except (OSError, TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError("adaptive overlay journal is invalid") from exc
        self._journal_snapshot = result
        return result

    def _materialize_proposal(self, proposal: NutritionProposal) -> NutritionProposal:
        self._require_profile_api()
        try:
            operator_body = render_operator_card(proposal)
            customer_body = render_customer_body(proposal)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive proposal body is invalid") from exc
        return replace(
            proposal,
            operator_body=operator_body,
            customer_body=customer_body,
            operator_body_digest=self._text_digest(operator_body),
            customer_body_digest=self._text_digest(customer_body),
            adherence_signal_digest=proposal.snapshot.adherence_digest,
        )

    @staticmethod
    def _decode_target(raw: object) -> MacroTarget | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise AdaptiveWorkflowError("persisted adaptive target is invalid")
        try:
            return MacroTarget(
                calories=int(raw["calories"]),
                carbs_g=int(raw["carbs_g"]),
                protein_g=int(raw["protein_g"]),
                fat_g=int(raw["fat_g"]),
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("persisted adaptive target is invalid") from exc

    @classmethod
    def _decode_meal_plan(cls, raw: object) -> MealPlan | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or MealPlan is None or MealSlot is None:
            raise AdaptiveWorkflowError("persisted adaptive meal plan is invalid")
        try:
            slots = []
            for slot_raw in raw["slots"]:
                if not isinstance(slot_raw, Mapping):
                    raise ValueError("meal slot is invalid")
                slots.append(
                    MealSlot(
                        name=str(slot_raw["name"]),
                        food_ids=tuple(str(value) for value in slot_raw["food_ids"]),
                        calories=int(slot_raw["calories"]),
                        carbs_g=int(slot_raw["carbs_g"]),
                        protein_g=int(slot_raw["protein_g"]),
                        fat_g=int(slot_raw["fat_g"]),
                        quantities=tuple(int(value) for value in slot_raw.get("quantities", ())),
                        serving_grams=tuple(int(value) for value in slot_raw.get("serving_grams", ())),
                        target=cls._decode_target(slot_raw.get("target")),
                    )
                )
            return MealPlan(
                slots=tuple(slots),
                swaps=tuple(str(value) for value in raw.get("swaps", ())),
                fallback=tuple(str(value) for value in raw.get("fallback", ())),
                target=cls._decode_target(raw.get("target")),
                exact=bool(raw.get("exact", True)),
                compiler_version=str(raw.get("compiler_version", "1.0")),
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("persisted adaptive meal plan is invalid") from exc
    @classmethod
    def _decode_weekly_carb_cycle(cls, raw: object) -> WeeklyCarbCycle | None:
        if raw is None:
            return None
        if (
            not isinstance(raw, Mapping)
            or WeeklyCarbCycle is None
            or DailyNutritionTarget is None
        ):
            raise AdaptiveWorkflowError("persisted adaptive weekly carb cycle is invalid")
        try:
            targets = []
            for target_raw in raw["targets"]:
                if not isinstance(target_raw, Mapping):
                    raise ValueError("weekly carb target is invalid")
                target = cls._decode_target(target_raw["target"])
                if target is None:
                    raise ValueError("weekly carb target is invalid")
                targets.append(
                    DailyNutritionTarget(
                        kst_day=date.fromisoformat(str(target_raw["kst_day"])),
                        category=str(target_raw["category"]),
                        target=target,
                    )
                )
            base_target = cls._decode_target(raw["base_target"])
            if base_target is None:
                raise ValueError("weekly carb base target is invalid")
            feasible = raw.get("feasible", True)
            if type(feasible) is not bool:
                raise ValueError("weekly carb feasibility is invalid")
            reason = raw.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("weekly carb reason is invalid")
            version = raw.get("version", "1.0")
            if not isinstance(version, str):
                raise ValueError("weekly carb version is invalid")
            return WeeklyCarbCycle(
                targets=tuple(targets),
                base_target=base_target,
                weekly_calories=int(raw["weekly_calories"]),
                weekly_carbs_g=int(raw["weekly_carbs_g"]),
                weekly_protein_g=int(raw["weekly_protein_g"]),
                weekly_fat_g=int(raw["weekly_fat_g"]),
                feasible=feasible,
                reason=reason,
                version=version,
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError(
                "persisted adaptive weekly carb cycle is invalid"
            ) from exc

    @staticmethod
    def _decode_cooldown(raw: object) -> CooldownResult | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or CooldownResult is None:
            raise AdaptiveWorkflowError("persisted adaptive cooldown is invalid")
        try:
            active = raw["active"]
            if type(active) is not bool:
                raise ValueError("cooldown active flag is invalid")
            reason = raw.get("reason", "")
            if not isinstance(reason, str):
                raise ValueError("cooldown reason is invalid")
            optional_strings = {}
            for field in (
                "anchor_kst",
                "cooldown_until_kst",
                "source_revision_id",
            ):
                value = raw.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError("cooldown metadata is invalid")
                optional_strings[field] = value
            days_remaining = raw.get("days_remaining", 0)
            if (
                isinstance(days_remaining, bool)
                or not isinstance(days_remaining, int)
            ):
                raise ValueError("cooldown remaining days are invalid")
            return CooldownResult(
                active=active,
                reason=reason,
                anchor_kst=optional_strings["anchor_kst"],
                cooldown_until_kst=optional_strings["cooldown_until_kst"],
                days_remaining=days_remaining,
                source_revision_id=optional_strings["source_revision_id"],
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("persisted adaptive cooldown is invalid") from exc

    def _validate_journal_consistency(
        self,
        *,
        customer: object,
        spec: object,
        events: tuple[object, ...],
        artifacts: ApprovedAdaptiveArtifacts,
        epoch: Mapping[str, object],
        registration_binding: _AdaptiveRegistrationBinding | None = None,
    ) -> None:
        snapshot = getattr(self, "_journal_snapshot", None)
        if not isinstance(snapshot, Mapping):
            raise AdaptiveWorkflowError("adaptive production journals are unavailable")
        sequence_rows = self._journal_rows(snapshot, "canonical_sequence")
        sequence_ids = {
            str(row.get("canonical_event_id", row.get("event_id")))
            for row in sequence_rows
        }
        sequence_ids.discard("None")
        mappings = self._journal_rows(snapshot, "source_day_mappings")
        mapped_ids: set[str] = set()
        mapping_ids: set[str] = set()
        for row in mappings:
            payload = self._journal_payload(row)
            customer_key = payload.get("customer_key")
            if customer_key is None or str(customer_key) != self.customer_key:
                raise AdaptiveWorkflowError("adaptive source-day authority mismatch")
            root_event_id = payload.get("root_event_id", payload.get("canonical_event_id"))
            if not isinstance(root_event_id, str) or not root_event_id:
                raise AdaptiveWorkflowError("adaptive source-day mapping is invalid")
            mapping_id = payload.get("mapping_id")
            if (
                not isinstance(mapping_id, str)
                or not mapping_id
                or mapping_id in mapping_ids
                or root_event_id in mapped_ids
            ):
                raise AdaptiveWorkflowError("adaptive source-day mapping identity is invalid")
            mapping_ids.add(mapping_id)
            if root_event_id not in sequence_ids:
                raise AdaptiveWorkflowError("adaptive source-day sequence mismatch")
            mapped_ids.add(root_event_id)
        if mapped_ids != sequence_ids:
            raise AdaptiveWorkflowError("adaptive source-day coverage mismatch")
        event_ids: set[str] = set()
        for event in events:
            if isinstance(event, Mapping):
                raw_event = event
            else:
                dump = getattr(event, "model_dump", None)
                if not callable(dump):
                    raise AdaptiveWorkflowError("adaptive canonical event is invalid")
                raw_event = dump(mode="json")
            event_id = raw_event.get("event_id") if isinstance(raw_event, Mapping) else None
            if not isinstance(event_id, str) or not event_id or event_id in event_ids:
                raise AdaptiveWorkflowError("adaptive canonical event identity is invalid")
            event_ids.add(event_id)
        if event_ids != sequence_ids:
            raise AdaptiveWorkflowError("adaptive canonical sequence coverage mismatch")
        committed_authority = [
            self._journal_payload(row)
            for row in self._journal_rows(snapshot, "authority")
            if self._journal_payload(row).get("state") == "committed"
        ]
        if not committed_authority:
            raise AdaptiveWorkflowError("adaptive authority mirror is incomplete")
        authority_payload = committed_authority[-1]
        authority_customer = authority_payload.get("customer_key")
        if authority_customer is None or str(authority_customer) != self.customer_key:
            raise AdaptiveWorkflowError("adaptive authority mirror customer mismatch")
        owner_key = self._live_owner_key()
        raw_owner = authority_payload.get(
            "owner",
            authority_payload.get("owner_key", authority_payload.get("owner_address")),
        )
        if raw_owner is None and all(
            field in authority_payload
            for field in ("owner_user_id", "owner_chat_id", "owner_topic_id")
        ):
            raw_owner = (
                authority_payload["owner_user_id"],
                authority_payload["owner_chat_id"],
                authority_payload["owner_topic_id"],
            )
        if self._owner_key(raw_owner) != owner_key:
            raise AdaptiveWorkflowError("adaptive authority mirror does not match live owner")
        authority_fields = {
            "schema_version": "1.0",
            "customer_key": self.customer_key,
            "owner": (
                dict(raw_owner)
                if isinstance(raw_owner, Mapping)
                else {
                    "user_id": owner_key[0],
                    "chat_id": owner_key[1],
                    "topic_id": owner_key[2],
                }
            ),
            "registry_digest": authority_payload.get("registry_digest"),
            "activation_receipt_digest": authority_payload.get(
                "activation_receipt_digest"
            ),
            "consent_digest": authority_payload.get("consent_digest"),
        }
        if any(
            not isinstance(authority_fields[field], str)
            or len(authority_fields[field]) != 64
            for field in (
                "registry_digest",
                "activation_receipt_digest",
                "consent_digest",
            )
        ):
            raise AdaptiveWorkflowError("adaptive authority mirror is incomplete")
        authority_digest = digest(authority_fields)
        mirror_digest = authority_payload.get("canonical_fact_digest")
        if not isinstance(mirror_digest, str) or mirror_digest != authority_digest:
            raise AdaptiveWorkflowError("adaptive authority mirror digest mismatch")
        committed_epochs = [
            self._journal_payload(row)
            for row in self._journal_rows(snapshot, "config_epoch")
            if (
                self._journal_payload(row).get("state") == "committed"
                and isinstance(self._journal_payload(row).get("customer_keys"), (tuple, list))
                and self.customer_key
                in tuple(self._journal_payload(row).get("customer_keys", ()))
            )
        ]
        if not committed_epochs:
            raise AdaptiveWorkflowError("adaptive config epoch is incomplete")
        payload = committed_epochs[-1]
        keys = payload.get("customer_keys")
        if (
            not isinstance(keys, (tuple, list))
            or any(
                type(key) is not str
                or not key
                or key != key.strip()
                for key in keys
            )
            or tuple(keys) != tuple(sorted(keys))
        ):
            raise AdaptiveWorkflowError("adaptive config epoch customer mismatch")
        canonical_keys = set(keys)
        if len(keys) != len(canonical_keys) or self.customer_key not in canonical_keys:
            raise AdaptiveWorkflowError("adaptive config epoch customer mismatch")
        authority = self.authority
        registry = getattr(authority, "registry", None) if authority is not None else None
        if registry is None and authority is not None:
            registry = getattr(authority, "_registry", None)
        customers = getattr(registry, "customers", None)
        states = payload.get("customer_state", payload.get("customer_states"))
        if (
            not isinstance(states, Mapping)
            or any(not isinstance(key, str) or not key.strip() for key in states)
            or {key.strip() for key in states} != canonical_keys
            or any(states[key] != "committed" for key in states)
        ):
            raise AdaptiveWorkflowError("adaptive config epoch customer is not committed")
        if not isinstance(customers, (tuple, list)):
            raise AdaptiveWorkflowError("adaptive config epoch live fanout is unavailable")
        enabled_keys = [
            str(getattr(getattr(runtime, "spec", None), "customer_key", "")).strip()
            for runtime in customers
            if getattr(getattr(runtime, "spec", None), "enabled", False) is True
        ]
        if (
            not enabled_keys
            or any(not key for key in enabled_keys)
            or len(enabled_keys) != len(set(enabled_keys))
            or canonical_keys != set(enabled_keys)
        ):
            raise AdaptiveWorkflowError("adaptive config epoch fanout mismatch")
        if payload.get("epoch") != epoch.get("epoch"):
            raise AdaptiveWorkflowError("adaptive config epoch does not match live config")
        live_config_digest = epoch.get("config_digest")
        journal_config_digest = payload.get("config_digest")
        if (
            not isinstance(live_config_digest, str)
            or len(live_config_digest) != 64
            or not isinstance(journal_config_digest, str)
            or journal_config_digest != live_config_digest
        ):
            raise AdaptiveWorkflowError("adaptive config epoch digest mismatch")
        digest_checks = (
            ("policy_digest", getattr(artifacts, "policy_digest", None)),
            (
                "meal_constraints_digest",
                (
                    registration_binding.meal_constraints_digest
                    if registration_binding is not None
                    else getattr(artifacts, "meal_constraints_digest", None)
                ),
            ),
            ("catalog_digest", getattr(artifacts, "catalog_digest", None)),
            (
                "registration_digest",
                registration_binding.registration_digest
                if registration_binding is not None
                else None,
            ),
        )
        if any(
            payload.get(field) is not None and payload.get(field) != expected
            for field, expected in digest_checks
        ):
            raise AdaptiveWorkflowError("adaptive config epoch artifact mismatch")
        if not events:
            raise AdaptiveWorkflowError("adaptive canonical sequence is empty")

    def _ensure_live_adaptive_journals(self) -> None:
        if getattr(self, "_adaptive_store_lock_held", False):
            return
        required_paths = (
            getattr(self.store, "source_day_path", None),
            getattr(self.store, "authority_path", None),
            getattr(self.store, "config_epoch_path", None),
        )
        has_persisted_journals = all(
            isinstance(value, (str, Path))
            and Path(value).is_file()
            and Path(value).stat().st_size > 0
            for value in required_paths
        )
        profile_root = self.profile_root
        has_live_registry = (
            profile_root is not None
            and any(
                candidate.is_file()
                for candidate in (
                    profile_root / "customers" / "registry.json",
                    profile_root / "registry.json",
                )
            )
        )
        if has_persisted_journals and not has_live_registry:
            return
        reconciler = reconcile_adaptive_nutrition_journals
        authority = self.authority
        registry = getattr(authority, "registry", None) if authority is not None else None
        if registry is None and authority is not None:
            registry = getattr(authority, "_registry", None)
        if (
            not callable(reconciler)
            or self.profile_root is None
            or self.canonical_event_source is None
            or registry is None
        ):
            raise AdaptiveWorkflowError("adaptive production journal reconciliation is unavailable")
        try:
            reconciler(
                self.profile_root,
                self.customer_key,
                canonical_events=self.canonical_event_source,
                registry=registry,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive production journal reconciliation failed") from exc

    def _production_context(
        self,
        *,
        require_activation: bool = False,
        require_delivery: bool = False,
    ) -> tuple[
        object,
        Path,
        object,
        tuple[object, ...],
        str,
        ApprovedAdaptiveArtifacts,
        Path,
        dict[str, object],
        _AdaptiveRegistrationBinding,
    ]:
        customer, data_root, spec = self._live_customer()
        self._ensure_live_adaptive_journals()
        self._validate_production_journals()
        events = self._canonical_events()
        try:
            validate_typed_safety(events)
            source_digest = canonical_event_digest(events)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive canonical source is stale or invalid") from exc
        try:
            artifacts = load_approved_adaptive_artifacts(data_root)
        except (ArithmeticError, OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive production artifacts are unavailable") from exc
        registration_binding = self._load_registration_binding(data_root, artifacts)
        epoch_path, epoch = self._feature_epoch(data_root)
        self._validate_journal_consistency(
            customer=customer,
            spec=spec,
            events=events,
            artifacts=artifacts,
            epoch=epoch,
            registration_binding=registration_binding,
        )
        if require_activation and epoch.get("activation") is not True:
            raise AdaptiveWorkflowError("adaptive plan activation is unavailable")
        if require_delivery and epoch.get("delivery") is not True:
            raise AdaptiveWorkflowError("adaptive delivery is disabled")
        return (
            customer,
            data_root,
            spec,
            events,
            source_digest,
            artifacts,
            epoch_path,
            epoch,
            registration_binding,
        )

    @staticmethod
    def _decode_proposal(raw: object) -> NutritionProposal:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("persisted adaptive proposal is invalid") from exc
        if not isinstance(raw, Mapping):
            raise AdaptiveWorkflowError("persisted adaptive proposal is invalid")
        if "registration_digest" in raw and raw.get("registration_digest") is not None:
            registration_digest = raw.get("registration_digest")
            if (
                not isinstance(registration_digest, str)
                or len(registration_digest) != 64
                or any(character not in "0123456789abcdef" for character in registration_digest)
            ):
                raise AdaptiveWorkflowError("persisted adaptive registration pin is invalid")
        snapshot_raw = raw.get("snapshot")
        if not isinstance(snapshot_raw, Mapping):
            raise AdaptiveWorkflowError("persisted adaptive snapshot is invalid")
        try:
            trainer_loads = tuple(
                (date.fromisoformat(str(item[0])), str(item[1]))
                for item in snapshot_raw.get("trainer_loads", ())
            )
            snapshot = TrendSnapshot(
                evaluation_day=date.fromisoformat(str(snapshot_raw["evaluation_day"])),
                d_plus=int(snapshot_raw["d_plus"]),
                current_samples=int(snapshot_raw["current_samples"]),
                prior_samples=int(snapshot_raw["prior_samples"]),
                current_mean_kg=(
                    Decimal(str(snapshot_raw["current_mean_kg"]))
                    if snapshot_raw.get("current_mean_kg") is not None
                    else None
                ),
                prior_mean_kg=(
                    Decimal(str(snapshot_raw["prior_mean_kg"]))
                    if snapshot_raw.get("prior_mean_kg") is not None
                    else None
                ),
                weekly_rate_percent=(
                    Decimal(str(snapshot_raw["weekly_rate_percent"]))
                    if snapshot_raw.get("weekly_rate_percent") is not None
                    else None
                ),
                adherent_days=int(snapshot_raw.get("adherent_days", 0)),
                safety_held=bool(snapshot_raw.get("safety_held", False)),
                trainer_loads=trainer_loads,
                adherence_complete_days=int(snapshot_raw.get("adherence_complete_days", 0)),
                adherence_inadequate_days=int(snapshot_raw.get("adherence_inadequate_days", 0)),
                adherence_missing_days=int(snapshot_raw.get("adherence_missing_days", 0)),
                adherence_contradictory_days=int(snapshot_raw.get("adherence_contradictory_days", 0)),
                adherence_signal_version=str(snapshot_raw.get("adherence_signal_version", "1.0")),
                adherence_digest=(
                    str(snapshot_raw["adherence_digest"])
                    if snapshot_raw.get("adherence_digest") is not None
                    else None
                ),
                trainer_load_version=str(snapshot_raw.get("trainer_load_version", "1.0")),
                trainer_ambiguity=bool(snapshot_raw.get("trainer_ambiguity", False)),
                canonical_projection=bool(snapshot_raw.get("canonical_projection", False)),
            )
            target = AdaptiveNutritionCoordinator._decode_target(raw.get("target"))
            meal_plan = AdaptiveNutritionCoordinator._decode_meal_plan(raw.get("meal_plan"))
            weekly_carb_cycle = AdaptiveNutritionCoordinator._decode_weekly_carb_cycle(
                raw.get("weekly_carb_cycle")
            )
            cooldown = AdaptiveNutritionCoordinator._decode_cooldown(raw.get("cooldown"))
            explanation = raw.get("explanation")
            if explanation is not None and not isinstance(explanation, str):
                raise ValueError("persisted adaptive explanation is invalid")
            carb_days = tuple(
                (date.fromisoformat(str(item[0])), str(item[1]))
                for item in raw.get("carb_days", ())
            )
            goal_mode = raw.get("goal_mode")
            if goal_mode is not None and not isinstance(goal_mode, str):
                raise ValueError("persisted adaptive goal mode is invalid")
            weekly_rate_min = (
                Decimal(str(raw["weekly_rate_min"]))
                if raw.get("weekly_rate_min") is not None
                else None
            )
            weekly_rate_max = (
                Decimal(str(raw["weekly_rate_max"]))
                if raw.get("weekly_rate_max") is not None
                else None
            )
            proposal = NutritionProposal(
                customer_key=str(raw["customer_key"]),
                snapshot=snapshot,
                decision=Decision(str(raw["decision"])),
                reasons=tuple(str(reason) for reason in raw.get("reasons", ())),
                target=target,
                carb_days=carb_days,
                revision=int(raw.get("revision", 1)),
                parent_digest=(
                    str(raw["parent_digest"])
                    if raw.get("parent_digest") is not None
                    else None
                ),
                operator_note=str(raw.get("operator_note", "")),
                source_digest=(
                    str(raw["source_digest"])
                    if raw.get("source_digest") is not None
                    else None
                ),
                policy_digest=(
                    str(raw["policy_digest"])
                    if raw.get("policy_digest") is not None
                    else None
                ),
                meal_constraints_digest=(
                    str(raw["meal_constraints_digest"])
                    if raw.get("meal_constraints_digest") is not None
                    else None
                ),
                catalog_digest=(
                    str(raw["catalog_digest"])
                    if raw.get("catalog_digest") is not None
                    else None
                ),
                meal_plan=meal_plan,
                operator_body=(
                    str(raw["operator_body"])
                    if raw.get("operator_body") is not None
                    else None
                ),
                customer_body=(
                    str(raw["customer_body"])
                    if raw.get("customer_body") is not None
                    else None
                ),
                operator_body_digest=(
                    str(raw["operator_body_digest"])
                    if raw.get("operator_body_digest") is not None
                    else None
                ),
                customer_body_digest=(
                    str(raw["customer_body_digest"])
                    if raw.get("customer_body_digest") is not None
                    else None
                ),
                adherence_signal_digest=(
                    str(raw["adherence_signal_digest"])
                    if raw.get("adherence_signal_digest") is not None
                    else None
                ),
                **{
                    key: value
                    for key, value in (
                        ("weekly_carb_cycle", weekly_carb_cycle),
                        ("cooldown", cooldown),
                        ("explanation", explanation),
                        ("goal_mode", goal_mode),
                        ("weekly_rate_min", weekly_rate_min),
                        ("weekly_rate_max", weekly_rate_max),
                    )
                    if isinstance(
                        getattr(NutritionProposal, "__dataclass_fields__", {}),
                        Mapping,
                    )
                    and key in NutritionProposal.__dataclass_fields__
                },
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("persisted adaptive proposal is invalid") from exc
        return proposal
    @classmethod
    def _decode_proposal_with_pin(
        cls,
        raw: object,
    ) -> tuple[NutritionProposal, str | None]:
        source = raw
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except (TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("persisted adaptive proposal is invalid") from exc
        if not isinstance(source, Mapping):
            raise AdaptiveWorkflowError("persisted adaptive proposal is invalid")
        registration_digest = source.get("registration_digest")
        if registration_digest is not None:
            cls._registration_digest(registration_digest, "registration digest")
        return cls._decode_proposal(source), registration_digest  # type: ignore[return-value]

    def _remember_registration_pin(
        self,
        proposal: NutritionProposal,
        registration_digest: object,
        *,
        required: bool = True,
    ) -> str | None:
        if not isinstance(proposal, NutritionProposal):
            raise AdaptiveWorkflowError("adaptive proposal is invalid")
        if registration_digest is None and not required:
            return None
        pin = self._registration_digest(registration_digest, "registration digest")
        self._registration_pins[proposal.digest] = pin
        return pin

    def _registration_pin(
        self,
        proposal: NutritionProposal,
        *,
        required: bool = False,
    ) -> str | None:
        pin = self._registration_pins.get(proposal.digest)
        if pin is None:
            try:
                rows = self.store.read()
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive event store is unreadable") from exc
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                payload = row.get("payload")
                if (
                    row.get("event_type") not in {"plan_proposed", "plan_edited"}
                    or not isinstance(payload, Mapping)
                    or payload.get("proposal_digest") != proposal.digest
                ):
                    continue
                candidate, encoded_pin = self._decode_proposal_with_pin(
                    payload.get("proposal")
                )
                if candidate.digest != proposal.digest:
                    raise AdaptiveWorkflowError("persisted adaptive proposal digest is invalid")
                top_pin = payload.get("registration_digest")
                if encoded_pin != top_pin:
                    raise AdaptiveWorkflowError("adaptive registration pin is stale")
                if encoded_pin is not None:
                    pin = self._remember_registration_pin(
                        proposal,
                        encoded_pin,
                    )
                    break
        if required and pin is None:
            raise AdaptiveWorkflowError("adaptive registration pin is unavailable")
        return pin

    def _latest_production_proposal(self) -> NutritionProposal:
        try:
            rows = self.store.read()
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive event store is unreadable") from exc
        latest: NutritionProposal | None = None
        for row in rows:
            if not isinstance(row, Mapping) or row.get("event_type") not in {
                "plan_proposed",
                "plan_edited",
            }:
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping) or payload.get("customer_key") != self.customer_key:
                continue
            if payload.get("execution_mode") != "production":
                continue
            proposal, encoded_pin = self._decode_proposal_with_pin(payload.get("proposal"))
            if payload.get("registration_digest") != encoded_pin or encoded_pin is None:
                raise AdaptiveWorkflowError("adaptive registration pin is stale")
            self._remember_registration_pin(proposal, encoded_pin)
            if payload.get("proposal_digest") != proposal.digest:
                raise AdaptiveWorkflowError("persisted adaptive proposal digest is invalid")
            if latest is not None:
                if (
                    proposal.revision != latest.revision + 1
                    or proposal.parent_digest != latest.digest
                ):
                    raise AdaptiveWorkflowError("adaptive proposal revision chain is invalid")
            elif proposal.revision != 1 or proposal.parent_digest is not None:
                raise AdaptiveWorkflowError("adaptive proposal revision chain is invalid")
            latest = proposal
        if latest is None:
            raise AdaptiveWorkflowError("no production adaptive proposal is available")
        return latest
    def _proposal_for_digest(self, proposal_digest: object) -> NutritionProposal:
        """Decode the latest persisted proposal for a shadow callback transition."""
        if not isinstance(proposal_digest, str) or not proposal_digest:
            raise AdaptiveWorkflowError("adaptive callback proposal is invalid")
        try:
            rows = self.store.read()
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive event store is unreadable") from exc
        proposal: NutritionProposal | None = None
        for row in rows:
            if not isinstance(row, Mapping) or row.get("event_type") not in {
                "plan_proposed",
                "plan_edited",
            }:
                continue
            payload = row.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("customer_key", self.customer_key) != self.customer_key
                or payload.get("proposal_digest") != proposal_digest
            ):
                continue
            if self._production_mode and payload.get("execution_mode") != "production":
                continue
            candidate, encoded_pin = self._decode_proposal_with_pin(payload.get("proposal"))
            if candidate.digest != proposal_digest:
                raise AdaptiveWorkflowError("persisted adaptive proposal digest is invalid")
            top_pin = payload.get("registration_digest")
            if self._production_mode:
                if encoded_pin != top_pin or encoded_pin is None:
                    raise AdaptiveWorkflowError("adaptive registration pin is stale")
                self._remember_registration_pin(candidate, encoded_pin)
            elif encoded_pin is not None and encoded_pin != top_pin:
                raise AdaptiveWorkflowError("adaptive registration pin is stale")
            proposal = candidate
        if proposal is None:
            raise AdaptiveWorkflowError("adaptive callback proposal is unavailable")
        return proposal

    @staticmethod
    def _proposal_pins(proposal: NutritionProposal) -> tuple[str, str, str, str]:
        values = (
            proposal.source_digest,
            proposal.policy_digest,
            proposal.meal_constraints_digest,
            proposal.catalog_digest,
        )
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise AdaptiveWorkflowError("adaptive proposal evidence pins are incomplete")
        return values  # type: ignore[return-value]

    def _proposal_body_pins(
        self,
        proposal: NutritionProposal,
        spec: object,
    ) -> Mapping[str, str]:
        body_values = (
            proposal.operator_body,
            proposal.customer_body,
            proposal.operator_body_digest,
            proposal.customer_body_digest,
        )
        if (
            not isinstance(proposal.operator_body, str)
            or not isinstance(proposal.customer_body, str)
            or not proposal.customer_body.strip()
            or not all(
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
                for value in body_values[2:]
            )
            or self._text_digest(proposal.operator_body) != proposal.operator_body_digest
            or self._text_digest(proposal.customer_body) != proposal.customer_body_digest
        ):
            raise AdaptiveWorkflowError("adaptive proposal body pins are incomplete")
        if not callable(validate_explanation):
            raise AdaptiveWorkflowError("adaptive explanation validator is unavailable")
        validated_explanation = validate_explanation(proposal.explanation, proposal)
        if validated_explanation != proposal.explanation:
            raise AdaptiveWorkflowError("adaptive proposal explanation is invalid")
        expected_operator_body = render_operator_card(proposal)
        expected_customer_body = render_customer_body(proposal)
        if (
            proposal.operator_body != expected_operator_body
            or proposal.customer_body != expected_customer_body
        ):
            raise AdaptiveWorkflowError("adaptive proposal body is not canonical")
        meal_digest = proposal.meal_plan.digest if proposal.meal_plan is not None else ""
        return {
            "operator_body_digest": proposal.operator_body_digest,
            "customer_body_digest": proposal.customer_body_digest,
            "meal_plan_digest": meal_digest,
            "meal_digest": meal_digest,
            "authority_digest": self._authority_digest(spec),
        }

    def _risk_policy_evidence(self) -> dict[str, str]:
        """Return the current sealed risk-policy pins for a lifecycle mutation."""
        if not self._production_mode or not callable(load_verified_dual_coach_risk_policy):
            raise AdaptiveWorkflowError("adaptive risk policy loader is unavailable")
        customer, _data_root, _spec = self._live_customer()
        try:
            policy = load_verified_dual_coach_risk_policy(customer)
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive risk policy is unavailable") from exc
        return {
            "risk_policy_version": policy.version,
            "risk_policy_digest": policy.policy_digest,
            "risk_policy_document_digest": policy.document_digest,
        }
    def _latest_committed_delivery_enable_locked(self) -> Mapping[str, object] | None:
        """Return this customer's most recent durable delivery-enable approval."""
        path_value = getattr(self.store, "config_epoch_path", None)
        if not isinstance(path_value, (str, Path)):
            raise AdaptiveWorkflowError("adaptive config epoch journal is unavailable")
        latest: Mapping[str, object] | None = None
        for row in self._read_config_epoch_rows(Path(path_value)):
            if (
                row.get("state") == "committed"
                and row.get("delivery") is True
                and self.customer_key in tuple(row.get("customer_keys", ()))
            ):
                latest = row
        return latest


    def _approval_payload(
        self,
        proposal: NutritionProposal,
        spec: object,
        operator_id: str,
    ) -> dict[str, object]:
        source_digest, policy_digest, constraints_digest, catalog_digest = self._proposal_pins(proposal)
        pins = self._proposal_body_pins(proposal, spec)
        registration_digest = self._registration_pin(
            proposal,
            required=self._production_mode,
        )
        audit_fields = (
            self._operator_audit_fields()
            if self._production_mode
            else {
                "authenticated_review_operator": None,
                "review_operator_version": 0,
                "canonical_owner_snapshot": None,
                "canonical_owner_version": 0,
            }
        )
        risk_policy = self._risk_policy_evidence() if self._production_mode else {}
        return {
            "customer_key": self.customer_key,
            "proposal_digest": proposal.digest,
            "revision": proposal.revision,
            "operator_id": operator_id,
            "operator_address": (
                list(self._live_owner_key()) if self._production_mode else None
            ),
            **audit_fields,
            "topic_id": self.operator_topic_id,
            "source_digest": source_digest,
            "policy_digest": policy_digest,
            "meal_constraints_digest": constraints_digest,
            "catalog_digest": catalog_digest,
            "registration_digest": registration_digest,
            "operator_body_digest": pins["operator_body_digest"],
            "customer_body_digest": pins["customer_body_digest"],
            "meal_plan_digest": pins["meal_plan_digest"],
            "meal_digest": pins["meal_digest"],
            "authority_digest": pins["authority_digest"],
            "customer_body": proposal.customer_body,
            "execution_mode": "production",
            **risk_policy,
        }

    @staticmethod
    def _matching_event(
        rows: Iterable[object],
        event_type: str,
        proposal_digest: str,
    ) -> Mapping[str, object] | None:
        latest: Mapping[str, object] | None = None
        for row in rows:
            if (
                isinstance(row, Mapping)
                and row.get("event_type") == event_type
                and isinstance(row.get("payload"), Mapping)
                and row["payload"].get("proposal_digest") == proposal_digest
            ):
                latest = row
        return latest

    @staticmethod
    def _has_event(rows: Iterable[object], event_type: str, proposal_digest: str) -> bool:
        return AdaptiveNutritionCoordinator._matching_event(rows, event_type, proposal_digest) is not None
    @staticmethod
    def _overlay_mapping(overlay: object) -> Mapping[str, object] | None:
        """Return the canonical, JSON-safe identity of one overlay."""
        if overlay is None:
            return None
        if isinstance(overlay, Mapping):
            raw = dict(overlay)
        else:
            raw = {
                key: getattr(overlay, key, None)
                for key in (
                    "revision_id",
                    "proposal_digest",
                    "effective_from",
                    "effective_through",
                    "supersedes_revision_id",
                    "authority_snapshot_id",
                    "state",
                )
            }
        if not raw.get("revision_id"):
            raise AdaptiveWorkflowError("adaptive overlay identity is invalid")
        for key in ("effective_from", "effective_through"):
            value = raw.get(key)
            if isinstance(value, (date, datetime)):
                raw[key] = value.isoformat()
        return raw
    @staticmethod
    def _overlay_same_identity(expected: object, actual: object) -> bool:
        expected_map = AdaptiveNutritionCoordinator._overlay_mapping(expected)
        actual_map = AdaptiveNutritionCoordinator._overlay_mapping(actual)
        if expected_map is None or actual_map is None:
            return expected_map is None and actual_map is None
        return (
            expected_map.get("revision_id") == actual_map.get("revision_id")
            and expected_map.get("proposal_digest") == actual_map.get("proposal_digest")
            and expected_map.get("state", "effective")
            == actual_map.get("state", "effective")
        )
    @staticmethod
    def _overlay_strict_identity(expected: object, actual: object) -> bool:
        expected_map = AdaptiveNutritionCoordinator._overlay_mapping(expected)
        actual_map = AdaptiveNutritionCoordinator._overlay_mapping(actual)
        if expected_map is None or actual_map is None:
            return expected_map is None and actual_map is None
        fields = (
            "revision_id",
            "proposal_digest",
            "effective_from",
            "effective_through",
            "supersedes_revision_id",
            "authority_snapshot_id",
            "state",
        )

        def normalize(field: str, value: object) -> object:
            if field not in {"effective_from", "effective_through"}:
                return value
            if value is None:
                return None
            try:
                parsed = (
                    datetime.fromisoformat(value)
                    if isinstance(value, str) and "T" in value
                    else date.fromisoformat(value)
                    if isinstance(value, str)
                    else value
                )
            except (TypeError, ValueError):
                return value
            if isinstance(parsed, datetime):
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_KST)
                else:
                    parsed = parsed.astimezone(_KST)
                return parsed.isoformat()
            if isinstance(parsed, date):
                return f"{parsed.isoformat()}T00:00:00+09:00"
            return value

        return all(
            normalize(field, expected_map.get(field, "effective" if field == "state" else None))
            == normalize(field, actual_map.get(field, "effective" if field == "state" else None))
            for field in fields
        )
    @classmethod
    def _overlay_at_effective_identity(
        cls,
        overlay: object,
        effective: object,
    ) -> Mapping[str, object] | None:
        identity = cls._overlay_mapping(overlay)
        if identity is None:
            return None
        adjusted = dict(identity)
        adjusted["effective_from"] = effective
        adjusted["effective_through"] = None
        adjusted["state"] = "effective"
        return adjusted

    @staticmethod
    def _same_json_mapping(expected: object, actual: object) -> bool:
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            return expected is None and actual is None
        try:
            return canonical_json(dict(expected)) == canonical_json(dict(actual))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _transition_payload(row: object) -> Mapping[str, object] | None:
        if not isinstance(row, Mapping):
            return None
        payload = row.get("payload")
        return payload if isinstance(payload, Mapping) else None

    @classmethod
    def _transition_rows(
        cls,
        rows: Iterable[object],
        *,
        event_type: str,
        transaction_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(
            row
            for row in rows
            if isinstance(row, Mapping)
            and row.get("event_type") == event_type
            and cls._transition_payload(row) is not None
            and cls._transition_payload(row).get("transaction_id") == transaction_id
        )

    @classmethod
    def _committed_transition(
        cls,
        rows: Iterable[object],
        *,
        action: str,
        proposal_digest: str,
    ) -> Mapping[str, object] | None:
        aliases = {"activate": "activation", "rollback": "rollback"}
        latest: Mapping[str, object] | None = None
        for row in rows:
            payload = cls._transition_payload(row)
            if (
                isinstance(row, Mapping)
                and row.get("event_type") == "transition_committed"
                and payload is not None
                and payload.get("action") in {action, aliases.get(action)}
                and payload.get("proposal_digest", payload.get("revision_id")) == proposal_digest
            ):
                latest = row
        return latest

    def _raw_overlay(self, effective_kst: object, *, as_of_sequence: int | None = None) -> object | None:
        resolver = getattr(self.store, "resolve_overlay", None)
        if not callable(resolver):
            raise AdaptiveWorkflowError("adaptive overlay lifecycle is unavailable")
        try:
            try:
                return resolver(effective_kst=effective_kst, as_of_sequence=as_of_sequence)
            except TypeError:
                return resolver(effective_kst=effective_kst)
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive overlay journal is invalid") from exc

    def _overlay_is_committed(
        self,
        overlay: object,
        rows: Iterable[object],
    ) -> bool:
        identity = self._overlay_mapping(overlay)
        if identity is None:
            return False
        proposal_digest = identity.get("proposal_digest")
        if not isinstance(proposal_digest, str) or not proposal_digest:
            return False
        return self._committed_transition(
            rows,
            action="activate",
            proposal_digest=proposal_digest,
        ) is not None

    def _overlay_sequence_limit(self) -> int | None:
        path_value = getattr(self.store, "overlay_path", None)
        if not isinstance(path_value, (str, Path)):
            return None
        path = Path(path_value)
        if not path.exists() or not path.is_file():
            return None
        try:
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive overlay journal is invalid") from exc
        sequences = [
            row.get("append_sequence")
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("append_sequence"), int)
        ]
        return max(sequences, default=0)

    def _overlay_at_identity(
        self,
        effective_kst: object,
        expected: object,
        *,
        as_of_sequence: int | None,
    ) -> object | None:
        expected_identity = self._overlay_mapping(expected)
        if expected_identity is None:
            return None
        limit = as_of_sequence
        if limit is None:
            limit = self._overlay_sequence_limit()
        if limit is None or limit < 1:
            current = self._raw_overlay(effective_kst, as_of_sequence=as_of_sequence)
            return (
                current
                if current is not None and self._overlay_same_identity(expected, current)
                else None
            )
        for sequence in range(limit, 0, -1):
            current = self._raw_overlay(effective_kst, as_of_sequence=sequence)
            if current is None:
                continue
            if self._overlay_same_identity(expected_identity, current):
                return current
        return None

    def _committed_overlay(
        self,
        effective_kst: object,
        *,
        as_of_sequence: int | None = None,
    ) -> object | None:
        """Resolve only the newest overlay backed by a committed lifecycle row."""
        try:
            rows = list(self.store.read())
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive event store is unreadable") from exc
        prepared_by_tx: dict[str, Mapping[str, object]] = {}
        terminal_by_tx: dict[str, str] = {}
        for row in rows:
            payload = self._transition_payload(row)
            if payload is None or payload.get("customer_key", self.customer_key) != self.customer_key:
                continue
            event_type = row.get("event_type") if isinstance(row, Mapping) else None
            if event_type not in {
                "transition_prepared",
                "transition_committed",
                "transition_aborted",
            }:
                continue
            transaction_id = payload.get("transaction_id")
            if not isinstance(transaction_id, str) or not transaction_id:
                raise AdaptiveWorkflowError("adaptive lifecycle transaction is invalid")
            if event_type == "transition_prepared":
                prepared_by_tx[transaction_id] = payload
            elif event_type in {"transition_committed", "transition_aborted"}:
                state = "committed" if event_type == "transition_committed" else "aborted"
                prior = terminal_by_tx.get(transaction_id)
                if prior is not None and prior != state:
                    raise AdaptiveWorkflowError("adaptive lifecycle transaction has conflicting terminals")
                terminal_by_tx[transaction_id] = state
        for transaction_id, state in terminal_by_tx.items():
            if state == "committed" and transaction_id not in prepared_by_tx:
                raise AdaptiveWorkflowError("adaptive lifecycle transaction is incomplete")
        candidates: list[tuple[int, Mapping[str, object], Mapping[str, object] | None]] = []
        for index, row in enumerate(rows):
            payload = self._transition_payload(row)
            if payload is None or payload.get("customer_key", self.customer_key) != self.customer_key:
                continue
            if row.get("event_type") != "transition_committed":
                continue
            transaction_id = payload.get("transaction_id")
            if not isinstance(transaction_id, str) or terminal_by_tx.get(transaction_id) != "committed":
                continue
            prepared = prepared_by_tx.get(transaction_id)
            candidates.append((index, payload, prepared))
        latest_prepared: tuple[int, Mapping[str, object]] | None = None
        for index, row in enumerate(rows):
            payload = self._transition_payload(row)
            if (
                isinstance(payload, Mapping)
                and row.get("event_type") == "transition_prepared"
                and payload.get("customer_key", self.customer_key) == self.customer_key
            ):
                transaction_id = payload.get("transaction_id")
                if isinstance(transaction_id, str) and terminal_by_tx.get(transaction_id) is None:
                    latest_prepared = (index, payload)
        expected: Mapping[str, object] | None = None
        if latest_prepared is not None:
            _index, payload = latest_prepared
            if payload.get("action") == "activate":
                expected = payload.get("prior_overlay")
        if expected is None and candidates:
            _index, commit, prepared = max(candidates, key=lambda item: item[0])
            action = commit.get("action")
            if action in {"activate", "activation"}:
                expected = commit.get("new_overlay")
                if expected is None and prepared is not None:
                    expected = prepared.get("new_overlay")
            elif action == "rollback":
                expected = prepared.get("restore_overlay") if prepared is not None else None
        if expected is None:
            return None
        return self._overlay_at_identity(
            effective_kst,
            expected,
            as_of_sequence=as_of_sequence,
        )

    @staticmethod
    def _epoch_digest(epoch: Mapping[str, object]) -> str:
        try:
            return digest(dict(epoch))
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid") from exc

    @staticmethod
    def _transaction_id(action: str, proposal_digest: str, epoch_digest: str) -> str:
        return hashlib.sha256(
            f"adaptive-transition\0{action}\0{proposal_digest}\0{epoch_digest}".encode("utf-8")
        ).hexdigest()
    @classmethod
    def _fresh_transaction_id(
        cls,
        action: str,
        proposal_digest: str,
        epoch_digest: str,
        rows: Iterable[object],
    ) -> str:
        base = cls._transaction_id(action, proposal_digest, epoch_digest)
        existing = {
            payload.get("transaction_id")
            for row in rows
            if isinstance((payload := cls._transition_payload(row)), Mapping)
            and isinstance(payload.get("transaction_id"), str)
        }
        if base not in existing:
            return base
        suffix = 1
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def _append_transition_locked(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        dedupe_key: str,
    ) -> Mapping[str, object]:
        if event_type not in {
            "transition_prepared",
            "transition_committed",
            "transition_aborted",
        }:
            raise AdaptiveWorkflowError("adaptive lifecycle transaction is invalid")
        return self._append_locked(
            self.store,
            event_type,
            payload,
            dedupe_key=dedupe_key,
        )

    def _receipt_for_transaction(
        self,
        rows: Iterable[object],
        *,
        event_type: str,
        transaction_id: str,
    ) -> Mapping[str, object] | None:
        for row in rows:
            payload = self._transition_payload(row)
            if (
                isinstance(row, Mapping)
                and row.get("event_type") == event_type
                and payload is not None
                and payload.get("transaction_id") == transaction_id
            ):
                return row
        return None

    def _validate_recovery_payload(self, payload: Mapping[str, object]) -> None:
        """Require the prepared transition to still match live production facts."""
        try:
            if payload.get("customer_key") != self.customer_key:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            action = payload.get("action")
            proposal_digest = payload.get("proposal_digest")
            if action not in {"activate", "rollback"} or not isinstance(proposal_digest, str):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            proposal = self._proposal_for_digest(proposal_digest)
            if proposal.digest != proposal_digest:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            revision = payload.get("revision")
            if revision is not None and proposal.revision != revision:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            if action == "rollback" and payload.get("revision_id") != proposal_digest:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            _customer, data_root, spec = self._live_customer()
            operator_address = self._owner_key(payload.get("operator_address"))
            if operator_address != self._live_owner_key():
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            self._validate_production_journals(allow_prepared_config_epoch=True)
            events = self._canonical_events()
            current_source_digest = canonical_event_digest(events)
            artifacts = load_approved_adaptive_artifacts(data_root)
            registration_binding = self._load_registration_binding(data_root, artifacts)
            current_pins = {
                "source_digest": current_source_digest,
                "policy_digest": artifacts.policy_digest,
                "meal_constraints_digest": registration_binding.meal_constraints_digest,
                "catalog_digest": artifacts.catalog_digest,
                "registration_digest": registration_binding.registration_digest,
                "authority_digest": self._authority_digest(spec),
            }
            for field, expected in current_pins.items():
                if payload.get(field) != expected:
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            proposal_pins = {
                "source_digest": proposal.source_digest,
                "policy_digest": proposal.policy_digest,
                "meal_constraints_digest": proposal.meal_constraints_digest,
                "catalog_digest": proposal.catalog_digest,
                "registration_digest": self._registration_pin(
                    proposal,
                    required=True,
                ),
            }
            for field, expected in proposal_pins.items():
                if payload.get(field) != expected:
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            if action == "activate":
                new_overlay = self._overlay_mapping(payload.get("new_overlay"))
                if (
                    new_overlay is None
                    or new_overlay.get("revision_id") != proposal_digest
                    or new_overlay.get("proposal_digest") != proposal_digest
                ):
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            else:
                prior_overlay = self._overlay_mapping(payload.get("prior_overlay"))
                if (
                    prior_overlay is None
                    or prior_overlay.get("revision_id") != proposal_digest
                ):
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        except (AdaptiveWorkflowError, OSError, TypeError, ValueError, AttributeError, KeyError, IndexError) as exc:
            if isinstance(exc, AdaptiveWorkflowError) and str(exc) == "adaptive lifecycle recovery is required":
                raise
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required") from exc
    def _activation_state(
        self,
        payload: Mapping[str, object],
    ) -> tuple[bool, bool, object | None, Mapping[str, object]]:
        effective = payload.get("effective_kst")
        current_overlay = self._raw_overlay(effective)
        new_overlay = payload.get("new_overlay")
        overlay_ok = self._overlay_strict_identity(new_overlay, current_overlay)
        epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
        _ = epoch_path
        new_epoch = payload.get("new_epoch")
        epoch_ok = self._same_json_mapping(new_epoch, current_epoch)
        return overlay_ok, epoch_ok, current_overlay, current_epoch

    def _restore_activation_prior(self, payload: Mapping[str, object]) -> None:
        effective = payload.get("effective_kst")
        current_overlay = self._raw_overlay(effective)
        new_overlay = payload.get("new_overlay")
        prior_overlay = payload.get("prior_overlay")
        prior_expected = self._overlay_at_effective_identity(
            prior_overlay,
            effective,
        )
        if self._overlay_strict_identity(new_overlay, current_overlay):
            new_revision = self._overlay_mapping(new_overlay).get("revision_id")
            rollback = getattr(self.store, "rollback_overlay", None)
            if not callable(rollback):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            try:
                rollback(
                    str(new_revision),
                    as_of_kst=effective,
                    reason=f"abort:{payload.get('transaction_id', '')}",
                )
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required") from exc
            current_overlay = self._raw_overlay(effective)
        if not self._overlay_strict_identity(prior_expected, current_overlay):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
        prior_epoch = payload.get("prior_epoch")
        new_epoch = payload.get("new_epoch")
        if self._same_json_mapping(prior_epoch, current_epoch):
            return
        if not self._same_json_mapping(new_epoch, current_epoch):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        if not isinstance(prior_epoch, Mapping):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        self._write_feature_epoch(epoch_path, prior_epoch)
        _path, restored = self._feature_epoch(epoch_path)
        if not self._same_json_mapping(prior_epoch, restored):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")

    def _recover_activation_locked(
        self,
        prepared: Mapping[str, object],
        rows: list[Mapping[str, object]],
    ) -> Mapping[str, object]:
        payload = self._transition_payload(prepared)
        if payload is None:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        transaction_id = payload.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        committed = self._transition_rows(
            rows,
            event_type="transition_committed",
            transaction_id=transaction_id,
        )
        aborted = self._transition_rows(
            rows,
            event_type="transition_aborted",
            transaction_id=transaction_id,
        )
        if committed and aborted:
            raise AdaptiveWorkflowError("adaptive lifecycle transaction has conflicting terminals")
        if committed or aborted:
            return committed[-1] if committed else aborted[-1]
        self._validate_recovery_payload(payload)
        overlay_ok, epoch_ok, _current_overlay, _current_epoch = self._activation_state(payload)
        prior_expected = self._overlay_at_effective_identity(
            payload.get("prior_overlay"),
            payload.get("effective_kst"),
        )
        prior_overlay_ok = self._overlay_strict_identity(
            prior_expected,
            _current_overlay,
        )
        if any(
            payload.get(key) != value
            for key, value in self._risk_policy_evidence().items()
        ):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        prior_epoch_ok = self._same_json_mapping(
            payload.get("prior_epoch"),
            _current_epoch,
        )
        if overlay_ok and epoch_ok:
            self._reconcile_config_epoch_transition_locked(payload, complete=True)
        elif prior_overlay_ok and prior_epoch_ok:
            self._reconcile_config_epoch_transition_locked(payload, complete=False)
        else:
            self._reconcile_config_epoch_transition_locked(payload, complete=False)
            self._restore_activation_prior(payload)
        receipt_type = "adaptive_plan_activated"
        receipt = self._receipt_for_transaction(
            rows,
            event_type=receipt_type,
            transaction_id=transaction_id,
        )
        overlay_ok, epoch_ok, _current_overlay, _current_epoch = self._activation_state(payload)
        if receipt is not None and not (overlay_ok and epoch_ok):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        if overlay_ok and epoch_ok:
            if receipt is None:
                receipt_payload = payload.get("receipt_payload")
                if not isinstance(receipt_payload, Mapping):
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
                receipt = self._append_locked(
                    self.store,
                    receipt_type,
                    receipt_payload,
                    dedupe_key=str(payload["receipt_dedupe_key"]),
                )
                rows.append(receipt)
            committed_payload = {
                "transaction_id": transaction_id,
                "operator_address": payload.get("operator_address"),
                "new_overlay": payload.get("new_overlay"),
                "action": "activate",
                "state": "committed",
                "proposal_digest": payload.get("proposal_digest"),
                "registration_digest": payload.get("registration_digest"),
                "source_digest": payload.get("source_digest"),
                "policy_digest": payload.get("policy_digest"),
                "meal_constraints_digest": payload.get("meal_constraints_digest"),
                "catalog_digest": payload.get("catalog_digest"),
                "operator_body_digest": payload.get("operator_body_digest"),
                "customer_body_digest": payload.get("customer_body_digest"),
                "authority_digest": payload.get("authority_digest"),
                "revision": payload.get("revision"),
                "prepared_digest": prepared.get("event_id"),
                "receipt_event_id": receipt.get("event_id"),
            }
            return self._append_transition_locked(
                "transition_committed",
                committed_payload,
                dedupe_key=f"adaptive-transition-commit:{transaction_id}",
            )
        self._restore_activation_prior(payload)
        aborted_payload = {
            "transaction_id": transaction_id,
            "action": "activate",
            "state": "aborted",
            "proposal_digest": payload.get("proposal_digest"),
            "registration_digest": payload.get("registration_digest"),
            "source_digest": payload.get("source_digest"),
            "policy_digest": payload.get("policy_digest"),
            "meal_constraints_digest": payload.get("meal_constraints_digest"),
            "catalog_digest": payload.get("catalog_digest"),
            "operator_body_digest": payload.get("operator_body_digest"),
            "customer_body_digest": payload.get("customer_body_digest"),
            "authority_digest": payload.get("authority_digest"),
            "revision": payload.get("revision"),
            "prepared_digest": prepared.get("event_id"),
            "reason": "incomplete_state",
        }
        return self._append_transition_locked(
            "transition_aborted",
            aborted_payload,
            dedupe_key=f"adaptive-transition-abort:{transaction_id}",
        )

    def _rollback_state(
        self,
        payload: Mapping[str, object],
    ) -> tuple[bool, bool, object | None]:
        effective = payload.get("effective_kst")
        current_overlay = self._raw_overlay(effective)
        restore_overlay = payload.get("restore_overlay")
        restore_expected = self._overlay_at_effective_identity(
            restore_overlay,
            effective,
        )
        overlay_ok = self._overlay_strict_identity(restore_expected, current_overlay)
        prior_overlay = self._overlay_mapping(payload.get("prior_overlay"))
        restore_identity = self._overlay_mapping(restore_overlay)
        prior_identity = self._overlay_mapping(prior_overlay)
        if (
            restore_identity is not None
            and prior_identity is not None
            and prior_identity.get("supersedes_revision_id") not in {
                None,
                restore_identity.get("revision_id"),
            }
        ):
            overlay_ok = False
        epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
        _ = epoch_path
        epoch_ok = self._same_json_mapping(payload.get("new_epoch"), current_epoch)
        return overlay_ok, epoch_ok, current_overlay

    def _restore_rollback_prior(self, payload: Mapping[str, object]) -> None:
        effective = payload.get("effective_kst")
        prior_overlay = payload.get("prior_overlay")
        restore_overlay = payload.get("restore_overlay")
        prior_expected = self._overlay_at_effective_identity(prior_overlay, effective)
        restore_expected = self._overlay_at_effective_identity(restore_overlay, effective)
        current_overlay = self._raw_overlay(effective)
        if self._overlay_strict_identity(prior_expected, current_overlay):
            pass
        elif self._overlay_strict_identity(restore_expected, current_overlay):
            prior_identity = self._overlay_mapping(prior_overlay)
            if prior_identity is None:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            restored = None
            for name in ("restore_overlay", "recover_overlay", "revert_overlay"):
                candidate = getattr(self.store, name, None)
                if not callable(candidate):
                    continue
                try:
                    try:
                        restored = candidate(
                            prior_identity,
                            as_of_kst=effective,
                            reason=f"abort:{payload.get('transaction_id', '')}",
                        )
                    except TypeError:
                        restored = candidate(prior_identity)
                except (OSError, TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required") from exc
                break
            if restored is None:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            current_overlay = self._raw_overlay(effective)
            if not self._overlay_strict_identity(prior_expected, current_overlay):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        else:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
        prior_epoch = payload.get("prior_epoch")
        new_epoch = payload.get("new_epoch")
        if self._same_json_mapping(prior_epoch, current_epoch):
            return
        if not self._same_json_mapping(new_epoch, current_epoch):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        if not isinstance(prior_epoch, Mapping):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        self._write_feature_epoch(epoch_path, prior_epoch)
        _path, restored = self._feature_epoch(epoch_path)
        if not self._same_json_mapping(prior_epoch, restored):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")


    def _recover_rollback_locked(
        self,
        prepared: Mapping[str, object],
        rows: list[Mapping[str, object]],
    ) -> Mapping[str, object]:
        payload = self._transition_payload(prepared)
        if payload is None:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        transaction_id = payload.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        committed = self._transition_rows(
            rows,
            event_type="transition_committed",
            transaction_id=transaction_id,
        )
        aborted = self._transition_rows(
            rows,
            event_type="transition_aborted",
            transaction_id=transaction_id,
        )
        if committed and aborted:
            raise AdaptiveWorkflowError("adaptive lifecycle transaction has conflicting terminals")
        if committed or aborted:
            return committed[-1] if committed else aborted[-1]
        self._validate_recovery_payload(payload)
        overlay_ok, epoch_ok, _current_overlay = self._rollback_state(payload)
        prior_expected = self._overlay_at_effective_identity(
            payload.get("prior_overlay"),
            payload.get("effective_kst"),
        )
        prior_overlay_ok = (
            self._overlay_strict_identity(payload.get("prior_overlay"), _current_overlay)
            or self._overlay_strict_identity(prior_expected, _current_overlay)
        )
        _epoch_path, _current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
        prior_epoch_ok = self._same_json_mapping(
            payload.get("prior_epoch"),
            _current_epoch,
        )
        if overlay_ok and not epoch_ok:
            if not prior_epoch_ok:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
            new_epoch = payload.get("new_epoch")
            if not isinstance(new_epoch, Mapping):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            if not self._same_json_mapping(payload.get("prior_epoch"), current_epoch):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            self._write_feature_epoch(epoch_path, new_epoch)
            _path, restored_epoch = self._feature_epoch(epoch_path)
            if not self._same_json_mapping(new_epoch, restored_epoch):
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            epoch_ok = True
        if overlay_ok:
            self._reconcile_config_epoch_transition_locked(payload, complete=True)
        elif prior_overlay_ok and prior_epoch_ok:
            self._reconcile_config_epoch_transition_locked(payload, complete=False)
        else:
            self._reconcile_config_epoch_transition_locked(payload, complete=False)
            self._restore_rollback_prior(payload)
        receipt = self._receipt_for_transaction(
            rows,
            event_type="adaptive_plan_rolled_back",
            transaction_id=transaction_id,
        )
        overlay_ok, epoch_ok, _current_overlay = self._rollback_state(payload)
        if receipt is not None and not overlay_ok:
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        if overlay_ok:
            if not epoch_ok:
                epoch_path, current_epoch = self._feature_epoch(Path(str(payload["epoch_path"])))
                prior_epoch = payload.get("prior_epoch")
                new_epoch = payload.get("new_epoch")
                if not self._same_json_mapping(prior_epoch, current_epoch):
                    if not self._same_json_mapping(new_epoch, current_epoch):
                        raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
                else:
                    if not isinstance(new_epoch, Mapping):
                        raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
                    self._write_feature_epoch(epoch_path, new_epoch)
                    _path, restored_epoch = self._feature_epoch(epoch_path)
                    if not self._same_json_mapping(new_epoch, restored_epoch):
                        raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
            if receipt is None:
                receipt_payload = payload.get("receipt_payload")
                if not isinstance(receipt_payload, Mapping):
                    raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
                receipt = self._append_locked(
                    self.store,
                    "adaptive_plan_rolled_back",
                    receipt_payload,
                    dedupe_key=str(payload["receipt_dedupe_key"]),
                )
                rows.append(receipt)
            return self._append_transition_locked(
                "transition_committed",
                {
                    "transaction_id": transaction_id,
                    "operator_address": payload.get("operator_address"),
                    "action": "rollback",
                    "state": "committed",
                    "revision_id": payload.get("revision_id"),
                    "proposal_digest": payload.get("proposal_digest"),
                    "registration_digest": payload.get("registration_digest"),
                    "source_digest": payload.get("source_digest"),
                    "policy_digest": payload.get("policy_digest"),
                    "meal_constraints_digest": payload.get("meal_constraints_digest"),
                    "catalog_digest": payload.get("catalog_digest"),
                    "operator_body_digest": payload.get("operator_body_digest"),
                    "customer_body_digest": payload.get("customer_body_digest"),
                    "authority_digest": payload.get("authority_digest"),
                    "prepared_digest": prepared.get("event_id"),
                    "receipt_event_id": receipt.get("event_id"),
                },
                dedupe_key=f"adaptive-transition-commit:{transaction_id}",
            )
        self._restore_rollback_prior(payload)
        return self._append_transition_locked(
            "transition_aborted",
            {
                "transaction_id": transaction_id,
                "action": "rollback",
                "state": "aborted",
                "revision_id": payload.get("revision_id"),
                "proposal_digest": payload.get("proposal_digest"),
                "registration_digest": payload.get("registration_digest"),
                "source_digest": payload.get("source_digest"),
                "policy_digest": payload.get("policy_digest"),
                "meal_constraints_digest": payload.get("meal_constraints_digest"),
                "catalog_digest": payload.get("catalog_digest"),
                "operator_body_digest": payload.get("operator_body_digest"),
                "customer_body_digest": payload.get("customer_body_digest"),
                "authority_digest": payload.get("authority_digest"),
                "prepared_digest": prepared.get("event_id"),
                "reason": "incomplete_state",
            },
            dedupe_key=f"adaptive-transition-abort:{transaction_id}",
        )

    def recover_lifecycle_transactions(self) -> Mapping[str, object]:
        """Reconcile prepared adaptive activation/rollback transactions idempotently."""
        self._require_production()
        self._require_non_diagnostic_delivery_runtime()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        recovered: list[Mapping[str, object]] = []
        with self._authority_lock(), self._lifecycle_lock:
            try:
                with locked():
                    self._adaptive_store_lock_held = True
                    try:
                        rows = list(self.store.read())
                        for row in tuple(rows):
                            payload = self._transition_payload(row)
                            if (
                                not isinstance(row, Mapping)
                                or row.get("event_type") != "transition_prepared"
                                or payload is None
                                or payload.get("customer_key") != self.customer_key
                            ):
                                continue
                            action = payload.get("action")
                            if action == "activate":
                                result = self._recover_activation_locked(row, rows)
                            elif action == "rollback":
                                result = self._recover_rollback_locked(row, rows)
                            else:
                                raise AdaptiveWorkflowError(
                                    "adaptive lifecycle recovery is required"
                                )
                            recovered.append(result)
                    finally:
                        self._adaptive_store_lock_held = False
            except AdaptiveWorkflowError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive lifecycle recovery is required") from exc
        return {"recovered": tuple(recovered)}


    def revalidate_transition(self, action: str, proposal_digest: str) -> NutritionProposal:
        """Revalidate authority, evidence, and digests under the adaptive ledger lock."""
        if action not in {"approve", "activate", "deliver"}:
            raise AdaptiveWorkflowError("adaptive lifecycle action is invalid")
        if not isinstance(proposal_digest, str) or not proposal_digest:
            raise AdaptiveWorkflowError("adaptive proposal digest is required")
        with self._authority_lock(), self._lifecycle_lock:
            proposal = self._latest_production_proposal()
            if proposal.digest != proposal_digest:
                raise AdaptiveWorkflowError("stale adaptive proposal digest")
            (
                _customer,
                _data_root,
                spec,
                events,
                source_digest,
                artifacts,
                _epoch_path,
                _epoch,
                registration_binding,
            ) = self._production_context(
                require_activation=action == "deliver",
                require_delivery=action == "deliver",
            )
            pins = self._proposal_pins(proposal)
            expected = (
                source_digest,
                artifacts.policy_digest,
                registration_binding.meal_constraints_digest,
                artifacts.catalog_digest,
            )
            if pins != expected:
                raise AdaptiveWorkflowError("adaptive proposal evidence is stale")
            if self._registration_pin(proposal, required=True) != registration_binding.registration_digest:
                raise AdaptiveWorkflowError("adaptive registration revision is stale")
            if proposal.decision in {Decision.HUMAN_REVIEW, Decision.OBSERVE}:
                raise AdaptiveWorkflowError("adaptive proposal requires human review")
            if proposal.meal_plan is None:
                raise AdaptiveWorkflowError("adaptive proposal meal plan is unavailable")
            rows = self.store.read()
            if action in {"activate", "deliver"}:
                approval = self._matching_event(rows, "plan_approved", proposal.digest)
                if approval is None:
                    raise AdaptiveWorkflowError("adaptive proposal is not approved")
                approval_payload = approval.get("payload")
                if not isinstance(approval_payload, Mapping):
                    raise AdaptiveWorkflowError("adaptive approval pins are invalid")
                expected_approval = self._approval_payload(
                    proposal,
                    spec,
                    str(approval_payload.get("operator_id", "")),
                )
                for key in (
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "registration_digest",
                    "catalog_digest",
                    "operator_body_digest",
                    "customer_body_digest",
                    "meal_plan_digest",
                    "meal_digest",
                    "authority_digest",
                    "operator_address",
                    "canonical_owner_snapshot",
                    "canonical_owner_version",
                    "customer_body",
                    "risk_policy_version",
                    "risk_policy_digest",
                    "risk_policy_document_digest",
                ):
                    if (
                        self._owner_key(approval_payload.get(key))
                        != self._owner_key(expected_approval.get(key))
                        if key == "operator_address"
                        else approval_payload.get(key) != expected_approval.get(key)
                    ):
                        raise AdaptiveWorkflowError("adaptive approval pins are stale")
            if action == "deliver":
                activation = self._matching_event(
                    rows,
                    "adaptive_plan_activated",
                    proposal.digest,
                )
                if activation is None:
                    raise AdaptiveWorkflowError("adaptive proposal is not activated")
                activation_payload = activation.get("payload")
                if not isinstance(activation_payload, Mapping):
                    raise AdaptiveWorkflowError("adaptive activation pins are invalid")
                for key in (
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "registration_digest",
                    "catalog_digest",
                    "operator_body_digest",
                    "customer_body_digest",
                    "meal_plan_digest",
                    "meal_digest",
                    "authority_digest",
                    "operator_address",
                    "canonical_owner_snapshot",
                    "canonical_owner_version",
                    "customer_body",
                    "risk_policy_version",
                    "risk_policy_digest",
                    "risk_policy_document_digest",
                ):
                    if (
                        self._owner_key(activation_payload.get(key))
                        != self._owner_key(expected_approval.get(key))
                        if key == "operator_address"
                        else activation_payload.get(key) != expected_approval.get(key)
                    ):
                        raise AdaptiveWorkflowError("adaptive activation pins are stale")
            return proposal
    def _latest_identity(self) -> tuple[str, int] | None:
        try:
            rows = self.store.read()
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive event store is unreadable") from exc
        latest: tuple[str, int] | None = None
        for row in rows:
            if not isinstance(row, Mapping):
                raise AdaptiveWorkflowError("adaptive event store contains an invalid row")
            if row.get("event_type") not in {"plan_proposed", "plan_edited"}:
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise AdaptiveWorkflowError(
                    "adaptive event store contains an invalid proposal"
                )
            if payload.get("customer_key", self.customer_key) != self.customer_key:
                continue
            if self._production_mode and payload.get("execution_mode") != "production":
                continue
            digest_value = payload.get("proposal_digest")
            revision = payload.get("revision")
            if not isinstance(digest_value, str) or not digest_value:
                raise AdaptiveWorkflowError(
                    "adaptive event store contains an invalid proposal digest"
                )
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise AdaptiveWorkflowError(
                    "adaptive event store contains an invalid proposal revision"
                )
            latest = (digest_value, revision)
        return latest

    def _validate_production_pins(
        self,
        proposal: NutritionProposal,
        *,
        require_activation: bool = False,
        require_delivery: bool = False,
    ) -> tuple[
        object,
        Path,
        object,
        tuple[object, ...],
        str,
        ApprovedAdaptiveArtifacts,
        Path,
        dict[str, object],
        _AdaptiveRegistrationBinding,
    ]:
        """Re-read live production facts and require an immutable proposal match."""
        (
            customer,
            data_root,
            spec,
            events,
            source_digest,
            artifacts,
            epoch_path,
            epoch,
            registration_binding,
        ) = self._production_context(
            require_activation=require_activation,
            require_delivery=require_delivery,
        )
        expected = (
            source_digest,
            artifacts.policy_digest,
            registration_binding.meal_constraints_digest,
            artifacts.catalog_digest,
        )
        if self._proposal_pins(proposal) != expected:
            raise AdaptiveWorkflowError("adaptive proposal evidence is stale")
        if self._registration_pin(proposal, required=True) != registration_binding.registration_digest:
            raise AdaptiveWorkflowError("adaptive registration revision is stale")
        return (
            customer,
            data_root,
            spec,
            events,
            source_digest,
            artifacts,
            epoch_path,
            epoch,
            registration_binding,
        )
    def _ensure_proposal(self, proposal: NutritionProposal) -> None:
        if not isinstance(proposal, NutritionProposal):
            raise AdaptiveWorkflowError("adaptive proposal is invalid")
        if proposal.customer_key != self.customer_key:
            raise AdaptiveWorkflowError("adaptive proposal customer is out of scope")

    @staticmethod
    def _encode_proposal(
        proposal: NutritionProposal,
        registration_digest: str | None = None,
    ) -> str:
        try:
            encoded = canonical_json(proposal)
            raw = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive proposal encoding is invalid") from exc
        if not isinstance(raw, Mapping):
            raise AdaptiveWorkflowError("adaptive proposal encoding is invalid")
        fields = getattr(proposal, "__dataclass_fields__", {})
        if isinstance(fields, Mapping):
            missing = tuple(name for name in fields if name not in raw)
            if missing:
                raw = {
                    **raw,
                    **{name: getattr(proposal, name) for name in missing},
                }
        if registration_digest is not None:
            if (
                not isinstance(registration_digest, str)
                or len(registration_digest) != 64
                or any(character not in "0123456789abcdef" for character in registration_digest)
            ):
                raise AdaptiveWorkflowError("adaptive registration digest is invalid")
            raw = {**raw, "registration_digest": registration_digest}
        try:
            encoded = canonical_json(raw)
            digest_payload = {
                key: value for key, value in raw.items() if key != "registration_digest"
            }
            if digest(digest_payload) != proposal.digest:
                raise AdaptiveWorkflowError("adaptive proposal encoding is not canonical")
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdaptiveWorkflowError("adaptive proposal encoding is invalid") from exc
        return encoded

    def _append_proposal(
        self,
        proposal: NutritionProposal,
        *,
        event_type: str,
        topic_id: object,
        operator_id: str,
        parent_digest: str | None = None,
        revision_action: str | None = None,
    ) -> Mapping[str, object]:
        self._topic(topic_id)
        self._require_profile_api()
        self._ensure_proposal(proposal)
        actor = str(operator_id or "").strip()
        if not actor or len(actor) > 128:
            raise AdaptiveWorkflowError("adaptive operator identity is required")
        if event_type not in {"plan_proposed", "plan_edited"}:
            raise AdaptiveWorkflowError("adaptive event type is invalid")
        registration_digest = self._registration_pin(
            proposal,
            required=self._production_mode,
        )
        proposal_json = self._encode_proposal(proposal, registration_digest)
        audit_fields = (
            self._operator_audit_fields()
            if self._production_mode
            else {
                "authenticated_review_operator": None,
                "review_operator_version": 0,
                "canonical_owner_snapshot": None,
                "canonical_owner_version": 0,
            }
        )
        payload: dict[str, object] = {
            "customer_key": proposal.customer_key,
            "proposal_digest": proposal.digest,
            "proposal": proposal_json,
            "revision": proposal.revision,
            "operator_id": actor,
            "operator_address": (
                list(self._live_owner_key()) if self._production_mode else None
            ),
            **audit_fields,
            "topic_id": self.operator_topic_id,
            "execution_mode": (
                "production"
                if proposal.source_digest is not None
                else "shadow_test_only"
            ),
            "source_digest": proposal.source_digest,
            "policy_digest": proposal.policy_digest,
            "meal_constraints_digest": proposal.meal_constraints_digest,
            "registration_digest": registration_digest,
            "catalog_digest": proposal.catalog_digest,
            "meal_plan_digest": (
                proposal.meal_plan.digest if proposal.meal_plan is not None else None
            ),
            "meal_digest": (
                proposal.meal_plan.digest if proposal.meal_plan is not None else None
            ),
            "operator_body": proposal.operator_body,
            "customer_body": proposal.customer_body,
            "operator_body_digest": proposal.operator_body_digest,
            "customer_body_digest": proposal.customer_body_digest,
            "adherence_signal_digest": proposal.adherence_signal_digest,
        }
        if parent_digest is not None:
            payload["parent_digest"] = parent_digest
        if revision_action is not None:
            payload["revision_action"] = revision_action
        try:
            if getattr(self, "_adaptive_store_lock_held", False):
                return self._append_locked(
                    self.store,
                    event_type,
                    payload,
                    dedupe_key=f"{event_type}:{proposal.digest}",
                )
            locked = getattr(self.store, "locked", None)
            if not callable(locked):
                raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
            with self._authority_lock(), self._lifecycle_lock, locked():
                return self._append_locked(
                    self.store,
                    event_type,
                    payload,
                    dedupe_key=f"{event_type}:{proposal.digest}",
                )
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError(
                "adaptive event store rejected proposal"
            ) from exc

    def persist_proposal(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        """Persist one immutable proposal revision without enabling delivery."""
        self._topic(topic_id)
        if self._production_mode:
            raise AdaptiveWorkflowError("caller-fed adaptive persistence is shadow/test-only")
        latest = self._latest_identity()
        if latest is not None and latest != (proposal.digest, proposal.revision):
            raise AdaptiveWorkflowError("stale adaptive proposal revision")
        return self._append_proposal(
            proposal,
            event_type="plan_proposed",
            topic_id=topic_id,
            operator_id=operator_id,
        )

    def create_proposal(
        self,
        *,
        topic_id: object,
        observations: Iterable[DailyObservation],
        evaluation_day: date,
        policy: CustomerPolicy,
        current_target: MacroTarget,
        protein_g: int,
        fat_g: int,
        operator_id: str = "richard",
    ) -> tuple[NutritionProposal, str]:
        """Compute a caller-fed shadow/test-only proposal; production must use create_production_proposal."""
        self._topic(topic_id)
        if self._production_mode:
            raise AdaptiveWorkflowError("caller-fed adaptive proposals are shadow/test-only")
        self._require_profile_api()
        if not isinstance(policy, CustomerPolicy):
            raise AdaptiveWorkflowError("adaptive policy is invalid")
        if not isinstance(current_target, MacroTarget):
            raise AdaptiveWorkflowError("adaptive current target is invalid")
        if not isinstance(evaluation_day, date):
            raise AdaptiveWorkflowError("adaptive evaluation day is invalid")
        if isinstance(protein_g, bool) or not isinstance(protein_g, int) or protein_g < 0:
            raise AdaptiveWorkflowError("adaptive protein target is invalid")
        if isinstance(fat_g, bool) or not isinstance(fat_g, int) or fat_g < 0:
            raise AdaptiveWorkflowError("adaptive fat target is invalid")
        starts_on = self.starts_on if self.starts_on is not None else policy.starts_on
        if not isinstance(starts_on, date):
            raise AdaptiveWorkflowError("adaptive plan start is invalid")
        if starts_on != policy.starts_on:
            raise AdaptiveWorkflowError("adaptive policy start does not match coordinator")
        try:
            snapshot = build_snapshot(observations, evaluation_day, starts_on)
            proposal = propose(
                self.customer_key,
                snapshot,
                policy,
                current_target=current_target,
                protein_g=protein_g,
                fat_g=fat_g,
            )
            proposal = self._materialize_proposal(proposal)
            card = str(proposal.operator_body or render_operator_card(proposal))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive proposal inputs are invalid") from exc
        self._append_proposal(
            proposal,
            event_type="plan_proposed",
            topic_id=topic_id,
            operator_id=operator_id,
        )
        return proposal, card

    @staticmethod
    def _target_from_plan_value(value: object) -> tuple[MacroTarget | None, str]:
        if isinstance(value, MacroTarget):
            target = value
            if solve_macros is None or solve_macros(
                target.calories,
                target.protein_g,
                target.fat_g,
            ) != target:
                return None, "canonical_target_infeasible"
            return target, ""
        if isinstance(value, Mapping):
            get_value = value.get
        else:
            get_value = lambda name, default=None: getattr(value, name, default)
        fields = {
            "calories": get_value("calories_kcal", get_value("calories")),
            "protein_g": get_value("protein_g"),
            "fat_g": get_value("fat_g"),
            "carbs_g": get_value("carbs_g", get_value("carbohydrate_g")),
        }
        if any(item is None for item in (fields["calories"], fields["protein_g"], fields["fat_g"])):
            return None, "canonical_target_missing"
        try:
            calories = int(fields["calories"])
            protein_g = int(fields["protein_g"])
            fat_g = int(fields["fat_g"])
            carbs_value = fields["carbs_g"]
            if carbs_value is not None:
                target = MacroTarget(calories, int(carbs_value), protein_g, fat_g)
                if (
                    solve_macros is None
                    or solve_macros(calories, protein_g, fat_g) != target
                ):
                    return None, "canonical_target_infeasible"
                return target, ""
            target = solve_macros(calories, protein_g, fat_g) if callable(solve_macros) else None
        except (ArithmeticError, TypeError, ValueError):
            return None, "canonical_target_infeasible"
        return (
            (target, "")
            if target is not None
            else (None, "canonical_target_infeasible")
        )

    def _production_current_target(
        self,
        customer: object,
        spec: object,
        evaluation_day: date,
        starts_on: date,
        *,
        as_of_sequence: int | None = None,
    ) -> tuple[MacroTarget | None, str]:
        overlay = self._committed_overlay(
            evaluation_day,
            as_of_sequence=as_of_sequence,
        )
        if overlay is not None:
            overlay_digest = getattr(overlay, "proposal_digest", None)
            if overlay_digest is None and isinstance(overlay, Mapping):
                overlay_digest = overlay.get("proposal_digest")
            prior = self._proposal_for_digest(overlay_digest)
            if prior.target is None:
                return None, "active_overlay_target_missing"
            return prior.target, ""

        week_loader = getattr(customer, "plan_week", None)
        try:
            week = (
                week_loader(evaluation_day)
                if callable(week_loader)
                else spec.plan.weeks[
                    min(11, max(0, (evaluation_day - starts_on).days // 7))
                ]
            )
        except (AttributeError, IndexError, KeyError, TypeError):
            return None, "canonical_target_missing"
        return self._target_from_plan_value(week)

    def _production_human_review(
        self,
        snapshot: TrendSnapshot,
        reasons: tuple[str, ...],
        *,
        source_digest: str,
        artifacts: ApprovedAdaptiveArtifacts,
        registration_binding: _AdaptiveRegistrationBinding,
    ) -> NutritionProposal:
        proposal = NutritionProposal(
            customer_key=self.customer_key,
            snapshot=snapshot,
            decision=Decision.HUMAN_REVIEW,
            reasons=reasons,
            source_digest=source_digest,
            policy_digest=artifacts.policy_digest,
            meal_constraints_digest=registration_binding.meal_constraints_digest,
            catalog_digest=artifacts.catalog_digest,
        )
        return self._materialize_proposal(proposal)

    @staticmethod
    def _registration_digest(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise AdaptiveWorkflowError(f"adaptive {label} is invalid")
        return value

    @classmethod
    def _registration_meal_constraints(cls, value: object) -> object:
        if MealConstraints is None or not isinstance(value, Mapping):
            raise AdaptiveWorkflowError("adaptive registration meal constraints are unavailable")
        raw = dict(value)
        fields = getattr(MealConstraints, "__dataclass_fields__", {})
        if not isinstance(fields, Mapping):
            raise AdaptiveWorkflowError("adaptive registration meal constraints are unavailable")
        unknown = set(raw) - set(fields)
        if unknown:
            raise AdaptiveWorkflowError("adaptive registration meal constraints are invalid")
        for key in ("allergies", "excluded_food_ids", "restrictions", "digestion_exclusions"):
            if raw.get(key) is not None:
                raw[key] = frozenset(str(item) for item in raw[key])
        if raw.get("preferences") is not None:
            raw["preferences"] = tuple(str(item) for item in raw["preferences"])
        for time_key in ("training_times", "training_time_by_day"):
            source = raw.get(time_key)
            if source is None:
                continue
            if isinstance(source, Mapping):
                source = source.items()
            if not isinstance(source, (list, tuple)):
                raise AdaptiveWorkflowError("adaptive registration training times are invalid")
            parsed_times: list[tuple[date, str]] = []
            for item in source:
                if isinstance(item, Mapping):
                    day = item.get("kst_day", item.get("day", item.get("date")))
                    training_time = item.get("training_time", item.get("time"))
                else:
                    try:
                        day, training_time = item
                    except (TypeError, ValueError) as exc:
                        raise AdaptiveWorkflowError(
                            "adaptive registration training times are invalid"
                        ) from exc
                try:
                    parsed_day = (
                        day.date()
                        if isinstance(day, datetime)
                        else day
                        if isinstance(day, date)
                        else date.fromisoformat(str(day)[:10])
                    )
                except (TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError(
                        "adaptive registration training times are invalid"
                    ) from exc
                if not isinstance(training_time, str) or not training_time.strip():
                    raise AdaptiveWorkflowError(
                        "adaptive registration training times are invalid"
                    )
                parsed_times.append((parsed_day, training_time.strip()))
            raw[time_key] = tuple(sorted(parsed_times))
        if raw.get("meal_shares_bps") is not None:
            shares = raw["meal_shares_bps"]
            if isinstance(shares, Mapping):
                shares = shares.items()
            if not isinstance(shares, (list, tuple)):
                raise AdaptiveWorkflowError("adaptive registration meal shares are invalid")
            try:
                raw["meal_shares_bps"] = tuple(
                    (str(name), int(value)) for name, value in shares
                )
            except (TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError(
                    "adaptive registration meal shares are invalid"
                ) from exc
        for key in (
            "meal_slot_calorie_tolerance_percent",
            "meal_slot_macro_tolerance_percent",
            "calorie_tolerance_percent",
            "macro_tolerance_percent",
        ):
            if raw.get(key) is not None:
                try:
                    raw[key] = Decimal(str(raw[key]))
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError(
                        "adaptive registration meal constraints are invalid"
                    ) from exc
        try:
            constraints = MealConstraints(**raw)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError(
                "adaptive registration meal constraints are invalid"
            ) from exc
        if not constraints.complete:
            raise AdaptiveWorkflowError("adaptive registration meal constraints are incomplete")
        return constraints

    def _load_registration_binding(
        self,
        data_root: Path,
        artifacts: ApprovedAdaptiveArtifacts,
    ) -> _AdaptiveRegistrationBinding:
        loader = load_approved_adaptive_registration_inputs
        if not callable(loader) or self.profile_root is None:
            raise AdaptiveWorkflowError(
                "adaptive production registration inputs are unavailable"
            )
        try:
            inputs = loader(self.profile_root, self.customer_key)
        except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError(
                "adaptive production registration inputs are unavailable"
            ) from exc
        if AdaptiveRegistrationInputs is None or type(inputs) is not AdaptiveRegistrationInputs:
            raise AdaptiveWorkflowError(
                "adaptive production registration inputs are unavailable"
            )
        if inputs.customer_key != self.customer_key:
            raise AdaptiveWorkflowError("adaptive registration customer mismatch")
        if inputs.approved is not True:
            raise AdaptiveWorkflowError("adaptive registration approval is invalid")
        registration_digest = self._registration_digest(
            inputs.digest,
            "registration digest",
        )
        constraints_value = inputs.derived_constraints
        if (
            inputs.meal_constraints_digest is not None
            and inputs.derived_constraints_digest is not None
            and inputs.meal_constraints_digest != inputs.derived_constraints_digest
        ):
            raise AdaptiveWorkflowError("adaptive registration meal constraints digest mismatch")
        constraints_digest = self._registration_digest(
            inputs.meal_constraints_digest
            if inputs.meal_constraints_digest is not None
            else inputs.derived_constraints_digest,
            "meal constraints digest",
        )
        if not isinstance(constraints_value, Mapping):
            raise AdaptiveWorkflowError(
                "adaptive registration meal constraints are unavailable"
            )
        try:
            if digest(dict(constraints_value)) != constraints_digest:
                raise AdaptiveWorkflowError(
                    "adaptive registration meal constraints digest mismatch"
                )
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError(
                "adaptive registration meal constraints are invalid"
            ) from exc
        meal_constraints = self._registration_meal_constraints(constraints_value)
        artifact_digests = inputs.artifact_digests
        if artifact_digests is not None and not isinstance(artifact_digests, Mapping):
            raise AdaptiveWorkflowError("adaptive registration artifact digests are invalid")
        if isinstance(artifact_digests, Mapping) and (
            artifact_digests.get("meal_constraints") != constraints_digest
        ):
            raise AdaptiveWorkflowError("adaptive registration artifact digests are stale")
        registration_policy_digest = inputs.base_policy_digest
        if registration_policy_digest is None and isinstance(artifact_digests, Mapping):
            registration_policy_digest = artifact_digests.get("base_policy")
        registration_catalog_digest = inputs.catalog_digest
        if registration_catalog_digest is None and isinstance(artifact_digests, Mapping):
            registration_catalog_digest = artifact_digests.get("catalog")
        if isinstance(artifact_digests, Mapping) and (
            (
                inputs.base_policy_digest is not None
                and artifact_digests.get("base_policy") != inputs.base_policy_digest
            )
            or (
                inputs.catalog_digest is not None
                and artifact_digests.get("catalog") != inputs.catalog_digest
            )
        ):
            raise AdaptiveWorkflowError("adaptive registration artifact digests are stale")
        registration_policy_digest = self._registration_digest(
            registration_policy_digest,
            "registration policy digest",
        )
        registration_catalog_digest = self._registration_digest(
            registration_catalog_digest,
            "registration catalog digest",
        )
        if (
            registration_policy_digest != artifacts.policy_digest
            or registration_catalog_digest != artifacts.catalog_digest
        ):
            raise AdaptiveWorkflowError("adaptive registration artifacts are stale")
        return _AdaptiveRegistrationBinding(
            registration_digest=registration_digest,
            meal_constraints=meal_constraints,
            meal_constraints_digest=constraints_digest,
            policy_digest=registration_policy_digest,
            inputs=inputs,
            catalog_digest=registration_catalog_digest,
        )

    def _approved_training_schedule(
        self,
        inputs: object,
        evaluation_day: date,
    ) -> tuple[tuple[tuple[date, str], ...], bool]:
        if AdaptiveRegistrationInputs is None or type(inputs) is not AdaptiveRegistrationInputs:
            raise AdaptiveWorkflowError("adaptive approved training schedule is unavailable")
        if inputs.customer_key != self.customer_key:
            raise AdaptiveWorkflowError("adaptive registration customer mismatch")
        if inputs.approved is not True:
            raise AdaptiveWorkflowError("adaptive registration approval is invalid")
        raw_schedule = inputs.training_schedule
        if not isinstance(raw_schedule, (tuple, list)) or not raw_schedule:
            raise AdaptiveWorkflowError("adaptive approved training schedule is unavailable")
        by_day: dict[date, str] = {}
        for entry in raw_schedule:
            if (
                CustomerTrainingScheduleEntry is None
                or type(entry) is not CustomerTrainingScheduleEntry
            ):
                raise AdaptiveWorkflowError("adaptive approved training schedule is invalid")
            parsed_day = entry.date
            load_value = entry.load_category
            if not isinstance(load_value, str) or not load_value.strip():
                raise AdaptiveWorkflowError("adaptive approved training schedule is invalid")
            if parsed_day in by_day:
                raise AdaptiveWorkflowError("adaptive approved training schedule is invalid")
            by_day[parsed_day] = load_value.strip().lower()
        expected_days = tuple(
            evaluation_day + timedelta(days=index)
            for index in range(7)
        )
        if any(day not in by_day for day in expected_days):
            return (), False
        return tuple((day, by_day[day]) for day in expected_days), True

    def create_production_proposal(
        self,
        evaluation_day: date,
        *,
        operator_id: str = "richard",
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        as_of_sequence: int | None = None,
    ) -> tuple[NutritionProposal, str]:
        """Build a proposal only from the registered EventStore and approved artifacts."""
        self._topic(topic_id)
        self._require_non_diagnostic_delivery_runtime()
        self._require_profile_api()
        if not isinstance(evaluation_day, date):
            raise AdaptiveWorkflowError("adaptive evaluation day is invalid")
        operator = self._require_operator_owner(operator_id, required_action="create")
        with self._authority_lock(), self._lifecycle_lock:
            (
                customer,
                _data_root,
                spec,
                events,
                source_digest,
                artifacts,
                _path,
                _epoch,
                registration_binding,
            ) = self._production_context()
            starts_on = self.starts_on or getattr(spec.plan, "starts_on", None)
            if not isinstance(starts_on, date) or artifacts.policy.starts_on != starts_on:
                raise AdaptiveWorkflowError("adaptive policy start does not match customer plan")
            if evaluation_day < starts_on:
                raise AdaptiveWorkflowError("adaptive evaluation day is before the customer plan")
            registration_inputs = registration_binding.inputs
            planned_sessions, schedule_complete = self._approved_training_schedule(
                registration_inputs,
                evaluation_day,
            )
            prior = None
            try:
                prior = self._latest_production_proposal()
            except AdaptiveWorkflowError as exc:
                if str(exc) != "no production adaptive proposal is available":
                    raise
            try:
                snapshot = project_canonical_events(events, evaluation_day, starts_on)
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive canonical projection is invalid") from exc
            target, target_reason = self._production_current_target(
                customer,
                spec,
                evaluation_day,
                starts_on,
                as_of_sequence=as_of_sequence,
            )
            if target is None:
                proposal = self._production_human_review(
                    snapshot,
                    (target_reason,),
                    source_digest=source_digest,
                    artifacts=artifacts,
                    registration_binding=registration_binding,
                )
            elif (
                snapshot.trainer_ambiguity
                or any(load == "ambiguous" for _, load in snapshot.trainer_loads)
            ):
                proposal = self._production_human_review(
                    snapshot,
                    ("trainer_schedule_ambiguous",),
                    source_digest=source_digest,
                    artifacts=artifacts,
                    registration_binding=registration_binding,
                )
            elif not schedule_complete:
                proposal = self._production_human_review(
                    snapshot,
                    ("schedule_evidence_incomplete", "trainer_schedule_required"),
                    source_digest=source_digest,
                    artifacts=artifacts,
                    registration_binding=registration_binding,
                )
            else:
                cycle_days = {day for day, _ in planned_sessions}
                actual_sessions = tuple(
                    (day, load)
                    for day, load in snapshot.trainer_loads
                    if day in cycle_days
                )
                # The profile proposer treats snapshot.trainer_loads as a
                # planned source. Keep actual projection rows in the explicit
                # actual channel so they override approved planned inputs.
                planning_snapshot = replace(snapshot, trainer_loads=())
                try:
                    proposal = propose(
                        self.customer_key,
                        planning_snapshot,
                        artifacts.policy,
                        current_target=target,
                        protein_g=target.protein_g,
                        fat_g=target.fat_g,
                        meal_constraints=registration_binding.meal_constraints,
                        catalog=artifacts.catalog,
                        source_digest=source_digest,
                        policy_digest=artifacts.policy_digest,
                        planned_sessions=planned_sessions,
                        actual_sessions=actual_sessions,
                    )
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError(
                        "adaptive canonical proposal inputs are invalid"
                    ) from exc
                proposal = replace(
                    proposal,
                    snapshot=snapshot,
                    source_digest=source_digest,
                    policy_digest=artifacts.policy_digest,
                    meal_constraints_digest=registration_binding.meal_constraints_digest,
                    catalog_digest=artifacts.catalog_digest,
                )
                proposal = self._materialize_proposal(proposal)
            if prior is not None:
                proposal = self._materialize_proposal(
                    replace(
                        proposal,
                        revision=prior.revision + 1,
                        parent_digest=prior.digest,
                    )
                )
            locked = getattr(self.store, "locked", None)
            if not callable(locked):
                raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
            with locked():
                self._adaptive_store_lock_held = True
                try:
                    current_identity = self._latest_identity()
                    expected_identity = (
                        (prior.digest, prior.revision)
                        if prior is not None
                        else None
                    )
                    if current_identity != expected_identity:
                        raise AdaptiveWorkflowError("stale adaptive proposal revision")
                    self._remember_registration_pin(
                        proposal,
                        registration_binding.registration_digest,
                    )
                    self._validate_production_pins(proposal)
                    if self._latest_identity() != expected_identity:
                        raise AdaptiveWorkflowError("stale adaptive proposal revision")
                    self._validate_production_pins(proposal)
                    self._append_proposal(
                        proposal,
                        event_type="plan_proposed",
                        topic_id=topic_id,
                        parent_digest=proposal.parent_digest,
                        operator_id=operator,
                        revision_action="propose",
                    )
                finally:
                    self._adaptive_store_lock_held = False
            return proposal, str(proposal.operator_body or render_operator_card(proposal))
    def revise_note(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        note: str,
        operator_id: str = "richard",
    ) -> NutritionProposal:
        """Append a new note revision; the previous digest is never edited."""
        self._topic(topic_id)
        self._require_profile_api()
        self._ensure_proposal(proposal)
        normalized = str(note or "").strip()
        if len(normalized) > 1_000:
            raise AdaptiveWorkflowError("operator note is too long")
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        revised: NutritionProposal
        with self._authority_lock(), self._lifecycle_lock, locked():
            self._adaptive_store_lock_held = True
            try:
                actor = (
                    self._require_operator_owner(operator_id, required_action="edit_note")
                    if self._production_mode
                    else str(operator_id or "").strip()
                )
                if not actor:
                    raise AdaptiveWorkflowError("adaptive operator identity is required")
                latest = self._latest_identity()
                if latest != (proposal.digest, proposal.revision):
                    raise AdaptiveWorkflowError("stale adaptive proposal revision")
                if self._production_mode:
                    self._validate_production_pins(proposal)
                revised = replace(
                    proposal,
                    revision=proposal.revision + 1,
                    parent_digest=proposal.digest,
                    operator_note=normalized,
                )
                revised = self._materialize_proposal(revised)
                if self._production_mode:
                    self._remember_registration_pin(
                        revised,
                        self._registration_pin(proposal, required=True),
                    )
                if self._latest_identity() != (proposal.digest, proposal.revision):
                    raise AdaptiveWorkflowError("stale adaptive proposal revision")
                if self._production_mode:
                    self._validate_production_pins(proposal)
                self._append_proposal(
                    revised,
                    event_type="plan_edited",
                    topic_id=topic_id,
                    operator_id=actor,
                    parent_digest=proposal.digest,
                    revision_action="edit",
                )
            finally:
                self._adaptive_store_lock_held = False
        return revised

    def _revision_transition(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        operator_id: str,
        action: str,
        decision: Decision | None = None,
        target: MacroTarget | None = None,
        meal_plan: MealPlan | None = None,
        carb_days: tuple[tuple[date, str], ...] | None = None,
        reasons: tuple[str, ...] | None = None,
    ) -> NutritionProposal:
        self._topic(topic_id)
        self._require_profile_api()
        self._ensure_proposal(proposal)
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        revised: NutritionProposal
        with self._authority_lock(), self._lifecycle_lock, locked():
            self._adaptive_store_lock_held = True
            try:
                actor = (
                    self._require_operator_owner(operator_id, required_action=action)
                    if self._production_mode
                    else str(operator_id or "").strip()
                )
                if not actor:
                    raise AdaptiveWorkflowError("adaptive operator identity is required")
                latest = self._latest_identity()
                if latest != (proposal.digest, proposal.revision):
                    raise AdaptiveWorkflowError("stale adaptive proposal revision")
                if self._production_mode:
                    self._validate_production_pins(proposal)
                revised = replace(
                    proposal,
                    decision=proposal.decision if decision is None else decision,
                    target=None if action == "hold" else (
                        proposal.target if target is None else target
                    ),
                    meal_plan=None if action == "hold" else (
                        proposal.meal_plan if meal_plan is None else meal_plan
                    ),
                    carb_days=proposal.carb_days if carb_days is None else carb_days,
                    reasons=proposal.reasons if reasons is None else reasons,
                    revision=proposal.revision + 1,
                    parent_digest=proposal.digest,
                )
                revised = self._materialize_proposal(revised)
                if self._production_mode:
                    self._remember_registration_pin(
                        revised,
                        self._registration_pin(proposal, required=True),
                    )
                if self._latest_identity() != (proposal.digest, proposal.revision):
                    raise AdaptiveWorkflowError("stale adaptive proposal revision")
                if self._production_mode:
                    self._validate_production_pins(proposal)
                    if action == "release":
                        self._risk_policy_evidence()
                self._append_proposal(
                    revised,
                    event_type="plan_edited",
                    topic_id=topic_id,
                    operator_id=actor,
                    parent_digest=proposal.digest,
                    revision_action=action,
                )
            finally:
                self._adaptive_store_lock_held = False
        return revised

    def hold_proposal(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        operator_id: str = "richard",
    ) -> NutritionProposal:
        return self._revision_transition(
            proposal,
            topic_id=topic_id,
            operator_id=operator_id,
            action="hold",
            decision=Decision.HUMAN_REVIEW,
            target=None,
            meal_plan=None,
            reasons=tuple((*proposal.reasons, "operator_hold")),
        )

    def release_proposal(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        operator_id: str = "richard",
    ) -> NutritionProposal:
        self._topic(topic_id)
        self._ensure_proposal(proposal)
        if "operator_hold" not in proposal.reasons:
            raise AdaptiveWorkflowError("adaptive safety hold cannot be released")
        if not proposal.parent_digest:
            raise AdaptiveWorkflowError("adaptive held revision parent is unavailable")
        parent = self._proposal_for_digest(proposal.parent_digest)
        if parent.decision is Decision.HUMAN_REVIEW or parent.target is None:
            raise AdaptiveWorkflowError("adaptive held revision has no releasable target")
        return self._revision_transition(
            proposal,
            topic_id=topic_id,
            operator_id=operator_id,
            action="release",
            decision=parent.decision,
            target=parent.target,
            meal_plan=parent.meal_plan,
            carb_days=parent.carb_days,
            reasons=tuple((*parent.reasons, "operator_release")),
        )

    def hold_latest(
        self,
        proposal_digest: str,
        *,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        proposal = self._proposal_for_digest(proposal_digest)
        self.hold_proposal(proposal, topic_id=topic_id, operator_id=operator_id)
        return self.store.read()[-1]

    def release_latest(
        self,
        proposal_digest: str,
        *,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        proposal = self._proposal_for_digest(proposal_digest)
        self.release_proposal(proposal, topic_id=topic_id, operator_id=operator_id)
        return self.store.read()[-1]

    # Compatibility names for operator lifecycle callers.
    hold = hold_proposal
    release = release_proposal
    def render_proposal(self, proposal: NutritionProposal, *, topic_id: object) -> str:
        """Render a proposal only in the fixed operator review topic."""
        self._topic(topic_id)
        self._require_profile_api()
        self._ensure_proposal(proposal)
        try:
            return render_operator_card(proposal)
        except (TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive proposal cannot be rendered") from exc

    def approve(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        operator_id: str,
        expected_digest: str,
    ) -> Mapping[str, object]:
        """Record approval only for the latest exact persisted revision."""
        self._topic(topic_id)
        self._require_profile_api()
        self._ensure_proposal(proposal)
        if self._production_mode:
            return self.approve_latest(
                proposal.digest,
                topic_id=topic_id,
                operator_id=operator_id,
                expected_digest=expected_digest,
            )
        actor = str(operator_id or "").strip()
        if not actor:
            raise AdaptiveWorkflowError("adaptive operator identity is required")
        if proposal.digest != expected_digest:
            raise ValueError("stale proposal digest")
        if self._latest_identity() != (proposal.digest, proposal.revision):
            raise ValueError("stale proposal revision")
        decision = getattr(proposal.decision, "value", proposal.decision)
        if str(decision) == "human_review":
            raise AdaptiveWorkflowError("safety hold requires human review")
        try:
            approver = getattr(self.store, "approve", None)
            if not callable(approver):
                raise AdaptiveWorkflowError("adaptive approval API is unavailable")
            return approver(
                proposal,
                operator_id=actor,
                expected_digest=expected_digest,
            )
        except AdaptiveWorkflowError:
            raise
        except ValueError:
            raise
        except (OSError, TypeError) as exc:
            raise AdaptiveWorkflowError("adaptive approval could not be recorded") from exc

    @staticmethod
    def _customer_action_texts(proposal: NutritionProposal) -> tuple[str, ...]:
        customer_body = str(proposal.customer_body or "").strip()
        actions = tuple(
            " ".join(line.split())
            for line in customer_body.splitlines()[2:]
            if line.strip()
        )[:3]
        if not actions:
            raise AdaptiveWorkflowError(
                "approved proposal has no canonical customer actions"
            )
        return actions

    def _append_approved_action_continuity(self, proposal: NutritionProposal) -> None:
        """Persist one to three authoritative customer actions for an approval."""
        runtime = self.customer_runtime
        if runtime is None:
            raise AdaptiveWorkflowError("registered customer runtime is unavailable")
        try:
            from checkin_cli.adaptive_nutrition import CustomerActionContinuity
        except ImportError as exc:
            raise AdaptiveWorkflowError("customer action continuity API is unavailable") from exc
        actions = self._customer_action_texts(proposal)
        existing_approval_times = {
            str(payload.get("approved_at_kst"))
            for row in self.store.read()
            if isinstance(row, Mapping)
            and row.get("event_type") == "customer_action_continuity"
            and isinstance((payload := row.get("payload")), Mapping)
            and payload.get("customer_key") == proposal.customer_key
            and payload.get("approved_proposal_digest") == proposal.digest
            and payload.get("revision") == proposal.revision
            and payload.get("approved_at_kst") is not None
        }
        if len(existing_approval_times) > 1:
            raise AdaptiveWorkflowError("customer action approval time is inconsistent")
        approved_at_kst = (
            next(iter(existing_approval_times))
            if existing_approval_times
            else datetime.now(_KST).replace(microsecond=0).isoformat()
        )
        evaluation_day = getattr(proposal.snapshot, "evaluation_day", None)
        if type(evaluation_day) is not date:
            raise AdaptiveWorkflowError("approved proposal evaluation day is invalid")
        next_check = f"{(evaluation_day + timedelta(days=1)).isoformat()}T08:00:00+09:00"
        for index, action in enumerate(actions, 1):
            continuity = CustomerActionContinuity(
                customer_key=proposal.customer_key,
                approved_proposal_digest=proposal.digest,
                revision=proposal.revision,
                effective_kst_day=evaluation_day,
                action_text=action,
                action_atom=f"approved_plan_action_{index}",
                criterion_text="다음 체크인의 계획 준수 기록",
                criterion_atom="adherence_recorded",
                next_check_kst=next_check,
                approved_at_kst=approved_at_kst,
            )
            payload = {
                "customer_key": continuity.customer_key,
                "approved_proposal_digest": continuity.approved_proposal_digest,
                "revision": continuity.revision,
                "effective_kst_day": continuity.effective_kst_day.isoformat(),
                "action_id": continuity.action_id,
                "action_text": continuity.action_text,
                "action_atom": continuity.action_atom,
                "criterion_text": continuity.criterion_text,
                "criterion_atom": continuity.criterion_atom,
                "next_check_kst": continuity.next_check_kst,
                "approved_at_kst": continuity.approved_at_kst,
            }
            dedupe_key = f"customer-action:{continuity.action_id}"
            if self._adaptive_store_lock_held:
                self._append_locked(
                    self.store,
                    "customer_action_continuity",
                    payload,
                    dedupe_key=dedupe_key,
                )
            else:
                self.store.append_customer_action_continuity(continuity)
    def preview_registered_daily_projection(
        self,
        proposal: NutritionProposal,
    ) -> str:
        try:
            from checkin_cli.adaptive_nutrition import CustomerActionContinuity
            from checkin_cli.customer_coaching import (
                build_registered_daily_customer_projection,
            )
        except ImportError as exc:
            raise AdaptiveWorkflowError(
                "registered daily projection API is unavailable"
            ) from exc
        evaluation_day = getattr(proposal.snapshot, "evaluation_day", None)
        if type(evaluation_day) is not date:
            raise AdaptiveWorkflowError("approved proposal evaluation day is invalid")
        next_check = (
            f"{(evaluation_day + timedelta(days=1)).isoformat()}T08:00:00+09:00"
        )
        actions = tuple(
            CustomerActionContinuity(
                customer_key=proposal.customer_key,
                approved_proposal_digest=proposal.digest,
                revision=proposal.revision,
                effective_kst_day=evaluation_day,
                action_text=action,
                action_atom=f"approved_plan_action_{index}",
                criterion_text="다음 체크인의 계획 준수 기록",
                criterion_atom="adherence_recorded",
                next_check_kst=next_check,
            )
            for index, action in enumerate(self._customer_action_texts(proposal), 1)
        )
        return build_registered_daily_customer_projection(
            self.customer_runtime,
            proposal,
            actions=actions,
            next_check=next_check,
        ).render()
    def _registered_daily_projection(
        self,
        proposal: NutritionProposal,
        *,
        canonical_events: Iterable[object],
        as_of_kst_day: date,
    ) -> str:
        try:
            from checkin_cli.adaptive_nutrition import CustomerActionContinuity
            from checkin_cli.customer_coaching import build_registered_daily_customer_projection
        except ImportError as exc:
            raise AdaptiveWorkflowError("registered daily projection API is unavailable") from exc
        actions = []
        for row in self.store.read():
            payload = row.get("payload") if isinstance(row, Mapping) else None
            if (
                not isinstance(payload, Mapping)
                or row.get("event_type") != "customer_action_continuity"
                or payload.get("customer_key") != proposal.customer_key
                or payload.get("approved_proposal_digest") != proposal.digest
                or payload.get("revision") != proposal.revision
            ):
                continue
            actions.append(CustomerActionContinuity(
                customer_key=proposal.customer_key,
                approved_proposal_digest=proposal.digest,
                revision=proposal.revision,
                effective_kst_day=date.fromisoformat(str(payload["effective_kst_day"])),
                action_id=str(payload["action_id"]),
                action_text=str(payload["action_text"]),
                action_atom=str(payload["action_atom"]),
                criterion_text=str(payload["criterion_text"]),
                criterion_atom=str(payload["criterion_atom"]),
                next_check_kst=str(payload["next_check_kst"]),
                approved_at_kst=(
                    str(payload["approved_at_kst"])
                    if payload.get("approved_at_kst") is not None
                    else None
                ),
            ))
        selected = tuple(actions[:3])
        if not selected:
            raise AdaptiveWorkflowError("approved customer actions are unavailable")
        self.store.project_customer_action_outcomes(
            customer_key=proposal.customer_key,
            canonical_events=canonical_events,
            as_of_kst_day=as_of_kst_day,
        )
        return build_registered_daily_customer_projection(
            self.customer_runtime,
            proposal,
            actions=selected,
            next_check=selected[0].next_check_kst,
        ).render()
    def approve_latest(
        self,
        proposal_digest: str | None = None,
        *,
        expected_digest: str | None = None,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        """Approve the latest production proposal after a locked authority recheck."""
        self._topic(topic_id)
        self._require_non_diagnostic_delivery_runtime()
        selected = proposal_digest if proposal_digest is not None else expected_digest
        if (
            selected is None
            or (
                proposal_digest is not None
                and expected_digest is not None
                and proposal_digest != expected_digest
            )
        ):
            raise AdaptiveWorkflowError("adaptive proposal digest is required")
        self._require_operator_owner(operator_id, required_action="approve")
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock:
            with locked():
                self._adaptive_store_lock_held = True
                try:
                    initial = self._latest_production_proposal()
                    if initial.digest != selected:
                        raise AdaptiveWorkflowError("stale adaptive proposal digest")
                    initial_context = self._validate_production_pins(initial)
                    initial_owner = self._live_owner_key()
                    initial_fingerprint = (
                        initial_context[4],
                        initial_context[5].policy_digest,
                        initial_context[8].meal_constraints_digest,
                        initial_context[5].catalog_digest,
                        initial_context[8].registration_digest,
                        self._authority_digest(initial_context[2]),
                        initial_owner,
                    )
                    proposal = self.revalidate_transition("approve", selected)
                    actor = self._require_operator_owner(operator_id, required_action="approve")
                    final_context = self._validate_production_pins(proposal)
                    final_owner = self._live_owner_key()
                    final_fingerprint = (
                        final_context[4],
                        final_context[5].policy_digest,
                        final_context[8].meal_constraints_digest,
                        final_context[5].catalog_digest,
                        final_context[8].registration_digest,
                        self._authority_digest(final_context[2]),
                        final_owner,
                    )
                    if initial_fingerprint != final_fingerprint:
                        raise AdaptiveWorkflowError("adaptive approval authority is stale")
                    if self._latest_identity() != (proposal.digest, proposal.revision):
                        raise AdaptiveWorkflowError("stale adaptive proposal revision")
                    last_context = self._validate_production_pins(proposal)
                    last_fingerprint = (
                        last_context[4],
                        last_context[5].policy_digest,
                        last_context[8].meal_constraints_digest,
                        last_context[5].catalog_digest,
                        last_context[8].registration_digest,
                        self._authority_digest(last_context[2]),
                        self._live_owner_key(),
                    )
                    if last_fingerprint != final_fingerprint:
                        raise AdaptiveWorkflowError("adaptive approval authority is stale")
                    self._append_approved_action_continuity(proposal)
                    payload = self._approval_payload(proposal, last_context[2], actor)
                    approved = self._append_locked(
                        self.store,
                        "plan_approved",
                        payload,
                        dedupe_key=f"production-approve:{proposal.digest}:{actor}",
                    )
                    return approved
                except AdaptiveWorkflowError:
                    raise
                except (OSError, TypeError, ValueError) as exc:
                    raise AdaptiveWorkflowError(
                        "adaptive approval could not be recorded"
                    ) from exc
                finally:
                    self._adaptive_store_lock_held = False

    @staticmethod
    def _write_feature_epoch(path: Path, payload: Mapping[str, object]) -> None:
        if path.is_symlink():
            raise AdaptiveWorkflowError("adaptive feature epoch symlink is not allowed")
        if payload.get("schema_version") != "1.0":
            raise AdaptiveWorkflowError("adaptive feature epoch version is invalid")
        if set(payload) != {
            "schema_version",
            "epoch",
            "config_digest",
            "analytics_shadow",
            "operator_candidates",
            "activation",
            "delivery",
        }:
            raise AdaptiveWorkflowError("adaptive feature epoch is invalid")
        configured = payload.get("config_digest")
        expected = AdaptiveNutritionCoordinator._feature_config_digest(payload)
        if (
            not isinstance(configured, str)
            or len(configured) != 64
            or not hmac.compare_digest(configured, expected)
        ):
            raise AdaptiveWorkflowError("adaptive feature config digest mismatch")
        encoded = (canonical_json(dict(payload)) + "\n").encode("utf-8")
        temporary = path.with_name(path.name + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise AdaptiveWorkflowError("adaptive feature epoch temporary file exists")
        try:
            temporary.write_bytes(encoded)
            temporary.chmod(0o600)
            os.replace(temporary, path)
            path.chmod(0o600)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise AdaptiveWorkflowError("adaptive feature epoch could not be persisted") from exc
    def _enabled_customer_keys(self) -> tuple[str, ...]:
        authority = self.authority
        registry = getattr(authority, "registry", None) if authority is not None else None
        customers = getattr(registry, "customers", None)
        if not isinstance(customers, (tuple, list)):
            raise AdaptiveWorkflowError("adaptive config epoch live fanout is unavailable")
        keys = [
            str(getattr(getattr(runtime, "spec", None), "customer_key", "")).strip()
            for runtime in customers
            if getattr(getattr(runtime, "spec", None), "enabled", False) is True
        ]
        if (
            not keys
            or any(not key for key in keys)
            or len(keys) != len(set(keys))
        ):
            raise AdaptiveWorkflowError("adaptive config epoch live fanout is unavailable")
        return tuple(sorted(keys))

    @staticmethod
    def _read_config_epoch_rows(path: Path) -> list[dict[str, object]]:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise AdaptiveWorkflowError("adaptive config epoch journal is unavailable")
        try:
            if path.stat().st_mode & 0o077:
                raise AdaptiveWorkflowError("adaptive config epoch journal permissions are invalid")
            raw = path.read_bytes()
        except OSError as exc:
            raise AdaptiveWorkflowError("adaptive config epoch journal is unavailable") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise AdaptiveWorkflowError("adaptive config epoch journal is invalid")
        rows: list[dict[str, object]] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive config epoch journal is invalid") from exc
            if not isinstance(row, dict):
                raise AdaptiveWorkflowError("adaptive config epoch journal is invalid")
            if not isinstance(row.get("row_digest"), str):
                raise AdaptiveWorkflowError("adaptive config epoch journal digest is missing")
            AdaptiveNutritionCoordinator._validate_journal_digest(row)
            rows.append(row)
        return rows

    def _append_config_epoch_locked(
        self,
        epoch: int,
        config_digest: str,
        customer_keys: tuple[str, ...],
        *,
        state: str,
        prepared_digest: str | None = None,
        approved_by: tuple[str, str, str] | None = None,
        risk_policy: Mapping[str, str] | None = None,
        delivery: bool | None = None,
    ) -> Mapping[str, object]:
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 0
            or not isinstance(state, str)
            or state not in {"prepared", "committed", "abandoned"}
        ):
            raise AdaptiveWorkflowError("adaptive config epoch transition is invalid")
        if (
            not isinstance(config_digest, str)
            or len(config_digest) != 64
            or any(character not in "0123456789abcdef" for character in config_digest)
            or not isinstance(customer_keys, tuple)
            or not customer_keys
            or any(
                type(key) is not str
                or not key
                or key != key.strip()
                for key in customer_keys
            )
            or tuple(sorted(set(customer_keys))) != customer_keys
            or self.customer_key not in customer_keys
        ):
            raise AdaptiveWorkflowError("adaptive config epoch payload is invalid")
        path_value = getattr(self.store, "config_epoch_path", None)
        if not isinstance(path_value, (str, Path)):
            raise AdaptiveWorkflowError("adaptive config epoch journal is unavailable")
        path = Path(path_value)
        rows = self._read_config_epoch_rows(path)
        intent_id = f"epoch:{epoch}"
        def owns(row: Mapping[str, object]) -> bool:
            raw_keys = row.get("customer_keys")
            if not isinstance(raw_keys, (tuple, list)):
                return False
            return (
                row.get("intent_id") == intent_id
                and row.get("epoch") == epoch
                and row.get("config_digest") == config_digest
                and tuple(raw_keys) == customer_keys
                and self.customer_key in customer_keys
                and (
                    risk_policy is None
                    or all(row.get(key) == value for key, value in risk_policy.items())
                )
            )

        existing = [row for row in rows if owns(row)]
        customer_state = {
            key: ("pending" if state == "prepared" else state)
            for key in customer_keys
        }
        body: dict[str, object] = {
            "schema_version": "1.0",
            "kind": "config_epoch",
            "intent_id": intent_id,
            "state": state,
            "epoch": epoch,
            "config_digest": config_digest,
            "customer_keys": customer_keys,
            "customer_state": customer_state,
        }
        if delivery is not None:
            if type(delivery) is not bool:
                raise AdaptiveWorkflowError("adaptive delivery gate is invalid")
            body["delivery"] = delivery
        if risk_policy is not None:
            if set(risk_policy) != {
                "risk_policy_version",
                "risk_policy_digest",
                "risk_policy_document_digest",
            } or any(not isinstance(value, str) or not value for value in risk_policy.values()):
                raise AdaptiveWorkflowError("adaptive risk policy pins are invalid")
            body.update(risk_policy)
        if approved_by is not None:
            if (
                not isinstance(approved_by, tuple)
                or len(approved_by) != 3
                or any(not isinstance(value, str) or not value for value in approved_by)
            ):
                raise AdaptiveWorkflowError("adaptive config epoch approver is invalid")
            body["approved_by"] = {
                "user_id": approved_by[0],
                "chat_id": approved_by[1],
                "topic_id": approved_by[2],
            }
        if state in {"committed", "abandoned"}:
            prepared = next(
                (row for row in existing if row.get("state") == "prepared"),
                None,
            )
            if prepared is None:
                raise AdaptiveWorkflowError("adaptive config epoch prepared row is missing")
            if (
                prepared.get("epoch") != epoch
                or prepared.get("config_digest") != config_digest
                or tuple(prepared.get("customer_keys", ())) != customer_keys
                or prepared.get("approved_by") != body.get("approved_by")
                or any(
                    prepared.get(key) != body.get(key)
                    for key in (
                        "risk_policy_version",
                        "risk_policy_digest",
                        "risk_policy_document_digest",
                    )
                    )
                    or prepared.get("delivery") != body.get("delivery")
            ):
                raise AdaptiveWorkflowError("adaptive config epoch prepared payload mismatch")
            body["prepared_digest"] = prepared.get("row_digest")
            if prepared_digest is not None and prepared_digest != body["prepared_digest"]:
                raise AdaptiveWorkflowError("adaptive config epoch prepared digest mismatch")
        row = {**body, "row_digest": digest(body)}
        for prior in existing:
            if prior.get("state") == state:
                if prior.get("row_digest") == row["row_digest"]:
                    return prior
                raise AdaptiveWorkflowError("adaptive config epoch replay conflict")
            if prior.get("state") in {"committed", "abandoned"}:
                raise AdaptiveWorkflowError("adaptive config epoch has a terminal state")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive config epoch could not be persisted") from exc
        return row

    def _reconcile_config_epoch_transition_locked(
        self,
        payload: Mapping[str, object],
        *,
        complete: bool,
    ) -> None:
        config_epoch = payload.get("config_epoch")
        config_digest = payload.get("config_digest")
        raw_keys = payload.get("config_customer_keys")
        if (
            isinstance(config_epoch, bool)
            or not isinstance(config_epoch, int)
            or config_epoch < 0
            or not isinstance(config_digest, str)
            or len(config_digest) != 64
            or any(character not in "0123456789abcdef" for character in config_digest)
            or not isinstance(raw_keys, (tuple, list))
        ):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        customer_keys = tuple(raw_keys)
        if (
            any(
                type(key) is not str
                or not key
                or key != key.strip()
                for key in customer_keys
            )
            or tuple(sorted(set(customer_keys))) != customer_keys
            or self.customer_key not in customer_keys
        ):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        new_epoch = payload.get("new_epoch")
        if (
            not isinstance(new_epoch, Mapping)
            or set(new_epoch)
            != {
                "schema_version",
                "epoch",
                "config_digest",
                "analytics_shadow",
                "operator_candidates",
                "activation",
                "delivery",
            }
            or new_epoch.get("epoch") != config_epoch
            or new_epoch.get("config_digest") != config_digest
            or self._feature_config_digest(new_epoch) != config_digest
        ):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        path_value = getattr(self.store, "config_epoch_path", None)
        if not isinstance(path_value, (str, Path)):
            raise AdaptiveWorkflowError("adaptive lifecycle recovery is required")
        existing = [
            row for row in self._read_config_epoch_rows(Path(path_value))
            if (
                row.get("intent_id") == f"epoch:{config_epoch}"
                and row.get("epoch") == config_epoch
                and row.get("config_digest") == config_digest
                and tuple(row.get("customer_keys", ())) == customer_keys
                and self.customer_key in tuple(row.get("customer_keys", ()))
            )
        ]
        if not existing and not complete:
            return
        if complete:
            self._append_config_epoch_locked(
                config_epoch,
                config_digest,
                customer_keys,
                state="committed",
                approved_by=self._live_owner_key(),
            )
        else:
            self._append_config_epoch_locked(
                config_epoch,
                config_digest,
                customer_keys,
                state="abandoned",
                approved_by=self._live_owner_key(),
            )

    def activate_latest(
        self,
        proposal_digest: str | None = None,
        *,
        expected_digest: str | None = None,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        """Activate one approved production proposal through a durable transaction."""
        self._topic(topic_id)
        self._require_non_diagnostic_delivery_runtime()
        selected = proposal_digest if proposal_digest is not None else expected_digest
        actor = self._require_operator_owner(operator_id, required_action="activate")
        owner_snapshot = self._live_owner_key()
        owner_version = self._live_owner_version()
        if (
            selected is None
            or (
                proposal_digest is not None
                and expected_digest is not None
                and proposal_digest != expected_digest
            )
        ):
            raise AdaptiveWorkflowError("adaptive proposal digest is required")
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock:
            self._ensure_live_adaptive_journals()
            try:
                with locked():
                    rows = list(self.store.read())
                    for row in tuple(rows):
                        prepared_payload = self._transition_payload(row)
                        if (
                            isinstance(row, Mapping)
                            and row.get("event_type") == "transition_prepared"
                            and prepared_payload is not None
                            and prepared_payload.get("customer_key") == self.customer_key
                        ):
                            action = prepared_payload.get("action")
                            if action == "activate":
                                self._recover_activation_locked(row, rows)
                                rows = list(self.store.read())
                            elif action == "rollback":
                                self._recover_rollback_locked(row, rows)
                                rows = list(self.store.read())
                    existing = self._matching_event(
                        rows,
                        "adaptive_plan_activated",
                        selected,
                    )
                    committed = self._committed_transition(
                        rows,
                        action="activate",
                        proposal_digest=selected,
                    )
                    if existing is not None and committed is not None:
                        return existing
                    self._adaptive_store_lock_held = True
                    try:
                        proposal = self.revalidate_transition("activate", selected)
                    finally:
                        self._adaptive_store_lock_held = False
                    self._adaptive_store_lock_held = True
                    try:
                        actor = self._require_operator_owner(
                            operator_id,
                            required_action="activate",
                        )
                    finally:
                        self._adaptive_store_lock_held = False
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    rows = list(self.store.read())
                    existing = self._matching_event(
                        rows,
                        "adaptive_plan_activated",
                        proposal.digest,
                    )
                    committed = self._committed_transition(
                        rows,
                        action="activate",
                        proposal_digest=proposal.digest,
                    )
                    if existing is not None and committed is not None:
                        return existing
                    self._adaptive_store_lock_held = True
                    try:
                        (
                            _customer,
                            _data_root,
                            spec,
                            _events,
                            _source,
                            _artifacts,
                            epoch_path,
                            epoch,
                            registration_binding,
                        ) = self._production_context()
                    finally:
                        self._adaptive_store_lock_held = False
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    approval_payload = self._approval_payload(proposal, spec, actor)
                    previous_overlay = self._raw_overlay(proposal.snapshot.evaluation_day)
                    prior_overlay = self._overlay_mapping(previous_overlay)
                    overlay_payload: dict[str, object] = {
                        "revision_id": proposal.digest,
                        "proposal_digest": proposal.digest,
                        "registration_digest": approval_payload["registration_digest"],
                        "effective_from": proposal.snapshot.evaluation_day.isoformat(),
                        "authority_snapshot_id": approval_payload["authority_digest"],
                        "state": "effective",
                    }
                    if prior_overlay is not None:
                        previous_revision = prior_overlay.get("revision_id")
                        if previous_revision == proposal.digest:
                            pass
                        else:
                            replace_overlay = getattr(self.store, "replace_overlay", None)
                            if not callable(replace_overlay):
                                raise AdaptiveWorkflowError(
                                    "adaptive overlay replacement is unavailable"
                                )
                            overlay_payload["supersedes_revision_id"] = previous_revision
                    else:
                        if not callable(getattr(self.store, "append_overlay", None)):
                            raise AdaptiveWorkflowError(
                                "adaptive overlay lifecycle is unavailable"
                            )
                    config_customer_keys = self._enabled_customer_keys()
                    updated = dict(epoch)
                    updated["epoch"] = int(epoch.get("epoch", 0)) + 1
                    updated["activation"] = True
                    new_epoch = self._with_feature_config_digest(updated)
                    txid = self._fresh_transaction_id(
                        "activate",
                        proposal.digest,
                        self._epoch_digest(epoch),
                        rows,
                    )
                    receipt_payload = dict(approval_payload)
                    receipt_payload.update(
                        {
                            "transaction_id": txid,
                            "overlay_revision_id": proposal.digest,
                            "execution_mode": "production",
                            "prior_epoch_digest": self._epoch_digest(epoch),
                            "new_epoch_digest": self._epoch_digest(new_epoch),
                        }
                    )
                    prepared_payload = {
                        "transaction_id": txid,
                        "action": "activate",
                        "state": "prepared",
                        "transition": "activation",
                        "customer_key": self.customer_key,
                        "proposal_digest": proposal.digest,
                        "revision": proposal.revision,
                        "operator_id": actor,
                        "operator_address": list(self._live_owner_key()),
                        **{
                            key: approval_payload[key]
                            for key in (
                                "authenticated_review_operator",
                                "review_operator_version",
                                "canonical_owner_snapshot",
                                "canonical_owner_version",
                            )
                        },
                        "effective_kst": proposal.snapshot.evaluation_day.isoformat(),
                        "epoch_path": str(_data_root),
                        "prior_epoch": dict(epoch),
                        "new_epoch": new_epoch,
                        "config_epoch": new_epoch["epoch"],
                        "config_digest": new_epoch["config_digest"],
                        "config_customer_keys": config_customer_keys,
                        "prior_overlay": prior_overlay,
                        "new_overlay": overlay_payload,
                        "source_digest": approval_payload["source_digest"],
                        "registration_digest": approval_payload["registration_digest"],
                        "policy_digest": approval_payload["policy_digest"],
                        "meal_constraints_digest": approval_payload["meal_constraints_digest"],
                        "catalog_digest": approval_payload["catalog_digest"],
                        "operator_body_digest": approval_payload["operator_body_digest"],
                        "customer_body_digest": approval_payload["customer_body_digest"],
                        "meal_plan_digest": approval_payload["meal_plan_digest"],
                        "authority_digest": approval_payload["authority_digest"],
                        "risk_policy_version": approval_payload["risk_policy_version"],
                        "risk_policy_digest": approval_payload["risk_policy_digest"],
                        "risk_policy_document_digest": approval_payload[
                            "risk_policy_document_digest"
                        ],
                        "receipt_payload": receipt_payload,
                        "receipt_dedupe_key": f"production-activate:{proposal.digest}",
                    }
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    prepared = self._append_transition_locked(
                        "transition_prepared",
                        prepared_payload,
                        dedupe_key=f"adaptive-transition-prepare:{txid}",
                    )
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    self._append_config_epoch_locked(
                        int(new_epoch["epoch"]),
                        str(new_epoch["config_digest"]),
                        config_customer_keys,
                        state="prepared",
                        approved_by=self._live_owner_key(),
                    )
                    try:
                        if prior_overlay is not None and prior_overlay.get("revision_id") != proposal.digest:
                            replace_overlay(
                                str(prior_overlay.get("revision_id")),
                                overlay_payload,
                            )
                        elif prior_overlay is None:
                            self.store.append_overlay(overlay_payload)
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._write_feature_epoch(epoch_path, new_epoch)
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        receipt = self._append_locked(
                            self.store,
                            "adaptive_plan_activated",
                            receipt_payload,
                            dedupe_key=f"production-activate:{proposal.digest}",
                        )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._append_config_epoch_locked(
                            int(new_epoch["epoch"]),
                            str(new_epoch["config_digest"]),
                            config_customer_keys,
                            state="committed",
                            approved_by=self._live_owner_key(),
                        )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._append_transition_locked(
                            "transition_committed",
                            {
                                "transaction_id": txid,
                                "action": "activate",
                                "operator_address": prepared_payload.get("operator_address"),
                                "new_overlay": overlay_payload,
                                "state": "committed",
                                "proposal_digest": proposal.digest,
                                "registration_digest": prepared_payload.get("registration_digest"),
                                "revision": proposal.revision,
                                "prepared_digest": prepared.get("event_id"),
                                "receipt_event_id": receipt.get("event_id"),
                            },
                            dedupe_key=f"adaptive-transition-commit:{txid}",
                        )
                        return receipt
                    except AdaptiveWorkflowError:
                        raise
                    except (OSError, TypeError, ValueError) as exc:
                        raise AdaptiveWorkflowError(
                            "adaptive activation transaction is incomplete"
                        ) from exc
            except AdaptiveWorkflowError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive activation transaction failed") from exc

    def rollback_latest(
        self,
        revision_id: str,
        *,
        as_of_kst: date | datetime | str,
        reason: str = "operator_rollback",
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
    ) -> Mapping[str, object]:
        self._topic(topic_id)
        self._require_non_diagnostic_delivery_runtime()
        actor = self._require_operator_owner(operator_id, required_action="rollback")
        owner_snapshot = self._live_owner_key()
        owner_version = self._live_owner_version()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive lifecycle lock is unavailable")
        target_revision = str(revision_id or "").strip()
        if not target_revision:
            raise AdaptiveWorkflowError("adaptive rollback revision is required")
        with self._authority_lock(), self._lifecycle_lock:
            self._ensure_live_adaptive_journals()
            try:
                with locked():
                    rows = list(self.store.read())
                    for row in tuple(rows):
                        prepared_payload = self._transition_payload(row)
                        if (
                            isinstance(row, Mapping)
                            and row.get("event_type") == "transition_prepared"
                            and prepared_payload is not None
                            and prepared_payload.get("customer_key") == self.customer_key
                        ):
                            action = prepared_payload.get("action")
                            if action == "activate":
                                self._recover_activation_locked(row, rows)
                                rows = list(self.store.read())
                            elif action == "rollback":
                                self._recover_rollback_locked(row, rows)
                                rows = list(self.store.read())
                    existing = self._matching_event(
                        rows,
                        "adaptive_plan_rolled_back",
                        target_revision,
                    )
                    committed = self._committed_transition(
                        rows,
                        action="rollback",
                        proposal_digest=target_revision,
                    )
                    if existing is not None and committed is not None:
                        return existing
                    self._adaptive_store_lock_held = True
                    try:
                        (
                            _customer,
                            _data_root,
                            spec,
                            _events,
                            _source,
                            _artifacts,
                            epoch_path,
                            epoch,
                            registration_binding,
                        ) = self._production_context()
                    finally:
                        self._adaptive_store_lock_held = False
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    previous_overlay = self._raw_overlay(as_of_kst)
                    prior_overlay = self._overlay_mapping(previous_overlay)
                    restore_overlay = None
                    append_sequence = getattr(previous_overlay, "append_sequence", None)
                    if isinstance(append_sequence, int) and append_sequence > 1:
                        restore_overlay = self._overlay_mapping(
                            self._raw_overlay(
                                as_of_kst,
                                as_of_sequence=append_sequence - 1,
                            )
                        )
                    if (
                        prior_overlay is None
                        or prior_overlay.get("revision_id") != target_revision
                    ):
                        raise AdaptiveWorkflowError("adaptive overlay rollback target is unavailable")
                    if self._committed_transition(
                        rows,
                        action="activate",
                        proposal_digest=target_revision,
                    ) is None:
                        raise AdaptiveWorkflowError(
                            "adaptive overlay rollback target is unavailable"
                        )
                    proposal = self._proposal_for_digest(
                        str(prior_overlay.get("proposal_digest", target_revision))
                    )
                    pins = self._proposal_pins(proposal)
                    body_pins = self._proposal_body_pins(proposal, spec)
                    if (
                        self._registration_pin(proposal, required=True)
                        != registration_binding.registration_digest
                        or pins[1] != registration_binding.policy_digest
                        or pins[2] != registration_binding.meal_constraints_digest
                        or pins[3] != registration_binding.catalog_digest
                    ):
                        raise AdaptiveWorkflowError("adaptive registration revision is stale")
                    committed = self._committed_transition(
                        rows,
                        action="rollback",
                        proposal_digest=target_revision,
                    )
                    existing = self._matching_event(
                        rows,
                        "adaptive_plan_rolled_back",
                        target_revision,
                    )
                    if existing is not None and committed is not None:
                        return existing
                    config_customer_keys = self._enabled_customer_keys()
                    updated = dict(epoch)
                    updated["epoch"] = int(epoch.get("epoch", 0)) + 1
                    for key in (
                        "analytics_shadow",
                        "operator_candidates",
                        "activation",
                        "delivery",
                    ):
                        updated[key] = False
                    new_epoch = self._with_feature_config_digest(updated)
                    txid = self._fresh_transaction_id(
                        "rollback",
                        target_revision,
                        self._epoch_digest(epoch),
                        rows,
                    )
                    receipt_payload = {
                        "transaction_id": txid,
                        "customer_key": self.customer_key,
                        "revision_id": target_revision,
                        "proposal_digest": proposal.digest,
                        "operator_id": actor,
                        "operator_address": list(self._live_owner_key()),
                        **{
                            key: self._operator_audit_fields()[key]
                            for key in (
                                "authenticated_review_operator",
                                "review_operator_version",
                                "canonical_owner_snapshot",
                                "canonical_owner_version",
                            )
                        },
                        "topic_id": self.operator_topic_id,
                        "reason": str(reason or "operator_rollback"),
                        "effective_kst": str(as_of_kst),
                        "prior_epoch_digest": self._epoch_digest(epoch),
                        "new_epoch_digest": self._epoch_digest(new_epoch),
                        "source_digest": pins[0],
                        "registration_digest": self._registration_pin(
                            proposal,
                            required=True,
                        ),
                        "policy_digest": pins[1],
                        "meal_constraints_digest": pins[2],
                        "catalog_digest": pins[3],
                        "operator_body_digest": body_pins["operator_body_digest"],
                        "customer_body_digest": body_pins["customer_body_digest"],
                        "meal_plan_digest": body_pins["meal_plan_digest"],
                        "meal_digest": body_pins["meal_digest"],
                        "authority_digest": body_pins["authority_digest"],
                        "execution_mode": "production",
                    }
                    prepared_payload = {
                        **receipt_payload,
                        "action": "rollback",
                        "state": "prepared",
                        "transition": "rollback",
                        "epoch_path": str(_data_root),
                        "prior_epoch": dict(epoch),
                        "new_epoch": new_epoch,
                        "config_epoch": new_epoch["epoch"],
                        "config_digest": new_epoch["config_digest"],
                        "config_customer_keys": config_customer_keys,
                        "prior_overlay": prior_overlay,
                        "restore_overlay": restore_overlay,
                        "new_overlay": {
                            "revision_id": target_revision,
                            "proposal_digest": proposal.digest,
                            "state": "deactivated",
                        },
                        "receipt_payload": receipt_payload,
                        "receipt_dedupe_key": f"production-rollback:{target_revision}:{txid}",
                    }
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    prepared = self._append_transition_locked(
                        "transition_prepared",
                        prepared_payload,
                        dedupe_key=f"adaptive-transition-prepare:{txid}",
                    )
                    self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                    self._append_config_epoch_locked(
                        int(new_epoch["epoch"]),
                        str(new_epoch["config_digest"]),
                        config_customer_keys,
                        state="prepared",
                        approved_by=self._live_owner_key(),
                    )
                    try:
                        rollback = getattr(self.store, "rollback_overlay", None)
                        if not callable(rollback):
                            raise AdaptiveWorkflowError(
                                "adaptive overlay rollback is unavailable"
                            )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        rollback(
                            target_revision,
                            as_of_kst=as_of_kst,
                            reason=str(reason or "operator_rollback"),
                        )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._write_feature_epoch(epoch_path, new_epoch)
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        receipt = self._append_locked(
                            self.store,
                            "adaptive_plan_rolled_back",
                            receipt_payload,
                            dedupe_key=f"production-rollback:{target_revision}:{txid}",
                        )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._append_config_epoch_locked(
                            int(new_epoch["epoch"]),
                            str(new_epoch["config_digest"]),
                            config_customer_keys,
                            state="committed",
                            approved_by=self._live_owner_key(),
                        )
                        self._assert_owner_snapshot(operator_id, owner_snapshot, owner_version)
                        self._append_transition_locked(
                            "transition_committed",
                            {
                                "transaction_id": txid,
                                "action": "rollback",
                                "operator_address": prepared_payload.get("operator_address"),
                                "restore_overlay": prepared_payload.get("restore_overlay"),
                                "state": "committed",
                                "revision_id": target_revision,
                                "proposal_digest": proposal.digest,
                                "registration_digest": prepared_payload.get("registration_digest"),
                                "prepared_digest": prepared.get("event_id"),
                                "receipt_event_id": receipt.get("event_id"),
                            },
                            dedupe_key=f"adaptive-transition-commit:{txid}",
                        )
                        return receipt
                    except AdaptiveWorkflowError:
                        raise
                    except (OSError, TypeError, ValueError) as exc:
                        raise AdaptiveWorkflowError(
                            "adaptive rollback transaction is incomplete"
                        ) from exc
            except AdaptiveWorkflowError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive rollback transaction failed") from exc

    rollback_overlay = rollback_latest
    @staticmethod
    def _receipt_message_id(receipt: object) -> str:
        if not isinstance(receipt, Mapping) or receipt.get("ok") is not True:
            return ""
        value = receipt.get("message_id")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return ""
        result = str(value).strip()
        return result if result and len(result) <= 128 else ""

    @staticmethod
    def _delivery_receipt_id(value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return ""
        result = str(value).strip()
        return result if result and len(result) <= 128 else ""

    def _validate_delivery_record(
        self,
        proposal: NutritionProposal,
        attempt_payload: Mapping[str, object],
        *,
        require_current: bool = False,
    ) -> None:
        if not self._production_mode:
            return
        if (
            attempt_payload.get("customer_key") != self.customer_key
            or attempt_payload.get("proposal_digest") != proposal.digest
            or attempt_payload.get("reservation_id") != attempt_payload.get("delivery_id")
        ):
            raise AdaptiveWorkflowError("adaptive delivery pins are stale")
        expected_meal_digest = (
            proposal.meal_plan.digest if proposal.meal_plan is not None else ""
        )
        rendered_body = attempt_payload.get("customer_body")
        if not isinstance(rendered_body, str) or not rendered_body:
            raise AdaptiveWorkflowError("adaptive delivery body pin is invalid")
        rendered_digest = self._text_digest(rendered_body)
        expected = {
            "customer_body_digest": rendered_digest,
            "rendered_digest": rendered_digest,
            "operator_body_digest": proposal.operator_body_digest,
            "meal_plan_digest": expected_meal_digest,
            "meal_digest": expected_meal_digest,
            "source_digest": proposal.source_digest,
            "policy_digest": proposal.policy_digest,
            "meal_constraints_digest": proposal.meal_constraints_digest,
            "catalog_digest": proposal.catalog_digest,
        }
        if any(
            attempt_payload.get(field) != expected_value
            for field, expected_value in expected.items()
        ):
            raise AdaptiveWorkflowError("adaptive delivery pins are stale")
        registration_digest = self._registration_pin(proposal, required=True)
        if attempt_payload.get("registration_digest") != registration_digest:
            raise AdaptiveWorkflowError("adaptive delivery pins are stale")
        for field in (
            "customer_body_digest",
            "rendered_digest",
            "operator_body_digest",
            "source_digest",
            "registration_digest",
            "policy_digest",
            "meal_constraints_digest",
            "catalog_digest",
            "authority_digest",
            "epoch_digest",
        ):
            value = attempt_payload.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise AdaptiveWorkflowError("adaptive delivery pins are stale")
        destination = attempt_payload.get("destination")
        if (
            not isinstance(destination, Mapping)
            or any(
                not isinstance(destination.get(field), str)
                or not str(destination.get(field)).strip()
                for field in ("user_id", "chat_id", "topic_id")
            )
        ):
            raise AdaptiveWorkflowError("adaptive delivery destination pin is invalid")
        if require_current:
            (
                _customer,
                _data_root,
                spec,
                _events,
                source_digest,
                artifacts,
                _epoch_path,
                _epoch,
                registration_binding,
            ) = self._production_context(
                require_activation=True,
                require_delivery=True,
            )
            expected = {
                "source_digest": source_digest,
                "policy_digest": artifacts.policy_digest,
                "meal_constraints_digest": registration_binding.meal_constraints_digest,
                "catalog_digest": artifacts.catalog_digest,
                "registration_digest": registration_binding.registration_digest,
                "authority_digest": self._authority_digest(spec),
            }
            if any(
                attempt_payload.get(field) != expected_value
                for field, expected_value in expected.items()
            ):
                raise AdaptiveWorkflowError("adaptive delivery pins are stale")

    def _reconcile_delivery_locked(
        self,
        *,
        rows: list[Mapping[str, object]],
        proposal: NutritionProposal,
        attempt_payload: Mapping[str, object],
        message_id: str | None = None,
    ) -> Mapping[str, object]:
        reservation_rows = [
            row
            for row in rows
            if (
                isinstance(row, Mapping)
                and row.get("event_type") == "delivery_attempt_started"
                and isinstance(row.get("payload"), Mapping)
                and row["payload"].get("delivery_id") == attempt_payload.get("delivery_id")
                and row["payload"].get("provider_receipt") is None
                and row["payload"].get("message_id") is None
            )
        ]
        if len(reservation_rows) != 1:
            raise AdaptiveWorkflowError("adaptive delivery reservation sequence is invalid")
        attempt_payload = dict(attempt_payload)
        attempt_payload["attempt_event_id"] = reservation_rows[0].get("event_id")
        self._validate_delivery_record(proposal, attempt_payload)
        delivery_id = attempt_payload.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")

        def receipt_id(payload: object) -> str:
            if not isinstance(payload, Mapping):
                raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
            values: list[str] = []
            for field in ("provider_receipt", "message_id"):
                value = payload.get(field)
                if value is None:
                    continue
                parsed = self._delivery_receipt_id(value)
                if not parsed:
                    raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
                values.append(parsed)
            if len(set(values)) > 1:
                raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
            return values[0] if values else ""

        provider_rows: list[Mapping[str, object]] = []
        delivered_rows: list[Mapping[str, object]] = []
        pending_rows: list[Mapping[str, object]] = []
        audited_rows: list[Mapping[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping) or payload.get("delivery_id") != delivery_id:
                continue
            event_type = row.get("event_type")
            if event_type in {"delivery_receipt_recorded", "delivery_attempt_started"} and (
                payload.get("provider_receipt") is not None
                or payload.get("message_id") is not None
            ):
                provider_rows.append(row)
            elif event_type == "delivered":
                delivered_rows.append(row)
            elif event_type == "audit_pending":
                pending_rows.append(row)
            elif event_type == "sent_audited":
                audited_rows.append(row)

        immutable_fields = (
            "customer_key",
            "delivery_id",
            "reservation_id",
            "proposal_digest",
            "customer_body_digest",
            "rendered_digest",
            "operator_body_digest",
            "meal_plan_digest",
            "meal_digest",
            "destination",
            "topic_id",
            "epoch_digest",
            "activation_proposal_digest",
            "registration_digest",
            "source_digest",
            "policy_digest",
            "meal_constraints_digest",
            "catalog_digest",
            "authority_digest",
        )
        pending_receipts: set[str] = set()
        for row in pending_rows:
            payload = row.get("payload")
            pending_id = receipt_id(payload)
            if not pending_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
            pending_receipts.add(pending_id)
            if any(
                payload.get(field) != attempt_payload.get(field)
                for field in (
                    "customer_key",
                    "delivery_id",
                    "reservation_id",
                    "proposal_digest",
                    "registration_digest",
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "catalog_digest",
                    "authority_digest",
                )
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")

        provider_receipts: set[str] = set()
        if len(provider_rows) > 1 or len(delivered_rows) > 1 or len(pending_rows) > 1 or len(audited_rows) > 1:
            raise AdaptiveWorkflowError("adaptive delivery receipt sequence is invalid")
        for row in provider_rows:
            payload = row.get("payload")
            provider_id = receipt_id(payload)
            if not provider_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
            provider_receipts.add(provider_id)
            if any(
                payload.get(field) != attempt_payload.get(field)
                for field in immutable_fields
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")

        supplied_id = ""
        if message_id is not None:
            supplied_id = self._delivery_receipt_id(message_id)
            if not supplied_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
        receipt_ids = provider_receipts | pending_receipts
        if supplied_id:
            receipt_ids.add(supplied_id)
        if len(receipt_ids) > 1:
            raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
        persisted_message_id = next(iter(receipt_ids), "")
        if not persisted_message_id:
            raise AdaptiveWorkflowError("adaptive delivery receipt is unavailable")
        for row in delivered_rows:
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise AdaptiveWorkflowError("adaptive delivered receipt is invalid")
            if receipt_id(payload) != persisted_message_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
            if any(
                payload.get(field) != attempt_payload.get(field)
                for field in (
                    "customer_key",
                    "delivery_id",
                    "reservation_id",
                    "proposal_digest",
                    "customer_body_digest",
                    "destination",
                    "registration_digest",
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "catalog_digest",
                    "authority_digest",
                    "meal_plan_digest",
                    "topic_id",
                )
            ):
                raise AdaptiveWorkflowError("adaptive delivered receipt pins are stale")
        for row in audited_rows:
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
            if receipt_id(payload) != persisted_message_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
            if any(
                payload.get(field) != attempt_payload.get(field)
                for field in (
                    "customer_key",
                    "delivery_id",
                    "reservation_id",
                    "registration_digest",
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "catalog_digest",
                    "authority_digest",
                )
                if field in payload
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")
            if (
                "proposal_digest" in payload
                and payload.get("proposal_digest") != proposal.digest
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")

        provider_row = provider_rows[0] if provider_rows else None
        if provider_row is None:
            receipt_payload = dict(attempt_payload)
            receipt_payload.update(
                {
                    "provider_receipt": persisted_message_id,
                    "receipt": {"ok": True, "message_id": persisted_message_id},
                    "message_id": persisted_message_id,
                    "receipt_id": self._text_digest(
                        canonical_json(
                            {
                                "delivery_id": delivery_id,
                                "message_id": persisted_message_id,
                            }
                        )
                    ),
                }
            )
            provider_row = self._append_locked(
                self.store,
                "delivery_receipt_recorded",
                receipt_payload,
                dedupe_key=f"production-receipt:{delivery_id}",
            )
            rows.append(provider_row)
        provider_payload = provider_row.get("payload")
        if not isinstance(provider_payload, Mapping):
            raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
        provider_id = receipt_id(provider_payload)
        if provider_id != persisted_message_id or any(
            provider_payload.get(field) != attempt_payload.get(field)
            for field in immutable_fields
        ):
            raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")

        delivered = delivered_rows[0] if delivered_rows else None
        if delivered is None:
            destination_payload = attempt_payload.get("destination")
            if not isinstance(destination_payload, Mapping):
                raise AdaptiveWorkflowError("adaptive delivery destination pin is invalid")
            delivered_payload = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "proposal_digest": proposal.digest,
                "message_id": persisted_message_id,
                "provider_receipt": persisted_message_id,
                "customer_body_digest": attempt_payload.get("customer_body_digest"),
                "registration_digest": attempt_payload.get("registration_digest"),
                "source_digest": attempt_payload.get("source_digest"),
                "policy_digest": attempt_payload.get("policy_digest"),
                "meal_constraints_digest": attempt_payload.get("meal_constraints_digest"),
                "catalog_digest": attempt_payload.get("catalog_digest"),
                "authority_digest": attempt_payload.get("authority_digest"),
                "meal_plan_digest": attempt_payload.get("meal_plan_digest"),
                "destination": dict(destination_payload),
                "topic_id": destination_payload.get("topic_id"),
                "attempt_event_id": attempt_payload.get("attempt_event_id"),
            }
            delivered = self._append_locked(
                self.store,
                "delivered",
                delivered_payload,
                dedupe_key=f"production-delivered:{delivery_id}",
            )
            rows.append(delivered)
        delivered_payload = delivered.get("payload")
        if not isinstance(delivered_payload, Mapping):
            raise AdaptiveWorkflowError("adaptive delivered receipt is invalid")
        if (
            receipt_id(delivered_payload) != persisted_message_id
            or any(
                delivered_payload.get(field) != attempt_payload.get(field)
                for field in (
                    "customer_key",
                    "delivery_id",
                    "reservation_id",
                    "proposal_digest",
                    "customer_body_digest",
                    "destination",
                    "registration_digest",
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "catalog_digest",
                    "authority_digest",
                    "meal_plan_digest",
                    "topic_id",
                )
            )
        ):
            raise AdaptiveWorkflowError("adaptive delivered receipt pins are stale")

        for row in audited_rows:
            payload = row.get("payload")
            audit_id = receipt_id(payload)
            if not audit_id or audit_id != persisted_message_id:
                raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
            if any(
                payload.get(field) != attempt_payload.get(field)
                for field in (
                    "customer_key",
                    "delivery_id",
                    "reservation_id",
                    "registration_digest",
                    "source_digest",
                    "policy_digest",
                    "meal_constraints_digest",
                    "catalog_digest",
                    "authority_digest",
                )
                if field in payload
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")
            if (
                "proposal_digest" in payload
                and payload.get("proposal_digest") != proposal.digest
            ):
                raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")
        audited = audited_rows[0] if audited_rows else None
        if audited is None:
            audit_payload = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "proposal_digest": proposal.digest,
                "delivered_event_id": delivered.get("event_id"),
                "provider_receipt": persisted_message_id,
                "message_id": persisted_message_id,
                "registration_digest": attempt_payload.get("registration_digest"),
                "source_digest": attempt_payload.get("source_digest"),
                "policy_digest": attempt_payload.get("policy_digest"),
                "meal_constraints_digest": attempt_payload.get("meal_constraints_digest"),
                "catalog_digest": attempt_payload.get("catalog_digest"),
                "authority_digest": attempt_payload.get("authority_digest"),
            }
            try:
                audited = self._append_locked(
                    self.store,
                    "sent_audited",
                    audit_payload,
                    dedupe_key=f"production-sent-audit:{delivery_id}",
                )
            except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                try:
                    pending = self._append_delivery_audit_pending(
                        delivery_id=delivery_id,
                        proposal_digest=proposal.digest,
                        attempt_payload=attempt_payload,
                        provider_payload=provider_payload,
                        delivered_event_id=delivered.get("event_id"),
                        _under_store_lock=True,
                    )
                except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                    # The provider receipt and delivered row are already durable;
                    # leave them intact for a later reconciliation attempt.
                    return {
                        "event_type": "audit_pending",
                        "status": "audit_pending",
                        "delivery_id": delivery_id,
                        "provider_receipt": persisted_message_id,
                        "text": adaptive_delivery_result_text(
                            {"event_type": "audit_pending"}
                        ),
                    }
                return {
                    **dict(pending),
                    "event_type": "audit_pending",
                    "status": "audit_pending",
                    "text": adaptive_delivery_result_text(
                        {"event_type": "audit_pending"}
                    ),
                }
            rows.append(audited)
        return _audited_delivery_result(audited)
    @staticmethod
    def project_delivery_attempt(
        rows: Iterable[Mapping[str, object]],
    ) -> tuple[str, ...]:
        """Project physical delivery rows onto one canonical provider attempt."""
        projection: list[str] = []
        delivery_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            event_type = row.get("event_type")
            payload = row.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            delivery_id = payload.get("delivery_id")
            if isinstance(delivery_id, str) and delivery_id:
                delivery_ids.add(delivery_id)
            if event_type == "delivery_attempt_started":
                projection.append("reservation-started")
                if (
                    payload.get("provider_receipt") is not None
                    or payload.get("message_id") is not None
                ):
                    projection.append("receipt-started")
                continue
            state = {
                "delivery_attempt_consumed": "consumed",
                "delivery_receipt_recorded": "receipt-started",
                "delivery_unknown": "delivery_unknown",
                "delivered": "delivered",
                "audit_pending": "audit_pending",
                "sent_audited": "sent_audited",
            }.get(str(event_type))
            if state is not None:
                projection.append(state)
        if len(delivery_ids) > 1:
            raise AdaptiveWorkflowError("adaptive delivery projection mixes attempts")
        canonical = tuple(projection)
        allowed_sequences = {
            ("reservation-started",),
            ("reservation-started", "consumed"),
            ("reservation-started", "delivery_unknown"),
            ("reservation-started", "consumed", "delivery_unknown"),
            ("reservation-started", "receipt-started"),
            ("reservation-started", "consumed", "receipt-started"),
            ("reservation-started", "receipt-started", "delivered"),
            ("reservation-started", "consumed", "receipt-started", "delivered"),
            ("reservation-started", "consumed", "audit_pending"),
            ("reservation-started", "consumed", "audit_pending", "receipt-started"),
            (
                "reservation-started",
                "consumed",
                "audit_pending",
                "receipt-started",
                "delivered",
            ),
            (
                "reservation-started",
                "receipt-started",
                "delivered",
                "audit_pending",
            ),
            (
                "reservation-started",
                "receipt-started",
                "delivered",
                "sent_audited",
            ),
            (
                "reservation-started",
                "consumed",
                "receipt-started",
                "delivered",
                "audit_pending",
            ),
            (
                "reservation-started",
                "consumed",
                "receipt-started",
                "delivered",
                "sent_audited",
            ),
            (
                "reservation-started",
                "consumed",
                "receipt-started",
                "delivered",
                "audit_pending",
                "sent_audited",
            ),
            (
                "reservation-started",
                "consumed",
                "audit_pending",
                "receipt-started",
                "delivered",
                "sent_audited",
            ),
        }
        if canonical not in allowed_sequences:
            raise AdaptiveWorkflowError("adaptive delivery projection sequence is invalid")
        return canonical
    def _delivery_status_without_reconciliation(
        self,
        rows: Iterable[Mapping[str, object]],
        proposal_digest: str,
    ) -> Mapping[str, object] | None:
        """Inspect durable delivery state without appending reconciliation rows."""
        normalized_rows = tuple(row for row in rows if isinstance(row, Mapping))
        delivery_ids = {
            str(row["payload"]["delivery_id"])
            for row in normalized_rows
            if isinstance(row.get("payload"), Mapping)
            and row["payload"].get("proposal_digest") == proposal_digest
            and isinstance(row["payload"].get("delivery_id"), str)
            and row["payload"].get("delivery_id")
        }
        if not delivery_ids:
            return None
        matching = tuple(
            row
            for row in normalized_rows
            if isinstance(row.get("payload"), Mapping)
            and row["payload"].get("delivery_id") in delivery_ids
        )
        audited = next(
            (row for row in matching if row.get("event_type") == "sent_audited"),
            None,
        )
        if audited is not None:
            payload = audited.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            return {
                **dict(audited),
                "event_type": "duplicate",
                "status": "duplicate",
                "duplicate": True,
                "delivery_id": payload.get("delivery_id"),
                "provider_receipt": payload.get("provider_receipt") or payload.get("message_id"),
                "text": adaptive_delivery_result_text({"event_type": "duplicate"}),
            }
        pending = next(
            (row for row in matching if row.get("event_type") == "audit_pending"),
            None,
        )
        delivered = next(
            (
                row
                for row in matching
                if row.get("event_type") == "delivered"
                or (
                    row.get("event_type") in {"delivery_receipt_recorded", "delivery_attempt_started"}
                    and isinstance(row.get("payload"), Mapping)
                    and row["payload"].get("provider_receipt") is not None
                )
            ),
            None,
        )
        existing = pending or delivered
        if existing is None:
            return None
        payload = existing.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        return {
            **dict(existing),
            "event_type": "audit_pending",
            "status": "audit_pending",
            "delivery_id": payload.get("delivery_id"),
            "provider_receipt": payload.get("provider_receipt") or payload.get("message_id"),
            "text": adaptive_delivery_result_text({"event_type": "audit_pending"}),
        }

    def _existing_delivery_status(
        self,
        proposal_digest: str,
    ) -> Mapping[str, object] | None:
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock, locked():
            return self._delivery_status_without_reconciliation(
                self.store.read(),
                proposal_digest,
            )

    def _reconcile_delivery_receipts_authorized(
        self,
        proposal_digest: str | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Complete durable delivery audits without invoking the provider."""
        self._require_non_diagnostic_delivery_runtime()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        completed: list[Mapping[str, object]] = []
        with self._authority_lock(), self._lifecycle_lock:
            live_delivery_pins: Mapping[str, object] | None = None
            if self._production_mode:
                (
                    _customer,
                    _data_root,
                    _spec,
                    _events,
                    source_digest,
                    artifacts,
                    _epoch_path,
                    _epoch,
                    registration_binding,
                ) = self._production_context()
                live_delivery_pins = {
                    "source_digest": source_digest,
                    "policy_digest": artifacts.policy_digest,
                    "meal_constraints_digest": registration_binding.meal_constraints_digest,
                    "catalog_digest": artifacts.catalog_digest,
                    "registration_digest": registration_binding.registration_digest,
                    "authority_digest": self._authority_digest(_spec),
                }
            with locked():
                rows = list(self.store.read())
                audited_ids = {
                    str(row["payload"].get("delivery_id"))
                    for row in rows
                    if row.get("event_type") == "sent_audited"
                    and isinstance(row.get("payload"), Mapping)
                    and isinstance(row["payload"].get("delivery_id"), str)
                    and row["payload"].get("delivery_id")
                }
                delivery_ids = {
                    str(row["payload"].get("delivery_id"))
                    for row in rows
                    if row.get("event_type") in {"delivery_attempt_started", "delivery_receipt_recorded", "delivered", "audit_pending"}
                    and isinstance(row.get("payload"), Mapping)
                    and isinstance(row["payload"].get("delivery_id"), str)
                    and row["payload"].get("delivery_id")
                }

                def receipt_id(payload: object) -> str:
                    if not isinstance(payload, Mapping):
                        raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
                    values: list[str] = []
                    for field in ("provider_receipt", "message_id"):
                        value = payload.get(field)
                        if value is None:
                            continue
                        parsed = self._delivery_receipt_id(value)
                        if not parsed:
                            raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
                        values.append(parsed)
                    if len(set(values)) > 1:
                        raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
                    return values[0] if values else ""

                for delivery_id in sorted(delivery_ids):
                    delivery_rows = [
                        row
                        for row in rows
                        if isinstance(row.get("payload"), Mapping)
                        and row["payload"].get("delivery_id") == delivery_id
                    ]
                    known_proposal_digests = {
                        str(row["payload"].get("proposal_digest"))
                        for row in delivery_rows
                        if isinstance(row.get("payload"), Mapping)
                        and isinstance(row["payload"].get("proposal_digest"), str)
                        and row["payload"].get("proposal_digest")
                    }
                    if (
                        proposal_digest is not None
                        and known_proposal_digests
                        and proposal_digest not in known_proposal_digests
                    ):
                        continue
                    pending_rows = [
                        row for row in delivery_rows if row.get("event_type") == "audit_pending"
                    ]
                    pending_receipts = {
                        receipt_id(row.get("payload"))
                        for row in pending_rows
                    }
                    if any(not value for value in pending_receipts):
                        raise AdaptiveWorkflowError("adaptive delivery receipt is incomplete")
                    receipt_candidates: set[str] = set()
                    for row in delivery_rows:
                        event_type = row.get("event_type")
                        payload = row.get("payload")
                        if event_type not in {
                            "delivery_attempt_started",
                            "delivery_receipt_recorded",
                            "delivered",
                            "audit_pending",
                            "sent_audited",
                        } or not isinstance(payload, Mapping):
                            continue
                        if payload.get("provider_receipt") is None and payload.get("message_id") is None:
                            continue
                        value = receipt_id(payload)
                        if not value:
                            raise AdaptiveWorkflowError("adaptive delivery receipt is incomplete")
                        receipt_candidates.add(value)
                    if len(receipt_candidates) > 1:
                        raise AdaptiveWorkflowError("adaptive delivery receipt conflict")
                    if delivery_id in audited_ids:
                        continue
                    attempts = [
                        row["payload"]
                        for row in delivery_rows
                        if row.get("event_type") == "delivery_attempt_started"
                        and isinstance(row.get("payload"), Mapping)
                        and row["payload"].get("provider_receipt") is None
                        and row["payload"].get("message_id") is None
                    ]
                    if len(attempts) != 1:
                        raise AdaptiveWorkflowError("adaptive delivery receipt is incomplete")
                    attempt = attempts[0]
                    selected = attempt.get("proposal_digest")
                    if not isinstance(selected, str) or not selected:
                        raise AdaptiveWorkflowError("adaptive delivery receipt is invalid")
                    if proposal_digest is not None and selected != proposal_digest:
                        raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")
                    if any(
                        isinstance(row.get("payload"), Mapping)
                        and "proposal_digest" in row["payload"]
                        and row["payload"].get("proposal_digest") != selected
                        for row in delivery_rows
                        if row.get("event_type") in {
                            "delivery_attempt_started",
                            "delivery_receipt_recorded",
                            "delivered",
                            "audit_pending",
                            "sent_audited",
                        }
                    ):
                        raise AdaptiveWorkflowError("adaptive delivery receipt pins are stale")
                    if not receipt_candidates:
                        raise AdaptiveWorkflowError("adaptive delivery receipt is incomplete")
                    proposal = self._proposal_for_digest(selected)
                    self._validate_delivery_record(proposal, attempt)
                    if live_delivery_pins is not None and any(
                        attempt.get(field) != expected
                        for field, expected in live_delivery_pins.items()
                    ):
                        raise AdaptiveWorkflowError("adaptive delivery pins are stale")
                    completed.append(
                        self._reconcile_delivery_locked(
                            rows=rows,
                            proposal=proposal,
                            attempt_payload=attempt,
                            message_id=next(iter(receipt_candidates)),
                        )
                    )
        return tuple(completed)
    def reconcile_delivery_receipts(
        self,
        proposal_digest: str | None = None,
        *,
        operator_id: object | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Reconcile only with a durable action=reconcile capability."""
        self._require_non_diagnostic_delivery_runtime()
        if self._production_mode:
            if not isinstance(operator_id, AdaptiveOperatorCapability):
                raise AdaptiveWorkflowError(
                    "adaptive reconciliation requires a typed operator capability "
                    "with action=reconcile"
                )
            if operator_id.action != "reconcile":
                raise AdaptiveWorkflowError("adaptive capability action is not authorized")
            capability_digest = operator_id.proposal_digest
            if (
                not isinstance(capability_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", capability_digest) is None
            ):
                raise AdaptiveWorkflowError("adaptive reconciliation proposal pin is invalid")
            if proposal_digest is None:
                proposal_digest = capability_digest
            elif proposal_digest != capability_digest:
                raise AdaptiveWorkflowError("adaptive reconciliation proposal pin is stale")
            self._require_operator_owner(operator_id, required_action="reconcile")
        return self._reconcile_delivery_receipts_authorized(proposal_digest)
    def _append_delivery_audit_pending(
        self,
        *,
        delivery_id: str,
        proposal_digest: str,
        attempt_payload: Mapping[str, object],
        provider_payload: Mapping[str, object],
        delivered_event_id: object = None,
        reason: str = "audit_persistence_failed",
        _under_store_lock: bool = False,
    ) -> Mapping[str, object]:
        """Retain a durable provider receipt when the audit append is unavailable."""
        self._require_non_diagnostic_delivery_runtime()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        if not _under_store_lock:
            with self._authority_lock(), self._lifecycle_lock, locked():
                return self._append_delivery_audit_pending(
                    delivery_id=delivery_id,
                    proposal_digest=proposal_digest,
                    attempt_payload=attempt_payload,
                    provider_payload=provider_payload,
                    delivered_event_id=delivered_event_id,
                    reason=reason,
                    _under_store_lock=True,
                )
        with (locked() if not _under_store_lock else nullcontext()):
            rows = list(self.store.read())
            for row in rows:
                payload = row.get("payload") if isinstance(row, Mapping) else None
                if (
                    isinstance(payload, Mapping)
                    and row.get("event_type") == "audit_pending"
                    and payload.get("delivery_id") == delivery_id
                ):
                    return row
            payload = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "proposal_digest": proposal_digest,
                "provider_receipt": provider_payload.get(
                    "message_id", provider_payload.get("provider_receipt")
                ),
                "message_id": provider_payload.get(
                    "message_id", provider_payload.get("provider_receipt")
                ),
                "delivered_event_id": delivered_event_id,
                "attempt_event_id": attempt_payload.get("attempt_event_id"),
                "reason": reason,
                "registration_digest": attempt_payload.get("registration_digest"),
                "source_digest": attempt_payload.get("source_digest"),
                "policy_digest": attempt_payload.get("policy_digest"),
                "meal_constraints_digest": attempt_payload.get("meal_constraints_digest"),
                "catalog_digest": attempt_payload.get("catalog_digest"),
                "authority_digest": attempt_payload.get("authority_digest"),
            }
            return self._append_locked(
                self.store,
                "audit_pending",
                payload,
                dedupe_key=f"production-audit-pending:{delivery_id}",
            )
    def _append_delivery_preflight_rejected(
        self,
        *,
        delivery_id: str,
        proposal_digest: str,
        reason: str,
        registration_digest: str | None = None,
        attempt_event_id: str | None = None,
    ) -> Mapping[str, object]:
        self._require_non_diagnostic_delivery_runtime()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock, locked():
            payload: dict[str, object] = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "proposal_digest": proposal_digest,
                "reason": reason,
                "registration_digest": registration_digest,
            }
            if attempt_event_id is not None:
                payload["attempt_event_id"] = attempt_event_id
            return self._append_locked(
                self.store,
                "delivery_preflight_rejected",
                payload,
                dedupe_key=f"production-preflight-rejected:{delivery_id}:{reason}",
            )
    def _append_delivery_unknown(
        self,
        *,
        delivery_id: str,
        proposal_digest: str,
        reason: str,
        registration_digest: str | None = None,
        attempt_event_id: str | None = None,
    ) -> Mapping[str, object]:
        self._require_non_diagnostic_delivery_runtime()
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock, locked():
            rows = list(self.store.read())
            for row in rows:
                payload = row.get("payload") if isinstance(row, Mapping) else None
                if (
                    isinstance(payload, Mapping)
                    and payload.get("delivery_id") == delivery_id
                    and row.get("event_type") == "delivery_unknown"
                ):
                    return row
            payload: dict[str, object] = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "proposal_digest": proposal_digest,
                "reason": reason,
                "registration_digest": registration_digest,
            }
            if attempt_event_id is not None:
                payload["attempt_event_id"] = attempt_event_id
            return self._append_locked(
                self.store,
                "delivery_unknown",
                payload,
                dedupe_key=f"production-unknown:{delivery_id}",
            )

    @staticmethod
    def _append_locked(
        store: object,
        event_type: str,
        payload: Mapping[str, object],
        *,
        dedupe_key: str,
    ) -> Mapping[str, object]:
        """Append one event while ``store.locked()`` is held.

        ``AdaptiveEventStore.append`` acquires the same flock again, which
        deadlocks when called from its ``locked`` context.  Write the already
        normalized event directly for the profile store; lightweight test
        stores can retain their normal append implementation.
        """
        path_value = getattr(store, "path", None)
        if not isinstance(path_value, (str, Path)):
            return store.append(event_type, payload, dedupe_key=dedupe_key)
        path = Path(path_value)
        try:
            normalized_payload = json.loads(canonical_json(dict(payload)))
            existing_rows = store.read()
            for existing in existing_rows:
                if (
                    isinstance(existing, Mapping)
                    and existing.get("dedupe_key") == dedupe_key
                ):
                    if (
                        existing.get("event_type") != event_type
                        or existing.get("payload") != normalized_payload
                    ):
                        raise AdaptiveWorkflowError("adaptive ledger dedupe conflict")
                    return existing
            body = {
                "event_type": event_type,
                "payload": normalized_payload,
                "dedupe_key": dedupe_key,
            }
            row = {**body, "event_id": digest(body)}
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o600)
            return row
        except (OSError, TypeError, ValueError) as exc:
            raise AdaptiveWorkflowError("adaptive delivery reservation failed") from exc
    async def deliver_latest_once(
        self,
        proposal_digest: str | None = None,
        *,
        expected_digest: str | None = None,
        topic_id: object = OPERATOR_REVIEW_TOPIC_ID,
        operator_id: str = "richard",
        strict_sender: Callable[[str, str, str], object] | None = None,
        destination: object | None = None,
        chat_id: str | None = None,
    ) -> Mapping[str, object]:
        """Deliver once through the registered customer transport.

        The durable attempt reservation is committed while the adaptive
        ledger lock is held.  The lock is released before the single provider
        invocation, so a slow network call cannot block unrelated lifecycle
        reads or reservations.
        """
        self._require_non_diagnostic_delivery_runtime()
        self._topic(topic_id)
        selected = proposal_digest if proposal_digest is not None else expected_digest
        if (
            selected is None
            or (
                proposal_digest is not None
                and expected_digest is not None
                and proposal_digest != expected_digest
            )
        ):
            raise AdaptiveWorkflowError("adaptive proposal digest is required")
        if strict_sender is not None:
            raise AdaptiveWorkflowError("production delivery requires registered customer transport")
        reconcile_only = (
            isinstance(operator_id, AdaptiveOperatorCapability)
            and operator_id.action == "reconcile"
        )
        actor = self._require_operator_owner(
            operator_id,
            required_action="reconcile" if reconcile_only else "send",
        )
        if reconcile_only:
            if (
                not isinstance(operator_id.proposal_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", operator_id.proposal_digest) is None
                or operator_id.proposal_digest != selected
            ):
                raise AdaptiveWorkflowError("adaptive reconciliation proposal pin is stale")
            reconciled = self._reconcile_delivery_receipts_authorized(selected)
            if reconciled:
                return reconciled[-1]
            raise AdaptiveWorkflowError("no adaptive delivery audit is pending")
        existing_status = self._existing_delivery_status(selected)
        if existing_status is not None:
            return existing_status
        with self._authority_lock(), self._lifecycle_lock:
            proposal = self.revalidate_transition("deliver", selected)
            (
                _customer,
                _data_root,
                spec,
                _events,
                _source,
                _artifacts,
                _epoch_path,
                _epoch,
                registration_binding,
            ) = self._production_context(require_activation=True, require_delivery=True)
            registered = getattr(spec, "telegram", None)
            if registered is None:
                raise AdaptiveWorkflowError("registered customer destination is unavailable")
            registered_key = tuple(
                str(getattr(registered, field, "") or "").strip()
                for field in ("user_id", "chat_id", "topic_id")
            )
            if not all(registered_key):
                raise AdaptiveWorkflowError("registered customer destination is unavailable")
            if chat_id is not None and str(chat_id) != registered_key[1]:
                raise AdaptiveWorkflowError("adaptive delivery destination is not canonical")
            if destination is not None:
                supplied_key = tuple(
                    str(getattr(destination, field, "") or "")
                    for field in ("user_id", "chat_id", "topic_id")
                )
                if supplied_key != registered_key:
                    raise AdaptiveWorkflowError("adaptive delivery destination is not canonical")
            delivery_id = hashlib.sha256(
                f"{proposal.digest}\0{registered_key[0]}\0{registered_key[1]}\0{registered_key[2]}".encode(
                    "utf-8"
                )
            ).hexdigest()
            pins = self._proposal_body_pins(proposal, spec)
            reader = getattr(_events, "_read_events", None)
            if not callable(reader):
                raise AdaptiveWorkflowError("canonical customer evidence is unavailable")
            rendered = self._registered_daily_projection(
                proposal,
                canonical_events=reader(),
                as_of_kst_day=proposal.snapshot.evaluation_day,
            )
            rendered_digest = self._text_digest(rendered)
            source_pins = self._proposal_pins(proposal)
            attempt_payload = {
                "customer_key": self.customer_key,
                "delivery_id": delivery_id,
                "reservation_id": delivery_id,
                "reservation_state": "committed",
                "proposal_digest": proposal.digest,
                "customer_body": rendered,
                "destination": {
                    "user_id": registered_key[0],
                    "chat_id": registered_key[1],
                    "topic_id": registered_key[2],
                },
                "topic_id": registered_key[2],
                "rendered_digest": rendered_digest,
                "customer_body_digest": rendered_digest,
                "operator_body_digest": pins["operator_body_digest"],
                "meal_plan_digest": pins["meal_plan_digest"],
                "meal_digest": pins["meal_digest"],
                "source_digest": source_pins[0],
                "registration_digest": registration_binding.registration_digest,
                "policy_digest": source_pins[1],
                "meal_constraints_digest": source_pins[2],
                "catalog_digest": source_pins[3],
                "authority_digest": pins["authority_digest"],
                **self._risk_policy_evidence(),
                "epoch": dict(_epoch),
                "epoch_digest": self._epoch_digest(_epoch),
                "activation_proposal_digest": proposal.digest,
                "delivery_epoch": _epoch.get("delivery_epoch", _epoch.get("epoch", 0)),
                "operator_id": actor,
                "operator_address": list(self._live_owner_key()),
                **{
                    key: self._operator_audit_fields()[key]
                    for key in (
                        "authenticated_review_operator",
                        "review_operator_version",
                        "canonical_owner_snapshot",
                        "canonical_owner_version",
                    )
                },
                "execution_mode": "production",
            }
            locked = getattr(self.store, "locked", None)
            if not callable(locked):
                raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
            try:
                with locked():
                    rows = list(self.store.read())
                    if self._committed_transition(
                        rows,
                        action="activate",
                        proposal_digest=proposal.digest,
                    ) is None:
                        raise AdaptiveWorkflowError("adaptive activation is not committed")
                    delivery_rows = [
                        row for row in rows
                        if isinstance(row, Mapping)
                        and isinstance(row.get("payload"), Mapping)
                        and row["payload"].get("delivery_id") == delivery_id
                    ]
                    existing_status = self._delivery_status_without_reconciliation(
                        delivery_rows,
                        selected,
                    )
                    if existing_status is not None:
                        return existing_status
                    if any(
                        row.get("event_type") == "delivery_unknown"
                        for row in delivery_rows
                    ):
                        raise AdaptiveWorkflowError("adaptive delivery already attempted")
                    if delivery_rows:
                        persisted_attempt_row = next(
                            (
                                row
                                for row in delivery_rows
                                if row.get("event_type") == "delivery_attempt_started"
                                and isinstance(row.get("payload"), Mapping)
                            ),
                            None,
                        )
                        if persisted_attempt_row is None:
                            raise AdaptiveWorkflowError(
                                "adaptive delivery reservation is incomplete"
                            )
                        persisted_attempt = persisted_attempt_row["payload"]
                        if any(
                            persisted_attempt.get(key) != attempt_payload.get(key)
                            for key in (
                                "delivery_id",
                                "proposal_digest",
                                "customer_body_digest",
                                "destination",
                                "epoch_digest",
                                "registration_digest",
                                "source_digest",
                                "policy_digest",
                                "meal_constraints_digest",
                                "catalog_digest",
                                "authority_digest",
                                "risk_policy_version",
                                "risk_policy_digest",
                                "risk_policy_document_digest",
                            )
                        ):
                            raise AdaptiveWorkflowError(
                                "adaptive delivery reservation pins are stale"
                            )
                        if not any(
                            row.get("event_type") == "delivery_preflight_rejected"
                            for row in delivery_rows
                        ):
                            raise AdaptiveWorkflowError(
                                "adaptive delivery already attempted"
                            )
                        reservation_event_id = persisted_attempt_row.get("event_id")
                        attempt_payload = {
                            **persisted_attempt,
                            "attempt_event_id": reservation_event_id,
                        }
                    else:
                        reservation_row = self._append_locked(
                            self.store,
                            "delivery_attempt_started",
                            attempt_payload,
                            dedupe_key=f"production-attempt:{delivery_id}",
                        )
                        reservation_event_id = (
                            reservation_row.get("event_id")
                            if isinstance(reservation_row, Mapping)
                            else None
                        )
                        attempt_payload = {
                            **attempt_payload,
                            "attempt_event_id": reservation_event_id,
                        }
            except AdaptiveWorkflowError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise AdaptiveWorkflowError("adaptive delivery reservation failed") from exc

        try:
            with self._authority_lock(), self._lifecycle_lock:
                self.revalidate_transition("deliver", selected)
                self._require_operator_owner(operator_id, required_action="send")
                (
                    _live_customer,
                    _live_data_root,
                    live_spec,
                    _live_events,
                    _live_source_digest,
                    _live_artifacts,
                    _live_epoch_path,
                    _live_epoch,
                    _live_registration,
                ) = self._production_context(
                    require_activation=True,
                    require_delivery=True,
                )
                live_destination = getattr(live_spec, "telegram", None)
                live_key = (
                    tuple(
                        str(getattr(live_destination, field, "") or "").strip()
                        for field in ("user_id", "chat_id", "topic_id")
                    )
                    if live_destination is not None
                    else ()
                )
                reserved_key = tuple(
                    str(attempt_payload["destination"].get(field, "") or "").strip()
                    for field in ("user_id", "chat_id", "topic_id")
                )
                if live_key != reserved_key:
                    raise AdaptiveWorkflowError(
                        "adaptive delivery reservation destination is stale"
                    )
        except Exception as exc:
            detail = str(exc).lower()
            try:
                _epoch_path_after_failure, epoch_after_failure = self._feature_epoch(_data_root)
            except AdaptiveWorkflowError:
                epoch_after_failure = {}
            delivery_was_revoked = (
                epoch_after_failure.get("delivery") is not True
                or epoch_after_failure.get("activation") is not True
            )
            reason = (
                "owner_changed_after_reservation"
                if "owner" in detail or "authority" in detail
                else "delivery_revoked_after_reservation"
                if delivery_was_revoked or any(
                    token in detail for token in ("delivery", "activation", "config", "epoch")
                )
                else "post_reservation_revalidation_failed"
            )
            self._append_delivery_preflight_rejected(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason=reason,
            )
            raise AdaptiveWorkflowError(
                "adaptive delivery authority changed after reservation"
            ) from exc
        transport = self.customer_transport
        sender = (
            getattr(transport, "send_adaptive_customer", None)
            if transport is not None
            else None
        )
        if not callable(sender):
            return self._append_delivery_preflight_rejected(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason="transport_unavailable",
            )
        try:
            receipt = sender(
                self.customer_key,
                registered,
                reservation_id=delivery_id,
            )
            if inspect.isawaitable(receipt):
                receipt = await receipt
        except AdaptiveTransportPreflightRejected as exc:
            return self._append_delivery_preflight_rejected(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason=str(exc)[:160] or "transport_preflight_rejected",
            )
        except asyncio.CancelledError:
            return self._append_delivery_unknown(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason="cancelled",
            )
        except Exception as exc:
            try:
                consumed = any(
                    isinstance(row, Mapping)
                    and row.get("event_type") == "delivery_attempt_consumed"
                    and isinstance(row.get("payload"), Mapping)
                    and row["payload"].get(
                        "reservation_id",
                        row["payload"].get("delivery_id"),
                    )
                    == delivery_id
                    for row in self.store.read()
                )
            except (OSError, TypeError, ValueError):
                consumed = True
            if not consumed:
                return self._append_delivery_preflight_rejected(
                    delivery_id=delivery_id,
                    proposal_digest=proposal.digest,
                    registration_digest=attempt_payload.get(
                        "registration_digest"
                    ),
                    attempt_event_id=reservation_event_id,
                    reason=type(exc).__name__,
                )
            return self._append_delivery_unknown(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason=type(exc).__name__,
            )
        message_id = self._receipt_message_id(receipt)
        if not message_id:
            return self._append_delivery_unknown(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                registration_digest=attempt_payload.get("registration_digest"),
                attempt_event_id=reservation_event_id,
                reason="invalid_provider_receipt",
            )
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        def mark_audit_pending_after_provider_receipt() -> Mapping[str, object] | None:
            try:
                return self._append_delivery_audit_pending(
                    delivery_id=delivery_id,
                    proposal_digest=proposal.digest,
                    attempt_payload=attempt_payload,
                    provider_payload={
                        "ok": True,
                        "message_id": message_id,
                        "provider_receipt": message_id,
                    },
                    delivered_event_id=None,
                    reason="post_provider_reconciliation_failed",
                )
            except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                return None

        def audit_pending_response(pending: Mapping[str, object] | None) -> Mapping[str, object]:
            response: dict[str, object] = {
                "event_type": "audit_pending",
                "status": "audit_pending",
                "delivery_id": delivery_id,
                "provider_receipt": message_id,
                "text": adaptive_delivery_result_text({"event_type": "audit_pending"}),
            }
            if isinstance(pending, Mapping):
                response = {**dict(pending), **response}
            return response

        try:
            with self._authority_lock(), self._lifecycle_lock, locked():
                rows = list(self.store.read())
                return self._reconcile_delivery_locked(
                    rows=rows,
                    proposal=proposal,
                    attempt_payload=attempt_payload,
                    message_id=message_id,
                )
        except AdaptiveWorkflowError:
            pending = mark_audit_pending_after_provider_receipt()
            return audit_pending_response(pending)
        except (OSError, TypeError, ValueError):
            pending = mark_audit_pending_after_provider_receipt()
            return audit_pending_response(pending)
    def deliver_latest_once_sync(self, *args: object, **kwargs: object) -> Mapping[str, object]:
        """Compatibility wrapper for callers that are not already async."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.deliver_latest_once(*args, **kwargs))
        raise AdaptiveWorkflowError("sync adaptive delivery cannot run in an event loop")
    def _approval_exists(self, proposal: NutritionProposal) -> bool:
        for row in self.store.read():
            if row.get("event_type") != "plan_approved":
                continue
            payload = row.get("payload")
            if isinstance(payload, Mapping) and payload.get("proposal_digest") == proposal.digest:
                return True
        return False

    def deliver_approved_once(
        self,
        proposal: NutritionProposal,
        *,
        topic_id: object,
        chat_id: str,
        operator_id: str,
        strict_sender: Callable[[str, int, str], Mapping[str, object]],
    ) -> Mapping[str, object]:
        """Attempt one strict-topic delivery for an exact approved revision.

        ``strict_sender`` must perform one provider invocation without retries or
        topic fallback and return a receipt containing one ``message_id``.
        Provider exceptions or malformed receipts are recorded as permanently
        unknown; a valid receipt whose audit append fails remains audit_pending.
        """
        self._require_non_diagnostic_delivery_runtime()
        self._topic(topic_id)
        if self._production_mode:
            raise AdaptiveWorkflowError("production delivery requires deliver_latest_once")
        self._ensure_proposal(proposal)
        if self._latest_identity() != (proposal.digest, proposal.revision):
            raise AdaptiveWorkflowError("stale adaptive proposal revision")
        if not self._approval_exists(proposal):
            raise AdaptiveWorkflowError("adaptive proposal is not approved")
        delivery_id = hashlib.sha256(
            f"{proposal.digest}\0{chat_id}\0{self.operator_topic_id}".encode("utf-8")
        ).hexdigest()
        rendered = proposal.customer_body
        if (
            not isinstance(rendered, str)
            or not rendered.strip()
            or not isinstance(proposal.customer_body_digest, str)
            or self._text_digest(rendered) != proposal.customer_body_digest
        ):
            raise AdaptiveWorkflowError("adaptive customer body is unavailable")
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        attempt_payload = {
            "delivery_id": delivery_id,
            "proposal_digest": proposal.digest,
            "chat_id": str(chat_id),
            "topic_id": self.operator_topic_id,
            "rendered_digest": proposal.customer_body_digest,
            "customer_body_digest": proposal.customer_body_digest,
            "meal_plan_digest": (
                proposal.meal_plan.digest if proposal.meal_plan is not None else None
            ),
            "operator_id": str(operator_id),
        }
        with self._authority_lock(), self._lifecycle_lock, locked():
            rows = self.store.read()
            for row in rows:
                payload = row.get("payload")
                if not isinstance(payload, Mapping) or payload.get("delivery_id") != delivery_id:
                    continue
                if row.get("event_type") in {
                    "delivery_attempt_started", "delivery_unknown", "delivered", "sent_audited",
                    "audit_pending",
                }:
                    raise AdaptiveWorkflowError("adaptive delivery already attempted")
            self._append_locked(
                self.store,
                "delivery_attempt_started",
                attempt_payload,
                dedupe_key=f"adaptive-attempt:{delivery_id}",
            )
        try:
            receipt = strict_sender(str(chat_id), self.operator_topic_id, rendered)
        except Exception as exc:
            return self._append_delivery_unknown(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                reason=type(exc).__name__,
            )
        message_id = self._receipt_message_id(receipt)
        if not message_id:
            return self._append_delivery_unknown(
                delivery_id=delivery_id,
                proposal_digest=proposal.digest,
                reason="invalid_provider_receipt",
            )
        delivered_payload = {
            "delivery_id": delivery_id,
            "message_id": message_id,
            "provider_receipt": message_id,
            "chat_id": str(chat_id),
            "topic_id": self.operator_topic_id,
            "customer_body_digest": proposal.customer_body_digest,
            "meal_plan_digest": (
                proposal.meal_plan.digest if proposal.meal_plan is not None else None
            ),
        }
        with self._authority_lock(), self._lifecycle_lock, locked():
            try:
                delivered = self._append_locked(
                    self.store,
                    "delivered",
                    delivered_payload,
                    dedupe_key=f"adaptive-delivered:{delivery_id}",
                )
            except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                try:
                    pending = self._append_delivery_audit_pending(
                        delivery_id=delivery_id,
                        proposal_digest=proposal.digest,
                        attempt_payload=attempt_payload,
                        provider_payload={
                            "message_id": message_id,
                            "provider_receipt": message_id,
                        },
                        delivered_event_id=None,
                        _under_store_lock=True,
                    )
                except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                    return {
                        "event_type": "audit_pending",
                        "status": "audit_pending",
                        "delivery_id": delivery_id,
                        "provider_receipt": message_id,
                        "text": adaptive_delivery_result_text(
                            {"event_type": "audit_pending"}
                        ),
                    }
                return {
                    **dict(pending),
                    "event_type": "audit_pending",
                    "status": "audit_pending",
                    "text": adaptive_delivery_result_text({"event_type": "audit_pending"}),
                }
            try:
                audited = self._append_locked(
                    self.store,
                    "sent_audited",
                    {"delivery_id": delivery_id, "delivered_event_id": delivered["event_id"]},
                    dedupe_key=f"adaptive-sent-audit:{delivery_id}",
                )
            except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                try:
                    pending = self._append_delivery_audit_pending(
                        delivery_id=delivery_id,
                        proposal_digest=proposal.digest,
                        attempt_payload=attempt_payload,
                        provider_payload={
                            "message_id": message_id,
                            "provider_receipt": message_id,
                        },
                        delivered_event_id=delivered.get("event_id"),
                        _under_store_lock=True,
                    )
                except (AdaptiveWorkflowError, OSError, TypeError, ValueError):
                    return {
                        "event_type": "audit_pending",
                        "status": "audit_pending",
                        "delivery_id": delivery_id,
                        "provider_receipt": message_id,
                        "text": adaptive_delivery_result_text(
                            {"event_type": "audit_pending"}
                        ),
                    }
                return {
                    **dict(pending),
                    "event_type": "audit_pending",
                    "status": "audit_pending",
                    "text": adaptive_delivery_result_text({"event_type": "audit_pending"}),
                }
        return _audited_delivery_result(audited)

    def issue_callback(self, proposal: NutritionProposal, *, action: str) -> str:
        if self._production_mode:
            raise AdaptiveWorkflowError(
                "production adaptive callbacks require persisted operator sessions"
            )
        allowed = {"view", "edit", "hold", "release", "approve", "activate", "send", "reconcile"}
        if action not in allowed:
            raise AdaptiveWorkflowError("adaptive callback action is invalid")
        token = hashlib.sha256(
            f"{self.customer_key}\0{proposal.digest}\0{action}".encode("utf-8")
        ).hexdigest()[:24]
        value = f"an1:{token}:{action}"
        if len(value.encode("utf-8")) > 64:
            raise AdaptiveWorkflowError("adaptive callback exceeds Telegram limit")
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock, locked():
            self._append_locked(
                self.store,
                "callback_issued",
                {
                    "token": token,
                    "action": action,
                    "proposal_digest": proposal.digest,
                    "revision": proposal.revision,
                    "rendered_card": render_operator_card(proposal),
                    "topic_id": 59,
                },
                dedupe_key=f"adaptive-callback:{token}:{action}",
            )
        return value

    def handle_callback_token(
        self,
        value: str,
        *,
        topic_id: object,
        operator_id: str,
    ) -> Mapping[str, object]:
        if self._production_mode:
            raise AdaptiveWorkflowError(
                "production adaptive callbacks require persisted operator sessions"
            )
        self._topic(topic_id)
        match = re.fullmatch(r"an1:([a-f0-9]{24}):(view|edit|hold|release|approve|activate|send|reconcile)", value)
        if match is None:
            raise AdaptiveWorkflowError("adaptive callback is invalid")
        token, action = match.groups()
        issued = None
        for row in self.store.read():
            payload = row.get("payload")
            if (
                row.get("event_type") == "callback_issued"
                and isinstance(payload, Mapping)
                and payload.get("token") == token
                and payload.get("action") == action
            ):
                issued = payload
        if issued is None:
            raise AdaptiveWorkflowError("adaptive callback is stale")
        digest_value = issued.get("proposal_digest")
        revision = issued.get("revision")
        if self._latest_identity() != (digest_value, revision):
            raise AdaptiveWorkflowError("adaptive callback revision is stale")
        if action == "view":
            return {"status": "view", "text": issued.get("rendered_card", "")}
        if action in {"hold", "release"}:
            if action == "hold":
                return self.hold_latest(
                    str(digest_value),
                    topic_id=topic_id,
                    operator_id=operator_id,
                )
            return self.release_latest(
                str(digest_value),
                topic_id=topic_id,
                operator_id=operator_id,
            )
        if action in {"edit", "reconcile", "send"}:
            return {
                "status": "operator_input_required",
                "action": action,
                "proposal_digest": digest_value,
            }
        proposal = self._proposal_for_digest(digest_value)
        if action == "approve":
            # Shadow/test callbacks still use the checked approval path so a
            # HUMAN_REVIEW proposal can never be approved by a callback.
            return self.approve(
                proposal,
                topic_id=topic_id,
                operator_id=operator_id,
                expected_digest=proposal.digest,
            )
        decision = getattr(proposal.decision, "value", proposal.decision)
        if str(decision) == "human_review":
            raise AdaptiveWorkflowError("safety hold requires human review")
        rows = self.store.read()
        if not self._has_event(rows, "plan_approved", proposal.digest):
            raise AdaptiveWorkflowError("adaptive proposal is not approved")
        # This append is retained only for the legacy shadow/test callback API.
        locked = getattr(self.store, "locked", None)
        if not callable(locked):
            raise AdaptiveWorkflowError("adaptive event store lock is unavailable")
        with self._authority_lock(), self._lifecycle_lock, locked():
            return self._append_locked(
                self.store,
                "adaptive_plan_effective",
                {
                    "proposal_digest": proposal.digest,
                    "revision": proposal.revision,
                    "operator_id": str(operator_id),
                    "topic_id": OPERATOR_REVIEW_TOPIC_ID,
                },
                dedupe_key=f"adaptive_plan_effective:{proposal.digest}:{operator_id}",
            )

    # Explicit aliases keep the gateway surface discoverable without exposing a
    # generic customer-send method.
    create_adaptive_proposal = create_proposal
    edit_proposal = revise_note
    approve_proposal = approve
    render_card = render_proposal
    render_adaptive_card = render_proposal


def prepare_adaptive_nutrition_runtime(
    profile_root: Path,
    customer_key: str,
    *,
    extension_through: date | None = None,
) -> Mapping[str, str]:
    """Prepare one registered customer's private adaptive runtime idempotently."""
    try:
        from checkin_cli.customer_admin import prepare_adaptive_nutrition_runtime as prepare

        return prepare(
            Path(profile_root),
            customer_key,
            extension_through=extension_through,
        )
    except Exception as exc:
        raise AdaptiveWorkflowError("adaptive runtime preparation failed") from exc


class NutritionCoachingIntegrationError(RuntimeError):
    """Raised when the production console cannot be wired safely."""


class CoordinatorEventSource:
    """Resolve canonical EventStore instances by the enabled customer key."""

    def __init__(self, coordinator: NutritionCoachingCoordinator, customer_key: str) -> None:
        self._coordinator = coordinator
        self._customer_key = customer_key

    def events_for(self, customer_key: str) -> object:
        if customer_key != self._customer_key:
            raise ValueError("canonical event source has no customer")
        customer = self._coordinator.customer(customer_key)
        source = self._coordinator.event_source(customer_key)
        if customer is None or source is None:
            raise ValueError("canonical EventStore is unavailable for customer")
        reader = getattr(source, "_read_events", None)
        if not callable(reader):
            raise ValueError("canonical event source is not an EventStore")
        expected_home = (customer.data_root / "wizard").resolve()
        actual_home = getattr(source, "_home", None)
        try:
            actual_path = Path(actual_home).resolve()
        except (TypeError, OSError, RuntimeError) as exc:
            raise ValueError("canonical EventStore root is unavailable") from exc
        if actual_path != expected_home:
            raise ValueError("canonical EventStore is not scoped to the customer")
        return source
@dataclass(frozen=True, slots=True)
class DualCoachReviewCard:
    """A read-only operator projection of one durable dual-coach review event."""

    card_id: str
    customer_key: str
    event_type: str
    text: str


class DualCoachReviewService:
    """Project customer-local dual-coach review events into the fixed operator topic."""

    _EVENT_TYPES = frozenset({"dual_coach_risk_review", "missing_checkin_reminder_review"})

    def __init__(self, coordinator: NutritionCoachingCoordinator, review_operator: object) -> None:
        if not isinstance(coordinator, NutritionCoachingCoordinator):
            raise AdaptiveWorkflowError("dual-coach review coordinator is unavailable")
        self._coordinator = coordinator
        self._review_operator = AdaptiveOperatorService._normalize_review_operator(review_operator)
        self._publication_lock = RLock()
        profile_root = getattr(coordinator, "profile_root", None)
        self._publication_ledger_path = (
            Path(profile_root) / "dual_coach_review_publications.jsonl"
            if profile_root is not None
            else None
        )

    def accepts(self, address: object) -> bool:
        key = getattr(address, "key", address)
        return isinstance(key, (tuple, list)) and tuple(str(value) for value in key) == self._review_operator

    @staticmethod
    def _value(row: object, name: str, default: object = None) -> object:
        return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)

    @classmethod
    def _event_parts(cls, row: object) -> tuple[str, Mapping[str, object], str] | None:
        event_type = cls._value(row, "event_type")
        event_type = getattr(event_type, "value", event_type)
        payload = cls._value(row, "payload", {})
        event_id = cls._value(row, "event_id", cls._value(row, "id", ""))
        if (
            not isinstance(event_type, str)
            or event_type not in cls._EVENT_TYPES
            or not isinstance(payload, Mapping)
            or not isinstance(event_id, str)
            or not event_id
        ):
            return None
        return event_type, payload, event_id

    @classmethod
    def _schedule_note(cls, events: tuple[object, ...]) -> str:
        for row in reversed(events):
            event_type = cls._value(row, "event_type")
            event_type = getattr(event_type, "value", event_type)
            if str(event_type) not in {"schedule_reference", "schedule_correction"}:
                continue
            reference = cls._value(row, "schedule_reference")
            note = cls._value(reference, "last_change_note")
            if isinstance(note, str) and note.strip():
                return note.strip()
            payload = cls._value(row, "payload", row)
            for source in (
                cls._value(payload, "schedule_reference", {}),
                cls._value(payload, "schedule", {}),
                payload,
            ):
                note = cls._value(source, "last_change_note")
                if isinstance(note, str) and note.strip():
                    return note.strip()
        return "기록 없음"

    @staticmethod
    def _text(value: object) -> str:
        return str(value).strip() if value is not None else ""

    @classmethod
    def _render(
        cls, event_type: str, payload: Mapping[str, object], event_id: str, schedule_note: str
    ) -> str:
        policy = "/".join(
            value
            for value in (
                cls._text(payload.get("policy_version")),
                cls._text(payload.get("policy_digest")),
                cls._text(payload.get("policy_document_digest")),
            )
            if value
        ) or "없음"
        if event_type == "dual_coach_risk_review":
            reasons = payload.get("reasons", ())
            reason_text = ", ".join(str(value) for value in reasons) if isinstance(reasons, (list, tuple)) else "없음"
            safety = "보류" if payload.get("held") is True else "검토"
            source = cls._text(payload.get("terminal_checkin_id")) or "없음"
            return (
                "듀얼 코치 위험 검토\n"
                f"이벤트: {event_id}\n원본 체크인: {source}\n사유: {reason_text or '없음'}\n"
                f"안전 상태: {safety}\n정책 핀: {policy}\n최근 일정 변경: {schedule_note}\n"
                "고객 발송 없음 — 운영자 검토 전용"
            )
        source = cls._text(payload.get("reminder_reservation_id")) or "없음"
        return (
            "미응답 체크인 알림 검토\n"
            f"이벤트: {event_id}\n알림 예약: {source}\n미응답 기간: {cls._text(payload.get('missing_window')) or '없음'}\n"
            f"응답 마감: {cls._text(payload.get('response_window_ends_at')) or '없음'}\n"
            f"정책 핀: {policy}\n최근 일정 변경: {schedule_note}\n고객 발송 없음 — 운영자 검토 전용"
        )

    @classmethod
    def _diagnostic_card(cls, customer_key: str, reason: str) -> DualCoachReviewCard:
        return DualCoachReviewCard(
            f"dual-coach-review-diagnostic:{customer_key}:{reason}",
            customer_key,
            "review_journal_diagnostic",
            "듀얼 코치 검토 기록을 안전하게 읽을 수 없습니다. "
            "고객 발송 없음 — 운영자 검토 전용",
        )

    def cards(self) -> tuple[DualCoachReviewCard, ...]:
        try:
            live = self._coordinator.refresh_live_registry()
        except Exception:
            live = False
        if live is not True:
            return (self._diagnostic_card("registry", "unavailable"),)
        cards: list[DualCoachReviewCard] = []
        diagnostics = 0
        for customer_key in sorted(getattr(self._coordinator, "_by_key", {})):
            try:
                from checkin_cli.customer_coaching import RegisteredCustomerDualCoachCoordinator

                customer = self._coordinator.customer(customer_key)
                if customer is None:
                    continue
                reviews = tuple(RegisteredCustomerDualCoachCoordinator(customer).adaptive_store.read())
                events = tuple(
                    CoordinatorEventSource(self._coordinator, customer_key).events_for(customer_key)._read_events()
                )
            except Exception:
                if diagnostics < 16:
                    cards.append(self._diagnostic_card(customer_key, "unavailable"))
                    diagnostics += 1
                continue
            schedule_note = self._schedule_note(events)
            for row in reviews:
                parts = self._event_parts(row)
                if parts is None:
                    continue
                event_type, payload, event_id = parts
                if self._text(payload.get("customer_key")) != customer_key:
                    continue
                cards.append(
                    DualCoachReviewCard(
                        f"dual-coach-review:{customer_key}:{event_id}",
                        customer_key,
                        event_type,
                        self._render(event_type, payload, event_id, schedule_note),
                    )
                )
        return tuple(cards)
    def _publication_rows(self) -> tuple[Mapping[str, object], ...]:
        """Read the append-only operator-publication fence."""
        path = self._publication_ledger_path
        if path is None or not path.is_file():
            return ()
        try:
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if not isinstance(row, Mapping):
                    raise ValueError("publication receipt row is invalid")
                rows.append(row)
            return tuple(rows)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AdaptiveWorkflowError("dual-coach review publication ledger is unavailable") from exc

    def _append_publication_row(self, row: Mapping[str, object]) -> None:
        path = self._publication_ledger_path
        if path is None:
            raise AdaptiveWorkflowError("dual-coach review publication ledger is unavailable")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AdaptiveWorkflowError("dual-coach review publication ledger is unavailable") from exc

    def claim_publication(self, card: DualCoachReviewCard) -> bool:
        """Durably consume a card before provider I/O; restart never replays a claim."""
        if not isinstance(card, DualCoachReviewCard):
            raise AdaptiveWorkflowError("dual-coach review card is invalid")
        with self._publication_lock:
            for row in self._publication_rows():
                if row.get("card_id") == card.card_id:
                    return False
            self._append_publication_row(
                {
                    "schema_version": 1,
                    "card_id": card.card_id,
                    "customer_key": card.customer_key,
                    "event_type": card.event_type,
                    "state": "claimed",
                }
            )
        return True

    def record_publication(self, card: DualCoachReviewCard, published_message_id: object) -> None:
        """Append the provider receipt after a successful operator-only publication."""
        if (
            isinstance(published_message_id, bool)
            or not isinstance(published_message_id, (str, int))
            or not str(published_message_id).strip()
        ):
            raise AdaptiveWorkflowError("dual-coach review publication receipt is invalid")
        with self._publication_lock:
            if not any(
                row.get("card_id") == card.card_id and row.get("state") == "claimed"
                for row in self._publication_rows()
            ):
                raise AdaptiveWorkflowError("dual-coach review publication was not claimed")
            self._append_publication_row(
                {
                    "schema_version": 1,
                    "card_id": card.card_id,
                    "state": "published",
                    "published_message_id": str(published_message_id).strip(),
                }
            )

    @staticmethod
    def publication_receipt(result: object) -> str:
        """Extract one Telegram receipt without accepting a customer transport result."""
        value = (
            result.get("message_id")
            if isinstance(result, Mapping)
            else getattr(result, "message_id", None)
        )
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AdaptiveWorkflowError("dual-coach review publication receipt is invalid")
        receipt = str(value).strip()
        if not receipt or len(receipt) > 128:
            raise AdaptiveWorkflowError("dual-coach review publication receipt is invalid")
        return receipt


class TelegramCustomerTransport:
    """Receipt-returning transport backed by one live Telegram adapter."""

    def __init__(self, adapter: object, coordinator: NutritionCoachingCoordinator) -> None:
        if not isinstance(coordinator, NutritionCoachingCoordinator):
            raise NutritionCoachingIntegrationError("live nutrition coordinator is required")
        strict_sender = getattr(adapter, "_send_message_strict_topic", None)
        if not callable(strict_sender):
            strict_sender = getattr(adapter, "_send_message_with_strict_topic", None)
        if not (
            callable(strict_sender)
            and callable(getattr(adapter, "_thread_kwargs_for_send", None))
        ):
            raise NutritionCoachingIntegrationError("live Telegram strict topic boundary is required")
        self._adapter = adapter
        self._coordinator = coordinator

    def _prepare_customer_send(
        self,
        customer_key: str,
        destination: object,
        text: str,
        *,
        adaptive: bool,
    ) -> tuple[Callable[..., object], dict[str, object]]:
        try:
            allowed = self._coordinator.customer_transport_allowed(
                customer_key,
                destination,
                adaptive=adaptive,
            )
        except TypeError as exc:
            if adaptive:
                raise RuntimeError(
                    "adaptive customer transport authority unavailable"
                ) from exc
            allowed = self._coordinator.customer_transport_allowed(
                customer_key,
                destination,
            )
        if not allowed:
            raise RuntimeError("customer transport authority unavailable")
        customer = self._coordinator.customer(customer_key)
        if customer is None:
            raise RuntimeError("customer route is unavailable")
        canonical = customer.spec.telegram
        destination_key = tuple(
            str(getattr(destination, field, "") or "")
            for field in ("user_id", "chat_id", "topic_id")
        )
        if destination_key != tuple(str(value) for value in canonical.key):
            raise RuntimeError("customer transport destination is not canonical")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("customer transport text is empty")
        if telegram_utf16_length(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT_UTF16:
            raise RuntimeError(
                "customer transport requires one Telegram message receipt"
            )
        chat_id = str(canonical.chat_id)
        topic_id = str(canonical.topic_id)
        strict_sender = getattr(self._adapter, "_send_message_strict_topic", None)
        if not callable(strict_sender):
            strict_sender = getattr(
                self._adapter,
                "_send_message_with_strict_topic",
                None,
            )
        if not callable(strict_sender):
            raise RuntimeError("Telegram strict topic sender is unavailable")
        thread_kwargs = self._adapter._thread_kwargs_for_send(
            chat_id,
            topic_id,
            {"thread_id": topic_id},
        )
        if thread_kwargs.get("message_thread_id") is None:
            raise RuntimeError(
                "Telegram customer delivery requires message_thread_id"
            )
        return strict_sender, {
            "chat_id": chat_id,
            "text": text,
            **thread_kwargs,
        }

    async def _send_customer(
        self,
        customer_key: str,
        destination: object,
        text: str,
        *,
        adaptive: bool,
    ) -> Mapping[str, object]:
        strict_sender, send_kwargs = self._prepare_customer_send(
            customer_key,
            destination,
            text,
            adaptive=adaptive,
        )
        result = strict_sender(**send_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _telegram_receipt(result)

    async def send_customer(
        self, customer_key: str, destination: object, text: str
    ) -> Mapping[str, object]:
        return await self._send_customer(
            customer_key,
            destination,
            text,
            adaptive=False,
        )
    def _adaptive_coordinator(self, customer_key: str) -> AdaptiveNutritionCoordinator:
        resolver = getattr(self._coordinator, "adaptive_nutrition_coordinator", None)
        if not callable(resolver):
            raise RuntimeError("adaptive customer transport authority unavailable")
        try:
            adaptive = resolver(customer_key)
        except Exception as exc:
            raise RuntimeError("adaptive customer transport authority unavailable") from exc
        if not all(
            callable(getattr(adaptive, name, None))
            for name in (
                "_committed_transition",
                "_proposal_for_digest",
                "_live_customer",
                "_proposal_pins",
                "_proposal_body_pins",
                "_feature_epoch",
                "_validate_delivery_record",
                "_production_context",
                "_authority_digest",
                "_epoch_digest",
                "_append_locked",
            )
        ) or not hasattr(adaptive, "store"):
            raise RuntimeError("adaptive customer transport authority unavailable")
        return adaptive

    @staticmethod
    def _reservation_key(destination: object) -> tuple[str, str, str]:
        return tuple(
            str(getattr(destination, field, "") or "").strip()
            for field in ("user_id", "chat_id", "topic_id")
        )

    async def send_adaptive_customer(
        self,
        customer_key: str,
        destination: object,
        *,
        reservation_id: str,
    ) -> Mapping[str, object]:
        """Deliver only an immutable, committed adaptive reservation."""
        if (
            not isinstance(reservation_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", reservation_id) is None
        ):
            raise RuntimeError("adaptive delivery reservation is invalid")
        adaptive = self._adaptive_coordinator(customer_key)
        store = adaptive.store
        locked = getattr(store, "locked", None)
        if not callable(locked):
            raise RuntimeError("adaptive delivery ledger lock is unavailable")
        body: str
        canonical_destination: object
        strict_sender: Callable[..., object]
        send_kwargs: dict[str, object]
        with adaptive._authority_lock():
            (
                _customer,
                _data_root,
                _spec,
                _events,
                live_source_digest,
                live_artifacts,
                _epoch_path,
                _epoch,
                live_registration_binding,
            ) = adaptive._production_context(
                require_activation=True,
                require_delivery=True,
            )
        live_delivery_pins = {
            "source_digest": live_source_digest,
            "policy_digest": live_artifacts.policy_digest,
            "meal_constraints_digest": live_registration_binding.meal_constraints_digest,
            "catalog_digest": live_artifacts.catalog_digest,
            "registration_digest": live_registration_binding.registration_digest,
            "authority_digest": adaptive._authority_digest(_spec),
        }
        with adaptive._authority_lock(), adaptive._lifecycle_lock, locked():
            try:
                rows = list(store.read())
                live_delivery_pins = {
                    **live_delivery_pins,
                    **adaptive._risk_policy_evidence(),
                }
                reservation = None
                for row in rows:
                    payload = row.get("payload") if isinstance(row, Mapping) else None
                    if (
                        isinstance(row, Mapping)
                        and row.get("event_type") == "delivery_attempt_started"
                        and isinstance(payload, Mapping)
                        and payload.get("reservation_id", payload.get("delivery_id"))
                        == reservation_id
                    ):
                        reservation = payload
                if reservation is None or reservation.get("reservation_state") != "committed":
                    raise RuntimeError("adaptive delivery reservation is unavailable")
                if any(
                    isinstance(row, Mapping)
                    and isinstance(row.get("payload"), Mapping)
                    and row["payload"].get("reservation_id", row["payload"].get("delivery_id"))
                    == reservation_id
                    and row.get("event_type")
                    in {
                        "delivery_attempt_consumed",
                        "delivery_receipt_recorded",
                        "delivery_unknown",
                        "delivered",
                        "sent_audited",
                    }
                    for row in rows
                ):
                    raise RuntimeError("adaptive delivery reservation is already consumed")
                customer = self._coordinator.customer(customer_key)
                if customer is None:
                    raise RuntimeError("customer route is unavailable")
                canonical_destination = customer.spec.telegram
                supplied_key = self._reservation_key(destination)
                reserved_destination = reservation.get("destination")
                if not isinstance(reserved_destination, Mapping):
                    raise RuntimeError("adaptive delivery destination pin is invalid")
                if (
                    reservation.get("delivery_id", reservation_id) != reservation_id
                    or reservation.get("topic_id")
                    != str(reserved_destination.get("topic_id", ""))
                ):
                    raise RuntimeError("adaptive delivery reservation is invalid")
                reserved_key = tuple(
                    str(reserved_destination.get(field, "") or "").strip()
                    for field in ("user_id", "chat_id", "topic_id")
                )
                if (
                    not all(supplied_key)
                    or supplied_key != reserved_key
                    or reserved_key != self._reservation_key(canonical_destination)
                ):
                    raise RuntimeError("adaptive delivery destination pin is invalid")
                if reservation.get("customer_key") != customer_key:
                    raise RuntimeError("adaptive delivery reservation customer is invalid")
                proposal_digest = reservation.get("proposal_digest")
                if not isinstance(proposal_digest, str) or not proposal_digest:
                    raise RuntimeError("adaptive delivery proposal pin is invalid")
                if adaptive._committed_transition(
                    rows,
                    action="activate",
                    proposal_digest=proposal_digest,
                ) is None:
                    raise RuntimeError("adaptive activation is not committed")
                proposal = adaptive._proposal_for_digest(proposal_digest)
                adaptive._validate_delivery_record(
                    proposal,
                    reservation,
                )
                if any(
                    reservation.get(field) != expected
                    for field, expected in live_delivery_pins.items()
                ):
                    raise RuntimeError("adaptive delivery pins are stale")
                _customer, data_root, spec = adaptive._live_customer()
                proposal_pins = adaptive._proposal_pins(proposal)
                body_pins = adaptive._proposal_body_pins(proposal, spec)
                body_value = reservation.get("customer_body")
                if (
                    not isinstance(body_value, str)
                    or not body_value
                    or adaptive._text_digest(body_value)
                    != reservation.get("customer_body_digest")
                    or reservation.get("rendered_digest")
                    != reservation.get("customer_body_digest")
                ):
                    raise RuntimeError("adaptive delivery body pin is invalid")
                for key, value in (
                    ("registration_digest", adaptive._registration_pin(proposal, required=True)),
                    ("source_digest", proposal_pins[0]),
                    ("policy_digest", proposal_pins[1]),
                    ("meal_constraints_digest", proposal_pins[2]),
                    ("catalog_digest", proposal_pins[3]),
                    ("operator_body_digest", body_pins["operator_body_digest"]),
                    ("meal_plan_digest", body_pins["meal_plan_digest"]),
                    ("meal_digest", body_pins["meal_digest"]),
                    ("authority_digest", body_pins["authority_digest"]),
                ):
                    if reservation.get(key) != value:
                        raise RuntimeError("adaptive delivery proposal pin is invalid")
                epoch_path, epoch = adaptive._feature_epoch(data_root)
                _ = epoch_path
                if (
                    epoch.get("activation") is not True
                    or epoch.get("delivery") is not True
                    or reservation.get("epoch_digest") != adaptive._epoch_digest(epoch)
                    or reservation.get("activation_proposal_digest") != proposal_digest
                    or reservation.get("delivery_epoch")
                    != epoch.get("delivery_epoch", epoch.get("epoch", 0))
                ):
                    raise RuntimeError("adaptive delivery epoch pin is invalid")
                if not self._coordinator.customer_transport_allowed(
                    customer_key,
                    canonical_destination,
                    adaptive=True,
                ):
                    raise RuntimeError("adaptive customer transport authority unavailable")
                body = body_value
                strict_sender, send_kwargs = self._prepare_customer_send(
                    customer_key,
                    canonical_destination,
                    body,
                    adaptive=True,
                )
                consumed_payload = {
                    "customer_key": customer_key,
                    "reservation_id": reservation_id,
                    "delivery_id": reservation.get("delivery_id", reservation_id),
                    "proposal_digest": proposal_digest,
                    "customer_body_digest": reservation.get("customer_body_digest"),
                    "destination": dict(reserved_destination),
                    "epoch_digest": reservation.get("epoch_digest"),
                    "risk_policy_version": reservation.get("risk_policy_version"),
                    "risk_policy_digest": reservation.get("risk_policy_digest"),
                    "risk_policy_document_digest": reservation.get("risk_policy_document_digest"),
                    "execution_mode": "production",
                }
                adaptive._append_locked(
                    store,
                    "delivery_attempt_consumed",
                    consumed_payload,
                    dedupe_key=f"production-consumed:{reservation_id}",
                )
            except RuntimeError as exc:
                raise AdaptiveTransportPreflightRejected(str(exc)) from exc
            except (TypeError, ValueError) as exc:
                raise AdaptiveTransportPreflightRejected(
                    "adaptive delivery reservation is invalid"
                ) from exc
            except OSError as exc:
                raise RuntimeError(
                    "adaptive delivery consume outcome is unknown"
                ) from exc
        result = strict_sender(**send_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _telegram_receipt(result)



def _telegram_receipt(result: object) -> Mapping[str, object]:
    if result is None or result is False:
        raise RuntimeError("Telegram customer transport rejected delivery")
    if isinstance(result, Mapping):
        if result.get("ok") is not True:
            raise RuntimeError("Telegram customer transport rejected delivery")
        message_id = result.get("message_id")
        raw_response = result.get("raw_response")
    else:
        if getattr(result, "success", True) is False or getattr(result, "ok", True) is False:
            raise RuntimeError("Telegram customer transport rejected delivery")
        message_id = getattr(result, "message_id", None)
        raw_response = getattr(result, "raw_response", None)
    if message_id is None and isinstance(raw_response, Mapping):
        if raw_response.get("ok") is not True:
            raise RuntimeError("Telegram customer transport rejected delivery")
        message_ids = raw_response.get("message_ids")
        if isinstance(message_ids, (tuple, list)) and len(message_ids) == 1:
            message_id = message_ids[0]
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        raise RuntimeError("Telegram customer transport must return one message receipt")
    receipt = str(message_id).strip()
    if not receipt or len(receipt) > 128:
        raise RuntimeError("Telegram customer transport must return one message receipt")
    return {"ok": True, "message_id": receipt}


def _enabled_customer_key(
    coordinator: NutritionCoachingCoordinator,
    requested: str | None,
) -> str:
    enabled = tuple(
        runtime.spec.customer_key
        for runtime in coordinator.registry.customers
        if runtime.spec.enabled and coordinator.customer(runtime.spec.customer_key) is not None
    )
    if requested is not None:
        if requested not in enabled:
            raise NutritionCoachingIntegrationError("requested customer route is unavailable")
        return requested
    if len(enabled) != 1:
        raise NutritionCoachingIntegrationError(
            "production console requires exactly one enabled customer route"
        )
    return enabled[0]


def create_nutrition_operator_console_server(
    coordinator: NutritionCoachingCoordinator,
    *,
    token_path: str | Path,
    customer_transport: object,
    customer_key: str | None = None,
    port: int = 0,
    bind_host: str = "127.0.0.1",
    max_body_bytes: int = 64 * 1024,
) -> Any:
    """Create the Richard-only console from the live gateway wiring.

    This is the gateway host boundary: it derives the canonical customer
    EventStore and destination from the live coordinator and passes no
    callback-only lifecycle stand-ins to the profile console.
    """
    if not isinstance(coordinator, NutritionCoachingCoordinator):
        raise NutritionCoachingIntegrationError("live nutrition coordinator is required")
    if not isinstance(token_path, (str, Path)) or not str(token_path):
        raise NutritionCoachingIntegrationError("rotated operator token path is required")
    if bind_host != "127.0.0.1":
        raise NutritionCoachingIntegrationError("production console must bind to 127.0.0.1")
    if not callable(getattr(customer_transport, "send_customer", None)):
        raise NutritionCoachingIntegrationError(
            "production console requires a receipt-returning customer transport"
        )
    selected_customer = _enabled_customer_key(coordinator, customer_key)
    try:
        owner = coordinator.owner
    except (AttributeError, TypeError, ValueError) as exc:
        raise NutritionCoachingIntegrationError("owner route is unavailable") from exc
    if not all(getattr(owner, field, "") for field in ("user_id", "chat_id", "topic_id")):
        raise NutritionCoachingIntegrationError("owner route is unavailable")
    customer = coordinator.customer(selected_customer)
    destination = getattr(getattr(customer, "spec", None), "telegram", None)
    if customer is None or destination is None or not all(
        isinstance(getattr(destination, field, None), str)
        and bool(getattr(destination, field, "").strip())
        for field in ("user_id", "chat_id", "topic_id")
    ):
        raise NutritionCoachingIntegrationError("customer transport destination is unavailable")
    event_source = CoordinatorEventSource(coordinator, selected_customer)
    try:
        event_source.events_for(selected_customer)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise NutritionCoachingIntegrationError(
            "canonical customer EventStore is unavailable"
        ) from exc
    package_root = coordinator.profile_root / "workspace" / "checkin_cli"
    if package_root.is_dir() and str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    try:
        from checkin_cli.operator_console import create_production_operator_console_server
    except (ImportError, OSError) as exc:
        raise NutritionCoachingIntegrationError(
            "profile operator console dependency is unavailable"
        ) from exc
    try:
        return create_production_operator_console_server(
            token_path=Path(token_path),
            canonical_event_source=event_source,
            coordinator=coordinator,
            customer_transport=customer_transport,
            port=port,
            bind_host=bind_host,
            max_body_bytes=max_body_bytes,
            customer_key=selected_customer,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise NutritionCoachingIntegrationError(
            f"production operator console could not be created: {type(exc).__name__}"
        ) from exc


def serve_nutrition_operator_console(
    coordinator: NutritionCoachingCoordinator,
    *,
    token_path: str | Path,
    customer_transport: object,
    customer_key: str | None = None,
    port: int = 0,
    bind_host: str = "127.0.0.1",
    max_body_bytes: int = 64 * 1024,
) -> None:
    """Serve the explicitly wired production console until shutdown."""
    server = create_nutrition_operator_console_server(
        coordinator,
        token_path=token_path,
        customer_transport=customer_transport,
        customer_key=customer_key,
        port=port,
        bind_host=bind_host,
        max_body_bytes=max_body_bytes,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
