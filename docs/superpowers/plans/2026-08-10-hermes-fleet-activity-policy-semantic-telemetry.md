# Hermes Fleet Activity Policy and Semantic Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a packaged, versioned activity-policy registry and audit-preserving logical-activity telemetry that distinguish scheduler completion from semantic success without changing production routing, schedules, delivery, profiles, or authority.

**Architecture:** Two focused packages establish the foundation. `activity_policy` validates an observational registry and resolves current cron names through explicit aliases; `activity_telemetry` stores immutable run identity, per-route inference usage, layered outcomes, and one terminal enrichment in a root-scoped SQLite database. Cron and `AIAgent` use narrow, best-effort adapters; `SessionDB` remains the session/model-call ledger and is linked by `session_id` rather than extended.

**Tech Stack:** Python 3.11, frozen dataclasses, PyYAML, `importlib.resources`, SQLite WAL, pytest, ruff, setuptools package data, existing Hermes `CanonicalUsage`, `EventBus`, and `SessionDB` patterns.

## Global Constraints

- Re-freeze the active main-profile inventory before implementation; do not assume the historical 66 definitions / 64 enabled counts are current.
- Classify a cron as model-capable unless both `no_agent: true` and a script are present.
- Preserve existing model, schedule, delivery, profile, and authority behavior in this workstream.
- Valid final outcomes are exactly `succeeded`, `no_work`, `blocked`, `partial`, `failed`, `budget_exhausted`, and `unknown`.
- Missing protocol, artifact, domain, or delivery evidence is `unknown`, never success.
- Record requested and served provider/model separately; model switches create distinct route-usage rows.
- Keep uncached input, cache read, cache write, output, and reasoning tokens separate.
- Keep recorded provider cost and API-equivalent estimate separate; unknown cost remains `NULL`, never zero.
- Tool allowlists fail closed and activity policy grants no authority absent from the runtime.
- Current jobs resolve through explicit observational aliases; do not mutate live or gitignored `profiles/main/cron/jobs.json` in this plan.
- Correlation ID is propagated when an external trigger supplies one; otherwise it equals the generated run ID. Cron session ID is a separate field.
- Run identity is immutable; counters are additive; terminal outcome enrichment is single-assignment. Canonical evidence is never deleted for rollback.
- Recorder and telemetry failures are bounded, sanitized, logged, and swallowed; observability cannot fail inference or cron execution.
- Delivery remains `unknown` inside `run_job`; only a downstream delivery adapter may supply delivery evidence.
- No production activation, live cohort, service restart, schedule edit, credential change, DDP decision, or live-checkout mutation is part of this plan.

---

## File map

### Policy contract and packaging

- Create `agent-src/activity_policy/__init__.py`: public policy exports.
- Create `agent-src/activity_policy/schema.py`: immutable policy types, enums, and strict validation.
- Create `agent-src/activity_policy/registry.py`: packaged YAML loading, ID lookup, and alias resolution.
- Create `agent-src/activity_policy/policies.yaml`: observational baseline declarations.
- Modify `agent-src/pyproject.toml`: include both new packages and package `policies.yaml` in wheels.
- Modify `agent-src/MANIFEST.in`: include `policies.yaml` in sdists.
- Modify `agent-src/tests/test_packaging_metadata.py`: guard package discovery and policy-data shipping.
- Create `agent-src/tests/activity_policy/test_registry.py`: schema, alias, completeness, and resource tests.
- Create `agent-src/tests/activity_policy/fixtures/enabled_model_capable_jobs.json`: reviewed, secret-free inventory fixture containing only IDs/names/classification metadata.

### Telemetry domain and persistence

- Create `agent-src/activity_telemetry/__init__.py`: public telemetry exports.
- Create `agent-src/activity_telemetry/schema.py`: run identity, layered outcomes, usage deltas, and route keys.
- Create `agent-src/activity_telemetry/store.py`: WAL store with immutable starts, additive route usage, and one terminal enrichment.
- Create `agent-src/tests/activity_telemetry/test_schema.py`: outcome derivation and validation tests.
- Create `agent-src/tests/activity_telemetry/test_store.py`: persistence, concurrency, rollback, and reopen tests.

### Runtime attribution

- Create `agent-src/activity_telemetry/recorder.py`: best-effort lifecycle facade.
- Modify `agent-src/run_agent.py`: optional recorder constructor forwarding only.
- Modify `agent-src/agent/agent_init.py`: retain optional recorder; no implicit recorder creation.
- Modify `agent-src/agent/conversation_loop.py`: record served route and canonical numeric usage independently of plugin hooks.
- Create `agent-src/tests/activity_telemetry/test_recorder.py`: failure isolation and lifecycle tests.
- Create `agent-src/tests/run_agent/test_activity_attribution.py`: constructor, no-hook, and route-switch attribution tests.

### Cron adapter and reporting

- Modify `agent-src/cron/scheduler.py`: resolve policy aliases, begin one run per fire, record deterministic terminal evidence, link session IDs, and pass the recorder to `AIAgent`.
- Create `agent-src/tests/cron/test_activity_telemetry.py`: no-work, blocked, failed, legacy, correlation, profile, and session-link tests.
- Create `agent-src/activity_telemetry/report.py`: read-only aggregation by policy, route, and outcome.
- Create `agent-src/tests/activity_telemetry/test_report.py`: read-only, Windows URI, grouping, and null-cost tests.
- Create `docs/operations/fleet-activity-policy.md`: inventory, contract, query, revision, rollout, and rollback guide.

---

### Task 1: Package a strict observational activity-policy registry

**Files:**
- Create: `agent-src/activity_policy/__init__.py`
- Create: `agent-src/activity_policy/schema.py`
- Create: `agent-src/activity_policy/registry.py`
- Create: `agent-src/activity_policy/policies.yaml`
- Create: `agent-src/tests/activity_policy/test_registry.py`
- Create: `agent-src/tests/activity_policy/fixtures/enabled_model_capable_jobs.json`
- Modify: `agent-src/pyproject.toml:339-365`
- Modify: `agent-src/MANIFEST.in`
- Modify: `agent-src/tests/test_packaging_metadata.py:35-79,113-157`

**Interfaces:**
- Consumes: `Mapping[str, Any]` from `yaml.safe_load`; package resource `activity_policy/policies.yaml`; optional current cron job name.
- Produces: `ActivityPolicy.from_mapping(activity_id: str, data: Mapping[str, Any]) -> ActivityPolicy`; `ActivityRegistry.from_mapping(data: Mapping[str, Any]) -> ActivityRegistry`; `ActivityRegistry.load(path: Path) -> ActivityRegistry`; `ActivityRegistry.load_default() -> ActivityRegistry`; `ActivityRegistry.require(activity_id: str) -> ActivityPolicy`; `ActivityRegistry.resolve(activity_id: str | None = None, alias: str | None = None) -> ActivityPolicy | None`.
- Invariant: `resolve()` returns `None` for an unmapped legacy job, but malformed or colliding registry declarations raise `PolicyError` at load time.

- [ ] **Step 1: Freeze a secret-free model-capable inventory fixture**

Run this read-only command from the repository root; it reads the canonical operational store but writes only the reviewed fixture:

```bash
python - <<'PY'
import json
from pathlib import Path
src = Path.home() / ".hermes" / "profiles" / "main" / "cron" / "jobs.json"
raw = json.loads(src.read_text(encoding="utf-8"))
jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
rows = []
for job in jobs:
    if not job.get("enabled", True):
        continue
    deterministic = job.get("no_agent") is True and bool(job.get("script"))
    if not deterministic:
        rows.append({"id": str(job["id"]), "name": str(job.get("name") or job["id"])})
out = Path("agent-src/tests/activity_policy/fixtures/enabled_model_capable_jobs.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {len(rows)} enabled model-capable jobs to {out}")
PY
```

Expected: prints the current count and writes only `id` and `name`; inspect the fixture before staging it. Do not copy prompts, delivery targets, credentials, stats, or the live store itself.

- [ ] **Step 2: Write failing registry and packaging tests**

Add tests with these exact contracts:

```python
from importlib.resources import files
from pathlib import Path
import json
import pytest

from activity_policy.registry import ActivityRegistry
from activity_policy.schema import PolicyError


def _valid_policy(**overrides):
    data = {
        "policy_version": 1,
        "owner": "jobflow",
        "aliases": ["jobflow-tailor"],
        "execution_class": "P3",
        "quality_floor": "premium",
        "preferred_models": ["claude-opus-5"],
        "allowed_fallbacks": ["claude-sonnet-5"],
        "reasoning": {"mode": "adaptive", "effort": "high"},
        "budgets": {
            "max_turns": 30,
            "max_model_calls": 8,
            "max_uncached_input_tokens": 180000,
            "max_cache_read_tokens": 900000,
            "max_cache_write_tokens": 90000,
            "max_output_tokens": 30000,
            "max_reasoning_tokens": 60000,
            "max_tool_calls": 40,
            "wall_clock_seconds": 2400,
            "retries": 1,
            "max_children": 2,
            "max_child_depth": 1,
            "max_recorded_provider_cost_usd": "5.00",
            "max_api_equivalent_cost_usd": "25.00",
        },
        "tools": {"allow": ["read_file"], "deny": ["merge", "deploy"]},
        "outcome_contract": {
            "required_artifacts": ["tailoring-brief.md"],
            "required_validations": ["factuality"],
        },
        "escalation": {"on": ["quality_gate_failed"]},
    }
    data.update(overrides)
    return data


def test_default_registry_is_observational_and_packaged():
    assert files("activity_policy").joinpath("policies.yaml").is_file()
    registry = ActivityRegistry.load_default()
    assert registry.enforcement == "observe"
    assert registry.require("jobflow.tailor.generate").owner == "jobflow"
    assert registry.resolve(alias="jobflow-tailor").activity_id == "jobflow.tailor.generate"


def test_registry_rejects_unknown_keys_and_alias_collisions():
    policy = _valid_policy(aliases=["same"], surprise=True)
    with pytest.raises(PolicyError, match="unknown keys"):
        ActivityRegistry.from_mapping({"enforcement": "observe", "activities": {"x": policy}})

    with pytest.raises(PolicyError, match="duplicate alias"):
        ActivityRegistry.from_mapping({
            "enforcement": "observe",
            "activities": {
                "x": _valid_policy(aliases=["same"]),
                "y": _valid_policy(aliases=["same"]),
            },
        })


def test_default_aliases_cover_frozen_model_capable_inventory():
    fixture = Path(__file__).parent / "fixtures" / "enabled_model_capable_jobs.json"
    jobs = json.loads(fixture.read_text(encoding="utf-8"))
    registry = ActivityRegistry.load_default()
    missing = sorted(job["name"] for job in jobs if registry.resolve(alias=job["name"]) is None)
    assert missing == []
```

Also add parameterized failures for: unknown class; non-positive `policy_version`; missing/blank owner; unknown quality floor; malformed reasoning; missing or negative/non-integer budgets; overlapping tool allow/deny; empty allowlist for a model-capable class; unknown escalation reason; duplicate activity ID after YAML construction; and D0 declarations that contain model routes or positive model-call budgets.

In `tests/test_packaging_metadata.py`, assert `activity_policy`, `activity_policy.*`, `activity_telemetry`, and `activity_telemetry.*` are present in the package-discovery include list; `activity_policy` package data contains `policies.yaml`; and `MANIFEST.in` recursively includes that YAML.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd agent-src && python -m pytest tests/activity_policy/test_registry.py tests/test_packaging_metadata.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'activity_policy'` and/or missing package metadata assertions.

- [ ] **Step 4: Implement immutable schema and fail-closed parsing**

Use these public types and constants:

```python
# activity_policy/schema.py
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

EXECUTION_CLASSES = frozenset({"D0", "S1", "S2", "P3", "P4"})
QUALITY_FLOORS = frozenset({"deterministic", "bounded", "standard", "premium", "exceptional"})
ESCALATION_REASONS = frozenset({
    "quality_gate_failed", "conflicting_evidence", "low_source_confidence",
    "high_value_application", "executive_role", "security_sensitive",
    "architecture_cross_service", "test_failure_nonlocal", "provider_unavailable",
    "context_limit_pressure",
})

class PolicyError(ValueError):
    pass

@dataclass(frozen=True)
class BudgetPolicy:
    max_turns: int
    max_model_calls: int
    max_uncached_input_tokens: int
    max_cache_read_tokens: int
    max_cache_write_tokens: int
    max_output_tokens: int
    max_reasoning_tokens: int
    max_tool_calls: int
    wall_clock_seconds: int
    retries: int
    max_children: int
    max_child_depth: int
    max_recorded_provider_cost_usd: Decimal
    max_api_equivalent_cost_usd: Decimal

@dataclass(frozen=True)
class ActivityPolicy:
    activity_id: str
    policy_version: int
    owner: str
    aliases: tuple[str, ...]
    execution_class: str
    quality_floor: str
    preferred_models: tuple[str, ...]
    allowed_fallbacks: tuple[str, ...]
    reasoning_mode: str
    reasoning_effort: str
    budgets: BudgetPolicy
    tools_allow: tuple[str, ...]
    tools_deny: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    required_validations: tuple[str, ...]
    escalation_on: tuple[str, ...]

    @classmethod
    def from_mapping(cls, activity_id: str, data: Mapping[str, Any]) -> "ActivityPolicy":
        """Validate one declaration and return its immutable policy."""
```

Implement the method body by validating exact allowed keys at every nesting level; reject booleans where integers are required; require non-negative integer budgets and strictly positive `policy_version`; parse costs through `Decimal(str(value))`; require a non-empty tool allowlist for every non-D0 class; reject allow/deny overlap; require D0 to use `quality_floor: deterministic`, no preferred/fallback models, and `max_model_calls: 0`; and reject escalation values outside `ESCALATION_REASONS`. The method must construct and return the `ActivityPolicy` shown above after all checks pass.

- [ ] **Step 5: Implement registry loading and observational alias resolution**

```python
# activity_policy/registry.py
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping
import yaml
from activity_policy.schema import ActivityPolicy, PolicyError

@dataclass(frozen=True)
class ActivityRegistry:
    enforcement: str
    policies: dict[str, ActivityPolicy]
    aliases: dict[str, str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ActivityRegistry":
        """Validate a complete document and build ID and alias indexes."""

    @classmethod
    def load(cls, path: Path) -> "ActivityRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping):
            raise PolicyError("policy document must be a mapping")
        return cls.from_mapping(raw)

    @classmethod
    def load_default(cls) -> "ActivityRegistry":
        resource = files("activity_policy").joinpath("policies.yaml")
        raw = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw)

    def require(self, activity_id: str) -> ActivityPolicy:
        try:
            return self.policies[activity_id]
        except KeyError as exc:
            raise PolicyError(f"activity policy not found: {activity_id}") from exc

    def resolve(self, activity_id: str | None = None, alias: str | None = None) -> ActivityPolicy | None:
        if activity_id:
            return self.require(activity_id)
        resolved = self.aliases.get(alias or "")
        return self.policies[resolved] if resolved else None
```

Implement `from_mapping()` without the ellipsis: accept only root keys `enforcement` and `activities`; require `enforcement == "observe"`; validate stable dotted IDs; build policies and a case-sensitive alias map; reject alias collisions with IDs or other aliases.

Populate `policies.yaml` from the approved design and reviewed inventory fixture. Use stable dotted IDs as keys and exact current job names as aliases. Include entries for enabled cron model activities and explicit auxiliary inference activities. This file is policy metadata only: do not copy prompts, credentials, delivery targets, or mutable runtime state.

- [ ] **Step 6: Add wheel and sdist metadata**

Add `activity_policy`, `activity_policy.*`, `activity_telemetry`, and `activity_telemetry.*` to `[tool.setuptools.packages.find].include`. Add:

```toml
activity_policy = ["policies.yaml"]
```

to `[tool.setuptools.package-data]`, and add this line to `MANIFEST.in`:

```text
recursive-include activity_policy policies.yaml
```

- [ ] **Step 7: Run policy, packaging, and static tests**

```bash
cd agent-src && python -m pytest tests/activity_policy/test_registry.py tests/test_packaging_metadata.py -q
```

Expected: PASS.

```bash
cd agent-src && python -m ruff check activity_policy tests/activity_policy tests/test_packaging_metadata.py
```

Expected: exit 0 with no findings.

- [ ] **Step 8: Commit the policy unit**

```bash
git add agent-src/activity_policy agent-src/tests/activity_policy agent-src/pyproject.toml agent-src/MANIFEST.in agent-src/tests/test_packaging_metadata.py && git commit -m "feat(fleet): add packaged activity policy registry"
```

Expected: one commit containing policy schema, baseline aliases, package metadata, and tests; no operational `jobs.json` changes.

---

### Task 2: Persist immutable logical runs, route usage, and terminal outcomes

**Files:**
- Create: `agent-src/activity_telemetry/__init__.py`
- Create: `agent-src/activity_telemetry/schema.py`
- Create: `agent-src/activity_telemetry/store.py`
- Create: `agent-src/tests/activity_telemetry/test_schema.py`
- Create: `agent-src/tests/activity_telemetry/test_store.py`

**Interfaces:**
- Consumes: `LogicalActivityStart`, `OutcomeLayers`, `RouteUsageDelta`; injected `clock: Callable[[], datetime]`.
- Produces: `derive_final_outcome(layers: OutcomeLayers) -> str`; `ActivityStore(db_path: Path, clock: Callable[[], datetime] = utc_now)`; `ActivityStore.start(record: LogicalActivityStart) -> None`; `ActivityStore.link_session(run_id: str, session_id: str) -> None`; `ActivityStore.record_usage(run_id: str, route: ServedRoute, delta: RouteUsageDelta) -> None`; `ActivityStore.finish(run_id: str, layers: OutcomeLayers, evidence_refs: tuple[str, ...] = (), escalation_reason: str | None = None) -> str`; `ActivityStore.get_run(run_id: str) -> dict[str, Any] | None`; `ActivityStore.get_routes(run_id: str) -> list[dict[str, Any]]`.
- Database default for adapters: `get_default_hermes_root() / "telemetry" / "activity.db"`; tests always inject a temporary path.

- [ ] **Step 1: Write failing schema tests**

```python
import pytest
from activity_telemetry.schema import OutcomeLayers, derive_final_outcome


def test_missing_semantic_and_delivery_evidence_stays_unknown():
    assert derive_final_outcome(OutcomeLayers(process="succeeded")) == "unknown"


def test_terminal_precedence_and_no_work():
    assert derive_final_outcome(OutcomeLayers(process="no_work")) == "no_work"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="failed")) == "failed"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="budget_exhausted")) == "budget_exhausted"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="blocked")) == "blocked"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="partial")) == "partial"


def test_success_requires_all_layers():
    layers = OutcomeLayers(**{name: "succeeded" for name in ("process", "protocol", "artifact", "domain", "delivery")})
    assert derive_final_outcome(layers) == "succeeded"


def test_invalid_layer_value_is_rejected():
    with pytest.raises(ValueError, match="outcome"):
        OutcomeLayers(process="green")
```

- [ ] **Step 2: Write failing persistence tests**

```python
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
import pytest

from activity_telemetry.schema import LogicalActivityStart, OutcomeLayers, RouteUsageDelta, ServedRoute
from activity_telemetry.store import ActivityStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _start(run_id="run-1"):
    return LogicalActivityStart(
        run_id=run_id, correlation_id="corr-1", activity_id="jobflow.tailor.generate",
        policy_version=1, trigger_source="cron", profile="tailor",
        effective_hermes_home="X", requested_provider="deepseek",
        requested_model="deepseek-v4-pro", session_id=None, parent_run_id=None,
        child_depth=0,
    )


def test_start_usage_link_and_single_terminal_enrichment(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    store.link_session("run-1", "cron_a_20260810_120000")
    route = ServedRoute(provider="openai-codex", model="gpt-5.6-sol")
    store.record_usage("run-1", route, RouteUsageDelta(
        turns=1, model_calls=1, tool_calls=2, retries=0,
        uncached_input_tokens=10, cache_read_tokens=90, cache_write_tokens=5,
        output_tokens=7, reasoning_tokens=3,
        recorded_provider_cost_usd=Decimal("0.12"),
        api_equivalent_cost_usd=Decimal("0.80"),
    ))
    final = store.finish("run-1", OutcomeLayers(process="succeeded"), ("session:cron_a_20260810_120000",))
    assert final == "unknown"
    row = store.get_run("run-1")
    assert row["session_id"] == "cron_a_20260810_120000"
    assert row["final_outcome"] == "unknown"
    assert row["delivery_outcome"] == "unknown"
    assert store.get_routes("run-1")[0]["cache_read_tokens"] == 90
    with pytest.raises(ValueError, match="already finished"):
        store.finish("run-1", OutcomeLayers(process="failed"))


def test_model_switches_have_distinct_route_rows(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    for provider, model in (("deepseek", "deepseek-v4-pro"), ("openai-codex", "gpt-5.6-sol")):
        store.record_usage("run-1", ServedRoute(provider, model), RouteUsageDelta(model_calls=1))
    assert [(r["served_provider"], r["served_model"]) for r in store.get_routes("run-1")] == [
        ("deepseek", "deepseek-v4-pro"), ("openai-codex", "gpt-5.6-sol")]
```

Add tests for duplicate start; immutable identity; session link set once; unknown run; negative deltas; additive concurrent usage from multiple threads; child count/depth fields; nullable costs versus zero; `Decimal` round-trip; escalation validation; JSON evidence round-trip; rollback after an injected `sqlite3.OperationalError`; and read-after-close/reopen.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd agent-src && python -m pytest tests/activity_telemetry/test_schema.py tests/activity_telemetry/test_store.py -q
```

Expected: FAIL because `activity_telemetry` does not exist.

- [ ] **Step 4: Implement validated telemetry types**

```python
# activity_telemetry/schema.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

OUTCOMES = frozenset({"succeeded", "no_work", "blocked", "partial", "failed", "budget_exhausted", "unknown"})

@dataclass(frozen=True)
class OutcomeLayers:
    process: str = "unknown"
    protocol: str = "unknown"
    artifact: str = "unknown"
    domain: str = "unknown"
    delivery: str = "unknown"

    def __post_init__(self) -> None:
        for value in (self.process, self.protocol, self.artifact, self.domain, self.delivery):
            if value not in OUTCOMES:
                raise ValueError(f"invalid outcome: {value}")

@dataclass(frozen=True)
class LogicalActivityStart:
    run_id: str
    correlation_id: str
    activity_id: str
    policy_version: int
    trigger_source: str
    profile: str
    effective_hermes_home: str
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None
    session_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    child_depth: int = 0

@dataclass(frozen=True)
class ServedRoute:
    provider: str
    model: str

@dataclass(frozen=True)
class RouteUsageDelta:
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    recorded_provider_cost_usd: Optional[Decimal] = None
    api_equivalent_cost_usd: Optional[Decimal] = None


def derive_final_outcome(layers: OutcomeLayers) -> str:
    values = (layers.process, layers.protocol, layers.artifact, layers.domain, layers.delivery)
    for terminal in ("failed", "budget_exhausted", "blocked", "partial"):
        if terminal in values:
            return terminal
    if layers.process == "no_work":
        return "no_work"
    if all(value == "succeeded" for value in values):
        return "succeeded"
    return "unknown"
```

Add `__post_init__` checks for non-empty identifiers, positive policy version, non-negative child depth and counters, and non-negative costs.

- [ ] **Step 5: Implement the WAL store and schema**

Create two tables:

```sql
CREATE TABLE IF NOT EXISTS logical_activity_runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    profile TEXT NOT NULL,
    effective_hermes_home TEXT NOT NULL,
    requested_provider TEXT,
    requested_model TEXT,
    session_id TEXT UNIQUE,
    parent_run_id TEXT,
    child_depth INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    wall_time_ms INTEGER,
    process_outcome TEXT NOT NULL DEFAULT 'unknown',
    protocol_outcome TEXT NOT NULL DEFAULT 'unknown',
    artifact_outcome TEXT NOT NULL DEFAULT 'unknown',
    domain_outcome TEXT NOT NULL DEFAULT 'unknown',
    delivery_outcome TEXT NOT NULL DEFAULT 'unknown',
    final_outcome TEXT NOT NULL DEFAULT 'unknown',
    escalation_reason TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS logical_activity_route_usage (
    run_id TEXT NOT NULL REFERENCES logical_activity_runs(run_id),
    served_provider TEXT NOT NULL,
    served_model TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    recorded_provider_cost_usd TEXT,
    api_equivalent_cost_usd TEXT,
    PRIMARY KEY (run_id, served_provider, served_model)
);
```

Follow `events/bus.py`: thread-local connections, `check_same_thread=False`, `timeout=10`, `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `journal_size_limit=33554432`, `wal_autocheckpoint=1000`, `sqlite3.Row`, and a lock around writes. Every failed write must call `rollback()` before re-raising. `finish()` computes wall time from injected UTC datetimes, derives final outcome internally, and updates only when `finished_at IS NULL`; zero updated rows must distinguish unknown run from duplicate finish. Costs are stored as decimal strings and added without float conversion.

- [ ] **Step 6: Run domain and store tests**

```bash
cd agent-src && python -m pytest tests/activity_telemetry/test_schema.py tests/activity_telemetry/test_store.py -q
```

Expected: PASS, including concurrency, rollback, and reopen cases.

```bash
cd agent-src && python -m ruff check activity_telemetry/schema.py activity_telemetry/store.py tests/activity_telemetry/test_schema.py tests/activity_telemetry/test_store.py
```

Expected: exit 0 with no findings.

- [ ] **Step 7: Commit the telemetry domain unit**

```bash
git add agent-src/activity_telemetry agent-src/tests/activity_telemetry && git commit -m "feat(fleet): persist logical activity telemetry"
```

Expected: one independently testable persistence commit.

---

### Task 3: Record per-response served routes and canonical usage in `AIAgent`

**Files:**
- Create: `agent-src/activity_telemetry/recorder.py`
- Modify: `agent-src/run_agent.py:438-587,2397-2411`
- Modify: `agent-src/agent/agent_init.py:276-349,401-507`
- Modify: `agent-src/agent/conversation_loop.py:4501-4541`
- Create: `agent-src/tests/activity_telemetry/test_recorder.py`
- Create: `agent-src/tests/run_agent/test_activity_attribution.py`

**Interfaces:**
- Consumes: optional `activity_recorder: ActivityRecorder | None`; per-response `response.model`; current `agent.provider`; sanitized dictionary returned by `_usage_summary_for_api_request_hook()`.
- Produces: `ActivityRecorder.open(...) -> ActivityRecorder`; `ActivityRecorder.link_session(session_id: str) -> None`; `ActivityRecorder.record_response(provider: str, model: str, usage: Mapping[str, Any]) -> None`; `ActivityRecorder.record_tool_call(count: int = 1) -> None`; `ActivityRecorder.record_retry(count: int = 1) -> None`; `ActivityRecorder.finish(layers: OutcomeLayers, evidence_refs: tuple[str, ...] = (), escalation_reason: str | None = None) -> str`.
- Route rule: each successful response is charged to `response.model` when present, otherwise the agent's current model; provider is the current resolved provider. Different `(provider, model)` pairs create separate route rows.

- [ ] **Step 1: Write failing recorder failure-isolation tests**

```python
from activity_telemetry.recorder import ActivityRecorder
from activity_telemetry.schema import OutcomeLayers


def test_recorder_swallowing_store_failure_never_raises(caplog):
    class BrokenStore:
        def record_usage(self, *args, **kwargs):
            raise RuntimeError("db unavailable with secret=must-not-appear")

    recorder = ActivityRecorder(store=BrokenStore(), run_id="r")
    recorder.record_response("anthropic", "claude-opus-5", {"input_tokens": 3, "request_count": 1})
    assert "must-not-appear" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_finish_process_success_remains_semantically_unknown(tmp_path):
    recorder = ActivityRecorder.open(
        tmp_path / "activity.db", run_id="r", correlation_id="r", activity_id="x",
        policy_version=1, trigger_source="test", profile="main",
        effective_hermes_home=str(tmp_path), requested_provider="deepseek",
        requested_model="deepseek-v4-pro",
    )
    assert recorder.finish(OutcomeLayers(process="succeeded")) == "unknown"
```

The production logger must emit only the operation and exception class, for example `activity telemetry record_response failed: RuntimeError`; do not include exception text because provider errors may contain credentials or response bodies.

- [ ] **Step 2: Write failing `AIAgent` attribution tests**

Add focused tests that:

1. construct `AIAgent(activity_recorder=spy)` and assert `agent.activity_recorder is spy`;
2. assert no recorder is created when omitted;
3. execute the successful response seam with no `post_api_request` plugin listener and assert one `record_response()` call;
4. assert canonical names are forwarded exactly: `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `request_count`;
5. simulate a response model switch and assert distinct served routes;
6. make `record_response()` raise and assert the conversation result is unchanged;
7. assert the recorder never receives the raw response, raw usage container, headers, API key, base URL, or credential pool.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd agent-src && python -m pytest tests/activity_telemetry/test_recorder.py tests/run_agent/test_activity_attribution.py -q
```

Expected: FAIL because `ActivityRecorder` and the constructor parameter do not exist.

- [ ] **Step 4: Implement the best-effort recorder facade**

```python
# activity_telemetry/recorder.py
class ActivityRecorder:
    def __init__(self, store: ActivityStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._lock = threading.Lock()

    @classmethod
    def open(cls, db_path: Path, **start_fields) -> "ActivityRecorder":
        store = ActivityStore(db_path)
        store.start(LogicalActivityStart(**start_fields))
        return cls(store, start_fields["run_id"])

    def record_response(self, provider: str, model: str, usage: Mapping[str, Any]) -> None:
        try:
            delta = RouteUsageDelta(
                turns=1,
                model_calls=int(usage.get("request_count") or 1),
                uncached_input_tokens=int(usage.get("input_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            )
            with self._lock:
                self.store.record_usage(self.run_id, ServedRoute(provider, model), delta)
        except Exception as exc:
            logger.warning("activity telemetry record_response failed: %s", type(exc).__name__)
```

Implement `open`, `link_session`, `record_tool_call`, `record_retry`, and `finish` with the same sanitized best-effort boundary. `open()` may raise so the cron adapter can decide whether to proceed without a recorder; methods on an existing recorder must not raise into inference.

- [ ] **Step 5: Thread the optional recorder through both constructor layers**

Add `activity_recorder=None` to `AIAgent.__init__` in `run_agent.py` and to `init_agent()` in `agent/agent_init.py`. Forward it in the `init_agent(...)` call and assign:

```python
agent.activity_recorder = activity_recorder
```

Do not instantiate a recorder, open SQLite, or resolve policy inside `AIAgent`.

- [ ] **Step 6: Record usage outside the plugin-hook gate**

Immediately before the existing `try: from hermes_cli.plugins ...` block in `conversation_loop.py`, compute the sanitized summary once and make the recorder call independent of `has_hook("post_api_request")`:

```python
_usage_summary = agent._usage_summary_for_api_request_hook(response)
if agent.activity_recorder is not None and _usage_summary:
    try:
        agent.activity_recorder.record_response(
            provider=agent.provider or "unknown",
            model=str(getattr(response, "model", None) or agent.model or "unknown"),
            usage=_usage_summary,
        )
    except Exception as exc:
        logger.warning("activity recorder callback failed: %s", type(exc).__name__)
```

Reuse `_usage_summary` as the `usage=` value passed to the existing plugin hook. Do not place recorder logic inside `if has_hook(...)`. Do not use stale keys `cache_read_input_tokens` or `cache_creation_input_tokens`; `CanonicalUsage` exposes `cache_read_tokens` and `cache_write_tokens`.

- [ ] **Step 7: Run attribution and existing usage/fallback regressions**

```bash
cd agent-src && python -m pytest tests/activity_telemetry/test_recorder.py tests/run_agent/test_activity_attribution.py tests/run_agent/test_iteration_budget_race.py tests/agent/test_usage_pricing.py tests/hermes_state/test_aux_usage_accounting.py -q
```

Expected: PASS.

```bash
cd agent-src && python -m pytest tests/run_agent/test_provider_fallback.py tests/run_agent/test_switch_model_fallback_prune.py tests/run_agent/test_run_agent.py -q
```

Expected: PASS; the no-listener fast path and provider fallback behavior remain unchanged.

- [ ] **Step 8: Commit the runtime attribution unit**

```bash
git add agent-src/activity_telemetry/recorder.py agent-src/run_agent.py agent-src/agent/agent_init.py agent-src/agent/conversation_loop.py agent-src/tests/activity_telemetry/test_recorder.py agent-src/tests/run_agent/test_activity_attribution.py && git commit -m "feat(fleet): attribute per-route model usage"
```

Expected: one runtime-attribution commit with no routing changes.

---

### Task 4: Attach observational telemetry to cron lifecycle branches

**Files:**
- Modify: `agent-src/cron/scheduler.py:3887-4158,4395-4489,4605-4636,4816-4993`
- Create: `agent-src/tests/cron/test_activity_telemetry.py`

**Interfaces:**
- Consumes: `ActivityRegistry.load_default()`; explicit `job.activity_id` when present, otherwise exact job-name alias; optional `job.correlation_id`; current profile/effective Hermes home; generated run/session IDs; optional `ActivityRecorder`.
- Produces: one logical activity run per mapped fire; `no_work`, `blocked`, `failed`, or process-only `succeeded` evidence; linked session ID only when inference crosses the current wake/prompt boundary.
- Compatibility: unmapped legacy jobs and every telemetry error execute exactly as before.

- [ ] **Step 1: Write failing mapping, correlation, and compatibility tests**

```python
from cron import scheduler


def test_unmapped_legacy_job_runs_without_telemetry(monkeypatch):
    opened = []
    monkeypatch.setattr(scheduler, "_open_cron_activity", lambda *a, **k: opened.append((a, k)))
    job = {"id": "legacy", "name": "not-in-registry", "no_agent": True, "script": "x"}
    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", lambda *a: (True, ""))
    ok, _, _, error = scheduler._run_job_impl(job)
    assert ok and error is None
    assert opened == []


def test_external_correlation_is_distinct_from_session(monkeypatch):
    opened = []
    monkeypatch.setattr(scheduler, "_open_cron_activity", lambda **kw: opened.append(kw) or None)
    # Use the existing scheduler fixture to stop after the wakeAgent:false gate.
    job = {"id": "a", "name": "mapped-job", "script": "x", "correlation_id": "event-123"}
    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", lambda *a: (True, '{"wakeAgent": false}'))
    scheduler._run_job_impl(job)
    assert opened[0]["correlation_id"] == "event-123"
    assert opened[0]["session_id"] is None
```

Use a fixture registry rather than the production YAML for exact unit isolation.

- [ ] **Step 2: Write failing terminal-evidence tests for every early branch**

Cover these exact outcomes:

| Existing branch | Process layer | Final outcome | Evidence |
|---|---:|---:|---|
| `no_agent` script failure | `failed` | `failed` | `script_failed` |
| `no_agent` `wakeAgent:false` | `no_work` | `no_work` | `wakeAgent:false` |
| `no_agent` empty stdout | `no_work` | `no_work` | `empty_stdout` |
| `no_agent` non-empty success | `succeeded` | `unknown` | `script_completed` |
| hybrid `wakeAgent:false` | `no_work` | `no_work` | `wakeAgent:false` |
| prompt injection blocked | `blocked` | `blocked` | `prompt_injection_blocked` |
| `_build_job_prompt()` returns `None` | `no_work` | `no_work` | `empty_prompt` |
| model loop failure | `failed` | `failed` | existing bounded error category |
| ordinary model completion | `succeeded` | `unknown` | `session:<session_id>` |

Assert delivery remains `unknown` for every row. Also assert recorder open/finish failures do not alter the existing `(success, full_output_doc, final_response, error_message)` tuple.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd agent-src && python -m pytest tests/cron/test_activity_telemetry.py -q
```

Expected: FAIL because the cron telemetry adapter does not exist.

- [ ] **Step 4: Add private policy and recorder helpers**

Implement helpers with these signatures near `run_job`:

```python
def _resolve_cron_activity_policy(job: dict) -> ActivityPolicy | None:
    """Resolve explicit activity_id first, then exact observational job-name alias."""


def _open_cron_activity(
    *, job: dict, policy: ActivityPolicy, run_id: str, correlation_id: str,
    profile: str, effective_hermes_home: str,
) -> ActivityRecorder | None:
    """Best-effort open; sanitize failures to exception class and return None."""


def _finish_cron_activity(
    recorder: ActivityRecorder | None, *, process: str,
    evidence_refs: tuple[str, ...] = (), escalation_reason: str | None = None,
) -> None:
    """Best-effort single terminal enrichment; never alter cron control flow."""
```

Cache the immutable packaged registry once per process, but if initial loading fails, log only the exception class and behave as unmapped. Use `get_default_hermes_root() / "telemetry" / "activity.db"` for the store. Resolve `effective_hermes_home` inside `_job_profile_context` so the stored path reflects the job profile; do not use `get_default_hermes_root()` as the profile attribution value.

- [ ] **Step 5: Begin one run before deterministic work**

At the start of `_run_job_impl`, resolve policy. If mapped, generate a UUID run ID and set:

```python
correlation_id = str(job.get("correlation_id") or run_id)
```

Open the recorder before the `no_agent` branch. Do not create `SessionDB` or a cron session for deterministic/no-work branches. Call `_finish_cron_activity()` immediately before each early return using the table in Step 2.

- [ ] **Step 6: Link inference to the existing cron session and pass the recorder**

Keep `_cron_session_id` creation after wake and prompt gates, at the current inference boundary. Once generated:

```python
if activity_recorder is not None:
    activity_recorder.link_session(_cron_session_id)
```

Pass `activity_recorder=activity_recorder` to the existing `AIAgent(...)` constructor. Requested provider/model come from the scheduler's pre-agent requested route; served route comes only from response recording in `conversation_loop.py`. Do not infer served route from requested configuration.

- [ ] **Step 7: Finish model branches without claiming semantic or delivery success**

On ordinary model-loop completion, finish with only `process="succeeded"` and `evidence_refs=(f"session:{_cron_session_id}",)`, yielding final `unknown`. On existing failure/timeout paths use `process="failed"`. Preserve the existing return tuple, SessionDB completion, delivery ordering, agent teardown, and cleanup behavior. Do not add delivery outcome writes in this task.

- [ ] **Step 8: Run cron regression suite**

```bash
cd agent-src && python -m pytest tests/cron/test_activity_telemetry.py tests/cron/test_cron_script.py tests/cron/test_cron_no_agent.py tests/cron/test_script_claim_heartbeat.py tests/cron/test_cron_profile.py tests/cron/test_cron_profile_isolation.py tests/cron/test_scheduler.py -q
```

Expected: PASS; existing script outputs, final responses, errors, delivery assertions, profile isolation, and session behavior remain unchanged.

- [ ] **Step 9: Run static checks and commit the cron adapter**

```bash
cd agent-src && python -m ruff check cron/scheduler.py tests/cron/test_activity_telemetry.py
```

Expected: exit 0 with no findings.

```bash
git add agent-src/cron/scheduler.py agent-src/tests/cron/test_activity_telemetry.py && git commit -m "feat(cron): record observational activity outcomes"
```

Expected: one independently revertible cron-adapter commit.

---

### Task 5: Add read-only fleet reporting and operator documentation

**Files:**
- Create: `agent-src/activity_telemetry/report.py`
- Create: `agent-src/tests/activity_telemetry/test_report.py`
- Create: `docs/operations/fleet-activity-policy.md`

**Interfaces:**
- Consumes: an existing activity telemetry database and an ISO-8601 lower bound.
- Produces: `summarize(db_path: Path, since: str) -> list[dict[str, Any]]`, grouped by activity ID, policy version, requested provider/model, served provider/model, and final outcome.
- Read guarantee: open SQLite through a URI with `mode=ro`; never create, migrate, checkpoint, or write the database.

- [ ] **Step 1: Write failing aggregation tests**

```python
from activity_telemetry.report import summarize


def test_summary_keeps_unknown_success_and_routes_separate(seed_activity_db):
    rows = summarize(seed_activity_db, since="2026-08-01T00:00:00+00:00")
    keyed = {
        (r["activity_id"], r["policy_version"], r["served_model"], r["final_outcome"]): r
        for r in rows
    }
    assert keyed[("x", 1, "deepseek-v4-pro", "unknown")]["runs"] == 1
    assert keyed[("x", 2, "gpt-5.6-sol", "succeeded")]["runs"] == 1
    assert keyed[("x", 1, "deepseek-v4-pro", "unknown")]["cache_read_tokens"] == 90
```

Add tests for: missing database; malformed/naive `since`; Windows paths containing spaces and `#`; requested and served route differences; multiple served routes in one run; multiple policy versions; null wall time; null recorded/API-equivalent costs; zero cost distinct from unknown; explicit selected/grouped columns; and proof that no database or WAL file is created/changed by reporting.

- [ ] **Step 2: Run report tests and verify RED**

```bash
cd agent-src && python -m pytest tests/activity_telemetry/test_report.py -q
```

Expected: FAIL because `activity_telemetry.report` does not exist.

- [ ] **Step 3: Implement bounded read-only aggregation**

Validate `since` with `datetime.fromisoformat`; require timezone awareness; normalize it to UTC. Build the SQLite URI with `db_path.resolve().as_uri() + "?mode=ro"` and `sqlite3.connect(uri, uri=True)`. Use one parameterized query with explicit columns and grouping. Return these fields:

```python
{
    "activity_id": str,
    "policy_version": int,
    "requested_provider": str | None,
    "requested_model": str | None,
    "served_provider": str | None,
    "served_model": str | None,
    "final_outcome": str,
    "runs": int,
    "semantic_successes": int,
    "unknowns": int,
    "turns": int,
    "model_calls": int,
    "tool_calls": int,
    "retries": int,
    "uncached_input_tokens": int,
    "cache_read_tokens": int,
    "cache_write_tokens": int,
    "output_tokens": int,
    "reasoning_tokens": int,
    "recorded_provider_cost_usd": str | None,
    "api_equivalent_cost_usd": str | None,
    "average_wall_time_ms": float | None,
}
```

A run with multiple served routes contributes route usage to each route row, but `runs` must count distinct run IDs. Do not label process-only completion as semantic success.

- [ ] **Step 4: Write the operator contract**

Document in `docs/operations/fleet-activity-policy.md`:

1. canonical live inventory location and the read-only fixture refresh command from Task 1;
2. classification rule for deterministic versus model-capable jobs;
3. policy schema, owners, aliases, versions, fail-closed tools, standard escalation reasons, and `enforcement: observe`;
4. activity DB path and tables;
5. immutable identity, additive route usage, single terminal enrichment, correlation/session/parent distinctions;
6. outcome layers and the `unknown` rule;
7. requested versus served route and per-route switch semantics;
8. token and cost dimensions;
9. read-only report invocation with an example ISO timestamp;
10. explicit non-goals: no routing, budget, scheduling, authority, or delivery change;
11. policy revision procedure: edit YAML, bump `policy_version`, update fixture/coverage tests, rerun package checks;
12. rollout gate: fixtures first; a bounded shadow cohort requires separate runtime authorization;
13. rollback: disable/revert the cron adapter while preserving the activity DB and SessionDB evidence.

- [ ] **Step 5: Run the complete foundation verification**

```bash
cd agent-src && python -m pytest tests/activity_policy tests/activity_telemetry tests/cron/test_activity_telemetry.py tests/test_packaging_metadata.py -q
```

Expected: PASS.

```bash
cd agent-src && python -m ruff check activity_policy activity_telemetry tests/activity_policy tests/activity_telemetry tests/cron/test_activity_telemetry.py
```

Expected: exit 0 with no findings.

```bash
cd agent-src && python -m build --wheel --sdist
```

Expected: exit 0 and create wheel/sdist artifacts under `agent-src/dist/`.

```bash
cd agent-src && python - <<'PY'
from pathlib import Path
import zipfile
wheel = max(Path("dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
with zipfile.ZipFile(wheel) as zf:
    names = set(zf.namelist())
assert "activity_policy/policies.yaml" in names
assert any(name == "activity_policy/schema.py" for name in names)
assert any(name == "activity_telemetry/store.py" for name in names)
print(f"verified packaged fleet foundation in {wheel.name}")
PY
```

Expected: prints the verified wheel name. Remove only newly generated untracked `agent-src/dist/` artifacts after inspection; do not delete pre-existing files.

- [ ] **Step 6: Commit reporting and documentation**

```bash
git add agent-src/activity_telemetry/report.py agent-src/tests/activity_telemetry/test_report.py docs/operations/fleet-activity-policy.md && git commit -m "docs(fleet): add semantic telemetry reporting"
```

Expected: one reporting/docs commit; no live activation.

---

## Release and rollback gate

This plan ends at a tested, packaged, observational foundation. It does **not** authorize enabling telemetry in a live gateway, modifying current cron definitions, enforcing policy, changing models, changing schedules, changing delivery, or restarting services. A later, separately authorized rollout may shadow-record one bounded cohort and must verify that missing semantic layers remain visibly `unknown`. Rollback is independently reverting or disabling the optional cron adapter while retaining the audit-preserving activity database and existing `SessionDB` rows.
