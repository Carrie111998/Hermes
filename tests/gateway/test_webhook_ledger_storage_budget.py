"""Fail-closed total storage authority for the durable webhook ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import gateway.platforms.webhook_ledger as ledger_module
from gateway.platforms.webhook_auth import (
    WebhookLocalBypassReceipt,
    WebhookSignatureVerificationReceipt,
)
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    AdmitSaturationReason,
    MAXIMUM_MAX_STORAGE_BYTES,
    MINIMUM_MAX_STORAGE_BYTES,
    OperationState,
    Settlement,
    SettlementKind,
    TargetAttemptDisposition,
    WebhookLedgerCapacityError,
    WebhookLedgerConfigurationError,
    WebhookLedgerCorruptionError,
    WebhookLedgerError,
    WebhookLedgerTransitionError,
    WebhookOperationLedger,
)


def _envelope(
    identity: str,
    *,
    trace_id: str,
    route_name: str = "storage-budget",
    body_identity: str | None = None,
    local_bypass: bool = False,
) -> WebhookEnvelope:
    raw_body = json.dumps(
        {"event": "push", "identity": body_identity or identity},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {} if local_bypass else {"svix-id": identity}
    route = WebhookRouteConfig.bind(
        route_name,
        {"provider": "generic" if local_bypass else "svix"},
        headers=headers,
        request_profile="default",
    )
    receipt = (
        WebhookLocalBypassReceipt._issue(route, raw_body, headers)
        if local_bypass
        else WebhookSignatureVerificationReceipt._issue(route, raw_body, headers)
    )
    return WebhookEnvelope.from_receipt(
        receipt,
        raw_body=raw_body,
        media_type="application/json",
        trace_id=trace_id,
    )


def _create_populated_v4_ledger(
    db_path: Path,
    *,
    corrupt_state: bool = False,
    drop_replay_index: bool = False,
    invalid_event_json: bool = False,
    corrupt_body_digest: bool = False,
    invalid_created_at: bool = False,
    invalid_expiry: bool = False,
    extra_trigger: bool = False,
    weakened_operation_schema: bool = False,
    profile: str = "alpha",
) -> None:
    """Create the canonical pre-budget v4 core used by migration tests."""

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE webhook_operations (
                operation_id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                route TEXT NOT NULL,
                provider TEXT NOT NULL,
                replay_id TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                event_type TEXT NOT NULL,
                session_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'preparing','ready','running','delivery_ready',
                        'delivering','settled','indeterminate'
                    )
                ),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                owner_pid INTEGER,
                owner_started_at INTEGER,
                owner_instance TEXT NOT NULL,
                event_json TEXT,
                target_json TEXT,
                grant_json TEXT,
                script_started INTEGER NOT NULL DEFAULT 0 CHECK (
                    script_started IN (0,1)
                ),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                settled_at REAL,
                last_error TEXT
            );
            CREATE UNIQUE INDEX idx_webhook_operations_replay_identity
                ON webhook_operations(profile, route, provider, replay_id);
            CREATE INDEX idx_webhook_operations_state_updated
                ON webhook_operations(state, updated_at);
            CREATE TABLE webhook_targets (
                operation_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'pending','attempting','confirmed','suppressed','indeterminate'
                    )
                ),
                attempt_token TEXT,
                content_sha256 TEXT,
                delivery_json TEXT,
                delivery_sha256 TEXT,
                external_id TEXT,
                owner_pid INTEGER,
                owner_started_at INTEGER,
                owner_instance TEXT,
                started_at REAL,
                settled_at REAL,
                updated_at REAL NOT NULL,
                last_error TEXT,
                PRIMARY KEY(operation_id, target_id),
                FOREIGN KEY(operation_id) REFERENCES webhook_operations(operation_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE webhook_delivery_tombstones (
                profile TEXT NOT NULL,
                route TEXT NOT NULL,
                provider TEXT NOT NULL,
                replay_id TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('settled','indeterminate')
                ),
                settled_at REAL NOT NULL,
                expires_at REAL,
                PRIMARY KEY(profile, route, provider, replay_id)
            );
            CREATE INDEX idx_webhook_tombstones_expires_at
                ON webhook_delivery_tombstones(expires_at)
                WHERE expires_at IS NOT NULL;
            CREATE TABLE webhook_ledger_meta (
                schema_name TEXT PRIMARY KEY CHECK (
                    schema_name='webhook_operation_ledger'
                ),
                schema_version INTEGER NOT NULL CHECK (schema_version=4)
            );
            INSERT INTO webhook_ledger_meta VALUES (
                'webhook_operation_ledger', 4
            );
            """
        )
        target_json = json.dumps(
            {"kind": "log", "profile": profile, "v": 1},
            separators=(",", ":"),
            sort_keys=True,
        )
        target_id = hashlib.sha256(target_json.encode()).hexdigest()[:32]
        if corrupt_state:
            conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            """INSERT INTO webhook_operations (
                   operation_id, profile, route, provider, replay_id,
                   body_sha256, event_type, session_key, state, generation,
                   owner_pid, owner_started_at, owner_instance, event_json,
                   target_json, grant_json, script_started, created_at,
                   updated_at, settled_at, last_error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, ?, ?,
                         0, 100.0, 100.0, NULL, NULL)""",
            (
                "v4-operation",
                profile,
                "v4-route",
                "svix",
                "provider_id:v4-delivery",
                "a" * 64,
                "push",
                "agent:main:webhook:v4",
                "corrupt" if corrupt_state else "ready",
                "v4-instance",
                json.dumps(
                    {"payload": {"v": 4}, "text": "v4", "v": 1},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                target_json,
                json.dumps(
                    {"toolsets": [], "v": 1},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        if corrupt_state:
            conn.execute("PRAGMA ignore_check_constraints=OFF")
        if invalid_event_json:
            conn.execute(
                """UPDATE webhook_operations SET event_json='{'
                    WHERE operation_id='v4-operation'"""
            )
        if corrupt_body_digest:
            conn.execute(
                """UPDATE webhook_operations SET body_sha256=?
                    WHERE operation_id='v4-operation'""",
                ("z" * 64,),
            )
        if invalid_created_at:
            conn.execute(
                """UPDATE webhook_operations SET created_at='NaN'
                    WHERE operation_id='v4-operation'"""
            )
        conn.execute(
            """INSERT INTO webhook_targets (
                   operation_id, target_id, state, updated_at
               ) VALUES (?, ?, 'pending', 100.0)""",
            ("v4-operation", target_id),
        )
        conn.execute(
            """INSERT INTO webhook_delivery_tombstones (
                   profile, route, provider, replay_id, body_sha256,
                   operation_id, state, settled_at, expires_at
               ) VALUES (
                   ?, 'v4-history', 'svix', 'provider_id:v4-old', ?,
                   'v4-old-operation', 'settled', 90.0, NULL
               )""",
            (profile, "b" * 64),
        )
        if invalid_expiry:
            conn.execute(
                """UPDATE webhook_delivery_tombstones SET expires_at=100.0
                    WHERE replay_id='provider_id:v4-old'"""
            )
        if drop_replay_index:
            conn.execute("DROP INDEX idx_webhook_operations_replay_identity")
        if extra_trigger:
            conn.execute(
                """CREATE TRIGGER unexpected_v4_operation_trigger
                    AFTER UPDATE ON webhook_operations BEGIN SELECT 1; END"""
            )
        if weakened_operation_schema:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute(
                """UPDATE sqlite_master
                      SET sql=replace(
                          sql,
                          'generation INTEGER NOT NULL CHECK (generation >= 1)',
                          'generation INTEGER NOT NULL'
                      )
                    WHERE type='table' AND name='webhook_operations'"""
            )
            conn.execute("PRAGMA writable_schema=OFF")


def test_indeterminate_evidence_consumes_the_global_storage_budget(tmp_path: Path):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=8,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
    )
    envelope = _envelope("unknown", trace_id="unknown-trace")
    admitted = ledger.admit(envelope)
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None
    assert (
        ledger.admit(_envelope("unknown", trace_id="unknown-active")).disposition
        is AdmitDisposition.ACTIVE
    )
    assert (
        ledger.admit(
            _envelope(
                "unknown",
                trace_id="unknown-conflict",
                body_identity="changed",
            )
        ).disposition
        is AdmitDisposition.CONFLICT
    )
    assert ledger.mark_indeterminate(admitted.authority, "unknown postcondition")
    assert ledger.storage_usage() == (
        MINIMUM_MAX_STORAGE_BYTES,
        1,
        MINIMUM_MAX_STORAGE_BYTES,
    )

    saturated = ledger.admit(_envelope("new", trace_id="new-trace"))
    assert saturated.disposition is AdmitDisposition.SATURATED
    replay = ledger.admit(_envelope("unknown", trace_id="unknown-retry"))
    assert replay.disposition is AdmitDisposition.INDETERMINATE
    retained = ledger.lookup_session(admitted.authority.session_key)
    assert retained is not None
    assert retained.state is OperationState.INDETERMINATE


def test_permanent_proofs_eventually_saturate_without_reopening_replays(
    tmp_path: Path,
):
    tombstone_bytes = ledger_module._TOMBSTONE_STORAGE_RESERVATION_BYTES
    storage_limit = MINIMUM_MAX_STORAGE_BYTES + 2 * tombstone_bytes
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=8,
        max_storage_bytes=storage_limit,
    )

    for index in range(3):
        envelope = _envelope(
            f"settled-{index}",
            trace_id=f"trace-{index}",
            route_name=f"storage-budget-{index}",
        )
        admitted = ledger.admit(envelope)
        assert admitted.disposition is AdmitDisposition.ACCEPTED
        assert admitted.authority is not None
        assert ledger.settle_no_effect(admitted.authority)

    saturated = ledger.admit(
        _envelope(
            "overflow",
            trace_id="overflow-trace",
            route_name="storage-budget-overflow",
        )
    )
    assert saturated.disposition is AdmitDisposition.SATURATED
    assert ledger.count() == 0
    assert ledger.tombstone_count() == 3
    reserved, proofs, persisted_limit = ledger.storage_usage()
    assert reserved == 3 * tombstone_bytes
    assert proofs == 3
    assert persisted_limit == storage_limit

    duplicate = ledger.admit(
        _envelope(
            "settled-0",
            trace_id="settled-0-retry",
            route_name="storage-budget-0",
        )
    )
    assert duplicate.disposition is AdmitDisposition.DUPLICATE


def test_one_scope_cannot_consume_the_global_storage_reserve(tmp_path: Path):
    tombstone_bytes = ledger_module._TOMBSTONE_STORAGE_RESERVATION_BYTES
    storage_limit = MINIMUM_MAX_STORAGE_BYTES * 2
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=8,
        max_storage_bytes=storage_limit,
    )

    first = ledger.admit(_envelope("scope-a-1", trace_id="scope-a-1-trace"))
    assert first.authority is not None
    assert ledger.settle_no_effect(first.authority)

    # The first settled carrier compacts, but its permanent proof still owns
    # bytes in scope A. It cannot consume the heavy-carrier reserve.
    scope_full = ledger.admit(_envelope("scope-a-2", trace_id="scope-a-2-trace"))
    assert scope_full.disposition is AdmitDisposition.SATURATED
    assert scope_full.saturation is AdmitSaturationReason.SCOPE_STORAGE_LIMIT
    assert (
        ledger.admit(
            _envelope(
                "scope-b-1",
                trace_id="scope-b-1-trace",
                route_name="storage-budget-b",
            )
        ).disposition
        is AdmitDisposition.ACCEPTED
    )
    assert ledger.storage_usage() == (
        MINIMUM_MAX_STORAGE_BYTES + tombstone_bytes,
        2,
        storage_limit,
    )

    # Exact proof lookup remains available even while both the scope and
    # global admission budgets refuse new unique work.
    replay = ledger.admit(_envelope("scope-a-1", trace_id="scope-a-1-retry"))
    assert replay.disposition is AdmitDisposition.DUPLICATE
    globally_full = ledger.admit(
        _envelope(
            "scope-c-1",
            trace_id="scope-c-1-trace",
            route_name="storage-budget-c",
        )
    )
    assert globally_full.disposition is AdmitDisposition.SATURATED
    assert globally_full.saturation is AdmitSaturationReason.GLOBAL_STORAGE_LIMIT


def test_concurrent_handles_cannot_both_claim_the_last_storage_slot(tmp_path: Path):
    db_path = tmp_path / "state.db"
    first = WebhookOperationLedger(
        db_path,
        max_records=8,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
        instance_id="first",
    )
    second = WebhookOperationLedger(
        db_path,
        max_records=8,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
        instance_id="second",
    )

    def admit(ledger: WebhookOperationLedger, identity: str, route: str):
        return ledger.admit(
            _envelope(identity, trace_id=f"{identity}-trace", route_name=route)
        ).disposition

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(admit, first, "concurrent-a", "concurrent-a"),
            pool.submit(admit, second, "concurrent-b", "concurrent-b"),
        )
    assert sorted(result.result().value for result in futures) == [
        AdmitDisposition.ACCEPTED.value,
        AdmitDisposition.SATURATED.value,
    ]
    assert first.storage_usage() == (
        MINIMUM_MAX_STORAGE_BYTES,
        1,
        MINIMUM_MAX_STORAGE_BYTES,
    )


def test_reserved_carrier_can_reach_full_size_and_settle_at_budget(tmp_path: Path):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=8,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
    )
    admitted = ledger.admit(_envelope("large", trace_id="large-trace"))
    assert admitted.authority is not None

    large_event_value = "e" * (ledger_module._MAX_EVENT_JSON_BYTES - 128)
    prepared = ledger.prepare(
        admitted.authority,
        event_snapshot={"payload": large_event_value},
        target_snapshot={"kind": "log", "profile": "default"},
        grant_snapshot={"mode": "agent", "toolsets": []},
    )
    assert ledger.mark_running(prepared)
    large_delivery = "d" * (ledger_module._MAX_EVENT_JSON_BYTES - 256)
    staged = ledger.stage_delivery(
        prepared,
        content=large_delivery,
        carrier_snapshot={"v": 1, "kind": "agent_final"},
    )
    attempt = ledger.begin_target(staged)
    assert attempt.disposition is TargetAttemptDisposition.STARTED
    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.SUPPRESSED),
    )
    restored = ledger.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.SETTLED
    assert ledger.storage_usage() == (
        MINIMUM_MAX_STORAGE_BYTES,
        1,
        MINIMUM_MAX_STORAGE_BYTES,
    )


def test_persisted_limit_rejects_mismatched_process_configuration(tmp_path: Path):
    db_path = tmp_path / "state.db"
    higher_limit = MINIMUM_MAX_STORAGE_BYTES * 2
    first = WebhookOperationLedger(db_path, max_storage_bytes=higher_limit)
    with pytest.raises(WebhookLedgerError, match="limits do not match"):
        WebhookOperationLedger(
            db_path,
            max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
        )
    second = WebhookOperationLedger(db_path, max_storage_bytes=higher_limit)
    assert first.storage_usage()[2] == higher_limit
    assert second.storage_usage()[2] == higher_limit

    assert (
        first.admit(_envelope("one", trace_id="one-trace")).disposition
        is AdmitDisposition.ACCEPTED
    )
    assert (
        first.admit(
            _envelope("two", trace_id="two-trace", route_name="other-scope")
        ).disposition
        is AdmitDisposition.ACCEPTED
    )
    assert (
        first.admit(
            _envelope("three", trace_id="three-trace", route_name="third-scope")
        ).disposition
        is AdmitDisposition.SATURATED
    )


def test_persisted_limit_cannot_be_silently_widened_by_a_later_handle(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(
        db_path,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
    )
    with pytest.raises(WebhookLedgerError, match="limits do not match"):
        WebhookOperationLedger(
            db_path,
            max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES * 2,
        )
    assert ledger.storage_usage()[2] == MINIMUM_MAX_STORAGE_BYTES


@pytest.mark.parametrize(("first_limit", "second_limit"), [(8, 7), (7, 8)])
def test_persisted_record_limit_rejects_mismatched_handles(
    tmp_path: Path,
    first_limit: int,
    second_limit: int,
):
    db_path = tmp_path / "state.db"
    WebhookOperationLedger(db_path, max_records=first_limit)

    with pytest.raises(WebhookLedgerError, match="limits do not match"):
        WebhookOperationLedger(db_path, max_records=second_limit)

    with sqlite3.connect(db_path) as conn:
        persisted = conn.execute(
            """SELECT max_records FROM webhook_ledger_usage
                WHERE schema_name='webhook_operation_ledger'"""
        ).fetchone()[0]
    assert persisted == first_limit


def test_auth_binding_handle_adopts_root_limits_across_named_profile_quotas(
    tmp_path: Path,
):
    root_db = tmp_path / "root" / "state.db"
    profile_db = tmp_path / "root" / "profiles" / "alpha" / "state.db"
    root_ledger = WebhookOperationLedger(
        root_db,
        max_records=4096,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES * 4,
    )
    profile_ledger = WebhookOperationLedger(
        profile_db,
        max_records=8,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES * 2,
    )
    assert profile_ledger.max_records == 8

    auth_ledger = WebhookOperationLedger.for_authentication_bindings(root_db)
    assert auth_ledger.max_records == root_ledger.max_records == 4096
    assert (
        auth_ledger.max_storage_bytes
        == root_ledger.max_storage_bytes
        == MINIMUM_MAX_STORAGE_BYTES * 4
    )
    binding = (
        hashlib.sha256(b"named-profile-key").hexdigest(),
        "alpha",
        "events",
        "generic",
        "generic_v2",
        hashlib.sha256(b"named-profile-policy").hexdigest(),
    )
    auth_ledger.bind_authentication_keys([binding])

    reopened_root = WebhookOperationLedger(
        root_db,
        max_records=4096,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES * 4,
    )
    reopened_root.bind_authentication_keys([binding])
    with sqlite3.connect(root_db) as conn:
        persisted_limits = conn.execute(
            """SELECT max_records, max_storage_bytes
                 FROM webhook_ledger_usage"""
        ).fetchone()
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM webhook_auth_key_bindings"
        ).fetchone()[0]
    assert persisted_limits == (
        4096,
        MINIMUM_MAX_STORAGE_BYTES * 4,
    )
    assert binding_count == 1


def test_alpha_first_auth_binding_bootstrap_allows_default_custom_quotas(
    tmp_path: Path,
):
    root_db = tmp_path / "root" / "state.db"
    auth_ledger = WebhookOperationLedger.for_authentication_bindings(root_db)
    binding = (
        hashlib.sha256(b"alpha-first-key").hexdigest(),
        "alpha",
        "events",
        "generic",
        "generic_v2",
        hashlib.sha256(b"alpha-first-policy").hexdigest(),
    )
    auth_ledger.bind_authentication_keys([binding])

    custom_records = 17
    custom_storage = MINIMUM_MAX_STORAGE_BYTES * 3
    default_ledger = WebhookOperationLedger(
        root_db,
        max_records=custom_records,
        max_storage_bytes=custom_storage,
    )
    assert default_ledger.max_records == custom_records
    assert default_ledger.max_storage_bytes == custom_storage
    default_ledger.bind_authentication_keys([binding])

    reopened_auth = WebhookOperationLedger.for_authentication_bindings(root_db)
    assert reopened_auth.max_records == custom_records
    assert reopened_auth.max_storage_bytes == custom_storage
    with sqlite3.connect(root_db) as conn:
        usage = conn.execute(
            """SELECT max_records, max_storage_bytes,
                      operation_limits_provisional, proof_count,
                      auth_binding_count
                 FROM webhook_ledger_usage"""
        ).fetchone()
    assert usage == (custom_records, custom_storage, 0, 0, 1)


def test_provisional_limits_never_rewrite_existing_operation_evidence(
    tmp_path: Path,
):
    root_db = tmp_path / "root" / "state.db"
    auth_ledger = WebhookOperationLedger.for_authentication_bindings(root_db)
    admitted = auth_ledger.admit(
        _envelope("provisional-proof", trace_id="provisional-proof-trace")
    )
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None

    with pytest.raises(
        WebhookLedgerError,
        match="provisional webhook operation limits cannot change",
    ):
        WebhookOperationLedger(
            root_db,
            max_records=17,
            max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES * 3,
        )

    reopened = WebhookOperationLedger.for_authentication_bindings(root_db)
    assert reopened.max_records == ledger_module.DEFAULT_MAX_RECORDS
    assert reopened.max_storage_bytes == ledger_module.DEFAULT_MAX_STORAGE_BYTES
    assert reopened.lookup_session(admitted.authority.session_key) is not None
    with sqlite3.connect(root_db) as conn:
        usage = conn.execute(
            """SELECT max_records, max_storage_bytes,
                      operation_limits_provisional, proof_count
                 FROM webhook_ledger_usage"""
        ).fetchone()
    assert usage == (
        ledger_module.DEFAULT_MAX_RECORDS,
        ledger_module.DEFAULT_MAX_STORAGE_BYTES,
        1,
        1,
    )


def test_limit_cannot_be_lowered_below_reserved_evidence(tmp_path: Path):
    db_path = tmp_path / "state.db"
    higher_limit = MINIMUM_MAX_STORAGE_BYTES * 2
    ledger = WebhookOperationLedger(db_path, max_storage_bytes=higher_limit)
    for index in range(2):
        admitted = ledger.admit(
            _envelope(
                f"live-{index}",
                trace_id=f"live-{index}-trace",
                route_name=f"live-scope-{index}",
            )
        )
        assert admitted.disposition is AdmitDisposition.ACCEPTED

    with pytest.raises(WebhookLedgerError, match="below reserved evidence"):
        WebhookOperationLedger(
            db_path,
            max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
        )


def test_authentication_binding_policy_is_permanent_across_reopen(tmp_path: Path):
    db_path = tmp_path / "state.db"
    fingerprint = hashlib.sha256(b"stable-key").hexdigest()
    first_policy = hashlib.sha256(b"policy-one").hexdigest()
    second_policy = hashlib.sha256(b"policy-two").hexdigest()
    binding = (
        fingerprint,
        "default",
        "events",
        "generic",
        "generic_v2",
        first_policy,
    )
    ledger = WebhookOperationLedger(db_path)
    ledger.bind_authentication_keys([binding])
    ledger.bind_authentication_keys([binding])

    reopened = WebhookOperationLedger(db_path)
    reopened.bind_authentication_keys([binding])
    with pytest.raises(
        WebhookLedgerTransitionError,
        match="permanently bound to another route policy authority",
    ):
        reopened.bind_authentication_keys([
            (*binding[:-1], second_policy),
        ])

    with sqlite3.connect(db_path) as conn:
        persisted = conn.execute(
            """SELECT profile, route, provider, signature_mode, policy_sha256
                 FROM webhook_auth_key_bindings
                WHERE key_fingerprint=?""",
            (fingerprint,),
        ).fetchall()
        usage = conn.execute(
            """SELECT reserved_bytes, proof_count,
                      auth_binding_reserved_bytes, auth_binding_count
                 FROM webhook_ledger_usage
                WHERE schema_name='webhook_operation_ledger'"""
        ).fetchone()
    assert persisted == [binding[1:]]
    assert usage == (
        0,
        0,
        ledger_module._AUTH_BINDING_STORAGE_RESERVATION_BYTES,
        1,
    )


def test_authentication_bindings_are_globally_budgeted_and_atomic(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(
        db_path,
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
    )
    capacity = (
        ledger_module._AUTH_BINDING_GLOBAL_LIMIT_BYTES
        // ledger_module._AUTH_BINDING_STORAGE_RESERVATION_BYTES
    )
    policy_sha256 = hashlib.sha256(b"stable-policy").hexdigest()
    bindings = [
        (
            hashlib.sha256(f"key-{index}".encode()).hexdigest(),
            "default",
            f"route-{index}",
            "generic",
            "generic_v2",
            policy_sha256,
        )
        for index in range(capacity)
    ]
    overflow = (
        hashlib.sha256(b"overflow-key").hexdigest(),
        "default",
        "overflow",
        "generic",
        "generic_v2",
        policy_sha256,
    )
    ledger.bind_authentication_keys(bindings[:-1])
    with pytest.raises(
        WebhookLedgerCapacityError,
        match="authentication binding capacity exhausted",
    ):
        ledger.bind_authentication_keys([bindings[-1], overflow])
    with sqlite3.connect(db_path) as conn:
        rolled_back_count = conn.execute(
            "SELECT COUNT(*) FROM webhook_auth_key_bindings"
        ).fetchone()[0]
    assert rolled_back_count == capacity - 1

    ledger.bind_authentication_keys([bindings[-1]])
    ledger.bind_authentication_keys([bindings[0]])
    with pytest.raises(
        WebhookLedgerCapacityError,
        match="authentication binding capacity exhausted",
    ):
        ledger.bind_authentication_keys([overflow])

    with sqlite3.connect(db_path) as conn:
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM webhook_auth_key_bindings"
        ).fetchone()[0]
        usage = conn.execute(
            """SELECT reserved_bytes, proof_count,
                      auth_binding_reserved_bytes, auth_binding_count
                 FROM webhook_ledger_usage
                WHERE schema_name='webhook_operation_ledger'"""
        ).fetchone()
    assert binding_count == capacity
    assert usage == (
        0,
        0,
        ledger_module._AUTH_BINDING_GLOBAL_LIMIT_BYTES,
        capacity,
    )
    assert ledger.has_global_admission_capacity()


def test_authentication_binding_counter_corruption_fails_closed_on_reopen(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    ledger.bind_authentication_keys([
        (
            hashlib.sha256(b"counter-key").hexdigest(),
            "default",
            "counter-route",
            "generic",
            "generic_v2",
            hashlib.sha256(b"counter-policy").hexdigest(),
        )
    ])
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            """UPDATE webhook_ledger_usage SET auth_binding_count=0
                WHERE schema_name='webhook_operation_ledger'"""
        )
        conn.execute("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_authentication_binding_content_corruption_fails_closed_on_reopen(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    ledger.bind_authentication_keys([
        (
            hashlib.sha256(b"timestamp-key").hexdigest(),
            "default",
            "timestamp-route",
            "generic",
            "generic_v2",
            hashlib.sha256(b"timestamp-policy").hexdigest(),
        )
    ])
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_webhook_auth_bindings_immutable_update")
        conn.execute("UPDATE webhook_auth_key_bindings SET bound_at='NaN'")

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="authentication key binding is invalid",
    ):
        WebhookOperationLedger(db_path)


def test_authentication_key_match_is_bounded_indexed_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    policy_sha256 = hashlib.sha256(b"match-policy").hexdigest()
    bindings = [
        (
            hashlib.sha256(f"match-key-{index}".encode()).hexdigest(),
            "default",
            "match-route",
            "generic",
            "generic_v2",
            policy_sha256,
        )
        for index in range(2)
    ]
    ledger.bind_authentication_keys(bindings)

    statements: list[str] = []
    original_connect = ledger._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(ledger, "_connect", traced_connect)
    assert ledger.authentication_keys_match(bindings)
    assert not ledger.authentication_keys_match([
        (
            *bindings[0][:-1],
            hashlib.sha256(b"other-policy").hexdigest(),
        )
    ])
    assert not ledger.authentication_keys_match([
        (
            hashlib.sha256(b"missing-key").hexdigest(),
            *bindings[0][1:],
        )
    ])
    with pytest.raises(
        WebhookLedgerError,
        match="exceeds its fingerprint bound",
    ):
        ledger.authentication_keys_match(bindings * 5)

    binding_reads = [
        statement.lower()
        for statement in statements
        if statement.lstrip().lower().startswith("select")
        and "from webhook_auth_key_bindings" in statement.lower()
    ]
    assert binding_reads
    assert all("where key_fingerprint=" in statement for statement in binding_reads)
    assert all("count(" not in statement for statement in binding_reads)
    assert all("sum(" not in statement for statement in binding_reads)
    with sqlite3.connect(ledger.db_path) as conn:
        plan = conn.execute(
            """EXPLAIN QUERY PLAN
                SELECT profile, route, provider, signature_mode, policy_sha256
                  FROM webhook_auth_key_bindings
                 WHERE key_fingerprint=?""",
            (bindings[0][0],),
        ).fetchall()
    assert any(
        "USING INDEX sqlite_autoindex_webhook_auth_key_bindings_1" in row[3]
        for row in plan
    )
    assert not any("SCAN webhook_auth_key_bindings" in row[3] for row in plan)


def test_authentication_key_match_detects_root_database_loss_and_replacement(
    tmp_path: Path,
):
    db_path = tmp_path / "root" / "state.db"
    ledger = WebhookOperationLedger(db_path)
    fingerprint = hashlib.sha256(b"cached-alpha-key").hexdigest()
    policy_sha256 = hashlib.sha256(b"cached-alpha-policy").hexdigest()
    alpha_binding = (
        fingerprint,
        "alpha",
        "events",
        "generic",
        "generic_v2",
        policy_sha256,
    )
    ledger.bind_authentication_keys([alpha_binding])
    assert ledger.authentication_keys_match([alpha_binding])

    lost_backup = tmp_path / "lost-root-state.db"
    db_path.replace(lost_backup)
    with pytest.raises(WebhookLedgerError):
        ledger.authentication_keys_match([alpha_binding])
    lost_backup.replace(db_path)
    assert ledger.authentication_keys_match([alpha_binding])

    replacement_path = tmp_path / "replacement-state.db"
    replacement = WebhookOperationLedger(replacement_path)
    beta_binding = (
        fingerprint,
        "beta",
        "events",
        "generic",
        "generic_v2",
        hashlib.sha256(b"replacement-beta-policy").hexdigest(),
    )
    replacement.bind_authentication_keys([beta_binding])
    with sqlite3.connect(replacement_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    displaced_path = tmp_path / "displaced-alpha-state.db"
    db_path.replace(displaced_path)
    replacement_path.replace(db_path)

    assert not ledger.authentication_keys_match([alpha_binding])
    assert ledger.authentication_keys_match([beta_binding])


def test_one_route_cannot_consume_shared_capacity_with_key_rotations(
    tmp_path: Path,
):
    storage_limit = MINIMUM_MAX_STORAGE_BYTES * 2
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_storage_bytes=storage_limit,
    )
    scope_capacity = (
        ledger_module._AUTH_BINDING_SCOPE_LIMIT_BYTES
        // ledger_module._AUTH_BINDING_STORAGE_RESERVATION_BYTES
    )
    policy_sha256 = hashlib.sha256(b"rotating-policy").hexdigest()
    rotations = [
        (
            hashlib.sha256(f"rotation-{index}".encode()).hexdigest(),
            "default",
            "rotating-route",
            "generic",
            "generic_v2",
            policy_sha256,
        )
        for index in range(scope_capacity + 1)
    ]
    ledger.bind_authentication_keys(rotations[:-1])
    ledger.bind_authentication_keys([rotations[0]])

    with pytest.raises(
        WebhookLedgerCapacityError,
        match="route-scope capacity exhausted",
    ):
        ledger.bind_authentication_keys([rotations[-1]])
    assert ledger.has_global_admission_capacity()
    reserved_operation = ledger.admit(
        _envelope(
            "reserved-operation",
            trace_id="reserved-operation-trace",
            route_name="other-route",
        )
    )
    assert reserved_operation.disposition is AdmitDisposition.ACCEPTED
    assert ledger.has_global_admission_capacity()
    second_operation = ledger.admit(
        _envelope(
            "second-reserved-operation",
            trace_id="second-reserved-operation-trace",
            route_name="another-route",
        )
    )
    assert second_operation.disposition is AdmitDisposition.ACCEPTED
    assert not ledger.has_global_admission_capacity()


def test_full_operation_budget_cannot_strand_authentication_rotation(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
    )
    admitted = ledger.admit(
        _envelope("fills-operation-budget", trace_id="full-budget-trace")
    )
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert not ledger.has_global_admission_capacity()

    policy_sha256 = hashlib.sha256(b"full-budget-policy").hexdigest()
    old_binding = (
        hashlib.sha256(b"old-key").hexdigest(),
        "default",
        "rotating-route",
        "generic",
        "generic_v2",
        policy_sha256,
    )
    replacement_binding = (
        hashlib.sha256(b"replacement-key").hexdigest(),
        *old_binding[1:],
    )
    ledger.bind_authentication_keys([old_binding])
    ledger.bind_authentication_keys([replacement_binding])
    ledger.bind_authentication_keys([replacement_binding])

    with sqlite3.connect(ledger.db_path) as conn:
        usage = conn.execute(
            """SELECT reserved_bytes, auth_binding_reserved_bytes,
                      auth_binding_count
                 FROM webhook_ledger_usage"""
        ).fetchone()
    assert usage == (
        MINIMUM_MAX_STORAGE_BYTES,
        2 * ledger_module._AUTH_BINDING_STORAGE_RESERVATION_BYTES,
        2,
    )


def test_populated_v4_ledger_migrates_transactionally_to_v5(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path)
    with sqlite3.connect(db_path) as conn:
        before_rows = (
            conn.execute(
                "SELECT * FROM webhook_operations ORDER BY operation_id"
            ).fetchall(),
            conn.execute(
                "SELECT * FROM webhook_targets ORDER BY operation_id, target_id"
            ).fetchall(),
            conn.execute(
                """SELECT * FROM webhook_delivery_tombstones
                    ORDER BY profile, route, provider, replay_id"""
            ).fetchall(),
        )

    ledger = WebhookOperationLedger(db_path)

    restored = ledger.lookup_session("agent:main:webhook:v4")
    assert restored is not None
    assert restored.operation_id == "v4-operation"
    assert restored.state is OperationState.READY
    assert restored.target_state is not None
    assert ledger.count() == 1
    assert ledger.tombstone_count() == 1
    assert ledger.storage_usage() == (
        ledger_module._OPERATION_STORAGE_RESERVATION_BYTES
        + ledger_module._TOMBSTONE_STORAGE_RESERVATION_BYTES,
        2,
        ledger_module.DEFAULT_MAX_STORAGE_BYTES,
    )
    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            """SELECT schema_version FROM webhook_ledger_meta
                WHERE schema_name='webhook_operation_ledger'"""
        ).fetchone()[0]
        target_pk = [
            row[5] for row in conn.execute("PRAGMA table_info(webhook_targets)")
        ]
        usage = conn.execute(
            """SELECT active_record_count, settled_operation_count,
                      indeterminate_operation_count, auth_binding_count
                 FROM webhook_ledger_usage"""
        ).fetchone()
        parked_tables = conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE '%_v4'"""
        ).fetchall()
        after_rows = (
            conn.execute(
                "SELECT * FROM webhook_operations ORDER BY operation_id"
            ).fetchall(),
            conn.execute(
                "SELECT * FROM webhook_targets ORDER BY operation_id, target_id"
            ).fetchall(),
            conn.execute(
                """SELECT * FROM webhook_delivery_tombstones
                    ORDER BY profile, route, provider, replay_id"""
            ).fetchall(),
        )
    assert version == 5
    assert target_pk == [1] + [0] * 14
    assert usage == (1, 0, 0, 0)
    assert parked_tables == []
    assert after_rows == before_rows


def test_v4_migration_validates_large_operations_in_bounded_batches(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path)
    operation_count = 2 * ledger_module._V4_MIGRATION_VALIDATION_BATCH_SIZE + 2
    large_event_json = json.dumps(
        {
            "padding": "x" * (64 * 1024),
            "payload": {"v": 4},
            "text": "v4",
            "v": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_operations SET event_json=? WHERE operation_id=?",
            (large_event_json, "v4-operation"),
        )
        for index in range(1, operation_count):
            operation_id = f"v4-operation-{index:03d}"
            conn.execute(
                """INSERT INTO webhook_operations (
                       operation_id, profile, route, provider, replay_id,
                       body_sha256, event_type, session_key, state, generation,
                       owner_pid, owner_started_at, owner_instance, event_json,
                       target_json, grant_json, script_started, created_at,
                       updated_at, settled_at, last_error
                   )
                   SELECT ?, profile, route, provider, ?, body_sha256,
                          event_type, ?, state, generation, owner_pid,
                          owner_started_at, owner_instance, event_json,
                          target_json, grant_json, script_started, created_at,
                          updated_at, settled_at, last_error
                     FROM webhook_operations
                    WHERE operation_id='v4-operation'""",
                (
                    operation_id,
                    f"provider_id:v4-delivery-{index:03d}",
                    f"agent:main:webhook:v4:{index:03d}",
                ),
            )
            conn.execute(
                """INSERT INTO webhook_targets (
                       operation_id, target_id, state, attempt_token,
                       content_sha256, delivery_json, delivery_sha256,
                       external_id, owner_pid, owner_started_at, owner_instance,
                       started_at, settled_at, updated_at, last_error
                   )
                   SELECT ?, target_id, state, attempt_token, content_sha256,
                          delivery_json, delivery_sha256, external_id, owner_pid,
                          owner_started_at, owner_instance, started_at,
                          settled_at, updated_at, last_error
                     FROM webhook_targets
                    WHERE operation_id='v4-operation'""",
                (operation_id,),
            )

    fetch_sizes: list[int] = []

    class TrackingCursor(sqlite3.Cursor):
        _operation_scan = False

        def execute(self, sql, parameters=()):
            self._operation_scan = (
                "SELECT * FROM webhook_operations ORDER BY operation_id"
                in " ".join(str(sql).split())
            )
            return super().execute(sql, parameters)

        def fetchmany(self, size=None):
            if self._operation_scan:
                fetch_sizes.append(size)
            return super().fetchmany(size)

        def fetchall(self):
            if self._operation_scan:
                raise AssertionError("v4 operation validation must not fetch all rows")
            return super().fetchall()

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=TrackingCursor).execute(sql, parameters)

    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", tracking_connect)

    ledger = WebhookOperationLedger(db_path)

    assert ledger.count() == operation_count
    assert len(fetch_sizes) >= 3
    assert set(fetch_sizes) == {ledger_module._V4_MIGRATION_VALIDATION_BATCH_SIZE}


@pytest.mark.parametrize(
    "ambiguous_table",
    ["webhook_operations", "webhook_delivery_tombstones"],
)
def test_v4_default_profile_ambiguity_fails_closed_and_rolls_back(
    tmp_path: Path,
    ambiguous_table: str,
):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path, profile="alpha")
    with sqlite3.connect(db_path) as conn:
        if ambiguous_table == "webhook_operations":
            conn.execute("UPDATE webhook_operations SET profile='default'")
        else:
            conn.execute("UPDATE webhook_delivery_tombstones SET profile='default'")
    with sqlite3.connect(db_path) as conn:
        before = tuple(conn.iterdump())

    with pytest.raises(
        WebhookLedgerConfigurationError,
        match="physical authority profile is ambiguous",
    ):
        WebhookOperationLedger(db_path)

    with sqlite3.connect(db_path) as conn:
        after = tuple(conn.iterdump())
        version = conn.execute(
            "SELECT schema_version FROM webhook_ledger_meta"
        ).fetchone()
        v5_usage = conn.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='webhook_ledger_usage'"""
        ).fetchone()
    assert after == before
    assert version == (4,)
    assert v5_usage is None


def test_concurrent_constructors_migrate_v4_once_and_both_open_v5(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path)

    def open_ledger(instance_id: str) -> tuple[int, int]:
        ledger = WebhookOperationLedger(db_path, instance_id=instance_id)
        return ledger.count(), ledger.tombstone_count()

    with ThreadPoolExecutor(max_workers=2) as pool:
        opened = list(pool.map(open_ledger, ("migrator-a", "migrator-b")))

    assert opened == [(1, 1), (1, 1)]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM webhook_ledger_meta"
        ).fetchone() == (5,)
        assert conn.execute(
            """SELECT active_record_count, settled_operation_count,
                      indeterminate_operation_count, proof_count
                 FROM webhook_ledger_usage"""
        ).fetchone() == (1, 0, 0, 2)
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
                WHERE type='table' AND name LIKE '%_v4'"""
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("fixture_kwargs", "error_match"),
    [
        ({"drop_replay_index": True}, "v4 ledger indexes or triggers"),
        ({"extra_trigger": True}, "v4 ledger indexes or triggers"),
        ({"weakened_operation_schema": True}, "v4 ledger table definitions"),
        ({"corrupt_state": True}, "invalid durable authority"),
        ({"invalid_event_json": True}, "stored event snapshot is invalid JSON"),
        ({"corrupt_body_digest": True}, "stored operation body_sha256 is invalid"),
        ({"invalid_created_at": True}, "stored operation created_at is invalid"),
        ({"invalid_expiry": True}, "invalid durable authority"),
    ],
)
def test_v4_migration_corruption_rolls_back_original_ledger(
    tmp_path: Path,
    fixture_kwargs,
    error_match,
):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path, **fixture_kwargs)

    with pytest.raises(WebhookLedgerError, match=error_match):
        WebhookOperationLedger(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT schema_version FROM webhook_ledger_meta"
        ).fetchone()[0]
        operation = conn.execute(
            """SELECT operation_id, state FROM webhook_operations
                WHERE operation_id='v4-operation'"""
        ).fetchone()
        target_count = conn.execute("SELECT COUNT(*) FROM webhook_targets").fetchone()[
            0
        ]
        v5_usage_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='webhook_ledger_usage'"""
        ).fetchone()
    assert version == 4
    assert operation == (
        "v4-operation",
        "corrupt" if fixture_kwargs.get("corrupt_state") else "ready",
    )
    assert target_count == 1
    assert v5_usage_exists is None


def test_over_capacity_v4_migration_is_actionable_and_rolls_back(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _create_populated_v4_ledger(db_path)

    with pytest.raises(
        WebhookLedgerCapacityError,
        match=(
            "v4 migration exceeds the configured storage capacity; "
            "increase idempotency_max_storage_bytes"
        ),
    ):
        WebhookOperationLedger(
            db_path,
            max_storage_bytes=MINIMUM_MAX_STORAGE_BYTES,
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM webhook_ledger_meta"
        ).fetchone() == (4,)
        assert conn.execute("SELECT COUNT(*) FROM webhook_operations").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM webhook_delivery_tombstones"
        ).fetchone() == (1,)
        assert (
            conn.execute(
                """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='webhook_ledger_usage'"""
            ).fetchone()
            is None
        )


def test_usage_counter_corruption_fails_closed_on_reopen(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    assert (
        ledger.admit(_envelope("proof", trace_id="proof-trace")).disposition
        is AdmitDisposition.ACCEPTED
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_ledger_usage SET reserved_bytes=0
               WHERE schema_name='webhook_operation_ledger'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_scope_usage_counter_corruption_fails_closed_on_reopen(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    assert (
        ledger.admit(_envelope("scope-proof", trace_id="scope-proof-trace")).disposition
        is AdmitDisposition.ACCEPTED
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_ledger_scope_usage
                  SET reserved_bytes=reserved_bytes+1
                WHERE profile='default' AND route='storage-budget'
                  AND provider='svix'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="scope usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_scope_active_counter_corruption_fails_closed_on_reopen(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    assert (
        ledger.admit(
            _envelope("scope-active", trace_id="scope-active-trace")
        ).disposition
        is AdmitDisposition.ACCEPTED
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_ledger_scope_usage
                  SET active_record_count=0
                WHERE profile='default' AND route='storage-budget'
                  AND provider='svix'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="scope usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_state_class_counters_follow_transitions_deletes_and_compaction(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path, terminal_retention_seconds=1)

    def state_counts() -> tuple[int, int, int]:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT active_record_count, settled_operation_count,
                          indeterminate_operation_count
                     FROM webhook_ledger_usage
                    WHERE schema_name='webhook_operation_ledger'"""
            ).fetchone()
        assert row is not None
        return tuple(int(value) for value in row)

    settled = ledger.admit(_envelope("settled", trace_id="settled-trace"))
    assert settled.authority is not None
    assert state_counts() == (1, 0, 0)
    prepared = ledger.prepare(
        settled.authority,
        event_snapshot={"v": 1, "payload": {}},
        target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        grant_snapshot={"v": 1, "toolsets": []},
    )
    assert state_counts() == (1, 0, 0)
    assert ledger.settle_no_effect(prepared)
    assert state_counts() == (0, 1, 0)

    unknown = ledger.admit(
        _envelope(
            "unknown",
            trace_id="unknown-trace",
            route_name="unknown-scope",
        )
    )
    assert unknown.authority is not None
    assert state_counts() == (1, 1, 0)
    assert ledger.mark_indeterminate(unknown.authority, "unknown outcome")
    assert state_counts() == (0, 1, 1)

    released = ledger.admit(
        _envelope(
            "released",
            trace_id="released-trace",
            route_name="released-scope",
        )
    )
    assert released.authority is not None
    assert state_counts() == (1, 1, 1)
    assert ledger.release_pre_effect(released.authority)
    assert state_counts() == (0, 1, 1)

    assert ledger.prune(now=ledger_module.time.time() + 2) == 1
    assert state_counts() == (0, 0, 1)
    assert ledger.count() == 1
    assert ledger.tombstone_count() == 1


def test_state_counter_corruption_fails_closed_on_reopen(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    admitted = ledger.admit(_envelope("counter", trace_id="counter-trace"))
    assert admitted.authority is not None

    # Preserve the table-level total while assigning the proof to the wrong
    # state class, so startup validation must compare counters to durable rows.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_ledger_usage
                  SET active_record_count=0,
                      indeterminate_operation_count=1
                WHERE schema_name='webhook_operation_ledger'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_missing_state_transition_trigger_is_detected_after_transition(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    admitted = ledger.admit(_envelope("trigger", trace_id="trigger-trace"))
    assert admitted.authority is not None
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_webhook_operations_usage_state")

    assert ledger.settle_no_effect(admitted.authority)
    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="usage counter is inconsistent",
    ):
        WebhookOperationLedger(db_path)


def test_bounded_settlement_expiry_index_corruption_fails_closed_on_reopen(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    WebhookOperationLedger(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_webhook_operations_bounded_settled_expiry")
        conn.execute(
            """CREATE INDEX idx_webhook_operations_bounded_settled_expiry
                   ON webhook_operations(updated_at)
                 WHERE state='settled'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="bounded-settlement expiry index is unavailable",
    ):
        WebhookOperationLedger(db_path)


def test_global_capacity_readiness_uses_counters_and_bounded_indexed_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    statements: list[str] = []
    original_connect = ledger._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(ledger, "_connect", traced_connect)
    assert ledger.has_global_admission_capacity()

    proof_reads = [
        statement.lower()
        for statement in statements
        if statement.lstrip().lower().startswith("select")
        and (
            "from webhook_operations" in statement.lower()
            or "from webhook_delivery_tombstones" in statement.lower()
        )
    ]
    assert len(proof_reads) == 2
    assert all("indexed by" in statement for statement in proof_reads)
    assert all(
        f"limit {ledger_module._MAX_PRUNE_BATCH}" in statement
        for statement in proof_reads
    )
    assert all("count(" not in statement for statement in proof_reads)
    assert all("sum(" not in statement for statement in proof_reads)

    prefix = ledger_module._BOUNDED_REPLAY_PREFIXES[0]
    with sqlite3.connect(ledger.db_path) as conn:
        operation_plan = conn.execute(
            f"""EXPLAIN QUERY PLAN
                SELECT operation_id
                  FROM webhook_operations INDEXED BY
                       idx_webhook_operations_bounded_settled_expiry
                 WHERE state='settled'
                   AND substr(replay_id, 1, {len(prefix)})='{prefix}'
                   AND COALESCE(settled_at, updated_at) <= ?
                 ORDER BY COALESCE(settled_at, updated_at)
                 LIMIT ?""",
            (0.0, ledger_module._MAX_PRUNE_BATCH),
        ).fetchall()
        tombstone_plan = conn.execute(
            """EXPLAIN QUERY PLAN
                SELECT rowid
                  FROM webhook_delivery_tombstones INDEXED BY
                       idx_webhook_tombstones_expires_at
                 WHERE expires_at IS NOT NULL AND expires_at <= ?
                 ORDER BY expires_at, rowid
                 LIMIT ?""",
            (0.0, ledger_module._MAX_PRUNE_BATCH),
        ).fetchall()
    assert any(
        "USING INDEX idx_webhook_operations_bounded_settled_expiry" in row[3]
        for row in operation_plan
    )
    assert not any("SCAN webhook_operations" in row[3] for row in operation_plan)
    assert any(
        "USING COVERING INDEX idx_webhook_tombstones_expires_at" in row[3]
        for row in tombstone_plan
    )
    assert not any(
        "SCAN webhook_delivery_tombstones" in row[3] for row in tombstone_plan
    )


def test_populated_admission_uses_persisted_global_and_scope_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=128,
    )
    for index in range(30):
        admitted = ledger.admit(
            _envelope(
                f"populated-{index}",
                trace_id=f"populated-trace-{index}",
                route_name=f"route-{index % 10}",
            )
        )
        assert admitted.authority is not None
        if index % 3 == 0:
            assert ledger.settle_no_effect(admitted.authority)
        elif index % 3 == 1:
            assert ledger.mark_indeterminate(
                admitted.authority,
                "mixed-state capacity proof",
            )

    statements: list[str] = []
    original_connect = ledger._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(ledger, "_connect", traced_connect)
    admitted = ledger.admit(
        _envelope(
            "counter-admission",
            trace_id="counter-admission-trace",
            route_name="fresh-counter-scope",
        )
    )
    assert admitted.disposition is AdmitDisposition.ACCEPTED

    selects = [
        statement.lower()
        for statement in statements
        if statement.lstrip().lower().startswith("select")
    ]
    assert not any("count(" in statement for statement in selects)
    assert not any("sum(" in statement for statement in selects)
    assert any("from webhook_ledger_usage" in statement for statement in selects)
    assert any("from webhook_ledger_scope_usage" in statement for statement in selects)
    settled_probes = [
        statement
        for statement in selects
        if "from webhook_operations" in statement and "state='settled'" in statement
    ]
    assert settled_probes
    assert all("indexed by" in statement for statement in settled_probes)
    assert all("limit" in statement for statement in settled_probes)

    with sqlite3.connect(ledger.db_path) as conn:
        global_plan = conn.execute(
            """EXPLAIN QUERY PLAN
                SELECT * FROM webhook_operations INDEXED BY
                       idx_webhook_operations_state_updated
                 WHERE state='settled'
                 ORDER BY updated_at
                 LIMIT ?""",
            (ledger_module._MAX_PRUNE_BATCH,),
        ).fetchall()
        scope_plan = conn.execute(
            """EXPLAIN QUERY PLAN
                SELECT * FROM webhook_operations INDEXED BY
                       idx_webhook_operations_scope_state_updated
                 WHERE state='settled' AND profile=? AND route=? AND provider=?
                 ORDER BY updated_at
                 LIMIT ?""",
            (
                "default",
                "route-0",
                "svix",
                ledger_module._MAX_PRUNE_BATCH,
            ),
        ).fetchall()
    assert any(
        "USING INDEX idx_webhook_operations_state_updated" in row[3]
        for row in global_plan
    )
    assert not any("SCAN webhook_operations" in row[3] for row in global_plan)
    assert any(
        "USING INDEX idx_webhook_operations_scope_state_updated" in row[3]
        for row in scope_plan
    )
    assert not any("SCAN webhook_operations" in row[3] for row in scope_plan)


def test_mixed_states_keep_readiness_and_real_global_admission_in_lockstep(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=3,
    )
    active = ledger.admit(
        _envelope("active", trace_id="active-trace", route_name="active-route")
    )
    settled = ledger.admit(
        _envelope(
            "settled-mixed",
            trace_id="settled-mixed-trace",
            route_name="settled-route",
        )
    )
    indeterminate = ledger.admit(
        _envelope(
            "indeterminate-mixed",
            trace_id="indeterminate-mixed-trace",
            route_name="indeterminate-route",
        )
    )
    assert active.authority is not None
    assert settled.authority is not None
    assert indeterminate.authority is not None
    assert ledger.settle_no_effect(settled.authority)
    assert ledger.mark_indeterminate(
        indeterminate.authority,
        "mixed-state unknown outcome",
    )

    assert ledger.has_global_admission_capacity()
    second_active = ledger.admit(
        _envelope(
            "second-active",
            trace_id="second-active-trace",
            route_name="second-active-route",
        )
    )
    assert second_active.disposition is AdmitDisposition.ACCEPTED
    assert ledger.has_global_admission_capacity()
    third_active = ledger.admit(
        _envelope(
            "third-active",
            trace_id="third-active-trace",
            route_name="third-active-route",
        )
    )
    assert third_active.disposition is AdmitDisposition.ACCEPTED

    assert not ledger.has_global_admission_capacity()
    full = ledger.admit(
        _envelope(
            "globally-full",
            trace_id="globally-full-trace",
            route_name="globally-full-route",
        )
    )
    assert full.disposition is AdmitDisposition.SATURATED
    assert full.saturation is AdmitSaturationReason.GLOBAL_RECORD_LIMIT


def test_max_record_one_repeatedly_compacts_one_settlement_before_admission(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=1,
    )
    for index in range(12):
        assert ledger.has_global_admission_capacity()
        admitted = ledger.admit(
            _envelope(
                f"record-edge-{index}",
                trace_id=f"record-edge-trace-{index}",
            )
        )
        assert admitted.disposition is AdmitDisposition.ACCEPTED
        assert admitted.authority is not None
        assert ledger.count() == 1
        assert not ledger.has_global_admission_capacity()
        assert ledger.settle_no_effect(admitted.authority)
        assert ledger.has_global_admission_capacity()

    final = ledger.admit(
        _envelope("record-edge-final", trace_id="record-edge-final-trace")
    )
    assert final.disposition is AdmitDisposition.ACCEPTED
    assert ledger.count() == 1
    assert ledger.tombstone_count() == 12


def test_pruning_is_bounded_and_an_expired_exact_identity_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        terminal_retention_seconds=1,
        local_bypass_replay_retention_seconds=10,
    )
    for index in range(3):
        admitted = ledger.admit(
            _envelope(
                f"expired-{index}",
                trace_id=f"expired-{index}-trace",
                route_name=f"expired-scope-{index}",
                local_bypass=True,
            )
        )
        assert admitted.authority is not None
        assert ledger.settle_no_effect(admitted.authority)

    clock["now"] = 102.0
    monkeypatch.setattr(ledger_module, "_MAX_PRUNE_BATCH", 3)
    assert ledger.prune() == 3
    assert ledger.tombstone_count() == 3
    clock["now"] = 111.0
    monkeypatch.setattr(ledger_module, "_MAX_PRUNE_BATCH", 2)
    assert ledger.prune() == 2
    assert ledger.tombstone_count() == 1

    reopened = ledger.admit(
        _envelope(
            "expired-2",
            trace_id="expired-2-reopened",
            route_name="expired-scope-2",
            local_bypass=True,
        )
    )
    assert reopened.disposition is AdmitDisposition.ACCEPTED
    assert ledger.tombstone_count() == 0


def test_remote_proof_with_expiry_is_rejected_as_corruption(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path, terminal_retention_seconds=1)
    admitted = ledger.admit(_envelope("permanent", trace_id="permanent-trace"))
    assert admitted.authority is not None
    assert ledger.settle_no_effect(admitted.authority)
    assert ledger.prune(now=ledger_module.time.time() + 10) == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE webhook_delivery_tombstones SET expires_at=20_000")

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="tombstone expiry is invalid",
    ):
        WebhookOperationLedger(db_path)


@pytest.mark.parametrize(
    "invalid_limit",
    [
        MINIMUM_MAX_STORAGE_BYTES - 1,
        MAXIMUM_MAX_STORAGE_BYTES + 1,
        True,
        float(MINIMUM_MAX_STORAGE_BYTES),
        str(MINIMUM_MAX_STORAGE_BYTES),
        None,
    ],
)
def test_storage_limit_range_is_strict(tmp_path: Path, invalid_limit):
    with pytest.raises(ValueError, match="supported storage range"):
        WebhookOperationLedger(
            tmp_path / f"invalid-{invalid_limit}.db",
            max_storage_bytes=invalid_limit,
        )
