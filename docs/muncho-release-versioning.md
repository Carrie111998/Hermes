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

1. the exact SHA is deployed and all required production checks/smokes pass;
2. one summary is rendered from the source notes plus the verified smoke list;
3. immediately after the planned restart/shutdown lifecycle message, those
   exact bytes are automatically published to the Discord guild channel discovered from
   the current typed production config at
   `approvals.gateway_owner_escalation.owner_channel_id`;
4. the same bytes are published in the coordinating Codex task;
5. both delivery receipts are bound to the same summary digest, version, and
   SHA; only then is the terminal completion receipt and healthy status valid.

Discord delivery reserves its attempt before network I/O using the idempotency
key derived from `(version, exact SHA)`. A retry returns the existing successful
receipt. A crash after reservation is reconciliation-required and never sends
a blind duplicate. No channel ID or user-facing `HERMES_*` setting is added.

The legacy production release wrapper reserves the version/SHA mapping before
activation. It invokes `announce-after-smoke` only after the restarted service
is active and both the live Git HEAD and `.codex-source-commit` equal the target
SHA. Rollback, restart failure, unhealthy service, stale marker, or mismatched
identity paths exit before the announcement call. An announcement failure does
not falsely roll back a healthy runtime; it records
`deploy_smoke_passed_release_announcement_blocked` and leaves release
completion pending reconciliation.

Codex task delivery follows the same explicit sequence: reserve the typed
delivery attempt, publish the draft's exact `summary` bytes, then record the
returned message reference. An already-reserved attempt must be reconciled
against the task rather than blindly posting again.

The human summary contains only:

- Muncho version and exact SHA;
- 3–6 user-facing changes;
- the successful production checks/smokes;
- known limitations and a rollback note only when source metadata supplies
  them.

Raw tool output and logs are not summary inputs. Runtime code validates the
typed structure and integrity only; it does not classify or interpret the
meaning of note text.

`muncho-release inspect`, `reserve`, `announce-after-smoke`, `status`, and
`health` expose the package to release coordinators. The automatic command
returns the same rendered summary and digest sent to Discord, allowing the
coordinating task to publish identical verified content. Production config is
passed as an explicit file/path or typed mapping; secrets and behavioral
release settings are never sourced from a new environment variable.

## R1 sequencing

R1 is the already-green exact release
`5564ec24a48d819e8ba0dd924bdb82ca5064ed4c` and is published as `v2.3.1` only
after its independent PROD deploy/smoke. Its coordinator publishes the v2.3.1
announcement manually. This package must not delay, mutate, merge with, or
deploy R1. Merge the package after R1 completes; automatic post-restart
announcements begin with the first eligible packaged release, `v2.3.2`.
