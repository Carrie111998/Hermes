# PA replay run lifecycle

`hermes replay-run` is the PA replay orchestrator of record. It owns the run state around the native Hermes replay primitive and the systems-pcl replay target provider.

Boundary:

- Hermes `replay` executes normal gateway turns under `agent:replay:<run-id>`.
- `hermes replay-run` mints the run id, persists the run manifest, enforces the mechanical verify gate, and is the only Hermes-side path that calls provider promote/rollback.
- The target provider owns target preparation, run-id write guards, provider-specific invariants, promote, and rollback.

## State machine

```text
initialized
  -> preparing_target
  -> prepared
  -> running_agent_replay
  -> replayed
  -> verifying
  -> verified
  -> promoting
  -> promoted
  -> rolling_back
  -> rolled_back
```

Failure/abandon paths move to `dirty` or `failed`. Dirty runs are terminal and cannot promote. A promoted run may still roll back through the provider promotion manifest.

## Start a replay run

The start command performs `prepare_target -> run_agent_replay -> verify` and stops before promote.

```bash
export PS_REPLAY_PROVIDER_URL="http://127.0.0.1:5192"
export PS_REPLAY_PROVIDER_ADMIN_TOKEN="..."

hermes replay-run start \
  --provider-url "$PS_REPLAY_PROVIDER_URL" \
  --provider-admin-token "$PS_REPLAY_PROVIDER_ADMIN_TOKEN" \
  --source-data-dir /path/to/source-ps-data \
  --target-data-dir /path/to/fresh-eval-ps-data \
  --target-base-url http://127.0.0.1:5192 \
  --plan /path/to/replay-plan.json \
  --out-dir /path/to/replay-runs \
  --tool-error-budget 0
```

The command writes:

- `run-manifest.json` — state machine, target descriptor/baseline digests, attempt digests, gate result.
- `target-prepare.json` — provider prepare result without token literals.
- `replay-plan.json` — the exact `ReplayPlan` handed to Hermes.
- `attempt-<attempt-id>.json` — replay result + attempt provenance.
- `verify-report.json` — mechanical gate checks.

## Mechanical verify gate

Promote is refused unless all checks pass:

- corpus parity: processed message count equals the deterministic corpus count.
- processed-turn coverage: PA turn records exist and cover source message ids.
- zero failed PA turns.
- zero escaped outbound sends: captured replay outbounds
  (`delivery_mode=capture`) are reported for review but do not fail the gate;
  any non-capture outbound remains a hard-fail capture-lock leak.
- tool-error budget not exceeded.
- provider descriptor/baseline digests match their manifests.
- attempt/code/replay-policy digests match persisted manifests.
- provider invariants pass through `POST /api/operator/replay-target/verify`.

Re-run the gate:

```bash
hermes replay-run verify \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --session-db ~/.hermes/state.db \
  --tool-error-budget 0
```

A failed verify marks the target dirty locally and through the provider dirty endpoint. Dirty targets cannot promote; rebuild from a fresh baseline and rerun.

## Promote / rollback

Promotion must be called through the orchestrator manifest. Do not call the provider promote endpoint directly.

For this phase, use only non-prod target directories. Real TGG production promote is held for the gated validation phase.

```bash
hermes replay-run promote \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --prod-data-dir /path/to/non-prod-prod-dir \
  --confirm ORCHESTRATOR_PROMOTE
```

Rollback uses the provider promotion manifest recorded in `run-manifest.json`:

```bash
hermes replay-run rollback \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json
```

If the provider manifest path must be supplied manually:

```bash
hermes replay-run rollback \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --promotion-manifest-path /path/to/provider/promotions/<promotion>.json
```

## Recovery commands

Inspect current state:

```bash
hermes replay-run status --manifest /path/to/replay-runs/<run-id>/run-manifest.json
```

Mark an abandoned or suspect run dirty:

```bash
hermes replay-run dirty \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --reason "abandoned after partial replay"
```

Fresh-baseline-only rule: never promote an in-place resumed target. If a run dies after target preparation or replay starts, mark it dirty, prepare a new target data dir, and rerun from the original corpus/plan.
