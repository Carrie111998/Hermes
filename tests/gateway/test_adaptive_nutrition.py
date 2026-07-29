import json
import hashlib
import asyncio
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event as ThreadEvent
from types import SimpleNamespace
import sys
from pathlib import Path

_PROFILE_PACKAGE = Path("/home/cube/.hermes/profiles/physique-coach/workspace/checkin_cli")
if str(_PROFILE_PACKAGE) not in sys.path:
    sys.path.insert(0, str(_PROFILE_PACKAGE))
from dataclasses import make_dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from checkin_cli.adaptive_nutrition import (
    CustomerPolicy,
    canonical_json,
    CanonicalSequenceJournal,
    DailyObservation,
    Decision,
    MacroTarget,
    digest,
    initialize_adaptive_customer,
)
from checkin_cli.customer_admin import (
    CustomerDraft,
    activate_customer,
    approve_adaptive_registration_inputs,
    load_approved_adaptive_registration_inputs,
    register_customer,
    set_customer_ai_consent,
    reconcile_adaptive_nutrition_journals,
)
from checkin_cli.customer_coaching import AiProcessingConsent, CONSENT_VERSION
from checkin_cli.models import Event
from checkin_cli.store import CanonicalEventTransaction, EventStore
from gateway.platforms.nutrition_coaching import (
    AdaptiveNutritionCoordinator,
    AdaptiveCoachingFacts,
    _ADAPTIVE_SHADOW_FACTORY_TOKEN,
    load_committed_customer_registry,
    AdaptiveWorkflowError,
    NutritionCoachingCoordinator,
    TelegramCustomerTransport,
    AdaptiveOperatorService,
    AdaptiveOperatorCapability,
    adaptive_delivery_result_text,
)
from gateway.platforms.nutrition_coaching_config import (
    AdaptiveNutritionConfig,
    AdaptiveReviewOperator,
)
from gateway.platforms.telegram import TelegramAdapter
OWNER_TRIPLE = ("richard", "operator-chat", "59")


def rows(day, *, adherence=True, safety=False):
    result=[]
    for i in range(14):
        d=day-timedelta(days=13-i)
        result.append(DailyObservation(d, weight_kg=Decimal("80") if i<7 else Decimal("80.5"), adherence_ok=adherence, safety_held=safety and i==13))
    return result

def _canonical_event(index: int) -> Event:
    day = date(2026, 7, 1) + timedelta(days=index)
    return Event.model_validate({
        "event_id": f"checkin_{index:08d}",
        "event_type": "morning_checkin",
        "occurred_at_kst": f"{day.isoformat()}T08:00:00+09:00",
        "recorded_at_kst": f"{day.isoformat()}T08:00:00+09:00",
        "provenance": {
            "source_type": "telegram",
            "source_ref": "pilot:client_001:morning_checkin",
            "content_sha256": "0" * 64,
            "received_message_id": f"message-{index}",
        },
        "status": "accepted",
        "check_in": {
            "body_weight_kg": 80 if index < 7 else 79.7,
            "calories_kcal": 2300,
            "sleep_hours": 8,
            "sleep_quality_1to5": 4,
            "readiness_1to5": 4,
            "pain_summary": "없음",
            "training_plan": "계획대로 진행",
        },
    })


class _CanonicalProjectionEvent:
    def __init__(self, event: Event, index: int):
        self._event = event
        self._index = index
        self.status = event.status
        self.safety = event.safety

    def model_dump(self, **kwargs):
        value = self._event.model_dump(**kwargs)
        value["target"] = {
            "calories_kcal": 2300,
            "carbohydrate_g": 290,
            "protein_g": 150,
            "fat_g": 60,
        }
        value["actual"] = dict(value["target"])
        if self._index >= 14:
            value["trainer_session"] = {
                "session_done": True,
                "intensity_vs_plan": "as_planned",
            }
        return value
def _persisted_event(event):
    return getattr(event, "_event", event)


class _CanonicalSource:
    def __init__(self, events, persisted_path: Path):
        self.events = tuple(events)
        self._events = Path(persisted_path)

    def _read_events(self):
        return self.events

    def __iter__(self):
        return iter(self.events)
class _MutableRegistry:
    def __init__(self, registry_path: Path, customer: object):
        self._registry_path = registry_path
        self.owner = SimpleNamespace(key=OWNER_TRIPLE)
        self.customers = (customer,)

    def model_dump(self, **kwargs):
        return json.loads(self._registry_path.read_text(encoding="utf-8"))


def _production_fixture(tmp_path, monkeypatch):
    profile_root = tmp_path / "profile"
    registry_path = profile_root / "customers" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    owner = {
        "user_id": OWNER_TRIPLE[0],
        "chat_id": OWNER_TRIPLE[1],
        "topic_id": OWNER_TRIPLE[2],
    }
    registry_path.write_text(json.dumps({
        "version": 1,
        "owner": owner,
        "customers": [],
    }))
    register_customer(
        registry_path,
        CustomerDraft(
            customer_key="client_001",
            display_name="Client",
            user_id="2",
            chat_id="-100",
            topic_id="20",
            starts_on=date(2026, 7, 1),
            daily_time=dt_time(8, 0),
            weekly_weekday=2,
            monthly_day=1,
            calories_kcal=2300,
            protein_g=150,
            meals=("meal",),
            trainer_user_id="trainer-1",
            trainer_chat_id="-200",
            trainer_topic_id="30",
        ),
    )
    set_customer_ai_consent(
        registry_path,
        "client_001",
        AiProcessingConsent(
            granted=True,
            recorded_on=date(2026, 7, 1),
            notice_version=CONSENT_VERSION,
        ),
    )
    data_root = profile_root / "data" / "customers" / "client_001"
    data_root.mkdir(parents=True)
    checklist_path = tmp_path / "activation-checklist.json"
    checklist_path.write_text(json.dumps({
        "checklist": {
            "token_rotated": True,
            "missend_test_passed": True,
            "provider_terms_checked": {
                "checked": True,
                "version": CONSENT_VERSION,
            },
            "withdrawal_deletion_doc": True,
            "retention_backup_doc": True,
            "manual_fallback_doc": True,
        }
    }))
    activate_customer(
        profile_root,
        data_root,
        "client_001",
        checklist_path,
        kst_date=date(2026, 7, 1),
    )
    initialize_adaptive_customer(data_root)
    values = {
        "policy.json": (
            "policy",
            {
                "starts_on": "2026-07-01",
                "goal_mode": "fat_loss",
                "weekly_rate_min": "-0.5",
                "weekly_rate_max": "-0.25",
                "calorie_step": 100,
                "calorie_floor": 1800,
                "calorie_ceiling": 2600,
            },
        ),
        "meal-constraints.json": (
            "meal_constraints",
            {"meal_count": 1, "budget_tier": "standard", "cooking_access": "home"},
        ),
        "food-catalog.json": (
            "catalog",
            [{
                "food_id": "safe",
                "label": "safe",
                "calories": 2300,
                "carbs_g": 290,
                "protein_g": 150,
                "fat_g": 60,
            }],
        ),
    }
    for filename, (key, value) in values.items():
        path = data_root / "nutrition-plans" / filename
        path.write_text(json.dumps({
            "schema_version": "1.0",
            "version": "v1",
            "digest": digest(value),
            "approved": True,
            "approved_by": owner,
            "approved_at_kst": "2026-07-01T09:00:00+09:00",
            key: value,
        }))
        path.chmod(0o600)
    training_loads = ("high", "high", "low", "high", "medium", "low", "low")
    registration_inputs = {
        "version": "v1",
        "meal_count": 1,
        "budget_band": "standard",
        "cooking_access": "home",
        "preferences": ["simple"],
        "exclusions": [],
        "allergies": [],
        "training_schedule": [
            {
                "date": (date(2026, 7, 14) + timedelta(days=index)).isoformat(),
                "weekday": (date(2026, 7, 14) + timedelta(days=index)).weekday(),
                "time": "18:00",
                "load_category": training_loads[index % len(training_loads)],
            }
            for index in range(14)
        ],
    }
    approve_adaptive_registration_inputs(
        profile_root,
        "client_001",
        inputs=registration_inputs,
        approved_by=owner,
        approved_at_kst="2026-07-01T10:00:00+09:00",
    )
    customer = SimpleNamespace(
        data_root=data_root,
        spec=SimpleNamespace(
            customer_key="client_001",
            enabled=True,
            ai_processing_consent=SimpleNamespace(
                granted=True,
                recorded_on=date(2026, 7, 1),
                notice_version="privacy-v1",
            ),
            plan=SimpleNamespace(
                starts_on=date(2026, 7, 1),
                weeks=tuple(
                    SimpleNamespace(
                        calories_kcal=2300,
                        protein_g=150,
                        fat_g=60,
                    )
                    for _ in range(12)
                ),
            ),
            telegram=SimpleNamespace(user_id="2", chat_id="-100", topic_id="20"),
            trainer=SimpleNamespace(user_id="trainer-1", chat_id="-200", topic_id="30"),
        ),
    )
    authority = SimpleNamespace(
        owner=SimpleNamespace(key=OWNER_TRIPLE),
        registry=_MutableRegistry(registry_path, customer),
        _registry_path=profile_root / "customers" / "registry.json",
        refresh_live_registry=lambda: True,
        customer=lambda key: customer if key == "client_001" else None,
    )
    wizard_events = data_root / "wizard" / "events.jsonl"
    wizard_events.parent.mkdir(parents=True, exist_ok=True)
    canonical_source = _CanonicalSource(
        [
            _CanonicalProjectionEvent(_canonical_event(index), index)
            for index in range(21)
        ],
        wizard_events,
    )
    canonical_transaction = CanonicalEventTransaction(
        wizard_events,
        data_root / "nutrition-plans" / "canonical-sequence.jsonl",
    )
    coordinator = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key="client_001",
        starts_on=date(2026, 7, 1),
        event_path=data_root / "nutrition-plans" / "events.jsonl",
        profile_root=profile_root,
        registry_path=authority._registry_path,
        canonical_event_source=canonical_source,
        customer_runtime=customer,
        authority=authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    monkeypatch.setattr(
        "gateway.platforms.nutrition_coaching.load_verified_dual_coach_risk_policy",
        lambda _customer: SimpleNamespace(
            version="risk-v1",
            policy_digest="a" * 64,
            document_digest="b" * 64,
        ),
    )
    coordinator.store._canonical_transaction = canonical_transaction
    source_events = coordinator.canonical_event_source.events
    persisted_events = tuple(_persisted_event(event) for event in source_events)
    canonical_transaction.append_many(persisted_events)
    coordinator.store.canonical_events_path = wizard_events
    sequence = [
        dict(row) for row in coordinator.store.validate_canonical_prefix()
    ]
    coordinator._wizard_events_path = wizard_events
    intent = "source-day:test"
    coordinator.store.prepare_source_day(
        intent,
        customer_key="client_001",
        source_day="2026-07-01",
    )
    coordinator.store.commit_source_day(
        intent,
        customer_key="client_001",
        source_day="2026-07-01",
    )
    authority_digest = coordinator._authority_digest(customer.spec)
    authority_payload = {
        "customer_key": "client_001",
        "owner": {
            "user_id": OWNER_TRIPLE[0],
            "chat_id": OWNER_TRIPLE[1],
            "topic_id": OWNER_TRIPLE[2],
        },
        "authority_digest": authority_digest,
        "registry_digest": digest({"registry": "test"}),
        "activation_receipt_digest": digest({"activation_receipt_id": "test"}),
        "consent_digest": digest({
            "granted": True,
            "recorded_on": "2026-07-01",
            "notice_version": "privacy-v1",
        }),
    }
    coordinator.store.append_authority_mirror(
        intent_id="authority:test",
        authority_kind="nutrition",
        canonical_fact_id="authority:test",
        canonical_fact_digest=authority_digest,
        valid_from="2026-07-01T09:00:00+09:00",
        adaptive_sequence=len(sequence),
        state="prepared",
        **authority_payload,
    )
    coordinator.store.append_authority_mirror(
        intent_id="authority:test",
        authority_kind="nutrition",
        canonical_fact_id="authority:test",
        canonical_fact_digest=authority_digest,
        valid_from="2026-07-01T09:00:00+09:00",
        adaptive_sequence=len(sequence),
        state="committed",
        **authority_payload,
    )
    config_digest = json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text()
    )["config_digest"]
    coordinator.store.append_config_epoch(
        0,
        config_digest,
        ("client_001",),
        state="prepared",
        customer_states={"client_001": "pending"},
        approved_by=owner,
    )
    coordinator.store.append_config_epoch(
        0,
        config_digest,
        ("client_001",),
        state="committed",
        customer_states={"client_001": "committed"},
        approved_by=owner,
    )
    reconcile_adaptive_nutrition_journals(
        profile_root,
        "client_001",
        canonical_events=canonical_source,
        registry=authority.registry,
    )
    coordinator._test_journals = {
        "canonical_sequence": sequence,
        "source_day": coordinator.store.journal_rows("source_day"),
        "source_day_mappings": coordinator.store.source_day_rows(),
        "authority": coordinator.store.journal_rows("authority"),
        "config_epoch": coordinator.store.journal_rows("config_epoch"),
    }
    return coordinator, data_root
def _activated_production_fixture(
    tmp_path,
    monkeypatch,
    *,
    strict_capabilities=False,
):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    if strict_capabilities:
        coordinator._shadow_test_only = False
        proposal, _ = coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=_production_operator_capability(coordinator, "create"),
        )
        coordinator.approve_latest(
            proposal.digest,
            operator_id=_production_operator_capability(
                coordinator,
                "approve",
                proposal=proposal,
            ),
        )
        coordinator.activate_latest(
            proposal.digest,
            operator_id=_production_operator_capability(
                coordinator,
                "activate",
                proposal=proposal,
            ),
        )
    else:
        proposal, _ = coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )
        coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
        coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    coordinator.set_persisted_delivery(
        True,
        operator_id=(
            _production_operator_capability(
                coordinator,
                "delivery_enable",
                proposal=proposal,
            )
            if strict_capabilities
            else OWNER_TRIPLE
        ),
    )
    return coordinator, data_root, proposal
def _typed_capability(coordinator, action, *, proposal_digest=None, revision=None):
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0)
    return AdaptiveOperatorCapability(
        schema_version="1.0",
        capability_id="a" * 24,
        review_operator=("review-user", "review-chat", "59"),
        review_operator_version=1,
        canonical_owner=tuple(OWNER_TRIPLE),
        canonical_owner_version=coordinator._live_owner_version(),
        customer_key=coordinator.customer_key,
        action=action,
        proposal_digest=proposal_digest,
        revision=revision,
        config_digest="config",
        registry_digest="registry",
        consent_digest="consent",
        activation_digest="activation",
        issued_kst=(now - timedelta(minutes=1)).isoformat(),
        expires_kst=(now + timedelta(minutes=9)).isoformat(),
        nonce_digest=hashlib.sha256(("a" * 24).encode("ascii")).hexdigest(),
    )
def _adaptive_callback_service(coordinator, tmp_path, *, durable=False):
    from gateway.platforms.nutrition_coaching import IncomingAddress

    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 1)
    runtime = coordinator.customer_runtime
    proxy = SimpleNamespace(
        owner=SimpleNamespace(key=OWNER_TRIPLE),
        registry=SimpleNamespace(
            model_dump=lambda **_kwargs: json.loads(
                coordinator.registry_path.read_text(encoding="utf-8")
            )
        ),
        _by_key={"client_001": SimpleNamespace(customer=runtime)},
        adaptive_nutrition_coordinator=lambda _key: coordinator,
    )
    session_path = (
        coordinator.profile_root
        / "data"
        / "owner-actions"
        / "adaptive-operator-sessions.jsonl"
        if durable
        else tmp_path / "adaptive-operator-sessions.jsonl"
    )
    if durable:
        now_provider = lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0)
    else:
        now_provider = lambda: datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    service = AdaptiveOperatorService(
        proxy,
        review_operator=review,
        profile_root=coordinator.profile_root,
        session_path=session_path,
        now_provider=now_provider,
    )
    return service, IncomingAddress(*review.key)


def _production_operator_capability(
    coordinator,
    action,
    *,
    proposal=None,
):
    service, _address = _adaptive_callback_service(
        coordinator,
        coordinator.profile_root,
        durable=True,
    )
    kwargs = {
        "action": action,
        "customer_key": coordinator.customer_key,
        "originating_message_id": f"p2-p6:{action}:{getattr(proposal, 'revision', 'base')}",
        "originating_chat_id": "review-chat",
        "originating_topic_id": 59,
    }
    if proposal is not None:
        kwargs.update(
            {
                "proposal_digest": proposal.digest,
                "revision": proposal.revision,
                "source_digest": proposal.source_digest,
                "registration_digest": coordinator._registration_pin(
                    proposal,
                    required=True,
                ),
            }
        )
    callback = service.issue_session(**kwargs)
    claimed = service._claim_issued_callback(
        callback.split(":", 2)[1],
        action,
    )
    assert claimed is not None
    return service._capability(claimed, action=action)
def _rewrite_persisted_proposal(
    coordinator,
    proposal_digest,
    mutate,
    *,
    rewrite_lifecycle=False,
):
    rows = coordinator.store.read()
    target_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("event_type") in {"plan_proposed", "plan_edited"}
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("proposal_digest") == proposal_digest
    )
    target_payload = rows[target_index]["payload"]
    raw = json.loads(target_payload["proposal"])
    mutate(raw)
    customer_body = raw.get("customer_body")
    if not isinstance(customer_body, str) or not customer_body:
        raise AssertionError("tampered proposal must retain a customer body")
    customer_body_digest = coordinator._text_digest(customer_body)
    raw["customer_body_digest"] = customer_body_digest
    encoded = canonical_json(raw)
    new_digest = digest({
        key: value
        for key, value in raw.items()
        if key != "registration_digest"
    })
    old_body = target_payload.get("customer_body")
    old_body_digest = target_payload.get("customer_body_digest")

    def rewrite_value(value):
        if isinstance(value, dict):
            return {
                key: rewrite_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite_value(item) for item in value]
        if value == proposal_digest:
            return new_digest
        if value == old_body:
            return customer_body
        if value == old_body_digest:
            return customer_body_digest
        return value

    rewritten_rows = []
    for index, row in enumerate(rows):
        rewritten = dict(row)
        should_rewrite = index == target_index or rewrite_lifecycle
        if should_rewrite:
            payload = row.get("payload")
            if isinstance(payload, dict):
                rewritten["payload"] = rewrite_value(payload)
            dedupe_key = rewritten.get("dedupe_key")
            if isinstance(dedupe_key, str):
                rewritten["dedupe_key"] = dedupe_key.replace(
                    proposal_digest,
                    new_digest,
                )
        if index == target_index:
            payload = dict(rewritten["payload"])
            payload.update({
                "proposal": encoded,
                "proposal_digest": new_digest,
                "customer_body": customer_body,
                "customer_body_digest": customer_body_digest,
            })
            rewritten["payload"] = payload
        if "row_digest" in rewritten:
            rewritten["row_digest"] = digest({
                key: value
                for key, value in rewritten.items()
                if key != "row_digest"
            })
        rewritten["event_id"] = digest({
            key: value
            for key, value in rewritten.items()
            if key != "event_id"
        })
        rewritten_rows.append(rewritten)
    coordinator.store.path.write_text(
        "".join(canonical_json(row) + "\n" for row in rewritten_rows),
        encoding="utf-8",
    )
    coordinator.store.path.chmod(0o600)
    return new_digest
def _refresh_reconciled_test_sequence(coordinator):
    journals = coordinator._test_journals
    persisted_events = tuple(
        _persisted_event(event)
        for event in coordinator.canonical_event_source.events
    )
    records = tuple(
        event.model_dump(mode="json", exclude_none=True)
        for event in persisted_events
    )
    event_lines = tuple(
        (event.model_dump_json(exclude_none=True) + "\n").encode("utf-8")
        for event in persisted_events
    )
    sequence_rows = [
        {
            "schema_version": "canonical_sequence_v1",
            "sequence": index,
            "event_id": record["event_id"],
            "event_digest": hashlib.sha256(event_line).hexdigest(),
        }
        for index, (record, event_line) in enumerate(
            zip(records, event_lines),
            start=1,
        )
    ]
    for row in sequence_rows:
        row["row_digest"] = hashlib.sha256(
            canonical_json(row).encode("utf-8")
        ).hexdigest()
    journals["canonical_sequence"] = sequence_rows
    sequence_path = coordinator.store.canonical_sequence_path
    sequence_path.write_text(
        "".join(canonical_json(row) + "\n" for row in sequence_rows),
        encoding="utf-8",
    )
    sequence_path.chmod(0o600)
    canonical_path = coordinator.store.canonical_events_path
    canonical_path.write_bytes(b"".join(event_lines))
    canonical_path.chmod(0o600)
    wizard_path = coordinator._wizard_events_path
    wizard_path.write_bytes(canonical_path.read_bytes())
    wizard_path.chmod(0o600)
    mappings = journals["source_day_mappings"]
    refreshed_mappings = []
    for event, row in zip(persisted_events, mappings):
        record = event.model_dump(mode="json", exclude_none=True)
        body = {
            "schema_version": "1.0",
            "mapping_id": digest({
                "root_event_id": record["event_id"],
                "customer_key": "client_001",
                "mapped_flow": "morning",
                "observation_kst_day": str(record["occurred_at_kst"])[:10],
                "session_id": str(record["provenance"]["source_ref"]),
                "writer_epoch": row.get("writer_epoch", 0),
                "root_preimage_digest": digest(record),
            }),
            "root_event_id": record["event_id"],
            "customer_key": "client_001",
            "mapped_flow": "morning",
            "observation_kst_day": str(record["occurred_at_kst"])[:10],
            "session_id": str(record["provenance"]["source_ref"]),
            "writer_epoch": row.get("writer_epoch", 0),
            "root_preimage_digest": digest(record),
        }
        refreshed_mappings.append({**body, "row_digest": digest(body)})
    journals["source_day_mappings"] = refreshed_mappings
    coordinator.store.source_day_path.write_text(
        "".join(canonical_json(row) + "\n" for row in refreshed_mappings),
        encoding="utf-8",
    )
    coordinator.store.source_day_path.chmod(0o600)
def _persist_test_journal(coordinator, key, rows):
    paths = {
        "canonical_sequence": coordinator.store.canonical_sequence_path,
        "source_day": coordinator.store.source_intent_path,
        "source_day_mappings": coordinator.store.source_day_path,
        "authority": coordinator.store.authority_path,
        "config_epoch": coordinator.store.config_epoch_path,
    }
    persisted = []
    for row in rows:
        value = dict(row)
        if "row_digest" in value:
            value["row_digest"] = digest({
                field: item for field, item in value.items() if field != "row_digest"
            })
        persisted.append(value)
    paths[key].write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in persisted)
    )
    paths[key].chmod(0o600)
def test_empty_or_mismatched_journals_block_production(tmp_path, monkeypatch):
    coordinator, _ = _production_fixture(tmp_path, monkeypatch)
    coordinator.store.canonical_sequence_path.write_text("")
    with pytest.raises(AdaptiveWorkflowError, match="reconciliation failed"):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

    coordinator, _ = _production_fixture(tmp_path / "mismatch", monkeypatch)
    sequence = [dict(row) for row in coordinator._test_journals["canonical_sequence"]]
    sequence[0]["event_id"] = "missing-event"
    _persist_test_journal(coordinator, "canonical_sequence", sequence)
    with pytest.raises(AdaptiveWorkflowError, match="reconciliation failed"):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

    coordinator, _ = _production_fixture(tmp_path / "fanout", monkeypatch)
    incomplete = [
        dict(row) for row in coordinator._test_journals["config_epoch"]
    ]
    incomplete[-1].pop("customer_state")
    _persist_test_journal(coordinator, "config_epoch", incomplete)
    with pytest.raises(AdaptiveWorkflowError, match="reconciliation failed"):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

@pytest.mark.parametrize(
    "case",
    ("duplicate_customer", "extra_customer", "omitted_enabled_customer", "missing_digest", "mismatched_digest"),
)
def test_config_epoch_requires_exact_live_fanout_and_digest(
    tmp_path, monkeypatch, case
):
    coordinator, data_root = _production_fixture(tmp_path / case, monkeypatch)
    journals = json.loads(json.dumps(coordinator._test_journals))
    epoch = journals["config_epoch"][-1]
    if case == "duplicate_customer":
        epoch["customer_keys"].append("client_001")
    elif case == "extra_customer":
        epoch["customer_keys"].append("client_002")
        epoch["customer_state"]["client_002"] = "committed"
    elif case == "omitted_enabled_customer":
        second = SimpleNamespace(
            spec=SimpleNamespace(customer_key="client_002", enabled=True)
        )
        coordinator.authority.registry.customers = (
            *coordinator.authority.registry.customers,
            second,
        )
    elif case == "missing_digest":
        del epoch["config_digest"]
    else:
        epoch["config_digest"] = "f" * 64
    _persist_test_journal(coordinator, "config_epoch", journals["config_epoch"])
    with pytest.raises(
        AdaptiveWorkflowError,
        match="reconciliation failed|config epoch",
    ):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

@pytest.mark.parametrize(
    "case",
    ("duplicate_root", "duplicate_mapping_id", "missing", "unmapped"),
)
def test_source_day_mapping_identity_is_one_to_one(
    tmp_path, monkeypatch, case
):
    coordinator, _ = _production_fixture(tmp_path / case, monkeypatch)
    journals = json.loads(json.dumps(coordinator._test_journals))
    mappings = journals["source_day_mappings"]
    if case == "duplicate_root":
        duplicate = dict(mappings[0])
        duplicate["mapping_id"] = digest({"duplicate": mappings[0]["root_event_id"]})
        mappings.append(duplicate)
    elif case == "duplicate_mapping_id":
        mappings[1]["mapping_id"] = mappings[0]["mapping_id"]
    elif case == "missing":
        mappings.pop()
    else:
        mappings[-1]["root_event_id"] = "unmapped-event"
    _persist_test_journal(coordinator, "source_day_mappings", mappings)
    if case == "missing":
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )
        assert len(coordinator.store.source_day_rows()) == len(
            coordinator._test_journals["canonical_sequence"]
        )
        return
    with pytest.raises(
        AdaptiveWorkflowError,
        match="reconciliation failed|source-day",
    ):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )


def test_production_proposal_uses_canonical_events_and_pins_artifacts(tmp_path, monkeypatch):
    coordinator, _ = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(date(2026, 7, 14), operator_id=OWNER_TRIPLE)
    assert proposal.snapshot.current_samples == 7
    assert proposal.snapshot.canonical_projection is True
    assert proposal.snapshot.adherence_complete_days == 7
    assert proposal.snapshot.adherent_days == 7
    assert proposal.snapshot.trainer_loads == tuple(
        (date(2026, 7, 15 + index), "medium") for index in range(7)
    )
    assert proposal.weekly_carb_cycle is not None
    cycle_days = dict(proposal.weekly_carb_cycle.days)
    assert tuple(cycle_days) == tuple(
        date(2026, 7, 14) + timedelta(days=index)
        for index in range(7)
    )
    assert cycle_days[date(2026, 7, 14)] == "high"
    assert cycle_days[date(2026, 7, 15)] == "medium"
    assert proposal.meal_plan is not None
    assert proposal.meal_plan.exact is True
    assert proposal.meal_plan.digest == digest(proposal.meal_plan)
    assert proposal.customer_body_digest == coordinator._text_digest(proposal.customer_body)
    assert all((
        proposal.source_digest,
        proposal.policy_digest,
        proposal.meal_constraints_digest,
        proposal.catalog_digest,
    ))
    persisted_payload = coordinator.store.read()[-1]["payload"]
    assert coordinator._decode_proposal(
        persisted_payload["proposal"]
    ).digest == persisted_payload["proposal_digest"]
    with pytest.raises(AdaptiveWorkflowError, match="shadow/test-only"):
        coordinator.create_proposal(
            topic_id=59,
            observations=rows(date(2026, 7, 14)),
            evaluation_day=date(2026, 7, 14),
            policy=policy(date(2026, 7, 1)),
            current_target=MacroTarget(2300, 290, 150, 60),
            protein_g=150,
            fat_g=60,
        )

def test_untyped_approved_schedule_fails_closed(tmp_path, monkeypatch):
    coordinator, _ = _production_fixture(tmp_path, monkeypatch)
    start = date(2026, 7, 14)
    monkeypatch.setattr(
        "gateway.platforms.nutrition_coaching.load_approved_adaptive_registration_inputs",
        lambda *_args: SimpleNamespace(
            customer_key="client_001",
            approved=True,
            training_schedule=[
                {
                    "date": (start + timedelta(days=index)).isoformat(),
                    "load_category": "medium",
                }
                for index in range(6)
            ],
        ),
    )
    with pytest.raises(AdaptiveWorkflowError, match="registration inputs are unavailable"):
        coordinator.create_production_proposal(
            start,
            operator_id=OWNER_TRIPLE,
        )


def test_production_journal_reload_uses_persisted_rows(tmp_path, monkeypatch):
    coordinator, _data_root = _production_fixture(tmp_path, monkeypatch)
    reloaded = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key=coordinator.customer_key,
        starts_on=coordinator.starts_on,
        event_path=coordinator.store.path,
        profile_root=coordinator.profile_root,
        registry_path=coordinator.registry_path,
        canonical_event_source=coordinator.canonical_event_source,
        customer_runtime=coordinator.customer_runtime,
        authority=coordinator.authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    proposal, _ = reloaded.create_production_proposal(
        date(2026, 7, 14),
        operator_id=OWNER_TRIPLE,
    )
    assert proposal.source_digest
    config_rows = reloaded._read_persisted_journal_rows(
        reloaded.store.config_epoch_path,
        "config_epoch",
    )
    assert config_rows[-1]["state"] == "committed"
def test_reload_rejects_hash_consistent_persisted_explanation_and_body(
    tmp_path, monkeypatch
):
    coordinator, _data_root = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(
        date(2026, 7, 14),
        operator_id=OWNER_TRIPLE,
    )
    persisted = coordinator.store.read()[-1]
    assert coordinator._decode_proposal(
        persisted["payload"]["proposal"]
    ).digest == proposal.digest

    tampered_digest = _rewrite_persisted_proposal(
        coordinator,
        proposal.digest,
        lambda raw: raw.update({
            "explanation": "untrusted persisted explanation",
            "customer_body": "untrusted persisted customer body",
        }),
    )
    reloaded = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key=coordinator.customer_key,
        starts_on=coordinator.starts_on,
        event_path=coordinator.store.path,
        profile_root=coordinator.profile_root,
        registry_path=coordinator.registry_path,
        canonical_event_source=coordinator.canonical_event_source,
        customer_runtime=coordinator.customer_runtime,
        authority=coordinator.authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    with pytest.raises(
        AdaptiveWorkflowError,
        match="adaptive proposal explanation is invalid",
    ):
        reloaded.approve_latest(
            tampered_digest,
            operator_id=OWNER_TRIPLE,
        )


def test_reload_delivery_rejects_hash_consistent_noncanonical_customer_body(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path,
        monkeypatch,
    )

    tampered_digest = _rewrite_persisted_proposal(
        coordinator,
        proposal.digest,
        lambda raw: raw.update({
            "customer_body": f'{raw["customer_body"]}\nmalicious persisted suffix',
        }),
        rewrite_lifecycle=True,
    )
    reloaded = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key=coordinator.customer_key,
        starts_on=coordinator.starts_on,
        event_path=coordinator.store.path,
        profile_root=coordinator.profile_root,
        registry_path=coordinator.registry_path,
        canonical_event_source=coordinator.canonical_event_source,
        customer_runtime=coordinator.customer_runtime,
        authority=coordinator.authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append((customer_key, reservation_id))
            return {"ok": True, "message_id": "must-not-send"}

    reloaded.set_customer_transport(Transport())
    with pytest.raises(
        AdaptiveWorkflowError,
        match="adaptive proposal body is not canonical",
    ):
        asyncio.run(
            reloaded.deliver_latest_once(
                tampered_digest,
                operator_id=OWNER_TRIPLE,
                chat_id="-100",
            )
        )
    assert calls == []
def test_live_feature_config_digest_is_verified(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
    epoch = json.loads(epoch_path.read_text())
    epoch["delivery"] = True
    epoch_path.write_text(json.dumps(epoch))
    with pytest.raises(
        AdaptiveWorkflowError,
        match="reconciliation failed|feature config digest mismatch",
    ):
        coordinator.create_production_proposal(
            date(2026, 7, 14), operator_id=OWNER_TRIPLE
        )
def test_d_plus_transport_gate_revalidates_feature_config_digest(tmp_path, monkeypatch):
    data_root = tmp_path / "customer"
    data_root.mkdir()
    initialize_adaptive_customer(data_root)
    artifacts = SimpleNamespace(
        policy=SimpleNamespace(
            starts_on=date(2026, 7, 1),
            extended_through=date(2026, 9, 30),
        )
    )
    monkeypatch.setattr(
        "gateway.platforms.nutrition_coaching.load_approved_adaptive_artifacts",
        lambda _root: artifacts,
    )
    customer = SimpleNamespace(
        data_root=data_root,
        spec=SimpleNamespace(plan=SimpleNamespace(starts_on=date(2026, 7, 1))),
    )
    coordinator = object.__new__(NutritionCoachingCoordinator)
    coordinator._kst_date_provider = lambda: date(2026, 8, 1)
    coordinator._delivery_enabled = True
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
    epoch = json.loads(epoch_path.read_text())
    epoch["activation"] = True
    epoch["delivery"] = True
    epoch["config_digest"] = AdaptiveNutritionCoordinator._feature_config_digest(epoch)
    epoch_path.write_text(json.dumps(epoch))
    assert coordinator._adaptive_plan_window_allows(customer) is True
    epoch["delivery"] = False
    epoch_path.write_text(json.dumps(epoch))
    assert coordinator._adaptive_plan_window_allows(customer) is False
@pytest.mark.parametrize(
    ("offset", "extended_through", "expected"),
    (
        (0, None, True),
        (27, None, True),
        (28, None, False),
        (28, date(2026, 9, 22), True),
        (83, date(2026, 9, 22), True),
        (84, date(2026, 9, 23), False),
    ),
)
def test_real_adaptive_transport_window_covers_baseline_and_extension(
    tmp_path, monkeypatch, offset, extended_through, expected
):
    data_root = tmp_path / f"customer-{offset}-{extended_through}"
    data_root.mkdir()
    initialize_adaptive_customer(data_root)
    starts_on = date(2026, 7, 1)
    monkeypatch.setattr(
        "gateway.platforms.nutrition_coaching.load_approved_adaptive_artifacts",
        lambda _root: SimpleNamespace(
            policy=SimpleNamespace(
                starts_on=starts_on,
                extended_through=extended_through,
            )
        ),
    )
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
    epoch = json.loads(epoch_path.read_text())
    epoch["activation"] = True
    epoch["delivery"] = True
    epoch["config_digest"] = AdaptiveNutritionCoordinator._feature_config_digest(epoch)
    epoch_path.write_text(json.dumps(epoch))
    customer = SimpleNamespace(
        data_root=data_root,
        spec=SimpleNamespace(plan=SimpleNamespace(starts_on=starts_on)),
    )
    coordinator = object.__new__(NutritionCoachingCoordinator)
    coordinator._kst_date_provider = lambda: starts_on + timedelta(days=offset)
    coordinator._delivery_enabled = True
    assert coordinator._adaptive_plan_window_allows(customer) is expected

def test_owner_can_persist_and_revoke_delivery_capability(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    enabled = coordinator.set_persisted_delivery(
        True,
        operator_id=OWNER_TRIPLE,
    )
    assert enabled["delivery"] is True
    reloaded = json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text()
    )
    assert reloaded["delivery"] is True
    assert reloaded["config_digest"] == coordinator._feature_config_digest(reloaded)
    committed = [
        row for row in coordinator.store.journal_rows("config_epoch")
        if row["state"] == "committed" and row["epoch"] == reloaded["epoch"]
    ][-1]
    assert committed["approved_by"] == {
        "user_id": OWNER_TRIPLE[0],
        "chat_id": OWNER_TRIPLE[1],
        "topic_id": OWNER_TRIPLE[2],
    }

    disabled = coordinator.set_persisted_delivery(
        False,
        operator_id=OWNER_TRIPLE,
    )
    assert disabled["delivery"] is False
    assert coordinator.set_persisted_delivery(
        False,
        operator_id=OWNER_TRIPLE,
    )["delivery"] is False


def test_delivery_enable_does_not_rebind_after_document_digest_rotation(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    coordinator.set_persisted_delivery(True, operator_id=OWNER_TRIPLE)
    before_epoch = json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text()
    )
    before_rows = coordinator.store.journal_rows("config_epoch")
    original_evidence = coordinator._risk_policy_evidence
    rotated_evidence = dict(original_evidence())
    rotated_evidence["risk_policy_document_digest"] = "f" * 64
    monkeypatch.setattr(coordinator, "_risk_policy_evidence", lambda: rotated_evidence)

    with pytest.raises(AdaptiveWorkflowError, match="explicit owner reapproval"):
        coordinator.set_persisted_delivery(True, operator_id=OWNER_TRIPLE)

    assert json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text()
    ) == before_epoch
    assert coordinator.store.journal_rows("config_epoch") == before_rows


def test_production_lifecycle_rejects_source_mutation_and_wrong_destination(
    tmp_path, monkeypatch
):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(date(2026, 7, 14), operator_id=OWNER_TRIPLE)
    coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    source = coordinator.canonical_event_source
    source.events = source.events + (_canonical_event(14),)
    with pytest.raises(AdaptiveWorkflowError, match="stale"):
        coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    source.events = source.events[:-1]
    coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    coordinator.set_persisted_delivery(
        True,
        operator_id=OWNER_TRIPLE,
    )

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            return {"ok": True, "message_id": "message-1"}

    coordinator.set_customer_transport(Transport())
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                operator_id="not-richard",
                chat_id="-100",
            )
        )
    with pytest.raises(AdaptiveWorkflowError, match="destination"):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-wrong",
                operator_id=OWNER_TRIPLE,
            )
        )
    delivered = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            operator_id=OWNER_TRIPLE,
            chat_id="-100",
        )
    )
    assert delivered["event_type"] == "sent_audited"
@pytest.mark.parametrize("action", ("approve", "activate", "deliver"))
@pytest.mark.parametrize("revocation", ("consent", "safety", "owner", "destination"))
def test_production_revocation_matrix_fails_closed(
    tmp_path, monkeypatch, action, revocation
):
    if action == "deliver":
        coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
        calls = []

        class Transport:
            async def send_adaptive_customer(
                self, customer_key, destination, *, reservation_id
            ):
                calls.append((customer_key, reservation_id))
                return {"ok": True, "message_id": "revocation-matrix"}

        coordinator.set_customer_transport(Transport())
    else:
        coordinator, _data_root = _production_fixture(tmp_path, monkeypatch)
        proposal, _ = coordinator.create_production_proposal(
            date(2026, 7, 14), operator_id=OWNER_TRIPLE
        )
        if action == "activate":
            coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)

    customer = coordinator.customer_runtime
    if revocation == "consent":
        customer.spec.ai_processing_consent.granted = False
    elif revocation == "owner":
        coordinator.authority.owner = SimpleNamespace(
            key=("revoked-owner", "-100", "59")
        )
    elif revocation == "destination":
        customer.spec.telegram = SimpleNamespace(
            user_id="2", chat_id="-999", topic_id="20"
        )
        registry_path = coordinator.profile_root / "customers" / "registry.json"
        registry_document = json.loads(registry_path.read_text())
        registry_document["customers"][0]["telegram"]["chat_id"] = "-999"
        registry_path.write_text(json.dumps(registry_document))
    else:
        source = coordinator.canonical_event_source
        event_index = len(source.events) - 1
        raw = getattr(source.events[event_index], "_event").model_dump(mode="json")
        raw["status"] = "unsafe"
        raw["safety"] = {
            "level": "stop_and_escalate",
            "signals": ["pain"],
            "coaching_held": True,
            "reasons": [
                {
                    "class": "pain",
                    "source_flow": "customer_checkin",
                    "matched_field": "free_text",
                    "excerpt": "pain",
                    "rule_id": "S2",
                }
            ],
        }
        source.events = source.events[:-1] + (
            _CanonicalProjectionEvent(Event.model_validate(raw), event_index),
        )
        _refresh_reconciled_test_sequence(coordinator)

    with pytest.raises(AdaptiveWorkflowError):
        if action == "approve":
            coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
        elif action == "activate":
            coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
        else:
            asyncio.run(
                coordinator.deliver_latest_once(
                    proposal.digest,
                    operator_id=OWNER_TRIPLE,
                    chat_id="-100",
                )
            )
    if action == "deliver":
        assert calls == []

def test_production_lifecycle_requires_exact_configured_owner(tmp_path, monkeypatch):
    coordinator, _ = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(date(2026, 7, 14), operator_id=OWNER_TRIPLE)
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        coordinator.approve_latest(proposal.digest, operator_id="not-richard")
    approved = coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    assert approved["event_type"] == "plan_approved"
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        coordinator.activate_latest(proposal.digest, operator_id="not-richard")
    activated = coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    assert activated["event_type"] == "adaptive_plan_activated"

def policy(start):
    return CustomerPolicy(start, "fat_loss", Decimal("-0.5"), Decimal("-0.25"), 100, 1800, 2600)


def coordinator(tmp_path):
    return AdaptiveNutritionCoordinator(customer_key="client_001", starts_on=date(2026,7,1), event_path=tmp_path/"events.jsonl")
def test_public_shadow_constructor_flag_cannot_enable_raw_runtime_paths(tmp_path):
    events_path = tmp_path / "runtime" / "nutrition-plans" / "events.jsonl"
    with pytest.raises(TypeError):
        AdaptiveNutritionCoordinator(
            customer_key="client_001",
            event_path=events_path,
            customer_runtime=SimpleNamespace(data_root=tmp_path / "runtime"),
            shadow_test_only=True,
        )
    assert not events_path.exists()


def test_forged_runtime_paths_fail_before_store_or_provider_mutation(tmp_path):
    events_path = tmp_path / "runtime" / "nutrition-plans" / "events.jsonl"
    provider_calls = []

    class Transport:
        async def send_customer(self, *_args, **_kwargs):
            provider_calls.append(True)
            return {"ok": True}

    with pytest.raises(
        AdaptiveWorkflowError,
        match="registered customer runtime",
    ):
        AdaptiveNutritionCoordinator(
            customer_key="client_001",
            event_path=events_path,
            customer_runtime=SimpleNamespace(data_root=tmp_path / "runtime"),
            canonical_event_source=SimpleNamespace(_read_events=lambda: ()),
            authority=SimpleNamespace(),
            customer_transport=Transport(),
        )
    assert not events_path.exists()
    assert provider_calls == []


def test_shadow_factory_requires_the_exact_private_token(tmp_path):
    events_path = tmp_path / "events.jsonl"
    with pytest.raises(AdaptiveWorkflowError, match="sealed"):
        AdaptiveNutritionCoordinator._for_shadow_test(
            customer_key="client_001",
            event_path=events_path,
            _shadow_factory_token=object(),
        )
    assert not events_path.exists()



def test_topic_59_only(tmp_path):
    c=coordinator(tmp_path)
    with pytest.raises(AdaptiveWorkflowError, match="topic 59"):
        c.create_proposal(topic_id=4, observations=rows(date(2026,7,14)), evaluation_day=date(2026,7,14), policy=policy(date(2026,7,1)), current_target=MacroTarget(2300,290,150,60), protein_g=150, fat_g=60)
    with pytest.raises(AdaptiveWorkflowError, match="topic 59"):
        c.create_proposal(topic_id="59", observations=rows(date(2026,7,14)), evaluation_day=date(2026,7,14), policy=policy(date(2026,7,1)), current_target=MacroTarget(2300,290,150,60), protein_g=150, fat_g=60)
    with pytest.raises(AdaptiveWorkflowError, match="topic 59"):
        c.create_proposal(topic_id=" 59 ", observations=rows(date(2026,7,14)), evaluation_day=date(2026,7,14), policy=policy(date(2026,7,1)), current_target=MacroTarget(2300,290,150,60), protein_g=150, fat_g=60)


def test_missing_goal_prompts_and_never_sends(tmp_path):
    c=coordinator(tmp_path)
    p,_=c.create_proposal(topic_id=59, observations=rows(date(2026,7,14)), evaluation_day=date(2026,7,14), policy=CustomerPolicy(date(2026,7,1),"fat_loss"), current_target=MacroTarget(2300,290,150,60), protein_g=150, fat_g=60)
    assert p.decision is Decision.OBSERVE
    operator_card = c.render_proposal(p, topic_id=59)
    assert "검토 필요" in operator_card
    assert "고객 목표 정보가 없어 목표 범위를 확정하지 못했습니다." in operator_card
    assert "customer_goal_input_required" not in operator_card
    assert not hasattr(c, "send")


def test_revision_requires_reapproval_and_exact_digest(tmp_path):
    c=coordinator(tmp_path)
    p,_=c.create_proposal(topic_id=59, observations=rows(date(2026,7,14)), evaluation_day=date(2026,7,14), policy=policy(date(2026,7,1)), current_target=MacroTarget(2304,291,150,60), protein_g=150, fat_g=60)
    revised=c.revise_note(p,topic_id=59,note="수면 이행을 함께 확인")
    assert revised.parent_digest==p.digest and revised.digest!=p.digest
    with pytest.raises(ValueError,match="stale"):
        c.approve(revised,topic_id=59,operator_id="richard",expected_digest=p.digest)
    with pytest.raises(ValueError, match="stale"):
        c.approve(p, topic_id=59, operator_id="richard", expected_digest=p.digest)
    receipt=c.approve(revised,topic_id=59,operator_id="richard",expected_digest=revised.digest)
    assert receipt["payload"]["topic_id"]==59


def test_low_adherence_and_safety_fail_closed(tmp_path):
    c=coordinator(tmp_path); day=date(2026,7,14)
    low,_=c.create_proposal(topic_id=59,observations=rows(day,adherence=False),evaluation_day=day,policy=policy(date(2026,7,1)),current_target=MacroTarget(2300,290,150,60),protein_g=150,fat_g=60)
    held,_=c.create_proposal(topic_id=59,observations=rows(day,safety=True),evaluation_day=day,policy=policy(date(2026,7,1)),current_target=MacroTarget(2300,290,150,60),protein_g=150,fat_g=60)
    assert low.decision is Decision.MAINTAIN
    assert held.decision is Decision.HUMAN_REVIEW and held.target is None
def test_safety_callback_cannot_approve_or_activate(tmp_path):
    c = coordinator(tmp_path)
    day = date(2026, 7, 14)
    proposal, _ = c.create_proposal(
        topic_id=59,
        observations=rows(day, safety=True),
        evaluation_day=day,
        policy=policy(date(2026, 7, 1)),
        current_target=MacroTarget(2304, 291, 150, 60),
        protein_g=150,
        fat_g=60,
    )
    approve = c.issue_callback(proposal, action="approve")
    with pytest.raises(AdaptiveWorkflowError, match="human review|safety"):
        c.handle_callback_token(approve, topic_id=59, operator_id="richard")
    assert not any(
        row["event_type"] == "plan_approved" for row in c.store.read()
    )


def test_approved_delivery_calls_strict_sender_once_and_audits(tmp_path):
    c = coordinator(tmp_path)
    day = date(2026, 7, 14)
    proposal, _ = c.create_proposal(
        topic_id=59,
        observations=rows(day),
        evaluation_day=day,
        policy=policy(date(2026, 7, 1)),
        current_target=MacroTarget(2304, 291, 150, 60),
        protein_g=150,
        fat_g=60,
    )
    c.approve(
        proposal,
        topic_id=59,
        operator_id="richard",
        expected_digest=proposal.digest,
    )
    calls = []

    def sender(chat_id, topic_id, text):
        calls.append((chat_id, topic_id, text))
        return {"ok": True, "message_id": 77}

    delivered = c.deliver_approved_once(
        proposal,
        topic_id=59,
        chat_id="-1001",
        operator_id="richard",
        strict_sender=sender,
    )
    assert delivered["event_type"] == "sent_audited"
    assert len(calls) == 1 and calls[0][1] == 59
    assert any(row["event_type"] == "sent_audited" for row in c.store.read())
    with pytest.raises(AdaptiveWorkflowError, match="already attempted"):
        c.deliver_approved_once(
            proposal,
            topic_id=59,
            chat_id="-1001",
            operator_id="richard",
            strict_sender=sender,
        )
    assert len(calls) == 1


def test_unknown_delivery_is_never_retried(tmp_path):
    c = coordinator(tmp_path)
    day = date(2026, 7, 14)
    proposal, _ = c.create_proposal(
        topic_id=59,
        observations=rows(day),
        evaluation_day=day,
        policy=policy(date(2026, 7, 1)),
        current_target=MacroTarget(2304, 291, 150, 60),
        protein_g=150,
        fat_g=60,
    )
    c.approve(
        proposal,
        topic_id=59,
        operator_id="richard",
        expected_digest=proposal.digest,
    )
    calls = []

    def failing_sender(*args):
        calls.append(args)
        raise TimeoutError("provider outcome unknown")

    unknown = c.deliver_approved_once(
        proposal,
        topic_id=59,
        chat_id="-1002",
        operator_id="richard",
        strict_sender=failing_sender,
    )
    assert unknown["event_type"] == "delivery_unknown"
    with pytest.raises(AdaptiveWorkflowError, match="already attempted"):
        c.deliver_approved_once(
            proposal,
            topic_id=59,
            chat_id="-1002",
            operator_id="richard",
            strict_sender=failing_sender,
        )
    assert len(calls) == 1


def test_negative_provider_receipt_is_unknown_and_never_retried(tmp_path):
    c = coordinator(tmp_path)
    day = date(2026, 7, 14)
    proposal, _ = c.create_proposal(
        topic_id=59,
        observations=rows(day),
        evaluation_day=day,
        policy=policy(date(2026, 7, 1)),
        current_target=MacroTarget(2304, 291, 150, 60),
        protein_g=150,
        fat_g=60,
    )
    c.approve(
        proposal,
        topic_id=59,
        operator_id="richard",
        expected_digest=proposal.digest,
    )
    calls = []

    def rejected_sender(*args):
        calls.append(args)
        return {"ok": False, "message_id": "provider-rejected"}

    unknown = c.deliver_approved_once(
        proposal,
        topic_id=59,
        chat_id="-1003",
        operator_id="richard",
        strict_sender=rejected_sender,
    )
    assert unknown["event_type"] == "delivery_unknown"
    with pytest.raises(AdaptiveWorkflowError, match="already attempted"):
        c.deliver_approved_once(
            proposal,
            topic_id=59,
            chat_id="-1003",
            operator_id="richard",
            strict_sender=rejected_sender,
        )
    assert len(calls) == 1


def test_adaptive_config_is_exact_topic_and_disabled_by_default():
    assert AdaptiveNutritionConfig.from_extra({}) is None
    assert AdaptiveNutritionConfig.from_extra({
        "adaptive_nutrition": {
            "enabled": True,
            "operator_chat_id": "-1004290459350",
            "operator_topic_id": 4,
            "delivery_enabled": False,
        }
    }) is None
    config = AdaptiveNutritionConfig.from_extra({
        "adaptive_nutrition": {
            "enabled": True,
            "operator_chat_id": "-1004290459350",
            "operator_topic_id": 59,
            "delivery_enabled": False,
        }
    })
    assert config is None
    full = AdaptiveNutritionConfig.from_extra({
        "adaptive_nutrition": {
            "enabled": True,
            "operator_chat_id": "review-chat",
            "operator_topic_id": 59,
            "delivery_enabled": False,
            "review_operator": {
                "user_id": "review-user",
                "chat_id": "review-chat",
                "topic_id": 59,
                "version": 7,
            },
        }
    })
    assert full is not None
    assert full.enabled is True
    assert full.review_operator is not None
    assert full.review_operator.key == ("review-user", "review-chat", "59")
    assert full.review_operator.version == 7
    assert AdaptiveNutritionConfig.from_extra({
        "adaptive_nutrition": {
            "enabled": True,
            "operator_chat_id": "wrong-chat",
            "operator_topic_id": 59,
            "delivery_enabled": False,
            "review_operator": {
                "user_id": "review-user",
                "chat_id": "review-chat",
                "topic_id": 59,
                "version": 7,
            },
        }
    }) is None



def test_review_ingress_is_distinct_from_canonical_owner_and_sessions_are_exact(tmp_path):
    from gateway.platforms.nutrition_coaching import IncomingAddress

    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 3)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True, display_name="Client"))
    forbidden_calls = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append(True)

    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 4}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
        adaptive_nutrition_coordinator=lambda _key: None,
        deliver_latest_once=forbidden,
        activate_latest=forbidden,
        set_customer_transport=forbidden,
    )
    service = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        session_path=tmp_path / "adaptive-operator-sessions.jsonl",
        now_provider=lambda: datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    address = IncomingAddress(*review.key)
    menu = service.open_menu(address, message_id="message-1", chat_id="review-chat")
    assert menu["status"] == "menu"
    callback = menu["buttons"][0]["callback_data"]
    selected = service.handle_callback(callback, address, message_id="message-1")
    assert selected["status"] == "selected"
    assert forbidden_calls == []
    assert coordinator.owner.key != review.key


def test_review_session_wrong_user_and_stale_duplicate_are_terminal(tmp_path):
    from gateway.platforms.nutrition_coaching import IncomingAddress

    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 1)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True))
    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 1}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
    )
    service = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        session_path=tmp_path / "sessions.jsonl",
    )
    address = IncomingAddress(*review.key)
    callback = service.open_menu(address, message_id="m", chat_id="review-chat")["buttons"][0]["callback_data"]
    wrong = IncomingAddress("wrong-user", "review-chat", "59")
    assert service.handle_callback(callback, wrong, message_id="m")["status"] == "rejected"
    assert service.handle_callback(callback, address, message_id="m")["status"] == "selected"
    duplicate = service.handle_callback(callback, address, message_id="m")
    assert duplicate["status"] == "duplicate"
    assert service.handle_callback("an1:" + "0" * 24 + ":select", address)["status"] == "rejected"
def test_operator_sessions_bind_action_provenance_and_expiry(tmp_path):
    from gateway.platforms.nutrition_coaching import IncomingAddress

    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 4)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True))
    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 1}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
    )
    now = [datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))]
    service = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        session_path=tmp_path / "sessions.jsonl",
        now_provider=lambda: now[0],
    )
    address = IncomingAddress(*review.key)
    callback = service.issue_session(
        action="select",
        customer_key="client_001",
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    session = service._latest_session(callback)
    assert session["action"] == "select"
    assert session["action_allowlist"] == ["select"]
    assert session["originating_chat_id"] == "review-chat"
    assert session["originating_topic_id"] == "59"
    assert session["provenance_digest"]
    assert session["nonce_digest"] == hashlib.sha256(
        callback.split(":")[1].encode("ascii")
    ).hexdigest()

    wrong_action = callback.rsplit(":", 1)[0] + ":create"
    assert service.handle_callback(wrong_action, address, message_id="review-card")["status"] == "rejected"
    assert service._latest_session(callback)["state"] == "issued"

    now[0] += timedelta(minutes=59)
    assert service.handle_callback(callback, address, message_id="review-card")["status"] == "selected"

    callback = service.issue_session(
        action="select",
        customer_key="client_001",
        originating_message_id="review-card-2",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    now[0] += timedelta(minutes=60)
    assert service.handle_callback(callback, address, message_id="review-card-2")["status"] == "rejected"
    assert service._latest_session(callback)["state"] == "expired"


def test_callback_claim_race_dispatches_create_once(tmp_path, monkeypatch):
    root = tmp_path / "callback-create-race"
    coordinator, _data_root = _production_fixture(root, monkeypatch)
    first, address = _adaptive_callback_service(coordinator, root)
    second, _ = _adaptive_callback_service(coordinator, root)
    callback = first.issue_session(
        action="create",
        customer_key=coordinator.customer_key,
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    started = ThreadEvent()
    release = ThreadEvent()
    calls = []
    original_create = coordinator.create_production_proposal

    def blocked_create(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(coordinator, "create_production_proposal", blocked_create)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(
            first.handle_callback,
            callback,
            address,
            message_id="review-card",
        )
        assert started.wait(timeout=5)
        loser_future = executor.submit(
            second.handle_callback,
            callback,
            address,
            message_id="review-card",
        )
        loser = loser_future.result(timeout=5)
        assert loser["status"] == "duplicate"
        release.set()
        winner = winner_future.result(timeout=10)
    assert winner["status"] == "card"
    assert "테스트 전용" in winner["text"]
    assert "고객: Client" in winner["text"]
    assert "KST day:" in winner["text"]
    assert "revision: 1" in winner["text"]
    assert "state: proposed" in winner["text"]
    assert "진행 1/4 · 제안 검토 중 · 다음: 승인하기" in winner["text"]
    assert [button["label"] for button in winner["buttons"]] == [
        "1/4 승인하기",
        "고급 · 내용 보기",
        "고급 · 메모 수정",
        "고급 · 보류",
        "이전",
    ]
    assert [
        button["callback_data"].rsplit(":", 1)[-1]
        for button in winner["buttons"]
    ] == ["approve", "view", "edit_note", "hold", "back"]
    assert calls == [1]
    assert sum(row["event_type"] == "plan_proposed" for row in coordinator.store.read()) == 1
    token = callback.split(":")[1]
    claimed_rows = [
        row for row in first._read_rows() if row.get("session_id") == token
    ]
    assert [row["state"] for row in claimed_rows] == ["issued", "claimed", "consumed"]


def test_operator_card_exposes_only_state_appropriate_primary_action(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path / "progressive-actions",
        monkeypatch,
    )
    service, _address = _adaptive_callback_service(coordinator, tmp_path)
    registration_digest = coordinator._registration_pin(proposal, required=True)
    expectations = {
        "approved": (
            "2/4 활성화하기",
            "activate",
            "진행 2/4 · 승인 완료 · 다음: 활성화하기",
        ),
        "activated": (
            "3/4 고객 전송 허용",
            "delivery_enable",
            "진행 3/4 · 활성화 완료 · 다음: 고객 전송 허용",
        ),
        "delivery_enabled": (
            "4/4 고객에게 전송",
            "send",
            "진행 4/4 · 전송 허용됨 · 다음: 고객에게 전송",
        ),
        "delivery_revoked": (
            "고객 전송 다시 허용",
            "delivery_enable",
            "안전 잠금 · 고객 전송 비활성화",
        ),
    }

    for state, (label, action, progress) in expectations.items():
        buttons = service._card_buttons(
            customer_key=coordinator.customer_key,
            proposal=proposal,
            message_id=f"card-{state}",
            chat_id="review-chat",
            topic_id=59,
            state=state,
            source_digest=proposal.source_digest,
            registration_digest=registration_digest,
        )
        assert buttons[0]["label"] == label
        assert buttons[0]["callback_data"].rsplit(":", 1)[-1] == action
        assert sum(
            not button["label"].startswith("고급") and button["label"] != "이전"
            for button in buttons
        ) == 1
        text, envelope = service._operator_card_envelope(
            proposal,
            customer_key=coordinator.customer_key,
            state=state,
            body="검토 본문",
        )
        assert progress in text
        assert envelope["state"] == state


@pytest.mark.parametrize(
    ("terminal_state", "expected_label"),
    (
        ("sent_audited", None),
        ("delivery_unknown", None),
        ("duplicate", None),
        ("audit_pending", "감사 기록 복구"),
    ),
)
def test_terminal_delivery_card_removes_send_and_exposes_only_valid_recovery(
    tmp_path,
    monkeypatch,
    terminal_state,
    expected_label,
):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path / terminal_state,
        monkeypatch,
    )
    service, _address = _adaptive_callback_service(coordinator, tmp_path)
    callback = service.issue_session(
        action="send",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="delivery-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )

    card = service.terminal_delivery_card(
        callback,
        {"status": terminal_state, "event_type": terminal_state},
    )

    assert card["status"] == "view"
    assert "send" not in str(card["buttons"])
    if expected_label is None:
        assert card["buttons"] == []
    else:
        assert [button["label"] for button in card["buttons"]] == [expected_label]
        assert (
            card["buttons"][0]["callback_data"].rsplit(":", 1)[-1]
            == "reconcile"
        )


def test_callback_claim_race_dispatches_mutation_once(tmp_path, monkeypatch):
    root = tmp_path / "callback-mutation-race"
    coordinator, _data_root, proposal = _activated_production_fixture(root, monkeypatch)
    first, address = _adaptive_callback_service(coordinator, root)
    second, _ = _adaptive_callback_service(coordinator, root)
    callback = first.issue_session(
        action="hold",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    started = ThreadEvent()
    release = ThreadEvent()
    calls = []
    original_hold = coordinator.hold_latest

    def blocked_hold(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)
        return original_hold(*args, **kwargs)

    monkeypatch.setattr(coordinator, "hold_latest", blocked_hold)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(
            first.handle_callback,
            callback,
            address,
            message_id="review-card",
        )
        assert started.wait(timeout=5)
        loser_future = executor.submit(
            second.handle_callback,
            callback,
            address,
            message_id="review-card",
        )
        loser = loser_future.result(timeout=5)
        assert loser["status"] == "duplicate"
        release.set()
        winner = winner_future.result(timeout=10)
    assert winner["status"] == "card"
    assert "테스트 전용" in winner["text"]
    assert "고객: Client" in winner["text"]
    assert "KST day:" in winner["text"]
    assert f"revision: {proposal.revision + 1}" in winner["text"]
    assert "state: held" in winner["text"]
    assert calls == [1]
    assert sum(row["event_type"] == "plan_edited" for row in coordinator.store.read()) == 1
    token = callback.split(":")[1]
    claimed_rows = [
        row for row in first._read_rows() if row.get("session_id") == token
    ]
    assert [row["state"] for row in claimed_rows] == ["issued", "claimed", "consumed"]

def test_operator_edit_note_returns_identity_envelope(tmp_path, monkeypatch):
    root = tmp_path / "operator-edit-note"
    coordinator, _data_root, proposal = _activated_production_fixture(root, monkeypatch)
    service, address = _adaptive_callback_service(coordinator, root)
    callback = service.issue_session(
        action="edit_note",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )

    prompt = service.handle_callback(callback, address, message_id="review-card")
    assert prompt["status"] == "operator_input_required"
    card = service.handle_text(
        address,
        message_id="review-card",
        text="운영자 확인 메모",
    )

    assert card["status"] == "card"
    assert "테스트 전용" in card["text"]
    assert "고객: Client" in card["text"]
    assert "KST day:" in card["text"]
    assert f"revision: {proposal.revision + 1}" in card["text"]
    assert "state: edited" in card["text"]

def test_publish_pending_card_recovers_once(tmp_path):
    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 1)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True))
    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 1}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
    )
    preview = (
        "오늘 상태: 안정적입니다.\n"
        "이전 흐름과 비교: 유지 중입니다.\n"
        "오늘 판단: 현재 계획을 유지합니다.\n"
        "판단 이유: 승인된 근거 범위입니다.\n"
        "오늘 할 일: 승인 식단 실행\n"
        "다음 확인: 내일 체크인"
    )
    proposal = SimpleNamespace(digest="d" * 64, revision=1)
    adaptive = SimpleNamespace(
        _latest_production_proposal=lambda: proposal,
        preview_registered_daily_projection=lambda _proposal: preview,
    )
    coordinator.adaptive_nutrition_coordinator = lambda _key: adaptive
    service_state = "proposed"
    card_text = (
        "고객: Client · 날짜: 2026-07-14\n"
        "이전 기록과 이번 입력을 함께 반영했습니다.\n"
        "판단: 현재 근거 범위에서만 잠금 · 승인 전 고객에게 전송되지 않습니다.\n\n"
        "실행 제안\n1. 승인 식단 실행\n\n"
        f"고객 전달 미리보기\n{preview}\n\n"
        "안전·과장 점검: 확인된 기록 범위를 넘는 단정이나 전송은 하지 않습니다."
    )
    service = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        session_path=tmp_path / "sessions.jsonl",
        now_provider=lambda: datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    service._proposal_card_state = lambda _adaptive, _proposal: service_state
    callback = service.issue_session(
        action="select",
        customer_key="client_001",
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    card = {
        "status": "card",
        "customer_key": "client_001",
        "text": card_text,
        "envelope": {
            "customer_label": "Client",
            "kst_day": "2026-07-14",
        },
        "buttons": [],
        **service._card_hidden_pins(proposal, service_state, preview),
    }
    service._consume(service._latest_session(callback), state="consumed")
    pending = service.mark_publish_pending(
        callback,
        card_payload=card,
        origin_message_id="review-card",
    )
    assert pending["state"] == "publish_pending"
    calls = []


    def publisher(payload):
        calls.append(payload)
        return {"ok": True, "message_id": "card-1"}

    recovered = service.recover_pending_cards(publisher)
    assert len(recovered["recovered"]) == 1
    recovered_text = recovered["recovered"][0]["card_payload"]["text"]
    assert recovered_text == card_text
    assert "revision:" not in recovered_text
    assert "state:" not in recovered_text
    assert recovered["pending"] == ()
    assert calls == [card]
    assert service._latest_session(callback)["state"] == "published"
    assert service._latest_session(callback)["published_message_id"] == "card-1"

    again = service.recover_pending_cards(publisher)
    assert again["recovered"] == ()
    assert again["pending"] == ()
    assert calls == [card]



@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (
            {"event_type": "delivery_unknown"},
            "전송 결과를 확인할 수 없습니다. 다시 보내지 마세요. 조정이 필요합니다.",
        ),
        (
            {"event_type": "delivered", "audit_pending": True},
            "고객 전송 영수증은 확인됐습니다. 재전송하지 말고 감사 기록을 복구해 주세요.",
        ),
        ({"event_type": "sent_audited"}, "고객 전송과 감사 기록이 완료되었습니다."),
        ({"status": "duplicate"}, "이미 처리된 전송입니다."),
    ),
)
def test_adaptive_delivery_result_text_is_safe(result, expected):
    assert adaptive_delivery_result_text(result) == expected
def test_owner_rotation_aborts_persisted_delivery_before_append(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path / "delivery", monkeypatch)
    capability = _typed_capability(coordinator, "delivery_enable")
    before = json.loads((data_root / "nutrition-plans" / "feature-epoch.json").read_text())
    original_assert = coordinator._assert_owner_snapshot

    def rotate_then_assert(operator_id, expected_key, expected_version):
        coordinator.authority.owner = SimpleNamespace(
            key=("rotated-owner", "rotated-chat", "8")
        )
        return original_assert(operator_id, expected_key, expected_version)

    monkeypatch.setattr(coordinator, "_assert_owner_snapshot", rotate_then_assert)
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        coordinator.set_persisted_delivery(True, operator_id=capability)
    after = json.loads((data_root / "nutrition-plans" / "feature-epoch.json").read_text())
    assert after == before
    assert not any(
        row.get("event_type") == "config_epoch"
        and row.get("payload", {}).get("epoch", 0) > 0
        for row in coordinator.store.read()
    )


def test_owner_rotation_aborts_activation_before_transition_append(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path / "activate", monkeypatch)
    proposal, _ = coordinator.create_production_proposal(
        date(2026, 7, 14),
        operator_id=OWNER_TRIPLE,
    )
    coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    capability = _typed_capability(
        coordinator,
        "activate",
        proposal_digest=proposal.digest,
        revision=proposal.revision,
    )
    before_rows = list(coordinator.store.read())
    original_assert = coordinator._assert_owner_snapshot

    def rotate_then_assert(operator_id, expected_key, expected_version):
        coordinator.authority.owner = SimpleNamespace(
            key=("rotated-owner", "rotated-chat", "8")
        )
        return original_assert(operator_id, expected_key, expected_version)

    monkeypatch.setattr(coordinator, "_assert_owner_snapshot", rotate_then_assert)
    with pytest.raises(AdaptiveWorkflowError):
        coordinator.activate_latest(proposal.digest, operator_id=capability)
    assert coordinator.store.read() == before_rows
    assert not any(
        row.get("event_type") == "transition_prepared"
        and row.get("payload", {}).get("proposal_digest") == proposal.digest
        for row in coordinator.store.read()[len(before_rows):]
    )
    assert json.loads((data_root / "nutrition-plans" / "feature-epoch.json").read_text())["activation"] is False


def test_owner_rotation_aborts_rollback_before_transition_append(tmp_path, monkeypatch):
    coordinator, data_root, proposal = _activated_production_fixture(
        tmp_path / "rollback",
        monkeypatch,
    )
    capability = _typed_capability(
        coordinator,
        "rollback",
        proposal_digest=proposal.digest,
        revision=proposal.revision,
    )
    before_rows = list(coordinator.store.read())
    original_assert = coordinator._assert_owner_snapshot

    def rotate_then_assert(operator_id, expected_key, expected_version):
        coordinator.authority.owner = SimpleNamespace(
            key=("rotated-owner", "rotated-chat", "8")
        )
        return original_assert(operator_id, expected_key, expected_version)

    monkeypatch.setattr(coordinator, "_assert_owner_snapshot", rotate_then_assert)
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        coordinator.rollback_latest(
            proposal.digest,
            as_of_kst=date(2026, 7, 14),
            operator_id=capability,
        )
    assert not any(
        row.get("event_type") == "transition_prepared"
        and row.get("payload", {}).get("proposal_digest") == proposal.digest
        for row in coordinator.store.read()[len(before_rows):]
    )
    assert json.loads((data_root / "nutrition-plans" / "feature-epoch.json").read_text())["activation"] is True

def test_production_lifecycle_rejects_raw_operator_identity(tmp_path, monkeypatch):
    coordinator, _data_root = _production_fixture(tmp_path / "strict", monkeypatch)
    coordinator._shadow_test_only = False
    with pytest.raises(AdaptiveWorkflowError, match="typed operator capability"):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

def test_opaque_adaptive_callbacks_are_bounded_and_stale_safe(tmp_path):
    c = coordinator(tmp_path)
    day = date(2026, 7, 14)
    proposal, _ = c.create_proposal(
        topic_id=59,
        observations=rows(day),
        evaluation_day=day,
        policy=policy(date(2026, 7, 1)),
        current_target=MacroTarget(2304, 291, 150, 60),
        protein_g=150,
        fat_g=60,
    )
    view = c.issue_callback(proposal, action="view")
    approve = c.issue_callback(proposal, action="approve")
    assert view.startswith("an1:") and len(view.encode()) <= 64
    assert c.handle_callback_token(
        view, topic_id=59, operator_id="richard"
    )["status"] == "view"
    receipt = c.handle_callback_token(
        approve, topic_id=59, operator_id="richard"
    )
    assert receipt["event_type"] == "plan_approved"
    with pytest.raises(AdaptiveWorkflowError, match="topic 59"):
        c.handle_callback_token(view, topic_id=4, operator_id="richard")


def test_live_telegram_adaptive_callback_requires_exact_owner_topic():
    from gateway.platforms.nutrition_coaching import IncomingAddress

    answers = []
    review = AdaptiveReviewOperator("8693203710", "-1004290459350", 59, 1)

    class Query:
        from_user = SimpleNamespace(id=8693203710)

        async def answer(self, text=None):
            answers.append(text)

        async def edit_message_text(self, text, **_kwargs):
            answers.append(text)

    def handle_callback(_data, _address, **_kwargs):
        return {"status": "view", "text": "검토 카드"}

    service = SimpleNamespace(
        accepts=lambda address: address.key == review.key,
        handle_callback=handle_callback,
    )
    message = SimpleNamespace(
        chat_id=-1004290459350,
        message_thread_id=59,
    )
    adapter = SimpleNamespace(
        _adaptive_nutrition_config=AdaptiveNutritionConfig(
            True,
            "-1004290459350",
            59,
            False,
            "8693203710",
            1,
            review,
        ),
        _adaptive_operator_service=service,
        _nutrition_address=lambda query, target: IncomingAddress(
            str(query.from_user.id),
            str(target.chat_id),
            str(target.message_thread_id),
        ),
    )
    asyncio.run(TelegramAdapter._handle_adaptive_review_callback(
        adapter,
        Query(),
        "an1:" + "a" * 24 + ":view",
        message,
    ))
    assert answers == ["검토 카드", None]
def test_live_telegram_adaptive_callback_rejects_non_owner_or_wrong_space():
    from gateway.platforms.nutrition_coaching import IncomingAddress

    answers = []
    review = AdaptiveReviewOperator("8693203710", "-1004290459350", 59, 1)

    class Query:
        def __init__(self, user_id):
            self.from_user = SimpleNamespace(id=user_id)

        async def answer(self, text=None):
            answers.append(text)

    def reject_if_called(_data, address, **_kwargs):
        assert address.key != review.key
        return {
            "status": "rejected",
            "text": "이 버튼은 운영자 검토실에서만 사용할 수 있습니다.",
        }

    service = SimpleNamespace(
        accepts=lambda address: address.key == review.key,
        handle_callback=reject_if_called,
    )
    adapter = SimpleNamespace(
        _adaptive_nutrition_config=AdaptiveNutritionConfig(
            True,
            "-1004290459350",
            59,
            False,
            "8693203710",
            1,
            review,
        ),
        _adaptive_operator_service=service,
        _nutrition_address=lambda query, target: IncomingAddress(
            str(query.from_user.id),
            str(target.chat_id),
            str(target.message_thread_id),
        ),
    )
    for user_id, chat_id, topic_id in (
        (123, -1004290459350, 59),
        (8693203710, -1000000000000, 59),
        (8693203710, -1004290459350, " 59 "),
    ):
        message = SimpleNamespace(
            chat_id=chat_id,
            message_thread_id=topic_id,
        )
        asyncio.run(TelegramAdapter._handle_adaptive_review_callback(
            adapter,
            Query(user_id),
            "an1:" + "a" * 24 + ":view",
            message,
        ))
    assert len(answers) == 3
    assert all(answer == "이 버튼은 운영자 검토실에서만 사용할 수 있습니다." for answer in answers)
def test_live_telegram_adaptive_callback_requires_configured_review_space():
    from gateway.platforms.nutrition_coaching import IncomingAddress

    answers = []
    review = AdaptiveReviewOperator("8693203710", "-1007000000000", 59, 1)

    class Query:
        def __init__(self, user_id):
            self.from_user = SimpleNamespace(id=user_id)

        async def answer(self, text=None):
            answers.append(text)

        async def edit_message_text(self, text, **_kwargs):
            answers.append(text)

    def handle_callback(_data, address, **_kwargs):
        if address.key != review.key:
            return {
                "status": "rejected",
                "text": "이 버튼은 운영자 검토실에서만 사용할 수 없습니다.",
            }
        return {"status": "view", "text": "검토 카드"}

    service = SimpleNamespace(
        accepts=lambda address: address.key == review.key,
        handle_callback=handle_callback,
    )
    adapter = SimpleNamespace(
        _adaptive_nutrition_config=AdaptiveNutritionConfig(
            True,
            "-1007000000000",
            59,
            False,
            "8693203710",
            1,
            review,
        ),
        _adaptive_operator_service=service,
        _nutrition_address=lambda query, target: IncomingAddress(
            str(query.from_user.id),
            str(target.chat_id),
            str(target.message_thread_id),
        ),
    )
    old_message = SimpleNamespace(chat_id=-1004290459350, message_thread_id=59)
    asyncio.run(TelegramAdapter._handle_adaptive_review_callback(
        adapter,
        Query(8693203710),
        "an1:" + "a" * 24 + ":view",
        old_message,
    ))
    assert answers == ["이 버튼은 운영자 검토실에서만 사용할 수 없습니다."]

    new_message = SimpleNamespace(chat_id=-1007000000000, message_thread_id=59)
    asyncio.run(TelegramAdapter._handle_adaptive_review_callback(
        adapter,
        Query(8693203710),
        "an1:" + "a" * 24 + ":view",
        new_message,
    ))
    assert answers[-2:] == ["검토 카드", None]
def test_telegram_registry_authority_is_configured_and_contained(tmp_path, monkeypatch):
    profile_root = tmp_path / "configured-profile"
    registry_path = profile_root / "customers" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(profile_root))
    config = SimpleNamespace(registry_path=Path("customers/registry.json"))
    adapter_config = SimpleNamespace(extra={})

    resolved_root, resolved_registry = TelegramAdapter._configured_nutrition_registry(
        config,
        adapter_config,
    )
    assert resolved_root == profile_root.resolve()
    assert resolved_registry == registry_path.resolve()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-profile"))
    with pytest.raises(ValueError, match="outside|unavailable|invalid"):
        TelegramAdapter._configured_nutrition_registry(config, adapter_config)


def test_async_registered_transport_success_is_audited(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append((customer_key, destination, reservation_id))
            await asyncio.sleep(0)
            return {"ok": True, "message_id": "async-message-1"}

    coordinator.set_customer_transport(Transport())
    delivered = asyncio.run(coordinator.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE))
    assert delivered["event_type"] == "sent_audited"
    assert len(calls) == 1
    assert len(calls[0][2]) == 64
    assert any(row["event_type"] == "sent_audited" for row in coordinator.store.read())


def test_async_registered_transport_unknown_is_terminal(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append((customer_key, destination, reservation_id))
            await asyncio.sleep(0)
            raise TimeoutError("provider outcome unknown")

    coordinator.set_customer_transport(Transport())
    unknown = asyncio.run(coordinator.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE))
    assert unknown["event_type"] == "delivery_unknown"
    with pytest.raises(AdaptiveWorkflowError, match="already attempted"):
        asyncio.run(coordinator.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE))
    assert len(calls) == 1


def test_delivery_disabled_rejects_before_provider_call(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append(1)
            return {"ok": True, "message_id": "must-not-send"}

    coordinator.set_customer_transport(Transport())
    coordinator.set_persisted_delivery(False, operator_id=OWNER_TRIPLE)
    with pytest.raises(AdaptiveWorkflowError, match="disabled"):
        asyncio.run(coordinator.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE))
    assert calls == []
def test_adaptive_transport_rejects_raw_and_unreserved_calls(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    transport = object.__new__(TelegramCustomerTransport)
    transport._adaptive_coordinator = lambda customer_key: coordinator
    destination = SimpleNamespace(user_id="2", chat_id="-100", topic_id="20")
    with pytest.raises(TypeError):
        asyncio.run(
            transport.send_adaptive_customer(
                "client_001",
                destination,
                proposal.customer_body,
            )
        )
    with pytest.raises(RuntimeError, match="reservation"):
        asyncio.run(
            transport.send_adaptive_customer(
                "client_001",
                destination,
                reservation_id="a" * 64,
            )
        )
@pytest.mark.parametrize("mode", ("positive", "d_plus_85", "revoked"))
def test_real_telegram_transport_checks_authority_before_consuming_reservation(
    tmp_path, monkeypatch, mode
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    coordinator.customer_runtime.spec.telegram.key = ("2", "-100", "20")
    outer = object.__new__(NutritionCoachingCoordinator)
    outer._delivery_enabled = True
    outer._kst_date_provider = lambda: (
        date(2026, 7, 14) if mode != "d_plus_85" else date(2026, 9, 23)
    )
    outer.adaptive_nutrition_coordinator = lambda _key: coordinator
    outer.customer = lambda _key: coordinator.customer_runtime
    if mode == "revoked":
        outer.customer_transport_allowed = lambda *_args, **_kwargs: False
    else:
        outer.customer_transport_allowed = lambda _key, _destination, **_kwargs: (
            NutritionCoachingCoordinator._adaptive_plan_window_allows(
                outer,
                coordinator.customer_runtime,
            )
        )
    strict_calls = []

    class Adapter:
        @staticmethod
        def _thread_kwargs_for_send(_chat_id, topic_id, _context):
            return {"message_thread_id": int(topic_id)}

        async def _send_message_strict_topic(self, **kwargs):
            strict_calls.append(kwargs)
            return {"ok": True, "message_id": "real-transport-message"}

    transport = TelegramCustomerTransport(Adapter(), outer)
    coordinator.set_customer_transport(transport)
    result = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=OWNER_TRIPLE,
        )
    )
    rows = coordinator.store.read()
    consumed = [
        row for row in rows
        if row["event_type"] == "delivery_attempt_consumed"
    ]
    if mode == "positive":
        assert result["event_type"] == "sent_audited"
        assert len(consumed) == 1
        assert consumed[0]["payload"]["risk_policy_version"]
        assert consumed[0]["payload"]["risk_policy_digest"]
        assert consumed[0]["payload"]["risk_policy_document_digest"]
        assert len(strict_calls) == 1
    else:
        assert result["event_type"] == "delivery_unknown"
        assert consumed == []
        assert strict_calls == []



def test_activation_crash_between_overlay_and_epoch_aborts_after_recovery(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(date(2026, 7, 14), operator_id=OWNER_TRIPLE)
    coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
    prior_epoch = json.loads(epoch_path.read_text())

    def fail_epoch(*args, **kwargs):
        raise OSError("simulated epoch crash")

    monkeypatch.setattr(coordinator, "_write_feature_epoch", fail_epoch)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    assert any(
        row["event_type"] == "transition_prepared"
        for row in coordinator.store.read()
    )
    monkeypatch.setattr(
        coordinator,
        "_write_feature_epoch",
        AdaptiveNutritionCoordinator._write_feature_epoch,
    )
    coordinator.recover_lifecycle_transactions()
    rows_after = coordinator.store.read()
    assert any(row["event_type"] == "transition_aborted" for row in rows_after)
    assert not any(row["event_type"] == "transition_committed" for row in rows_after)
    assert json.loads(epoch_path.read_text()) == prior_epoch
    assert coordinator._raw_overlay(date(2026, 7, 14)) is None
def test_activation_recovery_rejects_document_digest_rotation(tmp_path, monkeypatch):
    coordinator, _data_root = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(
        date(2026, 7, 14), operator_id=OWNER_TRIPLE
    )
    coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)

    def fail_epoch(*args, **kwargs):
        raise OSError("simulated epoch crash")

    monkeypatch.setattr(coordinator, "_write_feature_epoch", fail_epoch)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    monkeypatch.setattr(
        coordinator,
        "_write_feature_epoch",
        AdaptiveNutritionCoordinator._write_feature_epoch,
    )
    original_evidence = coordinator._risk_policy_evidence
    rotated_evidence = dict(original_evidence())
    rotated_evidence["risk_policy_document_digest"] = "f" * 64
    monkeypatch.setattr(coordinator, "_risk_policy_evidence", lambda: rotated_evidence)

    with pytest.raises(AdaptiveWorkflowError, match="recovery is required"):
        coordinator.recover_lifecycle_transactions()

    rows = coordinator.store.read()
    assert not any(
        row["event_type"] == "adaptive_plan_activated"
        and row["payload"]["proposal_digest"] == proposal.digest
        for row in rows
    )
    assert not any(row["event_type"] == "transition_committed" for row in rows)


def test_replacement_overlay_epoch_failure_aborts_and_restores_predecessor(
    tmp_path, monkeypatch
):
    coordinator, data_root, predecessor = _activated_production_fixture(tmp_path, monkeypatch)
    source = coordinator.canonical_event_source
    continuation = []
    for index in range(14, 28):
        raw = _canonical_event(index).model_dump(mode="json")
        raw["check_in"]["body_weight_kg"] = 79.4
        continuation.append(
            _CanonicalProjectionEvent(Event.model_validate(raw), index)
        )
    source.events = source.events[:14] + tuple(continuation[:7])
    _refresh_reconciled_test_sequence(coordinator)
    replacement, _ = coordinator.create_production_proposal(date(2026, 7, 21), operator_id=OWNER_TRIPLE)
    assert replacement.weekly_carb_cycle is not None
    assert len(replacement.weekly_carb_cycle.targets) == 7
    assert dict(replacement.weekly_carb_cycle.days)[date(2026, 7, 21)] == "medium"
    coordinator.approve_latest(replacement.digest, operator_id=OWNER_TRIPLE)
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"
    prior_epoch = json.loads(epoch_path.read_text())
    effective_day = replacement.snapshot.evaluation_day

    def fail_epoch(*args, **kwargs):
        raise OSError("simulated epoch crash")

    monkeypatch.setattr(coordinator, "_write_feature_epoch", fail_epoch)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.activate_latest(replacement.digest, operator_id=OWNER_TRIPLE)

    prepared = [
        row
        for row in coordinator.store.read()
        if row["event_type"] == "transition_prepared"
        and row["payload"]["proposal_digest"] == replacement.digest
    ]
    assert len(prepared) == 1
    transaction_id = prepared[0]["payload"]["transaction_id"]
    persisted = coordinator._raw_overlay(effective_day)
    assert persisted is not None
    assert persisted.revision_id == replacement.digest
    committed_before_recovery = coordinator._committed_overlay(effective_day)
    assert committed_before_recovery is not None
    assert committed_before_recovery.revision_id == predecessor.digest

    monkeypatch.setattr(
        coordinator,
        "_write_feature_epoch",
        AdaptiveNutritionCoordinator._write_feature_epoch,
    )
    coordinator.recover_lifecycle_transactions()
    rows_after = coordinator.store.read()
    assert any(
        row["event_type"] == "transition_aborted"
        and row["payload"]["transaction_id"] == transaction_id
        and row["payload"]["proposal_digest"] == replacement.digest
        for row in rows_after
    )
    assert not any(
        row["event_type"] == "transition_committed"
        and row["payload"]["transaction_id"] == transaction_id
        and row["payload"]["proposal_digest"] == replacement.digest
        for row in rows_after
    )
    assert not any(
        row["event_type"] == "adaptive_plan_activated"
        and row["payload"]["proposal_digest"] == replacement.digest
        for row in rows_after
    )
    resolved = coordinator._committed_overlay(effective_day)
    assert resolved is not None
    assert resolved.revision_id == predecessor.digest
    assert resolved.proposal_digest == predecessor.digest
    assert json.loads(epoch_path.read_text()) == prior_epoch


def test_base_rollback_crash_restores_prior_state_idempotently(tmp_path, monkeypatch):
    coordinator, data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"

    def fail_epoch(*args, **kwargs):
        raise OSError("simulated rollback epoch crash")

    monkeypatch.setattr(coordinator, "_write_feature_epoch", fail_epoch)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.rollback_latest(
            proposal.digest,
            as_of_kst=date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )
    assert coordinator._committed_overlay(date(2026, 7, 14)) is not None

    monkeypatch.setattr(
        coordinator,
        "_write_feature_epoch",
        AdaptiveNutritionCoordinator._write_feature_epoch,
    )
    coordinator.recover_lifecycle_transactions()
    rows_after = coordinator.store.read()
    assert any(
        row["event_type"] == "transition_aborted"
        and row["payload"]["action"] == "rollback"
        for row in rows_after
    )
    resolved = coordinator._committed_overlay(date(2026, 7, 14))
    assert resolved is not None
    assert resolved.revision_id == proposal.digest
    assert json.loads(epoch_path.read_text())["activation"] is True


def test_replacement_rollback_crash_restores_prior_state_idempotently(
    tmp_path, monkeypatch
):
    coordinator, data_root, predecessor = _activated_production_fixture(tmp_path, monkeypatch)
    source = coordinator.canonical_event_source
    continuation = []
    for index in range(14, 28):
        raw = _canonical_event(index).model_dump(mode="json")
        raw["check_in"]["body_weight_kg"] = 79.4
        continuation.append(
            _CanonicalProjectionEvent(Event.model_validate(raw), index)
        )
    source.events = source.events[:14] + tuple(continuation[:7])
    _refresh_reconciled_test_sequence(coordinator)
    replacement, _ = coordinator.create_production_proposal(
        date(2026, 7, 21),
        operator_id=OWNER_TRIPLE,
    )
    assert replacement.weekly_carb_cycle is not None
    assert len(replacement.weekly_carb_cycle.targets) == 7
    assert dict(replacement.weekly_carb_cycle.days)[date(2026, 7, 21)] == "medium"
    coordinator.approve_latest(replacement.digest, operator_id=OWNER_TRIPLE)
    coordinator.activate_latest(replacement.digest, operator_id=OWNER_TRIPLE)
    epoch_path = data_root / "nutrition-plans" / "feature-epoch.json"

    def fail_epoch(*args, **kwargs):
        raise OSError("simulated rollback epoch crash")

    monkeypatch.setattr(coordinator, "_write_feature_epoch", fail_epoch)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.rollback_latest(
            replacement.digest,
            as_of_kst=date(2026, 7, 21),
            operator_id=OWNER_TRIPLE,
        )
    committed_before_recovery = coordinator._committed_overlay(date(2026, 7, 21))
    assert committed_before_recovery is not None
    assert committed_before_recovery.revision_id == replacement.digest

    monkeypatch.setattr(
        coordinator,
        "_write_feature_epoch",
        AdaptiveNutritionCoordinator._write_feature_epoch,
    )
    coordinator.recover_lifecycle_transactions()
    rows_after = coordinator.store.read()
    assert any(
        row["event_type"] == "transition_aborted"
        and row["payload"]["action"] == "rollback"
        and row["payload"]["proposal_digest"] == replacement.digest
        for row in rows_after
    )
    resolved = coordinator._committed_overlay(date(2026, 7, 21))
    assert resolved is not None
    assert resolved.revision_id == replacement.digest
    assert json.loads(epoch_path.read_text())["activation"] is True

def test_activation_crash_before_receipt_commits_missing_receipt_on_recovery(
    tmp_path, monkeypatch
):
    coordinator, _data_root = _production_fixture(tmp_path, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(date(2026, 7, 14), operator_id=OWNER_TRIPLE)
    coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    original_append_locked = coordinator._append_locked

    def fail_receipt(store, event_type, payload, *, dedupe_key):
        if event_type == "adaptive_plan_activated":
            raise OSError("simulated receipt crash")
        return original_append_locked(store, event_type, payload, dedupe_key=dedupe_key)

    monkeypatch.setattr(coordinator, "_append_locked", fail_receipt)
    with pytest.raises(AdaptiveWorkflowError, match="incomplete"):
        coordinator.activate_latest(proposal.digest, operator_id=OWNER_TRIPLE)
    monkeypatch.setattr(
        coordinator,
        "_append_locked",
        AdaptiveNutritionCoordinator._append_locked,
    )
    coordinator.recover_lifecycle_transactions()
    rows_after = coordinator.store.read()
    assert any(row["event_type"] == "adaptive_plan_activated" for row in rows_after)
    assert any(row["event_type"] == "transition_committed" for row in rows_after)
    assert coordinator._committed_overlay(date(2026, 7, 14)) is not None


def test_concurrent_coordinators_reserve_once_before_async_provider_call(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    second = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key=coordinator.customer_key,
        starts_on=coordinator.starts_on,
        event_path=coordinator.store.path,
        profile_root=coordinator.profile_root,
        registry_path=coordinator.registry_path,
        canonical_event_source=coordinator.canonical_event_source,
        customer_runtime=coordinator.customer_runtime,
        authority=coordinator.authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append(1)
            await asyncio.sleep(0.01)
            return {"ok": True, "message_id": "one-provider-call"}

    transport = Transport()
    coordinator.set_customer_transport(transport)
    second.set_customer_transport(transport)

    async def run_both():
        return await asyncio.gather(
            coordinator.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE),
            second.deliver_latest_once(proposal.digest, chat_id="-100", operator_id=OWNER_TRIPLE),
            return_exceptions=True,
        )

    results = asyncio.run(run_both())
    assert sum(isinstance(result, dict) and result.get("event_type") == "sent_audited" for result in results) == 1
    assert sum(
        isinstance(result, AdaptiveWorkflowError)
        or (isinstance(result, dict) and result.get("status") == "duplicate")
        for result in results
    ) == 1
    assert len(calls) == 1
    reservations = [
        row for row in coordinator.store.read()
        if row["event_type"] == "delivery_attempt_started"
        and row["payload"].get("provider_receipt") is None
    ]
    assert len(reservations) == 1
def test_subprocess_shared_directory_reservation_race(tmp_path):
    events_path = tmp_path / "events.jsonl"
    calls_path = tmp_path / "provider-calls.jsonl"
    start_path = tmp_path / "start"
    ready_paths = (tmp_path / "ready-0", tmp_path / "ready-1")
    script = r"""
import os
import sys
import time
from pathlib import Path

from checkin_cli.adaptive_nutrition import AdaptiveEventStore, digest

events_path, calls_path, ready_path, start_path = map(Path, sys.argv[1:5])
ready_path.touch()
while not start_path.exists():
    time.sleep(0.005)
store = AdaptiveEventStore(events_path)
payload = {
    "customer_key": "client_001",
    "delivery_id": "shared-delivery",
    "reservation_id": "shared-delivery",
    "proposal_digest": "a" * 64,
    "customer_body_digest": "b" * 64,
    "destination": {"user_id": "2", "chat_id": "-100", "topic_id": "20"},
}
with store.locked():
    rows = store.read()
    existing = next(
        (
            row for row in rows
            if row.get("dedupe_key") == "production-attempt:shared-delivery"
        ),
        None,
    )
    if existing is None:
        body = {
            "event_type": "delivery_attempt_started",
            "payload": payload,
            "dedupe_key": "production-attempt:shared-delivery",
        }
        store._append_row(
            store.path,
            {**body, "event_id": digest(body)},
        )
        result = "reserved"
    else:
        result = "existing"
if result == "reserved":
    with calls_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
print(result)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(_PROFILE_PACKAGE), env.get("PYTHONPATH", "")))
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(events_path),
                str(calls_path),
                str(ready_path),
                str(start_path),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready_path in ready_paths
    ]
    deadline = time.monotonic() + 5
    while not all(path.exists() for path in ready_paths):
        if time.monotonic() >= deadline:
            raise AssertionError("reservation race subprocesses did not start")
        time.sleep(0.005)
    start_path.touch()
    completed = [process.communicate(timeout=10) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], completed
    assert sorted(output.strip() for output, _error in completed) == ["existing", "reserved"]
    assert len(calls_path.read_text(encoding="utf-8").splitlines()) == 1
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "production-attempt:shared-delivery"

def test_provider_receipt_reconciles_missing_delivery_audit_without_resend(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    send_capability = _typed_capability(
        coordinator,
        "send",
        proposal_digest=proposal.digest,
        revision=proposal.revision,
    )
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append(reservation_id)
            return {"ok": True, "message_id": "receipt-before-crash"}

    original_append_locked = coordinator._append_locked
    failed = {"value": False}

    def fail_audit(store, event_type, payload, *, dedupe_key):
        if event_type == "sent_audited" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated audit append failure")
        return original_append_locked(store, event_type, payload, dedupe_key=dedupe_key)

    coordinator.set_customer_transport(Transport())
    monkeypatch.setattr(coordinator, "_append_locked", fail_audit)
    pending = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=send_capability,
        )
    )
    assert pending["event_type"] == "audit_pending"
    assert pending["text"] == "고객 전송 영수증은 확인됐습니다. 재전송하지 말고 감사 기록을 복구해 주세요."
    monkeypatch.setattr(
        coordinator,
        "_append_locked",
        AdaptiveNutritionCoordinator._append_locked,
    )
    coordinator.set_persisted_delivery(
        False,
        operator_id=_typed_capability(coordinator, "delivery_revoke"),
    )
    reconciled = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=_typed_capability(
                coordinator,
                "reconcile",
                proposal_digest=proposal.digest,
                revision=proposal.revision,
            ),
        )
    )
    assert reconciled["event_type"] == "sent_audited"
    assert len(calls) == 1
    assert sum(
        row["event_type"] == "sent_audited"
        for row in coordinator.store.read()
    ) == 1
def _inject_source_revision(coordinator):
    source = coordinator.canonical_event_source
    event_index = len(source.events) - 1
    raw = getattr(source.events[event_index], "_event").model_dump(mode="json")
    check_in = dict(raw["check_in"])
    check_in["calories_kcal"] = int(check_in["calories_kcal"]) + 1
    raw["check_in"] = check_in
    source.events = source.events[:-1] + (
        _CanonicalProjectionEvent(Event.model_validate(raw), event_index),
    )


def _inject_registration_revision(coordinator):
    current = load_approved_adaptive_registration_inputs(
        coordinator.profile_root,
        coordinator.customer_key,
    )
    document = current.model_dump(mode="json", exclude_none=True)
    document.pop("digest", None)
    document.pop("supersedes_digest", None)
    document["preferences"] = [*document["preferences"], "race"]
    approve_adaptive_registration_inputs(
        coordinator.profile_root,
        coordinator.customer_key,
        inputs=document,
        approved_by={
            "user_id": OWNER_TRIPLE[0],
            "chat_id": OWNER_TRIPLE[1],
            "topic_id": OWNER_TRIPLE[2],
        },
        approved_at_kst="2026-07-01T11:00:00+09:00",
    )


def _inject_consent_revocation(coordinator):
    coordinator.customer_runtime.spec.ai_processing_consent.granted = False


def _inject_owner_revision(coordinator):
    coordinator.authority.owner = SimpleNamespace(
        key=("changed-owner", "-100", "59")
    )


def test_public_delivery_false_uses_persisted_enable_then_fresh_revoke_fails_closed(
    tmp_path, monkeypatch
):
    static_config = AdaptiveNutritionConfig.from_extra({
        "adaptive_nutrition": {
            "enabled": True,
            "operator_chat_id": "operator-chat",
            "operator_topic_id": 59,
            "delivery_enabled": False,
            "review_operator": {
                "user_id": "review-user",
                "chat_id": "operator-chat",
                "topic_id": 59,
                "version": 1,
            },
        }
    })
    assert static_config is not None
    assert static_config.delivery_enabled is False

    enabled, enabled_root, enabled_proposal = _activated_production_fixture(
        tmp_path / "persisted-enabled",
        monkeypatch,
    )

    def fresh_surface(source, strict_calls):
        registry, registry_path = load_committed_customer_registry(source.profile_root)

        class Adapter:
            @staticmethod
            def _thread_kwargs_for_send(_chat_id, topic_id, _context):
                return {"message_thread_id": int(topic_id)}

            async def _send_message_strict_topic(self, **kwargs):
                strict_calls.append(kwargs)
                return {"ok": True, "message_id": "telegram-real-1"}

        public = NutritionCoachingCoordinator(
            source.profile_root,
            registry,
            registry_path=registry_path,
            kst_date_provider=lambda: date(2026, 7, 14),
            delivery_enabled=static_config.delivery_enabled,
        )
        public.event_source = lambda key: (
            source.canonical_event_source if key == "client_001" else None
        )
        public.set_customer_transport(TelegramCustomerTransport(Adapter(), public))
        adaptive = public.adaptive_nutrition_coordinator("client_001")
        return public, adaptive

    strict_calls = []
    _public, fresh = fresh_surface(enabled, strict_calls)
    fresh._shadow_test_only = True
    persisted = json.loads(
        (enabled_root / "nutrition-plans" / "feature-epoch.json").read_text()
    )
    assert fresh.delivery_enabled is False
    assert persisted["delivery"] is True
    send_capability = _typed_capability(
        fresh,
        "send",
        proposal_digest=enabled_proposal.digest,
        revision=enabled_proposal.revision,
    )
    delivered = asyncio.run(
        fresh.deliver_latest_once(
            enabled_proposal.digest,
            chat_id="-100",
            operator_id=send_capability,
        )
    )
    assert delivered["event_type"] == "sent_audited"
    assert len(strict_calls) == 1
    assert sum(
        row["event_type"] == "delivery_attempt_consumed"
        for row in fresh.store.read()
    ) == 1

    revoked, revoked_root, revoked_proposal = _activated_production_fixture(
        tmp_path / "persisted-revoked",
        monkeypatch,
    )
    revoked.set_persisted_delivery(
        False,
        operator_id=_typed_capability(revoked, "delivery_revoke"),
    )
    revoked_calls = []
    _public, fresh_revoked = fresh_surface(revoked, revoked_calls)
    fresh_revoked._shadow_test_only = True
    persisted_revoked = json.loads(
        (revoked_root / "nutrition-plans" / "feature-epoch.json").read_text()
    )
    assert fresh_revoked.delivery_enabled is False
    assert persisted_revoked["delivery"] is False
    revoked_capability = _typed_capability(
        fresh_revoked,
        "send",
        proposal_digest=revoked_proposal.digest,
        revision=revoked_proposal.revision,
    )
    with pytest.raises(AdaptiveWorkflowError, match="disabled"):
        asyncio.run(
            fresh_revoked.deliver_latest_once(
                revoked_proposal.digest,
                chat_id="-100",
                operator_id=revoked_capability,
            )
        )
    assert revoked_calls == []
    assert not any(
        row["event_type"] == "delivery_attempt_started"
        and row["payload"].get("proposal_digest") == revoked_proposal.digest
        for row in fresh_revoked.store.read()
    )


@pytest.mark.parametrize(
    "mutation",
    ("source", "registration", "consent", "owner"),
)
def test_delivery_reservation_revalidation_records_unknown_for_each_authority_change(
    tmp_path, monkeypatch, mutation
):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path / mutation,
        monkeypatch,
    )
    mutators = {
        "source": _inject_source_revision,
        "registration": _inject_registration_revision,
        "consent": _inject_consent_revocation,
        "owner": _inject_owner_revision,
    }
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append((customer_key, destination, reservation_id))
            return {"ok": True, "message_id": "must-not-send"}

    coordinator.set_customer_transport(Transport())
    original_revalidate = coordinator.revalidate_transition
    revalidation_count = {"value": 0}

    def revalidate(action, proposal_digest):
        revalidation_count["value"] += 1
        if revalidation_count["value"] == 2:
            mutators[mutation](coordinator)
        return original_revalidate(action, proposal_digest)

    monkeypatch.setattr(coordinator, "revalidate_transition", revalidate)
    with pytest.raises(AdaptiveWorkflowError, match="authority changed"):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-100",
                operator_id=OWNER_TRIPLE,
            )
        )

    rows = coordinator.store.read()
    reservations = [
        row
        for row in rows
        if row["event_type"] == "delivery_attempt_started"
        and row["payload"].get("provider_receipt") is None
    ]
    unknown = [
        row
        for row in rows
        if row["event_type"] == "delivery_unknown"
        and row["payload"].get("proposal_digest") == proposal.digest
    ]
    assert revalidation_count["value"] == 2
    assert len(reservations) == 1
    assert len(unknown) == 1
    reservation_payload = reservations[0]["payload"]
    unknown_payload = unknown[0]["payload"]
    assert unknown_payload["delivery_id"] == reservation_payload["delivery_id"]
    assert unknown_payload["reservation_id"] == reservation_payload["reservation_id"]
    assert unknown_payload["attempt_event_id"] == reservations[0]["event_id"]
    assert unknown_payload["registration_digest"] == reservation_payload["registration_digest"]
    assert calls == []

    with pytest.raises(AdaptiveWorkflowError):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-100",
                operator_id=OWNER_TRIPLE,
            )
        )
    assert calls == []
    assert sum(row["event_type"] == "delivery_unknown" for row in coordinator.store.read()) == 1


@pytest.mark.parametrize("mutation", ("source", "registration"))
def test_approve_revalidation_rejects_source_or_registration_change_before_append(
    tmp_path, monkeypatch, mutation
):
    coordinator, _data_root = _production_fixture(tmp_path / mutation, monkeypatch)
    proposal, _ = coordinator.create_production_proposal(
        date(2026, 7, 14),
        operator_id=OWNER_TRIPLE,
    )
    original_validate = coordinator._validate_production_pins
    validation_count = {"value": 0}

    def validate(candidate, **kwargs):
        result = original_validate(candidate, **kwargs)
        validation_count["value"] += 1
        if validation_count["value"] == 1:
            (
                _inject_source_revision if mutation == "source"
                else _inject_registration_revision
            )(coordinator)
        return result

    monkeypatch.setattr(coordinator, "_validate_production_pins", validate)
    with pytest.raises(AdaptiveWorkflowError):
        coordinator.approve_latest(proposal.digest, operator_id=OWNER_TRIPLE)

    assert validation_count["value"] == 1
    assert not any(
        row["event_type"] == "plan_approved"
        and row["payload"].get("proposal_digest") == proposal.digest
        for row in coordinator.store.read()
    )


@pytest.mark.parametrize("mutation", ("source", "registration"))
def test_proposal_append_recheck_rejects_source_or_registration_change(
    tmp_path, monkeypatch, mutation
):
    coordinator, _data_root = _production_fixture(tmp_path / mutation, monkeypatch)
    original_validate = coordinator._validate_production_pins
    validation_count = {"value": 0}

    def validate(candidate, **kwargs):
        result = original_validate(candidate, **kwargs)
        validation_count["value"] += 1
        if validation_count["value"] == 1:
            (
                _inject_source_revision if mutation == "source"
                else _inject_registration_revision
            )(coordinator)
        return result

    monkeypatch.setattr(coordinator, "_validate_production_pins", validate)
    with pytest.raises(AdaptiveWorkflowError):
        coordinator.create_production_proposal(
            date(2026, 7, 14),
            operator_id=OWNER_TRIPLE,
        )

    assert validation_count["value"] == 1
    assert not any(
        row["event_type"] in {"plan_proposed", "plan_edited"}
        for row in coordinator.store.read()
    )


def test_competing_revision_children_leave_stale_child_unapprovable_and_undeliverable(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path,
        monkeypatch,
    )
    child = coordinator.revise_note(
        proposal,
        topic_id=59,
        note="first child",
        operator_id=OWNER_TRIPLE,
    )
    barrier = Barrier(2)

    def compete(action):
        barrier.wait(timeout=5)
        try:
            if action == "note":
                return coordinator.revise_note(
                    child,
                    topic_id=59,
                    note="competing note",
                    operator_id=OWNER_TRIPLE,
                )
            return coordinator.hold_proposal(
                child,
                topic_id=59,
                operator_id=OWNER_TRIPLE,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(compete, ("note", "hold")))

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], AdaptiveWorkflowError)
    edited = [
        row for row in coordinator.store.read()
        if row["event_type"] == "plan_edited"
    ]
    assert len(edited) == 2
    assert coordinator._latest_production_proposal().digest != child.digest

    provider_calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            provider_calls.append((customer_key, reservation_id))
            return {"ok": True, "message_id": "must-not-send"}

    coordinator.set_customer_transport(Transport())
    with pytest.raises(AdaptiveWorkflowError, match="stale"):
        coordinator.approve_latest(child.digest, operator_id=OWNER_TRIPLE)
    assert not any(
        row["event_type"] == "plan_approved"
        and row["payload"].get("proposal_digest") == child.digest
        for row in coordinator.store.read()
    )
    with pytest.raises(AdaptiveWorkflowError, match="stale"):
        asyncio.run(
            coordinator.deliver_latest_once(
                child.digest,
                chat_id="-100",
                operator_id=OWNER_TRIPLE,
            )
        )
    assert provider_calls == []
    assert not any(
        row["event_type"] == "delivery_attempt_started"
        and row["payload"].get("proposal_digest") == child.digest
        for row in coordinator.store.read()
    )
def test_p2_p6_fresh_revisions_have_exact_terminal_rows_and_provider_counts(
    tmp_path, monkeypatch
):
    success_text = "고객 전송과 감사 기록이 완료되었습니다."
    unknown_text = "전송 결과를 확인할 수 없습니다. 다시 보내지 마세요. 조정이 필요합니다."
    audit_pending_text = "고객 전송 영수증은 확인됐습니다. 재전송하지 말고 감사 기록을 복구해 주세요."

    coordinator, data_root, predecessor = _activated_production_fixture(
        tmp_path / "p2-p6-chain",
        monkeypatch,
        strict_capabilities=True,
    )
    import gateway.platforms.nutrition_coaching as nutrition_module

    original_propose = nutrition_module.propose

    def transport_scenario_propose(*args, **kwargs):
        candidate = original_propose(*args, **kwargs)
        if candidate.decision not in {Decision.HUMAN_REVIEW, Decision.OBSERVE}:
            return candidate
        return replace(
            candidate,
            decision=Decision.MAINTAIN,
            reasons=("gate_d_transport_scenario",),
            target=predecessor.target,
            meal_plan=predecessor.meal_plan,
            carb_days=predecessor.carb_days,
            weekly_carb_cycle=predecessor.weekly_carb_cycle,
            explanation=predecessor.explanation,
        )

    monkeypatch.setattr(nutrition_module, "propose", transport_scenario_propose)

    def fresh_child(_case):
        nonlocal predecessor
        child, _ = coordinator.create_production_proposal(
            predecessor.snapshot.evaluation_day + timedelta(days=1),
            operator_id=_production_operator_capability(coordinator, "create"),
        )
        coordinator.approve_latest(
            child.digest,
            operator_id=_production_operator_capability(
                coordinator,
                "approve",
                proposal=child,
            ),
        )
        coordinator.activate_latest(
            child.digest,
            operator_id=_production_operator_capability(
                coordinator,
                "activate",
                proposal=child,
            ),
        )
        coordinator.set_persisted_delivery(
            True,
            operator_id=_production_operator_capability(
                coordinator,
                "delivery_enable",
                proposal=child,
            ),
        )
        assert child.revision == predecessor.revision + 1
        predecessor = child
        return coordinator, data_root, child

    def install_transport(coordinator, outcome):
        coordinator.customer_runtime.spec.telegram.key = ("2", "-100", "20")
        provider_calls = []

        class Adapter:
            @staticmethod
            def _thread_kwargs_for_send(_chat_id, topic_id, _context):
                return {"message_thread_id": int(topic_id)}

            async def _send_message_strict_topic(self, **kwargs):
                provider_calls.append(dict(kwargs))
                if outcome == "unknown":
                    raise TimeoutError("provider outcome unknown")
                return {"ok": True, "message_id": f"{outcome}-provider-1"}

        outer = object.__new__(NutritionCoachingCoordinator)
        outer._delivery_enabled = True
        outer._kst_date_provider = lambda: date(2026, 7, 14)
        outer.adaptive_nutrition_coordinator = lambda _key: coordinator
        outer.customer = lambda _key: coordinator.customer_runtime
        outer.customer_transport_allowed = lambda *_args, **_kwargs: True
        coordinator.set_customer_transport(TelegramCustomerTransport(Adapter(), outer))
        return provider_calls

    def delivery_rows(coordinator, proposal):
        rows = [
            row
            for row in coordinator.store.read()
            if isinstance(row.get("payload"), dict)
        ]
        delivery_ids = {
            row["payload"].get("delivery_id")
            for row in rows
            if row["payload"].get("proposal_digest") == proposal.digest
            and isinstance(row["payload"].get("delivery_id"), str)
        }
        return [
            row
            for row in rows
            if row["event_type"]
            in {
                "delivery_attempt_started",
                "delivery_receipt_recorded",
                "delivery_attempt_consumed",
                "delivery_unknown",
                "delivered",
                "sent_audited",
                "audit_pending",
            }
            and (
                row["payload"].get("proposal_digest") == proposal.digest
                or row["payload"].get("delivery_id") in delivery_ids
            )
        ]

    def reservation(rows):
        return next(
            row for row in rows
            if row["event_type"] == "delivery_attempt_started"
            and row["payload"].get("provider_receipt") is None
        )

    def linked_unknown(rows):
        return next(row for row in rows if row["event_type"] == "delivery_unknown")
    def durable_states(rows):
        return list(AdaptiveNutritionCoordinator.project_delivery_attempt(rows))

    def disable_feature_flags(coordinator, data_root, proposal):
        coordinator.rollback_latest(
            proposal.digest,
            as_of_kst=proposal.snapshot.evaluation_day,
            reason="abort:gate_d_cleanup",
            operator_id=_production_operator_capability(
                coordinator,
                "rollback",
                proposal=proposal,
            ),
        )
        coordinator.set_persisted_delivery(
            False,
            operator_id=_production_operator_capability(
                coordinator,
                "delivery_revoke",
                proposal=proposal,
            ),
        )
        coordinator.set_delivery_enabled(False)
        epoch = json.loads(
            (data_root / "nutrition-plans" / "feature-epoch.json").read_text()
        )
        assert epoch["activation"] is False
        assert epoch["delivery"] is False
        assert coordinator.delivery_enabled is False

    def deliver_after_deterministic_mutation(coordinator, proposal, mutate):
        send_capability = _production_operator_capability(
            coordinator,
            "send",
            proposal=proposal,
        )
        barrier = Barrier(2)
        original_revalidate = coordinator.revalidate_transition
        call_count = {"value": 0}

        def revalidate(action, proposal_digest):
            call_count["value"] += 1
            result = original_revalidate(action, proposal_digest)
            if call_count["value"] == 2:
                mutate()
                barrier.wait(timeout=5)
            return result

        monkeypatch.setattr(coordinator, "revalidate_transition", revalidate)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: asyncio.run(
                    coordinator.deliver_latest_once(
                        proposal.digest,
                        chat_id="-100",
                        operator_id=send_capability,
                    )
                )
            )
            barrier.wait(timeout=5)
            with pytest.raises(
                AdaptiveWorkflowError,
                match="authority changed after reservation",
            ):
                future.result(timeout=10)
        monkeypatch.setattr(coordinator, "revalidate_transition", original_revalidate)
        assert call_count["value"] == 2

    coordinator, data_root, proposal = fresh_child("p2")
    provider_calls = install_transport(coordinator, "success")
    delivered = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=_production_operator_capability(
                coordinator,
                "send",
                proposal=proposal,
            ),
        )
    )
    rows_for_delivery = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_for_delivery
    ] == [
        "delivery_attempt_started",
        "delivery_attempt_consumed",
        "delivery_receipt_recorded",
        "delivered",
        "sent_audited",
    ]
    assert rows_for_delivery[2]["payload"]["provider_receipt"] == "success-provider-1"
    assert durable_states(rows_for_delivery) == [
        "reservation-started",
        "consumed",
        "receipt-started",
        "delivered",
        "sent_audited",
    ]
    assert delivered["event_type"] == "sent_audited"
    assert adaptive_delivery_result_text(delivered) == success_text
    assert len(provider_calls) == 1
    assert any("\uac00" <= char <= "\ud7a3" for char in provider_calls[0]["text"])
    rows_before_duplicate = list(rows_for_delivery)
    duplicate = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=_production_operator_capability(
                coordinator,
                "send",
                proposal=proposal,
            ),
        )
    )
    assert duplicate["event_type"] == "duplicate"
    assert duplicate["status"] == "duplicate"
    assert adaptive_delivery_result_text(duplicate) == "이미 처리된 전송입니다."
    assert delivery_rows(coordinator, proposal) == rows_before_duplicate
    assert len(provider_calls) == 1
    disable_feature_flags(coordinator, data_root, proposal)

    coordinator, data_root, proposal = fresh_child("p3")
    provider_calls = install_transport(coordinator, "unknown")
    unknown_result = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=_production_operator_capability(
                coordinator,
                "send",
                proposal=proposal,
            ),
        )
    )
    rows_for_delivery = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_for_delivery
    ] == [
        "delivery_attempt_started",
        "delivery_attempt_consumed",
        "delivery_unknown",
    ]
    assert durable_states(rows_for_delivery) == [
        "reservation-started",
        "consumed",
        "delivery_unknown",
    ]
    unknown = linked_unknown(rows_for_delivery)
    started = reservation(rows_for_delivery)
    assert unknown["payload"]["delivery_id"] == started["payload"]["delivery_id"]
    assert unknown["payload"]["reservation_id"] == started["payload"]["reservation_id"]
    assert unknown["payload"]["attempt_event_id"] == started["event_id"]
    assert unknown["payload"]["reason"] == "TimeoutError"
    assert unknown_result["event_type"] == "delivery_unknown"
    assert adaptive_delivery_result_text(unknown_result) == unknown_text
    with pytest.raises(AdaptiveWorkflowError, match="already attempted"):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-100",
                operator_id=_production_operator_capability(
                    coordinator,
                    "send",
                    proposal=proposal,
                ),
            )
        )
    assert len(provider_calls) == 1
    assert delivery_rows(coordinator, proposal) == rows_for_delivery
    disable_feature_flags(coordinator, data_root, proposal)

    coordinator, data_root, proposal = fresh_child("p4")
    provider_calls = install_transport(coordinator, "success")
    original_append_locked = coordinator._append_locked
    failed = {"value": False}

    def fail_first_audit(store, event_type, payload, *, dedupe_key):
        if event_type == "sent_audited" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated audit append failure")
        return original_append_locked(store, event_type, payload, dedupe_key=dedupe_key)

    monkeypatch.setattr(coordinator, "_append_locked", fail_first_audit)
    audit_pending = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=_production_operator_capability(
                coordinator,
                "send",
                proposal=proposal,
            ),
        )
    )
    rows_before_reconcile = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_before_reconcile
    ] == [
        "delivery_attempt_started",
        "delivery_attempt_consumed",
        "delivery_receipt_recorded",
        "delivered",
        "audit_pending",
    ]
    assert durable_states(rows_before_reconcile) == [
        "reservation-started",
        "consumed",
        "receipt-started",
        "delivered",
        "audit_pending",
    ]
    assert audit_pending["event_type"] == "audit_pending"
    assert audit_pending["text"] == audit_pending_text
    assert not any(
        row["event_type"] == "sent_audited"
        for row in rows_before_reconcile
    )
    monkeypatch.setattr(
        coordinator,
        "_append_locked",
        AdaptiveNutritionCoordinator._append_locked,
    )
    rows_before_raw_reconcile = list(coordinator.store.read())
    with pytest.raises(AdaptiveWorkflowError, match="action=reconcile"):
        coordinator.reconcile_delivery_receipts(proposal.digest)
    assert coordinator.store.read() == rows_before_raw_reconcile
    reconciled = coordinator.reconcile_delivery_receipts(
        proposal.digest,
        operator_id=_production_operator_capability(
            coordinator,
            "reconcile",
            proposal=proposal,
        ),
    )
    assert len(reconciled) == 1
    assert reconciled[0]["event_type"] == "sent_audited"
    assert adaptive_delivery_result_text({"event_type": "audit_pending"}) == audit_pending_text
    assert len(provider_calls) == 1
    rows_after_reconcile = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_after_reconcile
    ] == [
        "delivery_attempt_started",
        "delivery_attempt_consumed",
        "delivery_receipt_recorded",
        "delivered",
        "audit_pending",
        "sent_audited",
    ]
    assert durable_states(rows_after_reconcile) == [
        "reservation-started",
        "consumed",
        "receipt-started",
        "delivered",
        "audit_pending",
        "sent_audited",
    ]
    disable_feature_flags(coordinator, data_root, proposal)

    coordinator, data_root, proposal = fresh_child("p5")
    provider_calls = install_transport(coordinator, "success")
    delivery_revoke_capability = _production_operator_capability(
        coordinator,
        "delivery_revoke",
        proposal=proposal,
    )
    deliver_after_deterministic_mutation(
        coordinator,
        proposal,
        lambda: coordinator.set_persisted_delivery(
            False,
            operator_id=delivery_revoke_capability,
        ),
    )
    rows_for_delivery = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_for_delivery
    ] == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert durable_states(rows_for_delivery) == [
        "reservation-started",
        "delivery_unknown",
    ]
    unknown = linked_unknown(rows_for_delivery)
    started = reservation(rows_for_delivery)
    assert unknown["payload"]["delivery_id"] == started["payload"]["delivery_id"]
    assert unknown["payload"]["reservation_id"] == started["payload"]["reservation_id"]
    assert unknown["payload"]["attempt_event_id"] == started["event_id"]
    assert unknown["payload"]["reason"] == "delivery_revoked_after_reservation"
    assert adaptive_delivery_result_text(unknown) == unknown_text
    assert not any(
        row["event_type"] == "delivery_attempt_consumed"
        for row in rows_for_delivery
    )
    assert provider_calls == []
    with pytest.raises(AdaptiveWorkflowError):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-100",
                operator_id=_production_operator_capability(
                    coordinator,
                    "send",
                    proposal=proposal,
                ),
            )
        )
    assert provider_calls == []
    disable_feature_flags(coordinator, data_root, proposal)

    coordinator, data_root, proposal = fresh_child("p6")
    provider_calls = install_transport(coordinator, "success")

    def rotate_owner():
        coordinator.authority.owner = SimpleNamespace(
            key=("changed-owner", "-100", "59")
        )

    deliver_after_deterministic_mutation(
        coordinator,
        proposal,
        rotate_owner,
    )
    rows_for_delivery = delivery_rows(coordinator, proposal)
    assert [
        row["event_type"] for row in rows_for_delivery
    ] == [
        "delivery_attempt_started",
        "delivery_unknown",
    ]
    assert durable_states(rows_for_delivery) == [
        "reservation-started",
        "delivery_unknown",
    ]
    unknown = linked_unknown(rows_for_delivery)
    started = reservation(rows_for_delivery)
    assert unknown["payload"]["delivery_id"] == started["payload"]["delivery_id"]
    assert unknown["payload"]["reservation_id"] == started["payload"]["reservation_id"]
    assert unknown["payload"]["attempt_event_id"] == started["event_id"]
    assert unknown["payload"]["reason"] == "owner_changed_after_reservation"
    assert adaptive_delivery_result_text(unknown) == unknown_text
    assert not any(
        row["event_type"] == "delivery_attempt_consumed"
        for row in rows_for_delivery
    )
    assert provider_calls == []
    coordinator.authority.owner = SimpleNamespace(key=OWNER_TRIPLE)
    with pytest.raises(AdaptiveWorkflowError):
        asyncio.run(
            coordinator.deliver_latest_once(
                proposal.digest,
                chat_id="-100",
                operator_id=_production_operator_capability(
                    coordinator,
                    "send",
                    proposal=proposal,
                ),
            )
        )
    assert provider_calls == []
    disable_feature_flags(coordinator, data_root, proposal)
def test_missing_review_operator_version_is_rejected():
    assert AdaptiveNutritionConfig.from_extra(
        {
            "adaptive_nutrition": {
                "enabled": True,
                "operator_chat_id": "review-chat",
                "operator_topic_id": 59,
                "delivery_enabled": False,
                "review_operator": {
                    "user_id": "review-user",
                    "chat_id": "review-chat",
                    "topic_id": 59,
                },
            }
        }
    ) is None
    with pytest.raises(TypeError):
        AdaptiveReviewOperator("review-user", "review-chat", 59)


def test_forged_capability_without_durable_session_fails_before_mutation(tmp_path, monkeypatch):
    coordinator, data_root = _production_fixture(tmp_path / "forged", monkeypatch)
    coordinator._shadow_test_only = False
    capability = _typed_capability(coordinator, "delivery_enable")
    before_epoch = json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text(encoding="utf-8")
    )
    before_rows = list(coordinator.store.read())
    with pytest.raises(AdaptiveWorkflowError, match="durable session ledger"):
        coordinator.set_persisted_delivery(True, operator_id=capability)
    assert json.loads(
        (data_root / "nutrition-plans" / "feature-epoch.json").read_text(encoding="utf-8")
    ) == before_epoch
    assert coordinator.store.read() == before_rows


def test_profile_authority_flock_serializes_processes_and_owner_change_is_stale(
    tmp_path, monkeypatch
):
    coordinator, _data_root = _production_fixture(tmp_path / "owner-change", monkeypatch)
    coordinator._shadow_test_only = False
    capability = _typed_capability(coordinator, "delivery_enable")
    coordinator.authority.owner = SimpleNamespace(
        key=("rotated-owner", "rotated-chat", "59")
    )
    with pytest.raises(AdaptiveWorkflowError, match="owner"):
        coordinator.set_persisted_delivery(True, operator_id=capability)
    profile_root = tmp_path / "owner-change" / "profile"
    entered = tmp_path / "lock-entered"
    release = tmp_path / "lock-release"
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from checkin_cli.customer_admin import profile_authority_lock\n"
        "root=Path(sys.argv[1]); entered=Path(sys.argv[2]); release=Path(sys.argv[3])\n"
        "with profile_authority_lock(root):\n"
        "    entered.touch()\n"
        "    while not release.exists(): time.sleep(0.01)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PROFILE_PACKAGE) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(profile_root), str(entered), str(release)],
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists()
        contender = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "from pathlib import Path\n"
                    "from checkin_cli.customer_admin import profile_authority_lock\n"
                    "with profile_authority_lock(Path(sys.argv[1])): pass\n"
                ),
                str(profile_root),
            ],
            env=env,
        )
        time.sleep(0.05)
        assert contender.poll() is None
        release.touch()
        assert process.wait(timeout=5) == 0
        assert contender.wait(timeout=5) == 0
    finally:
        release.touch()
        process.kill() if process.poll() is None else None
def test_adaptive_send_callback_duplicate_is_exact_and_does_not_retry(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append(reservation_id)
            return {"ok": True, "message_id": "provider-1"}

    coordinator.set_customer_transport(Transport())
    asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=OWNER_TRIPLE,
        )
    )
    before = list(coordinator.store.read())
    service, address = _adaptive_callback_service(coordinator, tmp_path)
    callback = service.issue_session(
        action="send",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    pending_result = service.handle_callback(
        callback,
        address,
        message_id="review-card",
    )
    assert pending_result["status"] == "delivery_pending"
    result = asyncio.run(pending_result["delivery"])
    assert result["status"] == "duplicate"
    assert result["text"] == "이미 처리된 전송입니다."
    assert coordinator.store.read() == before
    assert len(calls) == 1
    second = service.handle_callback(callback, address, message_id="review-card")
    assert second["status"] == "duplicate"
    assert second["text"] == "이미 처리된 전송입니다."
    assert coordinator.store.read() == before
    assert len(calls) == 1


def test_adaptive_reconcile_callback_completes_without_provider_retry(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            calls.append(reservation_id)
            return {"ok": True, "message_id": "provider-1"}

    coordinator.set_customer_transport(Transport())
    original_append_locked = coordinator._append_locked
    failed = {"value": False}

    def fail_first_audit(store, event_type, payload, *, dedupe_key):
        if event_type == "sent_audited" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated audit append failure")
        return original_append_locked(store, event_type, payload, dedupe_key=dedupe_key)

    monkeypatch.setattr(coordinator, "_append_locked", fail_first_audit)
    pending = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=OWNER_TRIPLE,
        )
    )
    assert pending["event_type"] == "audit_pending"
    monkeypatch.setattr(
        coordinator,
        "_append_locked",
        AdaptiveNutritionCoordinator._append_locked,
    )
    service, address = _adaptive_callback_service(coordinator, tmp_path)
    send_callback = service.issue_session(
        action="send",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    before_send = list(coordinator.store.read())
    send_result = service.handle_callback(
        send_callback,
        address,
        message_id="review-card",
    )
    assert send_result["status"] == "delivery_pending"
    pending_delivery = asyncio.run(send_result["delivery"])
    assert pending_delivery["status"] == "audit_pending"
    assert pending_delivery["text"] == "고객 전송 영수증은 확인됐습니다. 재전송하지 말고 감사 기록을 복구해 주세요."
    assert coordinator.store.read() == before_send
    assert len(calls) == 1
    callback = service.issue_session(
        action="reconcile",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=proposal.source_digest,
        registration_digest=coordinator._registration_pin(proposal, required=True),
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    result = service.handle_callback(callback, address, message_id="review-card")
    assert result["status"] == "sent_audited"
    assert result["text"] == "고객 전송과 감사 기록이 완료되었습니다."
    assert len(calls) == 1
    assert sum(row["event_type"] == "sent_audited" for row in coordinator.store.read()) == 1
@pytest.mark.parametrize(
    "action",
    (
        "hold",
        "release",
        "approve",
        "activate",
        "delivery_enable",
        "delivery_revoke",
        "send",
        "reconcile",
    ),
)
def test_mutating_action_pin_toctou_rejects_before_downstream_mutation(
    tmp_path, monkeypatch, action
):
    coordinator, _data_root, proposal = _activated_production_fixture(
        tmp_path / action,
        monkeypatch,
    )
    service, address = _adaptive_callback_service(coordinator, tmp_path / action)
    registration_digest = coordinator._registration_pin(proposal, required=True)
    before = list(coordinator.store.read())
    pin_fields = (
        "config_digest",
        "registry_digest",
        "consent_digest",
        "activation_digest",
        "source_digest",
        "registration_digest",
        "policy_digest",
        "catalog_digest",
        "meal_constraints_digest",
        "epoch_digest",
    )
    for index, pin_field in enumerate(pin_fields):
        callback = service.issue_session(
            action=action,
            customer_key=coordinator.customer_key,
            proposal_digest=proposal.digest,
            revision=proposal.revision,
            source_digest=proposal.source_digest,
            registration_digest=registration_digest,
            originating_message_id=f"review-card-{index}",
            originating_chat_id="review-chat",
            originating_topic_id=59,
        )
        original_pins = service._pins

        def stale_pins(*args, _field=pin_field, **kwargs):
            pins = dict(original_pins(*args, **kwargs))
            pins[_field] = "0" * 64
            return pins

        monkeypatch.setattr(service, "_pins", stale_pins)
        result = service.handle_callback(
            callback,
            address,
            message_id=f"review-card-{index}",
        )
        monkeypatch.setattr(service, "_pins", original_pins)
        assert result["status"] == "rejected"
        assert service._latest_session(callback)["state"] == "revoked"
        assert coordinator.store.read() == before
def test_publication_recovery_claims_once_across_services(tmp_path):
    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 1)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True))
    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 1}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
    )
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    session_path = profile_root / "data" / "owner-actions" / "sessions.jsonl"
    first = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        profile_root=profile_root,
        session_path=session_path,
    )
    second = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        profile_root=profile_root,
        session_path=session_path,
    )
    callback = first.issue_session(
        action="select",
        customer_key="client_001",
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    card = {"status": "menu", "text": "검토 메뉴", "buttons": []}
    first.mark_publish_pending(
        callback,
        card_payload=card,
        origin_message_id="review-card",
    )
    calls = []

    def publisher(payload):
        calls.append(payload)
        time.sleep(0.05)
        return {"ok": True, "message_id": "card-1"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda service: service.recover_pending_cards(publisher),
                (first, second),
            )
        )
    assert len(calls) == 1
    assert sum(len(result["recovered"]) for result in results) == 1
    assert first._latest_session(callback)["state"] == "published"
def test_receipt_only_audit_pending_recovery_survives_restart_without_provider(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    provider_calls = []

    class Transport:
        async def send_adaptive_customer(
            self, customer_key, destination, *, reservation_id
        ):
            provider_calls.append(reservation_id)
            return {"ok": True, "message_id": "receipt-only-provider"}

    coordinator.set_customer_transport(Transport())
    original_append_locked = coordinator._append_locked

    def fail_receipt_append(store, event_type, payload, *, dedupe_key):
        if (
            event_type == "delivery_receipt_recorded"
            and isinstance(payload, dict)
            and payload.get("provider_receipt") is not None
        ):
            raise OSError("simulated receipt append failure")
        return original_append_locked(store, event_type, payload, dedupe_key=dedupe_key)

    monkeypatch.setattr(coordinator, "_append_locked", fail_receipt_append)
    pending = asyncio.run(
        coordinator.deliver_latest_once(
            proposal.digest,
            chat_id="-100",
            operator_id=OWNER_TRIPLE,
        )
    )
    assert pending["event_type"] == "audit_pending"
    assert len(provider_calls) == 1
    assert [
        row["event_type"]
        for row in coordinator.store.read()
        if row.get("event_type") in {
            "delivery_attempt_started",
            "delivery_receipt_recorded",
            "delivery_attempt_consumed",
            "delivery_unknown",
            "delivered",
            "sent_audited",
            "audit_pending",
        }
        and row.get("payload", {}).get("proposal_digest") == proposal.digest
    ] == [
        "delivery_attempt_started",
        "audit_pending",
    ]

    monkeypatch.setattr(
        coordinator,
        "_append_locked",
        AdaptiveNutritionCoordinator._append_locked,
    )
    restarted = AdaptiveNutritionCoordinator._for_shadow_test(
        customer_key=coordinator.customer_key,
        starts_on=coordinator.starts_on,
        event_path=coordinator.store.path,
        profile_root=coordinator.profile_root,
        registry_path=coordinator.registry_path,
        canonical_event_source=coordinator.canonical_event_source,
        customer_runtime=coordinator.customer_runtime,
        authority=coordinator.authority,
        delivery_enabled=True,
        _shadow_factory_token=_ADAPTIVE_SHADOW_FACTORY_TOKEN,
    )
    capability = _production_operator_capability(
        restarted,
        "reconcile",
        proposal=proposal,
    )
    reconciled = restarted.reconcile_delivery_receipts(
        proposal.digest,
        operator_id=capability,
    )
    assert len(reconciled) == 1
    assert reconciled[0]["event_type"] == "sent_audited"
    assert len(provider_calls) == 1
    rows_after = list(restarted.store.read())
    assert sum(row["event_type"] == "sent_audited" for row in rows_after) == 1
    assert restarted.reconcile_delivery_receipts(
        proposal.digest,
        operator_id=capability,
    ) == ()
    assert restarted.store.read() == rows_after


def test_view_and_back_callbacks_replay_without_claiming_or_reissuing(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    service, address = _adaptive_callback_service(coordinator, tmp_path)
    registration_digest = coordinator._registration_pin(proposal, required=True)
    common = {
        "customer_key": coordinator.customer_key,
        "proposal_digest": proposal.digest,
        "revision": proposal.revision,
        "source_digest": proposal.source_digest,
        "registration_digest": registration_digest,
        "originating_message_id": "review-card",
        "originating_chat_id": "review-chat",
        "originating_topic_id": 59,
    }

    view_callback = service.issue_session(action="view", **common)
    first_view = service.handle_callback(view_callback, address, message_id="review-card")
    rows_after_view = list(service._read_rows())
    second_view = service.handle_callback(view_callback, address, message_id="review-card")
    assert first_view == second_view
    assert service._read_rows() == rows_after_view
    assert service._latest_session(view_callback)["state"] == "issued"

    back_callback = service.issue_session(action="back", **common)
    first_back = service.handle_callback(back_callback, address, message_id="review-card")
    rows_after_back = list(service._read_rows())
    second_back = service.handle_callback(back_callback, address, message_id="review-card")
    assert first_back == second_back
    assert service._read_rows() == rows_after_back
    assert service._latest_session(back_callback)["state"] == "issued"


@pytest.mark.parametrize("mismatch", ("message", "pins"))
def test_view_callback_stale_forwarded_or_pinned_replay_rejects(
    tmp_path, monkeypatch, mismatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    service, address = _adaptive_callback_service(coordinator, tmp_path)
    registration_digest = coordinator._registration_pin(proposal, required=True)
    callback = service.issue_session(
        action="view",
        customer_key=coordinator.customer_key,
        proposal_digest=proposal.digest,
        revision=proposal.revision,
        source_digest=("0" * 64 if mismatch == "pins" else proposal.source_digest),
        registration_digest=registration_digest,
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    rejected = service.handle_callback(
        callback,
        address,
        message_id="forwarded-card" if mismatch == "message" else "review-card",
    )
    assert rejected["status"] == "rejected"
    assert service._latest_session(callback)["state"] == "revoked"


def test_humanizer_adaptive_grounding_accepts_only_production_fact_schema():
    from gateway.platforms.korean_humanizer import AdaptiveGroundingInput

    facts = AdaptiveCoachingFacts(
        evaluation_day="2026-07-28",
        goal_mode="lean_mass_gain",
        goal_range=("+0.10", "+0.25"),
        current_mean_kg="78.10",
        prior_mean_kg="78.00",
        weekly_rate_percent="+0.13",
        decision="observe",
        reason_category_ids=("insufficient_weight_samples",),
        target_macros=(("calories_kcal", 2600),),
        carb_category_targets=(),
        safety_held=False,
        approval_state="pending",
        delivery_state="not_delivered",
        proposal_digest="b" * 64,
        revision=1,
        revision_binding_digest="c" * 64,
    )

    accepted = AdaptiveGroundingInput.from_card(facts, customer_key="client_001")
    assert accepted.revision_binding_digest == "c" * 64

    foreign_type = make_dataclass(
        "AdaptiveCoachingFacts",
        [(field, object) for field in facts.__dataclass_fields__],
        frozen=True,
    )
    foreign = foreign_type(
        **{field: getattr(facts, field) for field in facts.__dataclass_fields__}
    )
    with pytest.raises(TypeError, match="exact typed projection"):
        AdaptiveGroundingInput.from_card(foreign)
def test_coaching_facts_reject_forged_digest_before_mutation(tmp_path, monkeypatch):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    service, _address = _adaptive_callback_service(coordinator, tmp_path)
    forged = SimpleNamespace(
        customer_key=proposal.customer_key,
        snapshot=proposal.snapshot,
        decision=proposal.decision,
        reasons=proposal.reasons,
        target=proposal.target,
        weekly_carb_cycle=proposal.weekly_carb_cycle,
        revision=proposal.revision,
        digest=object(),
    )
    before_rows = list(coordinator.store.read())
    with pytest.raises(AdaptiveWorkflowError, match="proposal digest"):
        service.coaching_facts_for_current_card(
            coordinator.customer_key,
            proposal=forged,
        )
    assert coordinator.store.read() == before_rows


def test_coaching_facts_binding_changes_for_omitted_projection_field_drift(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    service, _address = _adaptive_callback_service(coordinator, tmp_path)
    facts = service.coaching_facts_for_current_card(
        coordinator.customer_key,
        proposal=proposal,
    )
    drifted_snapshot = replace(
        proposal.snapshot,
        current_mean_kg=Decimal("999.99"),
    )
    drifted = SimpleNamespace(
        customer_key=proposal.customer_key,
        snapshot=drifted_snapshot,
        decision=proposal.decision,
        reasons=proposal.reasons,
        target=proposal.target,
        weekly_carb_cycle=proposal.weekly_carb_cycle,
        revision=proposal.revision,
        digest=proposal.digest,
    )
    drifted_facts = service.coaching_facts_for_current_card(
        coordinator.customer_key,
        proposal=drifted,
    )
    assert facts.current_mean_kg != drifted_facts.current_mean_kg
    assert facts.revision_binding_digest != drifted_facts.revision_binding_digest


def test_current_coaching_facts_binding_match_and_mismatch_are_read_only(
    tmp_path, monkeypatch
):
    coordinator, _data_root, proposal = _activated_production_fixture(tmp_path, monkeypatch)
    service, _address = _adaptive_callback_service(coordinator, tmp_path)
    facts = service.coaching_facts_for_current_card(
        coordinator.customer_key,
        proposal=proposal,
    )
    before_rows = list(coordinator.store.read())
    before_sessions = service._read_rows()
    provider_calls: list[object] = []
    assert service.current_coaching_facts_match_binding(
        coordinator.customer_key,
        facts.revision_binding_digest,
        proposal=proposal,
    ) is True
    assert service.current_coaching_facts_match_binding(
        coordinator.customer_key,
        "0" * 64,
        proposal=proposal,
    ) is False
    assert coordinator.store.read() == before_rows
    assert service._read_rows() == before_sessions
    assert provider_calls == []

def test_publication_unknown_outcome_is_not_retried(tmp_path):
    review = AdaptiveReviewOperator("review-user", "review-chat", 59, 1)
    customer = SimpleNamespace(spec=SimpleNamespace(enabled=True))
    coordinator = SimpleNamespace(
        owner=SimpleNamespace(key=("owner-user", "owner-chat", "7")),
        registry=SimpleNamespace(model_dump=lambda **_kwargs: {"version": 1}),
        _by_key={"client_001": SimpleNamespace(customer=customer)},
    )
    service = AdaptiveOperatorService(
        coordinator,
        review_operator=review,
        session_path=tmp_path / "sessions.jsonl",
    )
    callback = service.issue_session(
        action="select",
        customer_key="client_001",
        originating_message_id="review-card",
        originating_chat_id="review-chat",
        originating_topic_id=59,
    )
    service._consume(service._latest_session(callback), state="consumed")
    card = {"status": "menu", "text": "검토 메뉴", "buttons": []}
    service.mark_publish_pending(
        callback,
        card_payload=card,
        origin_message_id="review-card",
    )
    calls = []

    def uncertain(payload):
        calls.append(payload)
        raise TimeoutError("accepted response was lost")

    first = service.recover_pending_cards(uncertain)
    second = service.recover_pending_cards(uncertain)

    assert len(calls) == 1
    assert first["recovered"] == ()
    assert first["pending"][0]["state"] == "publish_claimed"
    assert second == {"recovered": (), "pending": ()}
    assert service._latest_session(callback)["state"] == "publish_claimed"
