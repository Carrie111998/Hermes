# Scout-wide Firecrawl credits circuit — design

**Date:** 2026-08-02

**Status:** Approved for implementation planning

**Extends:** `C:/Users/diego/.hermes/docs/superpowers/specs/2026-08-02-mission-control-alert-triage-design.md` §6.10

**Scope:** The nested `agent-src` repository. One `jobflow-scout` cron activation, including Firecrawl web search, web extraction, and Firecrawl cloud-browser session creation.

## 1. Problem

Committed behavior at `20aebb713` correctly classifies a Firecrawl HTTP 402 as account-level, non-retryable credit exhaustion. It also skips later URLs inside the same `FirecrawlWebSearchProvider.extract()` invocation. The circuit ends with that invocation.

A Scout activation can perform multiple tool calls. After one confirmed account-level 402, later search, extract, or cloud-browser calls can still make charge-bearing Firecrawl requests. Each failure can also become separate action evidence, producing repeated noise for one account condition.

The run therefore needs one deterministic owner that:

1. Encloses every Firecrawl call made during one Scout activation.
2. Opens one run-level circuit after the first confirmed HTTP 402.
3. Makes one fallback decision for the rest of that run.
4. Produces at most one genuine credits action signal.

## 2. Goals and non-goals

### Goals

- Scope the circuit to exactly one canonical `jobflow-scout` cron activation.
- Stop later charge-bearing Firecrawl operations after the first confirmed HTTP 402.
- Cover Firecrawl search, extraction, and cloud-browser session creation.
- Preserve browser cleanup after the circuit opens.
- Make one first-writer-wins fallback decision: continue the run without Firecrawl.
- Execute that decision through one capability-appropriate fallback, without a fallback chain.
- Emit at most one structured credits human gate for the activation.
- Preserve routing-v3's existing structured human-gate semantics.
- Preserve public response envelopes, URL ordering, SSRF gates, website-access policy, and sanitized diagnostics.
- Build through deterministic TDD without live Firecrawl calls.

### Non-goals

- A process-global or cross-run Firecrawl account breaker.
- Persisting the circuit across cron activations or gateway restarts.
- Changing routing-v3 classification or WhatsApp escalation policy.
- Retrying Firecrawl after a confirmed 402 within the same run.
- Trying a sequence of multiple fallback providers.
- Blocking `close_session` or `emergency_cleanup` operations.
- Editing live cron configuration, Scout workspace artifacts, or unrelated dirty files.
- Restarting the gateway.

## 3. Deterministic run owner

The owner is the scheduler's execution of one canonical `jobflow-scout` activation.

`cron/scheduler.py` constructs an ephemeral agent and runs its entire conversation inside a copied context:

```python
_cron_context = contextvars.copy_context()
_cron_future = _cron_pool.submit(
    _cron_context.run,
    agent.run_conversation,
    prompt,
)
```

The tool executor also propagates context into concurrent tool worker threads, and `asyncio.to_thread()` propagates the current context into synchronous provider work. This makes a scheduler-installed `ContextVar` visible across the complete conversation without attaching mutable state to `AIAgent` or introducing a process-global registry.

The scheduler installs the context only when the canonical job identity is `jobflow-scout`. Installation returns both the `ContextVar` token and the mutable state object. The scheduler retains that state object for finalization even after worker execution ends. One shared activation-finalization seam receives it for every completion path, emits any owed action evidence, and only then resets the token in a guaranteed outer `finally`. This ordering covers successful, failed, timed-out, malformed-response, and cancelled executions without trying to rediscover state through a reset context. Context absence preserves current behavior for interactive use, non-Scout jobs, direct provider tests, and other callers.

Both scheduled ticks and direct/manual execution of the same cron job must invoke this shared finalizer exactly once. Existing success-gated generic iteration emission may remain for ordinary runs, but it cannot be the sole credits finalizer because credit exhaustion can precede a later execution failure.

The activation—not an individual conversation turn, tool invocation, URL, or provider instance—is the lifecycle boundary.

## 4. Run-state component

Add one focused module under `agent/` that owns Firecrawl run state and exposes narrow operations. Exact names may follow local conventions, but the interface must support:

- Installing and resetting a Scout Firecrawl run context.
- Reading whether the run circuit is open.
- Atomically recording the first confirmed Firecrawl credit exhaustion.
- Reading sanitized first-failure evidence.
- Atomically selecting the run fallback decision and memoizing one provider choice per web capability.
- Atomically claiming the one credits action emission attempt.

The context value is a mutable state object guarded by a lock. A copied Python context carries the same state-object reference into worker threads; the lock makes simultaneous 402 responses deterministic.

### Required state semantics

The first successful transition from closed to open wins. It records only stable, sanitized evidence:

```json
{
  "code": "provider_credits_exhausted",
  "provider": "firecrawl",
  "scope": "account",
  "retryable": false
}
```

No response body, request URL, headers, credentials, or exception representation enters run state or event evidence.

Later charge-bearing calls observe the open circuit and return or raise stable circuit evidence:

```json
{
  "code": "provider_circuit_open",
  "provider": "firecrawl",
  "scope": "account",
  "retryable": false
}
```

Ordinary exceptions, timeouts, HTTP statuses other than 402, malformed provider output, and policy blocks do not open the circuit.

### Concurrency invariant

If concurrent Firecrawl calls encounter 402 responses, exactly one call performs the closed-to-open transition. All callers then observe the same open state, fallback decision, provider selections, and signal-claim state. At-most-once behavior must not depend on call ordering or the GIL.

The circuit is an admission gate, not cancellation of requests already admitted or in flight. A call that passed its final pre-network admission check before another call confirmed a 402 may complete its already-authorized request. Every operation reaching admission after the open transition is denied. Tests must pin this boundary explicitly rather than claim that the circuit can retract concurrent network I/O already underway.

## 5. Covered and permitted operations

### Charge-bearing operations blocked after opening

- `FirecrawlWebSearchProvider.search()` network calls.
- Firecrawl web extraction/scrape network calls.
- `FirecrawlBrowserProvider.create_session()` POST to `/v2/browser`.

Each operation checks the run circuit immediately before its Firecrawl network request. The provider still classifies a genuine HTTP 402 at the boundary where the status is known, then records it in shared run state.

### Operations still permitted

- `FirecrawlBrowserProvider.close_session()`.
- `FirecrawlBrowserProvider.emergency_cleanup()`.

Cleanup may release a remote resource created before exhaustion and must never be blocked by the circuit. Existing best-effort cleanup semantics remain intact.

### Outside a Scout run

With no run context installed, existing invocation-local behavior remains. In particular, direct and interactive extraction still skips later URLs in the same invocation after an initial 402; this design does not silently introduce a process-wide circuit.

## 6. One fallback decision, capability-specific execution

The first confirmed 402 atomically selects one run-level decision:

> Continue this Scout activation without Firecrawl.

This policy decision is made once. Individual tool surfaces execute it according to their capability, but do not independently select policy or start provider chains.

For web capabilities, the run state separately memoizes one search-provider choice and one extract-provider choice. Resolution is lock-protected first-writer-wins, so repeated or concurrent calls use the same provider for that capability. Search and extract may select different providers because registry capabilities differ. Cloud browser has no provider-choice slot: its approved fallback is the existing local Chromium path.

### Deterministic web fallback resolution

The fallback resolver uses the existing registry's normal capability priority and availability checks, with `firecrawl` excluded. This preserves the established order (`parallel`, `tavily`, `exa`, `searxng`, `brave-free`, `ddgs` after Firecrawl is removed) rather than inventing a second policy. An explicitly configured non-Firecrawl provider wins when it supports the requested capability and is available. An explicitly configured Firecrawl provider is excluded only on this post-exhaustion path; resolution then continues through the existing capability order. The first resolved provider, including an explicit “none available” sentinel, is memoized for that capability for the rest of the run.

### Web search

When Firecrawl search returns the first confirmed credit failure—or when a later search call finds the run circuit open—`web_search_tool` uses the memoized non-Firecrawl search provider and attempts it once.

- Normal explicit-provider semantics remain unchanged before exhaustion.
- The exclusion is narrow to this fallback path.
- The fallback cannot resolve back to Firecrawl.
- If no non-Firecrawl search provider is available, the tool returns the stable credit/circuit result without another network attempt.
- A fallback failure is returned normally; no second fallback provider is tried.

### Web extraction

`web_extract_tool` preserves already successful results, policy-blocked results, input ordering, and duplicate-URL reconstruction. It retries only entries left unresolved by `provider_credits_exhausted` or `provider_circuit_open`, using one available extract provider excluding Firecrawl.

- One fallback provider is selected for the capability.
- Unresolved entries are attempted once through that provider.
- Existing SSRF and website-access checks remain authoritative; fallback never bypasses them.
- Successful Firecrawl results from before the 402 are not repeated.
- If no fallback exists or it fails, affected entries remain explicitly incomplete with sanitized diagnostics.
- A whole-call fallback exception is converted into one sanitized failure result for each unresolved input entry; it does not replace preserved results.
- A malformed, short, overlong, or URL-misaligned fallback result is normalized only across the unresolved subset. Missing or unusable positions become sanitized incomplete entries, extra positions are ignored, and the original full-input ordering remains authoritative.
- No retry returns to Firecrawl, and no fallback chain is attempted.

### Cloud browser

`tools/browser_tool.py` already falls back from cloud-session creation failure to local Chromium. A Firecrawl 402 and a pre-request circuit-open condition use that existing branch.

The local-session result retains existing fallback metadata, with a stable reason that distinguishes credit exhaustion from a skipped circuit-open request. It must not expose Firecrawl response text.

### Deferred enrichment

Fallback success can allow Scout to continue, but it does not make the Firecrawl account condition disappear. If fallback cannot complete enrichment, Scout preserves the incomplete candidate for later enrichment under its existing workflow rather than manufacturing data or emitting one failure per URL.

## 7. One genuine credits action signal

The first confirmed HTTP 402 is the only event that creates credits action evidence. A later `provider_circuit_open` observation is derivative and cannot create another action signal.

The scheduler is the deterministic emission seam. At the end of the activation it reads the run state and, if genuine credit exhaustion was recorded and the signal has not been consumed, ensures one Scout `AGENT_ITERATION` carries:

```json
{
  "action_required": true,
  "action_kind": "credits"
}
```

This augmentation applies regardless of whether the model response contains:

- a valid `<AGENT_ITERATION_JSON>` marker,
- no marker,
- a malformed marker, or
- no usable final response because execution failed after the 402.

The scheduler must not attempt a second credits-gated `AGENT_ITERATION` for the same activation. Immediately before `bus.emit()`, the shared finalizer atomically claims the run's single emission attempt. This is deliberately an **at-most-once attempt** contract: if `bus.emit()` itself raises after the claim, the scheduler preserves/logs that emission failure but does not retry and risk duplicate delivery from an ambiguous partial emit. Existing event-bus durability begins once `emit()` accepts the event. The claim operation is the source of the scheduler-side at-most-once guarantee.

Fallback success does not suppress the action signal: local or alternate-provider completion does not restore Firecrawl account credits.

The payload may include stable diagnostic fields such as the sanitized provider code and account scope. It must not include response bodies, secrets, request data, or per-URL copies of the same failure.

## 8. Routing-v3 contract

`events/routing_policy.py` already treats this pair as an authoritative structured human gate:

```json
{
  "action_required": true,
  "action_kind": "credits"
}
```

This design does not change that classifier, add prose sniffing, or broaden Action Required. The existing route remains:

- attention: `ACT`,
- destination: Action Required,
- existing immediate/urgent WhatsApp escalation behavior.

One activation produces at most one actionable credits item. Provider diagnostics and circuit-open tool results are not independently routed as human gates.

## 9. Failure behavior

| Condition | Circuit | Fallback | Credits action |
|---|---|---|---|
| First confirmed Firecrawl HTTP 402 | Opens atomically | Select continue-without-Firecrawl once | Eligible exactly once |
| Later Firecrawl operation in same run | Network call skipped | Execute chosen capability fallback at most once per tool need | None |
| Concurrent HTTP 402 responses | First writer opens; others observe open | One shared decision | One total |
| HTTP 429/5xx, timeout, connection error | Remains closed | Existing behavior | None |
| SSRF or website-policy block | Remains closed | No bypass | None |
| No non-Firecrawl web fallback | Remains open | Return incomplete stable result | One total from original 402 |
| Alternate provider fails | Remains open | Return fallback failure; no chain | One total from original 402 |
| Firecrawl browser cleanup | No effect | Cleanup proceeds | None |
| Context absent | Existing invocation behavior | Existing behavior | None from run coordinator |

## 10. TDD strategy

Implementation proceeds test-first. Each behavior is first expressed as a failing test and observed failing for the intended reason before production code changes.

### Run-state unit tests

- Context absent is a no-op and preserves existing behavior.
- First confirmed credit failure opens the circuit and records sanitized evidence.
- A second credit failure cannot replace first-failure state.
- Concurrent record attempts yield one transition.
- Fallback policy and per-capability provider choices are first-writer-wins and stable, including a memoized no-provider result.
- Credits action claim returns true once and false thereafter.
- Reset prevents state leaking into a later run.
- Ordinary/transient failures do not open the circuit.

### Firecrawl web-provider tests

- Search checks an open run circuit before network I/O.
- A genuine search HTTP 402 records the run failure.
- Extraction HTTP 402 opens the run circuit and skips remaining Firecrawl URLs.
- A later provider invocation in the same run performs no Firecrawl request.
- HTTP 429/5xx and ordinary exceptions do not open the run circuit.
- Existing outside-context invocation-local tests continue to pass.

### Web-tool fallback tests

- Search selects one non-Firecrawl provider after the first 402.
- A later search in the same run skips Firecrawl and uses the same run decision.
- Search cannot resolve its fallback back to Firecrawl.
- No available fallback returns sanitized incomplete evidence.
- Extract retries only credit/circuit entries.
- Extract preserves successful, policy-blocked, duplicate, and ordered entries.
- Extract uses one fallback provider and never chains.
- Transient Firecrawl failures retain current behavior and do not invoke credits fallback policy.

### Browser tests

- `/v2/browser` HTTP 402 is classified without exposing response text and opens the run circuit.
- An open circuit blocks later session-creation POSTs before network I/O.
- First-402 and circuit-open paths both use existing local Chromium fallback.
- `close_session` and `emergency_cleanup` still issue cleanup after the circuit opens.
- Non-402 browser failures do not open the run circuit.

### Scheduler and event tests

- Only canonical `jobflow-scout` installs the run context.
- Context reaches conversation execution; the retained state reaches one shared finalizer; reset occurs after finalization on success, exception, timeout, and cancellation.
- Scheduled-tick and direct/manual cron execution paths invoke the same finalizer exactly once.
- A valid iteration marker is augmented with one credits gate.
- A missing marker synthesizes one credits-gated Scout iteration.
- A malformed marker or failed final response still yields at most one credits-gated Scout iteration while preserving existing error evidence.
- No confirmed 402 produces no credits gate.
- Multiple provider observations in one activation still emit one gate.

### Routing and replay regression tests

- The exact structured credits pair routes to ACT/Action Required under routing-v3.
- Incomplete or invalid pairs do not.
- Replay of one Scout credits-gated iteration produces one Action Required destination and no duplicate delivery.

### Verification

Run focused suites first, then the relevant scheduler, web, browser, and event regressions. No test makes a live Firecrawl request. Every mocked suppression assertion includes a positive control proving the network seam would have been called with the circuit closed.

## 11. Implementation boundaries

Expected task-owned changes are limited to the nested `agent-src` repository:

- one focused run-state module under `agent/`,
- `cron/scheduler.py`,
- Firecrawl web and browser providers,
- web and browser tool integration,
- a narrow non-Firecrawl provider-resolution helper if required,
- focused tests for those components and routing replay,
- this specification and the later implementation plan.

The implementation must not modify:

- `C:/Users/diego/.hermes/profiles/main/cron/jobs.json`,
- Scout workspace files,
- root `.hermes` dirty work,
- live provider configuration,
- gateway process state.

Before every commit, inspect nested-repository status and stage explicit task-owned paths only.

## 12. Acceptance criteria

A deterministic test activation proves all of the following:

1. Scout begins with a closed run circuit.
2. A charge-bearing Firecrawl operation receives a genuine HTTP 402.
3. The response is classified as account-level, non-retryable credit exhaustion.
4. The run circuit opens exactly once.
5. Every Firecrawl search, extract, and cloud-session creation operation admitted after the open transition is skipped before network I/O; requests already admitted or in flight are not retroactively cancelled.
6. Browser cleanup remains permitted.
7. The run makes one continue-without-Firecrawl decision.
8. Each affected capability uses at most one non-Firecrawl/local fallback without chaining.
9. Successful and policy-blocked extract results retain order and are not retried.
10. Unresolved enrichment remains explicit and deferred.
11. The scheduler emits at most one `action_required=true`, `action_kind=credits` Scout iteration.
12. Routing-v3 sends that signal to Action Required with existing WhatsApp behavior.
13. A normal transient provider failure opens no circuit and emits no credits action.
14. A subsequent Scout activation starts with a fresh closed circuit.
15. No unrelated dirty work, live configuration, or gateway state changes.
