from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from checkin_cli.diagnostic_isolation import (
    ActivatedDiagnosticDelivery,
    DiagnosticAuthorityError,
    DiagnosticDeliveryAuthority,
    DiagnosticDeliveryCandidate,
    DiagnosticIsolationSpecV1,
    DiagnosticUnknownNoSend,
    DiagnosticRoleRoute,
    DiagnosticLiveAuthorityLoader,
    DiagnosticLiveAuthoritySources,
    DiagnosticSessionState,
    DurableDiagnosticActivationLoader,
    _DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
    _DIAGNOSTIC_HOST_ADMISSION_TOKEN,
    VerifiedDiagnosticReservation,
    _digest,
)

_DIAGNOSTIC_TRANSPORT_FACTORY_TOKEN = object()
_DIAGNOSTIC_BINDING_FACTORY_TOKEN = object()
_DIAGNOSTIC_CONTROLLER_FACTORY_TOKEN = object()
_DIAGNOSTIC_HOST_FACTORY_TOKEN = object()
_DIAGNOSTIC_RUNTIME_LOADER_FACTORY_TOKEN = object()
_DIAGNOSTIC_COORDINATOR_FACTORY_TOKEN = object()
_DIAGNOSTIC_ROUTE_INSTALL_TOKEN = object()
_DIAGNOSTIC_TEST_FACTORY_TOKEN = object()


class _DiagnosticTestAdapter:
    def __init__(self, *, bot: object, bot_digest: str, factory_token: object) -> None:
        if factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic test adapter construction is sealed")
        if not inspect.iscoroutinefunction(getattr(bot, "send_message", None)):
            raise DiagnosticGatewayError("diagnostic test bot sender must be native async")
        self._bot = bot
        self._diagnostic_test_bot_digest = bot_digest
        self._nutrition_coaching = None


def _bind_test_adapter(
    bot: object,
    bot_digest: str,
    *,
    factory_token: object,
) -> _DiagnosticTestAdapter:
    if factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN:
        raise DiagnosticGatewayError("diagnostic test adapter construction is sealed")
    return _DiagnosticTestAdapter(
        bot=bot,
        bot_digest=bot_digest,
        factory_token=factory_token,
    )


def _require_test_adapter(
    adapter: object,
    *,
    expected_bot_digest: str,
    factory_token: object,
) -> object:
    if factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN:
        raise DiagnosticGatewayError("diagnostic test adapter is sealed")
    if type(adapter) is not _DiagnosticTestAdapter:
        raise DiagnosticGatewayError("diagnostic test adapter is invalid")
    if adapter._diagnostic_test_bot_digest != expected_bot_digest:
        raise DiagnosticGatewayError("diagnostic test bot digest is stale")
    bot = adapter._bot
    if not inspect.iscoroutinefunction(getattr(bot, "send_message", None)):
        raise DiagnosticGatewayError("diagnostic test bot sender must be native async")
    return bot


@runtime_checkable
class DiagnosticRuntimeLoader(Protocol):
    """Typed loader for the activated diagnostic CustomerRuntime."""

    def load_runtime(self, customer_key_digest: str) -> object:
        ...


def _require_production_adapter(
    adapter: object,
    *,
    expected_bot_digest: str,
) -> object:
    """Return the one started Telegram bot trusted by the production path."""
    try:
        from gateway.platforms.telegram import (
            TelegramAdapter,
            _DIAGNOSTIC_PRODUCTION_ADAPTER_TOKEN as production_token,
        )
    except Exception as exc:  # pragma: no cover - import failure is fail-closed
        raise DiagnosticGatewayError(
            "diagnostic Telegram adapter is unavailable"
        ) from exc
    if type(adapter) is not TelegramAdapter:
        raise DiagnosticGatewayError("diagnostic adapter is not the trusted TelegramAdapter")
    if getattr(adapter, "_diagnostic_production_adapter_token", None) is not production_token:
        raise DiagnosticGatewayError("diagnostic Telegram adapter is not startup-verified")
    bot = getattr(adapter, "_bot", None)
    pinned_bot = getattr(adapter, "_diagnostic_production_bot_identity", None)
    if bot is None or pinned_bot is not bot:
        raise DiagnosticGatewayError("diagnostic production bot identity changed")
    digest = getattr(adapter, "_diagnostic_production_bot_digest", None)
    if digest != expected_bot_digest:
        raise DiagnosticGatewayError("diagnostic production bot digest is stale")
    sender = getattr(bot, "send_message", None)
    if not inspect.iscoroutinefunction(sender):
        raise DiagnosticGatewayError("diagnostic bot sender must be native async")
    return bot



@dataclass(frozen=True, slots=True)
class VerifiedDiagnosticTransportBinding:
    """Factory-only gateway inputs derived from verified profile authority."""

    destination: tuple[int, int]
    diagnostic_transport_binding_digest: str
    test_bot_digest: str
    max_provider_timeout_seconds: int
    authority: DiagnosticDeliveryAuthority
    activation_record_digest: str = field(
        default="",
        repr=False,
        compare=False,
    )
    _seal_digest: str = field(
        default="",
        repr=False,
        compare=False,
    )
    _factory_token: object | None = field(
        default=None,
        kw_only=True,
        repr=False,
        compare=False,
    )

    @staticmethod
    def _seal_for(
        destination: tuple[int, int],
        binding_digest: str,
        test_bot_digest: str,
        timeout: int,
        authority: DiagnosticDeliveryAuthority,
        activation_record_digest: str,
    ) -> str:
        return _digest(
            {
                "destination": destination,
                "diagnostic_transport_binding_digest": binding_digest,
                "test_bot_digest": test_bot_digest,
                "max_provider_timeout_seconds": timeout,
                "session_digest": authority.session.spec_digest,
                "authority_digest": authority.session.authority_digest,
                "generation": authority.session.generation,
                "activation_record_digest": activation_record_digest,
            }
        )

    def _require_sealed(self) -> None:
        if not isinstance(self.authority, DiagnosticDeliveryAuthority):
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        if self._seal_digest != self._seal_for(
            self.destination,
            self.diagnostic_transport_binding_digest,
            self.test_bot_digest,
            self.max_provider_timeout_seconds,
            self.authority,
            self.activation_record_digest,
        ):
            raise DiagnosticGatewayError("diagnostic transport binding is stale")
    def __post_init__(self) -> None:
        if self._factory_token is not _DIAGNOSTIC_BINDING_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic binding construction is sealed")
        if (
            not isinstance(self.destination, tuple)
            or len(self.destination) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in self.destination
            )
        ):
            raise DiagnosticGatewayError("diagnostic destination is not concrete")
        for value in (
            self.diagnostic_transport_binding_digest,
            self.test_bot_digest,
            self.activation_record_digest,
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                or value == "0" * 64
            ):
                raise DiagnosticGatewayError("diagnostic transport binding is invalid")
        if (
            isinstance(self.max_provider_timeout_seconds, bool)
            or not isinstance(self.max_provider_timeout_seconds, int)
            or not 1 <= self.max_provider_timeout_seconds <= 30
        ):
            raise DiagnosticGatewayError("invalid diagnostic timeout")
        if not isinstance(self.authority, DiagnosticDeliveryAuthority):
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        self._require_sealed()

    @classmethod
    def from_verified_spec(
        cls,
        spec: DiagnosticIsolationSpecV1,
        authority: DiagnosticDeliveryAuthority,
        activation_loader: DurableDiagnosticActivationLoader,
    ) -> "VerifiedDiagnosticTransportBinding":
        if cls is not VerifiedDiagnosticTransportBinding:
            raise DiagnosticGatewayError("diagnostic binding factory is sealed")
        if not isinstance(spec, DiagnosticIsolationSpecV1):
            raise DiagnosticGatewayError("diagnostic isolation spec is unverified")
        if not isinstance(authority, DiagnosticDeliveryAuthority):
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        if type(activation_loader) is not DurableDiagnosticActivationLoader:
            raise DiagnosticGatewayError("diagnostic activation loader is unverified")
        if activation_loader.authority is not authority:
            raise DiagnosticGatewayError("diagnostic activation authority changed")
        session = authority.session
        if (
            spec.spec_digest != session.spec_digest
            or spec.authority_digest != session.authority_digest
            or spec.diagnostic_transport_binding_digest
            != session.transport_binding_digest
        ):
            raise DiagnosticGatewayError("diagnostic isolation spec is stale")
        if authority.spec is not spec:
            raise DiagnosticGatewayError("diagnostic authority spec changed")
        activated = activation_loader.load_activated_delivery(session.session_id)
        if activated.diagnostic_transport_binding_digest != spec.diagnostic_transport_binding_digest:
            raise DiagnosticGatewayError("diagnostic activation binding is stale")
        if activated.destination_digest != spec.customer_destination_digest:
            raise DiagnosticGatewayError("diagnostic activation destination is stale")
        if activated.session_id != session.session_id:
            raise DiagnosticGatewayError("diagnostic activation session is stale")
        if activated.session_generation != session.generation:
            raise DiagnosticGatewayError("diagnostic activation generation is stale")
        destination_values: list[int] = []
        for value in activated.destination:
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise DiagnosticGatewayError(
                    "diagnostic activation destination is not Telegram-shaped"
                ) from exc
            if str(parsed) != value:
                raise DiagnosticGatewayError(
                    "diagnostic activation destination is not canonical"
                )
            destination_values.append(parsed)
        return cls(
            tuple(destination_values),
            spec.diagnostic_transport_binding_digest,
            spec.test_bot_digest,
            spec.max_provider_timeout_seconds,
            authority,
            activation_loader.record_digest(activated),
            _seal_digest=cls._seal_for(
                tuple(destination_values),
                spec.diagnostic_transport_binding_digest,
                spec.test_bot_digest,
                spec.max_provider_timeout_seconds,
                authority,
                activation_loader.record_digest(activated),
            ),
            _factory_token=_DIAGNOSTIC_BINDING_FACTORY_TOKEN,
        )




class DiagnosticGatewayError(RuntimeError):
    pass


def _require_exact_diagnostic_runtime(
    runtime: object,
    *,
    activated: ActivatedDiagnosticDelivery,
    profile_root: Path | None = None,
) -> object:
    """Validate the one CustomerRuntime admitted by an activated record."""
    try:
        from checkin_cli.customer_coaching import (
            CustomerRuntime,
            RegisteredCustomerBinding,
            _canonical_digest,
            _path_digest,
        )
    except Exception as exc:  # pragma: no cover - profile package is production-provided
        raise DiagnosticGatewayError("diagnostic customer runtime is unavailable") from exc
    if type(runtime) is not CustomerRuntime:
        raise DiagnosticGatewayError("diagnostic customer runtime is not exact")
    try:
        binding = runtime.registered_binding
    except Exception as exc:
        raise DiagnosticGatewayError("diagnostic customer binding is unavailable") from exc
    if type(binding) is not RegisteredCustomerBinding:
        raise DiagnosticGatewayError("diagnostic customer binding is not exact")
    if getattr(runtime, "mode", None) != "diagnostic_isolated_v1":
        raise DiagnosticGatewayError("diagnostic customer runtime mode is not isolated")
    if getattr(binding, "mode", None) != "diagnostic_isolated_v1":
        raise DiagnosticGatewayError("diagnostic customer binding mode is not isolated")
    try:
        data_root = runtime.data_root
        if type(data_root) is not type(Path()) or data_root.is_symlink():
            raise DiagnosticGatewayError("diagnostic customer root is not registered")
        if binding.data_root_digest != _path_digest(data_root):
            raise DiagnosticGatewayError("diagnostic customer root binding is stale")
        if profile_root is not None:
            if type(profile_root) is not type(Path()):
                raise DiagnosticGatewayError("diagnostic profile root is invalid")
            if not data_root.resolve().is_relative_to(profile_root.resolve()):
                raise DiagnosticGatewayError("diagnostic customer root escapes profile")
    except DiagnosticGatewayError:
        raise
    except Exception as exc:
        raise DiagnosticGatewayError("diagnostic customer root is unavailable") from exc
    spec = getattr(runtime, "spec", None)
    customer_key = getattr(spec, "customer_key", None)
    if not isinstance(customer_key, str) or binding.customer_key_digest != _canonical_digest(
        {"customer_key": customer_key}
    ):
        raise DiagnosticGatewayError("diagnostic customer key provenance is stale")
    if binding.customer_key_digest != activated.customer_key_digest:
        raise DiagnosticGatewayError("diagnostic customer key binding is stale")
    if binding.registry_digest != activated.registry_digest:
        raise DiagnosticGatewayError("diagnostic customer registry binding is stale")
    if binding.activation_digest != activated.activation_receipt_digest:
        raise DiagnosticGatewayError("diagnostic customer activation binding is stale")
    telegram = getattr(spec, "telegram", None)
    destination = (
        getattr(telegram, "chat_id", None),
        getattr(telegram, "topic_id", None),
    )
    if destination != activated.destination:
        raise DiagnosticGatewayError("diagnostic customer destination binding is stale")
    if getattr(spec, "enabled", None) is not True:
        raise DiagnosticGatewayError("diagnostic customer runtime is disabled")
    return runtime

def _canonical_telegram_id(value: object) -> int:
    if type(value) is not str or not value:
        raise DiagnosticGatewayError("diagnostic role identity is invalid")
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise DiagnosticGatewayError("diagnostic role identity is invalid") from exc
    if str(parsed) != value:
        raise DiagnosticGatewayError("diagnostic role identity is not canonical")
    return parsed


def _registered_role_routes(
    customer_runtime: object,
    *,
    operator: DiagnosticRoleRoute,
    generation: int,
) -> tuple[DiagnosticRoleRoute, ...]:
    spec = getattr(customer_runtime, "spec", None)
    addresses = (
        ("customer", getattr(spec, "telegram", None)),
        ("trainer", getattr(spec, "trainer", None)),
    )
    routes = [operator]
    for role, address in addresses:
        if address is None:
            raise DiagnosticGatewayError(f"diagnostic {role} route is unavailable")
        routes.append(
            DiagnosticRoleRoute(
                _canonical_telegram_id(getattr(address, "user_id", None)),
                _canonical_telegram_id(getattr(address, "chat_id", None)),
                _canonical_telegram_id(getattr(address, "topic_id", None)),
                role,
                generation,
            )
        )
    spaces = {(route.chat_id, route.topic_id) for route in routes}
    triples = {(route.user_id, route.chat_id, route.topic_id) for route in routes}
    if len(spaces) != len(routes) or len(triples) != len(routes):
        raise DiagnosticGatewayError("diagnostic role routes are not distinct")
    return tuple(routes)


class _ProfileDiagnosticRuntimeLoader:
    """Load exactly one committed diagnostic CustomerRuntime after activation."""

    def __init__(
        self,
        authority: DiagnosticDeliveryAuthority,
        activated: ActivatedDiagnosticDelivery,
        *,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _DIAGNOSTIC_RUNTIME_LOADER_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic runtime loader construction is sealed")
        if type(authority) is not DiagnosticDeliveryAuthority:
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        if type(activated) is not ActivatedDiagnosticDelivery:
            raise DiagnosticGatewayError("diagnostic activation is invalid")
        try:
            from checkin_cli.diagnostic_isolation import DiagnosticLiveAuthorityLoader
        except Exception as exc:
            raise DiagnosticGatewayError("diagnostic live authority loader is unavailable") from exc
        if type(getattr(authority, "live_loader", None)) is not DiagnosticLiveAuthorityLoader:
            raise DiagnosticGatewayError("diagnostic live authority loader is unverified")
        self._authority = authority
        self._profile_root = authority.profile_root
        self._session_digest = authority.session.spec_digest
        self._customer_key_digest = activated.customer_key_digest
        self._registry_digest = activated.registry_digest
        self._activated = activated
        self._owner_route_values: tuple[object, object, object] | None = None

    def load_runtime(self, customer_key_digest: str) -> object:
        if customer_key_digest != self._customer_key_digest:
            raise DiagnosticGatewayError("diagnostic customer key input is stale")
        try:
            from checkin_cli.customer_admin import _resolve_profile_root
            from checkin_cli.customer_coaching import (
                load_diagnostic_runtime_customer_registry,
            )
            root = _resolve_profile_root(Path(self._profile_root))
            registry_path = root / "customers" / "registry.json"
            if not registry_path.exists() or registry_path.is_symlink():
                raise DiagnosticGatewayError("diagnostic canonical registry is unavailable")
            registry_path = registry_path.resolve()
            if not registry_path.is_relative_to(root):
                raise DiagnosticGatewayError("diagnostic canonical registry escapes profile")
            raw = registry_path.read_bytes()
            registry_digest = hashlib.sha256(raw).hexdigest()
            if registry_digest != self._registry_digest:
                raise DiagnosticGatewayError("diagnostic customer registry is stale")
            registry = load_diagnostic_runtime_customer_registry(
                registry_path,
                root,
                session_digest=self._session_digest,
            )
            owner = getattr(registry, "owner", None)
            self._owner_route_values = (
                getattr(owner, "user_id", None),
                getattr(owner, "chat_id", None),
                getattr(owner, "topic_id", None),
            )
        except DiagnosticGatewayError:
            raise
        except Exception as exc:
            raise DiagnosticGatewayError("diagnostic customer runtime is unavailable") from exc
        matches = []
        for runtime in getattr(registry, "customers", ()):
            try:
                binding = runtime.registered_binding
            except Exception:
                continue
            if getattr(binding, "customer_key_digest", None) == customer_key_digest:
                matches.append(runtime)
        if len(matches) != 1:
            raise DiagnosticGatewayError("diagnostic customer runtime is ambiguous")
        return _require_exact_diagnostic_runtime(
            matches[0],
            activated=self._activated,
            profile_root=self._profile_root,
        )

    def load_owner_route(self, *, generation: int) -> DiagnosticRoleRoute:
        values = self._owner_route_values
        if values is None:
            raise DiagnosticGatewayError("diagnostic registry owner is unavailable")
        return DiagnosticRoleRoute(
            _canonical_telegram_id(values[0]),
            _canonical_telegram_id(values[1]),
            _canonical_telegram_id(values[2]),
            "operator",
            generation,
        )


class _ActivatedDiagnosticCoordinator:
    """One-use child coordinator that alone can mint host candidates."""

    def __init__(
        self,
        *,
        customer_runtime: object,
        authority: DiagnosticDeliveryAuthority,
        transport: "TelegramDiagnosticTransport",
        activation_loader: DurableDiagnosticActivationLoader,
        activated: ActivatedDiagnosticDelivery,
        runtime_loader: DiagnosticRuntimeLoader,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _DIAGNOSTIC_COORDINATOR_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic coordinator construction is sealed")
        if type(authority) is not DiagnosticDeliveryAuthority:
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        if type(transport) is not TelegramDiagnosticTransport:
            raise DiagnosticGatewayError("diagnostic transport is not sealed")
        if type(activation_loader) is not DurableDiagnosticActivationLoader:
            raise DiagnosticGatewayError("diagnostic activation loader is unverified")
        if activation_loader.authority is not authority:
            raise DiagnosticGatewayError("diagnostic activation authority changed")
        if not isinstance(runtime_loader, DiagnosticRuntimeLoader):
            raise DiagnosticGatewayError("diagnostic runtime loader is unverified")
        if type(activated) is not ActivatedDiagnosticDelivery:
            raise DiagnosticGatewayError("diagnostic activation is invalid")
        if getattr(transport, "_authority", None) is not authority:
            raise DiagnosticGatewayError("diagnostic transport authority changed")
        if (
            activated.session_id != authority.session.session_id
            or activated.session_generation != authority.session.generation
            or activated.diagnostic_transport_binding_digest
            != authority.session.transport_binding_digest
        ):
            raise DiagnosticGatewayError("diagnostic activation is stale")
        runtime = _require_exact_diagnostic_runtime(
            customer_runtime,
            activated=activated,
            profile_root=authority.profile_root,
        )
        self._runtime = runtime
        self._authority = authority
        self._transport = transport
        self._activation_loader = activation_loader
        self._runtime_loader = runtime_loader
        self._activated = activated
        self._provenance_token = object()
        self._candidate_ids: set[int] = set()

    @property
    def authority(self) -> DiagnosticDeliveryAuthority:
        return self._authority

    @property
    def transport(self) -> "TelegramDiagnosticTransport":
        return self._transport

    @property
    def customer_runtime(self) -> object:
        return self._runtime

    def _require_current_inputs(self, session_id: str) -> ActivatedDiagnosticDelivery:
        if not isinstance(session_id, str) or not session_id:
            raise DiagnosticGatewayError("diagnostic activation session is invalid")
        if self._authority.session.state is not DiagnosticSessionState.ACTIVE:
            raise DiagnosticGatewayError("diagnostic session is not active")
        if (
            self._authority.session.session_id != self._activated.session_id
            or self._authority.session.generation != self._activated.session_generation
        ):
            raise DiagnosticGatewayError("diagnostic activation session is stale")
        current = self._activation_loader.load_activated_delivery(session_id)
        if current != self._activated:
            raise DiagnosticGatewayError("diagnostic activation current input is stale")
        runtime = self._runtime_loader.load_runtime(self._activated.customer_key_digest)
        runtime = _require_exact_diagnostic_runtime(
            runtime,
            activated=self._activated,
            profile_root=self._authority.profile_root,
        )
        if runtime != self._runtime:
            raise DiagnosticGatewayError("diagnostic customer runtime current input is stale")
        if getattr(self._transport, "_authority", None) is not self._authority:
            raise DiagnosticGatewayError("diagnostic transport authority changed")
        return current

    def candidate_for_session(self, session_id: str) -> DiagnosticDeliveryCandidate:
        """Mint a candidate only after revalidating the frozen activation inputs."""
        activated = self._require_current_inputs(session_id)
        candidate = DiagnosticDeliveryCandidate.from_activated(
            activated,
            factory_token=_DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
            session_digest=self._authority.session.spec_digest,
        )
        # The candidate dataclass is owned by the profile package.  Keep the
        # gateway provenance private and identity-bound without widening that
        # package's persisted schema.
        object.__setattr__(
            candidate,
            "_diagnostic_coordinator_provenance",
            self._provenance_token,
        )
        for name in (
            "customer_key_digest",
            "owner_digest",
            "registry_digest",
            "consent_digest",
            "activation_receipt_digest",
            "proposal_digest",
            "revision",
            "revision_digest",
            "rendered_body_digest",
            "destination_digest",
            "config_digest",
            "policy_digest",
            "catalog_digest",
            "meal_constraints_digest",
            "source_digest",
            "registration_digest",
            "epoch_digest",
            "diagnostic_transport_binding_digest",
        ):
            if hasattr(activated, name):
                object.__setattr__(candidate, name, getattr(activated, name))
        object.__setattr__(candidate, "_diagnostic_coordinator", self)
        self._candidate_ids.add(id(candidate))
        return candidate



    def require_candidate(self, candidate: object) -> DiagnosticDeliveryCandidate:
        if (
            not isinstance(candidate, DiagnosticDeliveryCandidate)
            or getattr(candidate, "_diagnostic_coordinator_provenance", None)
            is not self._provenance_token
            or id(candidate) not in self._candidate_ids
        ):
            raise DiagnosticGatewayError(
                "diagnostic candidate provenance is not from the activated coordinator"
            )
        return candidate
    def bind_live_snapshot(self, candidate: object, snapshot: object) -> DiagnosticDeliveryCandidate:
        """Attach the lease-held live pins before authority validation."""
        candidate = self.require_candidate(candidate)
        try:
            from checkin_cli.diagnostic_isolation import LiveDiagnosticAuthoritySnapshot
        except Exception as exc:  # pragma: no cover - profile package is production-provided
            raise DiagnosticGatewayError("diagnostic live snapshot is unavailable") from exc
        if type(snapshot) is not LiveDiagnosticAuthoritySnapshot:
            raise DiagnosticGatewayError("diagnostic live snapshot is unverified")
        sentinel = object()
        for name in (
            "customer_key_digest",
            "owner_digest",
            "registry_digest",
            "consent_digest",
            "activation_receipt_digest",
            "proposal_digest",
            "revision",
            "revision_digest",
            "rendered_body_digest",
            "destination_digest",
            "config_digest",
            "policy_digest",
            "catalog_digest",
            "meal_constraints_digest",
            "source_digest",
            "registration_digest",
            "epoch_digest",
            "diagnostic_transport_binding_digest",
        ):
            if not hasattr(snapshot, name):
                raise DiagnosticGatewayError("diagnostic live snapshot is incomplete")
            value = getattr(snapshot, name)
            current = getattr(candidate, name, sentinel)
            if current is not sentinel and current != value:
                raise DiagnosticGatewayError("diagnostic live candidate pin is stale")
            if current is sentinel:
                object.__setattr__(candidate, name, value)
        return candidate







class TelegramDiagnosticTransport:
    diagnostic_transport_v1 = True
    cancellation_cooperative = True

    def __init__(
        self,
        adapter: object,
        *,
        bot: object,
        destination: tuple[int, int],
        binding: str,
        max_timeout: int,
        authority: DiagnosticDeliveryAuthority,
        bot_digest: str,
        _factory_token: object | None = None,
        _test_factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _DIAGNOSTIC_TRANSPORT_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic transport construction is sealed")
        if not 1 <= max_timeout <= 30:
            raise DiagnosticGatewayError("invalid diagnostic timeout")
        sender = getattr(bot, "send_message", None)
        if not inspect.iscoroutinefunction(sender):
            raise DiagnosticGatewayError("diagnostic bot sender must be native async")
        self._adapter = adapter
        self._bot = bot
        self._destination = destination
        self._authority = authority
        self._session_digest = authority.session.spec_digest
        self._generation = authority.session.generation
        self._bot_digest = bot_digest
        self.binding_digest = binding
        self.max_provider_timeout_seconds = max_timeout
        self._test_factory_token = _test_factory_token

    @classmethod
    def for_diagnostic(
        cls,
        adapter: object,
        verified_binding: VerifiedDiagnosticTransportBinding,
        authority: DiagnosticDeliveryAuthority | None = None,
    ) -> "TelegramDiagnosticTransport":
        if cls is not TelegramDiagnosticTransport:
            raise DiagnosticGatewayError("diagnostic transport construction is sealed")
        if not isinstance(verified_binding, VerifiedDiagnosticTransportBinding):
            raise DiagnosticGatewayError("diagnostic transport inputs are not verified")
        if getattr(verified_binding, "_factory_token", None) is not _DIAGNOSTIC_BINDING_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic transport inputs are not verified")
        verified_binding._require_sealed()
        if authority is not None and authority is not verified_binding.authority:
            raise DiagnosticGatewayError("diagnostic delivery authority changed")
        bot = _require_production_adapter(
            adapter,
            expected_bot_digest=verified_binding.test_bot_digest,
        )
        session = getattr(verified_binding.authority, "_session", None)
        if session is None or not isinstance(
            getattr(session, "transport_binding_digest", None), str
        ):
            raise DiagnosticGatewayError("diagnostic authority session is unavailable")
        if (
            session.transport_binding_digest
            != verified_binding.diagnostic_transport_binding_digest
        ):
            raise DiagnosticGatewayError("diagnostic transport binding is stale")
        return cls(
            adapter,
            bot=bot,
            destination=verified_binding.destination,
            binding=verified_binding.diagnostic_transport_binding_digest,
            max_timeout=verified_binding.max_provider_timeout_seconds,
            authority=verified_binding.authority,
            bot_digest=verified_binding.test_bot_digest,
            _factory_token=_DIAGNOSTIC_TRANSPORT_FACTORY_TOKEN,
        )

    @classmethod
    def _for_test(
        cls,
        adapter: object,
        verified_binding: VerifiedDiagnosticTransportBinding,
        authority: DiagnosticDeliveryAuthority,
        *,
        factory_token: object,
    ) -> "TelegramDiagnosticTransport":
        if cls is not TelegramDiagnosticTransport:
            raise DiagnosticGatewayError("diagnostic test transport construction is sealed")
        if not isinstance(verified_binding, VerifiedDiagnosticTransportBinding):
            raise DiagnosticGatewayError("diagnostic transport inputs are not verified")
        verified_binding._require_sealed()
        if authority is not verified_binding.authority:
            raise DiagnosticGatewayError("diagnostic delivery authority changed")
        bot = _require_test_adapter(
            adapter,
            expected_bot_digest=verified_binding.test_bot_digest,
            factory_token=factory_token,
        )
        return cls(
            adapter,
            bot=bot,
            destination=verified_binding.destination,
            binding=verified_binding.diagnostic_transport_binding_digest,
            max_timeout=verified_binding.max_provider_timeout_seconds,
            authority=authority,
            bot_digest=verified_binding.test_bot_digest,
            _factory_token=_DIAGNOSTIC_TRANSPORT_FACTORY_TOKEN,
            _test_factory_token=factory_token,
        )

    def _verify_reservation_before_native_send(
        self,
        verified: VerifiedDiagnosticReservation,
    ) -> bytes:
        if (
            not isinstance(verified, VerifiedDiagnosticReservation)
            or verified.provider_authority is not True
            or getattr(verified, "_authority_token", None) is not _DIAGNOSTIC_HOST_ADMISSION_TOKEN
        ):
            raise DiagnosticGatewayError("diagnostic reservation is unverified")
        candidate = verified.candidate
        if candidate.transport_binding_digest != self.binding_digest:
            raise DiagnosticGatewayError("diagnostic transport binding changed")
        if candidate.destination != tuple(str(value) for value in self._destination):
            raise DiagnosticGatewayError("diagnostic destination changed")
        if candidate.session_digest != self._session_digest:
            raise DiagnosticGatewayError("diagnostic session changed")
        if candidate.generation != self._generation:
            raise DiagnosticGatewayError("diagnostic generation changed")
        session = self._authority.session
        if session.spec_digest != self._session_digest or session.generation != self._generation:
            raise DiagnosticGatewayError("diagnostic authority session changed")
        if self._test_factory_token is _DIAGNOSTIC_TEST_FACTORY_TOKEN:
            current_bot = _require_test_adapter(
                self._adapter,
                expected_bot_digest=self._bot_digest,
                factory_token=self._test_factory_token,
            )
        else:
            current_bot = _require_production_adapter(
                self._adapter,
                expected_bot_digest=self._bot_digest,
            )
        if current_bot is not self._bot:
            raise DiagnosticGatewayError("diagnostic bot identity changed")
        body = candidate.body_bytes
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != candidate.body_digest:
            raise DiagnosticGatewayError("diagnostic rendered body changed")
        return body
    async def send_diagnostic_customer(
        self, verified: VerifiedDiagnosticReservation, *, deadline_monotonic: float
    ) -> Mapping[str, object]:
        body = self._verify_reservation_before_native_send(verified)
        remaining = deadline_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("diagnostic deadline elapsed")
        remaining = min(remaining, float(self.max_provider_timeout_seconds))
        chat_id, topic_id = self._destination
        receipt = await self._bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=body.decode("utf-8"),
            connect_timeout=remaining,
            pool_timeout=remaining,
            write_timeout=remaining,
            read_timeout=remaining,
        )
        return {"message_id": getattr(receipt, "message_id", None), "ok": True}


class DiagnosticHost:
    def __init__(
        self,
        authority: DiagnosticDeliveryAuthority,
        transport: TelegramDiagnosticTransport,
        *,
        generation: int,
        max_provider_timeout_seconds: int,
        activation_loader: DurableDiagnosticActivationLoader,
        coordinator: _ActivatedDiagnosticCoordinator | None = None,
        _expected_activation_record_digest: str,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _DIAGNOSTIC_HOST_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic host construction is sealed")
        if type(authority) is not DiagnosticDeliveryAuthority:
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        live_loader = getattr(authority, "live_loader", None)
        if type(live_loader) is not DiagnosticLiveAuthorityLoader:
            raise DiagnosticGatewayError("diagnostic live authority loader is unverified")
        if live_loader.authority is not authority:
            raise DiagnosticGatewayError("diagnostic live authority changed")
        if type(transport) is not TelegramDiagnosticTransport:
            raise DiagnosticGatewayError("diagnostic transport is not sealed")
        if type(activation_loader) is not DurableDiagnosticActivationLoader:
            raise DiagnosticGatewayError("diagnostic activation loader is unverified")
        if activation_loader.authority is not authority:
            raise DiagnosticGatewayError("diagnostic activation authority changed")
        if type(coordinator) is not _ActivatedDiagnosticCoordinator:
            raise DiagnosticGatewayError("diagnostic host coordinator is unverified")
        if coordinator.authority is not authority or coordinator.transport is not transport:
            raise DiagnosticGatewayError("diagnostic host coordinator binding changed")
        if type(generation) is not int or generation < 1:
            raise DiagnosticGatewayError("invalid diagnostic generation")
        if generation != authority.session.generation:
            raise DiagnosticGatewayError("diagnostic generation is stale")
        if authority.session.state is not DiagnosticSessionState.ACTIVE:
            raise DiagnosticGatewayError("diagnostic host session is not active")
        if (
            not isinstance(_expected_activation_record_digest, str)
            or len(_expected_activation_record_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in _expected_activation_record_digest
            )
            or _expected_activation_record_digest == "0" * 64
        ):
            raise DiagnosticGatewayError("diagnostic activation digest is invalid")
        if not getattr(transport, "diagnostic_transport_v1", False) or not getattr(
            transport, "cancellation_cooperative", False
        ):
            raise DiagnosticGatewayError("unsealed diagnostic transport")
        if not inspect.iscoroutinefunction(transport.send_diagnostic_customer):
            raise DiagnosticGatewayError("diagnostic transport is not async")
        if (
            isinstance(max_provider_timeout_seconds, bool)
            or not isinstance(max_provider_timeout_seconds, int)
            or max_provider_timeout_seconds < 1
        ):
            raise DiagnosticGatewayError("invalid diagnostic timeout")
        transport_timeout = getattr(transport, "max_provider_timeout_seconds", 0)
        if (
            isinstance(transport_timeout, bool)
            or not isinstance(transport_timeout, int)
            or transport_timeout < 1
        ):
            raise DiagnosticGatewayError("invalid diagnostic transport timeout")
        self._authority = authority
        self._coordinator = coordinator
        self._admission_token = _DIAGNOSTIC_HOST_ADMISSION_TOKEN
        self._activation_loader = activation_loader
        self._activation_record_digest = _expected_activation_record_digest
        self._transport = transport
        self._generation = generation
        self._max_timeout = min(max_provider_timeout_seconds, transport_timeout, 30)
        if self._max_timeout < 1:
            raise DiagnosticGatewayError("invalid diagnostic timeout")
        self._admission_lock = asyncio.Lock()
        self._state = "active"
        self._routes: dict[tuple[int, int], DiagnosticRoleRoute] = {}
        self._reserved_spaces: frozenset[tuple[int, int]] = frozenset()


    @classmethod
    def _for_test(
        cls,
        authority: DiagnosticDeliveryAuthority,
        transport: TelegramDiagnosticTransport,
        *,
        generation: int,
        max_provider_timeout_seconds: int,
        activation_loader: DurableDiagnosticActivationLoader,
        coordinator: _ActivatedDiagnosticCoordinator,
        expected_activation_record_digest: str,
        factory_token: object,
    ) -> "DiagnosticHost":
        if factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic test host construction is sealed")
        return cls(
            authority,
            transport,
            generation=generation,
            max_provider_timeout_seconds=max_provider_timeout_seconds,
            activation_loader=activation_loader,
            coordinator=coordinator,
            _expected_activation_record_digest=expected_activation_record_digest,
            _factory_token=_DIAGNOSTIC_HOST_FACTORY_TOKEN,
        )
    def _session_ttl_seconds(self) -> float:
        session = getattr(self._authority, "_session", None)
        expires_at = getattr(session, "expires_at", None)
        if isinstance(expires_at, datetime):
            expiry = expires_at
        elif isinstance(expires_at, str):
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DiagnosticGatewayError("diagnostic session expiry is invalid") from exc
        else:
            raise DiagnosticGatewayError("diagnostic session expiry is unavailable")
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return (expiry.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()

    @staticmethod
    def _receipt_with_message_id(receipt: object) -> Mapping[str, object] | None:
        if not isinstance(receipt, Mapping) or receipt.get("ok") is not True:
            return None
        message_id = receipt.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
            return None
        normalized = str(message_id).strip()
        if not normalized or len(normalized) > 128:
            return None
        return {"ok": True, "message_id": normalized}

    def add_route(self, route: DiagnosticRoleRoute) -> None:
        raise DiagnosticGatewayError("diagnostic routes are immutable")

    def _install_routes(
        self,
        routes: tuple[DiagnosticRoleRoute, ...],
        *,
        factory_token: object,
    ) -> None:
        if factory_token is not _DIAGNOSTIC_ROUTE_INSTALL_TOKEN:
            raise DiagnosticGatewayError("diagnostic route installation is sealed")
        if self._routes or self._state != "active":
            raise DiagnosticGatewayError("diagnostic routes are immutable")
        if (
            type(routes) is not tuple
            or len(routes) != 3
            or any(
                type(route) is not DiagnosticRoleRoute
                or route.generation != self._generation
                for route in routes
            )
        ):
            raise DiagnosticGatewayError("diagnostic role routes are invalid")
        self._routes = {
            (route.chat_id, route.topic_id): route
            for route in routes
        }
        if len(self._routes) != len(routes):
            self._routes.clear()
            raise DiagnosticGatewayError("diagnostic role routes are not distinct")
        self._reserved_spaces = frozenset(self._routes)
    def owns_space(self, *, chat_id: int, topic_id: int) -> bool:
        try:
            self._authority.verify_route_active(generation=self._generation)
        except DiagnosticAuthorityError:
            return False
        return (chat_id, topic_id) in self._routes

    def reserves_space(self, *, chat_id: int, topic_id: int) -> bool:
        return (chat_id, topic_id) in self._reserved_spaces


    def authorize_route(self, *, user_id: int, chat_id: int, topic_id: int) -> DiagnosticRoleRoute:
        try:
            self._authority.verify_route_active(generation=self._generation)
        except DiagnosticAuthorityError as exc:
            raise DiagnosticGatewayError("diagnostic route is detached") from exc
        route = self._routes.get((chat_id, topic_id))
        if route is None or not route.matches(user_id=user_id, chat_id=chat_id, topic_id=topic_id, generation=self._generation):
            raise DiagnosticGatewayError("diagnostic route rejected")
        return route

    async def deliver_activated(self, session_id: str) -> Mapping[str, object] | object:
        coordinator = self._coordinator
        if coordinator is None:
            raise DiagnosticGatewayError("diagnostic host coordinator is unavailable")
        candidate = coordinator.candidate_for_session(session_id)
        return await self._deliver_verified(candidate, activated=candidate.activated)

    async def deliver(
        self,
        candidate: DiagnosticDeliveryCandidate,
    ) -> Mapping[str, object] | object:
        coordinator = self._coordinator
        if coordinator is None:
            raise DiagnosticGatewayError("diagnostic host coordinator is unavailable")
        coordinator.require_candidate(candidate)
        raise DiagnosticGatewayError(
            "diagnostic delivery requires activated coordinator provenance; "
            "persisted activation is required"
        )

    async def _deliver_verified(
        self,
        candidate: DiagnosticDeliveryCandidate,
        *,
        activated: ActivatedDiagnosticDelivery,
    ) -> Mapping[str, object] | object:
        coordinator = self._coordinator
        if coordinator is None:
            raise DiagnosticGatewayError("diagnostic host coordinator is unavailable")
        coordinator.require_candidate(candidate)
        if self._authority is not coordinator.authority:
            raise DiagnosticGatewayError("diagnostic authority changed")
        if self._transport is not coordinator.transport:
            raise DiagnosticGatewayError("diagnostic transport changed")
        if candidate.activated != activated:
            raise DiagnosticGatewayError("diagnostic activation candidate is stale")
        async with self._admission_lock:
            with self._authority.delivery_admission(self._admission_token) as authority_token:
                live_loader = getattr(self._authority, "live_loader", None)
                if type(live_loader) is not DiagnosticLiveAuthorityLoader:
                    raise DiagnosticGatewayError("diagnostic live authority loader is unverified")
                live_snapshot = live_loader.load_snapshot(
                    activated,
                    lock_token=authority_token,
                )
                coordinator.bind_live_snapshot(candidate, live_snapshot)
                self._authority.verify_activated_delivery(
                    candidate,
                    activated,
                    session_id=activated.session_id,
                    activation_loader=self._activation_loader,
                    lock_token=authority_token,
                )
                transport = self._transport
                if self._state != "active" or candidate.generation != self._generation:
                    raise DiagnosticGatewayError("diagnostic delivery detached")
                if transport is not self._transport:
                    raise DiagnosticGatewayError("diagnostic transport changed")
                session_ttl = self._session_ttl_seconds()
                effective_timeout = min(float(self._max_timeout), session_ttl)
                if effective_timeout <= 0:
                    raise DiagnosticGatewayError("diagnostic session expired")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + effective_timeout
                decision = self._authority.reserve_and_verify(
                    candidate,
                    lock_token=authority_token,
                )
                if not isinstance(decision, VerifiedDiagnosticReservation):
                    return decision
                if self._state != "active" or candidate.generation != self._generation:
                    raise DiagnosticGatewayError("diagnostic delivery detached")
                try:
                    decision = self._authority.verify_provider_start(
                        decision,
                        deadline_monotonic=deadline,
                        lock_token=authority_token,
                        activated=activated,
                        activation_loader=self._activation_loader,
                    )
                except DiagnosticAuthorityError as exc:
                    if not str(exc).startswith("diagnostic live"):
                        raise
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    return DiagnosticUnknownNoSend()
                if transport is not self._transport:
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    raise DiagnosticGatewayError("diagnostic transport changed")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    raise TimeoutError("diagnostic deadline elapsed")
                receipt: Mapping[str, object] | None = None
                try:
                    async with asyncio.timeout(remaining):
                        receipt = await transport.send_diagnostic_customer(
                            decision, deadline_monotonic=deadline
                        )
                except (asyncio.CancelledError, TimeoutError):
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    raise
                except Exception:
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    raise
                if transport is not self._transport:
                    self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                    raise DiagnosticGatewayError("diagnostic transport changed")
                normalized_receipt = self._receipt_with_message_id(receipt)
                if normalized_receipt is None:
                    return self._authority.record_terminal(
                        decision,
                        receipt=None,
                        audited=False,
                        lock_token=authority_token,
                    )
                return self._authority.record_terminal(
                    decision,
                    receipt=normalized_receipt,
                    audited=True,
                    lock_token=authority_token,
                )

    async def detach(self, *, terminal_state: str = "closed") -> None:
        async with self._admission_lock:
            if self._state != "active":
                return
            generation = self._generation
            self._state = "detaching"
            self._generation += 1
            self._routes.clear()
            try:
                self._authority.detach(
                    generation=generation,
                    state="detaching",
                )
            except BaseException:
                self._state = "recovery_required"
                raise
            self._state = terminal_state

    async def close(self) -> None:
        await self.detach(terminal_state="closed")

    async def expire(self) -> None:
        await self.detach(terminal_state="expired")

    async def stop(self) -> None:
        await self.detach(terminal_state="closed")


class DiagnosticControlService:
    def __init__(self, *, owner_user_id: int, review_chat_id: int, review_topic_id: int) -> None:
        values = (owner_user_id, review_chat_id, review_topic_id)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise DiagnosticGatewayError("diagnostic control identity is invalid")
        self._owner = owner_user_id
        self._surface = (review_chat_id, review_topic_id)

    @property
    def owner_user_id(self) -> int:
        return self._owner

    @property
    def review_chat_id(self) -> int:
        return self._surface[0]

    @property
    def review_topic_id(self) -> int:
        return self._surface[1]

    def matches_space(self, *, chat_id: int, topic_id: int) -> bool:
        return (chat_id, topic_id) == self._surface

    def authenticate(self, *, user_id: int, chat_id: int, topic_id: int) -> None:
        values = (user_id, chat_id, topic_id)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise DiagnosticGatewayError("diagnostic control rejected")
        if user_id != self._owner or (chat_id, topic_id) != self._surface:
            raise DiagnosticGatewayError("diagnostic control rejected")


class DormantDiagnosticController:
    """Typed dormant Topic-59 handle that can be consumed exactly once."""

    def __init__(
        self,
        *,
        spec: DiagnosticIsolationSpecV1,
        authority: DiagnosticDeliveryAuthority,
        control: DiagnosticControlService,
        adapter: object | None = None,
        _factory_token: object | None = None,
        _test_factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _DIAGNOSTIC_CONTROLLER_FACTORY_TOKEN:
            raise DiagnosticGatewayError("diagnostic controller construction is sealed")
        if (
            _test_factory_token is not None
            and _test_factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN
        ):
            raise DiagnosticGatewayError(
                "diagnostic test controller construction is sealed"
            )
        if type(spec) is not DiagnosticIsolationSpecV1:
            raise DiagnosticGatewayError("diagnostic isolation spec is unverified")
        if type(authority) is not DiagnosticDeliveryAuthority:
            raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
        if type(control) is not DiagnosticControlService:
            raise DiagnosticGatewayError("diagnostic control service is unverified")
        self._spec = spec
        self._authority = authority
        self._control = control
        self._adapter = adapter
        self._test_factory_token = _test_factory_token
        if adapter is not None:
            if self._test_factory_token is _DIAGNOSTIC_TEST_FACTORY_TOKEN:
                _require_test_adapter(
                    adapter,
                    expected_bot_digest=spec.test_bot_digest,
                    factory_token=self._test_factory_token,
                )
            else:
                _require_production_adapter(
                    adapter,
                    expected_bot_digest=spec.test_bot_digest,
                )
        self._generation = authority.session.generation
        self._activation_lock = threading.Lock()
        self._activation_consumed = False
        self._activation_loader: DurableDiagnosticActivationLoader | None = None
        self._host: DiagnosticHost | None = None

    @property
    def spec(self) -> DiagnosticIsolationSpecV1:
        return self._spec

    @property
    def authority(self) -> DiagnosticDeliveryAuthority:
        return self._authority

    @property
    def control(self) -> DiagnosticControlService:
        return self._control

    @property
    def session(self):
        return self._authority.session

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def host(self) -> DiagnosticHost | None:
        return self._host

    @property
    def active(self) -> bool:
        return self._host is not None

    def bind_adapter(self, adapter: object) -> None:
        """Bind only the started, exact production TelegramAdapter."""
        _require_production_adapter(
            adapter,
            expected_bot_digest=self._spec.test_bot_digest,
        )
        if self._adapter is not None and self._adapter is not adapter:
            raise DiagnosticGatewayError("diagnostic adapter changed")
        self._adapter = adapter


    async def deliver_activated(self, session_id: str) -> Mapping[str, object] | object:
        host = self._host
        if host is None:
            raise DiagnosticGatewayError("diagnostic isolation is dormant")
        try:
            return await host.deliver_activated(session_id)
        except DiagnosticAuthorityError as exc:
            if str(exc) == "diagnostic session is durably detached":
                raise DiagnosticGatewayError("diagnostic delivery detached") from exc
            raise

    async def deliver(self, candidate: DiagnosticDeliveryCandidate) -> Mapping[str, object] | object:
        host = self._host
        if host is None:
            raise DiagnosticGatewayError("diagnostic isolation is dormant")
        return await host.deliver(candidate)

    def activate(
        self,
        *,
        user_id: int,
        chat_id: int,
        topic_id: int,
        generation: int,
        adapter: object | None = None,
        routes: tuple[DiagnosticRoleRoute, ...] = (),
    ) -> DiagnosticHost:
        """Authenticate and consume the dormant session before any delivery admission."""
        with self._activation_lock:
            if self._activation_consumed or self._host is not None:
                raise DiagnosticGatewayError("diagnostic activation has already been consumed")
            self._control.authenticate(
                user_id=user_id,
                chat_id=chat_id,
                topic_id=topic_id,
            )
            if type(generation) is not int or generation < 1:
                raise DiagnosticGatewayError("diagnostic activation generation is invalid")
            if generation != self._generation:
                raise DiagnosticGatewayError("diagnostic activation generation is stale")
            session = self._authority.session
            if session.state is not DiagnosticSessionState.PREPARED:
                raise DiagnosticGatewayError("diagnostic session is not dormant")
            if self._authority.restart_unvalidated:
                raise DiagnosticGatewayError("diagnostic restart requires revalidation")
            try:
                expiry = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise DiagnosticGatewayError("diagnostic session expiry is invalid") from exc
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise DiagnosticGatewayError("diagnostic session expired")
            selected_adapter = adapter if adapter is not None else self._adapter
            if selected_adapter is None:
                raise DiagnosticGatewayError("diagnostic adapter is unavailable")
            if selected_adapter is not self._adapter:
                raise DiagnosticGatewayError("diagnostic adapter changed")
            if self._test_factory_token is _DIAGNOSTIC_TEST_FACTORY_TOKEN:
                _require_test_adapter(
                    selected_adapter,
                    expected_bot_digest=self._spec.test_bot_digest,
                    factory_token=self._test_factory_token,
                )
            else:
                _require_production_adapter(
                    selected_adapter,
                    expected_bot_digest=self._spec.test_bot_digest,
                )
            if getattr(selected_adapter, "_nutrition_coaching", None) is not None:
                raise DiagnosticGatewayError("diagnostic host cannot share a production coordinator")
            for route in routes:
                if (
                    type(route) is not DiagnosticRoleRoute
                    or route.generation != generation
                ):
                    raise DiagnosticGatewayError("diagnostic role route is invalid")
            if routes:
                raise DiagnosticGatewayError(
                    "diagnostic role routes must come from the registered runtime"
                )

            with self._authority.delivery_admission(
                _DIAGNOSTIC_HOST_ADMISSION_TOKEN
            ) as authority_token:
                loader = DurableDiagnosticActivationLoader(self._authority)
                activated = loader.load_activated_delivery(
                    session.session_id,
                    lock_token=authority_token,
                )
                live_loader = self._authority.live_loader
                live_loader.load_snapshot(
                    activated,
                    lock_token=authority_token,
                )
                runtime_loader = _ProfileDiagnosticRuntimeLoader(
                    self._authority,
                    activated,
                    _factory_token=_DIAGNOSTIC_RUNTIME_LOADER_FACTORY_TOKEN,
                )
                customer_runtime = runtime_loader.load_runtime(activated.customer_key_digest)
                operator_route = runtime_loader.load_owner_route(generation=generation)
                if (
                    operator_route.user_id != self._control.owner_user_id
                    or operator_route.chat_id != self._control.review_chat_id
                    or operator_route.topic_id != self._control.review_topic_id
                ):
                    raise DiagnosticGatewayError(
                        "diagnostic control route does not match the registered owner"
                    )
                binding = VerifiedDiagnosticTransportBinding.from_verified_spec(
                    self._spec,
                    self._authority,
                    loader,
                )
                if self._test_factory_token is _DIAGNOSTIC_TEST_FACTORY_TOKEN:
                    transport = TelegramDiagnosticTransport._for_test(
                        selected_adapter,
                        binding,
                        self._authority,
                        factory_token=self._test_factory_token,
                    )
                else:
                    transport = TelegramDiagnosticTransport.for_diagnostic(
                        selected_adapter,
                        binding,
                        self._authority,
                    )
                coordinator = _ActivatedDiagnosticCoordinator(
                    customer_runtime=customer_runtime,
                    authority=self._authority,
                    transport=transport,
                    activation_loader=loader,
                    activated=activated,
                    runtime_loader=runtime_loader,
                    _factory_token=_DIAGNOSTIC_COORDINATOR_FACTORY_TOKEN,
                )
                self._authority.activate_session(generation=generation)
                if self._test_factory_token is _DIAGNOSTIC_TEST_FACTORY_TOKEN:
                    host = DiagnosticHost._for_test(
                        self._authority,
                        transport,
                        generation=generation,
                        max_provider_timeout_seconds=binding.max_provider_timeout_seconds,
                        activation_loader=loader,
                        coordinator=coordinator,
                        expected_activation_record_digest=binding.activation_record_digest,
                        factory_token=self._test_factory_token,
                    )
                else:
                    host = DiagnosticHost(
                        self._authority,
                        transport,
                        generation=generation,
                        max_provider_timeout_seconds=binding.max_provider_timeout_seconds,
                        activation_loader=loader,
                        coordinator=coordinator,
                        _expected_activation_record_digest=binding.activation_record_digest,
                        _factory_token=_DIAGNOSTIC_HOST_FACTORY_TOKEN,
                    )
                role_routes = _registered_role_routes(
                    customer_runtime,
                    operator=operator_route,
                    generation=generation,
                )
                host._install_routes(
                    role_routes,
                    factory_token=_DIAGNOSTIC_ROUTE_INSTALL_TOKEN,
                )
                self._activation_consumed = True
                self._activation_loader = loader
                self._host = host
                self._adapter = selected_adapter
                setattr(selected_adapter, "_diagnostic_isolation_controller", self)
                setattr(selected_adapter, "_diagnostic_control_service", self._control)
                setattr(selected_adapter, "_diagnostic_isolation_host", host)
                return host

    async def close(self) -> None:
        host = self._host
        if host is not None:
            await host.close()


def _require_record_backed_live_sources(
    authority: DiagnosticDeliveryAuthority,
) -> None:
    if type(authority) is not DiagnosticDeliveryAuthority:
        raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
    sources = authority.live_sources
    if type(sources) is not DiagnosticLiveAuthoritySources:
        raise DiagnosticGatewayError("diagnostic live sources are unverified")
    if sources.authority is not authority or sources.spec is not authority.spec:
        raise DiagnosticGatewayError("diagnostic live source binding changed")
    record_path = sources.record_path
    expected_path = (
        authority.profile_root.absolute()
        / "data"
        / "diagnostic-isolation"
        / "live-authority.json"
    )
    if type(record_path) is not type(Path()) or record_path != expected_path:
        raise DiagnosticGatewayError("diagnostic live source is not record-backed")
def _build_dormant_diagnostic_controller(
    *,
    spec: DiagnosticIsolationSpecV1,
    authority: DiagnosticDeliveryAuthority,
    owner_user_id: int,
    review_chat_id: int,
    review_topic_id: int,
    adapter: object | None = None,
    test_factory_token: object | None = None,
) -> DormantDiagnosticController:
    if type(spec) is not DiagnosticIsolationSpecV1:
        raise DiagnosticGatewayError("diagnostic isolation spec is required")
    if type(authority) is not DiagnosticDeliveryAuthority:
        raise DiagnosticGatewayError("diagnostic delivery authority is unverified")
    _require_record_backed_live_sources(authority)
    bound_spec = authority.spec
    if bound_spec is None:
        raise DiagnosticGatewayError("diagnostic authority spec is required")
    if bound_spec is not spec:
        raise DiagnosticGatewayError("diagnostic authority spec changed")
    session = authority.session
    if (
        spec.spec_digest != session.spec_digest
        or spec.authority_digest != session.authority_digest
        or spec.diagnostic_transport_binding_digest != session.transport_binding_digest
    ):
        raise DiagnosticGatewayError("diagnostic isolation spec is stale")
    if session.state is not DiagnosticSessionState.PREPARED:
        raise DiagnosticGatewayError("diagnostic session is not dormant")
    if authority.restart_unvalidated:
        raise DiagnosticGatewayError("diagnostic restart requires revalidation")
    values = (owner_user_id, review_chat_id, review_topic_id)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise DiagnosticGatewayError("diagnostic control identity is invalid")
    if review_topic_id != 59:
        raise DiagnosticGatewayError("diagnostic control topic must be 59")
    control = DiagnosticControlService(
        owner_user_id=owner_user_id,
        review_chat_id=review_chat_id,
        review_topic_id=review_topic_id,
    )
    return DormantDiagnosticController(
        spec=spec,
        authority=authority,
        control=control,
        _factory_token=_DIAGNOSTIC_CONTROLLER_FACTORY_TOKEN,
        adapter=adapter,
        _test_factory_token=test_factory_token,
    )


def bootstrap_diagnostic_isolation(
    *,
    spec: DiagnosticIsolationSpecV1,
    authority: DiagnosticDeliveryAuthority,
    owner_user_id: int,
    review_chat_id: int,
    review_topic_id: int,
    adapter: object | None,
) -> DormantDiagnosticController:
    """Create the dormant production Topic-59 control boundary."""
    if adapter is None:
        raise DiagnosticGatewayError("diagnostic production adapter is required")
    controller = _build_dormant_diagnostic_controller(
        spec=spec,
        authority=authority,
        owner_user_id=owner_user_id,
        review_chat_id=review_chat_id,
        review_topic_id=review_topic_id,
    )
    controller.bind_adapter(adapter)
    return controller


def _bootstrap_diagnostic_isolation_for_test(
    *,
    spec: DiagnosticIsolationSpecV1,
    authority: DiagnosticDeliveryAuthority,
    owner_user_id: int,
    review_chat_id: int,
    review_topic_id: int,
    adapter: object,
    factory_token: object,
) -> DormantDiagnosticController:
    if factory_token is not _DIAGNOSTIC_TEST_FACTORY_TOKEN:
        raise DiagnosticGatewayError("diagnostic test bootstrap is sealed")
    _require_test_adapter(
        adapter,
        expected_bot_digest=spec.test_bot_digest,
        factory_token=factory_token,
    )
    return _build_dormant_diagnostic_controller(
        spec=spec,
        authority=authority,
        owner_user_id=owner_user_id,
        review_chat_id=review_chat_id,
        review_topic_id=review_topic_id,
        adapter=adapter,
        test_factory_token=factory_token,
    )
