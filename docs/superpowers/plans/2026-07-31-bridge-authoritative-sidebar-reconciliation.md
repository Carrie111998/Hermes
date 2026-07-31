# Bridge-Authoritative Sidebar Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsupported broker-side Codex task search with durable, bridge-authoritative signed-marker reconciliation and transactionally revalidated at-most-once project-aware creation.

**Architecture:** The existing `SidebarThreadVerifier` performs a fresh, fully paginated active-and-archived Codex inventory/search read and returns authenticated reconciliation evidence. Session Bridge persists that evidence against the leased job, returns only `recovered` or `absence_proven` leases, and repeats the authoritative check immediately before atomically reserving native creation. The broker never calls `list_threads`; it either authenticates the exact recovered ID, reserves and creates once from a valid absence proof, or fails closed.

**Tech Stack:** Python 3.11+, asyncio, SQLite, Starlette/FastMCP, Codex app-server read APIs, Codex Desktop project/thread tools, pytest through `scripts/run_tests.sh`.

---

## Execution invariants

- Execute in a dedicated worktree created from production `main`, recommended path `C:\tmp\hermes-bridge-authoritative-reconciliation`, branch `codex/bridge-authoritative-reconciliation`.
- Use `apply_patch` for source and test edits.
- Run every test through the repository wrapper. On Windows use:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/tmp/hermes-bridge-authoritative-reconciliation/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_reconciliation.py -q'
```

- If the worktree has no `.venv`, set `HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe`; do not copy or recreate the production database.
- Keep the production `session-sidebar-sync` automation paused, `continuous=false`, and legacy hydration disabled until Task 8's rollout gates pass.
- Never mutate Claude JSONL, Codex private databases, rollout files, or packaged Codex application code.
- Never authorize creation from a title, tag, summary, bounded recent listing, stale proof, incomplete inventory, or any job with a prior create reservation.
- After a native create reservation is committed, uncertainty never authorizes replacement creation.
- Preserve the existing readable preview, latest-five, `.hermes` placement, immediate returned-ID binding, and continuation safety contracts.

## File and responsibility map

| File | Responsibility in this change |
|---|---|
| `session_bridge/sidebar_reconciliation.py` | Typed reconciliation states/evidence and canonical proof digest construction |
| `hermes_state.py` | Durable reconciliation-proof table, indexes, immutability guards, and schema version |
| `session_bridge/store.py` | Proof persistence, claim gating, compare-and-set reserve revalidation, and status counts |
| `session_bridge/codex_adapter.py` | Fresh complete active/archived marker reconciliation with authenticated full task reads |
| `session_bridge/coordinator.py` | Produce proofs before lease return, recover exact tasks, recheck immediately before reserve |
| `session_bridge/mcp_server.py` | Fail-closed lease payload and authoritative reserve endpoint |
| `session_bridge/assets/session-sidebar-sync/SKILL.md` | Query-free broker procedure using only lease-provided reconciliation |
| `tests/session_bridge/fixtures/sidebar_skill_baseline.txt` | Reviewed query-free skill baseline |
| `session_bridge/sidebar_skill.py` | Existing packaged-skill installation boundary; digest behavior remains unchanged |
| `tests/hermes_state/test_session_bridge_schema.py` | Fresh/upgrade schema, constraints, and immutable proof tests |
| `tests/session_bridge/test_target_adapters.py` | Complete inventory/search, authentication, conflict, and failure evidence tests |
| `tests/session_bridge/test_store.py` | Durable proof and transactional reservation race tests |
| `tests/session_bridge/test_sidebar_reconciliation.py` | Coordinator state selection, recovery, stale proof, and ambiguity tests |
| `tests/session_bridge/test_mcp_server.py` | Lease/reserve API contract and fail-closed serialization tests |
| `tests/session_bridge/test_sidebar_skill.py` | No `list_threads`, exact recovered-ID read, guarded one-create broker contract |
| `tests/session_bridge/test_end_to_end.py` | Existing-task recovery and genuinely missing-task creation paths |
| `tests/session_bridge/test_fault_injection.py` | Crash, response loss, restart, and concurrency duplicate prevention |
| `session_bridge/cli.py` | Sanitized reconciliation health rendering |
| `docs/superpowers/audits/2026-07-30-session-sidebar-requirement-traceability.md` | Named implementation evidence and canary results |

## Public reconciliation contract

The plan uses these exact names throughout:

```python
class SidebarReconciliationState(StrEnum):
    RECOVERED = "recovered"
    ABSENCE_PROVEN = "absence_proven"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SidebarReconciliationEvidence:
    state: SidebarReconciliationState
    generation: str
    completed_at: float
    expires_at: float
    inventory_digest: str
    marker_digest: str
    match_count: int
    recovered_thread: VerifiedSidebarThread | None
    fixed_reason: str | None
```

The broker payload uses one of these two valid shapes:

```json
{
  "reconciliation_state": "recovered",
  "reconciliation_generation": "codex:100000000:4f2c1d8a",
  "reconciliation_proof_digest": "1111111111111111111111111111111111111111111111111111111111111111",
  "recovered_thread_id": "22222222-2222-4222-8222-222222222222",
  "create_eligible": false,
  "create_reserved": false
}
```

```json
{
  "reconciliation_state": "absence_proven",
  "reconciliation_generation": "codex:100000001:e3b0c442",
  "reconciliation_proof_digest": "2222222222222222222222222222222222222222222222222222222222222222",
  "recovered_thread_id": null,
  "create_eligible": true,
  "create_reserved": false
}
```

`blocked` is durable bridge state and never leaves the service as a creation-capable broker lease. `create_eligible` is true only for `absence_proven` with no prior reservation. Existing fields remain during one compatibility release only where tests prove they cannot weaken this contract.

### Task 1: Add typed reconciliation evidence and canonical proof digests

**Files:**
- Create: `session_bridge/sidebar_reconciliation.py`
- Modify: `session_bridge/__init__.py`
- Test: `tests/session_bridge/test_sidebar_reconciliation.py`

- [ ] **Step 1: Write failing type and digest tests**

Add tests that construct recovered, absent, and blocked evidence and prove that the digest changes for every authority-bearing field:

```python
def test_sidebar_reconciliation_proof_digest_binds_every_authority_field():
    base = SidebarReconciliationProofInput(
        job_id="sidebar-job:1",
        source_session_id=SOURCE,
        bridge_id=BRIDGE,
        marker_digest="1" * 64,
        placement_generation=1,
        delivery_generation=1,
        reconciliation_generation="scan:1",
        completed_at=100.0,
        expires_at=130.0,
        inventory_digest="2" * 64,
        state=SidebarReconciliationState.ABSENCE_PROVEN,
        match_count=0,
        recovered_thread_id=None,
        fixed_reason=None,
    )
    digest = sidebar_reconciliation_proof_digest(base)
    assert len(digest) == 64
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, reconciliation_generation="scan:2")
    )
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, placement_generation=2)
    )
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, source_session_id="claude:other")
    )


@pytest.mark.parametrize(
    ("state", "match_count", "thread_id", "reason"),
    [
        (SidebarReconciliationState.RECOVERED, 1, THREAD, None),
        (SidebarReconciliationState.ABSENCE_PROVEN, 0, None, None),
        (SidebarReconciliationState.BLOCKED, 2, None, "marker_conflict"),
    ],
)
def test_sidebar_reconciliation_evidence_enforces_state_shape(
    state, match_count, thread_id, reason
):
    evidence = SidebarReconciliationEvidence.create(
        state=state,
        generation="scan:1",
        completed_at=100.0,
        expires_at=130.0,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=match_count,
        recovered_thread_id=thread_id,
        fixed_reason=reason,
    )
    assert evidence.state is state
```

- [ ] **Step 2: Run the focused test and verify the missing-module failure**

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_reconciliation.py -q'
```

Expected: FAIL because `session_bridge.sidebar_reconciliation` and its types do not exist.

- [ ] **Step 3: Implement the exact state, evidence, proof-input, and digest API**

Create `session_bridge/sidebar_reconciliation.py` with:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json


class SidebarReconciliationState(StrEnum):
    RECOVERED = "recovered"
    ABSENCE_PROVEN = "absence_proven"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SidebarReconciliationEvidence:
    state: SidebarReconciliationState
    generation: str
    completed_at: float
    expires_at: float
    inventory_digest: str
    marker_digest: str
    match_count: int
    recovered_thread_id: str | None
    fixed_reason: str | None

    @classmethod
    def create(cls, **values: object) -> "SidebarReconciliationEvidence":
        evidence = cls(**values)  # type: ignore[arg-type]
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if not self.generation or self.completed_at > self.expires_at:
            raise ValueError("sidebar reconciliation generation is malformed")
        for value in (self.inventory_digest, self.marker_digest):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError("sidebar reconciliation digest is malformed")
        shapes = {
            SidebarReconciliationState.RECOVERED: (1, True, None),
            SidebarReconciliationState.ABSENCE_PROVEN: (0, False, None),
        }
        if self.state in shapes:
            count, requires_thread, reason = shapes[self.state]
            if self.match_count != count or bool(self.recovered_thread_id) != requires_thread or self.fixed_reason is not reason:
                raise ValueError("sidebar reconciliation state shape is malformed")
        elif self.state is SidebarReconciliationState.BLOCKED:
            if self.recovered_thread_id is not None or not self.fixed_reason:
                raise ValueError("blocked sidebar reconciliation is malformed")
        else:
            raise ValueError("sidebar reconciliation state is unsupported")


@dataclass(frozen=True)
class SidebarReconciliationProofInput:
    job_id: str
    source_session_id: str
    bridge_id: str
    marker_digest: str
    placement_generation: int
    delivery_generation: int
    reconciliation_generation: str
    completed_at: float
    expires_at: float
    inventory_digest: str
    state: SidebarReconciliationState
    match_count: int
    recovered_thread_id: str | None
    fixed_reason: str | None


def sidebar_reconciliation_proof_digest(
    value: SidebarReconciliationProofInput,
) -> str:
    payload = asdict(value)
    payload["state"] = value.state.value
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

Tighten construction with existing exact-text and finite-number conventions if tests expose invalid Python values; do not add a permissive normalization path.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the Task 1 wrapper command again. Expected: PASS.

- [ ] **Step 5: Commit the typed contract**

```powershell
git add session_bridge/sidebar_reconciliation.py session_bridge/__init__.py tests/session_bridge/test_sidebar_reconciliation.py
git commit -m "feat(session-bridge): define sidebar reconciliation proofs"
```

### Task 2: Persist immutable authoritative proofs

**Files:**
- Modify: `hermes_state.py:237-241`
- Modify: `hermes_state.py:1132-1170`
- Modify: `session_bridge/store.py:5804-6259`
- Test: `tests/hermes_state/test_session_bridge_schema.py`
- Test: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing fresh-schema, upgrade, and constraint tests**

Add tests proving the table exists on a fresh database and after upgrading a version-30 database:

```python
def test_sidebar_reconciliation_proof_schema_is_immutable(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    columns = {
        row[1] for row in db._conn.execute(
            'PRAGMA table_info("session_sidebar_reconciliation_proofs")'
        )
    }
    assert columns == {
        "job_id", "proof_digest", "source_session_id", "bridge_id",
        "marker_digest", "placement_generation", "delivery_generation",
        "reconciliation_generation", "completed_at", "expires_at",
        "inventory_digest", "state", "match_count",
        "recovered_thread_id", "fixed_reason", "created_at",
    }
    assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 31
```

In `test_store.py`, seed one sidebar job and assert:

```python
proof = store.record_sidebar_reconciliation_proof(
    lease_token=lease_token,
    evidence=absence_evidence,
    marker_digest="1" * 64,
    placement_generation=1,
    delivery_generation=1,
    now=100.0,
)
assert proof["state"] == "absence_proven"
assert proof["match_count"] == 0
with pytest.raises(sqlite3.IntegrityError, match="immutable"):
    db._conn.execute(
        "UPDATE session_sidebar_reconciliation_proofs SET state='blocked'"
    )
```

Also prove `recovered` requires one thread ID, `absence_proven` requires zero matches and no thread ID, `blocked` requires a fixed reason, and a proof cannot reference another job/source/bridge.

- [ ] **Step 2: Run schema/store tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_store.py -q'
```

Expected: FAIL because schema version 31, the proof table, and store methods are absent.

- [ ] **Step 3: Add the declarative schema and version**

Bump `SCHEMA_VERSION` from `30` to `31` and add:

```sql
CREATE TABLE IF NOT EXISTS session_sidebar_reconciliation_proofs (
    proof_digest TEXT PRIMARY KEY CHECK (
        length(proof_digest) = 64
        AND proof_digest NOT GLOB '*[^0-9a-f]*'
    ),
    job_id TEXT NOT NULL
        REFERENCES session_sidebar_jobs(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_session_id TEXT NOT NULL,
    bridge_id TEXT NOT NULL,
    marker_digest TEXT NOT NULL CHECK (
        length(marker_digest) = 64
        AND marker_digest NOT GLOB '*[^0-9a-f]*'
    ),
    placement_generation INTEGER NOT NULL CHECK (placement_generation > 0),
    delivery_generation INTEGER NOT NULL CHECK (delivery_generation > 0),
    reconciliation_generation TEXT NOT NULL,
    completed_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at >= completed_at),
    inventory_digest TEXT NOT NULL CHECK (
        length(inventory_digest) = 64
        AND inventory_digest NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (
        state IN ('recovered', 'absence_proven', 'blocked')
    ),
    match_count INTEGER NOT NULL CHECK (match_count >= 0),
    recovered_thread_id TEXT,
    fixed_reason TEXT,
    created_at REAL NOT NULL,
    CHECK (
        (state = 'recovered' AND match_count = 1
            AND recovered_thread_id IS NOT NULL AND fixed_reason IS NULL)
        OR (state = 'absence_proven' AND match_count = 0
            AND recovered_thread_id IS NULL AND fixed_reason IS NULL)
        OR (state = 'blocked' AND recovered_thread_id IS NULL
            AND fixed_reason IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_sidebar_reconciliation_job_created
    ON session_sidebar_reconciliation_proofs(job_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_sidebar_reconciliation_proofs_no_update
BEFORE UPDATE ON session_sidebar_reconciliation_proofs
BEGIN
    SELECT RAISE(ABORT, 'sidebar reconciliation proofs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_sidebar_reconciliation_proofs_no_delete
BEFORE DELETE ON session_sidebar_reconciliation_proofs
BEGIN
    SELECT RAISE(ABORT, 'sidebar reconciliation proofs are immutable');
END;
```

The table is append-only because one job receives a newer proof during reserve
revalidation. Add nullable `reconciliation_proof_digest TEXT` to
`session_sidebar_jobs` through the repository's required-column mechanism,
referencing the current immutable proof by validated lookup in store
transactions. The fresh schema and upgrade tests must assert this final shape.

- [ ] **Step 4: Implement proof record and lookup methods**

Add methods with these exact signatures:

```python
def record_sidebar_reconciliation_proof(
    self,
    *,
    lease_token: str,
    evidence: SidebarReconciliationEvidence,
    marker_digest: str,
    placement_generation: int,
    delivery_generation: int,
    now: float,
) -> dict[str, Any]:

def get_sidebar_reconciliation_proof(
    self, *, lease_token: str
) -> dict[str, Any] | None:
```

These are signature declarations for the plan; implement their bodies in
`SessionBridgeStore`. `record_sidebar_reconciliation_proof` must, in one
`_execute_write` transaction, validate the live lease, derive
`SidebarReconciliationProofInput` from the authoritative database job plus
evidence, insert the immutable row, and compare-and-set the job's current
digest. `get_sidebar_reconciliation_proof` resolves the live lease, loads only
the digest currently named by the job, and returns a copied row. Exact replay
of the same digest returns the existing row. Cross-job, expired-lease,
identity, generation, or shape mismatch raises `ValueError`.

- [ ] **Step 5: Run schema/store tests and verify pass**

Run the Task 2 wrapper command again. Expected: PASS.

- [ ] **Step 6: Commit durable proof storage**

```powershell
git add hermes_state.py session_bridge/store.py tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): persist sidebar reconciliation proofs"
```

### Task 3: Produce fresh complete authenticated reconciliation evidence

**Files:**
- Modify: `session_bridge/codex_adapter.py:149-430`
- Test: `tests/session_bridge/test_target_adapters.py`

- [ ] **Step 1: Write failing adapter evidence tests**

Add tests covering full active and archived pagination, exact marker authentication, and fail-closed scans:

```python
def test_sidebar_reconcile_marker_returns_complete_absence_evidence():
    verifier, client = _sidebar_verifier_with_pages(active=[[]], archived=[[]])
    evidence = verifier.reconcile_marker(EXPECTED, now=100.0, ttl_seconds=30.0)
    assert evidence.state is SidebarReconciliationState.ABSENCE_PROVEN
    assert evidence.match_count == 0
    assert evidence.completed_at == 100.0
    assert evidence.expires_at == 130.0
    assert client.requests == [
        ("thread/search", {"archived": False, "searchTerm": EXPECTED_PREFIX}),
        ("thread/search", {"archived": True, "searchTerm": EXPECTED_PREFIX}),
    ]


def test_sidebar_reconcile_marker_authenticates_full_task_not_summary():
    verifier = _sidebar_verifier_with_summary_hit_and_invalid_full_marker()
    evidence = verifier.reconcile_marker(EXPECTED, now=100.0, ttl_seconds=30.0)
    assert evidence.state is SidebarReconciliationState.BLOCKED
    assert evidence.fixed_reason == "marker_conflict"


@pytest.mark.parametrize("failure", ["repeated_cursor", "page_cap", "read_error"])
def test_sidebar_reconcile_marker_never_proves_absence_from_incomplete_inventory(failure):
    verifier = _sidebar_verifier_with_failure(failure)
    with pytest.raises(SidebarVerificationError) as raised:
        verifier.reconcile_marker(EXPECTED, now=100.0, ttl_seconds=30.0)
    assert raised.value.code == "bridge_temporarily_unavailable"
```

Also assert one match yields `recovered`, two authenticated matches yield `blocked/marker_conflict`, and inventory/title metadata never supplies identity without a full task read.

- [ ] **Step 2: Run adapter tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_target_adapters.py -q'
```

Expected: FAIL because `reconcile_marker` does not exist.

- [ ] **Step 3: Implement a fresh evidence-producing scan**

Add:

```python
def reconcile_marker(
    self,
    expected: BridgeMarkerPayload,
    *,
    now: float,
    ttl_seconds: float,
) -> SidebarReconciliationEvidence:
    expected = _validated_sidebar_marker_payload(expected)
    projections = self._fresh_marker_inventory_projections(expected)
    matches: dict[str, VerifiedSidebarThread] = {}
    for projection in projections:
        verified = _verified_sidebar_projection(
            projection,
            expected=expected,
            marker_secret=self._marker_secret,
            strict=False,
        )
        if verified is not None:
            matches[verified.thread_id] = verified
    marker = encode_bridge_marker(expected, self._marker_secret)
    marker_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
    inventory_digest = _sidebar_inventory_digest(projections)
    generation = f"codex:{int(now * 1_000_000)}:{inventory_digest}"
    if len(matches) > 1:
        return SidebarReconciliationEvidence.create(
            state=SidebarReconciliationState.BLOCKED,
            generation=generation,
            completed_at=now,
            expires_at=now + ttl_seconds,
            inventory_digest=inventory_digest,
            marker_digest=marker_digest,
            match_count=len(matches),
            recovered_thread_id=None,
            fixed_reason="marker_conflict",
        )
    recovered = next(iter(matches.values()), None)
    return SidebarReconciliationEvidence.create(
        state=(SidebarReconciliationState.RECOVERED if recovered else SidebarReconciliationState.ABSENCE_PROVEN),
        generation=generation,
        completed_at=now,
        expires_at=now + ttl_seconds,
        inventory_digest=inventory_digest,
        marker_digest=marker_digest,
        match_count=int(recovered is not None),
        recovered_thread_id=(recovered.thread_id if recovered else None),
        fixed_reason=None,
    )
```

`_fresh_marker_inventory_projections` must bypass `_inventory_snapshot`. Use fully paginated `thread/search` over both active and archived inventories when supported; otherwise use a fresh fully paginated `list_sidebar_inventory`. Read every returned candidate and authenticate its full signed marker. Any page cap, deadline, repeated cursor, malformed result, conflicting summary, or candidate read failure must raise a fixed `SidebarVerificationError`; it must never return absence. `_sidebar_inventory_digest` must hash canonical tuples of native ID, revision, native status, and projected marker-bearing user content, sorted by native ID.

Keep `find_by_marker` as a compatibility wrapper around `reconcile_marker`, returning the exact recovered thread only and converting blocked state to the existing fixed error.

- [ ] **Step 4: Run adapter tests and verify pass**

Run the Task 3 wrapper command again. Expected: PASS.

- [ ] **Step 5: Commit authoritative scanner evidence**

```powershell
git add session_bridge/codex_adapter.py tests/session_bridge/test_target_adapters.py
git commit -m "feat(session-bridge): reconcile sidebar markers authoritatively"
```

### Task 4: Gate leases on durable bridge-authoritative results

**Files:**
- Modify: `session_bridge/coordinator.py:1246-1430`
- Modify: `session_bridge/mcp_server.py:478-520`
- Modify: `session_bridge/mcp_server.py:1878-1980`
- Test: `tests/session_bridge/test_sidebar_reconciliation.py`
- Test: `tests/session_bridge/test_mcp_server.py`

- [ ] **Step 1: Write failing coordinator and payload tests**

Add three claim tests:

```python
async def test_claim_returns_recovered_authoritative_task_without_creation():
    coordinator, store = _coordinator_with_evidence(_recovered_evidence(THREAD))
    claim = (await coordinator.claim_sidebar_jobs_for_delivery(now=100.0))[0]
    assert claim.reconciliation_state is SidebarReconciliationState.RECOVERED
    assert claim.recovered_thread_id == THREAD
    assert claim.create_eligible is False
    assert store.binds == [("opaque-lease-token", THREAD, 100.0)]


async def test_claim_returns_creation_eligibility_only_from_durable_absence_proof():
    coordinator, store = _coordinator_with_evidence(_absence_evidence())
    claim = (await coordinator.claim_sidebar_jobs_for_delivery(now=100.0))[0]
    assert claim.reconciliation_state is SidebarReconciliationState.ABSENCE_PROVEN
    assert claim.create_eligible is True
    assert claim.reconciliation_proof_digest == store.current_proof_digest


async def test_claim_with_prior_reservation_and_zero_match_never_leases_creation():
    coordinator, store = _coordinator_with_evidence(
        _absence_evidence(), create_reserved=True
    )
    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0) == ()
    assert store.failures == [
        ("opaque-lease-token", "native_create_ambiguous", 100.0)
    ]
```

Add MCP serialization tests proving `blocked` never appears as a broker job, a missing proof settles fail-closed, and no payload field implies search authority.

- [ ] **Step 2: Run coordinator/MCP tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py -q'
```

Expected: FAIL because claim and broker payload fields are absent.

- [ ] **Step 3: Replace the claim contract**

Change `SidebarDeliveryClaim` to include:

```python
@dataclass(frozen=True)
class SidebarDeliveryClaim:
    lease_token: str
    source_session_id: str
    bridge_id: str
    reconciliation_state: SidebarReconciliationState
    reconciliation_generation: str
    reconciliation_proof_digest: str
    recovered_thread_id: str | None
    create_eligible: bool
    rename_required: bool
    create_reserved: bool = False
```

In `claim_sidebar_jobs_for_delivery`, after the store lease and prior-reservation lookup:

1. Call `verifier.reconcile_marker(expected, now=claim_time, ttl_seconds=min(30.0, self._config.service.reconcile_seconds))`.
2. Record the evidence through `record_sidebar_reconciliation_proof` before returning anything.
3. For `recovered`, bind the exact evidence ID, set `create_eligible=False`, and return the bind-only claim.
4. For `absence_proven` with no earlier reservation, return `create_eligible=True`.
5. For `absence_proven` with an earlier reservation, fail once with `native_create_ambiguous` and return no claim.
6. For `blocked`, fail with its fixed reason and return no claim.
7. For scanner/proof persistence failure, settle once with `bridge_temporarily_unavailable` and return no claim.

Remove the old `reconcile_required` path and never return an unproved zero-match lease.

- [ ] **Step 4: Serialize only the authoritative fields**

In `_build_sidebar_broker_job`, validate state/ID/create shape and return:

```python
authoritative_fields = {
    "reconciliation_state": claim.reconciliation_state.value,
    "reconciliation_generation": claim.reconciliation_generation,
    "reconciliation_proof_digest": claim.reconciliation_proof_digest,
    "recovered_thread_id": claim.recovered_thread_id,
    "create_eligible": claim.create_eligible,
    "create_reserved": claim.create_reserved,
}
job.update(authoritative_fields)
return job
```

Here `job` is the existing registration-prompt/title/provider/cwd/git mapping
built earlier in `_build_sidebar_broker_job`; remove its legacy
`reconcile_required` entry before applying `authoritative_fields`.

Reject `recovered` without an ID, `absence_proven` with an ID, any state other than those two, and `create_eligible=True` outside `absence_proven`.

- [ ] **Step 5: Run coordinator/MCP tests and verify pass**

Run the Task 4 wrapper command again. Expected: PASS.

- [ ] **Step 6: Commit lease gating**

```powershell
git add session_bridge/coordinator.py session_bridge/mcp_server.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): gate sidebar leases on reconciliation proof"
```

### Task 5: Revalidate proof and reserve creation atomically

**Files:**
- Modify: `session_bridge/store.py:6157-6228`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/mcp_server.py:680-723`
- Test: `tests/session_bridge/test_store.py`
- Test: `tests/session_bridge/test_sidebar_reconciliation.py`
- Test: `tests/session_bridge/test_mcp_server.py`

- [ ] **Step 1: Write failing stale-proof and race tests**

Add store and MCP tests proving each revalidation field is mandatory:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        {"expires_at": 99.0},
        {"state": "recovered", "match_count": 1, "recovered_thread_id": THREAD},
        {"placement_generation": 2},
        {"delivery_generation": 2},
        {"reconciliation_generation": "scan:new"},
    ],
)
def test_reserve_sidebar_create_rejects_stale_or_changed_proof(mutation):
    store, lease, proof = _store_with_absence_proof(now=100.0)
    _mutate_current_proof_for_test(store, proof, mutation)
    with pytest.raises(ValueError, match="reconciliation proof"):
        store.reserve_sidebar_create(
            lease_token=lease,
            recovery_key="r" * 64,
            reconciliation_proof_digest=proof,
            reconciliation_generation="scan:1",
            now=100.0,
        )
    assert store.get_sidebar_create_reservation(SOURCE) is None
```

Add a coordinator test where the fresh pre-reserve scan finds a task and assert it binds that task and performs no reservation. Add another where a newer zero proof is recorded and the reserve succeeds once. Add concurrent reserve calls and assert one durable reservation with exact replay only.

- [ ] **Step 2: Run focused tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_store.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py -q'
```

Expected: FAIL because reserve accepts no proof arguments and performs no fresh authoritative recheck.

- [ ] **Step 3: Add the coordinator reserve boundary**

Add:

```python
async def reserve_sidebar_create_authoritatively(
    self,
    *,
    lease_token: str,
    reconciliation_proof_digest: str,
    reconciliation_generation: str,
    now: float | None = None,
) -> Mapping[str, Any]:
```

This is the exact method signature; implement its body in
`SessionBridgeCoordinator`. It must load the exact lease/job, reject any
existing thread ID or create reservation, reconstruct the expected marker,
perform a fresh `reconcile_marker`, and then:

- bind and return `{"state": "recovered", "codex_thread_id": exact_id}` if one task now exists;
- persist blocked evidence and fail closed on conflict/incomplete state;
- persist the new absence proof and call the store reserve method with the new proof digest/generation if zero remains proven.

The originally leased proof arguments establish that the broker is acting on the issued lease, while the fresh proof is the only proof allowed to authorize the reservation.

- [ ] **Step 4: Enforce proof comparison inside `reserve_sidebar_create`**

Change its signature to:

```python
def reserve_sidebar_create(
    self,
    *,
    lease_token: str,
    recovery_key: str,
    reconciliation_proof_digest: str,
    reconciliation_generation: str,
    now: float,
) -> dict[str, Any]:
```

This is the exact replacement signature; implement its body in
`SessionBridgeStore`. In the existing `_execute_write` transaction, require the
job's current proof digest to equal the supplied digest; load the immutable
proof; require same job/source/bridge, `absence_proven`, zero matches, no
recovered ID, unexpired at `now`, matching placement and delivery generations,
and exact generation. Recheck no canonical target and no existing reservation
before writing the existing recovery-key reservation. Exact replay may return
the same reservation only when recovery key, proof digest, and generation all
match.

- [ ] **Step 5: Route MCP reserve through the coordinator**

Change `session_sidebar_reserve` to accept:

```python
async def session_sidebar_reserve(
    lease_token: Any,
    reconciliation_proof_digest: Any,
    reconciliation_generation: Any,
) -> dict[str, Any]:
```

Call `coordinator.reserve_sidebar_create_authoritatively`. Return either:

```python
{"state": "sidebar_leased", "create_reserved": True}
```

or:

```python
{"state": "recovered", "codex_thread_id": thread_id, "create_reserved": False}
```

Malformed, stale, or blocked proof responses map to the fixed public `sidebar_reserve_failed` without exposing provider text and never authorize create.

- [ ] **Step 6: Run focused tests and verify pass**

Run the Task 5 wrapper command again. Expected: PASS.

- [ ] **Step 7: Commit transactional revalidation**

```powershell
git add session_bridge/store.py session_bridge/coordinator.py session_bridge/mcp_server.py tests/session_bridge/test_store.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): revalidate sidebar proof before create"
```

### Task 6: Remove all broker-side task discovery

**Files:**
- Modify: `session_bridge/assets/session-sidebar-sync/SKILL.md`
- Modify: `tests/session_bridge/fixtures/sidebar_skill_baseline.txt`
- Modify: `tests/session_bridge/test_sidebar_skill.py`
- Test: `tests/session_bridge/test_end_to_end.py`

- [ ] **Step 1: Replace search-dependent skill tests with query-free assertions**

Delete assertions requiring
`list_threads({"query":"<exact signed marker>","limit":20})` and add:

```python
def test_sidebar_skill_never_discovers_tasks_through_native_list_threads():
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    assert "list_threads" not in skill
    assert "reconciliation_state" in skill
    assert "reconciliation_proof_digest" in skill
    assert "reconciliation_generation" in skill
    assert "create_eligible" in skill


def test_sidebar_skill_uses_only_authoritative_reconciliation_paths():
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    step = skill.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    assert "When `reconciliation_state` is `recovered`" in step
    assert "read_thread" in step
    assert "session_sidebar_bind" in step
    assert "When `reconciliation_state` is `absence_proven`" in step
    assert "do not inspect any other native task" in step
    assert "A missing or unsupported reconciliation state" in step
```

Update the E2E contract parser so it rejects any `list_threads` instruction and parses the three proof fields.

- [ ] **Step 2: Run skill/E2E tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_end_to_end.py -q'
```

Expected: FAIL because the packaged skill still requires unsupported broker search.

- [ ] **Step 3: Rewrite the reconciliation and reserve steps**

Replace the current search branch with these normative rules:

```markdown
5. Trust only the authoritative reconciliation object returned by
   `session_sidebar_pending`. Never call `list_threads`, search by title or tag,
   paginate Codex tasks, or infer absence from Recents.
   - When `reconciliation_state` is `recovered`, require
     `recovered_thread_id`, require `create_eligible=false`, read only that exact
     task, authenticate its full signed marker and Session Inbox placement, then
     bind that same ID. Never create on any recovered-task read or bind failure.
   - When `reconciliation_state` is `absence_proven`, require no recovered ID,
     require `create_eligible=true`, and do not inspect any other native task.
   - A missing or unsupported reconciliation state, generation, or proof digest
     maps to `bridge_temporarily_unavailable` and never permits creation.

6. For `absence_proven`, call
   `session_sidebar_reserve(lease_token=<exact token>,
   reconciliation_proof_digest=<exact digest>,
   reconciliation_generation=<exact generation>)` immediately before native
   create. If reserve returns `recovered`, read, authenticate, and bind only its
   exact task ID; do not create. Create exactly once only when reserve returns
   `state=sidebar_leased` and `create_reserved=true`. Every other response fails
   closed.
```

Retain the current project preflight, exact create payload, immediate bind, bounded read verification, rename, commit, and ambiguity rules unchanged. Regenerate the baseline from the reviewed asset through the repository's existing baseline procedure; do not hand-edit divergent wording into the fixture.

- [ ] **Step 4: Run skill/E2E tests and verify pass**

Run the Task 6 wrapper command again. Expected: PASS.

- [ ] **Step 5: Commit the query-free broker contract**

```powershell
git add session_bridge/assets/session-sidebar-sync/SKILL.md tests/session_bridge/fixtures/sidebar_skill_baseline.txt tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_end_to_end.py
git commit -m "fix(session-bridge): remove broker-side task search"
```

### Task 7: Prove crash safety, status, and requirement coverage

**Files:**
- Modify: `session_bridge/store.py:7999-8240`
- Modify: `session_bridge/mcp_server.py:384-448`
- Modify: `session_bridge/cli.py`
- Modify: `tests/session_bridge/test_fault_injection.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_mcp_server.py`
- Modify: `docs/superpowers/audits/2026-07-30-session-sidebar-requirement-traceability.md`

- [ ] **Step 1: Write failing restart, ambiguity, and status tests**

Add fault-injection tests for these exact boundaries:

```python
@pytest.mark.parametrize(
    "crash_point",
    [
        "after_scan_before_proof",
        "after_proof_before_lease_return",
        "after_lease_before_reserve_recheck",
        "after_reservation_before_create",
        "after_create_before_bind",
        "after_bind_before_commit",
    ],
)
def test_authoritative_reconciliation_restart_never_authorizes_second_create(
    crash_point
):
    harness = SidebarFaultHarness(crash_point=crash_point)
    harness.run_then_restart()
    assert harness.native_create_calls <= 1
    assert harness.canonical_thread_count <= 1
    if harness.create_was_reserved_without_bound_id:
        assert harness.job.error_code == "native_create_ambiguous"
```

Add status tests expecting only bounded fields:

```python
assert status["reconciliation_counts"] == {
    "recovered": 1,
    "absence_proven": 2,
    "blocked": 3,
}
assert status["reconciliation_blocked_codes"] == {
    "marker_conflict": 1,
    "native_create_ambiguous": 2,
}
assert status["oldest_reconciliation_wait_age_seconds"] == 40.0
assert "signed_marker" not in json.dumps(status)
assert "proof_digest" not in json.dumps(status)
```

- [ ] **Step 2: Run fault/status tests and verify failure**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_fault_injection.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py -q'
```

Expected: FAIL because proof status and new crash scenarios are absent.

- [ ] **Step 3: Add sanitized reconciliation status**

Extend `sidebar_delivery_status` with counts grouped by proof state, fixed blocked reason, oldest pending job without a current valid proof, reserve rejection counts, and recovered-versus-created totals. Return only fixed codes, counts, ages, completion timestamps, and a redacted generation age; do not return raw generation, marker, digest, lease, path, message, or provider error fields.

Shape MCP and CLI output with:

```python
{
    "reconciliation_counts": {
        state: _nonnegative_status_int(
            _status_mapping(source.get("reconciliation_counts")).get(state), 0
        )
        for state in ("recovered", "absence_proven", "blocked")
    },
    "reconciliation_blocked_codes": {
        code: _nonnegative_status_int(
            _status_mapping(source.get("reconciliation_blocked_codes")).get(code),
            0,
        )
        for code in ("marker_conflict", "native_create_ambiguous",
                     "bridge_temporarily_unavailable")
    },
    "oldest_reconciliation_wait_age_seconds": _finite_status_number(
        source.get("oldest_reconciliation_wait_age_seconds")
    ),
    "reconciliation_scan_age_seconds": _finite_status_number(
        source.get("reconciliation_scan_age_seconds")
    ),
    "recovered_existing_total": _nonnegative_status_int(
        source.get("recovered_existing_total"), 0
    ),
    "created_new_total": _nonnegative_status_int(
        source.get("created_new_total"), 0
    ),
}
```

- [ ] **Step 4: Update traceability with amendment requirements**

Append rows:

```markdown
| BAR-001 | 2026-07-31 bridge-authoritative reconciliation | Broker performs no native task discovery | implemented | `session_bridge/assets/session-sidebar-sync/SKILL.md` | `test_sidebar_skill_never_discovers_tasks_through_native_list_threads` | focused suite passed |
| BAR-002 | 2026-07-31 bridge-authoritative reconciliation | Complete authenticated scan is the only source of negative proof | implemented | `session_bridge/codex_adapter.py::SidebarThreadVerifier.reconcile_marker` | `test_sidebar_reconcile_marker_never_proves_absence_from_incomplete_inventory` | focused suite passed |
| BAR-003 | 2026-07-31 bridge-authoritative reconciliation | Proof is durably bound to job, marker, placement, delivery, and catalog generation | implemented | `session_bridge/store.py::record_sidebar_reconciliation_proof` | `test_sidebar_reconciliation_proof_digest_binds_every_authority_field` | focused suite passed |
| BAR-004 | 2026-07-31 bridge-authoritative reconciliation | Reserve rechecks fresh evidence transactionally | implemented | `session_bridge/coordinator.py::reserve_sidebar_create_authoritatively` | `test_reserve_sidebar_create_rejects_stale_or_changed_proof` | focused suite passed |
| BAR-005 | 2026-07-31 bridge-authoritative reconciliation | Prior reservation or ambiguous dispatch never replacement-creates | implemented | `session_bridge/store.py::reserve_sidebar_create` | `test_authoritative_reconciliation_restart_never_authorizes_second_create` | fault suite passed |
```

Update affected VIS rows from `missing` to the exact shipped commit/test evidence. Do not rewrite preserved or upstream-deferred history.

- [ ] **Step 5: Run fault/status tests and verify pass**

Run the Task 7 wrapper command again. Expected: PASS.

- [ ] **Step 6: Commit recovery, status, and audit evidence**

```powershell
git add session_bridge/store.py session_bridge/mcp_server.py session_bridge/cli.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py
git add -f docs/superpowers/audits/2026-07-30-session-sidebar-requirement-traceability.md
git commit -m "test(session-bridge): prove authoritative reconciliation recovery"
```

### Task 8: Verify, deploy safely, and resume delivery

**Files:**
- Verify: all files changed in Tasks 1-7
- Operational state: `C:\Users\diego\.hermes\state.db`
- Installed skill: `C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md`

- [ ] **Step 1: Run the focused reconciliation suite**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_target_adapters.py tests/session_bridge/test_store.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_end_to_end.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_cli.py -q'
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 2: Run the complete Session Bridge suite**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/tmp/hermes-bridge-authoritative-reconciliation && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge tests/hermes_state/test_session_bridge_schema.py -q'
```

Expected: all tests PASS with zero failures.

- [ ] **Step 3: Request code review and resolve findings**

Use `superpowers:requesting-code-review`. Review against the July 31 specification, with special attention to negative-proof completeness, append-only proof semantics, reserve races, prior-reservation ambiguity, and removal of every broker `list_threads` path. Apply accepted findings test-first and rerun Steps 1-2.

- [ ] **Step 4: Merge through the finishing workflow**

Use `superpowers:finishing-a-development-branch`. Fast-forward production `main` only if the worktree is clean, every commit is reviewed, Steps 1-2 pass on the final head, and production main has not gained conflicting commits. Preserve unrelated user changes and do not reset production state.

- [ ] **Step 5: Restart Session Bridge with mutation still disabled**

Restart the production service from `C:\Users\diego\.hermes\agent-src`, then verify the running process uses the new main head. Keep:

```text
session_bridge.sidebar.continuous = false
session_bridge.sidebar.legacy_hydration_enabled = false
automation session-sidebar-sync = paused
```

Expected health: schema version 31, no active sidebar lease, no malformed proof, and existing pending/retry/reservation rows preserved.

- [ ] **Step 6: Install and verify the query-free skill**

Use the repository's supported packaged-skill installer. Compare SHA-256 of the packaged and installed `SKILL.md`; require equality. Assert the installed skill contains no `list_threads`, contains all authoritative proof fields, and retains exact project preflight/create/bind/verify rules.

- [ ] **Step 7: Run a recovered-task canary**

Select one source already mapped to an authenticated Codex task. Trigger one manual broker cycle with automation paused. Require:

```text
reconciliation_state = recovered
recovered_thread_id = existing exact task
native create calls = 0
canonical task count = 1
```

Any mismatch keeps automation paused.

- [ ] **Step 8: Run one genuinely missing-session canary**

Select one eligible source with no prior create reservation and a current complete zero-match proof. Run one manual broker cycle. Require exactly one `.hermes` task, immediate exact-ID binding, readable summary and latest five messages, authenticated marker, one canonical link, and no second create after a service restart.

- [ ] **Step 9: Resume one-minute automation and inspect the newest five**

Resume only the exact `session-sidebar-sync` automation targeting broker task `019f9b71-7109-7ed0-943a-d7291190245c`. Keep continuous source enqueueing disabled until five manual leases are inspected. For each, record source ID, state, recovered-or-created disposition, exact task ID, `.hermes` placement, readable content, latency, and uniqueness in the audit. If all five pass, enable continuous delivery; otherwise pause immediately without deleting tasks or reservations.

- [ ] **Step 10: Run the final production soak gate**

Observe at least three consecutive successful one-minute broker wakes with no overdue eligible job, no scanner degradation, no marker conflict, no ambiguous create, no ordinary task interruption, and no duplicate source/task identity. Then rerun the complete Session Bridge suite on production main and record the exact pass counts and timestamps in the traceability audit.

- [ ] **Step 11: Commit final rollout evidence**

```powershell
git add -f docs/superpowers/audits/2026-07-30-session-sidebar-requirement-traceability.md
git commit -m "docs: record authoritative sidebar reconciliation rollout"
```

## Completion criteria

- The installed broker skill contains no `list_threads` call or independent task-discovery instruction.
- A creation-capable lease exists only after Session Bridge persists a current complete authenticated absence proof.
- `session_sidebar_reserve` performs a fresh authoritative recheck and atomically validates the resulting proof before reserving create.
- Existing exact tasks are recovered without native creation.
- Incomplete/stale scans, multiple matches, inconsistent identity, and any prior ambiguous reservation fail closed.
- At most one native create is authorized and at most one canonical task is linked per source across concurrency, crashes, and restarts.
- Project-aware `.hermes` placement, immediate exact-ID binding, readable summary/latest-five content, and source-workspace safety remain intact.
- Focused, full, recovered-task, missing-task, restart, newest-five, and production-soak gates all pass before continuous delivery remains enabled.
