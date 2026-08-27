# Deadline-layer conformance cells — Phase 3: poisoned-state contract

Machine-checked acceptance cells for Phase 3a of #85125. The cells became
hard-green after the Phase 3a protocol salvage (#93796) and conformance harness
salvage (#93794) merged.

## Contract under test

- `run_bounded_async` and `run_bounded_sync` accept an optional `backend=`.
  When a bounded call times out, the layer calls
  `backend.mark_suspect(reason)` exactly once. A call that completes on time
  never marks its backend.
- Backends without `mark_suspect` are tolerated. Incremental Phase 3b adoption
  must never weaken the deadline bound.
- Reuse recycling through `ensure_healthy()` remains consumer-scoped. The
  shared layer only publishes the suspect mark.

The cells are deterministic and LLM-free. They exercise the real
`agent.deadline` machinery without mocking the layer under test. A failure is
a contract regression and must not be converted back to `xfail`.
