from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import checkin_cli.diagnostic_isolation as profile_diagnostic

from checkin_cli.diagnostic_isolation import (
    AUDIT_PENDING_TEXT,
    DUPLICATE_TEXT,
    UNKNOWN_TEXT,
    ActivatedDiagnosticDelivery,
    DiagnosticAuditPendingNoSend,
    DiagnosticDeliveryAuthority,
    DiagnosticDeliveryCandidate,
    DiagnosticDuplicateNoSend,
    DiagnosticIsolationSpecV1,
    DiagnosticRoleRoute,
    DiagnosticSession,
    DiagnosticSessionState,
    DiagnosticUnknownNoSend,
    VerifiedDiagnosticReservation,
    _DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
    _DIAGNOSTIC_HOST_ADMISSION_TOKEN,
)
from gateway.platforms.diagnostic_isolation import (
    DiagnosticControlService,
    DiagnosticGatewayError,
    DiagnosticHost,
    DormantDiagnosticController,
    TelegramDiagnosticTransport,
    _ActivatedDiagnosticCoordinator,
    bootstrap_diagnostic_isolation,
    _DIAGNOSTIC_TEST_FACTORY_TOKEN,
    _bind_test_adapter,
    _bootstrap_diagnostic_isolation_for_test,
    _DiagnosticTestAdapter,
)


def _spec() -> DiagnosticIsolationSpecV1:
    destination = ("-100", "71")
    destination_digest = hashlib.sha256(
        json.dumps(destination, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DiagnosticIsolationSpecV1(
        owner_digest="1" * 64,
        test_bot_digest="2" * 64,
        customer_destination_digest=destination_digest,
        trainer_destination_digest="4" * 64,
        operator_destination_digest="5" * 64,
        profile_digest="6" * 64,
        max_provider_timeout_seconds=2,
        expires_at="2099-01-01T00:00:00Z",
        approved_by="owner",
        approved_at="2026-01-01T00:00:00Z",
    )
_TEST_ACTIVATION_DIGEST = profile_diagnostic._digest({
    "activation_receipt_id": None,
    "activation_receipt_digest": None,
    "authority_digest": None,
    "customer_key": "client_001",
})


def _live_sources(
    spec: DiagnosticIsolationSpecV1,
    *,
    customer_key_digest: str = "7" * 64,
    registry_digest: str = "9" * 64,
):
    token = profile_diagnostic._DIAGNOSTIC_LIVE_LOADER_FACTORY_TOKEN
    loaders = (
        profile_diagnostic.DiagnosticOwnerLiveLoader(
            None, spec, profile_diagnostic.DiagnosticOwnerSnapshot("1" * 64, "active"),
            factory_token=token,
        ),
        profile_diagnostic.DiagnosticRegistryLiveLoader(
            None, spec, profile_diagnostic.DiagnosticRegistrySnapshot(
                customer_key_digest, registry_digest, "f" * 64, "6" * 64, "enabled"
            ), factory_token=token,
        ),
        profile_diagnostic.DiagnosticConsentActivationLiveLoader(
            None, spec, profile_diagnostic.DiagnosticConsentActivationSnapshot(
                "5" * 64, _TEST_ACTIVATION_DIGEST, "s", 1, "2099-01-01T00:00:00Z",
                "activated", False,
            ), factory_token=token,
        ),
        profile_diagnostic.DiagnosticProposalLiveLoader(
            None, spec, profile_diagnostic.DiagnosticProposalSnapshot(
                "8" * 64, 1, profile_diagnostic._digest(1),
                hashlib.sha256(b"diagnostic only").hexdigest(),
                spec.customer_destination_digest, "approved",
                "2099-01-01T00:00:00Z",
            ), factory_token=token,
        ),
        profile_diagnostic.DiagnosticConfigLiveLoader(
            None, spec, profile_diagnostic.DiagnosticConfigSnapshot(
                "b" * 64, "4" * 64,
                spec.diagnostic_transport_binding_digest, "active",
            ), factory_token=token,
        ),
        profile_diagnostic.DiagnosticArtifactLiveLoader(
            None, spec, profile_diagnostic.DiagnosticArtifactSnapshot(
                "c" * 64, "d" * 64, "e" * 64, "approved"
            ), factory_token=token,
        ),
    )
    return profile_diagnostic.DiagnosticLiveAuthoritySources.from_verified_loaders(
        authority=None,
        spec=spec,
        owner_loader=loaders[0],
        registry_loader=loaders[1],
        consent_activation_loader=loaders[2],
        proposal_loader=loaders[3],
        config_loader=loaders[4],
        artifact_loader=loaders[5],
        factory_token=token,
    )


def _write_live_authority_record(
    tmp_path: Path,
    spec: DiagnosticIsolationSpecV1,
    *,
    customer_key_digest: str,
    registry_digest: str,
) -> None:
    body = b"diagnostic only"
    destination = ("-100", "71")
    destination_digest = hashlib.sha256(
        json.dumps(destination, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot = profile_diagnostic.LiveDiagnosticAuthoritySnapshot(
        schema_version="live_diagnostic_authority_v1",
        customer_key_digest=customer_key_digest,
        session_id="s",
        session_generation=1,
        owner_digest="1" * 64,
        registry_digest=registry_digest,
        consent_digest="5" * 64,
        activation_receipt_digest=_TEST_ACTIVATION_DIGEST,
        proposal_digest="8" * 64,
        revision=1,
        revision_digest=profile_diagnostic._digest(1),
        rendered_body_digest=hashlib.sha256(body).hexdigest(),
        destination_digest=destination_digest,
        config_digest="b" * 64,
        policy_digest="c" * 64,
        catalog_digest="d" * 64,
        meal_constraints_digest="e" * 64,
        source_digest="f" * 64,
        registration_digest="6" * 64,
        epoch_digest="4" * 64,
        diagnostic_transport_binding_digest=spec.diagnostic_transport_binding_digest,
        state="activated",
        expires_at_kst="2099-01-01T00:00:00Z",
        revoked=False,
    )
    record_path = tmp_path / "data" / "diagnostic-isolation" / "live-authority.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").chmod(0o700)
    record_path.parent.chmod(0o700)
    record_path.write_text(
        json.dumps(snapshot.to_closed_record(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    record_path.chmod(0o600)


def _authority(
    tmp_path: Path,
    spec: DiagnosticIsolationSpecV1,
    *,
    state=DiagnosticSessionState.PREPARED,
    customer_key_digest: str = "7" * 64,
    registry_digest: str = "9" * 64,
):
    session = DiagnosticSession(
        "s",
        state,
        1,
        "boot",
        spec.spec_digest,
        spec.authority_digest,
        spec.diagnostic_transport_binding_digest,
        "2099-01-01T00:00:00Z",
    )
    _write_live_authority_record(
        tmp_path,
        spec,
        customer_key_digest=customer_key_digest,
        registry_digest=registry_digest,
    )
    return DiagnosticDeliveryAuthority.from_profile_record(
        session,
        boot_epoch="boot",
        profile_root=tmp_path,
        spec=spec,
    )


def _activated(
    spec: DiagnosticIsolationSpecV1,
    *,
    customer_key_digest: str = "7" * 64,
    registry_digest: str = "9" * 64,
) -> ActivatedDiagnosticDelivery:
    body = b"diagnostic only"
    destination = ("-100", "71")
    destination_digest = hashlib.sha256(
        json.dumps(destination, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ActivatedDiagnosticDelivery.from_persisted(
        schema_version="diagnostic_activated_delivery_v1",
        customer_key_digest=customer_key_digest,
        proposal_digest="8" * 64,
        revision=1,
        rendered_body=body,
        rendered_body_digest=hashlib.sha256(body).hexdigest(),
        destination=destination,
        destination_digest=destination_digest,
        session_id="s",
        session_generation=1,
        diagnostic_transport_binding_digest=spec.diagnostic_transport_binding_digest,
        registry_digest=registry_digest,
        activation_receipt_digest=_TEST_ACTIVATION_DIGEST,
        config_digest="b" * 64,
        policy_digest="c" * 64,
        catalog_digest="d" * 64,
        meal_constraints_digest="e" * 64,
        expires_at_kst="2099-01-01T00:00:00Z",
    )


def _persist(authority: DiagnosticDeliveryAuthority, activated: ActivatedDiagnosticDelivery) -> None:
    authority.activation_path.write_text(
        json.dumps(activated.to_persisted_record(), sort_keys=True),
        encoding="utf-8",
    )
    authority.activation_path.chmod(0o600)


_UNSET_RECEIPT = object()


class FakeBot:
    def __init__(
        self,
        *,
        receipt: object = _UNSET_RECEIPT,
        error: BaseException | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []
        self.receipt = (
            SimpleNamespace(message_id=1)
            if receipt is _UNSET_RECEIPT
            else receipt
        )
        self.error = error
        self.started = started
        self.release = release

    async def send_message(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.receipt


def _adapter(spec: DiagnosticIsolationSpecV1, bot: FakeBot):
    return _bind_test_adapter(
        bot,
        spec.test_bot_digest,
        factory_token=_DIAGNOSTIC_TEST_FACTORY_TOKEN,
    )


def _write_diagnostic_registry(
    tmp_path: Path,
    spec: DiagnosticIsolationSpecV1,
) -> tuple[str, str]:
    customer_key = "client_001"
    payload = {
        "version": 1,
        "registry_mode": "diagnostic_isolated_v1",
        "diagnostic_session_digest": spec.spec_digest,
        "owner": {"user_id": "7", "chat_id": "-100", "topic_id": "59"},
        "customers": [{
            "customer_key": customer_key,
            "display_name": "진단 고객",
            "enabled": True,
            "activation_receipt_digest": "a" * 64,
            "telegram": {"user_id": "7", "chat_id": "-100", "topic_id": "71"},
            "trainer": {"user_id": "7", "chat_id": "-100", "topic_id": "72"},
            "schedule": {"daily_time": "08:00", "weekly_weekday": 0, "monthly_day": 1},
            "profile": {"primary_goal": "진단"},
            "ai_processing_consent": {
                "granted": True,
                "recorded_on": "2026-07-19",
                "notice_version": "privacy-v1",
            },
            "plan": {
                "starts_on": "2026-07-20",
                "focus": "nutrition_90_training_10",
                "weeks": [
                    {
                        "week": week,
                        "calories_kcal": 2300,
                        "protein_g": 150,
                        "meal_structure": ["아침", "점심", "저녁"],
                    }
                    for week in range(1, 13)
                ],
            },
        }],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    registry_dir = tmp_path / "customers"
    registry_dir.mkdir()
    (registry_dir / "registry.json").write_bytes(raw)
    return (
        profile_diagnostic._digest({"customer_key": customer_key}),
        hashlib.sha256(raw).hexdigest(),
    )


def _bootstrap(
    tmp_path: Path,
    *,
    record: bool = True,
    bot: FakeBot | None = None,
):
    spec = _spec()
    customer_key_digest, registry_digest = _write_diagnostic_registry(tmp_path, spec)
    authority = _authority(
        tmp_path,
        spec,
        customer_key_digest=customer_key_digest,
        registry_digest=registry_digest,
    )
    activated = _activated(
        spec,
        customer_key_digest=customer_key_digest,
        registry_digest=registry_digest,
    )
    if record:
        _persist(authority, activated)
    bot = bot or FakeBot()
    adapter = _adapter(spec, bot)
    controller = _bootstrap_diagnostic_isolation_for_test(
        spec=spec,
        authority=authority,
        owner_user_id=7,
        review_chat_id=-100,
        review_topic_id=59,
        adapter=adapter,
        factory_token=_DIAGNOSTIC_TEST_FACTORY_TOKEN,
    )
    return spec, authority, activated, bot, adapter, controller


def test_bootstrap_is_dormant_and_does_not_read_activation(tmp_path: Path):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path, record=False)
    assert isinstance(controller, DormantDiagnosticController)
    assert controller.host is None
    assert controller.generation == authority.session.generation == 1
    assert not authority.activation_path.exists()
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.asyncio
async def test_pre_activation_delivery_fails_without_rows_or_provider(tmp_path: Path):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path, record=False
    )
    with pytest.raises(DiagnosticGatewayError, match="dormant"):
        await controller.deliver_activated(activated.session_id)
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_wrong_user_and_stale_generation_are_rejected_before_loader(tmp_path: Path):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    with pytest.raises(DiagnosticGatewayError, match="control"):
        controller.activate(
            user_id=8,
            chat_id=-100,
            topic_id=59,
            generation=1,
        )
    with pytest.raises(DiagnosticGatewayError, match="stale"):
        controller.activate(
            user_id=7,
            chat_id=-100,
            topic_id=59,
            generation=2,
        )
    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_restart_generation_cannot_activate_again(tmp_path: Path):
    spec = _spec()
    authority = _authority(tmp_path, spec)
    _persist(authority, _activated(spec))
    restarted = DiagnosticDeliveryAuthority.from_profile_record(
        authority.session,
        boot_epoch="boot",
        profile_root=tmp_path,
        spec=spec,
    )
    bot = FakeBot()
    adapter = _adapter(spec, bot)
    with pytest.raises(DiagnosticGatewayError, match="restart"):
        bootstrap_diagnostic_isolation(
            spec=spec,
            authority=restarted,
            owner_user_id=7,
            review_chat_id=-100,
            review_topic_id=59,
            adapter=adapter,
        )


@pytest.mark.asyncio
async def test_authenticated_activation_is_one_use_and_reaches_provider(tmp_path: Path):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    assert bot.calls == 0
    host = controller.activate(
        user_id=7,
        chat_id=-100,
        topic_id=59,
        generation=1,
    )
    assert controller.host is host
    assert authority.session.state is DiagnosticSessionState.ACTIVE
    host.authorize_route(user_id=7, chat_id=-100, topic_id=59)
    result = await controller.deliver_activated(activated.session_id)
    assert result["status"] == "sent_audited"
    assert bot.calls == 1
    with pytest.raises(DiagnosticGatewayError, match="consumed"):
        controller.activate(
            user_id=7,
            chat_id=-100,
            topic_id=59,
            generation=1,
        )


def test_spec_less_authority_is_rejected(tmp_path: Path):
    spec = _spec()
    session = DiagnosticSession(
        "s",
        DiagnosticSessionState.PREPARED,
        1,
        "boot",
        spec.spec_digest,
        spec.authority_digest,
        spec.diagnostic_transport_binding_digest,
        "2099-01-01T00:00:00Z",
    )
    with pytest.raises(Exception, match="required"):
        DiagnosticDeliveryAuthority(
            session,
            boot_epoch="boot",
            profile_root=tmp_path,
            spec=None,
            live_sources=object(),
        )


@pytest.mark.asyncio
async def test_public_generic_delivery_is_rejected_without_lifecycle(tmp_path: Path):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    host = controller.activate(
        user_id=7,
        chat_id=-100,
        topic_id=59,
        generation=1,
    )
    candidate = DiagnosticDeliveryCandidate.from_activated(
        activated,
        factory_token=_DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
        session_digest=spec.spec_digest,
    )
    with pytest.raises(DiagnosticGatewayError, match="provenance"):
        await host.deliver(candidate)
    assert authority.rows(candidate.dedupe_key) == ()
    assert bot.calls == 0


def test_test_host_constructor_is_private_and_token_sealed():
    assert "_for_test" in vars(DiagnosticHost)
    with pytest.raises(DiagnosticGatewayError, match="test host construction"):
        DiagnosticHost._for_test(
            object(),
            object(),
            generation=1,
            max_provider_timeout_seconds=1,
            activation_loader=object(),
            coordinator=object(),
            expected_activation_record_digest="a" * 64,
            factory_token=object(),
        )
@pytest.mark.parametrize(
    "kind",
    ("none", "simple_namespace", "exact_adapter_fake_bot", "subclass"),
)
def test_production_binding_rejects_untrusted_adapter_before_activation(
    tmp_path: Path,
    kind: str,
):
    spec, authority, activated, bot, _adapter_obj, _bound_controller = _bootstrap(
        tmp_path,
        record=False,
    )
    from gateway.platforms.telegram import TelegramAdapter
    if kind == "none":
        candidate = None
    elif kind == "simple_namespace":
        candidate = SimpleNamespace(
            bot=bot,
            _diagnostic_test_bot_digest=spec.test_bot_digest,
        )
    elif kind == "exact_adapter_fake_bot":
        candidate = object.__new__(TelegramAdapter)
        candidate._bot = bot
    else:
        class TelegramAdapterSubclass(TelegramAdapter):
            pass

        candidate = object.__new__(TelegramAdapterSubclass)
        candidate._bot = bot

    with pytest.raises(DiagnosticGatewayError):
        bootstrap_diagnostic_isolation(
            spec=spec,
            authority=authority,
            owner_user_id=7,
            review_chat_id=-100,
            review_topic_id=59,
            adapter=candidate,
        )

    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_production_bootstrap_rejects_sealed_test_adapter(tmp_path: Path):
    spec, authority, activated, bot, adapter, _controller = _bootstrap(
        tmp_path,
        record=False,
    )
    with pytest.raises(DiagnosticGatewayError):
        bootstrap_diagnostic_isolation(
            spec=spec,
            authority=authority,
            owner_user_id=7,
            review_chat_id=-100,
            review_topic_id=59,
            adapter=adapter,
        )
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_private_test_adapter_and_bootstrap_require_exact_token_and_type(
    tmp_path: Path,
):
    spec, authority, activated, bot, adapter, _controller = _bootstrap(
        tmp_path,
        record=False,
    )

    class TestAdapterSubclass(_DiagnosticTestAdapter):
        pass

    with pytest.raises(DiagnosticGatewayError, match="sealed"):
        _bind_test_adapter(bot, spec.test_bot_digest, factory_token=object())
    forged = object.__new__(TestAdapterSubclass)
    forged._bot = bot
    forged._diagnostic_test_bot_digest = spec.test_bot_digest
    forged._nutrition_coaching = None
    with pytest.raises(DiagnosticGatewayError):
        _bootstrap_diagnostic_isolation_for_test(
            spec=spec,
            authority=authority,
            owner_user_id=7,
            review_chat_id=-100,
            review_topic_id=59,
            adapter=forged,
            factory_token=_DIAGNOSTIC_TEST_FACTORY_TOKEN,
        )
    with pytest.raises(DiagnosticGatewayError, match="sealed"):
        _bootstrap_diagnostic_isolation_for_test(
            spec=spec,
            authority=authority,
            owner_user_id=7,
            review_chat_id=-100,
            review_topic_id=59,
            adapter=adapter,
            factory_token=object(),
        )
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_diagnostic_controller_and_host_constructors_require_factory_tokens(
    tmp_path: Path,
):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        record=False,
    )
    control = DiagnosticControlService(
        owner_user_id=7,
        review_chat_id=-100,
        review_topic_id=59,
    )
    with pytest.raises(DiagnosticGatewayError, match="controller construction"):
        DormantDiagnosticController(
            spec=spec,
            authority=authority,
            control=control,
        )
    with pytest.raises(DiagnosticGatewayError, match="controller construction"):
        DormantDiagnosticController(
            spec=spec,
            authority=authority,
            control=control,
            _factory_token=object(),
        )
    with pytest.raises(DiagnosticGatewayError, match="host construction"):
        DiagnosticHost(
            None,
            None,
            generation=0,
            max_provider_timeout_seconds=0,
            activation_loader=None,
            _expected_activation_record_digest="",
        )

    assert controller.host is None
    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_transport_constructor_requires_factory_token_before_activation(tmp_path: Path):
    spec, authority, activated, bot, adapter, _controller = _bootstrap(
        tmp_path,
        record=True,
    )

    with pytest.raises(DiagnosticGatewayError, match="transport construction"):
        TelegramDiagnosticTransport(
            adapter,
            bot=bot,
            destination=(-100, 71),
            binding=spec.diagnostic_transport_binding_digest,
            max_timeout=spec.max_provider_timeout_seconds,
            authority=authority,
            bot_digest=spec.test_bot_digest,
        )

    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.asyncio
async def test_untrusted_activate_argument_has_zero_routes_reservations_and_provider(
    tmp_path: Path,
):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    with pytest.raises(DiagnosticGatewayError, match="changed"):
        controller.activate(
            user_id=7,
            chat_id=-100,
            topic_id=59,
            generation=1,
            adapter=SimpleNamespace(
                bot=bot,
                _diagnostic_test_bot_digest=spec.test_bot_digest,
            ),
        )

    assert controller.host is None
    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0

def _activate(
    controller: DormantDiagnosticController,
    *,
    routes: tuple[DiagnosticRoleRoute, ...] = (),
) -> DiagnosticHost:
    return controller.activate(
        user_id=7,
        chat_id=-100,
        topic_id=59,
        generation=1,
        routes=routes,
    )


def test_activation_derives_customer_and_trainer_routes_from_registered_runtime(
    tmp_path: Path,
):
    _spec_obj, _authority, _activated, _bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )

    host = _activate(controller)

    customer = host.authorize_route(user_id=7, chat_id=-100, topic_id=71)
    trainer = host.authorize_route(user_id=7, chat_id=-100, topic_id=72)
    operator = host.authorize_route(user_id=7, chat_id=-100, topic_id=59)
    assert (operator.role, customer.role, trainer.role) == (
        "operator",
        "customer",
        "trainer",
    )


def test_activation_rejects_caller_supplied_routes_before_rows_or_provider(
    tmp_path: Path,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )

    with pytest.raises(
        DiagnosticGatewayError,
        match="must come from the registered runtime",
    ):
        _activate(
            controller,
            routes=(
                DiagnosticRoleRoute(7, -100, 73, "customer", 1),
            ),
        )

    assert controller.host is None
    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.parametrize(
    ("owner_user_id", "review_chat_id", "review_topic_id"),
    (
        (8, -100, 59),
        (7, -101, 59),
        (7, -100, 60),
    ),
)
def test_bootstrap_control_must_match_registered_owner_before_activation(
    tmp_path: Path,
    owner_user_id: int,
    review_chat_id: int,
    review_topic_id: int,
):
    spec = _spec()
    customer_key_digest, registry_digest = _write_diagnostic_registry(tmp_path, spec)
    authority = _authority(
        tmp_path,
        spec,
        customer_key_digest=customer_key_digest,
        registry_digest=registry_digest,
    )
    activated = _activated(
        spec,
        customer_key_digest=customer_key_digest,
        registry_digest=registry_digest,
    )
    _persist(authority, activated)
    bot = FakeBot()
    adapter = _adapter(spec, bot)
    controller = None
    with pytest.raises(
        DiagnosticGatewayError,
        match="registered owner|control topic must be 59",
    ):
        controller = _bootstrap_diagnostic_isolation_for_test(
            spec=spec,
            authority=authority,
            owner_user_id=owner_user_id,
            review_chat_id=review_chat_id,
            review_topic_id=review_topic_id,
            adapter=adapter,
            factory_token=_DIAGNOSTIC_TEST_FACTORY_TOKEN,
        )
        controller.activate(
            user_id=owner_user_id,
            chat_id=review_chat_id,
            topic_id=review_topic_id,
            generation=1,
        )

    assert controller is None or controller.host is None
    assert authority.session.state is DiagnosticSessionState.PREPARED
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_active_host_rejects_post_activation_route_injection(tmp_path: Path):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )
    host = _activate(controller)

    with pytest.raises(DiagnosticGatewayError, match="immutable"):
        host.add_route(DiagnosticRoleRoute(7, -100, 73, "customer", 1))

    assert not host.owns_space(chat_id=-100, topic_id=73)
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_durable_detach_revokes_cached_routes_before_ingress(tmp_path: Path):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )
    host = _activate(controller)
    assert host.owns_space(chat_id=-100, topic_id=71)

    authority.detach(generation=1, state="detaching")

    assert not host.owns_space(chat_id=-100, topic_id=71)
    with pytest.raises(DiagnosticGatewayError, match="detached"):
        host.authorize_route(user_id=7, chat_id=-100, topic_id=71)
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def _candidate(
    spec: DiagnosticIsolationSpecV1,
    activated: ActivatedDiagnosticDelivery,
) -> DiagnosticDeliveryCandidate:
    candidate = DiagnosticDeliveryCandidate.from_activated(
        activated,
        factory_token=_DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
        session_digest=spec.spec_digest,
    )
    values = {
        **dict(activated.pins),
        "owner_digest": "1" * 64,
        "consent_digest": "5" * 64,
        "source_digest": "f" * 64,
        "registration_digest": "6" * 64,
        "epoch_digest": "4" * 64,
        "diagnostic_transport_binding_digest": spec.diagnostic_transport_binding_digest,
    }
    for name, value in values.items():
        object.__setattr__(candidate, name, value)
    object.__setattr__(candidate, "revision", activated.revision)
    return candidate


def _statuses(
    authority: DiagnosticDeliveryAuthority,
    activated: ActivatedDiagnosticDelivery,
) -> list[str]:
    return [str(row["status"]) for row in authority.rows(activated.dedupe_key)]


def _restart_authority(
    authority: DiagnosticDeliveryAuthority,
    spec: DiagnosticIsolationSpecV1,
) -> DiagnosticDeliveryAuthority:
    restarted = DiagnosticDeliveryAuthority.from_profile_record(
        authority.session,
        boot_epoch="boot",
        profile_root=authority.profile_root,
        spec=spec,
    )
    restarted.revalidate_after_restart(new_boot_epoch="boot-restarted")
    return restarted


def _write_activation_record(
    authority: DiagnosticDeliveryAuthority,
    record: dict[str, object],
) -> None:
    authority.activation_path.write_text(
        json.dumps(record, sort_keys=True),
        encoding="utf-8",
    )
    authority.activation_path.chmod(0o600)


@pytest.mark.asyncio
async def test_sealed_activation_delivery_records_exact_terminal_destination_and_body(
    tmp_path: Path,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    _activate(controller)

    result = await controller.deliver_activated(activated.session_id)

    assert result["status"] == "sent_audited"
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "provider_receipt",
        "sent_audited",
    ]
    assert bot.calls == 1
    assert bot.kwargs == [
        {
            "chat_id": -100,
            "message_thread_id": 71,
            "text": "diagnostic only",
            "connect_timeout": pytest.approx(2, abs=0.1),
            "pool_timeout": pytest.approx(2, abs=0.1),
            "write_timeout": pytest.approx(2, abs=0.1),
            "read_timeout": pytest.approx(2, abs=0.1),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    (
        SimpleNamespace(),
        SimpleNamespace(message_id=None),
        SimpleNamespace(message_id=True),
        SimpleNamespace(message_id=" "),
    ),
    ids=("missing", "none", "boolean", "blank"),
)
async def test_malformed_or_missing_message_id_is_unknown_and_never_resends(
    tmp_path: Path,
    receipt: object,
):
    bot = FakeBot(receipt=receipt)
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        bot=bot,
    )
    _activate(controller)

    first = await controller.deliver_activated(activated.session_id)
    second = await controller.deliver_activated(activated.session_id)

    assert first["status"] == "delivery_unknown"
    assert isinstance(second, DiagnosticUnknownNoSend)
    assert second.status == "delivery_unknown"
    assert second.text == UNKNOWN_TEXT
    assert second.reconciliation_available is False
    assert second.provider_authority is False
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert sum(row["status"] in {"delivery_unknown", "audit_pending", "sent_audited"} for row in authority.rows(activated.dedupe_key)) == 1
    assert all(row["status"] != "sent_audited" for row in authority.rows(activated.dedupe_key))
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_timeout_persists_one_unknown_terminal_and_reraises(tmp_path: Path):
    bot = FakeBot(error=TimeoutError("provider timeout"))
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        bot=bot,
    )
    _activate(controller)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await controller.deliver_activated(activated.session_id)

    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert bot.calls == 1
    assert sum(row["status"] == "delivery_unknown" for row in authority.rows(activated.dedupe_key)) == 1


@pytest.mark.asyncio
async def test_cancellation_persists_one_unknown_terminal_and_reraises(tmp_path: Path):
    started = asyncio.Event()
    release = asyncio.Event()
    bot = FakeBot(started=started, release=release)
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        bot=bot,
    )
    _activate(controller)

    delivery = asyncio.create_task(controller.deliver_activated(activated.session_id))
    await started.wait()
    delivery.cancel()

    with pytest.raises(asyncio.CancelledError):
        await delivery

    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert bot.calls == 1
    assert sum(row["status"] == "delivery_unknown" for row in authority.rows(activated.dedupe_key)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ("close", "expire", "stop"))
async def test_close_first_admission_race_has_zero_reservation_and_provider_calls(
    tmp_path: Path,
    terminal: str,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    host = _activate(controller)

    lifecycle = asyncio.create_task(getattr(host, terminal)())
    delivery = asyncio.create_task(controller.deliver_activated(activated.session_id))
    await lifecycle

    with pytest.raises(DiagnosticGatewayError, match="detached"):
        await delivery

    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ("close", "expire", "stop"))
async def test_provider_first_admission_race_terminalizes_before_detach(
    tmp_path: Path,
    terminal: str,
):
    started = asyncio.Event()
    release = asyncio.Event()
    bot = FakeBot(started=started, release=release)
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        bot=bot,
    )
    host = _activate(controller)

    delivery = asyncio.create_task(controller.deliver_activated(activated.session_id))
    await started.wait()
    lifecycle = asyncio.create_task(getattr(host, terminal)())
    await asyncio.sleep(0)
    assert not lifecycle.done()

    release.set()
    result = await delivery
    await lifecycle

    assert result["status"] == "sent_audited"
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "provider_receipt",
        "sent_audited",
    ]
    assert bot.calls == 1


@pytest.mark.parametrize("terminal", ("close", "expire", "stop"))
@pytest.mark.asyncio
async def test_detach_fence_failure_revokes_local_routes_and_provider(
    tmp_path: Path,
    monkeypatch,
    terminal: str,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )
    host = _activate(controller)
    assert host.owns_space(chat_id=-100, topic_id=71)
    initial_generation = host._generation
    detach_calls: list[tuple[int, str]] = []

    def fail_detach(*, generation: int, state: str):
        detach_calls.append((generation, state))
        raise OSError("durable fence unavailable")

    monkeypatch.setattr(authority, "detach", fail_detach)
    with pytest.raises(OSError, match="fence unavailable"):
        await getattr(host, terminal)()
    assert detach_calls == [(initial_generation, "detaching")]
    assert host._state == "recovery_required"
    assert host._generation == initial_generation + 1

    assert not host.owns_space(chat_id=-100, topic_id=71)
    with pytest.raises(DiagnosticGatewayError, match="detached"):
        await controller.deliver_activated(activated.session_id)
    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ("adapter", "bot", "object", "destination", "binding"),
)
async def test_transport_mutation_before_provider_is_terminal_unknown_and_provider_zero(
    tmp_path: Path,
    mutation: str,
):
    spec, authority, activated, bot, adapter, controller = _bootstrap(tmp_path)
    host = _activate(controller)
    transport = host._transport

    if mutation == "adapter":
        adapter._bot = FakeBot()
    elif mutation == "bot":
        transport._bot = FakeBot()
    elif mutation == "object":
        host._transport = object()
    elif mutation == "destination":
        transport._destination = (-200, 72)
    else:
        transport.binding_digest = "f" * 64

    with pytest.raises(Exception):
        await controller.deliver_activated(activated.session_id)

    expected_statuses = [] if mutation == "object" else [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert _statuses(authority, activated) == expected_statuses
    assert bot.calls == 0
    assert all(row["status"] != "sent_audited" for row in authority.rows(activated.dedupe_key))


def _reserve_authority(
    authority: DiagnosticDeliveryAuthority,
    candidate: DiagnosticDeliveryCandidate,
):
    with authority.delivery_admission(_DIAGNOSTIC_HOST_ADMISSION_TOKEN) as admission:
        return authority.reserve_and_verify(candidate, lock_token=admission)


@pytest.mark.asyncio
async def test_unknown_duplicate_mapping_is_exact_in_process_and_after_restart(
    tmp_path: Path,
):
    bot = FakeBot(receipt=SimpleNamespace(message_id=None))

    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path,
        bot=bot,
    )
    _activate(controller)

    first = await controller.deliver_activated(activated.session_id)
    same_process = await controller.deliver_activated(activated.session_id)
    restarted = _restart_authority(authority, spec)
    after_restart = _reserve_authority(
        restarted,
        _candidate(spec, activated),
    )

    assert first["status"] == "delivery_unknown"
    assert isinstance(same_process, DiagnosticUnknownNoSend)
    assert isinstance(after_restart, DiagnosticUnknownNoSend)
    assert same_process.text == after_restart.text == UNKNOWN_TEXT
    assert same_process.reconciliation_available is False
    assert after_restart.reconciliation_available is False
    assert same_process.provider_authority is False
    assert after_restart.provider_authority is False
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert _statuses(restarted, activated) == _statuses(authority, activated)
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_audit_pending_duplicate_mapping_and_reconcile_are_exact_after_restart(
    tmp_path: Path,
):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    _activate(controller)
    reservation = _reserve_authority(
        authority,
        _candidate(spec, activated),
    )
    assert isinstance(reservation, VerifiedDiagnosticReservation)
    receipt = {"ok": True, "message_id": "1"}
    with authority.delivery_admission(_DIAGNOSTIC_HOST_ADMISSION_TOKEN) as admission:
        authority.record_terminal(
            reservation,
            receipt=receipt,
            audited=False,
            lock_token=admission,
        )

    same_process = await controller.deliver_activated(activated.session_id)
    restarted = _restart_authority(authority, spec)
    after_restart = _reserve_authority(
        restarted,
        _candidate(spec, activated),
    )
    with restarted.delivery_admission(_DIAGNOSTIC_HOST_ADMISSION_TOKEN) as admission:
        restarted.record_terminal(
            reservation,
            receipt=receipt,
            audited=True,
            lock_token=admission,
        )
    reconciled = _reserve_authority(
        restarted,
        _candidate(spec, activated),
    )

    assert isinstance(same_process, DiagnosticAuditPendingNoSend)
    assert isinstance(after_restart, DiagnosticAuditPendingNoSend)
    assert same_process.status == after_restart.status == "audit_pending"
    assert same_process.text == after_restart.text == AUDIT_PENDING_TEXT
    assert same_process.reconciliation_available is True
    assert after_restart.reconciliation_available is True
    assert same_process.provider_authority is False
    assert after_restart.provider_authority is False
    assert isinstance(reconciled, DiagnosticDuplicateNoSend)
    assert reconciled.text == DUPLICATE_TEXT
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "provider_receipt",
        "audit_pending",
        "sent_audited",
    ]
    assert _statuses(restarted, activated) == _statuses(authority, activated)
    assert bot.calls == 0


@pytest.mark.asyncio
async def test_sent_audited_duplicate_mapping_is_exact_in_process_and_after_restart(
    tmp_path: Path,
):
    spec, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    _activate(controller)

    first = await controller.deliver_activated(activated.session_id)
    same_process = await controller.deliver_activated(activated.session_id)
    restarted = _restart_authority(authority, spec)
    after_restart = _reserve_authority(
        restarted,
        _candidate(spec, activated),
    )

    assert first["status"] == "sent_audited"
    assert isinstance(same_process, DiagnosticDuplicateNoSend)
    assert isinstance(after_restart, DiagnosticDuplicateNoSend)
    assert same_process.status == after_restart.status == "duplicate"
    assert same_process.text == after_restart.text == DUPLICATE_TEXT
    assert same_process.reconciliation_available is False
    assert after_restart.reconciliation_available is False
    assert same_process.provider_authority is False
    assert after_restart.provider_authority is False
    assert _statuses(authority, activated) == [
        "delivery_attempt_started",
        "provider_receipt",
        "sent_audited",
    ]
    assert _statuses(restarted, activated) == _statuses(authority, activated)
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_activation_record_replacement_after_activation_has_zero_provider_and_rows(
    tmp_path: Path,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    _activate(controller)
    replacement = authority.activation_path.with_name("activated-delivery-replacement.json")
    replacement.write_text(
        json.dumps(activated.to_persisted_record(), sort_keys=True),
        encoding="utf-8",
    )
    replacement.chmod(0o600)
    replacement.replace(authority.activation_path)

    with pytest.raises(Exception, match="diagnostic activation"):
        await controller.deliver_activated(activated.session_id)

    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("pin", "body", "destination"))
async def test_activation_pin_body_destination_mutation_after_activation_has_zero_provider_and_rows(
    tmp_path: Path,
    mutation: str,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    _activate(controller)
    record = json.loads(authority.activation_path.read_text(encoding="utf-8"))

    if mutation == "pin":
        record["registry_digest"] = "f" * 64
    elif mutation == "body":
        body = "tampered body"
        record["rendered_body"] = body
        record["rendered_body_digest"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    else:
        destination = ["-100", "72"]
        record["destination"] = destination
        record["destination_digest"] = hashlib.sha256(
            json.dumps(destination, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    _write_activation_record(authority, record)

    with pytest.raises(Exception, match="diagnostic activation"):
        await controller.deliver_activated(activated.session_id)

    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


@pytest.mark.parametrize("field", ("user_id", "chat_id", "topic_id", "generation"))
def test_role_route_rejects_every_wrong_identity_component(field: str):
    route = DiagnosticRoleRoute(7, -100, 59, "operator", 1)
    values = {
        "user_id": 7,
        "chat_id": -100,
        "topic_id": 59,
        "generation": 1,
    }
    wrong = {"user_id": 8, "chat_id": -101, "topic_id": 60, "generation": 2}
    values[field] = wrong[field]

    assert route.matches(user_id=7, chat_id=-100, topic_id=59, generation=1)
    assert not route.matches(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ("message", "callback"))
async def test_wrong_user_message_and_callback_ingress_have_zero_downstream(
    tmp_path: Path,
    ingress: str,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(tmp_path)
    host = _activate(controller)
    assert host is not None

    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter._diagnostic_control_service = controller.control
    adapter._diagnostic_isolation_host = host
    adapter._send_adaptive_operator_result = AsyncMock()
    wrong_user = 8

    if ingress == "message":
        message = SimpleNamespace(
            text="send",
            chat=SimpleNamespace(id=-100, type="supergroup"),
            message_thread_id=59,
            from_user=SimpleNamespace(id=wrong_user),
            reply_to_message=None,
        )
        adapter._get_nutrition_coaching = lambda: None
        await adapter._handle_text_message(
            SimpleNamespace(effective_message=message, update_id=1),
            None,
        )
        assert adapter._send_adaptive_operator_result.await_count == 1
    else:
        query_message = SimpleNamespace(
            chat_id=-100,
            chat=SimpleNamespace(id=-100, type="supergroup"),
            message_thread_id=59,
            message_id=1,
        )
        query = SimpleNamespace(
            data="diagnostic",
            message=query_message,
            from_user=SimpleNamespace(id=wrong_user),
            answer=AsyncMock(),
        )
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query),
            None,
        )
        query.answer.assert_awaited_once_with(text="격리 진단 제어 권한이 없습니다.")

    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0


def test_diagnostic_process_fence_blocks_lazy_generic_coordinator():
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter._diagnostic_isolation_fenced = True
    adapter._nutrition_coaching_config = object()
    adapter._nutrition_coaching = None
    assert adapter._get_nutrition_coaching() is None
    assert adapter._nutrition_coaching is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ("message", "callback"))
async def test_detached_diagnostic_spaces_never_fall_through_to_generic_ingress(
    tmp_path: Path,
    ingress: str,
):
    _spec_obj, authority, activated, bot, _adapter_obj, controller = _bootstrap(
        tmp_path
    )
    host = _activate(controller)
    await host.close()
    assert host.reserves_space(chat_id=-100, topic_id=71)
    assert not host.owns_space(chat_id=-100, topic_id=71)

    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter._diagnostic_control_service = controller.control
    adapter._diagnostic_isolation_host = host
    adapter._diagnostic_isolation_fenced = True
    adapter._send_adaptive_operator_result = AsyncMock()
    adapter._get_nutrition_coaching = lambda: pytest.fail(
        "detached diagnostic space reached generic coordinator"
    )
    if ingress == "message":
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100),
            message_thread_id=71,
            from_user=SimpleNamespace(id=7),
        )
        consumed = await adapter._reserve_adaptive_review_update(
            SimpleNamespace(),
            message,
        )
        assert consumed is True
        adapter._send_adaptive_operator_result.assert_awaited_once()
    else:
        query = SimpleNamespace(
            data="diagnostic",
            message=SimpleNamespace(
                chat_id=-100,
                chat=SimpleNamespace(type="supergroup"),
                message_thread_id=71,
            ),
            from_user=SimpleNamespace(id=7, first_name="owner"),
            answer=AsyncMock(),
        )
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query),
            None,
        )
        query.answer.assert_awaited_once_with(
            text="격리 진단 역할 권한이 없습니다."
        )

    assert authority.rows(activated.dedupe_key) == ()
    assert bot.calls == 0

def test_activated_coordinator_constructor_is_sealed_before_inputs():
    with pytest.raises(DiagnosticGatewayError, match="coordinator construction"):
        _ActivatedDiagnosticCoordinator(
            customer_runtime=None,
            authority=None,
            transport=None,
            activation_loader=None,
            activated=None,
            runtime_loader=None,
        )
    with pytest.raises(DiagnosticGatewayError, match="coordinator construction"):
        _ActivatedDiagnosticCoordinator(
            customer_runtime=None,
            authority=None,
            transport=None,
            activation_loader=None,
            activated=None,
            runtime_loader=None,
            _factory_token=object(),
        )


def test_candidate_provenance_requires_the_activated_child_instance():
    spec = _spec()
    activated = _activated(spec)
    candidate = DiagnosticDeliveryCandidate.from_activated(
        activated,
        factory_token=_DIAGNOSTIC_CANDIDATE_FACTORY_TOKEN,
        session_digest=spec.spec_digest,
    )
    coordinator = object.__new__(_ActivatedDiagnosticCoordinator)
    coordinator._provenance_token = object()
    coordinator._candidate_ids = set()
    with pytest.raises(DiagnosticGatewayError, match="provenance"):
        coordinator.require_candidate(candidate)

    object.__setattr__(
        candidate,
        "_diagnostic_coordinator_provenance",
        coordinator._provenance_token,
    )
    with pytest.raises(DiagnosticGatewayError, match="provenance"):
        coordinator.require_candidate(candidate)

    coordinator._candidate_ids.add(id(candidate))
    assert coordinator.require_candidate(candidate) is candidate
