# Muncho release versioning

Muncho SemVer is a human-friendly alias. The immutable 40-character Git SHA
remains the security identity, deployment address, production gate, and
rollback identity. The upstream Hermes package version is independent and is
not changed by a Muncho release.

The source-controlled contract lives in `ops/muncho/release/`:

- `metadata.json` declares the next Muncho version and 3–6 short user-facing
  changes. It may optionally carry known limitations and a rollback note.
- `history.json` is an append-only version-to-SHA history. Existing entries
  are never edited or reordered. A version can identify only one exact SHA.
- Both documents use canonical JSON, exact field sets, strict SemVer/SHA
  parsing, and self-digests. Invalid or partial metadata fails closed in a
  release path. An upstream Hermes tree with neither file has a clean
  no-Muncho fallback.

The historical prefix is deliberately retrospective:

- `v2.3.0` → `62fbf327b3507a97a34807bf4834d35c396817de`
- `v2.3.1` → `5564ec24a48d819e8ba0dd924bdb82ca5064ed4c`

Both records set `metadata_present_at_source=false`. This records the releases
without claiming that either old source tree contained metadata added later.
The first source tree carrying this package declares the next patch release,
`v2.3.2`.

## Version policy

- Backward-compatible fixes after `v2.3.1` increment the patch:
  `v2.3.2`, `v2.3.3`, and so on.
- A meaningful new capability increments the minor version.
- A breaking change or authority/security-boundary change increments the
  major version.

The exact Git SHA always remains visible beside the alias. CLI and gateway
`/version` replies show Muncho, the unchanged upstream Hermes version, and the
full plus short release SHA. Official upstream installations without Muncho
metadata retain the original Hermes-only reply.

## Production completion

Reserve `(Muncho version, exact SHA)` before mutation. The create-only mapping
receipt burns that pair even when a later deploy step fails; a corrected source
commit therefore needs a new version. This prevents a familiar version label
from being silently reassigned.

A release is complete only in this order:

1. before activation, the wrapper durably records the current systemd
   `InvocationID` against the reserved version/SHA mapping and planned-stop
   marker;
2. after restart, the service has a different valid `InvocationID`, consumes
   the marker, runs the exact deployed SHA, and passes all required production
   checks/smokes; these facts become the immutable restart attestation;
3. one summary is rendered from the source notes plus the verified smoke list;
4. immediately after the planned restart/shutdown lifecycle message, those
   exact bytes are automatically published to the Discord guild channel discovered from
   the current typed production config at
   `approvals.gateway_owner_escalation.owner_channel_id`;
5. the same bytes are published in the coordinating Codex task;
6. both delivery receipts are bound to the same summary digest, version, and
   SHA; only then is the terminal completion receipt and healthy status valid.

The wrapper calls `restart-prepare` only before its systemd mutation and
`restart-complete` only after exact identity, marker consumption, service
health, and smoke checks. An already-active retry may reconcile a pre-existing
attempt or replay an existing attestation, but it cannot create the missing
pre-restart evidence, and replay succeeds only when the supplied current
systemd `InvocationID` is the attested post-restart invocation. Therefore that
fast path cannot announce merely because the target happens to be active. The
smoke receipt, all later summary receipts, status, health, and terminal
completion are chained to the restart attestation.
Any embedded version, SHA, idempotency key, or receipt-link mismatch fails
closed even if a record was placed under an expected filename.

Discord delivery reserves its attempt before network I/O and writes a sealed
request containing the exact rendered bytes, their digest, the restart
attestation digest, and its post-restart systemd `InvocationID`. The restarted
gateway watches that private release state only after it has finished the
restart/startup lifecycle notifications. It compares the request to its own
current systemd `INVOCATION_ID`; a missing or later invocation blocks before
the Relay. Once the deploy coordinator has confirmed the exact active SHA and
production smokes, the gateway sends the request only through its live Relay
to the privileged Discord connector. A native Discord adapter or direct REST
fallback is rejected on this production path. A Discord delivery receipt,
terminal completion, and healthy status are invalid unless they bind to that
exact persisted gateway request.

The connector receives a stable idempotency key derived from `(version, exact
SHA)`. A successful replay returns the existing exact Discord message ID. A
timeout or crash after connector acceptance can therefore be reconciled with
the same key without a second Discord mutation. Missing relay authority,
identity or destination drift, malformed receipts, and unconfirmed dispatch
never create delivery truth. No channel ID or user-facing `HERMES_*` setting
is added.

Generic upstream `hermes send --to discord:<channel> --json` remains a separate
edge: the sealed package includes Discord's plugin manifest so PluginManager
can discover its registered standalone sender. That path uses the existing bot
configuration for ordinary Hermes installations. Muncho production keeps the
privileged writer policy intact and does not use this direct sender for release
announcements.

The legacy production release wrapper reserves the version/SHA mapping before
activation. It invokes `announce-after-smoke` only after the restarted service
is active and both the live Git HEAD and `.codex-source-commit` equal the target
SHA. Rollback, restart failure, unhealthy service, stale marker, or mismatched
identity paths exit before the announcement call. An announcement failure does
not falsely roll back a healthy runtime; it records
`deploy_smoke_passed_release_announcement_blocked` and leaves release
completion pending reconciliation.

Codex task delivery follows the same explicit sequence. It is intentionally an
acknowledged coordinator workflow because release code cannot observe the
Codex UI and must not infer that a message was published:

1. run `coordinator-prepare` with the exact version, SHA, private state path,
   and coordinating task ID;
2. publish the returned `summary` value byte-for-byte in that task and retain
   the task API/UI's stable message reference;
3. run `coordinator-complete` with that message reference plus the returned
   `summary_sha256` and `attempt_receipt_sha256`;
4. only the resulting completion receipt permits `health` to report
   `healthy=true`.

`coordinator-prepare` creates only an append-only attempt receipt. It reports
`reserved` on the first call and `reconciliation_required` after a crash or
retry; neither state claims publication. The coordinator must inspect the task
before retrying a post. `coordinator-complete` records the explicit
acknowledgement and finalizes atomically as two replay-safe receipts. A crash
between them is recovered by replaying the same command; a different task,
message reference, summary digest, or attempt digest fails closed.

The human summary contains only:

- Muncho version and exact SHA;
- 3–6 user-facing changes;
- the successful production checks/smokes;
- known limitations and a rollback note only when source metadata supplies
  them.

Raw tool output and logs are not summary inputs. Runtime code validates the
typed structure and integrity only; it does not classify or interpret the
meaning of note text.

`muncho-release inspect`, `reserve`, `restart-prepare`, `restart-complete`,
`announce-after-smoke`,
`coordinator-prepare`, `coordinator-complete`, `status`, and `health` expose the
package to release coordinators. `announce-after-smoke` waits for the
gateway-recorded Discord receipt, then returns the same rendered summary and
digest for the task workflow. Until the task acknowledgement is recorded it
truthfully reports `codex_task_summary_pending`; it cannot emit a terminal
completion or healthy result. Production config is passed as an explicit
file/path or typed mapping; secrets and behavioral release settings are never
sourced from a new environment variable.

## R1 sequencing

R1 is the already-green exact release
`5564ec24a48d819e8ba0dd924bdb82ca5064ed4c` and is published as `v2.3.1` only
after its independent PROD deploy/smoke. Its coordinator publishes the v2.3.1
announcement manually. This package must not delay, mutate, merge with, or
deploy R1. Merge the package after R1 completes; automatic post-restart
announcements begin with the first eligible packaged release, `v2.3.2`.
