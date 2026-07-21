# Per-chat parallel drain — deploy hold handoff

Status: **DEPLOYED, VERIFIED, PAUSED. LIVE DRAIN NOT RELEASED.**

## Deployed state

- Source: `91868a5c293b746946f0f7935e7983d0a2fc4576`
- Deploy transaction: `tgg-christopher-20260721-160720-4b92cb29cb` (`committed`)
- Receipt: `/Users/pcloffice/pcl-client-data/tgg/deploy/20260721T160712Z-hermes`
- `deploy.json`: `ok=true`
- `verify.json`: `ok=true`; full isolated smoke passed with zero client mutations and zero external outbound sends.
- Service: `active/running`, `NRestarts=0`, stable across the post-deploy observation window.
- Unit: package invocation plus `--site-concurrency 4 --chat-batch-size 25`.
- Runtime status: `scheduler_mode=per-chat-parallel`, `site_concurrency=4`, `chat_batch_size=25`, total `3172`.
- Inbox at hold: completed `1052`, failed `2`, pending `1270`, skipped `848`, processing `0`; total conserved at `3172`.
- Current transaction candidate count under the managed remote root: `0`.
- Capture bridge: active.

## Safety boundary held

- `TGG_DEMO_MANAGEMENT_ONLY=true` is present in the live `.env` and the running process environment.
- `CHRISTOPHER_TGG_PS_SERVICE_TOKEN` is absent from both.
- `pa.enabled=true`.
- Processing gate: `enabled=true`, generation `3`, authority verdict `65341699-02d4-46ca-ab73-80a06eb87927`.
- Rung `3b` remains released to exactly:
  - `120363426509183563@g.us`
  - `120363407903158826@g.us`
- Paused environment snapshot: `/home/pclaw/.hermes-christopher-tgg/.env.paused-snapshot-20260721-152823`.
- Pre-build runtime backup: `/home/pclaw/.hermes-christopher-tgg/runtime/backups/b6240e2d-20260721T111624Z`.

The demo pause was not removed. The site backlog remains intentionally undrained. Live concurrency, seq-3030 management-priority, conservation-under-drain, and interrupt/resume canaries remain gated on Teren's explicit live go.

## Verification completed while paused

- Cross-provider review cleared the core build and each corrective deploy change.
- `tests/gateway/test_durable_jsonl_consumer.py`: 15 passed.
- Concurrent replay cross-talk regression: 1 passed; replay adapter, home-channel context, and session context remained isolated across two simultaneous chats.
- Management continuity regression confirms two turns from one management chat reuse `agent:live-drain:persistent-chat` and remain single-chat batches.
- A real crash during the first deploy exercised startup reconciliation: `998` stranded processing rows returned to pending or reconciled completed; total remained `3172`, with no deletion or bulk terminal-state flip.

## Availability incident and fixes

The first deploy briefly crash-looped the consumer because systemd passed the new concurrency flags to a mismatched invocation/binary. Capture remained active, so no messages were lost; the outage lasted about 3.5 minutes. Immediate rollback restored service. The root cause was absolute script invocation setting `sys.path` inside `gateway/`; `ab4b6567b` changed the service to `python -m gateway.durable_jsonl_consumer`. A second full-verify import failure was fixed by `def336b60`, which supplies the app root through `PYTHONPATH` for the isolated smoke. Both fixes received independent Claude review.

Marshal's exact-recovery path was also repaired and released at `c5cc26aed`: 600-second SSH timeout, recovered-state cleanup, conditional candidate cleanup, and fail-closed transaction handling. Exact recovery was exercised on both failed transaction IDs before the successful deploy.

## Cross-source dedupe assumption failed

`chat::WA-message-id` **cannot** dedupe export against capture:

- Export message IDs are synthetic, e.g. `EXPORT_BACKFILL_HG_1783071670_...`.
- Capture message IDs are WhatsApp-native, e.g. `3EB0...` / `AC39...`.
- Existing overlap evidence in `deduped-samples.jsonl` joins on chat + timestamp + body + media type, not message ID.

Backfill WB `5ac490b5` therefore remains blocked until import assigns a durable `canonical_message_id` from that cross-source matcher. This row did not build that contract.

## Deliberate non-changes and follow-up traps

- `TGG_PERSISTENT_CHAT_SESSION_SCOPE` remains `management`. Scope `all` is still WB `24c53c9c` and was not folded into this deploy.
- `DeliveryRouter.adapters` is rebound after construction in `run.py`. It is correct today, but fragile if future `__init__` ordering changes; preserve or eliminate that ordering dependency when the file is next touched.
- Out-of-root `.env` files are not covered by deploy drift checks. That predates this change and remains tracked under WB `d4e194fc`.

