# Scout-wide Firecrawl Credits Circuit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop charge-bearing Firecrawl work after the first confirmed HTTP 402 in one Scout activation, continue through one deterministic fallback per capability, and emit at most one structured credits action signal for that activation.

**Architecture:** One lock-protected `FirecrawlRunState` is installed in a `ContextVar` only around a canonical `jobflow-scout` scheduler activation. Firecrawl web and browser providers enforce the admission gate and record the first genuine 402; web tools memoize one non-Firecrawl provider per capability, the browser tool reuses local Chromium, and a scheduler finalizer emits the one routing-v3 credits gate before resetting context.

**Tech Stack:** Python 3.11, `contextvars`, `threading.Lock`, asyncio/`asyncio.to_thread`, pytest, existing Hermes plugin registries, existing SQLite event bus and routing-v3.

## Global Constraints

- Work only in the nested `C:/Users/diego/.hermes/agent-src` repository.
- Do not modify `C:/Users/diego/.hermes/profiles/main/cron/jobs.json`, Scout workspace files, root `.hermes` dirty work, live provider configuration, or gateway process state.
- Do not restart the gateway and do not make live Firecrawl calls.
- Preserve routing-v3's exact human gate: `action_required is True` and `action_kind == "credits"`.
- Preserve SSRF checks, website-access policy, input order, duplicate URL reconstruction, public response envelopes, and allowlisted diagnostics.
- Never store or emit Firecrawl response bodies, URLs, headers, credentials, or raw exception representations as circuit/action evidence.
- The circuit blocks only operations admitted after it opens; it does not cancel requests already admitted or in flight.
- Browser `close_session()` and `emergency_cleanup()` must remain allowed after opening.
- Outside a Scout run context, preserve the existing invocation-local extraction circuit.
- Use TDD for every task: write the focused failing test, run it and inspect the intended failure, implement minimally, rerun focused tests, then commit explicit task-owned paths.
- Before every commit, run `git status --short` and stage explicit paths only.

## File Structure

- Create `agent/firecrawl_run_state.py`: the sole run-scoped state, stable public diagnostics, typed browser exceptions, ContextVar lifecycle, locking, fallback memoization, and action claim.
- Modify `agent/web_search_registry.py`: add deterministic capability resolution with Firecrawl excluded; leave ordinary active-provider semantics unchanged.
- Modify `plugins/web/firecrawl/provider.py`: enforce the run admission gate and record genuine search/extract 402s while retaining invocation-local behavior outside context.
- Modify `tools/web_tools.py`: invoke one memoized non-Firecrawl fallback and merge only unresolved extract entries.
- Modify `plugins/browser/firecrawl/provider.py`: type/sanitize browser 402 and pre-request circuit-open failures while leaving DELETE cleanup untouched.
- Modify `tools/browser_tool.py`: map the two typed Firecrawl credit errors to stable local-fallback metadata.
- Modify `cron/scheduler.py`: install state only for canonical Scout, finalize generic/credits iteration once on every completion path, then reset context.
- Create focused test files for state, tool fallback, browser circuit, and scheduler lifecycle; extend existing plugin and routing tests where their current fixtures already express the contract.

---

### Task 1: Run-scoped state and concurrency contract

**Files:**
- Create: `agent/firecrawl_run_state.py`
- Create: `tests/agent/test_firecrawl_run_state.py`

**Interfaces:**
- Consumes: only Python standard-library `contextvars`, `dataclasses`, and `threading`.
- Produces:
  - `CREDITS_EXHAUSTED_INFO: Mapping[str, object]`
  - `CIRCUIT_OPEN_INFO: Mapping[str, object]`
  - `class FirecrawlCreditsExhaustedError(RuntimeError)` with sanitized `error_info`
  - `class FirecrawlCircuitOpenError(RuntimeError)` with sanitized `error_info`
  - `class FirecrawlRunState`
  - `install_firecrawl_run() -> tuple[FirecrawlRunState, contextvars.Token]`
  - `reset_firecrawl_run(token: contextvars.Token) -> None`
  - `current_firecrawl_run() -> FirecrawlRunState | None`
  - `raise_if_firecrawl_circuit_open() -> None`
  - `record_firecrawl_credits_exhausted() -> bool`
  - `get_or_select_fallback_provider(capability: str, resolver: Callable[[], object | None]) -> object | None`
  - `claim_credits_action() -> bool`

- [ ] **Step 1: Write failing state lifecycle and sanitization tests**

Create `tests/agent/test_firecrawl_run_state.py` with direct tests for absent context, installation/reset, first-writer-wins evidence, and typed errors:

```python
from agent import firecrawl_run_state as state


def test_context_is_absent_until_installed_and_reset_restores_absence():
    assert state.current_firecrawl_run() is None
    run, token = state.install_firecrawl_run()
    try:
        assert state.current_firecrawl_run() is run
        assert run.circuit_open is False
    finally:
        state.reset_firecrawl_run(token)
    assert state.current_firecrawl_run() is None


def test_first_credit_failure_opens_once_with_only_stable_evidence():
    run, token = state.install_firecrawl_run()
    try:
        assert state.record_firecrawl_credits_exhausted() is True
        assert state.record_firecrawl_credits_exhausted() is False
        assert run.first_failure == {
            "code": "provider_credits_exhausted",
            "provider": "firecrawl",
            "scope": "account",
            "retryable": False,
        }
        assert run.fallback_decision == "continue_without_firecrawl"
    finally:
        state.reset_firecrawl_run(token)


def test_open_gate_raises_sanitized_circuit_error_only_after_402():
    run, token = state.install_firecrawl_run()
    try:
        state.raise_if_firecrawl_circuit_open()
        state.record_firecrawl_credits_exhausted()
        try:
            state.raise_if_firecrawl_circuit_open()
        except state.FirecrawlCircuitOpenError as exc:
            assert str(exc) == "Firecrawl account credit circuit is open"
            assert exc.error_info == dict(state.CIRCUIT_OPEN_INFO)
        else:
            raise AssertionError("open circuit did not reject admission")
    finally:
        state.reset_firecrawl_run(token)
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python -m pytest tests/agent/test_firecrawl_run_state.py -q
```

Expected: collection fails with `ImportError` because `agent.firecrawl_run_state` does not exist.

- [ ] **Step 3: Implement the minimal ContextVar state**

Create `agent/firecrawl_run_state.py` with immutable public constants and a mutable lock-protected dataclass. Return copies from evidence properties so consumers cannot mutate shared state:

```python
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

CREDITS_EXHAUSTED_INFO: Mapping[str, object] = MappingProxyType({
    "code": "provider_credits_exhausted",
    "provider": "firecrawl",
    "scope": "account",
    "retryable": False,
})
CIRCUIT_OPEN_INFO: Mapping[str, object] = MappingProxyType({
    "code": "provider_circuit_open",
    "provider": "firecrawl",
    "scope": "account",
    "retryable": False,
})
_UNSET = object()

class FirecrawlCreditsExhaustedError(RuntimeError):
    error_info = CREDITS_EXHAUSTED_INFO
    def __init__(self) -> None:
        super().__init__("Firecrawl account credits are exhausted")

class FirecrawlCircuitOpenError(RuntimeError):
    error_info = CIRCUIT_OPEN_INFO
    def __init__(self) -> None:
        super().__init__("Firecrawl account credit circuit is open")

@dataclass
class FirecrawlRunState:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _first_failure: dict[str, object] | None = None
    fallback_decision: str | None = None
    _fallback_providers: dict[str, object] = field(default_factory=dict, repr=False)
    _credits_action_claimed: bool = False

    @property
    def circuit_open(self) -> bool:
        with self._lock:
            return self._first_failure is not None

    @property
    def first_failure(self) -> dict[str, object] | None:
        with self._lock:
            return dict(self._first_failure) if self._first_failure else None

    def record_credits_exhausted(self) -> bool:
        with self._lock:
            if self._first_failure is not None:
                return False
            self._first_failure = dict(CREDITS_EXHAUSTED_INFO)
            self.fallback_decision = "continue_without_firecrawl"
            return True

    def get_or_select_provider(self, capability: str, resolver: Callable[[], object | None]):
        with self._lock:
            current = self._fallback_providers.get(capability, _UNSET)
            if current is not _UNSET:
                return None if current is _UNSET_NONE else current
            selected = resolver()
            self._fallback_providers[capability] = _UNSET_NONE if selected is None else selected
            return selected

    def claim_credits_action(self) -> bool:
        with self._lock:
            if self._first_failure is None or self._credits_action_claimed:
                return False
            self._credits_action_claimed = True
            return True

_UNSET_NONE = object()
_current: contextvars.ContextVar[FirecrawlRunState | None] = contextvars.ContextVar(
    "firecrawl_run_state", default=None
)
```

Add the thin module-level wrappers named in **Interfaces**. `record_firecrawl_credits_exhausted()` returns `False` when no context exists; `raise_if_firecrawl_circuit_open()` is a no-op without context; `get_or_select_fallback_provider()` runs the resolver directly without memoization when no context exists; `claim_credits_action()` returns `False` without context.

- [ ] **Step 4: Add concurrency, fallback, and action-claim tests**

Append tests using `ThreadPoolExecutor` and `contextvars.copy_context()`:

```python
import contextvars
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_402_recording_has_exactly_one_winner():
    run, token = state.install_firecrawl_run()
    try:
        contexts = [contextvars.copy_context() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda ctx: ctx.run(state.record_firecrawl_credits_exhausted),
                contexts,
            ))
        assert results.count(True) == 1
        assert results.count(False) == 7
    finally:
        state.reset_firecrawl_run(token)


def test_fallback_provider_and_none_are_memoized_per_capability():
    run, token = state.install_firecrawl_run()
    calls = []
    try:
        search = object()
        assert state.get_or_select_fallback_provider(
            "search", lambda: calls.append("search") or search
        ) is search
        assert state.get_or_select_fallback_provider(
            "search", lambda: calls.append("unexpected")
        ) is search
        assert state.get_or_select_fallback_provider(
            "extract", lambda: calls.append("extract") or None
        ) is None
        assert state.get_or_select_fallback_provider(
            "extract", lambda: calls.append("unexpected-none") or object()
        ) is None
        assert calls == ["search", "extract"]
    finally:
        state.reset_firecrawl_run(token)


def test_credits_action_can_be_claimed_once_only_after_real_402():
    run, token = state.install_firecrawl_run()
    try:
        assert state.claim_credits_action() is False
        state.record_firecrawl_credits_exhausted()
        assert state.claim_credits_action() is True
        assert state.claim_credits_action() is False
    finally:
        state.reset_firecrawl_run(token)
```

- [ ] **Step 5: Run state tests and verify GREEN**

```bash
python -m pytest tests/agent/test_firecrawl_run_state.py -q
```

Expected: all tests pass. Also run the existing context propagation tests:

```bash
python -m pytest tests/agent/test_tool_executor_context.py tests/cron/test_cron_context_from.py -q
```

If `tests/agent/test_tool_executor_context.py` is not present, locate the existing `propagate_context_to_thread` test with `rg -l 'propagate_context_to_thread' tests` and run that exact file; do not add a duplicate propagation mechanism.

- [ ] **Step 6: Commit the state unit**

```bash
git status --short
git add agent/firecrawl_run_state.py tests/agent/test_firecrawl_run_state.py
git commit -m "feat(firecrawl): add run-scoped credit state"
```

Expected: only the two task-owned paths are committed.

---

### Task 2: Deterministic non-Firecrawl provider resolution

**Files:**
- Modify: `agent/web_search_registry.py:122-219,281-298`
- Modify: `tests/plugins/web/test_web_search_provider_plugins.py:250-331`

**Interfaces:**
- Consumes: existing `_providers`, `_LEGACY_PREFERENCE`, `_read_config_key()`, and provider capability/availability methods.
- Produces: `get_fallback_provider(capability: str, *, excluded: frozenset[str] = frozenset()) -> WebSearchProvider | None`.

- [ ] **Step 1: Write failing exclusion-resolution tests**

Add these tests to `TestRegistryResolution`:

```python
def test_fallback_search_excludes_firecrawl_and_uses_existing_priority(monkeypatch):
    _ensure_plugins_loaded()
    from agent.web_search_registry import get_fallback_provider
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel")
    result = get_fallback_provider("search", excluded=frozenset({"firecrawl"}))
    assert result is not None
    assert result.name == "parallel"


def test_explicit_firecrawl_is_excluded_but_explicit_non_firecrawl_must_be_available(monkeypatch):
    _ensure_plugins_loaded()
    import agent.web_search_registry as registry
    monkeypatch.setattr(registry, "_read_config_key", lambda *path: "firecrawl")
    monkeypatch.setenv("EXA_API_KEY", "exa")
    assert registry.get_fallback_provider(
        "extract", excluded=frozenset({"firecrawl"})
    ).name == "exa"


def test_fallback_returns_none_when_every_capable_provider_is_excluded_or_unavailable(monkeypatch):
    _ensure_plugins_loaded()
    import agent.web_search_registry as registry
    monkeypatch.setattr(registry, "_providers", {
        "firecrawl": registry.get_provider("firecrawl")
    })
    assert registry.get_fallback_provider(
        "extract", excluded=frozenset({"firecrawl"})
    ) is None
```

- [ ] **Step 2: Run focused registry tests and verify RED**

```bash
python -m pytest tests/plugins/web/test_web_search_provider_plugins.py::TestRegistryResolution -q
```

Expected: new tests fail because `get_fallback_provider` is undefined.

- [ ] **Step 3: Refactor resolution helpers without changing active semantics**

Extract the existing capability and safe-availability checks into private module helpers, then add:

```python
def get_fallback_provider(
    capability: str,
    *,
    excluded: frozenset[str] = frozenset(),
) -> Optional[WebSearchProvider]:
    if capability not in {"search", "extract"}:
        raise ValueError(f"Unsupported web capability: {capability}")
    explicit = (
        _read_config_key("web", f"{capability}_backend")
        or _read_config_key("web", "backend")
    )
    with _lock:
        snapshot = dict(_providers)

    if explicit and explicit not in excluded:
        provider = snapshot.get(explicit)
        if provider and _capable(provider, capability) and _is_available_safe(provider):
            return provider

    eligible = [
        provider for provider in snapshot.values()
        if provider.name not in excluded
        and _capable(provider, capability)
        and _is_available_safe(provider)
    ]
    if len(eligible) == 1:
        return eligible[0]
    for name in _LEGACY_PREFERENCE:
        provider = snapshot.get(name)
        if provider in eligible:
            return provider
    return None
```

Keep `_resolve()`'s explicit-config behavior unchanged: ordinary explicit providers still win even when unavailable. Include the existing registry's `xai` behavior unchanged; do not add it to `_LEGACY_PREFERENCE` unless current tests require that independently.

- [ ] **Step 4: Run registry and plugin tests**

```bash
python -m pytest tests/plugins/web/test_web_search_provider_plugins.py -q
```

Expected: all tests pass, including ordinary explicit-provider behavior.

- [ ] **Step 5: Commit the resolver**

```bash
git status --short
git add agent/web_search_registry.py tests/plugins/web/test_web_search_provider_plugins.py
git commit -m "feat(web): resolve non-Firecrawl fallbacks"
```

---

### Task 3: Firecrawl web-provider run admission

**Files:**
- Modify: `plugins/web/firecrawl/provider.py:370-644`
- Modify: `tests/plugins/web/test_web_search_provider_plugins.py:452-end`

**Interfaces:**
- Consumes: `raise_if_firecrawl_circuit_open()`, `record_firecrawl_credits_exhausted()`, `CREDITS_EXHAUSTED_INFO`, and `CIRCUIT_OPEN_INFO` from Task 1.
- Produces: existing provider envelopes with run-aware credit/circuit evidence; no public signature changes.

- [ ] **Step 1: Write failing search admission tests with a positive control**

Append tests that install state explicitly and count the real provider seam:

```python
def test_firecrawl_search_run_circuit_blocks_later_network_call(monkeypatch):
    from agent import firecrawl_run_state as state
    from plugins.web.firecrawl import provider as firecrawl_provider
    calls = []

    class Client:
        def search(self, **kwargs):
            calls.append(kwargs)
            return {"web": []}

    monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())
    provider = firecrawl_provider.FirecrawlWebSearchProvider()

    # Positive control: a closed circuit reaches the network seam.
    run, token = state.install_firecrawl_run()
    try:
        assert provider.search("before")["success"] is True
        assert len(calls) == 1
        state.record_firecrawl_credits_exhausted()
        blocked = provider.search("after")
        assert len(calls) == 1
        assert blocked["error_info"] == dict(state.CIRCUIT_OPEN_INFO)
    finally:
        state.reset_firecrawl_run(token)


def test_firecrawl_search_402_opens_shared_run_state(monkeypatch):
    from agent import firecrawl_run_state as state
    from plugins.web.firecrawl import provider as firecrawl_provider

    class PaymentRequired(Exception):
        status_code = 402
    class Client:
        def search(self, **kwargs):
            raise PaymentRequired("secret response body must not escape")

    monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())
    run, token = state.install_firecrawl_run()
    try:
        result = firecrawl_provider.FirecrawlWebSearchProvider().search("query")
        assert run.circuit_open is True
        assert result["error_info"] == dict(state.CREDITS_EXHAUSTED_INFO)
        assert "secret response body" not in repr(run.first_failure)
    finally:
        state.reset_firecrawl_run(token)
```

- [ ] **Step 2: Write failing cross-invocation extraction and transient controls**

Add a test where invocation 1 receives 402 and invocation 2 attempts another URL in the same installed context. Assert invocation 2 returns `provider_circuit_open` and the scrape call count stays one. Add a separate `TimeoutError` test asserting a second invocation does call scrape, proving transient errors do not open shared state.

- [ ] **Step 3: Run focused provider tests and verify RED**

```bash
python -m pytest tests/plugins/web/test_web_search_provider_plugins.py -k "firecrawl and (circuit or 402 or transient or payment_required)" -q
```

Expected: cross-invocation suppression tests fail because the provider does not consult shared state.

- [ ] **Step 4: Implement final pre-network admission and 402 recording**

In `search()`, put `_get_firecrawl_client()` before or after admission only if it is credential/setup-only; the final check must occur immediately before `client.search(...)`:

```python
try:
    raise_if_firecrawl_circuit_open()
except FirecrawlCircuitOpenError as exc:
    return {"success": False, "error": str(exc), "error_info": dict(exc.error_info)}

try:
    response = client.search(query=query, limit=limit)
except Exception as exc:
    error_info = _credit_error_info(exc)
    if error_info:
        record_firecrawl_credits_exhausted()
```

In `extract()`, preserve `credit_circuit_open` for no-context invocation behavior. Before each `_get_firecrawl_client().scrape` call, run the shared admission check and append a stable circuit-open item if denied. On genuine 402 call `record_firecrawl_credits_exhausted()` and also set the local boolean. Use the stable constants rather than duplicate dictionaries.

Do not move either pre- or post-redirect website policy check, and do not classify any non-402 exception as credits.

- [ ] **Step 5: Run all web plugin tests**

```bash
python -m pytest tests/plugins/web/test_web_search_provider_plugins.py -q
```

Expected: all pass. Confirm the positive-control call assertions execute.

- [ ] **Step 6: Commit provider admission**

```bash
git status --short
git add plugins/web/firecrawl/provider.py tests/plugins/web/test_web_search_provider_plugins.py
git commit -m "feat(firecrawl): enforce Scout run admission"
```

---

### Task 4: Web search and extraction fallback execution

**Files:**
- Modify: `tools/web_tools.py:619-1051`
- Create: `tests/tools/test_web_firecrawl_run_fallback.py`

**Interfaces:**
- Consumes: Task 1 `current_firecrawl_run()` and `get_or_select_fallback_provider()`; Task 2 `get_fallback_provider()`; existing provider `search()`/`extract()` contracts.
- Produces private helpers:
  - `_is_firecrawl_credit_result(value: object) -> bool`
  - `_invoke_extract_provider(provider, urls: list[str], format: str | None) -> Awaitable[list[dict[str, object]]]`
  - `_merge_credit_fallback_results(primary, fallback, safe_urls) -> list[dict[str, object]]`

- [ ] **Step 1: Write failing search fallback tests**

Create `tests/tools/test_web_firecrawl_run_fallback.py` with lightweight fake providers and monkeypatch registry/tool loading. Cover:

```python
def test_search_402_uses_one_memoized_non_firecrawl_provider(monkeypatch):
    # Firecrawl returns provider_credits_exhausted on first call.
    # Resolver returns fallback A once; mutate resolver to fallback B afterward.
    # Two web_search_tool calls in one installed run both use A, Firecrawl is called once,
    # and no provider chain occurs.


def test_search_open_circuit_without_available_fallback_keeps_sanitized_evidence(monkeypatch):
    # Resolver returns None; parsed JSON contains provider_circuit_open and no raw 402 text.


def test_transient_firecrawl_search_failure_does_not_enter_credits_fallback(monkeypatch):
    # Primary returns success=False without credit error_info; fallback call count remains zero.
```

Use `json.loads(web_search_tool(...))` and exact counter assertions. Install/reset Task 1 context in `try/finally`; never rely on fixture teardown for ContextVar cleanup.

- [ ] **Step 2: Run search fallback tests and verify RED**

```bash
python -m pytest tests/tools/test_web_firecrawl_run_fallback.py -k search -q
```

Expected: tests fail because `web_search_tool()` returns Firecrawl's first error and does not resolve fallback.

- [ ] **Step 3: Implement one search fallback**

After the primary response, detect only the two allowlisted Firecrawl codes. Resolve with:

```python
def _resolve_run_fallback(capability: str):
    return get_or_select_fallback_provider(
        capability,
        lambda: get_fallback_provider(
            capability, excluded=frozenset({"firecrawl"})
        ),
    )
```

For a credit result, invoke the memoized provider once. If none exists, retain the original stable result. Do not invoke fallback for ordinary errors, do not recurse through `web_search_tool`, and do not re-run plugin selection after a memoized `None`.

- [ ] **Step 4: Write failing extract merge tests**

Add async tests (`pytest.mark.asyncio`) for a primary Firecrawl result list containing:

1. one success,
2. one policy-blocked item,
3. one `provider_credits_exhausted`,
4. one `provider_circuit_open`,
5. a duplicate input URL,
6. an invalid input and an SSRF-blocked input handled before provider dispatch.

Assert fallback receives only positions 3 and 4, and final output preserves all original positions. Add cases for:

- fallback raises for the whole batch → both unresolved positions become sanitized errors;
- fallback returns one result for two URLs → missing position becomes `Extract backend returned no result for this URL` (or a dedicated stable fallback-incomplete message);
- fallback returns extra/misaligned entries → extras ignored, requested unresolved URL order authoritative;
- async and sync fallback providers both work;
- transient primary failures do not trigger credits fallback.

- [ ] **Step 5: Run extract tests and verify RED**

```bash
python -m pytest tests/tools/test_web_firecrawl_run_fallback.py -k extract -q
```

Expected: tests fail because unresolved credit entries are not retried.

- [ ] **Step 6: Extract a single invocation helper and merge only unresolved entries**

Use one helper for both primary and fallback dispatch:

```python
async def _invoke_extract_provider(provider, urls, *, format):
    if inspect.iscoroutinefunction(provider.extract):
        return await provider.extract(urls, format=format)
    return await asyncio.to_thread(provider.extract, urls, format=format)
```

After primary safe results return, identify unresolved positions by exact `error_info.code` membership in `{"provider_credits_exhausted", "provider_circuit_open"}`. Resolve the memoized extract provider once and call it with only those URLs. Normalize fallback exceptions and short/malformed lists into per-unresolved-position dictionaries, then replace only unresolved slots before the existing invalid/SSRF reconstruction and output trimming.

Never retry successes or `blocked_by_policy` entries. Do not bypass `async_is_safe_url`; fallback receives only the already-approved `safe_urls` subset.

- [ ] **Step 7: Run focused and existing web-tool regressions**

```bash
python -m pytest tests/tools/test_web_firecrawl_run_fallback.py tests/plugins/web/test_web_search_provider_plugins.py -q
```

Then locate existing web extract security/order tests and run them:

```bash
python -m pytest tests/tools -k "web_extract and (ssrf or secret or order or invalid)" -q
```

Expected: all selected tests pass; no network access occurs.

- [ ] **Step 8: Commit web fallback behavior**

```bash
git status --short
git add tools/web_tools.py tests/tools/test_web_firecrawl_run_fallback.py
git commit -m "feat(web): fall back once after Firecrawl credits"
```

---

### Task 5: Firecrawl cloud-browser classification and local fallback

**Files:**
- Modify: `plugins/browser/firecrawl/provider.py:80-156`
- Modify: `tests/plugins/browser/test_browser_provider_plugins.py`
- Modify: `tools/browser_tool.py:2083-2124`
- Modify: `tests/tools/test_browser_cloud_fallback.py`

**Interfaces:**
- Consumes: Task 1 typed `FirecrawlCreditsExhaustedError`, `FirecrawlCircuitOpenError`, admission check, and record function.
- Produces: stable local fallback metadata:
  - `fallback_reason == "provider_credits_exhausted"` for the first 402;
  - `fallback_reason == "provider_circuit_open"` for pre-request suppression;
  - existing free-form behavior remains for unrelated cloud-provider errors.

- [ ] **Step 1: Write failing provider 402 and admission tests**

Extend browser plugin tests with mocked `requests.post` and `requests.delete`:

```python
def test_firecrawl_browser_402_opens_run_without_response_body(monkeypatch):
    from agent import firecrawl_run_state as state
    from plugins.browser.firecrawl.provider import FirecrawlBrowserProvider
    response = Mock(ok=False, status_code=402, text="secret billing response")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-only")
    monkeypatch.setattr("plugins.browser.firecrawl.provider.requests.post", Mock(return_value=response))
    run, token = state.install_firecrawl_run()
    try:
        with pytest.raises(state.FirecrawlCreditsExhaustedError) as raised:
            FirecrawlBrowserProvider().create_session("task")
        assert run.circuit_open is True
        assert "secret billing response" not in str(raised.value)
    finally:
        state.reset_firecrawl_run(token)


def test_open_run_blocks_browser_post_but_not_cleanup_delete(monkeypatch):
    # Positive control in a closed context reaches POST once.
    # Open the circuit, then a second create_session raises FirecrawlCircuitOpenError
    # without increasing POST count. close_session and emergency_cleanup still call DELETE.
```

Add a non-402 response test asserting shared state remains closed.

- [ ] **Step 2: Run provider tests and verify RED**

```bash
python -m pytest tests/plugins/browser/test_browser_provider_plugins.py -k "firecrawl and (402 or circuit or cleanup)" -q
```

Expected: 402 raises raw `RuntimeError` including response text and no run state opens.

- [ ] **Step 3: Implement typed browser errors and leave cleanup untouched**

Immediately before `requests.post`, call `raise_if_firecrawl_circuit_open()`. For `status_code == 402`, call `record_firecrawl_credits_exhausted()` and raise `FirecrawlCreditsExhaustedError()` without referencing `response.text`. Preserve existing handling for other statuses. Make no circuit check in `close_session()` or `emergency_cleanup()`.

- [ ] **Step 4: Write failing local-fallback metadata tests**

Extend `tests/tools/test_browser_cloud_fallback.py`:

```python
@pytest.mark.parametrize(
    "error,reason",
    [
        (FirecrawlCreditsExhaustedError(), "provider_credits_exhausted"),
        (FirecrawlCircuitOpenError(), "provider_circuit_open"),
    ],
)
def test_firecrawl_credit_errors_use_stable_local_fallback_reason(monkeypatch, error, reason):
    _reset_session_state(monkeypatch)
    provider = Mock(name="firecrawl-provider")
    provider.name = "firecrawl"
    provider.create_session.side_effect = error
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
    session = browser_tool._get_session_info(f"task-{reason}")
    assert session["features"]["local"] is True
    assert session["fallback_reason"] == reason
    assert "response" not in session["fallback_reason"]
```

- [ ] **Step 5: Implement stable metadata mapping**

In `_get_session_info()`'s exception branch, keep existing logging and fallback for unrelated providers, but map Task 1 exceptions without `str(e)`:

```python
if isinstance(e, FirecrawlCreditsExhaustedError):
    fallback_reason = "provider_credits_exhausted"
elif isinstance(e, FirecrawlCircuitOpenError):
    fallback_reason = "provider_circuit_open"
else:
    fallback_reason = str(e)
```

Set `fallback_provider` to `provider.name` when available, otherwise the class name, without changing current tests for generic mock providers.

- [ ] **Step 6: Run browser-focused regressions**

```bash
python -m pytest tests/plugins/browser/test_browser_provider_plugins.py tests/tools/test_browser_cloud_fallback.py tests/tools/test_browser_cleanup.py -q
```

Expected: all pass; DELETE positive controls prove cleanup remains wired.

- [ ] **Step 7: Commit browser circuit behavior**

```bash
git status --short
git add plugins/browser/firecrawl/provider.py tests/plugins/browser/test_browser_provider_plugins.py tools/browser_tool.py tests/tools/test_browser_cloud_fallback.py
git commit -m "feat(browser): honor Firecrawl credit circuit"
```

---

### Task 6: Scheduler-owned Scout lifecycle and action finalizer

**Files:**
- Modify: `cron/scheduler.py:1405-1527,4868-5105,5236-5570`
- Create: `tests/cron/test_scout_firecrawl_run.py`
- Modify: `tests/cron/test_scheduler.py:4711-end` only if existing generic iteration fixtures need direct regression coverage.

**Interfaces:**
- Consumes: Task 1 state install/reset/claim/evidence and existing `canonical_agent_source()`.
- Produces private scheduler helpers:
  - `_install_scout_firecrawl_run(job: dict) -> tuple[FirecrawlRunState | None, contextvars.Token | None]`
  - `_finalize_agent_iteration_event(emitter, job: dict, final_response: str, *, success: bool, firecrawl_state: FirecrawlRunState | None) -> None`
  - `_reset_scout_firecrawl_run(token) -> None`

- [ ] **Step 1: Write failing canonical-owner and context propagation tests**

Create `tests/cron/test_scout_firecrawl_run.py`. Patch the normal execution pipeline as `tests/cron/test_run_one_job.py` does, but make `fake_run_job()` inspect `current_firecrawl_run()`:

```python
def test_run_one_job_installs_state_only_for_canonical_scout(monkeypatch):
    seen = []
    # Run jobflow-scout and tracker jobs through run_one_job.
    # fake_run_job appends whether current_firecrawl_run() is present.
    assert seen == [("jobflow-scout", True), ("jobflow-tracker", False)]
    assert current_firecrawl_run() is None


def test_state_is_reset_when_run_job_raises(monkeypatch):
    # fake_run_job asserts context exists then raises RuntimeError.
    # run_one_job returns False and current_firecrawl_run() is None afterward.
```

Use the actual canonical job names from `events.producers.agent_source_mapping`; do not match Scout by substring.

- [ ] **Step 2: Run lifecycle tests and verify RED**

```bash
python -m pytest tests/cron/test_scout_firecrawl_run.py -k "installs or reset" -q
```

Expected: Scout sees no installed run state.

- [ ] **Step 3: Implement scheduler lifecycle helpers around both fire paths**

Use lazy imports to preserve scheduler boot behavior. `_install_scout_firecrawl_run()` canonicalizes `job["name"]` and installs only when it resolves to `scout`. In both `run_one_job()` and tick's `_process_job()`:

1. initialize `firecrawl_state = None`, `firecrawl_token = None`, `success = False`, and `final_response = ""` before execution;
2. install immediately before `run_job()` so its `copy_context()` carries the same state into the conversation and tool workers;
3. call the shared finalizer exactly once in an outer `finally` after success/failure variables settle;
4. reset the token after the finalizer, even if finalization raises;
5. preserve deferred-agent teardown and existing deadline/abandon behavior.

Do not install for `no_agent` jobs or non-Scout jobs. Do not mutate the job dictionary and do not change `run_job()`'s four-tuple return contract.

- [ ] **Step 4: Write failing valid/missing/malformed/failed action tests**

Use a fake emitter whose `.bus.emit()` records event type and payload. Add tests for:

- valid marker: existing payload is augmented with `action_required=True`, `action_kind="credits"`, and sanitized `provider_error`/`provider_scope`; only one `AGENT_ITERATION`;
- missing marker: one synthesized Scout `AGENT_ITERATION` with the credits pair;
- malformed marker: existing `AGENT_ERROR` remains and exactly one synthesized credits `AGENT_ITERATION` is also emitted;
- failed run after recording 402: one credits `AGENT_ITERATION` despite `success=False`;
- no 402: failed run emits no new generic iteration;
- two record calls: one credits iteration;
- `bus.emit()` raises after claim: finalizer logs/preserves the failure and a second finalizer call emits nothing (at-most-once attempt).

Have `fake_run_job()` call `record_firecrawl_credits_exhausted()` inside the installed context; do not manually inject state from outside the owner seam.

- [ ] **Step 5: Run action tests and verify RED**

```bash
python -m pytest tests/cron/test_scout_firecrawl_run.py -k "credits or marker or failed or emit" -q
```

Expected: no action augmentation/failure-path iteration exists yet.

- [ ] **Step 6: Refactor generic iteration emission into one finalizer**

Change `_emit_agent_iteration_event()` to accept optional credit augmentation or introduce `_finalize_agent_iteration_event()` around it. Required behavior:

```python
credits_owed = bool(
    firecrawl_state
    and firecrawl_state.first_failure
    and firecrawl_state.claim_credits_action()
)
```

Claim immediately before the one credits `bus.emit()` attempt, not during payload parsing. For valid and missing markers, add:

```python
payload.update({
    "action_required": True,
    "action_kind": "credits",
    "provider_error": "provider_credits_exhausted",
    "provider_scope": "account",
})
```

For malformed markers, emit the existing `AGENT_ERROR` first, then synthesize one credits-gated Scout iteration. For failed runs with no credit state, preserve existing behavior by emitting no generic iteration. For successful ordinary runs, preserve current valid/missing/malformed semantics.

Remove the two existing success-gated direct calls to `_emit_agent_iteration_event()` and replace them with exactly one finalizer call per path. Keep `_emit_tailor_iteration_event()` success-gated and unchanged.

- [ ] **Step 7: Prove scheduled tick and direct/provider fire share the lifecycle**

Add one test that drives `run_one_job()` and one that drives `tick(sync=True)` with a due Scout job. Both fake `run_job()` implementations record a 402. Assert each path emits exactly one credits-gated iteration and leaves `current_firecrawl_run() is None` afterward.

- [ ] **Step 8: Run scheduler-focused regressions**

```bash
python -m pytest tests/cron/test_scout_firecrawl_run.py tests/cron/test_run_one_job.py tests/cron/test_scheduler.py -k "agent_iteration or run_one_job or scout_firecrawl or tick_process_job" -q
```

Then run deadline/context coverage:

```bash
python -m pytest tests/cron/test_cron_wallclock_timeout.py tests/cron/test_cron_inactivity_timeout.py tests/cron/test_shutdown_interrupt.py tests/cron/test_cron_profile.py -q
```

Expected: all pass; context is absent after every tested completion mode.

- [ ] **Step 9: Commit scheduler ownership**

```bash
git status --short
git add cron/scheduler.py tests/cron/test_scout_firecrawl_run.py tests/cron/test_scheduler.py
git commit -m "feat(cron): finalize Scout Firecrawl credits once"
```

If `tests/cron/test_scheduler.py` is unchanged, omit it from `git add`.

---

### Task 7: Routing replay, end-to-end run proof, and final verification

**Files:**
- Modify: `tests/events/test_routing_replay.py`
- Modify: `tests/events/test_routing_policy.py` only if strengthening the exact pair adds coverage beyond its existing parameterized tests.
- Create: `tests/integration/test_scout_firecrawl_credit_circuit.py`
- Create: `docs/superpowers/plans/2026-08-02-scout-firecrawl-run-circuit.md` (this plan; commit with final verification if not already committed)

**Interfaces:**
- Consumes: all prior task interfaces and existing `events.routing_replay.replay_audit()`.
- Produces: one deterministic cross-component acceptance test; no new production API.

- [ ] **Step 1: Tighten routing replay to assert one exact credits destination**

Extend the existing replay fixture with a single Scout credits-gated `AGENT_ITERATION`, then assert:

```python
credits_rows = [
    row for row in report["rows"]
    if row["payload"].get("action_kind") == "credits"
]
assert len(credits_rows) == 1
assert credits_rows[0]["attention"] == "act"
assert credits_rows[0]["topic"] == "action_required"
assert credits_rows[0]["destination_count"] == 1
```

Keep the existing destination totals internally consistent. Do not change routing policy production code; the exact pair is already authoritative.

- [ ] **Step 2: Write the failing end-to-end deterministic activation test**

Create `tests/integration/test_scout_firecrawl_credit_circuit.py` with mocked network seams and real state/provider/tool/finalizer integration. The test must demonstrate in one Scout context:

1. closed-circuit search reaches Firecrawl and receives a synthetic status-402 exception;
2. fallback search provider runs once;
3. later Firecrawl extraction and browser session creation are rejected before their mocked network seams;
4. extraction fallback and local Chromium execute;
5. cleanup DELETE remains callable;
6. scheduler finalization emits one credits pair;
7. ordinary TimeoutError in a fresh activation does not open the circuit or emit credits;
8. the next installed activation starts closed.

Use only monkeypatched providers/HTTP functions. Assert positive-control call counts before opening so a zero-call result cannot pass with an unwired mock.

- [ ] **Step 3: Run integration and replay tests; inspect the intended failure if any seam is missing**

```bash
python -m pytest tests/integration/test_scout_firecrawl_credit_circuit.py tests/events/test_routing_replay.py tests/events/test_routing_policy.py -q
```

Expected before any needed glue correction: the integration test identifies the first missing cross-component seam; after prior tasks are complete it should pass without production routing changes.

- [ ] **Step 4: Make only minimal cross-component corrections**

If the end-to-end test exposes a mismatch, change the owning component from Tasks 1–6 rather than adding logic to the integration test. Valid corrections include import placement, stable code mapping, fallback-result normalization, or scheduler finalizer wiring. Invalid corrections include text sniffing, a global registry, live configuration edits, retrying Firecrawl, or weakening assertions.

- [ ] **Step 5: Run focused feature suite**

```bash
python -m pytest tests/agent/test_firecrawl_run_state.py tests/plugins/web/test_web_search_provider_plugins.py tests/tools/test_web_firecrawl_run_fallback.py tests/plugins/browser/test_browser_provider_plugins.py tests/tools/test_browser_cloud_fallback.py tests/tools/test_browser_cleanup.py tests/cron/test_scout_firecrawl_run.py tests/events/test_routing_policy.py tests/events/test_routing_replay.py tests/integration/test_scout_firecrawl_credit_circuit.py -q
```

Expected: all pass, no skipped feature tests, no external requests.

- [ ] **Step 6: Run broader affected regressions**

```bash
python -m pytest tests/cron/test_run_one_job.py tests/cron/test_scheduler.py tests/cron/test_cron_wallclock_timeout.py tests/cron/test_cron_inactivity_timeout.py tests/cron/test_shutdown_interrupt.py tests/tools -k "web or browser_cloud_fallback or browser_cleanup" -q
```

Expected: all selected tests pass. If the `-k` selection accidentally collects an unbounded unrelated suite, split it into the exact files reported by `pytest --collect-only`; do not silently skip failing affected tests.

- [ ] **Step 7: Run static and diff checks**

```bash
python -m compileall -q agent/firecrawl_run_state.py agent/web_search_registry.py plugins/web/firecrawl/provider.py plugins/browser/firecrawl/provider.py tools/web_tools.py tools/browser_tool.py cron/scheduler.py
```

Expected: exit 0.

```bash
git diff --check
git status --short
git diff --stat main...HEAD
```

Expected: no whitespace errors; only task-owned nested-repository paths. Confirm explicitly that `profiles/main/cron/jobs.json`, Scout workspace files, and root `.hermes` files are absent.

- [ ] **Step 8: Commit replay/integration coverage and plan**

```bash
git add tests/events/test_routing_replay.py tests/events/test_routing_policy.py tests/integration/test_scout_firecrawl_credit_circuit.py docs/superpowers/plans/2026-08-02-scout-firecrawl-run-circuit.md
git commit -m "test(firecrawl): prove Scout-wide credit circuit"
```

Omit any unchanged path from `git add`. Because `docs/superpowers/*` is ignored in this repository, use `git add -f docs/superpowers/plans/2026-08-02-scout-firecrawl-run-circuit.md` for the plan only.

- [ ] **Step 9: Final acceptance report**

Report:

- exact commits created;
- focused and broader test commands with pass/fail counts;
- the first-402 and transient positive controls;
- confirmation that later admitted Firecrawl requests were skipped while cleanup ran;
- confirmation that exactly one routing-v3 credits action was emitted;
- confirmation that no live Firecrawl call, config edit, gateway restart, or unrelated dirty-file change occurred.
