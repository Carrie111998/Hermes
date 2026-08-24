# Goal-mode judge transport failure

## What

Goal-mode completion and review handoffs must fail open only when `judge_goal()` explicitly classifies an auxiliary-model transport failure. A provider/model 404 is infrastructure failure, not a substantive `continue` judgment on the worker's evidence.

## Why

The K3 bake-off seats use the normalized model ID `moonshotai/kimi-k3`. Their main inference path rewrites that route for Fireworks, but the auxiliary goal judge reached Fireworks with the normalized ID and received HTTP 404. `judge_goal()` correctly returned `transport_failed=True`; both handoff gates discarded that flag and rejected completion as though the judge had found the work incomplete.

An earlier implementation also isolated delegated review children from their parent's `HERMES_KANBAN_*` authority. Equivalent, more comprehensive child isolation is already present on current `main`, so this rebased change does not duplicate it.

## How

- Preserve the full five-value `judge_goal()` contract in the tool and CLI handoff gates.
- Allow completion and review only when `transport_failed is True`.
- Continue rejecting `continue`, `wait`, `skipped`, and parsed failures.
- Validate verdict, reason, and boolean flags so malformed return contracts or uncaught implementation exceptions cannot silently bypass the acceptance gate.
- Cover tool and CLI completion/review paths plus the contract matrix.

## What could go wrong

- Failing open on every exception would allow implementation bugs to disable the gate. Only the explicit transport signal bypasses it.
- Treating truthy non-booleans as transport failures would also bypass the gate. The return contract is type-checked before the signal is honored.
- Failing closed on a real provider outage would re-wedge workers. `judge_goal()` already catches provider/network exceptions and emits `transport_failed=True`; regression tests pin that behavior at both handoff surfaces.
