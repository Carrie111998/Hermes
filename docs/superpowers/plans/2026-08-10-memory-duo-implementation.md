# Memory Duo v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight, durable Hermes + Obsidian memory system for the Autopilot profile that keeps Hermes hot memory/session history intact, uses Obsidian as deep knowledge, inherits the active Hermes session model for optional intelligence, and never silently falls over to a different paid provider.

**Architecture:** Implement `obsidian_duo` as a memory-provider plugin with a thin `MemoryProvider` adapter and a service-ready `MemoryBrokerClient` boundary backed by an embedded broker in v1. The broker uses SQLite + FTS5, normal Markdown/frontmatter, deterministic retrieval first, one bounded worker queue, optional host-owned plugin LLM calls, and explicit provenance/conflict/recovery state. Obsidian-specific code stays inside the plugin; Hermes core changes are limited to generic plugin-LLM parity, context propagation, and a restrictive auxiliary fallback policy that benefits any trusted plugin.

**Tech Stack:** Python stdlib (`sqlite3`, `queue`, `threading`, `hashlib`, `tempfile`, `pathlib`, `contextvars`), existing PyYAML dependency, Hermes `MemoryProvider`, `MemoryManager`, `PluginLlm`, `auxiliary_client`, SQLite FTS5, Markdown/YAML frontmatter.

## Global Constraints

- Primary deployment is a remote Hermes backend; Hermes Desktop on Windows is only the client.
- Client machine has Windows 11 Pro, a 14th-gen Intel Core i5, 16 GB DDR5 and no dedicated GPU.
- Required path must use no local LLM, no local embedding model, no CUDA, no PyTorch, no vector database and no graph database.
- Required path must add zero Memory Duo background services on the Windows client.
- V1 broker runs embedded, not as a separate daemon, but all callers go through a stable `MemoryBrokerClient` contract.
- SQLite + FTS5 is the only required broker database.
- `MEMORY.md`/`USER.md` remain Hermes hot memory; SessionDB/Graphify remain episodic evidence; Obsidian is deep curated knowledge.
- Obsidian durable writes are controlled by the broker; subagents may only propose candidates.
- Hermes may read the configured vault, but automatic memory writes are restricted to the managed `Hermes Memory/` area.
- Manual user edits to managed Obsidian notes are authoritative.
- Durable conflicts use append-first/supersession history; important memories are not silently overwritten.
- Secrets and credentials must never be persisted as memory or diagnostic text.
- Retrieval is deterministic first: scope/metadata → FTS5 → links/relationships → authority/verification/importance/recency.
- LLM assistance is optional and must use Hermes host-owned plugin LLM access.
- Default LLM route is the currently active Hermes session provider/model, with no provider/model override.
- Memory Duo LLM calls use restrictive same-provider-only fallback; an unavailable/rate-limited free route causes deterministic degradation or deferred work, not an automatic paid-provider switch.
- No mandatory embedding API.
- No model call for trivial, exact or ordinary structured recall when deterministic retrieval is sufficient.
- `sync_turn()` remains non-blocking from the main conversation path.
- Candidate/event queues are bounded; no unbounded RAM growth during long Autopilot jobs.
- Indexing is incremental after first cataloguing; unchanged Markdown is not reparsed.
- Managed note writes use temp-file + fsync + atomic replacement where supported.
- Multi-step mutations are journaled and recoverable after process death.
- Deep-memory failure never prevents Hermes chat/agent operation.
- Preserve Hermes prompt-cache invariants and do not mutate the system prompt mid-session.
- Keep model-tool footprint narrow: one provider-gated `memory_duo` tool at most.
- Behavioral config belongs in `config.yaml` / provider JSON, not new non-secret environment variables.
- Every task uses test-first development and ends with an independently reviewable commit.

---

## File Structure

### Generic Hermes changes

- Modify `plugins/memory/__init__.py` — give memory-provider `register(ctx)` the same host-owned `ctx.llm` facade that normal plugins receive.
- Modify `agent/plugin_llm.py` — add a restrictive `fallback_policy="same_provider_only"` option without changing the current default.
- Modify `agent/auxiliary_client.py` — honor the restrictive fallback policy while retaining same-provider retry/credential refresh.
- Modify `agent/memory_manager.py` — preserve `contextvars` when provider background work is submitted.
- Modify `website/docs/developer-guide/plugin-llm-access.md` — document restrictive fallback policy.
- Modify `website/docs/developer-guide/memory-provider-plugin.md` — document `ctx.llm` availability to `register(ctx)` memory plugins.
- Modify `tests/agent/test_plugin_llm.py`, `tests/agent/test_auxiliary_client.py`, `tests/agent/test_memory_provider.py` — generic behavior tests.

### Memory Duo plugin

Create under `plugins/memory/obsidian_duo/` in this private fork. Keep every Obsidian-specific dependency in this directory so the plugin can later be copied to `$HERMES_HOME/plugins/obsidian_duo/` as a standalone user plugin.

- `__init__.py` — `ObsidianDuoMemoryProvider`, provider hooks, one gated tool, registration.
- `plugin.yaml` — plugin metadata.
- `config.py` — `ObsidianDuoConfig`, config load/save/validation.
- `contracts.py` — typed broker, candidate, packet, status and event contracts.
- `client.py` — `MemoryBrokerClient` protocol and embedded client implementation.
- `broker.py` — orchestration of store/vault/policy/retrieval/inference/queue/recovery.
- `store.py` — SQLite schema, migrations, WAL configuration, transactions and FTS5.
- `vault.py` — Markdown/frontmatter parser, manifest, note hashing, atomic writes, manual-edit detection.
- `security.py` — deterministic secret/credential scanner and redaction.
- `policy.py` — candidate classification, authority/verification, promotion, supersession/conflict rules.
- `retrieval.py` — deterministic candidate retrieval, ranking and packet assembly.
- `inference.py` — optional `ctx.llm` structured calls using the active session route and restrictive fallback.
- `sync.py` — no-op + debounced command sync adapters; no permanent process required.
- `cli.py` — status/doctor/rebuild-index/reconcile/pending/conflicts/stats commands.
- `README.md` — setup, remote-backend topology, config and recovery docs.

### Tests

- `tests/plugins/memory/test_obsidian_duo_config.py`
- `tests/plugins/memory/test_obsidian_duo_store.py`
- `tests/plugins/memory/test_obsidian_duo_vault.py`
- `tests/plugins/memory/test_obsidian_duo_security.py`
- `tests/plugins/memory/test_obsidian_duo_policy.py`
- `tests/plugins/memory/test_obsidian_duo_retrieval.py`
- `tests/plugins/memory/test_obsidian_duo_inference.py`
- `tests/plugins/memory/test_obsidian_duo_broker.py`
- `tests/plugins/memory/test_obsidian_duo_provider.py`
- `tests/plugins/memory/test_obsidian_duo_cli.py`
- `tests/plugins/memory/test_obsidian_duo_sync.py`
- `tests/plugins/memory/test_obsidian_duo_e2e.py`
- `tests/plugins/memory/test_obsidian_duo_resources.py`

### Approved docs

- Add `docs/superpowers/specs/2026-08-10-memory-duo-design.md` from the approved design document.
- Add `docs/superpowers/plans/2026-08-10-memory-duo-implementation.md` from this plan.

---

### Task 1: Give memory plugins host-owned `ctx.llm`

**Files:**
- Modify: `plugins/memory/__init__.py`
- Modify: `website/docs/developer-guide/memory-provider-plugin.md`
- Modify: `tests/agent/test_memory_provider.py`

**Interfaces:**
- Consumes: `agent.plugin_llm.PluginLlm(plugin_id=<provider-directory-name>)`
- Produces: `_ProviderCollector(plugin_id: str).llm: PluginLlm`; memory plugin `register(ctx)` can capture `ctx.llm` without seeing API keys.

- [ ] **Step 1: Write the failing collector test**

Add to `tests/agent/test_memory_provider.py`:

```python
from agent.plugin_llm import PluginLlm
from plugins.memory import _ProviderCollector

def test_memory_provider_register_context_exposes_host_owned_llm():
    collector = _ProviderCollector(plugin_id="obsidian_duo")
    assert isinstance(collector.llm, PluginLlm)
    assert collector.llm._plugin_id == "obsidian_duo"
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
python -m pytest tests/agent/test_memory_provider.py::test_memory_provider_register_context_exposes_host_owned_llm -q
```

Expected: failure because `_ProviderCollector` does not accept `plugin_id` and has no `llm`.

- [ ] **Step 3: Implement the minimal generic context parity**

Change the `register(ctx)` branch in `plugins/memory/__init__.py` from a no-argument collector to:

```python
collector = _ProviderCollector(plugin_id=name)
```

Change `_ProviderCollector` to:

```python
class _ProviderCollector:
    def __init__(self, plugin_id: str):
        from agent.plugin_llm import PluginLlm
        self.provider = None
        self.llm = PluginLlm(plugin_id=plugin_id)

    def register_memory_provider(self, provider):
        self.provider = provider

    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass
```

Do not expose credentials on the collector.

- [ ] **Step 4: Add a real temporary-plugin loading test**

Create a test in `tests/agent/test_memory_provider.py` that writes a user memory plugin under a temporary `HERMES_HOME/plugins/demo_memory/`:

```python
PLUGIN = '''
from agent.memory_provider import MemoryProvider

class Demo(MemoryProvider):
    def __init__(self, llm):
        self.llm = llm
    @property
    def name(self): return "demo_memory"
    def is_available(self): return True
    def initialize(self, session_id, **kwargs): pass
    def get_tool_schemas(self): return []

def register(ctx):
    ctx.register_memory_provider(Demo(ctx.llm))
'''
```

Load it through `plugins.memory.load_memory_provider("demo_memory")` and assert `isinstance(provider.llm, PluginLlm)`.

- [ ] **Step 5: Run memory-provider tests**

```bash
python -m pytest tests/agent/test_memory_provider.py -q
```

Expected: pass.

- [ ] **Step 6: Document the generic surface**

In `website/docs/developer-guide/memory-provider-plugin.md`, add a short section stating that `register(ctx)` memory plugins receive `ctx.llm`, that credentials remain host-owned, and that default calls inherit the live main runtime.

- [ ] **Step 7: Commit**

```bash
git add plugins/memory/__init__.py tests/agent/test_memory_provider.py website/docs/developer-guide/memory-provider-plugin.md
git commit -m "feat(memory): expose host-owned llm to memory plugins"
```

---

### Task 2: Add restrictive same-provider-only plugin LLM fallback

**Files:**
- Modify: `agent/plugin_llm.py`
- Modify: `agent/auxiliary_client.py`
- Modify: `website/docs/developer-guide/plugin-llm-access.md`
- Modify: `tests/agent/test_plugin_llm.py`
- Modify: `tests/agent/test_auxiliary_client.py`

**Interfaces:**
- Produces: `PluginLlm.complete(..., fallback_policy="host" | "same_provider_only")`
- Produces: `PluginLlm.complete_structured(..., fallback_policy=...)` and async equivalents.
- Produces: `call_llm(..., fallback_policy="host" | "same_provider_only")` and `async_call_llm(...)`.
- `host` preserves existing behavior exactly.
- `same_provider_only` permits retries/credential refresh on the resolved active provider but prohibits alternate provider/model fallback chains.

- [ ] **Step 1: Write PluginLlm forwarding tests**

Add tests to `tests/agent/test_plugin_llm.py` using the existing injected caller pattern. Use the file's existing fake-response helper rather than creating a parallel response model:

```python
def test_complete_forwards_same_provider_only_policy():
    seen = {}
    def caller(**kwargs):
        seen.update(kwargs)
        return "openrouter", "deepseek/deepseek-v4-flash", fake_response("ok")
    llm = make_plugin_llm_for_test(
        plugin_id="obsidian_duo",
        policy=_TrustPolicy(plugin_id="obsidian_duo"),
        sync_caller=caller,
    )
    result = llm.complete(
        [{"role": "user", "content": "classify"}],
        fallback_policy="same_provider_only",
    )
    assert result.text == "ok"
    assert seen["fallback_policy"] == "same_provider_only"
```

Mirror this for `complete_structured`, `acomplete` and `acomplete_structured`.

- [ ] **Step 2: Run those tests and verify failure**

```bash
python -m pytest tests/agent/test_plugin_llm.py -q
```

Expected: new tests fail because the keyword is not accepted/forwarded.

- [ ] **Step 3: Add and validate the public keyword**

In `agent/plugin_llm.py` add:

```python
_ALLOWED_FALLBACK_POLICIES = {"host", "same_provider_only"}

def _validate_fallback_policy(value: str) -> str:
    value = str(value or "host").strip().lower()
    if value not in _ALLOWED_FALLBACK_POLICIES:
        raise ValueError(
            "fallback_policy must be 'host' or 'same_provider_only'"
        )
    return value
```

Add keyword-only `fallback_policy: str = "host"` to all four public methods and to `_invoke_sync`/`_invoke_async`. Forward it to the auxiliary caller. Do not treat a restrictive policy as a provider/model override.

- [ ] **Step 4: Add auxiliary no-cross-provider tests**

In `tests/agent/test_auxiliary_client.py`, reuse that file's existing fake-client/fallback helpers. Add one sync and one async test where the active/main client raises a simulated credit-exhaustion error, a secondary fallback candidate is available, `fallback_policy="same_provider_only"` is supplied, the secondary provider spy is never called, and the original/recovered same-provider error is surfaced after permitted retry is exhausted.

Add a control test proving `fallback_policy="host"` still reaches the existing fallback path.

- [ ] **Step 5: Implement the guard centrally in `auxiliary_client`**

Add keyword-only `fallback_policy: str = "host"` to `call_llm` and `async_call_llm`. Validate it once near function entry:

```python
same_provider_only = fallback_policy == "same_provider_only"
```

Keep all same-provider credential refresh/rebuild/retry logic unchanged.

Guard every path that selects a *different* provider/model with:

```python
if same_provider_only:
    raise original_or_recovered_error
```

The guard must cover configured task fallback chains, top-level main fallback providers and hardcoded auto-discovery fallthrough after the selected main route has failed. Do not disable rebuilding the same provider after OAuth/key refresh.

- [ ] **Step 6: Run focused suites**

```bash
python -m pytest tests/agent/test_plugin_llm.py tests/agent/test_auxiliary_client.py -q
```

Expected: pass, including existing fallback behavior.

- [ ] **Step 7: Document the policy**

Document that `same_provider_only` is intended for cost-sensitive plugin work: it follows the current active route but refuses cross-provider fallback.

- [ ] **Step 8: Commit**

```bash
git add agent/plugin_llm.py agent/auxiliary_client.py tests/agent/test_plugin_llm.py tests/agent/test_auxiliary_client.py website/docs/developer-guide/plugin-llm-access.md
git commit -m "feat(plugins): add same-provider-only llm fallback policy"
```

---

### Task 3: Preserve session runtime context in memory background work

**Files:**
- Modify: `agent/memory_manager.py`
- Modify: `agent/auxiliary_client.py` — adapt the existing process-global runtime override to a ContextVar-backed set/reset/clear surface so copied worker contexts retain the submitting route.
- Modify: `tests/agent/test_memory_provider.py`

**Interfaces:**
- `MemoryManager._submit_background()` captures `contextvars.copy_context()` at submission.
- Provider `sync_turn`, queued prefetch and session-boundary work see the exact `auxiliary_client` runtime context of the submitting Hermes turn.

- [ ] **Step 1: Write the failing propagation test**

Add a provider to `tests/agent/test_memory_provider.py` whose `sync_turn()` reads:

```python
from agent.auxiliary_client import _read_main_provider, _read_main_model
self.seen_runtime = (_read_main_provider(), _read_main_model())
```

Test:

```python
from agent.auxiliary_client import set_runtime_main, reset_runtime_main

def test_background_sync_inherits_submitting_session_runtime():
    token = set_runtime_main(
        "opencode-zen",
        "deepseek/deepseek-v4-flash",
        base_url="https://example.invalid/v1",
        api_key="test-only",
        api_mode="chat_completions",
        session_id="session-a",
    )
    try:
        mgr = MemoryManager()
        provider = RuntimeReadingProvider("external")
        mgr.add_provider(provider)
        mgr.sync_all("u", "a", session_id="session-a")
        assert mgr.flush_pending(timeout=5)
        assert provider.seen_runtime == (
            "opencode-zen",
            "deepseek/deepseek-v4-flash",
        )
    finally:
        reset_runtime_main(token)
```

- [ ] **Step 2: Verify failure**

```bash
python -m pytest tests/agent/test_memory_provider.py::test_background_sync_inherits_submitting_session_runtime -q
```

Expected: background thread cannot see the submitting context.

- [ ] **Step 3: Capture context at submission**

Import `contextvars` in `agent/memory_manager.py`. In `_submit_background`, capture before `executor.submit`:

```python
submission_context = contextvars.copy_context()
```

Submit:

```python
future = executor.submit(submission_context.run, fn)
```

The inline fallback path continues to call `fn()` directly because it is already in the caller context.

- [ ] **Step 4: Add an isolation test**

Set runtime A, submit a blocking background job, switch the caller to runtime B, then release A. Assert the job still sees A. This proves a later gateway turn cannot overwrite an earlier memory job's route.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/agent/test_memory_provider.py tests/agent/test_auxiliary_runtime_cache_key.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add agent/memory_manager.py tests/agent/test_memory_provider.py
git commit -m "fix(memory): preserve runtime context in background provider work"
```

---

### Task 4: Create Memory Duo plugin contracts and configuration

**Files:**
- Create: `plugins/memory/obsidian_duo/__init__.py`
- Create: `plugins/memory/obsidian_duo/plugin.yaml`
- Create: `plugins/memory/obsidian_duo/config.py`
- Create: `plugins/memory/obsidian_duo/contracts.py`
- Create: `plugins/memory/obsidian_duo/client.py`
- Create: `tests/plugins/memory/test_obsidian_duo_config.py`
- Create: `tests/plugins/memory/test_obsidian_duo_provider.py`

**Interfaces:**
- `ObsidianDuoConfig.load(hermes_home: Path) -> ObsidianDuoConfig`
- `ObsidianDuoConfig.save(hermes_home: Path) -> None`
- `MemoryBrokerClient` protocol:
  - `retrieve(request: RetrievalRequest) -> MemoryPacket`
  - `observe(event: MemoryEvent) -> None`
  - `propose(candidate: MemoryCandidate) -> CandidateDecision`
  - `flush(reason: str, timeout: float) -> bool`
  - `status() -> BrokerStatus`
  - `shutdown(timeout: float) -> None`
- `EmbeddedMemoryBrokerClient` delegates to an embedded broker object.
- Provider name is `obsidian_duo`.

- [ ] **Step 1: Write config tests**

```python
def test_config_defaults_are_lightweight(tmp_path):
    cfg = ObsidianDuoConfig(vault_path=str(tmp_path / "Vault"))
    assert cfg.managed_folder == "Hermes Memory"
    assert cfg.index_mode == "lazy"
    assert cfg.sync_mode == "none"
    assert cfg.inference_mode == "inherit_session"
    assert cfg.cost_policy == "no_paid_fallback"
    assert cfg.queue_maxsize == 256
    assert cfg.recall_max_memories == 12
    assert cfg.recall_max_tokens == 5000
```

Test save/load under `HERMES_HOME/obsidian_duo.json` and ensure the JSON contains no API-key field.

- [ ] **Step 2: Verify config tests fail**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_config.py -q
```

- [ ] **Step 3: Implement config**

Use:

```python
@dataclass
class ObsidianDuoConfig:
    vault_path: str
    managed_folder: str = "Hermes Memory"
    index_mode: str = "lazy"
    sync_mode: str = "none"
    sync_command: tuple[str, ...] = ()
    sync_debounce_seconds: float = 30.0
    inference_mode: str = "inherit_session"
    cost_policy: str = "no_paid_fallback"
    queue_maxsize: int = 256
    recall_max_memories: int = 12
    recall_max_tokens: int = 5000
    managed_scan_min_interval_seconds: float = 5.0
```

Validate enum-like fields and positive bounds. Save atomically using an existing repo JSON helper if available; otherwise use temp + `os.replace`.

- [ ] **Step 4: Implement typed contracts**

Use dataclasses/enums only; do not add Pydantic.

```python
class MemoryStatus(str, Enum):
    ACTIVE = "active"
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    NEEDS_ATTENTION = "needs_attention"

class Verification(str, Enum):
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    SOURCE_SUPPORTED = "source_supported"
    DIRECTLY_OBSERVED = "directly_observed"
    USER_CONFIRMED = "user_confirmed"

class Authority(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    SOURCE = "source"
    USER = "user"
```

Define `MemoryCandidate`, `MemoryRecord`, `EvidenceRecord`, `MemoryEvent`, `RetrievalRequest`, `MemoryPacket`, `CandidateDecision`, `BrokerStatus`.

`MemoryEvent` carries optional `mission_id`, `task_id`, `agent_id`, `parent_agent_id`, `workspace_id`, `project_id`, `session_id`.

- [ ] **Step 5: Create provider skeleton capturing `ctx.llm`**

```python
class ObsidianDuoMemoryProvider(MemoryProvider):
    def __init__(self, llm=None):
        self._llm = llm
        self._broker = None
        self._hermes_home = None

    @property
    def name(self) -> str:
        return "obsidian_duo"

    def is_available(self) -> bool:
        return ObsidianDuoConfig.find_config() is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = Path(kwargs["hermes_home"])

    def get_tool_schemas(self):
        return []

def register(ctx):
    ctx.register_memory_provider(ObsidianDuoMemoryProvider(llm=ctx.llm))
```

Implement `find_config()` in `config.py` as a local file check with no network calls.

- [ ] **Step 6: Test discovery/registration**

Assert `load_memory_provider("obsidian_duo")` returns the provider when a valid config/vault exists and that `provider._llm` is host-owned.

- [ ] **Step 7: Run tests and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_config.py tests/plugins/memory/test_obsidian_duo_provider.py -q
git add plugins/memory/obsidian_duo tests/plugins/memory/test_obsidian_duo_config.py tests/plugins/memory/test_obsidian_duo_provider.py
git commit -m "feat(memory-duo): add plugin contracts and config"
```

---

### Task 5: Build the SQLite/WAL durable store

**Files:**
- Create: `plugins/memory/obsidian_duo/store.py`
- Create: `tests/plugins/memory/test_obsidian_duo_store.py`

**Interfaces:**
- `SqliteMemoryStore(path: Path)`
- `initialize()`
- `upsert_memory(record: MemoryRecord, version_reason: str) -> None`
- `get_memory(memory_id: str) -> MemoryRecord | None`
- `insert_evidence(record: EvidenceRecord) -> None`
- `link_evidence(memory_id: str, evidence_id: str) -> None`
- `search_fts(query: str, limit: int) -> list[SearchHit]`
- `record_relationship(...)`
- `record_conflict(...)`
- `stage_candidate(candidate: MemoryCandidate) -> str`
- `record_journal(txn_id: str, operation: str, state: str, payload: dict)`
- `set_note_index(...)`
- `metrics_increment(name: str, value: int = 1)`

- [ ] **Step 1: Write schema/WAL tests**

```python
def test_store_enables_wal_foreign_keys_and_fts(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    with store.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute(
            "INSERT INTO memory_fts(memory_id,title,body,tags,entities) VALUES(?,?,?,?,?)",
            ("mem_1", "HUD drag", "pointer dragging", "hermes", "HUD"),
        )
        rows = conn.execute(
            "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?",
            ('"pointer"',),
        ).fetchall()
    assert rows[0][0] == "mem_1"
```

- [ ] **Step 2: Verify failure**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_store.py -q
```

- [ ] **Step 3: Implement schema**

Create tables:

`schema_meta`, `memories`, `memory_versions`, `evidence`, `memory_evidence`, `relationships`, `conflicts`, `candidates`, `note_index`, `journal`, `metrics`, plus contentless `memory_fts`.

Use:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

Use a thread-local connection because recall and the broker worker may touch the DB concurrently. Do not create a connection-pool dependency.

- [ ] **Step 4: Implement transactional versioning**

Every durable memory mutation must:
1. begin a transaction;
2. preserve the prior representation in `memory_versions`;
3. update the `memories` row;
4. update `memory_fts`;
5. commit.

Stable IDs use stdlib UUIDs:

```python
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
```

- [ ] **Step 5: Add rebuild test**

Insert records, delete only the FTS rows, call `rebuild_fts()`, and assert retrieval works again.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_store.py -q
git add plugins/memory/obsidian_duo/store.py tests/plugins/memory/test_obsidian_duo_store.py
git commit -m "feat(memory-duo): add sqlite durable store"
```

---

### Task 6: Add Obsidian Markdown vault adapter and incremental manifest

**Files:**
- Create: `plugins/memory/obsidian_duo/vault.py`
- Create: `tests/plugins/memory/test_obsidian_duo_vault.py`

**Interfaces:**
- `ObsidianVault(vault_path: Path, managed_folder: str)`
- `ensure_managed_structure()`
- `parse_note(path: Path) -> ParsedNote`
- `write_managed_note(record: MemoryRecord) -> Path`
- `scan_managed_changes(store: SqliteMemoryStore) -> ScanResult`
- `catalog_markdown_paths() -> Iterator[Path]`
- `rebuild_from_vault(store: SqliteMemoryStore, *, full: bool) -> RebuildResult`

- [ ] **Step 1: Write round-trip/frontmatter tests**

Create a managed note with frontmatter and body, parse it, rename it, and assert `memory_id` survives.

- [ ] **Step 2: Write atomic-write failure test**

Patch `os.replace` to raise. Assert the original note remains unchanged and the temporary file is cleaned up.

- [ ] **Step 3: Implement frontmatter parsing**

Use the repository's existing `yaml` dependency. Parse only a leading `---` frontmatter block. Preserve normal Markdown body exactly apart from normalized final newline on broker-authored files.

Do not treat the filename as identity.

- [ ] **Step 4: Implement managed note rendering**

Render the approved frontmatter fields and readable sections. Keep machine metadata compact and the Markdown body pleasant to inspect in Obsidian.

- [ ] **Step 5: Implement atomic writes**

```python
fd, tmp_name = tempfile.mkstemp(
    dir=str(path.parent),
    prefix=f".{path.name}.",
    suffix=".tmp",
)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_name, path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
```

- [ ] **Step 6: Implement incremental manifest**

Track `path`, `mtime_ns`, `size`, `content_hash`, `memory_id`, and `last_broker_txn` in `note_index`.

A managed scan compares mtime/size first, hashes only changed candidates, reparses only changed Markdown, and detects a manual edit when the new hash does not match the last broker transaction.

No continuous poller is added. Broker calls this opportunistically with the configured minimum scan interval.

- [ ] **Step 7: Add “one changed note” test**

Generate 100 notes, index them, modify one, rerun scan, and assert exactly one note is reparsed/reindexed.

- [ ] **Step 8: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_vault.py -q
git add plugins/memory/obsidian_duo/vault.py tests/plugins/memory/test_obsidian_duo_vault.py
git commit -m "feat(memory-duo): add obsidian vault adapter"
```

---

### Task 7: Add deterministic secret scanning and redaction

**Files:**
- Create: `plugins/memory/obsidian_duo/security.py`
- Create: `tests/plugins/memory/test_obsidian_duo_security.py`

**Interfaces:**
- `scan_for_secrets(text: str) -> SecretScanResult`
- `redact_secrets(text: str) -> str`
- `assert_safe_to_persist(text: str) -> None`

- [ ] **Step 1: Write blocking tests**

Cover PEM private-key blocks, bearer/authorization headers, JWT-shaped tokens, common API-key prefixes already recognized elsewhere in Hermes where reusable, assignments such as `GITHUB_TOKEN=<value>` and `API_KEY=<value>`, and high-entropy long token-like strings only when adjacent to credential vocabulary.

Also prove ordinary UUIDs, git SHAs, file hashes and code snippets are not rejected by the entropy rule.

- [ ] **Step 2: Verify failure**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_security.py -q
```

- [ ] **Step 3: Implement scanner**

Reuse existing Hermes secret-pattern helpers if they already cover a case; do not fork a second divergent pattern library unnecessarily. Add plugin-local patterns only for gaps.

Return match categories and redacted spans, never secret values in error messages.

- [ ] **Step 4: Add log-safety test**

Trigger rejection under `caplog` and assert the raw secret is absent from every log record.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_security.py -q
git add plugins/memory/obsidian_duo/security.py tests/plugins/memory/test_obsidian_duo_security.py
git commit -m "feat(memory-duo): block secrets from durable memory"
```

---

### Task 8: Implement promotion, authority, provenance and conflict policy

**Files:**
- Create: `plugins/memory/obsidian_duo/policy.py`
- Create: `tests/plugins/memory/test_obsidian_duo_policy.py`

**Interfaces:**
- `MemoryPolicy.evaluate(candidate: MemoryCandidate) -> CandidateDecision`
- `MemoryPolicy.apply_user_edit(old: MemoryRecord, parsed: ParsedNote) -> MemoryRecord`
- `MemoryPolicy.merge_or_conflict(existing: list[MemoryRecord], candidate: MemoryCandidate) -> CandidateDecision`

- [ ] **Step 1: Write event/promotion matrix tests**

Test:
- explicit user `remember` + safe content → promote;
- direct user correction → promote with `Authority.USER` / `Verification.USER_CONFIRMED`;
- high-confidence agent inference without evidence → stage, not verified;
- transient path/tool dump → discard;
- research/coding lesson before milestone → stage;
- verified task-end lesson → promote;
- secret scan failure → reject;
- conflicting important memory → create disputed/supersession decision, never destructive overwrite.

- [ ] **Step 2: Verify failure**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_policy.py -q
```

- [ ] **Step 3: Implement deterministic first-pass policy**

```python
class EventKind(str, Enum):
    TURN = "turn"
    EXPLICIT_REMEMBER = "explicit_remember"
    USER_CORRECTION = "user_correction"
    DECISION_CONFIRMED = "decision_confirmed"
    MILESTONE = "milestone"
    TASK_COMPLETE = "task_complete"
    SESSION_END = "session_end"
    DELEGATION_RESULT = "delegation_result"
    BUILTIN_MEMORY_WRITE = "builtin_memory_write"
    MANUAL_VAULT_EDIT = "manual_vault_edit"
```

The policy may request optional LLM classification later, but deterministic safety/authority rules always win.

- [ ] **Step 4: Implement append-first conflict behavior**

A candidate that contradicts an active important memory must either supersede when authority/evidence clearly dominates or create an unresolved conflict link.

Never update the old note body in place and erase its history.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_policy.py -q
git add plugins/memory/obsidian_duo/policy.py tests/plugins/memory/test_obsidian_duo_policy.py
git commit -m "feat(memory-duo): add durable memory policy"
```

---

### Task 9: Build deterministic hybrid retrieval and bounded packets

**Files:**
- Create: `plugins/memory/obsidian_duo/retrieval.py`
- Create: `tests/plugins/memory/test_obsidian_duo_retrieval.py`

**Interfaces:**
- `MemoryRetriever.retrieve(request: RetrievalRequest) -> MemoryPacket`
- `MemoryRetriever.classify_query(query: str) -> RecallClass`
- `RecallClass`: `NONE`, `EXACT`, `STRUCTURED`, `SEMANTIC`, `DEEP`.

- [ ] **Step 1: Create retrieval fixture**

In the test file, seed at least 30 memories across two projects, including an exact named decision, a related but lexically weaker lesson, a superseded memory, an unresolved conflict and an unrelated high-recency memory.

- [ ] **Step 2: Write exact/structured tests**

Assert exact HUD query retrieves the active HUD decision, project scope beats unrelated recency, superseded memories are excluded from ordinary results but remain available as history, unresolved conflicts are included in `packet.conflicts`, and an unrelated query may return `no_verified_memory=True`.

- [ ] **Step 3: Implement safe FTS query building**

Do not pass arbitrary user FTS syntax directly. Tokenize Unicode words and quote terms/phrases before `MATCH`.

- [ ] **Step 4: Implement deterministic scoring**

```python
score = (
    0.45 * lexical_score
    + 0.15 * scope_score
    + 0.15 * verification_score
    + 0.10 * authority_score
    + 0.10 * importance_score
    + 0.05 * recency_score
)
```

Type-specific recency must not decay immutable profile facts as aggressively as project state.

- [ ] **Step 5: Enforce packet budgets**

Assemble at most `request.max_memories` and trim rendered text to `request.max_tokens`. Evidence is summarized by ID/excerpt; raw large evidence is not dumped into the packet.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_retrieval.py -q
git add plugins/memory/obsidian_duo/retrieval.py tests/plugins/memory/test_obsidian_duo_retrieval.py
git commit -m "feat(memory-duo): add deterministic retrieval packets"
```

---

### Task 10: Add optional inherited-session LLM assistance without paid fallback

**Files:**
- Create: `plugins/memory/obsidian_duo/inference.py`
- Create: `tests/plugins/memory/test_obsidian_duo_inference.py`

**Interfaces:**
- `MemoryInference(llm)`
- `rerank(query: str, candidates: list[MemoryRecord]) -> InferenceResult`
- `extract_candidates(event: MemoryEvent) -> InferenceResult`
- `consolidate(events: list[MemoryEvent], evidence: list[EvidenceRecord]) -> InferenceResult`
- Every call uses `fallback_policy="same_provider_only"` with no provider/model override.

- [ ] **Step 1: Write route-inheritance test**

Inject a fake `ctx.llm`, call `inference.rerank(...)`, then assert the captured `complete_structured` call omitted `provider` and `model`, used `fallback_policy="same_provider_only"`, and used `purpose="memory-duo.rerank"`.

- [ ] **Step 2: Write free-route failure behavior test**

Have the fake LLM raise a rate-limit/provider error. Assert `rerank()` returns a structured deferred/degraded result and does not mutate durable memory.

- [ ] **Step 3: Define strict JSON schemas**

```python
RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked_ids": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ranked_ids", "uncertainties"],
}
```

Candidate extraction schema contains `candidates[]` with `type`, `title`, `content`, `confidence`, `verification`, `importance`, `evidence_ids`, and `reason`.

- [ ] **Step 4: Implement bounded calls**

Use small `max_tokens`, deterministic temperature where supported, concise input and `purpose` audit strings. Never send the entire vault or full session transcript.

- [ ] **Step 5: Scan model output before persistence**

Inference output is only a candidate. Run the deterministic secret scanner again before any later commit.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_inference.py -q
git add plugins/memory/obsidian_duo/inference.py tests/plugins/memory/test_obsidian_duo_inference.py
git commit -m "feat(memory-duo): add inherited-session inference"
```

---

### Task 11: Build the embedded broker, bounded queue and crash journal

**Files:**
- Create: `plugins/memory/obsidian_duo/broker.py`
- Modify: `plugins/memory/obsidian_duo/client.py`
- Create: `tests/plugins/memory/test_obsidian_duo_broker.py`

**Interfaces:**
- `EmbeddedMemoryBroker(...)`
- `start()`
- `retrieve(request) -> MemoryPacket`
- `observe(event) -> None`
- `propose(candidate) -> CandidateDecision`
- `flush(reason, timeout) -> bool`
- `recover() -> RecoveryResult`
- `shutdown(timeout) -> None`

- [ ] **Step 1: Write bounded-queue test**

Construct with `queue_maxsize=2`. Fill with low-importance events, then add a high-importance user correction. Assert low-value events are coalesced/dropped before the user correction is lost, and queue size never exceeds two.

- [ ] **Step 2: Write crash-recovery test**

Simulate a transaction journaled as `written` with the note on disk but FTS/index state absent. Reopen broker and assert `recover()` indexes the note and marks the journal `committed`.

- [ ] **Step 3: Implement one lazy daemon worker**

```python
self._events = queue.Queue(maxsize=config.queue_maxsize)
```

Start one daemon worker only when the first asynchronous event is submitted. No broker worker is needed for pure read-only startup.

`observe()` returns quickly. If the queue is full, coalesce same-session low-value turn events, discard ephemeral events, and persist high-value deferred events into SQLite.

- [ ] **Step 4: Implement mutation transaction states**

For a durable commit:

```text
prepared -> written -> indexed -> committed
```

Record the journal before note mutation. On recovery, inspect disk hash + DB state and move forward idempotently.

- [ ] **Step 5: Implement health states**

Broker status supports `READY`, `DEGRADED`, `SYNCING`, `REINDEXING`, `RECOVERING`, `UNAVAILABLE`. Retrieval from a healthy SQLite index may remain usable when optional sync/inference is unavailable.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_broker.py -q
git add plugins/memory/obsidian_duo/broker.py plugins/memory/obsidian_duo/client.py tests/plugins/memory/test_obsidian_duo_broker.py
git commit -m "feat(memory-duo): add embedded durable broker"
```

---

### Task 12: Wire provider lifecycle, deep-memory tool and duo behavior

**Files:**
- Modify: `plugins/memory/obsidian_duo/__init__.py`
- Modify: `tests/plugins/memory/test_obsidian_duo_provider.py`

**Interfaces:**
- Provider implements `initialize`, `prefetch`, `sync_turn`, `on_session_end`, `on_session_switch`, `on_pre_compress`, `on_memory_write`, `on_delegation`, `shutdown`, `backup_paths`.
- One tool: `memory_duo` with actions `search`, `propose`, `status`. No direct `commit` action.

- [ ] **Step 1: Define one tool schema**

```python
MEMORY_DUO_SCHEMA = {
    "name": "memory_duo",
    "description": (
        "Search Hermes deep Obsidian memory, propose a durable memory candidate, "
        "or inspect memory status. Proposals are policy-reviewed; this tool cannot "
        "force a direct durable commit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "propose", "status"]},
            "query": {"type": "string"},
            "content": {"type": "string"},
            "memory_type": {"type": "string"},
            "project": {"type": "string"},
            "semantic": {"type": "boolean"},
        },
        "required": ["action"],
    },
}
```

- [ ] **Step 2: Write lifecycle tests**

Prove:
- `initialize` places DB under supplied `hermes_home`, not hardcoded `~/.hermes`;
- `prefetch("thanks")` returns empty;
- normal prefetch uses deterministic retrieval only;
- `sync_turn` enqueues and returns promptly;
- `on_memory_write` mirrors as a candidate, not an unquestioned verified fact;
- `on_delegation` stores a candidate with `DELEGATION_RESULT`, never user-confirmed;
- `on_pre_compress` flushes high-value pending state with a short bound;
- `shutdown` is bounded;
- subagent/non-primary contexts do not independently write durable user memory.

- [ ] **Step 3: Implement provider initialization**

```python
store = SqliteMemoryStore(Path(hermes_home) / "obsidian_duo" / "memory.db")
vault = ObsidianVault(Path(cfg.vault_path), cfg.managed_folder)
policy = MemoryPolicy(...)
retriever = MemoryRetriever(...)
inference = MemoryInference(self._llm) if self._llm else None
broker = EmbeddedMemoryBroker(...)
self._broker = EmbeddedMemoryBrokerClient(broker)
```

Replace the ellipses above with the concrete constructor arguments defined by Tasks 4–11; do not create alternate object graphs.

- [ ] **Step 4: Implement prefetch cheap path**

Before retrieval, opportunistically scan the managed folder only when the minimum scan interval elapsed.

Default `prefetch()` never invokes an LLM. It returns the deterministic packet rendered as concise text; `MemoryManager` owns the outer `<memory-context>` fencing.

- [ ] **Step 5: Implement tool semantic escalation**

`memory_duo(action="search", semantic=True)` may use `MemoryInference.rerank()` after deterministic narrowing. A failed inherited-session call returns the deterministic packet with `semantic_degraded=true`.

- [ ] **Step 6: Implement hot/deep mirroring conservatively**

`on_memory_write` receives built-in hot-memory writes. Mirror them as candidates with provenance metadata. User-originated/explicit confirmed content may promote; ordinary agent-generated hot-memory entries remain subject to broker policy.

Do not automatically delete built-in hot memory when deep memory changes.

- [ ] **Step 7: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_provider.py -q
git add plugins/memory/obsidian_duo/__init__.py tests/plugins/memory/test_obsidian_duo_provider.py
git commit -m "feat(memory-duo): wire provider lifecycle"
```

---

### Task 13: Add debounced optional vault synchronization

**Files:**
- Create: `plugins/memory/obsidian_duo/sync.py`
- Create: `tests/plugins/memory/test_obsidian_duo_sync.py`
- Modify: `plugins/memory/obsidian_duo/broker.py`

**Interfaces:**
- `SyncAdapter.sync(reason: str) -> SyncResult`
- `NoopSyncAdapter`
- `CommandSyncAdapter(command: Sequence[str], debounce_seconds: float)`
- No `shell=True`.
- Successful local memory commit is independent of sync success.

- [ ] **Step 1: Write no-op and debounce tests**

Use a fake executable/script that appends to a file. Trigger four dirty writes within the debounce interval and assert one command invocation after flush.

- [ ] **Step 2: Write failure test**

Make the command exit nonzero. Assert local note and DB commit remain, broker status is usable/degraded rather than unavailable, and `sync_dirty` remains set for retry.

- [ ] **Step 3: Implement adapters**

Default `sync_mode="none"` uses `NoopSyncAdapter`.

For `sync_mode="command"`, execute configured argv with `subprocess.run(..., shell=False, timeout=<bounded>)`, using a finite timeout stored in config with a default of 60 seconds.

Document an optional Obsidian-headless command example as configuration, but keep the adapter generic.

- [ ] **Step 4: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_sync.py tests/plugins/memory/test_obsidian_duo_broker.py -q
git add plugins/memory/obsidian_duo/sync.py plugins/memory/obsidian_duo/broker.py tests/plugins/memory/test_obsidian_duo_sync.py
git commit -m "feat(memory-duo): add debounced optional sync"
```

---

### Task 14: Add CLI diagnostics, repair and setup

**Files:**
- Create: `plugins/memory/obsidian_duo/cli.py`
- Create: `tests/plugins/memory/test_obsidian_duo_cli.py`
- Modify: `plugins/memory/obsidian_duo/__init__.py`
- Modify: `plugins/memory/obsidian_duo/README.md`

**Interfaces:**
- `hermes obsidian_duo status`
- `hermes obsidian_duo doctor`
- `hermes obsidian_duo rebuild-index [--full]`
- `hermes obsidian_duo reconcile`
- `hermes obsidian_duo pending`
- `hermes obsidian_duo conflicts`
- `hermes obsidian_duo stats`

- [ ] **Step 1: Write CLI registration test**

Call `discover_plugin_cli_commands()` with `memory.provider: obsidian_duo` and assert one provider command is registered.

- [ ] **Step 2: Implement `register_cli(subparser)` and `obsidian_duo_command(args)`**

Follow the existing Honcho memory plugin convention. Keep CLI imports lightweight; do not import the OpenAI SDK or broker-heavy modules just to render `hermes --help`.

- [ ] **Step 3: Implement `doctor` checks**

Check configured remote vault path readability, managed directory writability, SQLite integrity/schema, incomplete journal transactions, duplicate IDs, broken supersession links, malformed managed notes, secret-scan violations in managed note content and sync state.

Do not print secret values.

- [ ] **Step 4: Implement rebuild/reconcile**

`rebuild-index` reconstructs derived SQLite state from the vault. `--full` includes the wider vault catalogue; default prioritizes managed memory.

`reconcile` resolves incomplete journal entries and reparses changed managed notes without deleting user content.

- [ ] **Step 5: Add minimal provider setup schema**

`get_config_schema()` prompts only for `vault_path` and `managed_folder`. Advanced settings remain in `$HERMES_HOME/obsidian_duo.json`.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_cli.py -q
git add plugins/memory/obsidian_duo/cli.py plugins/memory/obsidian_duo/__init__.py plugins/memory/obsidian_duo/README.md tests/plugins/memory/test_obsidian_duo_cli.py
git commit -m "feat(memory-duo): add diagnostics and repair cli"
```

---

### Task 15: Complete manual-edit authority, task consolidation and hot/deep candidates

**Files:**
- Modify: `plugins/memory/obsidian_duo/broker.py`
- Modify: `plugins/memory/obsidian_duo/policy.py`
- Modify: `plugins/memory/obsidian_duo/store.py`
- Modify: `tests/plugins/memory/test_obsidian_duo_broker.py`
- Modify: `tests/plugins/memory/test_obsidian_duo_policy.py`

**Interfaces:**
- `broker.process_manual_changes()`
- `broker.consolidate(reason: str, events: list[MemoryEvent])`
- `store.hot_memory_candidates(limit: int) -> list[MemoryRecord]`

- [ ] **Step 1: Write authoritative manual-edit test**

Broker writes an agent memory, then the test edits the Markdown file directly. `process_manual_changes()` must create a user-authority version, invalidate stale retrieval, preserve prior version and not rewrite the user's file back to the agent text.

- [ ] **Step 2: Write malformed-edit test**

Break frontmatter manually. Assert the file remains untouched, index keeps the last valid representation and note state is surfaced as `needs_attention`.

- [ ] **Step 3: Implement bounded task/session consolidation**

On `TASK_COMPLETE` and `SESSION_END`, batch only task summaries/events/evidence already retained by the broker. If inference is available, call `MemoryInference.consolidate`; if not, leave valuable candidates staged. Never replay an entire huge transcript solely for Memory Duo.

- [ ] **Step 4: Implement hot-memory candidate scoring**

Compute a suggestion score from explicit user importance/pin, retrieval frequency, verification, authority, small/stable content and cross-session usage.

Return candidates only. Do not autonomously churn `MEMORY.md`/`USER.md` in v1.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_broker.py tests/plugins/memory/test_obsidian_duo_policy.py -q
git add plugins/memory/obsidian_duo/broker.py plugins/memory/obsidian_duo/policy.py plugins/memory/obsidian_duo/store.py tests/plugins/memory/test_obsidian_duo_broker.py tests/plugins/memory/test_obsidian_duo_policy.py
git commit -m "feat(memory-duo): finalize authority and consolidation"
```

---

### Task 16: Prove remote-profile, delegation, backup and failure isolation

**Files:**
- Create: `tests/plugins/memory/test_obsidian_duo_e2e.py`
- Modify: `plugins/memory/obsidian_duo/__init__.py`
- Modify: `plugins/memory/obsidian_duo/README.md`

**Interfaces:**
- Same `HERMES_HOME` + different platform surface → same Memory Duo DB/vault namespace.
- Different `HERMES_HOME` → isolated broker state.
- `backup_paths()` includes only configured managed external memory path when appropriate.

- [ ] **Step 1: Write profile isolation integration test**

Create two temporary Hermes homes and one temporary vault per profile. Initialize provider for `platform="desktop"`, `"telegram"` and `"cli"` against profile A and prove they resolve A's DB. Initialize profile B and prove no A memory is visible.

- [ ] **Step 2: Write remote-client invariant test**

No test code under `apps/desktop` is required for memory persistence. Simulate the desktop surface solely by initializing the remote provider with `platform="desktop"` and a remote `HERMES_HOME`. Assert every Memory Duo path is under the remote home or configured remote vault.

- [ ] **Step 3: Write delegation test**

Call parent provider `on_delegation("investigate X", "I think Y", child_session_id="child-1")`. Assert candidate provenance contains the child ID and verification remains unverified/inferred until independent evidence promotes it.

- [ ] **Step 4: Write outage test**

Make vault writes fail and plugin LLM fail. Assert deterministic DB recall still works where indexed data exists, Hermes-facing `prefetch` returns safely, provider exceptions do not escape the MemoryManager path, and dirty/deferred work is retained.

- [ ] **Step 5: Implement `backup_paths()`**

If managed vault memory is outside `HERMES_HOME` and configured for inclusion, return the managed `Hermes Memory/` path, not the user's entire unrelated vault. Broker DB under `HERMES_HOME` needs no extra path.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_e2e.py tests/agent/test_memory_provider.py -q
git add plugins/memory/obsidian_duo/__init__.py plugins/memory/obsidian_duo/README.md tests/plugins/memory/test_obsidian_duo_e2e.py
git commit -m "test(memory-duo): prove remote and failure-safe behavior"
```

---

### Task 17: Add evaluation fixtures, lightweight resource checks and final documentation

**Files:**
- Create: `tests/plugins/memory/fixtures/obsidian_duo_retrieval.json`
- Modify: `tests/plugins/memory/test_obsidian_duo_retrieval.py`
- Create: `tests/plugins/memory/test_obsidian_duo_resources.py`
- Modify: `plugins/memory/obsidian_duo/README.md`
- Create: `docs/superpowers/specs/2026-08-10-memory-duo-design.md`
- Create: `docs/superpowers/plans/2026-08-10-memory-duo-implementation.md`

**Interfaces:**
- Retrieval fixture contains at least 30 query/expected-memory cases.
- Resource tests assert architecture, not brittle millisecond/RSS thresholds.

- [ ] **Step 1: Add retrieval evaluation fixture**

Include exact lookup, project continuation, related lesson, contradiction, stale/superseded memory, no-answer and cross-project cases. Each fixture names expected memory IDs and forbidden IDs.

- [ ] **Step 2: Run fixture evaluation**

```bash
python -m pytest tests/plugins/memory/test_obsidian_duo_retrieval.py -q
```

Require every curated fixture to pass before completion.

- [ ] **Step 3: Add resource-path tests**

Assert provider import/initialize does not import or require `torch`, `sentence_transformers`, `chromadb`, `qdrant_client`, `weaviate`, `neo4j` or `redis`.

Assert initialization spawns no broker worker thread until asynchronous work is first queued.

Assert no subprocess is started when `sync_mode="none"`.

- [ ] **Step 4: Add 10k-note incremental correctness test**

Generate 10,000 tiny Markdown notes in a temporary vault in a test marked `slow`. Build the catalogue, change one note, rescan, and assert only that note is reparsed. Do not assert a fragile wall-clock threshold.

- [ ] **Step 5: Add approved docs**

Copy the approved Memory Duo design into:

```text
docs/superpowers/specs/2026-08-10-memory-duo-design.md
```

Save this plan as:

```text
docs/superpowers/plans/2026-08-10-memory-duo-implementation.md
```

- [ ] **Step 6: Run complete focused test matrix**

```bash
python -m pytest   tests/agent/test_memory_provider.py   tests/agent/test_plugin_llm.py   tests/agent/test_auxiliary_client.py   tests/plugins/memory/test_obsidian_duo_config.py   tests/plugins/memory/test_obsidian_duo_store.py   tests/plugins/memory/test_obsidian_duo_vault.py   tests/plugins/memory/test_obsidian_duo_security.py   tests/plugins/memory/test_obsidian_duo_policy.py   tests/plugins/memory/test_obsidian_duo_retrieval.py   tests/plugins/memory/test_obsidian_duo_inference.py   tests/plugins/memory/test_obsidian_duo_broker.py   tests/plugins/memory/test_obsidian_duo_provider.py   tests/plugins/memory/test_obsidian_duo_sync.py   tests/plugins/memory/test_obsidian_duo_cli.py   tests/plugins/memory/test_obsidian_duo_e2e.py   tests/plugins/memory/test_obsidian_duo_resources.py   -q
```

Expected: pass.

- [ ] **Step 7: Run broader regression suites**

Run the repository's normal test script or the broad agent/plugin suites documented in `AGENTS.md`, including memory, plugin and auxiliary routing coverage. Do not claim completion if a regression is unexplained.

- [ ] **Step 8: Manual remote smoke test**

On the actual remote Hermes host using the Autopilot profile:
1. configure `memory.provider: obsidian_duo`;
2. point `vault_path` to the remotely available Obsidian vault;
3. start a Hermes session through remote Desktop;
4. use the currently selected free model route;
5. state an explicit durable preference;
6. end/reset the session;
7. confirm a managed Obsidian note appears;
8. start a new session and retrieve it;
9. edit the note manually in Obsidian, sync it to the remote vault, and confirm the next recall honors the user edit;
10. temporarily make the optional LLM route unavailable and confirm deterministic recall still works without cross-provider fallback.

- [ ] **Step 9: Final evidence report**

Before the final commit, record the exact tests run and pass counts, files changed, DB schema version, whether any new dependency was added, number of required background threads/processes at idle, a sample free-route audit showing Memory Duo used the active session provider/model, proof the paid-fallback spy was not contacted, and known limitations that remain for the later orchestration phase.

- [ ] **Step 10: Commit**

```bash
git add docs/superpowers plugins/memory/obsidian_duo tests/plugins/memory tests/agent website/docs/developer-guide
git commit -m "feat: complete Hermes Obsidian Memory Duo v1"
```

---

## Final Acceptance Checklist

The implementation is complete only when all of the following are evidenced by tests or the manual smoke test:

- Hermes hot memory remains intact and bounded.
- Graphify/SessionDB remain the episodic history source.
- Obsidian managed notes are readable ordinary Markdown.
- Stable IDs survive rename/move.
- User edits are authoritative.
- Conflicts/supersession preserve history.
- Secrets cannot be persisted.
- Exact/structured retrieval needs no LLM.
- No local model, embeddings or GPU dependency exists.
- No vector/graph/Redis/Postgres service is required.
- Windows Hermes Desktop performs no Memory Duo compute.
- Broker is embedded and service-ready.
- Idle broker requires no separate process.
- Candidate queue is bounded.
- Crash recovery is deterministic.
- Deep-memory outage does not break Hermes.
- Active session model/provider is inherited for optional inference.
- Same-provider-only policy prevents cross-provider paid fallback.
- Free-route rate limits cause deterministic degradation/defer, not silent spending.
- Remote profile isolation works.
- Subagent results are candidates, not trusted durable facts.
- Wider-vault indexing is incremental/lazy.
- Obsidian sync is optional/debounced and failure-safe.
- One provider-gated model tool is the maximum added tool footprint.
- Full focused tests and broader regression tests pass.
