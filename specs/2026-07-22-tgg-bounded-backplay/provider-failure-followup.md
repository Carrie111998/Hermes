# Bounded backplay provider-failure follow-up

WB: `ef6e0ad2-c0b2-48b3-9220-b58df375eabb`

## Live evidence

The bounded TGG run that exposed this defect is preserved outside the source
repository at:

- `/Users/pcloffice/pcl-client-data/tgg/backplay/20260722T093841Z-b812dfb0/run-report.json`
- `/Users/pcloffice/pcl-client-data/tgg/backplay/20260722T093841Z-b812dfb0/live-run-audit.json`
- `/Users/pcloffice/pcl-client-data/tgg/backplay/20260722T093841Z-b812dfb0/would-send-by-chat-case.json`

That run selected 499 inbox rows, processed 498 pending rows, captured 194
provider-authentication notices, recorded zero PA turns for the run window, and
still returned `ok=true`. The durable audit stored only the capture count. The
third artifact had to be reconstructed later from request dumps because the
captured bodies were no longer present in the run artifact.

## Root cause and correction boundary

`bounded-backplay` accepted an explicit `--config` path but constructed its
`GatewayRunner` from the ambient Hermes home. The ordinary consumer was started
with the Christopher Hermes home and therefore resolved
`openai-direct-primary/gpt-5.6-luna`; the one-shot bounded invocation fell back
to the default Hermes home, resolved an empty model, and sent the request to
OpenRouter without authentication.

The correction binds runner construction to the explicit config's runtime
home, distinguishes a provider/model failure from a legitimate consumed-no-turn
outcome, requeues a failed claimed batch, and persists every captured body with
its batch/chat/message/turn attribution in the audit before a success return.

This slice changes no inbox schema and performs no client operation. It does
not reset or rerun the TGG window, flip processing, alter the bridge allowlist,
change the constitution, or change pause state. Those remain separate live
actions requiring their own authorization.

## Shape audit

- Existing primitive extended: the shared durable consumer and its existing
  bounded audit; no parallel runner or sidecar format.
- Actor/layer: Hermes durable consumer, before it terminalizes selected inbox
  rows.
- Success signal: provider failures return nonzero, the owned batch is pending,
  and the audit is `ok=false` with the provider error and captured outputs.
- Legitimate no-turn signal: no provider error evidence and no completed turn;
  rows may remain terminal skipped as before.
- Blast boundary: the bounded one-shot command only. Ordinary live-consumer
  runner construction receives the same explicit config it already consumes.
- Rollback: revert the implementation commit. No data rollback or migration is
  required; the audit additions are backward-compatible JSON fields.
